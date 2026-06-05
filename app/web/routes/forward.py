"""공고 포워딩 발송 / 발송 이력 조회 라우터 (Phase A-2 Part 2 / task 00109-5).

설계 근거:
    docs/phase_a2_part2_design_note.md §10 (API 스펙 요약) + 첨부
    phase_a2_part2_prompt.md "백엔드 변경 §4 API endpoints".

엔드포인트 4 개:
    POST /api/canonical/{canonical_id}/forward
        → 공고 1건을 N명에게 메일로 포워딩한다. 로그인 사용자 전용.
    GET  /api/canonical/{canonical_id}/forward-logs
        → 해당 공고의 포워딩 발송 이력 목록. 비로그인 허용.
    GET  /api/canonical/{canonical_id}/forward-logs/{forward_log_id}/sends
        → 발송 이력 1건의 수신자별 발송 시도 결과. 비로그인 허용.
    GET  /api/users/search
        → 수신자 chip 입력의 내부 사용자 자동완성용 검색. 로그인 사용자 전용.

라우터 분리 근거 (design note §4):
    ``admin_email.py`` 는 라우터 레벨 ``admin_user_required`` 로 admin 전용이다.
    공고 포워딩은 일반 로그인 사용자(POST)와 비로그인 GET 이 혼재하므로,
    admin 라우터와 분리된 본 모듈로 둔다. 권한은 endpoint 별로 dependency 를
    달리 건다 (``progress.py`` 와 동일한 endpoint 단위 권한 분기 패턴).

보호:
    - POST /forward: ``current_user_required`` + ``ensure_same_origin``.
    - GET 2 종: ``current_user_optional`` 만 적용 — 비로그인도 동일 응답
      (Phase B/C 의 GET history 정책과 일관).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.auth.dependencies import (
    current_user_optional,
    current_user_required,
    ensure_same_origin,
)
from app.backup.service import get_setting
from app.db.models import (
    EmailForwardLog,
    EmailForwardStatus,
    EmailSendRun,
    User,
    UserOrganization,
    as_utc,
)
from app.db.repository import get_canonical_project_by_id
from app.db.session import session_scope
from app.email.constants import (
    DEFAULT_EMAIL_MAX_RETRY_COUNT,
    SETTING_KEY_EMAIL_MAX_RETRY_COUNT,
)
from app.email.forwarding import (
    ForwardPreparation,
    ForwardRequest,
    forward_announcement_prepare,
    forward_announcement_run,
    get_forward_log_with_send_runs,
    list_forward_logs_for_canonical,
)
from app.email.gate import EmailSendingDisabledError
from app.email.transport.base import EmailTransport
from app.email.transport.factory import build_transport_from_settings
from app.organizations.service import get_user_organization_ids
from app.timezone import now_utc

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# GET /forward-logs 의 limit query 파라미터 상하한 (design note §10).
FORWARD_LOGS_LIMIT_DEFAULT: int = 50
FORWARD_LOGS_LIMIT_MAX: int = 200

# POST /forward 의 recipients 개수 제한 (첨부 prompt §4 ForwardSendRequest).
# 서버 제한과 프론트엔드 chip 입력 최대 개수(00109-9)가 일치한다.
RECIPIENTS_MIN_COUNT: int = 1
RECIPIENTS_MAX_COUNT: int = 50

# POST /forward 의 additional_message 최대 길이 (첨부 prompt §4).
ADDITIONAL_MESSAGE_MAX_LENGTH: int = 5000

# POST /forward 의 subject 최대 길이. EmailForwardLog.subject 컬럼이
# String(200) 이므로, 200 자 초과 입력을 DB INSERT 단계의 500 이 아니라
# 라우터 단계의 422 로 먼저 끊는다 (design note §10 "subject ≤200").
SUBJECT_MAX_LENGTH: int = 200

# GET /api/users/search 의 q query 파라미터 길이 제한 (첨부 prompt §4 /
# design note §10). 1 자 미만/50 자 초과는 FastAPI 가 자동으로 422 로 막는다.
USER_SEARCH_QUERY_MIN_LENGTH: int = 1
USER_SEARCH_QUERY_MAX_LENGTH: int = 50

# GET /api/users/search 의 limit query 파라미터 상하한 (첨부 prompt §4 /
# design note §10).
USER_SEARCH_LIMIT_DEFAULT: int = 10
USER_SEARCH_LIMIT_MAX: int = 30


# ──────────────────────────────────────────────────────────────
# 요청 스키마
# ──────────────────────────────────────────────────────────────


class ForwardSendRequest(BaseModel):
    """POST /api/canonical/{canonical_id}/forward 의 요청 본문 스키마.

    Pydantic 검증 단계에서 1차로 422 를 반환한다 (빈/초과 recipients,
    additional_message 길이 초과, subject 길이 초과). 그 외 의미 검증
    (canonical 존재, 발신 조직 소속)은 라우터 핸들러가 수행한다.

    Attributes:
        recipients: 수신자 이메일 주소 목록. ``EmailStr`` 이 각 항목의 이메일
            형식을 검증한다. 개수는 1 ~ 50 개 — 빈 리스트나 51 개 이상이면 422.
        subject: 메일 제목. ``None`` 또는 빈 문자열이면 forwarding service 가
            ``build_default_forward_subject`` 로 자동 생성한다. 최대 200 자
            (``EmailForwardLog.subject`` 컬럼 길이와 정합).
        additional_message: 수신자에게 전달할 추가 메시지. 본문 빌드에만 쓰이고
            DB 에는 저장되지 않는다 (첨부 여부만 boolean 으로 기록). 최대 5000 자
            — 초과 시 422.
        sender_organization_id: 발송 시점 발송자의 조직 PK. 무소속/미지정이면
            ``None``. ``None`` 이 아니면 라우터가 "본인 소속 조직인지" 를
            검증하며, 본인 소속이 아니면 403.
    """

    recipients: list[EmailStr] = Field(
        min_length=RECIPIENTS_MIN_COUNT,
        max_length=RECIPIENTS_MAX_COUNT,
    )
    subject: str | None = Field(default=None, max_length=SUBJECT_MAX_LENGTH)
    additional_message: str | None = Field(
        default=None,
        max_length=ADDITIONAL_MESSAGE_MAX_LENGTH,
    )
    sender_organization_id: int | None = None


# ──────────────────────────────────────────────────────────────
# 라우터
# ──────────────────────────────────────────────────────────────


router = APIRouter(tags=["forward"])


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _read_max_retry_count(session) -> int:
    """SystemSetting 에서 ``email.max_retry_count`` 를 int 로 읽고 fallback 처리한다.

    ``admin_email.py::_read_max_retry_count`` 와 동일한 방어 패턴을 따른다.
    SystemSetting.value 는 Text 컬럼이라 잘못된 값(예: ``"abc"``)이 들어와
    있어도 ``ValueError`` 를 catch 해 default 로 fallback 하며, 음수도
    default 로 정정한다.

    Args:
        session: SystemSetting 조회용 ORM 세션.

    Returns:
        int max_retry_count (0 이상). 없거나 잘못된 값이면
        ``DEFAULT_EMAIL_MAX_RETRY_COUNT``.
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


