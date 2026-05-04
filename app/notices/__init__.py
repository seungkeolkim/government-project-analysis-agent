"""공지사항 게시판 패키지 (task 00056).

본 패키지는 공지사항 게시판의 ORM 모델과 repository 함수를 노출한다.
DB 파일은 건의사항 게시판(``app.suggestions``) 과 동일한 ``boards.sqlite3`` 를
공유하며, 동일한 declarative base(``app.suggestions.models.Base``) 위에 선언된다.
엔진·세션은 ``app.suggestions.session`` 의 것을 그대로 재사용한다.

공개 API:
    - 모델:
        :class:`Notice`
    - repository:
        :func:`get_notice_by_id`,
        :func:`count_notices`,
        :func:`list_notices`,
        :func:`create_notice`,
        :func:`update_notice`,
        :func:`delete_notice`
"""

from app.notices.models import Notice
from app.notices.repository import (
    count_notices,
    create_notice,
    delete_notice,
    get_notice_by_id,
    list_notices,
    update_notice,
)

__all__ = [
    "Notice",
    "get_notice_by_id",
    "count_notices",
    "list_notices",
    "create_notice",
    "update_notice",
    "delete_notice",
]
