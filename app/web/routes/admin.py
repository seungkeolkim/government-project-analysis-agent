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

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from loguru import logger

from sqlalchemy import select

from app.auth.constants import (
    COOKIE_HTTP_ONLY,
    COOKIE_PATH,
    COOKIE_SAMESITE,
    COOKIE_SECURE,
    SESSION_COOKIE_NAME,
    SESSION_LIFETIME_DAYS,
)
from app.auth.dependencies import admin_user_required, ensure_same_origin
from app.auth.service import (
    PasswordPolicyError,
    change_email,
    change_email_subscribed,
    change_password,
    create_session,
    delete_user,
)
from app.db.models import Organization, ScrapeRun, User
from app.db.session import SessionLocal, session_scope
from app.organizations.io import (
    export_organization_tree_json,
    import_organization_tree_json,
)
from app.organizations.service import (
    DuplicateOrganizationNameError,
    OrganizationHasChildrenError,
    OrganizationInvalidMoveError,
    OrganizationNotFoundError,
    build_organization_tree,
    create_organization,
    delete_organization,
    get_user_organization_ids,
    list_all_organizations,
    move_organization,
    rename_organization,
    set_user_organizations,
)
from app.db.repository import (
    count_scrape_runs,
    get_available_source_ids,
    get_running_scrape_run,
    list_recent_scrape_runs,
)
from app.backup.constants import (
    BACKUP_TRIGGER_MANUAL,
    SETTING_KEY_BACKUP_MAX_COUNT,
)
from app.backup.service import (
    get_backup_settings,
    list_backup_files,
    list_backup_history,
    run_backup,
    set_setting as set_backup_setting,
)
from app.scheduler import (
    CronExpressionError,
    ScheduledJobConfigError,
    ScheduledJobRecord,
    add_general_schedule,
    delete_scheduled_job,
    list_general_schedules,
    reinstall_crontab_after_change,
    set_scheduled_job_enabled,
    upsert_singleton_schedule,
    validate_cron_expression,
)
from app.scheduler.constants import (
    JOB_KIND_BACKUP,
    TRIGGER_TYPE_CRON,
    TRIGGER_TYPE_INTERVAL,
)
from app.scrape_control import (
    ComposeEnvironmentError,
    ScrapeAlreadyRunningError,
    read_recent_startup_events,
    request_cancel,
    scrape_run_log_path,
    start_scrape_run,
    trigger_self_restart,
)
from app.sources.yaml_editor import (
    SourcesYamlValidationError,
    load_sources_yaml_text,
    save_sources_yaml_text,
)
from app.config import get_settings
from app.timezone import format_kst
from app.web.access_log_stats import (
    aggregate_daily_stats,
    aggregate_ip_history,
    filter_records_by_ip,
    get_recent_raw_rows,
    iter_log_records_for_days,
)
from app.web.template_filters import register_kst_filters

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# 최근 이력 기본 개수. UI 폴링 응답에도 동일 값을 사용한다.
RECENT_RUN_LIMIT: int = 20

# 최근 이력 페이지네이션 — 한 페이지당 표시 건수.
RECENT_RUN_PAGE_SIZE: int = 10

# 로그 파일 응답 최대 크기 (bytes). 너무 긴 로그가 UI 를 마비시키지 않도록.
# 현재는 파일 전체를 반환하지만 상한을 두어 안전망을 확보한다.
LOG_FILE_MAX_BYTES: int = 1_000_000  # 1MB

# 조직 트리 import 허용 최대 파일 크기 (bytes). 조직 트리 규모를 고려해 1MB 로 설정한다.
_IMPORT_MAX_FILE_SIZE: int = 1_048_576  # 1MB

# 관리자 템플릿 루트. Phase 1b 인증 템플릿과 같은 디렉터리를 공유한다.
_ADMIN_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"
_templates: Jinja2Templates = Jinja2Templates(directory=str(_ADMIN_TEMPLATES_DIR))
# task 00040-3 — 관리자 탭(수집 제어/sources.yaml/스케줄) 템플릿도 KST 필터를
# 사용하도록 모듈 import 시점에 한 번 등록한다. ``app.web.main.create_app`` 의
# 다른 Jinja2Templates 인스턴스와 별개라 따로 등록해야 한다.
register_kst_filters(_templates)


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


