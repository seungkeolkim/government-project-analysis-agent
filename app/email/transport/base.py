"""이메일 발송 transport 의 추상 인터페이스 (Phase A-1 / task 00104-4).

설계 근거:
    docs/phase_a1_design_note.md §1-4, §11. 첨부 phase_a1_prompt.md 의
    ``app/email/transport/base.py`` 섹션을 그대로 옮긴다.

본 모듈은 외부 의존성을 일체 갖지 않는다 — Python 표준 라이브러리 ``abc`` 와
``email.message`` 만 사용한다. 그래야 단위 테스트에서 외부 네트워크 없이도
임포트할 수 있고, 향후 옵션 C(Basic Auth SMTP) 같은 다른 transport 구현체
가 추가되어도 본 인터페이스에는 변동이 없다.

호출 계층:
    상위 layer (``app.email.sender.send_with_retry``) 와 모든 외부 호출자는
    오직 ``EmailTransport`` 인터페이스에만 의존한다. 구체 구현체는 SystemSetting
    의 ``email.transport.type`` 값에 따라 factory (``app.email.transport.factory``,
    subtask 00104-6) 가 선택적으로 인스턴스화한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from email.message import EmailMessage


class EmailTransport(ABC):
    """이메일 발송 transport 의 공통 인터페이스.

    상위 발송 layer (sender.py) 와 모든 호출자는 이 인터페이스에만 의존합니다.
    구현체는 SystemSetting 으로 결정되며, 현재는 M365OAuthSmtpTransport 단 하나만
    존재합니다. 향후 옵션 C (Basic Auth SMTP) 가 추가될 경우 동일 인터페이스를
    구현하는 클래스를 하나 더 추가하고 factory 에 매핑만 더하면 됩니다.
    """

    @abstractmethod
    def send(self, message: EmailMessage) -> None:
        """주어진 EmailMessage 를 발송합니다.

        실패 시 예외를 그대로 던지며, 재시도 결정은 호출자가 합니다.

        Args:
            message: 발송할 ``email.message.EmailMessage`` 객체. From 헤더가
                비어 있다면 구현체가 SystemSetting 의 ``email.from_display_name`` +
                ``email.m365.sender_address`` 조합으로 채웁니다.

        Raises:
            Exception: 발송 실패 시 transport 구현체별 적절한 예외를 그대로 던집니다.
                (예: ``RuntimeError`` — 토큰 발급 실패 / XOAUTH2 인증 실패,
                 ``smtplib.SMTPException`` — SMTP 프로토콜 오류,
                 ``OSError`` — 네트워크 연결 실패 등).
                상위 layer (``send_with_retry``) 가 ``Exception`` 단위로 catch
                하여 재시도 정책을 적용합니다.
        """
        ...


__all__ = ["EmailTransport"]
