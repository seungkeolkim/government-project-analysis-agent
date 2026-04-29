"""사용자 원문 17 검증 항목 + modify v2 추가 항목 회귀 매트릭스 (Phase 5b / task 00042-7).

본 모듈은 두 가지 책임을 진다:

(1) **검증 매트릭스 — 추적용 메타데이터**:
    사용자 원문이 명시한 17 항목 (그리고 modify v2 가 추가한 18·19 항목) 이 어느
    회귀 테스트로 보호되는지 한눈에 보이게 dict 로 박아 둔다. ``test_all_user_verification_items_have_coverage``
    가 17 항목 모두 커버되어 있음을 회귀 보호한다 (목록 자체가 사라지면 즉시
    실패). 향후 회귀 매트릭스가 변경되면 본 dict 를 갱신해 새 항목과 보호
    테스트를 같이 박는다.

(2) **검증 5 / 17 의 직접 회귀**:
    검증 5 (캘린더 snapshot 없는 날짜 → 클릭 비활성) 는 JS 동작이라 직접 단위
    테스트는 어렵지만, 서버가 클라이언트에 임베드한 가용 날짜 set 이 정확하게
    snapshot 존재 여부 한 가지로만 결정되는지 (도메인 모델) 회귀 보호한다.

    검증 17 (5a 회귀 — dashboard 가 read-only 라 snapshot/머지/GC 동작에 영향
    없음) 은 dashboard 빌더 함수들이 INSERT/UPDATE/DELETE 를 한 번도 발급하지
    않음을 SQL 카운트로 회귀 보호한다.

5a 회귀 자체는 ``tests/db/`` 의 기존 테스트 suite 가 그대로 통과하면 자연스럽게
입증되며, 본 모듈은 \"본 task 가 추가한 dashboard 코드\" 가 read-only 인지를
독립적으로 회귀한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    ScrapeSnapshot,
)
from app.db.snapshot import normalize_payload


# ──────────────────────────────────────────────────────────────
# 17 검증 항목 + modify v2 추가 매트릭스 — 어느 테스트가 보호하는지 매핑
# ──────────────────────────────────────────────────────────────


# 사용자 원문 17 검증 항목 + modify v2 (#18·19) 의 dict 표현. 각 키는 검증 번호,
# 값은 \"한국어 시나리오 설명\" 과 \"보호 테스트 list\" 를 가진다.
USER_VERIFICATION_COVERAGE: dict[int, dict[str, object]] = {
    1: {
        "scenario": "비로그인 / dashboard 접근 → 페이지 로드, 라벨링 위젯 영역 미표시",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestDashboardPage::test_anonymous_access_returns_200",
            "tests/dashboard/test_dashboard_routes.py::TestWidgetsOnPage::test_anonymous_skips_widgets_section",
        ],
    },
    2: {
        "scenario": "로그인 후 / dashboard → 라벨링 위젯 4종 표시",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestDashboardPage::test_logged_in_access_returns_200",
            "tests/dashboard/test_dashboard_routes.py::TestWidgetsOnPage::test_logged_in_renders_all_four_widgets",
        ],
    },
    3: {
        "scenario": "기준일 = 오늘, 비교 = 전날 → A/B 섹션 정상",
        "covered_by": [
            "tests/dashboard/test_verification_matrix.py::TestNamedVerificationScenarios::test_today_with_prev_day_compare_renders_all_sections",
        ],
    },
    4: {
        "scenario": "기준일 = 어제 (snapshot 있음), 비교 = 전주 → 정상",
        "covered_by": [
            "tests/dashboard/test_verification_matrix.py::TestNamedVerificationScenarios::test_yesterday_base_with_prev_week_compare_renders",
        ],
    },
    5: {
        "scenario": "기준일 캘린더의 snapshot 없는 날짜 → 클릭 비활성",
        "covered_by": [
            "tests/dashboard/test_verification_matrix.py::TestCalendarUnavailableDateInactive::test_snapshot_dates_set_governs_calendar_active_state",
            "tests/dashboard/test_dashboard_routes.py::TestSnapshotDatesApi::test_zero_change_snapshot_is_active_in_calendar_set",
        ],
    },
    6: {
        "scenario": "비교 = 직접 선택 + 가용 날짜 → 정상",
        "covered_by": [
            "tests/dashboard/test_verification_matrix.py::TestNamedVerificationScenarios::test_custom_compare_with_available_date_no_fallback",
        ],
    },
    7: {
        "scenario": "비교 = 직접 선택 + 가용 안 됨 → 가장 가까운 이전 snapshot 사용 + 안내문",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestSectionAOnPage::test_fallback_notice_renders_when_compare_unavailable",
            "tests/dashboard/test_dashboard_section_a.py::TestBuildSectionA::test_fallback_uses_nearest_previous_snapshot",
        ],
    },
    8: {
        "scenario": "비교일 이전 snapshot 전무 → A 섹션 \"데이터 없음\", B 섹션 정상",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestSectionAOnPage::test_no_data_section_renders_notice",
            "tests/dashboard/test_dashboard_section_a.py::TestBuildSectionA::test_no_data_when_compare_snapshot_missing_and_no_previous",
        ],
    },
    9: {
        "scenario": "A 섹션 카드 클릭 → expand, 전체 리스트",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestSectionAOnPage::test_section_a_cards_render_with_expand_links",
        ],
    },
    10: {
        "scenario": "row 클릭 → 상세 페이지 / 가운데 클릭 → 새 창 (<a href> 표준 동작)",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestSectionAOnPage::test_section_a_cards_render_with_expand_links",
            "tests/dashboard/test_dashboard_routes.py::TestSectionBOnPage::test_section_b_lists_soon_to_close_announcements",
        ],
    },
    11: {
        "scenario": "B 섹션 to=과거 → 안내문 + 현재 시점 활성 공고 표시",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestSectionBOnPage::test_section_b_past_notice_when_to_in_past",
            "tests/dashboard/test_dashboard_section_b.py::TestBuildSectionB::test_to_in_past_sets_notice",
        ],
    },
    12: {
        # task 00043-3 갱신: 기존 \"기준일 ±15일 (양방향 31일)\" 에서 \"기준일 기준
        # 과거 30일 (단방향 31일)\" 로 시맨틱 이동.
        "scenario": "추이 차트가 기준일 기준 과거 30일 일별 카운트 line chart",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestTrendChartOnPage::test_trend_chart_section_renders",
            "tests/dashboard/test_dashboard_routes.py::TestTrendChartOnPage::test_trend_chart_embeds_31_day_json",
            "tests/dashboard/test_dashboard_trend_chart.py::TestBuildTrendChart::test_default_window_produces_31_days",
        ],
    },
    13: {
        "scenario": "모든 timestamp KST (Jinja2 필터 경유 확인)",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestTrendChartOnPage::test_trend_chart_uses_kst_mm_dd_label",
            "tests/dashboard/test_verification_matrix.py::TestKstFilterUsage::test_dashboard_uses_kst_filters_for_timestamps",
        ],
    },
    14: {
        "scenario": "(from, to] 구간 N일치 누적 머지 결과가 단일 snapshot 비교와 일관 — merge_snapshot_payload 재사용 회귀",
        "covered_by": [
            "tests/dashboard/test_dashboard_section_a.py::TestBuildSectionA::test_single_day_range_matches_single_snapshot_compare",
            "tests/dashboard/test_dashboard_section_a.py::TestBuildSectionA::test_n_day_reduce_consistent_with_pairwise_merge",
            "tests/dashboard/test_dashboard_section_a.py::TestBuildSectionA::test_reduce_with_empty_initial_payload_is_idempotent",
        ],
    },
    15: {
        "scenario": "위젯 쿼리 N+1 회피 (announcement_ids 한 번의 IN 쿼리)",
        "covered_by": [
            "tests/dashboard/test_dashboard_section_a.py::TestBuildSectionA::test_announcements_join_is_single_query",
            "tests/dashboard/test_dashboard_widgets.py::TestBuildUserLabelWidgets::test_in_range_helpers_run_single_query_each",
            "tests/dashboard/test_dashboard_trend_chart.py::TestBuildTrendChart::test_single_select_query_for_snapshots",
        ],
    },
    16: {
        "scenario": "비로그인 시 위젯 쿼리 자체 skip (DEBUG 로그로 확인)",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestWidgetsOnPage::test_anonymous_skip_emits_debug_log",
            "tests/dashboard/test_dashboard_widgets.py::TestCountUnreadInAnnouncementIds::test_empty_id_list_returns_zero_without_query",
            "tests/dashboard/test_dashboard_widgets.py::TestCountUnjudgedInCanonicalIds::test_empty_id_list_returns_zero_without_query",
        ],
    },
    17: {
        "scenario": "5a 회귀: snapshot 생성/머지/GC 동작 영향 없음 (대시보드는 read-only)",
        "covered_by": [
            "tests/dashboard/test_verification_matrix.py::TestDashboardIsReadOnly::test_dashboard_route_emits_no_writes",
            "tests/db/test_scrape_snapshot.py (5a 회귀 suite — 대시보드 변경 후에도 통과)",
        ],
    },
    18: {
        "scenario": "(modify v2) 변화 0건 ScrapeRun 도 캘린더에서 활성 (snapshot row 존재 기준)",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestSnapshotDatesApi::test_zero_change_snapshot_is_active_in_calendar_set",
        ],
    },
    19: {
        "scenario": "(modify v2) 그날 ScrapeRun 이 모두 failed/cancelled → 캘린더 비활성",
        "covered_by": [
            "tests/dashboard/test_dashboard_routes.py::TestSnapshotDatesApi::test_empty_db_returns_empty_list",
        ],
    },
}


def test_all_user_verification_items_have_coverage() -> None:
    """17 항목 + modify v2 의 18·19 모두 회귀 매트릭스에 등록되어 있다."""
    expected_keys = set(range(1, 20))  # 1..19 inclusive.
    assert set(USER_VERIFICATION_COVERAGE.keys()) == expected_keys

    for verification_id, entry in USER_VERIFICATION_COVERAGE.items():
        assert "scenario" in entry, f"검증 #{verification_id}: scenario 누락"
        assert "covered_by" in entry, f"검증 #{verification_id}: covered_by 누락"
        covered_by = entry["covered_by"]
        assert isinstance(covered_by, list)
        assert len(covered_by) >= 1, (
            f"검증 #{verification_id}: 보호 테스트가 0개 — 최소 1개 이상이어야 함"
        )


# ──────────────────────────────────────────────────────────────
# fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def session(test_engine: Engine) -> Iterator[Session]:
    """test_engine 위 ORM 세션."""
    from app.db.session import SessionLocal

    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()


def _make_canonical_with_announcement(
    session: Session,
    *,
    canonical_key: str,
    title: str,
    source_announcement_id: str,
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
    received_at: datetime | None = None,
    deadline_at: datetime | None = None,
) -> tuple[CanonicalProject, Announcement]:
    """canonical 1개 + announcement 1개 INSERT (verification 시나리오용)."""
    canonical = CanonicalProject(
        canonical_key=canonical_key,
        key_scheme="official",
        representative_title=title,
    )
    session.add(canonical)
    session.flush()
    announcement = Announcement(
        source_type="IRIS",
        source_announcement_id=source_announcement_id,
        title=title,
        agency="시나리오기관",
        status=status,
        received_at=received_at,
        deadline_at=deadline_at,
        scraped_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        canonical_group_id=canonical.id,
        canonical_key=canonical_key,
    )
    session.add(announcement)
    session.flush()
    return canonical, announcement


def _insert_snapshot(
    session: Session, *, snapshot_date_iso: str, payload: dict
) -> ScrapeSnapshot:
    """payload 정규형으로 INSERT — 시나리오용."""
    snap = ScrapeSnapshot(
        snapshot_date=date.fromisoformat(snapshot_date_iso),
        payload=normalize_payload(payload),
    )
    session.add(snap)
    session.flush()
    return snap


# ──────────────────────────────────────────────────────────────
# 검증 3·4·6 — 시나리오 직접 회귀 (라우트 + 컨텍스트 정합)
# ──────────────────────────────────────────────────────────────


from fastapi.testclient import TestClient  # noqa: E402 (검증용 fixture 와 함께)


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """대시보드 라우터가 mount 된 TestClient."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


