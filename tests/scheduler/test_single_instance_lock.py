"""스케줄러 단일 인스턴스 flock 회귀 테스트 (task 00131-1).

cron 중복 실행 버그의 근본 원인은 uvicorn ``--reload`` 환경에서
``BackgroundScheduler`` 인스턴스가 여러 프로세스에 누적되어, 같은
``scheduler_jobs`` 테이블을 보는 살아있는 스케줄러 수만큼 동일 job 이 중복
실행되는 것이다. ``app/scheduler/single_instance.py`` 의 flock 기반 단일
인스턴스 가드가 '컨테이너당 job 을 실행하는 스케줄러는 1개' 불변식을 지키는지
검증한다.

검증 항목:
    - lock 미점유 상태에서 ``try_acquire`` → True (winner).
    - 같은 프로세스의 반복 호출은 멱등하게 True.
    - 이미 다른 fd 가 flock 을 쥔 상태에서 ``try_acquire`` → False (loser).
      flock(2) 은 동일 프로세스라도 서로 다른 open file description 끼리는
      상호 배제되므로, '다른 프로세스가 lock 을 점유한 상황' 을 단일 프로세스
      테스트로 결정적으로 재현할 수 있다.
    - release 후 재획득 가능 (좀비 worker 가 죽으면 다음 worker 가 승계).
    - lock 이 점유된 상태에서 ``start_scheduler()`` 는 스케줄러를 띄우지 않는다
      — loser 프로세스는 job 을 실행하지 않아 동일 trigger 시각의 중복 실행이
      차단된다.
    - lock 이 비어 있으면 ``start_scheduler()`` 가 정상 기동하고, lock 도 함께
      쥔다. ``stop_scheduler()`` 는 스케줄러 정지와 함께 lock 을 해제한다.

설계 결정:
    - 모듈 수준 ``_scheduler`` 싱글턴은 각 테스트가 ``stop_scheduler`` 로
      리셋한다 (기존 ``test_daily_report_schedule.py`` 컨벤션과 동일).
    - 모듈 수준 lock 상태(``_lock_file_descriptor``)는 ``_reset_single_instance_lock``
      픽스처가 매 테스트 전후로 강제 해제해 테스트 간 누수를 막는다.
    - lock 파일 경로는 SQLite DB 디렉터리에서 파생되므로, ``test_engine``
      픽스처의 테스트별 임시 DB 덕분에 테스트별로 자동 격리된다.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.scheduler import (
    is_scheduler_running,
    start_scheduler,
    stop_scheduler,
)
from app.scheduler.single_instance import (
    _resolve_lock_file_path,
    holds_single_instance_lock,
    release_single_instance_lock,
    try_acquire_single_instance_lock,
)


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def _reset_single_instance_lock() -> Iterator[None]:
    """각 테스트 전후로 단일 인스턴스 lock 모듈 상태를 강제 초기화한다.

    ``release_single_instance_lock`` 은 lock 을 쥔 적이 없으면 no-op 이므로,
    이전 테스트가 lock 을 남겼든 아니든 안전하게 깨끗한 상태에서 시작한다.
    """
    release_single_instance_lock()
    try:
        yield
    finally:
        release_single_instance_lock()


# ──────────────────────────────────────────────────────────────
# try_acquire / release 단위 동작
# ──────────────────────────────────────────────────────────────


def test_try_acquire_succeeds_when_lock_is_free(
    test_engine: Engine,
    _reset_single_instance_lock: None,
) -> None:
    """lock 이 비어 있으면 ``try_acquire`` 는 True (winner) 를 반환한다."""
    assert holds_single_instance_lock() is False

    acquired = try_acquire_single_instance_lock()

    assert acquired is True
    assert holds_single_instance_lock() is True


def test_try_acquire_is_idempotent_for_same_process(
    test_engine: Engine,
    _reset_single_instance_lock: None,
) -> None:
    """이미 lock 을 쥔 프로세스가 다시 호출해도 멱등하게 True 를 반환한다."""
    assert try_acquire_single_instance_lock() is True
    # 두 번째 호출은 새 fd 를 열지 않고 곧바로 True.
    assert try_acquire_single_instance_lock() is True
    assert holds_single_instance_lock() is True


def test_try_acquire_fails_when_another_holder_locks_file(
    test_engine: Engine,
    _reset_single_instance_lock: None,
) -> None:
    """다른 fd 가 이미 flock 을 쥔 상태에서는 ``try_acquire`` 가 False (loser).

    flock(2) 은 동일 프로세스라도 서로 다른 open file description 끼리는 상호
    배제된다. 따라서 별도 fd 로 lock 을 잡아 '다른 프로세스가 점유 중' 상황을
    결정적으로 재현한다.
    """
    lock_path: Path = _resolve_lock_file_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    external_descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # 다른 프로세스가 lock 을 점유한 상황을 시뮬레이션.
        fcntl.flock(external_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # 이미 점유 중 → try_acquire 는 False, lock 을 쥐지 못한다.
        assert try_acquire_single_instance_lock() is False
        assert holds_single_instance_lock() is False
    finally:
        fcntl.flock(external_descriptor, fcntl.LOCK_UN)
        os.close(external_descriptor)


def test_release_allows_reacquire(
    test_engine: Engine,
    _reset_single_instance_lock: None,
) -> None:
    """release 후에는 다시 lock 을 획득할 수 있다.

    좀비 worker 가 실제로 종료되면(=lock 해제) 다음 worker 가 lock 을 승계해
    스케줄러를 정상 기동할 수 있어야 한다 — ``--reload`` 개발 편의 유지.
    """
    assert try_acquire_single_instance_lock() is True

    release_single_instance_lock()
    assert holds_single_instance_lock() is False

    # 해제 후 재획득 가능.
    assert try_acquire_single_instance_lock() is True
    assert holds_single_instance_lock() is True


# ──────────────────────────────────────────────────────────────
# start_scheduler / stop_scheduler 통합 동작
# ──────────────────────────────────────────────────────────────


def test_start_scheduler_skips_when_lock_held_by_other(
    test_engine: Engine,
    _reset_single_instance_lock: None,
) -> None:
    """다른 프로세스가 lock 을 점유 중이면 ``start_scheduler`` 는 스케줄러를 띄우지 않는다.

    이것이 cron 중복 실행 방지의 핵심이다 — lock 을 쥐지 못한 프로세스는
    스케줄러를 띄우지 않아 동일 trigger 시각에 job 을 실행하지 않는다.
    """
    lock_path: Path = _resolve_lock_file_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    external_descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # 다른 프로세스가 lock 을 점유한 상황을 시뮬레이션.
        fcntl.flock(external_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        start_scheduler()

        # lock 을 쥐지 못했으므로 스케줄러는 기동되지 않아야 한다.
        assert is_scheduler_running() is False
    finally:
        # _scheduler 싱글턴을 None 으로 리셋 (기동 안 됐어도 객체는 빌드됨).
        stop_scheduler(wait=False)
        fcntl.flock(external_descriptor, fcntl.LOCK_UN)
        os.close(external_descriptor)


def test_start_scheduler_runs_when_lock_is_free_and_stop_releases_lock(
    test_engine: Engine,
    _reset_single_instance_lock: None,
) -> None:
    """lock 이 비어 있으면 ``start_scheduler`` 가 정상 기동하고 lock 도 쥔다.

    또한 ``stop_scheduler`` 가 스케줄러 정지와 함께 lock 을 해제하는지 검증한다
    — 다음 worker 가 lock 을 승계할 수 있어야 한다.
    """
    start_scheduler()
    try:
        assert is_scheduler_running() is True
        # 기동에 성공한 프로세스는 단일 인스턴스 lock 도 쥐고 있어야 한다.
        assert holds_single_instance_lock() is True
    finally:
        stop_scheduler(wait=False)

    # stop 이 스케줄러 정지와 함께 lock 을 해제했어야 한다.
    assert is_scheduler_running() is False
    assert holds_single_instance_lock() is False


# ──────────────────────────────────────────────────────────────
# task 00133 — ASGI lifespan startup 훅 회귀 가드
# ──────────────────────────────────────────────────────────────
#
# 회귀 배경: ``app = create_app()`` 은 모듈 수준 코드라 import 마다 실행된다.
# uvicorn ``--reload`` 환경에서는 reload 감시 부모 프로세스와 실제 요청을
# 처리하는 worker 프로세스가 둘 다 ``app.web.main`` 을 import 한다. 예전처럼
# create_app() 본문에서 start_scheduler() 를 호출하면, task 131 의 단일
# 인스턴스 flock 을 reload 감시 부모가 worker 보다 먼저 쥐어 버려, 정작 HTTP
# 를 처리하는 worker 의 스케줄러가 영구히 미기동 상태가 된다 (task 131 이후
# 보고된 'APScheduler 가 기동되지 않았습니다' 회귀). 수정: 스케줄러 기동을
# ASGI lifespan 의 startup 훅으로 옮겨, ASGI 서버가 실제로 가동되는 프로세스
# (= worker) 에서만 기동되도록 한다.


def test_create_app_does_not_start_scheduler_in_module_body(
    test_engine: Engine,
    _reset_single_instance_lock: None,
) -> None:
    """``create_app()`` 호출만으로는 스케줄러가 기동되지 않아야 한다 (task 00133).

    스케줄러 기동은 ASGI lifespan 의 startup 훅으로 옮겨졌다. 누군가
    실수로 start_scheduler() 를 다시 create_app() 본문으로 되돌리면 본
    테스트가 깨져 회귀를 즉시 잡는다.
    """
    from app.web.main import create_app

    # 직전 테스트가 남긴 _scheduler 싱글턴이 있을 수 있으니 먼저 리셋.
    stop_scheduler(wait=False)

    app = create_app()

    # create_app() 본문은 더 이상 스케줄러를 기동하지 않는다 — startup 훅
    # (TestClient with-블록 진입) 전까지는 미기동 상태여야 한다.
    assert is_scheduler_running() is False
    assert holds_single_instance_lock() is False
    # app 객체 자체는 정상적으로 만들어져야 한다.
    assert app is not None


def test_asgi_startup_hook_starts_scheduler(
    test_engine: Engine,
    _reset_single_instance_lock: None,
) -> None:
    """ASGI lifespan startup 훅이 실행되면 스케줄러가 기동된다 (task 00133).

    ``with TestClient(app)`` 진입은 FastAPI 의 startup 이벤트를 발생시킨다.
    이는 실제 ASGI 서버가 가동되는 worker 프로세스의 동작을 그대로 모사한다 —
    이 시점에 스케줄러가 기동되고 단일 인스턴스 lock 도 쥐어야 한다. with-블록
    종료 시 shutdown 훅이 스케줄러를 정지하고 lock 을 해제해야 한다.
    """
    from fastapi.testclient import TestClient

    from app.web.main import create_app

    stop_scheduler(wait=False)
    app = create_app()

    # with-블록 진입 = startup 훅 발화 → 스케줄러 기동.
    with TestClient(app):
        assert is_scheduler_running() is True
        # 기동에 성공한 프로세스는 단일 인스턴스 lock 도 쥐고 있어야 한다.
        assert holds_single_instance_lock() is True

    # with-블록 종료 = shutdown 훅 발화 → 스케줄러 정지 + lock 해제.
    assert is_scheduler_running() is False
    assert holds_single_instance_lock() is False
