"""건의사항 게시판 도메인 서비스 — 고아 글 노출/마스킹 정책.

본 모듈은 라우트와 템플릿 사이에서 "메인 DB users 가 사라진 글/댓글" 을 어떻게
표시할지를 결정하는 단일 정책을 제공한다. 사용자 modify 턴 요구:

    "메인 db 가 reset 되어 게시글 작성자 정보가 사라지면 글, 댓글 작성자는 NULL 로
     표시되고 관리자만 볼 수 있도록 게시판이 망가지지 않는 방어로직"

정책 (단일 정의):
    - **고아(orphan)** = ``author_user_id`` 가 ``None`` 이거나, 메인 DB ``users``
      테이블에 더 이상 존재하지 않는 경우.
    - **비관리자**: 고아 글/댓글은 결과에서 **제외**한다(목록·뷰어·댓글 모두 동일).
    - **관리자**: 고아 글/댓글도 **표시**하되, ``display_author_name`` 을 ``None``
      으로 마스킹한다 — "작성자 정보가 사라진 글" 임을 시각적으로 구분.
    - 그 외(alive): ``author_name`` 을 그대로 노출한다(유저가 작성 시 입력한 표기명).

사용 패턴 (subtask 00051-2/3/4 공통):
    1. 라우트가 게시글/댓글을 한 번에 조회한다.
    2. ``author_user_id`` set 을 모은다.
    3. cross-DB 헬퍼 호출:
        - 글 목록/뷰어: ``get_alive_user_ids`` 로 alive set 만 받음 — Suggestion 은
          모델 자체에 ``author_name`` 컬럼이 있어 표시명을 가져오지 않아도 된다.
        - 댓글 목록: ``get_alive_user_username_map`` 으로 ``id → username`` map 을
          받음 — SuggestionComment 는 ``author_name`` 컬럼이 없어 메인 DB 의
          username 을 즉석 조회해야 한다.
    4. ``apply_orphan_policy_to_suggestions(items, alive, is_admin=...)`` 또는
       ``apply_orphan_policy_to_comments(items, username_map, is_admin=...)`` 로
       템플릿이 그대로 쓸 수 있는 view 리스트를 얻는다.

본 모듈은 ORM 인스턴스를 변형하지 않는다 — 항상 view dataclass 를 새로 생성한다.
세션이 닫힌 뒤에도 안전하게 템플릿이 참조할 수 있도록(SQLAlchemy detached
인스턴스 lazy-load 회피) 표시에 필요한 값은 view 가 미리 들고 있어야 한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from app.suggestions.models import AcceptanceStatus, Suggestion, SuggestionComment


@dataclass(frozen=True)
class SuggestionView:
    """라우트→템플릿 표시용 view dataclass.

    ORM ``Suggestion`` 인스턴스를 직접 참조하는 대신, 템플릿이 필요로 하는
    필드만 추출해 보존한다. 이렇게 하면 세션이 닫힌 뒤에도 detached lazy-load
    예외 없이 템플릿이 안전하게 렌더할 수 있다.

    Attributes:
        suggestion_id: 게시글 PK.
        title: 게시글 제목.
        is_secret: 비밀글 여부.
        is_orphan: 고아 글 여부 (작성자가 메인 DB 에 없거나 None).
        display_author_name: 표시용 작성자명. 고아 글이면 None 으로 마스킹.
        contact_email: 선택 입력된 연락처 이메일. 고아 글일 때는 None 으로 마스킹.
        acceptance_status: 관리자 수용 상태 (검토중/수용/일부수용/거절).
        created_at: 작성 시각(UTC). KST 표시는 템플릿 필터가 처리한다.
        expected_completion_date: 관리자 입력 예상 완료일.
        comment_count: 게시글에 달린 댓글 수. 목록 페이지 [N] 표시용.
    """

    suggestion_id: int
    title: str
    is_secret: bool
    is_orphan: bool
    display_author_name: str | None
    contact_email: str | None
    acceptance_status: AcceptanceStatus
    created_at: datetime
    expected_completion_date: date | None
    comment_count: int


def is_orphan_author(
    author_user_id: int | None,
    alive_user_ids: set[int],
) -> bool:
    """단일 author_user_id 가 고아인지 판정한다.

    "고아" 판정 규칙:
        - ``author_user_id is None`` → True (alive 도 orphan 도 아닌 별 케이스이지만
          가시성·표시 정책상 orphan 과 동일 분기로 처리).
        - ``author_user_id not in alive_user_ids`` → True (메인 DB users 에 없음).
        - 그 외 → False (alive).

    본 함수는 후속 subtask(뷰어/댓글) 에서 단일 row 의 권한 분기에 그대로 재사용된다.

    Args:
        author_user_id: 게시글 또는 댓글의 author_user_id 값.
        alive_user_ids: ``get_alive_user_ids`` 가 반환한 살아있는 user id set.

    Returns:
        고아면 True, alive 면 False.
    """
    if author_user_id is None:
        return True
    return author_user_id not in alive_user_ids


def apply_orphan_policy_to_suggestions(
    suggestions: Iterable[Suggestion],
    alive_user_ids: set[int],
    *,
    is_admin: bool,
    comment_count_map: dict[int, int],
) -> list[SuggestionView]:
    """건의사항 목록에 고아 글 노출/마스킹 정책을 적용한다.

    - 비관리자: 고아 글은 반환 리스트에서 제외 (사용자 modify 턴 — "일반 사용자
      에게는 비노출").
    - 관리자: 고아 글도 포함시키되 ``display_author_name`` / ``contact_email`` 을
      ``None`` 으로 마스킹 (사용자 modify 턴 — "관리자만 볼 수 있고 작성자는
      NULL 로 표시").
    - alive 글: ``author_name`` / ``contact_email`` 을 원본 그대로 노출.

    입력 순서를 그대로 보존한다(라우트가 정렬을 책임진다).

    Args:
        suggestions: 정렬·페이지네이션이 끝난 ``Suggestion`` 시퀀스.
        alive_user_ids: ``get_alive_user_ids`` 결과.
        is_admin: 현재 사용자가 관리자(``is_admin=True``)인지 여부. 비로그인은 False
            로 호출한다(비로그인 = 비관리자).
        comment_count_map: ``count_comments_by_suggestion_ids`` 결과.
            댓글이 없는 게시글은 키가 없으며, 조회 시 ``.get(id, 0)`` 으로 처리한다.

    Returns:
        ``SuggestionView`` 리스트. 비관리자 호출에서는 길이가 입력보다 짧을 수 있다.
    """
    views: list[SuggestionView] = []
    for suggestion in suggestions:
        is_orphan = is_orphan_author(suggestion.author_user_id, alive_user_ids)
        if is_orphan and not is_admin:
            # 비관리자에게는 고아 글을 비노출 처리한다.
            continue

        # alive 글은 원본 노출, orphan 글(관리자 전용 경로)은 마스킹.
        display_author_name = None if is_orphan else suggestion.author_name
        contact_email = None if is_orphan else suggestion.contact_email

        views.append(
            SuggestionView(
                suggestion_id=suggestion.id,
                title=suggestion.title,
                is_secret=suggestion.is_secret,
                is_orphan=is_orphan,
                display_author_name=display_author_name,
                contact_email=contact_email,
                acceptance_status=suggestion.acceptance_status,
                created_at=suggestion.created_at,
                expected_completion_date=suggestion.expected_completion_date,
                comment_count=comment_count_map.get(suggestion.id, 0),
            )
        )
    return views


@dataclass(frozen=True)
class SuggestionCommentView:
    """라우트→템플릿 표시용 댓글 view dataclass.

    :class:`SuggestionComment` ORM 인스턴스를 직접 참조하는 대신, 템플릿이 필요로
    하는 필드만 추출해 보존한다. 세션이 닫힌 뒤에도 detached lazy-load 예외 없이
    템플릿이 안전하게 렌더할 수 있도록 한다.

    Attributes:
        comment_id: 댓글 PK.
        body: 댓글 본문 (사용자 입력 — XSS 위험. 템플릿에서 |safe 절대 금지).
        is_orphan: 고아 댓글 여부 (작성자가 메인 DB 에 없거나 None).
        display_author_name: 표시용 작성자명.
            - alive 작성자: 메인 DB users.username 값(렌더 시점 조회).
            - 고아 댓글: ``None`` 으로 마스킹 (관리자 전용 경로에서만 노출됨).
        created_at: 댓글 작성 시각(UTC). KST 표시는 템플릿 필터가 처리한다.
        is_owner: 현재 로그인 사용자가 이 댓글의 작성자인지 여부 (00064-1).
            비로그인 또는 작성자가 아니면 False. 템플릿에서 수정·삭제 버튼
            노출 여부를 결정하는 데 사용한다.
    """

    comment_id: int
    body: str
    is_orphan: bool
    display_author_name: str | None
    created_at: datetime
    is_owner: bool


def apply_orphan_policy_to_comments(
    comments: Iterable[SuggestionComment],
    alive_user_username_map: dict[int, str],
    *,
    is_admin: bool,
    current_user_id: int | None = None,
) -> list[SuggestionCommentView]:
    """댓글 목록에 고아 댓글 노출/마스킹 정책을 적용한다.

    글(:func:`apply_orphan_policy_to_suggestions`) 과 완전히 동일한 정책 — 단지
    표시 페이로드(body/created_at) 와 작성자명 출처(메인 DB users.username) 가
    다르다. 사용자 modify 턴 \"댓글 작성자는 NULL 로 표시되고 관리자만 볼 수
    있도록\" 을 그대로 충족한다.

    - 비관리자: 고아 댓글은 결과에서 **제외** (UI 비노출).
    - 관리자: 고아 댓글도 포함시키되 ``display_author_name=None`` 으로 마스킹.
    - alive 댓글: ``alive_user_username_map[author_user_id]`` 를 표시명으로 사용.

    입력 순서를 그대로 보존한다(라우트가 정렬을 책임진다 — 보통 created_at 오름).

    Args:
        comments: 정렬·페이지네이션이 끝난 ``SuggestionComment`` 시퀀스.
        alive_user_username_map: ``get_alive_user_username_map`` 결과 dict.
            살아있는 user id 만 키로 포함되어 있다.
        is_admin: 현재 사용자가 관리자 (``is_admin=True``) 인지. 비로그인은 False.
        current_user_id: 현재 로그인 사용자의 메인 DB ``users.id`` 값 (00064-1).
            비로그인이면 ``None`` — 모든 댓글의 ``is_owner`` 가 False 로 채워진다.

    Returns:
        ``SuggestionCommentView`` 리스트. 비관리자 호출에서는 길이가 입력보다 짧을
        수 있다(고아 댓글 제외).
    """
    views: list[SuggestionCommentView] = []
    for comment in comments:
        # 고아 판정은 글과 동일한 단일 정책 — alive_set 키만 추출해 재사용.
        is_orphan = is_orphan_author(
            comment.author_user_id,
            set(alive_user_username_map.keys()),
        )
        if is_orphan and not is_admin:
            # 비관리자: 고아 댓글 비노출 (사용자 modify 턴).
            continue

        # alive 댓글은 username 으로 표시, orphan 댓글은 None 으로 마스킹.
        if is_orphan:
            display_author_name: str | None = None
        else:
            # author_user_id 가 alive 라면 map 에 반드시 존재한다.
            # author_user_id 가 None 인 경우는 is_orphan=True 분기에서 이미 처리됨.
            display_author_name = alive_user_username_map[comment.author_user_id]

        # 비로그인(current_user_id is None) 이면 항상 False. 고아 댓글에서도
        # author_user_id 가 None 이므로 정수 비교가 매칭되지 않아 안전하다.
        is_owner = (
            current_user_id is not None
            and comment.author_user_id == current_user_id
        )

        views.append(
            SuggestionCommentView(
                comment_id=comment.id,
                body=comment.body,
                is_orphan=is_orphan,
                display_author_name=display_author_name,
                created_at=comment.created_at,
                is_owner=is_owner,
            )
        )
    return views


__all__ = [
    "SuggestionView",
    "SuggestionCommentView",
    "is_orphan_author",
    "apply_orphan_policy_to_suggestions",
    "apply_orphan_policy_to_comments",
]
