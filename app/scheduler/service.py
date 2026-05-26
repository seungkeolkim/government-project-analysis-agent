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

import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from app.db.session import get_engine
from app.scheduler.constants import (
    DEFAULT_MISFIRE_GRACE_TIME_SEC,
    JOB_ID_BACKUP,
    JOB_ID_DAILY_REPORT,
    JOB_ID_PREFIX_CRON,
    JOB_ID_PREFIX_INTERVAL,
    JOB_NAME_BACKUP_PREFIX,
    JOB_NAME_CRON_PREFIX,
    JOB_NAME_DAILY_REPORT_PREFIX,
    JOB_NAME_INTERVAL_PREFIX,
    MAX_INTERVAL_HOURS,
    SCHEDULER_JOBS_TABLENAME,
)
from app.scheduler.job_runner import (
    gc_orphan_attachments_job,
    scheduled_backup_job,
    scheduled_daily_report_job,
    scheduled_scrape,
)
# task 00040-4 — APScheduler 의 글로벌/트리거 timezone 을 Asia/Seoul 로 통일.
# ``app.timezone`` 이 KST 의 단일 진실 소스이므로 직접 ``ZoneInfo("Asia/Seoul")``
# 을 만들지 않고 그곳의 상수를 가져온다.
from app.timezone import KST

# task 00131 — cron 중복 실행 버그 수정. 컨테이너당 스케줄러 job 을 실행하는
# 프로세스를 1개로 강제하는 flock 기반 단일 인스턴스 가드.
from app.scheduler.single_instance import (
    release_single_instance_lock,
    try_acquire_single_instance_lock,
)

# task 00147 — 표준 crontab 요일 표기를 APScheduler 요일 규약으로 보정하는 헬퍼.
# CronTrigger.from_crontab 은 요일 숫자를 변환 없이 넘겨 '1-5'(월~금) 가 화~토로
# 어긋나므로, 모든 cron trigger 생성은 이 헬퍼를 거치도록 통일한다.
from app.scheduler.cron import build_cron_trigger

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
        # task 00040-4 — 사용자 원문 결정 'APScheduler cron: Asia/Seoul.
        # cron 30 9 * * * = KST 09:30'. 호스트 컨테이너의 TZ env 와 무관하게
        # 명시적 KST 로 평가되도록 ``app.timezone.KST`` 를 그대로 넣는다.
        timezone=KST,
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


