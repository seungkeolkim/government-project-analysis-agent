"""즐겨찾기 repository 헬퍼 단위 테스트 (00036-3)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    FavoriteEntry,
    FavoriteFolder,
    User,
)
from app.db.repository import (
    get_favorite_canonical_id_set,
    get_siblings_by_canonical_id_map,
)
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_user(username: str) -> int:
    with session_scope() as s:
        user = User(username=username, password_hash="dummy")
        s.add(user)
        s.flush()
        return user.id


def _make_canonical(key: str) -> int:
    with session_scope() as s:
        cp = CanonicalProject(canonical_key=key, key_scheme="official")
        s.add(cp)
        s.flush()
        return cp.id


def _make_folder(user_id: int, name: str, parent_id: int | None = None) -> int:
    with session_scope() as s:
        depth = 0 if parent_id is None else 1
        folder = FavoriteFolder(
            user_id=user_id, name=name, parent_id=parent_id, depth=depth
        )
        s.add(folder)
        s.flush()
        return folder.id


def _add_entry(folder_id: int, canonical_project_id: int) -> int:
    with session_scope() as s:
        entry = FavoriteEntry(
            folder_id=folder_id, canonical_project_id=canonical_project_id
        )
        s.add(entry)
        s.flush()
        return entry.id


def _make_announcement(canonical_group_id: int, source_type: str, title: str) -> int:
    with session_scope() as s:
        ann = Announcement(
            source_announcement_id=f"test_{title[:8]}",
            source_type=source_type,
            title=title,
            status=AnnouncementStatus.RECEIVING,
            is_current=True,
            canonical_group_id=canonical_group_id,
        )
        s.add(ann)
        s.flush()
        return ann.id


# ---------------------------------------------------------------------------
# FavoriteEntry ORM — 기본 CRUD
# ---------------------------------------------------------------------------


def test_favorite_entry_insert_and_read(test_engine: Engine):
    uid = _make_user("fe_user_1")
    cid = _make_canonical("official:test-fe-001")
    fid = _make_folder(uid, "내 폴더")
    eid = _add_entry(fid, cid)

    with session_scope() as s:
        entry = s.get(FavoriteEntry, eid)
    assert entry is not None
    assert entry.folder_id == fid
    assert entry.canonical_project_id == cid


def test_favorite_entry_unique_constraint(test_engine: Engine):
    """동일 (folder_id, canonical_project_id) 중복 삽입 시 IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    uid = _make_user("fe_user_2")
    cid = _make_canonical("official:test-fe-002")
    fid = _make_folder(uid, "폴더2")
    _add_entry(fid, cid)

    with pytest.raises(IntegrityError):
        _add_entry(fid, cid)


def test_favorite_entry_cascade_on_folder_delete(test_engine: Engine):
    """폴더 삭제 시 FavoriteEntry 도 CASCADE 삭제된다."""
    uid = _make_user("fe_user_3")
    cid = _make_canonical("official:test-fe-003")
    fid = _make_folder(uid, "삭제폴더")
    eid = _add_entry(fid, cid)

    with session_scope() as s:
        folder = s.get(FavoriteFolder, fid)
        s.delete(folder)

    with session_scope() as s:
        assert s.get(FavoriteEntry, eid) is None


def test_favorite_folder_depth_constraint_root(test_engine: Engine):
    """루트 폴더(parent_id=None)는 depth=0 으로 생성 가능."""
    uid = _make_user("fe_user_depth_1")
    fid = _make_folder(uid, "루트")
    with session_scope() as s:
        f = s.get(FavoriteFolder, fid)
    assert f is not None
    assert f.depth == 0
    assert f.parent_id is None


def test_favorite_folder_depth_constraint_child(test_engine: Engine):
    """루트의 자식(depth=1)은 정상 생성 가능."""
    uid = _make_user("fe_user_depth_2")
    root_id = _make_folder(uid, "루트")
    child_id = _make_folder(uid, "자식", parent_id=root_id)
    with session_scope() as s:
        child = s.get(FavoriteFolder, child_id)
    assert child is not None
    assert child.depth == 1
    assert child.parent_id == root_id


def test_favorite_folder_depth_constraint_grandchild_raises(test_engine: Engine):
    """depth=2(손자) 폴더는 ValueError 를 유발한다."""
    from sqlalchemy.exc import StatementError

    uid = _make_user("fe_user_depth_3")
    root_id = _make_folder(uid, "루트")
    child_id = _make_folder(uid, "자식", parent_id=root_id)

    with pytest.raises((ValueError, StatementError)):
        _make_folder(uid, "손자", parent_id=child_id)


# ---------------------------------------------------------------------------
# get_favorite_canonical_id_set
# ---------------------------------------------------------------------------


