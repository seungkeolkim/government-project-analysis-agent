"""HTTP 요청/응답 access log 미들웨어와 전역 미처리 예외 핸들러.

00030-2 — 사용자 원문의 "원인 찾기 쉽게 전체 로그를 강화해줘" 요구를 충족하기
위해 두 가지 safety-net 을 FastAPI 에 부착한다.

(1) :func:`install_request_logging_middleware`
    모든 요청에 대해 ``method / path / client_ip / status_code / duration_ms`` 를
    loguru 로 한 줄 access log 로 남긴다. 요청별 ``request_id`` 를 생성해
    ``logger.contextualize(...)`` 로 컨텍스트에 넣기 때문에, 이 요청 동안
    호출된 도메인 코드의 ``logger.info/debug`` 에도 동일한 ``req=...`` 필드가
    자동 노출된다 (``app.logging_setup`` 의 포맷 ``{extra[request_id]}`` 참조).

(2) :func:`install_unhandled_exception_handler`
    라우트에서 발생한 미처리 예외(Exception) 를 loguru 로 stack trace 와 함께
    ERROR 레벨로 기록하고 500 JSON 응답을 돌려준다. ``HTTPException`` 이나
    ``RequestValidationError`` 처럼 FastAPI 가 자체 핸들러로 먼저 처리하는
    예외는 여기 도달하지 않으므로 4xx 상황에서 불필요한 stack trace 를
    유발하지 않는다.

주의 — 미들웨어와 예외 핸들러가 동일 예외를 둘 다 잡지 않도록 설계했다.
미들웨어는 ``try/except`` 없이 ``call_next`` 를 호출하고, 예외 로깅은 전적으로
예외 핸들러에 맡긴다. 이렇게 해야 stack trace 가 중복으로 찍히지 않는다.

한계 — 라우트 핸들러가 ``BackgroundTask`` 를 반환해 응답 후 비동기로 실행되는
작업 안에서 예외가 발생하면, 미들웨어/예외 핸들러 모두 잡을 수 없다 (응답
이 이미 나간 뒤 실행되기 때문). 그런 경로에 대한 로깅이 필요해지면 ASGI
레벨 미들웨어 또는 BackgroundTask 내부 try/except 가 필요하다 — 본 task 범위
밖.
"""

from __future__ import annotations

import time
import uuid
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger


def _short_request_id() -> str:
    """요청별 12자 16진 ID 를 생성한다.

    uuid4 의 앞 12자리를 사용해 로그 한 줄에 섞여도 눈에 띄는 길이로 맞춘다.
    완전한 uniqueness 가 필요한 추적 시스템이 아니므로 충돌 확률은 무시한다
    (수만 건/일 수준의 로컬 운영 환경 가정).
    """
    return uuid.uuid4().hex[:12]


def _client_host_of(request: Request) -> str:
    """요청 객체에서 클라이언트 IP 를 안전하게 추출한다.

    ``request.client`` 는 테스트·일부 ASGI 서버에서 ``None`` 일 수 있어 가드한다.
    """
    if request.client is None:
        return "-"
    return request.client.host or "-"


