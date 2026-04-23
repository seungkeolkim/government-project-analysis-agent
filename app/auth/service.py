"""인증 서비스 레이어.

비밀번호 해시/검증, 사용자 생성, 세션 발급·조회·삭제를 담는다.
모든 함수는 **호출자가 전달한 ``Session`` 을 그대로 사용**하며, 트랜잭션
경계(commit/rollback) 는 호출자가 제어한다. 본 모듈은 ``flush()`` 까지만
수행한다 — ``app.db.repository`` 와 동일한 규약.

예외 정책:
    - 입력 검증 실패는 도메인별 전용 예외(``UsernamePolicyError``,
      ``PasswordPolicyError``) 를 발생시킨다. 호출자(라우트)는 이를 잡아
      400 응답으로 변환한다.
    - ``authenticate`` 는 실패 사유를 상세히 노출하지 않고 ``None`` 을
      반환한다 (username enumeration 방지).
    - ``create_user`` 는 username UNIQUE 충돌 시 ``DuplicateUsernameError`` 를
      발생시킨다 — DB IntegrityError 를 서비스 타입 예외로 정제해 라우트가
      catch 하기 쉽게 한다.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from passlib.context import CryptContext
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.constants import (
    BCRYPT_ROUNDS,
    EMAIL_LIGHT_PATTERN,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    SESSION_LIFETIME_DAYS,
    SESSION_TOKEN_BYTES,
    USERNAME_PATTERN,
)
from app.db.models import User, UserSession, as_utc


# ──────────────────────────────────────────────────────────────
# 예외 타입
# ──────────────────────────────────────────────────────────────


class UsernamePolicyError(ValueError):
    """username 이 정책을 만족하지 못할 때 발생."""


class PasswordPolicyError(ValueError):
    """password 가 정책을 만족하지 못할 때 발생."""


class DuplicateUsernameError(ValueError):
    """이미 존재하는 username 으로 가입을 시도했을 때 발생."""


class InvalidCredentialsError(ValueError):
    """인증 실패(아이디 없음/비밀번호 불일치) 를 라우트가 공통으로 처리하고
    싶을 때 사용. ``authenticate`` 는 이 예외를 던지지 않고 ``None`` 을
    반환하지만, 상위 계층에서 "반드시 User 가 필요" 한 흐름에서 공통 변환에
    쓸 수 있도록 제공한다."""


# ──────────────────────────────────────────────────────────────
# 비밀번호 해시 컨텍스트 (passlib bcrypt)
# ──────────────────────────────────────────────────────────────


# passlib CryptContext 는 호환성/라운드 변경을 손쉽게 흡수할 수 있어 bcrypt 를
# 직접 쓰는 것보다 유지보수 측면에서 이득이다. bcrypt 만 허용하며 deprecated
# 스킴은 두지 않는다.
_password_context: CryptContext = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=BCRYPT_ROUNDS,
)


def hash_password(plain: str) -> str:
    """평문 비밀번호를 bcrypt 해시 문자열로 반환한다.

    Args:
        plain: 사용자 원문 비밀번호.

    Returns:
        passlib 포맷의 해시 문자열 (예: ``$2b$12$...``).
    """
    return _password_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """평문과 해시 문자열을 비교한다.

    해시 포맷이 잘못되었거나 passlib 이 해석할 수 없으면 ``False`` 를 반환한다
    (예외를 상위로 던지지 않는다 — 인증 플로우에서는 실패 사유를 숨긴다).

    Args:
        plain: 사용자 원문 비밀번호.
        hashed: DB 에 저장된 해시 문자열.

    Returns:
        일치하면 True, 아니면 False.
    """
    try:
        return _password_context.verify(plain, hashed)
    except (ValueError, TypeError):
        # passlib 이 해시 포맷을 해석 못 할 때 ValueError 를 던진다.
        # 잘못된 해시는 "인증 실패" 로 취급하면 충분하다.
        return False


# ──────────────────────────────────────────────────────────────
# 입력 정책 검증
# ──────────────────────────────────────────────────────────────


def validate_username(value: str) -> str:
    """username 정책을 검증하고 정규화된 값을 반환한다.

    정규화: 좌우 공백 제거 + lower(). 정책은 :data:`USERNAME_PATTERN` 참조.

    Args:
        value: 사용자 입력 username.

    Returns:
        정책을 통과한 정규화 username.

    Raises:
        UsernamePolicyError: 정책 위반.
    """
    if not isinstance(value, str):
        raise UsernamePolicyError("username 은 문자열이어야 합니다.")
    normalized = value.strip().lower()
    if not USERNAME_PATTERN.match(normalized):
        raise UsernamePolicyError(
            "username 은 소문자 영문/숫자/밑줄(_) 조합의 3~64자여야 합니다."
        )
    return normalized


def validate_password(value: str) -> None:
    """password 정책을 검증한다.

    길이 범위만 확인한다(복잡도 규칙은 강제하지 않음 — 로컬 전제). 실패 시
    ``PasswordPolicyError`` 를 던지며, 통과 시 반환값 없음.

    Args:
        value: 사용자 입력 비밀번호 평문.

    Raises:
        PasswordPolicyError: 정책 위반.
    """
    if not isinstance(value, str):
        raise PasswordPolicyError("password 는 문자열이어야 합니다.")
    if len(value) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(
            f"password 는 최소 {PASSWORD_MIN_LENGTH}자 이상이어야 합니다."
        )
    if len(value) > PASSWORD_MAX_LENGTH:
        raise PasswordPolicyError(
            f"password 는 최대 {PASSWORD_MAX_LENGTH}자까지 허용됩니다."
        )


def _normalize_optional_email(value: str | None) -> str | None:
    """이메일 값을 좌우 공백 제거 후 소문자로 정규화한다.

    비어 있으면 ``None`` 을 반환하고, 간이 패턴을 통과하지 못하면
    ``ValueError`` 를 던진다. 엄격한 RFC 검증은 수행하지 않는다(사용자 원문
    "이메일 검증 X").
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    normalized = stripped.lower()
    if not EMAIL_LIGHT_PATTERN.match(normalized):
        raise ValueError(f"이메일 형식이 올바르지 않습니다: {value!r}")
    return normalized


