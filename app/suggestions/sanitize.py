"""게시글 리치 텍스트(HTML) 서버측 sanitization (task 00153-1).

게시판 게시글이 리치 텍스트 편집기로 작성되면, 사용자(건의사항은 일반 로그인
사용자도 작성 가능)가 제출한 HTML 에 ``<script>``·이벤트 핸들러·``javascript:``
URL 같은 XSS 벡터가 섞여 들어올 수 있다. 본 모듈은 **허용목록(allowlist) 기반**
으로 HTML 을 정화해, 표·폰트·서식 같은 문서 작성 태그/속성은 보존하면서 위험
벡터는 제거한다. 정화는 **저장 시점**에 수행되어 DB 에는 안전한 HTML 만 남는다.

## sanitizer 라이브러리 선택 (구현 결정)
plan/guidance 는 1순위로 nh3(ammonia, Rust 기반) 를 권했고 beautifulsoup4 기반
allowlist 구현을 차선책으로 제시했다. 본 구현은 **차선책인 beautifulsoup4 를
채택**했다. 근거:
  - 본 작업 환경에서는 패키지 설치(pip/Docker 재빌드)가 금지되어 nh3 를 실제로
    설치·import 할 수 없고, 단위 테스트가 ``ModuleNotFoundError`` 로 깨진다.
    (제약: "패키지 매니저 실행 금지" + "관련 단위 테스트가 통과해야 한다".)
  - 요구사항의 핵심은 "표·폰트 style 속성은 허용하되 위험한 CSS 는 차단" 으로,
    CSS 속성 단위 필터링이 필요하다. nh3(ammonia) 는 ``style`` 속성을 허용하면
    내부 CSS 선언을 속성 단위로 걸러주지 않으므로, 오히려 본 요구에는 명시적
    CSS 허용목록을 직접 구현하는 편이 정밀하다.
  - beautifulsoup4 는 이미 설치된 의존성이라 신규 의존성·Docker 재빌드가 불필요하다.

따라서 본 모듈은 단일 구현(beautifulsoup4) 으로 동작하며, 새 런타임 의존성을
추가하지 않는다.

## 정화 정책 요약
  1. 주석(조건부 주석 포함) 제거 — IE 조건부 주석 XSS 차단.
  2. 위험 태그(script/style/iframe/object/embed/form/svg/math 등) 는 내용까지
     통째로 제거(decompose).
  3. 허용목록에 없는 태그는 ``unwrap`` — 태그만 벗기고 자식/텍스트는 보존.
  4. 허용 태그라도 허용목록에 없는 속성·``on*`` 이벤트 핸들러는 제거.
  5. ``style`` 속성은 CSS 속성 단위 허용목록으로 필터링하고, 위험 토큰
     (``expression``/``url(``/``javascript:`` 등) 이 든 선언은 버린다.
  6. ``href`` 등 URL 속성은 허용 스킴(http/https/mailto/tel) 과 상대경로만 통과.
"""

from __future__ import annotations

from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment

from app.suggestions.models import BODY_FORMAT_HTML, BODY_FORMAT_PLAIN

# ──────────────────────────────────────────────────────────────
# 허용목록 정의
# ──────────────────────────────────────────────────────────────

# 보존할 문서 작성 태그. 표·서식·목록·링크 등 일반 워드프로세싱 요소를 포함한다.
ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        # 블록·구조
        "p", "div", "span", "br", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "pre", "code",
        # 텍스트 서식 (굵게/기울임/밑줄/취소선/위첨자/아래첨자/형광 등)
        "b", "strong", "i", "em", "u", "s", "strike", "del", "ins",
        "sub", "sup", "small", "mark",
        # 목록
        "ul", "ol", "li",
        # 링크
        "a",
        # 표
        "table", "thead", "tbody", "tfoot", "tr", "th", "td",
        "caption", "colgroup", "col",
        # 레거시 폰트 태그 (Word/Outlook 붙여넣기에서 자주 등장)
        "font",
    }
)

# 내용까지 통째로 제거할 위험 태그. unwrap 이 아니라 decompose 로 제거한다.
DANGEROUS_TAGS: frozenset[str] = frozenset(
    {
        "script", "style", "iframe", "object", "embed", "applet",
        "form", "input", "button", "textarea", "select", "option",
        "link", "meta", "base", "title", "noscript",
        "frame", "frameset", "svg", "math", "template",
    }
)

# 모든 허용 태그에 공통으로 허용하는 속성.
GLOBAL_ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {"style", "class", "align", "dir", "title", "lang"}
)

