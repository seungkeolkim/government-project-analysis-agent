"""게시판 DB 파일명 마이그레이션 헬퍼 (task 00056).

기존 운영 환경에 존재하는 ``suggestions.sqlite3`` 파일을 ``boards.sqlite3`` 로
원자적으로 이름을 바꾼다.

## 설계 원칙
- **멱등**: 같은 환경에서 여러 번 실행해도 데이터 유실 없고 동작 변화 없음.
- **원자성**: ``os.rename`` 은 동일 파일시스템 내에서 atomic 보장.
  cross-device(EXDEV) 발생 시 ``shutil.move`` 로 fallback 하고, 이동 후
  sqlite 파일이 정상 열리는지 즉시 검증한다.
- **lru_cache 순서**: 본 함수는 ``init_suggestions_db()`` 호출 직전에 실행되어야
  엔진이 신규 경로(boards.sqlite3)로 처음 생성된다. startup hook 의 호출 순서
  유지 필수.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from loguru import logger

from app.config import PROJECT_ROOT, get_settings


def _resolve_sqlite_path(url: str) -> Path:
    """SQLite URL 에서 파일 시스템 절대 경로를 추출한다."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError(f"SQLite URL 이 아닙니다: {url!r}")
    raw = url[len(prefix):]
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def migrate_suggestions_to_boards() -> None:
    """suggestions.sqlite3 → boards.sqlite3 파일 이름 변경.

    ``Settings.suggestions_db_url`` 이 가리키는 경로(boards.sqlite3)와 같은
    디렉터리에 있는 레거시 ``suggestions.sqlite3`` 를 원자적으로 이름을 바꾼다.

    ## 경우의 수
    - **둘 다 없음**: 신규 설치. 건너뜀.
    - **boards 만 존재**: 이미 마이그레이션 완료. 건너뜀.
    - **suggestions 만 존재**: ``os.rename`` (실패 시 ``shutil.move`` fallback) 수행.
    - **둘 다 존재**: 자동 병합 대신 운영자 수동 판단 강제. WARNING 로그 후 boards
      파일로 기동을 계속한다. 데이터 유실 방지를 위해 어떤 파일도 건드리지 않는다.
    """
    settings = get_settings()
    boards_path = _resolve_sqlite_path(settings.suggestions_db_url)

    # 레거시 파일은 항상 boards 와 동일 디렉터리의 고정 이름
    legacy_path = boards_path.parent / "suggestions.sqlite3"

    boards_exists = boards_path.exists()
    legacy_exists = legacy_path.exists()

    if not legacy_exists and not boards_exists:
        logger.info(
            "boards DB 마이그레이션: 두 파일 모두 없음 — 신규 설치로 간주, 건너뜀 "
            "(boards={boards}, legacy={legacy})",
            boards=boards_path,
            legacy=legacy_path,
        )
        return

    if boards_exists and legacy_exists:
        # 자동 병합 시도는 데이터 유실 가능성이 크므로 운영자에게 수동 결정 위임
        logger.warning(
            "boards DB 마이그레이션: 두 파일이 동시에 존재합니다. "
            "신규({boards})를 보존하려면 레거시({legacy}) 파일을 백업 후 삭제하고 "
            "재시작하세요. 레거시를 채택하려면 신규 파일을 삭제 후 재시작하세요. "
            "이번 기동은 신규(boards) 파일로 계속합니다.",
            boards=boards_path,
            legacy=legacy_path,
        )
        return

    if boards_exists and not legacy_exists:
        logger.info(
            "boards DB 마이그레이션: 이미 완료됨 ({boards})",
            boards=boards_path,
        )
        return

    # legacy 만 존재하는 경우 → 이름 변경 수행
    boards_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "boards DB 마이그레이션 시작: {legacy} → {boards}",
        legacy=legacy_path,
        boards=boards_path,
    )
    _rename_with_fallback(legacy_path, boards_path)


