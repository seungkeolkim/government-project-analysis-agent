"""사용자 라벨링 위젯 4종 + 헬퍼 4종 단위 테스트 (Phase 5b / task 00042-5).

검증 표면:
    - count_unread_announcements_for_user — is_current=True + NOT EXISTS read=True.
    - count_unjudged_canonical_for_user — canonical_projects 전체 중 RJ 없는 것.
    - count_unread_in_announcement_ids — IN + 위와 같은 NOT EXISTS, 빈 리스트는 0.
    - count_unjudged_in_canonical_ids — IN + 위와 같은 NOT EXISTS, 빈 리스트는 0.
    - build_user_label_widgets — 4 헬퍼 호출 결과를 dataclass 로 묶음.

검증 15 (N+1 회피) 회귀:
    - in_range 헬퍼 2종이 단일 SELECT 쿼리 — announcement / canonical_project IN 1번씩.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    AnnouncementUserState,
    CanonicalProject,
    RelevanceJudgment,
    User,
)
from app.db.repository import (
    count_unjudged_canonical_for_user,
    count_unjudged_in_canonical_ids,
    count_unread_announcements_for_user,
    count_unread_in_announcement_ids,
)
from app.web.dashboard_widgets import (
    DashboardWidgetsData,
    WIDGET_LABEL_UNJUDGED_IN_RANGE,
    WIDGET_LABEL_UNJUDGED_TOTAL,
    WIDGET_LABEL_UNREAD_IN_RANGE,
    WIDGET_LABEL_UNREAD_TOTAL,
    build_user_label_widgets,
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


def _make_user(session: Session, *, username: str) -> User:
    """User 1개 INSERT (테스트용 password_hash 는 비어 있는 문자열)."""
    user = User(username=username, password_hash="testhash", is_admin=False)
    session.add(user)
    session.flush()
    return user


def _make_canonical_with_announcement(
    session: Session,
    *,
    canonical_key: str,
    title: str,
    source_announcement_id: str,
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
    is_current: bool = True,
) -> tuple[CanonicalProject, Announcement]:
    """canonical 1개 + announcement 1개 INSERT."""
    canonical = CanonicalProject(
        canonical_key=canonical_key,
        key_scheme="official",
        representative_title=title,
    )
    session.add(canonical)
    session.flush()

    announcement = Announcement(
        source_type="IRIS",
        source_announcement_id=source_announcement_id,
        title=title,
        agency="기관A",
        status=status,
        is_current=is_current,
        scraped_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        canonical_group_id=canonical.id,
        canonical_key=canonical_key,
    )
    session.add(announcement)
    session.flush()
    return canonical, announcement


def _mark_read(
    session: Session,
    *,
    user_id: int,
    announcement_id: int,
) -> None:
    """AnnouncementUserState 를 is_read=True 로 INSERT."""
    state = AnnouncementUserState(
        announcement_id=announcement_id,
        user_id=user_id,
        is_read=True,
        read_at=datetime(2026, 4, 1, 1, 0, tzinfo=UTC),
    )
    session.add(state)
    session.flush()


def _judge(
    session: Session,
    *,
    user_id: int,
    canonical_project_id: int,
    verdict: str = "관련",
) -> None:
    """RelevanceJudgment INSERT."""
    rj = RelevanceJudgment(
        canonical_project_id=canonical_project_id,
        user_id=user_id,
        verdict=verdict,
    )
    session.add(rj)
    session.flush()


# ---------------------------------------------------------------------------
# 위젯 1 — count_unread_announcements_for_user
# ---------------------------------------------------------------------------


class TestCountUnreadAnnouncementsForUser:
    """위젯 1 — 전체 미확인 공고 수."""

    def test_zero_when_no_announcements(self, session: Session) -> None:
        """공고 자체가 없으면 0."""
        user = _make_user(session, username="user_a")
        assert count_unread_announcements_for_user(session, user_id=user.id) == 0

    def test_counts_active_unread_only(self, session: Session) -> None:
        """is_current=True + 사용자가 읽지 않은 announcement 만 카운트."""
        user = _make_user(session, username="user_b")
        # (a) is_current=True + 미읽음 → 카운트.
        _, ann_unread = _make_canonical_with_announcement(
            session, canonical_key="key-a", title="활성 미읽음",
            source_announcement_id="A-1",
        )
        # (b) is_current=True + 읽음 → 미카운트.
        _, ann_read = _make_canonical_with_announcement(
            session, canonical_key="key-b", title="활성 읽음",
            source_announcement_id="A-2",
        )
        _mark_read(session, user_id=user.id, announcement_id=ann_read.id)
        # (c) is_current=False (이력) → 미카운트.
        _make_canonical_with_announcement(
            session, canonical_key="key-c", title="이력",
            source_announcement_id="A-3", is_current=False,
        )

        assert count_unread_announcements_for_user(session, user_id=user.id) == 1

    def test_other_user_read_state_does_not_affect_count(
        self, session: Session
    ) -> None:
        """다른 사용자가 읽었어도 본인 카운트에는 영향 없음."""
        user_a = _make_user(session, username="user_alpha")
        user_b = _make_user(session, username="user_beta")
        _, announcement = _make_canonical_with_announcement(
            session, canonical_key="key-x", title="동일 공고",
            source_announcement_id="X-1",
        )
        _mark_read(session, user_id=user_b.id, announcement_id=announcement.id)

        # user_a 는 안 읽었으므로 1.
        assert count_unread_announcements_for_user(session, user_id=user_a.id) == 1
        # user_b 는 읽었으므로 0.
        assert count_unread_announcements_for_user(session, user_id=user_b.id) == 0


# ---------------------------------------------------------------------------
# 위젯 2 — count_unjudged_canonical_for_user
# ---------------------------------------------------------------------------


class TestCountUnjudgedCanonicalForUser:
    """위젯 2 — 전체 미판정 canonical 수."""

    def test_zero_when_no_canonicals(self, session: Session) -> None:
        """canonical_projects 자체가 없으면 0."""
        user = _make_user(session, username="user_c1")
        assert count_unjudged_canonical_for_user(session, user_id=user.id) == 0

    def test_counts_canonicals_without_user_judgment(
        self, session: Session
    ) -> None:
        """RelevanceJudgment 가 없는 canonical 만 카운트 (전수 행 기준)."""
        user = _make_user(session, username="user_c2")
        canonical_unjudged, _ = _make_canonical_with_announcement(
            session, canonical_key="key-cu", title="미판정",
            source_announcement_id="CU-1",
        )
        canonical_judged, _ = _make_canonical_with_announcement(
            session, canonical_key="key-cj", title="판정",
            source_announcement_id="CJ-1",
        )
        _judge(session, user_id=user.id, canonical_project_id=canonical_judged.id)

        assert count_unjudged_canonical_for_user(session, user_id=user.id) == 1

    def test_other_user_judgment_does_not_count(self, session: Session) -> None:
        """다른 사용자가 판정한 canonical 도 본인 기준 미판정이면 카운트."""
        user_a = _make_user(session, username="user_uca")
        user_b = _make_user(session, username="user_ucb")
        canonical, _ = _make_canonical_with_announcement(
            session, canonical_key="key-shared", title="공유 canonical",
            source_announcement_id="S-1",
        )
        _judge(session, user_id=user_b.id, canonical_project_id=canonical.id)

        assert count_unjudged_canonical_for_user(session, user_id=user_a.id) == 1
        assert count_unjudged_canonical_for_user(session, user_id=user_b.id) == 0

    def test_announcement_only_not_judged_does_not_lower_count(
        self, session: Session
    ) -> None:
        """위젯 2 는 announcements 를 보지 않는다 — 읽음 표시는 카운트에 영향 없음."""
        user = _make_user(session, username="user_uc_separation")
        canonical, announcement = _make_canonical_with_announcement(
            session, canonical_key="key-separation", title="혼동 가드",
            source_announcement_id="SEP-1",
        )
        # announcement 읽음 처리 — 위젯 2 (canonical 단위) 에 영향 없어야 한다.
        _mark_read(session, user_id=user.id, announcement_id=announcement.id)

        # 판정은 안 했으므로 미판정 canonical 1.
        assert count_unjudged_canonical_for_user(session, user_id=user.id) == 1


# ---------------------------------------------------------------------------
# 위젯 3 — count_unread_in_announcement_ids
# ---------------------------------------------------------------------------


class TestCountUnreadInAnnouncementIds:
    """위젯 3 — 기준일 변경 공고 중 미확인."""

    def test_empty_id_list_returns_zero_without_query(
        self, session: Session
    ) -> None:
        """빈 리스트 / None-only 리스트 → 쿼리 없이 0."""
        user = _make_user(session, username="user_w3_empty")
        assert (
            count_unread_in_announcement_ids(
                session, user_id=user.id, announcement_ids=[]
            )
            == 0
        )
        assert (
            count_unread_in_announcement_ids(
                session, user_id=user.id, announcement_ids=[None, None]  # type: ignore[list-item]
            )
            == 0
        )

    def test_counts_only_unread_in_given_ids(self, session: Session) -> None:
        """주어진 ID 들 안에서 사용자가 미확인인 것만 카운트."""
        user = _make_user(session, username="user_w3")
        _, ann_a = _make_canonical_with_announcement(
            session, canonical_key="k-w3a", title="A",
            source_announcement_id="W3-A",
        )
        _, ann_b = _make_canonical_with_announcement(
            session, canonical_key="k-w3b", title="B",
            source_announcement_id="W3-B",
        )
        _, ann_c = _make_canonical_with_announcement(
            session, canonical_key="k-w3c", title="C",
            source_announcement_id="W3-C",
        )
        _mark_read(session, user_id=user.id, announcement_id=ann_b.id)

        # 주어진 ID = {a, b, c}, 미읽음 = {a, c} → 2.
        assert (
            count_unread_in_announcement_ids(
                session,
                user_id=user.id,
                announcement_ids=[ann_a.id, ann_b.id, ann_c.id],
            )
            == 2
        )

    def test_unknown_id_in_input_does_not_inflate_count(
        self, session: Session
    ) -> None:
        """입력에 DB 에 없는 ID 가 섞여도 카운트는 영향 없음."""
        user = _make_user(session, username="user_w3_missing")
        _, ann = _make_canonical_with_announcement(
            session, canonical_key="k-w3m", title="존재",
            source_announcement_id="W3M-1",
        )
        # 99999 는 DB 에 없는 ID.
        result = count_unread_in_announcement_ids(
            session, user_id=user.id, announcement_ids=[ann.id, 99999]
        )
        assert result == 1


# ---------------------------------------------------------------------------
# 위젯 4 — count_unjudged_in_canonical_ids
# ---------------------------------------------------------------------------


class TestCountUnjudgedInCanonicalIds:
    """위젯 4 — 기준일 변경 공고 중 미판정 canonical."""

    def test_empty_id_list_returns_zero_without_query(
        self, session: Session
    ) -> None:
        """빈 리스트 / None-only 리스트 → 쿼리 없이 0."""
        user = _make_user(session, username="user_w4_empty")
        assert (
            count_unjudged_in_canonical_ids(
                session, user_id=user.id, canonical_ids=[]
            )
            == 0
        )
        assert (
            count_unjudged_in_canonical_ids(
                session, user_id=user.id, canonical_ids=[None]  # type: ignore[list-item]
            )
            == 0
        )

    def test_counts_only_unjudged_in_given_ids(self, session: Session) -> None:
        """주어진 canonical ID 중 미판정인 것만 카운트."""
        user = _make_user(session, username="user_w4")
        canonical_a, _ = _make_canonical_with_announcement(
            session, canonical_key="k-w4a", title="A",
            source_announcement_id="W4-A",
        )
        canonical_b, _ = _make_canonical_with_announcement(
            session, canonical_key="k-w4b", title="B",
            source_announcement_id="W4-B",
        )
        _judge(session, user_id=user.id, canonical_project_id=canonical_b.id)

        # 주어진 ID = {a, b}, 미판정 = {a} → 1.
        assert (
            count_unjudged_in_canonical_ids(
                session,
                user_id=user.id,
                canonical_ids=[canonical_a.id, canonical_b.id],
            )
            == 1
        )

    def test_canonical_ids_use_separate_unit_from_announcement_read(
        self, session: Session
    ) -> None:
        """canonical(관련성) vs announcement(읽음) 단위 혼용 금지 회귀."""
        user = _make_user(session, username="user_w4_separation")
        canonical, announcement = _make_canonical_with_announcement(
            session, canonical_key="k-w4-sep", title="단위 분리 회귀",
            source_announcement_id="W4SEP-1",
        )
        # announcement 읽음 처리 — 위젯 4 (canonical 단위) 에 영향 없어야 한다.
        _mark_read(session, user_id=user.id, announcement_id=announcement.id)

        # 판정은 안 했으므로 canonical 미판정 1.
        assert (
            count_unjudged_in_canonical_ids(
                session, user_id=user.id, canonical_ids=[canonical.id]
            )
            == 1
        )


# ---------------------------------------------------------------------------
# build_user_label_widgets — dataclass 조립 + N+1 회피 회귀
# ---------------------------------------------------------------------------


class TestBuildUserLabelWidgets:
    """``build_user_label_widgets`` 가 4 헬퍼 호출을 한 dataclass 로 묶는다."""

    def test_returns_dataclass_with_four_counts_and_labels(
        self, session: Session
    ) -> None:
        """기본 동작 — 4 카운트가 정확히 들어가고 라벨도 사용자 원문 그대로."""
        user = _make_user(session, username="user_widget_build")
        canonical_a, ann_a = _make_canonical_with_announcement(
            session, canonical_key="k-wb-a", title="A",
            source_announcement_id="WB-A",
        )
        canonical_b, ann_b = _make_canonical_with_announcement(
            session, canonical_key="k-wb-b", title="B",
            source_announcement_id="WB-B",
        )
        # 사용자 상태:
        #   - ann_a 읽음 / canonical_a 판정 / canonical_b 미판정 / ann_b 미읽음
        _mark_read(session, user_id=user.id, announcement_id=ann_a.id)
        _judge(session, user_id=user.id, canonical_project_id=canonical_a.id)

        result = build_user_label_widgets(
            session,
            user_id=user.id,
            announcement_ids=[ann_a.id, ann_b.id],
            canonical_ids=[canonical_a.id, canonical_b.id],
        )

        assert isinstance(result, DashboardWidgetsData)
        # 위젯 1 — 전체 미확인 공고 (announcement 단위) — ann_b 만.
        assert result.unread_total_count == 1
        # 위젯 2 — 전체 미판정 canonical — canonical_b 만.
        assert result.unjudged_total_count == 1
        # 위젯 3 — in-range 미확인 (ID list 한정).
        assert result.unread_in_range_count == 1
        # 위젯 4 — in-range 미판정 (ID list 한정).
        assert result.unjudged_in_range_count == 1

        # 라벨 — 사용자 원문 그대로.
        assert result.unread_total_label == WIDGET_LABEL_UNREAD_TOTAL
        assert result.unjudged_total_label == WIDGET_LABEL_UNJUDGED_TOTAL
        assert result.unread_in_range_label == WIDGET_LABEL_UNREAD_IN_RANGE
        assert result.unjudged_in_range_label == WIDGET_LABEL_UNJUDGED_IN_RANGE

    def test_in_range_helpers_run_single_query_each(self, session: Session) -> None:
        """검증 15 회귀: in_range 헬퍼 각각 announcement / canonical IN 1회만 SELECT.

        SQLAlchemy after_cursor_execute 이벤트로 announcement / canonical_projects
        SELECT 카운트를 잰다. 위젯 3 호출 = 1 announcement SELECT, 위젯 4 호출 =
        1 canonical_projects SELECT — 둘 다 N+1 아님.
        """
        user = _make_user(session, username="user_n1_guard")
        announcement_ids = []
        canonical_ids = []
        for index in range(1, 6):
            canonical, announcement = _make_canonical_with_announcement(
                session, canonical_key=f"k-n1-{index}", title=f"공고 {index}",
                source_announcement_id=f"N1-{index}",
            )
            announcement_ids.append(announcement.id)
            canonical_ids.append(canonical.id)

        import re

        announcement_select_count = {"value": 0}
        canonical_select_count = {"value": 0}

        # 'FROM announcements' 와 'FROM canonical_projects' 를 토큰 단위로
        # 매칭한다 — SQLAlchemy 가 줄바꿈으로 분리해 SELECT 를 만들 수 있어
        # 단순 포함 검색 (앞에 공백 가정) 으로는 놓치는 경우가 있다. 단어 경계
        # 정규식으로 대체.
        announcement_pattern = re.compile(r"\bfrom\s+announcements\b")
        canonical_pattern = re.compile(r"\bfrom\s+canonical_projects\b")

        def _count_selects(conn, cursor, statement, parameters, context, executemany):
            statement_lower = statement.lower()
            if not statement_lower.lstrip().startswith("select"):
                return
            if announcement_pattern.search(statement_lower):
                announcement_select_count["value"] += 1
            if canonical_pattern.search(statement_lower):
                canonical_select_count["value"] += 1

        engine = session.get_bind()
        event.listen(engine, "after_cursor_execute", _count_selects)
        try:
            in_range_unread = count_unread_in_announcement_ids(
                session, user_id=user.id, announcement_ids=announcement_ids
            )
            in_range_unjudged = count_unjudged_in_canonical_ids(
                session, user_id=user.id, canonical_ids=canonical_ids
            )
        finally:
            event.remove(engine, "after_cursor_execute", _count_selects)

        # 5 announcement 모두 미읽음 / 5 canonical 모두 미판정.
        assert in_range_unread == 5
        assert in_range_unjudged == 5
        # outer SELECT FROM announcements / canonical_projects 가 정확히 1번씩.
        assert announcement_select_count["value"] == 1
        assert canonical_select_count["value"] == 1

    def test_empty_id_lists_skip_in_range_queries(self, session: Session) -> None:
        """위젯 3·4 입력이 비어 있어도 build_user_label_widgets 는 정상 동작 (0 반환)."""
        user = _make_user(session, username="user_widget_empty")
        result = build_user_label_widgets(
            session,
            user_id=user.id,
            announcement_ids=[],
            canonical_ids=[],
        )
        assert result.unread_in_range_count == 0
        assert result.unjudged_in_range_count == 0
