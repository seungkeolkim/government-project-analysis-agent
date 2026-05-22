"""대시보드·데일리 리포트 메일이 공유하는 '공고 1행' 렌더링 라이브러리 (task 00136-1).

배경 (사용자 원문):
    데일리 리포트 메일의 각 공고 항목을 대시보드 Section A expand 행과 동일한
    포맷(출처 배지·현재 상태/상태 전이·공고명·접수/마감 일시)으로 통일하고,
    가능하면 대시보드와 메일의 출력 생성 코드를 공유해 "한 곳을 고치면 양쪽에
    같이 반영" 되도록 한다.

설계:
    대시보드는 ``style.css`` 의 클래스 기반 마크업, 메일은 인라인 CSS 마크업
    이라 템플릿/CSS 파일 자체는 공유할 수 없다. 공유 가능한 단위는 '공고 1행의
    데이터(view-model) + 인라인 CSS HTML 조각 렌더러' 이며, 본 모듈이 그
    single source of truth 다. 이후:

        - 데일리 리포트 메일(task 00136-2)은 본 모듈로 항목을 렌더한다.
        - 대시보드 Section A expand 행(task 00136-3)도 본 모듈로 전환한다.

    그러면 본 모듈의 렌더러 한 곳을 고치면 대시보드·메일 양쪽 공고 표현이
    동시에 바뀐다.

레이아웃 방식 (task 00139-1):
    행 컨테이너를 ``display:flex + gap`` 에서 ``<table>/<td>`` 기반으로 전환했다.
    Outlook(Word 렌더 엔진)·Gmail 등 메일 클라이언트가 ``flex`` 와 ``gap`` 을
    제거하면 항목 사이 간격이 모두 사라지기 때문이다. ``<table>/<td>`` 는 메일
    클라이언트가 안정적으로 지원하며, 셀별 명시적 ``padding`` 으로 간격을 보장한다.

컬럼 최소 너비 정책 (task 00140):
    일부 메일 클라이언트가 ``<td>`` 의 ``white-space:nowrap`` 을 제거하면
    title_cell 의 ``width:100%`` 가 나머지 셀을 최소 글자 폭까지 눌러 상태·날짜
    문자가 세로로 한 글자씩 줄바꿈된다. 이를 방지하기 위해 상태 셀·날짜 셀에
    ``min-width`` (px 절대 단위)를 추가했다:

        - 상태 셀(status_cell): ``min-width:160px`` — 전이 행의 '배지 + → + 배지'
          조합이 한 줄에 들어가도록 크기 산정 (12px 폰트 기준 '접수예정 → 접수중'
          ≈ 150px + 여유). 변경 전: 한 글자씩 줄바꿈 / 변경 후: 한 줄 표시.
        - 날짜 셀(dates_cell): ``min-width:400px`` — '접수 YYYY-MM-DD HH:MM:SS 마감 YYYY-MM-DD HH:MM:SS'
          접수·마감 두 날짜가 한 줄에 표시될 수 있도록 확장 (task 00141).
          변경 전: 185px(날짜 1개 기준) / 변경 후: 400px(날짜 2개 한 줄 기준).
        - 공고명 셀(title_cell): ``min-width:80px`` — 줄바꿈을 허용하면서 완전 붕괴만
          방지. 상대 크기(``width:100%``)는 유지해 남은 공간을 차지한다.

의존 방향:
    본 모듈은 ``app.timezone`` 과 표준 라이브러리만 import 한다. ``app.web`` /
    ``app.email`` 어느 쪽도 import 하지 않으므로 두 레이어 모두 순환 import
    없이 본 모듈을 사용할 수 있다.

배지 색상:
    출처/상태 배지 색상은 ``style.css`` 의 ``.source-iris`` / ``.source-ntis`` /
    ``.status-receiving`` / ``.status-scheduled`` / ``.status-closed`` 값을
    그대로 옮긴 상수 (``SOURCE_BADGE_COLORS`` / ``STATUS_BADGE_COLORS``) 에서
    가져온다 — 대시보드 클래스 색상과 메일 인라인 색상의 단일 진입점.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime

from app.timezone import format_kst


# 공고 행의 KST 시각 표기 포맷. 대시보드의 ``kst_date`` Jinja 필터
# (``app/web/template_filters.py`` — task 00122 에서 초 단위로 통일됨) 와
# 동일한 ``YYYY-MM-DD HH:MM:SS`` 를 사용해 대시보드 expand 행과 메일 항목의
# 날짜 표기를 1:1 로 맞춘다.
ANNOUNCEMENT_ROW_DATETIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"


# ──────────────────────────────────────────────────────────────
# 배지 색상 — style.css 값을 옮긴 single source of truth
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BadgeColor:
    """배지 1종의 글자색·배경색·테두리색 묶음.

    Attributes:
        text_color:       글자색 (CSS ``color``).
        background_color: 배경색 (CSS ``background``).
        border_color:     테두리색 (CSS ``border-color``).
    """

    text_color: str
    background_color: str
    border_color: str


# 출처 배지 색상 — ``style.css`` 의 ``.source-iris`` / ``.source-ntis`` 값을
# 그대로 옮긴 매핑. 키는 ``source_type`` 의 lowercase. 새 출처가 추가되면
# 본 dict 한 곳만 갱신하면 대시보드·메일 양쪽에 반영된다.
SOURCE_BADGE_COLORS: dict[str, BadgeColor] = {
    "iris": BadgeColor(
        text_color="#1e3a8a", background_color="#dbeafe", border_color="#93c5fd"
    ),
    "ntis": BadgeColor(
        text_color="#5b21b6", background_color="#ede9fe", border_color="#c4b5fd"
    ),
}

# 상태 배지 색상 — ``style.css`` 의 ``.status-receiving`` / ``.status-scheduled``
# / ``.status-closed`` 값을 그대로 옮긴 매핑. 키는 ``AnnouncementStatus`` enum
# name 의 lowercase (``receiving`` / ``scheduled`` / ``closed``).
STATUS_BADGE_COLORS: dict[str, BadgeColor] = {
    "receiving": BadgeColor(
        text_color="#065f46", background_color="#d1fae5", border_color="#6ee7b7"
    ),
    "scheduled": BadgeColor(
        text_color="#92400e", background_color="#fef3c7", border_color="#fcd34d"
    ),
    "closed": BadgeColor(
        text_color="#4b5563", background_color="#e5e7eb", border_color="#d1d5db"
    ),
}

# 알 수 없는 출처/상태 키에 대한 안전 fallback 색상. 중립 회색 — 데이터
# 이상으로 모르는 키가 들어와도 렌더가 깨지지 않고 배지 1개로 떨어진다.
FALLBACK_BADGE_COLOR: BadgeColor = BadgeColor(
    text_color="#4b5563", background_color="#e5e7eb", border_color="#d1d5db"
)


# ──────────────────────────────────────────────────────────────
# view-model — 대시보드·메일이 함께 채우는 '공고 1행' 데이터
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnnouncementRowView:
    """대시보드 expand 행 / 데일리 리포트 메일 항목이 함께 소비하는 '공고 1행' view-model.

    대시보드의 ``SectionAExpandItem`` (``app.web.dashboard_section_a``) 와
    메일의 ``AnnouncementSummary`` (``app.email.daily_report``) 는 각자 다른
    형태이므로, 두 호출 맥락 모두 이 view-model 로 변환(어댑터)해
    :func:`render_announcement_row_html` 에 넘긴다.

    Attributes:
        source_type:        공고 출처 (예: ``"IRIS"`` / ``"NTIS"`` / ``"iris"``).
                            배지 색상 키는 내부에서 lowercase 로 정규화하며,
                            배지 글자는 입력 문자열을 그대로(대문자 변환 CSS
                            적용) 표시한다.
        status_label:       현재 상태 한글값 (``"접수중"`` / ``"접수예정"`` /
                            ``"마감"``).
        status_key:         현재 상태의 CSS 키 — ``AnnouncementStatus`` enum
                            name 의 lowercase (``receiving`` / ``scheduled`` /
                            ``closed``). ``STATUS_BADGE_COLORS`` 에 없는 값이면
                            ``FALLBACK_BADGE_COLOR`` 로 안전하게 떨어진다.
        transition_from:    전이 행에서만 의미 있는 이전 상태 한글값
                            (예: ``"접수예정"``). 비전이 행이면 ``None`` —
                            이 경우 이전상태 배지와 ``→`` 화살표를 모두 생략한다.
        transition_from_key: ``transition_from`` 에 대응하는 CSS 상태 키.
                            ``transition_from`` 이 있어도 매핑 누락 등으로
                            ``None`` 일 수 있고, 그때는 fallback 색상으로 렌더.
        title:              공고 제목. 렌더 시 ``html.escape`` 된다.
        detail_url:         공고 상세 페이지 절대 URL. ``wrap_with_link=True``
                            (기본) 일 때 행 전체를 감싸는 ``<a href>`` 의
                            대상이 된다.
        agency:             주관 기관명. 현재 행 마크업에는 출력하지 않지만
                            (대시보드 expand 행이 기관명을 행에 노출하지 않음),
                            두 소스(``SectionAExpandItem`` / ``AnnouncementSummary``)
                            가 모두 보유한 필드라 view-model 완결성을 위해
                            유지한다. ``None`` 가능.
        received_at:        접수 시작 시각 (UTC tz-aware ``datetime``). ``None``
                            이면 ``"접수일 미정"`` 으로 렌더.
        deadline_at:        마감 시각 (UTC tz-aware ``datetime``). ``None`` 이면
                            ``"마감 미정"`` 으로 렌더.
        duplicate_badges:   대시보드 '내용 변경' 행 전용 중복 표시 배지 텍스트
                            list. 메일 등 중복 표시가 없는 맥락에서는 빈 list
                            로 넘기며, 빈 list 면 배지 영역을 통째로 생략한다.
    """

    source_type: str
    status_label: str
    status_key: str
    transition_from: str | None
    transition_from_key: str | None
    title: str
    detail_url: str
    agency: str | None
    received_at: datetime | None
    deadline_at: datetime | None
    duplicate_badges: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# 렌더러 — 인라인 CSS HTML 조각
# ──────────────────────────────────────────────────────────────


def _render_source_badge(source_type: str) -> str:
    """출처 배지 ``<span>`` 1개를 인라인 CSS 로 렌더한다.

    ``style.css`` 의 ``.source-badge`` (font-size 11px / padding 1px 7px /
    radius 3px / bold / uppercase / 1px 테두리) 와 ``.source-iris`` 등 색상을
    그대로 인라인화한다. 알 수 없는 출처는 ``FALLBACK_BADGE_COLOR`` 로 떨어진다.

    Args:
        source_type: 공고 출처 문자열. 색상 키는 lowercase 로 정규화한다.

    Returns:
        출처 배지 ``<span>`` HTML 문자열. 배지 글자는 ``html.escape`` 된다.
    """
    color = SOURCE_BADGE_COLORS.get(source_type.strip().lower(), FALLBACK_BADGE_COLOR)
    return (
        '<span style="display:inline-block;font-size:11px;padding:1px 7px;'
        "border-radius:3px;font-weight:700;letter-spacing:0.04em;"
        "text-transform:uppercase;"
        f"border:1px solid {color.border_color};"
        f"color:{color.text_color};background:{color.background_color};\">"
        f"{html.escape(source_type)}</span>"
    )


def _render_status_badge(status_label: str, status_key: str | None) -> str:
    """상태 배지 ``<span>`` 1개를 인라인 CSS 로 렌더한다.

    ``style.css`` 의 ``.status-badge`` (font-size 12px / padding 2px 8px /
    radius 999px / semibold / 1px 테두리) 와 ``.status-receiving`` 등 색상을
    그대로 인라인화한다. 현재 상태 배지와 전이 이전 상태 배지 양쪽에 쓰인다.

    Args:
        status_label: 배지에 표시할 상태 한글값.
        status_key:   색상 매핑 키 (``receiving`` / ``scheduled`` / ``closed``).
            ``None`` 이거나 매핑에 없으면 ``FALLBACK_BADGE_COLOR`` 사용.

    Returns:
        상태 배지 ``<span>`` HTML 문자열. 배지 글자는 ``html.escape`` 된다.
    """
    normalized_key = (status_key or "").strip().lower()
    color = STATUS_BADGE_COLORS.get(normalized_key, FALLBACK_BADGE_COLOR)
    return (
        '<span style="display:inline-block;font-size:12px;padding:2px 8px;'
        "border-radius:999px;font-weight:600;"
        f"border:1px solid {color.border_color};"
        f"color:{color.text_color};background:{color.background_color};\">"
        f"{html.escape(status_label)}</span>"
    )


def _render_dates_group(
    received_at: datetime | None,
    deadline_at: datetime | None,
) -> str:
    """접수 일시·마감 일시를 두 ``<span>`` 으로 렌더한다.

    메일 클라이언트 호환을 위해 ``display:flex / gap / margin-left:auto`` 에
    의존하지 않는다 — 대신 접수 ``<span>`` 에 ``margin-right:10px`` 를 명시적으로
    부여해 두 일시 사이 간격을 보장한다. 우측 정렬은 호출자 ``<td>`` 의
    ``text-align:right`` 로 처리한다.

    시각이 ``None`` 이면 흐린 이탤릭 톤의 ``"접수일 미정"`` / ``"마감 미정"``
    으로 떨어진다.

    Args:
        received_at: 접수 시작 시각 (UTC tz-aware) 또는 ``None``.
        deadline_at: 마감 시각 (UTC tz-aware) 또는 ``None``.

    Returns:
        접수 ``<span>`` 과 마감 ``<span>`` 을 이어 붙인 HTML 문자열.
    """
    if received_at is not None:
        received_text = "접수 " + format_kst(
            received_at, ANNOUNCEMENT_ROW_DATETIME_FORMAT
        )
        received_html = (
            '<span style="color:#6b7280;font-size:12px;white-space:nowrap;'
            'margin-right:10px;">'
            f"{html.escape(received_text)}</span>"
        )
    else:
        received_html = (
            '<span style="color:#9ca3af;font-size:12px;font-style:italic;'
            'white-space:nowrap;margin-right:10px;">접수일 미정</span>'
        )

    if deadline_at is not None:
        deadline_text = "마감 " + format_kst(
            deadline_at, ANNOUNCEMENT_ROW_DATETIME_FORMAT
        )
        deadline_html = (
            '<span style="color:#6b7280;font-size:12px;white-space:nowrap;">'
            f"{html.escape(deadline_text)}</span>"
        )
    else:
        deadline_html = (
            '<span style="color:#9ca3af;font-size:12px;font-style:italic;'
            'white-space:nowrap;">마감 미정</span>'
        )

    return f"{received_html}{deadline_html}"


def render_announcement_row_html(
    row: AnnouncementRowView,
    *,
    wrap_with_link: bool = True,
) -> str:
    """공고 1행을 인라인 CSS HTML 조각으로 렌더한다.

    행 요소 순서는 대시보드 Section A expand 행과 동일하다:

        출처 배지 → (전이 행이면) 이전상태 배지 + ``→`` 화살표 → 현재상태 배지
        → 공고명 → (있으면) 중복 배지들 → 접수 일시·마감 일시 묶음(우측 정렬).

    행 컨테이너는 메일 클라이언트 호환성을 위해 ``<table>/<td>`` 기반 레이아웃을
    사용한다. Outlook(Word 렌더 엔진)·Gmail 등이 ``display:flex`` 와 ``gap`` 을
    제거해도 각 ``<td>`` 의 명시적 ``padding`` 이 항목 간 간격을 보장한다.

    링크 래핑:
        ``wrap_with_link=True`` (기본) 이면 ``<table>`` 전체를 ``row.detail_url``
        을 대상으로 하는 ``<a href style="display:block;">`` 으로 감싼다 —
        대시보드 expand 행처럼 '행 전체가 클릭 영역' 이 되고, 메일 항목도 클릭
        가능하다. ``wrap_with_link=False`` 이면 ``<table>`` 을 그대로 반환해
        호출자가 외부에서 링크 래핑을 제어할 수 있다.

    이스케이프:
        제목·출처·상태·중복 배지 등 모든 동적 값은 ``html.escape`` 되고,
        ``detail_url`` 은 ``href`` 속성에 들어가므로 ``quote=True`` 까지
        적용한다 — HTML injection 을 막는다.

    Args:
        row:            렌더할 공고 1행 view-model.
        wrap_with_link: ``True`` 면 ``<a href style="display:block;">`` 으로,
            ``False`` 면 ``<table>`` 을 그대로 반환한다. 기본 ``True``.

    Returns:
        공고 1행의 인라인 CSS HTML 조각 문자열.
    """
    # 1. 출처 배지 셀.
    source_cell = (
        '<td style="padding:6px 6px;white-space:nowrap;vertical-align:middle;">'
        + _render_source_badge(row.source_type)
        + "</td>"
    )

    # 2. 상태 배지 셀 — 전이 행이면 이전상태 + 화살표 + 현재상태, 비전이 행이면
    #    현재상태만.
    if row.transition_from is not None:
        status_inner = (
            _render_status_badge(row.transition_from, row.transition_from_key)
            + '<span style="color:#6b7280;font-size:12px;margin:0 4px;">→</span>'
            + _render_status_badge(row.status_label, row.status_key)
        )
    else:
        status_inner = _render_status_badge(row.status_label, row.status_key)

    # task 00140: min-width:160px 로 전이 행 '배지 → 배지' 조합까지 한 줄 보장
    status_cell = (
        '<td style="padding:6px 4px;white-space:nowrap;vertical-align:middle;min-width:160px;">'
        + status_inner
        + "</td>"
    )

    # 3. 공고명 셀 — width:100% 로 남은 가변 폭을 차지하며, 줄바꿈을 허용한다.
    #    min-width:80px 는 완전 붕괴(0px) 방지용 최소치이다.
    title_cell = (
        '<td style="padding:6px 8px;width:100%;vertical-align:middle;min-width:80px;">'
        + f'<span style="color:#111827;">{html.escape(row.title)}</span>'
        + "</td>"
    )

    # 4. 중복 배지 셀 — 빈 list 이면 셀 자체를 생략한다.
    if row.duplicate_badges:
        dup_badges_inner = "".join(
            '<span style="display:inline-block;background:#f3e8ff;color:#6b21a8;'
            "font-size:11px;padding:2px 6px;border-radius:3px;margin-right:4px;\">"
            + html.escape(badge_text)
            + "</span>"
            for badge_text in row.duplicate_badges
        )
        duplicate_cell = (
            '<td style="padding:6px 4px;white-space:nowrap;vertical-align:middle;">'
            + dup_badges_inner
            + "</td>"
        )
    else:
        duplicate_cell = ""

    # 5. 접수·마감 일시 셀 — 우측 정렬.
    #    _render_dates_group 이 반환하는 두 <span> 은 각각 white-space:nowrap 을
    #    가지고, td 의 min-width:400px 가 '접수 YYYY-MM-DD HH:MM:SS 마감 YYYY-MM-DD
    #    HH:MM:SS' 두 날짜가 한 줄에 들어가도록 최소 너비를 보장한다 (task 00141).
    dates_cell = (
        '<td style="padding:6px 6px;white-space:nowrap;vertical-align:middle;'
        'text-align:right;min-width:400px;">'
        + _render_dates_group(row.received_at, row.deadline_at)
        + "</td>"
    )

    table_html = (
        '<table style="width:100%;border-collapse:collapse;font-size:13px;"><tr>'
        + source_cell
        + status_cell
        + title_cell
        + duplicate_cell
        + dates_cell
        + "</tr></table>"
    )

    if wrap_with_link:
        safe_url = html.escape(row.detail_url, quote=True)
        return (
            f'<a href="{safe_url}" '
            'style="display:block;color:inherit;text-decoration:none;">'
            + table_html
            + "</a>"
        )
    return table_html


__all__ = [
    "ANNOUNCEMENT_ROW_DATETIME_FORMAT",
    "AnnouncementRowView",
    "BadgeColor",
    "FALLBACK_BADGE_COLOR",
    "SOURCE_BADGE_COLORS",
    "STATUS_BADGE_COLORS",
    "render_announcement_row_html",
]
