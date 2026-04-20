"""IRIS 공고 수집 어댑터.

기존 `app/scraper/iris/list_scraper` / `detail_scraper` 를 `BaseSourceAdapter` API 로
감싸는 adapter 클래스. HTTP 클라이언트를 생성·관리하며, list/detail 스크래퍼의
private helper 를 외부에 노출하지 않는다.

TODO [IRIS 접수예정·마감 수집 시작 시]:
    현재 scrape_list 는 '접수중' 상태의 공고만 반환한다.
    접수예정·마감 수집을 추가할 때 다음 사항을 함께 처리해야 한다:

    1. list_scraper.scrape_list 에 status_filter 파라미터 추가
       (예: status_filter="접수예정" / "마감" / None=전체)
    2. 반환되는 row 의 status 값이 AnnouncementStatus Enum(접수중/접수예정/마감)으로
       정규화되어야 한다. 현재 IRIS API 가 반환하는 상태값 문자열과 Enum 값을 매핑하는
       로직을 list_scraper 또는 이 어댑터에서 처리한다.
    3. 상태 전이(예: 접수예정 → 접수중) 가 발생하면 repository.upsert_announcement 가
       action="status_transitioned" 을 반환한다. CLI(_log_upsert_action)에서
       WARNING 으로 기록되며, 실제 검증 및 추가 처리(알림 등)를 이때 구현한다.
    4. docs/status_transition_todo.md 참고.
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
        """IRIS AJAX API 로 '접수중' 공고 목록을 수집한다.

        list_scraper 가 반환하는 'iris_announcement_id' 키를 DB 범용 키
        'source_announcement_id' 로 정규화하고, 'source_type' 을 추가한다.

        Args:
            max_pages: 순회할 최대 페이지 수.

        Returns:
            정규화된 공고 메타 dict 리스트.
        """
        from app.scraper.iris.list_scraper import scrape_list

        raw_rows = await scrape_list(settings=self.settings, max_pages=max_pages)

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