class TestNamedVerificationScenarios:
    """사용자 원문 검증 3·4·6 — 라우트 + 컨텍스트 정합."""

    def test_today_with_prev_day_compare_renders_all_sections(
        self, client: TestClient
    ) -> None:
        """검증 3: 기준일 = 오늘, 비교 = 전날 → A/B 섹션 정상."""
        from app.timezone import now_kst

        today_iso = now_kst().date().isoformat()
        response = client.get(
            "/dashboard",
            params={"base_date": today_iso, "compare_mode": "prev_day"},
        )
        assert response.status_code == 200
        body = response.text
        # A 섹션 5종 카드 모두 + B 섹션 두 그룹 모두 + 추이 차트 영역 노출.
        for category_key in (
            "new",
            "content_changed",
            "transitioned_to_접수예정",
            "transitioned_to_접수중",
            "transitioned_to_마감",
        ):
            assert f'data-section-a-card="{category_key}"' in body
        assert 'data-section-b-group="soon_to_open"' in body
        assert 'data-section-b-group="soon_to_close"' in body
        assert "data-dashboard-trend-chart" in body
        # 컨트롤 form 의 prev_day 가 selected 로 들어가 있다.
        import re

        assert re.search(r'value="prev_day"\s+selected', body) is not None

    def test_yesterday_base_with_prev_week_compare_renders(
        self, client: TestClient
    ) -> None:
        """검증 4: 기준일 = 어제 (snapshot 있음), 비교 = 전주 → 정상."""
        from app.timezone import now_kst

        yesterday = now_kst().date() - timedelta(days=1)
        # 어제 snapshot + 전주(7일전) snapshot 을 fixture 로 INSERT.
        # client fixture 의 test_engine 과 같은 DB 를 공유하려면 session_scope 사용.
        from app.db.session import session_scope

        with session_scope() as sess:
            for offset_days in (0, 7):  # 어제 + 8일 전 (= 전주 baseline 역할).
                d = yesterday - timedelta(days=offset_days)
                snap = ScrapeSnapshot(
                    snapshot_date=d, payload=normalize_payload({"new": []})
                )
                sess.add(snap)

        response = client.get(
            "/dashboard",
            params={
                "base_date": yesterday.isoformat(),
                "compare_mode": "prev_week",
            },
        )
        assert response.status_code == 200
        body = response.text
        # 컨트롤 form summary 에 어제 일자가 to_date 로 들어가 있다.
        assert yesterday.isoformat() in body
        # A/B 섹션 영역 노출 확인.
        assert 'data-section-a-card="new"' in body
        assert 'data-section-b-group="soon_to_open"' in body

    def test_custom_compare_with_available_date_no_fallback(
        self, client: TestClient
    ) -> None:
        """검증 6: 비교 = 직접 선택 + 가용 날짜 → fallback 미발동, A 섹션 정상."""
        from app.db.session import session_scope

        # 비교일 (2026-04-22) 과 기준일 (2026-04-29) 둘 다 snapshot 가용.
        with session_scope() as sess:
            for iso in ("2026-04-22", "2026-04-29"):
                snap = ScrapeSnapshot(
                    snapshot_date=date.fromisoformat(iso),
                    payload=normalize_payload({"new": []}),
                )
                sess.add(snap)

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
        # fallback / 데이터 없음 안내문 모두 미노출 (가용 날짜라 fallback 미발동).
        assert "data-section-a-fallback" not in body
        assert "data-section-a-no-data" not in body


