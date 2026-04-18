"""IRIS 스크래퍼 오케스트레이터 CLI.

이전 subtask 에서 구현된 list/detail 스크래퍼와 다운로더, repository 레이어를
한 흐름으로 묶어 실행한다. 호출 형태(가이드라인 준수):

    python -m app.cli run --status 접수중 --max-pages 5 --max-announcements 20

옵션:
    --status N           수집 대상 상태값(기본: '접수중'). 현재는 '접수중' 외의
                         값에 대해 list_scraper 가 별도 처리하지 않으므로,
                         '접수중' 이외의 값을 주면 경고만 남기고 진행한다.
    --max-pages N        list_scraper 가 순회할 최대 페이지 수.
    --max-announcements N
                         처리(상세/다운로드/UPSERT) 할 최대 공고 수.
                         기본값 0 = 무제한.
    --dry-run            DB 쓰기와 첨부 파일 저장을 모두 건너뛴다(읽기 전용 검증).
    --log-level LEVEL    로그 레벨 일회성 오버라이드(기본: settings.log_level).

흐름 (가이드라인의 (1)~(7) 단계):
    (1) init_db 로 스키마 보장 (dry-run 이어도 단순 create_all 호출)
    (2) Playwright 브라우저(상세/다운로드 공유용) 부팅
    (3) list_scraper.scrape_list 로 '접수중' 목록을 한 번에 수집
    (4) 각 공고에 대해:
        a. upsert_announcement 로 기본 메타 적재
        b. detail_scraper.scrape_detail 로 본문 메타+첨부 트리거 수집
        c. raw_metadata 를 포함해 다시 upsert_announcement (메타 보강)
        d. downloader.download_attachments_for_announcement 로 첨부 저장
        e. 각 첨부 결과를 upsert_attachment 로 적재
        예외는 공고 단위로 격리 — 한 공고의 실패가 전체 흐름을 중단시키지 않는다.

종료 코드:
    0  : 정상(처리한 공고 수가 0 건이어도 정상 종료)
    1  : 부트스트랩 단계(init_db / 브라우저 / 목록 수집) 자체가 실패한 경우
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
from app.db.repository import upsert_announcement, upsert_attachment
from app.db.session import session_scope
from app.logging_setup import configure_logging
from app.scraper.detail_scraper import scrape_detail
from app.scraper.downloader import download_attachments_for_announcement
from app.scraper.list_scraper import _open_browser_context, scrape_list

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# 가이드라인 기본 상태값. list_scraper 자체가 '접수중' 만 통과시키므로
# 다른 값이 들어오면 경고만 출력하고 그대로 진행한다.
DEFAULT_STATUS_LABEL: str = "접수중"

# CLI 가 인식하는 상태값 화이트리스트(검증/경고용). 실제 필터링은 list_scraper 의
# DOM 셀렉터 가 담당하므로, 이 값은 표면 검증과 로깅에만 사용한다.
ALLOWED_STATUS_LABELS: tuple[str, ...] = ("접수중", "접수예정", "마감")

# argparse 기본값. 0 은 '무제한' 으로 해석한다.
DEFAULT_MAX_PAGES_FLAG: int = 0
DEFAULT_MAX_ANNOUNCEMENTS_FLAG: int = 0

# list_scraper 의 안전 상한(임포트 시점이 아니라 호출 시점에 결정되도록 별도 상수로 보관).
LIST_SCRAPER_DEFAULT_MAX_PAGES_SAFE_CEILING: int = 50

# 날짜 텍스트 → datetime 변환 시 시도할 포맷 후보(앞부터 매칭).
_DATETIME_TEXT_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


# ──────────────────────────────────────────────────────────────
# 데이터 변환 헬퍼
# ──────────────────────────────────────────────────────────────


def _parse_datetime_text(value: Optional[str]) -> Optional[datetime]:
    """목록에서 추출된 날짜 텍스트를 timezone-aware UTC datetime 으로 변환한다.

    - 공백/빈 문자열/None 입력은 None 으로 반환한다.
    - 구분자 '/', '.' 는 '-' 로 정규화한 뒤 `%Y-%m-%d[ HH:MM[:SS]]` 포맷을 시도한다.
    - 어떤 포맷에도 매칭되지 않으면 경고 로그를 남기고 None 을 반환한다.

    IRIS 가 노출하는 시각은 KST(한국시간) 일 수 있으나 안전한 변환 정보가 없을 때
    임의 보정을 가하면 오차가 누적된다. 따라서 이 함수에서는 timezone 추정 없이
    UTC 로 보존한다(원문 텍스트는 raw_metadata 에 함께 저장된다).
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


