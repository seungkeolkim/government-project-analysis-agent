"""DB = SystemSetting 단일 SSOT 정합성 검증 (task 00156-2).

task 00156-1 백필이 레거시 ``scheduler_jobs`` 의 일반 수집 스케줄을 신규 SSOT
(SystemSetting ``scheduler.general_schedules``)로 복구한 뒤, 00156-2 의 드롭
마이그레이션이 고아가 된 ``scheduler_jobs`` 테이블을 제거한다. 이 테스트는:

- ``alembic upgrade head`` 후 ``scheduler_jobs`` 테이블이 더 이상 존재하지
  않음(드롭 마이그레이션이 실행됨),
- ``upgrade head`` 재실행이 멱등하게 성공하고 테이블이 계속 부재함,
- 테이블 부재 상태에서 일반 수집 잡 수집·crontab 렌더가 예외 없이 동작함,
- crontab 이 SystemSetting(DB) 에 저장된 항목만 일관 출력하고, DB 에 없는
  스케줄은 절대 나타나지 않으며, DB 에서 지우면 crontab 에서도 사라짐
  (DB = SSOT)

을 고정한다. ``test_engine`` / ``db_session`` fixture(tests/conftest.py)의 격리
SQLite 위에서 동작한다.
"""

from __future__ import annotations

from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session

from app.backup.constants import SETTING_KEY_BACKUP_CRON
from app.backup.service import set_setting
from app.email.constants import (
    SETTING_KEY_DAILY_REPORT_CRON,
    SETTING_KEY_DAILY_REPORT_ENABLED,
)
from app.scheduler.constants import SCHEDULER_JOBS_TABLENAME
from app.scheduler.crontab_generator import (
    CrontabEnvironment,
    collect_general_schedule_jobs,
    generate_crontab_text,
)
from app.scheduler.schedule_store import (
    SCHEDULE_MODE_CRON,
    add_general_schedule_record,
    delete_general_schedule_record,
)


def _make_environment() -> CrontabEnvironment:
    """테스트용 고정 실행 컨텍스트를 만든다."""
    return CrontabEnvironment(
        project_dir="/app",
        python_executable="/usr/local/bin/python",
        log_dir="/app/data/logs",
    )


# ──────────────────────────────────────────────────────────────
# 고아 테이블 드롭 검증
# ──────────────────────────────────────────────────────────────


def test_scheduler_jobs_table_dropped_at_head(test_engine: Engine) -> None:
    """``alembic upgrade head`` 후 레거시 scheduler_jobs 테이블이 부재한다.

    ``test_engine`` fixture 가 init_db → ``alembic upgrade head`` 를 적용하므로,
    00156-2 드롭 마이그레이션이 실행돼 고아 테이블이 사라져 있어야 한다.
    """
    inspector = inspect(test_engine)
    assert SCHEDULER_JOBS_TABLENAME not in inspector.get_table_names()


def test_upgrade_head_is_idempotent(test_engine: Engine) -> None:
    """``upgrade head`` 재실행이 멱등하게 성공하고 테이블은 계속 부재한다.

    이미 head 인 DB 에 init_db 를 다시 호출해도 예외 없이 통과하며, 드롭된
    테이블이 되살아나지 않음을 확인한다(전체 마이그레이션 흐름의 멱등성).
    """
    from app.db.init_db import init_db

    # 두 번째 호출은 alembic_version 이 이미 head 이므로 upgrade head no-op.
    init_db(test_engine)

    inspector = inspect(test_engine)
    assert SCHEDULER_JOBS_TABLENAME not in inspector.get_table_names()


# ──────────────────────────────────────────────────────────────
# DB = SSOT 정합성 (테이블 부재 상태에서 crontab 렌더)
# ──────────────────────────────────────────────────────────────


def test_general_collect_works_without_legacy_table(db_session: Session) -> None:
    """레거시 테이블이 없어도 일반 수집 잡 수집이 예외 없이 동작한다.

    일반 수집 잡은 SystemSetting(SSOT)만 읽으므로 scheduler_jobs 부재와 무관히
    정상 동작해야 한다.
    """
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 1 * * *",
    )
    db_session.commit()

    jobs = collect_general_schedule_jobs(db_session)
    assert [job.cron_expression for job in jobs] == ["0 1 * * *"]


def test_crontab_reflects_exactly_db_ssot(db_session: Session) -> None:
    """crontab 이 DB(SystemSetting)에 저장된 스케줄 전부를 일관 출력한다.

    일반 수집 2건(0 1·0 13) + 백업 + Daily Report(enabled)를 SystemSetting 에
    넣고 crontab 을 렌더하면, 그 전부가 한 번에 나타나야 한다.
    """
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 1 * * *",
    )
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 13 * * *",
    )
    set_setting(db_session, SETTING_KEY_BACKUP_CRON, "30 2 * * *")
    set_setting(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "true")
    set_setting(db_session, SETTING_KEY_DAILY_REPORT_CRON, "45 8 * * 1-5")
    db_session.commit()

    crontab_text = generate_crontab_text(db_session, _make_environment())

    scrape_lines = [
        line
        for line in crontab_text.splitlines()
        if "app.scheduler.run_job scrape" in line
    ]
    assert any(line.startswith("0 1 * * *") for line in scrape_lines)
    assert any(line.startswith("0 13 * * *") for line in scrape_lines)
    # 백업·Daily Report 도 DB 기준으로 함께 나타난다.
    assert "30 2 * * *" in crontab_text and "run_job backup" in crontab_text
    assert "45 8 * * 1-5" in crontab_text and "run_job daily-report" in crontab_text


def test_crontab_omits_schedule_not_in_db(db_session: Session) -> None:
    """DB 에 없는 스케줄은 crontab 에 절대 나타나지 않는다(DB = SSOT).

    사용자 보고의 핵심 증상('DB 엔 없는데 어딘가에 남아 있다')의 역방향 가드:
    SystemSetting 에 저장하지 않은 표현식(15 11)은 crontab 에서 보이지 않는다.
    """
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 1 * * *",
    )
    db_session.commit()

    crontab_text = generate_crontab_text(db_session, _make_environment())

    assert "0 1 * * *" in crontab_text
    # DB 에 넣지 않은 15 11 은 어디에도 없어야 한다.
    assert "15 11 * * *" not in crontab_text


def test_crontab_drops_schedule_after_db_delete(db_session: Session) -> None:
    """DB 에서 스케줄을 삭제하면 crontab 에서도 사라진다(SSOT 단방향성).

    재기동 정합성 시나리오: 화면/저장소에서 삭제한 스케줄은 다음 crontab
    재생성 시점부터 출력되지 않아야 한다.
    """
    keep = add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 1 * * *",
    )
    drop = add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 13 * * *",
    )
    db_session.commit()

    before = generate_crontab_text(db_session, _make_environment())
    assert "0 1 * * *" in before and "0 13 * * *" in before

    assert delete_general_schedule_record(db_session, drop.id) is True
    db_session.commit()

    after = generate_crontab_text(db_session, _make_environment())
    # 남긴 것은 유지, 지운 것은 사라짐.
    assert "0 1 * * *" in after
    scrape_lines = [
        line for line in after.splitlines() if "run_job scrape" in line
    ]
    assert not any(line.startswith("0 13 * * *") for line in scrape_lines)
    # keep 레코드는 그대로 존재.
    assert keep.cron_expression == "0 1 * * *"
