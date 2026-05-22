"""스케줄러 단일 인스턴스 보장을 위한 프로세스 수준 파일 lock (flock).

배경 (task 00131 — cron 중복 실행 버그):
    이 프로젝트는 OS crontab / systemd timer 를 쓰지 않는다. 공고 수집·daily
    report·백업·GC 등 모든 정기 작업은 웹 프로세스 내부의 APScheduler
    ``BackgroundScheduler`` (+ ``SQLAlchemyJobStore('scheduler_jobs')``) 가
    담당한다.

    docker-compose 의 uvicorn 은 개발 편의를 위해 항상 ``--reload`` 로 떠 있어,
    코드 변경/재기동마다 worker 프로세스가 교체된다. 이전 worker 의
    ``BackgroundScheduler`` 인스턴스가 깨끗이 정리되지 못하고 (shutdown 훅 미완료
    /지연/지연 종료) 살아남으면, **같은 ``scheduler_jobs`` 테이블을 보는 살아있는
    스케줄러가 N개** 가 된다. APScheduler 3.x ``BackgroundScheduler`` 는 프로세스
    간 실행 조율(분산 lock) 수단이 없으므로, 살아있는 스케줄러가 N개면 동일 job
    이 trigger 시각마다 N회 중복 실행된다 — 사용자가 관측한 '2번 → 3번' 점증
    증상의 근본 원인이다.

방식:
    고정 경로의 lock 파일에 ``flock(LOCK_EX | LOCK_NB)`` 을 건다. lock 획득에
    성공한 **단 하나의 프로세스만** 스케줄러를 기동해 job 을 실행하고, 실패한
    프로세스는 스케줄러를 띄우지 않는다 — '컨테이너당 job 을 실행하는 스케줄러는
    1개' 불변식을 코드로 강제한다.

    flock 은 advisory lock 이며 lock 을 쥔 프로세스가 종료되거나 fd 가 닫히면
    커널이 자동으로 해제한다. 따라서 좀비 worker 가 실제로 사라지면 다음 worker
    가 자연스럽게 lock 을 승계한다 — ``--reload`` 개발 편의를 그대로 유지한다.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

# 단일 인스턴스 lock 파일 이름. SQLite DB 파일과 같은 디렉터리에 둔다.
_LOCK_FILENAME: str = "scheduler-single-instance.lock"

# 획득한 lock 파일 디스크립터. flock 은 fd(open file description) 에 묶여 있어,
# fd 가 닫히거나 GC 되면 lock 이 풀린다. 따라서 프로세스 수명 내내 모듈 수준에
# 보관해 명시적으로 release 할 때까지 열어 둔다.
_lock_file_descriptor: Optional[int] = None

# 모듈 수준 상태(_lock_file_descriptor) 보호용 lock. 스케줄러 start/stop 이
# 여러 스레드에서 호출될 수 있어 획득·해제를 직렬화한다.
_lock_state_guard: threading.Lock = threading.Lock()


def _resolve_lock_file_path() -> Path:
    """단일 인스턴스 lock 파일의 절대 경로를 결정한다.

    SQLite DB 파일과 같은 디렉터리에 lock 파일을 둔다. 운영 환경에서는
    ``/app/data`` 같은 컨테이너 내 고정 경로가 되어 같은 컨테이너의 모든
    worker 프로세스가 동일 파일을 본다. 테스트 환경에서는 테스트별 임시
    디렉터리(tmp_path)의 DB 를 쓰므로 lock 파일도 테스트별로 격리된다.

    DB 경로를 알 수 없거나 in-memory SQLite 인 경우 OS 임시 디렉터리로
    폴백한다.

    Returns:
        lock 파일의 절대 경로.
    """
    # 순환 import 회피 + 엔진 초기화 순서 의존을 줄이기 위해 함수 안에서 import.
    from app.db.session import get_engine

    database_path: Optional[str]
    try:
        database_path = get_engine().url.database
    except Exception:
        # 엔진을 아직 만들 수 없는 예외적 상황 — 폴백 경로를 쓴다.
        database_path = None

    if database_path and database_path != ":memory:":
        return Path(database_path).resolve().parent / _LOCK_FILENAME

    # 폴백: OS 임시 디렉터리. 같은 호스트의 동일 컨테이너라면 동일 경로가 된다.
    return Path(tempfile.gettempdir()) / f"iris-{_LOCK_FILENAME}"


def try_acquire_single_instance_lock() -> bool:
    """스케줄러 job 실행 권한을 나타내는 프로세스 수준 flock 을 시도한다.

    이미 이 프로세스가 lock 을 쥐고 있으면 새 fd 를 열지 않고 곧바로 ``True`` 를
    반환한다 (멱등). 다른 프로세스가 lock 을 점유 중이면 ``False`` 를 반환하며,
    이 경우 호출자는 스케줄러를 띄우지 않아야 한다 (중복 실행 방지).

    Returns:
        True  — 이 프로세스가 lock 을 획득했다. 스케줄러 job 을 실행해도 된다.
        False — 다른 프로세스가 이미 lock 을 점유 중이다. job 실행 금지.
    """
    global _lock_file_descriptor

    with _lock_state_guard:
        if _lock_file_descriptor is not None:
            # 이미 이 프로세스가 lock 을 쥐고 있다 — 멱등하게 성공 처리.
            return True

        lock_path = _resolve_lock_file_path()
        # 디렉터리는 보통 ensure_runtime_paths 가 보장하지만 방어적으로 한 번 더.
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            # LOCK_NB: 이미 점유 중이면 블로킹하지 않고 즉시 OSError 를 던진다.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # 다른 프로세스가 lock 을 점유 중 — 방금 연 fd 를 닫고 실패를 알린다.
            os.close(descriptor)
            logger.warning(
                "스케줄러 단일 인스턴스 lock 획득 실패 — 다른 프로세스가 이미 "
                "스케줄러를 실행 중이다. 이 프로세스에서는 스케줄 job 을 "
                "실행하지 않는다 (lock_path={} pid={}).",
                lock_path,
                os.getpid(),
            )
            return False

        _lock_file_descriptor = descriptor
        logger.info(
            "스케줄러 단일 인스턴스 lock 획득 — 이 프로세스가 스케줄러 job 을 "
            "실행한다 (lock_path={} pid={}).",
            lock_path,
            os.getpid(),
        )
        return True


def release_single_instance_lock() -> None:
    """획득한 단일 인스턴스 lock 을 해제한다.

    lock 을 쥔 적이 없으면 아무 일도 하지 않는다 (no-op). 스케줄러를 정지할 때
    호출해, 다음 worker(재기동/reload) 가 lock 을 승계해 스케줄러를 정상
    기동할 수 있도록 한다.
    """
    global _lock_file_descriptor

    with _lock_state_guard:
        if _lock_file_descriptor is None:
            return

        try:
            fcntl.flock(_lock_file_descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            logger.warning(
                "스케줄러 단일 인스턴스 lock 해제 중 flock(LOCK_UN) 예외(무시): {}: {}",
                type(exc).__name__,
                exc,
            )
        try:
            os.close(_lock_file_descriptor)
        except OSError as exc:
            logger.warning(
                "스케줄러 단일 인스턴스 lock fd close 중 예외(무시): {}: {}",
                type(exc).__name__,
                exc,
            )

        _lock_file_descriptor = None
        logger.info("스케줄러 단일 인스턴스 lock 해제 완료.")


def holds_single_instance_lock() -> bool:
    """이 프로세스가 현재 단일 인스턴스 lock 을 쥐고 있는지 반환한다.

    진단·테스트용 헬퍼. 운영 코드 흐름에서는 ``try_acquire`` 의 반환값으로
    분기하면 충분하다.
    """
    with _lock_state_guard:
        return _lock_file_descriptor is not None


__all__ = [
    "holds_single_instance_lock",
    "release_single_instance_lock",
    "try_acquire_single_instance_lock",
]