def _build_initial_announcement_payload(
    row_metadata: dict[str, Any],
    status_label: str,
) -> dict[str, Any]:
    """list_scraper 의 row 메타를 repository.upsert_announcement payload 로 변환한다.

    이 단계에서는 raw_metadata 에 row 자체를 보존만 한다. 상세 페이지 메타는
    이후 `_build_enriched_announcement_payload` 에서 합쳐진다.
    """
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
                # row_html 은 디버깅용으로 분리 보관한다(JSON 컬럼 부피 최소화 목적).
                if key != "row_html"
            },
        },
    }


def _build_enriched_announcement_payload(
    row_metadata: dict[str, Any],
    detail_result: dict[str, Any],
    status_label: str,
) -> dict[str, Any]:
    """list row + scrape_detail 결과를 합쳐 보강된 upsert payload 를 만든다.

    raw_metadata 에는 list_row 와 detail.raw_metadata, attachments 트리거 정보를
    함께 보존한다(다음 분석/디버깅 단계가 원본 메타를 잃지 않도록).
    """
    base_payload = _build_initial_announcement_payload(row_metadata, status_label)

    detail_url_from_detail = detail_result.get("detail_url")
    if detail_url_from_detail:
        base_payload["detail_url"] = detail_url_from_detail

    base_payload["raw_metadata"] = {
        "list_row": base_payload["raw_metadata"]["list_row"],
        "detail": detail_result.get("raw_metadata") or {},
        # 실제 다운로드 결과는 별도 첨부 테이블로 들어가지만, 트리거 원본은
        # 재현 가능성을 위해 함께 보존한다(디스크/JSON 컬럼 양쪽).
        "attachments_trigger": [
            {
                "original_filename": attachment.get("original_filename"),
                "file_ext": attachment.get("file_ext"),
                "download_url": attachment.get("download_url"),
                "download_trigger": attachment.get("download_trigger"),
            }
            for attachment in (detail_result.get("attachments") or [])
        ],
    }
    return base_payload


# ──────────────────────────────────────────────────────────────
# 공고 단위 처리 (예외 격리의 단위)
# ──────────────────────────────────────────────────────────────


async def _process_single_announcement(
    row_metadata: dict[str, Any],
    *,
    page,
    settings: Settings,
    dry_run: bool,
    status_label: str,
) -> dict[str, Any]:
    """한 공고에 대해 (a)~(e) 단계를 수행하고 결과 통계를 반환한다.

    Args:
        row_metadata: list_scraper 가 만든 단일 row dict.
        page:         재사용할 Playwright Page (상세 진입 + 다운로드용).
        settings:     주입할 Settings.
        dry_run:      True 면 DB 쓰기와 파일 저장을 모두 건너뛴다.
        status_label: CLI 가 받은 --status 값(저장 시 fallback 으로 사용).

    Returns:
        통계 dict — `{
            "iris_announcement_id": str,
            "attachments_attempted": int,
            "attachments_downloaded": int,
            "attachments_persisted": int,
        }`

    Raises:
        Exception: 호출자가 격리 처리할 수 있도록 예외를 재던진다.
            (호출자: `_orchestrate` 의 per-row try/except 블록)
    """
    iris_announcement_id = row_metadata["iris_announcement_id"]
    log = logger.bind(iris_announcement_id=iris_announcement_id)
    log.info("공고 처리 시작: title={!r}", row_metadata.get("title"))

    # (a) 기본 메타 upsert.
    initial_payload = _build_initial_announcement_payload(row_metadata, status_label)
    if dry_run:
        log.info("[dry-run] upsert_announcement(skip) — 기본 메타")
    else:
        with session_scope() as session:
            upsert_announcement(session, initial_payload)

    # (b) 상세 메타 + 첨부 트리거 수집.
    detail_result = await scrape_detail(row_metadata, page=page, settings=settings)
    raw_metadata_pairs = detail_result.get("raw_metadata") or {}
    attachment_triggers = detail_result.get("attachments") or []
    log.info(
        "상세 수집 완료: meta_pair={} attachment_trigger={}",
        len(raw_metadata_pairs),
        len(attachment_triggers),
    )

    # (c) raw_metadata 를 포함해 메타 보강 upsert.
    enriched_payload = _build_enriched_announcement_payload(
        row_metadata,
        detail_result,
        status_label,
    )

    # (d) 첨부파일 다운로드 + (e) 각 첨부 upsert.
    persisted_attachment_count = 0
    downloaded_attachment_count = 0

    if dry_run:
        log.info("[dry-run] 메타 보강 upsert / 첨부 다운로드(skip)")
    else:
        # 보강된 메타와 첨부 적재는 동일 트랜잭션으로 묶는다(메타와 첨부의 정합성).
        download_payloads = await download_attachments_for_announcement(
            detail_result,
            page=page,
            settings=settings,
        )
        downloaded_attachment_count = len(download_payloads)

        with session_scope() as session:
            persisted_announcement = upsert_announcement(session, enriched_payload)
            for download_payload in download_payloads:
                # download_url 이 빠진 채 들어오는 경우(트리거 다운로드)도 있으므로,
                # 트리거의 download_url 은 None 그대로 받는다(repository 가 처리).
                upsert_attachment(
                    session,
                    persisted_announcement.id,
                    download_payload,
                )
                persisted_attachment_count += 1

    log.info(
        "공고 처리 완료: 첨부 시도 {} / 다운로드 {} / 적재 {}",
        len(attachment_triggers),
        downloaded_attachment_count,
        persisted_attachment_count,
    )
    return {
        "iris_announcement_id": iris_announcement_id,
        "attachments_attempted": len(attachment_triggers),
        "attachments_downloaded": downloaded_attachment_count,
        "attachments_persisted": persisted_attachment_count,
    }


