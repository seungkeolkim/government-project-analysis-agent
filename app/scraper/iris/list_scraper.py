"""IRIS 사업공고 목록 스크래퍼 (httpx 기반).

Playwright 없이 IRIS AJAX 엔드포인트에 직접 POST 요청을 보내
접수예정·접수중·마감 3개 상태 공고 메타데이터(list[dict])를 수집한다.

## 탐사 결과 (2026-04-21)
IRIS 목록 페이지(`retrieveBsnsAncmBtinSituListView.do`)는 서버사이드 렌더링 없이
jQuery AJAX로 별도 엔드포인트를 POST 호출한다.

    엔드포인트: POST https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituList.do
    요청 바디 (application/x-www-form-urlencoded):
        pageIndex = 1, 2, 3, ...
        pageUnit  = 10  (페이지당 건수)
        ancmPrg   = ancmPre | ancmIng | ancmEnd  (상태 필터 코드)
    응답: JSON
        listBsnsAncmBtinSitu : 공고 목록 (list)
        paginationInfo       : { totalPageCount, currentPageNo, ... }

### 파라미터 변경 근거 — searchBtinSituCd → ancmPrg (2026-04-21 확인)

**[1] JS 소스 근거** (`/resources/js/contents/bsnsancm/bsnsAncmBtinSituList.js`):
  - `f_bsnsAncmListForm_go(ancmPrg)` 함수가 `$("input[name=ancmPrg]").val(ancmPrg)` 로
    폼에 상태값을 쓰고 submit 한다.
  - `f_bsnsAncmBtinSituListForm_search` 는 `$('#bsnsAncmBtinSituListForm').serializeObject()` 로
    폼 전체를 직렬화해 API 에 전송 — 즉 `ancmPrg` 가 POST body 에 포함된다.
  - 페이지 HTML: `<input type="hidden" name="ancmPrg" value="ancmPre"/>` (접수예정 탭)

**[2] 실제 API 호출 교차 검증** (2026-04-21 curl):
  - `searchBtinSituCd=RCP` → total=8, ancmIds=['021054','020915','021118'], rcveStt=['진행중',...]
  - 파라미터 없음     → total=8, ancmIds=['021054','020915','021118'], rcveStt=['진행중',...]  ← 동일
  - `ancmPrg=ancmIng` → total=8, ancmIds=['021054','020915','021118'], rcveStt=['진행중',...]  ← 동일
  - `ancmPrg=ancmPre` → total=556, ancmIds=['021094','020975','020937'], rcveStt=['예정',...]  ← 완전히 다름

  결론: `searchBtinSituCd` 는 현재 IRIS API 에서 **무시**된다. 기존 코드가 접수중을 수집했던
  이유는 해당 파라미터가 필터 역할을 한 것이 아니라 API 기본 반환값이 접수중이었기 때문이다.
  실제 상태 필터는 `ancmPrg` 이며, 접수예정(ancmPre)/접수중(ancmIng)/마감(ancmEnd)을 반환한다.

반환 스키마(list[dict]) — 각 원소:
    - iris_announcement_id : IRIS 공고 고유 ID (ancmId)
    - title                : 공고 제목 (ancmTl)
    - agency               : 주관기관명 (sorgnNm)
    - status               : '접수예정' / '접수중' / '마감' (순회 중인 상태 라벨)
    - received_at_text     : 접수시작일 원문 (rcveStrDe, 'YYYY.MM.DD' 형식, 접수예정 시 공란 가능)
    - deadline_at_text     : 접수마감일 원문 (rcveEndDe, 'YYYY.MM.DD' 형식, 접수예정 시 공란 가능)
    - detail_url           : 상세 페이지 URL (ancmId 기반 구성)
    - detail_onclick       : None (HTTP 기반이므로 해당 없음)
    - row_html             : None (HTTP 기반이므로 해당 없음)

IRIS API 필드나 엔드포인트가 변경되면 아래 상수 블록만 교체한다.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from typing import Any

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

# 상태 필터 파라미터명 (IRIS 폼 직렬화 기준)
STATUS_FILTER_PARAM: str = "ancmPrg"

# 상태 코드 — IRIS API ancmPrg 값
STATUS_CODE_SCHEDULED: str = "ancmPre"  # 접수예정
STATUS_CODE_RECEIVING: str = "ancmIng"  # 접수중
STATUS_CODE_CLOSED: str = "ancmEnd"  # 마감

# 공고 상태 한글 라벨 (AnnouncementStatus.value 와 1:1 일치)
STATUS_LABEL_SCHEDULED: str = "접수예정"
STATUS_LABEL_RECEIVING: str = "접수중"
STATUS_LABEL_CLOSED: str = "마감"

# 한글 라벨 → API 코드 매핑 테이블
STATUS_LABEL_TO_API_CODE: dict[str, str] = {
    STATUS_LABEL_SCHEDULED: STATUS_CODE_SCHEDULED,
    STATUS_LABEL_RECEIVING: STATUS_CODE_RECEIVING,
    STATUS_LABEL_CLOSED: STATUS_CODE_CLOSED,
}

# 기본 수집 상태 목록 (sources.yaml 에 statuses 가 없을 때 fallback)
DEFAULT_STATUSES: list[str] = [STATUS_LABEL_SCHEDULED, STATUS_LABEL_RECEIVING, STATUS_LABEL_CLOSED]

# 페이지당 레코드 수 (IRIS 기본값)
PAGE_UNIT: int = 10

# 응답 JSON에서 목록을 담는 키
RESPONSE_LIST_KEY: str = "listBsnsAncmBtinSitu"

# 응답 JSON에서 페이지네이션 정보를 담는 키
RESPONSE_PAGINATION_KEY: str = "paginationInfo"

# 페이지네이션 내 전체 페이지 수 키
PAGINATION_TOTAL_PAGE_KEY: str = "totalPageCount"

# list_scraper 직접 호출 시 사용하는 기본 상한 (오케스트레이터는 항상 명시적으로 전달함)
DEFAULT_MAX_PAGES: int = 10

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
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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
    status_code: str,
) -> dict[str, Any]:
    """IRIS 목록 AJAX 엔드포인트에서 지정 페이지 데이터를 가져온다.

    Args:
        client:      재사용할 httpx.AsyncClient.
        page_index:  1-based 페이지 번호.
        status_code: IRIS ancmPrg 상태 코드 (ancmPre/ancmIng/ancmEnd).

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
        STATUS_FILTER_PARAM: status_code,
    }
    logger.debug("IRIS API POST: url={} page={} status_code={}", url, page_index, status_code)
    response = await client.post(url, data=payload)
    response.raise_for_status()
    return response.json()


