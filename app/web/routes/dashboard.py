"""대시보드 페이지 + 가용 snapshot 날짜 API 라우터 (Phase 5b / task 00042-2).

본 라우터는 Phase 5b 의 GET 전용 엔드포인트만 다룬다 (사용자 원문 "POST
엔드포인트 없음 — 대시보드는 read-only").

엔드포인트:
    GET /dashboard                       대시보드 HTML 페이지 (비로그인 가능)
    GET /dashboard/api/snapshot-dates    가용 snapshot_date 목록 JSON

라우터 분리 근거:
    - app/web/main.py 의 index_page / detail_page / favorites_page 는 한 파일에
      모여 있지만, 5b 는 후속 subtask 들 (00042-3 A 섹션 / 00042-4 B 섹션 /
      00042-5 위젯 / 00042-6 차트) 에서 같은 페이지의 컨텍스트를 계속 확장한다.
      한 모듈에 모든 dashboard 코드를 모아 두면 후속 subtask 의 diff 를 한
      파일에 집중시킬 수 있다.
    - app/web/routes/admin 패턴과 동일하게 Jinja2Templates 를 모듈 수준에 두고
      register_kst_filters 를 import 시점에 호출해 KST 필터를 등록한다 (다른
      Jinja2Templates 인스턴스와 분리되어 있어 별도 등록 필요 — task 00040-3
      컨벤션).

비로그인 / 로그인 분기 (사용자 원문):
    - current_user_optional Depends — 비로그인은 None, 로그인은 User.
    - 본 subtask 는 페이지 골격만 담당. 위젯 영역은 후속 subtask (00042-5) 가
      current_user 분기로 채운다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user_optional
from app.db.models import User
from app.db.repository import list_available_snapshot_dates
from app.db.session import SessionLocal
from app.timezone import now_kst
from app.web.dashboard_compare import (
    COMPARE_MODE_VALUES,
    CompareMode,
    resolve_compare_range,
)
from app.web.dashboard_section_a import build_section_a
from app.web.template_filters import register_kst_filters

# ──────────────────────────────────────────────────────────────
# 템플릿 인스턴스 — Jinja2Templates 는 라우터 모듈마다 따로 두는 컨벤션이다.
# 같은 base.html 을 공유하므로 templates 디렉터리는 app/web/templates 로
# main.py / admin.py 와 동일하다.
# ──────────────────────────────────────────────────────────────

_DASHBOARD_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "templates"
_templates: Jinja2Templates = Jinja2Templates(directory=str(_DASHBOARD_TEMPLATES_DIR))
# task 00040-3 — KST 표시 필터 (kst_format / kst_date) 등록.
# 본 라우터의 모든 timestamp 표시는 이 필터를 거친다 (사용자 원문 컨벤션).
register_kst_filters(_templates)


# ──────────────────────────────────────────────────────────────
# 라우터
# ──────────────────────────────────────────────────────────────

router = APIRouter(tags=["dashboard"])


# ──────────────────────────────────────────────────────────────
# 의존성 — 요청 단위 DB 세션
# ──────────────────────────────────────────────────────────────


def _get_dashboard_session() -> Iterator[Session]:
    """요청 단위 DB 세션 의존성.

    app.web.main.get_session / app.auth.dependencies._auth_db_session 과
    동일 패턴 — 라우터 모듈 간 순환 import 를 피하려고 자체 의존성을 둔다.
    같은 요청에서 여러 의존성이 호출돼 세션이 2개 만들어질 수도 있지만 모두
    PK lookup / 단일 query 수준이라 실무상 문제 없다.
    """
    session = SessionLocal()
    logger.debug("dashboard DB 세션 open")
    try:
        yield session
    finally:
        session.close()
        logger.debug("dashboard DB 세션 close")


# ──────────────────────────────────────────────────────────────
# 쿼리 파라미터 정규화 헬퍼
# ──────────────────────────────────────────────────────────────


def _coerce_compare_mode(raw_value: Optional[str]) -> CompareMode:
    """쿼리 compare_mode 값을 CompareMode 로 변환한다.

    허용 입력:
        - None / 빈 문자열 → CompareMode.PREV_DAY (기본값).
        - COMPARE_MODE_VALUES 중 하나.

    그 외 값은 사용자 원문 검증 가이드의 'compare_mode 값 검증 필수(허용 값
    외 400)' 를 만족하기 위해 HTTPException(400) 으로 거절한다.

    Args:
        raw_value: 쿼리 문자열의 compare_mode 값 (또는 None).

    Returns:
        정규화된 CompareMode 인스턴스.

    Raises:
        HTTPException(400): 허용 5종 외 값을 받았을 때.
    """
    if raw_value is None or not raw_value.strip():
        return CompareMode.PREV_DAY
    stripped = raw_value.strip()
    try:
        return CompareMode(stripped)
    except ValueError as exc:
        allowed_list = ", ".join(COMPARE_MODE_VALUES)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"알 수 없는 compare_mode: {stripped!r}. 허용: {allowed_list}",
        ) from exc


def _parse_iso_date(
    raw_value: Optional[str], *, fallback: date | None
) -> date | None:
    """ISO 'YYYY-MM-DD' 문자열을 date 로 파싱한다.

    파싱 실패 / None / 빈 문자열은 fallback 을 반환한다 — 사용자 원문 '기준일
    default = 오늘 KST' 와 'compare_date 형식 오류는 400' 을 한 함수의 호출
    인자로 분기한다 (호출자가 fallback=None 을 넘기면 호출자가 명시적으로 None
    처리, fallback=today 면 today 로 대체).

    Args:
        raw_value: 쿼리 문자열 (또는 None).
        fallback:  파싱 실패 시 반환할 date 또는 None.

    Returns:
        파싱된 date 또는 fallback.
    """
    if raw_value is None:
        return fallback
    stripped = raw_value.strip()
    if not stripped:
        return fallback
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        return fallback


# ──────────────────────────────────────────────────────────────
# GET /dashboard/api/snapshot-dates — 라우트 등록 순서 주의
# ──────────────────────────────────────────────────────────────
# FastAPI 의 path matching 은 등록 순서대로이며 정적 prefix 가 동적 라우트보다
# 먼저 등록되면 충돌 가능성이 줄어든다. 본 모듈은 /dashboard/api/... 를
# /dashboard 보다 먼저 정의해 'api' 가 path 의 일부로 잘못 매칭되는 일을 미연
# 에 방지한다 (현재 /dashboard 는 path 변수가 없어 충돌 위험은 사실상 없지만
# 향후 확장 안전망).


@router.get("/dashboard/api/snapshot-dates", response_class=JSONResponse)
def get_snapshot_dates(
    session: Session = Depends(_get_dashboard_session),
    current_user: User | None = Depends(current_user_optional),
) -> dict[str, list[str]]:
    """가용 snapshot_date 목록을 ISO 문자열 list 로 반환한다.

    캘린더 컴포넌트가 페이지 초기 로딩 시 한 번 fetch 하여 set 으로 보관한다.
    기준일 캘린더 / 비교일 캘린더 두 인스턴스가 같은 set 을 공유한다 (사용자
    원문 'GET /dashboard/api/snapshot-dates: 가용 snapshot_date 목록 (KST 날짜
    ISO 문자열 배열) JSON 반환').

    가용 날짜 판정 정책 (docs/dashboard_design.md §4.1):
        snapshot_date 의 존재 여부 한 가지로 결정. '수집은 됐지만 변화 0건' 인
        날도 5a 의 upsert_scrape_snapshot 이 row 를 INSERT 하므로 결과에 포함
        된다 — 사용자 원문 modify v2 의 가정과는 반대지만 코드와 정합되는
        디자인 의도다 (§2 / §4.1 참조).

    비로그인도 호출 가능 (current_user_optional) — 캘린더 자체가 비로그인
    경로의 컨트롤이라 인증을 요구하지 않는다.

    Args:
        session:      요청 단위 DB 세션.
        current_user: 로그인된 User 또는 None. 본 엔드포인트는 응답 형식을
                      바꾸지 않지만, observability 미들웨어가 user_id 를 찍을
                      수 있도록 의존성을 주입해 둔다.

    Returns:
        {"dates": ["2026-04-15", "2026-04-16", ...]}. 빈 set 은 {"dates": []}.
    """
    snapshot_dates = list_available_snapshot_dates(session)
    # date.isoformat() 은 'YYYY-MM-DD' 를 반환 — guidance 'snapshot_date 는 KST
    # date 이므로 ISO 변환 시 dateobj.isoformat() 그대로' 그대로.
    return {"dates": [d.isoformat() for d in snapshot_dates]}


# ──────────────────────────────────────────────────────────────
# GET /dashboard — 대시보드 HTML 페이지 (비로그인 가능)
# ──────────────────────────────────────────────────────────────


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    base_date_str: Optional[str] = Query(default=None, alias="base_date"),
    compare_mode_raw: Optional[str] = Query(default=None, alias="compare_mode"),
    compare_date_str: Optional[str] = Query(default=None, alias="compare_date"),
    session: Session = Depends(_get_dashboard_session),
    current_user: User | None = Depends(current_user_optional),
) -> HTMLResponse:
    """대시보드 HTML 페이지 (비로그인 가능).

    쿼리 파라미터 (모두 optional):
        - base_date:    KST date ISO ('YYYY-MM-DD'). 미지정/파싱 실패 시
                        now_kst().date() 로 대체. 사용자 원문 '기준일 캘린더
                        (default = 오늘 KST)'.
        - compare_mode: prev_day / prev_week / prev_month / prev_year / custom.
                        미지정 시 prev_day. 그 외 값은 400 (사용자 원문 검증).
        - compare_date: compare_mode == 'custom' 일 때만 의미. ISO 문자열.
                        CUSTOM 인데 미지정/파싱 실패면 400.

    본 subtask (00042-2) 는 페이지 골격만 담당한다 — A 섹션 누적 머지 / B
    섹션 활성 공고 / 위젯 카운트 / 추이 차트는 모두 후속 subtask (00042-3 ~
    00042-6) 에서 본 라우트의 컨텍스트를 확장하며 채운다. 본 attempt 의
    템플릿은 컨트롤 영역만 실제 렌더하고 나머지 4개 섹션은 placeholder block
    으로 비워 둔다 (templates/dashboard.html 참조).

    Args:
        request:           FastAPI Request (Jinja2Templates.TemplateResponse 필수).
        base_date_str:     쿼리 base_date 원문 문자열.
        compare_mode_raw:  쿼리 compare_mode 원문 문자열.
        compare_date_str:  쿼리 compare_date 원문 문자열.
        session:           요청 단위 DB 세션.
        current_user:      로그인된 User 또는 None.

    Returns:
        HTMLResponse — dashboard.html 렌더 결과.
    """
    # ── (1) base_date 정규화 ─────────────────────────────────────────────
    # 사용자 원문 'base_date 는 사용자가 캘린더에서 가용 날짜만 선택하므로 라우트
    # 는 그대로 받되 가용성 자체는 fallback 적용 없이 통과(기준일 fallback 불필요)'.
    # 외부에서 직접 URL 을 친 경우에도 페이지가 뜨도록 파싱 실패 / 미지정은
    # 오늘로 fallback 한다.
    today_kst: date = now_kst().date()
    base_date_parsed = _parse_iso_date(base_date_str, fallback=today_kst)
    # _parse_iso_date 는 fallback=date 를 받으면 date 를 보장한다 (None 가능성 0).
    assert base_date_parsed is not None
    base_date: date = base_date_parsed

    # ── (2) compare_mode 검증 ────────────────────────────────────────────
    compare_mode = _coerce_compare_mode(compare_mode_raw)

    # ── (3) compare_date 정규화 (CUSTOM 일 때만 필수) ───────────────────
    # CUSTOM 이 아닌 mode 에서는 사용자 원문 'compare_date 는 custom 모드에서만
    # 필수, 그 외엔 무시' 그대로 — None 으로 둔다.
    compare_date: date | None = None
    if compare_mode is CompareMode.CUSTOM:
        if compare_date_str is None or not compare_date_str.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="compare_mode='custom' 일 때 compare_date 는 필수입니다.",
            )
        compare_date = _parse_iso_date(compare_date_str, fallback=None)
        if compare_date is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"compare_date 형식이 올바르지 않습니다: {compare_date_str!r}",
            )

    # ── (4) (from, to) 산출 ───────────────────────────────────────────────
    compare_range = resolve_compare_range(
        base_date=base_date,
        compare_mode=compare_mode,
        compare_date=compare_date,
    )

    # ── (5) 가용 snapshot_date set — 캘린더 초기 강조용 ───────────────────
    # 페이지 초기 렌더 시점에 한 번 사전 계산해 템플릿에 dict 로 전달한다.
    # 클라이언트 JS 가 같은 데이터를 GET /dashboard/api/snapshot-dates 로 다시
    # fetch 해도 되지만, 첫 렌더에서 깜빡임 없이 활성/비활성 표시가 잡히도록
    # 서버 사전 계산 결과를 함께 임베드한다.
    available_snapshot_dates = list_available_snapshot_dates(session)
    available_snapshot_date_iso = [d.isoformat() for d in available_snapshot_dates]

    # ── (5b) A 섹션 — (from, to] 누적 머지 + 5종 카드 + expand + fallback ──
    # task 00042-3. compare_range.from_date 가 비교일 (사용자가 캘린더에서 고른
    # 날짜 또는 드롭다운 산출 결과) — fallback 정책은 build_section_a 가 처리.
    section_a_data = build_section_a(
        session,
        base_date=base_date,
        requested_compare_date=compare_range.from_date,
    )

    # ── (6) 디버그 로그 ──────────────────────────────────────────────────
    # 사용자 원문 검증 16 ('비로그인 시 위젯 쿼리 자체 skip') 회귀를 후속
    # subtask 에서 가시화하려고 본 라우트 입구에 한 줄 DEBUG 로그를 둔다.
    # 본 subtask 는 위젯 자체가 없지만 로그는 미리 박아 두어 후속 attempt 에서
    # '비로그인 → 위젯 skip' 이 같은 라우트 로그 라인 옆에 붙도록 한다.
    logger.debug(
        "dashboard 진입: user={} base_date={} mode={} compare_date={} from={} to={}",
        getattr(current_user, "id", None),
        base_date.isoformat(),
        compare_mode.value,
        compare_date.isoformat() if compare_date is not None else None,
        compare_range.from_date.isoformat(),
        compare_range.to_date.isoformat(),
    )

    return _templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            # 컨트롤 영역 / 캘린더가 사용
            "base_date": base_date,
            "base_date_iso": base_date.isoformat(),
            "compare_mode": compare_mode.value,
            "compare_mode_values": COMPARE_MODE_VALUES,
            "compare_date": compare_date,
            "compare_date_iso": compare_date.isoformat() if compare_date is not None else None,
            "from_date": compare_range.from_date,
            "from_date_iso": compare_range.from_date.isoformat(),
            "to_date": compare_range.to_date,
            "to_date_iso": compare_range.to_date.isoformat(),
            # 캘린더 활성 날짜 set (서버 사전 계산 — 첫 렌더 깜빡임 방지).
            "available_snapshot_date_iso": available_snapshot_date_iso,
            # base.html 의 네비 분기에 필요 — 다른 페이지 라우트 동일 컨벤션.
            "current_user": current_user,
            # 후속 subtask 들이 채울 자리 — placeholder None 으로 미리 둔다.
            # 템플릿에서는 {% if widgets %} 같은 truthy 체크로 통째 skip.
            "widgets": None,             # 00042-5 가 채움
            # task 00042-3 — A 섹션 컨텍스트.
            "section_a": section_a_data,
            "section_b": None,           # 00042-4 가 채움
            "trend_chart": None,         # 00042-6 가 채움
        },
    )


__all__ = ["router"]
