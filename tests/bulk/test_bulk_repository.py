"""bulk 읽음 처리 repository 헬퍼 단위 테스트 (Phase 3a / 00035-4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select

from app.db.models import Announcement, AnnouncementStatus, AnnouncementUserState, User
from app.db.repository import (
    MAX_BULK_MARK,
    bulk_mark_announcements_read,
    bulk_mark_announcements_unread,
    resolve_announcement_ids_by_filter,
)
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_user(username: str) -> int:
    with session_scope() as s:
        user = User(username=username, password_hash="dummy")
        s.add(user)
        s.flush()
        return user.id


def _make_announcement(
    *,
    title: str,
    source_type: str = "IRIS",
    source_id: str | None = None,
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
) -> int:
    with session_scope() as s:
        ann = Announcement(
            source_announcement_id=source_id or title,
            source_type=source_type,
            title=title,
            status=status,
            raw_metadata={},
            is_current=True,
        )
        s.add(ann)
        s.flush()
        return ann.id


# ---------------------------------------------------------------------------
# resolve_announcement_ids_by_filter
# ---------------------------------------------------------------------------


def test_resolve_ids_returns_all_when_no_filter(test_engine: Engine):
    id1 = _make_announcement(title="공고A")
    id2 = _make_announcement(title="공고B")

    with session_scope() as s:
        ids = resolve_announcement_ids_by_filter(s)
    assert id1 in ids
    assert id2 in ids


def test_resolve_ids_filter_by_source(test_engine: Engine):
    id_iris = _make_announcement(title="IRIS공고", source_type="IRIS", source_id="iris-1")
    id_ntis = _make_announcement(title="NTIS공고", source_type="NTIS", source_id="ntis-1")

    with session_scope() as s:
        ids = resolve_announcement_ids_by_filter(s, source="IRIS")
    assert id_iris in ids
    assert id_ntis not in ids


def test_resolve_ids_filter_by_search(test_engine: Engine):
    id_match = _make_announcement(title="AI 인공지능 과제", source_id="ai-1")
    id_nomatch = _make_announcement(title="무관한 공고", source_id="etc-1")

    with session_scope() as s:
        ids = resolve_announcement_ids_by_filter(s, search="AI")
    assert id_match in ids
    assert id_nomatch not in ids


def test_resolve_ids_filter_by_status(test_engine: Engine):
    id_open = _make_announcement(
        title="접수중공고",
        source_id="open-1",
        status=AnnouncementStatus.RECEIVING,
    )
    id_closed = _make_announcement(
        title="마감공고",
        source_id="closed-1",
        status=AnnouncementStatus.CLOSED,
    )

    with session_scope() as s:
        ids = resolve_announcement_ids_by_filter(s, status="접수중")
    assert id_open in ids
    assert id_closed not in ids


def test_resolve_ids_empty_result(test_engine: Engine):
    with session_scope() as s:
        ids = resolve_announcement_ids_by_filter(s, search="절대없는검색어XYZ")
    assert ids == []


# ---------------------------------------------------------------------------
# MAX_BULK_MARK
# ---------------------------------------------------------------------------


def test_max_bulk_mark_default():
    assert MAX_BULK_MARK == 5000


# ---------------------------------------------------------------------------
# bulk_mark_announcements_read
# ---------------------------------------------------------------------------


def test_bulk_read_inserts_new_rows(test_engine: Engine):
    uid = _make_user("bulk_read_user")
    id1 = _make_announcement(title="읽음1", source_id="r1")
    id2 = _make_announcement(title="읽음2", source_id="r2")
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)

    with session_scope() as s:
        updated = bulk_mark_announcements_read(
            s, user_id=uid, announcement_ids=[id1, id2], now=now
        )
    assert updated == 2

    with session_scope() as s:
        rows = s.execute(
            select(AnnouncementUserState).where(
                AnnouncementUserState.user_id == uid,
                AnnouncementUserState.announcement_id.in_([id1, id2]),
            )
        ).scalars().all()
    assert len(rows) == 2
    assert all(r.is_read for r in rows)
    # SQLite 는 tzinfo 를 저장하지 않으므로 naive 비교.
    assert all(r.read_at == now.replace(tzinfo=None) for r in rows)


def test_bulk_read_updates_existing_rows(test_engine: Engine):
    uid = _make_user("bulk_read_update_user")
    id1 = _make_announcement(title="기존공고", source_id="ex-1")

    # 먼저 읽지 않음 상태로 row 생성
    with session_scope() as s:
        s.add(AnnouncementUserState(
            announcement_id=id1, user_id=uid, is_read=False, read_at=None
        ))
        s.flush()

    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    with session_scope() as s:
        updated = bulk_mark_announcements_read(
            s, user_id=uid, announcement_ids=[id1], now=now
        )
    assert updated == 1

    with session_scope() as s:
        row = s.execute(
            select(AnnouncementUserState).where(
                AnnouncementUserState.user_id == uid,
                AnnouncementUserState.announcement_id == id1,
            )
        ).scalar_one()
    assert row.is_read is True
    assert row.read_at == now.replace(tzinfo=None)


def test_bulk_read_empty_ids_returns_zero(test_engine: Engine):
    uid = _make_user("bulk_read_empty_user")
    with session_scope() as s:
        updated = bulk_mark_announcements_read(s, user_id=uid, announcement_ids=[])
    assert updated == 0


# ---------------------------------------------------------------------------
# bulk_mark_announcements_unread
# ---------------------------------------------------------------------------


def test_bulk_unread_inserts_new_rows(test_engine: Engine):
    uid = _make_user("bulk_unread_user")
    id1 = _make_announcement(title="안읽음1", source_id="u1")

    with session_scope() as s:
        updated = bulk_mark_announcements_unread(
            s, user_id=uid, announcement_ids=[id1]
        )
    assert updated == 1

    with session_scope() as s:
        row = s.execute(
            select(AnnouncementUserState).where(
                AnnouncementUserState.user_id == uid,
                AnnouncementUserState.announcement_id == id1,
            )
        ).scalar_one()
    assert row.is_read is False
    assert row.read_at is None


def test_bulk_unread_updates_existing_read_row(test_engine: Engine):
    uid = _make_user("bulk_unread_update_user")
    id1 = _make_announcement(title="읽음→안읽음", source_id="ru-1")
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)

    # 먼저 읽음 처리
    with session_scope() as s:
        bulk_mark_announcements_read(s, user_id=uid, announcement_ids=[id1], now=now)

    with session_scope() as s:
        updated = bulk_mark_announcements_unread(
            s, user_id=uid, announcement_ids=[id1]
        )
    assert updated == 1

    with session_scope() as s:
        row = s.execute(
            select(AnnouncementUserState).where(
                AnnouncementUserState.user_id == uid,
                AnnouncementUserState.announcement_id == id1,
            )
        ).scalar_one()
    assert row.is_read is False
    assert row.read_at is None


def test_bulk_unread_empty_ids_returns_zero(test_engine: Engine):
    uid = _make_user("bulk_unread_empty_user")
    with session_scope() as s:
        updated = bulk_mark_announcements_unread(s, user_id=uid, announcement_ids=[])
    assert updated == 0