def _recompute_all_jobs_next_run_time(scheduler: Any) -> int:
    """jobstore 의 모든 잡에 대해 cron/interval trigger 기준으로 ``next_run_time``
    을 현재 시각 기준으로 강제 재계산한다 (task 00149-1).

    배경 (사용자 원문 task 00149):
        APScheduler 의 ``SQLAlchemyJobStore`` 는 ``next_run_time`` 을 **저장된
        절대 epoch float** 으로 들고 있어, 시스템이 꺼져 있던 동안 그 시각이
        지나가면 다음 회차로 자동 advance 되지 않는다. 다음 worker 가 떠도 그
        stale 시각이 그대로 남고, 단일 인스턴스 lock 점유·새 worker reload·일시
        적 ASGI 종료 등 어떤 이유로든 그 순간 발화가 누락되면 **영구적으로**
        잡이 멈춘다. 사용자 원문이 진단한 daily-report 09:30 누락 사고가 이
        패턴이다 — ``select * from scheduler_jobs`` 의 daily-report row 가
        09:50 시점에도 09:30 epoch 그대로 남아 있었다.

        근본 수정 방향: ``cron 표현식을 source of truth`` 로 보고, 스케줄러가
        살아나는 모든 순간(=startup)에 **모든 잡의 next_run_time 을 무조건
        재계산** 한다. 그러면 stale 절대 시각이 발화되지 못한 채 남아 있을
        구조적 여지 자체가 없어진다.

    재계산 정책 (timezone 동일/상이 모두 한 흐름으로 통합):
        - cron 잡: ``Job.name`` 에 ``JOB_NAME_*_PREFIX`` + cron 표현식이 저장돼
          있다 (``_job_name_for``). 그 표현식을 ``build_cron_trigger`` 로 다시
          파싱해 **KST 기반 새 trigger** 를 만들어 ``reschedule_job`` 에 넘긴다.
          이렇게 하면 legacy(UTC) timezone 으로 직렬화된 잡도 함께 KST 로 정정
          되고(=00040-4 의 tz 재해석 책임을 본 함수가 흡수), 동시에
          ``reschedule_job`` 의 ``trigger.get_next_fire_time(None, now)`` 가
          호출돼 next_run_time 이 \"now 이후 첫 fire-time\" 으로 자동 advance
          된다 — 지나간 절대 시각이 자동 보정된다.
        - interval 잡: ``trigger.interval`` (timedelta) 에서 시간을 복원하고
          ``IntervalTrigger(..., timezone=KST)`` 로 재생성해 같은 패턴으로
          reschedule_job 한다.
        - Job.name 에 spec 이 없거나 파싱이 실패한 잡, cron/interval 외 trigger
          는 건너뛰고 경고 로그만 남긴다 — 운영자가 수동 재등록해야 함을 알린다.

    ``modify_job(trigger=...)`` 가 아니라 ``reschedule_job(trigger=...)`` 를 쓰는
    이유: 전자는 trigger 객체만 교체하고 ``next_run_time`` 은 옛 값(=stale 가능)
    을 유지한다. 후자는 새 trigger 의 ``get_next_fire_time`` 으로 now 이후 첫
    fire-time 을 다시 계산하므로 \"지나간 시각 자동 보정 + cron 기반 재계산\" 의
    의도와 정확히 일치한다 (APScheduler 3.x ``BaseScheduler.reschedule_job``
    구현 참조).

    Args:
        scheduler: 이미 ``start()`` 된 BackgroundScheduler. ``reschedule_job``
            호출이 가능해야 한다.

    Returns:
        실제로 재계산되어 ``next_run_time`` 이 갱신된 잡의 개수. 등록 잡이 0건
        이거나 모두 reschedule 실패면 0.
    """
    # apscheduler 모듈은 lazy import (호환성 문제를 런타임에만 노출).
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    recomputed_count = 0
    for job in scheduler.get_jobs():
        old_trigger = job.trigger
        old_timezone = getattr(old_trigger, "timezone", None)
        old_next_run_time = job.next_run_time

        if isinstance(old_trigger, CronTrigger):
            # Job.name 의 prefix 를 벗겨 원본 cron 표현식 복원.
            # scrape 잡: "cron:0 3 * * *", backup 잡: "backup-cron:0 3 * * *",
            # daily report 잡: "daily-report-cron:0 9 * * 1-5",
            # GC 잡: "gc-orphan-cron:0 4 * * *"
            if isinstance(job.name, str) and job.name.startswith(JOB_NAME_CRON_PREFIX):
                cron_expression = job.name[len(JOB_NAME_CRON_PREFIX):]
            elif isinstance(job.name, str) and job.name.startswith(JOB_NAME_BACKUP_PREFIX):
                # task 00094-2 — 백업 잡 prefix 인식
                cron_expression = job.name[len(JOB_NAME_BACKUP_PREFIX):]
            elif isinstance(job.name, str) and job.name.startswith(JOB_NAME_DAILY_REPORT_PREFIX):
                # task 00125-7 — daily report 잡 prefix 인식
                cron_expression = job.name[len(JOB_NAME_DAILY_REPORT_PREFIX):]
            elif isinstance(job.name, str) and job.name.startswith(JOB_NAME_GC_ORPHAN_PREFIX):
                # task 00041-5 — GC 고아 첨부 잡 prefix 인식
                cron_expression = job.name[len(JOB_NAME_GC_ORPHAN_PREFIX):]
            else:
                logger.warning(
                    "next_run_time 재계산 스킵 — cron job.name 파싱 실패: job_id={} name={!r}",
                    job.id, job.name,
                )
                continue
            try:
                # task 00147 — 요일 규약 보정 헬퍼를 거쳐 재해석한다. startup 시
                # jobstore 에 잘못된 요일로 저장돼 있던 기존 잡들도 이 경로를
                # 타면서 다음 기동 때 올바른 요일로 자동 reschedule 된다 —
                # 별도 마이그레이션 스크립트가 필요 없다.
                new_trigger = build_cron_trigger(cron_expression, timezone=KST)
            except Exception as exc:
                logger.warning(
                    "next_run_time 재계산 스킵 — cron 표현식 재파싱 실패: "
                    "job_id={} expr={!r} err={}",
                    job.id, cron_expression, exc,
                )
                continue
        elif isinstance(old_trigger, IntervalTrigger):
            interval = old_trigger.interval
            total_seconds = int(interval.total_seconds())
            # 시간 단위로 떨어지면 hours=, 아니면 seconds= 로 저장한다.
            if total_seconds > 0 and total_seconds % 3600 == 0:
                new_trigger = IntervalTrigger(
                    hours=total_seconds // 3600, timezone=KST
                )
            else:
                new_trigger = IntervalTrigger(seconds=total_seconds, timezone=KST)
        else:
            logger.warning(
                "next_run_time 재계산 스킵 — 지원하지 않는 trigger 종류: "
                "job_id={} trigger={!r}",
                job.id, type(old_trigger).__name__,
            )
            continue

        try:
            # reschedule_job 은 trigger 교체 + next_run_time 재계산을 함께 수행.
            # modify_job 은 trigger 만 갈고 next_run_time 은 옛 값 유지라 부적합.
            # 이 호출이 본 함수의 핵심 — \"stale 절대 시각 → now 이후 첫
            # fire-time\" 자동 보정이 trigger.get_next_fire_time(None, now) 으로
            # 일어난다.
            scheduler.reschedule_job(job.id, trigger=new_trigger)
        except Exception as exc:
            logger.warning(
                "next_run_time 재계산 실패(reschedule_job 예외): job_id={} err={}",
                job.id, exc,
            )
            continue

        recomputed_count += 1
        # 재계산된 next_run_time 을 한 줄에 함께 노출해 운영자가 의도된 KST
        # 시각인지, 그리고 \"stale 시각 → advance\" 가 실제로 일어났는지를
        # docker logs 만으로 즉시 진단할 수 있게 한다. acceptance criteria 의
        # \"docker logs 에서 'APScheduler next_run_time 재계산 완료' 로그가 모든
        # 잡에 대해 1회씩 찍힌다\" 요건을 충족.
        updated = scheduler.get_job(job.id)
        logger.info(
            "APScheduler next_run_time 재계산 완료: job_id={} old_tz={} new_tz=KST "
            "old_next_run_time={} new_next_run_time={}",
            job.id,
            old_timezone,
            old_next_run_time,
            getattr(updated, "next_run_time", None),
        )

    return recomputed_count


