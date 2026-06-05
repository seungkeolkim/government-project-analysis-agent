"""``/api/admin/email/daily-report/*`` 6 endpoint 통합 테스트 (Phase A-3 / task 00125-8).

검증 시나리오 (subtask 00125-8 의 acceptance_criteria + 디자인 노트 §9):

    1. 4종 endpoint 모두 비관리자 403 / 비로그인 401 (라우터 레벨 권한 보호 확인).
    2. PUT settings — cron 표현식 잘못된 값 → 422 (Pydantic validator).
    3. PUT settings — 정상 저장 → SystemSetting 반영 + ``register_daily_report_cron_schedule``
       호출 검증 (mock).
    4. POST test-send — recipient body 우선 / SystemSetting fallback / 게이트 비활성 503.
    5. POST send-now — 수신 대상 사용자 email 자동 수집 + ``recipients`` 가 전달되는지
       검증 (mock, admin 제약 없이 전체 사용자 — task 00144).
    6. GET runs / GET runs/{run_id}/sends — 응답 스키마 + 404 분기.

fixture 패턴:
    ``tests/web/test_admin_email_api.py`` 와 동일하게 ``test_engine`` 격리 DB +
    ``TestClient`` + 폼 로그인. task 00155-4 에서 APScheduler 가 제거돼 startup
    훅은 더 이상 스케줄러를 띄우지 않으므로 별도 stub 가 필요 없다. PUT settings
    저장 후의 crontab 재설치는 ``ENABLE_CRON`` 미설정(테스트 호스트)이라 graceful
    no-op 으로 동작한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.backup.service import get_setting, set_setting
from app.scheduler.constants import JOB_KIND_DAILY_REPORT
from app.scheduler.scheduled_job_store import get_singleton_schedule
from app.db.models import (
    EmailDailyReportRun,
    EmailDailyReportStatus,
    EmailSendRun,
    EmailSendRunStatus,
)
from app.email.constants import (
    RELATED_KIND_DAILY_REPORT,
    SETTING_KEY_DAILY_REPORT_TEST_RECIPIENT,
    SETTING_KEY_EMAIL_SEND_ENABLED,
)
from app.email.daily_report import DailyReportResult


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


def _register(client: TestClient, username: str, password: str) -> None:
    """``/auth/register`` 폼 호출. 성공 시 303 응답이어야 한다."""
    response = client.post(
        "/auth/register",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"회원가입 실패: {response.status_code}"


def _login(client: TestClient, username: str, password: str) -> None:
    """``/auth/login`` 폼 호출. 성공 시 303 응답이어야 한다."""
    response = client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"로그인 실패: {response.status_code}"


@pytest.fixture
def admin_client(client: TestClient, db_session: Session) -> TestClient:
    """관리자 (is_admin=True, email 정상) 로 로그인된 TestClient.

    GET /daily-report/settings 응답의 ``recipients`` 가 본 admin 1명을 포함
    하도록 email 까지 채운다. test-send / send-now 시 ``requested_by_user_id`` 도
    이 사용자 PK 로 기록된다.
    """
    from app.auth.service import create_user

    create_user(
        db_session,
        username="dr_admin",
        password="Admin_pass_1!",
        email="dr_admin@example.com",
        is_admin=True,
    )
    db_session.commit()
    _login(client, "dr_admin", "Admin_pass_1!")
    return client


# ──────────────────────────────────────────────────────────────
# 1. 권한 보호 — 비관리자 403 / 비로그인 401
# ──────────────────────────────────────────────────────────────


def test_daily_report_endpoints_require_admin_403(client: TestClient) -> None:
    """일반 사용자가 ``/api/admin/email/daily-report/*`` 6 endpoint 모두 호출 시 403.

    라우터 레벨 ``admin_user_required`` 가 공통으로 보호. 비로그인 401 은 별도
    테스트(``test_daily_report_endpoints_require_login_401``) 로 분리.
    """
    _register(client, "regular_user", "Regular_pass_1!")
    _login(client, "regular_user", "Regular_pass_1!")

    # 6 endpoint 각각 호출 — 모두 403 이어야 함.
    response = client.get(
        "/api/admin/email/daily-report/settings", follow_redirects=False
    )
    assert response.status_code == 403, response.text

    response = client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": False,
            "cron_expression": "",
            "test_recipient": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403, response.text

    response = client.post(
        "/api/admin/email/daily-report/test-send",
        json={"recipient": "ops@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 403, response.text

    response = client.post(
        "/api/admin/email/daily-report/send-now",
        json={},
        follow_redirects=False,
    )
    assert response.status_code == 403, response.text

    response = client.get(
        "/api/admin/email/daily-report/runs", follow_redirects=False
    )
    assert response.status_code == 403, response.text

    response = client.get(
        "/api/admin/email/daily-report/runs/1/sends", follow_redirects=False
    )
    assert response.status_code == 403, response.text


def test_daily_report_endpoints_require_login_401(client: TestClient) -> None:
    """비로그인 호출 시 ``/api/admin/email/daily-report/*`` 6 endpoint 모두 401.

    ``current_user_required`` 가 ``admin_user_required`` 보다 먼저 걸려 401 을
    돌려준다.
    """
    response = client.get(
        "/api/admin/email/daily-report/settings", follow_redirects=False
    )
    assert response.status_code == 401, response.text

    response = client.put(
        "/api/admin/email/daily-report/settings",
        json={"enabled": False, "cron_expression": "", "test_recipient": ""},
        follow_redirects=False,
    )
    assert response.status_code == 401, response.text

    response = client.get(
        "/api/admin/email/daily-report/runs/9999/sends", follow_redirects=False
    )
    assert response.status_code == 401, response.text


# ──────────────────────────────────────────────────────────────
# 2. GET /daily-report/settings — 응답 스키마 + 수신자 명단
# ──────────────────────────────────────────────────────────────


def test_get_daily_report_settings_default_response(
    admin_client: TestClient,
) -> None:
    """default 상태에서 GET /daily-report/settings 응답 스키마가 디자인 노트와 일치.

    검증:
        - enabled=False (default), cron_expression=DEFAULT, last_sent_at=None,
          test_recipient="", next_run_at=None (잡 미등록), recipients 1건 포함.
    """
    response = admin_client.get("/api/admin/email/daily-report/settings")
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["enabled"] is False
    assert data["cron_expression"] == "0 9 * * 1-5"
    assert data["last_sent_at"] is None
    assert data["test_recipient"] == ""
    assert data["next_run_at"] is None

    # recipients — admin_client fixture 가 dr_admin 1명을 만들었으므로 1건.
    recipients = data["recipients"]
    assert len(recipients) == 1
    assert recipients[0]["username"] == "dr_admin"
    assert recipients[0]["email"] == "dr_admin@example.com"
    assert recipients[0]["email_subscribed"] is True
    assert recipients[0]["eligible"] is True

    assert data["recipient_count_eligible"] == 1
    assert data["recipient_count_without_email"] == 0
    assert data["recipient_count_unsubscribed"] == 0


def test_get_daily_report_settings_recipient_overview(
    admin_client: TestClient,
    db_session: Session,
) -> None:
    """recipients 가 admin/비-admin 을 모두 포함하고 eligible/미설정/unsubscribed 를 분류.

    task 00144 — admin 제약 제거 후, 비-admin 사용자도 수신 대상 명단에 포함되고
    admin 이라도 email 미설정/수신거부면 제외(eligible=False)됨을 검증한다.
    """
    from app.auth.service import create_user

    # admin_client fixture 의 dr_admin 외에 추가:
    #  - admin · email 미설정 → eligible=False (without_email).
    create_user(
        db_session,
        username="dr_no_email",
        password="X_pass_1!",
        email=None,
        is_admin=True,
    )
    #  - admin · 수신거부 → eligible=False (unsubscribed).
    no_subscribe = create_user(
        db_session,
        username="dr_unsubscribed",
        password="X_pass_1!",
        email="opt_out@example.com",
        is_admin=True,
    )
    no_subscribe.email_subscribed = False
    #  - 비-admin · email 정상 + 수신 동의 → eligible=True (task 00144 신규 포함).
    create_user(
        db_session,
        username="aa_regular_user",
        password="X_pass_1!",
        email="regular@example.com",
        is_admin=False,
    )
    db_session.commit()

    response = admin_client.get("/api/admin/email/daily-report/settings")
    assert response.status_code == 200, response.text
    data = response.json()

    # eligible 2명 = dr_admin + aa_regular_user (비-admin 도 포함).
    assert data["recipient_count_eligible"] == 2
    assert data["recipient_count_without_email"] == 1
    assert data["recipient_count_unsubscribed"] == 1
    usernames = [row["username"] for row in data["recipients"]]
    # 정렬은 username 알파벳순.
    assert usernames == [
        "aa_regular_user",
        "dr_admin",
        "dr_no_email",
        "dr_unsubscribed",
    ]
    # 비-admin 사용자가 eligible=True 로 발송 대상에 포함된다.
    regular_row = next(
        row for row in data["recipients"] if row["username"] == "aa_regular_user"
    )
    assert regular_row["eligible"] is True


# ──────────────────────────────────────────────────────────────
# 3. PUT /daily-report/settings — 정상 저장 + scheduler hook 호출
# ──────────────────────────────────────────────────────────────


def test_put_daily_report_settings_saves_and_reinstalls_crontab(
    admin_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정상 PUT → SystemSetting 3종 반영 + crontab 재설치 트리거 (task 00155-4).

    crontab 재설치 호출은 monkey-patch 로 캡처해, 설정 저장 후 정확히 1회
    트리거되는지 확인한다 (실제 cron 설치 없이도 라우터 흐름 검증).
    """
    from app.scheduler.crontab_installer import CrontabInstallResult

    reinstall_call_count = {"n": 0}

    def _fake_reinstall(*args, **kwargs) -> CrontabInstallResult:
        reinstall_call_count["n"] += 1
        return CrontabInstallResult(installed=False, crontab_text="", reason="test stub")

    monkeypatch.setattr(
        "app.web.routes.admin_email.reinstall_crontab_after_change",
        _fake_reinstall,
    )

    response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": True,
            "cron_expression": "30 9 * * 1-5",
            "test_recipient": "ops@example.com",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["enabled"] is True
    assert data["cron_expression"] == "30 9 * * 1-5"
    assert data["test_recipient"] == "ops@example.com"

    # 스케줄 트리거(enabled + cron)는 SSOT(scheduled_jobs)에 반영된다.
    db_session.expire_all()
    daily_job = get_singleton_schedule(db_session, JOB_KIND_DAILY_REPORT)
    assert daily_job is not None
    assert daily_job.enabled is True
    assert daily_job.cron_expression == "30 9 * * 1-5"
    # 비-스케줄 설정인 test_recipient 는 system_settings 에 그대로 저장된다.
    assert (
        get_setting(db_session, SETTING_KEY_DAILY_REPORT_TEST_RECIPIENT)
        == "ops@example.com"
    )

    # 설정 저장 후 crontab 재설치가 정확히 1회 트리거됐는지 확인.
    assert reinstall_call_count["n"] == 1


def test_put_daily_report_settings_invalid_cron_422(
    admin_client: TestClient,
) -> None:
    """잘못된 cron 표현식 → 422 (Pydantic field_validator)."""
    response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": True,
            "cron_expression": "this is not a cron",
            "test_recipient": "",
        },
    )
    assert response.status_code == 422, response.text


def test_put_daily_report_settings_enabled_without_cron_422(
    admin_client: TestClient,
) -> None:
    """``enabled=True`` 인데 cron 이 빈 값이면 422 (model_validator)."""
    response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": True,
            "cron_expression": "",
            "test_recipient": "",
        },
    )
    assert response.status_code == 422, response.text


def test_put_daily_report_settings_disabled_with_empty_cron_ok(
    admin_client: TestClient,
) -> None:
    """``enabled=False`` + cron 빈 값은 허용 (비활성 저장).

    crontab 재설치는 테스트 호스트에서 graceful no-op 이라 라우트가 500 나지 않는다.
    """
    response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": False,
            "cron_expression": "",
            "test_recipient": "",
        },
    )
    assert response.status_code == 200, response.text


def test_put_daily_report_settings_enable_toggle_preserves_cron_and_recipient(
    admin_client: TestClient,
    db_session: Session,
) -> None:
    """[task 00144] '활성화' 토글 PUT 은 enabled 만 바꾸고 cron/test_recipient 를 보존한다.

    FE 의 'Daily Report 활성화' 체크박스는 변경 즉시 현재 cron/test_recipient
    값을 함께 실어 PUT 한다. 본 테스트는 그 흐름을 모사한다 — 먼저 cron 과
    test_recipient 를 저장해 두고, enabled 만 True 로 바꾼 PUT 을 보낸 뒤
    cron/test_recipient SystemSetting 이 그대로 유지되는지 확인한다.
    """
    # 1. 비활성 상태로 cron + test_recipient 를 먼저 저장한다.
    initial_response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": False,
            "cron_expression": "15 8 * * 1-5",
            "test_recipient": "ops@example.com",
        },
    )
    assert initial_response.status_code == 200, initial_response.text

    # 2. FE 토글 모사 — 현재 cron/test_recipient 값을 그대로 싣고 enabled 만 True.
    toggle_response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": True,
            "cron_expression": "15 8 * * 1-5",
            "test_recipient": "ops@example.com",
        },
    )
    assert toggle_response.status_code == 200, toggle_response.text
    toggle_data = toggle_response.json()
    assert toggle_data["enabled"] is True
    # cron / test_recipient 는 토글 전 값 그대로 보존.
    assert toggle_data["cron_expression"] == "15 8 * * 1-5"
    assert toggle_data["test_recipient"] == "ops@example.com"

    # SSOT(scheduled_jobs)에 enabled 만 바뀌고 cron 은 보존됐는지 확인.
    db_session.expire_all()
    daily_job = get_singleton_schedule(db_session, JOB_KIND_DAILY_REPORT)
    assert daily_job is not None
    assert daily_job.enabled is True
    assert daily_job.cron_expression == "15 8 * * 1-5"
    # 비-스케줄 설정인 test_recipient 는 system_settings 에 보존된다.
    assert (
        get_setting(db_session, SETTING_KEY_DAILY_REPORT_TEST_RECIPIENT)
        == "ops@example.com"
    )


# ──────────────────────────────────────────────────────────────
# 3-b. PUT /daily-report/settings — 저장 영속성 (task 00155-4)
# ──────────────────────────────────────────────────────────────
#
# task 00155-4 에서 APScheduler 가 제거돼 daily report 잡은 OS cron 데몬이
# 실행한다. 라이브 스케줄러가 없으므로 응답의 next_run_at 은 항상 None 이며,
# UI 는 cron 표현식/활성 여부로 다음 실행 시점을 표시한다. 아래 두 테스트는
# enabled 저장이 500 없이 커밋되고 GET 으로 영속됨을 회귀로 가드한다.


def test_put_daily_report_settings_enabled_persists_and_next_run_at_is_none(
    admin_client: TestClient,
    db_session: Session,
) -> None:
    """enabled=True 저장이 500 없이 커밋되고 GET 으로 영속된다. next_run_at 은 None.

    crontab 재설치는 테스트 호스트에서 graceful no-op 이므로 라우트가 깨지지 않는다.
    """
    response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": True,
            "cron_expression": "30 9 * * 1-5",
            "test_recipient": "ops@example.com",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["enabled"] is True
    assert data["cron_expression"] == "30 9 * * 1-5"
    # cron 데몬이 스케줄을 실행하므로 라이브 next_run_at 은 보유하지 않는다.
    assert data["next_run_at"] is None

    # SSOT(scheduled_jobs)에 실제로 커밋됐는지 확인 — 롤백되지 않았다.
    db_session.expire_all()
    daily_job = get_singleton_schedule(db_session, JOB_KIND_DAILY_REPORT)
    assert daily_job is not None and daily_job.enabled is True

    # 새로고침(GET) 해도 활성화 상태와 cron 이 그대로 유지된다.
    get_response = admin_client.get(
        "/api/admin/email/daily-report/settings"
    )
    assert get_response.status_code == 200, get_response.text
    get_data = get_response.json()
    assert get_data["enabled"] is True
    assert get_data["cron_expression"] == "30 9 * * 1-5"
    assert get_data["next_run_at"] is None


def test_put_daily_report_settings_disabled_persists_and_next_run_at_is_none(
    admin_client: TestClient,
) -> None:
    """활성화 후 비활성화 저장도 500 없이 200 이고 next_run_at 은 None (회귀 가드)."""
    # 먼저 활성화한다.
    enable_response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": True,
            "cron_expression": "30 9 * * 1-5",
            "test_recipient": "",
        },
    )
    assert enable_response.status_code == 200, enable_response.text
    assert enable_response.json()["next_run_at"] is None

    # 비활성화 — 200 + next_run_at None.
    disable_response = admin_client.put(
        "/api/admin/email/daily-report/settings",
        json={
            "enabled": False,
            "cron_expression": "",
            "test_recipient": "",
        },
    )
    assert disable_response.status_code == 200, disable_response.text
    disable_data = disable_response.json()
    assert disable_data["enabled"] is False
    assert disable_data["next_run_at"] is None


# ──────────────────────────────────────────────────────────────
# 4. POST /daily-report/test-send — recipient 우선순위 / 게이트
# ──────────────────────────────────────────────────────────────


def _stub_prepare_and_send(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured_calls: list[dict],
    result: DailyReportResult,
) -> None:
    """라우터의 ``prepare_and_send_daily_report`` 를 stub 으로 교체한다.

    호출 인자(``request_dto`` 의 trigger / recipients / requested_by_user_id) 를
    captured_calls 에 기록하고, 미리 준비된 result 를 반환한다. transport / sender
    까지 끝까지 들어가는 흐름은 단위 테스트(tests/email/test_daily_report_send.py)
    가 별도로 가드하므로 본 통합 테스트는 라우터 → service 경계만 검증한다.
    """

    def _fake(
        request_dto,
        *,
        session,
        transport,
        max_retry_count,
        now=None,
    ) -> DailyReportResult:
        captured_calls.append(
            {
                "trigger": request_dto.trigger,
                "recipients": list(request_dto.recipients),
                "requested_by_user_id": request_dto.requested_by_user_id,
                "max_retry_count": max_retry_count,
            }
        )
        return result

    monkeypatch.setattr(
        "app.web.routes.admin_email.prepare_and_send_daily_report",
        _fake,
    )
    monkeypatch.setattr(
        "app.web.routes.admin_email.build_transport_from_settings",
        lambda session: MagicMock(),
    )


def test_post_daily_report_test_send_uses_body_recipient(
    admin_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """body.recipient 가 있으면 그 값으로 prepare_and_send_daily_report 가 호출된다."""
    captured: list[dict] = []
    _stub_prepare_and_send(
        monkeypatch,
        captured_calls=captured,
        result=DailyReportResult(
            run_id=42,
            status=EmailDailyReportStatus.SUCCESS,
            snapshot_count=3,
            recipient_count=1,
            success_count=1,
            failure_count=0,
            error_message=None,
        ),
    )

    response = admin_client.post(
        "/api/admin/email/daily-report/test-send",
        json={"recipient": "tester@example.com"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data == {
        "run_id": 42,
        "status": "success",
        "snapshot_count": 3,
        "recipient_count": 1,
        "success_count": 1,
        "failure_count": 0,
        "error_message": None,
    }

    # service 호출 인자 검증 — trigger=manual_test + body recipient 그대로 전달.
    assert len(captured) == 1
    assert captured[0]["trigger"] == "manual_test"
    assert captured[0]["recipients"] == ["tester@example.com"]


def test_post_daily_report_test_send_falls_back_to_stored_recipient(
    admin_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """body.recipient 가 빈 값이면 SystemSetting 의 test_recipient 가 사용된다."""
    set_setting(
        db_session,
        SETTING_KEY_DAILY_REPORT_TEST_RECIPIENT,
        "stored@example.com",
    )
    db_session.commit()

    captured: list[dict] = []
    _stub_prepare_and_send(
        monkeypatch,
        captured_calls=captured,
        result=DailyReportResult(
            run_id=7,
            status=EmailDailyReportStatus.SUCCESS,
            snapshot_count=0,
            recipient_count=1,
            success_count=1,
            failure_count=0,
            error_message=None,
        ),
    )

    response = admin_client.post(
        "/api/admin/email/daily-report/test-send",
        json={"recipient": ""},
    )
    assert response.status_code == 200, response.text

    assert captured[0]["recipients"] == ["stored@example.com"]


def test_post_daily_report_test_send_missing_recipient_422(
    admin_client: TestClient,
    db_session: Session,
) -> None:
    """body.recipient + SystemSetting 둘 다 비어 있으면 422."""
    response = admin_client.post(
        "/api/admin/email/daily-report/test-send",
        json={"recipient": ""},
    )
    assert response.status_code == 422, response.text


def test_post_daily_report_test_send_returns_503_when_gate_disabled(
    admin_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """게이트 비활성 상태에서 호출 시 503 + EmailDailyReportRun 이력은 commit 된다.

    실제 게이트 OFF 동작은 ``prepare_and_send_daily_report`` 가
    ``EmailSendingDisabledError`` 를 raise 한 결과로 발생한다. 본 테스트는
    라우터가 그 예외를 503 으로 변환하는지를 검증한다.
    """
    from app.email.gate import EmailSendingDisabledError

    def _raise_disabled(*args, **kwargs):
        raise EmailSendingDisabledError("메일 전송이 비활성화 상태")

    monkeypatch.setattr(
        "app.web.routes.admin_email.prepare_and_send_daily_report",
        _raise_disabled,
    )
    monkeypatch.setattr(
        "app.web.routes.admin_email.build_transport_from_settings",
        lambda session: MagicMock(),
    )

    response = admin_client.post(
        "/api/admin/email/daily-report/test-send",
        json={"recipient": "tester@example.com"},
    )
    assert response.status_code == 503, response.text


# ──────────────────────────────────────────────────────────────
# 5. POST /daily-report/send-now — 수신 대상 사용자 email 자동 수집
# ──────────────────────────────────────────────────────────────


def test_post_daily_report_send_now_collects_eligible_recipients(
    admin_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """수신 대상 사용자 email 이 ``recipients`` 로 자동 수집되어 service 에 전달된다.

    task 00144 — admin 제약이 제거되어, email 정상 + 수신 동의한 사용자는
    admin 여부와 무관하게 모두 수집된다.

    fixture 의 dr_admin 외에 3명을 추가:
        - other_admin (admin, eligible) → 포함
        - regular_user (비-admin, eligible) → 포함 (task 00144 신규)
        - opt_out_admin (admin, 수신거부) → 제외
    결과적으로 eligible 3명만 ``recipients`` 에 포함되어야 한다.
    """
    from app.auth.service import create_user

    create_user(
        db_session,
        username="other_admin",
        password="X_pass_1!",
        email="other@example.com",
        is_admin=True,
    )
    # 비-admin 일반 사용자도 수신 대상에 포함되어야 한다 (task 00144).
    create_user(
        db_session,
        username="regular_user",
        password="X_pass_1!",
        email="regular@example.com",
        is_admin=False,
    )
    opt_out = create_user(
        db_session,
        username="opt_out_admin",
        password="X_pass_1!",
        email="silent@example.com",
        is_admin=True,
    )
    opt_out.email_subscribed = False
    db_session.commit()

    captured: list[dict] = []
    _stub_prepare_and_send(
        monkeypatch,
        captured_calls=captured,
        result=DailyReportResult(
            run_id=100,
            status=EmailDailyReportStatus.SUCCESS,
            snapshot_count=5,
            recipient_count=3,
            success_count=3,
            failure_count=0,
            error_message=None,
        ),
    )

    response = admin_client.post(
        "/api/admin/email/daily-report/send-now", json={}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["run_id"] == 100
    assert data["status"] == "success"
    assert data["recipient_count"] == 3

    # service 호출 인자 검증 — trigger=manual_admin + eligible 3명 (admin+비-admin).
    assert len(captured) == 1
    assert captured[0]["trigger"] == "manual_admin"
    assert sorted(captured[0]["recipients"]) == [
        "dr_admin@example.com",
        "other@example.com",
        "regular@example.com",
    ]
    # opt_out_admin (email_subscribed=False) 는 제외되어야 한다.
    assert "silent@example.com" not in captured[0]["recipients"]


# ──────────────────────────────────────────────────────────────
# 6. GET /daily-report/runs — 응답 스키마 + 정렬
# ──────────────────────────────────────────────────────────────


def test_get_daily_report_runs_returns_recent_rows(
    admin_client: TestClient,
    db_session: Session,
) -> None:
    """EmailDailyReportRun row 들을 started_at 내림차순으로 반환 + 응답 스키마 검증."""
    older = EmailDailyReportRun(
        trigger="scheduled",
        status=EmailDailyReportStatus.SUCCESS,
        aggregation_from=datetime(2026, 5, 18, 0, 0, tzinfo=UTC),
        aggregation_to=datetime(2026, 5, 19, 0, 0, tzinfo=UTC),
        snapshot_count=3,
        recipient_count=2,
        success_count=2,
        failure_count=0,
        error_message=None,
        started_at=datetime(2026, 5, 19, 0, 0, tzinfo=UTC),
        completed_at=datetime(2026, 5, 19, 0, 1, tzinfo=UTC),
    )
    newer = EmailDailyReportRun(
        trigger="manual_admin",
        status=EmailDailyReportStatus.PARTIAL,
        aggregation_from=datetime(2026, 5, 19, 0, 0, tzinfo=UTC),
        aggregation_to=datetime(2026, 5, 20, 0, 0, tzinfo=UTC),
        snapshot_count=4,
        recipient_count=2,
        success_count=1,
        failure_count=1,
        error_message="ConnectionError: blocked",
        started_at=datetime(2026, 5, 20, 0, 0, tzinfo=UTC),
        completed_at=datetime(2026, 5, 20, 0, 1, tzinfo=UTC),
    )
    db_session.add_all([older, newer])
    db_session.commit()

    response = admin_client.get("/api/admin/email/daily-report/runs")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["count"] == 2
    items = data["items"]
    # 최신순 — newer 가 먼저.
    assert items[0]["trigger"] == "manual_admin"
    assert items[0]["status"] == "partial"
    assert items[0]["snapshot_count"] == 4
    assert items[0]["error_message"] == "ConnectionError: blocked"
    assert items[1]["trigger"] == "scheduled"
    assert items[1]["status"] == "success"
    # ISO-8601 직렬화 형식 가드.
    assert items[0]["started_at"].startswith("2026-05-20T")
    assert items[1]["aggregation_from"].startswith("2026-05-18T")
    # UTC tz-aware 직렬화 검증 — 프론트엔드 KST 변환이 정상 동작하려면 +00:00 이 있어야 한다.
    for item in items:
        for field in ("started_at", "completed_at", "aggregation_from", "aggregation_to"):
            value = item.get(field)
            if value is not None:
                assert "+00:00" in value, f"{field}={value!r} 에 UTC 오프셋이 없음"


def test_get_daily_report_runs_limit_validation(
    admin_client: TestClient,
) -> None:
    """``limit`` 범위 위반 시 422 (Pydantic Query validation)."""
    response = admin_client.get(
        "/api/admin/email/daily-report/runs?limit=0"
    )
    assert response.status_code == 422

    response = admin_client.get(
        "/api/admin/email/daily-report/runs?limit=999"
    )
    assert response.status_code == 422


# ──────────────────────────────────────────────────────────────
# 7. GET /daily-report/runs/{run_id}/sends — 응답 스키마 + 404
# ──────────────────────────────────────────────────────────────


def test_get_daily_report_run_sends_returns_matching_send_runs(
    admin_client: TestClient,
    db_session: Session,
) -> None:
    """related_kind='daily_report' AND related_id=run_id 매칭 row 만 반환."""
    run = EmailDailyReportRun(
        trigger="scheduled",
        status=EmailDailyReportStatus.PARTIAL,
        aggregation_from=datetime(2026, 5, 19, 0, 0, tzinfo=UTC),
        aggregation_to=datetime(2026, 5, 20, 0, 0, tzinfo=UTC),
        snapshot_count=2,
        recipient_count=2,
        success_count=1,
        failure_count=1,
        error_message="ConnectionError: timeout",
        started_at=datetime(2026, 5, 20, 0, 0, tzinfo=UTC),
        completed_at=datetime(2026, 5, 20, 0, 1, tzinfo=UTC),
    )
    db_session.add(run)
    db_session.flush()

    matched_success = EmailSendRun(
        recipient="alice@example.com",
        subject="[정부사업 모니터링] Daily Report",
        body_preview="...",
        transport_type="m365_oauth",
        status=EmailSendRunStatus.SENT,
        error_message=None,
        attempt_count=1,
        related_kind=RELATED_KIND_DAILY_REPORT,
        related_id=run.id,
        created_at=datetime(2026, 5, 20, 0, 0, 10, tzinfo=UTC),
        sent_at=datetime(2026, 5, 20, 0, 0, 11, tzinfo=UTC),
    )
    matched_failed = EmailSendRun(
        recipient="bob@example.com",
        subject="[정부사업 모니터링] Daily Report",
        body_preview="...",
        transport_type="m365_oauth",
        status=EmailSendRunStatus.FAILED,
        error_message="TimeoutError: too slow",
        attempt_count=3,
        related_kind=RELATED_KIND_DAILY_REPORT,
        related_id=run.id,
        created_at=datetime(2026, 5, 20, 0, 0, 20, tzinfo=UTC),
        sent_at=None,
    )
    # 다른 daily report run 의 row — 본 응답에 포함되면 안 된다.
    unrelated = EmailSendRun(
        recipient="other@example.com",
        subject="다른 발송",
        body_preview="...",
        transport_type="m365_oauth",
        status=EmailSendRunStatus.SENT,
        error_message=None,
        attempt_count=1,
        related_kind="forward",  # 다른 도메인
        related_id=run.id,
        created_at=datetime(2026, 5, 20, 0, 0, 30, tzinfo=UTC),
        sent_at=datetime(2026, 5, 20, 0, 0, 31, tzinfo=UTC),
    )
    db_session.add_all([matched_success, matched_failed, unrelated])
    db_session.commit()

    response = admin_client.get(
        f"/api/admin/email/daily-report/runs/{run.id}/sends"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["count"] == 2
    items = data["items"]
    # created_at 오름차순.
    assert items[0]["recipient"] == "alice@example.com"
    assert items[0]["status"] == "sent"
    assert items[0]["error_message"] is None
    assert items[1]["recipient"] == "bob@example.com"
    assert items[1]["status"] == "failed"
    assert items[1]["error_message"] == "TimeoutError: too slow"
    assert items[1]["attempt_count"] == 3
    # UTC tz-aware 직렬화 검증 — sent_at 이 있는 row 는 +00:00 오프셋 포함.
    assert items[0]["sent_at"] is not None
    assert "+00:00" in items[0]["sent_at"], f"sent_at={items[0]['sent_at']!r} 에 UTC 오프셋이 없음"
    # bob 은 sent_at=None 이므로 None 이어야 한다.
    assert items[1]["sent_at"] is None


def test_get_daily_report_run_sends_404_when_run_missing(
    admin_client: TestClient,
) -> None:
    """존재하지 않는 run_id → 404."""
    response = admin_client.get(
        "/api/admin/email/daily-report/runs/99999/sends"
    )
    assert response.status_code == 404, response.text
