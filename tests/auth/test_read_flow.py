"""읽음/안읽음 UI 통합 + 비로그인 호환 회귀 테스트.

subtask 00021-4 의 acceptance 를 검증한다:

- 로그인 사용자가 목록 페이지를 열면 ``read_id_set`` 컨텍스트가 올바르게
  채워지고 템플릿 클래스가 bold/normal 로 분기된다.
- 상세 페이지 진입 시 ``AnnouncementUserState`` 가 UPSERT 된다
  (없던 사용자는 row 생성, 이미 있는 사용자는 is_read=True/read_at 갱신,
  두 번 호출해도 row 는 1개 유지).
- 비로그인 사용자가 상세 페이지를 열어도 200 이며 ``AnnouncementUserState``
  는 생성되지 않는다.

Phase 1a 에서 'is_read 리셋' 의 가짜 User 단위 검증은 이미 존재한다
(tests/db/test_change_detection.py). 여기서는 실제 라우트를 거친 **실사용자
흐름** 만 본다 — 변경 감지 자체의 회귀는 00021-6 의 통합 테스트가 다룬다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.auth.constants import SESSION_COOKIE_NAME
from app.auth.service import create_user
from app.db.models import (
    Announcement,
    AnnouncementStatus,
    AnnouncementUserState,
)


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """격리된 DB 위에서 FastAPI TestClient 를 띄운다."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _make_announcement(
    db_session: Session,
    *,
    title: str,
    source_type: str = "IRIS",
    source_announcement_id: str = "ANN-001",
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
) -> Announcement:
    """테스트용 공고 한 건을 DB 에 만들고 반환한다."""
    announcement = Announcement(
        source_announcement_id=source_announcement_id,
        source_type=source_type,
        title=title,
        status=status,
        raw_metadata={},
        is_current=True,
    )
    db_session.add(announcement)
    db_session.commit()
    db_session.refresh(announcement)
    return announcement


def _register_and_login(client: TestClient, *, username: str, password: str) -> None:
    """회원가입으로 자동 로그인한다. TestClient 가 세션 쿠키를 자동 보관."""
    response = client.post(
        "/auth/register",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert SESSION_COOKIE_NAME in response.cookies


# ──────────────────────────────────────────────────────────────
# 목록 페이지 — read_id_set 반영 (bold/normal HTML 분기)
# ──────────────────────────────────────────────────────────────


def test_index_unread_class_for_fresh_logged_in_user(
    client: TestClient, db_session: Session
) -> None:
    """가입 직후 로그인 상태에서 목록을 열면 아직 읽은 공고가 없으므로
    제목 링크 모두 --unread 클래스가 달린다."""
    _make_announcement(db_session, title="IRIS 공고 A", source_announcement_id="A")
    _register_and_login(client, username="alice", password="alice_password_1")

    response = client.get("/")
    assert response.status_code == 200
    # read_id_set 이 비었으므로 제목 링크는 전부 unread 클래스
    assert "announcement-title-link--unread" in response.text
    assert "announcement-title-link--read" not in response.text


def test_index_read_class_after_visiting_detail(
    client: TestClient, db_session: Session
) -> None:
    """상세 페이지를 한 번 방문하면 목록에서 해당 공고만 --read 로 바뀐다."""
    ann_a = _make_announcement(
        db_session, title="IRIS 공고 A", source_announcement_id="A"
    )
    _make_announcement(db_session, title="IRIS 공고 B", source_announcement_id="B")
    _register_and_login(client, username="bob", password="bob_password_1")

    # 공고 A 상세 방문 → 자동 읽음 UPSERT
    detail_response = client.get(f"/announcements/{ann_a.id}")
    assert detail_response.status_code == 200

    # 목록 복귀 — 공고 A 는 read, B 는 unread 여야 한다.
    list_response = client.get("/")
    assert list_response.status_code == 200
    body = list_response.text
    assert "announcement-title-link--read" in body
    assert "announcement-title-link--unread" in body
    # 한 항목만 read 여야 한다 — read 빈도 1회 확인.
    assert body.count("announcement-title-link--read") == 1


def test_index_for_anonymous_has_no_read_class(
    client: TestClient, db_session: Session
) -> None:
    """비로그인은 read_id_set 이 빈 set 이라 --read 가 등장하지 않는다.
    is-anonymous body 클래스가 달려 있어야 한다 (CSS override 가 적용될 수 있게)."""
    _make_announcement(db_session, title="IRIS 공고 A", source_announcement_id="A")

    response = client.get("/")
    assert response.status_code == 200
    assert "announcement-title-link--read" not in response.text
    assert "is-anonymous" in response.text


# ──────────────────────────────────────────────────────────────
# 상세 페이지 — 자동 읽음 UPSERT
# ──────────────────────────────────────────────────────────────


def test_detail_logged_in_inserts_user_state(
    client: TestClient, db_session: Session
) -> None:
    """로그인 사용자가 처음 방문한 공고는 AnnouncementUserState row 가 생긴다."""
    ann = _make_announcement(db_session, title="IRIS 공고 A", source_announcement_id="A")
    _register_and_login(client, username="carol", password="carol_password_1")

    response = client.get(f"/announcements/{ann.id}")
    assert response.status_code == 200

    states = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == ann.id
        )
    ).scalars().all()
    assert len(states) == 1
    assert states[0].is_read is True
    assert states[0].read_at is not None


