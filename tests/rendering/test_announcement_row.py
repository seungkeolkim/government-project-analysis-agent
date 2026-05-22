"""``app.rendering.announcement_row`` 단위 테스트 (task 00136-1).

검증 범위 (subtask guidance):
    (a) 전이 행 / 비전이 행 — 이전상태 배지·화살표 노출/생략.
    (b) 접수·마감 일시 None / 존재 — '미정' 텍스트 vs KST 시각.
    (c) IRIS / NTIS 출처 — 배지 색상 단일 진입점 사용.
    (d) HTML escape — 제목에 ``<`` 가 포함될 때 그대로 새지 않는지.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.rendering.announcement_row import (
    FALLBACK_BADGE_COLOR,
    SOURCE_BADGE_COLORS,
    STATUS_BADGE_COLORS,
    AnnouncementRowView,
    render_announcement_row_html,
)


def _make_view(**overrides) -> AnnouncementRowView:
    """테스트용 ``AnnouncementRowView`` 를 기본값 + override 로 만든다.

    각 테스트는 관심 있는 필드만 ``overrides`` 로 바꾸고 나머지는 평범한
    비전이·날짜 존재 행 기본값을 그대로 쓴다.
    """
    defaults = {
        "source_type": "IRIS",
        "status_label": "접수중",
        "status_key": "receiving",
        "transition_from": None,
        "transition_from_key": None,
        "title": "2026년 정부 R&D 지원 공고",
        "detail_url": "https://example.com/announcements/42",
        "agency": "한국연구재단",
        "received_at": datetime(2026, 5, 20, 0, 0, 0, tzinfo=UTC),
        "deadline_at": datetime(2026, 6, 1, 5, 30, 0, tzinfo=UTC),
        "duplicate_badges": [],
    }
    defaults.update(overrides)
    return AnnouncementRowView(**defaults)


# ── (a) 전이 행 / 비전이 행 ─────────────────────────────────────


def test_non_transition_row_omits_previous_status_badge_and_arrow() -> None:
    """비전이 행(transition_from=None)은 이전상태 배지와 화살표를 생략한다."""
    html_fragment = render_announcement_row_html(_make_view())
    # 전이 화살표는 전이 행에서만 등장한다.
    assert "→" not in html_fragment
    # 현재 상태 배지(접수중)는 그대로 노출된다.
    assert "접수중" in html_fragment


def test_transition_row_includes_previous_and_current_status() -> None:
    """전이 행은 이전상태 배지 → 화살표 → 현재상태 배지 순서로 렌더한다."""
    html_fragment = render_announcement_row_html(
        _make_view(
            status_label="접수중",
            status_key="receiving",
            transition_from="접수예정",
            transition_from_key="scheduled",
        )
    )
    # 이전 상태(접수예정)·화살표·현재 상태(접수중)가 모두 포함된다.
    assert "접수예정" in html_fragment
    assert "→" in html_fragment
    assert "접수중" in html_fragment
    # 이전 상태가 화살표보다, 화살표가 현재 상태보다 앞에 온다.
    assert (
        html_fragment.index("접수예정")
        < html_fragment.index("→")
        < html_fragment.index("접수중")
    )
    # 이전 상태 배지는 transition_from_key(scheduled) 색상을 쓴다.
    assert STATUS_BADGE_COLORS["scheduled"].background_color in html_fragment


def test_transition_row_with_unknown_from_key_uses_fallback_color() -> None:
    """transition_from 은 있으나 from_key 가 None 이면 fallback 색상으로 렌더한다."""
    html_fragment = render_announcement_row_html(
        _make_view(transition_from="알수없음상태", transition_from_key=None)
    )
    assert "알수없음상태" in html_fragment
    assert FALLBACK_BADGE_COLOR.background_color in html_fragment


# ── (b) 접수·마감 일시 None / 존재 ──────────────────────────────


def test_dates_present_render_in_kst() -> None:
    """접수·마감 일시가 있으면 KST 변환된 ``YYYY-MM-DD HH:MM:SS`` 로 렌더한다."""
    html_fragment = render_announcement_row_html(_make_view())
    # 2026-05-20 00:00 UTC → KST 09:00:00, 2026-06-01 05:30 UTC → KST 14:30:00.
    assert "접수 2026-05-20 09:00:00" in html_fragment
    assert "마감 2026-06-01 14:30:00" in html_fragment
    # 접수 span 이 마감 span 보다 왼쪽(앞)에 온다.
    assert html_fragment.index("접수 2026") < html_fragment.index("마감 2026")


def test_dates_none_render_unknown_placeholder() -> None:
    """접수·마감 일시가 None 이면 '접수일 미정' / '마감 미정' 으로 렌더한다."""
    html_fragment = render_announcement_row_html(
        _make_view(received_at=None, deadline_at=None)
    )
    assert "접수일 미정" in html_fragment
    assert "마감 미정" in html_fragment


# ── (c) IRIS / NTIS 출처 ────────────────────────────────────────


def test_iris_source_uses_iris_badge_colors() -> None:
    """IRIS 출처 행은 SOURCE_BADGE_COLORS['iris'] 색상을 사용한다."""
    html_fragment = render_announcement_row_html(_make_view(source_type="IRIS"))
    assert "IRIS" in html_fragment
    assert SOURCE_BADGE_COLORS["iris"].background_color in html_fragment


def test_ntis_source_uses_ntis_badge_colors() -> None:
    """NTIS 출처 행은 SOURCE_BADGE_COLORS['ntis'] 색상을 사용한다."""
    html_fragment = render_announcement_row_html(_make_view(source_type="NTIS"))
    assert "NTIS" in html_fragment
    assert SOURCE_BADGE_COLORS["ntis"].background_color in html_fragment


def test_unknown_source_uses_fallback_color() -> None:
    """알 수 없는 출처는 FALLBACK_BADGE_COLOR 로 안전하게 떨어진다."""
    html_fragment = render_announcement_row_html(_make_view(source_type="UNKNOWN"))
    assert "UNKNOWN" in html_fragment
    assert FALLBACK_BADGE_COLOR.background_color in html_fragment


def test_source_type_lowercase_is_matched() -> None:
    """source_type 이 소문자로 들어와도 색상 키 매칭은 정상 동작한다."""
    html_fragment = render_announcement_row_html(_make_view(source_type="iris"))
    assert SOURCE_BADGE_COLORS["iris"].background_color in html_fragment


# ── (d) HTML escape ────────────────────────────────────────────


def test_title_with_angle_bracket_is_escaped() -> None:
    """제목에 ``<`` 가 들어가도 raw 태그로 새지 않고 escape 된다."""
    html_fragment = render_announcement_row_html(
        _make_view(title="<script>alert(1)</script> 위험 공고")
    )
    # raw 태그가 그대로 들어가면 안 된다.
    assert "<script>" not in html_fragment
    # escape 된 형태로는 존재한다.
    assert "&lt;script&gt;" in html_fragment


def test_detail_url_is_escaped_in_href() -> None:
    """detail_url 은 href 속성에서 quote 까지 이스케이프된다."""
    html_fragment = render_announcement_row_html(
        _make_view(detail_url='https://example.com/a?x="1"&y=2')
    )
    # 따옴표가 raw 로 들어가 href 속성을 깨뜨리면 안 된다.
    assert '?x="1"' not in html_fragment
    assert "&quot;" in html_fragment
    assert "&amp;y=2" in html_fragment


def test_duplicate_badge_text_is_escaped() -> None:
    """중복 배지 텍스트도 escape 된다."""
    html_fragment = render_announcement_row_html(
        _make_view(duplicate_badges=["<b>신규에도</b>"])
    )
    assert "<b>신규에도</b>" not in html_fragment
    assert "&lt;b&gt;신규에도&lt;/b&gt;" in html_fragment


# ── 링크 래핑 / 중복 배지 영역 ──────────────────────────────────


def test_wrap_with_link_true_wraps_row_in_anchor() -> None:
    """기본(wrap_with_link=True)은 행 전체를 detail_url 대상 <a> 로 감싼다."""
    html_fragment = render_announcement_row_html(_make_view())
    assert html_fragment.startswith("<a ")
    assert html_fragment.endswith("</a>")
    assert 'href="https://example.com/announcements/42"' in html_fragment


def test_wrap_with_link_false_wraps_row_in_table() -> None:
    """wrap_with_link=False 는 행을 <table> 컨테이너로 감싸 링크를 생략한다."""
    html_fragment = render_announcement_row_html(
        _make_view(), wrap_with_link=False
    )
    assert html_fragment.startswith("<table ")
    assert html_fragment.endswith("</table>")
    assert "<a " not in html_fragment


def test_empty_duplicate_badges_omits_badge_area() -> None:
    """duplicate_badges 가 빈 list 면 중복 배지 마크업이 전혀 없다."""
    html_fragment = render_announcement_row_html(
        _make_view(duplicate_badges=[])
    )
    # 중복 배지 전용 보라색 배경(#f3e8ff)이 등장하지 않는다.
    assert "#f3e8ff" not in html_fragment


def test_present_duplicate_badges_render_each_text() -> None:
    """duplicate_badges 가 있으면 각 배지 텍스트가 모두 렌더된다."""
    html_fragment = render_announcement_row_html(
        _make_view(duplicate_badges=["🆕 신규", "🔄 전이→마감"])
    )
    assert "🆕 신규" in html_fragment
    assert "🔄 전이→마감" in html_fragment
    # 중복 배지 보라색 배경이 등장한다.
    assert "#f3e8ff" in html_fragment


def test_row_element_order_matches_dashboard_expand_row() -> None:
    """출처 → 상태 → 공고명 → 접수/마감 일시 순서가 대시보드 expand 행과 같다."""
    html_fragment = render_announcement_row_html(
        _make_view(
            source_type="IRIS",
            title="순서 검증 공고",
        )
    )
    assert (
        html_fragment.index("IRIS")
        < html_fragment.index("접수중")
        < html_fragment.index("순서 검증 공고")
        < html_fragment.index("접수 2026")
    )


# ── (e) 컬럼 최소 너비 — task 00140 ─────────────────────────────


def test_status_cell_has_min_width_px() -> None:
    """상태 셀 <td> 에 px 절대단위 min-width:160px 가 인라인으로 지정된다 (task 00140).

    일부 메일 클라이언트가 white-space:nowrap 을 <td> 에서 제거할 때 status_cell 이
    최소 글자 폭까지 눌려 한 글자씩 줄바꿈되는 현상을 방지한다. min-width:160px 는
    전이 행의 '배지 + → + 배지' 조합(약 150px)이 한 줄에 들어갈 수 있는 기준값이다.
    """
    html_fragment = render_announcement_row_html(_make_view())
    assert "min-width:160px" in html_fragment


def test_status_cell_transition_row_has_min_width_px() -> None:
    """전이 행에서도 상태 셀 <td> 에 min-width:160px 가 유지된다 (task 00140)."""
    html_fragment = render_announcement_row_html(
        _make_view(
            status_label="접수중",
            status_key="receiving",
            transition_from="접수예정",
            transition_from_key="scheduled",
        )
    )
    assert "min-width:160px" in html_fragment


def test_dates_cell_has_min_width_px() -> None:
    """날짜 셀 <td> 에 px 절대단위 min-width:400px 가 인라인으로 지정된다 (task 00141).

    min-width:400px 는 '접수 YYYY-MM-DD HH:MM:SS 마감 YYYY-MM-DD HH:MM:SS' 형식으로
    접수·마감 두 날짜가 한 줄에 줄바꿈 없이 표시될 수 있는 기준값이다.
    """
    html_fragment = render_announcement_row_html(_make_view())
    assert "min-width:400px" in html_fragment
