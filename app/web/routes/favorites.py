"""즐겨찾기 폴더/항목 API 라우터 (Phase 3b / 00036-4).

엔드포인트:
    GET    /favorites/folders                       → 폴더 트리 조회
    POST   /favorites/folders                       → 폴더 생성
    PATCH  /favorites/folders/{folder_id}           → 폴더 이름 변경
    DELETE /favorites/folders/{folder_id}           → 폴더 삭제 (cascade)
    POST   /favorites/entries                       → 즐겨찾기 항목 추가
    DELETE /favorites/entries/{entry_id}            → 즐겨찾기 항목 제거
    GET    /favorites/folders/{folder_id}/entries   → 폴더 내 항목 목록 (페이지네이션)

보호:
    GET /favorites/folders, GET /favorites/folders/{id}/entries:
        current_user_required (읽기 전용, Origin 체크 생략)
    POST/PATCH/DELETE:
        current_user_required + ensure_same_origin
    자기 폴더만 조회/수정 가능. 타 사용자 폴더 접근 시 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, StatementError

from app.auth.dependencies import current_user_required, ensure_same_origin
from app.db.models import (
    CanonicalProject,
    FavoriteEntry,
    FavoriteFolder,
    User,
)
from app.db.session import session_scope

router = APIRouter(tags=["favorites"])

# 에러 메시지 상수 — UI 에서 직접 표시하므로 한국어로 고정.
_FOLDER_NOT_FOUND = "폴더를 찾을 수 없습니다."
_ENTRY_NOT_FOUND = "즐겨찾기 항목을 찾을 수 없습니다."
_FOLDER_DEPTH_LIMIT = "폴더는 최대 2단계까지만 허용됩니다."
_FOLDER_NAME_CONFLICT = "같은 이름의 폴더가 이미 존재합니다."
_ENTRY_DUPLICATE = "이미 즐겨찾기에 추가된 과제입니다."
_CANONICAL_NOT_FOUND = "과제를 찾을 수 없습니다."


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
    """즐겨찾기 항목 추가 요청 body."""

    folder_id: int
    canonical_project_id: int


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
        [{\"id\": int, \"name\": str, \"depth\": int, \"children\": [{...}]}, ...]
    """
    nodes: dict[int, dict] = {
        f.id: {"id": f.id, "name": f.name, "depth": f.depth, "children": []}
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


@router.delete(
    "/favorites/folders/{folder_id}",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def delete_folder(
    folder_id: int,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """즐겨찾기 폴더를 삭제한다.

    FavoriteEntry(favorite_entries FK CASCADE) 는 함께 삭제된다.
    자식 FavoriteFolder(favorite_folders FK SET NULL) 는 parent_id=NULL 로 변경된다.
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
    """즐겨찾기 항목을 추가한다.

    canonical 단위로 저장: IRIS/NTIS 동일 과제는 같은 canonical_project_id 를 가지므로
    어느 소스에서 저장해도 중복이 되지 않는다.
    폴더 소유자 불일치 → 404, 중복 추가 → 409.
    """
    with session_scope() as session:
        # 폴더 소유권 확인
        folder = session.get(FavoriteFolder, body.folder_id)
        if folder is None or folder.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_FOLDER_NOT_FOUND,
            )
        # canonical_project 존재 확인
        cp = session.get(CanonicalProject, body.canonical_project_id)
        if cp is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=_CANONICAL_NOT_FOUND,
            )
        try:
            entry = FavoriteEntry(
                folder_id=body.folder_id,
                canonical_project_id=body.canonical_project_id,
            )
            session.add(entry)
            session.flush()
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_ENTRY_DUPLICATE,
            )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "id": entry.id,
                "folder_id": entry.folder_id,
                "canonical_project_id": entry.canonical_project_id,
                "added_at": entry.added_at.isoformat() if entry.added_at else None,
            },
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
    """폴더 내 즐겨찾기 항목 목록을 페이지네이션으로 반환한다.

    canonical_title 은 CanonicalProject.representative_title 로 채운다.
    N+1 방지: canonical_project_id 목록을 IN 절 단일 쿼리로 조회.
    """
    with session_scope() as session:
        _get_owned_folder_or_404(session, folder_id, current_user.id)

        total_count: int = session.execute(
            select(func.count()).select_from(FavoriteEntry).where(
                FavoriteEntry.folder_id == folder_id
            )
        ).scalar_one()

        entries = session.execute(
            select(FavoriteEntry)
            .where(FavoriteEntry.folder_id == folder_id)
            .order_by(FavoriteEntry.added_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars().all()

        # canonical 제목 배치 조회 (IN 1회)
        canonical_ids = [e.canonical_project_id for e in entries]
        canonical_map: dict[int, CanonicalProject] = {}
        if canonical_ids:
            cp_rows = session.execute(
                select(CanonicalProject).where(CanonicalProject.id.in_(canonical_ids))
            ).scalars().all()
            canonical_map = {cp.id: cp for cp in cp_rows}

        items = [
            {
                "id": e.id,
                "canonical_project_id": e.canonical_project_id,
                "canonical_title": (
                    canonical_map[e.canonical_project_id].representative_title
                    if e.canonical_project_id in canonical_map
                    else None
                ),
                "added_at": e.added_at.isoformat() if e.added_at else None,
            }
            for e in entries
        ]

        return JSONResponse(
            {
                "folder_id": folder_id,
                "page": page,
                "page_size": page_size,
                "total": total_count,
                "items": items,
            }
        )
