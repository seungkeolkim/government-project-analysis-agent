"""HOST_PROJECT_DIR fail-fast 검증 테스트 (task 00143).

배경:
    task 67 스케줄 수집이 task 134 수정 이후에도 '환경변수 HOST_PROJECT_DIR 가
    설정되지 않았습니다' 로 실패했다 (2026-05-22 13:00:00).

    근본 원인:
        - docker-compose.yml 의 app 서비스는 HOST_PROJECT_DIR 를
          ``env_file: - .env`` 로 주입한다. env_file 은 컨테이너 **생성 시점** 에
          1회 평가돼 컨테이너 환경에 박힌다. task 134 가 고친 것은 compose
          **파일** 뿐이라, task 134 이전(버그 있는 compose)에 생성돼 계속 떠
          있는 stale 컨테이너는 여전히 빈 HOST_PROJECT_DIR 를 안고 있다.
        - ``scheduled_scrape`` 가 ComposeEnvironmentError 를 swallow 해 스케줄이
          매 주기 조용히 실패만 반복하는 silent-failure 구조였다.

    근본 수정: 빈/미설정 HOST_PROJECT_DIR 로는 app(웹+스케줄러)이 기동되지
    않도록, FastAPI ASGI startup 훅에서 ``validate_host_project_dir`` 로
    fail-fast 검증한다.

검증 대상:
    - ``validate_host_project_dir`` 헬퍼: 설정값 정상 반환 / 미설정·빈 값 거부.
    - ``build_compose_command`` 가 이 헬퍼를 재사용하므로 task 134 의
      'HOST_PROJECT_DIR 없으면 ComposeEnvironmentError' 계약이 유지된다.
    - app(FastAPI) ASGI startup 훅: HOST_PROJECT_DIR 가 비어 있으면 app 기동
      자체가 ComposeEnvironmentError 로 실패한다 (silent-failure 차단).

docker 의존성 제거:
    ``build_compose_command`` 는 내부에서 ``_resolve_docker_binary()`` 로 docker
    CLI 경로를 찾는다. 테스트 환경에 docker 가 없을 수 있으므로 해당 함수를
    고정 경로 반환으로 monkeypatch 해 HOST_PROJECT_DIR 분기에만 집중한다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from app.scrape_control import runner
from app.scrape_control.constants import HOST_PROJECT_DIR_ENV_VAR
from app.scrape_control.runner import (
    ComposeEnvironmentError,
    build_compose_command,
    validate_host_project_dir,
)

# 테스트용 호스트 프로젝트 루트 경로 (실제 존재할 필요는 없다 — 문자열로만 쓰인다).
_TEST_HOST_PROJECT_DIR = "/home/user/workspace/iris-agent"

# 테스트용 가짜 docker CLI 절대경로.
_FAKE_DOCKER_BINARY = "/usr/bin/docker"


# ──────────────────────────────────────────────────────────────
# validate_host_project_dir 헬퍼 단위 테스트
# ──────────────────────────────────────────────────────────────


def test_validate_returns_value_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOST_PROJECT_DIR 가 정상 설정돼 있으면 그 값을 그대로 반환한다."""
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, _TEST_HOST_PROJECT_DIR)

    assert validate_host_project_dir() == _TEST_HOST_PROJECT_DIR


def test_validate_strips_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """앞뒤 공백이 있는 값은 정규화(strip)된 형태로 반환한다."""
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, f"  {_TEST_HOST_PROJECT_DIR}  ")

    assert validate_host_project_dir() == _TEST_HOST_PROJECT_DIR


def test_validate_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOST_PROJECT_DIR 가 아예 없으면 ComposeEnvironmentError 를 던진다.

    stale 컨테이너(task 134 이전 compose 로 생성)가 빈 HOST_PROJECT_DIR 를 안고
    있는 상황을 모사한다.
    """
    monkeypatch.delenv(HOST_PROJECT_DIR_ENV_VAR, raising=False)

    with pytest.raises(ComposeEnvironmentError) as exc_info:
        validate_host_project_dir()

    message = str(exc_info.value)
    # 운영자가 docker logs 에서 원인을 바로 알 수 있도록 환경변수 이름·설정
    # 예시·설계 문서 참조가 메시지에 포함돼야 한다.
    assert HOST_PROJECT_DIR_ENV_VAR in message
    assert "docs/scrape_control_design.md" in message


def test_validate_raises_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOST_PROJECT_DIR 가 공백뿐이어도 미설정과 동일하게 거부한다.

    docker compose 의 보간 실패로 env 가 빈 문자열/공백으로 떨어진 경우 —
    이번 버그의 정확한 형태 — 를 재현한다.
    """
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, "   ")

    with pytest.raises(ComposeEnvironmentError) as exc_info:
        validate_host_project_dir()

    assert HOST_PROJECT_DIR_ENV_VAR in str(exc_info.value)


