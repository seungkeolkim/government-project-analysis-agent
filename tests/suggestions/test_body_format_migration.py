"""body_format 컬럼 멱등 마이그레이션 단위 테스트 (task 00153-1).

검증 시나리오:
    (a) 신규 환경(boards.sqlite3 없음) → ensure 는 skip, init_suggestions_db 의
        create_all 이 body_format 포함 테이블을 만든다.
    (b) 기존 DB(body_format 없음) → ensure 호출 시 notices·suggestions 에 컬럼
        추가 + 기존 row 'plain' backfill.
    (c) 멱등성 — 재호출해도 데이터 변화 없음.
    (d) suggestion_comments 는 대상에서 제외(댓글은 평문 유지).

단위 테스트이므로 FastAPI startup / 서버 기동은 건드리지 않는다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app.notices.models  # noqa: F401 — Notice 를 Base.metadata 에 등록
from app.suggestions.migration import ensure_body_format_columns


@pytest.fixture()
def db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """테스트용 임시 DB 디렉터리 + Settings 캐시 격리."""
    from app.config import get_settings
    from app.suggestions.session import reset_suggestions_engine_cache

    boards_file = tmp_path / "boards.sqlite3"
    monkeypatch.setenv("SUGGESTIONS_DB_URL", f"sqlite:///{boards_file}")
    get_settings.cache_clear()
    reset_suggestions_engine_cache()

    yield tmp_path

    get_settings.cache_clear()
    reset_suggestions_engine_cache()


@pytest.fixture()
def existing_db_without_body_format(db_dir: Path) -> Path:
    """body_format 컬럼 없이 세 테이블이 존재하고 row 가 있는 boards.sqlite3 생성."""
    boards = db_dir / "boards.sqlite3"
    conn = sqlite3.connect(str(boards))
    conn.execute(
        "CREATE TABLE notices (id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE suggestions (id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE suggestion_comments (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO notices (id, title, body) VALUES (1, '공지', '평문 공지')")
    conn.execute("INSERT INTO suggestions (id, title, body) VALUES (1, '건의', '평문 건의')")
    conn.execute("INSERT INTO suggestion_comments (id, body) VALUES (1, '댓글')")
    conn.commit()
    conn.close()
    return db_dir


# ---------------------------------------------------------------------------
# (a) 신규 환경 — create_all 이 body_format 포함 생성
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", ["notices", "suggestions"])
def test_new_db_has_body_format_column(db_dir: Path, table_name: str) -> None:
    """신규 환경에서 init_suggestions_db() 호출 시 body_format 컬럼이 포함된다."""
    from app.suggestions.session import init_suggestions_db

    boards = db_dir / "boards.sqlite3"
    assert not boards.exists()

    init_suggestions_db()

    conn = sqlite3.connect(str(boards))
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    conn.close()
    assert "body_format" in cols


def test_ensure_skips_when_db_missing(db_dir: Path) -> None:
    """boards.sqlite3 가 없으면 ensure 는 아무것도 만들지 않고 skip 한다."""
    boards = db_dir / "boards.sqlite3"
    assert not boards.exists()

    ensure_body_format_columns()

    assert not boards.exists(), "신규 환경에서는 파일을 생성하지 않아야 한다"


# ---------------------------------------------------------------------------
# (b) 기존 DB — 컬럼 추가 + backfill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", ["notices", "suggestions"])
def test_adds_column_and_backfills_plain(
    existing_db_without_body_format: Path, table_name: str
) -> None:
    """기존 row 가 있는 테이블에 body_format 추가 후 'plain' 으로 backfill 된다."""
    db_dir = existing_db_without_body_format
    boards = db_dir / "boards.sqlite3"

    conn = sqlite3.connect(str(boards))
    before = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    conn.close()
    assert "body_format" not in before

    ensure_body_format_columns()

    conn = sqlite3.connect(str(boards))
    after = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    value = conn.execute(f"SELECT body_format FROM {table_name} WHERE id = 1").fetchone()
    conn.close()
    assert "body_format" in after
    assert value == ("plain",), "기존 row 는 'plain' 으로 backfill 되어야 한다"


# ---------------------------------------------------------------------------
# (c) 멱등성
# ---------------------------------------------------------------------------


def test_idempotent_second_call_noop(existing_db_without_body_format: Path) -> None:
    """두 번 호출해도 오류 없이 데이터가 변하지 않는다."""
    db_dir = existing_db_without_body_format
    boards = db_dir / "boards.sqlite3"

    ensure_body_format_columns()
    # 첫 호출 후 임의로 'html' 로 바꿔두고, 재호출이 이 값을 덮어쓰지 않는지 확인.
    conn = sqlite3.connect(str(boards))
    conn.execute("UPDATE notices SET body_format = 'html' WHERE id = 1")
    conn.commit()
    conn.close()

    ensure_body_format_columns()  # 멱등 재호출

    conn = sqlite3.connect(str(boards))
    value = conn.execute("SELECT body_format FROM notices WHERE id = 1").fetchone()
    conn.close()
    assert value == ("html",), "재호출이 기존 값을 덮어써선 안 된다(backfill 은 IS NULL 한정)"


# ---------------------------------------------------------------------------
# (d) 댓글 테이블 제외
# ---------------------------------------------------------------------------


def test_comments_table_not_modified(existing_db_without_body_format: Path) -> None:
    """suggestion_comments 에는 body_format 컬럼이 추가되지 않는다(댓글 평문 유지)."""
    db_dir = existing_db_without_body_format
    boards = db_dir / "boards.sqlite3"

    ensure_body_format_columns()

    conn = sqlite3.connect(str(boards))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(suggestion_comments)").fetchall()}
    conn.close()
    assert "body_format" not in cols
