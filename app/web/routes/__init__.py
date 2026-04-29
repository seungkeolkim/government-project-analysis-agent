"""``app.web.routes`` — 기능 영역별로 분리된 FastAPI 라우터 모음.

라우트를 ``create_app()`` 내부 클로저로만 두면 파일이 비대해지고 관리자/사용자/
공개 기능이 한 곳에 섞인다. Phase 2 부터는 기능 단위 모듈로 분리해,
``create_app()`` 에서 ``include_router()`` 호출만 한다.

지금은 ``admin`` 만 분리되어 있으며, 기존 index/detail 라우트는 그대로
``app/web/main.py`` 의 클로저로 유지한다 (회귀 방지).
"""

from __future__ import annotations

from app.web.routes.admin import router as admin_router
from app.web.routes.bulk import router as bulk_router
from app.web.routes.dashboard import router as dashboard_router
from app.web.routes.favorites import router as favorites_router
from app.web.routes.relevance import router as relevance_router

__all__ = [
    "admin_router",
    "bulk_router",
    "dashboard_router",
    "favorites_router",
    "relevance_router",
]