# ──────────────────────────────────────────────────────────────
# 사용자 생성 / 인증
# ──────────────────────────────────────────────────────────────


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    email: str | None = None,
    is_admin: bool = False,
) -> User:
    """신규 사용자를 생성해 반환한다.

    동작:
        1. username/password 를 검증·정규화.
        2. bcrypt 해시 생성.
        3. ``User`` row INSERT + ``session.flush()``.
           commit 은 호출자 책임.

    UNIQUE 충돌(동일 username 이 이미 존재) 시에는 사전 SELECT 없이 DB 의
    UNIQUE 제약에 의존한다 — race 조건에서도 정확하게 한 번만 성공한다.
    ``IntegrityError`` 는 ``DuplicateUsernameError`` 로 변환된다.

    Args:
        session: 호출자 세션.
        username: 원문 username 입력.
        password: 원문 비밀번호.
        email: 이메일 주소 (없으면 None).
        is_admin: 관리자 플래그. 일반 회원가입은 False. CLI
            (``scripts/create_admin.py``) 에서만 True 로 호출한다.

    Returns:
        새로 생성된 ``User`` 인스턴스 (flush 완료, id 할당됨).

    Raises:
        UsernamePolicyError: username 정책 위반.
        PasswordPolicyError: password 정책 위반.
        DuplicateUsernameError: username 중복.
        ValueError: email 형식 오류.
    """
    normalized_username = validate_username(username)
    validate_password(password)
    normalized_email = _normalize_optional_email(email)

    user = User(
        username=normalized_username,
        password_hash=hash_password(password),
        email=normalized_email,
        is_admin=is_admin,
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        # UNIQUE 제약 충돌 외의 에러라면 원본 예외를 그대로 올린다.
        # 이를 위해 rollback 없이 메시지로 판정한다 — IntegrityError 는 보통
        # 세션이 Invalidated 상태가 되므로 호출자가 세션 처리를 결정한다.
        if "uq_users_username" in str(exc.orig) or "UNIQUE" in str(exc.orig).upper():
            raise DuplicateUsernameError(
                f"이미 사용 중인 username 입니다: {normalized_username!r}"
            ) from exc
        raise

    logger.info("사용자 생성: id={} username={!r} is_admin={}",
                user.id, user.username, user.is_admin)
    return user


def authenticate(
    session: Session,
    *,
    username: str,
    password: str,
) -> User | None:
    """username + 평문 password 를 검증해 일치하는 사용자를 반환한다.

    실패 시 이유(존재하지 않음 / 비밀번호 불일치)를 구별해 노출하지 않는다.
    username enumeration 방지 목적. 또한 username 미존재 시에도 해시 검증을
    수행해 응답 시간 차이를 최소화한다.

    Args:
        session: 호출자 세션.
        username: 원문 username 입력 (대소문자·공백은 정규화된다).
        password: 원문 비밀번호.

    Returns:
        일치하는 ``User`` 또는 ``None``.
    """
    # username 정책 위반은 인증 실패와 동일하게 처리 — 에러 상세 노출 X.
    try:
        normalized_username = validate_username(username)
    except UsernamePolicyError:
        # 그래도 dummy 해시 검증을 한 번 수행해 timing 을 균일화한다.
        _password_context.dummy_verify()
        return None

    user = session.execute(
        select(User).where(User.username == normalized_username)
    ).scalar_one_or_none()

    if user is None:
        # username 미존재. 응답 시간을 타 사용자 조회와 비슷하게 맞춘다.
        _password_context.dummy_verify()
        return None

    if not verify_password(password, user.password_hash):
        return None

    return user


# ──────────────────────────────────────────────────────────────
# 세션 발급 / 조회 / 삭제
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _NowProvider:
    """테스트에서 monkeypatch 하기 쉽도록 now() 호출을 1개 함수에 모은다."""

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(tz=UTC)


def _resolve_now(now: datetime | None) -> datetime:
    """주입된 ``now`` 가 있으면 그대로, 없으면 현재 시각(UTC)을 반환한다."""
    if now is not None:
        return now
    return _NowProvider.utcnow()


def create_session(
    session: Session,
    user: User,
    *,
    now: datetime | None = None,
    lifetime_days: int = SESSION_LIFETIME_DAYS,
) -> UserSession:
    """사용자에게 새 세션을 발급하고 UserSession row 를 반환한다.

    ``secrets.token_urlsafe(SESSION_TOKEN_BYTES)`` 로 랜덤 토큰을 발급한다.
    ``expires_at = now + lifetime_days``.

    Args:
        session: 호출자 세션.
        user: 세션을 발급할 사용자 (반드시 flush 된 ``User.id`` 가 있어야 함).
        now: 현재 시각(UTC). 테스트에서 주입 가능. None 이면 :func:`datetime.now(UTC)`.
        lifetime_days: 세션 유효기간(일). 기본
            :data:`SESSION_LIFETIME_DAYS` (30일).

    Returns:
        flush 완료된 ``UserSession`` 인스턴스.
    """
    if user.id is None:
        raise ValueError(
            "create_session 에 전달된 user 는 flush 되어 user.id 가 할당된 상태여야 합니다."
        )

    now_ts = _resolve_now(now)
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)

    user_session = UserSession(
        session_id=token,
        user_id=user.id,
        expires_at=now_ts + timedelta(days=lifetime_days),
        created_at=now_ts,
    )
    session.add(user_session)
    session.flush()

    logger.info(
        "세션 발급: user_id={} session_id_prefix={!r} expires_at={}",
        user.id,
        token[:8],
        user_session.expires_at.isoformat(),
    )
    return user_session


