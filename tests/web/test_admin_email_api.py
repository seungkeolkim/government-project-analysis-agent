"""``/api/admin/email/*`` 4 endpoint 단위 테스트 (Phase A-1 / task 00104-10).

검증 시나리오 (subtask guidance bullet 1 f/g/h):
    - test_put_settings_client_secret_blank_preserves_existing — PUT body 에
      client_secret 가 비어/누락이면 SystemSetting 기존 값 유지.
    - test_get_settings_client_secret_masked — GET 응답에서 client_secret 가
      마지막 4 자만 노출 (``\"****<last4>\"``).
    - test_non_admin_403 — 일반 사용자가 4 개 endpoint 모두 호출 시 403.

기존 ``tests/web/test_admin_backup.py`` 의 fixture 패턴을 그대로 따라
TestClient + admin_user 로그인 → 실제 HTTP 요청 → 응답 / DB 검증.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session


# ──────────────────────────────────────────────────────────────
# 공통 fixture
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """격리된 DB 가 적용된 FastAPI TestClient."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient, username: str, password: str) -> None:
    """``/auth/register`` 폼 호출. 성공 시 303 응답이어야 한다."""
    response = client.post(
        "/auth/register",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"회원가입 실패: {response.status_code}"


def _login(client: TestClient, username: str, password: str) -> None:
    """``/auth/login`` 폼 호출. 성공 시 303 응답이어야 한다."""
    response = client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"로그인 실패: {response.status_code}"


@pytest.fixture
def admin_client(client: TestClient, db_session: Session) -> TestClient:
    """관리자 (is_admin=True) 로 로그인된 TestClient.

    test_admin_backup.py 의 admin_client fixture 와 동일 패턴.
    """
    from app.auth.service import create_user

    create_user(
        db_session,
        username="email_admin",
        password="Admin_pass_1!",
        is_admin=True,
    )
    db_session.commit()
    _login(client, "email_admin", "Admin_pass_1!")
    return client


# ──────────────────────────────────────────────────────────────
# 1. test_put_settings_client_secret_blank_preserves_existing
# ──────────────────────────────────────────────────────────────


def test_put_settings_client_secret_blank_preserves_existing(
    admin_client: TestClient,
) -> None:
    """PUT body 의 client_secret 이 None / 빈 문자열이면 SystemSetting 기존 값 유지.

    1. 먼저 PUT 으로 자격증명을 한 번 저장 (client_secret 명시).
    2. 다음 PUT 에서 client_secret 키를 아예 누락 — 기존 mask 유지 확인.
    3. 또 다음 PUT 에서 client_secret 를 빈 문자열로 — 기존 mask 유지 확인.

    디자인 노트 §4-3 의 \"빈 값/누락 → 기존 값 유지\" 결정 그대로.
    """
    # 1. 최초 PUT — client_secret = \"AAAA-BBBB-CCCC-1234\"
    response = admin_client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "tenant-1",
                "client_id": "client-1",
                "client_secret": "AAAA-BBBB-CCCC-1234",
                "sender_address": "first@innodep.com",
            },
            "from_display_name": "테스트 봇",
            "max_retry_count": 2,
            "public_base_url": "http://localhost:8000",
        },
    )
    assert response.status_code == 200, response.text
    initial_mask = response.json()["m365"]["client_secret_masked"]
    assert initial_mask == "****1234", initial_mask

    # 2. client_secret 키 자체를 누락 — 기존 값 유지 (mask 동일).
    response = admin_client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "tenant-2",
                "client_id": "client-2",
                # client_secret 키 자체를 누락
                "sender_address": "second@innodep.com",
            },
            "from_display_name": "테스트 봇 2",
            "max_retry_count": 3,
            "public_base_url": "http://localhost:8000",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["m365"]["tenant_id"] == "tenant-2"  # 다른 필드는 변경됨
    assert data["m365"]["client_secret_masked"] == "****1234", (
        f"client_secret 미전달 시 기존 값 유지되어야 함; got {data!r}"
    )

    # 3. client_secret = \"\" (빈 문자열) — 기존 값 유지.
    response = admin_client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "tenant-3",
                "client_id": "client-3",
                "client_secret": "",
                "sender_address": "third@innodep.com",
            },
            "from_display_name": "테스트 봇 3",
            "max_retry_count": 4,
            "public_base_url": "http://localhost:8000",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["m365"]["client_secret_masked"] == "****1234", (
        f"client_secret 빈 문자열 시 기존 값 유지되어야 함; got {data!r}"
    )

    # 4. 명시적으로 새 값을 넣으면 변경된다는 sanity check.
    response = admin_client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "tenant-4",
                "client_id": "client-4",
                "client_secret": "NEW-SECRET-VALUE-WXYZ",
                "sender_address": "fourth@innodep.com",
            },
            "from_display_name": "테스트 봇 4",
            "max_retry_count": 5,
            "public_base_url": "http://localhost:8000",
        },
    )
    assert response.status_code == 200
    assert response.json()["m365"]["client_secret_masked"] == "****WXYZ"


