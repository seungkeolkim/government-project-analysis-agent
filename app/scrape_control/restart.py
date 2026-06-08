"""iris-agent-web 컨테이너 셀프 재시작 모듈 (task 00161).

관리자 페이지의 [시스템 재시작] 서브탭이 호출하는 서비스 레이어. 웹 컨테이너가
**자기 자신** 을 호스트 dockerd(마운트된 ``/var/run/docker.sock``)를 통해
``docker restart`` 하는 'A 방식' 을 구현한다.

설계 요약(사용자 원문 근거):
    - ``docker stop`` 이 아니라 ``docker restart`` 를 쓴다. compose 의
      ``restart: unless-stopped`` 정책상 stop 한 컨테이너는 daemon 이 다시
      살리지 않으므로, '재시작' 의도에는 restart 가 맞다. restart 는 docker
      daemon 이 수행하므로, 명령을 띄운 client(=자기 자신 웹 프로세스)가 중간에
      죽어도 daemon 이 재시작을 끝까지 완료한다.
    - 명령은 ``sleep 1 && docker restart <container>`` 형태로 띄운다. 1초
      sleep 은 endpoint 가 돌려준 HTTP 200 응답이 브라우저로 flush 될 시간을
      벌기 위함이다(응답을 받기 전에 컨테이너가 죽으면 프론트가 '재시작 중…'
      상태로 전환할 기회를 놓친다).
    - child 는 ``runner.start_scrape_run`` 과 동일한 detached 패턴으로 띄운다:
      ``preexec_fn=os.setsid`` 로 새 세션/프로세스 그룹을 만들어 부모 생명주기와
      분리하고, ``stdin=DEVNULL`` / ``close_fds=True`` / stdout·stderr 는 로그
      파일 핸들로 direct. 원문의 'Popen start_new_session=True' 와 동일 의도다.
    - docker CLI 절대경로는 :func:`runner._resolve_docker_binary` 를 **재사용**
      한다('지금 시작' 버튼과 동일한 docker.sock 권한 경로라 추가 권한 설정이
      불필요하다). 바이너리 해석 로직을 이 모듈에 복제하지 않는다.
    - Windows 호환은 의도적으로 포기한다(``os.setsid`` POSIX 전용) — runner.py
      와 동일한 운영 전제.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable

from loguru import logger

from app.config import PROJECT_ROOT
from app.scrape_control import runner
from app.timezone import now_utc, to_kst


# ──────────────────────────────────────────────────────────────
# 컨테이너 이름
# ──────────────────────────────────────────────────────────────

# 재시작 대상 컨테이너 이름을 주입할 수 있는 환경변수. 운영자가 compose 의
# container_name 을 바꿨을 때 코드 수정 없이 맞출 수 있도록 둔다.
SELF_RESTART_CONTAINER_ENV_VAR: str = "SELF_RESTART_CONTAINER"

# 기본 컨테이너 이름. docker-compose.yml 의 ``container_name: iris-agent-web`` 과
# 일치해야 한다. 환경변수 미설정/빈 값이면 이 값을 쓴다.
DEFAULT_SELF_RESTART_CONTAINER: str = "iris-agent-web"


def resolve_container_name() -> str:
    """재시작 대상 컨테이너 이름을 결정한다.

    우선순위:
        1. 환경변수 ``SELF_RESTART_CONTAINER`` (앞뒤 공백 제거 후 비어있지 않으면).
        2. 기본값 ``iris-agent-web`` (docker-compose.yml 의 container_name).

    Returns:
        ``docker restart`` 인자로 넘길 컨테이너 이름.
    """
    raw_value = os.environ.get(SELF_RESTART_CONTAINER_ENV_VAR, "").strip()
    return raw_value or DEFAULT_SELF_RESTART_CONTAINER


# ──────────────────────────────────────────────────────────────
# 로그 파일 경로
# ──────────────────────────────────────────────────────────────

# 재시작 명령 실행 로그를 남길 디렉터리/파일 이름. constants.py 의
# scrape_run_log_root/scrape_run_log_path 컨벤션(PROJECT_ROOT/data/logs/...,
# data_dir 주입 가능)을 모방한다. 최종 경로: PROJECT_ROOT/data/logs/system_restart.log
SYSTEM_RESTART_LOG_DIRNAME: str = "logs"
SYSTEM_RESTART_LOG_FILENAME: str = "system_restart.log"


def system_restart_log_path(*, data_dir: Path | None = None) -> Path:
    """셀프 재시작 명령 실행 로그 파일의 절대경로를 반환한다.

    기본은 ``PROJECT_ROOT/data/logs/system_restart.log``. 테스트에서 ``data_dir``
    를 주입하면 ``<data_dir>/logs/system_restart.log`` 를 반환한다. 디렉터리
    생성은 호출자가 필요 시 mkdir 한다(:func:`trigger_self_restart` 가 수행).

    Args:
        data_dir: 테스트 주입용 override. None 이면 PROJECT_ROOT/data.

    Returns:
        로그 파일 Path.
    """
    base = data_dir if data_dir is not None else PROJECT_ROOT / "data"
    return base / SYSTEM_RESTART_LOG_DIRNAME / SYSTEM_RESTART_LOG_FILENAME


# ──────────────────────────────────────────────────────────────
# subprocess 기동
# ──────────────────────────────────────────────────────────────

# runner.py 와 동일하게, 테스트에서 monkeypatch 하기 쉽도록 모듈 수준 alias 를
# 둔다. 실제 호출 시점에 이 전역 이름이 조회되므로, 테스트는
# ``monkeypatch.setattr(restart, \"_popen_factory\", fake)`` 로 주입할 수 있다.
_popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen


@dataclass(frozen=True)
class RestartResult:
    """:func:`trigger_self_restart` 반환값.

    Attributes:
        pid:        기동된 detached child(``sh -c ...``) 프로세스의 pid.
        container:  재시작 대상 컨테이너 이름.
        argv:       실제로 ``Popen`` 에 넘긴 argv(검증/로깅용).
        log_path:   재시작 명령 출력이 append 되는 로그 파일 절대경로(문자열).
    """

    pid: int
    container: str
    argv: list[str]
    log_path: str


def trigger_self_restart(
    *,
    container_name: str | None = None,
    delay_seconds: int = 1,
    data_dir: Path | None = None,
) -> RestartResult:
    """detached subprocess 로 ``sleep N && docker restart <container>`` 를 띄운다.

    호출 즉시(non-blocking) 반환한다 — child 는 새 세션으로 분리되어 부모(웹
    프로세스)가 재시작으로 죽어도 docker daemon 이 restart 를 완료한다.

    Args:
        container_name: 재시작 대상 컨테이너. None 이면 :func:`resolve_container_name`.
        delay_seconds:  docker restart 실행 전 sleep 초. 기본 1초(HTTP 응답 flush
                        시간 확보). 음수는 0 으로 보정한다.
        data_dir:       로그 파일 위치 override(테스트 주입용).

    Returns:
        :class:`RestartResult` — pid / container / argv / log_path.

    Raises:
        ComposeEnvironmentError: docker CLI 바이너리를 찾지 못한 경우
            (:func:`runner._resolve_docker_binary` 가 던진다).
        OSError: Popen 기동 자체가 실패한 경우.
    """
    # docker CLI 절대경로는 runner 의 단일 출처를 재사용한다(복제 금지).
    docker_binary = runner._resolve_docker_binary()
    container = container_name if container_name is not None else resolve_container_name()
    # 음수 delay 는 sleep 인자로 부적절하므로 0 으로 보정.
    safe_delay = delay_seconds if delay_seconds >= 0 else 0

    # sh -c 한 줄로 'sleep 후 restart' 를 묶는다. 컨테이너 이름/바이너리 경로는
    # shlex.quote 로 안전하게 인용해 셸 주입을 막는다.
    shell_command = (
        f"sleep {safe_delay} && "
        f"{shlex.quote(docker_binary)} restart {shlex.quote(container)}"
    )
    argv: list[str] = ["sh", "-c", shell_command]

    # 로그 파일 준비 — 부모 디렉터리를 멱등하게 생성한다.
    log_path = system_restart_log_path(data_dir=data_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # append 바이너리 + buffering=0 으로 즉시 flush(runner 와 동일 컨벤션).
    log_handle: IO[bytes] = log_path.open("ab", buffering=0)

    # 트리거 시각/명령을 헤더로 한 줄 남겨, 나중에 로그만 보고도 '언제 무엇을
    # 실행했는지' 추적할 수 있게 한다. KST 표기로 운영자 친화적으로 남긴다.
    kst_now = to_kst(now_utc())
    timestamp_text = kst_now.isoformat() if kst_now is not None else "(unknown)"
    header_line = (
        f"[{timestamp_text}] 셀프 재시작 트리거: container={container} "
        f"argv={shlex.join(argv)}\n"
    )
    try:
        log_handle.write(header_line.encode("utf-8"))
    except OSError:
        # 헤더 기록 실패가 재시작 자체를 막지는 않도록 한다(로그는 부가 정보).
        logger.warning("셀프 재시작 로그 헤더 기록 실패(무시하고 진행)")

    try:
        popen = _popen_factory(
            argv,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # os.setsid: 새 session + 새 프로세스 그룹 leader. 부모(웹 프로세스)가
            # docker restart 로 죽어도 child 는 그룹 분리되어 살아남아 명령을
            # 끝까지 띄운다. POSIX 전용(runner 와 동일 전제).
            preexec_fn=os.setsid,
            close_fds=True,
        )
    except Exception:
        # 기동 실패 시 부모가 연 핸들을 닫고 예외를 그대로 전파한다.
        log_handle.close()
        raise

    # 부모 쪽 핸들은 닫는다 — child 가 자신의 dup 된 fd 로 stdout/stderr 를
    # 계속 쓰므로 부모 복사본은 더 필요 없다(runner 는 watcher 가 닫지만 여기엔
    # watcher 가 없고 부모는 곧 재시작된다).
    try:
        log_handle.close()
    except OSError:
        pass

    logger.info(
        "셀프 재시작 child 기동: pid={} container={} argv={!r}",
        popen.pid,
        container,
        shlex.join(argv),
    )

    return RestartResult(
        pid=popen.pid,
        container=container,
        argv=argv,
        log_path=str(log_path),
    )


__all__ = [
    "DEFAULT_SELF_RESTART_CONTAINER",
    "RestartResult",
    "SELF_RESTART_CONTAINER_ENV_VAR",
    "SYSTEM_RESTART_LOG_DIRNAME",
    "SYSTEM_RESTART_LOG_FILENAME",
    "resolve_container_name",
    "system_restart_log_path",
    "trigger_self_restart",
]
