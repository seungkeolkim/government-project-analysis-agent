"""NTIS 공고 상세 페이지 스크래퍼 (httpx + BeautifulSoup).

NTIS 상세 페이지(`/rndgate/eg/un/ra/view.do?roRndUid=...`)는
SSR HTML 으로 렌더링된다. httpx GET 만으로 본문을 수집한다.

## 탐사 결과 (docs/ntis_site_exploration.md §4)

- 로그인 불필요. 게스트 GET 200 응답.
- 본문 컨테이너: `div.new_ntis_contents`
- 구조화 메타: `div.summary1`, `div.summary2` 내 `<li><span>키: </span>값</li>` 패턴
- 공고번호: `div.se-contents` 텍스트에서 정규식 파싱 (NFKC + dash 정규화 필수)
- 첨부파일: `<a onclick="fn_fileDownload('{wfUid}', '{roTextUid}')">파일명</a>` 패턴

반환 스키마 (dict):
    detail_html         : str | None  — div.new_ntis_contents 의 outer HTML
    detail_text         : str | None  — 위에서 get_text(separator='\\n', strip=True) 추출 텍스트
    detail_fetched_at   : datetime    — 수집 완료 시각 (UTC)
    detail_fetch_status : str         — 'ok' / 'empty' / 'error'
    ntis_ancm_no        : str | None  — 정규화된 공식 공고번호 (canonical_key 후보)
    ntis_meta           : dict        — 구조화 메타 필드 (공고형태, 부처명, 공고기관명 등)
    attachments         : list[dict]  — [{filename, wf_uid, ro_text_uid}]
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from loguru import logger

from app.config import Settings, get_settings
from app.scraper.ntis.list_scraper import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_USER_AGENT,
    HTTP_CONNECT_TIMEOUT_SEC,
    HTTP_READ_TIMEOUT_SEC,
    NTIS_BASE_DOMAIN,
    PAGE_JITTER_RANGE_SEC,
    RETRY_BACKOFF_BASE_SEC,
)

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# 상세 페이지 Referer (목록 페이지 URL)
DETAIL_REFERER_URL: str = NTIS_BASE_DOMAIN + "/rndgate/eg/un/ra/mng.do"

# 상세 본문 컨테이너 CSS 선택자 (탐사 §4)
DETAIL_CONTENT_SELECTOR: str = "div.new_ntis_contents"

# 공고번호 추출 정규식 (탐사 §4-3)
# \xa0(nbsp), en-dash(–, U+2013), em-dash(—, U+2014), 일반 공백, 숫자, 하이픈을 포함한다.
ANCM_NO_PATTERN: re.Pattern[str] = re.compile(
    r"[가-힣]+\s*공고\s*제\s*[\d\s\-–—\xa0]+\s*호"
)

# 첨부파일 onclick 파라미터 추출 정규식 (탐사 §6)
DOWNLOAD_ONCLICK_PATTERN: re.Pattern[str] = re.compile(
    r"fn_fileDownload\('(\d+)',\s*'([^']+)'\)"
)

# 구조화 메타를 담는 div class 목록 (순서대로 탐색)
META_DIV_CLASSES: tuple[str, ...] = ("summary1", "summary2")


# ──────────────────────────────────────────────────────────────
# 내부 파싱 유틸
# ──────────────────────────────────────────────────────────────


def _extract_ancm_no(section: Tag) -> Optional[str]:
    """section 내 div.se-contents 에서 공식 공고번호를 추출하여 정규화한다.

    탐사 §4-3 기준 정규식: `[가-힣]+공고제[...숫자/공백/대시...]호`
    정규화: NFKC(\\xa0→공백) → en/em-dash를 ASCII 하이픈으로 통일 → 공백 완전 제거.
    반환값은 canonical.py._normalize_official_key 와 idempotent 해야 한다.

    # 입력 예시: '과학기술정보통신부 공고 제 2026-0455 호' → '과학기술정보통신부공고제2026-0455호'
    # 입력 예시: '과학기술정보통신부 공고 제2026\\xa0–\\xa00484호' → '과학기술정보통신부공고제2026-0484호'

    Args:
        section: detail 본문 BeautifulSoup Tag.

    Returns:
        정규화된 공고번호 문자열. 없으면 None.
    """
    se_contents = section.find("div", class_="se-contents")
    target = se_contents if se_contents is not None else section
    text = target.get_text(separator="\n", strip=True)

    m = ANCM_NO_PATTERN.search(text)
    if m is None:
        return None

    raw = m.group(0)
    # NFKC: \xa0(nbsp) → 일반 공백, 전각 문자 반각 통일
    normalized = unicodedata.normalize("NFKC", raw)
    # en-dash(–, U+2013) / em-dash(—, U+2014) → ASCII 하이픈(-) 통일
    normalized = normalized.replace("–", "-").replace("—", "-")
    # 모든 공백 완전 제거 (canonical.py 와 동일한 처리)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized or None


def _parse_ntis_meta(section: Tag) -> dict[str, str]:
    """탐사 §4-2 의 summary1/summary2 div 패턴에서 구조화 메타를 추출한다.

    각 `<li>` 의 첫 `<span>` 을 키, 이후 sibling 텍스트를 값으로 취한다.
    `<span class="unit">억원</span>` 과 같은 nested span 도 값에 포함된다.

    # 입력 예시: <li><span>부처명 : </span>과학기술정보통신부</li> → {'부처명': '과학기술정보통신부'}
    # 입력 예시: <li><span>공고금액 : </span>15<span class="unit">억원</span></li> → {'공고금액': '15억원'}

    Returns:
        {'공고형태': '통합공고', '부처명': '과학기술정보통신부', ...} 형태의 dict.
    """
    meta: dict[str, str] = {}

    for cls in META_DIV_CLASSES:
        for div in section.find_all("div", class_=cls):
            for li in div.find_all("li"):
                label_span = li.find("span")
                if label_span is None:
                    continue
                # 키: span 텍스트에서 후행 ' : ' 또는 ':' 제거
                key = label_span.get_text(strip=True).rstrip(":").strip()
                if not key:
                    continue

                # 값: label_span 이후 sibling 순회
                value_parts: list[str] = []
                for sibling in label_span.next_siblings:
                    if isinstance(sibling, NavigableString):
                        part = str(sibling).strip()
                        if part:
                            value_parts.append(part)
                    elif isinstance(sibling, Tag):
                        part = sibling.get_text(strip=True)
                        if part:
                            value_parts.append(part)
                value = "".join(value_parts).strip()
                meta[key] = value

    return meta


def _parse_attachments(section: Tag) -> list[dict[str, str]]:
    """section 내 `fn_fileDownload(...)` onclick 링크에서 첨부파일 정보를 추출한다.

    탐사 §6-1 기준 패턴:
        <a onclick="fn_fileDownload('wfUid', 'roTextUid')">파일명.pdf</a>

    # 입력 예시: onclick="fn_fileDownload('1369403', '20260417151534778OL3HN6F8T2')"
    #           → {'filename': '붙임1. 공고문.pdf', 'wf_uid': '1369403', 'ro_text_uid': '20260417151534778OL3HN6F8T2'}

    Returns:
        [{'filename': str, 'wf_uid': str, 'ro_text_uid': str}, ...]
    """
    attachments: list[dict[str, str]] = []
    for a_tag in section.find_all("a"):
        onclick = a_tag.get("onclick") or ""
        m = DOWNLOAD_ONCLICK_PATTERN.search(onclick)
        if m is None:
            continue
        wf_uid = m.group(1)
        ro_text_uid = m.group(2)
        filename = a_tag.get_text(strip=True) or ""
        attachments.append({
            "filename": filename,
            "wf_uid": wf_uid,
            "ro_text_uid": ro_text_uid,
        })
    return attachments


def _extract_detail_section(
    html_text: str,
) -> tuple[Optional[str], Optional[str], Optional[str], dict[str, str], list[dict[str, str]]]:
    """HTML 응답에서 상세 본문 섹션을 추출하고 NTIS 전용 필드를 파싱한다.

    Args:
        html_text: httpx 로 받은 전체 HTML 문자열.

    Returns:
        (detail_html, detail_text, ntis_ancm_no, ntis_meta, attachments) 5-튜플.
        섹션을 찾지 못하면 detail_html/detail_text 는 None.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    section = soup.select_one(DETAIL_CONTENT_SELECTOR)

    if section is None:
        return None, None, None, {}, []

    detail_html: str = str(section)
    raw_text = section.get_text(separator="\n", strip=True)
    lines = [line for line in raw_text.splitlines() if line.strip()]
    detail_text: str = "\n".join(lines) or ""

    ntis_ancm_no = _extract_ancm_no(section)
    ntis_meta = _parse_ntis_meta(section)
    attachments = _parse_attachments(section)

    return detail_html, detail_text or None, ntis_ancm_no, ntis_meta, attachments


