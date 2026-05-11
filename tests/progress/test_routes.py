"""Phase C — 진행 상태 / 선점 라우터 HTTP 통합 테스트.

TestClient 로 4 개 엔드포인트를 커버한다:
    POST   /canonical/{id}/progress
    PATCH  /canonical/{id}/progress/{progress_id}
    DELETE /canonical/{id}/progress/{progress_id}
    GET    /canonical/{id}/progress/history

검증 시나리오 매핑 (사용자 원문 17 항목 중 라우터 책임):
    1. 본인 소속 조직 진행 row 신규/수정/삭제 정상 — POST/PATCH/DELETE 200.
    2. 본인 소속 외 조직 row 작성/수정 시도 → POST=422 / PATCH=403.
    3. 무소속 사용자 작성 시도 → 422.
    4. status='진행' 선점 — 다른 조직이 이미 '진행' 인 상태에서 본인 '진행' 시도 → 409.
    5. 선점 조직이 '종료'/다른 단계로 내려가면 다른 조직이 '진행' 으로 올릴 수 있음.
    6. 양방향 롤백 — 진행/검토/관심/종료 사이 모든 전이가 PATCH 로 가능.
    9. 같은 조직 동료가 만든 row 를 본인이 수정·삭제 가능 (조직 멤버 누구나 권한).
    14. 비로그인 GET history 200 (변경 영역만 비활성).
    17. N+1 회귀 — GET history / get_progress_summary 가 페이지당 추가 쿼리 1~2 개로 고정.

TestClient.delete() 에 json= 키워드를 직접 못 쓰는 점 주의 (memory:
feedback_test_delete_pattern.md). DELETE body 가 필요한 곳은
``client.request(\"DELETE\", url, json=...)`` 로 호출한다. 본 라우터의 DELETE 는
body 없이 path 기반이라 단순 ``client.delete(url)`` 로 충분하다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select

from app.db.models import (
    AnnouncementProgress,
    AnnouncementProgressStatus,
    CanonicalProject,
    Organization,
    User,
    UserOrganization,
)
from app.db.session import session_scope
from app.progress.repository import (
    create_progress,
    get_progress_summary_by_canonical_id_map,
)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """본 모듈의 모든 테스트가 공유하는 TestClient — 매 테스트 별 fresh DB."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    """alice 로 회원가입 후 세션 쿠키 유지된 TestClient."""
    client.post(
        "/auth/register",
        data={"username": "progress_alice", "password": "password_123"},
        follow_redirects=False,
    )
    return client


@pytest.fixture
def canonical_id(test_engine: Engine) -> int:
    """테스트용 CanonicalProject 한 건을 만들고 PK 를 반환한다."""
    with session_scope() as s:
        cp = CanonicalProject(
            canonical_key="official:progress-route-001",
            key_scheme="official",
        )
        s.add(cp)
        s.flush()
        return cp.id


# ── helper functions ────────────────────────────────────────────────────────


def _create_organization(name: str) -> int:
    """테스트용 조직 한 건을 만들고 PK 반환."""
    with session_scope() as s:
        organization = Organization(name=name)
        s.add(organization)
        s.flush()
        return organization.id


def _add_user_to_organization(username: str, organization_id: int) -> int:
    """username 사용자에게 organization_id 매핑을 추가하고 user_id 를 반환한다."""
    with session_scope() as s:
        user = s.execute(select(User).where(User.username == username)).scalar_one()
        s.add(UserOrganization(user_id=user.id, organization_id=organization_id))
        s.flush()
        return user.id


def _create_progress_via_repo(
    canonical_id: int,
    organization_id: int,
    status_value: AnnouncementProgressStatus,
    note: str | None,
    created_by_user_id: int,
) -> int:
    """라우터 우회 — repository 로 직접 row 를 심는다 (선점/동료 시나리오용)."""
    with session_scope() as s:
        row = create_progress(
            s,
            canonical_project_id=canonical_id,
            organization_id=organization_id,
            status=status_value,
            note=note,
            created_by_user_id=created_by_user_id,
        )
        return row.id


# ── 비로그인 정책 (시나리오 14) ──────────────────────────────────────────────


def test_post_progress_requires_login(
    client: TestClient, canonical_id: int
) -> None:
    """비로그인 POST → 401."""
    resp = client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": 1, "status": "관심", "note": ""},
    )
    assert resp.status_code == 401


def test_patch_progress_requires_login(
    client: TestClient, canonical_id: int
) -> None:
    """비로그인 PATCH → 401."""
    resp = client.patch(
        f"/canonical/{canonical_id}/progress/1",
        json={"status": "검토", "note": ""},
    )
    assert resp.status_code == 401


def test_delete_progress_requires_login(
    client: TestClient, canonical_id: int
) -> None:
    """비로그인 DELETE → 401. 본 라우터의 DELETE 는 body 가 없어 단순 client.delete()."""
    resp = client.delete(f"/canonical/{canonical_id}/progress/1")
    assert resp.status_code == 401


def test_get_history_allows_anonymous(
    client: TestClient, canonical_id: int
) -> None:
    """비로그인 GET history → 200 + 빈 배열. 시나리오 14 (비로그인 동일 노출)."""
    resp = client.get(f"/canonical/{canonical_id}/progress/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["canonical_project_id"] == canonical_id
    assert body["history"] == []


# ── canonical 부재 → 404 ────────────────────────────────────────────────────


def test_post_progress_canonical_not_found(logged_in_client: TestClient) -> None:
    """존재하지 않는 canonical 로 POST → 404."""
    org_id = _create_organization("not-found-org")
    _add_user_to_organization("progress_alice", org_id)
    resp = logged_in_client.post(
        "/canonical/999999/progress",
        json={"organization_id": org_id, "status": "관심", "note": ""},
    )
    assert resp.status_code == 404


def test_get_history_canonical_not_found(logged_in_client: TestClient) -> None:
    """존재하지 않는 canonical 로 GET history → 404."""
    resp = logged_in_client.get("/canonical/999999/progress/history")
    assert resp.status_code == 404


# ── 시나리오 1: 본인 소속 조직 진행 상태 신규/수정/삭제 정상 ────────────


def test_post_progress_creates_row(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인 소속 조직 PK + 4 단계 한글 enum → 200 + 응답 스키마 점검."""
    org_id = _create_organization("팀-create-route")
    _add_user_to_organization("progress_alice", org_id)

    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={
            "organization_id": org_id,
            "status": "관심",
            "note": "alice 의 첫 입장",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["canonical_project_id"] == canonical_id
    assert body["organization_id"] == org_id
    assert body["organization_name"] == "팀-create-route"
    assert body["status"] == "관심"
    assert body["note"] == "alice 의 첫 입장"
    assert body["last_modifier_username"] == "progress_alice"
    assert body["last_modifier_user_id"] is not None
    assert body["updated_at"] is not None
    assert body["created_at"] is not None
    assert body["id"] is not None


def test_post_progress_idempotent_when_row_exists(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """이미 (canonical, organization) row 가 있는데 다시 POST → 멱등 UPDATE."""
    org_id = _create_organization("팀-idempotent-route")
    _add_user_to_organization("progress_alice", org_id)

    logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": org_id, "status": "관심", "note": "처음"},
    )
    second = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": org_id, "status": "검토", "note": "갱신"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "검토"
    assert body["note"] == "갱신"

    # history 에 1 건 archive 되어야 한다 (직전 '관심' row).
    hist_resp = logged_in_client.get(
        f"/canonical/{canonical_id}/progress/history"
    )
    assert hist_resp.status_code == 200
    hist = hist_resp.json()["history"]
    assert len(hist) == 1
    assert hist[0]["status"] == "관심"
    assert hist[0]["archive_reason"] == "user_changed"


def test_patch_progress_updates_row(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인 조직 row 를 PATCH → status / note 갱신, history 1 건 누적."""
    org_id = _create_organization("팀-patch-route")
    _add_user_to_organization("progress_alice", org_id)

    initial = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": org_id, "status": "관심", "note": "초기"},
    ).json()
    progress_id = initial["id"]

    resp = logged_in_client.patch(
        f"/canonical/{canonical_id}/progress/{progress_id}",
        json={"status": "진행", "note": "본격 진행"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == progress_id
    assert body["status"] == "진행"
    assert body["note"] == "본격 진행"

    # history 에 직전 '관심' row 가 archive 되어야 한다.
    hist = logged_in_client.get(
        f"/canonical/{canonical_id}/progress/history"
    ).json()["history"]
    assert len(hist) == 1
    assert hist[0]["status"] == "관심"


def test_delete_progress_removes_row(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인 조직 row 를 DELETE → 200 + history 1 건 (user_changed)."""
    org_id = _create_organization("팀-delete-route")
    _add_user_to_organization("progress_alice", org_id)

    initial = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": org_id, "status": "검토", "note": "삭제 대상"},
    ).json()
    progress_id = initial["id"]

    resp = logged_in_client.delete(
        f"/canonical/{canonical_id}/progress/{progress_id}"
    )
    assert resp.status_code == 200
    assert resp.json()["detail"] == "삭제되었습니다."

    # active row 0 건, history 1 건.
    with session_scope() as s:
        active = s.execute(
            select(AnnouncementProgress).where(
                AnnouncementProgress.canonical_project_id == canonical_id
            )
        ).scalars().all()
        assert active == []
    hist = logged_in_client.get(
        f"/canonical/{canonical_id}/progress/history"
    ).json()["history"]
    assert len(hist) == 1
    assert hist[0]["archive_reason"] == "user_changed"


# ── 시나리오 2: 본인 소속 외 조직 작성/수정 시도 ─────────────────────────


def test_post_progress_non_member_organization_returns_422(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인 소속 외 조직 PK 로 POST → 422 + 한국어 detail. 시나리오 2."""
    foreign_org_id = _create_organization("팀-foreign")
    # alice 는 이 조직에 속하지 않음.
    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": foreign_org_id, "status": "관심", "note": ""},
    )
    assert resp.status_code == 422
    assert "본인 소속 조직" in resp.json()["detail"]


def test_patch_progress_non_member_organization_returns_403(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인 소속 외 조직 row 를 PATCH → 403 (시나리오 2 — Phase C 의 PATCH 분기).

    alice 는 다른 조직 (팀-alice-own) 에 속해 있어 무소속(422) 분기는 회피된다.
    공격 대상은 alice 가 속하지 않은 조직 (팀-foreign-row) 의 row.
    """
    # alice 는 자신의 다른 조직에 속한다 — 무소속(422) 분기 회피.
    alice_org_id = _create_organization("팀-alice-own-foreign-test")
    _add_user_to_organization("progress_alice", alice_org_id)

    # 외부 조직 row 를 미리 심는다 (다른 사용자 ID 로).
    with session_scope() as s:
        other = User(username="progress_bob_other_org", password_hash="dummy")
        s.add(other)
        s.flush()
        other_id = other.id
    foreign_org_id = _create_organization("팀-foreign-row")
    _add_user_to_organization("progress_bob_other_org", foreign_org_id)
    progress_id = _create_progress_via_repo(
        canonical_id,
        foreign_org_id,
        AnnouncementProgressStatus.REVIEW,
        "외부 조직 row",
        other_id,
    )

    # alice (foreign_org_id 미소속이지만 다른 조직 멤버) 가 PATCH 시도 → 403.
    resp = logged_in_client.patch(
        f"/canonical/{canonical_id}/progress/{progress_id}",
        json={"status": "진행", "note": "탈취 시도"},
    )
    assert resp.status_code == 403
    assert "본인 소속" in resp.json()["detail"]


def test_delete_progress_non_member_organization_returns_403(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인 소속 외 조직 row DELETE 시도 → 403.

    alice 는 다른 조직에 속해 있어 무소속(422) 분기는 회피된다.
    """
    # alice 는 자신의 다른 조직에 속한다 — 무소속(422) 분기 회피.
    alice_org_id = _create_organization("팀-alice-own-delete-test")
    _add_user_to_organization("progress_alice", alice_org_id)

    with session_scope() as s:
        other = User(username="progress_bob_delete_other", password_hash="dummy")
        s.add(other)
        s.flush()
        other_id = other.id
    foreign_org_id = _create_organization("팀-foreign-delete")
    _add_user_to_organization("progress_bob_delete_other", foreign_org_id)
    progress_id = _create_progress_via_repo(
        canonical_id,
        foreign_org_id,
        AnnouncementProgressStatus.IN_PROGRESS,
        "지키는 row",
        other_id,
    )

    resp = logged_in_client.delete(
        f"/canonical/{canonical_id}/progress/{progress_id}"
    )
    assert resp.status_code == 403


# ── 시나리오 3: 무소속 사용자 작성/수정 시도 → 422 ─────────────────────


def test_post_progress_unaffiliated_user_returns_422(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """alice 가 어떤 조직에도 속하지 않은 상태에서 POST → 422.

    무소속 사용자는 어느 organization_id 를 줘도 422 ('본인 소속 조직이 아닙니다.').
    """
    org_id = _create_organization("팀-unaffiliated-target")
    # _add_user_to_organization 호출 안 함 → alice 는 무소속.
    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": org_id, "status": "관심", "note": ""},
    )
    assert resp.status_code == 422


def test_patch_progress_unaffiliated_user_returns_422(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """무소속 alice 가 PATCH 시도 → 422 (소속 매핑 자체가 비어 있음)."""
    with session_scope() as s:
        other = User(username="progress_seeder_unaff", password_hash="dummy")
        s.add(other)
        s.flush()
        other_id = other.id
    target_org_id = _create_organization("팀-target-unaff")
    _add_user_to_organization("progress_seeder_unaff", target_org_id)
    progress_id = _create_progress_via_repo(
        canonical_id,
        target_org_id,
        AnnouncementProgressStatus.INTEREST,
        "row",
        other_id,
    )

    resp = logged_in_client.patch(
        f"/canonical/{canonical_id}/progress/{progress_id}",
        json={"status": "검토", "note": "무소속이 시도"},
    )
    assert resp.status_code == 422


# ── 시나리오 4: 선점 충돌 → 409 ───────────────────────────────────────────


def test_post_progress_in_progress_preemption_conflict_returns_409(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """다른 조직이 이미 '진행' 인데 본인 '진행' 시도 → 409 + 한국어 안내."""
    occupier_org_id = _create_organization("팀-A-선점")
    me_org_id = _create_organization("팀-B-도전")
    _add_user_to_organization("progress_alice", me_org_id)

    # A 조직이 먼저 '진행' 으로 점유.
    with session_scope() as s:
        seeder = User(username="progress_seeder_preempt", password_hash="dummy")
        s.add(seeder)
        s.flush()
        seeder_id = seeder.id
    _add_user_to_organization("progress_seeder_preempt", occupier_org_id)
    _create_progress_via_repo(
        canonical_id,
        occupier_org_id,
        AnnouncementProgressStatus.IN_PROGRESS,
        "A 진행 중",
        seeder_id,
    )

    # alice 가 B 조직으로 '진행' 시도 → 409.
    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": me_org_id, "status": "진행", "note": ""},
    )
    assert resp.status_code == 409
    assert "팀-A-선점" in resp.json()["detail"]


def test_patch_progress_in_progress_preemption_conflict_returns_409(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """본인 조직 row 를 PATCH 로 '진행' 으로 올리는데 다른 조직이 이미 진행 → 409."""
    occupier_org_id = _create_organization("팀-occupier-patch")
    me_org_id = _create_organization("팀-mine-patch")
    _add_user_to_organization("progress_alice", me_org_id)

    # 다른 조직이 점유.
    with session_scope() as s:
        seeder = User(username="progress_seeder_preempt2", password_hash="dummy")
        s.add(seeder)
        s.flush()
        seeder_id = seeder.id
    _add_user_to_organization("progress_seeder_preempt2", occupier_org_id)
    _create_progress_via_repo(
        canonical_id,
        occupier_org_id,
        AnnouncementProgressStatus.IN_PROGRESS,
        "A 진행 중",
        seeder_id,
    )

    # alice 가 본인 조직 row 를 만든 뒤 (관심 단계) PATCH 로 진행 시도.
    initial = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": me_org_id, "status": "관심", "note": ""},
    ).json()
    resp = logged_in_client.patch(
        f"/canonical/{canonical_id}/progress/{initial['id']}",
        json={"status": "진행", "note": "올리려고 시도"},
    )
    assert resp.status_code == 409


# ── 시나리오 5: 선점 조직이 종료/하향 후 다른 조직 진행 가능 ────────────


def test_done_status_still_holds_preemption_mutex_in_routes(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """A 가 진행→종료 전환 후에도 B 의 진행 시도 → 409. task 00098: 종료도 mutex.

    '종료' 도 mutex 대상이 되었으므로 A 가 DONE 이 된 후에도 B 는 IN_PROGRESS 를 잡을 수 없다.
    """
    occupier_org_id = _create_organization("팀-A-release-mutex")
    me_org_id = _create_organization("팀-B-takeover-blocked")
    _add_user_to_organization("progress_alice", me_org_id)

    with session_scope() as s:
        seeder = User(username="progress_seeder_release_mutex", password_hash="dummy")
        s.add(seeder)
        s.flush()
        seeder_id = seeder.id
    _add_user_to_organization("progress_seeder_release_mutex", occupier_org_id)
    occupier_progress_id = _create_progress_via_repo(
        canonical_id,
        occupier_org_id,
        AnnouncementProgressStatus.IN_PROGRESS,
        "A 진행",
        seeder_id,
    )

    # 1) alice 가 B 조직 진행 시도 → 409 (A 가 IN_PROGRESS 보유).
    blocked_inprogress = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": me_org_id, "status": "진행", "note": ""},
    )
    assert blocked_inprogress.status_code == 409

    # 2) A 조직 row 를 '종료' 로 내린다.
    with session_scope() as s:
        from app.progress.repository import update_progress as _update

        _update(
            s,
            progress_id=occupier_progress_id,
            status=AnnouncementProgressStatus.DONE,
            note="A 종료",
            modifier_user_id=seeder_id,
        )

    # 3) A 가 DONE 이 된 후에도 B 의 진행 시도 → 409 (종료도 mutex 보유).
    blocked_after_done = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": me_org_id, "status": "진행", "note": "B 인수 시도"},
    )
    assert blocked_after_done.status_code == 409
    assert "팀-A-release-mutex" in blocked_after_done.json()["detail"]


# ── 시나리오 6: 양방향 롤백 — 모든 4 단계 자유 전이 ─────────────────────


def test_status_transitions_all_directions(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """진행→검토→관심→종료→검토 모든 전이를 PATCH 로 수행 가능."""
    org_id = _create_organization("팀-transitions-route")
    _add_user_to_organization("progress_alice", org_id)

    initial = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": org_id, "status": "진행", "note": "시작"},
    ).json()
    progress_id = initial["id"]

    transitions = ["검토", "관심", "종료", "검토"]
    for next_status in transitions:
        resp = logged_in_client.patch(
            f"/canonical/{canonical_id}/progress/{progress_id}",
            json={"status": next_status, "note": next_status},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == next_status

    # active row 1 건 + history 4 건 (PATCH 4 회).
    hist = logged_in_client.get(
        f"/canonical/{canonical_id}/progress/history"
    ).json()["history"]
    assert len(hist) == 4


# ── 시나리오 9: 같은 조직 동료 권한 — 작성자 무관 수정·삭제 ──────────


def test_same_organization_colleague_can_update(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """alice (조직 멤버) 가 bob 이 만든 같은 조직 row 를 PATCH 로 수정 가능."""
    org_id = _create_organization("팀-collab-route")
    _add_user_to_organization("progress_alice", org_id)

    # bob 이 같은 조직에 row 작성.
    with session_scope() as s:
        bob = User(username="progress_bob_collab", password_hash="dummy")
        s.add(bob)
        s.flush()
        bob_id = bob.id
    _add_user_to_organization("progress_bob_collab", org_id)
    progress_id = _create_progress_via_repo(
        canonical_id,
        org_id,
        AnnouncementProgressStatus.INTEREST,
        "bob 작성",
        bob_id,
    )

    # alice 가 PATCH — 200 + last_modifier 가 alice 로 갱신되어야 한다.
    resp = logged_in_client.patch(
        f"/canonical/{canonical_id}/progress/{progress_id}",
        json={"status": "검토", "note": "alice 가 수정"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "검토"
    assert body["note"] == "alice 가 수정"
    assert body["last_modifier_username"] == "progress_alice"


def test_same_organization_colleague_can_delete(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """alice (조직 멤버) 가 bob 이 만든 같은 조직 row 를 DELETE 가능."""
    org_id = _create_organization("팀-collab-delete")
    _add_user_to_organization("progress_alice", org_id)

    with session_scope() as s:
        bob = User(username="progress_bob_collab_delete", password_hash="dummy")
        s.add(bob)
        s.flush()
        bob_id = bob.id
    _add_user_to_organization("progress_bob_collab_delete", org_id)
    progress_id = _create_progress_via_repo(
        canonical_id,
        org_id,
        AnnouncementProgressStatus.REVIEW,
        "bob 작성",
        bob_id,
    )

    resp = logged_in_client.delete(
        f"/canonical/{canonical_id}/progress/{progress_id}"
    )
    assert resp.status_code == 200


# ── 입력 검증 ──────────────────────────────────────────────────────────────


def test_post_progress_invalid_status_returns_422(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """status 가 enum 도메인 밖이면 Pydantic field_validator 에서 422."""
    org_id = _create_organization("팀-invalid-status")
    _add_user_to_organization("progress_alice", org_id)

    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": org_id, "status": "잘못된값", "note": ""},
    )
    assert resp.status_code == 422


# ── 시나리오 17: N+1 회귀 — summary 헬퍼 단일 SELECT 묶음 ──────────────


def test_summary_helper_single_query_for_many_canonicals(
    test_engine: Engine,
) -> None:
    """get_progress_summary_by_canonical_id_map 가 canonical N 개 처리에 SELECT 1 회만 발행.

    설계 문서 §7.2 + Phase B get_relevance_summary_by_canonical_id_map 동일 패턴.
    라우터 자체보다는 라우터가 의존하는 summary 헬퍼의 회귀를 차단한다.
    """
    canonical_ids: list[int] = []
    organization_ids: list[int] = []
    with session_scope() as s:
        seeder = User(username="progress_summary_seeder", password_hash="dummy")
        s.add(seeder)
        s.flush()
        seeder_id = seeder.id
        for index in range(5):
            cp = CanonicalProject(
                canonical_key=f"official:summary-q-{index}",
                key_scheme="official",
            )
            s.add(cp)
            s.flush()
            canonical_ids.append(cp.id)
            org = Organization(name=f"팀-summary-{index}")
            s.add(org)
            s.flush()
            organization_ids.append(org.id)

    # canonical 마다 한 조직에 진행 row 를 심는다 (선점 충돌 회피 — canonical 별
    # 한 조직만 진행).
    for canonical_id_value, organization_id_value in zip(
        canonical_ids, organization_ids, strict=True
    ):
        _create_progress_via_repo(
            canonical_id_value,
            organization_id_value,
            AnnouncementProgressStatus.IN_PROGRESS,
            "summary seed",
            seeder_id,
        )

    # 쿼리 카운터 — engine 에 before_cursor_execute 리스너로 SELECT 만 집계.
    select_count = 0

    def _count_select(
        conn, cursor, statement, parameters, context, executemany
    ):
        """SELECT 문 카운트 — INSERT/UPDATE/DELETE 는 무시한다."""
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(test_engine, "before_cursor_execute", _count_select)
    try:
        with session_scope() as s:
            summary_map = get_progress_summary_by_canonical_id_map(
                s, user_id=None, canonical_project_ids=canonical_ids
            )
    finally:
        event.remove(test_engine, "before_cursor_execute", _count_select)

    assert len(summary_map) == 5
    # 비로그인 (user_id=None) 일 때 user_organizations 조회를 생략 → SELECT 1 회.
    assert select_count == 1, (
        f"N+1 회귀 의심 — canonical 5 개 처리에 SELECT {select_count} 회 발행됨. "
        "단일 쿼리 묶음 패턴이 깨졌는지 확인하세요."
    )


# ── task 00098: 종료 mutex 확장 라우터 시나리오 ──────────────────────────────


def test_done_org_preemption_blocks_other_in_progress_via_post(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """다른 조직이 종료 상태일 때 본인이 진행 시도 → 409.

    task 00098 시나리오: DONE 도 mutex 단계이므로 타 조직 DONE 이 있으면 IN_PROGRESS 불가.
    """
    done_org_id = _create_organization("팀-종료-선점-route")
    me_org_id = _create_organization("팀-진행-도전-route")
    _add_user_to_organization("progress_alice", me_org_id)

    with session_scope() as s:
        seeder = User(username="progress_done_preempt_seeder", password_hash="dummy")
        s.add(seeder)
        s.flush()
        seeder_id = seeder.id
    _add_user_to_organization("progress_done_preempt_seeder", done_org_id)

    # 다른 조직이 DONE 상태로 row 를 보유한다.
    _create_progress_via_repo(
        canonical_id,
        done_org_id,
        AnnouncementProgressStatus.DONE,
        "타 조직 종료",
        seeder_id,
    )

    # alice (me_org) 가 IN_PROGRESS 시도 → 409.
    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": me_org_id, "status": "진행", "note": ""},
    )
    assert resp.status_code == 409
    assert "팀-종료-선점-route" in resp.json()["detail"]


def test_in_progress_org_preemption_blocks_other_done_via_post(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """다른 조직이 진행 상태일 때 본인이 종료 시도 → 409.

    task 00098 시나리오: IN_PROGRESS 보유 조직이 있으면 타 조직의 DONE 진입도 불가.
    """
    in_progress_org_id = _create_organization("팀-진행-선점-done-route")
    me_org_id = _create_organization("팀-종료-도전-route")
    _add_user_to_organization("progress_alice", me_org_id)

    with session_scope() as s:
        seeder = User(username="progress_inprog_preempt_seeder", password_hash="dummy")
        s.add(seeder)
        s.flush()
        seeder_id = seeder.id
    _add_user_to_organization("progress_inprog_preempt_seeder", in_progress_org_id)

    # 다른 조직이 IN_PROGRESS 상태.
    _create_progress_via_repo(
        canonical_id,
        in_progress_org_id,
        AnnouncementProgressStatus.IN_PROGRESS,
        "타 조직 진행",
        seeder_id,
    )

    # alice (me_org) 가 DONE 시도 → 409.
    resp = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": me_org_id, "status": "종료", "note": ""},
    )
    assert resp.status_code == 409
    assert "팀-진행-선점-done-route" in resp.json()["detail"]


def test_done_org_does_not_block_interest_or_review_via_post(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """다른 조직이 종료 상태일 때 본인이 관심/검토 → 허용.

    task 00098 시나리오: '관심', '검토' 는 mutex 비대상 — DONE 이 있어도 진입 가능.
    """
    done_org_id = _create_organization("팀-종료-보유-route")
    me_org_id = _create_organization("팀-관심검토-진입-route")
    _add_user_to_organization("progress_alice", me_org_id)

    with session_scope() as s:
        seeder = User(username="progress_done_holder_seeder", password_hash="dummy")
        s.add(seeder)
        s.flush()
        seeder_id = seeder.id
    _add_user_to_organization("progress_done_holder_seeder", done_org_id)

    # 다른 조직이 DONE 상태 선점.
    _create_progress_via_repo(
        canonical_id,
        done_org_id,
        AnnouncementProgressStatus.DONE,
        "타 조직 종료",
        seeder_id,
    )

    # alice 가 INTEREST 로 진입 → 200 허용.
    resp_interest = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": me_org_id, "status": "관심", "note": "관심 표명"},
    )
    assert resp_interest.status_code == 200
    assert resp_interest.json()["status"] == "관심"

    # alice 가 REVIEW 로 갱신 → 200 허용.
    progress_id = resp_interest.json()["id"]
    resp_review = logged_in_client.patch(
        f"/canonical/{canonical_id}/progress/{progress_id}",
        json={"status": "검토", "note": "검토 진입"},
    )
    assert resp_review.status_code == 200
    assert resp_review.json()["status"] == "검토"


def test_get_history_returns_serialized_rows(
    logged_in_client: TestClient, canonical_id: int
) -> None:
    """GET history 응답의 각 row 가 _serialize_progress_history 형식으로 직렬화."""
    org_id = _create_organization("팀-history-route")
    _add_user_to_organization("progress_alice", org_id)

    initial = logged_in_client.post(
        f"/canonical/{canonical_id}/progress",
        json={"organization_id": org_id, "status": "관심", "note": "원본"},
    ).json()
    logged_in_client.patch(
        f"/canonical/{canonical_id}/progress/{initial['id']}",
        json={"status": "검토", "note": "갱신"},
    )

    resp = logged_in_client.get(f"/canonical/{canonical_id}/progress/history")
    assert resp.status_code == 200
    history = resp.json()["history"]
    assert len(history) == 1
    item = history[0]
    assert item["status"] == "관심"
    assert item["note"] == "원본"
    assert item["organization_id"] == org_id
    assert item["organization_name"] == "팀-history-route"
    assert item["archive_reason"] == "user_changed"
    assert item["last_modifier_username"] == "progress_alice"
    assert item["archived_at"] is not None
