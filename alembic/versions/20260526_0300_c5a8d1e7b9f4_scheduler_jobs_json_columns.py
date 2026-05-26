"""task 00149-2: scheduler_jobs 테이블 BLOB → JSON + 컬럼화 + 상태 타임스탬프.

설계 근거
    사용자 원문 task 00149 가 진단한 두 가지 결함을 한 마이그레이션으로 해소한다.

    1. **운영자 SQL 가시성 0**: 기존 ``scheduler_jobs`` 는 APScheduler 의 기본
       ``SQLAlchemyJobStore`` 가 런타임에 자동 생성한 ``(id, next_run_time FLOAT,
       job_state LargeBinary[pickle])`` 3컬럼 테이블이다. cron 표현식과 잡 실행
       이력이 모두 pickle blob 에 묻혀 있어 ``sqlite3 select * from scheduler_jobs``
       로는 ``daily-report | 1779755400.0 | (bytes)`` 같은 식의 진단 불가 출력만
       나온다. 사용자가 09:50 시점에 09:30 누락 여부를 \"왜 안 갔는지\" 라이브로
       확인할 수 없었던 이유.
    2. **BLOB pickle 의존**: APScheduler 가 Job 객체를 통째로 pickle 해 직렬화
       하므로 Python 버전·라이브러리 버전 호환성 부담이 크고, 잡 메타데이터
       구조 진화가 불가능하다.

    본 마이그레이션은 다음 변경을 적용한다.

    - ``next_run_time``: ``FLOAT(25)`` → ``DateTime(timezone=True)``. 운영자가
      ISO 문자열로 즉시 읽을 수 있고, 추후 Postgres 이식 시 TIMESTAMPTZ 호환.
    - ``job_state``: ``LargeBinary`` → ``Text``. ``json.loads`` 로 파싱 가능한
      구조화 JSON. pickle 제거.
    - 신규 컬럼 9 개:
        * ``trigger_type VARCHAR(16) NOT NULL`` — ``cron`` / ``interval``.
        * ``cron_expression TEXT NULL`` — cron 잡의 5-필드 표현식.
        * ``interval_seconds INTEGER NULL`` — interval 잡의 초 단위.
        * ``created_at DateTime(timezone=True) NOT NULL`` — 잡 등록 시각.
        * ``updated_at DateTime(timezone=True) NOT NULL`` — 메타 갱신 시각.
        * ``last_run_at DateTime(timezone=True) NULL`` — listener 가 갱신.
        * ``last_success_at DateTime(timezone=True) NULL``.
        * ``last_fail_at DateTime(timezone=True) NULL``.
        * ``last_error_message TEXT NULL`` — 최대 1024자 (jobstore 측 truncate).
    - 신규 인덱스 ``ix_scheduler_jobs_trigger_type`` — trigger 종류 필터링 가속.
    - 기존 인덱스 ``ix_scheduler_jobs_next_run_time`` 은 새 컬럼 타입으로 재생성.

기존 데이터 마이그레이션
    APScheduler 의 ``SQLAlchemyJobStore`` 는 ``next_run_time`` 을 epoch float 으로,
    ``job_state`` 를 ``pickle.dumps(job.__getstate__())`` 로 저장한다. 본
    마이그레이션은 이 두 컬럼을 Python 측에서 unpickle 후 변환·재INSERT 한다.

    실패 row 처리: pickle 역직렬화가 실패한 경우 (라이브러리 버전 비호환 등),
    해당 row 는 새 테이블에 **이전하지 않고 WARNING 로그만 남긴다**. 운영자는
    admin 페이지에서 잡을 재등록해야 한다. 데이터 손실보다 일관된 새 스키마를
    유지하는 편이 운영 안전상 낫다 (실패 row 가 남아 있으면 새 jobstore 가
    재구성 실패로 매번 삭제 시도 → 로그 노이즈).

기존 row 영향
    잡 함수는 (``app.scheduler.job_runner.scheduled_scrape`` /
    ``scheduled_backup_job`` / ``scheduled_daily_report_job`` /
    ``gc_orphan_attachments_job``) 4 종 모두 module path 가 유지되므로
    ``ref_to_obj`` 가 성공적으로 동작해야 한다. 다만 잡 ``args`` 가
    primitive 가 아닌 경우(과거 잘못 등록된 잡)는 새 JSON jobstore 가 거부할
    수 있다 — 본 마이그레이션은 args/kwargs 가 JSON serializable 한지도 함께
    검증한다.

다운그레이드
    본 변경은 **단방향**이다. JSON / 컬럼화 / 타임스탬프 정보를 BLOB pickle 로
    복원할 수 없으므로 ``downgrade`` 는 \"신 스키마 → 빈 BLOB 스키마\" 로 잡
    데이터를 폐기한다 (downgrade docstring 에 명시 — 개발/검증 경로 전용).

SQLite ↔ Postgres 이식성 (docs/db_portability.md §1, §3, §4)
    - ``DateTime(timezone=True)`` — Postgres TIMESTAMPTZ / SQLite TEXT 호환.
    - 모든 컬럼·인덱스 이름 명시.
    - 기존 테이블 존재 / 부재 분기는 dialect-agnostic 한 ``Inspector`` API 사용.
    - 새 테이블 생성은 ``op.create_table``, 기존 row 변환은 raw connection 의
      ``execute(sa.text(...))`` 로 SQLAlchemy core 만 사용 (text() SQL 은
      두 dialect 가 호환되는 표현 only).

검증 절차 (docs/db_portability.md §4 3단계)
    1. 기존 운영 DB 사본에 신규 migration 적용 — pickle blob 변환 + 새 컬럼 채움.
    2. 빈 SQLite 에 alembic upgrade head — baseline 부터 head 까지 통과.
    3. Postgres syntax 호환 정적 검토 — DateTime(timezone=True), 이름 명시
       constraint, dialect 비의존 변환 SQL 확인.

Revision ID: c5a8d1e7b9f4
Revises: b3d9e1f7c264
Create Date: 2026-05-26 03:00:00.000000+00:00
"""
from __future__ import annotations

