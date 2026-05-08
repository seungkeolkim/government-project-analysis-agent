"""관련성 판정 라우터 HTTP 통합 테스트.

TestClient 로 세 엔드포인트를 커버한다:
    POST   /canonical/{id}/relevance
    DELETE /canonical/{id}/relevance
    GET    /canonical/{id}/relevance/history

task 00085 — 조직 단위 판정 확장 시나리오:
    - POST/DELETE body 의 ``organization_id`` (선택) 동작.
    - 본인 소속 외 organization_id → 422.
    - 무소속 사용자 organization_id 지정 → 422.
    - 다른 멤버가 만든 조직 row 삭제 시도 → 404 (자동 user_id 필터로 미매칭).
    - 비로그인 GET /history 허용 (로그인과 동일 노출).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db.models import CanonicalProject, Organization, User, UserOrganization
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    """회원가입 후 세션 쿠키가 유지되는 TestClient."""
    client.post(
        "/auth/register",
        data={"username": "rj_route_user", "password": "password_123"},
        follow_redirects=False,
    )
    return client


@pytest.fixture
def canonical_id(test_engine: Engine) -> int:
    """테스트용 CanonicalProject 를 DB 에 생성하고 id 를 반환한다."""
    with session_scope() as s:
        cp = CanonicalProject(
            canonical_key="official:route-test-001",
            key_scheme="official",
        )
        s.add(cp)
        s.flush()
        return cp.id


# ---------------------------------------------------------------------------
# 비로그인 정책: POST/DELETE → 401, GET /history → 200 (task 00085 결정 3)
# ---------------------------------------------------------------------------


def test_set_relevance_requires_login(client: TestClient, canonical_id: int) -> None:
    resp = client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련"},
    )
    assert resp.status_code == 401


def test_delete_relevance_requires_login(client: TestClient, canonical_id: int) -> None:
    resp = client.delete(f"/canonical/{canonical_id}/relevance")
    assert resp.status_code == 401


def test_get_history_allows_anonymous(client: TestClient, canonical_id: int) -> None:
    """task 00085 결정 3 — 비로그인 사용자도 history 조회 가능."""
    resp = client.get(f"/canonical/{canonical_id}/relevance/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["canonical_project_id"] == canonical_id
    assert data["history"] == []


# ---------------------------------------------------------------------------
# 존재하지 않는 canonical → 404
# ---------------------------------------------------------------------------


def test_set_relevance_canonical_not_found(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post(
        "/canonical/999999/relevance",
        json={"verdict": "관련"},
    )
    assert resp.status_code == 404


def test_delete_relevance_canonical_not_found(logged_in_client: TestClient) -> None:
    resp = logged_in_client.delete("/canonical/999999/relevance")
    assert resp.status_code == 404


def test_get_history_canonical_not_found(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/canonical/999999/relevance/history")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST — 판정 저장
# ---------------------------------------------------------------------------


def test_set_relevance_success(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련", "reason": "테스트 이유"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "관련"
    assert data["reason"] == "테스트 이유"
    assert data["canonical_project_id"] == canonical_id


def test_set_relevance_invalid_verdict(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "잘못된값"},
    )
    assert resp.status_code == 422


def test_set_relevance_overwrite(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련"},
    )
    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "무관"},
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "무관"

    # 히스토리에 이전 판정이 남아야 한다
    hist_resp = logged_in_client.get(
        f"/canonical/{canonical_id}/relevance/history"
    )
    assert hist_resp.status_code == 200
    hist_data = hist_resp.json()
    assert len(hist_data["history"]) == 1
    assert hist_data["history"][0]["verdict"] == "관련"
    assert hist_data["history"][0]["archive_reason"] == "user_overwrite"


# ---------------------------------------------------------------------------
# DELETE — 판정 삭제
# ---------------------------------------------------------------------------


def test_delete_relevance_success(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련"},
    )
    resp = logged_in_client.delete(f"/canonical/{canonical_id}/relevance")
    assert resp.status_code == 200
    assert resp.json()["detail"] == "삭제되었습니다."


def test_delete_relevance_not_found(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    resp = logged_in_client.delete(f"/canonical/{canonical_id}/relevance")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET history
# ---------------------------------------------------------------------------


def test_get_history_empty(logged_in_client: TestClient, canonical_id: int) -> None:
    resp = logged_in_client.get(f"/canonical/{canonical_id}/relevance/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["canonical_project_id"] == canonical_id
    assert data["history"] == []


def test_get_history_after_delete(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련"},
    )
    logged_in_client.delete(f"/canonical/{canonical_id}/relevance")

    resp = logged_in_client.get(f"/canonical/{canonical_id}/relevance/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["history"]) == 1
    item = data["history"][0]
    assert item["verdict"] == "관련"
    assert item["username"] == "rj_route_user"
    assert item["archive_reason"] == "user_overwrite"
    # task 00085 — history 응답에 organization_id 가 포함되어야 한다 (개인 row 라 None).
    assert item["organization_id"] is None


# ---------------------------------------------------------------------------
# task 00085 — organization_id 동작 검증
# ---------------------------------------------------------------------------


def _create_organization(name: str) -> int:
    """테스트용 조직을 만들고 PK 를 반환한다."""
    with session_scope() as s:
        org = Organization(name=name)
        s.add(org)
        s.flush()
        return org.id


def _add_user_to_organization(username: str, organization_id: int) -> int:
    """username 사용자에게 organization_id 의 매핑을 추가하고 user_id 반환."""
    with session_scope() as s:
        user = s.execute(
            __import__("sqlalchemy").select(User).where(User.username == username)
        ).scalar_one()
        s.add(UserOrganization(user_id=user.id, organization_id=organization_id))
        s.flush()
        return user.id


def test_set_relevance_organization_member_succeeds(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인이 소속된 조직 PK 로 조직 판정 작성 → 200 + organization_id 응답."""
    org_id = _create_organization("route-org-member")
    _add_user_to_organization("rj_route_user", org_id)

    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={
            "verdict": "관련",
            "reason": "조직 입장",
            "organization_id": org_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "관련"
    assert data["organization_id"] == org_id


def test_set_relevance_non_member_organization_returns_422(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인 소속 외 조직 PK → 422 + 한국어 detail. 사용자 원문 검증 #3."""
    foreign_org_id = _create_organization("route-org-foreign")
    # rj_route_user 는 이 조직에 속하지 않음.

    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={
            "verdict": "관련",
            "organization_id": foreign_org_id,
        },
    )
    assert resp.status_code == 422
    assert "본인 소속 조직" in resp.json()["detail"]


def test_set_relevance_unaffiliated_user_organization_returns_422(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """무소속 사용자가 organization_id 를 지정해도 422 (소속 매핑이 비어 있음).

    사용자 원문 검증 #4 의 서버 측 거부 분기.
    """
    org_id = _create_organization("route-org-no-membership")
    # 사용자에게 매핑을 추가하지 않음 — 무소속 상태 유지.

    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "무관", "organization_id": org_id},
    )
    assert resp.status_code == 422
    assert "본인 소속 조직" in resp.json()["detail"]


