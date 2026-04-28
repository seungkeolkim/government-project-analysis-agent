"""즐겨찾기 폴더/항목 API 라우터 (Phase 3b / task 00037 announcement 단위 재정합).

엔드포인트:
    GET    /favorites/folders                              → 폴더 트리 조회
    POST   /favorites/folders                              → 폴더 생성
    PATCH  /favorites/folders/{folder_id}                  → 폴더 이름 변경
    GET    /favorites/folders/{folder_id}/delete-preview   → 삭제 시 cascade 개수 미리보기
    DELETE /favorites/folders/{folder_id}                  → 폴더 삭제 (cascade: 하위 폴더·공고 모두)
    POST   /favorites/entries                              → 즐겨찾기 항목 추가 (announcement 단위)
                                                            - apply_to_all_siblings=True 이면
                                                              동일 canonical 의 is_current 공고 일괄 등록
    PATCH  /favorites/entries/{entry_id}                   → 즐겨찾기 항목 폴더 이동
    DELETE /favorites/entries/{entry_id}                   → 즐겨찾기 항목 제거
    GET    /favorites/folders/{folder_id}/entries          → 폴더 내 항목 목록 (페이지네이션)

보호:
    GET /favorites/folders, GET /favorites/folders/{id}/entries,
    GET /favorites/folders/{id}/delete-preview:
        current_user_required (읽기 전용, Origin 체크 생략)
    POST/PATCH/DELETE:
        current_user_required + ensure_same_origin
    자기 폴더·항목만 조회/수정 가능. 타 사용자 리소스 접근 시 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError

from app.auth.dependencies import current_user_required, ensure_same_origin
from app.db.models import (
    Announcement,
    FavoriteEntry,
    FavoriteFolder,
    User,
)
from app.db.repository import (
    count_folder_delete_cascade,
    get_current_sibling_announcement_ids,
    list_favorites_with_announcements,
)
from app.db.session import session_scope

router = APIRouter(tags=["favorites"])

# 에러 메시지 상수 — UI 에서 직접 표시하므로 한국어로 고정.
_FOLDER_NOT_FOUND = "폴더를 찾을 수 없습니다."
_ENTRY_NOT_FOUND = "즐겨찾기 항목을 찾을 수 없습니다."
_FOLDER_DEPTH_LIMIT = "폴더는 최대 2단계까지만 허용됩니다."
_FOLDER_NAME_CONFLICT = "같은 이름의 폴더가 이미 존재합니다."
_ANNOUNCEMENT_NOT_FOUND = "공고를 찾을 수 없습니다."


# ---------------------------------------------------------------------------
# Request body models
# ---------------------------------------------------------------------------


class FolderCreateIn(BaseModel):
    """폴더 생성 요청 body."""

    name: str
    parent_id: int | None = None


class FolderRenameIn(BaseModel):
    """폴더 이름 변경 요청 body."""

    name: str


class EntryCreateIn(BaseModel):
    """즐겨찾기 항목 추가 요청 body (task 00037 announcement 단위).

    apply_to_all_siblings:
        - False (기본): announcement_id 1건만 등록. 사용자 원문 #4 \"별표를 누른
          그 공고가 반드시 등록\" 에 해당하는 기본 플로우.
        - True: announcement_id 의 canonical_group 에 속한 is_current 공고 전체를
          일괄 등록. 별표를 누른 공고 자체도 반드시 포함된다.
    """

    folder_id: int
    announcement_id: int
    apply_to_all_siblings: bool = False


class EntryMoveIn(BaseModel):
    """즐겨찾기 항목 폴더 이동 요청 body."""

    target_folder_id: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_folder_tree(folders: list[FavoriteFolder]) -> list[dict]:
    """FavoriteFolder 목록을 중첩 트리 구조로 변환한다.

    루트(parent_id=None) 가 최상위, depth 1 폴더들이 children 에 배치된다.
    depth 2 제약상 최대 1단계 중첩만 발생한다.

    Args:
        folders: 특정 사용자의 전체 FavoriteFolder 목록 (depth 오름차순 정렬 권장).

    Returns:
        [{\"id\": int, \"name\": str, \"depth\": int, \"parent_id\": int|None,
         \"children\": [{...}]}, ...]
    """
    nodes: dict[int, dict] = {
        f.id: {
            "id": f.id,
            "name": f.name,
            "depth": f.depth,
            "parent_id": f.parent_id,
            "children": [],
        }
        for f in folders
    }
    roots: list[dict] = []
    for f in folders:
        node = nodes[f.id]
        if f.parent_id is None:
            roots.append(node)
        elif f.parent_id in nodes:
            nodes[f.parent_id]["children"].append(node)
    return roots


def _get_owned_folder_or_404(
    session,
    folder_id: int,
    user_id: int,
) -> FavoriteFolder:
    """folder_id 에 해당하는 폴더를 조회한다. 없거나 소유자 불일치 시 404.

    타 사용자 폴더 존재 여부를 노출하지 않기 위해 두 경우 모두 404 반환.
    """
    folder = session.get(FavoriteFolder, folder_id)
    if folder is None or folder.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_FOLDER_NOT_FOUND,
        )
    return folder


def _get_owned_entry_or_404(
    session,
    entry_id: int,
    user_id: int,
) -> FavoriteEntry:
    """entry_id 에 해당하는 항목을 조회한다. 없거나 소유자 폴더 불일치 시 404.

    항목 → 폴더 → user_id 순으로 소유권을 검증한다.
    타 사용자 항목 존재 여부를 노출하지 않기 위해 두 경우 모두 404 반환.
    """
    entry = session.get(FavoriteEntry, entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_ENTRY_NOT_FOUND,
        )
    folder = session.get(FavoriteFolder, entry.folder_id)
    if folder is None or folder.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_ENTRY_NOT_FOUND,
        )
    return entry


# ---------------------------------------------------------------------------
# 폴더 엔드포인트
# ---------------------------------------------------------------------------


@router.get(
    "/favorites/folders",
    status_code=status.HTTP_200_OK,
)
def list_folders(
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """현재 사용자의 즐겨찾기 폴더 전체를 트리 구조로 반환한다."""
    with session_scope() as session:
        rows = session.execute(
            select(FavoriteFolder)
            .where(FavoriteFolder.user_id == current_user.id)
            .order_by(FavoriteFolder.depth, FavoriteFolder.created_at)
        ).scalars().all()
        return JSONResponse({"folders": _build_folder_tree(list(rows))})


@router.post(
    "/favorites/folders",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_201_CREATED,
)
def create_folder(
    body: FolderCreateIn,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """즐겨찾기 폴더를 생성한다.

    depth 2 초과 시도 → 400, 동명 폴더 충돌 → 409.
    depth 및 parent_id 소유권은 서버에서 검증한다.
    """
    with session_scope() as session:
        # 부모 폴더 지정 시 소유권 확인
        if body.parent_id is not None:
            parent = session.get(FavoriteFolder, body.parent_id)
            if parent is None or parent.user_id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_FOLDER_NOT_FOUND,
                )
        else:
            # 루트 폴더 동명 중복 앱 레벨 체크.
            # UNIQUE(user_id, parent_id, name) 는 parent_id IS NULL 인 행에서
            # SQLite/Postgres 모두 NULL 을 "서로 다름"으로 취급해 중복을 허용한다.
            # DB 제약 대신 INSERT 전 SELECT 로 직접 확인한다.
            existing = session.execute(
                select(FavoriteFolder).where(
                    FavoriteFolder.user_id == current_user.id,
                    FavoriteFolder.parent_id.is_(None),
                    FavoriteFolder.name == body.name,
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_FOLDER_NAME_CONFLICT,
                )
        try:
            folder = FavoriteFolder(
                user_id=current_user.id,
                name=body.name,
                parent_id=body.parent_id,
                # depth 는 before_insert 이벤트 리스너(_enforce_favorite_folder_depth) 가 계산
            )
            session.add(folder)
            session.flush()
        except (ValueError, StatementError) as exc:
            # SQLAlchemy 는 이벤트 리스너 예외를 StatementError 로 감싸는 경우가 있다.
            orig: BaseException = exc.__cause__ if exc.__cause__ is not None else exc
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(orig) or _FOLDER_DEPTH_LIMIT,
            )
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_FOLDER_NAME_CONFLICT,
            )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "id": folder.id,
                "name": folder.name,
                "depth": folder.depth,
                "parent_id": folder.parent_id,
                "created_at": folder.created_at.isoformat(),
            },
        )


@router.patch(
    "/favorites/folders/{folder_id}",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def rename_folder(
    folder_id: int,
    body: FolderRenameIn,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """즐겨찾기 폴더 이름을 변경한다. 동명 충돌 → 409."""
    with session_scope() as session:
        folder = _get_owned_folder_or_404(session, folder_id, current_user.id)
        folder.name = body.name
        try:
            session.flush()
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_FOLDER_NAME_CONFLICT,
            )
        return JSONResponse(
            content={
                "id": folder.id,
                "name": folder.name,
                "depth": folder.depth,
                "parent_id": folder.parent_id,
            }
        )


@router.get(
    "/favorites/folders/{folder_id}/delete-preview",
    status_code=status.HTTP_200_OK,
)
def preview_folder_delete(
    folder_id: int,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """폴더 삭제 시 cascade 로 함께 지워질 서브폴더·공고 수를 미리 반환한다.

    UI 는 이 응답으로 삭제 확인 모달에 \"하위 서브그룹 N개, 공고 M건이 함께
    삭제됩니다\" 경고를 표시한다. 실제 삭제는 수행하지 않는다(멱등).

    Returns:
        ``{\"folder_id\": int, \"folder_name\": str, \"subfolder_count\": int,
            \"entry_count\": int}``
    """
    with session_scope() as session:
        folder = _get_owned_folder_or_404(session, folder_id, current_user.id)
        counts = count_folder_delete_cascade(session, folder_id=folder_id)
        return JSONResponse(
            {
                "folder_id": folder.id,
                "folder_name": folder.name,
                "subfolder_count": counts["subfolder_count"],
                "entry_count": counts["entry_count"],
            }
        )


@router.delete(
    "/favorites/folders/{folder_id}",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def delete_folder(
    folder_id: int,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """즐겨찾기 폴더를 삭제한다 (task 00037 — 자식 폴더·공고까지 cascade).

    스키마 변경 (migration c4a8d1e7b2f3):
        - favorite_folders.parent_id FK ondelete = CASCADE
        - favorite_entries.folder_id FK ondelete = CASCADE (기존 유지)

    ORM 레벨에서도 FavoriteFolder.children 에 cascade=\"all, delete\" 가 설정되어
    있어 SQLite PRAGMA foreign_keys 설정 여부와 무관하게 자식 폴더·공고가 모두
    세션에서 재귀 삭제된다. 사용자 원문 #2 \"격상 없음\" 보장.
    """
    with session_scope() as session:
        folder = _get_owned_folder_or_404(session, folder_id, current_user.id)
        session.delete(folder)
        return JSONResponse({"detail": "삭제되었습니다."})


# ---------------------------------------------------------------------------
# 항목 엔드포인트
# ---------------------------------------------------------------------------


@router.post(
    "/favorites/entries",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_201_CREATED,
)
def create_entry(
    body: EntryCreateIn,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """즐겨찾기 항목을 추가한다 (task 00037 — announcement 단위).

    동작:
        1. 폴더 소유권 검증 (타인 폴더 → 404).
        2. announcement 존재 검증.
        3. apply_to_all_siblings=False (기본) → body.announcement_id 1건만 INSERT.
        4. apply_to_all_siblings=True → 동일 canonical 그룹의 is_current 공고 전체를
           IN 절 단일 SELECT 로 조회 후, 이미 이 폴더에 존재하는 announcement_id 들을
           제외하고 나머지를 일괄 INSERT. 이미 존재하던 announcement_id 들은
           ``skipped_announcement_ids`` 로 응답. 사용자 원문 \"별표를 누른 그 공고가
           반드시 등록\" 을 보장하기 위해 body.announcement_id 가 sibling 목록에 포함
           되어 있는지 서버 레벨에서 재확인한다.

    응답 (201):
        {
            \"created_entries\": [
                {\"id\": int, \"folder_id\": int, \"announcement_id\": int,
                 \"added_at\": str}, ...
            ],
            \"skipped_announcement_ids\": [int, ...],
            \"applied_to_all_siblings\": bool,
        }

    에러:
        - 폴더 없음/타인 폴더  → 404
        - 공고 없음            → 404
        - 단건 호출에서 이미 존재 → 200 (skipped 리스트로 응답, 409 대신).
          \"이미 등록된 공고를 다시 등록\" 요청은 사용자 관점 에러가 아니다.
    """
    with session_scope() as session:
        # 폴더 소유권 확인
        folder = session.get(FavoriteFolder, body.folder_id)
        if folder is None or folder.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_FOLDER_NOT_FOUND,
            )

        # announcement 존재 확인
        requested_ann = session.get(Announcement, body.announcement_id)
        if requested_ann is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_ANNOUNCEMENT_NOT_FOUND,
            )

        # 등록 대상 announcement_id 목록 결정.
        # 단건이든 bulk 이든 body.announcement_id 는 반드시 포함되어야 한다.
        if body.apply_to_all_siblings:
            target_ids = get_current_sibling_announcement_ids(
                session, announcement_id=body.announcement_id
            )
        else:
            target_ids = [body.announcement_id]

        # 방어적 재검증 — 의도와 달리 대상 목록에서 본인 announcement_id 가 빠졌다면
        # 강제로 포함시킨다. 사용자 원문 #4 \"별표를 누른 그 공고가 반드시 등록\" 보장.
        if body.announcement_id not in target_ids:
            target_ids = sorted(set(target_ids) | {body.announcement_id})

        # 동일 폴더에 이미 있는 announcement_id 는 skipped 처리 (409 대신 200/201 흐름).
        existing_ids = {
            row[0]
            for row in session.execute(
                select(FavoriteEntry.announcement_id).where(
                    FavoriteEntry.folder_id == body.folder_id,
                    FavoriteEntry.announcement_id.in_(target_ids),
                )
            ).all()
        }
        to_create = [aid for aid in target_ids if aid not in existing_ids]

        created: list[dict] = []
        try:
            for aid in to_create:
                entry = FavoriteEntry(
                    folder_id=body.folder_id,
                    announcement_id=aid,
                )
                session.add(entry)
            session.flush()
        except IntegrityError:
            # 동시성 경계 — 같은 (folder_id, announcement_id) 중복. UI 충돌을 줄이기 위해
            # 500 대신 400 으로 완만하게 응답한다. 재시도하면 skipped 경로로 흡수된다.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="즐겨찾기 추가 도중 충돌이 발생했습니다. 다시 시도해주세요.",
            )

        # flush 이후 id 가 할당된 entry 를 응답용으로 재수집.
        # 생성된 (folder_id, announcement_id) 순으로 조회해 결정적 순서를 보장.
        if to_create:
            created_rows = session.execute(
                select(FavoriteEntry).where(
                    FavoriteEntry.folder_id == body.folder_id,
                    FavoriteEntry.announcement_id.in_(to_create),
                )
            ).scalars().all()
            created = [
                {
                    "id": e.id,
                    "folder_id": e.folder_id,
                    "announcement_id": e.announcement_id,
                    "added_at": e.added_at.isoformat() if e.added_at else None,
                }
                for e in created_rows
            ]

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "created_entries": created,
                "skipped_announcement_ids": sorted(existing_ids),
                "applied_to_all_siblings": body.apply_to_all_siblings,
            },
        )


@router.patch(
    "/favorites/entries/{entry_id}",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def move_entry(
    entry_id: int,
    body: EntryMoveIn,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """즐겨찾기 항목을 다른 폴더로 이동한다 (task 00037 #3).

    기능:
        - entry_id 의 folder_id 를 body.target_folder_id 로 갱신.
        - 양쪽 폴더 모두 현재 사용자 소유여야 한다(아니면 404).
        - 대상 폴더에 이미 같은 announcement_id 가 있으면 409
          (UNIQUE(folder_id, announcement_id) 충돌).

    드래그 앤 드롭은 구현하지 않는다 — UI 는 모달 기반 폴더 선택 재사용
    (사용자 원문 \"드래그 앤 드롭은 구현하지 않음\").
    """
    with session_scope() as session:
        entry = _get_owned_entry_or_404(session, entry_id, current_user.id)
        # 대상 폴더 소유권도 동일 사용자여야 한다.
        target_folder = _get_owned_folder_or_404(
            session, body.target_folder_id, current_user.id
        )

        # 같은 폴더로의 이동은 멱등 처리 (no-op + 200).
        if entry.folder_id == target_folder.id:
            return JSONResponse(
                content={
                    "id": entry.id,
                    "folder_id": entry.folder_id,
                    "announcement_id": entry.announcement_id,
                    "moved": False,
                }
            )

        entry.folder_id = target_folder.id
        try:
            session.flush()
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이동 대상 폴더에 이미 같은 공고가 있습니다.",
            )
        return JSONResponse(
            content={
                "id": entry.id,
                "folder_id": entry.folder_id,
                "announcement_id": entry.announcement_id,
                "moved": True,
            }
        )


@router.delete(
    "/favorites/entries/{entry_id}",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def delete_entry(
    entry_id: int,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """즐겨찾기 항목을 제거한다."""
    with session_scope() as session:
        entry = _get_owned_entry_or_404(session, entry_id, current_user.id)
        session.delete(entry)
        return JSONResponse({"detail": "삭제되었습니다."})


@router.get(
    "/favorites/folders/{folder_id}/entries",
    status_code=status.HTTP_200_OK,
)
def list_folder_entries(
    folder_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """폴더 내 즐겨찾기 항목 목록을 페이지네이션으로 반환한다 (announcement 단위).

    repository.list_favorites_with_announcements 로 announcement + canonical
    그룹 배치 조회를 수행한다 (N+1 방지).

    응답 items 각 원소:
        entry_id, announcement_id, ann_title, canonical_title,
        ann_agency, ann_source_type, ann_status, ann_deadline_at,
        canonical_project_id, added_at
    """
    with session_scope() as session:
        _get_owned_folder_or_404(session, folder_id, current_user.id)
        items, total = list_favorites_with_announcements(
            session,
            folder_id=folder_id,
            page=page,
            page_size=page_size,
        )
        # JSON 직렬화를 위해 datetime/enum 을 문자열화.
        def _jsonable(item: dict) -> dict:
            """응답 JSON 직렬화용으로 datetime/enum 을 문자열로 정규화한다."""
            result = dict(item)
            if result.get("ann_deadline_at") is not None:
                result["ann_deadline_at"] = result["ann_deadline_at"].isoformat()
            if result.get("added_at") is not None:
                result["added_at"] = result["added_at"].isoformat()
            if result.get("ann_status") is not None:
                # AnnouncementStatus enum 은 .value 로 한글 라벨을 갖는다.
                status_value = result["ann_status"]
                result["ann_status"] = (
                    status_value.value
                    if hasattr(status_value, "value")
                    else status_value
                )
            return result

        return JSONResponse(
            {
                "folder_id": folder_id,
                "page": page,
                "page_size": page_size,
                "total": total,
                "items": [_jsonable(it) for it in items],
            }
        )
