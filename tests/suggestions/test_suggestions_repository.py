"""건의사항·댓글 repository 소프트 삭제 단위 테스트 (task 00069-2).

검증 범위:
    - list_suggestions: 소프트 삭제된 게시글은 목록에 포함되지 않는다.
    - count_suggestions: 소프트 삭제된 게시글은 카운트에 포함되지 않는다.
    - get_suggestion_by_id: 소프트 삭제된 게시글은 None 을 반환한다.
    - list_comments_by_suggestion_id: 소프트 삭제된 댓글은 목록에 포함되지 않는다.
    - count_comments_by_suggestion_ids: 소프트 삭제된 댓글은 카운트에 포함되지 않는다.
    - delete_suggestion: deleted_at 을 기록하고 True 를 반환한다.
    - delete_suggestion: 소속 댓글도 동일 타임스탬프로 소프트 삭제한다.
    - delete_suggestion: 이미 소프트 삭제된 게시글에 대해 False 를 반환한다.
    - delete_suggestion_comment: deleted_at 을 기록하고 True 를 반환한다.
    - delete_suggestion_comment: 이미 소프트 삭제된 댓글에 대해 False 를 반환한다.
    - get_comment_by_id: 소프트 삭제된 댓글은 None 을 반환한다.
    - update_suggestion: 소프트 삭제된 게시글에 대해 None 을 반환한다.
    - update_suggestion_acceptance: 소프트 삭제된 게시글에 대해 None 을 반환한다.
    - update_suggestion_comment: 소프트 삭제된 댓글에 대해 None 을 반환한다.

픽스처 전략:
    - FastAPI / 서버 기동 없음.
    - tmp_path 에 임시 SQLite 파일을 생성하고 ``Base.metadata.create_all`` 로
      테이블을 한 번에 생성한다.
    - 세션은 테스트 전용 sessionmaker 로 직접 생성한다 (앱 세션 설정 우회).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.suggestions.models import AcceptanceStatus, Base
from app.suggestions.repository import (
    count_comments_by_suggestion_ids,
    count_suggestions,
    create_suggestion,
    create_suggestion_comment,
    delete_suggestion,
    delete_suggestion_comment,
    get_comment_by_id,
    get_suggestion_by_id,
    list_comments_by_suggestion_id,
    list_suggestions,
    update_suggestion,
    update_suggestion_acceptance,
    update_suggestion_comment,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def boards_engine(tmp_path: Path) -> Iterator[Engine]:
    """테스트용 임시 boards SQLite 엔진."""
    db_file = tmp_path / "boards_test.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_file}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def boards_session(boards_engine: Engine) -> Iterator[Session]:
    """테스트용 boards DB 세션. 각 테스트 종료 시 rollback + close."""
    factory = sessionmaker(
        bind=boards_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    session = factory()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _make_suggestion(
    session: Session,
    *,
    title: str = "건의 제목",
    body: str = "건의 본문",
    author_user_id: int = 1,
) -> object:
    """테스트용 Suggestion 을 생성하고 commit 한 뒤 반환한다."""
    suggestion = create_suggestion(
        session,
        author_user_id=author_user_id,
        title=title,
        body=body,
        password_hash="dummy_hash",
        is_secret=False,
        author_name="테스터",
        contact_email=None,
    )
    session.commit()
    return suggestion


def _make_comment(
    session: Session,
    *,
    suggestion_id: int,
    body: str = "댓글 본문",
    author_user_id: int = 1,
) -> object:
    """테스트용 SuggestionComment 를 생성하고 commit 한 뒤 반환한다."""
    comment = create_suggestion_comment(
        session,
        suggestion_id=suggestion_id,
        author_user_id=author_user_id,
        body=body,
    )
    session.commit()
    return comment


# ---------------------------------------------------------------------------
# delete_suggestion: 소프트 삭제 동작
# ---------------------------------------------------------------------------


def test_delete_suggestion_sets_deleted_at(boards_session: Session) -> None:
    """delete_suggestion 이 게시글의 deleted_at 을 설정하고 True 를 반환한다."""
    suggestion = _make_suggestion(boards_session, title="삭제 대상")

    result = delete_suggestion(boards_session, suggestion_id=suggestion.id)
    boards_session.commit()

    assert result is True
    # ORM 인스턴스 refresh 로 DB 값 확인
    boards_session.refresh(suggestion)
    assert suggestion.deleted_at is not None


def test_delete_suggestion_cascades_to_comments(boards_session: Session) -> None:
    """delete_suggestion 이 소속 댓글도 동일 타임스탬프로 소프트 삭제한다."""
    suggestion = _make_suggestion(boards_session, title="댓글 있는 게시글")
    comment1 = _make_comment(boards_session, suggestion_id=suggestion.id, body="댓글1")
    comment2 = _make_comment(boards_session, suggestion_id=suggestion.id, body="댓글2")

    delete_suggestion(boards_session, suggestion_id=suggestion.id)
    boards_session.commit()

    boards_session.refresh(comment1)
    boards_session.refresh(comment2)
    boards_session.refresh(suggestion)

    # 댓글들의 deleted_at 이 게시글과 동일한 타임스탬프여야 한다.
    assert comment1.deleted_at is not None
    assert comment2.deleted_at is not None
    assert comment1.deleted_at == suggestion.deleted_at
    assert comment2.deleted_at == suggestion.deleted_at


def test_delete_suggestion_already_deleted_returns_false(boards_session: Session) -> None:
    """이미 소프트 삭제된 게시글에 delete_suggestion 을 호출하면 False 를 반환한다."""
    suggestion = _make_suggestion(boards_session, title="재삭제 시도")

    delete_suggestion(boards_session, suggestion_id=suggestion.id)
    boards_session.commit()

    # 두 번째 호출 — 이미 deleted_at 이 설정되어 있으므로 False
    result = delete_suggestion(boards_session, suggestion_id=suggestion.id)
    assert result is False


def test_delete_suggestion_not_found_returns_false(boards_session: Session) -> None:
    """존재하지 않는 id 로 delete_suggestion 을 호출하면 False 를 반환한다."""
    result = delete_suggestion(boards_session, suggestion_id=99999)
    assert result is False


# ---------------------------------------------------------------------------
# delete_suggestion_comment: 소프트 삭제 동작
# ---------------------------------------------------------------------------


def test_delete_suggestion_comment_sets_deleted_at(boards_session: Session) -> None:
    """delete_suggestion_comment 가 댓글의 deleted_at 을 설정하고 True 를 반환한다."""
    suggestion = _make_suggestion(boards_session)
    comment = _make_comment(boards_session, suggestion_id=suggestion.id)

    result = delete_suggestion_comment(boards_session, comment_id=comment.id)
    boards_session.commit()

    assert result is True
    boards_session.refresh(comment)
    assert comment.deleted_at is not None


def test_delete_suggestion_comment_already_deleted_returns_false(
    boards_session: Session,
) -> None:
    """이미 소프트 삭제된 댓글에 delete_suggestion_comment 를 호출하면 False 를 반환한다."""
    suggestion = _make_suggestion(boards_session)
    comment = _make_comment(boards_session, suggestion_id=suggestion.id)

    delete_suggestion_comment(boards_session, comment_id=comment.id)
    boards_session.commit()

    result = delete_suggestion_comment(boards_session, comment_id=comment.id)
    assert result is False


def test_delete_suggestion_comment_not_found_returns_false(
    boards_session: Session,
) -> None:
    """존재하지 않는 id 로 delete_suggestion_comment 를 호출하면 False 를 반환한다."""
    result = delete_suggestion_comment(boards_session, comment_id=99999)
    assert result is False


# ---------------------------------------------------------------------------
# get_suggestion_by_id: 소프트 삭제 필터
# ---------------------------------------------------------------------------


def test_get_suggestion_by_id_active_found(boards_session: Session) -> None:
    """소프트 삭제되지 않은 게시글은 정상 조회된다."""
    suggestion = _make_suggestion(boards_session, title="활성 게시글")
    found = get_suggestion_by_id(boards_session, suggestion.id)
    assert found is not None
    assert found.id == suggestion.id


def test_get_suggestion_by_id_deleted_returns_none(boards_session: Session) -> None:
    """소프트 삭제된 게시글은 None 을 반환한다."""
    suggestion = _make_suggestion(boards_session, title="삭제될 게시글")
    delete_suggestion(boards_session, suggestion_id=suggestion.id)
    boards_session.commit()

    result = get_suggestion_by_id(boards_session, suggestion.id)
    assert result is None


# ---------------------------------------------------------------------------
# list_suggestions: 소프트 삭제 필터
# ---------------------------------------------------------------------------


def test_list_suggestions_excludes_deleted(boards_session: Session) -> None:
    """list_suggestions 가 소프트 삭제된 게시글을 목록에서 제외한다."""
    active = _make_suggestion(boards_session, title="활성 게시글")
    deleted = _make_suggestion(boards_session, title="삭제된 게시글")
    delete_suggestion(boards_session, suggestion_id=deleted.id)
    boards_session.commit()

    results = list_suggestions(boards_session, limit=10, offset=0)
    ids = {s.id for s in results}
    assert active.id in ids
    assert deleted.id not in ids


# ---------------------------------------------------------------------------
# count_suggestions: 소프트 삭제 필터
# ---------------------------------------------------------------------------


def test_count_suggestions_excludes_deleted(boards_session: Session) -> None:
    """count_suggestions 가 소프트 삭제된 게시글을 카운트에서 제외한다."""
    _make_suggestion(boards_session, title="활성 1")
    deleted = _make_suggestion(boards_session, title="삭제됨")
    delete_suggestion(boards_session, suggestion_id=deleted.id)
    boards_session.commit()

    assert count_suggestions(boards_session) == 1


# ---------------------------------------------------------------------------
# list_comments_by_suggestion_id: 소프트 삭제 필터
# ---------------------------------------------------------------------------


def test_list_comments_excludes_deleted(boards_session: Session) -> None:
    """list_comments_by_suggestion_id 가 소프트 삭제된 댓글을 목록에서 제외한다."""
    suggestion = _make_suggestion(boards_session)
    active_comment = _make_comment(boards_session, suggestion_id=suggestion.id, body="활성 댓글")
    deleted_comment = _make_comment(boards_session, suggestion_id=suggestion.id, body="삭제된 댓글")
    delete_suggestion_comment(boards_session, comment_id=deleted_comment.id)
    boards_session.commit()

    results = list_comments_by_suggestion_id(boards_session, suggestion_id=suggestion.id)
    ids = {c.id for c in results}
    assert active_comment.id in ids
    assert deleted_comment.id not in ids


# ---------------------------------------------------------------------------
# count_comments_by_suggestion_ids: 소프트 삭제 필터
# ---------------------------------------------------------------------------


def test_count_comments_excludes_deleted(boards_session: Session) -> None:
    """count_comments_by_suggestion_ids 가 소프트 삭제된 댓글을 카운트에서 제외한다."""
    suggestion = _make_suggestion(boards_session)
    _make_comment(boards_session, suggestion_id=suggestion.id, body="활성 댓글")
    deleted_comment = _make_comment(boards_session, suggestion_id=suggestion.id, body="삭제된 댓글")
    delete_suggestion_comment(boards_session, comment_id=deleted_comment.id)
    boards_session.commit()

    count_map = count_comments_by_suggestion_ids(boards_session, [suggestion.id])
    assert count_map.get(suggestion.id, 0) == 1


# ---------------------------------------------------------------------------
# get_comment_by_id: 소프트 삭제 필터
# ---------------------------------------------------------------------------


def test_get_comment_by_id_active_found(boards_session: Session) -> None:
    """소프트 삭제되지 않은 댓글은 정상 조회된다."""
    suggestion = _make_suggestion(boards_session)
    comment = _make_comment(boards_session, suggestion_id=suggestion.id)
    found = get_comment_by_id(boards_session, comment.id)
    assert found is not None
    assert found.id == comment.id


def test_get_comment_by_id_deleted_returns_none(boards_session: Session) -> None:
    """소프트 삭제된 댓글은 None 을 반환한다."""
    suggestion = _make_suggestion(boards_session)
    comment = _make_comment(boards_session, suggestion_id=suggestion.id)
    delete_suggestion_comment(boards_session, comment_id=comment.id)
    boards_session.commit()

    result = get_comment_by_id(boards_session, comment.id)
    assert result is None


# ---------------------------------------------------------------------------
# update_suggestion: 소프트 삭제된 게시글 → None 반환
# ---------------------------------------------------------------------------


def test_update_suggestion_deleted_returns_none(boards_session: Session) -> None:
    """update_suggestion 이 소프트 삭제된 게시글에 대해 None 을 반환한다."""
    suggestion = _make_suggestion(boards_session, title="수정 시도 대상")
    delete_suggestion(boards_session, suggestion_id=suggestion.id)
    boards_session.commit()

    result = update_suggestion(
        boards_session,
        suggestion_id=suggestion.id,
        title="새 제목",
        body="새 본문",
        is_secret=False,
    )
    assert result is None


# ---------------------------------------------------------------------------
# update_suggestion_acceptance: 소프트 삭제된 게시글 → None 반환
# ---------------------------------------------------------------------------


def test_update_suggestion_acceptance_deleted_returns_none(boards_session: Session) -> None:
    """update_suggestion_acceptance 가 소프트 삭제된 게시글에 대해 None 을 반환한다."""
    suggestion = _make_suggestion(boards_session)
    delete_suggestion(boards_session, suggestion_id=suggestion.id)
    boards_session.commit()

    result = update_suggestion_acceptance(
        boards_session,
        suggestion_id=suggestion.id,
        acceptance_status=AcceptanceStatus.ACCEPTED,
        acceptance_reason="수용",
        expected_completion_date=None,
    )
    assert result is None


# ---------------------------------------------------------------------------
# update_suggestion_comment: 소프트 삭제된 댓글 → None 반환
# ---------------------------------------------------------------------------


def test_update_suggestion_comment_deleted_returns_none(boards_session: Session) -> None:
    """update_suggestion_comment 가 소프트 삭제된 댓글에 대해 None 을 반환한다."""
    suggestion = _make_suggestion(boards_session)
    comment = _make_comment(boards_session, suggestion_id=suggestion.id)
    delete_suggestion_comment(boards_session, comment_id=comment.id)
    boards_session.commit()

    result = update_suggestion_comment(
        boards_session,
        comment_id=comment.id,
        body="수정 시도",
    )
    assert result is None
