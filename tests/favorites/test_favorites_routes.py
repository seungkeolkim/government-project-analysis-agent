"""즐겨찾기 API 라우터 HTTP 통합 테스트 (task 00037 announcement 단위 갱신).

엔드포인트별 커버리지:
    GET    /favorites/folders                              - 401, 빈 트리, 트리 구조
    POST   /favorites/folders                              - 401, 생성, depth 제약 400, 중복 409
    PATCH  /favorites/folders/{id}                         - 401, 이름 변경, 타 사용자 404
    GET    /favorites/folders/{id}/delete-preview          - 401, cascade 개수 반환
    DELETE /favorites/folders/{id}                         - 401, 삭제(cascade), 타 사용자 404
    POST   /favorites/entries                              - 401, single 추가, bulk(siblings) 추가,
                                                             재추가(skipped 흡수), 타 사용자 폴더 404,
                                                             존재하지 않는 announcement 404
    PATCH  /favorites/entries/{id}                         - 401, 이동, 동일 폴더 no-op, 타 사용자 항목 404
    DELETE /favorites/entries/{id}                         - 401, 삭제, 타 사용자 항목 404
    GET    /favorites/folders/{id}/entries                 - 401, 목록, 페이지네이션, announcement 메타
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    FavoriteFolder,
    User,
)
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


def _make_canonical_with_announcements(
    key: str,
    *,
    source_types: tuple[str, ...] = ("IRIS",),
    title_prefix: str = "공고",
) -> tuple[int, list[int]]:
    """canonical + 각 source_type 당 is_current=True announcement 1개씩 생성.

    Returns:
        (canonical_id, [announcement_id, ...])
    """
    with session_scope() as s:
        cp = CanonicalProject(
            canonical_key=key,
            key_scheme="official",
            representative_title=f"{title_prefix} 대표",
        )
        s.add(cp)
        s.flush()
        canonical_id = cp.id
        announcement_ids: list[int] = []
        for index, source_type in enumerate(source_types):
            ann = Announcement(
                source_announcement_id=f"{key}_{index}",
                source_type=source_type,
                title=f"{title_prefix} {source_type}",
                status=AnnouncementStatus.RECEIVING,
                is_current=True,
                canonical_group_id=canonical_id,
            )
            s.add(ann)
            s.flush()
            announcement_ids.append(ann.id)
        return canonical_id, announcement_ids


@pytest.fixture
def announcement_id(test_engine: Engine) -> int:
    """canonical 1개 + is_current=True 공고 1건을 DB 에 만들고 announcement_id 반환."""
    _, ann_ids = _make_canonical_with_announcements("official:fav-route-test-001")
    return ann_ids[0]


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
    """비로그인 사용자는 폴더 트리를 조회할 수 없다."""
    resp = client.get("/favorites/folders")
    assert resp.status_code == 401


def test_create_folder_requires_login(client: TestClient) -> None:
    """비로그인 사용자는 폴더를 생성할 수 없다."""
    resp = client.post("/favorites/folders", json={"name": "테스트폴더"})
    assert resp.status_code == 401


def test_rename_folder_requires_login(client: TestClient) -> None:
    """비로그인 사용자는 폴더 이름을 변경할 수 없다."""
    resp = client.patch("/favorites/folders/1", json={"name": "새이름"})
    assert resp.status_code == 401


def test_delete_folder_requires_login(client: TestClient) -> None:
    """비로그인 사용자는 폴더를 삭제할 수 없다."""
    resp = client.delete("/favorites/folders/1")
    assert resp.status_code == 401


def test_create_entry_requires_login(client: TestClient) -> None:
    """비로그인 사용자는 즐겨찾기 항목을 추가할 수 없다."""
    resp = client.post(
        "/favorites/entries",
        json={"folder_id": 1, "announcement_id": 1},
    )
    assert resp.status_code == 401


def test_move_entry_requires_login(client: TestClient) -> None:
    """비로그인 사용자는 즐겨찾기 항목을 이동할 수 없다(task 00037 신규)."""
    resp = client.patch(
        "/favorites/entries/1",
        json={"target_folder_id": 1},
    )
    assert resp.status_code == 401


def test_delete_entry_requires_login(client: TestClient) -> None:
    """비로그인 사용자는 즐겨찾기 항목을 제거할 수 없다."""
    resp = client.delete("/favorites/entries/1")
    assert resp.status_code == 401


def test_list_entries_requires_login(client: TestClient) -> None:
    """비로그인 사용자는 폴더 내 항목 목록을 볼 수 없다."""
    resp = client.get("/favorites/folders/1/entries")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /favorites/folders — 트리 조회
# ---------------------------------------------------------------------------


def test_list_folders_empty(logged_in_client: TestClient) -> None:
    """폴더가 없으면 빈 배열을 반환한다."""
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
    """루트 폴더(parent_id=None) 생성 시 depth=0 으로 저장된다."""
    resp = logged_in_client.post("/favorites/folders", json={"name": "새 루트폴더"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "새 루트폴더"
    assert data["depth"] == 0
    assert data["parent_id"] is None
    assert "id" in data
    assert "created_at" in data


def test_create_child_folder(logged_in_client: TestClient) -> None:
    """루트 하위에 자식 폴더를 만들면 depth=1 로 저장된다."""
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
    """자기 폴더 이름을 성공적으로 변경한다."""
    create_resp = logged_in_client.post("/favorites/folders", json={"name": "원래이름"})
    folder_id = create_resp.json()["id"]

    resp = logged_in_client.patch(
        f"/favorites/folders/{folder_id}", json={"name": "새이름"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "새이름"


def test_rename_nonexistent_folder_returns_404(logged_in_client: TestClient) -> None:
    """없는 폴더 id 로 PATCH 시 404."""
    resp = logged_in_client.patch(
        "/favorites/folders/999999", json={"name": "없는폴더"}
    )
    assert resp.status_code == 404


def test_rename_other_user_folder_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    """타 사용자 폴더 이름 변경 시도 시 404."""
    resp = logged_in_client.patch(
        f"/favorites/folders/{other_user_folder_id}", json={"name": "침입"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /favorites/folders/{id}/delete-preview — task 00037 신규
# ---------------------------------------------------------------------------


def test_delete_preview_counts_cascade(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """삭제 미리보기가 하위 서브그룹 수 + entry 수를 정확히 반환한다."""
    root_resp = logged_in_client.post("/favorites/folders", json={"name": "프리뷰 루트"})
    root_id = root_resp.json()["id"]
    logged_in_client.post(
        "/favorites/folders", json={"name": "자식1", "parent_id": root_id}
    )
    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": root_id, "announcement_id": announcement_id},
    )

    resp = logged_in_client.get(f"/favorites/folders/{root_id}/delete-preview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["folder_id"] == root_id
    assert data["folder_name"] == "프리뷰 루트"
    assert data["subfolder_count"] == 1
    assert data["entry_count"] == 1


def test_delete_preview_other_user_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    """타 사용자 폴더 미리보기 시도 시 404."""
    resp = logged_in_client.get(
        f"/favorites/folders/{other_user_folder_id}/delete-preview"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /favorites/folders/{id} — 삭제 (task 00037 — cascade 확장)
# ---------------------------------------------------------------------------


def test_delete_folder_success(logged_in_client: TestClient) -> None:
    """루트 폴더 삭제 후 트리에서 사라진다."""
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
    """없는 폴더 id 로 DELETE 시 404."""
    resp = logged_in_client.delete("/favorites/folders/999999")
    assert resp.status_code == 404


def test_delete_other_user_folder_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    """타 사용자 폴더 삭제 시도 시 404."""
    resp = logged_in_client.delete(f"/favorites/folders/{other_user_folder_id}")
    assert resp.status_code == 404


def test_delete_folder_cascades_entries_and_subfolders(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """task 00037 #2 — 루트 폴더 삭제 시 자식 폴더 + 공고 모두 cascade 삭제.

    \"자식이 루트로 격상\" 되는 기존 동작이 제거되었음을 검증한다.
    """
    root_resp = logged_in_client.post("/favorites/folders", json={"name": "루트"})
    root_id = root_resp.json()["id"]
    child_resp = logged_in_client.post(
        "/favorites/folders", json={"name": "자식", "parent_id": root_id}
    )
    child_id = child_resp.json()["id"]

    entry_resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": root_id, "announcement_id": announcement_id},
    )
    entry_id = entry_resp.json()["created_entries"][0]["id"]

    # 루트 삭제 → 자식 폴더 + entry 가 함께 사라진다.
    logged_in_client.delete(f"/favorites/folders/{root_id}")

    tree_resp = logged_in_client.get("/favorites/folders")
    folder_ids = [f["id"] for f in tree_resp.json()["folders"]]
    # 루트는 당연히, 자식 폴더도 루트로 격상 없이 사라져야 한다.
    assert root_id not in folder_ids
    assert child_id not in folder_ids

    # entry 도 CASCADE 로 제거되었으므로 재삭제 시 404.
    del_resp = logged_in_client.delete(f"/favorites/entries/{entry_id}")
    assert del_resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /favorites/entries — task 00037 announcement 단위 + 라디오
# ---------------------------------------------------------------------------


def test_create_entry_single_success(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """apply_to_all_siblings=False (기본) 로 단일 announcement 만 등록된다."""
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "싱글 폴더"})
    folder_id = folder_resp.json()["id"]

    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "announcement_id": announcement_id},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["applied_to_all_siblings"] is False
    created = data["created_entries"]
    assert len(created) == 1
    assert created[0]["folder_id"] == folder_id
    assert created[0]["announcement_id"] == announcement_id
    assert data["skipped_announcement_ids"] == []


def test_create_entry_apply_to_all_siblings(
    logged_in_client: TestClient,
) -> None:
    """apply_to_all_siblings=True 이면 동일 canonical 의 is_current 공고 전체가 등록된다.

    사용자 원문 \"별표를 누른 그 공고가 반드시 등록\" 을 확인: 요청 announcement_id 가
    결과 created_entries 또는 skipped 에 반드시 포함되어야 한다.
    """
    _, ann_ids = _make_canonical_with_announcements(
        "official:fav-siblings-001",
        source_types=("IRIS", "NTIS"),
    )
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "시블링 폴더"})
    folder_id = folder_resp.json()["id"]

    resp = logged_in_client.post(
        "/favorites/entries",
        json={
            "folder_id": folder_id,
            "announcement_id": ann_ids[0],
            "apply_to_all_siblings": True,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["applied_to_all_siblings"] is True

    created = {e["announcement_id"] for e in data["created_entries"]}
    skipped = set(data["skipped_announcement_ids"])
    # 두 announcement 가 모두 등록되어야 한다.
    assert created == set(ann_ids)
    assert skipped == set()
    # 요청한 announcement_id 가 created 경로에 반드시 들어있어야 한다.
    assert ann_ids[0] in created


def test_create_entry_duplicate_goes_to_skipped(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """이미 폴더에 있는 announcement 를 다시 추가 요청하면 skipped 리스트로 흡수된다.

    task 00037 정책 — 409 대신 200/201 + skipped 로 UI 충돌 최소화.
    """
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "중복 폴더"})
    folder_id = folder_resp.json()["id"]
    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "announcement_id": announcement_id},
    )

    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "announcement_id": announcement_id},
    )
    # 상태코드는 201 이지만 payload 로 구분한다.
    assert resp.status_code == 201
    data = resp.json()
    assert data["created_entries"] == []
    assert data["skipped_announcement_ids"] == [announcement_id]


def test_create_entry_bulk_with_partial_overlap(
    logged_in_client: TestClient,
) -> None:
    """bulk 요청 시 이미 있는 공고는 skipped 로, 나머지는 created 로 나뉘어 반환된다."""
    _, ann_ids = _make_canonical_with_announcements(
        "official:fav-siblings-002",
        source_types=("IRIS", "NTIS"),
    )
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "부분 중복 폴더"})
    folder_id = folder_resp.json()["id"]

    # 첫 번째 announcement 만 먼저 단건 등록 → bulk 호출 시 skipped 대상.
    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "announcement_id": ann_ids[0]},
    )
    resp = logged_in_client.post(
        "/favorites/entries",
        json={
            "folder_id": folder_id,
            "announcement_id": ann_ids[1],
            "apply_to_all_siblings": True,
        },
    )
    data = resp.json()
    created = {e["announcement_id"] for e in data["created_entries"]}
    skipped = set(data["skipped_announcement_ids"])
    assert created == {ann_ids[1]}
    assert skipped == {ann_ids[0]}


def test_create_entry_nonexistent_folder_returns_404(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """없는 폴더에 등록 시도 시 404."""
    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": 999999, "announcement_id": announcement_id},
    )
    assert resp.status_code == 404


def test_create_entry_nonexistent_announcement_returns_404(
    logged_in_client: TestClient,
) -> None:
    """없는 announcement_id 로 등록 시도 시 404."""
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "없는공고폴더"})
    folder_id = folder_resp.json()["id"]

    resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "announcement_id": 999999},
    )
    assert resp.status_code == 404


def test_create_entry_other_user_folder_returns_404(
    logged_in_client: TestClient,
    other_user_folder_id: int,
    announcement_id: int,
) -> None:
    """타 사용자 폴더에 등록 시도 시 404."""
    resp = logged_in_client.post(
        "/favorites/entries",
        json={
            "folder_id": other_user_folder_id,
            "announcement_id": announcement_id,
        },
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /favorites/entries/{id} — task 00037 신규 (폴더 이동)
# ---------------------------------------------------------------------------


def test_move_entry_success(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """폴더 A 의 entry 를 폴더 B 로 이동하면 folder_id 가 갱신되고 moved=True."""
    r1 = logged_in_client.post("/favorites/folders", json={"name": "소스 폴더"})
    r2 = logged_in_client.post("/favorites/folders", json={"name": "대상 폴더"})
    src_id, dst_id = r1.json()["id"], r2.json()["id"]
    entry_resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": src_id, "announcement_id": announcement_id},
    )
    entry_id = entry_resp.json()["created_entries"][0]["id"]

    resp = logged_in_client.patch(
        f"/favorites/entries/{entry_id}",
        json={"target_folder_id": dst_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["folder_id"] == dst_id
    assert body["moved"] is True


def test_move_entry_same_folder_is_noop(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """같은 폴더로의 이동 요청은 moved=False 로 응답(멱등)."""
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "동일 폴더"})
    folder_id = folder_resp.json()["id"]
    entry_resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "announcement_id": announcement_id},
    )
    entry_id = entry_resp.json()["created_entries"][0]["id"]

    resp = logged_in_client.patch(
        f"/favorites/entries/{entry_id}",
        json={"target_folder_id": folder_id},
    )
    assert resp.status_code == 200
    assert resp.json()["moved"] is False


def test_move_entry_conflict_when_target_has_same_announcement(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """대상 폴더에 이미 같은 announcement 가 있으면 UNIQUE 충돌로 409."""
    r1 = logged_in_client.post("/favorites/folders", json={"name": "충돌 소스"})
    r2 = logged_in_client.post("/favorites/folders", json={"name": "충돌 대상"})
    src_id, dst_id = r1.json()["id"], r2.json()["id"]
    src_entry = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": src_id, "announcement_id": announcement_id},
    ).json()["created_entries"][0]
    # 대상에도 동일 announcement 를 미리 등록
    logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": dst_id, "announcement_id": announcement_id},
    )

    resp = logged_in_client.patch(
        f"/favorites/entries/{src_entry['id']}",
        json={"target_folder_id": dst_id},
    )
    assert resp.status_code == 409


def test_move_entry_other_user_entry_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    """타 사용자의 entry 이동 시도 시 404."""
    resp = logged_in_client.patch(
        "/favorites/entries/999999",
        json={"target_folder_id": other_user_folder_id},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /favorites/entries/{id} — 항목 제거
# ---------------------------------------------------------------------------


def test_delete_entry_success(
    logged_in_client: TestClient, announcement_id: int
) -> None:
    """자기 entry 를 성공적으로 제거한다."""
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "항목삭제폴더"})
    folder_id = folder_resp.json()["id"]
    entry_resp = logged_in_client.post(
        "/favorites/entries",
        json={"folder_id": folder_id, "announcement_id": announcement_id},
    )
    entry_id = entry_resp.json()["created_entries"][0]["id"]

    resp = logged_in_client.delete(f"/favorites/entries/{entry_id}")
    assert resp.status_code == 200
    assert resp.json()["detail"] == "삭제되었습니다."


def test_delete_nonexistent_entry_returns_404(logged_in_client: TestClient) -> None:
    """없는 entry id 로 DELETE 시 404."""
    resp = logged_in_client.delete("/favorites/entries/999999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /favorites/folders/{id}/entries — 목록 + 페이지네이션
# ---------------------------------------------------------------------------


def test_list_folder_entries_empty(logged_in_client: TestClient) -> None:
    """빈 폴더의 items 는 빈 배열."""
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "빈항목폴더"})
    folder_id = folder_resp.json()["id"]

    resp = logged_in_client.get(f"/favorites/folders/{folder_id}/entries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []
    assert data["folder_id"] == folder_id


def test_list_folder_entries_announcement_meta(
    logged_in_client: TestClient,
) -> None:
    """항목이 있을 때 announcement_id / canonical_title 등 메타가 포함된다."""
    _, ann_ids = _make_canonical_with_announcements(
        "official:fav-list-001",
        source_types=("IRIS", "NTIS"),
        title_prefix="리스트",
    )
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "목록확인폴더"})
    folder_id = folder_resp.json()["id"]
    for aid in ann_ids:
        logged_in_client.post(
            "/favorites/entries",
            json={"folder_id": folder_id, "announcement_id": aid},
        )

    resp = logged_in_client.get(f"/favorites/folders/{folder_id}/entries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    returned_ann_ids = {item["announcement_id"] for item in data["items"]}
    assert returned_ann_ids == set(ann_ids)
    # 각 item 은 템플릿이 쓰는 키를 모두 포함해야 한다.
    for item in data["items"]:
        assert "entry_id" in item
        assert "announcement_id" in item
        assert "ann_id" in item
        assert item["ann_id"] == item["announcement_id"]
        assert "canonical_title" in item
        assert "ann_title" in item


def test_list_folder_entries_pagination(logged_in_client: TestClient) -> None:
    """page_size=1 로 2개를 1개씩 조회해 서로 다른 entry 를 얻는다."""
    _, ann_ids = _make_canonical_with_announcements(
        "official:fav-page-001",
        source_types=("IRIS", "NTIS"),
    )
    folder_resp = logged_in_client.post("/favorites/folders", json={"name": "페이지네이션폴더"})
    folder_id = folder_resp.json()["id"]
    for aid in ann_ids:
        logged_in_client.post(
            "/favorites/entries",
            json={"folder_id": folder_id, "announcement_id": aid},
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
    assert d1["items"][0]["entry_id"] != d2["items"][0]["entry_id"]


def test_list_entries_other_user_folder_returns_404(
    logged_in_client: TestClient, other_user_folder_id: int
) -> None:
    """타 사용자 폴더 항목 조회 시 404."""
    resp = logged_in_client.get(f"/favorites/folders/{other_user_folder_id}/entries")
    assert resp.status_code == 404
