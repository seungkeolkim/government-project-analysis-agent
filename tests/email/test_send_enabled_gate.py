"""``email.send_enabled`` 게이트 단위 테스트 (task 00115-1).

검증 시나리오:
    1. ``is_email_sending_enabled`` — row 없음 → False (default off).
    2. ``is_email_sending_enabled`` — "true" 저장 → True.
    3. ``is_email_sending_enabled`` — "false" 저장 → False.
    4. ``is_email_sending_enabled`` — "TRUE" (대문자) → True (case-insensitive).
    5. ``is_email_sending_enabled`` — 예상 외 값 ("yes") → False + warning.
    6. ``forward_announcement`` — 게이트 off 시 ``EmailSendingDisabledError`` raise
       + ``EmailForwardLog`` row 가 DB 에 생성되지 않음 (SELECT count 검증).
    7. ``forward_announcement`` — 게이트 on 시 정상 진행 (EmailForwardLog row 생성).

DB:
    conftest.py 의 ``test_engine`` + ``db_session`` fixture 사용 — 격리된 SQLite.

transport mock:
    ``tests/email/test_forwarding.py`` 의 _FakeTransport 패턴 재사용.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.backup.service import set_setting
from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    EmailForwardLog,
    User,
)
from app.email.constants import SETTING_KEY_EMAIL_SEND_ENABLED
from app.email.forwarding import ForwardRequest, forward_announcement
from app.email.gate import EmailSendingDisabledError, is_email_sending_enabled
from app.email.transport.base import EmailTransport
from app.sources.constants import SOURCE_TYPE_IRIS


# ──────────────────────────────────────────────────────────────
# 테스트 더블 / autouse fixture
# ──────────────────────────────────────────────────────────────


class _FakeTransport(EmailTransport):
    """테스트용 transport 구현체 (항상 성공)."""

    def __init__(self) -> None:
        """transport 를 초기화한다."""
        self.send_call_count: int = 0

    def send(self, message: EmailMessage) -> None:
        """호출 횟수를 증가시키고 정상 반환한다."""
        self.send_call_count += 1


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """``time.sleep`` 을 no-op 으로 patch 해 테스트가 실제로 대기하지 않게 한다."""
    monkeypatch.setattr("app.email.sender.time.sleep", lambda _seconds: None)


# ──────────────────────────────────────────────────────────────
# DB row 생성 헬퍼
# ──────────────────────────────────────────────────────────────


def _make_canonical_and_announcement(
    session: Session,
    *,
    key_suffix: str,
) -> tuple[CanonicalProject, Announcement]:
    """테스트용 CanonicalProject + Announcement row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        key_suffix: canonical_key 뒤에 붙는 식별 suffix (테스트 간 중복 방지).

    Returns:
        flush 된 (CanonicalProject, Announcement) 튜플.
    """
    project = CanonicalProject(
        canonical_key=f"official:gate-test-{key_suffix}",
        key_scheme="official",
    )
    session.add(project)
    session.flush()

    announcement = Announcement(
        source_announcement_id=f"gate-ann-{key_suffix}",
        source_type=SOURCE_TYPE_IRIS,
        title="게이트 테스트 공고",
        agency="테스트 기관",
        status=AnnouncementStatus.RECEIVING,
        canonical_group_id=project.id,
        is_current=True,
    )
    session.add(announcement)
    session.flush()
    return project, announcement


def _make_user(session: Session, *, username: str) -> User:
    """테스트용 User row 를 생성하고 flush 후 반환한다.

    Args:
        session: 대상 ORM 세션.
        username: UNIQUE 제약 만족용 고유 로그인 ID.

    Returns:
        flush 된 User 인스턴스.
    """
    user = User(
        username=username,
        password_hash="x",
        is_admin=False,
    )
    session.add(user)
    session.flush()
    return user


# ──────────────────────────────────────────────────────────────
# 1~5. is_email_sending_enabled 단위 테스트
# ──────────────────────────────────────────────────────────────


def test_is_email_sending_enabled_default_off(db_session: Session) -> None:
    """SystemSetting row 가 없으면 기본값 False 를 반환한다."""
    assert is_email_sending_enabled(db_session) is False


def test_is_email_sending_enabled_true(db_session: Session) -> None:
    """\"true\" 로 저장된 경우 True 를 반환한다."""
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "true")
    db_session.flush()
    assert is_email_sending_enabled(db_session) is True


