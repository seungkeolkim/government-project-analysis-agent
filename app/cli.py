"""사업공고 스크래퍼 오케스트레이터.

sources.yaml 의 scrape 섹션과 sources 목록을 읽어 공고 수집을 실행한다.
모든 실행 파라미터는 sources.yaml 과 .env 를 통해 제어하며, CLI 인자는 사용하지 않는다.

현재 구현 상태:
    - IRIS: 완전 구현 (list + detail + attachment 수집). 접수예정·접수중·마감 3개 상태 수집.
    - NTIS: stub (경고 로그 + 빈 결과 반환)

증분 수집 전략 (UpsertResult 기반):
    목록 UPSERT 후 `UpsertResult.needs_detail_scraping` 값으로 상세 수집 여부를 결정한다.
    - "created" / "new_version" / "status_transitioned": 항상 상세 수집
    - "unchanged" + detail 이미 있음: 상세 수집 생략 (기존 데이터 재사용)
    - "unchanged" + detail 없음: 상세 수집 진행

    첨부파일 수집은 상세 수집이 완료(또는 기존 detail_html 있음)된 공고에 대해 실행한다.
    이미 다운로드된 파일(로컬 FS 기준)은 sha256 만 계산하여 반환하고 재다운로드하지 않는다.

상태 전이 동작:
    동일 공고가 접수예정→접수중→마감 으로 상태가 바뀌면 status_transitioned 분기가 발동한다.
    상태만 변경된 경우 in-place UPDATE, title/deadline_at/agency 도 함께 바뀐 경우 이력 보존
    (기존 row 봉인 + 신규 INSERT). docs/status_transition_todo.md 참고.

실행 설정 (sources.yaml 의 scrape: 섹션):
    active_sources: 실행할 소스 ID 목록. 비어 있으면 enabled=true 소스 전체 실행.
    max_pages:       소스당 최대 페이지 수 (null → 소스별 설정 → 코드 default 10).
    max_announcements: 소스당 최대 공고 수 (null → 소스별 설정 → 코드 default 200).
    skip_detail:     True 면 상세 수집 건너뜀.
    skip_attachments: True 면 첨부파일 다운로드 건너뜀.
    dry_run:         True 면 DB 쓰기 건너뜀.
    log_level:       로그 레벨 오버라이드 (null → .env LOG_LEVEL).

실행 형태:
    docker compose --profile scrape run --rm scraper

우선순위:
    sources.yaml scrape 섹션 > sources.yaml 소스별 설정 > 코드 내부 default

종료 코드:
    0  : 정상(처리한 공고 수가 0 건이어도 정상 종료)
    1  : 부트스트랩 단계(init_db) 자체가 실패한 경우
    130: SIGINT (Ctrl+C) 로 중단된 경우
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from app.config import Settings, get_settings
from app.db.init_db import init_db
from app.db.repository import (
    UpsertResult,
    get_announcement_by_id,
    recompute_canonical_with_ancm_no,
    upsert_announcement,
    upsert_announcement_detail,
    upsert_attachment,
)
from app.db.session import session_scope
from app.logging_setup import configure_logging
from app.scraper.attachment_downloader import scrape_attachments_for_announcement
from app.scraper.base import BaseSourceAdapter
from app.scraper.registry import get_adapter
from app.sources.config_schema import (
    ScrapeRunConfig,
    SourceConfig,
    SourcesConfig,
    load_sources_config,
)

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# 어댑터가 row['status'] 를 채우지 않은 비정상 케이스 대비 fallback. 정상 경로에서는 발생하지 않는다.
DEFAULT_STATUS_LABEL: str = "접수중"

# 코드 내부 default (CLI·sources.yaml 둘 다 없을 때 사용)
CODE_DEFAULT_MAX_PAGES: int = 10
CODE_DEFAULT_MAX_ANNOUNCEMENTS: int = 200

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

    ancm_no: IRIS 는 목록 단계에서 공식 공고번호(ancmNo)를 row 에 담아 반환한다.
             NTIS 는 목록 단계에서 알 수 없으므로 None 이다(상세 수집 후 subtask 8 에서 재계산).
             두 경우 모두 이 함수가 소스 무관하게 패스스루하여 repository 로 전달한다.
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
        # repository._apply_canonical 이 canonical_key 계산에 사용하는 공식 공고번호.
        # 소스 무관하게 row_metadata 에서 꺼내 패스스루한다.
        "ancm_no": row_metadata.get("ancm_no"),
        "raw_metadata": {
            "list_row": {
                key: value
                for key, value in row_metadata.items()
                if key != "row_html"
            },
        },
    }


# ──────────────────────────────────────────────────────────────
# 첨부파일 수집 헬퍼
# ──────────────────────────────────────────────────────────────


async def _scrape_and_store_attachments(
    *,
    announcement_id: int,
    source_type: str,
    source_announcement_id: str,
    settings: Settings,
    adapter: Optional[BaseSourceAdapter] = None,
) -> tuple[int, int, int]:
    """공고 한 건의 첨부파일을 수집하여 DB 에 저장하고 (성공, 실패, 스킵) 카운트를 반환한다.

    단계:
        1. 새 세션에서 공고를 조회하고 ORM 인스턴스를 세션에서 분리(expunge)한다.
           분리 후에도 단순 컬럼(detail_html, id 등)은 계속 접근 가능하다.
        2. 세션 바깥에서 비동기 다운로드를 실행한다.
        3. 새 세션에서 성공 항목을 upsert_attachment 로 저장하고,
           오류 항목은 공고의 raw_metadata.attachment_errors 에 누적한다.

    공고 단위 예외는 호출자(_run_source_announcements)에서 격리한다.

    Args:
        announcement_id:          공고 내부 PK.
        source_type:              로그 컨텍스트용 소스 유형.
        source_announcement_id:   로그 컨텍스트용 소스 공고 ID.
        settings:                 전역 설정.

    Returns:
        (attachment_success_count, attachment_failure_count, attachment_skipped_count)
    """
    # 1. 최신 공고 조회 후 세션에서 분리 — detail_html 이 포함된 상태여야 한다.
    announcement_for_scraping = None
    with session_scope() as session:
        fresh_ann = get_announcement_by_id(session, announcement_id)
        if not fresh_ann or not fresh_ann.detail_html:
            logger.debug(
                "첨부파일 수집 건너뜀: detail_html 없음 — announcement_id={}",
                announcement_id,
            )
            return 0, 0, 0
        # 세션에서 분리하여 비동기 컨텍스트에서 안전하게 사용
        session.expunge(fresh_ann)
        announcement_for_scraping = fresh_ann

    # 2. 첨부파일 다운로드 (비동기 네트워크 I/O — 세션 바깥에서 실행)
    att_result = await scrape_attachments_for_announcement(
        announcement_for_scraping,
        settings=settings,
        adapter=adapter,
    )

    success_count = 0
    failure_count = 0
    skipped_count = 0

    # 3. 수집 결과를 DB 에 저장 (성공·오류 둘 다 없으면 세션 생략)
    if att_result.success_entries or att_result.error_entries:
        with session_scope() as session:
            # 성공 항목: upsert_attachment (sha256 기반 중복 방지)
            for entry in att_result.success_entries:
                _, was_upserted = upsert_attachment(session, entry)
                if was_upserted:
                    success_count += 1
                else:
                    # sha256 동일 → 이미 최신 상태, DB 변경 없음
                    skipped_count += 1

            # 오류 항목: raw_metadata.attachment_errors 에 누적 (기존 오류와 병합)
            if att_result.error_entries:
                ann = get_announcement_by_id(session, announcement_id)
                if ann is not None:
                    existing_raw = dict(ann.raw_metadata or {})
                    existing_errors: list[Any] = existing_raw.get("attachment_errors", [])
                    existing_raw["attachment_errors"] = existing_errors + att_result.error_entries
                    ann.raw_metadata = existing_raw
                failure_count += len(att_result.error_entries)

    logger.info(
        "첨부파일 수집 완료: source={} id={} 성공={} 스킵={} 실패={}",
        source_type, source_announcement_id,
        success_count, skipped_count, failure_count,
    )

    return success_count, failure_count, skipped_count


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
    skip_attachments: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """단일 소스 어댑터로 목록·상세·첨부를 증분 수집하고 DB 에 적재한다.

    수집 순서: 목록 UPSERT → (조건부) 상세 수집 → (조건부) 첨부파일 수집.

    UpsertResult.needs_detail_scraping 을 기반으로 상세 수집 여부를 결정한다.
    - needs_detail_scraping=False: 비교 필드 변경 없고 기존 상세 데이터 있음 → 상세 생략
    - needs_detail_scraping=True: 신규·변경·상태전이·상세 미수집 → 상세 수집

    첨부파일 수집 조건: detail_html 이 DB 에 존재하고 skip_attachments=False 인 경우.
    공고 단위 예외는 격리 — 한 공고 실패가 같은 소스의 다음 공고를 중단시키지 않는다.
    첨부파일 수집 실패는 공고 목록·상세 UPSERT 성공에 영향을 주지 않는다.

    Args:
        adapter:           이미 열린(open) 소스 어댑터.
        settings:          전역 설정 (request_delay_sec 등).
        max_pages:         목록 페이지 순회 상한 (양의 정수).
        max_announcements: 소스당 최대 처리 공고 수 (양의 정수).
        skip_detail:       True 면 모든 상세 수집을 건너뛴다 (sources.yaml scrape.skip_detail).
        skip_attachments:  True 면 첨부파일 다운로드를 건너뛴다 (sources.yaml scrape.skip_attachments).
        dry_run:           True 면 DB 쓰기를 건너뛴다.

    Returns:
        {success_count, failure_count, failed_announcement_ids,
         detail_success_count, detail_failure_count, skipped_detail_count,
         attachment_success_count, attachment_failure_count, attachment_skipped_count}
    """
    source_type = adapter.source_type

    # 목록 수집
    logger.info("목록 수집 시작: source={} max_pages={}", source_type, max_pages)
    aggregated_rows = await adapter.scrape_list(max_pages=max_pages)
    logger.info("목록 수집 완료: source={} {}건", source_type, len(aggregated_rows))

    target_rows = aggregated_rows[:max_announcements]
    if len(target_rows) < len(aggregated_rows):
        logger.info(
            "처리 대상 제한: source={} max_announcements={} → {}건만 처리",
            source_type, max_announcements, len(target_rows),
        )

    if not target_rows:
        logger.warning("처리할 공고가 없음: source={}", source_type)
        return {
            "success_count": 0,
            "failure_count": 0,
            "failed_announcement_ids": [],
            "detail_success_count": 0,
            "detail_failure_count": 0,
            "skipped_detail_count": 0,
            "action_counts": {},
            "attachment_success_count": 0,
            "attachment_failure_count": 0,
            "attachment_skipped_count": 0,
        }

    success_count = 0
    failure_count = 0
    failed_announcement_ids: list[str] = []
    detail_success_count = 0
    detail_failure_count = 0
    # 변경 없음 + 기존 상세 있음 → 상세 수집을 생략한 건수
    skipped_detail_count = 0
    # upsert action 별 건수 (created / unchanged / new_version / status_transitioned)
    action_counts: dict[str, int] = {}
    # 첨부파일 카운터
    attachment_success_count = 0
    attachment_failure_count = 0
    attachment_skipped_count = 0

    # 상세 수집 실제 요청 순서 인덱스 (지연 계산용)
    detail_request_index = 0

    for row_index, row_metadata in enumerate(target_rows, start=1):
        source_announcement_id = row_metadata.get("source_announcement_id") or "(unknown)"
        detail_url: Optional[str] = row_metadata.get("detail_url")

        logger.info(
            "── [{}/{}] 공고 처리: source={} id={}",
            row_index, len(target_rows), source_type, source_announcement_id,
        )

        # ── (1) 목록 UPSERT ──────────────────────────────────────────────────
        upsert_result: Optional[UpsertResult] = None
        try:
            payload = _build_announcement_payload(row_metadata)
            if dry_run:
                logger.info(
                    "[dry-run] upsert_announcement(skip): source={} id={}",
                    source_type, source_announcement_id,
                )
            else:
                with session_scope() as session:
                    upsert_result = upsert_announcement(session, payload)
                _log_upsert_action(upsert_result, source_type, source_announcement_id)
                # action 분포 집계
                action_counts[upsert_result.action] = action_counts.get(upsert_result.action, 0) + 1
            success_count += 1
        except Exception as exc:
            failure_count += 1
            failed_announcement_ids.append(str(source_announcement_id))
            logger.exception(
                "공고 upsert 실패(스킵): source={} id={} ({}: {})",
                source_type, source_announcement_id, type(exc).__name__, exc,
            )
            # UPSERT 실패 시 상세·첨부 단계도 스킵
            continue

        # ── (2) 상세 수집 ────────────────────────────────────────────────────
        # announcement_has_detail: 이번 루프 종료 시점에 DB 에 detail_html 이 있으면 True.
        # - 방금 수집 성공 했거나
        # - unchanged 이고 기존 detail_html 이 있거나
        announcement_has_detail = False

        if skip_detail or dry_run:
            # skip_detail: 목록 적재만 요청됨. dry_run: DB 쓰기 없음.
            # 두 경우 모두 상세·첨부 수집 없이 다음 공고로.
            pass

        elif upsert_result is not None and not upsert_result.needs_detail_scraping:
            # 변경 없음 + 기존 상세 데이터 있음 → 상세 수집 생략
            # detail_html 이 DB 에 이미 있으므로 첨부 수집은 가능하다.
            skipped_detail_count += 1
            announcement_has_detail = True
            logger.info(
                "상세 수집 생략(변경 없음, 기존 데이터 재사용): source={} id={}",
                source_type, source_announcement_id,
            )

        elif not detail_url:
            logger.warning(
                "detail_url 없음 — 상세 수집 스킵: source={} id={}",
                source_type, source_announcement_id,
            )
            detail_failure_count += 1
            # detail_url 없으면 detail_html 도 없으므로 첨부도 불가.

        else:
            # ── 상세 수집 실행 ────────────────────────────────────────────────
            # 공고 간 요청 지연 (첫 번째 상세 요청은 건너뜀)
            if detail_request_index > 0:
                await asyncio.sleep(settings.request_delay_sec)
            detail_request_index += 1

            logger.info(
                "상세 수집 시작: source={} id={} url={}",
                source_type, source_announcement_id, detail_url,
            )
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
                # 상세 저장 실패 → detail_html 미보장 → 첨부 스킵
            else:
                fetch_status = detail_result.get("detail_fetch_status", "error")
                if fetch_status == "ok":
                    detail_success_count += 1
                    announcement_has_detail = True
                    logger.info(
                        "상세 수집 완료(ok): source={} id={}",
                        source_type, source_announcement_id,
                    )
                    # NTIS 전용: 상세 파싱에서 공식 공고번호가 확보된 경우 canonical 재계산.
                    # 목록 단계에서는 공고번호를 알 수 없어 fuzzy canonical 이 부여되었으므로
                    # 여기서 official key 로 승급하여 cross-source 매칭 정확도를 높인다.
                    ntis_ancm_no: Optional[str] = detail_result.get("ntis_ancm_no")
                    if ntis_ancm_no:
                        try:
                            with session_scope() as session:
                                recomputed = recompute_canonical_with_ancm_no(
                                    session,
                                    source_announcement_id,
                                    source_type=source_type,
                                    ancm_no=ntis_ancm_no,
                                )
                            if recomputed:
                                logger.info(
                                    "canonical 재계산 완료(fuzzy→official): "
                                    "source={} id={} ancm_no={}",
                                    source_type, source_announcement_id, ntis_ancm_no,
                                )
                        except Exception as exc:
                            logger.warning(
                                "canonical 재계산 실패(스킵, 공고 수집은 유지됨): "
                                "source={} id={} ({}: {})",
                                source_type, source_announcement_id, type(exc).__name__, exc,
                            )
                else:
                    detail_failure_count += 1
                    logger.warning(
                        "상세 수집 결과 '{}': source={} id={}",
                        fetch_status, source_type, source_announcement_id,
                    )

        # ── (3) 첨부파일 수집 ────────────────────────────────────────────────
        # 조건: detail_html 이 DB 에 있고, 첨부 수집 건너뜀 플래그가 없고, dry_run 도 아닐 것.
        # upsert_result 가 None 인 경우는 dry_run=True 에서만 발생하며 위에서 이미 pass.
        if (
            not skip_attachments
            and not dry_run
            and announcement_has_detail
            and upsert_result is not None
        ):
            try:
                att_success, att_failure, att_skipped = await _scrape_and_store_attachments(
                    announcement_id=upsert_result.announcement.id,
                    source_type=source_type,
                    source_announcement_id=source_announcement_id,
                    settings=settings,
                    adapter=adapter,
                )
                attachment_success_count += att_success
                attachment_failure_count += att_failure
                attachment_skipped_count += att_skipped
            except Exception as exc:
                logger.exception(
                    "첨부파일 수집 실패(스킵, 공고 upsert 성공은 유지됨): "
                    "source={} id={} ({}: {})",
                    source_type, source_announcement_id, type(exc).__name__, exc,
                )
                # 첨부 실패는 공고 성공 카운터에 영향 없음
                attachment_failure_count += 1

    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "failed_announcement_ids": failed_announcement_ids,
        "detail_success_count": detail_success_count,
        "detail_failure_count": detail_failure_count,
        "skipped_detail_count": skipped_detail_count,
        "action_counts": action_counts,
        "attachment_success_count": attachment_success_count,
        "attachment_failure_count": attachment_failure_count,
        "attachment_skipped_count": attachment_skipped_count,
    }


def _log_upsert_action(
    result: UpsertResult,
    source_type: str,
    source_announcement_id: str,
) -> None:
    """UpsertResult.action 에 따라 적절한 로그를 남긴다.

    status_transitioned 는 접수예정→접수중→마감 전이가 감지된 정상 경로이며 INFO 로 기록한다.
    title/deadline_at/agency 도 함께 바뀐 경우에는 new_version 분기(INFO)로 기록된다.

    Args:
        result:                 upsert_announcement 의 반환값.
        source_type:            공고 소스 유형 (로그 컨텍스트용).
        source_announcement_id: 공고 소스 ID (로그 컨텍스트용).
    """
    action = result.action
    if action == "created":
        logger.info(
            "신규 공고 등록: source={} id={}",
            source_type, source_announcement_id,
        )
    elif action == "unchanged":
        logger.debug(
            "변경 없음: source={} id={} (needs_detail={})",
            source_type, source_announcement_id, result.needs_detail_scraping,
        )
    elif action == "new_version":
        logger.info(
            "내용 변경 — 신규 버전 등록: source={} id={} changed_fields={}",
            source_type, source_announcement_id, sorted(result.changed_fields),
        )
    elif action == "status_transitioned":
        # 동일 공고의 상태(status)만 변경된 경우 — 정상 전이 경로 (in-place UPDATE)
        logger.info(
            "상태 전이 — in-place 갱신: source={} id={} changed_fields={}",
            source_type, source_announcement_id, sorted(result.changed_fields),
        )
    else:
        logger.warning(
            "알 수 없는 upsert action: source={} id={} action={}",
            source_type, source_announcement_id, action,
        )


# ──────────────────────────────────────────────────────────────
# 오케스트레이션
# ──────────────────────────────────────────────────────────────


def _resolve_active_sources(
    scrape_config: ScrapeRunConfig,
    sources_cfg: SourcesConfig,
) -> list[SourceConfig]:
    """실행할 소스 목록을 결정한다.

    - scrape_config.active_sources 가 지정되면 해당 소스만 실행 (enabled 설정 무시).
    - 비어 있으면 sources.yaml 의 enabled=True 소스를 모두 실행한다.
    - sources.yaml 에 없는 소스 ID 는 ERROR 로그 후 건너뛴다.
    """
    if scrape_config.active_sources:
        result = []
        for source_id in scrape_config.active_sources:
            source_config = sources_cfg.get_source(source_id)
            if source_config is None:
                logger.error(
                    "sources.yaml 에 '{}' 소스가 없습니다. 이 소스는 건너뜁니다.",
                    source_id,
                )
            else:
                result.append(source_config)
        return result

    return sources_cfg.get_enabled_sources()


async def _orchestrate(
    *,
    settings: Settings,
    scrape_config: ScrapeRunConfig,
    sources_cfg: SourcesConfig,
) -> dict[str, Any]:
    """소스 목록 → 소스별 공고 수집 → DB 적재 흐름을 수행하고 통계 dict 를 반환한다.

    max_pages / max_announcements 는 sources.yaml scrape 섹션에서 읽는다.
    각 소스 루프에서 scrape 섹션 > sources.yaml 소스별 > 코드 default 우선순위로 유효 상한을 결정한다.

    부트스트랩 실패(init_db)는 호출자로 전파한다.
    소스 단위 예외는 격리 — 한 소스 실패가 다른 소스를 중단시키지 않는다.
    """
    # (1) DB 스키마 보장
    logger.info("DB 초기화 시작 — dry_run={}", scrape_config.dry_run)
    init_db()

    # (2) 활성 소스 목록 결정
    active_sources = _resolve_active_sources(scrape_config, sources_cfg)
    logger.info("활성 소스: {}", [s.id for s in active_sources])

    # (3) 소스별 실행 및 통계 집계
    total_success = 0
    total_failure = 0
    total_failed_ids: list[str] = []
    total_detail_success = 0
    total_detail_failure = 0
    total_skipped_detail = 0
    total_action_counts: dict[str, int] = {}
    total_attachment_success = 0
    total_attachment_failure = 0
    total_attachment_skipped = 0

    for source_config in active_sources:
        # scrape 섹션 > sources.yaml 소스별 > 코드 default 우선순위로 유효 상한 결정
        effective_max_pages: int = (
            scrape_config.max_pages if scrape_config.max_pages is not None
            else (source_config.max_pages if source_config.max_pages is not None
                  else CODE_DEFAULT_MAX_PAGES)
        )
        effective_max_announcements: int = (
            scrape_config.max_announcements if scrape_config.max_announcements is not None
            else (source_config.max_announcements if source_config.max_announcements is not None
                  else CODE_DEFAULT_MAX_ANNOUNCEMENTS)
        )

        logger.info(
            "── 소스 {} 수집 시작 (max_pages={} max_announcements={})",
            source_config.id, effective_max_pages, effective_max_announcements,
        )
        try:
            adapter = get_adapter(source_config, settings)
            async with adapter:
                source_stats = await _run_source_announcements(
                    adapter=adapter,
                    settings=settings,
                    max_pages=effective_max_pages,
                    max_announcements=effective_max_announcements,
                    skip_detail=scrape_config.skip_detail,
                    skip_attachments=scrape_config.skip_attachments,
                    dry_run=scrape_config.dry_run,
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
        total_skipped_detail += source_stats["skipped_detail_count"]
        for action, count in source_stats.get("action_counts", {}).items():
            total_action_counts[action] = total_action_counts.get(action, 0) + count
        total_attachment_success += source_stats.get("attachment_success_count", 0)
        total_attachment_failure += source_stats.get("attachment_failure_count", 0)
        total_attachment_skipped += source_stats.get("attachment_skipped_count", 0)

        source_action_counts = source_stats.get("action_counts", {})
        logger.info(
            "소스 {} 완료: 목록 성공 {}건 / 실패 {}건 | "
            "상세 성공 {}건 / 실패 {}건 / 생략(변경없음) {}건 | "
            "첨부 저장 {}건 / 스킵 {}건 / 실패 {}건 | "
            "action 분포: 신규={} 변경없음={} 버전갱신={} 상태전이={}",
            source_config.id,
            source_stats["success_count"], source_stats["failure_count"],
            source_stats["detail_success_count"], source_stats["detail_failure_count"],
            source_stats["skipped_detail_count"],
            source_stats.get("attachment_success_count", 0),
            source_stats.get("attachment_skipped_count", 0),
            source_stats.get("attachment_failure_count", 0),
            source_action_counts.get("created", 0),
            source_action_counts.get("unchanged", 0),
            source_action_counts.get("new_version", 0),
            source_action_counts.get("status_transitioned", 0),
        )

    return {
        "success_count": total_success,
        "failure_count": total_failure,
        "failed_announcement_ids": total_failed_ids,
        "detail_success_count": total_detail_success,
        "detail_failure_count": total_detail_failure,
        "skipped_detail_count": total_skipped_detail,
        "action_counts": total_action_counts,
        "attachment_success_count": total_attachment_success,
        "attachment_failure_count": total_attachment_failure,
        "attachment_skipped_count": total_attachment_skipped,
    }


# ──────────────────────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────────────────────


async def _async_main() -> int:
    """async 진입점. sources.yaml 의 scrape 설정을 읽어 수집을 실행한다.

    Returns:
        프로세스 종료 코드. 0 이 정상.
    """
    sources_cfg = load_sources_config()
    scrape_config = sources_cfg.scrape

    settings = get_settings()
    # sources.yaml 의 log_level 이 지정된 경우 in-place 로 덮어쓴다.
    if scrape_config.log_level:
        settings.log_level = scrape_config.log_level
    configure_logging(settings)
    settings.ensure_runtime_paths()

    logger.info(
        "scrape 실행 시작: active_sources={} max_pages={} max_announcements={} "
        "skip_detail={} skip_attachments={} dry_run={}",
        scrape_config.active_sources or "(enabled 소스 전체)",
        scrape_config.max_pages,
        scrape_config.max_announcements,
        scrape_config.skip_detail,
        scrape_config.skip_attachments,
        scrape_config.dry_run,
    )

    try:
        summary = await _orchestrate(
            settings=settings,
            scrape_config=scrape_config,
            sources_cfg=sources_cfg,
        )
    except Exception as bootstrap_exc:
        logger.exception(
            "부트스트랩 실패 — 비정상 종료: ({}: {})",
            type(bootstrap_exc).__name__,
            bootstrap_exc,
        )
        return 1

    final_action_counts = summary.get("action_counts", {})
    logger.info(
        "scrape 실행 완료: 목록 성공 {}건 / 목록 실패 {}건 | "
        "상세 성공 {}건 / 실패 {}건 / 생략(변경없음) {}건 | "
        "첨부 저장 {}건 / 스킵 {}건 / 실패 {}건 | "
        "action 분포: 신규={} 변경없음={} 버전갱신={} 상태전이={}",
        summary["success_count"],
        summary["failure_count"],
        summary["detail_success_count"],
        summary["detail_failure_count"],
        summary["skipped_detail_count"],
        summary.get("attachment_success_count", 0),
        summary.get("attachment_skipped_count", 0),
        summary.get("attachment_failure_count", 0),
        final_action_counts.get("created", 0),
        final_action_counts.get("unchanged", 0),
        final_action_counts.get("new_version", 0),
        final_action_counts.get("status_transitioned", 0),
    )
    if summary["failure_count"]:
        logger.warning("목록 실패 공고 ID 목록: {}", summary["failed_announcement_ids"])
    return 0


def main() -> None:
    """OS 진입점. asyncio.run 으로 _async_main 을 실행하고 sys.exit 한다.

    SIGINT(Ctrl+C) 를 받으면 종료 코드 130 으로 종료한다.
    """
    try:
        exit_code = asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.warning("사용자 중단(SIGINT) 감지 — 종료 코드 130")
        sys.exit(130)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()


__all__ = [
    "main",
    "DEFAULT_STATUS_LABEL",
    "CODE_DEFAULT_MAX_PAGES",
    "CODE_DEFAULT_MAX_ANNOUNCEMENTS",
]
