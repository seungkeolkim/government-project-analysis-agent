"""IRIS 사업공고 목록 페이지 스크래퍼.

Playwright(chromium, headless) 로 IRIS 사업공고 목록 페이지를 열어
'접수중' 상태의 공고 메타데이터만 수집한다. 상세 내용과 첨부파일
수집은 후속 subtask 에서 수행하며, 이 모듈은 상세 페이지 조회에 필요한
식별자와 화면에 노출되는 기본 필드만 추출한다.

반환 스키마(list[dict]) — 각 원소는 아래 키를 갖는다.
    - iris_announcement_id: IRIS 가 부여한 공고 고유 ID (필수)
    - title:                공고 제목
    - agency:               주관/공고 기관명 (알 수 없으면 None)
    - status:               '접수중/접수예정/마감' 원문 (알 수 없으면 None)
    - received_at_text:     접수시작 일시 원문 텍스트 (알 수 없으면 None)
    - deadline_at_text:     접수마감 일시 원문 텍스트 (알 수 없으면 None)
    - detail_url:           상세 페이지 href 원문 (알 수 없으면 None)
    - detail_onclick:       상세를 여는 onclick 스크립트 원문 (알 수 없으면 None)
    - row_html:             원본 row HTML (디버깅/추가 파싱용 원본 보존)

IRIS DOM 은 사전 공지 없이 변경될 수 있으므로, 이 모듈에서 사용하는
모든 CSS/XPath 셀렉터와 재시도 상수는 파일 상단의 상수 블록에 모아
둔다. 이후 DOM 변경 시 이 상수들만 교체하면 된다.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Optional

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from playwright.async_api import (
    async_playwright,
)

from app.config import Settings, get_settings

# ──────────────────────────────────────────────────────────────
# 셀렉터 상수 블록 (IRIS DOM 변경 시 이 블록만 교체)
# ──────────────────────────────────────────────────────────────

# '접수중' 필터(체크박스/탭/라벨)를 가리키는 후보 셀렉터.
# 위에서부터 순차 탐색하여 처음으로 visible 한 요소를 클릭한다.
SELECTOR_RECEIVING_FILTER_CANDIDATES: tuple[str, ...] = (
    "label:has-text('접수중')",
    "input[type='checkbox'][value='접수중']",
    "a:has-text('접수중')",
    "button:has-text('접수중')",
)

# 필터 적용 후 렌더링이 완료되었음을 확인할 결과 영역 셀렉터.
SELECTOR_LIST_READY: str = "table tbody"

# 결과 테이블의 각 row(공고 한 건) 셀렉터.
SELECTOR_LIST_ROW: str = "table tbody tr"

# row 안에서 상세로 이동하는 링크 후보 셀렉터.
SELECTOR_ROW_TITLE_LINK: str = "a"

# '다음 페이지' 버튼 후보 셀렉터. 순차 탐색한다.
SELECTOR_NEXT_PAGE_CANDIDATES: tuple[str, ...] = (
    "a:has-text('다음')",
    "button:has-text('다음')",
    "a[aria-label='다음']",
    ".pagination .next",
    "a.next",
)

# onclick="fnViewAncm('ABC123', ...)" 스크립트에서 첫 따옴표 인자를 뽑는 정규식.
# IRIS 는 목록에서 상세로 이동할 때 JS 함수에 공고 id 를 실어 호출하는 패턴이 일반적이다.
ONCLICK_PARAM_PATTERN: re.Pattern[str] = re.compile(r"""['"]([^'"]+)['"]""")

# 'YYYY-MM-DD[ HH:MM[:SS]]' 패턴. 날짜 셀 탐지에 사용.
DATE_TEXT_PATTERN: re.Pattern[str] = re.compile(
    r"\d{4}[-./]\d{2}[-./]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?"
)

# 한 번의 스크래핑에서 순회할 최대 페이지 수(무한 루프 방어 상한).
DEFAULT_MAX_PAGES: int = 50

# 재시도 기본 상수.
DEFAULT_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_SEC: float = 2.0

# 페이지 간 지터(추가 지연) 범위(초).
PAGE_JITTER_RANGE_SEC: tuple[float, float] = (0.5, 1.5)

# 네비게이션 타임아웃 하한(ms). settings 값이 더 크면 그 값을 쓴다.
MINIMUM_NAVIGATION_TIMEOUT_MS: int = 60_000

# 한국 공공 사이트 대응용 기본 locale / timezone.
DEFAULT_LOCALE: str = "ko-KR"
DEFAULT_TIMEZONE: str = "Asia/Seoul"

# settings.user_agent 가 빈 문자열일 때 사용할 기본 User-Agent.
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 상태 셀에서 그대로 매칭할 키워드 집합.
STATUS_KEYWORDS: tuple[str, ...] = ("접수중", "접수예정", "마감")


# ──────────────────────────────────────────────────────────────
# 유틸 함수
# ──────────────────────────────────────────────────────────────


async def _sleep_with_jitter(base_delay_sec: float) -> None:
    """base_delay_sec + 균등분포 지터 만큼 비동기 대기한다.

    지터 범위는 `PAGE_JITTER_RANGE_SEC` 에 정의되어 있으며,
    차단 방지를 위해 페이지 이동 사이마다 삽입된다.
    """
    safe_base = max(float(base_delay_sec), 0.0)
    jitter_min, jitter_max = PAGE_JITTER_RANGE_SEC
    jitter_sec = random.uniform(jitter_min, jitter_max)
    await asyncio.sleep(safe_base + jitter_sec)


async def _with_retry(
    operation: Callable[[], Awaitable[Any]],
    *,
    description: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_sec: float = RETRY_BACKOFF_BASE_SEC,
) -> Any:
    """비동기 작업을 지수 백오프로 재시도한다.

    Playwright 의 TimeoutError / Error 및 `asyncio.TimeoutError` 를 포착하고,
    최대 `max_attempts` 회까지 `backoff_base_sec * 2^(attempt-1)` 초 만큼
    대기한 뒤 재시도한다. 마지막 시도에서도 실패하면 예외를 그대로 전파한다.

    Args:
        operation:         인자 없이 호출되는 비동기 작업.
        description:       로그 표시용 작업 이름.
        max_attempts:      최대 시도 횟수 (1 미만은 1 로 보정).
        backoff_base_sec:  지수 백오프 기본 단위(초).

    Returns:
        `operation` 이 성공적으로 반환한 값.

    Raises:
        `operation` 이 마지막 시도에서 일으킨 예외를 그대로 다시 던진다.
    """
    effective_max = max(int(max_attempts), 1)
    last_exc: Optional[BaseException] = None

    for attempt_index in range(1, effective_max + 1):
        try:
            return await operation()
        except (PlaywrightTimeoutError, PlaywrightError, asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt_index >= effective_max:
                logger.error(
                    "[{}] 최종 시도 {}/{} 실패: {}",
                    description,
                    attempt_index,
                    effective_max,
                    exc,
                )
                raise
            wait_sec = backoff_base_sec * (2 ** (attempt_index - 1))
            logger.warning(
                "[{}] 시도 {}/{} 실패({}): {:.1f}초 후 재시도",
                description,
                attempt_index,
                effective_max,
                type(exc).__name__,
                wait_sec,
            )
            await asyncio.sleep(wait_sec)

    # 도달할 수 없는 경로 — 방어적 raise.
    assert last_exc is not None
    raise last_exc


@asynccontextmanager
async def _open_browser_context(
    settings: Settings,
) -> AsyncIterator[tuple[Browser, BrowserContext, Page]]:
    """Playwright chromium 브라우저·컨텍스트·페이지를 연다.

    동작:
        - `settings.headless`, `settings.user_agent` 값을 반영한다.
        - navigation/기본 timeout 을 60s 이상으로 강제한다.
        - 컨텍스트에 `Accept-Language` 헤더와 `ko-KR / Asia/Seoul` locale/timezone 을 설정한다.
        - with 블록을 빠져나갈 때 page → context → browser 순으로 닫는다.

    Yields:
        `(browser, context, page)` 튜플.
    """
    requested_nav_ms = max(int(settings.navigation_timeout_ms), MINIMUM_NAVIGATION_TIMEOUT_MS)
    effective_user_agent = settings.user_agent or DEFAULT_USER_AGENT

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        try:
            context = await browser.new_context(
                user_agent=effective_user_agent,
                locale=DEFAULT_LOCALE,
                timezone_id=DEFAULT_TIMEZONE,
                extra_http_headers={
                    "Accept-Language": f"{DEFAULT_LOCALE},ko;q=0.9,en;q=0.8",
                },
            )
            try:
                context.set_default_navigation_timeout(requested_nav_ms)
                context.set_default_timeout(requested_nav_ms)
                page = await context.new_page()
                try:
                    yield browser, context, page
                finally:
                    await page.close()
            finally:
                await context.close()
        finally:
            await browser.close()


def _extract_onclick_param(onclick_text: Optional[str]) -> Optional[str]:
    """onclick 속성 문자열에서 첫 번째 따옴표 인자를 추출한다.

    예) `fnViewAncm('ABC123','2024')` → `'ABC123'`
    인자가 없거나 onclick 텍스트가 비어 있으면 `None` 을 반환한다.
    """
    if not onclick_text:
        return None
    match = ONCLICK_PARAM_PATTERN.search(onclick_text)
    return match.group(1) if match else None


# ──────────────────────────────────────────────────────────────
# 페이지 단위 동작
# ──────────────────────────────────────────────────────────────


async def _wait_for_list_rendered(page: Page) -> None:
    """목록 테이블이 화면에 그려질 때까지 기다린다."""
    await page.wait_for_selector(SELECTOR_LIST_READY, state="visible")


async def _click_receiving_filter(page: Page) -> bool:
    """'접수중' 상태 필터 UI 를 클릭한다.

    `SELECTOR_RECEIVING_FILTER_CANDIDATES` 를 순차 시도하여
    처음으로 visible 한 요소를 클릭한다. 클릭 후에는 AJAX 재렌더를
    기다리기 위해 `networkidle` 과 결과 영역 재표시를 동기화한다.

    Returns:
        필터를 실제로 클릭했으면 True. 어떤 후보 셀렉터도 찾지 못했으면 False.
    """
    for candidate_selector in SELECTOR_RECEIVING_FILTER_CANDIDATES:
        candidate_locator = page.locator(candidate_selector).first
        try:
            if await candidate_locator.count() == 0:
                continue
            if not await candidate_locator.is_visible():
                continue
        except PlaywrightError:
            continue

        logger.info("접수중 필터 클릭: selector={}", candidate_selector)
        await candidate_locator.click()

        try:
            await page.wait_for_load_state("networkidle")
        except PlaywrightTimeoutError:
            # networkidle 이 끝내 만족되지 않더라도 치명적이지 않으므로 경고만 남긴다.
            logger.warning("필터 적용 후 networkidle 대기 타임아웃 — 렌더링 계속 진행")

        await _wait_for_list_rendered(page)
        return True

    logger.warning(
        "'접수중' 필터 셀렉터를 찾지 못했다. 후보={}",
        SELECTOR_RECEIVING_FILTER_CANDIDATES,
    )
    return False


async def _extract_row_metadata(row_locator: Any, row_index: int) -> Optional[dict[str, Any]]:
    """하나의 row 에서 메타데이터를 추출해 dict 로 반환한다.

    IRIS 의 정확한 컬럼 순서를 단정할 수 없어 다음 휴리스틱을 사용한다:
        - 제목/상세 링크: row 안에서 텍스트가 가장 긴 `<a>` 요소.
        - 상태: '접수중/접수예정/마감' 텍스트를 그대로 포함하는 셀.
        - 접수시작/마감: 'YYYY-MM-DD' 패턴 매칭. 앞이 시작, 뒤가 마감.
        - 기관명: 제목/상태/날짜/순번에 해당하지 않는 첫 번째 텍스트 셀.

    row 에 상세 링크 단서가 전혀 없으면(헤더 행 등) `None` 을 반환한다.

    Args:
        row_locator:  Playwright Locator (단일 row).
        row_index:    로깅용 row 인덱스.

    Returns:
        메타데이터 dict 또는 None.
    """
    # 원본 HTML 을 먼저 확보한다. inner_html 실패는 DOM 탈락(리렌더) 신호이므로 스킵.
    try:
        row_html = await row_locator.inner_html()
    except PlaywrightError as exc:
        logger.warning("row {} inner_html 실패 — 스킵: {}", row_index, exc)
        return None

    # ── 제목 링크 탐색 (텍스트 가장 긴 a 를 대표 링크로 간주) ──
    link_locator = row_locator.locator(SELECTOR_ROW_TITLE_LINK)
    link_count = await link_locator.count()

    title_text: Optional[str] = None
    detail_href: Optional[str] = None
    detail_onclick: Optional[str] = None
    best_text_length = -1

    for link_index in range(link_count):
        single_link = link_locator.nth(link_index)
        try:
            link_inner_text = (await single_link.inner_text()).strip()
        except PlaywrightError:
            continue
        if len(link_inner_text) > best_text_length:
            best_text_length = len(link_inner_text)
            title_text = link_inner_text
            try:
                detail_href = await single_link.get_attribute("href")
            except PlaywrightError:
                detail_href = None
            try:
                detail_onclick = await single_link.get_attribute("onclick")
            except PlaywrightError:
                detail_onclick = None

    # ── 공고 id 추출: onclick → javascript:href → href 원문 순서 ──
    iris_announcement_id: Optional[str] = _extract_onclick_param(detail_onclick)
    if iris_announcement_id is None and detail_href:
        if detail_href.startswith("javascript:"):
            iris_announcement_id = _extract_onclick_param(detail_href)
        else:
            # 쿼리스트링/패스에 식별자가 묻혀 있는 경우를 위한 최후 수단.
            # 상세 scraper 가 이 값을 다시 해석할 수 있도록 href 원문을 그대로 사용.
            iris_announcement_id = detail_href

    if not iris_announcement_id or not title_text:
        # 식별자나 제목이 없으면 유효 row 가 아니다(헤더/빈 행 등).
        return None

    # ── 셀 텍스트 전수 수집 ──
    cell_locator = row_locator.locator("td")
    cell_count = await cell_locator.count()
    cell_texts: list[str] = []
    for cell_index in range(cell_count):
        try:
            cell_raw_text = await cell_locator.nth(cell_index).inner_text()
        except PlaywrightError:
            cell_raw_text = ""
        cell_texts.append(cell_raw_text.strip())

    # ── 상태 키워드 매칭 ──
    status_text: Optional[str] = None
    for cell_text in cell_texts:
        if cell_text in STATUS_KEYWORDS:
            status_text = cell_text
            break

    # ── 날짜 패턴 매칭(여러 셀에 걸쳐 순서대로 수집) ──
    collected_dates: list[str] = []
    for cell_text in cell_texts:
        collected_dates.extend(DATE_TEXT_PATTERN.findall(cell_text))

    received_at_text = collected_dates[0] if len(collected_dates) >= 1 else None
    deadline_at_text = collected_dates[1] if len(collected_dates) >= 2 else None

    # ── 기관명 휴리스틱 ──
    agency_text: Optional[str] = None
    for cell_text in cell_texts:
        if not cell_text:
            continue
        if cell_text == title_text:
            continue
        if cell_text == status_text:
            continue
        if DATE_TEXT_PATTERN.search(cell_text):
            continue
        if cell_text.isdigit():
            # 순번 컬럼(숫자만)은 제외.
            continue
        agency_text = cell_text
        break

    return {
        "iris_announcement_id": iris_announcement_id,
        "title": title_text,
        "agency": agency_text,
        "status": status_text,
        "received_at_text": received_at_text,
        "deadline_at_text": deadline_at_text,
        "detail_url": detail_href,
        "detail_onclick": detail_onclick,
        "row_html": row_html,
    }


async def _collect_rows_in_current_page(page: Page) -> list[dict[str, Any]]:
    """현재 페이지의 모든 row 를 읽어 메타데이터 리스트를 만든다.

    '접수중' 이 아닌 row 는 필터링해서 제외한다(상태를 판정하지 못한 row 는
    일단 통과시킨다 — 필터 UI 가 이미 제대로 걸려 있다고 가정).
    """
    await _wait_for_list_rendered(page)

    row_locator = page.locator(SELECTOR_LIST_ROW)
    total_row_count = await row_locator.count()
    logger.info("현재 페이지 row 수: {}건", total_row_count)

    collected_rows: list[dict[str, Any]] = []
    for row_index in range(total_row_count):
        single_row = row_locator.nth(row_index)
        row_metadata = await _extract_row_metadata(single_row, row_index)
        if row_metadata is None:
            continue

        # 상태 판별이 됐고 '접수중' 이 아니면 제외.
        row_status = row_metadata["status"]
        if row_status is not None and row_status != "접수중":
            continue

        collected_rows.append(row_metadata)

    logger.info("현재 페이지 유효 row: {}건", len(collected_rows))
    return collected_rows


async def _try_go_next_page(page: Page) -> bool:
    """다음 페이지로 이동을 시도한다.

    후보 셀렉터를 순회하여 visible & enabled 인 버튼이 있으면 클릭하고 True.
    다음 페이지가 없으면 False.
    """
    for candidate_selector in SELECTOR_NEXT_PAGE_CANDIDATES:
        candidate_locator = page.locator(candidate_selector).first
        try:
            if await candidate_locator.count() == 0:
                continue
            if not await candidate_locator.is_visible():
                continue
            if not await candidate_locator.is_enabled():
                continue
        except PlaywrightError:
            continue

        logger.info("다음 페이지 이동: selector={}", candidate_selector)
        await candidate_locator.click()
        try:
            await page.wait_for_load_state("networkidle")
        except PlaywrightTimeoutError:
            logger.warning("다음 페이지 이동 후 networkidle 대기 타임아웃")
        await _wait_for_list_rendered(page)
        return True

    return False


# ──────────────────────────────────────────────────────────────
# 공개 엔트리포인트
# ──────────────────────────────────────────────────────────────


async def scrape_list(
    *,
    settings: Optional[Settings] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[dict[str, Any]]:
    """IRIS 목록 페이지에서 '접수중' 공고 메타데이터를 수집한다.

    동작 순서:
        1. chromium headless 브라우저/컨텍스트/페이지를 연다.
        2. 목록 URL 로 이동한 뒤 '접수중' 필터를 클릭한다 (실패 시 지수 백오프 재시도).
        3. 현재 페이지의 모든 row 에서 메타데이터를 수집한다.
        4. `REQUEST_DELAY_SEC + 0.5~1.5s 지터` 만큼 대기 후 다음 페이지로 이동.
        5. 다음 페이지 버튼이 없거나 `max_pages` 에 도달하면 종료.
        6. 누적된 list[dict] 를 반환한다(공고 id 기준 중복 제거).

    Args:
        settings:   주입할 `Settings`. 없으면 `get_settings()` 를 사용한다.
        max_pages:  순회 안전 상한. 기본값 `DEFAULT_MAX_PAGES`.

    Returns:
        각 공고의 메타데이터 dict 를 모은 리스트. 상세/첨부 수집 전 단계의 결과.

    Raises:
        PlaywrightError 또는 그 서브클래스:
            초기 페이지 이동/필터 적용이 재시도 한도 내에서도 실패한 경우.
    """
    effective_settings = settings or get_settings()
    base_request_delay_sec = effective_settings.request_delay_sec
    safe_max_pages = max(int(max_pages), 1)

    async with _open_browser_context(effective_settings) as (_browser, _context, page):

        async def goto_and_apply_filter() -> None:
            """목록 URL 이동 + '접수중' 필터 클릭 (재시도 단위로 묶는다)."""
            logger.info("IRIS 목록 페이지 이동: {}", effective_settings.base_url)
            await page.goto(effective_settings.base_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle")
            except PlaywrightTimeoutError:
                logger.warning("초기 networkidle 대기 타임아웃 — 진행")
            await _wait_for_list_rendered(page)
            await _click_receiving_filter(page)

        await _with_retry(goto_and_apply_filter, description="goto+filter")

        aggregated_rows: list[dict[str, Any]] = []
        seen_announcement_ids: set[str] = set()

        for page_index in range(1, safe_max_pages + 1):
            current_page_number = page_index

            async def collect_current_page() -> list[dict[str, Any]]:
                """현재 페이지 수집을 재시도 단위로 감싼다."""
                return await _collect_rows_in_current_page(page)

            current_page_rows = await _with_retry(
                collect_current_page,
                description=f"collect page {current_page_number}",
            )

            # 중복 제거: 동일 공고 id 는 한 번만 누적한다.
            newly_added_count = 0
            for row_metadata in current_page_rows:
                announcement_id = row_metadata["iris_announcement_id"]
                if announcement_id in seen_announcement_ids:
                    continue
                seen_announcement_ids.add(announcement_id)
                aggregated_rows.append(row_metadata)
                newly_added_count += 1

            logger.info(
                "페이지 {} 신규 {}건 (누적 {}건)",
                current_page_number,
                newly_added_count,
                len(aggregated_rows),
            )

            # 차단 방지: request_delay_sec + 페이지 지터.
            await _sleep_with_jitter(base_request_delay_sec)

            moved_to_next = await _try_go_next_page(page)
            if not moved_to_next:
                logger.info(
                    "다음 페이지 없음 — 순회 종료 (총 {}건)",
                    len(aggregated_rows),
                )
                break
        else:
            logger.warning(
                "max_pages({}) 상한 도달 — 순회 강제 종료",
                safe_max_pages,
            )

        return aggregated_rows


__all__ = [
    "scrape_list",
    "SELECTOR_RECEIVING_FILTER_CANDIDATES",
    "SELECTOR_LIST_READY",
    "SELECTOR_LIST_ROW",
    "SELECTOR_ROW_TITLE_LINK",
    "SELECTOR_NEXT_PAGE_CANDIDATES",
    "ONCLICK_PARAM_PATTERN",
    "DATE_TEXT_PATTERN",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_ATTEMPTS",
    "RETRY_BACKOFF_BASE_SEC",
    "PAGE_JITTER_RANGE_SEC",
]
