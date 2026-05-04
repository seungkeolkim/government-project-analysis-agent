"""건의사항 게시판 패키지 (task 00051).

본 패키지는 메인 DB(``app.sqlite3``) 와 격리된 별도 SQLite 파일에 저장되는
건의사항 게시판의 ORM 모델, 엔진/세션 헬퍼, 그리고 cross-DB 작성자 유효성
batch 헬퍼를 노출한다.

후속 subtask 에서 추가될 라우트·템플릿은 본 패키지의 공개 API 만을 사용하며,
패키지 내부 구현(개별 모듈)은 직접 import 하지 않는 것을 컨벤션으로 한다.

공개 API:
    - 모델:
        :data:`Base`, :class:`Suggestion`, :class:`SuggestionComment`,
        :class:`AcceptanceStatus`
    - 엔진/세션:
        :func:`get_suggestions_engine`,
        :func:`SuggestionsSessionLocal`,
        :func:`suggestions_session_scope`,
        :func:`reset_suggestions_engine_cache`,
        :func:`init_suggestions_db`
    - cross-DB author 헬퍼:
        :func:`get_alive_user_ids`,
        :func:`get_alive_user_username_map`
"""

from app.suggestions.author_validity import (
    get_alive_user_ids,
    get_alive_user_username_map,
)
from app.suggestions.models import (
    AcceptanceStatus,
    Base,
    Suggestion,
    SuggestionComment,
)
from app.suggestions.repository import (
    count_suggestions,
    create_suggestion,
    create_suggestion_comment,
    delete_suggestion,
    get_suggestion_by_id,
    list_comments_by_suggestion_id,
    list_suggestions,
    update_suggestion,
    update_suggestion_acceptance,
)
from app.suggestions.service import (
    SuggestionCommentView,
    SuggestionView,
    apply_orphan_policy_to_comments,
    apply_orphan_policy_to_suggestions,
    is_orphan_author,
)
from app.suggestions.session import (
    SuggestionsSessionLocal,
    get_suggestions_engine,
    init_suggestions_db,
    reset_suggestions_engine_cache,
    suggestions_session_scope,
)

__all__ = [
    "Base",
    "AcceptanceStatus",
    "Suggestion",
    "SuggestionComment",
    "get_suggestions_engine",
    "SuggestionsSessionLocal",
    "suggestions_session_scope",
    "reset_suggestions_engine_cache",
    "init_suggestions_db",
    "get_alive_user_ids",
    "get_alive_user_username_map",
    "count_suggestions",
    "list_suggestions",
    "create_suggestion",
    "get_suggestion_by_id",
    "list_comments_by_suggestion_id",
    "create_suggestion_comment",
    "update_suggestion_acceptance",
    "update_suggestion",
    "delete_suggestion",
    "SuggestionView",
    "SuggestionCommentView",
    "apply_orphan_policy_to_suggestions",
    "apply_orphan_policy_to_comments",
    "is_orphan_author",
]
