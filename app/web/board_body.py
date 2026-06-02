"""게시글 본문(평문/리치 텍스트) 검증·정화 공용 헬퍼 (task 00153-1).

공지사항·건의사항 두 게시판의 작성/수정 라우트가 본문을 저장하기 전에 공통으로
거치는 처리(포맷 정규화 → 검증 → HTML sanitization)를 한곳에 모은다. 두 라우트가
같은 계약을 공유하도록 해, "한쪽 게시판만 정화됐다" 는 불일치를 방지한다.

처리 흐름:
    1. ``normalize_body_format`` 으로 포맷을 'plain'/'html' 로 정규화한다(알 수
       없는 값은 안전하게 'plain').
    2. 'html' 인 경우: 원문(HTML) 기준 필수/길이 검증 → 서버측 sanitization →
       정화 결과에 실제 콘텐츠(텍스트 또는 표)가 남았는지 확인.
    3. 'plain' 인 경우: 기존 평문 검증(strip 후 필수/길이) 을 그대로 적용.

저장 계층(repository) 은 본 헬퍼가 돌려준 (body, body_format) 을 그대로 받는다.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from fastapi import HTTPException, status

from app.suggestions.models import BODY_FORMAT_HTML, BODY_FORMAT_PLAIN
from app.suggestions.sanitize import normalize_body_format, sanitize_post_html

# 평문 본문 길이 상한 — 기존 라우트(_BODY_MAX_LENGTH) 와 동일하게 20000자.
PLAIN_BODY_MAX_LENGTH: int = 20000

# 리치 텍스트(HTML) 본문 길이 상한. 표·인라인 style 이 섞인 Word/Outlook 붙여넣기는
# 평문 대비 마크업 오버헤드가 크므로 넉넉한 상한을 둔다(원문 텍스트 분량 기준이
# 아니라 직렬화된 HTML 바이트 기준).
HTML_BODY_MAX_LENGTH: int = 200000


def prepare_board_body(
    body: str,
    body_format: str | None,
    *,
    plain_max_length: int = PLAIN_BODY_MAX_LENGTH,
    html_max_length: int = HTML_BODY_MAX_LENGTH,
) -> tuple[str, str]:
    """게시글 본문을 검증·정화하고 (저장할 본문, 정규화된 포맷) 을 반환한다.

    Args:
        body: 폼에서 받은 원본 본문 문자열.
        body_format: 폼에서 받은 본문 포맷('plain'/'html' 또는 None).
        plain_max_length: 평문 본문 최대 길이.
        html_max_length: HTML 본문 최대 길이.

    Returns:
        ``(저장할 본문, 'plain' 또는 'html')`` 튜플.
        - 'plain': strip 된 평문.
        - 'html': sanitization 을 거친 안전한 HTML.

    Raises:
        HTTPException(400): 빈 본문(또는 정화 후 빈 콘텐츠) 또는 길이 초과.
    """
    normalized_format = normalize_body_format(body_format)

    if normalized_format == BODY_FORMAT_HTML:
        raw = body or ""
        # 빈 입력 검증은 원문 기준(공백만 입력 거절).
        if not raw.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="본문 은(는) 필수 입력 항목입니다.",
            )
        # 길이 검증은 정화 전 원문(HTML 직렬화 문자열) 기준.
        if len(raw) > html_max_length:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"본문 의 길이가 너무 깁니다 (최대 {html_max_length}자).",
            )
        cleaned_html = sanitize_post_html(raw)
        # 정화 결과에 실제 콘텐츠(보이는 텍스트 또는 표) 가 전혀 없으면 빈 글로 간주.
        if not _has_visible_content(cleaned_html):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="본문 은(는) 필수 입력 항목입니다.",
            )
        return cleaned_html, BODY_FORMAT_HTML

    # 평문 경로 — 기존 동작과 동일(하위 호환).
    stripped = (body or "").strip()
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="본문 은(는) 필수 입력 항목입니다.",
        )
    if len(stripped) > plain_max_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"본문 의 길이가 너무 깁니다 (최대 {plain_max_length}자).",
        )
    return stripped, BODY_FORMAT_PLAIN


def _has_visible_content(cleaned_html: str) -> bool:
    """정화된 HTML 에 실제 콘텐츠(보이는 텍스트 또는 표) 가 있는지 판정한다.

    ``<p></p>`` / ``<br>`` 처럼 마크업만 있고 비어 보이는 본문을 빈 글로 거절하기
    위함이다. 단, 표(table) 는 셀이 비어 있어도 의미 있는 콘텐츠로 인정한다.
    """
    if not cleaned_html:
        return False
    soup = BeautifulSoup(cleaned_html, "html.parser")
    if soup.get_text(strip=True):
        return True
    # 텍스트는 없지만 표가 있으면 콘텐츠로 인정한다.
    return soup.find("table") is not None


__all__ = [
    "prepare_board_body",
    "PLAIN_BODY_MAX_LENGTH",
    "HTML_BODY_MAX_LENGTH",
]
