"""공고 포워딩 라우터 (routes/forward.py) 단위 테스트 (Phase A-2 Part 2 / task 00109-7).

검증 시나리오 (첨부 phase_a2_part2_prompt.md "검증 > Agent 작성 단위 테스트"
tests/web/test_forward_routes.py 항목 그대로):

    POST /api/canonical/{id}/forward
        1. 비로그인 → 401
        2. recipients=[] → 422
        3. recipients 51개 → 422
        4. additional_message 5001자 → 422
        5. 없는 canonical → 404
        6. sender_organization_id 가 본인 소속 아님 → 403
        7. 정상 → 200 + 응답 스키마

    GET /api/canonical/{id}/forward-logs
        8. 비로그인 허용 + 응답 스키마

    GET /api/canonical/{id}/forward-logs/{forward_log_id}/sends
        9. 응답 스키마

    GET /api/users/search
        10. 비로그인 → 401
        11. 정상 → 응답 스키마 (email IS NOT NULL 인 사용자만)

fixture 패턴:
    ``tests/web/test_admin_email_api.py`` 와 동일하게 ``test_engine`` 격리 DB +
    ``TestClient`` + 폼 로그인. 사용자/조직/공고 등 사전 데이터는 ``db_session``
    으로 만들고 commit 한다 — 라우터는 ``session_scope()`` 로 별도 세션을 열지만
    동일 SQLite 파일을 보므로 commit 된 데이터가 보인다.

주의:
    ``TestClient.delete()`` 는 ``json=`` kwarg 를 지원하지 않으므로 본문 있는
    DELETE 는 ``client.request("DELETE", url, json=...)`` 를 써야 한다. 본
    task 의 4개 endpoint 에 DELETE 는 없지만 패턴으로 인지해 둔다.
"""

from __future__ import annotations

from collections.abc import Iterator
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
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
    UserOrganization,
)
from app.backup.service import set_setting
from app.email.constants import RELATED_KIND_FORWARD, SETTING_KEY_EMAIL_SEND_ENABLED
from app.email.transport.base import EmailTransport
from app.sources.constants import SOURCE_TYPE_IRIS
from app.timezone import now_utc


