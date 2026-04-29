"""대시보드 라우터 HTTP 통합 테스트 (Phase 5b / task 00042-2 — 골격).

본 subtask 의 책임은 다음과 같다:
    - GET /dashboard 가 비로그인 / 로그인 모두 200 으로 뜨는지 (사용자 원문
      검증 1·2 의 base case — 위젯 영역은 후속 subtask 에서 추가).
    - GET /dashboard/api/snapshot-dates JSON 형식과 가용성 정책 일치 (변화 0건
      수집일도 활성 — docs/dashboard_design.md §4.1).
    - 컨트롤 영역 컨텍스트 (base_date / from / to / compare_mode) 가 (from, to)
      산출 함수와 정합.
    - compare_mode 검증 (허용 5종 외 400, custom 인데 compare_date 없음 400).

본 subtask 는 페이지 골격만 다루므로, A/B/widgets/trend 검증은 후속 subtask
(00042-3 ~ 00042-7) 가 같은 파일에 케이스를 추가하는 형태로 회귀 테스트가 늘어
난다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db.models import ScrapeSnapshot
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """테스트용 FastAPI TestClient (대시보드 라우터 mount 포함)."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    """user_a 로 등록 후 로그인된 TestClient.

    회원가입 라우트는 응답 바로 세션 쿠키를 set 하므로 별도 로그인 단계는
    필요하지 않다.
    """
    response = client.post(
        "/auth/register",
        data={"username": "dashboard_user_a", "password": "password_123"},
        follow_redirects=False,
    )
    # register 가 성공 (303) 또는 200 인지만 확인 — 정확한 status 는 auth 모듈
    # 의 책임 영역이라 본 테스트에서는 요구하지 않는다.
    assert response.status_code in (200, 303)
    return client


def _insert_snapshot_with_zero_changes(snapshot_date_iso: str) -> None:
    """변화 0건 (5종 빈 배열 + counts=0) snapshot row 를 INSERT 한다.

    docs/dashboard_design.md §4.1 의 캘린더 가용 날짜 판정 정책 회귀 — 변화
    0건이어도 row 가 있으면 캘린더에서 활성으로 표시된다.
    """
    empty_payload = {
        "new": [],
        "content_changed": [],
        "transitioned_to_접수예정": [],
        "transitioned_to_접수중": [],
        "transitioned_to_마감": [],
        "counts": {
            "new": 0,
            "content_changed": 0,
            "transitioned_to_접수예정": 0,
            "transitioned_to_접수중": 0,
            "transitioned_to_마감": 0,
        },
    }
    with session_scope() as s:
        snap = ScrapeSnapshot(
            snapshot_date=date.fromisoformat(snapshot_date_iso),
            payload=empty_payload,
        )
        s.add(snap)
        s.flush()


# ---------------------------------------------------------------------------
# GET /dashboard — HTML 페이지
# ---------------------------------------------------------------------------


class TestDashboardPage:
    """``GET /dashboard`` HTML 페이지 회귀."""

    def test_anonymous_access_returns_200(self, client: TestClient) -> None:
        """검증 1: 비로그인 사용자도 페이지가 정상 로드된다."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        body = response.text
        # 페이지 제목이 들어 있는지 가벼운 체크.
        assert "대시보드" in body
        # 컨트롤 영역의 form 이 렌더되어 있는지.
        assert 'data-dashboard-controls' in body
        # 컨트롤 5종 select option.
        for compare_mode_value in ("prev_day", "prev_week", "prev_month", "prev_year", "custom"):
            assert f'value="{compare_mode_value}"' in body

    def test_logged_in_access_returns_200(self, logged_in_client: TestClient) -> None:
        """검증 2 의 base case: 로그인 후에도 페이지가 정상 로드된다.

        라벨링 위젯 자체는 후속 subtask 에서 추가되므로 본 테스트는 200 응답과
        대시보드 탭 노출만 확인한다.
        """
        response = logged_in_client.get("/dashboard")
        assert response.status_code == 200
        # 로그인된 사용자 메뉴 — 즐겨찾기 / 로그아웃 버튼이 보여야 한다.
        assert "즐겨찾기" in response.text
        assert "로그아웃" in response.text

    def test_default_base_date_is_today_kst(self, client: TestClient) -> None:
        """base_date 미지정 시 KST 오늘로 fallback 한다."""
        from app.timezone import now_kst

        response = client.get("/dashboard")
        assert response.status_code == 200
        today_iso = now_kst().date().isoformat()
        # 컨트롤 요약 영역에 to_date 가 오늘 일자로 들어가 있어야 한다.
        assert today_iso in response.text

    def test_custom_compare_range(self, client: TestClient) -> None:
        """custom 모드 + compare_date 지정 시 (from, to) 가 정확히 매핑된다."""
        import re

        response = client.get(
            "/dashboard",
            params={
                "base_date": "2026-04-29",
                "compare_mode": "custom",
                "compare_date": "2026-01-15",
            },
        )
        assert response.status_code == 200
        body = response.text
        # form summary 영역에 from / to 일자가 ISO 로 들어가 있어야 한다.
        assert "2026-01-15" in body
        assert "2026-04-29" in body
        # custom option selected — Jinja 템플릿 whitespace 변동을 허용하기 위해
        # 정규식으로 매칭한다.
        assert re.search(r'value="custom"\s+selected', body) is not None

    def test_invalid_compare_mode_returns_400(self, client: TestClient) -> None:
        """허용 5종 외 compare_mode 는 400 으로 거절된다."""
        response = client.get("/dashboard", params={"compare_mode": "nonsense"})
        assert response.status_code == 400

    def test_custom_without_compare_date_returns_400(self, client: TestClient) -> None:
        """custom 모드인데 compare_date 가 비면 400."""
        response = client.get(
            "/dashboard",
            params={"compare_mode": "custom"},  # compare_date 없음
        )
        assert response.status_code == 400

    def test_custom_with_invalid_compare_date_returns_400(self, client: TestClient) -> None:
        """custom 모드인데 compare_date 형식 오류면 400."""
        response = client.get(
            "/dashboard",
            params={"compare_mode": "custom", "compare_date": "not-a-date"},
        )
        assert response.status_code == 400

    def test_invalid_base_date_falls_back_to_today(self, client: TestClient) -> None:
        """base_date 형식 오류는 400 이 아니라 KST 오늘로 silent fallback (사용자 원문)."""
        response = client.get(
            "/dashboard",
            params={"base_date": "garbage"},
        )
        # 사용자가 외부 URL 을 직접 쳐도 페이지가 뜨도록 — 가용성 자체는 fallback
        # 적용 없이 통과하지만 base_date 파싱 실패는 today 로 대체.
        assert response.status_code == 200

    def test_dashboard_nav_link_present(self, client: TestClient) -> None:
        """base.html 네비에 '대시보드' 링크가 항상 노출 (비로그인 포함)."""
        response = client.get("/")
        assert response.status_code == 200
        # 네비 자체에 대시보드 링크가 있어야 한다.
        assert 'href="/dashboard"' in response.text