def _rename_with_fallback(src: Path, dst: Path) -> None:
    """os.rename 을 시도하고 EXDEV(cross-device) 발생 시 shutil.move 로 fallback."""
    try:
        os.rename(src, dst)
        logger.info(
            "boards DB 마이그레이션 완료 (atomic rename): {dst}",
            dst=dst,
        )
    except OSError as exc:
        # errno 18 = EXDEV (Cross-device link not permitted)
        if exc.errno != 18:
            raise
        logger.warning(
            "boards DB 마이그레이션: os.rename EXDEV 발생, shutil.move 로 fallback",
        )
        shutil.move(str(src), str(dst))
        _verify_sqlite_file(dst)


def _verify_sqlite_file(path: Path) -> None:
    """이동 후 sqlite 파일이 정상 열리는지 확인한다.

    검증 실패 시 신규 파일을 삭제하고 RuntimeError 를 발생시켜 기동을 중단한다.
    """
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("SELECT 1")
        conn.close()
        logger.info(
            "boards DB 마이그레이션(fallback) 완료 및 검증 성공: {path}",
            path=path,
        )
    except Exception as verify_exc:
        logger.warning(
            "boards DB 마이그레이션(fallback) 후 검증 실패, 신규 파일 삭제: {exc}",
            exc=verify_exc,
        )
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"boards DB 마이그레이션(fallback) 실패 — 검증 오류: {verify_exc}"
        ) from verify_exc


def ensure_suggestion_comment_updated_at_column() -> None:
    """기존 boards.sqlite3 의 suggestion_comments 테이블에 updated_at 컬럼이 없으면 추가한다.

    신규 환경(boards.sqlite3 없음)에서는 init_suggestions_db() 의 create_all 이
    updated_at 포함 테이블을 한 번에 만들어주므로, 본 함수는 "기존 boards.sqlite3 가
    있는데 updated_at 컬럼만 빠진" 운영 환경의 무중단 마이그레이션 전용이다.

    ## 처리 순서
    1. PRAGMA table_info 로 updated_at 컬럼 존재 여부 확인.
    2. 없으면 ADD COLUMN (nullable) → UPDATE backfill(created_at 값으로) 수행.
    3. 이미 있으면 NOOP — 멱등성 보장.

    ## SQLite ALTER TABLE 제약 우회
    SQLite 는 NOT NULL + DEFAULT 를 동시에 가진 컬럼을 ALTER TABLE 로 추가할 수 없다.
    따라서 DB 레벨은 nullable 로 두고, ORM 레벨(mapped_column nullable=False) 에서만
    NOT NULL 을 강제한다. 신규 row 는 ORM default=_utcnow 가 채운다.
    """
    settings = get_settings()
    boards_path = _resolve_sqlite_path(settings.suggestions_db_url)

    if not boards_path.exists():
        # 신규 환경 — init_suggestions_db() 가 create_all 로 처리하므로 스킵
        logger.info(
            "ensure updated_at column: boards DB 없음 — 신규 환경으로 간주, 건너뜀 ({})",
            boards_path,
        )
        return

    conn = sqlite3.connect(str(boards_path))
    try:
        # PRAGMA table_info 로 기존 컬럼 목록 조회
        rows = conn.execute("PRAGMA table_info(suggestion_comments)").fetchall()
        existing_columns = {row[1] for row in rows}  # row[1] = 컬럼명

        if "updated_at" in existing_columns:
            logger.info(
                "ensure updated_at column: 이미 존재함 — NOOP ({}, suggestion_comments)",
                boards_path,
            )
            return

        logger.info(
            "ensure updated_at column: suggestion_comments 에 updated_at 컬럼 추가 시작 ({})",
            boards_path,
        )
        # ADD COLUMN 은 nullable 로만 추가 가능 (SQLite 제약)
        conn.execute(
            "ALTER TABLE suggestion_comments ADD COLUMN updated_at TIMESTAMP"
        )
        # 기존 row 의 updated_at 을 created_at 값으로 backfill (idempotent)
        conn.execute(
            "UPDATE suggestion_comments SET updated_at = created_at WHERE updated_at IS NULL"
        )
        conn.commit()
        logger.info(
            "ensure updated_at column: 완료 — suggestion_comments.updated_at 추가 및 backfill ({}, suggestion_comments)",
            boards_path,
        )
    finally:
        conn.close()


__all__ = ["migrate_suggestions_to_boards", "ensure_suggestion_comment_updated_at_column"]