def get_active_session(
    session: Session,
    session_id: str,
    *,
    now: datetime | None = None,
) -> UserSession | None:
    """아직 만료되지 않은 UserSession 을 조회해 반환한다.

    만료된(expires_at <= now) 세션이나 존재하지 않는 session_id 는 ``None``.
    만료된 row 자체를 이 함수에서 삭제하지 않는다 — 적극적 cleanup 은
    Phase 2 스케줄러가 담당한다 (단순한 read-only 경로 유지).

    Args:
        session: 호출자 세션.
        session_id: 쿠키에서 읽은 세션 토큰.
        now: 현재 시각(UTC). 테스트에서 주입 가능.

    Returns:
        유효한 ``UserSession`` 또는 ``None``.
    """
    if not session_id:
        return None

    now_ts = _resolve_now(now)
    user_session = session.execute(
        select(UserSession).where(UserSession.session_id == session_id)
    ).scalar_one_or_none()

    if user_session is None:
        return None
    # SQLite 는 DateTime(timezone=True) 의 tz 정보를 저장하지 못한다. SELECT
    # 직후 expires_at 이 naive 로 돌아오면 tz-aware now_ts 와 비교가 깨지므로
    # 양쪽 값을 모두 UTC tz-aware 로 정규화한 뒤에 비교한다.
    # as_utc 는 Phase 2 에서 scrape_runs 비교에도 재사용하기 위해 app/db/models.py
    # 공용 헬퍼로 승격됐다. auth 는 여기 하나에서만 사용한다.
    if as_utc(user_session.expires_at) <= as_utc(now_ts):
        # 만료 세션은 유효하지 않은 것으로 보고만 한다. 삭제는 하지 않는다.
        return None
    return user_session


def delete_session(session: Session, session_id: str) -> None:
    """주어진 session_id 의 UserSession row 를 삭제한다.

    로그아웃 플로우에서 호출된다. 존재하지 않는 session_id 는 no-op.
    커밋은 호출자 책임.

    Args:
        session: 호출자 세션.
        session_id: 삭제 대상 세션 토큰.
    """
    if not session_id:
        return

    result = session.execute(
        delete(UserSession).where(UserSession.session_id == session_id)
    )
    session.flush()
    deleted = int(result.rowcount or 0)
    if deleted:
        logger.info("세션 삭제: session_id_prefix={!r}", session_id[:8])


__all__ = [
    "DuplicateUsernameError",
    "InvalidCredentialsError",
    "PasswordPolicyError",
    "UsernamePolicyError",
    "authenticate",
    "create_session",
    "create_user",
    "delete_session",
    "get_active_session",
    "hash_password",
    "validate_password",
    "validate_username",
    "verify_password",
]
