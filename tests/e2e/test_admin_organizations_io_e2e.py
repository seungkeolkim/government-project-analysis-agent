"""조직 트리 Export/Import 관리자 UI E2E 테스트 (task 00058-2).

검증 시나리오:
    1. Export — Export 버튼 클릭 → JSON 파일 다운로드 → 유효한 조직 트리 JSON.
    2. Import 성공 — JSON 파일 업로드 → confirm 수락 → success flash + 조직 수 표시.
    3. Import FK 자동 정리 — 사용자 조직 매핑이 있던 조직을 포함하지 않는 트리로
       교체 → success flash 에 정리 건수 표시 + DB user_organizations 행 삭제 확인.

Playwright + uvicorn 서브프로세스 기반 E2E 이므로 Playwright 미설치 환경에서는
모듈 자체가 skip 된다 (conftest.py 와 동일 정책).
"""

from __future__ import annotations

import json
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

from playwright.sync_api import Browser, BrowserContext, Page, expect


pytestmark = pytest.mark.e2e

# ── 시드 상수 ────────────────────────────────────────────────────────────────

_ADMIN_USERNAME: str = "io_e2e_admin"
_ADMIN_PASSWORD: str = "io_e2e_admin_pw1"

# 일반 사용자 — 조직 매핑 FK 정리 테스트용
_TEST_USER_USERNAME: str = "io_e2e_user"
_TEST_USER_PASSWORD: str = "io_e2e_user_pw1"

# 시드 조직명
_ORG_ROOT: str = "io_root_org"
_ORG_CHILD: str = "io_child_org"
_ORG_MAPPED: str = "io_mapped_org"  # 일반 사용자가 매핑될 조직