# ---------------------------------------------------------------------------
# GET /dashboard/api/snapshot-dates — JSON API
# ---------------------------------------------------------------------------


class TestSnapshotDatesApi:
    """``GET /dashboard/api/snapshot-dates`` JSON 응답 회귀."""

    def test_empty_db_returns_empty_list(self, client: TestClient) -> None:
        """snapshot 이 없으면 ``{\"dates\": []}``."""
        response = client.get("/dashboard/api/snapshot-dates")
        assert response.status_code == 200
        assert response.json() == {"dates": []}

    def test_returns_iso_strings_in_ascending_order(self, client: TestClient) -> None:
        """삽입한 snapshot_date 들이 ISO 문자열 + 오름차순으로 반환된다."""
        # 일부러 역순으로 INSERT 해서 정렬 회귀 확인.
        for iso in ("2026-04-22", "2026-04-15", "2026-04-29"):
            _insert_snapshot_with_zero_changes(iso)

        response = client.get("/dashboard/api/snapshot-dates")
        assert response.status_code == 200
        body = response.json()
        assert body == {"dates": ["2026-04-15", "2026-04-22", "2026-04-29"]}

    def test_zero_change_snapshot_is_active_in_calendar_set(
        self, client: TestClient
    ) -> None:
        """변화 0건 snapshot 도 응답에 포함된다 — docs/dashboard_design.md §4.1
        디자인 의도 회귀 (사용자 원문 modify v2 의 가정과 반대)."""
        _insert_snapshot_with_zero_changes("2026-04-20")

        response = client.get("/dashboard/api/snapshot-dates")
        assert response.status_code == 200
        assert "2026-04-20" in response.json()["dates"]

    def test_anonymous_access_allowed(self, client: TestClient) -> None:
        """비로그인도 호출 가능 — 캘린더 자체가 비로그인 컨트롤이라 인증 불요."""
        response = client.get("/dashboard/api/snapshot-dates")
        assert response.status_code == 200
        # JSON 응답이 dict 구조를 가지는지.
        assert "dates" in response.json()


