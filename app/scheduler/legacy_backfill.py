"""레거시 APScheduler jobstore(``scheduler_jobs``) → 신규 SSOT 백필 (task 00156-1).

배경
----
task 155 가 일반 공고 수집 스케줄의 단일 진실(SSOT)을, 레거시 APScheduler
jobstore 테이블(``scheduler_jobs``)에서 신규 SystemSetting JSON 키
(:data:`~app.scheduler.constants.SETTING_KEY_GENERAL_SCHEDULES`)로 옮겼다.
그런데 155-4(commit f88a45e)가 ``app/scheduler/jobstore.py`` 와 APScheduler 를
제거하면서, 그 레거시 테이블에만 존재하던 기존 일반 수집 스케줄(예:
``0 1 * * *``, ``0 13 * * *``)을 신규 저장소로 옮기지 않았다. 결과적으로
사용자 관점에서 '예전 스케줄이 화면·crontab 에서 사라지고, DB(scheduler_jobs)
에는 유령처럼 남아 있는' 정합성 오류가 발생했다.

이 모듈은 그 유실분을 **1회 멱등 백필**로 복구한다. Alembic 데이터
마이그레이션이 이 함수를 호출한다(entrypoint 가 기동 시 ``alembic upgrade
head`` 를 먼저 수행한 뒤 crontab 을 재생성하므로, 마이그레이션에서 신규
저장소를 채우면 그 직후 crontab 재생성이 복구된 스케줄을 반영한다).

대상/제외
---------
- **대상**: 레거시 잡의 ``job_state.name`` 이 ``cron:`` / ``interval:`` prefix 로
  시작하는 '일반 공고 수집' 잡(:data:`~app.scheduler.constants.JOB_NAME_CRON_PREFIX`
  / :data:`~app.scheduler.constants.JOB_NAME_INTERVAL_PREFIX`).
- **제외**: 백업(``backup-cron:`` / id ``backup-db``), Daily Report
  (``daily-report-cron:`` / id ``daily-report``), 고아 GC(``gc-orphan-cron:``)
  잡. 이들은 본래부터 SystemSetting(``backup.cron_expression`` /
  ``email.daily_report.*``)이 SSOT 라 유실이 없으며, 별도 백필이 불필요하다
  (:func:`app.scheduler.crontab_generator.collect_system_jobs` 가 SystemSetting
  만 읽는다는 점이 그 근거).

설계 메모
---------
- **순수 connection 기반**: ORM 세션을 만들지 않고, 주어진
  :class:`sqlalchemy.engine.Connection`(alembic bind 또는 테스트의
  ``session.connection()``) 위에서 raw SQL 만 실행한다. alembic 컨텍스트에서
  ORM 모델/세션 부트스트랩 부담을 피하기 위함이다.
- **요일 보정 금지**: system cron 규약 그대로 ``cron_expression`` 컬럼값을
  옮긴다(:mod:`app.scheduler.crontab_generator` 의 핵심 설계 결정과 동일).
- **신규 id 부여**: 레거시 job id 를 재사용하지 않고 ``uuid4().hex`` 를 새로
  부여한다(신규 저장소 규약과 일치).
- **멱등**: dedupe 키는 ``(mode, normalized cron_expression 또는
  interval_hours, sorted active_sources)``. 이미 신규 저장소에 같은 조합이
  있으면(예: 사용자가 155 후 직접 추가한 ``15 11 * * *``) 추가하지 않는다.
  두 번 실행해도 중복이 생기지 않는다.
- **paused 보존**: 레거시 잡이 paused(next_run_time NULL)였으면 신규 레코드도
  ``enabled=False`` 로 옮긴다.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.scheduler.constants import (
    JOB_NAME_CRON_PREFIX,
    JOB_NAME_INTERVAL_PREFIX,
    MAX_INTERVAL_HOURS,
    SCHEDULER_JOBS_TABLENAME,
    SETTING_KEY_GENERAL_SCHEDULES,
)
from app.scheduler.schedule_store import (
    SCHEDULE_MODE_CRON,
    SCHEDULE_MODE_INTERVAL,
)

# 신규 저장소 레코드의 dedupe 정규화에 쓰는 cron 표현식 정리(중복 공백 → 단일
# 공백). crontab_generator/schedule_store 의 표현식 정규화와 동일한 규칙이다.


def _normalize_cron_expression(cron_expression: str | None) -> str:
    """cron 표현식의 중복 공백을 단일 공백으로 정리해 dedupe 비교에 쓴다.

    Args:
        cron_expression: 원본 cron 표현식(None 허용).

    Returns:
        필드 사이 공백이 정리된 표현식. 입력이 None/빈 문자열이면 빈 문자열.
    """
    if not cron_expression:
        return ""
    return " ".join(cron_expression.split())


def _dedupe_key(record: dict[str, Any]) -> tuple[Any, ...]:
    """신규 저장소 레코드 1건의 멱등 dedupe 키를 만든다.

    키 구성: (mode, 정규화 cron_expression 또는 interval_hours, 정렬된
    active_sources). 같은 키가 이미 있으면 백필 대상에서 제외해 중복을 막는다.

    Args:
        record: ``GeneralScheduleRecord.to_dict()`` 와 동일 형태의 dict.

    Returns:
        비교용 튜플 키.
    """
    sorted_sources = tuple(sorted(record.get("active_sources") or []))
    if record.get("mode") == SCHEDULE_MODE_INTERVAL:
        return (SCHEDULE_MODE_INTERVAL, record.get("interval_hours"), sorted_sources)
    return (
        SCHEDULE_MODE_CRON,
        _normalize_cron_expression(record.get("cron_expression")),
        sorted_sources,
    )


def _extract_active_sources(job_state: dict[str, Any]) -> list[str]:
    """레거시 job_state 의 args 에서 active_sources 를 복원한다.

    레거시 일반 수집 잡은 ``args=[active_sources_list]`` 형태(리스트 1개를
    감싼 리스트)로 등록됐다(``app/scheduler/service.py`` 의 add_*_schedule).
    따라서 ``args[0]`` 가 source id 리스트다. 복원 불가하면 빈 리스트(=전체).

    Args:
        job_state: 레거시 ``job_state`` 컬럼을 파싱한 dict.

    Returns:
        source id 문자열 리스트. 복원 실패 시 빈 리스트.
    """
    args = job_state.get("args")
    if isinstance(args, list) and args and isinstance(args[0], list):
        return [str(source) for source in args[0] if str(source).strip()]
    return []


def _legacy_row_to_record(row: Any) -> dict[str, Any] | None:
    """레거시 ``scheduler_jobs`` row 1건을 신규 저장소 레코드 dict 로 변환한다.

    일반 공고 수집 잡(job_state.name 이 ``cron:`` / ``interval:`` prefix)만
    변환하고, 백업/Daily Report/GC 등 그 외 잡은 None 을 반환해 건너뛴다.

    Args:
        row: ``id, next_run_time, trigger_type, cron_expression,
            interval_seconds, job_state`` 컬럼을 가진 SELECT row.

    Returns:
        ``GeneralScheduleRecord.to_dict()`` 와 동일 필드(id/mode/cron_expression/
        interval_hours/active_sources/enabled)를 가진 dict. 대상이 아니거나
        복원 실패면 None.
    """
    # job_state JSON 파싱 — 손상된 row 는 조용히 건너뛴다.
    try:
        job_state = json.loads(row.job_state) if row.job_state else {}
    except (TypeError, ValueError):
        return None
    if not isinstance(job_state, dict):
        return None

    name = job_state.get("name")
    if not isinstance(name, str):
        return None

    # paused(next_run_time NULL) 였으면 비활성으로 복구한다.
    enabled = row.next_run_time is not None
    active_sources = _extract_active_sources(job_state)

    if name.startswith(JOB_NAME_CRON_PREFIX):
        cron_expression = _normalize_cron_expression(row.cron_expression)
        if not cron_expression:
            # cron 잡인데 표현식 컬럼이 비었으면 복원 불가 — 건너뛴다.
            return None
        return {
            "id": uuid.uuid4().hex,
            "mode": SCHEDULE_MODE_CRON,
            "cron_expression": cron_expression,
            "interval_hours": None,
            "active_sources": active_sources,
            "enabled": enabled,
        }

    if name.startswith(JOB_NAME_INTERVAL_PREFIX):
        interval_seconds = row.interval_seconds
        if not interval_seconds or interval_seconds <= 0:
            return None
        # 초 → 시간 환산. 레거시는 hours*3600 으로 저장했으므로 반올림으로
        # 안전하게 정수 시간을 복원한다.
        interval_hours = round(interval_seconds / 3600)
        if interval_hours < 1:
            interval_hours = 1
        if interval_hours > MAX_INTERVAL_HOURS:
            # 신규 저장소 규약(최대 24시간)을 넘으면 백필 대상에서 제외한다.
            return None
        return {
            "id": uuid.uuid4().hex,
            "mode": SCHEDULE_MODE_INTERVAL,
            "cron_expression": None,
            "interval_hours": interval_hours,
            "active_sources": active_sources,
            "enabled": enabled,
        }

    # 그 외(backup-cron:/daily-report-cron:/gc-orphan-cron:/알 수 없는) 잡 제외.
    return None


def _load_existing_records(connection: Connection) -> list[dict[str, Any]]:
    """신규 SSOT(system_settings) 에 이미 저장된 일반 수집 레코드를 로드한다.

    값이 없거나 JSON 파싱에 실패하면 빈 리스트로 취급한다(백필이 통째로 죽지
    않도록 방어적).

    Args:
        connection: SQL 을 실행할 connection.

    Returns:
        저장된 원본 dict 리스트(없거나 손상되면 빈 리스트).
    """
    value = connection.execute(
        text("SELECT value FROM system_settings WHERE key = :key"),
        {"key": SETTING_KEY_GENERAL_SCHEDULES},
    ).scalar()
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _save_records(connection: Connection, records: list[dict[str, Any]]) -> None:
    """레코드 리스트를 system_settings JSON 으로 직렬화해 upsert 한다.

    key 가 이미 있으면 UPDATE, 없으면 INSERT 한다. dialect 중립을 위해
    select → insert/update 분기로 처리한다.

    Args:
        connection: SQL 을 실행할 connection.
        records: 저장할 전체 레코드 dict 리스트.
    """
    serialized = json.dumps(records, ensure_ascii=False)
    now = datetime.now(UTC)
    exists = connection.execute(
        text("SELECT 1 FROM system_settings WHERE key = :key"),
        {"key": SETTING_KEY_GENERAL_SCHEDULES},
    ).scalar()
    if exists is None:
        connection.execute(
            text(
                "INSERT INTO system_settings (key, value, updated_at) "
                "VALUES (:key, :value, :updated_at)"
            ),
            {
                "key": SETTING_KEY_GENERAL_SCHEDULES,
                "value": serialized,
                "updated_at": now,
            },
        )
    else:
        connection.execute(
            text(
                "UPDATE system_settings SET value = :value, updated_at = :updated_at "
                "WHERE key = :key"
            ),
            {
                "key": SETTING_KEY_GENERAL_SCHEDULES,
                "value": serialized,
                "updated_at": now,
            },
        )


def backfill_general_schedules_from_legacy(connection: Connection) -> int:
    """레거시 ``scheduler_jobs`` 의 일반 수집 스케줄을 신규 SSOT 로 멱등 백필한다.

    절차:
        1. ``scheduler_jobs`` 테이블이 없으면 no-op(0 반환).
        2. 레거시 row 를 읽어 일반 수집 잡(cron:/interval: name)만 신규 레코드로
           변환(백업/Daily Report/GC 제외).
        3. 이미 신규 저장소에 있는 레코드 + 이번 변환분 사이에서 dedupe 키로
           중복을 제거(사용자가 추가한 스케줄 보존).
        4. 신규 레코드를 기존 목록 뒤에 덧붙여 system_settings 에 upsert.

    멱등하다: 같은 입력으로 두 번 호출해도 두 번째 호출은 0건을 추가한다.

    Args:
        connection: SQL 을 실행할 SQLAlchemy connection(alembic bind 또는
            테스트의 ``session.connection()``).

    Returns:
        신규로 백필된(추가된) 레코드 수. no-op 이면 0.
    """
    inspector = inspect(connection)
    if SCHEDULER_JOBS_TABLENAME not in inspector.get_table_names():
        # 레거시 테이블이 없는 환경(신규 설치/이미 드롭됨) — 안전하게 통과.
        return 0

    legacy_rows = connection.execute(
        text(
            "SELECT id, next_run_time, trigger_type, cron_expression, "
            "interval_seconds, job_state "
            f"FROM {SCHEDULER_JOBS_TABLENAME}"
        )
    ).all()

    existing_records = _load_existing_records(connection)
    seen_keys = {_dedupe_key(record) for record in existing_records}

    added_records: list[dict[str, Any]] = []
    for row in legacy_rows:
        candidate = _legacy_row_to_record(row)
        if candidate is None:
            continue
        key = _dedupe_key(candidate)
        if key in seen_keys:
            # 이미 신규 저장소에 존재하거나(예: 사용자 추가분) 이번 백필에서
            # 동일 조합을 이미 옮겼음 — 중복 추가 방지.
            continue
        seen_keys.add(key)
        added_records.append(candidate)

    if not added_records:
        # 추가할 것이 없으면 기존 값을 건드리지 않는다(멱등 보장).
        return 0

    _save_records(connection, existing_records + added_records)
    return len(added_records)


__all__ = [
    "backfill_general_schedules_from_legacy",
]
