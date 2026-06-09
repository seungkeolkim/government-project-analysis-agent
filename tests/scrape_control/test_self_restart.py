"""셀프 재시작 모듈(restart.py) 단위 테스트 (task 00161).

검증 대상:
    - trigger_self_restart 가 ``sh -c \"sleep N && <docker> restart <container>\"``
      형태 argv 를 구성한다(docker stop 이 아니라 restart 사용).
    - detached 옵션(preexec_fn=os.setsid, stdin=DEVNULL, close_fds=True) 과
      stdout/stderr 로그 파일 redirect 가 의도대로 전달된다.
    - 로그 파일 경로가 data_dir 하위 logs/system_restart.log 로 잡힌다.
    - 컨테이너 이름은 기본값(iris-agent-web) 과 SELF_RESTART_CONTAINER env
      override 양쪽을 따른다.
    - docker 바이너리 해석은 runner._resolve_docker_binary 단일 출처를 재사용한다.

docker 의존성 제거:
    실제 docker 를 호출하지 않도록 _popen_factory 와 _resolve_docker_binary 를
    monkeypatch 한다. 테스트는 전부 동기로 실행되며 어떤 프로세스도 띄우지 않는다.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.scrape_control import restart, runner

# 테스트용 가짜 docker CLI 절대경로. 실제 파일일 필요는 없다 — argv 구성에만 쓰인다.
_FAKE_DOCKER_BINARY = "/usr/bin/docker"


class _FakePopen:
    """subprocess.Popen 을 대체하는 가짜 — 호출 인자를 기록하고 pid 를 흉내낸다."""

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """_popen_factory 와 _resolve_docker_binary 를 가짜로 대체한다.

    Returns:
        마지막으로 생성된 _FakePopen 을 담는 dict (테스트가 인자를 검사).
    """
    captured: dict[str, Any] = {}

    def _factory(argv: list[str], **kwargs: Any) -> _FakePopen:
        popen = _FakePopen(argv, **kwargs)
        captured["popen"] = popen
        return popen

    monkeypatch.setattr(restart, "_popen_factory", _factory)
    monkeypatch.setattr(runner, "_resolve_docker_binary", lambda: _FAKE_DOCKER_BINARY)
    return captured


def test_trigger_builds_restart_command(
    fake_popen: dict[str, Any], tmp_path: Path
) -> None:
    """argv 가 sh -c 'sleep N && docker restart <container>' 형태여야 한다."""
    result = restart.trigger_self_restart(data_dir=tmp_path)

    argv = result.argv
    assert argv[0] == "sh"
    assert argv[1] == "-c"
    shell_command = argv[2]
    # sleep 후 restart 순서, stop 이 아니라 restart 여야 한다.
    assert shell_command.startswith("sleep 1 &&")
    assert "restart" in shell_command
    assert "stop" not in shell_command
    assert _FAKE_DOCKER_BINARY in shell_command
    assert restart.DEFAULT_SELF_RESTART_CONTAINER in shell_command
    assert result.pid == 4242
    assert result.container == restart.DEFAULT_SELF_RESTART_CONTAINER


def test_trigger_uses_detached_options(
    fake_popen: dict[str, Any], tmp_path: Path
) -> None:
    """detached 옵션과 로그 redirect 가 Popen 에 전달돼야 한다."""
    restart.trigger_self_restart(data_dir=tmp_path)

    kwargs = fake_popen["popen"].kwargs
    # 새 세션 분리(start_new_session 과 동일 의도)는 preexec_fn=os.setsid 로.
    assert kwargs["preexec_fn"] is os.setsid
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    # stderr 는 stdout 으로 합치고, stdout 은 로그 파일 핸들이어야 한다.
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdout"] is not None


def test_trigger_writes_log_under_data_dir(
    fake_popen: dict[str, Any], tmp_path: Path
) -> None:
    """로그 파일이 <data_dir>/logs/system_restart.log 로 생성돼야 한다."""
    result = restart.trigger_self_restart(data_dir=tmp_path)

    expected = tmp_path / "logs" / "system_restart.log"
    assert result.log_path == str(expected)
    # 헤더 라인이 파일에 기록됐는지 확인 (mkdir + open 동작 검증).
    assert expected.is_file()
    content = expected.read_text(encoding="utf-8")
    # task 00162 — 구조화 라인(event=restart_via_ui)으로 기록되며 container/pid/argv
    # 정보가 보존된다.
    assert f"event={restart.STARTUP_EVENT_TYPE_RESTART_VIA_UI}" in content
    assert restart.DEFAULT_SELF_RESTART_CONTAINER in content
    assert "pid=4242" in content


def test_trigger_appends_restart_via_ui_event(
    fake_popen: dict[str, Any], tmp_path: Path
) -> None:
    """trigger_self_restart 가 read_recent_startup_events 로 읽히는 restart_via_ui
    이벤트를 남겨야 한다(파서 round-trip)."""
    restart.trigger_self_restart(data_dir=tmp_path)

    events = restart.read_recent_startup_events(data_dir=tmp_path)
    assert len(events) == 1
    assert events[0].event_type == restart.STARTUP_EVENT_TYPE_RESTART_VIA_UI
    assert events[0].type_label == "지금 재시작 버튼 트리거"
    assert restart.DEFAULT_SELF_RESTART_CONTAINER in events[0].message
    assert events[0].timestamp_kst is not None
    assert events[0].timestamp_display != ""


def test_container_name_env_override(
    fake_popen: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SELF_RESTART_CONTAINER env 가 있으면 그 이름으로 restart 한다."""
    monkeypatch.setenv(restart.SELF_RESTART_CONTAINER_ENV_VAR, "custom-web")

    result = restart.trigger_self_restart(data_dir=tmp_path)

    assert result.container == "custom-web"
    assert "custom-web" in result.argv[2]


