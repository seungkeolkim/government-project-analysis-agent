"""게시판 DB 파일명·스키마 마이그레이션 헬퍼 (task 00056, 00068, 00069, 00072).

기존 운영 환경에 존재하는 ``suggestions.sqlite3`` 파일을 ``boards.sqlite3`` 로
원자적으로 이름을 바꾸고, 이후 컬럼 추가·제약 변경을 멱등하게 적용한다.

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
import re
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


def ensure_deleted_at_columns() -> None:
    """기존 boards.sqlite3 의 세 테이블에 deleted_at 컬럼이 없으면 추가한다.

    대상 테이블: suggestions, suggestion_comments, notices

    신규 환경(boards.sqlite3 없음)에서는 init_suggestions_db() 의 create_all 이
    deleted_at 포함 테이블을 한 번에 만들어주므로, 본 함수는 "기존 boards.sqlite3 가
    있는데 deleted_at 컬럼이 빠진" 운영 환경의 무중단 마이그레이션 전용이다.

    ## 처리 순서 (테이블별)
    1. PRAGMA table_info 로 deleted_at 컬럼 존재 여부 확인.
    2. 테이블 자체가 없으면 SKIP — init_suggestions_db() 의 create_all 이 처리한다.
    3. 없으면 ADD COLUMN (nullable TIMESTAMP) 수행.
    4. 이미 있으면 NOOP — 멱등성 보장.
    5. 기존 row 는 deleted_at = NULL 로 유지 (삭제 안됨 상태, backfill 불필요).

    ## SQLite ALTER TABLE 특성
    deleted_at 은 nullable 이므로 NOT NULL + DEFAULT 제약 없이 ADD COLUMN 이 가능하다.
    """
    settings = get_settings()
    boards_path = _resolve_sqlite_path(settings.suggestions_db_url)

    if not boards_path.exists():
        # 신규 환경 — init_suggestions_db() 가 create_all 로 처리하므로 스킵
        logger.info(
            "ensure deleted_at columns: boards DB 없음 — 신규 환경으로 간주, 건너뜀 ({})",
            boards_path,
        )
        return

    # 소프트 삭제 컬럼을 추가할 대상 테이블 목록
    target_tables = ["suggestions", "suggestion_comments", "notices"]

    conn = sqlite3.connect(str(boards_path))
    try:
        for table_name in target_tables:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()

            if not rows:
                # 테이블이 아직 없으면 SKIP — init_suggestions_db() 가 create_all 로 처리한다
                logger.info(
                    "ensure deleted_at column: 테이블 없음, 건너뜀 ({}, {})",
                    boards_path,
                    table_name,
                )
                continue

            existing_columns = {row[1] for row in rows}  # row[1] = 컬럼명

            if "deleted_at" in existing_columns:
                logger.info(
                    "ensure deleted_at column: 이미 존재함 — NOOP ({}, {})",
                    boards_path,
                    table_name,
                )
                continue

            logger.info(
                "ensure deleted_at column: {} 에 deleted_at 컬럼 추가 시작 ({})",
                table_name,
                boards_path,
            )
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN deleted_at TIMESTAMP"
            )
            conn.commit()
            logger.info(
                "ensure deleted_at column: 완료 — {}.deleted_at 추가 ({})",
                table_name,
                boards_path,
            )
    finally:
        conn.close()


def ensure_updated_at_initial_null_backfill() -> None:
    """notices·suggestions·suggestion_comments 의 updated_at 을 INSERT 시 NULL 정책으로 정착시킨다.

    ## 목적
    최초 작성(INSERT) 시 updated_at 이 NULL 로 저장되고, 수정(UPDATE) 시에만
    ORM onupdate 콜러블이 현재 시각을 채우는 정책을 DB 레벨에서 보장한다.

    ## 각 테이블 처리 방식
    - **notices / suggestions**: updated_at 이 NOT NULL 제약이면 SQLite recreate
      패턴으로 nullable 로 변경하고, 기존 row 중 updated_at = created_at 인 것
      (수정 이력 없는 것으로 간주)을 NULL 로 backfill 한다.
      이미 nullable 이면 backfill UPDATE 만 수행한다.
    - **suggestion_comments**: 이미 nullable (00068 마이그레이션에서 ADD COLUMN).
      backfill UPDATE(updated_at = created_at → NULL) 만 수행한다.

    ## 멱등성
    - notices/suggestions recreate: 이미 nullable 이면 recreate 건너뜀.
    - backfill UPDATE: WHERE updated_at IS NOT NULL AND updated_at = created_at.
      이미 NULL 이면 WHERE 절 미매칭 → no-op.
    - 이 함수를 여러 번 실행해도 데이터가 변하지 않는다.

    ## FK 안전성
    suggestion_comments → suggestions FK (ON DELETE CASCADE) 가 있으므로,
    suggestions 테이블 recreate 전에 PRAGMA foreign_keys=OFF 로 비활성화한다.
    recreation 완료 후 PRAGMA foreign_keys_check 로 무결성을 확인하고 ON 으로 복구.
    """
    settings = get_settings()
    boards_path = _resolve_sqlite_path(settings.suggestions_db_url)

    if not boards_path.exists():
        logger.info(
            "ensure updated_at null backfill: boards DB 없음 — 신규 환경, 건너뜀 ({})",
            boards_path,
        )
        return

    # isolation_level=None — 명시적 BEGIN/COMMIT/ROLLBACK 으로 단일 트랜잭션 관리.
    conn = sqlite3.connect(str(boards_path), isolation_level=None)
    try:
        # FK 비활성화는 트랜잭션 외부에서 설정해야 확실히 적용된다.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            for table_name in ("notices", "suggestions"):
                _ensure_table_updated_at_nullable(conn, table_name, boards_path)

            # suggestion_comments 는 이미 nullable — backfill UPDATE 만 수행
            _backfill_updated_at_to_null(conn, "suggestion_comments", boards_path)

            violations = conn.execute("PRAGMA foreign_keys_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"FK 무결성 검사 실패 — recreate 롤백: {violations}"
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()


def _ensure_table_updated_at_nullable(
    conn: sqlite3.Connection,
    table_name: str,
    boards_path: Path,
) -> None:
    """단일 테이블(notices 또는 suggestions) 의 updated_at 을 nullable 로 보장한다.

    NOT NULL 이면 create-copy-drop-rename 패턴으로 제약을 제거하고,
    기존 row 중 updated_at = created_at 인 것을 NULL 로 backfill 한다.
    이미 nullable 이면 backfill UPDATE 만 수행한다.
    """
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not rows:
        logger.info(
            "ensure updated_at nullable: 테이블 없음, 건너뜀 ({}, {})",
            table_name,
            boards_path,
        )
        return

    updated_at_info = next((r for r in rows if r[1] == "updated_at"), None)
    if updated_at_info is None:
        logger.warning(
            "ensure updated_at nullable: updated_at 컬럼 없음 — 건너뜀 ({}, {})",
            table_name,
            boards_path,
        )
        return

    is_not_null = updated_at_info[3] == 1  # PRAGMA table_info row[3] = notnull 플래그

    if not is_not_null:
        # 이미 nullable — backfill UPDATE 만 수행 (멱등)
        logger.info(
            "ensure updated_at nullable: 이미 nullable — backfill UPDATE 수행 ({}, {})",
            table_name,
            boards_path,
        )
        _backfill_updated_at_to_null(conn, table_name, boards_path)
        return

    # NOT NULL → recreate 로 nullable 로 변경
    logger.info(
        "ensure updated_at nullable: NOT NULL 제약 제거 위해 recreate 시작 ({}, {})",
        table_name,
        boards_path,
    )

    temp_name = f"_{table_name}_updated_at_null_migration"

    # 기존 CREATE TABLE SQL 을 sqlite_master 에서 읽어 updated_at NOT NULL → nullable 로 수정
    create_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    existing_sql = create_sql_row[0]

    new_sql = re.sub(
        r"(updated_at\s+DATETIME)\s+NOT\s+NULL",
        r"\1",
        existing_sql,
        flags=re.IGNORECASE,
    )
    if new_sql == existing_sql:
        # 패턴 미매칭 — 이미 nullable 이거나 다른 형태일 수 있으므로 backfill 만 수행
        logger.warning(
            "ensure updated_at nullable: NOT NULL 패턴 미매칭 — backfill 만 수행 ({}, {})",
            table_name,
            boards_path,
        )
        _backfill_updated_at_to_null(conn, table_name, boards_path)
        return

    # 임시 테이블 이름으로 CREATE TABLE
    new_sql = re.sub(
        rf"(?i)\b{re.escape(table_name)}\b",
        temp_name,
        new_sql,
        count=1,
    )
    conn.execute(new_sql)

    # 컬럼 목록 기반 INSERT SELECT — updated_at = created_at 인 row 는 NULL 로 backfill
    column_names = [r[1] for r in rows]
    select_parts = [
        "CASE WHEN updated_at = created_at THEN NULL ELSE updated_at END"
        if col == "updated_at"
        else col
        for col in column_names
    ]
    cols_joined = ", ".join(column_names)
    select_joined = ", ".join(select_parts)
    conn.execute(
        f"INSERT INTO {temp_name} ({cols_joined}) SELECT {select_joined} FROM {table_name}"
    )

    # 기존 인덱스 SQL 을 미리 수집해 둔다 (DROP 이후에는 조회 불가)
    index_rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table_name,),
    ).fetchall()

    # 기존 테이블 삭제 (인덱스도 함께 제거됨)
    conn.execute(f"DROP TABLE {table_name}")

    # 임시 테이블을 원본 이름으로 교체
    conn.execute(f"ALTER TABLE {temp_name} RENAME TO {table_name}")

    # 기존 인덱스를 원본 이름으로 재생성 — RENAME 후 테이블명이 복원됐으므로
    # 원래 인덱스 SQL 을 그대로 사용 가능
    for index_row in index_rows:
        conn.execute(index_row[0])

    logger.info(
        "ensure updated_at nullable: recreate 완료 ({}, {})",
        table_name,
        boards_path,
    )


def _backfill_updated_at_to_null(
    conn: sqlite3.Connection,
    table_name: str,
    boards_path: Path,
) -> None:
    """updated_at = created_at 인 row 를 NULL 로 backfill 한다.

    수정 이력이 없는 것으로 간주되는 row (최초 작성 시 created_at 과 동일 값) 를
    NULL 로 변경한다. 멱등성: 이미 NULL 이면 WHERE 절 미매칭 → no-op.
    """
    result = conn.execute(
        f"UPDATE {table_name} SET updated_at = NULL"
        f" WHERE updated_at IS NOT NULL AND updated_at = created_at"
    )
    affected = result.rowcount
    if affected > 0:
        logger.info(
            "ensure updated_at null backfill: {} 행 NULL 로 backfill ({}, {})",
            affected,
            table_name,
            boards_path,
        )
    else:
        logger.info(
            "ensure updated_at null backfill: backfill no-op ({}, {})",
            table_name,
            boards_path,
        )


__all__ = [
    "migrate_suggestions_to_boards",
    "ensure_suggestion_comment_updated_at_column",
    "ensure_deleted_at_columns",
    "ensure_updated_at_initial_null_backfill",
]
