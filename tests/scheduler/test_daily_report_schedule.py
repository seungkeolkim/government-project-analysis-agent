"""Daily Report APScheduler 잡 CRUD 회귀 테스트 (task 00125-7 / Phase A-3).

검증 매트릭스 (디자인 노트 ``docs/phase_a3_design_note.md`` §8 의 4 케이스):

    A. ``register_daily_report_cron_schedule(cron, enabled=True)`` (잡 없음)
       → ``add_job`` 으로 신규 등록되고 ``ScheduleSummary`` 의 trigger_spec /
         job_id / next_run_time 이 기대값.
    B. ``register_daily_report_cron_schedule(cron2, enabled=True)`` (잡 있음)
       → ``reschedule_job`` + ``modify_job(name)`` 흐름. 같은 ``JOB_ID_DAILY_REPORT``
         로 cron 표현식만 갈아끼워진다 (job_id 보존).
    C. ``register_daily_report_cron_schedule(enabled=False)`` (잡 있음)
       → ``remove_job`` + None 반환.
    D. ``ensure_daily_report_cron_registered`` 동작
       → SystemSetting enabled=True 이면 cron 으로 등록, False 이면 등록하지 않음.
         스케줄러가 미기동이면 silent no-op.

추가 회귀 가드:
    - 잘못된 cron 표현식 → ``ScheduleValidationError``.
    - ``register(cron="", enabled=True)`` 도 비활성화 분기로 흐름 (빈 cron 가드).
    - ``list_general_schedules`` 가 daily report 잡을 제외하는지.
    - ``get_daily_report_schedule_summary`` 가 등록 잡을 단독 조회하는지.
    - ``register`` 인자 None → SystemSetting 에서 cron / enabled 직접 로드.

설계 결정:
    - 모듈 수준 ``_scheduler`` 싱글턴은 fixture 가 매 테스트 시작 시 ``start``,
      종료 시 ``stop`` 으로 재설정해 잡스토어 격리를 보장한다 — service 의 stop
      이 None 으로 리셋해 다음 테스트의 start 가 새 BackgroundScheduler 를
      만든다.
    - APScheduler 실 백그라운드 스레드가 떠 있어 잡 호출이 일어나면 안 된다.
      따라서 cron 표현식은 모두 \"먼 미래\" 패턴(매 분, 매 시간 등) 으로 둬도
      충분하다 — 본 테스트가 잡 실행을 검증하지 않고 등록·복원·제거만 본다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.email.constants import (
    SETTING_KEY_DAILY_REPORT_CRON,
    SETTING_KEY_DAILY_REPORT_ENABLED,
)
from app.scheduler import (
    JOB_ID_DAILY_REPORT,
    ScheduleValidationError,
    ensure_daily_report_cron_registered,
    get_daily_report_schedule_summary,
    list_general_schedules,
    register_daily_report_cron_schedule,
    remove_daily_report_cron_schedule,
    start_scheduler,
    stop_scheduler,
)


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def running_scheduler(test_engine: Engine) -> Iterator[None]:
    """테스트별로 격리된 BackgroundScheduler 를 기동·종료한다.

    service 의 ``stop`` 이 모듈 싱글턴을 None 으로 리셋하므로, 다음 테스트의
    ``start_scheduler()`` 호출이 새 BackgroundScheduler + 새 jobstore 를 생성해
    잡이 절대 누수되지 않는다.
    """
    # test_engine 픽스처가 새 DB + Alembic upgrade 까지 끝낸 후 호출되어야 한다.
    # 잡스토어가 같은 엔진을 ``get_engine()`` 으로 재사용하므로 순서 의존.
    start_scheduler()
    try:
        yield
    finally:
        # wait=False — 잡이 떠 있어도 즉시 종료. 본 테스트는 trigger 실행을 보지
        # 않으니 안전하다.
        stop_scheduler(wait=False)


def _set_setting_value(session: Session, key: str, value: str) -> None:
    """SystemSetting upsert 헬퍼. ``set_setting`` 은 commit 을 호출자에게 위임."""
    from app.backup.service import set_setting

    set_setting(session, key, value)
    session.commit()


# ──────────────────────────────────────────────────────────────
# Case A — 잡 없음 + enabled=True → add_job
# ──────────────────────────────────────────────────────────────


def test_register_daily_report_when_no_existing_job_adds_new_job(
    running_scheduler: None,
) -> None:
    """잡이 없는 상태에서 enabled=True 로 register 하면 신규 add_job 이 일어난다.

    검증 포인트:
        - 반환 ScheduleSummary 의 job_id == JOB_ID_DAILY_REPORT.
        - trigger_type == 'cron', trigger_spec 이 입력 cron 표현식 그대로.
        - enabled=True (next_run_time 채워짐).
    """
    cron_expression = "0 9 * * 1-5"

    summary = register_daily_report_cron_schedule(
        cron_expression=cron_expression, enabled=True,
    )

    # add 결과 검증
    assert summary is not None
    assert summary.job_id == JOB_ID_DAILY_REPORT
    assert summary.trigger_type == "cron"
    assert summary.trigger_spec == cron_expression
    assert summary.enabled is True
    assert summary.next_run_time is not None

    # get_daily_report_schedule_summary 가 동일 잡을 조회해 동일 값을 돌려주는지
    fetched = get_daily_report_schedule_summary()
    assert fetched is not None
    assert fetched.job_id == JOB_ID_DAILY_REPORT
    assert fetched.trigger_spec == cron_expression


# ──────────────────────────────────────────────────────────────
# Case B — 잡 있음 + enabled=True + 새 cron → reschedule
# ──────────────────────────────────────────────────────────────


def test_register_daily_report_when_job_exists_reschedules_same_id(
    running_scheduler: None,
) -> None:
    """이미 잡이 있는 상태에서 다른 cron 으로 register 하면 같은 job_id 로 reschedule.

    검증 포인트:
        - 두 번 호출 후에도 daily report 잡은 정확히 1건만 존재 (중복 add 안 함).
        - 두 번째 호출의 trigger_spec 이 새 cron 으로 갱신됨.
        - job_id 는 양 호출에서 JOB_ID_DAILY_REPORT 로 고정.
    """
    first_cron = "0 9 * * 1-5"
    second_cron = "30 8 * * *"

    first_summary = register_daily_report_cron_schedule(
        cron_expression=first_cron, enabled=True,
    )
    assert first_summary is not None
    assert first_summary.trigger_spec == first_cron

    second_summary = register_daily_report_cron_schedule(
        cron_expression=second_cron, enabled=True,
    )
    assert second_summary is not None
    # 같은 job_id 가 유지된다 — reschedule 이지 신규 add 아니다.
    assert second_summary.job_id == JOB_ID_DAILY_REPORT
    assert second_summary.job_id == first_summary.job_id
    # cron 표현식만 갈아끼워졌다.
    assert second_summary.trigger_spec == second_cron

    # list_schedules 에서 daily report 잡은 1건만 존재해야 한다.
    fetched = get_daily_report_schedule_summary()
    assert fetched is not None
    assert fetched.trigger_spec == second_cron


# ──────────────────────────────────────────────────────────────
# Case C — 잡 있음 + enabled=False → remove_job + None
# ──────────────────────────────────────────────────────────────


def test_register_daily_report_with_enabled_false_removes_existing_job(
    running_scheduler: None,
) -> None:
    """잡이 있는 상태에서 enabled=False 로 register 하면 기존 잡이 제거된다.

    cron 인자는 무시되어도(혹은 None) 무방하다 — 디자인 노트 §8 의 비활성화
    분기 시맨틱과 일치.
    """
    # 사전 조건: 잡 1건 등록.
    register_daily_report_cron_schedule(
        cron_expression="0 9 * * 1-5", enabled=True,
    )
    assert get_daily_report_schedule_summary() is not None

    # 비활성화 — None 반환 + 잡 제거.
    result = register_daily_report_cron_schedule(
        cron_expression="0 9 * * 1-5", enabled=False,
    )
    assert result is None
    assert get_daily_report_schedule_summary() is None


def test_register_daily_report_with_empty_cron_and_enabled_true_does_not_register(
    running_scheduler: None,
) -> None:
    """``cron_expression=""`` + enabled=True 는 비활성화 분기로 흐른다 (빈 cron 가드).

    잡스토어가 비어 있는 상태에서는 단순히 None 반환 + 잡 미등록. 이미 잡이
    있는 경우의 제거 동작은 `test_register_daily_report_with_enabled_false_...`
    가 가드한다.
    """
    result = register_daily_report_cron_schedule(
        cron_expression="", enabled=True,
    )
    assert result is None
    assert get_daily_report_schedule_summary() is None


def test_remove_daily_report_cron_schedule_is_no_op_when_job_absent(
    running_scheduler: None,
) -> None:
    """``remove_daily_report_cron_schedule`` 는 잡이 없을 때도 에러 없이 no-op."""
    # 사전 조건: 잡 없음.
    assert get_daily_report_schedule_summary() is None

    # 예외가 나지 않아야 한다.
    remove_daily_report_cron_schedule()

    # 여전히 잡 없음.
    assert get_daily_report_schedule_summary() is None


def test_remove_daily_report_cron_schedule_removes_existing_job(
    running_scheduler: None,
) -> None:
    """``remove_daily_report_cron_schedule`` 가 등록된 잡을 제거한다."""
    register_daily_report_cron_schedule(
        cron_expression="0 9 * * 1-5", enabled=True,
    )
    assert get_daily_report_schedule_summary() is not None

    remove_daily_report_cron_schedule()
    assert get_daily_report_schedule_summary() is None


# ──────────────────────────────────────────────────────────────
# Cron 표현식 검증
# ──────────────────────────────────────────────────────────────


def test_register_daily_report_with_invalid_cron_raises_validation_error(
    running_scheduler: None,
) -> None:
    """잘못된 cron 표현식은 ``ScheduleValidationError`` 로 명시 에러.

    enabled=False / cron="" 분기와 다르게 enabled=True 면서 cron 이 파싱 실패하는
    경우 — Admin API 가 422 로 변환할 수 있도록 명시 에러로 던진다.
    """
    with pytest.raises(ScheduleValidationError):
        register_daily_report_cron_schedule(
            cron_expression="not a cron", enabled=True,
        )


# ──────────────────────────────────────────────────────────────
# SystemSetting 기반 로딩 (인자 None)
# ──────────────────────────────────────────────────────────────


def test_register_daily_report_with_none_args_loads_from_system_setting(
    running_scheduler: None,
    db_session: Session,
) -> None:
    """``register_daily_report_cron_schedule()`` (인자 없음) 은 SystemSetting 로드.

    enabled=True / cron="*/15 * * * *" 가 SystemSetting 에 저장돼 있으면 그 값으로
    등록되어야 한다.
    """
    cron_value = "*/15 * * * *"
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_CRON, cron_value)
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "true")

    summary = register_daily_report_cron_schedule()
    assert summary is not None
    assert summary.job_id == JOB_ID_DAILY_REPORT
    assert summary.trigger_spec == cron_value


def test_register_daily_report_with_none_args_respects_disabled_setting(
    running_scheduler: None,
    db_session: Session,
) -> None:
    """SystemSetting enabled=false 면 인자 None 호출 시 잡이 등록되지 않는다."""
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_CRON, "0 9 * * 1-5")
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "false")

    result = register_daily_report_cron_schedule()
    assert result is None
    assert get_daily_report_schedule_summary() is None


# ──────────────────────────────────────────────────────────────
# Case D — ensure_daily_report_cron_registered (startup 복원)
# ──────────────────────────────────────────────────────────────


def test_ensure_daily_report_registers_when_enabled_in_settings(
    running_scheduler: None,
    db_session: Session,
) -> None:
    """``ensure_daily_report_cron_registered`` — SystemSetting enabled=true 이면 등록.

    startup 흐름의 자동 복원 시뮬레이션 — 백업 잡의 ``ensure_backup_cron_registered``
    와 같은 라인에 호출되는 함수다.
    """
    cron_value = "0 8 * * *"
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_CRON, cron_value)
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "true")

    ensure_daily_report_cron_registered()

    summary = get_daily_report_schedule_summary()
    assert summary is not None
    assert summary.trigger_spec == cron_value


def test_ensure_daily_report_skips_when_disabled_in_settings(
    running_scheduler: None,
    db_session: Session,
) -> None:
    """SystemSetting enabled=false 이면 ensure 는 잡을 등록하지 않는다.

    잡스토어에 이전에 떠 있던 잡이 자동 복원돼 있던 경우(다음 테스트가 시뮬레이션)
    조차도 본 함수가 그 잡을 제거해 SystemSetting 토글을 single source of truth
    로 만든다 — 별도 테스트(`..._removes_restored_job`) 로 가드.
    """
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_CRON, "0 9 * * 1-5")
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "false")

    ensure_daily_report_cron_registered()
    assert get_daily_report_schedule_summary() is None


def test_ensure_daily_report_removes_restored_job_when_disabled(
    running_scheduler: None,
    db_session: Session,
) -> None:
    """잡스토어에 자동 복원된 잡이 있어도 enabled=false 면 ensure 가 제거.

    재기동 시나리오를 시뮬레이션: 먼저 enabled=true 로 등록 → SystemSetting 을
    false 로 바꾼 뒤 ensure 호출 → 잡이 제거되어야 한다 (디자인 노트 §8 결정).
    """
    # 1단계 — enabled=true 로 등록.
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_CRON, "0 9 * * 1-5")
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "true")
    ensure_daily_report_cron_registered()
    assert get_daily_report_schedule_summary() is not None

    # 2단계 — SystemSetting 을 false 로 바꾼 뒤 ensure 다시 호출.
    _set_setting_value(db_session, SETTING_KEY_DAILY_REPORT_ENABLED, "false")
    ensure_daily_report_cron_registered()
    assert get_daily_report_schedule_summary() is None


def test_ensure_daily_report_is_no_op_when_scheduler_not_running(
    test_engine: Engine,
) -> None:
    """스케줄러가 미기동 상태이면 ensure 는 silent no-op (예외 없음).

    웹 startup 흐름에서 ``start_scheduler()`` 가 실패한 경우라도 본 호출이
    create_app 을 막지 않아야 한다 (백업의 ensure 와 동일 안전 정책).
    """
    # 의도적으로 running_scheduler 픽스처를 사용하지 않는다. 다만 다른 테스트가
    # 남긴 싱글턴이 있을 수 있으므로 안전을 위해 stop 한 번 호출.
    stop_scheduler(wait=False)

    # 예외가 발생하지 않는 것이 본 테스트의 통과 조건.
    ensure_daily_report_cron_registered()


# ──────────────────────────────────────────────────────────────
# list_general_schedules / get_daily_report_schedule_summary 분리
# ──────────────────────────────────────────────────────────────


def test_list_general_schedules_excludes_daily_report_job(
    running_scheduler: None,
) -> None:
    """``list_general_schedules`` 는 daily report 잡을 결과에서 제외해야 한다.

    [스케줄] 탭은 일반 수집 스케줄만 표시하고, daily report 잡은 [메일 발송 설정]
    의 Daily Report 카드에서 단독으로 노출한다 (디자인 노트 §8).
    """
    register_daily_report_cron_schedule(
        cron_expression="0 9 * * 1-5", enabled=True,
    )
    # 백업 잡과 동일하게 daily report 잡도 일반 목록에서 빠져야 한다.
    general = list_general_schedules()
    assert all(s.job_id != JOB_ID_DAILY_REPORT for s in general)

    # 그러나 단독 조회는 가능해야 한다 (Admin API 가 next_run_at 을 채울 때 사용).
    assert get_daily_report_schedule_summary() is not None
