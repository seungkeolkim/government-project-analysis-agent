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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user_optional
from app.auth.routes import router as auth_router
from app.config import Settings, get_settings
from app.db.init_db import init_db
from app.db.models import Announcement, AnnouncementStatus, Organization, User
from app.db.repository import (
    RELEVANCE_SUMMARY_EMPTY,
    count_announcements,
    count_canonical_groups,
    get_announcement_by_id,
    get_attachment_by_id,
    get_attachments_by_announcement,
    get_available_source_ids,
    get_group_size_map,
    get_favorite_entry_map,
    get_folder_tree_for_user,
    list_favorites_with_announcements,
    get_read_announcement_id_set,
    get_relevance_history,
    get_relevance_summary_by_canonical_id_map,
    get_siblings_by_canonical_id_map,
    list_announcements,
    list_canonical_groups,
    mark_announcement_read,
)
from app.organizations.service import get_user_organization_ids
from app.progress.repository import (
    PROGRESS_SUMMARY_EMPTY,
    get_progress,
    get_progress_rows_by_canonical_id_map,
    get_progress_summary_by_canonical_id_map,
)
from app.db.session import SessionLocal
from app.logging_setup import configure_logging
from app.scheduler import ensure_backup_cron_registered, start_scheduler, stop_scheduler
from app.scrape_control import cleanup_stale_running_runs
from app.suggestions import (
    ensure_deleted_at_columns,
    ensure_suggestion_comment_updated_at_column,
    ensure_updated_at_initial_null_backfill,
    init_suggestions_db,
    migrate_suggestions_to_boards,
)
from app.web.access_log import install_access_history_middleware
from app.web.observability import (
    install_request_logging_middleware,
    install_unhandled_exception_handler,
)
from app.web.routes import (
    admin_router,
    bulk_router,
    dashboard_router,
    favorites_router,
    notices_router,
    progress_router,
    relevance_router,
    settings_router,
    suggestions_router,
)
from app.web.template_filters import register_kst_filters

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
    # 00030-3 — 요청 단위 DB 세션 lifecycle DEBUG 로그. 요청 미들웨어의
    # request_id 컨텍스트가 함께 찍혀 "한 요청에 세션이 몇 번 열렸나" 확인이
    # 쉽다. FastAPI 가 Depends 로 주입할 때마다 이 함수가 호출된다.
    session = SessionLocal()
    logger.debug("web get_session open")
    try:
        yield session
    finally:
        session.close()
        logger.debug("web get_session close")


# ──────────────────────────────────────────────────────────────
# 직렬화 헬퍼
# ──────────────────────────────────────────────────────────────


