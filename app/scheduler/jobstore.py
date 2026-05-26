"""구조화 JSON 기반 APScheduler JobStore (task 00149-2).

배경 (사용자 원문 task 00149):
    기본 ``SQLAlchemyJobStore`` 는 잡 정보를 ``(id, next_run_time FLOAT, job_state
    LargeBinary[pickle])`` 3컬럼으로만 저장한다. 그 결과 운영자가 ``sqlite3
    scheduler_jobs`` 를 직접 조회해도:

    - cron 표현식이 무엇인지 알 수 없다 (pickle blob 안에 묶여 있음).
    - 잡이 마지막으로 언제 성공/실패했는지, 어떤 에러였는지 알 수 없다.
    - ``next_run_time`` 이 float epoch 라 시각화하려면 외부 변환이 필요하다.

    또한 ``job_state`` 가 pickle 이라 Python 의존성·버전 호환성 문제가 누적되고
    구조 가시성이 0 이다. 본 task 의 두 번째 핵심 요구는 \"BLOB → 구조화 JSON\"
    + \"cron 표현식 / 상태 타임스탬프 컬럼화\" 다.

본 모듈의 스토어 (``JsonSchedulerJobStore``) 는 위 결함을 한꺼번에 해소한다.

    1. trigger 종류·cron 표현식·interval 초 를 별도 컬럼으로 추출
       (``trigger_type``, ``cron_expression``, ``interval_seconds``). sqlite3
       1줄 SELECT 로 운영자가 잡의 발사 조건을 즉시 읽는다.
    2. ``next_run_time`` 을 ``DateTime(timezone=True)`` 으로 저장 — 사람이 읽을
       수 있는 ISO 문자열로 표시된다.
    3. ``created_at`` / ``updated_at`` / ``last_run_at`` / ``last_success_at`` /
       ``last_fail_at`` / ``last_error_message`` 6개 상태 컬럼을 추가 —
       이벤트 리스너(``app/scheduler/service.py`` 에 등록)가 잡 실행 결과를
       기록해 운영 가시성을 극대화한다.
    4. ``job_state`` 는 TEXT JSON 으로 저장 — 컬럼화된 항목(trigger / id /
       next_run_time)을 제거한 \"잔여 메타데이터만\" 직렬화한다. ``json.loads()``
       만으로 파싱 가능하다 (pickle 의존 제거).

APScheduler 3.x ``BaseJobStore`` 계약 (``apscheduler/jobstores/base.py``) 의 9개
메서드(``start`` / ``lookup_job`` / ``get_due_jobs`` / ``get_next_run_time`` /
``get_all_jobs`` / ``add_job`` / ``update_job`` / ``remove_job`` /
``remove_all_jobs``) 를 모두 구현한다. ``shutdown`` 은 BaseJobStore 의 기본
no-op 을 그대로 사용한다(엔진 dispose 는 ``app.db.session`` 에 위임).

Job 재구성 정책:
    - ``CronTrigger`` 는 ``cron_expression`` 컬럼을 ``build_cron_trigger`` 로
      재파싱해 KST timezone 부착으로 만든다. CronTrigger 객체에서 표현식을
      \"역추출\" 하는 안정적인 방법이 표준에 없어, 컬럼 값을 source of truth
      로 둔다.
    - ``IntervalTrigger`` 는 ``interval_seconds`` 컬럼으로 재생성 (KST timezone).
    - 그 외 trigger 종류(``DateTrigger`` 등) 는 본 프로젝트가 등록하지 않으므로
      지원하지 않는다. add_job 단계에서 ``TypeError`` 로 거부한다.

JSON serializable 검증:
    이 프로젝트의 잡 함수 인자는 (a) 빈 리스트 ``args=[]`` 또는 (b) ``args=
    [list[str]]`` (scheduled_scrape) 만이다. ``add_job`` 진입 시 ``json.dumps``
    로 args/kwargs 가 JSON-serializable 한지 검증하고, 비-primitive 가 들어오면
    ``TypeError`` 로 즉시 거부한다 — pickle 호환성 가정에 의존하지 않는다.

SQLite 단일 writer 충돌 회피 (PROJECT_NOTES L240):
    ``SQLAlchemyJobStore`` 와 동일하게 자체 ``engine.begin()`` 컨텍스트로 별도
    connection 을 사용한다. ORM ``session_scope`` 는 본 클래스 내부에서 절대
    호출하지 않는다 — write 트랜잭션이 겹치면 ``database is locked`` 가 난다.

DDL 관리:
    ``scheduler_jobs`` 테이블의 생성/스키마 변경은 Alembic 마이그레이션
    (``20260526_*_scheduler_jobs_json_columns.py``) 이 전담한다. 본 클래스의
    ``start()`` 는 ``CREATE TABLE IF NOT EXISTS`` 를 호출하지 **않는다** —
    Alembic 이 baseline-bootstrap / stamp-then-upgrade 경로로 보장한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.expression import null

from apscheduler.job import Job
from apscheduler.jobstores.base import (
    BaseJobStore,
    ConflictingIdError,
    JobLookupError,
)
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.scheduler.constants import (
    JOB_NAME_BACKUP_PREFIX,
    JOB_NAME_CRON_PREFIX,
    JOB_NAME_DAILY_REPORT_PREFIX,
    JOB_NAME_GC_ORPHAN_PREFIX,
    JOB_NAME_INTERVAL_PREFIX,
    SCHEDULER_JOBS_TABLENAME,
)
from app.scheduler.cron import build_cron_trigger
from app.timezone import KST, now_utc

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# trigger_type 컬럼에 저장되는 값. 운영자가 sqlite3 에서 직접 필터링할 때 쓰는
# 사람-친화 토큰. 본 프로젝트는 cron / interval 두 종류만 등록한다.
TRIGGER_TYPE_CRON: str = "cron"
TRIGGER_TYPE_INTERVAL: str = "interval"

# job.name 의 prefix → cron 표현식 추출. ``_recompute_all_jobs_next_run_time``
# 의 분기와 동일 순서로 본 jobstore 가 직접 파싱한다 (service.py 와의 순환
# import 방지).
_CRON_NAME_PREFIXES: tuple[str, ...] = (
    JOB_NAME_CRON_PREFIX,
    JOB_NAME_BACKUP_PREFIX,
    JOB_NAME_DAILY_REPORT_PREFIX,
    JOB_NAME_GC_ORPHAN_PREFIX,
)


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _extract_cron_expression_from_name(job_name: Any) -> Optional[str]:
    """``Job.name`` 의 prefix 를 벗겨 cron 표현식을 복원한다.

    본 프로젝트는 cron 잡 등록 시 ``Job.name`` 에 ``"<prefix>:<cron>"`` 패턴으로
    표현식을 박아 둔다 (예: ``cron:0 3 * * *``, ``backup-cron:0 3 * * *``,
    ``daily-report-cron:0 9 * * 1-5``, ``gc-orphan-cron:0 4 * * *``). 본 함수가
    이 prefix 들을 순서대로 검사해 cron 표현식만 반환한다.

    Args:
        job_name: ``Job.name`` 값. 일반적으로 문자열이지만 안전을 위해 Any 로 받는다.

    Returns:
        cron 표현식 문자열. prefix 가 매칭되지 않으면 None — 호출 측은 cron
        잡임에도 표현식 복원에 실패한 케이스로 처리해야 한다(보통은 발생하지
        않는 경로지만, 외부 도구로 잡 name 을 임의 변경했을 때 대비).
    """
    if not isinstance(job_name, str):
        return None
    for prefix in _CRON_NAME_PREFIXES:
        if job_name.startswith(prefix):
            return job_name[len(prefix):]
    return None


def _extract_interval_spec_from_name(job_name: Any) -> Optional[str]:
    """``Job.name`` 의 prefix 를 벗겨 interval 표현(예: ``매 6시간``)을 복원한다.

    interval 잡은 ``IntervalTrigger.interval`` 자체에서 정확한 초 단위 값을
    복원할 수 있으므로 본 함수의 반환값은 \"표시용 spec 보존\" 목적이며,
    재구성 자체에는 사용하지 않는다. 매칭 실패 시 None.
    """
    if not isinstance(job_name, str):
        return None
    if job_name.startswith(JOB_NAME_INTERVAL_PREFIX):
        return job_name[len(JOB_NAME_INTERVAL_PREFIX):]
    return None


def _assert_jsonable(value: Any, *, name: str) -> None:
    """주어진 값이 JSON-serializable 한지 검증한다.

    실패 시 ``TypeError`` 를 발생시킨다. 본 jobstore 는 pickle 을 사용하지 않으
    므로 add_job 단계에서 args/kwargs 가 primitive(list/dict/str/int/float/
    bool/None) 의 조합인지 미리 확인해야 한다. 위반하면 update_job / get_all_jobs
    경로에서 알 수 없는 직렬화 오류로 깨지므로, 등록 시 즉시 거부하는 편이
    낫다.

    Args:
        value: 검증할 값 (보통 args 또는 kwargs).
        name:  에러 메시지에 노출할 이름 (디버깅 편의).
    """
    try:
        json.dumps(value)
    except TypeError as exc:
        raise TypeError(
            f"JsonSchedulerJobStore: {name} 가 JSON-serializable 하지 않습니다 "
            f"({type(value).__name__}). 본 jobstore 는 pickle 을 쓰지 않으므로 "
            f"primitive(list/dict/str/int/float/bool/None) 만 허용합니다. "
            f"원인: {exc}"
        ) from exc


def _normalize_datetime_to_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite 가 SELECT 로 돌려준 naive datetime 을 UTC tz-aware 로 정규화한다.

    SQLite 는 ``DateTime(timezone=True)`` 컬럼이라도 TEXT 로 저장되며 SELECT 시
    tzinfo 가 떨어진 naive 값을 돌려준다 (``app/db/models.py:as_utc`` 패턴과
    동일). 본 jobstore 가 Job 으로 재구성할 때 APScheduler 의 비교/계산은
    tz-aware 를 전제하므로, 읽기 직후 일관되게 UTC 부착으로 처리한다.

    Postgres 의 ``TIMESTAMPTZ`` 컬럼은 이미 tz-aware 로 돌아오므로 본 함수는
    no-op 으로 통과시킨다.

    Args:
        value: SELECT 로 받은 datetime 또는 None.

    Returns:
        UTC tz-aware datetime. 입력이 None 이면 None.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        # 컨벤션: \"저장값은 UTC\" — naive 는 UTC 가정.
        from datetime import UTC
        return value.replace(tzinfo=UTC)
    return value


def _to_storage_utc(value: Optional[datetime]) -> Optional[datetime]:
    """``DateTime(timezone=True)`` 컬럼에 INSERT 할 datetime 을 UTC 로 정규화한다.

    배경: SQLite 의 ``DateTime(timezone=True)`` 컬럼은 ISO 문자열에 timezone
    offset 을 함께 저장하지만, SELECT 결과는 tz-aware 가 아닌 \"offset 부착이
    벗겨진 naive\" 로 돌아온다(``app/db/models.py:as_utc`` 의 docstring 와
    동일 한계). 그래서 KST(+09:00) tz-aware 값을 저장하면 SELECT 시 KST 시각의
    숫자가 그대로 naive 로 돌아오고, ``_normalize_datetime_to_utc`` 가 \"naive
    는 UTC\" 가정으로 tzinfo=UTC 를 부착해 9시간 어긋난 값이 된다.

    해결: 저장 단계에서 UTC 로 정규화해 두면, 어떤 backend 에서도 SELECT 결과
    가 UTC 가 된다 (PROJECT_NOTES \"DB 저장은 UTC\" 컨벤션과 일치).

    Args:
        value: 저장할 datetime (tz-aware 또는 naive). None 통과.

    Returns:
        UTC tz-aware datetime. naive 입력은 UTC 가정으로 attach 후 통과.
    """
    if value is None:
        return None
    from datetime import UTC

    if value.tzinfo is None:
        # naive 는 컨벤션상 UTC 가정 — 단위 테스트가 modify_job(naive 시각) 으로
        # stale 박는 경로가 있다.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ──────────────────────────────────────────────────────────────
# JsonSchedulerJobStore
# ──────────────────────────────────────────────────────────────


class JsonSchedulerJobStore(BaseJobStore):
    """구조화 JSON / 컬럼화 메타 / 상태 타임스탬프를 갖는 APScheduler JobStore.

    APScheduler 3.x 의 ``BaseJobStore`` 9 메서드를 모두 구현한다. 테이블 스키마와
    인덱스는 Alembic 마이그레이션(``20260526_*_scheduler_jobs_json_columns``) 이
    관리하므로 본 클래스는 ``CREATE TABLE`` 을 호출하지 않는다 — ``init_db``
    가 baseline-bootstrap / stamp-then-upgrade 경로로 보장한다.

    Attributes:
        engine: ``app.db.session.get_engine()`` 가 반환한 동일 엔진. SQLite 의
            경우 단일 writer 제약을 우회하기 위해 본 클래스는 자체 ``begin()``
            컨텍스트만 사용하고 ORM 세션은 호출하지 않는다.
        tablename: 잡 저장 테이블명. 기본 ``SCHEDULER_JOBS_TABLENAME``.
        jobs_t: SQLAlchemy ``Table`` 정의 — 마이그레이션이 만든 실제 컬럼과
            정확히 일치해야 한다. SELECT/INSERT/UPDATE/DELETE SQL 빌더가
            이 정의를 통해 발급된다.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        tablename: str = SCHEDULER_JOBS_TABLENAME,
    ) -> None:
        """JobStore 인스턴스를 만든다.

        Args:
            engine: 모든 SQL 을 발행할 SQLAlchemy 엔진. ``get_engine()`` 의
                싱글턴을 재사용해 동일 SQLite 파일에 접근한다.
            tablename: 잡 저장 테이블명. 운영 코드에서는 기본값 사용.
        """
        super().__init__()
        self.engine = engine
        self.tablename = tablename

        # MetaData 는 본 모듈 안에서만 쓰는 isolated container.
        # ORM 의 ``app.db.models.Base.metadata`` 와 충돌하지 않도록 분리한다.
        self._metadata = MetaData()
        self.jobs_t = Table(
            tablename,
            self._metadata,
            # APScheduler 3.x 의 max id 길이(SQLAlchemyJobStore 도 191).
            Column("id", String(191), primary_key=True),
            # next_run_time 은 tz-aware UTC. paused 잡은 NULL.
            Column("next_run_time", DateTime(timezone=True), nullable=True, index=True),
            # 운영자가 sqlite3 에서 직접 trigger 종류로 필터링하기 위한 컬럼.
            Column("trigger_type", String(16), nullable=False),
            # cron 표현식 — interval/date 잡이면 NULL.
            Column("cron_expression", Text, nullable=True),
            # interval 초 — cron/date 잡이면 NULL.
            Column("interval_seconds", Integer, nullable=True),
            # 잔여 메타데이터를 JSON 으로 직렬화 (pickle 미사용).
            Column("job_state", Text, nullable=False),
            # 잡 최초 등록 시각.
            Column("created_at", DateTime(timezone=True), nullable=False),
            # trigger / 메타 갱신 시각.
            Column("updated_at", DateTime(timezone=True), nullable=False),
            # 마지막 실행 시작 시각 — listener 가 갱신.
            Column("last_run_at", DateTime(timezone=True), nullable=True),
            # 마지막 성공 종료 시각.
            Column("last_success_at", DateTime(timezone=True), nullable=True),
            # 마지막 실패 종료 시각.
            Column("last_fail_at", DateTime(timezone=True), nullable=True),
            # 마지막 실패의 에러 요약 (최대 1024자 truncate — listener 가 처리).
            Column("last_error_message", Text, nullable=True),
        )

    # ──────────────────────────────────────────────
    # BaseJobStore 9 메서드 구현
    # ──────────────────────────────────────────────

    def start(self, scheduler: Any, alias: str) -> None:
        """스케줄러가 본 jobstore 를 마운트할 때 호출된다.

        ``BaseJobStore.start`` 가 ``self._scheduler`` 와 ``self._alias`` 를 설정한다.
        ``SQLAlchemyJobStore`` 와 달리 본 클래스는 ``self.jobs_t.create(engine)``
        을 호출하지 **않는다** — 테이블/인덱스는 Alembic 마이그레이션이 관리한다.
        ``init_db()`` 의 baseline-bootstrap / stamp-then-upgrade 가 미리 보장한
        상태에서 본 메서드가 호출되어야 한다.
        """
        super().start(scheduler, alias)

    def lookup_job(self, job_id: str) -> Optional[Job]:
        """``job_id`` 로 잡 1건을 조회해 ``Job`` 인스턴스로 복원한다.

        Args:
            job_id: 조회할 잡의 ID.

        Returns:
            복원된 ``Job`` 또는 ``None`` (해당 ID 가 없거나 재구성에 실패).
        """
        selectable = select(
            self.jobs_t.c.id,
            self.jobs_t.c.next_run_time,
            self.jobs_t.c.trigger_type,
            self.jobs_t.c.cron_expression,
            self.jobs_t.c.interval_seconds,
            self.jobs_t.c.job_state,
        ).where(self.jobs_t.c.id == job_id)
        with self.engine.begin() as connection:
            row = connection.execute(selectable).first()
            if row is None:
                return None
            try:
                return self._reconstitute_job(row)
            except Exception as exc:
                # 손상된 row 는 lookup 단계에서 None 으로 떨어뜨린다. get_all_jobs
                # 에서 동일 row 가 재시도 시 정리되도록 둔다 (logger 만 남김).
                self._logger.warning(
                    "잡 lookup 실패 (재구성 오류): id=%s err=%s", job_id, exc,
                )
                return None

    def get_due_jobs(self, now: datetime) -> list[Job]:
        """``now`` 시각까지 fire 예정이었던 잡 목록을 반환한다.

        Args:
            now: 비교 기준 시각 (tz-aware).

        Returns:
            ``next_run_time <= now`` 인 잡들 (next_run_time 오름차순).
        """
        return self._get_jobs(self.jobs_t.c.next_run_time <= now)

    def get_next_run_time(self) -> Optional[datetime]:
        """모든 잡 중 가장 빠른 ``next_run_time`` 을 반환한다.

        paused 잡(next_run_time IS NULL) 은 제외하며, 등록 잡이 0건이면 None.
        """
        selectable = (
            select(self.jobs_t.c.next_run_time)
            .where(self.jobs_t.c.next_run_time != null())
            .order_by(self.jobs_t.c.next_run_time)
            .limit(1)
        )
        with self.engine.begin() as connection:
            value = connection.execute(selectable).scalar()
            return _normalize_datetime_to_utc(value)

    def get_all_jobs(self) -> list[Job]:
        """모든 잡을 next_run_time 오름차순(paused 는 뒤로)으로 반환한다."""
        jobs = self._get_jobs()
        self._fix_paused_jobs_sorting(jobs)
        return jobs

    def add_job(self, job: Job) -> None:
        """신규 잡을 jobstore 에 INSERT 한다.

        동일 ID 가 이미 존재하면 ``ConflictingIdError`` 를 던진다 — APScheduler
        가 이 예외를 가로채 ``replace_existing=True`` 분기에서 ``update_job``
        으로 대체한다.

        Args:
            job: 등록할 ``Job`` 인스턴스.

        Raises:
            ConflictingIdError: 동일 ID 의 잡이 이미 존재.
            TypeError: args/kwargs 가 JSON-serializable 하지 않음.
        """
        serialized = self._serialize_job(job)
        now = now_utc()
        insert = self.jobs_t.insert().values(
            id=job.id,
            next_run_time=_to_storage_utc(job.next_run_time),
            trigger_type=serialized["trigger_type"],
            cron_expression=serialized["cron_expression"],
            interval_seconds=serialized["interval_seconds"],
            job_state=serialized["job_state_json"],
            created_at=now,
            updated_at=now,
            last_run_at=None,
            last_success_at=None,
            last_fail_at=None,
            last_error_message=None,
        )
        with self.engine.begin() as connection:
            try:
                connection.execute(insert)
            except IntegrityError as exc:
                raise ConflictingIdError(job.id) from exc

    def update_job(self, job: Job) -> None:
        """기존 잡을 UPDATE 한다 (next_run_time / trigger / 메타 갱신).

        존재하지 않는 잡을 갱신하면 ``JobLookupError`` 를 던진다 — APScheduler
        가 잡 동기화 실패를 인지해 다음 주기로 넘어가게 한다.

        Args:
            job: 갱신할 ``Job`` 인스턴스.

        Raises:
            JobLookupError: 해당 ID 의 잡이 존재하지 않음.
            TypeError: args/kwargs 가 JSON-serializable 하지 않음.
        """
        serialized = self._serialize_job(job)
        update = (
            self.jobs_t.update()
            .values(
                next_run_time=_to_storage_utc(job.next_run_time),
                trigger_type=serialized["trigger_type"],
                cron_expression=serialized["cron_expression"],
                interval_seconds=serialized["interval_seconds"],
                job_state=serialized["job_state_json"],
                updated_at=now_utc(),
            )
            .where(self.jobs_t.c.id == job.id)
        )
        with self.engine.begin() as connection:
            result = connection.execute(update)
            if result.rowcount == 0:
                raise JobLookupError(job.id)

    def remove_job(self, job_id: str) -> None:
        """``job_id`` 로 잡 1건을 삭제한다.

        Args:
            job_id: 삭제할 잡의 ID.

        Raises:
            JobLookupError: 해당 ID 의 잡이 존재하지 않음.
        """
        delete = self.jobs_t.delete().where(self.jobs_t.c.id == job_id)
        with self.engine.begin() as connection:
            result = connection.execute(delete)
            if result.rowcount == 0:
                raise JobLookupError(job_id)

    def remove_all_jobs(self) -> None:
        """모든 잡을 삭제한다 (테스트/관리자 reset 경로용)."""
        delete = self.jobs_t.delete()
        with self.engine.begin() as connection:
            connection.execute(delete)

    # ──────────────────────────────────────────────
    # 직렬화 / 역직렬화
    # ──────────────────────────────────────────────

    def _serialize_job(self, job: Job) -> dict[str, Any]:
        """``Job`` 인스턴스를 INSERT/UPDATE 컬럼 dict 로 직렬화한다.

        반환 dict 의 키:
            - ``trigger_type``: ``"cron"`` 또는 ``"interval"``.
            - ``cron_expression``: cron 잡의 표현식. interval 이면 None.
            - ``interval_seconds``: interval 잡의 초. cron 이면 None.
            - ``job_state_json``: 잔여 메타데이터를 직렬화한 JSON 문자열.

        Args:
            job: 직렬화 대상 ``Job``.

        Raises:
            TypeError: trigger 가 cron/interval 외 종류이거나 args/kwargs 가
                JSON-serializable 하지 않음.
        """
        trigger = job.trigger
        if isinstance(trigger, CronTrigger):
            trigger_type = TRIGGER_TYPE_CRON
            # cron 표현식은 Job.name prefix 에서 복원 — CronTrigger 객체 자체에서
            # \"표현식 문자열\" 을 역추출하는 안정적 표준 API 가 없다 (fields 로
            # 부분 복원은 가능하나 step / 콤마 리스트 / 와일드카드 조합을 정확히
            # 되돌리기 어렵다). 본 프로젝트의 모든 cron 잡 등록 함수는 name 에
            # ``<prefix>:<cron>`` 패턴을 박아 두므로 그 패턴이 source of truth.
            cron_expression = _extract_cron_expression_from_name(job.name)
            interval_seconds: Optional[int] = None
        elif isinstance(trigger, IntervalTrigger):
            trigger_type = TRIGGER_TYPE_INTERVAL
            cron_expression = None
            interval_seconds = int(trigger.interval.total_seconds())
        else:
            raise TypeError(
                f"JsonSchedulerJobStore: 지원하지 않는 trigger 종류입니다: "
                f"{type(trigger).__name__}. 본 프로젝트는 cron / interval 잡만 "
                f"등록합니다."
            )

        # args/kwargs JSON serializable 검증. pickle 미사용 컨벤션의 핵심 가드.
        _assert_jsonable(list(job.args), name=f"job(id={job.id}).args")
        _assert_jsonable(dict(job.kwargs), name=f"job(id={job.id}).kwargs")

        # func_ref 는 APScheduler 의 ``obj_to_ref`` 가 만든 ``module:qualname``
        # 문자열. add_job 시점에는 이미 Job._modify 가 func_ref 를 채워둔다.
        if not job.func_ref:
            raise TypeError(
                f"JsonSchedulerJobStore: job(id={job.id}) 의 func_ref 가 비어 "
                f"있습니다. 클로저/람다는 직렬화할 수 없습니다 — top-level "
                f"함수로 등록하세요."
            )

        # 잔여 메타데이터만 JSON 으로. trigger / id / next_run_time 은 별도
        # 컬럼이라 JSON 에 중복 저장하지 않는다.
        job_state = {
            "version": 1,
            "func_ref": job.func_ref,
            "args": list(job.args),
            "kwargs": dict(job.kwargs),
            "name": job.name,
            "misfire_grace_time": job.misfire_grace_time,
            "coalesce": bool(job.coalesce),
            "max_instances": int(job.max_instances),
            "executor": job.executor,
        }
        job_state_json = json.dumps(job_state, ensure_ascii=False)

        return {
            "trigger_type": trigger_type,
            "cron_expression": cron_expression,
            "interval_seconds": interval_seconds,
            "job_state_json": job_state_json,
        }

    def _reconstitute_job(self, row: Any) -> Job:
        """SELECT row 1건을 ``Job`` 인스턴스로 복원한다.

        Args:
            row: SELECT 결과 row. ``id`` / ``next_run_time`` / ``trigger_type`` /
                ``cron_expression`` / ``interval_seconds`` / ``job_state`` 컬럼을
                포함해야 한다.

        Returns:
            복원된 ``Job`` 인스턴스. ``_scheduler`` / ``_jobstore_alias`` 도 함께
            세팅한다 (APScheduler 의 add_job 시점과 동일 상태).

        Raises:
            ValueError: job_state JSON 파싱 실패 / 필수 필드 누락 / trigger 종류
                미지원.
        """
        state = json.loads(row.job_state)
        if state.get("version", 1) != 1:
            raise ValueError(
                f"JsonSchedulerJobStore: job_state version={state.get('version')} "
                f"은 지원하지 않습니다 (version=1만 호환)."
            )

        # trigger 재구성 — cron 은 cron_expression 으로, interval 은 초 단위로.
        if row.trigger_type == TRIGGER_TYPE_CRON:
            if not row.cron_expression:
                raise ValueError(
                    f"JsonSchedulerJobStore: cron 잡인데 cron_expression 이 "
                    f"비어 있습니다 (id={row.id}). 운영자가 admin 페이지에서 "
                    f"재등록해야 합니다."
                )
            trigger = build_cron_trigger(row.cron_expression, timezone=KST)
        elif row.trigger_type == TRIGGER_TYPE_INTERVAL:
            if row.interval_seconds is None or row.interval_seconds <= 0:
                raise ValueError(
                    f"JsonSchedulerJobStore: interval 잡인데 interval_seconds 가 "
                    f"잘못됐습니다 (id={row.id} seconds={row.interval_seconds})."
                )
            trigger = IntervalTrigger(seconds=int(row.interval_seconds), timezone=KST)
        else:
            raise ValueError(
                f"JsonSchedulerJobStore: 지원하지 않는 trigger_type={row.trigger_type!r} "
                f"(id={row.id})."
            )

        # Job 인스턴스 생성 — SQLAlchemyJobStore 와 동일하게 __new__ + __setstate__
        # 패턴. __init__ 은 func 의 실시간 callable 검증을 요구해 lookup 경로에
        # 부적절하다.
        job_state_for_setstate = {
            "version": 1,
            "id": row.id,
            # func 키에는 ref 문자열을 둔다 — Job.__setstate__ 가 ref_to_obj 로
            # callable 을 복원한다.
            "func": state["func_ref"],
            "trigger": trigger,
            "executor": state.get("executor", "default"),
            "args": tuple(state.get("args", ())),
            "kwargs": dict(state.get("kwargs", {})),
            "name": state.get("name"),
            "misfire_grace_time": state.get("misfire_grace_time"),
            "coalesce": bool(state.get("coalesce", True)),
            "max_instances": int(state.get("max_instances", 1)),
            "next_run_time": _normalize_datetime_to_utc(row.next_run_time),
        }

        job = Job.__new__(Job)
        job.__setstate__(job_state_for_setstate)
        job._scheduler = self._scheduler
        job._jobstore_alias = self._alias
        return job

    def _get_jobs(self, *conditions: Any) -> list[Job]:
        """conditions 에 매칭되는 잡들을 next_run_time 오름차순으로 반환한다.

        ``_reconstitute_job`` 가 실패하는 row 는 jobstore 일관성을 위해 삭제하고
        WARN 로그만 남긴다 (``SQLAlchemyJobStore`` 와 동일 정책).

        Args:
            *conditions: 추가 WHERE 절. 비어 있으면 전체 조회.

        Returns:
            복원된 ``Job`` 리스트.
        """
        selectable = select(
            self.jobs_t.c.id,
            self.jobs_t.c.next_run_time,
            self.jobs_t.c.trigger_type,
            self.jobs_t.c.cron_expression,
            self.jobs_t.c.interval_seconds,
            self.jobs_t.c.job_state,
        ).order_by(self.jobs_t.c.next_run_time)
        if conditions:
            selectable = selectable.where(and_(*conditions))

        jobs: list[Job] = []
        failed_job_ids: set[str] = set()
        with self.engine.begin() as connection:
            for row in connection.execute(selectable):
                try:
                    jobs.append(self._reconstitute_job(row))
                except Exception:
                    self._logger.exception(
                        "잡 재구성 실패 — 잡스토어에서 제거합니다: id=%s", row.id,
                    )
                    failed_job_ids.add(row.id)
            if failed_job_ids:
                delete = self.jobs_t.delete().where(
                    self.jobs_t.c.id.in_(failed_job_ids)
                )
                connection.execute(delete)
        return jobs

    # ──────────────────────────────────────────────
    # 상태 컬럼 갱신 (listener 가 사용)
    # ──────────────────────────────────────────────

    def record_job_run_start(self, job_id: str, *, when: Optional[datetime] = None) -> None:
        """잡 실행 시작을 ``last_run_at`` 컬럼에 기록한다.

        listener 가 EVENT_JOB_EXECUTED / EVENT_JOB_ERROR / EVENT_JOB_MISSED 직전에
        호출하면 \"마지막 실행 시작 시각\" 으로 사용된다. 본 시그니처는 listener
        가 이벤트 코드와 무관하게 한 번에 갱신할 수 있도록 설계됐다.

        Args:
            job_id: 갱신할 잡의 ID.
            when: 기록할 시각 (tz-aware). 기본은 ``now_utc()``.
        """
        timestamp = when or now_utc()
        update = (
            self.jobs_t.update()
            .values(last_run_at=timestamp, updated_at=timestamp)
            .where(self.jobs_t.c.id == job_id)
        )
        with self.engine.begin() as connection:
            connection.execute(update)

    def record_job_success(self, job_id: str, *, when: Optional[datetime] = None) -> None:
        """잡 성공 이력을 기록한다.

        ``last_run_at`` 과 ``last_success_at`` 을 함께 갱신해 \"마지막 정상
        실행\" 추적을 단순화한다.

        Args:
            job_id: 갱신할 잡의 ID.
            when: 기록할 시각 (tz-aware). 기본은 ``now_utc()``.
        """
        timestamp = when or now_utc()
        update = (
            self.jobs_t.update()
            .values(
                last_run_at=timestamp,
                last_success_at=timestamp,
                updated_at=timestamp,
            )
            .where(self.jobs_t.c.id == job_id)
        )
        with self.engine.begin() as connection:
            connection.execute(update)

    def record_job_failure(
        self,
        job_id: str,
        *,
        error_message: str,
        when: Optional[datetime] = None,
    ) -> None:
        """잡 실패 이력을 기록한다.

        ``last_run_at`` / ``last_fail_at`` / ``last_error_message`` 를 동시 갱신.
        에러 메시지는 1024자로 truncate 하며 (DB 비대 방지), tz-aware now_utc()
        를 사용한다.

        Args:
            job_id: 갱신할 잡의 ID.
            error_message: 실패 메시지. 길면 1024자로 잘린다.
            when: 기록할 시각 (tz-aware). 기본은 ``now_utc()``.
        """
        timestamp = when or now_utc()
        truncated = (error_message or "")[:1024]
        update = (
            self.jobs_t.update()
            .values(
                last_run_at=timestamp,
                last_fail_at=timestamp,
                last_error_message=truncated,
                updated_at=timestamp,
            )
            .where(self.jobs_t.c.id == job_id)
        )
        with self.engine.begin() as connection:
            connection.execute(update)

    def __repr__(self) -> str:
        return f"<JsonSchedulerJobStore (table={self.tablename})>"


__all__ = [
    "JsonSchedulerJobStore",
    "TRIGGER_TYPE_CRON",
    "TRIGGER_TYPE_INTERVAL",
]
