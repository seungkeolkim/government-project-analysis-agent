"""crontab 런타임 설치기 단위 테스트 (task 00155-3).

순수 생성기(:mod:`app.scheduler.crontab_generator`)에 런타임 값을 주입하는
:func:`build_runtime_environment` 와, ``crontab`` 명령으로 설치를 시도하는
:func:`install_crontab` 의 graceful 동작을 검증한다.

핵심 검증:
    - env_file 이 컨테이너 마운트 규약(${HOST_PROJECT_DIR}/.env, task 00143)을
      따르고, HOST_PROJECT_DIR 미설정 시 ``.env`` 로 폴백한다.
    - crontab 바이너리가 없는 환경(개발/CI)에서 예외 없이 no-op 으로 동작한다
      (후속 00155-4 admin 라우트가 500 나지 않기 위한 계약).
    - 바이너리가 있으면 렌더된 텍스트를 stdin 으로 ``crontab -`` 에 넘긴다.
"""

from __future__ import annotations

import sys

import pytest
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, get_settings
from app.scheduler import crontab_installer
from app.scheduler.crontab_installer import (
    CRONTAB_BINARY_NAME,
    DEFAULT_JOB_PATH,
    CrontabInstallResult,
    build_runtime_environment,
    install_crontab,
    render_runtime_crontab,
)


# ──────────────────────────────────────────────────────────────
# build_runtime_environment
# ──────────────────────────────────────────────────────────────


def test_build_runtime_environment_uses_host_project_dir_for_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOST_PROJECT_DIR 가 설정되면 env_file 은 그 경로의 .env 절대경로가 된다."""
    monkeypatch.setenv("HOST_PROJECT_DIR", "/home/op/workspace/iris-agent")

    environment = build_runtime_environment()

    assert environment.env_file == "/home/op/workspace/iris-agent/.env"


def test_build_runtime_environment_strips_trailing_slash_on_host_project_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOST_PROJECT_DIR 끝에 슬래시가 있어도 .env 경로가 중복 슬래시 없이 만들어진다."""
    monkeypatch.setenv("HOST_PROJECT_DIR", "/home/op/iris-agent/")

    environment = build_runtime_environment()

    assert environment.env_file == "/home/op/iris-agent/.env"


