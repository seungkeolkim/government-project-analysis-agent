"""스케줄러 전역 상수.

설계 문서 §13 에서 결정된 값들. 외부에서는 이름 기반으로 import 해 동일 값을
공유한다 (라우트·서비스·job_runner 에 중복 리터럴을 두지 않는다).
"""

from __future__ import annotations

from typing import Final

# APScheduler SQLAlchemyJobStore 가 사용할 테이블명. 사용자 원문: 'SQLite
# scheduler_jobs 테이블'. alembic 관리 밖(APScheduler 가 자체 생성) 이며,
# 마이그레이션 툴과 이름이 겹치지 않도록 'scheduler_jobs' 로 고정한다.
# 설계 문서 §13.2 참조.
SCHEDULER_JOBS_TABLENAME: Final[str] = "scheduler_jobs"

# misfire_grace_time: 웹 프로세스가 정지돼 예정 시각을 놓친 잡을 그 이후 몇
# 초까지 '뒤늦게라도 실행' 으로 볼지. guidance 명시: '재시작 직후 누락된 잡이
# 한꺼번에 폭주하지 않게 보호'. 기본 5분(300초) — coalesce=True 와 병용해 한
# 번에 합쳐 실행한다.
DEFAULT_MISFIRE_GRACE_TIME_SEC: Final[int] = 300

# 이 값을 초과한 '매 N시간' 입력은 거부한다 (cron 없이 단순 interval 을 너무
# 길게 잡으면 UX 혼란). 하루 단위 이상 반복은 cron 탭을 쓰라고 안내.
MAX_INTERVAL_HOURS: Final[int] = 24

# job_id prefix. 스케줄 타입 식별이 테이블에서 바로 가능하도록 prefix 로 분리.
JOB_ID_PREFIX_CRON: Final[str] = "cron"
JOB_ID_PREFIX_INTERVAL: Final[str] = "interval"

# job.name 에 trigger_spec 을 prefix 와 함께 저장해두면 list 시점에 역직렬화가
# 쉽다. 예) "cron:0 3 * * *" / "interval:매 6시간".
JOB_NAME_CRON_PREFIX: Final[str] = "cron:"
JOB_NAME_INTERVAL_PREFIX: Final[str] = "interval:"


__all__ = [
    "DEFAULT_MISFIRE_GRACE_TIME_SEC",
    "JOB_ID_PREFIX_CRON",
    "JOB_ID_PREFIX_INTERVAL",
    "JOB_NAME_CRON_PREFIX",
    "JOB_NAME_INTERVAL_PREFIX",
    "MAX_INTERVAL_HOURS",
    "SCHEDULER_JOBS_TABLENAME",
]