# ──────────────────────────────────────────────────────────────
# Fixture — 이 모듈 전용 workspace
# ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def e2e_workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """조직 IO e2e 용 module-scope workspace.

    시드 데이터:
        - 관리자: io_e2e_admin
        - 일반 사용자: io_e2e_user (io_mapped_org 에 매핑)
        - 조직: io_root_org → io_child_org, io_mapped_org (루트)

    Yields:
        {"db_url", "db_path", "download_dir", "test_user_id"} dict.
    """
    monkeypatch = pytest.MonkeyPatch()
    workspace_dir: Path = tmp_path_factory.mktemp("e2e_org_io_workspace")
    db_path: Path = workspace_dir / "e2e.sqlite3"
    download_dir: Path = workspace_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    db_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setenv("DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    from app.config import get_settings
    from app.db.init_db import init_db
    from app.db.models import UserOrganization
    from app.db.session import SessionLocal, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from app.auth.service import create_user
    from app.organizations.service import create_organization

    session = SessionLocal()
    test_user_id: int
    try:
        create_user(session, username=_ADMIN_USERNAME, password=_ADMIN_PASSWORD, is_admin=True)
        test_user = create_user(
            session, username=_TEST_USER_USERNAME, password=_TEST_USER_PASSWORD, is_admin=False
        )
        test_user_id = test_user.id

        root_org = create_organization(session, name=_ORG_ROOT)
        create_organization(session, name=_ORG_CHILD, parent_id=root_org.id)
        mapped_org = create_organization(session, name=_ORG_MAPPED)

        # 일반 사용자를 io_mapped_org 에 매핑
        session.add(UserOrganization(user_id=test_user.id, organization_id=mapped_org.id))
        session.commit()
    finally:
        session.close()

    yield {
        "db_url": db_url,
        "db_path": db_path,
        "download_dir": download_dir,
        "test_user_id": test_user_id,
    }

    monkeypatch.undo()
    get_settings.cache_clear()
    reset_engine_cache()


@pytest.fixture
def e2e_download_page(
    e2e_browser: Browser,
    e2e_server: str,
    e2e_workspace: dict[str, Any],
) -> Iterator[Page]:
    """다운로드를 허용하는 BrowserContext 위의 Page fixture.

    표준 e2e_page 는 accept_downloads=False 이므로 Export 다운로드 테스트에서는
    이 fixture 를 사용한다.
    """
    download_dir = e2e_workspace["download_dir"]
    context: BrowserContext = e2e_browser.new_context(
        accept_downloads=True,
        downloads_path=str(download_dir),
    )
    page: Page = context.new_page()
    try:
        yield page
    finally:
        context.close()


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _admin_login(page: Page, base_url: str) -> None:
    """관리자 계정으로 로그인하고 세션 쿠키를 확보한다."""
    page.goto(f"{base_url}/login")
    page.fill("input[name=username]", _ADMIN_USERNAME)
    page.fill("input[name=password]", _ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=5000)


def _get_user_org_count(test_user_id: int) -> int:
    """DB 에서 특정 사용자의 user_organizations 행 수를 직접 조회한다."""
    from sqlalchemy import select

    from app.db.models import UserOrganization
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        rows = session.execute(
            select(UserOrganization).where(UserOrganization.user_id == test_user_id)
        ).scalars().all()
        return len(rows)
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────
# E2E 테스트
# ──────────────────────────────────────────────────────────────


def test_export_downloads_valid_json(
    e2e_download_page: Page, e2e_server: str
) -> None:
    """Export 버튼 클릭 시 유효한 JSON 파일이 다운로드된다."""
    _admin_login(e2e_download_page, e2e_server)
    e2e_download_page.goto(f"{e2e_server}/admin/organizations")

    # Export 버튼 클릭 → 다운로드 대기
    with e2e_download_page.expect_download(timeout=10000) as download_info:
        e2e_download_page.locator(".org-io-actions__export-btn").click()

    download = download_info.value
    download_path = download.path()

    assert download_path is not None, "다운로드 파일 경로가 None 입니다."

    # 파일명에 "organizations_" 가 포함돼야 한다
    assert "organizations_" in download.suggested_filename

    # 파일 내용이 유효한 JSON 이고 리스트여야 한다
    content = Path(download_path).read_text(encoding="utf-8")
    tree = json.loads(content)
    assert isinstance(tree, list), "export JSON 최상위가 list 가 아닙니다."

    # 시드 데이터 조직명이 포함돼야 한다
    all_names = json.dumps(tree, ensure_ascii=False)
    assert _ORG_ROOT in all_names
    assert _ORG_MAPPED in all_names

    # pretty-print 확인 — 개행이 있어야 한다
    assert "\n" in content


def test_import_success_shows_flash(
    e2e_page: Page,
    e2e_server: str,
    tmp_path: Path,
) -> None:
    """JSON 파일 업로드 후 confirm 수락 시 success flash 가 표시된다.

    io_mapped_org 를 포함하는 트리를 import 하여 사용자 매핑이 보존되도록 한다.
    (다음 테스트 test_import_drops_user_org_mappings 가 매핑 제거를 검증한다.)
    """
    # io_mapped_org 를 포함하는 트리를 준비한다
    new_tree = [
        {"name": _ORG_MAPPED, "children": []},
        {"name": "io_extra_org", "children": []},
    ]
    import_file = tmp_path / "import_with_mapped.json"
    import_file.write_text(
        json.dumps(new_tree, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _admin_login(e2e_page, e2e_server)
    e2e_page.goto(f"{e2e_server}/admin/organizations")

    # confirm 다이얼로그를 자동 수락
    e2e_page.on("dialog", lambda dialog: dialog.accept())

    # 파일 선택 후 Import 버튼 클릭
    e2e_page.locator("#org-import-file").set_input_files(str(import_file))
    e2e_page.locator(".org-io-actions__import-btn").click()

    # PRG 완료 후 success flash 확인
    e2e_page.wait_for_url(re.compile(r"/admin/organizations"), timeout=8000)
    expect(e2e_page.locator(".admin-flash--success")).to_be_visible()
    # flash 에 조직 수가 포함돼야 한다
    expect(e2e_page.locator(".admin-flash--success")).to_contain_text("2개 조직 등록")


def test_import_drops_user_org_mappings(
    e2e_page: Page,
    e2e_server: str,
    e2e_workspace: dict[str, Any],
    tmp_path: Path,
) -> None:
    """io_mapped_org 가 없는 트리로 import 시 사용자 매핑이 정리되고 flash 에 통보된다.

    이전 테스트(test_import_success_shows_flash) 후 DB 에 io_mapped_org 가 존재하므로
    사용자는 여전히 io_mapped_org 에 매핑된 상태다.
    완전히 다른 트리를 import 하면 매핑이 1건 정리되어야 한다.
    """
    test_user_id = e2e_workspace["test_user_id"]

    # io_mapped_org 를 포함하지 않는 트리 준비
    different_tree = [{"name": "io_completely_different", "children": []}]
    import_file = tmp_path / "import_different.json"
    import_file.write_text(
        json.dumps(different_tree, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _admin_login(e2e_page, e2e_server)
    e2e_page.goto(f"{e2e_server}/admin/organizations")

    e2e_page.on("dialog", lambda dialog: dialog.accept())
    e2e_page.locator("#org-import-file").set_input_files(str(import_file))
    e2e_page.locator(".org-io-actions__import-btn").click()

    e2e_page.wait_for_url(re.compile(r"/admin/organizations"), timeout=8000)

    # success flash 에 "정리" 키워드가 포함돼야 한다
    expect(e2e_page.locator(".admin-flash--success")).to_be_visible()
    expect(e2e_page.locator(".admin-flash--success")).to_contain_text("정리")

    # DB 직접 확인: 사용자의 user_organizations 가 0건이어야 한다
    remaining_count = _get_user_org_count(test_user_id)
    assert remaining_count == 0, (
        f"user_id={test_user_id} 의 user_organizations 가 {remaining_count}건 남아 있습니다. "
        "0건이어야 합니다."
    )