# ──────────────────────────────────────────────────────────────
# 검증 5 — 캘린더 가용 날짜 set 의 단일 결정 기준 (snapshot 존재)
# ──────────────────────────────────────────────────────────────


class TestCalendarUnavailableDateInactive:
    """검증 5: 캘린더의 snapshot 없는 날짜 → 클릭 비활성.

    JS 동작 자체는 단위 테스트가 어렵지만, 서버가 클라이언트에 임베드한 가용
    날짜 set 이 'snapshot row 존재' 한 가지로만 결정되는 게 도메인 회귀 가드의
    핵심이다. 가용 set 에 들어 있지 않은 날짜는 dashboard_calendar.js 의
    available_set.has(iso) 가 false 가 되어 disabled 클래스가 적용된다 — 본
    테스트는 set 의 결정 기준만 회귀 보호.
    """

    def test_snapshot_dates_set_governs_calendar_active_state(
        self, client: TestClient
    ) -> None:
        """캘린더 활성 set = snapshot 존재 일자 ⊆ DB. 외부 입력에 흔들리지 않는다."""
        from app.db.session import session_scope

        # 두 일자만 snapshot 존재.
        with session_scope() as sess:
            for iso in ("2026-04-15", "2026-04-29"):
                snap = ScrapeSnapshot(
                    snapshot_date=date.fromisoformat(iso),
                    payload=normalize_payload({"new": []}),
                )
                sess.add(snap)

        api_response = client.get("/dashboard/api/snapshot-dates")
        assert api_response.status_code == 200
        api_dates = set(api_response.json()["dates"])
        # API 응답 = 캘린더가 활성으로 그릴 일자 set.
        assert api_dates == {"2026-04-15", "2026-04-29"}

        # 페이지의 임베드 JSON 도 동일 set 이어야 한다 (서버 사전 계산 결과 일관).
        page_response = client.get("/dashboard")
        assert page_response.status_code == 200
        page_body = page_response.text
        # #dashboardSnapshotDates JSON 안에 두 일자만 들어 있다.
        assert '"2026-04-15"' in page_body
        assert '"2026-04-29"' in page_body
        # 가용 안 한 일자 (예: 04-14) 는 set 에 없어야 한다 — 캘린더가 비활성으로 그린다.
        # 가용 set JSON 안에서만 확인 (페이지 다른 곳에서 우연히 나타날 수도 있어
        # script 안의 list 내부 검증을 단순 substring 으로 한다).
        # 임베드 형식 ["2026-04-15", "2026-04-29"] 안에 04-14 ISO 가 들어 있지 않음.
        # 대시보드 from_date / to_date 컨트롤 영역에 04-14 가 등장할 수 있어, 임베드
        # 영역만 분리해서 확인한다.
        embed_start = page_body.find('id="dashboardSnapshotDates"')
        embed_end = page_body.find("</script>", embed_start)
        embed_block = page_body[embed_start:embed_end]
        assert '"2026-04-14"' not in embed_block


