"""APScheduler BackgroundScheduler 싱글턴 관리 + CRUD API.

설계 요약:
    - ``BackgroundScheduler`` 1개를 웹 프로세스 내부에서 가동 (별도 스레드).
    - JobStore 는 ``SQLAlchemyJobStore(engine=..., tablename='scheduler_jobs')``
      하나만 사용. 재기동 후 scheduler_jobs 테이블에서 잡을 자동 복원한다.
    - 실제 수집 실행은 :func:`app.scheduler.job_runner.scheduled_scrape` 가
      담당 (pickle-safe top-level 함수).

주의:
    - APScheduler 3.x 는 4.x 와 API 가 크게 다르다. pyproject.toml 에
      ``apscheduler>=3.10,<4.0`` 으로 고정되어 있다.
    - 스케줄러 인스턴스는 모듈 수준 싱글턴. 테스트에서 재시작이 필요하면
      :func:`stop` 을 먼저 호출해 None 으로 리셋한 뒤 :func:`start` 한다.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from app.db.session import get_engine
from app.scheduler.constants import (
    DEFAULT_MISFIRE_GRACE_TIME_SEC,
    JOB_ID_PREFIX_CRON,
    JOB_ID_PREFIX_INTERVAL,
    JOB_NAME_CRON_PREFIX,
    JOB_NAME_INTERVAL_PREFIX,
    MAX_INTERVAL_HOURS,
    SCHEDULER_JOBS_TABLENAME,
)
from app.scheduler.job_runner import scheduled_scrape

# ──────────────────────────────────────────────────────────────
# 예외
# ──────────────────────────────────────────────────────────────


class ScheduleValidationError(ValueError):
    """스케줄 입력값이 올바르지 않을 때 발생. UI 에서 flash 로 노출."""


# ──────────────────────────────────────────────────────────────
# 반환 타입
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScheduleSummary:
    """UI 렌더 및 라우트 응답에서 스케줄 1건을 표현하는 DTO.

    APScheduler Job 객체를 직접 템플릿에 넘기면 민감한 함수 참조나 trigger
    내부 필드가 노출될 수 있어, 필요한 것만 추려 담는다.

    Attributes:
        job_id:          APScheduler job id (prefix 로 종류 식별 — 'cron-...' / 'interval-...').
        trigger_type:    'cron' 또는 'interval'.
        trigger_spec:    사용자에게 보여줄 사람-친화 표현
                         (cron: "0 3 * * *", interval: "매 6시간").
        active_sources:  이 스케줄이 트리거될 때 실행할 source id 목록 (빈 리스트=전체).
        enabled:         paused 가 아니어서 다음 실행이 예약되어 있는지.
        next_run_time:   다음 예정 시각 (UTC tz-aware). paused 이면 None.
    """

    job_id: str
    trigger_type: str
    trigger_spec: str
    active_sources: list[str]
    enabled: bool
    next_run_time: Optional[datetime]


# ──────────────────────────────────────────────────────────────
# 싱글턴 scheduler
# ──────────────────────────────────────────────────────────────


# 모듈 수준 싱글턴 — BackgroundScheduler 는 start/stop 이 멱등하지 않고 재사용
# 이 까다로우므로, stop 에서 None 리셋해 다음 start 에서 새 인스턴스를 만든다.
_scheduler: Optional[Any] = None
_scheduler_lock: threading.Lock = threading.Lock()


def _build_scheduler() -> Any:
    """BackgroundScheduler 를 새로 생성한다.

    SQLAlchemyJobStore 는 ``app.db.session.get_engine()`` 의 엔진을 재사용해
    같은 SQLite 파일에 scheduler_jobs 테이블을 둔다. alembic 관리 밖이며,
    APScheduler 가 최초 start 시 ``CREATE TABLE IF NOT EXISTS`` 를 실행한다.
    """
    # 의존성(apscheduler)은 import 가 늦을수록 낫다 — 호환성 문제를 런타임에만 노출.
    # 00025-2 의 'python-multipart 패턴: 선언만 추가, import 는 쓰는 순간에' 와 동일 정책.
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.background import BackgroundScheduler

    jobstore = SQLAlchemyJobStore(
        engine=get_engine(),
        tablename=SCHEDULER_JOBS_TABLENAME,
    )
    scheduler = BackgroundScheduler(
        jobstores={"default": jobstore},
        # coalesce=True + max_instances=1: 재시작 직후 밀린 실행은 한 번으로 합치고
        # 동일 잡의 중복 실행을 금지. misfire_grace_time 은 guidance 의 '재시작
        # 직후 누락 잡 폭주 방지' 요건.
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": DEFAULT_MISFIRE_GRACE_TIME_SEC,
        },
        timezone=timezone.utc,
    )
    return scheduler


def _get_or_build_scheduler() -> Any:
    """싱글턴 scheduler 를 반환한다. 없으면 생성 (start 는 하지 않음)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = _build_scheduler()
        return _scheduler


