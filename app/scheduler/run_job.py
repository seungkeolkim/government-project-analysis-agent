"""cron 데몬이 쉘에서 직접 호출하는 단일 스케줄 작업 CLI 진입점 (task 00155-1).

배경 (error.log 18b0d4249dbf 근본 원인):
    2026-06-04 08:00 KST Daily Report 자동 발송 도중, 메인 커넥션이 발송 루프에서
    EmailDailyReportRun / last_sent_at 등을 write 하는 사이에 APScheduler 의 별도
    스레드가 ``scheduler_jobs`` 의 ``next_run_time`` 을 UPDATE(jobstore.py:461)
    하려다 SQLite ``database is locked`` 에 걸렸다. 이 예외가 ``_main_loop`` 밖으로
    전파되어 ``Exception in thread APScheduler`` 로 **스케줄러 스레드 자체가 사망**
    했고, 그 결과 이후 공고 수집·메일 발송·DB 백업·고아 첨부 GC 등 APScheduler 가
    돌리던 **모든 스케줄 잡이 영구 정지**했다. 컨테이너를 재기동하지 않는 한 다음
    주기가 영영 오지 않는 single-point-of-failure 구조였다.

    핵심 교훈: "SW 적으로 cron 을 흉내내는"(웹 프로세스 내부 단일 스레드 스케줄러)
    구조는 그 스레드 하나가 죽으면 전 스케줄이 멈춘다. OS 레벨 cron 데몬은 각 잡을
    매 주기 **독립 프로세스**로 새로 띄우므로, 한 번의 잡 실패가 다음 주기·다른
    잡에 전파되지 않는다. 본 모듈은 그 cron 데몬이 쉘에서 직접 호출할 수 있는
    작업 진입점을 제공한다.

설계:
    - ``python -m app.scheduler.run_job <job>`` 형태로 호출한다.
      job ∈ {scrape, backup, daily-report, gc}. scrape 는 ``--sources a,b`` 옵션을
      받는다(빈 값이면 enabled 전체 수집).
    - 각 서브커맨드는 기존 서비스 함수를 **그대로** 재사용한다:
        scrape       → app.scrape_control.start_scrape_run(trigger='scheduled')
        backup       → app.backup.service.run_backup(trigger=scheduled)
        daily-report → collect_recipient_emails + build_transport_from_settings
                       + prepare_and_send_daily_report
        gc           → app.scrape_control.orphan_gc.run_gc(dry_run=False)
      즉 :mod:`app.scheduler.job_runner` 의 4개 잡 함수가 호출하던 서비스 경로와
      동일하다. 다만 cron 은 한 주기에 한 번만 잡을 발사하므로 APScheduler 시절의
      single-flight 가드(``try_claim_schedule_slot`` / jobstore 결합)는 **불필요**해
      제거했다. claim 결합이 없으니 ``database is locked`` 의 원인이었던 jobstore
      write 자체가 사라진다.
    - 종료 코드: 정상 0 / 실패 비0. 잘못된·누락된 서브커맨드는 사용법을 출력하고
      비0 으로 종료한다(cron / 모니터링이 실패를 감지할 수 있도록).
    - 로깅은 웹 컨벤션과 동일하게 loguru + :func:`configure_logging` 1회 호출.
    - 시각/설정 접근은 :mod:`app.timezone`, :func:`app.config.get_settings` 헬퍼
      경유.

비고:
    이 subtask(00155-1)는 APScheduler 를 아직 제거하지 않는다 — cron 이 호출할
    진입점만 추가한다. APScheduler 제거와 crontab 설치는 후속 subtask 가 맡는다.
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from app.config import get_settings
from app.logging_setup import configure_logging
from app.scrape_control import (
    ComposeEnvironmentError,
    ScrapeAlreadyRunningError,
    start_scrape_run,
    validate_host_project_dir,
)
from app.scrape_control.orphan_gc import run_gc

# ──────────────────────────────────────────────────────────────
# 종료 코드 상수
# ──────────────────────────────────────────────────────────────

# 정상 완료(또는 "이미 실행 중/게이트 차단" 같은 양성 skip).
EXIT_SUCCESS: int = 0

# 잡 실행 중 예기치 못한 실패. cron / 운영 모니터링이 비0 으로 감지한다.
EXIT_FAILURE: int = 1

# HOST_PROJECT_DIR 미설정 등 환경 자체가 비정상(stale 프로세스 의심).
# APScheduler 시절 scheduled_scrape 의 ``os._exit(2)`` fail-fast 의도를 CLI
# 문맥에 맞게 "프로세스 즉시 종료" 대신 "비0 종료 코드"로 대체한다(task 00148).
EXIT_ENVIRONMENT_INVALID: int = 2

# 잘못된/누락된 서브커맨드. argparse 의 관례(SystemExit(2))와 동일하게 맞춘다.
EXIT_USAGE_ERROR: int = 2


# ──────────────────────────────────────────────────────────────
# 서브커맨드 핸들러 — 각각 종료 코드(int)를 반환한다.
# ──────────────────────────────────────────────────────────────


def _parse_sources_option(raw_sources: str) -> list[str]:
    """``--sources`` 옵션 문자열을 source id 리스트로 파싱한다.

    Args:
        raw_sources: 콤마로 구분된 source id 문자열(예: ``"iris,ntis"``). 빈 문자열
            이면 빈 리스트를 반환해, ``start_scrape_run`` 이 sources.yaml 의
            enabled 전체를 수집하도록 한다.

    Returns:
        공백이 제거되고 빈 토큰이 걸러진 source id 리스트.
    """
    return [token.strip() for token in raw_sources.split(",") if token.strip()]


def run_scrape(active_sources: list[str]) -> int:
    """공고 수집 잡을 1회 실행한다(cron 트리거 등가).

    :func:`app.scheduler.job_runner.scheduled_scrape` 와 동일한 서비스 경로
    (``start_scrape_run(trigger='scheduled')``)를 호출하되, single-flight claim
    없이 동작한다. HOST_PROJECT_DIR fail-fast 의도는 유지하되 CLI 문맥에 맞게
    ``os._exit`` 대신 비0 종료 코드로 처리한다.

    Args:
        active_sources: 이번 실행에 한정할 source id 목록. 빈 리스트면 enabled
            전체 수집.

    Returns:
        종료 코드. 정상 기동 또는 "이미 실행 중" 양성 skip 은 0, 환경 비정상은
        :data:`EXIT_ENVIRONMENT_INVALID`, 그 외 실패는 :data:`EXIT_FAILURE`.
    """
    # task 00148 — 잡 진입 즉시 HOST_PROJECT_DIR 검증. cron 은 환경을 비우고
    # 띄우므로, crontab 래퍼가 .env 를 제대로 로딩하지 못했다면 여기서 걸린다.
    # APScheduler 시절의 ``os._exit(2)`` 자기-종료 대신, CLI 는 비0 종료 코드를
    # 돌려줘 cron 이 이번 주기 실패로 기록하게 한다(프로세스를 강제로 죽이지
    # 않아 로깅/정리가 정상 수행됨).
    try:
        validate_host_project_dir()
    except ComposeEnvironmentError as exc:
        logger.critical(
            "스케줄 수집 진입 시 HOST_PROJECT_DIR 검증 실패 — 환경이 비정상이다. "
            "crontab 래퍼의 환경 로딩(.env)을 확인하라. error={}",
            exc,
        )
        return EXIT_ENVIRONMENT_INVALID

    try:
        result = start_scrape_run(active_sources, trigger="scheduled")
        logger.info(
            "스케줄 수집 기동 완료: scrape_run_id={} pid={} active_sources={}",
            result.scrape_run_id,
            result.pid,
            active_sources or "(전체)",
        )
        return EXIT_SUCCESS
    except ScrapeAlreadyRunningError as exc:
        # 이미 다른 수집이 진행 중이면 이번 주기는 건너뛴다 — 이는 정상적인
        # 양성 skip 이므로 종료 코드 0(실패 아님).
        logger.warning(
            "스케줄 수집 건너뜀 — 이미 다른 수집 실행 중: scrape_run_id={} "
            "trigger={} ({}: {})",
            exc.running_run_id,
            exc.running_trigger,
            type(exc).__name__,
            exc,
        )
        return EXIT_SUCCESS
    except ComposeEnvironmentError as exc:
        # docker compose 환경 문제(운영자 조치 필요). 비0 으로 실패를 알린다.
        logger.error("스케줄 수집 환경 오류(운영자 조치 필요): {}", exc)
        return EXIT_FAILURE
    except Exception as exc:
        logger.exception(
            "스케줄 수집 중 예기치 못한 예외: {}: {}", type(exc).__name__, exc
        )
        return EXIT_FAILURE


def run_backup() -> int:
    """DB 파일 백업 잡을 1회 실행한다(cron 트리거 등가).

    :func:`app.scheduler.job_runner.scheduled_backup_job` 와 동일하게
    ``app.backup.service.run_backup(trigger=scheduled)`` 를 호출한다. 순환 import
    방지를 위해 함수 내부에서 lazy import 한다(job_runner 와 동일 정책).

    Returns:
        정상 완료 시 0, 예외 발생 시 :data:`EXIT_FAILURE`.
    """
    try:
        # 순환 import 방지를 위해 함수 내부 lazy import.
        from app.backup.constants import BACKUP_TRIGGER_SCHEDULED
        from app.backup.service import run_backup as run_backup_service

        history = run_backup_service(trigger=BACKUP_TRIGGER_SCHEDULED)
        logger.info(
            "스케줄 백업 완료: id={} success={} backup_files={} duration={}s",
            history.id,
            history.success,
            history.backup_files,
            history.duration_seconds,
        )
        return EXIT_SUCCESS
    except Exception as exc:
        logger.exception(
            "스케줄 백업 중 예기치 못한 예외: {}: {}", type(exc).__name__, exc
        )
        return EXIT_FAILURE


def run_daily_report() -> int:
    """Daily Report 자동 발송 잡을 1회 실행한다(cron 트리거 등가).

    :func:`app.scheduler.job_runner.scheduled_daily_report_job` 와 동일한 흐름
    (``collect_recipient_emails`` → ``build_transport_from_settings`` →
    ``prepare_and_send_daily_report``)을 single-flight claim 없이 수행한다. 잡
    함수가 쓰던 lazy import 정책을 그대로 따른다.

    Returns:
        정상 발송 또는 게이트 비활성(양성 skip) 시 0, 예외 발생 시
        :data:`EXIT_FAILURE`.
    """
    try:
        # 순환 import 방지를 위해 함수 내부 lazy import(job_runner 와 동일 패턴).
        from app.backup.service import get_setting
        from app.db.session import session_scope
        from app.email.constants import (
            DEFAULT_EMAIL_MAX_RETRY_COUNT,
            SETTING_KEY_EMAIL_MAX_RETRY_COUNT,
        )
        from app.email.daily_report import (
            TRIGGER_SCHEDULED,
            DailyReportRequest,
            collect_recipient_emails,
            prepare_and_send_daily_report,
        )
        from app.email.gate import EmailSendingDisabledError
        from app.email.transport.factory import build_transport_from_settings

        with session_scope() as session:
            # 발송 대상 수집 — email 정상 + email_subscribed=True 전체 사용자.
            # 빈 list 가 와도 prepare_and_send 가 "수신자 0 → FAILED" 분기로
            # 처리한다.
            recipients = collect_recipient_emails(session)

            # max_retry_count 는 SystemSetting 에서 로드. 파싱 실패 시 DEFAULT.
            raw_max_retry = get_setting(session, SETTING_KEY_EMAIL_MAX_RETRY_COUNT)
            try:
                max_retry_count = (
                    int(raw_max_retry)
                    if raw_max_retry not in (None, "")
                    else DEFAULT_EMAIL_MAX_RETRY_COUNT
                )
            except (TypeError, ValueError):
                max_retry_count = DEFAULT_EMAIL_MAX_RETRY_COUNT

            transport = build_transport_from_settings(session)

            request = DailyReportRequest(
                trigger=TRIGGER_SCHEDULED,
                recipients=recipients,
                requested_by_user_id=None,
            )
            try:
                result = prepare_and_send_daily_report(
                    request,
                    session=session,
                    transport=transport,
                    max_retry_count=max_retry_count,
                )
            except EmailSendingDisabledError as exc:
                # 게이트 차단 — prepare_and_send 가 이미 run row 를 FAILED 로
                # commit 한 상태다. 운영자 설정 문제이므로 잡 자체는 양성 skip
                # (종료 코드 0)으로 처리하고, 게이트 활성화 시 다음 주기 자동 재개.
                logger.warning(
                    "Daily report 스케줄 발송 건너뜀 — 메일 전송 게이트 비활성: {}",
                    exc,
                )
                return EXIT_SUCCESS

        logger.info(
            "Daily report 스케줄 발송 완료: run_id={} status={!r} snapshot_count={} "
            "recipient_count={} success={} failure={}",
            result.run_id,
            result.status.value if hasattr(result.status, "value") else str(result.status),
            result.snapshot_count,
            result.recipient_count,
            result.success_count,
            result.failure_count,
        )
        return EXIT_SUCCESS
    except Exception as exc:
        logger.exception(
            "Daily report 스케줄 발송 중 예기치 못한 예외: {}: {}",
            type(exc).__name__,
            exc,
        )
        return EXIT_FAILURE


def run_gc_job() -> int:
    """고아 첨부 파일 GC 잡을 1회 실행한다(cron 트리거 등가).

    :func:`app.scheduler.job_runner.gc_orphan_attachments_job` 와 동일하게
    ``app.scrape_control.orphan_gc.run_gc(dry_run=False)`` 를 호출한다.

    Returns:
        정상 완료 또는 "수집 진행 중이라 거부됨"(양성 skip) 시 0, 예외 발생 시
        :data:`EXIT_FAILURE`.
    """
    try:
        report = run_gc(dry_run=False)
        if report.skipped_due_to_running_scrape_run:
            # ScrapeRun running 으로 GC 가 자체 거부됨. 다음 주기에 재시도되므로
            # 양성 skip(종료 코드 0)으로 처리한다.
            logger.warning(
                "GC 자동 실행 건너뜀 — ScrapeRun running 으로 거부됨. "
                "다음 cron 주기에 재시도됩니다.",
            )
            return EXIT_SUCCESS
        logger.info(
            "GC 자동 실행 완료: scanned_root={} disk_files={} db_paths={} "
            "deleted={} failed={} removed_dirs={} freed_bytes={}",
            report.scanned_root,
            report.disk_file_count,
            report.db_referenced_count,
            report.deleted_count,
            len(report.deletion_failed),
            report.removed_directory_count,
            report.total_orphan_bytes,
        )
        return EXIT_SUCCESS
    except Exception as exc:
        logger.exception(
            "GC 자동 실행 중 예기치 못한 예외: {}: {}", type(exc).__name__, exc
        )
        return EXIT_FAILURE


# ──────────────────────────────────────────────────────────────
# 인자 파서 / 진입점
# ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """cron 작업 CLI 의 argparse 파서를 구성한다.

    Returns:
        scrape/backup/daily-report/gc 4개 서브커맨드를 가진 파서. scrape 만
        ``--sources`` 옵션을 받는다.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.scheduler.run_job",
        description=(
            "cron 데몬이 호출하는 단일 스케줄 작업 실행기. "
            "한 번에 하나의 잡(scrape/backup/daily-report/gc)을 1회 실행한다."
        ),
    )
    # dest='command' — 서브커맨드 미지정 시 None 으로 들어와 사용법을 출력한다.
    subparsers = parser.add_subparsers(dest="command", metavar="<job>")

    scrape_parser = subparsers.add_parser(
        "scrape",
        help="공고 수집을 1회 실행한다(start_scrape_run, trigger=scheduled).",
    )
    scrape_parser.add_argument(
        "--sources",
        default="",
        metavar="a,b,c",
        help=(
            "콤마로 구분된 source id 목록. 비우면 sources.yaml 의 enabled 전체를 "
            "수집한다(예: --sources iris,ntis)."
        ),
    )

    subparsers.add_parser(
        "backup",
        help="DB 파일 백업을 1회 실행한다(run_backup, trigger=scheduled).",
    )
    subparsers.add_parser(
        "daily-report",
        help="Daily Report 자동 발송을 1회 실행한다(prepare_and_send_daily_report).",
    )
    subparsers.add_parser(
        "gc",
        help="고아 첨부 파일 GC 를 1회 실행한다(run_gc, dry_run=False).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. 서브커맨드를 디스패치하고 종료 코드를 반환한다.

    Args:
        argv: 테스트 주입용 인자 리스트. None 이면 ``sys.argv[1:]`` 를 사용한다.

    Returns:
        잡 실행 종료 코드. 잘못된/누락된 서브커맨드는 사용법을 출력하고
        :data:`EXIT_USAGE_ERROR` 를 반환한다.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse 는 알 수 없는 서브커맨드/옵션에서 stderr 에 에러+사용법을
        # 출력하고 SystemExit(2) 를 던진다. main 이 int 를 반환하도록 코드만
        # 회수한다(프로세스 전체를 즉시 죽이지 않음).
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE_ERROR

    # 서브커맨드 누락 — 사용법을 출력하고 비0 으로 종료한다.
    if not args.command:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE_ERROR

    # 로깅은 웹/CLI 컨벤션대로 잡 실행 직전 1회만 초기화한다. 시각/설정 접근은
    # get_settings 헬퍼 경유.
    configure_logging(get_settings())

    if args.command == "scrape":
        return run_scrape(_parse_sources_option(args.sources))
    if args.command == "backup":
        return run_backup()
    if args.command == "daily-report":
        return run_daily_report()
    if args.command == "gc":
        return run_gc_job()

    # add_subparsers 의 choices 밖은 argparse 가 위에서 이미 걸러내므로 도달하지
    # 않는다. 방어적으로 사용법을 출력한다.
    parser.print_usage(sys.stderr)
    return EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
