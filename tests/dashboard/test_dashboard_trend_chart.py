"""추이 차트 빌더 단위 테스트 (Phase 5b / task 00042-6 → 00043-3 갱신).

검증 표면:
    - 기준일 기준 과거 30일 = 31일 (양끝 포함) 범위 (task 00043-3).
      이전 ±15일 (양방향) 가정에서 \"기준일 ~ 기준일 - 30일\" 단방향으로 이동.
    - snapshot 없는 날짜는 0 으로 채움 (사용자 원문 \"snapshot 없는 날짜는 0\").
    - 카운트는 payload.counts 합산 (전이 3종 합계는 transitioned_to_X counts 합).
    - x축 라벨은 'MM-DD' KST 형식 — design doc §9.1 의 format_kst(date, '%m-%d').
    - SQL SELECT FROM scrape_snapshots 가 1회만 (검증 15 패턴 — N+1 회피).
    - serialize_trend_chart_for_template 이 dict 로 변환되어 JSON 직렬화 친화.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from app.db.models import ScrapeSnapshot
from app.db.snapshot import normalize_payload
from app.web.dashboard_trend_chart import (
    TREND_CHART_DEFAULT_PAST_DAYS,
    TrendChartData,
    TrendChartDayPoint,
    build_trend_chart,
    serialize_trend_chart_for_template,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(test_engine: Engine) -> Iterator[Session]:
    """test_engine 위 ORM 세션."""
    from app.db.session import SessionLocal

    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()


def _insert_snapshot(
    session: Session, *, snapshot_date_iso: str, payload: dict
) -> ScrapeSnapshot:
    """payload 를 정규형으로 INSERT — 추이 차트 회귀 테스트용."""
    normalized = normalize_payload(payload)
    snapshot = ScrapeSnapshot(
        snapshot_date=date.fromisoformat(snapshot_date_iso),
        payload=normalized,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


# ---------------------------------------------------------------------------
# build_trend_chart — 31일 / 카운트 / 정렬 / 0 채움
# ---------------------------------------------------------------------------


class TestBuildTrendChart:
    """``build_trend_chart`` 빌더 회귀."""

    def test_default_window_produces_31_days(self, session: Session) -> None:
        """기본 past_days=30 → days list 길이 31 (양끝 포함, base_date 가 끝점).

        base_date = 2026-04-29 → 구간 [2026-03-30, 2026-04-29] (31일).
        days[0] 은 from_date (= base - 30), days[-1] 은 base_date 그대로.
        """
        result = build_trend_chart(session, base_date=date(2026, 4, 29))
        assert isinstance(result, TrendChartData)
        assert result.past_days == TREND_CHART_DEFAULT_PAST_DAYS == 30
        assert len(result.days) == 31
        # 첫/마지막 일자 — base_date 가 끝점, 시작은 base - 30.
        assert result.from_date == date(2026, 3, 30)
        assert result.to_date == date(2026, 4, 29)
        assert result.days[0].date_iso == "2026-03-30"
        assert result.days[-1].date_iso == "2026-04-29"

    def test_no_snapshots_fills_zero(self, session: Session) -> None:
        """snapshot 이 전혀 없으면 31일 모두 0."""
        result = build_trend_chart(session, base_date=date(2026, 4, 29))
        for point in result.days:
            assert point.new_count == 0
            assert point.content_changed_count == 0
            assert point.transitioned_count == 0

    def test_snapshot_counts_propagate(self, session: Session) -> None:
        """snapshot 의 payload.counts 가 그대로 day-by-day 데이터에 들어간다.

        새 시맨틱: base_date = 2026-04-29 → 구간 [2026-03-30, 2026-04-29].
        2026-04-29 (끝점) snapshot → 신규 5, 내용 변경 3, 전이 3종 합 = 1+2+4=7.
        """
        # base_date = 2026-04-29 → 구간 [2026-03-30, 2026-04-29].
        # 2026-04-29 (마지막 일자) snapshot — 신규 5, 내용 변경 3, 전이 3종 합 = 1+2+4=7.
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-29",
            payload={
                "new": [10, 20, 30, 40, 50],
                "content_changed": [11, 12, 13],
                "transitioned_to_접수예정": [{"id": 100, "from": "접수중"}],
                "transitioned_to_접수중": [
                    {"id": 101, "from": "접수예정"},
                    {"id": 102, "from": "접수예정"},
                ],
                "transitioned_to_마감": [
                    {"id": 103, "from": "접수중"},
                    {"id": 104, "from": "접수중"},
                    {"id": 105, "from": "접수중"},
                    {"id": 106, "from": "접수중"},
                ],
            },
        )

        result = build_trend_chart(session, base_date=date(2026, 4, 29))
        # 2026-04-29 인덱스 = 30 (마지막 — past_days 만큼 뒤로 가야 함).
        end_point = result.days[30]
        assert end_point.date_iso == "2026-04-29"
        assert end_point.new_count == 5
        assert end_point.content_changed_count == 3
        assert end_point.transitioned_count == 1 + 2 + 4

    def test_x_axis_label_is_mm_dd_format(self, session: Session) -> None:
        """x축 라벨은 'MM-DD' KST 표시 (design doc §9.1)."""
        result = build_trend_chart(session, base_date=date(2026, 4, 29))
        # 첫 일자 = 2026-03-30 → "03-30".
        assert result.days[0].x_axis_label == "03-30"
        # 마지막 = base_date = 2026-04-29 → "04-29".
        assert result.days[-1].x_axis_label == "04-29"
        # 모든 라벨이 정규식 \d{2}-\d{2} 형식.
        for point in result.days:
            assert re.fullmatch(r"\d{2}-\d{2}", point.x_axis_label) is not None

    def test_only_in_range_snapshots_propagate(self, session: Session) -> None:
        """범위 밖 snapshot 은 결과에 영향이 없다.

        새 시맨틱: 구간 [2026-03-30, 2026-04-29].
        - 2026-03-29 → 시작 경계 1일 전 (범위 밖).
        - 2026-04-30 → 끝 경계 1일 후 (범위 밖, 미래).
        - 2026-04-20 → 범위 안.
        """
        # 범위 밖 (앞 — 시작 1일 전).
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-03-29",
            payload={"new": [99]},
        )
        # 범위 밖 (뒤 — base 1일 이후 미래).
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-30",
            payload={"new": [98]},
        )
        # 범위 안.
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-20",
            payload={"new": [1, 2]},
        )

        result = build_trend_chart(session, base_date=date(2026, 4, 29))
        new_counts = {point.date_iso: point.new_count for point in result.days}
        assert "2026-03-29" not in new_counts  # 범위 밖.
        assert "2026-04-30" not in new_counts  # 범위 밖.
        assert new_counts["2026-04-20"] == 2
        # 다른 날짜는 0.
        for iso, count in new_counts.items():
            if iso != "2026-04-20":
                assert count == 0

    def test_boundary_snapshots_included(self, session: Session) -> None:
        """범위 양끝 (base-30, base) snapshot 이 포함된다 (양끝 포함 회귀)."""
        # 시작 경계 = base - 30 = 2026-03-30.
        _insert_snapshot(
            session, snapshot_date_iso="2026-03-30", payload={"new": [1]}
        )
        # 끝 경계 = base = 2026-04-29.
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-29", payload={"new": [2, 3]}
        )

        result = build_trend_chart(session, base_date=date(2026, 4, 29))
        assert result.days[0].new_count == 1   # base-30 = 2026-03-30.
        assert result.days[-1].new_count == 2  # base = 2026-04-29.

    def test_custom_past_days(self, session: Session) -> None:
        """past_days 인자 전달 시 days 길이 = past_days + 1."""
        result = build_trend_chart(
            session, base_date=date(2026, 4, 29), past_days=7
        )
        assert result.past_days == 7
        assert len(result.days) == 8  # 7 + 1 (양끝 포함).
        assert result.from_date == date(2026, 4, 22)
        assert result.to_date == date(2026, 4, 29)

    def test_single_select_query_for_snapshots(self, session: Session) -> None:
        """N+1 회피 회귀: scrape_snapshots SELECT 카운트 = 1.

        SQLAlchemy after_cursor_execute 이벤트로 'FROM scrape_snapshots' SELECT
        카운트를 잰다. 31일 범위에 N개 snapshot 이 있어도 SELECT 1회.
        """
        # 5개 snapshot — 모두 범위 안 (2026-04-20..2026-04-24).
        for offset in range(5):
            iso = (date(2026, 4, 29).replace(day=20 + offset)).isoformat()
            _insert_snapshot(
                session, snapshot_date_iso=iso, payload={"new": [offset]}
            )

        snapshot_select_count = {"value": 0}
        snapshot_pattern = re.compile(r"\bfrom\s+scrape_snapshots\b")

        def _count_selects(conn, cursor, statement, parameters, context, executemany):
            statement_lower = statement.lower()
            if not statement_lower.lstrip().startswith("select"):
                return
            if snapshot_pattern.search(statement_lower):
                snapshot_select_count["value"] += 1

        engine = session.get_bind()
        event.listen(engine, "after_cursor_execute", _count_selects)
        try:
            result = build_trend_chart(session, base_date=date(2026, 4, 29))
        finally:
            event.remove(engine, "after_cursor_execute", _count_selects)

        assert snapshot_select_count["value"] == 1
        # 5개 일자 모두 new=[offset] 1개씩 INSERT — counts.new = 1 (list length).
        # 결과 day-by-day 데이터에서 카운트가 0보다 큰 일자가 정확히 5개여야 한다.
        non_zero_days = [point for point in result.days if point.new_count > 0]
        assert len(non_zero_days) == 5

    def test_result_days_are_frozen_dataclass(self, session: Session) -> None:
        """``TrendChartDayPoint`` 가 frozen dataclass — 필드 대입 시 예외."""
        result = build_trend_chart(session, base_date=date(2026, 4, 29))
        first_point = result.days[0]
        assert isinstance(first_point, TrendChartDayPoint)
        with pytest.raises(Exception):  # FrozenInstanceError.
            first_point.new_count = 999  # type: ignore[misc]


class TestSerializeTrendChartForTemplate:
    """``serialize_trend_chart_for_template`` 회귀."""

    def test_returns_plain_dict_with_iso_dates(self, session: Session) -> None:
        """dataclass → dict 변환이 ISO 일자 / int 카운트만 포함.

        새 시맨틱: from_date = base - 30, to_date = base, past_days 필드 노출.
        """
        chart = build_trend_chart(session, base_date=date(2026, 4, 29))
        payload = serialize_trend_chart_for_template(chart)

        assert payload["base_date"] == "2026-04-29"
        assert payload["from_date"] == "2026-03-30"
        assert payload["to_date"] == "2026-04-29"
        assert payload["past_days"] == 30
        assert isinstance(payload["days"], list)
        assert len(payload["days"]) == 31
        # 각 day 가 plain dict.
        first_day = payload["days"][0]
        assert isinstance(first_day, dict)
        assert set(first_day.keys()) == {
            "date_iso",
            "x_axis_label",
            "new_count",
            "content_changed_count",
            "transitioned_count",
        }

    def test_payload_is_json_serializable(self, session: Session) -> None:
        """Payload 가 표준 ``json.dumps`` 로 직렬화 가능 — 템플릿 임베드 친화."""
        import json

        chart = build_trend_chart(session, base_date=date(2026, 4, 29))
        payload = serialize_trend_chart_for_template(chart)
        # raise 없이 통과해야 한다.
        json_text = json.dumps(payload)
        assert "2026-04-29" in json_text
