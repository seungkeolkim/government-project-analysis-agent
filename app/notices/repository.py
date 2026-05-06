"""공지사항 게시판 repository — boards DB 기준 CRUD (task 00056).

설계 원칙:
    - 모든 함수는 호출자가 전달한 ``Session`` 을 그대로 사용한다.
      트랜잭션 경계(commit/rollback)는 호출자가 제어한다.
    - 본 repository 는 ``flush()`` 까지만 수행한다.
    - 세션은 ``SuggestionsSessionLocal()`` 로 생성된 인스턴스를 그대로 쓴다.
      (건의사항과 동일 DB 파일, 동일 엔진 공유)

함수 목록:
    - :func:`get_notice_by_id` — 단일 row 조회 (상세 페이지용)
    - :func:`count_notices` — 전체 건수 (페이지네이션용)
    - :func:`list_notices` — 최신순 페이지 조회
    - :func:`create_notice` — 신규 공지사항 작성 (관리자 전용)
    - :func:`update_notice` — 제목·본문 수정 (관리자 전용)
    - :func:`delete_notice` — 삭제 (관리자 전용)
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.notices.models import Notice
from app.suggestions.models import _utcnow


def get_notice_by_id(session: Session, notice_id: int) -> Notice | None:
    """주어진 PK 의 ``Notice`` row 를 조회한다. 없으면 ``None`` 반환.

    라우트에서 단일 row 조회 + 404 분기에 사용한다.

    Args:
        session: boards DB ORM 세션.
        notice_id: ``notices.id`` 값.

    Returns:
        해당 row 의 ``Notice`` 인스턴스 또는 ``None``.
    """
    return session.execute(
        select(Notice).where(
            Notice.id == notice_id,
            Notice.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def count_notices(session: Session) -> int:
    """공지사항 전체 건수를 반환한다.

    페이지네이션 계산용.

    Args:
        session: boards DB ORM 세션.

    Returns:
        ``notices`` 테이블의 총 row 수.
    """
    return int(
        session.execute(
            select(func.count(Notice.id)).where(Notice.deleted_at.is_(None))
        ).scalar_one()
    )


def list_notices(
    session: Session,
    *,
    limit: int,
    offset: int,
) -> list[Notice]:
    """공지사항을 최신순(작성시각 내림차순) 으로 페이지 단위 조회한다.

    동일 시각 다중 row 의 결정적 순서 보장을 위해 ``id`` 내림차순을 보조
    정렬키로 둔다.

    Args:
        session: boards DB ORM 세션.
        limit: 페이지 크기.
        offset: 0-based 시작 오프셋.

    Returns:
        ``Notice`` ORM 인스턴스 리스트.
    """
    rows = (
        session.execute(
            select(Notice)
            .where(Notice.deleted_at.is_(None))
            .order_by(Notice.created_at.desc(), Notice.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(rows)


def create_notice(
    session: Session,
    *,
    author_user_id: int,
    author_name: str | None,
    title: str,
    body: str,
) -> Notice:
    """새 공지사항 게시글을 생성한다.

    트랜잭션 commit 은 호출자가 책임진다. ``add`` + ``flush`` 까지만 수행하고,
    ``Notice.id`` 가 채워진 인스턴스를 반환한다.

    공지사항 작성은 관리자 전용 — 본 함수는 권한 검사를 하지 않으며 라우트에서
    보장한다.

    Args:
        session: boards DB ORM 세션.
        author_user_id: 작성자(관리자) 의 메인 DB users.id 값.
        author_name: 작성 시점 사용자명. 이후 메인 DB 변경에 무관하게 표기 보존.
        title: 공지사항 제목 (필수).
        body: 공지사항 본문 (필수).

    Returns:
        flush 된 ``Notice`` ORM 인스턴스(``id`` 포함).
    """
    notice = Notice(
        author_user_id=author_user_id,
        author_name=author_name,
        title=title,
        body=body,
        # created_at 은 모델 default(_utcnow) 가 채운다.
        # updated_at 은 INSERT 시 NULL (수정 이력 없음 초기값).
    )
    session.add(notice)
    session.flush()
    return notice


def update_notice(
    session: Session,
    *,
    notice_id: int,
    title: str,
    body: str,
) -> Notice | None:
    """공지사항 게시글의 제목과 본문을 in-place 갱신한다.

    관리자 전용. 트랜잭션 commit 은 호출자 책임이다. ``flush()`` 만 호출하며,
    ``updated_at`` 은 모델 ``onupdate`` 가 자동으로 갱신한다.

    Args:
        session: boards DB ORM 세션.
        notice_id: 갱신 대상 공지사항 PK.
        title: 새 제목 (필수).
        body: 새 본문 (필수).

    Returns:
        갱신된 ``Notice`` 인스턴스. 게시글이 존재하지 않으면 ``None`` 반환.
    """
    notice = session.execute(
        select(Notice).where(
            Notice.id == notice_id,
            Notice.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if notice is None:
        return None
    notice.title = title
    notice.body = body
    session.flush()
    return notice


def delete_notice(session: Session, *, notice_id: int) -> bool:
    """공지사항 게시글을 삭제한다.

    관리자 전용. 트랜잭션 commit 은 호출자 책임. ``deleted_at`` 을 현재 시각으로
    설정하는 소프트 삭제이며 ``flush()`` 까지만 수행한다.

    Args:
        session: boards DB ORM 세션.
        notice_id: 삭제 대상 공지사항 PK.

    Returns:
        실제로 삭제됐으면 ``True``, 이미 존재하지 않았다면 ``False``.
    """
    notice = session.execute(
        select(Notice).where(
            Notice.id == notice_id,
            Notice.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if notice is None:
        return False
    notice.deleted_at = _utcnow()
    session.flush()
    return True


__all__ = [
    "get_notice_by_id",
    "count_notices",
    "list_notices",
    "create_notice",
    "update_notice",
    "delete_notice",
]
