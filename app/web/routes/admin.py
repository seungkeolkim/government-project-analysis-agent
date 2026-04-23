"""관리자 페이지 라우터 (Phase 2 / 00025-4, 00025-5, 00025-6).

사용자 원문의 '관리자 페이지 (app/web/routes/admin.py)' 섹션을 구현한다.
탭 3개 전부 구현:
    - [수집 제어]:   00025-4 — 수동 시작/중단/이력/5초 폴링.
    - [sources.yaml]: 00025-5 — textarea 편집 + Pydantic 검증 + 백업.
    - [스케줄]:      00025-6 (본 subtask) — cron/매N시간 등록/토글/삭제.

엔드포인트:
    GET  /admin                              → 307 redirect /admin/scrape
    GET  /admin/scrape                       HTML 수집 제어 탭
    GET  /admin/scrape/status                JSON (5초 폴링용)
    POST /admin/scrape/start                 수동 수집 시작 → 303 /admin/scrape
    POST /admin/scrape/cancel                중단 요청 → 303 /admin/scrape
    GET  /admin/scrape/runs/{id}/log         text/plain 로그 파일 덤프
    GET  /admin/sources/yaml                 HTML sources.yaml 편집기 탭
    POST /admin/sources/yaml                 YAML 검증·백업·저장 후 재렌더
    GET  /admin/schedule                     HTML 스케줄 탭
    POST /admin/schedule                     cron/interval 스케줄 등록
    POST /admin/schedule/{job_id}/toggle     활성/비활성 토글
    POST /admin/schedule/{job_id}/delete     스케줄 삭제

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
from app.scheduler import (
    ScheduleValidationError,
    add_cron_schedule,
    add_interval_schedule,
    delete_schedule,
    is_scheduler_running,
    list_schedules,
    toggle_schedule,
)
from app.scrape_control import (
    ComposeEnvironmentError,
    ScrapeAlreadyRunningError,
    request_cancel,
    scrape_run_log_path,
    start_scrape_run,
)
from app.sources.yaml_editor import (
    SourcesYamlValidationError,
    load_sources_yaml_text,
    save_sources_yaml_text,
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


# ──────────────────────────────────────────────────────────────
# [sources.yaml] 탭 (Phase 2 / 00025-5)
# ──────────────────────────────────────────────────────────────


def _render_sources_yaml_page(
    request: Request,
    *,
    current_user: User,
    yaml_text: str,
    flash: Optional[str],
    flash_level: str,
    error_details: Optional[list[str]] = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """[sources.yaml] 탭 템플릿을 렌더하는 공통 헬퍼.

    GET 진입, POST 성공 후 재렌더, POST 실패(textarea 값 보존) 모두 이 함수를
    통과해 코드 중복을 없앤다. error_details 가 주어지면 템플릿이 에러 블록을
    표시한다.
    """
    return _templates.TemplateResponse(
        request,
        "admin/sources_yaml.html",
        {
            "active_tab": "sources",
            "yaml_text": yaml_text,
            "flash": flash,
            "flash_level": flash_level,
            "error_details": error_details or [],
            "current_user": current_user,
        },
        status_code=status_code,
    )


@router.get("/sources/yaml", response_class=HTMLResponse)
def sources_yaml_page(
    request: Request,
    flash: Optional[str] = Query(default=None),
    flash_level: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """sources.yaml 편집 폼을 렌더한다.

    호스트 바인드 마운트 원본(컨테이너 기준 /run/config/sources.yaml) 을 그대로
    읽어 textarea 에 채운다. 파일이 없으면 빈 textarea 로 진입한다 — 편집 후
    저장하면 신규 생성된다.

    파일 읽기 중 OSError 가 나면 편집 UI 를 띄우되 에러 배지를 통해 상황을
    알린다. 빈 textarea 가 아니라 500 을 띄우면 관리자가 yaml 내용을 잃을 수
    있어, 복구 가능성을 남기는 편이 낫다.
    """
    try:
        yaml_text = load_sources_yaml_text()
        load_error: Optional[str] = None
    except OSError as exc:
        logger.exception(
            "sources.yaml 로드 실패: ({}: {})", type(exc).__name__, exc,
        )
        yaml_text = ""
        load_error = (
            f"sources.yaml 읽기 중 OS 오류가 발생했습니다: "
            f"{type(exc).__name__}: {exc}"
        )

    # 쿼리스트링 flash 가 있으면 그걸 우선, 없으면 load_error 노출.
    effective_flash = flash if flash else load_error
    effective_level = flash_level if flash_level else ("error" if load_error else "success")

    return _render_sources_yaml_page(
        request,
        current_user=current_user,
        yaml_text=yaml_text,
        flash=effective_flash,
        flash_level=effective_level,
        error_details=None,
    )


@router.post("/sources/yaml", dependencies=[Depends(ensure_same_origin)])
def sources_yaml_save(
    request: Request,
    yaml_text: str = Form(...),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """sources.yaml 저장 처리.

    검증 실패:
        - YAML syntax 오류 / Pydantic 검증 실패 — 파일 변경 없음(사용자 원문:
          '실패 시 에러 + 저장 안 함'). 원문을 그대로 textarea 에 돌려주고
          (guidance: '변경사항을 잃지 않도록') error_details 를 표시한다.

    검증 성공:
        - 원본 파일을 data/backups/sources/YYYYMMDD_HHMMSS.yaml 로 백업한 뒤
          원자적 쓰기. 성공 flash 로 GET 리다이렉트 (PRG 패턴).

    OS 에러(백업/쓰기 실패):
        - 500 계열 에러로 간주해 textarea 값을 유지한 채 에러 메시지 표시.
    """
    # textarea 가 submit 시 개행 코드 정규화(기본 CRLF → LF). Python 기본
    # request form 처리가 이미 값을 str 로 주지만, 안전하게 한 번 더 정규화.
    submitted_text = yaml_text.replace("\r\n", "\n").replace("\r", "\n")

    try:
        result = save_sources_yaml_text(submitted_text)
    except SourcesYamlValidationError as exc:
        # 검증 실패 — textarea 원문을 그대로 돌려주고 error_details 노출.
        # PRG 가 아닌 동일 url 에 200 재렌더 — 새로고침 시 form 재전송 경고가
        # 뜰 수 있지만, 에러 맥락을 유지하려면 이게 더 안전.
        logger.info(
            "sources.yaml 저장 거부 — 검증 실패: 관리자={} message={!r} details={}",
            current_user.username, exc.message, exc.details,
        )
        return _render_sources_yaml_page(
            request,
            current_user=current_user,
            yaml_text=submitted_text,
            flash=exc.message,
            flash_level="error",
            error_details=exc.details,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except OSError as exc:
        logger.exception(
            "sources.yaml 저장 중 OS 오류: 관리자={} ({}: {})",
            current_user.username, type(exc).__name__, exc,
        )
        return _render_sources_yaml_page(
            request,
            current_user=current_user,
            yaml_text=submitted_text,
            flash=(
                f"저장 중 OS 오류가 발생했습니다 ({type(exc).__name__}): {exc}. "
                "파일 권한 또는 디스크 상태를 확인하세요."
            ),
            flash_level="error",
            error_details=None,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # 성공 — PRG 패턴(POST-Redirect-GET).
    backup_note = (
        f" 백업: {result.backup_path.name}" if result.backup_path is not None else " (기존 원본 없음 — 백업 생략)"
    )
    message = f"저장 완료 ({result.byte_count} bytes).{backup_note}"
    logger.info(
        "sources.yaml 저장 완료: 관리자={} target={} bytes={} backup={}",
        current_user.username,
        result.target_path,
        result.byte_count,
        result.backup_path,
    )
    return RedirectResponse(
        url=_sources_yaml_flash_url(message, level="success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _sources_yaml_flash_url(message: str, *, level: str) -> str:
    """/admin/sources/yaml?flash=...&flash_level=... redirect URL 빌더."""
    from urllib.parse import urlencode

    query = urlencode({"flash": message, "flash_level": level})
    return f"/admin/sources/yaml?{query}"


# ──────────────────────────────────────────────────────────────
# [스케줄] 탭 (Phase 2 / 00025-6)
# ──────────────────────────────────────────────────────────────


def _schedule_flash_url(message: str, *, level: str) -> str:
    """/admin/schedule?flash=...&flash_level=... redirect URL 빌더."""
    from urllib.parse import urlencode

    query = urlencode({"flash": message, "flash_level": level})
    return f"/admin/schedule?{query}"


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(
    request: Request,
    flash: Optional[str] = Query(default=None),
    flash_level: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[스케줄] 탭 — 등록된 스케줄 목록 + 신규 등록 폼.

    템플릿 컨텍스트:
        active_tab:         'schedule'.
        schedules:          ScheduleSummary 리스트 (next_run_time 오름차순).
        available_sources:  시작 폼에서 고를 수 있는 source id 목록.
        scheduler_running:  APScheduler 가 기동되어 있는지.
        flash / flash_level: 상단 안내 배지.
        current_user:       네비 + is_admin 분기용.
    """
    try:
        schedules = list_schedules()
    except Exception as exc:
        # jobstore 접근 실패 등 예외. UI 는 빈 목록으로 폴백하되 에러를 flash 로 노출.
        logger.exception(
            "스케줄 목록 조회 실패: {}: {}", type(exc).__name__, exc,
        )
        schedules = []
        if flash is None:
            flash = f"스케줄 목록 조회 실패: {exc}"
            flash_level = "error"

    return _templates.TemplateResponse(
        request,
        "admin/schedule.html",
        {
            "active_tab": "schedule",
            "schedules": schedules,
            "available_sources": get_available_source_ids(),
            "scheduler_running": is_scheduler_running(),
            "flash": flash,
            "flash_level": flash_level or "success",
            "current_user": current_user,
        },
    )


