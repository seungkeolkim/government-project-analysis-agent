"""task 00085-6 — 조직 단위 관련성 판정 회귀·통합 테스트.

이 파일은 사용자 원문의 검증 항목 1·2·3·4·6~14 (5번 제외) 중 단위 테스트
(test_relevance_repository.py / test_relevance_routes.py / test_change_detection.py)
가 직접 다루지 않은 항목을 통합 시나리오로 채운다:

    - 검증 #6 후속: 다른 멤버 row 를 본인이 *수정* 시도 → 트리플 미매칭으로 본인 row 새로
      만들어지고 동료 row 는 그대로 (안 1 단일 UNIQUE 의 자연스러운 동작 확인).
    - 검증 #8: 목록 셀에 본인 큰 배지·카운터·툴팁 마크업이 정확히 렌더되는지 (TestClient
      HTML 응답 검사).
    - 검증 #9: hover 툴팁에 본인 소속 조직 평가 (조직명 + 판정) 가 노출되는지.
    - 검증 #10: 비로그인 시 카운터·OTHERS 정보는 그대로, 본인 영역만 비활성 (readonly
      span / 본인 영역 안내 문구).
    - 검증 #11: 상세 페이지에 mine_personal / mine_organization / others / history 가
      모두 풀어 표시되고 본인 row 에만 [수정][삭제] 버튼이 노출되는지.
    - 검증 #12: 입력 모달의 '판정 주체' 라디오 — 무소속/단일/복수 조직 케이스 마크업.
    - 검증 #14 후속: 목록 페이지 GET 의 추가 쿼리가 1~2 개로 고정되는지 (N+1 ceiling
      회귀 차단). repository 단위 테스트 (test_summary_n_plus_1_avoidance_single_query)
      는 헬퍼 1 회 SELECT 만 검증하지만, 본 테스트는 라우트·템플릿·읽음·즐겨찾기 헬퍼까지
      포함한 페이지 GET 전체 흐름의 N+1 회귀를 더 넓은 ceiling 으로 차단한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    Organization,
    RelevanceJudgment,
    User,
    UserOrganization,
)
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# fixtures — TestClient + 데이터 시드
# ---------------------------------------------------------------------------


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """TestClient 를 생성한다 — relevance 라우트 + index 페이지 모두 동일 앱 인스턴스."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _register_and_login(client: TestClient, username: str, password: str = "password_123") -> int:
    """TestClient 로 회원가입 + 로그인 (세션 쿠키 자동 보존). user_id 를 반환한다."""
    client.post(
        "/auth/register",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    with session_scope() as s:
        user = s.execute(select(User).where(User.username == username)).scalar_one()
        return user.id


def _create_organization_with_member(name: str, member_user_id: int) -> int:
    """조직을 만들고 member_user_id 를 매핑한 뒤 organization_id 를 반환한다."""
    with session_scope() as s:
        org = Organization(name=name)
        s.add(org)
        s.flush()
        s.add(UserOrganization(user_id=member_user_id, organization_id=org.id))
        s.flush()
        return org.id


def _seed_canonical_with_announcement(canonical_key: str, source_id: str, title: str = "통합 공고") -> dict[str, int]:
    """canonical_project + 1 개의 is_current=True announcement 를 만들고 둘 다 PK 를 반환한다."""
    with session_scope() as s:
        cp = CanonicalProject(canonical_key=canonical_key, key_scheme="official")
        s.add(cp)
        s.flush()
        ann = Announcement(
            source_announcement_id=source_id,
            source_type="IRIS",
            title=title,
            status=AnnouncementStatus.RECEIVING,
            agency="기관",
            canonical_group_id=cp.id,
            canonical_key=canonical_key,
            canonical_key_scheme="official",
            deadline_at=datetime(2026, 12, 31, tzinfo=UTC),
            is_current=True,
            scraped_at=datetime.now(tz=UTC),
            raw_metadata={},
        )
        s.add(ann)
        s.flush()
        return {"canonical_id": cp.id, "announcement_id": ann.id}


# ---------------------------------------------------------------------------
# 검증 #6 후속 — 다른 멤버 조직 row 를 본인이 '수정' (POST) 시도 시 동료 row 보존
# ---------------------------------------------------------------------------


def test_other_member_organization_row_is_independent_on_update(
    client: TestClient, test_engine: Engine
) -> None:
    """동료가 만든 조직 row 는 본인이 같은 조직으로 POST 해도 영향받지 않는다.

    안 1 단일 UNIQUE (canonical, user, organization_id) 가 user_id 를 키에 포함하므로
    같은 (canonical, organization_id) 라도 user_id 가 다르면 별개 row. 본인이 POST 하면
    본인 트리플의 신규 row 만 INSERT 되고 동료 row 는 그대로 유지된다.

    사용자 원문 검증 #6 의 의도 — '다른 사용자 row 를 본인이 못 지움' — 가 확장되어,
    수정 (POST) 도 같은 의미로 동료 row 를 건드리지 않음을 보장한다.
    """
    me_id = _register_and_login(client, "integ_me")
    seeded = _seed_canonical_with_announcement(
        "official:integ-other-member", "INTEG-OM-1"
    )
    canonical_id = seeded["canonical_id"]
    org_id = _create_organization_with_member("integ-shared-org", me_id)

    # 동료(peer) 사용자도 같은 조직 멤버로 추가하고 row 작성 — 라우터가 user_id 자동 필터를
    # 적용하므로 별도 client 로 등록 필요. 등록만 직접 DB 로 시드한다.
    with session_scope() as session:
        peer = User(username="integ_peer", password_hash="dummy")
        session.add(peer)
        session.flush()
        session.add(UserOrganization(user_id=peer.id, organization_id=org_id))
        # peer 의 조직 입장 row — verdict='관련'
        session.add(
            RelevanceJudgment(
                canonical_project_id=canonical_id,
                user_id=peer.id,
                organization_id=org_id,
                verdict="관련",
                reason="동료 사유",
            )
        )

    # me 가 같은 조직 입장으로 POST → me 의 조직 row 가 새로 생긴다.
    response = client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "무관", "reason": "내 사유", "organization_id": org_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["organization_id"] == org_id
    assert body["verdict"] == "무관"
    assert body["user_id"] == me_id

    # 동료 row 는 그대로 유지되어야 한다.
    with session_scope() as session:
        rows = session.execute(
            select(RelevanceJudgment).where(
                RelevanceJudgment.canonical_project_id == canonical_id,
                RelevanceJudgment.organization_id == org_id,
            ).order_by(RelevanceJudgment.user_id.asc())
        ).scalars().all()
        assert len(rows) == 2
        verdicts = sorted((row.user_id, row.verdict) for row in rows)
        # 작은 user_id (me) 가 먼저, 동료 row 는 verdict='관련' 그대로.
        peer_row = next(r for r in rows if r.user_id != me_id)
        my_row = next(r for r in rows if r.user_id == me_id)
        assert peer_row.verdict == "관련"
        assert peer_row.reason == "동료 사유"
        assert my_row.verdict == "무관"
        assert my_row.reason == "내 사유"


# ---------------------------------------------------------------------------
# 검증 #8 / #9 / #10 — 목록 셀 본인 배지 + 카운터 + 툴팁 + 비로그인 흐름
# ---------------------------------------------------------------------------


def test_list_cell_renders_my_badge_counter_and_organization_tooltip(
    client: TestClient, test_engine: Engine
) -> None:
    """로그인 사용자의 목록 페이지에서 큰 배지·카운터·hover 툴팁 마크업이 모두 노출된다.

    검증 #8: 본인 큰 배지 (개인 우선) + 카운터.
    검증 #9: hover 툴팁에 본인 소속 조직 평가 (조직명 + 판정).
    """
    me_id = _register_and_login(client, "integ_render_me")
    seeded = _seed_canonical_with_announcement(
        "official:integ-render-1", "INTEG-RENDER-1"
    )
    canonical_id = seeded["canonical_id"]
    org_id = _create_organization_with_member("렌더링-조직", me_id)

    # 본인: 개인 + 본인이 만든 조직 row 모두 보유 — 큰 배지는 개인 우선 (관련).
    client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련", "reason": "내 개인", "organization_id": None},
    )
    client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "무관", "reason": "내 조직", "organization_id": org_id},
    )

    # OTHERS 시드 — 다른 사용자가 개인/조직 row 작성 (카운터에 들어감).
    with session_scope() as session:
        peer = User(username="integ_render_peer", password_hash="dummy")
        session.add(peer)
        session.flush()
        session.add(UserOrganization(user_id=peer.id, organization_id=org_id))
        session.add(
            RelevanceJudgment(
                canonical_project_id=canonical_id,
                user_id=peer.id,
                organization_id=None,
                verdict="관련",
                reason="동료 개인",
            )
        )
        session.add(
            RelevanceJudgment(
                canonical_project_id=canonical_id,
                user_id=peer.id,
                organization_id=org_id,
                verdict="무관",
                reason="동료 조직",
            )
        )

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # 본인 큰 배지 — 개인 우선이므로 'related' (관련).
    assert "rj-badge--related" in html, "본인 개인 row 의 verdict 가 큰 배지로 노출돼야 한다."
    # 카운터 (OTHERS = 동료 2 건 — 관련 1, 무관 1).
    assert "rj-counter" in html
    # ✅ 1 ❌ 1 표기를 단언 — 공백 차이를 허용하기 위해 핵심 토큰만 검증.
    # task 00086 — ❓(미검토) 셀은 매크로에서 제거되어 더 이상 렌더되지 않는다.
    assert "✅ 1" in html
    assert "❌ 1" in html
    assert "❓" not in html, "task 00086 — ❓(미검토) 카운터 셀은 더 이상 노출되지 않아야 한다."
    # hover 툴팁에 본인 조직명 + verdict — \"렌더링-조직\" 이 mine_organization 영역으로.
    assert "렌더링-조직" in html, "hover 툴팁에 본인 조직명이 노출돼야 한다."
    assert "rj-tooltip" in html


