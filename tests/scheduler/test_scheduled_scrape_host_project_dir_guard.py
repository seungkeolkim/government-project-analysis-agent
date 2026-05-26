"""``scheduled_scrape`` 진입 시 HOST_PROJECT_DIR fail-fast 자기-종료 회귀 테스트 (task 00148).

배경:
    task 00143 에서 app 부팅 경로의 ASGI startup 훅에 HOST_PROJECT_DIR
    fail-fast 검증을 넣어 stale **컨테이너** 가 빈 환경변수를 안고 부팅하는
    경로를 차단했다. 그러나 task 00148 의 라이브 진단에서 다음 사실이 확정됐다:

        - 호스트에서 별개로 떠 있는 **stale 프로세스** (예: 과거 UI smoke-test
          용으로 띄워진 ``python -c \"... uvicorn.run(app, ...)\"`` 잔류 서버) 가
          동일 ``data/db/app.sqlite3`` 의 ``scheduler_jobs`` 테이블을 공유하면서
          자기 APScheduler 로 ``scheduled_scrape`` 를 발사한다.
        - 그 stale 프로세스는 task 00131-1/-2 의 단일 인스턴스 lock·single-flight
          claim 가드 코드 이전 시점에 기동돼 해당 가드 코드가 메모리에 없다.
        - 동시에 ``HOST_PROJECT_DIR`` 가 호스트 쉘에 export 돼 있지 않아 매 cron
          주기마다 ``start_scrape_run → build_compose_command → validate_host_project_dir``
          에서 ``ComposeEnvironmentError`` 를 던지고, ScrapeRun row 가
          ``failed`` 로 마감된다.

    근본 수정: ``scheduled_scrape`` 진입 직후, 가장 먼저 ``validate_host_project_dir``
    를 호출하고 실패하면 ``logger.critical`` + ``os._exit(2)`` 로 프로세스 자체
    를 즉시 종료한다. 정상 컨테이너에는 영향이 없고, stale 인스턴스는 다음
    cron 주기에 자기를 죽여 silent-failure 의 원천을 끊는다.

검증 대상 (subtask 00148-1 acceptance_criteria):
    1. HOST_PROJECT_DIR 미설정/빈 값/공백뿐일 때:
        - ``scheduled_scrape`` 가 ``os._exit(2)`` 를 호출한다.
        - ``try_claim_schedule_slot`` / ``start_scrape_run`` 은 **호출되지 않는다**.
        - ``scheduler_job_claims`` 테이블에 claim row 가 INSERT 되지 않는다.
    2. HOST_PROJECT_DIR 정상일 때:
        - ``os._exit`` 는 호출되지 않는다.
        - 기존 흐름(claim 획득 → ``start_scrape_run``) 이 그대로 진행된다.
    3. '동일 cron 시각에 두 ScrapeRun 이 만들어지지 않는다' 회귀 가드:
        - stale 프로세스 시뮬레이션(빈 HOST_PROJECT_DIR) 호출은 ScrapeRun 을
          만들지 않고 자기-종료한다.
        - 같은 cron 시각에 정상 프로세스(설정된 HOST_PROJECT_DIR) 가 단 1건만
          ``start_scrape_run`` 을 부른다.

테스트 격리:
    ``os._exit`` 는 실제로 호출되면 pytest 프로세스 자체가 죽으므로 항상
    monkeypatch 로 가짜 함수에 위임해 호출 사실만 기록한다. ``validate_host_project_dir``
    가 던지는 ``ComposeEnvironmentError`` 는 ``os._exit`` 직전에 발생하며, 가짜
    ``_exit`` 는 호출을 기록하고 return 한다 — 그래서 본 테스트는 단순히 함수
    실행 후 호출 횟수·인자만 검증한다.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, func, select

from app.db.models import SchedulerJobClaim
from app.db.session import session_scope
from app.scheduler.job_guard import JOB_NAME_SCHEDULED_SCRAPE
from app.scheduler.job_runner import scheduled_scrape
from app.scrape_control.constants import HOST_PROJECT_DIR_ENV_VAR


# 테스트용 호스트 프로젝트 루트 경로 (실제 존재할 필요 없음 — 문자열 검증용).
_TEST_HOST_PROJECT_DIR = "/home/test/workspace/iris-agent"

# slot 결정성 확보용 고정 시각 (UTC). 같은 분 안의 값이면 slot_key 가 동일하므로
# job_guard.now_utc 를 이 값으로 묶어 모든 호출이 같은 슬롯을 본다.
_FIXED_NOW: datetime = datetime(2026, 5, 26, 0, 0, 5, tzinfo=UTC)


def _count_claims(job_name: str) -> int:
    """``scheduler_job_claims`` 에 쌓인 특정 job 의 claim row 수를 센다."""
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(SchedulerJobClaim)
                .where(SchedulerJobClaim.job_name == job_name)
            ).scalar_one()
        )


@pytest.fixture
def _patch_os_exit(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """``app.scheduler.job_runner.os._exit`` 를 가짜로 교체해 호출 인자를 기록한다.

    실제 ``os._exit`` 가 호출되면 pytest 프로세스 자체가 종료되므로, 본 테스트
    파일의 모든 시나리오는 반드시 이 픽스처로 ``os._exit`` 를 무력화한 뒤
    함수를 호출해야 한다.

    가짜 함수는 exit code 를 리스트에 기록한 뒤 ``SystemExit`` 를 던진다 —
    이 동작이 운영 환경의 'os._exit 호출 시점 이후 코드는 실행되지 않는다' 라는
    핵심 의미를 그대로 모사한다. 단순 no-op 으로 두면 가드 이후의
    ``try_claim_schedule_slot`` / ``start_scrape_run`` 까지 실행돼 검증이
    어긋난다. 테스트 호출 측에서는 ``pytest.raises(SystemExit)`` 로 받는다.
    """
    exit_codes: list[int] = []

    def fake_exit(code: int) -> None:
        exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("app.scheduler.job_runner.os._exit", fake_exit)
    return exit_codes


@pytest.fixture
def _freeze_guard_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """``job_guard.now_utc`` 를 고정 시각으로 묶어 slot 버킷을 결정적으로 만든다.

    test_job_guard.py 의 동명 픽스처와 동일 의도 — 가드의 시각 소스를 고정해
    한 테스트 안의 반복 호출이 같은 슬롯을 계산하도록 한다.
    """
    monkeypatch.setattr(
        "app.scheduler.job_guard.now_utc", lambda: _FIXED_NOW,
    )


def test_scheduled_scrape_exits_when_host_project_dir_missing(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    _patch_os_exit: list[int],
) -> None:
    """HOST_PROJECT_DIR 미설정 시 ``scheduled_scrape`` 가 즉시 ``os._exit(2)`` 호출 (1).

    stale 프로세스(빈 env) 시나리오. conftest autouse 픽스처가 주입한 기본
    HOST_PROJECT_DIR 를 ``delenv`` 로 제거해 stale 인스턴스 상태를 모사한다.
    """
    monkeypatch.delenv(HOST_PROJECT_DIR_ENV_VAR, raising=False)

    # 가드 이후 단계가 호출되면 안 된다는 것을 검증하기 위해, 호출되면
    # 즉시 실패하도록 sentinel 을 심어 둔다.
    claim_calls: list[str] = []
    start_calls: list[str] = []

    def fail_claim(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        claim_calls.append("called")
        return True

    def fail_start(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        start_calls.append("called")
        return SimpleNamespace(scrape_run_id=999, pid=1)

    monkeypatch.setattr(
        "app.scheduler.job_runner.try_claim_schedule_slot", fail_claim,
    )
    monkeypatch.setattr(
        "app.scheduler.job_runner.start_scrape_run", fail_start,
    )

    with pytest.raises(SystemExit) as exit_info:
        scheduled_scrape([])

    # 1-1. os._exit(2) 가 정확히 한 번 호출됐고 exit code 가 2 다.
    assert _patch_os_exit == [2]
    assert exit_info.value.code == 2
    # 1-2. claim 시도 / start_scrape_run 호출 모두 일어나지 않았다.
    assert claim_calls == []
    assert start_calls == []
    # 1-3. scheduler_job_claims 에 row 가 추가되지 않았다.
    assert _count_claims(JOB_NAME_SCHEDULED_SCRAPE) == 0


def test_scheduled_scrape_exits_when_host_project_dir_blank(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    _patch_os_exit: list[int],
) -> None:
    """HOST_PROJECT_DIR 가 공백뿐이면 미설정과 동일하게 자기-종료 (1, 추가 케이스).

    docker compose 보간 실패로 빈 문자열이 들어간 경우 — 이번 버그의 정확한
    형태 — 를 재현한다.
    """
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, "   ")

    monkeypatch.setattr(
        "app.scheduler.job_runner.try_claim_schedule_slot",
        lambda *a, **k: pytest.fail("공백뿐 HOST_PROJECT_DIR 인데 claim 이 호출됐다"),
    )
    monkeypatch.setattr(
        "app.scheduler.job_runner.start_scrape_run",
        lambda *a, **k: pytest.fail("공백뿐 HOST_PROJECT_DIR 인데 start_scrape_run 이 호출됐다"),
    )

    with pytest.raises(SystemExit):
        scheduled_scrape([])

    assert _patch_os_exit == [2]


def test_scheduled_scrape_proceeds_when_host_project_dir_set(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    _patch_os_exit: list[int],
    _freeze_guard_now: None,
) -> None:
    """HOST_PROJECT_DIR 정상이면 기존 흐름이 그대로 진행되고 ``os._exit`` 미호출 (2).

    정상 컨테이너 경로의 회귀 가드 — fail-fast 검증이 운영 경로를 막지 않음을
    확인한다.
    """
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, _TEST_HOST_PROJECT_DIR)

    start_calls: list[list[str]] = []

    def fake_start_scrape_run(active_sources, **kwargs):  # noqa: ANN001, ANN003
        """호출 인자만 기록하는 가짜 start_scrape_run."""
        start_calls.append(list(active_sources))
        return SimpleNamespace(scrape_run_id=42, pid=4242)

    monkeypatch.setattr(
        "app.scheduler.job_runner.start_scrape_run", fake_start_scrape_run,
    )

    scheduled_scrape([])

    # 2-1. os._exit 은 호출되지 않는다.
    assert _patch_os_exit == []
    # 2-2. start_scrape_run 이 정확히 한 번 호출된다 (claim 까지 정상 통과).
    assert len(start_calls) == 1
    assert start_calls[0] == []
    # 2-3. claim row 가 정확히 1건 INSERT 됐다 (정상 single-flight 통과).
    assert _count_claims(JOB_NAME_SCHEDULED_SCRAPE) == 1


def test_stale_and_healthy_processes_yield_single_scrape_run(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    _patch_os_exit: list[int],
    _freeze_guard_now: None,
) -> None:
    """동일 cron 시각에 stale + healthy 두 호출이 와도 ScrapeRun 은 1건만 (3).

    핵심 회귀: 라이브 환경에서 stale 프로세스(빈 HOST_PROJECT_DIR) 가 동일
    cron 슬롯에 ``scheduled_scrape`` 를 호출해도 ScrapeRun row 가 생성되지
    않아야 하고, 정상 프로세스(설정된 HOST_PROJECT_DIR) 가 단 1건만
    ``start_scrape_run`` 을 부른다.

    시뮬레이션:
        - 1차 호출 (stale): HOST_PROJECT_DIR 제거 후 호출 → ``os._exit(2)``,
          start_scrape_run 미호출, claim 미INSERT.
        - 2차 호출 (healthy): HOST_PROJECT_DIR 설정 후 같은 슬롯에 호출 →
          정상 흐름. 1차에서 claim 이 없었으므로 2차가 그대로 통과한다.
    """
    start_calls: list[list[str]] = []

    def fake_start_scrape_run(active_sources, **kwargs):  # noqa: ANN001, ANN003
        start_calls.append(list(active_sources))
        return SimpleNamespace(scrape_run_id=1, pid=1)

    monkeypatch.setattr(
        "app.scheduler.job_runner.start_scrape_run", fake_start_scrape_run,
    )

    # 1차: stale 프로세스 호출 — 자기-종료한다.
    monkeypatch.delenv(HOST_PROJECT_DIR_ENV_VAR, raising=False)
    with pytest.raises(SystemExit):
        scheduled_scrape([])
    assert _patch_os_exit == [2]
    assert start_calls == []
    # stale 호출은 claim 도 시도하지 않으므로 row 가 없어야 정상 호출이
    # 자기 슬롯을 빼앗기지 않는다.
    assert _count_claims(JOB_NAME_SCHEDULED_SCRAPE) == 0

    # 2차: healthy 프로세스 호출 — 같은 슬롯에서 정상 진행된다.
    monkeypatch.setenv(HOST_PROJECT_DIR_ENV_VAR, _TEST_HOST_PROJECT_DIR)
    scheduled_scrape([])
    # 추가 _exit 호출은 없어야 한다.
    assert _patch_os_exit == [2]
    # start_scrape_run 은 healthy 호출에서 정확히 1번 호출된다.
    assert len(start_calls) == 1
    # claim row 도 healthy 호출분 1건만 추가됐다.
    assert _count_claims(JOB_NAME_SCHEDULED_SCRAPE) == 1
