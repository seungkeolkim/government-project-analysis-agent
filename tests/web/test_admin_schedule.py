"""[스케줄] 탭 + 관련 관리자 화면 정합성 통합 테스트 (task 00156-2).

00156-2 의 드롭 마이그레이션으로 레거시 ``scheduler_jobs`` 테이블이 head 에서
사라진 뒤에도, admin 스케줄/메일/백업 화면과 스케줄 추가·삭제·토글 흐름이
SystemSetting(SSOT) 기준으로 예외 없이 동작함을 FastAPI TestClient 로 검증한다.

검증 범위:
- 레거시 테이블 부재 상태(= alembic head)에서 GET /admin/schedule,
  /admin/email, /admin/backup 이 200 으로 렌더된다.
- POST /admin/schedule(cron 등록) → 303 후 목록에 노출.
- POST /admin/schedule/{id}/delete → 303 후 목록에서 사라짐.
- POST /admin/schedule/{id}/toggle → 303(crontab 재설치는 테스트 호스트에서
  ENABLE_CRON 미설정으로 graceful no-op).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session

from app.scheduler.constants import SCHEDULER_JOBS_TABLENAME


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """메인 DB 가 격리된 TestClient."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, username: str, password: str) -> None:
    """로그인 폼 POST. 303 리다이렉트를 성공으로 본다."""
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

    create_user(
        db_session, username="sched_admin", password="Admin_pass_1!", is_admin=True
    )
    db_session.commit()

    _login(client, "sched_admin", "Admin_pass_1!")
    return client


# ──────────────────────────────────────────────────────────────
# 화면 렌더 (레거시 테이블 부재 상태)
# ──────────────────────────────────────────────────────────────


def test_legacy_table_absent_then_schedule_page_ok(
    admin_client: TestClient, test_engine: Engine
) -> None:
    """레거시 테이블이 없는 head 상태에서 /admin/schedule 이 200 으로 렌더된다."""
    # 전제: 드롭 마이그레이션으로 scheduler_jobs 가 사라져 있다.
    assert SCHEDULER_JOBS_TABLENAME not in inspect(test_engine).get_table_names()

    resp = admin_client.get("/admin/schedule", follow_redirects=False)
    assert resp.status_code == 200


def test_email_and_backup_pages_render_without_legacy_table(
    admin_client: TestClient,
) -> None:
    """레거시 테이블 부재 상태에서 메일·백업 화면도 200 으로 렌더된다."""
    assert admin_client.get("/admin/email", follow_redirects=False).status_code == 200
    assert admin_client.get("/admin/backup", follow_redirects=False).status_code == 200


# ──────────────────────────────────────────────────────────────
# 추가 / 삭제 / 토글 흐름
# ──────────────────────────────────────────────────────────────


def test_schedule_add_cron_then_listed(admin_client: TestClient) -> None:
    """cron 스케줄 등록 → 303 후 목록 화면에 표현식이 노출된다."""
    resp = admin_client.post(
        "/admin/schedule",
        data={"trigger_type": "cron", "cron_expression": "0 1 * * *"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" not in resp.headers["location"]

    page = admin_client.get("/admin/schedule", follow_redirects=False)
    assert page.status_code == 200
    assert "0 1 * * *" in page.text


def test_schedule_delete_removes_from_list(
    admin_client: TestClient, db_session: Session
) -> None:
    """저장소에 직접 넣은 스케줄을 삭제 라우트로 지우면 목록에서 사라진다."""
    from app.scheduler.constants import TRIGGER_TYPE_CRON
    from app.scheduler.scheduled_job_store import add_general_schedule

    record = add_general_schedule(
        db_session, trigger_type=TRIGGER_TYPE_CRON, cron_expression="0 13 * * *"
    )
    db_session.commit()

    before = admin_client.get("/admin/schedule", follow_redirects=False)
    assert "0 13 * * *" in before.text

    resp = admin_client.post(
        f"/admin/schedule/{record.id}/delete", follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error" not in resp.headers["location"]

    after = admin_client.get("/admin/schedule", follow_redirects=False)
    assert "0 13 * * *" not in after.text


def test_schedule_toggle_ok(
    admin_client: TestClient, db_session: Session
) -> None:
    """스케줄 토글 라우트가 예외 없이 303 으로 동작한다(crontab 재설치 no-op)."""
    from app.scheduler.constants import TRIGGER_TYPE_CRON
    from app.scheduler.scheduled_job_store import add_general_schedule

    record = add_general_schedule(
        db_session, trigger_type=TRIGGER_TYPE_CRON, cron_expression="0 5 * * *"
    )
    db_session.commit()

    resp = admin_client.post(
        f"/admin/schedule/{record.id}/toggle",
        data={"enabled": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error" not in resp.headers["location"]
