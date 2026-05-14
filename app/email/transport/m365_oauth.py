"""M365 OAuth XOAUTH2 SMTP transport 구현 (옵션 B / task 00104-5).

설계 근거:
    docs/phase_a1_design_note.md §6 (msal 사용 정책) + §4-5 (transport_type
    확장 여유) + §11 (옵션 A 코드 금지) + 첨부 phase_a1_prompt.md 의
    ``app/email/transport/m365_oauth.py`` 섹션.

동작 흐름 (send 메서드 1회 호출당):
    1. ``msal.ConfidentialClientApplication`` 인스턴스를 **그 호출 한정으로**
       새로 만든다 (디자인 노트 §6-3 — 사용자가 관리자 페이지에서 자격증명을
       바꾸면 다음 발송부터 즉시 반영되도록). 토큰 캐시 효과는 잃지만 A-1
       단계의 운영 빈도(테스트 발송 + A-3 daily report) 에서 영향은 작다.
    2. ``acquire_token_for_client(scopes=[\"https://outlook.office365.com/.default\"])``
       로 client credentials flow 액세스 토큰을 발급. 응답 dict 에 ``access_token``
       키가 없으면 RuntimeError (응답의 ``error`` / ``error_description`` 포함).
    3. ``smtplib.SMTP(\"smtp.office365.com\", 587)`` 컨텍스트로 연결을 열고
       ``starttls()`` 로 TLS 업그레이드.
    4. RFC 4954 + Google·M365 XOAUTH2 스펙대로 ``user=...\\x01auth=Bearer
       ...\\x01\\x01`` 문자열을 base64 로 인코딩해 ``AUTH XOAUTH2 <b64>`` 명령
       을 raw ``docmd`` 로 전송. 응답 코드가 235 가 아니면 RuntimeError.
    5. EmailMessage 의 ``From`` 헤더가 비어 있으면
       ``f\"{from_display_name} <{sender_address}>\"`` 형식으로 채운다. 이미 값이
       있으면 그대로 보존.
    6. ``smtp.send_message(message)`` 로 발송.

토큰 캐싱:
    MSAL 의 내부 in-memory cache 만 사용한다 (디자인 노트 §6-1, 외부 영속
    저장 금지). 본 transport 가 매 send() 마다 새 ConfidentialClientApplication
    을 만들기 때문에 실질적으로 캐시가 활용되지 않지만, 명시적으로 외부 저장
    경로를 추가하지 않는다는 결정을 본 docstring 에 박는다.

옵션 A / 환경변수:
    구 A-0 의 port 25 + IP 기반 inbound connector 경로 및 환경변수 자격증명
    경로는 **본 모듈에 일체 포함하지 않는다** (디자인 노트 §11, 첨부 문서
    \"범위 밖\" 섹션). 모든 설정은 SystemSetting 으로만 흐른다.

예외 전파 정책:
    msal / smtplib 가 던지는 예외(RuntimeError 외에 OSError, smtplib.SMTPException
    등) 는 catch 하지 않고 그대로 호출자(sender.py 의 send_with_retry) 로
    전파한다. 재시도 정책과 EmailSendRun row 기록은 호출자 책임 (디자인 노트
    §4-4).

로깅:
    각 단계 시작/완료/실패를 module-level loguru logger 로 기록한다.
    민감 정보 보호:
        - ``access_token`` 은 로그에 절대 출력하지 않는다.
        - ``client_secret`` 은 로그에 절대 출력하지 않는다 (config 객체 전체
          를 직접 로그에 찍지 않는다).
        - ``tenant_id`` / ``client_id`` 는 UUID-like 이라 디버깅 용도로 prefix
          (앞 8자) 만 출력한다 (디자인 노트 §8).
"""

from __future__ import annotations

import base64
import smtplib
from email.message import EmailMessage

import msal
from loguru import logger

