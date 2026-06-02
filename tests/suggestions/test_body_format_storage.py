"""body_format 저장·복원 repository 단위 테스트 (task 00153-1).

검증 범위:
    - create_notice / create_suggestion: body_format 미지정 시 기본 'plain'(하위 호환),
      'html' 명시 시 그대로 저장.
    - update_notice / update_suggestion: body_format 갱신이 반영된다.
    - 저장된 본문(HTML 문자열) 이 손상 없이 복원된다.

FastAPI / 서버 기동 없이 tmp SQLite + create_all 로 두 게시판 테이블을 만든다.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.notices.models  # noqa: F401 — Notice 를 Base.metadata 에 등록
from app.notices.repository import create_notice, get_notice_by_id, update_notice
from app.suggestions.models import BODY_FORMAT_HTML, BODY_FORMAT_PLAIN, Base
from app.suggestions.repository import (
    create_suggestion,
    get_suggestion_by_id,
    update_suggestion,
)


@pytest.fixture()
def boards_session(tmp_path: Path) -> Iterator[Session]:
    """notices + suggestions 테이블이 생성된 테스트 전용 boards 세션."""
    db_file = tmp_path / "boards_test.sqlite3"
    engine: Engine = create_engine(
        f"sqlite:///{db_file}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Notice
# ---------------------------------------------------------------------------


def test_create_notice_defaults_to_plain(boards_session: Session) -> None:
    """body_format 미지정 시 기본값 'plain' 으로 저장된다(하위 호환)."""
    notice = create_notice(
        boards_session,
        author_user_id=1,
        author_name="관리자",
        title="제목",
        body="평문 본문",
    )
    boards_session.commit()

    loaded = get_notice_by_id(boards_session, notice.id)
    assert loaded is not None
    assert loaded.body_format == BODY_FORMAT_PLAIN
    assert loaded.body == "평문 본문"


def test_create_notice_html_roundtrip(boards_session: Session) -> None:
    """body_format='html' 과 HTML 본문이 손상 없이 저장·복원된다."""
    html = '<p><b>굵게</b></p><table><tr><td>셀</td></tr></table>'
    notice = create_notice(
        boards_session,
        author_user_id=1,
        author_name="관리자",
        title="제목",
        body=html,
        body_format=BODY_FORMAT_HTML,
    )
    boards_session.commit()

    loaded = get_notice_by_id(boards_session, notice.id)
    assert loaded is not None
    assert loaded.body_format == BODY_FORMAT_HTML
    assert loaded.body == html


def test_update_notice_changes_body_format(boards_session: Session) -> None:
    """update_notice 가 body_format 을 'plain' → 'html' 로 갱신한다."""
    notice = create_notice(
        boards_session,
        author_user_id=1,
        author_name="관리자",
        title="제목",
        body="평문",
    )
    boards_session.commit()

    update_notice(
        boards_session,
        notice_id=notice.id,
        title="제목",
        body="<p>리치</p>",
        body_format=BODY_FORMAT_HTML,
    )
    boards_session.commit()

    loaded = get_notice_by_id(boards_session, notice.id)
    assert loaded is not None
    assert loaded.body_format == BODY_FORMAT_HTML
    assert loaded.body == "<p>리치</p>"


# ---------------------------------------------------------------------------
# Suggestion
# ---------------------------------------------------------------------------


def test_create_suggestion_defaults_to_plain(boards_session: Session) -> None:
    """건의사항도 body_format 미지정 시 'plain' 으로 저장된다."""
    suggestion = create_suggestion(
        boards_session,
        author_user_id=1,
        title="제목",
        body="평문 건의",
        password_hash="hash",
        is_secret=False,
        author_name="작성자",
        contact_email=None,
    )
    boards_session.commit()

    loaded = get_suggestion_by_id(boards_session, suggestion.id)
    assert loaded is not None
    assert loaded.body_format == BODY_FORMAT_PLAIN


def test_create_suggestion_html_roundtrip(boards_session: Session) -> None:
    """건의사항 body_format='html' HTML 본문 저장·복원."""
    html = '<p style="color: red;">색상</p>'
    suggestion = create_suggestion(
        boards_session,
        author_user_id=1,
        title="제목",
        body=html,
        password_hash="hash",
        is_secret=False,
        author_name="작성자",
        contact_email=None,
        body_format=BODY_FORMAT_HTML,
    )
    boards_session.commit()

    loaded = get_suggestion_by_id(boards_session, suggestion.id)
    assert loaded is not None
    assert loaded.body_format == BODY_FORMAT_HTML
    assert loaded.body == html


def test_update_suggestion_changes_body_format(boards_session: Session) -> None:
    """update_suggestion 이 body_format 을 갱신한다."""
    suggestion = create_suggestion(
        boards_session,
        author_user_id=1,
        title="제목",
        body="평문",
        password_hash="hash",
        is_secret=False,
        author_name="작성자",
        contact_email=None,
    )
    boards_session.commit()

    update_suggestion(
        boards_session,
        suggestion_id=suggestion.id,
        title="제목",
        body="<p>리치</p>",
        is_secret=False,
        body_format=BODY_FORMAT_HTML,
    )
    boards_session.commit()

    loaded = get_suggestion_by_id(boards_session, suggestion.id)
    assert loaded is not None
    assert loaded.body_format == BODY_FORMAT_HTML
