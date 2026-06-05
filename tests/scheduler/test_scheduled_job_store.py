"""scheduled_jobs SSOT 접근 계층(store) 단위 테스트 (task 00157-1).

신규 ``scheduled_jobs`` 테이블 위의 통합 list/get/add/toggle/delete/upsert API 와
입력 검증·싱글턴 시드를 검증한다. ``db_session`` fixture(tests/conftest.py)의 격리
SQLite 위에서 동작하며, 이 SQLite 는 Alembic upgrade(00157-1 마이그레이션 포함)로
스키마가 올라가므로 backup/daily_report/gc 싱글턴이 이미 기본 시드돼 있다.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.models import ScheduledJob
from app.scheduler.constants import (
    DEFAULT_GC_ORPHAN_CRON,
    JOB_KIND_BACKUP,
    JOB_KIND_DAILY_REPORT,
    JOB_KIND_GC,
    JOB_KIND_SCRAPE_GENERAL,
    TRIGGER_TYPE_CRON,
    TRIGGER_TYPE_INTERVAL,
)
from app.scheduler.scheduled_job_store import (
    ScheduledJobConfigError,
    add_general_schedule,
    delete_scheduled_job,
    ensure_default_seed_jobs,
    get_scheduled_job,
    get_singleton_schedule,
    list_general_schedules,
    list_scheduled_jobs,
    set_scheduled_job_enabled,
    upsert_singleton_schedule,
)


# ──────────────────────────────────────────────────────────────
# 신규 설치 기본 시드 (마이그레이션이 보장)
# ──────────────────────────────────────────────────────────────


def test_fresh_install_seeds_singletons(db_session: Session) -> None:
    """빈 DB 에 마이그레이션이 올라가면 backup/daily_report/gc 싱글턴이 시드된다."""
    backup = get_singleton_schedule(db_session, JOB_KIND_BACKUP)
    daily = get_singleton_schedule(db_session, JOB_KIND_DAILY_REPORT)
    gc = get_singleton_schedule(db_session, JOB_KIND_GC)

    assert backup is not None
    assert backup.trigger_type == TRIGGER_TYPE_CRON
    assert backup.cron_expression == "0 3 * * *"
    assert backup.enabled is True

    assert daily is not None
    assert daily.cron_expression == "0 9 * * 1-5"
    # Daily Report 는 최초 기동 시 off 가 기본이다.
    assert daily.enabled is False

    assert gc is not None
    assert gc.cron_expression == DEFAULT_GC_ORPHAN_CRON
    assert gc.enabled is True

    # 일반 수집 스케줄은 신규 설치 시 0건이다.
    assert list_general_schedules(db_session) == []


# ──────────────────────────────────────────────────────────────
# 일반 수집(scrape_general) CRUD round-trip
# ──────────────────────────────────────────────────────────────


def test_add_cron_general_schedule_round_trip(db_session: Session) -> None:
    """cron 일반 수집을 추가하면 id 가 부여되고 DB 에 영속된다."""
    record = add_general_schedule(
        db_session,
        trigger_type=TRIGGER_TYPE_CRON,
        cron_expression="0 */6 * * *",
        active_sources=["iris", "ntis"],
    )
    db_session.commit()

    assert isinstance(record.id, int)
    assert record.job_kind == JOB_KIND_SCRAPE_GENERAL
    assert record.trigger_type == TRIGGER_TYPE_CRON
    assert record.cron_expression == "0 */6 * * *"
    assert record.interval_hours is None
    assert record.active_sources == ["iris", "ntis"]
    assert record.enabled is True

    fetched = get_scheduled_job(db_session, record.id)
    assert fetched is not None
    assert fetched.cron_expression == "0 */6 * * *"
    assert fetched.active_sources == ["iris", "ntis"]


def test_add_interval_general_schedule(db_session: Session) -> None:
    """interval 일반 수집을 추가하면 interval_hours 가 저장된다."""
    record = add_general_schedule(
        db_session,
        trigger_type=TRIGGER_TYPE_INTERVAL,
        interval_hours=6,
        active_sources=[],
    )
    db_session.commit()

    assert record.trigger_type == TRIGGER_TYPE_INTERVAL
    assert record.interval_hours == 6
    assert record.cron_expression is None
    assert record.active_sources == []


def test_list_general_schedules_preserves_order(db_session: Session) -> None:
    """여러 건을 추가하면 id 오름차순(추가 순서)이 보존된다."""
    first = add_general_schedule(
        db_session, trigger_type=TRIGGER_TYPE_CRON, cron_expression="0 7 * * 1-5"
    )
    second = add_general_schedule(
        db_session, trigger_type=TRIGGER_TYPE_INTERVAL, interval_hours=12
    )
    db_session.commit()

    ids = [record.id for record in list_general_schedules(db_session)]
    assert ids == [first.id, second.id]


def test_list_general_schedules_excludes_singletons(db_session: Session) -> None:
    """일반 수집 목록에는 backup/daily_report/gc 싱글턴이 섞이지 않는다."""
    add_general_schedule(
        db_session, trigger_type=TRIGGER_TYPE_CRON, cron_expression="0 3 * * *"
    )
    db_session.commit()

    kinds = {record.job_kind for record in list_general_schedules(db_session)}
    assert kinds == {JOB_KIND_SCRAPE_GENERAL}


def test_toggle_enabled(db_session: Session) -> None:
    """토글로 enabled 상태가 바뀌고 영속된다."""
    record = add_general_schedule(
        db_session, trigger_type=TRIGGER_TYPE_CRON, cron_expression="0 3 * * *"
    )
    db_session.commit()

    updated = set_scheduled_job_enabled(db_session, record.id, enabled=False)
    db_session.commit()
    assert updated.enabled is False

    reloaded = get_scheduled_job(db_session, record.id)
    assert reloaded is not None
    assert reloaded.enabled is False


def test_toggle_unknown_raises(db_session: Session) -> None:
    """없는 id 토글은 ScheduledJobConfigError 를 던진다."""
    with pytest.raises(ScheduledJobConfigError):
        set_scheduled_job_enabled(db_session, 999999, enabled=True)


def test_delete_removes_record(db_session: Session) -> None:
    """삭제하면 목록에서 빠지고 True 를 반환한다."""
    record = add_general_schedule(
        db_session, trigger_type=TRIGGER_TYPE_CRON, cron_expression="0 3 * * *"
    )
    db_session.commit()

    assert delete_scheduled_job(db_session, record.id) is True
    db_session.commit()
    assert list_general_schedules(db_session) == []


def test_delete_unknown_returns_false(db_session: Session) -> None:
    """없는 id 삭제는 False 를 반환한다."""
    assert delete_scheduled_job(db_session, 999999) is False


def test_active_sources_are_normalized(db_session: Session) -> None:
    """active_sources 의 공백·빈 토큰이 정규화돼 저장된다."""
    record = add_general_schedule(
        db_session,
        trigger_type=TRIGGER_TYPE_CRON,
        cron_expression="0 3 * * *",
        active_sources=[" iris ", "", "ntis", "   "],
    )
    assert record.active_sources == ["iris", "ntis"]


def test_cron_expression_whitespace_is_normalized(db_session: Session) -> None:
    """cron 표현식의 중복 공백이 단일 공백으로 정리된다."""
    record = add_general_schedule(
        db_session,
        trigger_type=TRIGGER_TYPE_CRON,
        cron_expression="0   3 *  * *",
    )
    assert record.cron_expression == "0 3 * * *"


# ──────────────────────────────────────────────────────────────
# 입력 검증
# ──────────────────────────────────────────────────────────────


def test_add_cron_without_expression_raises(db_session: Session) -> None:
    """cron 트리거인데 표현식이 없으면 거부한다."""
    with pytest.raises(ScheduledJobConfigError):
        add_general_schedule(db_session, trigger_type=TRIGGER_TYPE_CRON)


def test_add_cron_with_wrong_field_count_raises(db_session: Session) -> None:
    """5-필드가 아닌 cron 표현식은 거부한다."""
    with pytest.raises(ScheduledJobConfigError):
        add_general_schedule(
            db_session, trigger_type=TRIGGER_TYPE_CRON, cron_expression="0 3 * *"
        )


def test_add_cron_with_out_of_range_field_raises(db_session: Session) -> None:
    """필드 범위를 벗어난 cron 표현식은 거부한다(분 99)."""
    with pytest.raises(ScheduledJobConfigError):
        add_general_schedule(
            db_session, trigger_type=TRIGGER_TYPE_CRON, cron_expression="99 3 * * *"
        )


def test_add_interval_without_hours_raises(db_session: Session) -> None:
    """interval 트리거인데 interval_hours 가 없으면 거부한다."""
    with pytest.raises(ScheduledJobConfigError):
        add_general_schedule(db_session, trigger_type=TRIGGER_TYPE_INTERVAL)


@pytest.mark.parametrize("invalid_hours", [0, -1, 25, 100])
def test_add_interval_out_of_range_raises(
    db_session: Session, invalid_hours: int
) -> None:
    """interval_hours 가 1~24 범위를 벗어나면 거부한다."""
    with pytest.raises(ScheduledJobConfigError):
        add_general_schedule(
            db_session,
            trigger_type=TRIGGER_TYPE_INTERVAL,
            interval_hours=invalid_hours,
        )


def test_unknown_trigger_type_raises(db_session: Session) -> None:
    """알 수 없는 trigger_type 은 거부한다."""
    with pytest.raises(ScheduledJobConfigError):
        add_general_schedule(
            db_session, trigger_type="weekly", cron_expression="0 3 * * *"
        )


# ──────────────────────────────────────────────────────────────
# 싱글턴 upsert
# ──────────────────────────────────────────────────────────────


def test_upsert_singleton_updates_existing(db_session: Session) -> None:
    """기존 backup 싱글턴의 cron 을 갱신해도 row 가 늘지 않는다(in-place)."""
    before = list_scheduled_jobs(db_session, job_kind=JOB_KIND_BACKUP)
    assert len(before) == 1

    updated = upsert_singleton_schedule(
        db_session, job_kind=JOB_KIND_BACKUP, cron_expression="30 2 * * *"
    )
    db_session.commit()

    assert updated.cron_expression == "30 2 * * *"
    after = list_scheduled_jobs(db_session, job_kind=JOB_KIND_BACKUP)
    assert len(after) == 1
    assert after[0].cron_expression == "30 2 * * *"


def test_upsert_singleton_partial_enabled_only(db_session: Session) -> None:
    """enabled 만 지정하면 트리거 cron 은 유지된 채 활성 상태만 바뀐다."""
    original = get_singleton_schedule(db_session, JOB_KIND_DAILY_REPORT)
    assert original is not None

    updated = upsert_singleton_schedule(
        db_session, job_kind=JOB_KIND_DAILY_REPORT, enabled=True
    )
    db_session.commit()

    assert updated.enabled is True
    assert updated.cron_expression == original.cron_expression


def test_upsert_singleton_creates_when_missing(db_session: Session) -> None:
    """싱글턴 row 를 지운 뒤 upsert 하면 새로 생성된다."""
    existing = get_singleton_schedule(db_session, JOB_KIND_GC)
    assert existing is not None
    assert delete_scheduled_job(db_session, existing.id) is True
    db_session.commit()
    assert get_singleton_schedule(db_session, JOB_KIND_GC) is None

    created = upsert_singleton_schedule(
        db_session, job_kind=JOB_KIND_GC, cron_expression="0 5 * * *"
    )
    db_session.commit()
    assert created.job_kind == JOB_KIND_GC
    assert created.cron_expression == "0 5 * * *"


def test_upsert_singleton_rejects_non_singleton_kind(db_session: Session) -> None:
    """scrape_general 처럼 싱글턴이 아닌 종류는 upsert 대상이 아니다."""
    with pytest.raises(ScheduledJobConfigError):
        upsert_singleton_schedule(
            db_session,
            job_kind=JOB_KIND_SCRAPE_GENERAL,
            cron_expression="0 3 * * *",
        )


def test_upsert_singleton_rejects_both_triggers(db_session: Session) -> None:
    """cron 과 interval 을 동시에 지정하면 거부한다."""
    with pytest.raises(ScheduledJobConfigError):
        upsert_singleton_schedule(
            db_session,
            job_kind=JOB_KIND_BACKUP,
            cron_expression="0 3 * * *",
            interval_hours=6,
        )


# ──────────────────────────────────────────────────────────────
# ensure_default_seed_jobs 멱등성
# ──────────────────────────────────────────────────────────────


def test_ensure_default_seed_jobs_is_idempotent(db_session: Session) -> None:
    """이미 시드된 상태에서 다시 호출하면 0건을 추가한다."""
    assert ensure_default_seed_jobs(db_session) == 0


def test_ensure_default_seed_jobs_restores_deleted_singleton(
    db_session: Session,
) -> None:
    """운영자가 싱글턴을 지워도 재시드로 1건이 복원된다."""
    backup = get_singleton_schedule(db_session, JOB_KIND_BACKUP)
    assert backup is not None
    delete_scheduled_job(db_session, backup.id)
    db_session.commit()

    inserted = ensure_default_seed_jobs(db_session)
    db_session.commit()
    assert inserted == 1

    restored = get_singleton_schedule(db_session, JOB_KIND_BACKUP)
    assert restored is not None
    assert restored.cron_expression == "0 3 * * *"


def test_scheduled_job_table_is_mapped() -> None:
    """ScheduledJob ORM 모델이 scheduled_jobs 테이블로 매핑됐는지 확인한다."""
    assert ScheduledJob.__tablename__ == "scheduled_jobs"
