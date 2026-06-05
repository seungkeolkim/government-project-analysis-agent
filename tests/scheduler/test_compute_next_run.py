"""``compute_next_run`` cron→다음 실행 시각 계산 단위 테스트 (task 00160-1).

배경:
    task 00155 에서 웹 프로세스 내부 APScheduler 를 걷어내고 OS cron 으로
    전환하면서 라이브 ``next_run_time`` 을 잃어, 메일 발송 관리 화면의 '다음 실행
    예측'이 cron 이 활성·유효해도 항상 '— (비활성 또는 미등록)' 으로 표시되던
    버그가 있었다. ``compute_next_run`` 은 croniter 등 외부 의존성 없이 표준
    5-필드 cron 을 KST(OS cron 의 ``CRON_TZ``) 벽시계 기준으로 해석해 다음 실행
    시각을 자체 계산한다.

핵심 회귀 가드:
    - 매일 특정 시각 / 특정 요일 / 분 단위 / 매 N분 / 매월 특정일 등 다양한 cron
      패턴에 대해 다음 실행 시각이 KST 벽시계 기준으로 정확하다.
    - 반환값은 UTC tz-aware 이며, KST 시각을 UTC 로 환산한 값이다.
    - '지금이 정확히 매칭 분' 이면 그 다음 매칭(직후 시맨틱)을 반환한다.
    - 무효 표현식·존재할 수 없는 날짜 조합은 None 으로 흡수한다.
    - '일'·'요일' 이 모두 제약이면 OR 시맨틱(표준 Vixie cron)을 따른다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.scheduler.crontab_generator import compute_next_run

KST = ZoneInfo("Asia/Seoul")


def _kst(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """KST 벽시계 시각을 tz-aware datetime 으로 만든다(테스트 가독성용)."""
    return datetime(year, month, day, hour, minute, tzinfo=KST)


def _assert_kst_equal(actual: datetime | None, expected_kst: datetime) -> None:
    """``compute_next_run`` 결과(UTC tz-aware)가 기대 KST 시각과 같은 순간인지 검증."""
    assert actual is not None
    # 반환값은 UTC tz-aware 여야 한다(프로젝트 컨벤션).
    assert actual.tzinfo is not None
    assert actual.utcoffset() == UTC.utcoffset(None)
    # 같은 절대 시각인지 비교(타임존 표현만 다름).
    assert actual == expected_kst


def test_daily_fixed_time_before_today_run() -> None:
    """매일 08:00 cron — 오늘 08:00 이전이면 오늘 08:00(KST)을 반환한다."""
    after = _kst(2026, 6, 5, 7, 59)
    result = compute_next_run("0 8 * * *", after)
    _assert_kst_equal(result, _kst(2026, 6, 5, 8, 0))


def test_daily_fixed_time_after_today_run() -> None:
    """매일 08:00 cron — 오늘 08:00 이 지났으면 내일 08:00(KST)을 반환한다."""
    after = _kst(2026, 6, 5, 8, 1)
    result = compute_next_run("0 8 * * *", after)
    _assert_kst_equal(result, _kst(2026, 6, 6, 8, 0))


def test_exactly_on_matching_minute_returns_next_occurrence() -> None:
    """지금이 정확히 매칭 분(08:00:00)이면 직후 시맨틱상 내일 08:00 을 반환한다."""
    after = _kst(2026, 6, 5, 8, 0)
    result = compute_next_run("0 8 * * *", after)
    _assert_kst_equal(result, _kst(2026, 6, 6, 8, 0))


def test_returns_utc_tz_aware_in_correct_offset() -> None:
    """KST 08:00 은 UTC 로 전날(또는 당일) -9h 인 23:00/UTC 로 환산되어 반환된다."""
    after = _kst(2026, 6, 5, 7, 0)
    result = compute_next_run("0 8 * * *", after)
    assert result is not None
    # 2026-06-05 08:00 KST == 2026-06-04 23:00 UTC.
    assert result == datetime(2026, 6, 4, 23, 0, tzinfo=UTC)


def test_weekday_range_skips_weekend() -> None:
    """평일(월~금) 09:00 cron — 토요일에 호출하면 다음 월요일 09:00 을 반환한다."""
    # 2026-06-06 은 토요일. 다음 평일 발화는 2026-06-08(월) 09:00.
    after = _kst(2026, 6, 6, 10, 0)
    result = compute_next_run("0 9 * * 1-5", after)
    _assert_kst_equal(result, _kst(2026, 6, 8, 9, 0))


def test_weekday_alias_is_supported() -> None:
    """요일 별칭(MON)도 숫자와 동일하게 해석된다."""
    # 2026-06-06(토) 이후 첫 월요일은 2026-06-08.
    after = _kst(2026, 6, 6, 0, 0)
    result = compute_next_run("30 7 * * MON", after)
    _assert_kst_equal(result, _kst(2026, 6, 8, 7, 30))


def test_every_n_minutes_step() -> None:
    """매 15분(*/15) cron — 다음 15분 경계를 반환한다."""
    after = _kst(2026, 6, 5, 10, 7)
    result = compute_next_run("*/15 * * * *", after)
    _assert_kst_equal(result, _kst(2026, 6, 5, 10, 15))


def test_every_minute_returns_next_minute() -> None:
    """매분(* * * * *) cron — 항상 다음 분을 반환한다."""
    after = _kst(2026, 6, 5, 10, 30)
    result = compute_next_run("* * * * *", after)
    _assert_kst_equal(result, _kst(2026, 6, 5, 10, 31))


def test_minute_list_picks_nearest() -> None:
    """분 리스트(0,30) cron — 현재 분 이후 가장 가까운 매칭 분을 고른다."""
    after = _kst(2026, 6, 5, 10, 5)
    result = compute_next_run("0,30 * * * *", after)
    _assert_kst_equal(result, _kst(2026, 6, 5, 10, 30))


def test_specific_day_of_month_next_month() -> None:
    """매월 1일 00:00 cron — 1일이 지났으면 다음 달 1일을 반환한다."""
    after = _kst(2026, 6, 5, 0, 0)
    result = compute_next_run("0 0 1 * *", after)
    _assert_kst_equal(result, _kst(2026, 7, 1, 0, 0))


def test_specific_month_and_day_next_year() -> None:
    """매년 1월 1일 cron — 그 해 1월 1일이 지났으면 다음 해를 반환한다."""
    after = _kst(2026, 6, 5, 0, 0)
    result = compute_next_run("0 0 1 1 *", after)
    _assert_kst_equal(result, _kst(2027, 1, 1, 0, 0))


def test_dom_and_dow_both_restricted_uses_or_semantics() -> None:
    """일·요일이 모두 제약이면 표준 cron 의 OR 시맨틱을 따른다.

    ``0 0 13 * 5`` = 매월 13일 **또는** 매주 금요일 00:00. 2026-06-05(금) 직후
    첫 발화는 다음 주 금요일이 아니라 더 이른 13일 또는 금요일 중 빠른 쪽이다.
    2026-06-05 은 금요일 자정(00:00)이 이미 지난 상태라, 다음 발화는 6/12(금).
    """
    after = _kst(2026, 6, 5, 1, 0)
    result = compute_next_run("0 0 13 * 5", after)
    # 6/12(금) 00:00 이 6/13(토) 00:00 보다 빠르므로 6/12 가 선택된다.
    _assert_kst_equal(result, _kst(2026, 6, 12, 0, 0))


def test_dom_match_earlier_than_dow_with_or_semantics() -> None:
    """OR 시맨틱에서 '일' 매칭이 '요일' 매칭보다 빠르면 '일'이 선택된다."""
    # 0 0 7 * 1 = 매월 7일 또는 매주 월요일. 2026-06-05(금) 이후 가장 빠른 발화는
    # 6/7(일) — 7일 매칭이 다음 월요일(6/8)보다 빠르다.
    after = _kst(2026, 6, 5, 12, 0)
    result = compute_next_run("0 0 7 * 1", after)
    _assert_kst_equal(result, _kst(2026, 6, 7, 0, 0))


def test_sunday_zero_and_seven_equivalent() -> None:
    """요일 7 은 표준 crontab 규약상 일요일(0)과 동일하게 해석된다."""
    after = _kst(2026, 6, 5, 0, 0)  # 금요일
    result_zero = compute_next_run("0 6 * * 0", after)
    result_seven = compute_next_run("0 6 * * 7", after)
    # 둘 다 다음 일요일 06:00 = 2026-06-07.
    _assert_kst_equal(result_zero, _kst(2026, 6, 7, 6, 0))
    assert result_zero == result_seven


def test_naive_after_is_treated_as_utc() -> None:
    """naive 입력은 UTC 로 간주되어 KST 로 환산 후 계산된다."""
    # 2026-06-05 00:00 UTC == 2026-06-05 09:00 KST. cron 0 10 * * * 는 같은 날
    # 10:00 KST 가 아직 안 지났으므로 당일 10:00 KST 를 반환한다.
    after_naive = datetime(2026, 6, 5, 0, 0)
    result = compute_next_run("0 10 * * *", after_naive)
    _assert_kst_equal(result, _kst(2026, 6, 5, 10, 0))


def test_invalid_expression_returns_none() -> None:
    """필드 개수가 5가 아닌 무효 표현식은 None 으로 흡수한다."""
    assert compute_next_run("bogus", _kst(2026, 6, 5, 0, 0)) is None
    assert compute_next_run("0 8 * *", _kst(2026, 6, 5, 0, 0)) is None


def test_empty_expression_returns_none() -> None:
    """빈 표현식은 None 으로 흡수한다."""
    assert compute_next_run("", _kst(2026, 6, 5, 0, 0)) is None
    assert compute_next_run("   ", _kst(2026, 6, 5, 0, 0)) is None


def test_impossible_date_combination_returns_none() -> None:
    """존재할 수 없는 날짜 조합(2월 30일)은 탐색 상한 내 매칭이 없어 None."""
    # 30 일은 1~31 범위라 검증은 통과하지만, 2월(30일 없음)+요일 제약 없음이라
    # 실제 매칭이 영원히 없다.
    after = _kst(2026, 6, 5, 0, 0)
    assert compute_next_run("0 0 30 2 *", after) is None
