"""[시스템 재시작] 셀프 재시작 endpoint + health endpoint 통합 테스트 (task 00161).

검증 범위:
    - POST /admin/system/restart: 비로그인 → 401, 비관리자 → 403, 관리자 → 200.
    - 관리자 POST 시 trigger_self_restart 가 호출되고 200(JSON status=restarting)
      을 즉시 반환한다(실제 docker 는 호출하지 않도록 모킹).
    - ensure_same_origin: 다른 origin 헤더는 400 으로 차단된다.
    - 진행중 scrape 가드: running 이 있으면 응답에 scrape_running=true 가 실린다.
    - GET /healthz: 인증 없이 200 + {\"status\": \"ok\"}.

docker 의존성 제거:
    admin 라우트가 호출하는 trigger_self_restart 를 monkeypatch 해 실제 컨테이너를
    재시작하지 않는다. 테스트는 전부 동기로 실행된다.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.scrape_control.restart import RestartResult


# ──────────────────────────────────────────────────────────────
# 픽스처
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_trigger_self_restart(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """admin 라우트가 호출하는 trigger_self_restart 를 가짜로 대체한다.

    실제 docker restart 가 일어나지 않도록 하고, 호출 여부를 dict 로 노출한다.
    """
    called: dict[str, Any] = {"count": 0}

    def _fake_trigger(**kwargs: Any) -> RestartResult:
        called["count"] += 1
        called["kwargs"] = kwargs
        return RestartResult(
            pid=4242,
            container="iris-agent-web",
            argv=["sh", "-c", "sleep 1 && /usr/bin/docker restart iris-agent-web"],
            log_path="/tmp/system_restart.log",
        )

    # admin 모듈이 import 한 심볼을 직접 대체해야 라우트가 가짜를 쓴다.
    monkeypatch.setattr(
        "app.web.routes.admin.trigger_self_restart", _fake_trigger
    )
    return called


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

    create_user(
        db_session, username="restart_admin", password="Admin_pass_1!", is_admin=True
    )
    db_session.commit()

    _login(client, "restart_admin", "Admin_pass_1!")
    return client


# ──────────────────────────────────────────────────────────────
# POST /admin/system/restart — 권한
# ──────────────────────────────────────────────────────────────


def test_restart_anonymous_401(
    client: TestClient, _stub_trigger_self_restart: dict[str, Any]
) -> None:
    """비로그인 요청은 401 이어야 하고 재시작이 트리거되지 않는다."""
    resp = client.post("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 401
    assert _stub_trigger_self_restart["count"] == 0


def test_restart_non_admin_403(
    client: TestClient, _stub_trigger_self_restart: dict[str, Any]
) -> None:
    """비관리자 로그인 상태에서는 403 이어야 한다."""
    _register(client, "plain_user", "Plain_pass_1!")
    resp = client.post("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 403
    assert _stub_trigger_self_restart["count"] == 0


def test_restart_admin_ok(
    admin_client: TestClient, _stub_trigger_self_restart: dict[str, Any]
) -> None:
    """관리자 POST 는 200 + status=restarting 을 즉시 반환하고 재시작을 트리거한다."""
    resp = admin_client.post("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "restarting"
    assert body["container"] == "iris-agent-web"
    assert body["scrape_running"] is False
    assert body["running_scrape_run_id"] is None
    assert _stub_trigger_self_restart["count"] == 1


# ──────────────────────────────────────────────────────────────
# POST /admin/system/restart — ensure_same_origin
# ──────────────────────────────────────────────────────────────


def test_restart_other_origin_blocked(
    admin_client: TestClient, _stub_trigger_self_restart: dict[str, Any]
) -> None:
    """다른 origin 헤더가 붙은 요청은 ensure_same_origin 에 의해 400 으로 차단된다."""
    resp = admin_client.post(
        "/admin/system/restart",
        headers={"origin": "https://evil.example.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert _stub_trigger_self_restart["count"] == 0


# ──────────────────────────────────────────────────────────────
# POST /admin/system/restart — 진행중 scrape 가드
# ──────────────────────────────────────────────────────────────


def test_restart_reports_running_scrape(
    admin_client: TestClient,
    db_session: Session,
    _stub_trigger_self_restart: dict[str, Any],
) -> None:
    """진행중 수집이 있으면 응답에 scrape_running=true 와 run id 가 실린다."""
    from app.db.repository import create_scrape_run

    run = create_scrape_run(
        db_session, trigger="manual", source_counts={"active_sources": []}
    )
    db_session.commit()
    running_id = run.id

    resp = admin_client.post("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scrape_running"] is True
    assert body["running_scrape_run_id"] == running_id


# ──────────────────────────────────────────────────────────────
# GET /healthz
# ──────────────────────────────────────────────────────────────


def test_healthz_no_auth_ok(client: TestClient) -> None:
    """health endpoint 는 인증 없이 200 + status=ok 를 반환한다."""
    resp = client.get("/healthz", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
