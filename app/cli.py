"""IRIS 스크래퍼 오케스트레이터 CLI.

현재 활성화된 흐름:
    (1) init_db 로 스키마 보장
    (2) list_scraper.scrape_list 로 '접수중' 목록 수집
    (3) 각 공고에 대해 upsert_announcement 로 기본 메타 적재
    (4) 각 공고의 상세 페이지를 detail_scraper 로 수집해 DB 갱신 (--skip-detail 로 건너뛸 수 있음)

비활성화(첨부파일 다운로드는 별도 subtask에서 활성화 예정):
    - 첨부파일 다운로드

호출 형태:
    python -m app.cli run [--max-pages N] [--max-announcements N] [--skip-detail] [--dry-run] [--log-level LEVEL]

옵션:
    --max-pages N          list_scraper 가 순회할 최대 페이지 수. 0(기본) = 안전 상한 사용.
    --max-announcements N  UPSERT 할 최대 공고 수. 0(기본) = 무제한.
    --skip-detail          상세 페이지 수집을 건너뛴다(목록 적재만 수행).
    --dry-run              DB 쓰기를 건너뛰고 수집만 검증한다.
    --log-level LEVEL      로그 레벨 일회성 오버라이드(기본: settings.log_level).

종료 코드:
    0  : 정상(처리한 공고 수가 0 건이어도 정상 종료)
    1  : 부트스트랩 단계(init_db / 목록 수집) 자체가 실패한 경우
    130: SIGINT (Ctrl+C) 로 중단된 경우
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from loguru import logger

import httpx

from app.config import Settings, get_settings
from app.db.init_db import init_db
from app.db.repository import upsert_announcement, upsert_announcement_detail
from app.db.session import session_scope
from app.logging_setup import configure_logging
from app.scraper.detail_scraper import _build_detail_http_client, scrape_detail_with_client
from app.scraper.list_scraper import scrape_list

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

DEFAULT_STATUS_LABEL: str = "접수중"

# argparse 기본값. 0 은 '무제한' 으로 해석한다.
DEFAULT_MAX_PAGES_FLAG: int = 0
DEFAULT_MAX_ANNOUNCEMENTS_FLAG: int = 0

# list_scraper 의 안전 상한.
LIST_SCRAPER_DEFAULT_MAX_PAGES_SAFE_CEILING: int = 50

# 날짜 텍스트 → datetime 변환 시 시도할 포맷 후보.
_DATETIME_TEXT_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    # IRIS API 응답 날짜 형식 (예: "2026.04.17")
    "%Y.%m.%d",
)


# ──────────────────────────────────────────────────────────────
# 데이터 변환 헬퍼
# ──────────────────────────────────────────────────────────────


def _parse_datetime_text(value: Optional[str]) -> Optional[datetime]:
    """날짜 텍스트를 timezone-aware UTC datetime 으로 변환한다.

    구분자 '/', '.' 는 '-' 로 정규화한 뒤 포맷을 순차 시도한다.
    매칭되지 않으면 경고 로그를 남기고 None 을 반환한다.

    IRIS 노출 시각은 KST 일 수 있으나 timezone 추정 없이 UTC 로 보존한다
    (원문 텍스트는 raw_metadata 에 함께 저장된다).
    """
    if not value:
        return None

    normalized_text = value.strip().replace("/", "-").replace(".", "-")
    for candidate_format in _DATETIME_TEXT_FORMATS:
        try:
            naive_dt = datetime.strptime(normalized_text, candidate_format)
        except ValueError:
            continue
        return naive_dt.replace(tzinfo=timezone.utc)

    logger.warning("날짜 텍스트 파싱 실패 — 무시: {!r}", value)
    return None


def _build_announcement_payload(
    row_metadata: dict[str, Any],
    status_label: str,
) -> dict[str, Any]:
    """list_scraper 의 row 메타를 repository.upsert_announcement payload 로 변환한다."""
    return {
        "iris_announcement_id": row_metadata["iris_announcement_id"],
        "title": row_metadata.get("title") or "(제목 미상)",
        "agency": row_metadata.get("agency"),
        "status": row_metadata.get("status") or status_label,
        "received_at": _parse_datetime_text(row_metadata.get("received_at_text")),
        "deadline_at": _parse_datetime_text(row_metadata.get("deadline_at_text")),
        "detail_url": row_metadata.get("detail_url"),
        "raw_metadata": {
            "list_row": {
                key: value
                for key, value in row_metadata.items()
                if key != "row_html"
            },
        },
    }


# ──────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def _null_async_context() -> AsyncIterator[None]:
    """skip_detail=True 또는 dry_run=True 일 때 httpx 클라이언트 자리를 채우는 더미 컨텍스트."""
    yield None


# ──────────────────────────────────────────────────────────────
# 오케스트레이션
# ──────────────────────────────────────────────────────────────


async def _orchestrate(
    *,
    settings: Settings,
    max_pages: int,
    max_announcements: int,
    skip_detail: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """목록 수집 → DB 적재 → 상세 수집 흐름을 수행하고 최종 통계 dict 를 반환한다.

    부트스트랩 실패(목록 수집 자체가 안 되는 경우)는 호출자로 전파한다.
    공고 단위 예외는 격리 — 한 공고 실패가 전체를 중단시키지 않는다.
    """
    # (1) DB 스키마 보장
    logger.info("DB 초기화(create_all) 시작 — dry_run={}", dry_run)
    init_db()

    # max_pages 0(무제한) → 안전 상한 사용
    safe_max_pages = (
        max(int(max_pages), 1)
        if max_pages > 0
        else LIST_SCRAPER_DEFAULT_MAX_PAGES_SAFE_CEILING
    )

    # (2) 목록 수집
    logger.info("scrape_list 시작: max_pages={}", safe_max_pages)
    aggregated_rows = await scrape_list(
        settings=settings,
        max_pages=safe_max_pages,
    )
    logger.info("목록 수집 완료: {}건", len(aggregated_rows))

    if max_announcements > 0:
        target_rows = aggregated_rows[:max_announcements]
        logger.info(
            "처리 대상 제한: max_announcements={} → {}건만 처리",
            max_announcements,
            len(target_rows),
        )
    else:
        target_rows = aggregated_rows

    if not target_rows:
        logger.warning("처리할 공고가 없어 종료한다.")
        return {
            "success_count": 0,
            "failure_count": 0,
            "failed_announcement_ids": [],
            "detail_success_count": 0,
            "detail_failure_count": 0,
        }

    # (3) 공고 단위 upsert + (4) 상세 수집
    success_count = 0
    failure_count = 0
    failed_announcement_ids: list[str] = []
    detail_success_count = 0
    detail_failure_count = 0

    # 상세 수집용 httpx 클라이언트 — 공고 간 재사용으로 연결 오버헤드 절감
    detail_client_ctx = (
        _build_detail_http_client(settings) if not skip_detail and not dry_run else None
    )

    async with (detail_client_ctx if detail_client_ctx else _null_async_context()) as detail_client:
        for row_index, row_metadata in enumerate(target_rows, start=1):
            iris_announcement_id = row_metadata.get("iris_announcement_id") or "(unknown)"
            detail_url: Optional[str] = row_metadata.get("detail_url")

            logger.info(
                "── [{}/{}] 공고 upsert: id={}",
                row_index,
                len(target_rows),
                iris_announcement_id,
            )

            try:
                payload = _build_announcement_payload(row_metadata, DEFAULT_STATUS_LABEL)
                if dry_run:
                    logger.info("[dry-run] upsert_announcement(skip): {}", iris_announcement_id)
                else:
                    with session_scope() as session:
                        upsert_announcement(session, payload)
                success_count += 1
            except Exception as exc:
                failure_count += 1
                failed_announcement_ids.append(str(iris_announcement_id))
                logger.exception(
                    "공고 upsert 실패(스킵): id={} ({}: {})",
                    iris_announcement_id,
                    type(exc).__name__,
                    exc,
                )
                # upsert 실패 시 상세 수집도 건너뛴다
                continue

            # (4) 상세 페이지 수집
            if skip_detail or dry_run:
                continue

            if not detail_url:
                logger.warning("detail_url 없음 — 상세 수집 스킵: id={}", iris_announcement_id)
                detail_failure_count += 1
                continue

            # 공고 간 요청 지연 (첫 번째 공고는 건너뜀)
            if row_index > 1:
                await asyncio.sleep(settings.request_delay_sec)

            logger.info("상세 수집 시작: id={} url={}", iris_announcement_id, detail_url)
            detail_result = await scrape_detail_with_client(detail_client, detail_url)

            try:
                with session_scope() as session:
                    upsert_announcement_detail(session, iris_announcement_id, detail_result)
            except Exception as exc:
                logger.exception(
                    "상세 DB 갱신 실패: id={} ({}: {})",
                    iris_announcement_id,
                    type(exc).__name__,
                    exc,
                )
                detail_failure_count += 1
                continue

            status = detail_result.get("detail_fetch_status", "error")
            if status == "ok":
                detail_success_count += 1
                logger.info("상세 수집 완료(ok): id={}", iris_announcement_id)
            else:
                detail_failure_count += 1
                logger.warning("상세 수집 결과 '{}': id={}", status, iris_announcement_id)

    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "failed_announcement_ids": failed_announcement_ids,
        "detail_success_count": detail_success_count,
        "detail_failure_count": detail_failure_count,
    }


# ──────────────────────────────────────────────────────────────
# argparse / CLI 진입점
# ──────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 생성한다."""
    root_parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="IRIS 사업공고 목록 수집 및 DB 적재 CLI",
    )
    subparsers = root_parser.add_subparsers(dest="subcommand", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="'접수중' 공고 목록을 수집하고 DB 에 적재한다.",
    )
    run_parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES_FLAG,
        help="list_scraper 가 순회할 최대 페이지 수. 0(기본) = 안전 상한 사용.",
    )
    run_parser.add_argument(
        "--max-announcements",
        type=int,
        default=DEFAULT_MAX_ANNOUNCEMENTS_FLAG,
        help="UPSERT 할 최대 공고 수. 0(기본) = 무제한.",
    )
    run_parser.add_argument(
        "--skip-detail",
        action="store_true",
        help="상세 페이지 수집을 건너뛴다(목록 적재만 수행).",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB 쓰기를 건너뛰고 수집만 검증한다.",
    )
    run_parser.add_argument(
        "--log-level",
        default=None,
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="이번 실행에만 적용할 로그 레벨(기본: settings.log_level).",
    )

    return root_parser


