"""관리자 건의사항 삭제 권한 통합 테스트 (task 00081-1).

검증 범위:
- 관리자 세션이 타인이 작성한 글(평문/비밀글/고아) 삭제 → 303 + DB 소프트 삭제 확인
- 비관리자 비작성자 세션 → 403
- 작성자 본인 비관리자 세션 → 303 (기존 동작 보존)
- 관리자 세션이 타인 글 edit GET/POST → 403 (수정 라우트 회귀 방지)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.suggestions.models import Base as SuggestionsBase
from app.suggestions.repository import create_suggestion, get_suggestion_by_id


# ──────────────────────────────────────────────────────────────
# DB 픽스처 (건의사항 전용 DB)
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def _test_suggestions_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """건의사항 DB URL 을 tmp_path 의 임시 파일로 치환하고 환경변수에 주입한다."""
    db_file = tmp_path / "boards_test.sqlite3"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("SUGGESTIONS_DB_URL", db_url)
    return db_url


@pytest.fixture
def suggestions_test_engine(
    _test_suggestions_db_url: str,
    test_engine: Engine,
) -> Iterator[Engine]:
    """격리된 건의사항 SQLite 엔진.

    test_engine 이 메인 DB 캐시를 이미 설정한 뒤에 실행되어, get_settings 캐시를
    한 번 더 비워 SUGGESTIONS_DB_URL 이 재로드되게 한다.
    """
    from app.config import get_settings
    from app.suggestions.session import get_suggestions_engine, reset_suggestions_engine_cache

    get_settings.cache_clear()
    reset_suggestions_engine_cache()

    engine = get_suggestions_engine()
    SuggestionsBase.metadata.create_all(engine)

    try:
        yield engine
    finally:
        engine.dispose()
        reset_suggestions_engine_cache()
        get_settings.cache_clear()


@pytest.fixture
def suggestions_session(suggestions_test_engine: Engine) -> Iterator[Session]:
    """테스트용 건의사항 DB 세션. 테스트 종료 시 close."""
    factory = sessionmaker(
        bind=suggestions_test_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────
# 앱 클라이언트 픽스처
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def client(suggestions_test_engine: Engine) -> Iterator[TestClient]:
    """메인 DB + 건의사항 DB 가 모두 격리된 TestClient."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


# ──────────────────────────────────────────────────────────────
# 유저 헬퍼
# ──────────────────────────────────────────────────────────────


