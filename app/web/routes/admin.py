"""관리자 페이지 라우터 (Phase 2 / 00025-4).

사용자 원문의 '관리자 페이지 (app/web/routes/admin.py)' 섹션을 구현한다.
탭 3개 중 본 subtask 에서는 **[수집 제어]** 만 구현하고, [sources.yaml] 과
[스케줄] 탭은 **탭 링크만** base 템플릿에 둔 채 진입 라우트는 후속 subtask
(00025-5 / 00025-6) 가 추가한다 — placeholder 라우트는 일부러 두지 않는다.

엔드포인트:
    GET  /admin                         → 307 redirect /admin/scrape
    GET  /admin/scrape                  HTML 수집 제어 탭
    GET  /admin/scrape/status           JSON (5초 폴링용)
    POST /admin/scrape/start            수동 수집 시작 → 302 /admin/scrape
    POST /admin/scrape/cancel           중단 요청 → 302 /admin/scrape
    GET  /admin/scrape/runs/{id}/log    text/plain 로그 파일 덤프

보호:
    라우터 레벨 ``dependencies=[Depends(admin_user_required)]`` 로 GET/POST
    전부를 admin-only 로 고정한다. 비로그인 → 401 (current_user_required),
    비관리자 로그인 → 403. POST 는 ``ensure_same_origin`` 도 병용해 가벼운
    CSRF 방어를 건다 (auth 라우트와 동일 패턴).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from loguru import logger

from app.auth.dependencies import admin_user_required, ensure_same_origin
from app.db.models import ScrapeRun, User
from app.db.repository import (
    get_available_source_ids,
    get_running_scrape_run,
    list_recent_scrape_runs,
)
from app.db.session import session_scope
from app.scrape_control import (
    ComposeEnvironmentError,
    ScrapeAlreadyRunningError,
    request_cancel,
    scrape_run_log_path,
    start_scrape_run,
)

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# 최근 이력 기본 개수. UI 폴링 응답에도 동일 값을 사용한다.
RECENT_RUN_LIMIT: int = 20

# 로그 파일 응답 최대 크기 (bytes). 너무 긴 로그가 UI 를 마비시키지 않도록.
# 현재는 파일 전체를 반환하지만 상한을 두어 안전망을 확보한다.
LOG_FILE_MAX_BYTES: int = 1_000_000  # 1MB

# 관리자 템플릿 루트. Phase 1b 인증 템플릿과 같은 디렉터리를 공유한다.
_ADMIN_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"
_templates: Jinja2Templates = Jinja2Templates(directory=str(_ADMIN_TEMPLATES_DIR))


# ──────────────────────────────────────────────────────────────
# 라우터
# ──────────────────────────────────────────────────────────────


# 모든 라우트는 admin-only. dependencies 가 request 단위로 평가되므로
# GET/POST 동일하게 적용된다. 비관리자는 403 (admin_user_required 내부).
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(admin_user_required)],
)


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼 — 직렬화
# ──────────────────────────────────────────────────────────────


def _serialize_scrape_run(run: ScrapeRun) -> dict[str, Any]:
    """ScrapeRun ORM 을 JSON 직렬화 가능한 dict 로 변환한다.

    템플릿 렌더링(HTML) 과 폴링 응답(JSON) 양쪽에서 공통 사용한다.
    민감 정보(예: pid) 는 관리자 전용 화면이므로 포함한다.

    - datetime 은 ISO-8601 문자열.
    - source_counts 는 dict 원본을 그대로 노출 (UI 는 subset 만 사용).
    """
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "status": run.status,
        "trigger": run.trigger,
        "source_counts": dict(run.source_counts or {}),
        "error_message": run.error_message,
        "pid": run.pid,
    }


# ──────────────────────────────────────────────────────────────
# 진입 redirect
# ──────────────────────────────────────────────────────────────


@router.get("/", include_in_schema=False)
def admin_root() -> Response:
    """``/admin`` 진입 시 첫 탭(수집 제어) 으로 redirect 한다.

    관리자 페이지의 default 탭을 명시적으로 '/admin/scrape' 로 둔다. 후속
    subtask 에서 [sources.yaml] / [스케줄] 탭이 추가되어도 default 탭은
    [수집 제어] 유지 — 사용자 원문 탭 순서 첫 번째.
    """
    return RedirectResponse("/admin/scrape", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


# ──────────────────────────────────────────────────────────────
# [수집 제어] 탭 (HTML)
# ──────────────────────────────────────────────────────────────


@router.get("/scrape", response_class=HTMLResponse)
def scrape_control_page(
    request: Request,
    flash: Optional[str] = Query(default=None),
    flash_level: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[수집 제어] 탭 — 현재 상태, 시작/중단 폼, 최근 이력.

    쿼리 파라미터:
        flash:       POST 후 redirect 시 안내 메시지. 없으면 비노출.
        flash_level: 'success' / 'error'. CSS 배지 색 분기용. 기본 'success'.

    템플릿 컨텍스트:
        active_tab:       탭 활성 표시 용 ('scrape').
        running:          현재 running ScrapeRun 의 직렬화 dict 또는 None.
        recent_runs:      최근 ScrapeRun 직렬화 리스트 (최신순).
        available_sources: sources.yaml 에서 읽은 등록 소스 id 목록.
        flash / flash_level: 상단 안내 배지.
        current_user:     상단 네비 + is_admin 조건 분기용.
    """
    with session_scope() as session:
        running_row = get_running_scrape_run(session)
        recent_rows = list_recent_scrape_runs(session, limit=RECENT_RUN_LIMIT)
        running_payload = _serialize_scrape_run(running_row) if running_row else None
        recent_payload = [_serialize_scrape_run(row) for row in recent_rows]

    # sources.yaml 기반 등록 소스 목록. 변경 시 자동 반영 (session 과 무관).
    available_sources = get_available_source_ids()

    return _templates.TemplateResponse(
        request,
        "admin/control.html",
        {
            "active_tab": "scrape",
            "running": running_payload,
            "recent_runs": recent_payload,
            "available_sources": available_sources,
            "flash": flash,
            "flash_level": flash_level or "success",
            "current_user": current_user,
        },
    )


