"""스크래퍼 subprocess 기동 / 관찰 모듈.

웹(관리자 페이지) 또는 스케줄러가 수동/자동 수집을 시작할 때 호출하는 진입점.
내부에서 ``docker compose --profile scrape run --rm scraper`` 를
``subprocess.Popen`` 으로 기동하고, pid 를 ``ScrapeRun`` row 에 기록한 뒤
백그라운드 스레드가 종료를 감시해 status 를 마감한다.

설계 요약:
    - docker-in-docker 가 아니다. app 컨테이너에 마운트된 호스트 /var/run/docker.sock
      을 통해 호스트 dockerd 를 원격 조작한다. 설계 문서 §4~§5 참조.
    - subprocess 는 새 프로세스 그룹으로 띄운다(``os.setsid``). 중단 시 부모가
      ``os.killpg`` 로 그룹 전체에 SIGTERM 을 보내고, docker compose v2 는
      이를 관리 컨테이너(PID 1 의 app.cli)로 릴레이한다.
    - stdout/stderr 는 로그 파일로 바로 redirect 한다. PIPE 를 쓰면 버퍼가 차서
      자식이 write 에서 블로킹될 수 있다(guidance 명시).
    - lock 검증 + ScrapeRun row 생성은 **단일 트랜잭션** 에서 수행해
      SELECT→INSERT 사이의 race 를 막는다.
    - Windows 호환은 의도적으로 포기한다 (``os.setsid`` POSIX 전용).
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import IO, Callable

from loguru import logger

from app.db.repository import (
    create_scrape_run,
    finalize_scrape_run,
    get_running_scrape_run,
    set_scrape_run_pid,
)
from app.db.session import session_scope
from app.scrape_control.constants import (
    COMPOSE_FILE_IN_CONTAINER,
    COMPOSE_PROJECT_NAME_ENV_VAR,
    COMPOSE_SCRAPE_PROFILE,
    COMPOSE_SCRAPER_SERVICE_NAME,
    DEFAULT_COMPOSE_PROJECT_NAME,
    ExternalTrigger,
    HOST_PROJECT_DIR_ENV_VAR,
    SCRAPE_ACTIVE_SOURCES_ENV_VAR,
    scrape_run_log_path,
    scrape_run_log_root,
)


# ──────────────────────────────────────────────────────────────
# 예외
# ──────────────────────────────────────────────────────────────


class ScrapeAlreadyRunningError(RuntimeError):
    """동시 실행 금지 위반 — 이미 진행 중인 ScrapeRun 이 존재한다.

    웹 라우트에서는 409 Conflict 로, CLI 에서는 exit(2) 로 변환된다.
    """

    def __init__(self, running_run_id: int, running_trigger: str) -> None:
        self.running_run_id = running_run_id
        self.running_trigger = running_trigger
        super().__init__(
            f"이미 다른 수집이 진행 중입니다 (ScrapeRun id={running_run_id}, "
            f"trigger={running_trigger!r})."
        )


class ComposeEnvironmentError(RuntimeError):
    """docker compose 호출에 필요한 호스트 환경 설정이 누락됐을 때 발생.

    대표 사례: HOST_PROJECT_DIR 환경변수 미설정. 운영자에게 .env 설정을
    안내해야 한다.
    """


# ──────────────────────────────────────────────────────────────
# 반환 타입
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StartResult:
    """``start_scrape_run`` 반환값.

    Attributes:
        scrape_run_id: 생성된 ScrapeRun 의 PK.
        pid:           기동된 ``docker compose run`` 프로세스의 pid.
                       프로세스 그룹 리더이며, 중단 시 ``os.killpg(pid, SIGTERM)``.
        log_path:      subprocess stdout/stderr 가 쓰이는 로그 파일 절대경로.
                       UI 의 로그 조회 엔드포인트가 이 파일을 스트리밍한다.
    """

    scrape_run_id: int
    pid: int
    log_path: str


# ──────────────────────────────────────────────────────────────
# compose 명령 구성
# ──────────────────────────────────────────────────────────────


# docker CLI 바이너리 탐색 시 관행적 후보 경로. shutil.which 가 실패한
# 경우(스케줄러 실행 컨텍스트의 PATH 가 제한적일 때 등) 에만 시도한다.
#
# Dockerfile 에서 Debian `docker.io` 패키지를 설치하므로 컨테이너 안의
# 정규 경로는 /usr/bin/docker 다. 호스트 직접 실행 개발 환경 등에서는
# /usr/local/bin/docker 에 있을 수 있어 2차 후보로 둔다.
_DEFAULT_DOCKER_BINARY_PATHS: tuple[str, ...] = (
    "/usr/bin/docker",
    "/usr/local/bin/docker",
)

# 환경변수로 직접 지정할 수 있는 오버라이드 키. 운영자가 비표준 위치에
# docker CLI 를 둔 경우(예: /opt/docker-ce/bin/docker) 의 비상 수단.
_DOCKER_BINARY_ENV_VAR: str = "DOCKER_BINARY"


def _resolve_docker_binary() -> str:
    """subprocess.Popen 에 사용할 docker CLI 의 절대경로를 결정한다.

    기존 코드는 argv[0] 을 문자열 \"docker\" 로 두어 execvpe 의 PATH 탐색에
    의존했지만, APScheduler 백그라운드 실행 컨텍스트처럼 PATH 가 축소된
    환경에서는 ``FileNotFoundError: [Errno 2] No such file or directory: 'docker'``
    가 발생할 수 있다. 이를 예방하고, 탐색 실패 시에는 ScrapeRun 을 failed 로
    마감하며 운영자에게 명확한 진단 메시지를 남기는 것이 본 함수의 역할이다.

    우선순위:
        1. 환경변수 ``DOCKER_BINARY`` 로 절대경로가 주어진 경우 그대로 사용한다.
        2. ``shutil.which('docker')`` — 현재 프로세스 PATH 기준 탐색.
        3. 관행 경로(_DEFAULT_DOCKER_BINARY_PATHS) 를 순차 점검.

    Returns:
        실행 가능한 docker CLI 의 절대경로.

    Raises:
        ComposeEnvironmentError: 모든 후보에서 바이너리를 찾지 못했거나
            DOCKER_BINARY 가 유효하지 않은 경로를 가리킬 때.
    """
    override = os.environ.get(_DOCKER_BINARY_ENV_VAR, "").strip()
    if override:
        # 오버라이드 값이 절대경로이고 실제 실행 가능한 파일을 가리켜야만
        # 허용한다. 상대경로·존재하지 않는 경로는 거부해 침묵 오작동을 막는다.
        if (
            os.path.isabs(override)
            and os.path.isfile(override)
            and os.access(override, os.X_OK)
        ):
            return override
        raise ComposeEnvironmentError(
            f"환경변수 {_DOCKER_BINARY_ENV_VAR}={override!r} 가 유효한 "
            "실행 가능한 절대경로가 아닙니다. docker CLI 바이너리의 절대경로를 "
            "지정하거나 환경변수를 제거하세요."
        )

    discovered = shutil.which("docker")
    if discovered:
        return discovered

    for candidate in _DEFAULT_DOCKER_BINARY_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise ComposeEnvironmentError(
        "docker CLI 바이너리를 찾지 못했습니다. app 컨테이너에 docker.io 패키지가 "
        "설치되어 있는지, 또는 비특권 UID 가 해당 파일을 실행할 수 있는지 확인하세요 "
        "(이미지 재빌드가 필요할 수 있습니다). 비표준 경로를 쓰는 경우 환경변수 "
        f"{_DOCKER_BINARY_ENV_VAR} 로 docker CLI 의 절대경로를 주입할 수 있습니다. "
        "탐색 범위: PATH(shutil.which) · "
        f"{', '.join(_DEFAULT_DOCKER_BINARY_PATHS)}."
    )


def build_compose_command(active_sources: list[str]) -> list[str]:
    """docker compose 호출에 사용할 argv 를 만든다.

    최종 argv 예시:

        /usr/bin/docker compose \\
            -f /app/docker-compose.yml \\
            --project-directory /home/user/project \\
            -p iris-agent \\
            --profile scrape \\
            run --rm \\
            -e SCRAPE_ACTIVE_SOURCES=IRIS,NTIS \\
            scraper

    ``argv[0]`` 은 ``_resolve_docker_binary()`` 가 반환하는 **절대경로** 다.
    예전에는 문자열 ``\"docker\"`` 를 그대로 넘겨 execvpe 의 PATH 탐색에
    의존했으나, APScheduler 의 백그라운드 실행 컨텍스트 등에서는 PATH 가
    제한적이어서 ``FileNotFoundError: [Errno 2] No such file or directory:
    'docker'`` 가 발생할 수 있다. 이를 피하고자 argv[0] 을 절대경로로 고정한다.
    compose v2 플러그인은 ``/usr/libexec/docker/cli-plugins`` 에서 docker CLI
    가 자동 탐색하므로 ``compose`` 서브커맨드 체계는 건드리지 않는다.

    ``--project-directory`` 로 **호스트 경로** 를 지정해야 compose 가 compose
    파일의 상대경로(``./app`` 등) 를 호스트 기준 절대경로로 전개해 dockerd 에
    마운트를 지시할 수 있다 (docker-in-docker 가 아니므로 호스트 관점에서
    경로가 유효해야 한다). 설계 문서 §5.3 참조.

    Args:
        active_sources: 이번 run 에 실행할 소스 id 목록.
                        비어 있으면 ``-e`` 플래그를 추가하지 않고 sources.yaml
                        의 기본 active_sources 를 그대로 사용한다.

    Returns:
        ``subprocess.Popen`` 에 그대로 전달 가능한 argv 리스트.

    Raises:
        ComposeEnvironmentError: HOST_PROJECT_DIR 미설정, 또는 docker CLI
            바이너리를 app 컨테이너 내에서 찾지 못한 경우.
    """
    # docker CLI 바이너리 위치를 먼저 확정한다. 탐색 실패 시 이 함수를 호출한
    # start_scrape_run 이 ScrapeRun 을 failed 로 마감하고 에러 메시지를 UI
    # '로그' 컬럼에 노출한다.
    docker_binary = _resolve_docker_binary()

    host_project_dir = os.environ.get(HOST_PROJECT_DIR_ENV_VAR, "").strip()
    if not host_project_dir:
        raise ComposeEnvironmentError(
            f"환경변수 {HOST_PROJECT_DIR_ENV_VAR} 가 설정되지 않았습니다. "
            "호스트의 프로젝트 루트 절대경로를 .env 에 설정해야 합니다 "
            "(예: HOST_PROJECT_DIR=/home/user/workspace/iris-agent). "
            "이 값은 docker compose 가 compose 파일의 상대경로를 호스트 기준으로 "
            "해석하는 데 쓰입니다 — docs/scrape_control_design.md §5.3 참조."
        )

    project_name = os.environ.get(
        COMPOSE_PROJECT_NAME_ENV_VAR, DEFAULT_COMPOSE_PROJECT_NAME
    ).strip() or DEFAULT_COMPOSE_PROJECT_NAME

    argv: list[str] = [
        docker_binary,
        "compose",
        "-f",
        COMPOSE_FILE_IN_CONTAINER,
        "--project-directory",
        host_project_dir,
        "-p",
        project_name,
        "--profile",
        COMPOSE_SCRAPE_PROFILE,
        "run",
        "--rm",
    ]

    # active_sources 가 지정된 경우에만 환경변수 주입. 빈 리스트는 "전체"를 뜻한다.
    if active_sources:
        # 쉼표로 join — cli.py 가 split(',') 로 해석한다.
        joined = ",".join(active_sources)
        argv.extend(["-e", f"{SCRAPE_ACTIVE_SOURCES_ENV_VAR}={joined}"])

    argv.append(COMPOSE_SCRAPER_SERVICE_NAME)
    return argv


# ──────────────────────────────────────────────────────────────
# subprocess 기동
# ──────────────────────────────────────────────────────────────


# subprocess.Popen 은 실제 기동 시점에 의존성 주입하기 어려우므로 테스트에서
# monkeypatch 하기 쉽도록 모듈 수준 alias 를 둔다.
_popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen


def start_scrape_run(
    active_sources: list[str],
    *,
    trigger: ExternalTrigger,
) -> StartResult:
    """스크래퍼 subprocess 를 기동하고 ScrapeRun row 를 생성한다.

    흐름:
        1. 동일 트랜잭션에서 ``get_running_scrape_run`` 으로 lock 체크 + running
           없으면 ``create_scrape_run(trigger=trigger)`` 으로 row 생성. 둘이
           같은 session_scope 안에서 실행되어 race 를 막는다.
        2. 로그 파일 경로를 결정하고(run_id 기반) 부모 디렉터리를 mkdir.
        3. ``subprocess.Popen`` — stdout/stderr 는 로그 파일로 direct,
           ``preexec_fn=os.setsid`` 로 새 세션 + 프로세스 그룹 생성,
           ``close_fds=True`` 로 부모 fd 상속 차단.
        4. 별도 트랜잭션에서 ``set_scrape_run_pid`` 로 pid 기록.
        5. daemon 스레드가 subprocess 를 ``wait()`` 후 ``finalize_scrape_run``
           호출.

    Args:
        active_sources: 이번 run 에 실행할 소스 id 목록 (빈 리스트면 전체).
        trigger:        'manual' 또는 'scheduled'. cli 는 이 함수를 쓰지 않음.

    Returns:
        StartResult — scrape_run_id / pid / log_path.

    Raises:
        ScrapeAlreadyRunningError: 이미 running row 가 존재.
        ComposeEnvironmentError:   HOST_PROJECT_DIR 미설정.
        OSError:                   Popen 자체가 실패 (docker CLI 미설치 등).
    """
    # 도메인 검증 — Literal 타입은 정적 체크용이므로 런타임에도 방어.
    if trigger not in ("manual", "scheduled"):
        raise ValueError(
            f"start_scrape_run 의 trigger 는 'manual'/'scheduled' 여야 합니다: {trigger!r}"
        )

    normalized_sources = [s.strip() for s in active_sources if s.strip()]

    # 1. lock 체크 + ScrapeRun 생성 (같은 트랜잭션).
    with session_scope() as session:
        running = get_running_scrape_run(session)
        if running is not None:
            raise ScrapeAlreadyRunningError(
                running_run_id=running.id,
                running_trigger=running.trigger,
            )
        scrape_run_row = create_scrape_run(
            session,
            trigger=trigger,
            source_counts={"active_sources": list(normalized_sources)},
        )
        scrape_run_id: int = scrape_run_row.id

    # 2. compose argv 구성 (HOST_PROJECT_DIR 누락이면 여기서 ComposeEnvironmentError).
    #    이 시점에 예외가 나면 방금 만든 ScrapeRun 을 failed 로 마감해야 한다.
    try:
        argv = build_compose_command(normalized_sources)
    except ComposeEnvironmentError as exc:
        _safe_finalize(scrape_run_id, status="failed", error_message=str(exc))
        raise

    # 3. 로그 파일 준비.
    log_path = scrape_run_log_path(scrape_run_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # append 바이너리 — 파일 시스템에 즉시 flush 되도록 buffering=0.
    log_handle: IO[bytes] = log_path.open("ab", buffering=0)

    # 4. subprocess 기동.
    try:
        popen = _popen_factory(
            argv,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # os.setsid: 새 session + 새 프로세스 그룹 leader. pgid == pid.
            # POSIX 전용 — Windows 호환은 의도적으로 포기한다(docker compose 운영 전제).
            preexec_fn=os.setsid,
            close_fds=True,
        )
    except Exception as exc:
        log_handle.close()
        _safe_finalize(
            scrape_run_id,
            status="failed",
            error_message=f"subprocess 기동 실패: {type(exc).__name__}: {exc}",
        )
        raise

    logger.info(
        "스크래퍼 subprocess 기동: scrape_run_id={} pid={} argv={!r}",
        scrape_run_id, popen.pid, shlex.join(argv),
    )

    # 5. pid 기록.
    try:
        with session_scope() as session:
            set_scrape_run_pid(session, scrape_run_id, popen.pid)
    except Exception as exc:
        # pid 기록이 실패해도 프로세스는 이미 떴다 — watcher 가 종료 시 finalize 한다.
        # 다만 중단 경로는 pid 조회가 안 되므로 경고 로그를 남긴다.
        logger.exception(
            "ScrapeRun.pid 기록 실패 — 중단 버튼이 동작하지 않을 수 있습니다: "
            "scrape_run_id={} pid={} ({}: {})",
            scrape_run_id, popen.pid, type(exc).__name__, exc,
        )

    # 6. watcher 스레드 기동.
    watcher = threading.Thread(
        target=_watch_subprocess,
        args=(scrape_run_id, popen, log_handle),
        name=f"scrape-watcher-{scrape_run_id}",
        daemon=True,
    )
    watcher.start()

    return StartResult(
        scrape_run_id=scrape_run_id,
        pid=popen.pid,
        log_path=str(log_path),
    )


def _watch_subprocess(
    scrape_run_id: int,
    popen: subprocess.Popen[bytes],
    log_handle: IO[bytes],
) -> None:
    """subprocess 가 종료될 때까지 대기한 뒤 ScrapeRun 을 마감한다.

    watcher 는 daemon 스레드이고, 예외가 새면 데몬 스레드가 조용히 죽는다.
    따라서 내부에서 예외를 잡고 logger 로 기록한 뒤 finalize 까지 시도한다.
    ``finalize_scrape_run`` 은 terminal 상태를 중복 갱신하지 않으므로, CLI 측이
    이미 finalize 한 경우에는 여기서는 no-op 가 된다.
    """
    return_code: int = -1
    try:
        return_code = popen.wait()
    except Exception as exc:
        logger.exception(
            "scrape_run_id={} subprocess.wait() 실패: {}: {}",
            scrape_run_id, type(exc).__name__, exc,
        )
    finally:
        try:
            log_handle.close()
        except Exception:
            pass

    status, error_message = _resolve_final_status(return_code)
    logger.info(
        "scrape_run_id={} subprocess 종료: return_code={} → status={}",
        scrape_run_id, return_code, status,
    )

    _safe_finalize(
        scrape_run_id,
        status=status,
        error_message=error_message,
    )


def _resolve_final_status(return_code: int) -> tuple[str, str | None]:
    """subprocess return code 를 ScrapeRun.status + error_message 로 매핑한다.

    매핑 규칙 (설계 문서 §9.2):
        - 0: completed. CLI 가 자체 finalize 한 상태일 수 있어 watcher 쪽 finalize
             는 idempotent 하게 동작한다 (cancelled/partial 이 먼저 기록되어
             있으면 그대로 유지).
        - -SIGTERM (= -15): cancelled. killpg 로 외부 중단된 경로.
        - 130: cancelled (SIGINT 의 관행적 종료 코드. 사용 빈도 낮음).
        - -9 (SIGKILL): failed. 운영자 개입 흔적 — error_message 에 기록.
        - 기타: failed. return_code 를 error_message 에 포함.

    Returns:
        (status, error_message) 튜플.
    """
    if return_code == 0:
        return "completed", None
    if return_code == -signal.SIGTERM or return_code == 130:
        return "cancelled", None
    if return_code == -signal.SIGKILL:
        return "failed", "subprocess killed (SIGKILL)"
    return "failed", f"subprocess exit_code={return_code}"


def _safe_finalize(
    scrape_run_id: int,
    *,
    status: str,
    error_message: str | None,
) -> None:
    """finalize_scrape_run 을 호출하고, 실패해도 watcher 스레드를 죽이지 않는다.

    finalize 는 idempotent 하므로 여러 경로에서 반복 호출해도 안전하다.
    DB 이슈로 마감 자체가 실패해도 running 상태로 남을 수 있는데, 이 경우
    다음 번 웹 재시작 시 stale cleanup 이 실패 처리해 복구한다.
    """
    try:
        with session_scope() as session:
            finalize_scrape_run(
                session,
                scrape_run_id,
                status=status,
                error_message=error_message,
            )
    except Exception as exc:
        logger.exception(
            "finalize_scrape_run 실패(스킵): scrape_run_id={} status={} ({}: {})",
            scrape_run_id, status, type(exc).__name__, exc,
        )


__all__ = [
    "ComposeEnvironmentError",
    "ScrapeAlreadyRunningError",
    "StartResult",
    "build_compose_command",
    "start_scrape_run",
]
