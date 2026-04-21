"""NTIS 국가R&D통합공고 목록 스크래퍼 (httpx + BeautifulSoup 기반).

## 탐사 결과 적용 (docs/ntis_site_exploration.md §1~§2)

NTIS 목록 페이지는 **SSR HTML** 로 렌더링된다. JSON API가 없으므로
POST 요청 후 BeautifulSoup 으로 HTML 을 파싱한다. IRIS 와 달리 전용 AJAX
엔드포인트가 없다.

    엔드포인트: POST https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do
    요청 바디 (application/x-www-form-urlencoded):
        pageIndex         = 1, 2, 3, …
        searchStatusList  = P | B | Y | ""  (접수예정 | 접수중 | 마감 | 전체)
        searchFormList    = "" (공고형태 필터 미사용 — 전체)
        searchDeptList    = "" (부처 필터 미사용 — 전체)
        flag              = ""

    응답: HTML (text/html; charset=UTF-8)
        hidden input name="totalCount" → 전체 건수
        table > tbody > tr (데이터 행):
            td[0]: <input type="checkbox" name="selectCheckList" value="{roRndUid}">
            td[1]: 순번 (누적 sequential, URL 파라미터 아님)
            td[2]: 현황 (접수예정 / 접수중 / 마감)
            td[3]: <a href="/rndgate/eg/un/ra/view.do?roRndUid=…&flag=rndList">공고명</a>
            td[4]: 부처명
            td[5]: 접수일 (YYYY.MM.DD, 미표기 가능)
            td[6]: 마감일 (YYYY.MM.DD, 미표기 가능)
            td[7]: D-day

상태 코드 실측값:
    P = 접수예정, B = 접수중, Y = 마감

날짜 기반 조기 종료 없음 — max_pages / max_announcements 상한만으로 제어.
(PROJECT_NOTES.md '주요 결정' 참조)

반환 스키마 (list[dict]):
    ntis_announcement_id : NTIS roRndUid (str, source_announcement_id 로 쓰임)
    title                : 공고명 (str)
    agency               : 부처명 (str | None)
    status               : AnnouncementStatus.value 정규화 후 값 ('접수예정'/'접수중'/'마감')
    received_at_text     : 접수일 원문 ('YYYY.MM.DD' 형식, 없으면 None)
    deadline_at_text     : 마감일 원문 ('YYYY.MM.DD' 형식, 없으면 None)
    detail_url           : 상세 페이지 전체 URL
    detail_onclick       : None (HTTP 기반 상세 URL 직접 사용이므로 해당 없음)
    row_html             : None (SSR 파싱 방식이므로 해당 없음)
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Sequence
from typing import Any

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from app.config import Settings, get_settings
from app.scraper.ntis.status_normalizer import (
    NTIS_STATUS_RAW_CLOSED,
    NTIS_STATUS_RAW_RECEIVING,
    NTIS_STATUS_RAW_SCHEDULED,
    normalize_ntis_status,
)

# ──────────────────────────────────────────────────────────────
# 엔드포인트 및 요청 파라미터 상수 블록
# (NTIS 사이트 변경 시 이 블록만 수정한다)
# ──────────────────────────────────────────────────────────────

# NTIS 기본 도메인
NTIS_BASE_DOMAIN: str = "https://www.ntis.go.kr"

# 목록 POST 엔드포인트 (SSR HTML 응답)
LIST_PAGE_PATH: str = "/rndgate/eg/un/ra/mng.do"

# 상세 페이지 경로 패턴 (roRndUid 파라미터)
DETAIL_VIEW_PATH: str = "/rndgate/eg/un/ra/view.do"

# 상태 필터 파라미터명
STATUS_FILTER_PARAM: str = "searchStatusList"

# 상태 코드 — NTIS searchStatusList 실측값 (탐사 §2)
STATUS_CODE_SCHEDULED: str = "P"   # 접수예정
STATUS_CODE_RECEIVING: str = "B"   # 접수중
STATUS_CODE_CLOSED: str = "Y"      # 마감

# 한글 라벨 → API 코드 매핑 테이블 (탐사 §2 실측 기준)
STATUS_LABEL_TO_API_CODE: dict[str, str] = {
    NTIS_STATUS_RAW_SCHEDULED: STATUS_CODE_SCHEDULED,
    NTIS_STATUS_RAW_RECEIVING: STATUS_CODE_RECEIVING,
    NTIS_STATUS_RAW_CLOSED: STATUS_CODE_CLOSED,
}

# 기본 수집 상태 목록 (sources.yaml 에 statuses 가 없을 때 fallback)
DEFAULT_STATUSES: list[str] = [
    NTIS_STATUS_RAW_SCHEDULED,
    NTIS_STATUS_RAW_RECEIVING,
    NTIS_STATUS_RAW_CLOSED,
]

# 페이지당 레코드 수 (NTIS 기본값 — 변경 불가)
PAGE_SIZE: int = 10

# 응답 HTML 에서 전체 건수를 담는 hidden input name
TOTAL_COUNT_INPUT_NAME: str = "totalCount"

# list_scraper 직접 호출 시 사용하는 기본 상한 (오케스트레이터는 항상 명시적으로 전달함)
DEFAULT_MAX_PAGES: int = 10

# HTTP 재시도 상수
DEFAULT_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_SEC: float = 2.0

# 페이지 간 지터(추가 지연) 범위(초)
PAGE_JITTER_RANGE_SEC: tuple[float, float] = (0.5, 1.5)

# httpx 요청 타임아웃 (초)
HTTP_CONNECT_TIMEOUT_SEC: float = 10.0
HTTP_READ_TIMEOUT_SEC: float = 30.0

# 기본 User-Agent
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
    jitter = random.uniform(jitter_min, jitter_max)
    await asyncio.sleep(safe_base + jitter)


def _build_http_client(settings: Settings) -> httpx.AsyncClient:
    """NTIS 요청용 httpx.AsyncClient 를 생성한다.

    Referer 와 Accept-Language 로 브라우저 요청처럼 위장한다.
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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": NTIS_BASE_DOMAIN + LIST_PAGE_PATH,
        "Origin": NTIS_BASE_DOMAIN,
    }
    return httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True)


