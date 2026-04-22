"""인증 라우트.

사용자가 실제로 회원가입·로그인·로그아웃할 수 있게 HTTP 엔드포인트를 제공한다.
라우트는 :class:`APIRouter` 하나에 모아 ``app/web/main.py`` 의
``create_app()`` 에서 ``include_router(router)`` 로 mount 된다.

엔드포인트 목록:
    - ``GET  /register`` — 회원가입 폼 HTML (이미 로그인이면 / 로 redirect).
    - ``POST /auth/register`` — 가입 처리. 성공 시 세션 쿠키 + / 로 redirect.
    - ``GET  /login`` — 로그인 폼 HTML (이미 로그인이면 / 로 redirect).
    - ``POST /auth/login`` — 로그인 처리. 성공 시 세션 쿠키 + / 로 redirect.
    - ``POST /auth/logout`` — 세션 삭제 + 쿠키 제거 + / 로 redirect.
    - ``GET  /auth/me`` — 현재 사용자 JSON (비로그인 ``{"user": null}``).

설계 원칙 (``docs/auth_ui_design.md`` §6 참고):
    - POST 라우트는 :func:`ensure_same_origin` 을 의존성으로 걸어 가벼운
      CSRF 방어. CSRF 토큰은 쓰지 않는다 (로컬 전제).
    - redirect 는 303 (POST-redirect-GET 패턴).
    - 로그인 후 리다이렉트 대상은 next 쿼리 파라미터를 받지 않고 항상 / 로
      보낸다 (사용자 원문 미언급 — 단순화).
    - 실패 시 동일 템플릿을 ``flash`` 메시지와 함께 재렌더한다 (status 400).
    - 쿠키 속성은 :mod:`app.auth.constants` 의 상수를 그대로 사용한다.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.orm import Session

from app.auth.constants import (
    COOKIE_HTTP_ONLY,
    COOKIE_PATH,
    COOKIE_SAMESITE,
    COOKIE_SECURE,
    SESSION_COOKIE_NAME,
    SESSION_LIFETIME_DAYS,
)
from app.auth.dependencies import (
    _auth_db_session,
    current_user_optional,
    ensure_same_origin,
)
from app.auth.service import (
    DuplicateUsernameError,
    PasswordPolicyError,
    UsernamePolicyError,
    authenticate,
    create_session,
    create_user,
    delete_session,
)
from app.db.models import User

# 기존 웹 템플릿과 동일한 디렉터리(app/web/templates)를 공유해
# base.html 레이아웃 상속을 재사용한다.
_WEB_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "web" / "templates"
_templates: Jinja2Templates = Jinja2Templates(directory=str(_WEB_TEMPLATES_DIR))


router = APIRouter(tags=["auth"])


# ──────────────────────────────────────────────────────────────
# 쿠키 헬퍼
# ──────────────────────────────────────────────────────────────


def _apply_session_cookie(
    response: Response,
    session_id: str,
    *,
    lifetime_days: int = SESSION_LIFETIME_DAYS,
) -> None:
    """주어진 응답에 세션 쿠키를 설정한다.

    쿠키 속성(HttpOnly/SameSite/Secure/Path) 는 :mod:`app.auth.constants`
    에 정의된 값을 그대로 사용한다.

    Args:
        response: 쿠키를 실을 응답 객체(리다이렉트 또는 일반 응답).
        session_id: 발급된 세션 토큰.
        lifetime_days: 쿠키 max_age 계산에 쓰일 일수. 기본
            :data:`SESSION_LIFETIME_DAYS`.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=lifetime_days * 86400,
        httponly=COOKIE_HTTP_ONLY,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path=COOKIE_PATH,
    )