from app.email.config import M365OAuthSmtpConfig
from app.email.transport.base import EmailTransport


# ──────────────────────────────────────────────────────────────
# 옵션 B 정의 자체에 묶인 코드 상수
# ──────────────────────────────────────────────────────────────
# 본 5 개 상수는 SystemSetting 으로 노출하지 않는다 — \"옵션 B = M365 OAuth
# XOAUTH2 SMTP\" 라는 transport 종류 자체의 정의에 묶인 값이라, 변경되는 순간
# 사실상 다른 transport 가 된다. SystemSetting 으로 들어가는 변동 값은
# tenant_id / client_id / client_secret / sender_address / from_display_name
# 5 개 뿐이며, 본 코드 상수들은 그와 별개이다 (config.py docstring 참조).


# Microsoft 365 SMTP relay 호스트. RFC 8314 권장 STARTTLS 587 포트 사용.
SMTP_HOST: str = "smtp.office365.com"
SMTP_PORT: int = 587

# Client credentials flow 의 scope. ``/.default`` 는 \"app 등록 시 부여된 모든
# scope 의 합\" 을 의미 — M365 의 SMTP.SendAsApp 권한이 이 scope 하나로 커버됨.
OAUTH_SCOPE: str = "https://outlook.office365.com/.default"

# MSAL authority URL 템플릿. tenant_id 를 format 으로 치환해서 사용.
AUTHORITY_TEMPLATE: str = "https://login.microsoftonline.com/{tenant_id}"

# SMTP AUTH 명령의 성공 응답 코드 (RFC 4954). 235 이외 응답은 모두 실패로 간주.
SMTP_AUTH_SUCCESS_CODE: int = 235


# ──────────────────────────────────────────────────────────────
# Transport 구현
# ──────────────────────────────────────────────────────────────