# ──────────────────────────────────────────────────────────────
# [수집 제어] 탭 — 5초 폴링용 JSON 상태
# ──────────────────────────────────────────────────────────────


@router.get("/scrape/status", response_class=JSONResponse)
def scrape_status() -> JSONResponse:
    """5초 폴링으로 현재 running 상태 + 최근 이력을 갱신해서 돌려준다.

    사용자 원문 '5초 폴링으로 상태 갱신 (SSE 과함)' 에 따라 JSON 엔드포인트만
    제공한다. 응답 스키마:

    ```
    {
      \"running\": { id, started_at, status, trigger, source_counts, pid, ... } | null,
      \"recent\":  [ { id, started_at, ended_at, status, trigger, source_counts, ... }, ... ],
      \"poll_interval_ms\": 5000
    }
    ```

    guidance: stdout 마지막 N 줄 포함은 본 subtask 범위 밖 — source_counts /
    started_at / status 만 노출한다.
    """
    with session_scope() as session:
        running_row = get_running_scrape_run(session)
        recent_rows = list_recent_scrape_runs(session, limit=RECENT_RUN_LIMIT)
        running_payload = _serialize_scrape_run(running_row) if running_row else None
        recent_payload = [_serialize_scrape_run(row) for row in recent_rows]

    return JSONResponse(
        {
            "running": running_payload,
            "recent": recent_payload,
            "poll_interval_ms": 5000,
        }
    )


# ──────────────────────────────────────────────────────────────
# [수집 제어] 탭 — 수동 시작 / 중단
# ──────────────────────────────────────────────────────────────


def _parse_active_sources_form(
    raw_values: list[str],
    available: list[str],
) -> list[str]:
    """폼으로 들어온 ``active_sources`` 체크박스 값을 검증·정규화한다.

    체크박스 미선택 → 빈 리스트 (전체 실행 default 의미). 모르는 소스 id 는
    400 을 던지기 전 로그에 남기고 제거한다 — 악의적 입력이 아니라 sources.yaml
    이 그 사이에 바뀌어 체크박스와 동기화가 깨진 경우를 상상할 수 있다. 안전을
    위해 **알 수 없는 id 는 거부** 로 가는 방향이 낫다 — HTTPException(400).
    """
    cleaned: list[str] = []
    for value in raw_values:
        trimmed = value.strip()
        if not trimmed:
            continue
        if trimmed not in available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"알 수 없는 source id: {trimmed!r}. "
                       f"허용: {', '.join(available)}",
            )
        if trimmed not in cleaned:
            cleaned.append(trimmed)
    return cleaned


