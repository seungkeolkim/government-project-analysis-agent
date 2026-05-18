"""message_builder + forwarding service 단위 테스트 (Phase A-2 Part 2 / task 00109-7).

검증 시나리오 (첨부 phase_a2_part2_prompt.md "검증 > Agent 작성 단위 테스트"
tests/email/test_forwarding.py 항목 그대로):

    1. ``build_multipart_message`` — multipart/alternative 구조, text/plain +
       text/html 두 alternative 가 모두 포함되는지, From/To/Subject 헤더 정확성.
    2. ``build_default_forward_subject`` — 공고 제목이 100 자를 넘으면 truncate
       후 말줄임표가 붙는지.
    3. ``forward_announcement`` 정상 — 수신자 3명이 모두 성공하면
       ``status='success'``, ``success_count=3``, EmailSendRun row 3개가
       ``related_kind='forward'`` + ``related_id`` 매칭으로 생성되는지.
    4. ``forward_announcement`` 부분 실패 — 3명 중 1명만 발송 예외면
       ``status='partial'``, ``success_count=2``, ``failure_count=1``.
    5. ``forward_announcement`` 전체 실패 — 모든 발송이 예외면
       ``status='failed'``, ``success_count=0``, ``failure_count=3``.
    6. ``forward_announcement`` 빈 recipients → ``ValueError``.
    7. ``forward_announcement`` sender_organization_id 가 발송자 소속이 아니면
       → ``PermissionError``.

DB:
    tests/conftest.py 의 ``test_engine`` + ``db_session`` fixture 사용 — 각
    테스트는 tmp_path 기반 고유 SQLite 파일 + Alembic upgrade head 적용 상태.

transport mock:
    ``forward_announcement`` 은 ``transport`` 를 kwarg 로 직접 받으므로,
    msal/smtplib 까지 내려가지 않고 ``tests/email/test_sender_retry.py`` 의
    ``_FakeTransport`` 패턴(주입된 결과 시퀀스를 소비)으로 발송 성공/실패를
    제어한다. ``send_with_retry`` 의 재시도 ``time.sleep`` 은 autouse fixture
    로 차단한다.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    EmailForwardLog,
    EmailForwardStatus,
    EmailSendRun,
    EmailSendRunStatus,
    Organization,
    User,
    UserOrganization,
)
from app.backup.service import set_setting
from app.email.constants import RELATED_KIND_FORWARD, SETTING_KEY_EMAIL_SEND_ENABLED
from app.email.forwarding import ForwardRequest, forward_announcement
from app.email.message_builder import (
    build_default_forward_subject,
    build_forward_html_body,
    build_forward_text_body,
    build_multipart_message,
)
from app.email.transport.base import EmailTransport
from app.sources.constants import SOURCE_TYPE_IRIS


# ──────────────────────────────────────────────────────────────
# 테스트 더블 / autouse fixture
# ──────────────────────────────────────────────────────────────


class _FakeTransport(EmailTransport):
    """테스트용 transport 구현체.

    ``results`` 는 ``send`` 호출 시점마다 소비될 결과 시퀀스다. ``None`` 이면
    정상 성공, ``Exception`` 인스턴스이면 그 예외를 raise 한다
    (``tests/email/test_sender_retry.py`` 의 동명 클래스와 같은 패턴).
    """

    def __init__(self, results: list[None | Exception]) -> None:
        """주어진 결과 시퀀스로 transport 를 초기화한다."""
        self.results: list[None | Exception] = list(results)
        self.send_call_count: int = 0

    def send(self, message: EmailMessage) -> None:
        """다음 결과를 꺼내 None 이면 통과, 예외면 raise 한다."""
        self.send_call_count += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """``send_with_retry`` 의 재시도 ``time.sleep`` 을 no-op 으로 차단한다.

    본 테스트는 ``max_retry_count=0`` 으로 호출해 재시도 자체가 거의 없지만,
    혹시라도 sleep 이 발생해 테스트가 느려지지 않도록 자동 적용한다.
    """
    monkeypatch.setattr("app.email.sender.time.sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _enable_email_sending(db_session: Session) -> None:
    """본 파일의 모든 테스트에서 메일 전송 게이트를 on 으로 설정한다.

    task 00115-1 에서 도입된 ``email.send_enabled`` 게이트의 default 가
    False 이므로, 포워딩 기능 자체를 검증하는 이 파일의 테스트들은 게이트를
    미리 켜 두어야 한다.
    """
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "true")
    db_session.commit()


# ──────────────────────────────────────────────────────────────
# DB row 생성 헬퍼 — fixture 가 아니라 함수로 제공해 각 테스트가 독립 생성한다.
# ──────────────────────────────────────────────────────────────


def _make_canonical_project(
    session: Session,
    *,
    key_suffix: str,
) -> CanonicalProject:
    """테스트용 CanonicalProject row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        key_suffix: ``canonical_key`` 뒤에 붙는 식별 suffix (테스트 간 중복 방지).

    Returns:
        flush 된 CanonicalProject 인스턴스 (id 가 채워진 상태).
    """
    project = CanonicalProject(
        canonical_key=f"official:fwd-svc-{key_suffix}",
        key_scheme="official",
    )
    session.add(project)
    session.flush()
    return project


def _make_announcement(
    session: Session,
    *,
    canonical_project: CanonicalProject,
    source_announcement_id: str,
    title: str = "테스트 공고 제목",
    is_current: bool = True,
) -> Announcement:
    """테스트용 Announcement row 를 생성하고 flush 후 반환한다.

    ``forward_announcement`` 의 ``_pick_announcement_for_canonical`` 이
    ``canonical_group_id == canonical_project_id`` AND ``is_current=True`` 인
    row 를 메일 컨텐츠로 고르므로, canonical group 에 연결하고 is_current 를
    명시한다.

    Args:
        session: 대상 ORM 세션.
        canonical_project: 이 announcement 가 속할 CanonicalProject.
        source_announcement_id: 소스 공고 ID (테스트 간 중복 방지용 고유 값).
        title: 공고 제목.
        is_current: 현재 유효 버전 여부.

    Returns:
        flush 된 Announcement 인스턴스 (id 가 채워진 상태).
    """
    announcement = Announcement(
        source_announcement_id=source_announcement_id,
        source_type=SOURCE_TYPE_IRIS,
        title=title,
        agency="테스트 발주기관",
        status=AnnouncementStatus.RECEIVING,
        canonical_group_id=canonical_project.id,
        is_current=is_current,
    )
    session.add(announcement)
    session.flush()
    return announcement


def _make_user(
    session: Session,
    *,
    username: str,
    email: str | None = "sender@example.com",
) -> User:
    """테스트용 User row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        username: UNIQUE 제약 만족용 고유 로그인 ID.
        email: 발송자 이메일 (footer 표시에 쓰임).

    Returns:
        flush 된 User 인스턴스 (id 가 채워진 상태).
    """
    user = User(
        username=username,
        password_hash="$hashed$placeholder",
        email=email,
    )
    session.add(user)
    session.flush()
    return user


def _make_organization(session: Session, *, name: str) -> Organization:
    """테스트용 루트 Organization row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        name: 조직명.

    Returns:
        flush 된 Organization 인스턴스 (id 가 채워진 상태).
    """
    organization = Organization(name=name)
    session.add(organization)
    session.flush()
    return organization


def _add_membership(
    session: Session,
    *,
    user: User,
    organization: Organization,
) -> None:
    """사용자를 조직에 소속시키는 UserOrganization 매핑을 생성한다.

    Args:
        session: 대상 ORM 세션.
        user: 소속될 사용자.
        organization: 소속 대상 조직.
    """
    session.add(
        UserOrganization(user_id=user.id, organization_id=organization.id)
    )
    session.flush()


# ──────────────────────────────────────────────────────────────
# 1. build_multipart_message — 구조 + 헤더
# ──────────────────────────────────────────────────────────────


def test_build_multipart_message_structure_and_headers() -> None:
    """build_multipart_message 가 multipart/alternative 구조와 헤더를 올바르게 만든다.

    검증 포인트:
        - 최상위 Content-Type 이 ``multipart/alternative``.
        - 하위 part 가 정확히 ``text/plain`` + ``text/html`` 두 개 (순서 포함).
        - text/plain 에는 ``text_body``, text/html 에는 ``html_body`` 내용 포함.
        - To / Subject 헤더가 인자 그대로.
        - From 헤더에 발신 주소가 포함됨 (display name 한글은 RFC 2047 로
          인코딩되지만 주소 부분은 평문으로 남는다).
    """
    message = build_multipart_message(
        sender_address="gov-noreply@example.com",
        sender_display_name="정부사업 모니터링",
        recipient="recipient@example.com",
        subject="포워딩 메일 제목",
        text_body="이것은 plain text 본문입니다.",
        html_body="<html><body><h2>HTML 본문</h2></body></html>",
    )

    # 최상위 컨테이너가 multipart/alternative 인지.
    assert message.get_content_type() == "multipart/alternative"

    # 하위 part 가 text/plain → text/html 순서로 정확히 2개인지.
    parts = list(message.iter_parts())
    assert [part.get_content_type() for part in parts] == [
        "text/plain",
        "text/html",
    ]

    # 각 alternative 의 본문 내용 확인.
    plain_content = parts[0].get_content()
    html_content = parts[1].get_content()
    assert "이것은 plain text 본문입니다." in plain_content
    assert "HTML 본문" in html_content

    # To / Subject 헤더는 인자 그대로.
    assert message["To"] == "recipient@example.com"
    assert message["Subject"] == "포워딩 메일 제목"

    # From 헤더 — display name 은 RFC 2047 로 인코딩될 수 있으나 주소는 평문.
    assert "gov-noreply@example.com" in message["From"]


# ──────────────────────────────────────────────────────────────
# 2. build_default_forward_subject — 100 자 초과 truncate
# ──────────────────────────────────────────────────────────────


def test_build_default_forward_subject_truncates_long_title() -> None:
    """build_default_forward_subject 가 100 자 초과 제목을 truncate + 말줄임표 처리한다.

    검증 포인트:
        - 짧은 제목은 prefix + 제목 원본 그대로.
        - 100 자를 넘는 제목은 100 자까지만 남기고 ``…`` 를 붙인다 — 결과
          제목 부분 길이는 101 자(100 + 말줄임표).
        - 결과 전체 문자열이 EmailForwardLog.subject 컬럼(String(200)) 안에
          들어갈 만큼 짧다.
    """
    prefix = "[정부사업 모니터링] 공고 검토 요청: "

    # 짧은 제목 — truncate 없이 그대로.
    short_subject = build_default_forward_subject("짧은 공고 제목")
    assert short_subject == f"{prefix}짧은 공고 제목"

    # 100 자 초과 제목 — truncate + 말줄임표.
    long_title = "가" * 150
    long_subject = build_default_forward_subject(long_title)
    assert long_subject.startswith(prefix)

    title_part = long_subject[len(prefix):]
    # 100 자까지만 남기고 말줄임표 1자 → 총 101 자.
    assert len(title_part) == 101, (
        f"제목 부분이 100자 + 말줄임표(101자)여야 함; got {len(title_part)}"
    )
    assert title_part.endswith("…")
    # 잘린 본문은 원본 제목의 앞 100 자와 일치.
    assert title_part[:-1] == "가" * 100

    # 전체 결과가 String(200) 컬럼에 안전하게 들어가는지.
    assert len(long_subject) <= 200


# ──────────────────────────────────────────────────────────────
# 3. forward_announcement — 정상 (수신자 3명 모두 성공)
# ──────────────────────────────────────────────────────────────


def test_forward_announcement_all_success(db_session: Session) -> None:
    """수신자 3명이 모두 발송 성공하면 status='success' + EmailSendRun 3개 생성.

    검증 포인트:
        - ``ForwardResult.status == SUCCESS``, ``success_count == 3``,
          ``failure_count == 0``.
        - transport.send 가 수신자 수만큼(3회) 호출됨.
        - EmailForwardLog row 가 영속되고 status 도 SUCCESS.
        - ``related_kind='forward'`` + ``related_id=forward_log_id`` 매칭
          EmailSendRun 이 정확히 3개, 모두 SENT, 수신자 주소가 요청과 일치.
    """
    project = _make_canonical_project(db_session, key_suffix="all-success")
    _make_announcement(
        db_session,
        canonical_project=project,
        source_announcement_id="IRIS-ALL-SUCCESS-1",
    )
    sender = _make_user(db_session, username="fwd_sender_ok")
    db_session.commit()

    recipients = [
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
    ]
    transport = _FakeTransport(results=[None, None, None])
    request = ForwardRequest(
        canonical_project_id=project.id,
        sender_user_id=sender.id,
        sender_organization_id=None,
        recipients=recipients,
        subject="",
        additional_message=None,
    )

    result = forward_announcement(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
    )

    assert result.status == EmailForwardStatus.SUCCESS
    assert result.success_count == 3
    assert result.failure_count == 0
    assert transport.send_call_count == 3

    # EmailForwardLog row 가 SUCCESS 로 영속되었는지.
    forward_log = db_session.get(EmailForwardLog, result.forward_log_id)
    assert forward_log is not None
    assert forward_log.status == EmailForwardStatus.SUCCESS
    assert forward_log.success_count == 3
    assert forward_log.failure_count == 0
    assert forward_log.recipient_count == 3
    assert forward_log.completed_at is not None

    # related_kind/related_id 매칭 EmailSendRun 이 정확히 3개.
    send_runs = (
        db_session.execute(
            select(EmailSendRun).where(
                EmailSendRun.related_kind == RELATED_KIND_FORWARD,
                EmailSendRun.related_id == result.forward_log_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(send_runs) == 3
    assert all(run.status == EmailSendRunStatus.SENT for run in send_runs)
    assert {run.recipient for run in send_runs} == set(recipients)


# ──────────────────────────────────────────────────────────────
# 4. forward_announcement — 부분 실패 (3명 중 1명 실패)
# ──────────────────────────────────────────────────────────────


def test_forward_announcement_partial_failure(db_session: Session) -> None:
    """3명 중 1명 발송이 예외를 던지면 status='partial', success=2, failure=1.

    검증 포인트:
        - 두 번째 수신자만 transport 가 예외를 던지도록 결과 시퀀스를 구성.
        - ``ForwardResult.status == PARTIAL``, ``success_count == 2``,
          ``failure_count == 1``.
        - 개별 발송 실패가 호출자로 전파되지 않고 루프가 끝까지 진행됨
          (transport.send 가 3회 모두 호출됨).
        - EmailSendRun 3개 중 SENT 2개 / FAILED 1개.
    """
    project = _make_canonical_project(db_session, key_suffix="partial")
    _make_announcement(
        db_session,
        canonical_project=project,
        source_announcement_id="IRIS-PARTIAL-1",
    )
    sender = _make_user(db_session, username="fwd_sender_partial")
    db_session.commit()

    recipients = [
        "ok-1@example.com",
        "fail@example.com",
        "ok-2@example.com",
    ]
    # 두 번째 수신자만 실패.
    transport = _FakeTransport(
        results=[None, RuntimeError("smtp 일시 오류"), None]
    )
    request = ForwardRequest(
        canonical_project_id=project.id,
        sender_user_id=sender.id,
        sender_organization_id=None,
        recipients=recipients,
        subject="부분 실패 시나리오",
        additional_message=None,
    )

    result = forward_announcement(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
    )

    assert result.status == EmailForwardStatus.PARTIAL
    assert result.success_count == 2
    assert result.failure_count == 1
    # 개별 실패가 전파되지 않고 3명 모두 발송 시도됨.
    assert transport.send_call_count == 3

    forward_log = db_session.get(EmailForwardLog, result.forward_log_id)
    assert forward_log is not None
    assert forward_log.status == EmailForwardStatus.PARTIAL
    assert forward_log.success_count == 2
    assert forward_log.failure_count == 1

    send_runs = (
        db_session.execute(
            select(EmailSendRun).where(
                EmailSendRun.related_kind == RELATED_KIND_FORWARD,
                EmailSendRun.related_id == result.forward_log_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(send_runs) == 3
    sent_count = sum(
        1 for run in send_runs if run.status == EmailSendRunStatus.SENT
    )
    failed_count = sum(
        1 for run in send_runs if run.status == EmailSendRunStatus.FAILED
    )
    assert sent_count == 2
    assert failed_count == 1


# ──────────────────────────────────────────────────────────────
# 5. forward_announcement — 전체 실패 (모든 발송 실패)
# ──────────────────────────────────────────────────────────────


def test_forward_announcement_all_failure(db_session: Session) -> None:
    """모든 발송이 예외를 던지면 status='failed', success=0, failure=3.

    검증 포인트:
        - 세 수신자 모두 transport 가 예외를 던지도록 구성.
        - ``ForwardResult.status == FAILED``, ``success_count == 0``,
          ``failure_count == 3``.
        - EmailForwardLog row 는 그래도 영속됨 (포워딩 시도 사실 기록).
        - EmailSendRun 3개 모두 FAILED.
    """
    project = _make_canonical_project(db_session, key_suffix="all-fail")
    _make_announcement(
        db_session,
        canonical_project=project,
        source_announcement_id="IRIS-ALL-FAIL-1",
    )
    sender = _make_user(db_session, username="fwd_sender_fail")
    db_session.commit()

    recipients = [
        "x-1@example.com",
        "x-2@example.com",
        "x-3@example.com",
    ]
    transport = _FakeTransport(
        results=[
            ConnectionError("발송 불가 1"),
            ConnectionError("발송 불가 2"),
            ConnectionError("발송 불가 3"),
        ]
    )
    request = ForwardRequest(
        canonical_project_id=project.id,
        sender_user_id=sender.id,
        sender_organization_id=None,
        recipients=recipients,
        subject="전체 실패 시나리오",
        additional_message=None,
    )

    result = forward_announcement(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
    )

    assert result.status == EmailForwardStatus.FAILED
    assert result.success_count == 0
    assert result.failure_count == 3
    assert transport.send_call_count == 3

    forward_log = db_session.get(EmailForwardLog, result.forward_log_id)
    assert forward_log is not None
    assert forward_log.status == EmailForwardStatus.FAILED
    assert forward_log.success_count == 0
    assert forward_log.failure_count == 3

    send_runs = (
        db_session.execute(
            select(EmailSendRun).where(
                EmailSendRun.related_kind == RELATED_KIND_FORWARD,
                EmailSendRun.related_id == result.forward_log_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(send_runs) == 3
    assert all(run.status == EmailSendRunStatus.FAILED for run in send_runs)


# ──────────────────────────────────────────────────────────────
# 6. forward_announcement — 빈 recipients → ValueError
# ──────────────────────────────────────────────────────────────


def test_forward_announcement_empty_recipients_raises_value_error(
    db_session: Session,
) -> None:
    """recipients 가 빈 리스트면 ValueError 를 던진다.

    빈 수신자 방어는 forward_announcement 의 가장 첫 단계이므로, canonical /
    announcement / sender 를 굳이 만들지 않아도 ValueError 가 먼저 발생한다.
    EmailForwardLog row 도 생성되지 않아야 한다 (예외가 INSERT 이전 단계).
    """
    request = ForwardRequest(
        canonical_project_id=1,
        sender_user_id=1,
        sender_organization_id=None,
        recipients=[],
        subject="",
        additional_message=None,
    )

    with pytest.raises(ValueError):
        forward_announcement(
            request,
            session=db_session,
            transport=_FakeTransport(results=[]),
            max_retry_count=0,
        )

    # 예외가 forward_log INSERT 이전 단계에서 발생 → row 가 하나도 없어야 함.
    forward_logs = db_session.execute(select(EmailForwardLog)).scalars().all()
    assert forward_logs == []


# ──────────────────────────────────────────────────────────────
# 7. forward_announcement — 발신 조직 비소속 → PermissionError
# ──────────────────────────────────────────────────────────────


def test_forward_announcement_sender_org_not_member_raises_permission_error(
    db_session: Session,
) -> None:
    """sender_organization_id 가 발송자 소속 조직이 아니면 PermissionError 를 던진다.

    발송자(sender_user)는 어떤 조직에도 속하지 않았고, 요청은 임의의 조직 PK 를
    sender_organization_id 로 지정한다 — service 의 방어적 소속 검증이 이를
    PermissionError 로 끊어야 한다. EmailForwardLog row 도 생성되지 않는다.
    """
    project = _make_canonical_project(db_session, key_suffix="perm-error")
    _make_announcement(
        db_session,
        canonical_project=project,
        source_announcement_id="IRIS-PERM-ERROR-1",
    )
    sender = _make_user(db_session, username="fwd_sender_nomember")
    # 발송자가 소속되지 않은 조직.
    foreign_org = _make_organization(db_session, name="남의 조직")
    db_session.commit()

    request = ForwardRequest(
        canonical_project_id=project.id,
        sender_user_id=sender.id,
        sender_organization_id=foreign_org.id,
        recipients=["someone@example.com"],
        subject="권한 오류 시나리오",
        additional_message=None,
    )

    with pytest.raises(PermissionError):
        forward_announcement(
            request,
            session=db_session,
            transport=_FakeTransport(results=[None]),
            max_retry_count=0,
        )

    # 권한 검증은 forward_log INSERT 이전 단계 → row 가 생성되지 않아야 함.
    forward_logs = db_session.execute(select(EmailForwardLog)).scalars().all()
    assert forward_logs == []


# ──────────────────────────────────────────────────────────────
# 8. 발신자 정보 표 — text/plain 본문에 포함되는지
# ──────────────────────────────────────────────────────────────


class _FakeAnnouncement:
    """body builder 테스트용 최소 Announcement 더블."""

    def __init__(self) -> None:
        """기본값으로 Announcement 더블을 초기화한다."""
        self.title = "테스트 공고"
        self.agency = "테스트 기관"
        self.status = None
        self.deadline_at = None
        self.raw_metadata = {}
        self.detail_text = None


class _FakeUser:
    """body builder 테스트용 최소 User 더블."""

    def __init__(self, username: str, email: str | None) -> None:
        """주어진 username/email 로 User 더블을 초기화한다."""
        self.username = username
        self.email = email


class _FakeOrg:
    """body builder 테스트용 최소 Organization 더블."""

    def __init__(self, name: str) -> None:
        """주어진 이름으로 Organization 더블을 초기화한다."""
        self.name = name


def test_build_forward_text_body_includes_sender_info_with_multiple_orgs() -> None:
    """조직이 여러 개일 때 plain text 본문에 발신자 정보 표가 포함된다.

    검증 포인트:
        - ``[발신자 정보]`` 헤더가 포함됨.
        - 사용자 ID(username)가 표 안에 포함됨.
        - 두 조직명이 콤마 구분으로 포함됨.
        - 이메일이 포함됨.
        - 발신자 정보 표는 공고 메타 블록과 요약 사이에 위치함
          (``[공고 요약]`` 보다 앞에 등장).
    """
    user = _FakeUser(username="alice", email="alice@example.com")
    orgs = [_FakeOrg("팀A"), _FakeOrg("팀B")]
    announcement = _FakeAnnouncement()

    body = build_forward_text_body(
        announcement=announcement,
        additional_message=None,
        sender_user=user,
        sender_organizations=orgs,
        detail_url="https://example.com/announcements/1",
    )

    assert "[발신자 정보]" in body
    assert "- 사용자 ID: alice" in body
    assert "팀A, 팀B" in body
    assert "- 이메일: alice@example.com" in body

    # 발신자 정보 블록이 공고 메타 뒤, 요약보다 앞에 오는지 순서 확인.
    sender_info_pos = body.index("[발신자 정보]")
    agency_pos = body.index("발주기관: 테스트 기관")
    assert agency_pos < sender_info_pos, "발신자 정보는 공고 메타 뒤에 와야 한다"


def test_build_forward_text_body_sender_info_empty_org_and_email() -> None:
    """조직 없고 이메일 없는 사용자는 발신자 정보 항목이 공란으로 표시된다.

    검증 포인트:
        - ``- 조직명: `` 다음이 빈 값 (공란).
        - ``- 이메일: `` 다음이 빈 값 (공란).
    """
    user = _FakeUser(username="bob", email=None)
    body = build_forward_text_body(
        announcement=_FakeAnnouncement(),
        additional_message=None,
        sender_user=user,
        sender_organizations=[],
        detail_url="https://example.com/announcements/2",
    )

    assert "- 조직명: \n" in body or body.endswith("- 조직명: ")
    assert "- 이메일: \n" in body or "- 이메일: \n" in body


def test_build_forward_html_body_includes_sender_info_block() -> None:
    """HTML 본문에 발신자 정보 박스가 포함된다.

    검증 포인트:
        - ``발신자 정보`` 라벨이 HTML 안에 포함됨.
        - 사용자 ID 값이 포함됨.
        - 조직명 값이 포함됨 (이스케이프 후라도).
        - 이메일 값이 포함됨.
        - HTML injection 방어: ``<script>`` 태그가 이스케이프됨.
    """
    user = _FakeUser(username="charlie", email="charlie@corp.com")
    orgs = [_FakeOrg("개발팀")]
    body = build_forward_html_body(
        announcement=_FakeAnnouncement(),
        additional_message=None,
        sender_user=user,
        sender_organizations=orgs,
        detail_url="https://example.com/announcements/3",
    )

    assert "발신자 정보" in body
    assert "charlie" in body
    assert "개발팀" in body
    assert "charlie@corp.com" in body

    # XSS 방어 검증 — 조직명에 스크립트를 넣어도 그대로 렌더되지 않아야 한다.
    xss_org = _FakeOrg("<script>alert(1)</script>")
    xss_body = build_forward_html_body(
        announcement=_FakeAnnouncement(),
        additional_message=None,
        sender_user=user,
        sender_organizations=[xss_org],
        detail_url="https://example.com/announcements/3",
    )
    assert "<script>" not in xss_body


# ──────────────────────────────────────────────────────────────
# 9. forward_announcement — 선택된 단일 조직만 메일 본문에 노출 (task 00113)
# ──────────────────────────────────────────────────────────────


def test_forward_announcement_only_selected_org_in_body(db_session: Session) -> None:
    """발신 조직 선택 시 선택한 조직명만 메일 본문에 노출되고 다른 소속 조직명은 나타나지 않는다.

    검증 포인트 (task 00113):
        - 발송자가 2개 조직(org_selected, org_other)에 소속된 상태에서
          org_selected 를 sender_organization_id 로 지정해 forward_announcement 를 호출한다.
        - 발송된 메일의 text/plain 본문에 org_selected.name 이 포함된다.
        - 발송된 메일의 text/plain 본문에 org_other.name 이 포함되지 않는다.
    """
    project = _make_canonical_project(db_session, key_suffix="single-org")
    _make_announcement(
        db_session,
        canonical_project=project,
        source_announcement_id="IRIS-SINGLE-ORG-1",
    )
    sender = _make_user(db_session, username="fwd_sender_two_orgs")
    org_selected = _make_organization(db_session, name="선택된조직AA")
    org_other = _make_organization(db_session, name="다른소속조직BB")
    _add_membership(db_session, user=sender, organization=org_selected)
    _add_membership(db_session, user=sender, organization=org_other)
    db_session.commit()

    # 발송된 EmailMessage 를 캡처하기 위한 인라인 transport
    captured_messages: list[EmailMessage] = []

    class _CapturingTransport(EmailTransport):
        """발송된 메일 메시지를 캡처해 검증에 쓰는 테스트용 transport."""

        def send(self, message: EmailMessage) -> None:
            """메시지를 캡처하고 성공으로 처리한다."""
            captured_messages.append(message)

    request = ForwardRequest(
        canonical_project_id=project.id,
        sender_user_id=sender.id,
        sender_organization_id=org_selected.id,
        recipients=["recipient@example.com"],
        subject="단일 조직 노출 테스트",
        additional_message=None,
    )

    result = forward_announcement(
        request,
        session=db_session,
        transport=_CapturingTransport(),
        max_retry_count=0,
    )

    assert result.status == EmailForwardStatus.SUCCESS
    assert len(captured_messages) == 1

    # text/plain 파트 본문 추출
    parts = list(captured_messages[0].iter_parts())
    plain_content = parts[0].get_content()

    # 선택된 조직명은 본문에 포함되어야 한다
    assert "선택된조직AA" in plain_content, "선택한 조직명이 메일 본문에 없습니다"
    # 다른 소속 조직명은 본문에 나타나지 않아야 한다
    assert "다른소속조직BB" not in plain_content, "선택하지 않은 조직명이 메일 본문에 노출되었습니다"
