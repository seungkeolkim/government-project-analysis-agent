"""고아 첨부 파일 GC 스크립트 (Phase 5a / task 00041-5).

설계 근거: docs/snapshot_pipeline_design.md §11.2.

사용 가이드:
    1. **컨테이너 안에서 실행한다** — 호스트에서 직접 ``python scripts/...`` 로
       돌리지 말 것. settings.download_dir / DB_URL 등이 컨테이너 기준으로만
       의미가 있고, 호스트 경로와 컨테이너 경로가 어긋나면 멀쩡한 파일을
       \"고아\" 로 판정해 삭제할 수 있다.

       권장 호출:
           docker compose --profile scrape run --rm scraper \\
               python scripts/gc_orphan_attachments.py --dry-run

    2. **먼저 ``--dry-run`` 으로 후보 확인** → 출력 검토 → 같은 명령어에서
       ``--dry-run`` 을 빼고 재실행해 실제 삭제. 사용자 원문 검증 8 그대로
       (\"--dry-run 검증 후 실제 삭제\").

    3. ``ScrapeRun.status='running'`` 인 row 가 있으면 GC 는 거부된다 (종료
       코드 2). 수집이 끝났는데도 lock 이 남아 있으면 웹 startup 의 stale
       cleanup 이 자동 정리한다 (Phase 2 §7.4). 그래도 강제 진행이 필요하면
       ``--force`` 를 쓰되, **방금 다운로드된 파일이 잘못 삭제될 수 있다**.

옵션 요약:
    --dry-run       삭제하지 않고 후보만 출력 (기본 동작은 실제 삭제).
    --force         ScrapeRun running 가드를 우회 (위험 — 위 가이드 참조).
    --root PATH     download_dir 오버라이드 (테스트용. 운영에서는 settings 사용).

종료 코드:
    0  정상 (고아 0건이라도 0).
    1  디렉터리 접근 실패 등 환경 오류.
    2  진행 중 ScrapeRun 이 있어 GC 가 거부됨 (--force 없이).

본 스크립트의 핵심 로직은 ``app.scrape_control.orphan_gc`` 모듈이다 — 본
파일은 argparse + sys.exit 어댑터다. APScheduler 일 1회 자동 실행 경로는
``app/scheduler/job_runner.py`` 의 ``gc_orphan_attachments_job`` 가 동일 모듈을
공유한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가 — scripts/ 는 패키지가 아니라 직접 실행 시
# ``app`` import 가 안 된다. 다른 운영 스크립트(create_admin / backup_db 등)와
# 동일 패턴.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger  # noqa: E402

from app.logging_setup import configure_logging  # noqa: E402
from app.scrape_control.orphan_gc import (  # noqa: E402
    EXIT_ENV_ERROR,
    EXIT_OK,
    EXIT_SCRAPE_RUNNING,
    OrphanGcReport,
    run_gc,
)


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 만든다. 본 스크립트의 외부 인터페이스 정의.

    옵션 의미는 모듈 docstring 참조.
    """
    parser = argparse.ArgumentParser(
        prog="python scripts/gc_orphan_attachments.py",
        description=(
            "data/downloads/ 의 첨부 파일 중 본 테이블 attachments 가 참조하지 "
            "않는 고아 파일을 스캔/삭제한다. (사용자 원문 검증 8)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="삭제하지 않고 후보만 출력 (기본은 실제 삭제). 운영 시 먼저 "
             "--dry-run 으로 후보를 검토한 뒤 같은 명령어에서 --dry-run 을 "
             "빼고 재실행하는 것을 권장한다.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ScrapeRun running 가드를 우회. 수집 중 다운로드된 파일이 "
             "잘못 삭제될 수 있어 위험하다 — 운영자가 의도적으로 결정해야 한다.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="download_dir 오버라이드 (기본: app.config.get_settings().download_dir). "
             "테스트 / 일회성 검증 용도이며 운영에서는 지정하지 않는다.",
    )
    return parser


def _print_dry_run_summary(report: OrphanGcReport) -> None:
    """--dry-run 보고서를 stdout 으로 출력한다.

    파일 수가 많아도 처음 일부만 보여주고 나머지는 합계로 안내한다 — 화면
    스크롤 폭주 방지. 정확한 후보 목록은 loguru 로그에서 확인 가능.
    """
    print(
        f"[DRY-RUN] scanned_root={report.scanned_root} "
        f"disk_files={report.disk_file_count} "
        f"db_paths={report.db_referenced_count} "
        f"orphans={len(report.orphan_files)} "
        f"total_bytes={report.total_orphan_bytes}"
    )
    if not report.orphan_files:
        return
    sample_limit = 20
    for orphan in report.orphan_files[:sample_limit]:
        print(f"  ORPHAN  {orphan}")
    if len(report.orphan_files) > sample_limit:
        print(
            f"  ... 외 {len(report.orphan_files) - sample_limit}건 "
            "(전체는 로그 확인)"
        )


def _print_apply_summary(report: OrphanGcReport) -> None:
    """실제 삭제 모드의 결과 요약을 stdout 으로 출력한다."""
    print(
        f"[APPLY] scanned_root={report.scanned_root} "
        f"deleted={report.deleted_count} "
        f"failed={len(report.deletion_failed)} "
        f"removed_dirs={report.removed_directory_count} "
        f"total_bytes_freed={report.total_orphan_bytes}"
    )
    if report.deletion_failed:
        sample_limit = 10
        for path, reason in report.deletion_failed[:sample_limit]:
            print(f"  FAILED  {path} — {reason}")
        if len(report.deletion_failed) > sample_limit:
            print(
                f"  ... 외 {len(report.deletion_failed) - sample_limit}건 실패 "
                "(전체는 로그 확인)"
            )


def main() -> None:
    """CLI 진입점.

    sys.exit 종료 코드 (모듈 docstring 참조):
        0  정상.
        1  환경 오류 (예: download_dir 접근 불가).
        2  ScrapeRun running 가드로 거부됨.
    """
    args = _build_arg_parser().parse_args()
    configure_logging()

    try:
        report = run_gc(
            dry_run=args.dry_run,
            root_override=args.root,
            force=args.force,
        )
    except OSError as exc:
        # 디렉터리 접근 / DB 접속 실패 등 — 운영 환경 문제.
        logger.exception("GC 실행 중 환경 오류: {}: {}", type(exc).__name__, exc)
        sys.exit(EXIT_ENV_ERROR)

    if report.skipped_due_to_running_scrape_run:
        # 메시지는 run_gc 가 이미 logger.warning 으로 남겼다.
        sys.exit(EXIT_SCRAPE_RUNNING)

    if report.dry_run:
        _print_dry_run_summary(report)
    else:
        _print_apply_summary(report)

    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
