"""메일 전송 활성화 게이트 헬퍼 (task 00115-1).

``email.send_enabled`` SystemSetting 키를 읽어 메일 전송 기능이 켜져 있는지
판단하는 단일 관심사 모듈이다. forwarding, test-send, daily report 등 메일을
실제로 발송하는 모든 경로가 이 모듈을 import 해 진입 직후에 게이트를 확인한다.

설계 결정:
    - ``DEFAULT_EMAIL_SEND_ENABLED = False`` — row 가 없으면 off 로 fallback 해
      최초 기동 시 의도치 않은 메일 발송을 방지한다.
    - 저장 포맷은 ``\"true\"`` / ``\"false\"`` (소문자 통일). 읽을 때는
      case-insensitive 비교 — 운영자가 ``\"True\"`` 로 입력해도 허용.
    - ``_read_max_retry_count`` 와 동일한 방어 패턴: 잘못된 값 → fallback.
    - 본 모듈은 read-only. DB 에 쓰지 않는다.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.orm import Session

from app.backup.service import get_setting
from app.email.constants import (
    DEFAULT_EMAIL_SEND_ENABLED,
    SETTING_KEY_EMAIL_SEND_ENABLED,
)


class EmailSendingDisabledError(Exception):
    """메일 전송 기능이 비활성화된 상태에서 발송이 시도될 때 발생한다.

    라우터가 이 예외를 잡아 HTTP 503 으로 변환한다. 메시지에는 활성화
    방법(관리자 페이지 경로)이 포함되어 있어 운영자가 즉시 조치할 수 있다.
    """


def is_email_sending_enabled(session: Session) -> bool:
    """``email.send_enabled`` SystemSetting 을 읽어 메일 전송 활성화 여부를 반환한다.

    방어 패턴 (``_read_max_retry_count`` 와 동일):
        - row 가 없거나 값이 빈 문자열 → ``DEFAULT_EMAIL_SEND_ENABLED`` (False).
        - ``"true"`` (case-insensitive) → True.
        - 그 외 (``"false"``, 알 수 없는 값, 파싱 오류) → False + warning 로그.

    Args:
        session: SystemSetting 조회용 ORM 세션. 본 함수는 읽기만 하고 commit 하지
            않는다.

    Returns:
        메일 전송 기능이 활성화되어 있으면 True, 아니면 False.
    """
    raw_value = get_setting(session, SETTING_KEY_EMAIL_SEND_ENABLED)
    if raw_value is None or raw_value.strip() == "":
        # row 없음 → default(False). 최초 기동 시 off 보장.
        return DEFAULT_EMAIL_SEND_ENABLED
    normalized = raw_value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    # 예상치 못한 값(예: "1", "yes") — warning 후 안전하게 False 로.
    logger.warning(
        "SystemSetting {!r} 값이 예상 범위 밖입니다 ({!r}). "
        "False 로 fallback.",
        SETTING_KEY_EMAIL_SEND_ENABLED,
        raw_value,
    )
    return False


__all__ = [
    "EmailSendingDisabledError",
    "is_email_sending_enabled",
]
