"""IRIS 공고 상세 페이지 스크래퍼 (httpx + BeautifulSoup).

IRIS 상세 페이지(`/contents/retrieveBsnsAncmView.do?ancmId=...`)는
서버사이드 렌더링으로 공고 본문이 HTML에 포함된다(JS 렌더 불필요).
→ httpx GET 만으로 `div.tstyle_view` 전체를 수집한다.

## 탐사 결과 (2026-04-20)
- HTTP 200 응답에 `div.tstyle_view` 섹션이 존재하고 공고 상세 정보 포함.
- 첨부파일 다운로드 링크는 `javascript:f_bsnsAncm_downloadAtchFile(atchDocId, atchFileId, ...)` 형식이며,
  실제 다운로드 시 AJAX POST (`/comm/file/retrieveCheckFileDownload.do`)를 거쳐
  `/comm/file/fileDownload.do?atchDocId=...&atchFileId=...` 로 다운로드된다.
  → 이 구조는 00003-3 설계 문서에서 상세히 다룬다.

반환 스키마 (dict):
    detail_html         : str | None  — div.tstyle_view 의 outer HTML
    detail_text         : str | None  — 위에서 추출한 가독성 텍스트
    detail_fetched_at   : datetime    — 수집 완료 시각 (UTC)
    detail_fetch_status : str         — 'ok' / 'empty' / 'error'
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from app.config import Settings, get_settings
from app.scraper.iris.list_scraper import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_USER_AGENT,
    HTTP_CONNECT_TIMEOUT_SEC,
    HTTP_READ_TIMEOUT_SEC,
    IRIS_BASE_DOMAIN,
    PAGE_JITTER_RANGE_SEC,
    RETRY_BACKOFF_BASE_SEC,
)

# 상세 페이지 Referer: 목록 뷰 URL (브라우저처럼 보이기 위해)
DETAIL_REFERER_URL: str = (
    "https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituListView.do"
)

# 상세 본문을 감싸는 섹션 CSS 선택자
DETAIL_CONTENT_SELECTOR: str = "div.tstyle_view"


def _build_detail_http_client(settings: Settings) -> httpx.AsyncClient:
    """상세 페이지 조회용 httpx.AsyncClient 를 생성한다.

    목록 스크래퍼와 동일한 User-Agent/Accept-Language 를 사용하되,
    Accept 는 HTML 문서용으로, Referer 는 목록 페이지 URL 로 설정한다.
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
        "Referer": DETAIL_REFERER_URL,
    }
    return httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True)