def install_request_logging_middleware(app: FastAPI) -> None:
    """FastAPI 앱에 HTTP access log 미들웨어를 등록한다.

    등록 내용:
        - 요청 진입 시 DEBUG 로 "request 진입" 한 줄 기록 (DEBUG 레벨일 때만
          출력되므로 운영 기본 INFO 에서는 가려짐).
        - 응답 직전 access log 한 줄 기록 — 레벨은 상태 코드에 따라 자동 선택:
          500 이상 → WARNING, 그 외 → INFO. 상태 코드가 바로 눈에 띄도록 하기
          위함이다.
        - 요청 전체 구간을 ``logger.contextualize(request_id=...)`` 로 감싸,
          라우트/의존성/서비스 계층에서 찍는 모든 loguru 로그에 동일한 ``req=...``
          필드가 따라오게 한다.

    Args:
        app: 미들웨어를 장착할 FastAPI 인스턴스.
    """

    @app.middleware("http")
    async def _access_log_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """요청 한 건의 진입→완료 구간을 로그로 감싼다."""
        request_id = _short_request_id()
        method = request.method
        path = request.url.path
        client_host = _client_host_of(request)

        # 요청 전체를 contextualize 블록으로 감싸 nested 로그에 request_id 가
        # 자동 노출되게 한다. Starlette 의 ``BaseHTTPMiddleware`` 특성상
        # ``call_next`` 가 예외를 다시 던지는 경우가 있어(등록된 exception
        # handler 가 응답을 만들더라도 middleware 레벨로 예외가 전파됨),
        # access log 의 duration 을 **모든** 경로에서 보장하려면 try/finally
        # 가 필요하다. 다만 여기서는 예외 메시지를 스스로 로깅하지 않고
        # "request 실패" 라는 한 줄로만 끝낸다 — stack trace 는
        # :func:`install_unhandled_exception_handler` 가 전담해 중복 로깅을
        # 방지한다 (guidance: \"미들웨어와 exception_handler 가 같은 예외를
        # 두 번 로깅하지 않도록\").
        with logger.contextualize(request_id=request_id):
            start_time = time.perf_counter()
            logger.debug(
                "request 진입: method={} path={} client={}",
                method,
                path,
                client_host,
            )

            try:
                response = await call_next(request)
            except Exception:
                # exception_handler 가 stack trace 와 500 응답을 만들 것이므로
                # 여기서는 access 라인만 한 줄 남기고 재전파한다. status 는
                # 실제 클라이언트로 나갈 500 과 일치하게 고정한다.
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                logger.warning(
                    "request 실패(예외 전파): method={} path={} status=500 "
                    "duration_ms={:.1f} client={} (stack trace 는 미처리 예외 "
                    "핸들러 로그 참고)",
                    method,
                    path,
                    duration_ms,
                    client_host,
                )
                raise

            duration_ms = (time.perf_counter() - start_time) * 1000.0
            status_code = response.status_code

            # 500 이상은 비정상, 그 외(4xx 포함) 는 INFO 로 기록.
            # 4xx 는 사용자 입력 오류가 대부분이므로 운영자 주의까지 끌 필요가
            # 없어 INFO 로 두되, status 필드 자체가 눈에 띄므로 대시보드에서도
            # 집계 가능하다.
            log_fn = logger.warning if status_code >= 500 else logger.info
            log_fn(
                "request 완료: method={} path={} status={} duration_ms={:.1f} client={}",
                method,
                path,
                status_code,
                duration_ms,
                client_host,
            )

        return response


def install_unhandled_exception_handler(app: FastAPI) -> None:
    """FastAPI 앱에 최종 fallback Exception 핸들러를 등록한다.

    동작:
        - FastAPI 의 기본 ``HTTPException`` / ``RequestValidationError`` 핸들러가
          먼저 실행되므로, 본 핸들러는 "정말 예상치 못한" 예외만 받는다.
        - 받은 예외는 ``logger.opt(exception=exc).error(...)`` 로 stack trace 와
          함께 기록한다. 이 단계가 stdlib logging 을 경유하지 않기 때문에
          loguru 의 backtrace/diagnose 옵션이 그대로 적용된다
          (``app.logging_setup`` 의 ``backtrace=True`` + 조건부 ``diagnose``).
        - 응답은 500 JSON 으로 통일한다. 현재 앱은 HTML 페이지도 있지만, 미처리
          예외가 던져진 시점에 안전하게 렌더할 컨텍스트가 없을 수 있어 단순
          ``{\"detail\": ...}`` 로 고정한다. 사용자 원문의 주된 목표는 docker
          logs 에 원인이 남는 것이므로 응답 포맷은 부차적이다.

    Args:
        app: 핸들러를 장착할 FastAPI 인스턴스.
    """

    @app.exception_handler(Exception)
    async def _log_unhandled_exception(request: Request, exc: Exception) -> Response:
        """미들웨어가 통과시킨 미처리 예외를 loguru 로 stack trace 포함 기록."""
        method = request.method
        path = request.url.path
        client_host = _client_host_of(request)

        # logger.opt(exception=exc) 를 사용하면 loguru 가 traceback 을 포함해
        # 포맷을 뽑는다. ERROR 레벨로 기록해 운영자 주의를 끌고, 500 응답은
        # 별도로 돌려준다. request_id 는 contextualize 블록 안에서 호출될 때만
        # 포함되므로, 미들웨어가 바깥에서 감싼 상태여야 한다 — 본 프로젝트의
        # 등록 순서는 ``install_request_logging_middleware`` → 라우트 → 예외
        # 전파 → 예외 핸들러 순이라 컨텍스트는 유지된다.
        logger.opt(exception=exc).error(
            "미처리 예외 발생: method={} path={} client={} exc_type={}: {}",
            method,
            path,
            client_host,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error"},
        )


__all__ = [
    "install_request_logging_middleware",
    "install_unhandled_exception_handler",
]