import json
import pickle
from datetime import UTC, datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "c5a8d1e7b9f4"
down_revision: Union[str, None] = "b3d9e1f7c264"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 본 마이그레이션이 다루는 테이블명. 운영 코드(app/scheduler/constants.py) 와
# 동일한 리터럴이어야 하지만 마이그레이션 파일은 Python 모듈 import 위주의
# 결합을 피하기 위해 상수를 inline 한다 (Alembic 마이그레이션 컨벤션).
_TABLE_NAME: str = "scheduler_jobs"
_OLD_INDEX_NEXT_RUN_TIME: str = "ix_scheduler_jobs_next_run_time"
_NEW_INDEX_TRIGGER_TYPE: str = "ix_scheduler_jobs_trigger_type"

# job.name 의 prefix 들. cron 표현식 복원에 사용한다 — 본 변환 로직이
# 잡 함수가 \"문자열로 표현 가능한 cron 표현식\" 을 안다고 가정한다.
# (app/scheduler/constants.py 와 동일한 리터럴 — 결합도 최소화를 위해 inline.)
_PREFIX_CRON: str = "cron:"
_PREFIX_BACKUP: str = "backup-cron:"
_PREFIX_DAILY_REPORT: str = "daily-report-cron:"
_PREFIX_GC_ORPHAN: str = "gc-orphan-cron:"
_PREFIX_INTERVAL: str = "interval:"
_CRON_PREFIXES: tuple[str, ...] = (
    _PREFIX_CRON,
    _PREFIX_BACKUP,
    _PREFIX_DAILY_REPORT,
    _PREFIX_GC_ORPHAN,
)


def _extract_cron_expression(job_name: object) -> Union[str, None]:
    """잡 이름의 prefix 를 벗겨 cron 표현식을 복원한다.

    Args:
        job_name: 잡 이름(보통 문자열). None 이면 None 반환.

    Returns:
        cron 표현식. prefix 가 매칭되지 않으면 None.
    """
    if not isinstance(job_name, str):
        return None
    for prefix in _CRON_PREFIXES:
        if job_name.startswith(prefix):
            return job_name[len(prefix):]
    return None