# ──────────────────────────────────────────────────────────────
# HTTP 클라이언트 + 재시도
# ──────────────────────────────────────────────────────────────


def _build_detail_http_client(settings: Settings) -> httpx.AsyncClient:
    """상세 페이지 조회용 httpx.AsyncClient 를 생성한다."""
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


async def _fetch_detail_page(
    client: httpx.AsyncClient,
    detail_url: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_sec: float = RETRY_BACKOFF_BASE_SEC,
) -> str:
    """상세 페이지 HTML 을 지수 백오프로 재시도하며 가져온다.

    httpx.RequestError 발생 시에만 재시도한다. HTTP 4xx/5xx 는 즉시 전파한다.
    """
    effective_max = max(int(max_attempts), 1)
    last_exc: Optional[BaseException] = None

    for attempt_index in range(1, effective_max + 1):
        try:
            logger.debug("NTIS 상세 페이지 GET: url={}", detail_url)
            response = await client.get(detail_url)
            response.raise_for_status()
            return response.text
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt_index >= effective_max:
                logger.error(
                    "NTIS 상세 페이지 최종 시도 {}/{} 실패: url={} error={}",
                    attempt_index, effective_max, detail_url, exc,
                )
                raise
            wait_sec = backoff_base_sec * (2 ** (attempt_index - 1))
            logger.warning(
                "NTIS 상세 페이지 시도 {}/{} 실패({}): {:.1f}초 후 재시도 — url={}",
                attempt_index, effective_max, type(exc).__name__, wait_sec, detail_url,
            )
            await asyncio.sleep(wait_sec)

    assert last_exc is not None
    raise last_exc


