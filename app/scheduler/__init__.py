"""system crontab 기반 스케줄 관리 패키지 (task 00155).

배경 (구조 전환):
    예전에는 웹 프로세스 내부 APScheduler(SW 스케줄러)가 공고 수집·백업·Daily
    Report·GC 를 돌렸다. 그러나 그 단일 스레드가 한 번의 ``database is locked``
    예외로 사망하면(error.log 18b0d4249dbf) 이후 모든 스케줄이 영구 정지하는
    single-point-of-failure 였다. task 00155 에서 APScheduler 를 완전히 걷어내고,
    컨테이너 기동 시 스케줄 설정을 읽어 **실제 OS crontab** 에 등록한 뒤 cron
    데몬이 직접 작업 CLI(:mod:`app.scheduler.run_job`)를 호출하는 구조로 바꿨다.

공개 API (관리자 라우트가 사용하는 표면):
    - 일반 수집 스케줄 영속 저장소(:mod:`app.scheduler.schedule_store`):
      ``list/get/add/delete/toggle`` + ``GeneralScheduleRecord`` + 모드 상수.
    - cron 표현식 검증(:func:`validate_cron_expression`) + 예외
      (:class:`CronExpressionError`).
    - 설정 변경 후 crontab 재설치(:func:`reinstall_crontab_after_change`).

외부(admin 라우트 등)에서는 본 패키지의 공개 심볼만 import 하고, 하위 모듈을
직접 참조하지 않는다.
"""

from __future__ import annotations

from app.scheduler.crontab_generator import (
    CronExpressionError,
    validate_cron_expression,
)
from app.scheduler.crontab_installer import (
    CrontabInstallResult,
    install_crontab,
    is_crontab_reinstall_enabled,
    reinstall_crontab_after_change,
)
from app.scheduler.schedule_store import (
    SCHEDULE_MODE_CRON,
    SCHEDULE_MODE_INTERVAL,
    GeneralScheduleRecord,
    ScheduleConfigError,
    add_general_schedule_record,
    delete_general_schedule_record,
    get_general_schedule_record,
    list_general_schedule_records,
    set_general_schedule_enabled,
)

__all__ = [
    "CronExpressionError",
    "CrontabInstallResult",
    "GeneralScheduleRecord",
    "SCHEDULE_MODE_CRON",
    "SCHEDULE_MODE_INTERVAL",
    "ScheduleConfigError",
    "add_general_schedule_record",
    "delete_general_schedule_record",
    "get_general_schedule_record",
    "install_crontab",
    "is_crontab_reinstall_enabled",
    "list_general_schedule_records",
    "reinstall_crontab_after_change",
    "set_general_schedule_enabled",
    "validate_cron_expression",
]
