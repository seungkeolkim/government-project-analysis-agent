"""``app.web.dashboard_compare`` 단위 테스트 (Phase 5b / task 00042-2).

테스트 표면은 ``resolve_compare_range`` 함수 하나로 좁다 — 5종 분기 + 두 가지
clamp 시나리오 + 두 가지 실패 케이스만 검증한다 (외부 의존 없음, DB 미사용).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.web.dashboard_compare import (
    COMPARE_MODE_VALUES,
    CompareMode,
    CompareRange,
    resolve_compare_range,
)


class TestResolveCompareRange:
    """``resolve_compare_range`` 의 5종 분기 + clamp 회귀."""

    def test_prev_day_subtracts_one_day(self) -> None:
        """전날 모드: from = base_date - 1일."""
        base = date(2026, 4, 29)
        result = resolve_compare_range(
            base_date=base, compare_mode=CompareMode.PREV_DAY, compare_date=None
        )
        assert result == CompareRange(from_date=date(2026, 4, 28), to_date=base)

    def test_prev_week_subtracts_seven_days(self) -> None:
        """전주 모드: from = base_date - 7일."""
        base = date(2026, 4, 29)
        result = resolve_compare_range(
            base_date=base, compare_mode=CompareMode.PREV_WEEK, compare_date=None
        )
        assert result == CompareRange(from_date=date(2026, 4, 22), to_date=base)

    def test_prev_month_subtracts_one_month(self) -> None:
        """전월 모드: from = base_date - 1개월 (같은 day 평행 이동)."""
        base = date(2026, 4, 29)
        result = resolve_compare_range(
            base_date=base, compare_mode=CompareMode.PREV_MONTH, compare_date=None
        )
        assert result == CompareRange(from_date=date(2026, 3, 29), to_date=base)

    def test_prev_month_clamps_to_last_day_of_target_month(self) -> None:
        """전월 모드 clamp: 5월 31일 → 4월 30일 (4월은 30일까지)."""
        base = date(2026, 5, 31)
        result = resolve_compare_range(
            base_date=base, compare_mode=CompareMode.PREV_MONTH, compare_date=None
        )
        assert result == CompareRange(from_date=date(2026, 4, 30), to_date=base)

    def test_prev_year_subtracts_one_year(self) -> None:
        """전년 모드: from = base_date - 1년 (같은 month/day)."""
        base = date(2026, 4, 29)
        result = resolve_compare_range(
            base_date=base, compare_mode=CompareMode.PREV_YEAR, compare_date=None
        )
        assert result == CompareRange(from_date=date(2025, 4, 29), to_date=base)

    def test_prev_year_clamps_leap_day(self) -> None:
        """전년 모드 clamp: 2024-02-29 → 2023-02-28 (평년 2월 28일까지)."""
        base = date(2024, 2, 29)
        result = resolve_compare_range(
            base_date=base, compare_mode=CompareMode.PREV_YEAR, compare_date=None
        )
        assert result == CompareRange(from_date=date(2023, 2, 28), to_date=base)

    def test_custom_uses_compare_date(self) -> None:
        """직접 선택 모드: from = compare_date, to = base_date."""
        base = date(2026, 4, 29)
        compare = date(2026, 1, 15)
        result = resolve_compare_range(
            base_date=base, compare_mode=CompareMode.CUSTOM, compare_date=compare
        )
        assert result == CompareRange(from_date=compare, to_date=base)

    def test_custom_without_compare_date_raises(self) -> None:
        """CUSTOM 인데 compare_date None 이면 ValueError."""
        with pytest.raises(ValueError, match="compare_date 는 반드시"):
            resolve_compare_range(
                base_date=date(2026, 4, 29),
                compare_mode=CompareMode.CUSTOM,
                compare_date=None,
            )

    def test_unknown_string_mode_raises(self) -> None:
        """문자열로 받은 알 수 없는 mode 는 ValueError."""
        with pytest.raises(ValueError, match="알 수 없는 compare_mode"):
            resolve_compare_range(
                base_date=date(2026, 4, 29),
                compare_mode="nonsense",
                compare_date=None,
            )

    def test_string_mode_is_normalized(self) -> None:
        """문자열 'prev_day' 도 enum 과 동일하게 동작."""
        base = date(2026, 4, 29)
        from_enum = resolve_compare_range(
            base_date=base, compare_mode=CompareMode.PREV_DAY, compare_date=None
        )
        from_string = resolve_compare_range(
            base_date=base, compare_mode="prev_day", compare_date=None
        )
        assert from_enum == from_string

    def test_compare_range_is_immutable(self) -> None:
        """``CompareRange`` 는 frozen dataclass — 필드 대입 시 FrozenInstanceError."""
        result = resolve_compare_range(
            base_date=date(2026, 4, 29),
            compare_mode=CompareMode.PREV_DAY,
            compare_date=None,
        )
        with pytest.raises(Exception):  # FrozenInstanceError 는 dataclasses 모듈 의존.
            result.from_date = date(2025, 1, 1)  # type: ignore[misc]


class TestCompareModeValues:
    """``COMPARE_MODE_VALUES`` 상수 회귀."""

    def test_contains_exactly_five_modes(self) -> None:
        """사용자 원문 5종이 정확히 들어 있다."""
        assert set(COMPARE_MODE_VALUES) == {
            "prev_day",
            "prev_week",
            "prev_month",
            "prev_year",
            "custom",
        }

    def test_matches_enum(self) -> None:
        """``CompareMode`` enum 의 value 와 1:1 일치."""
        assert set(COMPARE_MODE_VALUES) == {member.value for member in CompareMode}
