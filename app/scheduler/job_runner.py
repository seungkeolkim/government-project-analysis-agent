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
from app.scrape_control.orphan_gc import run_gc

# task 00131-2 — 스케줄 job 의 동일 주기 중복 실행 방지 가드(single-flight).
# 동일 trigger 시각에 job 이 2~3회 호출돼도 부수효과는 1회만 일어나도록,
# 각 잡 함수 진입부에서 claim 을 시도하고 실패 시 조용히 return 한다.
from app.scheduler.job_guard import (
    JOB_NAME_BACKUP,
    JOB_NAME_DAILY_REPORT,
    JOB_NAME_GC_ORPHAN,
    JOB_NAME_SCHEDULED_SCRAPE,
    try_claim_schedule_slot,
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

    중복 실행 방지 (task 00131-2):
        함수 진입 시 :func:`try_claim_schedule_slot` 으로 single-flight claim 을
        시도한다. 동일 trigger 주기에 다른 호출이 이미 claim 했으면 WARN 로그
        후 조용히 return 한다 — scrape run 이 1건만 생성되도록 보장한다.
        ``start_scrape_run`` 의 기존 running-lock 과는 별개 가드로, 순차로 빠르게
        끝나는 중복 trigger 까지 막는다.

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

    # task 00131-2 — 동일 trigger 주기의 중복 실행 방지. claim 에 실패하면
    # 다른 호출이 이미 이번 주기의 수집을 맡았다는 뜻이므로 조용히 return.
    if not try_claim_schedule_slot(JOB_NAME_SCHEDULED_SCRAPE):
        logger.warning(
            "스케줄 수집 건너뜀 — 동일 주기에 다른 호출이 이미 수집을 "
            "시작했다 (중복 실행 방지, active_sources={}).",
            normalized or "(전체)",
        )
        return

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


def gc_orphan_attachments_job() -> None:
    """APScheduler 가 호출하는 고아 첨부 파일 GC 진입점 (Phase 5a / task 00041-5).

    APScheduler jobstore 에 ``app.scheduler.job_runner.gc_orphan_attachments_job``
    경로로 pickle 되므로, 이 함수의 모듈 경로·이름은 **절대 변경 금지**.

    동작:
        - ``app.scrape_control.orphan_gc.run_gc(dry_run=False)`` 를 호출한다.
          자동 실행이므로 dry_run=False (실제 삭제) 가 기본 — 운영자가 cron 으로
          등록할 때 \"매일 새벽 청소\" 의도를 그대로 수행한다.
        - ``ScrapeRun.status='running'`` 이 있으면 ``run_gc`` 가 자체적으로
          가드해 GC 를 거부한다 (skipped_due_to_running_scrape_run=True). 이
          경우 다음 주기에 다시 시도한다 (cron 자체는 유지).
        - 인자 없음 — APScheduler 가 pickle 하는 args 가 없어 jobstore 에서
          \"호출 시 어떤 source 를 GC 할지\" 같은 부수 상태가 따라가지 않는다.

    중복 실행 방지 (task 00131-2):
        함수 진입 시 :func:`try_claim_schedule_slot` 으로 single-flight claim 을
        시도한다. 동일 trigger 주기에 다른 호출이 이미 claim 했으면 WARN 로그
        후 조용히 return 한다 — GC 가 1회만 수행되도록 보장한다.

    예외 처리 정책 (scheduled_scrape 와 동일):
        - 모든 예외를 swallow + ``logger.exception`` — APScheduler 스레드가
          조용히 죽지 않게 한다. cron 은 다음 주기에 다시 실행된다.
    """
    # task 00131-2 — 동일 trigger 주기의 중복 실행 방지.
    if not try_claim_schedule_slot(JOB_NAME_GC_ORPHAN):
        logger.warning(
            "GC 자동 실행 건너뜀 — 동일 주기에 다른 호출이 이미 GC 를 "
            "시작했다 (중복 실행 방지).",
        )
        return

    try:
        report = run_gc(dry_run=False)
        if report.skipped_due_to_running_scrape_run:
            logger.warning(
                "GC 자동 실행 건너뜀 — ScrapeRun running 으로 거부됨. "
                "다음 cron 주기에 재시도됩니다.",
            )
            return
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
    except Exception as exc:
        # APScheduler 가 잡 예외를 전파하게 두면 일부 버전에서 이후 주기가
        # 스킵될 수 있어, 안전하게 삼킨다 (scheduled_scrape 와 동일 정책).
        logger.exception(
            "GC 자동 실행 중 예기치 못한 예외(다음 주기로 이월): {}: {}",
            type(exc).__name__, exc,
        )


def scheduled_backup_job() -> None:
    """APScheduler 가 호출하는 DB 백업 진입점 (task 00094-2).

    APScheduler jobstore 에 ``app.scheduler.job_runner.scheduled_backup_job``
    경로로 pickle 되므로, 이 함수의 모듈 경로·이름은 **절대 변경 금지**.

    설계:
        - 인자 없음 — pickle 에 args 를 저장하면 운영 중 max_count 변경이
          즉시 반영되지 않는다. run_backup 이 실행 시점에 DB 에서 설정을 읽는다.
        - ``app.backup.service`` 를 함수 안에서 lazy import — 순환 import 방지.
        - 모든 예외를 swallow + ``logger.exception`` — APScheduler 스레드가
          조용히 죽지 않게 한다 (``scheduled_scrape`` 와 동일 정책).

    중복 실행 방지 (task 00131-2):
        함수 진입 시 :func:`try_claim_schedule_slot` 으로 single-flight claim 을
        시도한다. 동일 trigger 주기에 다른 호출이 이미 claim 했으면 WARN 로그
        후 조용히 return 한다 — 백업이 1회만 수행되도록 보장한다.
    """
    # task 00131-2 — 동일 trigger 주기의 중복 실행 방지.
    if not try_claim_schedule_slot(JOB_NAME_BACKUP):
        logger.warning(
            "스케줄 백업 건너뜀 — 동일 주기에 다른 호출이 이미 백업을 "
            "시작했다 (중복 실행 방지).",
        )
        return

    try:
        # 순환 import 방지를 위해 함수 내부 lazy import 사용.
        from app.backup.constants import BACKUP_TRIGGER_SCHEDULED
        from app.backup.service import run_backup

        history = run_backup(trigger=BACKUP_TRIGGER_SCHEDULED)
        logger.info(
            "스케줄 백업 완료: id={} success={} backup_files={} duration={}s",
            history.id,
            history.success,
            history.backup_files,
            history.duration_seconds,
        )
    except Exception as exc:
        logger.exception(
            "스케줄 백업 중 예기치 못한 예외(다음 주기로 이월): {}: {}",
            type(exc).__name__, exc,
        )


def scheduled_daily_report_job() -> None:
    """APScheduler 가 호출하는 Daily Report 자동 발송 진입점 (task 00125-7).

    APScheduler jobstore 에 ``app.scheduler.job_runner.scheduled_daily_report_job``
    경로로 pickle 되므로, 이 함수의 모듈 경로·이름은 **절대 변경 금지**.

    설계:
        - 인자 없음 — pickle 안정성 + 운영 중 SystemSetting 변경이 즉시 반영
          (잡 args 에 박혀 있지 않도록).
        - ``app.email`` / ``app.db.session`` 모듈은 함수 안에서 lazy import —
          순환 import 방지 (디자인 노트 §0-3 컨벤션과 동일 정책).
        - 모든 예외를 swallow + ``logger.exception`` — APScheduler 스레드가
          조용히 죽지 않게 한다 (``scheduled_scrape`` / ``scheduled_backup_job``
          과 동일 정책).

    동작 흐름:
        1. ``session_scope()`` 로 ORM 세션 컨텍스트 진입.
        2. ``collect_admin_recipient_emails(session)`` 로 발송 대상 admin 수집.
        3. ``build_transport_from_settings(session)`` 으로 EmailTransport 빌드.
        4. ``DailyReportRequest(trigger='scheduled', ...)`` 생성.
        5. ``prepare_and_send_daily_report(...)`` 호출 — 게이트 / aggregate /
           본문 / 발송 / last_sent_at 정책은 모두 그쪽 함수가 책임진다.
        6. ``EmailSendingDisabledError`` (게이트 차단) 는 \"운영자 설정 문제\" 라
           WARN 로그만 — EmailDailyReportRun row 는 prepare_and_send 가 미리
           FAILED 로 commit 해 두므로 이력은 남는다.

    중복 실행 방지 (task 00131-2):
        함수 진입 시 :func:`try_claim_schedule_slot` 으로 single-flight claim 을
        시도한다. 동일 예정 시각에 여러 스케줄러 인스턴스가 발사돼도, claim 에
        성공한 단 한 호출만 ``prepare_and_send_daily_report`` 에 도달한다 —
        2026-05-22 09:00 KST 에 3통이 발송된 증상을 막는다. claim 은 발송 루프와
        last_sent_at 갱신보다 앞서 수행되므로, 중복 호출은 메일을 보내기 전에
        걸러진다.
    """
    # task 00131-2 — 동일 예정 시각의 중복 발송 방지. claim 에 실패하면 다른
    # 호출이 이미 이번 주기의 발송을 맡았다는 뜻이므로 조용히 return.
    if not try_claim_schedule_slot(JOB_NAME_DAILY_REPORT):
        logger.warning(
            "Daily report 스케줄 발송 건너뜀 — 동일 예정 시각에 다른 호출이 "
            "이미 발송을 시작했다 (중복 발송 방지).",
        )
        return

    try:
        # 순환 import 방지를 위해 함수 내부 lazy import 사용 (scheduled_backup_job
        # 과 동일 패턴).
        from app.db.session import session_scope
        from app.email.constants import (
            DEFAULT_EMAIL_MAX_RETRY_COUNT,
            SETTING_KEY_EMAIL_MAX_RETRY_COUNT,
        )
        from app.email.daily_report import (
            TRIGGER_SCHEDULED,
            DailyReportRequest,
            collect_admin_recipient_emails,
            prepare_and_send_daily_report,
        )
        from app.email.gate import EmailSendingDisabledError
        from app.email.transport.factory import build_transport_from_settings
        from app.backup.service import get_setting

        with session_scope() as session:
            # 발송 대상 admin 수집 — design note §5 정책 (is_admin AND email AND
            # email_subscribed). 빈 list 가 와도 prepare_and_send 가
            # \"수신자 0 → FAILED\" 분기로 처리한다.
            recipients = collect_admin_recipient_emails(session)

            # max_retry_count 는 SystemSetting 에서 로드 — sender / forwarding 의
            # 동일 패턴. 파싱 실패 시 DEFAULT 로 안전 fallback.
            raw_max_retry = get_setting(session, SETTING_KEY_EMAIL_MAX_RETRY_COUNT)
            try:
                max_retry_count = (
                    int(raw_max_retry) if raw_max_retry not in (None, "") else DEFAULT_EMAIL_MAX_RETRY_COUNT
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
                # 게이트 차단 — prepare_and_send_daily_report 가 이미
                # EmailDailyReportRun row 를 FAILED 로 commit 한 상태다. cron
                # 자체는 유지해 게이트 활성화 시 다음 주기에 자동 재개.
                logger.warning(
                    "Daily report 스케줄 발송 건너뜀 — 메일 전송 게이트 비활성: {}",
                    exc,
                )
                return

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
    except Exception as exc:
        # APScheduler 가 잡 예외를 전파하게 두면 일부 버전에서 이후 주기가
        # 스킵될 수 있어, 안전하게 삼킨다 (scheduled_scrape 와 동일 정책).
        logger.exception(
            "Daily report 스케줄 발송 중 예기치 못한 예외(다음 주기로 이월): {}: {}",
            type(exc).__name__, exc,
        )


__all__ = [
    "gc_orphan_attachments_job",
    "scheduled_backup_job",
    "scheduled_daily_report_job",
    "scheduled_scrape",
]
