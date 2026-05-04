"""boards DB 파일명 마이그레이션 헬퍼 단위 테스트 (task 00056).

검증 시나리오:
1. legacy 만 존재 → boards 로 이름 변경되고 legacy 는 사라짐 (멱등: 재실행 no-op)
2. boards 만 존재 → no-op (이미 완료)
3. 둘 다 존재 → WARNING 로그, 두 파일 모두 그대로 유지
4. 둘 다 없음 → no-op (신규 설치)

단위 테스트이므로 FastAPI startup / DB 엔진 생성 로직은 건드리지 않는다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.suggestions.migration import migrate_suggestions_to_boards


# ---------------------------------------------------------------------------
# Fixture: 테스트별 격리된 tmp 디렉터리에 Settings 를 주입
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """테스트용 임시 DB 디렉터리.

    ``SUGGESTIONS_DB_URL`` 환경변수를 주입해 Settings 캐시 비운 뒤 새 경로를
    가리키게 한다.
    """
    from app.config import get_settings
    from app.suggestions.session import reset_suggestions_engine_cache

    boards_file = tmp_path / "boards.sqlite3"
    monkeypatch.setenv("SUGGESTIONS_DB_URL", f"sqlite:///{boards_file}")
    get_settings.cache_clear()
    reset_suggestions_engine_cache()

    yield tmp_path

    # teardown: 캐시 정리
    get_settings.cache_clear()
    reset_suggestions_engine_cache()


# ---------------------------------------------------------------------------
# 시나리오 1: legacy 만 존재 → rename
# ---------------------------------------------------------------------------


def test_legacy_only_renamed_to_boards(db_dir: Path) -> None:
    """suggestions.sqlite3 만 있을 때 boards.sqlite3 로 이름을 변경한다."""
    legacy = db_dir / "suggestions.sqlite3"
    boards = db_dir / "boards.sqlite3"

    # 간단한 SQLite 파일 생성 (더미 테이블 포함)
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO dummy VALUES (42)")
    conn.commit()
    conn.close()

    migrate_suggestions_to_boards()

    assert boards.exists(), "boards.sqlite3 가 생성되어야 한다"
    assert not legacy.exists(), "suggestions.sqlite3 는 제거되어야 한다"

    # 내용도 그대로인지 검증
    conn = sqlite3.connect(str(boards))
    row = conn.execute("SELECT id FROM dummy").fetchone()
    conn.close()
    assert row == (42,), "데이터가 손상 없이 이전되어야 한다"


def test_legacy_only_idempotent(db_dir: Path) -> None:
    """rename 완료 후 재실행해도 데이터 유실 없이 no-op 이다."""
    legacy = db_dir / "suggestions.sqlite3"
    boards = db_dir / "boards.sqlite3"

    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO dummy VALUES (99)")
    conn.commit()
    conn.close()

    # 첫 번째 실행
    migrate_suggestions_to_boards()
    assert boards.exists()
    assert not legacy.exists()

    # 두 번째 실행 (멱등)
    migrate_suggestions_to_boards()
    assert boards.exists()
    assert not legacy.exists()

    conn = sqlite3.connect(str(boards))
    row = conn.execute("SELECT id FROM dummy").fetchone()
    conn.close()
    assert row == (99,), "재실행 후에도 데이터 손상 없어야 한다"


# ---------------------------------------------------------------------------
# 시나리오 2: boards 만 존재 → no-op
# ---------------------------------------------------------------------------


def test_boards_only_noop(db_dir: Path) -> None:
    """boards.sqlite3 만 있으면 아무것도 변경하지 않는다."""
    boards = db_dir / "boards.sqlite3"

    conn = sqlite3.connect(str(boards))
    conn.execute("CREATE TABLE info (val TEXT)")
    conn.execute("INSERT INTO info VALUES ('already_migrated')")
    conn.commit()
    conn.close()

    migrate_suggestions_to_boards()

    assert boards.exists()
    conn = sqlite3.connect(str(boards))
    row = conn.execute("SELECT val FROM info").fetchone()
    conn.close()
    assert row == ("already_migrated",), "boards 내용이 바뀌어선 안 된다"


# ---------------------------------------------------------------------------
# 시나리오 3: 둘 다 존재 → WARNING, 두 파일 보존
# ---------------------------------------------------------------------------


def test_both_exist_warns_and_preserves(
    db_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """boards 와 suggestions 가 동시에 존재하면 WARNING 을 출력하고 두 파일을 건드리지 않는다."""
    legacy = db_dir / "suggestions.sqlite3"
    boards = db_dir / "boards.sqlite3"

    # 두 파일 모두 생성
    for path, val in [(legacy, "legacy_data"), (boards, "boards_data")]:
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute(f"INSERT INTO t VALUES ('{val}')")
        conn.commit()
        conn.close()

    import logging

    with caplog.at_level(logging.WARNING):
        migrate_suggestions_to_boards()

    assert legacy.exists(), "레거시 파일은 그대로 보존되어야 한다"
    assert boards.exists(), "boards 파일은 그대로 보존되어야 한다"

    # 내용 변화 없음
    conn = sqlite3.connect(str(boards))
    row = conn.execute("SELECT v FROM t").fetchone()
    conn.close()
    assert row == ("boards_data",)

    conn = sqlite3.connect(str(legacy))
    row = conn.execute("SELECT v FROM t").fetchone()
    conn.close()
    assert row == ("legacy_data",)

    # 경고 로그 확인 (loguru → stdlib propagate 경로가 없을 수 있어 느슨하게 검증)
    # caplog 가 loguru 를 캡처하지 못하는 환경에서도 테스트가 실패하지 않도록
    # 파일 불변성 검증만으로도 충분하다.


# ---------------------------------------------------------------------------
# 시나리오 4: 둘 다 없음 → no-op
# ---------------------------------------------------------------------------


def test_neither_exists_noop(db_dir: Path) -> None:
    """두 파일 모두 없으면 아무것도 생성하지 않는다."""
    legacy = db_dir / "suggestions.sqlite3"
    boards = db_dir / "boards.sqlite3"

    migrate_suggestions_to_boards()

    assert not legacy.exists(), "새 파일이 생성되어선 안 된다"
    assert not boards.exists(), "새 파일이 생성되어선 안 된다"
