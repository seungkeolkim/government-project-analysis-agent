"""M365OAuthSmtpTransport 단위 테스트 (Phase A-1 / task 00104-10).

검증 시나리오 (subtask guidance bullet 1 a/b/c):
    1. test_m365_oauth_send_success — msal mock 이 access_token 반환,
       smtplib mock 의 docmd 가 235, send_message 가 올바른 EmailMessage 로 호출,
       From 헤더가 ``f\"{display_name} <{sender_address}>\"`` 형식. SMTP 시퀀스가
       ``starttls → ehlo → docmd(AUTH) → send_message`` 순서로 호출되는지 함께
       검증한다 (RFC 3207 §4.2 — STARTTLS 후 EHLO 재전송 누락 회귀 방지, task
       00110).
    2. test_m365_oauth_token_failure — msal mock 이 ``access_token`` 키 없는
       dict 반환 시 RuntimeError. error/description 이 메시지에 포함. SMTP
       단계 도달 전에 raise 되므로 ehlo 도 호출되지 않아야 함.
    3. test_m365_oauth_auth_response_not_235 — smtp.docmd 가 535 등 반환 시
       RuntimeError. code/response 가 메시지에 포함. AUTH docmd 보다 먼저
       ehlo 가 호출되었음을 함께 검증.

외부 의존성은 monkeypatch 로 모두 차단 — msal.ConfidentialClientApplication
과 smtplib.SMTP 가 모두 가짜 객체로 치환되어 실제 네트워크 호출 0회.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest

from app.email.config import M365OAuthSmtpConfig
from app.email.transport.m365_oauth import M365OAuthSmtpTransport


def _make_config() -> M365OAuthSmtpConfig:
    """테스트용 표준 M365OAuthSmtpConfig (frozen dataclass)."""
    return M365OAuthSmtpConfig(
        tenant_id="00112233-4455-6677-8899-aabbccddeeff",
        client_id="ffeeddcc-bbaa-9988-7766-554433221100",
        client_secret="SECRET-VALUE-1234",
        sender_address="gov-agent-noreply@innodep.com",
        from_display_name="정부사업 모니터링 봇",
    )


def _install_smtp_mock(monkeypatch: pytest.MonkeyPatch, *, auth_code: int) -> MagicMock:
    """smtplib.SMTP 를 monkeypatch 해 context manager 가 가짜 SMTP 객체를 반환하게 한다.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        auth_code: ``docmd('AUTH', ...)`` 가 반환할 SMTP 응답 코드.

    Returns:
        mock SMTP 인스턴스 (``smtp_ctx``). ``send_message`` / ``docmd`` 호출 검증
        용도로 호출자가 받아 쓴다.
    """
    smtp_ctx = MagicMock(name="smtp_ctx")
    smtp_ctx.docmd.return_value = (auth_code, b"response-body")
    smtp_outer = MagicMock(name="smtp_outer")
    smtp_outer.__enter__.return_value = smtp_ctx
    smtp_outer.__exit__.return_value = False
    smtp_factory = MagicMock(name="smtp_factory", return_value=smtp_outer)
    monkeypatch.setattr("app.email.transport.m365_oauth.smtplib.SMTP", smtp_factory)
    # 호출자가 factory / outer / ctx 어느 단계든 쉽게 들여다 볼 수 있도록 ctx 만 반환.
    # 추가 attribute 로 factory 도 부착해 둔다 (검증에서 SMTP(host, port) 인자 확인 가능).
    smtp_ctx._factory_mock = smtp_factory  # type: ignore[attr-defined]
    return smtp_ctx


def _install_msal_mock(
    monkeypatch: pytest.MonkeyPatch, *, token_response: dict
) -> MagicMock:
    """msal.ConfidentialClientApplication 을 monkeypatch 한다.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        token_response: ``acquire_token_for_client`` 가 반환할 dict. 정상 응답은
            ``{\"access_token\": \"TKN\"}``, 실패는 ``{\"error\": ..., \"error_description\": ...}``.

    Returns:
        ConfidentialClientApplication 클래스 mock (생성자 호출 인자 검증용).
    """
    msal_app_instance = MagicMock(name="msal_app_instance")
    msal_app_instance.acquire_token_for_client.return_value = token_response
    msal_app_class = MagicMock(
        name="ConfidentialClientApplication", return_value=msal_app_instance
    )
    monkeypatch.setattr(
        "app.email.transport.m365_oauth.msal.ConfidentialClientApplication",
        msal_app_class,
    )
    msal_app_class._instance = msal_app_instance  # type: ignore[attr-defined]
    return msal_app_class


# ──────────────────────────────────────────────────────────────
# 1. test_m365_oauth_send_success
# ──────────────────────────────────────────────────────────────


def test_m365_oauth_send_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 시나리오: msal 토큰 발급 → smtplib STARTTLS → XOAUTH2 235 → send_message.

    검증 포인트:
        - msal.ConfidentialClientApplication 이 client_id/client_credential/
          authority 의 올바른 값으로 인스턴스화됨.
        - acquire_token_for_client 가 ``scopes=[OAUTH_SCOPE]`` 로 호출됨.
        - smtplib.SMTP 가 ('smtp.office365.com', 587) 인자로 인스턴스화됨.
        - smtp.starttls() 호출됨.
        - smtp.docmd 가 ``AUTH XOAUTH2 <b64>`` 명령으로 호출되었고, base64
          디코딩 결과가 ``user={sender}\\x01auth=Bearer {token}\\x01\\x01`` 형식.
        - smtp.send_message 가 원본 EmailMessage 로 호출됨.
        - From 헤더가 ``\"정부사업 모니터링 봇 <gov-agent-noreply@innodep.com>\"``
          으로 자동 채워짐 (사전에 비어 있었으므로).
    """
    config = _make_config()
    msal_app_class = _install_msal_mock(
        monkeypatch, token_response={"access_token": "ACC-TKN-12345"}
    )
    smtp_ctx = _install_smtp_mock(monkeypatch, auth_code=235)

    message = EmailMessage()
    message["To"] = "user@innodep.com"
    message["Subject"] = "테스트"
    message.set_content("본문")
    # From 헤더는 일부러 비워둔다 — transport 의 auto-fill 검증을 위해.

    transport = M365OAuthSmtpTransport(config)
    transport.send(message)

    # 1. msal 인스턴스화 인자 검증.
    msal_app_class.assert_called_once_with(
        client_id=config.client_id,
        client_credential=config.client_secret,
        authority=f"https://login.microsoftonline.com/{config.tenant_id}",
    )
    msal_app_class._instance.acquire_token_for_client.assert_called_once_with(
        scopes=["https://outlook.office365.com/.default"]
    )

    # 2. smtplib SMTP host/port 검증.
    smtp_ctx._factory_mock.assert_called_once_with(
        "smtp.office365.com", 587
    )
    smtp_ctx.starttls.assert_called_once_with()
    # 2-1. RFC 3207 §4.2 — STARTTLS 직후 EHLO 가 한 번 호출되어야 한다.
    # (task 00110 — 빠지면 M365 가 503 5.5.2 'Send hello first' 응답.)
    smtp_ctx.ehlo.assert_called_once_with()
    # 2-2. SMTP 시퀀스 순서 검증: starttls → ehlo → docmd → send_message.
    # method_calls 에는 mock 위에 호출된 모든 attribute 접근이 시간순으로
    # 쌓이므로, 관심 있는 4 개 메서드만 필터링해 순서를 단언한다.
    interested_method_names = {"starttls", "ehlo", "docmd", "send_message"}
    actual_sequence = [
        call[0]
        for call in smtp_ctx.method_calls
        if call[0] in interested_method_names
    ]
    assert actual_sequence == [
        "starttls",
        "ehlo",
        "docmd",
        "send_message",
    ], f"SMTP method 호출 순서가 예상과 다름: {actual_sequence!r}"

    # 3. XOAUTH2 base64 blob 검증.
    auth_call = smtp_ctx.docmd.call_args
    cmd, payload = auth_call.args
    assert cmd == "AUTH"
    assert payload.startswith("XOAUTH2 ")
    b64_string = payload[len("XOAUTH2 "):]
    decoded = base64.b64decode(b64_string).decode()
    expected = (
        f"user={config.sender_address}\x01auth=Bearer ACC-TKN-12345\x01\x01"
    )
    assert decoded == expected, f"XOAUTH2 blob mismatch: {decoded!r}"

    # 4. send_message 가 원본 EmailMessage 로 호출됨.
    smtp_ctx.send_message.assert_called_once_with(message)

    # 5. From 헤더 auto-fill — display_name <sender_address> 형식.
    expected_from = f"{config.from_display_name} <{config.sender_address}>"
    assert message["From"] == expected_from, message["From"]


# ──────────────────────────────────────────────────────────────
# 2. test_m365_oauth_token_failure
# ──────────────────────────────────────────────────────────────


def test_m365_oauth_token_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """msal 응답에 access_token 키가 없으면 RuntimeError 가 raise 된다.

    응답의 error / error_description 값이 예외 메시지에 포함되어 디버깅에
    도움이 되어야 한다.
    """
    config = _make_config()
    _install_msal_mock(
        monkeypatch,
        token_response={
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret",
        },
    )
    # SMTP 도 mock — 토큰 발급 단계에서 raise 되어 SMTP 까지는 도달하지 않아야 함.
    smtp_ctx = _install_smtp_mock(monkeypatch, auth_code=235)

    message = EmailMessage()
    message["To"] = "u@x.y"
    message["Subject"] = "s"
    message.set_content("b")
    transport = M365OAuthSmtpTransport(config)

    with pytest.raises(RuntimeError) as exc_info:
        transport.send(message)

    msg = str(exc_info.value)
    assert "M365 OAuth token 발급 실패" in msg
    assert "invalid_client" in msg
    assert "AADSTS7000215" in msg

    # SMTP 호출이 발생하지 않아야 함.
    smtp_ctx._factory_mock.assert_not_called()


# ──────────────────────────────────────────────────────────────
# 3. test_m365_oauth_auth_response_not_235
# ──────────────────────────────────────────────────────────────


def test_m365_oauth_auth_response_not_235(monkeypatch: pytest.MonkeyPatch) -> None:
    """smtp.docmd 가 235 가 아니면 RuntimeError 가 raise 된다.

    응답 코드와 응답 메시지 본문이 예외 메시지에 포함되어야 한다.
    또한 send_message 는 호출되지 않아야 함 (인증 실패 후 발송 안 함).
    """
    config = _make_config()
    _install_msal_mock(
        monkeypatch, token_response={"access_token": "ACC-TKN"}
    )
    smtp_ctx = _install_smtp_mock(monkeypatch, auth_code=535)
    # 535 응답 메시지 본문을 명시적으로 set.
    smtp_ctx.docmd.return_value = (
        535,
        b"5.7.3 Authentication unsuccessful",
    )

    message = EmailMessage()
    message["To"] = "u@x.y"
    message["Subject"] = "s"
    message.set_content("b")
    transport = M365OAuthSmtpTransport(config)

    with pytest.raises(RuntimeError) as exc_info:
        transport.send(message)

    msg = str(exc_info.value)
    assert "M365 OAuth XOAUTH2 SMTP 인증 실패" in msg
    assert "code=535" in msg
    assert "Authentication unsuccessful" in msg

    # send_message 는 호출되지 않아야 함.
    smtp_ctx.send_message.assert_not_called()

    # RFC 3207 §4.2 회귀 방지 (task 00110): AUTH 실패 케이스에서도 ehlo 가
    # docmd 보다 먼저 호출되었어야 한다. AUTH 응답 코드 검증보다 앞서
    # EHLO 가 누락되면 M365 가 503 'Send hello first' 를 돌려주는데 그
    # 분기는 본 transport 의 인증 실패 처리와 분리해 두지 않으면 디버깅이
    # 어려워진다.
    interested_method_names = {"starttls", "ehlo", "docmd"}
    actual_sequence = [
        call[0]
        for call in smtp_ctx.method_calls
        if call[0] in interested_method_names
    ]
    assert actual_sequence == [
        "starttls",
        "ehlo",
        "docmd",
    ], f"SMTP method 호출 순서가 예상과 다름: {actual_sequence!r}"
