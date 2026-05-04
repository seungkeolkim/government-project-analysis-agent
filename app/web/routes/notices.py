"""공지사항 게시판 라우터 (task 00056-3).

엔드포인트:
    GET    /notices              공지사항 목록 HTML (비로그인도 열람 가능)
    GET    /notices/new          작성 폼 HTML (관리자만 폼 노출, 비관리자는 안내)
    POST   /notices              작성 처리 (관리자 전용 + Origin 체크)
    GET    /notices/{id}         공지사항 상세 HTML
    GET    /notices/{id}/edit    수정 폼 HTML (관리자 전용)
    POST   /notices/{id}/edit    수정 처리 (관리자 전용 + Origin 체크)
    POST   /notices/{id}/delete  삭제 처리 (관리자 전용 + Origin 체크)

설계 메모:
    - 공지사항은 관리자 전용 작성이므로 건의사항과 달리 메인 DB 세션이 불필요하다
      (고아 정책 없음 — 관리자가 작성자이므로 사라질 가능성이 극히 낮고, 안내에서도
      author_name 을 작성 시점 저장값으로 그대로 표시한다).
    - 세션은 SuggestionsSessionLocal 그대로 재사용(boards.sqlite3 공유).
    - 비밀글·수용여부·댓글 기능 없음 (사용자 원문 명시).
"""

from __future__ import annotations

from collections.abc import Iterator
from math import ceil
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    admin_user_required,
    current_user_optional,
    ensure_same_origin,
)
from app.db.models import User
from app.notices import (
    count_notices,
    create_notice,
    delete_notice,
    get_notice_by_id,
    list_notices,
    update_notice,
)
from app.suggestions import SuggestionsSessionLocal
from app.web.template_filters import register_kst_filters

router = APIRouter(tags=["notices"])

_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"
_templates: Jinja2Templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_kst_filters(_templates)

# 페이지네이션 정책
_DEFAULT_PAGE_SIZE: int = 20
_MAX_PAGE_SIZE: int = 100

# 입력 길이 정책
_TITLE_MAX_LENGTH: int = 255
_BODY_MAX_LENGTH: int = 20000


# ──────────────────────────────────────────────────────────────
# 세션 의존성 (boards.sqlite3 단일 세션)
# ──────────────────────────────────────────────────────────────


def _boards_db_session() -> Iterator[Session]:
    """boards DB(boards.sqlite3) 의 요청 단위 세션 의존성.

    건의사항과 동일한 SuggestionsSessionLocal 을 사용한다. 00056-1 에서
    Settings.suggestions_db_url 이 boards.sqlite3 를 가리키도록 변경되었으므로
    함수 이름을 바꾸지 않아도 정상 동작한다.
    """
    session = SuggestionsSessionLocal()
    logger.debug("notices boards DB 세션 open")
    try:
        yield session
    finally:
        session.close()
        logger.debug("notices boards DB 세션 close")


# ──────────────────────────────────────────────────────────────
# 입력 검증 헬퍼
# ──────────────────────────────────────────────────────────────


def _validate_required_text(value: str, *, field_label: str, max_length: int) -> str:
    """필수 텍스트 입력을 검증한다.

    빈 문자열·공백만 입력은 400, 길이 초과도 400.
    """
    stripped = (value or "").strip()
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_label} 은(는) 필수 입력 항목입니다.",
        )
    if len(stripped) > max_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_label} 의 길이가 너무 깁니다 (최대 {max_length}자).",
        )
    return stripped


# ──────────────────────────────────────────────────────────────
# 라우트
# ──────────────────────────────────────────────────────────────


