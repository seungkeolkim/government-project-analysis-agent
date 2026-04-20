"""NTIS 공고 수집 어댑터 (stub).

실제 구현은 보류 상태이다. 호출 시 경고 로그를 남기고 빈 결과를 반환한다.
NTIS 스크래핑 요구사항이 확정되면 이 파일을 실제 구현으로 교체한다.

TODO [NTIS 신규 크롤러 구현 시]:
    scrape_list 를 실제 구현할 때 다음 인터페이스 규약을 반드시 준수해야 한다.

    1. 반환하는 각 row dict 에 반드시 포함되어야 하는 키:
       - source_announcement_id: str  (NTIS 내부 공고 ID)
       - source_type: str             (반드시 "NTIS" 또는 constants.SOURCE_TYPE_NTIS)
       - title: str
       - status: str                  (AnnouncementStatus Enum 값 중 하나:
                                       "접수중" / "접수예정" / "마감")
       선택 키: agency, received_at_text, deadline_at_text, detail_url, row_html

    2. status 값은 app.db.models.AnnouncementStatus 의 값("접수중"/"접수예정"/"마감")과
       일치해야 한다. NTIS 가 다른 형태로 상태를 반환하면 이 어댑터에서 정규화한다.

    3. 증분 수집 동작(UpsertResult 4-branch)은 repository 계층이 자동으로 처리한다.
       상태 전이(status_transitioned)·이력 보존(new_version) 등은 별도 구현 없이
       기존 인터페이스를 따르기만 하면 된다.

    4. docs/status_transition_todo.md 참고.
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
