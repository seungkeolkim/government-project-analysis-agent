"""건의사항 게시판 라우터 (task 00051-2 — 진입점·작성·목록).

엔드포인트:
    GET    /suggestions          건의사항 목록 HTML (비로그인도 열람 가능 — 단,
                                  modify 요구상 고아 글은 비관리자에게 비노출).
    GET    /suggestions/new      작성 폼 HTML. 비로그인 시 "글쓰기는 로그인이
                                  필요합니다" 안내만 표시.
    POST   /suggestions          작성 처리. 로그인 필수 + Origin 체크.

설계 메모:
    - 본 라우터는 두 개의 DB 세션을 동시에 들고 동작한다:
        * suggestions_session — 건의사항 별도 SQLite (``app.sqlite3`` 와 격리).
        * main_session       — 메인 DB(``app.sqlite3``). cross-DB author 유효성
                                판정 시 ``users`` 테이블 조회용.
      두 세션은 명시적으로 분리된 ``Depends`` 로 주입되며, 서로의 트랜잭션과
      독립적이다.
    - 고아 글 노출/마스킹 정책은 :mod:`app.suggestions.service` 한 곳에 모아두고
      뷰어/댓글 subtask 가 동일 헬퍼를 재사용한다 (사용자 modify 턴 분산 통합).
    - 작성 폼 CSRF 방어는 ``ensure_same_origin`` 으로 충분하다(프로젝트 컨벤션).
    - 비밀번호 해싱은 사용자 계정과 동일한 ``app.auth.service.hash_password``
      (bcrypt) 를 재사용한다 — 게시글별 비밀번호는 향후 수정/삭제 권한용으로
      저장만 한다(원문은 비밀글 열람 권한을 본인/관리자 세션 기반으로 명시).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
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
    current_user_required,
    ensure_same_origin,
)
from app.auth.service import hash_password
from app.db.models import User
from app.db.session import SessionLocal
from app.suggestions import (
    AcceptanceStatus,
    Suggestion,
    SuggestionsSessionLocal,
    apply_orphan_policy_to_comments,
    apply_orphan_policy_to_suggestions,
    count_comments_by_suggestion_ids,
    count_suggestions,
    create_suggestion,
    create_suggestion_comment,
    delete_suggestion,
    get_alive_user_ids,
    get_alive_user_username_map,
    get_suggestion_by_id,
    is_orphan_author,
    list_comments_by_suggestion_id,
    list_suggestions,
    update_suggestion,
    update_suggestion_acceptance,
)
from app.web.template_filters import register_kst_filters

router = APIRouter(tags=["suggestions"])

_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"
_templates: Jinja2Templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_kst_filters(_templates)


# ──────────────────────────────────────────────────────────────
# 페이지네이션 / 입력 길이 정책
# ──────────────────────────────────────────────────────────────

_DEFAULT_PAGE_SIZE: int = 20
_MAX_PAGE_SIZE: int = 100

# 게시글 제목/본문은 모델 컬럼 길이와 일치하는 상한선을 둔다 — 폼 단계에서
# DB 제약 위반 전에 일찍 사용자에게 검증 실패를 알리기 위함.
_TITLE_MAX_LENGTH: int = 255
_BODY_MAX_LENGTH: int = 20000
_AUTHOR_NAME_MAX_LENGTH: int = 64
_CONTACT_EMAIL_MAX_LENGTH: int = 255

# 게시글별 비밀번호는 향후 수정/삭제 권한용. 사용자 계정 비밀번호 정책과 별도로,
# 본 게시판은 짧은 PIN 도 허용한다(원문 "비밀번호 필수입력" 만 명시).
# 너무 짧으면 brute force 위험이 있어 4자 이상으로 보수적 하한을 둔다.
_POST_PASSWORD_MIN_LENGTH: int = 4
_POST_PASSWORD_MAX_LENGTH: int = 128

# 댓글 본문 길이 상한 — 본문보다는 짧게 두어 길어도 채팅 형태를 유지하게 한다.
_COMMENT_BODY_MAX_LENGTH: int = 2000


# ──────────────────────────────────────────────────────────────
# 세션 의존성 (메인 DB / 건의사항 DB 두 개를 명시 분리)
# ──────────────────────────────────────────────────────────────


def _main_db_session() -> Iterator[Session]:
    """메인 DB(``app.sqlite3``) 의 요청 단위 세션 의존성.

    ``app.web.main.get_session`` 과 동일 패턴이지만, 본 라우터가 ``main`` 의
    헬퍼에 의존하지 않도록 자체 정의한다. cross-DB author 유효성 헬퍼가 사용한다.
    """
    session = SessionLocal()
    logger.debug("suggestions main DB 세션 open")
    try:
        yield session
    finally:
        session.close()
        logger.debug("suggestions main DB 세션 close")


def _suggestions_db_session() -> Iterator[Session]:
    """건의사항 별도 DB(``suggestions.sqlite3``) 의 요청 단위 세션 의존성."""
    session = SuggestionsSessionLocal()
    logger.debug("suggestions DB 세션 open")
    try:
        yield session
    finally:
        session.close()
        logger.debug("suggestions DB 세션 close")


# ──────────────────────────────────────────────────────────────
# 폼 검증 헬퍼
# ──────────────────────────────────────────────────────────────


def _normalize_optional_text(value: str | None, *, max_length: int) -> str | None:
    """선택 입력 텍스트를 strip 한 뒤 빈 문자열은 ``None`` 으로 정규화한다.

    길이 초과 시 ``HTTPException(400)`` 으로 거절한다.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > max_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"입력값이 너무 깁니다 (최대 {max_length}자).",
        )
    return stripped


