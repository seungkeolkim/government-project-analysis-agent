"""EmailMessage 빌더 — plain text (Phase A-1) + multipart/HTML (Phase A-2 Part 2).

Phase A-1 (task 00104-7):
    ``build_plain_text_message`` — To / Subject / (옵션) From 헤더를 채우고
    ``set_content(body)`` 로 plain text 본문만 가진 EmailMessage 를 반환한다.

Phase A-2 Part 2 (task 00109-2, docs/phase_a2_part2_design_note.md §3·§7·§8):
    공고 포워딩 메일용 빌더 4종을 추가한다.

    - ``build_multipart_message`` — text/plain + text/html 두 alternative 를
      모두 가진 multipart/alternative EmailMessage. 수신자 MUA 가 HTML 을
      렌더하지 못하면 text 본문이, 렌더하면 HTML 본문이 표시된다.
    - ``build_default_forward_subject`` — 포워딩 기본 제목 문자열.
    - ``build_forward_text_body`` / ``build_forward_html_body`` — 공고 메타 +
      추가 메시지 + CTA + footer 로 구성된 포워딩 본문 (각각 plain / HTML).
    - ``build_announcement_detail_url`` — public_base_url + 공고 id 로 메일
      본문에 넣을 상세 페이지 URL 을 조립한다.

비-범위 (다른 subtask 또는 후속 phase 의 책임):
    - 첨부 파일 / cc / bcc 헤더 추가 금지 (첨부 문서 \"핵심 결정\" 섹션).
    - 발송 실제 수행 / 재시도 / 이력 기록 — 본 모듈은 EmailMessage 객체만
      반환하며 transport / sender 가 발송한다.
    - 수신자 N명 To 헤더 묶음 발송 금지 — ``build_multipart_message`` 의
      ``recipient`` 은 단일 주소이며, 호출자(forwarding service)가 수신자별로
      1건씩 발송한다 (프라이버시 + per-recipient 추적, design note §6).
    - public_base_url SystemSetting row 의 실제 read — forwarding service
      (00109-3) 가 ``get_setting`` 으로 읽어 ``detail_url`` 을 조립해 본 모듈의
      본문 빌더에 넘긴다. 본 모듈은 조립 헬퍼만 제공한다.
    - ``build_plain_text_message`` 의 From 헤더 자동 채움 — ``sender=None`` 으로
      호출하면 본 빌더는 From 을 비워두고 ``M365OAuthSmtpTransport.send``
      (00104-5) 의 ``_fill_from_header_if_empty`` 에 위임한다. 반면
      ``build_multipart_message`` 는 호출자가 넘긴 ``sender_address`` /
      ``sender_display_name`` 으로 From 을 직접 채운다 (포워딩은 발신자 정보가
      항상 확정되어 있으므로 transport 자동 채움에 의존하지 않는다).
"""

from __future__ import annotations

import html
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from app.timezone import format_kst, now_kst, to_kst


def build_plain_text_message(
    *,
    recipient: str,
    subject: str,
    body: str,
    sender: str | None = None,
) -> EmailMessage:
    """plain text 본문을 가진 EmailMessage 를 만든다.

    헤더와 본문은 모두 UTF-8 로 인코딩된다 (``EmailMessage.set_content``
    기본 동작). 본문 길이/형식 검증은 본 함수가 수행하지 않는다 — 호출자
    (admin API 의 Pydantic schema, subtask 00104-9) 가 길이 / 비어 있음 등을
    이미 검증한 상태로 본 함수에 전달한다.

    Args:
        recipient: 수신자 메일 주소. ``To`` 헤더에 그대로 들어간다.
            단일 주소만 허용 — cc / bcc 는 Phase A-1 범위 밖이므로 본 빌더
            인터페이스에서 노출하지 않는다.
        subject: 메일 제목. ``Subject`` 헤더에 그대로 들어간다. EmailMessage
            가 비-ASCII 문자를 RFC 2047 ``=?utf-8?...?=`` 형식으로 자동
            인코딩한다.
        body: 메일 본문 (plain text). ``set_content(body)`` 로 채워진다.
            HTML 마크업이 들어와도 본 빌더는 plain text 로만 취급한다.
        sender: From 헤더 값. ``None`` (default) 이면 From 헤더를 채우지
            않고 transport 의 자동 채움 로직 (``M365OAuthSmtpTransport`` 의
            ``_fill_from_header_if_empty``, 00104-5) 에 위임한다. 호출자가
            SystemSetting 과 무관한 발신자를 명시하려면 문자열로 전달
            (예: ``\"홍길동 <hong@example.com>\"`` 또는 ``\"hong@example.com\"``).

    Returns:
        헤더와 본문이 채워진 ``email.message.EmailMessage`` 인스턴스. 호출자
        (sender layer) 가 그대로 ``transport.send(message)`` 에 넘긴다.
    """
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    # sender 가 명시되면 From 헤더 채움. None 이면 transport 의 자동 채움 로직에
    # 맡긴다 (M365OAuthSmtpTransport._fill_from_header_if_empty 가 SystemSetting
    # 의 from_display_name + sender_address 조합으로 채움).
    if sender is not None:
        message["From"] = sender
    # plain text 본문 설정. set_content 의 기본 동작:
    #   - Content-Type: text/plain; charset=\"utf-8\"
    #   - Content-Transfer-Encoding: 8bit (또는 7bit / quoted-printable, 본문 내용에 따라)
    # HTML+plain multipart 는 본 빌더에서 의도적으로 만들지 않는다 (A-2 책임).
    message.set_content(body)
    return message


