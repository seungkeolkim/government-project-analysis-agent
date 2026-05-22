"""스케줄 job single-flight 가드 회귀 테스트 (task 00131-2).

검증 대상 (subtask 00131-2 의 acceptance_criteria):

    A. ``try_claim_schedule_slot`` — claim 원자성.
        A-1. 동일 (job_name, slot) 두 번째 호출부터 False (첫 호출만 True).
        A-2. 다른 슬롯이면 다시 claim 가능.
        A-3. 다른 job_name 은 서로 독립.
        A-4. 동시(thread) 다중 호출 시 정확히 1개만 True.

    B. 잡 함수 — 동일 주기 2~3회 호출돼도 부수효과 1회.
        B-1. ``scheduled_scrape`` 3회 호출 → ``start_scrape_run`` 1회.
        B-2. ``scheduled_daily_report_job`` 3회 호출 →
             ``prepare_and_send_daily_report`` 1회.
        B-3. ``scheduled_backup_job`` 3회 호출 → ``run_backup`` 1회.
        B-4. ``gc_orphan_attachments_job`` 3회 호출 → ``run_gc`` 1회.
        B-5. 가드 발동(skip) 시 예외 없이 조용히 return.

DB:
    tests/conftest.py 의 ``test_engine`` fixture 가 Alembic upgrade head 까지
    적용해 ``scheduler_job_claims`` 테이블을 만든 상태를 제공한다.

시각 결정성:
    잡 함수는 ``now`` 인자를 받지 않으므로, B 시나리오에서는
    ``app.scheduler.job_guard.now_utc`` 를 고정 시각으로 monkeypatch 해 3회
    호출이 모두 같은 slot 버킷을 계산하도록 만든다. A 시나리오는
    ``try_claim_schedule_slot`` 의 ``now`` 인자를 직접 주입한다.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, func, select

from app.db.models import SchedulerJobClaim
from app.db.session import session_scope
from app.scheduler.job_guard import (
    JOB_NAME_BACKUP,
    JOB_NAME_DAILY_REPORT,
    JOB_NAME_GC_ORPHAN,
    JOB_NAME_SCHEDULED_SCRAPE,
    try_claim_schedule_slot,
)
from app.scheduler.job_runner import (
    gc_orphan_attachments_job,
    scheduled_backup_job,
    scheduled_daily_report_job,
    scheduled_scrape,
)


# 모든 테스트가 공유하는 고정 기준 시각 (UTC). 같은 분 안의 값이면 slot 이
# 동일하므로, 명시적으로 초까지 고정해 결정성을 확보한다.
_FIXED_NOW: datetime = datetime(2026, 5, 22, 0, 0, 5, tzinfo=UTC)


def _count_claims(job_name: str) -> int:
    """``scheduler_job_claims`` 에 쌓인 특정 job 의 claim row 수를 센다."""
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(SchedulerJobClaim)
                .where(SchedulerJobClaim.job_name == job_name)
            ).scalar_one()
        )


# ──────────────────────────────────────────────────────────────
# A. try_claim_schedule_slot — claim 원자성
# ──────────────────────────────────────────────────────────────


def test_first_claim_succeeds_and_same_slot_duplicates_skip(
    test_engine: Engine,
) -> None:
    """동일 (job_name, slot) 은 첫 호출만 True, 이후 호출은 모두 False (A-1)."""
    first = try_claim_schedule_slot("test_job", now=_FIXED_NOW)
    second = try_claim_schedule_slot("test_job", now=_FIXED_NOW)
    third = try_claim_schedule_slot("test_job", now=_FIXED_NOW)

    assert first is True
    assert second is False
    assert third is False

    # claim row 는 첫 호출 1건만 INSERT 되어야 한다.
    assert _count_claims("test_job") == 1


def test_different_slot_can_be_claimed_again(test_engine: Engine) -> None:
    """slot 버킷(시간창)이 다르면 같은 job 도 다시 claim 가능하다 (A-2)."""
    first = try_claim_schedule_slot("test_job", now=_FIXED_NOW)
    # 기본 시간창 60초보다 충분히 뒤 — 다른 버킷이 된다.
    later = _FIXED_NOW + timedelta(seconds=120)
    second = try_claim_schedule_slot("test_job", now=later)

    assert first is True
    assert second is True
    assert _count_claims("test_job") == 2


def test_different_job_name_is_independent(test_engine: Engine) -> None:
    """job_name 이 다르면 같은 slot 이라도 각자 독립적으로 claim 된다 (A-3)."""
    claimed_a = try_claim_schedule_slot("job_a", now=_FIXED_NOW)
    claimed_b = try_claim_schedule_slot("job_b", now=_FIXED_NOW)

    assert claimed_a is True
    assert claimed_b is True


def test_concurrent_claims_only_one_wins(test_engine: Engine) -> None:
    """동일 슬롯을 여러 스레드가 동시에 claim 하면 정확히 1개만 True (A-4).

    멀티-인스턴스 스케줄러가 같은 trigger 시각에 거의 동시에 job 을 발사하는
    상황을 시뮬레이션한다. SQLite UNIQUE 제약 + 단일 writer 직렬화로 단 하나의
    INSERT 만 성공해야 한다.
    """
    thread_count = 6
    barrier = threading.Barrier(thread_count)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker() -> None:
        """배리어로 출발을 맞춘 뒤 동일 슬롯 claim 을 시도한다."""
        barrier.wait()
        claimed = try_claim_schedule_slot("concurrent_job", now=_FIXED_NOW)
        with results_lock:
            results.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 정확히 1개의 호출만 claim 을 획득한다.
    assert results.count(True) == 1
    assert results.count(False) == thread_count - 1
    assert _count_claims("concurrent_job") == 1


# ──────────────────────────────────────────────────────────────
# B. 잡 함수 — 동일 주기 2~3회 호출돼도 부수효과 1회
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def freeze_guard_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """``job_guard.now_utc`` 를 고정 시각으로 묶어 slot 버킷을 결정적으로 만든다.

    잡 함수는 ``now`` 인자를 받지 않으므로, 가드가 참조하는 시각 소스를 직접
    고정해야 한 테스트 안의 2~3회 호출이 같은 slot 을 계산한다.
    """
    monkeypatch.setattr(
        "app.scheduler.job_guard.now_utc", lambda: _FIXED_NOW,
    )


def test_scheduled_scrape_runs_side_effect_once_for_repeated_calls(
    test_engine: Engine,
    freeze_guard_now: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``scheduled_scrape`` 를 동일 주기에 3회 호출해도 수집은 1회만 기동된다 (B-1).

    실제 docker subprocess 기동을 피하려 ``start_scrape_run`` 을 가짜로 교체해
    호출 횟수만 센다. 가드가 2·3번째 호출을 걸러야 한다.
    """
    call_count = {"value": 0}

    def fake_start_scrape_run(active_sources, **kwargs):  # noqa: ANN001, ANN003
        """호출 횟수만 세는 가짜 start_scrape_run."""
        call_count["value"] += 1
        return SimpleNamespace(scrape_run_id=call_count["value"], pid=1234)

    monkeypatch.setattr(
        "app.scheduler.job_runner.start_scrape_run", fake_start_scrape_run,
    )

    # 동일 주기 3회 호출 — 예외 없이 모두 정상 반환해야 한다.
    scheduled_scrape([])
    scheduled_scrape([])
    scheduled_scrape([])

    # start_scrape_run 은 첫 호출에서만 실행된다.
    assert call_count["value"] == 1
    assert _count_claims(JOB_NAME_SCHEDULED_SCRAPE) == 1


