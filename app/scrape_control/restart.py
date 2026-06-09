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
from datetime import datetime
from pathlib import Path
from typing import IO, Callable

from loguru import logger

from app.config import PROJECT_ROOT
from app.scrape_control import runner
from app.timezone import format_kst, now_utc, to_kst


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
# 구조화 기동/재시작 이벤트 로그 (task 00162)
# ──────────────────────────────────────────────────────────────
#
# system_restart.log 한 줄을 파싱 가능한 고정 포맷으로 정의한다. 이전 task 161
# 까지는 'UI 재시작' 트리거 시에만 한 줄을 남겼지만, task 162 부터는 웹 컨테이너가
# **일반 기동**(컨테이너/프로세스 시작)될 때도 같은 파일에 한 줄을 남겨, 운영자가
# 로그만 보고 "shell 에서 수동으로 기동했는지(=startup)" vs. "관리자 화면의 재시작
# 버튼을 눌렀는지(=restart_via_ui)" 를 구분할 수 있게 한다.
#
# 라인 포맷:
#     [<KST ISO8601>] event=<event_type> key=value key=value ...
# 예시:
#     [2026-06-09T12:34:56.789+09:00] event=startup pid=1234
#     [2026-06-09T12:34:56.789+09:00] event=restart_via_ui container=iris-agent-web pid=4242 argv=sh -c sleep 1 && ...
#
# 파서(read_recent_startup_events)는 맨 앞 [..] 의 시각과 첫 토큰 event=<...> 만
# 구조적으로 해석하고, 나머지(argv 처럼 공백이 섞일 수 있는 값 포함)는 표시용
# message 로 통째로 보존한다. 따라서 argv 의 공백/`&&` 가 파싱을 깨지 않는다.

# 기동 이벤트 유형 식별자. 파일에 그대로 기록되는 토큰 값이다.
STARTUP_EVENT_TYPE_STARTUP: str = "startup"
STARTUP_EVENT_TYPE_RESTART_VIA_UI: str = "restart_via_ui"

# event_type → 사용자 표시용 한국어 라벨. 템플릿(162-2)이 event_type 분기 없이
# 그대로 출력할 수 있도록 파서가 이 매핑으로 type_label 을 채워 준다. 미등록
# 유형은 _STARTUP_EVENT_FALLBACK_LABEL 로 대체한다.
STARTUP_EVENT_TYPE_LABELS: dict[str, str] = {
    STARTUP_EVENT_TYPE_STARTUP: "일반 기동",
    STARTUP_EVENT_TYPE_RESTART_VIA_UI: "UI 재시작",
}

# 알 수 없는 event_type 의 표시 라벨(후방호환·미래 확장 대비).
_STARTUP_EVENT_FALLBACK_LABEL: str = "기타 기동"


@dataclass(frozen=True)
class StartupEvent:
    """system_restart.log 한 줄을 파싱한 결과(표시용).

    Attributes:
        timestamp_kst:     라인의 KST tz-aware 시각. 파싱 실패/없으면 None.
        timestamp_display: 화면 표시용 KST 문자열("YYYY-MM-DD HH:MM:SS").
                           timestamp_kst 가 None 이면 빈 문자열.
        event_type:        'startup' / 'restart_via_ui' / 그 외(legacy·미래값).
        type_label:        event_type 의 한국어 표시 라벨(템플릿이 그대로 출력).
        message:           event= 토큰 이후의 나머지 텍스트(부가 필드/argv 등).
                           legacy 라인은 라인 본문 전체가 들어온다.
        raw:               원본 라인(디버깅/후방호환 표시용).
    """

    timestamp_kst: datetime | None
    timestamp_display: str
    event_type: str
    type_label: str
    message: str
    raw: str


def _resolve_startup_event_label(event_type: str) -> str:
    """event_type 에 대응하는 사용자 표시용 한국어 라벨을 돌려준다.

    Args:
        event_type: 이벤트 유형 식별자('startup' 등).

    Returns:
        :data:`STARTUP_EVENT_TYPE_LABELS` 의 라벨. 미등록이면 fallback 라벨.
    """
    return STARTUP_EVENT_TYPE_LABELS.get(event_type, _STARTUP_EVENT_FALLBACK_LABEL)


