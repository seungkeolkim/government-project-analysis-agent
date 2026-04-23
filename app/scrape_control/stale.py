"""웹 startup 시 stale running row 정리 모듈.

웹 프로세스가 재시작되면 이전 인스턴스가 관리하던 subprocess 와 연결이 끊긴다.
pid 가 사라졌거나(프로세스 고아화) pid 자체가 기록되지 않은 running row 는
"관리 불가능한 상태" 이므로 failed 로 마감해 lock 을 해제한다.

사용 위치:
    - ``app/web/main.py`` startup event 에서 ``cleanup_stale_running_runs()``
      를 호출. 스케줄러 기동보다 **먼저** 돌아야 스케줄러가 곧바로 다음
      run 을 시작할 수 있다.

판정 규칙은 ``app.db.repository.fail_stale_running_runs`` 의 docstring 참조.
본 모듈은 세션 관리만 감싸는 얇은 래퍼다 — CLI 에서도 수동 정리에 쓸 수 있도록
함수 이름을 별도로 둔다.
"""

from __future__ import annotations

from loguru import logger

from app.db.repository import fail_stale_running_runs
from app.db.session import session_scope


def cleanup_stale_running_runs() -> int:
    """pid 미기록 또는 프로세스 사라진 running ScrapeRun row 를 failed 로 정리한다.

    단일 트랜잭션에서 수행한다. 반환값은 정리된 row 수.

    Returns:
        정리된 row 수. 0 이면 stale 이 없거나 이미 정상 상태.
    """
    with session_scope() as session:
        cleaned = fail_stale_running_runs(session)
    if cleaned:
        logger.info("startup stale cleanup: {}건의 running ScrapeRun 을 failed 로 마감", cleaned)
    return cleaned


__all__ = [
    "cleanup_stale_running_runs",
]