def test_build_runtime_environment_falls_back_to_relative_env_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOST_PROJECT_DIR 미설정(개발 호스트)이면 env_file 은 관용적 ``.env`` 로 폴백한다."""
    monkeypatch.delenv("HOST_PROJECT_DIR", raising=False)

    environment = build_runtime_environment()

    assert environment.env_file == ".env"


def test_build_runtime_environment_runtime_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """python 실행기·로그 디렉터리·PATH·작업 디렉터리가 런타임 값으로 채워진다."""
    monkeypatch.delenv("HOST_PROJECT_DIR", raising=False)
    settings = get_settings()

    environment = build_runtime_environment(settings)

    # cron 의 빈약한 PATH 대비 — python 절대경로.
    assert environment.python_executable == sys.executable
    # 잡 cd 대상은 컨테이너 코드 루트(/app == PROJECT_ROOT).
    assert environment.project_dir == str(PROJECT_ROOT)
    # 로그 redirect 대상은 access_log_dir(data/logs) 재사용.
    assert environment.log_dir == str(settings.access_log_dir)
    # docker 바이너리 탐색을 위한 표준 PATH 가 crontab 상단 환경으로 박힌다.
    assert environment.extra_environment.get("PATH") == DEFAULT_JOB_PATH


# ──────────────────────────────────────────────────────────────
# render_runtime_crontab
# ──────────────────────────────────────────────────────────────


def test_render_runtime_crontab_contains_header_and_system_jobs(
    db_session: Session,
) -> None:
    """렌더 결과에 CRON_TZ 헤더와 (기본) 백업·GC 시스템 잡 라인이 포함된다."""
    text = render_runtime_crontab(db_session)

    assert "CRON_TZ=" in text
    assert "SHELL=/bin/bash" in text
    # 백업·GC 는 설정이 비어도 기본값으로 항상 포함된다(생성기 계약).
    assert "app.scheduler.run_job backup" in text
    assert "app.scheduler.run_job gc" in text
    # 잡 라인이 .env 를 source 하는 래퍼를 포함한다(cron 빈 환경 대비).
    assert "set -a && . " in text


# ──────────────────────────────────────────────────────────────
# install_crontab — graceful no-op / 성공 / 실패
# ──────────────────────────────────────────────────────────────


def test_install_crontab_noop_when_binary_missing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """crontab 바이너리가 없으면 예외 없이 installed=False 로 건너뛴다."""
    monkeypatch.setattr(crontab_installer.shutil, "which", lambda _name: None)

    result = install_crontab(db_session)

    assert isinstance(result, CrontabInstallResult)
    assert result.installed is False
    assert CRONTAB_BINARY_NAME in result.reason
    # 설치 실패와 무관하게 렌더 텍스트는 채워진다.
    assert "CRON_TZ=" in result.crontab_text


def test_install_crontab_invokes_crontab_with_rendered_text(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """바이너리가 있으면 렌더된 텍스트를 stdin 으로 ``crontab -`` 에 전달한다."""
    monkeypatch.setattr(
        crontab_installer.shutil, "which", lambda _name: "/usr/bin/crontab"
    )

    captured: dict[str, object] = {}

    def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")

        class _Completed:
            returncode = 0
            stderr = ""

        return _Completed()

    monkeypatch.setattr(crontab_installer.subprocess, "run", _fake_run)

    result = install_crontab(db_session)

    assert result.installed is True
    assert captured["argv"] == ["/usr/bin/crontab", "-"]
    # stdin 으로 넘긴 텍스트가 곧 렌더 텍스트여야 한다.
    assert captured["input"] == result.crontab_text
    assert "app.scheduler.run_job backup" in str(captured["input"])


def test_install_crontab_reports_failure_on_nonzero_exit(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """crontab 이 비0 종료하면 installed=False 와 stderr 사유를 담아 반환한다."""
    monkeypatch.setattr(
        crontab_installer.shutil, "which", lambda _name: "/usr/bin/crontab"
    )

    def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        class _Completed:
            returncode = 1
            stderr = "must be privileged to use -u"

        return _Completed()

    monkeypatch.setattr(crontab_installer.subprocess, "run", _fake_run)

    result = install_crontab(db_session)

    assert result.installed is False
    assert "must be privileged" in result.reason


def test_install_crontab_handles_os_error(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """crontab 실행 자체가 OSError 면 예외를 삼키고 installed=False 로 반환한다."""
    monkeypatch.setattr(
        crontab_installer.shutil, "which", lambda _name: "/usr/bin/crontab"
    )

    def _raise(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("exec format error")

    monkeypatch.setattr(crontab_installer.subprocess, "run", _raise)

    result = install_crontab(db_session)

    assert result.installed is False
    assert "exec format error" in result.reason


# ──────────────────────────────────────────────────────────────
# main — 종료 코드 규약
# ──────────────────────────────────────────────────────────────


def test_main_returns_zero_when_binary_absent(
    test_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cron 미설치(개발/CI) 환경에서 main 은 0(정상 건너뜀)을 반환한다."""
    # test_engine fixture 로 DB 가 마이그레이션된 상태 — main 이 session_scope 로
    # 직접 세션을 연다.
    monkeypatch.setattr(crontab_installer.shutil, "which", lambda _name: None)

    assert crontab_installer.main() == 0


def test_main_returns_one_when_install_fails(
    test_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """바이너리는 있으나 설치가 실패하면 main 은 1(운영 이상 신호)을 반환한다."""
    monkeypatch.setattr(
        crontab_installer.shutil, "which", lambda _name: "/usr/bin/crontab"
    )

    def _fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        class _Completed:
            returncode = 1
            stderr = "boom"

        return _Completed()

    monkeypatch.setattr(crontab_installer.subprocess, "run", _fake_run)

    assert crontab_installer.main() == 1
