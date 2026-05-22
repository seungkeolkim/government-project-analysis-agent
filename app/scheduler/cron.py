"""표준 crontab 표현식을 APScheduler 요일 규약에 맞춰 보정하는 cron 파싱 헬퍼.

배경 (task 00147)
-----------------
APScheduler 3.x 의 ``CronTrigger.from_crontab(expr, timezone=...)`` 는 expr 을
공백으로 split 한 뒤 5번째 필드(요일)를 **아무 변환 없이** 그대로
``CronTrigger(day_of_week=...)`` 에 넘긴다.

그런데 표준 crontab 의 요일 숫자와 APScheduler 의 요일 숫자는 규약이 다르다::

    표준 crontab : 0=일, 1=월, 2=화, 3=수, 4=목, 5=금, 6=토, 7=일
    APScheduler  : 0=월, 1=화, 2=수, 3=목, 4=금, 5=토, 6=일

숫자가 1칸씩 어긋나므로 사용자가 ``40 7 * * 1-5`` (의도: 월~금) 를 입력하면
APScheduler 는 이를 화~토로 해석한다. 그 결과 ``next_run_time`` (다음 실행 예측)
뿐 아니라 실제 트리거 발화 시각까지 틀어져, 월요일이 누락되고 토요일에 잡이
실행된다.

해결
----
cron 표현식의 **5번째(요일) 필드의 숫자만** APScheduler 규약으로 1:1 보정한 뒤
``CronTrigger.from_crontab`` 에 넘긴다. 분/시/일/월 필드는 두 규약이 동일하므로
손대지 않는다.

요일 숫자 보정 공식은 ``(crontab_number - 1) % 7`` 하나로 0~7 을 모두 처리한다::

    crontab 0(일) → (0-1)%7 = 6 → APScheduler 6(일)
    crontab 1(월) → (1-1)%7 = 0 → APScheduler 0(월)
    crontab 6(토) → (6-1)%7 = 5 → APScheduler 5(토)
    crontab 7(일) → (7-1)%7 = 6 → APScheduler 6(일)

요일명(``mon``, ``mon-fri`` 등)으로 출력하지 않고 **숫자**로 출력하는 이유:
APScheduler 의 ``WeekdayRangeExpression`` 정규식은 스텝 접미사 ``/n`` 을 받지
않아 ``mon-fri/2`` 같은 범위+스텝 표현이 조용히 깨진다. 반면 숫자
``RangeExpression`` 은 ``0-4/2`` 처럼 스텝을 정상 처리하므로, 범위+스텝까지
안전하게 지원하려면 숫자 출력이 옳다.
"""

from __future__ import annotations

from typing import Any

# crontab 요일 숫자(0~7) 가 허용되는 범위. 0/7 은 둘 다 일요일.
_CRONTAB_DAY_OF_WEEK_MIN: int = 0
_CRONTAB_DAY_OF_WEEK_MAX: int = 7

# 5-필드 cron 표현식에서 요일 필드의 인덱스 (분 시 일 월 [요일]).
_DAY_OF_WEEK_FIELD_INDEX: int = 4
_CRON_FIELD_COUNT: int = 5


def _convert_crontab_day_number(token: str) -> str:
    """crontab 요일 숫자 하나를 APScheduler 요일 숫자로 변환한다.

    Args:
        token: crontab 요일 숫자 문자열. 0~7 범위만 허용한다 (0/7 = 일요일).

    Returns:
        APScheduler 요일 규약(0=월 … 6=일) 의 숫자 문자열.

    Raises:
        ValueError: token 이 정수가 아니거나 0~7 범위를 벗어난 경우.
            ``CronTrigger.from_crontab`` 이 잘못된 표현식에 대해 ValueError 를
            던지는 것과 동일한 예외 타입을 유지한다.
    """
    crontab_number = int(token)
    if not (_CRONTAB_DAY_OF_WEEK_MIN <= crontab_number <= _CRONTAB_DAY_OF_WEEK_MAX):
        raise ValueError(
            f"crontab 요일 숫자는 {_CRONTAB_DAY_OF_WEEK_MIN}~{_CRONTAB_DAY_OF_WEEK_MAX} "
            f"범위여야 합니다: {token!r}"
        )
    # (n - 1) % 7 한 줄로 crontab 0~7 → APScheduler 0~6 을 모두 매핑한다.
    return str((crontab_number - 1) % 7)


