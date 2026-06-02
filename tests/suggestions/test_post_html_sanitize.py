"""게시글 리치 텍스트 sanitization 단위 테스트 (task 00153-1).

검증 범위:
    - sanitize_post_html: XSS 벡터(script/iframe/on*/javascript:/위험 CSS) 제거,
      문서 서식(표/폰트/굵기/목록/링크/색상) 보존.
    - normalize_body_format: 'html' 만 html 로 인정, 그 외는 안전하게 'plain'.
    - prepare_board_body: 포맷별 검증·정화 계약(빈 입력/길이/정화 후 빈 콘텐츠).

서버측 정화는 건의사항(일반 로그인 사용자 작성 가능) XSS 방어의 핵심이므로
공격 벡터 제거를 우선 검증한다.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.suggestions.models import BODY_FORMAT_HTML, BODY_FORMAT_PLAIN
from app.suggestions.sanitize import normalize_body_format, sanitize_post_html
from app.web.board_body import HTML_BODY_MAX_LENGTH, prepare_board_body

# ---------------------------------------------------------------------------
# sanitize_post_html — XSS 벡터 제거
# ---------------------------------------------------------------------------


def test_script_tag_removed_with_content() -> None:
    """<script> 는 내용까지 통째로 제거된다."""
    cleaned = sanitize_post_html("<p>안녕</p><script>alert('xss')</script>")
    assert "<script" not in cleaned.lower()
    assert "alert" not in cleaned
    assert "안녕" in cleaned


def test_iframe_object_embed_removed() -> None:
    """iframe/object/embed 같은 위험 임베드 태그는 제거된다."""
    raw = (
        "<p>본문</p>"
        "<iframe src='http://evil'></iframe>"
        "<object data='x'></object>"
        "<embed src='y'>"
    )
    cleaned = sanitize_post_html(raw)
    assert "<iframe" not in cleaned.lower()
    assert "<object" not in cleaned.lower()
    assert "<embed" not in cleaned.lower()
    assert "본문" in cleaned


def test_event_handler_attribute_removed() -> None:
    """on* 이벤트 핸들러 속성은 제거되고 태그 자체와 텍스트는 보존된다."""
    cleaned = sanitize_post_html('<p onclick="steal()">클릭</p>')
    assert "onclick" not in cleaned.lower()
    assert "steal" not in cleaned
    assert "클릭" in cleaned


def test_img_onerror_removed() -> None:
    """img 는 허용목록에 없어 벗겨지고, onerror 핸들러도 사라진다."""
    cleaned = sanitize_post_html('<img src=x onerror="alert(1)">')
    assert "onerror" not in cleaned.lower()
    assert "alert" not in cleaned


def test_javascript_url_in_href_removed() -> None:
    """href 의 javascript: 스킴은 제거되지만 a 태그/텍스트는 남는다."""
    cleaned = sanitize_post_html('<a href="javascript:alert(1)">링크</a>')
    assert "javascript:" not in cleaned.lower()
    assert "링크" in cleaned


def test_obfuscated_javascript_url_removed() -> None:
    """제어문자로 위장한 java\\tscript: 스킴도 제거된다."""
    cleaned = sanitize_post_html('<a href="java\tscript:alert(1)">x</a>')
    assert "alert" not in cleaned


def test_safe_http_href_preserved() -> None:
    """안전한 http(s) 링크는 그대로 보존된다."""
    cleaned = sanitize_post_html('<a href="https://example.com">사이트</a>')
    assert 'href="https://example.com"' in cleaned
    assert "사이트" in cleaned


def test_dangerous_css_declaration_dropped() -> None:
    """style 안의 위험 CSS(expression/url()) 선언만 버리고 안전한 선언은 남긴다."""
    cleaned = sanitize_post_html(
        '<p style="color: red; background: url(javascript:alert(1)); '
        'width: expression(alert(1))">텍스트</p>'
    )
    assert "expression" not in cleaned.lower()
    assert "url(" not in cleaned.lower()
    assert "javascript" not in cleaned.lower()
    # 안전한 color 선언은 보존된다.
    assert "color: red" in cleaned


def test_html_comment_removed() -> None:
    """주석(IE 조건부 주석 포함) 은 제거된다."""
    cleaned = sanitize_post_html("<p>A</p><!--[if IE]><script>bad()</script><![endif]-->")
    assert "bad()" not in cleaned
    assert "<!--" not in cleaned


# ---------------------------------------------------------------------------
# sanitize_post_html — 문서 서식 보존
# ---------------------------------------------------------------------------


def test_table_structure_preserved() -> None:
    """표 구조(table/thead/tbody/tr/th/td) 는 보존된다."""
    raw = (
        "<table border='1'><thead><tr><th>머리</th></tr></thead>"
        "<tbody><tr><td>셀</td></tr></tbody></table>"
    )
    cleaned = sanitize_post_html(raw)
    for tag in ["<table", "<thead", "<tbody", "<tr", "<th", "<td"]:
        assert tag in cleaned.lower(), f"{tag} 가 보존되어야 한다"
    assert "머리" in cleaned
    assert "셀" in cleaned


def test_font_and_style_preserved() -> None:
    """폰트·색상·크기 등 서식 속성(font 태그, span style) 은 보존된다."""
    raw = (
        '<font face="맑은 고딕" color="#ff0000" size="3">폰트</font>'
        '<span style="font-family: Arial; font-size: 14px; color: blue;">스팬</span>'
    )
    cleaned = sanitize_post_html(raw)
    assert "<font" in cleaned.lower()
    assert 'face="맑은 고딕"' in cleaned
    assert "font-family: Arial" in cleaned
    assert "font-size: 14px" in cleaned
    assert "color: blue" in cleaned


def test_text_formatting_tags_preserved() -> None:
    """굵게/기울임/밑줄 및 목록 태그는 보존된다."""
    raw = "<b>굵게</b><i>기울임</i><u>밑줄</u><ul><li>항목</li></ul>"
    cleaned = sanitize_post_html(raw)
    for tag in ["<b>", "<i>", "<u>", "<ul>", "<li>"]:
        assert tag in cleaned.lower()


def test_unknown_tag_unwrapped_keeps_text() -> None:
    """허용목록에 없는 태그는 벗겨지되 안쪽 텍스트는 보존된다."""
    cleaned = sanitize_post_html("<marquee>흐르는 글자</marquee>")
    assert "<marquee" not in cleaned.lower()
    assert "흐르는 글자" in cleaned


def test_empty_input_returns_empty_string() -> None:
    """빈/None 입력은 빈 문자열을 반환한다."""
    assert sanitize_post_html("") == ""
    assert sanitize_post_html(None) == ""


# ---------------------------------------------------------------------------
# normalize_body_format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("html", BODY_FORMAT_HTML),
        ("HTML", BODY_FORMAT_HTML),
        ("  html  ", BODY_FORMAT_HTML),
        ("plain", BODY_FORMAT_PLAIN),
        ("", BODY_FORMAT_PLAIN),
        (None, BODY_FORMAT_PLAIN),
        ("unknown", BODY_FORMAT_PLAIN),
        ("javascript", BODY_FORMAT_PLAIN),
    ],
)
def test_normalize_body_format(value: str | None, expected: str) -> None:
    """'html' 만 html 로 인정하고 그 외는 안전하게 'plain' 으로 정규화한다."""
    assert normalize_body_format(value) == expected


# ---------------------------------------------------------------------------
# prepare_board_body
# ---------------------------------------------------------------------------


def test_prepare_plain_body_strips_and_returns_plain() -> None:
    """평문 경로는 strip 후 'plain' 포맷으로 반환한다."""
    body, fmt = prepare_board_body("  내용  ", "plain")
    assert body == "내용"
    assert fmt == BODY_FORMAT_PLAIN


def test_prepare_html_body_sanitizes_and_returns_html() -> None:
    """html 경로는 정화된 HTML 과 'html' 포맷을 반환한다."""
    body, fmt = prepare_board_body(
        '<p>안전</p><script>bad()</script>', "html"
    )
    assert fmt == BODY_FORMAT_HTML
    assert "<script" not in body.lower()
    assert "안전" in body


def test_prepare_html_with_only_table_is_content() -> None:
    """텍스트가 없어도 표가 있으면 콘텐츠로 인정한다(거절하지 않음)."""
    body, fmt = prepare_board_body(
        "<table><tr><td></td></tr></table>", "html"
    )
    assert fmt == BODY_FORMAT_HTML
    assert "<table" in body.lower()


def test_prepare_empty_plain_body_rejected() -> None:
    """공백만 있는 평문 본문은 400 으로 거절한다."""
    with pytest.raises(HTTPException) as exc:
        prepare_board_body("   ", "plain")
    assert exc.value.status_code == 400


def test_prepare_html_body_empty_after_sanitize_rejected() -> None:
    """정화 후 보이는 콘텐츠가 전혀 없는 HTML 은 400 으로 거절한다."""
    with pytest.raises(HTTPException) as exc:
        prepare_board_body("<p></p><br>", "html")
    assert exc.value.status_code == 400


def test_prepare_html_body_too_long_rejected() -> None:
    """HTML 본문 길이 상한 초과는 400 으로 거절한다."""
    too_long = "<p>" + ("가" * (HTML_BODY_MAX_LENGTH + 1)) + "</p>"
    with pytest.raises(HTTPException) as exc:
        prepare_board_body(too_long, "html")
    assert exc.value.status_code == 400


def test_prepare_unknown_format_falls_back_to_plain() -> None:
    """알 수 없는 포맷은 평문 경로로 처리되어 escape 대상이 된다(|safe 미경유)."""
    body, fmt = prepare_board_body("<b>raw</b>", "weird")
    assert fmt == BODY_FORMAT_PLAIN
    # 평문이므로 원문 그대로 저장(escape 는 템플릿 출력 단계에서 수행).
    assert body == "<b>raw</b>"
