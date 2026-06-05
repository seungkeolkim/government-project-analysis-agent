"""일반 공고 수집 스케줄 영속 저장소 단위 테스트 (task 00155-2).

SystemSetting JSON 리스트에 cron/interval 스케줄을 저장/조회/삭제/토글하는
CRUD 계약과 입력 검증을 검증한다. ``db_session`` fixture(tests/conftest.py)의
격리 SQLite 위에서 동작한다.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from app.db.models import SystemSetting
from app.scheduler.constants import SETTING_KEY_GENERAL_SCHEDULES
from app.scheduler.schedule_store import (
    SCHEDULE_MODE_CRON,
    SCHEDULE_MODE_INTERVAL,
    ScheduleConfigError,
    add_general_schedule_record,
    delete_general_schedule_record,
    get_general_schedule_record,
    list_general_schedule_records,
    set_general_schedule_enabled,
)


def test_list_is_empty_when_no_setting(db_session: Session) -> None:
    """키가 아직 없으면 빈 리스트를 반환한다."""
    assert list_general_schedule_records(db_session) == []


def test_add_cron_schedule_persists(db_session: Session) -> None:
    """cron 모드 스케줄을 추가하면 id 가 부여되고 DB JSON 에 영속된다."""
    record = add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 */6 * * *",
        active_sources=["iris", "ntis"],
    )
    db_session.commit()

    assert record.id  # uuid hex 가 부여됨
    assert record.mode == SCHEDULE_MODE_CRON
    assert record.cron_expression == "0 */6 * * *"
    assert record.interval_hours is None
    assert record.active_sources == ["iris", "ntis"]
    assert record.enabled is True

    # 실제 SystemSetting row 에 JSON 리스트로 저장됐는지 확인.
    row = db_session.get(SystemSetting, SETTING_KEY_GENERAL_SCHEDULES)
    assert row is not None
    stored = json.loads(row.value)
    assert isinstance(stored, list)
    assert stored[0]["cron_expression"] == "0 */6 * * *"


def test_add_interval_schedule_persists(db_session: Session) -> None:
    """interval 모드 스케줄을 추가하면 interval_hours 가 저장된다."""
    record = add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_INTERVAL,
        interval_hours=6,
        active_sources=[],
    )
    db_session.commit()

    assert record.mode == SCHEDULE_MODE_INTERVAL
    assert record.interval_hours == 6
    assert record.cron_expression is None
    assert record.active_sources == []


def test_add_multiple_preserves_order(db_session: Session) -> None:
    """여러 건을 추가하면 추가 순서가 보존된다."""
    first = add_general_schedule_record(
        db_session, mode=SCHEDULE_MODE_CRON, cron_expression="0 7 * * 1-5"
    )
    second = add_general_schedule_record(
        db_session, mode=SCHEDULE_MODE_INTERVAL, interval_hours=12
    )
    db_session.commit()

    records = list_general_schedule_records(db_session)
    assert [r.id for r in records] == [first.id, second.id]


def test_get_by_id(db_session: Session) -> None:
    """id 로 단건 조회가 동작하고, 없는 id 는 None 을 반환한다."""
    record = add_general_schedule_record(
        db_session, mode=SCHEDULE_MODE_CRON, cron_expression="0 3 * * *"
    )
    db_session.commit()

    fetched = get_general_schedule_record(db_session, record.id)
    assert fetched is not None
    assert fetched.id == record.id
    assert get_general_schedule_record(db_session, "nonexistent") is None


def test_delete_removes_record(db_session: Session) -> None:
    """삭제하면 리스트에서 빠지고 True 를 반환한다."""
    record = add_general_schedule_record(
        db_session, mode=SCHEDULE_MODE_CRON, cron_expression="0 3 * * *"
    )
    db_session.commit()

    assert delete_general_schedule_record(db_session, record.id) is True
    db_session.commit()
    assert list_general_schedule_records(db_session) == []


def test_delete_unknown_returns_false(db_session: Session) -> None:
    """없는 id 삭제는 False 를 반환한다."""
    assert delete_general_schedule_record(db_session, "nope") is False


def test_toggle_enabled(db_session: Session) -> None:
    """토글로 enabled 상태가 바뀌고 영속된다."""
    record = add_general_schedule_record(
        db_session, mode=SCHEDULE_MODE_CRON, cron_expression="0 3 * * *"
    )
    db_session.commit()

    updated = set_general_schedule_enabled(db_session, record.id, enabled=False)
    db_session.commit()
    assert updated.enabled is False

    reloaded = get_general_schedule_record(db_session, record.id)
    assert reloaded is not None
    assert reloaded.enabled is False


def test_toggle_unknown_raises(db_session: Session) -> None:
    """없는 id 토글은 ScheduleConfigError 를 던진다."""
    with pytest.raises(ScheduleConfigError):
        set_general_schedule_enabled(db_session, "nope", enabled=True)


def test_active_sources_are_normalized(db_session: Session) -> None:
    """active_sources 의 공백·빈 토큰이 정규화돼 저장된다."""
    record = add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 3 * * *",
        active_sources=[" iris ", "", "ntis", "   "],
    )
    assert record.active_sources == ["iris", "ntis"]


def test_cron_expression_whitespace_is_normalized(db_session: Session) -> None:
    """cron 표현식의 중복 공백이 단일 공백으로 정리된다."""
    record = add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0   3 *  * *",
    )
    assert record.cron_expression == "0 3 * * *"


def test_add_cron_without_expression_raises(db_session: Session) -> None:
    """cron 모드인데 표현식이 없으면 거부한다."""
    with pytest.raises(ScheduleConfigError):
        add_general_schedule_record(db_session, mode=SCHEDULE_MODE_CRON)


def test_add_cron_with_wrong_field_count_raises(db_session: Session) -> None:
    """5-필드가 아닌 cron 표현식은 거부한다."""
    with pytest.raises(ScheduleConfigError):
        add_general_schedule_record(
            db_session, mode=SCHEDULE_MODE_CRON, cron_expression="0 3 * *"
        )


def test_add_interval_without_hours_raises(db_session: Session) -> None:
    """interval 모드인데 interval_hours 가 없으면 거부한다."""
    with pytest.raises(ScheduleConfigError):
        add_general_schedule_record(db_session, mode=SCHEDULE_MODE_INTERVAL)


@pytest.mark.parametrize("invalid_hours", [0, -1, 25, 100])
def test_add_interval_out_of_range_raises(
    db_session: Session, invalid_hours: int
) -> None:
    """interval_hours 가 1~24 범위를 벗어나면 거부한다."""
    with pytest.raises(ScheduleConfigError):
        add_general_schedule_record(
            db_session,
            mode=SCHEDULE_MODE_INTERVAL,
            interval_hours=invalid_hours,
        )


def test_unknown_mode_raises(db_session: Session) -> None:
    """알 수 없는 mode 는 거부한다."""
    with pytest.raises(ScheduleConfigError):
        add_general_schedule_record(
            db_session, mode="weekly", cron_expression="0 3 * * *"
        )


def test_corrupted_setting_value_is_treated_as_empty(db_session: Session) -> None:
    """JSON 파싱 불가한 손상 값은 빈 리스트로 방어한다."""
    db_session.add(
        SystemSetting(key=SETTING_KEY_GENERAL_SCHEDULES, value="{not json")
    )
    db_session.commit()
    assert list_general_schedule_records(db_session) == []
