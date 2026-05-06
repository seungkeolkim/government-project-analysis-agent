"""애플리케이션 설정 로더.

환경변수 또는 프로젝트 루트의 `.env` 파일로부터 값을 읽어 `Settings` 객체에 담는다.
다른 모듈은 `get_settings()` 를 호출해 싱글턴으로 공유한다.

소스별 URL 등 상세 파라미터는 `sources.yaml` 에서 관리한다.
여기서는 전역 공통 동작 설정(지연, 로깅, 경로)만 보유한다.

보안상의 이유로 기본값에는 비밀 정보를 넣지 않는다.
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
    예) `REQUEST_DELAY_SEC=2.0`, `LOG_LEVEL=DEBUG`.

    소스별 URL 등 상세 파라미터는 `sources.yaml` 에서 관리한다.
    여기서는 전역 공통 동작 옵션(지연, 로깅, 경로)만 보유한다.
    """

    # ──────────────────────────────────────────────────────────────
    # 스크래핑 공통 설정 및 네트워크 정책
    # ──────────────────────────────────────────────────────────────

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

    # 게시판 DB (task 00051 건의사항, task 00056 공지사항)는 메인 DB(app.sqlite3) 가
    # reset 되어도 게시글 데이터가 보존되도록 별도 파일(boards.sqlite3)에 저장한다.
    # 두 DB 사이에는 cross-DB FK 가 불가능하므로 작성자 동기화는
    # app.suggestions.author_validity 가 batch 쿼리로 수행한다.
    # (task 00056) 파일명을 suggestions.sqlite3 → boards.sqlite3 로 변경.
    # 기존 파일이 있으면 startup 시 migrate_suggestions_to_boards() 가 이름을 바꾼다.
    suggestions_db_url: str = Field(
        default=f"sqlite:///{(PROJECT_ROOT / 'data' / 'db' / 'boards.sqlite3').as_posix()}",
        description="게시판(건의사항·공지사항) 전용 SQLAlchemy 접속 문자열. 메인 DB 와 격리.",
    )

    # task 00073 — IP 접근 이력 날짜별 로그파일 저장 디렉터리.
    # 파일명 형식: access_history_YYMMDD.log (예: access_history_260506.log)
    # 환경변수 ACCESS_LOG_DIR 로 재지정 가능.
    access_log_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "logs",
        description="접근 이력 로그파일(access_history_YYMMDD.log) 저장 디렉터리.",
    )

    # task 00073 — IP 접근 이력 방문 집계 세션 비활성 기준 (분).
    # 같은 IP 에서 마지막 요청 후 이 시간 이상 활동이 없으면 새 방문으로 집계.
    # 기본 30분: 비활성 타임아웃 방식. 60 으로 변경하면 "1시간 퉁" 동작과 동치.
    access_history_session_gap_minutes: int = Field(
        default=30,
        ge=1,
        description="방문 집계 세션 비활성 기준(분). 이 시간 이상 무활동이면 새 방문으로 카운트.",
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
        # 메인 DB 와 건의사항 별도 DB 양쪽을 동일하게 보장한다.
        sqlite_prefix = "sqlite:///"
        for sqlite_url in (self.db_url, self.suggestions_db_url):
            if not sqlite_url.startswith(sqlite_prefix):
                continue
            raw_path = sqlite_url[len(sqlite_prefix):]
            db_file = Path(raw_path).expanduser()
            if not db_file.is_absolute():
                db_file = (PROJECT_ROOT / db_file).resolve()
            db_file.parent.mkdir(parents=True, exist_ok=True)

        # task 00073 — 접근 이력 로그 디렉터리 생성.
        self.access_log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """`Settings` 싱글턴을 반환한다.

    프로세스 수명 동안 한 번만 생성되며, 테스트에서 재구성이 필요하면
    `get_settings.cache_clear()` 로 캐시를 비울 수 있다.
    """
    return Settings()