def _resolve_settings(log_level_override: Optional[str]) -> Settings:
    """현재 실행에 사용할 Settings 를 결정한다.

    --log-level 이 지정된 경우 Settings 의 log_level 을 in-place 로 덮어쓴다.
    원본 환경/.env 파일은 변경하지 않는다.
    """
    settings = get_settings()
    if log_level_override:
        settings.log_level = log_level_override.upper()
    return settings


async def _async_main(argv: list[str]) -> int:
    """async 진입점.

    Returns:
        프로세스 종료 코드. 0 이 정상.
    """
    parser = _build_arg_parser()
    parsed_args = parser.parse_args(argv)

    settings = _resolve_settings(parsed_args.log_level)
    configure_logging(settings)
    settings.ensure_runtime_paths()

    logger.info(
        "iris-scrape 실행 시작: max_pages={} max_announcements={} skip_detail={} dry_run={}",
        parsed_args.max_pages,
        parsed_args.max_announcements,
        parsed_args.skip_detail,
        parsed_args.dry_run,
    )

    try:
        summary = await _orchestrate(
            settings=settings,
            max_pages=parsed_args.max_pages,
            max_announcements=parsed_args.max_announcements,
            skip_detail=parsed_args.skip_detail,
            dry_run=parsed_args.dry_run,
        )
    except Exception as bootstrap_exc:
        logger.exception(
            "부트스트랩 실패 — 비정상 종료: ({}: {})",
            type(bootstrap_exc).__name__,
            bootstrap_exc,
        )
        return 1

    logger.info(
        "iris-scrape 실행 완료: 목록 성공 {}건 / 목록 실패 {}건 | 상세 성공 {}건 / 상세 실패 {}건",
        summary["success_count"],
        summary["failure_count"],
        summary["detail_success_count"],
        summary["detail_failure_count"],
    )
    if summary["failure_count"]:
        logger.warning("목록 실패 공고 ID 목록: {}", summary["failed_announcement_ids"])
    return 0


def main() -> None:
    """OS 진입점. asyncio.run 으로 _async_main 을 실행하고 sys.exit 한다.

    SIGINT(Ctrl+C) 를 받으면 종료 코드 130 으로 종료한다.
    """
    try:
        exit_code = asyncio.run(_async_main(sys.argv[1:]))
    except KeyboardInterrupt:
        logger.warning("사용자 중단(SIGINT) 감지 — 종료 코드 130")
        sys.exit(130)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()


__all__ = [
    "main",
    "DEFAULT_STATUS_LABEL",
]
