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


__all__ = ["migrate_suggestions_to_boards"]