def is_scheduler_running() -> bool:
    """스케줄러가 start 된 상태인지 반환. UI 의 비활성 경로 판별용."""
    with _scheduler_lock:
        if _scheduler is None:
            return False
        return bool(getattr(_scheduler, "running", False))


def start() -> None:
    """스케줄러를 기동한다. 이미 running 이면 no-op (멱등).

    웹 startup 시점에 ``create_app`` 이 호출한다. 스케줄러가 혼자 떠서 잡을
    감시·실행하고, 실제 스크래퍼 기동은 job_runner.scheduled_scrape 를 통한다.
    """
    scheduler = _get_or_build_scheduler()
    if scheduler.running:
        return
    scheduler.start()
    logger.info(
        "APScheduler 기동: tablename={} misfire_grace_time={}s max_instances=1 coalesce=True",
        SCHEDULER_JOBS_TABLENAME, DEFAULT_MISFIRE_GRACE_TIME_SEC,
    )


def stop(*, wait: bool = False) -> None:
    """스케줄러를 정지하고 싱글턴을 None 으로 리셋한다.

    Args:
        wait: True 면 진행 중 잡이 끝날 때까지 블록. 웹 shutdown 은 즉시 종료가
              나으니 기본은 False.
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            return
        if getattr(_scheduler, "running", False):
            try:
                _scheduler.shutdown(wait=wait)
            except Exception as exc:
                logger.warning(
                    "APScheduler shutdown 중 예외(무시): {}: {}",
                    type(exc).__name__, exc,
                )
        _scheduler = None
        logger.info("APScheduler 정지")


# ──────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────


def _make_job_id(prefix: str) -> str:
    """고유 job id 를 만든다. prefix 로 스케줄 타입 구분."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _job_name_for(trigger_type: str, trigger_spec: str) -> str:
    """APScheduler Job.name 은 임의 문자열이라 역직렬화용 메타로 쓴다.

    prefix(JOB_NAME_CRON_PREFIX / JOB_NAME_INTERVAL_PREFIX) + trigger_spec 형식.
    list_schedules 에서 이 문자열을 파싱해 trigger_type/spec 를 복원한다.
    """
    if trigger_type == "cron":
        return f"{JOB_NAME_CRON_PREFIX}{trigger_spec}"
    if trigger_type == "interval":
        return f"{JOB_NAME_INTERVAL_PREFIX}{trigger_spec}"
    return trigger_spec