def _epoch_to_utc_datetime(value: object) -> Union[datetime, None]:
    """기존 ``next_run_time FLOAT`` 값을 UTC tz-aware datetime 으로 변환한다.

    Args:
        value: SELECT 결과 (float epoch 또는 None).

    Returns:
        UTC tz-aware datetime. 입력이 None 또는 float 가 아니면 None.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _convert_pickle_row(
    row_id: str,
    next_run_time_float: object,
    job_state_blob: object,
) -> Union[dict, None]:
    """기존 BLOB pickle row 1건을 새 스키마 컬럼 dict 로 변환한다.

    Args:
        row_id: 잡 ID (PK).
        next_run_time_float: 기존 epoch float 값.
        job_state_blob: 기존 pickle bytes (``bytes`` 또는 ``memoryview``).

    Returns:
        새 스키마 INSERT 에 쓸 dict. 변환 실패 시 None.
    """
    if isinstance(job_state_blob, memoryview):
        job_state_blob = bytes(job_state_blob)
    if not isinstance(job_state_blob, (bytes, bytearray)):
        return None

    try:
        state = pickle.loads(bytes(job_state_blob))
    except Exception as exc:  # pragma: nocover - 손상된 row 대비
        print(
            f"[WARN] scheduler_jobs 마이그레이션: id={row_id!r} pickle 역직렬화 실패: {exc}"
        )
        return None

    job_name = state.get("name")
    trigger = state.get("trigger")
    trigger_class_name = type(trigger).__name__ if trigger is not None else ""

    cron_expression = None
    interval_seconds = None
    if trigger_class_name == "CronTrigger":
        trigger_type = "cron"
        cron_expression = _extract_cron_expression(job_name)
        if cron_expression is None:
            # job.name 에 prefix:cron 패턴이 없는 경우 — 알 수 없는 잡 형태로
            # 보고 보존하지 않는다 (새 jobstore 는 cron_expression 이 NOT
            # NULL-ish 라 운영자가 재등록해야 함).
            print(
                f"[WARN] scheduler_jobs 마이그레이션: id={row_id!r} cron 잡인데 "
                f"job.name={job_name!r} 에서 cron 표현식 복원 불가 — row 폐기"
            )
            return None
    elif trigger_class_name == "IntervalTrigger":
        trigger_type = "interval"
        # IntervalTrigger.interval 은 timedelta. seconds 합으로 변환.
        interval = getattr(trigger, "interval", None)
        if interval is None:
            print(
                f"[WARN] scheduler_jobs 마이그레이션: id={row_id!r} interval 잡인데 "
                f"interval 속성이 없습니다 — row 폐기"
            )
            return None
        try:
            interval_seconds = int(interval.total_seconds())
        except Exception as exc:
            print(
                f"[WARN] scheduler_jobs 마이그레이션: id={row_id!r} interval 변환 실패: {exc}"
            )
            return None
    else:
        # date trigger 등 본 프로젝트가 등록하지 않는 종류 — 폐기.
        print(
            f"[WARN] scheduler_jobs 마이그레이션: id={row_id!r} 지원하지 않는 "
            f"trigger 종류 {trigger_class_name!r} — row 폐기"
        )
        return None

    func_ref = state.get("func")
    if not isinstance(func_ref, str) or not func_ref:
        print(
            f"[WARN] scheduler_jobs 마이그레이션: id={row_id!r} func_ref 가 비어있어 "
            f"폐기 (현장 코드에선 등록 못 함 — 손상된 row 추정)"
        )
        return None

    args = list(state.get("args", ()) or ())
    kwargs = dict(state.get("kwargs", {}) or {})

    # JSON serializable 검증 — 본 프로젝트의 잡 함수는 모두 list[str] 또는
    # 빈 args 만 쓰므로 정상 row 는 통과한다.
    try:
        json.dumps(args)
        json.dumps(kwargs)
    except TypeError as exc:
        print(
            f"[WARN] scheduler_jobs 마이그레이션: id={row_id!r} args/kwargs 가 "
            f"JSON-serializable 하지 않습니다 — row 폐기: {exc}"
        )
        return None

    job_state = {
        "version": 1,
        "func_ref": func_ref,
        "args": args,
        "kwargs": kwargs,
        "name": job_name,
        "misfire_grace_time": state.get("misfire_grace_time"),
        "coalesce": bool(state.get("coalesce", True)),
        "max_instances": int(state.get("max_instances", 1)),
        "executor": state.get("executor", "default"),
    }
    job_state_json = json.dumps(job_state, ensure_ascii=False)

    now = datetime.now(tz=UTC)
    return {
        "id": row_id,
        "next_run_time": _epoch_to_utc_datetime(next_run_time_float),
        "trigger_type": trigger_type,
        "cron_expression": cron_expression,
        "interval_seconds": interval_seconds,
        "job_state": job_state_json,
        "created_at": now,
        "updated_at": now,
        "last_run_at": None,
        "last_success_at": None,
        "last_fail_at": None,
        "last_error_message": None,
    }


def upgrade() -> None:
    """scheduler_jobs 테이블을 신 스키마로 교체한다.

    실행 순서:
        1. 기존 테이블이 있으면 BLOB pickle row 를 모두 SELECT → Python 측 변환.
        2. 기존 테이블 (있다면) DROP — 인덱스 자동 동반 삭제.
        3. 신 스키마로 ``scheduler_jobs`` CREATE TABLE + 인덱스 2종 추가.
        4. 변환에 성공한 row 들을 새 테이블에 BULK INSERT.

    1·2 단계가 같은 트랜잭션 안에서 일어나도록 op.execute / op.create_table 흐름
    을 유지한다 — alembic 의 ``with op.batch_alter_table`` 보다 \"DROP → CREATE
    → INSERT\" 가 본 변환의 복잡도(BLOB → 9개 신규 컬럼 + 데이터 변환)에 더
    적합하다.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── 1. 기존 데이터 수집 ──────────────────────────────────────────────────
    converted_rows: list[dict] = []
    table_existed = _TABLE_NAME in inspector.get_table_names()
    if table_existed:
        try:
            old_rows = list(
                bind.execute(
                    sa.text(
                        f"SELECT id, next_run_time, job_state FROM {_TABLE_NAME}"
                    )
                )
            )
        except Exception as exc:  # pragma: nocover
            # 기존 테이블 스키마가 예상과 다르면(예: 손상) 전부 폐기한다.
            print(
                f"[WARN] scheduler_jobs 마이그레이션: 기존 데이터 SELECT 실패 — "
                f"기존 row 를 모두 폐기하고 신 스키마로 진행합니다: {exc}"
            )
            old_rows = []

        for row in old_rows:
            converted = _convert_pickle_row(row.id, row.next_run_time, row.job_state)
            if converted is not None:
                converted_rows.append(converted)

        # ── 2. 기존 테이블 DROP ──────────────────────────────────────────────
        # 인덱스도 함께 삭제됨. 부수효과 없음 (APScheduler 가 자동 재생성하던
        # 테이블이라 외래키도 없다).
        op.drop_table(_TABLE_NAME)

    # ── 3. 신 스키마 CREATE TABLE + 인덱스 ──────────────────────────────────
    op.create_table(
        _TABLE_NAME,
        # APScheduler 의 max id 길이 191 을 그대로 사용한다.
        sa.Column("id", sa.String(191), primary_key=True),
        # next_run_time 은 paused 잡이면 NULL. UTC tz-aware 저장.
        sa.Column("next_run_time", sa.DateTime(timezone=True), nullable=True),
        # trigger 종류 — 'cron' / 'interval'. 운영자가 sqlite3 에서 필터링.
        sa.Column("trigger_type", sa.String(16), nullable=False),
        # cron 표현식 — interval 잡이면 NULL.
        sa.Column("cron_expression", sa.Text, nullable=True),
        # interval 초 — cron 잡이면 NULL.
        sa.Column("interval_seconds", sa.Integer, nullable=True),
        # 잔여 메타데이터 JSON. pickle 미사용 — json.loads 로 파싱 가능.
        sa.Column("job_state", sa.Text, nullable=False),
        # 잡 등록 시각 (UTC tz-aware). application 측이 INSERT 시 채운다.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # 메타 갱신 시각.
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # 마지막 실행 시작 시각 — 잡 실행 listener 가 갱신.
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        # 마지막 성공 종료 시각.
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        # 마지막 실패 종료 시각.
        sa.Column("last_fail_at", sa.DateTime(timezone=True), nullable=True),
        # 마지막 실패의 에러 요약 (최대 1024자 — jobstore 측 truncate).
        sa.Column("last_error_message", sa.Text, nullable=True),
    )

    # 기존과 동일 이름의 next_run_time 인덱스 + 신규 trigger_type 인덱스.
    # 인덱스 이름 명시 — Postgres / SQLite 양쪽에서 동일 이름으로 관리되도록.
    op.create_index(
        _OLD_INDEX_NEXT_RUN_TIME,
        _TABLE_NAME,
        ["next_run_time"],
    )
    op.create_index(
        _NEW_INDEX_TRIGGER_TYPE,
        _TABLE_NAME,
        ["trigger_type"],
    )

    # ── 4. 변환된 row INSERT ────────────────────────────────────────────────
    if converted_rows:
        # SQLAlchemy 의 Table 메타데이터를 inline 으로 재구성해 bulk insert.
        # op.bulk_insert 는 alembic 의 표준 API 이고 dialect 비의존이다.
        scheduler_jobs_t = sa.Table(
            _TABLE_NAME,
            sa.MetaData(),
            sa.Column("id", sa.String(191)),
            sa.Column("next_run_time", sa.DateTime(timezone=True)),
            sa.Column("trigger_type", sa.String(16)),
            sa.Column("cron_expression", sa.Text),
            sa.Column("interval_seconds", sa.Integer),
            sa.Column("job_state", sa.Text),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("last_run_at", sa.DateTime(timezone=True)),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.Column("last_fail_at", sa.DateTime(timezone=True)),
            sa.Column("last_error_message", sa.Text),
        )
        op.bulk_insert(scheduler_jobs_t, converted_rows)
        print(
            f"[INFO] scheduler_jobs 마이그레이션: {len(converted_rows)} 건의 "
            f"기존 잡을 신 스키마로 이전했습니다."
        )
    elif table_existed:
        print(
            "[INFO] scheduler_jobs 마이그레이션: 기존 테이블은 있었지만 변환 "
            "성공한 row 가 없습니다 (모두 폐기). 운영자는 admin 페이지에서 잡을 "
            "재등록해야 합니다."
        )


def downgrade() -> None:
    """신 스키마를 옛 BLOB 스키마로 되돌린다 (데이터 폐기).

    JSON / 컬럼화 / 상태 타임스탬프 정보를 BLOB pickle 로 복원할 수 없으므로
    \"신 스키마 → 빈 BLOB 스키마\" 가 본 downgrade 의 의미다. 잡 데이터는
    유실되며, 운영자가 admin 페이지에서 재등록해야 한다 — downgrade 는
    개발/검증 경로 전용이다.
    """
    op.drop_index(_NEW_INDEX_TRIGGER_TYPE, table_name=_TABLE_NAME)
    op.drop_index(_OLD_INDEX_NEXT_RUN_TIME, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

    # APScheduler 의 SQLAlchemyJobStore 가 자동 생성하던 옛 스키마 그대로 재현.
    op.create_table(
        _TABLE_NAME,
        sa.Column("id", sa.Unicode(191), primary_key=True),
        sa.Column("next_run_time", sa.Float(25)),
        sa.Column("job_state", sa.LargeBinary, nullable=False),
    )
    op.create_index(_OLD_INDEX_NEXT_RUN_TIME, _TABLE_NAME, ["next_run_time"])
