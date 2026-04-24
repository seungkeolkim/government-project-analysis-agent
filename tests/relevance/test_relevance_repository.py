"""관련성 판정 repository 헬퍼 단위 테스트."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from app.db.models import CanonicalProject, User
from app.db.repository import (
    RELEVANCE_VERDICT_RELATED,
    RELEVANCE_VERDICT_UNRELATED,
    delete_relevance_judgment,
    get_canonical_project_by_id,
    get_relevance_by_canonical_id_map,
    get_relevance_history,
    get_relevance_history_by_canonical_id_map,
    get_relevance_judgment,
    set_relevance_judgment,
)
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_user(session_scope_ctx, username: str) -> int:
    with session_scope_ctx() as s:
        user = User(username=username, password_hash="dummy")
        s.add(user)
        s.flush()
        return user.id


def _make_canonical(session_scope_ctx, key: str) -> int:
    with session_scope_ctx() as s:
        cp = CanonicalProject(
            canonical_key=key,
            key_scheme="official",
        )
        s.add(cp)
        s.flush()
        return cp.id


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def setup(test_engine: Engine):
    """user_id, canonical_id 쌍 반환."""
    user_id = _make_user(session_scope, "rj_user")
    canonical_id = _make_canonical(session_scope, "official:test-rj-001")
    return {"user_id": user_id, "canonical_id": canonical_id}


# ---------------------------------------------------------------------------
# get_canonical_project_by_id
# ---------------------------------------------------------------------------


def test_get_canonical_project_by_id_found(test_engine: Engine):
    cid = _make_canonical(session_scope, "official:test-cpid-001")
    with session_scope() as s:
        cp = get_canonical_project_by_id(s, cid)
    assert cp is not None
    assert cp.id == cid


def test_get_canonical_project_by_id_not_found(test_engine: Engine):
    with session_scope() as s:
        assert get_canonical_project_by_id(s, 999999) is None


# ---------------------------------------------------------------------------
# set_relevance_judgment — 신규
# ---------------------------------------------------------------------------


def test_set_relevance_judgment_new(test_engine: Engine, setup):
    uid = setup["user_id"]
    cid = setup["canonical_id"]

    with session_scope() as s:
        rj = set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            reason="테스트 이유",
        )
        assert rj.verdict == RELEVANCE_VERDICT_RELATED
        assert rj.reason == "테스트 이유"
        assert rj.canonical_project_id == cid
        assert rj.user_id == uid


def test_set_relevance_judgment_invalid_verdict(test_engine: Engine, setup):
    uid = setup["user_id"]
    cid = setup["canonical_id"]

    with session_scope() as s:
        with pytest.raises(ValueError, match="verdict"):
            set_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=uid,
                verdict="잘못된값",
            )


# ---------------------------------------------------------------------------
# set_relevance_judgment — 덮어쓰기 (history 이관)
# ---------------------------------------------------------------------------


def test_set_relevance_judgment_overwrite_creates_history(test_engine: Engine, setup):
    uid = setup["user_id"]
    cid = setup["canonical_id"]
    t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            reason="초기 판정",
            now=t1,
        )

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            reason="변경 판정",
            now=t2,
        )

    with session_scope() as s:
        rj = get_relevance_judgment(s, canonical_project_id=cid, user_id=uid)
        assert rj is not None
        assert rj.verdict == RELEVANCE_VERDICT_UNRELATED

        hist_list = get_relevance_history(s, canonical_project_id=cid)
        assert len(hist_list) == 1
        hw = hist_list[0]
        assert hw.history.verdict == RELEVANCE_VERDICT_RELATED
        assert hw.history.archive_reason == "user_overwrite"
        assert hw.username == "rj_user"


# ---------------------------------------------------------------------------
# delete_relevance_judgment
# ---------------------------------------------------------------------------


def test_delete_relevance_judgment(test_engine: Engine, setup):
    uid = setup["user_id"]
    cid = setup["canonical_id"]

    with session_scope() as s:
        set_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, verdict=RELEVANCE_VERDICT_RELATED
        )

    with session_scope() as s:
        result = delete_relevance_judgment(s, canonical_project_id=cid, user_id=uid)
        assert result is True

    with session_scope() as s:
        assert get_relevance_judgment(s, canonical_project_id=cid, user_id=uid) is None
        hist_list = get_relevance_history(s, canonical_project_id=cid)
        assert len(hist_list) == 1
        assert hist_list[0].history.archive_reason == "user_overwrite"


def test_delete_relevance_judgment_not_found(test_engine: Engine, setup):
    uid = setup["user_id"]
    cid = setup["canonical_id"]

    with session_scope() as s:
        result = delete_relevance_judgment(s, canonical_project_id=cid, user_id=uid)
        assert result is False


# ---------------------------------------------------------------------------
# bulk queries
# ---------------------------------------------------------------------------


def test_get_relevance_by_canonical_id_map(test_engine: Engine):
    uid1 = _make_user(session_scope, "bulk_user1")
    uid2 = _make_user(session_scope, "bulk_user2")
    cid1 = _make_canonical(session_scope, "official:bulk-test-001")
    cid2 = _make_canonical(session_scope, "official:bulk-test-002")

    with session_scope() as s:
        set_relevance_judgment(
            s, canonical_project_id=cid1, user_id=uid1, verdict=RELEVANCE_VERDICT_RELATED
        )
        set_relevance_judgment(
            s, canonical_project_id=cid1, user_id=uid2, verdict=RELEVANCE_VERDICT_UNRELATED
        )
        set_relevance_judgment(
            s, canonical_project_id=cid2, user_id=uid1, verdict=RELEVANCE_VERDICT_RELATED
        )

    with session_scope() as s:
        result = get_relevance_by_canonical_id_map(s, [cid1, cid2])
        assert cid1 in result
        assert cid2 in result
        assert len(result[cid1]) == 2
        assert len(result[cid2]) == 1
        assert all(rj.user is not None for rj in result[cid1])


def test_get_relevance_by_canonical_id_map_empty(test_engine: Engine):
    with session_scope() as s:
        result = get_relevance_by_canonical_id_map(s, [])
        assert result == {}


def test_get_relevance_history_by_canonical_id_map(test_engine: Engine):
    uid = _make_user(session_scope, "hist_bulk_user")
    cid1 = _make_canonical(session_scope, "official:histbulk-001")
    cid2 = _make_canonical(session_scope, "official:histbulk-002")

    with session_scope() as s:
        set_relevance_judgment(
            s, canonical_project_id=cid1, user_id=uid, verdict=RELEVANCE_VERDICT_RELATED
        )
    with session_scope() as s:
        set_relevance_judgment(
            s, canonical_project_id=cid1, user_id=uid, verdict=RELEVANCE_VERDICT_UNRELATED
        )
    with session_scope() as s:
        set_relevance_judgment(
            s, canonical_project_id=cid2, user_id=uid, verdict=RELEVANCE_VERDICT_RELATED
        )
    with session_scope() as s:
        delete_relevance_judgment(s, canonical_project_id=cid2, user_id=uid)

    with session_scope() as s:
        result = get_relevance_history_by_canonical_id_map(s, [cid1, cid2])
        assert cid1 in result
        assert len(result[cid1]) == 1
        assert result[cid1][0].username == "hist_bulk_user"
        assert cid2 in result
        assert len(result[cid2]) == 1
