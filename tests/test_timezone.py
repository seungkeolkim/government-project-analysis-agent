"""``app.timezone`` KST 헬퍼 단위 테스트 (task 00040-2).

본 테스트는 ``app/timezone.py`` 가 사용자 원문 task 00040 의 API 표면을 정확히
이행하는지 고정한다. 후속 subtask
(Jinja2 필터 / APScheduler / 외부 응답 파싱 / backfill / 검증) 가 본 모듈에
의존하므로, API 표면이 흔들리지 않도록 결정적으로 잡아 둔다.

회귀 포인트:
    - ``KST`` 가 ``ZoneInfo("Asia/Seoul")`` 인지.
    - ``to_kst`` 가 None / naive(UTC 가정) / UTC tz-aware / KST tz-aware /
      그 외 tz-aware 입력 모두에서 KST tz-aware 결과를 일관되게 만드는지.
    - ``now_utc`` 가 UTC tz-aware, ``now_kst`` 가 KST tz-aware 인지.
    - ``format_kst`` 가 None 을 빈 문자열로, 그 외는 KST 변환 후 ``strftime`` 결과
      인지.
    - ``kst_date_boundaries`` 가 KST [00:00, 24:00) 을 UTC tz-aware 한 쌍으로
      반환하고, 표준 시간(2026-04-28 등 DST 미적용) 에서 +9시간 차이를 만드는지.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.timezone import (
    DEFAULT_KST_FORMAT,
    KST,
    format_kst,
    kst_date_boundaries,
    now_kst,
    now_utc,
    to_kst,
)


# ──────────────────────────────────────────────────────────────
# KST 상수
# ──────────────────────────────────────────────────────────────


def test_kst_constant_is_zoneinfo_seoul() -> None:
    """``KST`` 가 ``ZoneInfo(\"Asia/Seoul\")`` 와 동치인지 확인.

    문자열 비교 대신 ZoneInfo 자체로 비교해, 후속 subtask 가 ``KST`` 를 직접
    임포트해 ``tzinfo`` 인자로 넘겨도 정확히 KST 가 되는지 보장한다.
    """
    assert KST == ZoneInfo("Asia/Seoul")


# ──────────────────────────────────────────────────────────────
# to_kst
# ──────────────────────────────────────────────────────────────


def test_to_kst_passes_none_through() -> None:
    """``to_kst(None)`` 은 None 을 그대로 반환한다 (호출 측 None-guard 부담 절감)."""
    assert to_kst(None) is None


def test_to_kst_naive_input_is_assumed_utc() -> None:
    """naive 입력은 UTC 가정으로 부착 후 KST 변환되어야 한다.

    프로젝트 컨벤션 (DB 저장값은 UTC) 에 따라 naive ``datetime`` 은 UTC 로
    가정한다. SQLite SELECT 가 tz 를 떨어뜨려 naive 로 돌려준 컬럼값을 그대로
    표시할 때 이 가정이 성립한다.
    """
    naive_input = datetime(2026, 4, 28, 0, 0, 0)  # 의미상 UTC 자정
    kst_result = to_kst(naive_input)
    assert kst_result is not None
    assert kst_result.tzinfo is not None
    # KST 는 UTC+09:00 이므로 자정 UTC 는 09:00 KST 가 된다.
    assert kst_result == datetime(2026, 4, 28, 9, 0, 0, tzinfo=KST)
    assert kst_result.utcoffset() == timedelta(hours=9)


def test_to_kst_utc_aware_input_converts_to_kst() -> None:
    """UTC tz-aware 입력은 ``astimezone(KST)`` 로 KST tz-aware 로 변환된다."""
    utc_input = datetime(2026, 4, 28, 0, 30, 0, tzinfo=UTC)
    kst_result = to_kst(utc_input)
    assert kst_result is not None
    # 같은 절대 시각인지 (instant identity) 와 표시 tz 가 KST 인지 둘 다 확인.
    assert kst_result == utc_input
    assert kst_result.tzinfo == KST
    assert kst_result.hour == 9
    assert kst_result.minute == 30


def test_to_kst_kst_aware_input_returns_equivalent_kst() -> None:
    """이미 KST tz-aware 인 입력도 동일한 KST 시각으로 정규화된다.

    내부 구현이 ``astimezone(KST)`` 라 결과 instant 가 동일하면 충분하다 —
    객체 동일성(identity)이 아닌 datetime 값 동일성을 검사한다.
    """
    kst_input = datetime(2026, 4, 28, 12, 0, 0, tzinfo=KST)
    kst_result = to_kst(kst_input)
    assert kst_result is not None
    assert kst_result == kst_input
    assert kst_result.tzinfo == KST


def test_to_kst_other_offset_aware_input_converts_to_kst() -> None:
    """KST 도 UTC 도 아닌 임의 tz-aware 입력도 KST 로 정확히 변환된다.

    예) UTC-05:00 의 2026-04-28 00:00 → UTC 2026-04-28 05:00 → KST 2026-04-28 14:00.
    """
    eastern_offset = timezone(timedelta(hours=-5))
    other_input = datetime(2026, 4, 28, 0, 0, 0, tzinfo=eastern_offset)
    kst_result = to_kst(other_input)
    assert kst_result is not None
    assert kst_result == datetime(2026, 4, 28, 14, 0, 0, tzinfo=KST)


# ──────────────────────────────────────────────────────────────
# now_utc / now_kst
# ──────────────────────────────────────────────────────────────


def test_now_utc_is_utc_aware_and_close_to_now() -> None:
    """``now_utc`` 가 UTC tz-aware 이고 호출 시점에 충분히 가까운지 확인."""
    before = datetime.now(tz=UTC)
    captured = now_utc()
    after = datetime.now(tz=UTC)

    assert captured.tzinfo is not None
    assert captured.utcoffset() == timedelta(0)
    assert before <= captured <= after


def test_now_kst_is_kst_aware_and_offset_plus_9() -> None:
    """``now_kst`` 가 KST tz-aware 이고 UTC 와 동일한 절대 시각인지 확인."""
    captured_kst = now_kst()
    captured_utc = datetime.now(tz=UTC)

    assert captured_kst.tzinfo is not None
    assert captured_kst.utcoffset() == timedelta(hours=9)
    # 두 시각의 차이가 1초 이내여야 한다 (호출 간 경과 시간).
    assert abs(captured_kst - captured_utc) < timedelta(seconds=1)


# ──────────────────────────────────────────────────────────────
# format_kst
# ──────────────────────────────────────────────────────────────


def test_format_kst_none_returns_empty_string() -> None:
    """None 입력은 빈 문자열을 반환한다 (가이드 상 fallback 분기 위임)."""
    assert format_kst(None) == ""


def test_format_kst_default_format_is_minute_precision() -> None:
    """기본 포맷은 분 단위 (``%Y-%m-%d %H:%M``) 이며 KST 시각으로 표시된다.

    UTC 자정 입력은 KST 09:00 으로 표시되어야 한다.
    """
    utc_input = datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC)
    assert format_kst(utc_input) == "2026-04-28 09:00"
    # 기본 포맷이 모듈에 노출돼 있는지 동시 확인.
    assert DEFAULT_KST_FORMAT == "%Y-%m-%d %H:%M"


def test_format_kst_custom_format_is_applied_after_kst_conversion() -> None:
    """사용자 지정 포맷은 KST 변환 결과에 ``strftime`` 으로 적용된다."""
    utc_input = datetime(2026, 4, 28, 15, 30, 45, tzinfo=UTC)
    # UTC 15:30:45 → KST 다음날 00:30:45.
    assert format_kst(utc_input, "%Y-%m-%d %H:%M:%S") == "2026-04-29 00:30:45"
    # 일자만 (Jinja2 `kst_date` 필터 매핑 후보).
    assert format_kst(utc_input, "%Y-%m-%d") == "2026-04-29"


def test_format_kst_naive_input_assumed_utc() -> None:
    """naive 입력은 UTC 가정 후 KST 로 변환되어 포맷된다."""
    naive_input = datetime(2026, 4, 28, 0, 0, 0)
    assert format_kst(naive_input) == "2026-04-28 09:00"


# ──────────────────────────────────────────────────────────────
# kst_date_boundaries
# ──────────────────────────────────────────────────────────────


def test_kst_date_boundaries_returns_utc_tz_aware_pair() -> None:
    """반환된 한 쌍이 모두 UTC tz-aware 인지 확인."""
    target = date(2026, 4, 28)
    start_utc, end_utc = kst_date_boundaries(target)

    assert start_utc.tzinfo is not None
    assert end_utc.tzinfo is not None
    assert start_utc.utcoffset() == timedelta(0)
    assert end_utc.utcoffset() == timedelta(0)


def test_kst_date_boundaries_aligns_to_kst_midnight() -> None:
    """경계가 KST 자정과 다음날 KST 자정에 정확히 일치하는지 확인.

    2026-04-28 KST 자정 = 2026-04-27 15:00 UTC.
    2026-04-29 KST 자정 = 2026-04-28 15:00 UTC.
    한국은 DST 가 없어 전날/당일/다음날 모두 +09:00 고정이다.
    """
    target = date(2026, 4, 28)
    start_utc, end_utc = kst_date_boundaries(target)

    assert start_utc == datetime(2026, 4, 27, 15, 0, 0, tzinfo=UTC)
    assert end_utc == datetime(2026, 4, 28, 15, 0, 0, tzinfo=UTC)
    # 정확히 24시간 구간이어야 한다.
    assert end_utc - start_utc == timedelta(days=1)


def test_kst_date_boundaries_round_trips_through_kst() -> None:
    """경계를 KST 로 다시 변환하면 KST 자정 두 개가 나오는지 확인.

    Phase 5 GROUP BY 가 ``WHERE col >= start_utc AND col < end_utc`` 형태로
    잘라낸다. KST 표시 tz 로 되돌렸을 때 ``00:00:00`` 이 정확히 나와야 사용자
    의도(\"하루 단위로 묶기\")가 보장된다.
    """
    target = date(2026, 4, 28)
    start_utc, end_utc = kst_date_boundaries(target)

    start_kst = start_utc.astimezone(KST)
    end_kst = end_utc.astimezone(KST)

    assert start_kst == datetime(2026, 4, 28, 0, 0, 0, tzinfo=KST)
    assert end_kst == datetime(2026, 4, 29, 0, 0, 0, tzinfo=KST)


def test_kst_date_boundaries_consecutive_days_are_contiguous() -> None:
    """인접한 두 날짜의 경계가 정확히 맞물리는지 (gap 없음)."""
    today_start, today_end = kst_date_boundaries(date(2026, 4, 28))
    tomorrow_start, _tomorrow_end = kst_date_boundaries(date(2026, 4, 29))

    # 오늘의 종료 = 내일의 시작 (반-open 구간이 빈틈없이 이어진다).
    assert today_end == tomorrow_start


# ──────────────────────────────────────────────────────────────
# as_utc + to_kst 결합 (task 00040-6 검증 #7)
# ──────────────────────────────────────────────────────────────


def test_as_utc_then_to_kst_recovers_kst_midnight() -> None:
    """SQLite SELECT 시뮬레이션 — naive UTC datetime 을 as_utc 로 정규화한 뒤
    to_kst 로 표시 변환했을 때 사용자가 의도한 KST 시각으로 round-trip 되는지.

    배경 (사용자 원문 검증 #7 'SQLite + _as_utc + to_kst 결합 정상'):
        SQLite ``DateTime(timezone=True)`` 컬럼은 SELECT 시 tz 정보를 떨어뜨리는
        알려진 동작이 있다 (audit §6). DB 에 ``2026-04-30 15:00:00 UTC`` (= KST
        2026-05-01 00:00) 가 저장됐다고 하자. SQLite SELECT 후 ORM 인스턴스의
        ``deadline_at`` 은 ``datetime(2026, 4, 30, 15, 0, 0)`` (naive). 비교 직전
        ``app.db.models.as_utc`` 로 UTC tz-aware 로 정규화하고, 표시 직전
        ``to_kst`` 로 KST tz-aware 로 변환하면 사용자가 의도한 KST 자정으로
        정확히 돌아와야 한다. 두 헬퍼는 서로 다른 레이어 (SELECT 보정 vs KST
        변환) 이며 직렬 결합으로 사용된다는 사용자 원문 명시를 코드로 고정한다.
    """
    # 본 테스트는 app.timezone 의 to_kst 만 검증해도 동치다 (to_kst 가 naive
    # 입력을 UTC 가정으로 흡수). 그러나 결합 패턴을 명시적으로 보여주기 위해
    # as_utc 도 함께 호출한다.
    from app.db.models import as_utc
    from app.timezone import to_kst

    # SQLite 가 tz 를 떨어뜨려 돌려준 \"2026-04-30 15:00 UTC\" 의 naive 표현.
    sqlite_naive = datetime(2026, 4, 30, 15, 0, 0)

    # 비교 경로 — UTC tz-aware 로 정규화. 다른 UTC tz-aware 값과 == 비교 가능.
    normalized_utc = as_utc(sqlite_naive)
    assert normalized_utc == datetime(2026, 4, 30, 15, 0, 0, tzinfo=UTC)

    # 표시 경로 — KST tz-aware 로 변환. 사용자 의도 시각(KST 자정) 으로 round-trip.
    displayed_kst = to_kst(sqlite_naive)
    assert displayed_kst == datetime(2026, 5, 1, 0, 0, 0, tzinfo=KST)

    # 두 결과는 같은 절대 시각이지만 표시 tz 만 다르다.
    assert normalized_utc == displayed_kst


def test_to_kst_alone_handles_sqlite_naive_input() -> None:
    """to_kst 단독 호출도 SQLite naive 입력을 그대로 처리한다 (편의 함수 검증).

    표시 경로에서는 as_utc 를 거치지 않고 to_kst 만 호출해도 정상이라는 점을
    회귀 고정한다 (Jinja2 필터의 단순 호출 패턴 ``{{ dt | kst_format }}`` 이
    이 경로다).
    """
    from app.timezone import to_kst

    sqlite_naive = datetime(2026, 4, 30, 15, 0, 0)
    assert to_kst(sqlite_naive) == datetime(2026, 5, 1, 0, 0, 0, tzinfo=KST)
