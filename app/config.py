"""애플리케이션 설정 로더.

환경변수 또는 프로젝트 루트의 `.env` 파일로부터 값을 읽어 `Settings` 객체에 담는다.
다른 모듈은 `get_settings()` 를 호출해 싱글턴으로 공유한다.

소스별 상세 파라미터(base_url 등)는 `sources.yaml` 에서 관리한다.
여기서는 전역 fallback 값과 공통 동작 설정만 보유한다.

보안상의 이유로 기본값에는 비밀 정보를 넣지 않는다.
URL/경로 기본값은 로컬 개발 편의를 위한 값이므로 운영 환경에서는 반드시 환경변수로 덮어쓴다.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 프로젝트 루트 경로 (app/config.py 기준으로 두 단계 상위)
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """애플리케이션 전역 설정값.

    모든 필드는 환경변수로 덮어쓸 수 있으며, 이름은 필드명과 동일하다(대소문자 무시).
    예) `BASE_URL=...`, `REQUEST_DELAY_SEC=2.0`.

    소스별 base_url 등 상세 파라미터는 `sources.yaml` 에서 관리한다.
    여기서는 소스 공통 fallback 값과 전역 동작 옵션만 보유한다.
    """

    # ──────────────────────────────────────────────────────────────
    # 스크래핑 공통 설정 및 네트워크 정책
    # ──────────────────────────────────────────────────────────────

    base_url: str = Field(
        default="https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituListView.do",
        description=(
            "사업공고 목록 페이지 기본 URL. "
            "sources.yaml 에 소스별 base_url 이 지정되지 않은 경우의 fallback 값."
        ),
    )
    request_delay_sec: float = Field(
        default=1.5,
        ge=0.0,
        description="각 요청 사이에 삽입할 최소 지연(초). 차단 방지 목적.",
    )
    user_agent: str = Field(
        default="",
        description="HTTP 요청에 사용할 User-Agent. 빈 문자열이면 스크래퍼 어댑터의 기본값을 사용한다.",
    )

    # ──────────────────────────────────────────────────────────────
    # 로컬 저장 경로
    # ──────────────────────────────────────────────────────────────

    download_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "downloads",
        description="첨부파일 다운로드 루트 디렉터리.",
    )
    db_url: str = Field(
        default=f"sqlite:///{(PROJECT_ROOT / 'data' / 'db' / 'app.sqlite3').as_posix()}",
        description="SQLAlchemy 접속 문자열.",
    )

    # ──────────────────────────────────────────────────────────────
    # 로깅
    # ──────────────────────────────────────────────────────────────

    log_level: str = Field(
        default="INFO",
        description="루트 로거 레벨. DEBUG / INFO / WARNING / ERROR 중 하나.",
    )

    # pydantic-settings 설정
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        """로그 레벨을 대문자로 정규화하고 허용된 값인지 검증한다."""
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = (value or "INFO").upper()
        if normalized not in allowed:
            raise ValueError(
                f"log_level 은 {sorted(allowed)} 중 하나여야 합니다. 입력값: {value!r}"
            )
        return normalized

    @field_validator("download_dir")
    @classmethod
    def _resolve_download_dir(cls, value: Path) -> Path:
        """다운로드 디렉터리를 절대경로로 변환한다.

        상대경로(`./data/downloads`)로 주어져도 프로젝트 루트 기준 절대경로로 고정한다.
        실제 디렉터리 생성은 `ensure_runtime_paths()` 에서 수행한다.
        """
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return path

    def ensure_runtime_paths(self) -> None:
        """런타임에 필요한 로컬 디렉터리를 생성한다.

        - 첨부파일 다운로드 루트
        - SQLite 를 쓸 경우 DB 파일의 상위 디렉터리

        존재 여부만 보장하며 기존 파일은 건드리지 않는다.
        """
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # sqlite 접속 문자열인 경우 DB 파일의 부모 디렉터리를 생성한다.
        sqlite_prefix = "sqlite:///"
        if self.db_url.startswith(sqlite_prefix):
            raw_path = self.db_url[len(sqlite_prefix):]
            db_file = Path(raw_path).expanduser()
            if not db_file.is_absolute():
                db_file = (PROJECT_ROOT / db_file).resolve()
            db_file.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """`Settings` 싱글턴을 반환한다.

    프로세스 수명 동안 한 번만 생성되며, 테스트에서 재구성이 필요하면
    `get_settings.cache_clear()` 로 캐시를 비울 수 있다.
    """
    return Settings()