@router.post("/scrape/start", dependencies=[Depends(ensure_same_origin)])
def scrape_start(
    request: Request,
    active_sources: list[str] = Form(default_factory=list),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """'지금 시작' 버튼 제출 처리.

    Form:
        active_sources: 체크박스 name="active_sources" multi 값.
                        빈 리스트(전부 미선택) = sources.yaml enabled 전체.

    흐름:
        1. active_sources 검증 (available 내에 있는지).
        2. scrape_control.start_scrape_run(active_sources, trigger='manual').
        3. ScrapeAlreadyRunningError → flash_level=error 로 302 /admin/scrape.
        4. ComposeEnvironmentError → 운영자가 .env 를 고쳐야 함 → 동일하게 302
           + 에러 flash.
        5. 그 외 성공 → flash_level=success 로 302 /admin/scrape.
    """
    available = get_available_source_ids()
    normalized = _parse_active_sources_form(active_sources, available)

    try:
        result = start_scrape_run(normalized, trigger="manual")
    except ScrapeAlreadyRunningError as exc:
        logger.info(
            "관리자 '{}' 의 수동 수집 시작 거부 — 이미 running: {}",
            current_user.username, exc,
        )
        return RedirectResponse(
            url=_flash_url("이미 다른 수집이 진행 중입니다.", level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except ComposeEnvironmentError as exc:
        # HOST_PROJECT_DIR 등 .env 설정 누락. 운영자에게 원문 메시지를 그대로 노출해
        # 무엇을 고쳐야 하는지 알려준다.
        logger.error(
            "수동 수집 실행 환경 오류(관리자 '{}'): {}",
            current_user.username, exc,
        )
        return RedirectResponse(
            url=_flash_url(str(exc), level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    logger.info(
        "관리자 '{}' 의 수동 수집 시작: scrape_run_id={} pid={} active_sources={}",
        current_user.username,
        result.scrape_run_id,
        result.pid,
        normalized or "(전체)",
    )
    message = (
        f"수집을 시작했습니다 (scrape_run_id={result.scrape_run_id}, "
        f"pid={result.pid})."
    )
    return RedirectResponse(
        url=_flash_url(message, level="success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/scrape/cancel", dependencies=[Depends(ensure_same_origin)])
def scrape_cancel(
    request: Request,
    scrape_run_id: int = Form(...),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """'중단' 버튼 제출 처리. SIGTERM 을 전파한다.

    중단 결과는 비동기적이다(subprocess 가 현재 공고 마무리 후 종료).
    즉시 반환되는 flash 는 'SIGTERM 전송 성공 여부' 만 알려 준다.
    """
    sent = request_cancel(scrape_run_id)
    if sent:
        logger.info(
            "관리자 '{}' 의 중단 요청 전송 완료: scrape_run_id={}",
            current_user.username, scrape_run_id,
        )
        return RedirectResponse(
            url=_flash_url(
                "중단 요청을 보냈습니다. 현재 공고 마무리 후 정지됩니다.",
                level="success",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    logger.info(
        "관리자 '{}' 의 중단 요청 거부/무시: scrape_run_id={}",
        current_user.username, scrape_run_id,
    )
    return RedirectResponse(
        url=_flash_url(
            "중단 대상이 없거나 이미 종료됐습니다.",
            level="error",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _flash_url(message: str, *, level: str) -> str:
    """/admin/scrape?flash=...&flash_level=... 형태의 redirect URL 을 만든다.

    FastAPI/Starlette 의 ``URL`` 객체를 쓰면 자동 인코딩이 되지만, 여기서는
    메시지가 길지 않아 단순 str concat + starlette.datastructures.URL 을 통한
    query 추가 대신 간단히 ``from urllib.parse`` 로 처리한다.
    """
    from urllib.parse import urlencode

    query = urlencode({"flash": message, "flash_level": level})
    return f"/admin/scrape?{query}"


# ──────────────────────────────────────────────────────────────
# [수집 제어] 탭 — 로그 파일 덤프
# ──────────────────────────────────────────────────────────────


@router.get("/scrape/runs/{run_id}/log", response_class=PlainTextResponse)
def scrape_run_log(run_id: int) -> Response:
    """지정 ScrapeRun 의 subprocess 로그 파일(/app/data/logs/scrape_runs/{id}.log)
    을 text/plain 으로 반환한다.

    - 파일이 없으면 404 (아직 기동되지 않았거나 CLI 로 실행된 run).
    - 상한(LOG_FILE_MAX_BYTES) 를 넘으면 마지막 N 바이트만 잘라서 반환한다.

    guidance 에 따라 본 subtask 에서는 로그 '꼬리 자르기' 뷰를 UI 에 직접 심지는
    않지만, 관리자가 URL 을 열어 직접 확인할 수 있도록 최소 엔드포인트를 둔다.
    """
    log_path = scrape_run_log_path(run_id)
    if not log_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"로그 파일을 찾을 수 없습니다: run_id={run_id}",
        )

    try:
        file_size = log_path.stat().st_size
        with log_path.open("rb") as handle:
            if file_size > LOG_FILE_MAX_BYTES:
                # 마지막 N 바이트만 — 앞쪽은 잘린다는 경고 주석을 붙인다.
                handle.seek(file_size - LOG_FILE_MAX_BYTES)
                truncated_prefix = (
                    f"[로그 앞부분 {file_size - LOG_FILE_MAX_BYTES} 바이트 생략]\n"
                )
                body_bytes = truncated_prefix.encode("utf-8") + handle.read()
            else:
                body_bytes = handle.read()
    except OSError as exc:
        logger.exception(
            "로그 파일 읽기 실패: run_id={} path={} ({}: {})",
            run_id, log_path, type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="로그 파일을 읽을 수 없습니다.",
        ) from exc

    return PlainTextResponse(
        content=body_bytes.decode("utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )


__all__ = [
    "LOG_FILE_MAX_BYTES",
    "RECENT_RUN_LIMIT",
    "router",
]
