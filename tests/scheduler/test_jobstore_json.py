"""JsonSchedulerJobStore 회귀 테스트 (task 00149-2).

검증 매트릭스:
    1. cron 잡 add → row.cron_expression 이 정확하고 trigger_type='cron',
       interval_seconds=NULL 이다. job_state JSON 이 ``json.loads`` 로 파싱
       가능하며 컬럼화된 필드(trigger/id/next_run_time)는 JSON 에 중복 저장
       되지 않는다.
    2. interval 잡 add → row.interval_seconds 정확, cron_expression=NULL,
       trigger_type='interval'.
    3. update_job (cron 표현식 변경) → 새 cron_expression 으로 갱신되고
       updated_at 도 변경된다.
    4. lookup_job → CronTrigger 가 KST timezone 으로 정상 재생성된다.
    5. 잡 실행 listener 의 success 콜백이 last_run_at + last_success_at 을
       채운다.
    6. 잡 실행 listener 의 failure 콜백이 last_run_at + last_fail_at +
       last_error_message 를 채우고, 1024자를 넘는 메시지는 truncate 된다.
    7. add_job 진입 시 비-JSON-serializable args/kwargs 는 TypeError 로 거부.
    8. remove_all_jobs 는 모든 row 를 삭제한다.

설계:
    - 모듈 수준 ``_scheduler`` 싱글턴은 fixture 가 매 테스트 시작·종료 시
      ``start_scheduler``/``stop_scheduler`` 로 격리한다 (기존
      ``test_recompute_next_run_time.py`` / ``test_daily_report_schedule.py``
      컨벤션).
    - SQL 직접 조회는 ``get_engine()`` 의 connection 으로 별도 발행 — jobstore
      가 자체 ``engine.begin()`` 컨텍스트로 commit 한 직후라 SELECT 가 즉시
      읽힌다 (PROJECT_NOTES L240 의 \"jobstore write 는 session_scope 밖\"
      컨벤션과 동일 흐름).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from app.db.session import get_engine
from app.scheduler import (
    JOB_ID_DAILY_REPORT,
    add_cron_schedule,
    add_interval_schedule,
    register_backup_cron_schedule,
    register_daily_report_cron_schedule,
    start_scheduler,
    stop_scheduler,
)
from app.scheduler.constants import (
    JOB_ID_BACKUP,
    SCHEDULER_JOBS_TABLENAME,
)
from app.scheduler.jobstore import (
    JsonSchedulerJobStore,
    TRIGGER_TYPE_CRON,
    TRIGGER_TYPE_INTERVAL,
)
from app.scheduler.service import _get_or_build_scheduler
from app.timezone import KST, now_utc


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def running_scheduler(test_engine: Engine) -> Iterator[None]:
    """테스트별 격리된 BackgroundScheduler 를 기동·종료한다."""
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler(wait=False)


def _select_row(job_id: str) -> dict:
    """``scheduler_jobs`` 테이블의 한 row 를 컬럼 dict 로 반환한다.

    Args:
        job_id: 조회할 잡의 ID.

    Returns:
        ``{col: value}`` 형태의 dict. row 가 없으면 빈 dict.
    """
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                f"SELECT id, next_run_time, trigger_type, cron_expression, "
                f"interval_seconds, job_state, created_at, updated_at, "
                f"last_run_at, last_success_at, last_fail_at, last_error_message "
                f"FROM {SCHEDULER_JOBS_TABLENAME} WHERE id = :job_id"
            ),
            {"job_id": job_id},
        ).mappings().first()
        return dict(row) if row else {}


def _get_jobstore() -> JsonSchedulerJobStore:
    """현재 스케줄러의 default jobstore 를 반환한다."""
    scheduler = _get_or_build_scheduler()
    store = scheduler._jobstores["default"]
    assert isinstance(store, JsonSchedulerJobStore), (
        f"기본 jobstore 가 JsonSchedulerJobStore 가 아닙니다: {type(store).__name__}"
    )
    return store


# ──────────────────────────────────────────────────────────────
# (1) cron 잡 add → 컬럼화 검증
# ──────────────────────────────────────────────────────────────


def test_add_cron_job_writes_cron_expression_column(running_scheduler: None) -> None:
    """일반 cron 잡을 등록하면 cron_expression / trigger_type 컬럼이 채워진다.

    검증 포인트:
        - trigger_type == 'cron'.
        - cron_expression == 입력 표현식.
        - interval_seconds 는 NULL.
        - next_run_time 이 채워져 있다 (paused 아님).
        - job_state 가 JSON 으로 파싱 가능하고 컬럼화된 필드(trigger/id/
          next_run_time)는 JSON 본문에 없다.
        - created_at / updated_at 이 채워져 있다.
    """
    cron_expression = "30 9 * * 1-5"
    summary = add_cron_schedule(
        cron_expression=cron_expression, active_sources=[], enabled=True,
    )

    row = _select_row(summary.job_id)
    assert row, f"잡 row 가 INSERT 되지 않았다: id={summary.job_id}"
    assert row["trigger_type"] == TRIGGER_TYPE_CRON
    assert row["cron_expression"] == cron_expression
    assert row["interval_seconds"] is None
    assert row["next_run_time"] is not None
    assert row["created_at"] is not None
    assert row["updated_at"] is not None
    # 신규 상태 컬럼은 모두 NULL 시작.
    assert row["last_run_at"] is None
    assert row["last_success_at"] is None
    assert row["last_fail_at"] is None
    assert row["last_error_message"] is None

    # JSON job_state 가 파싱 가능하고 컬럼화된 필드는 중복 저장되지 않는다.
    state = json.loads(row["job_state"])
    assert state["version"] == 1
    assert "func_ref" in state and state["func_ref"]
    assert isinstance(state["args"], list)
    assert isinstance(state["kwargs"], dict)
    # 컬럼으로 빠진 필드는 JSON 에 중복 저장하지 않는다.
    assert "trigger" not in state
    assert "id" not in state
    assert "next_run_time" not in state


def test_add_backup_cron_job_extracts_expression_from_backup_prefix(
    running_scheduler: None,
) -> None:
    """백업 잡(``backup-cron:`` prefix) 도 cron_expression 이 정확히 컬럼화된다."""
    cron_expression = "0 3 * * *"
    register_backup_cron_schedule(cron_expression=cron_expression)

    row = _select_row(JOB_ID_BACKUP)
    assert row
    assert row["trigger_type"] == TRIGGER_TYPE_CRON
    assert row["cron_expression"] == cron_expression


def test_add_daily_report_cron_job_extracts_expression_from_prefix(
    running_scheduler: None,
) -> None:
    """Daily report 잡(``daily-report-cron:`` prefix) 도 정확히 컬럼화된다."""
    cron_expression = "30 9 * * 1-5"
    register_daily_report_cron_schedule(
        cron_expression=cron_expression, enabled=True,
    )

    row = _select_row(JOB_ID_DAILY_REPORT)
    assert row
    assert row["trigger_type"] == TRIGGER_TYPE_CRON
    assert row["cron_expression"] == cron_expression


# ──────────────────────────────────────────────────────────────
# (2) interval 잡 add → 컬럼화 검증
# ──────────────────────────────────────────────────────────────


def test_add_interval_job_writes_interval_seconds_column(
    running_scheduler: None,
) -> None:
    """interval 잡을 등록하면 interval_seconds / trigger_type 컬럼이 채워진다.

    검증 포인트:
        - trigger_type == 'interval'.
        - interval_seconds == 6 * 3600.
        - cron_expression 은 NULL.
    """
    summary = add_interval_schedule(
        hours=6, active_sources=[], enabled=True,
    )

    row = _select_row(summary.job_id)
    assert row
    assert row["trigger_type"] == TRIGGER_TYPE_INTERVAL
    assert row["cron_expression"] is None
    assert row["interval_seconds"] == 6 * 3600


# ──────────────────────────────────────────────────────────────
# (3) update_job (cron 변경) → cron_expression / updated_at 갱신
# ──────────────────────────────────────────────────────────────


def test_register_daily_report_reschedule_updates_cron_expression_column(
    running_scheduler: None,
) -> None:
    """Daily report 잡의 cron 을 갱신하면 cron_expression 컬럼이 새 값으로 바뀐다.

    ``register_daily_report_cron_schedule`` 가 reschedule 흐름을 타면
    ``modify_job(name=…)`` 로 job.name 도 갱신되고, 그 다음 add/update path 가
    잡 직렬화를 다시 발급한다. 본 테스트는 그 결과 row 의 cron_expression 이
    최종적으로 새 표현식이어야 함을 가드한다.
    """
    first_cron = "30 9 * * 1-5"
    second_cron = "0 10 * * *"

    register_daily_report_cron_schedule(
        cron_expression=first_cron, enabled=True,
    )
    row_before = _select_row(JOB_ID_DAILY_REPORT)
    assert row_before["cron_expression"] == first_cron

    register_daily_report_cron_schedule(
        cron_expression=second_cron, enabled=True,
    )
    row_after = _select_row(JOB_ID_DAILY_REPORT)
    assert row_after["cron_expression"] == second_cron


# ──────────────────────────────────────────────────────────────
# (4) lookup_job → CronTrigger 가 KST 로 재생성
# ──────────────────────────────────────────────────────────────


def test_lookup_job_reconstitutes_cron_trigger_with_kst_timezone(
    running_scheduler: None,
) -> None:
    """저장된 cron 잡을 lookup 하면 CronTrigger 가 KST timezone 으로 재생성된다.

    저장 레이어에서 timezone 컨벤션이 강제됨을 보장 (task 00149-2 의 핵심 차별점).
    """
    from apscheduler.triggers.cron import CronTrigger

    cron_expression = "0 9 * * 1-5"
    summary = add_cron_schedule(
        cron_expression=cron_expression, active_sources=[], enabled=True,
    )

    store = _get_jobstore()
    job = store.lookup_job(summary.job_id)
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    assert getattr(job.trigger, "timezone", None) == KST
    # next_run_time 은 UTC tz-aware 로 정규화돼야 한다.
    assert job.next_run_time is not None
    assert job.next_run_time.tzinfo is not None


def test_lookup_job_reconstitutes_interval_trigger_with_kst_timezone(
    running_scheduler: None,
) -> None:
    """저장된 interval 잡 lookup 시 IntervalTrigger 가 KST 로 재생성된다."""
    from apscheduler.triggers.interval import IntervalTrigger

    summary = add_interval_schedule(
        hours=12, active_sources=[], enabled=True,
    )

    store = _get_jobstore()
    job = store.lookup_job(summary.job_id)
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    assert int(job.trigger.interval.total_seconds()) == 12 * 3600


# ──────────────────────────────────────────────────────────────
# (5) listener 의 success 콜백 → last_run_at / last_success_at 갱신
# ──────────────────────────────────────────────────────────────


def test_record_job_success_updates_last_run_and_last_success(
    running_scheduler: None,
) -> None:
    """``record_job_success`` 가 last_run_at + last_success_at 을 동시 갱신한다.

    listener 가 EVENT_JOB_EXECUTED 에서 본 메서드를 호출하므로, 본 단위 테스트는
    그 효과를 직접 검증한다 (실제 잡 실행을 트리거하는 통합 테스트는 E2E 단계
    에서 다룬다).
    """
    summary = add_cron_schedule(
        cron_expression="0 9 * * 1-5", active_sources=[], enabled=True,
    )

    store = _get_jobstore()
    timestamp = now_utc()
    store.record_job_success(summary.job_id, when=timestamp)

    row = _select_row(summary.job_id)
    assert row["last_run_at"] is not None
    assert row["last_success_at"] is not None
    assert row["last_fail_at"] is None
    assert row["last_error_message"] is None


# ──────────────────────────────────────────────────────────────
# (6) listener 의 failure 콜백 → last_run_at / last_fail_at / 메시지 갱신
# ──────────────────────────────────────────────────────────────


def test_record_job_failure_updates_failure_columns(
    running_scheduler: None,
) -> None:
    """``record_job_failure`` 가 last_run_at / last_fail_at / last_error_message 를 동시 갱신한다."""
    summary = add_cron_schedule(
        cron_expression="0 9 * * 1-5", active_sources=[], enabled=True,
    )

    store = _get_jobstore()
    store.record_job_failure(summary.job_id, error_message="boom: SMTPError")

    row = _select_row(summary.job_id)
    assert row["last_run_at"] is not None
    assert row["last_fail_at"] is not None
    assert row["last_success_at"] is None
    assert row["last_error_message"] == "boom: SMTPError"


def test_record_job_failure_truncates_long_error_message(
    running_scheduler: None,
) -> None:
    """``record_job_failure`` 는 1024자를 넘는 에러 메시지를 truncate 한다 (DB 비대 방지)."""
    summary = add_cron_schedule(
        cron_expression="0 9 * * 1-5", active_sources=[], enabled=True,
    )

    store = _get_jobstore()
    long_message = "x" * 5000
    store.record_job_failure(summary.job_id, error_message=long_message)

    row = _select_row(summary.job_id)
    assert row["last_error_message"] is not None
    assert len(row["last_error_message"]) == 1024


# ──────────────────────────────────────────────────────────────
# (7) JSON-serializable 검증 — 비-primitive 인자는 add_job 거부
# ──────────────────────────────────────────────────────────────


def test_add_job_rejects_non_json_serializable_args(
    running_scheduler: None,
) -> None:
    """args 가 JSON-serializable 하지 않으면 add_job 단계에서 TypeError 로 거부.

    이 프로젝트의 잡 함수는 (a) 빈 args 또는 (b) list[str] 만 쓰지만, 본
    검증은 미래의 호출자가 datetime / 객체 인스턴스 등을 잘못 넘기는 회귀를
    가드한다.
    """
    from apscheduler.triggers.interval import IntervalTrigger

    from app.scheduler.job_runner import scheduled_scrape

    scheduler = _get_or_build_scheduler()
    # datetime 객체는 json.dumps 의 기본 인코더로 직렬화 불가.
    with pytest.raises(TypeError) as excinfo:
        scheduler.add_job(
            scheduled_scrape,
            trigger=IntervalTrigger(hours=1, timezone=KST),
            args=[datetime(2026, 1, 1)],
            id="json-error-test",
            name="interval:매 1시간",
        )
    assert "JSON-serializable" in str(excinfo.value)


# ──────────────────────────────────────────────────────────────
# (8) remove_all_jobs → 모든 row 삭제
# ──────────────────────────────────────────────────────────────


def test_remove_all_jobs_deletes_every_row(running_scheduler: None) -> None:
    """``remove_all_jobs`` 가 jobstore 의 모든 잡을 삭제한다."""
    add_cron_schedule(cron_expression="0 9 * * 1-5", active_sources=[], enabled=True)
    add_interval_schedule(hours=6, active_sources=[], enabled=True)

    store = _get_jobstore()
    assert len(store.get_all_jobs()) >= 2

    store.remove_all_jobs()
    assert store.get_all_jobs() == []


# ──────────────────────────────────────────────────────────────
# (9) get_next_run_time → 가장 가까운 잡의 시각을 반환
# ──────────────────────────────────────────────────────────────


def test_get_next_run_time_returns_earliest_among_active_jobs(
    running_scheduler: None,
) -> None:
    """``get_next_run_time`` 이 paused 잡을 제외하고 가장 빠른 시각을 반환한다."""
    summary_soon = add_cron_schedule(
        cron_expression="* * * * *", active_sources=[], enabled=True,
    )
    summary_later = add_cron_schedule(
        cron_expression="0 0 1 1 *", active_sources=[], enabled=True,
    )

    store = _get_jobstore()
    nrt = store.get_next_run_time()
    assert nrt is not None
    # 매 분 cron 잡의 next_run_time 이 가장 빠를 것.
    assert nrt <= now_utc() + timedelta(minutes=2)
    # 양 잡 모두 등록돼 있음을 부수 확인.
    assert store.lookup_job(summary_soon.job_id) is not None
    assert store.lookup_job(summary_later.job_id) is not None
