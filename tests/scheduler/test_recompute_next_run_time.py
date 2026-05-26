"""스케줄러 startup 시 ``next_run_time`` 강제 재계산 회귀 테스트 (task 00149-1).

사용자 원문 task 00149 가 진단한 영구 누락 시나리오의 핵심 가드:

    - daily-report 잡의 ``next_run_time`` 이 **과거 시각**(stale) 으로 jobstore
      에 박혀 있어도, 컨테이너 재기동 직후 ``start()`` 가 cron 표현식을 다시
      파싱해 \"now 이후 첫 fire-time\" 으로 자동 advance 되어야 한다.
    - APScheduler 의 ``SQLAlchemyJobStore`` 는 ``next_run_time`` 을 절대 epoch
      float 으로 저장한다 — 시스템이 그 시각을 놓치고 살아나면 \"발화 없이\"
      stale 시각이 영구 보존되는 구조적 결함이 있다. 본 task 의
      ``_recompute_all_jobs_next_run_time`` 이 이 결함을 startup 단계에서 차단
      한다.

검증 매트릭스:
    1. cron 잡(KST timezone) 의 stale next_run_time(과거) 이 ``start()`` 직후
       cron 의 다음 fire-time(=now 이후) 으로 advance 된다.
    2. legacy UTC timezone 으로 직렬화된 cron 잡이 KST 기반 trigger 로 재구성
       되고 next_run_time 도 함께 갱신된다 (00040-4 회귀 보호 — 본 task 가
       _reinterpret_existing_jobs_to_kst 의 책임을 흡수).
    3. \"컨테이너 재기동 시뮬레이션\" — stop → 등록 잡들의 stale next_run_time
       조작 → start → backup/daily-report/일반 cron 3종 모두 현재 시각 이후로
       갱신된다.

설계:
    - 모듈 수준 ``_scheduler`` 싱글턴은 fixture 가 매 테스트 시작·종료 시
      ``start_scheduler``/``stop_scheduler`` 로 격리한다 (기존
      ``test_daily_report_schedule.py`` 컨벤션).
    - stale next_run_time 시뮬레이션은 ``scheduler.modify_job(next_run_time=…)``
      직접 호출로 만든다. modify_job 은 trigger.get_next_fire_time 을 거치지
      않고 그대로 박기 때문에 \"jobstore 에 과거 절대 시각이 남은 상태\"를
      결정적으로 재현할 수 있다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from app.scheduler import (
    JOB_ID_DAILY_REPORT,
    add_cron_schedule,
    register_backup_cron_schedule,
    register_daily_report_cron_schedule,
    start_scheduler,
    stop_scheduler,
)
from app.scheduler.service import (
    _get_or_build_scheduler,
    _recompute_all_jobs_next_run_time,
)
from app.timezone import KST, now_utc

# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def running_scheduler(test_engine: Engine) -> Iterator[None]:
    """테스트별 격리된 BackgroundScheduler 를 기동·종료한다.

    service 의 ``stop_scheduler`` 가 모듈 싱글턴을 None 으로 리셋하므로, 다음
    테스트의 ``start_scheduler`` 호출이 새 BackgroundScheduler + 새 jobstore
    를 생성한다 — 잡 누수 방지.
    """
    start_scheduler()
    try:
        yield
    finally:
        # wait=False — 잡 trigger 가 실제로 발화하지 않는 \"먼 미래\" 표현식이라
        # 즉시 종료해도 안전.
        stop_scheduler(wait=False)


def _force_stale_next_run_time(job_id: str, *, minutes_in_past: int = 60) -> datetime:
    """주어진 job 의 ``next_run_time`` 을 강제로 과거 시각으로 박는다.

    \"jobstore 에 절대 시각이 stale 한 채 남아 있는 상태\" 를 결정적으로 재현
    하기 위한 헬퍼. ``modify_job(next_run_time=…)`` 은 trigger 의
    ``get_next_fire_time`` 을 거치지 않고 그대로 박기 때문에 의도된 stale 상태
    를 만들 수 있다 (``reschedule_job`` 과의 차이가 본 회귀의 핵심).

    Args:
        job_id: 조작할 잡의 ID.
        minutes_in_past: 현재(UTC) 보다 몇 분 과거로 박을지. 기본 60분.

    Returns:
        실제로 설정한 stale ``next_run_time`` (UTC tz-aware).
    """
    scheduler = _get_or_build_scheduler()
    stale_time = now_utc() - timedelta(minutes=minutes_in_past)
    scheduler.modify_job(job_id, next_run_time=stale_time)
    return stale_time


def _get_next_run_time(job_id: str) -> datetime | None:
    """현재 jobstore 에서 잡의 ``next_run_time`` 을 직접 조회한다."""
    scheduler = _get_or_build_scheduler()
    job = scheduler.get_job(job_id)
    assert job is not None, f"잡 id={job_id} 를 찾을 수 없습니다."
    return job.next_run_time


# ──────────────────────────────────────────────────────────────
# (1) stale 일반 cron 잡 → 현재 시각 이후로 advance
# ──────────────────────────────────────────────────────────────


def test_recompute_advances_stale_kst_cron_job_past_to_future(
    running_scheduler: None,
) -> None:
    """KST timezone 인 일반 cron 잡의 stale next_run_time 이 현재 시각 이후로 advance.

    재현: ``*/5 * * * *`` (5분마다) cron 잡을 등록한 뒤 ``next_run_time`` 을
    1시간 과거로 강제 조작 → ``_recompute_all_jobs_next_run_time`` 호출 →
    next_run_time 이 현재 시각보다 미래로 advance 된 것을 확인.

    본 함수는 \"일반 cron 잡 (add_cron_schedule 경로)\" 의 startup 자동 보정을
    가드한다 — 백업/daily-report 잡과 달리 ensure_*_cron_registered 보호가
    없는 잡 종류라 본 task 의 핵심 fix 대상이다.
    """
    summary = add_cron_schedule(
        cron_expression="*/5 * * * *", active_sources=[], enabled=True,
    )
    assert summary.next_run_time is not None

    # stale 상태 만들기 — 1시간 과거로 박는다.
    stale_time = _force_stale_next_run_time(summary.job_id, minutes_in_past=60)
    assert _get_next_run_time(summary.job_id) == stale_time

    # 본 task 의 핵심 호출 — startup 재계산을 시뮬레이션.
    scheduler = _get_or_build_scheduler()
    recomputed = _recompute_all_jobs_next_run_time(scheduler)
    assert recomputed >= 1, "최소 1건은 재계산되어야 한다."

    # next_run_time 이 현재 시각 이후로 advance 됐는지 검증.
    after = _get_next_run_time(summary.job_id)
    assert after is not None
    assert after > now_utc(), (
        f"stale next_run_time 이 advance 되지 않았다: {after}"
    )
    # cron 의 다음 fire-time 이라 매우 가까운 미래여야 한다 (최대 5분 + buffer).
    assert after - now_utc() <= timedelta(minutes=6)


def test_recompute_advances_stale_backup_cron_job(running_scheduler: None) -> None:
    """백업 cron 잡의 stale next_run_time 이 cron 기반 다음 fire-time 으로 advance."""
    summary = register_backup_cron_schedule(cron_expression="*/10 * * * *")
    assert summary.next_run_time is not None

    _force_stale_next_run_time(summary.job_id, minutes_in_past=30)

    scheduler = _get_or_build_scheduler()
    _recompute_all_jobs_next_run_time(scheduler)

    after = _get_next_run_time(summary.job_id)
    assert after is not None
    assert after > now_utc()
    assert after - now_utc() <= timedelta(minutes=11)


def test_recompute_advances_stale_daily_report_cron_job(
    running_scheduler: None,
) -> None:
    """Daily report cron 잡의 stale next_run_time 이 cron 기반 다음 fire-time 으로 advance.

    사용자 원문이 진단한 daily-report 09:30 누락 사고의 정확한 회귀 가드.
    cron 표현식은 매분 (``* * * * *``) 으로 둬 advance 후 1분 이내 fire-time 이
    잡혀야 한다.
    """
    summary = register_daily_report_cron_schedule(
        cron_expression="* * * * *", enabled=True,
    )
    assert summary is not None
    assert summary.next_run_time is not None

    _force_stale_next_run_time(JOB_ID_DAILY_REPORT, minutes_in_past=45)

    scheduler = _get_or_build_scheduler()
    _recompute_all_jobs_next_run_time(scheduler)

    after = _get_next_run_time(JOB_ID_DAILY_REPORT)
    assert after is not None
    assert after > now_utc()
    # 매분 cron 이라 advance 후 다음 fire-time 은 60초 이내.
    assert after - now_utc() <= timedelta(seconds=120)


# ──────────────────────────────────────────────────────────────
# (2) legacy UTC timezone 잡 → KST 기반 trigger 로 재구성
# ──────────────────────────────────────────────────────────────


def test_recompute_reinterprets_legacy_utc_cron_trigger_to_kst(
    running_scheduler: None,
) -> None:
    """UTC timezone 으로 시도된 legacy cron 잡이 KST 기반 trigger 로 자동 재구성된다.

    배경:
        task 00040-4 → 00149-1 이 ``_recompute_all_jobs_next_run_time`` 에 흡수한
        \"tz 재해석\" 책임이, task 00149-2 의 ``JsonSchedulerJobStore`` 도입으로
        **저장 레이어 자체** 에서 더 근본적으로 차단된다. 새 jobstore 는 cron 잡
        의 trigger 객체를 직렬화하지 않고 cron_expression 컬럼만 저장하고, 읽을
        때마다 ``build_cron_trigger(expr, timezone=KST)`` 로 재생성한다 — 따라서
        UTC timezone 으로 잡을 \"박을 수 있는\" 경로 자체가 없다.

    회귀 가드:
        - 호출자가 ``modify_job(trigger=UTC_trigger)`` 로 UTC trigger 를 강제로
          밀어넣어도, 다음 ``get_job`` 은 jobstore 의 reconstitute 를 거쳐
          KST trigger 를 돌려준다. (저장 레이어에서 컨벤션 강제.)
        - stale next_run_time 도 ``_recompute_all_jobs_next_run_time`` 호출 후
          현재 시각 이후로 advance 된다.
    """
    from apscheduler.triggers.cron import CronTrigger

    summary = add_cron_schedule(
        cron_expression="*/15 * * * *", active_sources=[], enabled=True,
    )
    scheduler = _get_or_build_scheduler()

    # UTC trigger 로 modify 를 시도 — JsonSchedulerJobStore 가 cron_expression 만
    # 저장하므로, 다음 lookup 에서는 KST 로 재생성된다 (저장 레이어 보호).
    utc_trigger = CronTrigger.from_crontab("*/15 * * * *", timezone=UTC)
    scheduler.modify_job(summary.job_id, trigger=utc_trigger)
    # stale next_run_time 박기 — modify_job 은 trigger.get_next_fire_time 을
    # 거치지 않아 의도된 stale 상태 재현 가능.
    stale = now_utc() - timedelta(hours=2)
    scheduler.modify_job(summary.job_id, next_run_time=stale)

    # task 00149-2 — 저장 레이어가 cron 잡 timezone 을 KST 로 자동 정정함을 확인.
    before_job = scheduler.get_job(summary.job_id)
    assert getattr(before_job.trigger, "timezone", None) == KST, (
        "JsonSchedulerJobStore 는 cron 잡 trigger 를 항상 KST 로 재구성해야 한다 "
        "(저장 레이어에서 timezone 컨벤션 강제 — task 00149-2)."
    )

    # task 00149-1 — 재계산 호출 후 stale next_run_time 이 advance 됨을 검증.
    _recompute_all_jobs_next_run_time(scheduler)

    after_job = scheduler.get_job(summary.job_id)
    assert getattr(after_job.trigger, "timezone", None) == KST
    assert after_job.next_run_time is not None
    assert after_job.next_run_time > now_utc()


# ──────────────────────────────────────────────────────────────
# (3) 컨테이너 재기동 시뮬레이션 — stop → start 후 3종 잡 모두 advance
# ──────────────────────────────────────────────────────────────


def test_container_restart_recomputes_all_jobs(
    running_scheduler: None,
) -> None:
    """컨테이너 재기동 시뮬레이션 — stop 직전 stale 박힌 잡들이 start 직후 모두 advance.

    백업·daily-report·일반 cron 3종을 모두 등록 → 강제로 stale next_run_time
    박기 → stop → start (= 컨테이너 재기동 시뮬레이션) → 3종 잡 모두
    next_run_time 이 현재 시각 이후로 갱신됐는지 검증.

    핵심: ``start()`` 가 lock 획득 + scheduler.start + 재계산을 한 흐름으로
    수행해야 함을 보장한다. APScheduler 의 jobstore 자동 복원만으로는 stale
    절대 시각이 그대로 남는다는 점이 본 task 의 진단 결론이며, 본 테스트가
    \"start 만으로도 영구 누락 차단\" 의 회귀를 가드한다.
    """
    general = add_cron_schedule(
        cron_expression="*/3 * * * *", active_sources=[], enabled=True,
    )
    backup = register_backup_cron_schedule(cron_expression="*/7 * * * *")
    daily = register_daily_report_cron_schedule(
        cron_expression="* * * * *", enabled=True,
    )
    assert daily is not None

    # 3종 모두 강제로 stale.
    _force_stale_next_run_time(general.job_id, minutes_in_past=120)
    _force_stale_next_run_time(backup.job_id, minutes_in_past=120)
    _force_stale_next_run_time(JOB_ID_DAILY_REPORT, minutes_in_past=120)

    # 재기동 시뮬레이션 — stop → start.
    stop_scheduler(wait=False)
    start_scheduler()

    # 3종 모두 현재 시각 이후로 advance 됐어야 한다.
    for job_id in (general.job_id, backup.job_id, JOB_ID_DAILY_REPORT):
        scheduler = _get_or_build_scheduler()
        job = scheduler.get_job(job_id)
        assert job is not None, f"잡 {job_id} 가 재기동 후 사라졌다"
        assert job.next_run_time is not None
        assert job.next_run_time > now_utc(), (
            f"잡 {job_id} 의 stale next_run_time 이 재기동 후 advance 되지 않았다: "
            f"{job.next_run_time}"
        )


# ──────────────────────────────────────────────────────────────
# (4) 잡이 0건일 때 — silent no-op
# ──────────────────────────────────────────────────────────────


def test_recompute_returns_zero_when_no_jobs(running_scheduler: None) -> None:
    """등록 잡이 0건이면 ``_recompute_all_jobs_next_run_time`` 은 0 반환 + no-op."""
    scheduler = _get_or_build_scheduler()
    assert _recompute_all_jobs_next_run_time(scheduler) == 0