class M365OAuthSmtpTransport(EmailTransport):
    """Microsoft 365 OAuth XOAUTH2 SMTP 발송 transport (옵션 B).

    EmailTransport 인터페이스의 현재 유일한 구현체. msal 의 client credentials
    flow 로 access token 을 받아 SMTP AUTH XOAUTH2 명령에 사용한다. 외부
    네트워크 의존성 (login.microsoftonline.com + smtp.office365.com) 이 있어,
    단위 테스트는 msal / smtplib 를 monkeypatch 한 환경에서 돌린다
    (subtask 00104-10 의 책임 영역).

    인스턴스 라이프사이클:
        ``__init__`` 에서 설정만 보관하고, 실제 외부 호출은 ``send()`` 시점에
        모두 일어난다. transport 인스턴스 자체는 가벼우며 한 번 만들어 여러
        send() 에 재사용해도 무방하지만, 본 task 의 sender (subtask 00104-8)
        는 매 발송마다 factory 를 통해 새로 만든다 (SystemSetting 변경 즉시
        반영을 위해).

    재시도 / 이력 기록:
        본 transport 는 한 번의 SMTP 발송 시도만 수행한다. 재시도 정책과
        EmailSendRun row 기록은 상위 layer (``send_with_retry``) 가 담당한다
        (디자인 노트 §4-4).
    """

    def __init__(self, config: M365OAuthSmtpConfig) -> None:
        """주어진 설정으로 transport 를 초기화한다.

        Args:
            config: 5 개 자격증명/메타 값을 담은 frozen
                ``M365OAuthSmtpConfig``. 빈 문자열 자격증명이 들어와도 본
                생성자는 검증하지 않는다 — 실제 자격증명 부재는 ``send()``
                안의 msal 응답에서 ``error='invalid_client'`` 등으로 드러난다.
        """
        self._config = config

    def send(self, message: EmailMessage) -> None:
        """EmailMessage 를 M365 OAuth XOAUTH2 SMTP 로 1회 발송한다.

        실패 시 RuntimeError 또는 msal/smtplib 가 던지는 예외 (OSError /
        smtplib.SMTPException 등) 를 그대로 호출자에게 전파한다. 재시도는
        호출자 (``send_with_retry``) 가 결정한다.

        Args:
            message: 발송할 ``EmailMessage``. From 헤더가 비어 있으면 본
                메서드가 SystemSetting 의 ``email.from_display_name`` +
                ``email.m365.sender_address`` 조합으로 채운 뒤 발송한다.

        Raises:
            RuntimeError: 토큰 발급 응답에 ``access_token`` 키가 없거나
                SMTP AUTH XOAUTH2 응답 코드가 235 가 아닐 때.
            Exception: msal / smtplib 가 던지는 모든 예외가 그대로 전파됨.
        """
        access_token = self._acquire_access_token()
        self._fill_from_header_if_empty(message)

        logger.debug(
            "M365 OAuth SMTP 연결 시작: host={} port={} sender_address={}",
            SMTP_HOST,
            SMTP_PORT,
            self._config.sender_address,
        )

        # smtplib.SMTP 는 context manager 를 지원 — with 종료 시 quit() 호출.
        # 예외 발생 시에도 cleanup 보장.
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            logger.debug("STARTTLS 완료. XOAUTH2 AUTH 명령 송신.")

            xoauth2_b64 = self._build_xoauth2_blob(
                self._config.sender_address, access_token
            )
            # docmd 는 (code, response_bytes) tuple 을 반환한다.
            # 응답 코드 235 만 성공으로 간주. 334 (continuation/challenge) 등
            # 다른 코드도 본 transport 가 처리하지 않으며 — 즉시 RuntimeError
            # 로 실패시킨다. with 컨텍스트가 connection 을 quit 으로 정리한다.
            code, response = smtp.docmd("AUTH", f"XOAUTH2 {xoauth2_b64}")
            if code != SMTP_AUTH_SUCCESS_CODE:
                # response 는 bytes — 디코드는 errors='replace' 로 안전하게.
                response_text = (
                    response.decode("utf-8", errors="replace")
                    if isinstance(response, (bytes, bytearray))
                    else str(response)
                )
                raise RuntimeError(
                    "M365 OAuth XOAUTH2 SMTP 인증 실패: "
                    f"code={code} response={response_text!r}"
                )
            logger.debug("XOAUTH2 인증 성공 (code={}).", code)

            smtp.send_message(message)
            logger.info(
                "M365 OAuth SMTP 발송 완료: recipient={} subject={!r}",
                message["To"],
                message["Subject"],
            )

    # ──────────────────────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────────────────────

    def _acquire_access_token(self) -> str:
        """MSAL client credentials flow 로 access token 을 발급한다.

        매 호출마다 새 ``ConfidentialClientApplication`` 인스턴스를 만들어,
        사용자가 관리자 페이지에서 자격증명을 바꿨을 때 다음 발송부터 즉시
        반영되도록 한다 (디자인 노트 §6-3). 토큰 자체의 in-memory cache 는
        msal 이 자동으로 처리하지만, 인스턴스가 새로 만들어지면 캐시도
        새로 초기화된다 — A-1 단계 운영 빈도에서는 무시 가능한 비용.

        Returns:
            발급된 access token 문자열. 로그에는 절대 노출하지 않는다.

        Raises:
            RuntimeError: 응답에 ``access_token`` 키가 없을 때. 응답의
                ``error`` / ``error_description`` 을 메시지에 포함한다.
        """
        config = self._config
        # tenant_id / client_id 는 UUID-like — prefix 만 로그에 노출 (디자인 노트 §8).
        tenant_id_prefix = (
            f"{config.tenant_id[:8]}..."
            if len(config.tenant_id) > 8
            else (config.tenant_id or "<empty>")
        )
        client_id_prefix = (
            f"{config.client_id[:8]}..."
            if len(config.client_id) > 8
            else (config.client_id or "<empty>")
        )
        logger.debug(
            "M365 OAuth 토큰 발급 시작: tenant_id_prefix={} client_id_prefix={}",
            tenant_id_prefix,
            client_id_prefix,
        )

        msal_app = msal.ConfidentialClientApplication(
            client_id=config.client_id,
            client_credential=config.client_secret,
            authority=AUTHORITY_TEMPLATE.format(tenant_id=config.tenant_id),
        )
        result = msal_app.acquire_token_for_client(scopes=[OAUTH_SCOPE])

        # MSAL 은 통상 dict 를 반환하지만, 방어적으로 타입 체크.
        # 성공 시 'access_token' 키, 실패 시 'error' / 'error_description' 키.
        if not isinstance(result, dict) or "access_token" not in result:
            if isinstance(result, dict):
                error = result.get("error")
                description = result.get("error_description")
            else:
                error = "non_dict_response"
                description = repr(result)
            # logger.error 는 traceback 을 첨부하지 않으므로 raise 후 sender
            # 의 logger.warning/error 가 traceback 까지 기록하게 둔다.
            raise RuntimeError(
                "M365 OAuth token 발급 실패: "
                f"error={error!r} description={description!r}"
            )

        logger.debug("M365 OAuth 토큰 발급 성공.")
        return result["access_token"]

    def _fill_from_header_if_empty(self, message: EmailMessage) -> None:
        """``message['From']`` 이 비어 있으면 SystemSetting 값으로 채운다.

        이미 비어 있지 않은 값이 있으면 그대로 보존한다. 빈 문자열 헤더가
        존재하는 경우 (드물지만 호출자가 잘못 설정) 중복 헤더 생성을 피하기
        위해 명시적으로 ``del`` 한 뒤 set 한다. ``email.message.Message`` 의
        ``del`` 는 헤더가 없어도 silent.

        Args:
            message: From 헤더를 보정할 EmailMessage. 인자 자체를 mutate.
        """
        existing_from = message["From"]
        if existing_from:
            # 이미 채워져 있음 — 그대로 보존. 디버그 로그도 굳이 남기지 않음.
            return

        config = self._config
        # 빈 값 헤더가 존재할 가능성을 고려해 명시적으로 제거 후 set.
        del message["From"]
        message["From"] = (
            f"{config.from_display_name} <{config.sender_address}>"
        )
        logger.debug(
            "From 헤더가 비어 있어 SystemSetting 값으로 채움: from_display_name={!r} "
            "sender_address={}",
            config.from_display_name,
            config.sender_address,
        )

    @staticmethod
    def _build_xoauth2_blob(sender_address: str, access_token: str) -> str:
        """SMTP XOAUTH2 base64 인증 페이로드를 만든다.

        첨부 phase_a1_prompt.md 의 XOAUTH2 빌드 공식 그대로:

        - raw = ``f\"user={sender_address}\\\\x01auth=Bearer {access_token}\\\\x01\\\\x01\"``
        - return = ``base64.b64encode(raw.encode()).decode()``

        구분자 ``\\x01`` 은 Ctrl-A (SOH) — RFC 4954 + Google/M365 XOAUTH2 스펙.

        Args:
            sender_address: SMTP MAIL FROM 으로 사용할 메일박스 주소.
            access_token: msal 이 발급한 OAuth access token.

        Returns:
            base64 인코딩된 XOAUTH2 페이로드 문자열. ``AUTH XOAUTH2 <return>``
            형태로 SMTP 서버에 전송된다.
        """
        raw = f"user={sender_address}\x01auth=Bearer {access_token}\x01\x01"
        return base64.b64encode(raw.encode()).decode()


__all__ = [
    "AUTHORITY_TEMPLATE",
    "M365OAuthSmtpTransport",
    "OAUTH_SCOPE",
    "SMTP_AUTH_SUCCESS_CODE",
    "SMTP_HOST",
    "SMTP_PORT",
]
