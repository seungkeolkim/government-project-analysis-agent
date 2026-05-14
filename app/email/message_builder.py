"""Plain text EmailMessage 빌더 (Phase A-1 / task 00104-7).

설계 근거:
    docs/phase_a1_design_note.md §12 (subtask scope 표) + 첨부 phase_a1_prompt.md
    의 \"plain text 메일 전용\" 결정 (\"핵심 결정\" 섹션 — A-1 단계에서는
    set_content() 만, HTML+plain multipart 빌더는 A-2 의 첫 subtask).

본 모듈은 단일 함수 ``build_plain_text_message`` 만 노출한다. EmailMessage
인스턴스에 To / Subject / (옵션) From 헤더를 채우고 ``set_content(body)`` 로
plain text 본문을 설정해 반환한다. 인코딩은 ``set_content`` 기본인 UTF-8 +
8-bit transfer encoding 을 그대로 사용한다.

비-범위 (다른 subtask 또는 후속 phase 의 책임):
    - 첨부 파일 / cc / bcc 헤더 추가 금지 (첨부 문서 \"핵심 결정\" 섹션).
    - HTML+plain multipart 빌더 — A-2 (공고 포워딩) 의 첫 subtask 책임.
    - 발송 실제 수행 / 재시도 / 이력 기록 — 본 모듈은 EmailMessage 객체만
      반환하며 transport / sender 가 발송한다.
    - From 헤더 자동 채움 — ``sender=None`` 으로 호출하면 본 빌더는 From 을
      비워둔다. ``M365OAuthSmtpTransport.send`` (00104-5) 의
      ``_fill_from_header_if_empty`` 가 SystemSetting 의 from_display_name +
      sender_address 조합으로 채운다. 호출자가 SystemSetting 과 무관한 발신자
      (예: 단위 테스트의 가짜 발신자) 를 명시하고 싶으면 ``sender=...`` 에 전달.
"""

from __future__ import annotations

from email.message import EmailMessage


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


__all__ = ["build_plain_text_message"]