def _convert_day_of_week_token(token: str) -> str:
    """요일 필드를 콤마로 나눈 토큰 1개를 APScheduler 규약으로 변환한다.

    하나의 토큰은 다음 형태 중 하나다::

        *            와일드카드
        */2          와일드카드 + 스텝
        5            단일 숫자
        1-5          범위
        1-5/2        범위 + 스텝

    알파벳이 섞인 토큰(``mon``, ``mon-fri``, ``sun`` 등)은 사용자가 이미
    APScheduler 가 그대로 받는 요일명으로 쓴 것이므로 변환하지 않고 통과시킨다
    (대소문자 무관).

    Args:
        token: 콤마로 분리된 요일 필드 토큰 1개.

    Returns:
        APScheduler 규약으로 보정된 토큰 문자열.

    Raises:
        ValueError: 토큰 안의 요일 숫자가 0~7 범위를 벗어나거나 정수가 아닌 경우.
    """
    token = token.strip()

    # 알파벳 요일명이 섞인 토큰은 그대로 통과 — APScheduler 가 직접 해석한다.
    if any(character.isalpha() for character in token):
        return token

    # 스텝 접미사 '/n' 을 분리해 보존한다 (변환 대상은 base 부분뿐).
    if "/" in token:
        base, step = token.split("/", 1)
        step_suffix = "/" + step
    else:
        base, step_suffix = token, ""
    base = base.strip()

    # 와일드카드는 요일 규약과 무관하므로 변환 없이 통과.
    if base == "*":
        return base + step_suffix

    # 범위 'a-b' — 양 끝 숫자를 각각 변환한다.
    if "-" in base:
        range_start, range_end = base.split("-", 1)
        converted_start = _convert_crontab_day_number(range_start.strip())
        converted_end = _convert_crontab_day_number(range_end.strip())
        return f"{converted_start}-{converted_end}{step_suffix}"

    # 단일 숫자.
    return _convert_crontab_day_number(base) + step_suffix


def _convert_day_of_week_field(day_of_week_field: str) -> str:
    """요일 필드 전체(콤마 리스트 포함)를 APScheduler 규약으로 변환한다.

    Args:
        day_of_week_field: cron 표현식의 5번째 필드. 예) ``1-5``, ``0,3,5``,
            ``mon-fri``, ``*/2``.

    Returns:
        각 콤마 토큰이 APScheduler 규약으로 보정된 요일 필드 문자열.
    """
    tokens = day_of_week_field.split(",")
    converted_tokens = [_convert_day_of_week_token(token) for token in tokens]
    return ",".join(converted_tokens)


def normalize_crontab_expression(cron_expression: str) -> str:
    """표준 crontab 표현식의 요일 필드를 APScheduler 규약으로 보정해 반환한다.

    분/시/일/월 필드는 두 규약이 동일하므로 원본 그대로 두고, 5번째 요일 필드만
    변환한다. 필드 개수가 5개가 아니면 변환 없이 원본을 그대로 반환한다 — 필드
    개수 오류는 ``CronTrigger.from_crontab`` 의 기존 검증 메시지가 그대로
    노출되도록 위임하기 위함이다.

    Args:
        cron_expression: 표준 crontab 5-필드 표현식 (분 시 일 월 요일).

    Returns:
        요일 필드가 APScheduler 규약으로 보정된 cron 표현식 문자열.

    Raises:
        ValueError: 요일 필드 안의 요일 숫자가 0~7 범위를 벗어난 경우.
    """
    fields = cron_expression.split()
    if len(fields) != _CRON_FIELD_COUNT:
        # 필드 개수 오류는 from_crontab 에 그대로 위임한다 (원본 반환).
        return cron_expression
    fields[_DAY_OF_WEEK_FIELD_INDEX] = _convert_day_of_week_field(
        fields[_DAY_OF_WEEK_FIELD_INDEX]
    )
    return " ".join(fields)


def build_cron_trigger(cron_expression: str, *, timezone: Any) -> Any:
    """표준 crontab 표현식으로 올바른 ``CronTrigger`` 를 생성한다.

    ``CronTrigger.from_crontab`` 의 드롭인 대체 함수다. 호출 시그니처가
    ``from_crontab(expr, timezone=...)`` 과 동일하므로 호출부를 최소 diff 로
    교체할 수 있다. 내부에서 요일 필드를 APScheduler 규약으로 보정한 뒤
    ``from_crontab`` 에 넘겨, 예측(``next_run_time``)과 실제 발화 시각이 모두
    표준 crontab 의도대로 잡히도록 한다.

    잘못된 표현식에 대한 예외 동작은 기존 ``from_crontab`` 과 동일하게
    ``ValueError`` 등을 그대로 raise 한다 — 호출부의 try/except 와
    ``ScheduleValidationError`` 변환 로직을 변경 없이 재사용하기 위함이다.

    Args:
        cron_expression: 표준 crontab 5-필드 표현식 (분 시 일 월 요일).
        timezone: 트리거 평가에 사용할 타임존 (이 프로젝트에서는 KST).

    Returns:
        요일 규약이 보정된 ``apscheduler.triggers.cron.CronTrigger`` 인스턴스.

    Raises:
        ValueError: cron 표현식의 필드 개수/범위/형식이 잘못된 경우.
    """
    # lazy import — apscheduler 의 호환성 문제를 런타임에만 노출 (service.py 의
    # 다른 함수들과 동일 정책).
    from apscheduler.triggers.cron import CronTrigger

    normalized_expression = normalize_crontab_expression(cron_expression)
    return CronTrigger.from_crontab(normalized_expression, timezone=timezone)