def _extract_detail_section(html_text: str) -> tuple[Optional[str], Optional[str]]:
    """HTML 응답에서 상세 본문 섹션을 추출한다.

    Args:
        html_text: httpx 로 받은 전체 HTML 문자열.

    Returns:
        (detail_html, detail_text) 튜플.
        - detail_html : div.tstyle_view 의 outer HTML 문자열. 없으면 None.
        - detail_text : 위에서 공백/줄바꿈 정리한 가독성 텍스트. 없으면 None.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    section = soup.select_one(DETAIL_CONTENT_SELECTOR)

    if section is None:
        return None, None

    detail_html: str = str(section)

    # 공백 노이즈를 줄이면서 줄바꿈을 구분자로 텍스트 추출
    raw_text = section.get_text(separator="\n", strip=True)
    # 연속 빈 줄을 한 줄로 압축
    lines = [line for line in raw_text.splitlines() if line.strip()]
    detail_text: str = "\n".join(lines)

    return detail_html, detail_text or None


async def _fetch_detail_page(
    client: httpx.AsyncClient,
    detail_url: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_sec: float = RETRY_BACKOFF_BASE_SEC,
) -> str:
    """상세 페이지 HTML 을 지수 백오프로 재시도하며 가져온다.

    httpx.RequestError (타임아웃 포함) 발생 시에만 재시도한다.
    HTTP 4xx/5xx 는 재시도 없이 즉시 전파한다.

    Args:
        client:          재사용할 httpx.AsyncClient.
        detail_url:      상세 페이지 전체 URL.
        max_attempts:    최대 시도 횟수.
        backoff_base_sec: 지수 백오프 기본 대기 초.

    Returns:
        응답 HTML 문자열.

    Raises:
        httpx.HTTPStatusError: HTTP 오류 응답.
        httpx.RequestError:    재시도 한도 초과 연결 실패.
    """
    effective_max = max(int(max_attempts), 1)
    last_exc: Optional[BaseException] = None

    for attempt_index in range(1, effective_max + 1):
        try:
            logger.debug("상세 페이지 GET: url={}", detail_url)
            response = await client.get(detail_url)
            response.raise_for_status()
            return response.text
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt_index >= effective_max:
                logger.error(
                    "상세 페이지 최종 시도 {}/{} 실패: url={} error={}",
                    attempt_index,
                    effective_max,
                    detail_url,
                    exc,
                )
                raise
            wait_sec = backoff_base_sec * (2 ** (attempt_index - 1))
            logger.warning(
                "상세 페이지 시도 {}/{} 실패({}): {:.1f}초 후 재시도 — url={}",
                attempt_index,
                effective_max,
                type(exc).__name__,
                wait_sec,
                detail_url,
            )
            await asyncio.sleep(wait_sec)

    assert last_exc is not None
    raise last_exc


async def scrape_detail(
    detail_url: str,
    *,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """IRIS 공고 상세 페이지를 수집해 결과 dict 를 반환한다.

    내부적으로 httpx 클라이언트를 생성·소멸하므로, 대량 수집 시에는
    `scrape_detail_with_client` 를 사용해 클라이언트를 재사용한다.

    Args:
        detail_url: 공고 상세 페이지 전체 URL (https://...).
        settings:   주입할 Settings. 없으면 get_settings() 를 사용한다.

    Returns:
        {
          "detail_html": str | None,
          "detail_text": str | None,
          "detail_fetched_at": datetime,
          "detail_fetch_status": "ok" | "empty" | "error",
        }
    """
    effective_settings = settings or get_settings()
    async with _build_detail_http_client(effective_settings) as client:
        return await scrape_detail_with_client(client, detail_url)


async def scrape_detail_with_client(
    client: httpx.AsyncClient,
    detail_url: str,
) -> dict[str, Any]:
    """주어진 httpx 클라이언트로 상세 페이지를 수집한다.

    CLI 오케스트레이터가 클라이언트를 재사용할 수 있도록 클라이언트를 외부에서 주입받는다.
    예외를 내부에서 흡수하고 `detail_fetch_status='error'` 로 반환한다.

    Args:
        client:     재사용 가능한 httpx.AsyncClient.
        detail_url: 공고 상세 페이지 전체 URL.

    Returns:
        {
          "detail_html": str | None,
          "detail_text": str | None,
          "detail_fetched_at": datetime,
          "detail_fetch_status": "ok" | "empty" | "error",
        }
    """
    fetched_at = datetime.now(tz=timezone.utc)

    try:
        html_text = await _fetch_detail_page(client, detail_url)
    except Exception as exc:
        logger.warning("상세 수집 실패 — error로 기록: url={} ({})", detail_url, exc)
        return {
            "detail_html": None,
            "detail_text": None,
            "detail_fetched_at": fetched_at,
            "detail_fetch_status": "error",
        }

    detail_html, detail_text = _extract_detail_section(html_text)

    if detail_html is None:
        # 서버 응답은 성공했지만 기대하는 섹션이 없음 → JS 렌더 의존 가능성
        logger.warning(
            "상세 페이지에서 '{}' 섹션을 찾지 못함 — 'empty' 기록: url={}",
            DETAIL_CONTENT_SELECTOR,
            detail_url,
        )
        return {
            "detail_html": None,
            "detail_text": None,
            "detail_fetched_at": fetched_at,
            "detail_fetch_status": "empty",
        }

    logger.debug(
        "상세 수집 성공: url={} html_len={} text_len={}",
        detail_url,
        len(detail_html),
        len(detail_text) if detail_text else 0,
    )
    return {
        "detail_html": detail_html,
        "detail_text": detail_text,
        "detail_fetched_at": fetched_at,
        "detail_fetch_status": "ok",
    }


async def _jitter_sleep() -> None:
    """PAGE_JITTER_RANGE_SEC 범위의 지터만큼 비동기 대기한다."""
    jitter_min, jitter_max = PAGE_JITTER_RANGE_SEC
    await asyncio.sleep(random.uniform(jitter_min, jitter_max))


__all__ = [
    "scrape_detail",
    "scrape_detail_with_client",
    "DETAIL_CONTENT_SELECTOR",
    "DETAIL_REFERER_URL",
]
