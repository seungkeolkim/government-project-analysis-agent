"""관련성 판정 repository 헬퍼 단위 테스트."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, event, select

from app.db.models import (
    CanonicalProject,
    Organization,
    RelevanceJudgment,
    RelevanceJudgmentHistory,
    User,
)
from app.db.repository import (
    RELEVANCE_SUMMARY_EMPTY,
    RELEVANCE_VERDICT_RELATED,
    RELEVANCE_VERDICT_UNRELATED,
    delete_relevance_judgment,
    get_canonical_project_by_id,
    get_relevance_by_canonical_id_map,
    get_relevance_history,
    get_relevance_history_by_canonical_id_map,
    get_relevance_judgment,
    get_relevance_summary_by_canonical_id_map,
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


def _make_organization(session_scope_ctx, name: str) -> int:
    """테스트용 조직을 생성하고 PK 를 반환한다."""
    with session_scope_ctx() as s:
        org = Organization(name=name)
        s.add(org)
        s.flush()
        return org.id


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def setup(test_engine: Engine):
    """user_id, canonical_id, org_id 쌍 반환."""
    user_id = _make_user(session_scope, "rj_user")
    canonical_id = _make_canonical(session_scope, "official:test-rj-001")
    org_id = _make_organization(session_scope, "테스트-조직")
    return {"user_id": user_id, "canonical_id": canonical_id, "org_id": org_id}


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
    org_id = setup["org_id"]

    with session_scope() as s:
        rj = set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
            reason="테스트 이유",
        )
        assert rj.verdict == RELEVANCE_VERDICT_RELATED
        assert rj.reason == "테스트 이유"
        assert rj.canonical_project_id == cid
        assert rj.user_id == uid
        assert rj.organization_id == org_id


def test_set_relevance_judgment_invalid_verdict(test_engine: Engine, setup):
    uid = setup["user_id"]
    cid = setup["canonical_id"]
    org_id = setup["org_id"]

    with session_scope() as s:
        with pytest.raises(ValueError, match="verdict"):
            set_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=uid,
                verdict="잘못된값",
                organization_id=org_id,
            )


# ---------------------------------------------------------------------------
# set_relevance_judgment — 덮어쓰기 (history 이관)
# ---------------------------------------------------------------------------


def test_set_relevance_judgment_overwrite_creates_history(test_engine: Engine, setup):
    uid = setup["user_id"]
    cid = setup["canonical_id"]
    org_id = setup["org_id"]
    t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
            reason="초기 판정",
            now=t1,
        )

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_id,
            reason="변경 판정",
            now=t2,
        )

    with session_scope() as s:
        rj = get_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=org_id
        )
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
    org_id = setup["org_id"]

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
        )

    with session_scope() as s:
        result = delete_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=org_id
        )
        assert result is True

    with session_scope() as s:
        assert (
            get_relevance_judgment(
                s, canonical_project_id=cid, user_id=uid, organization_id=org_id
            )
            is None
        )
        hist_list = get_relevance_history(s, canonical_project_id=cid)
        assert len(hist_list) == 1
        assert hist_list[0].history.archive_reason == "user_overwrite"


def test_delete_relevance_judgment_not_found(test_engine: Engine, setup):
    uid = setup["user_id"]
    cid = setup["canonical_id"]
    org_id = setup["org_id"]

    with session_scope() as s:
        result = delete_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=org_id
        )
        assert result is False


# ---------------------------------------------------------------------------
# bulk queries
# ---------------------------------------------------------------------------


def test_get_relevance_by_canonical_id_map(test_engine: Engine):
    uid1 = _make_user(session_scope, "bulk_user1")
    uid2 = _make_user(session_scope, "bulk_user2")
    cid1 = _make_canonical(session_scope, "official:bulk-test-001")
    cid2 = _make_canonical(session_scope, "official:bulk-test-002")
    org_id = _make_organization(session_scope, "bulk-조직")

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid1,
            user_id=uid1,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
        )
        set_relevance_judgment(
            s,
            canonical_project_id=cid1,
            user_id=uid2,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_id,
        )
        set_relevance_judgment(
            s,
            canonical_project_id=cid2,
            user_id=uid1,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
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
    org_id = _make_organization(session_scope, "histbulk-조직")

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid1,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
        )
    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid1,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_id,
        )
    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid2,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
        )
    with session_scope() as s:
        delete_relevance_judgment(
            s, canonical_project_id=cid2, user_id=uid, organization_id=org_id
        )

    with session_scope() as s:
        result = get_relevance_history_by_canonical_id_map(s, [cid1, cid2])
        assert cid1 in result
        assert len(result[cid1]) == 1
        assert result[cid1][0].username == "hist_bulk_user"
        assert cid2 in result
        assert len(result[cid2]) == 1


# ---------------------------------------------------------------------------
# task 00085 — organization_id 관련 헬퍼 동작 검증
# ---------------------------------------------------------------------------


def test_set_relevance_judgment_overwrite_preserves_organization_id_in_history(
    test_engine: Engine, setup
):
    """조직 row 변경 시 History 이관 row 에도 organization_id 가 유지돼야 한다."""
    uid = setup["user_id"]
    cid = setup["canonical_id"]
    org_id = _make_organization(session_scope, "관련성-org-history")
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, tzinfo=UTC)

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
            reason="조직 초기",
            now=t1,
        )

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_id,
            reason="조직 변경",
            now=t2,
        )

    # 활성 row 는 조직 슬롯에서 새 verdict 로 교체됨.
    with session_scope() as s:
        current_org_row = get_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=org_id
        )
        assert current_org_row is not None
        assert current_org_row.verdict == RELEVANCE_VERDICT_UNRELATED
        assert current_org_row.organization_id == org_id

        # History 에는 이관된 1 건이 있고, organization_id 가 보존되어야 한다.
        rows = list(
            s.execute(
                select(RelevanceJudgmentHistory).where(
                    RelevanceJudgmentHistory.canonical_project_id == cid,
                    RelevanceJudgmentHistory.user_id == uid,
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert rows[0].organization_id == org_id
        assert rows[0].verdict == RELEVANCE_VERDICT_RELATED
        assert rows[0].archive_reason == "user_overwrite"


def test_delete_relevance_judgment_multiple_org_rows_isolated(
    test_engine: Engine, setup
):
    """delete 는 지정된 (canonical, user, org) 트리플만 지우고 다른 트리플 row 는 보존해야 한다."""
    uid = setup["user_id"]
    cid = setup["canonical_id"]
    org_a_id = _make_organization(session_scope, "관련성-org-A-delete")
    org_b_id = _make_organization(session_scope, "관련성-org-B-delete")

    # 조직 A + 조직 B row 동시 생성
    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_a_id,
        )
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_b_id,
        )

    # 조직 A row 만 삭제
    with session_scope() as s:
        deleted = delete_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=org_a_id
        )
        assert deleted is True

    # 조직 A row 는 사라지고 조직 B row 는 살아 있어야 한다.
    with session_scope() as s:
        assert (
            get_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=uid,
                organization_id=org_a_id,
            )
            is None
        )
        org_b_row = get_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=org_b_id
        )
        assert org_b_row is not None
        assert org_b_row.organization_id == org_b_id


# ---------------------------------------------------------------------------
# get_relevance_summary_by_canonical_id_map
# ---------------------------------------------------------------------------


def test_summary_logged_in_mine_organization_and_others(
    test_engine: Engine
):
    """본인 조직 row + OTHERS 가 섞인 케이스에서 mine_organization / others / 카운터를 검증."""
    me_id = _make_user(session_scope, "summary_me")
    peer1_id = _make_user(session_scope, "summary_peer1")
    peer2_id = _make_user(session_scope, "summary_peer2")
    cid = _make_canonical(session_scope, "official:summary-001")
    org_a_id = _make_organization(session_scope, "조직 A")
    org_b_id = _make_organization(session_scope, "조직 B")

    # 본인 — 조직 A row, 다른 사용자들 — 조직 row 섞어서
    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_a_id,
            reason="내 조직 A",
        )
        # peer1 조직 A: 무관
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=peer1_id,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_a_id,
        )
        # peer2 조직 B: 관련
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=peer2_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_b_id,
        )

    with session_scope() as s:
        summary_map = get_relevance_summary_by_canonical_id_map(
            s, user_id=me_id, canonical_project_ids=[cid]
        )

    summary = summary_map[cid]

    # 본인 조직 row 1 개
    assert len(summary.mine_organization) == 1
    assert summary.mine_organization[0].judgment.organization_id == org_a_id
    assert summary.mine_organization[0].organization_name == "조직 A"
    assert summary.mine_organization[0].judgment.verdict == RELEVANCE_VERDICT_RELATED

    # OTHERS — peer1 조직(무관) + peer2 조직(관련) = 총 2
    others_usernames = sorted(meta.username for meta in summary.others)
    assert others_usernames == ["summary_peer1", "summary_peer2"]

    # 카운터: 관련 2 (본인 조직 A + peer2 조직 B), 무관 1 (peer1 조직 A)
    assert summary.count_related == 2
    assert summary.count_unrelated == 1


def test_summary_logged_in_multiple_organization_rows_sorted_desc(
    test_engine: Engine
):
    """본인이 여러 조직 row 를 가질 때 mine_organization 이 decided_at DESC 정렬이어야 한다."""
    me_id = _make_user(session_scope, "summary_me_org_only")
    cid = _make_canonical(session_scope, "official:summary-002")
    org_a_id = _make_organization(session_scope, "조직 A2")
    org_b_id = _make_organization(session_scope, "조직 B2")
    t_old = datetime(2026, 1, 1, tzinfo=UTC)
    t_new = datetime(2026, 2, 1, tzinfo=UTC)

    with session_scope() as s:
        # 오래된 조직 A row
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_a_id,
            now=t_old,
        )
        # 더 최근의 조직 B row
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_b_id,
            now=t_new,
        )

    with session_scope() as s:
        summary_map = get_relevance_summary_by_canonical_id_map(
            s, user_id=me_id, canonical_project_ids=[cid]
        )

    summary = summary_map[cid]
    # mine_organization 은 decided_at DESC 정렬 — 첫 항목이 최근(B)
    assert len(summary.mine_organization) == 2
    assert summary.mine_organization[0].judgment.organization_id == org_b_id
    assert summary.mine_organization[0].judgment.verdict == RELEVANCE_VERDICT_UNRELATED
    assert summary.mine_organization[1].judgment.organization_id == org_a_id

    # OTHERS 비어 있음 + 전체 카운터 (본인 조직 2개: 관련 1 + 무관 1)
    assert summary.others == ()
    assert summary.count_related == 1
    assert summary.count_unrelated == 1


def test_summary_anonymous_user_treats_all_as_others(test_engine: Engine):
    """비로그인 (user_id=None) 호출은 mine 영역이 비어 있고 모든 row 가 OTHERS 카운터에 들어가야 한다."""
    user_id = _make_user(session_scope, "summary_anon_data_owner")
    cid = _make_canonical(session_scope, "official:summary-003")
    org_a_id = _make_organization(session_scope, "조직 anon-test-A")
    org_b_id = _make_organization(session_scope, "조직 anon-test-B")

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=user_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_a_id,
        )
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=user_id,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_b_id,
        )

    with session_scope() as s:
        summary_map = get_relevance_summary_by_canonical_id_map(
            s, user_id=None, canonical_project_ids=[cid]
        )

    summary = summary_map[cid]
    # 비로그인이면 mine 영역은 비어 있어야 한다.
    assert summary.mine_organization == ()
    # OTHERS 에 두 row 모두 들어감
    assert len(summary.others) == 2
    # 카운터 분리 (비로그인 = mine 없음, others = 전체 = 관련 1 + 무관 1)
    assert summary.count_related == 1
    assert summary.count_unrelated == 1


def test_summary_empty_inputs(test_engine: Engine):
    """canonical_project_ids 가 비어 있으면 빈 dict 를 반환하고 쿼리하지 않는다."""
    with session_scope() as s:
        result = get_relevance_summary_by_canonical_id_map(
            s, user_id=None, canonical_project_ids=[]
        )
    assert result == {}
    # RELEVANCE_SUMMARY_EMPTY 는 호출자가 default 로 사용 가능한 상수.
    fallback = result.get(123, RELEVANCE_SUMMARY_EMPTY)
    assert fallback.mine_organization == ()
    assert fallback.others == ()


def test_summary_n_plus_1_avoidance_single_query(test_engine: Engine):
    """canonical 이 N 개여도 SELECT 쿼리는 정확히 1 번만 발행돼야 한다 (N+1 회귀 차단)."""
    me_id = _make_user(session_scope, "summary_n1_me")
    peer_id = _make_user(session_scope, "summary_n1_peer")
    canonical_ids = [
        _make_canonical(session_scope, f"official:summary-n1-{i:03d}") for i in range(5)
    ]
    org_a_id = _make_organization(session_scope, "조직 n1-test-A")
    org_b_id = _make_organization(session_scope, "조직 n1-test-B")

    with session_scope() as s:
        for cid in canonical_ids:
            set_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=me_id,
                verdict=RELEVANCE_VERDICT_RELATED,
                organization_id=org_a_id,
            )
            set_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=me_id,
                verdict=RELEVANCE_VERDICT_UNRELATED,
                organization_id=org_b_id,
            )
            set_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=peer_id,
                verdict=RELEVANCE_VERDICT_RELATED,
                organization_id=org_a_id,
            )

    # 쿼리 카운터 — engine 에 before_cursor_execute 리스너로 SELECT 수만 집계.
    select_count = 0

    def _count_select(conn, cursor, statement, parameters, context, executemany):
        """SELECT 문 카운트 — INSERT/UPDATE/DELETE 는 무시한다."""
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(test_engine, "before_cursor_execute", _count_select)
    try:
        with session_scope() as s:
            summary_map = get_relevance_summary_by_canonical_id_map(
                s, user_id=me_id, canonical_project_ids=canonical_ids
            )
    finally:
        event.remove(test_engine, "before_cursor_execute", _count_select)

    # canonical 5 개에 대한 summary 가 모두 채워졌고 SELECT 는 1 회만 발행.
    assert len(summary_map) == 5
    assert select_count == 1, (
        f"N+1 회귀 의심 — canonical 5 개 처리에 SELECT {select_count} 회 발행됨. "
        "단일 쿼리 묶음 패턴이 깨졌는지 확인하세요."
    )
    # 임의 한 개의 summary 를 검증해 데이터 형태도 확인.
    sample = summary_map[canonical_ids[0]]
    assert len(sample.mine_organization) == 2
    assert len(sample.others) == 1
    # 카운터: 관련 2 (본인 조직 A + peer 조직 A), 무관 1 (본인 조직 B)
    assert sample.count_related == 2
    assert sample.count_unrelated == 1


def test_summary_returns_only_canonicals_with_rows(test_engine: Engine):
    """row 가 없는 canonical 은 summary_map 의 키로 포함되지 않아야 한다."""
    me_id = _make_user(session_scope, "summary_partial_me")
    cid_with_data = _make_canonical(session_scope, "official:summary-with-data")
    cid_empty = _make_canonical(session_scope, "official:summary-empty")
    org_id = _make_organization(session_scope, "partial-조직")

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid_with_data,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
        )

    with session_scope() as s:
        result = get_relevance_summary_by_canonical_id_map(
            s, user_id=me_id, canonical_project_ids=[cid_with_data, cid_empty]
        )

    assert cid_with_data in result
    assert cid_empty not in result
    # 호출자는 RELEVANCE_SUMMARY_EMPTY 를 default 로 사용 가능.
    fallback = result.get(cid_empty, RELEVANCE_SUMMARY_EMPTY)
    assert fallback is RELEVANCE_SUMMARY_EMPTY


def test_summary_only_self_judgment_shows_counter(test_engine: Engine):
    """본인만 판정하고 다른 사용자가 아무도 판정하지 않은 경우에도 카운터가 올바르게 표시되어야 한다.

    회귀 테스트 (task 00090): 이전에는 others 만 카운트해 본인만 판정하면
    0/0 이 되어 카운터가 숨겨지는 버그가 있었다.
    """
    me_id = _make_user(session_scope, "summary_self_only_me")
    cid_related = _make_canonical(session_scope, "official:summary-self-related")
    cid_unrelated = _make_canonical(session_scope, "official:summary-self-unrelated")
    org_id = _make_organization(session_scope, "self-only-조직")

    with session_scope() as s:
        # 본인만 '관련' 1건 판정
        set_relevance_judgment(
            s,
            canonical_project_id=cid_related,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
        )
        # 본인만 '무관' 1건 판정 (다른 조직 사용)
        org_id2 = Organization(name="self-only-조직-2")
        s.add(org_id2)
        s.flush()
        set_relevance_judgment(
            s,
            canonical_project_id=cid_unrelated,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_id2.id,
        )

    with session_scope() as s:
        summary_map = get_relevance_summary_by_canonical_id_map(
            s, user_id=me_id, canonical_project_ids=[cid_related, cid_unrelated]
        )

    # 본인만 '관련' 판정 → count_related == 1, count_unrelated == 0
    s_related = summary_map[cid_related]
    assert len(s_related.mine_organization) == 1
    assert s_related.others == ()
    assert s_related.count_related == 1
    assert s_related.count_unrelated == 0

    # 본인만 '무관' 판정 → count_related == 0, count_unrelated == 1
    s_unrelated = summary_map[cid_unrelated]
    assert len(s_unrelated.mine_organization) == 1
    assert s_unrelated.others == ()
    assert s_unrelated.count_related == 0
    assert s_unrelated.count_unrelated == 1
