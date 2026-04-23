"""APScheduler 가 호출하는 top-level 잡 함수.

**이 파일의 모듈 경로와 함수 이름은 절대 변경하지 않는다.** APScheduler 의
SQLAlchemyJobStore 가 잡을 pickle 할 때 ``(module, qualname)`` 조합을 저장하는
데, 이를 바꾸면 재기동 후 저장된 잡을 복원할 수 없다. 리팩터가 필요하면 별도
마이그레이션(기존 scheduler_jobs 테이블 정리 + 재등록)을 동반해야 한다.

guidance 핵심:
    - 잡 함수는 **top-level 함수** (closure 금지 — pickle 불가).
    - ``start_scrape_run`` 이 :class:`ScrapeAlreadyRunningError` (RuntimeError
      서브클래스) 를 던지면 **swallow + WARN 로그** 만 — 스케줄 자체는 멈추지
      않고 다음 주기를 기다린다.
    - 기타 예외도 swallow 해 스케줄러 스레드를 보호한다 (APScheduler 는 job
      예외 시 해당 trigger 를 비활성화하지 않고 계속 돌리지만, 에러 조사를 위해
      logger.exception 으로 남긴다).
"""

from __future__ import annotations

from loguru import logger

from app.scrape_control import (
    ComposeEnvironmentError,
    ScrapeAlreadyRunningError,
    start_scrape_run,
)


def scheduled_scrape(active_sources: list[str]) -> None:
    """스케줄 트리거로 호출되는 진입점.

    APScheduler jobstore 에 ``app.scheduler.job_runner.scheduled_scrape`` 경로로
    pickle 되므로, 이 함수의 모듈 경로·이름은 **절대 변경 금지**.

    Args:
        active_sources: 이번 실행에 한정할 source id 목록. 빈 리스트면
                        sources.yaml 의 enabled 전체 실행. APScheduler jobstore
                        에 pickle 되므로 리스트/문자열 등 primitive 만 사용한다.

    Returns:
        None. 실행 결과는 ScrapeRun 행으로 DB 에 남고, UI 에서 확인한다.

    예외 처리 정책:
        - :class:`ScrapeAlreadyRunningError` → WARN 로그 후 swallow.
          (이미 다른 수집이 돌고 있으면 이번 주기는 건너뛰고 다음 주기 대기.)
        - :class:`ComposeEnvironmentError` → ERROR 로그 후 swallow.
          (HOST_PROJECT_DIR 등 운영자 조치 필요 — 스케줄은 유지해 자동 재시도.)
        - 그 외 ``Exception`` → ``logger.exception`` 후 swallow.
          APScheduler 가 잡 예외를 전파하게 두면 일부 버전에서 이후 주기가
          스킵될 수 있어, 안전하게 삼킨다.
    """
    normalized = list(active_sources or [])
    try:
        result = start_scrape_run(normalized, trigger="scheduled")
        logger.info(
            "스케줄 수집 기동 완료: scrape_run_id={} pid={} active_sources={}",
            result.scrape_run_id, result.pid, normalized or "(전체)",
        )
    except ScrapeAlreadyRunningError as exc:
        # guidance: start_scrape_subprocess 가 RuntimeError(중복 실행)을 던질 때
        # 잡 함수는 로깅만 하고 swallow(스케줄 잡이 중단되지 않게).
        logger.warning(
            "스케줄 수집 건너뜀 — 이미 다른 수집 실행 중: scrape_run_id={} trigger={} ({}: {})",
            exc.running_run_id, exc.running_trigger, type(exc).__name__, exc,
        )
    except ComposeEnvironmentError as exc:
        # 운영자가 .env (HOST_PROJECT_DIR) 를 설정해야 해결되는 환경 문제.
        # 스케줄 자체는 유지해 설정이 고쳐진 뒤 자동 복귀하도록.
        logger.error(
            "스케줄 수집 환경 오류(스케줄 유지, 다음 주기에 재시도): {}", exc,
        )
    except Exception as exc:
        # 나머지 예외도 swallow — APScheduler 스레드가 조용히 죽지 않게 한다.
        logger.exception(
            "스케줄 수집 중 예기치 못한 예외(다음 주기로 이월): {}: {}",
            type(exc).__name__, exc,
        )


__all__ = [
    "scheduled_scrape",
]
