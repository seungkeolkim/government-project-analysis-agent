"""SystemSetting → 메일 발송 설정 dataclass 변환 (Phase A-1 / task 00104-4).

본 모듈은 SystemSetting 테이블의 ``email.m365.*`` + ``email.from_display_name``
키 5 개를 단일 dataclass(``M365OAuthSmtpConfig``) 로 묶어 반환한다. 후속 subtask
(00104-5 M365OAuthSmtpTransport, 00104-6 factory) 가 이 dataclass 를 받아 발송
인스턴스를 만든다.

설계 결정 (docs/phase_a1_design_note.md):
    §2-3 — SystemSetting read/write 헬퍼는 ``app.backup.service.get_setting`` /
        ``set_setting`` 을 재사용한다. email 도메인용 별도 헬퍼는 만들지 않는다.
    §2-4 — Default 값은 SystemSetting row 가 없을 때 코드 상의 fallback 으로만
        적용한다 (별도 seed migration 없음). 본 함수가 ``get_setting(...) or
        DEFAULT_*`` 로 그 fallback 을 수행한다.
    §6-3 — msal 인스턴스 자체는 ``send()`` 호출마다 새로 생성하므로(캐시 효과
        포기 + 항상 최신 자격증명 보장), 본 dataclass 도 매 발송마다 새로 로드
        되는 것을 전제로 한다.

비-범위:
    - max_retry_count 는 본 dataclass 에 포함하지 않는다. 첨부 phase_a1_prompt.md
      의 send_with_retry 의사 signature 가 ``max_retry_count: int`` 를 명시적 인자
      로 받기 때문이며, sender (00104-8) 가 SystemSetting 에서 직접 읽어 전달한다.
    - 향후 옵션 C (Basic Auth SMTP) 가 추가될 때는 별도 dataclass
      (``BasicAuthSmtpConfig`` 등) 를 추가하면 된다. 본 dataclass 는 옵션 B 전용.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.backup.service import get_setting
from app.email.constants import (
    DEFAULT_EMAIL_FROM_DISPLAY_NAME,
    DEFAULT_EMAIL_M365_CLIENT_ID,
    DEFAULT_EMAIL_M365_CLIENT_SECRET,
    DEFAULT_EMAIL_M365_SENDER_ADDRESS,
    DEFAULT_EMAIL_M365_TENANT_ID,
    SETTING_KEY_EMAIL_FROM_DISPLAY_NAME,
    SETTING_KEY_EMAIL_M365_CLIENT_ID,
    SETTING_KEY_EMAIL_M365_CLIENT_SECRET,
    SETTING_KEY_EMAIL_M365_SENDER_ADDRESS,
    SETTING_KEY_EMAIL_M365_TENANT_ID,
)


@dataclass(frozen=True)
class M365OAuthSmtpConfig:
    """Option B (M365 OAuth XOAUTH2 SMTP) 발송에 필요한 5 개 설정 값을 묶는 immutable dataclass.

    ``frozen=True`` 로 불변성을 보장한다 — transport 가 생성된 이후 외부에서 필드
    값을 바꿔도 인증 동작이 바뀌지 않도록 (전형적 동시성/혼동 방지 패턴).

    필드:
        tenant_id: Azure AD Directory (tenant) ID. IT 팀이 Azure AD app
            registration 셋업 후 사용자에게 알려주는 값. 빈 문자열 가능하나
            그 경우 transport.send() 가 토큰 발급에서 실패한다.
        client_id: Application (client) ID. IT 팀 제공 값.
        client_secret: Client secret value (평문). DB-level 암호화는 향후 별도
            작업 (디자인 노트 §10). 빈 문자열이면 토큰 발급 실패.
        sender_address: SMTP MAIL FROM + From 헤더 메일 박스. IT 가 SendAs 권한을
            부여한 mailbox. default ``\"gov-agent-noreply@innodep.com\"``.
        from_display_name: From 헤더의 표시명. EmailMessage 가 From 헤더를 가지지
            않은 채로 들어왔을 때 transport 가 ``f\"{display_name} <{sender_address}>\"``
            형식으로 채운다. default ``\"정부사업 모니터링 봇\"``.

    본 dataclass 는 SMTP 연결 정보(host/port=smtp.office365.com:587) 와 OAuth scope
    (``https://outlook.office365.com/.default``) 는 들고 있지 않다 — 이 값들은
    옵션 B 의 정의 자체에 묶인 \"코드 상수\" 라서 transport 구현체
    (``app.email.transport.m365_oauth``) 내부에 박는다 (SystemSetting 화하지 않음).
    """

    tenant_id: str
    client_id: str
    client_secret: str
    sender_address: str
    from_display_name: str


def load_m365_oauth_config(session: Session) -> M365OAuthSmtpConfig:
    """SystemSetting 에서 M365 OAuth 5 개 키를 읽어 dataclass 로 반환한다.

    SystemSetting row 가 없거나 ``value`` 가 NULL/빈 문자열이면 본 모듈의 default
    상수로 fallback 한다 (백업 도메인 ``app.backup.service`` 의 같은 패턴).
    fallback 동작은 \"빈 문자열 == 미설정\" 시맨틱을 의미하며, 사용자가 의도적으로
    빈 값으로 만들 수 없도록 한다 — sender_address / from_display_name 처럼
    non-empty default 가 있는 키는 의미 있게 채워진 채로 사용된다.

    Args:
        session: SQLAlchemy ORM 세션. ``app.backup.service.get_setting`` 의 첫
            인자로 그대로 전달된다. 호출자가 트랜잭션 경계를 책임지며 본 함수는
            commit / rollback 하지 않는다 — 단순 read-only.

    Returns:
        5 개 필드를 모두 채운 ``M365OAuthSmtpConfig``. 자격증명이 미입력 상태
        (빈 문자열 default) 이면 client_secret/tenant_id/client_id 가 빈 문자열로
        남아 있으므로, 호출자(transport) 가 send() 시 명확한 에러를 던지도록
        구현해야 한다 (subtask 00104-5 의 책임).
    """
    return M365OAuthSmtpConfig(
        tenant_id=(
            get_setting(session, SETTING_KEY_EMAIL_M365_TENANT_ID)
            or DEFAULT_EMAIL_M365_TENANT_ID
        ),
        client_id=(
            get_setting(session, SETTING_KEY_EMAIL_M365_CLIENT_ID)
            or DEFAULT_EMAIL_M365_CLIENT_ID
        ),
        client_secret=(
            get_setting(session, SETTING_KEY_EMAIL_M365_CLIENT_SECRET)
            or DEFAULT_EMAIL_M365_CLIENT_SECRET
        ),
        sender_address=(
            get_setting(session, SETTING_KEY_EMAIL_M365_SENDER_ADDRESS)
            or DEFAULT_EMAIL_M365_SENDER_ADDRESS
        ),
        from_display_name=(
            get_setting(session, SETTING_KEY_EMAIL_FROM_DISPLAY_NAME)
            or DEFAULT_EMAIL_FROM_DISPLAY_NAME
        ),
    )


__all__ = [
    "M365OAuthSmtpConfig",
    "load_m365_oauth_config",
]