def _register(client: TestClient, username: str, password: str) -> None:
    """회원가입 후 TestClient 쿠키에 세션을 남긴다."""
    resp = client.post(
        "/auth/register",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"회원가입 실패: {resp.status_code}"


def _login(client: TestClient, username: str, password: str) -> None:
    """로그인 후 TestClient 쿠키에 세션을 남긴다."""
    resp = client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"로그인 실패: {resp.status_code}"


# ──────────────────────────────────────────────────────────────
# 사용자 픽스처
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def author_user_id(db_session: Session) -> int:
    """건의사항 작성자로 쓸 일반 사용자의 user_id 를 반환한다."""
    from app.auth.service import create_user

    user = create_user(db_session, username="post_author", password="Author_pass_1!")
    db_session.commit()
    return user.id


@pytest.fixture
def admin_client(client: TestClient, db_session: Session) -> TestClient:
    """관리자(is_admin=True)로 로그인된 TestClient 를 반환한다."""
    from app.auth.service import create_user

    create_user(db_session, username="admin_user", password="Admin_pass_1!", is_admin=True)
    db_session.commit()

    _login(client, "admin_user", "Admin_pass_1!")
    return client


# ──────────────────────────────────────────────────────────────
# 건의사항 픽스처
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def plain_suggestion_id(suggestions_session: Session, author_user_id: int) -> int:
    """타 사용자가 작성한 평문 건의사항 ID."""
    s = create_suggestion(
        suggestions_session,
        author_user_id=author_user_id,
        title="평문 건의",
        body="평문 본문",
        password_hash="dummy_hash",
        is_secret=False,
        author_name="post_author",
        contact_email=None,
    )
    suggestions_session.commit()
    return s.id


@pytest.fixture
def secret_suggestion_id(suggestions_session: Session, author_user_id: int) -> int:
    """타 사용자가 작성한 비밀글 건의사항 ID."""
    s = create_suggestion(
        suggestions_session,
        author_user_id=author_user_id,
        title="비밀 건의",
        body="비밀 본문",
        password_hash="dummy_hash",
        is_secret=True,
        author_name="post_author",
        contact_email=None,
    )
    suggestions_session.commit()
    return s.id


@pytest.fixture
def orphan_suggestion_id(suggestions_session: Session) -> int:
    """작성자가 메인 DB 에 존재하지 않는 고아 건의사항 ID."""
    s = create_suggestion(
        suggestions_session,
        author_user_id=99999,  # 메인 DB 에 없는 ID → 고아 글
        title="고아 건의",
        body="고아 본문",
        password_hash="dummy_hash",
        is_secret=False,
        author_name="사라진 사용자",
        contact_email=None,
    )
    suggestions_session.commit()
    return s.id


# ──────────────────────────────────────────────────────────────
# 테스트: 관리자 삭제 성공 (303 + DB 소프트 삭제)
# ──────────────────────────────────────────────────────────────


def test_admin_can_delete_plain_suggestion(
    admin_client: TestClient,
    plain_suggestion_id: int,
    suggestions_session: Session,
) -> None:
    """관리자는 타인이 작성한 평문 건의사항을 삭제할 수 있어야 한다."""
    resp = admin_client.post(
        f"/suggestions/{plain_suggestion_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # DB 소프트 삭제 확인 — get_suggestion_by_id 는 deleted_at IS NULL 조건으로 조회
    suggestions_session.expire_all()
    assert get_suggestion_by_id(suggestions_session, plain_suggestion_id) is None


def test_admin_can_delete_secret_suggestion(
    admin_client: TestClient,
    secret_suggestion_id: int,
    suggestions_session: Session,
) -> None:
    """관리자는 타인이 작성한 비밀글 건의사항도 삭제할 수 있어야 한다."""
    resp = admin_client.post(
        f"/suggestions/{secret_suggestion_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    suggestions_session.expire_all()
    assert get_suggestion_by_id(suggestions_session, secret_suggestion_id) is None


def test_admin_can_delete_orphan_suggestion(
    admin_client: TestClient,
    orphan_suggestion_id: int,
    suggestions_session: Session,
) -> None:
    """관리자는 고아(작성자가 사라진) 건의사항도 삭제할 수 있어야 한다."""
    resp = admin_client.post(
        f"/suggestions/{orphan_suggestion_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    suggestions_session.expire_all()
    assert get_suggestion_by_id(suggestions_session, orphan_suggestion_id) is None


# ──────────────────────────────────────────────────────────────
# 테스트: 비관리자 비작성자 → 403
# ──────────────────────────────────────────────────────────────


def test_non_admin_non_author_cannot_delete(
    client: TestClient,
    plain_suggestion_id: int,
) -> None:
    """비관리자 비작성자 세션은 타인의 건의사항 삭제 시 403 이어야 한다."""
    _register(client, "stranger_user", "Stranger_pass_1!")

    resp = client.post(
        f"/suggestions/{plain_suggestion_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────
# 테스트: 작성자 본인 비관리자 → 303 (기존 동작 보존)
# ──────────────────────────────────────────────────────────────


def test_author_can_delete_own_suggestion(
    client: TestClient,
    plain_suggestion_id: int,
) -> None:
    """작성자 본인(비관리자) 세션은 자신의 건의사항을 삭제할 수 있어야 한다."""
    # author_user_id 픽스처가 post_author 계정을 생성했으므로 그대로 로그인한다.
    _login(client, "post_author", "Author_pass_1!")

    resp = client.post(
        f"/suggestions/{plain_suggestion_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 303


# ──────────────────────────────────────────────────────────────
# 테스트: 수정 라우트 회귀 — 관리자도 비작성자이면 403
# ──────────────────────────────────────────────────────────────


def test_admin_cannot_edit_others_suggestion_get(
    admin_client: TestClient,
    plain_suggestion_id: int,
) -> None:
    """관리자라도 타인의 건의사항 수정 폼(GET)에 접근하면 403 이어야 한다."""
    resp = admin_client.get(
        f"/suggestions/{plain_suggestion_id}/edit",
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_admin_cannot_edit_others_suggestion_post(
    admin_client: TestClient,
    plain_suggestion_id: int,
) -> None:
    """관리자라도 타인의 건의사항 수정(POST)을 시도하면 403 이어야 한다."""
    resp = admin_client.post(
        f"/suggestions/{plain_suggestion_id}/edit",
        data={"title": "수정 시도", "body": "수정 본문"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
