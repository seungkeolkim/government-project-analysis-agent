"""실사용자 흐름 통합 테스트.

Phase 1a 에서 가짜 User 단위로 검증된 'is_read 리셋' 과 세션 만료 동작이
실제 register → login → 상세 진입 → (스크래핑 시뮬) → 재방문 흐름에서도
동작하는지 회귀 방지선을 친다.

두 가지 시나리오를 담는다:

1. **변경 감지 리셋 — 실사용자 흐름**
   - FastAPI TestClient 로 register → 자동 로그인.
   - DB 에 공고 1건 생성(upsert_announcement "created").
   - GET /announcements/{id} → AnnouncementUserState 로 is_read=True.
   - repository.upsert_announcement 로 **title 만 바뀐** payload 를 다시 호출해
     내용 변경(new_version) 을 트리거한다. 실제 스크래퍼는 호출하지 않는다.
   - 기대: Phase 1a 의 ``_reset_user_state_on_content_change`` 가 구 row 의
     AnnouncementUserState 를 is_read=False / read_at=NULL 로 리셋.

2. **세션 만료 검증**
   - 가입 후 DB 에서 UserSession.expires_at 을 과거로 UPDATE.
   - GET /auth/me → ``{"user": null}`` (만료 세션은 비로그인과 동일 처리).
   - GET /login → 200 (이미 로그인 상태가 아니므로 로그인 페이지 렌더).

guidance 의 핵심 주의사항:
    - 변경 감지 트리거는 repository.upsert_announcement 로 직접 호출 (실제
      스크래퍼 금지).
    - TestClient 가 요청마다 새 SessionLocal() 을 열기 때문에, 테스트의
      ``db_session`` 과 commit 가시성은 ``expire_all()`` 로 맞춘다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.auth.constants import SESSION_COOKIE_NAME
from app.db.models import (
    Announcement,
    AnnouncementStatus,
    AnnouncementUserState,
    User,
    UserSession,
)
from app.db.repository import upsert_announcement


# ──────────────────────────────────────────────────────────────
# Fixtures & helpers
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """격리된 DB 위에서 FastAPI TestClient 를 띄운다.

    ``test_engine`` fixture 가 선행 실행되어 DB_URL 환경변수와 cache 가
    초기화된 상태에서 create_app() 을 돌린다.
    """
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _build_payload(
    *,
    source_announcement_id: str,
    title: str,
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
    deadline_at: datetime | None = None,
) -> dict[str, Any]:
    """upsert_announcement payload 를 구성한다.

    테스트 전체에서 동일 ``source_announcement_id`` + ``source_type='IRIS'`` 로
    같은 '공고' 를 가리키게 해 new_version 분기가 탈 수 있도록 한다.
    """
    return {
        "source_announcement_id": source_announcement_id,
        "source_type": "IRIS",
        "title": title,
        "status": status,
        "agency": "테스트 기관",
        "deadline_at": deadline_at or datetime(2026, 5, 31, tzinfo=UTC),
        "raw_metadata": {},
        "ancm_no": f"ANCM-{source_announcement_id}",
    }


def _register_and_login(
    client: TestClient, *, username: str, password: str
) -> None:
    """회원가입을 통해 자동 로그인 상태를 만든다.

    TestClient 는 Set-Cookie 를 자동 보관하므로 이후 요청은 인증 상태가 된다.
    """
    response = client.post(
        "/auth/register",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert SESSION_COOKIE_NAME in response.cookies


# ──────────────────────────────────────────────────────────────
# 시나리오 1 — 변경 감지 리셋 (실사용자 흐름)
# ──────────────────────────────────────────────────────────────


def test_title_change_resets_is_read_in_real_user_flow(
    client: TestClient,
    db_session: Session,
) -> None:
    """사용자 원문 검증 항목 #7 — 읽음 처리 후 title 변경 스크래핑 시뮬 → is_read False.

    흐름:
        1. 사용자 가입(자동 로그인).
        2. 공고 1건 upsert_announcement 로 최초 생성 (action='created').
        3. 상세 페이지 방문으로 AnnouncementUserState(is_read=True) 생성.
        4. 같은 source id 에 title 만 바꾼 payload 로 upsert_announcement 재호출.
           → action='new_version' 이 반환되고, 기존 id 의 AnnouncementUserState 가
             is_read=False, read_at=None 으로 **실사용자 흐름에서도** 리셋된다.
        5. 새 id 의 공고는 AnnouncementUserState row 자체가 없으므로 목록에서
           --unread 로 렌더된다.
    """
    # ── 1) 사용자 가입 + 자동 로그인 ──────────────────────────────────────────
    _register_and_login(
        client, username="realflow_user", password="realflow_password_1"
    )

    user = db_session.query(User).filter_by(username="realflow_user").one()

    # ── 2) 공고 최초 등록 (action='created') ──────────────────────────────────
    first_result = upsert_announcement(
        db_session,
        _build_payload(source_announcement_id="SRC-001", title="최초 공고명"),
    )
    db_session.commit()
    assert first_result.action == "created"
    original_announcement_id = first_result.announcement.id

    # ── 3) 상세 페이지 진입 → is_read=True ───────────────────────────────────
    detail_response = client.get(f"/announcements/{original_announcement_id}")
    assert detail_response.status_code == 200

    # TestClient 는 별도 session 에서 commit 했으므로 db_session 의 캐시를
    # 무효화해 최신 상태를 읽는다.
    db_session.expire_all()

    state_after_read = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == original_announcement_id,
            AnnouncementUserState.user_id == user.id,
        )
    ).scalar_one()
    assert state_after_read.is_read is True
    assert state_after_read.read_at is not None

    # ── 4) 스크래핑 시뮬 — title 만 변경한 payload 로 재-upsert ─────────────
    # title 은 _CHANGE_DETECTION_FIELDS 에 포함되므로 (d) new_version 분기가 탄다.
    second_result = upsert_announcement(
        db_session,
        _build_payload(
            source_announcement_id="SRC-001",
            title="제목이 교체된 공고명",
        ),
    )
    db_session.commit()
    assert second_result.action == "new_version"
    assert "title" in second_result.changed_fields
    new_announcement_id = second_result.announcement.id
    assert new_announcement_id != original_announcement_id

    # ── 5) 기존 id 의 AnnouncementUserState 가 리셋되었는지 확인 ──────────────
    db_session.expire_all()
    reset_state = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == original_announcement_id,
            AnnouncementUserState.user_id == user.id,
        )
    ).scalar_one()
    # Phase 1a 의 _reset_user_state_on_content_change 가 실사용자 row 에서도
    # 정확히 동작해야 한다. (tests/db/test_change_detection.py 는 가짜 User 로 이미
    # 검증했지만, 여기서는 실 라우트를 경유한 읽음 상태가 대상이라는 게 포인트.)
    assert reset_state.is_read is False
    assert reset_state.read_at is None

    # 새 id 의 공고에는 아직 어떤 user state 도 없어야 한다 (리셋이 아니라 '생성
    # 전' 상태).
    new_state_rows = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == new_announcement_id,
        )
    ).scalars().all()
    assert new_state_rows == []

    # ── 6) 목록 페이지 복귀 — 새 공고는 --unread 로 표시 ─────────────────────
    list_response = client.get("/")
    assert list_response.status_code == 200
    body = list_response.text
    # 새 id 는 아직 읽지 않았으므로 --unread 클래스가 붙는다.
    assert "announcement-title-link--unread" in body
    # 구 id 는 is_current=False 로 봉인되어 목록에 나타나지 않는다.
    assert f'/announcements/{original_announcement_id}"' not in body
    # 새 id 의 링크는 있어야 한다.
    assert f'/announcements/{new_announcement_id}"' in body


def test_status_only_change_does_not_reset_is_read_in_real_user_flow(
    client: TestClient,
    db_session: Session,
) -> None:
    """status 단독 전이(status_transitioned)는 리셋을 일으키지 않는다.

    Phase 1a 규약 재확인 — 실사용자 흐름에서도 status 만 바뀌는 경우(접수예정→
    접수중 등)는 in-place UPDATE 로 처리되어 is_read 가 유지되어야 한다.
    """
    _register_and_login(
        client, username="status_user", password="status_password_1"
    )
    user = db_session.query(User).filter_by(username="status_user").one()

    first = upsert_announcement(
        db_session,
        _build_payload(
            source_announcement_id="SRC-002",
            title="상태만 바뀔 공고",
            status=AnnouncementStatus.SCHEDULED,
        ),
    )
    db_session.commit()
    announcement_id = first.announcement.id

    client.get(f"/announcements/{announcement_id}")
    db_session.expire_all()

    # status 단독 전이 (SCHEDULED → RECEIVING). 나머지 필드는 동일.
    second = upsert_announcement(
        db_session,
        _build_payload(
            source_announcement_id="SRC-002",
            title="상태만 바뀔 공고",
            status=AnnouncementStatus.RECEIVING,
        ),
    )
    db_session.commit()
    assert second.action == "status_transitioned"
    # 같은 row 가 in-place 로 업데이트 되므로 id 가 바뀌지 않는다.
    assert second.announcement.id == announcement_id

    db_session.expire_all()
    state = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == announcement_id,
            AnnouncementUserState.user_id == user.id,
        )
    ).scalar_one()
    # is_read 는 유지되어야 한다 (리셋 금지 불변식).
    assert state.is_read is True
    assert state.read_at is not None


# ──────────────────────────────────────────────────────────────
# 시나리오 2 — 세션 만료 검증
# ──────────────────────────────────────────────────────────────


def test_expired_session_falls_back_to_anonymous(
    client: TestClient,
    db_session: Session,
) -> None:
    """사용자 원문 검증 항목 #6 — expires_at 을 과거로 수정하면 로그인 페이지가 열린다.

    만료된 세션은 서버가 "비로그인" 으로 취급해야 하므로:
      - /auth/me → {"user": null}
      - /login → 200 (이미 로그인 상태 redirect 분기를 타지 않는다)
      - / → 200 + 상단 네비가 로그인/회원가입 링크
    """
    _register_and_login(
        client, username="expiring_user", password="expiring_password_1"
    )

    # 로그인 직후 /auth/me 는 user 객체를 돌려준다 (sanity check).
    me_before = client.get("/auth/me")
    assert me_before.status_code == 200
    assert me_before.json()["user"]["username"] == "expiring_user"

    # DB 에 직접 UPDATE 해 세션 만료 시각을 과거로 당긴다 — guidance 가 권장한
    # 방식. TestClient 요청은 별도 SessionLocal() 을 쓰므로 여기서 commit 하면
    # 다음 요청이 곧바로 만료를 본다.
    past_time = datetime.now(tz=UTC) - timedelta(days=1)
    db_session.execute(
        update(UserSession).values(expires_at=past_time)
    )
    db_session.commit()

    # ── /auth/me 는 만료 세션을 null 로 취급 ─────────────────────────────────
    me_after = client.get("/auth/me")
    assert me_after.status_code == 200
    assert me_after.json() == {"user": None}

    # ── /login 은 200 + 로그인 폼 렌더 (이미 로그인 시 redirect 분기 회피) ───
    login_response = client.get("/login", follow_redirects=False)
    assert login_response.status_code == 200
    assert 'action="/auth/login"' in login_response.text

    # ── / 목록도 200 + 상단 네비는 로그인/회원가입 링크 ───────────────────────
    list_response = client.get("/")
    assert list_response.status_code == 200
    body = list_response.text
    assert "is-anonymous" in body  # body class 로 비로그인 상태 표시
    assert '/login' in body
    assert '/register' in body


def test_expired_session_detail_page_does_not_create_state(
    client: TestClient,
    db_session: Session,
) -> None:
    """만료된 쿠키로 상세 페이지 진입 시 자동 읽음 UPSERT 가 일어나지 않는다.

    만료 세션은 비로그인과 동일 취급이므로, 사용자 원문의 "비로그인 상세 진입
    시 에러 없음 + 읽음 로직 skip" 규약을 만족해야 한다.
    """
    _register_and_login(
        client, username="expire_detail_user", password="expire_password_1"
    )

    # 공고 1건 준비
    result = upsert_announcement(
        db_session,
        _build_payload(source_announcement_id="SRC-EXP", title="만료 세션 대상"),
    )
    db_session.commit()
    announcement_id = result.announcement.id

    # 세션을 과거로 만료시킨다.
    past_time = datetime.now(tz=UTC) - timedelta(hours=1)
    db_session.execute(update(UserSession).values(expires_at=past_time))
    db_session.commit()

    # 상세 진입 — 200 이지만 user_state 는 생성되지 않아야 한다.
    response = client.get(f"/announcements/{announcement_id}")
    assert response.status_code == 200

    db_session.expire_all()
    user_states = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == announcement_id
        )
    ).scalars().all()
    assert user_states == []


def test_boundary_expired_session_at_now_is_also_anonymous(
    client: TestClient,
    db_session: Session,
) -> None:
    """expires_at == now 경계도 만료로 처리되는지 (<= 조건) 확인."""
    _register_and_login(
        client, username="boundary_user", password="boundary_password_1"
    )

    # expires_at 을 거의 현재 시각으로 당긴다.
    # SQLite 의 datetime 해상도 + Python now() 비교에 여유를 주기 위해
    # 1초 전으로 세팅 (로컬 전제, tz=UTC).
    boundary_time = datetime.now(tz=UTC) - timedelta(seconds=1)
    db_session.execute(update(UserSession).values(expires_at=boundary_time))
    db_session.commit()

    me_response = client.get("/auth/me")
    assert me_response.status_code == 200
    assert me_response.json() == {"user": None}
