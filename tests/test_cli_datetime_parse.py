"""``app.cli._parse_datetime_text`` KST 가정 파싱 회귀 테스트 (task 00040-5).

본 테스트는 외부 응답(IRIS / NTIS)의 날짜 텍스트가 \"한국 현지 시각\" 의미라는
사용자 원문 결정을 코드가 정확히 이행하는지 고정한다. audit §5 / §7 에서
확인된 결함(naive 파싱 결과에 그대로 ``tzinfo=UTC`` 부착) 의 회귀 방지가 핵심.

회귀 포인트:
    - ``\"YYYY-MM-DD\"`` 와 ``\"YYYY.MM.DD\"`` 는 KST 자정으로 해석되어 UTC 로는
      전날 15:00 으로 저장된다.
    - ``\"YYYY-MM-DD HH:MM\"`` / ``\"YYYY-MM-DD HH:MM:SS\"`` 도 KST 가정 → UTC 변환.
    - None / 빈 문자열은 None 통과.
    - 잘못된 포맷은 경고 후 None.
    - 반환 datetime 은 항상 UTC tz-aware (offset 0).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.cli import _parse_datetime_text
from app.timezone import KST


def test_parse_dotted_date_assumes_kst_midnight() -> None:
    """``\"2026.05.01\"`` 은 KST 자정 → UTC 2026-04-30 15:00 으로 변환된다."""
    parsed = _parse_datetime_text("2026.05.01")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime(2026, 4, 30, 15, 0, 0, tzinfo=UTC)
    # KST 로 다시 변환하면 사용자가 의도한 KST 자정으로 round-trip.
    assert parsed.astimezone(KST) == datetime(2026, 5, 1, 0, 0, 0, tzinfo=KST)


def test_parse_dashed_date_assumes_kst_midnight() -> None:
    """``\"2026-05-01\"`` (구분자 다른 입력) 도 동일하게 KST 자정으로 해석."""
    parsed = _parse_datetime_text("2026-05-01")
    assert parsed == datetime(2026, 4, 30, 15, 0, 0, tzinfo=UTC)


def test_parse_slashed_date_assumes_kst_midnight() -> None:
    """``\"2026/05/01\"`` 도 정규화 후 동일 결과."""
    parsed = _parse_datetime_text("2026/05/01")
    assert parsed == datetime(2026, 4, 30, 15, 0, 0, tzinfo=UTC)


def test_parse_datetime_with_time_assumes_kst() -> None:
    """``\"YYYY-MM-DD HH:MM\"`` 입력은 KST 그 시각으로 해석되어 UTC 9시간 차감.

    KST 09:30 = UTC 00:30 (당일).
    """
    parsed = _parse_datetime_text("2026-05-01 09:30")
    assert parsed == datetime(2026, 5, 1, 0, 30, 0, tzinfo=UTC)


def test_parse_datetime_with_seconds_assumes_kst() -> None:
    """``\"YYYY-MM-DD HH:MM:SS\"`` 입력도 KST 가정 → UTC 변환.

    KST 23:59:59 = UTC 14:59:59.
    """
    parsed = _parse_datetime_text("2026-05-01 23:59:59")
    assert parsed == datetime(2026, 5, 1, 14, 59, 59, tzinfo=UTC)


def test_parse_none_passes_through() -> None:
    """None / 빈 문자열은 None 그대로 반환."""
    assert _parse_datetime_text(None) is None
    assert _parse_datetime_text("") is None
    assert _parse_datetime_text("   ") is None


def test_parse_invalid_format_returns_none() -> None:
    """포맷 후보 어디에도 매칭되지 않으면 None."""
    assert _parse_datetime_text("not-a-date") is None
    assert _parse_datetime_text("2026") is None


def test_parsed_value_is_utc_tz_aware() -> None:
    """파싱 결과는 항상 UTC tz-aware (utcoffset=0).

    raw_metadata 의 KST 텍스트와 컬럼값을 비교할 때 일관된 비교 기준을 제공한다.
    """
    samples = ["2026.05.01", "2026-05-01 12:00", "2026/05/01 12:00:00"]
    for sample in samples:
        parsed = _parse_datetime_text(sample)
        assert parsed is not None, sample
        assert parsed.tzinfo is not None, sample
        assert parsed.utcoffset() == timedelta(0), sample
