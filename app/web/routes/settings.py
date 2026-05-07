"""개인 설정 페이지 라우터 (task 00049-2).

로그인 사용자가 자기 비밀번호·이메일·이메일 수신 토글·조직 소속을 변경할 수 있는
단일 페이지를 제공한다.

엔드포인트:
    GET  /settings                 설정 페이지 HTML (비로그인 → /login?next=/settings)
    POST /settings/password        비밀번호 변경 (현재 비밀번호 확인 → 전 세션 삭제 → 새 세션 발급)
    POST /settings/email           이메일 주소 변경
    POST /settings/notification    이메일 수신 토글 변경
    POST /settings/organizations   조직 소속 변경 (0개 이상 다중 선택)

보호:
    GET 는 비로그인 시 /login?next=/settings 로 리다이렉트.
    POST 4개는 current_user_required + ensure_same_origin 으로 보호한다.

flash 설계:
    성공 시: POST 처리 후 /settings?flash_msg=...&flash_section=...&flash_type=success 로
             303 리다이렉트 (PRG 패턴).
    실패 시: 동일 템플릿을 flash + flash_section + flash_type='error' 와 함께 400 재렌더.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
from app.auth.dependencies import current_user_optional, current_user_required, ensure_same_origin
from app.auth.service import (
    PasswordPolicyError,
    change_email,
    change_email_subscribed,
    change_password,
    create_session,
    verify_password,
)
from app.db.models import User
from app.db.session import SessionLocal
from app.organizations.service import (
    build_organization_tree,
    get_user_organization_ids,
    list_all_organizations,
    set_user_organizations,
)
from app.web.template_filters import register_kst_filters

_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"
_templates: Jinja2Templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_kst_filters(_templates)

router = APIRouter(tags=["settings"])


# ──────────────────────────────────────────────────────────────
# 내부 세션 의존성
# ──────────────────────────────────────────────────────────────


def _settings_db_session() -> Iterator[Session]:
    """설정 라우터 전용 요청 단위 DB 세션 의존성.

    app.auth.dependencies._auth_db_session 과 동일한 패턴 — 순환 import 를
    피하기 위해 독립적으로 선언한다.
    """
    session = SessionLocal()
    logger.debug("settings DB 세션 open")
    try:
        yield session
    finally:
        session.close()
        logger.debug("settings DB 세션 close")


# ──────────────────────────────────────────────────────────────
# 쿠키 헬퍼
# ──────────────────────────────────────────────────────────────


def _apply_session_cookie(response: Response, session_id: str) -> None:
    """응답에 세션 쿠키를 설정한다.

    app.auth.routes._apply_session_cookie 와 동일한 로직.
    비밀번호 변경 후 새 세션 발급 시 사용한다.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_LIFETIME_DAYS * 86400,
        httponly=COOKIE_HTTP_ONLY,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path=COOKIE_PATH,
    )


# ──────────────────────────────────────────────────────────────
# 공통 컨텍스트 빌더
# ──────────────────────────────────────────────────────────────


def _build_settings_context(
    request: Request,
    session: Session,
    current_user: User,
    *,
    flash: str | None = None,
    flash_section: str | None = None,
    flash_type: str = "error",
) -> dict:
    """설정 페이지 렌더에 필요한 공통 템플릿 컨텍스트를 반환한다.

    Args:
        request: FastAPI 요청 객체 (Jinja2 TemplateResponse 용).
        session: DB 세션.
        current_user: 로그인된 사용자.
        flash: 표시할 메시지. None 이면 메시지 없음.
        flash_section: 메시지가 속한 섹션 ('password'·'email'·'notification'·'organizations').
        flash_type: 'success' 또는 'error'.
    """
    all_orgs = list_all_organizations(session)
    org_tree = build_organization_tree(all_orgs)
    user_org_ids = set(get_user_organization_ids(session, current_user.id))
    return {
        "current_user": current_user,
        "org_tree": org_tree,
        "user_org_ids": user_org_ids,
        "flash": flash,
        "flash_section": flash_section,
        "flash_type": flash_type,
    }


