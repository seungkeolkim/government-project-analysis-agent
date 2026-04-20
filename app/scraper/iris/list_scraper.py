"""IRIS 사업공고 목록 스크래퍼 (httpx 기반).

Playwright 없이 IRIS AJAX 엔드포인트에 직접 POST 요청을 보내
'접수중' 상태 공고 메타데이터(list[dict])를 수집한다.

## 탐사 결과 (2026-04-20)
IRIS 목록 페이지(`retrieveBsnsAncmBtinSituListView.do`)는 서버사이드 렌더링 없이
jQuery AJAX로 별도 엔드포인트를 POST 호출한다.

    엔드포인트: POST https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituList.do
    요청 바디 (application/x-www-form-urlencoded):
        pageIndex       = 1, 2, 3, ...
        pageUnit        = 10  (페이지당 건수)
        searchBtinSituCd = RCP  (접수중 필터 코드)
    응답: JSON
        listBsnsAncmBtinSitu : 공고 목록 (list)
        paginationInfo       : { totalPageCount, currentPageNo, ... }

반환 스키마(list[dict]) — 각 원소:
    - iris_announcement_id : IRIS 공고 고유 ID (ancmId)
    - title                : 공고 제목 (ancmTl)
    - agency               : 주관기관명 (sorgnNm)
    - status               : '접수중' 고정 (RCP 필터 결과이므로)
    - received_at_text     : 접수시작일 원문 (rcveStrDe, 'YYYY.MM.DD' 형식)
    - deadline_at_text     : 접수마감일 원문 (rcveEndDe, 'YYYY.MM.DD' 형식)
    - detail_url           : 상세 페이지 URL (ancmId 기반 구성)
    - detail_onclick       : None (HTTP 기반이므로 해당 없음)
    - row_html             : None (HTTP 기반이므로 해당 없음)

IRIS API 필드나 엔드포인트가 변경되면 아래 상수 블록만 교체한다.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

import httpx
from loguru import logger

from app.config import Settings, get_settings

# ──────────────────────────────────────────────────────────────
# 엔드포인트 및 요청 파라미터 상수 블록
# (IRIS API 변경 시 이 블록만 수정한다)
# ──────────────────────────────────────────────────────────────

# IRIS 기본 도메인
IRIS_BASE_DOMAIN: str = "https://www.iris.go.kr"

# 목록 AJAX 엔드포인트 (View 없는 버전 = 순수 데이터 API)
LIST_API_PATH: str = "/contents/retrieveBsnsAncmBtinSituList.do"

# 상세 페이지 경로 패턴 (ancmId 파라미터)
DETAIL_VIEW_PATH: str = "/contents/retrieveBsnsAncmView.do"

# '접수중' 상태 필터 코드
STATUS_CODE_RECEIVING: str = "RCP"

# 페이지당 레코드 수 (IRIS 기본값)
PAGE_UNIT: int = 10

# 응답 JSON에서 목록을 담는 키
RESPONSE_LIST_KEY: str = "listBsnsAncmBtinSitu"

# 응답 JSON에서 페이지네이션 정보를 담는 키
RESPONSE_PAGINATION_KEY: str = "paginationInfo"

# 페이지네이션 내 전체 페이지 수 키
PAGINATION_TOTAL_PAGE_KEY: str = "totalPageCount"

# 한 번의 수집에서 순회할 최대 페이지 수 (무한 루프 방어 상한)
DEFAULT_MAX_PAGES: int = 50

# HTTP 재시도 기본 상수
DEFAULT_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_SEC: float = 2.0

# 페이지 간 지터(추가 지연) 범위(초)
PAGE_JITTER_RANGE_SEC: tuple[float, float] = (0.5, 1.5)

# httpx 요청 타임아웃 (초)
HTTP_CONNECT_TIMEOUT_SEC: float = 10.0
HTTP_READ_TIMEOUT_SEC: float = 30.0

# 기본 User-Agent (브라우저를 흉내 내 차단 방지)
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 공고 상태 레이블 (status 필드에 저장할 원문)
STATUS_LABEL_RECEIVING: str = "접수중"


# ──────────────────────────────────────────────────────────────
# 내부 유틸
# ──────────────────────────────────────────────────────────────


async def _sleep_with_jitter(base_delay_sec: float) -> None:
    """base_delay_sec + 균등분포 지터만큼 비동기 대기한다.

    차단 방지를 위해 페이지 요청 사이마다 삽입된다.
    """
    safe_base = max(float(base_delay_sec), 0.0)
    jitter_min, jitter_max = PAGE_JITTER_RANGE_SEC
    jitter_sec = random.uniform(jitter_min, jitter_max)
    await asyncio.sleep(safe_base + jitter_sec)


def _build_http_client(settings: Settings) -> httpx.AsyncClient:
    """IRIS 요청용 httpx.AsyncClient를 생성한다.

    - Referer와 Accept-Language로 브라우저 요청처럼 위장한다.
    - 타임아웃은 connect/read 분리 적용한다.
    """
    effective_user_agent = settings.user_agent or DEFAULT_USER_AGENT
    timeout = httpx.Timeout(
        connect=HTTP_CONNECT_TIMEOUT_SEC,
        read=HTTP_READ_TIMEOUT_SEC,
        write=10.0,
        pool=10.0,
    )
    headers = {
        "User-Agent": effective_user_agent,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": settings.base_url,
        "Origin": IRIS_BASE_DOMAIN,
    }
    return httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True)


async def _fetch_page(
    client: httpx.AsyncClient,
    page_index: int,
) -> dict[str, Any]:
    """IRIS 목록 AJAX 엔드포인트에서 지정 페이지 데이터를 가져온다.

    Args:
        client:     재사용할 httpx.AsyncClient.
        page_index: 1-based 페이지 번호.

    Returns:
        JSON 응답 dict.

    Raises:
        httpx.HTTPStatusError: HTTP 오류 응답 (4xx/5xx).
        httpx.RequestError:    네트워크/타임아웃 오류.
        ValueError:            응답이 JSON이 아닌 경우.
    """
    url = IRIS_BASE_DOMAIN + LIST_API_PATH
    payload = {
        "pageIndex": str(page_index),
        "pageUnit": str(PAGE_UNIT),
        "searchBtinSituCd": STATUS_CODE_RECEIVING,
    }
    logger.debug("IRIS API POST: url={} page={}", url, page_index)
    response = await client.post(url, data=payload)
    response.raise_for_status()
    return response.json()


async def _fetch_page_with_retry(
    client: httpx.AsyncClient,
    page_index: int,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_sec: float = RETRY_BACKOFF_BASE_SEC,
) -> dict[str, Any]:
    """지수 백오프로 재시도하며 페이지 데이터를 가져온다.

    httpx.RequestError (타임아웃 포함) 발생 시에만 재시도한다.
    HTTP 4xx/5xx는 재시도 없이 즉시 전파한다.
    """
    effective_max = max(int(max_attempts), 1)
    last_exc: Optional[BaseException] = None

    for attempt_index in range(1, effective_max + 1):
        try:
            return await _fetch_page(client, page_index)
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt_index >= effective_max:
                logger.error(
                    "IRIS API 페이지 {} 최종 시도 {}/{} 실패: {}",
                    page_index,
                    attempt_index,
                    effective_max,
                    exc,
                )
                raise
            wait_sec = backoff_base_sec * (2 ** (attempt_index - 1))
            logger.warning(
                "IRIS API 페이지 {} 시도 {}/{} 실패({}): {:.1f}초 후 재시도",
                page_index,
                attempt_index,
                effective_max,
                type(exc).__name__,
                wait_sec,
            )
            await asyncio.sleep(wait_sec)

    assert last_exc is not None
    raise last_exc


def _build_detail_url(ancm_id: str) -> str:
    """ancmId로 상세 페이지 URL을 조립한다."""
    return f"{IRIS_BASE_DOMAIN}{DETAIL_VIEW_PATH}?ancmId={ancm_id}"


def _map_api_record_to_dict(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    """IRIS API 레코드 하나를 scrape_list 반환 스키마 dict로 변환한다.

    ancmId가 없으면 None을 반환한다(방어 처리).
    """
    ancm_id: Optional[str] = record.get("ancmId")
    if not ancm_id:
        logger.warning("ancmId 없는 레코드 발견 — 스킵: {!r}", record)
        return None

    title: Optional[str] = record.get("ancmTl") or None
    agency: Optional[str] = record.get("sorgnNm") or None
    received_at_text: Optional[str] = record.get("rcveStrDe") or None
    deadline_at_text: Optional[str] = record.get("rcveEndDe") or None

    return {
        "iris_announcement_id": ancm_id,
        "title": title,
        "agency": agency,
        # RCP 필터로 조회했으므로 상태는 항상 접수중
        "status": STATUS_LABEL_RECEIVING,
        "received_at_text": received_at_text,
        "deadline_at_text": deadline_at_text,
        "detail_url": _build_detail_url(ancm_id),
        "detail_onclick": None,
        "row_html": None,
    }


# ──────────────────────────────────────────────────────────────
# 공개 엔트리포인트
# ──────────────────────────────────────────────────────────────


async def scrape_list(
    *,
    settings: Optional[Settings] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[dict[str, Any]]:
    """IRIS AJAX API로 '접수중' 공고 메타데이터를 수집한다.

    동작 순서:
        1. httpx.AsyncClient를 생성하고 1페이지를 요청한다.
        2. 응답의 paginationInfo.totalPageCount로 전체 페이지 수를 파악한다.
        3. 2페이지 이후를 순차 요청하며 레코드를 누적한다.
        4. 각 페이지 사이에 REQUEST_DELAY_SEC + 지터를 삽입한다.
        5. max_pages 상한에 도달하거나 전체 페이지를 소진하면 종료한다.
        6. 중복 제거(ancmId 기준) 후 list[dict]를 반환한다.

    Args:
        settings:   주입할 Settings. 없으면 get_settings()를 사용한다.
        max_pages:  순회 안전 상한. 기본값 DEFAULT_MAX_PAGES.

    Returns:
        각 공고의 메타데이터 dict를 모은 리스트.

    Raises:
        httpx.HTTPStatusError: IRIS API가 HTTP 오류를 반환한 경우.
        httpx.RequestError:    재시도 한도 내에서도 연결에 실패한 경우.
    """
    effective_settings = settings or get_settings()
    base_request_delay_sec = effective_settings.request_delay_sec
    safe_max_pages = max(int(max_pages), 1)

    aggregated_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    async with _build_http_client(effective_settings) as client:
        # 1페이지 요청으로 전체 페이지 수 파악
        first_page_data = await _fetch_page_with_retry(client, page_index=1)

        pagination = first_page_data.get(RESPONSE_PAGINATION_KEY) or {}
        total_page_count: int = int(pagination.get(PAGINATION_TOTAL_PAGE_KEY) or 1)
        effective_max = min(total_page_count, safe_max_pages)

        logger.info(
            "IRIS API 총 페이지: {} (상한 적용 후: {})",
            total_page_count,
            effective_max,
        )

        # 1페이지 레코드 처리
        for record in first_page_data.get(RESPONSE_LIST_KEY) or []:
            row = _map_api_record_to_dict(record)
            if row and row["iris_announcement_id"] not in seen_ids:
                seen_ids.add(row["iris_announcement_id"])
                aggregated_rows.append(row)

        logger.info("페이지 1 처리 완료: 누적 {}건", len(aggregated_rows))

        # 2페이지 이후 순차 요청
        for page_index in range(2, effective_max + 1):
            # 차단 방지 딜레이
            await _sleep_with_jitter(base_request_delay_sec)

            page_data = await _fetch_page_with_retry(client, page_index=page_index)

            newly_added = 0
            for record in page_data.get(RESPONSE_LIST_KEY) or []:
                row = _map_api_record_to_dict(record)
                if row and row["iris_announcement_id"] not in seen_ids:
                    seen_ids.add(row["iris_announcement_id"])
                    aggregated_rows.append(row)
                    newly_added += 1

            logger.info(
                "페이지 {} 처리 완료: 신규 {}건 (누적 {}건)",
                page_index,
                newly_added,
                len(aggregated_rows),
            )

        if total_page_count > safe_max_pages:
            logger.warning(
                "max_pages({}) 상한으로 인해 {} 페이지 중 {}까지만 수집함",
                safe_max_pages,
                total_page_count,
                safe_max_pages,
            )

    logger.info("scrape_list 완료: 총 {}건", len(aggregated_rows))
    return aggregated_rows


__all__ = [
    "scrape_list",
    "IRIS_BASE_DOMAIN",
    "LIST_API_PATH",
    "DETAIL_VIEW_PATH",
    "STATUS_CODE_RECEIVING",
    "STATUS_LABEL_RECEIVING",
    "PAGE_UNIT",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_ATTEMPTS",
    "RETRY_BACKOFF_BASE_SEC",
    "PAGE_JITTER_RANGE_SEC",
]
