"""시스템 백업 관리자 탭 통합 테스트 (task 00094-3).

검증 범위:
- GET /admin/backup: 비로그인 → 401, 비관리자 → 403, 관리자 → 200
- POST /admin/backup/settings: 유효 설정 저장 → 303 + DB 반영
- POST /admin/backup/settings: max_count=0 → 303 error flash
- POST /admin/backup/run: 성공 → 303 success flash
- POST /admin/backup/run: history.success=False → 303 error flash
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session


# ──────────────────────────────────────────────────────────────
# 앱 클라이언트 픽스처
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """메인 DB 가 격리된 TestClient."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _register(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/auth/register",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"회원가입 실패: {resp.status_code}"


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"로그인 실패: {resp.status_code}"


@pytest.fixture
def admin_client(client: TestClient, db_session: Session) -> TestClient:
    """관리자(is_admin=True)로 로그인된 TestClient."""
    from app.auth.service import create_user

    create_user(db_session, username="backup_admin", password="Admin_pass_1!", is_admin=True)
    db_session.commit()

    _login(client, "backup_admin", "Admin_pass_1!")
    return client


# ──────────────────────────────────────────────────────────────
# GET /admin/backup
# ──────────────────────────────────────────────────────────────


def test_backup_page_anonymous_401(client: TestClient) -> None:
    """비로그인 요청은 401 이어야 한다."""
    resp = client.get("/admin/backup", follow_redirects=False)
    assert resp.status_code == 401


def test_backup_page_non_admin_403(client: TestClient) -> None:
    """비관리자 로그인 상태에서는 403 이어야 한다."""
    _register(client, "plain_user", "Plain_pass_1!")
    resp = client.get("/admin/backup", follow_redirects=False)
    assert resp.status_code == 403


def test_backup_page_admin_ok(admin_client: TestClient) -> None:
    """관리자는 200 응답과 함께 백업 탭 콘텐츠를 볼 수 있어야 한다."""
    resp = admin_client.get("/admin/backup", follow_redirects=False)
    assert resp.status_code == 200
    assert "백업" in resp.text


def test_backup_page_shows_default_cron(admin_client: TestClient) -> None:
    """설정이 없을 때 기본 cron 표현식이 폼에 노출되어야 한다."""
    from app.backup.constants import DEFAULT_BACKUP_CRON

    resp = admin_client.get("/admin/backup", follow_redirects=False)
    assert resp.status_code == 200
    assert DEFAULT_BACKUP_CRON in resp.text


# ──────────────────────────────────────────────────────────────
# POST /admin/backup/settings
# ──────────────────────────────────────────────────────────────


