"""관련성 판정 라우터 HTTP 통합 테스트.

TestClient 로 세 엔드포인트를 커버한다:
    POST   /canonical/{id}/relevance
    DELETE /canonical/{id}/relevance
    GET    /canonical/{id}/relevance/history
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db.models import CanonicalProject
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
# 비로그인 → 401
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


def test_get_history_requires_login(client: TestClient, canonical_id: int) -> None:
    resp = client.get(f"/canonical/{canonical_id}/relevance/history")
    assert resp.status_code == 401


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