def _clear_session_cookie(response: Response) -> None:
    """주어진 응답에서 세션 쿠키를 제거하는 Set-Cookie 를 설정한다."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=COOKIE_PATH,
    )


# ──────────────────────────────────────────────────────────────
# 회원가입
# ──────────────────────────────────────────────────────────────


@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    current_user: User | None = Depends(current_user_optional),
) -> Response:
    """회원가입 폼 HTML 을 반환한다. 이미 로그인 상태면 / 로 redirect."""
    if current_user is not None:
        return RedirectResponse("/", status_code=303)
    return _templates.TemplateResponse(
        request,
        "register.html",
        {
            "current_user": None,
            "flash": None,
            "username_value": "",
            "email_value": "",
        },
    )


@router.post("/auth/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    email: str | None = Form(default=None),
    session: Session = Depends(_auth_db_session),
    _same_origin: None = Depends(ensure_same_origin),
) -> Response:
    """회원가입 요청을 처리한다.

    성공하면 세션을 발급하고 쿠키를 심어 / 로 303 redirect 한다.
    검증/중복 실패 시 동일 폼을 flash 메시지와 함께 400 으로 재렌더한다.
    """
    # 사용자 생성 — 정책 위반/중복은 도메인 예외로 잡힌다.
    try:
        created_user = create_user(
            session,
            username=username,
            password=password,
            email=email,
        )
    except DuplicateUsernameError:
        session.rollback()
        logger.info("회원가입 실패 — username 중복: {!r}", username)
        return _templates.TemplateResponse(
            request,
            "register.html",
            {
                "current_user": None,
                "flash": "이미 사용 중인 아이디입니다.",
                "username_value": username,
                "email_value": email or "",
            },
            status_code=400,
        )
    except (UsernamePolicyError, PasswordPolicyError, ValueError) as exc:
        # email 형식 오류 등 일반 ValueError 도 여기서 잡아 폼 에러로 표시.
        session.rollback()
        logger.info("회원가입 실패 — 정책 위반: {}", exc)
        return _templates.TemplateResponse(
            request,
            "register.html",
            {
                "current_user": None,
                "flash": str(exc),
                "username_value": username,
                "email_value": email or "",
            },
            status_code=400,
        )

    session.commit()

    # 가입 완료 후 자동 로그인 처리 — 세션을 바로 발급한다.
    user_session = create_session(session, created_user)
    session.commit()

    redirect_response = RedirectResponse("/", status_code=303)
    _apply_session_cookie(redirect_response, user_session.session_id)
    logger.info("회원가입 성공 + 자동 로그인: user_id={}", created_user.id)
    return redirect_response


# ──────────────────────────────────────────────────────────────
# 로그인
# ──────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    current_user: User | None = Depends(current_user_optional),
) -> Response:
    """로그인 폼 HTML 을 반환한다. 이미 로그인 상태면 / 로 redirect."""
    if current_user is not None:
        return RedirectResponse("/", status_code=303)
    return _templates.TemplateResponse(
        request,
        "login.html",
        {
            "current_user": None,
            "flash": None,
            "username_value": "",
        },
    )


@router.post("/auth/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(_auth_db_session),
    _same_origin: None = Depends(ensure_same_origin),
) -> Response:
    """로그인 요청을 처리한다.

    성공하면 세션을 발급하고 쿠키를 심어 / 로 303 redirect 한다.
    실패 시(아이디 없음/비밀번호 불일치) 이유를 구별하지 않고 동일 메시지로
    400 재렌더 — username enumeration 방지.
    """
    authenticated_user = authenticate(
        session,
        username=username,
        password=password,
    )
    if authenticated_user is None:
        logger.info("로그인 실패: username={!r}", username)
        return _templates.TemplateResponse(
            request,
            "login.html",
            {
                "current_user": None,
                "flash": "아이디 또는 비밀번호가 올바르지 않습니다.",
                "username_value": username,
            },
            status_code=400,
        )

    user_session = create_session(session, authenticated_user)
    session.commit()

    redirect_response = RedirectResponse("/", status_code=303)
    _apply_session_cookie(redirect_response, user_session.session_id)
    logger.info("로그인 성공: user_id={}", authenticated_user.id)
    return redirect_response


# ──────────────────────────────────────────────────────────────
# 로그아웃
# ──────────────────────────────────────────────────────────────


@router.post("/auth/logout")
def logout_submit(
    request: Request,
    session: Session = Depends(_auth_db_session),
    _same_origin: None = Depends(ensure_same_origin),
) -> Response:
    """현재 세션을 삭제하고 쿠키를 제거한 뒤 / 로 redirect 한다.

    비로그인 상태에서 호출되어도 오류 없이 / 로 redirect 한다 (멱등).
    """
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value:
        delete_session(session, cookie_value)
        session.commit()

    redirect_response = RedirectResponse("/", status_code=303)
    _clear_session_cookie(redirect_response)
    return redirect_response


# ──────────────────────────────────────────────────────────────
# 현재 사용자 조회 (JSON)
# ──────────────────────────────────────────────────────────────


@router.get("/auth/me")
def me(
    current_user: User | None = Depends(current_user_optional),
) -> JSONResponse:
    """현재 로그인된 사용자 정보를 JSON 으로 반환한다.

    비로그인이면 ``{"user": null}``. 비밀번호 해시 등 민감 정보는 제외한다.
    """
    if current_user is None:
        return JSONResponse({"user": None})
    return JSONResponse(
        {
            "user": {
                "id": current_user.id,
                "username": current_user.username,
                "email": current_user.email,
                "is_admin": current_user.is_admin,
            }
        }
    )


__all__ = ["router"]