# ──────────────────────────────────────────────────────────────
# build_compose_command 회귀 — 헬퍼 재사용 후에도 계약 유지
# ──────────────────────────────────────────────────────────────


def test_build_compose_command_still_raises_via_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_compose_command 가 공용 헬퍼를 재사용해도 ComposeEnvironmentError 계약을 유지한다.

    task 134 의 ``test_build_compose_command.py`` 회귀 테스트와 동일한 계약
    ('HOST_PROJECT_DIR 없으면 ComposeEnvironmentError') 이, 인라인 체크를
    ``validate_host_project_dir`` 로 추출한 뒤에도 깨지지 않음을 확인한다.
    """
    # docker CLI 미설치 환경에서도 HOST_PROJECT_DIR 분기만 검증하도록 stub.
    monkeypatch.setattr(
        runner, "_resolve_docker_binary", lambda: _FAKE_DOCKER_BINARY
    )
    monkeypatch.delenv(HOST_PROJECT_DIR_ENV_VAR, raising=False)

    with pytest.raises(ComposeEnvironmentError) as exc_info:
        build_compose_command([])

    assert HOST_PROJECT_DIR_ENV_VAR in str(exc_info.value)


# ──────────────────────────────────────────────────────────────
# app(FastAPI) ASGI startup 훅 — 부팅 시점 fail-fast
# ──────────────────────────────────────────────────────────────


def test_app_startup_aborts_when_host_project_dir_missing(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOST_PROJECT_DIR 미설정이면 app(FastAPI) 기동 자체가 실패한다 (task 00143).

    ``with TestClient(app)`` 진입은 FastAPI 의 ASGI startup 이벤트를 발화시킨다 —
    실제 ASGI 서버가 가동되는 worker 프로세스의 동작을 그대로 모사한다. 이
    시점에 HOST_PROJECT_DIR 가 비어 있으면 startup 훅이 ComposeEnvironmentError
    를 전파해 기동이 중단돼야 한다. 운영 환경에서는 이것이 컨테이너 재시작
    루프로 이어져, 매 스케줄 주기 조용히 실패하던 silent-failure 가 부팅 단계의
    명시적 실패로 전환된다.
    """
    from fastapi.testclient import TestClient

    from app.scheduler import stop_scheduler
    from app.web.main import create_app

    # conftest 의 autouse 픽스처가 넣어 둔 기본 HOST_PROJECT_DIR 를 제거해
    # stale 컨테이너(빈 HOST_PROJECT_DIR) 상황을 모사한다.
    monkeypatch.delenv(HOST_PROJECT_DIR_ENV_VAR, raising=False)

    app = create_app()
    try:
        with pytest.raises(ComposeEnvironmentError) as exc_info:
            with TestClient(app):
                pass

        assert HOST_PROJECT_DIR_ENV_VAR in str(exc_info.value)
    finally:
        # 검증은 start_scheduler() 이전에 실패하므로 스케줄러는 기동되지 않지만,
        # 직전/이후 테스트와의 _scheduler 싱글턴 누수를 막기 위해 방어적으로 정지.
        stop_scheduler(wait=False)


def test_app_startup_succeeds_when_host_project_dir_set(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOST_PROJECT_DIR 가 정상이면 app(FastAPI) 이 정상 기동된다 (회귀 가드).

    fail-fast 검증이 정상 환경의 기동까지 막지 않음을 확인한다 — conftest
    autouse 픽스처가 주입한 기본 HOST_PROJECT_DIR 로 startup 훅이 통과한다.
    """
    from fastapi.testclient import TestClient

    from app.scheduler import stop_scheduler
    from app.web.main import create_app

    # conftest autouse 픽스처가 이미 HOST_PROJECT_DIR 를 주입했지만, 의도를
    # 명시적으로 드러내기 위해 이 테스트에서 직접 한 번 더 설정한다.
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, _TEST_HOST_PROJECT_DIR)

    app = create_app()
    try:
        # startup 훅이 예외 없이 통과하면 with-블록 진입이 성공한다.
        with TestClient(app) as client:
            response = client.get("/")
            assert response.status_code == 200
    finally:
        stop_scheduler(wait=False)