def _validate_required_text(value: str, *, field_label: str, max_length: int) -> str:
    """필수 입력 텍스트를 검증한다.

    빈 문자열·공백만 입력은 400 으로 거절한다. 길이 초과도 400.

    Args:
        value: 폼에서 받은 원문 문자열.
        field_label: 에러 메시지에 사용할 사용자 친화적 필드 라벨.
        max_length: 허용 최대 길이.

    Returns:
        strip 된 검증 통과 문자열.

    Raises:
        HTTPException(400): 빈 입력 또는 길이 초과.
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


def _validate_post_password(value: str) -> str:
    """게시글별 비밀번호 길이를 검증한다.

    사용자 계정 비밀번호와는 별도 정책(짧은 PIN 도 허용 — 본 게시판 한정).
    원문 "비밀번호 필수 입력" 외에 명시 정책이 없어, brute force 방어 하한선만 둔다.
    """
    if value is None or not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="비밀번호는 필수 입력 항목입니다.",
        )
    if len(value) < _POST_PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"비밀번호는 최소 {_POST_PASSWORD_MIN_LENGTH}자 이상이어야 합니다.",
        )
    if len(value) > _POST_PASSWORD_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"비밀번호는 최대 {_POST_PASSWORD_MAX_LENGTH}자까지 허용됩니다.",
        )
    return value


# ──────────────────────────────────────────────────────────────
# 라우트
# ──────────────────────────────────────────────────────────────


@router.get(
    "/suggestions",
    response_class=HTMLResponse,
    response_model=None,
)
def list_suggestions_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    main_session: Session = Depends(_main_db_session),
    suggestions_session: Session = Depends(_suggestions_db_session),
    current_user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """건의사항 목록 HTML 페이지.

    누구나 열람 가능하지만(비로그인 포함), 고아 글(작성자가 메인 DB users 에 없는
    글) 은 비관리자에게 비노출 / 관리자에게는 작성자 마스킹 표시된다.

    Args:
        request: FastAPI Request (Jinja2 컨텍스트용).
        page: 1-based 페이지 번호.
        page_size: 페이지 크기(최대 100).
        main_session: 메인 DB 세션 (cross-DB author 유효성 조회용).
        suggestions_session: 건의사항 DB 세션 (게시글 목록 조회용).
        current_user: 로그인 사용자 또는 None. base.html 상단 네비 분기 + 관리자
            여부 판정에 쓰인다.

    Returns:
        ``suggestions/list.html`` 렌더 결과.
    """
    is_admin = bool(current_user is not None and current_user.is_admin)

    safe_offset = (page - 1) * page_size
    suggestions = list_suggestions(
        suggestions_session,
        limit=page_size,
        offset=safe_offset,
    )

    # cross-DB 작성자 유효성 batch 판정 — IN 절 단일 쿼리. 빈/all-None 입력 시
    # 메인 DB 에 쿼리 자체가 가지 않는다(헬퍼 내부 short-circuit).
    author_user_ids = {s.author_user_id for s in suggestions}
    alive_user_ids = get_alive_user_ids(main_session, author_user_ids)

    # 현재 페이지 게시글들의 댓글 수 batch 조회 — GROUP BY 단일 쿼리(N+1 금지).
    suggestion_ids = [s.id for s in suggestions]
    comment_count_map = count_comments_by_suggestion_ids(suggestions_session, suggestion_ids)

    # 정책 적용: 비관리자 → 고아 글 제외, 관리자 → 고아 글 마스킹 포함.
    suggestion_views = apply_orphan_policy_to_suggestions(
        suggestions,
        alive_user_ids,
        is_admin=is_admin,
        comment_count_map=comment_count_map,
    )

    # 전체 건수는 정책 적용 전 raw count 를 그대로 노출한다 — 비관리자에게도
    # "원래 N건 있는데 일부 가려졌다" 는 사실은 굳이 드러내지 않는다.
    # 페이지 계산상 약간의 row 가 비관리자 페이지에서 누락될 수 있으나, 본
    # 게시판 규모상 사용자 체감으로는 무리 없다.
    total_count = count_suggestions(suggestions_session)
    total_pages = ceil(total_count / page_size) if total_count > 0 else 1

    return _templates.TemplateResponse(
        request,
        "suggestions/list.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "suggestion_views": suggestion_views,
            "page": page,
            "page_size": page_size,
            "total": total_count,
            "total_pages": total_pages,
        },
    )


@router.get(
    "/suggestions/new",
    response_class=HTMLResponse,
    response_model=None,
)
def new_suggestion_page(
    request: Request,
    current_user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """건의사항 작성 폼 HTML.

    로그인 사용자에게는 폼을 그대로 보여주고, 비로그인 사용자에게는 동일
    페이지에 "글쓰기는 로그인이 필요합니다" 안내만 표시한다 — 사용자 원문 그대로.

    Args:
        request: FastAPI Request.
        current_user: 로그인 사용자 또는 None.

    Returns:
        ``suggestions/new.html`` 렌더 결과.
    """
    return _templates.TemplateResponse(
        request,
        "suggestions/new.html",
        {
            "current_user": current_user,
            # 로그인 안내 분기는 템플릿이 current_user 로 직접 판단한다.
        },
    )


@router.post(
    "/suggestions",
    dependencies=[Depends(ensure_same_origin)],
    response_class=RedirectResponse,
    response_model=None,
)
def create_suggestion_route(
    title: str = Form(...),
    body: str = Form(...),
    password: str = Form(...),
    is_secret: str | None = Form(default=None),
    contact_email: str | None = Form(default=None),
    current_user: User = Depends(current_user_required),
    suggestions_session: Session = Depends(_suggestions_db_session),
) -> RedirectResponse:
    """건의사항 작성 처리 — application/x-www-form-urlencoded POST.

    로그인 필수(비로그인 시 dependency 단계에서 401). Origin/Referer 가 현재 host
    와 다르면 400 (CSRF 방어). 입력 검증 통과 시 ``Suggestion`` row 를 INSERT 하고
    ``/suggestions`` 목록 페이지로 303 리다이렉트(PRG 패턴).

    Form 필드:
        - ``title``: 필수, ≤255자.
        - ``body``: 필수, ≤20000자.
        - ``password``: 필수, 4~128자 — bcrypt 해시로 저장.
        - ``is_secret``: 체크박스. 체크되어 있으면 임의의 truthy 문자열이 넘어오고,
            체크 해제 시 키 자체가 누락되어 None 이 된다(브라우저 표준 동작).
        - ``contact_email``: 선택, ≤255자(엄격 RFC 검증 X — 로컬 전제).

    작성자명은 task 00052-2 부터 폼에서 받지 않고, 로그인 사용자의
    ``current_user.username`` 을 그대로 ``author_name`` 컬럼에 저장한다.
    ``User.username`` 은 NOT NULL + UNIQUE String(64) 라 None 이 될 수 없고
    길이도 64자를 넘을 수 없으므로, 별도 정규화 없이 그대로 전달한다.

    Returns:
        303 리다이렉트 (``Location: /suggestions``).
    """
    # ── 입력 정규화·검증 ──────────────────────────────────────────────────
    title_normalized = _validate_required_text(
        title, field_label="제목", max_length=_TITLE_MAX_LENGTH
    )
    body_normalized = _validate_required_text(
        body, field_label="본문", max_length=_BODY_MAX_LENGTH
    )
    password_normalized = _validate_post_password(password)
    is_secret_bool = is_secret is not None and is_secret.strip() != ""

    contact_email_normalized = _normalize_optional_text(
        contact_email, max_length=_CONTACT_EMAIL_MAX_LENGTH
    )

    # ── INSERT (트랜잭션은 라우트 끝에서 명시 commit) ──────────────────────
    try:
        create_suggestion(
            suggestions_session,
            author_user_id=current_user.id,
            title=title_normalized,
            body=body_normalized,
            password_hash=hash_password(password_normalized),
            is_secret=is_secret_bool,
            # 로그인 사용자명을 그대로 작성자명으로 저장. username 은 모델 단에서
            # NOT NULL + UNIQUE + String(64) 이라 None/길이초과 가능성이 없다.
            author_name=current_user.username,
            contact_email=contact_email_normalized,
        )
        suggestions_session.commit()
    except Exception:
        suggestions_session.rollback()
        raise

    # PRG 패턴: 새로고침 시 중복 INSERT 방지.
    return RedirectResponse(
        url="/suggestions",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/suggestions/{suggestion_id}",
    response_class=HTMLResponse,
    response_model=None,
)
def view_suggestion_page(
    request: Request,
    suggestion_id: int,
    main_session: Session = Depends(_main_db_session),
    suggestions_session: Session = Depends(_suggestions_db_session),
    current_user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """건의사항 게시글 뷰어 HTML 페이지 (GET /suggestions/{id}).

    두 권한 게이트를 가이던스에 명시된 순서로 적용한다:

    1. **고아 게이트 (404 우선)** — ``author_user_id`` 가 메인 DB ``users`` 에
       살아있는지 cross-DB 헬퍼로 확인. 고아인데 관리자가 아니면 404 로
       응답해 게시글 존재 자체를 노출하지 않는다(사용자 modify 턴 — "관리자만
       볼 수 있도록").
    2. **비밀글 게이트 (403)** — 통과한 글이 ``is_secret=True`` 면 작성자 본인
       (author_user_id 정수 비교) 또는 관리자만 본문 열람 가능. 그 외는 403
       (글 존재는 이미 목록에서 \"비밀글입니다\" 라벨로 노출되었기 때문에
       404 가 아닌 403 으로 응답해 권한 부족 사실을 명시).

    관리자는 두 게이트 모두 통과한다(비밀글 여부와 무관).

    표시 규칙:
        - 고아 글에 관리자가 진입하면 ``display_author_name`` / ``contact_email`` 을
          ``None`` 으로 마스킹한다 — 목록 페이지 정책과 동일.
        - 본문은 사용자 입력 자유 텍스트라 XSS 위험이 있다. Jinja2 의
          기본 자동 escape 에 의존하고 ``|safe`` 를 절대 쓰지 않는다.
          줄바꿈은 CSS ``white-space: pre-wrap`` (.suggestion-detail__body) 으로
          처리해 별도 변환 없이 보존한다.

    Args:
        request: FastAPI Request (Jinja2 컨텍스트용).
        suggestion_id: 게시글 PK.
        main_session: 메인 DB 세션 (cross-DB author 유효성 조회용).
        suggestions_session: 건의사항 DB 세션 (게시글 조회용).
        current_user: 로그인 사용자 또는 None.

    Returns:
        ``suggestions/detail.html`` 렌더 결과.

    Raises:
        HTTPException(404): 게시글이 없거나, 고아 글에 비관리자가 접근.
        HTTPException(403): 비밀글에 작성자 본인/관리자 외 접근.
    """
    suggestion = get_suggestion_by_id(suggestions_session, suggestion_id)
    if suggestion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 건의사항을 찾을 수 없습니다.",
        )

    is_admin = bool(current_user is not None and current_user.is_admin)
    # author_user_id 정수 비교 — 별도 DB 이지만 메인 DB users.id 는 공통 식별자.
    # 작성자가 메인 DB 에서 사라진 고아 글이면 어떤 사용자도 정수가 일치하지
    # 않으므로 자연스럽게 is_owner=False 가 된다(고아 게이트가 우선이라 실제
    # 분기 도달 전에 차단되지만, 방어적으로 의미 있는 동작).
    is_owner = bool(
        current_user is not None
        and suggestion.author_user_id is not None
        and current_user.id == suggestion.author_user_id
    )

    # ── 게이트 1: 고아 (404 우선) ─────────────────────────────────────────
    # cross-DB batch 헬퍼를 단일 row 입력으로 호출한다. IN 절 단일 쿼리이며
    # 입력이 None 1개뿐이면 헬퍼가 메인 DB 쿼리 자체를 skip 한다.
    alive_user_ids = get_alive_user_ids(main_session, [suggestion.author_user_id])
    is_orphan = is_orphan_author(suggestion.author_user_id, alive_user_ids)
    if is_orphan and not is_admin:
        # 사용자 modify 턴: "일반 사용자에게는 비노출 — 관리자만 볼 수 있도록".
        # 존재 자체를 가리려면 403 보다 404 가 적절하다(가이던스 명시).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 건의사항을 찾을 수 없습니다.",
        )

    # ── 게이트 2: 비밀글 (403) ────────────────────────────────────────────
    # 작성자 본인 또는 관리자만 통과. 비밀번호 입력 분기는 본 task 범위 외
    # (가이던스 명시 — 사용자 원문은 본인/관리자 세션 기반 권한만 명시).
    if suggestion.is_secret and not is_admin and not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비밀글은 작성자 본인 또는 관리자만 열람할 수 있습니다.",
        )

    # ── 표시 데이터 가공 (글) ────────────────────────────────────────────
    # 고아 글(관리자 전용 경로)은 작성자명/연락처를 NULL 로 마스킹한다.
    if is_orphan:
        display_author_name = None
        display_contact_email = None
    else:
        display_author_name = suggestion.author_name
        display_contact_email = suggestion.contact_email

    # ── 댓글 로드 + 고아 댓글 정책 적용 (00051-4) ─────────────────────────
    # 가이던스: "비밀글 권한 위반 시 댓글 작성/열람 모두 막는다(이미 뷰어 단에서
    # 차단되므로 상위 게이트 통과 후 댓글 렌더)" — 위 두 게이트를 통과한 시점에서만
    # 본 분기로 진입하므로 별도 추가 게이트는 불필요.
    raw_comments = list_comments_by_suggestion_id(
        suggestions_session,
        suggestion_id=suggestion.id,
    )
    # cross-DB 헬퍼를 batch 로 한 번 호출 — 댓글 작성자들의 alive 여부 + username
    # 매핑을 동시에 얻는다(N+1 회피, 가이던스 \"동일 패턴 함수 재사용\").
    comment_author_user_ids = [c.author_user_id for c in raw_comments]
    alive_username_map = get_alive_user_username_map(main_session, comment_author_user_ids)
    comment_views = apply_orphan_policy_to_comments(
        raw_comments,
        alive_username_map,
        is_admin=is_admin,
    )

    return _templates.TemplateResponse(
        request,
        "suggestions/detail.html",
        {
            "current_user": current_user,
            "is_admin": is_admin,
            "is_owner": is_owner,
            "is_orphan": is_orphan,
            "suggestion": suggestion,
            "display_author_name": display_author_name,
            "display_contact_email": display_contact_email,
            # 00051-4 — 댓글 view 리스트 (정책 적용 끝). 비관리자에게는 고아 댓글이
            # 이미 제외된 상태이며, 관리자에게는 display_author_name=None 으로
            # 마스킹된 채로 포함된다.
            "comment_views": comment_views,
        },
    )


@router.post(
    "/suggestions/{suggestion_id}/comments",
    dependencies=[Depends(ensure_same_origin)],
    response_class=RedirectResponse,
    response_model=None,
)
def create_comment_route(
    suggestion_id: int,
    body: str = Form(...),
    current_user: User = Depends(current_user_required),
    main_session: Session = Depends(_main_db_session),
    suggestions_session: Session = Depends(_suggestions_db_session),
) -> RedirectResponse:
    """건의사항 게시글에 댓글을 작성한다 (POST /suggestions/{id}/comments).

    가이던스 그대로 — 로그인 필수, 대댓글 없음, application/x-www-form-urlencoded.
    AJAX 가 아니라 폼 submit 후 PRG 패턴으로 뷰어 페이지로 303 리다이렉트한다.

    권한 게이트는 뷰어와 동일한 두 단계를 동일 순서로 적용한다 — 작성 단계에서도
    \"고아 글에 비관리자 댓글 작성\" / \"비밀글에 비-작성자 비-관리자 댓글 작성\"
    을 차단해야 GET 우회로 댓글이 달리는 경로를 막을 수 있다.

    Form 필드:
        - ``body``: 필수, ≤2000자.

    Returns:
        303 리다이렉트 (``Location: /suggestions/{id}``).

    Raises:
        HTTPException(401): 비로그인 (current_user_required dependency).
        HTTPException(404): 게시글 없음 또는 고아 글에 비관리자 접근.
        HTTPException(403): 비밀글에 작성자 본인/관리자 외 접근.
        HTTPException(400): 빈 본문 또는 길이 초과.
    """
    # ── 입력 검증 ─────────────────────────────────────────────────────────
    body_normalized = _validate_required_text(
        body, field_label="댓글 본문", max_length=_COMMENT_BODY_MAX_LENGTH
    )

    # ── 부모 게시글 조회 + 두 권한 게이트 (뷰어와 동일 순서·동일 정책) ────────
    suggestion = get_suggestion_by_id(suggestions_session, suggestion_id)
    if suggestion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 건의사항을 찾을 수 없습니다.",
        )

    is_admin = bool(current_user.is_admin)
    is_owner = bool(
        suggestion.author_user_id is not None
        and current_user.id == suggestion.author_user_id
    )

    alive_user_ids = get_alive_user_ids(main_session, [suggestion.author_user_id])
    is_orphan = is_orphan_author(suggestion.author_user_id, alive_user_ids)
    if is_orphan and not is_admin:
        # 비관리자가 고아 글에 댓글을 시도해도 글 자체 존재를 노출하지 않는다.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 건의사항을 찾을 수 없습니다.",
        )

    if suggestion.is_secret and not is_admin and not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비밀글에는 작성자 본인 또는 관리자만 댓글을 달 수 있습니다.",
        )

    # ── INSERT (트랜잭션 commit 명시) ─────────────────────────────────────
    try:
        create_suggestion_comment(
            suggestions_session,
            suggestion_id=suggestion.id,
            author_user_id=current_user.id,
            body=body_normalized,
        )
        suggestions_session.commit()
    except Exception:
        suggestions_session.rollback()
        raise

    # PRG: 작성 후 뷰어로 303 리다이렉트 (새로고침 중복 INSERT 방지).
    return RedirectResponse(
        url=f"/suggestions/{suggestion.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ──────────────────────────────────────────────────────────────
# 관리자 수용 여부 모달 저장 (00051-5)
# ──────────────────────────────────────────────────────────────


# 관리자 사유 텍스트 길이 상한 — 게시글 본문보다 짧게 두어 \"한 줄 결정 사유\"
# 수준의 입력을 유도한다. 일반 댓글 본문 상한과 동일.
_ACCEPTANCE_REASON_MAX_LENGTH: int = 2000


@router.post(
    "/suggestions/{suggestion_id}/acceptance",
    dependencies=[Depends(ensure_same_origin), Depends(admin_user_required)],
    response_class=RedirectResponse,
    response_model=None,
)
def update_acceptance_route(
    suggestion_id: int,
    acceptance_status_value: str = Form(..., alias="acceptance_status"),
    acceptance_reason: str | None = Form(default=None),
    expected_completion_date: str | None = Form(default=None),
    suggestions_session: Session = Depends(_suggestions_db_session),
) -> RedirectResponse:
    """관리자 수용 여부 체크/수정 모달 저장 (POST /suggestions/{id}/acceptance).

    가이던스 그대로 — \"수용 여부 체크/수정\" 단일 버튼이 신규/재수정 두 흐름을
    모두 처리한다. 본 라우트는 그 단일 엔드포인트로, 모달 폼이 그대로 POST 한다.

    권한:
        ``admin_user_required`` dependency 가 비로그인 401 / 비관리자 403 을 사전
        차단한다. 관리자 전용 경로라 사용자 modify 턴의 \"고아\" 방어로직 영향이
        없으며, 고아 글에 대해서도 관리자는 자유롭게 수용여부를 갱신할 수 있다
        (e2e: \"고아 글 → 관리자 진입 → 수용여부 변경\" 흐름 확인).

    Form 필드:
        - ``acceptance_status``: \"검토중\" / \"수용\" / \"일부수용\" / \"거절\" 중 하나.
        - ``acceptance_reason``: 사유 텍스트 (선택). 빈 문자열은 ``None`` 으로 정규화.
            거절 선택 시에도 사유 입력은 자유 — 가이던스 \"사유 필드 자체는 항상
            보존\".
        - ``expected_completion_date``: ``YYYY-MM-DD`` 형식. 수용/일부수용 일 때만
            의미 있으며, 그 외 상태에서는 입력값과 무관하게 ``None`` 으로 강제된다
            (사용자 원문: \"수용·일부 수용일 경우 캘린더를 통해 ... 입력\").

    Returns:
        303 리다이렉트 (``Location: /suggestions/{id}``) — PRG 패턴.

    Raises:
        HTTPException(400): 잘못된 status 값, 사유 길이 초과, 또는 잘못된 날짜 형식.
        HTTPException(404): 게시글이 존재하지 않음.
    """
    # ── status 정규화 ────────────────────────────────────────────────────
    # AcceptanceStatus enum 의 value("검토중"/"수용"/"일부수용"/"거절") 와 1:1 매칭.
    try:
        normalized_status = AcceptanceStatus(acceptance_status_value)
    except ValueError as exc:
        allowed_values = ", ".join(member.value for member in AcceptanceStatus)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"수용 여부 값이 올바르지 않습니다. 허용: {allowed_values}",
        ) from exc

    # ── 사유 정규화 (선택 필드, 빈 입력은 None) ────────────────────────────
    reason_normalized = (acceptance_reason or "").strip()
    if not reason_normalized:
        reason_normalized = None
    elif len(reason_normalized) > _ACCEPTANCE_REASON_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"사유가 너무 깁니다 (최대 {_ACCEPTANCE_REASON_MAX_LENGTH}자).",
        )

    # ── 예상 완료일 정규화 ────────────────────────────────────────────────
    # 수용/일부수용 일 때만 의미. 그 외 상태에서는 사용자가 어떤 값을 보내든
    # None 으로 강제해, 거절 → 수용 → 거절 같은 재수정 흐름에서도 stale date 가
    # 남지 않도록 한다.
    completion_date_normalized: date | None = None
    if normalized_status in (AcceptanceStatus.ACCEPTED, AcceptanceStatus.PARTIAL):
        date_input = (expected_completion_date or "").strip()
        if date_input:
            try:
                completion_date_normalized = date.fromisoformat(date_input)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="예상 완료일 형식이 올바르지 않습니다 (YYYY-MM-DD).",
                ) from exc

    # ── DB 갱신 ──────────────────────────────────────────────────────────
    try:
        result = update_suggestion_acceptance(
            suggestions_session,
            suggestion_id=suggestion_id,
            acceptance_status=normalized_status,
            acceptance_reason=reason_normalized,
            expected_completion_date=completion_date_normalized,
        )
        if result is None:
            # 게시글이 사라진 경우 — 라우트 단에서 명시적으로 404.
            # rollback 은 finally 블록 격이지만 본 분기에서는 row 변경이 없어 무해.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 건의사항을 찾을 수 없습니다.",
            )
        suggestions_session.commit()
    except HTTPException:
        # 404 분기 — 트랜잭션 변경 없으므로 rollback 으로 충분.
        suggestions_session.rollback()
        raise
    except Exception:
        suggestions_session.rollback()
        raise

    # PRG: 저장 후 뷰어로 303 — 새로고침 시 중복 갱신 방지(idempotent 라 무해
    # 하지만 관성상 PRG 유지).
    return RedirectResponse(
        url=f"/suggestions/{suggestion_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ──────────────────────────────────────────────────────────────
# 작성자 본인의 글 수정·삭제 (00052-4)
# ──────────────────────────────────────────────────────────────


def _apply_owner_edit_gates(
    main_session: Session,
    suggestions_session: Session,
    *,
    suggestion_id: int,
    current_user: User,
) -> Suggestion:
    """수정·삭제 라우트가 공통으로 적용해야 하는 4단 게이트.

    적용 순서는 ``view_suggestion_page`` 와 일관되며, 마지막에 작성자 본인
    검증을 추가한다 — 가이던스 그대로:

    1. 게시글 존재 (404)
    2. 고아 게이트 (404 비관리자) — 작성자 user 가 사라진 글은 비관리자에게
       존재 자체를 가린다. (수정/삭제 흐름에서도 동일 정책으로 처리.)
    3. 비밀글 게이트 (403 비-작성자/비-관리자) — 작성자 본인은 자동 통과.
    4. 작성자 본인 게이트 (403 비-작성자) — 관리자라도 본인 글이 아니면 거절.
       사용자 원문이 \"작성자\" 한정이라 보수적으로 본인만 허용한다.

    Args:
        main_session: 메인 DB 세션 (cross-DB 고아 판정용).
        suggestions_session: 건의사항 DB 세션 (게시글 조회용).
        suggestion_id: 게시글 PK.
        current_user: 로그인 사용자(필수).

    Returns:
        4단 게이트를 통과한 ``Suggestion`` ORM 인스턴스 (수정/삭제 대상).

    Raises:
        HTTPException(404): 게시글이 없거나, 고아 글에 비관리자 접근.
        HTTPException(403): 비밀글에 비-작성자 비-관리자 접근, 또는 비-작성자 접근.
    """
    suggestion = get_suggestion_by_id(suggestions_session, suggestion_id)
    if suggestion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 건의사항을 찾을 수 없습니다.",
        )

    is_admin = bool(current_user.is_admin)
    is_owner = bool(
        suggestion.author_user_id is not None
        and current_user.id == suggestion.author_user_id
    )

    # 게이트 2: 고아 (404 우선) — 비관리자에게 존재 자체를 노출하지 않는다.
    alive_user_ids = get_alive_user_ids(main_session, [suggestion.author_user_id])
    is_orphan = is_orphan_author(suggestion.author_user_id, alive_user_ids)
    if is_orphan and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 건의사항을 찾을 수 없습니다.",
        )

    # 게이트 3: 비밀글 (403). 작성자 본인은 자동 통과(아래 게이트 4 에서 별도 검증).
    if suggestion.is_secret and not is_admin and not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비밀글은 작성자 본인 또는 관리자만 열람할 수 있습니다.",
        )

    # 게이트 4: 작성자 본인. 관리자는 본 task 범위에서 수정·삭제 불가.
    if not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="자신이 작성한 글만 수정·삭제할 수 있습니다.",
        )

    return suggestion


@router.get(
    "/suggestions/{suggestion_id}/edit",
    response_class=HTMLResponse,
    response_model=None,
)
def edit_suggestion_page(
    request: Request,
    suggestion_id: int,
    main_session: Session = Depends(_main_db_session),
    suggestions_session: Session = Depends(_suggestions_db_session),
    current_user: User = Depends(current_user_required),
) -> HTMLResponse:
    """건의사항 수정 폼 페이지 (GET /suggestions/{id}/edit) — 작성자 본인 전용.

    ``_apply_owner_edit_gates`` 가 4단 게이트(존재/고아/비밀글/작성자) 를
    적용한다. 통과한 ``Suggestion`` 을 그대로 폼에 prefill 한다.

    수정 가능 필드는 제목/본문/비밀글 여부 3개로 제한된다(가이던스: 작성자명·
    비밀번호·연락처 이메일은 본 task 범위 밖). 비밀번호 입력 필드는 폼에
    아예 두지 않으므로 의도치 않은 비밀번호 변경도 발생하지 않는다.

    Args:
        request: FastAPI Request (Jinja2 컨텍스트용).
        suggestion_id: 게시글 PK.
        main_session: 메인 DB 세션 (cross-DB 고아 판정용).
        suggestions_session: 건의사항 DB 세션 (게시글 조회용).
        current_user: 로그인 사용자(필수). 비로그인은 401 (dependency).

    Returns:
        ``suggestions/edit.html`` 렌더 결과.

    Raises:
        HTTPException(401): 비로그인 (current_user_required dependency).
        HTTPException(404): 게시글 없음 또는 고아 글에 비관리자 접근.
        HTTPException(403): 비밀글 비-작성자 접근 또는 비-작성자 접근.
    """
    suggestion = _apply_owner_edit_gates(
        main_session,
        suggestions_session,
        suggestion_id=suggestion_id,
        current_user=current_user,
    )

    return _templates.TemplateResponse(
        request,
        "suggestions/edit.html",
        {
            "current_user": current_user,
            "suggestion": suggestion,
        },
    )


@router.post(
    "/suggestions/{suggestion_id}/edit",
    dependencies=[Depends(ensure_same_origin)],
    response_class=RedirectResponse,
    response_model=None,
)
def update_suggestion_route(
    suggestion_id: int,
    title: str = Form(...),
    body: str = Form(...),
    is_secret: str | None = Form(default=None),
    main_session: Session = Depends(_main_db_session),
    suggestions_session: Session = Depends(_suggestions_db_session),
    current_user: User = Depends(current_user_required),
) -> RedirectResponse:
    """건의사항 수정 처리 (POST /suggestions/{id}/edit) — 작성자 본인 전용.

    GET 폼과 동일한 4단 게이트를 통과해야만 갱신이 수행된다(URL 직접 호출
    우회 방지). 수정 가능 필드는 제목/본문/비밀글 여부로 한정한다.

    Form 필드:
        - ``title``: 필수, ≤255자.
        - ``body``: 필수, ≤20000자.
        - ``is_secret``: 체크박스. 체크되어 있으면 truthy 문자열, 해제 시 None.

    Returns:
        303 리다이렉트 (``Location: /suggestions/{id}``) — PRG.

    Raises:
        HTTPException(401): 비로그인.
        HTTPException(404): 게시글 없음 또는 고아 글에 비관리자.
        HTTPException(403): 비밀글 비-작성자 또는 비-작성자.
        HTTPException(400): 빈 입력 또는 길이 초과.
    """
    # 4단 게이트 — 게시글 존재/고아/비밀글/작성자 본인.
    _apply_owner_edit_gates(
        main_session,
        suggestions_session,
        suggestion_id=suggestion_id,
        current_user=current_user,
    )

    # ── 입력 정규화·검증 ──────────────────────────────────────────────────
    title_normalized = _validate_required_text(
        title, field_label="제목", max_length=_TITLE_MAX_LENGTH
    )
    body_normalized = _validate_required_text(
        body, field_label="본문", max_length=_BODY_MAX_LENGTH
    )
    is_secret_bool = is_secret is not None and is_secret.strip() != ""

    # ── UPDATE (트랜잭션은 라우트 끝에서 명시 commit) ──────────────────────
    try:
        result = update_suggestion(
            suggestions_session,
            suggestion_id=suggestion_id,
            title=title_normalized,
            body=body_normalized,
            is_secret=is_secret_bool,
        )
        if result is None:
            # 게이트 통과와 갱신 사이에 row 가 사라진 race — 보수적으로 404.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 건의사항을 찾을 수 없습니다.",
            )
        suggestions_session.commit()
    except HTTPException:
        suggestions_session.rollback()
        raise
    except Exception:
        suggestions_session.rollback()
        raise

    return RedirectResponse(
        url=f"/suggestions/{suggestion_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/suggestions/{suggestion_id}/delete",
    dependencies=[Depends(ensure_same_origin)],
    response_class=RedirectResponse,
    response_model=None,
)
def delete_suggestion_route(
    suggestion_id: int,
    main_session: Session = Depends(_main_db_session),
    suggestions_session: Session = Depends(_suggestions_db_session),
    current_user: User = Depends(current_user_required),
) -> RedirectResponse:
    """건의사항 삭제 처리 (POST /suggestions/{id}/delete) — 작성자 본인 전용.

    동일한 4단 게이트를 적용한 뒤 게시글을 삭제한다. 소속 댓글은 모델
    cascade(``all, delete-orphan`` + DB ``ON DELETE CASCADE``) 로 함께 정리된다.
    성공 시 목록 페이지(``/suggestions``) 로 303 리다이렉트한다.

    Returns:
        303 리다이렉트 (``Location: /suggestions``) — PRG.

    Raises:
        HTTPException(401): 비로그인.
        HTTPException(404): 게시글 없음 또는 고아 글에 비관리자.
        HTTPException(403): 비밀글 비-작성자 또는 비-작성자.
    """
    # 4단 게이트 — 게시글 존재/고아/비밀글/작성자 본인.
    _apply_owner_edit_gates(
        main_session,
        suggestions_session,
        suggestion_id=suggestion_id,
        current_user=current_user,
    )

    # ── DELETE (트랜잭션은 라우트 끝에서 명시 commit) ──────────────────────
    try:
        deleted = delete_suggestion(
            suggestions_session,
            suggestion_id=suggestion_id,
        )
        if not deleted:
            # 게이트 통과 직후 사라진 race 케이스. 사용자 경험상 404 보다
            # \"이미 삭제됨\" 이 자연스러우나, 정합성 차원에서 404 로 응답한다.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="해당 건의사항을 찾을 수 없습니다.",
            )
        suggestions_session.commit()
    except HTTPException:
        suggestions_session.rollback()
        raise
    except Exception:
        suggestions_session.rollback()
        raise

    # 글이 사라졌으니 뷰어가 아니라 목록으로 보낸다.
    return RedirectResponse(
        url="/suggestions",
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = [
    "router",
]
