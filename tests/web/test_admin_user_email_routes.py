"""POST /admin/users/{id}/email 및 /admin/users/{id}/email-subscription 통합 테스트.

FastAPI TestClient 로 admin 로그인 → 엔드포인트 호출 → 별도 DB 세션으로 재조회해
변경이 실제로 commit 됐는지 검증한다.

검증 시나리오:
    - 이메일 변경: 유효한 이메일이 DB 에 반영된다.
    - 이메일 제거: 빈 문자열로 POST 하면 User.email 이 None 으로 저장된다.
    - 이메일 형식 오류: 잘못된 형식은 거부되고 DB 값이 유지된다.
    - 이메일 수신 토글: True → False, False → True 모두 DB 에 반영된다.
    - 비admin 접근 차단: 일반 사용자는 403 응답을 받는다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.auth.constants import SESSION_COOKIE_NAME
from app.db.models import User
from app.db.session import SessionLocal


# ──────────────────────────────────────────────────────────────
# fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """격리된 SQLite + Alembic head 준비 후 TestClient를 제공한다."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def db_verify(test_engine: Engine) -> Iterator[Session]:
    """HTTP 요청 완료 후 DB 상태를 직접 확인하기 위한 별도 세션."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────
# 보조 함수
# ──────────────────────────────────────────────────────────────


def _register(client: TestClient, username: str, password: str) -> None:
    """사용자를 등록한다.

    Args:
        client:   TestClient 인스턴스.
        username: 등록할 username.
        password: 등록할 비밀번호.
    """
    response = client.post(
        "/auth/register",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"회원가입 실패: {response.status_code}"


def _login(client: TestClient, username: str, password: str) -> None:
    """기존 계정으로 로그인한다.

    Args:
        client:   TestClient 인스턴스.
        username: 로그인할 username.
        password: 로그인할 비밀번호.
    """
    response = client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"로그인 실패: {response.status_code}"


@pytest.fixture
def admin_client(client: TestClient, db_session: Session) -> TestClient:
    """admin 계정으로 로그인된 TestClient.

    admin_user를 직접 DB에 생성하고 로그인한다.

    Args:
        client:     공용 TestClient.
        db_session: DB 직접 조작용 세션 (admin 계정 생성에 사용).
    """
    from app.auth.service import create_user

    create_user(
        db_session,
        username="test_admin",
        password="Admin_pass_1!",
        is_admin=True,
    )
    db_session.commit()
    _login(client, "test_admin", "Admin_pass_1!")
    return client


@pytest.fixture
def target_user_id(db_session: Session) -> int:
    """테스트 대상 일반 사용자를 DB에 생성하고 PK를 반환한다.

    Args:
        db_session: DB 직접 조작용 세션.
    """
    from app.auth.service import create_user

    user = create_user(
        db_session,
        username="target_user",
        password="Target_pass_1!",
        email="original@example.com",
    )
    db_session.commit()
    return user.id


# ──────────────────────────────────────────────────────────────
# POST /admin/users/{id}/email
# ──────────────────────────────────────────────────────────────


def test_admin_set_email_persists_to_db(
    admin_client: TestClient,
    db_verify: Session,
    target_user_id: int,
) -> None:
    """유효한 이메일 변경 요청이 DB에 반영되어야 한다."""
    response = admin_client.post(
        f"/admin/users/{target_user_id}/email",
        data={"new_email": "new@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    user = db_verify.execute(
        select(User).where(User.id == target_user_id)
    ).scalar_one()
    assert user.email == "new@example.com"


def test_admin_set_email_blank_removes_email(
    admin_client: TestClient,
    db_verify: Session,
    target_user_id: int,
) -> None:
    """빈 문자열로 POST하면 User.email 이 None 으로 저장되어야 한다."""
    response = admin_client.post(
        f"/admin/users/{target_user_id}/email",
        data={"new_email": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303

    user = db_verify.execute(
        select(User).where(User.id == target_user_id)
    ).scalar_one()
    assert user.email is None


def test_admin_set_email_invalid_format_rejected(
    admin_client: TestClient,
    db_verify: Session,
    target_user_id: int,
) -> None:
    """잘못된 이메일 형식은 변경이 거부되고 DB 값이 원래대로 유지되어야 한다."""
    response = admin_client.post(
        f"/admin/users/{target_user_id}/email",
        data={"new_email": "not-an-email"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # flash error redirect → /admin/users?flash=...&flash_level=error
    assert "error" in response.headers["location"]

    user = db_verify.execute(
        select(User).where(User.id == target_user_id)
    ).scalar_one()
    # 원본 이메일 유지
    assert user.email == "original@example.com"


# ──────────────────────────────────────────────────────────────
# POST /admin/users/{id}/email-subscription
# ──────────────────────────────────────────────────────────────


def test_admin_set_email_subscription_true(
    admin_client: TestClient,
    db_verify: Session,
    target_user_id: int,
    db_session: Session,
) -> None:
    """이메일 수신 여부를 True 로 설정하면 DB에 반영되어야 한다."""
    # target_user 를 먼저 수신 거부로 초기화
    from app.auth.service import change_email_subscribed

    user_obj = db_session.get(User, target_user_id)
    change_email_subscribed(db_session, user_obj, subscribed=False)
    db_session.commit()

    response = admin_client.post(
        f"/admin/users/{target_user_id}/email-subscription",
        data={"subscribed": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_verify.expire_all()
    user = db_verify.execute(
        select(User).where(User.id == target_user_id)
    ).scalar_one()
    assert user.email_subscribed is True


def test_admin_set_email_subscription_false(
    admin_client: TestClient,
    db_verify: Session,
    target_user_id: int,
    db_session: Session,
) -> None:
    """이메일 수신 여부를 False 로 설정(체크박스 미체크)하면 DB에 반영되어야 한다."""
    # target_user 를 먼저 수신 동의로 초기화
    from app.auth.service import change_email_subscribed

    user_obj = db_session.get(User, target_user_id)
    change_email_subscribed(db_session, user_obj, subscribed=True)
    db_session.commit()

    # 체크박스 미체크 → subscribed 필드 전송 안 함 (default=False)
    response = admin_client.post(
        f"/admin/users/{target_user_id}/email-subscription",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_verify.expire_all()
    user = db_verify.execute(
        select(User).where(User.id == target_user_id)
    ).scalar_one()
    assert user.email_subscribed is False


# ──────────────────────────────────────────────────────────────
# 비admin 접근 차단
# ──────────────────────────────────────────────────────────────


def test_non_admin_email_change_blocked(
    client: TestClient,
    target_user_id: int,
) -> None:
    """admin 이 아닌 사용자가 이메일 변경 엔드포인트에 접근하면 차단되어야 한다."""
    _register(client, "plain_user", "Plain_pass_1!")

    response = client.post(
        f"/admin/users/{target_user_id}/email",
        data={"new_email": "hacked@example.com"},
        follow_redirects=False,
    )
    assert response.status_code in (401, 403)


def test_non_admin_email_subscription_blocked(
    client: TestClient,
    target_user_id: int,
) -> None:
    """admin 이 아닌 사용자가 이메일 수신 여부 변경 엔드포인트에 접근하면 차단되어야 한다."""
    _register(client, "plain_user2", "Plain_pass_1!")

    response = client.post(
        f"/admin/users/{target_user_id}/email-subscription",
        data={"subscribed": "true"},
        follow_redirects=False,
    )
    assert response.status_code in (401, 403)