# ──────────────────────────────────────────────────────────────
# 공개 엔트리포인트
# ──────────────────────────────────────────────────────────────


async def scrape_detail(
    detail_url: str,
    *,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """NTIS 공고 상세 페이지를 수집해 결과 dict 를 반환한다.

    내부적으로 httpx 클라이언트를 생성·소멸한다. 대량 수집 시에는
    `scrape_detail_with_client` 를 사용해 클라이언트를 재사용한다.

    Args:
        detail_url: 공고 상세 페이지 전체 URL.
        settings:   주입할 Settings. 없으면 get_settings() 를 사용한다.

    Returns:
        {
          "detail_html": str | None,
          "detail_text": str | None,
          "detail_fetched_at": datetime,
          "detail_fetch_status": "ok" | "empty" | "error",
          "ntis_ancm_no": str | None,
          "ntis_meta": dict[str, str],
          "attachments": list[dict[str, str]],
        }
    """
    effective_settings = settings or get_settings()
    async with _build_detail_http_client(effective_settings) as client:
        return await scrape_detail_with_client(client, detail_url)


async def scrape_detail_with_client(
    client: httpx.AsyncClient,
    detail_url: str,
) -> dict[str, Any]:
    """주어진 httpx 클라이언트로 NTIS 상세 페이지를 수집한다.

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
          "ntis_ancm_no": str | None,
          "ntis_meta": dict[str, str],
          "attachments": list[dict[str, str]],
        }
    """
    fetched_at = datetime.now(tz=timezone.utc)
    _empty_extras: dict[str, Any] = {"ntis_ancm_no": None, "ntis_meta": {}, "attachments": []}

    try:
        html_text = await _fetch_detail_page(client, detail_url)
    except Exception as exc:
        logger.warning("NTIS 상세 수집 실패 — error 기록: url={} ({})", detail_url, exc)
        return {
            "detail_html": None,
            "detail_text": None,
            "detail_fetched_at": fetched_at,
            "detail_fetch_status": "error",
            **_empty_extras,
        }

    detail_html, detail_text, ntis_ancm_no, ntis_meta, attachments = _extract_detail_section(html_text)

    if detail_html is None:
        logger.warning(
            "NTIS 상세 페이지에서 '{}' 섹션을 찾지 못함 — 'empty' 기록: url={}",
            DETAIL_CONTENT_SELECTOR, detail_url,
        )
        return {
            "detail_html": None,
            "detail_text": None,
            "detail_fetched_at": fetched_at,
            "detail_fetch_status": "empty",
            **_empty_extras,
        }

    logger.debug(
        "NTIS 상세 수집 성공: url={} html_len={} text_len={} ancm_no={} attachments={}",
        detail_url,
        len(detail_html),
        len(detail_text) if detail_text else 0,
        ntis_ancm_no,
        len(attachments),
    )
    return {
        "detail_html": detail_html,
        "detail_text": detail_text,
        "detail_fetched_at": fetched_at,
        "detail_fetch_status": "ok",
        "ntis_ancm_no": ntis_ancm_no,
        "ntis_meta": ntis_meta,
        "attachments": attachments,
    }


__all__ = [
    "scrape_detail",
    "scrape_detail_with_client",
    "DETAIL_CONTENT_SELECTOR",
    "DETAIL_REFERER_URL",
    "ANCM_NO_PATTERN",
    "DOWNLOAD_ONCLICK_PATTERN",
]
