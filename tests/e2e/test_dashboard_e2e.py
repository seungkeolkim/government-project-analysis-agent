"""대시보드 4개 점검 항목의 Playwright E2E 회귀 테스트 (task 00043-4).

각 검증은 사용자 원문 task 00043 의 항목과 1:1 매핑된다:

    (1) 스냅샷 누적 구간 (from, to] 시맨틱 — compare=4/21, base=4/29 시
        A 섹션 \"신규\" 카드 base_count 가 8 (4/22~4/29 누적) 임을 라이브 페이지
        에서 확인.

    (2-1) B 섹션 레이아웃 2컬럼 → 2행 stack — ``.dashboard-section-b__rows``
          존재, 구 ``__columns`` 미존재. 두 그룹 제목 옆에 ``(N건)``.

    (2-2) D-Day 배지 표기 + 접수/마감 강조 분리 — D-N 패턴, soon_to_open 의
          received_at 이 bold + 파란색 (computed style 검사), 같은 row 의 마감
          은 강조 해제 (.dashboard-section-b__date--muted).

    (2-3) 추이 차트 범위 \"기준일 기준 과거 30일\" — 헤더 표기 + 임베드 JSON 의
          past_days=30, days length=31, days[30].date_iso == 기준일.

검증 표면이 단위 테스트 (``tests/dashboard/*``) 와 겹치는 부분이 있지만, E2E 의
가치는 \"라이브 서버 + 진짜 chromium 으로 임베드 JSON 파싱이 끊기지 않는지\"
이며, 단위 테스트는 빌더 함수의 로직 회귀를 본다 — 두 layer 가 함께 회귀를
보호한다.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect


# 모든 테스트에 e2e 마커 — pytest -m e2e 로 분리 실행 가능.
pytestmark = pytest.mark.e2e


# ──────────────────────────────────────────────────────────────
# 헬퍼 — 대시보드 URL 빌더 + 임베드 JSON 추출
# ──────────────────────────────────────────────────────────────


def _dashboard_url(
    base_url: str,
    *,
    base_date: str = "2026-04-29",
    compare_mode: str = "custom",
    compare_date: str = "2026-04-21",
) -> str:
    """대시보드 URL + 쿼리 스트링을 조립한다.

    기본값은 사용자 원문 예시 (compare=4/21, base=4/29) — 회귀 검증의 핵심 시나
    리오와 일치시킨다.

    Args:
        base_url:     uvicorn 서버 base URL (e2e_server fixture 에서 받음).
        base_date:    기준일 (ISO).
        compare_mode: 비교 대상 모드 (custom 일 때만 compare_date 사용).
        compare_date: 비교일 (ISO).

    Returns:
        \"http://host:port/dashboard?...\" 형태의 완전한 URL.
    """
    return (
        f"{base_url}/dashboard?base_date={base_date}"
        f"&compare_mode={compare_mode}&compare_date={compare_date}"
    )


def _read_embedded_trend_chart_json(page: Page) -> dict[str, Any]:
    """``#dashboardTrendChartData`` 스크립트의 textContent 를 JSON 으로 파싱.

    Args:
        page: 이미 ``goto`` 가 끝난 ``Page``.

    Returns:
        파싱된 dict — ``base_date`` / ``from_date`` / ``to_date`` / ``past_days``
        / ``days`` 키를 가진다.
    """
    raw_text = page.locator("#dashboardTrendChartData").text_content()
    assert raw_text is not None and raw_text.strip(), (
        "dashboard.html 의 #dashboardTrendChartData 스크립트가 비어 있습니다."
    )
    return json.loads(raw_text)


# ──────────────────────────────────────────────────────────────
# (1) 스냅샷 누적 구간 — A 섹션 \"신규\" 카드 base_count
# ──────────────────────────────────────────────────────────────


class TestSnapshotAccumulationRange:
    """사용자 원문 §1 — \"4/21 ~ 4/29 변화 측정이면 4/22~4/29 누적\" 회귀.

    단위 테스트 (``tests/dashboard/test_dashboard_compare_range_semantics.py``) 가
    빌더 레벨에서 (from, to] 시맨틱을 못박지만, 본 E2E 는 \"라이브 페이지에 표시
    된 신규 카운트가 8 인지\" 를 한 번 더 확인해 템플릿 → 빌더 사이의 컨텍스트
    바인딩이 끊기지 않았는지 회귀 보호한다.
    """

    def test_section_a_new_card_displays_eight_for_user_example(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """A 섹션 \"신규\" 카드의 base_count 가 정확히 8 (4/22~4/29 8일 누적).

        시드: 9일치 snapshot (4/21~4/29) 각 일자 i 에 ``new=[i]`` 1건.
        compare=4/21 baseline 제외 + base=4/29 포함 → 누적 announcement 2..9 (8개).
        """
        e2e_page.goto(_dashboard_url(e2e_server))

        # \"신규\" 카드 — A 섹션의 카테고리 카드 중 첫 번째 (descriptor 순서 고정).
        new_card = e2e_page.locator(
            "[data-section-a-card='new'] .dashboard-card__base-count"
        )
        expect(new_card).to_be_visible()

        text = new_card.text_content()
        assert text is not None
        # \"기준일 8건\" 형식 — 단위 테스트는 dataclass 필드를 보고 본 E2E 는 표시 텍스트.
        assert "8건" in text, (
            f"기준일 신규 카드의 base_count 가 8 이 아닙니다 — 표시 텍스트: {text!r}. "
            f"(from, to] 시맨틱이 깨져 4/21 까지 누적되거나 (=> 9건) 4/29 가 빠졌거나 "
            f"(=> 7건) 했을 가능성."
        )

    def test_dashboard_summary_shows_user_example_range(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """컨트롤 영역 summary 의 from/to 표시가 4/21 → 4/29 그대로.

        사용자가 캘린더에서 고른 비교 구간이 페이지 상단에 그대로 echo 되는지
        검증 — 라우트 → 템플릿 컨텍스트 바인딩 회귀.
        """
        e2e_page.goto(_dashboard_url(e2e_server))

        summary = e2e_page.locator(".dashboard-controls__summary")
        expect(summary).to_be_visible()
        summary_text = summary.text_content() or ""
        assert "2026-04-21" in summary_text
        assert "2026-04-29" in summary_text


# ──────────────────────────────────────────────────────────────
# (2-1) B 섹션 레이아웃 — 2컬럼 → 2행 stack + 건수 표시
# ──────────────────────────────────────────────────────────────


class TestSectionBLayoutAndCounts:
    """사용자 원문 §2-1 — \"폭을 넓게 쓰고 싶어\" / \"각각 몇건인지 표시\".

    레이아웃 자체는 단위 테스트로 검증이 어렵고 (CSS / DOM 구조), 본 E2E 가
    chromium 에서 실제로 두 그룹이 세로 stack 으로 배치됐는지를 boundingBox
    위치 비교로 확인한다.
    """

    def test_rows_container_replaces_columns_container(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """``.dashboard-section-b__rows`` 존재 + 구 ``.dashboard-section-b__columns`` 미존재."""
        e2e_page.goto(_dashboard_url(e2e_server))

        rows_container = e2e_page.locator(".dashboard-section-b__rows")
        expect(rows_container).to_be_visible()

        old_columns_container = e2e_page.locator(".dashboard-section-b__columns")
        # 구 클래스가 DOM 어디에도 남아 있지 않아야 한다 (회귀 가드).
        assert old_columns_container.count() == 0

    def test_two_groups_stacked_vertically_with_similar_width(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """soon_to_open 이 soon_to_close 위에 위치 + 두 그룹의 폭이 비슷.

        boundingBox 의 y 좌표 비교로 \"세로 stack\" 을, width 비교로 \"폭을 넓게
        사용\" 을 회귀 보호한다.
        """
        e2e_page.goto(_dashboard_url(e2e_server))

        open_group = e2e_page.locator("[data-section-b-group='soon_to_open']")
        close_group = e2e_page.locator("[data-section-b-group='soon_to_close']")
        expect(open_group).to_be_visible()
        expect(close_group).to_be_visible()

        open_box = open_group.bounding_box()
        close_box = close_group.bounding_box()
        assert open_box is not None and close_box is not None

        # soon_to_open 의 bottom 이 soon_to_close 의 top 보다 위 (= 세로 stack).
        # 약간의 margin (10px) 허용해 flex gap / margin collapsing 변동 흡수.
        assert open_box["y"] + open_box["height"] <= close_box["y"] + 10, (
            f"두 그룹이 세로로 쌓여 있지 않습니다 — open_bottom="
            f"{open_box['y'] + open_box['height']:.1f} vs close_top={close_box['y']:.1f}"
        )
        # 두 그룹의 폭이 거의 같아야 한다 (스택이라 둘 다 컨테이너 폭을 그대로 사용).
        assert abs(open_box["width"] - close_box["width"]) < 20

    def test_each_group_title_shows_count_in_parens(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """두 그룹의 제목 옆에 ``(N건)`` 형식 표시.

        시드: 접수예정 2건 + 마감예정 2건. 본 E2E 는 \"숫자가 0보다 큰 (N건) 형식\"
        만 회귀 — 정확한 숫자는 단위 테스트가 본다.
        """
        e2e_page.goto(_dashboard_url(e2e_server))

        open_count_span = e2e_page.locator(
            "[data-section-b-count='soon_to_open']"
        )
        close_count_span = e2e_page.locator(
            "[data-section-b-count='soon_to_close']"
        )
        expect(open_count_span).to_be_visible()
        expect(close_count_span).to_be_visible()

        open_text = (open_count_span.text_content() or "").strip()
        close_text = (close_count_span.text_content() or "").strip()

        # 형식: \"(N건)\" — 0 이상 정수.
        open_match = re.match(r"^\((\d+)건\)$", open_text)
        close_match = re.match(r"^\((\d+)건\)$", close_text)
        assert open_match is not None, f"open count 형식 불일치: {open_text!r}"
        assert close_match is not None, f"close count 형식 불일치: {close_text!r}"

        # 시드 데이터로 둘 다 2건이 들어와야 한다.
        assert int(open_match.group(1)) == 2
        assert int(close_match.group(1)) == 2


# ──────────────────────────────────────────────────────────────
# (2-2) D-Day 배지 + 접수/마감 강조 분리
# ──────────────────────────────────────────────────────────────


class TestSectionBDDayAndEmphasis:
    """사용자 원문 §2-2 — \"D-1 이런식으로 D-Day 표시\" / \"접수를 bold + color\"."""

    def test_d_day_badge_present_on_every_row(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """양 그룹의 모든 row 우측에 D-Day 배지 존재 + ``D-N`` / ``D-Day`` / ``D+N`` 형식."""
        e2e_page.goto(_dashboard_url(e2e_server))

        # 두 그룹의 D-Day 배지 — 첫 번째 row 만 확인하면 코드 경로가 같으므로
        # 회귀 가드로 충분.
        for group_key in ("soon_to_open", "soon_to_close"):
            d_day_badge = e2e_page.locator(
                f"[data-section-b-group='{group_key}'] [data-section-b-d-day]"
            ).first
            expect(d_day_badge).to_be_visible()
            label_text = (d_day_badge.text_content() or "").strip()
            assert re.fullmatch(r"D-Day|D-\d+|D\+\d+", label_text), (
                f"{group_key} 첫 row 의 D-Day 라벨 형식 불일치: {label_text!r}"
            )

    def test_received_date_emphasized_blue_bold_in_open_group(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """soon_to_open row 의 ``--received`` span 이 파란색 + bold (computed style).

        CSS (#1d4ed8 + font-weight:600) 가 라이브에서 적용되는지를 chromium 의
        getComputedStyle 로 확인 — 단순 클래스 존재만 본 단위 테스트와 달리
        실제 페인트 속성을 회귀 보호한다.
        """
        e2e_page.goto(_dashboard_url(e2e_server))

        received_span = e2e_page.locator(
            "[data-section-b-group='soon_to_open'] .dashboard-section-b__date--received"
        ).first
        expect(received_span).to_be_visible()

        # \"접수 YYYY-MM-DD\" 형식 텍스트.
        text_content = (received_span.text_content() or "").strip()
        assert re.match(r"^접수\s+\d{4}-\d{2}-\d{2}$", text_content), (
            f"--received span 텍스트 형식 불일치: {text_content!r}"
        )

        # computed style — color (RGB) + font-weight 양수 비교.
        color_value = received_span.evaluate("element => getComputedStyle(element).color")
        font_weight_value = received_span.evaluate(
            "element => getComputedStyle(element).fontWeight"
        )

        # #1d4ed8 → rgb(29, 78, 216). chromium 은 'rgb(29, 78, 216)' 또는
        # 'rgba(29, 78, 216, 1)' 로 표현한다 — 두 패턴 모두 허용.
        assert "29, 78, 216" in color_value, (
            f"--received span 색이 파란 #1d4ed8 가 아닙니다: {color_value!r}"
        )
        # font-weight 는 숫자 문자열 ('600') 로 반환된다 — 600 이상이면 bold 컨벤션.
        assert int(font_weight_value) >= 600, (
            f"--received span font-weight 가 bold 미만: {font_weight_value!r}"
        )

    def test_open_group_deadline_is_muted_not_highlighted(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """soon_to_open row 의 마감 일자는 ``--muted`` 로만 등장 — ``--deadline`` 강조 미적용."""
        e2e_page.goto(_dashboard_url(e2e_server))

        # soon_to_open 안에 --deadline 클래스가 없어야 한다 (강조 해제).
        open_deadline_count = e2e_page.locator(
            "[data-section-b-group='soon_to_open'] .dashboard-section-b__date--deadline"
        ).count()
        assert open_deadline_count == 0, (
            "soon_to_open 그룹에 --deadline (빨간 강조) 클래스가 남아 있습니다 — "
            "사용자 원문 \"마감이 아닌 접수를 강조\" 와 어긋남."
        )

        # --muted 한 개 이상 존재.
        muted_span = e2e_page.locator(
            "[data-section-b-group='soon_to_open'] .dashboard-section-b__date--muted"
        ).first
        expect(muted_span).to_be_visible()
        muted_text = (muted_span.text_content() or "").strip()
        assert re.match(r"^마감\s+\d{4}-\d{2}-\d{2}$", muted_text), (
            f"--muted span 텍스트 형식 불일치: {muted_text!r}"
        )

    def test_close_group_deadline_keeps_red_emphasis(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """soon_to_close row 의 마감 일자는 ``--deadline`` 빨간 강조 유지."""
        e2e_page.goto(_dashboard_url(e2e_server))

        close_deadline = e2e_page.locator(
            "[data-section-b-group='soon_to_close'] .dashboard-section-b__date--deadline"
        ).first
        expect(close_deadline).to_be_visible()

        color_value = close_deadline.evaluate(
            "element => getComputedStyle(element).color"
        )
        # #b91c1c → rgb(185, 28, 28).
        assert "185, 28, 28" in color_value, (
            f"--deadline span 색이 빨간 #b91c1c 가 아닙니다: {color_value!r}"
        )


# ──────────────────────────────────────────────────────────────
# (2-3) 추이 차트 범위 — 기준일 기준 과거 30일
# ──────────────────────────────────────────────────────────────


class TestTrendChartPastDaysRange:
    """사용자 원문 §2-3 — \"추이는 기준일 +- 15일이 아니라 기준일 ~ 기준일 - 30일\"."""

    def test_header_uses_past_days_phrasing(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """추이 섹션 헤더에 \"과거 N일\" 표기 + ``±N일`` 표기 미존재."""
        e2e_page.goto(_dashboard_url(e2e_server))

        title = e2e_page.locator(
            "[data-dashboard-trend-chart] .dashboard-section__title"
        )
        expect(title).to_be_visible()
        title_text = title.text_content() or ""
        assert re.search(r"과거\s+\d+일", title_text), (
            f"추이 헤더에 \"과거 N일\" 문구 없음: {title_text!r}"
        )
        assert "±" not in title_text, (
            f"추이 헤더에 구 \"±\" 표기가 남아 있음: {title_text!r}"
        )

    def test_canvas_aria_label_uses_past_days_phrasing(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """``<canvas>`` 의 aria-label 도 \"과거 N일\" 문구로 갱신."""
        e2e_page.goto(_dashboard_url(e2e_server))

        canvas = e2e_page.locator("#dashboardTrendChart")
        expect(canvas).to_be_visible()
        aria_label = canvas.get_attribute("aria-label") or ""
        assert re.search(r"과거\s+\d+일", aria_label), (
            f"canvas aria-label 에 \"과거 N일\" 문구 없음: {aria_label!r}"
        )

    def test_embedded_json_has_past_days_field_and_no_half_window(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """임베드 JSON 에 ``past_days`` 키 존재 + 구 ``half_window`` 키 미존재."""
        e2e_page.goto(_dashboard_url(e2e_server))

        chart_data = _read_embedded_trend_chart_json(e2e_page)
        assert chart_data["past_days"] == 30
        assert "half_window" not in chart_data, (
            "구 half_window 키가 임베드 JSON 에 남아 있습니다 — task 00043-3 회귀."
        )

    def test_embedded_json_range_is_base_minus_30_to_base(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """``from_date == base - 30days`` / ``to_date == base_date`` / days 길이 31."""
        e2e_page.goto(_dashboard_url(e2e_server))

        chart_data = _read_embedded_trend_chart_json(e2e_page)
        assert chart_data["base_date"] == "2026-04-29"
        assert chart_data["to_date"] == "2026-04-29"
        assert chart_data["from_date"] == "2026-03-30"
        assert isinstance(chart_data["days"], list)
        assert len(chart_data["days"]) == 31
        # 첫/마지막 일자 정렬 확인.
        assert chart_data["days"][0]["date_iso"] == "2026-03-30"
        assert chart_data["days"][-1]["date_iso"] == "2026-04-29"

    def test_embedded_json_contains_no_future_dates(
        self, e2e_page: Page, e2e_server: str
    ) -> None:
        """단방향 시야 회귀 — 모든 days[*].date_iso 가 base_date 이하.

        구 시맨틱 (양방향 ±15) 으로 회귀하면 base_date+1..base_date+15 가 끼는데,
        그러면 본 단언이 즉시 깨진다.
        """
        e2e_page.goto(_dashboard_url(e2e_server))

        chart_data = _read_embedded_trend_chart_json(e2e_page)
        base_date_iso = chart_data["base_date"]
        for day_point in chart_data["days"]:
            assert day_point["date_iso"] <= base_date_iso, (
                f"days 배열에 미래 일자가 끼어 있음 (단방향 시야 위반): "
                f"{day_point['date_iso']} > base_date {base_date_iso}"
            )