# ──────────────────────────────────────────────────────────────
# 오케스트레이션 본체
# ──────────────────────────────────────────────────────────────


async def _orchestrate(
    *,
    settings: Settings,
    status_label: str,
    max_pages: int,
    max_announcements: int,
    dry_run: bool,
) -> dict[str, Any]:
    """전체 흐름((1)~(7))을 수행하고 최종 통계 dict 를 반환한다.

    이 함수는 부트스트랩 실패(목록 수집 자체가 안 되는 경우)를 제외하면 예외를
    삼키고 통계만 반환한다. main() 에서 종료 코드 결정의 근거가 된다.
    """
    if status_label != DEFAULT_STATUS_LABEL:
        # 현재 list_scraper 는 '접수중' 만 통과시키도록 구현되어 있다.
        # 사용자가 다른 값을 주면 명시적으로 경고를 남긴다.
        logger.warning(
            "지원되지 않는 status={!r} — 현재 list_scraper 는 '접수중' 만 수집한다.",
            status_label,
        )

    # (1) DB 스키마 보장. dry-run 이어도 호출(create_all 은 멱등이며, 후속 단계의
    # 동일 코드 경로를 검증할 수 있다).
    logger.info("DB 초기화(create_all) 시작 — dry_run={}", dry_run)
    init_db()

    # max_pages 0(무제한) → list_scraper 의 안전 상한을 그대로 사용.
    safe_max_pages = (
        max(int(max_pages), 1)
        if max_pages > 0
        else LIST_SCRAPER_DEFAULT_MAX_PAGES_SAFE_CEILING
    )

    # (3) 목록 수집. scrape_list 자체가 브라우저를 열고 닫는다.
    logger.info(
        "list_scraper.scrape_list 시작: max_pages={} (요청값={})",
        safe_max_pages,
        max_pages,
    )
    aggregated_rows = await scrape_list(
        settings=settings,
        max_pages=safe_max_pages,
    )
    logger.info("목록 수집 완료: 누적 {}건", len(aggregated_rows))

    if max_announcements > 0:
        target_rows = aggregated_rows[:max_announcements]
        logger.info(
            "처리 대상 제한: max_announcements={} → {}건만 처리",
            max_announcements,
            len(target_rows),
        )
    else:
        target_rows = aggregated_rows

    # 통계 누적
    success_count = 0
    failure_count = 0
    total_attachments_attempted = 0
    total_attachments_downloaded = 0
    total_attachments_persisted = 0
    failed_announcement_ids: list[str] = []

    if not target_rows:
        logger.warning("처리할 공고가 없어 종료한다.")
        return {
            "success_count": 0,
            "failure_count": 0,
            "total_attachments_attempted": 0,
            "total_attachments_downloaded": 0,
            "total_attachments_persisted": 0,
            "failed_announcement_ids": [],
        }

    # (2) 상세/다운로드용 단일 브라우저 컨텍스트.
    # list_scraper 와 별도로 여기서 한 번 더 연다(scrape_list 는 자체 컨텍스트를
    # 닫고 반환하므로 재사용이 불가능하다). 이렇게 해도 브라우저 부팅은 총 2회로
    # 끝나며, 공고 1건당 매번 새 브라우저를 띄우는 비용을 피한다.
    async with _open_browser_context(settings) as (_browser, _context, shared_page):
        for row_index, row_metadata in enumerate(target_rows, start=1):
            iris_announcement_id = row_metadata.get("iris_announcement_id") or "(unknown)"
            logger.info(
                "── [{}/{}] 공고 처리 진입: id={}",
                row_index,
                len(target_rows),
                iris_announcement_id,
            )

            try:
                stats = await _process_single_announcement(
                    row_metadata,
                    page=shared_page,
                    settings=settings,
                    dry_run=dry_run,
                    status_label=status_label,
                )
            except Exception as per_row_exc:
                # 공고 단위 격리 — 어떤 예외든 다음 공고로 넘어간다.
                failure_count += 1
                failed_announcement_ids.append(str(iris_announcement_id))
                logger.exception(
                    "공고 처리 실패(스킵): id={} ({}: {})",
                    iris_announcement_id,
                    type(per_row_exc).__name__,
                    per_row_exc,
                )
                continue

            success_count += 1
            total_attachments_attempted += stats["attachments_attempted"]
            total_attachments_downloaded += stats["attachments_downloaded"]
            total_attachments_persisted += stats["attachments_persisted"]

    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "total_attachments_attempted": total_attachments_attempted,
        "total_attachments_downloaded": total_attachments_downloaded,
        "total_attachments_persisted": total_attachments_persisted,
        "failed_announcement_ids": failed_announcement_ids,
    }