def test_detail_twice_keeps_single_row(
    client: TestClient, db_session: Session
) -> None:
    """같은 사용자가 동일 공고를 두 번 방문해도 row 는 1개 유지(UPSERT).
    두 번째 방문 시 read_at 이 갱신된다."""
    ann = _make_announcement(db_session, title="IRIS 공고 A", source_announcement_id="A")
    _register_and_login(client, username="dave", password="dave_password_1")

    # 1회차 방문
    first_response = client.get(f"/announcements/{ann.id}")
    assert first_response.status_code == 200

    first_state = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == ann.id
        )
    ).scalar_one()
    first_read_at = first_state.read_at
    assert first_read_at is not None

    # 약간의 시간 간격 후 2회차 방문 — read_at 이 update 되는지 본다.
    # SQLAlchemy 객체를 expire 해서 다음 조회가 DB 에서 새로 가져오게 한다.
    db_session.expire_all()

    second_response = client.get(f"/announcements/{ann.id}")
    assert second_response.status_code == 200

    # row 수는 여전히 1개.
    all_states = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == ann.id
        )
    ).scalars().all()
    assert len(all_states) == 1
    # 동일 row 의 read_at 이 갱신 또는 유지 (시스템 clock 해상도에 따라
    # 같을 수도 있어 '이전 값 이상' 조건만 확인).
    assert all_states[0].read_at is not None
    assert all_states[0].read_at >= first_read_at
    assert all_states[0].is_read is True


def test_detail_anonymous_does_not_create_state(
    client: TestClient, db_session: Session
) -> None:
    """비로그인 상태에서 상세 진입해도 200 + AnnouncementUserState 생성 없음.
    사용자 원문 '비로그인 상세 진입 시 에러 없음' 의 회귀 방지선."""
    ann = _make_announcement(db_session, title="IRIS 공고 A", source_announcement_id="A")

    response = client.get(f"/announcements/{ann.id}")
    assert response.status_code == 200

    states = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == ann.id
        )
    ).scalars().all()
    assert states == []


# ──────────────────────────────────────────────────────────────
# announcement 단위 — IRIS 읽어도 NTIS 는 유지
# ──────────────────────────────────────────────────────────────


