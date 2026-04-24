"""즐겨찾기 API 라우터 HTTP 통합 테스트 (00036-4).

엔드포인트별 커버리지:
    GET    /favorites/folders                     - 401, 빈 트리, 트리 구조
    POST   /favorites/folders                     - 401, 생성, depth 제약 400, 중복 409
    PATCH  /favorites/folders/{id}                - 401, 이름 변경, 타 사용자 404
    DELETE /favorites/folders/{id}                - 401, 삭제, 타 사용자 404
    POST   /favorites/entries                     - 401, 추가, 중복 409, 폴더 불일치 404
    DELETE /favorites/entries/{id}                - 401, 삭제, 타 사용자 항목 404
    GET    /favorites/folders/{id}/entries        - 401, 목록, 페이지네이션
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db.models import CanonicalProject, FavoriteFolder, User
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """테스트용 FastAPI TestClient."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    """user_a 로 등록 후 로그인된 TestClient."""
    client.post(
        "/auth/register",
        data={"username": "fav_route_user_a", "password": "password_123"},
        follow_redirects=False,
    )
    return client


@pytest.fixture
def canonical_id(test_engine: Engine) -> int:
    """테스트용 CanonicalProject 를 DB 에 생성하고 id 를 반환한다."""
    with session_scope() as s:
        cp = CanonicalProject(
            canonical_key="official:fav-route-test-001",
            key_scheme="official",
        )
        s.add(cp)
        s.flush()
        return cp.id


@pytest.fixture
def other_user_folder_id(test_engine: Engine) -> int:
    """다른 사용자(user_b) 의 폴더 id 를 직접 DB 에 생성한다.

    소유권 격리 테스트에서 user_a 클라이언트로 이 id 에 접근 시 404 가 와야 한다.
    """
    with session_scope() as s:
        user_b = User(username="fav_other_user_b", password_hash="dummy_hash")
        s.add(user_b)
        s.flush()
        folder = FavoriteFolder(
            user_id=user_b.id,
            name="남의 폴더",
            depth=0,
        )
        s.add(folder)
        s.flush()
        return folder.id


# ---------------------------------------------------------------------------
# 비로그인 → 401
# ---------------------------------------------------------------------------


def test_list_folders_requires_login(client: TestClient) -> None:
    resp = client.get("/favorites/folders")
    assert resp.status_code == 401


def test_create_folder_requires_login(client: TestClient) -> None:
    resp = client.post("/favorites/folders", json={"name": "테스트폴더"})
    assert resp.status_code == 401


def test_rename_folder_requires_login(client: TestClient) -> None:
    resp = client.patch("/favorites/folders/1", json={"name": "새이름"})
    assert resp.status_code == 401


def test_delete_folder_requires_login(client: TestClient) -> None:
    resp = client.delete("/favorites/folders/1")
    assert resp.status_code == 401


def test_create_entry_requires_login(client: TestClient) -> None:
    resp = client.post(
        "/favorites/entries",
        json={"folder_id": 1, "canonical_project_id": 1},
    )
    assert resp.status_code == 401


def test_delete_entry_requires_login(client: TestClient) -> None:
    resp = client.delete("/favorites/entries/1")
    assert resp.status_code == 401


def test_list_entries_requires_login(client: TestClient) -> None:
    resp = client.get("/favorites/folders/1/entries")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /favorites/folders — 트리 조회
# ---------------------------------------------------------------------------


def test_list_folders_empty(logged_in_client: TestClient) -> None:
    resp = logged_in_client.get("/favorites/folders")
    assert resp.status_code == 200
    data = resp.json()
    assert data["folders"] == []


def test_list_folders_tree_structure(
    logged_in_client: TestClient,
) -> None:
    """루트 폴더와 하위 폴더가 트리로 정렬되어 반환된다."""
    # 루트 폴더 생성
    r1 = logged_in_client.post("/favorites/folders", json={"name": "루트A"})
    assert r1.status_code == 201
    root_id = r1.json()["id"]

    # 하위 폴더 생성
    r2 = logged_in_client.post(
        "/favorites/folders", json={"name": "자식A", "parent_id": root_id}
    )
    assert r2.status_code == 201

    resp = logged_in_client.get("/favorites/folders")
    assert resp.status_code == 200
    folders = resp.json()["folders"]

    # 루트가 최상위에 있어야 한다
    assert any(f["name"] == "루트A" for f in folders)
    root_node = next(f for f in folders if f["name"] == "루트A")
    assert root_node["depth"] == 0
    assert len(root_node["children"]) == 1
    child = root_node["children"][0]
    assert child["name"] == "자식A"
    assert child["depth"] == 1


# ---------------------------------------------------------------------------
# POST /favorites/folders — 생성
# ---------------------------------------------------------------------------


def test_create_root_folder(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post("/favorites/folders", json={"name": "새 루트폴더"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "새 루트폴더"
    assert data["depth"] == 0
    assert data["parent_id"] is None
    assert "id" in data
    assert "created_at" in data


def test_create_child_folder(logged_in_client: TestClient) -> None:
    root_resp = logged_in_client.post("/favorites/folders", json={"name": "부모폴더"})
    root_id = root_resp.json()["id"]

    resp = logged_in_client.post(
        "/favorites/folders", json={"name": "자식폴더", "parent_id": root_id}
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["depth"] == 1
    assert data["parent_id"] == root_id


def test_create_grandchild_folder_returns_400(logged_in_client: TestClient) -> None:
    """depth 2 제약: 자식의 자식(손자) 폴더 생성 시도 → 400."""
    root = logged_in_client.post("/favorites/folders", json={"name": "루트"})
    root_id = root.json()["id"]
    child = logged_in_client.post(
        "/favorites/folders", json={"name": "자식", "parent_id": root_id}
    )
    child_id = child.json()["id"]

    resp = logged_in_client.post(
        "/favorites/folders", json={"name": "손자", "parent_id": child_id}
    )
    assert resp.status_code == 400


def test_create_folder_duplicate_name_returns_409(logged_in_client: TestClient) -> None:
    """같은 이름의 루트 폴더 중복 생성 시 409."""
    logged_in_client.post("/favorites/folders", json={"name": "중복폴더"})
    resp = logged_in_client.post("/favorites/folders", json={"name": "중복폴더"})
    assert resp.status_code == 409


def test_create_folder_with_other_user_parent_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    """타 사용자 폴더를 parent_id 로 지정 시 404."""
    resp = logged_in_client.post(
        "/favorites/folders",
        json={"name": "침입 폴더", "parent_id": other_user_folder_id},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /favorites/folders/{id} — 이름 변경
# ---------------------------------------------------------------------------


def test_rename_folder_success(logged_in_client: TestClient) -> None:
    create_resp = logged_in_client.post("/favorites/folders", json={"name": "원래이름"})
    folder_id = create_resp.json()["id"]

    resp = logged_in_client.patch(
        f"/favorites/folders/{folder_id}", json={"name": "새이름"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "새이름"


def test_rename_nonexistent_folder_returns_404(logged_in_client: TestClient) -> None:
    resp = logged_in_client.patch(
        "/favorites/folders/999999", json={"name": "없는폴더"}
    )
    assert resp.status_code == 404


def test_rename_other_user_folder_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    resp = logged_in_client.patch(
        f"/favorites/folders/{other_user_folder_id}", json={"name": "침입"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /favorites/folders/{id} — 삭제
# ---------------------------------------------------------------------------


def test_delete_folder_success(logged_in_client: TestClient) -> None:
    create_resp = logged_in_client.post("/favorites/folders", json={"name": "삭제할폴더"})
    folder_id = create_resp.json()["id"]

    resp = logged_in_client.delete(f"/favorites/folders/{folder_id}")
    assert resp.status_code == 200
    assert resp.json()["detail"] == "삭제되었습니다."

    # 삭제 후 트리에서 사라졌는지 확인
    tree_resp = logged_in_client.get("/favorites/folders")
    folder_ids = [f["id"] for f in tree_resp.json()["folders"]]
    assert folder_id not in folder_ids


def test_delete_nonexistent_folder_returns_404(logged_in_client: TestClient) -> None:
    resp = logged_in_client.delete("/favorites/folders/999999")
    assert resp.status_code == 404


def test_delete_other_user_folder_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    resp = logged_in_client.delete(f"/favorites/folders/{other_user_folder_id}")
    assert resp.status_code == 404


def test_delete_folder_cascades_entries(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """폴더 삭제 시 즐겨찾기 항목도 CASCADE 삭제된다."""
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "cascade폴더"})
    folder_id = folder_resp.json()["id"]

    entry_resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": canonical_id},
    )
    entry_id = entry_resp.json()["id"]

    # 폴더 삭제
    logged_in_client.delete(f"/favorites/folders/{folder_id}")

    # 항목도 삭제됐으므로 재삭제 시 404
    del_resp = logged_in_client.delete(f"/favorites/entries/{entry_id}")
    assert del_resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /favorites/entries — 항목 추가
# ---------------------------------------------------------------------------


def test_create_entry_success(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "항목추가폴더"})
    folder_id = folder_resp.json()["id"]

    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": canonical_id},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["folder_id"] == folder_id
    assert data["canonical_project_id"] == canonical_id
    assert "id" in data


def test_create_entry_duplicate_returns_409(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "중복항목폴더"})
    folder_id = folder_resp.json()["id"]

    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": canonical_id},
    )
    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": canonical_id},
    )
    assert resp.status_code == 409


def test_create_entry_nonexistent_folder_returns_404(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": 999999, "canonical_project_id": canonical_id},
    )
    assert resp.status_code == 404


def test_create_entry_nonexistent_canonical_returns_404(
    logged_in_client: TestClient,
) -> None:
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "canonical없는폴더"})
    folder_id = folder_resp.json()["id"]

    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": 999999},
    )
    assert resp.status_code == 404


def test_create_entry_other_user_folder_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int, canonical_id: int
) -> None:
    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": other_user_folder_id, "canonical_project_id": canonical_id},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /favorites/entries/{id} — 항목 제거
# ---------------------------------------------------------------------------


def test_delete_entry_success(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "항목삭제폴더"})
    folder_id = folder_resp.json()["id"]
    entry_resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": canonical_id},
    )
    entry_id = entry_resp.json()["id"]

    resp = logged_in_client.delete(f"/favorites/entries/{entry_id}")
    assert resp.status_code == 200
    assert resp.json()["detail"] == "삭제되었습니다."


def test_delete_nonexistent_entry_returns_404(logged_in_client: TestClient) -> None:
    resp = logged_in_client.delete("/favorites/entries/999999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /favorites/folders/{id}/entries — 목록 + 페이지네이션
# ---------------------------------------------------------------------------


def test_list_folder_entries_empty(logged_in_client: TestClient) -> None:
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "빈항목폴더"})
    folder_id = folder_resp.json()["id"]

    resp = logged_in_client.get(f"/favorites/folders/{folder_id}/entries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []
    assert data["folder_id"] == folder_id


def test_list_folder_entries_with_items(
    logged_in_client: TestClient, test_engine: Engine
) -> None:
    """항목이 있을 때 canonical_title 포함 반환."""
    # canonical 2개 생성
    with session_scope() as s:
        cp1 = CanonicalProject(
            canonical_key="official:fav-list-001",
            key_scheme="official",
            representative_title="테스트 과제 제목 1",
        )
        cp2 = CanonicalProject(
            canonical_key="official:fav-list-002",
            key_scheme="official",
            representative_title="테스트 과제 제목 2",
        )
        s.add_all([cp1, cp2])
        s.flush()
        cid1, cid2 = cp1.id, cp2.id

    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "목록확인폴더"})
    folder_id = folder_resp.json()["id"]

    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": cid1},
    )
    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": cid2},
    )

    resp = logged_in_client.get(f"/favorites/folders/{folder_id}/entries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2

    # canonical_title 이 포함돼야 한다
    titles = {item["canonical_title"] for item in data["items"]}
    assert "테스트 과제 제목 1" in titles
    assert "테스트 과제 제목 2" in titles


def test_list_folder_entries_pagination(
    logged_in_client: TestClient, test_engine: Engine
) -> None:
    """page_size=1 로 2개를 1개씩 조회한다."""
    with session_scope() as s:
        cp = CanonicalProject(
            canonical_key="official:fav-page-001", key_scheme="official"
        )
        cp2 = CanonicalProject(
            canonical_key="official:fav-page-002", key_scheme="official"
        )
        s.add_all([cp, cp2])
        s.flush()
        cid_a, cid_b = cp.id, cp2.id

    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "페이지네이션폴더"})
    folder_id = folder_resp.json()["id"]

    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": cid_a},
    )
    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "canonical_project_id": cid_b},
    )

    resp_p1 = logged_in_client.get(
        f"/favorites/folders/{folder_id}/entries?page=1&page_size=1"
    )
    assert resp_p1.status_code == 200
    d1 = resp_p1.json()
    assert d1["total"] == 2
    assert len(d1["items"]) == 1

    resp_p2 = logged_in_client.get(
        f"/favorites/folders/{folder_id}/entries?page=2&page_size=1"
    )
    assert resp_p2.status_code == 200
    d2 = resp_p2.json()
    assert len(d2["items"]) == 1

    # 두 페이지에서 반환된 항목이 달라야 한다
    assert d1["items"][0]["id"] != d2["items"][0]["id"]


def test_list_entries_other_user_folder_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    resp = logged_in_client.get(f"/favorites/folders/{other_user_folder_id}/entries")
    assert resp.status_code == 404
