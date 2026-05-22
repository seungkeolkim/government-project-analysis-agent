"""스케줄 job 의 동일 주기 중복 실행 방지 가드 (single-flight). task 00131-2.

배경 (task 00131 — cron 중복 실행 버그):
    이 프로젝트의 정기 작업(공고 수집·daily report·백업·GC) 은 모두 웹 프로세스
    내부 APScheduler ``BackgroundScheduler`` 가 실행한다. subtask 00131-1 이
    flock 으로 '컨테이너당 스케줄러 인스턴스 1개' 를 보장했지만, flock 이 어떤
    이유로 실패하거나 좀비 worker 가 잠시 겹치는 구간에서는 동일 job 이 같은
    trigger 시각에 2~3회 호출될 수 있다.

    본 모듈은 그 경우에도 job 의 **부수효과**(메일 발송·scrape run 생성·백업)
    가 동일 스케줄 주기에 단 1회만 일어나도록 하는 defense-in-depth 가드를
    제공한다.

방식 (DB UNIQUE 제약 기반 claim):
    - job 함수는 실제 작업에 들어가기 전에 :func:`try_claim_schedule_slot` 을
      호출해 ``(job_name, slot_key)`` 로 ``scheduler_job_claims`` 테이블에
      row 를 INSERT 한다.
    - ``slot_key`` 는 현재 시각을 고정 시간창(:data:`DEFAULT_SCHEDULE_SLOT_WINDOW_SECONDS`,
      기본 60초)으로 내림한 버킷 문자열이다. 동일 trigger 시각에 거의 동시에
      발사된 2~3개의 호출은 모두 같은 ``slot_key`` 를 계산한다.
    - ``(job_name, slot_key)`` UNIQUE 제약 덕분에 **먼저 INSERT 에 성공한 단 한
      호출만** claim 을 얻고(True 반환), 나머지는 IntegrityError 로 거절돼
      False 를 받는다. 호출자는 False 면 WARN 로그 후 조용히 return 한다.

설계 결정:
    - **claim 트랜잭션은 짧고 독립적**: claim INSERT 는 자체 ``session_scope``
      안에서 즉시 commit 한 뒤 닫는다. 이후 job 의 본 작업은 별도 세션에서
      수행한다. PROJECT_NOTES L233/L348(00128) 의 'SQLAlchemyJobStore write 가
      session_scope write 트랜잭션과 SQLite 단일 writer 충돌' 교훈에 따라, lock
      성격의 짧은 트랜잭션을 본 작업 트랜잭션보다 **먼저, 따로** 끝낸다.
    - **fail-open**: claim INSERT 가 UNIQUE 위반(IntegrityError) 이 아닌 예기치
      못한 예외로 실패하면 True 를 반환해 job 실행을 허용한다. 가드 자체의
      버그로 정기 작업이 영구히 멈추는 것보다, 드물게 중복이 발생하더라도
      job 이 도는 편이 안전하다 (근본 차단은 00131-1 flock 이 담당).
    - **시간창 60초**: cron 의 최소 단위가 1분이고 interval 모드 최소가 1시간
      이라, 60초 버킷은 어떤 정상 스케줄도 가로막지 않으면서(서로 다른 분에
      발사되는 정상 호출은 다른 버킷) 동일 trigger 시각의 중복 발사(밀리초~초
      단위로 몰림) 는 같은 버킷으로 묶어 걸러낸다.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.db.models import SchedulerJobClaim
from app.db.session import session_scope
from app.timezone import now_utc


# ──────────────────────────────────────────────────────────────
# job_name 상수 — scheduler_job_claims.job_name 에 들어가는 논리적 이름.
# APScheduler 의 job_id 와는 별개이며, 본 상수가 single source of truth 다.
# ──────────────────────────────────────────────────────────────

JOB_NAME_SCHEDULED_SCRAPE: str = "scheduled_scrape"
JOB_NAME_DAILY_REPORT: str = "scheduled_daily_report_job"
JOB_NAME_BACKUP: str = "scheduled_backup_job"
JOB_NAME_GC_ORPHAN: str = "gc_orphan_attachments_job"


# ──────────────────────────────────────────────────────────────
# 시간창 / 보관 기간 상수
# ──────────────────────────────────────────────────────────────

# slot_key 버킷의 폭(초). 이 값으로 현재 시각을 내림해 버킷 경계를 만든다.
# 60초인 이유는 모듈 docstring '설계 결정 — 시간창 60초' 참조.
DEFAULT_SCHEDULE_SLOT_WINDOW_SECONDS: int = 60

# claim row 를 보관하는 기간(초). 이 기간이 지난 row 는 다음 claim 성공 시
# 함께 정리된다. claim row 는 '이 슬롯은 처리됨' 사실만 담는 휘발성 데이터라
# 7일이면 충분히 길다 (운영 이력 용도가 아님).
SCHEDULE_CLAIM_RETENTION_SECONDS: int = 7 * 24 * 60 * 60


def _compute_slot_key(moment: datetime, window_seconds: int) -> str:
    """주어진 시각을 ``window_seconds`` 폭의 버킷으로 내림한 키 문자열을 만든다.

    동일 trigger 시각에 거의 동시에(밀리초~초 단위) 발사된 중복 호출들이 모두
    같은 문자열을 계산하도록, epoch 초를 ``window_seconds`` 로 나눈 몫에 다시
    곱해 버킷 시작 시각을 구한 뒤 ISO-8601 UTC 문자열로 표현한다.

    Args:
        moment: 기준 시각 (tz-aware 권장). naive 면 UTC 로 간주한다.
        window_seconds: 버킷 폭(초). 양의 정수.

    Returns:
        버킷 시작 시각의 ISO-8601 UTC 문자열 (예: ``"2026-05-22T00:00:00+00:00"``).
    """
    # naive 입력은 UTC 로 간주해 비교 안전성을 확보한다.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    epoch_seconds = int(moment.timestamp())
    bucket_start_epoch = (epoch_seconds // window_seconds) * window_seconds
    return datetime.fromtimestamp(bucket_start_epoch, tz=UTC).isoformat()


def try_claim_schedule_slot(
    job_name: str,
    *,
    window_seconds: int = DEFAULT_SCHEDULE_SLOT_WINDOW_SECONDS,
    now: datetime | None = None,
) -> bool:
    """주어진 job 의 현재 스케줄 슬롯에 대한 single-flight claim 을 시도한다.

    동일 trigger 주기(``slot_key`` 버킷)에 대해 **먼저 호출한 단 한 호출만**
    True 를 받는다. 이미 claim 된 슬롯이면 False 를 반환하므로, 호출자는
    False 일 때 WARN 로그 후 조용히 return 해 job 의 부수효과를 건너뛰어야
    한다.

    Args:
        job_name: 가드 대상 job 의 논리적 이름 (``JOB_NAME_*`` 상수 중 하나).
        window_seconds: slot_key 버킷 폭(초). 기본
            :data:`DEFAULT_SCHEDULE_SLOT_WINDOW_SECONDS` (60초).
        now: 기준 시각. None 이면 ``now_utc()`` 를 쓴다. 테스트에서 고정 시각을
            주입해 슬롯 버킷을 결정적으로 만들 때 사용한다.

    Returns:
        True  — 이 호출이 claim 을 획득했다. job 의 본 작업을 수행해도 된다.
        False — 동일 슬롯이 이미 claim 됐다. job 의 부수효과를 건너뛰어야 한다.

    Notes:
        UNIQUE 위반(IntegrityError) 이 아닌 예기치 못한 예외로 claim INSERT 가
        실패하면 fail-open 정책에 따라 True 를 반환한다 (모듈 docstring 참조).
    """
    moment = now if now is not None else now_utc()
    slot_key = _compute_slot_key(moment, window_seconds)
    process_id = os.getpid()

    try:
        # claim INSERT 는 자체 트랜잭션에서 즉시 commit. 본 작업 트랜잭션과
        # 분리해 SQLite 단일 writer 충돌을 피한다 (모듈 docstring 설계 결정).
        with session_scope() as session:
            session.add(
                SchedulerJobClaim(
                    job_name=job_name,
                    slot_key=slot_key,
                    claimed_at=moment,
                    claimed_by_pid=process_id,
                )
            )
    except IntegrityError:
        # (job_name, slot_key) UNIQUE 위반 — 동일 슬롯을 다른 호출이 이미
        # 선점했다. 이 호출은 job 을 건너뛰어야 한다.
        logger.warning(
            "스케줄 job 중복 실행 방지 — 동일 주기를 다른 호출이 선점함: "
            "job_name={} slot_key={} pid={}",
            job_name, slot_key, process_id,
        )
        return False
    except Exception as exc:
        # UNIQUE 위반이 아닌 예기치 못한 예외 — fail-open. 가드 버그로 정기
        # 작업이 영구히 멈추는 것보다 드문 중복을 감수하는 편이 안전하다.
        logger.exception(
            "스케줄 job claim 중 예기치 못한 예외 — fail-open 으로 job 실행을 "
            "허용한다: job_name={} ({}: {})",
            job_name, type(exc).__name__, exc,
        )
        return True

    logger.info(
        "스케줄 job claim 획득 — 이 호출이 job 을 실행한다: "
        "job_name={} slot_key={} pid={}",
        job_name, slot_key, process_id,
    )
    # claim 에 성공한 호출만 오래된 row 정리를 떠맡는다 (best-effort).
    _purge_stale_claims_quietly(reference=moment)
    return True


def _purge_stale_claims_quietly(*, reference: datetime) -> None:
    """보관 기간이 지난 ``scheduler_job_claims`` row 를 정리한다 (best-effort).

    claim row 는 매 job 발사마다 누적되므로 방치하면 무한히 늘어난다. claim
    획득에 성공한 호출이 이 정리를 떠맡는다. 정리 자체가 실패해도 job 실행
    흐름을 막지 않도록 모든 예외를 swallow 한다.

    Args:
        reference: 보관 기간 계산의 기준 시각. ``reference -
            SCHEDULE_CLAIM_RETENTION_SECONDS`` 이전의 row 를 삭제한다.
    """
    try:
        cutoff = reference - timedelta(seconds=SCHEDULE_CLAIM_RETENTION_SECONDS)
        with session_scope() as session:
            result = session.execute(
                delete(SchedulerJobClaim).where(
                    SchedulerJobClaim.claimed_at < cutoff
                )
            )
        deleted_count = result.rowcount or 0
        if deleted_count:
            logger.info(
                "오래된 스케줄 job claim {}건 정리 (cutoff={}).",
                deleted_count, cutoff.isoformat(),
            )
    except Exception as exc:
        # 정리는 부차적 작업 — 실패해도 job 실행을 막지 않는다.
        logger.warning(
            "스케줄 job claim 정리 중 예외(무시): {}: {}",
            type(exc).__name__, exc,
        )


__all__ = [
    "DEFAULT_SCHEDULE_SLOT_WINDOW_SECONDS",
    "JOB_NAME_BACKUP",
    "JOB_NAME_DAILY_REPORT",
    "JOB_NAME_GC_ORPHAN",
    "JOB_NAME_SCHEDULED_SCRAPE",
    "SCHEDULE_CLAIM_RETENTION_SECONDS",
    "try_claim_schedule_slot",
]