def test_list_cell_anonymous_shows_counter_and_disables_owner_area(
    client: TestClient, test_engine: Engine
) -> None:
    """비로그인 GET / 의 관련성 셀에 카운터·OTHERS 정보 동일 노출 + 본인 영역 비활성.

    검증 #10: 비로그인 = 로그인 동일 노출 (본인 영역만 비활성).
    """
    # 데이터 시드 — 로그인 없이 직접 DB 에 OTHERS row 만 만든다.
    seeded = _seed_canonical_with_announcement(
        "official:integ-anon-1", "INTEG-ANON-1"
    )
    canonical_id = seeded["canonical_id"]
    with session_scope() as session:
        author = User(username="integ_anon_author", password_hash="dummy")
        session.add(author)
        session.flush()
        session.add(
            RelevanceJudgment(
                canonical_project_id=canonical_id,
                user_id=author.id,
                organization_id=None,
                verdict="관련",
                reason="OTHERS",
            )
        )

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # 비로그인은 본인 row 가 없으므로 큰 배지는 미검토 + readonly span.
    assert "rj-badge--unreviewed" in html
    assert "rj-badge--readonly" in html, "비로그인 큰 배지는 readonly span 으로 렌더돼야 한다."
    # 카운터는 OTHERS = 1 (관련) — 비로그인도 동일 노출.
    assert "rj-counter" in html
    assert "✅ 1" in html
    # 비로그인이라 모달이 렌더되지 않아야 한다 — _relevance_modal.html 의 {% if current_user %}.
    assert 'id="relevance-modal"' not in html, "비로그인은 모달이 렌더되지 않아야 한다."


