"""읽음 bulk 처리 라우터 (Phase 3a / 00035-4).

엔드포인트:
    POST /announcements/bulk-mark-read    → 선택 또는 필터 전체를 읽음으로 처리
    POST /announcements/bulk-mark-unread  → 선택 또는 필터 전체를 읽지 않음으로 처리

요청 body (두 가지 모드):
    { "mode": "ids",    "ids": [1, 2, 3] }
    { "mode": "filter", "filter": { "status": "접수중", "source": "IRIS", "search": "AI" } }

보호:
    current_user_required + ensure_same_origin (가벼운 CSRF 방어).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator

from app.auth.dependencies import current_user_required, ensure_same_origin
from app.db.models import User
from app.db.repository import (
    MAX_BULK_MARK,
    bulk_mark_announcements_read,
    bulk_mark_announcements_unread,
    resolve_announcement_ids_by_filter,
)
from app.db.session import session_scope

router = APIRouter(tags=["bulk"])


# ---------------------------------------------------------------------------
# Request body models
# ---------------------------------------------------------------------------


class BulkFilterParams(BaseModel):
    status: str | None = None
    source: str | None = None
    search: str | None = None


class BulkMarkBody(BaseModel):
    mode: str
    ids: list[int] | None = None
    filter: BulkFilterParams | None = None

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        if v not in {"ids", "filter"}:
            raise ValueError("mode 는 'ids' 또는 'filter' 이어야 합니다.")
        return v

    @model_validator(mode="after")
    def _check_payload(self) -> BulkMarkBody:
        if self.mode == "ids":
            if not self.ids:
                raise ValueError("mode='ids' 일 때 ids 는 비어 있을 수 없습니다.")
        elif self.mode == "filter":
            if self.filter is None:
                self.filter = BulkFilterParams()
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_ids(session_factory, body: BulkMarkBody) -> list[int]:
    """body 에서 announcement id 목록을 추출한다. MAX_BULK_MARK 초과 시 422."""
    if body.mode == "ids":
        ids = body.ids or []
        if len(ids) > MAX_BULK_MARK:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"ids 가 최대 허용치({MAX_BULK_MARK}건)를 초과합니다.",
            )
        return ids

    # filter 모드
    f = body.filter or BulkFilterParams()
    with session_factory() as session:
        ids = resolve_announcement_ids_by_filter(
            session,
            status=f.status or None,
            source=f.source or None,
            search=f.search or None,
        )
    if len(ids) > MAX_BULK_MARK:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"필터 결과 {len(ids)}건이 최대 허용치({MAX_BULK_MARK}건)를 초과합니다."
            ),
        )
    return ids


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/announcements/bulk-mark-read",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def bulk_mark_read(
    body: BulkMarkBody,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """선택 또는 필터 전체 공고를 읽음으로 처리한다."""
    ids = _resolve_ids(session_scope, body)
    if not ids:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"updated": 0})

    with session_scope() as session:
        updated = bulk_mark_announcements_read(
            session,
            user_id=current_user.id,
            announcement_ids=ids,
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"updated": updated})


@router.post(
    "/announcements/bulk-mark-unread",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def bulk_mark_unread(
    body: BulkMarkBody,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """선택 또는 필터 전체 공고를 읽지 않음으로 처리한다."""
    ids = _resolve_ids(session_scope, body)
    if not ids:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"updated": 0})

    with session_scope() as session:
        updated = bulk_mark_announcements_unread(
            session,
            user_id=current_user.id,
            announcement_ids=ids,
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content={"updated": updated})
