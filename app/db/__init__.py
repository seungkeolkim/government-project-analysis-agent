"""DB 계층 패키지.

SQLAlchemy 기반 ORM 모델과 세션 팩토리를 노출한다.
- models: `Base`, `Announcement`, `AnnouncementStatus`, `Attachment`
- repository: `UpsertResult` (증분 UPSERT 반환 타입)
- session: `get_engine`, `SessionLocal`, `session_scope`, `reset_engine_cache`
- init_db: `init_db` (CLI 및 서비스에서 호출 가능)
"""

from app.db.init_db import init_db
from app.db.models import Announcement, AnnouncementStatus, Attachment, Base
from app.db.repository import (
    UpsertResult,
    upsert_attachment,
    get_attachment_by_id,
    get_attachment_by_announcement_and_filename,
    get_attachments_by_announcement,
)
from app.db.session import (
    SessionLocal,
    get_engine,
    reset_engine_cache,
    session_scope,
)

__all__ = [
    "Base",
    "Announcement",
    "AnnouncementStatus",
    "Attachment",
    "UpsertResult",
    "get_engine",
    "SessionLocal",
    "session_scope",
    "reset_engine_cache",
    "init_db",
    "upsert_attachment",
    "get_attachment_by_id",
    "get_attachment_by_announcement_and_filename",
    "get_attachments_by_announcement",
]
