"""POST /settings/* 엔드포인트 DB 영속화 통합 테스트.

FastAPI TestClient 로 로그인 → POST /settings/{email,notification,password,organizations}
→ 별도 DB 세션으로 재조회해 변경이 실제로 commit 됐는지 검증한다.

회귀 방지 목적: auth 세션과 settings 세션이 분리되어 있을 때 세션 mismatch 로
변경이 DB에 반영되지 않는 버그(task 00080)를 커버한다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.auth.constants import SESSION_COOKIE_NAME
from app.db.models import Organization, User
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
    """HTTP 요청 완료 후 DB 상태를 직접 확인하기 위한 별도 세션.

    test_engine과 같은 SQLite 파일을 바라보므로, TestClient가 commit한
    변경을 이 세션으로 재조회할 수 있다.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────
# 테스트 보조
# ──────────────────────────────────────────────────────────────


def _register_and_login(
    client: TestClient,
    username: str,
    password: str,
    email: str = "",
) -> None:
    """사용자를 등록하고 TestClient 쿠키에 세션을 남긴다.

    /auth/register 는 성공 시 세션을 발급하므로 별도 로그인 단계가 불필요하다.

    Args:
        client: TestClient 인스턴스 (쿠키 저장 목적).
        username: 등록할 username.
        password: 등록할 비밀번호.
        email: 등록할 이메일 주소 (빈 문자열이면 생략).
    """
    data: dict[str, str] = {"username": username, "password": password}
    if email:
        data["email"] = email
    response = client.post("/auth/register", data=data, follow_redirects=False)
    assert response.status_code == 303, f"회원가입 실패: {response.status_code}"


# ──────────────────────────────────────────────────────────────
# POST /settings/email
# ──────────────────────────────────────────────────────────────


def test_email_change_persists_to_db(client: TestClient, db_verify: Session) -> None:
    """이메일 변경 후 DB 재조회 시 새 이메일이 저장돼야 한다."""
    _register_and_login(client, "alice", "alice_password_1")

    response = client.post(
        "/settings/email",
        data={"new_email": "alice@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    user = db_verify.execute(
        select(User).where(User.username == "alice")
    ).scalar_one()
    assert user.email == "alice@example.com"


def test_email_change_to_empty_removes_email(client: TestClient, db_verify: Session) -> None:
    """빈 문자열 이메일 제출 시 DB에 None으로 저장돼야 한다."""
    _register_and_login(client, "bob", "bob_password_1", email="bob@example.com")

    response = client.post(
        "/settings/email",
        data={"new_email": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303

    # db_verify 세션이 먼저 bob을 로드했을 수 있으므로 identity map을 비운다.
    db_verify.expire_all()
    user = db_verify.execute(
        select(User).where(User.username == "bob")
    ).scalar_one()
    assert user.email is None


def test_email_change_invalid_format_returns_400(client: TestClient) -> None:
    """잘못된 이메일 형식은 400을 반환해야 한다."""
    _register_and_login(client, "carol", "carol_password_1")

    response = client.post(
        "/settings/email",
        data={"new_email": "not-an-email"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "이메일" in response.text


# ──────────────────────────────────────────────────────────────
# POST /settings/notification
# ──────────────────────────────────────────────────────────────


def test_notification_subscribe_persists_to_db(client: TestClient, db_verify: Session) -> None:
    """email_subscribed=True 변경이 DB에 반영돼야 한다."""
    _register_and_login(client, "dave", "dave_password_1")

    response = client.post(
        "/settings/notification",
        data={"email_subscribed": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    user = db_verify.execute(
        select(User).where(User.username == "dave")
    ).scalar_one()
    assert user.email_subscribed is True


def test_notification_unsubscribe_persists_to_db(client: TestClient, db_verify: Session) -> None:
    """email_subscribed=False(미체크) 변경이 DB에 반영돼야 한다."""
    _register_and_login(client, "eve", "eve_password_1")

    # 먼저 True로 설정
    client.post(
        "/settings/notification",
        data={"email_subscribed": "1"},
        follow_redirects=False,
    )

    # 체크박스 미선택 → 필드 없음 → False
    response = client.post(
        "/settings/notification",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_verify.expire_all()
    user = db_verify.execute(
        select(User).where(User.username == "eve")
    ).scalar_one()
    assert user.email_subscribed is False


# ──────────────────────────────────────────────────────────────
# POST /settings/password
# ──────────────────────────────────────────────────────────────


def test_password_change_persists_to_db(client: TestClient) -> None:
    """비밀번호 변경 후 새 비밀번호로 로그인 가능하고, 기존 비밀번호로는 불가여야 한다."""
    _register_and_login(client, "frank", "frank_password_1")

    response = client.post(
        "/settings/password",
        data={
            "current_password": "frank_password_1",
            "new_password": "frank_new_password_2",
            "confirm_password": "frank_new_password_2",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    # 쿠키 초기화 후 재로그인 시도
    client.cookies.clear()

    # 기존 비밀번호로 로그인 불가
    old_login = client.post(
        "/auth/login",
        data={"username": "frank", "password": "frank_password_1"},
        follow_redirects=False,
    )
    assert old_login.status_code == 400

    # 새 비밀번호로 로그인 가능
    new_login = client.post(
        "/auth/login",
        data={"username": "frank", "password": "frank_new_password_2"},
        follow_redirects=False,
    )
    assert new_login.status_code == 303
    assert SESSION_COOKIE_NAME in new_login.cookies


def test_password_change_wrong_current_password_returns_400(client: TestClient) -> None:
    """현재 비밀번호가 틀리면 400을 반환해야 한다."""
    _register_and_login(client, "gina", "gina_password_1")

    response = client.post(
        "/settings/password",
        data={
            "current_password": "wrong_password_x",
            "new_password": "gina_new_password_2",
            "confirm_password": "gina_new_password_2",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_password_change_mismatched_confirm_returns_400(client: TestClient) -> None:
    """새 비밀번호와 확인 비밀번호가 다르면 400을 반환해야 한다."""
    _register_and_login(client, "henry", "henry_password_1")

    response = client.post(
        "/settings/password",
        data={
            "current_password": "henry_password_1",
            "new_password": "henry_new_password_2",
            "confirm_password": "henry_different_password_3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


# ──────────────────────────────────────────────────────────────
# POST /settings/organizations
# ──────────────────────────────────────────────────────────────


def test_organizations_change_persists_to_db(client: TestClient, db_verify: Session) -> None:
    """조직 소속 변경이 DB에 반영돼야 한다."""
    # 테스트용 조직 레코드를 DB에 직접 생성한다.
    org = Organization(name="테스트 조직")
    db_verify.add(org)
    db_verify.commit()
    org_id: int = org.id  # type: ignore[assignment]

    _register_and_login(client, "iris", "iris_password_1")

    response = client.post(
        "/settings/organizations",
        data={"organization_ids": str(org_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_verify.expire_all()
    user = db_verify.execute(
        select(User).where(User.username == "iris")
    ).scalar_one()
    user_org_ids = {uo.organization_id for uo in user.user_organizations}
    assert org_id in user_org_ids
