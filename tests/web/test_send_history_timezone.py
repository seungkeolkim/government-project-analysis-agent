"""발송 이력 serializer 의 timestamp 시간대 정규화(as_utc) 단위 테스트.

task 00160-2: SQLite ``DateTime(timezone=True)`` 컬럼이 SELECT 시 naive
datetime 으로 돌아오는 탓에, serializer 가 raw ``.isoformat()`` 으로 직렬화하면
offset(+00:00)이 빠진 ISO 문자열이 나가고 프론트 ``formatDateTimeKst`` 가 이를
로컬 타임으로 오파싱한다(+9h 누락). serializer 에 ``as_utc()`` 를 적용해 naive
입력에도 항상 UTC offset 이 붙는지, None 은 여전히 None 으로 직렬화되는지를
검증한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.web.routes.admin_email import _serialize_send_run as serialize_admin_send_run
from app.web.routes.forward import _serialize_forward_log
from app.web.routes.forward import _serialize_send_run as serialize_forward_send_run

# SQLite SELECT 결과를 모사한 naive datetime (offset 정보 없음).
_NAIVE_DT = datetime(2026, 6, 5, 7, 30, 0)


def _fake_status(value: str) -> SimpleNamespace:
    """``status.value`` 접근을 흉내내는 가짜 enum 객체를 만든다."""
    return SimpleNamespace(value=value)


def test_admin_serialize_send_run_naive_datetime_gets_utc_offset() -> None:
    """admin_email._serialize_send_run 이 naive created_at/sent_at 에 +00:00 을 부여한다."""
    run = SimpleNamespace(
        id=1,
        recipient="user@example.com",
        subject="제목",
        status=_fake_status("sent"),
        attempt_count=1,
        error_message=None,
        created_at=_NAIVE_DT,
        sent_at=_NAIVE_DT,
        requested_by_user_id=None,
        requested_by=None,
        related_kind=None,
        related_id=None,
        transport_type="smtp",
    )

    result = serialize_admin_send_run(run)

    assert result["created_at"] == "2026-06-05T07:30:00+00:00"
    assert result["sent_at"] == "2026-06-05T07:30:00+00:00"


def test_admin_serialize_send_run_none_timestamps_stay_none() -> None:
    """None 인 created_at/sent_at 은 여전히 None 으로 직렬화된다(회귀 방지)."""
    run = SimpleNamespace(
        id=2,
        recipient="user@example.com",
        subject="제목",
        status=None,
        attempt_count=0,
        error_message=None,
        created_at=None,
        sent_at=None,
        requested_by_user_id=None,
        requested_by=None,
        related_kind=None,
        related_id=None,
        transport_type="smtp",
    )

    result = serialize_admin_send_run(run)

    assert result["created_at"] is None
    assert result["sent_at"] is None


def test_admin_serialize_send_run_aware_datetime_preserved() -> None:
    """이미 tz-aware 인 값은 as_utc 가 그대로 두어 offset 이 유지된다."""
    aware_dt = datetime(2026, 6, 5, 7, 30, 0, tzinfo=UTC)
    run = SimpleNamespace(
        id=3,
        recipient="user@example.com",
        subject="제목",
        status=_fake_status("sent"),
        attempt_count=1,
        error_message=None,
        created_at=aware_dt,
        sent_at=aware_dt,
        requested_by_user_id=None,
        requested_by=None,
        related_kind=None,
        related_id=None,
        transport_type="smtp",
    )

    result = serialize_admin_send_run(run)

    assert result["created_at"] == "2026-06-05T07:30:00+00:00"
    assert result["sent_at"] == "2026-06-05T07:30:00+00:00"


def test_forward_serialize_send_run_naive_datetime_gets_utc_offset() -> None:
    """forward._serialize_send_run 이 naive sent_at 에 +00:00 을 부여한다."""
    send_run = SimpleNamespace(
        id=10,
        recipient="user@example.com",
        status=_fake_status("sent"),
        attempt_count=1,
        error_message=None,
        sent_at=_NAIVE_DT,
    )

    result = serialize_forward_send_run(send_run)

    assert result["sent_at"] == "2026-06-05T07:30:00+00:00"


def test_forward_serialize_send_run_none_sent_at_stays_none() -> None:
    """None 인 sent_at 은 여전히 None 으로 직렬화된다(회귀 방지)."""
    send_run = SimpleNamespace(
        id=11,
        recipient="user@example.com",
        status=None,
        attempt_count=0,
        error_message=None,
        sent_at=None,
    )

    result = serialize_forward_send_run(send_run)

    assert result["sent_at"] is None


def test_forward_serialize_forward_log_naive_datetimes_get_utc_offset() -> None:
    """forward._serialize_forward_log 이 naive created_at/completed_at 에 +00:00 을 부여한다."""
    forward_log = SimpleNamespace(
        id=20,
        sender_user=None,
        sender_organization=None,
        subject="제목",
        recipient_count=3,
        has_additional_message=False,
        status=_fake_status("succeeded"),
        success_count=3,
        failure_count=0,
        created_at=_NAIVE_DT,
        completed_at=_NAIVE_DT,
    )

    result = _serialize_forward_log(forward_log)

    assert result["created_at"] == "2026-06-05T07:30:00+00:00"
    assert result["completed_at"] == "2026-06-05T07:30:00+00:00"


def test_forward_serialize_forward_log_none_completed_at_stays_none() -> None:
    """completed_at 이 None(진행 중)이면 None 으로 직렬화되고 회귀가 없다."""
    forward_log = SimpleNamespace(
        id=21,
        sender_user=None,
        sender_organization=None,
        subject="제목",
        recipient_count=3,
        has_additional_message=False,
        status=None,
        success_count=0,
        failure_count=0,
        created_at=_NAIVE_DT,
        completed_at=None,
    )

    result = _serialize_forward_log(forward_log)

    assert result["created_at"] == "2026-06-05T07:30:00+00:00"
    assert result["completed_at"] is None