def _build_detail_url(ro_rnd_uid: str) -> str:
    """roRndUid 로 상세 페이지 전체 URL 을 조립한다."""
    return f"{NTIS_BASE_DOMAIN}{DETAIL_VIEW_PATH}?roRndUid={ro_rnd_uid}&flag=rndList"


def _build_list_payload(page_index: int, status_code: str) -> dict[str, str]:
    """목록 POST 요청 바디를 조립한다."""
    return {
        "flag": "",
        "searchFormList": "",
        STATUS_FILTER_PARAM: status_code,
        "searchDeptList": "",
        "pageIndex": str(page_index),
    }


async def _fetch_list_page(
    client: httpx.AsyncClient,
    page_index: int,
    status_code: str,
) -> str:
    """NTIS 목록 POST 요청으로 지정 페이지 HTML 을 가져온다.

    Args:
        client:      재사용할 httpx.AsyncClient.
        page_index:  1-based 페이지 번호.
        status_code: NTIS searchStatusList 상태 코드 (P/B/Y).

    Returns:
        응답 HTML 텍스트.

    Raises:
        httpx.HTTPStatusError: HTTP 오류 응답 (4xx/5xx).
        httpx.RequestError:    네트워크/타임아웃 오류.
    """
    url = NTIS_BASE_DOMAIN + LIST_PAGE_PATH
    payload = _build_list_payload(page_index, status_code)
    logger.debug(
        "NTIS 목록 POST: url={} page={} status_code={}",
        url, page_index, status_code,
    )
    response = await client.post(url, data=payload)
    response.raise_for_status()
    return response.text


async def _fetch_list_page_with_retry(
    client: httpx.AsyncClient,
    page_index: int,
    status_code: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_sec: float = RETRY_BACKOFF_BASE_SEC,
) -> str:
    """지수 백오프로 재시도하며 목록 페이지 HTML 을 가져온다.

    httpx.RequestError (타임아웃 포함) 발생 시에만 재시도한다.
    HTTP 4xx/5xx 는 재시도 없이 즉시 전파한다.
    """
    effective_max = max(int(max_attempts), 1)
    last_exc: BaseException | None = None

    for attempt_index in range(1, effective_max + 1):
        try:
            return await _fetch_list_page(client, page_index, status_code)
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt_index >= effective_max:
                logger.error(
                    "NTIS 목록 페이지 {} (상태:{}) 최종 시도 {}/{} 실패: {}",
                    page_index, status_code, attempt_index, effective_max, exc,
                )
                raise
            wait_sec = backoff_base_sec * (2 ** (attempt_index - 1))
            logger.warning(
                "NTIS 목록 페이지 {} (상태:{}) 시도 {}/{} 실패({}): {:.1f}초 후 재시도",
                page_index, status_code, attempt_index, effective_max,
                type(exc).__name__, wait_sec,
            )
            await asyncio.sleep(wait_sec)

    assert last_exc is not None
    raise last_exc


