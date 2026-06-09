"""[시스템 재시작] 탭 페이지 렌더 테스트 (task 00161-2).

검증 범위:
    - GET /admin/system/restart: 비로그인 → 401, 비관리자 → 403, 관리자 → 200.
    - 관리자 렌더 시 [시스템 재시작] 서브탭이 active 로 표시되고, '지금 재시작'
      버튼과 admin_restart.js 가 페이지에 존재한다.
    - 진행중 수집(scrape_runs.status='running')이 없으면 버튼이 활성(enabled),
      진행중 수집이 있으면 버튼이 disabled + 경고 배너가 노출된다.

E2E/안전:
    실제 docker restart 는 트리거하지 않는다. 본 테스트는 GET 페이지 렌더만
    검증하므로 셀프 재시작 endpoint(POST)나 컨테이너를 건드리지 않는다. POST
    endpoint 동작은 tests/web/test_admin_system_restart.py 가 모킹으로 검증한다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session


# ──────────────────────────────────────────────────────────────
# 픽스처
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

    create_user(
        db_session,
        username="restart_page_admin",
        password="Admin_pass_1!",
        is_admin=True,
    )
    db_session.commit()

    _login(client, "restart_page_admin", "Admin_pass_1!")
    return client


# ──────────────────────────────────────────────────────────────
# GET /admin/system/restart — 권한
# ──────────────────────────────────────────────────────────────


def test_restart_page_anonymous_401(client: TestClient) -> None:
    """비로그인 요청은 401 이어야 한다."""
    resp = client.get("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 401


def test_restart_page_non_admin_403(client: TestClient) -> None:
    """비관리자 로그인 상태에서는 403 이어야 한다."""
    _register(client, "restart_plain_user", "Plain_pass_1!")
    resp = client.get("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────
# GET /admin/system/restart — 렌더 내용
# ──────────────────────────────────────────────────────────────


def test_restart_page_renders_for_admin(admin_client: TestClient) -> None:
    """관리자 렌더 시 서브탭 active · 버튼 · 폴링 JS 가 존재한다."""
    resp = admin_client.get("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    html = resp.text

    # 서브탭 nav 에 [시스템 재시작] 링크가 있고 active 로 표시된다.
    assert 'href="/admin/system/restart"' in html
    assert "admin-tab--active" in html
    assert "시스템 재시작" in html

    # 재시작 버튼과 상태 표시 영역, 폴링 JS 가 존재한다.
    assert 'id="restart-button"' in html
    assert 'id="restart-status-area"' in html
    assert "/static/js/admin_restart.js" in html


def test_restart_page_button_enabled_when_no_running_scrape(
    admin_client: TestClient,
) -> None:
    """진행중 수집이 없으면 버튼이 disabled 되지 않고 경고 배너도 없다."""
    resp = admin_client.get("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    html = resp.text

    # 버튼 영역에 disabled 속성이 들어가지 않는다.
    assert "disabled" not in _restart_button_tag(html)
    # 진행중 scrape 경고 배너(admin-flash--warning)는 노출되지 않는다.
    assert "admin-flash--warning" not in html


def test_restart_page_button_disabled_when_running_scrape(
    admin_client: TestClient,
    db_session: Session,
) -> None:
    """진행중 수집이 있으면 버튼이 disabled 되고 경고 배너가 노출된다."""
    from app.db.repository import create_scrape_run

    run = create_scrape_run(
        db_session, trigger="manual", source_counts={"active_sources": []}
    )
    db_session.commit()
    running_id = run.id

    resp = admin_client.get("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 200, resp.text
    html = resp.text

    # 버튼이 disabled 처리된다.
    assert "disabled" in _restart_button_tag(html)
    # 진행중 scrape 경고 배너가 노출되고, 해당 run id 가 안내에 포함된다.
    assert "admin-flash--warning" in html
    assert f"scrape_run_id={running_id}" in html


# ──────────────────────────────────────────────────────────────
# GET /admin/system/restart — 기동 이력 컨텍스트 (task 00162)
# ──────────────────────────────────────────────────────────────


def test_restart_page_injects_startup_events_context(
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """system_restart_page 가 startup_events 컨텍스트를 채워 전달한다.

    admin 라우트가 read_recent_startup_events 결과를 템플릿 컨텍스트
    'startup_events' 로 넘기는지를, 템플릿 렌더 컨텍스트를 가로채 검증한다.
    (템플릿 마크업 자체는 162-2 가 담당하므로 여기서는 컨텍스트 키/형태만 본다.)
    """
    from app.scrape_control import restart as restart_module
    from app.web.routes import admin as admin_module

    # 임시 로그 파일에 기동 이벤트 한 줄을 남기고, 라우트의 read_recent_startup_events
    # 가 임시 data_dir 를 보도록 우회한다(라우트는 limit 만 넘긴다).
    restart_module.append_startup_event(
        restart_module.STARTUP_EVENT_TYPE_STARTUP,
        data_dir=tmp_path,
        extra={"pid": 4242},
    )
    original_reader = restart_module.read_recent_startup_events

    def _reader_with_tmp_dir(*, limit: int = 30, data_dir=None):
        return original_reader(limit=limit, data_dir=tmp_path)

    monkeypatch.setattr(
        admin_module, "read_recent_startup_events", _reader_with_tmp_dir
    )

    # 템플릿 렌더 직전의 컨텍스트를 가로챈다.
    captured: dict = {}
    original_template_response = admin_module._templates.TemplateResponse

    def _capturing_template_response(request, name, context, *args, **kwargs):
        captured["name"] = name
        captured["context"] = context
        return original_template_response(request, name, context, *args, **kwargs)

    monkeypatch.setattr(
        admin_module._templates, "TemplateResponse", _capturing_template_response
    )

    resp = admin_client.get("/admin/system/restart", follow_redirects=False)
    assert resp.status_code == 200, resp.text

    # startup_events 키가 존재하고 StartupEvent 리스트 형태여야 한다.
    assert "startup_events" in captured["context"]
    startup_events = captured["context"]["startup_events"]
    assert isinstance(startup_events, list)
    assert len(startup_events) == 1
    event = startup_events[0]
    assert event.event_type == restart_module.STARTUP_EVENT_TYPE_STARTUP
    assert event.type_label == "일반 기동"
    assert hasattr(event, "timestamp_display")
    assert hasattr(event, "message")


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _restart_button_tag(html: str) -> str:
    """렌더된 HTML 에서 '지금 재시작' 버튼의 여는 태그 문자열만 잘라 반환한다.

    버튼 태그 안에 disabled 속성이 있는지 좁혀서 보기 위함이다(페이지 다른 곳의
    'disabled' 문자열에 오탐하지 않도록).
    """
    marker = 'id="restart-button"'
    start = html.index(marker)
    # 버튼 여는 태그의 시작('<')과 끝('>')을 marker 기준으로 찾는다.
    open_bracket = html.rindex("<", 0, start)
    close_bracket = html.index(">", start)
    return html[open_bracket : close_bracket + 1]