@router.get(
    "/notices",
    response_class=HTMLResponse,
    response_model=None,
)
def list_notices_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    boards_session: Session = Depends(_boards_db_session),
    current_user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """공지사항 목록 HTML 페이지.

    누구나 열람 가능하다(비로그인 포함). 관리자에게는 글쓰기 버튼이 노출된다.

    Args:
        request: FastAPI Request (Jinja2 컨텍스트용).
        page: 1-based 페이지 번호.
        page_size: 페이지 크기 (최대 100).
        boards_session: boards DB 세션.
        current_user: 로그인 사용자 또는 None.

    Returns:
        ``notices/list.html`` 렌더 결과.
    """
    is_admin = bool(current_user is not None and current_user.is_admin)
    safe_offset = (page - 1) * page_size

    notices = list_notices(boards_session, limit=page_size, offset=safe_offset)
    total = count_notices(boards_session)
    total_pages = max(1, ceil(total / page_size))

    return _templates.TemplateResponse(
        request,
        "notices/list.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "notices": notices,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    )


@router.get(
    "/notices/new",
    response_class=HTMLResponse,
    response_model=None,
)
def new_notice_page(
    request: Request,
    current_user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """공지사항 작성 폼 페이지 (GET /notices/new).

    관리자에게만 폼을 노출한다. 비로그인 또는 비관리자에게는 안내 메시지만 표시한다.

    Args:
        request: FastAPI Request (Jinja2 컨텍스트용).
        current_user: 로그인 사용자 또는 None.

    Returns:
        ``notices/new.html`` 렌더 결과.
    """
    is_admin = bool(current_user is not None and current_user.is_admin)
    return _templates.TemplateResponse(
        request,
        "notices/new.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
        },
    )


@router.post(
    "/notices",
    dependencies=[Depends(ensure_same_origin), Depends(admin_user_required)],
    response_class=RedirectResponse,
    response_model=None,
)
def create_notice_route(
    title: str = Form(...),
    body: str = Form(...),
    current_user: User = Depends(admin_user_required),
    boards_session: Session = Depends(_boards_db_session),
) -> RedirectResponse:
    """공지사항 작성 처리 (POST /notices).

    관리자 전용. Origin 체크(ensure_same_origin) + 관리자 인증(admin_user_required).
    PRG 패턴: 저장 후 상세 페이지로 303 리다이렉트.

    Form 필드:
        - ``title``: 필수, ≤255자.
        - ``body``: 필수, ≤20000자.

    Returns:
        303 리다이렉트 (``Location: /notices/{id}``).

    Raises:
        HTTPException(401): 비로그인 (admin_user_required dependency).
        HTTPException(403): 비관리자 (admin_user_required dependency).
        HTTPException(400): 필수 필드 누락 또는 길이 초과.
    """
    title_normalized = _validate_required_text(
        title, field_label="제목", max_length=_TITLE_MAX_LENGTH
    )
    body_normalized = _validate_required_text(
        body, field_label="본문", max_length=_BODY_MAX_LENGTH
    )

    try:
        notice = create_notice(
            boards_session,
            author_user_id=current_user.id,
            author_name=current_user.username,
            title=title_normalized,
            body=body_normalized,
        )
        boards_session.commit()
    except Exception:
        boards_session.rollback()
        raise

    return RedirectResponse(
        url=f"/notices/{notice.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/notices/{notice_id}",
    response_class=HTMLResponse,
    response_model=None,
)
def view_notice_page(
    request: Request,
    notice_id: int,
    boards_session: Session = Depends(_boards_db_session),
    current_user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """공지사항 상세 페이지 (GET /notices/{id}).

    누구나 열람 가능하다. 관리자에게는 수정/삭제 버튼이 노출된다.

    Args:
        request: FastAPI Request (Jinja2 컨텍스트용).
        notice_id: ``notices.id`` 값.
        boards_session: boards DB 세션.
        current_user: 로그인 사용자 또는 None.

    Returns:
        ``notices/detail.html`` 렌더 결과.

    Raises:
        HTTPException(404): 공지사항이 존재하지 않음.
    """
    notice = get_notice_by_id(boards_session, notice_id)
    if notice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 공지사항을 찾을 수 없습니다.",
        )

    is_admin = bool(current_user is not None and current_user.is_admin)

    return _templates.TemplateResponse(
        request,
        "notices/detail.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "notice": notice,
        },
    )