# ──────────────────────────────────────────────────────────────
# 검증 13 — KST Jinja2 필터 호출 회귀
# ──────────────────────────────────────────────────────────────


class TestKstFilterUsage:
    """검증 13: 모든 timestamp 표시는 Jinja2 kst_format / kst_date 필터 경유."""

    def test_dashboard_uses_kst_filters_for_timestamps(
        self, client: TestClient
    ) -> None:
        """A 섹션 expand 의 마감일 / B 섹션 행의 마감일 모두 KST 일자 'YYYY-MM-DD' 형식.

        kst_date 필터의 출력은 'YYYY-MM-DD' (KST). UTC 저장된 deadline_at 이
        템플릿에서 KST date 로 변환되어 노출되는지 회귀 보호.
        """
        from app.db.session import session_scope

        # 2026-04-30 09:00 KST = 2026-04-30 00:00 UTC. 마감일 KST 표시는 '2026-04-30'.
        deadline_kst_2026_04_30_morning_utc = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
        with session_scope() as sess:
            canonical = CanonicalProject(
                canonical_key="key-kst-filter",
                key_scheme="official",
                representative_title="KST 필터 검증 공고",
            )
            sess.add(canonical)
            sess.flush()
            announcement = Announcement(
                source_type="IRIS",
                source_announcement_id="KST-1",
                title="KST 필터 검증 공고",
                agency="시각필터검증",
                status=AnnouncementStatus.RECEIVING,
                received_at=None,
                deadline_at=deadline_kst_2026_04_30_morning_utc,
                scraped_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
                canonical_group_id=canonical.id,
                canonical_key="key-kst-filter",
            )
            sess.add(announcement)
            sess.flush()
            # B 섹션 마감예정 그룹에 노출되도록 to=2026-04-29 로 호출.
            snap = ScrapeSnapshot(
                snapshot_date=date(2026, 4, 28),
                payload=normalize_payload({"new": []}),
            )
            sess.add(snap)
            snap = ScrapeSnapshot(
                snapshot_date=date(2026, 4, 29),
                payload=normalize_payload({"new": [announcement.id]}),
            )
            sess.add(snap)

        response = client.get(
            "/dashboard",
            params={"base_date": "2026-04-29", "compare_mode": "prev_day"},
        )
        assert response.status_code == 200
        body = response.text
        # KST 필터로 변환한 마감일 표시 (KST 일자 = 2026-04-30).
        assert "2026-04-30" in body
        # UTC 시각 그대로 (예: '09:00:00' 등) 가 본문에 노출되지 않아야 한다 —
        # 표시는 모두 kst_date / kst_format 필터를 거치므로 ISO datetime 이 그대로
        # 노출되는 일은 없다.
        # 본 테스트는 보수적으로 'T00:00:00' 같은 UTC datetime substring 을 부재
        # 검증한다.
        assert "T00:00:00" not in body
        assert "+00:00" not in body