# ---------------------------------------------------------------------------
# 검증 #11 — 상세 페이지에 모든 행 풀어 표시 + 본인 row 만 수정·삭제 버튼
# ---------------------------------------------------------------------------


def test_detail_page_unfolds_rows_and_owner_actions(
    client: TestClient, test_engine: Engine
) -> None:
    """상세 페이지에서 mine / others / history 가 모두 풀어 표시되고 본인 row 에만
    [수정][삭제] 버튼이 노출된다.

    검증 #11 + 결정 3 (비로그인 = 동일 노출) 의 본인 영역만 비활성 의미를 함께 확인.
    """
    me_id = _register_and_login(client, "integ_detail_me")
    seeded = _seed_canonical_with_announcement(
        "official:integ-detail-1", "INTEG-DETAIL-1", title="상세 통합 공고"
    )
    canonical_id = seeded["canonical_id"]
    announcement_id = seeded["announcement_id"]

    # 본인 — 개인 row 1 개. OTHERS — 다른 사용자 1 명이 개인 row 1 개.
    client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련", "reason": "내 개인 row"},
    )
    with session_scope() as session:
        peer = User(username="integ_detail_peer", password_hash="dummy")
        session.add(peer)
        session.flush()
        session.add(
            RelevanceJudgment(
                canonical_project_id=canonical_id,
                user_id=peer.id,
                organization_id=None,
                verdict="무관",
                reason="동료 개인 row",
            )
        )

    # 로그인 사용자 상세 페이지 — 본인 영역에 [수정][삭제] 버튼 + OTHERS 행 노출.
    response = client.get(f"/announcements/{announcement_id}")
    assert response.status_code == 200
    html = response.text
    assert "rj-detail-section" in html
    assert "rj-self-group" in html
    assert "rj-others-group" in html
    assert "rj-detail-edit-btn" in html, "본인 row 에 [수정] 버튼 마크업이 있어야 한다."
    assert "rj-detail-delete-btn" in html, "본인 row 에 [삭제] 버튼 마크업이 있어야 한다."
    assert "내 개인 row" in html
    assert "integ_detail_peer" in html, "OTHERS 행에 동료 username 이 노출돼야 한다."
    assert "동료 개인 row" in html

    # 비로그인 클라이언트 (logout) — 본인 영역 안내문 + OTHERS 동일 노출 + 본인 액션 버튼 없음.
    # /auth/logout 은 POST 라우트 (GET 으로 호출하면 405 이라 세션이 그대로 남는다).
    client.post("/auth/logout", follow_redirects=False)
    anon_response = client.get(f"/announcements/{announcement_id}")
    assert anon_response.status_code == 200
    anon_html = anon_response.text
    assert "rj-others-group" in anon_html
    assert "동료 개인 row" in anon_html
    assert "로그인 후" in anon_html, "비로그인 본인 영역 안내문이 표시돼야 한다."
    assert "rj-detail-edit-btn" not in anon_html, "비로그인은 본인 액션 버튼이 없어야 한다."
    assert "rj-detail-delete-btn" not in anon_html


