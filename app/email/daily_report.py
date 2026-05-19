"""단체 Daily Report 도메인 service (Phase A-3 / task 00125).

본 모듈은 운영자가 cron 으로 예약한 시점에 \"마지막 발송 이후 누적된 공고 변화
(snapshot diff)\" 를 정리해 admin 명단에게 메일로 발송하는 흐름의 핵심 로직을
담는다. 모듈은 후속 subtask 가 부분씩 채워 가며, 본 subtask(00125-3) 는 다음만
담당한다:

    - 후속 단계가 공용으로 쓸 dataclass 3종 정의.
    - 누적 구간 계산 함수 ``compute_aggregation_window``.

``aggregate_snapshots`` / ``build_daily_report_*`` / ``prepare_and_send_daily_report``
/ ``collect_admin_recipient_emails`` 는 본 subtask 의 범위가 아니라 후속 subtask
(00125-4 / 5 / 6) 가 같은 모듈에 추가한다. \"스텁만 두지 말고 함수 자체를 다음
subtask 로 이월\" — subtask guidance 그대로.

설계 노트 참조:
    - ``docs/phase_a3_design_note.md`` §1 (시간 컬럼 = ``created_at``),
      §3 (5종 카테고리 키 표기), §4 (fallback_days=7 default), §14
      (SystemSetting 키 + 저장 포맷 = ISO-8601 UTC).
    - ``phase_a3_prompt.md`` §3 \"Aggregation 로직 — app/email/daily_report.py 신규\".

시간 처리 컨벤션 (subtask guidance 명시):
    - ``compute_aggregation_window`` 가 다루는 모든 ``datetime`` 은
      **timezone-aware UTC** 이다. naive datetime 입력은 ``app.timezone.as_utc``
      를 통해 UTC tz 부착 후 비교한다.
    - SystemSetting ``email.daily_report.last_sent_at`` 은 ISO-8601 문자열로
      저장되며 ``datetime.fromisoformat`` 로 파싱한다. 파싱 실패는 \"값 없음\"
      과 동일하게 첫 발송(``is_first_send=True``) 분기로 fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from app.backup.service import get_setting
from app.db.models import as_utc
from app.db.repository import list_snapshots_created_in_range
from app.email.constants import SETTING_KEY_DAILY_REPORT_LAST_SENT_AT


# ──────────────────────────────────────────────────────────────
# 공용 dataclass — 후속 subtask 가 import 해 채워 나간다
# ──────────────────────────────────────────────────────────────


@dataclass
class AggregationWindow:
    """Daily report 1회의 누적 구간 + 메타.

    ``compute_aggregation_window`` 가 반환하며, ``aggregate_snapshots``
    (subtask 00125-4) / 본문 빌더 (00125-5) / ``prepare_and_send_daily_report``
    (00125-6) 의 입력으로 그대로 쓰인다.

    Attributes:
        from_dt: 누적 구간 시작 시각 (배타). UTC tz-aware datetime.
            ``SystemSetting[\"email.daily_report.last_sent_at\"]`` 가 있으면
            그 값을 파싱한 시각, 없거나 파싱 실패면 ``to_dt - fallback_days``.
        to_dt: 누적 구간 끝 시각 (포함). UTC tz-aware datetime. 호출자가
            전달한 ``now`` 와 동일하다 (\"발송 시각까지\" 시맨틱).
        snapshot_count: 구간 내 ``scrape_snapshots.created_at`` 이 들어 있는
            row 수. ``compute_aggregation_window`` 는 이 값이 0 이면 None 을
            반환하므로, AggregationWindow 가 생성되면 ``snapshot_count >= 1`` 이
            보장된다.
        is_first_send: ``last_sent_at`` SystemSetting 이 NULL / 빈 값 / 파싱
            실패였던 \"첫 발송\" 케이스면 True. 본문 빌더가 헤더 박스에 \"최초
            발송 — 직전 N일치 포함\" 안내를 노출한다.
        fallback_days: 첫 발송 분기에서 적용된 fallback 일수 (보통 7).
            ``is_first_send=False`` 이면 None.

    frozen 정책:
        ``@dataclass(frozen=False)`` (default). 사용 시점에 mutate 하지 않지만,
        후속 코드가 디버깅 / 로깅 목적으로 필드를 임시 수정해야 할 여지를
        남긴다 (subtask guidance 명시).
    """

    from_dt: datetime
    to_dt: datetime
    snapshot_count: int
    is_first_send: bool
    fallback_days: int | None


@dataclass
class AnnouncementSummary:
    """본문에 표시할 공고 1건의 최소 메타.

    ``aggregate_snapshots`` (subtask 00125-4) 가 5종 카테고리별로 모으는
    list 의 원소이며, 본문 빌더 (00125-5) 가 텍스트 / HTML 줄로 변환한다.
    공고 전체 필드를 다 들고 다니지 않고, 메일 본문에 실제로 노출되는 5개 +
    1 (announcement_id, canonical_project_id) 만 갖는다.

    Attributes:
        announcement_id: 본 row 가 가리키는 ``announcements.id``.
            ``aggregate_snapshots`` 는 ``is_current`` 필터 없이 PK 로 직접
            SELECT 해 이력 row 도 포함한다 (design note §3, prompt §3).
        canonical_project_id: 대응하는 ``CanonicalProject.id``. canonical 이
            없는 단독 공고의 경우 None.
        title: 공고 제목. 본문에 그대로 노출되므로 trim 등 가공은 호출자가
            한다 (본 dataclass 는 단순 컨테이너).
        source_type: 공고 출처 (예: ``iris`` / ``ntis``). 본문에서는 라벨용
            으로만 사용.
        agency: 발주 기관. 메타가 누락된 공고는 None.
        deadline_at: 마감 시각. UTC tz-aware datetime. 미정이면 None. 본문
            빌더가 KST 변환해 표시한다.
        detail_url: 메일에 박힐 공고 상세 페이지 URL. ``aggregate_snapshots``
            가 ``build_announcement_detail_url`` 로 사전 생성해 채운다.

    frozen 정책: ``@dataclass(frozen=False)`` (default). 동일 사유.
    """

    announcement_id: int
    canonical_project_id: int | None
    title: str
    source_type: str
    agency: str | None
    deadline_at: datetime | None
    detail_url: str


@dataclass
class AggregatedSnapshotPayload:
    """5종 카테고리별 공고 리스트 + 총 카운트.

    ``aggregate_snapshots`` (subtask 00125-4) 의 반환 타입이며, 본문 빌더
    (subtask 00125-5) 의 입력이다. 5종 카테고리는 ``app.db.snapshot`` 의
    payload 키와 1:1 대응한다 (design note §3 매핑 표):

        - new → ``\"new\"``
        - content_changed → ``\"content_changed\"``
        - transitioned_to_received_scheduled → ``\"transitioned_to_접수예정\"``
        - transitioned_to_receiving → ``\"transitioned_to_접수중\"``
        - transitioned_to_closed → ``\"transitioned_to_마감\"``

    dataclass 필드명은 영문(``received_scheduled`` / ``receiving`` / ``closed``)
    이고 payload 키는 한글이다 — ``AnnouncementStatus`` enum 의 영문 name /
    한글 value 분리와 동일 컨벤션.

    Attributes:
        new: 신규 공고 list.
        content_changed: 내용이 변경된 공고 list.
        transitioned_to_received_scheduled: 접수예정 으로 전이된 공고 list.
        transitioned_to_receiving: 접수중 으로 전이된 공고 list.
        transitioned_to_closed: 마감 으로 전이된 공고 list.
        total_count: 5종 list 길이의 합 (중복 제거 없음 — 동일 공고가 \"내용
            변경\" 과 \"접수중 전이\" 양쪽에 포함될 수 있다).

    frozen 정책: ``@dataclass(frozen=False)`` (default). 동일 사유.
    """

    new: list[AnnouncementSummary]
    content_changed: list[AnnouncementSummary]
    transitioned_to_received_scheduled: list[AnnouncementSummary]
    transitioned_to_receiving: list[AnnouncementSummary]
    transitioned_to_closed: list[AnnouncementSummary]
    total_count: int


# ──────────────────────────────────────────────────────────────
# 누적 구간 계산
# ──────────────────────────────────────────────────────────────


def _parse_last_sent_at(raw_value: str | None) -> datetime | None:
    """SystemSetting 의 ``last_sent_at`` 문자열을 UTC tz-aware datetime 으로 파싱.

    저장 포맷은 ``now_utc().isoformat()`` (예: ``\"2026-05-19T00:00:00+00:00\"``)
    이다. 다음 경우는 모두 None 을 반환해 호출자가 \"첫 발송\" 분기로 fallback
    하게 한다:

        - row 없음 / NULL → ``get_setting`` 이 None 반환 → None
        - 빈 문자열 (``DEFAULT_DAILY_REPORT_LAST_SENT_AT``) → None
        - ``datetime.fromisoformat`` 가 ValueError 를 던지는 손상 값 → None +
          warning 로그 (운영 중 SystemSetting 직접 수정 등으로 깨진 케이스)

    Args:
        raw_value: ``get_setting(..., SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)``
            의 반환값. None 또는 문자열.

    Returns:
        파싱 성공 시 UTC tz-aware ``datetime``. 위 3 케이스 어디든 해당하면
        None.
    """
    if raw_value is None:
        return None
    trimmed = raw_value.strip()
    if trimmed == "":
        return None
    try:
        parsed = datetime.fromisoformat(trimmed)
    except ValueError:
        # 손상 / 형식 오류. 잘못된 값으로 발송을 막아 무한 SKIPPED 가 발생하지
        # 않도록 첫 발송 분기로 강제 회복한다. 운영자가 이력에서 이 시점을 확인
        # 할 수 있게 warning 로그를 남긴다.
        logger.warning(
            "SystemSetting {!r} 값을 ISO-8601 로 파싱하지 못했습니다 ({!r}). "
            "첫 발송(fallback) 분기로 처리합니다.",
            SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
            raw_value,
        )
        return None
    # SQLite SELECT 또는 손수 입력으로 tz 정보가 없는 naive 값이 들어와도 UTC 로
    # 정규화해 비교 안전성을 보장한다.
    return as_utc(parsed)


def compute_aggregation_window(
    session: Session,
    *,
    now: datetime,
    fallback_days: int = 7,
) -> AggregationWindow | None:
    """누적 구간 ``(last_sent_at, now]`` 을 계산한다.

    동작 순서 (prompt §3 의사 코드 + design note §1·§7 정책 반영):
        1. ``SystemSetting[\"email.daily_report.last_sent_at\"]`` 로드.
        2. 값이 있으면 ``from_dt = parsed`` , ``is_first_send=False`` ,
           ``fallback_days=None``.
           값이 없거나 파싱 실패면 ``from_dt = now - fallback_days`` ,
           ``is_first_send=True`` , ``fallback_days=fallback_days``.
        3. ``to_dt = now`` (호출자가 ``now_utc()`` 를 전달하는 게 표준).
        4. ``list_snapshots_created_in_range(session, from_dt, to_dt)`` 의
           ``len`` 으로 누적 후보 row 수를 센다.
        5. 0건이면 None 반환 — 호출자(``prepare_and_send_daily_report``) 가
           ``EmailDailyReportRun.status='skipped'`` 분기로 들어간다.
        6. 1건 이상이면 ``AggregationWindow`` 인스턴스 반환.

    시간 처리:
        모든 datetime 비교는 UTC tz-aware 로 수행한다. ``now`` 도 UTC tz-aware
        가 전제이며, naive 가 들어와도 ``as_utc`` 로 자동 부착한다.

    Args:
        session: SystemSetting / ScrapeSnapshot 조회용 ORM 세션. 본 함수는
            읽기만 하고 commit 하지 않는다.
        now: 발송 시각 (UTC tz-aware datetime). 잡 발화 시각 또는 manual
            트리거 시각. 테스트는 고정 시각을 주입해 재현성을 확보한다.
        fallback_days: 첫 발송 (``last_sent_at`` 부재) 시 직전 N일치를 누적
            대상 구간으로 잡는다. design note §4 결정에 따라 default 7.

    Returns:
        구간 내 snapshot 이 1건 이상이면 ``AggregationWindow``. 0건이면 None.
    """
    # 외부 입력 정규화 — naive 가 들어와도 비교 안전성 보장.
    now_utc_value = as_utc(now)

    # 1. SystemSetting 로드 + 파싱.
    raw_last_sent = get_setting(session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)
    parsed_last_sent = _parse_last_sent_at(raw_last_sent)

    # 2. 첫 발송 분기 결정. 파싱 실패도 \"첫 발송\" 으로 회복.
    if parsed_last_sent is None:
        is_first_send = True
        applied_fallback_days: int | None = fallback_days
        from_dt = now_utc_value - timedelta(days=fallback_days)
    else:
        is_first_send = False
        applied_fallback_days = None
        from_dt = parsed_last_sent

    # 3. to_dt 는 항상 now.
    to_dt = now_utc_value

    # 4. 구간 내 snapshot row 수 카운트. 본 헬퍼는 rows 자체를 반환해
    #    aggregate_snapshots(00125-4) 가 동일 SELECT 를 재사용할 수 있게 한다.
    #    구간이 좁아 (보통 하루) row 수가 적어 len() 의 비용은 무시 가능.
    snapshots_in_window = list_snapshots_created_in_range(
        session,
        from_exclusive=from_dt,
        to_inclusive=to_dt,
    )
    snapshot_count = len(snapshots_in_window)

    # 5. 빈 구간 → None (호출자가 SKIPPED 분기).
    if snapshot_count == 0:
        return None

    # 6. 정상 구간 → AggregationWindow.
    return AggregationWindow(
        from_dt=from_dt,
        to_dt=to_dt,
        snapshot_count=snapshot_count,
        is_first_send=is_first_send,
        fallback_days=applied_fallback_days,
    )


__all__ = [
    "AggregatedSnapshotPayload",
    "AggregationWindow",
    "AnnouncementSummary",
    "compute_aggregation_window",
]