def _ensure_canonical_exists(session, canonical_id: int) -> None:
    """canonical_project 가 존재하지 않으면 404 를 던진다.

    ``progress.py`` 의 동명 헬퍼와 동일한 패턴 — 포워딩 발송·이력 조회 3 개
    endpoint 가 공통으로 호출한다.

    Args:
        session: 조회용 ORM 세션.
        canonical_id: 검증 대상 CanonicalProject PK.

    Raises:
        HTTPException(404): 해당 PK 의 CanonicalProject 가 없을 때.
    """
    if get_canonical_project_by_id(session, canonical_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"canonical_project id={canonical_id} 를 찾을 수 없습니다.",
        )


# 응답 직렬화에서 "발송 진행 중" 을 가리키는 가상 status 값. DB 의
# email_forward_status enum 에는 존재하지 않으며, ``completed_at IS NULL`` 인
# row 의 placeholder status (FAILED) 를 사용자 응답에서 본 값으로 덮어쓴다
# (task 00120 비동기 발송 도입 후 진행 중 상태 표시용).
FORWARD_STATUS_IN_PROGRESS: str = "in_progress"


def _serialize_forward_log(forward_log: EmailForwardLog) -> dict[str, Any]:
    """EmailForwardLog 1 row 를 GET /forward-logs 응답용 dict 로 직렬화한다.

    첨부 prompt §4 의 응답 예시 스키마를 그대로 따른다. 수신자 이메일 주소
    목록(``recipient_addresses``)은 개별 수신자 노출을 expand 응답으로 분리
    하기 위해 본 응답에서 제외한다.

    ``sender`` / ``sender_organization`` 은 ``list_forward_logs_for_canonical``
    이 ``selectinload`` 로 eager 로딩해 둔 관계를 사용한다 (N+1 회피). 발송자
    User 가 탈퇴해 ``sender_user_id`` 가 NULL 인 row 는 ``sender`` 를 ``None``
    으로, 발송 조직 미지정/삭제 row 는 ``sender_organization`` 을 ``None`` 으로
    반환한다.

    status 후처리 (task 00120):
        포워딩이 비동기로 발송되면서 ``completed_at IS NULL`` 인 row 가 응답에
        섞이게 된다. 이 경우 DB enum value (placeholder 'failed') 대신 가상
        값 ``'in_progress'`` 로 덮어써 사용자에게 "진행 중" 을 노출한다.
        DB schema (enum) 는 변경하지 않는다.

    Args:
        forward_log: 직렬화할 ``EmailForwardLog`` 인스턴스.

    Returns:
        JSON 응답에 그대로 넣을 dict.
    """
    sender_user = forward_log.sender_user
    sender_organization = forward_log.sender_organization
    # completed_at IS NULL 인 row 는 발송 루프가 아직 끝나지 않은 상태 — DB
    # enum 의 placeholder ('failed') 대신 가상 'in_progress' 를 노출한다.
    if forward_log.completed_at is None:
        serialized_status: str | None = FORWARD_STATUS_IN_PROGRESS
    elif forward_log.status is not None:
        serialized_status = forward_log.status.value
    else:
        serialized_status = None
    return {
        "id": forward_log.id,
        "sender": (
            {
                "id": sender_user.id,
                "username": sender_user.username,
                # User 모델에 display_name 컬럼이 없어 username 을 표시명으로
                # 사용한다 (design note §0 탐사 결과). 향후 표시명 컬럼이
                # 생기면 이 한 줄만 바꾸면 된다.
                "display_name": sender_user.username,
            }
            if sender_user is not None
            else None
        ),
        "sender_organization": (
            {
                "id": sender_organization.id,
                "name": sender_organization.name,
            }
            if sender_organization is not None
            else None
        ),
        "subject": forward_log.subject,
        "recipient_count": forward_log.recipient_count,
        "has_additional_message": forward_log.has_additional_message,
        "status": serialized_status,
        "success_count": forward_log.success_count,
        "failure_count": forward_log.failure_count,
        "created_at": (
            as_utc(forward_log.created_at).isoformat()
            if forward_log.created_at is not None
            else None
        ),
        "completed_at": (
            as_utc(forward_log.completed_at).isoformat()
            if forward_log.completed_at is not None
            else None
        ),
    }


