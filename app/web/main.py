"""FastAPI 로컬 열람 웹 백엔드.

로컬 전용 조회·다운로드 API. 인증이 없으므로 외부 노출 금지.
첨부파일 다운로드는 `settings.download_dir` 하위 경로만 서빙하도록
경로 트래버설 방지 검사를 거친다.

라우트:
    - GET /                              공고 목록 HTML 페이지
    - GET /announcements                 공고 목록 JSON API (status/search/페이징)
    - GET /announcements/{id}            공고 상세 HTML 페이지
    - GET /announcements/{id}.json       공고 상세 JSON
    - GET /attachments/{id}/download     첨부파일 FileResponse

템플릿:
    `app/web/templates/` 하위의 Jinja2 템플릿(`base.html`/`list.html`/`detail.html`).
    정적 리소스(CSS)는 `app/web/static/` 을 `/static` 경로로 마운트해 서빙한다.
    외부 CDN 의존 없이 로컬/격리 환경에서도 그대로 동작하는 것을 목표로 한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from math import ceil
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.init_db import init_db
from app.db.models import Announcement, AnnouncementStatus, Attachment
from app.db.repository import (
    count_announcements,
    get_announcement,
    list_announcements,
)
from app.db.session import SessionLocal

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# Jinja2 템플릿 루트. 패키지와 함께 배포되도록 `app/web/templates/` 에 둔다.
TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "templates"

# 정적 리소스(CSS 등) 루트. 외부 CDN 없이 오프라인 환경에서도 서빙 가능하도록
# 패키지 내부(`app/web/static/`)에 둔다. `/static` URL prefix 로 마운트된다.
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

# 페이지네이션 기본/상한값. 상한을 명시해 악의적 큰 page_size 로 DB 를 터뜨리지 못하게 한다.
DEFAULT_PAGE_SIZE: int = 20
MAX_PAGE_SIZE: int = 100


# ──────────────────────────────────────────────────────────────
# 의존성
# ──────────────────────────────────────────────────────────────


def get_session() -> Iterator[Session]:
    """요청 단위 DB 세션 의존성.

    FastAPI `Depends(get_session)` 를 통해 각 엔드포인트에 주입된다.
    블록이 끝나면 정상/예외 모두 세션을 close() 로 반환한다.
    읽기 전용 엔드포인트를 가정하므로 commit 은 수행하지 않는다.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────
# 직렬화 헬퍼
# ──────────────────────────────────────────────────────────────


def _serialize_announcement(
    announcement: Announcement,
    *,
    include_attachments: bool = False,
) -> dict[str, Any]:
    """Announcement ORM 인스턴스를 JSON 직렬화 가능한 dict 로 변환한다.

    datetime 은 ISO-8601 문자열로 고정하고, Enum 은 한글 value 로 보존한다.
    첨부파일 상세까지 포함하려면 `include_attachments=True` 를 쓴다(목록 응답에는 기본 미포함).
    """
    payload: dict[str, Any] = {
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
        "attachment_count": len(announcement.attachments),
    }
    if include_attachments:
        payload["attachments"] = [
            _serialize_attachment(attachment) for attachment in announcement.attachments
        ]
    return payload


def _serialize_attachment(attachment: Attachment) -> dict[str, Any]:
    """Attachment ORM 인스턴스를 JSON 직렬화 가능한 dict 로 변환한다.

    `stored_path` 는 서버 로컬 절대경로이므로 API 응답에서는 제외한다
    (정보 노출 최소화). 다운로드는 `/attachments/{id}/download` 를 통해서만 이뤄진다.
    """
    return {
        "id": attachment.id,
        "announcement_id": attachment.announcement_id,
        "original_filename": attachment.original_filename,
        "file_ext": attachment.file_ext,
        "file_size": attachment.file_size,
        "sha256": attachment.sha256,
        "download_url": attachment.download_url,
        "downloaded_at": attachment.downloaded_at.isoformat(),
    }


