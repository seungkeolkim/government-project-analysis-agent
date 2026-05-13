"""EmailTransport factory — SystemSetting 값 기반 구현체 선택 (Phase A-1 / task 00104-6).

설계 근거:
    docs/phase_a1_design_note.md §4-5 (transport_type 확장 여유) + 첨부
    phase_a1_prompt.md 의 ``app/email/transport/factory.py`` 섹션.

본 모듈은 SystemSetting 의 ``email.transport.type`` 값을 보고 적절한
``EmailTransport`` 구현체를 인스턴스화해서 돌려준다. 현재 유효한 값은 ``m365_oauth``
단 하나이며, 다른 값이 들어오면 ``ValueError`` 를 던진다.

확장 정책 (디자인 노트 §4-5):
    향후 옵션 C (Basic Auth SMTP) 가 추가될 경우 다음 두 가지만 더 하면 된다.

    1. ``app.email.constants.ALLOWED_EMAIL_TRANSPORT_TYPES`` frozenset 에 새
       문자열 값을 추가 (예: ``\"basic_auth_smtp\"``).
    2. 본 모듈에 새 구현체 클래스를 import 하고, 아래 분기 체인에 ``elif`` 한
       줄을 더한다.

    옵션 C 의 ``BasicAuthSmtpTransport`` 분기는 본 subtask 에서 작성하지 않는다
    (첨부 문서 \"범위 밖\" — 옵션 C 구현 금지). Transport ABC 가 확장 여유를
    제공하는 것만으로 충분하다.

사이드 이펙트:
    본 factory 는 SystemSetting read (DB 단일 SELECT 7회) 와 dataclass / 클래스
    인스턴스화만 수행한다. 외부 네트워크 호출 (msal 토큰 발급, smtplib 연결) 은
    반환된 transport 의 ``send()`` 가 호출될 때 일어난다 (디자인 노트 §6-3 의
    \"매 send() 마다 새 msal 인스턴스\" 정책과 정합).
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.orm import Session

from app.backup.service import get_setting
from app.email.config import load_m365_oauth_config
from app.email.constants import (
    DEFAULT_EMAIL_TRANSPORT_TYPE,
    SETTING_KEY_EMAIL_TRANSPORT_TYPE,
    TRANSPORT_TYPE_M365_OAUTH,
)
from app.email.transport.base import EmailTransport
from app.email.transport.m365_oauth import M365OAuthSmtpTransport


def build_transport_from_settings(session: Session) -> EmailTransport:
    """SystemSetting 의 ``email.transport.type`` 값을 보고 적절한 transport 를 만든다.

    현재 유효한 값은 ``\"m365_oauth\"`` 단 하나이며, 다른 값이 들어 있으면
    ``ValueError`` 를 던진다 (사용자 / 운영자에게 SystemSetting 입력 오류를
    즉시 알리기 위함 — 잘못된 값으로 \"발송\" 하면 더 어렵게 실패한다).

    SystemSetting row 가 없거나 값이 비어 있으면 ``DEFAULT_EMAIL_TRANSPORT_TYPE``
    (= ``\"m365_oauth\"``) 로 fallback 한다 (디자인 노트 §2-4 — backup 도메인의
    fallback-only seed 패턴). 따라서 신규 설치 직후에도 ``ValueError`` 가 나지
    않고 m365_oauth 가 선택된다 — 자격증명이 비어 있어 실제 발송은 토큰 발급
    단계에서 실패하지만, 그건 transport.send() 의 책임 (자격증명 미입력
    안내).

    파라미터 명은 첨부 phase_a1_prompt.md 의 의사 시그니처
    ``build_transport_from_settings(system_setting_service)`` 와 다르지만,
    실제 코드베이스의 ``app.backup.service.get_setting(session, key)`` 패턴을
    그대로 따른다 (디자인 노트 §2-3). 구현 의미는 동일하다.

    Args:
        session: SQLAlchemy ORM Session. SystemSetting 조회용. 본 함수는
            read-only 로 사용하며 commit / rollback 하지 않는다.

    Returns:
        SystemSetting 값에 맞춰 만들어진 ``EmailTransport`` 구현체 인스턴스.
        현재는 항상 ``M365OAuthSmtpTransport`` (또는 ``ValueError``).

    Raises:
        ValueError: ``email.transport.type`` 값이 코드가 인식하는 값이 아닐 때.
            메시지 형식: ``f\"미지원 transport type: {value!r}\"`` (운영자가 즉시
            SystemSetting 을 수정할 수 있도록 정확한 값 그대로 노출).
    """
    transport_type = (
        get_setting(session, SETTING_KEY_EMAIL_TRANSPORT_TYPE)
        or DEFAULT_EMAIL_TRANSPORT_TYPE
    )
    logger.debug(
        "EmailTransport factory: 선택된 transport_type={!r}", transport_type
    )

    if transport_type == TRANSPORT_TYPE_M365_OAUTH:
        # M365OAuthSmtpConfig 5 필드 (tenant_id / client_id / client_secret /
        # sender_address / from_display_name) 를 SystemSetting 에서 읽어 frozen
        # dataclass 로 묶고, M365OAuthSmtpTransport 에 주입한다.
        config = load_m365_oauth_config(session)
        return M365OAuthSmtpTransport(config)

    # 향후 옵션 C 추가 시 여기에 ``elif transport_type == \"basic_auth_smtp\":``
    # 한 줄과 import + 구현체 인스턴스화 1 줄을 더하면 된다. 본 subtask 에서는
    # 옵션 C 구현 금지 (첨부 문서 \"범위 밖\" + 디자인 노트 §11).

    raise ValueError(f"미지원 transport type: {transport_type!r}")


__all__ = ["build_transport_from_settings"]
