"""DB 백업 비즈니스 로직 — 단일 진입 모듈 (task 00094-1).

책임:
    - SQLite DB 파일 안전 복사 (``sqlite3.Connection.backup()``)
    - 백업 이력(BackupHistory) 기록
    - SystemSetting key-value CRUD (백업 cron·보관 수)
    - 보관 수 초과 시 오래된 백업 자동 삭제
    - 기존 백업 파일 목록 조회

설계 결정:
    - 백업 대상은 ``app.config.Settings.db_url`` 과 ``suggestions_db_url`` 에서 파생한다.
      **첨부 다운로드 디렉터리(download_dir)는 절대 포함하지 않는다 — 사용자 원문 명시.**
    - SQLite 안전 복사: ``sqlite3.connect(src).backup(dst_conn)`` 사용.
      단순 ``shutil.copy2`` 는 진행 중 트랜잭션 시 손상 위험이 있다.
    - 백업 파일명: ``{db_stem}_{KST_timestamp}{suffix}`` (예: ``app_20260508_030000.sqlite3``).
    - 보관 수(max_count): 타임스탬프 기준 최신 N 그룹만 유지, 초과분 자동 삭제.
    - 이 모듈은 라우트·스케줄러를 등록하지 않는다. 그것은 00094-2·3 의 책임이다.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.backup.constants import (
    BACKUP_TRIGGER_MANUAL,
    DEFAULT_BACKUP_CRON,
    DEFAULT_BACKUP_MAX_COUNT,
    SETTING_KEY_BACKUP_CRON,
    SETTING_KEY_BACKUP_MAX_COUNT,
)
from app.config import PROJECT_ROOT, get_settings
from app.db.models import BackupHistory, SystemSetting
from app.db.session import session_scope
from app.timezone import format_kst, now_utc


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _sqlite_file_paths_from_url(db_url: str) -> Path | None:
    """SQLite URL 에서 파일 경로를 추출한다. SQLite 가 아니면 None."""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return None
    raw = db_url[len(prefix):]
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def _get_backup_dir() -> Path:
    """백업 파일을 저장할 디렉터리 Path 를 반환한다."""
    return get_settings().backup_dir


def _get_max_count_from_db() -> int:
    """DB 의 SystemSetting 에서 max_count 를 읽는다. 없으면 기본값."""
    with session_scope() as session:
        raw = get_setting(session, SETTING_KEY_BACKUP_MAX_COUNT)
    if raw is None:
        return DEFAULT_BACKUP_MAX_COUNT
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_BACKUP_MAX_COUNT
    except (ValueError, TypeError):
        return DEFAULT_BACKUP_MAX_COUNT


def _prune_old_backups(backup_dir: Path, max_count: int) -> int:
    """타임스탬프 그룹 기준으로 오래된 백업을 삭제한다.

    백업 파일명은 ``{stem}_{YYYYMMDD}_{HHMMSS}{suffix}`` 형식이다.
    가장 마지막 두 언더스코어 구분 부분을 타임스탬프 그룹 키로 사용한다.
    max_count 를 초과하는 오래된 그룹부터 삭제한다.

    Args:
        backup_dir: 백업 파일이 저장된 디렉터리.
        max_count:  유지할 최대 타임스탬프 그룹 수.

    Returns:
        실제로 삭제된 파일 수.
    """
    if max_count <= 0 or not backup_dir.exists():
        return 0

    # 파일을 타임스탬프 그룹으로 묶는다.
    # 파일명 예: app_20260508_030000.sqlite3 → 그룹 키 "20260508_030000"
    groups: dict[str, list[Path]] = {}
    for f in backup_dir.iterdir():
        if not f.is_file():
            continue
        parts = f.stem.split("_")
        if len(parts) >= 3:
            # 마지막 두 파트가 날짜·시각
            timestamp_key = f"{parts[-2]}_{parts[-1]}"
            groups.setdefault(timestamp_key, []).append(f)

    # 타임스탬프 내림차순 정렬 → 최신 max_count 개 초과분 삭제
    sorted_timestamps = sorted(groups.keys(), reverse=True)
    deleted = 0
    for ts in sorted_timestamps[max_count:]:
        for f in groups[ts]:
            try:
                f.unlink()
                deleted += 1
                logger.info("오래된 백업 삭제: {}", f.name)
            except OSError as exc:
                logger.warning("백업 파일 삭제 실패: {} — {}", f.name, exc)

    return deleted


# ──────────────────────────────────────────────────────────────
# SystemSetting CRUD
# ──────────────────────────────────────────────────────────────


def get_setting(session: Session, key: str) -> str | None:
    """SystemSetting 에서 key 의 값을 반환한다.

    Args:
        session: ORM 세션.
        key:     설정 키. ``app.backup.constants.SETTING_KEY_*`` 상수 참조.

    Returns:
        저장된 값 문자열. 키가 없거나 값이 NULL 이면 None.
    """
    row = session.get(SystemSetting, key)
    return row.value if row is not None else None


def set_setting(session: Session, key: str, value: str | None) -> None:
    """SystemSetting 을 upsert 한다.

    키가 이미 존재하면 value 를 업데이트하고, 없으면 새 row 를 INSERT 한다.
    ``session.flush()`` 를 호출하지 않으므로 커밋은 호출 측(session_scope) 에서 처리한다.

    Args:
        session: ORM 세션.
        key:     설정 키.
        value:   설정 값. None 이면 DB 에 NULL 저장.
    """
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=value, updated_at=now_utc()))
    else:
        row.value = value
        row.updated_at = now_utc()


# ──────────────────────────────────────────────────────────────
# 백업 대상 조회
# ──────────────────────────────────────────────────────────────


def get_backup_db_targets() -> list[Path]:
    """백업 대상 SQLite DB 파일 경로 목록을 반환한다.

    ``app.config.Settings.db_url`` 과 ``suggestions_db_url`` 에서 파생한다.
    SQLite 가 아닌 URL (예: Postgres) 은 파일 기반 복사 대상이 아니므로 제외한다.
    **첨부 다운로드 디렉터리(download_dir)는 절대 포함하지 않는다.**

    Returns:
        존재 여부와 무관하게, 설정에서 파생된 SQLite 파일 경로 목록.
        경로가 존재하지 않는 경우는 run_backup 에서 건너뛴다.
    """
    settings = get_settings()
    targets: list[Path] = []
    for db_url in (settings.db_url, settings.suggestions_db_url):
        path = _sqlite_file_paths_from_url(db_url)
        if path is not None:
            targets.append(path)
    return targets


# ──────────────────────────────────────────────────────────────
# 핵심 백업 실행
# ──────────────────────────────────────────────────────────────


def run_backup(*, trigger: str = BACKUP_TRIGGER_MANUAL) -> BackupHistory:
    """모든 대상 DB 파일을 백업하고 이력을 기록한다.

    ``sqlite3.Connection.backup()`` 을 사용해 진행 중 트랜잭션이 있어도
    일관된 스냅샷을 얻는다. 백업 디렉터리는 없으면 자동 생성한다.

    백업 파일명: ``{db_stem}_{KST_YYYYMMDD_HHMMSS}{suffix}``
    예: ``app_20260508_030000.sqlite3``

    성공 후 보관 수(max_count) 를 초과한 오래된 백업을 자동으로 삭제한다.

    Args:
        trigger: ``'manual'`` 또는 ``'scheduled'``. 기본값은 ``'manual'``.

    Returns:
        DB 에 저장된 BackupHistory 레코드.
    """
    start_time = time.monotonic()
    executed_at = now_utc()
    # 백업 파일명에 사용할 KST 타임스탬프
    timestamp = format_kst(executed_at, "%Y%m%d_%H%M%S")

    targets = get_backup_db_targets()
    backup_dir = _get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    backed_up_files: list[str] = []
    target_file_strs: list[str] = []
    total_size_bytes: int = 0
    error_message: str | None = None
    success = True

    for db_path in targets:
        if not db_path.exists():
            logger.warning("백업 대상 파일이 존재하지 않아 건너뜀: {}", db_path)
            continue

        # PROJECT_ROOT 기준 상대 경로로 이력에 기록
        try:
            rel_str = str(db_path.relative_to(PROJECT_ROOT))
        except ValueError:
            rel_str = str(db_path)
        target_file_strs.append(rel_str)

        backup_filename = f"{db_path.stem}_{timestamp}{db_path.suffix}"
        backup_path = backup_dir / backup_filename

        try:
            src_conn = sqlite3.connect(str(db_path))
            dst_conn = sqlite3.connect(str(backup_path))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()

            try:
                backup_rel_str = str(backup_path.relative_to(PROJECT_ROOT))
            except ValueError:
                backup_rel_str = str(backup_path)

            backed_up_files.append(backup_rel_str)
            total_size_bytes += backup_path.stat().st_size
            logger.info("백업 완료: {} → {}", rel_str, backup_filename)

        except Exception as exc:
            success = False
            error_message = f"{db_path.name}: {exc}"
            logger.exception("백업 실패: {} — {}", db_path.name, exc)
            # 부분 실패도 이력에 남기고 루프 계속
            break

    duration_seconds = round(time.monotonic() - start_time, 3)

    history = BackupHistory(
        executed_at=executed_at,
        trigger=trigger,
        target_files=target_file_strs,
        backup_files=backed_up_files,
        success=success,
        error_message=error_message,
        duration_seconds=duration_seconds,
        total_size_bytes=total_size_bytes if success else None,
    )

    with session_scope() as session:
        session.add(history)

    if success:
        max_count = _get_max_count_from_db()
        pruned = _prune_old_backups(backup_dir, max_count)
        if pruned:
            logger.info("오래된 백업 {}건 삭제 (max_count={})", pruned, max_count)

    return history


# ──────────────────────────────────────────────────────────────
# 조회 API
# ──────────────────────────────────────────────────────────────


def list_backup_history(session: Session, *, limit: int = 50) -> list[BackupHistory]:
    """최근 백업 이력을 내림차순으로 반환한다.

    Args:
        session: ORM 세션.
        limit:   반환할 최대 레코드 수. 기본값 50.

    Returns:
        ``executed_at`` 내림차순(최신 먼저)으로 정렬된 BackupHistory 목록.
    """
    from sqlalchemy import select

    stmt = (
        select(BackupHistory)
        .order_by(BackupHistory.executed_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def list_backup_files() -> list[dict[str, Any]]:
    """백업 디렉터리에 존재하는 백업 파일 목록을 반환한다.

    파일은 수정 시각(mtime) 내림차순으로 정렬된다 (최신 먼저).
    파일 생성 시 즉시 mtime 이 찍히므로 생성 시각과 실질적으로 동일하다.

    Returns:
        각 파일에 대한 dict 목록. 키:
            - ``filename``: 파일명 (경로 제외)
            - ``size_bytes``: 파일 크기 (bytes)
            - ``modified_at``: 수정 시각 (UTC tz-aware datetime)
    """
    backup_dir = _get_backup_dir()
    if not backup_dir.exists():
        return []

    result: list[dict[str, Any]] = []
    for f in sorted(
        backup_dir.iterdir(),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if not f.is_file():
            continue
        stat = f.stat()
        result.append(
            {
                "filename": f.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            }
        )
    return result


def get_backup_settings(session: Session) -> dict[str, str]:
    """현재 백업 설정(cron 표현식·최대 보관 수)을 반환한다.

    SystemSetting 에 값이 없으면 기본값으로 채운다.

    Args:
        session: ORM 세션.

    Returns:
        키: ``'cron_expression'``, ``'max_count'``.
    """
    cron = get_setting(session, SETTING_KEY_BACKUP_CRON) or DEFAULT_BACKUP_CRON
    max_count = get_setting(session, SETTING_KEY_BACKUP_MAX_COUNT) or str(DEFAULT_BACKUP_MAX_COUNT)
    return {
        "cron_expression": cron,
        "max_count": max_count,
    }


__all__ = [
    "get_backup_db_targets",
    "get_backup_settings",
    "get_setting",
    "list_backup_files",
    "list_backup_history",
    "run_backup",
    "set_setting",
]
