"""사업공고 스크래퍼 오케스트레이터 CLI.

sources.yaml 에 정의된 활성 소스를 순회하여 공고 목록·상세를 수집하고 DB 에 적재한다.
각 소스는 `app/scraper/registry.get_adapter()` 가 반환하는 어댑터로 실행된다.

현재 구현 상태:
    - IRIS: 완전 구현 (list + detail 수집)
    - NTIS: stub (경고 로그 + 빈 결과 반환)

활성화된 흐름:
    (1) init_db 로 스키마 보장
    (2) sources.yaml 에서 활성 소스 목록 로드
    (3) 소스별로 어댑터를 생성하고 목록·상세 수집 후 DB 적재
    (4) 소스 단위 예외 격리 — 한 소스 실패가 다른 소스를 중단시키지 않는다

비활성화(첨부파일 다운로드는 별도 subtask에서 활성화 예정):
    - 첨부파일 다운로드

호출 형태:
    python -m app.cli run [--max-pages N] [--max-announcements N]
                          [--skip-detail] [--dry-run] [--log-level LEVEL]
                          [--source SOURCE_ID]

옵션:
    --max-pages N          각 소스에서 순회할 최대 페이지 수. 0(기본) = 안전 상한 사용.
    --max-announcements N  소스당 UPSERT 할 최대 공고 수. 0(기본) = 무제한.
    --skip-detail          상세 페이지 수집을 건너뛴다(목록 적재만 수행).
    --dry-run              DB 쓰기를 건너뛰고 수집만 검증한다.
    --log-level LEVEL      로그 레벨 일회성 오버라이드(기본: settings.log_level).
    --source SOURCE_ID     sources.yaml 의 enabled 설정을 무시하고 지정 소스만 실행.
                           예: --source NTIS (stub 동작 확인용)

종료 코드:
    0  : 정상(처리한 공고 수가 0 건이어도 정상 종료)
    1  : 부트스트랩 단계(init_db) 자체가 실패한 경우
    130: SIGINT (Ctrl+C) 로 중단된 경우
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from app.config import Settings, get_settings
from app.db.init_db import init_db
from app.db.repository import upsert_announcement, upsert_announcement_detail
from app.db.session import session_scope
from app.logging_setup import configure_logging
from app.scraper.base import BaseSourceAdapter
from app.scraper.registry import get_adapter
from app.sources.config_schema import SourceConfig, load_sources_config
from app.sources.constants import SOURCE_TYPE_IRIS

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

DEFAULT_STATUS_LABEL: str = "접수중"

# argparse 기본값. 0 은 '무제한' 으로 해석한다.
DEFAULT_MAX_PAGES_FLAG: int = 0
DEFAULT_MAX_ANNOUNCEMENTS_FLAG: int = 0

# 안전 상한: 소스당 최대 페이지 수 (max_pages=0 일 때 적용)
MAX_PAGES_SAFE_CEILING: int = 50

# 날짜 텍스트 → datetime 변환 시 시도할 포맷 후보.
_DATETIME_TEXT_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y.%m.%d",
)


# ──────────────────────────────────────────────────────────────
# 데이터 변환 헬퍼
# ──────────────────────────────────────────────────────────────


def _parse_datetime_text(value: Optional[str]) -> Optional[datetime]:
    """날짜 텍스트를 timezone-aware UTC datetime 으로 변환한다.

    구분자 '/', '.' 는 '-' 로 정규화한 뒤 포맷을 순차 시도한다.
    매칭되지 않으면 경고 로그를 남기고 None 을 반환한다.
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