def _parse_total_count(html: str) -> int:
    """HTML 에서 hidden input[name=totalCount] 값을 추출하여 정수로 반환한다.

    파싱 실패 시 0 을 반환한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    total_input = soup.find("input", {"name": TOTAL_COUNT_INPUT_NAME})
    if total_input is None:
        logger.warning("totalCount hidden input 을 찾지 못함 — 0 반환")
        return 0
    try:
        return int(total_input.get("value", "0") or "0")
    except (ValueError, TypeError):
        logger.warning(
            "totalCount 값 파싱 실패: {!r} — 0 반환",
            total_input.get("value"),
        )
        return 0


def _parse_list_rows(html: str, status_label: str) -> list[dict[str, Any]]:
    """HTML 목록 페이지에서 공고 행을 파싱하여 dict 리스트로 반환한다.

    공고 테이블: `<th>` 에 '순번', '현황', '공고명' 을 모두 포함하는 테이블을 선택한다.
    각 데이터 행의 셀 구조(탐사 §1-2 기준):
        td[0]: checkbox input, value=roRndUid
        td[1]: 순번
        td[2]: 현황 (원문 상태 라벨)
        td[3]: a 태그 — href, text=공고명
        td[4]: 부처명
        td[5]: 접수일 (YYYY.MM.DD, 빈 문자열 가능)
        td[6]: 마감일 (YYYY.MM.DD, 빈 문자열 가능)
        td[7]: D-day

    status_label 은 현재 순회 중인 상태의 한글 라벨이다. HTML 파싱 상태 셀보다
    루프 변수를 신뢰하여 주입한다 (필터 쿼리와 실제 응답이 항상 일치함).

    Args:
        html:         목록 페이지 HTML 텍스트.
        status_label: 현재 순회 중인 상태 한글 라벨.

    Returns:
        파싱된 공고 메타 dict 목록. roRndUid 없는 행은 스킵.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 공고 목록 테이블 찾기 — th에 '순번', '현황', '공고명' 모두 있는 테이블
    target_table = None
    for table in soup.find_all("table"):
        header_texts = {th.get_text(strip=True) for th in table.find_all("th")}
        if "순번" in header_texts and "공고명" in header_texts:
            target_table = table
            break

    if target_table is None:
        logger.warning("공고 목록 테이블을 찾지 못함 (status={}) — 빈 목록 반환", status_label)
        return []

    rows: list[dict[str, Any]] = []
    data_rows = target_table.find_all("tr")

    for tr in data_rows:
        cells = tr.find_all("td")
        if len(cells) < 6:
            # 헤더 행이나 td 가 부족한 행은 스킵
            continue

        # td[0]: checkbox input value = roRndUid
        checkbox_input = cells[0].find("input", {"name": "selectCheckList"})
        if checkbox_input is None:
            continue
        ro_rnd_uid = (checkbox_input.get("value") or "").strip()
        if not ro_rnd_uid:
            logger.warning("roRndUid 없는 행 발견 — 스킵")
            continue

        # td[3]: 공고명 (a 태그 내 텍스트)
        title_cell = cells[3]
        title_link = title_cell.find("a")
        title = (title_link.get_text(strip=True) if title_link else title_cell.get_text(strip=True)) or None

        # td[4]: 부처명
        agency_text = cells[4].get_text(strip=True) or None

        # td[5]: 접수일 원문
        received_at_text = cells[5].get_text(strip=True) or None

        # td[6]: 마감일 원문
        deadline_at_text = cells[6].get_text(strip=True) if len(cells) > 6 else None
        if not deadline_at_text:
            deadline_at_text = None

        rows.append({
            "ntis_announcement_id": ro_rnd_uid,
            "title": title,
            "agency": agency_text,
            "status": status_label,          # normalize_ntis_status 는 호출자에서 적용
            "received_at_text": received_at_text,
            "deadline_at_text": deadline_at_text,
            "detail_url": _build_detail_url(ro_rnd_uid),
            "detail_onclick": None,
            "row_html": None,
        })

    return rows


