"""FastAPI 로컬 열람 웹 백엔드.

활성화된 라우트:
    - GET  /                                       공고 목록 HTML 페이지 (상태 필터·검색·페이지네이션)
    - GET  /announcements                          공고 목록 JSON API
    - GET  /announcements/{id}                     공고 상세 HTML 페이지 (첨부파일 목록 포함)
    - GET  /attachments/{attachment_id}/download   로컬 파일 스트리밍 다운로드
    - GET  /register, GET /login                   회원가입/로그인 폼 HTML (Phase 1b)
    - POST /auth/register, /auth/login, /auth/logout  인증 처리 (Phase 1b)
    - GET  /auth/me                                현재 사용자 JSON (Phase 1b)

Phase 1b 에서 자유 회원가입 기반 세션 인증이 추가되었으나, 외부 노출은 여전히
금지된다. 비로그인에서도 기존 목록/상세 URL 은 그대로 동작한다.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterator
from math import ceil
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user_optional
from app.auth.routes import router as auth_router
from app.config import Settings, get_settings
from app.db.init_db import init_db
from app.db.models import Announcement, AnnouncementStatus, User
from app.db.repository import (
    count_announcements,
    count_canonical_groups,
    get_announcement_by_id,
    get_attachment_by_id,
    get_attachments_by_announcement,
    get_available_source_ids,
    get_group_size_map,
    get_read_announcement_id_set,
    list_announcements,
    list_canonical_groups,
    mark_announcement_read,
)
from app.db.session import SessionLocal

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "templates"
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

DEFAULT_PAGE_SIZE: int = 20
MAX_PAGE_SIZE: int = 100

# 허용되는 정렬 기준 값 (repository._ALLOWED_SORT_VALUES 와 일치해야 한다).
_ALLOWED_SORT_VALUES: tuple[str, ...] = ("received_desc", "deadline_asc", "title_asc")


# ──────────────────────────────────────────────────────────────
# 의존성
# ──────────────────────────────────────────────────────────────


def get_session() -> Iterator[Session]:
    """요청 단위 DB 세션 의존성."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────
# 직렬화 헬퍼
# ──────────────────────────────────────────────────────────────


def _serialize_announcement(announcement: Announcement, *, group_size: int = 1) -> dict[str, Any]:
    """Announcement ORM 인스턴스를 JSON 직렬화 가능한 dict 로 변환한다.

    datetime 은 ISO-8601 문자열로 고정하고, Enum 은 한글 value 로 보존한다.
    detail_html 은 용량이 크므로 목록 API 에서는 제외한다(상세 API 전용).

    Args:
        announcement: 직렬화할 Announcement 인스턴스.
        group_size:   동일 canonical 그룹 내 is_current=True 공고 수(자신 포함). 기본 1.
    """
    return {
        "id": announcement.id,
        "source_announcement_id": announcement.source_announcement_id,
        "source_type": announcement.source_type,
        "title": announcement.title,
        "agency": announcement.agency,
        "status": announcement.status.value,
        "received_at": (
            announcement.received_at.isoformat() if announcement.received_at else None
        ),
        "deadline_at": (
            announcement.deadline_at.isoformat() if announcement.deadline_at else None
        ),
        "detail_url": announcement.detail_url,
        "detail_fetched_at": (
            announcement.detail_fetched_at.isoformat() if announcement.detail_fetched_at else None
        ),
        "detail_fetch_status": announcement.detail_fetch_status,
        "scraped_at": announcement.scraped_at.isoformat(),
        "updated_at": announcement.updated_at.isoformat(),
        "canonical_group_id": announcement.canonical_group_id,
        "canonical_key": announcement.canonical_key,
        "group_size": group_size,
    }


def _coerce_status_query(raw_status: Optional[str]) -> Optional[AnnouncementStatus]:
    """쿼리스트링 status 값을 `AnnouncementStatus` 로 변환한다.

    허용 입력:
        - None / 빈 문자열 → None (전체 조회).
        - 한글 value("접수중"/"접수예정"/"마감").
        - name("RECEIVING"/"SCHEDULED"/"CLOSED").

    그 외 값은 400 으로 거절한다.
    """
    if raw_status is None:
        return None
    stripped = raw_status.strip()
    if not stripped:
        return None
    for member in AnnouncementStatus:
        if member.value == stripped:
            return member
    try:
        return AnnouncementStatus[stripped]
    except KeyError as exc:
        allowed_values = ", ".join(member.value for member in AnnouncementStatus)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"알 수 없는 status 값: {stripped!r}. 허용: {allowed_values}",
        ) from exc


