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

# ── 백업 잡 식별자 (task 00094-2) ─────────────────────────────────────────────
# 백업 잡은 항상 1건만 존재한다. 고정 ID 를 사용해 중복 등록을 방지한다.
JOB_ID_BACKUP: Final[str] = "backup-db"
# job.name 에 저장할 prefix. 예) "backup-cron:0 3 * * *"
# _recompute_all_jobs_next_run_time 가 이 prefix 로 cron 표현식을 복원한다.
JOB_NAME_BACKUP_PREFIX: Final[str] = "backup-cron:"

# ── Daily Report 잡 식별자 (task 00125-7 / Phase A-3) ──────────────────────────
# Daily report 잡도 백업과 마찬가지로 항상 1건만 존재한다. SystemSetting
# (email.daily_report.enabled / cron_expression) 이 토글 source of truth 이고,
# 고정 ID 로 add_or_reschedule 흐름을 단순화한다.
# (디자인 노트 §8 결정 — 백업 잡의 ``backup-db`` / ``backup-cron:`` 패턴 미러)
JOB_ID_DAILY_REPORT: Final[str] = "daily-report"
# job.name 에 저장할 prefix. 예) "daily-report-cron:0 9 * * 1-5"
JOB_NAME_DAILY_REPORT_PREFIX: Final[str] = "daily-report-cron:"

# ── 일반 수집 스케줄 영속 저장 키 (task 00155-2) ──────────────────────────────
# 기존엔 일반 공고 수집 스케줄(cron/interval + active_sources)이 APScheduler
# jobstore(scheduler_jobs)에만 존재해, cron 데몬·crontab 생성기가 읽을 수 있는
# 외부 source of truth 가 없었다. system crontab 으로 전환하려면 컨테이너 기동
# 시 스케줄을 읽어 crontab 으로 렌더해야 하므로, Alembic 신규 테이블을 만들지
# 않고 SystemSetting 단일 키에 JSON 리스트로 영속화한다.
SETTING_KEY_GENERAL_SCHEDULES: Final[str] = "scheduler.general_schedules"

# ── GC 고아 첨부 파일 잡 식별자 (task 00041-5) ───────────────────────────────
# Phase 5a 의 고아 첨부 파일 GC 잡. service.py 의 ``add_gc_orphan_cron_schedule``
# 가 등록하고, ``_recompute_all_jobs_next_run_time`` / ``JsonSchedulerJobStore``
# 가 cron 표현식 복원에 사용한다. service 모듈 후반부에 정의돼 있던 값을
# constants.py 로 옮긴 이유는 task 00149-2 의 신규 jobstore 가 본 prefix 를
# 참조해야 하는데 service.py 와 순환 import 가 생기기 때문이다.
JOB_NAME_GC_ORPHAN_PREFIX: Final[str] = "gc-orphan-cron:"

# GC 고아 첨부 파일 잡의 기본 cron 표현식(KST 04:00 매일). 기존엔
# service.py 의 ``GC_ORPHAN_DEFAULT_CRON`` 에 있었으나, system crontab 생성기
# (task 00155-2)가 apscheduler 의존 모듈인 service.py 를 import 하지 않도록
# 중립 모듈(constants.py)에도 동일 값을 둔다. (00155-4 에서 service.py 의
# 사본이 제거되면 본 상수가 단일 진실이 된다.)
DEFAULT_GC_ORPHAN_CRON: Final[str] = "0 4 * * *"


# ── 스케줄 SSOT 전용 테이블 (task 00157) ──────────────────────────────────────
# task 155·156 을 거치며 스케줄 SSOT 가 system_settings JSON 키와 기동 시 설치되는
# OS crontab(외부 파일)로 이원화됐다. task 00157 은 모든 스케줄 트리거(공고 수집·
# 백업·Daily Report·GC)를 단일 관계형 테이블 ``scheduled_jobs`` 로 모아 SSOT 를
# DB 한 곳으로 되돌린다. 기동 시 이 테이블만 읽어 crontab 을 재생성하므로 외부 cron
# 파일을 별도로 백업·휴대할 필요가 없다.
#
# 주의: 155/156 에서 drop 된 APScheduler jobstore 테이블명은 ``scheduler_jobs``(r)
# 였고, 본 SSOT 테이블은 task 제목대로 ``scheduled_jobs``(d) 다. 철자·의미가 모두
# 달라 충돌하지 않으며, APScheduler pickle/job_state 잔재를 되살리지 않는다.
SCHEDULED_JOBS_TABLENAME: Final[str] = "scheduled_jobs"

# scheduled_jobs.job_kind 의 허용 값. 잡 종류를 한 컬럼으로 구분한다.
# - scrape_general: 일반 공고 수집(여러 row 가능, N건).
# - backup / daily_report / gc: 각각 단일 row(싱글턴).
JOB_KIND_SCRAPE_GENERAL: Final[str] = "scrape_general"
JOB_KIND_BACKUP: Final[str] = "backup"
JOB_KIND_DAILY_REPORT: Final[str] = "daily_report"
JOB_KIND_GC: Final[str] = "gc"

# 싱글턴(항상 1건만 존재)인 잡 종류 모음. 시드·스토어 계층이 이 집합을 보고
# '존재하지 않을 때만 기본값으로 1건 시드' 하는 멱등 보장을 수행한다.
SINGLETON_JOB_KINDS: Final[tuple[str, ...]] = (
    JOB_KIND_BACKUP,
    JOB_KIND_DAILY_REPORT,
    JOB_KIND_GC,
)

# scheduled_jobs.trigger_type 의 허용 값. cron 표현식 기반 / '매 N시간' interval 기반.
TRIGGER_TYPE_CRON: Final[str] = "cron"
TRIGGER_TYPE_INTERVAL: Final[str] = "interval"


__all__ = [
    "DEFAULT_GC_ORPHAN_CRON",
    "DEFAULT_MISFIRE_GRACE_TIME_SEC",
    "JOB_ID_BACKUP",
    "JOB_ID_DAILY_REPORT",
    "JOB_ID_PREFIX_CRON",
    "JOB_ID_PREFIX_INTERVAL",
    "JOB_KIND_BACKUP",
    "JOB_KIND_DAILY_REPORT",
    "JOB_KIND_GC",
    "JOB_KIND_SCRAPE_GENERAL",
    "JOB_NAME_BACKUP_PREFIX",
    "JOB_NAME_CRON_PREFIX",
    "JOB_NAME_DAILY_REPORT_PREFIX",
    "JOB_NAME_GC_ORPHAN_PREFIX",
    "JOB_NAME_INTERVAL_PREFIX",
    "MAX_INTERVAL_HOURS",
    "SCHEDULED_JOBS_TABLENAME",
    "SCHEDULER_JOBS_TABLENAME",
    "SETTING_KEY_GENERAL_SCHEDULES",
    "SINGLETON_JOB_KINDS",
    "TRIGGER_TYPE_CRON",
    "TRIGGER_TYPE_INTERVAL",
]
