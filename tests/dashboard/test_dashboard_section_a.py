"""A 섹션 빌더 단위 테스트 (Phase 5b / task 00042-3).

검증 표면:
    - (from, to] reduce 누적 머지 결과가 단일 snapshot 비교와 일관 (검증 14).
    - 카운트 정합성: card.base_count == merged_payload.counts[*]
      (ID 리스트 length 가 아님 — 회귀 가드).
    - fallback 분기 3종 (a)(b)(c) 가 SectionAFallback 으로 옳게 노출.
    - 내용 변경 행의 중복 등장 배지 표시.
    - 전이 행의 transition_from 표기 (사용자 원문 '(접수예정에서)').
    - announcements JOIN 이 IN 1회만 — N+1 회피 (사용자 원문 검증 15).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    ScrapeRun,
    ScrapeSnapshot,
)
from app.db.snapshot import (
    CATEGORY_CONTENT_CHANGED,
    CATEGORY_NEW,
    merge_snapshot_payload,
    normalize_payload,
)
from app.rendering.announcement_row import (
    AnnouncementRowView,
    render_announcement_row_html,
)
from app.web.dashboard_section_a import (
    SECTION_A_CATEGORY_DESCRIPTORS,
    SectionAExpandItem,
    build_announcement_row_view,
    build_section_a,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(test_engine: Engine) -> Iterator[Session]:
    """test_engine 위에 ORM 세션. 테스트 종료 시 close."""
    from app.db.session import SessionLocal

    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()


def _make_canonical_with_announcement(
    session: Session,
    *,
    canonical_key: str,
    title: str,
    source_announcement_id: str,
    source_type: str = "IRIS",
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
    received_at: datetime | None = None,
    deadline_at: datetime | None = None,
) -> tuple[CanonicalProject, Announcement]:
    """canonical 1개 + is_current=True announcement 1개 INSERT 헬퍼."""
    canonical = CanonicalProject(
        canonical_key=canonical_key,
        key_scheme="official",
        representative_title=title,
    )
    session.add(canonical)
    session.flush()

    announcement = Announcement(
        source_type=source_type,
        source_announcement_id=source_announcement_id,
        title=title,
        agency="테스트기관",
        status=status,
        received_at=received_at,
        deadline_at=deadline_at,
        scraped_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        canonical_group_id=canonical.id,
        canonical_key=canonical_key,
    )
    session.add(announcement)
    session.flush()
    return canonical, announcement


def _insert_snapshot(
    session: Session, *, snapshot_date_iso: str, payload: dict
) -> ScrapeSnapshot:
    """snapshot_date 와 payload 를 받아 ScrapeSnapshot + 보조 ScrapeRun 1쌍을 INSERT.

    task 00150-1 에서 ``scrape_snapshots.scrape_run_id`` 가 NOT NULL FK 로 추가됐다.
    호출 시마다 새 ScrapeRun (completed) 1건도 함께 만들어 1:1 매핑을 유지한다.
    """
    normalized = normalize_payload(payload)
    scrape_run = ScrapeRun(
        started_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        status="completed",
        trigger="cli",
        source_counts={},
    )
    session.add(scrape_run)
    session.flush()

    snapshot = ScrapeSnapshot(
        scrape_run_id=scrape_run.id,
        snapshot_date=date.fromisoformat(snapshot_date_iso),
        payload=normalized,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


# ---------------------------------------------------------------------------
# build_section_a — 누적 머지 / 카운트 정합성
# ---------------------------------------------------------------------------


class TestBuildSectionA:
    """``build_section_a`` 빌더의 핵심 회귀."""

    def test_no_data_when_compare_snapshot_missing_and_no_previous(
        self, session: Session
    ) -> None:
        """(d) baseline 도 없고 (from, to] 구간 안 snapshot 도 0건 → is_no_data=True.

        DB 에 snapshot 이 전혀 없는 상태에서 호출하면 baseline 부재 + 구간 비어
        있음 두 조건이 모두 충족되어 진짜 \"데이터 없음\" 분기로 빠진다.
        """
        result = build_section_a(
            session,
            base_date=date(2026, 4, 29),
            requested_compare_date=date(2026, 4, 28),
        )

        assert result.fallback.is_no_data is True
        # 진짜 데이터 없음 박스가 우선이므로 fallback 안내문은 띄우지 않는다.
        assert result.fallback.applied is False
        assert result.fallback.message == ""
        assert result.fallback.effective_compare_date is None
        # 5종 카드 모두 0 + items 빈 list.
        assert len(result.cards) == 5
        for card in result.cards:
            assert card.base_count == 0
            assert card.compare_count is None
            assert card.delta is None
            assert card.items == []
        assert result.merged_announcement_ids == []
        assert result.merged_canonical_group_ids == []

    def test_no_baseline_but_base_snapshot_present_shows_cumulative(
        self, session: Session
    ) -> None:
        """(c-NEW) baseline 부재 + 기준일 snapshot 1건 → 카드에 누적 변화 표시.

        사용자 원문 시나리오 (task 00048 회귀) — 비교일(2026-04-28) snapshot 도,
        그 이전 snapshot 도 없으나 기준일(2026-04-29) snapshot 은 존재.
        (from, to] = (2026-04-28, 2026-04-29] 구간의 4/29 snapshot 을 누적
        머지해 카드/expand 를 정상 표시하면서, baseline 부재 자체는 데이터
        무결성에 영향이 없으므로 노란 안내문은 띄우지 않는다 (compare_count=None
        으로 카드의 \"비교일 — \" 표기만 발동).
        """
        # 사용자 원문 payload 의 announcement id 들을 1:1 로 INSERT (메타 fetch 가
        # expand items 까지 채울 수 있도록) — id 40, 55-64, 58.
        announcement_ids_in_payload = [40, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64]
        for announcement_id in announcement_ids_in_payload:
            _make_canonical_with_announcement(
                session,
                canonical_key=f"key-{announcement_id:03d}",
                title=f"공고 {announcement_id}",
                source_announcement_id=f"A-{announcement_id:03d}",
            )
        # ORM 이 1 부터 PK 를 채우므로 실제 announcement.id 와 payload id 가
        # 어긋날 수 있다 — 카운트 검증은 payload.counts 기준이라 영향이 없으나,
        # expand items 의 announcement_id 매핑까지 검증하려면 payload 의 id 를
        # 실제 INSERT 결과와 맞춰 줘야 한다. 본 테스트는 카운트/베이스라인 부재
        # 분기 검증이 핵심이므로 카운트 단언만 둔다.

        # 사용자 원문 payload 그대로 — 기준일 (2026-04-29) snapshot 1건만 INSERT.
        user_payload = {
            "new": [55, 56, 57, 59, 60, 61, 62, 63, 64],
            "content_changed": [58],
            "transitioned_to_접수예정": [],
            "transitioned_to_접수중": [{"id": 58, "from": "접수예정"}],
            "transitioned_to_마감": [{"id": 40, "from": "접수중"}],
        }
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-29",
            payload=user_payload,
        )

        result = build_section_a(
            session,
            base_date=date(2026, 4, 29),
            requested_compare_date=date(2026, 4, 28),
        )

        # baseline 부재 분기 — fallback.applied=False / message='' (task 00048).
        # is_no_data 도 False (구간 안 snapshot 1건 존재).
        assert result.fallback.is_no_data is False
        assert result.fallback.applied is False
        assert result.fallback.message == ""
        assert result.fallback.effective_compare_date is None
        assert result.fallback.requested_compare_date == date(2026, 4, 28)

        # 카운트는 사용자 원문 payload 와 정확히 일치해야 한다 (counts 기준).
        cards_by_key = {card.category_key: card for card in result.cards}
        assert cards_by_key[CATEGORY_NEW].base_count == 9
        assert cards_by_key[CATEGORY_CONTENT_CHANGED].base_count == 1
        assert cards_by_key["transitioned_to_접수예정"].base_count == 0
        assert cards_by_key["transitioned_to_접수중"].base_count == 1
        assert cards_by_key["transitioned_to_마감"].base_count == 1

        # baseline 이 없으므로 5종 모두 compare_count/delta/direction 이 None.
        for card in result.cards:
            assert card.compare_count is None, card.category_key
            assert card.delta is None, card.category_key
            assert card.delta_direction is None, card.category_key

    def test_single_day_range_matches_single_snapshot_compare(
        self, session: Session
    ) -> None:
        """검증 14: (from, to] 누적 머지가 단일 snapshot 비교와 일관.

        from=2026-04-28 (비교일 baseline), to=2026-04-29.
        구간 안의 snapshot 은 to 일자 1개뿐 → 누적 머지 결과 = to snapshot.payload 정규형.
        """
        # canonical + announcement 1개 (id=1).
        _make_canonical_with_announcement(
            session,
            canonical_key="key-001",
            title="신규 공고 A",
            source_announcement_id="A-001",
        )

        # 비교일 (from) snapshot — 빈 변화.
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-28",
            payload={"new": [], "content_changed": []},
        )
        # 기준일 (to) snapshot — id=1 신규.
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-29",
            payload={"new": [1], "content_changed": []},
        )

        result = build_section_a(
            session,
            base_date=date(2026, 4, 29),
            requested_compare_date=date(2026, 4, 28),
        )

        # fallback 미발동 (비교일 snapshot 가용).
        assert result.fallback.is_no_data is False
        assert result.fallback.applied is False
        assert result.fallback.effective_compare_date == date(2026, 4, 28)

        # 신규 카드 — base 1, compare 0, delta 1, direction up.
        new_card = next(c for c in result.cards if c.category_key == CATEGORY_NEW)
        assert new_card.base_count == 1
        assert new_card.compare_count == 0
        assert new_card.delta == 1
        assert new_card.delta_direction == "up"
        # expand items 1개 + announcement 메타 채워짐.
        assert len(new_card.items) == 1
        assert new_card.items[0].announcement_id == 1
        assert new_card.items[0].title == "신규 공고 A"
        assert new_card.items[0].source_type == "IRIS"
        assert new_card.items[0].transition_from is None  # 신규 카드는 from 없음.
        assert new_card.items[0].duplicate_badges == []

        assert result.merged_announcement_ids == [1]

    def test_multi_row_same_snapshot_date_merges_consistently(
        self, session: Session
    ) -> None:
        """task 00150-1 회귀: 같은 KST 날짜에 2 row 가 있어도 reduce 머지가 정상 동작.

        이전 (UNIQUE(snapshot_date)) 설계에서는 같은 날짜 row 가 최대 1건이라
        ``list_snapshots_in_range`` 결과의 일자별 차원이 자동으로 1:1 이었다.
        새 설계에서는 같은 날에 row 가 여러 개일 수 있어, 빌더의 reduce 머지가
        \"같은 날 2 row → set union\" 까지 정합하게 동작하는지 보호한다.

        시나리오:
            - 비교일(2026-04-25) baseline (빈 변화 1 row).
            - 기준일(2026-04-26) 에 새로 끝난 ScrapeRun 2개 → 2 row.
              row1.new=[1], row2.new=[2]. 머지 결과 new={1, 2}.
        """
        for index in range(1, 3):
            _make_canonical_with_announcement(
                session,
                canonical_key=f"key-multi-{index:03d}",
                title=f"공고 {index}",
                source_announcement_id=f"M-{index:03d}",
            )

        # baseline.
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-25", payload={"new": []}
        )
        # 같은 KST 날짜의 2 row.
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-26", payload={"new": [1]}
        )
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-26", payload={"new": [2]}
        )

        result = build_section_a(
            session,
            base_date=date(2026, 4, 26),
            requested_compare_date=date(2026, 4, 25),
        )

        new_card = next(c for c in result.cards if c.category_key == CATEGORY_NEW)
        assert new_card.base_count == 2, (
            "같은 KST 날짜의 multi-row 가 set union 으로 머지되어야 한다. "
            "1건만 잡히면 row 덮어쓰기 버그."
        )
        assert sorted([item.announcement_id for item in new_card.items]) == [1, 2]
        assert result.merged_announcement_ids == [1, 2]

    def test_n_day_reduce_consistent_with_pairwise_merge(
        self, session: Session
    ) -> None:
        """N=3 일 reduce 결과 == merge(merge(s1, s2), s3) — 사용자 원문 검증 14 회귀."""
        # 3개 announcement.
        for index in range(1, 4):
            _make_canonical_with_announcement(
                session,
                canonical_key=f"key-{index:03d}",
                title=f"공고 {index}",
                source_announcement_id=f"A-{index:03d}",
            )

        # 비교일 baseline (from).
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-25", payload={"new": []}
        )
        # 구간 (from, to]: 2026-04-26, 2026-04-27, 2026-04-28, 2026-04-29 4일.
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-26", payload={"new": [1]}
        )
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-27", payload={"new": [2]}
        )
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-28", payload={"new": [3]}
        )
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-29", payload={"new": [1]}
        )  # id=1 중복 — set union 으로 흡수.

        result = build_section_a(
            session,
            base_date=date(2026, 4, 29),
            requested_compare_date=date(2026, 4, 25),
        )

        new_card = next(c for c in result.cards if c.category_key == CATEGORY_NEW)
        # 누적 머지 결과 new = {1, 2, 3} → counts.new = 3.
        assert new_card.base_count == 3
        # 비교일 snapshot.counts.new = 0 (baseline 빈 변화).
        assert new_card.compare_count == 0
        assert new_card.delta == 3
        assert new_card.delta_direction == "up"
        assert sorted([item.announcement_id for item in new_card.items]) == [1, 2, 3]
        assert result.merged_announcement_ids == [1, 2, 3]

    def test_count_uses_payload_counts_not_id_list_length(
        self, session: Session
    ) -> None:
        """사용자 원문 주의사항: '카운트는 머지 결과 payload.counts 합산 (ID 리스트 length 아님)'.

        announcement_id 가 DB 에 없어 메타 fetch 에서 누락된 경우라도 base_count
        는 payload.counts 그대로여야 한다 (items 만 빈 list 가 됨).
        """
        # announcement INSERT 안 함 → 메타 fetch 결과는 비어 있다.
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-28", payload={"new": []}
        )
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-29", payload={"new": [999]}
        )

        result = build_section_a(
            session,
            base_date=date(2026, 4, 29),
            requested_compare_date=date(2026, 4, 28),
        )

        new_card = next(c for c in result.cards if c.category_key == CATEGORY_NEW)
        # counts 기준이면 1, items 기준이면 0 — 정답은 1.
        assert new_card.base_count == 1
        assert new_card.items == []  # 메타 누락분은 행에서 빠짐.

    def test_expand_item_carries_received_at_for_both_branches(
        self, session: Session
    ) -> None:
        """expand item 이 announcement.received_at 을 transition·non-transition
        두 빌더 경로 모두에서 채운다 (task 00135-2).

        - 신규(non-transition) 행: received_at 이 있는 공고는 그대로 전달.
        - 전이(transition) 행: received_at 이 None 인 공고는 None 으로 전달.
        """
        # id=1 — 신규 카드용. received_at 명시.
        _make_canonical_with_announcement(
            session,
            canonical_key="key-001",
            title="접수 일시 있는 공고",
            source_announcement_id="A-001",
            received_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
            deadline_at=datetime(2026, 6, 22, 0, 0, tzinfo=UTC),
        )
        # id=2 — 전이 카드용. received_at 미상(None).
        _make_canonical_with_announcement(
            session,
            canonical_key="key-002",
            title="접수 일시 없는 공고",
            source_announcement_id="A-002",
            received_at=None,
        )

        _insert_snapshot(
            session, snapshot_date_iso="2026-04-28", payload={"new": []}
        )
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-29",
            payload={
                "new": [1],
                "transitioned_to_접수중": [{"id": 2, "from": "접수예정"}],
            },
        )

        result = build_section_a(
            session,
            base_date=date(2026, 4, 29),
            requested_compare_date=date(2026, 4, 28),
        )

        # non-transition(신규) 경로 — received_at 그대로 전달.
        # SQLite 는 tz-aware datetime 을 naive 로 돌려주므로 date/시각 성분만
        # 비교한다 (deadline_at 도 동일한 round-trip 을 거친다).
        new_card = next(c for c in result.cards if c.category_key == CATEGORY_NEW)
        assert len(new_card.items) == 1
        received_at = new_card.items[0].received_at
        assert received_at is not None
        assert (received_at.year, received_at.month, received_at.day) == (
            2026,
            5,
            1,
        )
        deadline_at = new_card.items[0].deadline_at
        assert deadline_at is not None
        assert (deadline_at.year, deadline_at.month, deadline_at.day) == (
            2026,
            6,
            22,
        )

        # transition(전이) 경로 — received_at 이 None 이어도 필드가 채워진다.
        transition_card = next(
            c for c in result.cards if c.category_key == "transitioned_to_접수중"
        )
        assert len(transition_card.items) == 1
        assert transition_card.items[0].received_at is None

    def test_fallback_uses_nearest_previous_snapshot(self, session: Session) -> None:
        """(b) fallback: 비교일 가용 안 되면 가장 가까운 이전 snapshot 사용 + 안내문."""
        _make_canonical_with_announcement(
            session,
            canonical_key="key-001",
            title="신규 공고 A",
            source_announcement_id="A-001",
        )
        # 비교일 = 2026-04-22 — 가용 set 에 없음.
        # 가장 가까운 이전 = 2026-04-20.
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-20", payload={"new": []}
        )
        _insert_snapshot(
            session, snapshot_date_iso="2026-04-29", payload={"new": [1]}
        )

        result = build_section_a(
            session,
            base_date=date(2026, 4, 29),
            requested_compare_date=date(2026, 4, 22),
        )

        assert result.fallback.applied is True
        assert result.fallback.is_no_data is False
        assert result.fallback.effective_compare_date == date(2026, 4, 20)
        assert result.fallback.requested_compare_date == date(2026, 4, 22)
        # 안내문 — 사용자 원문 §4.3 (a) 형식 그대로.
        assert "2026-04-22" in result.fallback.message
        assert "2026-04-20" in result.fallback.message
        assert "사용했습니다" in result.fallback.message

    def test_content_changed_duplicate_badge_for_concurrent_transition(
        self, session: Session
    ) -> None:
        """내용 변경 행에 같은 announcement 가 전이 카테고리에도 있으면 중복 배지 표시."""
        _make_canonical_with_announcement(
            session,
            canonical_key="key-007",
            title="제조혁신 R&D",
            source_announcement_id="A-007",
            status=AnnouncementStatus.RECEIVING,
        )

        _insert_snapshot(
            session, snapshot_date_iso="2026-04-28", payload={"new": []}
        )
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-29",
            payload={
                "new": [],
                "content_changed": [1],
                "transitioned_to_접수중": [{"id": 1, "from": "접수예정"}],
            },
        )

        result = build_section_a(
            session,
            base_date=date(2026, 4, 29),
            requested_compare_date=date(2026, 4, 28),
        )

        content_card = next(
            c for c in result.cards if c.category_key == CATEGORY_CONTENT_CHANGED
        )
        assert len(content_card.items) == 1
        item = content_card.items[0]
        # 같은 ID 가 transitioned_to_접수중 에도 있으므로 배지 1개.
        assert any("접수중" in badge for badge in item.duplicate_badges)
        # transition 카드의 행은 transition_from / transition_from_key 가 있어야 한다.
        transition_card = next(
            c for c in result.cards if c.category_key == "transitioned_to_접수중"
        )
        assert len(transition_card.items) == 1
        assert transition_card.items[0].transition_from == "접수예정"
        assert transition_card.items[0].transition_from_key == "scheduled"

    def test_announcements_join_is_single_query(self, session: Session) -> None:
        """검증 15: announcement 메타 JOIN 은 IN 1회 — N+1 회피 회귀.

        SQLAlchemy 의 ``after_cursor_execute`` 이벤트로 SELECT 카운트를 잰다.
        announcement 가 5개여도 JOIN SELECT 는 정확히 1회만.
        """
        # announcement 5개.
        for index in range(1, 6):
            _make_canonical_with_announcement(
                session,
                canonical_key=f"key-{index:03d}",
                title=f"공고 {index}",
                source_announcement_id=f"A-{index:03d}",
            )

        _insert_snapshot(
            session, snapshot_date_iso="2026-04-28", payload={"new": []}
        )
        _insert_snapshot(
            session,
            snapshot_date_iso="2026-04-29",
            payload={"new": [1, 2, 3, 4, 5]},
        )

        # announcement 테이블 SELECT 카운트.
        announcement_select_count = {"value": 0}

        def _count_announcement_selects(
            conn, cursor, statement, parameters, context, executemany
        ):
            # SELECT ... FROM announcements ... 만 카운트 (대소문자 무시).
            statement_lower = statement.lower()
            if "from announcements" in statement_lower and statement_lower.startswith("select"):
                announcement_select_count["value"] += 1

        engine = session.get_bind()
        event.listen(engine, "after_cursor_execute", _count_announcement_selects)
        try:
            result = build_section_a(
                session,
                base_date=date(2026, 4, 29),
                requested_compare_date=date(2026, 4, 28),
            )
        finally:
            event.remove(engine, "after_cursor_execute", _count_announcement_selects)

        # 빌더가 실행한 announcements SELECT 는 list_announcements_by_ids 의
        # IN 쿼리 1회만.
        assert announcement_select_count["value"] == 1
        # 결과도 정확히 5개.
        new_card = next(c for c in result.cards if c.category_key == CATEGORY_NEW)
        assert new_card.base_count == 5
        assert sorted([item.announcement_id for item in new_card.items]) == [1, 2, 3, 4, 5]

    def test_reduce_with_empty_initial_payload_is_idempotent(self) -> None:
        """reduce 초깃값 None / 빈 dict / normalize_payload(None) 모두 같은 결과.

        구현은 normalize_payload(None) 을 reduce 초깃값으로 둔다 — merge 가 첫
        step 에서 normalize 를 한 번 더 호출해도 idempotent 함을 회귀 보호한다.
        """
        from functools import reduce

        payload_a = {"new": [10, 20]}
        payload_b = {"new": [30]}

        # 빌더와 동일한 패턴.
        result_with_normalize_initial = reduce(
            merge_snapshot_payload, [payload_a, payload_b], normalize_payload(None)
        )
        # 다른 동등 표현 — None 초깃값.
        result_with_none_initial = reduce(
            merge_snapshot_payload, [payload_a, payload_b], None
        )

        assert result_with_normalize_initial == result_with_none_initial
        assert result_with_normalize_initial["new"] == [10, 20, 30]
        assert result_with_normalize_initial["counts"]["new"] == 3


class TestSectionACategoryDescriptors:
    """``SECTION_A_CATEGORY_DESCRIPTORS`` 상수 회귀 — 사용자 원문 5종 순서."""

    def test_five_descriptors_in_user_request_order(self) -> None:
        """5종 디스크립터가 사용자 원문 순서 (신규 / 내용 변경 / 전이 3종) 로 노출."""
        keys_in_order = [d["key"] for d in SECTION_A_CATEGORY_DESCRIPTORS]
        assert keys_in_order == [
            "new",
            "content_changed",
            "transitioned_to_접수예정",
            "transitioned_to_접수중",
            "transitioned_to_마감",
        ]

    def test_transition_descriptors_marked_is_transition(self) -> None:
        """transition 3종만 ``is_transition == 'true'``."""
        for descriptor in SECTION_A_CATEGORY_DESCRIPTORS:
            if descriptor["key"].startswith("transitioned_to_"):
                assert descriptor["is_transition"] == "true"
            else:
                assert descriptor["is_transition"] == "false"

    def test_transition_labels_use_parenthesis_format(self) -> None:
        """전이 카드 헤더 라벨이 '(전이) X' 형식인지 확인 (task 00075)."""
        transition_descriptors = [
            d for d in SECTION_A_CATEGORY_DESCRIPTORS
            if d["is_transition"] == "true"
        ]
        assert len(transition_descriptors) == 3
        for descriptor in transition_descriptors:
            assert descriptor["label"].startswith("(전이) "), (
                f"전이 라벨이 '(전이) X' 형식이어야 함: {descriptor['label']!r}"
            )


# ---------------------------------------------------------------------------
# build_announcement_row_view — SectionAExpandItem → 공유 view-model 어댑터 (00136-3)
# ---------------------------------------------------------------------------


def _make_expand_item(
    *,
    announcement_id: int = 100,
    title: str = "테스트 공고",
    source_type: str = "IRIS",
    status_label: str = "접수중",
    status_key: str = "receiving",
    agency: str | None = "테스트기관",
    received_at: datetime | None = None,
    deadline_at: datetime | None = None,
    transition_from: str | None = None,
    transition_from_key: str | None = None,
    duplicate_badges: list[str] | None = None,
) -> SectionAExpandItem:
    """어댑터 테스트용 ``SectionAExpandItem`` 헬퍼 — 키워드로 필드만 골라 채운다."""
    return SectionAExpandItem(
        announcement_id=announcement_id,
        title=title,
        source_type=source_type,
        status_label=status_label,
        status_key=status_key,
        agency=agency,
        received_at=received_at,
        deadline_at=deadline_at,
        canonical_group_id=None,
        transition_from=transition_from,
        transition_from_key=transition_from_key,
        duplicate_badges=duplicate_badges if duplicate_badges is not None else [],
    )


class TestBuildAnnouncementRowView:
    """``build_announcement_row_view`` — 대시보드 expand 행 → 공유 view-model 변환 (00136-3)."""

    def test_returns_announcement_row_view(self) -> None:
        """변환 결과 타입이 공유 ``AnnouncementRowView`` 다."""
        view = build_announcement_row_view(_make_expand_item())
        assert isinstance(view, AnnouncementRowView)

    def test_maps_all_common_fields(self) -> None:
        """공통 필드(출처/상태/공고명/기관/날짜/중복 배지)가 1:1 로 옮겨진다."""
        received = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        deadline = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)
        item = _make_expand_item(
            title="자율주행 R&D 공고",
            source_type="NTIS",
            status_label="접수중",
            status_key="receiving",
            agency="국토교통부",
            received_at=received,
            deadline_at=deadline,
            duplicate_badges=["📝 내용 변경"],
        )
        view = build_announcement_row_view(item)
        assert view.title == "자율주행 R&D 공고"
        assert view.source_type == "NTIS"
        assert view.status_label == "접수중"
        assert view.status_key == "receiving"
        assert view.agency == "국토교통부"
        assert view.received_at == received
        assert view.deadline_at == deadline
        assert view.duplicate_badges == ["📝 내용 변경"]

    def test_builds_detail_url_from_announcement_id(self) -> None:
        """``detail_url`` 이 기존 expand 행 href 와 동일한 경로로 조립된다."""
        view = build_announcement_row_view(_make_expand_item(announcement_id=4242))
        assert view.detail_url == "/announcements/4242"

    def test_preserves_transition_fields(self) -> None:
        """전이 행은 ``transition_from`` / ``transition_from_key`` 가 보존된다."""
        item = _make_expand_item(
            status_label="접수중",
            status_key="receiving",
            transition_from="접수예정",
            transition_from_key="scheduled",
        )
        view = build_announcement_row_view(item)
        assert view.transition_from == "접수예정"
        assert view.transition_from_key == "scheduled"

    def test_non_transition_row_has_none_transition(self) -> None:
        """비전이 행은 ``transition_from`` 이 None 으로 그대로 넘어간다."""
        view = build_announcement_row_view(_make_expand_item())
        assert view.transition_from is None
        assert view.transition_from_key is None

    def test_duplicate_badges_list_is_copied(self) -> None:
        """중복 배지 list 는 원본과 같은 객체를 공유하지 않도록 복사된다."""
        item = _make_expand_item(duplicate_badges=["🆕 신규"])
        view = build_announcement_row_view(item)
        assert view.duplicate_badges == ["🆕 신규"]
        assert view.duplicate_badges is not item.duplicate_badges

    def test_rendered_row_contains_dashboard_format_elements(self) -> None:
        """변환된 view-model 을 공유 렌더러에 넘기면 대시보드 포맷 행이 렌더된다.

        출처 배지·전이/현재 상태 배지·공고명·접수/마감 일시·상세 링크가 모두
        한 ``<a href>`` 행에 포함되는지(00136-3 전환의 핵심) 확인한다.
        """
        item = _make_expand_item(
            announcement_id=777,
            title="전이 검증 공고",
            source_type="IRIS",
            status_label="접수중",
            status_key="receiving",
            received_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
            deadline_at=datetime(2026, 5, 31, 0, 0, tzinfo=UTC),
            transition_from="접수예정",
            transition_from_key="scheduled",
        )
        html_fragment = render_announcement_row_html(
            build_announcement_row_view(item)
        )
        assert 'href="/announcements/777"' in html_fragment
        assert "IRIS" in html_fragment
        assert "접수예정" in html_fragment  # 전이 이전 상태 배지
        assert "→" in html_fragment  # 전이 화살표
        assert "접수중" in html_fragment  # 현재 상태 배지
        assert "전이 검증 공고" in html_fragment
        assert "접수 " in html_fragment
        assert "마감 " in html_fragment