def _build_announcement_payload(row_metadata: dict[str, Any]) -> dict[str, Any]:
    """어댑터가 반환한 row 메타를 repository.upsert_announcement payload 로 변환한다.

    어댑터는 source_announcement_id / source_type 키를 이미 정규화하여 반환한다.
    """
    return {
        "source_announcement_id": row_metadata["source_announcement_id"],
        "source_type": row_metadata["source_type"],
        "title": row_metadata.get("title") or "(제목 미상)",
        "agency": row_metadata.get("agency"),
        "status": row_metadata.get("status") or DEFAULT_STATUS_LABEL,
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
# 소스별 수집 실행
# ──────────────────────────────────────────────────────────────


async def _run_source_announcements(
    *,
    adapter: BaseSourceAdapter,
    settings: Settings,
    max_pages: int,
    max_announcements: int,
    skip_detail: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """단일 소스 어댑터로 목록·상세를 수집하고 DB 에 적재한다.

    공고 단위 예외는 격리 — 한 공고 실패가 같은 소스의 다음 공고를 중단시키지 않는다.

    Args:
        adapter:           이미 열린(open) 소스 어댑터.
        settings:          전역 설정 (request_delay_sec 등).
        max_pages:         목록 페이지 순회 상한.
        max_announcements: 소스당 최대 처리 공고 수. 0 이면 무제한.
        skip_detail:       True 면 상세 수집을 건너뛴다.
        dry_run:           True 면 DB 쓰기를 건너뛴다.

    Returns:
        {success_count, failure_count, failed_announcement_ids,
         detail_success_count, detail_failure_count}
    """
    source_type = adapter.source_type

    # 목록 수집
    logger.info("목록 수집 시작: source={} max_pages={}", source_type, max_pages)
    aggregated_rows = await adapter.scrape_list(max_pages=max_pages)
    logger.info("목록 수집 완료: source={} {}건", source_type, len(aggregated_rows))

    if max_announcements > 0:
        target_rows = aggregated_rows[:max_announcements]
        logger.info(
            "처리 대상 제한: source={} max_announcements={} → {}건만 처리",
            source_type, max_announcements, len(target_rows),
        )
    else:
        target_rows = aggregated_rows

    if not target_rows:
        logger.warning("처리할 공고가 없음: source={}", source_type)
        return {
            "success_count": 0,
            "failure_count": 0,
            "failed_announcement_ids": [],
            "detail_success_count": 0,
            "detail_failure_count": 0,
        }

    success_count = 0
    failure_count = 0
    failed_announcement_ids: list[str] = []
    detail_success_count = 0
    detail_failure_count = 0

    for row_index, row_metadata in enumerate(target_rows, start=1):
        source_announcement_id = row_metadata.get("source_announcement_id") or "(unknown)"
        detail_url: Optional[str] = row_metadata.get("detail_url")

        logger.info(
            "── [{}/{}] 공고 upsert: source={} id={}",
            row_index, len(target_rows), source_type, source_announcement_id,
        )

        # (1) 목록 UPSERT
        try:
            payload = _build_announcement_payload(row_metadata)
            if dry_run:
                logger.info(
                    "[dry-run] upsert_announcement(skip): source={} id={}",
                    source_type, source_announcement_id,
                )
            else:
                with session_scope() as session:
                    upsert_announcement(session, payload)
            success_count += 1
        except Exception as exc:
            failure_count += 1
            failed_announcement_ids.append(str(source_announcement_id))
            logger.exception(
                "공고 upsert 실패(스킵): source={} id={} ({}: {})",
                source_type, source_announcement_id, type(exc).__name__, exc,
            )
            continue

        # (2) 상세 수집
        if skip_detail or dry_run:
            continue

        if not detail_url:
            logger.warning("detail_url 없음 — 상세 수집 스킵: source={} id={}", source_type, source_announcement_id)
            detail_failure_count += 1
            continue

        # 공고 간 요청 지연 (첫 번째 공고는 건너뜀)
        if row_index > 1:
            await asyncio.sleep(settings.request_delay_sec)

        logger.info("상세 수집 시작: source={} id={} url={}", source_type, source_announcement_id, detail_url)
        detail_result = await adapter.scrape_detail(detail_url)

        try:
            with session_scope() as session:
                upsert_announcement_detail(
                    session,
                    source_announcement_id,
                    detail_result,
                    source_type=source_type,
                )
        except Exception as exc:
            logger.exception(
                "상세 DB 갱신 실패: source={} id={} ({}: {})",
                source_type, source_announcement_id, type(exc).__name__, exc,
            )
            detail_failure_count += 1
            continue

        fetch_status = detail_result.get("detail_fetch_status", "error")
        if fetch_status == "ok":
            detail_success_count += 1
            logger.info("상세 수집 완료(ok): source={} id={}", source_type, source_announcement_id)
        else:
            detail_failure_count += 1
            logger.warning("상세 수집 결과 '{}': source={} id={}", fetch_status, source_type, source_announcement_id)

    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "failed_announcement_ids": failed_announcement_ids,
        "detail_success_count": detail_success_count,
        "detail_failure_count": detail_failure_count,
    }


# ──────────────────────────────────────────────────────────────
# 오케스트레이션
# ──────────────────────────────────────────────────────────────


def _resolve_active_sources(
    source_override: Optional[str],
    settings: Settings,
) -> list[SourceConfig]:
    """실행할 소스 목록을 결정한다.

    - source_override 가 지정되면 해당 소스만 실행 (enabled 설정 무시).
    - 지정하지 않으면 sources.yaml 의 enabled=true 소스를 실행한다.
    - sources.yaml 이 없거나 활성 소스가 없으면 IRIS 를 fallback 으로 사용한다.
    """
    sources_cfg = load_sources_config()

    if source_override:
        # yaml 에서 해당 소스를 찾고, 없으면 최소 설정으로 생성
        source_config = sources_cfg.get_source(source_override)
        if source_config is None:
            logger.warning(
                "sources.yaml 에 '{}' 소스가 없습니다. base_url=settings.base_url 로 임시 생성합니다.",
                source_override,
            )
            source_config = SourceConfig(id=source_override, base_url=settings.base_url)
        return [source_config]

    active_sources = sources_cfg.get_enabled_sources()
    if not active_sources:
        logger.warning(
            "sources.yaml 에 활성 소스가 없습니다. IRIS fallback 을 사용합니다."
        )
        fallback = sources_cfg.get_source(SOURCE_TYPE_IRIS)
        if fallback is None:
            fallback = SourceConfig(id=SOURCE_TYPE_IRIS, base_url=settings.base_url)
        return [fallback]

    return active_sources


async def _orchestrate(
    *,
    settings: Settings,
    max_pages: int,
    max_announcements: int,
    skip_detail: bool,
    dry_run: bool,
    source_override: Optional[str],
) -> dict[str, Any]:
    """소스 목록 → 소스별 공고 수집 → DB 적재 흐름을 수행하고 통계 dict 를 반환한다.

    부트스트랩 실패(init_db)는 호출자로 전파한다.
    소스 단위 예외는 격리 — 한 소스 실패가 다른 소스를 중단시키지 않는다.
    """
    # (1) DB 스키마 보장
    logger.info("DB 초기화 시작 — dry_run={}", dry_run)
    init_db()

    safe_max_pages = (
        max(int(max_pages), 1)
        if max_pages > 0
        else MAX_PAGES_SAFE_CEILING
    )

    # (2) 활성 소스 목록 결정
    active_sources = _resolve_active_sources(source_override, settings)
    logger.info("활성 소스: {}", [s.id for s in active_sources])

    # (3) 소스별 실행 및 통계 집계
    total_success = 0
    total_failure = 0
    total_failed_ids: list[str] = []
    total_detail_success = 0
    total_detail_failure = 0

    for source_config in active_sources:
        logger.info("── 소스 {} 수집 시작", source_config.id)
        try:
            adapter = get_adapter(source_config, settings)
            async with adapter:
                source_stats = await _run_source_announcements(
                    adapter=adapter,
                    settings=settings,
                    max_pages=safe_max_pages,
                    max_announcements=max_announcements,
                    skip_detail=skip_detail,
                    dry_run=dry_run,
                )
        except Exception as exc:
            logger.exception(
                "소스 {} 수집 실패 — 다음 소스로 계속: ({}: {})",
                source_config.id, type(exc).__name__, exc,
            )
            continue

        total_success += source_stats["success_count"]
        total_failure += source_stats["failure_count"]
        total_failed_ids.extend(source_stats["failed_announcement_ids"])
        total_detail_success += source_stats["detail_success_count"]
        total_detail_failure += source_stats["detail_failure_count"]

        logger.info(
            "소스 {} 완료: 목록 성공 {}건 / 실패 {}건 | 상세 성공 {}건 / 실패 {}건",
            source_config.id,
            source_stats["success_count"], source_stats["failure_count"],
            source_stats["detail_success_count"], source_stats["detail_failure_count"],
        )

    return {
        "success_count": total_success,
        "failure_count": total_failure,
        "failed_announcement_ids": total_failed_ids,
        "detail_success_count": total_detail_success,
        "detail_failure_count": total_detail_failure,
    }


# ──────────────────────────────────────────────────────────────
# argparse / CLI 진입점
# ──────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 생성한다."""
    root_parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="사업공고 목록 수집 및 DB 적재 CLI",
    )
    subparsers = root_parser.add_subparsers(dest="subcommand", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="공고 목록을 수집하고 DB 에 적재한다.",
    )
    run_parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES_FLAG,
        help="소스당 목록 순회 최대 페이지 수. 0(기본) = 안전 상한 사용.",
    )
    run_parser.add_argument(
        "--max-announcements",
        type=int,
        default=DEFAULT_MAX_ANNOUNCEMENTS_FLAG,
        help="소스당 UPSERT 최대 공고 수. 0(기본) = 무제한.",
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
    run_parser.add_argument(
        "--source",
        default=None,
        metavar="SOURCE_ID",
        help=(
            "sources.yaml 의 enabled 설정을 무시하고 지정 소스만 실행한다. "
            "예: --source IRIS, --source NTIS (stub 동작 확인용)"
        ),
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
        "scrape 실행 시작: max_pages={} max_announcements={} "
        "skip_detail={} dry_run={} source={}",
        parsed_args.max_pages,
        parsed_args.max_announcements,
        parsed_args.skip_detail,
        parsed_args.dry_run,
        parsed_args.source or "(sources.yaml 활성 소스)",
    )

    try:
        summary = await _orchestrate(
            settings=settings,
            max_pages=parsed_args.max_pages,
            max_announcements=parsed_args.max_announcements,
            skip_detail=parsed_args.skip_detail,
            dry_run=parsed_args.dry_run,
            source_override=parsed_args.source,
        )
    except Exception as bootstrap_exc:
        logger.exception(
            "부트스트랩 실패 — 비정상 종료: ({}: {})",
            type(bootstrap_exc).__name__,
            bootstrap_exc,
        )
        return 1

    logger.info(
        "scrape 실행 완료: 목록 성공 {}건 / 목록 실패 {}건 | 상세 성공 {}건 / 상세 실패 {}건",
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