def test_get_favorite_canonical_id_set_basic(test_engine: Engine):
    uid = _make_user("fav_set_user_1")
    cid1 = _make_canonical("official:test-fset-001")
    cid2 = _make_canonical("official:test-fset-002")
    cid3 = _make_canonical("official:test-fset-003")
    fid = _make_folder(uid, "기본폴더")
    _add_entry(fid, cid1)
    _add_entry(fid, cid2)

    with session_scope() as s:
        result = get_favorite_canonical_id_set(
            s, user_id=uid, canonical_ids=[cid1, cid2, cid3]
        )

    assert cid1 in result
    assert cid2 in result
    assert cid3 not in result


def test_get_favorite_canonical_id_set_empty_input(test_engine: Engine):
    uid = _make_user("fav_set_user_2")
    with session_scope() as s:
        result = get_favorite_canonical_id_set(s, user_id=uid, canonical_ids=[])
    assert result == set()


def test_get_favorite_canonical_id_set_other_user_invisible(test_engine: Engine):
    """다른 사용자의 즐겨찾기는 반환에 포함되지 않는다."""
    uid_a = _make_user("fav_set_user_a")
    uid_b = _make_user("fav_set_user_b")
    cid = _make_canonical("official:test-fset-cross")
    fid_a = _make_folder(uid_a, "A 폴더")
    _add_entry(fid_a, cid)

    with session_scope() as s:
        result = get_favorite_canonical_id_set(
            s, user_id=uid_b, canonical_ids=[cid]
        )
    assert cid not in result


def test_get_favorite_canonical_id_set_across_folders(test_engine: Engine):
    """같은 canonical 이 여러 폴더에 있어도 중복 없이 1건만 반환한다."""
    uid = _make_user("fav_set_user_multi")
    cid = _make_canonical("official:test-fset-multi")
    fid1 = _make_folder(uid, "폴더A")
    fid2 = _make_folder(uid, "폴더B")
    _add_entry(fid1, cid)
    _add_entry(fid2, cid)

    with session_scope() as s:
        result = get_favorite_canonical_id_set(
            s, user_id=uid, canonical_ids=[cid]
        )
    assert result == {cid}


# ---------------------------------------------------------------------------
# get_siblings_by_canonical_id_map
# ---------------------------------------------------------------------------


def test_get_siblings_by_canonical_id_map_basic(test_engine: Engine):
    cid = _make_canonical("official:test-sib-001")
    aid1 = _make_announcement(cid, "IRIS", "IRIS 공고 제목")
    aid2 = _make_announcement(cid, "NTIS", "NTIS 공고 제목")

    with session_scope() as s:
        result = get_siblings_by_canonical_id_map(s, [cid])

    assert cid in result
    ann_ids = {a["id"] for a in result[cid]}
    assert aid1 in ann_ids
    assert aid2 in ann_ids


def test_get_siblings_by_canonical_id_map_empty_input(test_engine: Engine):
    with session_scope() as s:
        result = get_siblings_by_canonical_id_map(s, [])
    assert result == {}


def test_get_siblings_by_canonical_id_map_no_match(test_engine: Engine):
    with session_scope() as s:
        result = get_siblings_by_canonical_id_map(s, [999999])
    assert result == {}


def test_get_siblings_by_canonical_id_map_multiple_canonicals(test_engine: Engine):
    """여러 canonical_id 를 한 번에 조회한다."""
    cid_a = _make_canonical("official:test-sib-multi-a")
    cid_b = _make_canonical("official:test-sib-multi-b")
    aid_a = _make_announcement(cid_a, "IRIS", "A 공고")
    aid_b1 = _make_announcement(cid_b, "IRIS", "B IRIS 공고")
    aid_b2 = _make_announcement(cid_b, "NTIS", "B NTIS 공고")

    with session_scope() as s:
        result = get_siblings_by_canonical_id_map(s, [cid_a, cid_b])

    assert cid_a in result
    assert cid_b in result
    assert {a["id"] for a in result[cid_a]} == {aid_a}
    assert {a["id"] for a in result[cid_b]} == {aid_b1, aid_b2}


def test_get_siblings_by_canonical_id_map_returns_dict_keys(test_engine: Engine):
    """반환 dict 의 항목이 올바른 키를 포함한다."""
    cid = _make_canonical("official:test-sib-keys")
    _make_announcement(cid, "IRIS", "키 확인용 공고")

    with session_scope() as s:
        result = get_siblings_by_canonical_id_map(s, [cid])

    assert cid in result
    item = result[cid][0]
    assert "id" in item
    assert "title" in item
    assert "source_type" in item
    assert "deadline_at" in item
    assert "status" in item