@router.get(
    "/notices/{notice_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
def edit_notice_page(
    request: Request,
    notice_id: int,
    boards_session: Session = Depends(_boards_db_session),
    current_user: User = Depends(admin_user_required),
) -> HTMLResponse:
    """공지사항 수정 폼 페이지 (GET /notices/{id}/edit).

    관리자 전용. 비로그인은 401, 비관리자는 403 (admin_user_required dependency).

    Args:
        request: FastAPI Request (Jinja2 컨텍스트용).
        notice_id: 수정 대상 공지사항 PK.
        boards_session: boards DB 세션.
        current_user: 관리자 사용자 (dependency 보장).

    Returns:
        ``notices/edit.html`` 렌더 결과.

    Raises:
        HTTPException(401): 비로그인.
        HTTPException(403): 비관리자.
        HTTPException(404): 공지사항이 존재하지 않음.
    """
    notice = get_notice_by_id(boards_session, notice_id)
    if notice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 공지사항을 찾을 수 없습니다.",
        )

    return _templates.TemplateResponse(
        request,
        "notices/edit.html",
        {
            "current_user": current_user,
            "is_admin": True,
            "notice": notice,
        },
    )


@router.post(
    "/notices/{notice_id}/edit",
    dependencies=[Depends(ensure_same_origin), Depends(admin_user_required)],
    response_class=RedirectResponse,
    response_model=None,
)
def update_notice_route(
    notice_id: int,
    title: str = Form(...),
    body: str = Form(...),
    boards_session: Session = Depends(_boards_db_session),
) -> RedirectResponse:
    """공지사항 수정 처리 (POST /notices/{id}/edit).

    관리자 전용 + Origin 체크. PRG 패턴: 저장 후 상세 페이지로 303 리다이렉트.

    Form 필드:
        - ``title``: 필수, ≤255자.
        - ``body``: 필수, ≤20000자.

    Returns:
        303 리다이렉트 (``Location: /notices/{id}``).

    Raises:
        HTTPException(401): 비로그인.
        HTTPException(403): 비관리자.
        HTTPException(404): 공지사항이 존재하지 않음.
        HTTPException(400): 필수 필드 누락 또는 길이 초과.
    """
    title_normalized = _validate_required_text(
        title, field_label="제목", max_length=_TITLE_MAX_LENGTH
    )
    body_normalized = _validate_required_text(
        body, field_label="본문", max_length=_BODY_MAX_LENGTH
    )

    try:
        result = update_notice(
            boards_session,
            notice_id=notice_id,
            title=title_normalized,
            body=body_normalized,
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 공지사항을 찾을 수 없습니다.",
            )
        boards_session.commit()
    except HTTPException:
        boards_session.rollback()
        raise
    except Exception:
        boards_session.rollback()
        raise

    return RedirectResponse(
        url=f"/notices/{notice_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/notices/{notice_id}/delete",
    dependencies=[Depends(ensure_same_origin), Depends(admin_user_required)],
    response_class=RedirectResponse,
    response_model=None,
)
def delete_notice_route(
    notice_id: int,
    boards_session: Session = Depends(_boards_db_session),
) -> RedirectResponse:
    """공지사항 삭제 처리 (POST /notices/{id}/delete).

    관리자 전용 + Origin 체크. PRG 패턴: 삭제 후 목록 페이지로 303 리다이렉트.

    Returns:
        303 리다이렉트 (``Location: /notices``).

    Raises:
        HTTPException(401): 비로그인.
        HTTPException(403): 비관리자.
        HTTPException(404): 공지사항이 존재하지 않음.
    """
    try:
        deleted = delete_notice(boards_session, notice_id=notice_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 공지사항을 찾을 수 없습니다.",
            )
        boards_session.commit()
    except HTTPException:
        boards_session.rollback()
        raise
    except Exception:
        boards_session.rollback()
        raise

    return RedirectResponse(
        url="/notices",
        status_code=status.HTTP_303_SEE_OTHER,
    )