# ---------------------------------------------------------------------------
# 검증 #12 — 입력 모달 '판정 주체' 라디오 (무소속 / 단일 / 복수)
# ---------------------------------------------------------------------------


def test_modal_radio_unaffiliated_user_disables_organization_option(
    client: TestClient, test_engine: Engine
) -> None:
    """무소속 사용자: '조직' 라디오 자체가 disabled 처리되고 드롭다운 행은 렌더되지 않는다."""
    _register_and_login(client, "integ_modal_unaffiliated")
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    # 무소속이면 user_organization_options 가 빈 리스트 → 라디오 disabled + 드롭다운 행 미렌더.
    assert "rj-modal__subject-group" in html
    assert "rj-modal__subject-label--disabled" in html
    # 무소속 사용자에게 드롭다운 행 자체가 존재하지 않아야 한다 (modal 의 {% if has_any_organization %}).
    assert "rj-modal-organization-row" not in html


def test_modal_radio_single_organization_uses_inline_label(
    client: TestClient, test_engine: Engine
) -> None:
    """단일 조직 소속: 라디오 라벨에 조직명이 직접 노출되고 드롭다운 select 는 없다."""
    me_id = _register_and_login(client, "integ_modal_single")
    _create_organization_with_member("단일조직-라벨", me_id)
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "rj-modal__subject-group" in html
    # 단일 조직 라벨은 \"조직 판정 (단일조직-라벨)\" 형태로 직접 노출.
    assert "단일조직-라벨" in html
    assert "rj-modal-organization-row" in html
    assert 'data-modal-org-mode="single"' in html
    # 단일 모드에서는 select 요소가 없다 (자동 채움).
    assert 'id="rj-modal-organization"' not in html


def test_modal_radio_multiple_organizations_renders_dropdown(
    client: TestClient, test_engine: Engine
) -> None:
    """복수 조직 소속: 드롭다운 select 가 렌더되고 옵션이 모두 노출된다."""
    me_id = _register_and_login(client, "integ_modal_multi")
    _create_organization_with_member("멀티-조직-A", me_id)
    _create_organization_with_member("멀티-조직-B", me_id)
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "rj-modal__subject-group" in html
    assert "rj-modal-organization-row" in html
    assert 'data-modal-org-mode="multiple"' in html
    assert 'id="rj-modal-organization"' in html, "복수 모드에서는 select 요소가 렌더돼야 한다."
    assert "멀티-조직-A" in html
    assert "멀티-조직-B" in html