# ──────────────────────────────────────────────────────────────
# 2. test_get_settings_client_secret_masked
# ──────────────────────────────────────────────────────────────


def test_get_settings_client_secret_masked(admin_client: TestClient) -> None:
    """GET /settings 응답의 client_secret_masked 가 정확히 마지막 4 자만 노출.

    디자인 노트 §4-3 의 mask 규칙:
        - 빈 값 → null
        - 1~4 자 → \"****\"
        - 5 자 이상 → \"****<last4>\"
    """
    # 1. 빈 값 (default 상태) → null
    response = admin_client.get("/api/admin/email/settings")
    assert response.status_code == 200
    assert response.json()["m365"]["client_secret_masked"] is None

    # 2. 5 자 이상 → \"****<last4>\"
    admin_client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "SuperLongSecretValue-12345678",
                "sender_address": "s@innodep.com",
            },
            "from_display_name": "x",
            "max_retry_count": 2,
            "public_base_url": "http://localhost:8000",
        },
    )
    response = admin_client.get("/api/admin/email/settings")
    assert response.status_code == 200
    mask = response.json()["m365"]["client_secret_masked"]
    # 마지막 4 자만 노출 — \"5678\" 만 그대로, 나머지는 \"****\" prefix.
    assert mask == "****5678", f"got {mask!r}"

    # 3. 평문 client_secret 이 응답에 절대 노출되지 않아야 함.
    body_text = response.text
    assert "SuperLongSecretValue" not in body_text, (
        "client_secret 평문이 응답 본문에 노출되면 안 됨"
    )

    # 4. 4 자 이하 → \"****\" (마지막 자 노출 X)
    admin_client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "abc",  # 3 자
                "sender_address": "s@innodep.com",
            },
            "from_display_name": "x",
            "max_retry_count": 2,
            "public_base_url": "http://localhost:8000",
        },
    )
    response = admin_client.get("/api/admin/email/settings")
    assert response.json()["m365"]["client_secret_masked"] == "****"


# ──────────────────────────────────────────────────────────────
# 3. test_non_admin_403
# ──────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────
# 4. test_get_settings_public_base_url_default
# ──────────────────────────────────────────────────────────────


def test_get_settings_public_base_url_default(admin_client: TestClient) -> None:
    """GET /settings 에서 public_base_url 이 default 값(localhost)으로 반환됨을 확인.

    SystemSetting 에 app.public_base_url row 가 없을 때
    DEFAULT_APP_PUBLIC_BASE_URL(='http://localhost:8000') 로 fallback 되어야 한다.
    """
    response = admin_client.get("/api/admin/email/settings")
    assert response.status_code == 200
    data = response.json()
    assert "public_base_url" in data, f"public_base_url 키가 없음: {data.keys()}"
    assert data["public_base_url"] == "http://localhost:8000", (
        f"default 값이 아님: {data['public_base_url']!r}"
    )


# ──────────────────────────────────────────────────────────────
# 5. test_put_settings_public_base_url_saved
# ──────────────────────────────────────────────────────────────