def _coerce_source_query(
    raw_source: Optional[str],
    available_sources: list[str],
) -> Optional[str]:
    """쿼리스트링 source 값을 검증한다.

    허용 입력:
        - None / 빈 문자열 → None (전체 조회).
        - sources.yaml 에 등록된 source_id 문자열.

    그 외 값은 400 으로 거절한다.

    Args:
        raw_source:        쿼리스트링 source 값.
        available_sources: sources.yaml 에서 읽어 온 허용 소스 ID 목록.
    """
    if raw_source is None:
        return None
    stripped = raw_source.strip()
    if not stripped:
        return None
    if stripped in available_sources:
        return stripped
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"알 수 없는 소스: {stripped!r}. 허용: {', '.join(available_sources)}",
    )


def _coerce_sort_query(raw_sort: Optional[str]) -> str:
    """쿼리스트링 sort 값을 검증하고 정규화한다.

    허용 입력:
        - None / 빈 문자열 → 'received_desc' (기본값).
        - 'received_desc' / 'deadline_asc' / 'title_asc'.

    그 외 값은 400 으로 거절한다.
    """
    if raw_sort is None or not raw_sort.strip():
        return "received_desc"
    stripped = raw_sort.strip()
    if stripped not in _ALLOWED_SORT_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"알 수 없는 정렬 값: {stripped!r}. 허용: {', '.join(_ALLOWED_SORT_VALUES)}",
        )
    return stripped


