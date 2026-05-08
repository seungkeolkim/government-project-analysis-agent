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


# ---------------------------------------------------------------------------
# task 00085 — organization_id 추가에 대응한 헬퍼 동작 검증
# ---------------------------------------------------------------------------


def _make_organization(session_scope_ctx, name: str) -> int:
    """테스트용 조직을 생성하고 PK 를 반환한다."""
    with session_scope_ctx() as s:
        org = Organization(name=name)
        s.add(org)
        s.flush()
        return org.id


def test_get_relevance_judgment_personal_vs_organization_isolated(
    test_engine: Engine, setup
):
    """개인 row 와 조직 row 는 (canonical, user, org) 트리플로 독립 슬롯이어야 한다."""
    uid = setup["user_id"]
    cid = setup["canonical_id"]
    org_id = _make_organization(session_scope, "관련성-org-A")

    # 같은 사용자가 개인 + 조직 row 동시에 보유
    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            reason="개인 사유",
            organization_id=None,
        )
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            reason="조직 사유",
            organization_id=org_id,
        )

    # organization_id 인자별로 조회 결과가 분리되어야 한다.
    with session_scope() as s:
        personal = get_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=None
        )
        org_row = get_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=org_id
        )
        assert personal is not None
        assert personal.organization_id is None
        assert personal.verdict == RELEVANCE_VERDICT_RELATED
        assert personal.reason == "개인 사유"

        assert org_row is not None
        assert org_row.organization_id == org_id
        assert org_row.verdict == RELEVANCE_VERDICT_UNRELATED
        assert org_row.reason == "조직 사유"


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
            reason="조직 초기",
            organization_id=org_id,
            now=t1,
        )

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            reason="조직 변경",
            organization_id=org_id,
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


def test_delete_relevance_judgment_only_deletes_target_triple(
    test_engine: Engine, setup
):
    """delete 는 (canonical, user, org) 트리플만 지우고 다른 트리플 row 는 보존해야 한다."""
    uid = setup["user_id"]
    cid = setup["canonical_id"]
    org_id = _make_organization(session_scope, "관련성-org-delete")

    # 개인 + 조직 row 동시 생성
    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=None,
        )
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=uid,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=org_id,
        )

    # 조직 row 만 삭제
    with session_scope() as s:
        deleted = delete_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=org_id
        )
        assert deleted is True

    # 조직 row 는 사라지고 개인 row 는 살아 있어야 한다. History 에는 조직 row 이관본 1 건.
    with session_scope() as s:
        assert (
            get_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=uid,
                organization_id=org_id,
            )
            is None
        )
        personal = get_relevance_judgment(
            s, canonical_project_id=cid, user_id=uid, organization_id=None
        )
        assert personal is not None
        assert personal.organization_id is None

        history_rows = list(
            s.execute(
                select(RelevanceJudgmentHistory).where(
                    RelevanceJudgmentHistory.canonical_project_id == cid,
                )
            ).scalars()
        )
        assert len(history_rows) == 1
        assert history_rows[0].organization_id == org_id


# ---------------------------------------------------------------------------
# get_relevance_summary_by_canonical_id_map
# ---------------------------------------------------------------------------


def test_summary_logged_in_personal_priority_and_other_counter(
    test_engine: Engine
):
    """본인 + OTHERS 가 섞인 케이스에서 mine_personal / mine_organization / others / 카운터를 검증."""
    me_id = _make_user(session_scope, "summary_me")
    peer1_id = _make_user(session_scope, "summary_peer1")
    peer2_id = _make_user(session_scope, "summary_peer2")
    cid = _make_canonical(session_scope, "official:summary-001")
    org_a_id = _make_organization(session_scope, "조직 A")

    # 본인 — 개인 row + 조직 A row, 다른 사용자들 — 개인/조직 row 섞어서
    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            reason="내 개인",
            organization_id=None,
        )
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            reason="내 조직 A",
            organization_id=org_a_id,
        )
        # peer1 개인: 관련
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=peer1_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=None,
        )
        # peer1 조직 A: 무관 (안 1 — 같은 조직 안 다른 의견 row 가능)
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=peer1_id,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_a_id,
        )
        # peer2 개인: 관련
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=peer2_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=None,
        )

    with session_scope() as s:
        summary_map = get_relevance_summary_by_canonical_id_map(
            s, user_id=me_id, canonical_project_ids=[cid]
        )

    summary = summary_map[cid]
    # 본인 개인 row 가 큰 배지(우선)
    assert summary.mine_personal is not None
    assert summary.mine_personal.judgment.verdict == RELEVANCE_VERDICT_RELATED
    assert summary.mine_personal.organization_name is None
    assert summary.mine_personal.username == "summary_me"

    # 본인 조직 row 1 개
    assert len(summary.mine_organization) == 1
    assert summary.mine_organization[0].judgment.organization_id == org_a_id
    assert summary.mine_organization[0].organization_name == "조직 A"
    assert summary.mine_organization[0].judgment.verdict == RELEVANCE_VERDICT_UNRELATED

    # OTHERS — peer1 개인(관련) + peer1 조직(무관) + peer2 개인(관련) = 총 3
    others_usernames = sorted(meta.username for meta in summary.others)
    assert others_usernames == ["summary_peer1", "summary_peer1", "summary_peer2"]

    # 카운터: 관련 2 (peer1 개인, peer2 개인), 무관 1 (peer1 조직)
    assert summary.others_count_related == 2
    assert summary.others_count_unrelated == 1