# ──────────────────────────────────────────────────────────────
# GET /settings — 설정 페이지
# ──────────────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    flash_msg: str | None = Query(default=None),
    flash_section: str | None = Query(default=None),
    flash_type: str = Query(default="success"),
    session: Session = Depends(_settings_db_session),
    current_user: User | None = Depends(current_user_optional),
) -> Response:
    """개인 설정 페이지 HTML 을 반환한다.

    비로그인 시 /login?next=/settings 로 리다이렉트한다.
    성공 POST 에서 리다이렉트될 때 flash_msg/flash_section/flash_type 쿼리 파라미터로
    성공 메시지를 전달받아 표시한다.

    Args:
        request: FastAPI 요청 객체.
        flash_msg: 성공 POST 에서 전달한 flash 메시지 (옵션).
        flash_section: 메시지가 속한 섹션 (옵션).
        flash_type: 메시지 유형. 기본 'success'.
        session: DB 세션.
        current_user: 로그인 사용자 또는 None.
    """
    if current_user is None:
        return RedirectResponse(
            url="/login?next=/settings",
            status_code=status.HTTP_302_FOUND,
        )

    context = _build_settings_context(
        request,
        session,
        current_user,
        flash=flash_msg,
        flash_section=flash_section,
        flash_type=flash_type,
    )
    return _templates.TemplateResponse(request, "settings.html", context)


# ──────────────────────────────────────────────────────────────
# POST /settings/password — 비밀번호 변경
# ──────────────────────────────────────────────────────────────


