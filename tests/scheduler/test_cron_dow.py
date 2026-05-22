"""cron 요일 필드 보정 헬퍼 회귀 테스트 (task 00147).

배경:
    APScheduler 3.x 의 ``CronTrigger.from_crontab`` 은 요일 필드의 숫자를 표준
    crontab 규약(0=일 … 6=토)에서 APScheduler 규약(0=월 … 6=일)으로 변환하지
    않고 그대로 넘긴다. 그 결과 ``40 7 * * 1-5``(사용자 의도: 월~금)가 화~토로
    어긋나, 다음 실행 예측은 물론 실제 발화 시각까지 토요일에 잡히고 월요일이
    누락됐다.

검증 매트릭스 (subtask 00147-1 guidance):
    (a) ``40 7 * * 1-5`` 가 금요일 이후 다음 발화로 월요일을 반환하고,
        토요일/일요일에는 발화하지 않는다.
    (b) 헬퍼 단위 변환: ``1-5`` → 월~금 의미, ``0`` 과 ``7`` 모두 일요일,
        ``1,3,5`` 리스트, ``*/2`` 스텝, ``1-5/2`` 범위+스텝, ``mon-fri`` 명시
        입력 통과.
    (c) 백업·Daily Report·공고 수집 등록 함수가 새 헬퍼를 거쳐 평일만 잡히게
        등록된다.
    (d) 잘못된 표현식 예외 동작이 기존 from_crontab 과 동일하게 유지된다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import Engine

from app.scheduler import (
    ScheduleValidationError,
    add_cron_schedule,
    register_backup_cron_schedule,
    register_daily_report_cron_schedule,
    start_scheduler,
    stop_scheduler,
)
from app.scheduler.cron import build_cron_trigger, normalize_crontab_expression
from app.timezone import KST

# 요일 인덱스(파이썬 datetime.weekday(): 월=0 … 일=6).
_MONDAY = 0
_SATURDAY = 5
_SUNDAY = 6
_WEEKDAYS = {0, 1, 2, 3, 4}


# ──────────────────────────────────────────────────────────────
# (b) 헬퍼 단위 변환 테스트 — 스케줄러 불필요, 순수 함수 검증
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "crontab_expression, expected_normalized",
    [
        # 범위 1-5(월~금) → APScheduler 0-4.
        ("* * * * 1-5", "* * * * 0-4"),
        # crontab 0(일) → APScheduler 6.
        ("* * * * 0", "* * * * 6"),
        # crontab 7(일) → APScheduler 6 — 0 과 7 모두 일요일.
        ("* * * * 7", "* * * * 6"),
        # 리스트 1,3,5(월·수·금) → 0,2,4.
        ("* * * * 1,3,5", "* * * * 0,2,4"),
        # 와일드카드+스텝은 요일 규약과 무관 → 그대로.
        ("* * * * */2", "* * * * */2"),
        # 범위+스텝 — 양 끝만 변환하고 스텝은 보존.
        ("* * * * 1-5/2", "* * * * 0-4/2"),
        # 이미 알파벳 요일명으로 쓴 입력은 변환하지 않고 통과.
        ("* * * * mon-fri", "* * * * mon-fri"),
        ("40 7 * * MON-FRI", "40 7 * * MON-FRI"),
        # 분/시/일/월 필드는 손대지 않는다 (요일만 변환).
        ("40 7 1-5 * 1-5", "40 7 1-5 * 0-4"),
        # 매일 수집(*) 은 변환 후에도 그대로.
        ("0 3 * * *", "0 3 * * *"),
    ],
)
def test_normalize_crontab_expression_converts_only_day_of_week(
    crontab_expression: str, expected_normalized: str
) -> None:
    """요일 필드 숫자만 APScheduler 규약으로 보정되고 나머지 필드는 보존된다."""
    assert normalize_crontab_expression(crontab_expression) == expected_normalized


def test_normalize_keeps_invalid_field_count_untouched() -> None:
    """필드 개수가 5개가 아니면 변환 없이 원본을 반환한다 (from_crontab 위임)."""
    assert normalize_crontab_expression("1 2 3") == "1 2 3"


@pytest.mark.parametrize("invalid_day_of_week", ["8", "1-9", "9/2"])
def test_build_cron_trigger_rejects_out_of_range_day(invalid_day_of_week: str) -> None:
    """요일 숫자가 0~7 범위를 벗어나면 ValueError 를 던진다 (from_crontab 동일)."""
    with pytest.raises(ValueError):
        build_cron_trigger(f"0 0 * * {invalid_day_of_week}", timezone=KST)


def test_build_cron_trigger_rejects_wrong_field_count() -> None:
    """필드 개수 오류는 from_crontab 의 기존 검증으로 위임돼 ValueError 가 난다."""
    with pytest.raises(ValueError):
        build_cron_trigger("0 0 * *", timezone=KST)


def test_day_of_week_range_fires_only_on_weekdays() -> None:
    """``* * * * 1-5`` 가 월~금에만 발화하고 토/일에는 발화하지 않는다."""
    trigger = build_cron_trigger("0 12 * * 1-5", timezone=KST)
    # 토요일(2026-05-23) 자정부터 10회 발화를 모아 요일을 검증한다.
    cursor = datetime(2026, 5, 23, 0, 0, tzinfo=KST)
    for _ in range(10):
        fire_time = trigger.get_next_fire_time(None, cursor)
        assert fire_time is not None
        assert fire_time.weekday() in _WEEKDAYS, (
            f"평일 외 발화 감지: {fire_time}"
        )
        # 다음 발화 탐색을 위해 커서를 1초 뒤로 민다.
        cursor = fire_time.replace(second=1)


@pytest.mark.parametrize("sunday_token", ["0", "7"])
def test_zero_and_seven_both_mean_sunday(sunday_token: str) -> None:
    """crontab 의 0 과 7 은 둘 다 일요일로 해석된다."""
    trigger = build_cron_trigger(f"0 0 * * {sunday_token}", timezone=KST)
    fire_time = trigger.get_next_fire_time(
        None, datetime(2026, 5, 22, 0, 0, tzinfo=KST)
    )
    assert fire_time is not None
    assert fire_time.weekday() == _SUNDAY


# ──────────────────────────────────────────────────────────────
# (a) 다음 실행 예측 — 금요일 이후 다음 발화는 월요일
# ──────────────────────────────────────────────────────────────


def test_weekday_cron_next_fire_after_friday_is_monday() -> None:
    """``40 7 * * 1-5`` 가 2026-05-22(금) 12:00 기준 2026-05-25(월) 07:40 을 예측한다.

    버그 재현 대비: 수정 전에는 같은 입력이 토요일(2026-05-23)을 반환했다.
    """
    trigger = build_cron_trigger("40 7 * * 1-5", timezone=KST)
    now_friday_noon = datetime(2026, 5, 22, 12, 0, tzinfo=KST)
    next_fire = trigger.get_next_fire_time(None, now_friday_noon)
    assert next_fire is not None
    # 정확한 다음 발화: 2026-05-25(월) 07:40 KST.
    assert next_fire.year == 2026
    assert next_fire.month == 5
    assert next_fire.day == 25
    assert next_fire.hour == 7
    assert next_fire.minute == 40
    assert next_fire.weekday() == _MONDAY


def test_weekday_cron_does_not_fire_on_weekend() -> None:
    """``40 7 * * 1-5`` 는 토요일·일요일에 발화하지 않는다."""
    trigger = build_cron_trigger("40 7 * * 1-5", timezone=KST)
    cursor = datetime(2026, 5, 22, 0, 0, tzinfo=KST)
    for _ in range(15):
        fire_time = trigger.get_next_fire_time(None, cursor)
        assert fire_time is not None
        assert fire_time.weekday() not in (_SATURDAY, _SUNDAY)
        cursor = fire_time.replace(second=1)


# ──────────────────────────────────────────────────────────────
# (c) 등록 함수 회귀 — backup / daily report / 공고 수집
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def running_scheduler(test_engine: Engine) -> Iterator[None]:
    """테스트별로 격리된 BackgroundScheduler 를 기동·종료한다.

    service 의 ``stop`` 이 모듈 싱글턴을 None 으로 리셋하므로, 다음 테스트의
    ``start_scheduler()`` 가 새 BackgroundScheduler + 새 jobstore 를 만든다.
    """
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler(wait=False)


def test_add_cron_schedule_registers_weekday_only(running_scheduler: None) -> None:
    """공고 수집 cron 등록 시 ``40 7 * * 1-5`` 다음 실행이 평일로 잡힌다."""
    summary = add_cron_schedule(
        cron_expression="40 7 * * 1-5", active_sources=[], enabled=True
    )
    assert summary.next_run_time is not None
    assert summary.next_run_time.weekday() in _WEEKDAYS


def test_register_backup_cron_schedule_weekday_only(running_scheduler: None) -> None:
    """시스템 백업 cron 등록 시 ``40 7 * * 1-5`` 다음 실행이 평일로 잡힌다."""
    summary = register_backup_cron_schedule(cron_expression="40 7 * * 1-5")
    assert summary.next_run_time is not None
    assert summary.next_run_time.weekday() in _WEEKDAYS


def test_register_daily_report_cron_schedule_weekday_only(
    running_scheduler: None,
) -> None:
    """Daily Report cron 등록 시 ``40 7 * * 1-5`` 다음 실행이 평일로 잡힌다."""
    summary = register_daily_report_cron_schedule(
        cron_expression="40 7 * * 1-5", enabled=True
    )
    assert summary is not None
    assert summary.next_run_time is not None
    assert summary.next_run_time.weekday() in _WEEKDAYS


# ──────────────────────────────────────────────────────────────
# (d) 잘못된 표현식 예외 동작 — ScheduleValidationError 변환 유지
# ──────────────────────────────────────────────────────────────


def test_invalid_cron_still_raises_schedule_validation_error(
    running_scheduler: None,
) -> None:
    """잘못된 cron 표현식은 등록 경로에서 ScheduleValidationError 로 변환된다."""
    with pytest.raises(ScheduleValidationError):
        register_backup_cron_schedule(cron_expression="이건 cron 이 아님")