# 태그별 추가 허용 속성. GLOBAL_ALLOWED_ATTRIBUTES 와 합집합으로 적용한다.
PER_TAG_ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "target", "rel", "name"}),
    "font": frozenset({"color", "face", "size"}),
    "table": frozenset(
        {"border", "cellpadding", "cellspacing", "width", "height", "bgcolor", "summary"}
    ),
    "td": frozenset(
        {"colspan", "rowspan", "width", "height", "valign", "bgcolor", "nowrap", "scope"}
    ),
    "th": frozenset(
        {"colspan", "rowspan", "width", "height", "valign", "bgcolor", "nowrap", "scope"}
    ),
    "tr": frozenset({"valign", "bgcolor"}),
    "col": frozenset({"span", "width"}),
    "colgroup": frozenset({"span", "width"}),
    "ol": frozenset({"start", "type"}),
    "ul": frozenset({"type"}),
    "li": frozenset({"value"}),
}

# style 속성 안에서 허용할 CSS 속성. 표·폰트·정렬·여백 등 문서 서식만 남긴다.
ALLOWED_CSS_PROPERTIES: frozenset[str] = frozenset(
    {
        "color", "background-color", "background",
        "font", "font-family", "font-size", "font-weight", "font-style",
        "font-variant",
        "line-height", "letter-spacing", "word-spacing",
        "text-align", "text-decoration", "text-decoration-line",
        "text-indent", "text-transform",
        "vertical-align", "white-space", "direction",
        "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
        "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
        "border", "border-top", "border-right", "border-bottom", "border-left",
        "border-color", "border-style", "border-width",
        "border-collapse", "border-spacing",
        "width", "height", "min-width", "max-width", "min-height", "max-height",
        "list-style", "list-style-type", "list-style-position",
    }
)

# CSS 선언 값에 등장하면 그 선언 전체를 버리는 위험 토큰(소문자 비교).
# url() 은 외부 리소스 로딩/데이터 유출 벡터라 서식 목적상 전면 차단한다.
DANGEROUS_CSS_TOKENS: tuple[str, ...] = (
    "expression",
    "javascript:",
    "vbscript:",
    "url(",
    "@import",
    "behavior",
    "binding",
    "\\",
)

# href 등 URL 속성에서 허용하는 스킴. 상대경로/앵커(스킴 없음) 도 허용한다.
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto", "tel"})

# URL 속성 검사 대상 속성명.
_URL_ATTRIBUTES: frozenset[str] = frozenset({"href", "src"})


# ──────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────


def normalize_body_format(value: str | None) -> str:
    """폼/외부에서 들어온 body_format 값을 안전한 식별자로 정규화한다.

    명시적으로 ``'html'`` 일 때만 HTML 로 인정하고, 그 외(빈 값·미지정·알 수 없는
    값) 는 모두 안전한 기본값 ``'plain'`` 으로 처리한다. 알 수 없는 포맷을 평문으로
    떨어뜨리면, 신뢰할 수 없는 입력이 실수로 ``|safe`` 출력 경로를 타는 일을 막는다.

    Args:
        value: 폼 필드 등에서 받은 원본 포맷 문자열(또는 None).

    Returns:
        ``BODY_FORMAT_HTML`` 또는 ``BODY_FORMAT_PLAIN`` 중 하나.
    """
    if value is None:
        return BODY_FORMAT_PLAIN
    normalized = value.strip().lower()
    if normalized == BODY_FORMAT_HTML:
        return BODY_FORMAT_HTML
    return BODY_FORMAT_PLAIN


