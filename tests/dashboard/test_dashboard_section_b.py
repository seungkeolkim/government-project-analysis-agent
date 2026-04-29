"""B 섹션 빌더 단위 테스트 (Phase 5b / task 00042-4).

검증 표면:
    - is_current=True 활성 공고만 매칭 (이력 row 활용 범위 밖).
    - 접수예정 그룹: status='접수예정' AND received_at BETWEEN to AND to+30days.
    - 마감예정 그룹: status='접수중' AND deadline_at BETWEEN to AND to+30days.
    - 정렬: 접수예정 received_at ASC, 마감예정 deadline_at ASC (임박 순).
    - to 가 KST 오늘 과거이면 안내문 + is_to_in_past=True (사용자 원문 §7.2).
    - 구간 경계 (KST 자정) — 30일 정확.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.models import Announcement, AnnouncementStatus, CanonicalProject
from app.web.dashboard_section_b import (
    SECTION_B_PAST_BASE_DATE_NOTICE,
    SectionBData,
    build_section_b,
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


def _make_announcement(
    session: Session,
    *,
    title: str,
    source_announcement_id: str,
    canonical_key: str,
    status: AnnouncementStatus,
    received_at: datetime | None = None,
    deadline_at: datetime | None = None,
    is_current: bool = True,
    source_type: str = "IRIS",
) -> Announcement:
    """canonical 1개 + announcement 1개 INSERT 헬퍼 (B 섹션 검증용)."""
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
        is_current=is_current,
        scraped_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        canonical_group_id=canonical.id,
        canonical_key=canonical_key,
    )
    session.add(announcement)
    session.flush()
    return announcement


# UTC 자정 헬퍼 — KST 자정과의 9시간 차이 검증을 단순하게 하기 위해.
def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    """``datetime(year, month, day, hour, tzinfo=UTC)`` 를 짧게 만든다."""
    return datetime(year, month, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# build_section_b — 그룹별 매칭 / 정렬 / past 안내문
# ---------------------------------------------------------------------------


class TestBuildSectionB:
    """``build_section_b`` 의 핵심 회귀."""

    def test_empty_db_returns_empty_groups(self, session: Session) -> None:
        """DB 가 비면 두 그룹 모두 빈 list. 안내문도 비활성 (to=오늘)."""
        from app.timezone import now_kst

        result = build_section_b(session, to_date=now_kst().date())
        assert isinstance(result, SectionBData)
        assert result.soon_to_open == []
        assert result.soon_to_close == []
        assert result.days_window == 30
        assert result.is_to_in_past is False
        assert result.past_notice_message == ""

    def test_soon_to_open_matches_only_scheduled_status_within_window(
        self, session: Session
    ) -> None:
        """접수예정 그룹: status=접수예정 + received_at 이 [to, to+30) 안에만 매칭."""
        # to_date = 2026-04-29 (KST). 구간 = [2026-04-29 KST, 2026-05-29 KST) →
        # UTC = [2026-04-28 15:00, 2026-05-28 15:00).

        # (a) 매칭 — 접수예정 + received_at 구간 안.
        match_announcement = _make_announcement(
            session,
            canonical_key="cb-001",
            title="A 접수예정 매칭",
            source_announcement_id="A-001",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 5, 1, 0),  # 구간 안.
        )
        # (b) 비매칭 — status 가 접수중.
        _make_announcement(
            session,
            canonical_key="cb-002",
            title="B 접수중 (마감예정 그룹 후보)",
            source_announcement_id="B-002",
            status=AnnouncementStatus.RECEIVING,
            received_at=_utc(2026, 5, 2, 0),
            deadline_at=_utc(2026, 5, 10, 0),
        )
        # (c) 비매칭 — received_at 이 구간 밖 (지난).
        _make_announcement(
            session,
            canonical_key="cb-003",
            title="C 접수예정이지만 과거",
            source_announcement_id="C-003",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 4, 1, 0),  # 구간 시작 전.
        )
        # (d) 비매칭 — received_at 이 구간 밖 (미래).
        _make_announcement(
            session,
            canonical_key="cb-004",
            title="D 접수예정 미래",
            source_announcement_id="D-004",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 6, 1, 0),  # 구간 끝 후.
        )
        # (e) 비매칭 — is_current=False 이력 row.
        _make_announcement(
            session,
            canonical_key="cb-005",
            title="E 접수예정 이력",
            source_announcement_id="E-005",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 5, 3, 0),
            is_current=False,
        )

        result = build_section_b(session, to_date=date(2026, 4, 29))

        # 매칭은 (a) 1건만.
        assert len(result.soon_to_open) == 1
        assert result.soon_to_open[0].announcement_id == match_announcement.id

    def test_soon_to_close_matches_only_receiving_status_within_window(
        self, session: Session
    ) -> None:
        """마감예정 그룹: status=접수중 + deadline_at 이 [to, to+30) 안에만 매칭."""
        match_announcement = _make_announcement(
            session,
            canonical_key="cb-101",
            title="A 마감예정 매칭",
            source_announcement_id="A-101",
            status=AnnouncementStatus.RECEIVING,
            deadline_at=_utc(2026, 5, 10, 0),  # 구간 안.
        )
        # 비매칭 — status 가 접수예정 (접수예정 그룹은 별도 검증).
        _make_announcement(
            session,
            canonical_key="cb-102",
            title="B 접수예정 (소속 다른 그룹)",
            source_announcement_id="B-102",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 5, 5, 0),
            deadline_at=_utc(2026, 5, 11, 0),
        )
        # 비매칭 — 마감일 구간 밖.
        _make_announcement(
            session,
            canonical_key="cb-103",
            title="C 마감 미래",
            source_announcement_id="C-103",
            status=AnnouncementStatus.RECEIVING,
            deadline_at=_utc(2026, 6, 5, 0),
        )
        # 비매칭 — is_current=False 이력 row.
        _make_announcement(
            session,
            canonical_key="cb-104",
            title="D 마감예정 이력",
            source_announcement_id="D-104",
            status=AnnouncementStatus.RECEIVING,
            deadline_at=_utc(2026, 5, 12, 0),
            is_current=False,
        )

        result = build_section_b(session, to_date=date(2026, 4, 29))

        assert len(result.soon_to_close) == 1
        assert result.soon_to_close[0].announcement_id == match_announcement.id

    def test_soon_to_open_sorted_by_received_at_ascending(
        self, session: Session
    ) -> None:
        """접수예정 그룹은 received_at 임박 순(asc) 정렬."""
        late = _make_announcement(
            session,
            canonical_key="cb-201",
            title="늦은 접수예정",
            source_announcement_id="L-201",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 5, 20, 0),
        )
        early = _make_announcement(
            session,
            canonical_key="cb-202",
            title="이른 접수예정",
            source_announcement_id="E-202",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 5, 1, 0),
        )
        middle = _make_announcement(
            session,
            canonical_key="cb-203",
            title="중간 접수예정",
            source_announcement_id="M-203",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 5, 10, 0),
        )

        result = build_section_b(session, to_date=date(2026, 4, 29))

        assert [row.announcement_id for row in result.soon_to_open] == [
            early.id,
            middle.id,
            late.id,
        ]

    def test_soon_to_close_sorted_by_deadline_at_ascending(
        self, session: Session
    ) -> None:
        """마감예정 그룹은 deadline_at 임박 순(asc) 정렬."""
        late = _make_announcement(
            session,
            canonical_key="cb-301",
            title="늦은 마감",
            source_announcement_id="L-301",
            status=AnnouncementStatus.RECEIVING,
            deadline_at=_utc(2026, 5, 20, 0),
        )
        early = _make_announcement(
            session,
            canonical_key="cb-302",
            title="이른 마감",
            source_announcement_id="E-302",
            status=AnnouncementStatus.RECEIVING,
            deadline_at=_utc(2026, 5, 1, 0),
        )

        result = build_section_b(session, to_date=date(2026, 4, 29))

        assert [row.announcement_id for row in result.soon_to_close] == [
            early.id,
            late.id,
        ]

    def test_to_in_past_sets_notice(self, session: Session) -> None:
        """검증 11: to 가 오늘보다 과거면 안내문 + is_to_in_past=True (정상 표시는 유지)."""
        from app.timezone import now_kst

        past_date = now_kst().date() - timedelta(days=10)
        # 매칭이 안 나오도록 하나만 미래에 INSERT.
        _make_announcement(
            session,
            canonical_key="cb-401",
            title="과거 to 검증용 — 미매칭",
            source_announcement_id="X-401",
            status=AnnouncementStatus.RECEIVING,
            deadline_at=_utc(2099, 1, 1, 0),
        )

        result = build_section_b(session, to_date=past_date)

        assert result.is_to_in_past is True
        assert result.past_notice_message == SECTION_B_PAST_BASE_DATE_NOTICE
        # to 가 과거여도 두 그룹 자체는 정상 (사용자 원문 \"+ 정상 표시\").
        # 매칭이 0건이어도 list 자체는 빈 list 로 노출.
        assert isinstance(result.soon_to_open, list)
        assert isinstance(result.soon_to_close, list)

    def test_today_to_does_not_set_past_notice(self, session: Session) -> None:
        """to 가 KST 오늘이면 안내문 비활성."""
        from app.timezone import now_kst

        result = build_section_b(session, to_date=now_kst().date())
        assert result.is_to_in_past is False
        assert result.past_notice_message == ""

    def test_window_boundary_inclusive_of_start_exclusive_of_end(
        self, session: Session
    ) -> None:
        """[to, to+30) 반-open 구간 — 시작 KST 자정 포함, 끝 KST 자정 배타."""
        # to_date = 2026-04-29 → KST 자정 = 2026-04-29 00:00 KST = 2026-04-28 15:00 UTC.
        # to+30 = 2026-05-29 → KST 자정 = 2026-05-28 15:00 UTC.

        # 시작 KST 자정 정각 — 포함되어야 한다.
        boundary_start = _make_announcement(
            session,
            canonical_key="cb-501",
            title="시작 경계",
            source_announcement_id="S-501",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 4, 28, 15),  # KST 2026-04-29 00:00 정각.
        )
        # 시작 1초 전 — 제외되어야 한다.
        _make_announcement(
            session,
            canonical_key="cb-502",
            title="시작 1초 전",
            source_announcement_id="S-502",
            status=AnnouncementStatus.SCHEDULED,
            received_at=datetime(2026, 4, 28, 14, 59, 59, tzinfo=UTC),
        )
        # 끝 1초 전 — 포함되어야 한다.
        boundary_end_minus_1 = _make_announcement(
            session,
            canonical_key="cb-503",
            title="끝 1초 전",
            source_announcement_id="E-503",
            status=AnnouncementStatus.SCHEDULED,
            received_at=datetime(2026, 5, 28, 14, 59, 59, tzinfo=UTC),
        )
        # 끝 정각 — 제외되어야 한다 (반-open).
        _make_announcement(
            session,
            canonical_key="cb-504",
            title="끝 정각",
            source_announcement_id="E-504",
            status=AnnouncementStatus.SCHEDULED,
            received_at=_utc(2026, 5, 28, 15),
        )

        result = build_section_b(session, to_date=date(2026, 4, 29))
        ids = {row.announcement_id for row in result.soon_to_open}

        assert boundary_start.id in ids
        assert boundary_end_minus_1.id in ids
        assert len(ids) == 2  # 시작-1 / 끝-정각 두 건은 제외됨.

    def test_default_days_window_is_30(self, session: Session) -> None:
        """``days`` 기본값 30 — 사용자 원문 \"1개월 이내\"."""
        result = build_section_b(session, to_date=date(2026, 4, 29))
        assert result.days_window == 30

    def test_custom_days_window_propagates(self, session: Session) -> None:
        """``days`` 인자는 SectionBData.days_window 에 그대로 들어간다."""
        result = build_section_b(session, to_date=date(2026, 4, 29), days=14)
        assert result.days_window == 14
