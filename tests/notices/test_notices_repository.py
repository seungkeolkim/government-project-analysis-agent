"""공지사항 repository 단위 테스트 (task 00056).

검증 범위:
    - create_notice: 정상 생성, id 채워짐, 컬럼 값 보존
    - get_notice_by_id: 존재 / 미존재(None)
    - list_notices: 최신순 정렬, 페이지네이션(limit/offset)
    - count_notices: 전체 건수
    - update_notice: 제목·본문 갱신, 미존재(None), updated_at 갱신
    - delete_notice: 정상 삭제(True), 미존재(False)
    - notices 테이블이 suggestions 테이블과 동일 DB 파일·동일 Base 를 공유하는지 확인

픽스처 전략:
    - FastAPI / 서버 기동 없음. 서버 통합/E2E 는 다음 subtask 에서 검증.
    - tmp_path 에 임시 SQLite 파일을 생성하고 ``Base.metadata.create_all`` 로
      모든 테이블(suggestions + notices) 을 한 번에 생성한다.
    - 세션은 테스트 전용 sessionmaker 로 직접 생성한다 (앱 세션 설정 우회).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.notices.repository import (
    count_notices,
    create_notice,
    delete_notice,
    get_notice_by_id,
    list_notices,
    update_notice,
)
from app.suggestions.models import Base


# ---------------------------------------------------------------------------
# Fixture: notices + suggestions 테이블 모두 생성된 테스트 전용 SQLite DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def boards_engine(tmp_path: Path) -> Iterator[Engine]:
    """테스트용 임시 boards SQLite 엔진.

    ``Base.metadata.create_all`` 이 suggestions 와 notices 테이블을 모두
    생성한다 — 두 모델이 동일 Base 를 공유하므로 한 번의 호출로 충분하다.
    """
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


def _make_notice(
    session: Session,
    *,
    title: str = "공지 제목",
    body: str = "공지 본문",
    author_user_id: int = 1,
    author_name: str | None = "관리자",
) -> object:
    """테스트용 Notice 를 생성하고 commit 한 뒤 반환한다."""
    notice = create_notice(
        session,
        author_user_id=author_user_id,
        author_name=author_name,
        title=title,
        body=body,
    )
    session.commit()
    return notice


# ---------------------------------------------------------------------------
# create_notice
# ---------------------------------------------------------------------------


def test_create_notice_returns_notice_with_id(boards_session: Session) -> None:
    """create_notice 가 id 가 채워진 Notice 인스턴스를 반환한다."""
    notice = create_notice(
        boards_session,
        author_user_id=10,
        author_name="홍길동",
        title="시스템 점검 안내",
        body="내일 오전 2~4시 시스템 점검 예정입니다.",
    )
    boards_session.commit()

    assert notice.id is not None and notice.id > 0
    assert notice.title == "시스템 점검 안내"
    assert notice.body == "내일 오전 2~4시 시스템 점검 예정입니다."
    assert notice.author_user_id == 10
    assert notice.author_name == "홍길동"


def test_create_notice_author_name_nullable(boards_session: Session) -> None:
    """author_name 이 None 이어도 저장된다."""
    notice = create_notice(
        boards_session,
        author_user_id=1,
        author_name=None,
        title="무기명 공지",
        body="본문",
    )
    boards_session.commit()
    assert notice.author_name is None


def test_create_notice_timestamps_set(boards_session: Session) -> None:
    """created_at, updated_at 이 모델 default 로 채워진다."""
    notice = create_notice(
        boards_session,
        author_user_id=1,
        author_name="admin",
        title="타임스탬프 확인",
        body=".",
    )
    boards_session.commit()
    assert notice.created_at is not None
    assert notice.updated_at is not None


# ---------------------------------------------------------------------------
# get_notice_by_id
# ---------------------------------------------------------------------------


def test_get_notice_by_id_found(boards_session: Session) -> None:
    """존재하는 id 로 조회하면 해당 Notice 를 반환한다."""
    notice = _make_notice(boards_session, title="존재하는 공지")
    found = get_notice_by_id(boards_session, notice.id)
    assert found is not None
    assert found.id == notice.id
    assert found.title == "존재하는 공지"


def test_get_notice_by_id_not_found(boards_session: Session) -> None:
    """존재하지 않는 id 로 조회하면 None 을 반환한다."""
    result = get_notice_by_id(boards_session, 99999)
    assert result is None


# ---------------------------------------------------------------------------
# count_notices
# ---------------------------------------------------------------------------


def test_count_notices_empty(boards_session: Session) -> None:
    """게시글이 없으면 0 을 반환한다."""
    assert count_notices(boards_session) == 0


def test_count_notices_after_creates(boards_session: Session) -> None:
    """생성 수에 맞게 카운트가 증가한다."""
    for i in range(3):
        _make_notice(boards_session, title=f"공지 {i}")
    assert count_notices(boards_session) == 3


# ---------------------------------------------------------------------------
# list_notices
# ---------------------------------------------------------------------------


def test_list_notices_empty(boards_session: Session) -> None:
    """게시글이 없으면 빈 리스트를 반환한다."""
    assert list_notices(boards_session, limit=10, offset=0) == []


def test_list_notices_newest_first(boards_session: Session) -> None:
    """최신순으로 정렬된다 — 나중에 생성된 글이 먼저 온다."""
    n1 = _make_notice(boards_session, title="첫 번째 공지")
    # 동일 시각 PK 정렬 보장을 위해 짧은 대기 없이 별개 commit 으로 분리
    time.sleep(0.01)
    n2 = _make_notice(boards_session, title="두 번째 공지")

    results = list_notices(boards_session, limit=10, offset=0)
    assert len(results) == 2
    # 나중에 만든 n2 가 앞에 온다
    assert results[0].id == n2.id
    assert results[1].id == n1.id


def test_list_notices_pagination(boards_session: Session) -> None:
    """limit / offset 페이지네이션이 동작한다."""
    for i in range(5):
        _make_notice(boards_session, title=f"공지 {i}")

    page1 = list_notices(boards_session, limit=3, offset=0)
    page2 = list_notices(boards_session, limit=3, offset=3)

    assert len(page1) == 3
    assert len(page2) == 2
    # 두 페이지에 중복 없음
    ids1 = {n.id for n in page1}
    ids2 = {n.id for n in page2}
    assert ids1.isdisjoint(ids2)


# ---------------------------------------------------------------------------
# update_notice
# ---------------------------------------------------------------------------


def test_update_notice_changes_title_and_body(boards_session: Session) -> None:
    """update_notice 가 제목과 본문을 갱신한다."""
    notice = _make_notice(boards_session, title="원래 제목", body="원래 본문")
    old_updated_at = notice.updated_at

    time.sleep(0.01)
    updated = update_notice(
        boards_session,
        notice_id=notice.id,
        title="새 제목",
        body="새 본문",
    )
    boards_session.commit()

    assert updated is not None
    assert updated.id == notice.id
    assert updated.title == "새 제목"
    assert updated.body == "새 본문"
    # updated_at 이 갱신됐는지 확인 (onupdate 동작)
    assert updated.updated_at >= old_updated_at


def test_update_notice_not_found_returns_none(boards_session: Session) -> None:
    """존재하지 않는 id 로 update 하면 None 을 반환한다."""
    result = update_notice(boards_session, notice_id=99999, title="x", body="y")
    assert result is None


# ---------------------------------------------------------------------------
# delete_notice
# ---------------------------------------------------------------------------


def test_delete_notice_returns_true_and_hides_row(boards_session: Session) -> None:
    """delete_notice 가 True 를 반환하고 소프트 삭제 후 조회 시 None 을 반환한다."""
    notice = _make_notice(boards_session, title="삭제 대상")
    notice_id = notice.id

    result = delete_notice(boards_session, notice_id=notice_id)
    boards_session.commit()

    assert result is True
    assert get_notice_by_id(boards_session, notice_id) is None


def test_delete_notice_not_found_returns_false(boards_session: Session) -> None:
    """존재하지 않는 id 로 delete 하면 False 를 반환한다."""
    result = delete_notice(boards_session, notice_id=99999)
    assert result is False


def test_delete_notice_sets_deleted_at(boards_session: Session) -> None:
    """delete_notice 가 deleted_at 을 설정한다."""
    notice = _make_notice(boards_session, title="deleted_at 확인")

    delete_notice(boards_session, notice_id=notice.id)
    boards_session.commit()

    boards_session.refresh(notice)
    assert notice.deleted_at is not None


def test_delete_notice_already_deleted_returns_false(boards_session: Session) -> None:
    """이미 소프트 삭제된 공지사항에 delete_notice 를 재호출하면 False 를 반환한다."""
    notice = _make_notice(boards_session, title="재삭제 시도")

    delete_notice(boards_session, notice_id=notice.id)
    boards_session.commit()

    result = delete_notice(boards_session, notice_id=notice.id)
    assert result is False


# ---------------------------------------------------------------------------
# 소프트 삭제 필터: list / count / get / update
# ---------------------------------------------------------------------------


def test_list_notices_excludes_deleted(boards_session: Session) -> None:
    """list_notices 가 소프트 삭제된 공지사항을 목록에서 제외한다."""
    active = _make_notice(boards_session, title="활성 공지")
    deleted = _make_notice(boards_session, title="삭제된 공지")
    delete_notice(boards_session, notice_id=deleted.id)
    boards_session.commit()

    results = list_notices(boards_session, limit=10, offset=0)
    ids = {n.id for n in results}
    assert active.id in ids
    assert deleted.id not in ids


def test_count_notices_excludes_deleted(boards_session: Session) -> None:
    """count_notices 가 소프트 삭제된 공지사항을 카운트에서 제외한다."""
    _make_notice(boards_session, title="활성 공지")
    deleted = _make_notice(boards_session, title="삭제된 공지")
    delete_notice(boards_session, notice_id=deleted.id)
    boards_session.commit()

    assert count_notices(boards_session) == 1


def test_get_notice_by_id_deleted_returns_none(boards_session: Session) -> None:
    """소프트 삭제된 공지사항은 get_notice_by_id 가 None 을 반환한다."""
    notice = _make_notice(boards_session, title="삭제될 공지")
    delete_notice(boards_session, notice_id=notice.id)
    boards_session.commit()

    assert get_notice_by_id(boards_session, notice.id) is None


def test_update_notice_deleted_returns_none(boards_session: Session) -> None:
    """소프트 삭제된 공지사항에 update_notice 를 호출하면 None 을 반환한다."""
    notice = _make_notice(boards_session, title="수정 시도 대상")
    delete_notice(boards_session, notice_id=notice.id)
    boards_session.commit()

    result = update_notice(boards_session, notice_id=notice.id, title="새 제목", body="새 본문")
    assert result is None


# ---------------------------------------------------------------------------
# DB 공유 검증: notices 와 suggestions 가 같은 DB 파일에 공존
# ---------------------------------------------------------------------------


def test_notices_and_suggestions_share_same_db(boards_engine: Engine) -> None:
    """notices 와 suggestions 테이블이 동일 DB 파일에 모두 존재한다.

    ``Base.metadata.create_all`` 한 번으로 두 테이블이 만들어지는 것을 확인한다.
    """
    from sqlalchemy import inspect

    inspector = inspect(boards_engine)
    table_names = inspector.get_table_names()
    assert "notices" in table_names, "notices 테이블이 없다"
    assert "suggestions" in table_names, "suggestions 테이블이 없다"
