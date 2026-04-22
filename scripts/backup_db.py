"""SQLite DB 백업 + 로테이션 스크립트.

사용자 원문 요구:
    - scripts/backup_db.py — SQLite 복사 + 타임스탬프
    - data/backups/ 디렉터리에 저장
    - 최근 14개만 보관 (오래된 것부터 삭제)
    - pg_dump 는 skip (Postgres 는 별도 백업 도구 사용)

동작 순서:
    (1) app.config.get_settings().db_url 이 'sqlite:///' 로 시작하는지 확인.
        그 외 방언(postgres 등)은 INFO 로그 후 종료 코드 0 으로 no-op 종료.
    (2) URL 에서 실제 DB 파일 경로를 추출하고 존재 여부를 검증한다.
    (3) 목적지 디렉터리(기본: data/backups/) 존재 보장 + 파일명 생성
        (app.sqlite3.YYYYMMDDThhmmssZ.bak, UTC 타임스탬프).
    (4) sqlite3.connect(src).backup(dst) API 로 안전 온라인 백업 수행 —
        SQLite WAL 모드에서도 일관된 스냅샷을 만든다 (shutil.copy2 보다 안전).
    (5) 같은 디렉터리 내 `app.sqlite3.*.bak` 파일을 mtime 내림차순 정렬,
        --keep (기본 14) 개수를 초과하는 오래된 파일 삭제.
    (6) loguru 로 성공/삭제 수/유지 목록 로깅.

실행 예:
    docker compose --profile scrape run --rm scraper python scripts/backup_db.py
    python scripts/backup_db.py --keep 30 --dest /mnt/backups

옵션:
    --keep N     최근 몇 개의 백업을 보관할지 (기본값: 14).
    --dest PATH  백업 파일 저장 디렉터리 (기본값: <project_root>/data/backups).

종료 코드:
    0  : 정상 백업 완료 또는 SQLite 가 아니라 no-op skip.
    1  : DB 파일이 실제로 존재하지 않거나 백업/삭제 중 복구 불가 오류 발생.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가 (scripts/ 에서 app 패키지 임포트)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger  # noqa: E402

from app.config import PROJECT_ROOT, get_settings  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402

# SQLite 접속 문자열 prefix — app/config.py 의 ensure_runtime_paths 와 동일.
_SQLITE_URL_PREFIX: str = "sqlite:///"

# 기본 보관 개수 (사용자 원문: "최근 14개 보관").
_DEFAULT_KEEP_COUNT: int = 14

# 기본 목적지 디렉터리 (사용자 원문: "data/backups/").
_DEFAULT_DEST_DIR: Path = PROJECT_ROOT / "data" / "backups"

# 백업 파일명 타임스탬프 포맷 — UTC ISO-like, 파일시스템 안전.
_TIMESTAMP_FORMAT: str = "%Y%m%dT%H%M%SZ"


def _extract_sqlite_file_path(db_url: str) -> Path | None:
    """SQLAlchemy db_url 에서 SQLite 파일 경로를 추출한다.

    'sqlite:///' 로 시작하지 않으면 None 을 반환한다(지원 대상 외).
    상대경로면 PROJECT_ROOT 기준으로 절대경로화한다.

    Args:
        db_url: 예) 'sqlite:////abs/path.sqlite3' 또는 'sqlite:///./data/db/app.sqlite3'.

    Returns:
        절대경로 Path. SQLite 가 아니면 None.
    """
    if not db_url.startswith(_SQLITE_URL_PREFIX):
        return None

    raw_path = db_url[len(_SQLITE_URL_PREFIX):]
    # 빈 경로(in-memory 'sqlite:///') 는 백업 대상 아님.
    if not raw_path or raw_path == ":memory:":
        return None

    db_file = Path(raw_path).expanduser()
    if not db_file.is_absolute():
        db_file = (PROJECT_ROOT / db_file).resolve()
    return db_file


def _build_backup_filename(source_file: Path, timestamp: datetime) -> str:
    """'app.sqlite3.YYYYMMDDThhmmssZ.bak' 형태의 백업 파일명을 생성한다.

    원본 파일명을 prefix 로 사용해 서로 다른 SQLite 파일을 같은 디렉터리에
    쌓아도 충돌하지 않게 한다.

    Args:
        source_file: 원본 DB 파일 경로.
        timestamp: UTC timezone-aware datetime.

    Returns:
        백업 파일명 문자열 (디렉터리 포함 아님).
    """
    stamp = timestamp.strftime(_TIMESTAMP_FORMAT)
    return f"{source_file.name}.{stamp}.bak"


def _perform_sqlite_online_backup(source_file: Path, dest_file: Path) -> None:
    """sqlite3 내장 backup() API 로 원본 DB 파일을 dest_file 로 복제한다.

    WAL 모드에서도 일관된 스냅샷을 만들 수 있도록 shutil.copy2 대신
    사용한다. 백업 도중 원본이 쓰여도 sqlite3 가 원자적으로 처리한다.

    Args:
        source_file: 원본 DB 파일 경로 (존재해야 함).
        dest_file:   새로 만들 백업 파일 경로. 부모 디렉터리는 미리 존재해야 함.

    Raises:
        sqlite3.Error: 백업 중 SQLite 레벨 오류 발생 시.
    """
    # check_same_thread=False 는 필요 없지만 (단일 스레드 스크립트),
    # 기본 connect 는 URI 해석을 위해 문자열 경로만 허용한다.
    source_connection = sqlite3.connect(str(source_file))
    try:
        dest_connection = sqlite3.connect(str(dest_file))
        try:
            # backup() 은 전체 페이지를 복사 — WAL 포함 일관 스냅샷.
            source_connection.backup(dest_connection)
        finally:
            dest_connection.close()
    finally:
        source_connection.close()


def _list_existing_backups(dest_dir: Path, source_filename: str) -> list[Path]:
    """목적지 디렉터리에서 같은 원본 파일명을 가진 백업 파일 목록을 반환한다.

    패턴: '<source_filename>.*.bak'. mtime 내림차순 정렬 (최신 먼저).
    다른 DB 의 백업 파일과 섞이지 않도록 prefix 로 필터링한다.

    Args:
        dest_dir:        백업 디렉터리.
        source_filename: 원본 DB 파일명(확장자 포함). 예: 'app.sqlite3'.

    Returns:
        mtime 내림차순으로 정렬된 Path 리스트.
    """
    pattern = f"{source_filename}.*.bak"
    candidates = list(dest_dir.glob(pattern))
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates


def _prune_old_backups(
    dest_dir: Path,
    source_filename: str,
    keep_count: int,
) -> tuple[int, list[Path]]:
    """keep_count 를 초과하는 오래된 백업을 삭제한다.

    Args:
        dest_dir:        백업 디렉터리.
        source_filename: 원본 DB 파일명 (필터용 prefix).
        keep_count:      유지할 최신 파일 개수.

    Returns:
        (삭제된 파일 수, 유지된 파일 Path 리스트).
    """
    all_backups = _list_existing_backups(dest_dir, source_filename)
    kept = all_backups[:keep_count]
    to_delete = all_backups[keep_count:]

    deleted_count = 0
    for path in to_delete:
        try:
            path.unlink()
            deleted_count += 1
            logger.info("오래된 백업 삭제: {}", path)
        except OSError as exc:
            # 삭제 실패는 치명적이지 않음 — 경고만 남기고 계속
            logger.warning("백업 삭제 실패(건너뜀): {} ({})", path, exc)

    return deleted_count, kept


def run_backup(*, dest_dir: Path, keep_count: int) -> int:
    """전체 백업 흐름을 실행하고 종료 코드를 반환한다.

    Args:
        dest_dir:   백업 파일을 저장할 디렉터리.
        keep_count: 유지할 최신 백업 개수. 1 이상이어야 한다.

    Returns:
        0 이면 정상(또는 SQLite 미사용으로 skip), 1 이면 오류.
    """
    settings = get_settings()
    db_url = settings.db_url

    # (1) SQLite 여부 판정
    source_file = _extract_sqlite_file_path(db_url)
    if source_file is None:
        logger.info(
            "SQLite 가 아닌 DB URL 감지 — 백업 skip (pg_dump 등은 이 스크립트 범위 밖): url={}",
            # URL 을 그대로 로그에 남기면 비밀번호가 유출될 수 있으므로 scheme 만 노출
            db_url.split("://", 1)[0] if "://" in db_url else db_url,
        )
        return 0

    # (2) 원본 파일 존재 확인
    if not source_file.exists():
        logger.error(
            "원본 DB 파일을 찾을 수 없습니다: {} (db_url={})", source_file, db_url
        )
        return 1

    # (3) 목적지 디렉터리 보장 + 파일명 생성
    dest_dir = dest_dir.expanduser()
    if not dest_dir.is_absolute():
        dest_dir = (PROJECT_ROOT / dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=UTC)
    backup_filename = _build_backup_filename(source_file, timestamp)
    dest_file = dest_dir / backup_filename

    # 같은 이름의 파일이 이미 있으면 덮어쓰지 않고 에러 처리.
    # (동일 초에 두 번 실행된 희귀 케이스 — 초 단위 타임스탬프 충돌)
    if dest_file.exists():
        logger.error(
            "같은 이름의 백업 파일이 이미 존재합니다 (초 단위 충돌): {}", dest_file
        )
        return 1

    # (4) SQLite 온라인 백업
    source_size_bytes = source_file.stat().st_size
    logger.info(
        "백업 시작: src={} ({:.2f} MiB) → dst={}",
        source_file,
        source_size_bytes / (1024 * 1024),
        dest_file,
    )

    try:
        _perform_sqlite_online_backup(source_file, dest_file)
    except sqlite3.Error as exc:
        logger.error("SQLite 백업 실패: {} ({}: {})", dest_file, type(exc).__name__, exc)
        # 부분 생성된 파일이 남아 있을 수 있으므로 정리 시도
        try:
            if dest_file.exists():
                dest_file.unlink()
                logger.info("부분 생성된 백업 파일 정리: {}", dest_file)
        except OSError:
            pass
        return 1
    except OSError as exc:
        logger.error("백업 파일 쓰기 실패: {} ({}: {})", dest_file, type(exc).__name__, exc)
        return 1

    backup_size_bytes = dest_file.stat().st_size
    logger.info(
        "백업 완료: {} ({:.2f} MiB)",
        dest_file,
        backup_size_bytes / (1024 * 1024),
    )

    # (5) 오래된 백업 삭제 — keep_count 초과분
    if keep_count < 1:
        logger.warning("keep 값이 1 미만이라 rotation 을 건너뜁니다: keep={}", keep_count)
        return 0

    deleted_count, kept_paths = _prune_old_backups(
        dest_dir=dest_dir,
        source_filename=source_file.name,
        keep_count=keep_count,
    )

    # (6) 로깅 — 최종 상태
    logger.info(
        "로테이션 완료: 삭제 {}건, 유지 {}건 (keep={})",
        deleted_count,
        len(kept_paths),
        keep_count,
    )
    if kept_paths:
        # 최신→과거 순으로 파일명만 나열 (경로 전체는 너무 장황)
        names = [path.name for path in kept_paths]
        logger.info("유지 중인 백업 파일: {}", names)

    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 구성한다."""
    parser = argparse.ArgumentParser(
        prog="python scripts/backup_db.py",
        description="SQLite DB 를 타임스탬프 파일로 복제하고 최근 N 개만 보관한다.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=_DEFAULT_KEEP_COUNT,
        help=f"유지할 최신 백업 개수 (기본값: {_DEFAULT_KEEP_COUNT}).",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=_DEFAULT_DEST_DIR,
        help=f"백업 저장 디렉터리 (기본값: {_DEFAULT_DEST_DIR.relative_to(PROJECT_ROOT)}).",
    )
    return parser


def main() -> None:
    """OS 진입점. argparse → run_backup → sys.exit."""
    configure_logging()
    args = _build_arg_parser().parse_args()

    if args.keep < 1:
        # argparse 의 choices 로는 1 이상 제약이 어려우니 여기서 early-exit.
        logger.error("--keep 은 1 이상이어야 합니다: {}", args.keep)
        sys.exit(1)

    exit_code = run_backup(dest_dir=args.dest, keep_count=args.keep)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