def _summary_from_job(job: Any) -> ScheduleSummary:
    """APScheduler Job → ScheduleSummary.

    trigger_type 은 Job.trigger 클래스 이름으로 판정하고, trigger_spec 은 Job.name
    에 저장해 둔 prefix+spec 문자열을 파싱해 복원한다. 파싱이 실패하면 trigger
    객체의 repr 을 fallback.
    """
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    if isinstance(job.trigger, CronTrigger):
        trigger_type = "cron"
        if isinstance(job.name, str) and job.name.startswith(JOB_NAME_CRON_PREFIX):
            trigger_spec = job.name[len(JOB_NAME_CRON_PREFIX):]
        else:
            # 폴백: cron field 직접 표현. fmt 는 'CronTrigger(...)' 형태.
            trigger_spec = repr(job.trigger)
    elif isinstance(job.trigger, IntervalTrigger):
        trigger_type = "interval"
        if isinstance(job.name, str) and job.name.startswith(JOB_NAME_INTERVAL_PREFIX):
            trigger_spec = job.name[len(JOB_NAME_INTERVAL_PREFIX):]
        else:
            seconds = int(job.trigger.interval.total_seconds())
            hours, remainder = divmod(seconds, 3600)
            if remainder == 0 and hours >= 1:
                trigger_spec = f"매 {hours}시간"
            else:
                trigger_spec = f"매 {seconds}초"
    else:
        trigger_type = "other"
        trigger_spec = repr(job.trigger)

    # 잡 args 는 [active_sources_list] 형태. add_*_schedule 에서 리스트 1개로 넣음.
    if job.args and isinstance(job.args[0], list):
        active_sources = list(job.args[0])
    else:
        active_sources = []

    return ScheduleSummary(
        job_id=job.id,
        trigger_type=trigger_type,
        trigger_spec=trigger_spec,
        active_sources=active_sources,
        # paused job 은 next_run_time 이 None.
        enabled=job.next_run_time is not None,
        next_run_time=job.next_run_time,
    )


def list_schedules() -> list[ScheduleSummary]:
    """등록된 모든 스케줄을 사람 친화적 형태로 반환한다.

    스케줄러가 아직 start 되지 않았으면 빈 리스트. 순서는 next_run_time 오름차순
    (가까운 예정 순). None (paused) 은 뒤로.
    """
    with _scheduler_lock:
        scheduler = _scheduler
    if scheduler is None or not getattr(scheduler, "running", False):
        return []
    jobs = scheduler.get_jobs()
    summaries = [_summary_from_job(j) for j in jobs]
    summaries.sort(
        key=lambda s: (s.next_run_time is None, s.next_run_time or datetime.max.replace(tzinfo=timezone.utc))
    )
    return summaries


def _require_running_scheduler() -> Any:
    """CRUD 호출 시점에 scheduler 가 start 되어 있어야 함을 보장한다."""
    scheduler = _get_or_build_scheduler()
    if not scheduler.running:
        raise ScheduleValidationError(
            "스케줄러가 아직 기동되지 않았습니다. 웹 서버를 먼저 기동하세요."
        )
    return scheduler


def add_cron_schedule(
    *,
    cron_expression: str,
    active_sources: list[str],
    enabled: bool = True,
) -> ScheduleSummary:
    """cron 표현식 기반 스케줄을 등록한다.

    Args:
        cron_expression: 5-필드 cron (분 시 일 월 요일). 예) ``0 3 * * *``.
                         CronTrigger.from_crontab 로 파싱.
        active_sources:  실행할 source id 목록. 빈 리스트면 전체.
        enabled:         False 로 주면 생성 직후 pause 한다.

    Returns:
        등록된 스케줄의 ScheduleSummary.

    Raises:
        ScheduleValidationError: cron 표현식이 잘못됐거나 빈 문자열.
    """
    cron_expression = (cron_expression or "").strip()
    if not cron_expression:
        raise ScheduleValidationError("cron 표현식이 비어 있습니다.")

    from apscheduler.triggers.cron import CronTrigger

    try:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=timezone.utc)
    except Exception as exc:
        # from_crontab 은 잘못된 표현식에 대해 ValueError 또는 다른 예외를 던진다.
        raise ScheduleValidationError(
            f"cron 표현식 파싱 실패: {exc}"
        ) from exc

    scheduler = _require_running_scheduler()
    job_id = _make_job_id(JOB_ID_PREFIX_CRON)
    scheduler.add_job(
        scheduled_scrape,
        trigger=trigger,
        args=[list(active_sources)],
        id=job_id,
        name=_job_name_for("cron", cron_expression),
        replace_existing=False,
    )
    if not enabled:
        scheduler.pause_job(job_id)

    updated = scheduler.get_job(job_id)
    logger.info(
        "cron 스케줄 등록: job_id={} expr={!r} active_sources={} enabled={}",
        job_id, cron_expression, list(active_sources), enabled,
    )
    return _summary_from_job(updated)


