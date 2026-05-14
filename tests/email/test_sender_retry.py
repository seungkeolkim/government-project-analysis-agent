"""send_with_retry 단위 테스트 (Phase A-1 / task 00104-10).

검증 시나리오 (subtask guidance bullet 1 d/e):
    1. test_send_with_retry_first_fail_second_success — 첫 시도 실패 + 두 번째
       시도 성공 시: ``row.status='sent'``, ``attempt_count=2``,
       ``error_message`` 시맨틱은 design note §4-1 결정 (성공 시 None 으로 클리어).
    2. test_send_with_retry_max_exceeded — max_retry_count 초과 시:
       ``row.status='failed'``, 마지막 예외가 호출자로 그대로 전파.

DB 는 conftest 의 ``test_engine`` + ``db_session`` fixture 사용 (격리된 SQLite).
``time.sleep`` 은 monkeypatch 로 차단해 테스트가 실제로 sleep 하지 않도록 한다.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from sqlalchemy.orm import Session

from app.db.models import EmailSendRunStatus
from app.email.message_builder import build_plain_text_message
from app.email.sender import send_with_retry
from app.email.transport.base import EmailTransport


class _FakeTransport(EmailTransport):
    """테스트용 transport 구현체.

    ``results`` 는 호출 시점마다 소비될 결과 시퀀스이다. None 이면 정상 성공,
    Exception 인스턴스이면 그 예외를 raise 한다.
    """

    def __init__(self, results: list[None | Exception]) -> None:
        """주어진 결과 시퀀스로 transport 를 초기화한다."""
        self.results: list[None | Exception] = list(results)
        self.send_call_count: int = 0

    def send(self, message: EmailMessage) -> None:
        """다음 결과를 꺼내 None 이면 통과, 예외면 raise."""
        self.send_call_count += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """``time.sleep`` 을 no-op 으로 patch 해 테스트가 실제로 대기하지 않게 한다.

    sender 의 RETRY_BACKOFF_SECONDS=2.0 sleep 이 모든 retry 테스트를 느리게
    하므로 자동 적용.
    """
    monkeypatch.setattr("app.email.sender.time.sleep", lambda _seconds: None)


# ──────────────────────────────────────────────────────────────
# 1. test_send_with_retry_first_fail_second_success
# ──────────────────────────────────────────────────────────────


def test_send_with_retry_first_fail_second_success(db_session: Session) -> None:
    """첫 시도 실패 → 두 번째 시도 성공 시 EmailSendRun row 가 SENT 로 마무리.

    design note §4-1 결정에 따라:
        - ``status == SENT``
        - ``attempt_count == 2`` (총 시도 횟수)
        - ``error_message is None`` (성공 시 클리어 — 마지막 시도의 결과만 반영)
        - ``sent_at`` 가 set (UTC tz-aware)
        - 마지막 예외가 호출자로 전파되지 않음 (성공이므로)
    """
    transport = _FakeTransport(
        results=[RuntimeError("1차 일시적 실패"), None]
    )
    message = build_plain_text_message(
        recipient="user@example.com",
        subject="2차 성공 시나리오",
        body="본문",
    )

    run = send_with_retry(
        transport=transport,
        message=message,
        max_retry_count=2,
        related_kind="test_send",
        related_id=None,
        requested_by_user_id=None,
        session=db_session,
    )

    assert transport.send_call_count == 2, "1차 + 재시도 1회 = 총 2회 호출"
    assert run.status == EmailSendRunStatus.SENT
    assert run.attempt_count == 2
    # design note §4-1 결정: 성공 시 error_message 는 None 으로 클리어된다.
    assert run.error_message is None, (
        f"성공 row 의 error_message 는 None 이어야 함; got {run.error_message!r}"
    )
    assert run.sent_at is not None
    assert run.recipient == "user@example.com"
    assert run.subject == "2차 성공 시나리오"


# ──────────────────────────────────────────────────────────────
# 2. test_send_with_retry_max_exceeded
# ──────────────────────────────────────────────────────────────


def test_send_with_retry_max_exceeded(db_session: Session) -> None:
    """모든 시도 실패 시 EmailSendRun row 가 FAILED 로 마무리되고 마지막 예외가 전파.

    design note §4-1 결정:
        - ``status == FAILED``
        - ``attempt_count == 1 + max_retry_count`` (총 시도 횟수)
        - ``error_message`` 는 마지막 시도의 ``\"ClassName: msg\"`` 형식
          (중간 시도 에러는 본 컬럼에 저장하지 않고 loguru 로그에만 남김)
        - ``sent_at is None``
        - 마지막 예외가 호출자로 그대로 raise

    Row 는 send_with_retry 의 final commit 으로 이미 DB 에 영속되어, 후속
    호출자 (admin API) 가 send_run_id 를 사용자에게 안내할 수 있다.
    """
    last_exception = ConnectionError("최종 실패 메시지")
    transport = _FakeTransport(
        results=[
            RuntimeError("1차 실패"),
            RuntimeError("2차 실패"),
            last_exception,
        ]
    )
    message = build_plain_text_message(
        recipient="user@example.com",
        subject="전체 실패 시나리오",
        body="본문",
    )

    with pytest.raises(ConnectionError) as exc_info:
        send_with_retry(
            transport=transport,
            message=message,
            max_retry_count=2,
            related_kind="test_send",
            related_id=None,
            requested_by_user_id=42,
            session=db_session,
        )

    # 마지막 예외 객체가 그대로 전파됐는지 확인 (동일 인스턴스).
    assert exc_info.value is last_exception
    assert str(exc_info.value) == "최종 실패 메시지"

    # transport.send 호출 횟수 = 1 + max_retry_count = 3
    assert transport.send_call_count == 3

    # DB 에서 EmailSendRun row 가 commit 되었는지 확인.
    # send_with_retry 의 final session.commit() 직후 raise 됐으므로 row 는 영속.
    from app.db.models import EmailSendRun
    from sqlalchemy import select

    run = db_session.execute(
        select(EmailSendRun)
        .where(EmailSendRun.requested_by_user_id == 42)
        .order_by(EmailSendRun.created_at.desc())
    ).scalar_one()

    assert run.status == EmailSendRunStatus.FAILED
    assert run.attempt_count == 3, "총 시도 = 1 + max_retry_count = 3"
    assert run.sent_at is None, "실패 row 는 sent_at 가 NULL 이어야 함"
    # error_message 는 마지막 예외의 ClassName + str(exc) (design note §4-1).
    assert run.error_message == "ConnectionError: 최종 실패 메시지", (
        f"error_message 가 마지막 예외만 반영해야 함; got {run.error_message!r}"
    )
    assert run.recipient == "user@example.com"
    assert run.related_kind == "test_send"
    assert run.requested_by_user_id == 42
