"""게시글 리치 텍스트 에디터 E2E 테스트 (task 00153-2).

검증 시나리오(브라우저 실동작):
    1. editor_renders_on_form — 작성 폼 진입 시 평문 textarea 대신 리치 텍스트
       에디터(툴바 + contenteditable)가 노출되고, 자산이 static/ 에서 로드된다.
    2. suggestions_rich_roundtrip — 건의사항 게시글에 표·폰트·굵기·색상 서식을
       입력/주입(Word 가 만드는 형태의 HTML)하고 저장 → 상세 화면에서 동일 서식이
       렌더되는 전체 라운드트립. 이어서 수정 화면 진입 시 저장된 서식이 에디터에
       그대로 로드되는지 확인.
    3. toolbar_bold_applies — 툴바 굵게 버튼이 execCommand 로 실제 서식을 적용해
       저장된다(에디터 상호작용 경로 검증).
    4. notices_rich_render — 공지사항도 동일 패턴으로 표·폰트가 보존·렌더된다
       (나머지 게시판 커버).
    5. plain_post_backward_compatible — 기존 평문 게시글(body_format='plain')은
       상세 화면에서 깨짐 없이 표시된다(하위 호환).
    6. comment_textarea_stays_plain — 댓글 입력은 평문 textarea 그대로 유지되어
       리치 에디터가 적용되지 않는다(범위 외 보장).

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
_ADMIN_USERNAME: str = "e2e_rt_admin"
_ADMIN_PASSWORD: str = "e2e_rt_admin_pw1"
_USER_USERNAME: str = "e2e_rt_user"
_USER_PASSWORD: str = "e2e_rt_user_pw1"

# Word/Outlook 가 만드는 형태의 서식 있는 HTML 샘플.
# - 굵기(<b>), 폰트 색상(span style color), 폰트 종류(span style font-family),
#   표(table border + td style border) 를 모두 포함한다.
# - 모두 서버 sanitizer allowlist 가 보존하는 요소이므로 저장 후에도 살아남아야
#   한다(클라이언트 허용 서식 ↔ 서버 allowlist 정합성 검증).
_WORD_LIKE_HTML: str = (
    "<p><b>굵은 제목 문장</b>입니다.</p>"
    '<p><span style="color: #ff0000; font-family: \'Times New Roman\';">'
    "빨간 타임스뉴로만 글자</span></p>"
    '<table style="border-collapse: collapse;" border="1">'
    '<tr><td style="border: 1px solid #999999; padding: 6px;">셀A1</td>'
    '<td style="border: 1px solid #999999; padding: 6px;">셀B1</td></tr>'
    '<tr><td style="border: 1px solid #999999; padding: 6px;">셀A2</td>'
    '<td style="border: 1px solid #999999; padding: 6px;">셀B2</td></tr>'
    "</table>"
)


# ──────────────────────────────────────────────────────────────
# Fixture — 이 모듈용 workspace (conftest e2e_workspace 를 모듈 수준에서 대체)
# ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def e2e_workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """리치 텍스트 e2e 용 module-scope workspace.

    메인 DB + boards DB(notices·suggestions)를 모두 tmp_path 에 격리한다.
    하위 호환 검증용으로 평문(body_format='plain') 건의사항 1건을 미리 시드한다.
    """
    monkeypatch = pytest.MonkeyPatch()
    workspace_dir: Path = tmp_path_factory.mktemp("e2e_rich_text_workspace")
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

    init_db()

    from app.suggestions import (
        SuggestionsSessionLocal,
        init_suggestions_db,
    )
    from app.suggestions.repository import create_suggestion

    init_suggestions_db()

    from app.auth.service import create_user

    session = SessionLocal()
    try:
        admin = create_user(
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
        admin_id = admin.id
    finally:
        session.close()

    # 하위 호환 검증용 평문 건의사항 1건 시드(body_format 기본 'plain').
    boards_session = SuggestionsSessionLocal()
    try:
        plain_suggestion = create_suggestion(
            boards_session,
            author_user_id=admin_id,
            author_name=_ADMIN_USERNAME,
            title="평문 하위호환 글",
            body="평문 본문\n둘째 줄",
            password_hash="not-used-in-this-test",
            is_secret=False,
            contact_email=None,
        )
        boards_session.commit()
        plain_suggestion_id = plain_suggestion.id
    finally:
        boards_session.close()

    yield {
        "db_url": db_url,
        "boards_db_url": boards_db_url,
        "plain_suggestion_id": plain_suggestion_id,
    }

    monkeypatch.undo()
    get_settings.cache_clear()
    reset_engine_cache()
    reset_suggestions_engine_cache()


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _login(page: Page, base_url: str, username: str, password: str) -> None:
    """주어진 계정으로 로그인한다."""
    page.goto(f"{base_url}/login")
    page.fill("input[name=username]", username)
    page.fill("input[name=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=5000)


def _inject_editor_html(page: Page, html: str) -> None:
    """현재 페이지의 리치 에디터 편집 영역에 HTML 을 주입하고 input 이벤트를 쏜다.

    실제 Word/Outlook 붙여넣기를 클립보드로 재현하기 어려우므로, Word 가 만드는
    형태의 HTML 을 편집 영역에 직접 주입해 동치 검증한다(guidance 허용).
    """
    page.eval_on_selector(
        ".rich-text-editor__content",
        """(element, value) => {
            element.innerHTML = value;
            element.dispatchEvent(new Event('input', { bubbles: true }));
        }""",
        html,
    )


# ──────────────────────────────────────────────────────────────
# E2E 테스트
# ──────────────────────────────────────────────────────────────


def test_editor_renders_on_form(e2e_page: Page, e2e_server: str) -> None:
    """작성 폼 진입 시 평문 textarea 가 숨겨지고 리치 에디터가 노출된다."""
    _login(e2e_page, e2e_server, _USER_USERNAME, _USER_PASSWORD)
    e2e_page.goto(f"{e2e_server}/suggestions/new")

    # 에디터 툴바 + 편집 영역이 보인다.
    expect(e2e_page.locator(".rich-text-editor__toolbar")).to_be_visible()
    expect(e2e_page.locator(".rich-text-editor__content")).to_be_visible()
    # 원본 textarea 는 숨겨진다(에디터가 display:none 처리).
    expect(e2e_page.locator("textarea[name=body]")).to_be_hidden()
    # body_format hidden 필드가 'html' 로 세팅된다(라우트 계약).
    body_format_value = e2e_page.eval_on_selector(
        "input[name=body_format]", "el => el.value"
    )
    assert body_format_value == "html"

    # 에디터 자산이 외부 CDN 이 아니라 static/ 에서 로드된다.
    script_src = e2e_page.eval_on_selector(
        "script[src*='rich_text_editor']", "el => el.getAttribute('src')"
    )
    assert script_src.startswith("/static/"), script_src


def test_suggestions_rich_roundtrip(e2e_page: Page, e2e_server: str) -> None:
    """건의사항에 서식(표·폰트·색상·굵기)을 저장 → 상세 렌더 → 수정 로드까지 검증."""
    _login(e2e_page, e2e_server, _USER_USERNAME, _USER_PASSWORD)
    e2e_page.goto(f"{e2e_server}/suggestions/new")

    expect(e2e_page.locator(".rich-text-editor__content")).to_be_visible()
    e2e_page.fill("input[name=title]", "리치 텍스트 라운드트립")
    e2e_page.fill("input[name=password]", "rtpw1234")
    _inject_editor_html(e2e_page, _WORD_LIKE_HTML)

    e2e_page.click("button[type=submit]")
    e2e_page.wait_for_url(lambda url: "/suggestions/" in url, timeout=5000)

    # ── 상세 화면 — 저장된 서식이 렌더된다 ──────────────────────────────
    rich_body = e2e_page.locator(".suggestion-detail__body--rich")
    expect(rich_body).to_be_visible()
    # 표가 렌더되고 셀 텍스트가 보인다.
    expect(rich_body.locator("table")).to_be_visible()
    expect(rich_body.locator("td", has_text="셀A1")).to_be_visible()
    # 굵기 서식 보존.
    expect(rich_body.locator("b", has_text="굵은 제목 문장")).to_be_visible()
    # 폰트 색상 보존 — 빨간 글자의 computed color 가 rgb(255, 0, 0).
    red_color = e2e_page.eval_on_selector(
        ".suggestion-detail__body--rich span",
        "el => getComputedStyle(el).color",
    )
    assert red_color == "rgb(255, 0, 0)", red_color
    # 표 셀에 보더가 렌더된다(표시 CSS 적용 확인).
    cell_border = e2e_page.eval_on_selector(
        ".suggestion-detail__body--rich td",
        "el => getComputedStyle(el).borderTopWidth",
    )
    assert cell_border != "0px", cell_border

    # ── 수정 화면 — 저장된 서식이 에디터에 그대로 로드된다 ──────────────
    detail_url = e2e_page.url
    suggestion_id = detail_url.rstrip("/").split("/")[-1]
    e2e_page.goto(f"{e2e_server}/suggestions/{suggestion_id}/edit")

    expect(e2e_page.locator(".rich-text-editor__content")).to_be_visible()
    expect(
        e2e_page.locator(".rich-text-editor__content table")
    ).to_be_visible()
    expect(
        e2e_page.locator(".rich-text-editor__content b", has_text="굵은 제목 문장")
    ).to_be_visible()


def test_toolbar_bold_applies(e2e_page: Page, e2e_server: str) -> None:
    """툴바 굵게 버튼이 실제 서식을 적용해 저장된다(에디터 상호작용 경로)."""
    _login(e2e_page, e2e_server, _USER_USERNAME, _USER_PASSWORD)
    e2e_page.goto(f"{e2e_server}/suggestions/new")

    editable = e2e_page.locator(".rich-text-editor__content")
    expect(editable).to_be_visible()
    e2e_page.fill("input[name=title]", "툴바 굵게 검증")
    e2e_page.fill("input[name=password]", "rtpw1234")

    # 편집 영역에 텍스트 입력 후 전체 선택 → 굵게 버튼 클릭.
    editable.click()
    e2e_page.keyboard.type("툴바로 굵게 만든 문장")
    e2e_page.keyboard.press("Control+A")
    e2e_page.locator(".rich-text-editor__btn", has_text="B").first.click()

    e2e_page.click("button[type=submit]")
    e2e_page.wait_for_url(lambda url: "/suggestions/" in url, timeout=5000)

    rich_body = e2e_page.locator(".suggestion-detail__body--rich")
    # 굵게(<b>/<strong>)로 감싼 텍스트가 저장·렌더된다.
    bold_text = e2e_page.eval_on_selector_all(
        ".suggestion-detail__body--rich b, .suggestion-detail__body--rich strong",
        "els => els.map(e => e.textContent).join('')",
    )
    assert "툴바로 굵게 만든 문장" in bold_text, bold_text
    expect(rich_body).to_contain_text("툴바로 굵게 만든 문장")


def test_notices_rich_render(e2e_page: Page, e2e_server: str) -> None:
    """공지사항도 동일 패턴으로 표·폰트가 보존·렌더된다."""
    _login(e2e_page, e2e_server, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    e2e_page.goto(f"{e2e_server}/notices/new")

    expect(e2e_page.locator(".rich-text-editor__content")).to_be_visible()
    e2e_page.fill("input[name=title]", "공지 리치 텍스트")
    _inject_editor_html(e2e_page, _WORD_LIKE_HTML)

    e2e_page.click("button[type=submit]")
    e2e_page.wait_for_url(lambda url: "/notices/" in url, timeout=5000)

    rich_body = e2e_page.locator(".suggestion-detail__body--rich")
    expect(rich_body).to_be_visible()
    expect(rich_body.locator("table")).to_be_visible()
    expect(rich_body.locator("td", has_text="셀A1")).to_be_visible()
    expect(rich_body.locator("b", has_text="굵은 제목 문장")).to_be_visible()


def test_plain_post_backward_compatible(
    e2e_page: Page, e2e_server: str, e2e_workspace: dict[str, Any]
) -> None:
    """기존 평문 게시글(body_format='plain')은 상세에서 깨짐 없이 표시된다."""
    plain_id = e2e_workspace["plain_suggestion_id"]
    _login(e2e_page, e2e_server, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    e2e_page.goto(f"{e2e_server}/suggestions/{plain_id}")

    # 평문 본문은 --rich 변형이 아닌 기본 .suggestion-detail__body 로 렌더된다.
    plain_body = e2e_page.locator(
        ".suggestion-detail__body:not(.suggestion-detail__body--rich)"
    )
    expect(plain_body).to_be_visible()
    expect(plain_body).to_contain_text("평문 본문")
    # 리치 변형 컨테이너는 없어야 한다.
    expect(e2e_page.locator(".suggestion-detail__body--rich")).to_have_count(0)


def test_comment_textarea_stays_plain(e2e_page: Page, e2e_server: str) -> None:
    """댓글 입력은 평문 textarea 그대로 유지되어 리치 에디터가 붙지 않는다."""
    _login(e2e_page, e2e_server, _USER_USERNAME, _USER_PASSWORD)

    # 리치 텍스트 글을 1건 작성해 상세로 진입.
    e2e_page.goto(f"{e2e_server}/suggestions/new")
    expect(e2e_page.locator(".rich-text-editor__content")).to_be_visible()
    e2e_page.fill("input[name=title]", "댓글 평문 검증 글")
    e2e_page.fill("input[name=password]", "rtpw1234")
    _inject_editor_html(e2e_page, "<p>본문 텍스트</p>")
    e2e_page.click("button[type=submit]")
    e2e_page.wait_for_url(lambda url: "/suggestions/" in url, timeout=5000)

    # 댓글 작성 textarea 는 평범한 textarea 이며 data-rich-editor 가 없어야 한다.
    comment_textarea = e2e_page.locator(".suggestion-comment-form__textarea")
    expect(comment_textarea).to_be_visible()
    has_rich_attr = e2e_page.eval_on_selector(
        ".suggestion-comment-form__textarea",
        "el => el.hasAttribute('data-rich-editor')",
    )
    assert has_rich_attr is False
    # 댓글 영역에는 리치 에디터 편집 영역이 없다.
    expect(
        e2e_page.locator(
            ".suggestion-comments-section .rich-text-editor__content"
        )
    ).to_have_count(0)