# ──────────────────────────────────────────────────────────────
# Phase A-2 Part 2 — 공고 포워딩 메일 빌더 (task 00109-2)
# ──────────────────────────────────────────────────────────────


# 포워딩 기본 제목의 고정 prefix. design note §11-5 의 default 문구 —
# 변경이 필요하면 본 상수 한 곳만 수정한다.
_FORWARD_SUBJECT_PREFIX: str = "[정부사업 모니터링] 공고 검토 요청: "

# 기본 제목에 들어가는 공고 제목의 최대 글자 수. 초과분은 truncate 한다.
# prefix(약 21자) + 100자 + 말줄임표 1자 = 약 122자로 EmailForwardLog.subject
# 컬럼(String(200)) 안에 안전하게 들어간다.
_FORWARD_SUBJECT_TITLE_MAX_LENGTH: int = 100

# 공고 요약(summary 또는 detail_text)을 메일 본문에 넣을 때의 최대 글자 수.
# 초과 시 잘라내고 말줄임표를 붙인다.
_FORWARD_SUMMARY_MAX_LENGTH: int = 300

# raw_metadata 에서 예산 값을 찾을 때 시도하는 키 후보. 소스(IRIS/NTIS)별로
# 키 이름이 제각각이라 best-effort 로 몇 개만 확인하고, 없으면 예산 행을
# 생략한다 (design note §8 — 무리하게 raw_metadata 를 파싱하지 않는다).
_BUDGET_METADATA_KEYS: tuple[str, ...] = (
    "budget",
    "예산",
    "사업비",
    "지원금액",
    "사업금액",
    "총사업비",
)


def build_announcement_detail_url(public_base_url: str, announcement_id: int) -> str:
    """공고 상세 페이지의 절대 URL 을 조립한다.

    메일 본문(텍스트/HTML)과 CTA 버튼이 가리킬 링크다. forwarding service
    (00109-3)가 SystemSetting ``app.public_base_url`` 값(없으면
    ``DEFAULT_APP_PUBLIC_BASE_URL``)을 ``public_base_url`` 로 넘긴다.

    Args:
        public_base_url: 시스템의 외부 노출 base URL. 끝에 ``/`` 가 있어도
            없어도 동일하게 처리된다.
        announcement_id: 링크 대상 ``Announcement`` 의 내부 PK.

    Returns:
        ``{base}/announcements/{id}`` 형태의 절대 URL 문자열.
    """
    # base URL 끝의 슬래시는 제거해 ``//announcements`` 처럼 슬래시가 겹치는
    # 것을 막는다.
    return f"{public_base_url.rstrip('/')}/announcements/{announcement_id}"


def build_default_forward_subject(announcement_title: str) -> str:
    """포워딩 메일의 기본 제목 문자열을 만든다.

    형식은 ``[정부사업 모니터링] 공고 검토 요청: {title}``. 모달 진입 시
    placeholder/default value 로 쓰이며, 사용자가 제목을 비워서 발송하면
    서버(forwarding service)가 본 함수로 자동 생성한다.

    공고 제목이 ``_FORWARD_SUBJECT_TITLE_MAX_LENGTH`` 자를 넘으면 잘라내고
    말줄임표(``…``)를 붙인다 — ``EmailForwardLog.subject`` 컬럼이
    ``String(200)`` 이라 제목이 지나치게 길면 저장이 잘릴 수 있기 때문이다.

    Args:
        announcement_title: 공고 제목 (``Announcement.title``).

    Returns:
        prefix 와 (필요 시 truncate 된) 제목을 결합한 제목 문자열.
    """
    title = announcement_title.strip()
    if len(title) > _FORWARD_SUBJECT_TITLE_MAX_LENGTH:
        # 잘라낸 뒤 끝에 남은 공백을 정리하고 말줄임표를 붙인다.
        title = title[:_FORWARD_SUBJECT_TITLE_MAX_LENGTH].rstrip() + "…"
    return f"{_FORWARD_SUBJECT_PREFIX}{title}"