def _serialize_send_run(send_run: EmailSendRun) -> dict[str, Any]:
    """EmailSendRun 1 row 를 발송 이력 expand 응답용 dict 로 직렬화한다.

    첨부 prompt §4 의 ``/sends`` 응답 예시 스키마를 그대로 따른다 — 수신자별
    발송 시도 결과만 노출하며, ``related_kind`` / ``related_id`` / 본문
    preview 등 내부 메타는 제외한다.

    Args:
        send_run: 직렬화할 ``EmailSendRun`` 인스턴스.

    Returns:
        JSON 응답에 그대로 넣을 dict.
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


def _serialize_user_search_result(user: User) -> dict[str, Any]:
    """User 1 row 를 GET /api/users/search 응답용 dict 로 직렬화한다.

    첨부 prompt §4 의 ``/api/users/search`` 응답 예시 스키마를 그대로 따른다 —
    수신자 chip 자동완성에 필요한 ``id`` / ``username`` / ``email`` 과, 발송자
    참고용 소속 조직 목록(``organizations``)만 노출한다.

    소속 조직은 ``search_users_route`` 가 ``selectinload`` 로 eager 로딩해 둔
    ``user_organizations`` 관계를 사용한다 (N+1 회피). 조직이 삭제되어 매핑만
    남은 비정상 row 는 건너뛰며, 결과는 조직명 알파벳순으로 정렬해 표시
    순서를 안정화한다.

    Args:
        user: 직렬화할 ``User`` 인스턴스. ``email`` 은 호출부 쿼리에서 이미
            ``IS NOT NULL`` 로 걸러졌으므로 항상 값이 있다.

    Returns:
        JSON 응답에 그대로 넣을 dict.
    """
    organizations = [
        {
            "id": mapping.organization.id,
            "name": mapping.organization.name,
        }
        for mapping in user.user_organizations
        if mapping.organization is not None
    ]
    organizations.sort(key=lambda organization: organization["name"])
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "organizations": organizations,
    }


# ──────────────────────────────────────────────────────────────
# 1. POST /api/canonical/{canonical_id}/forward
# ──────────────────────────────────────────────────────────────


def _run_forward_in_background(
    preparation: ForwardPreparation,
    transport: EmailTransport,
    max_retry_count: int,
) -> None:
    """``BackgroundTasks`` 가 호출할 발송 루프 wrapper — 새 session_scope() 로 실행한다.

    라우터의 request-scoped session 은 응답 후 close 되므로, 본 함수는 새
    ``session_scope()`` 를 열어 ``forward_announcement_run`` 을 호출한다.
    sync 함수이므로 FastAPI 가 threadpool 로 실행해 send_with_retry 의
    ``time.sleep(2.0)`` 이 event loop 를 막지 않는다.

    예외 처리 (최종 가드):
        ``forward_announcement_run`` 내부의 개별 ``send_with_retry`` 실패는
        루프가 흡수해 ``failure_count`` 로 집계하므로 정상 흐름에서는 본
        함수의 try/except 까지 도달하지 않는다. 그래도 DB commit 실패 등
        예상치 못한 예외가 나면 ``logger.exception`` + forward_log
        ``status=FAILED`` / ``completed_at = now_utc()`` 로 마무리해
        polling 클라이언트가 무한 대기에 빠지지 않게 한다.

    Args:
        preparation: prepare 단계에서 만들어진 ``ForwardPreparation``. 응답
            직후에도 라우터 session 이 close 되었으므로 본 DTO 가 발송 루프에
            필요한 모든 입력을 담고 있다.
        transport: prepare 단계에서 만든 ``EmailTransport`` 구현체. session
            을 참조하지 않으므로 응답 이후에도 안전하게 재사용 가능.
        max_retry_count: ``send_with_retry`` 에 그대로 넘길 재시도 횟수.
    """
    try:
        with session_scope() as session:
            forward_announcement_run(
                preparation,
                session=session,
                transport=transport,
                max_retry_count=max_retry_count,
            )
    except Exception:
        # 발송 루프 자체의 개별 send 실패는 forward_announcement_run 이
        # 흡수하므로 여기까지 오는 것은 비정상 경로다. forward_log 가 미완료
        # 상태로 남으면 polling 클라이언트가 무한 대기하므로 별도 session
        # 으로 status=FAILED / completed_at 을 기록해 마무리한다.
        logger.exception(
            "포워딩 백그라운드 실행 실패: forward_log_id={}",
            preparation.forward_log_id,
        )
        try:
            with session_scope() as guard_session:
                forward_log = guard_session.get(
                    EmailForwardLog, preparation.forward_log_id
                )
                if (
                    forward_log is not None
                    and forward_log.completed_at is None
                ):
                    forward_log.status = EmailForwardStatus.FAILED
                    forward_log.completed_at = now_utc()
                    guard_session.commit()
        except Exception:
            # 가드마저 실패하면 더 할 수 있는 일이 없다 — 로그만 남긴다.
            logger.exception(
                "포워딩 최종 가드 실패: forward_log_id={}",
                preparation.forward_log_id,
            )


@router.post(
    "/api/canonical/{canonical_id}/forward",
    dependencies=[Depends(ensure_same_origin)],
    status_code=status.HTTP_200_OK,
)
def forward_canonical_route(
    canonical_id: int,
    body: ForwardSendRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """공고 1건을 요청 본문의 수신자 N명에게 메일로 비동기 포워딩한다 (task 00120).

    권한: 로그인 사용자 누구나(``current_user_required``). 조직 멤버일 필요는
    없으나, ``sender_organization_id`` 를 채우려면 본인 소속 조직이어야 한다.

    처리 흐름 (비동기화 이후):
        1. Pydantic ``ForwardSendRequest`` 가 recipients 개수(1~50),
           additional_message 길이(≤5000), subject 길이(≤200)를 1차 검증 —
           위반 시 FastAPI 가 자동으로 422 를 반환한다.
        2. ``canonical_id`` 의 CanonicalProject 존재 확인 — 없으면 404.
        3. ``sender_organization_id`` 가 ``None`` 이 아니면 발송자 본인 소속
           조직인지 검증 — 본인 소속이 아니면 403.
        4. ``build_transport_from_settings`` 로 EmailTransport 생성 —
           SystemSetting 의 transport.type 값이 미지원이면 422 (운영자에게
           설정 오류임을 알린다, admin_email.py 와 동일 정책).
        5. SystemSetting 에서 ``email.max_retry_count`` 를 정수로 읽는다.
        6. ``forward_announcement_prepare`` 를 동기 호출해 forward_log row
           를 INSERT + commit 한다. 이 단계까지의 예외(canonical 없음,
           transport 오류 등)는 4xx/5xx 로 즉시 반환된다.
        7. ``forward_announcement_run`` 을 ``BackgroundTasks`` 에 위임한다 —
           FastAPI 가 응답 직후 threadpool 로 발송 루프를 실행해 event loop
           가 blocking 되지 않는다.
        8. 즉시 200 + ``{forward_log_id, status:'in_progress', success_count:0,
           failure_count:0}`` 로 반환한다. 클라이언트는 forward_log_id 로
           ``GET /api/canonical/{id}/forward-logs/{forward_log_id}`` 를 폴링해
           진행 상황을 표시한다.

    Args:
        canonical_id: 포워딩 대상 공고의 CanonicalProject PK (path 파라미터).
        body: 검증된 ``ForwardSendRequest`` 요청 본문.
        background_tasks: FastAPI 가 주입하는 ``BackgroundTasks`` — 응답 후
            발송 루프를 threadpool 로 실행한다.
        current_user: ``current_user_required`` 가 통과시킨 로그인 User —
            발송자(sender_user)로 기록된다.

    Returns:
        200 + ``{forward_log_id, status:'in_progress', success_count:0,
        failure_count:0}``. 최종 상태는 polling endpoint 로 확인.

    Raises:
        HTTPException(404): ``canonical_id`` 의 CanonicalProject 가 없거나,
            그 canonical 에 현재 유효한 Announcement 가 1건도 없을 때.
        HTTPException(403): ``sender_organization_id`` 가 발송자 본인의 소속
            조직이 아닐 때.
        HTTPException(422): transport 설정 값이 미지원이거나, service 가
            ``ValueError`` 를 던질 때.
        HTTPException(503): 메일 전송 기능 게이트가 off 일 때.
        HTTPException(500): prepare 단계에서 예기치 못한 예외 발생.
    """
    logger.info(
        "POST /api/canonical/{}/forward 진입: user_id={} recipient_count={} "
        "sender_organization_id={}",
        canonical_id,
        current_user.id,
        len(body.recipients),
        body.sender_organization_id,
    )

    # transport / max_retry_count / preparation 은 with 블록 안에서 산출하지만
    # BackgroundTasks 등록은 session 이 close 된 뒤(=200 응답 후)에 실행되므로
    # 본 변수들은 응답 이후에도 살아 있어야 한다. with 종료 후에도 closure 로
    # 참조 가능하도록 외부 변수에 보관한다.
    transport: EmailTransport | None = None
    max_retry_count: int = DEFAULT_EMAIL_MAX_RETRY_COUNT
    preparation: ForwardPreparation | None = None

    with session_scope() as session:
        # 1. canonical 존재 확인 — 없으면 404.
        _ensure_canonical_exists(session, canonical_id)

        # 2. sender_organization_id 본인 소속 검증 — 본인 소속이 아니면 403.
        #    forwarding service 도 방어적으로 한 번 더 검증하지만, 라우터에서
        #    먼저 끊으면 forward_log row 가 만들어지기 전에 차단된다.
        if body.sender_organization_id is not None:
            my_organization_ids = get_user_organization_ids(
                session, current_user.id
            )
            if body.sender_organization_id not in my_organization_ids:
                logger.warning(
                    "포워딩 거부 — 발신 조직 비소속: user_id={} "
                    "sender_organization_id={}",
                    current_user.id,
                    body.sender_organization_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "발신 조직으로 지정할 수 없습니다 — 본인이 소속된 "
                        "조직이 아닙니다."
                    ),
                )

        # 3. transport 구성 — SystemSetting transport.type 이 미지원 값이면
        #    ValueError 가 raise 되므로 422 로 변환한다 (admin_email.py 와
        #    동일 정책 — 운영자에게 설정 입력 오류임을 알린다).
        try:
            transport = build_transport_from_settings(session)
        except ValueError as exc:
            logger.warning(
                "포워딩 실패 — transport 구성 오류: user_id={} error={}",
                current_user.id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

        # 4. max_retry_count 읽기 (SystemSetting + fallback).
        max_retry_count = _read_max_retry_count(session)

        # 5. forward_announcement_prepare 동기 호출 — forward_log INSERT/commit
        #    까지 마치고 forward_log_id 를 확보한다. service 가 던지는 예외는
        #    HTTP 상태로 변환해 즉시 반환한다. 본 단계까지가 동기 4xx/5xx 의
        #    마지막 경계 — 이후 발송 루프 안에서의 개별 실패는 forward_log
        #    row 의 counts 에 집계될 뿐 본 핸들러까지 전파되지 않는다.
        forward_request = ForwardRequest(
            canonical_project_id=canonical_id,
            sender_user_id=current_user.id,
            sender_organization_id=body.sender_organization_id,
            recipients=[str(recipient) for recipient in body.recipients],
            subject=body.subject or "",
            additional_message=body.additional_message,
        )
        try:
            preparation = forward_announcement_prepare(
                forward_request, session=session
            )
        except EmailSendingDisabledError as exc:
            # 메일 전송 기능이 비활성화된 상태 — 503 으로 사용자에게 안내한다.
            logger.warning(
                "포워딩 거부 — 메일 전송 기능 비활성화: user_id={} canonical_id={}",
                current_user.id,
                canonical_id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except LookupError as exc:
            # canonical 없음 / 그 canonical 에 현재 유효한 announcement 없음.
            logger.warning(
                "포워딩 실패 — 대상 조회 실패: user_id={} canonical_id={} error={}",
                current_user.id,
                canonical_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except PermissionError as exc:
            # 발신 조직 비소속 — 라우터에서 선검증했으나 service 방어 경로.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            # 빈 recipients 등 — Pydantic 이 먼저 막아 거의 도달하지 않는다.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            # 발송 루프 시작 전 준비 단계의 예기치 못한 예외 — 5xx 로 전파한다.
            # detail 에는 예외 클래스명 + 메시지만 담는다 (민감 정보 미포함).
            logger.exception(
                "포워딩 실패 — 준비 단계 예외: user_id={} canonical_id={}",
                current_user.id,
                canonical_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"포워딩 처리 중 오류가 발생했습니다: {type(exc).__name__}: {exc}",
            ) from exc

    # 6. 발송 루프는 BackgroundTasks 에 위임. with 블록을 벗어나 session 은
    #    이미 close 된 상태 — _run_forward_in_background 가 새 session_scope()
    #    을 연다. transport 는 session 을 참조하지 않아 응답 이후에도 안전.
    assert preparation is not None
    assert transport is not None
    background_tasks.add_task(
        _run_forward_in_background,
        preparation,
        transport,
        max_retry_count,
    )

    logger.info(
        "포워딩 응답 반환 (백그라운드 발송 등록): user_id={} canonical_id={} "
        "forward_log_id={} recipient_count={}",
        current_user.id,
        canonical_id,
        preparation.forward_log_id,
        len(preparation.recipients),
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "forward_log_id": preparation.forward_log_id,
            "status": FORWARD_STATUS_IN_PROGRESS,
            "success_count": 0,
            "failure_count": 0,
        },
    )


# ──────────────────────────────────────────────────────────────
# 2. GET /api/canonical/{canonical_id}/forward-logs
# ──────────────────────────────────────────────────────────────


@router.get(
    "/api/canonical/{canonical_id}/forward-logs",
    status_code=status.HTTP_200_OK,
)
def list_forward_logs_route(
    canonical_id: int,
    limit: int = Query(
        default=FORWARD_LOGS_LIMIT_DEFAULT,
        ge=1,
        le=FORWARD_LOGS_LIMIT_MAX,
    ),
    current_user: User | None = Depends(current_user_optional),
) -> JSONResponse:
    """공고 1건의 포워딩 발송 이력을 최근순으로 반환한다 (비로그인 허용).

    Phase B/C 의 GET history 와 동일 정책 — 비로그인 사용자도 로그인 사용자와
    동일한 응답을 받는다. 발송 이력 섹션(00109-10)의 데이터 소스다.

    Args:
        canonical_id: 발송 이력을 조회할 CanonicalProject PK (path 파라미터).
        limit: 반환할 최대 row 수. 기본 50, 최대 200 (design note §10).
        current_user: ``current_user_optional`` 결과. 본 응답에는 영향을 주지
            않으며(비로그인도 동일), 의존성으로만 주입한다.

    Returns:
        200 + ``_serialize_forward_log`` 직렬화 결과 list. ``created_at``
        내림차순(최근 발송이 앞). 이력이 없으면 빈 list.

    Raises:
        HTTPException(404): ``canonical_id`` 의 CanonicalProject 가 없을 때.
    """
    # current_user 는 응답에 영향을 주지 않는다 — 비로그인도 동일 응답.
    _ = current_user
    logger.debug(
        "GET /api/canonical/{}/forward-logs 진입: limit={}", canonical_id, limit
    )

    with session_scope() as session:
        _ensure_canonical_exists(session, canonical_id)
        forward_logs = list_forward_logs_for_canonical(
            session, canonical_id, limit=limit
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=[
                _serialize_forward_log(forward_log)
                for forward_log in forward_logs
            ],
        )


# ──────────────────────────────────────────────────────────────
# 3-1. GET /api/canonical/{canonical_id}/forward-logs/{forward_log_id}
# ──────────────────────────────────────────────────────────────


@router.get(
    "/api/canonical/{canonical_id}/forward-logs/{forward_log_id}",
    status_code=status.HTTP_200_OK,
)
def get_forward_log_route(
    canonical_id: int,
    forward_log_id: int,
    current_user: User | None = Depends(current_user_optional),
) -> JSONResponse:
    """발송 이력 1건을 단건 조회한다 — 프론트엔드 polling 용 (task 00120).

    POST /forward 가 BackgroundTasks 로 발송을 위임하므로, 프론트엔드는 응답
    으로 받은 ``forward_log_id`` 를 1초 간격 등으로 폴링해 진행 상황을 표시
    한다. 본 endpoint 는 그 polling 의 데이터 소스다.

    응답 정책:
        - ``_serialize_forward_log`` 를 재사용해 GET /forward-logs (목록) 와
          동일한 dict 스키마를 반환한다 (status 후처리 포함 —
          ``completed_at IS NULL`` 인 row 의 status 는 ``'in_progress'`` 로
          노출된다).
        - ``recipient_addresses`` 는 목록 응답과 동일하게 제외한다 (개별
          수신자 노출은 ``/sends`` expand 응답으로 분리).
        - 권한: ``current_user_optional`` — 비로그인도 동일 응답 (GET 정책
          일관). 모달 안에서 호출되므로 로그인 필요 없음.

    URL path 정합성 검사:
        ``forward_log.canonical_project_id != canonical_id`` 인 경우 다른
        공고의 발송 이력을 들여다보려는 요청이므로 404 로 차단한다
        (``/sends`` endpoint 와 동일 패턴).

    Args:
        canonical_id: 발송 이력이 속한 CanonicalProject PK (path 파라미터).
        forward_log_id: 조회 대상 EmailForwardLog PK (path 파라미터).
        current_user: ``current_user_optional`` 결과. 응답에 영향 없음.

    Returns:
        200 + ``_serialize_forward_log`` 직렬화 결과 dict.

    Raises:
        HTTPException(404): ``canonical_id`` 의 CanonicalProject 가 없거나,
            ``forward_log_id`` 의 EmailForwardLog 가 없거나, 그 forward_log
            가 path 의 ``canonical_id`` 에 속하지 않을 때.
    """
    # current_user 는 응답에 영향을 주지 않는다 — 비로그인도 동일 응답.
    _ = current_user
    logger.debug(
        "GET /api/canonical/{}/forward-logs/{} 진입",
        canonical_id,
        forward_log_id,
    )

    with session_scope() as session:
        _ensure_canonical_exists(session, canonical_id)
        forward_log = session.get(EmailForwardLog, forward_log_id)
        if forward_log is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"발송 이력을 찾을 수 없습니다: forward_log_id={forward_log_id}",
            )
        # path 의 canonical_id 와 forward_log 의 소속 canonical 정합성 검사 —
        # 불일치면 다른 공고의 이력을 들여다보려는 요청이므로 404.
        if forward_log.canonical_project_id != canonical_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"발송 이력을 찾을 수 없습니다: forward_log_id={forward_log_id}",
            )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_serialize_forward_log(forward_log),
        )


# ──────────────────────────────────────────────────────────────
# 3. GET /api/canonical/{canonical_id}/forward-logs/{forward_log_id}/sends
# ──────────────────────────────────────────────────────────────


@router.get(
    "/api/canonical/{canonical_id}/forward-logs/{forward_log_id}/sends",
    status_code=status.HTTP_200_OK,
)
def get_forward_log_sends_route(
    canonical_id: int,
    forward_log_id: int,
    current_user: User | None = Depends(current_user_optional),
) -> JSONResponse:
    """발송 이력 1건의 수신자별 발송 시도 결과를 반환한다 (비로그인 허용).

    발송 이력 행 expand(00109-10)의 데이터 소스다. 한 포워딩은 수신자 N명에게
    1건씩 발송하므로, EmailForwardLog 1건에 EmailSendRun 이 N개 대응한다.

    URL path 정합성 검사:
        ``get_forward_log_with_send_runs`` 자체는 ``forward_log_id`` 만으로
        조회하고 canonical 소속을 검증하지 않는다 (00109-4 의 known_concern).
        다른 공고의 발송 이력을 이 URL 로 들여다보지 못하도록, 조회한
        forward_log 의 ``canonical_project_id`` 가 path 의 ``canonical_id`` 와
        일치하는지 본 핸들러가 확인한다 — 불일치면 404 (``progress.py`` 의
        PATCH/DELETE 와 동일한 path 일관성 강제 패턴).

    Args:
        canonical_id: 발송 이력이 속한 CanonicalProject PK (path 파라미터).
        forward_log_id: 조회 대상 EmailForwardLog PK (path 파라미터).
        current_user: ``current_user_optional`` 결과. 본 응답에는 영향을 주지
            않으며(비로그인도 동일), 의존성으로만 주입한다.

    Returns:
        200 + ``_serialize_send_run`` 직렬화 결과 list. ``created_at``
        오름차순(발송 시도 순서). 매칭되는 EmailSendRun 이 없으면 빈 list.

    Raises:
        HTTPException(404): ``canonical_id`` 의 CanonicalProject 가 없거나,
            ``forward_log_id`` 의 EmailForwardLog 가 없거나, 그 forward_log 가
            path 의 ``canonical_id`` 에 속하지 않을 때.
    """
    # current_user 는 응답에 영향을 주지 않는다 — 비로그인도 동일 응답.
    _ = current_user
    logger.debug(
        "GET /api/canonical/{}/forward-logs/{}/sends 진입",
        canonical_id,
        forward_log_id,
    )

    with session_scope() as session:
        _ensure_canonical_exists(session, canonical_id)
        try:
            forward_log, send_runs = get_forward_log_with_send_runs(
                session, forward_log_id
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        # path 의 canonical_id 와 forward_log 의 소속 canonical 정합성 검사 —
        # 불일치면 다른 공고의 이력을 들여다보려는 요청이므로 404.
        if forward_log.canonical_project_id != canonical_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"발송 이력을 찾을 수 없습니다: forward_log_id={forward_log_id}"
                ),
            )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=[_serialize_send_run(send_run) for send_run in send_runs],
        )


# ──────────────────────────────────────────────────────────────
# 4. GET /api/users/search
# ──────────────────────────────────────────────────────────────


@router.get(
    "/api/users/search",
    status_code=status.HTTP_200_OK,
)
def search_users_route(
    q: str = Query(
        min_length=USER_SEARCH_QUERY_MIN_LENGTH,
        max_length=USER_SEARCH_QUERY_MAX_LENGTH,
    ),
    limit: int = Query(
        default=USER_SEARCH_LIMIT_DEFAULT,
        ge=1,
        le=USER_SEARCH_LIMIT_MAX,
    ),
    current_user: User = Depends(current_user_required),
) -> JSONResponse:
    """수신자 chip 입력의 내부 사용자 자동완성용 검색 결과를 반환한다.

    권한: 로그인 사용자 전용(``current_user_required``) — 자동완성은 로그인
    시에만 제공하므로 비로그인 요청은 401 이다.

    검색 조건 (첨부 prompt §4 / design note §10):
        - ``q`` 가 ``username`` 또는 ``email`` 에 부분 일치(대소문자 무시).
          대소문자 무시 검색은 ``func.lower()`` + ``LIKE`` 로 수행한다
          (PROJECT_NOTES "Migration / ORM 이식성" 컨벤션).
        - ``email IS NOT NULL`` 인 사용자만 — 이메일이 없으면 수신자로 쓸 수
          없으므로 자동완성 결과에서 제외한다.
        - ``User`` 모델에 ``is_active`` 류 컬럼이 없으므로 활성 필터는 적용하지
          않는다 (첨부 prompt: "없으면 무시").
        - 결과는 ``username`` 알파벳순. 정확 일치 우선 정렬은 over-engineering
          으로 보아 도입하지 않는다 (design note §10 결정).

    Args:
        q: 검색어. ``username`` 또는 ``email`` 의 부분 일치 대상. 1 ~ 50 자 —
           위반 시 FastAPI 가 자동으로 422 를 반환한다.
        limit: 반환할 최대 사용자 수. 기본 10, 최대 30 (design note §10).
        current_user: ``current_user_required`` 가 통과시킨 로그인 User.
            응답 내용에는 영향을 주지 않으며, 로그인 여부 게이트로만 쓰인다.

    Returns:
        200 + ``_serialize_user_search_result`` 직렬화 결과 list. 매칭되는
        사용자가 없으면 빈 list.
    """
    # current_user 는 응답에 영향을 주지 않는다 — 로그인 게이트 용도로만 받는다.
    _ = current_user

    # 앞뒤 공백만 입력된 경우(예: "   ")는 Pydantic 길이 검증은 통과하지만
    # 검색어로서 의미가 없으므로, 정규화 후 빈 문자열이면 빈 결과를 반환한다.
    normalized_query = q.strip()
    if not normalized_query:
        return JSONResponse(status_code=status.HTTP_200_OK, content=[])

    logger.debug(
        "GET /api/users/search 진입: q={!r} limit={}", normalized_query, limit
    )

    # func.lower() 로 컬럼 값을, 파이썬 .lower() 로 검색어를 각각 소문자화해
    # 대소문자 무시 부분 일치(LIKE) 를 만든다.
    like_pattern = f"%{normalized_query.lower()}%"

    with session_scope() as session:
        statement = (
            select(User)
            .where(
                User.email.isnot(None),
                or_(
                    func.lower(User.username).like(like_pattern),
                    func.lower(User.email).like(like_pattern),
                ),
            )
            .order_by(User.username)
            .limit(limit)
            # 응답의 organizations 를 채우려면 user_organizations → organization
            # 까지 필요하다. selectinload 2 단으로 N+1 을 회피한다.
            .options(
                selectinload(User.user_organizations).selectinload(
                    UserOrganization.organization
                )
            )
        )
        users = session.execute(statement).scalars().all()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=[_serialize_user_search_result(user) for user in users],
        )


__all__ = ["router"]
