"""cron 작업 CLI(app.scheduler.run_job) 단위 테스트 (task 00155-1).

검증 포인트:
    - 각 서브커맨드(scrape/backup/daily-report/gc)가 대응 서비스 함수를 호출한다.
    - scrape 의 --sources 파싱이 active_sources 리스트로 start_scrape_run 에 전달된다.
    - 정상 완료는 종료 코드 0, 실패/환경 비정상/잘못된 서브커맨드는 비0.
    - 잘못된/누락된 서브커맨드는 사용법 메시지를 출력한다.

서비스 함수는 모두 mock 으로 대체해 실제 수집/발송/백업이 일어나지 않게 한다.
configure_logging 도 patch 해 로깅 부수효과를 차단한다.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.scheduler import run_job
from app.scrape_control import ComposeEnvironmentError, ScrapeAlreadyRunningError


@pytest.fixture(autouse=True)
def _silence_logging_setup():
    """모든 테스트에서 configure_logging/get_settings 부수효과를 차단한다."""
    with patch.object(run_job, "configure_logging") as configure_mock, patch.object(
        run_job, "get_settings"
    ) as settings_mock:
        settings_mock.return_value = object()
        yield configure_mock


# ──────────────────────────────────────────────────────────────
# scrape
# ──────────────────────────────────────────────────────────────


def test_scrape_dispatches_start_scrape_run_with_sources():
    """scrape --sources iris,ntis 가 active_sources 리스트로 start_scrape_run 호출."""
    start_mock = MagicMock(return_value=SimpleNamespace(scrape_run_id=7, pid=123))
    with patch.object(run_job, "validate_host_project_dir"), patch.object(
        run_job, "start_scrape_run", start_mock
    ):
        exit_code = run_job.main(["scrape", "--sources", "iris, ntis"])

    assert exit_code == run_job.EXIT_SUCCESS
    start_mock.assert_called_once_with(["iris", "ntis"], trigger="scheduled")


def test_scrape_without_sources_passes_empty_list():
    """--sources 미지정 시 빈 리스트(=enabled 전체)로 호출된다."""
    start_mock = MagicMock(return_value=SimpleNamespace(scrape_run_id=1, pid=1))
    with patch.object(run_job, "validate_host_project_dir"), patch.object(
        run_job, "start_scrape_run", start_mock
    ):
        exit_code = run_job.main(["scrape"])

    assert exit_code == run_job.EXIT_SUCCESS
    start_mock.assert_called_once_with([], trigger="scheduled")


def test_scrape_already_running_is_benign_skip():
    """이미 수집 중이면 종료 코드 0(양성 skip)."""
    already = ScrapeAlreadyRunningError(running_run_id=5, running_trigger="manual")
    with patch.object(run_job, "validate_host_project_dir"), patch.object(
        run_job, "start_scrape_run", MagicMock(side_effect=already)
    ):
        exit_code = run_job.main(["scrape"])

    assert exit_code == run_job.EXIT_SUCCESS


def test_scrape_host_project_dir_invalid_returns_env_code():
    """HOST_PROJECT_DIR 검증 실패는 비0(EXIT_ENVIRONMENT_INVALID) 반환."""
    with patch.object(
        run_job,
        "validate_host_project_dir",
        MagicMock(side_effect=ComposeEnvironmentError("HOST_PROJECT_DIR 미설정")),
    ), patch.object(run_job, "start_scrape_run") as start_mock:
        exit_code = run_job.main(["scrape"])

    assert exit_code == run_job.EXIT_ENVIRONMENT_INVALID
    # 환경이 비정상이면 수집을 아예 시작하지 않는다.
    start_mock.assert_not_called()


def test_scrape_unexpected_exception_returns_failure():
    """수집 시작 중 일반 예외는 비0(EXIT_FAILURE) 반환."""
    with patch.object(run_job, "validate_host_project_dir"), patch.object(
        run_job, "start_scrape_run", MagicMock(side_effect=RuntimeError("boom"))
    ):
        exit_code = run_job.main(["scrape"])

    assert exit_code == run_job.EXIT_FAILURE


# ──────────────────────────────────────────────────────────────
# backup
# ──────────────────────────────────────────────────────────────


def test_backup_dispatches_run_backup():
    """backup 이 app.backup.service.run_backup 를 scheduled 트리거로 호출."""
    history = SimpleNamespace(
        id=3, success=True, backup_files=2, duration_seconds=1.5
    )
    run_backup_mock = MagicMock(return_value=history)
    with patch("app.backup.service.run_backup", run_backup_mock):
        exit_code = run_job.main(["backup"])

    assert exit_code == run_job.EXIT_SUCCESS
    assert run_backup_mock.call_count == 1
    # trigger 키워드가 전달됐는지 확인.
    _, kwargs = run_backup_mock.call_args
    assert "trigger" in kwargs


def test_backup_exception_returns_failure():
    """백업 중 예외는 비0 반환."""
    with patch("app.backup.service.run_backup", MagicMock(side_effect=OSError("disk"))):
        exit_code = run_job.main(["backup"])

    assert exit_code == run_job.EXIT_FAILURE


# ──────────────────────────────────────────────────────────────
# daily-report
# ──────────────────────────────────────────────────────────────


def test_daily_report_dispatches_prepare_and_send():
    """daily-report 가 prepare_and_send_daily_report 를 호출한다."""
    result = SimpleNamespace(
        run_id=39,
        status=SimpleNamespace(value="success"),
        snapshot_count=10,
        recipient_count=6,
        success_count=5,
        failure_count=1,
    )
    prepare_mock = MagicMock(return_value=result)
    session_cm = MagicMock()
    session_cm.__enter__.return_value = MagicMock()
    session_cm.__exit__.return_value = False

    with patch("app.db.session.session_scope", MagicMock(return_value=session_cm)), patch(
        "app.email.daily_report.collect_recipient_emails",
        MagicMock(return_value=["a@example.com"]),
    ), patch(
        "app.email.daily_report.prepare_and_send_daily_report", prepare_mock
    ), patch(
        "app.email.transport.factory.build_transport_from_settings",
        MagicMock(return_value=MagicMock()),
    ), patch(
        "app.backup.service.get_setting", MagicMock(return_value=None)
    ):
        exit_code = run_job.main(["daily-report"])

    assert exit_code == run_job.EXIT_SUCCESS
    assert prepare_mock.call_count == 1


def test_daily_report_gate_disabled_is_benign_skip():
    """게이트 비활성(EmailSendingDisabledError)은 종료 코드 0."""
    from app.email.gate import EmailSendingDisabledError

    session_cm = MagicMock()
    session_cm.__enter__.return_value = MagicMock()
    session_cm.__exit__.return_value = False

    with patch("app.db.session.session_scope", MagicMock(return_value=session_cm)), patch(
        "app.email.daily_report.collect_recipient_emails",
        MagicMock(return_value=[]),
    ), patch(
        "app.email.daily_report.prepare_and_send_daily_report",
        MagicMock(side_effect=EmailSendingDisabledError("게이트 비활성")),
    ), patch(
        "app.email.transport.factory.build_transport_from_settings",
        MagicMock(return_value=MagicMock()),
    ), patch(
        "app.backup.service.get_setting", MagicMock(return_value=None)
    ):
        exit_code = run_job.main(["daily-report"])

    assert exit_code == run_job.EXIT_SUCCESS


def test_daily_report_exception_returns_failure():
    """발송 흐름 중 일반 예외는 비0 반환."""
    with patch(
        "app.db.session.session_scope", MagicMock(side_effect=RuntimeError("db"))
    ):
        exit_code = run_job.main(["daily-report"])

    assert exit_code == run_job.EXIT_FAILURE


# ──────────────────────────────────────────────────────────────
# gc
# ──────────────────────────────────────────────────────────────


def test_gc_dispatches_run_gc():
    """gc 가 run_gc(dry_run=False) 를 호출한다."""
    report = SimpleNamespace(
        skipped_due_to_running_scrape_run=False,
        scanned_root="/data",
        disk_file_count=10,
        db_referenced_count=8,
        deleted_count=2,
        deletion_failed=[],
        removed_directory_count=1,
        total_orphan_bytes=2048,
    )
    run_gc_mock = MagicMock(return_value=report)
    with patch.object(run_job, "run_gc", run_gc_mock):
        exit_code = run_job.main(["gc"])

    assert exit_code == run_job.EXIT_SUCCESS
    run_gc_mock.assert_called_once_with(dry_run=False)


def test_gc_skipped_due_to_running_scrape_is_benign():
    """수집 진행 중이라 GC 가 거부되면 종료 코드 0(양성 skip)."""
    report = SimpleNamespace(skipped_due_to_running_scrape_run=True)
    with patch.object(run_job, "run_gc", MagicMock(return_value=report)):
        exit_code = run_job.main(["gc"])

    assert exit_code == run_job.EXIT_SUCCESS


def test_gc_exception_returns_failure():
    """GC 중 예외는 비0 반환."""
    with patch.object(run_job, "run_gc", MagicMock(side_effect=RuntimeError("io"))):
        exit_code = run_job.main(["gc"])

    assert exit_code == run_job.EXIT_FAILURE


# ──────────────────────────────────────────────────────────────
# 잘못된/누락된 서브커맨드
# ──────────────────────────────────────────────────────────────


def test_missing_subcommand_returns_usage_error(capsys):
    """서브커맨드 없이 호출하면 사용법 출력 + 비0 종료."""
    exit_code = run_job.main([])

    assert exit_code == run_job.EXIT_USAGE_ERROR
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()


def test_unknown_subcommand_returns_usage_error(capsys):
    """알 수 없는 서브커맨드는 비0 종료(argparse 가 사용법/에러 출력)."""
    exit_code = run_job.main(["frobnicate"])

    assert exit_code != run_job.EXIT_SUCCESS
    captured = capsys.readouterr()
    # argparse 는 invalid choice 메시지를 stderr 에 낸다.
    assert "invalid choice" in captured.err.lower() or "usage" in captured.err.lower()