# ──────────────────────────────────────────────────────────────
# argparse / CLI 진입점
# ──────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 생성한다.

    서브커맨드 형태( `run` )를 사용해, 추후 `recheck` / `download-only` 등의
    서브커맨드 추가 여지를 둔다.
    """
    root_parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="IRIS 사업공고 스크래퍼 오케스트레이터",
    )
    subparsers = root_parser.add_subparsers(dest="subcommand", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="목록 → 상세 → 첨부 다운로드 → DB 적재 까지의 전체 흐름을 수행한다.",
    )
    run_parser.add_argument(
        "--status",
        default=DEFAULT_STATUS_LABEL,
        choices=ALLOWED_STATUS_LABELS,
        help=f"수집 대상 상태값 (기본: {DEFAULT_STATUS_LABEL}).",
    )
    run_parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES_FLAG,
        help=(
            "list_scraper 가 순회할 최대 페이지 수. "
            "0(기본) 이면 list_scraper 의 안전 상한을 사용한다."
        ),
    )
    run_parser.add_argument(
        "--max-announcements",
        type=int,
        default=DEFAULT_MAX_ANNOUNCEMENTS_FLAG,
        help=(
            "상세/다운로드/UPSERT 를 수행할 최대 공고 수. "
            "0(기본) 이면 무제한."
        ),
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB 쓰기와 파일 저장을 건너뛰고 수집만 검증한다.",
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

    --log-level 이 지정된 경우, 캐시된 Settings 의 log_level 을 그 자리에서 덮어쓴다.
    원본 환경/.env 파일은 변경하지 않는다.
    """
    settings = get_settings()
    if log_level_override:
        # pydantic v2 에서는 model_copy 로 안전하게 복제한 뒤 lru_cache 를 우회한다.
        # 다만 다른 모듈이 이미 get_settings() 캐시를 잡고 있으므로, 단순 setattr 로
        # in-place 수정한다(테스트는 get_settings.cache_clear() 로 격리한다).
        settings.log_level = log_level_override.upper()
    return settings


async def _async_main(argv: list[str]) -> int:
    """async 진입점. main() 이 asyncio.run 으로 감싼다.

    Returns:
        프로세스 종료 코드. 0 이 정상.
    """
    parser = _build_arg_parser()
    parsed_args = parser.parse_args(argv)

    settings = _resolve_settings(parsed_args.log_level)
    configure_logging(settings)
    settings.ensure_runtime_paths()

    logger.info(
        "iris-scrape 실행 시작: status={} max_pages={} max_announcements={} dry_run={}",
        parsed_args.status,
        parsed_args.max_pages,
        parsed_args.max_announcements,
        parsed_args.dry_run,
    )

    try:
        summary = await _orchestrate(
            settings=settings,
            status_label=parsed_args.status,
            max_pages=parsed_args.max_pages,
            max_announcements=parsed_args.max_announcements,
            dry_run=parsed_args.dry_run,
        )
    except Exception as bootstrap_exc:
        # 부트스트랩 단계(목록 수집/브라우저/init_db) 자체가 실패하면 종료 코드 1.
        logger.exception(
            "부트스트랩 실패 — 비정상 종료: ({}: {})",
            type(bootstrap_exc).__name__,
            bootstrap_exc,
        )
        return 1

    logger.info(
        "iris-scrape 실행 완료: 성공 {}건 / 실패 {}건 / 첨부(시도/다운로드/적재) {}/{}/{}",
        summary["success_count"],
        summary["failure_count"],
        summary["total_attachments_attempted"],
        summary["total_attachments_downloaded"],
        summary["total_attachments_persisted"],
    )
    if summary["failure_count"]:
        logger.warning(
            "실패 공고 ID 목록: {}",
            summary["failed_announcement_ids"],
        )
    return 0


def main() -> None:
    """OS 진입점. asyncio.run 으로 _async_main 을 실행하고 sys.exit 한다.

    SIGINT(Ctrl+C) 를 받으면 종료 코드 130 으로 종료한다 — 셸 관례.
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
    "ALLOWED_STATUS_LABELS",
]