def test_summary_logged_in_only_organization_rows_uses_latest_as_badge(
    test_engine: Engine
):
    """본인 개인 row 가 없고 본인 조직 row 가 여러 개일 때 mine_organization 이 decided_at DESC 정렬."""
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
    # 본인 개인 row 없음
    assert summary.mine_personal is None
    # mine_organization 은 decided_at DESC 정렬 — 첫 항목이 최근(B)
    assert len(summary.mine_organization) == 2
    assert summary.mine_organization[0].judgment.organization_id == org_b_id
    assert summary.mine_organization[0].judgment.verdict == RELEVANCE_VERDICT_UNRELATED
    assert summary.mine_organization[1].judgment.organization_id == org_a_id

    # OTHERS 비어 있음 + 카운터 0
    assert summary.others == ()
    assert summary.others_count_related == 0
    assert summary.others_count_unrelated == 0


def test_summary_anonymous_user_treats_all_as_others(test_engine: Engine):
    """비로그인 (user_id=None) 호출은 mine 영역이 비어 있고 모든 row 가 OTHERS 카운터에 들어가야 한다."""
    user_id = _make_user(session_scope, "summary_anon_data_owner")
    cid = _make_canonical(session_scope, "official:summary-003")
    org_id = _make_organization(session_scope, "조직 anon-test")

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=user_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=None,
        )
        set_relevance_judgment(
            s,
            canonical_project_id=cid,
            user_id=user_id,
            verdict=RELEVANCE_VERDICT_UNRELATED,
            organization_id=org_id,
        )

    with session_scope() as s:
        summary_map = get_relevance_summary_by_canonical_id_map(
            s, user_id=None, canonical_project_ids=[cid]
        )

    summary = summary_map[cid]
    # 비로그인이면 mine 영역은 비어 있어야 한다.
    assert summary.mine_personal is None
    assert summary.mine_organization == ()
    # OTHERS 에 두 row 모두 들어감
    assert len(summary.others) == 2
    # 카운터 분리
    assert summary.others_count_related == 1
    assert summary.others_count_unrelated == 1


def test_summary_empty_inputs(test_engine: Engine):
    """canonical_project_ids 가 비어 있으면 빈 dict 를 반환하고 쿼리하지 않는다."""
    with session_scope() as s:
        result = get_relevance_summary_by_canonical_id_map(
            s, user_id=None, canonical_project_ids=[]
        )
    assert result == {}
    # RELEVANCE_SUMMARY_EMPTY 는 호출자가 default 로 사용 가능한 상수.
    fallback = result.get(123, RELEVANCE_SUMMARY_EMPTY)
    assert fallback.mine_personal is None
    assert fallback.mine_organization == ()
    assert fallback.others == ()


def test_summary_n_plus_1_avoidance_single_query(test_engine: Engine):
    """canonical 이 N 개여도 SELECT 쿼리는 정확히 1 번만 발행돼야 한다 (N+1 회귀 차단)."""
    me_id = _make_user(session_scope, "summary_n1_me")
    peer_id = _make_user(session_scope, "summary_n1_peer")
    canonical_ids = [
        _make_canonical(session_scope, f"official:summary-n1-{i:03d}") for i in range(5)
    ]
    org_id = _make_organization(session_scope, "조직 n1-test")

    with session_scope() as s:
        for cid in canonical_ids:
            set_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=me_id,
                verdict=RELEVANCE_VERDICT_RELATED,
                organization_id=None,
            )
            set_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=me_id,
                verdict=RELEVANCE_VERDICT_UNRELATED,
                organization_id=org_id,
            )
            set_relevance_judgment(
                s,
                canonical_project_id=cid,
                user_id=peer_id,
                verdict=RELEVANCE_VERDICT_RELATED,
                organization_id=None,
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
    assert sample.mine_personal is not None
    assert len(sample.mine_organization) == 1
    assert len(sample.others) == 1
    assert sample.others_count_related == 1
    assert sample.others_count_unrelated == 0


def test_summary_returns_only_canonicals_with_rows(test_engine: Engine):
    """row 가 없는 canonical 은 summary_map 의 키로 포함되지 않아야 한다."""
    me_id = _make_user(session_scope, "summary_partial_me")
    cid_with_data = _make_canonical(session_scope, "official:summary-with-data")
    cid_empty = _make_canonical(session_scope, "official:summary-empty")

    with session_scope() as s:
        set_relevance_judgment(
            s,
            canonical_project_id=cid_with_data,
            user_id=me_id,
            verdict=RELEVANCE_VERDICT_RELATED,
            organization_id=None,
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
