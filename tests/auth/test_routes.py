"""app.auth.routes 의 HTTP 레벨 통합 테스트.

FastAPI ``TestClient`` 로 실제 라우트를 호출해 register → login → /auth/me
→ logout 전체 흐름과 주요 실패 케이스를 검증한다. 세션 쿠키는 TestClient 가
자동으로 보관·재전송하므로 별도 수작업이 필요 없다.

bcrypt 해시 연산이 라운드=12 에서 ~100ms 걸리므로 회원가입/로그인/로그아웃
한 왕복만으로도 수백 ms 가 든다. 각 테스트는 가능한 한 사용자 1명·세션
1개만 생성한다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.auth.constants import SESSION_COOKIE_NAME


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """TestClient fixture.

    ``test_engine`` fixture 가 선행되어 격리된 SQLite + Alembic head 가 준비된
    상태에서 create_app() 으로 FastAPI 인스턴스를 만든다. TestClient 는
    테스트 종료 시 자동으로 정리된다.
    """
    # create_app 이 init_db 를 다시 호출해도 멱등이다.
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


# ──────────────────────────────────────────────────────────────
# GET 페이지 렌더링 & 비로그인 호환
# ──────────────────────────────────────────────────────────────


def test_register_page_renders_for_anonymous(client: TestClient) -> None:
    """비로그인 상태에서 /register 가 200 + HTML 을 반환한다."""
    response = client.get("/register")
    assert response.status_code == 200
    assert "회원가입" in response.text
    # 폼 action 이 올바르게 렌더되었는지 확인
    assert 'action="/auth/register"' in response.text


def test_login_page_renders_for_anonymous(client: TestClient) -> None:
    """비로그인 상태에서 /login 이 200 + HTML 을 반환한다."""
    response = client.get("/login")
    assert response.status_code == 200
    assert "로그인" in response.text
    assert 'action="/auth/login"' in response.text


def test_me_returns_null_for_anonymous(client: TestClient) -> None:
    """비로그인 /auth/me 는 {'user': null}."""
    response = client.get("/auth/me")
    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_index_renders_for_anonymous(client: TestClient) -> None:
    """비로그인이 / 를 열어도 200. 회귀 방지 — 비로그인 열람 불변식."""
    response = client.get("/")
    assert response.status_code == 200
    # 네비에 '로그인' 링크가 있어야 함
    assert "로그인" in response.text
    assert "회원가입" in response.text


# ──────────────────────────────────────────────────────────────
# 회원가입 → 로그인 → /auth/me → 로그아웃 전체 흐름
# ──────────────────────────────────────────────────────────────


def test_register_login_me_logout_flow(client: TestClient) -> None:
    """register → (자동 로그인) → /auth/me → logout 왕복.

    회원가입은 성공 시 곧바로 세션을 발급하므로, 명시적 /auth/login 없이도
    /auth/me 가 user 를 돌려준다. logout 은 쿠키를 제거하고 /auth/me 가 null
    로 돌아온다.
    """
    # 1) 회원가입 — 자동 로그인 + redirect
    register_response = client.post(
        "/auth/register",
        data={"username": "alice", "password": "alice_password_1", "email": "alice@example.com"},
        follow_redirects=False,
    )
    assert register_response.status_code == 303
    assert register_response.headers["location"] == "/"
    # 세션 쿠키가 발급됨
    assert SESSION_COOKIE_NAME in register_response.cookies

    # 2) /auth/me 로 로그인 상태 확인 — TestClient 가 쿠키 자동 보관
    me_after_register = client.get("/auth/me")
    assert me_after_register.status_code == 200
    payload = me_after_register.json()
    assert payload["user"] is not None
    assert payload["user"]["username"] == "alice"
    assert payload["user"]["email"] == "alice@example.com"
    assert payload["user"]["is_admin"] is False

    # 3) 로그인 상태에서 /login 접근 시 / 로 redirect (이미 로그인 대비)
    login_page_response = client.get("/login", follow_redirects=False)
    assert login_page_response.status_code == 303
    assert login_page_response.headers["location"] == "/"

    # 4) 로그아웃 — 쿠키 제거 + redirect
    logout_response = client.post("/auth/logout", follow_redirects=False)
    assert logout_response.status_code == 303
    assert logout_response.headers["location"] == "/"
    # delete_cookie 는 동일 이름으로 만료된 Set-Cookie 를 내려준다
    logout_set_cookie = logout_response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in logout_set_cookie

    # 5) 로그아웃 후 /auth/me 는 null
    me_after_logout = client.get("/auth/me")
    assert me_after_logout.status_code == 200
    assert me_after_logout.json() == {"user": None}


def test_login_with_existing_credentials(client: TestClient) -> None:
    """register 이후 logout 하고 다시 POST /auth/login 으로 재로그인."""
    client.post(
        "/auth/register",
        data={"username": "bob", "password": "bob_password_1"},
        follow_redirects=False,
    )
    client.post("/auth/logout", follow_redirects=False)

    # 재로그인
    login_response = client.post(
        "/auth/login",
        data={"username": "bob", "password": "bob_password_1"},
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/"
    assert SESSION_COOKIE_NAME in login_response.cookies

    me_response = client.get("/auth/me")
    assert me_response.json()["user"]["username"] == "bob"


# ──────────────────────────────────────────────────────────────
# 실패 케이스
# ──────────────────────────────────────────────────────────────


def test_register_duplicate_username_returns_400(client: TestClient) -> None:
    """동일 username 으로 재가입 시 400 + 에러 메시지를 포함한 폼 재렌더."""
    # 첫 가입
    first_response = client.post(
        "/auth/register",
        data={"username": "carol", "password": "carol_password_1"},
        follow_redirects=False,
    )
    assert first_response.status_code == 303
    # 두 번째 가입은 중복 username — 첫 세션 쿠키는 로그인 유지용으로 테스트
    # 본질과 무관하니 제거하고 시도
    client.cookies.clear()

    duplicate_response = client.post(
        "/auth/register",
        data={"username": "carol", "password": "another_password_2"},
        follow_redirects=False,
    )
    assert duplicate_response.status_code == 400
    assert "이미 사용 중인 아이디" in duplicate_response.text
    # 세션 쿠키는 발급되지 않아야 한다
    assert SESSION_COOKIE_NAME not in duplicate_response.cookies


def test_register_short_password_returns_400(client: TestClient) -> None:
    """8자 미만 비밀번호는 400 에러와 함께 폼 재렌더."""
    response = client.post(
        "/auth/register",
        data={"username": "dave", "password": "short"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "password" in response.text.lower() or "최소" in response.text


def test_register_invalid_username_returns_400(client: TestClient) -> None:
    """정책을 벗어난 username 은 400."""
    response = client.post(
        "/auth/register",
        data={"username": "has space", "password": "valid_password_1"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_login_wrong_password_returns_400(client: TestClient) -> None:
    """가입된 사용자라도 비밀번호가 틀리면 400 + 공통 에러 메시지."""
    # 가입 + 로그아웃
    client.post(
        "/auth/register",
        data={"username": "ellen", "password": "ellen_password_1"},
        follow_redirects=False,
    )
    client.post("/auth/logout", follow_redirects=False)

    response = client.post(
        "/auth/login",
        data={"username": "ellen", "password": "wrong_password_x"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    # 실패 사유를 구별하지 않는 공통 메시지
    assert "아이디 또는 비밀번호" in response.text
    # 세션 쿠키가 새로 발급되지 않았어야 함
    assert SESSION_COOKIE_NAME not in response.cookies


def test_login_unknown_username_returns_400(client: TestClient) -> None:
    """존재하지 않는 username 도 동일한 공통 에러로 400."""
    response = client.post(
        "/auth/login",
        data={"username": "no_such_user", "password": "whatever_password_1"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "아이디 또는 비밀번호" in response.text


def test_logout_when_not_logged_in_is_noop(client: TestClient) -> None:
    """비로그인 상태에서 POST /auth/logout 을 호출해도 redirect 한다 (멱등)."""
    response = client.post("/auth/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# ──────────────────────────────────────────────────────────────
# CSRF 최소 방어 (same-origin)
# ──────────────────────────────────────────────────────────────


def test_register_rejects_foreign_origin(client: TestClient) -> None:
    """외부 도메인 Origin 헤더가 붙은 POST 는 400 으로 거절."""
    response = client.post(
        "/auth/register",
        data={"username": "frank", "password": "frank_password_1"},
        headers={"Origin": "https://evil.example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_login_accepts_same_origin(client: TestClient) -> None:
    """TestClient 의 기본 host(testserver) 와 일치하는 Origin 은 허용."""
    client.post(
        "/auth/register",
        data={"username": "gina", "password": "gina_password_1"},
        follow_redirects=False,
    )
    client.post("/auth/logout", follow_redirects=False)

    response = client.post(
        "/auth/login",
        data={"username": "gina", "password": "gina_password_1"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