def build_multipart_message(
    *,
    sender_address: str,
    sender_display_name: str,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> EmailMessage:
    """HTML + plain text multipart/alternative 메일을 만든다.

    ``set_content(text_body)`` 로 text/plain 본문을 먼저 채운 뒤
    ``add_alternative(html_body, subtype="html")`` 로 text/html 대체본을
    추가한다. 이 순서로 EmailMessage 는 multipart/alternative 컨테이너가 되며,
    수신자 MUA 는 HTML 을 렌더하지 못하면 ``text_body`` 를, 렌더할 수 있으면
    뒤쪽(우선순위가 높은) ``html_body`` 를 표시한다.

    ``build_plain_text_message`` 와 달리 From 헤더를 본 함수가 직접 채운다.
    포워딩 메일은 발신자 정보(SystemSetting 의 sender_address /
    from_display_name)가 항상 확정되어 있어 transport 의 자동 채움 로직에
    의존할 필요가 없기 때문이다.

    Args:
        sender_address: 발신 mailbox 주소. From 헤더의 주소 부분.
        sender_display_name: From 헤더에 표시될 이름. 비-ASCII 문자가 있으면
            ``formataddr`` 가 RFC 2047 형식으로 자동 인코딩한다.
        recipient: 수신자 메일 주소 — **단일 주소**. 수신자가 N명이면 호출자
            (forwarding service)가 본 함수를 N번 호출해 1건씩 발송한다
            (수신자 프라이버시 + per-recipient 발송 추적, design note §6).
        subject: 메일 제목. Subject 헤더에 그대로 들어간다.
        text_body: text/plain 대체본 본문. ``build_forward_text_body`` 의
            반환값을 넘긴다.
        html_body: text/html 대체본 본문. ``build_forward_html_body`` 의
            반환값을 넘긴다.

    Returns:
        From / To / Subject 헤더가 채워지고 text/plain + text/html 두
        alternative 를 가진 multipart/alternative ``EmailMessage``.
    """
    message = EmailMessage()
    # formataddr 는 display name 에 비-ASCII(한글)가 있으면 RFC 2047
    # =?utf-8?...?= 형식으로 자동 인코딩한다.
    message["From"] = formataddr((sender_display_name, sender_address))
    message["To"] = recipient
    message["Subject"] = subject
    # set_content 로 text/plain 을 먼저 채운 뒤 add_alternative 로 text/html 을
    # 더하면 EmailMessage 가 multipart/alternative 가 된다. MUA 는 마지막에
    # 추가된(= 가장 풍부한) alternative 를 우선 렌더하므로 html_body 가 우선.
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def _format_announcement_status(announcement: Any) -> str:
    """공고 접수 상태를 사람이 읽는 한글 문자열로 변환한다.

    ``Announcement.status`` 는 ``AnnouncementStatus`` enum 이며 그 ``value`` 가
    이미 한글('접수중'/'접수예정'/'마감')이다. enum 이 아닌 값이 들어와도
    문자열로 안전하게 떨어지도록 ``getattr`` 로 방어한다.

    Args:
        announcement: ``Announcement`` ORM 인스턴스.

    Returns:
        접수 상태 한글 문자열. 알 수 없으면 ``"-"``.
    """
    status = announcement.status
    if status is None:
        return "-"
    # AnnouncementStatus(StrEnum) 는 .value 가 한글 원문. enum 이 아니면
    # str() 로 그대로 떨어진다.
    return getattr(status, "value", str(status))


def _format_deadline_with_dday(announcement: Any) -> str | None:
    """마감일을 ``YYYY-MM-DD (D-N)`` 형태의 표시 문자열로 만든다.

    ``deadline_at`` 이 ``None`` 이면 마감일 정보가 없는 것이므로 ``None`` 을
    반환하고, 호출 측에서 마감일 행을 생략한다. D-N 의 N 은 KST 기준 오늘
    날짜와 마감 날짜의 일수 차이다 (시각이 아닌 날짜 단위 비교 — 사용자가
    "며칠 남았는지"를 직관적으로 읽도록).

    Args:
        announcement: ``Announcement`` ORM 인스턴스.

    Returns:
        ``"2026-05-20 (D-6)"`` 같은 문자열. 마감일이 없으면 ``None``.
    """
    deadline_at = announcement.deadline_at
    if deadline_at is None:
        return None

    # 저장값(UTC, SQLite 는 naive 로 돌려줌)을 KST tz-aware 로 정규화한 뒤
    # 날짜 부분만 비교한다.
    deadline_kst = to_kst(deadline_at)
    assert deadline_kst is not None  # deadline_at 이 None 이 아니므로 항상 datetime
    date_str = format_kst(deadline_at, "%Y-%m-%d")
    days_left = (deadline_kst.date() - now_kst().date()).days

    if days_left > 0:
        return f"{date_str} (D-{days_left})"
    if days_left == 0:
        return f"{date_str} (D-Day)"
    # 마감일이 이미 지난 경우. 음수 D-N 대신 '마감' 으로 표기한다.
    return f"{date_str} (마감)"


def _extract_budget(announcement: Any) -> str | None:
    """공고 예산 값을 ``raw_metadata`` 에서 best-effort 로 추출한다.

    ``Announcement`` 모델에 예산 전용 컬럼이 없어, 소스가 ``raw_metadata``
    JSON 에 흘려보낸 값을 후보 키 몇 개로만 확인한다. 어느 키에도 값이 없으면
    ``None`` 을 반환하고 호출 측에서 예산 행을 생략한다 — raw_metadata 를
    무리하게 파싱하지 않는다 (design note §8).

    Args:
        announcement: ``Announcement`` ORM 인스턴스.

    Returns:
        예산 표시 문자열. 확인되지 않으면 ``None``.
    """
    metadata = announcement.raw_metadata or {}
    if not isinstance(metadata, dict):
        return None
    for key in _BUDGET_METADATA_KEYS:
        value = metadata.get(key)
        if value:
            text = str(value).strip()
            if text:
                return text
    return None


def _extract_summary(announcement: Any) -> str | None:
    """메일 본문에 넣을 공고 요약 텍스트를 추출한다.

    우선순위: ``announcement.summary`` (있는 경우) → ``announcement.detail_text``
    (상세 페이지 본문에서 추출한 가독성 텍스트). 둘 다 비어 있으면 ``None`` 을
    반환하고 호출 측에서 요약 섹션을 통째로 생략한다. 길이가
    ``_FORWARD_SUMMARY_MAX_LENGTH`` 자를 넘으면 잘라내고 말줄임표를 붙인다.

    Args:
        announcement: ``Announcement`` ORM 인스턴스.

    Returns:
        (필요 시 truncate 된) 요약 문자열. 본문이 없으면 ``None``.
    """
    # summary 컬럼은 현재 모델에 없을 수 있어 getattr 로 방어한다.
    raw = getattr(announcement, "summary", None) or announcement.detail_text
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) > _FORWARD_SUMMARY_MAX_LENGTH:
        return text[:_FORWARD_SUMMARY_MAX_LENGTH].rstrip() + "..."
    return text


