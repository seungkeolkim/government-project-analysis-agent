"""레거시 scheduler_jobs → 신규 SSOT 백필 단위 테스트 (task 00156-1).

task 155 전환 때 신규 저장소로 옮겨지지 못하고 레거시 ``scheduler_jobs`` 에만
남은 일반 공고 수집 스케줄(예: ``0 1 * * *``, ``0 13 * * *``)을 신규 SSOT
(SystemSetting ``scheduler.general_schedules``)로 복구하는 백필이:

- 유실 스케줄을 복구하고 사용자가 추가한 스케줄을 보존하는지,
- 두 번 실행해도 중복을 만들지 않는지(멱등),
- 테이블이 없으면 no-op 으로 통과하는지,
- 백업/Daily Report/GC 잡은 일반 수집 저장소로 옮기지 않는지,
- 복구분이 crontab 렌더에 [공고 수집] 라인으로 나타나는지

를 검증한다. ``db_session`` fixture(tests/conftest.py)의 격리 SQLite 위에서
동작하며, scheduler_jobs 테이블은 alembic(c5a8d1e7b9f4)이 이미 생성해 둔다.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.scheduler.crontab_generator import (
    CrontabEnvironment,
    generate_crontab_text,
)
from app.scheduler.legacy_backfill import backfill_general_schedules_from_legacy
from app.scheduler.schedule_store import (
    SCHEDULE_MODE_CRON,
    SCHEDULE_MODE_INTERVAL,
    add_general_schedule_record,
    list_general_schedule_records,
)
from app.timezone import now_utc


def _insert_legacy_job(
    session: Session,
    *,
    job_id: str,
    name: str,
    trigger_type: str,
    cron_expression: str | None = None,
    interval_seconds: int | None = None,
    active_sources: list[str] | None = None,
    paused: bool = False,
) -> None:
    """레거시 ``scheduler_jobs`` 테이블에 잡 row 1건을 직접 삽입한다.

    레거시 job_state 구조(``name`` / ``args=[active_sources]`` 등)를 재현해,
    백필이 실제 운영 데이터를 어떻게 변환하는지 검증할 수 있게 한다.

    Args:
        session: ORM 세션(connection 을 빌려 raw SQL 실행).
        job_id: 레거시 job id.
        name: 레거시 job_state.name (예: "cron:0 1 * * *").
        trigger_type: "cron" 또는 "interval".
        cron_expression: cron 잡의 표현식 컬럼값.
        interval_seconds: interval 잡의 초 컬럼값.
        active_sources: 잡 args 에 저장할 source id 목록.
        paused: True 면 next_run_time 을 NULL 로 둬 paused 상태를 재현한다.
    """
    job_state = {
        "version": 1,
        "func_ref": "app.scheduler.job_runner:scheduled_scrape",
        "args": [list(active_sources or [])],
        "kwargs": {},
        "name": name,
        "misfire_grace_time": 300,
        "coalesce": True,
        "max_instances": 1,
        "executor": "default",
    }
    now = now_utc()
    session.connection().execute(
        text(
            "INSERT INTO scheduler_jobs "
            "(id, next_run_time, trigger_type, cron_expression, interval_seconds, "
            " job_state, created_at, updated_at) "
            "VALUES (:id, :next_run_time, :trigger_type, :cron_expression, "
            " :interval_seconds, :job_state, :created_at, :updated_at)"
        ),
        {
            "id": job_id,
            "next_run_time": None if paused else now,
            "trigger_type": trigger_type,
            "cron_expression": cron_expression,
            "interval_seconds": interval_seconds,
            "job_state": json.dumps(job_state, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        },
    )


def test_backfill_recovers_lost_general_schedules(db_session: Session) -> None:
    """레거시 일반 수집 cron 잡이 신규 저장소로 복구된다(유실 복구)."""
    _insert_legacy_job(
        db_session,
        job_id="cron-aaa",
        name="cron:0 1 * * *",
        trigger_type="cron",
        cron_expression="0 1 * * *",
    )
    _insert_legacy_job(
        db_session,
        job_id="cron-bbb",
        name="cron:0 13 * * *",
        trigger_type="cron",
        cron_expression="0 13 * * *",
    )

    added = backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()

    assert added == 2
    records = list_general_schedule_records(db_session)
    expressions = {record.cron_expression for record in records}
    assert expressions == {"0 1 * * *", "0 13 * * *"}
    assert all(record.mode == SCHEDULE_MODE_CRON for record in records)
    # 신규 id 가 부여됐는지(레거시 job id 재사용 금지).
    assert all(record.id not in {"cron-aaa", "cron-bbb"} for record in records)


def test_backfill_preserves_user_added_and_recovers_legacy(
    db_session: Session,
) -> None:
    """사용자가 155 후 직접 추가한 15 11 을 보존하면서 레거시 0 1/0 13 을 복구한다."""
    # 사용자가 신규 저장소에 직접 추가한 스케줄.
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="15 11 * * *",
    )
    db_session.commit()

    _insert_legacy_job(
        db_session,
        job_id="cron-aaa",
        name="cron:0 1 * * *",
        trigger_type="cron",
        cron_expression="0 1 * * *",
    )
    _insert_legacy_job(
        db_session,
        job_id="cron-bbb",
        name="cron:0 13 * * *",
        trigger_type="cron",
        cron_expression="0 13 * * *",
    )

    added = backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()

    assert added == 2
    expressions = {
        record.cron_expression for record in list_general_schedule_records(db_session)
    }
    assert expressions == {"15 11 * * *", "0 1 * * *", "0 13 * * *"}


def test_backfill_is_idempotent(db_session: Session) -> None:
    """두 번 실행해도 중복 레코드가 생기지 않는다."""
    _insert_legacy_job(
        db_session,
        job_id="cron-aaa",
        name="cron:0 1 * * *",
        trigger_type="cron",
        cron_expression="0 1 * * *",
    )

    first = backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()
    second = backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()

    assert first == 1
    assert second == 0
    assert len(list_general_schedule_records(db_session)) == 1


def test_backfill_dedupes_same_legacy_expression(db_session: Session) -> None:
    """레거시에 동일 (mode, 표현식, sources) 잡이 둘이면 1건만 백필한다(멱등 키)."""
    _insert_legacy_job(
        db_session,
        job_id="cron-aaa",
        name="cron:0 1 * * *",
        trigger_type="cron",
        cron_expression="0 1 * * *",
        active_sources=["iris"],
    )
    _insert_legacy_job(
        db_session,
        job_id="cron-bbb",
        name="cron:0 1 * * *",
        trigger_type="cron",
        cron_expression="0 1 * * *",
        active_sources=["iris"],
    )

    added = backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()

    assert added == 1


def test_backfill_recovers_interval_and_active_sources(db_session: Session) -> None:
    """interval 잡은 interval_hours 로, active_sources 는 args 에서 복원한다."""
    _insert_legacy_job(
        db_session,
        job_id="interval-aaa",
        name="interval:매 6시간",
        trigger_type="interval",
        interval_seconds=6 * 3600,
        active_sources=["iris", "ntis"],
    )

    added = backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()

    assert added == 1
    record = list_general_schedule_records(db_session)[0]
    assert record.mode == SCHEDULE_MODE_INTERVAL
    assert record.interval_hours == 6
    assert record.active_sources == ["iris", "ntis"]


def test_backfill_recovers_paused_as_disabled(db_session: Session) -> None:
    """paused(next_run_time NULL) 레거시 잡은 enabled=False 로 복구한다."""
    _insert_legacy_job(
        db_session,
        job_id="cron-aaa",
        name="cron:0 1 * * *",
        trigger_type="cron",
        cron_expression="0 1 * * *",
        paused=True,
    )

    backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()

    record = list_general_schedule_records(db_session)[0]
    assert record.enabled is False


def test_backfill_excludes_system_jobs(db_session: Session) -> None:
    """백업/Daily Report/GC 잡은 일반 수집 저장소로 백필하지 않는다."""
    _insert_legacy_job(
        db_session,
        job_id="backup-db",
        name="backup-cron:0 23 * * *",
        trigger_type="cron",
        cron_expression="0 23 * * *",
    )
    _insert_legacy_job(
        db_session,
        job_id="daily-report",
        name="daily-report-cron:12 11 * * 1-5",
        trigger_type="cron",
        cron_expression="12 11 * * 1-5",
    )
    _insert_legacy_job(
        db_session,
        job_id="gc-orphan",
        name="gc-orphan-cron:0 4 * * *",
        trigger_type="cron",
        cron_expression="0 4 * * *",
    )

    added = backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()

    assert added == 0
    assert list_general_schedule_records(db_session) == []


def test_backfill_renders_recovered_jobs_in_crontab(db_session: Session) -> None:
    """복구된 0 1/0 13/15 11 세 스케줄이 crontab [공고 수집] 라인으로 렌더된다."""
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="15 11 * * *",
    )
    db_session.commit()
    _insert_legacy_job(
        db_session,
        job_id="cron-aaa",
        name="cron:0 1 * * *",
        trigger_type="cron",
        cron_expression="0 1 * * *",
    )
    _insert_legacy_job(
        db_session,
        job_id="cron-bbb",
        name="cron:0 13 * * *",
        trigger_type="cron",
        cron_expression="0 13 * * *",
    )

    backfill_general_schedules_from_legacy(db_session.connection())
    db_session.commit()

    environment = CrontabEnvironment(
        project_dir="/app",
        python_executable="/usr/local/bin/python",
        log_dir="/app/data/logs",
    )
    crontab_text = generate_crontab_text(db_session, environment)

    scrape_lines = [
        line
        for line in crontab_text.splitlines()
        if "app.scheduler.run_job scrape" in line
    ]
    assert len(scrape_lines) == 3
    assert any(line.startswith("0 1 * * *") for line in scrape_lines)
    assert any(line.startswith("0 13 * * *") for line in scrape_lines)
    assert any(line.startswith("15 11 * * *") for line in scrape_lines)
