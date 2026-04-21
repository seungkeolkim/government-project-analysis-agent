"""IRIS 공고 수집 어댑터.

기존 `app/scraper/iris/list_scraper` / `detail_scraper` 를 `BaseSourceAdapter` API 로
감싸는 adapter 클래스. HTTP 클라이언트를 생성·관리하며, list/detail 스크래퍼의
private helper 를 외부에 노출하지 않는다.

접수예정·접수중·마감 3개 상태를 순차 수집한다. 수집 대상 상태는
source_config.statuses 에서 읽어오며, sources.yaml 에 고정 설정한다.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

from app.config import Settings
from app.scraper.base import BaseSourceAdapter
from app.sources.config_schema import SourceConfig


class IrisSourceAdapter(BaseSourceAdapter):
    """IRIS 사업공고 공개 API를 통해 공고를 수집하는 어댑터.

    context manager 로 사용할 때 상세 수집용 httpx.AsyncClient 를
    공고 간에 재사용하여 연결 오버헤드를 줄인다.
    """

    def __init__(self, source_config: SourceConfig, settings: Settings) -> None:
        """어댑터를 초기화한다."""
        super().__init__(source_config, settings)
        self._detail_client: Optional[httpx.AsyncClient] = None

    async def open(self) -> None:
        """상세 수집용 httpx.AsyncClient 를 생성하고 연결 풀을 시작한다."""
        from app.scraper.iris.detail_scraper import _build_detail_http_client

        client = _build_detail_http_client(self.settings)
        # httpx.AsyncClient.__aenter__ 는 self 를 반환하며 연결 풀을 초기화한다.
        self._detail_client = await client.__aenter__()
        logger.debug("IrisSourceAdapter: HTTP 클라이언트 열림")

    async def close(self) -> None:
        """httpx.AsyncClient 를 닫고 연결 풀을 해제한다."""
        if self._detail_client is not None:
            await self._detail_client.__aexit__(None, None, None)
            self._detail_client = None
            logger.debug("IrisSourceAdapter: HTTP 클라이언트 닫힘")

    async def scrape_list(self, *, max_pages: int) -> list[dict[str, Any]]:
        """IRIS AJAX API 로 접수예정·접수중·마감 공고 목록을 순차 수집한다.

        source_config.statuses 에 지정된 순서대로 각 상태를 수집하며,
        source_config.max_announcements 상한에 도달하면 조기 종료한다.

        list_scraper 가 반환하는 'iris_announcement_id' 키를 DB 범용 키
        'source_announcement_id' 로 정규화하고, 'source_type' 을 추가한다.

        Args:
            max_pages: 상태별 순회할 최대 페이지 수.

        Returns:
            정규화된 공고 메타 dict 리스트.
        """
        from app.scraper.iris.list_scraper import scrape_list

        raw_rows = await scrape_list(
            settings=self.settings,
            max_pages=max_pages,
            max_announcements=self.source_config.max_announcements,
            statuses=self.source_config.statuses,
        )

        normalized_rows: list[dict[str, Any]] = []
        for row in raw_rows:
            normalized = dict(row)
            # iris_announcement_id → source_announcement_id 로 키 통일
            if "iris_announcement_id" in normalized:
                normalized["source_announcement_id"] = normalized.pop("iris_announcement_id")
            normalized["source_type"] = self.source_type
            normalized_rows.append(normalized)

        logger.debug("IrisSourceAdapter.scrape_list: {}건 정규화 완료", len(normalized_rows))
        return normalized_rows

    async def scrape_detail(self, detail_url: str) -> dict[str, Any]:
        """IRIS 공고 상세 페이지를 수집한다.

        context manager 안에서 호출되면 열린 클라이언트를 재사용한다.
        컨텍스트 밖에서 호출되면 임시 클라이언트를 생성한다.

        Args:
            detail_url: 상세 페이지 전체 URL.

        Returns:
            {detail_html, detail_text, detail_fetched_at, detail_fetch_status}
        """
        from app.scraper.iris.detail_scraper import (
            scrape_detail,
            scrape_detail_with_client,
        )

        if self._detail_client is not None:
            return await scrape_detail_with_client(self._detail_client, detail_url)

        # context manager 밖에서 호출된 경우 — 임시 클라이언트 사용
        return await scrape_detail(detail_url, settings=self.settings)


__all__ = ["IrisSourceAdapter"]
