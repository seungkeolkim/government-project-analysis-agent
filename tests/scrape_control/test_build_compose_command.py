"""build_compose_command 의 HOST_PROJECT_DIR 분기 회귀 테스트 (task 00134).

배경:
    공고 수집 실행 시 app 컨테이너의 uvicorn 프로세스가
    ``os.environ['HOST_PROJECT_DIR']`` 를 빈 값으로 읽어
    ``ComposeEnvironmentError('환경변수 HOST_PROJECT_DIR 가 설정되지
    않았습니다 …')`` 를 던지고 ScrapeRun 이 failed 로 마감되는 버그가 있었다.

    근본 원인은 docker-compose.yml 의 app 서비스가 HOST_PROJECT_DIR 를
    ``env_file: - .env`` 와 ``environment: - HOST_PROJECT_DIR=${HOST_PROJECT_DIR:-}``
    양쪽에서 중복 정의한 데 있다. docker compose 에서 environment 는 env_file
    보다 우선순위가 높고, ``${HOST_PROJECT_DIR:-}`` 보간이 빈 값으로 떨어지면
    그 빈 값이 env_file 의 정상값을 덮어쓴다. 수정(00134)에서 environment 의
    HOST_PROJECT_DIR 항목을 제거해 env_file 을 단일 출처로 만들었다.

검증 대상:
    docker-compose.yml 자체는 단위 테스트로 직접 검증하기 어렵지만,
    수정 후에도 변하지 않는 계약은 'app 컨테이너의 HOST_PROJECT_DIR 가
    정상이면 build_compose_command 가 올바른 argv 를 만들고, 미설정/빈 값이면
    ComposeEnvironmentError 를 던진다' 는 것이다. 이 테스트는 그 분기를
    ``os.environ`` 의 HOST_PROJECT_DIR 유/무를 monkeypatch 로 시뮬레이션해
    덮는다 — compose 수정으로 컨테이너 env 가 항상 정상이 되더라도, 이
    함수의 방어 로직 자체가 회귀하지 않도록 고정한다.

docker 의존성 제거:
    build_compose_command 는 내부에서 ``_resolve_docker_binary()`` 로 docker
    CLI 경로를 찾는다. 테스트 환경에 docker 가 없을 수 있으므로 해당 함수를
    고정 경로를 반환하도록 monkeypatch 해 HOST_PROJECT_DIR 분기에만 집중한다.
"""

from __future__ import annotations

import pytest

from app.scrape_control import runner
from app.scrape_control.constants import (
    COMPOSE_FILE_IN_CONTAINER,
    DEFAULT_COMPOSE_PROJECT_NAME,
    HOST_PROJECT_DIR_ENV_VAR,
)
from app.scrape_control.runner import (
    ComposeEnvironmentError,
    build_compose_command,
)

# 테스트용 가짜 docker CLI 절대경로. 실제 파일일 필요는 없다 —
# build_compose_command 는 이 값을 argv[0] 에 넣기만 한다.
_FAKE_DOCKER_BINARY = "/usr/bin/docker"

# 테스트용 호스트 프로젝트 루트 경로.
_TEST_HOST_PROJECT_DIR = "/home/user/workspace/iris-agent"


@pytest.fixture(autouse=True)
def _stub_docker_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_docker_binary 를 고정 경로 반환으로 대체한다.

    실제 docker CLI 설치 여부와 무관하게 HOST_PROJECT_DIR 분기만 검증하기
    위함이다. build_compose_command 는 docker 바이너리 해석을 먼저 수행하므로,
    이 stub 이 없으면 docker 미설치 환경에서 테스트가 ComposeEnvironmentError
    (docker CLI 미발견) 로 잘못 실패할 수 있다.
    """
    monkeypatch.setattr(
        runner, "_resolve_docker_binary", lambda: _FAKE_DOCKER_BINARY
    )


def test_build_compose_command_uses_host_project_dir_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOST_PROJECT_DIR 가 설정돼 있으면 정상 argv 를 만든다.

    핵심 검증: ``--project-directory`` 다음 인자가 환경변수 값과 일치한다.
    이 값이 compose 파일의 상대경로를 호스트 기준으로 해석하는 데 쓰인다.
    """
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, _TEST_HOST_PROJECT_DIR)

    argv = build_compose_command([])

    assert argv[0] == _FAKE_DOCKER_BINARY
    assert argv[1] == "compose"
    # -f 다음에 컨테이너 내부 compose 파일 경로가 온다.
    assert "-f" in argv
    assert argv[argv.index("-f") + 1] == COMPOSE_FILE_IN_CONTAINER
    # --project-directory 다음 인자가 HOST_PROJECT_DIR 값이어야 한다.
    assert "--project-directory" in argv
    assert (
        argv[argv.index("--project-directory") + 1] == _TEST_HOST_PROJECT_DIR
    )
    # 프로젝트 이름 기본값과 scraper 서비스명이 포함된다.
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == DEFAULT_COMPOSE_PROJECT_NAME
    assert argv[-1] == "scraper"


def test_build_compose_command_raises_when_host_project_dir_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOST_PROJECT_DIR 가 아예 없으면 ComposeEnvironmentError 를 던진다.

    docker-compose.yml 수정 전, environment 보간 실패로 컨테이너 env 의
    HOST_PROJECT_DIR 가 미설정되던 상황을 모사한다.
    """
    monkeypatch.delenv(HOST_PROJECT_DIR_ENV_VAR, raising=False)

    with pytest.raises(ComposeEnvironmentError) as exc_info:
        build_compose_command([])

    # 운영자 안내 메시지에 환경변수 이름이 포함되는지 확인한다.
    assert HOST_PROJECT_DIR_ENV_VAR in str(exc_info.value)


def test_build_compose_command_raises_when_host_project_dir_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOST_PROJECT_DIR 가 빈 문자열이어도 ComposeEnvironmentError 를 던진다.

    이번 버그의 정확한 형태 — environment 의 ``${HOST_PROJECT_DIR:-}`` 보간이
    빈 값으로 떨어져 env_file 의 정상값을 빈 문자열로 덮어쓴 경우 — 를
    재현한다. 미설정과 동일하게 취급돼야 한다(공백만 있는 값 포함).
    """
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, "   ")

    with pytest.raises(ComposeEnvironmentError) as exc_info:
        build_compose_command([])

    assert HOST_PROJECT_DIR_ENV_VAR in str(exc_info.value)