def _format_sender_display(sender_user: Any, sender_organization: Any) -> str:
    """발송자 정보를 footer 한 줄용 표시 문자열로 만든다.

    형식: ``{username} <{email}> ({조직명})``. email 이 없으면 ``<...>`` 부분을
    생략하고, 발신 조직이 없으면(무소속 발송) ``(개인)`` 으로 표기한다.

    Args:
        sender_user: 발송자 ``User`` ORM 인스턴스.
        sender_organization: 발신 조직 ``Organization`` 인스턴스 또는 ``None``.

    Returns:
        발송자 표시 문자열.
    """
    parts = [sender_user.username]
    if sender_user.email:
        parts.append(f"<{sender_user.email}>")
    base = " ".join(parts)
    if sender_organization is not None:
        return f"{base} ({sender_organization.name})"
    return f"{base} (개인)"


def build_forward_text_body(
    *,
    announcement: Any,
    additional_message: str | None,
    sender_user: Any,
    sender_organization: Any,
    detail_url: str,
) -> str:
    """포워딩 메일의 text/plain 본문을 만든다.

    multipart/alternative 의 text 대체본용 — HTML 을 렌더하지 못하는 MUA 가
    표시한다. 첨부 prompt '#### plain text 본문 구성' 레이아웃을 그대로 따른다:
    공고 메타(제목/발주기관/상태/마감일/예산) → 공고 요약 → 보낸 사람 메시지
    → 상세 링크 → footer. 예산·요약·보낸 사람 메시지는 값이 없으면 해당
    구획을 생략한다.

    Args:
        announcement: 메일 컨텐츠로 쓸 ``Announcement`` ORM 인스턴스.
        additional_message: 사용자가 입력한 추가 메시지. 비어 있으면 보낸
            사람 메시지 구획을 생략한다 (메일 본문 빌드 후 저장되지 않음).
        sender_user: 발송자 ``User`` ORM 인스턴스.
        sender_organization: 발신 조직 ``Organization`` 또는 ``None``.
        detail_url: 공고 상세 페이지 절대 URL
            (``build_announcement_detail_url`` 의 반환값).

    Returns:
        plain text 본문 문자열.
    """
    lines: list[str] = ["[공고 검토 요청]", ""]

    # ── 공고 메타 ────────────────────────────────────────────────
    lines.append(f"공고: {announcement.title}")
    lines.append(f"발주기관: {announcement.agency or '-'}")
    lines.append(f"상태: {_format_announcement_status(announcement)}")
    deadline = _format_deadline_with_dday(announcement)
    if deadline is not None:
        lines.append(f"마감일: {deadline}")
    budget = _extract_budget(announcement)
    if budget is not None:
        lines.append(f"예산: {budget}")

    # ── 공고 요약 (본문이 없으면 구획 생략) ──────────────────────
    summary = _extract_summary(announcement)
    if summary is not None:
        lines.extend(["", "[공고 요약]", summary])

    # ── 보낸 사람 메시지 (없으면 구획 생략) ──────────────────────
    message_text = (additional_message or "").strip()
    if message_text:
        lines.extend(["", "[보낸 사람 메시지]", message_text])

    # ── 상세 링크 + footer ───────────────────────────────────────
    lines.extend(
        [
            "",
            "---",
            f"공고 상세 보기: {detail_url}",
            "",
            "이 메일은 정부사업 모니터링 시스템에서 발송되었습니다.",
            f"보낸 사람: {_format_sender_display(sender_user, sender_organization)}",
            "회신은 발송자에게 직접 부탁드립니다.",
        ]
    )
    # 마지막 줄에 개행 1개를 둬 MUA 표시가 깔끔하도록 한다.
    return "\n".join(lines) + "\n"


