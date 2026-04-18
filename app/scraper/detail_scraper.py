"""IRIS 사업공고 상세 페이지 스크래퍼.

목록 스크래퍼(`list_scraper.scrape_list`)가 만든 row 메타데이터를 입력으로 받아,
Playwright(chromium, headless) 로 상세 페이지에 진입한 뒤 본문 메타데이터와
첨부파일 트리거 정보를 수집한다. 실제 파일 다운로드는 수행하지 않으며,
후속 subtask(00005-7) 에서 이 모듈이 반환한 트리거 정보를 사용해 다운로드를
수행한다.

IRIS 상세 페이지 특성:
    - 일부 첨부파일 영역이 탭/아코디언 뒤에 숨어 있어 DOM 에는 존재하지만
      `display:none` 또는 `aria-hidden` 처리되어 렌더링되지 않는다.
    - 다운로드 링크는 `<a href=...>` 직접 형태와, `onclick="fn_egov_downFile(...)"`
      처럼 JS 함수 호출로 POST 다운로드를 트리거하는 형태가 섞여 있다.
    - 본문 메타는 대체로 `<table>` 의 `th/td` 또는 `<dl>` 의 `dt/dd` 쌍으로 표현된다.

이 모듈은 두 단계를 거쳐 상세 페이지 전체를 '노출' 시킨 뒤 한 번에 스캔한다.
    1. 모든 탭/아코디언을 프로그래밍적으로 open 한다(click + JS 로 display 강제).
    2. `page.evaluate` 로 DOM 트리 전체를 순회하며 다운로드 패턴 요소를 수집한다.

반환 스키마(dict) — 키:
    - iris_announcement_id: str
    - detail_url:           최종 네비게이션 후의 page.url
    - raw_metadata:         dict[str, str]. 라벨 → 값 매핑(공고 원본 보존용).
    - attachments:          list[dict]. 각 원소는 아래 키를 갖는다.
        * original_filename: str
        * file_ext:          str (소문자, '.' 없음. 'unknown' 가능)
        * download_url:      str | None  (직접 GET 가능한 절대 URL)
        * download_trigger:  dict | None (JS 함수 기반 호출 시의 트리거 정보)
            - kind:          'js_function_call'
            - function_name: str
            - arguments:     list[str]
            - raw_onclick:   str
            - tag_name:      str
            - href:          str
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from loguru import logger
from playwright.async_api import (
    Page,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from app.config import Settings, get_settings
from app.scraper.list_scraper import (
    DEFAULT_MAX_ATTEMPTS,
    RETRY_BACKOFF_BASE_SEC,
    _open_browser_context,
    _sleep_with_jitter,
    _with_retry,
)

# ──────────────────────────────────────────────────────────────
# 셀렉터/패턴 상수 블록 (IRIS DOM 변경 시 이 블록만 교체)
# ──────────────────────────────────────────────────────────────

# 상세 페이지가 적어도 domcontentloaded 상태에 도달했음을 확인할 최소 셀렉터.
# body 태그는 어떤 페이지든 반드시 존재하므로 '완전 실패' 감지용이다.
SELECTOR_DETAIL_READY: str = "body"

# IRIS/전자정부 프레임워크에서 파일 다운로드를 트리거하는 대표적 JS 함수 이름들.
# 여기에 이름이 없어도 `onclick` 에 'download/fileDown/atchFile' 문자열이 포함되면
# 후보로 잡는다.
JS_DOWNLOAD_FUNCTION_NAMES: tuple[str, ...] = (
    "fn_egov_downFile",
    "fn_fileDown",
    "fn_AtchFileDown",
    "fn_atchFileDown",
    "fnFileDown",
    "fnDownload",
    "goDownload",
    "goFileDown",
    "downloadFile",
    "fileDown",
    "cmmnFileDown",
    "atchFileDown",
)

# 파일 확장자 후보. IRIS 에서 주로 등장하는 문서/압축 확장자를 우선 등재한다.
ATTACHMENT_FILE_EXTENSIONS: tuple[str, ...] = (
    "pdf",
    "hwp",
    "hwpx",
    "zip",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "txt",
    "csv",
)

# 파일명에서 확장자를 추출하는 정규식. 쿼리스트링(?)·프래그먼트(#)·공백은 경계로 본다.
_file_extension_regex: re.Pattern[str] = re.compile(
    r"\.(" + "|".join(ATTACHMENT_FILE_EXTENSIONS) + r")(?=$|[\s?#])",
    re.IGNORECASE,
)

# onclick 문자열에서 '함수명(인자들)' 를 뽑는 정규식.
ONCLICK_FUNCTION_CALL_PATTERN: re.Pattern[str] = re.compile(
    r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\(([^)]*)\)"
)

# 함수 호출 인자 중 따옴표로 묶인 문자열 리터럴 만을 추출한다.
ONCLICK_STRING_ARGUMENT_PATTERN: re.Pattern[str] = re.compile(r"""['"]([^'"]*)['"]""")

# raw_metadata 에 저장할 라벨 키 사이즈 방어 상한.
MAX_METADATA_PAIR_COUNT: int = 200

# 상세 진입 직후 추가로 기다릴 여유 시간(초). networkidle 이 끝나도
# 일부 JS 로 그려지는 영역이 살짝 늦게 나타나는 경우를 대비한다.
POST_NAVIGATION_GRACE_SEC: float = 0.5

# 탭/아코디언 강제 open 후 DOM 이 안정되기까지의 여유 시간(초).
POST_EXPAND_GRACE_SEC: float = 0.5

# JS 평가로 상세 진입을 시도한 뒤 URL 변화를 기다릴 최대 시간(ms).
JS_NAVIGATION_WAIT_MS: int = 10_000


# ──────────────────────────────────────────────────────────────
# 브라우저 안에서 실행할 JS 스니펫 (page.evaluate 로 전달)
# ──────────────────────────────────────────────────────────────

# 모든 탭/아코디언/숨은 섹션을 강제로 open 시키는 JS.
# 동작:
#   1) inline style 의 display:none / visibility:hidden 을 제거.
#   2) 대표적인 collapse/accordion/hidden 클래스와 aria-hidden 을 해제.
#   3) 탭/아코디언 헤더류 요소를 모두 click 한다.
#   4) <details> 태그는 open 속성을 true 로 설정한다.
_OPEN_COLLAPSIBLES_JS: str = r"""
() => {
  let clickCount = 0;

  // 1) inline style 로 숨겨진 요소를 복원.
  document.querySelectorAll('*').forEach(el => {
    if (!el.style) return;
    if (el.style.display === 'none') el.style.display = '';
    if (el.style.visibility === 'hidden') el.style.visibility = '';
  });

  // 2) 흔한 collapse/hidden 클래스와 aria-hidden 해제.
  const hiddenSelectors = [
    '.collapse', '.collapsed', '.is-hidden', '.hidden',
    '[aria-hidden="true"]', '[hidden]'
  ];
  hiddenSelectors.forEach(sel => {
    document.querySelectorAll(sel).forEach(el => {
      el.classList.remove('collapse', 'collapsed', 'is-hidden', 'hidden');
      el.removeAttribute('hidden');
      el.setAttribute('aria-hidden', 'false');
      if (el.style) el.style.display = '';
    });
  });

  // 3) 탭/아코디언 헤더류를 모두 click.
  const clickTargets = document.querySelectorAll([
    '[role="tab"]',
    '.tab', '.tab-link', '.tabs a', '.tab-header',
    '.accordion', '.accordion-header', '.accordion-toggle', 'button.accordion',
    '[data-toggle="tab"]', '[data-toggle="collapse"]',
    '[data-bs-toggle="tab"]', '[data-bs-toggle="collapse"]',
    '.collapsible', 'summary'
  ].join(','));
  clickTargets.forEach(el => {
    try { el.click(); clickCount += 1; } catch (e) { /* 무시 */ }
  });

  // 4) <details> 는 open 속성 직접 설정.
  document.querySelectorAll('details').forEach(el => { el.open = true; });

  return { click_count: clickCount };
}
"""

# 본문 메타데이터(th/td, dt/dd) 쌍을 수집하는 JS.
# 인자: { maxPairs: number } — 방어적 상한.
# 반환: [[label, value], ...] 리스트.
_EXTRACT_METADATA_PAIRS_JS: str = r"""
(config) => {
  const maxPairs = (config && config.maxPairs) || 200;
  const pairs = [];

  const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();

  const pushPair = (label, value) => {
    if (pairs.length >= maxPairs) return;
    const key = normalize(label);
    const val = normalize(value);
    if (!key || !val) return;
    pairs.push([key, val]);
  };

  // (1) th/td 구조의 표.
  document.querySelectorAll('table').forEach(table => {
    table.querySelectorAll('tr').forEach(tr => {
      const ths = tr.querySelectorAll('th');
      const tds = tr.querySelectorAll('td');
      if (ths.length >= 1 && tds.length >= 1) {
        // 가장 흔한 '첫 th + 첫 td' 쌍을 우선 채택.
        pushPair(ths[0].innerText, tds[0].innerText);
        // 행에 여러 쌍이 반복되는 경우(th/td/th/td) 에도 안전하게 커버.
        const minLen = Math.min(ths.length, tds.length);
        for (let i = 1; i < minLen; i++) {
          pushPair(ths[i].innerText, tds[i].innerText);
        }
      }
    });
  });

  // (2) dl > dt / dd 쌍.
  document.querySelectorAll('dl').forEach(dl => {
    const dts = Array.from(dl.querySelectorAll(':scope > dt'));
    const dds = Array.from(dl.querySelectorAll(':scope > dd'));
    const len = Math.min(dts.length, dds.length);
    for (let i = 0; i < len; i++) {
      pushPair(dts[i].innerText, dds[i].innerText);
    }
  });

  return pairs;
}
"""

# 다운로드 트리거로 의심되는 모든 요소를 수집하는 JS.
# 인자: { functionNames: string[], fileExtensions: string[] }
# 반환: [{ text, href, onclick, tag_name, outer_html }, ...]
_EXTRACT_ATTACHMENTS_JS: str = r"""
(config) => {
  const functionNames = (config && config.functionNames) || [];
  const fileExtensions = (config && config.fileExtensions) || [];

  // 함수 이름을 | 로 연결한 OR 패턴. 없으면 아무에도 매칭되지 않도록 빈 대안 처리.
  const fnUnion = functionNames.length
    ? functionNames.map(n => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
    : '__NO_FUNCTION_NAMES__';
  const fnRegex = new RegExp('(' + fnUnion + ')\\s*\\(([^)]*)\\)');

  // 파일 확장자 union
  const extUnion = fileExtensions.length ? fileExtensions.join('|') : 'pdf';
  const extRegex = new RegExp('\\.(' + extUnion + ')(?=$|[\\s?#])', 'i');

  const hrefHintRegex = /download|filedown|atchfile|goDownload/i;

  const candidates = [];
  const seen = new Set();

  // 다운로드 트리거는 <a>/<button> 이 일반적이지만, onclick 만 걸린 임의 요소도 있으므로
  // onclick 속성을 가진 모든 요소 + 기본 클릭 태그를 대상으로 한다.
  const nodes = document.querySelectorAll('a, button, [onclick]');
  for (const el of nodes) {
    const href = el.getAttribute('href') || '';
    const onclick = el.getAttribute('onclick') || '';
    const text = (el.innerText || el.textContent || '').trim();

    const hasFnMatch = fnRegex.test(onclick);
    const hasHrefHint = hrefHintRegex.test(href);
    const hasExtInText = extRegex.test(text);
    const hasExtInHref = extRegex.test(href);

    // '의심 요소'의 필터링 규칙:
    //   - onclick 에 다운로드 함수 호출이 있거나
    //   - href 가 다운로드 힌트를 포함하거나
    //   - 텍스트/href 에 파일 확장자가 드러나 있고 클릭 가능한 요소인 경우.
    if (!hasFnMatch && !hasHrefHint && !hasExtInText && !hasExtInHref) continue;

    // 중복 제거: outerHTML 앞부분 + onclick + href 조합.
    const descriptor = (onclick || '') + '||' + (href || '') + '||' + text;
    if (seen.has(descriptor)) continue;
    seen.add(descriptor);

    // outer_html 은 디버깅/원본 보존용. 너무 크지 않게 앞부분만.
    const outerHtml = (el.outerHTML || '').substring(0, 1000);

    candidates.push({
      text: text,
      href: href,
      onclick: onclick,
      tag_name: el.tagName ? el.tagName.toLowerCase() : '',
      outer_html: outerHtml,
    });
  }

  return candidates;
}
"""


# ──────────────────────────────────────────────────────────────
# 유틸 함수
# ──────────────────────────────────────────────────────────────


def _is_list_page(current_url: Optional[str], base_url: str) -> bool:
    """현재 페이지 URL 이 IRIS 목록 페이지인지 판정한다.

    쿼리스트링/프래그먼트를 무시하고 path 까지만 비교한다.
    둘 중 어느 값이라도 비어 있으면 False 로 간주한다.
    """
    if not current_url or not base_url:
        return False

    def _strip(target_url: str) -> str:
        without_query = target_url.split("?", 1)[0]
        without_fragment = without_query.split("#", 1)[0]
        return without_fragment

    return _strip(current_url) == _strip(base_url)


def _resolve_detail_url(detail_url_value: str, base_url: str) -> str:
    """상세 URL 문자열을 절대 URL 로 해석한다.

    - `http(s)://...` 로 시작하면 그대로 반환.
    - `/` 로 시작하면 base_url 의 scheme+host 뒤에 결합.
    - 그 외(`?foo=bar` 등 상대 path)는 `urljoin` 으로 해석.
    """
    stripped = detail_url_value.strip()
    if stripped.startswith(("http://", "https://")):
        return stripped

    parsed_base = urlparse(base_url)
    if stripped.startswith("/"):
        return f"{parsed_base.scheme}://{parsed_base.netloc}{stripped}"

    return urljoin(base_url, stripped)


def _parse_onclick_call(onclick_text: Optional[str]) -> Optional[dict[str, Any]]:
    """onclick 문자열에서 첫 번째 함수 호출과 인자 리스트를 분해한다.

    예) `"fn_egov_downFile('FILE123','docs.pdf')"`
        → `{"function_name": "fn_egov_downFile",
            "arguments": ["FILE123", "docs.pdf"], ...}`

    매칭되는 함수 호출이 없으면 None 을 반환한다.
    """
    if not onclick_text:
        return None

    call_match = ONCLICK_FUNCTION_CALL_PATTERN.search(onclick_text)
    if call_match is None:
        return None

    function_name = call_match.group(1)
    arguments_raw = call_match.group(2)
    string_arguments = ONCLICK_STRING_ARGUMENT_PATTERN.findall(arguments_raw)

    return {
        "kind": "js_function_call",
        "function_name": function_name,
        "arguments": list(string_arguments),
        "raw_onclick": onclick_text.strip(),
    }


def _extract_file_extension(*candidate_strings: Optional[str]) -> str:
    """후보 문자열들에서 알려진 파일 확장자를 찾아 소문자로 반환한다.

    파일명 뒤에 공백/쿼리/프래그먼트가 올 수도 있으므로 `_file_extension_regex`
    로 경계 검사까지 수행한다. 어느 후보에서도 확장자를 찾지 못하면
    `'unknown'` 을 반환한다.
    """
    for candidate_value in candidate_strings:
        if not candidate_value:
            continue
        match = _file_extension_regex.search(candidate_value)
        if match is not None:
            return match.group(1).lower()
    return "unknown"


def _guess_filename_from_candidate(raw_candidate: dict[str, Any]) -> str:
    """JS 가 수집한 단일 후보에서 원본 파일명을 추정한다.

    우선순위:
        1) 앵커의 innerText 가 파일명 형태(확장자 포함)이면 채택.
        2) onclick 인자 중 확장자가 있는 문자열이 있으면 채택.
        3) href 의 마지막 path 조각이 파일명 형태이면 채택.
        4) 그 외에는 앵커 텍스트(비어 있으면 '(이름 미확인)').
    """
    raw_text = (raw_candidate.get("text") or "").strip()
    raw_onclick = raw_candidate.get("onclick") or ""
    raw_href = (raw_candidate.get("href") or "").strip()

    # (1) 앵커 텍스트 우선.
    if raw_text and _file_extension_regex.search(raw_text):
        return raw_text

    # (2) onclick 인자에서 확장자 달린 문자열 탐색.
    for argument_string in ONCLICK_STRING_ARGUMENT_PATTERN.findall(raw_onclick):
        if _file_extension_regex.search(argument_string):
            return argument_string

    # (3) href 마지막 조각.
    if raw_href and not raw_href.startswith("javascript:"):
        last_path_segment = raw_href.split("?", 1)[0].rstrip("/").split("/")[-1]
        if last_path_segment and _file_extension_regex.search(last_path_segment):
            return last_path_segment

    # (4) 폴백: 앵커 텍스트(그마저 없으면 플레이스홀더).
    if raw_text:
        return raw_text
    return "(이름 미확인)"


def _derive_download_url(raw_href: str, base_url: str) -> Optional[str]:
    """앵커의 href 에서 GET 다운로드용 절대 URL 을 도출한다.

    `javascript:` 스킴이거나 빈 href 는 None 을 반환한다(POST 트리거로 분류되어야 함).
    그 외 상대/절대 URL 은 `_resolve_detail_url` 과 동일한 규칙으로 해석한다.
    """
    href_value = (raw_href or "").strip()
    if not href_value:
        return None
    if href_value.startswith("javascript:"):
        return None
    if href_value.startswith("#"):
        return None
    return _resolve_detail_url(href_value, base_url)


# ──────────────────────────────────────────────────────────────
# 상세 페이지 진입 & 준비
# ──────────────────────────────────────────────────────────────


async def _navigate_to_detail_page(
    page: Page,
    row_metadata: dict[str, Any],
    settings: Settings,
) -> str:
    """row 메타데이터를 근거로 상세 페이지에 진입하고 최종 URL 을 반환한다.

    진입 전략(우선순위 순서):
        1) detail_url 이 http(s) 절대 URL 이면 바로 goto.
        2) detail_url 이 `/` 로 시작하면 base_url 오리진과 결합해 goto.
        3) detail_url 이 `javascript:...` 스킴이거나, detail_onclick 이 있으면
           현재 페이지(또는 목록 페이지) 에서 해당 JS 를 평가한다.
           JS 평가 결과로 URL 이 바뀌거나 내용이 재렌더될 때까지 대기한다.

    Args:
        page:         재사용 Playwright Page. None 불가(호출자가 주입).
        row_metadata: 목록 스크래퍼가 반환한 row dict.
        settings:     전역 Settings.

    Returns:
        최종 `page.url` 문자열(네비게이션 실패 시에도 현재 URL 을 반환).

    Raises:
        ValueError:
            어떤 진입 수단도 row_metadata 에서 찾을 수 없는 경우.
    """
    detail_url_value = row_metadata.get("detail_url")
    detail_onclick_value = row_metadata.get("detail_onclick")

    # ── 케이스 1/2: URL 기반 진입 ──
    if isinstance(detail_url_value, str) and detail_url_value and not detail_url_value.startswith(
        ("javascript:", "#")
    ):
        absolute_url = _resolve_detail_url(detail_url_value, settings.base_url)
        logger.info("URL 기반 상세 진입: {}", absolute_url)
        await page.goto(absolute_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle")
        except PlaywrightTimeoutError:
            logger.warning("상세 페이지 networkidle 대기 타임아웃 — 진행")
        await page.wait_for_selector(SELECTOR_DETAIL_READY, state="attached")
        return page.url

    # ── 케이스 3: JS 기반 진입 ──
    js_expression: Optional[str] = None
    if isinstance(detail_url_value, str) and detail_url_value.startswith("javascript:"):
        js_expression = detail_url_value[len("javascript:"):]
    if not js_expression and detail_onclick_value:
        js_expression = str(detail_onclick_value)

    if not js_expression:
        raise ValueError(
            "상세 진입 수단을 찾을 수 없습니다. "
            f"detail_url={detail_url_value!r}, detail_onclick={detail_onclick_value!r}"
        )

    # 현재 페이지가 목록이 아니면 먼저 목록으로 이동한다(함수 스코프 확보 목적).
    if not _is_list_page(page.url, settings.base_url):
        logger.info("JS 진입 준비: 목록 페이지로 이동 {}", settings.base_url)
        await page.goto(settings.base_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle")
        except PlaywrightTimeoutError:
            logger.warning("목록 페이지 networkidle 대기 타임아웃 — 진행")

    logger.info("JS 기반 상세 진입: {}", js_expression)
    url_before_evaluation = page.url

    # 페이지 컨텍스트 내 함수를 호출한다. 여러 statement 가 있을 수 있으므로
    # 화살표 함수의 블록으로 감싸 안전하게 평가한다.
    wrapped_js = "() => { " + js_expression.rstrip(";").strip() + "; }"
    try:
        await page.evaluate(wrapped_js)
    except PlaywrightError as evaluation_exc:
        logger.error("JS 상세 진입 실패: {}", evaluation_exc)
        raise

    # URL 변화를 기다린다(JS 가 AJAX 만 호출하는 경우도 있으니 실패 허용).
    try:
        await page.wait_for_function(
            "(prev) => window.location.href !== prev",
            arg=url_before_evaluation,
            timeout=JS_NAVIGATION_WAIT_MS,
        )
        await page.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError:
        logger.warning("JS 평가 후 URL 변화 없음 — AJAX 갱신일 가능성이 있음")

    try:
        await page.wait_for_load_state("networkidle")
    except PlaywrightTimeoutError:
        logger.warning("상세 네비게이션 후 networkidle 타임아웃 — 진행")

    await page.wait_for_selector(SELECTOR_DETAIL_READY, state="attached")
    return page.url


async def _open_all_collapsibles(page: Page) -> None:
    """상세 페이지의 모든 탭/아코디언/숨은 섹션을 프로그래밍적으로 open 시킨다.

    JS 한 번으로 일괄 처리하고, 클릭 이벤트로 유발된 추가 AJAX 가 있다면
    networkidle 로 한 번 더 안정화한다. 클릭이 아무 효과도 주지 않는 경우에도
    오류를 일으키지 않는다.
    """
    try:
        result = await page.evaluate(_OPEN_COLLAPSIBLES_JS)
    except PlaywrightError as exc:
        logger.warning("탭/아코디언 open 중 JS 오류: {}", exc)
        return

    logger.info(
        "탭/아코디언 강제 open 완료: click_count={}",
        (result or {}).get("click_count", 0),
    )

    try:
        await page.wait_for_load_state("networkidle")
    except PlaywrightTimeoutError:
        logger.warning("탭 open 후 networkidle 타임아웃 — 진행")

    await _sleep_with_jitter(POST_EXPAND_GRACE_SEC)


# ──────────────────────────────────────────────────────────────
# 상세 페이지 DOM 에서 데이터 추출
# ──────────────────────────────────────────────────────────────


async def _collect_metadata_pairs(page: Page) -> dict[str, str]:
    """th/td, dt/dd 쌍을 수집해 label→value dict 로 묶어 반환한다.

    JS 가 반환한 pair 목록을 동일 key 충돌 시 '뒤 값 우선' 으로 정리한다.
    (한 페이지 안에서 라벨 중복은 거의 없지만, 있더라도 크게 해롭지 않은 정책)
    """
    try:
        raw_pairs = await page.evaluate(
            _EXTRACT_METADATA_PAIRS_JS,
            {"maxPairs": MAX_METADATA_PAIR_COUNT},
        )
    except PlaywrightError as exc:
        logger.warning("메타데이터 쌍 수집 JS 실패: {}", exc)
        return {}

    aggregated_pairs: dict[str, str] = {}
    for pair_item in raw_pairs or []:
        # JS 는 [label, value] 2-tuple 을 반환하지만 타입 방어를 해 둔다.
        if not isinstance(pair_item, (list, tuple)) or len(pair_item) != 2:
            continue
        label_text, value_text = pair_item
        if not isinstance(label_text, str) or not isinstance(value_text, str):
            continue
        cleaned_label = label_text.strip()
        cleaned_value = value_text.strip()
        if not cleaned_label or not cleaned_value:
            continue
        aggregated_pairs[cleaned_label] = cleaned_value

    logger.info("상세 메타데이터 수집: {}쌍", len(aggregated_pairs))
    return aggregated_pairs


async def _collect_attachment_candidates(page: Page) -> list[dict[str, Any]]:
    """다운로드 의심 요소를 JS 로 싹 긁어 리스트로 반환한다.

    JS 반환값은 `{text, href, onclick, tag_name, outer_html}` 을 원소로 갖는
    리스트이다. 이 함수 자체는 Python 레벨에서 해석/정규화를 하지 않고
    원본을 그대로 반환한다(정규화는 `_normalize_attachment_candidates` 에서).
    """
    try:
        raw_candidates = await page.evaluate(
            _EXTRACT_ATTACHMENTS_JS,
            {
                "functionNames": list(JS_DOWNLOAD_FUNCTION_NAMES),
                "fileExtensions": list(ATTACHMENT_FILE_EXTENSIONS),
            },
        )
    except PlaywrightError as exc:
        logger.error("첨부파일 후보 수집 JS 실패: {}", exc)
        raise

    if not isinstance(raw_candidates, list):
        logger.warning("첨부파일 후보 JS 반환값이 리스트가 아님 — 무시")
        return []

    logger.info("첨부파일 후보 수: {}건", len(raw_candidates))
    return raw_candidates


def _normalize_attachment_candidates(
    raw_candidates: list[dict[str, Any]],
    base_url: str,
) -> list[dict[str, Any]]:
    """JS 수집 결과를 파이썬 dict 스키마로 정제/중복 제거한다.

    각 후보 → `{original_filename, file_ext, download_url, download_trigger}` 로 변환한다.
    중복 판정 키는 `(original_filename, download_url, frozen_trigger_signature)` 이다.

    `download_trigger` 와 `download_url` 중 하나는 반드시 채워진다.
    둘 다 비어 있는 후보(파일명만 있고 실행 수단이 없는 경우)는 버린다.
    """
    normalized_entries: list[dict[str, Any]] = []
    seen_signatures: set[tuple[str, Optional[str], Optional[tuple[Any, ...]]]] = set()

    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            continue

        raw_href = str(raw_candidate.get("href") or "")
        raw_onclick = str(raw_candidate.get("onclick") or "")
        raw_tag_name = str(raw_candidate.get("tag_name") or "")

        download_trigger_info = _parse_onclick_call(raw_onclick)

        download_url = _derive_download_url(raw_href, base_url)

        # 직접 URL 도 없고 JS 트리거도 없으면 다운로드 수단이 없는 것과 같다.
        if download_url is None and download_trigger_info is None:
            continue

        guessed_filename = _guess_filename_from_candidate(raw_candidate)
        file_extension = _extract_file_extension(
            guessed_filename,
            raw_href,
            raw_onclick,
        )

        # 트리거가 있으면 부가 정보(tag, href) 도 함께 담아 다음 subtask 가 활용하게 한다.
        if download_trigger_info is not None:
            download_trigger_info["tag_name"] = raw_tag_name
            download_trigger_info["href"] = raw_href

        trigger_signature: Optional[tuple[Any, ...]]
        if download_trigger_info is None:
            trigger_signature = None
        else:
            trigger_signature = (
                download_trigger_info["function_name"],
                tuple(download_trigger_info["arguments"]),
            )
        dedupe_key = (guessed_filename, download_url, trigger_signature)
        if dedupe_key in seen_signatures:
            continue
        seen_signatures.add(dedupe_key)

        normalized_entries.append(
            {
                "original_filename": guessed_filename,
                "file_ext": file_extension,
                "download_url": download_url,
                "download_trigger": download_trigger_info,
            }
        )

    logger.info(
        "첨부파일 정규화 완료: 후보 {}건 → 유효 {}건",
        len(raw_candidates),
        len(normalized_entries),
    )
    return normalized_entries


# ──────────────────────────────────────────────────────────────
# 공개 엔트리포인트
# ──────────────────────────────────────────────────────────────


async def scrape_detail(
    row_metadata: dict[str, Any],
    *,
    page: Optional[Page] = None,
    settings: Optional[Settings] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_sec: float = RETRY_BACKOFF_BASE_SEC,
) -> dict[str, Any]:
    """IRIS 공고의 상세 페이지에서 본문 메타와 첨부파일 트리거를 수집한다.

    실제 파일 다운로드는 수행하지 않는다. 반환된 attachments 의 각 원소는
    후속 subtask 가 HTTP 레벨로 파일을 받아오기 위한 정보를 담고 있다.

    Args:
        row_metadata:
            목록 스크래퍼가 반환한 단일 row dict. 최소한 `iris_announcement_id`
            가 필요하며, `detail_url` 또는 `detail_onclick` 중 하나 이상이
            유효해야 상세 진입이 가능하다.
        page:
            재사용할 Playwright Page. None 이면 이 함수가 자체적으로 browser/
            context/page 를 열었다 닫는다(스탠드얼론 실행 지원).
        settings:
            주입할 `Settings`. None 이면 `get_settings()` 사용.
        max_attempts:
            진입/수집 파이프라인 전체에 대한 재시도 횟수. 각 단계의 타임아웃/
            일시적 DOM 오류에 대응한다.
        backoff_base_sec:
            지수 백오프 기본 단위(초).

    Returns:
        dict 스키마:
            - iris_announcement_id: str
            - detail_url:           str (최종 URL)
            - raw_metadata:         dict[str, str]
            - attachments:          list[dict]  (스키마는 모듈 docstring 참고)

    Raises:
        KeyError:
            row_metadata 에 `iris_announcement_id` 가 없는 경우.
        ValueError:
            상세 진입 수단을 찾지 못한 경우.
        PlaywrightError:
            재시도 한도 내에서도 진입/수집이 실패한 경우.
    """
    if "iris_announcement_id" not in row_metadata:
        raise KeyError("row_metadata 에 'iris_announcement_id' 가 반드시 포함되어야 합니다.")

    effective_settings = settings or get_settings()
    iris_announcement_id = row_metadata["iris_announcement_id"]

    async def run_full_pipeline(active_page: Page) -> dict[str, Any]:
        """단일 Page 를 받아 진입→open→수집을 한 번 실행한다(재시도 단위)."""
        final_url = await _navigate_to_detail_page(
            active_page, row_metadata, effective_settings
        )
        await _sleep_with_jitter(POST_NAVIGATION_GRACE_SEC)

        await _open_all_collapsibles(active_page)

        raw_metadata_pairs = await _collect_metadata_pairs(active_page)
        raw_attachment_candidates = await _collect_attachment_candidates(active_page)
        normalized_attachments = _normalize_attachment_candidates(
            raw_attachment_candidates,
            effective_settings.base_url,
        )

        return {
            "iris_announcement_id": iris_announcement_id,
            "detail_url": final_url,
            "raw_metadata": raw_metadata_pairs,
            "attachments": normalized_attachments,
        }

    # 재사용 page 가 주어진 경우: 그 page 로 파이프라인 실행(재시도 감싸기).
    if page is not None:
        async def reuse_existing_page() -> dict[str, Any]:
            """주입된 page 로 파이프라인을 실행한다."""
            return await run_full_pipeline(page)

        return await _with_retry(
            reuse_existing_page,
            description=f"scrape_detail[{iris_announcement_id}]",
            max_attempts=max_attempts,
            backoff_base_sec=backoff_base_sec,
        )

    # 재사용 page 가 없으면 이 함수가 브라우저를 소유한다.
    async with _open_browser_context(effective_settings) as (_browser, _context, owned_page):

        async def run_with_owned_page() -> dict[str, Any]:
            """이 함수 스코프에서 새로 연 page 로 파이프라인을 실행한다."""
            return await run_full_pipeline(owned_page)

        return await _with_retry(
            run_with_owned_page,
            description=f"scrape_detail[{iris_announcement_id}]",
            max_attempts=max_attempts,
            backoff_base_sec=backoff_base_sec,
        )


__all__ = [
    "scrape_detail",
    "SELECTOR_DETAIL_READY",
    "JS_DOWNLOAD_FUNCTION_NAMES",
    "ATTACHMENT_FILE_EXTENSIONS",
    "ONCLICK_FUNCTION_CALL_PATTERN",
    "ONCLICK_STRING_ARGUMENT_PATTERN",
    "MAX_METADATA_PAIR_COUNT",
    "POST_NAVIGATION_GRACE_SEC",
    "POST_EXPAND_GRACE_SEC",
    "JS_NAVIGATION_WAIT_MS",
]