def start() -> None:
    """스케줄러를 기동한다. 이미 running 이면 no-op (멱등).

    웹 startup 시점에 ``create_app`` 이 호출한다. 스케줄러가 혼자 떠서 잡을
    감시·실행하고, 실제 스크래퍼 기동은 job_runner.scheduled_scrape 를 통한다.

    task 00149-1 — start 직후 jobstore 의 모든 잡에 대해 cron/interval 표현식
    기준으로 ``next_run_time`` 을 강제 재계산한다
    (``_recompute_all_jobs_next_run_time`` 참조). 이 호출이 \"지나간 절대 시각으로
    저장된 next_run_time 이 영구 누락을 일으키는\" 구조적 결함의 근본 차단점이다.
    timezone 동일 여부와 무관하게 모든 잡을 ``reschedule_job(trigger=...)`` 으로
    재계산하므로, task 00040-4 의 \"UTC 로 직렬화된 legacy 잡 KST 재해석\" 책임도
    본 호출이 흡수한다. 등록 잡이 0건이면 no-op.

    task 00131 — cron 중복 실행 버그 수정. 스케줄러를 실제로 띄우기 전에
    프로세스 수준 단일 인스턴스 flock 을 시도한다. lock 획득에 실패하면(=다른
    프로세스가 이미 스케줄러를 실행 중) 이 프로세스에서는 스케줄러를 띄우지
    않고 조기 반환한다. uvicorn ``--reload`` 환경에서 worker 프로세스가 교체될
    때 이전 worker 의 ``BackgroundScheduler`` 가 깨끗이 정리되지 못하고 살아
    남으면, 같은 ``scheduler_jobs`` 테이블을 보는 스케줄러가 여러 개가 되어
    동일 job 이 trigger 시각마다 중복 실행된다. flock 으로 '컨테이너당 job 을
    실행하는 스케줄러는 1개' 불변식을 코드로 강제한다 (자세한 배경은
    ``app.scheduler.single_instance`` 모듈 docstring 참조).
    """
    scheduler = _get_or_build_scheduler()
    if scheduler.running:
        return

    # task 00131 — 단일 인스턴스 lock 획득에 실패하면 스케줄러를 띄우지 않는다.
    # lock 을 쥔 다른 프로세스가 이미 job 을 실행하므로, 이 프로세스까지 띄우면
    # 동일 job 이 중복 실행된다.
    if not try_acquire_single_instance_lock():
        # pid 를 남겨 운영자가 single_instance 의 'lock 획득/실패' 로그와
        # 짝지어 어느 프로세스가 lock 을 쥐었는지 docker logs 만으로 추적할 수
        # 있게 한다. uvicorn --reload 환경에서 이 로그가 worker pid 로 찍히면
        # 비정상(=task 00133 회귀) 이다 — startup 훅으로 옮긴 뒤에는 worker 가
        # 항상 lock 을 쥐어야 한다.
        logger.warning(
            "APScheduler 기동 생략 — 단일 인스턴스 lock 을 다른 프로세스가 "
            "점유 중이다. 이 프로세스에서는 스케줄 job 이 실행되지 않는다 "
            "(cron 중복 실행 방지, pid={}).",
            os.getpid(),
        )
        return

    scheduler.start()
    logger.info(
        "APScheduler 기동: pid={} tablename={} misfire_grace_time={}s max_instances=1 coalesce=True timezone=KST",
        os.getpid(), SCHEDULER_JOBS_TABLENAME, DEFAULT_MISFIRE_GRACE_TIME_SEC,
    )

    # task 00149-1 — jobstore 의 모든 잡의 next_run_time 을 cron/interval 표현식
    # 기준으로 현재 시각 이후 첫 fire-time 으로 강제 재계산. \"지나간 절대 시각\"
    # 으로 인한 영구 누락 차단의 핵심 호출.
    try:
        recomputed = _recompute_all_jobs_next_run_time(scheduler)
    except Exception as exc:
        # 재계산 실패가 스케줄러 기동 자체를 망가뜨리지 않도록 방어. 운영자는
        # admin/schedule 탭의 next_run_time 을 보고 수동 재등록할 수 있다.
        logger.exception(
            "APScheduler next_run_time 재계산 실패(스킵): {}: {}",
            type(exc).__name__, exc,
        )
        recomputed = 0
    if recomputed:
        logger.info(
            "APScheduler next_run_time 재계산: {}건의 잡을 현재 시각 기준으로 갱신",
            recomputed,
        )


