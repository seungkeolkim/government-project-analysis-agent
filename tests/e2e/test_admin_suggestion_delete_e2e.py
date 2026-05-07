"""관리자 건의사항 삭제 E2E 테스트 (task 00081-2).

검증 시나리오:
    1. test_admin_sees_delete_button_not_edit_on_others_suggestion
       — 관리자가 타인의 건의사항 상세 페이지에서 삭제 버튼은 보이고 수정 버튼은 보이지 않는다.
    2. test_admin_can_delete_others_suggestion
       — 관리자가 타인의 건의사항을 삭제하면 목록 페이지로 리다이렉트되고 해당 글이 사라진다.
    3. test_author_sees_both_edit_and_delete_on_own_suggestion
       — 작성자 본인은 자신의 건의사항에서 수정·삭제 버튼을 모두 볼 수 있다 (회귀 방지).

Playwright + uvicorn 서브프로세스 기반 E2E 이므로 Playwright 미설치 환경에서는
모듈 자체가 skip 된다 (conftest.py 와 동일 정책).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright 가 설치되지 않은 환경에서는 E2E 테스트를 건너뜁니다.",
)

from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

# 시드 계정 정보
_ADMIN_USERNAME: str = "e2e_sug_admin"
_ADMIN_PASSWORD: str = "e2e_sug_admin_pw1"
_AUTHOR_USERNAME: str = "e2e_sug_author"
_AUTHOR_PASSWORD: str = "e2e_sug_author_pw1"

# 건의사항 게시글 비밀번호 (작성 폼 필수 입력)
_POST_PASSWORD: str = "post1234"


# ──────────────────────────────────────────────────────────────
# Fixture — 이 모듈용 workspace (conftest e2e_workspace 를 모듈 수준에서 대체)
# ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def e2e_workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """건의사항 관리자 삭제 e2e 용 module-scope workspace.

    메인 DB(DB_URL) + 게시판 DB(SUGGESTIONS_DB_URL) 를 모두 tmp_path 에 격리해
    운영 DB 와 충돌을 방지한다. 관리자 계정과 일반 작성자 계정을 시드한다.
    """
    monkeypatch = pytest.MonkeyPatch()
    workspace_dir: Path = tmp_path_factory.mktemp("e2e_admin_sug_delete_workspace")
    db_path: Path = workspace_dir / "e2e.sqlite3"
    boards_db_path: Path = workspace_dir / "boards.sqlite3"
    download_dir: Path = workspace_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    db_url = f"sqlite:///{db_path.as_posix()}"
    boards_db_url = f"sqlite:///{boards_db_path.as_posix()}"
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setenv("SUGGESTIONS_DB_URL", boards_db_url)
    monkeypatch.setenv("DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    from app.config import get_settings
    from app.db.init_db import init_db
    from app.db.session import SessionLocal, reset_engine_cache
    from app.suggestions import init_suggestions_db
    from app.suggestions.session import reset_suggestions_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_suggestions_engine_cache()

    # 메인 DB 초기화 (Alembic upgrade head)
    init_db()

    # 게시판 DB 초기화 (suggestions + notices 테이블)
    init_suggestions_db()

    # 관리자 + 일반 작성자 계정 시드
    from app.auth.service import create_user

    session = SessionLocal()
    try:
        create_user(
            session,
            username=_ADMIN_USERNAME,
            password=_ADMIN_PASSWORD,
            is_admin=True,
        )
        create_user(
            session,
            username=_AUTHOR_USERNAME,
            password=_AUTHOR_PASSWORD,
            is_admin=False,
        )
        session.commit()
    finally:
        session.close()

    yield {
        "db_url": db_url,
        "boards_db_url": boards_db_url,
        "db_path": db_path,
        "download_dir": download_dir,
    }

    monkeypatch.undo()
    get_settings.cache_clear()
    reset_engine_cache()
    reset_suggestions_engine_cache()


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


def _author_login(page: Page, base_url: str) -> None:
    """일반 작성자 계정으로 로그인한다."""
    page.goto(f"{base_url}/login")
    page.fill("input[name=username]", _AUTHOR_USERNAME)
    page.fill("input[name=password]", _AUTHOR_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=5000)


def _create_suggestion(page: Page, base_url: str, title: str) -> str:
    """현재 로그인 세션으로 건의사항을 작성하고 상세 페이지 URL을 반환한다.

    Args:
        page: 현재 로그인 상태의 Playwright Page.
        base_url: 서버 base URL.
        title: 건의사항 제목 (목록에서 클릭해 상세 URL 을 찾는 데 사용).

    Returns:
        작성 완료 후 도달한 상세 페이지 URL (e.g. "http://…/suggestions/3").
    """
    page.goto(f"{base_url}/suggestions/new")
    page.fill("input[name=title]", title)
    page.fill("textarea[name=body]", f"{title}의 E2E 테스트 본문입니다.")
    page.fill("input[name=password]", _POST_PASSWORD)
    page.click("button[type=submit]")
    # PRG 후 목록 페이지로 이동 확인
    page.wait_for_url(
        lambda url: url.rstrip("/").endswith("/suggestions"),
        timeout=5000,
    )
    # 방금 작성한 글의 제목 링크를 클릭해 상세 URL 확보
    page.locator(f"a:has-text(\"{title}\")").first.click()
    page.wait_for_url(
        lambda url: "/suggestions/" in url
        and not url.rstrip("/").endswith("/suggestions"),
        timeout=5000,
    )
    return page.url


# ──────────────────────────────────────────────────────────────
# E2E 테스트
# ──────────────────────────────────────────────────────────────


def test_admin_sees_delete_button_not_edit_on_others_suggestion(
    e2e_page: Page, e2e_server: str
) -> None:
    """관리자는 타인의 건의사항 상세 페이지에서 삭제 버튼은 보이고 수정 버튼은 보이지 않는다."""
    # 1. 작성자가 건의사항을 작성한다.
    _author_login(e2e_page, e2e_server)
    detail_url = _create_suggestion(e2e_page, e2e_server, "관리자 버튼 가시성 E2E")

    # 2. 관리자 세션으로 전환한다.
    e2e_page.context.clear_cookies()
    _admin_login(e2e_page, e2e_server)

    # 3. 해당 건의사항 상세 페이지로 이동한다.
    e2e_page.goto(detail_url)

    delete_button = e2e_page.locator("button.suggestion-detail__owner-action--delete")
    edit_link = e2e_page.locator("a.suggestion-detail__owner-action--edit")

    # 관리자에게 삭제 버튼이 보여야 한다.
    expect(delete_button).to_be_visible()
    # 관리자는 수정 권한이 없으므로 수정 버튼은 보이지 않아야 한다.
    expect(edit_link).not_to_be_visible()


def test_admin_can_delete_others_suggestion(
    e2e_page: Page, e2e_server: str
) -> None:
    """관리자가 타인의 건의사항을 삭제하면 목록 페이지로 이동하고 해당 글이 사라진다."""
    # 1. 작성자가 건의사항을 작성한다.
    _author_login(e2e_page, e2e_server)
    suggestion_title = "관리자 삭제 대상 E2E"
    detail_url = _create_suggestion(e2e_page, e2e_server, suggestion_title)

    # 2. 관리자 세션으로 전환한다.
    e2e_page.context.clear_cookies()
    _admin_login(e2e_page, e2e_server)
    e2e_page.goto(detail_url)

    # 3. confirm 다이얼로그를 수락하고 삭제 버튼을 클릭한다.
    e2e_page.on("dialog", lambda dialog: dialog.accept())
    e2e_page.click("button.suggestion-detail__owner-action--delete")

    # 4. 목록 페이지로 리다이렉트되어야 한다.
    e2e_page.wait_for_url(
        lambda url: url.rstrip("/").endswith("/suggestions"),
        timeout=5000,
    )

    # 5. 목록에서 해당 글이 보이지 않아야 한다.
    expect(e2e_page.locator(f"text={suggestion_title}")).not_to_be_visible()


def test_author_sees_both_edit_and_delete_on_own_suggestion(
    e2e_page: Page, e2e_server: str
) -> None:
    """작성자 본인은 자신의 건의사항에서 수정·삭제 버튼을 모두 볼 수 있다 (회귀 방지)."""
    # 작성자가 건의사항을 작성한다.
    _author_login(e2e_page, e2e_server)
    detail_url = _create_suggestion(e2e_page, e2e_server, "작성자 회귀 확인 E2E")

    # 동일 세션으로 상세 페이지를 다시 방문한다.
    e2e_page.goto(detail_url)

    delete_button = e2e_page.locator("button.suggestion-detail__owner-action--delete")
    edit_link = e2e_page.locator("a.suggestion-detail__owner-action--edit")

    # 작성자에게는 수정·삭제 버튼 모두 보여야 한다.
    expect(delete_button).to_be_visible()
    expect(edit_link).to_be_visible()
