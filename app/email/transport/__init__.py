"""이메일 발송 transport 추상화 + 구현체 모음 (Phase A-1 / task 00104).

구성 (subtask 단위 단계별 도입):

- :mod:`app.email.transport.base` — EmailTransport(ABC) 추상 인터페이스
  (subtask 00104-4 의 산출물 — 본 모듈을 import 하는 시점부터 사용 가능).
- :mod:`app.email.transport.m365_oauth` — M365OAuthSmtpTransport
  (subtask 00104-5 에서 도입). msal + smtplib 기반 옵션 B (XOAUTH2) 구현체.
- :mod:`app.email.transport.factory` — build_transport_from_settings 함수
  (subtask 00104-6 에서 도입). SystemSetting 의 transport.type 키를 보고 적절한
  구현체를 만든다.

본 ``__init__`` 은 의도적으로 비어 있다 — 상위 호출자는 하위 모듈을 명시적으로
import 한다 (``from app.email.transport.base import EmailTransport`` 등).
``app.email`` 상위 패키지의 컨벤션과 동일.

설계 근거: ``docs/phase_a1_design_note.md`` §11, §12.
"""

__all__: list[str] = []