@router.post("/settings/password")
def settings_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    session: Session = Depends(_settings_db_session),
    current_user: User = Depends(current_user_required),
    _same_origin: None = Depends(ensure_same_origin),
) -> Response:
    """비밀번호를 변경한다.

    처리 순서:
        1. 현재 비밀번호 일치 여부 검증.
        2. 새 비밀번호/확인 비밀번호 일치 여부 검증.
        3. 비밀번호 정책 검증 + 해시 업데이트 + 모든 세션 삭제.
        4. 새 세션 발급 + 쿠키 심어 /settings 로 303 리다이렉트.

    실패 시: 동일 템플릿을 flash_section='password' 와 함께 400 재렌더.

    Args:
        request: FastAPI 요청 객체.
        current_password: 현재 비밀번호 평문.
        new_password: 새 비밀번호 평문.
        confirm_password: 새 비밀번호 확인 평문.
        session: DB 세션.
        current_user: 로그인 사용자 (비로그인 → 401).
    """
    # current_user는 _auth_db_session 소속 인스턴스다. settings 세션 기준으로
    # 재조회해야 이후 change_password의 flush/commit이 실제 UPDATE로 이어진다.
    user = session.get(User, current_user.id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="사용자를 찾을 수 없습니다.",
        )

    def _error(msg: str) -> Response:
        """실패 시 settings 페이지를 400 으로 재렌더하는 내부 헬퍼."""
        context = _build_settings_context(
            request, session, user,
            flash=msg, flash_section="password", flash_type="error",
        )
        return _templates.TemplateResponse(
            request, "settings.html", context, status_code=400,
        )

    # 현재 비밀번호 검증
    if not verify_password(current_password, user.password_hash):
        return _error("현재 비밀번호가 올바르지 않습니다.")

    # 새 비밀번호 확인 일치
    if new_password != confirm_password:
        return _error("새 비밀번호와 확인 비밀번호가 일치하지 않습니다.")

    # 정책 검증 + 해시 업데이트 + 모든 세션 삭제
    try:
        change_password(session, user, new_password=new_password)
    except PasswordPolicyError as exc:
        return _error(str(exc))

    # 새 세션 발급 (비밀번호 변경으로 인해 기존 세션이 모두 삭제됐으므로 재발급 필수)
    new_user_session = create_session(session, user)
    session.commit()

    redirect_response = RedirectResponse(
        url="/settings?flash_msg=비밀번호가 변경되었습니다.&flash_section=password&flash_type=success",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _apply_session_cookie(redirect_response, new_user_session.session_id)
    logger.info("비밀번호 변경 완료: user_id={}", user.id)
    return redirect_response


# ──────────────────────────────────────────────────────────────
# POST /settings/email — 이메일 변경
# ──────────────────────────────────────────────────────────────


@router.post("/settings/email")
def settings_email(
    request: Request,
    new_email: str = Form(default=""),
    session: Session = Depends(_settings_db_session),
    current_user: User = Depends(current_user_required),
    _same_origin: None = Depends(ensure_same_origin),
) -> Response:
    """이메일 주소를 변경한다.

    빈 문자열을 전송하면 이메일을 제거한다(None 처리).
    간이 형식 검증 실패 시 flash 메시지와 함께 400 재렌더한다.

    Args:
        request: FastAPI 요청 객체.
        new_email: 새 이메일 주소. 빈 문자열이면 이메일 제거.
        session: DB 세션.
        current_user: 로그인 사용자.
    """
    # current_user는 _auth_db_session 소속 인스턴스다. settings 세션 기준으로
    # 재조회해야 change_email의 flush/commit이 실제 UPDATE로 이어진다.
    user = session.get(User, current_user.id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="사용자를 찾을 수 없습니다.",
        )

    try:
        change_email(session, user, new_email=new_email or None)
        session.commit()
    except ValueError as exc:
        context = _build_settings_context(
            request, session, user,
            flash=str(exc), flash_section="email", flash_type="error",
        )
        return _templates.TemplateResponse(
            request, "settings.html", context, status_code=400,
        )

    logger.info("이메일 변경 완료: user_id={}", user.id)
    return RedirectResponse(
        url="/settings?flash_msg=이메일이 변경되었습니다.&flash_section=email&flash_type=success",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ──────────────────────────────────────────────────────────────
# POST /settings/notification — 이메일 수신 토글
# ──────────────────────────────────────────────────────────────


@router.post("/settings/notification")
def settings_notification(
    request: Request,
    email_subscribed: str | None = Form(default=None),
    session: Session = Depends(_settings_db_session),
    current_user: User = Depends(current_user_required),
    _same_origin: None = Depends(ensure_same_origin),
) -> Response:
    """이메일 수신 동의 여부를 변경한다.

    폼의 체크박스(name='email_subscribed', value='1')가 체크되어 있으면
    '1' 이 전송되고, 체크 해제 시 필드 자체가 전송되지 않아 None 이 된다.

    Args:
        request: FastAPI 요청 객체.
        email_subscribed: 체크 시 '1', 미체크 시 None.
        session: DB 세션.
        current_user: 로그인 사용자.
    """
    # current_user는 _auth_db_session 소속 인스턴스다. settings 세션 기준으로
    # 재조회해야 change_email_subscribed의 flush/commit이 실제 UPDATE로 이어진다.
    user = session.get(User, current_user.id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="사용자를 찾을 수 없습니다.",
        )

    subscribed = email_subscribed == "1"
    change_email_subscribed(session, user, subscribed=subscribed)
    session.commit()

    label = "켰습니다" if subscribed else "껐습니다"
    # 알림을 끈 상태는 warning 톤으로 표시해 한 눈에 비활성 상태를 인식할 수 있도록 한다.
    flash_type = "success" if subscribed else "warning"
    logger.info("이메일 수신 설정 변경 완료: user_id={} subscribed={}", user.id, subscribed)
    return RedirectResponse(
        url=f"/settings?flash_msg=이메일 알림을 {label}.&flash_section=notification&flash_type={flash_type}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ──────────────────────────────────────────────────────────────
# POST /settings/organizations — 조직 소속 변경
# ──────────────────────────────────────────────────────────────


@router.post("/settings/organizations")
def settings_organizations(
    request: Request,
    organization_ids: list[int] = Form(default=[]),
    session: Session = Depends(_settings_db_session),
    current_user: User = Depends(current_user_required),
    _same_origin: None = Depends(ensure_same_origin),
) -> Response:
    """조직 소속을 변경한다.

    체크박스 다중 선택으로 전달된 organization_ids 로 기존 매핑을 교체한다.
    빈 리스트(아무것도 선택 안 함)도 유효 — 모든 조직 소속을 해제한다.

    존재하지 않는 organization_id 가 포함된 경우 DB 무결성 오류가 발생해
    flash 메시지와 함께 400 으로 재렌더한다.

    Args:
        request: FastAPI 요청 객체.
        organization_ids: 새로 소속시킬 조직 PK 목록. 없으면 빈 리스트.
        session: DB 세션.
        current_user: 로그인 사용자.
    """
    from sqlalchemy.exc import IntegrityError

    try:
        set_user_organizations(session, current_user.id, organization_ids)
        session.commit()
    except IntegrityError:
        session.rollback()
        context = _build_settings_context(
            request, session, current_user,
            flash="유효하지 않은 조직이 포함되어 있습니다.",
            flash_section="organizations",
            flash_type="error",
        )
        return _templates.TemplateResponse(
            request, "settings.html", context, status_code=400,
        )

    logger.info(
        "조직 소속 변경 완료: user_id={} organization_ids={}",
        current_user.id, organization_ids,
    )
    return RedirectResponse(
        url="/settings?flash_msg=조직 소속이 저장되었습니다.&flash_section=organizations&flash_type=success",
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = ["router"]
