"""대시보드 추이 차트 — 기준일 기준 과거 30일 일별 카운트 빌더 (Phase 5b / task 00043-3).

배경 (사용자 원문 — task 00043):
    \"추이는 기준일 +- 15일이 아니라 기준일 ~ 기준일 - 30일 로 해줘.\"

    초기 (task 00042-6) 에는 기준일을 가운데 두고 ±15일 (총 31일) 양방향 그래프
    였으나, 사용자 요구에 따라 기준일을 그래프의 오른쪽 끝에 두고 과거 30일을
    펼치는 단방향 그래프로 변경됐다 (총 31일은 동일 — 양끝 포함).

설계 근거:
    - 새로운 구간: ``[base_date - 30days, base_date]`` 양끝 포함 → 31일.
    - 기존 ±15일 → 31일과 총 일수는 같지만 그래프의 의미가 달라진다 (\"미래까지
      펼쳐 보던\" 시야가 \"과거 30일을 돌아보는\" 시야로 이동).
    - snapshot 없는 날짜는 0 으로 채운다 (기존과 동일).
    - 서버 사전 계산 후 JSON 임베드 (별도 fetch/API 호출 없음).
    - Chart.js 4.x 로컬 vendor 번들 — 클라이언트 JS 는 days[].x_axis_label 그대로
      사용하므로 클라이언트 변경 불필요.

데이터 흐름 (1회 IN 쿼리):
    1. ``list_snapshots_in_inclusive_range(from_inclusive=base-30, to_inclusive=base)``
       으로 31일 구간의 ScrapeSnapshot list fetch.
    2. snapshot_date → counts dict map 으로 변환.
    3. base-30 ~ base 31일을 순회하며 각 날짜별로 series 3종 (신규 / 내용 변경 /
       전이) 카운트를 채운다. 매칭 안되는 날짜는 0.
    4. x축 라벨은 ``date.strftime(\"%m-%d\")`` 로 'MM-DD' KST 표시 (KST date 라
       변환 불필요).

API 표면:
    - :data:`TREND_CHART_DEFAULT_PAST_DAYS` — 기본 과거 일수 상수 (= 30).
    - :class:`TrendChartDayPoint` — 한 일자의 series 3종 카운트.
    - :class:`TrendChartData` — 라우트가 템플릿에 전달하는 dict 표현 dataclass.
    - :func:`build_trend_chart` — 단일 진입점.
    - :func:`serialize_trend_chart_for_template` — Jinja2 ``| tojson`` 친화 dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.db.repository import list_snapshots_in_inclusive_range
from app.db.snapshot import (
    CATEGORY_CONTENT_CHANGED,
    CATEGORY_NEW,
    TRANSITION_TO_LABELS,
)


# ──────────────────────────────────────────────────────────────
# 상수 — task 00043-3 의 \"기준일 기준 과거 30일\" 결정
# ──────────────────────────────────────────────────────────────


# 기준일 기준 과거로 펼치는 일수. past_days=30 → 총 31일 (base-30..base 양끝 포함).
# 사용자 원문 \"기준일 ~ 기준일 - 30일\".
TREND_CHART_DEFAULT_PAST_DAYS: int = 30


def _transition_count_keys() -> tuple[str, ...]:
    """전이 3종 카테고리 키 — payload.counts 에서 합계할 키 list.

    ``app.db.snapshot._transition_key`` 가 module-private 라 같은 형식으로
    미러한다 (dashboard_section_a.py 와 동일 패턴).
    """
    return tuple(f"transitioned_to_{label}" for label in TRANSITION_TO_LABELS)


_TRANSITION_COUNT_KEYS: tuple[str, ...] = _transition_count_keys()


# ──────────────────────────────────────────────────────────────
# Public dataclass — UI 가 소비하는 형태
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TrendChartDayPoint:
    """추이 차트의 한 일자 데이터.

    Attributes:
        date_iso:         ``YYYY-MM-DD`` KST 일자 — 임베드 JSON 의 키 / 클라이언트
                          digest.
        x_axis_label:     x축 라벨 — \"MM-DD\". design doc §9.1 의 ``format_kst(date,
                          \"%m-%d\")`` 와 동치.
        new_count:        그 날 ``payload.counts['new']`` 값. snapshot 없으면 0.
        content_changed_count: 그 날 ``payload.counts['content_changed']`` 값.
        transitioned_count: 그 날의 transitioned_to_접수예정/접수중/마감 3종 합산.
    """

    date_iso: str
    x_axis_label: str
    new_count: int
    content_changed_count: int
    transitioned_count: int


@dataclass(frozen=True)
class TrendChartData:
    """추이 차트 컨텍스트 — ``dashboard.html`` 의 trend_chart placeholder 가 사용.

    Attributes:
        base_date:    기준일 (KST date) — 그래프의 오른쪽 끝 일자.
        from_date:    그래프 시작 일자 (= ``base_date - past_days``).
        to_date:      그래프 끝 일자 (= ``base_date``).
        past_days:    기준일 기준 과거로 펼친 일수 (기본 30 → 총 31일).
        days:         day-by-day 데이터 list — ``past_days + 1`` 개 (양끝 포함).
                      각 항목이 ``TrendChartDayPoint``.

    클라이언트는 ``days`` list 를 그대로 ``Chart.js`` 의 datasets 에 넘긴다 —
    JSON 직렬화 시 dataclass 의 keys 가 그대로 노출되므로 별도 변환 불필요.
    """

    base_date: date
    from_date: date
    to_date: date
    past_days: int
    days: list[TrendChartDayPoint]


# ──────────────────────────────────────────────────────────────
# 빌더 — 단일 진입점
# ──────────────────────────────────────────────────────────────


def build_trend_chart(
    session: Session,
    *,
    base_date: date,
    past_days: int = TREND_CHART_DEFAULT_PAST_DAYS,
) -> TrendChartData:
    """추이 차트의 ``past_days + 1`` 일 day-by-day 데이터를 단일 IN 쿼리로 산출.

    호출 흐름:
        1. ``[base_date - past_days, base_date]`` 양끝 포함 구간 산출.
        2. ``list_snapshots_in_inclusive_range`` 로 1회 SELECT — N+1 회피.
        3. snapshot_date → counts dict 로 lookup map 생성.
        4. ``past_days + 1`` 일을 순회하며 각 날짜별 series 3종 카운트 산출 —
           snapshot 없으면 0.
        5. ``TrendChartDayPoint`` list 로 묶어 ``TrendChartData`` 반환.

    Args:
        session:   호출자 세션.
        base_date: 기준일 (KST date) — 그래프 오른쪽 끝.
        past_days: 과거로 펼치는 일수 (기본 30 → 총 31일). 음수 / 0 도 명시적
                   으로 허용한다 (past_days=0 이면 1일 그래프) — 호출자 검증을
                   단순하게 유지.

    Returns:
        ``TrendChartData`` — ``past_days + 1`` 개 일자 데이터.
    """
    from_date = base_date - timedelta(days=past_days)
    to_date = base_date

    snapshots_in_range = list_snapshots_in_inclusive_range(
        session,
        from_inclusive=from_date,
        to_inclusive=to_date,
    )
    counts_by_snapshot_date: dict[date, dict[str, int]] = {}
    for snapshot in snapshots_in_range:
        snapshot_counts = snapshot.payload.get("counts", {}) if snapshot.payload else {}
        counts_by_snapshot_date[snapshot.snapshot_date] = {
            "new": int(snapshot_counts.get(CATEGORY_NEW, 0)),
            "content_changed": int(snapshot_counts.get(CATEGORY_CONTENT_CHANGED, 0)),
            "transitioned": sum(
                int(snapshot_counts.get(key, 0)) for key in _TRANSITION_COUNT_KEYS
            ),
        }

    days: list[TrendChartDayPoint] = []
    total_days = past_days + 1
    for day_offset in range(total_days):
        day = from_date + timedelta(days=day_offset)
        day_counts = counts_by_snapshot_date.get(day)
        if day_counts is not None:
            new_count = day_counts["new"]
            content_changed_count = day_counts["content_changed"]
            transitioned_count = day_counts["transitioned"]
        else:
            new_count = 0
            content_changed_count = 0
            transitioned_count = 0
        days.append(
            TrendChartDayPoint(
                date_iso=day.isoformat(),
                x_axis_label=day.strftime("%m-%d"),
                new_count=new_count,
                content_changed_count=content_changed_count,
                transitioned_count=transitioned_count,
            )
        )

    return TrendChartData(
        base_date=base_date,
        from_date=from_date,
        to_date=to_date,
        past_days=past_days,
        days=days,
    )


def serialize_trend_chart_for_template(trend_chart: TrendChartData) -> dict:
    """``TrendChartData`` 를 ``Jinja2 | tojson`` 에 친화적인 dict 로 직렬화한다.

    frozen dataclass 자체는 ``| tojson`` 에서 직접 직렬화되지 않으므로 (Python
    표준 json 인코더가 dataclass 를 모름), 라우트에서 본 함수를 거쳐 dict 로
    바꿔 임베드한다. 클라이언트 JS 는 ``JSON.parse`` 후 ``data.days`` 를
    Chart.js datasets 에 매핑한다.

    Args:
        trend_chart: 빌더가 만든 ``TrendChartData``.

    Returns:
        ``Chart.js`` 가 소비하기 좋은 dict 표현. ``days`` 는 plain dict list.
    """
    return {
        "base_date": trend_chart.base_date.isoformat(),
        "from_date": trend_chart.from_date.isoformat(),
        "to_date": trend_chart.to_date.isoformat(),
        "past_days": trend_chart.past_days,
        "days": [
            {
                "date_iso": point.date_iso,
                "x_axis_label": point.x_axis_label,
                "new_count": point.new_count,
                "content_changed_count": point.content_changed_count,
                "transitioned_count": point.transitioned_count,
            }
            for point in trend_chart.days
        ],
    }


__all__ = [
    "TREND_CHART_DEFAULT_PAST_DAYS",
    "TrendChartData",
    "TrendChartDayPoint",
    "build_trend_chart",
    "serialize_trend_chart_for_template",
]