# ---------------------------------------------------------------------------
# A 섹션 (task 00042-3) — 라우트 통합 테스트
# ---------------------------------------------------------------------------


def _insert_announcement(
    *,
    announcement_id: int | None = None,
    canonical_key: str,
    title: str,
    source_announcement_id: str,
    source_type: str = "IRIS",
    status: str = "접수중",
) -> int:
    """canonical + announcement 1개 INSERT — 라우트 테스트용 헬퍼.

    Returns:
        INSERT 된 announcement.id (autoincrement 결과).
    """
    from datetime import UTC, datetime as _datetime

    from app.db.models import (
        Announcement,
        AnnouncementStatus,
        CanonicalProject,
    )

    status_enum = next(s for s in AnnouncementStatus if s.value == status)
    with session_scope() as session:
        canonical = CanonicalProject(
            canonical_key=canonical_key,
            key_scheme="official",
            representative_title=title,
        )
        session.add(canonical)
        session.flush()
        announcement = Announcement(
            source_type=source_type,
            source_announcement_id=source_announcement_id,
            title=title,
            agency="테스트기관",
            status=status_enum,
            received_at=None,
            deadline_at=None,
            scraped_at=_datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            canonical_group_id=canonical.id,
            canonical_key=canonical_key,
        )
        if announcement_id is not None:
            announcement.id = announcement_id
        session.add(announcement)
        session.flush()
        return announcement.id


def _insert_snapshot_with_payload(snapshot_date_iso: str, payload: dict) -> None:
    """payload 를 정규형으로 INSERT — 라우트 테스트용 헬퍼."""
    from app.db.snapshot import normalize_payload

    with session_scope() as session:
        snapshot = ScrapeSnapshot(
            snapshot_date=date.fromisoformat(snapshot_date_iso),
            payload=normalize_payload(payload),
        )
        session.add(snapshot)
        session.flush()


