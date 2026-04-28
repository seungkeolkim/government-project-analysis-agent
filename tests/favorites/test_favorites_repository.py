"""즐겨찾기 repository 헬퍼 단위 테스트 (task 00037 announcement 단위 갱신).

FavoriteEntry 의 저장 단위가 canonical_project_id → announcement_id 로 바뀐
스키마(migration c4a8d1e7b2f3) 에 맞춰 테스트를 전면 재구성했다. 기존 00036
테스트에서 canonical_project_id 를 직접 assert 하던 부분은 announcement_id 로
대체한다. 신규 헬퍼(get_current_sibling_announcement_ids / count_folder_delete_cascade)
의 단위 테스트도 함께 제공한다.
"""

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
    count_folder_delete_cascade,
    get_current_sibling_announcement_ids,
    get_favorite_announcement_id_set,
    get_favorite_entry_map,
    get_siblings_by_canonical_id_map,
    list_favorites_with_announcements,
)
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# 공통 헬퍼 — canonical + 여러 announcement 를 한 번에 만드는 유틸 (guidance 권장)
# ---------------------------------------------------------------------------


def _make_user(username: str) -> int:
    """테스트용 User 한 명을 생성해 id 를 반환한다."""
    with session_scope() as s:
        user = User(username=username, password_hash="dummy")
        s.add(user)
        s.flush()
        return user.id


def _make_canonical(key: str, title: str | None = None) -> int:
    """CanonicalProject 하나를 만들고 id 를 반환한다."""
    with session_scope() as s:
        cp = CanonicalProject(
            canonical_key=key,
            key_scheme="official",
            representative_title=title,
        )
        s.add(cp)
        s.flush()
        return cp.id


def _make_announcement(
    canonical_group_id: int | None,
    source_type: str,
    title: str,
    *,
    is_current: bool = True,
    source_id_suffix: str = "",
) -> int:
    """Announcement 하나를 만들고 id 를 반환한다.

    동일 테스트 내에서 여러 announcement 를 만들 때 source_announcement_id 가
    중복되지 않도록 source_id_suffix 로 구분할 수 있다.
    """
    with session_scope() as s:
        ann = Announcement(
            source_announcement_id=f"test_{title[:6]}_{source_id_suffix}",
            source_type=source_type,
            title=title,
            status=AnnouncementStatus.RECEIVING,
            is_current=is_current,
            canonical_group_id=canonical_group_id,
        )
        s.add(ann)
        s.flush()
        return ann.id


def _make_canonical_with_current_announcements(
    key: str,
    *,
    source_types: tuple[str, ...] = ("IRIS",),
    title_prefix: str = "공고",
) -> tuple[int, list[int]]:
    """canonical 1개 + 각 source_type 당 is_current=True announcement 1개씩 생성.

    guidance 권장 — 동일 canonical 에 여러 공고를 빠르게 만들어 시블링 로직을
    테스트하기 쉽게 한다.

    Returns:
        (canonical_id, [announcement_id, ...]) — announcement_id 는 입력
        source_types 순서와 동일하게 정렬되어 반환된다.
    """
    canonical_id = _make_canonical(key, title=f"{title_prefix} 대표")
    announcement_ids: list[int] = []
    for index, source_type in enumerate(source_types):
        ann_id = _make_announcement(
            canonical_id,
            source_type=source_type,
            title=f"{title_prefix} {source_type}",
            source_id_suffix=f"{index}",
        )
        announcement_ids.append(ann_id)
    return canonical_id, announcement_ids


def _make_folder(user_id: int, name: str, parent_id: int | None = None) -> int:
    """FavoriteFolder 하나를 만들고 id 를 반환한다."""
    with session_scope() as s:
        depth = 0 if parent_id is None else 1
        folder = FavoriteFolder(
            user_id=user_id, name=name, parent_id=parent_id, depth=depth
        )
        s.add(folder)
        s.flush()
        return folder.id


def _add_entry(folder_id: int, announcement_id: int) -> int:
    """FavoriteEntry 하나를 만들고 id 를 반환한다 (announcement 단위)."""
    with session_scope() as s:
        entry = FavoriteEntry(
            folder_id=folder_id, announcement_id=announcement_id
        )
        s.add(entry)
        s.flush()
        return entry.id


# ---------------------------------------------------------------------------
# FavoriteEntry ORM — 기본 CRUD (announcement 단위)
# ---------------------------------------------------------------------------


