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
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select

from app.auth.dependencies import admin_user_required, ensure_same_origin
from app.backup.service import get_setting, set_setting
from app.db.models import EmailSendRun, EmailSendRunStatus, User
from app.db.session import session_scope
from app.email.constants import (
    DEFAULT_APP_PUBLIC_BASE_URL,
    DEFAULT_EMAIL_FROM_DISPLAY_NAME,
    DEFAULT_EMAIL_M365_CLIENT_ID,
    DEFAULT_EMAIL_M365_CLIENT_SECRET,
    DEFAULT_EMAIL_M365_SENDER_ADDRESS,
    DEFAULT_EMAIL_M365_TENANT_ID,
    DEFAULT_EMAIL_MAX_RETRY_COUNT,
    DEFAULT_EMAIL_SEND_ENABLED,
    DEFAULT_EMAIL_TRANSPORT_TYPE,
    RELATED_KIND_TEST_SEND,
    SETTING_KEY_APP_PUBLIC_BASE_URL,
    SETTING_KEY_EMAIL_FROM_DISPLAY_NAME,
    SETTING_KEY_EMAIL_M365_CLIENT_ID,
    SETTING_KEY_EMAIL_M365_CLIENT_SECRET,
    SETTING_KEY_EMAIL_M365_SENDER_ADDRESS,
    SETTING_KEY_EMAIL_M365_TENANT_ID,
    SETTING_KEY_EMAIL_MAX_RETRY_COUNT,
    SETTING_KEY_EMAIL_SEND_ENABLED,
    SETTING_KEY_EMAIL_TRANSPORT_TYPE,
)
from app.email.gate import is_email_sending_enabled
from app.email.message_builder import build_plain_text_message
from app.email.sender import send_with_retry
from app.email.transport.factory import build_transport_from_settings


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
        # 0. 메일 전송 기능 활성화 확인 — off 이면 503 으로 즉시 차단.
        #    EmailSendRun row 가 INSERT 되기 전에 확인해 이력이 남지 않도록 한다.
        if not is_email_sending_enabled(session):
            logger.warning(
                "테스트 발송 거부 — 메일 전송 기능 비활성화: user_id={}",
                current_user.id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "메일 전송 기능이 비활성화되어 있습니다. "
                    "시스템 관리 > 메일 발송 탭에서 활성화해 주세요."
                ),
            )

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


__all__ = ["router"]