def _coerce_count(value: Any) -> int:
    """source_counts dict 에서 읽어 들인 카운트를 안전하게 정수로 변환한다.

    Args:
        value: dict 에서 꺼낸 임의 값. 정상적으로는 int 지만, 옛 row 또는
            손상된 row 에 None / 문자열 / float 가 들어 있을 수 있다.

    Returns:
        가능한 경우 int 로 환산한 값. 변환 실패하면 0 (raise 하지 않는다 —
        UI 가 통째로 500 이 나면 관리자 탭이 마비된다).
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        # bool 은 int 의 서브클래스라 isinstance(value, int) 보다 먼저 거른다.
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _summarize_source_counts(source_counts: dict[str, Any]) -> list[str]:
    """ScrapeRun.source_counts 를 사용자 화면 표시용 요약 segment 리스트로 변환.

    스크래퍼 로그 마지막 줄(``app/cli.py`` 의 'scrape 실행 완료: ...' 라인) 의
    형식과 동일한 segment 5개를 만들어 돌려준다 — 사용자 원문에서 \"바로 보이길
    원하는 정보\" 가 그 로그 한 줄이기 때문이다.

    반환되는 segment 순서/형식:
        1) ``성공 N건 / 실패 N건`` (collection.delta_inserted/delta_failed)
        2) ``상세 성공 N건 / 실패 N건 / 생략(unchanged peek) N건``
        3) ``첨부 다운로드 성공 N건 / 실패 N건``
        4) ``apply action 분포: 신규=N 변경없음=N 버전갱신=N 상태전이=N``
        5) ``apply 2차 감지(첨부 변경)=N건``

    ``_build_final_source_counts`` (``app/cli.py``) 가 만든 새 schema(``collection``
    /``apply`` 두 섹션) 를 1순위로 읽고, 누락 키는 0 으로 폴백한다. 진행 중
    running row 처럼 ``collection`` 도 ``apply`` 도 없는 경우(키가 ``active_sources``
    뿐) 에는 표시할 의미 있는 값이 없으므로 **빈 리스트** 를 반환한다 —
    호출자(템플릿/JS) 는 비어 있으면 셀에 아무것도 그리지 않는다.

    옛 row 폴백 정책: collection/apply 가 부분적으로라도 있으면 segment 를
    그리고, 누락 키는 0 으로 둔다. raise 하지 않는다 — UI 가 500 이 나면
    [수집 제어] 탭 전체가 마비되고 그쪽 피해가 더 크다.

    Args:
        source_counts: ScrapeRun.source_counts 컬럼에서 읽은 dict (또는 빈 dict).

    Returns:
        화면 표시용 한 줄 segment 의 리스트. 표시할 값이 없으면 빈 리스트.
    """
    if not source_counts:
        return []

    collection = source_counts.get("collection")
    apply_section = source_counts.get("apply")
    if not isinstance(collection, dict) and not isinstance(apply_section, dict):
        # 새 schema 의 두 섹션이 모두 없다 — running row 또는 finalize 가 안 된
        # 케이스. 표시할 정보가 없으므로 빈 리스트를 돌려준다.
        return []

    collection_dict: dict[str, Any] = collection if isinstance(collection, dict) else {}
    apply_dict: dict[str, Any] = (
        apply_section if isinstance(apply_section, dict) else {}
    )
    action_counts_raw = apply_dict.get("action_counts")
    action_counts: dict[str, Any] = (
        action_counts_raw if isinstance(action_counts_raw, dict) else {}
    )

    delta_inserted = _coerce_count(collection_dict.get("delta_inserted"))
    delta_failed = _coerce_count(collection_dict.get("delta_failed"))
    detail_success = _coerce_count(collection_dict.get("detail_success"))
    detail_failure = _coerce_count(collection_dict.get("detail_failure"))
    skipped_detail = _coerce_count(collection_dict.get("skipped_detail"))
    attachment_dl_success = _coerce_count(
        collection_dict.get("attachment_download_success")
    )
    attachment_dl_failure = _coerce_count(
        collection_dict.get("attachment_download_failure")
    )

    apply_created = _coerce_count(action_counts.get("created"))
    apply_unchanged = _coerce_count(action_counts.get("unchanged"))
    apply_new_version = _coerce_count(action_counts.get("new_version"))
    apply_status_transitioned = _coerce_count(
        action_counts.get("status_transitioned")
    )
    apply_attachment_content_change = _coerce_count(
        apply_dict.get("attachment_content_change")
    )

    return [
        f"성공 {delta_inserted}건 / 실패 {delta_failed}건",
        (
            f"상세 성공 {detail_success}건 / 실패 {detail_failure}건 / "
            f"생략(unchanged peek) {skipped_detail}건"
        ),
        f"첨부 다운로드 성공 {attachment_dl_success}건 / 실패 {attachment_dl_failure}건",
        (
            f"apply action 분포: 신규={apply_created} 변경없음={apply_unchanged} "
            f"버전갱신={apply_new_version} 상태전이={apply_status_transitioned}"
        ),
        f"apply 2차 감지(첨부 변경)={apply_attachment_content_change}건",
    ]


def _serialize_scrape_run(run: ScrapeRun) -> dict[str, Any]:
    """ScrapeRun ORM 을 JSON 직렬화 가능한 dict 로 변환한다.

    템플릿 렌더링(HTML) 과 폴링 응답(JSON) 양쪽에서 공통 사용한다.
    민감 정보(예: pid) 는 관리자 전용 화면이므로 포함한다.

    - ``started_at`` / ``ended_at`` 은 ISO-8601 UTC 문자열로 유지한다 (외부 JSON
      컨슈머 호환).
    - ``started_at_display`` / ``ended_at_display`` 는 task 00040-3 에서 추가된
      KST 표시용 문자열(``\"%Y-%m-%d %H:%M:%S\"``) 이다. 관리자 [수집 제어] 탭
      템플릿(``admin/_recent_runs_table.html`` / ``admin/control.html`` 의 폴링 JS)
      이 사용자 화면 표기에 사용한다 — 사용자 원문 검증 ③ 의 \"ScrapeRun
      started_at/ended_at 표시 KST\" 요건을 만족시키되 원본 ISO 필드는
      깨뜨리지 않기 위함이다. None 입력은 빈 문자열로 통일.
    - ``source_counts`` 는 dict 원본을 그대로 노출 (UI 는 subset 만 사용).
    - ``summary_segments`` 는 표시용으로 사전 계산해 둔 한 줄짜리 segment 리스트
      이다 (task 00045 — \"최근 실행 이력 카드에 스크래퍼 로그 마지막 줄 수준의
      정보가 바로 보이길 원해\"). 템플릿/폴링 JS 는 이 필드를 그대로 출력만 하면
      되므로, 표시 로직 분기점이 한 곳(``_summarize_source_counts``)에 모인다.
    """
    source_counts_dict = dict(run.source_counts or {})
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        # KST 표시용 — 화면 출력 직전에만 사용한다. 정렬·비교 로직은 절대 이
        # 필드를 쓰지 말 것 (단순 표시 문자열).
        "started_at_display": format_kst(run.started_at, "%Y-%m-%d %H:%M:%S"),
        "ended_at_display": format_kst(run.ended_at, "%Y-%m-%d %H:%M:%S"),
        "status": run.status,
        "trigger": run.trigger,
        "source_counts": source_counts_dict,
        "summary_segments": _summarize_source_counts(source_counts_dict),
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
    page: int = Query(default=1, ge=1),
    flash: Optional[str] = Query(default=None),
    flash_level: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[수집 제어] 탭 — 현재 상태, 시작/중단 폼, 최근 이력.

    쿼리 파라미터:
        page:        최근 이력 페이지 번호 (1-based, 기본 1).
        flash:       POST 후 redirect 시 안내 메시지. 없으면 비노출.
        flash_level: 'success' / 'error'. CSS 배지 색 분기용. 기본 'success'.

    템플릿 컨텍스트:
        active_tab:       탭 활성 표시 용 ('scrape').
        running:          현재 running ScrapeRun 의 직렬화 dict 또는 None.
        recent_runs:      최근 ScrapeRun 직렬화 리스트 (최신순, 현재 페이지).
        page:             현재 페이지 번호.
        page_size:        페이지당 표시 건수.
        total:            전체 ScrapeRun 수.
        total_pages:      전체 페이지 수.
        available_sources: sources.yaml 에서 읽은 등록 소스 id 목록.
        flash / flash_level: 상단 안내 배지.
        current_user:     상단 네비 + is_admin 조건 분기용.
    """
    # 00030-3 — /admin/scrape 500 원인 추적용 진입 로그. 이 라인이 없으면
    # admin_user_required 이전 단계(쿠키/세션/User 로드) 문제, 있으나 그 다음
    # DB 조회 / 템플릿 렌더에서 터졌다면 해당 구간 문제로 범위를 좁힐 수 있다.
    logger.debug(
        "admin.scrape_control_page 진입: user_id={} page={} has_flash={}",
        current_user.id,
        page,
        flash is not None,
    )
    with session_scope() as session:
        total = count_scrape_runs(session)
        total_pages = max(1, math.ceil(total / RECENT_RUN_PAGE_SIZE))
        # page 가 범위를 벗어나면 마지막 페이지로 클램프
        page = min(page, total_pages)
        offset = (page - 1) * RECENT_RUN_PAGE_SIZE
        running_row = get_running_scrape_run(session)
        recent_rows = list_recent_scrape_runs(
            session, limit=RECENT_RUN_PAGE_SIZE, offset=offset
        )
        running_payload = _serialize_scrape_run(running_row) if running_row else None
        recent_payload = [_serialize_scrape_run(row) for row in recent_rows]

    # sources.yaml 기반 등록 소스 목록. 변경 시 자동 반영 (session 과 무관).
    available_sources = get_available_source_ids()
    logger.debug(
        "admin.scrape_control_page DB 조회 완료: running={} recent_count={} sources_count={} page={}/{}",
        running_payload is not None,
        len(recent_payload),
        len(available_sources),
        page,
        total_pages,
    )

    return _templates.TemplateResponse(
        request,
        "admin/control.html",
        {
            "top_tab": "scrape_group",
            "sub_tab": "scrape",
            "running": running_payload,
            "recent_runs": recent_payload,
            "page": page,
            "page_size": RECENT_RUN_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
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
def scrape_status(
    page: int = Query(default=1, ge=1),
) -> JSONResponse:
    """5초 폴링으로 현재 running 상태 + 최근 이력을 갱신해서 돌려준다.

    사용자 원문 '5초 폴링으로 상태 갱신 (SSE 과함)' 에 따라 JSON 엔드포인트만
    제공한다. 응답 스키마:

    ```
    {
      \"running\": { id, started_at, status, trigger, source_counts, pid, ... } | null,
      \"recent\":  [ { id, started_at, ended_at, status, trigger, source_counts, ... }, ... ],
      \"poll_interval_ms\": 5000,
      \"page\": 1,
      \"page_size\": 10,
      \"total\": 25,
      \"total_pages\": 3
    }
    ```

    쿼리 파라미터:
        page: 최근 이력 페이지 번호 (1-based, 기본 1). JS 폴링이 현재 보는 페이지를 전달.
    """
    with session_scope() as session:
        total = count_scrape_runs(session)
        total_pages = max(1, math.ceil(total / RECENT_RUN_PAGE_SIZE))
        # page 가 범위를 벗어나면 마지막 페이지로 클램프
        page = min(page, total_pages)
        offset = (page - 1) * RECENT_RUN_PAGE_SIZE
        running_row = get_running_scrape_run(session)
        recent_rows = list_recent_scrape_runs(
            session, limit=RECENT_RUN_PAGE_SIZE, offset=offset
        )
        running_payload = _serialize_scrape_run(running_row) if running_row else None
        recent_payload = [_serialize_scrape_run(row) for row in recent_rows]

    return JSONResponse(
        {
            "running": running_payload,
            "recent": recent_payload,
            "poll_interval_ms": 5000,
            "page": page,
            "page_size": RECENT_RUN_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
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
            "top_tab": "scrape_group",
            "sub_tab": "sources",
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
    logger.debug("admin.sources_yaml_page 진입: user_id={}", current_user.id)
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


@dataclass(frozen=True)
class _GeneralScheduleView:
    """[스케줄] 탭 템플릿이 1건의 일반 수집 스케줄을 렌더할 때 쓰는 표시용 DTO.

    APScheduler 시절의 ``ScheduleSummary`` 가 제공하던 표면(job_id/trigger_type/
    trigger_spec/active_sources/enabled)을 그대로 유지해 템플릿 변경을 최소화한다.
    cron 데몬이 스케줄을 실행하므로 정확한 next_run_time 은 더 이상 보유하지 않고,
    템플릿은 cron 표현식/활성 여부로 충분히 상태를 보여 준다.

    Attributes:
        job_id:         스케줄 식별자(:class:`ScheduledJobRecord.id`, 정수 대리 키).
        trigger_type:   'cron' 또는 'interval'.
        trigger_spec:   사람-친화 표현(cron 표현식 또는 '매 N시간').
        active_sources: 트리거 시 수집할 source id 목록(빈 리스트=전체).
        enabled:        활성 여부.
    """

    job_id: int
    trigger_type: str
    trigger_spec: str
    active_sources: list[str]
    enabled: bool


def _build_general_schedule_view(record: ScheduledJobRecord) -> _GeneralScheduleView:
    """SSOT(scheduled_jobs) 레코드를 [스케줄] 탭 표시용 DTO 로 변환한다.

    interval 트리거는 '매 N시간' 으로, cron 트리거는 cron 표현식 그대로 표시한다.

    Args:
        record: ``scheduled_jobs`` 에서 읽은 :class:`ScheduledJobRecord`.

    Returns:
        템플릿이 렌더할 :class:`_GeneralScheduleView`.
    """
    if record.trigger_type == TRIGGER_TYPE_INTERVAL:
        trigger_spec = f"매 {record.interval_hours}시간"
    else:
        trigger_spec = record.cron_expression or ""
    return _GeneralScheduleView(
        job_id=record.id,
        trigger_type=record.trigger_type,
        trigger_spec=trigger_spec,
        active_sources=list(record.active_sources),
        enabled=record.enabled,
    )


def _reinstall_crontab_quietly() -> None:
    """스케줄/백업/Daily Report 설정 변경 후 crontab 을 재설치한다(비치명적).

    :func:`reinstall_crontab_after_change` 는 ``ENABLE_CRON=1`` 컨테이너에서만
    실제 설치하고, 개발/테스트 호스트나 설치 실패 시 graceful no-op 으로 결과를
    돌려준다(예외를 던지지 않는다). 따라서 라우트는 설치 결과를 로깅만 하고
    절대 500 으로 떨어지지 않는다. 호출 측은 **설정 저장 트랜잭션을 커밋한 뒤**
    이 함수를 호출해야 한다(재설치가 새 세션으로 최신 설정을 다시 읽기 때문).
    """
    result = reinstall_crontab_after_change()
    if not result.installed:
        logger.info("crontab 재설치 생략/실패(비치명적): {}", result.reason)


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(
    request: Request,
    flash: Optional[str] = Query(default=None),
    flash_level: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[스케줄] 탭 — 등록된 일반 수집 스케줄 목록 + 신규 등록 폼.

    스케줄은 단일 SSOT 테이블 ``scheduled_jobs``(:mod:`app.scheduler.scheduled_job_store`)
    에서 읽으며, 변경 시 crontab 을 재설치한다(컨테이너에서만 실제 설치). 백업/Daily Report 잡은 각
    [시스템 백업]/[메일 발송] 탭에서 별도 관리하므로 본 목록에는 포함하지 않는다.

    템플릿 컨텍스트:
        schedules:          _GeneralScheduleView 리스트 (저장 순서 보존).
        available_sources:  등록 폼에서 고를 수 있는 source id 목록.
        flash / flash_level: 상단 안내 배지.
        current_user:       네비 + is_admin 분기용.
    """
    logger.debug("admin.schedule_page 진입: user_id={}", current_user.id)
    try:
        with session_scope() as session:
            records = list_general_schedules(session)
        schedules = [_build_general_schedule_view(record) for record in records]
    except Exception as exc:
        # 저장소 접근 실패 등 예외. UI 는 빈 목록으로 폴백하되 에러를 flash 로 노출.
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
            "top_tab": "scrape_group",
            "sub_tab": "schedule",
            "schedules": schedules,
            "available_sources": get_available_source_ids(),
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
            raise ScheduledJobConfigError(
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
    """새 일반 수집 스케줄을 저장소에 등록하고 crontab 을 재설치한다.

    Form 필드:
        trigger_type:    'cron' 또는 'interval' — 입력 분기.
        cron_expression: trigger_type='cron' 일 때 사용. 5-필드 cron.
        interval_hours:  trigger_type='interval' 일 때 사용. 양의 정수.
        active_sources:  체크박스 multi 값. 비어 있으면 전체.
        enabled:         체크박스 'on' 또는 누락. 기본은 등록 즉시 활성.

    흐름 & 예외:
        trigger_type/필드 누락 또는 cron 표현식·interval 범위 위반이면
        ScheduleConfigError/CronExpressionError 로 flash error redirect. 저장
        커밋 후 crontab 을 재설치한다(컨테이너에서만 실제 설치). 성공 시 PRG
        패턴(303) 로 /admin/schedule.
    """
    trigger_type_normalized = trigger_type.strip().lower()
    should_enable = bool(enabled) and enabled != "off"

    available = get_available_source_ids()
    try:
        normalized_sources = _parse_schedule_active_sources(active_sources, available)

        with session_scope() as session:
            if trigger_type_normalized == "cron":
                if not cron_expression:
                    raise ScheduledJobConfigError("cron 표현식이 누락되었습니다.")
                # cron 표현식 내용을 먼저 검증한다(필드 개수 + 값 범위). 저장소도
                # 필드 개수를 검증하지만, 라우트에서 값 범위까지 미리 막아 잘못된
                # 표현식이 crontab 으로 흘러가지 않게 한다.
                validated_expression = validate_cron_expression(cron_expression)
                record = add_general_schedule(
                    session,
                    trigger_type=TRIGGER_TYPE_CRON,
                    cron_expression=validated_expression,
                    active_sources=normalized_sources,
                    enabled=should_enable,
                )
            elif trigger_type_normalized == "interval":
                if interval_hours is None:
                    raise ScheduledJobConfigError("매 N시간 값이 누락되었습니다.")
                record = add_general_schedule(
                    session,
                    trigger_type=TRIGGER_TYPE_INTERVAL,
                    interval_hours=interval_hours,
                    active_sources=normalized_sources,
                    enabled=should_enable,
                )
            else:
                raise ScheduledJobConfigError(
                    f"지원하지 않는 trigger_type: {trigger_type!r}. "
                    "'cron' 또는 'interval' 만 허용합니다."
                )
    except (ScheduledJobConfigError, CronExpressionError) as exc:
        logger.info(
            "스케줄 등록 거부 — 검증 실패: 관리자={} ({})",
            current_user.username, exc,
        )
        return RedirectResponse(
            url=_schedule_flash_url(str(exc), level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except Exception as exc:
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

    # 저장이 커밋된 뒤 crontab 을 재설치한다(새 세션이 방금 저장한 값을 읽는다).
    _reinstall_crontab_quietly()

    view = _build_general_schedule_view(record)
    logger.info(
        "스케줄 등록: 관리자={} id={} trigger_type={} spec={!r} enabled={}",
        current_user.username,
        view.job_id,
        view.trigger_type,
        view.trigger_spec,
        view.enabled,
    )
    message = (
        f"스케줄 등록 완료 (id={view.job_id}, {view.trigger_type}: "
        f"{view.trigger_spec}, enabled={view.enabled})."
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
    job_id: int,
    enabled: str = Form(...),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """스케줄을 활성/비활성 토글하고 crontab 을 재설치한다.

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
        with session_scope() as session:
            set_scheduled_job_enabled(session, job_id, enabled=target_enabled)
    except ScheduledJobConfigError as exc:
        logger.info(
            "스케줄 토글 실패: 관리자={} job_id={} ({})",
            current_user.username, job_id, exc,
        )
        return RedirectResponse(
            url=_schedule_flash_url(str(exc), level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    _reinstall_crontab_quietly()

    logger.info(
        "스케줄 토글 완료: 관리자={} job_id={} enabled={}",
        current_user.username,
        job_id,
        target_enabled,
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
    job_id: int,
    current_user: User = Depends(admin_user_required),
) -> Response:
    """스케줄을 SSOT(scheduled_jobs)에서 삭제하고 crontab 을 재설치한다.

    삭제 전에 확인 팝업은 UI JS 가 처리한다.
    """
    with session_scope() as session:
        deleted = delete_scheduled_job(session, job_id)

    if not deleted:
        logger.info(
            "스케줄 삭제 실패(없음): 관리자={} job_id={}",
            current_user.username, job_id,
        )
        return RedirectResponse(
            url=_schedule_flash_url(
                f"스케줄 id={job_id!r} 를 찾을 수 없습니다.",
                level="error",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    _reinstall_crontab_quietly()

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


# ──────────────────────────────────────────────────────────────
# [조직 구성] 탭 (task 00049-3)
# ──────────────────────────────────────────────────────────────


def _flatten_org_tree(nodes: list[dict], depth: int = 0) -> list[dict]:
    """조직 트리를 select 드롭다운용 평탄한 리스트로 변환한다.

    depth 에 따라 들여쓰기 prefix 를 option label 에 붙여 트리 구조를 암시한다.

    Args:
        nodes: build_organization_tree 가 반환한 중첩 dict 목록.
        depth: 현재 노드의 깊이. 재귀 호출 시 1씩 증가한다.

    Returns:
        [{"id": int, "label": str}, ...] 평탄한 목록. 트리 순서 유지.
    """
    result: list[dict] = []
    for node in nodes:
        prefix = "　" * depth  # 전각 공백으로 depth 시각화 (HTML select 에서도 보임)
        result.append({"id": node["id"], "label": f"{prefix}{node['name']}"})
        result.extend(_flatten_org_tree(node["children"], depth + 1))
    return result


def _org_flash_url(message: str, *, level: str) -> str:
    """/admin/organizations?flash=...&flash_level=... redirect URL 빌더."""
    from urllib.parse import urlencode

    query = urlencode({"flash": message, "flash_level": level})
    return f"/admin/organizations?{query}"


@router.get("/organizations", response_class=HTMLResponse)
def organizations_page(
    request: Request,
    flash: Optional[str] = Query(default=None),
    flash_level: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[조직 구성] 탭 — 조직 트리 표시 + 추가/삭제 UI.

    쿼리 파라미터:
        flash:       POST 후 redirect 시 안내 메시지. 없으면 비노출.
        flash_level: 'success' / 'error'. 기본 'success'.

    템플릿 컨텍스트:
        top_tab:       'org_group'.
        sub_tab:       'organizations'.
        org_tree:      build_organization_tree 가 반환한 중첩 dict 목록.
        flat_orgs:     select 드롭다운용 평탄한 list[{"id", "label"}].
                       "(최상위)" 옵션은 템플릿에서 value="" 로 추가한다.
        flash / flash_level: 상단 안내 배지.
        current_user:  네비 + is_admin 분기용.
    """
    logger.debug("admin.organizations_page 진입: user_id={}", current_user.id)
    session = SessionLocal()
    try:
        all_orgs = list_all_organizations(session)
    finally:
        session.close()

    org_tree = build_organization_tree(all_orgs)
    flat_orgs = _flatten_org_tree(org_tree)

    return _templates.TemplateResponse(
        request,
        "admin/organizations.html",
        {
            "top_tab": "org_group",
            "sub_tab": "organizations",
            "org_tree": org_tree,
            "flat_orgs": flat_orgs,
            "flash": flash,
            "flash_level": flash_level or "success",
            "current_user": current_user,
        },
    )


@router.get("/organizations/export")
def organizations_export(
    current_user: User = Depends(admin_user_required),
) -> Response:
    """조직 트리를 pretty-print JSON 파일로 다운로드한다.

    파일명은 KST 타임스탬프를 포함한 ASCII 문자열로 지정한다
    (브라우저 Content-Disposition 헤더가 ASCII 만 안전하게 처리하므로).
    """
    from datetime import datetime, timedelta, timezone

    session = SessionLocal()
    try:
        json_content = export_organization_tree_json(session)
    finally:
        session.close()

    kst = timezone(timedelta(hours=9))
    timestamp = datetime.now(kst).strftime("%Y%m%d_%H%M%S")
    filename = f"organizations_{timestamp}.json"

    logger.info(
        "조직 트리 export: 관리자={} filename={}", current_user.username, filename
    )
    return Response(
        content=json_content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post(
    "/organizations/import",
    dependencies=[Depends(ensure_same_origin)],
)
async def organizations_import(
    file: UploadFile = File(...),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """JSON 파일을 업로드하여 조직 트리 전체를 교체한다.

    처리 흐름:
        1. 파일 크기 검사 (1MB 초과 시 error flash redirect).
        2. import_organization_tree_json 호출 (트랜잭션).
        3. 성공 → 통계 포함 success flash redirect.
        4. ValueError / DuplicateOrganizationNameError → error flash redirect.

    import 실패 시 전체 롤백 — session.rollback() 으로 보장.
    """
    content = await file.read()
    if len(content) > _IMPORT_MAX_FILE_SIZE:
        return RedirectResponse(
            url=_org_flash_url(
                f"파일 크기가 너무 큽니다 (최대 1MB). 현재: {len(content) // 1024}KB",
                level="error",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    json_text = content.decode("utf-8", errors="replace")

    session = SessionLocal()
    try:
        try:
            stats = import_organization_tree_json(session, json_text)
            session.commit()
        except (ValueError, DuplicateOrganizationNameError) as exc:
            session.rollback()
            logger.info(
                "조직 트리 import 거부: 관리자={} ({})",
                current_user.username, exc,
            )
            return RedirectResponse(
                url=_org_flash_url(f"Import 실패: {exc}", level="error"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
    finally:
        session.close()

    total = stats["total_organizations"]
    dropped = stats["dropped_user_org_count"]
    affected = stats["affected_user_count"]

    flash_msg = f"조직 트리 import 완료: 총 {total}개 조직 등록."
    if dropped > 0:
        flash_msg += f" (사용자-조직 매핑 {dropped}건 정리, {affected}명 영향)"

    logger.info(
        "조직 트리 import 완료: 관리자={} total={} dropped={} affected={}",
        current_user.username, total, dropped, affected,
    )
    return RedirectResponse(
        url=_org_flash_url(flash_msg, level="success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/organizations/create", dependencies=[Depends(ensure_same_origin)])
def organizations_create(
    request: Request,
    name: str = Form(...),
    parent_id: Optional[int] = Form(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """새 조직을 생성한다.

    Form 필드:
        name:      조직명. 좌우 공백은 서비스 레이어에서 제거된다.
        parent_id: 부모 조직 PK. 공백/0/미전송이면 최상위(None)로 처리한다.

    흐름:
        1. parent_id 정규화(0 또는 미전송 → None).
        2. create_organization 호출.
        3. OrganizationNotFoundError / DuplicateOrganizationNameError → flash error redirect.
        4. 성공 → flash success redirect (PRG 패턴).
    """
    # HTML select 에서 value="" 로 최상위를 표현하므로 0 또는 None 모두 최상위.
    normalized_parent_id = parent_id if parent_id else None

    session = SessionLocal()
    try:
        try:
            create_organization(session, name=name, parent_id=normalized_parent_id)
            session.commit()
        except (OrganizationNotFoundError, DuplicateOrganizationNameError) as exc:
            session.rollback()
            logger.info(
                "조직 생성 거부: 관리자={} name={!r} parent_id={} ({})",
                current_user.username, name, normalized_parent_id, exc,
            )
            return RedirectResponse(
                url=_org_flash_url(str(exc), level="error"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
    finally:
        session.close()

    logger.info(
        "조직 생성 완료: 관리자={} name={!r} parent_id={}",
        current_user.username, name, normalized_parent_id,
    )
    return RedirectResponse(
        url=_org_flash_url(f"조직 '{name}' 이(가) 추가되었습니다.", level="success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/organizations/{org_id}/delete",
    dependencies=[Depends(ensure_same_origin)],
)
def organizations_delete(
    org_id: int,
    current_user: User = Depends(admin_user_required),
) -> Response:
    """조직을 삭제한다.

    자식 조직이 있으면 OrganizationHasChildrenError 가 발생해 flash error redirect 한다.
    삭제된 조직의 user_organizations 매핑은 ORM cascade 또는 DB FK 에 의해 정리된다.

    Args:
        org_id: 삭제할 조직 PK (URL 경로 파라미터).
    """
    session = SessionLocal()
    org_name = f"id={org_id}"
    try:
        try:
            # 삭제 전 이름을 읽어 둔다 (삭제 후에는 ORM 인스턴스를 참조할 수 없다).
            org = session.get(Organization, org_id)
            if org is not None:
                org_name = org.name
            delete_organization(session, org_id)
            session.commit()
        except OrganizationNotFoundError as exc:
            session.rollback()
            logger.info(
                "조직 삭제 거부(없음): 관리자={} org_id={} ({})",
                current_user.username, org_id, exc,
            )
            return RedirectResponse(
                url=_org_flash_url(str(exc), level="error"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except OrganizationHasChildrenError as exc:
            session.rollback()
            logger.info(
                "조직 삭제 거부(자식 있음): 관리자={} org_id={} ({})",
                current_user.username, org_id, exc,
            )
            return RedirectResponse(
                url=_org_flash_url(str(exc), level="error"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
    finally:
        session.close()

    logger.info(
        "조직 삭제 완료: 관리자={} org_id={} name={!r}",
        current_user.username, org_id, org_name,
    )
    return RedirectResponse(
        url=_org_flash_url(f"조직 '{org_name}' 이(가) 삭제되었습니다.", level="success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/organizations/{org_id}/rename",
    dependencies=[Depends(ensure_same_origin)],
)
def organizations_rename(
    org_id: int,
    new_name: str = Form(...),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """조직명을 변경한다.

    Form 필드:
        new_name: 새 조직명. 좌우 공백은 서비스 레이어에서 제거된다.

    흐름:
        1. rename_organization 호출.
        2. ValueError(빈 이름) / OrganizationNotFoundError / DuplicateOrganizationNameError
           → flash error redirect.
        3. 성공 → flash success redirect (PRG 패턴).

    Args:
        org_id:  이름을 변경할 조직 PK (URL 경로 파라미터).
        new_name: 새 조직명 (Form).
    """
    session = SessionLocal()
    try:
        try:
            rename_organization(session, org_id, new_name)
            session.commit()
        except ValueError as exc:
            session.rollback()
            logger.info(
                "조직 이름 변경 거부: 관리자={} org_id={} new_name={!r} ({})",
                current_user.username, org_id, new_name, exc,
            )
            return RedirectResponse(
                url=_org_flash_url(str(exc), level="error"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
    finally:
        session.close()

    stripped_name = new_name.strip()
    logger.info(
        "조직 이름 변경 완료: 관리자={} org_id={} new_name={!r}",
        current_user.username, org_id, stripped_name,
    )
    return RedirectResponse(
        url=_org_flash_url(f"조직명이 '{stripped_name}' 으로 변경되었습니다.", level="success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/organizations/{org_id}/move",
    dependencies=[Depends(ensure_same_origin)],
)
def organizations_move(
    org_id: int,
    new_parent_id: Optional[int] = Form(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """조직의 상위 조직(parent_id)을 변경한다.

    Form 필드:
        new_parent_id: 새 부모 조직 PK. 공백/0/미전송이면 최상위(None)로 처리한다.

    흐름:
        1. new_parent_id 정규화(0 또는 None → None).
        2. move_organization 호출.
        3. ValueError 계열(OrganizationNotFoundError / DuplicateOrganizationNameError /
           OrganizationInvalidMoveError) → flash error redirect.
        4. 성공 → flash success redirect (PRG 패턴).

    Args:
        org_id:        이동할 조직 PK (URL 경로 파라미터).
        new_parent_id: 새 부모 조직 PK (Form). 빈 문자열/0 은 최상위 이동.
    """
    # HTML select 에서 value="" 로 최상위를 표현하므로 0 또는 None 모두 최상위.
    normalized_parent_id = new_parent_id if new_parent_id else None

    session = SessionLocal()
    try:
        try:
            move_organization(session, org_id, normalized_parent_id)
            session.commit()
        except ValueError as exc:
            session.rollback()
            logger.info(
                "조직 이동 거부: 관리자={} org_id={} new_parent_id={} ({})",
                current_user.username, org_id, normalized_parent_id, exc,
            )
            return RedirectResponse(
                url=_org_flash_url(str(exc), level="error"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
    finally:
        session.close()

    parent_label = f"id={normalized_parent_id}" if normalized_parent_id else "루트(최상위)"
    logger.info(
        "조직 이동 완료: 관리자={} org_id={} new_parent_id={}",
        current_user.username, org_id, normalized_parent_id,
    )
    return RedirectResponse(
        url=_org_flash_url(f"조직이 '{parent_label}' 아래로 이동되었습니다.", level="success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ──────────────────────────────────────────────────────────────
# [사용자 관리] 탭 (task 00049-4)
# ──────────────────────────────────────────────────────────────


def _users_flash_url(message: str, *, level: str) -> str:
    """/admin/users?flash=...&flash_level=... redirect URL 빌더."""
    from urllib.parse import urlencode

    query = urlencode({"flash": message, "flash_level": level})
    return f"/admin/users?{query}"


def _apply_admin_session_cookie(response: Response, session_id: str) -> None:
    """응답에 세션 쿠키를 설정한다.

    관리자가 자신의 비밀번호를 변경했을 때 새 세션 쿠키를 즉시 발급하기 위해 사용한다.
    auth.routes 와 settings.py 의 _apply_session_cookie 와 동일한 로직이다.
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


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    flash: Optional[str] = Query(default=None),
    flash_level: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[사용자 관리] 탭 — 전체 사용자 목록 + 비밀번호 변경 / 조직 변경 / 계정 삭제 UI.

    쿼리 파라미터:
        flash:       POST 후 redirect 시 안내 메시지. 없으면 비노출.
        flash_level: 'success' / 'error'. 기본 'success'.

    템플릿 컨텍스트:
        top_tab:       'org_group'.
        sub_tab:       'users'.
        users:         User 인스턴스 목록 (id ASC).
        org_tree:      build_organization_tree 반환 중첩 dict 목록.
        org_name_map:  {org_id: name} 조직 이름 조회 맵.
        user_org_map:  {user_id: set[org_id]} 사용자별 조직 소속 맵.
        flash / flash_level: 상단 안내 배지.
        current_user:  네비 + 본인 확인 분기용.
    """
    logger.debug("admin.users_page 진입: user_id={}", current_user.id)
    session = SessionLocal()
    try:
        users = session.execute(
            select(User).order_by(User.id)
        ).scalars().all()

        all_orgs = list_all_organizations(session)
        org_tree = build_organization_tree(all_orgs)
        org_name_map: dict[int, str] = {org.id: org.name for org in all_orgs}

        user_org_map: dict[int, set[int]] = {
            user.id: set(get_user_organization_ids(session, user.id))
            for user in users
        }
    finally:
        session.close()

    return _templates.TemplateResponse(
        request,
        "admin/users.html",
        {
            "top_tab": "org_group",
            "sub_tab": "users",
            "users": users,
            "org_tree": org_tree,
            "org_name_map": org_name_map,
            "user_org_map": user_org_map,
            "flash": flash,
            "flash_level": flash_level or "success",
            "current_user": current_user,
        },
    )


@router.post(
    "/users/{user_id}/password",
    dependencies=[Depends(ensure_same_origin)],
)
def users_set_password(
    user_id: int,
    request: Request,
    new_password: str = Form(...),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """관리자가 대상 사용자의 비밀번호를 변경한다.

    처리 순서:
        1. 대상 사용자를 DB 에서 조회 (없으면 flash error redirect).
        2. change_password 호출 — 정책 검증 + 해시 갱신 + 모든 세션 삭제.
        3. 대상이 현재 관리자 본인이면 새 세션 발급 + 쿠키 갱신 (로그인 유지).
        4. 성공 flash redirect. 본인이면 쿠키가 담긴 Response 로 반환.

    사용자 원문 "비밀번호 변경은 개인이 자신의 계정 설정에서 변경한 것과 동일한 효과":
    change_password 는 모든 세션 삭제 + 해시 갱신을 수행하므로 동일 효과.

    Args:
        user_id:      비밀번호를 변경할 사용자 PK (URL 경로 파라미터).
        new_password: 새 비밀번호 평문 (Form).
    """
    session = SessionLocal()
    try:
        target_user = session.get(User, user_id)
        if target_user is None:
            return RedirectResponse(
                url=_users_flash_url(
                    f"사용자를 찾을 수 없습니다: user_id={user_id}", level="error"
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        try:
            change_password(session, target_user, new_password=new_password)
        except PasswordPolicyError as exc:
            session.rollback()
            return RedirectResponse(
                url=_users_flash_url(str(exc), level="error"),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        # 본인 비밀번호 변경 시 새 세션 발급 (기존 세션 삭제됐으므로 쿠키 갱신 필수).
        is_self = user_id == current_user.id
        target_username = target_user.username  # session.close() 이전에 캡처
        new_session_id: str | None = None
        if is_self:
            new_user_session = create_session(session, target_user)
            new_session_id = new_user_session.session_id

        session.commit()
    finally:
        session.close()

    logger.info(
        "관리자 비밀번호 변경: 관리자={} target_user_id={} is_self={}",
        current_user.username, user_id, is_self,
    )
    target_label = f"본인({target_username})" if is_self else target_username
    redirect_response = RedirectResponse(
        url=_users_flash_url(
            f"'{target_label}' 의 비밀번호가 변경되었습니다.", level="success"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    if new_session_id is not None:
        _apply_admin_session_cookie(redirect_response, new_session_id)
    return redirect_response


@router.post(
    "/users/{user_id}/delete",
    dependencies=[Depends(ensure_same_origin)],
)
def users_delete(
    user_id: int,
    current_user: User = Depends(admin_user_required),
) -> Response:
    """관리자가 대상 사용자 계정을 삭제한다.

    제한:
        - 본인(self) 삭제 차단: 관리자가 자신의 계정을 삭제하는 경우를 막는다.
        - 마지막 관리자 삭제 차단: is_admin=True 인 사용자가 1명뿐이면 삭제 거부.

    삭제 효과(delete_user 서비스 레이어 동일):
        - user_sessions / announcement_user_states / relevance_judgments /
          favorite_folders → favorite_entries / user_organizations: ORM cascade.
        - relevance_judgment_history: 명시적 DELETE (SQLite PRAGMA 미설정 환경 보호).

    Args:
        user_id: 삭제할 사용자 PK (URL 경로 파라미터).
    """
    # 본인 삭제 차단
    if user_id == current_user.id:
        return RedirectResponse(
            url=_users_flash_url("본인 계정은 삭제할 수 없습니다.", level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    session = SessionLocal()
    try:
        target_user = session.get(User, user_id)
        if target_user is None:
            return RedirectResponse(
                url=_users_flash_url(
                    f"사용자를 찾을 수 없습니다: user_id={user_id}", level="error"
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        # 마지막 관리자 삭제 차단
        if target_user.is_admin:
            admin_count = session.execute(
                select(User).where(User.is_admin.is_(True))
            ).scalars().all()
            if len(admin_count) <= 1:
                return RedirectResponse(
                    url=_users_flash_url(
                        f"마지막 관리자({target_user.username})는 삭제할 수 없습니다.",
                        level="error",
                    ),
                    status_code=status.HTTP_303_SEE_OTHER,
                )

        target_username = target_user.username
        deleted = delete_user(session, user_id)
        if not deleted:
            return RedirectResponse(
                url=_users_flash_url(
                    f"사용자 삭제에 실패했습니다: user_id={user_id}", level="error"
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        session.commit()
    finally:
        session.close()

    logger.info(
        "관리자 계정 삭제: 관리자={} deleted_user_id={} deleted_username={!r}",
        current_user.username, user_id, target_username,
    )
    return RedirectResponse(
        url=_users_flash_url(
            f"사용자 '{target_username}' 계정이 삭제되었습니다.", level="success"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/users/{user_id}/organizations",
    dependencies=[Depends(ensure_same_origin)],
)
def users_set_organizations(
    user_id: int,
    organization_ids: list[int] = Form(default=[]),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """관리자가 대상 사용자의 조직 소속을 변경한다.

    organization_ids 가 빈 리스트이면 모든 조직 소속을 해제한다.
    존재하지 않는 organization_id 가 포함된 경우 DB FK 에 의해 IntegrityError 가
    발생해 flash error redirect 한다.

    Args:
        user_id:          조직 소속을 변경할 사용자 PK.
        organization_ids: 새로 소속시킬 조직 PK 목록. 빈 리스트이면 전체 해제.
    """
    from sqlalchemy.exc import IntegrityError

    session = SessionLocal()
    try:
        target_user = session.get(User, user_id)
        if target_user is None:
            return RedirectResponse(
                url=_users_flash_url(
                    f"사용자를 찾을 수 없습니다: user_id={user_id}", level="error"
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        target_username = target_user.username  # session.close() 이전에 캡처
        try:
            set_user_organizations(session, user_id, organization_ids)
            session.commit()
        except IntegrityError:
            session.rollback()
            return RedirectResponse(
                url=_users_flash_url(
                    "유효하지 않은 조직이 포함되어 있습니다.", level="error"
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )
    finally:
        session.close()

    logger.info(
        "관리자 조직 소속 변경: 관리자={} target_user_id={} organization_ids={}",
        current_user.username, user_id, organization_ids,
    )
    return RedirectResponse(
        url=_users_flash_url(
            f"'{target_username}' 의 조직 소속이 변경되었습니다.", level="success"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/users/{user_id}/email",
    dependencies=[Depends(ensure_same_origin)],
)
def users_set_email(
    user_id: int,
    new_email: str = Form(default=""),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """관리자가 대상 사용자의 이메일 주소를 변경한다.

    빈 문자열로 저장하면 해당 사용자의 이메일이 제거(None)된다.
    형식 오류 시 change_email 이 ValueError 를 던지며 flash error redirect 한다.

    Args:
        user_id:   이메일을 변경할 사용자 PK (URL 경로 파라미터).
        new_email: 새 이메일 주소. 빈 문자열이면 이메일 제거(None).
    """
    session = SessionLocal()
    try:
        target_user = session.get(User, user_id)
        if target_user is None:
            return RedirectResponse(
                url=_users_flash_url(
                    f"사용자를 찾을 수 없습니다: user_id={user_id}", level="error"
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        target_username = target_user.username  # session.close() 이전에 캡처
        try:
            change_email(session, target_user, new_email=new_email)
        except ValueError as exc:
            session.rollback()
            return RedirectResponse(
                url=_users_flash_url(str(exc), level="error"),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        session.commit()
    finally:
        session.close()

    logger.info(
        "관리자 이메일 변경: 관리자={} target_user_id={} new_email={!r}",
        current_user.username, user_id, new_email or None,
    )
    return RedirectResponse(
        url=_users_flash_url(
            f"'{target_username}' 의 이메일이 변경되었습니다.", level="success"
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/users/{user_id}/email-subscription",
    dependencies=[Depends(ensure_same_origin)],
)
def users_set_email_subscription(
    user_id: int,
    subscribed: bool = Form(default=False),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """관리자가 대상 사용자의 이메일 수신 여부를 변경한다.

    체크박스 미체크 시 Form 값이 전송되지 않으므로 default=False 로 처리한다.
    Pydantic 은 체크박스 "true" 값을 True 로 강제 변환한다.

    Args:
        user_id:    이메일 수신 여부를 변경할 사용자 PK.
        subscribed: True 이면 수신 동의, False 이면 수신 거부.
    """
    session = SessionLocal()
    try:
        target_user = session.get(User, user_id)
        if target_user is None:
            return RedirectResponse(
                url=_users_flash_url(
                    f"사용자를 찾을 수 없습니다: user_id={user_id}", level="error"
                ),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        target_username = target_user.username  # session.close() 이전에 캡처
        change_email_subscribed(session, target_user, subscribed=subscribed)
        session.commit()
    finally:
        session.close()

    logger.info(
        "관리자 이메일 수신 설정 변경: 관리자={} target_user_id={} subscribed={}",
        current_user.username, user_id, subscribed,
    )
    subscribed_label = "수신 동의" if subscribed else "수신 거부"
    return RedirectResponse(
        url=_users_flash_url(
            f"'{target_username}' 의 이메일 수신 여부가 '{subscribed_label}' 로 변경되었습니다.",
            level="success",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/usage", response_class=HTMLResponse)
def admin_usage_page(
    request: Request,
    ips: Optional[str] = Query(default=None),
    mode: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> HTMLResponse:
    """이용 통계 탭 — 일별 접근 통계 / IP 방문 이력 / 오늘 최근 원본 로그.

    쿼리 파라미터:
        ips:  comma-separated IP 주소 목록 (예: 192.168.1.1,10.0.0.2).
              생략 또는 빈 문자열이면 전체 집계.
        mode: 'include' — ips 에 있는 IP 만 집계.
              'exclude' — ips 에 있는 IP 를 제외하고 집계.
              그 외 값 또는 생략 시 'include' 로 폴백.
    """
    settings = get_settings()
    log_dir = settings.access_log_dir
    gap_minutes = settings.access_history_session_gap_minutes

    # ips 파싱: comma-separated → 공백 제거 → 빈 값/중복 제거
    ip_list: list[str] = []
    if ips:
        seen: set[str] = set()
        for token in ips.split(","):
            cleaned = token.strip()
            if cleaned and cleaned not in seen:
                ip_list.append(cleaned)
                seen.add(cleaned)

    # mode 정규화: 'exclude' 만 허용, 그 외는 'include' 로 폴백
    filter_mode = "exclude" if (mode or "").strip().lower() == "exclude" else "include"

    flash: str | None = None
    flash_level: str | None = None
    daily_stats: list[dict] = []
    ip_history: list[dict] = []
    recent_rows: list[dict] = []

    try:
        all_records = list(filter_records_by_ip(
            iter_log_records_for_days(log_dir, days=7),
            ip_list=ip_list,
            mode=filter_mode,
        ))
        daily_stats = aggregate_daily_stats(all_records, days=7)
        ip_history = aggregate_ip_history(all_records, gap_minutes=gap_minutes)
        recent_rows = get_recent_raw_rows(
            log_dir,
            limit=50,
            ip_list=ip_list,
            filter_mode=filter_mode,
        )
    except Exception as exc:
        logger.warning("이용 통계 로그 집계 실패: {}: {}", type(exc).__name__, exc)
        flash = "로그 파일을 읽는 중 오류가 발생했습니다."
        flash_level = "error"

    return _templates.TemplateResponse(
        request,
        "admin/usage.html",
        {
            "top_tab": "usage_group",
            "sub_tab": "usage",
            "current_user": current_user,
            "daily_stats": daily_stats,
            "ip_history": ip_history,
            "recent_rows": recent_rows,
            "gap_minutes": gap_minutes,
            "flash": flash,
            "flash_level": flash_level,
            # IP 필터 상태 — 템플릿에서 폼 값 복원에 사용
            "filter_ips_raw": ips or "",
            "filter_mode": filter_mode,
        },
    )


# ──────────────────────────────────────────────────────────────
# [시스템 백업] 탭 (task 00094-3)
# ──────────────────────────────────────────────────────────────


def _backup_flash_url(message: str, *, level: str) -> str:
    """/admin/backup?flash=...&flash_level=... redirect URL 빌더."""
    from urllib.parse import urlencode

    query = urlencode({"flash": message, "flash_level": level})
    return f"/admin/backup?{query}"


@router.get("/backup", response_class=HTMLResponse)
def backup_page(
    request: Request,
    flash: Optional[str] = Query(default=None),
    flash_level: Optional[str] = Query(default=None),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[시스템 백업] 탭 — 백업 설정, 수동 실행, 이력, 파일 목록.

    템플릿 컨텍스트:
        top_tab:          'system_group'.
        sub_tab:          'backup'.
        backup_cron:      현재 저장된 cron 표현식 (cron 데몬이 실행).
        backup_max_count: 현재 저장된 최대 보관 개수 (str).
        history:          BackupHistory 리스트 (최신 먼저).
        backup_files:     list[dict] — filename, size_bytes, modified_at.
        flash / flash_level: 상단 안내 배지.
        current_user:     네비 분기용.
    """
    logger.debug("admin.backup_page 진입: user_id={}", current_user.id)
    with session_scope() as session:
        settings = get_backup_settings(session)
        history = list_backup_history(session, limit=20)

    backup_files = list_backup_files()

    return _templates.TemplateResponse(
        request,
        "admin/backup.html",
        {
            "top_tab": "system_group",
            "sub_tab": "backup",
            "backup_cron": settings["cron_expression"],
            "backup_max_count": settings["max_count"],
            "history": history,
            "backup_files": backup_files,
            "flash": flash,
            "flash_level": flash_level or "success",
            "current_user": current_user,
        },
    )


@router.post("/backup/settings", dependencies=[Depends(ensure_same_origin)])
def backup_settings_save(
    request: Request,
    cron_expression: str = Form(...),
    max_count: int = Form(...),
    current_user: User = Depends(admin_user_required),
) -> Response:
    """백업 설정(cron 표현식·보관 개수)을 저장하고 crontab 을 재설치한다.

    Form 필드:
        cron_expression: 5-필드 cron 표현식.
        max_count:       보관할 최대 백업 그룹 수 (1 이상).

    흐름:
        1. max_count 최솟값 검증 (< 1 이면 flash error redirect).
        2. cron 표현식 검증(필드 개수 + 값 범위). 위반 시 flash error redirect.
        3. 백업 cron 트리거는 SSOT(scheduled_jobs 의 backup 싱글턴)에 저장하고,
           비-스케줄 설정인 max_count 만 system_settings 에 저장한다.
        4. 저장 커밋 후 crontab 재설치(컨테이너에서만 실제 설치). cron 데몬이
           backup 싱글턴 cron 시각에 ``python -m app.scheduler.run_job backup``
           을 호출한다.
        5. 성공 → PRG 패턴(303) /admin/backup.
    """
    if max_count < 1:
        return RedirectResponse(
            url=_backup_flash_url("보관 개수는 1 이상이어야 합니다.", level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        validate_cron_expression(cron_expression)
    except CronExpressionError as exc:
        logger.info(
            "백업 설정 저장 거부 — cron 표현식 검증 실패: 관리자={} ({})",
            current_user.username, exc,
        )
        return RedirectResponse(
            url=_backup_flash_url(str(exc), level="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    with session_scope() as session:
        # 백업 cron 트리거는 단일 SSOT(scheduled_jobs)에 기록한다. crontab 생성기는
        # 이 backup 싱글턴 row 만 읽으므로, 외부 파일/ system_settings 미러 없이
        # DB 만으로 기동 시 백업 스케줄이 복원된다.
        upsert_singleton_schedule(
            session, job_kind=JOB_KIND_BACKUP, cron_expression=cron_expression
        )
        # 보관 개수는 비-스케줄 설정이라 system_settings 에 그대로 둔다.
        set_backup_setting(session, SETTING_KEY_BACKUP_MAX_COUNT, str(max_count))

    # 저장이 커밋된 뒤 crontab 을 재설치한다(새 백업 cron 이 반영되도록).
    _reinstall_crontab_quietly()

    logger.info(
        "백업 설정 저장: 관리자={} cron={!r} max_count={}",
        current_user.username, cron_expression, max_count,
    )
    return RedirectResponse(
        url=_backup_flash_url(
            f"설정이 저장되었습니다 (cron={cron_expression}, 보관={max_count}개).",
            level="success",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/backup/run", dependencies=[Depends(ensure_same_origin)])
def backup_run_manual(
    request: Request,
    current_user: User = Depends(admin_user_required),
) -> Response:
    """백업을 즉시 수동으로 실행한다.

    흐름:
        1. run_backup(trigger='manual') 호출.
        2. 예기치 못한 예외 → flash error redirect.
        3. history.success=False → 오류 메시지 flash error redirect.
        4. 성공 → 파일 목록 포함 flash success redirect (PRG 패턴).
    """
    try:
        history = run_backup(trigger=BACKUP_TRIGGER_MANUAL)
    except Exception as exc:
        logger.exception(
            "수동 백업 실패: 관리자={} ({}: {})",
            current_user.username, type(exc).__name__, exc,
        )
        return RedirectResponse(
            url=_backup_flash_url(
                f"백업 실패({type(exc).__name__}): {exc}", level="error"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if history.success:
        files_str = ", ".join(history.backup_files) if history.backup_files else "(없음)"
        message = (
            f"백업 완료 (파일: {files_str}, {history.duration_seconds}초)."
        )
        level = "success"
    else:
        message = f"백업 중 오류 발생: {history.error_message}"
        level = "error"

    logger.info(
        "수동 백업 완료: 관리자={} success={} files={}",
        current_user.username, history.success, history.backup_files,
    )
    return RedirectResponse(
        url=_backup_flash_url(message, level=level),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ──────────────────────────────────────────────────────────────
# [시스템 재시작] 탭 (task 00161, task 00162)
# ──────────────────────────────────────────────────────────────

# task 00162 — 재시작 버튼 하단에 표시할 시스템 기동 이력 건수. 사용자 원문의
# '최근 30건' 요구를 그대로 반영한다(메일 발송 이력과 유사한 최근 N건 표시).
SYSTEM_RESTART_HISTORY_LIMIT: int = 30


@router.get("/system/restart", response_class=HTMLResponse)
def system_restart_page(
    request: Request,
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[시스템 재시작] 탭 — iris-agent-web 컨테이너 셀프 재시작 페이지.

    backup_page 와 같은 패턴으로 ``top_tab='system_group'`` + ``sub_tab='restart'``
    컨텍스트를 주입해 admin/base.html 의 sub-nav 가 [시스템 재시작] 탭을 active 로
    표시하도록 한다. 페이지 진입 시점에 ``get_running_scrape_run`` 으로 진행중
    수집 여부를 조회해 ``scrape_running`` boolean 으로 전달한다 — 템플릿이 서버
    렌더 시점부터 재시작 버튼 비활성화/경고 배너를 반영할 수 있게 한다(재시작 시
    진행중 scrape 가 끊기고, 다음 부팅의 ``cleanup_stale_running_runs`` 가 failed
    로 정리하기 때문).

    실제 재시작 트리거는 페이지의 ``admin_restart.js`` 가 POST /admin/system/restart
    (00161-1 endpoint)를 호출해 처리하며, 본 라우트는 SSR 골격만 그린다.

    task 00162 — 재시작 버튼 하단에 표시할 '시스템 기동 이력' 을
    ``read_recent_startup_events(limit=30)`` 으로 조회해 ``startup_events`` 컨텍스트
    로 전달한다. 이력은 system_restart.log(파일 기반 SSOT)에서 읽으며, 일반 기동
    (startup)과 UI 재시작(restart_via_ui)을 구분해 보여 준다. 재시작 직후 페이지가
    자동 새로고침되므로 별도 폴링 endpoint 없이 SSR 컨텍스트만으로 최신화된다.

    Args:
        request: FastAPI 요청 객체 (Jinja2 TemplateResponse 용).
        current_user: 라우터 dependency 에서 이미 검증된 admin user. 상단 네비
            분기에 사용된다.

    Returns:
        ``admin/restart.html`` 을 렌더한 HTMLResponse.
    """
    logger.debug("admin.system_restart_page 진입: user_id={}", current_user.id)
    with session_scope() as session:
        running_row = get_running_scrape_run(session)
        running_scrape_run_id = running_row.id if running_row is not None else None
    scrape_running = running_scrape_run_id is not None

    # task 00162 — 최근 30건 기동 이력. read_recent_startup_events 는 파일 부재/
    # 깨진 라인에서도 예외 없이 빈 리스트/부분 결과를 돌려주므로 페이지가 500 으로
    # 깨지지 않는다.
    startup_events = read_recent_startup_events(limit=SYSTEM_RESTART_HISTORY_LIMIT)

    return _templates.TemplateResponse(
        request,
        "admin/restart.html",
        {
            "top_tab": "system_group",
            "sub_tab": "restart",
            "scrape_running": scrape_running,
            "running_scrape_run_id": running_scrape_run_id,
            "startup_events": startup_events,
            "current_user": current_user,
        },
    )


@router.post("/system/restart", dependencies=[Depends(ensure_same_origin)])
def system_restart(
    request: Request,
    current_user: User = Depends(admin_user_required),
) -> JSONResponse:
    """iris-agent-web 컨테이너를 셀프 재시작한다(A 방식 — docker.sock).

    detached subprocess 로 ``sleep 1 && docker restart <container>`` 를 띄운
    뒤 **즉시** HTTP 200 을 반환한다. ``subprocess.Popen`` 은 non-blocking 이라
    명령 완료를 기다리지 않으며, 1초 sleep 동안 이 200 응답이 브라우저로 flush
    된다. 실제 재시작은 docker daemon 이 수행하므로 호출 client(자기 자신)가
    죽어도 완료된다.

    진행중 수집 가드:
        프론트(confirm)와 별개로 서버에서도 ``get_running_scrape_run`` 으로
        running 여부를 한 번 더 조회해 응답(``scrape_running`` / ``running_scrape_run_id``)
        에 실어 준다. 여기서 강제 차단하지는 않는다 — 최종 판단(경고 후 강행)은
        프론트와 일관되게 둔다. 재시작 시 진행중 scrape 는 끊기고, 다음 부팅의
        ``cleanup_stale_running_runs`` 가 failed 로 정리한다.

    Returns:
        ``{\"status\": \"restarting\", \"container\": ..., \"pid\": ...,
        \"scrape_running\": bool, \"running_scrape_run_id\": int | null,
        \"log_path\": ...}`` 200 응답.
    """
    # 서버 측 진행중 수집 확인 — 프론트가 경고/표시에 활용하도록 응답에 싣는다.
    with session_scope() as session:
        running_row = get_running_scrape_run(session)
        running_scrape_run_id = running_row.id if running_row is not None else None
    scrape_running = running_scrape_run_id is not None

    logger.info(
        "관리자 '{}' 가 셀프 재시작을 트리거했습니다 (scrape_running={} running_scrape_run_id={}).",
        current_user.username,
        scrape_running,
        running_scrape_run_id,
    )

    try:
        result = trigger_self_restart()
    except ComposeEnvironmentError as exc:
        # docker CLI 바이너리를 못 찾는 경우 — 운영자가 환경을 고쳐야 한다.
        # 셀프 재시작은 시작도 못 했으므로 500 으로 명확히 실패를 알린다.
        logger.error(
            "셀프 재시작 실패(관리자 '{}'): docker CLI 해석 실패 — {}",
            current_user.username,
            exc,
        )
        return JSONResponse(
            {"status": "error", "detail": str(exc)},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except OSError as exc:
        logger.exception(
            "셀프 재시작 subprocess 기동 실패(관리자 '{}'): {}: {}",
            current_user.username,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            {"status": "error", "detail": f"{type(exc).__name__}: {exc}"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return JSONResponse(
        {
            "status": "restarting",
            "container": result.container,
            "pid": result.pid,
            "scrape_running": scrape_running,
            "running_scrape_run_id": running_scrape_run_id,
            "log_path": result.log_path,
        }
    )


# ──────────────────────────────────────────────────────────────
# [메일 발송] 탭 (Phase A-1 / task 00104-11)
# ──────────────────────────────────────────────────────────────


@router.get("/email", response_class=HTMLResponse)
def email_page(
    request: Request,
    current_user: User = Depends(admin_user_required),
) -> Response:
    """[메일 발송] 탭 HTML 페이지 — 메일 설정 form + (후속 subtask 의 테스트 발송/이력).

    본 라우트는 SSR 골격만 그리며, 실제 데이터 로드 / 저장은 페이지의
    ``admin_email.js`` 가 ``/api/admin/email/*`` JSON API 를 fetch 해 처리한다.
    backup_page 와 같은 패턴으로 ``top_tab='system_group'`` + ``sub_tab='email'``
    컨텍스트만 주입해 admin/base.html 의 sub-nav 가 [메일 발송] 탭을 active 로
    표시하도록 한다.

    Args:
        request: FastAPI 요청 객체 (Jinja2 TemplateResponse 용).
        current_user: 라우터 dependency 에서 이미 검증된 admin user. 템플릿
            상단 네비 분기에 사용된다.

    Returns:
        ``admin/email.html`` 을 렌더한 HTMLResponse.
    """
    logger.debug("admin.email_page 진입: user_id={}", current_user.id)
    return _templates.TemplateResponse(
        request,
        "admin/email.html",
        {
            "top_tab": "system_group",
            "sub_tab": "email",
            "current_user": current_user,
        },
    )


__all__ = [
    "LOG_FILE_MAX_BYTES",
    "RECENT_RUN_LIMIT",
    "RECENT_RUN_PAGE_SIZE",
    "router",
]
