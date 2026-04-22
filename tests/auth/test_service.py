"""app.auth.service 단위 테스트.

bcrypt 해시 연산이 라운드=12 에서 ~100ms 걸리므로 각 테스트는 해시 계산
회수를 최소화한다. 동일 비밀번호 검증은 여러 번 되풀이하지 않는다.

db_session fixture 는 ``tests/conftest.py`` 가 제공하며, 테스트별로 격리된
SQLite 파일 + Alembic HEAD 까지 upgrade 된 상태다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.constants import SESSION_LIFETIME_DAYS
from app.auth.service import (
    DuplicateUsernameError,
    PasswordPolicyError,
    UsernamePolicyError,
    authenticate,
    create_session,
    create_user,
    delete_session,
    get_active_session,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)
from app.db.models import User, UserSession


# ──────────────────────────────────────────────────────────────
# 비밀번호 해시
# ──────────────────────────────────────────────────────────────


def test_hash_password_and_verify_round_trip() -> None:
    """hash_password → verify_password 왕복이 True 를 반환한다.

    해시 문자열은 passlib bcrypt 포맷 (``$2b$`` 로 시작) 이어야 한다.
    """
    plain = "correct_horse_battery_staple"
    hashed = hash_password(plain)
    assert hashed.startswith("$2")  # passlib bcrypt 출력
    assert verify_password(plain, hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    """다른 평문으로는 검증이 실패한다."""
    hashed = hash_password("hello_world_1")
    assert verify_password("hello_world_2", hashed) is False


def test_verify_password_rejects_malformed_hash() -> None:
    """해시 문자열이 잘못되었을 때 예외 없이 False 를 반환한다."""
    assert verify_password("any", "this_is_not_a_hash") is False


# ──────────────────────────────────────────────────────────────
# 정책 검증
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  seungkeol ", "seungkeol"),  # 공백 제거 + lowercase
        ("User_42", "user_42"),
        ("abc", "abc"),  # 최소 3자
    ],
)
def test_validate_username_normalizes_and_accepts(raw: str, expected: str) -> None:
    """허용 패턴의 username 을 정규화해 반환한다."""
    assert validate_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ab",  # 3자 미만
        "with space",  # 공백 포함
        "한글계정",  # ASCII 외 문자
        "has-dash",  # 허용되지 않는 특수문자
        "",  # 빈 문자열
    ],
)
def test_validate_username_rejects_invalid(raw: str) -> None:
    """정책을 벗어난 username 은 UsernamePolicyError."""
    with pytest.raises(UsernamePolicyError):
        validate_username(raw)


def test_validate_password_rejects_short() -> None:
    """8자 미만 비밀번호는 거절."""
    with pytest.raises(PasswordPolicyError):
        validate_password("short")


def test_validate_password_accepts_min_length() -> None:
    """8자 정확히는 통과."""
    validate_password("12345678")


# ──────────────────────────────────────────────────────────────
# 사용자 생성 / 인증
# ──────────────────────────────────────────────────────────────


def _make_user(session: Session, *, username: str = "alice", password: str = "alice_password_1") -> User:
    """테스트 보조: 기본 파라미터로 사용자 한 명을 만들어 commit 한다."""
    user = create_user(session, username=username, password=password)
    session.commit()
    return user


def test_create_user_persists_row(db_session: Session) -> None:
    """create_user 는 User row 를 DB 에 남기고 password_hash 가 검증 가능하다."""
    user = _make_user(db_session, username="alice", password="alice_password_1")
    assert user.id is not None
    assert user.username == "alice"
    assert user.is_admin is False
    # 평문이 그대로 저장되지 않음을 확인
    assert user.password_hash != "alice_password_1"
    assert verify_password("alice_password_1", user.password_hash) is True


def test_create_user_duplicate_username_raises(db_session: Session) -> None:
    """동일 username 으로 두 번째 생성 시 DuplicateUsernameError."""
    _make_user(db_session, username="bob", password="bob_password_1")
    # 중복 시 IntegrityError → DuplicateUsernameError 변환.
    with pytest.raises(DuplicateUsernameError):
        create_user(db_session, username="bob", password="another_password_2")


def test_create_user_with_is_admin_true(db_session: Session) -> None:
    """is_admin=True 로 만든 사용자는 플래그가 True 로 저장된다 (create_admin CLI 경로)."""
    user = create_user(
        db_session, username="root_user", password="admin_password_1", is_admin=True
    )
    db_session.commit()
    assert user.is_admin is True


def test_authenticate_success_returns_user(db_session: Session) -> None:
    """정확한 자격증명은 User 를 반환한다."""
    created = _make_user(db_session, username="carol", password="carol_password_1")
    authenticated = authenticate(
        db_session, username="carol", password="carol_password_1"
    )
    assert authenticated is not None
    assert authenticated.id == created.id


def test_authenticate_wrong_password_returns_none(db_session: Session) -> None:
    """비밀번호 불일치 시 None."""
    _make_user(db_session, username="dave", password="dave_password_1")
    assert authenticate(db_session, username="dave", password="wrong_password_1") is None


def test_authenticate_unknown_username_returns_none(db_session: Session) -> None:
    """존재하지 않는 username 이면 None. 예외를 던지지 않는다."""
    assert authenticate(db_session, username="nosuch", password="whatever_1") is None


def test_authenticate_normalizes_username(db_session: Session) -> None:
    """대문자/공백이 섞인 입력도 정규화해서 매칭한다."""
    _make_user(db_session, username="ellen", password="ellen_password_1")
    assert (
        authenticate(db_session, username="  Ellen ", password="ellen_password_1")
        is not None
    )


# ──────────────────────────────────────────────────────────────
# 세션 발급 / 조회 / 삭제
# ──────────────────────────────────────────────────────────────


def test_create_session_issues_valid_token(db_session: Session) -> None:
    """create_session 은 UserSession row 를 남기고 만료가 now+30일 근방이다."""
    user = _make_user(db_session, username="frank", password="frank_password_1")

    fixed_now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    user_session = create_session(db_session, user, now=fixed_now)
    db_session.commit()

    assert user_session.session_id
    assert len(user_session.session_id) >= 32  # token_urlsafe(32) ≈ 43자
    assert user_session.user_id == user.id
    assert user_session.created_at == fixed_now
    assert user_session.expires_at == fixed_now + timedelta(days=SESSION_LIFETIME_DAYS)

    # DB 에 실제로 들어갔는지 재조회로 확인
    reloaded = db_session.execute(
        select(UserSession).where(UserSession.session_id == user_session.session_id)
    ).scalar_one()
    assert reloaded.user_id == user.id


def test_create_session_tokens_are_unique(db_session: Session) -> None:
    """동일 사용자 대상 2회 발급 시 서로 다른 session_id 를 반환한다."""
    user = _make_user(db_session, username="gina", password="gina_password_1")
    first = create_session(db_session, user)
    second = create_session(db_session, user)
    db_session.commit()
    assert first.session_id != second.session_id


def test_get_active_session_returns_within_lifetime(db_session: Session) -> None:
    """만료 전이면 UserSession 을 반환하고 user relationship 을 참조할 수 있다."""
    user = _make_user(db_session, username="henry", password="henry_password_1")
    issued_at = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    user_session = create_session(db_session, user, now=issued_at)
    db_session.commit()

    # 만료 직전
    check_time = issued_at + timedelta(days=SESSION_LIFETIME_DAYS - 1)
    resolved = get_active_session(db_session, user_session.session_id, now=check_time)
    assert resolved is not None
    assert resolved.user_id == user.id
    assert resolved.user.username == "henry"


def test_get_active_session_returns_none_if_expired(db_session: Session) -> None:
    """expires_at 이 현재 시각 이하면 None."""
    user = _make_user(db_session, username="iris", password="iris_password_1")
    issued_at = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    user_session = create_session(db_session, user, now=issued_at)
    db_session.commit()

    # 만료 직후
    check_time = user_session.expires_at + timedelta(seconds=1)
    assert get_active_session(db_session, user_session.session_id, now=check_time) is None


def test_get_active_session_returns_none_at_expires_at_boundary(db_session: Session) -> None:
    """만료 시각 자체도 '유효하지 않음' 으로 취급 (<=)."""
    user = _make_user(db_session, username="jack", password="jack_password_1")
    issued_at = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    user_session = create_session(db_session, user, now=issued_at)
    db_session.commit()

    assert (
        get_active_session(db_session, user_session.session_id, now=user_session.expires_at)
        is None
    )


def test_get_active_session_unknown_token(db_session: Session) -> None:
    """DB 에 없는 session_id 는 None."""
    assert get_active_session(db_session, "does-not-exist") is None


def test_get_active_session_empty_token(db_session: Session) -> None:
    """빈 문자열은 DB 조회 없이 곧바로 None."""
    assert get_active_session(db_session, "") is None


def test_delete_session_removes_row(db_session: Session) -> None:
    """delete_session 후 동일 session_id 로 조회하면 None."""
    user = _make_user(db_session, username="karen", password="karen_password_1")
    user_session = create_session(db_session, user)
    db_session.commit()

    delete_session(db_session, user_session.session_id)
    db_session.commit()

    assert (
        db_session.execute(
            select(UserSession).where(UserSession.session_id == user_session.session_id)
        ).scalar_one_or_none()
        is None
    )


def test_delete_session_unknown_token_is_noop(db_session: Session) -> None:
    """존재하지 않는 session_id 삭제는 예외 없이 no-op."""
    delete_session(db_session, "does-not-exist")
    # 커밋 까지 가도 이상 없어야 함
    db_session.commit()


def test_delete_session_does_not_affect_other_sessions(db_session: Session) -> None:
    """한 사용자가 2개 세션을 가진 상태에서 하나만 삭제해도 다른 건 남는다."""
    user = _make_user(db_session, username="liam", password="liam_password_1")
    first = create_session(db_session, user)
    second = create_session(db_session, user)
    db_session.commit()

    delete_session(db_session, first.session_id)
    db_session.commit()

    remaining = db_session.execute(
        select(UserSession).where(UserSession.user_id == user.id)
    ).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].session_id == second.session_id