# ---------------------------------------------------------------------------
# 검증 #14 — 목록 페이지 GET 의 N+1 ceiling
# ---------------------------------------------------------------------------


def test_list_page_get_query_count_within_ceiling(
    client: TestClient, test_engine: Engine
) -> None:
    """목록 페이지 GET 에서 발행되는 SELECT 수가 합리적인 ceiling 안에 머무른다.

    repository 단위 테스트는 summary 헬퍼 자체가 1 회 SELECT 임을 검증하지만, 본 테스트는
    라우트·템플릿·사용자 라벨링·즐겨찾기 헬퍼까지 포함한 페이지 GET 전체 흐름의 N+1 회귀를
    더 넓은 ceiling 으로 차단한다.

    공고 5 건 vs 10 건의 SELECT 수 차이를 측정해, 공고 수에 비례해 SELECT 가 늘지 않는지
    (= 페이지 row 수에 비례한 N+1 회귀가 없는지) 를 검증한다 — 절대 수치는 라우트의
    구조 변화에 따라 변할 수 있어 차이 기반이 더 안전.
    """
    me_id = _register_and_login(client, "integ_n1_me")
    org_id = _create_organization_with_member("N1-조직", me_id)

    def seed_announcements(n: int, prefix: str) -> list[int]:
        """n 개의 canonical+announcement 묶음 + 각각 OTHERS row 1 개를 시드. canonical id 들 반환."""
        canonical_ids: list[int] = []
        with session_scope() as session:
            for i in range(n):
                canonical = CanonicalProject(
                    canonical_key=f"official:{prefix}-{i:03d}", key_scheme="official"
                )
                session.add(canonical)
                session.flush()
                canonical_ids.append(canonical.id)
                session.add(
                    Announcement(
                        source_announcement_id=f"{prefix}-{i:03d}",
                        source_type="IRIS",
                        title=f"{prefix}-{i:03d} 공고",
                        status=AnnouncementStatus.RECEIVING,
                        agency="기관",
                        canonical_group_id=canonical.id,
                        canonical_key=f"official:{prefix}-{i:03d}",
                        canonical_key_scheme="official",
                        deadline_at=datetime(2026, 12, 31, tzinfo=UTC),
                        is_current=True,
                        scraped_at=datetime.now(tz=UTC),
                        raw_metadata={},
                    )
                )
                # OTHERS row 시드 — 다른 사용자가 row 를 가지면 summary 헬퍼가 카운트한다.
                session.add(
                    RelevanceJudgment(
                        canonical_project_id=canonical.id,
                        user_id=me_id,  # 나의 row 도 1 개씩 — 본인 큰 배지가 채워진다.
                        organization_id=org_id,
                        verdict="관련",
                    )
                )
        return canonical_ids

    # 5 개 시드 → GET / 의 SELECT 수 측정.
    seed_announcements(5, "N1A")
    select_count_5 = _measure_select_count(test_engine, lambda: client.get("/?group=off"))
    assert select_count_5 > 0, "smoke: SELECT 가 1 개 이상은 발행돼야 한다."

    # 10 개 시드 (총 15 개) → 동일 페이지 GET 의 SELECT 수 측정.
    seed_announcements(10, "N1B")
    select_count_15 = _measure_select_count(test_engine, lambda: client.get("/?group=off"))

    # row 수가 3 배가 되어도 SELECT 수 증가는 작은 상수 (≤ 3) 이내여야 한다.
    # N+1 이라면 row 당 1 개씩 늘어나 select_count_15 - select_count_5 ≥ 10 이 된다.
    assert select_count_15 - select_count_5 <= 3, (
        f"N+1 회귀 의심 — 공고 5 건일 때 SELECT={select_count_5}, "
        f"15 건일 때 SELECT={select_count_15}. row 수에 비례해 늘면 N+1 회귀."
    )