# ──────────────────────────────────────────────────────────────
# 공통 fixture / 헬퍼
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """격리된 DB 가 적용된 FastAPI TestClient."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, username: str, password: str) -> None:
    """``/auth/login`` 폼 호출. 성공 시 303 응답이어야 한다."""
    response = client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"로그인 실패: {response.status_code}"


@pytest.fixture
def logged_in_client(client: TestClient, db_session: Session) -> TestClient:
    """일반 로그인 사용자로 인증된 TestClient.

    POST /forward 와 GET /api/users/search 가 ``current_user_required`` 로
    보호되므로, 정상 흐름 테스트는 본 fixture 로 로그인 상태를 만든다.
    """
    from app.auth.service import create_user

    create_user(
        db_session,
        username="fwd_user",
        password="Fwd_pass_1!",
        email="fwd_user@example.com",
    )
    db_session.commit()
    _login(client, "fwd_user", "Fwd_pass_1!")
    return client


@pytest.fixture(autouse=True)
def _enable_email_sending(db_session: Session) -> None:
    """본 파일의 모든 테스트에서 메일 전송 게이트를 on 으로 설정한다.

    task 00115-1 에서 도입된 ``email.send_enabled`` 게이트의 default 가
    False 이므로, 포워딩 라우터 기능 자체를 검증하는 이 파일의 테스트들은
    게이트를 미리 켜 두어야 한다.
    """
    set_setting(db_session, SETTING_KEY_EMAIL_SEND_ENABLED, "true")
    db_session.commit()


class _FakeRouteTransport(EmailTransport):
    """라우터 정상 흐름 테스트용 transport — 항상 발송 성공.

    POST /forward 정상(200) 테스트는 실제 msal/smtplib 까지 내려가면 안 되므로,
    ``build_transport_from_settings`` 를 본 클래스 인스턴스로 monkeypatch 한다.
    """

    def send(self, message: EmailMessage) -> None:
        """발송 성공을 시뮬레이션한다 (아무 것도 하지 않음)."""
        return None


def _make_canonical_with_announcement(
    session: Session,
    *,
    key_suffix: str,
    with_announcement: bool = True,
) -> CanonicalProject:
    """테스트용 CanonicalProject (+ 현재 유효 Announcement) 를 생성하고 반환한다.

    Args:
        session: 대상 ORM 세션.
        key_suffix: ``canonical_key`` / ``source_announcement_id`` 의 식별 suffix.
        with_announcement: True 면 ``is_current=True`` Announcement 1건도 함께
            생성한다. POST /forward 정상 흐름은 announcement 가 있어야 하지만,
            "없는 canonical → 404" 같은 케이스는 canonical 만 필요하다.

    Returns:
        flush 된 CanonicalProject 인스턴스.
    """
    project = CanonicalProject(
        canonical_key=f"official:fwd-route-{key_suffix}",
        key_scheme="official",
    )
    session.add(project)
    session.flush()

    if with_announcement:
        announcement = Announcement(
            source_announcement_id=f"IRIS-ROUTE-{key_suffix}",
            source_type=SOURCE_TYPE_IRIS,
            title="라우터 테스트 공고 제목",
            agency="라우터 테스트 기관",
            status=AnnouncementStatus.RECEIVING,
            canonical_group_id=project.id,
            is_current=True,
        )
        session.add(announcement)
        session.flush()

    return project


# ──────────────────────────────────────────────────────────────
# 1. POST /forward — 비로그인 → 401
# ──────────────────────────────────────────────────────────────


def test_post_forward_unauthenticated_401(
    client: TestClient,
    db_session: Session,
) -> None:
    """비로그인 사용자의 POST /forward 는 401 을 받는다.

    ``ensure_same_origin`` 은 TestClient 가 Origin/Referer 헤더를 보내지 않아
    통과하고, ``current_user_required`` 가 비로그인을 401 로 끊는다.
    """
    project = _make_canonical_with_announcement(db_session, key_suffix="401")
    db_session.commit()

    response = client.post(
        f"/api/canonical/{project.id}/forward",
        json={"recipients": ["someone@example.com"]},
        follow_redirects=False,
    )
    assert response.status_code == 401, response.text


# ──────────────────────────────────────────────────────────────
# 2. POST /forward — recipients=[] → 422
# ──────────────────────────────────────────────────────────────


def test_post_forward_empty_recipients_422(
    logged_in_client: TestClient,
    db_session: Session,
) -> None:
    """recipients 가 빈 리스트면 Pydantic 검증 단계에서 422 를 받는다."""
    project = _make_canonical_with_announcement(
        db_session, key_suffix="empty-recipients"
    )
    db_session.commit()

    response = logged_in_client.post(
        f"/api/canonical/{project.id}/forward",
        json={"recipients": []},
    )
    assert response.status_code == 422, response.text


# ──────────────────────────────────────────────────────────────
# 3. POST /forward — recipients 51개 → 422
# ──────────────────────────────────────────────────────────────


def test_post_forward_too_many_recipients_422(
    logged_in_client: TestClient,
    db_session: Session,
) -> None:
    """recipients 가 50개를 초과(51개)하면 Pydantic 검증 단계에서 422 를 받는다."""
    project = _make_canonical_with_announcement(
        db_session, key_suffix="too-many"
    )
    db_session.commit()

    recipients = [f"user{index}@example.com" for index in range(51)]
    response = logged_in_client.post(
        f"/api/canonical/{project.id}/forward",
        json={"recipients": recipients},
    )
    assert response.status_code == 422, response.text


# ──────────────────────────────────────────────────────────────
# 4. POST /forward — additional_message 5001자 → 422
# ──────────────────────────────────────────────────────────────


def test_post_forward_additional_message_too_long_422(
    logged_in_client: TestClient,
    db_session: Session,
) -> None:
    """additional_message 가 5000자를 초과(5001자)하면 422 를 받는다."""
    project = _make_canonical_with_announcement(
        db_session, key_suffix="msg-too-long"
    )
    db_session.commit()

    response = logged_in_client.post(
        f"/api/canonical/{project.id}/forward",
        json={
            "recipients": ["someone@example.com"],
            "additional_message": "가" * 5001,
        },
    )
    assert response.status_code == 422, response.text


# ──────────────────────────────────────────────────────────────
# 5. POST /forward — 없는 canonical → 404
# ──────────────────────────────────────────────────────────────


def test_post_forward_unknown_canonical_404(
    logged_in_client: TestClient,
) -> None:
    """존재하지 않는 canonical_id 로 POST /forward 하면 404 를 받는다.

    ``_ensure_canonical_exists`` 가 transport 구성보다 먼저 실행되므로,
    transport mock 없이도 404 가 반환된다.
    """
    response = logged_in_client.post(
        "/api/canonical/999999/forward",
        json={"recipients": ["someone@example.com"]},
    )
    assert response.status_code == 404, response.text


# ──────────────────────────────────────────────────────────────
# 6. POST /forward — 소속 아닌 조직 → 403
# ──────────────────────────────────────────────────────────────


def test_post_forward_foreign_organization_403(
    logged_in_client: TestClient,
    db_session: Session,
) -> None:
    """sender_organization_id 가 본인 소속이 아니면 403 을 받는다.

    로그인 사용자(fwd_user)는 어떤 조직에도 속하지 않았고, 요청은 임의의 조직
    PK 를 sender_organization_id 로 지정한다 — 라우터의 소속 검증이 403 으로
    끊어야 한다.
    """
    project = _make_canonical_with_announcement(
        db_session, key_suffix="foreign-org"
    )
    foreign_org = Organization(name="남의 조직")
    db_session.add(foreign_org)
    db_session.flush()
    db_session.commit()

    response = logged_in_client.post(
        f"/api/canonical/{project.id}/forward",
        json={
            "recipients": ["someone@example.com"],
            "sender_organization_id": foreign_org.id,
        },
    )
    assert response.status_code == 403, response.text


# ──────────────────────────────────────────────────────────────
# 7. POST /forward — 정상 → 200 + 응답 스키마
# ──────────────────────────────────────────────────────────────


def test_post_forward_success_200_and_schema(
    logged_in_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정상 발송 시 200 과 {forward_log_id, status, success_count, failure_count} 응답.

    실제 msal/smtplib 로 내려가지 않도록 ``build_transport_from_settings`` 를
    항상 성공하는 ``_FakeRouteTransport`` 로 monkeypatch 한다. ``send_with_retry``
    의 재시도 sleep 도 차단한다.
    """
    project = _make_canonical_with_announcement(
        db_session, key_suffix="success-200"
    )
    db_session.commit()

    # 라우터가 import 한 이름을 patch — 항상 성공하는 transport 를 돌려준다.
    monkeypatch.setattr(
        "app.web.routes.forward.build_transport_from_settings",
        lambda session: _FakeRouteTransport(),
    )
    monkeypatch.setattr("app.email.sender.time.sleep", lambda _seconds: None)

    response = logged_in_client.post(
        f"/api/canonical/{project.id}/forward",
        json={
            "recipients": ["alice@example.com", "bob@example.com"],
        },
    )
    assert response.status_code == 200, response.text

    data = response.json()
    assert set(data.keys()) == {
        "forward_log_id",
        "status",
        "success_count",
        "failure_count",
    }
    assert isinstance(data["forward_log_id"], int)
    assert data["status"] == "success"
    assert data["success_count"] == 2
    assert data["failure_count"] == 0