def test_set_relevance_personal_and_organization_coexist(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """동일 canonical 에 개인 + 조직 row 가 동시 보유 가능 (안 1 트리플 슬롯)."""
    org_id = _create_organization("route-org-coexist")
    _add_user_to_organization("rj_route_user", org_id)

    # 개인 row
    resp_personal = logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련", "organization_id": None},
    )
    assert resp_personal.status_code == 200
    assert resp_personal.json()["organization_id"] is None

    # 조직 row — 다른 verdict 로 동시 보유
    resp_org = logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "무관", "organization_id": org_id},
    )
    assert resp_org.status_code == 200
    assert resp_org.json()["organization_id"] == org_id

    # history 는 트리플 단위 INSERT 만 일어나 비어 있어야 한다 (덮어쓰기 발생 X).
    hist_resp = logged_in_client.get(
        f"/canonical/{canonical_id}/relevance/history"
    )
    assert hist_resp.status_code == 200
    assert hist_resp.json()["history"] == []


def test_delete_relevance_with_organization_id_targets_org_row(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """body 에 organization_id 를 보내면 그 트리플 row 만 삭제, 개인 row 는 보존."""
    org_id = _create_organization("route-org-delete-target")
    _add_user_to_organization("rj_route_user", org_id)

    # 개인 + 조직 row 모두 생성
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련"},
    )
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "무관", "organization_id": org_id},
    )

    # 조직 row 만 삭제
    resp = logged_in_client.request(
        "DELETE",
        f"/canonical/{canonical_id}/relevance",
        json={"organization_id": org_id},
    )
    assert resp.status_code == 200

    # 다시 같은 트리플 삭제 시도 → 404 (이미 사라졌고 개인 row 만 남음).
    resp_again = logged_in_client.request(
        "DELETE",
        f"/canonical/{canonical_id}/relevance",
        json={"organization_id": org_id},
    )
    assert resp_again.status_code == 404
    assert resp_again.json()["detail"] == "삭제할 판정이 없습니다."

    # 개인 row 는 살아 있어야 하므로 body 없이 DELETE 하면 200 으로 잘 지워져야 한다.
    resp_personal = logged_in_client.delete(f"/canonical/{canonical_id}/relevance")
    assert resp_personal.status_code == 200


