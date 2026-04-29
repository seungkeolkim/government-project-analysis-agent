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
    ScrapeSnapshot,
)
from app.db.snapshot import (
    CATEGORY_CONTENT_CHANGED,
    CATEGORY_NEW,
    merge_snapshot_payload,
    normalize_payload,
)
from app.web.dashboard_section_a import (
    SECTION_A_CATEGORY_DESCRIPTORS,
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
        received_at=None,
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
    """snapshot_date 와 payload 를 받아 ScrapeSnapshot 을 INSERT 한다."""
    normalized = normalize_payload(payload)
    snapshot = ScrapeSnapshot(
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

        사용자 원문 시나리오 — 비교일(2026-04-28) snapshot 도, 그 이전
        snapshot 도 없으나 기준일(2026-04-29) snapshot 은 존재. 이전 동작은
        is_no_data=True 로 0건을 표시했지만, 본 회귀는 (from, to] 구간 안의
        2026-04-29 snapshot 을 활용해 누적 결과 + compare_count=None (\"비교일
        — \") 으로 노출되는지 검증한다.
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

        # baseline 부재 분기 — fallback.applied=True (\"baseline 없이 누적만\"
        # 안내문) + is_no_data=False.
        assert result.fallback.is_no_data is False
        assert result.fallback.applied is True
        assert result.fallback.effective_compare_date is None
        assert "baseline 없이" in result.fallback.message

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
        # transition 카드의 행은 transition_from 이 있어야 한다.
        transition_card = next(
            c for c in result.cards if c.category_key == "transitioned_to_접수중"
        )
        assert len(transition_card.items) == 1
        assert transition_card.items[0].transition_from == "접수예정"

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
