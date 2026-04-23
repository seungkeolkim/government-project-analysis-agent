"""웹/스케줄러가 스크래퍼 subprocess 를 제어하는 서비스 레이어.

외부에서는 이 패키지의 공개 API 만 사용하고 하위 모듈을 직접 import 하지 않는다.
"""

from __future__ import annotations

from app.scrape_control.cancel import request_cancel
from app.scrape_control.constants import (
    SCRAPE_ACTIVE_SOURCES_ENV_VAR,
    ExternalTrigger,
    scrape_run_log_path,
    scrape_run_log_root,
)
from app.scrape_control.runner import (
    ComposeEnvironmentError,
    ScrapeAlreadyRunningError,
    StartResult,
    build_compose_command,
    start_scrape_run,
)
from app.scrape_control.stale import cleanup_stale_running_runs

__all__ = [
    "ComposeEnvironmentError",
    "ExternalTrigger",
    "SCRAPE_ACTIVE_SOURCES_ENV_VAR",
    "ScrapeAlreadyRunningError",
    "StartResult",
    "build_compose_command",
    "cleanup_stale_running_runs",
    "request_cancel",
    "scrape_run_log_path",
    "scrape_run_log_root",
    "start_scrape_run",
]