def _load_user_organization_options(
    session: Session, current_user: User | None
) -> list[dict[str, Any]]:
    """현재 로그인 사용자가 소속된 조직 목록을 모달 드롭다운용 dict 리스트로 반환한다.

    task 00085 — 관련성 판정 모달의 '조직' 드롭다운에 노출할 옵션을 미리 계산해
    템플릿 컨텍스트에 주입한다. 단일 조직 소속이면 라디오 라벨에 직접 표시하고,
    복수 조직 소속이면 라디오 선택 시 드롭다운으로 표시된다.

    비로그인 호출 (current_user is None) 또는 무소속 사용자는 빈 리스트를 반환한다 —
    템플릿이 옵션 길이로 무소속/단일/복수 케이스를 분기한다.

    Args:
        session: 호출자 세션.
        current_user: 현재 로그인 사용자 또는 None.

    Returns:
        ``[{\"id\": int, \"name\": str}, ...]`` 형태의 리스트. 비로그인/무소속이면 빈 리스트.
    """
    if current_user is None:
        return []
    organization_ids = get_user_organization_ids(session, current_user.id)
    if not organization_ids:
        return []
    organizations = session.execute(
        select(Organization)
        .where(Organization.id.in_(organization_ids))
        .order_by(Organization.name.asc())
    ).scalars().all()
    return [{"id": org.id, "name": org.name} for org in organizations]


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

    # 00030-1 — 웹 프로세스 부트스트랩 가장 먼저 로깅을 설정한다.
    # 이전에는 configure_logging() 이 CLI 진입점(app.cli, scripts/*) 에서만
    # 호출돼, uvicorn 이 기동한 FastAPI 프로세스에서는 loguru 가 기본 상태로
    # 남아 LOG_LEVEL=DEBUG 가 무시되고 stdlib logging(uvicorn/starlette/
    # fastapi/sqlalchemy/alembic) 이 loguru 로 라우팅되지 않았다. 결과적으로
    # 미처리 예외의 stack trace 가 docker logs 에 전혀 남지 않는 문제가 있어,
    # create_app() 최상단에서 명시적으로 호출하도록 고정한다.
    # init_db() 내부에서 다시 호출될 가능성에 대비해 configure_logging 은
    # idempotent 하게 구현돼 있다.
    configure_logging(effective_settings)

    effective_settings.ensure_runtime_paths()
    init_db()

    # task 00056 — suggestions.sqlite3 → boards.sqlite3 원자적 이름 변경.
    # 반드시 get_suggestions_engine() lru_cache 첫 호출(init_suggestions_db) 직전에
    # 실행되어야 엔진이 신규 경로(boards.sqlite3)로 처음 생성된다.
    migrate_suggestions_to_boards()

    # task 00068 — 기존 boards.sqlite3 에 suggestion_comments.updated_at 컬럼이
    # 없으면 멱등하게 ALTER + backfill 한다. 신규 환경(boards.sqlite3 없음)에서는
    # 아래 init_suggestions_db() 의 create_all 이 컬럼 포함 테이블을 한 번에 만든다.
    ensure_suggestion_comment_updated_at_column()

    # task 00069 — 기존 boards.sqlite3 의 세 테이블(suggestions, suggestion_comments,
    # notices)에 deleted_at 컬럼이 없으면 멱등하게 ALTER ADD COLUMN 한다. 신규 환경에서는
    # 아래 init_suggestions_db() 의 create_all 이 컬럼 포함 테이블을 한 번에 만든다.
    ensure_deleted_at_columns()

    # task 00072 — notices/suggestions/suggestion_comments 의 updated_at 을
    # INSERT 시 NULL 정책으로 정착시킨다. 기존 환경의 테이블 recreate + backfill.
    # 신규 환경(boards.sqlite3 없음)에서는 아래 init_suggestions_db() 의 create_all
    # 이 nullable updated_at 포함 테이블을 한 번에 만든다.
    ensure_updated_at_initial_null_backfill()

    # task 00051 — 게시판 별도 DB 의 테이블을 멱등하게 보장한다.
    # 메인 DB(init_db, Alembic) 와 별개의 SQLite 파일이며, 메인 DB reset 시에도
    # 영향을 받지 않는다. create_all 기반이라 반복 호출 안전(이미 존재하면 무시).
    init_suggestions_db()

    # Phase 2(00025) — 웹 startup 시 "pid 없는 running row" 및 프로세스 사라진
    # running row 를 failed 로 정리한다. 이전 인스턴스가 관리하던 subprocess
    # 는 재시작 후 제어 불가능한 고아 상태이므로 lock 을 해제해 다음 수집을
    # 받을 수 있도록 한다 (사용자 원문 '동시성 · Stale cleanup' 요구).
    #
    # 주의: create_app 은 테스트에서 여러 번 호출될 수 있다. cleanup_stale_running_runs
    # 은 idempotent (terminal 상태 row 는 건너뜀) — 반복 호출 안전하다.
    try:
        cleaned_count = cleanup_stale_running_runs()
        if cleaned_count:
            logger.warning(
                "startup stale cleanup: {}건의 running ScrapeRun 을 failed 로 정리",
                cleaned_count,
            )
    except Exception as exc:
        # stale cleanup 실패가 웹 기동 자체를 막지는 않도록 한다 — 최악의 경우
        # lock 이 해제되지 않아 관리자가 수동 정리가 필요할 수 있지만, 웹 UI
        # 는 뜨는 것이 우선.
        logger.exception(
            "startup stale cleanup 실패(스킵, 웹 기동 계속): ({}: {})",
            type(exc).__name__, exc,
        )

    # Phase 2(00025-6): APScheduler BackgroundScheduler 기동.
    # stale cleanup 이 끝난 **뒤** 에 start 한다 — 스케줄 잡이 곧바로
    # start_scrape_run 을 부르게 되면 방금 failed 로 정리한 lock 을 다시
    # running 으로 올려야 하는데, cleanup 과 scheduler.start() 사이에 race
    # 가 있으면 간헐적으로 lock 경합이 생길 수 있다. 순서 고정으로 방지.
    #
    # 재시작 후 복원: SQLAlchemyJobStore 가 scheduler_jobs 테이블에서 잡을
    # 자동 로드. misfire_grace_time(기본 300초) 안에 들어오는 놓친 잡은
    # coalesce=True 로 한 번에 합쳐 실행된다.
    #
    # 실패 시 정책: 수동 수집(/admin/scrape) 은 스케줄러가 없어도 동작해야
    # 하므로 예외를 삼키고 웹 기동은 계속한다.
    try:
        start_scheduler()
    except Exception as exc:
        logger.exception(
            "APScheduler 기동 실패(스킵, 수동 수집은 계속 가능): ({}: {})",
            type(exc).__name__, exc,
        )

    # task 00094-2 — startup 시 백업 cron 잡이 없으면 자동 등록한다.
    # APScheduler 가 jobstore 에서 자동 복원한 경우 no-op. 첫 실행 시 DB 설정
    # (없으면 DEFAULT_BACKUP_CRON) 으로 등록한다. 스케줄러 기동 실패 시에도
    # 이 호출은 내부에서 조용히 skip 하므로 웹 기동을 막지 않는다.
    try:
        ensure_backup_cron_registered()
    except Exception as exc:
        logger.warning(
            "백업 cron startup 등록 실패(스킵, 웹 기동 계속): ({}: {})",
            type(exc).__name__, exc,
        )

    # sources.yaml 에서 소스 목록을 한 번만 읽어 라우트 클로저에 공유한다.
    available_source_ids: list[str] = get_available_source_ids()

    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # task 00040-3 — 모든 timestamp 표시는 KST 기준 필터(kst_format/kst_date)
    # 경유로 통일한다. create_app 직후 한 번만 등록하면 충분하지만, 같은 키에
    # 다시 대입해도 안전하므로 테스트에서 create_app 을 여러 번 호출해도
    # idempotent 하다 (template_filters.register_kst_filters docstring 참조).
    register_kst_filters(templates)

    fastapi_app = FastAPI(
        title="정부과제 공고 수집/분석 시스템",
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

    # 00030-2 — HTTP access log 미들웨어 + 전역 예외 핸들러.
    # 미들웨어는 라우터 mount 보다 먼저 등록해야 모든 라우트를 덮고, 예외
    # 핸들러도 함께 설치해 라우트 안에서 터진 미처리 예외를 stack trace 와
    # 함께 loguru 로 기록한다 (docker logs 가 비어 있던 문제 대응).
    # FastAPI 기본 HTTPException/RequestValidationError 핸들러가 더 구체적이라
    # 먼저 매칭되므로 4xx 경로는 본 핸들러를 거치지 않는다 (주석:
    # app/web/observability.py 참조).
    install_request_logging_middleware(fastapi_app)
    install_unhandled_exception_handler(fastapi_app)

    # task 00073-1 — IP 접근 이력 로그 미들웨어.
    # install_request_logging_middleware 보다 나중에 등록해 Starlette 미들웨어 스택에서
    # 바깥쪽(먼저 실행)으로 배치된다. call_next 반환 후 status_code 를 읽어 기록.
    install_access_history_middleware(fastapi_app)

    # Phase 1b: 인증 라우터(register/login/logout/me) 를 mount 한다.
    # 기존 라우트와 충돌하지 않으며, /auth/* 와 /login, /register 를 노출한다.
    fastapi_app.include_router(auth_router)

    # Phase 2(00025-4): 관리자 라우터(/admin/*) mount.
    # 라우터 자체에 admin_user_required dependency 가 걸려 있어 비로그인 401,
    # 비관리자 403. 본 subtask 범위는 [수집 제어] 탭 + startup stale cleanup.
    fastapi_app.include_router(admin_router)

    # Phase 3a(00035-2): 관련성 판정 라우터(/canonical/{id}/relevance*) mount.
    fastapi_app.include_router(relevance_router)

    # Phase C(00097-4): 공고 진행 상태 / 선점 라우터
    # (/canonical/{id}/progress, /canonical/{id}/progress/{progress_id},
    #  /canonical/{id}/progress/history) mount.
    fastapi_app.include_router(progress_router)

    # Phase 3a(00035-4): 읽음 bulk 라우터(/announcements/bulk-mark-*) mount.
    fastapi_app.include_router(bulk_router)

    # Phase 3b(00036-4): 즐겨찾기 라우터(/favorites/*) mount.
    fastapi_app.include_router(favorites_router)

    # Phase 5b(00042-2): 대시보드 라우터(/dashboard, /dashboard/api/*) mount.
    # 비로그인도 접근 가능 — 라우터 내부에서 current_user_optional 로 분기한다.
    fastapi_app.include_router(dashboard_router)

    # task 00049-2: 개인 설정 라우터(/settings, /settings/*) mount.
    # 비로그인 GET → /login?next=/settings 리다이렉트. POST 는 401 반환.
    fastapi_app.include_router(settings_router)

    # task 00056-3: 공지사항 게시판 라우터(/notices/*) mount.
    # 목록·상세는 비로그인도 열람 가능. 작성/수정/삭제는 관리자 전용.
    fastapi_app.include_router(notices_router)

    # task 00051-2: 건의사항 게시판 라우터(/suggestions, /suggestions/new) mount.
    # 비로그인도 목록·폼 진입 가능(폼은 안내만 표시), POST /suggestions 는 로그인 필수.
    fastapi_app.include_router(suggestions_router)

    # Phase 2(00025-6): shutdown 시 APScheduler 정지.
    # docker-compose 의 `restart: unless-stopped` 와 결합해, 웹 프로세스가 정상
    # 종료되면 잡도 깨끗이 멈췄다가 재기동 시 scheduler_jobs 테이블에서 자동
    # 복원된다 (progress 손실 없음). wait=False 는 진행 중 잡(수집 subprocess)
    # 을 기다리지 않고 즉시 shutdown — subprocess 자체는 별도 watcher 스레드가
    # 관리하므로 웹이 죽어도 수집은 계속 돌다가 자기 ScrapeRun 을 마감한다.
    @fastapi_app.on_event("shutdown")
    def _shutdown_scheduler() -> None:  # noqa: D401 - FastAPI 훅
        """FastAPI shutdown 훅 — scheduler 정지."""
        try:
            stop_scheduler(wait=False)
        except Exception as exc:
            logger.warning(
                "APScheduler shutdown 중 예외(무시): {}: {}",
                type(exc).__name__, exc,
            )

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

            # Phase 1b — 로그인 사용자에 한해 대표 공고들의 read/relevance 를
            # 한 번에 조회한다 (N+1 방지). 비로그인은 빈 값.
            if current_user is not None:
                representative_ids = [gr.representative.id for gr in groups]
                read_id_set = get_read_announcement_id_set(
                    session,
                    user_id=current_user.id,
                    announcement_ids=representative_ids,
                )
            else:
                read_id_set = set()

            # task 00085 — 관련성 summary batch 조회 (페이지당 추가 쿼리 1 개로 고정).
            # current_user is None 인 비로그인 호출도 같은 헬퍼로 처리되며 mine 영역만
            # 비고 OTHERS 카운터가 채워진다 (사용자 modify 턴 결정 3 — 비로그인 = 동일 노출).
            canonical_ids = list({
                gr.representative.canonical_group_id
                for gr in groups
                if gr.representative.canonical_group_id is not None
            })
            relevance_summary_map = get_relevance_summary_by_canonical_id_map(
                session,
                user_id=current_user.id if current_user is not None else None,
                canonical_project_ids=canonical_ids,
            )
            # task 00097 — 진행 상태 summary batch 조회. 페이지당 추가 쿼리 1~2 개로
            # 선점 조직 / 검토·관심 카운터 / 본인 활동 단계를 모두 받는다.
            progress_summary_map = get_progress_summary_by_canonical_id_map(
                session,
                user_id=current_user.id if current_user is not None else None,
                canonical_project_ids=canonical_ids,
            )
            # task 00097-5 — 셀 expand 본문용 조직 detail rows. 단일 SELECT + JOIN
            # 으로 페이지당 추가 쿼리 1 회 (N+1 회피 — 시나리오 17 가드 유지).
            progress_rows_map = get_progress_rows_by_canonical_id_map(
                session, canonical_project_ids=canonical_ids
            )
            user_organization_options = _load_user_organization_options(
                session, current_user
            )

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
                    # task 00085 — 관련성 요약 batch (mine_organization / others / 카운터).
                    # 비로그인 / 로그인 모두 동일 키로 주입.
                    "relevance_summary_map": relevance_summary_map,
                    "relevance_summary_empty": RELEVANCE_SUMMARY_EMPTY,
                    "user_organization_options": user_organization_options,
                    # task 00097 — 진행 상태 요약 batch (group_mode).
                    "progress_summary_map": progress_summary_map,
                    "progress_summary_empty": PROGRESS_SUMMARY_EMPTY,
                    # task 00097-5 — 셀 expand 본문 (canonical_id → ProgressRowDetail 리스트).
                    "progress_rows_map": progress_rows_map,
                    # Phase 3b — group_mode 에서는 expand/별 없음; undefined 방지.
                    "siblings_map": {},
                    "favorite_entry_map": {},
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
            # 동일과제 expand — 비로그인도 표시하므로 user 분기 밖에서 batch 조회.
            siblings_map = get_siblings_by_canonical_id_map(session, list(cgids))
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

            # Phase 1b — 로그인 사용자의 페이지 공고별 read/relevance 여부를
            # 한 쿼리로 조회. 비로그인은 빈 값으로 두어 시각 차이를 상쇄한다.
            if current_user is not None:
                announcement_ids = [ann.id for ann, _size in ann_with_sizes]
                read_id_set = get_read_announcement_id_set(
                    session,
                    user_id=current_user.id,
                    announcement_ids=announcement_ids,
                )
                # Phase 3b (task 00037 갱신) — 별 아이콘 초기 상태 (announcement → entry_id).
                favorite_entry_map = get_favorite_entry_map(
                    session,
                    user_id=current_user.id,
                    announcement_ids=announcement_ids,
                )
            else:
                read_id_set = set()
                favorite_entry_map = {}

            # task 00085 — 관련성 summary batch 조회 (비로그인도 동일 노출).
            # 페이지당 추가 쿼리 1 개로 mine_organization / others / 카운터를 모두 받는다.
            canonical_ids = list({
                ann.canonical_group_id
                for ann, _ in ann_with_sizes
                if ann.canonical_group_id is not None
            })
            relevance_summary_map = get_relevance_summary_by_canonical_id_map(
                session,
                user_id=current_user.id if current_user is not None else None,
                canonical_project_ids=canonical_ids,
            )
            # task 00097 — 진행 상태 summary batch 조회 (분리 모드).
            progress_summary_map = get_progress_summary_by_canonical_id_map(
                session,
                user_id=current_user.id if current_user is not None else None,
                canonical_project_ids=canonical_ids,
            )
            # task 00097-5 — 셀 expand 본문용 조직 detail rows (분리 모드도 동일 패턴).
            progress_rows_map = get_progress_rows_by_canonical_id_map(
                session, canonical_project_ids=canonical_ids
            )
            user_organization_options = _load_user_organization_options(
                session, current_user
            )

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
                    # task 00085 — 관련성 요약 batch.
                    "relevance_summary_map": relevance_summary_map,
                    "relevance_summary_empty": RELEVANCE_SUMMARY_EMPTY,
                    "user_organization_options": user_organization_options,
                    # task 00097 — 진행 상태 요약 batch (분리 모드).
                    "progress_summary_map": progress_summary_map,
                    "progress_summary_empty": PROGRESS_SUMMARY_EMPTY,
                    # task 00097-5 — 셀 expand 본문 (분리 모드).
                    "progress_rows_map": progress_rows_map,
                    # Phase 3b — 동일과제 expand 데이터 (비로그인 포함).
                    "siblings_map": siblings_map,
                    # Phase 3b — 별 아이콘 초기 상태 (로그인 시만).
                    "favorite_entry_map": favorite_entry_map,
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

        # task 00085 — 관련성 summary 조회 (비로그인 = 로그인 동일 노출).
        # canonical_group_id 가 있을 때만 의미가 있다. _cid 가 None 이면 빈 summary 로 대체.
        canonical_group_id = announcement.canonical_group_id
        if canonical_group_id is not None:
            relevance_summary_map = get_relevance_summary_by_canonical_id_map(
                session,
                user_id=current_user.id if current_user is not None else None,
                canonical_project_ids=[canonical_group_id],
            )
            relevance_summary = relevance_summary_map.get(
                canonical_group_id, RELEVANCE_SUMMARY_EMPTY
            )
            # 상세 페이지 하단의 판정 이력 섹션은 history 헬퍼로 별도 조회 (canonical 1 개).
            history_items = get_relevance_history(
                session, canonical_project_id=canonical_group_id
            )
        else:
            relevance_summary = RELEVANCE_SUMMARY_EMPTY
            history_items = []

        # 즐겨찾기 별 아이콘 초기 상태 — 로그인 사용자에게만 의미가 있다.
        if current_user is not None:
            _fav_map = get_favorite_entry_map(
                session,
                user_id=current_user.id,
                announcement_ids=[announcement.id],
            )
            _fav_entry_id: int | None = _fav_map.get(announcement.id)
        else:
            _fav_entry_id = None

        # 모달 드롭다운에 채울 본인 소속 조직 목록 (비로그인이면 빈 리스트).
        user_organization_options = _load_user_organization_options(
            session, current_user
        )

        # task 00097 — Phase C 진행 상태 인라인 섹션 데이터.
        # canonical 매칭이 없으면 비활성 (None 으로 두고 템플릿이 분기).
        if canonical_group_id is not None:
            progress_rows = get_progress(session, canonical_group_id)
        else:
            progress_rows = []
        # 본인 소속 조직 옵션 — 본인 소속 조직별로 row 슬롯을 풀어 표시할 때 사용.
        # 비로그인 / 무소속이면 빈 리스트 — 템플릿이 분기.
        progress_my_organizations = _load_user_organization_options(
            session, current_user
        )
        progress_my_organization_ids = {
            opt["id"] for opt in progress_my_organizations
        }

        # 동일과제 섹션 — 비로그인도 표시. 현재 공고는 목록에서 제외.
        if canonical_group_id is not None:
            _sib_map = get_siblings_by_canonical_id_map(
                session, [canonical_group_id]
            )
            _siblings = [
                s
                for s in _sib_map.get(canonical_group_id, [])
                if s["id"] != announcement.id
            ]
        else:
            _siblings = []

        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "announcement": announcement,
                "attachments": attachments,
                # Phase 1b — base.html 상단 네비 분기에 필요.
                "current_user": current_user,
                # task 00085 — 관련성 요약 + 이력 + 모달 드롭다운 옵션.
                "canonical_id": canonical_group_id,
                "relevance_summary": relevance_summary,
                "relevance_summary_empty": RELEVANCE_SUMMARY_EMPTY,
                "history_items": history_items,
                "user_organization_options": user_organization_options,
                # task 00097 — 진행 상태 인라인 섹션 데이터.
                "progress_rows": progress_rows,
                "progress_my_organizations": progress_my_organizations,
                "progress_my_organization_ids": progress_my_organization_ids,
                # Phase 3b — 동일과제 섹션 (비로그인 포함).
                "siblings": _siblings,
                # Phase 3b — 별 아이콘 초기 상태 (로그인 시만).
                "fav_entry_id": _fav_entry_id,
            },
        )

    # ──────────────────────────────────────────────────────────
    # HTML: 즐겨찾기 전용 탭 페이지 (Phase 3b / 00036-7)
    # ──────────────────────────────────────────────────────────

    @fastapi_app.get("/favorites", response_class=HTMLResponse, response_model=None)
    def favorites_page(
        request: Request,
        folder_id: Optional[int] = Query(None),
        page: int = Query(1, ge=1),
        session: Session = Depends(get_session),
        current_user: User | None = Depends(current_user_optional),
    ) -> HTMLResponse | RedirectResponse:
        """즐겨찾기 전용 탭 페이지.

        비로그인 시 /login?next=/favorites 로 리다이렉트한다.
        좌 폴더 트리(SSR) + 우 폴더 내 공고 목록(SSR) 2-panel 레이아웃을 반환한다.
        """
        if current_user is None:
            return RedirectResponse(
                url="/login?next=/favorites",
                status_code=status.HTTP_302_FOUND,
            )

        page_size = 20
        folder_tree = get_folder_tree_for_user(session, user_id=current_user.id)

        items: list[dict] = []
        total = 0
        total_pages = 1
        selected_folder_id: int | None = folder_id

        if selected_folder_id is not None:
            items, total = list_favorites_with_announcements(
                session,
                folder_id=selected_folder_id,
                page=page,
                page_size=page_size,
            )
            total_pages = max(1, ceil(total / page_size))

        # task 00085 — 즐겨찾기 페이지의 관련성 summary batch 조회.
        # favorites 페이지는 비로그인이 위 분기에서 redirect 되므로 항상 current_user 가 있다.
        # canonical_project_id 가 None 인 항목은 summary 조회 대상에서 제외된다.
        canonical_ids = [
            it["canonical_project_id"]
            for it in items
            if it.get("canonical_project_id") is not None
        ]
        relevance_summary_map = get_relevance_summary_by_canonical_id_map(
            session,
            user_id=current_user.id,
            canonical_project_ids=canonical_ids,
        )
        user_organization_options = _load_user_organization_options(
            session, current_user
        )

        # task 00037 plan_review 추가요청 — 즐겨찾기 테이블도 목록 페이지와 동일하게
        # 읽음/안읽음에 따라 제목 일반/bold 로 분기하기 위해 read_id_set 을 주입한다.
        # items 의 announcement_id(=ann_id) 목록을 IN 절 단일 쿼리로 조회.
        announcement_ids = [
            it["announcement_id"]
            for it in items
            if it.get("announcement_id") is not None
        ]
        if announcement_ids:
            read_id_set = get_read_announcement_id_set(
                session,
                user_id=current_user.id,
                announcement_ids=announcement_ids,
            )
        else:
            read_id_set = set()

        return templates.TemplateResponse(
            request,
            "favorites.html",
            {
                "current_user": current_user,
                "folder_tree": folder_tree,
                "selected_folder_id": selected_folder_id,
                "entries": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                # task 00085 — 관련성 요약 batch + 모달 드롭다운 옵션.
                "relevance_summary_map": relevance_summary_map,
                "relevance_summary_empty": RELEVANCE_SUMMARY_EMPTY,
                "user_organization_options": user_organization_options,
                # task 00037 plan_review 추가요청 — read/unread bold 분기용.
                "read_id_set": read_id_set,
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