# ──────────────────────────────────────────────────────────────
# 8. GET /forward-logs — 비로그인 허용 + 응답 스키마
# ──────────────────────────────────────────────────────────────


def test_get_forward_logs_unauthenticated_allowed_and_schema(
    client: TestClient,
    db_session: Session,
) -> None:
    """GET /forward-logs 는 비로그인도 허용하며, 응답 스키마가 prompt §4 와 일치한다.

    EmailForwardLog row 1건을 sender_user / sender_organization 와 함께 직접
    생성하고, 비로그인 client 로 조회해 직렬화 스키마를 검증한다.
    ``recipient_addresses`` 는 응답에서 제외되어야 한다.
    """
    from app.auth.service import create_user

    project = _make_canonical_with_announcement(
        db_session, key_suffix="get-logs"
    )
    sender = create_user(
        db_session,
        username="log_sender",
        password="Log_pass_1!",
        email="log_sender@example.com",
    )
    organization = Organization(name="발송 조직")
    db_session.add(organization)
    db_session.flush()

    forward_log = EmailForwardLog(
        canonical_project_id=project.id,
        sender_user_id=sender.id,
        sender_organization_id=organization.id,
        subject="발송 이력 제목",
        has_additional_message=True,
        recipient_addresses=["alice@example.com", "bob@example.com"],
        recipient_count=2,
        status=EmailForwardStatus.SUCCESS,
        success_count=2,
        failure_count=0,
        created_at=now_utc(),
        completed_at=now_utc(),
    )
    db_session.add(forward_log)
    db_session.commit()
    forward_log_id = forward_log.id

    # 비로그인 client 로 조회 — 허용되어야 한다.
    response = client.get(f"/api/canonical/{project.id}/forward-logs")
    assert response.status_code == 200, response.text

    data = response.json()
    assert len(data) == 1
    row = data[0]
    assert row["id"] == forward_log_id
    assert row["sender"]["username"] == "log_sender"
    assert row["sender_organization"]["name"] == "발송 조직"
    assert row["subject"] == "발송 이력 제목"
    assert row["recipient_count"] == 2
    assert row["has_additional_message"] is True
    assert row["status"] == "success"
    assert row["success_count"] == 2
    assert row["failure_count"] == 0
    assert row["created_at"] is not None
    assert row["completed_at"] is not None
    # 개별 수신자 주소는 목록 응답에서 제외 (expand 응답으로 분리).
    assert "recipient_addresses" not in row


# ──────────────────────────────────────────────────────────────
# 9. GET /forward-logs/{forward_log_id}/sends — 응답 스키마
# ──────────────────────────────────────────────────────────────


