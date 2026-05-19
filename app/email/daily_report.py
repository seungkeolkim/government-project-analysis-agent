"""단체 Daily Report 도메인 service (Phase A-3 / task 00125).

본 모듈은 운영자가 cron 으로 예약한 시점에 \"마지막 발송 이후 누적된 공고 변화
(snapshot diff)\" 를 정리해 admin 명단에게 메일로 발송하는 흐름의 핵심 로직을
담는다. 모듈은 후속 subtask 가 부분씩 채워 가며, 현재 채워진 함수 / 책임은
다음과 같다:

    - 후속 단계가 공용으로 쓸 dataclass 3종 정의 (subtask 00125-3).
    - 누적 구간 계산 함수 ``compute_aggregation_window`` (subtask 00125-3).
    - ``AggregationWindow`` 구간 내 snapshot 들을 시간순 reduce 머지해 5종
      카테고리별 공고 메타를 만드는 ``aggregate_snapshots`` (subtask 00125-4).

``build_daily_report_*`` / ``prepare_and_send_daily_report`` /
``collect_admin_recipient_emails`` 는 후속 subtask(00125-5 / 6) 가 같은 모듈에
추가한다. \"스텁만 두지 말고 함수 자체를 다음 subtask 로 이월\" — subtask
guidance 그대로.

설계 노트 참조:
    - ``docs/phase_a3_design_note.md`` §1 (시간 컬럼 = ``created_at``),
      §2 (``merge_snapshot_payload`` reduce 재사용), §3 (5종 카테고리 키 표기),
      §4 (fallback_days=7 default), §14 (SystemSetting 키 + 저장 포맷 = ISO-8601
      UTC).
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
from functools import reduce
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.backup.service import get_setting
from app.db.models import Announcement, AnnouncementStatus, as_utc
from app.db.repository import (
    list_announcements_by_ids,
    list_snapshots_created_in_range,
)
from app.db.snapshot import (
    CATEGORY_CONTENT_CHANGED,
    CATEGORY_NEW,
    merge_snapshot_payload,
    normalize_payload,
)
from app.email.constants import (
    DEFAULT_APP_PUBLIC_BASE_URL,
    SETTING_KEY_APP_PUBLIC_BASE_URL,
    SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
)
from app.email.message_builder import build_announcement_detail_url


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


# ──────────────────────────────────────────────────────────────
# 누적 머지 — 5종 카테고리별 AggregatedSnapshotPayload 빌드
# ──────────────────────────────────────────────────────────────


# payload 의 transition 카테고리 키(한글) → AggregatedSnapshotPayload 의 필드명
# (영문) 매핑. design note §3 의 매핑 표를 1:1 로 옮긴 single source of truth.
# 키 표기는 ``app.db.snapshot.TRANSITION_TO_LABELS`` 의 한글 라벨에 맞추고,
# 필드명은 ``AnnouncementStatus`` enum 의 영문 ``name.lower()`` 컨벤션을 따라
# 한 자리에 모아 둔다 — 새 status 가 도입되면 본 dict 만 갱신한다.
_TRANSITION_PAYLOAD_KEY_TO_FIELD: dict[str, str] = {
    f"transitioned_to_{AnnouncementStatus.SCHEDULED.value}": (
        "transitioned_to_received_scheduled"
    ),
    f"transitioned_to_{AnnouncementStatus.RECEIVING.value}": (
        "transitioned_to_receiving"
    ),
    f"transitioned_to_{AnnouncementStatus.CLOSED.value}": (
        "transitioned_to_closed"
    ),
}


def aggregate_snapshots(
    session: Session,
    window: AggregationWindow,
) -> AggregatedSnapshotPayload:
    """``window`` 구간의 ScrapeSnapshot 들을 시간순 reduce 머지해 5종 카테고리별
    공고 메타를 만든다.

    동작 순서 (prompt §3 의사 코드 + design note §2 reduce 패턴):
        1. ``list_snapshots_created_in_range(window.from_dt, window.to_dt)`` 로
           구간 내 snapshot list 를 ``created_at ASC`` 로 fetch.
        2. ``reduce(merge_snapshot_payload, payloads, normalize_payload(None))``
           — dashboard ``build_section_a`` 와 동일 패턴. 초깃값을 정규형 빈
           dict 으로 두면 첫 step 이 ``merge(empty, first) == normalize(first)``
           로 idempotent 하다.
        3. 머지 결과의 5종 카테고리에서 announcement_id union 을 만든다.
        4. ``list_announcements_by_ids`` 로 한 번의 IN 쿼리만 발생시킨다 (N+1
           회피). ``is_current`` 필터를 적용하지 않아 변경 전 (history) row 도
           그대로 포함된다 (prompt §3 주의사항).
        5. 카테고리별로 ``AnnouncementSummary`` list 를 만들어
           ``AggregatedSnapshotPayload`` 를 반환. ``total_count`` 는 5 리스트
           길이의 합 (중복 제거 없음 — 같은 공고가 \"내용 변경\" 과 \"접수중
           전이\" 양쪽에 있을 수 있다).

    카테고리 cap (50건 등) 은 본 함수가 적용하지 않는다 — 본문 빌더(00125-5)가
    UI 표시 단계에서 cap 을 적용한다. 본 함수는 cap 없이 full 리스트를 반환해
    빌더가 \"외 N건\" 안내문을 계산할 수 있게 한다 (subtask guidance 명시).

    Args:
        session: ORM 세션. 본 함수는 read-only — commit / flush 하지 않는다.
        window: ``compute_aggregation_window`` 의 반환값. 빈 구간(``snapshot_count
            ==0``)으로 본 함수가 직접 호출되는 일은 정상 흐름에서 없지만, 호출자가
            잘못 만든 window 가 들어와도 빈 ``AggregatedSnapshotPayload`` 를
            반환하도록 defensive 하게 동작한다.

    Returns:
        ``AggregatedSnapshotPayload`` — 5종 카테고리별 ``AnnouncementSummary``
        list + ``total_count``. 카테고리 안에서는 announcement_id 오름차순으로
        정렬된다 (``normalize_payload`` 의 asc 정렬 + ``list_announcements_by_ids``
        의 id ASC 가 그대로 전달됨).
    """
    # 1. 구간 내 snapshot rows — created_at ASC.
    snapshots_in_window = list_snapshots_created_in_range(
        session,
        from_exclusive=window.from_dt,
        to_inclusive=window.to_dt,
    )

    # 2. reduce 누적 머지. normalize_payload(None) 은 정규형 빈 dict — 첫 step
    #    이 merge(empty, first) == normalize(first) 로 자연스럽게 시작된다.
    merged_payload: dict[str, Any] = reduce(
        merge_snapshot_payload,
        (snapshot.payload for snapshot in snapshots_in_window),
        normalize_payload(None),
    )

    # 3. 5종 카테고리에서 announcement_id union — 단일 IN SELECT 의 입력.
    announcement_id_union = _collect_announcement_id_union(merged_payload)

    # 4. 일괄 SELECT — is_current 필터 없이 PK 로 직접 (이력 row 포함).
    announcement_meta_list = list_announcements_by_ids(
        session, announcement_ids=announcement_id_union
    )
    announcement_meta_map: dict[int, Announcement] = {
        ann.id: ann for ann in announcement_meta_list
    }

    # 5. detail_url 조립용 public_base_url 로드 — forwarding 패턴과 동일.
    #    row 가 없으면 코드 fallback 상수 사용.
    public_base_url = (
        get_setting(session, SETTING_KEY_APP_PUBLIC_BASE_URL)
        or DEFAULT_APP_PUBLIC_BASE_URL
    )

    # 6. plain 카테고리 (new / content_changed) — payload 의 int list 그대로 사용.
    new_items = _build_summaries(
        announcement_ids=(int(aid) for aid in merged_payload.get(CATEGORY_NEW, [])),
        announcement_meta_map=announcement_meta_map,
        public_base_url=public_base_url,
    )
    content_changed_items = _build_summaries(
        announcement_ids=(
            int(aid) for aid in merged_payload.get(CATEGORY_CONTENT_CHANGED, [])
        ),
        announcement_meta_map=announcement_meta_map,
        public_base_url=public_base_url,
    )

    # 7. transition 3종 — payload 의 [{id, from}, ...] 에서 id 만 추출.
    #    field_name 별로 결과 list 를 채운 뒤 이름으로 dispatch.
    transition_field_to_items: dict[str, list[AnnouncementSummary]] = {
        field_name: [] for field_name in _TRANSITION_PAYLOAD_KEY_TO_FIELD.values()
    }
    for payload_key, field_name in _TRANSITION_PAYLOAD_KEY_TO_FIELD.items():
        entries = merged_payload.get(payload_key, [])
        transition_field_to_items[field_name] = _build_summaries(
            announcement_ids=(int(entry["id"]) for entry in entries),
            announcement_meta_map=announcement_meta_map,
            public_base_url=public_base_url,
        )

    received_scheduled_items = transition_field_to_items[
        "transitioned_to_received_scheduled"
    ]
    receiving_items = transition_field_to_items["transitioned_to_receiving"]
    closed_items = transition_field_to_items["transitioned_to_closed"]

    # 8. total_count — 5 list 길이의 단순 합. 중복 제거 없음 (한 공고가 \"내용
    #    변경\" + \"접수중 전이\" 양쪽에 있을 수 있다 — design note §3).
    total_count = (
        len(new_items)
        + len(content_changed_items)
        + len(received_scheduled_items)
        + len(receiving_items)
        + len(closed_items)
    )

    return AggregatedSnapshotPayload(
        new=new_items,
        content_changed=content_changed_items,
        transitioned_to_received_scheduled=received_scheduled_items,
        transitioned_to_receiving=receiving_items,
        transitioned_to_closed=closed_items,
        total_count=total_count,
    )


def _collect_announcement_id_union(merged_payload: dict[str, Any]) -> set[int]:
    """머지된 payload 의 5종 카테고리에 등장하는 announcement_id 들의 union.

    ``new`` / ``content_changed`` 는 int list, transition 3종은 ``[{id, from},
    ...]`` 형식이라 두 가지 모두 다룬다. ``list_announcements_by_ids`` 의 단일
    IN 쿼리 입력으로 쓰여 N+1 을 회피하는 게 본 헬퍼의 존재 이유다.

    Args:
        merged_payload: ``merge_snapshot_payload`` reduce 결과 (정규형).

    Returns:
        announcement_id 의 set. 빈 카테고리 / 빈 payload 도 빈 set 으로 안전.
    """
    union: set[int] = set()
    union.update(
        int(announcement_id) for announcement_id in merged_payload.get(CATEGORY_NEW, [])
    )
    union.update(
        int(announcement_id)
        for announcement_id in merged_payload.get(CATEGORY_CONTENT_CHANGED, [])
    )
    for payload_key in _TRANSITION_PAYLOAD_KEY_TO_FIELD:
        for entry in merged_payload.get(payload_key, []):
            union.add(int(entry["id"]))
    return union


def _build_summaries(
    *,
    announcement_ids,
    announcement_meta_map: dict[int, Announcement],
    public_base_url: str,
) -> list[AnnouncementSummary]:
    """announcement_id iterable → ``AnnouncementSummary`` list.

    메타가 ``announcement_meta_map`` 에 없는 id 는 silent skip (DB 에서 삭제된
    공고 등 — dashboard ``_build_expand_items_for_category`` 와 같은 정책). 표시할
    수 있는 메타가 있는 row 만 본문에 들어가야 의미가 있어서, 누락분은 자연스럽게
    빠진다.

    ``announcement_id`` 의 입력 순서를 그대로 유지한다 — payload 가 이미 id ASC
    로 정렬돼 있으므로 결과 list 도 id ASC 가 된다.

    Args:
        announcement_ids: 변환 대상 id iterable. 정렬 / 중복 제거는 호출자가
            payload 의 정규형에 맡긴다.
        announcement_meta_map: ``list_announcements_by_ids`` 결과 (id → ORM row).
        public_base_url: ``build_announcement_detail_url`` 의 base.

    Returns:
        ``AnnouncementSummary`` list — 입력 순서 그대로. 메타 누락분은 제외.
    """
    summaries: list[AnnouncementSummary] = []
    for announcement_id in announcement_ids:
        announcement = announcement_meta_map.get(int(announcement_id))
        if announcement is None:
            # 메타 없는 row 는 본문에 표시할 수 없으므로 조용히 건너뛴다.
            continue
        summaries.append(
            AnnouncementSummary(
                announcement_id=announcement.id,
                canonical_project_id=announcement.canonical_group_id,
                title=announcement.title,
                source_type=announcement.source_type,
                agency=announcement.agency,
                deadline_at=announcement.deadline_at,
                # detail_url 은 본 row 의 PK 기준 (history row 라도 그 row 의
                # 자체 URL 로 — prompt §3 주의사항 \"detail_url 은 그 row 가
                # 가리키는 announcement 자체로\").
                detail_url=build_announcement_detail_url(public_base_url, announcement.id),
            )
        )
    return summaries


__all__ = [
    "AggregatedSnapshotPayload",
    "AggregationWindow",
    "AnnouncementSummary",
    "aggregate_snapshots",
    "compute_aggregation_window",
]
