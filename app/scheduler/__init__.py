"""웹 프로세스 내부에서 도는 APScheduler 통합 (Phase 2 / 00025-6).

관리자 페이지 [스케줄] 탭이 사용하는 API 집합.
외부에서는 본 패키지의 공개 심볼만 쓰고 하위 모듈(service/job_runner/constants)
을 직접 import 하지 않는다.

설계 근거:
    docs/scrape_control_design.md §13. APScheduler 3.x BackgroundScheduler +
    SQLAlchemyJobStore('scheduler_jobs' 테이블) + misfire_grace_time·coalesce·
    max_instances=1 보호.
"""

from __future__ import annotations

from app.scheduler.constants import (
    DEFAULT_MISFIRE_GRACE_TIME_SEC,
    SCHEDULER_JOBS_TABLENAME,
)
from app.scheduler.job_runner import scheduled_scrape
from app.scheduler.service import (
    ScheduleSummary,
    ScheduleValidationError,
    add_cron_schedule,
    add_interval_schedule,
    delete_schedule,
    is_scheduler_running,
    list_schedules,
    start as start_scheduler,
    stop as stop_scheduler,
    toggle_schedule,
)

__all__ = [
    "DEFAULT_MISFIRE_GRACE_TIME_SEC",
    "SCHEDULER_JOBS_TABLENAME",
    "ScheduleSummary",
    "ScheduleValidationError",
    "add_cron_schedule",
    "add_interval_schedule",
    "delete_schedule",
    "is_scheduler_running",
    "list_schedules",
    "scheduled_scrape",
    "start_scheduler",
    "stop_scheduler",
    "toggle_schedule",
]
