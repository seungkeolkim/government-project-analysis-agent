"""재시도 + EmailSendRun 이력 기록 sender (Phase A-1 / task 00104-8).

설계 근거:
    docs/phase_a1_design_note.md §4-1 (attempt_count / error_message 시맨틱),
    §4-4 (트랜잭션 경계), §7 (UTC 저장 컨벤션) + 첨부 phase_a1_prompt.md 의
    ``app/email/sender.py`` 의사 시그니처.

본 모듈은 단일 함수 ``send_with_retry`` 만 노출한다. transport
(``EmailTransport`` 구현체) 위에 다음 두 가지 책임을 얹는다:

1. **재시도 정책** — 1 차 시도 + 최대 ``max_retry_count`` 회 재시도. 시도 사이
   에 ``time.sleep(2.0)`` 단순 backoff. exponential 금지 (디자인 노트 §4-4).
2. **EmailSendRun row 기록** — 한 호출당 row 1 개. 모든 시도가 같은 row 의
   ``attempt_count`` 에 누적. ``error_message`` 는 \"마지막 시도의 예외\" 만
   저장하고, 성공 시 NULL 로 클리어 (디자인 노트 §4-1 결정).

트랜잭션 경계 (디자인 노트 §4-4):
    호출자가 주입한 ``session`` 을 사용하며, 본 함수가 직접 ``session.commit()``
    한다 — 성공 종료 시점과 \"모든 재시도 실패 후 raise 직전\" 시점 두 곳.
    이는 \"재시도 도중 process crash 시에도 호출자 트랜잭션이 EmailSendRun row
    를 잃지 않도록\" + \"호출자가 자기 트랜잭션 안에서 다른 row 와 같이 묶고
    싶은 경우에도 commit 책임을 sender 가 진다\" 의 절충안이다. 호출자(admin
    API, subtask 00104-9) 는 본 함수 호출이 자기 session 의 commit 을 트리거
    한다는 점을 인지하고 동작해야 한다.

본 모듈이 직접 SystemSetting 을 읽지 않는다 — ``max_retry_count`` 는 호출자가
SystemSetting 에서 미리 읽어 kwarg 로 전달한다 (디자인 노트 §4-4 의 first
prompt 의사 signature 그대로). transport 인스턴스도 호출자가 factory 로 미리
만들어 주입한다.
"""

from __future__ import annotations

import time
from email.message import EmailMessage

from loguru import logger
from sqlalchemy.orm import Session

from app.db.models import EmailSendRun, EmailSendRunStatus
from app.email.constants import TRANSPORT_TYPE_M365_OAUTH
from app.email.transport.base import EmailTransport
from app.timezone import now_utc


# ──────────────────────────────────────────────────────────────
# 코드 상수 (정책 결정 — SystemSetting 화하지 않음)
# ──────────────────────────────────────────────────────────────


# 재시도 간격 (초). 첨부 phase_a1_prompt.md \"재시도 정책\" 섹션 명시: 단순 2초
# sleep, exponential 금지. 향후 정책 변경 시 본 상수만 수정한다.
RETRY_BACKOFF_SECONDS: float = 2.0

# body_preview 컬럼이 보관할 본문 prefix 길이. EmailSendRun.body_preview 의
# String(200) 컬럼 길이와 일치해야 한다 (00104-3 migration).
BODY_PREVIEW_MAX_LENGTH: int = 200


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────