def _resolve_statuses(statuses: Sequence[str]) -> list[tuple[str, str]]:
    """한글 라벨 목록을 (라벨, API 코드) 쌍의 리스트로 변환한다.

    알 수 없는 라벨은 WARNING 로그 후 스킵한다.
    유효한 상태가 하나도 없으면 fallback 으로 접수중만 수집한다.

    Args:
        statuses: 한글 상태 라벨의 시퀀스.

    Returns:
        (한글 라벨, searchStatusList 코드) 쌍의 리스트. 최소 1개 이상.
    """
    result: list[tuple[str, str]] = []
    for label in statuses:
        code = STATUS_LABEL_TO_API_CODE.get(label)
        if code is None:
            logger.warning("알 수 없는 NTIS 공고 상태 라벨 — 스킵: {!r}", label)
        else:
            result.append((label, code))

    if not result:
        logger.warning(
            "유효한 상태 라벨이 없음 — 접수중만 수집하는 fallback 적용: statuses={!r}",
            list(statuses),
        )
        result.append((NTIS_STATUS_RAW_RECEIVING, STATUS_CODE_RECEIVING))

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
    """NTIS 목록 SSR HTML 파싱으로 접수예정·접수중·마감 공고 메타데이터를 순차 수집한다.

    동작 순서:
        1. statuses 에서 유효한 (라벨, 코드) 쌍을 추출한다.
        2. 각 상태별로:
            a. POST 1페이지를 요청하여 totalCount 를 파악하고 전체 페이지 수를 계산한다.
            b. max_pages 상한까지 순차 요청하며 HTML 파싱 결과를 누적한다.
            c. 누적 건수가 max_announcements 상한에 도달하면 전체 루프를 종료한다.
        3. seen_ids 집합을 모든 상태에 걸쳐 공유하여 상태 간 중복을 제거한다.
        4. 각 페이지 사이, 상태 전환 사이에 request_delay_sec + 지터를 삽입한다.
        5. normalize_ntis_status() 로 상태 라벨을 정규화한다.
        6. 수집 완료 후 list[dict] 를 반환한다.

    날짜 기반 조기 종료 조건은 사용하지 않는다.
    상한 도달 시 단순히 루프를 종료한다.

    Args:
        settings:          주입할 Settings. 없으면 get_settings() 를 사용한다.
        max_pages:         상태별 순회 안전 상한. 기본값 DEFAULT_MAX_PAGES.
        max_announcements: 전체 누적 건수 상한. None 이면 상한 없음.
        statuses:          수집할 상태 한글 라벨 목록.

    Returns:
        각 공고의 메타데이터 dict 를 모은 리스트.

    Raises:
        httpx.HTTPStatusError: NTIS 서버가 HTTP 오류를 반환한 경우.
        httpx.RequestError:    재시도 한도 내에서도 연결에 실패한 경우.
    """
    effective_settings = settings or get_settings()
    base_request_delay_sec = effective_settings.request_delay_sec
    safe_max_pages = max(int(max_pages), 1)

    # 유효 (라벨, 코드) 쌍 목록 확보
    status_pairs = _resolve_statuses(statuses)

    aggregated_rows: list[dict[str, Any]] = []
    # seen_ids: 상태 간 경계 포함 전체 루프에 걸쳐 공유 (roRndUid 기준 중복 제거)
    seen_ids: set[str] = set()
    # 상한 도달 여부 플래그
    limit_reached = False

    async with _build_http_client(effective_settings) as client:
        for status_index, (status_label, status_code) in enumerate(status_pairs):
            if limit_reached:
                break

            # 상태 전환 사이 지터 삽입 (첫 번째 상태는 건너뜀)
            if status_index > 0:
                await _sleep_with_jitter(base_request_delay_sec)

            logger.info(
                "=== NTIS 상태 수집 시작: {} (code={}) ===",
                status_label, status_code,
            )

            # 1페이지 요청으로 totalCount 파악
            first_page_html = await _fetch_list_page_with_retry(
                client, page_index=1, status_code=status_code,
            )
            total_count = _parse_total_count(first_page_html)
            # 페이지 수 계산: ceil(totalCount / PAGE_SIZE)
            total_page_count = max(1, math.ceil(total_count / PAGE_SIZE)) if total_count > 0 else 1
            effective_max = min(total_page_count, safe_max_pages)

            logger.info(
                "NTIS [{}] 총 건수: {} → 총 페이지: {} (상한 적용 후: {})",
                status_label, total_count, total_page_count, effective_max,
            )

            # 1페이지 파싱
            page_rows = _parse_list_rows(first_page_html, status_label)
            newly_added_count = 0
            for row in page_rows:
                uid = row["ntis_announcement_id"]
                if uid not in seen_ids:
                    # normalize_ntis_status 적용 (파싱된 원문 라벨 정규화)
                    try:
                        normalized_status = normalize_ntis_status(row["status"])
                        row["status"] = normalized_status.value
                    except ValueError as exc:
                        logger.warning("NTIS 상태 정규화 실패 — 스킵: {}", exc)
                        continue
                    seen_ids.add(uid)
                    aggregated_rows.append(row)
                    newly_added_count += 1

            logger.info(
                "[{}] 페이지 1 처리 완료: 신규 {}건 (누적 {}건)",
                status_label, newly_added_count, len(aggregated_rows),
            )

            # max_announcements 상한 확인
            if max_announcements and len(aggregated_rows) >= max_announcements:
                logger.info(
                    "max_announcements({}) 상한 도달: {} 상태 1페이지 처리 후 종료",
                    max_announcements, status_label,
                )
                limit_reached = True
                break

            # max_pages 상한 경고
            if total_page_count > safe_max_pages:
                logger.warning(
                    "max_pages({}) 상한으로 인해 [{}] {} 페이지 중 {}까지만 수집함",
                    safe_max_pages, status_label, total_page_count, safe_max_pages,
                )

            # 2페이지 이후 순차 요청
            for page_index in range(2, effective_max + 1):
                # 차단 방지 딜레이
                await _sleep_with_jitter(base_request_delay_sec)

                page_html = await _fetch_list_page_with_retry(
                    client, page_index=page_index, status_code=status_code,
                )
                page_rows = _parse_list_rows(page_html, status_label)

                newly_added_count = 0
                for row in page_rows:
                    uid = row["ntis_announcement_id"]
                    if uid not in seen_ids:
                        try:
                            normalized_status = normalize_ntis_status(row["status"])
                            row["status"] = normalized_status.value
                        except ValueError as exc:
                            logger.warning("NTIS 상태 정규화 실패 — 스킵: {}", exc)
                            continue
                        seen_ids.add(uid)
                        aggregated_rows.append(row)
                        newly_added_count += 1

                logger.info(
                    "[{}] 페이지 {} 처리 완료: 신규 {}건 (누적 {}건)",
                    status_label, page_index, newly_added_count, len(aggregated_rows),
                )

                # max_announcements 상한 확인
                if max_announcements and len(aggregated_rows) >= max_announcements:
                    logger.info(
                        "max_announcements({}) 상한 도달: {} 상태 {}페이지 처리 후 종료",
                        max_announcements, status_label, page_index,
                    )
                    limit_reached = True
                    break

            logger.info(
                "=== NTIS 상태 수집 완료: {} — 누적 {}건 ===",
                status_label, len(aggregated_rows),
            )

    logger.info("NTIS scrape_list 완료: 총 {}건", len(aggregated_rows))
    return aggregated_rows


__all__ = [
    "scrape_list",
    "NTIS_BASE_DOMAIN",
    "LIST_PAGE_PATH",
    "DETAIL_VIEW_PATH",
    "STATUS_FILTER_PARAM",
    "STATUS_CODE_SCHEDULED",
    "STATUS_CODE_RECEIVING",
    "STATUS_CODE_CLOSED",
    "STATUS_LABEL_TO_API_CODE",
    "DEFAULT_STATUSES",
    "PAGE_SIZE",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_ATTEMPTS",
    "RETRY_BACKOFF_BASE_SEC",
    "PAGE_JITTER_RANGE_SEC",
]
