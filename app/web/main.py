"""FastAPI 로컬 열람 웹 백엔드.

현재 활성화된 라우트:
    - GET /                공고 목록 HTML 페이지 (상태 필터·검색·페이지네이션)
    - GET /announcements   공고 목록 JSON API

비활성화(상세·첨부 기능은 별도 subtask에서 활성화 예정):
    - 공고 상세 HTML/JSON
    - 첨부파일 다운로드

로컬 전용. 인증이 없으므로 외부 노출 금지.
"""

from __future__ import annotations

from collections.abc import Iterator
from math import ceil
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.init_db import init_db
from app.db.models import Announcement, AnnouncementStatus
from app.db.repository import (
    count_announcements,
    list_announcements,
)
from app.db.session import SessionLocal

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "templates"
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

DEFAULT_PAGE_SIZE: int = 20
MAX_PAGE_SIZE: int = 100


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


def _serialize_announcement(announcement: Announcement) -> dict[str, Any]:
    """Announcement ORM 인스턴스를 JSON 직렬화 가능한 dict 로 변환한다.

    datetime 은 ISO-8601 문자열로 고정하고, Enum 은 한글 value 로 보존한다.
    """
    return {
        "id": announcement.id,
        "iris_announcement_id": announcement.iris_announcement_id,
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
        "scraped_at": announcement.scraped_at.isoformat(),
        "updated_at": announcement.updated_at.isoformat(),
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

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    fastapi_app = FastAPI(
        title="IRIS 공고 로컬 열람",
        description="로컬에 적재된 IRIS 사업공고를 조회한다.",
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

    # ──────────────────────────────────────────────────────────
    # HTML: 목록 페이지
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/", response_class=HTMLResponse)
    def index_page(
        request: Request,
        status_param: Optional[str] = Query(default=None, alias="status"),
        search: Optional[str] = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        """공고 목록 HTML 페이지.

        쿼리 파라미터:
            - status:    `접수중` / `접수예정` / `마감` 중 하나. 생략 시 전체.
            - search:    제목/기관명 부분일치 검색어.
            - page:      1-based 페이지 번호.
            - page_size: 페이지 크기(최대 MAX_PAGE_SIZE).
        """
        status_enum = _coerce_status_query(status_param)
        safe_offset = (page - 1) * page_size
        announcement_items = list_announcements(
            session,
            status=status_enum,
            limit=page_size,
            offset=safe_offset,
            search=search,
        )
        total_count = count_announcements(
            session,
            status=status_enum,
            search=search,
        )
        total_pages = ceil(total_count / page_size) if total_count > 0 else 1

        return templates.TemplateResponse(
            request,
            "list.html",
            {
                "announcements": announcement_items,
                "total": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "status": status_param or "",
                "search": search or "",
            },
        )

    # ──────────────────────────────────────────────────────────
    # JSON API: 목록
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/announcements", response_class=JSONResponse)
    def list_announcements_api(
        status_param: Optional[str] = Query(default=None, alias="status"),
        search: Optional[str] = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """공고 목록을 JSON 으로 반환한다.

        응답 스키마:
            {
              "items":       [ {announcement ...}, ... ],
              "total":       전체 건수,
              "page":        현재 페이지(1-based),
              "page_size":   페이지 크기,
              "total_pages": 전체 페이지 수
            }
        """
        status_enum = _coerce_status_query(status_param)
        safe_offset = (page - 1) * page_size
        announcement_items = list_announcements(
            session,
            status=status_enum,
            limit=page_size,
            offset=safe_offset,
            search=search,
        )
        total_count = count_announcements(
            session,
            status=status_enum,
            search=search,
        )
        total_pages = ceil(total_count / page_size) if total_count > 0 else 0

        return {
            "items": [
                _serialize_announcement(announcement)
                for announcement in announcement_items
            ],
            "total": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
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
]