def test_is_email_sending_enabled_false(db_session: Session) -> None:
    """\"false\" 로 저장된 경우 False 를 반환한다."""
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "false")
    db_session.flush()
    assert is_email_sending_enabled(db_session) is False


def test_is_email_sending_enabled_case_insensitive(db_session: Session) -> None:
    """대문자 \"TRUE\" 도 True 로 읽힌다 (case-insensitive)."""
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "TRUE")
    db_session.flush()
    assert is_email_sending_enabled(db_session) is True


def test_is_email_sending_enabled_unexpected_value_fallback(
    db_session: Session,
) -> None:
    """예상 범위 밖의 값(\"yes\") 은 False 로 fallback 한다."""
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "yes")
    db_session.flush()
    assert is_email_sending_enabled(db_session) is False


# ──────────────────────────────────────────────────────────────
# 6. forward_announcement — 게이트 off 시 차단 + EmailForwardLog row 없음
# ──────────────────────────────────────────────────────────────


def test_forward_announcement_blocked_when_send_disabled(
    db_session: Session,
) -> None:
    """메일 전송 기능 off 시 ``EmailSendingDisabledError`` 가 raise 되고,
    ``EmailForwardLog`` row 가 DB 에 생성되지 않는다.
    """
    # 게이트 off — row 없음이면 default False 이므로 별도 set 불필요.
    # (명시적으로 "false" 를 설정해 의도를 분명히 한다.)
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "false")
    db_session.commit()

    project, _ = _make_canonical_and_announcement(db_session, key_suffix="gate-off")
    user = _make_user(db_session, username="gate-off-user")
    db_session.commit()

    transport = _FakeTransport()
    request = ForwardRequest(
        canonical_project_id=project.id,
        sender_user_id=user.id,
        sender_organization_id=None,
        recipients=["recv@example.com"],
        subject="게이트 off 테스트",
        additional_message=None,
    )

    with pytest.raises(EmailSendingDisabledError):
        forward_announcement(
            request,
            session=db_session,
            transport=transport,
            max_retry_count=0,
        )

    # EmailForwardLog row 가 생성되지 않아야 한다.
    count = db_session.execute(
        select(func.count()).select_from(EmailForwardLog)
    ).scalar_one()
    assert count == 0, "게이트 off 시 EmailForwardLog row 가 생성되어서는 안 됩니다."

    # transport.send 도 호출되지 않아야 한다.
    assert transport.send_call_count == 0


# ──────────────────────────────────────────────────────────────
# 7. forward_announcement — 게이트 on 시 정상 진행 (EmailForwardLog row 생성)
# ──────────────────────────────────────────────────────────────


def test_forward_announcement_proceeds_when_send_enabled(
    db_session: Session,
) -> None:
    """메일 전송 기능 on 시 ``forward_announcement`` 가 정상적으로 진행되고
    ``EmailForwardLog`` row 가 1건 생성된다.
    """
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "true")
    db_session.commit()

    project, _ = _make_canonical_and_announcement(db_session, key_suffix="gate-on")
    user = _make_user(db_session, username="gate-on-user")
    db_session.commit()

    transport = _FakeTransport()
    request = ForwardRequest(
        canonical_project_id=project.id,
        sender_user_id=user.id,
        sender_organization_id=None,
        recipients=["recv@example.com"],
        subject="게이트 on 테스트",
        additional_message=None,
    )

    result = forward_announcement(
        request,
        session=db_session,
        transport=transport,
        max_retry_count=0,
    )

    assert result.success_count == 1
    assert result.failure_count == 0

    # EmailForwardLog row 가 정확히 1건 생성되어야 한다.
    count = db_session.execute(
        select(func.count()).select_from(EmailForwardLog)
    ).scalar_one()
    assert count == 1, "게이트 on 시 EmailForwardLog row 가 1건 있어야 합니다."
