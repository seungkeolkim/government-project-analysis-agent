"""게시글 리치 텍스트 폼·저장·렌더 통합 테스트 (task 00153-2).

브라우저 E2E(``tests/e2e/test_board_rich_text_e2e.py``)는 Playwright/chromium 이
없는 환경에서 skip 되므로, 본 통합 테스트가 서버측 통합(폼 마크업 → HTML 본문
저장 → 상세 렌더 → 수정 화면 로드)을 브라우저 없이 검증한다.

검증 범위:
    - 작성/수정 폼이 리치 텍스트 에디터 자산(rich_text_editor.js)과 hidden
      body_format 필드, data-rich-editor textarea 를 노출한다(외부 CDN 미사용).
    - body_format='html' 로 제출한 Word 형태 HTML(표·폰트·색상·굵기)이 서버측
      sanitization 을 거쳐 저장되고, 상세 화면에서 .suggestion-detail__body--rich
      컨테이너에 |safe 렌더된다.
    - 수정 화면이 저장된 HTML 과 data-initial-format='html' 을 그대로 로드한다.
    - 공지사항·건의사항 두 게시판 모두 동일하게 동작한다.
    - 댓글 입력 textarea 는 평문 그대로(리치 에디터 미적용) 유지된다(범위 외).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.suggestions.models import Base as SuggestionsBase

# Word/Outlook 가 만드는 형태의 서식 있는 HTML — 굵기/폰트색/폰트종류/표 포함.
_WORD_LIKE_HTML: str = (
    "<p><b>굵은 제목 문장</b>입니다.</p>"
    '<p><span style="color: #ff0000; font-family: \'Times New Roman\';">'
    "빨간 타임스뉴로만 글자</span></p>"
    '<table style="border-collapse: collapse;" border="1">'
    '<tr><td style="border: 1px solid #999999; padding: 6px;">셀A1</td>'
    '<td style="border: 1px solid #999999; padding: 6px;">셀B1</td></tr>'
    "</table>"
    '<p onclick="alert(1)">이벤트 핸들러는 제거되어야 함</p>'
    "<script>alert('xss')</script>"
)


# ──────────────────────────────────────────────────────────────
# DB / 클라이언트 픽스처 (boards DB 격리)
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def _test_suggestions_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """건의사항/공지사항 공유 boards DB URL 을 tmp_path 임시 파일로 치환한다."""
    db_file = tmp_path / "boards_test.sqlite3"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("SUGGESTIONS_DB_URL", db_url)
    return db_url


@pytest.fixture
def suggestions_test_engine(
    _test_suggestions_db_url: str,
    test_engine: Engine,
) -> Iterator[Engine]:
    """격리된 boards SQLite 엔진(notices·suggestions 테이블 생성)."""
    from app.config import get_settings
    from app.suggestions.session import (
        get_suggestions_engine,
        reset_suggestions_engine_cache,
    )

    get_settings.cache_clear()
    reset_suggestions_engine_cache()

    engine = get_suggestions_engine()
    SuggestionsBase.metadata.create_all(engine)

    try:
        yield engine
    finally:
        engine.dispose()
        reset_suggestions_engine_cache()
        get_settings.cache_clear()


@pytest.fixture
def suggestions_session(suggestions_test_engine: Engine) -> Iterator[Session]:
    """테스트용 boards DB 세션."""
    factory = sessionmaker(
        bind=suggestions_test_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(suggestions_test_engine: Engine) -> Iterator[TestClient]:
    """메인 DB + boards DB 가 모두 격리된 TestClient."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, username: str, password: str) -> None:
    """로그인 후 TestClient 쿠키에 세션을 남긴다."""
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
        db_session, username="rt_admin", password="Admin_pass_1!", is_admin=True
    )
    db_session.commit()
    _login(client, "rt_admin", "Admin_pass_1!")
    return client


@pytest.fixture
def user_client(client: TestClient, db_session: Session) -> TestClient:
    """일반 사용자로 로그인된 TestClient(건의사항 작성용)."""
    from app.auth.service import create_user

    create_user(db_session, username="rt_user", password="User_pass_1!")
    db_session.commit()
    _login(client, "rt_user", "User_pass_1!")
    return client


# ──────────────────────────────────────────────────────────────
# 작성 폼 마크업 검증
# ──────────────────────────────────────────────────────────────


def test_notice_new_form_has_editor_assets(admin_client: TestClient) -> None:
    """공지 작성 폼이 에디터 자산·hidden body_format·data-rich-editor 를 노출한다."""
    resp = admin_client.get("/notices/new")
    assert resp.status_code == 200
    html = resp.text
    assert "data-rich-editor" in html
    assert 'name="body_format"' in html
    assert "/static/js/rich_text_editor.js" in html
    # 외부 CDN 을 쓰지 않는다.
    assert "http://" not in html.split("<body")[0] or "cdn" not in html.lower()


