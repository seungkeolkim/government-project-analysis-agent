"""건의사항 게시판 repository — 별도 DB 기준 CRUD.

설계 원칙(메인 DB :mod:`app.db.repository` 와 동일):
    - 모든 함수는 호출자가 전달한 ``Session`` 을 그대로 사용한다.
      트랜잭션 경계(commit/rollback)는 호출자가
      :func:`app.suggestions.suggestions_session_scope` 등으로 제어한다.
    - 본 repository 는 ``flush()`` 까지만 수행한다.

본 단계(subtask 00051-2) 범위:
    - :func:`list_suggestions` / :func:`count_suggestions` — 목록 페이지용.
    - :func:`create_suggestion` — POST /suggestions 작성 처리용.

뷰어/댓글/관리자 모달은 후속 subtask 에서 본 모듈에 추가될 예정이며, 본 단계에서는
의존성 노이즈를 줄이기 위해 정의하지 않는다.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.suggestions.models import AcceptanceStatus, Suggestion


def count_suggestions(session: Session) -> int:
    """건의사항 게시글 전체 건수를 반환한다.

    페이지네이션 계산용. 목록 정책상 모든 글을 카운트한다(고아 필터링은 라우트
    단에서 수행 — count 와 표시 row 수는 일치하지 않을 수 있으나, 비관리자에게
    숨기는 row 가 일부 있을 뿐 페이지 계산이 크게 어긋나지 않는다. 정확한
    페이지 수가 중요해지면 후속 subtask 에서 cross-DB join 우회 카운트로 보강).

    Args:
        session: 건의사항 DB ORM 세션.

    Returns:
        ``suggestions`` 테이블의 총 row 수.
    """
    return int(session.execute(select(func.count(Suggestion.id))).scalar_one())


def list_suggestions(
    session: Session,
    *,
    limit: int,
    offset: int,
) -> list[Suggestion]:
    """건의사항 게시글을 최신순(작성시각 내림차순) 으로 페이지 단위 조회한다.

    동일 시각 다중 row 의 결정적 순서 보장을 위해 ``id`` 내림차순을 보조
    정렬키로 둔다.

    Args:
        session: 건의사항 DB ORM 세션.
        limit: 페이지 크기.
        offset: 0-based 시작 오프셋.

    Returns:
        ``Suggestion`` ORM 인스턴스 리스트.
    """
    rows = session.execute(
        select(Suggestion)
        .order_by(Suggestion.created_at.desc(), Suggestion.id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    return list(rows)


def create_suggestion(
    session: Session,
    *,
    author_user_id: int,
    title: str,
    body: str,
    password_hash: str,
    is_secret: bool,
    author_name: str | None,
    contact_email: str | None,
) -> Suggestion:
    """새 건의사항 게시글을 생성한다 (작성 시각/수용 상태는 모델 default 사용).

    트랜잭션 commit 은 호출자가 책임진다. 본 함수는 ``add`` + ``flush`` 까지만
    수행하고, ``Suggestion.id`` 가 채워진 인스턴스를 반환한다.

    Args:
        session: 건의사항 DB ORM 세션.
        author_user_id: 작성자(로그인 사용자)의 메인 DB users.id 값.
            본 단계에서는 작성 자체가 로그인 필수라 항상 정수가 주어진다.
        title: 게시글 제목 (필수).
        body: 게시글 본문 (필수).
        password_hash: 게시글별 비밀번호의 bcrypt 해시 (필수).
        is_secret: 비밀글 여부 (필수).
        author_name: 선택 입력 작성자명. 비어 있으면 None 을 그대로 전달한다.
        contact_email: 선택 입력 연락처 이메일. 비어 있으면 None.

    Returns:
        flush 된 ``Suggestion`` ORM 인스턴스(``id`` 포함).
    """
    suggestion = Suggestion(
        author_user_id=author_user_id,
        title=title,
        body=body,
        password_hash=password_hash,
        is_secret=is_secret,
        author_name=author_name,
        contact_email=contact_email,
        # acceptance_status / created_at / updated_at 는 모델 default 가 채운다.
        # acceptance_status default = AcceptanceStatus.PENDING ("검토중").
        acceptance_status=AcceptanceStatus.PENDING,
    )
    session.add(suggestion)
    session.flush()
    return suggestion


__all__ = [
    "count_suggestions",
    "list_suggestions",
    "create_suggestion",
]
