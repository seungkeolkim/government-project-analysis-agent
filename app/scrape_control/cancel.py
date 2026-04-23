"""스크래퍼 subprocess 중단 요청 모듈.

관리자 페이지의 "중단" 버튼, 스케줄러의 취소 로직 등이 공통으로 호출한다.
실제 수집 중단은 비동기적이다 — SIGTERM 을 보낸 뒤 subprocess 가 현재 공고를
마무리하고 종료하면 watcher 스레드(``runner._watch_subprocess``) 가
``ScrapeRun.status='cancelled'`` 로 마감한다. 따라서 이 모듈의 반환값은
"시그널 전송이 성공했는가" 를 의미하며, status 반영은 이후 5초 폴링으로 확인
해야 한다.
"""

from __future__ import annotations

import os
import signal

from loguru import logger

from app.db.models import ScrapeRun
from app.db.session import session_scope


def request_cancel(scrape_run_id: int) -> bool:
    """지정된 ScrapeRun 에 SIGTERM 을 전파해 중단 요청을 보낸다.

    전파 체인 (설계 문서 §6):
        1. 대상 ScrapeRun 을 조회. 존재 안 함 / running 아님 / pid 없음 →
           False 반환(호출자가 UI 상으로 "이미 끝난 실행" 안내).
        2. ``os.getpgid(pid)`` 로 프로세스 그룹 id 조회.
        3. ``os.killpg(pgid, SIGTERM)`` 으로 그룹 전체에 SIGTERM.
           → docker compose v2 가 관리 컨테이너로 signal 을 relay.
           → iris-agent-scraper 컨테이너의 PID 1 (python -m app.cli) 이 수신.
           → cli.py 의 SIGTERM handler 가 공고 마무리 후 깨끗이 종료.

    Args:
        scrape_run_id: 중단할 ScrapeRun PK.

    Returns:
        True  — SIGTERM 전송 시도 성공 (status 변화는 watcher 가 마감할 때까지
                비동기).
        False — 대상 row 가 없거나 이미 종료됨 / pid 없음 / 프로세스 사라짐.
    """
    with session_scope() as session:
        row = session.get(ScrapeRun, scrape_run_id)
        if row is None:
            logger.warning("중단 요청 대상 ScrapeRun 없음: id={}", scrape_run_id)
            return False
        if row.status != "running":
            logger.info(
                "중단 요청 무시(이미 terminal): id={} status={}",
                scrape_run_id, row.status,
            )
            return False
        if row.pid is None:
            logger.warning(
                "중단 요청 무시(pid 미기록): id={} — 다음 startup stale cleanup 에서 정리됩니다.",
                scrape_run_id,
            )
            return False
        target_pid: int = row.pid

    # 여기서부터 DB 트랜잭션 바깥. 실패 시 ScrapeRun 상태는 건드리지 않고
    # False 만 돌려 호출자가 UI 에 알림을 띄울 수 있게 한다.
    try:
        pgid = os.getpgid(target_pid)
    except ProcessLookupError:
        logger.warning(
            "SIGTERM 전송 무시: pid={} 프로세스가 이미 사라졌습니다. "
            "다음 startup stale cleanup 에서 정리됩니다.",
            target_pid,
        )
        return False

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        logger.warning(
            "SIGTERM 전송 무시: pgid={} 프로세스 그룹이 사라졌습니다.",
            pgid,
        )
        return False
    except PermissionError as exc:
        logger.error(
            "SIGTERM 전송 권한 부족: pgid={} ({}). "
            "이 경우는 컨테이너 유저/그룹 설정을 확인하세요.",
            pgid, exc,
        )
        return False

    logger.info(
        "SIGTERM 전송 완료: scrape_run_id={} pid={} pgid={} — "
        "subprocess 가 현재 공고 마무리 후 종료합니다.",
        scrape_run_id, target_pid, pgid,
    )
    return True


__all__ = [
    "request_cancel",
]
