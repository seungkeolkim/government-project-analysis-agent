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
    assert "셀프 재시작 트리거" in content
    assert restart.DEFAULT_SELF_RESTART_CONTAINER in content


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
