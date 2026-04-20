"""소스 어댑터 기반 클래스.

각 수집 소스(IRIS, NTIS 등)는 `BaseSourceAdapter` 를 상속하여
`scrape_list` / `scrape_detail` 을 구현한다.

어댑터는 async context manager 로 사용하여 HTTP 클라이언트 등
리소스를 안전하게 관리한다:

    async with get_adapter(source_config, settings) as adapter:
        rows = await adapter.scrape_list(max_pages=10)
        for row in rows:
            detail = await adapter.scrape_detail(row["detail_url"])
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.config import Settings
from app.sources.config_schema import SourceConfig


class BaseSourceAdapter(ABC):
    """공고 수집 소스 어댑터의 기반 클래스.

    서브클래스는 `scrape_list` / `scrape_detail` 을 반드시 구현해야 한다.
    HTTP 클라이언트 등 리소스 초기화/정리가 필요하면 `open` / `close` 를
    오버라이드한다.
    """

    def __init__(self, source_config: SourceConfig, settings: Settings) -> None:
        """어댑터를 초기화한다.

        Args:
            source_config: sources.yaml 에서 로드한 소스 설정.
            settings:       전역 애플리케이션 설정.
        """
        self.source_config = source_config
        self.settings = settings

    @property
    def source_type(self) -> str:
        """이 어댑터가 담당하는 소스 유형 문자열 (source_config.id 의 별칭)."""
        return self.source_config.id

    async def open(self) -> None:
        """어댑터 리소스를 초기화한다 (HTTP 클라이언트 생성 등).

        async context manager 진입 시 호출된다.
        리소스가 필요 없는 어댑터는 오버라이드하지 않아도 된다.
        """

    async def close(self) -> None:
        """어댑터 리소스를 정리한다 (HTTP 클라이언트 닫기 등).

        async context manager 종료 시 호출된다.
        리소스가 없는 어댑터는 오버라이드하지 않아도 된다.
        """

    async def __aenter__(self) -> "BaseSourceAdapter":
        """async with 진입 시 open() 을 호출하고 self 를 반환한다."""
        await self.open()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """async with 종료 시 close() 를 호출한다."""
        await self.close()

    @abstractmethod
    async def scrape_list(self, *, max_pages: int) -> list[dict[str, Any]]:
        """공고 목록을 수집하여 공고 메타 dict 리스트를 반환한다.

        반환된 각 dict 는 최소 아래 키를 포함해야 한다:
            - source_announcement_id : 소스가 부여한 공고 고유 ID
            - source_type            : 소스 유형 문자열 (self.source_type)
            - title                  : 공고 제목
            - status                 : 접수 상태 텍스트
            - detail_url             : 상세 페이지 URL (없으면 None)

        Args:
            max_pages: 목록 페이지 순회 상한.

        Returns:
            공고 메타 dict 리스트. 수집 결과가 없으면 빈 리스트.
        """

    @abstractmethod
    async def scrape_detail(self, detail_url: str) -> dict[str, Any]:
        """단일 공고의 상세 페이지를 수집하여 결과 dict 를 반환한다.

        Args:
            detail_url: 공고 상세 페이지 전체 URL.

        Returns:
            {
              "detail_html": str | None,
              "detail_text": str | None,
              "detail_fetched_at": datetime,
              "detail_fetch_status": "ok" | "empty" | "error",
            }
        """


__all__ = ["BaseSourceAdapter"]