async def _fetch_page_with_retry(
    client: httpx.AsyncClient,
    page_index: int,
    status_code: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_sec: float = RETRY_BACKOFF_BASE_SEC,
) -> dict[str, Any]:
    """지수 백오프로 재시도하며 페이지 데이터를 가져온다.

    httpx.RequestError (타임아웃 포함) 발생 시에만 재시도한다.
    HTTP 4xx/5xx는 재시도 없이 즉시 전파한다.
    """
    effective_max = max(int(max_attempts), 1)
    last_exc: BaseException | None = None

    for attempt_index in range(1, effective_max + 1):
        try:
            return await _fetch_page(client, page_index, status_code)
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt_index >= effective_max:
                logger.error(
                    "IRIS API 페이지 {} (상태:{}) 최종 시도 {}/{} 실패: {}",
                    page_index,
                    status_code,
                    attempt_index,
                    effective_max,
                    exc,
                )
                raise
            wait_sec = backoff_base_sec * (2 ** (attempt_index - 1))
            logger.warning(
                "IRIS API 페이지 {} (상태:{}) 시도 {}/{} 실패({}): {:.1f}초 후 재시도",
                page_index,
                status_code,
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


def _map_api_record_to_dict(
    record: dict[str, Any],
    status_label: str,
) -> dict[str, Any] | None:
    """IRIS API 레코드 하나를 scrape_list 반환 스키마 dict로 변환한다.

    status_label은 현재 순회 중인 상태의 한글 라벨이다.
    API 응답의 rcveStt 필드가 없거나 불일치할 수 있으므로 루프 변수를 신뢰한다.
    ancmId가 없으면 None을 반환한다(방어 처리).

    Args:
        record:       IRIS API 응답 목록의 단일 레코드 dict.
        status_label: 현재 순회 중인 상태 한글 라벨 ('접수예정'/'접수중'/'마감').
    """
    ancm_id: str | None = record.get("ancmId")
    if not ancm_id:
        logger.warning("ancmId 없는 레코드 발견 — 스킵: {!r}", record)
        return None

    title: str | None = record.get("ancmTl") or None
    agency: str | None = record.get("sorgnNm") or None
    # 접수예정 상태에서는 rcveStrDe/rcveEndDe 가 공란인 경우가 많다.
    received_at_text: str | None = record.get("rcveStrDe") or None
    deadline_at_text: str | None = record.get("rcveEndDe") or None
    # ancmNo: 외부 공유 가능한 공식 공고번호. canonical_key 계산에 사용된다.
    # N:1 구조(1 ancmNo → 여러 ancmId)이므로 canonical group 단위 식별자로 활용한다.
    ancm_no: str | None = record.get("ancmNo") or None

    return {
        "iris_announcement_id": ancm_id,
        "ancm_no": ancm_no,
        "title": title,
        "agency": agency,
        "status": status_label,
        "received_at_text": received_at_text,
        "deadline_at_text": deadline_at_text,
        "detail_url": _build_detail_url(ancm_id),
        "detail_onclick": None,
        "row_html": None,
    }


def _resolve_statuses(statuses: Sequence[str]) -> list[tuple[str, str]]:
    """한글 라벨 목록을 (라벨, API 코드) 쌍의 리스트로 변환한다.

    알 수 없는 라벨은 WARNING 로그 후 스킵한다.
    유효한 상태가 하나도 없으면 fallback으로 접수중만 수집하도록 반환한다.

    Args:
        statuses: 한글 상태 라벨의 시퀀스.

    Returns:
        (한글 라벨, ancmPrg 코드) 쌍의 리스트. 최소 1개 이상.
    """
    result: list[tuple[str, str]] = []
    for label in statuses:
        code = STATUS_LABEL_TO_API_CODE.get(label)
        if code is None:
            logger.warning("알 수 없는 공고 상태 라벨 — 스킵: {!r}", label)
        else:
            result.append((label, code))

    if not result:
        logger.warning(
            "유효한 상태 라벨이 없음 — 접수중만 수집하는 fallback 적용: statuses={!r}",
            list(statuses),
        )
        result.append((STATUS_LABEL_RECEIVING, STATUS_CODE_RECEIVING))

    return result


# ──────────────────────────────────────────────────────────────
# 공개 엔트리포인트
# ──────────────────────────────────────────────────────────────


async def scrape_list(
    *,
    settings: Settings | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_announcements: int | None = None,
    statuses: Sequence[str] = DEFAULT_STATUSES,
) -> list[dict[str, Any]]:
    """IRIS AJAX API로 접수예정·접수중·마감 공고 메타데이터를 순차 수집한다.

    동작 순서:
        1. statuses 에서 유효한 (라벨, API 코드) 쌍을 추출한다.
        2. 각 상태별로:
            a. httpx.AsyncClient를 공유하며 1페이지를 요청하고 전체 페이지 수를 파악한다.
            b. max_pages 상한까지 순차 요청하며 레코드를 누적한다.
            c. 누적 건수가 max_announcements 상한에 도달하면 전체 루프를 종료한다.
        3. seen_ids 집합을 모든 상태에 걸쳐 공유하여 상태 간 중복을 제거한다.
        4. 각 페이지 사이, 그리고 상태 전환 사이에 REQUEST_DELAY_SEC + 지터를 삽입한다.
        5. 수집 완료 후 list[dict]를 반환한다.

    날짜 기반 조기 종료 조건은 사용하지 않는다.
    상한 도달 시 단순히 루프를 종료한다.

    Args:
        settings:          주입할 Settings. 없으면 get_settings()를 사용한다.
        max_pages:         상태별 순회 안전 상한. 기본값 DEFAULT_MAX_PAGES.
        max_announcements: 전체 누적 건수 상한. None이면 상한 없음.
        statuses:          수집할 상태 한글 라벨 목록.
                           기본값 DEFAULT_STATUSES (['접수예정','접수중','마감']).

    Returns:
        각 공고의 메타데이터 dict를 모은 리스트.

    Raises:
        httpx.HTTPStatusError: IRIS API가 HTTP 오류를 반환한 경우.
        httpx.RequestError:    재시도 한도 내에서도 연결에 실패한 경우.
    """
    effective_settings = settings or get_settings()
    base_request_delay_sec = effective_settings.request_delay_sec
    safe_max_pages = max(int(max_pages), 1)

    # 유효 (라벨, 코드) 쌍 목록 확보
    status_pairs = _resolve_statuses(statuses)

    aggregated_rows: list[dict[str, Any]] = []
    # seen_ids 는 상태 간 경계를 포함해 전체 루프에 걸쳐 공유한다.
    seen_ids: set[str] = set()
    # 상한 도달 여부 플래그 — 내부 루프가 외부 루프를 종료하는 데 사용한다.
    limit_reached = False

    async with _build_http_client(effective_settings) as client:
        for status_index, (status_label, status_code) in enumerate(status_pairs):
            if limit_reached:
                break

            # 상태 전환 사이 지터 삽입 (첫 번째 상태는 건너뜀)
            if status_index > 0:
                await _sleep_with_jitter(base_request_delay_sec)

            logger.info("=== 상태 수집 시작: {} (code={}) ===", status_label, status_code)

            # 1페이지 요청으로 전체 페이지 수 파악
            first_page_data = await _fetch_page_with_retry(client, page_index=1, status_code=status_code)

            pagination = first_page_data.get(RESPONSE_PAGINATION_KEY) or {}
            total_page_count: int = int(pagination.get(PAGINATION_TOTAL_PAGE_KEY) or 1)
            effective_max = min(total_page_count, safe_max_pages)

            logger.info(
                "IRIS API [{}] 총 페이지: {} (상한 적용 후: {})",
                status_label,
                total_page_count,
                effective_max,
            )

            # 1페이지 레코드 처리
            for record in first_page_data.get(RESPONSE_LIST_KEY) or []:
                row = _map_api_record_to_dict(record, status_label)
                if row and row["iris_announcement_id"] not in seen_ids:
                    seen_ids.add(row["iris_announcement_id"])
                    aggregated_rows.append(row)

            logger.info(
                "[{}] 페이지 1 처리 완료: 누적 {}건",
                status_label,
                len(aggregated_rows),
            )

            # max_announcements 상한 확인
            if max_announcements and len(aggregated_rows) >= max_announcements:
                logger.info(
                    "max_announcements({}) 상한 도달: {} 상태 1페이지 처리 후 종료",
                    max_announcements,
                    status_label,
                )
                limit_reached = True
                break

            # 2페이지 이후 순차 요청
            if total_page_count > safe_max_pages:
                logger.warning(
                    "max_pages({}) 상한으로 인해 [{}] {} 페이지 중 {}까지만 수집함",
                    safe_max_pages,
                    status_label,
                    total_page_count,
                    safe_max_pages,
                )

            for page_index in range(2, effective_max + 1):
                # 차단 방지 딜레이
                await _sleep_with_jitter(base_request_delay_sec)

                page_data = await _fetch_page_with_retry(
                    client, page_index=page_index, status_code=status_code
                )

                newly_added = 0
                for record in page_data.get(RESPONSE_LIST_KEY) or []:
                    row = _map_api_record_to_dict(record, status_label)
                    if row and row["iris_announcement_id"] not in seen_ids:
                        seen_ids.add(row["iris_announcement_id"])
                        aggregated_rows.append(row)
                        newly_added += 1

                logger.info(
                    "[{}] 페이지 {} 처리 완료: 신규 {}건 (누적 {}건)",
                    status_label,
                    page_index,
                    newly_added,
                    len(aggregated_rows),
                )

                # max_announcements 상한 확인
                if max_announcements and len(aggregated_rows) >= max_announcements:
                    logger.info(
                        "max_announcements({}) 상한 도달: {} 상태 {}페이지 처리 후 종료",
                        max_announcements,
                        status_label,
                        page_index,
                    )
                    limit_reached = True
                    break

            logger.info("=== 상태 수집 완료: {} — {}건 ===", status_label, len(aggregated_rows))

    logger.info("scrape_list 완료: 총 {}건", len(aggregated_rows))
    return aggregated_rows


__all__ = [
    "scrape_list",
    "IRIS_BASE_DOMAIN",
    "LIST_API_PATH",
    "DETAIL_VIEW_PATH",
    "STATUS_FILTER_PARAM",
    "STATUS_CODE_SCHEDULED",
    "STATUS_CODE_RECEIVING",
    "STATUS_CODE_CLOSED",
    "STATUS_LABEL_SCHEDULED",
    "STATUS_LABEL_RECEIVING",
    "STATUS_LABEL_CLOSED",
    "STATUS_LABEL_TO_API_CODE",
    "DEFAULT_STATUSES",
    "PAGE_UNIT",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_ATTEMPTS",
    "RETRY_BACKOFF_BASE_SEC",
    "PAGE_JITTER_RANGE_SEC",
]
