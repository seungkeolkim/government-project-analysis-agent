"""고아 첨부파일 GC 핵심 로직 (Phase 5a / task 00041-5).

설계 근거: docs/snapshot_pipeline_design.md §11.

존재 이유:
    Phase 5a 의 delta + apply 트랜잭션은 SQLAlchemy auto-rollback 으로 본 테이블
    / snapshot / delta 를 atomic 단위로 보호하지만, **첨부 파일 자체는 파일
    시스템에 떨어져 있어 트랜잭션 보호 밖**이다 (사용자 원문 주의:
    \"첨부 파일 자체는 트랜잭션 보호 못함 (FS) — 고아 파일은 GC 로 정리\").

    따라서 다음 경로로 \"디스크에 파일은 있지만 본 테이블 attachments 가 참조
    하지 않는\" 고아 파일이 누적될 수 있다:
        1. apply 트랜잭션이 raise → SQLAlchemy 가 본 테이블 변경을 rollback 했지만
           파일은 이미 디스크에 떨어져 있음 (검증 11 시나리오의 부산물).
        2. 운영자가 본 테이블 attachments row 를 수동 삭제한 경우.
        3. (d) new_version 후속 정리 등 — 향후 phase 의 정리 작업이 row 를 지운
           경우.

본 모듈은 두 호출 경로를 공유한다:
    - CLI: ``scripts/gc_orphan_attachments.py`` — 운영자가 수동 실행.
    - 스케줄러: ``app/scheduler/job_runner.py::gc_orphan_attachments_job`` — APScheduler
      cron 으로 일 1회 자동 실행 (권장: KST 04:00 — 새벽 시간대로 수집과의
      충돌을 피한다).

동시성 가드 (설계 §11.3):
    GC 실행 시점에 ``ScrapeRun.status='running'`` 인 row 가 있으면 **즉시 거부**
    한다. 이유:
        수집 도중에는 \"파일은 디스크에 떨어졌지만 delta_attachments INSERT 가
        아직 안 됐고, 본 테이블에는 더더욱 안 들어간\" 짧은 윈도우가 있다. 이
        시점에 GC 가 돌면 \"본 테이블에 없는 파일\" 로 잘못 판정해 방금 다운로드
        된 파일을 지운다.
    --force 로 우회할 수 있지만 운영자가 의도적으로 위험을 감수해야 한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Attachment
from app.db.repository import get_running_scrape_run
from app.db.session import session_scope


# ──────────────────────────────────────────────────────────────
# 종료 코드 상수 (설계 §11.2)
# ──────────────────────────────────────────────────────────────

# 정상 종료 — 고아 0 건이라도 OK.
EXIT_OK: int = 0

# 디렉터리 접근 실패 / 환경 변수 누락 등 운영 환경 오류.
EXIT_ENV_ERROR: int = 1

# 진행 중 ScrapeRun 이 있어 GC 가 거부됨 (--force 없이 호출).
EXIT_SCRAPE_RUNNING: int = 2


# 디버그 / 운영 로그용 — dry-run 모드에서 샘플로 보여줄 고아 파일 수의 상한.
_SAMPLE_LOG_LIMIT: int = 5


@dataclass
class OrphanGcReport:
    """GC 1회 실행 결과 요약.

    CLI / 스케줄러 / 단위 테스트가 공통으로 소비한다. 외부 표시(JSON 직렬화)는
    호출자 책임이며, 본 dataclass 자체는 평문 dataclass 다.

    Attributes:
        scanned_root: 실제로 스캔한 download_dir 절대경로.
        dry_run: True 면 디스크 변경 없이 후보만 산출.
        skipped_due_to_running_scrape_run: ScrapeRun 가드로 GC 가 거부됐는지.
                                            True 면 disk/orphan 카운터는 미수집.
        disk_file_count: scanned_root 아래 (재귀) 발견된 일반 파일 총수.
        db_referenced_count: 본 테이블 ``attachments.stored_path`` 가 가리킨 절대
                             경로 set 의 크기 (정규화 후).
        orphan_files: 고아로 판정된 파일 절대경로 리스트 (asc 정렬).
        total_orphan_bytes: 고아 파일 크기 합계 (bytes). 읽기 실패 시 0 합산.
        deleted_count: 실제로 unlink 에 성공한 파일 수 (dry_run 이면 0).
        deletion_failed: ``[(path, reason), ...]`` — unlink 실패 항목.
        removed_directory_count: cleanup 으로 정리된 빈 디렉터리 수.
    """

    scanned_root: Path
    dry_run: bool
    skipped_due_to_running_scrape_run: bool = False
    disk_file_count: int = 0
    db_referenced_count: int = 0
    orphan_files: list[Path] = field(default_factory=list)
    total_orphan_bytes: int = 0
    deleted_count: int = 0
    deletion_failed: list[tuple[Path, str]] = field(default_factory=list)
    removed_directory_count: int = 0


# ──────────────────────────────────────────────────────────────
# 핵심 헬퍼 (순수 함수 — 단위 테스트 대상)
# ──────────────────────────────────────────────────────────────


def _normalize_path(path_value: str | Path) -> Path:
    """경로 문자열/Path 를 절대경로로 정규화한다.

    guidance 명시: \"settings.download_dir 절대화는 Path.resolve(strict=False)
    로 통일\". 심볼릭 링크 / 이중 슬래시 / 상대 경로를 한 함수로 통일해 디스크
    set 와 DB set 의 비교가 시맨틱적으로 일치하도록 한다.

    strict=False 는 경로가 실재하지 않아도 OSError 를 내지 않는다 — DB 에 stale
    한 stored_path 가 있어도 정규화는 진행한다 (비교 결과 그대로 디스크 set
    에 매칭이 안 되면 자연스럽게 스킵).
    """
    return Path(path_value).expanduser().resolve(strict=False)


def gather_db_attachment_paths(session: Session) -> set[Path]:
    """본 테이블 ``attachments.stored_path`` 를 정규화된 절대경로 set 으로 반환.

    NULL / 빈 문자열은 제외한다 (다운로드 실패로 stored_path 가 비어있는 row 가
    드물게 있을 수 있다). 비교 시점의 일관성을 위해 모두 ``_normalize_path``
    를 통과시킨다.

    설계 §11.1 에 따라 **delta_attachments.stored_path 는 포함하지 않는다** —
    delta 는 임시 staging 이며 GC 시점의 비교 안전 set 은 본 테이블 한정이다.
    이 정책의 위험(verification 11 의 retry 시점 충돌) 은 §11.3 의 ScrapeRun
    running 가드가 1차 방어선으로 동작한다.

    Args:
        session: 호출자 세션.

    Returns:
        본 테이블이 참조 중인 절대경로 set.
    """
    rows = session.execute(select(Attachment.stored_path)).all()
    paths: set[Path] = set()
    for (stored_path,) in rows:
        if not stored_path:
            continue
        paths.add(_normalize_path(stored_path))
    return paths


def collect_disk_files(root: Path) -> Iterator[Path]:
    """``root`` 아래의 모든 일반 파일을 재귀 yield 한다.

    심볼릭 링크는 따라가지 않는다 (보안 + 무한 루프 방지). ``root`` 가 존재
    하지 않으면 yield 0건. 디렉터리는 yield 하지 않는다.

    Args:
        root: 스캔할 디렉터리 절대경로.

    Yields:
        파일의 ``Path`` (정규화 전 — 호출자가 _normalize_path 를 별도 적용).
    """
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            yield path


def compute_orphan_files(
    root: Path,
    db_paths: set[Path],
) -> tuple[list[Path], int]:
    """root 의 disk 파일 집합 − db 참조 집합 = 고아 파일 리스트를 산출한다.

    안전 가드:
        - 정규화 후 ``root.is_relative_to`` 검사로 root 외부 경로는 절대 포함
          하지 않는다. ``rglob`` 가 root 내부만 yield 하지만 심볼릭 링크 등이
          섞여 있을 가능성을 한 번 더 막는다.
        - 결과 리스트는 asc 정렬 (재현 가능성 + 운영 시 로그 가독성).

    Args:
        root:     스캔 대상 절대경로 (정규화된 형태).
        db_paths: ``gather_db_attachment_paths`` 결과 — 본 테이블 참조 set.

    Returns:
        ``(orphan_paths, disk_file_total_count)``.
        disk_file_total_count 는 disk 의 일반 파일 총 개수 (db 매칭 / 미매칭
        합산) — 보고서의 disk_file_count 채움 용.
    """
    orphans: list[Path] = []
    disk_total = 0
    for disk_path in collect_disk_files(root):
        disk_total += 1
        normalized = _normalize_path(disk_path)
        # root 외부 경로는 GC 대상 아님 (방어 가드).
        if not normalized.is_relative_to(root):
            continue
        if normalized in db_paths:
            continue
        orphans.append(normalized)
    orphans.sort()
    return orphans, disk_total


def _file_size_bytes(path: Path) -> int:
    """파일 크기 (bytes). 읽기 실패 시 0.

    GC 보고서의 합계 표시 용이므로 stat 실패가 있어도 GC 진행을 막지 않는다.
    """
    try:
        return path.stat().st_size
    except OSError:
        return 0


def delete_orphan_files(
    orphans: list[Path],
    root: Path,
) -> tuple[int, list[tuple[Path, str]], int]:
    """고아 파일을 unlink 하고 빈 디렉터리를 cleanup 한다.

    삭제 정책:
        - root 외부 경로는 절대 unlink 하지 않는다 (defense-in-depth).
        - unlink 실패는 ``deletion_failed`` 에 누적해 다음 항목 진행 — 한 파일이
          실패해도 다른 고아의 처리를 막지 않는다.
        - 영향받은 디렉터리(고아의 부모) 들을 bottom-up 으로 ``rmdir`` 시도.
          빈 디렉터리만 정리되며, 안에 남은 파일이 있으면 ``OSError`` 로 자연
          스킵. ``root`` 자체는 비어도 절대 삭제하지 않는다.

    Args:
        orphans: ``compute_orphan_files`` 가 반환한 절대경로 리스트.
        root:    스캔 대상 절대경로 (cleanup 의 상한 경계).

    Returns:
        ``(deleted_count, failed_list, removed_directory_count)``.
    """
    deleted = 0
    failed: list[tuple[Path, str]] = []
    affected_dirs: set[Path] = set()

    for orphan in orphans:
        # root 안에만 동작 — 외부 경로는 절대 건드리지 않는다.
        if not orphan.is_relative_to(root):
            failed.append((orphan, "root 외부 경로 (스킵)"))
            continue
        try:
            orphan.unlink(missing_ok=True)
            deleted += 1
            affected_dirs.add(orphan.parent)
        except OSError as exc:
            failed.append((orphan, f"{type(exc).__name__}: {exc}"))

    # 빈 디렉터리 정리 — 깊은 곳부터 위로 (rmdir 은 빈 디렉터리만).
    candidates = sorted(
        affected_dirs, key=lambda candidate: len(candidate.parts), reverse=True
    )
    removed_dirs = 0
    visited: set[Path] = set()
    for current in candidates:
        cursor = current
        while (
            cursor != root
            and cursor.is_relative_to(root)
            and cursor not in visited
        ):
            visited.add(cursor)
            try:
                cursor.rmdir()
                removed_dirs += 1
            except OSError:
                # 아직 파일이 남아있거나 권한 문제. 부모로 더 올라가지 않는다
                # (부모도 어차피 비어있지 않을 가능성이 크다).
                break
            cursor = cursor.parent

    return deleted, failed, removed_dirs


# ──────────────────────────────────────────────────────────────
# 공개 API — run_gc
# ──────────────────────────────────────────────────────────────


def run_gc(
    *,
    dry_run: bool = False,
    root_override: Optional[Path] = None,
    force: bool = False,
    session_scope_factory: Optional[
        Callable[[], AbstractContextManager[Session]]
    ] = None,
) -> OrphanGcReport:
    """고아 첨부파일을 스캔하고 (dry_run=False 면) 삭제한다.

    동작 (설계 §11.2):
        1. download_dir 결정: ``root_override`` 가 있으면 그 값, 없으면
           ``settings.download_dir`` (이미 절대경로화 되어 있음).
        2. ScrapeRun ``running`` 가드: 매칭 row 가 있으면 ``--force`` 가 없는 한
           즉시 종료 (보고서.skipped_due_to_running_scrape_run=True).
        3. 본 테이블 ``attachments.stored_path`` 를 정규화 set 으로 수집.
        4. 디스크 스캔 → ``compute_orphan_files`` 로 disk − db 차집합 산출.
        5. dry_run 이면 보고서만, 아니면 ``delete_orphan_files`` 호출해 unlink
           + 빈 디렉터리 cleanup.

    Args:
        dry_run:               True 면 디스크 변경 없이 보고서만 산출.
        root_override:         테스트용 download_dir 강제 지정. 운영에서는 None.
        force:                 True 면 ScrapeRun running 가드를 우회. 운영자가
                                 의도적으로 위험을 감수할 때만 사용.
        session_scope_factory: 테스트용 세션 팩토리 주입점. None 이면 운영 기본
                                 ``app.db.session.session_scope``.

    Returns:
        ``OrphanGcReport``. 호출자(CLI/scheduler) 가 종료 코드 결정.
    """
    settings = get_settings()
    root = _normalize_path(
        root_override if root_override is not None else settings.download_dir
    )
    factory = session_scope_factory or session_scope

    report = OrphanGcReport(scanned_root=root, dry_run=dry_run)

    if not root.exists():
        # 실 운영에서는 settings.ensure_runtime_paths() 로 항상 존재 보장되지만,
        # 테스트 / 일회성 호출 등에서 누락된 경우를 안전 처리한다.
        logger.warning("download_dir 가 존재하지 않습니다: {}", root)
        return report

    # ── 1. ScrapeRun 가드 + DB 참조 set 수집 (같은 세션) ────────────────────
    with factory() as session:
        running = get_running_scrape_run(session)
        if running is not None and not force:
            logger.warning(
                "GC 거부 — ScrapeRun id={} 가 'running' 입니다 "
                "(--force 없이 진행 안 함, 종료 코드={}).",
                running.id,
                EXIT_SCRAPE_RUNNING,
            )
            report.skipped_due_to_running_scrape_run = True
            return report
        if running is not None and force:
            logger.warning(
                "GC --force — ScrapeRun id={} running 인데도 진행합니다. "
                "방금 다운로드된 파일이 잘못 삭제될 수 있어 위험합니다.",
                running.id,
            )

        db_paths = gather_db_attachment_paths(session)

    report.db_referenced_count = len(db_paths)

    # ── 2. 디스크 스캔 + 고아 산출 ──────────────────────────────────────────
    orphans, disk_total = compute_orphan_files(root, db_paths)
    report.disk_file_count = disk_total
    report.orphan_files = list(orphans)
    report.total_orphan_bytes = sum(_file_size_bytes(orphan) for orphan in orphans)

    if not orphans:
        logger.info(
            "고아 파일 없음: scanned_root={} disk_files={} db_paths={}",
            root,
            disk_total,
            report.db_referenced_count,
        )
        return report

    if dry_run:
        logger.info(
            "[DRY-RUN] 고아 파일 {}건 ({} bytes). 실제 삭제는 --dry-run 없이 재실행. "
            "scanned_root={}",
            len(orphans),
            report.total_orphan_bytes,
            root,
        )
        for sample in orphans[:_SAMPLE_LOG_LIMIT]:
            logger.info("  • {}", sample)
        if len(orphans) > _SAMPLE_LOG_LIMIT:
            logger.info(
                "  ... 외 {}건 (--dry-run 출력 제한)",
                len(orphans) - _SAMPLE_LOG_LIMIT,
            )
        return report

    # ── 3. 실제 삭제 ────────────────────────────────────────────────────────
    deleted, failed, removed_dirs = delete_orphan_files(orphans, root)
    report.deleted_count = deleted
    report.deletion_failed = failed
    report.removed_directory_count = removed_dirs

    logger.info(
        "GC 완료: 삭제={}건 실패={}건 빈 디렉터리 정리={}건 "
        "총 회수={} bytes scanned_root={}",
        deleted,
        len(failed),
        removed_dirs,
        report.total_orphan_bytes,
        root,
    )
    if failed:
        for path, reason in failed[:_SAMPLE_LOG_LIMIT]:
            logger.warning("  ✗ unlink 실패: {} — {}", path, reason)
    return report


__all__ = [
    "EXIT_ENV_ERROR",
    "EXIT_OK",
    "EXIT_SCRAPE_RUNNING",
    "OrphanGcReport",
    "collect_disk_files",
    "compute_orphan_files",
    "delete_orphan_files",
    "gather_db_attachment_paths",
    "run_gc",
]