def test_read_is_per_announcement_not_per_canonical(
    client: TestClient, db_session: Session
) -> None:
    """같은 제목의 IRIS / NTIS 공고는 announcement_id 가 다르므로, IRIS 를
    읽어도 NTIS 의 is_read 는 생성·갱신되지 않는다 (사용자 원문 요구).
    canonical_key 를 공유하더라도 read state 는 announcement 단위."""
    iris_ann = _make_announcement(
        db_session,
        title="공통 과제명",
        source_type="IRIS",
        source_announcement_id="IRIS-1",
    )
    ntis_ann = _make_announcement(
        db_session,
        title="공통 과제명",
        source_type="NTIS",
        source_announcement_id="NTIS-1",
    )
    _register_and_login(client, username="ellen", password="ellen_password_1")

    # IRIS 만 방문
    client.get(f"/announcements/{iris_ann.id}")

    iris_states = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == iris_ann.id
        )
    ).scalars().all()
    ntis_states = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == ntis_ann.id
        )
    ).scalars().all()

    assert len(iris_states) == 1
    assert iris_states[0].is_read is True
    # NTIS 건은 여전히 없음.
    assert ntis_states == []


# ──────────────────────────────────────────────────────────────
# repository 헬퍼 — 단위
# ──────────────────────────────────────────────────────────────


def test_get_read_announcement_id_set_returns_only_read(
    db_session: Session,
) -> None:
    """get_read_announcement_id_set 은 is_read=True 인 id 만 반환한다.

    N+1 방지용 헬퍼의 본연 동작: 주어진 id 목록 안에서 실제로 '읽은' 것만
    set 으로 돌려 받는지 직접 확인한다. 라우트 경유가 아니라 repository 를
    직접 호출한다.
    """
    from app.db.repository import get_read_announcement_id_set

    ann_a = _make_announcement(
        db_session, title="A", source_announcement_id="A"
    )
    ann_b = _make_announcement(
        db_session, title="B", source_announcement_id="B"
    )
    ann_c = _make_announcement(
        db_session, title="C", source_announcement_id="C"
    )

    user = create_user(
        db_session, username="frank", password="frank_password_1"
    )
    db_session.commit()

    # A 는 읽음, B 는 안 읽음 row 존재, C 는 row 없음
    db_session.add(
        AnnouncementUserState(
            announcement_id=ann_a.id, user_id=user.id, is_read=True,
            read_at=datetime.now(tz=UTC),
        )
    )
    db_session.add(
        AnnouncementUserState(
            announcement_id=ann_b.id, user_id=user.id, is_read=False,
        )
    )
    db_session.commit()

    result = get_read_announcement_id_set(
        db_session,
        user_id=user.id,
        announcement_ids=[ann_a.id, ann_b.id, ann_c.id],
    )
    assert result == {ann_a.id}


def test_get_read_announcement_id_set_empty_input_returns_empty(
    db_session: Session,
) -> None:
    """빈 id 목록이면 쿼리 없이 빈 set 을 반환한다."""
    from app.db.repository import get_read_announcement_id_set

    user = create_user(
        db_session, username="gina", password="gina_password_1"
    )
    db_session.commit()

    assert (
        get_read_announcement_id_set(
            db_session, user_id=user.id, announcement_ids=[]
        )
        == set()
    )


def test_mark_announcement_read_is_upsert(db_session: Session) -> None:
    """mark_announcement_read 를 같은 (announcement, user) 로 두 번 호출해도
    row 는 1개로 유지되며 read_at 이 업데이트된다."""
    from app.db.repository import mark_announcement_read

    ann = _make_announcement(db_session, title="A", source_announcement_id="A")
    user = create_user(
        db_session, username="henry", password="henry_password_1"
    )
    db_session.commit()

    first_time = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    mark_announcement_read(
        db_session, user_id=user.id, announcement_id=ann.id, now=first_time
    )
    db_session.commit()

    later_time = first_time + timedelta(hours=2)
    mark_announcement_read(
        db_session, user_id=user.id, announcement_id=ann.id, now=later_time
    )
    db_session.commit()

    rows = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.user_id == user.id
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].is_read is True
    assert rows[0].read_at == later_time
