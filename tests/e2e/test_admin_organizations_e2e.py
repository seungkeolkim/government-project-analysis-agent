"""조직 rename / move 관리자 UI E2E 테스트 (task 00055-2).

검증 시나리오:
    1. rename 성공 — 조직명 변경 후 success flash 표시 + 새 이름 트리에 반영.
    2. move 성공 — 조직을 다른 부모 아래로 이동 후 success flash 표시.
    3. rename 동명 충돌 — 이미 존재하는 이름으로 변경 시도 시 error flash 표시.

Playwright + uvicorn 서브프로세스 기반 E2E 이므로 Playwright 미설치 환경에서는
모듈 자체가 skip 된다 (conftest.py 와 동일 정책).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# Playwright 가 없으면 conftest 와 마찬가지로 모듈 skip.
pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright 가 설치되지 않은 환경에서는 E2E 테스트를 건너뜁니다.",
)

from playwright.sync_api import Page, expect


pytestmark = pytest.mark.e2e

# 시드 관리자 계정 정보
_ADMIN_USERNAME: str = "e2e_org_admin"
_ADMIN_PASSWORD: str = "e2e_org_admin_pw1"

# 테스트별로 사용할 조직명 (겹치지 않도록 명시적으로 구분)
_ORG_RENAME_TARGET: str = "org_rename_target"  # rename 성공 테스트 대상
_ORG_RENAME_NEW: str = "org_rename_target_new"  # 변경 후 이름
_ORG_DUP_EXISTING: str = "org_dup_existing"   # 동명 충돌 테스트 — 이미 존재하는 이름
_ORG_DUP_SOURCE: str = "org_dup_source"       # 동명 충돌 테스트 — 변경 시도 대상
_ORG_MOVE_SRC: str = "org_move_src"           # move 성공 테스트 — 이동할 조직
_ORG_MOVE_DEST: str = "org_move_dest"         # move 성공 테스트 — 이동 목적지 부모


# ──────────────────────────────────────────────────────────────
# Fixture — 이 모듈용 workspace (conftest e2e_workspace 를 모듈 수준에서 대체)
# ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def e2e_workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """조직 관리 e2e 용 module-scope workspace.

    conftest.py 의 e2e_workspace 를 이 모듈에서 오버라이드하여
    관리자 계정 + 테스트 조직을 시드한다.
    """
    monkeypatch = pytest.MonkeyPatch()
    workspace_dir: Path = tmp_path_factory.mktemp("e2e_org_workspace")
    db_path: Path = workspace_dir / "e2e.sqlite3"
    download_dir: Path = workspace_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    db_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setenv("DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    from app.config import get_settings
    from app.db.init_db import init_db
    from app.db.session import SessionLocal, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from app.auth.service import create_user
    from app.organizations.service import create_organization

    session = SessionLocal()
    try:
        # 관리자 계정 생성
        create_user(
            session,
            username=_ADMIN_USERNAME,
            password=_ADMIN_PASSWORD,
            is_admin=True,
        )
        # rename 테스트용 조직
        create_organization(session, name=_ORG_RENAME_TARGET)
        # 동명 충돌 테스트용 조직 2개
        create_organization(session, name=_ORG_DUP_EXISTING)
        create_organization(session, name=_ORG_DUP_SOURCE)
        # move 테스트용 조직 2개
        create_organization(session, name=_ORG_MOVE_SRC)
        create_organization(session, name=_ORG_MOVE_DEST)
        session.commit()
    finally:
        session.close()

    yield {
        "db_url": db_url,
        "db_path": db_path,
        "download_dir": download_dir,
    }

    monkeypatch.undo()
    get_settings.cache_clear()
    reset_engine_cache()


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _admin_login(page: Page, base_url: str) -> None:
    """관리자 계정으로 로그인하고 세션 쿠키를 확보한다."""
    page.goto(f"{base_url}/login")
    page.fill("input[name=username]", _ADMIN_USERNAME)
    page.fill("input[name=password]", _ADMIN_PASSWORD)
    page.click("button[type=submit]")
    # 로그인 성공 → / 로 redirect. /login 에서 벗어날 때까지 대기.
    page.wait_for_url(lambda url: "/login" not in url, timeout=5000)


def _get_org_row(page: Page, org_name: str):
    """org_name 을 포함하는 .org-tree__row locator 를 반환한다."""
    return page.locator(".org-tree__row").filter(
        has=page.locator(f".org-tree__name:text('{org_name}')")
    )


# ──────────────────────────────────────────────────────────────
# E2E 테스트
# ──────────────────────────────────────────────────────────────


def test_rename_org_success(e2e_page: Page, e2e_server: str) -> None:
    """조직명 변경이 성공하면 success flash 가 표시되고 새 이름이 트리에 보인다."""
    _admin_login(e2e_page, e2e_server)
    e2e_page.goto(f"{e2e_server}/admin/organizations")

    # 대상 조직 행에서 이름 변경 details 열기
    row = _get_org_row(e2e_page, _ORG_RENAME_TARGET)
    rename_details = row.locator("details.org-tree__rename-details")
    rename_details.locator("summary").click()

    # 새 이름 입력 후 제출
    rename_input = rename_details.locator("input[name=new_name]")
    rename_input.fill(_ORG_RENAME_NEW)
    rename_details.locator("button[type=submit]").click()

    # PRG 완료 후 success flash 와 변경된 이름 확인
    e2e_page.wait_for_url(re.compile(r"/admin/organizations"), timeout=5000)
    expect(e2e_page.locator(".admin-flash--success")).to_be_visible()
    expect(e2e_page.locator(".org-tree")).to_contain_text(_ORG_RENAME_NEW)


def test_move_org_success(e2e_page: Page, e2e_server: str) -> None:
    """조직 이동이 성공하면 success flash 가 표시된다."""
    _admin_login(e2e_page, e2e_server)
    e2e_page.goto(f"{e2e_server}/admin/organizations")

    # 이동 대상 조직 행에서 이동 details 열기
    row = _get_org_row(e2e_page, _ORG_MOVE_SRC)
    move_details = row.locator("details.org-tree__move-details")
    move_details.locator("summary").click()

    # 목적지 부모 선택
    move_details.locator("select[name=new_parent_id]").select_option(
        label=_ORG_MOVE_DEST
    )

    # confirm dialog 수락 후 제출
    e2e_page.on("dialog", lambda dialog: dialog.accept())
    move_details.locator("button[type=submit]").click()

    # PRG 완료 후 success flash 확인
    e2e_page.wait_for_url(re.compile(r"/admin/organizations"), timeout=5000)
    expect(e2e_page.locator(".admin-flash--success")).to_be_visible()


def test_rename_duplicate_shows_error(e2e_page: Page, e2e_server: str) -> None:
    """이미 존재하는 이름으로 rename 시도 시 error flash 가 표시된다."""
    _admin_login(e2e_page, e2e_server)
    e2e_page.goto(f"{e2e_server}/admin/organizations")

    # _ORG_DUP_SOURCE 를 _ORG_DUP_EXISTING 으로 rename 시도 (동명 충돌)
    row = _get_org_row(e2e_page, _ORG_DUP_SOURCE)
    rename_details = row.locator("details.org-tree__rename-details")
    rename_details.locator("summary").click()

    rename_input = rename_details.locator("input[name=new_name]")
    rename_input.fill(_ORG_DUP_EXISTING)
    rename_details.locator("button[type=submit]").click()

    # error flash 확인
    e2e_page.wait_for_url(re.compile(r"/admin/organizations"), timeout=5000)
    expect(e2e_page.locator(".admin-flash--error")).to_be_visible()