def _format_startup_event_line(
    event_type: str, *, extra: dict[str, object] | None = None
) -> str:
    """구조화 이벤트 한 줄(개행 포함)을 만든다.

    포맷: ``[<KST ISO8601>] event=<event_type> key=value ...\n``. 모든 기록 함수
    (append_startup_event / trigger_self_restart)가 이 단일 출처를 거쳐 같은 포맷을
    쓰도록 한다.

    Args:
        event_type: 이벤트 유형('startup' / 'restart_via_ui' 등).
        extra:      라인에 덧붙일 부가 필드(``key=value`` 로 직렬화). 순서는
                    dict 삽입 순서를 따른다. 값에 공백이 있어도(예: argv) 파서는
                    message 로 통째 보존하므로 인용 처리는 하지 않는다.

    Returns:
        개행으로 끝나는 한 줄 문자열.
    """
    kst_now = to_kst(now_utc())
    timestamp_text = kst_now.isoformat() if kst_now is not None else "(unknown)"
    parts: list[str] = [f"event={event_type}"]
    for key, value in (extra or {}).items():
        parts.append(f"{key}={value}")
    return f"[{timestamp_text}] {' '.join(parts)}\n"


def append_startup_event(
    event_type: str,
    *,
    data_dir: Path | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """system_restart.log 에 구조화 이벤트 한 줄을 append 한다.

    부모 디렉터리를 멱등하게 생성하고 append 모드로 한 줄을 추가한다. 기록 실패는
    예외를 전파하지 않고 경고만 남긴다 — 이 함수는 부가 이력 기록일 뿐이며,
    호출자(웹 startup 훅·재시작 트리거)의 본 동작을 막아서는 안 되기 때문이다.

    Args:
        event_type: 'startup'(일반 기동) / 'restart_via_ui'(UI 재시작) 등.
        data_dir:   로그 파일 위치 override(테스트 주입용). None 이면 기본 경로.
        extra:      라인에 덧붙일 부가 필드(pid/container/argv 등).
    """
    log_path = system_restart_log_path(data_dir=data_dir)
    line = _format_startup_event_line(event_type, extra=extra)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # append 모드 — 동시에 다른 핸들(예: 재시작 child 의 stdout)이 같은 파일을
        # 열고 있어도 POSIX O_APPEND 가 각 write 를 파일 끝에 원자적으로 붙인다.
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        logger.warning(
            "기동 이벤트 로그 기록 실패(무시하고 진행): event={} ({}: {})",
            event_type,
            type(exc).__name__,
            exc,
        )


def _parse_startup_event_line(line: str) -> StartupEvent | None:
    """system_restart.log 한 줄을 :class:`StartupEvent` 로 파싱한다.

    파싱 규칙:
        - 맨 앞이 ``[`` 로 시작하고 ``]`` 가 있으면 그 사이를 KST ISO8601 시각으로
          해석한다(실패하면 시각 None). 나머지를 본문으로 본다.
        - 본문의 첫 토큰이 ``event=<type>`` 이면 그 값을 event_type 으로, 이후
          텍스트를 message 로 본다.
        - ``event=`` 토큰이 없는 **legacy 라인**(task 161 의 '셀프 재시작 트리거:
          ...' 형태)은 후방호환을 위해 ``restart_via_ui`` 로 인식하고 본문 전체를
          message 로 둔다.

    Args:
        line: 원본 라인(앞뒤 공백 제거된 비어있지 않은 문자열 가정).

    Returns:
        파싱된 :class:`StartupEvent`. 의미 있는 정보가 전혀 없으면 None.
    """
    timestamp_kst: datetime | None = None
    body = line

    # 1) 선두 [<시각>] 블록 분리.
    if line.startswith("["):
        close_index = line.find("]")
        if close_index != -1:
            timestamp_text = line[1:close_index].strip()
            try:
                parsed = datetime.fromisoformat(timestamp_text)
            except ValueError:
                parsed = None
            # to_kst 로 KST tz-aware 로 정규화(naive 면 UTC 가정). 표시 일관성 확보.
            timestamp_kst = to_kst(parsed) if parsed is not None else None
            body = line[close_index + 1 :].strip()

    # 2) 본문에서 event= 토큰 해석.
    first_token, _, remainder = body.partition(" ")
    if first_token.startswith("event="):
        event_type = first_token[len("event=") :]
        message = remainder.strip()
    else:
        # legacy 라인(event= 토큰 없음) — UI 재시작 트리거로 간주하고 본문 보존.
        event_type = STARTUP_EVENT_TYPE_RESTART_VIA_UI
        message = body

    # event_type 이 비어 있고 본문도 비면 의미 없는 라인 — skip.
    if not event_type and not message:
        return None

    return StartupEvent(
        timestamp_kst=timestamp_kst,
        timestamp_display=format_kst(timestamp_kst) if timestamp_kst is not None else "",
        event_type=event_type,
        type_label=_resolve_startup_event_label(event_type),
        message=message,
        raw=line,
    )


def read_recent_startup_events(
    *, limit: int = 30, data_dir: Path | None = None
) -> list[StartupEvent]:
    """system_restart.log 를 읽어 최신순으로 최근 기동 이벤트를 반환한다.

    파일은 append 순(=오래된 것이 위)이므로 읽은 뒤 역순으로 뒤집어 최신 이벤트가
    리스트 앞에 오게 한다. 파일 부재/빈 파일/깨진 라인은 graceful 하게 처리해
    **절대 예외를 전파하지 않는다** — 관리자 페이지가 이 결과로 500 이 나면 안 된다.

    Args:
        limit:    반환할 최대 건수. 음수면 빈 리스트로 본다(방어).
        data_dir: 로그 파일 위치 override(테스트 주입용).

    Returns:
        최신순 :class:`StartupEvent` 리스트(최대 ``limit`` 건). 파일이 없거나 읽기
        실패면 빈 리스트.
    """
    if limit < 0:
        return []

    log_path = system_restart_log_path(data_dir=data_dir)
    try:
        raw_text = log_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # 아직 한 번도 기록되지 않은 정상 상태 — 빈 이력.
        return []
    except OSError as exc:
        logger.warning(
            "기동 이력 로그 읽기 실패(빈 이력으로 처리): {} ({}: {})",
            log_path,
            type(exc).__name__,
            exc,
        )
        return []

    events: list[StartupEvent] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = _parse_startup_event_line(stripped)
        if parsed is not None:
            events.append(parsed)

    # 파일은 오래된→최신 순으로 append 되므로 뒤집어 최신순으로 만든다.
    events.reverse()
    return events[:limit]


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

    # 로그 파일 준비 — 부모 디렉터리를 멱등하게 생성한다. child(docker restart)의
    # stdout/stderr 도 이 파일로 direct 된다(아래 Popen).
    log_path = system_restart_log_path(data_dir=data_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # append 바이너리 + buffering=0 으로 즉시 flush(runner 와 동일 컨벤션).
    log_handle: IO[bytes] = log_path.open("ab", buffering=0)

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

    # 'UI 재시작' 이력을 구조화 라인으로 한 줄 남긴다(append_startup_event 단일
    # 출처 재사용 — 일반 기동/재시작이 같은 포맷을 공유). child 는 sleep 중이라
    # 아직 출력이 없어 이 헤더가 파일에서 child 출력보다 앞에 오며, 기록 실패는
    # append_startup_event 내부에서 swallow 되므로 재시작 자체를 막지 않는다.
    # pid 는 위 Popen 으로 확보한 뒤라 container/argv 와 함께 보존된다.
    append_startup_event(
        STARTUP_EVENT_TYPE_RESTART_VIA_UI,
        data_dir=data_dir,
        extra={
            "container": container,
            "pid": popen.pid,
            "argv": shlex.join(argv),
        },
    )

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
    "STARTUP_EVENT_TYPE_LABELS",
    "STARTUP_EVENT_TYPE_RESTART_VIA_UI",
    "STARTUP_EVENT_TYPE_STARTUP",
    "SYSTEM_RESTART_LOG_DIRNAME",
    "SYSTEM_RESTART_LOG_FILENAME",
    "StartupEvent",
    "append_startup_event",
    "read_recent_startup_events",
    "resolve_container_name",
    "system_restart_log_path",
    "trigger_self_restart",
]
