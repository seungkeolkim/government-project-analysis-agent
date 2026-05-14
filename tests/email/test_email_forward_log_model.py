"""EmailForwardLog ORM 모델 단위 테스트 (Phase A-2 Part 1 / task 00106-4).

검증 대상 (첨부 문서 §단위 테스트 6가지 + enum 세분화):
    1. ORM round-trip — 모든 필수 컬럼 채워서 INSERT → SELECT, 값 일치 확인.
       recipient_addresses JSON list 보존 / status enum round-trip 포함.
    2. EmailForwardStatus 3종 — success / partial / failed 모두 저장·조회 가능.
    3. FK CASCADE — canonical_project 삭제 시 EmailForwardLog 도 CASCADE 삭제.
    4. FK SET NULL (sender_user) — User 삭제 시 sender_user_id 가 NULL.
    5. FK SET NULL (sender_organization) — Organization 삭제 시 sender_organization_id 가 NULL.
    6. has_additional_message 기본값 False — 명시 생략 시 False.
    7. recipient_addresses 빈 리스트 — DB 저장 자체는 허용 (app-level 검증은 Part 2).

DB:
    tests/conftest.py 의 test_engine + db_session fixture 사용.
    각 테스트는 tmp_path 기반 고유 SQLite 파일 + Alembic upgrade head 적용 상태.

FK enforcement:
    SQLite 는 기본적으로 PRAGMA foreign_keys=OFF 이므로, CASCADE / SET NULL 동작을
    검증하는 테스트에서는 각 삭제 트랜잭션 시작 직후에
    ``session.execute(text("PRAGMA foreign_keys = ON"))`` 을 명시적으로 실행한다.
    동일 세션은 트랜잭션 내에서 하나의 커넥션을 재사용하므로 PRAGMA 가 DELETE 까지
    유효하다.

repository / service / API endpoint 에 대한 테스트는 포함하지 않는다 — Part 2 영역.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import (
    CanonicalProject,
    EmailForwardLog,
    EmailForwardStatus,
    Organization,
    User,
)


# ── 헬퍼 함수 ────────────────────────────────────────────────────────────────
# fixture 수준이 아니라 함수로 제공 — 각 테스트가 인자를 달리해 독립 생성.


def _make_canonical_project(
    session: Session,
    *,
    key_suffix: str,
) -> CanonicalProject:
    """테스트용 CanonicalProject row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        key_suffix: canonical_key 뒤에 붙는 식별 suffix (테스트 간 중복 방지).

    Returns:
        flush 된 CanonicalProject 인스턴스 (id 가 채워진 상태).
    """
    project = CanonicalProject(
        canonical_key=f"official:test-fwd-{key_suffix}",
        key_scheme="official",
    )
    session.add(project)
    session.flush()
    return project


def _make_user(session: Session, *, username: str) -> User:
    """테스트용 User row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        username: UNIQUE 제약 만족을 위한 고유 로그인 ID.

    Returns:
        flush 된 User 인스턴스 (id 가 채워진 상태).
    """
    user = User(
        username=username,
        password_hash="$hashed$placeholder",
    )
    session.add(user)
    session.flush()
    return user


def _make_organization(session: Session, *, name: str) -> Organization:
    """테스트용 루트 Organization row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        name: 조직명.

    Returns:
        flush 된 Organization 인스턴스 (id 가 채워진 상태).
    """
    org = Organization(name=name)
    session.add(org)
    session.flush()
    return org


def _make_forward_log(
    session: Session,
    *,
    project: CanonicalProject,
    status: EmailForwardStatus = EmailForwardStatus.SUCCESS,
    sender_user: User | None = None,
    sender_org: Organization | None = None,
    recipient_addresses: list[str] | None = None,
    has_additional_message: bool | None = None,
    success_count: int = 0,
    failure_count: int = 0,
) -> EmailForwardLog:
    """테스트용 EmailForwardLog row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        project: 연결될 CanonicalProject.
        status: 포워딩 결과 enum. 기본값 SUCCESS.
        sender_user: 발송 트리거 사용자 (None 이면 컬럼 미설정).
        sender_org: 발송자 조직 (None 이면 컬럼 미설정).
        recipient_addresses: 수신자 목록. None 이면 기본 2인 목록 사용.
        has_additional_message: None 이면 ORM default(False) 에 맡김.
        success_count: 성공 수신자 수.
        failure_count: 실패 수신자 수.

    Returns:
        flush 된 EmailForwardLog 인스턴스 (id 가 채워진 상태).
    """
    if recipient_addresses is None:
        recipient_addresses = ["alice@example.com", "bob@example.com"]

    kwargs: dict = dict(
        canonical_project_id=project.id,
        subject="테스트 포워딩 제목",
        recipient_addresses=recipient_addresses,
        recipient_count=len(recipient_addresses),
        status=status,
        success_count=success_count,
        failure_count=failure_count,
        created_at=datetime.now(tz=UTC),
    )
    if sender_user is not None:
        kwargs["sender_user_id"] = sender_user.id
    if sender_org is not None:
        kwargs["sender_organization_id"] = sender_org.id
    if has_additional_message is not None:
        kwargs["has_additional_message"] = has_additional_message

    log = EmailForwardLog(**kwargs)
    session.add(log)
    session.flush()
    return log


def _enable_sqlite_fk(session: Session) -> None:
    """현재 세션의 커넥션에서 SQLite FK 제약을 활성화한다.

    SQLite 기본값은 PRAGMA foreign_keys=OFF 이다. 이 함수는 DELETE 트랜잭션
    시작 직후에 호출해 동일 커넥션 내의 모든 후속 SQL 에 FK 를 적용한다.
    트랜잭션이 시작된 이후 (첫 SQL 실행 이후) 에 설정해도 해당 트랜잭션의
    FK 동작에는 영향이 없으므로 반드시 DELETE 보다 먼저 호출한다.
    """
    session.execute(text("PRAGMA foreign_keys = ON"))


# ── 테스트 1: ORM round-trip ──────────────────────────────────────────────────


def test_email_forward_log_round_trip(db_session: Session) -> None:
    """모든 필수 컬럼을 채운 EmailForwardLog INSERT 후 SELECT 시 값이 일치한다.

    특히 검증:
        - recipient_addresses JSON list 가 정확히 보존되는지.
        - status enum 이 값으로 round-trip 되는지.
        - has_additional_message, success_count, failure_count, completed_at 포함.
    """
    project = _make_canonical_project(db_session, key_suffix="rt-001")
    user = _make_user(db_session, username="fwd_rt_user")
    org = _make_organization(db_session, name="라운드트립조직")

    now = datetime.now(tz=UTC)
    recipients = ["carol@example.com", "dave@example.com", "eve@example.com"]

    log = EmailForwardLog(
        canonical_project_id=project.id,
        sender_user_id=user.id,
        sender_organization_id=org.id,
        subject="포워딩 제목 round-trip",
        has_additional_message=True,
        recipient_addresses=recipients,
        recipient_count=len(recipients),
        status=EmailForwardStatus.PARTIAL,
        success_count=2,
        failure_count=1,
        created_at=now,
        completed_at=now,
    )
    db_session.add(log)
    db_session.commit()

    log_id = log.id

    # expire 후 fresh SELECT — DB 에 실제로 저장된 값 확인.
    db_session.expire(log)
    fetched = db_session.get(EmailForwardLog, log_id)

    assert fetched is not None
    assert fetched.canonical_project_id == project.id
    assert fetched.sender_user_id == user.id
    assert fetched.sender_organization_id == org.id
    assert fetched.subject == "포워딩 제목 round-trip"
    assert fetched.has_additional_message is True
    # JSON list 가 순서·값 보존되어 반환되는지
    assert fetched.recipient_addresses == recipients
    assert fetched.recipient_count == 3
    assert fetched.status == EmailForwardStatus.PARTIAL
    assert fetched.success_count == 2
    assert fetched.failure_count == 1
    assert fetched.completed_at is not None


# ── 테스트 2: EmailForwardStatus 3종 모두 저장·조회 ────────────────────────


@pytest.mark.parametrize(
    "status",
    [
        EmailForwardStatus.SUCCESS,
        EmailForwardStatus.PARTIAL,
        EmailForwardStatus.FAILED,
    ],
    ids=["success", "partial", "failed"],
)
def test_email_forward_status_all_values(
    db_session: Session,
    status: EmailForwardStatus,
) -> None:
    """EmailForwardStatus 3 종 값 모두 DB 에 저장하고 조회 시 동일 enum 으로 반환된다.

    native_enum=False 로 DB 에는 문자열('success'/'partial'/'failed')로 저장되고,
    ORM SELECT 시 EmailForwardStatus enum 인스턴스로 역변환되어야 한다.
    """
    project = _make_canonical_project(db_session, key_suffix=f"enum-{status.value}")
    log = _make_forward_log(db_session, project=project, status=status)
    db_session.commit()

    db_session.expire(log)
    fetched = db_session.get(EmailForwardLog, log.id)

    assert fetched is not None
    assert fetched.status == status
    # native_enum=False 이므로 DB raw value 는 string — ORM 이 enum 으로 변환했는지 확인.
    assert isinstance(fetched.status, EmailForwardStatus)
    assert fetched.status.value == status.value


# ── 테스트 3: FK CASCADE — canonical_project 삭제 시 log 도 삭제 ────────────


def test_fk_cascade_canonical_project_delete(db_session: Session) -> None:
    """canonical_project 삭제 시 연결된 EmailForwardLog row 도 CASCADE 로 삭제된다.

    SQLite FK 강제를 위해 삭제 트랜잭션 시작 직후 PRAGMA foreign_keys=ON 을 실행한다.
    ORM session.delete() 가 발행하는 DELETE SQL 에 DB 레벨 CASCADE 가 적용된다.
    """
    project = _make_canonical_project(db_session, key_suffix="cascade-001")
    log = _make_forward_log(db_session, project=project)
    log_id = log.id
    project_id = project.id
    db_session.commit()

    # 삭제 트랜잭션: FK 강제 활성화 후 project 삭제.
    _enable_sqlite_fk(db_session)
    project_obj = db_session.get(CanonicalProject, project_id)
    assert project_obj is not None
    db_session.delete(project_obj)
    db_session.commit()

    # EmailForwardLog 가 CASCADE 로 삭제되었는지 확인.
    db_session.expire_all()
    assert db_session.get(EmailForwardLog, log_id) is None, (
        "canonical_project 삭제 후 email_forward_log 도 CASCADE 로 삭제되어야 함"
    )


# ── 테스트 4: FK SET NULL — sender_user 삭제 시 sender_user_id → NULL ────────


def test_fk_set_null_sender_user_delete(db_session: Session) -> None:
    """sender_user 삭제 시 EmailForwardLog.sender_user_id 가 NULL 로 전환된다.

    EmailForwardLog row 자체는 보존되며 sender_user_id 컬럼만 NULL 로 마스킹된다.
    """
    project = _make_canonical_project(db_session, key_suffix="setnull-user-001")
    user = _make_user(db_session, username="fwd_setnull_user")
    log = _make_forward_log(db_session, project=project, sender_user=user)
    log_id = log.id
    user_id = user.id
    db_session.commit()

    # 삭제 트랜잭션: FK 강제 활성화 후 user 삭제.
    _enable_sqlite_fk(db_session)
    user_obj = db_session.get(User, user_id)
    assert user_obj is not None
    db_session.delete(user_obj)
    db_session.commit()

    # EmailForwardLog row 는 유지되고 sender_user_id 는 NULL.
    db_session.expire_all()
    fetched_log = db_session.get(EmailForwardLog, log_id)

    assert fetched_log is not None, "sender_user 삭제 후 email_forward_log row 는 보존되어야 함"
    assert fetched_log.sender_user_id is None, (
        f"sender_user 삭제 후 sender_user_id 는 NULL 이어야 함; got {fetched_log.sender_user_id!r}"
    )


# ── 테스트 5: FK SET NULL — sender_organization 삭제 시 sender_organization_id → NULL ──


def test_fk_set_null_sender_organization_delete(db_session: Session) -> None:
    """sender_organization 삭제 시 EmailForwardLog.sender_organization_id 가 NULL 로 전환된다.

    EmailForwardLog row 자체는 보존되며 sender_organization_id 컬럼만 NULL 로 마스킹된다.
    """
    project = _make_canonical_project(db_session, key_suffix="setnull-org-001")
    org = _make_organization(db_session, name="삭제예정조직")
    log = _make_forward_log(db_session, project=project, sender_org=org)
    log_id = log.id
    org_id = org.id
    db_session.commit()

    # 삭제 트랜잭션: FK 강제 활성화 후 organization 삭제.
    _enable_sqlite_fk(db_session)
    org_obj = db_session.get(Organization, org_id)
    assert org_obj is not None
    db_session.delete(org_obj)
    db_session.commit()

    # EmailForwardLog row 는 유지되고 sender_organization_id 는 NULL.
    db_session.expire_all()
    fetched_log = db_session.get(EmailForwardLog, log_id)

    assert fetched_log is not None, "sender_organization 삭제 후 email_forward_log row 는 보존되어야 함"
    assert fetched_log.sender_organization_id is None, (
        "sender_organization 삭제 후 sender_organization_id 는 NULL 이어야 함; "
        f"got {fetched_log.sender_organization_id!r}"
    )


# ── 테스트 6: has_additional_message 기본값 False ────────────────────────────


def test_has_additional_message_default_false(db_session: Session) -> None:
    """has_additional_message 를 명시하지 않으면 ORM default 인 False 가 적용된다."""
    project = _make_canonical_project(db_session, key_suffix="default-bool-001")
    # has_additional_message 인자를 전달하지 않음 → ORM default=False 에 맡김.
    log = _make_forward_log(db_session, project=project)
    db_session.commit()

    db_session.expire(log)
    fetched = db_session.get(EmailForwardLog, log.id)

    assert fetched is not None
    assert fetched.has_additional_message is False, (
        f"has_additional_message 기본값은 False 이어야 함; got {fetched.has_additional_message!r}"
    )


# ── 테스트 7: recipient_addresses 빈 리스트 허용 ─────────────────────────────


def test_recipient_addresses_empty_list_allowed(db_session: Session) -> None:
    """recipient_addresses 에 빈 리스트를 저장해도 DB 차원에서는 허용된다.

    NOT NULL 제약은 [] (빈 리스트) 를 거부하지 않는다 (NULL 이 아님).
    빈 리스트 비허용 검증은 app-level (Part 2) 의 책임이다.
    """
    project = _make_canonical_project(db_session, key_suffix="empty-list-001")
    log = _make_forward_log(
        db_session,
        project=project,
        recipient_addresses=[],  # 빈 리스트
    )
    db_session.commit()

    db_session.expire(log)
    fetched = db_session.get(EmailForwardLog, log.id)

    assert fetched is not None
    assert fetched.recipient_addresses == [], (
        f"빈 리스트가 그대로 보존되어야 함; got {fetched.recipient_addresses!r}"
    )
    assert fetched.recipient_count == 0
