"""경량 health 체크 라우터 (task 00161).

[시스템 재시작] 서브탭의 프론트엔드가 '재시작 중…' 동안 폴링해 **새 인스턴스가
떠서 요청을 받는지** 만 확인하기 위한 인증 불필요·DB 의존 없는 GET endpoint 다.

경로는 ``/healthz`` — ``app/web/access_log.py`` 의 비로깅 skip 목록(`/health`,
`/healthz`)에 이미 등재되어 있어, 폴링 트래픽이 접근 이력 로그를 오염시키지
않는다. admin 라우터 밖(무인증)에 두어 비로그인 상태에서도, 그리고 세션 쿠키가
없는 재시작 직후 폴링에서도 즉시 200 을 받을 수 있게 한다.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

# prefix/auth 없는 단순 라우터. main.py 가 include_router 로 mount 한다.
router = APIRouter(tags=["health"])


@router.get("/healthz", response_class=JSONResponse)
def healthz() -> JSONResponse:
    """프로세스 생존 신호용 경량 health 체크.

    DB 조회나 인증 없이 즉시 가벼운 JSON 을 반환한다. '프로세스가 떠서 요청을
    받는다' 는 사실만 신호하면 충분하므로, 무거운 의존성을 일절 두지 않는다.

    Returns:
        ``{\"status\": \"ok\"}`` 200 응답.
    """
    return JSONResponse({"status": "ok"})


__all__ = [
    "router",
]
