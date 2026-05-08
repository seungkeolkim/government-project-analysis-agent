"""관련성 판정 라우터 (Phase 3a / 00035-2 → task 00085 조직 단위 확장).

엔드포인트:
    POST   /canonical/{canonical_id}/relevance          → 판정 저장/갱신
    DELETE /canonical/{canonical_id}/relevance          → 판정 삭제
    GET    /canonical/{canonical_id}/relevance/history  → 판정 히스토리 조회
    GET    /canonical/{canonical_id}/relevance/mine     → 본인 판정 목록 조회 (task 00089)

task 00085 — 조직 단위 판정 확장:
    - POST/DELETE body 에 ``organization_id`` (선택) 를 추가한다.
      None 이면 본인 개인 판정 슬롯, 정수면 그 조직 입장의 슬롯.
    - 본인 소속 조직 검증: organization_id 가 본인이 속한 조직 PK 가 아니면 422.
      ``user_organizations`` 매핑 테이블을 ``get_user_organization_ids`` 로 조회한다.
    - 작성자 본인만 수정·삭제: 라우터가 ``user_id=current_user.id`` 로 자동
      필터하므로 다른 멤버가 만든 row 는 자연스럽게 잡히지 않는다 (404).
      안 1 단일 UNIQUE 채택으로 같은 조직 row 충돌 응답 (409/422 + 작성자 안내)
      로직은 추가하지 않는다.
    - GET /history 비로그인 허용 (사용자 modify 턴 결정 3 — 비로그인 = 로그인
      동일 노출). 응답에는 ``organization_id`` 도 포함된다.

보호:
    - POST/DELETE: ``current_user_required`` + ``ensure_same_origin``.
    - GET: ``current_user_optional`` 만 적용 (비로그인 허용).
      ``ensure_same_origin`` 은 GET 에서는 원래도 적용하지 않는다 (읽기 전용).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.auth.dependencies import (
    current_user_optional,
    current_user_required,
    ensure_same_origin,
)
from app.db.models import User
from app.db.repository import (
    RELEVANCE_ALLOWED_VERDICTS,
    delete_relevance_judgment,
    get_canonical_project_by_id,
    get_relevance_history,
    get_relevance_summary_by_canonical_id_map,
    set_relevance_judgment,
)
from app.db.session import session_scope
from app.organizations.service import get_user_organization_ids

router = APIRouter(tags=["relevance"])


class RelevanceJudgmentIn(BaseModel):
    """POST /canonical/{id}/relevance 요청 본문 스키마.

    Attributes:
        verdict: '관련' / '무관' 중 하나. RELEVANCE_ALLOWED_VERDICTS 검증.
        reason: 사용자가 적은 짧은 사유. 빈 문자열 / None 허용.
        organization_id: 조직 PK. None 이면 개인 판정, 정수면 그 조직 입장 판정.
            본인 소속이 아닌 조직 PK 가 들어오면 라우터가 422 로 응답한다
            (사용자 원문 검증 항목 3 — 본인 소속 외 조직 판정 시도 → 422).
    """

    verdict: str
    reason: str | None = None
    organization_id: int | None = None

    @field_validator("verdict")
    @classmethod
    def _check_verdict(cls, v: str) -> str:
        """verdict 가 허용 도메인('관련'/'무관') 안에 있는지 확인한다."""
        if v not in RELEVANCE_ALLOWED_VERDICTS:
            raise ValueError(
                f"verdict 는 {sorted(RELEVANCE_ALLOWED_VERDICTS)} 중 하나여야 합니다."
            )
        return v


class RelevanceJudgmentDeleteIn(BaseModel):
    """DELETE /canonical/{id}/relevance 요청 본문 스키마 (선택).

    body 가 비어 있으면 organization_id=None (개인 판정 row) 가 삭제 대상이다.
    조직 row 를 삭제하려면 본문에 ``{\"organization_id\": <int>}`` 를 보낸다.

    Attributes:
        organization_id: 삭제 대상 조직 PK. None 이면 개인 판정 row.
    """

    organization_id: int | None = None


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
    """판정을 저장한다. 기존 (canonical, user, organization_id) 트리플의 row 가
    있으면 History 이관(user_overwrite) 후 새 판정으로 덮어쓴다.

    organization_id 가 정수일 때:
        본인이 그 조직에 소속되어 있는지 ``user_organizations`` 매핑으로 검증한다.
        본인 소속이 아니면 422 — "본인 소속 조직이 아닙니다." 를 반환한다.
        무소속 사용자가 organization_id 를 지정해도 같은 흐름으로 422 가 된다
        (소속 목록이 비어 있으므로).

    Returns:
        200 + ``{canonical_project_id, user_id, organization_id, verdict, reason}``
        형태의 JSON.
    """
    with session_scope() as session:
        # canonical_project 존재 검증 — 없으면 404.
        project = get_canonical_project_by_id(session, canonical_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"canonical_project id={canonical_id} 를 찾을 수 없습니다.",
            )

        # 조직 판정인 경우 본인 소속 조직 검증 — 사용자 원문 검증 #3 / #4.
        # 무소속 사용자의 경우 get_user_organization_ids 가 빈 list 를 반환하므로
        # 같은 422 분기로 자연스럽게 차단된다.
        if body.organization_id is not None:
            user_organization_ids = get_user_organization_ids(
                session, current_user.id
            )
            if body.organization_id not in user_organization_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="본인 소속 조직이 아닙니다.",
                )

        # repository 헬퍼가 트리플 슬롯 단위의 'History 이관 → 기존 DELETE → 신규
        # INSERT' 트랜잭션을 처리한다. organization_id 도 그대로 전달.
        judgment = set_relevance_judgment(
            session,
            canonical_project_id=canonical_id,
            user_id=current_user.id,
            verdict=body.verdict,
            reason=body.reason,
            organization_id=body.organization_id,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "canonical_project_id": judgment.canonical_project_id,
                "user_id": judgment.user_id,
                "organization_id": judgment.organization_id,
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
    body: RelevanceJudgmentDeleteIn | None = Body(default=None),
) -> JSONResponse:
    """판정을 삭제한다. 삭제 전 History 이관(user_overwrite).

    삭제 대상 트리플:
        (canonical_project_id, user_id=current_user.id, organization_id=body.organization_id)
        body 가 없거나 organization_id 가 None 이면 개인 판정 row 가 대상.
        ``user_id=current_user.id`` 가 자동 적용되므로 다른 멤버가 만든 row 는
        자연스럽게 잡히지 않고 404 로 응답된다 (사용자 원문 검증 #6 — 본 라우터의
        선택은 가이드대로 404).

    조직 판정 삭제에는 본인 소속 검증을 별도로 하지 않는다 — 본인이 작성하지
    않은 조직 row 는 어차피 트리플 매칭에서 안 잡혀 404 가 되기 때문에 한 번의
    필터로 충분하다.

    Returns:
        200 + ``{detail: \"삭제되었습니다.\"}`` 또는 404.
    """
    organization_id_value = body.organization_id if body is not None else None

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
            organization_id=organization_id_value,
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
    current_user: User | None = Depends(current_user_optional),
) -> JSONResponse:
    """판정 히스토리를 최신순으로 반환한다 (개인 + 조직 row 모두 포함).

    task 00085 — 비로그인 허용 (사용자 modify 턴 결정 3 — 비로그인 = 로그인
    동일 노출). ``current_user_optional`` 가 None 을 반환하더라도 본 핸들러는
    그대로 응답한다.

    응답 항목별 ``organization_id`` 는 NULL (개인 판정 시) 또는 정수 (조직 판정
    시) 로 들어간다. 조직명 (``organization_name``) 은 본 응답에 포함하지
    않는다 — 상세 페이지가 ``get_relevance_summary_by_canonical_id_map`` 으로
    별도 조회한다.

    Returns:
        200 + ``{canonical_project_id, history: [...]}``. canonical 이 없으면 404.
    """
    # current_user 는 디버깅·로그 외에는 사용하지 않는다 — 비로그인도 동일 응답.
    _ = current_user

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
                        "organization_id": hw.history.organization_id,
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


@router.get(
    "/canonical/{canonical_id}/relevance/mine",
    status_code=status.HTTP_200_OK,
)
def get_my_relevance_judgments(
    canonical_id: int,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """본인이 해당 canonical 에 작성한 모든 판정(개인 + 본인 작성 조직 row)을 반환한다.

    task 00089 — 관련성 모달 하단 '내 판정 목록' 섹션의 초기 로딩 및 X 삭제 후
    목록 재갱신에 사용한다. others 는 포함하지 않는다 (사용자 원문 "자신이 한 판정").

    응답 items 의 정렬은 개인 판정(organization_id IS NULL) 먼저, 이후 조직 판정을
    decided_at DESC 순서로 나열한다 (get_relevance_summary_by_canonical_id_map 의
    정렬을 그대로 활용).

    Returns:
        200 + ``{canonical_project_id, items: [{organization_id, organization_name,
            verdict, reason, decided_at}]}``. 판정이 없으면 items = [].
        canonical 이 없으면 404. 비로그인이면 401.
    """
    with session_scope() as session:
        project = get_canonical_project_by_id(session, canonical_id)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"canonical_project id={canonical_id} 를 찾을 수 없습니다.",
            )

        summary_map = get_relevance_summary_by_canonical_id_map(
            session,
            user_id=current_user.id,
            canonical_project_ids=[canonical_id],
        )
        summary = summary_map.get(canonical_id)

        items: list[dict] = []
        if summary is not None:
            # 개인 판정 row (organization_id IS NULL)
            if summary.mine_personal is not None:
                m = summary.mine_personal
                items.append(
                    {
                        "organization_id": m.judgment.organization_id,
                        "organization_name": m.organization_name,
                        "verdict": m.judgment.verdict,
                        "reason": m.judgment.reason,
                        "decided_at": m.judgment.decided_at.isoformat()
                        if m.judgment.decided_at
                        else None,
                    }
                )
            # 본인이 작성한 조직 판정 rows (decided_at DESC)
            for m in summary.mine_organization:
                items.append(
                    {
                        "organization_id": m.judgment.organization_id,
                        "organization_name": m.organization_name,
                        "verdict": m.judgment.verdict,
                        "reason": m.judgment.reason,
                        "decided_at": m.judgment.decided_at.isoformat()
                        if m.judgment.decided_at
                        else None,
                    }
                )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "canonical_project_id": canonical_id,
                "items": items,
            },
        )
