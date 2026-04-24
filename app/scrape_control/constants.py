"""scrape_control 패키지 공용 상수.

웹/CLI/스케줄러 3경로가 공통으로 참조하는 환경변수 이름·경로·도메인 상수를
한 곳에 모은다. 이름 짓기 규칙은 대문자 스네이크 — Final 로 불변 타입 힌트를 준다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from app.config import PROJECT_ROOT

# ──────────────────────────────────────────────────────────────
# subprocess 환경변수
# ──────────────────────────────────────────────────────────────

# 웹/스케줄러가 scraper subprocess 에 주입하는 "이번 run 의 active_sources" 환경변수.
# sources.yaml 을 in-place 로 수정하면 entrypoint.sh 의 per-run 임시 복사본 격리가
# 깨지므로(동시 실행 시 설정 경합), 파일 대신 env 로 전달한다.
# 값 포맷: 소스 id 를 쉼표로 이어붙인 문자열. 예) "IRIS,NTIS".
# 비어 있는 상태로 주입하거나 미주입이면 sources.yaml 의 기존 active_sources 를 그대로 사용.
SCRAPE_ACTIVE_SOURCES_ENV_VAR: Final[str] = "SCRAPE_ACTIVE_SOURCES"

# 웹/스케줄러가 이미 생성해둔 ScrapeRun.id 를 subprocess CLI 로 이어주기 위한 키.
# 웹/스케줄러 경로는 start_scrape_run 에서 create_scrape_run 으로 running row 를
# 먼저 INSERT 하고 subprocess 를 기동한다. 이 환경변수가 주입되면 subprocess 측
# cli._async_main 은 **새 ScrapeRun row 를 만들지 않고** 기존 row 를 이어받아
# 마감까지 수행한다. 이로써 '웹이 방금 만든 running row 를 자기 자신이 또 조회해서
# 중복 running 으로 오판 후 exit 2 하는' 자기참조 문제를 제거한다 (task 00034).
# 값 포맷: ScrapeRun.id 의 정수 문자열 (예: "42"). 비어있거나 정수 파싱 실패 시
# CLI 는 기존 경로(trigger='cli' 로 자체 create_scrape_run) 로 동작한다.
SCRAPE_RUN_ID_ENV_VAR: Final[str] = "SCRAPE_RUN_ID"

# 호스트 프로젝트 루트의 절대 경로. 웹 컨테이너가 호스트 dockerd 에 mount 지시를
# 내릴 때 상대경로 해석의 기준이 된다(`docker compose --project-directory $HOST_PROJECT_DIR`).
# 설계 문서 §5.3 참조. 본 subtask(00025-3) 에서 runner 가 읽어 사용한다.
HOST_PROJECT_DIR_ENV_VAR: Final[str] = "HOST_PROJECT_DIR"

# compose 프로젝트 이름. docker compose CLI 가 생성하는 리소스의 prefix 로 쓰인다.
# 기본값은 iris-agent(현재 호스트 compose 가 기본으로 쓰는 이름).
COMPOSE_PROJECT_NAME_ENV_VAR: Final[str] = "COMPOSE_PROJECT_NAME"
DEFAULT_COMPOSE_PROJECT_NAME: Final[str] = "iris-agent"

# app 이미지에 COPY 된 docker-compose.yml 경로. Dockerfile 이 /app/docker-compose.yml
# 로 COPY 한다. docker compose CLI 가 이 파일을 읽는다.
COMPOSE_FILE_IN_CONTAINER: Final[str] = "/app/docker-compose.yml"

# 스크래퍼 service 이름. docker-compose.yml 의 services 섹션 이름과 동일해야 한다.
COMPOSE_SCRAPER_SERVICE_NAME: Final[str] = "scraper"

# docker compose 호출 시 사용할 profile. profiles: [scrape] 로 분리되어 있어
# 명시적 지정이 필요하다.
COMPOSE_SCRAPE_PROFILE: Final[str] = "scrape"


# ──────────────────────────────────────────────────────────────
# 로그 파일 경로
# ──────────────────────────────────────────────────────────────

# subprocess stdout/stderr 를 쓰는 파일 디렉터리. PROJECT_ROOT/data/logs/scrape_runs/
# 아래에 {run_id}.log 로 저장한다. PIPE 가 아닌 파일 redirect 를 쓰는 이유:
# PIPE 버퍼가 가득 차면 자식 프로세스가 write 에서 블로킹되어 멈출 수 있기 때문.
SCRAPE_RUN_LOG_DIRNAME: Final[str] = "logs/scrape_runs"


def scrape_run_log_root(data_dir: Path | None = None) -> Path:
    """ScrapeRun 로그 파일 루트 디렉터리 절대경로를 반환한다.

    기본은 ``PROJECT_ROOT/data/logs/scrape_runs``. 테스트에서 ``data_dir`` 를
    주입하면 ``<data_dir>/logs/scrape_runs`` 를 반환한다. 디렉터리 생성은
    호출자가 필요 시 mkdir 한다.
    """
    base = data_dir if data_dir is not None else PROJECT_ROOT / "data"
    return base / SCRAPE_RUN_LOG_DIRNAME


def scrape_run_log_path(run_id: int, *, data_dir: Path | None = None) -> Path:
    """특정 ScrapeRun id 에 대응하는 로그 파일 경로.

    Args:
        run_id:   대상 ScrapeRun PK.
        data_dir: 테스트 주입용 override. None 이면 PROJECT_ROOT/data.
    """
    return scrape_run_log_root(data_dir=data_dir) / f"{run_id}.log"


# ──────────────────────────────────────────────────────────────
# trigger 도메인 (service 레이어용)
# ──────────────────────────────────────────────────────────────

# 웹/스케줄러가 runner.start_scrape_run 에 전달할 수 있는 trigger 값.
# 'cli' 는 cli.py 자신이 직접 create_scrape_run 할 때만 사용한다 — 웹 경로에서는
# 사용자가 이 값을 주입할 수 없어야 하므로 Literal 타입으로 막는다.
ExternalTrigger = Literal["manual", "scheduled"]


__all__ = [
    "COMPOSE_FILE_IN_CONTAINER",
    "COMPOSE_PROJECT_NAME_ENV_VAR",
    "COMPOSE_SCRAPE_PROFILE",
    "COMPOSE_SCRAPER_SERVICE_NAME",
    "DEFAULT_COMPOSE_PROJECT_NAME",
    "ExternalTrigger",
    "HOST_PROJECT_DIR_ENV_VAR",
    "SCRAPE_ACTIVE_SOURCES_ENV_VAR",
    "SCRAPE_RUN_ID_ENV_VAR",
    "SCRAPE_RUN_LOG_DIRNAME",
    "scrape_run_log_path",
    "scrape_run_log_root",
]
