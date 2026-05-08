"""Phase C — app/progress/repository.py 단위 테스트.

검증 시나리오 (사용자 원문 17 항목 중 본 subtask 가 책임지는 4·5·6·7·8·9):
    4. 선점 충돌 — 다른 조직이 이미 '진행' 인 상태에서 본인 '진행' 시도 → PreemptionConflict.
    5. 다른 조직이 '종료' / 다른 단계로 내려가면 본인 '진행' 가능.
    6. 양방향 롤백 — 진행→검토→관심→종료→검토 등 모든 전이 가능.
    7. status 변경 시 history 이관 (이전 status, note, archived_at 정확히 보존).
    8. content_changed reset (Phase 1a) 시 announcement_progress 도 history 로 이관.
    9. 같은 조직 동료가 만든 row 를 본인이 수정·삭제 가능 (조직 멤버 누구나 권한).

추가 커버리지:
    - 신규 INSERT / 멱등 UPDATE 동작.
    - get_progress / list_progress_history 정렬 / 필터링.
    - get_progress_summary_by_canonical_id_map (로그인 / 비로그인 / my_org_active 우선순위).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.models import (
    AnnouncementProgress,
    AnnouncementProgressArchiveReason,
    AnnouncementProgressStatus,
    AnnouncementStatus,
    CanonicalProject,
    Organization,
    User,
    UserOrganization,
)
from app.db.repository import upsert_announcement
from app.progress.repository import (
    PROGRESS_SUMMARY_EMPTY,
    PreemptionConflict,
    create_progress,
    delete_progress,
    ensure_in_progress_unique,
    get_progress,
    get_progress_for_organization,
    get_progress_summary_by_canonical_id_map,
    list_progress_history,
    list_user_organization_ids,
    reset_progress_for_canonical,
    update_progress,
)


# ── fixture helpers ──────────────────────────────────────────────────────────


def _make_user(session: Session, username: str) -> User:
    """테스트용 User 를 만들어 flush 후 반환한다."""
    user = User(username=username, password_hash="dummy", is_admin=False)
    session.add(user)
    session.flush()
    return user


def _make_canonical(session: Session, key: str) -> CanonicalProject:
    """canonical_project 한 건을 만든다."""
    canonical_project = CanonicalProject(canonical_key=key, key_scheme="official")
    session.add(canonical_project)
    session.flush()
    return canonical_project


def _make_organization(session: Session, name: str) -> Organization:
    """조직 한 건을 만든다."""
    organization = Organization(name=name)
    session.add(organization)
    session.flush()
    return organization


def _make_user_with_org(
    session: Session, username: str, organization: Organization
) -> User:
    """User 를 만들고 organization 에 매핑까지 심는다."""
    user = _make_user(session, username)
    session.add(UserOrganization(user_id=user.id, organization_id=organization.id))
    session.flush()
    return user


def _payload_for(
    source_id: str,
    *,
    title: str = "원본 공고",
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
    agency: str | None = "기관A",
    deadline_at: datetime | None = None,
) -> dict[str, Any]:
    """upsert_announcement payload 헬퍼 (test_change_detection.py 와 동일 형식)."""
    return {
        "source_announcement_id": source_id,
        "source_type": "IRIS",
        "title": title,
        "status": status,
        "agency": agency,
        "deadline_at": deadline_at or datetime(2026, 5, 31, tzinfo=UTC),
        "raw_metadata": {},
        "ancm_no": f"ANCM-{source_id}",
    }


# ── (1) create / 멱등 UPDATE / 단순 조회 ────────────────────────────────────


def test_create_progress_inserts_new_row(test_engine: Engine, db_session: Session) -> None:
    """동일 (canonical, organization) row 가 없을 때 신규 INSERT."""
    canonical = _make_canonical(db_session, "official:create-1")
    organization = _make_organization(db_session, "팀-create")
    user = _make_user(db_session, "create-user")

    progress = create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.INTEREST,
        note="첫 입장",
        created_by_user_id=user.id,
    )

    assert progress.id is not None
    assert progress.status == AnnouncementProgressStatus.INTEREST
    assert progress.note == "첫 입장"
    assert progress.created_by_user_id == user.id
    # history 는 비어 있어야 한다.
    history = list_progress_history(db_session, canonical.id)
    assert history == []


def test_create_progress_idempotent_when_row_exists(
    test_engine: Engine, db_session: Session
) -> None:
    """이미 같은 (canonical, organization) row 가 있으면 UPDATE 위임 — history 1 건 누적."""
    canonical = _make_canonical(db_session, "official:create-idempotent-1")
    organization = _make_organization(db_session, "팀-idempotent")
    user = _make_user(db_session, "idempotent-user")

    create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.INTEREST,
        note="첫 입장",
        created_by_user_id=user.id,
    )
    second = create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.REVIEW,
        note="재검토",
        created_by_user_id=user.id,
    )

    assert second.status == AnnouncementProgressStatus.REVIEW
    assert second.note == "재검토"
    history = list_progress_history(db_session, canonical.id)
    assert len(history) == 1
    assert history[0].status == AnnouncementProgressStatus.INTEREST
    assert history[0].note == "첫 입장"
    assert history[0].archive_reason == AnnouncementProgressArchiveReason.USER_CHANGED


# ── (2) 시나리오 4: 선점 충돌 ────────────────────────────────────────────────


def test_in_progress_preemption_conflict(
    test_engine: Engine, db_session: Session
) -> None:
    """다른 조직이 이미 '진행' 인 상태에서 본인 '진행' 시도 → PreemptionConflict."""
    canonical = _make_canonical(db_session, "official:preempt-1")
    organization_first = _make_organization(db_session, "팀-A-선점")
    organization_second = _make_organization(db_session, "팀-B-도전")
    user_first = _make_user(db_session, "preempt-first")
    user_second = _make_user(db_session, "preempt-second")

    # A 조직이 먼저 '진행'.
    create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization_first.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="A 가 먼저",
        created_by_user_id=user_first.id,
    )

    # B 조직이 같은 canonical 을 '진행' 으로 올리려고 시도 → 충돌.
    with pytest.raises(PreemptionConflict) as excinfo:
        create_progress(
            db_session,
            canonical_project_id=canonical.id,
            organization_id=organization_second.id,
            status=AnnouncementProgressStatus.IN_PROGRESS,
            note="B 가 시도",
            created_by_user_id=user_second.id,
        )

    assert excinfo.value.conflicting_organization_id == organization_first.id
    assert excinfo.value.conflicting_organization_name == "팀-A-선점"


def test_in_progress_does_not_block_same_organization(
    test_engine: Engine, db_session: Session
) -> None:
    """같은 조직 row 의 '진행' UPDATE 는 선점 자기 자신과 충돌하지 않는다."""
    canonical = _make_canonical(db_session, "official:preempt-self-1")
    organization = _make_organization(db_session, "팀-self")
    user = _make_user(db_session, "self-user")

    progress = create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="A 가 진행",
        created_by_user_id=user.id,
    )
    # 같은 조직이 note 만 바꿔서 다시 '진행' 으로 UPDATE — 충돌 아님.
    updated = update_progress(
        db_session,
        progress_id=progress.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="note 만 변경",
        modifier_user_id=user.id,
    )
    assert updated.note == "note 만 변경"


# ── (3) 시나리오 5: 선점 조직이 다른 단계로 내려가면 다른 조직이 '진행' 가능 ──


def test_other_organization_can_take_over_after_release(
    test_engine: Engine, db_session: Session
) -> None:
    """A 가 '종료' 로 내려가면 B 가 '진행' 으로 올라갈 수 있다."""
    canonical = _make_canonical(db_session, "official:release-1")
    organization_first = _make_organization(db_session, "팀-A-종료")
    organization_second = _make_organization(db_session, "팀-B-새진행")
    user_first = _make_user(db_session, "release-first")
    user_second = _make_user(db_session, "release-second")

    progress_first = create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization_first.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="A 진행",
        created_by_user_id=user_first.id,
    )
    update_progress(
        db_session,
        progress_id=progress_first.id,
        status=AnnouncementProgressStatus.DONE,
        note="A 종료",
        modifier_user_id=user_first.id,
    )

    # 이제 B 조직이 '진행' 으로 올라갈 수 있어야 한다 — 선점 충돌 없음.
    progress_second = create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization_second.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="B 진행",
        created_by_user_id=user_second.id,
    )
    assert progress_second.status == AnnouncementProgressStatus.IN_PROGRESS

    # ensure_in_progress_unique 를 별도로 호출해도 통과.
    ensure_in_progress_unique(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization_second.id,
    )


# ── (4) 시나리오 6: 양방향 롤백 — 모든 4 단계 사이 자유 전이 ─────────────


def test_status_transitions_in_all_directions(
    test_engine: Engine, db_session: Session
) -> None:
    """진행→검토→관심→종료→검토 등 모든 전이 허용 + 매번 history 이관."""
    canonical = _make_canonical(db_session, "official:transitions-1")
    organization = _make_organization(db_session, "팀-transitions")
    user = _make_user(db_session, "trans-user")

    progress = create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="진행",
        created_by_user_id=user.id,
    )
    transitions = [
        AnnouncementProgressStatus.REVIEW,
        AnnouncementProgressStatus.INTEREST,
        AnnouncementProgressStatus.DONE,
        AnnouncementProgressStatus.REVIEW,
    ]
    for next_status in transitions:
        progress = update_progress(
            db_session,
            progress_id=progress.id,
            status=next_status,
            note=next_status.value,
            modifier_user_id=user.id,
        )
        assert progress.status == next_status

    # active row 1 건, history 4 건 (최초 INSERT 후 4 회 UPDATE).
    active_rows = db_session.execute(
        select(AnnouncementProgress).where(
            AnnouncementProgress.canonical_project_id == canonical.id
        )
    ).scalars().all()
    history_rows = list_progress_history(db_session, canonical.id)
    assert len(active_rows) == 1
    assert len(history_rows) == 4
    archived_statuses = [h.status for h in history_rows]
    # 가장 최근 archived 가 가장 최신 전이의 직전 status (DONE) 가 되어야 한다.
    assert archived_statuses[0] == AnnouncementProgressStatus.DONE


# ── (5) 시나리오 7: history 메타 보존 정확성 ─────────────────────────────


def test_history_preserves_status_note_and_archived_at(
    test_engine: Engine, db_session: Session
) -> None:
    """status / note / archived_at / created_by_user_id 가 history 에 정확히 복사."""
    canonical = _make_canonical(db_session, "official:history-meta-1")
    organization = _make_organization(db_session, "팀-history")
    user = _make_user(db_session, "history-user")

    fixed_now = datetime(2026, 5, 8, 10, 0, 0, tzinfo=UTC)
    progress = create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.REVIEW,
        note="검토 중",
        created_by_user_id=user.id,
        now=fixed_now,
    )

    later_time = datetime(2026, 5, 9, 11, 30, 0, tzinfo=UTC)
    update_progress(
        db_session,
        progress_id=progress.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="진행으로 변경",
        modifier_user_id=user.id,
        now=later_time,
    )

    history_rows = list_progress_history(db_session, canonical.id)
    assert len(history_rows) == 1
    archived = history_rows[0]
    assert archived.status == AnnouncementProgressStatus.REVIEW
    assert archived.note == "검토 중"
    assert archived.created_by_user_id == user.id
    # archived_at 은 update_progress 의 now 인자와 일치해야 한다.
    assert archived.archived_at.replace(tzinfo=UTC) == later_time
    # created_at 은 원본 now (REVIEW row 가 INSERT 된 시각) 와 일치 — Phase B 패턴.
    assert archived.created_at.replace(tzinfo=UTC) == fixed_now


# ── (6) 시나리오 8: content_changed reset ────────────────────────────────


def test_content_changed_reset_archives_progress_rows(
    test_engine: Engine, db_session: Session
) -> None:
    """canonical title 변경 → new_version + announcement_progress 도 history 이관.

    Phase 1a 변경 감지 hook (_reset_user_state_on_content_change) 가 본 subtask 에서
    추가한 reset_progress_for_canonical 호출을 통해 progress 도 함께 archive 한다.
    """
    organization_a = _make_organization(db_session, "팀-content-A")
    organization_b = _make_organization(db_session, "팀-content-B")
    user = _make_user(db_session, "content-changed-user")

    # 공고 등록 → canonical 생성.
    first = upsert_announcement(db_session, _payload_for("CC-1"))
    canonical_id = first.announcement.canonical_group_id
    assert canonical_id is not None

    # 두 조직 row 를 심는다 (선점 충돌 회피 — A 만 진행, B 는 검토).
    create_progress(
        db_session,
        canonical_project_id=canonical_id,
        organization_id=organization_a.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="A 진행",
        created_by_user_id=user.id,
    )
    create_progress(
        db_session,
        canonical_project_id=canonical_id,
        organization_id=organization_b.id,
        status=AnnouncementProgressStatus.REVIEW,
        note="B 검토",
        created_by_user_id=user.id,
    )

    # title 변경 → new_version + content_changed reset.
    second = upsert_announcement(
        db_session, _payload_for("CC-1", title="제목 변경됨")
    )
    assert second.action == "new_version"

    # active 0 건, history 2 건 (둘 다 archive_reason='content_changed').
    active = db_session.execute(
        select(AnnouncementProgress).where(
            AnnouncementProgress.canonical_project_id == canonical_id
        )
    ).scalars().all()
    history_rows = list_progress_history(db_session, canonical_id)
    assert len(active) == 0
    assert len(history_rows) == 2
    assert all(
        h.archive_reason == AnnouncementProgressArchiveReason.CONTENT_CHANGED
        for h in history_rows
    )
    archived_organization_ids = sorted(h.organization_id for h in history_rows)
    assert archived_organization_ids == sorted([organization_a.id, organization_b.id])


def test_status_only_transition_does_not_reset_progress(
    test_engine: Engine, db_session: Session
) -> None:
    """status 단독 전이는 in-place UPDATE — progress 가 reset 되지 않는다.

    Phase 1a 컨벤션: status 단독 변경 (status_transitioned) 분기는 사용자 라벨링
    리셋을 트리거하지 않는다 (사용자 원문). progress 도 동일하게 보존되어야 한다.
    """
    organization = _make_organization(db_session, "팀-status-only")
    user = _make_user(db_session, "status-only-user")

    first = upsert_announcement(db_session, _payload_for("ST-1"))
    canonical_id = first.announcement.canonical_group_id
    assert canonical_id is not None

    create_progress(
        db_session,
        canonical_project_id=canonical_id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="유지되어야 함",
        created_by_user_id=user.id,
    )

    # status 만 RECEIVING → CLOSED (전이만).
    second = upsert_announcement(
        db_session,
        _payload_for("ST-1", status=AnnouncementStatus.CLOSED),
    )
    assert second.action == "status_transitioned"

    # active row 가 그대로 살아 있어야 한다.
    active = list(get_progress(db_session, canonical_id))
    history_rows = list_progress_history(db_session, canonical_id)
    assert len(active) == 1
    assert active[0].status == AnnouncementProgressStatus.IN_PROGRESS
    assert active[0].note == "유지되어야 함"
    assert history_rows == []


def test_reset_progress_for_canonical_no_op_when_empty(
    test_engine: Engine, db_session: Session
) -> None:
    """activeprogress 가 없는 canonical 에 대해 호출하면 0 반환 (예외 없음)."""
    canonical = _make_canonical(db_session, "official:reset-empty-1")
    archived = reset_progress_for_canonical(db_session, canonical.id)
    assert archived == 0


# ── (7) 시나리오 9: 같은 조직 동료 권한 — 작성자 무관 수정·삭제 ──────────


def test_same_organization_colleague_can_update_and_delete(
    test_engine: Engine, db_session: Session
) -> None:
    """같은 조직의 다른 사용자가 만든 row 를 다른 멤버가 update / delete 할 수 있다.

    repository 는 권한 검증을 하지 않는다 — "조직 멤버 누구나" 정책의 결과는
    update_progress / delete_progress 가 modifier_user_id 만 받아 처리하고
    created_by_user_id 를 modifier 로 갱신한다는 점에서 드러난다.
    """
    canonical = _make_canonical(db_session, "official:colleague-1")
    organization = _make_organization(db_session, "팀-colleague")
    alice = _make_user(db_session, "alice")
    bob = _make_user(db_session, "bob")

    # alice 가 row 를 만든다.
    progress = create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.INTEREST,
        note="alice 작성",
        created_by_user_id=alice.id,
    )
    assert progress.created_by_user_id == alice.id

    # bob 이 같은 row 를 update — 본 repository 는 권한 검증을 하지 않으며
    # created_by_user_id 가 bob 으로 갱신되어야 한다.
    updated = update_progress(
        db_session,
        progress_id=progress.id,
        status=AnnouncementProgressStatus.REVIEW,
        note="bob 수정",
        modifier_user_id=bob.id,
    )
    assert updated.created_by_user_id == bob.id
    assert updated.status == AnnouncementProgressStatus.REVIEW

    # bob 이 삭제도 가능.
    deleted = delete_progress(
        db_session, progress_id=progress.id, modifier_user_id=bob.id
    )
    assert deleted is True
    assert get_progress_for_organization(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
    ) is None
    history_rows = list_progress_history(db_session, canonical.id)
    # update + delete 두 번 archive → history 2 건.
    assert len(history_rows) == 2


def test_delete_progress_no_op_when_missing(
    test_engine: Engine, db_session: Session
) -> None:
    """존재하지 않는 progress_id 삭제는 False 반환 (예외 없음)."""
    deleted = delete_progress(db_session, progress_id=999999, modifier_user_id=None)
    assert deleted is False


# ── (8) summary 헬퍼 ────────────────────────────────────────────────────────


def test_summary_with_login_user_my_org_active_priority(
    test_engine: Engine, db_session: Session
) -> None:
    """본인 소속 조직의 활동 단계가 우선순위(진행 > 검토 > 관심) 로 노출된다."""
    canonical_one = _make_canonical(db_session, "official:summary-1")
    canonical_two = _make_canonical(db_session, "official:summary-2")
    organization_mine_high = _make_organization(db_session, "팀-내-진행")
    organization_mine_low = _make_organization(db_session, "팀-내-관심")
    organization_other = _make_organization(db_session, "팀-외부")

    # alice 는 진행/관심 두 조직 모두에 소속.
    alice = _make_user_with_org(db_session, "summary-alice", organization_mine_high)
    db_session.add(
        UserOrganization(user_id=alice.id, organization_id=organization_mine_low.id)
    )
    db_session.flush()

    # canonical_one: alice 의 두 조직 모두 활동 — 진행 vs 관심 → 진행이 우선.
    create_progress(
        db_session,
        canonical_project_id=canonical_one.id,
        organization_id=organization_mine_high.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="진행",
        created_by_user_id=alice.id,
    )
    create_progress(
        db_session,
        canonical_project_id=canonical_one.id,
        organization_id=organization_mine_low.id,
        status=AnnouncementProgressStatus.INTEREST,
        note="관심",
        created_by_user_id=alice.id,
    )
    # 외부 조직은 검토 단계 — 카운터에 반영되지만 my_org_active 에는 들어가지 않는다.
    create_progress(
        db_session,
        canonical_project_id=canonical_one.id,
        organization_id=organization_other.id,
        status=AnnouncementProgressStatus.REVIEW,
        note="외부 검토",
        created_by_user_id=alice.id,
    )

    # canonical_two: 외부 조직만 활동 (검토 1, 관심 1).
    organization_other_b = _make_organization(db_session, "팀-외부-B")
    create_progress(
        db_session,
        canonical_project_id=canonical_two.id,
        organization_id=organization_other.id,
        status=AnnouncementProgressStatus.REVIEW,
        note="2-검토",
        created_by_user_id=alice.id,
    )
    create_progress(
        db_session,
        canonical_project_id=canonical_two.id,
        organization_id=organization_other_b.id,
        status=AnnouncementProgressStatus.INTEREST,
        note="2-관심",
        created_by_user_id=alice.id,
    )

    summary_map = get_progress_summary_by_canonical_id_map(
        db_session,
        user_id=alice.id,
        canonical_project_ids=[canonical_one.id, canonical_two.id],
    )

    summary_one = summary_map[canonical_one.id]
    assert summary_one.in_progress_org is not None
    assert summary_one.in_progress_org.id == organization_mine_high.id
    assert summary_one.in_progress_org.name == "팀-내-진행"
    assert summary_one.counter_review == 1
    assert summary_one.counter_interest == 1  # 본인 조직 관심 row 도 counter 에 포함
    assert summary_one.my_org_active == "진행"

    summary_two = summary_map[canonical_two.id]
    assert summary_two.in_progress_org is None
    assert summary_two.counter_review == 1
    assert summary_two.counter_interest == 1
    # alice 는 canonical_two 에서 활동 row 가 없다 (외부 조직만 활동).
    assert summary_two.my_org_active is None


def test_summary_anonymous_omits_my_org_active(
    test_engine: Engine, db_session: Session
) -> None:
    """비로그인 (user_id=None) 일 때 my_org_active 는 항상 None."""
    canonical = _make_canonical(db_session, "official:summary-anon-1")
    organization = _make_organization(db_session, "팀-anon")
    seeder = _make_user(db_session, "anon-seeder")
    create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.IN_PROGRESS,
        note="익명 노출",
        created_by_user_id=seeder.id,
    )

    summary_map = get_progress_summary_by_canonical_id_map(
        db_session, user_id=None, canonical_project_ids=[canonical.id]
    )
    summary = summary_map[canonical.id]
    assert summary.in_progress_org is not None
    assert summary.in_progress_org.id == organization.id
    assert summary.my_org_active is None


def test_summary_skips_done_status(test_engine: Engine, db_session: Session) -> None:
    """status='종료' 는 카운터 / my_org_active 어디에도 노출하지 않는다."""
    canonical = _make_canonical(db_session, "official:summary-done-1")
    organization = _make_organization(db_session, "팀-done")
    user = _make_user_with_org(db_session, "summary-done-user", organization)

    create_progress(
        db_session,
        canonical_project_id=canonical.id,
        organization_id=organization.id,
        status=AnnouncementProgressStatus.DONE,
        note="이미 종료",
        created_by_user_id=user.id,
    )

    summary_map = get_progress_summary_by_canonical_id_map(
        db_session,
        user_id=user.id,
        canonical_project_ids=[canonical.id],
    )
    # row 가 1 개라도 있으니 키는 들어가지만 모든 카운터·my_org_active 가 비어 있어야 함.
    summary = summary_map.get(canonical.id, PROGRESS_SUMMARY_EMPTY)
    assert summary.in_progress_org is None
    assert summary.counter_review == 0
    assert summary.counter_interest == 0
    assert summary.my_org_active is None


def test_summary_empty_input_returns_empty_dict(
    test_engine: Engine, db_session: Session
) -> None:
    """canonical_project_ids 가 비면 쿼리도 안 발행하고 빈 dict 반환."""
    summary_map = get_progress_summary_by_canonical_id_map(
        db_session, user_id=None, canonical_project_ids=[]
    )
    assert summary_map == {}


# ── (9) list_user_organization_ids ──────────────────────────────────────────


def test_list_user_organization_ids(test_engine: Engine, db_session: Session) -> None:
    """user 의 소속 조직 PK 리스트를 반환한다 (필터 SQL 분기에서 재사용)."""
    organization_a = _make_organization(db_session, "팀-list-A")
    organization_b = _make_organization(db_session, "팀-list-B")
    user = _make_user_with_org(db_session, "list-user", organization_a)
    db_session.add(
        UserOrganization(user_id=user.id, organization_id=organization_b.id)
    )
    db_session.flush()

    org_ids = sorted(list_user_organization_ids(db_session, user.id))
    assert org_ids == sorted([organization_a.id, organization_b.id])

    # 비로그인 / 무소속 사용자.
    assert list_user_organization_ids(db_session, None) == []
    isolated_user = _make_user(db_session, "isolated-user")
    assert list_user_organization_ids(db_session, isolated_user.id) == []


# NOTE: FK CASCADE (조직 삭제 → progress / history 자동 정리) 는 DDL 레벨 회귀로
# subtask 00097-2 의 migration smoke 테스트가 이미 검증했다. 본 repository 단위
# 테스트의 SQLite 세션은 PRAGMA foreign_keys=ON 을 켜지 않으므로 runtime CASCADE
# 가 발생하지 않는다. 운영 경로의 FK 강제는 app/db/session.py 와 docker entrypoint
# 가 보장한다.
