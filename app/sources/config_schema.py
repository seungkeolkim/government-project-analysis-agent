"""소스별 크롤링 설정 스키마.

sources.yaml 파일을 읽어 SourcesConfig 로 파싱한다.
스크래퍼가 어떤 소스를 어떤 파라미터로 실행할지를 이 파일로 제어한다.

사용 예:
    config = load_sources_config()
    for source in config.get_enabled_sources():
        print(source.id, source.base_url)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from app.config import PROJECT_ROOT

# sources.yaml 기본 위치
DEFAULT_SOURCES_CONFIG_PATH: Path = PROJECT_ROOT / "sources.yaml"

# per-run 복사본 경로를 주입할 환경변수 이름 (00016-3 entrypoint copy 격리에서 사용)
SOURCES_CONFIG_PATH_ENV_VAR: str = "SOURCES_CONFIG_PATH"


class SourceCredentials(BaseModel):
    """소스별 인증 정보.

    실제 값은 환경변수에서 읽으며, yaml 파일에 직접 기재하지 않는다.
    환경변수 이름 규칙: `{SOURCE_ID}_USERNAME`, `{SOURCE_ID}_PASSWORD`
    예) IRIS_USERNAME, IRIS_PASSWORD

    현재 지원 소스:
    - IRIS: 로그인 필요 여부 미확인
    - NTIS: 로그인 불필요 (docs/ntis_site_exploration.md §5). 환경변수 설정 불필요.
    """

    username: Optional[str] = None
    """로그인 사용자명. None 이면 인증 없이 게스트 접근."""

    password: Optional[str] = None
    """로그인 비밀번호. None 이면 인증 없이 게스트 접근."""

    @classmethod
    def from_env(cls, source_id: str) -> Optional["SourceCredentials"]:
        """환경변수에서 소스별 credentials 를 취득한다.

        `{SOURCE_ID}_USERNAME` / `{SOURCE_ID}_PASSWORD` 두 환경변수를 읽는다.
        두 환경변수 모두 설정되지 않은 경우 None 을 반환한다.

        Args:
            source_id: 소스 유형 식별자 (예: 'IRIS', 'NTIS').

        Returns:
            SourceCredentials 인스턴스, 또는 환경변수가 없으면 None.
        """
        prefix = source_id.upper()
        username = os.environ.get(f"{prefix}_USERNAME")
        password = os.environ.get(f"{prefix}_PASSWORD")
        if username is None and password is None:
            return None
        return cls(username=username, password=password)


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
    """소스당 최대 페이지 수. None 이면 scrape.max_pages 또는 코드 default 를 따른다."""

    max_announcements: Optional[int] = Field(default=None, gt=0)
    """소스당 최대 공고 수. None 이면 scrape.max_announcements 또는 코드 default 를 따른다."""

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

    def resolve_credentials(self) -> Optional[SourceCredentials]:
        """환경변수에서 소스별 credentials 를 취득한다.

        yaml 에 credentials 를 직접 기재하지 않고, 런타임에 환경변수로부터
        `{SOURCE_ID}_USERNAME` / `{SOURCE_ID}_PASSWORD` 를 읽는다.
        인증 로직은 이 메서드를 호출하는 세션 팩토리 함수 1곳에서만 사용한다.

        로그인이 불필요한 소스(NTIS 등)는 환경변수를 설정하지 않으면 None 을 반환한다.

        Returns:
            credentials 인스턴스, 또는 환경변수가 설정되지 않은 경우 None.
        """
        return SourceCredentials.from_env(self.id)


class ScrapeRunConfig(BaseModel):
    """전역 수집 실행 설정 (sources.yaml 의 scrape: 섹션).

    모든 실행 파라미터는 CLI 인자 대신 이 스키마를 통해 제어한다.
    docker compose --profile scrape run --rm scraper 실행 시 이 값이 적용된다.
    """

    active_sources: list[str] = Field(default_factory=list)
    """실행할 소스 ID 목록. 비어 있으면 enabled=True 인 소스 전체를 실행한다.
    예: [NTIS] → NTIS 만 실행, [] → enabled 소스 전체 실행."""

    max_pages: Optional[int] = Field(default=None, gt=0)
    """소스당 최대 페이지 수. None 이면 소스별 설정 → 코드 default(10) 순으로 사용."""

    max_announcements: Optional[int] = Field(default=None, gt=0)
    """소스당 최대 공고 수. None 이면 소스별 설정 → 코드 default(200) 순으로 사용."""

    skip_detail: bool = False
    """True 면 상세 페이지 수집을 건너뛴다 (목록 적재만 수행)."""

    skip_attachments: bool = False
    """True 면 첨부파일 다운로드를 건너뛴다. 목록·상세 수집은 정상 실행된다."""

    dry_run: bool = False
    """True 면 DB 쓰기를 건너뛴다 (수집만 검증할 때 사용)."""

    log_level: Optional[str] = None
    """로그 레벨 오버라이드 (DEBUG/INFO/WARNING/ERROR/CRITICAL).
    None 이면 .env 의 LOG_LEVEL 을 사용한다."""

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: Optional[str]) -> Optional[str]:
        """로그 레벨을 대문자로 정규화하고 허용된 값인지 검증한다."""
        if value is None:
            return None
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = value.upper()
        if normalized not in allowed:
            raise ValueError(
                f"log_level 은 {sorted(allowed)} 중 하나여야 합니다. 입력값: {value!r}"
            )
        return normalized


class SourcesConfig(BaseModel):
    """sources.yaml 파일 최상위 스키마."""

    scrape: ScrapeRunConfig = Field(default_factory=ScrapeRunConfig)
    """전역 수집 실행 설정."""

    sources: list[SourceConfig] = Field(default_factory=list)
    """소스별 크롤링 설정 목록."""

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

    path 우선순위:
    1. 인자로 전달된 path
    2. 환경변수 SOURCES_CONFIG_PATH (per-run 복사본 경로 주입에 사용)
    3. 프로젝트 루트 기본값 (sources.yaml)

    파일이 없거나 비어 있으면 빈 SourcesConfig 를 반환한다(예외를 일으키지 않는다).

    Args:
        path: sources.yaml 경로. None 이면 환경변수 → 프로젝트 루트 기본값 순으로 결정.

    Returns:
        파싱된 SourcesConfig 인스턴스.
    """
    if path is None:
        env_path = os.environ.get(SOURCES_CONFIG_PATH_ENV_VAR)
        config_path = Path(env_path) if env_path else DEFAULT_SOURCES_CONFIG_PATH
    else:
        config_path = Path(path)

    if not config_path.exists():
        return SourcesConfig()

    with config_path.open(encoding="utf-8") as file_handle:
        raw = yaml.safe_load(file_handle)

    if not raw:
        return SourcesConfig()

    return SourcesConfig.model_validate(raw)


__all__ = [
    "SourceCredentials",
    "SourceConfig",
    "ScrapeRunConfig",
    "SourcesConfig",
    "load_sources_config",
    "DEFAULT_SOURCES_CONFIG_PATH",
    "SOURCES_CONFIG_PATH_ENV_VAR",
]