def test_container_name_explicit_argument_wins(
    fake_popen: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """container_name 인자가 env override 보다 우선한다."""
    monkeypatch.setenv(restart.SELF_RESTART_CONTAINER_ENV_VAR, "env-web")

    result = restart.trigger_self_restart(
        container_name="explicit-web", data_dir=tmp_path
    )

    assert result.container == "explicit-web"


def test_system_restart_log_path_default() -> None:
    """data_dir 미주입 시 PROJECT_ROOT/data/logs/system_restart.log 를 가리킨다."""
    from app.config import PROJECT_ROOT

    path = restart.system_restart_log_path()
    assert path == PROJECT_ROOT / "data" / "logs" / "system_restart.log"


# ──────────────────────────────────────────────────────────────
# task 00162 — 구조화 기동 이벤트 로그 (append/read)
# ──────────────────────────────────────────────────────────────


def test_append_startup_event_writes_structured_line(tmp_path: Path) -> None:
    """append_startup_event 가 [시각] event=<type> ... 형태의 라인을 남긴다."""
    restart.append_startup_event(
        restart.STARTUP_EVENT_TYPE_STARTUP, data_dir=tmp_path, extra={"pid": 1234}
    )

    log_file = tmp_path / "logs" / "system_restart.log"
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")
    assert content.startswith("[")
    assert f"event={restart.STARTUP_EVENT_TYPE_STARTUP}" in content
    assert "pid=1234" in content
    # 한 번 호출 = 한 줄.
    assert content.count("\n") == 1


def test_append_startup_event_appends_in_order(tmp_path: Path) -> None:
    """여러 번 호출하면 파일에 append 되어 누적된다."""
    restart.append_startup_event(
        restart.STARTUP_EVENT_TYPE_STARTUP, data_dir=tmp_path, extra={"pid": 1}
    )
    restart.append_startup_event(
        restart.STARTUP_EVENT_TYPE_STARTUP, data_dir=tmp_path, extra={"pid": 2}
    )

    log_file = tmp_path / "logs" / "system_restart.log"
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "pid=1" in lines[0]
    assert "pid=2" in lines[1]


def test_read_recent_startup_events_missing_file_returns_empty(
    tmp_path: Path,
) -> None:
    """파일이 아직 없으면 빈 리스트를 반환한다(예외 없음)."""
    events = restart.read_recent_startup_events(data_dir=tmp_path)
    assert events == []


def test_read_recent_startup_events_empty_file_returns_empty(
    tmp_path: Path,
) -> None:
    """빈 파일/공백 라인만 있는 파일은 빈 리스트를 반환한다."""
    log_file = tmp_path / "logs" / "system_restart.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("\n   \n\n", encoding="utf-8")

    events = restart.read_recent_startup_events(data_dir=tmp_path)
    assert events == []


def test_read_recent_startup_events_newest_first(tmp_path: Path) -> None:
    """append 순(오래된 것이 위)인 파일을 최신순으로 뒤집어 반환한다."""
    for pid in (10, 20, 30):
        restart.append_startup_event(
            restart.STARTUP_EVENT_TYPE_STARTUP, data_dir=tmp_path, extra={"pid": pid}
        )

    events = restart.read_recent_startup_events(data_dir=tmp_path)
    assert [e.message for e in events] == ["pid=30", "pid=20", "pid=10"]
    assert all(e.event_type == restart.STARTUP_EVENT_TYPE_STARTUP for e in events)
    assert all(e.type_label == "재기동 완료" for e in events)


def test_read_recent_startup_events_respects_limit(tmp_path: Path) -> None:
    """limit 보다 많이 쌓여도 최신 limit 건만 반환한다."""
    for pid in range(5):
        restart.append_startup_event(
            restart.STARTUP_EVENT_TYPE_STARTUP, data_dir=tmp_path, extra={"pid": pid}
        )

    events = restart.read_recent_startup_events(limit=2, data_dir=tmp_path)
    assert len(events) == 2
    assert events[0].message == "pid=4"
    assert events[1].message == "pid=3"


def test_read_recent_startup_events_skips_broken_lines(tmp_path: Path) -> None:
    """깨진/형식 불명 라인이 섞여 있어도 예외 없이 처리한다."""
    log_file = tmp_path / "logs" / "system_restart.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        "[not-a-valid-timestamp] event=startup pid=7\n"
        "garbage line without brackets\n"
        "[2026-06-09T12:00:00+09:00] event=startup pid=8\n",
        encoding="utf-8",
    )

    events = restart.read_recent_startup_events(data_dir=tmp_path)
    # 세 줄 모두 어떤 형태로든 파싱되어 살아남는다(깨진 시각은 None 으로 graceful).
    assert len(events) == 3
    # 최신순(파일 역순): [2]event=startup pid=8 → [1]garbage → [0]event=startup pid=7.
    # events[0] = 정상 시각 + startup.
    assert events[0].event_type == restart.STARTUP_EVENT_TYPE_STARTUP
    assert events[0].timestamp_kst is not None
    assert events[0].message == "pid=8"
    # events[1] = 대괄호 없는 garbage 라인 → legacy 로 간주(restart_via_ui), 시각 None.
    assert events[1].event_type == restart.STARTUP_EVENT_TYPE_RESTART_VIA_UI
    assert events[1].timestamp_kst is None
    # events[2] = 시각 파싱 실패 라인 — event_type 은 살아 있고 timestamp 는 None.
    broken_timestamp = events[2]
    assert broken_timestamp.event_type == restart.STARTUP_EVENT_TYPE_STARTUP
    assert broken_timestamp.timestamp_kst is None
    assert broken_timestamp.timestamp_display == ""