def _coerce_status_query(raw_status: Optional[str]) -> Optional[AnnouncementStatus]:
    """쿼리스트링 status 값을 `AnnouncementStatus` 로 변환한다.

    허용 입력:
        - None / 빈 문자열 → None (전체 조회).
        - 한글 value("접수중"/"접수예정"/"마감").
        - name("RECEIVING"/"SCHEDULED"/"CLOSED").

    그 외 값은 400 으로 명시 거절해 오타를 조용히 무시하지 않는다.
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


def _resolve_attachment_path(
    attachment: Attachment,
    download_root: Path,
) -> Path:
    """첨부파일의 실제 디스크 경로를 안전하게 해석한다.

    보안 정책:
        - `stored_path` 와 `download_root` 를 각각 `resolve()` 로 정규화한다(심볼릭
          링크/상대경로 중화).
        - 정규화된 `stored_path` 가 `download_root` 의 하위가 아닌 경우 403 을 낸다.
          이 검사로 `../../etc/passwd` 같은 경로 트래버설을 차단한다.
        - 파일이 실제로 존재하지 않으면 404 를 낸다.

    Args:
        attachment: DB 에서 읽어온 Attachment 레코드.
        download_root: 서빙이 허용되는 루트 디렉터리(절대경로).

    Returns:
        서빙해도 안전하다고 판정된 절대경로.

    Raises:
        HTTPException(403): 허용 범위 밖 경로.
        HTTPException(404): 경로는 적법하지만 디스크에 파일이 없는 경우.
    """
    raw_stored_path = Path(attachment.stored_path)
    # 절대경로가 아니면 안전한 기준점을 잡을 수 없으므로 즉시 거절한다.
    # (다운로더는 항상 절대경로로 기록하도록 구현되어 있다.)
    if not raw_stored_path.is_absolute():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="허용되지 않은 첨부파일 경로입니다(상대경로).",
        )

    normalized_stored_path = raw_stored_path.resolve()
    normalized_download_root = download_root.resolve()

    # Python 3.9 의 `is_relative_to` 는 일부 환경에서 False 를 오판할 수 있어
    # `relative_to` 의 ValueError 여부로 판정한다(표준 관용구).
    try:
        normalized_stored_path.relative_to(normalized_download_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="허용되지 않은 첨부파일 경로입니다.",
        ) from exc

    if not normalized_stored_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="첨부파일이 디스크에 존재하지 않습니다.",
        )
    return normalized_stored_path


# ──────────────────────────────────────────────────────────────
# 앱 팩토리
# ──────────────────────────────────────────────────────────────


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """FastAPI 앱을 생성한다.

    팩토리 형태로 제공해 테스트에서 별도 settings(격리된 download_dir/DB) 를
    주입할 수 있도록 한다. 모듈 최하단의 `app` 전역은 기본 settings 로 한 번
    초기화된 싱글턴이다.

    Args:
        settings: 이 앱 인스턴스에 적용할 Settings. 생략 시 `get_settings()` 사용.

    Returns:
        라우트가 등록된 FastAPI 인스턴스.
    """
    effective_settings = settings or get_settings()
    # SQLite 파일/다운로드 디렉터리 등 런타임 경로 보장.
    effective_settings.ensure_runtime_paths()
    # 스크래퍼를 먼저 돌리지 않은 상태로 웹 UI 만 기동해도 조회가 가능하도록
    # 스키마를 보장한다. create_all 은 멱등이므로 기존 테이블은 건드리지 않는다.
    init_db()

    # 템플릿 디렉터리 보장. 패키지 내부에 함께 배포되는 것이 원칙이므로
    # 일반적으로 이미 존재하지만, 예기치 못한 삭제에도 부팅이 가능하도록 방어적으로 생성한다.
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    fastapi_app = FastAPI(
        title="IRIS 공고 로컬 열람",
        description="로컬에 적재된 IRIS 사업공고와 첨부파일을 조회/다운로드한다.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    # 정적 리소스(CSS) 마운트. 템플릿에서 `/static/css/style.css` 로 참조한다.
    # 디렉터리가 없어도 부팅에 실패하지 않도록 마운트 직전에 생성만 보장한다.
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
            - page_size: 페이지 크기(최대 `MAX_PAGE_SIZE`).
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
              "total_pages": 전체 페이지 수 (0 건이면 0)
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

    # ──────────────────────────────────────────────────────────
    # JSON API: 상세
    # ──────────────────────────────────────────────────────────
    # 주의: `.json` suffix 라우트를 HTML 라우트보다 먼저 등록해야
    # `/announcements/{id}` 라우트가 `.json` 을 가로채지 않는다.

    @fastapi_app.get("/announcements/{announcement_id}.json", response_class=JSONResponse)
    def announcement_detail_api(
        announcement_id: int,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """공고 상세 + 첨부파일 리스트를 JSON 으로 반환한다."""
        fetched = get_announcement(session, announcement_id)
        if fetched is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"공고를 찾을 수 없습니다: id={announcement_id}",
            )
        announcement, attachments = fetched
        return {
            "announcement": _serialize_announcement(announcement),
            "attachments": [_serialize_attachment(a) for a in attachments],
            "raw_metadata": announcement.raw_metadata,
        }

    # ──────────────────────────────────────────────────────────
    # HTML: 상세 페이지
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/announcements/{announcement_id}", response_class=HTMLResponse)
    def announcement_detail_page(
        announcement_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        """공고 상세 HTML 페이지."""
        fetched = get_announcement(session, announcement_id)
        if fetched is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"공고를 찾을 수 없습니다: id={announcement_id}",
            )
        announcement, attachments = fetched
        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "announcement": announcement,
                "attachments": attachments,
            },
        )

    # ──────────────────────────────────────────────────────────
    # 첨부파일 다운로드
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/attachments/{attachment_id}/download")
    def download_attachment(
        attachment_id: int,
        session: Session = Depends(get_session),
    ) -> FileResponse:
        """첨부파일을 `FileResponse` 로 서빙한다.

        - `stored_path` 가 반드시 `settings.download_dir` 하위에 있어야 한다.
          그렇지 않으면 403 을 낸다(경로 트래버설 차단).
        - 디스크에 실제 파일이 없으면 404.
        - 원본 파일명은 `Content-Disposition` 에 RFC 5987 로 인코딩되어 나간다
          (FastAPI/Starlette 이 한글 파일명을 알아서 처리).
        """
        attachment = session.get(Attachment, attachment_id)
        if attachment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"첨부파일을 찾을 수 없습니다: id={attachment_id}",
            )

        safe_disk_path = _resolve_attachment_path(
            attachment,
            download_root=effective_settings.download_dir,
        )

        return FileResponse(
            path=safe_disk_path,
            filename=attachment.original_filename or safe_disk_path.name,
            media_type="application/octet-stream",
        )

    return fastapi_app


# 모듈 임포트 시점에 기본 settings 로 한 번 생성한다.
# `uvicorn app.web.main:app` 으로 바로 실행하기 위함.
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