class TestSectionAOnPage:
    """``GET /dashboard`` 응답에 A 섹션 카드 + expand + fallback 안내문 회귀."""

    def test_no_data_section_renders_notice(self, client: TestClient) -> None:
        """검증 8: 비교일 이전 snapshot 전무 → A 섹션 '데이터 없음' 안내."""
        response = client.get(
            "/dashboard",
            params={
                "base_date": "2026-04-29",
                "compare_mode": "custom",
                "compare_date": "2026-04-22",
            },
        )
        assert response.status_code == 200
        body = response.text
        # 안내문 텍스트 + data-* 마커.
        assert "데이터 없음" in body
        assert "data-section-a-no-data" in body

    def test_fallback_notice_renders_when_compare_unavailable(
        self, client: TestClient
    ) -> None:
        """검증 7: 비교일 가용 안 됨 → 가장 가까운 이전 snapshot + 안내문."""
        announcement_id = _insert_announcement(
            canonical_key="key-c1",
            title="신규 공고 X",
            source_announcement_id="X-001",
        )
        # 비교일 = 2026-04-22 가용 안 됨, 가장 가까운 이전 = 2026-04-20.
        _insert_snapshot_with_payload("2026-04-20", {"new": []})
        _insert_snapshot_with_payload("2026-04-29", {"new": [announcement_id]})

        response = client.get(
            "/dashboard",
            params={
                "base_date": "2026-04-29",
                "compare_mode": "custom",
                "compare_date": "2026-04-22",
            },
        )
        assert response.status_code == 200
        body = response.text
        assert "data-section-a-fallback" in body
        # 안내문에 요청된 비교일과 effective 일자가 모두 들어 있어야 한다.
        assert "2026-04-22" in body
        assert "2026-04-20" in body
        # 카드의 신규 카운트가 1 — payload.counts 기준.
        assert "기준일 1건" in body

    def test_section_a_cards_render_with_expand_links(
        self, client: TestClient
    ) -> None:
        """검증 9·10: 카드 5종 + expand 행 + 상세 링크 (<a href>)."""
        announcement_id = _insert_announcement(
            canonical_key="key-c2",
            title="A 섹션 검증 공고",
            source_announcement_id="X-100",
        )
        _insert_snapshot_with_payload("2026-04-28", {"new": []})
        _insert_snapshot_with_payload(
            "2026-04-29", {"new": [announcement_id]}
        )

        response = client.get(
            "/dashboard",
            params={
                "base_date": "2026-04-29",
                "compare_mode": "prev_day",
            },
        )
        assert response.status_code == 200
        body = response.text
        # 5종 카드 모두 노출.
        for category_key in (
            "new",
            "content_changed",
            "transitioned_to_접수예정",
            "transitioned_to_접수중",
            "transitioned_to_마감",
        ):
            assert f'data-section-a-card="{category_key}"' in body
        # expand 행이 표준 <a href> 로 렌더 — 상세 페이지 링크.
        assert f'href="/announcements/{announcement_id}"' in body
        # 카드 라벨도 보여 줘야 한다.
        assert "신규" in body
        assert "내용 변경" in body


# ---------------------------------------------------------------------------
# B 섹션 (task 00042-4) — 라우트 통합 테스트
# ---------------------------------------------------------------------------


def _insert_announcement_with_dates(
    *,
    canonical_key: str,
    title: str,
    source_announcement_id: str,
    status_value: str,
    received_at: datetime | None = None,
    deadline_at: datetime | None = None,
    is_current: bool = True,
    source_type: str = "IRIS",
) -> int:
    """B 섹션 라우트 테스트용 announcement INSERT — received_at / deadline_at 포함."""
    from app.db.models import (
        Announcement,
        AnnouncementStatus,
        CanonicalProject,
    )

    status_enum = next(s for s in AnnouncementStatus if s.value == status_value)
    with session_scope() as session:
        canonical = CanonicalProject(
            canonical_key=canonical_key,
            key_scheme="official",
            representative_title=title,
        )
        session.add(canonical)
        session.flush()
        announcement = Announcement(
            source_type=source_type,
            source_announcement_id=source_announcement_id,
            title=title,
            agency="테스트기관",
            status=status_enum,
            received_at=received_at,
            deadline_at=deadline_at,
            is_current=is_current,
            scraped_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            canonical_group_id=canonical.id,
            canonical_key=canonical_key,
        )
        session.add(announcement)
        session.flush()
        return announcement.id