def add_interval_schedule(
    *,
    hours: int,
    active_sources: list[str],
    enabled: bool = True,
) -> ScheduleSummary:
    """'매 N시간' 간단 모드 스케줄을 등록한다.

    Args:
        hours: 양의 정수. 1 이상 :data:`MAX_INTERVAL_HOURS` 이하.
        active_sources: 실행할 source id 목록. 빈 리스트면 전체.
        enabled: False 로 주면 생성 직후 pause 한다.

    Returns:
        등록된 스케줄의 ScheduleSummary.

    Raises:
        ScheduleValidationError: hours 범위 위반.
    """
    if not isinstance(hours, int) or hours <= 0:
        raise ScheduleValidationError(
            f"hours 는 양의 정수여야 합니다 (입력: {hours!r})."
        )
    if hours > MAX_INTERVAL_HOURS:
        raise ScheduleValidationError(
            f"간단 모드 interval 은 최대 {MAX_INTERVAL_HOURS}시간까지입니다. "
            f"그 이상은 cron 표현식을 사용하세요 (입력: {hours}시간)."
        )

    from apscheduler.triggers.interval import IntervalTrigger

    trigger = IntervalTrigger(hours=hours, timezone=timezone.utc)
    scheduler = _require_running_scheduler()
    job_id = _make_job_id(JOB_ID_PREFIX_INTERVAL)
    spec = f"매 {hours}시간"
    scheduler.add_job(
        scheduled_scrape,
        trigger=trigger,
        args=[list(active_sources)],
        id=job_id,
        name=_job_name_for("interval", spec),
        replace_existing=False,
    )
    if not enabled:
        scheduler.pause_job(job_id)

    updated = scheduler.get_job(job_id)
    logger.info(
        "interval 스케줄 등록: job_id={} hours={} active_sources={} enabled={}",
        job_id, hours, list(active_sources), enabled,
    )
    return _summary_from_job(updated)


def toggle_schedule(job_id: str, *, enabled: bool) -> ScheduleSummary:
    """스케줄을 pause / resume 토글한다.

    pause 한 잡은 next_run_time 이 None 이 되어 실행되지 않지만 jobstore 에는
    남아 있다. resume 시 trigger 가 다시 계산된 next_run_time 을 세팅한다.

    Raises:
        ScheduleValidationError: job_id 에 해당하는 스케줄이 없음.
    """
    scheduler = _require_running_scheduler()
    job = scheduler.get_job(job_id)
    if job is None:
        raise ScheduleValidationError(f"스케줄 id={job_id!r} 를 찾을 수 없습니다.")

    if enabled:
        scheduler.resume_job(job_id)
    else:
        scheduler.pause_job(job_id)

    updated = scheduler.get_job(job_id)
    logger.info(
        "스케줄 토글: job_id={} enabled={} next_run_time={}",
        job_id, enabled, updated.next_run_time,
    )
    return _summary_from_job(updated)


def delete_schedule(job_id: str) -> None:
    """스케줄을 영구 삭제한다 (jobstore 에서 제거).

    Raises:
        ScheduleValidationError: job_id 에 해당하는 스케줄이 없음.
    """
    scheduler = _require_running_scheduler()
    job = scheduler.get_job(job_id)
    if job is None:
        raise ScheduleValidationError(f"스케줄 id={job_id!r} 를 찾을 수 없습니다.")
    scheduler.remove_job(job_id)
    logger.info("스케줄 삭제: job_id={}", job_id)


__all__ = [
    "ScheduleSummary",
    "ScheduleValidationError",
    "add_cron_schedule",
    "add_interval_schedule",
    "delete_schedule",
    "is_scheduler_running",
    "list_schedules",
    "start",
    "stop",
    "toggle_schedule",
]
