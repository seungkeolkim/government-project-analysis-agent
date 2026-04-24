"""bulk 읽음 처리 라우터 HTTP 통합 테스트 (Phase 3a / 00035-4).

TestClient 로 두 엔드포인트를 커버한다:
    POST /announcements/bulk-mark-read
    POST /announcements/bulk-mark-unread
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.db.models import Announcement, AnnouncementStatus, AnnouncementUserState
from app.db.session import session_scope


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    """회원가입 후 세션 쿠키가 유지되는 TestClient."""
    client.post(
        "/auth/register",
        data={"username": "bulk_user", "password": "password_123"},
        follow_redirects=False,
    )
    return client


def _make_announcement(title: str, source_id: str | None = None) -> int:
    with session_scope() as s:
        ann = Announcement(
            source_announcement_id=source_id or title,
            source_type="IRIS",
            title=title,
            status=AnnouncementStatus.RECEIVING,
            raw_metadata={},
            is_current=True,
        )
        s.add(ann)
        s.flush()
        return ann.id


# ---------------------------------------------------------------------------
# 비로그인 → 401
# ---------------------------------------------------------------------------


def test_bulk_mark_read_unauthenticated(client: TestClient):
    resp = client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "ids", "ids": [1]},
    )
    assert resp.status_code == 401


def test_bulk_mark_unread_unauthenticated(client: TestClient):
    resp = client.post(
        "/announcements/bulk-mark-unread",
        json={"mode": "ids", "ids": [1]},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# ids 모드 — 정상
# ---------------------------------------------------------------------------


def test_bulk_mark_read_ids_mode(logged_in_client: TestClient, test_engine: Engine):
    id1 = _make_announcement("ids-read-1", "ir1")
    id2 = _make_announcement("ids-read-2", "ir2")

    resp = logged_in_client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "ids", "ids": [id1, id2]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 2


def test_bulk_mark_unread_ids_mode(logged_in_client: TestClient, test_engine: Engine):
    id1 = _make_announcement("ids-unread-1", "iu1")

    # 먼저 읽음
    logged_in_client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "ids", "ids": [id1]},
    )

    resp = logged_in_client.post(
        "/announcements/bulk-mark-unread",
        json={"mode": "ids", "ids": [id1]},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1


# ---------------------------------------------------------------------------
# ids 모드 — 유효성 검사
# ---------------------------------------------------------------------------


def test_bulk_mark_read_empty_ids(logged_in_client: TestClient, test_engine: Engine):
    resp = logged_in_client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "ids", "ids": []},
    )
    assert resp.status_code == 422


def test_bulk_mark_read_invalid_mode(logged_in_client: TestClient, test_engine: Engine):
    resp = logged_in_client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "unknown", "ids": [1]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# filter 모드 — 정상
# ---------------------------------------------------------------------------


def test_bulk_mark_read_filter_mode(logged_in_client: TestClient, test_engine: Engine):
    _make_announcement("필터읽음공고1", "filt-r1")
    _make_announcement("필터읽음공고2", "filt-r2")

    resp = logged_in_client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "filter", "filter": {"status": "접수중"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] >= 2


def test_bulk_mark_read_filter_empty_result(
    logged_in_client: TestClient, test_engine: Engine
):
    resp = logged_in_client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "filter", "filter": {"search": "절대없는검색어XYZ"}},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 0


def test_bulk_mark_read_filter_no_filter_field(
    logged_in_client: TestClient, test_engine: Engine
):
    """filter 필드 생략 시 전체 공고 대상."""
    _make_announcement("노필터공고", "nf-1")

    resp = logged_in_client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "filter"},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] >= 1


# ---------------------------------------------------------------------------
# MAX_BULK_MARK 초과 → 422
# ---------------------------------------------------------------------------


def test_bulk_mark_read_exceeds_max(
    logged_in_client: TestClient, monkeypatch: pytest.MonkeyPatch, test_engine: Engine
):
    import app.web.routes.bulk as bulk_mod
    import app.db.repository as repo_mod

    monkeypatch.setattr(bulk_mod, "MAX_BULK_MARK", 2)
    monkeypatch.setattr(repo_mod, "MAX_BULK_MARK", 2)

    resp = logged_in_client.post(
        "/announcements/bulk-mark-read",
        json={"mode": "ids", "ids": [1, 2, 3]},
    )
    assert resp.status_code == 422
