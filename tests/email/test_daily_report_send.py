"""``prepare_and_send_daily_report`` + ``collect_admin_recipient_emails`` 단위 테스트.

검증 대상 (subtask 00125-6 의 acceptance_criteria + design note §5·§7 + prompt §5):

    A. ``collect_admin_recipient_emails`` — admin email 수집 정책.
        A-1. is_admin=True + email 정상 + email_subscribed=True 만 포함.
        A-2. email NULL / 빈 문자열 / email_subscribed=False / is_admin=False 모두 제외.
        A-3. 중복 제거 + ASCII case-insensitive 정렬.

    B. ``prepare_and_send_daily_report`` — 트랜잭션 3단계 / 게이트 / 정책표.
        B-1. 정상 시나리오: 2 수신자 모두 성공 → SUCCESS, last_sent_at 갱신.
        B-2. 부분 실패: 1 성공 / 1 실패 → PARTIAL, last_sent_at 갱신.
        B-3. 모두 실패 → FAILED, last_sent_at 유지.
        B-4. 빈 구간 → SKIPPED, last_sent_at 유지, EmailSendRun 생성 0건.
        B-5. ``email.send_enabled=false`` → 503 raise, EmailDailyReportRun row 는
             ``status=FAILED`` 로 commit.
        B-6. ``trigger='manual_test'`` 는 성공해도 last_sent_at 유지.
        B-7. EmailDailyReportRun.aggregation_from / aggregation_to /
             snapshot_count 가 window 와 일치.
        B-8. EmailSendRun row 가 ``related_kind='daily_report'`` +
             ``related_id=run_id`` 로 정확히 N개 생성.

DB:
    tests/conftest.py 의 ``test_engine`` + ``db_session`` fixture 사용.

transport mock:
    ``_FakeTransport`` (tests/email/test_forwarding.py 와 동일 패턴). ``send`` 호출
    시점마다 ``results`` 시퀀스를 소비해 None → 성공 / Exception → 실패.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backup.service import get_setting, set_setting
from app.db.models import (
    Announcement,
    AnnouncementStatus,
    EmailDailyReportRun,
    EmailDailyReportStatus,
    EmailSendRun,
    EmailSendRunStatus,
    ScrapeSnapshot,
    User,
)
from app.db.snapshot import normalize_payload
from app.email.constants import (
    RELATED_KIND_DAILY_REPORT,
    SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
    SETTING_KEY_EMAIL_SEND_ENABLED,
)
from app.email.daily_report import (
    TRIGGER_MANUAL_ADMIN,
    TRIGGER_MANUAL_TEST,
    TRIGGER_SCHEDULED,
    DailyReportRequest,
    collect_admin_recipient_emails,
    prepare_and_send_daily_report,
)
from app.email.gate import EmailSendingDisabledError
from app.email.transport.base import EmailTransport


# ──────────────────────────────────────────────────────────────
# 테스트 더블 / autouse fixture
# ──────────────────────────────────────────────────────────────


class _FakeTransport(EmailTransport):
    """테스트용 transport — ``results`` 시퀀스를 1회씩 소비한다.

    None → 정상 성공, Exception 인스턴스 → 그 예외를 raise. 호출 순서 검증을
    위해 ``send_call_count`` 누적, ``sent_recipients`` 에 To 헤더를 기록한다.
    tests/email/test_forwarding.py 의 동명 클래스와 같은 패턴.
    """

    def __init__(self, results: list[None | Exception]) -> None:
        """주어진 결과 시퀀스로 transport 를 초기화한다."""
        self.results: list[None | Exception] = list(results)
        self.send_call_count: int = 0
        self.sent_recipients: list[str] = []

    def send(self, message: EmailMessage) -> None:
        """다음 결과를 꺼내 None 이면 통과, 예외면 raise 한다."""
        self.send_call_count += 1
        self.sent_recipients.append(message["To"] or "")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """``send_with_retry`` 의 재시도 ``time.sleep`` 을 no-op 으로 차단한다."""
    monkeypatch.setattr("app.email.sender.time.sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _enable_email_sending(db_session: Session) -> None:
    """대부분의 테스트가 게이트 ON 을 전제한다 — autouse 로 미리 켠다.

    게이트 OFF 케이스(B-5) 는 본 fixture 가 commit 한 뒤 테스트 본문에서 다시
    ``"false"`` 로 덮어쓴다.
    """
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "true")
    db_session.commit()


# ──────────────────────────────────────────────────────────────
# DB row 생성 헬퍼
# ──────────────────────────────────────────────────────────────


def _insert_admin_user(
    session: Session,
    *,
    username: str,
    email: str | None,
    is_admin: bool = True,
    email_subscribed: bool = True,
) -> User:
    """테스트용 ``User`` row 1건을 생성한다.

    is_admin / email / email_subscribed 의 조합으로 admin 수신자 정책의 다양한
    케이스를 만든다. password_hash 는 placeholder.
    """
    user = User(
        username=username,
        password_hash="$placeholder$",
        email=email,
        email_subscribed=email_subscribed,
        is_admin=is_admin,
    )
    session.add(user)
    session.flush()
    return user


def _insert_snapshot(
    session: Session,
    *,
    created_at: datetime,
    snapshot_date_iso: str,
    payload: dict | None = None,
) -> ScrapeSnapshot:
    """``ScrapeSnapshot`` row 1건을 명시적 created_at 으로 INSERT 한다.

    payload 는 ``normalize_payload`` 로 정규형 dict 으로 만들어 둔다.
    snapshot_date 의 UNIQUE 제약을 만족하려면 테스트마다 다른 일자 문자열을
    전달해야 한다.
    """
    snapshot = ScrapeSnapshot(
        snapshot_date=date.fromisoformat(snapshot_date_iso),
        created_at=created_at,
        payload=normalize_payload(payload),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _insert_announcement(
    session: Session,
    *,
    source_announcement_id: str,
    title: str = "테스트 공고",
) -> Announcement:
    """본문 빌더가 announcement 메타를 조회할 수 있도록 ``Announcement`` 1건 INSERT."""
    announcement = Announcement(
        source_announcement_id=source_announcement_id,
        source_type="IRIS",
        title=title,
        agency="기관A",
        status=AnnouncementStatus.RECEIVING,
        is_current=True,
    )
    session.add(announcement)
    session.flush()
    return announcement


def _set_last_sent_at(session: Session, value_iso: str) -> None:
    """SystemSetting ``email.daily_report.last_sent_at`` 을 명시적으로 set."""
    set_setting(session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT, value_iso)
    session.commit()


def _make_window_with_snapshots(
    session: Session,
    *,
    last_sent: datetime,
    now: datetime,
    payload: dict,
) -> None:
    """``last_sent_at`` SystemSetting + ``(last_sent, now]`` 구간 안에 snapshot 1건.

    호출 후 ``compute_aggregation_window`` 가 항상 정상 ``AggregationWindow`` 를
    반환하도록 보장한다 — 발송 흐름 테스트의 공통 셋업.
    """
    _set_last_sent_at(session, last_sent.isoformat())
    _insert_snapshot(
        session,
        created_at=last_sent + timedelta(hours=1),
        snapshot_date_iso="2026-05-18",
        payload=payload,
    )
    session.commit()


# ──────────────────────────────────────────────────────────────
# A. collect_admin_recipient_emails — 수신자 정책
# ──────────────────────────────────────────────────────────────


def test_collect_admin_recipients_includes_only_eligible_admins(
    db_session: Session,
) -> None:
    """is_admin=True + email 정상 + email_subscribed=True 만 포함된다.

    제외 케이스:
        - is_admin=False (일반 사용자)
        - email IS NULL
        - email = '' (빈 문자열)
        - email_subscribed = False (옵트아웃)
    """
    # 포함 대상.
    _insert_admin_user(db_session, username="alice", email="alice@example.com")
    _insert_admin_user(db_session, username="bob", email="bob@example.com")

    # 제외 — 일반 사용자.
    _insert_admin_user(
        db_session,
        username="non_admin",
        email="non_admin@example.com",
        is_admin=False,
    )
    # 제외 — email NULL.
    _insert_admin_user(db_session, username="no_email_admin", email=None)
    # 제외 — email 빈 문자열.
    _insert_admin_user(db_session, username="empty_email_admin", email="")
    # 제외 — email_subscribed=False (옵트아웃).
    _insert_admin_user(
        db_session,
        username="unsub_admin",
        email="unsub@example.com",
        email_subscribed=False,
    )
    db_session.commit()

    emails = collect_admin_recipient_emails(db_session)

    assert emails == ["alice@example.com", "bob@example.com"]


def test_collect_admin_recipients_dedups_and_sorts_case_insensitively(
    db_session: Session,
) -> None:
    """동일 email 중복 제거 + ASCII case-insensitive 정렬.

    운영 데이터에서 admin 가 같은 email 을 공유하는 (또는 대소문자만 다른) 비정상
    케이스에도 발송이 1번만 발생해야 한다.
    """
    _insert_admin_user(db_session, username="a1", email="Charlie@example.com")
    _insert_admin_user(db_session, username="a2", email="alice@example.com")
    _insert_admin_user(db_session, username="a3", email="alice@example.com")
    _insert_admin_user(db_session, username="a4", email="bob@example.com")
    db_session.commit()

    emails = collect_admin_recipient_emails(db_session)

    # alice 중복 제거 + 소문자 비교 정렬: alice → bob → Charlie.
    assert emails == ["alice@example.com", "bob@example.com", "Charlie@example.com"]


# ──────────────────────────────────────────────────────────────
# B-1. 정상 시나리오 — SUCCESS, last_sent_at 갱신
# ──────────────────────────────────────────────────────────────


def test_prepare_and_send_all_success_updates_last_sent_at(
    db_session: Session,
) -> None:
    """수신자 2명 모두 성공 → SUCCESS + last_sent_at = now 로 갱신.

    검증:
        - ``DailyReportResult.status == SUCCESS`` , 카운트 정확.
        - EmailDailyReportRun row 가 SUCCESS 로 commit 되어 있다.
        - EmailSendRun row 가 ``related_kind=daily_report`` /
          ``related_id=run_id`` 로 정확히 2개, 모두 SENT.
        - SystemSetting last_sent_at 이 now (window.to_dt) 의 ISO-8601 로 갱신.
    """
    last_sent = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    announcement = _insert_announcement(
        db_session, source_announcement_id="A-OK"
    )
    _make_window_with_snapshots(
        db_session,
        last_sent=last_sent,
        now=now,
        payload={"new": [announcement.id]},
    )

    transport = _FakeTransport(results=[None, None])
    request = DailyReportRequest(
        trigger=TRIGGER_SCHEDULED,
        recipients=["alice@example.com", "bob@example.com"],
        requested_by_user_id=None,
    )

    result = prepare_and_send_daily_report(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
        now=now,
    )

    assert result.status == EmailDailyReportStatus.SUCCESS
    assert result.success_count == 2
    assert result.failure_count == 0
    assert result.recipient_count == 2
    assert result.snapshot_count == 1
    assert result.error_message is None
    assert transport.send_call_count == 2

    run = db_session.get(EmailDailyReportRun, result.run_id)
    assert run is not None
    assert run.status == EmailDailyReportStatus.SUCCESS
    assert run.success_count == 2
    assert run.failure_count == 0
    assert run.completed_at is not None
    # aggregation_* 컬럼이 채워졌는지 (B-7 일부).
    assert run.aggregation_from is not None
    assert run.aggregation_to is not None
    assert run.snapshot_count == 1

    # EmailSendRun row 가 2개, 모두 daily_report 로 연결.
    send_runs = (
        db_session.execute(
            select(EmailSendRun).where(
                EmailSendRun.related_kind == RELATED_KIND_DAILY_REPORT,
                EmailSendRun.related_id == result.run_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(send_runs) == 2
    assert all(send_run.status == EmailSendRunStatus.SENT for send_run in send_runs)

    # last_sent_at 갱신 — window.to_dt (= now) 의 ISO-8601.
    saved_last_sent = get_setting(
        db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT
    )
    assert saved_last_sent == now.isoformat()


# ──────────────────────────────────────────────────────────────
# B-2. 부분 실패 — PARTIAL, last_sent_at 갱신
# ──────────────────────────────────────────────────────────────


def test_prepare_and_send_partial_failure_still_updates_last_sent_at(
    db_session: Session,
) -> None:
    """1성공 / 1실패 → PARTIAL + last_sent_at 갱신.

    정책표 §7 — scheduled + PARTIAL 도 갱신 대상.
    """
    last_sent = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    announcement = _insert_announcement(
        db_session, source_announcement_id="A-PARTIAL"
    )
    _make_window_with_snapshots(
        db_session,
        last_sent=last_sent,
        now=now,
        payload={"new": [announcement.id]},
    )

    transport = _FakeTransport(
        results=[None, RuntimeError("recipient2 실패")]
    )
    request = DailyReportRequest(
        trigger=TRIGGER_SCHEDULED,
        recipients=["ok@example.com", "ng@example.com"],
        requested_by_user_id=None,
    )

    result = prepare_and_send_daily_report(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
        now=now,
    )

    assert result.status == EmailDailyReportStatus.PARTIAL
    assert result.success_count == 1
    assert result.failure_count == 1
    assert result.error_message is not None
    assert "RuntimeError" in result.error_message

    # last_sent_at 갱신 — PARTIAL 도 갱신 대상.
    assert (
        get_setting(db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)
        == now.isoformat()
    )

    run = db_session.get(EmailDailyReportRun, result.run_id)
    assert run is not None
    assert run.status == EmailDailyReportStatus.PARTIAL
    # error_message 가 commit 되어 있는지.
    assert run.error_message is not None
    assert "RuntimeError" in run.error_message


# ──────────────────────────────────────────────────────────────
# B-3. 모두 실패 — FAILED, last_sent_at 유지
# ──────────────────────────────────────────────────────────────


def test_prepare_and_send_all_failure_keeps_last_sent_at(
    db_session: Session,
) -> None:
    """모든 수신자 발송 실패 → FAILED + last_sent_at 유지 (다음 잡이 재시도).

    정책표 §7 — scheduled + FAILED 는 갱신 안 함.
    """
    last_sent = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    announcement = _insert_announcement(
        db_session, source_announcement_id="A-FAIL"
    )
    _make_window_with_snapshots(
        db_session,
        last_sent=last_sent,
        now=now,
        payload={"new": [announcement.id]},
    )
    # last_sent_at 의 기존 값을 보존 검증용으로 미리 캡처.
    saved_before = get_setting(db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)
    assert saved_before == last_sent.isoformat()

    transport = _FakeTransport(
        results=[RuntimeError("실패1"), RuntimeError("실패2")]
    )
    request = DailyReportRequest(
        trigger=TRIGGER_SCHEDULED,
        recipients=["x@example.com", "y@example.com"],
        requested_by_user_id=None,
    )

    result = prepare_and_send_daily_report(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
        now=now,
    )

    assert result.status == EmailDailyReportStatus.FAILED
    assert result.success_count == 0
    assert result.failure_count == 2

    # last_sent_at — 발송 전 값 그대로 유지.
    assert (
        get_setting(db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)
        == last_sent.isoformat()
    )


# ──────────────────────────────────────────────────────────────
# B-4. 빈 구간 — SKIPPED, last_sent_at 유지, EmailSendRun 0건
# ──────────────────────────────────────────────────────────────


def test_prepare_and_send_skipped_when_window_empty(db_session: Session) -> None:
    """구간 안 snapshot 0건 → SKIPPED + 발송 자체 skip + last_sent_at 유지.

    검증:
        - ``DailyReportResult.status == SKIPPED`` , 카운트 0.
        - transport.send 호출 0회.
        - EmailSendRun row 0개.
        - EmailDailyReportRun row 가 SKIPPED 로 commit.
        - last_sent_at 갱신되지 않음 (다음 잡이 같은 구간 처리).
    """
    last_sent = datetime(2026, 5, 19, 11, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    # 구간 (last_sent, now] 안에 snapshot 없음 — 직전에는 있어도 무관.
    _set_last_sent_at(db_session, last_sent.isoformat())

    transport = _FakeTransport(results=[])
    request = DailyReportRequest(
        trigger=TRIGGER_SCHEDULED,
        recipients=["admin@example.com"],
        requested_by_user_id=None,
    )

    result = prepare_and_send_daily_report(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
        now=now,
    )

    assert result.status == EmailDailyReportStatus.SKIPPED
    assert result.snapshot_count == 0
    assert result.success_count == 0
    assert result.failure_count == 0
    assert transport.send_call_count == 0

    # EmailSendRun 미생성 — daily_report 키로 조회해 0건.
    send_runs = (
        db_session.execute(
            select(EmailSendRun).where(
                EmailSendRun.related_kind == RELATED_KIND_DAILY_REPORT
            )
        )
        .scalars()
        .all()
    )
    assert send_runs == []

    # EmailDailyReportRun 은 SKIPPED 로 영속.
    run = db_session.get(EmailDailyReportRun, result.run_id)
    assert run is not None
    assert run.status == EmailDailyReportStatus.SKIPPED
    assert run.snapshot_count == 0
    # aggregation_from / to 는 SKIPPED 케이스에서 NULL.
    assert run.aggregation_from is None
    assert run.aggregation_to is None
    assert run.completed_at is not None

    # last_sent_at — 변경되지 않음.
    assert (
        get_setting(db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)
        == last_sent.isoformat()
    )


# ──────────────────────────────────────────────────────────────
# B-5. 게이트 차단 — 503 raise + EmailDailyReportRun FAILED
# ──────────────────────────────────────────────────────────────


def test_prepare_and_send_raises_when_gate_disabled(db_session: Session) -> None:
    """``email.send_enabled=false`` → ``EmailSendingDisabledError`` raise.

    raise 직전에 EmailDailyReportRun row 가 ``status=FAILED`` 로 commit 되어 있어
    이력에 \"발송 시도\" 가 남는다.
    """
    # autouse fixture 가 'true' 로 켜 둔 게이트를 다시 끈다.
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "false")
    db_session.commit()

    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    transport = _FakeTransport(results=[])
    request = DailyReportRequest(
        trigger=TRIGGER_SCHEDULED,
        recipients=["admin@example.com"],
        requested_by_user_id=None,
    )

    with pytest.raises(EmailSendingDisabledError):
        prepare_and_send_daily_report(
            request,
            session=db_session,
            transport=transport,
            max_retry_count=0,
            now=now,
        )

    # 최근 commit 된 EmailDailyReportRun row 가 FAILED 로 남아야 함.
    runs = (
        db_session.execute(
            select(EmailDailyReportRun).order_by(
                EmailDailyReportRun.id.desc()
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1
    failed_run = runs[0]
    assert failed_run.status == EmailDailyReportStatus.FAILED
    assert failed_run.completed_at is not None
    assert failed_run.error_message is not None
    assert "비활성화" in failed_run.error_message

    # transport.send 는 호출되지 않았음.
    assert transport.send_call_count == 0


# ──────────────────────────────────────────────────────────────
# B-6. manual_test 는 성공해도 last_sent_at 유지
# ──────────────────────────────────────────────────────────────


def test_prepare_and_send_manual_test_does_not_update_last_sent_at(
    db_session: Session,
) -> None:
    """``trigger='manual_test'`` 는 SUCCESS 여도 last_sent_at 을 건드리지 않는다.

    테스트 발송이 본 발송 구간을 망가뜨리지 않도록 — 정책표 §7 의 핵심 가드.
    """
    last_sent = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    announcement = _insert_announcement(
        db_session, source_announcement_id="A-TEST"
    )
    _make_window_with_snapshots(
        db_session,
        last_sent=last_sent,
        now=now,
        payload={"new": [announcement.id]},
    )

    transport = _FakeTransport(results=[None])
    request = DailyReportRequest(
        trigger=TRIGGER_MANUAL_TEST,
        recipients=["tester@example.com"],
        requested_by_user_id=None,
    )

    result = prepare_and_send_daily_report(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
        now=now,
    )

    # SUCCESS 인데도 last_sent_at 은 변경되지 않아야 한다.
    assert result.status == EmailDailyReportStatus.SUCCESS
    assert (
        get_setting(db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)
        == last_sent.isoformat()
    )


# ──────────────────────────────────────────────────────────────
# B-7. manual_admin SUCCESS → last_sent_at 갱신 + EmailDailyReportRun aggregation 컬럼
# ──────────────────────────────────────────────────────────────


def test_prepare_and_send_manual_admin_updates_last_sent_at_and_aggregation_columns(
    db_session: Session,
) -> None:
    """``manual_admin`` + SUCCESS → last_sent_at 갱신 + aggregation_from / to 일치.

    검증:
        - last_sent_at 이 now.isoformat() 로 갱신 (정책표 §7).
        - run.aggregation_from / aggregation_to / snapshot_count 가 window 와 일치.
        - requested_by_user_id 도 row 에 그대로 commit.
    """
    last_sent = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    announcement = _insert_announcement(
        db_session, source_announcement_id="A-MA"
    )
    _make_window_with_snapshots(
        db_session,
        last_sent=last_sent,
        now=now,
        payload={"new": [announcement.id]},
    )
    # manual_admin 의 requested_by_user_id 검증을 위해 admin 1명 추가.
    requesting_admin = _insert_admin_user(
        db_session, username="requesting_admin", email="ra@example.com"
    )
    db_session.commit()

    transport = _FakeTransport(results=[None])
    request = DailyReportRequest(
        trigger=TRIGGER_MANUAL_ADMIN,
        recipients=["ra@example.com"],
        requested_by_user_id=requesting_admin.id,
    )

    result = prepare_and_send_daily_report(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
        now=now,
    )

    assert result.status == EmailDailyReportStatus.SUCCESS
    # last_sent_at 갱신 확인.
    assert (
        get_setting(db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)
        == now.isoformat()
    )

    run = db_session.get(EmailDailyReportRun, result.run_id)
    assert run is not None
    # aggregation_* 컬럼이 정확히 window 와 일치.
    # SQLite 백엔드는 SELECT 시 tz 정보를 잃어 naive 로 돌아오므로 양쪽을 UTC 로
    # 정규화해 비교한다.
    from app.db.models import as_utc

    assert as_utc(run.aggregation_from) == last_sent
    assert as_utc(run.aggregation_to) == now
    assert run.snapshot_count == 1
    assert run.requested_by_user_id == requesting_admin.id
    assert run.trigger == TRIGGER_MANUAL_ADMIN
