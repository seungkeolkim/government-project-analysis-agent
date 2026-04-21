"""소스별 크롤링 설정 스키마.

sources.yaml 파일을 읽어 SourcesConfig 로 파싱한다.
스크래퍼가 어떤 소스를 어떤 파라미터로 실행할지를 이 파일로 제어한다.

사용 예:
    config = load_sources_config()
    for source in config.get_enabled_sources():
        print(source.id, source.base_url)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT

# sources.yaml 기본 위치
DEFAULT_SOURCES_CONFIG_PATH: Path = PROJECT_ROOT / "sources.yaml"


class SourceConfig(BaseModel):
    """단일 소스(IRIS, NTIS 등)에 대한 크롤링 설정."""

    id: str
    """소스 유형 식별자. app.sources.constants.SOURCE_TYPE_* 상수 중 하나."""

    enabled: bool = True
    """이 소스를 현재 활성화할지 여부."""

    base_url: str
    """목록 페이지(또는 API) 기본 URL."""

    request_delay_sec: float = Field(default=1.5, ge=0.0)
    """요청 간 최소 지연(초). 차단 방지 목적."""

    max_pages: Optional[int] = Field(default=None, gt=0)
    """소스당 최대 페이지 수. None 이면 CLI 인자 또는 코드 default 를 따른다."""

    max_announcements: Optional[int] = Field(default=None, gt=0)
    """소스당 최대 공고 수. None 이면 CLI 인자 또는 코드 default 를 따른다."""

    statuses: list[str] = Field(
        default_factory=lambda: ["접수예정", "접수중", "마감"]
    )
    """수집할 공고 상태 한글 라벨 목록. 어댑터가 순서대로 순회한다."""

    extra: dict[str, Any] = Field(default_factory=dict)
    """소스 어댑터 전용 추가 파라미터. 어댑터가 직접 읽는다."""

    @property
    def source_type(self) -> str:
        """소스 유형 문자열. id 의 별칭."""
        return self.id


class SourcesConfig(BaseModel):
    """sources.yaml 파일 최상위 스키마."""

    sources: list[SourceConfig] = Field(default_factory=list)

    def get_enabled_sources(self) -> list[SourceConfig]:
        """활성화된 소스 목록을 반환한다."""
        return [source for source in self.sources if source.enabled]

    def get_source(self, source_id: str) -> Optional[SourceConfig]:
        """ID 로 특정 소스 설정을 조회한다. 없으면 None."""
        for source in self.sources:
            if source.id == source_id:
                return source
        return None


def load_sources_config(path: Path | str | None = None) -> SourcesConfig:
    """YAML 파일을 읽어 SourcesConfig 를 반환한다.

    path 가 None 이면 DEFAULT_SOURCES_CONFIG_PATH 를 사용한다.
    파일이 없거나 비어 있으면 빈 SourcesConfig 를 반환한다(예외를 일으키지 않는다).

    Args:
        path: sources.yaml 경로. None 이면 프로젝트 루트 기본값 사용.

    Returns:
        파싱된 SourcesConfig 인스턴스.
    """
    config_path = Path(path) if path else DEFAULT_SOURCES_CONFIG_PATH

    if not config_path.exists():
        return SourcesConfig()

    with config_path.open(encoding="utf-8") as file_handle:
        raw = yaml.safe_load(file_handle)

    if not raw:
        return SourcesConfig()

    return SourcesConfig.model_validate(raw)


__all__ = [
    "SourceConfig",
    "SourcesConfig",
    "load_sources_config",
    "DEFAULT_SOURCES_CONFIG_PATH",
]