def _parse_schedule_active_sources(
    raw_values: list[str],
    available: list[str],
) -> list[str]:
    """schedule 등록 폼의 active_sources 체크박스 검증.

    [수집 제어] 탭의 _parse_active_sources_form 과 같은 정책 — 알 수 없는 id 는
    거부 (sources.yaml 과 동기화 깨짐을 알리는 쪽이 안전).
    """
    cleaned: list[str] = []
    for value in raw_values:
        trimmed = value.strip()
        if not trimmed:
            continue
        if trimmed not in available:
            raise ScheduleValidationError(
                f"알 수 없는 source id: {trimmed!r}. 허용: {', '.join(available)}"
            )
        if trimmed not in cleaned:
            cleaned.append(trimmed)
    return cleaned


@router.post("/schedule", dependencies=[Depends(ensure_same_origin)])
def schedule_add(
    request: Request,
    trigger_type: str = Form(...),
    cron_expression: Optional[str] = Form(default=None),
    interval_hours: Optional[int] = Form(default=None),
    active_sources: list[str] = Form(default_factory=list),
    enabled: Optional[str] = Form(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """새 스케줄을 등록한다.

    Form 필드:
        trigger_type:    'cron' 또는 'interval' — 입력 분기.
        cron_expression: trigger_type='cron' 일 때 사용. 5-필드 cron.
        interval_hours:  trigger_type='interval' 일 때 사용. 양의 정수.
        active_sources:  체크박스 multi 값. 비어 있으면 전체.
        enabled:         체크박스 'on' 또는 누락. 기본은 등록 즉시 활성.

    흐름 & 예외:
        trigger_type 값이 유효하지 않거나 필드 누락이면 ScheduleValidationError
        로 flash error redirect. add_*_schedule 이 raise 하는 동일 예외도 같은
        경로로 처리. 성공 시 PRG 패턴(303) 로 /admin/schedule.
    """
    trigger_type_normalized = trigger_type.strip().lower()
    should_enable = bool(enabled) and enabled != "off"

    available = get_available_source_ids()
    try:
        normalized_sources = _parse_schedule_active_sources(active_sources, available)

        if trigger_type_normalized == "cron":
            if not cron_expression:
                raise ScheduleValidationError(
                    "cron 표현식이 누락되었습니다."
                )
            summary = add_cron_schedule(
                cron_expression=cron_expression,
                active_sources=normalized_sources,
                enabled=should_enable,
            )
        elif trigger_type_normalized == "interval":
            if interval_hours is None:
                raise ScheduleValidationError(
                    "매 N시간 값이 누락되었습니다."
                )
            summary = add_interval_schedule(
                hours=interval_hours,
                active_sources=normalized_sources,
                enabled=should_enable,
            )
        else:
            raise ScheduleValidationError(
                f"지원하지 않는 trigger_type: {trigger_type!r}. "
                "'cron' 또는 'interval' 만 허용합니다."
            )
    except ScheduleValidationError as exc:
        logger.info(
            "스케줄 등록 거부 — 검증 실패: 관리자={} ({})",
            current_user.username, exc,
        )
        return RedirectResponse(
            url=_schedule_flash_url(str(exc), level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except Exception as exc:
        # APScheduler 자체의 오류(jobstore 연결 실패 등)
        logger.exception(
            "스케줄 등록 실패(예기치 못한 예외): 관리자={} ({}: {})",
            current_user.username, type(exc).__name__, exc,
        )
        return RedirectResponse(
            url=_schedule_flash_url(
                f"스케줄 등록 실패({type(exc).__name__}): {exc}",
                level="error",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    logger.info(
        "스케줄 등록: 관리자={} job_id={} trigger_type={} spec={!r} enabled={}",
        current_user.username,
        summary.job_id,
        summary.trigger_type,
        summary.trigger_spec,
        summary.enabled,
    )
    message = (
        f"스케줄 등록 완료 (id={summary.job_id}, {summary.trigger_type}: "
        f"{summary.trigger_spec}, enabled={summary.enabled})."
    )
    return RedirectResponse(
        url=_schedule_flash_url(message, level="success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/schedule/{job_id}/toggle",
    dependencies=[Depends(ensure_same_origin)],
)
def schedule_toggle(
    job_id: str,
    enabled: str = Form(...),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """스케줄을 활성/비활성 토글한다.

    Form:
        enabled: 'true' 또는 'false' 문자열. 체크박스 POST 와 맞추기 위해
                 문자열로 받고 수동 파싱.
    """
    normalized = enabled.strip().lower()
    if normalized not in ("true", "false"):
        return RedirectResponse(
            url=_schedule_flash_url(
                f"enabled 값은 'true' 또는 'false' 여야 합니다 (입력: {enabled!r}).",
                level="error",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    target_enabled = normalized == "true"

    try:
        summary = toggle_schedule(job_id, enabled=target_enabled)
    except ScheduleValidationError as exc:
        logger.info(
            "스케줄 토글 실패: 관리자={} job_id={} ({})",
            current_user.username, job_id, exc,
        )
        return RedirectResponse(
            url=_schedule_flash_url(str(exc), level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    logger.info(
        "스케줄 토글 완료: 관리자={} job_id={} enabled={} next_run_time={}",
        current_user.username,
        job_id,
        summary.enabled,
        summary.next_run_time,
    )
    verb = "활성화" if target_enabled else "비활성화"
    return RedirectResponse(
        url=_schedule_flash_url(
            f"스케줄 {verb} 완료 (id={job_id}).",
            level="success",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/schedule/{job_id}/delete",
    dependencies=[Depends(ensure_same_origin)],
)
def schedule_delete(
    job_id: str,
    current_user: User = Depends(admin_user_required),
) -> Response:
    """스케줄을 삭제한다 (jobstore 에서 제거).

    삭제 전에 확인 팝업은 UI JS 가 처리한다.
    """
    try:
        delete_schedule(job_id)
    except ScheduleValidationError as exc:
        logger.info(
            "스케줄 삭제 실패: 관리자={} job_id={} ({})",
            current_user.username, job_id, exc,
        )
        return RedirectResponse(
            url=_schedule_flash_url(str(exc), level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    logger.info(
        "스케줄 삭제 완료: 관리자={} job_id={}",
        current_user.username, job_id,
    )
    return RedirectResponse(
        url=_schedule_flash_url(
            f"스케줄 삭제 완료 (id={job_id}).",
            level="success",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = [
    "LOG_FILE_MAX_BYTES",
    "RECENT_RUN_LIMIT",
    "router",
]
