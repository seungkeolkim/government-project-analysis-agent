"""관리자 페이지 「메일 설정/테스트 발송/발송 이력」 JSON API 라우터 (Phase A-1 / task 00104-9).

설계 근거:
    docs/phase_a1_design_note.md §1-4 (API path prefix `/api/admin/email/*` 채택),
    §4-3 (client_secret mask 규칙), §4-4 (send_with_retry 트랜잭션 경계),
    §11 (옵션 A 코드 금지) + 첨부 phase_a1_prompt.md \"API endpoints\" 섹션.

엔드포인트 4 개:
    GET  /api/admin/email/settings    → 7 개 SystemSetting + client_secret_masked
    PUT  /api/admin/email/settings    → 사용자 입력으로 SystemSetting 갱신
    POST /api/admin/email/test-send   → 본인 메일로 plain text 테스트 발송
    GET  /api/admin/email/send-runs   → 최근 발송 이력 (status 필터 + limit)

보호:
    라우터 레벨 ``dependencies=[Depends(admin_user_required)]`` 로 4 개 endpoint
    전부 admin-only 고정. 비로그인 → 401 (current_user_required 가 먼저 걸림),
    비관리자 → 403. 본 라우터의 prefix 가 ``/api/admin/email`` 이므로 기존
    ``/admin/*`` HTML 라우트와 path 충돌이 없다.

HTML 페이지 / frontend 는 본 subtask 의 책임 밖이며 (subtask 00104-11 ~ 13 가
담당), 본 모듈은 JSON API 만 제공한다.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import select

from app.auth.dependencies import admin_user_required, ensure_same_origin
from app.backup.service import get_setting, set_setting
from app.db.models import (
    EmailDailyReportRun,
    EmailSendRun,
    EmailSendRunStatus,
    User,
    as_utc,
)
from app.db.session import session_scope
from app.email.constants import (
    DEFAULT_APP_PUBLIC_BASE_URL,
    DEFAULT_DAILY_REPORT_CRON,
    DEFAULT_DAILY_REPORT_ENABLED,
    DEFAULT_DAILY_REPORT_LAST_SENT_AT,
    DEFAULT_DAILY_REPORT_TEST_RECIPIENT,
    DEFAULT_EMAIL_FROM_DISPLAY_NAME,
    DEFAULT_EMAIL_M365_CLIENT_ID,
    DEFAULT_EMAIL_M365_CLIENT_SECRET,
    DEFAULT_EMAIL_M365_SENDER_ADDRESS,
    DEFAULT_EMAIL_M365_TENANT_ID,
    DEFAULT_EMAIL_MAX_RETRY_COUNT,
    DEFAULT_EMAIL_SEND_ENABLED,
    DEFAULT_EMAIL_TRANSPORT_TYPE,
    RELATED_KIND_DAILY_REPORT,
    RELATED_KIND_TEST_SEND,
    SETTING_KEY_APP_PUBLIC_BASE_URL,
    SETTING_KEY_DAILY_REPORT_CRON,
    SETTING_KEY_DAILY_REPORT_ENABLED,
    SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
    SETTING_KEY_DAILY_REPORT_TEST_RECIPIENT,
    SETTING_KEY_EMAIL_FROM_DISPLAY_NAME,
    SETTING_KEY_EMAIL_M365_CLIENT_ID,
    SETTING_KEY_EMAIL_M365_CLIENT_SECRET,
    SETTING_KEY_EMAIL_M365_SENDER_ADDRESS,
    SETTING_KEY_EMAIL_M365_TENANT_ID,
    SETTING_KEY_EMAIL_MAX_RETRY_COUNT,
    SETTING_KEY_EMAIL_SEND_ENABLED,
    SETTING_KEY_EMAIL_TRANSPORT_TYPE,
)
from app.email.daily_report import (
    TRIGGER_MANUAL_ADMIN,
    TRIGGER_MANUAL_TEST,
    DailyReportRequest,
    collect_recipient_emails,
    prepare_and_send_daily_report,
)
from app.email.gate import EmailSendingDisabledError, is_email_sending_enabled
from app.email.message_builder import build_plain_text_message
from app.email.sender import send_with_retry
from app.email.transport.factory import build_transport_from_settings
from app.scheduler import (
    ScheduleValidationError,
    get_daily_report_schedule_summary,
    register_daily_report_cron_schedule,
)
from app.timezone import KST


# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────


# /send-runs 의 limit query 파라미터 상하한 (첨부 phase_a1_prompt.md spec).
SEND_RUNS_LIMIT_DEFAULT: int = 50
SEND_RUNS_LIMIT_MAX: int = 200

# client_secret mask 규칙 (디자인 노트 §4-3):
#   - NULL / 빈 문자열 → null 응답
#   - 5 자 미만 (≤4) → \"****\" 만 (마지막 4자 노출 시 값 추정 위험)
#   - 5 자 이상 → \"****\" + 마지막 4자
CLIENT_SECRET_MIN_LENGTH_FOR_TAIL: int = 5
CLIENT_SECRET_TAIL_LENGTH: int = 4

# 테스트 발송 성공 안내 문구 (첨부 phase_a1_prompt.md \"POST /test-send\" 섹션).
TEST_SEND_SUCCESS_MESSAGE: str = (
    "발송 성공. 수신함과 정크메일 폴더를 모두 확인해주세요."
)


# ──────────────────────────────────────────────────────────────
# Pydantic schemas — 요청 / 응답 본문
# ──────────────────────────────────────────────────────────────


class M365SettingsOut(BaseModel):
    """GET /settings 의 m365 nested 블록.

    ``client_secret_masked`` 는 마지막 4 자만 노출 ('****abcd' 형식) 또는 빈
    값일 때 ``None``. 자세한 mask 규칙은 ``_mask_client_secret`` docstring.
    """

    tenant_id: str
    client_id: str
    client_secret_masked: str | None
    sender_address: str


class EmailSettingsOut(BaseModel):
    """GET /settings 및 PUT /settings 응답의 최상위 형식."""

    send_enabled: bool
    transport_type: str
    m365: M365SettingsOut
    from_display_name: str
    max_retry_count: int
    public_base_url: str


class M365SettingsIn(BaseModel):
    """PUT /settings 의 m365 nested 입력.

    ``client_secret`` 은 빈 값/없음이면 SystemSetting 의 기존 값 유지
    (첨부 phase_a1_prompt.md 명시).
    """

    tenant_id: str
    client_id: str
    client_secret: str | None = None
    sender_address: str


class EmailSettingsIn(BaseModel):
    """PUT /settings 입력 본문.

    ``max_retry_count`` 의 허용 범위는 첨부 phase_a1_prompt.md form spec 의
    ``min 0 max 5`` 와 정합.
    ``public_base_url`` 은 http:// 또는 https:// 스킴을 포함한 전체 URL 이어야
    한다 (예: http://172.23.10.19:8000/). 스킴 불일치 시 422 로 거부.
    ``send_enabled`` 는 메일 전송 기능 전체 활성화 스위치로 기본값은 False.
    """

    send_enabled: bool = False
    m365: M365SettingsIn
    from_display_name: str
    max_retry_count: int = Field(ge=0, le=5)
    public_base_url: str

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url_scheme(cls, v: str) -> str:
        """공고 상세 URL prefix 의 스킴을 검증한다.

        http:// 또는 https:// 로 시작해야 한다. 그 외 스킴(ftp, 상대경로 등)은
        메일 수신자가 링크를 클릭해도 열리지 않거나 보안 위협이 될 수 있어 거부.
        """
        stripped = v.strip()
        if not stripped.startswith(("http://", "https://")):
            raise ValueError(
                "시스템 접근 주소는 http:// 또는 https:// 로 시작해야 합니다."
            )
        return stripped


class TestSendIn(BaseModel):
    """POST /test-send 입력 본문 — 첨부 spec 의 Pydantic validation 그대로.

    - recipient: ``EmailStr`` (email-validator 가 형식 검증)
    - subject: 1 ≤ len ≤ 200
    - body: 1 ≤ len ≤ 10000
    """

    recipient: EmailStr
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)


# ──────────────────────────────────────────────────────────────
# 라우터
# ──────────────────────────────────────────────────────────────


router = APIRouter(
    prefix="/api/admin/email",
    tags=["admin-email"],
    dependencies=[Depends(admin_user_required)],
)


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _mask_client_secret(raw_value: str | None) -> str | None:
    """client_secret 평문값을 응답용 mask 문자열로 변환한다.

    디자인 노트 §4-3 의 mask 규칙:
        - ``None`` 또는 빈 문자열 → ``None`` (응답 JSON 에서 null)
        - 1~4 자 → ``\"****\"`` (마지막 자릿수 노출 시 추정 위험)
        - 5 자 이상 → ``\"****\" + 마지막 4 자`` (예: ``\"****abcd\"``)

    Args:
        raw_value: SystemSetting 에서 읽은 client_secret 원본 (평문) 또는 None.

    Returns:
        응답에 그대로 넣을 mask 문자열, 또는 빈 값일 때 None.
    """
    if not raw_value:
        return None
    if len(raw_value) < CLIENT_SECRET_MIN_LENGTH_FOR_TAIL:
        return "****"
    return f"****{raw_value[-CLIENT_SECRET_TAIL_LENGTH:]}"


def _read_max_retry_count(session) -> int:
    """SystemSetting 에서 max_retry_count 를 int 로 읽고 fallback 처리.

    backup 도메인 (``app.backup.service._get_max_count_from_db``) 와 동일 방어
    패턴 — SystemSetting.value 는 Text 라 잘못된 값 (예: ``\"abc\"``) 이 들어와
    있어도 ValueError 를 catch 해 default 로 fallback 한다.

    Args:
        session: SystemSetting 조회용 ORM 세션.

    Returns:
        int max_retry_count (0 이상). 잘못된 값/없음이면
        ``DEFAULT_EMAIL_MAX_RETRY_COUNT`` (=2) 반환.
    """
    raw_value = get_setting(session, SETTING_KEY_EMAIL_MAX_RETRY_COUNT)
    if raw_value is None or raw_value == "":
        return DEFAULT_EMAIL_MAX_RETRY_COUNT
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "SystemSetting email.max_retry_count 값이 정수로 파싱 불가 ({!r}). "
            "default {} 로 fallback.",
            raw_value,
            DEFAULT_EMAIL_MAX_RETRY_COUNT,
        )
        return DEFAULT_EMAIL_MAX_RETRY_COUNT
    # 0 이상의 정수만 의미가 있다 — 음수는 default 로 정정.
    if parsed < 0:
        return DEFAULT_EMAIL_MAX_RETRY_COUNT
    return parsed


def _build_settings_response(session) -> EmailSettingsOut:
    """SystemSetting 8 개 값을 읽어 GET 응답 형식으로 직렬화한다.

    ``client_secret`` 은 mask 처리되며, 다른 값은 fallback (빈 row → DEFAULT)
    을 거쳐 그대로 노출된다.
    """
    send_enabled = is_email_sending_enabled(session)
    transport_type = (
        get_setting(session, SETTING_KEY_EMAIL_TRANSPORT_TYPE)
        or DEFAULT_EMAIL_TRANSPORT_TYPE
    )
    tenant_id = (
        get_setting(session, SETTING_KEY_EMAIL_M365_TENANT_ID)
        or DEFAULT_EMAIL_M365_TENANT_ID
    )
    client_id = (
        get_setting(session, SETTING_KEY_EMAIL_M365_CLIENT_ID)
        or DEFAULT_EMAIL_M365_CLIENT_ID
    )
    # client_secret 은 mask 변환을 위해 fallback 전 raw 값을 그대로 읽어야 한다.
    # default ('') 도 결과적으로 None 으로 mask 되므로 의미는 같다.
    client_secret_raw = get_setting(session, SETTING_KEY_EMAIL_M365_CLIENT_SECRET)
    if not client_secret_raw:
        client_secret_raw = DEFAULT_EMAIL_M365_CLIENT_SECRET
    sender_address = (
        get_setting(session, SETTING_KEY_EMAIL_M365_SENDER_ADDRESS)
        or DEFAULT_EMAIL_M365_SENDER_ADDRESS
    )
    from_display_name = (
        get_setting(session, SETTING_KEY_EMAIL_FROM_DISPLAY_NAME)
        or DEFAULT_EMAIL_FROM_DISPLAY_NAME
    )
    max_retry_count = _read_max_retry_count(session)
    public_base_url = (
        get_setting(session, SETTING_KEY_APP_PUBLIC_BASE_URL)
        or DEFAULT_APP_PUBLIC_BASE_URL
    )

    return EmailSettingsOut(
        send_enabled=send_enabled,
        transport_type=transport_type,
        m365=M365SettingsOut(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret_masked=_mask_client_secret(client_secret_raw),
            sender_address=sender_address,
        ),
        from_display_name=from_display_name,
        max_retry_count=max_retry_count,
        public_base_url=public_base_url,
    )


def _serialize_send_run(run: EmailSendRun) -> dict[str, Any]:
    """EmailSendRun 1 row 를 /send-runs 응답용 dict 로 직렬화한다.

    ``body_preview`` 는 응답 슬림화를 위해 제외 (첨부 spec). ``created_at`` /
    ``sent_at`` 은 ISO-8601 문자열 (UTC tz-aware) — 프론트엔드가 KST 표시 시
    JS 측에서 변환한다.

    ``requested_by_username`` 은 ORM relationship 으로 lazy-load (페이지당 50
    row + 같은 admin user 1 명이 대부분이라 N+1 영향 작음). 사용자 탈퇴로
    ``requested_by_user_id`` 가 NULL 인 row 는 ``None`` 반환.
    """
    requested_by_user = run.requested_by
    return {
        "id": run.id,
        "recipient": run.recipient,
        "subject": run.subject,
        "status": run.status.value if run.status is not None else None,
        "attempt_count": run.attempt_count,
        "error_message": run.error_message,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "sent_at": run.sent_at.isoformat() if run.sent_at else None,
        "requested_by_user_id": run.requested_by_user_id,
        "requested_by_username": (
            requested_by_user.username if requested_by_user is not None else None
        ),
        "related_kind": run.related_kind,
        "related_id": run.related_id,
        "transport_type": run.transport_type,
    }


# ──────────────────────────────────────────────────────────────
# 1. GET /api/admin/email/settings
# ──────────────────────────────────────────────────────────────


@router.get("/settings", response_model=EmailSettingsOut)
def get_email_settings(
    current_user: User = Depends(admin_user_required),
) -> EmailSettingsOut:
    """현재 저장된 메일 인프라 설정 7 개 값을 mask 처리해 반환한다.

    SystemSetting row 가 없는 키는 ``app.email.constants`` 의 DEFAULT_* 로
    fallback 한다 (디자인 노트 §2-4). ``client_secret`` 은 항상 mask 형식
    (마지막 4 자만) 또는 ``null`` 로 노출 — 평문은 절대 반환하지 않는다
    (디자인 노트 §4-3).

    Returns:
        7 개 키의 현재 값을 담은 ``EmailSettingsOut``.
    """
    logger.debug("GET /api/admin/email/settings 진입: user_id={}", current_user.id)
    with session_scope() as session:
        return _build_settings_response(session)


# ──────────────────────────────────────────────────────────────
# 2. PUT /api/admin/email/settings
# ──────────────────────────────────────────────────────────────


@router.put(
    "/settings",
    response_model=EmailSettingsOut,
    dependencies=[Depends(ensure_same_origin)],
)
def put_email_settings(
    body: EmailSettingsIn,
    current_user: User = Depends(admin_user_required),
) -> EmailSettingsOut:
    """관리자가 보낸 새 메일 설정 값을 SystemSetting 에 저장한다.

    ``client_secret`` 처리 (디자인 노트 §4-3):
        - body 에 ``client_secret`` 키가 없거나 빈 문자열이면 SystemSetting
          의 기존 값을 유지 (덮어쓰지 않음).
        - 명시적으로 새 값이 들어와야 변경된다.

    ``transport.type`` 키 자체는 본 endpoint 에서 입력으로 받지 않는다 —
    A-1 에서 유효한 값이 ``m365_oauth`` 단 하나라 UI 에서 disabled dropdown
    으로 노출되므로 변경 경로를 막아 두는 게 안전. 향후 옵션 C 추가 시 본
    endpoint 의 schema 에 ``transport_type`` 필드를 더하면 된다.

    Returns:
        변경 적용 후 GET 과 동일 형식의 ``EmailSettingsOut`` 응답.
    """
    logger.info(
        "PUT /api/admin/email/settings 진입: user_id={} from_display_name={!r} "
        "max_retry_count={} sender_address={!r} public_base_url={!r}",
        current_user.id,
        body.from_display_name,
        body.max_retry_count,
        body.m365.sender_address,
        body.public_base_url,
    )

    with session_scope() as session:
        # 메일 전송 기능 활성화 스위치. bool → "true" / "false" (소문자 통일).
        set_setting(
            session,
            SETTING_KEY_EMAIL_SEND_ENABLED,
            "true" if body.send_enabled else "false",
        )
        # M365 자격증명 3 개 + sender_address 저장.
        set_setting(
            session, SETTING_KEY_EMAIL_M365_TENANT_ID, body.m365.tenant_id
        )
        set_setting(
            session, SETTING_KEY_EMAIL_M365_CLIENT_ID, body.m365.client_id
        )
        # client_secret 은 None / 빈 문자열이면 기존 값 유지 (덮어쓰지 않음).
        new_client_secret = body.m365.client_secret
        if new_client_secret is not None and new_client_secret != "":
            set_setting(
                session,
                SETTING_KEY_EMAIL_M365_CLIENT_SECRET,
                new_client_secret,
            )
        set_setting(
            session,
            SETTING_KEY_EMAIL_M365_SENDER_ADDRESS,
            body.m365.sender_address,
        )
        # From 표시명 + 재시도 횟수.
        set_setting(
            session,
            SETTING_KEY_EMAIL_FROM_DISPLAY_NAME,
            body.from_display_name,
        )
        # max_retry_count 는 SystemSetting.value (Text) 에 문자열로 저장.
        set_setting(
            session,
            SETTING_KEY_EMAIL_MAX_RETRY_COUNT,
            str(body.max_retry_count),
        )
        # 공고 상세 링크 생성 시 사용할 시스템 접근 주소 (http/https 포함 전체 URL).
        # Pydantic validator 가 스킴 검증을 이미 통과했으므로 그대로 저장.
        set_setting(
            session,
            SETTING_KEY_APP_PUBLIC_BASE_URL,
            body.public_base_url,
        )

        # 즉시 commit 해서 변경된 값으로 응답 직렬화 — session_scope 는 yield
        # 이후 자동 commit 하지만, 응답 직렬화 단계에서도 동일 세션으로 다시
        # SELECT 하므로 명시적 flush 후 응답 빌드한다.
        session.flush()
        response = _build_settings_response(session)

    logger.info("메일 설정 저장 완료: user_id={}", current_user.id)
    return response


# ──────────────────────────────────────────────────────────────
# 3. POST /api/admin/email/test-send
# ──────────────────────────────────────────────────────────────


@router.post(
    "/test-send",
    dependencies=[Depends(ensure_same_origin)],
)
def post_email_test_send(
    body: TestSendIn,
    current_user: User = Depends(admin_user_required),
) -> dict[str, Any]:
    """관리자가 입력한 recipient/subject/body 로 plain text 테스트 메일을 발송한다.

    흐름:
        1. SystemSetting 에서 ``max_retry_count`` 를 정수로 읽고 fallback.
        2. ``build_transport_from_settings`` 로 EmailTransport 인스턴스 생성.
        3. ``build_plain_text_message`` 로 EmailMessage 생성 (From 헤더는
           transport 가 SystemSetting 값으로 자동 채움).
        4. ``send_with_retry`` 로 발송 시도. 성공/실패 모두 EmailSendRun row 1
           개가 commit 된다.
        5. 성공 시 ``{success: true, send_run_id, message}`` 반환.
        6. 실패 시 HTTP 500 + ``detail`` (예외 클래스명 + 메시지 + send_run_id).

    Returns:
        성공 응답 dict. 실패는 HTTPException 으로 전파.
    """
    logger.info(
        "POST /api/admin/email/test-send 진입: user_id={} recipient={!r}",
        current_user.id,
        body.recipient,
    )

    with session_scope() as session:
        # 1. max_retry_count 읽기 (SystemSetting + fallback)
        max_retry_count = _read_max_retry_count(session)

        # 2. transport 구성 — SystemSetting transport.type 값을 보고 분기. 현재
        #    유효한 값은 'm365_oauth' 단 하나. 미지원 값일 때 ValueError 가
        #    raise 되므로 명시적 catch 해 422 로 변환 — 운영자에게 SystemSetting
        #    입력 오류임을 알린다.
        try:
            transport = build_transport_from_settings(session)
        except ValueError as exc:
            logger.warning(
                "테스트 발송 실패 — transport 구성 오류: user_id={} error={}",
                current_user.id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

        # 3. message 빌더로 plain text EmailMessage 생성. sender=None 으로
        #    두면 transport 가 SystemSetting 값으로 From 헤더를 자동 채움.
        message = build_plain_text_message(
            recipient=body.recipient,
            subject=body.subject,
            body=body.body,
        )

        # 4. send_with_retry 호출. 실패 시 예외가 그대로 raise 되며, 그 시점
        #    에도 EmailSendRun row 는 이미 status='failed' 로 commit 되어 있다.
        try:
            run = send_with_retry(
                transport=transport,
                message=message,
                max_retry_count=max_retry_count,
                related_kind=RELATED_KIND_TEST_SEND,
                related_id=None,
                requested_by_user_id=current_user.id,
                session=session,
            )
        except Exception as exc:
            # 실패 row 가 이미 commit 되어 있으므로, 가장 최근 (=이번) row 의
            # id 를 다시 읽어 응답 detail 에 포함시킨다. send_with_retry 가
            # 예외 전에 PK 를 채워서 commit 했으므로 created_at DESC 1개로
            # 안전하게 매칭된다 (admin 본인의 test-send 가 동시에 여러 건
            # 진행될 가능성은 매우 낮음).
            stmt = (
                select(EmailSendRun)
                .where(
                    EmailSendRun.requested_by_user_id == current_user.id,
                    EmailSendRun.related_kind == RELATED_KIND_TEST_SEND,
                )
                .order_by(EmailSendRun.created_at.desc())
                .limit(1)
            )
            last_run = session.execute(stmt).scalar_one_or_none()
            send_run_id = last_run.id if last_run is not None else None
            logger.warning(
                "테스트 발송 실패: user_id={} send_run_id={} error={}: {}",
                current_user.id,
                send_run_id,
                type(exc).__name__,
                exc,
            )
            # HTTP 500 + 예외 클래스명 / 메시지 / send_run_id 를 detail 에 포함.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"{type(exc).__name__}: {exc} "
                    f"(send_run_id={send_run_id})"
                ),
            ) from exc

        send_run_id = run.id
        logger.info(
            "테스트 발송 성공: user_id={} send_run_id={} attempt_count={}",
            current_user.id,
            send_run_id,
            run.attempt_count,
        )

    return {
        "success": True,
        "send_run_id": send_run_id,
        "message": TEST_SEND_SUCCESS_MESSAGE,
    }


# ──────────────────────────────────────────────────────────────
# 4. GET /api/admin/email/send-runs?limit=50&status=all
# ──────────────────────────────────────────────────────────────


# status 쿼리 파라미터의 허용 값. \"all\" 은 필터 없음을 의미.
_SEND_RUNS_STATUS_LITERAL = Literal["all", "sent", "failed"]


@router.get("/send-runs")
def get_email_send_runs(
    limit: int = Query(
        default=SEND_RUNS_LIMIT_DEFAULT,
        ge=1,
        le=SEND_RUNS_LIMIT_MAX,
    ),
    status_filter: _SEND_RUNS_STATUS_LITERAL = Query(
        default="all",
        alias="status",
    ),
    current_user: User = Depends(admin_user_required),
) -> dict[str, Any]:
    """최근 EmailSendRun 이력을 created_at 내림차순으로 반환한다.

    Args:
        limit: 반환할 최대 row 수. 기본 50, 최대 200 (첨부 spec).
        status_filter: \"all\" / \"sent\" / \"failed\" 중 하나. 기본 \"all\".
            HTTP 쿼리 키는 ``status`` (Python 예약어 회피용으로 함수 인자만
            ``status_filter`` 로 명명).
        current_user: 라우터 dependency 로 이미 검증된 admin user.

    Returns:
        ``{\"items\": [...], \"count\": N}`` 형식의 dict. 각 item 은
        ``_serialize_send_run`` 직렬화 결과 (body_preview 제외).
    """
    logger.debug(
        "GET /api/admin/email/send-runs 진입: user_id={} limit={} status={!r}",
        current_user.id,
        limit,
        status_filter,
    )

    with session_scope() as session:
        # 쿼리 빌드 — ORDER BY created_at DESC 는 SQL 측에서 명시 (인덱스는
        # ascending 으로 만들었지만 DB 가 양방향 스캔 가능하므로 효율적).
        stmt = select(EmailSendRun).order_by(EmailSendRun.created_at.desc())
        if status_filter == "sent":
            stmt = stmt.where(EmailSendRun.status == EmailSendRunStatus.SENT)
        elif status_filter == "failed":
            stmt = stmt.where(EmailSendRun.status == EmailSendRunStatus.FAILED)
        # status_filter == \"all\" 은 추가 필터 없음.
        stmt = stmt.limit(limit)

        rows = list(session.execute(stmt).scalars().all())
        items = [_serialize_send_run(row) for row in rows]

    return {
        "items": items,
        "count": len(items),
    }


# ──────────────────────────────────────────────────────────────
# Phase A-3 (task 00125-8) — Daily Report 관리 API
# ──────────────────────────────────────────────────────────────
#
# 디자인 노트 §9 / phase_a3_prompt.md §7 인용. 운영자가 \"메일 발송 설정\" 페이지의
# 「Daily Report」 카드에서 호출하는 5개 endpoint + 발송 이력 expand 1개를 본 라우터
# 안에 함께 둔다 (별도 admin_daily_report.py 신설하지 않음 — 같은 admin 도메인이라
# 응집도 우선). 권한은 라우터 레벨 ``admin_user_required`` 로 공통 보호.
#
# 엔드포인트:
#   GET  /api/admin/email/daily-report/settings
#       → 현재 설정 + 다음 실행 시각 + 수신자 명단 (eligible 플래그 포함)
#   PUT  /api/admin/email/daily-report/settings
#       → 4 SystemSetting 저장 + APScheduler 잡 자동 등록/제거
#   POST /api/admin/email/daily-report/test-send
#       → 단일 임의 주소로 trigger=manual_test 발송 (last_sent_at 갱신 안 함)
#   POST /api/admin/email/daily-report/send-now
#       → 현재 시점 발송 대상 사용자 email 자동 수집 후 trigger=manual_admin 발송
#   GET  /api/admin/email/daily-report/runs
#       → EmailDailyReportRun 최근 N건 (default 50, max 200)
#   GET  /api/admin/email/daily-report/runs/{run_id}/sends
#       → 해당 daily report 의 수신자별 EmailSendRun 목록


# /daily-report/runs 의 limit query 파라미터 상하한.
DAILY_REPORT_RUNS_LIMIT_DEFAULT: int = 50
DAILY_REPORT_RUNS_LIMIT_MAX: int = 200

# test_recipient SystemSetting 키의 최대 길이. EmailStr 의 RFC 320 자 한도와
# 정합 (recipient 컬럼이 String(320) 인 EmailSendRun 과 일치).
DAILY_REPORT_TEST_RECIPIENT_MAX_LENGTH: int = 320

# POST /daily-report/test-send 의 recipient 입력 최대 길이.
# 콤마 구분 다중 주소를 수용하기 위해 320자보다 큰 값으로 설정.
DAILY_REPORT_TEST_SEND_MULTI_RECIPIENT_MAX_LENGTH: int = 2000


def _validate_cron_expression(cron_expression: str) -> str:
    """5필드 cron 표현식을 ``CronTrigger.from_crontab`` 으로 파싱해 유효성을 확인한다.

    빈 문자열은 그대로 반환한다 (PUT settings 에서 ``enabled=False`` 인 경우
    cron 을 비워 저장하는 흐름을 허용). 형식이 잘못된 경우 ``ValueError`` 를
    raise — Pydantic validator 가 이를 422 로 변환한다.

    Args:
        cron_expression: 사용자가 입력한 cron 문자열.

    Returns:
        앞뒤 공백 제거된 cron 표현식. 빈 입력은 빈 문자열 그대로.

    Raises:
        ValueError: cron 표현식 파싱 실패 (잘못된 필드 수 / 범위 등).
    """
    stripped = cron_expression.strip()
    if not stripped:
        return ""
    # lazy import — apscheduler 의 호환성 문제를 런타임에만 노출 (service.py 와
    # 동일 정책). cron 형식만 검증하므로 timezone 은 부가 정보로만 쓰인다.
    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(stripped, timezone=KST)
    except Exception as exc:
        raise ValueError(
            f"cron 표현식이 올바르지 않습니다: {exc}"
        ) from exc
    return stripped


class RecipientInfo(BaseModel):
    """GET /daily-report/settings 응답의 수신자 상세 1건.

    UI 의 "자세히 보기" expand 영역에 표시되며, 운영자가 누가 발송 대상인지
    한눈에 확인할 수 있게 한다. ``eligible=False`` 인 사용자는 발송 명단에서
    제외된다 (이메일 미설정 또는 ``email_subscribed=False``).
    """

    username: str
    email: str | None
    email_subscribed: bool
    eligible: bool


class DailyReportSettingsOut(BaseModel):
    """GET / PUT /daily-report/settings 응답 본문 (디자인 노트 §9 스키마)."""

    enabled: bool
    cron_expression: str
    last_sent_at: str | None
    test_recipient: str
    next_run_at: str | None
    recipients: list[RecipientInfo]
    recipient_count_eligible: int
    recipient_count_without_email: int
    recipient_count_unsubscribed: int


class DailyReportSettingsIn(BaseModel):
    """PUT /daily-report/settings 요청 본문.

    cron 표현식은 Pydantic validator 가 ``CronTrigger.from_crontab`` 으로 파싱해
    형식 오류를 422 로 즉시 반환한다. ``enabled=True`` 인데 cron 이 빈 값이면
    ``model_validator`` 가 422 를 던진다 — 활성화하려면 cron 이 반드시 있어야
    한다는 운영 의미를 라우터 진입 전에 강제.

    ``test_recipient`` 는 빈 문자열 허용 (미설정 상태로 저장 가능). 형식 검증은
    실제 사용 시점(POST /test-send)에서 한다 — 사용자가 미완성 입력을 일시 저장
    하는 흐름을 허용한다.
    """

    enabled: bool = False
    cron_expression: str = Field(default="", max_length=200)
    test_recipient: str = Field(
        default="",
        max_length=DAILY_REPORT_TEST_RECIPIENT_MAX_LENGTH,
    )

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        """cron 표현식 형식을 ``CronTrigger.from_crontab`` 으로 검증한다."""
        return _validate_cron_expression(value)

    @field_validator("test_recipient")
    @classmethod
    def strip_test_recipient(cls, value: str) -> str:
        """앞뒤 공백 제거. 빈 문자열은 그대로 둔다 (미설정 상태 허용)."""
        return value.strip()

    @model_validator(mode="after")
    def validate_enabled_requires_cron(self) -> "DailyReportSettingsIn":
        """``enabled=True`` 인데 cron 이 빈 값이면 활성화 불가하다는 의미를 강제."""
        if self.enabled and not self.cron_expression:
            raise ValueError(
                "Daily Report 를 활성화하려면 cron 표현식을 입력해야 합니다."
            )
        return self


class DailyReportTestSendIn(BaseModel):
    """POST /daily-report/test-send 요청 본문.

    ``recipient`` 가 None 또는 빈 문자열이면 라우터가 SystemSetting 의
    ``email.daily_report.test_recipient`` 를 fallback 으로 사용한다. 둘 다 빈 값
    이면 라우터가 422 를 반환한다 (Pydantic 단계에서는 빈 문자열도 허용).

    형식 검증을 Pydantic ``EmailStr`` 에 위임하지 않고 라우터에서 처리하는 이유:
    Pydantic ``EmailStr`` 은 빈 문자열을 거부하므로 fallback 분기를 만들 수 없다.
    그 대신 라우터가 ``email_validator.validate_email`` 로 명시적 검증한다.
    """

    recipient: str | None = Field(
        default=None,
        max_length=DAILY_REPORT_TEST_SEND_MULTI_RECIPIENT_MAX_LENGTH,
    )


class DailyReportRunResult(BaseModel):
    """POST /daily-report/test-send / send-now 응답 본문 (디자인 노트 §9 스키마)."""

    run_id: int
    status: str
    snapshot_count: int
    recipient_count: int
    success_count: int
    failure_count: int
    error_message: str | None


# ──────────────────────────────────────────────────────────────
# Daily Report — 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _load_daily_report_setting_values(session) -> dict[str, Any]:
    """SystemSetting 4종 + send_enabled 게이트 값을 한 번에 읽어 dict 으로 반환한다.

    GET / PUT 모두 settings 응답 직렬화에 같은 값들을 사용하므로 헬퍼로 묶었다.
    각 값은 row 가 없으면 ``DEFAULT_*`` 로 fallback (constants 동일 패턴).

    Returns:
        ``{"enabled": bool, "cron_expression": str, "last_sent_at": str | None,
            "test_recipient": str}``. ``last_sent_at`` 은 빈 문자열을 None 으로
        변환해 응답 직렬화 단계에서 명확하게 \"미설정\" 을 표현한다.
    """
    raw_enabled = get_setting(session, SETTING_KEY_DAILY_REPORT_ENABLED)
    if raw_enabled is None or raw_enabled.strip() == "":
        enabled = DEFAULT_DAILY_REPORT_ENABLED
    else:
        enabled = raw_enabled.strip().lower() == "true"

    cron_expression = (
        get_setting(session, SETTING_KEY_DAILY_REPORT_CRON)
        or DEFAULT_DAILY_REPORT_CRON
    )
    raw_last_sent_at = get_setting(session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT)
    if raw_last_sent_at is None or raw_last_sent_at.strip() == "":
        last_sent_at: str | None = None
    else:
        last_sent_at = raw_last_sent_at.strip()
    test_recipient = (
        get_setting(session, SETTING_KEY_DAILY_REPORT_TEST_RECIPIENT)
        or DEFAULT_DAILY_REPORT_TEST_RECIPIENT
    )

    return {
        "enabled": enabled,
        "cron_expression": cron_expression,
        "last_sent_at": last_sent_at,
        "test_recipient": test_recipient,
    }


def _build_recipient_overview(
    session,
) -> tuple[list[RecipientInfo], int, int, int]:
    """전체 사용자의 발송 적합성 매트릭스를 만든다.

    수신 대상 정책 (eligible = email NOT NULL/'' AND email_subscribed=True)
    을 응답 직렬화 직전에 한 번 더 평가한다. ``collect_recipient_emails``
    가 실제 발송 대상 email list 만 반환하는 데 비해, 본 헬퍼는 운영자가 UI
    에서 \"누가 빠지는지\" 까지 보기 위해 전수 사용자의 상태를 반환한다.

    Returns:
        ``(recipients, eligible_count, without_email_count, unsubscribed_count)``.
        recipients 는 username 알파벳순으로 정렬돼 UI 표시 순서를 안정화한다.
    """
    statement = select(User).order_by(User.username.asc())
    users = list(session.execute(statement).scalars().all())

    recipients: list[RecipientInfo] = []
    eligible_count = 0
    without_email_count = 0
    unsubscribed_count = 0
    for user in users:
        # email 컬럼은 nullable + 빈 문자열도 \"미설정\" 으로 간주 (collect_recipient_emails
        # 의 SQL 필터와 동일 시맨틱).
        has_email = bool(user.email and user.email.strip())
        subscribed = bool(user.email_subscribed)
        eligible = has_email and subscribed
        if eligible:
            eligible_count += 1
        if not has_email:
            without_email_count += 1
        if has_email and not subscribed:
            unsubscribed_count += 1
        recipients.append(
            RecipientInfo(
                username=user.username,
                email=user.email if has_email else None,
                email_subscribed=subscribed,
                eligible=eligible,
            )
        )

    return recipients, eligible_count, without_email_count, unsubscribed_count


def _next_run_at_iso() -> str | None:
    """APScheduler 의 daily report 잡 next_run_time 을 ISO-8601 문자열로 반환한다.

    잡이 없거나 스케줄러가 미기동이면 None. ``ScheduleSummary.next_run_time``
    은 KST tz-aware datetime (BackgroundScheduler.timezone=KST) 이므로 그대로
    ``isoformat()`` 한다.
    """
    summary = get_daily_report_schedule_summary()
    if summary is None or summary.next_run_time is None:
        return None
    return summary.next_run_time.isoformat()


def _build_daily_report_settings_response(session) -> DailyReportSettingsOut:
    """SystemSetting + 수신자 명단 + next_run_at 을 한 번에 묶어 GET 응답으로 직렬화."""
    values = _load_daily_report_setting_values(session)
    (
        recipients,
        eligible_count,
        without_email_count,
        unsubscribed_count,
    ) = _build_recipient_overview(session)
    return DailyReportSettingsOut(
        enabled=values["enabled"],
        cron_expression=values["cron_expression"],
        last_sent_at=values["last_sent_at"],
        test_recipient=values["test_recipient"],
        next_run_at=_next_run_at_iso(),
        recipients=recipients,
        recipient_count_eligible=eligible_count,
        recipient_count_without_email=without_email_count,
        recipient_count_unsubscribed=unsubscribed_count,
    )


def _serialize_daily_report_run(run: EmailDailyReportRun) -> dict[str, Any]:
    """EmailDailyReportRun 1 row 를 /daily-report/runs 응답용 dict 로 직렬화.

    ``requested_by`` 는 lazy relationship 으로 load (페이지당 50 row + 같은
    admin user 가 대부분이라 N+1 영향이 작다 — EmailSendRun ``_serialize_send_run``
    과 동일 정책).
    """
    requested_by_user = run.requested_by
    return {
        "id": run.id,
        "trigger": run.trigger,
        "status": run.status.value if run.status is not None else None,
        "aggregation_from": (
            as_utc(run.aggregation_from).isoformat()
            if run.aggregation_from is not None
            else None
        ),
        "aggregation_to": (
            as_utc(run.aggregation_to).isoformat()
            if run.aggregation_to is not None
            else None
        ),
        "snapshot_count": run.snapshot_count,
        "recipient_count": run.recipient_count,
        "success_count": run.success_count,
        "failure_count": run.failure_count,
        "error_message": run.error_message,
        "started_at": (
            as_utc(run.started_at).isoformat() if run.started_at is not None else None
        ),
        "completed_at": (
            as_utc(run.completed_at).isoformat() if run.completed_at is not None else None
        ),
        "requested_by": (
            {
                "id": requested_by_user.id,
                "username": requested_by_user.username,
            }
            if requested_by_user is not None
            else None
        ),
    }


def _serialize_daily_report_send_run(send_run: EmailSendRun) -> dict[str, Any]:
    """EmailSendRun 1 row 를 발송 이력 expand 응답용 dict 로 직렬화.

    forward 의 ``_serialize_send_run`` 스키마 그대로 (수신자별 발송 시도 결과).
    """
    return {
        "id": send_run.id,
        "recipient": send_run.recipient,
        "status": send_run.status.value if send_run.status is not None else None,
        "attempt_count": send_run.attempt_count,
        "error_message": send_run.error_message,
        "sent_at": (
            as_utc(send_run.sent_at).isoformat() if send_run.sent_at is not None else None
        ),
    }


def _resolve_test_send_recipients(
    session, body_recipient: str | None
) -> list[str]:
    """``POST /daily-report/test-send`` 의 수신자 목록 결정 + 형식 검증.

    우선순위:
        1. body 의 ``recipient`` 가 비어 있지 않으면 콤마로 분리해 각 주소를 사용.
        2. 비어 있으면 SystemSetting ``test_recipient`` 를 fallback 단일 주소로 사용.
        3. 둘 다 빈 값이면 422 (HTTPException) raise.

    입력이 있는 경우 콤마 분리 후 각 항목 strip → 빈 문자열 제거 → 개별 형식 검증
    (``email_validator.validate_email``) 순서로 처리한다. 하나라도 형식 오류가 있으면
    해당 주소와 오류 메시지를 포함해 422 로 거부한다.

    Args:
        session: SystemSetting fallback 조회용 ORM 세션.
        body_recipient: 라우터가 받은 body 의 ``recipient`` 값 (콤마 구분 가능).

    Returns:
        검증을 통과한 정규화된(normalized) 이메일 주소 리스트.

    Raises:
        HTTPException(422): 받는 사람을 결정할 수 없거나 하나 이상의 주소 형식이 잘못됨.
    """
    from email_validator import EmailNotValidError, validate_email

    candidate = (body_recipient or "").strip()

    # 입력이 비어 있으면 SystemSetting fallback — 단일 주소로 리스트 반환.
    if not candidate:
        stored = (
            get_setting(session, SETTING_KEY_DAILY_REPORT_TEST_RECIPIENT)
            or DEFAULT_DAILY_REPORT_TEST_RECIPIENT
        )
        fallback = (stored or "").strip()
        if not fallback:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "받는 사람을 입력하거나, 「Daily Report」 카드에서 기본 받는 "
                    "사람을 먼저 저장해 주세요."
                ),
            )
        try:
            validated = validate_email(fallback, check_deliverability=False)
        except EmailNotValidError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"저장된 기본 받는 사람 주소 형식이 올바르지 않습니다: {exc}",
            ) from exc
        return [validated.normalized]

    # 콤마로 분리 → 각 항목 strip → 빈 문자열 제거.
    parts = [p.strip() for p in candidate.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "받는 사람을 입력하거나, 「Daily Report」 카드에서 기본 받는 "
                "사람을 먼저 저장해 주세요."
            ),
        )

    # 각 주소 형식 검증 — 하나라도 오류이면 해당 주소와 함께 422.
    normalized_addresses: list[str] = []
    for address in parts:
        try:
            validated = validate_email(address, check_deliverability=False)
            normalized_addresses.append(validated.normalized)
        except EmailNotValidError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"이메일 주소 형식이 올바르지 않습니다 ({address!r}): {exc}",
            ) from exc

    return normalized_addresses


def _result_to_response(result: Any) -> DailyReportRunResult:
    """``DailyReportResult`` dataclass → Pydantic 응답 객체로 변환."""
    return DailyReportRunResult(
        run_id=result.run_id,
        status=(
            result.status.value
            if result.status is not None
            else None
        ),
        snapshot_count=result.snapshot_count,
        recipient_count=result.recipient_count,
        success_count=result.success_count,
        failure_count=result.failure_count,
        error_message=result.error_message,
    )


# ──────────────────────────────────────────────────────────────
# 5. GET /api/admin/email/daily-report/settings
# ──────────────────────────────────────────────────────────────


@router.get(
    "/daily-report/settings",
    response_model=DailyReportSettingsOut,
)
def get_daily_report_settings(
    current_user: User = Depends(admin_user_required),
) -> DailyReportSettingsOut:
    """Daily Report 설정 + 수신자 명단 + 다음 실행 시각을 한 번에 반환한다.

    응답 스키마는 디자인 노트 §9 / phase_a3_prompt.md §7 기준 (task 00144 에서
    admin 한정 → 전체 사용자로 확장):
        - enabled / cron_expression / last_sent_at / test_recipient (SystemSetting)
        - next_run_at (APScheduler ``next_run_time``, KST tz-aware ISO-8601)
        - recipients (전수 사용자의 username/email/email_subscribed/eligible)
        - 카운터 3종 (eligible / without_email / unsubscribed)
    """
    logger.debug(
        "GET /api/admin/email/daily-report/settings 진입: user_id={}",
        current_user.id,
    )
    with session_scope() as session:
        return _build_daily_report_settings_response(session)


# ──────────────────────────────────────────────────────────────
# 6. PUT /api/admin/email/daily-report/settings
# ──────────────────────────────────────────────────────────────


@router.put(
    "/daily-report/settings",
    response_model=DailyReportSettingsOut,
    dependencies=[Depends(ensure_same_origin)],
)
def put_daily_report_settings(
    body: DailyReportSettingsIn,
    current_user: User = Depends(admin_user_required),
) -> DailyReportSettingsOut:
    """Daily Report SystemSetting 3종 저장 + APScheduler 잡 자동 등록/제거.

    저장 흐름:
        1. Pydantic validator 가 cron 표현식 형식 / enabled-vs-cron 일관성을 422
           로 미리 거른다.
        2. ``register_daily_report_cron_schedule(cron, enabled)`` 호출 —
           enabled=False / cron 빈 값 → 잡 제거, 그 외 → add or reschedule.
           ``ScheduleValidationError`` 는 422 로 변환 (Pydantic 검증과 별개로
           스케줄러 내부에서 거부된 경우).
        3. SystemSetting 3종 (enabled / cron_expression / test_recipient) 저장.
        4. 저장 후 GET 과 동일 형식의 응답을 빌드해 반환 — next_run_at 까지 갱신된
           최신 상태가 즉시 노출된다.

    순서 주의 (task 00128 버그 수정):
        스케줄러 잡 갱신(2)을 SystemSetting 저장(3)보다 **먼저**, 그리고
        ``session_scope`` 트랜잭션 **밖에서** 수행한다. ``register_daily_report_cron_schedule``
        은 APScheduler 의 ``SQLAlchemyJobStore`` 를 통해 같은 SQLite 파일의
        ``scheduler_jobs`` 테이블에 별도 커넥션으로 write 한다. 만약 ``set_setting``
        + ``flush`` 로 이미 write 트랜잭션이 열린 세션 안에서 호출하면 SQLite 의
        단일 writer 제약에 걸려 ``database is locked`` 가 발생한다. 백업 설정 저장
        (``admin.backup_settings_save``)도 동일하게 스케줄 등록을 ``session_scope``
        밖에서 먼저 처리한다 — 같은 순서를 따른다.

    ``last_sent_at`` 은 본 endpoint 에서 직접 수정하지 않는다 — 발송 흐름
    (``prepare_and_send_daily_report``) 의 정책 표가 single source of truth.
    수동 reset 이 필요하면 SQL 직접 수정 (README.USER.md 의 트러블슈팅 절).
    """
    logger.info(
        "PUT /api/admin/email/daily-report/settings 진입: user_id={} "
        "enabled={} cron={!r} test_recipient={!r}",
        current_user.id,
        body.enabled,
        body.cron_expression,
        body.test_recipient,
    )

    # 1. APScheduler 잡 갱신 — SystemSetting 저장 트랜잭션을 열기 **전에**, 그리고
    #    session_scope 밖에서 먼저 수행한다. 이렇게 해야 jobstore(scheduler_jobs
    #    테이블) write 가 웹 세션의 write 트랜잭션과 SQLite 단일 writer 제약에서
    #    충돌(\"database is locked\")하지 않는다 (위 docstring '순서 주의' 참조).
    #    register_daily_report_cron_schedule 은 비활성 분기에서
    #    _require_running_scheduler 를 호출하므로 스케줄러 미기동 시
    #    ScheduleValidationError 가 발생한다 — 그 경우는 422.
    try:
        register_daily_report_cron_schedule(
            body.cron_expression,
            enabled=body.enabled,
        )
    except ScheduleValidationError as exc:
        logger.warning(
            "Daily Report cron 등록 실패: user_id={} cron={!r} error={}",
            current_user.id,
            body.cron_expression,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # 2. 스케줄 잡 갱신이 끝난 뒤 SystemSetting 3종을 저장한다. 이 시점에는
    #    scheduler_jobs 쪽 write 가 이미 커밋돼 잠금 충돌이 없다.
    with session_scope() as session:
        # bool → \"true\" / \"false\" (소문자 통일, send_enabled 패턴과 동일).
        set_setting(
            session,
            SETTING_KEY_DAILY_REPORT_ENABLED,
            "true" if body.enabled else "false",
        )
        set_setting(
            session,
            SETTING_KEY_DAILY_REPORT_CRON,
            body.cron_expression,
        )
        set_setting(
            session,
            SETTING_KEY_DAILY_REPORT_TEST_RECIPIENT,
            body.test_recipient,
        )
        session.flush()

        # 저장된 값 + 갱신된 스케줄 잡(next_run_at)을 한 번에 직렬화해 응답한다.
        # _build_daily_report_settings_response 는 읽기 전용(SELECT)이므로 열린
        # write 트랜잭션 안에서 호출해도 잠금 충돌이 없다.
        response = _build_daily_report_settings_response(session)

    logger.info("Daily Report 설정 저장 완료: user_id={}", current_user.id)
    return response


# ──────────────────────────────────────────────────────────────
# 7. POST /api/admin/email/daily-report/test-send
# ──────────────────────────────────────────────────────────────


@router.post(
    "/daily-report/test-send",
    response_model=DailyReportRunResult,
    dependencies=[Depends(ensure_same_origin)],
)
def post_daily_report_test_send(
    body: DailyReportTestSendIn,
    current_user: User = Depends(admin_user_required),
) -> DailyReportRunResult:
    """콤마 구분 다중 수신자에게 daily report 본 발송 동작을 그대로 시도한다 (manual_test).

    ``trigger=manual_test`` 라 발송 성공해도 ``last_sent_at`` 은 갱신되지 않는다
    (정책표). 게이트 미통과 시 503 — A-1 의 ``/test-send`` 와 달리 daily report
    의 test-send 는 본 발송 흐름을 그대로 검증하는 게 목적이라 게이트 적용 필수
    (디자인 노트 §7).
    """
    logger.info(
        "POST /api/admin/email/daily-report/test-send 진입: user_id={} recipients={!r}",
        current_user.id,
        body.recipient,
    )

    with session_scope() as session:
        # 1. recipients 결정 + 형식 검증 (body 우선, 빈 값이면 SystemSetting fallback).
        #    콤마 구분 다중 주소를 지원한다.
        recipients = _resolve_test_send_recipients(session, body.recipient)

        # 2. transport 구성 — admin_email 의 test-send 와 동일 정책. 미지원 값
        #    → 422 (운영자에게 SystemSetting 입력 오류임을 알린다).
        try:
            transport = build_transport_from_settings(session)
        except ValueError as exc:
            logger.warning(
                "Daily Report 테스트 발송 실패 — transport 구성 오류: user_id={} error={}",
                current_user.id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

        # 3. max_retry_count 읽기.
        max_retry_count = _read_max_retry_count(session)

        request_dto = DailyReportRequest(
            trigger=TRIGGER_MANUAL_TEST,
            recipients=recipients,
            requested_by_user_id=current_user.id,
        )

        # 4. prepare_and_send_daily_report — 게이트 / 빈 구간 / 발송 루프 모두
        #    내부에서 처리. ``EmailSendingDisabledError`` 만 별도 403/503 변환.
        try:
            result = prepare_and_send_daily_report(
                request_dto,
                session=session,
                transport=transport,
                max_retry_count=max_retry_count,
            )
        except EmailSendingDisabledError as exc:
            logger.warning(
                "Daily Report 테스트 발송 거부 — 메일 전송 비활성화: user_id={}",
                current_user.id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    logger.info(
        "Daily Report 테스트 발송 완료: user_id={} run_id={} status={} recipients={}",
        current_user.id,
        result.run_id,
        result.status.value if result.status is not None else None,
        recipients,
    )
    return _result_to_response(result)


# ──────────────────────────────────────────────────────────────
# 8. POST /api/admin/email/daily-report/send-now
# ──────────────────────────────────────────────────────────────


@router.post(
    "/daily-report/send-now",
    response_model=DailyReportRunResult,
    dependencies=[Depends(ensure_same_origin)],
)
def post_daily_report_send_now(
    current_user: User = Depends(admin_user_required),
) -> DailyReportRunResult:
    """현재 시점 발송 대상 사용자 email 을 수집해 즉시 발송한다 (manual_admin).

    수신자 명단은 ``collect_recipient_emails`` (email NOT NULL/'' AND
    email_subscribed=True 인 전체 사용자 — task 00144 에서 admin 제약 제거).
    발송 성공이면 last_sent_at 이 갱신되어 다음 scheduled 잡의 누적 구간
    시작점이 \"방금\" 으로 당겨진다 — 같은 윈도우 중복 발송 방지.

    빈 수신자 케이스 (수신 대상 사용자 0명) 는 ``prepare_and_send_daily_report``
    가 FAILED + ``error_message='발송 대상 수신자가 없습니다.'`` 로 commit + 200
    응답으로 받아낸다 (HTTP 422 가 아니라 200 + status=failed 인 이유: 사용자는
    이 시점에 \"발송 시도 이력\" 을 남기길 원할 수 있기 때문).
    """
    logger.info(
        "POST /api/admin/email/daily-report/send-now 진입: user_id={}",
        current_user.id,
    )

    with session_scope() as session:
        # 1. 수신자 수집 — 유효 이메일 + 수신 동의 사용자 전체.
        recipients = collect_recipient_emails(session)

        # 2. transport 구성 + max_retry_count.
        try:
            transport = build_transport_from_settings(session)
        except ValueError as exc:
            logger.warning(
                "Daily Report 즉시 발송 실패 — transport 구성 오류: user_id={} error={}",
                current_user.id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

        max_retry_count = _read_max_retry_count(session)

        request_dto = DailyReportRequest(
            trigger=TRIGGER_MANUAL_ADMIN,
            recipients=recipients,
            requested_by_user_id=current_user.id,
        )

        # 3. prepare_and_send_daily_report — 게이트 / SKIPPED / 발송 루프 통합.
        try:
            result = prepare_and_send_daily_report(
                request_dto,
                session=session,
                transport=transport,
                max_retry_count=max_retry_count,
            )
        except EmailSendingDisabledError as exc:
            logger.warning(
                "Daily Report 즉시 발송 거부 — 메일 전송 비활성화: user_id={}",
                current_user.id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    logger.info(
        "Daily Report 즉시 발송 완료: user_id={} run_id={} status={} recipient_count={}",
        current_user.id,
        result.run_id,
        result.status.value if result.status is not None else None,
        result.recipient_count,
    )
    return _result_to_response(result)


# ──────────────────────────────────────────────────────────────
# 9. GET /api/admin/email/daily-report/runs?limit=50
# ──────────────────────────────────────────────────────────────


@router.get("/daily-report/runs")
def get_daily_report_runs(
    limit: int = Query(
        default=DAILY_REPORT_RUNS_LIMIT_DEFAULT,
        ge=1,
        le=DAILY_REPORT_RUNS_LIMIT_MAX,
    ),
    current_user: User = Depends(admin_user_required),
) -> dict[str, Any]:
    """최근 EmailDailyReportRun 이력을 ``started_at`` 내림차순으로 반환한다.

    Args:
        limit: 반환할 최대 row 수. 기본 50, 최대 200.
        current_user: 라우터 dependency 로 이미 검증된 admin user.

    Returns:
        ``{"items": [...], "count": N}`` — 각 item 은
        ``_serialize_daily_report_run`` 결과.
    """
    logger.debug(
        "GET /api/admin/email/daily-report/runs 진입: user_id={} limit={}",
        current_user.id,
        limit,
    )

    with session_scope() as session:
        stmt = (
            select(EmailDailyReportRun)
            .order_by(EmailDailyReportRun.started_at.desc())
            .limit(limit)
        )
        rows = list(session.execute(stmt).scalars().all())
        items = [_serialize_daily_report_run(run) for run in rows]

    return {
        "items": items,
        "count": len(items),
    }


# ──────────────────────────────────────────────────────────────
# 10. GET /api/admin/email/daily-report/runs/{run_id}/sends
# ──────────────────────────────────────────────────────────────


@router.get("/daily-report/runs/{run_id}/sends")
def get_daily_report_run_sends(
    run_id: int,
    current_user: User = Depends(admin_user_required),
) -> dict[str, Any]:
    """해당 daily report run 의 수신자별 EmailSendRun 목록을 반환한다.

    ``related_kind='daily_report'`` AND ``related_id={run_id}`` 매칭으로 발송
    이력 expand 데이터를 만든다 (forward 의 ``/sends`` 와 동일 패턴). 본 run
    의 ``status=SKIPPED`` 면 EmailSendRun 자체가 생성되지 않아 빈 list 가
    반환된다.

    Args:
        run_id: EmailDailyReportRun PK (path 파라미터).
        current_user: 라우터 dependency 로 이미 검증된 admin user.

    Returns:
        ``{"items": [...], "count": N}`` — ``created_at`` 오름차순 (발송 시도
        순서). 매칭되는 EmailSendRun 이 없으면 빈 list.

    Raises:
        HTTPException(404): ``run_id`` 에 해당하는 EmailDailyReportRun row 가 없음.
    """
    logger.debug(
        "GET /api/admin/email/daily-report/runs/{}/sends 진입: user_id={}",
        run_id,
        current_user.id,
    )

    with session_scope() as session:
        run = session.get(EmailDailyReportRun, run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Daily Report 발송 이력을 찾을 수 없습니다: run_id={run_id}",
            )

        stmt = (
            select(EmailSendRun)
            .where(
                EmailSendRun.related_kind == RELATED_KIND_DAILY_REPORT,
                EmailSendRun.related_id == run_id,
            )
            .order_by(EmailSendRun.created_at.asc())
        )
        send_runs = list(session.execute(stmt).scalars().all())
        items = [_serialize_daily_report_send_run(row) for row in send_runs]

    return {
        "items": items,
        "count": len(items),
    }


__all__ = ["router"]
