"""메일 발송 인프라 패키지 (Phase A-1 / task 00104).

본 패키지는 다음 구성으로 채워진다 (subtask 00104-3~8 에서 단계별로 도입):

- :mod:`app.email.constants` — SystemSetting 키 이름과 default 값 상수 (본 subtask
  00104-2 의 산출물).
- :mod:`app.email.config` — SystemSetting → 설정 dataclass 변환 (00104-4).
- :mod:`app.email.transport` — EmailTransport ABC + 구현체 + factory (00104-4~6).
- :mod:`app.email.message_builder` — plain text EmailMessage 생성 (00104-7).
- :mod:`app.email.sender` — 재시도 + EmailSendRun 이력 기록 상위 layer (00104-8).

본 ``__init__`` 은 의도적으로 비어 있다 — 상위 호출자는 하위 모듈을 명시적으로
import 한다 (``from app.email.constants import DEFAULT_EMAIL_TRANSPORT_TYPE`` 등).
패키지 수준 re-export 는 두지 않는다.

설계 근거: ``docs/phase_a1_design_note.md`` §2-3, §11.
"""

__all__: list[str] = []