def build_forward_html_body(
    *,
    announcement: Any,
    additional_message: str | None,
    sender_user: Any,
    sender_organization: Any,
    detail_url: str,
) -> str:
    """포워딩 메일의 text/html 본문을 만든다.

    multipart/alternative 의 HTML 대체본용. design note §8 의 인라인 CSS
    mockup 을 그대로 구현한다 — 외부 CDN/폰트 금지, system-ui/sans-serif
    fallback, 최대 너비 600px 1열 레이아웃, 중립 grayscale 색상
    (텍스트 #333 / 메타 박스 #f5f5f5 / CTA 버튼 #444 / 보조 텍스트 #888·#999).

    본문 요소(위→아래): 공고 제목(h2, 상세 링크) → 메타 박스(발주기관/상태/
    마감일/예산) → 공고 요약 → 보낸 사람 메시지 인용 박스 → CTA 버튼 →
    footer. 예산·요약·보낸 사람 메시지는 값이 없으면 해당 요소를 생략한다.

    모든 외부 입력(공고 제목·기관·요약·추가 메시지·발송자 정보·URL)은
    ``html.escape`` 로 이스케이프해 HTML injection 을 막는다.

    Args:
        announcement: 메일 컨텐츠로 쓸 ``Announcement`` ORM 인스턴스.
        additional_message: 사용자가 입력한 추가 메시지. 비어 있으면 인용
            박스를 생략한다.
        sender_user: 발송자 ``User`` ORM 인스턴스.
        sender_organization: 발신 조직 ``Organization`` 또는 ``None``.
        detail_url: 공고 상세 페이지 절대 URL.

    Returns:
        인라인 CSS 로 스타일링된 완결 HTML 문서 문자열.
    """
    # 모든 동적 값은 escape 한다. detail_url 은 href 속성에도 들어가므로
    # quote=True 로 따옴표까지 이스케이프한다.
    safe_title = html.escape(announcement.title)
    safe_detail_url = html.escape(detail_url, quote=True)
    safe_agency = html.escape(announcement.agency or "-")
    safe_status = html.escape(_format_announcement_status(announcement))

    # ── 메타 박스 행 (발주기관·상태는 항상, 마감일·예산은 값 있을 때만) ──
    meta_rows = [
        _html_meta_row("발주기관", safe_agency),
        _html_meta_row("상태", safe_status),
    ]
    deadline = _format_deadline_with_dday(announcement)
    if deadline is not None:
        meta_rows.append(_html_meta_row("마감일", html.escape(deadline)))
    budget = _extract_budget(announcement)
    if budget is not None:
        meta_rows.append(_html_meta_row("예산", html.escape(budget)))

    # ── 공고 요약 (본문이 없으면 빈 문자열 → 섹션 생략) ──────────
    summary = _extract_summary(announcement)
    summary_html = ""
    if summary is not None:
        summary_html = (
            '<div style="margin:16px 0;font-size:14px;white-space:pre-line;">'
            f"{html.escape(summary)}</div>"
        )

    # ── 보낸 사람 메시지 인용 박스 (없으면 생략) ─────────────────
    message_text = (additional_message or "").strip()
    message_html = ""
    if message_text:
        message_html = (
            '<div style="border-left:4px solid #888;padding:8px 14px;'
            'margin:16px 0;background:#fafafa;font-size:14px;'
            'white-space:pre-line;">'
            '<div style="color:#888;font-size:12px;margin-bottom:4px;">'
            "보낸 사람 메시지</div>"
            f"{html.escape(message_text)}</div>"
        )

    safe_sender = html.escape(
        _format_sender_display(sender_user, sender_organization)
    )

    # design note §8 mockup 그대로. 전부 인라인 style, 외부 리소스 없음.
    return (
        "<!DOCTYPE html>"
        '<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "</head><body style=\"margin:0;padding:0;background:#ffffff;\">"
        '<div style="max-width:600px;margin:0 auto;padding:24px;'
        "font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        'color:#333;line-height:1.6;">'
        # 1. 공고 제목 (h2, 상세 페이지 링크)
        '<h2 style="font-size:20px;margin:0 0 16px;">'
        f'<a href="{safe_detail_url}" '
        'style="color:#333;text-decoration:none;">'
        f"{safe_title}</a></h2>"
        # 2. 메타 박스
        '<table style="width:100%;background:#f5f5f5;border-radius:6px;'
        'padding:12px 16px;font-size:14px;border-collapse:collapse;">'
        f"{''.join(meta_rows)}"
        "</table>"
        # 3. 공고 요약 (없으면 빈 문자열)
        f"{summary_html}"
        # 4. 보낸 사람 메시지 (없으면 빈 문자열)
        f"{message_html}"
        # 5. CTA 버튼
        '<div style="margin:24px 0;">'
        f'<a href="{safe_detail_url}" '
        'style="display:inline-block;background:#444;color:#fff;'
        'padding:10px 20px;border-radius:6px;text-decoration:none;'
        'font-size:14px;">공고 상세 보기</a></div>'
        # 6. footer
        '<hr style="border:none;border-top:1px solid #e0e0e0;margin:24px 0;">'
        '<div style="font-size:12px;color:#999;">'
        f"보낸 사람: {safe_sender}<br>"
        "이 메일은 정부사업 모니터링 시스템에서 발송되었습니다. "
        "회신은 발송자에게 직접 부탁드립니다."
        "</div>"
        "</div></body></html>"
    )


def _html_meta_row(label: str, value_html: str) -> str:
    """HTML 메타 박스의 ``<tr>`` 한 행을 만든다.

    label 은 호출부에서 넘기는 고정 한글 라벨(이스케이프 불필요)이고,
    ``value_html`` 은 호출부가 **이미 이스케이프한** 값이다.

    Args:
        label: 행 라벨 (예: ``"발주기관"``). 고정 문자열.
        value_html: 행 값 — 호출부에서 이미 ``html.escape`` 된 상태로 넘긴다.

    Returns:
        ``<tr>...</tr>`` 문자열.
    """
    return (
        '<tr><td style="color:#888;width:90px;padding:2px 0;'
        'vertical-align:top;">'
        f"{label}</td><td style=\"padding:2px 0;\">{value_html}</td></tr>"
    )


__all__ = [
    "build_announcement_detail_url",
    "build_default_forward_subject",
    "build_forward_html_body",
    "build_forward_text_body",
    "build_multipart_message",
    "build_plain_text_message",
]
