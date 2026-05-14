"""공고 포워딩 service — forward_announcement (Phase A-2 Part 2 / task 00109-3).

설계 근거:
    docs/phase_a2_part2_design_note.md §4 (모듈 위치 결정), §5 (canonical →
    최신 announcement 1건 선택 우선순위), §6 (forward_announcement 의 commit
    경계 명세) + 첨부 phase_a2_part2_prompt.md "백엔드 변경 §2 Forwarding
    service".

본 모듈은 "공고 1건을 N명에게 메일로 포워딩" 하는 1회 액션의 전체 비즈니스
로직을 담당한다. 라우터(00109-5 의 routes/forward.py)는 입력 검증·HTTP 변환만
하고, 트랜잭션 + 수신자 발송 루프 + EmailForwardLog 의 status 집계는 모두 본
모듈의 ``forward_announcement`` 가 수행한다.

책임 분리:
    - 본문 빌드 (multipart/HTML/text) — ``app.email.message_builder`` 의
      빌더 함수에 위임한다. 본 모듈은 빌더에 넘길 입력(announcement,
      sender_user, detail_url 등)을 모으고, 빌드된 EmailMessage 를
      ``send_with_retry`` 로 발송한다.
    - 재시도 + EmailSendRun row 기록 — ``app.email.sender.send_with_retry`` 에
      위임한다. 본 모듈은 수신자별로 그것을 1회씩 호출하고, 성공/실패만
      집계한다.
    - transport 인스턴스화 / max_retry_count 읽기 — 라우터가 미리 만들어
      kwarg 로 주입한다 (admin_email.py 의 A-1 패턴과 동일).

트랜잭션 경계 (design note §6):
    ``send_with_retry`` 가 호출자 session 을 직접 ``commit()`` 한다는 사실
    때문에, 본 모듈은 트랜잭션을 3단계로 나눈다.

    1. forward_log 선(先) commit — EmailForwardLog row 를 INSERT 하고 즉시
       commit 해 ``forward_log.id`` 를 확보한다. 발송 루프 도중 crash 가 나도
       "포워딩 시도가 있었다" 는 사실이 DB 에 남는다.
    2. 수신자별 발송 루프 — 1명씩 ``send_with_retry`` 호출. 이 호출 각각이
       session 을 commit 하지만, 단계 1 에서 이미 commit 했으므로 미커밋
       변경분은 없어 안전하다.
    3. 결과 update commit — 집계된 success/failure count 와 최종 status,
       completed_at 을 forward_log 에 반영하고 commit 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.backup.service import get_setting
from app.db.models import (
    Announcement,
    CanonicalProject,
    EmailForwardLog,
    EmailForwardStatus,
    Organization,
    User,
)
from app.email.constants import (
    DEFAULT_APP_PUBLIC_BASE_URL,
    DEFAULT_EMAIL_FROM_DISPLAY_NAME,
    DEFAULT_EMAIL_M365_SENDER_ADDRESS,
    RELATED_KIND_FORWARD,
    SETTING_KEY_APP_PUBLIC_BASE_URL,
    SETTING_KEY_EMAIL_FROM_DISPLAY_NAME,
    SETTING_KEY_EMAIL_M365_SENDER_ADDRESS,
)
from app.email.message_builder import (
    build_announcement_detail_url,
    build_default_forward_subject,
    build_forward_html_body,
    build_forward_text_body,
    build_multipart_message,
)
from app.email.sender import send_with_retry
from app.email.transport.base import EmailTransport
from app.organizations.service import get_user_organization_ids
from app.sources.constants import SOURCE_TYPE_IRIS, SOURCE_TYPE_NTIS
from app.timezone import now_utc


@dataclass
class ForwardRequest:
    """포워딩 액션 1회의 입력값 (라우터 → service 전달).

    라우터(00109-5)가 Pydantic 요청 body + 인증 컨텍스트(current_user)를
    조합해 본 dataclass 를 만든 뒤 ``forward_announcement`` 에 넘긴다.

    Attributes:
        canonical_project_id: 포워딩 대상 공고의 CanonicalProject PK. 메일
            컨텐츠로 쓸 Announcement 1건을 본 service 가 이 값으로 조회한다.
        sender_user_id: 발송을 트리거한 로그인 사용자 PK.
        sender_organization_id: 발송 시점 발송자의 조직 PK. 무소속/미지정이면
            ``None``. ``None`` 이 아니면 본 service 가 "그 조직이 sender_user
            의 소속인지" 를 방어적으로 검증한다.
        recipients: 수신자 이메일 주소 목록. 빈 리스트면 ``ValueError``.
        subject: 메일 제목. 빈 문자열(또는 공백만)이면 본 service 가
            ``build_default_forward_subject`` 로 자동 생성한다.
        additional_message: 사용자가 입력한 추가 메시지. 본문 빌드에만 쓰이고
            DB 에는 저장되지 않는다 (EmailForwardLog 는 첨부 여부만 boolean 으로
            기록). ``None`` 또는 공백만이면 "추가 메시지 없음" 으로 처리한다.
    """

    canonical_project_id: int
    sender_user_id: int
    sender_organization_id: int | None
    recipients: list[str]
    subject: str
    additional_message: str | None


@dataclass
class ForwardResult:
    """``forward_announcement`` 의 반환값.

    라우터가 그대로 JSON 응답(``{forward_log_id, status, success_count,
    failure_count}``)으로 직렬화한다.

    Attributes:
        forward_log_id: 생성된 EmailForwardLog row 의 PK. 발송 이력 섹션이
            이 id 로 expand 조회를 한다.
        status: 포워딩 전체 결과. 전부 성공이면 ``SUCCESS``, 전부 실패면
            ``FAILED``, 혼재면 ``PARTIAL``.
        success_count: 발송 성공 수신자 수.
        failure_count: 발송 실패 수신자 수.
    """

    forward_log_id: int
    status: EmailForwardStatus
    success_count: int
    failure_count: int


def forward_announcement(
    request: ForwardRequest,
    *,
    session: Session,
    transport: EmailTransport,
    max_retry_count: int,
) -> ForwardResult:
    """공고 1건을 N명의 수신자에게 포워딩하고 EmailForwardLog 이력을 남긴다.

    전체 흐름은 design note §6 의 3단계 commit 경계를 그대로 따른다.

    준비 단계 (forward_log INSERT 이전 — 실패 시 orphan row 없이 예외만 전파):
        1. ``request.recipients`` 가 비어 있으면 ``ValueError``.
        2. ``sender_user_id`` 로 발송자 User 를 로드 (없으면 ``LookupError``).
        3. ``sender_organization_id`` 가 ``None`` 이 아니면, 그 조직이 발송자
           소속인지 검증한다 — 아니면 ``PermissionError`` (라우터가 선검증해도
           service 도 방어적으로 한 번 더 확인). 소속이면 Organization 로드.
        4. ``canonical_project_id`` → ``is_current=True`` Announcement 1건을
           ``_pick_announcement_for_canonical`` 로 확정 (없으면 ``LookupError``).
        5. ``subject`` 가 비어 있으면 ``build_default_forward_subject`` 로 생성.
        6. SystemSetting 에서 공고 상세 URL prefix / 발신 주소 / From 표시명을
           읽어 ``detail_url`` 과 발신자 헤더 값을 확정한다.
        7. text/plain · text/html 본문을 1회 빌드한다 (모든 수신자 동일 본문 —
           수신자별 개인화 없음).

    단계 1 — forward_log 선 commit:
        8. EmailForwardLog row 를 INSERT 한다. ``status`` 는 임시값
           ``EmailForwardStatus.FAILED`` (모델에 default 없음 — 명시 필수),
           ``success_count`` / ``failure_count`` 는 0, ``created_at`` 은
           ``now_utc()``. 그 뒤 ``session.commit()`` 으로 ``forward_log.id`` 를
           확보한다.

    단계 2 — 수신자별 발송 루프:
        9. 수신자 N명을 1명씩 돌며 ``build_multipart_message`` → ``send_with_retry``
           를 호출한다. ``send_with_retry`` 가 모든 시도 실패 시 예외를 raise
           하므로, 수신자별로 ``try/except`` 로 잡아 ``failure_count`` 를 올리고
           다음 수신자로 계속한다 (개별 실패를 전파하지 않는다). 성공이면
           ``success_count`` 를 올린다.

    단계 3 — 결과 update commit:
        10. 집계 결과로 ``status`` 를 결정한다 — 전부 성공 ``SUCCESS`` / 전부
            실패 ``FAILED`` / 혼재 ``PARTIAL``. ``success_count`` /
            ``failure_count`` / ``completed_at = now_utc()`` 를 forward_log 에
            반영하고 ``session.commit()`` 한 뒤 ``ForwardResult`` 를 반환한다.

    Args:
        request: 포워딩 액션 1회의 입력값 (``ForwardRequest``).
        session: SQLAlchemy ORM Session. 본 함수가 내부에서 INSERT/UPDATE 후
            ``commit()`` 을 직접 호출한다 — 라우터는 본 호출이 자기 session 을
            commit 한다는 점을 인지하고 호출해야 한다. ``send_with_retry`` 도
            동일 session 을 commit 한다.
        transport: 발송 실행을 위임할 ``EmailTransport`` 구현체. 라우터가
            ``build_transport_from_settings`` 로 미리 만들어 주입한다.
        max_retry_count: ``send_with_retry`` 에 그대로 넘길 재시도 횟수. 라우터가
            SystemSetting ``email.max_retry_count`` 를 미리 읽어 int 로 전달한다.

    Returns:
        ``ForwardResult`` — forward_log PK + 최종 status + 성공/실패 카운트.

    Raises:
        ValueError: ``recipients`` 가 빈 리스트일 때.
        LookupError: ``sender_user_id`` 에 해당하는 User 가 없거나,
            ``canonical_project_id`` 가 존재하지 않거나, 그 canonical 에
            ``is_current=True`` Announcement 가 1건도 없을 때.
        PermissionError: ``sender_organization_id`` 가 ``None`` 이 아닌데
            발송자(sender_user)의 소속 조직이 아닐 때.
        Exception: 발송 루프 시작 전 SystemSetting 읽기 / 본문 빌드 단계에서
            발생한 예외는 그대로 전파된다 (이 시점엔 forward_log row 가 아직
            INSERT 되지 않았으므로 정리할 것이 없다).
    """
    # ── 준비 1: 빈 수신자 방어 ────────────────────────────────────
    if not request.recipients:
        raise ValueError("발송 대상자가 없습니다.")

    # ── 준비 2: 발송자 User 로드 ──────────────────────────────────
    # 라우터가 current_user_required 로 선검증하지만, sender_user 가
    # 없으면 본문 빌더(_format_sender_display)가 AttributeError 로 모호하게
    # 죽으므로 여기서 명확한 LookupError 로 끊는다.
    sender_user = session.get(User, request.sender_user_id)
    if sender_user is None:
        raise LookupError(
            f"발송자 사용자를 찾을 수 없습니다: sender_user_id={request.sender_user_id}"
        )

    # ── 준비 3: 발신 조직 검증 + 로드 ─────────────────────────────
    sender_organization: Organization | None = None
    if request.sender_organization_id is not None:
        # 발신 조직으로 지정할 수 있는 것은 본인 소속 조직뿐이다. 라우터가
        # 선검증하더라도 service 도 방어적으로 한 번 더 확인한다 (design note §6).
        member_org_ids = get_user_organization_ids(session, request.sender_user_id)
        if request.sender_organization_id not in member_org_ids:
            raise PermissionError(
                "발신 조직으로 지정할 수 없습니다 — 본인이 소속된 조직이 "
                f"아닙니다: sender_organization_id={request.sender_organization_id}"
            )
        sender_organization = session.get(
            Organization, request.sender_organization_id
        )
        if sender_organization is None:
            # 소속 매핑은 있는데 조직 row 가 없는 비정상 상태 (FK 정합성 깨짐).
            raise LookupError(
                "발신 조직을 찾을 수 없습니다: "
                f"sender_organization_id={request.sender_organization_id}"
            )

    # ── 준비 4: canonical → 메일 컨텐츠로 쓸 announcement 1건 확정 ──
    canonical_project = session.get(
        CanonicalProject, request.canonical_project_id
    )
    if canonical_project is None:
        raise LookupError(
            f"공고를 찾을 수 없습니다: canonical_project_id={request.canonical_project_id}"
        )
    announcement = _pick_announcement_for_canonical(
        session, request.canonical_project_id
    )
    if announcement is None:
        raise LookupError(
            "해당 공고에 현재 유효한 announcement 가 없습니다: "
            f"canonical_project_id={request.canonical_project_id}"
        )

    # ── 준비 5: 제목 확정 (빈 값이면 default 생성) ────────────────
    subject = request.subject.strip()
    if not subject:
        subject = build_default_forward_subject(announcement.title)

    # ── 준비 6: SystemSetting 으로 detail_url / 발신자 헤더 확정 ───
    # row 가 없으면 코드 fallback 상수 사용 (다른 email.* 키와 동일 패턴).
    public_base_url = (
        get_setting(session, SETTING_KEY_APP_PUBLIC_BASE_URL)
        or DEFAULT_APP_PUBLIC_BASE_URL
    )
    detail_url = build_announcement_detail_url(public_base_url, announcement.id)
    sender_address = (
        get_setting(session, SETTING_KEY_EMAIL_M365_SENDER_ADDRESS)
        or DEFAULT_EMAIL_M365_SENDER_ADDRESS
    )
    sender_display_name = (
        get_setting(session, SETTING_KEY_EMAIL_FROM_DISPLAY_NAME)
        or DEFAULT_EMAIL_FROM_DISPLAY_NAME
    )

    # ── 준비 7: 본문 1회 빌드 (모든 수신자 동일 본문) ─────────────
    text_body = build_forward_text_body(
        announcement=announcement,
        additional_message=request.additional_message,
        sender_user=sender_user,
        sender_organization=sender_organization,
        detail_url=detail_url,
    )
    html_body = build_forward_html_body(
        announcement=announcement,
        additional_message=request.additional_message,
        sender_user=sender_user,
        sender_organization=sender_organization,
        detail_url=detail_url,
    )

    # ── 단계 1: forward_log 선 commit ─────────────────────────────
    has_additional_message = bool((request.additional_message or "").strip())
    forward_log = EmailForwardLog(
        canonical_project_id=request.canonical_project_id,
        sender_user_id=request.sender_user_id,
        sender_organization_id=request.sender_organization_id,
        subject=subject,
        has_additional_message=has_additional_message,
        recipient_addresses=list(request.recipients),
        recipient_count=len(request.recipients),
        # status 는 임시값 — 단계 3 에서 실제 결과로 덮어쓴다. 모델에 default
        # 가 없어 명시가 필수다.
        status=EmailForwardStatus.FAILED,
        success_count=0,
        failure_count=0,
        created_at=now_utc(),
    )
    session.add(forward_log)
    # forward_log.id 를 확보하고, 이후 발송 루프 중 crash 가 나도 "포워딩
    # 시도가 있었다" 는 사실이 DB 에 남도록 즉시 commit 한다.
    session.commit()
    logger.info(
        "공고 포워딩 시작: forward_log_id={} canonical_project_id={} "
        "recipient_count={} sender_user_id={}",
        forward_log.id,
        request.canonical_project_id,
        len(request.recipients),
        request.sender_user_id,
    )

    # ── 단계 2: 수신자별 발송 루프 (1명씩) ────────────────────────
    success_count = 0
    failure_count = 0
    for recipient in request.recipients:
        message = build_multipart_message(
            sender_address=sender_address,
            sender_display_name=sender_display_name,
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        try:
            # send_with_retry 가 내부에서 session 을 commit 한다. 단계 1 에서
            # 이미 commit 했으므로 이 시점 미커밋 변경분은 없어 안전하다.
            send_with_retry(
                transport,
                message,
                max_retry_count=max_retry_count,
                related_kind=RELATED_KIND_FORWARD,
                related_id=forward_log.id,
                requested_by_user_id=request.sender_user_id,
                session=session,
            )
        except Exception as exc:
            # 개별 수신자 발송 실패는 전파하지 않는다 — EmailSendRun row 는
            # send_with_retry 가 이미 'failed' 로 commit 해 두었고, 본 루프는
            # 카운트만 올리고 다음 수신자로 계속한다 (design note §6 단계 2).
            failure_count += 1
            logger.warning(
                "공고 포워딩 개별 발송 실패: forward_log_id={} recipient={!r} "
                "error={}: {}",
                forward_log.id,
                recipient,
                type(exc).__name__,
                exc,
            )
            continue
        success_count += 1

    # ── 단계 3: 결과 update commit ────────────────────────────────
    status = _decide_forward_status(success_count, failure_count)
    forward_log.status = status
    forward_log.success_count = success_count
    forward_log.failure_count = failure_count
    forward_log.completed_at = now_utc()
    session.commit()
    logger.info(
        "공고 포워딩 완료: forward_log_id={} status={} success_count={} "
        "failure_count={}",
        forward_log.id,
        status.value,
        success_count,
        failure_count,
    )

    return ForwardResult(
        forward_log_id=forward_log.id,
        status=status,
        success_count=success_count,
        failure_count=failure_count,
    )


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _pick_announcement_for_canonical(
    session: Session,
    canonical_project_id: int,
) -> Announcement | None:
    """canonical 1건의 메일 컨텐츠로 쓸 Announcement 를 고른다.

    같은 canonical 에 ``is_current=True`` 인 Announcement 가 IRIS·NTIS 양쪽에
    존재할 수 있으므로, design note §5 의 우선순위로 1건을 고른다:

    1. ``canonical_group_id == canonical_project_id`` **AND** ``is_current``
       인 row 만 후보.
    2. 정렬: **source priority (IRIS 우선 → NTIS → 그 외) → ``scraped_at``
       내림차순**. IRIS 가 1차 수집원이고 상세 본문 보유율이 높다는 근거.
    3. 첫 row 를 반환. 후보가 0건이면 ``None``.

    우선순위를 바꾸고 싶으면(예: NTIS 우선, 또는 수집일만으로) 본 함수의
    정렬 키만 수정하면 된다 — 변경 비용을 본 헬퍼 한 곳에 격리한다.

    Args:
        session: 호출자 세션 (read-only 로 사용).
        canonical_project_id: 후보를 찾을 CanonicalProject PK.

    Returns:
        우선순위상 첫 ``Announcement``. 후보가 없으면 ``None``.
    """
    # source_type 별 정렬 가중치 — 작을수록 우선. IRIS=0, NTIS=1, 그 외=2.
    source_priority = case(
        (Announcement.source_type == SOURCE_TYPE_IRIS, 0),
        (Announcement.source_type == SOURCE_TYPE_NTIS, 1),
        else_=2,
    )
    statement = (
        select(Announcement)
        .where(
            Announcement.canonical_group_id == canonical_project_id,
            Announcement.is_current.is_(True),
        )
        .order_by(source_priority, Announcement.scraped_at.desc())
        .limit(1)
    )
    return session.execute(statement).scalars().first()


def _decide_forward_status(
    success_count: int,
    failure_count: int,
) -> EmailForwardStatus:
    """성공/실패 카운트로 포워딩 전체 결과 status 를 결정한다.

    규칙 (design note §6 단계 3):
        - 실패가 0건이면 ``SUCCESS`` (= 모든 수신자 성공).
        - 성공이 0건이면 ``FAILED`` (= 모든 수신자 실패).
        - 그 외 (성공·실패 혼재) 는 ``PARTIAL``.

    수신자가 1명 이상임은 호출자(``forward_announcement``)가 빈 recipients 를
    ``ValueError`` 로 이미 막았으므로, ``success_count + failure_count >= 1``
    이 보장된다.

    Args:
        success_count: 발송 성공 수신자 수.
        failure_count: 발송 실패 수신자 수.

    Returns:
        ``EmailForwardStatus`` — ``SUCCESS`` / ``FAILED`` / ``PARTIAL`` 중 하나.
    """
    if failure_count == 0:
        return EmailForwardStatus.SUCCESS
    if success_count == 0:
        return EmailForwardStatus.FAILED
    return EmailForwardStatus.PARTIAL


__all__ = [
    "ForwardRequest",
    "ForwardResult",
    "forward_announcement",
]