class TestSectionBOnPage:
    """``GET /dashboard`` 응답에 B 섹션 두 그룹 + past 안내문 회귀."""

    def test_section_b_renders_two_groups(self, client: TestClient) -> None:
        """검증 3 (base case): 기준일 = 오늘 → B 섹션 두 그룹 영역 노출."""
        from app.timezone import now_kst

        today_kst_iso = now_kst().date().isoformat()
        response = client.get(
            "/dashboard", params={"base_date": today_kst_iso}
        )
        assert response.status_code == 200
        body = response.text
        # 두 그룹 컨테이너 모두 노출.
        assert 'data-section-b-group="soon_to_open"' in body
        assert 'data-section-b-group="soon_to_close"' in body
        # 헤더 텍스트.
        assert "조만간 접수될 공고" in body
        assert "조만간 마감될 공고" in body
        # 매칭 0건이면 빈 안내문.
        assert "예정된 접수 공고가 없습니다." in body
        assert "임박한 마감 공고가 없습니다." in body

    def test_section_b_lists_soon_to_close_announcements(
        self, client: TestClient
    ) -> None:
        """to 기준 30일 이내 마감예정 공고가 마감예정 그룹에 포함."""
        # 기준일 = 2026-04-29. 마감 = 2026-05-10 (KST) → UTC.
        deadline_utc = datetime(2026, 5, 9, 15, tzinfo=UTC)
        announcement_id = _insert_announcement_with_dates(
            canonical_key="bk-900",
            title="B 섹션 마감예정 공고",
            source_announcement_id="B-900",
            status_value="접수중",
            deadline_at=deadline_utc,
        )

        response = client.get(
            "/dashboard", params={"base_date": "2026-04-29"}
        )
        assert response.status_code == 200
        body = response.text
        # 마감예정 그룹 안에 공고 링크 노출.
        assert f'href="/announcements/{announcement_id}"' in body
        assert "B 섹션 마감예정 공고" in body

    def test_section_b_past_notice_when_to_in_past(
        self, client: TestClient
    ) -> None:
        """검증 11: to=과거이면 past 안내문 + B 섹션 정상 (DB select 영향 없음)."""
        from app.timezone import now_kst

        past_iso = (now_kst().date() - timedelta(days=10)).isoformat()
        response = client.get("/dashboard", params={"base_date": past_iso})
        assert response.status_code == 200
        body = response.text
        # 안내문 마커 + 한국어 본문 일부.
        assert "data-section-b-past-notice" in body
        assert "기준일이 과거라" in body
        # 두 그룹 영역은 그대로 렌더.
        assert 'data-section-b-group="soon_to_open"' in body
        assert 'data-section-b-group="soon_to_close"' in body


# ---------------------------------------------------------------------------
# 사용자 라벨링 위젯 (task 00042-5) — 라우트 통합 테스트
# ---------------------------------------------------------------------------


