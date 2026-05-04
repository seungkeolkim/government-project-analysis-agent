"""공지사항 게시판 E2E 테스트 (task 00056-3).

검증 시나리오:
    1. footer_link_order — 공지사항 링크가 건의사항 링크 DOM 앞(왼쪽)에 있는지.
    2. footer_links_different_colors — 두 링크의 CSS color 값이 다른지.
    3. admin_can_create_notice — 관리자가 공지사항을 작성하고 상세 페이지로 이동.
    4. admin_can_edit_notice — 관리자가 공지사항을 수정하고 변경 내용 확인.
    5. admin_can_delete_notice — 관리자가 공지사항을 삭제하고 목록에서 사라짐 확인.
    6. non_admin_cannot_write — 일반 사용자는 작성 폼 대신 안내 메시지가 표시.
    7. notice_detail_no_comments — 상세 페이지에 댓글/수용여부/비밀글 UI 없음.

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
_ADMIN_USERNAME: str = "e2e_notice_admin"
_ADMIN_PASSWORD: str = "e2e_notice_admin_pw1"
_USER_USERNAME: str = "e2e_notice_user"
_USER_PASSWORD: str = "e2e_notice_user_pw1"


# ──────────────────────────────────────────────────────────────
# Fixture — 이 모듈용 workspace (conftest e2e_workspace 를 모듈 수준에서 대체)
# ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def e2e_workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """공지사항 e2e 용 module-scope workspace.

    메인 DB + boards DB 를 모두 tmp_path 에 격리한다.
    boards DB (SUGGESTIONS_DB_URL) 도 분리해야 notices 테이블이
    실제 운영 boards.sqlite3 와 충돌하지 않는다.
    """
    monkeypatch = pytest.MonkeyPatch()
    workspace_dir: Path = tmp_path_factory.mktemp("e2e_notices_workspace")
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
    from app.suggestions.session import reset_suggestions_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_suggestions_engine_cache()

    # 메인 DB 초기화
    init_db()

    # boards DB 초기화 (notices + suggestions 테이블 모두 생성)
    from app.suggestions import init_suggestions_db

    init_suggestions_db()

    # 관리자 + 일반 사용자 시드
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
            username=_USER_USERNAME,
            password=_USER_PASSWORD,
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


def _user_login(page: Page, base_url: str) -> None:
    """일반 사용자 계정으로 로그인한다."""
    page.goto(f"{base_url}/login")
    page.fill("input[name=username]", _USER_USERNAME)
    page.fill("input[name=password]", _USER_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=5000)


# ──────────────────────────────────────────────────────────────
# E2E 테스트
# ──────────────────────────────────────────────────────────────


def test_footer_link_order(e2e_page: Page, e2e_server: str) -> None:
    """공지사항 링크가 DOM 상 건의사항 링크보다 앞(왼쪽)에 있다."""
    e2e_page.goto(f"{e2e_server}/")

    notices_link = e2e_page.locator("a.footer-notices-link")
    suggestions_link = e2e_page.locator("a.footer-suggestions-link")

    expect(notices_link).to_be_visible()
    expect(suggestions_link).to_be_visible()

    # DOM 순서 검증: evaluate 로 두 링크의 compareDocumentPosition 을 확인.
    # DOCUMENT_POSITION_FOLLOWING(4) → notices 가 suggestions 보다 앞에 위치.
    is_notices_before = e2e_page.evaluate(
        """() => {
            const notices = document.querySelector('a.footer-notices-link');
            const suggestions = document.querySelector('a.footer-suggestions-link');
            if (!notices || !suggestions) return false;
            // Node.compareDocumentPosition: FOLLOWING = 4
            return !!(notices.compareDocumentPosition(suggestions) & Node.DOCUMENT_POSITION_FOLLOWING);
        }"""
    )
    assert is_notices_before, "공지사항 링크가 건의사항 링크보다 DOM 상 앞에 있어야 한다"


def test_footer_links_different_colors(e2e_page: Page, e2e_server: str) -> None:
    """공지사항 링크와 건의사항 링크의 CSS color 가 다르다."""
    e2e_page.goto(f"{e2e_server}/")

    notices_color = e2e_page.evaluate(
        "() => getComputedStyle(document.querySelector('a.footer-notices-link')).color"
    )
    suggestions_color = e2e_page.evaluate(
        "() => getComputedStyle(document.querySelector('a.footer-suggestions-link')).color"
    )

    assert notices_color != suggestions_color, (
        f"공지사항 링크({notices_color})와 건의사항 링크({suggestions_color})의 색상이 같습니다"
    )


def test_admin_can_create_notice(e2e_page: Page, e2e_server: str) -> None:
    """관리자가 공지사항을 작성하면 상세 페이지로 이동한다."""
    _admin_login(e2e_page, e2e_server)
    e2e_page.goto(f"{e2e_server}/notices/new")

    # 작성 폼이 표시되는지 확인
    expect(e2e_page.locator("form[action='/notices']")).to_be_visible()

    e2e_page.fill("input[name=title]", "E2E 테스트 공지")
    e2e_page.fill("textarea[name=body]", "E2E 테스트 공지 본문입니다.")
    e2e_page.click("button[type=submit]")

    # PRG 완료 후 상세 페이지 확인
    e2e_page.wait_for_url(lambda url: "/notices/" in url, timeout=5000)
    expect(e2e_page.locator("h3")).to_contain_text("E2E 테스트 공지")


def test_admin_can_edit_notice(e2e_page: Page, e2e_server: str) -> None:
    """관리자가 공지사항을 수정하면 변경된 내용이 표시된다."""
    _admin_login(e2e_page, e2e_server)

    # 공지사항 작성
    e2e_page.goto(f"{e2e_server}/notices/new")
    e2e_page.fill("input[name=title]", "수정 전 제목")
    e2e_page.fill("textarea[name=body]", "수정 전 본문")
    e2e_page.click("button[type=submit]")
    e2e_page.wait_for_url(lambda url: "/notices/" in url, timeout=5000)

    # 수정 페이지로 이동
    detail_url = e2e_page.url
    notice_id = detail_url.rstrip("/").split("/")[-1]
    e2e_page.goto(f"{e2e_server}/notices/{notice_id}/edit")

    # 수정 폼 확인 후 내용 변경
    title_input = e2e_page.locator("input[name=title]")
    expect(title_input).to_have_value("수정 전 제목")
    title_input.fill("수정 후 제목")
    e2e_page.fill("textarea[name=body]", "수정 후 본문")
    e2e_page.click("button[type=submit]")

    # 수정 완료 후 상세 페이지 확인
    e2e_page.wait_for_url(lambda url: f"/notices/{notice_id}" in url, timeout=5000)
    expect(e2e_page.locator("h3")).to_contain_text("수정 후 제목")


def test_admin_can_delete_notice(e2e_page: Page, e2e_server: str) -> None:
    """관리자가 공지사항을 삭제하면 목록에서 사라진다."""
    _admin_login(e2e_page, e2e_server)

    # 공지사항 작성
    e2e_page.goto(f"{e2e_server}/notices/new")
    e2e_page.fill("input[name=title]", "삭제 대상 공지")
    e2e_page.fill("textarea[name=body]", "이 공지는 삭제됩니다.")
    e2e_page.click("button[type=submit]")
    e2e_page.wait_for_url(lambda url: "/notices/" in url, timeout=5000)
    detail_url = e2e_page.url

    # confirm dialog 수락 후 삭제
    e2e_page.on("dialog", lambda dialog: dialog.accept())
    e2e_page.click("button.suggestion-form__delete-button")

    # 목록 페이지로 리다이렉트 확인
    e2e_page.wait_for_url(lambda url: url.rstrip("/").endswith("/notices"), timeout=5000)

    # 목록에서 사라졌는지 확인
    expect(e2e_page.locator("text=삭제 대상 공지")).not_to_be_visible()


def test_non_admin_cannot_write(e2e_page: Page, e2e_server: str) -> None:
    """일반 사용자가 /notices/new 에 접근하면 안내 메시지가 표시된다(폼 없음)."""
    _user_login(e2e_page, e2e_server)
    e2e_page.goto(f"{e2e_server}/notices/new")

    # 작성 폼이 없어야 한다
    expect(e2e_page.locator("form[action='/notices']")).not_to_be_visible()
    # 안내 메시지가 표시되어야 한다
    expect(e2e_page.locator(".suggestion-login-required")).to_be_visible()


def test_notice_detail_no_comments_or_acceptance(
    e2e_page: Page, e2e_server: str
) -> None:
    """상세 페이지에 댓글·수용여부·비밀글 UI 가 없다."""
    _admin_login(e2e_page, e2e_server)

    # 공지사항 작성
    e2e_page.goto(f"{e2e_server}/notices/new")
    e2e_page.fill("input[name=title]", "UI 검증용 공지")
    e2e_page.fill("textarea[name=body]", "댓글, 수용여부, 비밀글 UI 없음을 검증한다.")
    e2e_page.click("button[type=submit]")
    e2e_page.wait_for_url(lambda url: "/notices/" in url, timeout=5000)

    # 댓글 입력폼 없음
    expect(e2e_page.locator("form[action*='/comments']")).not_to_be_visible()
    # 수용여부 모달 버튼 없음
    expect(e2e_page.locator(".suggestion-acceptance-button")).not_to_be_visible()
    # 비밀글 배지 없음
    expect(e2e_page.locator(".suggestion-status-badge--secret")).not_to_be_visible()
