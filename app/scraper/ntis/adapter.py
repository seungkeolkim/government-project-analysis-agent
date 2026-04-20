"""NTIS 공고 수집 어댑터 (stub).

실제 구현은 보류 상태이다. 호출 시 경고 로그를 남기고 빈 결과를 반환한다.
NTIS 스크래핑 요구사항이 확정되면 이 파일을 실제 구현으로 교체한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.config import Settings
from app.scraper.base import BaseSourceAdapter
from app.sources.config_schema import SourceConfig


class NtisSourceAdapter(BaseSourceAdapter):
    """NTIS 공고 수집 어댑터 — stub 구현.

    실제 NTIS 스크래핑 로직이 구현될 때까지 경고를 남기고 빈 결과를 반환한다.
    sources.yaml 에서 enabled=false 이므로 기본 실행에서는 호출되지 않는다.
    `--source NTIS` 플래그로 수동 테스트할 수 있다.
    """

    def __init__(self, source_config: SourceConfig, settings: Settings) -> None:
        """어댑터를 초기화한다."""
        super().__init__(source_config, settings)

    async def scrape_list(self, *, max_pages: int) -> list[dict[str, Any]]:
        """NTIS 공고 목록 수집 — stub.

        실제 구현 전까지 경고 로그를 남기고 빈 리스트를 반환한다.
        """
        logger.warning(
            "NtisSourceAdapter.scrape_list: NTIS 어댑터는 아직 구현되지 않았습니다. "
            "빈 목록을 반환합니다."
        )
        return []

    async def scrape_detail(self, detail_url: str) -> dict[str, Any]:
        """NTIS 공고 상세 수집 — stub.

        실제 구현 전까지 경고 로그를 남기고 error 상태 dict 를 반환한다.
        """
        logger.warning(
            "NtisSourceAdapter.scrape_detail: NTIS 어댑터는 아직 구현되지 않았습니다. "
            "url={}", detail_url,
        )
        return {
            "detail_html": None,
            "detail_text": None,
            "detail_fetched_at": datetime.now(tz=timezone.utc),
            "detail_fetch_status": "error",
        }


__all__ = ["NtisSourceAdapter"]
