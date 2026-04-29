"""Playwright E2E 테스트의 공유 fixture (task 00043-4).

본 conftest 는 ``tests/e2e/*`` 하위 테스트 모듈에서만 활성화된다 (pytest 의
디렉터리 단위 conftest 컨벤션). 따라서 다른 unit 테스트 (``tests/dashboard/``
등) 의 fixture 와 격리된다.

주요 fixture:
    - :func:`e2e_workspace` (module scope): tmp 디렉터리에 SQLite DB 파일과
      downloads 디렉터리를 만들고, env var 로 우회시킨 후 init_db + 시드 데이터
      INSERT 를 수행한다. teardown 에서 env 와 lru_cache 를 복원한다.
    - :func:`e2e_server` (module scope): :func:`e2e_workspace` 가 준비한 환경
      변수를 그대로 상속해 uvicorn 서브프로세스를 8001 포트로 띄운다. /dashboard
      엔드포인트가 200 으로 응답할 때까지 폴링 후 서버 base URL 을 yield 하고,
      teardown 에서 SIGTERM 으로 정리한다.
    - :func:`playwright_instance` (session scope): ``sync_playwright()`` 컨텍스트.
    - :func:`e2e_browser` (session scope): chromium headless 인스턴스.
    - :func:`e2e_page` (function scope): 새 ``BrowserContext`` 위의 새 ``Page``.

스킵 정책:
    Playwright chromium 바이너리가 다운로드돼 있지 않으면 (``playwright install
    chromium`` 미실행) 모듈 자체를 skip — 단위 테스트 흐름은 영향받지 않는다.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# Playwright 가 없거나 chromium 바이너리가 없을 때 모듈 자체를 skip — 단위 테스트
# 흐름이 깨지지 않도록 한다 (CI 환경에 따라 chromium 미설치 가능).
playwright_module = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright 가 설치되지 않은 환경에서는 E2E 테스트를 건너뜁니다.",
)

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


# ──────────────────────────────────────────────────────────────
# 상수 — 포트 / 호스트 / 헬스체크 타임아웃
# ──────────────────────────────────────────────────────────────


# 사용자 원문 task 00043 §3 — 운영 8000 포트 미점유. E2E 는 8001 격리.
E2E_HOST: str = "127.0.0.1"
E2E_PORT: int = 8001
E2E_BASE_URL: str = f"http://{E2E_HOST}:{E2E_PORT}"

# uvicorn 서브프로세스 헬스체크 — /dashboard 응답까지 최대 대기 (초).
E2E_SERVER_STARTUP_TIMEOUT_SECONDS: float = 20.0
# 서브프로세스 종료 (SIGTERM → wait) 에 허용할 대기 (초).
E2E_SERVER_SHUTDOWN_TIMEOUT_SECONDS: float = 5.0


# ──────────────────────────────────────────────────────────────
# DB / 환경 — module scope
# ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def e2e_workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """module 단위 E2E 작업 공간.

    동작:
        1. ``tmp_path_factory`` 로 격리된 디렉터리 생성.
        2. 그 안의 ``e2e.sqlite3`` 와 ``downloads/`` 를 환경변수로 우회시킨다
           (``DB_URL`` / ``DOWNLOAD_DIR``). pydantic-settings 가 env var 를
           ``.env`` 파일보다 우선시키므로 운영 DB ``data/db/app.sqlite3`` 와
           충돌하지 않는다.
        3. ``app.config.get_settings`` / ``app.db.session.reset_engine_cache`` 를
           재호출해 본 module 안의 import 시점 캐시를 비운다.
        4. ``init_db()`` 로 Alembic upgrade head 적용 + 본 module 의 시드 데이터
           insert.
        5. teardown — env / lru_cache 를 monkeypatch.undo() 로 복원해 후속 테스트
           모듈이 운영 설정 그대로 동작하도록 한다.

    Yields:
        ``{\"db_url\", \"db_path\", \"download_dir\"}`` dict — 다른 fixture 가
        같은 env 를 상속해 subprocess 에 넘길 수 있도록 노출한다.
    """
    monkeypatch = pytest.MonkeyPatch()
    workspace_dir = tmp_path_factory.mktemp("e2e_dashboard_workspace")
    db_path: Path = workspace_dir / "e2e.sqlite3"
    download_dir: Path = workspace_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    # SQLite URL — POSIX 절대 경로로 직렬화해 env 를 통해 subprocess 가 그대로
    # 읽도록 한다.
    db_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setenv("DOWNLOAD_DIR", str(download_dir))
    # E2E 동안 로그 노이즈 최소화 — uvicorn / loguru 모두 WARNING 으로 묶는다.
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    # lru_cache 무효화 — pytest 가 다른 테스트에서 미리 settings 를 잡아 뒀을
    # 가능성에 대비. (다른 unit 테스트와 같은 pytest 프로세스에서 본 모듈이
    # 뒤늦게 실행될 때 안전망.)
    from app.config import get_settings
    from app.db.init_db import init_db
    from app.db.session import SessionLocal, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()

    init_db()

    # 시드 INSERT — 본 module import 의 부수 효과로 두지 않고 명시 호출.
    from tests.e2e._seed import seed_dashboard_e2e_data

    session = SessionLocal()
    try:
        seed_dashboard_e2e_data(session)
        session.commit()
    finally:
        session.close()

    yield {
        "db_url": db_url,
        "db_path": db_path,
        "download_dir": download_dir,
    }

    # ── Teardown — env / 캐시 복원 ────────────────────────────────────
    monkeypatch.undo()
    get_settings.cache_clear()
    reset_engine_cache()


# ──────────────────────────────────────────────────────────────
# uvicorn subprocess — module scope
# ──────────────────────────────────────────────────────────────


def _is_port_in_use(host: str, port: int) -> bool:
    """``host:port`` 가 이미 listen 중인지 빠르게 확인한다.

    E2E 가 8001 포트를 점유하기 전, 다른 프로세스가 점유 중이면 즉시 명확한
    오류를 내기 위한 사전 검사다.

    Args:
        host: 검사할 호스트.
        port: 검사할 포트.

    Returns:
        listen 중이면 True, 아니면 False.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_socket:
        test_socket.settimeout(0.5)
        try:
            test_socket.connect((host, port))
        except (ConnectionRefusedError, OSError):
            return False
        return True


def _wait_for_server_ready(
    base_url: str,
    process: subprocess.Popen,
    timeout_seconds: float,
) -> None:
    """uvicorn 이 ``/dashboard`` 에 200 응답할 때까지 폴링한다.

    프로세스가 도중에 죽으면 (returncode 가 None 이 아니면) 즉시 stderr 를 모아
    ``RuntimeError`` 로 발생시킨다.

    Args:
        base_url:        \"http://host:port\" 형식의 base URL.
        process:         uvicorn ``Popen`` 핸들.
        timeout_seconds: 최대 대기 시간 (초).

    Raises:
        RuntimeError:  서브프로세스가 도중에 종료된 경우 (uvicorn 기동 실패).
        TimeoutError:  ``timeout_seconds`` 안에 200 응답이 오지 않은 경우.
    """
    deadline_monotonic = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    health_url = f"{base_url}/dashboard?base_date=2026-04-29"

    while time.monotonic() < deadline_monotonic:
        if process.poll() is not None:
            stderr_output = (process.stderr.read() if process.stderr else b"").decode(
                "utf-8", errors="replace"
            )
            stdout_output = (process.stdout.read() if process.stdout else b"").decode(
                "utf-8", errors="replace"
            )
            raise RuntimeError(
                f"uvicorn 서브프로세스가 기동 중 종료됐습니다 "
                f"(returncode={process.returncode}).\nstdout:\n{stdout_output}\n"
                f"stderr:\n{stderr_output}"
            )

        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last_error = exc

        time.sleep(0.2)

    raise TimeoutError(
        f"uvicorn /dashboard 가 {timeout_seconds}s 안에 200 응답하지 않았습니다. "
        f"last_error={last_error!r}"
    )


@pytest.fixture(scope="module")
def e2e_server(e2e_workspace: dict[str, Any]) -> Iterator[str]:
    """8001 포트에 uvicorn 서브프로세스를 띄우고 base URL 을 yield 한다.

    ``e2e_workspace`` 가 미리 설정한 ``DB_URL`` / ``DOWNLOAD_DIR`` env var 를 그대로
    상속해 subprocess 가 같은 SQLite 파일에 접근하도록 한다.

    Args:
        e2e_workspace: 격리된 DB / download 경로 dict (의존성 주입).

    Yields:
        ``http://127.0.0.1:8001`` (base URL).

    Raises:
        RuntimeError: 8001 포트가 이미 점유돼 있을 때 또는 uvicorn 이 기동에 실패한 때.
    """
    if _is_port_in_use(E2E_HOST, E2E_PORT):
        raise RuntimeError(
            f"E2E 포트 {E2E_HOST}:{E2E_PORT} 가 이미 점유돼 있습니다. "
            f"다른 인스턴스를 종료한 뒤 재실행하세요 (사용자 원문 task 00043 §3)."
        )

    # subprocess 가 ``app.web.main:app`` 을 import 하려면 PYTHONPATH 에 프로젝트
    # 루트 (= 현재 cwd) 가 포함돼야 한다. python -m uvicorn 은 자동으로 cwd 를
    # sys.path[0] 에 둬 import 경로가 잡힌다.
    server_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.web.main:app",
        "--host",
        E2E_HOST,
        "--port",
        str(E2E_PORT),
        "--log-level",
        "warning",
    ]

    process = subprocess.Popen(  # noqa: S603 — sys.executable + 정적 인자만 전달.
        server_command,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for_server_ready(
            base_url=E2E_BASE_URL,
            process=process,
            timeout_seconds=E2E_SERVER_STARTUP_TIMEOUT_SECONDS,
        )
    except Exception:
        # 헬스체크 실패 시 즉시 정리 후 예외를 그대로 던진다.
        process.kill()
        try:
            process.wait(timeout=E2E_SERVER_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        raise

    try:
        yield E2E_BASE_URL
    finally:
        # SIGTERM → wait → 안 되면 SIGKILL.
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=E2E_SERVER_SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=E2E_SERVER_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass


# ──────────────────────────────────────────────────────────────
# Playwright fixtures — session / function scope
# ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    """``sync_playwright()`` 컨텍스트를 session 단위로 1회 시작한다.

    Yields:
        활성 ``Playwright`` 인스턴스.
    """
    playwright = sync_playwright().start()
    try:
        yield playwright
    finally:
        playwright.stop()


@pytest.fixture(scope="session")
def e2e_browser(playwright_instance: Playwright) -> Iterator[Browser]:
    """chromium headless 브라우저를 session 단위로 1개 띄운다.

    호스트 의존성 (libgbm / libasound 등) 누락으로 ``BrowserType.launch`` 가
    실패하면 ``pytest.skip`` 으로 모듈 자체를 건너뛴다 — 단위 테스트 실행 흐름이
    같이 깨지지 않도록 한다. 누락된 라이브러리는 ``sudo playwright install-deps
    chromium`` (또는 apt 로 ``libgbm1`` / ``libasound2`` / ``libnss3`` 등) 로 설치
    한다.

    Yields:
        활성 ``Browser`` 인스턴스 — 각 테스트가 별도 ``BrowserContext`` 로 격리.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        browser = playwright_instance.chromium.launch(headless=True)
    except PlaywrightError as launch_error:
        # 흔한 실패 사례:
        #   - 시스템 lib 누락 (libgbm.so.1 / libnss3 등) → \"shared libraries\".
        #   - chromium 바이너리 자체 누락 → \"Executable doesn't exist\".
        # 어느 쪽이든 본 환경에선 E2E 를 돌릴 수 없으므로 skip — 단위 테스트
        # 흐름은 영향받지 않는다. 메시지에 사용자가 다음 단계로 무엇을 할지
        # 명시해 둔다.
        pytest.skip(
            f"chromium 기동 실패 (E2E 환경 미준비). 본 환경에서는 E2E 모듈을 "
            f"건너뜁니다. 해결: \"sudo playwright install-deps chromium\" 으로 "
            f"시스템 라이브러리 설치 또는 \"playwright install chromium\" 으로 "
            f"브라우저 캐시 다운로드. 원본 오류: {launch_error}"
        )
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def e2e_page(e2e_browser: Browser, e2e_server: str) -> Iterator[Page]:
    """테스트마다 새 ``BrowserContext`` + ``Page`` 를 만든다.

    각 테스트가 자체 쿠키/스토리지 상태를 갖도록 context 를 격리한다 (브라우저
    레벨 캐시는 공유 — 성능 vs 격리의 절충).

    Args:
        e2e_browser: session 단위 brwoser.
        e2e_server:  uvicorn 이 기동 중임을 보장하는 의존성 (URL 자체는 본
                     fixture 가 직접 사용하지 않지만 의존성 순서를 강제).

    Yields:
        새 ``Page``. 테스트 종료 시 context 가 닫히면서 page 도 함께 닫힌다.
    """
    # e2e_server 의존성은 \"먼저 띄워라\" 의미만 — base URL 자체는 테스트가
    # 직접 e2e_server fixture 를 받아서 navigate 한다.
    _ = e2e_server
    context: BrowserContext = e2e_browser.new_context()
    page: Page = context.new_page()
    try:
        yield page
    finally:
        context.close()