def test_read_recent_startup_events_legacy_line_compat(tmp_path: Path) -> None:
    """event= 토큰 없는 legacy 라인(task 161 형태)은 restart_via_ui 로 인식한다."""
    log_file = tmp_path / "logs" / "system_restart.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        "[2026-06-01T09:00:00+09:00] 셀프 재시작 트리거: container=iris-agent-web "
        "argv=sh -c sleep 1 && /usr/bin/docker restart iris-agent-web\n",
        encoding="utf-8",
    )

    events = restart.read_recent_startup_events(data_dir=tmp_path)
    assert len(events) == 1
    assert events[0].event_type == restart.STARTUP_EVENT_TYPE_RESTART_VIA_UI
    assert events[0].type_label == "지금 재시작 버튼 트리거"
    # 본문 전체가 message 로 보존된다.
    assert "셀프 재시작 트리거" in events[0].message
    assert "iris-agent-web" in events[0].message


def test_read_recent_startup_events_negative_limit(tmp_path: Path) -> None:
    """음수 limit 은 방어적으로 빈 리스트를 반환한다."""
    restart.append_startup_event(
        restart.STARTUP_EVENT_TYPE_STARTUP, data_dir=tmp_path, extra={"pid": 1}
    )
    assert restart.read_recent_startup_events(limit=-1, data_dir=tmp_path) == []
