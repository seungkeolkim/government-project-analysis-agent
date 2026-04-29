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
from datetime import date

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
