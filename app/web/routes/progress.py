"""공고 진행 상태 / 선점 라우터 (Phase C — task 00097).

엔드포인트:
    POST   /canonical/{canonical_id}/progress                 → 진행 상태 작성/갱신
    PATCH  /canonical/{canonical_id}/progress/{progress_id}    → 진행 상태 수정
    DELETE /canonical/{canonical_id}/progress/{progress_id}    → 진행 상태 삭제
    GET    /canonical/{canonical_id}/progress/history          → 이력 조회 (비로그인 허용)

설계 문서: ``docs/progress_org_design.md``.

Phase B (관련성 판정) 와의 의도적 차이:
    - **권한 = 조직 멤버 누구나** — Phase B 의 row 작성자 본인 한정과 달리,
      본인이 row 의 ``organization_id`` 에 소속되어 있기만 하면 row 작성자가
      누구든 수정·삭제 가능하다. 협업 의사결정 (작성자 휴가/퇴사 시 다른 멤버
      가 변경) 을 위한 결정.
    - row 단위 키에 ``user_id`` 가 없다 — UNIQUE ``(canonical_project_id,
      organization_id)``. 같은 조직에 대해 여러 사용자가 row 를 만들 수 없으며,
      ``created_by_user_id`` 는 \"마지막 수정자\" 메타로만 보존된다.
    - status='진행' 에 대해서만 한 canonical 당 단일 조직 선점 제약. 위반 시
      409 Conflict 응답 (설계 문서 §3.1).

응답 스키마 (UI subtask 가 직접 소비):
    POST/PATCH 응답 + GET history 모두 ``organization_id`` / ``organization_name``
    / ``status`` / ``note`` / ``updated_at`` / ``last_modifier_username`` 을
    포함한다 (guidance 명시). 비로그인 사용자가 GET history 를 호출해도 동일.

보호:
    - POST/PATCH/DELETE: ``current_user_required`` + ``ensure_same_origin``.
    - GET history: ``current_user_optional`` 만 적용 (비로그인 허용).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.auth.dependencies import (
    current_user_optional,
    current_user_required,
    ensure_same_origin,
)
from app.db.models import (
    AnnouncementProgress,
    AnnouncementProgressHistory,
    AnnouncementProgressStatus,
    User,
)
from app.db.repository import get_canonical_project_by_id
from app.db.session import session_scope
from app.organizations.service import get_user_organization_ids
from app.progress.repository import (
    PreemptionConflict,
    create_progress,
    delete_progress,
    get_progress_by_id,
    list_progress_history,
    update_progress,
)

router = APIRouter(tags=["progress"])


# ── 요청 스키마 ──────────────────────────────────────────────────────────────


# 라우터 검증 단계에서 status 도메인을 즉시 차단하기 위한 허용 집합.
# repository 의 _coerce_status 도 동일 도메인을 강제하지만, 라우터에서 422
# 를 먼저 반환하면 사용자 메시지가 더 구체적이고 트랜잭션 시작을 절약한다.
_ALLOWED_PROGRESS_STATUS_VALUES: frozenset[str] = frozenset(
    member.value for member in AnnouncementProgressStatus
)


def _validate_status_string(value: str) -> str:
    """status 문자열이 허용 도메인('관심'/'검토'/'진행'/'종료') 안에 있는지 확인한다.

    POST 와 PATCH 양쪽 Pydantic field_validator 가 공유하는 헬퍼.
    """
    if value not in _ALLOWED_PROGRESS_STATUS_VALUES:
        raise ValueError(
            f"status 는 {sorted(_ALLOWED_PROGRESS_STATUS_VALUES)} 중 하나여야 합니다."
        )
    return value


class ProgressCreateIn(BaseModel):
    """POST /canonical/{id}/progress 요청 본문 스키마.

    Attributes:
        organization_id: 입장 표명 조직 PK (필수). 본인 소속이 아니면 422.
        status: 4 단계 한글 enum 문자열.
        note: 자유 메모. 빈 문자열 / None 허용.
    """

    organization_id: int
    status: str
    note: str | None = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str) -> str:
        """status 값이 허용 도메인 안에 있는지 확인한다."""
        return _validate_status_string(value)


class ProgressUpdateIn(BaseModel):
    """PATCH /canonical/{id}/progress/{progress_id} 요청 본문 스키마.

    organization_id 는 path / 기존 row 로 결정되므로 body 에 받지 않는다.

    Attributes:
        status: 4 단계 한글 enum 문자열.
        note: 자유 메모. None 이면 NULL 로 저장.
    """

    status: str
    note: str | None = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str) -> str:
        """status 값이 허용 도메인 안에 있는지 확인한다."""
        return _validate_status_string(value)


# ── 응답 직렬화 헬퍼 ─────────────────────────────────────────────────────────


def _serialize_progress(progress: AnnouncementProgress) -> dict:
    """AnnouncementProgress 를 JSON 응답 dict 로 직렬화한다.

    UI subtask 가 직접 소비하는 핵심 응답 스키마 — guidance 가 요구한 모든
    필드 (organization_id / organization_name / status / note / updated_at /
    last_modifier_username) 를 포함한다.

    relationship lazy='select' 로 organization / created_by 가 첫 접근 시
    즉시 추가 SELECT 를 발행한다. 페이지당 1 ~ 2 회 노출이라 N+1 부담 없음.
    """
    organization = progress.organization
    last_modifier = progress.created_by
    return {
        "id": progress.id,
        "canonical_project_id": progress.canonical_project_id,
        "organization_id": progress.organization_id,
        "organization_name": organization.name if organization is not None else None,
        "status": (
            progress.status.value
            if isinstance(progress.status, AnnouncementProgressStatus)
            else progress.status
        ),
        "note": progress.note,
        "updated_at": (
            progress.updated_at.isoformat() if progress.updated_at is not None else None
        ),
        "created_at": (
            progress.created_at.isoformat() if progress.created_at is not None else None
        ),
        "last_modifier_user_id": progress.created_by_user_id,
        "last_modifier_username": (
            last_modifier.username if last_modifier is not None else None
        ),
    }


def _serialize_progress_history(history_row: AnnouncementProgressHistory) -> dict:
    """AnnouncementProgressHistory 를 JSON 응답 dict 로 직렬화한다.

    GET /canonical/{id}/progress/history 응답의 항목 단위. 비로그인 사용자도
    동일하게 노출된다.
    """
    organization = history_row.organization
    last_modifier = history_row.created_by
    return {
        "id": history_row.id,
        "canonical_project_id": history_row.canonical_project_id,
        "organization_id": history_row.organization_id,
        "organization_name": organization.name if organization is not None else None,
        "status": (
            history_row.status.value
            if isinstance(history_row.status, AnnouncementProgressStatus)
            else history_row.status
        ),
        "note": history_row.note,
        "updated_at": (
            history_row.updated_at.isoformat()
            if history_row.updated_at is not None
            else None
        ),
        "archived_at": (
            history_row.archived_at.isoformat()
            if history_row.archived_at is not None
            else None
        ),
        "archive_reason": (
            history_row.archive_reason.value
            if hasattr(history_row.archive_reason, "value")
            else history_row.archive_reason
        ),
        "last_modifier_user_id": history_row.created_by_user_id,
        "last_modifier_username": (
            last_modifier.username if last_modifier is not None else None
        ),
    }


# ── 권한 헬퍼 ────────────────────────────────────────────────────────────────


def _ensure_canonical_exists(session, canonical_id: int) -> None:
    """canonical_project 가 존재하지 않으면 404 를 던진다."""
    project = get_canonical_project_by_id(session, canonical_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"canonical_project id={canonical_id} 를 찾을 수 없습니다.",
        )


def _ensure_member_of_organization(
    session, *, user_id: int, organization_id: int
) -> None:
    """본인이 organization_id 에 소속되어 있는지 검증한다.

    Phase C 권한 매트릭스 (설계 문서 §3.1):
        - 무소속 사용자 (user_organizations 매핑 0 개) → 422.
        - 본인 소속이 아닌 organization_id → 403 (PATCH/DELETE 흐름) 또는 422
          (POST 의 body.organization_id 검증). 본 함수는 호출자가 status code 를
          별도 분기 처리할 수 있도록 ``OrganizationMembershipError`` 대신 단순한
          쌍 (멤버 여부 + my_org_ids) 을 반환하지 않고 boolean 한 줄로 끝난다.

    호출자가 적절한 4xx 매핑을 결정한다.

    Args:
        session: 호출자 세션.
        user_id: current_user.id.
        organization_id: 검증 대상 조직 PK.

    Returns:
        검증 통과면 None. 실패는 raise.

    Raises:
        HTTPException(422): 무소속 사용자.
        HTTPException(403): 본인 소속 외 조직.
    """
    my_organization_ids = get_user_organization_ids(session, user_id)
    if not my_organization_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="소속된 조직이 없습니다. 진행 상태를 작성·수정·삭제하려면 조직에 가입되어 있어야 합니다.",
        )
    if organization_id not in my_organization_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="본인 소속 조직이 아닙니다.",
        )


def _ensure_member_for_post(
    session, *, user_id: int, organization_id: int
) -> None:
    """POST 흐름의 본인 소속 검증.

    PATCH/DELETE 와 다르게 POST 는 body 의 organization_id 가 본인 소속이 아닌
    경우 **422 (Unprocessable)** 로 응답한다 — Phase B (관련성) 의 비-멤버 응답
    코드와 일관된다. 무소속 사용자도 동일하게 422.
    """
    my_organization_ids = get_user_organization_ids(session, user_id)
    if organization_id not in my_organization_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="본인 소속 조직이 아닙니다.",
        )


# ── 엔드포인트 ───────────────────────────────────────────────────────────────


@router.post(
    "/canonical/{canonical_id}/progress",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def create_progress_route(
    canonical_id: int,
    body: ProgressCreateIn,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """canonical 에 본인 조직 입장으로 진행 상태를 등록한다.

    동작:
        - 같은 (canonical, organization) row 가 이미 있으면 repository 가
          history 이관 후 in-place UPDATE 로 위임 (POST 의 멱등성).
        - status='진행' 인데 다른 조직이 이미 진행 중이면 ``PreemptionConflict``
          → 409 Conflict (설계 문서 §3.1).

    응답: 200 + ``_serialize_progress`` 형식.
    """
    with session_scope() as session:
        _ensure_canonical_exists(session, canonical_id)
        # POST 의 body.organization_id 본인 소속 검증 — 무소속·외부 조직 모두 422.
        _ensure_member_for_post(
            session,
            user_id=current_user.id,
            organization_id=body.organization_id,
        )
        try:
            progress = create_progress(
                session,
                canonical_project_id=canonical_id,
                organization_id=body.organization_id,
                status=body.status,
                note=body.note,
                created_by_user_id=current_user.id,
            )
        except PreemptionConflict as conflict:
            # 다른 조직이 이미 진행 중인 경우 — 설계 문서 §3.1 에 따라 409.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(conflict),
            ) from conflict
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_serialize_progress(progress),
        )


@router.patch(
    "/canonical/{canonical_id}/progress/{progress_id}",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def update_progress_route(
    canonical_id: int,
    progress_id: int,
    body: ProgressUpdateIn,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """본인 소속 조직의 진행 상태를 수정한다 (조직 멤버 누구나 권한).

    권한 분기 (설계 문서 §3.1):
        - 무소속 → 422.
        - 본인 소속 외 조직 row → 403.
        - 본인 소속 조직 row → 작성자 무관 허용 (Phase B 와의 차이!).

    선점 검증: status='진행' 으로 변경하는 경우만 다른 조직의 진행 row 와 충돌
    여부를 SELECT 후 거부 (PreemptionConflict → 409).
    """
    with session_scope() as session:
        _ensure_canonical_exists(session, canonical_id)
        progress = get_progress_by_id(session, progress_id)
        if progress is None or progress.canonical_project_id != canonical_id:
            # canonical 과 progress 가 짝이 안 맞으면 404 — URL 단의 path
            # 일관성을 강제 (다른 canonical 의 progress 를 들여다보지 못하게).
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"progress id={progress_id} 를 찾을 수 없습니다.",
            )
        # 본인 소속 조직 검증 — 무소속 422, 외부 조직 403.
        _ensure_member_of_organization(
            session,
            user_id=current_user.id,
            organization_id=progress.organization_id,
        )
        try:
            updated = update_progress(
                session,
                progress_id=progress_id,
                status=body.status,
                note=body.note,
                modifier_user_id=current_user.id,
            )
        except PreemptionConflict as conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(conflict),
            ) from conflict
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_serialize_progress(updated),
        )


@router.delete(
    "/canonical/{canonical_id}/progress/{progress_id}",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def delete_progress_route(
    canonical_id: int,
    progress_id: int,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """본인 소속 조직의 진행 상태 row 를 history 이관 후 삭제한다.

    권한은 PATCH 와 동일 — 무소속 422 / 본인 소속 외 조직 403 / 본인 소속 조직
    row 는 작성자 무관 허용.

    Returns:
        200 + ``{detail: \"삭제되었습니다.\"}`` 또는 404 (없는 progress_id).
    """
    with session_scope() as session:
        _ensure_canonical_exists(session, canonical_id)
        progress = get_progress_by_id(session, progress_id)
        if progress is None or progress.canonical_project_id != canonical_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"progress id={progress_id} 를 찾을 수 없습니다.",
            )
        _ensure_member_of_organization(
            session,
            user_id=current_user.id,
            organization_id=progress.organization_id,
        )
        deleted = delete_progress(
            session,
            progress_id=progress_id,
            modifier_user_id=current_user.id,
        )
        if not deleted:
            # get_progress_by_id 가 None 이 아닐 때만 여기까지 오므로 거의 발생하지
            # 않지만, 동시성 안전망용.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="삭제할 진행 상태가 없습니다.",
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"detail": "삭제되었습니다."},
        )


@router.get(
    "/canonical/{canonical_id}/progress/history",
    status_code=status.HTTP_200_OK,
)
def get_progress_history_route(
    canonical_id: int,
    current_user: User | None = Depends(current_user_optional),
) -> JSONResponse:
    """진행 상태 이력을 archived_at 내림차순으로 반환한다 (비로그인 허용).

    Phase B GET history 와 동일 정책 — 비로그인 사용자도 동일하게 노출.
    Returns:
        200 + ``{canonical_project_id, history: [...]}``. canonical 이 없으면 404.
    """
    # current_user 는 본 핸들러의 응답에 영향을 주지 않는다 — 비로그인도 동일 응답.
    _ = current_user
    with session_scope() as session:
        _ensure_canonical_exists(session, canonical_id)
        rows = list_progress_history(session, canonical_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "canonical_project_id": canonical_id,
                "history": [_serialize_progress_history(row) for row in rows],
            },
        )