def test_put_settings_public_base_url_saved(admin_client: TestClient) -> None:
    """PUT /settings 으로 public_base_url 저장 후 GET 에서 동일 값이 반환됨을 확인.

    http/https 스킴 모두 정상 저장되어야 한다. 앞뒤 공백은 strip 되어 저장.
    """
    # http 스킴
    response = admin_client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "t",
                "client_id": "c",
                "sender_address": "s@innodep.com",
            },
            "from_display_name": "봇",
            "max_retry_count": 2,
            "public_base_url": "http://172.23.10.19:8000/",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["public_base_url"] == "http://172.23.10.19:8000/"

    # GET 으로 재확인
    get_response = admin_client.get("/api/admin/email/settings")
    assert get_response.status_code == 200
    assert get_response.json()["public_base_url"] == "http://172.23.10.19:8000/"

    # https 스킴 변경
    response = admin_client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "t",
                "client_id": "c",
                "sender_address": "s@innodep.com",
            },
            "from_display_name": "봇",
            "max_retry_count": 2,
            "public_base_url": "https://myserver.example.com/",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["public_base_url"] == "https://myserver.example.com/"


# ──────────────────────────────────────────────────────────────
# 6. test_put_settings_public_base_url_invalid_scheme
# ──────────────────────────────────────────────────────────────


def test_put_settings_public_base_url_invalid_scheme(
    admin_client: TestClient,
) -> None:
    """http/https 이외 스킴 또는 상대경로로 PUT 시 422 응답.

    Pydantic validator 가 스킴을 거부해야 한다.
    """
    invalid_values = [
        "ftp://172.23.10.19:8000/",
        "//172.23.10.19:8000/",
        "172.23.10.19:8000/",
        "",
        "   ",
    ]
    for bad_url in invalid_values:
        response = admin_client.put(
            "/api/admin/email/settings",
            json={
                "m365": {
                    "tenant_id": "t",
                    "client_id": "c",
                    "sender_address": "s@innodep.com",
                },
                "from_display_name": "봇",
                "max_retry_count": 2,
                "public_base_url": bad_url,
            },
        )
        assert response.status_code == 422, (
            f"잘못된 스킴 {bad_url!r} 에 대해 422 가 아닌 {response.status_code} 응답"
        )


# ──────────────────────────────────────────────────────────────
# 7. test_non_admin_403
# ──────────────────────────────────────────────────────────────


def test_non_admin_403(client: TestClient) -> None:
    """일반 사용자가 ``/api/admin/email/*`` 4 endpoint 모두 호출 시 403 응답.

    각 endpoint 가 라우터 레벨 ``admin_user_required`` dependency 에 의해
    공통으로 보호됨을 확인. 비로그인 401 은 별도 테스트로 분리.
    """
    # 일반 사용자 회원가입 + 로그인 (is_admin=False default).
    _register(client, "regular_user", "Regular_pass_1!")
    # _register 가 이미 자동 로그인 상태로 만들어 줄 수도 있으나, 명시적으로
    # 한 번 더 로그인 호출해 cookie 상태를 확실히 한다.
    _login(client, "regular_user", "Regular_pass_1!")

    # 4 endpoint 각각 호출 — 모두 403 이어야 함.
    response = client.get("/api/admin/email/settings", follow_redirects=False)
    assert response.status_code == 403, (
        f"GET /settings non-admin 응답 코드 {response.status_code}"
    )

    response = client.put(
        "/api/admin/email/settings",
        json={
            "m365": {
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
                "sender_address": "s@x.y",
            },
            "from_display_name": "n",
            "max_retry_count": 2,
            "public_base_url": "http://localhost:8000",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403, (
        f"PUT /settings non-admin 응답 코드 {response.status_code}"
    )

    response = client.post(
        "/api/admin/email/test-send",
        json={
            "recipient": "u@innodep.com",
            "subject": "테스트",
            "body": "본문",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403, (
        f"POST /test-send non-admin 응답 코드 {response.status_code}"
    )

    response = client.get(
        "/api/admin/email/send-runs", follow_redirects=False
    )
    assert response.status_code == 403, (
        f"GET /send-runs non-admin 응답 코드 {response.status_code}"
    )
