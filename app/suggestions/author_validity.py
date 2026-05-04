"""cross-DB 작성자 유효성 batch 헬퍼.

건의사항 게시판은 메인 DB(``app.sqlite3``) 와 격리된 별도 SQLite 파일을 쓴다.
두 DB 사이에는 외래키 제약을 걸 수 없으므로, 게시글/댓글의 ``author_user_id``
가 메인 DB ``users`` 테이블에 실제로 살아있는지 여부는 **렌더 시점에 명시적인
cross-DB 쿼리로만** 판정해야 한다.

본 모듈은 그 판정을 batch 인터페이스(``IN`` 절 단일 쿼리) 로 제공한다.
N+1 쿼리를 만들지 않도록 라우트는 반드시 다음 패턴으로 사용한다:

    1. 게시글/댓글 목록을 한 번에 가져온다.
    2. ``author_user_id`` 의 set 을 모은다(``None`` 은 알아서 걸러진다).
    3. ``get_alive_user_ids(main_session, ids)`` 로 alive set 을 한 번 받는다.
    4. 각 row 를 alive set 에 비추어
       (a) 비관리자: alive 가 아닌 글/댓글은 결과에서 제외,
       (b) 관리자: alive 가 아닌 글/댓글은 작성자명을 NULL 로 마스킹,
       으로 분기한다.

설계 메모:
    - ``author_user_id`` 가 ``None`` 인 경우는 메인 DB 가 reset 되어 가리키던
      user row 가 사라진 것과 표시·가시성 차원에서 동일하게 취급한다 — 즉
      "alive 가 아닌 별 케이스" 다. 본 헬퍼는 ``None`` 을 자동으로 걸러내고
      반환 set 에는 절대 포함시키지 않는다. 따라서 호출자는
      ``alive_set = get_alive_user_ids(...)`` 후 ``uid in alive_set`` 만 보면 된다.
    - 입력은 ``Iterable[int | None]`` 로 받아 ``set(...)`` 로 정규화한다.
      반복자를 두 번 소비하지 않도록 한다.
    - 빈 입력은 추가 쿼리 없이 빈 set 을 즉시 반환한다.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User


def get_alive_user_ids(
    main_session: Session,
    user_ids: Iterable[int | None],
) -> set[int]:
    """주어진 user id 들 중 메인 DB ``users`` 테이블에 살아있는 id set 을 반환한다.

    ``None`` 값은 자동으로 제외되며 반환 set 에 절대 포함되지 않는다 — 호출자는
    ``None`` / orphan / alive 세 케이스를 다음과 같이 단순화해 처리할 수 있다:

        - ``uid is None``   → 별 케이스 (관리자만, 작성자 NULL 표시)
        - ``uid in alive``  → 정상 (alive 사용자)
        - 그 외             → orphan (관리자만, 작성자 NULL 표시)

    Args:
        main_session: 메인 DB(``app.sqlite3``) 의 ORM 세션. 본 헬퍼는 본 세션을
            소비하지 않으므로(SELECT 만 수행) 호출자가 트랜잭션을 그대로 유지해도 된다.
        user_ids: 검사 대상 user id 들의 iterable. ``None`` 포함 가능.

    Returns:
        메인 DB 에 살아있는 user id 의 set. 입력이 비어 있거나 ``None`` 만으로
        구성되어 있으면 빈 set 을 반환한다(추가 쿼리 발생 없음).

    Example:
        >>> author_ids = {s.author_user_id for s in suggestions}
        >>> alive = get_alive_user_ids(main_session, author_ids)
        >>> for s in suggestions:
        ...     is_alive = s.author_user_id is not None and s.author_user_id in alive
    """
    candidate_ids: set[int] = {uid for uid in user_ids if uid is not None}
    if not candidate_ids:
        # 빈 입력: 메인 DB 에 쿼리를 보내지 않고 즉시 빈 set 반환.
        return set()

    rows = main_session.execute(
        select(User.id).where(User.id.in_(candidate_ids))
    ).scalars().all()
    return set(rows)


def get_alive_user_username_map(
    main_session: Session,
    user_ids: Iterable[int | None],
) -> dict[int, str]:
    """주어진 user id 들에 대해 살아있는 user 의 ``id → username`` 매핑을 반환한다.

    :func:`get_alive_user_ids` 의 형제 함수로, IN 절 단일 쿼리는 동일하지만
    ``(id, username)`` 두 열을 함께 가져와 dict 으로 반환한다. 댓글 렌더 단계
    에서 사용한다 — :class:`SuggestionComment` 모델은 ``author_name`` 컬럼이
    없으므로 표시용 작성자명을 메인 DB ``users.username`` 에서 즉석으로
    조회해야 한다(스키마 변경 회피).

    호출자는 다음 단순한 규칙으로 alive/orphan 을 분기할 수 있다:

        - ``author_user_id is None``    → orphan (관리자만, 작성자 NULL 표시)
        - ``author_user_id in map``     → alive  → 표시명 = ``map[author_user_id]``
        - 그 외                         → orphan (관리자만, 작성자 NULL 표시)

    또한 ``set(map.keys())`` 는 :func:`get_alive_user_ids` 와 동일한 alive set 이
    되므로, 같은 입력으로 두 함수를 동시에 호출하지 않고 본 함수 결과만으로
    두 정보를 모두 얻을 수 있다.

    Args:
        main_session: 메인 DB(``app.sqlite3``) 의 ORM 세션. SELECT 만 수행한다.
        user_ids: 검사 대상 user id 들의 iterable. ``None`` 은 자동으로 제외된다.

    Returns:
        ``{user_id: username}`` dict. 입력이 비어 있거나 ``None`` 만으로 구성되면
        빈 dict 을 반환한다(메인 DB 에 쿼리 발송 없음).
    """
    candidate_ids: set[int] = {uid for uid in user_ids if uid is not None}
    if not candidate_ids:
        return {}

    rows = main_session.execute(
        select(User.id, User.username).where(User.id.in_(candidate_ids))
    ).all()
    return {user_id: username for user_id, username in rows}


__all__ = [
    "get_alive_user_ids",
    "get_alive_user_username_map",
]