def test_suggestion_new_form_has_editor_assets(user_client: TestClient) -> None:
    """건의 작성 폼이 에디터 자산·hidden body_format·data-rich-editor 를 노출한다."""
    resp = user_client.get("/suggestions/new")
    assert resp.status_code == 200
    html = resp.text
    assert "data-rich-editor" in html
    assert 'name="body_format"' in html
    assert "/static/js/rich_text_editor.js" in html


# ──────────────────────────────────────────────────────────────
# 라운드트립 — 저장(정화) → 상세 렌더 → 수정 로드
# ──────────────────────────────────────────────────────────────


def test_notice_rich_roundtrip(admin_client: TestClient) -> None:
    """공지 게시글 HTML 저장 → 상세 |safe 렌더 → 수정 화면 로드 라운드트립."""
    resp = admin_client.post(
        "/notices",
        data={
            "title": "공지 리치 텍스트",
            "body": _WORD_LIKE_HTML,
            "body_format": "html",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail_path = resp.headers["location"]

    detail = admin_client.get(detail_path)
    assert detail.status_code == 200
    detail_html = detail.text
    # 리치 본문 컨테이너에 표·폰트·굵기가 렌더된다.
    assert "suggestion-detail__body--rich" in detail_html
    assert "<table" in detail_html
    assert "셀A1" in detail_html
    assert "<b>굵은 제목 문장</b>" in detail_html
    assert "color: #ff0000" in detail_html
    # XSS 벡터는 서버 sanitization 으로 제거된다.
    assert "<script>" not in detail_html
    assert "onclick" not in detail_html

    # 수정 화면이 저장된 HTML + data-initial-format='html' 을 로드한다.
    notice_id = detail_path.rstrip("/").split("/")[-1]
    edit = admin_client.get(f"/notices/{notice_id}/edit")
    assert edit.status_code == 200
    edit_html = edit.text
    assert 'data-initial-format="html"' in edit_html
    # textarea 안에 저장된 본문이 (escape 된 형태로) 들어 있다.
    assert "&lt;table" in edit_html or "&lt;b&gt;" in edit_html


def _latest_suggestion_id(session: Session) -> int:
    """boards DB 에서 가장 최근(최대 id) 건의사항 PK 를 반환한다.

    건의사항 작성 라우트는 PRG 로 목록(`/suggestions`)으로 리다이렉트하므로
    상세 경로를 직접 만들기 위해 DB 에서 신규 글 id 를 조회한다.
    """
    from sqlalchemy import select

    from app.suggestions.models import Suggestion

    row = session.execute(
        select(Suggestion.id).order_by(Suggestion.id.desc()).limit(1)
    ).scalar_one()
    return int(row)


def test_suggestion_rich_roundtrip(
    user_client: TestClient, suggestions_session: Session
) -> None:
    """건의 게시글 HTML 저장 → 상세 |safe 렌더 → 수정 화면 로드 라운드트립."""
    resp = user_client.post(
        "/suggestions",
        data={
            "title": "건의 리치 텍스트",
            "body": _WORD_LIKE_HTML,
            "body_format": "html",
            "password": "postpw123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    suggestion_id = _latest_suggestion_id(suggestions_session)
    detail_path = f"/suggestions/{suggestion_id}"

    detail = user_client.get(detail_path)
    assert detail.status_code == 200
    detail_html = detail.text
    assert "suggestion-detail__body--rich" in detail_html
    assert "<table" in detail_html
    assert "셀A1" in detail_html
    assert "<b>굵은 제목 문장</b>" in detail_html
    assert "<script>" not in detail_html
    assert "onclick" not in detail_html

    edit = user_client.get(f"/suggestions/{suggestion_id}/edit")
    assert edit.status_code == 200
    assert 'data-initial-format="html"' in edit.text


# ──────────────────────────────────────────────────────────────
# 하위 호환 / 범위 경계
# ──────────────────────────────────────────────────────────────


def test_plain_notice_still_renders_without_rich(admin_client: TestClient) -> None:
    """body_format 미지정(평문) 게시글은 --rich 컨테이너 없이 기존대로 표시된다."""
    resp = admin_client.post(
        "/notices",
        data={"title": "평문 공지", "body": "평문 본문\n둘째 줄"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail = admin_client.get(resp.headers["location"])
    assert detail.status_code == 200
    detail_html = detail.text
    assert "평문 본문" in detail_html
    assert "suggestion-detail__body--rich" not in detail_html


def test_comment_textarea_stays_plain(
    user_client: TestClient, suggestions_session: Session
) -> None:
    """댓글 입력 textarea 는 평문 그대로(리치 에디터 미적용) 유지된다."""
    resp = user_client.post(
        "/suggestions",
        data={
            "title": "댓글 평문 검증 글",
            "body": "<p>본문</p>",
            "body_format": "html",
            "password": "postpw123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    suggestion_id = _latest_suggestion_id(suggestions_session)
    detail = user_client.get(f"/suggestions/{suggestion_id}")
    assert detail.status_code == 200
    # 댓글 작성 textarea 에는 data-rich-editor 가 붙지 않는다.
    comment_form_fragment = detail.text.split("suggestion-comment-form")[-1]
    assert "data-rich-editor" not in comment_form_fragment