def test_favorite_entry_insert_and_read(test_engine: Engine):
    """announcement_id 로 저장된 entry 를 다시 읽을 수 있다."""
    uid = _make_user("fe_user_1")
    _, ann_ids = _make_canonical_with_current_announcements("official:test-fe-001")
    fid = _make_folder(uid, "내 폴더")
    eid = _add_entry(fid, ann_ids[0])

    with session_scope() as s:
        entry = s.get(FavoriteEntry, eid)
    assert entry is not None
    assert entry.folder_id == fid
    assert entry.announcement_id == ann_ids[0]


def test_favorite_entry_unique_constraint(test_engine: Engine):
    """동일 (folder_id, announcement_id) 중복 삽입 시 IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    uid = _make_user("fe_user_2")
    _, ann_ids = _make_canonical_with_current_announcements("official:test-fe-002")
    fid = _make_folder(uid, "폴더2")
    _add_entry(fid, ann_ids[0])

    with pytest.raises(IntegrityError):
        _add_entry(fid, ann_ids[0])


def test_favorite_entry_cascade_on_folder_delete(test_engine: Engine):
    """폴더 삭제 시 FavoriteEntry 도 CASCADE 삭제된다(ORM cascade)."""
    uid = _make_user("fe_user_3")
    _, ann_ids = _make_canonical_with_current_announcements("official:test-fe-003")
    fid = _make_folder(uid, "삭제폴더")
    eid = _add_entry(fid, ann_ids[0])

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
# task 00037 — 자식 폴더 cascade 삭제 (SET NULL 격상 폐기)
# ---------------------------------------------------------------------------


def test_favorite_folder_cascade_deletes_children(test_engine: Engine):
    """부모 폴더 삭제 시 자식 폴더가 루트로 격상되지 않고 함께 삭제된다.

    migration c4a8d1e7b2f3 에서 parent_id FK ondelete 를 SET NULL → CASCADE 로
    변경했고, ORM children relationship 에 cascade=\"all, delete\" 를 추가해
    SQLite PRAGMA 와 무관하게 session 경로도 보장한다. 사용자 원문 #2 취지의
    \"격상 없음\" 을 테스트.
    """
    uid = _make_user("fe_cascade_parent")
    root_id = _make_folder(uid, "부모")
    child_id = _make_folder(uid, "자식", parent_id=root_id)

    with session_scope() as s:
        root = s.get(FavoriteFolder, root_id)
        s.delete(root)

    with session_scope() as s:
        assert s.get(FavoriteFolder, root_id) is None
        assert s.get(FavoriteFolder, child_id) is None


# ---------------------------------------------------------------------------
# get_favorite_announcement_id_set
# ---------------------------------------------------------------------------


def test_get_favorite_announcement_id_set_basic(test_engine: Engine):
    """즐겨찾기에 담긴 announcement_id 만 set 으로 반환된다."""
    uid = _make_user("fav_set_user_1")
    _, ann_ids = _make_canonical_with_current_announcements(
        "official:test-fset-001",
        source_types=("IRIS", "NTIS"),
    )
    extra_canonical_id = _make_canonical("official:test-fset-extra")
    extra_ann_id = _make_announcement(
        extra_canonical_id,
        source_type="IRIS",
        title="외부 공고",
        source_id_suffix="extra",
    )
    fid = _make_folder(uid, "기본폴더")
    _add_entry(fid, ann_ids[0])
    _add_entry(fid, ann_ids[1])

    with session_scope() as s:
        result = get_favorite_announcement_id_set(
            s,
            user_id=uid,
            announcement_ids=[ann_ids[0], ann_ids[1], extra_ann_id],
        )

    assert ann_ids[0] in result
    assert ann_ids[1] in result
    assert extra_ann_id not in result


def test_get_favorite_announcement_id_set_empty_input(test_engine: Engine):
    """announcement_ids 가 비어 있으면 쿼리 없이 빈 set 을 반환한다."""
    uid = _make_user("fav_set_user_2")
    with session_scope() as s:
        result = get_favorite_announcement_id_set(
            s, user_id=uid, announcement_ids=[]
        )
    assert result == set()


def test_get_favorite_announcement_id_set_other_user_invisible(test_engine: Engine):
    """다른 사용자의 즐겨찾기는 반환에 포함되지 않는다."""
    uid_a = _make_user("fav_set_user_a")
    uid_b = _make_user("fav_set_user_b")
    _, ann_ids = _make_canonical_with_current_announcements("official:test-fset-cross")
    fid_a = _make_folder(uid_a, "A 폴더")
    _add_entry(fid_a, ann_ids[0])

    with session_scope() as s:
        result = get_favorite_announcement_id_set(
            s, user_id=uid_b, announcement_ids=[ann_ids[0]]
        )
    assert result == set()


def test_get_favorite_announcement_id_set_across_folders(test_engine: Engine):
    """같은 announcement 이 여러 폴더에 있어도 중복 없이 1건만 반환한다."""
    uid = _make_user("fav_set_user_multi")
    _, ann_ids = _make_canonical_with_current_announcements("official:test-fset-multi")
    fid1 = _make_folder(uid, "폴더A")
    fid2 = _make_folder(uid, "폴더B")
    _add_entry(fid1, ann_ids[0])
    _add_entry(fid2, ann_ids[0])

    with session_scope() as s:
        result = get_favorite_announcement_id_set(
            s, user_id=uid, announcement_ids=[ann_ids[0]]
        )
    assert result == {ann_ids[0]}


# ---------------------------------------------------------------------------
# get_favorite_entry_map
# ---------------------------------------------------------------------------


def test_get_favorite_entry_map_min_entry_id(test_engine: Engine):
    """같은 announcement_id 가 여러 폴더에 있을 때 MIN(entry_id) 가 대표다."""
    uid = _make_user("fav_map_multi")
    _, ann_ids = _make_canonical_with_current_announcements("official:test-fmap-001")
    fid1 = _make_folder(uid, "폴더1")
    fid2 = _make_folder(uid, "폴더2")
    e1 = _add_entry(fid1, ann_ids[0])
    e2 = _add_entry(fid2, ann_ids[0])

    with session_scope() as s:
        result = get_favorite_entry_map(
            s, user_id=uid, announcement_ids=[ann_ids[0]]
        )
    assert result == {ann_ids[0]: min(e1, e2)}


# ---------------------------------------------------------------------------
# get_current_sibling_announcement_ids (신규 — task 00037)
# ---------------------------------------------------------------------------


def test_get_current_sibling_announcement_ids_multiple(test_engine: Engine):
    """동일 canonical_group_id 의 is_current=True 공고를 모두 반환한다."""
    _, ann_ids = _make_canonical_with_current_announcements(
        "official:test-sib-multi",
        source_types=("IRIS", "NTIS"),
    )

    with session_scope() as s:
        result = get_current_sibling_announcement_ids(
            s, announcement_id=ann_ids[0]
        )
    assert set(result) == set(ann_ids)
    # 요청한 announcement_id 는 반드시 포함되어야 한다(사용자 원문 보증).
    assert ann_ids[0] in result


def test_get_current_sibling_announcement_ids_skips_noncurrent(test_engine: Engine):
    """is_current=False 인 공고는 sibling 목록에서 제외된다."""
    canonical_id, ann_ids = _make_canonical_with_current_announcements(
        "official:test-sib-current-only",
        source_types=("IRIS",),
    )
    # 동일 canonical 에 is_current=False row 하나 추가(이력).
    stale_ann_id = _make_announcement(
        canonical_id,
        source_type="IRIS",
        title="이력 공고",
        is_current=False,
        source_id_suffix="stale",
    )

    with session_scope() as s:
        result = get_current_sibling_announcement_ids(
            s, announcement_id=ann_ids[0]
        )
    assert stale_ann_id not in result
    assert ann_ids[0] in result


def test_get_current_sibling_announcement_ids_canonical_none(test_engine: Engine):
    """canonical 매칭이 없는(canonical_group_id=NULL) 공고는 자기 자신만 반환."""
    orphan_ann_id = _make_announcement(
        None,
        source_type="IRIS",
        title="고아 공고",
        source_id_suffix="orphan",
    )

    with session_scope() as s:
        result = get_current_sibling_announcement_ids(
            s, announcement_id=orphan_ann_id
        )
    assert result == [orphan_ann_id]


def test_get_current_sibling_announcement_ids_unknown(test_engine: Engine):
    """존재하지 않는 announcement_id 는 그 id 만 담아 반환한다(방어 로직)."""
    with session_scope() as s:
        result = get_current_sibling_announcement_ids(
            s, announcement_id=9_999_999
        )
    assert result == [9_999_999]


# ---------------------------------------------------------------------------
# count_folder_delete_cascade (신규 — task 00037)
# ---------------------------------------------------------------------------


def test_count_folder_delete_cascade_with_children_and_entries(test_engine: Engine):
    """루트 폴더 + 자식 폴더 + 각 폴더에 entry 가 있을 때 정확히 집계한다."""
    uid = _make_user("fav_cnt_cascade_1")
    _, ann_ids = _make_canonical_with_current_announcements(
        "official:test-cnt-001",
        source_types=("IRIS", "NTIS"),
    )
    root_id = _make_folder(uid, "루트")
    child_a = _make_folder(uid, "자식A", parent_id=root_id)
    child_b = _make_folder(uid, "자식B", parent_id=root_id)
    _add_entry(root_id, ann_ids[0])
    _add_entry(child_a, ann_ids[0])
    _add_entry(child_b, ann_ids[1])

    with session_scope() as s:
        result = count_folder_delete_cascade(s, folder_id=root_id)
    assert result == {"subfolder_count": 2, "entry_count": 3}


def test_count_folder_delete_cascade_leaf_only(test_engine: Engine):
    """자식 없는 루트 폴더는 subfolder_count=0, entry_count 는 자기 entry 수."""
    uid = _make_user("fav_cnt_cascade_2")
    _, ann_ids = _make_canonical_with_current_announcements("official:test-cnt-002")
    root_id = _make_folder(uid, "혼자 루트")
    _add_entry(root_id, ann_ids[0])

    with session_scope() as s:
        result = count_folder_delete_cascade(s, folder_id=root_id)
    assert result == {"subfolder_count": 0, "entry_count": 1}


def test_count_folder_delete_cascade_empty(test_engine: Engine):
    """완전히 빈 폴더는 두 카운트 모두 0."""
    uid = _make_user("fav_cnt_cascade_3")
    root_id = _make_folder(uid, "빈 폴더")

    with session_scope() as s:
        result = count_folder_delete_cascade(s, folder_id=root_id)
    assert result == {"subfolder_count": 0, "entry_count": 0}


# ---------------------------------------------------------------------------
# list_favorites_with_announcements — announcement 단위 페이지네이션
# ---------------------------------------------------------------------------


def test_list_favorites_with_announcements_returns_ann_meta(test_engine: Engine):
    """반환 item 이 announcement 메타(ann_id / ann_title / canonical_title 등) 를 담는다."""
    uid = _make_user("fav_list_user_1")
    _, ann_ids = _make_canonical_with_current_announcements(
        "official:test-list-001",
        title_prefix="List",
    )
    fid = _make_folder(uid, "리스트폴더")
    _add_entry(fid, ann_ids[0])

    with session_scope() as s:
        items, total = list_favorites_with_announcements(s, folder_id=fid)
    assert total == 1
    item = items[0]
    assert item["announcement_id"] == ann_ids[0]
    assert item["ann_id"] == ann_ids[0]
    # canonical 대표 제목 또는 announcement 자체 제목 중 하나가 채워져야 한다.
    assert item["canonical_title"] is not None
    assert item["ann_title"] is not None


# ---------------------------------------------------------------------------
# get_siblings_by_canonical_id_map (기존 — 00036 에서 작성, 변경 없음 재검증)
# ---------------------------------------------------------------------------


def test_get_siblings_by_canonical_id_map_basic(test_engine: Engine):
    """canonical 당 is_current=True announcement 리스트를 dict 로 반환한다."""
    canonical_id, ann_ids = _make_canonical_with_current_announcements(
        "official:test-sibmap-001",
        source_types=("IRIS", "NTIS"),
    )

    with session_scope() as s:
        result = get_siblings_by_canonical_id_map(s, [canonical_id])

    assert canonical_id in result
    returned_ann_ids = {a["id"] for a in result[canonical_id]}
    assert returned_ann_ids == set(ann_ids)


def test_get_siblings_by_canonical_id_map_empty_input(test_engine: Engine):
    """canonical_ids 가 비면 쿼리 없이 빈 dict 반환."""
    with session_scope() as s:
        result = get_siblings_by_canonical_id_map(s, [])
    assert result == {}


def test_get_siblings_by_canonical_id_map_no_match(test_engine: Engine):
    """존재하지 않는 canonical_id 는 dict 에 포함되지 않는다."""
    with session_scope() as s:
        result = get_siblings_by_canonical_id_map(s, [999999])
    assert result == {}
