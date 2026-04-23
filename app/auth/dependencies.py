"""FastAPI ``Depends`` 용 인증 헬퍼.

본 모듈은 라우트에서 바로 쓸 수 있는 세 가지 의존성을 제공한다.

- :func:`current_user_optional` — 로그인되어 있으면 ``User``, 아니면 ``None``.
  **비로그인 열람 경로의 기본값**으로 사용된다.
- :func:`current_user_required` — 인증 필수. 비로그인 시 ``HTTPException(401)``.
- :func:`ensure_same_origin` — POST 라우트의 가벼운 CSRF 방어. Origin/Referer
  헤더가 현재 요청 host 와 일치하는지 확인. 사용자 원문 "CSRF skip (로컬 전제).
  POST same-origin 체크" 를 구현한다.

설계 메모:
    라우트 wiring(``app.include_router(...)``) 과 ``main.py`` 에서의 실제
    의존성 연결은 다음 subtask(00021-3) 에서 수행한다. 본 모듈은 import 만
    되고 wiring 되지 않아도 정상 동작해야 한다.

    ``main.py`` 에 이미 선언된 ``get_session`` 과의 순환 import 를 피하기 위해,
    본 모듈은 ``app.db.session.SessionLocal`` 을 직접 사용하는 자체
    ``_auth_db_session`` 의존성을 둔다. 이는 FastAPI 가 요청별로 새 세션을
    yield 하고 종료 시 close 하도록 한다.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy.orm import Session

from app.auth.constants import SESSION_COOKIE_NAME
from app.auth.service import get_active_session
from app.db.models import User
from app.db.session import SessionLocal


def _auth_db_session() -> Iterator[Session]:
    """요청 단위 DB 세션을 yield 하는 내부 의존성.

    ``app/web/main.py`` 의 ``get_session`` 과 동일한 패턴이지만, auth 모듈이
    ``main`` 에 의존하지 않도록 독립적으로 선언한다. FastAPI 의존성 시스템은
    중복 주입을 방지하지 않으므로 같은 요청에서 이 함수와 ``get_session`` 이
    모두 호출되면 세션이 2개 생성될 수 있다 — 인증 플로우의 DB 질의는 가볍고
    (PK lookup 수준) 쓰기도 단일 row 이므로 실무상 문제가 없다.
    """
    # 00030-3 — 세션 lifecycle 추적용 DEBUG 로그. 주 sink 는 FastAPI 요청
    # 미들웨어(observability.py) 가 request_id 로 스코프를 잡아두므로, 이 한
    # 줄만으로도 "한 요청에 auth 세션이 몇 개 열렸는지" 를 눈으로 확인할 수
    # 있다. DEBUG 한정이라 운영 레벨(INFO) 에서는 노이즈가 되지 않는다.
    session = SessionLocal()
    logger.debug("auth DB 세션 open")
    try:
        yield session
    finally:
        session.close()
        logger.debug("auth DB 세션 close")


def current_user_optional(
    request: Request,
    session: Session = Depends(_auth_db_session),
) -> User | None:
    """현재 로그인된 ``User`` 를 반환한다. 비로그인이면 ``None``.

    동작:
        1. 쿠키에서 ``SESSION_COOKIE_NAME`` 값을 읽는다.
        2. ``get_active_session`` 으로 만료 여부를 포함해 검증한다.
        3. 유효하면 ``UserSession.user`` relationship 으로 User 를 반환.

    반환값이 ``None`` 인 경우 라우트는 "비로그인 사용자" 로 처리해야 한다.
    쿠키 자체를 삭제하는 책임은 이 의존성이 지지 않는다 — 라우트가 필요할 때
    (예: 로그인 페이지 재렌더) 명시적으로 처리한다.

    Args:
        request: 쿠키 접근용 요청 객체.
        session: 의존성으로 주입된 DB 세션.

    Returns:
        로그인된 ``User`` 또는 ``None``.
    """
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        # 00030-3 — 비로그인 분기 가시화. 사용자 원문의 "admin 로그인 후 500
        # 에러" 원인 추적 시, 쿠키가 정말 넘어왔는지·로그인 상태인지를 구분
        # 하려면 이 지점에서 "쿠키 없음" 이 로그에 남아야 한다.
        logger.debug("current_user_optional: 세션 쿠키 없음 → 비로그인")
        return None

    user_session = get_active_session(session, cookie_value)
    if user_session is None:
        # 쿠키는 있으나 만료/미존재. 만료된 세션을 물고 들어오는 요청을
        # 식별해야 "로그인 직후에도 500" 같은 증상을 세션 issue 와 분리해
        # 디버그할 수 있다. session_id 는 앞 8자만 남겨 민감정보 유출을 최소화.
        logger.debug(
            "current_user_optional: 세션 검증 실패 (만료/미존재) session_id_prefix={!r}",
            cookie_value[:8],
        )
        return None

    # relationship 으로 lazy-load. SELECT 1회 추가되지만 인증 플로우 빈도가
    # 낮아 문제없다. 필요 시 future 에서 selectinload 로 바꿀 수 있다.
    user = user_session.user
    logger.debug(
        "current_user_optional: 세션 검증 성공 user_id={} is_admin={} session_id_prefix={!r}",
        user.id,
        user.is_admin,
        cookie_value[:8],
    )
    return user


def current_user_required(
    user: User | None = Depends(current_user_optional),
) -> User:
    """로그인된 ``User`` 를 반환한다. 비로그인이면 401 을 발생시킨다.

    이번 task 에서 실제로 사용되는 위치는 ``POST /auth/logout`` 정도로
    제한적이다 (관리자 기능 게이트는 Phase 2+). 다만 인증 필수 경로를 나중에
    쉽게 추가할 수 있도록 미리 제공한다.

    Args:
        user: ``current_user_optional`` 가 반환한 User 또는 None.

    Returns:
        로그인된 ``User``.

    Raises:
        HTTPException(401): 비로그인 상태.
    """
    if user is None:
        # 401 경로. 미들웨어는 access log status=401 로 남기지만, 본 DEBUG
        # 로그는 "왜 401 이 되었는지 (current_user_optional 이 None 반환)"
        # 를 알려준다 — 상위 DEBUG 라인과 함께 grep 하면 원인이 명확해진다.
        logger.debug("current_user_required: 비로그인 → 401")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다.",
        )
    return user


def admin_user_required(
    user: User = Depends(current_user_required),
) -> User:
    """관리자(``is_admin=True``) 사용자를 반환한다. 비관리자면 403.

    Phase 2(00025) 관리자 페이지 라우트가 공통으로 의존한다.
    ``current_user_required`` 를 기반으로 하므로 비로그인은 **먼저** 401 로 걸러지며,
    그 뒤 is_admin 플래그로 403 을 분기한다. 비관리자가 403 응답만 보고 라우트의
    존재 자체는 인지할 수 있다는 점은 의도된 설계(로컬 팀 전용 UI).

    is_admin 부여는 DB 직접 수정(``scripts/create_admin.py`` + Phase 1b SQL)
    으로만 가능하며, UI 에서 부여하는 경로는 Phase 5 범위.

    Args:
        user: ``current_user_required`` 가 통과시킨 로그인 User.

    Returns:
        관리자 권한이 확인된 User.

    Raises:
        HTTPException(403): 로그인했으나 ``is_admin=False``.
    """
    if not user.is_admin:
        # 403 경로. `/admin/*` 는 라우터 수준 dependency 로 이 함수를 공통
        # 주입하므로, admin 페이지에서 403 이 나는 원인이 "비로그인(401 은
        # current_user_required 에서 이미 걸림)" 이 아니라 "비관리자 사용자"
        # 임을 DEBUG 로 명시한다. username 은 민감정보일 수 있으므로 id 만.
        logger.debug(
            "admin_user_required: 비관리자 로그인 → 403 user_id={}",
            user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자만 접근할 수 있습니다.",
        )
    # 관리자 통과 — 원인 추적에 핵심 지점. "/admin/scrape 500" 시 이 로그의
    # 존재 여부로 가드가 정상 통과했는지 확인할 수 있다.
    logger.debug("admin_user_required: 통과 user_id={}", user.id)
    return user


def ensure_same_origin(request: Request) -> None:
    """POST 요청의 Origin/Referer 헤더가 현재 host 와 일치하는지 확인한다.

    CSRF 토큰을 사용하지 않는 대신 외부 사이트가 만든 form submit 만 차단하는
    가벼운 방어선이다. 브라우저가 보내는 Origin 을 우선 확인하고, 없으면
    Referer 로 fallback. 둘 다 없으면 curl/프로그램 요청이 많으므로 통과한다
    (로컬 전제에서 과도한 차단을 피한다).

    Args:
        request: 검증 대상 요청.

    Raises:
        HTTPException(400): Origin/Referer 가 현재 host 와 다를 때.
    """
    expected_host = request.url.netloc
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")

    candidate = origin or referer
    if not candidate:
        # header 가 없는 경우는 비-브라우저 요청으로 간주하고 통과.
        logger.debug("ensure_same_origin: Origin/Referer 없음 → 통과(비-브라우저)")
        return

    # Origin 은 scheme://host[:port], Referer 는 전체 URL 이다. netloc 비교
    # 를 위해 간단히 포함 여부로 본다.
    if f"//{expected_host}" not in candidate:
        logger.debug(
            "ensure_same_origin: host 불일치 → 400 expected_host={!r} candidate={!r}",
            expected_host,
            candidate,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="요청 origin 이 일치하지 않습니다.",
        )


__all__ = [
    "admin_user_required",
    "current_user_optional",
    "current_user_required",
    "ensure_same_origin",
]