def test_backup_settings_save_ok(
    admin_client: TestClient,
    db_session: Session,
) -> None:
    """유효한 설정 저장 → DB 에 반영되고 303 success redirect.

    저장 후 crontab 재설치는 ``ENABLE_CRON=1`` 컨테이너에서만 실제 수행되고,
    테스트 호스트에서는 graceful no-op 이라 라우트가 500 나지 않는다(task 00155-4).
    """
    resp = admin_client.post(
        "/admin/backup/settings",
        data={"cron_expression": "0 4 * * *", "max_count": "14"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/admin/backup" in resp.headers["location"]
    assert "error" not in resp.headers["location"]

    # DB 에 실제로 저장됐는지 확인. 백업 cron 트리거는 단일 SSOT(scheduled_jobs)에,
    # 비-스케줄 설정인 max_count 는 system_settings 에 저장된다.
    from app.backup.constants import SETTING_KEY_BACKUP_MAX_COUNT
    from app.backup.service import get_setting
    from app.scheduler.constants import JOB_KIND_BACKUP
    from app.scheduler.scheduled_job_store import get_singleton_schedule

    db_session.expire_all()
    backup_job = get_singleton_schedule(db_session, JOB_KIND_BACKUP)
    assert backup_job is not None
    assert backup_job.cron_expression == "0 4 * * *"
    assert get_setting(db_session, SETTING_KEY_BACKUP_MAX_COUNT) == "14"


def test_backup_settings_invalid_max_count_zero(
    admin_client: TestClient,
) -> None:
    """max_count=0 이면 저장 거부 — error flash redirect."""
    resp = admin_client.post(
        "/admin/backup/settings",
        data={"cron_expression": "0 3 * * *", "max_count": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


def test_backup_settings_invalid_cron_error_flash(
    admin_client: TestClient,
) -> None:
    """유효하지 않은 cron 표현식 → CronExpressionError → error flash redirect.

    task 00155-4 — 백업 cron 검증이 APScheduler 의 build_cron_trigger 대신 순수
    검증 함수(validate_cron_expression)로 바뀌었다. 'bad cron' 은 5필드지만 분
    필드 값('bad')이 숫자가 아니라 거부된다.
    """
    resp = admin_client.post(
        "/admin/backup/settings",
        data={"cron_expression": "bad 3 * * *", "max_count": "7"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


# ──────────────────────────────────────────────────────────────
# POST /admin/backup/run
# ──────────────────────────────────────────────────────────────


def test_backup_run_manual_success(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """수동 백업 실행 성공 → 303 success flash redirect."""
    from app.db.models import BackupHistory
    from app.timezone import now_utc

    fake_history = BackupHistory(
        executed_at=now_utc(),
        trigger="manual",
        target_files=["data/app.sqlite3"],
        backup_files=["data/backups/app_20260508_030000.sqlite3"],
        success=True,
        error_message=None,
        duration_seconds=0.12,
        total_size_bytes=102400,
    )

    monkeypatch.setattr(
        "app.web.routes.admin.run_backup",
        lambda trigger: fake_history,
    )

    resp = admin_client.post("/admin/backup/run", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "/admin/backup" in location
    assert "error" not in location


def test_backup_run_manual_failure_flash(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_backup 이 success=False 를 반환하면 error flash redirect."""
    from app.db.models import BackupHistory
    from app.timezone import now_utc

    fake_history = BackupHistory(
        executed_at=now_utc(),
        trigger="manual",
        target_files=["data/app.sqlite3"],
        backup_files=[],
        success=False,
        error_message="app.sqlite3: 테스트 오류",
        duration_seconds=0.01,
        total_size_bytes=None,
    )

    monkeypatch.setattr(
        "app.web.routes.admin.run_backup",
        lambda trigger: fake_history,
    )

    resp = admin_client.post("/admin/backup/run", follow_redirects=False)
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


def test_backup_run_exception_flash(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_backup 이 예외를 던지면 error flash redirect."""

    def _raise(trigger: str) -> None:
        raise RuntimeError("백업 디렉터리 없음")

    monkeypatch.setattr("app.web.routes.admin.run_backup", _raise)

    resp = admin_client.post("/admin/backup/run", follow_redirects=False)
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


# ──────────────────────────────────────────────────────────────
# GET /admin/backup — 백업 이력 20개 제한 (task 00152)
# ──────────────────────────────────────────────────────────────


def test_backup_page_history_limited_to_20(
    admin_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """백업 이력이 25개 있어도 /admin/backup 은 최신 20개만 반환한다."""
    from app.db.models import BackupHistory
    from app.timezone import now_utc

    monkeypatch.setattr(
        "app.web.routes.admin.list_backup_files",
        lambda: [],
    )

    # 25개 이력을 시간 순으로 삽입 (oldest → newest)
    total = 25
    histories = [
        BackupHistory(
            executed_at=now_utc().replace(microsecond=i),
            trigger="scheduled",
            target_files=["data/app.sqlite3"],
            backup_files=[f"data/backups/app_{i:05d}.sqlite3"],
            success=True,
            error_message=None,
            duration_seconds=0.1,
            total_size_bytes=1024,
        )
        for i in range(total)
    ]
    db_session.add_all(histories)
    db_session.commit()

    resp = admin_client.get("/admin/backup", follow_redirects=False)
    assert resp.status_code == 200

    # 가장 최신 20개 파일명만 포함되어야 한다 (i=5..24)
    for i in range(5, total):
        assert f"app_{i:05d}.sqlite3" in resp.text, f"최신 이력 {i} 가 페이지에 없음"

    # i=0..4 (오래된 5개)는 포함되지 않아야 한다
    for i in range(5):
        assert f"app_{i:05d}.sqlite3" not in resp.text, f"오래된 이력 {i} 가 잘못 포함됨"
