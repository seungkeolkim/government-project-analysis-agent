"""NTIS 공고 수집 어댑터.

기존 stub 구현을 대체하는 실제 구현.
`app/scraper/ntis/list_scraper` / `detail_scraper` 를 `BaseSourceAdapter` API 로
감싸는 adapter 클래스. HTTP 클라이언트를 생성·관리하며, list/detail 스크래퍼의
내부 함수를 외부에 노출하지 않는다.

## NTIS 수집 특성

- SSR HTML 기반 (JSON API 없음). httpx 전용, Playwright 불필요.
- 로그인 불필요 (게스트 수집 가능). docs/ntis_site_exploration.md §5 참조.
- 상태 필터: 접수예정(P) / 접수중(B) / 마감(Y). sources.yaml statuses 에서 읽는다.

## canonical_key 연결 전략

NTIS 목록 단계에서는 공식 공고번호(ancmNo)를 알 수 없다.
번호는 상세 페이지(div.se-contents)를 파싱해야 얻을 수 있다(detail_scraper.ntis_ancm_no).

따라서 목록 UPSERT 시에는 `ancm_no=None` 을 주입하여 fuzzy canonical 을 사용한다.
상세 수집 후 `ntis_ancm_no` 를 이용한 canonical 재계산은 subtask 8 에서 처리한다.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

from app.config import Settings
from app.scraper.base import BaseSourceAdapter
from app.sources.config_schema import SourceConfig


class NtisSourceAdapter(BaseSourceAdapter):
    """NTIS 국가R&D통합공고 수집 어댑터.

    context manager 로 사용할 때 상세 수집용 httpx.AsyncClient 를
    공고 간에 재사용하여 연결 오버헤드를 줄인다.
    """

    def __init__(self, source_config: SourceConfig, settings: Settings) -> None:
        """어댑터를 초기화한다."""
        super().__init__(source_config, settings)
        self._detail_client: Optional[httpx.AsyncClient] = None

    async def open(self) -> None:
        """상세 수집용 httpx.AsyncClient 를 생성하고 연결 풀을 시작한다."""
        from app.scraper.ntis.detail_scraper import _build_detail_http_client

        client = _build_detail_http_client(self.settings)
        self._detail_client = await client.__aenter__()
        logger.debug("NtisSourceAdapter: HTTP 클라이언트 열림")

    async def close(self) -> None:
        """httpx.AsyncClient 를 닫고 연결 풀을 해제한다."""
        if self._detail_client is not None:
            await self._detail_client.__aexit__(None, None, None)
            self._detail_client = None
            logger.debug("NtisSourceAdapter: HTTP 클라이언트 닫힘")

    async def scrape_list(self, *, max_pages: int) -> list[dict[str, Any]]:
        """NTIS SSR HTML 파싱으로 접수예정·접수중·마감 공고 목록을 순차 수집한다.

        source_config.statuses 에 지정된 순서대로 각 상태를 수집하며,
        source_config.max_announcements 상한에 도달하면 조기 종료한다.

        list_scraper 가 반환하는 'ntis_announcement_id' 키를 DB 범용 키
        'source_announcement_id' 로 정규화하고, 'source_type' 을 추가한다.

        canonical_key 산출용 'ancm_no' 를 None 으로 주입한다. NTIS 공식 공고번호는
        목록 단계에서 알 수 없으므로 목록 UPSERT 는 항상 fuzzy canonical 을 사용한다.
        상세 수집 후 ntis_ancm_no 를 이용한 canonical 재계산은 subtask 8 에서 처리한다.

        Args:
            max_pages: 상태별 순회할 최대 페이지 수.

        Returns:
            정규화된 공고 메타 dict 리스트.
        """
        from app.scraper.ntis.list_scraper import scrape_list

        raw_rows = await scrape_list(
            settings=self.settings,
            max_pages=max_pages,
            max_announcements=self.source_config.max_announcements,
            statuses=self.source_config.statuses,
        )

        normalized_rows: list[dict[str, Any]] = []
        for row in raw_rows:
            normalized = dict(row)
            # ntis_announcement_id → source_announcement_id 로 키 통일
            if "ntis_announcement_id" in normalized:
                normalized["source_announcement_id"] = normalized.pop("ntis_announcement_id")
            normalized["source_type"] = self.source_type
            # 목록 단계 canonical 후보: NTIS는 상세 파싱 전까지 공식 번호 불명 → None 주입
            normalized.setdefault("ancm_no", None)
            normalized_rows.append(normalized)

        logger.debug("NtisSourceAdapter.scrape_list: {}건 정규화 완료", len(normalized_rows))
        return normalized_rows

    def build_download_referer(self, source_announcement_id: str) -> str:
        """NTIS 공고 상세 페이지 URL 을 Referer 로 반환한다."""
        from app.scraper.ntis.list_scraper import NTIS_BASE_DOMAIN
        return f"{NTIS_BASE_DOMAIN}/rndgate/eg/un/ra/view.do?roRndUid={source_announcement_id}"

    def extract_attachment_links(self, detail_html: str) -> list:
        """NTIS 상세 HTML 에서 첨부파일 링크를 추출한다.

        fn_fileDownload(wfUid, roTextUid) onclick 패턴을 파싱하여
        httpx POST 방식의 AttachmentLinkInfo 목록을 반환한다.
        """
        from app.scraper.attachment_downloader import AttachmentLinkInfo
        from app.scraper.ntis.detail_scraper import DOWNLOAD_ONCLICK_PATTERN
        from app.scraper.ntis.list_scraper import NTIS_BASE_DOMAIN

        NTIS_FILE_DOWNLOAD_ENDPOINT = (
            NTIS_BASE_DOMAIN + "/rndgate/eg/cmm/file/download.do"
        )

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(detail_html, "html.parser")

        links: list[AttachmentLinkInfo] = []
        for a_tag in soup.find_all("a"):
            onclick = a_tag.get("onclick") or ""
            m = DOWNLOAD_ONCLICK_PATTERN.search(onclick)
            if m is None:
                continue
            wf_uid = m.group(1)
            ro_text_uid = m.group(2)
            filename = a_tag.get_text(strip=True) or f"attachment_{wf_uid}"
            links.append(
                AttachmentLinkInfo(
                    atc_doc_id=wf_uid,
                    atc_file_id=ro_text_uid,
                    original_filename=filename,
                    file_size_bytes=None,
                    download_url=NTIS_FILE_DOWNLOAD_ENDPOINT,
                    download_method="POST",
                    post_data={"wfUid": wf_uid, "roTextUid": ro_text_uid},
                )
            )

        return links

    async def scrape_detail(self, detail_url: str) -> dict[str, Any]:
        """NTIS 공고 상세 페이지를 수집한다.

        context manager 안에서 호출되면 열린 클라이언트를 재사용한다.
        컨텍스트 밖에서 호출되면 임시 클라이언트를 생성한다.

        반환 dict 에는 IRIS 와 공통된 기본 필드 외에 NTIS 전용 필드가 포함된다:
            ntis_ancm_no : 공식 공고번호 정규화 결과 (canonical 재계산에 사용).
            ntis_meta    : 구조화 메타 (공고형태, 부처명, 공고기관명, 공고금액 등).
            attachments  : [{filename, wf_uid, ro_text_uid}] 형태의 첨부파일 목록.

        Args:
            detail_url: 상세 페이지 전체 URL.

        Returns:
            {detail_html, detail_text, detail_fetched_at, detail_fetch_status,
             ntis_ancm_no, ntis_meta, attachments}
        """
        from app.scraper.ntis.detail_scraper import (
            scrape_detail,
            scrape_detail_with_client,
        )

        if self._detail_client is not None:
            return await scrape_detail_with_client(self._detail_client, detail_url)

        # context manager 밖에서 호출된 경우 — 임시 클라이언트 사용
        return await scrape_detail(detail_url, settings=self.settings)


__all__ = ["NtisSourceAdapter"]
