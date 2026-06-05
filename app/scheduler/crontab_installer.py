"""DB 의 스케줄 설정을 읽어 실제 OS crontab 에 설치하는 런타임 설치기 (task 00155-3).

위치 / 역할
-----------
:mod:`app.scheduler.crontab_generator` 는 컨테이너·cron·실시간 시계에 의존하지
않는 **순수 생성기**다. 본 모듈은 그 생성기에 "이 컨테이너의 실제 런타임 값"
(프로젝트 경로, python 실행기 절대경로, 로그 디렉터리, .env 위치, docker 바이너리
PATH 등)을 주입해 crontab 텍스트를 만들고, ``crontab`` 명령으로 현재 유저의
crontab 에 **설치**한다.

호출 지점
---------
1. 컨테이너 기동 시: ``docker/entrypoint.sh`` 가 alembic 마이그레이션 직후
   ``python -m app.scheduler.crontab_installer`` 를 (HOST_UID 권한으로) 호출한다.
   이 시점의 crontab 소유자가 곧 cron 이 잡을 실행할 유저(HOST_UID)가 된다.
2. (후속 task 00155-4) admin 스케줄/백업/Daily Report 설정 변경 시 재설치:
   :func:`install_crontab` 을 직접 호출한다. cron 이 없는 개발/테스트 호스트에서는
   :class:`CrontabInstallResult` 의 ``installed=False`` 로 graceful no-op 처리되어
   라우트가 500 으로 깨지지 않는다.

핵심 런타임 값
--------------
- **env_file**: cron 은 잡을 **빈 환경**으로 띄우므로, 각 잡 라인은 ``.env`` 를
  source 해 HOST_PROJECT_DIR 등을 로딩한다(생성기의 래퍼). 그런데 이 프로젝트의
  ``.env`` 는 컨테이너 안에서 ``/app/.env`` 가 아니라 **호스트 절대경로**
  ``${HOST_PROJECT_DIR}/.env`` 에 마운트된다(docker-compose.yml, task 00143).
  따라서 잡 라인이 source 할 env_file 도 그 절대경로여야 한다. HOST_PROJECT_DIR
  환경변수가 비어 있으면(개발 호스트 등) 관용적 ``.env`` 상대경로로 폴백한다.
- **python_executable**: cron 의 PATH 는 빈약하므로 ``sys.executable`` 절대경로를
  쓴다(예: /usr/local/bin/python).
- **extra_environment PATH**: cron 잡이 ``docker`` 바이너리를 찾을 수 있도록
  표준 PATH 를 crontab 상단에 박는다. (docker 미발견 시 scrape 잡의
  ``docker compose run scraper`` 가 깨진다.)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, Settings, get_settings
from app.scheduler.crontab_generator import (
    CrontabEnvironment,
    generate_crontab_text,
)
from app.scrape_control.constants import HOST_PROJECT_DIR_ENV_VAR

# ``crontab`` 명령 이름. shutil.which 로 존재 여부를 먼저 확인한다.
CRONTAB_BINARY_NAME: str = "crontab"

# cron 잡이 docker 등 바이너리를 찾도록 crontab 상단에 박을 표준 PATH.
# python_executable 은 절대경로로 직접 호출하므로 PATH 비의존이지만, scrape 잡이
# 호출하는 docker(/usr/bin/docker) 와 docker compose 플러그인 탐색을 위해 둔다.
DEFAULT_JOB_PATH: str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class CrontabInstallResult:
    """crontab 설치 시도 결과.

    Attributes:
        installed:    실제로 ``crontab`` 명령으로 설치에 성공했는지 여부.
        crontab_text: 렌더된 crontab 텍스트(설치 성공/실패와 무관하게 채워진다).
                      호출 측 로깅/디버깅/테스트 검증에 사용한다.
        reason:       installed=False 인 경우 그 이유(설치 성공 시 빈 문자열).
    """

    installed: bool
    crontab_text: str
    reason: str = ""


def _resolve_env_file_path() -> str:
    """cron 잡 라인이 source 할 ``.env`` 의 경로를 결정한다.

    컨테이너에서는 ``.env`` 가 ``${HOST_PROJECT_DIR}/.env`` 에 마운트되므로(00143)
    그 절대경로를 우선 사용한다. HOST_PROJECT_DIR 가 비어 있으면(개발 호스트 등)
    프로젝트 루트 기준 관용적 ``.env`` 상대경로로 폴백한다.

    Returns:
        잡 래퍼가 ``. <경로>`` 로 source 할 env 파일 경로 문자열.
    """
    import os

    host_project_dir = os.environ.get(HOST_PROJECT_DIR_ENV_VAR, "").strip()
    if host_project_dir:
        # 컨테이너 정상 경로 — .env 마운트 절대경로.
        return f"{host_project_dir.rstrip('/')}/.env"
    # 폴백 — cd project_dir 이후의 상대경로.
    return ".env"


def build_runtime_environment(settings: Settings | None = None) -> CrontabEnvironment:
    """현재 컨테이너/프로세스의 실제 런타임 값으로 :class:`CrontabEnvironment` 를 만든다.

    순수 생성기(:mod:`app.scheduler.crontab_generator`)에 주입할 실행 컨텍스트를
    구성한다. 단위 테스트는 이 함수의 출력 필드(특히 env_file 폴백/PATH)를 직접
    검증할 수 있다.

    Args:
        settings: 경로 산출에 쓸 설정. None 이면 :func:`get_settings` 싱글턴 사용.

    Returns:
        잡 라인 렌더에 필요한 모든 런타임 값이 채워진 :class:`CrontabEnvironment`.
    """
    settings = settings or get_settings()
    return CrontabEnvironment(
        # 잡 라인은 이 경로로 cd 한 뒤 상대 경로(./data 등)를 해석한다.
        # 컨테이너 안의 코드/작업 루트는 /app(PROJECT_ROOT)다.
        project_dir=str(PROJECT_ROOT),
        # cron 의 빈약한 PATH 에 의존하지 않도록 python 실행기 절대경로를 쓴다.
        python_executable=sys.executable or "python",
        # 잡 stdout/stderr redirect 대상. data/logs(access_log_dir) 를 재사용한다.
        log_dir=str(settings.access_log_dir),
        # 컨테이너에서는 ${HOST_PROJECT_DIR}/.env 절대경로(00143).
        env_file=_resolve_env_file_path(),
        # docker 바이너리 탐색을 위해 표준 PATH 를 crontab 상단에 박는다.
        extra_environment={"PATH": DEFAULT_JOB_PATH},
    )


def render_runtime_crontab(
    session: Session, *, settings: Settings | None = None
) -> str:
    """현재 런타임 컨텍스트로 설치될 crontab 텍스트를 렌더한다(설치는 하지 않음).

    Args:
        session:  스케줄 설정을 읽을 ORM 세션.
        settings: 경로 산출용 설정(None 이면 싱글턴).

    Returns:
        ``crontab`` 으로 설치 가능한 crontab 텍스트.
    """
    environment = build_runtime_environment(settings)
    return generate_crontab_text(session, environment)


def install_crontab(
    session: Session | None = None, *, settings: Settings | None = None
) -> CrontabInstallResult:
    """DB 스케줄 설정을 읽어 현재 유저의 crontab 에 설치한다.

    cron 데몬은 crontab 소유자 권한으로 잡을 실행하므로, 이 함수는 **잡을 실행할
    유저(HOST_UID)** 권한으로 호출되어야 한다(entrypoint 가 gosu 강등 후 호출).

    graceful 동작:
        - ``crontab`` 바이너리가 없는 환경(개발/테스트 호스트, CI 등)에서는 예외를
          던지지 않고 ``installed=False`` 로 반환한다. 후속 task 00155-4 의 admin
          라우트가 이 함수를 호출해도 500 이 나지 않도록 하기 위함이다.
        - ``crontab`` 실행이 비0 으로 실패하면 그 사실을 로깅하고 ``installed=False``
          (reason 에 stderr)로 반환한다. 호출 측(entrypoint)이 비치명적으로 처리한다.

    Args:
        session:  스케줄 설정을 읽을 ORM 세션. None 이면 :func:`session_scope` 로
                  새 세션을 연다.
        settings: 경로 산출용 설정(None 이면 싱글턴).

    Returns:
        설치 결과(:class:`CrontabInstallResult`).
    """
    # session 미주입 시 새 세션 컨텍스트를 열어 렌더한다(읽기 전용).
    if session is None:
        from app.db.session import session_scope

        with session_scope() as managed_session:
            crontab_text = render_runtime_crontab(managed_session, settings=settings)
    else:
        crontab_text = render_runtime_crontab(session, settings=settings)

    crontab_binary = shutil.which(CRONTAB_BINARY_NAME)
    if not crontab_binary:
        # cron 미설치 환경 — 설치를 건너뛰되 텍스트는 반환해 호출 측이 활용 가능.
        reason = (
            f"'{CRONTAB_BINARY_NAME}' 바이너리를 찾을 수 없어 crontab 설치를 "
            "건너뜁니다(cron 미설치 환경으로 추정)."
        )
        logger.warning("crontab 설치 skip — {}", reason)
        return CrontabInstallResult(
            installed=False, crontab_text=crontab_text, reason=reason
        )

    # `crontab -` 는 stdin 으로 받은 텍스트로 현재 유저의 crontab 을 통째로 교체한다.
    try:
        completed = subprocess.run(
            [crontab_binary, "-"],
            input=crontab_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        reason = f"crontab 실행 자체가 실패했습니다: {type(exc).__name__}: {exc}"
        logger.error("crontab 설치 실패 — {}", reason)
        return CrontabInstallResult(
            installed=False, crontab_text=crontab_text, reason=reason
        )

    if completed.returncode != 0:
        reason = (
            f"crontab 명령이 비0 종료했습니다(returncode={completed.returncode}): "
            f"{(completed.stderr or '').strip()}"
        )
        logger.error("crontab 설치 실패 — {}", reason)
        return CrontabInstallResult(
            installed=False, crontab_text=crontab_text, reason=reason
        )

    job_line_count = sum(
        1
        for line in crontab_text.splitlines()
        # 헤더/주석/환경 라인을 제외한 실제 잡 라인 수를 가볍게 센다.
        if line and not line.startswith("#") and "=" not in line.split(" ", 1)[0]
    )
    logger.info(
        "crontab 설치 완료: 잡 라인 약 {}건 등록(binary={}).",
        job_line_count,
        crontab_binary,
    )
    return CrontabInstallResult(installed=True, crontab_text=crontab_text)


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. 현재 유저 crontab 을 DB 설정 기준으로 설치한다.

    entrypoint 가 ``python -m app.scheduler.crontab_installer`` 로 호출한다.

    종료 코드:
        - 0: 설치 성공, **또는** cron 미설치 환경이라 정상적으로 건너뜀(개발/CI).
        - 1: cron 은 있으나 ``crontab`` 설치가 실제로 실패함(운영 환경 이상 신호).

    Args:
        argv: 미사용(인자 없는 단순 명령). 시그니처 일관성을 위해 둔다.

    Returns:
        위 종료 코드.
    """
    from app.logging_setup import configure_logging

    configure_logging(get_settings())

    result = install_crontab()
    if result.installed:
        return 0
    # crontab 바이너리 자체가 없으면 '정상적 건너뜀'(개발/CI) — 0 으로 본다.
    if shutil.which(CRONTAB_BINARY_NAME) is None:
        return 0
    # 바이너리는 있는데 설치가 실패 → 운영 환경 이상으로 비0.
    logger.error("crontab 설치가 실패했습니다: {}", result.reason)
    return 1


__all__ = [
    "CRONTAB_BINARY_NAME",
    "CrontabInstallResult",
    "DEFAULT_JOB_PATH",
    "build_runtime_environment",
    "install_crontab",
    "main",
    "render_runtime_crontab",
]


if __name__ == "__main__":
    sys.exit(main())
