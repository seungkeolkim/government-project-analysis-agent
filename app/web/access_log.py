"""IP 접근 이력 기록 미들웨어 (task 00073-1).

모든 HTTP 요청에서 ip/시각/경로/method/user_agent/user_id/status_code 를 추출해
KST 날짜별 access_history_YYMMDD.log 파일에 JSON-lines 형식으로 append 기록한다.

## 수집 항목
- ip_address:   접근 IP (request.client.host, 없으면 "-")
- accessed_at:  접근 시각 KST ISO 8601 문자열
- path:         요청 URL 경로
- method:       HTTP 메서드 (GET/POST 등)
- user_agent:   User-Agent 헤더 (없으면 "-")
- user_id:      로그인 세션의 사용자 DB ID (비로그인·조회 실패 시 "-")
- status_code:  응답 HTTP 상태 코드

## 파일 위치
- {settings.access_log_dir}/access_history_YYMMDD.log
- 파일명 예시: access_history_260506.log
- 날짜 기준은 KST. 자정이 지나면 자동으로 새 파일에 기록된다.
- 환경변수 ACCESS_LOG_DIR 로 경로 조정 가능.

## skip 대상 (로그 기록 제외)
- /static/ 으로 시작하는 경로 (정적 파일)
- /admin/scrape/status (폴링성 상태 확인)
- /favicon.ico, /health, /healthz (정확 일치)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import Response
from loguru import logger

from app.auth.constants import SESSION_COOKIE_NAME
from app.auth.service import get_active_session
from app.db.session import SessionLocal
from app.timezone import now_utc, to_kst


# ──────────────────────────────────────────────────────────────
# skip 패턴
# ──────────────────────────────────────────────────────────────

# 정확 일치로 건너뛸 경로
_SKIP_EXACT: frozenset[str] = frozenset({
    "/admin/scrape/status",
    "/favicon.ico",
    "/health",
    "/healthz",
})

# startswith 로 건너뛸 경로 prefix
_SKIP_PREFIXES: tuple[str, ...] = ("/static/",)


def _should_skip_path(path: str) -> bool:
    """로그 기록을 건너뛸 경로인지 판정한다."""
    if path in _SKIP_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


# ──────────────────────────────────────────────────────────────
# 헬퍼 함수
# ──────────────────────────────────────────────────────────────


def _client_ip_of(request: Request) -> str:
    """요청 객체에서 클라이언트 IP 를 안전하게 추출한다.

    observability.py 의 _client_host_of 와 동일한 None 가드("-") 컨벤션을 따른다.
    """
    if request.client is None:
        return "-"
    return request.client.host or "-"


def _extract_user_id_from_request(request: Request) -> str:
    """세션 쿠키에서 로그인 사용자 DB ID 를 추출한다.

    비로그인이거나 세션 조회 실패 시 "-" 를 반환한다.
    SessionLocal 을 직접 열고 닫아 요청의 DB 세션과 독립적으로 동작한다.
    예외를 모두 흡수해 추출 실패가 요청 처리에 영향을 주지 않는다.

    Returns:
        사용자 DB ID 문자열, 또는 "-".
    """
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return "-"

    db_session = SessionLocal()
    try:
        user_session = get_active_session(db_session, cookie_value)
        if user_session is None:
            return "-"
        return str(user_session.user_id)
    except Exception as exc:
        logger.debug("접근 이력 user_id 추출 실패: {}: {}", type(exc).__name__, exc)
        return "-"
    finally:
        db_session.close()


def _get_log_file_path(log_dir: Path) -> Path:
    """KST 오늘 날짜 기준 로그 파일 경로를 반환한다.

    파일명 형식: access_history_YYMMDD.log
    자정(KST)이 지나면 자동으로 새 파일 경로를 반환한다.

    Args:
        log_dir: 로그 파일을 저장할 디렉터리.

    Returns:
        오늘 날짜에 해당하는 로그 파일 Path.
    """
    kst_now = to_kst(now_utc())
    # kst_now 는 to_kst 에서 None 입력에만 None 이 되므로, now_utc() 결과로는 항상 datetime.
    assert kst_now is not None
    date_str = kst_now.strftime("%y%m%d")
    return log_dir / f"access_history_{date_str}.log"


def _append_log_entry(entry: dict, log_dir: Path) -> None:
    """JSON-lines 형식으로 로그 파일 끝에 한 줄 추가한다.

    파일이 없으면 새로 생성된다. 한글 user_agent 가 \\uXXXX 로 escape 되지 않도록
    ensure_ascii=False 를 사용한다.

    Args:
        entry:   기록할 필드 딕셔너리.
        log_dir: 로그 파일 저장 디렉터리.
    """
    log_file = _get_log_file_path(log_dir)
    line = json.dumps(entry, ensure_ascii=False)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ──────────────────────────────────────────────────────────────
# 미들웨어 등록
# ──────────────────────────────────────────────────────────────


def install_access_history_middleware(app: FastAPI) -> None:
    """FastAPI 앱에 접근 이력 기록 미들웨어를 등록한다.

    observability.py 와 동일한 @app.middleware("http") 데코레이터 패턴을 사용한다.
    응답 직후(call_next 반환 후) 로그를 기록해 status_code 를 안정적으로 읽는다.
    기록 실패는 WARNING 으로 남기고 응답 자체는 그대로 반환한다.

    등록 순서 주의:
        main.py 에서 install_request_logging_middleware 보다 나중에 호출해야
        Starlette 미들웨어 스택에서 access_history 가 바깥쪽(먼저 실행)이 된다.

    Args:
        app: 미들웨어를 장착할 FastAPI 인스턴스.
    """
    # lru_cache 싱글턴을 매 요청마다 재활용한다. 미들웨어 정의 시점(앱 기동 시)에
    # import 하면 create_app 내 ensure_runtime_paths 호출 전에 Settings 가 만들어질
    # 수 있어 지연 import 로 처리한다.
    from app.config import get_settings

    @app.middleware("http")
    async def _access_history_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """요청 완료 후 접근 이력을 KST 날짜별 로그파일에 기록한다."""
        path = request.url.path

        # 정적 파일·헬스체크·폴링 경로는 기록 제외 — 로그 노이즈 감소
        if _should_skip_path(path):
            return await call_next(request)

        response = await call_next(request)

        # call_next 가 반환된 뒤 status_code 가 확정된 시점에 기록한다.
        try:
            kst_now = to_kst(now_utc())
            assert kst_now is not None
            entry = {
                "ip_address": _client_ip_of(request),
                "accessed_at": kst_now.isoformat(),
                "path": path,
                "method": request.method,
                "user_agent": request.headers.get("user-agent", "-") or "-",
                "user_id": _extract_user_id_from_request(request),
                "status_code": response.status_code,
            }
            _append_log_entry(entry, get_settings().access_log_dir)
        except Exception as exc:
            # 기록 실패가 실제 응답을 막으면 안 된다.
            logger.warning(
                "접근 이력 로그 기록 실패: {}: {}",
                type(exc).__name__,
                exc,
            )

        return response


__all__ = [
    "install_access_history_middleware",
]
