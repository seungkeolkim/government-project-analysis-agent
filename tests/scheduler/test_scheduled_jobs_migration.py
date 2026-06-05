"""scheduled_jobs 데이터 이관/시드 마이그레이션 단위 테스트 (task 00157-1).

``migrate_system_settings_to_scheduled_jobs`` 가
(a) system_settings 의 일반 수집/백업/Daily Report 스케줄을 row 로 무손실 이관하고,
(b) 두 번 적용해도 중복이 없으며(멱등),
(c) 신규 설치(빈 system_settings)에서도 backup/daily_report/gc 기본 시드를 보장하는지
검증한다.

``db_session`` fixture 의 SQLite 는 Alembic upgrade(00157-1 포함)로 스키마가 올라가며,
그 과정에서 마이그레이션이 1회 실행돼 싱글턴이 이미 시드돼 있다. 일반 수집 이관 경로를
검증하기 위해, 테스트에서 system_settings 에 레거시 JSON 을 심은 뒤 마이그레이션을
한 번 더 호출한다(멱등 함수이므로 안전).
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.db.models import ScheduledJob, SystemSetting
from app.scheduler.constants import (
    DEFAULT_GC_ORPHAN_CRON,
    JOB_KIND_BACKUP,
    JOB_KIND_DAILY_REPORT,
    JOB_KIND_GC,
    JOB_KIND_SCRAPE_GENERAL,
    SETTING_KEY_GENERAL_SCHEDULES,
    TRIGGER_TYPE_CRON,
    TRIGGER_TYPE_INTERVAL,
)
from app.scheduler.scheduled_job_migration import (
    migrate_system_settings_to_scheduled_jobs,
)
from app.scheduler.scheduled_job_store import (
    list_scheduled_jobs,
)


def _set_setting(session: Session, key: str, value: str) -> None:
    """테스트 편의용 system_settings upsert 헬퍼."""
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()


def test_fresh_install_seeds_singletons_only(db_session: Session) -> None:
    """빈 system_settings 에서 마이그레이션은 backup/daily_report/gc 만 시드한다."""
    backups = list_scheduled_jobs(db_session, job_kind=JOB_KIND_BACKUP)
    dailies = list_scheduled_jobs(db_session, job_kind=JOB_KIND_DAILY_REPORT)
    gcs = list_scheduled_jobs(db_session, job_kind=JOB_KIND_GC)
    scrapes = list_scheduled_jobs(db_session, job_kind=JOB_KIND_SCRAPE_GENERAL)

    assert len(backups) == 1
    assert backups[0].cron_expression == "0 3 * * *"
    assert len(dailies) == 1
    assert dailies[0].cron_expression == "0 9 * * 1-5"
    assert dailies[0].enabled is False
    assert len(gcs) == 1
    assert gcs[0].cron_expression == DEFAULT_GC_ORPHAN_CRON
    assert scrapes == []


def test_migrates_general_schedules_from_system_settings(
    db_session: Session,
) -> None:
    """system_settings 의 일반 수집 JSON 이 scrape_general row 로 무손실 이관된다."""
    legacy = [
        {
            "id": "a",
            "mode": "cron",
            "cron_expression": "0 1 * * *",
            "interval_hours": None,
            "active_sources": ["iris"],
            "enabled": True,
        },
        {
            "id": "b",
            "mode": "interval",
            "cron_expression": None,
            "interval_hours": 6,
            "active_sources": [],
            "enabled": False,
        },
    ]
    _set_setting(
        db_session, SETTING_KEY_GENERAL_SCHEDULES, json.dumps(legacy)
    )
    db_session.commit()

    inserted = migrate_system_settings_to_scheduled_jobs(db_session.connection())
    db_session.commit()
    assert inserted == 2  # 싱글턴은 이미 존재하므로 scrape 2건만 신규.

    scrapes = list_scheduled_jobs(db_session, job_kind=JOB_KIND_SCRAPE_GENERAL)
    assert len(scrapes) == 2

    cron_record = next(r for r in scrapes if r.trigger_type == TRIGGER_TYPE_CRON)
    assert cron_record.cron_expression == "0 1 * * *"
    assert cron_record.active_sources == ["iris"]
    assert cron_record.enabled is True

    interval_record = next(
        r for r in scrapes if r.trigger_type == TRIGGER_TYPE_INTERVAL
    )
    assert interval_record.interval_hours == 6
    assert interval_record.active_sources == []
    assert interval_record.enabled is False


def test_migration_is_idempotent(db_session: Session) -> None:
    """두 번째 적용은 중복 없이 0건을 추가한다(scrape + 싱글턴 모두)."""
    legacy = [
        {
            "id": "a",
            "mode": "cron",
            "cron_expression": "0 1 * * *",
            "active_sources": ["iris", "ntis"],
            "enabled": True,
        }
    ]
    _set_setting(
        db_session, SETTING_KEY_GENERAL_SCHEDULES, json.dumps(legacy)
    )
    db_session.commit()

    first = migrate_system_settings_to_scheduled_jobs(db_session.connection())
    db_session.commit()
    assert first == 1

    total_after_first = len(list_scheduled_jobs(db_session))

    second = migrate_system_settings_to_scheduled_jobs(db_session.connection())
    db_session.commit()
    assert second == 0

    assert len(list_scheduled_jobs(db_session)) == total_after_first


def test_migration_dedupes_equivalent_schedules(db_session: Session) -> None:
    """active_sources 순서만 다른 동일 스케줄은 중복으로 보고 한 번만 넣는다."""
    legacy = [
        {
            "mode": "cron",
            "cron_expression": "0 2 * * *",
            "active_sources": ["iris", "ntis"],
            "enabled": True,
        },
        {
            "mode": "cron",
            "cron_expression": "0 2 * * *",
            "active_sources": ["ntis", "iris"],
            "enabled": True,
        },
    ]
    _set_setting(
        db_session, SETTING_KEY_GENERAL_SCHEDULES, json.dumps(legacy)
    )
    db_session.commit()

    inserted = migrate_system_settings_to_scheduled_jobs(db_session.connection())
    db_session.commit()
    assert inserted == 1

    scrapes = list_scheduled_jobs(db_session, job_kind=JOB_KIND_SCRAPE_GENERAL)
    assert len(scrapes) == 1


def test_migration_preserves_non_schedule_settings(db_session: Session) -> None:
    """메일/백업의 비-스케줄 설정 키는 마이그레이션 후에도 그대로 남는다."""
    _set_setting(db_session, "backup.max_count", "10")
    _set_setting(db_session, "email.smtp.client_secret", "secret-token")
    db_session.commit()

    migrate_system_settings_to_scheduled_jobs(db_session.connection())
    db_session.commit()

    assert db_session.get(SystemSetting, "backup.max_count").value == "10"
    assert (
        db_session.get(SystemSetting, "email.smtp.client_secret").value
        == "secret-token"
    )


def test_migration_reads_backup_cron_from_system_settings(
    db_session: Session,
) -> None:
    """백업 싱글턴이 없을 때 backup.cron_expression 값을 그대로 이관한다."""
    # 기존 시드 backup 싱글턴을 제거해 '이관' 경로를 강제한다.
    for model in (
        db_session.query(ScheduledJob)
        .filter(ScheduledJob.job_kind == JOB_KIND_BACKUP)
        .all()
    ):
        db_session.delete(model)
    _set_setting(db_session, "backup.cron_expression", "15 4 * * *")
    db_session.commit()

    migrate_system_settings_to_scheduled_jobs(db_session.connection())
    db_session.commit()

    backups = list_scheduled_jobs(db_session, job_kind=JOB_KIND_BACKUP)
    assert len(backups) == 1
    assert backups[0].cron_expression == "15 4 * * *"


def test_migration_reads_daily_report_enabled_from_system_settings(
    db_session: Session,
) -> None:
    """Daily Report 싱글턴 이관 시 enabled=true 설정이 보존된다."""
    for model in (
        db_session.query(ScheduledJob)
        .filter(ScheduledJob.job_kind == JOB_KIND_DAILY_REPORT)
        .all()
    ):
        db_session.delete(model)
    _set_setting(db_session, "email.daily_report.enabled", "true")
    _set_setting(db_session, "email.daily_report.cron_expression", "0 8 * * *")
    db_session.commit()

    migrate_system_settings_to_scheduled_jobs(db_session.connection())
    db_session.commit()

    dailies = list_scheduled_jobs(db_session, job_kind=JOB_KIND_DAILY_REPORT)
    assert len(dailies) == 1
    assert dailies[0].cron_expression == "0 8 * * *"
    assert dailies[0].enabled is True