def test_delete_relevance_other_member_org_row_returns_404(
    logged_in_client: TestClient,
    client: TestClient,
    canonical_id: int,
) -> None:
    """다른 멤버가 만든 조직 row 를 본인이 삭제 시도 → 404.

    사용자 원문 검증 #6: 라우터가 user_id=current_user.id 자동 필터를 적용하므로
    다른 멤버가 만든 row 는 트리플 매칭에 잡히지 않아 404 가 된다 (가이드 결정).
    """
    org_id = _create_organization("route-org-shared")
    _add_user_to_organization("rj_route_user", org_id)

    # 다른 사용자(peer) 가 같은 조직 입장으로 row 작성
    peer_username = "rj_route_peer"
    peer_password = "password_456"
    # 별도 client 로 peer 등록 (logged_in_client 는 rj_route_user 세션 유지).
    from fastapi.testclient import TestClient as _TestClient
    from app.web.main import create_app

    peer_app = create_app()
    with _TestClient(peer_app) as peer_client:
        peer_client.post(
            "/auth/register",
            data={"username": peer_username, "password": peer_password},
            follow_redirects=False,
        )
        # peer 도 같은 조직에 매핑 추가
        _add_user_to_organization(peer_username, org_id)
        # peer 가 조직 row 작성
        peer_resp = peer_client.post(
            f"/canonical/{canonical_id}/relevance",
            json={"verdict": "관련", "organization_id": org_id},
        )
        assert peer_resp.status_code == 200

    # rj_route_user 가 peer 의 조직 row 삭제 시도 → 404 (트리플 미매칭).
    resp = logged_in_client.request(
        "DELETE",
        f"/canonical/{canonical_id}/relevance",
        json={"organization_id": org_id},
    )
    assert resp.status_code == 404


def test_history_response_includes_organization_id(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """조직 row 가 변경/삭제되면 history 응답에 organization_id 가 포함돼야 한다."""
    org_id = _create_organization("route-org-history")
    _add_user_to_organization("rj_route_user", org_id)

    # 조직 row 생성 → 같은 트리플 덮어쓰기로 history 발생
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련", "organization_id": org_id},
    )
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "무관", "organization_id": org_id},
    )

    resp = logged_in_client.get(f"/canonical/{canonical_id}/relevance/history")
    assert resp.status_code == 200
    items = resp.json()["history"]
    assert len(items) == 1
    assert items[0]["organization_id"] == org_id
    assert items[0]["verdict"] == "관련"
    assert items[0]["archive_reason"] == "user_overwrite"


def test_anonymous_history_sees_organization_rows(
    client: TestClient,
    logged_in_client: TestClient,
    canonical_id: int,
) -> None:
    """비로그인 GET /history 가 조직 판정의 history 도 그대로 보여줘야 한다 (결정 3)."""
    org_id = _create_organization("route-org-anon-view")
    _add_user_to_organization("rj_route_user", org_id)

    # 로그인 사용자가 조직 row 작성/덮어쓰기 → history 한 건 발생
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "관련", "organization_id": org_id},
    )
    logged_in_client.post(
        f"/canonical/{canonical_id}/relevance",
        json={"verdict": "무관", "organization_id": org_id},
    )

    # 비로그인 (logged_in_client 와 다른 client) 으로 history 조회
    anon_resp = client.get(f"/canonical/{canonical_id}/relevance/history")
    assert anon_resp.status_code == 200
    items = anon_resp.json()["history"]
    assert len(items) == 1
    assert items[0]["organization_id"] == org_id
    assert items[0]["username"] == "rj_route_user"
