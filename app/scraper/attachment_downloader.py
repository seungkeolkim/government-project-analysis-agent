"""IRIS 첨부파일 다운로더 모듈.

Playwright headless 브라우저를 주 다운로드 경로로, httpx 직접 GET을 폴백으로 사용하여
공고의 첨부파일을 다운로드하고 로컬 파일시스템에 저장한다.

## 다운로드 플로우 (IRIS)
1. `detail_html`에서 `div.add_file_list a.file_down` 요소를 BeautifulSoup으로 파싱하여
   `atchDocId`, `atchFileId`, 파일명, 크기를 추출한다.
2. Playwright headless로 상세 페이지에 접속한 뒤, 각 `a.file_down`을 클릭하여
   브라우저 다운로드 API로 파일을 수신한다.
3. Playwright가 실패(브라우저 기동 불가, 요소 없음, 타임아웃 등)하면
   httpx로 `fileDownload.do?atchDocId=...&atchFileId=...`를 GET하여 폴백한다.
4. 성공한 파일은 sha256을 계산하여 반환한다.
   이미 동일 경로에 파일이 존재하면 재다운로드 없이 sha256만 계산해 반환한다.

참고: IRIS API 실측 결과는 `docs/attachment_download_plan.md`에 기록되어 있다.

반환 구조:
    AttachmentScrapeResult.success_entries: upsert_attachment() 에 바로 넣을 수 있는 dict 목록
    AttachmentScrapeResult.error_entries:   raw_metadata.attachment_errors 형식의 dict 목록
    DB 작업은 호출자(CLI 오케스트레이터, subtask 00008-4)가 담당한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from app.config import Settings, get_settings
from app.db.models import Announcement
from app.scraper.attachment_paths import build_attachment_dir, sanitize_filename
from app.scraper.iris.list_scraper import (
    DEFAULT_USER_AGENT,
    HTTP_CONNECT_TIMEOUT_SEC,
    HTTP_READ_TIMEOUT_SEC,
    IRIS_BASE_DOMAIN,
)

# ──────────────────────────────────────────────────────────────
# IRIS 첨부파일 관련 상수
# ──────────────────────────────────────────────────────────────

# IRIS 파일 다운로드 엔드포인트 (GET, 세션 인증 불필요 — 실측 확인)
IRIS_FILE_DOWNLOAD_ENDPOINT: str = f"{IRIS_BASE_DOMAIN}/comm/file/fileDownload.do"

# 첨부 파일 링크 CSS 선택자
ATTACH_LINK_SELECTOR: str = "div.add_file_list a.file_down"

# href 에서 f_bsnsAncm_downloadAtchFile 인자 추출 정규식
# href="javascript:f_bsnsAncm_downloadAtchFile('docId','fileId','파일명','크기');"
_ATTACH_FUNC_PATTERN: re.Pattern[str] = re.compile(
    r"f_bsnsAncm_downloadAtchFile\("
    r"['\"]([^'\"]+)['\"]"           # 1: atchDocId
    r"\s*,\s*['\"]([^'\"]+)['\"]"    # 2: atchFileId
    r"\s*,\s*['\"]([^'\"]*)['\"]"    # 3: original_filename
    r"\s*,\s*['\"]([^'\"]*)['\"]"    # 4: file_size_text (바이트 수 또는 텍스트)
    r"\s*\)",
    re.DOTALL,
)

# Playwright 단일 파일 다운로드 타임아웃 (ms)
PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS: int = 60_000

# Playwright 상세 페이지 로드 타임아웃 (ms)
PLAYWRIGHT_PAGE_LOAD_TIMEOUT_MS: int = 30_000

# 파일 간 다운로드 지연 범위 (초) — 차단 방지
_FILE_DOWNLOAD_JITTER_SEC: tuple[float, float] = (0.5, 1.5)

# httpx 파일 다운로드 read timeout 배수 (일반 요청보다 넉넉히)
_DOWNLOAD_READ_TIMEOUT_MULTIPLIER: int = 4


# ──────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────


@dataclass
class AttachmentLinkInfo:
    """detail_html에서 파싱한 첨부파일 링크 한 건."""

    atc_doc_id: str
    atc_file_id: str
    original_filename: str
    file_size_bytes: Optional[int]
    download_url: str


@dataclass
class DownloadResult:
    """단일 첨부파일 다운로드 결과."""

    original_filename: str
    stored_path: Optional[Path]
    file_size: Optional[int]
    sha256: Optional[str]
    download_url: Optional[str]
    downloaded_at: datetime
    success: bool
    method: Optional[Literal["playwright", "httpx", "already_exists"]]
    error_message: Optional[str] = None


@dataclass
class AttachmentScrapeResult:
    """공고 한 건에 대한 첨부파일 수집 결과.

    success_entries: upsert_attachment() 에 그대로 넘길 수 있는 payload dict 목록.
    error_entries:   raw_metadata['attachment_errors'] 에 병합할 오류 dict 목록.
    """

    announcement_id: int
    source_announcement_id: str
    source_type: str
    success_entries: list[dict[str, Any]] = field(default_factory=list)
    error_entries: list[dict[str, Any]] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼 함수
# ──────────────────────────────────────────────────────────────


def _compute_sha256(file_path: Path) -> str:
    """파일 전체의 SHA-256 해시를 hex 문자열(64자)로 반환한다."""
    sha256_hash = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def _build_download_http_client(
    settings: Settings,
    source_announcement_id: str,
) -> httpx.AsyncClient:
    """첨부파일 다운로드용 httpx.AsyncClient를 생성한다.

    Referer를 IRIS 상세 페이지 URL로 설정하여 브라우저에서 클릭한 것처럼 보이게 한다.
    """
    timeout = httpx.Timeout(
        connect=HTTP_CONNECT_TIMEOUT_SEC,
        read=HTTP_READ_TIMEOUT_SEC * _DOWNLOAD_READ_TIMEOUT_MULTIPLIER,
        write=10.0,
        pool=10.0,
    )
    referer_url = (
        f"{IRIS_BASE_DOMAIN}/contents/retrieveBsnsAncmView.do"
        f"?ancmId={source_announcement_id}"
    )
    headers = {
        "User-Agent": settings.user_agent or DEFAULT_USER_AGENT,
        "Referer": referer_url,
        "Accept": "*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }
    return httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True)


def _build_success_entry(
    *,
    announcement_id: int,
    link_info: AttachmentLinkInfo,
    stored_path: Optional[Path],
    file_size: Optional[int],
    sha256: Optional[str],
    downloaded_at: datetime,
    method: str,
) -> dict[str, Any]:
    """upsert_attachment()에 넘길 수 있는 payload dict를 구성한다.

    `_method` 키는 CLI 통계용이며, repository의 _filter_payload에서 자동으로 무시된다.
    """
    original_filename = link_info.original_filename
    file_ext = Path(original_filename).suffix.lstrip(".").lower() or "bin"

    return {
        "announcement_id": announcement_id,
        "original_filename": original_filename,
        "stored_path": str(stored_path) if stored_path else "",
        "file_ext": file_ext,
        "file_size": file_size,
        "download_url": link_info.download_url,
        "sha256": sha256,
        "downloaded_at": downloaded_at,
        "_method": method,  # CLI 통계용 메타 (repository에서 무시됨)
    }


async def _download_single_via_httpx(
    client: httpx.AsyncClient,
    link_info: AttachmentLinkInfo,
    save_path: Path,
) -> DownloadResult:
    """httpx 스트리밍 GET으로 단일 첨부파일을 다운로드하여 save_path에 저장한다.

    응답 Content-Type이 text/html이면 다운로드 실패(세션 만료/리다이렉트)로 간주한다.

    Args:
        client:    공유 httpx.AsyncClient.
        link_info: 다운로드 대상 첨부파일 정보.
        save_path: 저장할 파일의 전체 경로.

    Returns:
        DownloadResult (success=True/False).
    """
    downloaded_at = datetime.now(tz=timezone.utc)

    try:
        logger.info(
            "httpx 다운로드 시도: file={} url={}",
            link_info.original_filename,
            link_info.download_url,
        )

        async with client.stream("GET", link_info.download_url) as response:
            response.raise_for_status()

            # HTML 응답은 세션 만료/리다이렉트로 판단하여 즉시 실패 처리
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                raise ValueError(
                    f"다운로드 응답이 HTML임 (세션 만료 또는 서버 리다이렉트 가능성): "
                    f"Content-Type={content_type!r}"
                )

            save_path.parent.mkdir(parents=True, exist_ok=True)
            with save_path.open("wb") as out_file:
                async for chunk in response.aiter_bytes(65536):
                    out_file.write(chunk)

        file_size = save_path.stat().st_size
        sha256 = _compute_sha256(save_path)

        logger.info(
            "httpx 다운로드 성공: file={} size={} sha256={}...",
            link_info.original_filename,
            file_size,
            sha256[:12],
        )
        return DownloadResult(
            original_filename=link_info.original_filename,
            stored_path=save_path,
            file_size=file_size,
            sha256=sha256,
            download_url=link_info.download_url,
            downloaded_at=downloaded_at,
            success=True,
            method="httpx",
        )

    except Exception as exc:
        # 부분 저장된 파일이 남아있으면 제거한다.
        if save_path.exists():
            save_path.unlink(missing_ok=True)

        logger.warning(
            "httpx 다운로드 실패: file={} error={}",
            link_info.original_filename,
            exc,
        )
        return DownloadResult(
            original_filename=link_info.original_filename,
            stored_path=None,
            file_size=None,
            sha256=None,
            download_url=link_info.download_url,
            downloaded_at=downloaded_at,
            success=False,
            method=None,
            error_message=str(exc),
        )


async def _download_all_via_playwright(
    detail_url: str,
    attachment_links: list[AttachmentLinkInfo],
    save_dir: Path,
    *,
    settings: Settings,
) -> list[DownloadResult]:
    """Playwright headless로 상세 페이지에서 모든 첨부파일을 순차 다운로드한다.

    상세 페이지를 로드한 뒤 각 `a.file_down` 요소를 클릭하고
    브라우저 다운로드 API로 파일을 인터셉트하여 저장한다.

    라이브 DOM의 링크를 atchFileId 기준으로 input attachment_links와 매칭한다.
    매칭 실패 건은 DownloadResult(success=False)로 반환한다(httpx 폴백 대상).

    반환 목록의 길이는 항상 `attachment_links`와 동일하다.

    Args:
        detail_url:       공고 상세 페이지 전체 URL.
        attachment_links: 다운로드 대상 첨부파일 목록.
        save_dir:         저장 디렉터리.
        settings:         Settings 인스턴스.

    Returns:
        각 attachment_link에 대응하는 DownloadResult 목록 (순서 보장).
    """
    # playwright 는 런타임에 lazy import — 미설치 환경에서의 ImportError를 방지한다.
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    # 결과를 index 기준으로 관리 (순서 보장)
    results: list[Optional[DownloadResult]] = [None] * len(attachment_links)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                accept_downloads=True,
                user_agent=settings.user_agent or DEFAULT_USER_AGENT,
            )
            page = await context.new_page()

            # 상세 페이지 로드 — networkidle 대기로 동적 콘텐츠(첨부 목록)가 렌더되길 기다린다.
            logger.debug("Playwright: 상세 페이지 로드 중 url={}", detail_url)
            await page.goto(
                detail_url,
                wait_until="networkidle",
                timeout=PLAYWRIGHT_PAGE_LOAD_TIMEOUT_MS,
            )

            # 라이브 DOM에서 모든 a.file_down 의 href 를 추출한다.
            live_hrefs: list[str] = await page.eval_on_selector_all(
                ATTACH_LINK_SELECTOR,
                "elements => elements.map(el => el.getAttribute('href') || '')",
            )

            # atchFileId → DOM 인덱스 매핑 (중복 atchFileId 는 첫 번째만 사용)
            file_id_to_dom_index: dict[str, int] = {}
            for dom_idx, href in enumerate(live_hrefs):
                match = _ATTACH_FUNC_PATTERN.search(href)
                if match:
                    _, atc_file_id, *_ = match.groups()
                    if atc_file_id not in file_id_to_dom_index:
                        file_id_to_dom_index[atc_file_id] = dom_idx

            for list_idx, link_info in enumerate(attachment_links):
                downloaded_at = datetime.now(tz=timezone.utc)
                save_path = save_dir / sanitize_filename(link_info.original_filename)

                dom_index = file_id_to_dom_index.get(link_info.atc_file_id)
                if dom_index is None:
                    logger.warning(
                        "Playwright: 라이브 DOM에서 링크를 찾지 못함: "
                        "file={} atc_file_id={} (DOM에 {}개 링크 있음)",
                        link_info.original_filename,
                        link_info.atc_file_id,
                        len(live_hrefs),
                    )
                    results[list_idx] = DownloadResult(
                        original_filename=link_info.original_filename,
                        stored_path=None,
                        file_size=None,
                        sha256=None,
                        download_url=link_info.download_url,
                        downloaded_at=downloaded_at,
                        success=False,
                        method=None,
                        error_message="라이브 DOM에서 링크 요소를 찾지 못함",
                    )
                    continue

                logger.info(
                    "Playwright 다운로드 시도: file={} dom_index={}",
                    link_info.original_filename,
                    dom_index,
                )

                try:
                    # eval_on_selector_all 로 정확한 인덱스의 요소를 클릭한다.
                    # page.expect_download()가 클릭 이후 발생하는 다운로드를 인터셉트한다.
                    async with page.expect_download(
                        timeout=PLAYWRIGHT_DOWNLOAD_TIMEOUT_MS
                    ) as download_info:
                        await page.eval_on_selector_all(
                            ATTACH_LINK_SELECTOR,
                            f"elements => elements[{dom_index}].click()",
                        )

                    download = await download_info.value
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    await download.save_as(save_path)

                    file_size = save_path.stat().st_size
                    sha256 = _compute_sha256(save_path)

                    logger.info(
                        "Playwright 다운로드 성공: file={} size={} sha256={}...",
                        link_info.original_filename,
                        file_size,
                        sha256[:12],
                    )
                    results[list_idx] = DownloadResult(
                        original_filename=link_info.original_filename,
                        stored_path=save_path,
                        file_size=file_size,
                        sha256=sha256,
                        download_url=link_info.download_url,
                        downloaded_at=datetime.now(tz=timezone.utc),
                        success=True,
                        method="playwright",
                    )

                except (PlaywrightTimeoutError, Exception) as exc:
                    logger.warning(
                        "Playwright 다운로드 실패: file={} error={} → httpx 폴백 예정",
                        link_info.original_filename,
                        exc,
                    )
                    results[list_idx] = DownloadResult(
                        original_filename=link_info.original_filename,
                        stored_path=None,
                        file_size=None,
                        sha256=None,
                        download_url=link_info.download_url,
                        downloaded_at=downloaded_at,
                        success=False,
                        method=None,
                        error_message=str(exc),
                    )

                # 파일 간 지연 (차단 방지)
                if list_idx < len(attachment_links) - 1:
                    await asyncio.sleep(random.uniform(*_FILE_DOWNLOAD_JITTER_SEC))

            await browser.close()

    except Exception as exc:
        # 브라우저 기동 실패 등 전체 세션 오류 — 처리 안 된 항목 전부 실패로 채운다.
        logger.error("Playwright 브라우저 세션 실패: error={}", exc)
        for list_idx, link_info in enumerate(attachment_links):
            if results[list_idx] is None:
                results[list_idx] = DownloadResult(
                    original_filename=link_info.original_filename,
                    stored_path=None,
                    file_size=None,
                    sha256=None,
                    download_url=link_info.download_url,
                    downloaded_at=datetime.now(tz=timezone.utc),
                    success=False,
                    method=None,
                    error_message=f"Playwright 세션 오류: {exc}",
                )

    # None 이 남아있을 경우 방어적으로 처리 (정상 경로에서는 발생하지 않음)
    for list_idx, link_info in enumerate(attachment_links):
        if results[list_idx] is None:
            results[list_idx] = DownloadResult(
                original_filename=link_info.original_filename,
                stored_path=None,
                file_size=None,
                sha256=None,
                download_url=link_info.download_url,
                downloaded_at=datetime.now(tz=timezone.utc),
                success=False,
                method=None,
                error_message="알 수 없는 이유로 처리되지 않음",
            )

    return results  # type: ignore[return-value]


# ──────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────


def extract_attachment_links(detail_html: str) -> list[AttachmentLinkInfo]:
    """detail_html에서 IRIS 첨부파일 링크 정보를 추출한다.

    CSS 선택자 `div.add_file_list a.file_down`을 찾고
    href 속성의 `f_bsnsAncm_downloadAtchFile(...)` 인자를 정규식으로 파싱한다.

    Args:
        detail_html: Announcement.detail_html (div.tstyle_view 섹션 HTML).

    Returns:
        AttachmentLinkInfo 목록. 파싱에 실패한 요소는 건너뛴다.
    """
    soup = BeautifulSoup(detail_html, "html.parser")
    link_tags = soup.select(ATTACH_LINK_SELECTOR)

    if not link_tags:
        logger.debug("첨부파일 링크 없음 (선택자 매칭 0건): selector={}", ATTACH_LINK_SELECTOR)
        return []

    attachment_links: list[AttachmentLinkInfo] = []
    for tag in link_tags:
        href = tag.get("href", "")
        match = _ATTACH_FUNC_PATTERN.search(href)
        if not match:
            logger.debug("첨부파일 href 파싱 실패 (패턴 불일치): href={}", href[:120])
            continue

        atc_doc_id, atc_file_id, original_filename, file_size_text = match.groups()

        # 파일 크기: 정수 변환 가능하면 바이트 수, 아니면 None
        file_size_bytes: Optional[int] = None
        try:
            file_size_bytes = int(file_size_text)
        except (ValueError, TypeError):
            pass

        # IRIS 다운로드 URL 구성 — Base64 파라미터는 URL 인코딩 필수
        download_url = (
            f"{IRIS_FILE_DOWNLOAD_ENDPOINT}"
            f"?atchDocId={quote(atc_doc_id, safe='')}"
            f"&atchFileId={quote(atc_file_id, safe='')}"
        )

        attachment_links.append(
            AttachmentLinkInfo(
                atc_doc_id=atc_doc_id,
                atc_file_id=atc_file_id,
                original_filename=original_filename,
                file_size_bytes=file_size_bytes,
                download_url=download_url,
            )
        )

    logger.debug(
        "첨부파일 링크 추출 완료: 전체 {} 건 중 {} 건 파싱 성공",
        len(link_tags),
        len(attachment_links),
    )
    return attachment_links


async def scrape_attachments_for_announcement(
    announcement: Announcement,
    *,
    settings: Optional[Settings] = None,
) -> AttachmentScrapeResult:
    """공고 한 건의 첨부파일을 모두 다운로드하고 수집 결과를 반환한다.

    단계:
        1. detail_html에서 첨부 링크 목록을 추출한다.
        2. 이미 저장 경로에 파일이 존재하면 재다운로드 없이 sha256만 계산하여 반환한다.
        3. 없는 파일은 Playwright headless(주 경로)로 다운로드한다.
           detail_url이 없으면 Playwright를 건너뛰고 즉시 httpx 폴백으로 진행한다.
        4. Playwright 실패 건은 httpx 직접 GET으로 폴백한다.
        5. 성공/실패 결과를 AttachmentScrapeResult로 반환한다.

    DB 작업(upsert_attachment, attachment_errors 갱신)은 호출자가 담당한다.

    Args:
        announcement: is_current=True인 Announcement ORM 인스턴스.
                      detail_html이 채워져 있어야 첨부를 수집할 수 있다.
                      detail_url이 없으면 Playwright를 건너뛰고 httpx만 시도한다.
        settings:     주입할 Settings. 없으면 get_settings()를 사용한다.

    Returns:
        AttachmentScrapeResult (announcement_id, success_entries, error_entries).
    """
    effective_settings = settings or get_settings()

    result = AttachmentScrapeResult(
        announcement_id=announcement.id,
        source_announcement_id=announcement.source_announcement_id,
        source_type=announcement.source_type,
    )

    # detail_html 없으면 수집 불가
    if not announcement.detail_html:
        logger.warning(
            "첨부파일 수집 건너뜀: detail_html 없음 — announcement_id={}",
            announcement.id,
        )
        return result

    # 1. 링크 추출
    attachment_links = extract_attachment_links(announcement.detail_html)
    if not attachment_links:
        logger.debug("첨부파일 없음 (링크 0건): announcement_id={}", announcement.id)
        return result

    logger.info(
        "첨부파일 수집 시작: announcement_id={} source_id={} 파일수={}",
        announcement.id,
        announcement.source_announcement_id,
        len(attachment_links),
    )

    # 2. 저장 디렉터리 결정 (실제 생성은 다운로드 시 수행)
    save_dir = build_attachment_dir(
        effective_settings.download_dir,
        announcement.source_type,
        announcement.source_announcement_id,
    )

    # 3. 이미 로컬에 존재하는 파일 분리 — 재다운로드 없이 sha256만 반환
    pending_links: list[AttachmentLinkInfo] = []
    for link_info in attachment_links:
        save_path = save_dir / sanitize_filename(link_info.original_filename)
        if save_path.exists():
            sha256 = _compute_sha256(save_path)
            file_size = save_path.stat().st_size
            logger.debug(
                "파일 이미 존재 — 스킵: file={} sha256={}...",
                link_info.original_filename,
                sha256[:12],
            )
            result.success_entries.append(
                _build_success_entry(
                    announcement_id=announcement.id,
                    link_info=link_info,
                    stored_path=save_path,
                    file_size=file_size,
                    sha256=sha256,
                    downloaded_at=datetime.now(tz=timezone.utc),
                    method="already_exists",
                )
            )
        else:
            pending_links.append(link_info)

    if not pending_links:
        logger.info(
            "모든 첨부파일이 이미 존재함 — 다운로드 건너뜀: announcement_id={}",
            announcement.id,
        )
        return result

    # 4. Playwright 주 경로 시도
    #    detail_url 없으면 Playwright 불가 → 즉시 httpx 폴백 대상으로 분류
    playwright_results: list[DownloadResult] = []
    if announcement.detail_url:
        playwright_results = await _download_all_via_playwright(
            detail_url=announcement.detail_url,
            attachment_links=pending_links,
            save_dir=save_dir,
            settings=effective_settings,
        )
    else:
        logger.warning(
            "detail_url 없음 → Playwright 건너뛰고 httpx 폴백으로 진행: announcement_id={}",
            announcement.id,
        )
        # 모든 파일을 httpx 폴백 대상으로 표시
        playwright_results = [
            DownloadResult(
                original_filename=link_info.original_filename,
                stored_path=None,
                file_size=None,
                sha256=None,
                download_url=link_info.download_url,
                downloaded_at=datetime.now(tz=timezone.utc),
                success=False,
                method=None,
                error_message="detail_url 없음 — Playwright 불가",
            )
            for link_info in pending_links
        ]

    # 5. Playwright 실패 건 → httpx 폴백
    fallback_indices: list[int] = [
        i for i, pw_res in enumerate(playwright_results) if not pw_res.success
    ]

    if fallback_indices:
        async with _build_download_http_client(
            effective_settings,
            announcement.source_announcement_id,
        ) as httpx_client:
            for fallback_idx in fallback_indices:
                link_info = pending_links[fallback_idx]
                save_path = save_dir / sanitize_filename(link_info.original_filename)

                httpx_result = await _download_single_via_httpx(
                    httpx_client, link_info, save_path
                )
                # Playwright 실패 결과를 httpx 결과로 교체
                playwright_results[fallback_idx] = httpx_result

                # 마지막 폴백 파일이 아니면 지연 적용
                if fallback_idx != fallback_indices[-1]:
                    await asyncio.sleep(random.uniform(*_FILE_DOWNLOAD_JITTER_SEC))

    # 6. 최종 결과 집계 (pending_links 와 playwright_results 는 길이가 동일)
    for link_info, final_result in zip(pending_links, playwright_results):
        if final_result.success:
            result.success_entries.append(
                _build_success_entry(
                    announcement_id=announcement.id,
                    link_info=link_info,
                    stored_path=final_result.stored_path,
                    file_size=final_result.file_size,
                    sha256=final_result.sha256,
                    downloaded_at=final_result.downloaded_at,
                    method=final_result.method or "unknown",
                )
            )
        else:
            result.error_entries.append({
                "original_filename": link_info.original_filename,
                "atc_file_id": link_info.atc_file_id,
                "error": final_result.error_message or "알 수 없는 오류",
                "attempted_at": final_result.downloaded_at.isoformat(),
            })

    logger.info(
        "첨부파일 수집 완료: announcement_id={} 성공={} 실패={}",
        announcement.id,
        len(result.success_entries),
        len(result.error_entries),
    )

    return result


__all__ = [
    "AttachmentLinkInfo",
    "AttachmentScrapeResult",
    "DownloadResult",
    "extract_attachment_links",
    "scrape_attachments_for_announcement",
    "ATTACH_LINK_SELECTOR",
    "IRIS_FILE_DOWNLOAD_ENDPOINT",
]