def test_get_forward_log_sends_schema(
    client: TestClient,
    db_session: Session,
) -> None:
    """GET /sends 가 수신자별 발송 시도 결과를 prompt §4 스키마대로 반환한다.

    EmailForwardLog 1건 + ``related_kind='forward'`` 매칭 EmailSendRun 1건을
    직접 생성하고, 비로그인 client 로 조회해 직렬화 스키마를 검증한다.
    """
    project = _make_canonical_with_announcement(
        db_session, key_suffix="get-sends"
    )
    forward_log = EmailForwardLog(
        canonical_project_id=project.id,
        subject="expand 대상 제목",
        has_additional_message=False,
        recipient_addresses=["alice@example.com"],
        recipient_count=1,
        status=EmailForwardStatus.SUCCESS,
        success_count=1,
        failure_count=0,
        created_at=now_utc(),
        completed_at=now_utc(),
    )
    db_session.add(forward_log)
    db_session.flush()

    send_run = EmailSendRun(
        recipient="alice@example.com",
        subject="expand 대상 제목",
        body_preview="본문 미리보기",
        transport_type="m365_oauth",
        status=EmailSendRunStatus.SENT,
        attempt_count=1,
        related_kind=RELATED_KIND_FORWARD,
        related_id=forward_log.id,
        sent_at=now_utc(),
        created_at=now_utc(),
    )
    db_session.add(send_run)
    db_session.commit()
    forward_log_id = forward_log.id

    response = client.get(
        f"/api/canonical/{project.id}/forward-logs/{forward_log_id}/sends"
    )
    assert response.status_code == 200, response.text

    data = response.json()
    assert len(data) == 1
    row = data[0]
    assert set(row.keys()) == {
        "id",
        "recipient",
        "status",
        "attempt_count",
        "error_message",
        "sent_at",
    }
    assert row["recipient"] == "alice@example.com"
    assert row["status"] == "sent"
    assert row["attempt_count"] == 1
    assert row["error_message"] is None
    assert row["sent_at"] is not None


# ──────────────────────────────────────────────────────────────
# 10. GET /api/users/search — 비로그인 → 401
# ──────────────────────────────────────────────────────────────


def test_get_users_search_unauthenticated_401(client: TestClient) -> None:
    """GET /api/users/search 는 로그인 전용 — 비로그인 호출은 401.

    ``q`` 는 필수 query 파라미터이므로 누락 시 422 가 되어 401 검증이
    흐려진다. 따라서 유효한 ``q`` 를 넘긴 상태에서 비로그인 401 을 확인한다.
    """
    response = client.get("/api/users/search", params={"q": "alice"})
    assert response.status_code == 401, response.text


# ──────────────────────────────────────────────────────────────
# 11. GET /api/users/search — 정상 → 응답 스키마 (email IS NOT NULL 만)
# ──────────────────────────────────────────────────────────────


def test_get_users_search_success_and_schema(
    logged_in_client: TestClient,
    db_session: Session,
) -> None:
    """정상 검색 시 email 보유 사용자만 prompt §4 스키마대로 반환한다.

    검증 포인트:
        - 응답이 ``{id, username, email, organizations}`` 키만 가진다.
        - email 이 있는 사용자는 결과에 포함된다.
        - email 이 NULL 인 사용자는 결과에서 제외된다.
        - 소속 조직이 ``organizations`` 에 nested 로 직렬화된다.
    """
    from app.auth.service import create_user

    # email 보유 사용자 — 검색 결과에 포함되어야 한다.
    user_with_email = create_user(
        db_session,
        username="searchable_alice",
        password="Srch_pass_1!",
        email="searchable_alice@example.com",
    )
    # email 이 없는 사용자 — 검색 결과에서 제외되어야 한다.
    create_user(
        db_session,
        username="searchable_bob",
        password="Srch_pass_1!",
        email=None,
    )
    organization = Organization(name="검색 결과 조직")
    db_session.add(organization)
    db_session.flush()
    db_session.add(
        UserOrganization(
            user_id=user_with_email.id,
            organization_id=organization.id,
        )
    )
    db_session.commit()

    response = logged_in_client.get(
        "/api/users/search", params={"q": "searchable"}
    )
    assert response.status_code == 200, response.text

    data = response.json()
    usernames = [row["username"] for row in data]
    # email 보유 사용자는 포함, email NULL 사용자는 제외.
    assert "searchable_alice" in usernames
    assert "searchable_bob" not in usernames

    alice_row = next(
        row for row in data if row["username"] == "searchable_alice"
    )
    assert set(alice_row.keys()) == {
        "id",
        "username",
        "email",
        "organizations",
    }
    assert alice_row["email"] == "searchable_alice@example.com"
    assert len(alice_row["organizations"]) == 1
    assert alice_row["organizations"][0]["name"] == "검색 결과 조직"
