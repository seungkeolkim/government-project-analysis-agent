"""대시보드 B 섹션 — 조만간 변화 예정 (1개월 이내 활성 공고) 빌더 (Phase 5b / task 00042-4).

배경 (사용자 원문):
    B 섹션은 ``to`` (기준일) 시점 기준 향후 30일 이내에 접수 시작 또는 마감이
    예정된 ``is_current=True`` 활성 공고를 두 그룹으로 나눠 표시한다 — DB select
    기반이라 ``ScrapeSnapshot`` 의 가용성 fallback 과 무관하게 항상 동작한다.

설계 근거 (``docs/dashboard_design.md``):
    - §7.1 쿼리 — ``list_soon_to_open_announcements`` /
      ``list_soon_to_close_announcements`` 두 헬퍼 (``app.db.repository``).
    - §7.2 ``to`` 가 과거인 경우 — 안내문 \"기준일이 과거라 표시되는 정보는 현재
      기준이며 정확하지 않을 수 있습니다\" + 정상 표시. 이력 row 활용은 범위 밖
      (사용자 원문 주의사항).

API 표면:
    - :class:`SectionBRow` — 행 1개의 표시 데이터 (제목 / 소스 / 마감일).
    - :class:`SectionBData` — 라우트가 템플릿에 전달하는 dict 표현의 dataclass.
    - :func:`build_section_b` — 단일 진입점.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.db.repository import (
    list_soon_to_close_announcements,
    list_soon_to_open_announcements,
)
from app.timezone import now_kst, to_kst


# ──────────────────────────────────────────────────────────────
# 안내문 — 사용자 원문 §4.3 (c) / §7.2 그대로
# ──────────────────────────────────────────────────────────────


SECTION_B_PAST_BASE_DATE_NOTICE: str = (
    "기준일이 과거라 표시되는 정보는 현재 기준이며 정확하지 않을 수 있습니다."
)


# ──────────────────────────────────────────────────────────────
# Public dataclass — UI 가 소비하는 형태
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SectionBRow:
    """B 섹션 한 행의 표시 데이터.

    Attributes:
        announcement_id: 상세 링크용 PK.
        title:           공고 제목.
        source_type:     \"IRIS\" / \"NTIS\" 등 — 배지 표시.
        agency:          주관 기관명 (없으면 None).
        deadline_at:     마감 시각 UTC tz-aware datetime — 템플릿이 ``kst_date``
                         필터로 표시. 본 dataclass 자체는 tz 변환을 하지 않는다.
        received_at:     접수 시작 시각 UTC. 접수예정 그룹 표시 보조용 (템플릿이
                         ``kst_date`` 필터로 표시 — 미사용시 무시).
        d_day_label:     기준일 ``to_date`` (또는 ``to`` 가 과거가 아니면 KST 오늘)
                         시점에서 본 행의 핵심 일자 (접수예정은 ``received_at``,
                         마감예정은 ``deadline_at``) 까지 남은 일수 라벨. 형식
                         예시: ``\"D-1\"`` / ``\"D-Day\"`` / ``\"D+3\"``. 핵심 일자가
                         None 이면 빈 문자열 (정상 흐름에선 발생하지 않음 —
                         repository 가 None 인 row 를 걸러서 fetch 하므로).
    """

    announcement_id: int
    title: str
    source_type: str
    agency: str | None
    deadline_at: datetime | None
    received_at: datetime | None
    d_day_label: str


@dataclass(frozen=True)
class SectionBData:
    """B 섹션 컨텍스트 — ``dashboard.html`` 의 section_b placeholder 가 사용.

    Attributes:
        soon_to_open:        향후 30일 이내 접수 시작 예정 공고 list (받음일자 임박순).
        soon_to_close:       향후 30일 이내 마감 예정 공고 list (마감일자 임박순).
        days_window:         구간 길이 (일). UI 의 헤더 \"향후 N일\" 표시에 사용.
        is_to_in_past:       ``to`` 가 KST 오늘보다 과거인지 — 안내문 분기.
        past_notice_message: ``is_to_in_past`` 일 때 노출할 안내문. 그 외엔 빈
                             문자열.
        soon_to_open_count:  ``len(soon_to_open)`` 를 명시 필드로 노출. 템플릿이
                             ``soon_to_open|length`` 대신 본 필드를 직접 표시해
                             가독성을 높인다 (사용자 원문 \"각각 몇건인지 표시\").
        soon_to_close_count: ``len(soon_to_close)`` 를 명시 필드로 노출.
    """

    soon_to_open: list[SectionBRow]
    soon_to_close: list[SectionBRow]
    days_window: int
    is_to_in_past: bool
    past_notice_message: str
    soon_to_open_count: int
    soon_to_close_count: int


# ──────────────────────────────────────────────────────────────
# 빌더 — 단일 진입점
# ──────────────────────────────────────────────────────────────


def build_section_b(
    session: Session,
    *,
    to_date: date,
    days: int = 30,
) -> SectionBData:
    """B 섹션의 두 그룹 + ``to`` 과거 안내문을 조립한다.

    호출 흐름:
        1. ``list_soon_to_open_announcements`` 로 [to, to+days) 구간의 접수예정
           공고 fetch.
        2. ``list_soon_to_close_announcements`` 로 같은 구간의 마감예정 공고
           fetch.
        3. ``to_date`` 가 KST 오늘보다 과거면 ``is_to_in_past=True`` + 안내문.

    Args:
        session: 호출자 세션.
        to_date: 기준일 (KST date) — B 섹션 검색의 ``to`` 시점.
        days:    구간 길이. 기본 30 (사용자 원문 \"1개월 이내\").

    Returns:
        ``SectionBData`` — 두 그룹 list + 안내문 + days_window.
    """
    soon_to_open_announcements = list_soon_to_open_announcements(
        session, to_kst_date=to_date, days=days
    )
    soon_to_close_announcements = list_soon_to_close_announcements(
        session, to_kst_date=to_date, days=days
    )

    today_kst: date = now_kst().date()
    is_to_in_past = to_date < today_kst
    past_notice_message = SECTION_B_PAST_BASE_DATE_NOTICE if is_to_in_past else ""

    # D-Day 기준 날짜 — guidance §3 그대로:
    # - 기본은 ``now_kst().date()`` (사용자가 \"지금 시점에서 며칠 남았는지\" 를 본다).
    # - ``to_date`` 가 과거 (is_to_in_past) 인 경우는 ``to_date`` 를 기준으로 둬서
    #   당시 시점의 D-Day 표기를 유지한다 (음수 D-Day 방지 — fetch 구간이
    #   [to, to+30) 라 received_at/deadline_at 의 KST 일자는 to_date 이상이므로).
    d_day_reference_date: date = to_date if is_to_in_past else today_kst

    soon_to_open_rows = [
        SectionBRow(
            announcement_id=announcement.id,
            title=announcement.title,
            source_type=announcement.source_type,
            agency=announcement.agency,
            deadline_at=announcement.deadline_at,
            received_at=announcement.received_at,
            d_day_label=_compute_d_day_label(
                target_datetime_utc=announcement.received_at,
                reference_date=d_day_reference_date,
            ),
        )
        for announcement in soon_to_open_announcements
    ]
    soon_to_close_rows = [
        SectionBRow(
            announcement_id=announcement.id,
            title=announcement.title,
            source_type=announcement.source_type,
            agency=announcement.agency,
            deadline_at=announcement.deadline_at,
            received_at=announcement.received_at,
            d_day_label=_compute_d_day_label(
                target_datetime_utc=announcement.deadline_at,
                reference_date=d_day_reference_date,
            ),
        )
        for announcement in soon_to_close_announcements
    ]

    return SectionBData(
        soon_to_open=soon_to_open_rows,
        soon_to_close=soon_to_close_rows,
        days_window=days,
        is_to_in_past=is_to_in_past,
        past_notice_message=past_notice_message,
        soon_to_open_count=len(soon_to_open_rows),
        soon_to_close_count=len(soon_to_close_rows),
    )


def _compute_d_day_label(
    *,
    target_datetime_utc: datetime | None,
    reference_date: date,
) -> str:
    """``target_datetime_utc`` 의 KST 일자와 ``reference_date`` 사이의 D-Day 라벨.

    한국식 D-Day 표기 컨벤션 그대로:
        - target 이 reference 보다 미래: ``\"D-N\"`` (N = 양수 일수).
        - target 이 reference 와 같은 날: ``\"D-Day\"``.
        - target 이 reference 보다 과거: ``\"D+N\"`` (N = 양수 일수).

    호출 정상 흐름 (B 섹션) 에서 ``list_soon_to_open_announcements`` /
    ``list_soon_to_close_announcements`` 가 반환하는 row 의 ``received_at`` /
    ``deadline_at`` 은 KST 일자 기준으로 ``reference_date`` 이상이므로 일반적인
    결과는 ``\"D-Day\"`` 또는 ``\"D-N\"`` 두 가지다. ``D+N`` 분기는 호출자가
    direct row 를 만들어 넘기는 등 비정상 경로의 방어용으로 둔다.

    Args:
        target_datetime_utc: 기준 일자 산출의 대상 (UTC tz-aware 또는 naive UTC
                             가정). None 이면 빈 문자열을 반환한다 (호출 측의
                             None-guard 부담을 줄임 — repository 가 None 을
                             걸러주지만 ORM 캐시 경로 등 안전망).
        reference_date:      비교 기준 일자 (KST date).

    Returns:
        D-Day 라벨 문자열. ``target_datetime_utc`` 가 None 이면 ``\"\"`` (빈 문자열).
    """
    if target_datetime_utc is None:
        return ""

    # to_kst 는 None 입력에서만 None 을 반환하므로 위 가드 이후엔 항상 datetime.
    target_kst = to_kst(target_datetime_utc)
    assert target_kst is not None
    target_kst_date = target_kst.date()

    diff_days = (target_kst_date - reference_date).days
    if diff_days > 0:
        return f"D-{diff_days}"
    if diff_days == 0:
        return "D-Day"
    # 비정상 경로 방어 — 음수면 \"D+N\" (지난 후 N일).
    return f"D+{-diff_days}"


__all__ = [
    "SECTION_B_PAST_BASE_DATE_NOTICE",
    "SectionBData",
    "SectionBRow",
    "build_section_b",
]
