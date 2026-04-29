"""대시보드 비교 기준 (from, to) 산출 모듈 (Phase 5b / task 00042-2).

배경 (사용자 원문):
    대시보드 컨트롤 영역의 비교 대상 드롭다운(전날/전주/전월/전년/직접 선택) 이
    선택한 ``compare_mode`` 와 ``base_date`` (KST date) 를 받아 A 섹션의 ``(from,
    to]`` 누적 머지 구간을 결정한다. 본 모듈은 그 산출 함수만 단독으로 노출해
    유닛 테스트가 1줄 import 로 검증할 수 있게 한다.

설계 근거:
    - ``docs/dashboard_design.md §5.1`` 의 표를 코드로 옮긴 것이다. 표의 각 행이
      :func:`resolve_compare_range` 의 한 분기에 대응한다.
    - ``relativedelta`` (dateutil) 는 본 프로젝트의 ``pyproject.toml`` 의존성에
      없어 표준 라이브러리 ``calendar.monthrange`` 만으로 month/year 산술을
      구현한다. 의도된 시맨틱은 ``relativedelta(months=N)`` / ``years=N`` 와
      동일 — 같은 month/day 로 평행 이동하되 day 가 그 달의 마지막 일을 넘으면
      마지막 일로 clamp 한다 (예: 5월 31일 → 4월 30일, 2024-02-29 → 2023-02-28).

API 표면:
    - :class:`CompareMode` — 허용 5종을 enum 으로 고정. 라우트 검증 시 사용.
    - :data:`COMPARE_MODE_VALUES` — ``CompareMode`` 의 한글/영문 식별자 tuple.
    - :class:`CompareRange` — ``(from_date, to_date)`` 를 명시적으로 묶은 dataclass.
    - :func:`resolve_compare_range` — ``(base_date, compare_mode, compare_date)``
      → ``CompareRange`` 산출.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum


class CompareMode(str, Enum):
    """대시보드 비교 대상 드롭다운의 5종 (사용자 원문 그대로).

    ``str`` 을 mixin 으로 두어 ``CompareMode("prev_day").value == "prev_day"`` 와
    ``CompareMode("prev_day") == "prev_day"`` 가 모두 True — 라우트의 query
    parameter 검증 분기를 단순하게 유지한다.
    """

    # 전날 비교 — from = base_date - 1일.
    PREV_DAY = "prev_day"

    # 전주 비교 — from = base_date - 7일.
    PREV_WEEK = "prev_week"

    # 전월 비교 — from = base_date - 1개월 (clamp: 해당 day 가 전 달에 없으면 마지막 일).
    PREV_MONTH = "prev_month"

    # 전년 비교 — from = base_date - 1년 (clamp: 윤년 2/29 → 평년 2/28).
    PREV_YEAR = "prev_year"

    # 직접 선택 — from = compare_date (라우트에서 필수 입력 검증).
    CUSTOM = "custom"


# 라우트의 query parameter 검증에 쓰는 tuple — set 변환 비용을 매 요청 피한다.
COMPARE_MODE_VALUES: tuple[str, ...] = tuple(member.value for member in CompareMode)


@dataclass(frozen=True)
class CompareRange:
    """비교 기준 (from, to) 산출 결과.

    ``frozen=True`` 로 immutable — 라우트가 dict 로 컨텍스트에 넣은 뒤 템플릿이
    실수로 mutate 하는 회귀를 방지한다.

    Attributes:
        from_date: ``(from, to]`` 구간의 시작 (배타). 본 dataclass 는 산출
                   결과만 담고 비교일 snapshot 가용성 fallback 은 별도 단계
                   (``docs/dashboard_design.md §4.2``) 에서 수행한다.
        to_date:   ``(from, to]`` 구간의 끝 (포함) — 사용자가 캘린더에서 선택한
                   기준일이다.
    """

    from_date: date
    to_date: date


def _subtract_months(target_date: date, months: int) -> date:
    """``target_date`` 에서 정확히 ``months`` 개월을 뺀 날짜를 반환한다.

    표준 라이브러리만으로 ``dateutil.relativedelta(months=months)`` 와 동일한
    시맨틱을 구현한다 — 같은 day 로 평행 이동하되, 결과 month 의 마지막 일을
    넘으면 마지막 일로 clamp 한다.

    예시:
        - ``date(2026, 5, 31)`` − 1개월 → ``date(2026, 4, 30)`` (4월은 30일까지)
        - ``date(2024, 3, 30)`` − 1개월 → ``date(2024, 2, 29)`` (윤년 2월)
        - ``date(2025, 3, 30)`` − 1개월 → ``date(2025, 2, 28)`` (평년 2월)

    Args:
        target_date: 원본 날짜 (KST date).
        months:      뺄 개월 수 (양수만 사용 — 본 모듈에서는 1 만 호출됨).

    Returns:
        평행 이동한 새 ``date`` 객체.
    """
    new_year = target_date.year
    new_month = target_date.month - months
    # 월이 0 이하로 떨어지면 연도를 끌어내리며 12 단위로 정상화한다.
    while new_month <= 0:
        new_month += 12
        new_year -= 1
    while new_month > 12:
        new_month -= 12
        new_year += 1
    last_day_of_new_month = monthrange(new_year, new_month)[1]
    return date(new_year, new_month, min(target_date.day, last_day_of_new_month))


def _subtract_years(target_date: date, years: int) -> date:
    """``target_date`` 에서 정확히 ``years`` 년을 뺀 날짜를 반환한다.

    윤년 clamp 시맨틱은 ``_subtract_months`` 와 동일 — 결과 month/day 가
    유효하지 않으면 (``2024-02-29`` 1년 전) 그 달의 마지막 일로 내림한다.

    예시:
        - ``date(2024, 2, 29)`` − 1년 → ``date(2023, 2, 28)``
        - ``date(2026, 4, 29)`` − 1년 → ``date(2025, 4, 29)``

    Args:
        target_date: 원본 날짜 (KST date).
        years:       뺄 연 수 (양수만 사용).

    Returns:
        평행 이동한 새 ``date`` 객체.
    """
    new_year = target_date.year - years
    new_month = target_date.month
    last_day_of_new_month = monthrange(new_year, new_month)[1]
    return date(new_year, new_month, min(target_date.day, last_day_of_new_month))


def resolve_compare_range(
    *,
    base_date: date,
    compare_mode: CompareMode | str,
    compare_date: date | None,
) -> CompareRange:
    """기준일과 비교 모드로부터 ``(from, to)`` 를 산출한다.

    설계 ``docs/dashboard_design.md §5.1`` 표:

        | compare_mode  | from 산출                              | to 산출   |
        | prev_day      | base_date - 1일                        | base_date |
        | prev_week     | base_date - 7일                        | base_date |
        | prev_month    | base_date - 1개월 (마지막일 clamp)     | base_date |
        | prev_year     | base_date - 1년 (윤년 clamp)           | base_date |
        | custom        | compare_date                           | base_date |

    Args:
        base_date:    사용자가 캘린더에서 선택한 기준일 (KST date). ``to`` 가
                      된다. 호출자(라우트) 가 ``now_kst().date()`` 로 default
                      를 채워 준다.
        compare_mode: ``CompareMode`` enum 또는 그 ``value`` 문자열. 허용 5종
                      외 값을 받으면 :class:`ValueError` 를 raise — 라우트 단계
                      에서 미리 검증해 400 으로 응답하는 것이 표준 흐름이다.
        compare_date: ``compare_mode == CUSTOM`` 일 때만 의미를 가진다. CUSTOM
                      인데 ``None`` 이면 :class:`ValueError`. 그 외 mode 에서는
                      값이 들어와도 무시한다 (라우트 query string 의 ``compare_date``
                      가 stale 하더라도 안전).

    Returns:
        ``CompareRange(from_date, to_date)``. ``to_date`` 는 항상 ``base_date``.

    Raises:
        ValueError: 알 수 없는 ``compare_mode`` 값일 때.
        ValueError: ``compare_mode == CUSTOM`` 인데 ``compare_date`` 가 None 일 때.
    """
    # 문자열 입력도 받아주되 enum 으로 정규화 — 라우트가 query string 으로 받은
    # 값을 그대로 넘기는 흐름을 자연스럽게 한다.
    if isinstance(compare_mode, str):
        try:
            mode_enum = CompareMode(compare_mode)
        except ValueError as exc:
            allowed_list = ", ".join(COMPARE_MODE_VALUES)
            raise ValueError(
                f"알 수 없는 compare_mode: {compare_mode!r}. 허용: {allowed_list}"
            ) from exc
    else:
        mode_enum = compare_mode

    if mode_enum is CompareMode.PREV_DAY:
        return CompareRange(from_date=base_date - timedelta(days=1), to_date=base_date)

    if mode_enum is CompareMode.PREV_WEEK:
        return CompareRange(from_date=base_date - timedelta(days=7), to_date=base_date)

    if mode_enum is CompareMode.PREV_MONTH:
        return CompareRange(
            from_date=_subtract_months(base_date, 1),
            to_date=base_date,
        )

    if mode_enum is CompareMode.PREV_YEAR:
        return CompareRange(
            from_date=_subtract_years(base_date, 1),
            to_date=base_date,
        )

    # CompareMode.CUSTOM — compare_date 필수.
    if compare_date is None:
        raise ValueError(
            "compare_mode='custom' 일 때 compare_date 는 반드시 지정해야 합니다."
        )
    return CompareRange(from_date=compare_date, to_date=base_date)


__all__ = [
    "COMPARE_MODE_VALUES",
    "CompareMode",
    "CompareRange",
    "resolve_compare_range",
]