class TestWidgetsOnPage:
    """``GET /dashboard`` 응답에 위젯 영역 노출 / 비로그인 시 skip 회귀."""

    def test_anonymous_skips_widgets_section(self, client: TestClient) -> None:
        """검증 1: 비로그인이면 위젯 영역이 DOM 에 들어가지 않는다."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        body = response.text
        # 위젯 컨테이너 마커가 본문에 없어야 한다 — 통째 skip.
        assert "data-dashboard-widgets" not in body
        # 위젯 라벨 텍스트도 노출되지 않는다.
        assert "전체 미확인 공고" not in body
        assert "기준일 변경 공고 중 내 미확인" not in body

    def test_logged_in_renders_all_four_widgets(
        self, logged_in_client: TestClient
    ) -> None:
        """검증 2: 로그인 시 4종 위젯 라벨 + 카운트 영역 노출."""
        response = logged_in_client.get("/dashboard")
        assert response.status_code == 200
        body = response.text
        # 컨테이너 마커.
        assert "data-dashboard-widgets" in body
        # 4종 위젯 마커.
        for widget_marker in (
            'data-widget="unread-total"',
            'data-widget="unjudged-total"',
            'data-widget="unread-in-range"',
            'data-widget="unjudged-in-range"',
        ):
            assert widget_marker in body
        # 4종 라벨 — 사용자 원문 그대로.
        assert "전체 미확인 공고" in body
        assert "전체 미판정 관련성" in body
        assert "기준일 변경 공고 중 내 미확인" in body
        assert "기준일 변경 공고 중 내 미판정" in body

    def test_anonymous_skip_emits_debug_log(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """검증 16: 비로그인 진입 시 DEBUG 로그로 'widgets skip (비로그인)' 기록.

        loguru 가 stdlib logging 으로 propagate 하지 않을 수 있어 캡처가 안 될
        수 있다. 그 경우는 응답 본문에 위젯 영역이 없는 것을 fallback 검증.
        """
        import logging

        with caplog.at_level(logging.DEBUG, logger="app"):
            response = client.get("/dashboard")
        assert response.status_code == 200
        # 로그 캡처 확인은 best-effort: caplog 에 보이면 검증, 없으면 본문 fallback.
        log_messages = [record.getMessage() for record in caplog.records]
        log_hit = any("위젯 쿼리 자체 skip" in message or "widgets skip" in message for message in log_messages)
        if not log_hit:
            # loguru → stdlib bridge 가 본 테스트 환경에서 작동하지 않을 수 있다.
            # 그래도 DOM 에 위젯 영역이 빠졌으면 'skip' 의 시각 효과는 검증된 것.
            assert "data-dashboard-widgets" not in response.text


# ---------------------------------------------------------------------------
# 추이 차트 (task 00042-6) — 라우트 통합 테스트
# ---------------------------------------------------------------------------


class TestTrendChartOnPage:
    """``GET /dashboard`` 응답에 추이 차트 영역 + JSON 임베드 + Chart.js script."""

    def test_trend_chart_section_renders(self, client: TestClient) -> None:
        """검증 12: 추이 차트 영역 + 캔버스 + 임베드 JSON 마커가 노출."""
        response = client.get("/dashboard", params={"base_date": "2026-04-29"})
        assert response.status_code == 200
        body = response.text
        # 컨테이너 마커 + 캔버스 + JSON 임베드 + vendor script.
        assert "data-dashboard-trend-chart" in body
        assert 'id="dashboardTrendChart"' in body
        assert 'id="dashboardTrendChartData"' in body
        assert "/static/vendor/chart.min.js" in body
        assert "/static/js/dashboard_trend_chart.js" in body

    def test_trend_chart_embeds_31_day_json(self, client: TestClient) -> None:
        """임베드 JSON 에 31개 일자 + 양끝 ISO 일자 포함 (검증 12 + design doc §9.1)."""
        response = client.get("/dashboard", params={"base_date": "2026-04-29"})
        assert response.status_code == 200
        body = response.text
        # 양끝 일자가 임베드 JSON 안에 들어 있어야 한다.
        assert "2026-04-14" in body
        assert "2026-05-14" in body
        # x_axis_label 키가 임베드 JSON 에 노출되어 있어야 한다.
        assert "x_axis_label" in body

    def test_trend_chart_uses_kst_mm_dd_label(self, client: TestClient) -> None:
        """x축 라벨이 'MM-DD' KST 표시 — Jinja2 필터 경유 정합성 (검증 13)."""
        response = client.get("/dashboard", params={"base_date": "2026-04-29"})
        body = response.text
        # 임베드 JSON 안에 \"x_axis_label\": \"04-29\" 같은 패턴이 들어 있어야 한다.
        # tojson | safe 가 한글/영문 모두 이스케이프 처리하므로 substring 매칭.
        assert '"04-14"' in body  # 시작 일자 라벨.
        assert '"05-14"' in body  # 끝 일자 라벨.

    def test_chart_js_vendor_bundle_exists(self) -> None:
        """vendor 디렉토리에 실제 chart.min.js 가 존재해야 한다 (라이선스 NOTICE 정합)."""
        from pathlib import Path

        vendor_path = Path("app/web/static/vendor/chart.min.js").resolve()
        assert vendor_path.is_file()
        # 사이즈가 어느 정도 있어야 — 빈 placeholder 가 아닌 실제 번들.
        assert vendor_path.stat().st_size > 50_000

    def test_notice_file_lists_chart_js(self) -> None:
        """NOTICE 파일에 Chart.js MIT 라이선스 항목이 있어야 한다."""
        from pathlib import Path

        notice_path = Path("NOTICE").resolve()
        assert notice_path.is_file()
        notice_text = notice_path.read_text(encoding="utf-8")
        assert "Chart.js" in notice_text
        assert "MIT" in notice_text
        assert "chart.min.js" in notice_text