def test_scheduled_daily_report_sends_once_for_repeated_calls(
    test_engine: Engine,
    freeze_guard_now: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``scheduled_daily_report_job`` 을 동일 예정 시각에 3회 호출해도 발송 1회 (B-2).

    2026-05-22 09:00 KST 에 3통이 발송된 증상의 회귀 가드. 실제 메일 발송 대신
    ``prepare_and_send_daily_report`` 를 가짜로 교체해 호출 횟수를 센다.
    """
    send_count = {"value": 0}

    def fake_prepare_and_send(request, **kwargs):  # noqa: ANN001, ANN003
        """발송 횟수만 세는 가짜 prepare_and_send_daily_report."""
        send_count["value"] += 1
        return SimpleNamespace(
            run_id=send_count["value"],
            status="success",
            snapshot_count=0,
            recipient_count=0,
            success_count=0,
            failure_count=0,
        )

    def fake_build_transport(session):  # noqa: ANN001
        """transport 빌드를 우회하는 더미 — 가짜 발송 함수가 쓰지 않는다."""
        return SimpleNamespace()

    monkeypatch.setattr(
        "app.email.daily_report.prepare_and_send_daily_report",
        fake_prepare_and_send,
    )
    monkeypatch.setattr(
        "app.email.transport.factory.build_transport_from_settings",
        fake_build_transport,
    )

    # 동일 예정 시각 3회 호출.
    scheduled_daily_report_job()
    scheduled_daily_report_job()
    scheduled_daily_report_job()

    # 발송은 첫 호출에서만 일어난다.
    assert send_count["value"] == 1
    assert _count_claims(JOB_NAME_DAILY_REPORT) == 1


def test_scheduled_backup_runs_once_for_repeated_calls(
    test_engine: Engine,
    freeze_guard_now: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``scheduled_backup_job`` 을 동일 주기에 3회 호출해도 백업은 1회만 (B-3)."""
    backup_count = {"value": 0}

    def fake_run_backup(**kwargs):  # noqa: ANN003
        """백업 횟수만 세는 가짜 run_backup."""
        backup_count["value"] += 1
        return SimpleNamespace(
            id=backup_count["value"],
            success=True,
            backup_files=0,
            duration_seconds=0,
        )

    monkeypatch.setattr("app.backup.service.run_backup", fake_run_backup)

    scheduled_backup_job()
    scheduled_backup_job()
    scheduled_backup_job()

    assert backup_count["value"] == 1
    assert _count_claims(JOB_NAME_BACKUP) == 1


def test_gc_orphan_runs_once_for_repeated_calls(
    test_engine: Engine,
    freeze_guard_now: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gc_orphan_attachments_job`` 을 동일 주기에 3회 호출해도 GC 는 1회만 (B-4)."""
    gc_count = {"value": 0}

    def fake_run_gc(**kwargs):  # noqa: ANN003
        """GC 횟수만 세는 가짜 run_gc — 잡 함수가 읽는 필드를 모두 채운다."""
        gc_count["value"] += 1
        return SimpleNamespace(
            skipped_due_to_running_scrape_run=False,
            scanned_root="/tmp",
            disk_file_count=0,
            db_referenced_count=0,
            deleted_count=0,
            deletion_failed=[],
            removed_directory_count=0,
            total_orphan_bytes=0,
        )

    monkeypatch.setattr("app.scheduler.job_runner.run_gc", fake_run_gc)

    gc_orphan_attachments_job()
    gc_orphan_attachments_job()
    gc_orphan_attachments_job()

    assert gc_count["value"] == 1
    assert _count_claims(JOB_NAME_GC_ORPHAN) == 1


def test_guard_skip_does_not_raise(
    test_engine: Engine,
    freeze_guard_now: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """가드 발동(skip) 시 예외 없이 조용히 return 한다 (B-5).

    잡 함수의 2·3번째 호출은 claim 실패로 skip 되는데, 이때 예외가 전파되면
    APScheduler 스레드가 위험해진다. 본 테스트는 반복 호출이 예외를 던지지
    않는 것 자체를 통과 조건으로 둔다.
    """

    def fake_start_scrape_run(active_sources, **kwargs):  # noqa: ANN001, ANN003
        """첫 호출만 도달하는 가짜 start_scrape_run."""
        return SimpleNamespace(scrape_run_id=1, pid=1)

    monkeypatch.setattr(
        "app.scheduler.job_runner.start_scrape_run", fake_start_scrape_run,
    )

    # 2·3번째 호출은 가드가 skip — 예외가 새지 않아야 한다.
    scheduled_scrape([])
    scheduled_scrape([])
    scheduled_scrape([])
