"""system_settings 의 스케줄 트리거를 ``scheduled_jobs`` 로 무손실·멱등 이관 (task 00157).

배경
----
task 155·156 까지는 스케줄 트리거가 ``system_settings`` 에 흩어져 있었다:

- 일반 공고 수집: JSON 리스트 키 ``scheduler.general_schedules``
  (``[{mode, cron_expression, interval_hours, active_sources, enabled}, ...]``).
- DB 백업: ``backup.cron_expression``.
- Daily Report: ``email.daily_report.cron_expression`` + ``email.daily_report.enabled``.
- 고아 첨부 GC: 상수 기본값(외부 저장 없음).

task 00157 은 이 트리거들을 단일 관계형 테이블 ``scheduled_jobs`` 로 모은다. 이 모듈은
그 **일회성 데이터 이관 + 싱글턴 기본 시드**를 담당한다. Alembic upgrade 가
``scheduled_jobs`` 를 CREATE 한 직후 :func:`migrate_system_settings_to_scheduled_jobs`
를 호출한다.

이관 대상이 아닌 것
-------------------
메일/백업의 **비-스케줄 설정**(SMTP 자격증명·``backup.max_count``·Daily Report 수신자/
본문 설정 등)은 싱글턴 설정이므로 ``system_settings`` 에 그대로 둔다. 이 모듈은
'언제 실행할지'(cron/interval/enabled 트리거)만 옮긴다.

멱등성
------
- ``scrape_general``: dedupe 키 (trigger_type, 정규화 cron, interval_hours, 정렬
  active_sources)로 이미 존재하는 row 는 다시 넣지 않는다.
- ``backup`` / ``daily_report`` / ``gc``: 해당 종류 row 가 이미 있으면 시드하지
  않는다.
- 두 번 호출해도 두 번째는 0건을 추가한다(legacy_backfill.py 와 동일한 백필 철학).

순수 SQLAlchemy Core(Connection) 기반이라 Alembic 컨텍스트 밖(단위 테스트)에서도
임의의 Connection 으로 직접 호출해 멱등성을 검증할 수 있다.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.scheduler.constants import (
    DEFAULT_GC_ORPHAN_CRON,
    JOB_KIND_BACKUP,
    JOB_KIND_DAILY_REPORT,
    JOB_KIND_GC,
    JOB_KIND_SCRAPE_GENERAL,
    SCHEDULED_JOBS_TABLENAME,
    SETTING_KEY_GENERAL_SCHEDULES,
    TRIGGER_TYPE_CRON,
    TRIGGER_TYPE_INTERVAL,
)
from app.timezone import now_utc

# Core 표현식용 경량 테이블 핸들. 타입을 명시해 ORM 읽기와 직렬화 의미(JSON
# dumps/loads, DateTime 포맷, Boolean 0/1)를 정확히 일치시킨다.
_scheduled_jobs = sa.table(
    SCHEDULED_JOBS_TABLENAME,
    sa.column("job_kind", sa.String),
    sa.column("trigger_type", sa.String),
    sa.column("cron_expression", sa.Text),
    sa.column("interval_hours", sa.Integer),
    sa.column("active_sources", sa.JSON),
    sa.column("enabled", sa.Boolean),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

_system_settings = sa.table(
    "system_settings",
    sa.column("key", sa.String),
    sa.column("value", sa.Text),
)

# Daily Report 활성화 기본값과 cron 기본값. 도메인 상수(app.email.constants)와 동일
# 값을 사용하되, 본 마이그레이션이 email 패키지에 import 의존하지 않도록 리터럴로 둔다.
_DEFAULT_BACKUP_CRON: str = "0 3 * * *"
_DEFAULT_DAILY_REPORT_CRON: str = "0 9 * * 1-5"


def _normalize_cron_expression(raw: Any) -> str:
    """cron 표현식의 중복 공백을 단일 공백으로 정리한다(dedupe 비교 일관성).

    Args:
        raw: 원본 표현식(문자열이 아닐 수도 있어 방어적으로 str 변환).

    Returns:
        공백이 정리된 표현식. 비어 있으면 빈 문자열.
    """
    return " ".join(str(raw or "").split())


def _normalize_active_sources(raw: Any) -> list[str]:
    """active_sources 입력을 공백 제거·빈 토큰 제거한 리스트로 정규화한다.

    Args:
        raw: 원본 리스트(None/비-리스트 허용).

    Returns:
        정규화된 source id 리스트. 입력이 비정상이면 빈 리스트.
    """
    if not isinstance(raw, list):
        return []
    return [str(source).strip() for source in raw if str(source).strip()]


def _get_setting_value(connection: Connection, key: str) -> str | None:
    """system_settings 에서 단일 키 값을 읽는다.

    Args:
        connection: Core Connection.
        key: 설정 키.

    Returns:
        값 문자열. 키가 없으면 None.
    """
    row = connection.execute(
        sa.select(_system_settings.c.value).where(_system_settings.c.key == key)
    ).first()
    if row is None:
        return None
    return row[0]


def _scrape_dedupe_key(
    trigger_type: str,
    cron_expression: str | None,
    interval_hours: int | None,
    active_sources: list[str],
) -> tuple[str, str, int, tuple[str, ...]]:
    """scrape_general 중복 판정용 정규화 키를 만든다.

    Args:
        trigger_type: 'cron' 또는 'interval'.
        cron_expression: cron 표현식(None 허용).
        interval_hours: '매 N시간'(None 허용).
        active_sources: 정규화된 source id 리스트.

    Returns:
        해시 가능한 dedupe 키 튜플.
    """
    return (
        trigger_type,
        _normalize_cron_expression(cron_expression),
        int(interval_hours) if interval_hours is not None else -1,
        tuple(sorted(active_sources)),
    )


def _existing_scrape_keys(
    connection: Connection,
) -> set[tuple[str, str, int, tuple[str, ...]]]:
    """이미 저장된 scrape_general row 들의 dedupe 키 집합을 만든다.

    Args:
        connection: Core Connection.

    Returns:
        기존 scrape_general row 의 dedupe 키 집합.
    """
    rows = connection.execute(
        sa.select(
            _scheduled_jobs.c.trigger_type,
            _scheduled_jobs.c.cron_expression,
            _scheduled_jobs.c.interval_hours,
            _scheduled_jobs.c.active_sources,
        ).where(_scheduled_jobs.c.job_kind == JOB_KIND_SCRAPE_GENERAL)
    ).all()
    keys: set[tuple[str, str, int, tuple[str, ...]]] = set()
    for trigger_type, cron_expression, interval_hours, active_sources in rows:
        keys.add(
            _scrape_dedupe_key(
                trigger_type,
                cron_expression,
                interval_hours,
                _normalize_active_sources(active_sources),
            )
        )
    return keys


def _kind_exists(connection: Connection, job_kind: str) -> bool:
    """특정 job_kind row 가 1건이라도 존재하는지 확인한다.

    Args:
        connection: Core Connection.
        job_kind: 확인할 잡 종류.

    Returns:
        존재하면 True.
    """
    row = connection.execute(
        sa.select(sa.literal(1))
        .select_from(_scheduled_jobs)
        .where(_scheduled_jobs.c.job_kind == job_kind)
        .limit(1)
    ).first()
    return row is not None


def _insert_job(
    connection: Connection,
    *,
    job_kind: str,
    trigger_type: str,
    cron_expression: str | None,
    interval_hours: int | None,
    active_sources: list[str] | None,
    enabled: bool,
) -> None:
    """scheduled_jobs 에 1건 INSERT 한다(created_at/updated_at 자동 채움).

    Args:
        connection: Core Connection.
        job_kind: 잡 종류.
        trigger_type: 'cron' 또는 'interval'.
        cron_expression: cron 표현식(None 허용).
        interval_hours: '매 N시간'(None 허용).
        active_sources: scrape_general 전용 리스트(그 외 None).
        enabled: 활성 여부.
    """
    timestamp = now_utc()
    connection.execute(
        _scheduled_jobs.insert().values(
            job_kind=job_kind,
            trigger_type=trigger_type,
            cron_expression=cron_expression,
            interval_hours=interval_hours,
            active_sources=active_sources,
            enabled=enabled,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )


def _migrate_general_schedules(connection: Connection) -> int:
    """system_settings 의 일반 수집 스케줄 JSON 을 scrape_general row 로 이관한다.

    Args:
        connection: Core Connection.

    Returns:
        새로 삽입한 scrape_general row 수.
    """
    raw_value = _get_setting_value(connection, SETTING_KEY_GENERAL_SCHEDULES)
    if raw_value in (None, ""):
        return 0
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return 0
    if not isinstance(parsed, list):
        return 0

    existing_keys = _existing_scrape_keys(connection)
    inserted = 0
    for item in parsed:
        if not isinstance(item, dict):
            continue
        # 구 schedule_store 의 'mode' 필드가 신규 trigger_type 과 동일 값 도메인
        # ('cron'/'interval')을 가진다.
        trigger_type = str(item.get("mode", TRIGGER_TYPE_CRON))
        active_sources = _normalize_active_sources(item.get("active_sources"))
        enabled = bool(item.get("enabled", True))

        if trigger_type == TRIGGER_TYPE_INTERVAL:
            raw_hours = item.get("interval_hours")
            if not isinstance(raw_hours, int) or raw_hours <= 0:
                continue
            cron_expression = None
            interval_hours: int | None = raw_hours
        else:
            trigger_type = TRIGGER_TYPE_CRON
            cron_expression = _normalize_cron_expression(item.get("cron_expression"))
            if not cron_expression:
                continue
            interval_hours = None

        dedupe_key = _scrape_dedupe_key(
            trigger_type, cron_expression, interval_hours, active_sources
        )
        if dedupe_key in existing_keys:
            continue
        existing_keys.add(dedupe_key)

        _insert_job(
            connection,
            job_kind=JOB_KIND_SCRAPE_GENERAL,
            trigger_type=trigger_type,
            cron_expression=cron_expression,
            interval_hours=interval_hours,
            active_sources=active_sources,
            enabled=enabled,
        )
        inserted += 1
    return inserted


def _seed_backup(connection: Connection) -> int:
    """백업 싱글턴이 없으면 system_settings(backup.cron_expression) 기준으로 시드한다.

    Args:
        connection: Core Connection.

    Returns:
        시드한 row 수(0 또는 1).
    """
    if _kind_exists(connection, JOB_KIND_BACKUP):
        return 0
    cron = _normalize_cron_expression(
        _get_setting_value(connection, "backup.cron_expression")
    )
    if not cron:
        cron = _DEFAULT_BACKUP_CRON
    _insert_job(
        connection,
        job_kind=JOB_KIND_BACKUP,
        trigger_type=TRIGGER_TYPE_CRON,
        cron_expression=cron,
        interval_hours=None,
        active_sources=None,
        enabled=True,
    )
    return 1


def _seed_daily_report(connection: Connection) -> int:
    """Daily Report 싱글턴이 없으면 system_settings(email.daily_report.*) 기준 시드한다.

    cron 과 enabled 를 모두 system_settings 에서 읽어 트리거 상태를 보존한다.

    Args:
        connection: Core Connection.

    Returns:
        시드한 row 수(0 또는 1).
    """
    if _kind_exists(connection, JOB_KIND_DAILY_REPORT):
        return 0
    cron = _normalize_cron_expression(
        _get_setting_value(connection, "email.daily_report.cron_expression")
    )
    if not cron:
        cron = _DEFAULT_DAILY_REPORT_CRON
    raw_enabled = _get_setting_value(connection, "email.daily_report.enabled")
    enabled = (raw_enabled or "").strip().lower() == "true"
    _insert_job(
        connection,
        job_kind=JOB_KIND_DAILY_REPORT,
        trigger_type=TRIGGER_TYPE_CRON,
        cron_expression=cron,
        interval_hours=None,
        active_sources=None,
        enabled=enabled,
    )
    return 1


def _seed_gc(connection: Connection) -> int:
    """GC 싱글턴이 없으면 기본 cron(:data:`DEFAULT_GC_ORPHAN_CRON`)으로 시드한다.

    Args:
        connection: Core Connection.

    Returns:
        시드한 row 수(0 또는 1).
    """
    if _kind_exists(connection, JOB_KIND_GC):
        return 0
    _insert_job(
        connection,
        job_kind=JOB_KIND_GC,
        trigger_type=TRIGGER_TYPE_CRON,
        cron_expression=DEFAULT_GC_ORPHAN_CRON,
        interval_hours=None,
        active_sources=None,
        enabled=True,
    )
    return 1


def migrate_system_settings_to_scheduled_jobs(connection: Connection) -> int:
    """system_settings 스케줄 트리거를 scheduled_jobs 로 이관하고 싱글턴을 시드한다.

    멱등하다 — 이미 존재하는 row 는 다시 넣지 않으므로 두 번 호출해도 안전하다.
    신규 설치(빈 system_settings)에서도 backup/daily_report/gc 기본 시드가 보장된다.

    Args:
        connection: ``scheduled_jobs`` 가 이미 생성된 DB 의 Core Connection
            (Alembic upgrade 의 ``op.get_bind()`` 또는 테스트용 임의 Connection).

    Returns:
        새로 삽입한 총 row 수(이관 + 시드).
    """
    inserted = _migrate_general_schedules(connection)
    inserted += _seed_backup(connection)
    inserted += _seed_daily_report(connection)
    inserted += _seed_gc(connection)
    return inserted


__all__ = [
    "migrate_system_settings_to_scheduled_jobs",
]
