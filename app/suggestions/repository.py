"""건의사항 게시판 repository — 별도 DB 기준 CRUD.

설계 원칙(메인 DB :mod:`app.db.repository` 와 동일):
    - 모든 함수는 호출자가 전달한 ``Session`` 을 그대로 사용한다.
      트랜잭션 경계(commit/rollback)는 호출자가
      :func:`app.suggestions.suggestions_session_scope` 등으로 제어한다.
    - 본 repository 는 ``flush()`` 까지만 수행한다.

본 모듈 범위:
    - :func:`list_suggestions` / :func:`count_suggestions` — 목록 페이지용 (00051-2).
    - :func:`create_suggestion` — POST /suggestions 작성 처리용 (00051-2).
    - :func:`get_suggestion_by_id` — GET /suggestions/{id} 뷰어 페이지용 (00051-3).
    - :func:`list_comments_by_suggestion_id` — 뷰어 하단 댓글 목록 조회 (00051-4).
    - :func:`create_suggestion_comment` — POST 댓글 작성 처리용 (00051-4).
    - :func:`update_suggestion_acceptance` — 관리자 수용 여부 모달 저장 (00051-5).
    - :func:`update_suggestion` / :func:`delete_suggestion` — 작성자 본인의 글
      수정·삭제 (00052-4).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.suggestions.models import AcceptanceStatus, Suggestion, SuggestionComment


def get_suggestion_by_id(session: Session, suggestion_id: int) -> Suggestion | None:
    """주어진 PK 의 ``Suggestion`` row 를 조회한다. 없으면 ``None`` 반환.

    뷰어 라우트에서 단일 row 조회 + 404 분기에 사용한다. 라우트는 본 함수가
    ``None`` 을 반환하면 ``HTTPException(404)`` 를 던지고, 인스턴스를 받으면
    이어서 (a) 고아 게이트 → (b) 비밀글 게이트 순서로 권한 검사를 수행한다.

    Args:
        session: 건의사항 DB ORM 세션.
        suggestion_id: ``suggestions.id`` 값.

    Returns:
        해당 row 의 ``Suggestion`` 인스턴스 또는 ``None``.
    """
    return session.get(Suggestion, suggestion_id)


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


def list_comments_by_suggestion_id(
    session: Session,
    *,
    suggestion_id: int,
) -> list[SuggestionComment]:
    """주어진 게시글의 댓글을 작성 시각 오름차순(오래된 것 먼저) 으로 반환한다.

    오래된 댓글 → 최신 댓글 순서는 \"대화의 흐름\" 을 자연스럽게 따라가는
    Q&A 게시판 전형 패턴이다. 동일 시각 다중 row 의 결정적 순서 보장을 위해
    ``id`` 오름차순을 보조 정렬키로 둔다.

    Args:
        session: 건의사항 DB ORM 세션.
        suggestion_id: 부모 게시글 PK.

    Returns:
        ``SuggestionComment`` ORM 인스턴스 리스트. 댓글이 없으면 빈 리스트.
    """
    rows = session.execute(
        select(SuggestionComment)
        .where(SuggestionComment.suggestion_id == suggestion_id)
        .order_by(SuggestionComment.created_at.asc(), SuggestionComment.id.asc())
    ).scalars().all()
    return list(rows)


def create_suggestion_comment(
    session: Session,
    *,
    suggestion_id: int,
    author_user_id: int,
    body: str,
) -> SuggestionComment:
    """새 댓글을 생성한다.

    트랜잭션 commit 은 호출자 책임. ``add`` + ``flush`` 까지만 수행해 ``id`` 가
    채워진 인스턴스를 반환한다. 댓글 작성 자체는 로그인 필수이므로
    ``author_user_id`` 는 항상 정수가 주어진다(라우트에서 보장).

    Args:
        session: 건의사항 DB ORM 세션.
        suggestion_id: 부모 게시글 PK.
        author_user_id: 작성자(로그인 사용자) 의 메인 DB users.id 값.
        body: 댓글 본문 (필수).

    Returns:
        flush 된 ``SuggestionComment`` ORM 인스턴스.
    """
    comment = SuggestionComment(
        suggestion_id=suggestion_id,
        author_user_id=author_user_id,
        body=body,
        # created_at 은 모델 default(_utcnow) 가 채운다.
    )
    session.add(comment)
    session.flush()
    return comment


def update_suggestion_acceptance(
    session: Session,
    *,
    suggestion_id: int,
    acceptance_status: AcceptanceStatus,
    acceptance_reason: str | None,
    expected_completion_date: date | None,
) -> Suggestion | None:
    """관리자 수용 여부 모달 저장 — 게시글의 수용 관련 3 필드를 한 번에 갱신한다.

    사용자 원문: \"수용 여부 체크/수정 이라는 버튼과 모달을 만들어서 한 번에 입력하도록 하자\".

    각 필드 의미:
        - ``acceptance_status``: 수용 / 일부 수용 / 거절 / 검토중. 라우트 단에서
          AcceptanceStatus enum 으로 정규화된 값이 들어온다.
        - ``acceptance_reason``: 관리자 사유. 빈 입력은 ``None`` 으로 정규화 권장
          (라우트 책임). 길이 제한도 라우트 책임.
        - ``expected_completion_date``: 수용/일부수용 시에만 의미 있다. 거절/검토중
          이 들어오면 라우트가 ``None`` 으로 강제해 본 함수에 넘긴다.

    트랜잭션 commit 은 호출자가 책임진다. ``add`` 가 아니라 기존 row 의 in-place
    update 라 ``flush()`` 만 호출한다. ``updated_at`` 은 모델 ``onupdate`` 가
    자동으로 갱신한다.

    Args:
        session: 건의사항 DB ORM 세션.
        suggestion_id: 갱신 대상 게시글 PK.
        acceptance_status: 새 수용 상태.
        acceptance_reason: 새 사유 (또는 ``None``).
        expected_completion_date: 새 예상 완료일 (또는 ``None``).

    Returns:
        갱신된 ``Suggestion`` 인스턴스. 게시글이 존재하지 않으면 ``None`` 을
        반환해 호출자가 404 분기를 책임지도록 한다.
    """
    suggestion = session.get(Suggestion, suggestion_id)
    if suggestion is None:
        return None
    suggestion.acceptance_status = acceptance_status
    suggestion.acceptance_reason = acceptance_reason
    suggestion.expected_completion_date = expected_completion_date
    session.flush()
    return suggestion


def update_suggestion(
    session: Session,
    *,
    suggestion_id: int,
    title: str,
    body: str,
    is_secret: bool,
) -> Suggestion | None:
    """건의사항 게시글의 작성자 수정 가능 필드를 in-place 갱신한다 (00052-4).

    작성자 본인이 자기 글을 수정하는 흐름 전용 — 관리자 수용 관련 필드
    (acceptance_status / acceptance_reason / expected_completion_date) 는 본
    함수의 책임이 아니며 :func:`update_suggestion_acceptance` 가 따로 다룬다.
    작성자명 / 연락처 이메일 / 비밀번호 / 작성자 식별자(``author_user_id``) 는
    수정 범위 밖이라 본 함수가 건드리지 않는다.

    트랜잭션 commit 은 호출자(라우트) 책임이다. ``flush()`` 만 호출하며,
    ``updated_at`` 은 모델 ``onupdate`` 가 자동으로 갱신한다.

    Args:
        session: 건의사항 DB ORM 세션.
        suggestion_id: 갱신 대상 게시글 PK.
        title: 새 제목 (필수).
        body: 새 본문 (필수).
        is_secret: 새 비밀글 여부.

    Returns:
        갱신된 ``Suggestion`` 인스턴스. 게시글이 존재하지 않으면 ``None`` 을
        반환해 호출자가 404 분기를 책임지도록 한다.
    """
    suggestion = session.get(Suggestion, suggestion_id)
    if suggestion is None:
        return None
    suggestion.title = title
    suggestion.body = body
    suggestion.is_secret = is_secret
    session.flush()
    return suggestion


def delete_suggestion(session: Session, *, suggestion_id: int) -> bool:
    """건의사항 게시글을 삭제한다 (00052-4).

    소속 댓글은 모델의 ``cascade=\"all, delete-orphan\"`` + DB 레벨
    ``ON DELETE CASCADE`` 로 함께 정리된다. 트랜잭션 commit 은 호출자 책임.
    ``session.delete(...)`` + ``flush()`` 까지만 수행한다.

    Args:
        session: 건의사항 DB ORM 세션.
        suggestion_id: 삭제 대상 게시글 PK.

    Returns:
        실제로 삭제됐으면 ``True``, 게시글이 이미 존재하지 않았다면 ``False``
        — 호출자가 404 vs 정상 흐름을 분기할 수 있도록 한다.
    """
    suggestion = session.get(Suggestion, suggestion_id)
    if suggestion is None:
        return False
    session.delete(suggestion)
    session.flush()
    return True


__all__ = [
    "count_suggestions",
    "list_suggestions",
    "create_suggestion",
    "get_suggestion_by_id",
    "list_comments_by_suggestion_id",
    "create_suggestion_comment",
    "update_suggestion_acceptance",
    "update_suggestion",
    "delete_suggestion",
]