def sanitize_post_html(raw_html: str | None) -> str:
    """게시글 본문 HTML 을 허용목록 기반으로 정화한다.

    표·폰트·서식 등 문서 작성 태그/속성은 보존하고, ``<script>``·이벤트 핸들러·
    ``javascript:`` URL·위험 CSS 같은 XSS 벡터는 제거한다. 정화 결과는 저장 시
    그대로 DB 에 들어가며, 상세 화면에서 ``|safe`` 로 출력해도 안전하다.

    Args:
        raw_html: 사용자가 제출한 원본 HTML 문자열(또는 None).

    Returns:
        정화된 HTML 문자열. 입력이 비어 있으면 빈 문자열.
    """
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")

    # 1. 주석 제거 — IE 조건부 주석을 통한 스크립트 주입을 차단한다.
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # 2. 위험 태그는 내용까지 통째로 제거한다. (자식 텍스트도 함께 사라져야 안전)
    for dangerous in soup.find_all(lambda tag: tag.name in DANGEROUS_TAGS):
        dangerous.decompose()

    # 3. 남은 태그를 순회하며 허용목록을 적용한다.
    #    list() 로 스냅샷을 떠서 unwrap 으로 트리가 바뀌어도 안전하게 순회한다.
    for tag in list(soup.find_all(True)):
        # decompose/unwrap 으로 이미 트리에서 분리된 태그는 건너뛴다.
        if tag.name is None:
            continue
        if tag.name not in ALLOWED_TAGS:
            # 허용되지 않은 태그는 벗겨내되 자식/텍스트는 보존한다.
            tag.unwrap()
            continue
        _sanitize_tag_attributes(tag)

    return str(soup)


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _sanitize_tag_attributes(tag) -> None:
    """허용 태그의 속성을 허용목록 기준으로 정화한다(in-place).

    - ``on*`` 이벤트 핸들러는 무조건 제거한다.
    - 허용목록에 없는 속성은 제거한다.
    - ``style`` 은 CSS 속성 단위로 필터링한다.
    - ``href``/``src`` 는 허용 스킴/상대경로만 통과시킨다.
    - ``a target=_blank`` 에는 ``rel=noopener noreferrer`` 를 보강한다.
    """
    allowed_attributes = GLOBAL_ALLOWED_ATTRIBUTES | PER_TAG_ALLOWED_ATTRIBUTES.get(
        tag.name, frozenset()
    )

    for attribute_name in list(tag.attrs.keys()):
        lowered = attribute_name.lower()

        # 이벤트 핸들러(onclick, onerror 등) 는 무조건 제거.
        if lowered.startswith("on"):
            del tag[attribute_name]
            continue

        if lowered not in allowed_attributes:
            del tag[attribute_name]
            continue

        if lowered == "style":
            cleaned_style = _sanitize_style(tag[attribute_name])
            if cleaned_style:
                tag[attribute_name] = cleaned_style
            else:
                del tag[attribute_name]
            continue

        if lowered in _URL_ATTRIBUTES:
            if not _is_safe_url(_attribute_to_str(tag[attribute_name])):
                del tag[attribute_name]
            continue

    # 새 탭 링크는 reverse tabnabbing 방어를 위해 rel 을 강제한다.
    if tag.name == "a" and tag.get("target"):
        tag["rel"] = "noopener noreferrer"


def _attribute_to_str(value) -> str:
    """속성값이 리스트(예: class) 로 파싱된 경우에도 문자열로 정규화한다."""
    if isinstance(value, list):
        return " ".join(value)
    return value if value is not None else ""


def _sanitize_style(style_value) -> str:
    """style 속성 문자열을 CSS 속성 허용목록으로 필터링한다.

    허용된 CSS 속성만 남기고, 값에 위험 토큰(expression/url(/javascript: 등) 이
    포함된 선언은 통째로 버린다.

    Args:
        style_value: 원본 style 속성 문자열.

    Returns:
        정화된 ``"prop: value; prop: value"`` 형태의 문자열. 남는 선언이 없으면
        빈 문자열.
    """
    style_text = _attribute_to_str(style_value)
    safe_declarations: list[str] = []

    for declaration in style_text.split(";"):
        declaration = declaration.strip()
        if not declaration or ":" not in declaration:
            continue

        property_name, _, property_value = declaration.partition(":")
        property_name = property_name.strip().lower()
        property_value = property_value.strip()

        if not property_value:
            continue
        if property_name not in ALLOWED_CSS_PROPERTIES:
            continue

        lowered_value = property_value.lower()
        if any(token in lowered_value for token in DANGEROUS_CSS_TOKENS):
            continue

        safe_declarations.append(f"{property_name}: {property_value}")

    return "; ".join(safe_declarations)


def _is_safe_url(value: str) -> bool:
    """URL 속성값이 안전한지(허용 스킴 또는 상대경로) 판정한다.

    제어문자(``\\t\\n\\r``) 를 먼저 제거해 ``java\\tscript:`` 같은 우회를 막은 뒤
    스킴을 파싱한다. 스킴이 없으면 상대경로/앵커로 보고 허용한다.

    Args:
        value: 검사할 URL 문자열.

    Returns:
        안전하면 True.
    """
    if not value:
        return False

    # 브라우저가 무시하는 제어문자를 제거해 스킴 위장(java\tscript:) 을 차단.
    cleaned = "".join(ch for ch in value if ch not in "\t\n\r").strip()
    if not cleaned:
        return False

    scheme = urlparse(cleaned).scheme.lower()
    if not scheme:
        # 스킴 없음 — 상대경로/앵커(/foo, #anchor, foo.html) 로 보고 허용.
        return True
    return scheme in ALLOWED_URL_SCHEMES


__all__ = [
    "ALLOWED_TAGS",
    "ALLOWED_CSS_PROPERTIES",
    "ALLOWED_URL_SCHEMES",
    "sanitize_post_html",
    "normalize_body_format",
]
