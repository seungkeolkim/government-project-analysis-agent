"""crontab 텍스트 생성기 단위 테스트 (task 00155-2).

순수 렌더러(:func:`render_crontab`)는 고정 입력으로 출력 문자열을 직접
검증하고, DB 빌더(:func:`collect_*`, :func:`generate_crontab_text`)는
``db_session`` fixture 위에서 SystemSetting/저장소를 채운 뒤 검증한다.

핵심 회귀 가드:
    - 요일 필드가 APScheduler 보정 없이 표준 crontab 규약 그대로 출력된다.
    - 비활성/빈 cron 스케줄은 라인에서 제외된다.
    - interval('매 N시간')이 동등한 cron 표현식으로 변환된다.
    - 각 잡 라인이 00155-1 CLI 를 호출하고 cron 환경 비움 대비 .env 로딩을
      포함한다.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.backup.constants import DEFAULT_BACKUP_CRON, SETTING_KEY_BACKUP_CRON
from app.backup.service import set_setting
from app.email.constants import (
    SETTING_KEY_DAILY_REPORT_CRON,
    SETTING_KEY_DAILY_REPORT_ENABLED,
)
from app.scheduler.constants import DEFAULT_GC_ORPHAN_CRON
from app.scheduler.crontab_generator import (
    CrontabEnvironment,
    CrontabJob,
    LOG_FILENAME_GC,
    LOG_FILENAME_SCRAPE,
    RUN_JOB_MODULE,
    build_crontab_jobs,
    collect_general_schedule_jobs,
    collect_system_jobs,
    generate_crontab_text,
    interval_hours_to_cron_expression,
    render_crontab,
)
from app.scheduler.schedule_store import (
    SCHEDULE_MODE_CRON,
    SCHEDULE_MODE_INTERVAL,
    add_general_schedule_record,
)


def _make_environment(**overrides: object) -> CrontabEnvironment:
    """테스트용 고정 실행 컨텍스트를 만든다."""
    base = {
        "project_dir": "/app",
        "python_executable": "/usr/local/bin/python",
        "log_dir": "/app/data/logs",
    }
    base.update(overrides)
    return CrontabEnvironment(**base)  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────
# interval → cron 변환
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (1, "0 * * * *"),
        (2, "0 */2 * * *"),
        (6, "0 */6 * * *"),
        (12, "0 */12 * * *"),
        (24, "0 */24 * * *"),
    ],
)
def test_interval_hours_to_cron(hours: int, expected: str) -> None:
    """'매 N시간' 이 ``0 */N * * *`` 규칙으로 변환된다(N=1 은 ``0 * * * *``)."""
    assert interval_hours_to_cron_expression(hours) == expected


@pytest.mark.parametrize("invalid", [0, -1, 25])
def test_interval_out_of_range_raises(invalid: int) -> None:
    """1~24 범위 밖 interval 은 거부한다."""
    with pytest.raises(ValueError):
        interval_hours_to_cron_expression(invalid)


# ──────────────────────────────────────────────────────────────
# 순수 렌더러
# ──────────────────────────────────────────────────────────────


def test_render_header_contains_timezone_and_shell() -> None:
    """헤더에 CRON_TZ·SHELL 이 들어간다."""
    text = render_crontab([], _make_environment())
    assert "CRON_TZ=Asia/Seoul" in text
    assert "SHELL=/bin/bash" in text
    assert text.endswith("\n")


def test_render_extra_environment_lines() -> None:
    """extra_environment 가 헤더 KEY=VALUE 라인으로 정렬돼 출력된다."""
    env = _make_environment(
        extra_environment={"PATH": "/usr/bin:/bin", "HOST_PROJECT_DIR": "/srv/app"}
    )
    text = render_crontab([], env)
    assert "PATH=/usr/bin:/bin" in text
    assert "HOST_PROJECT_DIR=/srv/app" in text


def test_render_job_line_has_cli_env_loading_and_log_redirect() -> None:
    """잡 라인이 cron 표현식 + .env 로딩 래퍼 + CLI + 로그 redirect 를 포함한다."""
    job = CrontabJob(
        comment="[공고 수집] cron sources=iris",
        cron_expression="0 */6 * * *",
        cli_arguments=["scrape", "--sources", "iris"],
        log_filename=LOG_FILENAME_SCRAPE,
    )
    text = render_crontab([job], _make_environment())

    # cron 표현식이 라인 맨 앞에 그대로.
    assert "0 */6 * * * cd /app &&" in text
    # cron 환경 비움 대비 .env 로딩.
    assert "set -a && . .env && set +a" in text
    # 00155-1 CLI 호출(절대경로 python + -m run_job 모듈).
    assert f"/usr/local/bin/python -m {RUN_JOB_MODULE} scrape --sources iris" in text
    # stdout/stderr redirect.
    assert ">> /app/data/logs/cron-scrape.log 2>&1" in text
    # 주석.
    assert "# [공고 수집] cron sources=iris" in text


def test_render_day_of_week_is_not_corrected() -> None:
    """요일 필드가 APScheduler 보정 없이 표준 crontab 그대로 출력된다.

    '0 7 * * 1-5'(월~금)가 보정으로 '0 7 * * 0-4' 등으로 바뀌면 안 된다.
    """
    job = CrontabJob(
        comment="weekday",
        cron_expression="0 7 * * 1-5",
        cli_arguments=["scrape"],
        log_filename=LOG_FILENAME_SCRAPE,
    )
    text = render_crontab([job], _make_environment())
    assert "0 7 * * 1-5 cd /app" in text
    assert "0-4" not in text


# ──────────────────────────────────────────────────────────────
# 일반 수집 잡 수집(저장소 기반)
# ──────────────────────────────────────────────────────────────


def test_collect_general_skips_disabled(db_session: Session) -> None:
    """비활성 일반 수집 스케줄은 잡에서 제외된다."""
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 3 * * *",
        enabled=False,
    )
    db_session.commit()
    assert collect_general_schedule_jobs(db_session) == []


def test_collect_general_converts_interval(db_session: Session) -> None:
    """interval 스케줄이 cron 표현식 잡으로 변환된다."""
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_INTERVAL,
        interval_hours=6,
        active_sources=["iris"],
    )
    db_session.commit()

    jobs = collect_general_schedule_jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].cron_expression == "0 */6 * * *"
    assert jobs[0].cli_arguments == ["scrape", "--sources", "iris"]


def test_collect_general_empty_sources_omits_sources_flag(
    db_session: Session,
) -> None:
    """active_sources 가 비면 --sources 인자를 붙이지 않는다(전체 수집)."""
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 3 * * *",
        active_sources=[],
    )
    db_session.commit()

    jobs = collect_general_schedule_jobs(db_session)
    assert jobs[0].cli_arguments == ["scrape"]


# ──────────────────────────────────────────────────────────────
# 시스템 잡(백업/Daily Report/GC)
# ──────────────────────────────────────────────────────────────


def test_collect_system_jobs_defaults(db_session: Session) -> None:
    """설정이 없으면 백업(기본 cron)과 GC 가 포함되고 Daily Report 는 제외된다."""
    jobs = collect_system_jobs(db_session)
    comments = [job.comment for job in jobs]

    # 백업: 기본 cron.
    backup_job = next(job for job in jobs if job.cli_arguments == ["backup"])
    assert backup_job.cron_expression == DEFAULT_BACKUP_CRON
    # GC: 기본 cron, 항상 포함.
    gc_job = next(job for job in jobs if job.cli_arguments == ["gc"])
    assert gc_job.cron_expression == DEFAULT_GC_ORPHAN_CRON
    assert gc_job.log_filename == LOG_FILENAME_GC
    # Daily Report: enabled=False(미설정) 이므로 제외.
    assert not any("daily-report" in job.cli_arguments for job in jobs)
    assert any("DB 백업" in comment for comment in comments)


def test_collect_system_jobs_daily_report_enabled(db_session: Session) -> None:
    """Daily Report 가 enabled=true 면 저장된 cron 으로 포함된다."""
    set_setting(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "true")
    set_setting(db_session, SETTING_KEY_DAILY_REPORT_CRON, "0 9 * * 1-5")
    db_session.commit()

    jobs = collect_system_jobs(db_session)
    daily_job = next(
        job for job in jobs if job.cli_arguments == ["daily-report"]
    )
    assert daily_job.cron_expression == "0 9 * * 1-5"


def test_collect_system_jobs_respects_custom_backup_cron(
    db_session: Session,
) -> None:
    """저장된 backup cron 이 있으면 그 값을 쓴다."""
    set_setting(db_session, SETTING_KEY_BACKUP_CRON, "30 2 * * *")
    db_session.commit()

    jobs = collect_system_jobs(db_session)
    backup_job = next(job for job in jobs if job.cli_arguments == ["backup"])
    assert backup_job.cron_expression == "30 2 * * *"


# ──────────────────────────────────────────────────────────────
# end-to-end
# ──────────────────────────────────────────────────────────────


def test_generate_crontab_text_end_to_end(db_session: Session) -> None:
    """일반 수집 N건 + 백업 + Daily Report + GC 조합이 기대 crontab 으로 렌더된다."""
    # 일반 수집 2건(cron 1 + interval 1) + 비활성 1건.
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 7 * * 1-5",
        active_sources=["iris", "ntis"],
    )
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_INTERVAL,
        interval_hours=12,
    )
    add_general_schedule_record(
        db_session,
        mode=SCHEDULE_MODE_CRON,
        cron_expression="0 1 * * *",
        enabled=False,  # 제외 대상
    )
    # Daily Report 활성화.
    set_setting(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "true")
    set_setting(db_session, SETTING_KEY_DAILY_REPORT_CRON, "0 9 * * 1-5")
    db_session.commit()

    text = generate_crontab_text(db_session, _make_environment())

    # 일반 수집 cron(요일 보정 없이) + sources.
    assert "0 7 * * 1-5 cd /app" in text
    assert "scrape --sources iris,ntis" in text
    # interval 변환.
    assert "0 */12 * * * cd /app" in text
    # 비활성 스케줄은 제외.
    assert "0 1 * * *" not in text
    # 백업/Daily Report/GC.
    assert f"-m {RUN_JOB_MODULE} backup" in text
    assert f"-m {RUN_JOB_MODULE} daily-report" in text
    assert f"-m {RUN_JOB_MODULE} gc" in text
    assert f"{DEFAULT_GC_ORPHAN_CRON} cd /app" in text

    # 잡 개수 검증: 일반 2 + 백업 1 + daily 1 + gc 1 = 5.
    jobs = build_crontab_jobs(db_session)
    assert len(jobs) == 5