def _measure_select_count(test_engine: Engine, fn) -> int:
    """test_engine 에서 fn() 실행 동안 발행되는 SELECT 수를 카운트해 반환한다."""
    counter = {"value": 0}

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["value"] += 1

    event.listen(test_engine, "before_cursor_execute", _listener)
    try:
        fn()
    finally:
        event.remove(test_engine, "before_cursor_execute", _listener)
    return counter["value"]


# ---------------------------------------------------------------------------
# task 00086 — 관련성 카운터에서 ❓(미검토) 제거 회귀 테스트
# ---------------------------------------------------------------------------
#
# 매크로(`_relevance_badge_macro.html`) 출력 자체를 직접 렌더해 검증한다.
# 이유:
#   - DB CHECK 가 verdict='관련'/'무관' 만 허용하므로 실데이터로는
#     others_count_unreviewed > 0 인 RelevanceSummary 를 만들 수 없다.
#   - "❓ 만 있을 때 카운터가 렌더되지 않는다" 라는 매크로 분기 동작은
#     Jinja2 환경에서 RelevanceSummary 를 직접 구성해 렌더해야 검증 가능.


def _render_relevance_macro(summary, current_user=None, canonical_id: int = 1) -> str:
    """`_relevance_badge_macro.html` 의 relevance_badge 매크로를 직접 렌더한다.

    실제 앱과 동일한 templates 디렉토리를 가리키는 Jinja2 Environment 를 만든다.
    매크로는 외부 의존이 없으므로 (DB·라우트 무관) 직접 렌더가 가능하다.
    """
    from jinja2 import Environment, FileSystemLoader

    from app.web.main import TEMPLATES_DIR

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.from_string(
        '{% from "_relevance_badge_macro.html" import relevance_badge %}'
        "{{ relevance_badge(canonical_id, summary, current_user) }}"
    )
    return template.render(
        canonical_id=canonical_id,
        summary=summary,
        current_user=current_user,
    )


def test_macro_does_not_render_question_mark_cell() -> None:
    """매크로 출력에 ❓ 문자가 절대 등장하지 않는다 (task 00086).

    OTHERS 가 ✅·❌ 카운트만 있는 일반 케이스에서, 카운터 영역에는 ✅·❌ 두 셀만
    노출되고 ❓(미검토) 셀은 더 이상 렌더되지 않아야 한다.
    """
    from app.db.repository import RelevanceSummary

    summary_with_visible_others = RelevanceSummary(
        mine_personal=None,
        mine_organization=(),
        others=(),
        others_count_related=2,
        others_count_unrelated=1,
        others_count_unreviewed=0,
    )
    rendered = _render_relevance_macro(summary_with_visible_others)
    assert "rj-counter" in rendered, "✅·❌ 가 있을 때 카운터는 렌더돼야 한다."
    assert "✅ 2" in rendered
    assert "❌ 1" in rendered
    assert "❓" not in rendered, (
        "task 00086 — 매크로 출력 어디에도 ❓(미검토) 문자가 등장하지 않아야 한다."
    )
    assert "rj-counter__cell--unreviewed" not in rendered, (
        "task 00086 — ❓ 셀 클래스도 렌더되지 않아야 한다."
    )


def test_macro_does_not_render_counter_when_only_unreviewed_others_present() -> None:
    """✅·❌ 합이 0 이고 ❓(미검토) 만 있을 때 카운터 div 자체가 렌더되지 않는다.

    task 00086 — 카운터 표시 규칙이 OTHERS 의 ✅·❌ 합만 보고 결정되도록 변경됐다.
    DB CHECK 가 정상 데이터에서는 others_count_unreviewed=0 을 보장하지만,
    매크로 분기 자체가 미검토 카운트를 무시하는지 명시적으로 회귀 차단한다.
    """
    from app.db.repository import RelevanceSummary

    summary_only_unreviewed = RelevanceSummary(
        mine_personal=None,
        mine_organization=(),
        others=(),
        others_count_related=0,
        others_count_unrelated=0,
        others_count_unreviewed=5,
    )
    rendered = _render_relevance_macro(summary_only_unreviewed)
    assert "rj-counter" not in rendered, (
        "task 00086 — ✅·❌ 합이 0 이면 ❓ 카운트와 무관하게 카운터 자체가 렌더되지 않아야 한다."
    )
    assert "❓" not in rendered