def stop(*, wait: bool = False) -> None:
    """스케줄러를 정지하고 싱글턴을 None 으로 리셋한다.

    task 00131 — 스케줄러 정지 후 단일 인스턴스 flock 도 함께 해제한다. 이렇게
    해야 다음 worker(재기동/``--reload``) 가 lock 을 승계해 스케줄러를 정상
    기동할 수 있다. lock 을 쥔 적이 없는 프로세스(=lock 획득 실패로 스케줄러를
    띄우지 않은 프로세스)에서 호출돼도 release 는 안전한 no-op 이다. shutdown
    을 먼저 끝낸 뒤 lock 을 풀어, 다른 프로세스가 정지 도중에 lock 을 가로채
    스케줄러가 잠시 겹치는 일이 없도록 순서를 고정한다.

    Args:
        wait: True 면 진행 중 잡이 끝날 때까지 블록. 웹 shutdown 은 즉시 종료가
              나으니 기본은 False.
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
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

    # task 00131 — 스케줄러 인스턴스 유무와 무관하게 단일 인스턴스 lock 을
    # 해제한다. _scheduler_lock 밖에서 호출하지만 release 는 자체 lock
    # (_lock_state_guard) 으로 보호되며 두 lock 이 서로 중첩되지 않아 데드락이
    # 없다.
    release_single_instance_lock()


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
        elif isinstance(job.name, str) and job.name.startswith(JOB_NAME_BACKUP_PREFIX):
            # task 00094-2 — 백업 잡 prefix 인식
            trigger_spec = job.name[len(JOB_NAME_BACKUP_PREFIX):]
        elif isinstance(job.name, str) and job.name.startswith(JOB_NAME_DAILY_REPORT_PREFIX):
            # task 00125-7 — daily report 잡 prefix 인식
            trigger_spec = job.name[len(JOB_NAME_DAILY_REPORT_PREFIX):]
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


def list_general_schedules() -> list[ScheduleSummary]:
    """백업 잡과 daily report 잡을 제외한 일반 스케줄 목록을 반환한다.

    [스케줄] 탭은 일반 수집 스케줄만 표시하고, 백업 잡은 [시스템 백업] 탭에서
    별도 노출하며(task 00096) daily report 잡은 [메일 발송 설정] 의 「Daily
    Report」 카드에서 별도 노출한다(task 00125 / Phase A-3). 고정 ID 기반
    필터링이며 trigger_spec 매칭은 false-match 위험이 있으므로 사용하지 않는다.
    """
    excluded_job_ids = {JOB_ID_BACKUP, JOB_ID_DAILY_REPORT}
    return [s for s in list_schedules() if s.job_id not in excluded_job_ids]


def get_backup_schedule_summary() -> "ScheduleSummary | None":
    """APScheduler 에 등록된 백업 잡의 ScheduleSummary 를 반환한다.

    백업 잡이 존재하지 않거나 스케줄러가 미기동 상태면 None.
    """
    for s in list_schedules():
        if s.job_id == JOB_ID_BACKUP:
            return s
    return None


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

    try:
        # task 00040-4 — cron 표현식은 KST 기준으로 파싱한다. APScheduler 는
        # 새 잡의 다음 실행 시각을 trigger.timezone 과 scheduler.timezone 양쪽을
        # 종합해 계산하므로 trigger 자체에 KST 를 명시해 둔다.
        # task 00147 — build_cron_trigger 가 요일 필드를 APScheduler 규약으로
        # 보정한 뒤 CronTrigger 를 만든다.
        trigger = build_cron_trigger(cron_expression, timezone=KST)
    except Exception as exc:
        # build_cron_trigger 는 잘못된 표현식에 대해 ValueError 등을 그대로 던진다.
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

    # task 00040-4 — interval trigger 도 시작 기준 시각을 KST 로 정렬한다.
    trigger = IntervalTrigger(hours=hours, timezone=KST)
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


# ──────────────────────────────────────────────────────────────
# Phase 5a (task 00041-5) — 고아 첨부 파일 GC 의 일 1회 자동 실행
# ──────────────────────────────────────────────────────────────
#
# 운영자는 본 함수를 한 번 호출해 cron 잡을 등록하고, 등록 이후로는 jobstore
# 가 재기동에도 잡을 자동 복원한다. UI 노출은 5b 의 범위로 본 task 에서는
# 다루지 않는다 — 운영자는 Python REPL / 일회성 스크립트로 등록한다.
#
# 등록 예 (컨테이너 안에서):
#   python -c \"from app.scheduler.service import start, add_gc_orphan_cron_schedule; \\
#               start(); add_gc_orphan_cron_schedule(cron_expression='0 4 * * *')\"


# 기본 cron — 설계 §11.4 의 권장값 (KST 04:00).
GC_ORPHAN_DEFAULT_CRON: str = "0 4 * * *"

# job_id prefix — cron / interval 과 별개로 GC 잡임을 식별하기 위함.
JOB_ID_PREFIX_GC_ORPHAN: str = "gc-orphan"

# job.name prefix — list_schedules 가 trigger_spec 을 복원하는 패턴.
JOB_NAME_GC_ORPHAN_PREFIX: str = "gc-orphan-cron:"


def add_gc_orphan_cron_schedule(
    *,
    cron_expression: str = GC_ORPHAN_DEFAULT_CRON,
    enabled: bool = True,
) -> ScheduleSummary:
    """고아 첨부 파일 GC 잡을 cron 표현식으로 등록한다 (task 00041-5).

    수집(scheduled_scrape) 잡과 같은 BackgroundScheduler / SQLAlchemyJobStore 를
    공유하지만 함수 자체는 ``app.scheduler.job_runner.gc_orphan_attachments_job``
    이라 jobstore 에 별도로 pickle 된다. ``ScrapeRun`` 가드는 잡 함수 안에서
    ``run_gc`` 가 자체 처리하므로 본 등록 함수는 단순히 trigger 설정만 한다.

    Args:
        cron_expression: 5-필드 cron 표현식 (분 시 일 월 요일).
                          기본값 ``0 4 * * *`` (KST 04:00 — 설계 §11.4 권장).
                          타임존은 ``app.timezone.KST`` 로 고정 — Phase 4 컨벤션.
        enabled:         False 면 등록 직후 paused 상태로 둔다.

    Returns:
        등록된 잡의 ``ScheduleSummary``.

    Raises:
        ScheduleValidationError: cron 표현식이 잘못됐거나 빈 문자열.
    """
    cron_expression = (cron_expression or "").strip()
    if not cron_expression:
        raise ScheduleValidationError("GC cron 표현식이 비어 있습니다.")

    try:
        # task 00040-4 의 KST 컨벤션 그대로 — trigger 자체에 timezone 명시.
        # task 00147 — 요일 규약 보정 헬퍼를 거쳐 trigger 를 만든다.
        trigger = build_cron_trigger(cron_expression, timezone=KST)
    except Exception as exc:
        raise ScheduleValidationError(
            f"GC cron 표현식 파싱 실패: {exc}"
        ) from exc

    scheduler = _require_running_scheduler()
    job_id = _make_job_id(JOB_ID_PREFIX_GC_ORPHAN)
    scheduler.add_job(
        gc_orphan_attachments_job,
        trigger=trigger,
        # GC 잡은 인자 없이 호출 — pickle 안정성 + jobstore 단순성.
        args=[],
        id=job_id,
        name=f"{JOB_NAME_GC_ORPHAN_PREFIX}{cron_expression}",
        replace_existing=False,
    )
    if not enabled:
        scheduler.pause_job(job_id)

    updated = scheduler.get_job(job_id)
    logger.info(
        "GC 고아 파일 cron 스케줄 등록: job_id={} expr={!r} enabled={} next_run_time={}",
        job_id,
        cron_expression,
        enabled,
        getattr(updated, "next_run_time", None),
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


# ──────────────────────────────────────────────────────────────
# task 00094-2 — 백업 전용 스케줄 CRUD
# ──────────────────────────────────────────────────────────────


def register_backup_cron_schedule(*, cron_expression: str) -> ScheduleSummary:
    """백업 cron 잡을 등록하거나 기존 잡의 trigger 를 갱신한다.

    ``JOB_ID_BACKUP`` 으로 고정 ID 를 사용해 백업 잡은 **항상 1건**만 존재한다.
    잡이 이미 있으면 ``reschedule_job`` 으로 trigger 만 교체한다 (새 row 가 아님).
    잡이 없으면 새로 ``add_job`` 한다.

    Args:
        cron_expression: 5-필드 cron (분 시 일 월 요일). 예) ``0 3 * * *``.
                         Asia/Seoul 기준으로 파싱된다.

    Returns:
        등록/갱신된 잡의 ScheduleSummary.

    Raises:
        ScheduleValidationError: cron 표현식이 잘못됐거나 빈 문자열.
    """
    cron_expression = (cron_expression or "").strip()
    if not cron_expression:
        raise ScheduleValidationError("백업 cron 표현식이 비어 있습니다.")

    try:
        # task 00147 — 요일 규약 보정 헬퍼를 거쳐 trigger 를 만든다.
        trigger = build_cron_trigger(cron_expression, timezone=KST)
    except Exception as exc:
        raise ScheduleValidationError(
            f"백업 cron 표현식 파싱 실패: {exc}"
        ) from exc

    scheduler = _require_running_scheduler()
    existing = scheduler.get_job(JOB_ID_BACKUP)

    if existing is not None:
        # trigger 갱신 — next_run_time 도 함께 재계산
        scheduler.reschedule_job(JOB_ID_BACKUP, trigger=trigger)
        # name 도 새 cron 표현식으로 갱신 (_summary_from_job 이 name 으로 spec 을 복원함)
        scheduler.modify_job(JOB_ID_BACKUP, name=f"{JOB_NAME_BACKUP_PREFIX}{cron_expression}")
        updated = scheduler.get_job(JOB_ID_BACKUP)
        logger.info(
            "백업 cron 갱신: job_id={} expr={!r} next_run_time={}",
            JOB_ID_BACKUP, cron_expression, getattr(updated, "next_run_time", None),
        )
    else:
        scheduler.add_job(
            scheduled_backup_job,
            trigger=trigger,
            args=[],
            id=JOB_ID_BACKUP,
            name=f"{JOB_NAME_BACKUP_PREFIX}{cron_expression}",
            replace_existing=True,
        )
        updated = scheduler.get_job(JOB_ID_BACKUP)
        logger.info(
            "백업 cron 등록: job_id={} expr={!r} next_run_time={}",
            JOB_ID_BACKUP, cron_expression, getattr(updated, "next_run_time", None),
        )

    return _summary_from_job(updated)


def remove_backup_cron_schedule() -> None:
    """백업 cron 잡을 제거한다. 잡이 없어도 에러 없이 no-op 으로 처리한다."""
    scheduler = _require_running_scheduler()
    existing = scheduler.get_job(JOB_ID_BACKUP)
    if existing is None:
        logger.debug("백업 cron 제거 요청: 잡이 이미 없음 (no-op)")
        return
    scheduler.remove_job(JOB_ID_BACKUP)
    logger.info("백업 cron 제거: job_id={}", JOB_ID_BACKUP)


def ensure_backup_cron_registered() -> None:
    """startup 시 백업 cron 잡이 없으면 등록한다.

    APScheduler 가 ``SQLAlchemyJobStore`` 에서 자동 복원한 경우(재기동): no-op.
    첫 실행이거나 잡이 없는 경우: ``SystemSetting`` 의 cron 설정(없으면 기본값)으로
    등록한다. 기본값은 ``app.backup.constants.DEFAULT_BACKUP_CRON`` (KST 03:00 매일).

    ``create_app()`` 에서 ``start_scheduler()`` 직후 호출해야 한다.
    스케줄러가 아직 running 이 아닌 경우 조용히 no-op 처리한다.
    """
    with _scheduler_lock:
        scheduler = _scheduler
    if scheduler is None or not getattr(scheduler, "running", False):
        logger.debug("ensure_backup_cron_registered: 스케줄러 미기동 — 스킵")
        return

    existing = scheduler.get_job(JOB_ID_BACKUP)
    if existing is not None:
        # jobstore 에서 자동 복원된 경우 — 건드리지 않는다
        logger.debug(
            "백업 cron 잡 이미 존재 (jobstore 자동 복원): next_run_time={}",
            getattr(existing, "next_run_time", None),
        )
        return

    # 첫 실행이거나 잡이 없는 경우 — DB 설정 또는 기본값으로 등록
    from app.backup.constants import DEFAULT_BACKUP_CRON, SETTING_KEY_BACKUP_CRON
    from app.backup.service import get_setting
    from app.db.session import session_scope

    with session_scope() as session:
        cron_expression = get_setting(session, SETTING_KEY_BACKUP_CRON) or DEFAULT_BACKUP_CRON

    try:
        register_backup_cron_schedule(cron_expression=cron_expression)
        logger.info(
            "백업 cron 잡 startup 자동 등록: expr={!r}", cron_expression
        )
    except ScheduleValidationError as exc:
        # 저장된 cron 이 잘못된 경우 — 운영자가 admin 페이지에서 수정해야 함
        logger.warning("백업 cron startup 자동 등록 실패 (admin 페이지에서 수동 등록 필요): {}", exc)


# ──────────────────────────────────────────────────────────────
# task 00125-7 (Phase A-3) — Daily Report 전용 스케줄 CRUD
# ──────────────────────────────────────────────────────────────
#
# 운영 모델은 백업 잡(``register_backup_cron_schedule``) 과 동일하다 — 잡은 항상
# **1건 고정 ID** (``JOB_ID_DAILY_REPORT='daily-report'``) 로만 존재하고,
# SystemSetting (``email.daily_report.enabled`` / ``cron_expression``) 가 사용자
# 토글 source of truth 다. 따라서 본 모듈에서 노출하는 함수는 4종:
#
#     register_daily_report_cron_schedule(cron_expression, *, enabled)
#         → enabled=False / cron 빈 값 → 잡 제거 + None 반환
#         → 잡 없음 → add_job
#         → 잡 있음 → reschedule_job + modify_job(name)
#     remove_daily_report_cron_schedule()  — 잡 없어도 no-op
#     get_daily_report_schedule_summary() → ScheduleSummary | None
#     ensure_daily_report_cron_registered() — startup 자동 복원 (백업과 동일 라인)


def register_daily_report_cron_schedule(
    cron_expression: str | None = None,
    *,
    enabled: bool | None = None,
) -> Optional[ScheduleSummary]:
    """Daily report cron 잡을 등록·갱신·제거한다 (단일 진입점).

    ``JOB_ID_DAILY_REPORT`` 로 고정 ID 를 사용해 daily report 잡은 **항상 1건**만
    존재한다. 동작 매트릭스 (디자인 노트 §8 — 4 케이스 검증):

    | 입력                                       | 동작                                              |
    |--------------------------------------------|---------------------------------------------------|
    | ``enabled=False`` 또는 cron 빈 값          | 기존 잡이 있으면 ``remove_job`` + None 반환       |
    | 잡 없음 + enabled=True + cron 정상         | ``add_job``                                       |
    | 잡 있음 + enabled=True + cron 정상         | ``reschedule_job`` + ``modify_job(name=...)``     |

    Args:
        cron_expression: 5-필드 cron (분 시 일 월 요일). 예) ``"0 9 * * 1-5"``.
            ``Asia/Seoul`` 기준으로 파싱된다. ``None`` 이면 SystemSetting
            ``email.daily_report.cron_expression`` 에서 직접 로드한다.
            빈 문자열로 평가되면 비활성화 분기로 처리한다.
        enabled: ``True`` 면 등록·갱신, ``False`` 면 제거. ``None`` 이면
            SystemSetting ``email.daily_report.enabled`` 에서 직접 로드한다.

    Returns:
        등록·갱신된 잡의 ``ScheduleSummary``. 제거되었거나 등록되지 않으면 None.

    Raises:
        ScheduleValidationError: cron 표현식이 잘못된 경우 (enabled=True 일 때만).
    """
    # ── 1. None 입력은 SystemSetting 에서 로드 ──────────────────────────
    # 디자인 노트 §8 가 명시: \"인자 None 이면 SystemSetting 에서 직접 로드\".
    # startup 자동 복원(``ensure_daily_report_cron_registered``) 이 본 함수에
    # 인자 없이 의존하므로 본 로직이 중복되지 않도록 한 곳에서만 처리한다.
    if cron_expression is None or enabled is None:
        # lazy import — 순환 import 방지 (app.email.constants → app.scheduler 의존
        # 가능성 차단). scheduled_daily_report_job 안의 lazy import 패턴과 동일.
        from app.backup.service import get_setting
        from app.db.session import session_scope
        from app.email.constants import (
            DEFAULT_DAILY_REPORT_CRON,
            DEFAULT_DAILY_REPORT_ENABLED,
            SETTING_KEY_DAILY_REPORT_CRON,
            SETTING_KEY_DAILY_REPORT_ENABLED,
        )

        with session_scope() as session:
            if cron_expression is None:
                cron_expression = (
                    get_setting(session, SETTING_KEY_DAILY_REPORT_CRON)
                    or DEFAULT_DAILY_REPORT_CRON
                )
            if enabled is None:
                raw_enabled = get_setting(session, SETTING_KEY_DAILY_REPORT_ENABLED)
                # 저장 포맷 "true" / "false" (소문자) — case-insensitive 비교.
                if raw_enabled is None or raw_enabled.strip() == "":
                    enabled = DEFAULT_DAILY_REPORT_ENABLED
                else:
                    enabled = raw_enabled.strip().lower() == "true"

    cron_expression = (cron_expression or "").strip()

    # ── 2. 비활성화 분기 — 기존 잡이 있으면 제거 ────────────────────────
    if not enabled or not cron_expression:
        scheduler = _require_running_scheduler()
        existing = scheduler.get_job(JOB_ID_DAILY_REPORT)
        if existing is not None:
            scheduler.remove_job(JOB_ID_DAILY_REPORT)
            logger.info(
                "Daily report cron 제거 (비활성화): job_id={} prev_expr_name={!r}",
                JOB_ID_DAILY_REPORT,
                existing.name,
            )
        else:
            logger.debug(
                "Daily report cron 비활성화 요청: 잡이 없으므로 no-op (job_id={})",
                JOB_ID_DAILY_REPORT,
            )
        return None

    # ── 3. 활성화 분기 — cron 표현식 파싱 + add or reschedule ───────────
    try:
        # cron 표현식은 KST 기준으로 파싱한다 (task 00040-4 의 KST 컨벤션).
        # BackgroundScheduler.timezone 이 이미 KST 라 trigger 도 명시적으로 KST.
        # task 00147 — 요일 규약 보정 헬퍼를 거쳐 trigger 를 만든다.
        trigger = build_cron_trigger(cron_expression, timezone=KST)
    except Exception as exc:
        raise ScheduleValidationError(
            f"Daily report cron 표현식 파싱 실패: {exc}"
        ) from exc

    scheduler = _require_running_scheduler()
    existing = scheduler.get_job(JOB_ID_DAILY_REPORT)

    job_name = f"{JOB_NAME_DAILY_REPORT_PREFIX}{cron_expression}"
    if existing is not None:
        # trigger 갱신 — next_run_time 도 함께 재계산 (백업 잡 패턴과 동일).
        scheduler.reschedule_job(JOB_ID_DAILY_REPORT, trigger=trigger)
        # name 도 새 cron 표현식으로 갱신해 list_schedules 의 trigger_spec 복원이
        # 최신 값이 되도록 한다.
        scheduler.modify_job(JOB_ID_DAILY_REPORT, name=job_name)
        updated = scheduler.get_job(JOB_ID_DAILY_REPORT)
        logger.info(
            "Daily report cron 갱신: job_id={} expr={!r} next_run_time={}",
            JOB_ID_DAILY_REPORT,
            cron_expression,
            getattr(updated, "next_run_time", None),
        )
    else:
        scheduler.add_job(
            scheduled_daily_report_job,
            trigger=trigger,
            # pickle 안정성을 위해 args 는 빈 list. 잡 함수가 SystemSetting 에서
            # 발송 시점에 직접 설정을 읽는다 (백업 잡과 동일 정책).
            args=[],
            id=JOB_ID_DAILY_REPORT,
            name=job_name,
            replace_existing=True,
        )
        updated = scheduler.get_job(JOB_ID_DAILY_REPORT)
        logger.info(
            "Daily report cron 등록: job_id={} expr={!r} next_run_time={}",
            JOB_ID_DAILY_REPORT,
            cron_expression,
            getattr(updated, "next_run_time", None),
        )

    return _summary_from_job(updated)


def remove_daily_report_cron_schedule() -> None:
    """Daily report cron 잡을 제거한다. 잡이 없어도 에러 없이 no-op."""
    scheduler = _require_running_scheduler()
    existing = scheduler.get_job(JOB_ID_DAILY_REPORT)
    if existing is None:
        logger.debug(
            "Daily report cron 제거 요청: 잡이 이미 없음 (no-op, job_id={})",
            JOB_ID_DAILY_REPORT,
        )
        return
    scheduler.remove_job(JOB_ID_DAILY_REPORT)
    logger.info("Daily report cron 제거: job_id={}", JOB_ID_DAILY_REPORT)


def get_daily_report_schedule_summary() -> Optional[ScheduleSummary]:
    """APScheduler 에 등록된 daily report 잡의 ``ScheduleSummary`` 를 반환한다.

    daily report 잡이 존재하지 않거나 스케줄러가 미기동 상태면 None. Admin API
    의 ``GET /api/admin/email/daily-report/settings`` 응답에서 ``next_run_at`` 를
    채울 때 본 함수의 ``ScheduleSummary.next_run_time`` 을 그대로 사용한다.
    """
    for summary in list_schedules():
        if summary.job_id == JOB_ID_DAILY_REPORT:
            return summary
    return None


def ensure_daily_report_cron_registered() -> None:
    """startup 시 daily report cron 잡을 SystemSetting 기반으로 복원한다.

    ``create_app()`` 에서 ``ensure_backup_cron_registered()`` 바로 다음 라인에
    호출된다 (디자인 노트 §8 startup 복원). 스케줄러가 아직 running 이 아닌
    경우 조용히 no-op 처리한다.

    동작:
        - ``email.daily_report.enabled`` 가 ``True`` 면
          ``register_daily_report_cron_schedule(cron, enabled=True)`` 로 등록·갱신.
          (이미 jobstore 에서 자동 복원된 잡이 있으면 reschedule 흐름을 타고 cron
          표현식이 SystemSetting 값으로 동기화된다.)
        - ``email.daily_report.enabled`` 가 ``False`` 면
          ``register_daily_report_cron_schedule(enabled=False)`` 로 통일 — jobstore
          에 남아 있던 복원분도 함께 제거된다 (디자인 노트 §8 결정).
    """
    with _scheduler_lock:
        scheduler = _scheduler
    if scheduler is None or not getattr(scheduler, "running", False):
        logger.debug("ensure_daily_report_cron_registered: 스케줄러 미기동 — 스킵")
        return

    try:
        # 인자 None 으로 호출하면 본 함수가 SystemSetting 에서 직접 로드해
        # add/remove 분기를 결정한다 (백업의 ensure 패턴 미러).
        summary = register_daily_report_cron_schedule()
    except ScheduleValidationError as exc:
        # 저장된 cron 이 잘못된 경우 — 운영자가 admin 페이지에서 수정해야 함.
        logger.warning(
            "Daily report cron startup 자동 등록 실패 (admin 페이지에서 수동 수정 필요): {}",
            exc,
        )
        return

    if summary is None:
        logger.info(
            "Daily report cron startup 복원: enabled=False — 잡 미등록 상태로 시작",
        )
    else:
        logger.info(
            "Daily report cron startup 복원: enabled=True spec={!r} next_run_time={}",
            summary.trigger_spec,
            summary.next_run_time,
        )


__all__ = [
    "GC_ORPHAN_DEFAULT_CRON",
    "JOB_ID_PREFIX_GC_ORPHAN",
    "JOB_NAME_GC_ORPHAN_PREFIX",
    "ScheduleSummary",
    "ScheduleValidationError",
    "add_cron_schedule",
    "add_gc_orphan_cron_schedule",
    "add_interval_schedule",
    "delete_schedule",
    "ensure_backup_cron_registered",
    "ensure_daily_report_cron_registered",
    "get_daily_report_schedule_summary",
    "is_scheduler_running",
    "list_schedules",
    "register_backup_cron_schedule",
    "register_daily_report_cron_schedule",
    "remove_backup_cron_schedule",
    "remove_daily_report_cron_schedule",
    "start",
    "stop",
    "toggle_schedule",
]
