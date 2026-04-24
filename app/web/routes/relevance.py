"""관련성 판정 라우터 (Phase 3a / 00035-2).

엔드포인트:
    POST   /canonical/{canonical_id}/relevance          → 판정 저장/갱신
    DELETE /canonical/{canonical_id}/relevance          → 판정 삭제
    GET    /canonical/{canonical_id}/relevance/history  → 판정 히스토리 조회

보호:
    POST/DELETE: current_user_required + ensure_same_origin (가벼운 CSRF 방어).
    GET:         current_user_required (읽기 전용이므로 origin 체크 생략).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.auth.dependencies import current_user_required, ensure_same_origin
from app.db.models import User
from app.db.repository import (
    RELEVANCE_ALLOWED_VERDICTS,
    delete_relevance_judgment,
    get_canonical_project_by_id,
    get_relevance_history,
    set_relevance_judgment,
)
from app.db.session import session_scope

router = APIRouter(tags=["relevance"])


class RelevanceJudgmentIn(BaseModel):
    verdict: str
    reason: str | None = None

    @field_validator("verdict")
    @classmethod
    def _check_verdict(cls, v: str) -> str:
        if v not in RELEVANCE_ALLOWED_VERDICTS:
            raise ValueError(
                f"verdict 는 {sorted(RELEVANCE_ALLOWED_VERDICTS)} 중 하나여야 합니다."
            )
        return v


@router.post(
    "/canonical/{canonical_id}/relevance",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def set_relevance(
    canonical_id: int,
    body: RelevanceJudgmentIn,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """판정을 저장한다. 기존 판정이 있으면 History 이관 후 덮어쓴다."""
    with session_scope() as session:
        project = get_canonical_project_by_id(session, canonical_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"canonical_project id={canonical_id} 를 찾을 수 없습니다.",
            )
        judgment = set_relevance_judgment(
            session,
            canonical_project_id=canonical_id,
            user_id=current_user.id,
            verdict=body.verdict,
            reason=body.reason,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "canonical_project_id": judgment.canonical_project_id,
                "user_id": judgment.user_id,
                "verdict": judgment.verdict,
                "reason": judgment.reason,
            },
        )


@router.delete(
    "/canonical/{canonical_id}/relevance",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def remove_relevance(
    canonical_id: int,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """판정을 삭제한다. 삭제 전 History 이관. 판정이 없으면 404."""
    with session_scope() as session:
        project = get_canonical_project_by_id(session, canonical_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"canonical_project id={canonical_id} 를 찾을 수 없습니다.",
            )
        deleted = delete_relevance_judgment(
            session,
            canonical_project_id=canonical_id,
            user_id=current_user.id,
        )
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="삭제할 판정이 없습니다.",
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"detail": "삭제되었습니다."},
        )


@router.get(
    "/canonical/{canonical_id}/relevance/history",
    status_code=status.HTTP_200_OK,
)
def get_history(
    canonical_id: int,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """판정 히스토리를 최신순으로 반환한다."""
    with session_scope() as session:
        project = get_canonical_project_by_id(session, canonical_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"canonical_project id={canonical_id} 를 찾을 수 없습니다.",
            )
        items = get_relevance_history(session, canonical_project_id=canonical_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "canonical_project_id": canonical_id,
                "history": [
                    {
                        "username": hw.username,
                        "verdict": hw.history.verdict,
                        "reason": hw.history.reason,
                        "decided_at": hw.history.decided_at.isoformat()
                        if hw.history.decided_at
                        else None,
                        "archived_at": hw.history.archived_at.isoformat()
                        if hw.history.archived_at
                        else None,
                        "archive_reason": hw.history.archive_reason,
                    }
                    for hw in items
                ],
            },
        )