def send_with_retry(
    transport: EmailTransport,
    message: EmailMessage,
    *,
    max_retry_count: int,
    related_kind: str,
    related_id: int | None,
    requested_by_user_id: int | None,
    session: Session,
) -> EmailSendRun:
    """transport.send 를 재시도 정책으로 감싸고 EmailSendRun row 1 개를 기록한다.

    동작 흐름:

    1. EmailSendRun row 를 생성하고 session 에 add → flush 로 PK 확보 (로그용).
       이 시점에서 row 는 ``status=FAILED`` placeholder + ``attempt_count=0``.
       호출자 session 에 INSERT 가 flush 되지만 아직 commit 되지 않음.
    2. 시도 루프를 ``1 + max_retry_count`` 회 돈다.
       각 시도 전에 ``run.attempt_count`` 를 현재 시도 번호로 갱신한다.
       각 시도에서 ``transport.send(message)`` 호출.
        - 성공 시 (예외 없이 반환): ``status=SENT``, ``sent_at=now_utc()``,
          ``error_message=None`` 으로 갱신 → ``session.commit()`` 후 row 를
          반환한다. 이전 시도에서 set 된 ``error_message`` 도 클리어된다.
        - 실패 시 (Exception): ``error_message`` 를 ``f\"{ClassName}: {exc}\"`` 형식
          으로 set. 마지막 시도가 아니면 ``time.sleep(2.0)`` 후 다음 시도.
    3. 모든 시도 실패 시: ``status=FAILED``, ``sent_at=None`` 으로 마무리,
       ``session.commit()`` 후 마지막 예외를 그대로 raise 한다. row 는 이미
       commit 되어 있으므로 호출자 (admin API) 가 ``send_run_id`` 를 응답에
       포함해 사용자에게 알릴 수 있다.

    시각 처리:
        ``created_at`` 은 ORM Python default ``_utcnow`` 가 자동 set (UTC
        tz-aware). ``sent_at`` 은 본 함수가 성공 시 ``now_utc()`` 로 set. DB
        저장은 UTC, 사용자 표시 직전에 ``app.timezone.format_kst`` 또는 Jinja2
        ``kst_format`` 필터로 KST 변환 (PROJECT_NOTES 컨벤션 + 디자인 노트 §7).

    Args:
        transport: 발송 실행을 위임할 ``EmailTransport`` 구현체. 보통
            ``app.email.transport.factory.build_transport_from_settings`` 가 만든
            ``M365OAuthSmtpTransport`` 인스턴스이지만, 단위 테스트에서는 Mock
            객체 / 가짜 구현체를 직접 주입할 수 있다.
        message: 발송할 ``EmailMessage``. ``app.email.message_builder.
            build_plain_text_message`` 등으로 미리 만들어 주입. ``To`` / ``Subject``
            헤더와 본문은 본 함수가 EmailSendRun row 의 recipient / subject /
            body_preview 컬럼에 그대로 복사한다.
        max_retry_count: 재시도 횟수. 0 이면 1 차 시도만 하고 끝, 2 이면 총 3 회
            시도. SystemSetting ``email.max_retry_count`` 를 호출자가 미리 읽어
            int 로 캐스트한 값.
        related_kind: 발송 컨텍스트 식별자. A-1 단계에서는 ``\"test_send\"`` 만
            (admin API 의 POST /test-send 경로). A-2 부터 ``\"forward\"``,
            ``\"daily_report\"`` 등 추가.
        related_id: ``related_kind`` 와 짝지어 외부 객체를 가리키는 PK. A-1
            에서는 ``None`` (test-send 는 외부 객체 미연결).
        requested_by_user_id: 발송을 트리거한 사용자 PK. 시스템 자동 발송 대비
            ``None`` 허용.
        session: SQLAlchemy ORM Session. 본 함수가 내부에서 INSERT/UPDATE 후
            ``commit()`` 을 호출한다 — 호출자는 자기 session 의 다른 미커밋
            변경사항이 함께 커밋될 수 있음을 알고 호출해야 한다.

    Returns:
        성공 시 ``status=SENT`` 가 채워지고 commit 된 ``EmailSendRun`` 인스턴스.
        호출자는 ``.id`` 로 admin API 응답에 포함 가능.

    Raises:
        Exception: 모든 시도가 실패한 경우 마지막 시도의 예외가 그대로 전파된다.
            이 시점에서 EmailSendRun row 는 ``status=FAILED`` 로 이미 commit
            되어 있으므로, 호출자 (admin API) 가 ``run.id`` 와 함께 사용자에게
            에러를 보여줄 수 있다. ``transport.send`` 가 던지는
            ``RuntimeError`` / ``smtplib.SMTPException`` / ``OSError`` 등 모든
            예외 타입이 그대로 전파된다.
    """
    recipient = message["To"] or ""
    subject = message["Subject"] or ""
    body_preview = _extract_body_preview(message)

    # 1. EmailSendRun row 생성 (status placeholder = FAILED, 시도 루프가 success
    # 시 SENT 로 갱신). attempt_count=0 으로 시작해 시도 직전에 attempt_number
    # 로 set 한다.
    run = EmailSendRun(
        recipient=recipient,
        subject=subject,
        body_preview=body_preview,
        # A-1 에서는 단일 transport_type. 향후 옵션 C 가 추가되면 transport
        # 인스턴스로부터 type 문자열을 유도하는 방식 (예: getattr(transport,
        # 'TRANSPORT_TYPE_NAME', ...)) 으로 확장 가능. 본 subtask 에서는
        # 단일 값으로 하드코딩.
        transport_type=TRANSPORT_TYPE_M365_OAUTH,
        status=EmailSendRunStatus.FAILED,
        attempt_count=0,
        related_kind=related_kind,
        related_id=related_id,
        requested_by_user_id=requested_by_user_id,
    )
    session.add(run)
    # flush 로 PK 를 확보해 로그에 run_id 를 찍을 수 있게 한다. 호출자
    # session 의 트랜잭션이 commit 되기 전까지 다른 connection 에서는 보이지
    # 않음 (Postgres read-committed / SQLite serializable).
    session.flush()

    total_attempts = max_retry_count + 1
    logger.info(
        "메일 발송 시작: run_id={} recipient={!r} subject={!r} max_attempts={}",
        run.id,
        recipient,
        subject,
        total_attempts,
    )

    last_exception: Exception | None = None

    for attempt_number in range(1, total_attempts + 1):
        # attempt_count 는 시도 직전에 갱신 — 이로써 \"시도 횟수 누적\" 시맨틱
        # (디자인 노트 §4-1) 을 만족한다.
        run.attempt_count = attempt_number
        logger.debug(
            "메일 발송 시도 {}/{}: run_id={}",
            attempt_number,
            total_attempts,
            run.id,
        )

        try:
            transport.send(message)
        except Exception as exc:
            # 마지막 예외만 error_message 에 보존 (디자인 노트 §4-1). 중간
            # 시도 에러는 본 컬럼에 영구 저장하지 않고 loguru 로그에만 남긴다.
            last_exception = exc
            run.error_message = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "메일 발송 실패 (시도 {}/{}): run_id={} error={}: {}",
                attempt_number,
                total_attempts,
                run.id,
                type(exc).__name__,
                exc,
            )
            if attempt_number < total_attempts:
                # 다음 시도 전에 단순 2초 sleep. 첨부 \"재시도 정책\" + 디자인
                # 노트 §4-4 결정: exponential backoff 미도입.
                logger.debug(
                    "재시도 대기 {}초 후 다음 시도 (run_id={})",
                    RETRY_BACKOFF_SECONDS,
                    run.id,
                )
                time.sleep(RETRY_BACKOFF_SECONDS)
            continue

        # 성공 분기: status / sent_at / error_message 를 success 값으로 갱신
        # 하고 commit 한 뒤 row 를 반환한다. 이전 시도에서 set 된
        # error_message 는 None 으로 클리어 — \"마지막 시도의 결과만 status
        # 에 반영\" 시맨틱.
        run.status = EmailSendRunStatus.SENT
        run.sent_at = now_utc()
        run.error_message = None
        session.commit()
        logger.info(
            "메일 발송 성공: run_id={} recipient={!r} attempt_count={}",
            run.id,
            recipient,
            run.attempt_count,
        )
        return run

    # 모든 시도가 실패한 경우. status=FAILED, sent_at=None 으로 마무리한 뒤
    # commit 하고 마지막 예외를 그대로 호출자에게 던진다. row 는 이미 commit
    # 되어 있으므로 호출자가 send_run_id 로 사용자에게 안내 가능.
    run.status = EmailSendRunStatus.FAILED
    run.sent_at = None
    session.commit()
    logger.error(
        "메일 발송 최종 실패: run_id={} recipient={!r} attempt_count={} last_error={!r}",
        run.id,
        recipient,
        run.attempt_count,
        run.error_message,
    )
    # last_exception 은 시도 루프가 한 번이라도 돌았으면 항상 set 된다
    # (total_attempts >= 1 보장 — max_retry_count >= 0).
    assert last_exception is not None
    raise last_exception


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _extract_body_preview(message: EmailMessage) -> str:
    """EmailMessage 의 본문 앞 ``BODY_PREVIEW_MAX_LENGTH`` 자를 추출한다.

    ``EmailMessage.get_content()`` 는 plain text 단일 part 메일에서 본문
    문자열을 반환한다 (마지막에 ``\\n`` 이 자동 추가될 수 있음). 본 함수는
    그대로 prefix slicing 만 수행하며, 줄바꿈 제거 / 공백 정리 등의 별도
    가공은 하지 않는다.

    Args:
        message: ``app.email.message_builder.build_plain_text_message`` 등이 만든
            EmailMessage. multipart 가 들어와도 ``get_content()`` 가 raise 하거나
            non-string 을 반환할 수 있어 방어적으로 처리한다 — A-1 에서는
            단일 plain text part 만 사용하므로 통상 정상 경로.

    Returns:
        본문 앞 200 자 (또는 그 이하). EmailSendRun.body_preview 컬럼의
        String(200) 길이와 정합.
    """
    try:
        content = message.get_content()
    except (KeyError, AttributeError):
        # 비정상적인 EmailMessage (예: 본문 미설정) — 빈 문자열로 fallback.
        # EmailSendRun.body_preview 는 NOT NULL 이라 \"\" 가 들어가도 무방.
        content = ""
    if not isinstance(content, str):
        # multipart payload 등이 list/bytes 로 들어오는 케이스 방어. A-1 의
        # build_plain_text_message 경유라면 항상 str 이지만, 호출자가 직접
        # EmailMessage 를 만들었을 수도 있다.
        content = str(content)
    return content[:BODY_PREVIEW_MAX_LENGTH]


__all__ = [
    "BODY_PREVIEW_MAX_LENGTH",
    "RETRY_BACKOFF_SECONDS",
    "send_with_retry",
]