# ──────────────────────────────────────────────────────────────
# 앱 팩토리
# ──────────────────────────────────────────────────────────────


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """FastAPI 앱을 생성한다.

    팩토리 형태로 제공해 테스트에서 격리된 settings 를 주입할 수 있도록 한다.
    """
    effective_settings = settings or get_settings()
    effective_settings.ensure_runtime_paths()
    init_db()

    # sources.yaml 에서 소스 목록을 한 번만 읽어 라우트 클로저에 공유한다.
    available_source_ids: list[str] = get_available_source_ids()

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    fastapi_app = FastAPI(
        title="사업공고 로컬 열람",
        description="로컬에 적재된 사업공고를 조회한다.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    fastapi_app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    # Phase 1b: 인증 라우터(register/login/logout/me) 를 mount 한다.
    # 기존 라우트와 충돌하지 않으며, /auth/* 와 /login, /register 를 노출한다.
    fastapi_app.include_router(auth_router)

    # ──────────────────────────────────────────────────────────
    # HTML: 목록 페이지
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/", response_class=HTMLResponse)
    def index_page(
        request: Request,
        status_param: Optional[str] = Query(default=None, alias="status"),
        search: Optional[str] = Query(default=None),
        source: Optional[str] = Query(default=None),
        sort: Optional[str] = Query(default=None),
        group: str = Query(default="off"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        session: Session = Depends(get_session),
        current_user: User | None = Depends(current_user_optional),
    ) -> HTMLResponse:
        """공고 목록 HTML 페이지.

        쿼리 파라미터:
            - status:    접수중 / 접수예정 / 마감 중 하나. 생략 시 전체.
            - search:    제목 부분일치(LIKE) 검색어.
            - source:    소스 유형(IRIS / NTIS 등). 생략 시 전체.
            - sort:      정렬 기준. received_desc(기본) / deadline_asc / title_asc.
            - group:     'on' 이면 canonical 묶어 보기 모드. 기본 'off'.
            - page:      1-based 페이지 번호.
            - page_size: 페이지 크기(최대 MAX_PAGE_SIZE).
        """
        status_enum = _coerce_status_query(status_param)
        source_str = _coerce_source_query(source, available_source_ids)
        sort_str = _coerce_sort_query(sort)
        group_on = group.strip().lower() == "on"
        safe_offset = (page - 1) * page_size

        if group_on:
            # ── 묶어 보기 모드: canonical 그룹 단위 ─────────────────────────────
            groups = list_canonical_groups(
                session,
                status=status_enum,
                source=source_str,
                search=search,
                sort=sort_str,
                limit=page_size,
                offset=safe_offset,
            )
            total_count = count_canonical_groups(
                session,
                status=status_enum,
                source=source_str,
                search=search,
            )
            total_pages = ceil(total_count / page_size) if total_count > 0 else 1

            # Phase 1b — 로그인 사용자에 한해 대표 공고들의 read 여부를
            # 한 번에 조회한다 (N+1 방지). 비로그인은 빈 set.
            if current_user is not None:
                representative_ids = [gr.representative.id for gr in groups]
                read_id_set = get_read_announcement_id_set(
                    session,
                    user_id=current_user.id,
                    announcement_ids=representative_ids,
                )
            else:
                read_id_set = set()

            return templates.TemplateResponse(
                request,
                "list.html",
                {
                    "group_mode": True,
                    "groups": groups,
                    "ann_with_sizes": [],
                    "total": total_count,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                    "status": status_param or "",
                    "search": search or "",
                    "source": source or "",
                    "sort": sort_str,
                    "group": "on",
                    "available_sources": available_source_ids,
                    # Phase 1b — base.html 상단 네비 + 목록 bold/normal 분기에 필요.
                    "current_user": current_user,
                    "read_id_set": read_id_set,
                },
            )
        else:
            # ── 분리 표시 모드 (기본): 각 소스 row 개별 표시 ───────────────────
            announcement_items = list_announcements(
                session,
                status=status_enum,
                limit=page_size,
                offset=safe_offset,
                search=search,
                source=source_str,
                sort=sort_str,
            )

            # group_size 일괄 조회 (N+1 방지)
            cgids = {
                ann.canonical_group_id
                for ann in announcement_items
                if ann.canonical_group_id is not None
            }
            group_size_map = get_group_size_map(session, cgids)
            ann_with_sizes = [
                (
                    ann,
                    group_size_map.get(ann.canonical_group_id, 1)
                    if ann.canonical_group_id is not None
                    else 1,
                )
                for ann in announcement_items
            ]

            total_count = count_announcements(
                session,
                status=status_enum,
                search=search,
                source=source_str,
            )
            total_pages = ceil(total_count / page_size) if total_count > 0 else 1

            # Phase 1b — 로그인 사용자의 페이지 공고별 read 여부를 한 쿼리로
            # 조회. 비로그인은 빈 set 으로 두어 템플릿이 전부 unread 클래스를
            # 달지만 body.is-anonymous CSS override 가 시각 차이를 상쇄한다.
            if current_user is not None:
                announcement_ids = [ann.id for ann, _size in ann_with_sizes]
                read_id_set = get_read_announcement_id_set(
                    session,
                    user_id=current_user.id,
                    announcement_ids=announcement_ids,
                )
            else:
                read_id_set = set()

            return templates.TemplateResponse(
                request,
                "list.html",
                {
                    "group_mode": False,
                    "groups": [],
                    "ann_with_sizes": ann_with_sizes,
                    "total": total_count,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                    "status": status_param or "",
                    "search": search or "",
                    "source": source or "",
                    "sort": sort_str,
                    "group": "off",
                    "available_sources": available_source_ids,
                    # Phase 1b — base.html 상단 네비 + 목록 bold/normal 분기에 필요.
                    "current_user": current_user,
                    "read_id_set": read_id_set,
                },
            )

    # ──────────────────────────────────────────────────────────
    # HTML: 공고 상세 페이지
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/announcements/{announcement_id}", response_class=HTMLResponse)
    def detail_page(
        request: Request,
        announcement_id: int,
        session: Session = Depends(get_session),
        current_user: User | None = Depends(current_user_optional),
    ) -> HTMLResponse:
        """공고 상세 HTML 페이지.

        내부 PK(`id`)로 공고를 조회해 DB에 저장된 `detail_html` 을 렌더한다.
        공고에 연결된 첨부파일 목록도 함께 조회해 템플릿에 전달한다.
        없는 id 는 404 로 응답한다.

        `current_user` 는 base.html 상단 네비 분기에 필요하다. 로그인 상태일
        때는 상세 진입 시 :func:`mark_announcement_read` 를 UPSERT 호출해
        ``AnnouncementUserState`` 를 갱신하고 세션을 커밋한다. 비로그인 경로는
        그대로 읽기 전용으로 유지되어 기존 URL 이 깨지지 않는다 (사용자 원문
        "비로그인 상세 진입 시 에러 없음").
        """
        announcement = get_announcement_by_id(session, announcement_id)
        if announcement is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"공고 id={announcement_id} 를 찾을 수 없습니다.",
            )

        # Phase 1b — 로그인 사용자일 때만 자동 읽음 UPSERT. announcement 단위
        # 이므로 동일 canonical 내 다른 소스 공고(예: NTIS) 는 영향받지 않는다.
        if current_user is not None:
            try:
                mark_announcement_read(
                    session,
                    user_id=current_user.id,
                    announcement_id=announcement.id,
                )
                session.commit()
            except Exception:
                # 읽음 처리 실패가 페이지 열람 자체를 망가뜨리지 않도록 방어.
                # 세션을 롤백해 다음 쿼리가 오염되지 않게 하고 경고만 기록.
                session.rollback()
                logger.warning(
                    "자동 읽음 처리 실패: user_id={} announcement_id={}",
                    current_user.id,
                    announcement.id,
                )

        attachments = get_attachments_by_announcement(session, announcement_id)

        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "announcement": announcement,
                "attachments": attachments,
                # Phase 1b — base.html 상단 네비 분기에 필요.
                "current_user": current_user,
            },
        )

    # ──────────────────────────────────────────────────────────
    # 첨부파일 로컬 다운로드
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/attachments/{attachment_id}/download")
    def attachment_download(
        attachment_id: int,
        session: Session = Depends(get_session),
    ) -> FileResponse:
        """로컬에 저장된 첨부파일을 스트리밍으로 반환한다.

        보안 검증:
            1. DB에서 첨부파일 레코드 조회 — 없으면 404.
            2. `stored_path` 가 실제 파일인지 확인 — 없으면 404.
            3. `stored_path.resolve()` 가 `settings.download_dir.resolve()` 하위인지
               `Path.is_relative_to()` 로 검증 — 벗어나면 403 (경로 트래버설 방어).

        한글 파일명은 starlette FileResponse 가 RFC 5987 `filename*=UTF-8''...` 형식으로
        Content-Disposition 헤더를 설정하므로 별도 처리가 불필요하다.
        """
        attachment = get_attachment_by_id(session, attachment_id)
        if attachment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"첨부파일 id={attachment_id} 를 찾을 수 없습니다.",
            )

        stored = Path(attachment.stored_path).resolve()
        download_root = effective_settings.download_dir.resolve()

        if not stored.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="첨부파일이 로컬에 존재하지 않습니다.",
            )

        # 경로 트래버설 방어: stored_path 가 download_dir 하위인지 검증
        if not stored.is_relative_to(download_root):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="허용되지 않는 파일 경로입니다.",
            )

        media_type, _ = mimetypes.guess_type(str(stored))
        return FileResponse(
            path=str(stored),
            filename=attachment.original_filename,
            media_type=media_type or "application/octet-stream",
        )

    # ──────────────────────────────────────────────────────────
    # JSON API: 목록
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/announcements", response_class=JSONResponse)
    def list_announcements_api(
        status_param: Optional[str] = Query(default=None, alias="status"),
        search: Optional[str] = Query(default=None),
        source: Optional[str] = Query(default=None),
        sort: Optional[str] = Query(default=None),
        group: str = Query(default="off"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """공고 목록을 JSON 으로 반환한다.

        쿼리 파라미터:
            - status:    접수중 / 접수예정 / 마감. 생략 시 전체.
            - search:    제목 부분일치(LIKE) 검색어.
            - source:    소스 유형(IRIS / NTIS 등). 생략 시 전체.
            - sort:      received_desc(기본) / deadline_asc / title_asc.
            - group:     'on' 이면 canonical 묶어 보기 모드. 기본 'off'.
            - page / page_size: 페이지네이션.

        응답 스키마:
            {
              "items":       [ {announcement + group_size + canonical_key ...}, ... ],
              "total":       전체 건수(또는 그룹 수),
              "page":        현재 페이지(1-based),
              "page_size":   페이지 크기,
              "total_pages": 전체 페이지 수,
              "group_mode":  bool
            }
        """
        status_enum = _coerce_status_query(status_param)
        source_str = _coerce_source_query(source, available_source_ids)
        sort_str = _coerce_sort_query(sort)
        group_on = group.strip().lower() == "on"
        safe_offset = (page - 1) * page_size

        if group_on:
            groups = list_canonical_groups(
                session,
                status=status_enum,
                source=source_str,
                search=search,
                sort=sort_str,
                limit=page_size,
                offset=safe_offset,
            )
            total_count = count_canonical_groups(
                session,
                status=status_enum,
                source=source_str,
                search=search,
            )
            total_pages = ceil(total_count / page_size) if total_count > 0 else 0
            items = [
                _serialize_announcement(gr.representative, group_size=gr.group_size)
                for gr in groups
            ]
        else:
            announcement_items = list_announcements(
                session,
                status=status_enum,
                limit=page_size,
                offset=safe_offset,
                search=search,
                source=source_str,
                sort=sort_str,
            )
            cgids = {
                ann.canonical_group_id
                for ann in announcement_items
                if ann.canonical_group_id is not None
            }
            group_size_map = get_group_size_map(session, cgids)
            total_count = count_announcements(
                session,
                status=status_enum,
                search=search,
                source=source_str,
            )
            total_pages = ceil(total_count / page_size) if total_count > 0 else 0
            items = [
                _serialize_announcement(
                    ann,
                    group_size=(
                        group_size_map.get(ann.canonical_group_id, 1)
                        if ann.canonical_group_id is not None
                        else 1
                    ),
                )
                for ann in announcement_items
            ]

        return {
            "items": items,
            "total": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "group_mode": group_on,
        }

    return fastapi_app


# `uvicorn app.web.main:app` 으로 바로 실행하기 위한 모듈 수준 싱글턴.
app: FastAPI = create_app()


__all__ = [
    "app",
    "create_app",
    "get_session",
    "TEMPLATES_DIR",
    "STATIC_DIR",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "_ALLOWED_SORT_VALUES",
]