# ──────────────────────────────────────────────────────────────
# 검증 17 — 5a 회귀: 대시보드는 read-only
# ──────────────────────────────────────────────────────────────


class TestDashboardIsReadOnly:
    """검증 17: 대시보드가 5a 의 snapshot pipeline 에 영향 없음 (read-only 회귀).

    대시보드 라우트 + 빌더 함수들이 INSERT/UPDATE/DELETE 를 한 번도 발급하지
    않음을 SQLAlchemy after_cursor_execute 이벤트로 회귀 보호한다.
    """

    def test_dashboard_route_emits_no_writes(self, client: TestClient) -> None:
        """``GET /dashboard`` 처리 동안 INSERT/UPDATE/DELETE 가 0회."""
        from app.db.session import session_scope

        # 사전 데이터 INSERT 는 client 호출 전에 끝낸다 — 본 테스트의 카운트는
        # 라우트 내부 동작만 본다.
        with session_scope() as sess:
            for iso in ("2026-04-28", "2026-04-29"):
                snap = ScrapeSnapshot(
                    snapshot_date=date.fromisoformat(iso),
                    payload=normalize_payload({"new": []}),
                )
                sess.add(snap)

        write_statement_count = {"value": 0}

        def _count_writes(conn, cursor, statement, parameters, context, executemany):
            statement_lower = statement.lower().lstrip()
            if statement_lower.startswith("insert ") or statement_lower.startswith(
                "update "
            ) or statement_lower.startswith("delete "):
                write_statement_count["value"] += 1

        # ScopeFactory 가 만드는 모든 connection 에 listener 부착.
        from app.db.session import get_engine

        engine = get_engine()
        event.listen(engine, "after_cursor_execute", _count_writes)
        try:
            response = client.get("/dashboard", params={"base_date": "2026-04-29"})
        finally:
            event.remove(engine, "after_cursor_execute", _count_writes)

        assert response.status_code == 200
        # 비로그인이면 '자동 읽음' 같은 UPSERT 도 없어야 한다 — 0 이 정답.
        assert write_statement_count["value"] == 0, (
            f"대시보드 라우트가 INSERT/UPDATE/DELETE 를 "
            f"{write_statement_count['value']} 회 발급 — read-only 정책 위반"
        )
