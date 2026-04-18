"""IRIS 첨부파일 다운로더.

상세 페이지 스크래퍼(`detail_scraper.scrape_detail`) 가 만들어준 정규화된
첨부파일 dict 한 건을 받아서 실제 파일을 로컬 파일시스템에 저장한다.

다운로드 경로는 두 가지가 있다.
    1) `download_url` 이 채워진 경우(직접 GET):
       httpx.AsyncClient 로 GET 한다. 이때 Playwright BrowserContext 의 쿠키를
       그대로 주입하여 IRIS 의 세션을 공유한다.
    2) `download_trigger` 가 채워진 경우(JS 함수 호출):
       `page.expect_download()` 안에서 `page.evaluate()` 로 JS 함수를 호출하여
       Playwright 가 다운로드 이벤트를 가로채도록 한다.
       이 경우 `page` 는 다운로드 트리거 함수가 정의된 페이지(상세 페이지) 위에
       있어야 한다.

`download_url` 과 `download_trigger` 가 모두 있으면 직접 GET 을 우선한다(가볍고
세션도 그대로 공유되므로).

저장 정책:
    - 저장 디렉터리: `settings.download_dir / {sanitized_announcement_id}/`
    - 파일명: 원본 파일명(`original_filename`)을 sanitize. 경로 분리자/제어
      문자/예약 문자 제거, 너무 긴 이름은 utf-8 바이트 기준으로 자른다.
    - 확장자 화이트리스트:
        * pdf / hwp / hwpx / zip 은 정상.
        * doc / docx / xls / xlsx / ppt / pptx 는 허용하되 WARN 로그.
        * 그 외(또는 'unknown') 는 다운로드는 진행하되 ERROR 로 기록.
    - 동일 경로에 파일이 이미 존재하면 재다운로드를 스킵한다(기존 파일의
      sha256 만 다시 계산해서 반환). DB upsert 는 호출자가 어떤 경우든 수행한다.

재시도:
    - 네트워크 오류(httpx 타임아웃/네트워크/5xx, Playwright 타임아웃/오류) 는
      최대 `max_attempts` 회 재시도하고, 각 시도 사이에 지수 백오프
      (`backoff_base_sec * 2^(attempt-1)` 초) 를 적용한다. 4xx 는 즉시 중단한다.

반환 스키마(dict — `repository.upsert_attachment` payload 와 호환):
    - original_filename:   str  (입력 그대로. 비어 있으면 sanitized 와 동일)
    - sanitized_filename:  str  (디스크에 저장한 파일명)
    - stored_path:         str  (저장된 절대 경로)
    - file_ext:            str  (소문자, 점 없음. 미상이면 'unknown')
    - file_size:           int | None
    - sha256:              str | None  (성공 시 64자 hex)
    - download_url:        str | None  (직접 URL. JS 트리거인 경우 None)
    - reused_existing:     bool        (기존 파일을 재사용했는지)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import httpx
from loguru import logger
from playwright.async_api import (
    BrowserContext,
    Page,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from app.config import Settings, get_settings
from app.scraper.list_scraper import DEFAULT_USER_AGENT

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# IRIS 의 핵심 첨부파일 확장자(정상 처리, 로그 없음).
PRIMARY_FILE_EXTENSIONS: frozenset[str] = frozenset({"pdf", "hwp", "hwpx", "zip"})

# 추가 허용 확장자 — 다운로드는 하되 WARN 로그를 남긴다.
SECONDARY_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {"doc", "docx", "xls", "xlsx", "ppt", "pptx"}
)

# 다운로드 재시도 기본값.
DEFAULT_DOWNLOAD_MAX_ATTEMPTS: int = 3
DOWNLOAD_RETRY_BACKOFF_BASE_SEC: float = 1.0

# Playwright expect_download 의 최대 대기 시간(ms).
DOWNLOAD_EVENT_TIMEOUT_MS: int = 60_000

# httpx 요청 timeout(초).
HTTPX_TIMEOUT_SEC: float = 60.0

# 디스크에서 SHA-256 계산 시 한 번에 읽을 청크 크기(바이트).
HASH_CHUNK_SIZE_BYTES: int = 1024 * 1024  # 1 MiB

# sanitize 후 파일명의 utf-8 바이트 기준 최대 길이(파일시스템 한계 회피).
MAX_SANITIZED_FILENAME_BYTES: int = 200

# 디렉터리명으로 사용할 announcement id 의 최대 길이.
MAX_DIRNAME_LENGTH: int = 128

# 파일명에서 허용하지 않는 문자(경로 구분자/제어/윈도우 예약문자).
_INVALID_FILENAME_CHAR_PATTERN: re.Pattern[str] = re.compile(
    r"""[\x00-\x1F\x7F<>:"/\\|?*]"""
)

# 디렉터리명에서 허용하지 않는 문자(파일명 + 공백까지 추가로 차단).
_INVALID_DIRNAME_CHAR_PATTERN: re.Pattern[str] = re.compile(
    r"""[\x00-\x1F\x7F<>:"/\\|?*\s]"""
)

# Content-Disposition 헤더에서 filename / filename* 값을 뽑는 정규식.
_CONTENT_DISPOSITION_FILENAME_PATTERN: re.Pattern[str] = re.compile(
    r"""filename\*=(?:UTF-8'')?["']?([^;"']+)|filename=["']?([^;"']+)["']?""",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────
# 파일명 / 확장자 처리
# ──────────────────────────────────────────────────────────────


def _sanitize_filename(raw_name: str, fallback_ext: Optional[str] = None) -> str:
    """파일명에서 경로/제어/예약 문자를 제거해 안전한 이름으로 만든다.

    동작:
        - NFC 정규화로 한글 분리/조합 차이를 흡수한다.
        - 경로 구분자(`/`, `\\`), 제어문자, 윈도우 예약문자(`<>:"|?*`) 를
          모두 `_` 로 치환한다.
        - 선·후행 공백과 점을 제거한다(예: '.hidden ' 같은 위험한 이름 회피).
        - 빈 문자열이 되면 'unnamed' 로 대체한다.
        - utf-8 인코딩이 `MAX_SANITIZED_FILENAME_BYTES` 를 넘으면, 확장자를
          최대한 보존한 채 stem 을 잘라낸다.

    Args:
        raw_name:     IRIS 표기 원본 파일명.
        fallback_ext: raw_name 에서 확장자를 식별할 수 없을 때 붙일 보조
                      확장자(점 없음, 소문자). None 이면 보강하지 않는다.

    Returns:
        디스크에 그대로 쓸 수 있는 안전한 파일명 문자열.
    """
    normalized_name = unicodedata.normalize("NFC", raw_name or "")
    cleaned = _INVALID_FILENAME_CHAR_PATTERN.sub("_", normalized_name)
    cleaned = cleaned.strip().strip(".").strip()
    if not cleaned:
        cleaned = "unnamed"

    # stem / ext 분리. 확장자는 8자 이하의 ASCII 영숫자일 때만 인정한다
    # (한글/유니코드 문자가 우연히 확장자처럼 보이는 경우를 차단).
    base_part, dot, tail_part = cleaned.rpartition(".")
    if dot and tail_part and len(tail_part) <= 8 and tail_part.isascii() and tail_part.isalnum():
        stem_text, ext_text = base_part, tail_part.lower()
    else:
        stem_text, ext_text = cleaned, ""
        if fallback_ext:
            ext_text = fallback_ext.lower().lstrip(".")

    suffix_with_dot = f".{ext_text}" if ext_text else ""
    encoded_suffix = suffix_with_dot.encode("utf-8")
    available_for_stem = MAX_SANITIZED_FILENAME_BYTES - len(encoded_suffix)
    if available_for_stem < 1:
        # 확장자가 길이를 다 먹어버린 비정상 케이스 — 안전한 기본 이름 사용.
        return ("unnamed" + suffix_with_dot)[:MAX_SANITIZED_FILENAME_BYTES]

    encoded_stem = stem_text.encode("utf-8")
    if len(encoded_stem) > available_for_stem:
        truncated_bytes = encoded_stem[:available_for_stem]
        # utf-8 문자 경계가 깨졌을 수 있으므로 errors='ignore' 로 안전 디코드.
        stem_text = truncated_bytes.decode("utf-8", errors="ignore") or "unnamed"

    return stem_text + suffix_with_dot


def _sanitize_dirname(raw_id: str) -> str:
    """공고 ID 를 디렉터리 이름으로 안전화한다.

    공백을 포함한 파일시스템 위험 문자를 모두 `_` 로 치환하고,
    선후행 점/언더스코어를 정리한다. 너무 긴 ID 는 자른다.
    """
    if not raw_id:
        return "unknown_announcement"
    cleaned = _INVALID_DIRNAME_CHAR_PATTERN.sub("_", raw_id)
    cleaned = cleaned.strip("._") or "unknown_announcement"
    return cleaned[:MAX_DIRNAME_LENGTH]


def _classify_file_extension(file_ext: str) -> str:
    """확장자를 정상/추가허용/그외 로 분류한다.

    Returns:
        - "primary"   : pdf/hwp/hwpx/zip
        - "secondary" : doc/docx/xls/xlsx/ppt/pptx
        - "unknown"   : 화이트리스트 밖 또는 식별 실패
    """
    normalized = (file_ext or "").lower().lstrip(".")
    if not normalized or normalized == "unknown":
        return "unknown"
    if normalized in PRIMARY_FILE_EXTENSIONS:
        return "primary"
    if normalized in SECONDARY_FILE_EXTENSIONS:
        return "secondary"
    return "unknown"


def _extract_content_disposition_filename(header_value: Optional[str]) -> Optional[str]:
    """Content-Disposition 헤더에서 filename(또는 filename*) 값을 추출한다.

    - `filename*=UTF-8''xxx` 형태(RFC 6266) 는 percent-decoding 해서 돌려준다.
    - `filename="xxx"` 는 따옴표를 벗긴 값을 돌려준다.
    - 헤더가 없거나 매칭이 없으면 None.
    """
    if not header_value:
        return None
    match = _CONTENT_DISPOSITION_FILENAME_PATTERN.search(header_value)
    if not match:
        return None
    star_value = match.group(1)
    plain_value = match.group(2)
    raw_value = star_value or plain_value
    if raw_value is None:
        return None
    raw_value = raw_value.strip().strip('"').strip("'")
    if star_value:
        try:
            raw_value = unquote(raw_value, encoding="utf-8")
        except (UnicodeDecodeError, LookupError):
            pass
    return raw_value or None


# ──────────────────────────────────────────────────────────────
# 해시 / 재시도 판정 / httpx 클라이언트 빌더
# ──────────────────────────────────────────────────────────────


async def _compute_sha256(file_path: Path) -> str:
    """파일의 SHA-256 해시(hex 64자) 를 반환한다.

    동기 IO 를 별도 스레드로 보내 이벤트 루프를 막지 않는다.
    """

    def _read_and_hash() -> str:
        """블로킹 컨텍스트에서 파일을 청크 단위로 읽으며 해시한다."""
        sha = hashlib.sha256()
        with file_path.open("rb") as fp:
            while True:
                chunk = fp.read(HASH_CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    return await asyncio.to_thread(_read_and_hash)


def _is_retryable_http_error(exc: BaseException) -> bool:
    """httpx 예외가 재시도 대상인지 판정한다.

    재시도 대상:
        - TimeoutException / NetworkError / RemoteProtocolError
        - HTTPStatusError 중 5xx
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


async def _build_httpx_client_from_context(
    context: BrowserContext,
    settings: Settings,
) -> httpx.AsyncClient:
    """Playwright BrowserContext 의 쿠키를 그대로 가진 httpx.AsyncClient 를 만든다.

    User-Agent 는 settings.user_agent 가 비어 있으면 list_scraper 의
    `DEFAULT_USER_AGENT` 를 그대로 쓴다(브라우저가 들고 있는 UA 와 같은 값을
    내보내야 IRIS 가 동일 세션으로 인식할 가능성이 높다).
    """
    raw_cookies = await context.cookies()
    cookie_jar = httpx.Cookies()
    for cookie_record in raw_cookies:
        cookie_name = cookie_record.get("name")
        cookie_value = cookie_record.get("value")
        cookie_domain = cookie_record.get("domain") or ""
        cookie_path = cookie_record.get("path") or "/"
        if not cookie_name or cookie_value is None:
            continue
        cookie_jar.set(
            cookie_name,
            cookie_value,
            domain=cookie_domain,
            path=cookie_path,
        )

    effective_user_agent = settings.user_agent or DEFAULT_USER_AGENT
    request_headers = {
        "Accept": "*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "User-Agent": effective_user_agent,
    }

    return httpx.AsyncClient(
        cookies=cookie_jar,
        headers=request_headers,
        follow_redirects=True,
        timeout=httpx.Timeout(HTTPX_TIMEOUT_SEC),
    )


# ──────────────────────────────────────────────────────────────
# 다운로드 채널 1: 직접 GET (httpx)
# ──────────────────────────────────────────────────────────────


async def _download_via_http_get(
    download_url: str,
    target_path: Path,
    httpx_client: httpx.AsyncClient,
    *,
    max_attempts: int,
    backoff_base_sec: float,
) -> tuple[Path, Optional[str]]:
    """httpx 로 직접 GET 해서 파일을 target_path 에 저장한다.

    - 임시 파일(`target_path + '.part'`) 에 스트리밍 저장한 뒤 atomic rename.
    - 4xx 는 즉시 raise. 5xx/네트워크 오류만 지수 백오프 재시도.

    Returns:
        (저장된 절대 경로, Content-Disposition 에서 추정한 파일명 또는 None).

    Raises:
        httpx 예외: 모든 재시도가 소진된 경우 마지막 예외를 그대로 던진다.
    """
    temp_path = target_path.with_suffix(target_path.suffix + ".part")
    last_exc: Optional[BaseException] = None
    suggested_filename: Optional[str] = None

    for attempt_index in range(1, max_attempts + 1):
        try:
            async with httpx_client.stream("GET", download_url) as response:
                response.raise_for_status()
                suggested_filename = _extract_content_disposition_filename(
                    response.headers.get("content-disposition")
                )
                with temp_path.open("wb") as out_fp:
                    async for chunk in response.aiter_bytes(chunk_size=HASH_CHUNK_SIZE_BYTES):
                        if chunk:
                            out_fp.write(chunk)
            os.replace(temp_path, target_path)
            return target_path, suggested_filename
        except httpx.HTTPError as exc:
            last_exc = exc
            # 임시 파일은 다음 시도 전에 정리.
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            if not _is_retryable_http_error(exc):
                logger.error(
                    "직접 다운로드 실패(재시도 불가): url={} ({}: {})",
                    download_url,
                    type(exc).__name__,
                    exc,
                )
                raise
            if attempt_index >= max_attempts:
                logger.error(
                    "직접 다운로드 최종 실패 ({}회 재시도): url={} ({}: {})",
                    max_attempts,
                    download_url,
                    type(exc).__name__,
                    exc,
                )
                raise
            wait_sec = backoff_base_sec * (2 ** (attempt_index - 1))
            logger.warning(
                "직접 다운로드 실패 {}/{} — {:.1f}초 후 재시도: url={} ({}: {})",
                attempt_index,
                max_attempts,
                wait_sec,
                download_url,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(wait_sec)

    # 도달 불가 — 방어적 raise.
    assert last_exc is not None
    raise last_exc


# ──────────────────────────────────────────────────────────────
# 다운로드 채널 2: JS 트리거 (Playwright expect_download)
# ──────────────────────────────────────────────────────────────


def _serialize_js_string_argument(value: Any) -> str:
    """JS 함수 인자를 문자열 리터럴로 직렬화한다.

    detail_scraper 가 따옴표 인자만 추출하므로 모든 인자는 str 로 들어온다.
    JS 컨텍스트로 안전하게 들어가도록 백슬래시·따옴표·줄바꿈을 escape 한 뒤
    홑따옴표로 감싼다.
    """
    string_value = "" if value is None else str(value)
    escaped = (
        string_value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f"'{escaped}'"


async def _download_via_js_trigger(
    download_trigger: dict[str, Any],
    target_path: Path,
    page: Page,
    *,
    max_attempts: int,
    backoff_base_sec: float,
) -> tuple[Path, Optional[str]]:
    """JS 함수 호출로 다운로드를 유발하고 Playwright 가 가로채 저장한다.

    Args:
        download_trigger: detail_scraper 의 정규화된 트리거 dict.
            최소 `function_name` 과 `arguments` 를 가져야 한다.
        target_path: 최종 저장 경로(부모 디렉터리는 호출자가 보장).
        page: 다운로드 함수가 정의된 페이지(상세 페이지).

    Returns:
        (저장된 경로, Playwright 가 알려준 suggested_filename 또는 None).
    """
    function_name = str(download_trigger.get("function_name") or "").strip()
    arguments_list = download_trigger.get("arguments") or []
    if not function_name:
        raise ValueError(f"download_trigger 에 function_name 이 없습니다: {download_trigger!r}")

    serialized_args = ", ".join(
        _serialize_js_string_argument(arg_value) for arg_value in arguments_list
    )
    invocation_js = (
        f"() => {{ const fn = window['{function_name}']; "
        f"if (typeof fn !== 'function') {{ "
        f"throw new Error('JS 함수 미존재: {function_name}'); }} "
        f"return fn({serialized_args}); }}"
    )

    last_exc: Optional[BaseException] = None
    for attempt_index in range(1, max_attempts + 1):
        try:
            async with page.expect_download(timeout=DOWNLOAD_EVENT_TIMEOUT_MS) as download_info:
                await page.evaluate(invocation_js)
            download_event = await download_info.value
            suggested_filename = download_event.suggested_filename or None
            await download_event.save_as(target_path)
            return target_path, suggested_filename
        except (PlaywrightTimeoutError, PlaywrightError, OSError) as exc:
            last_exc = exc
            if attempt_index >= max_attempts:
                logger.error(
                    "JS 트리거 다운로드 최종 실패 ({}회): {}({!r}) ({}: {})",
                    max_attempts,
                    function_name,
                    arguments_list,
                    type(exc).__name__,
                    exc,
                )
                raise
            wait_sec = backoff_base_sec * (2 ** (attempt_index - 1))
            logger.warning(
                "JS 트리거 다운로드 실패 {}/{} — {:.1f}초 후 재시도: {}({!r}) ({}: {})",
                attempt_index,
                max_attempts,
                wait_sec,
                function_name,
                arguments_list,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(wait_sec)

    assert last_exc is not None
    raise last_exc


# ──────────────────────────────────────────────────────────────
# 공개 엔트리포인트
# ──────────────────────────────────────────────────────────────


def _build_attachment_payload(
    *,
    original_filename: str,
    sanitized_filename: str,
    stored_path: Path,
    file_ext_hint: str,
    sha256_value: Optional[str],
    download_url: Optional[str],
    reused_existing: bool,
) -> dict[str, Any]:
    """`upsert_attachment` 와 호환되는 payload dict 를 생성한다.

    - `file_size` 는 stored_path 가 실제로 존재할 때만 stat 으로 측정한다.
    - `file_ext` 는 hint 를 우선 채택하되, 비어 있으면 sanitized_filename 의
      확장자를 소문자로 추출해서 사용한다(추출 실패 시 'unknown').
    """
    file_size: Optional[int]
    try:
        file_size = stored_path.stat().st_size
    except OSError:
        file_size = None

    if file_ext_hint:
        final_ext = file_ext_hint.lower().lstrip(".")
    else:
        _, dot_separator, tail_text = sanitized_filename.rpartition(".")
        if (
            dot_separator
            and tail_text
            and len(tail_text) <= 8
            and tail_text.isascii()
            and tail_text.isalnum()
        ):
            final_ext = tail_text.lower()
        else:
            final_ext = "unknown"

    return {
        "original_filename": original_filename,
        "sanitized_filename": sanitized_filename,
        "stored_path": str(stored_path),
        "file_ext": final_ext,
        "file_size": file_size,
        "sha256": sha256_value,
        "download_url": download_url,
        "reused_existing": reused_existing,
    }


async def download_attachment(
    iris_announcement_id: str,
    attachment: dict[str, Any],
    *,
    page: Optional[Page] = None,
    httpx_client: Optional[httpx.AsyncClient] = None,
    settings: Optional[Settings] = None,
    max_attempts: int = DEFAULT_DOWNLOAD_MAX_ATTEMPTS,
    backoff_base_sec: float = DOWNLOAD_RETRY_BACKOFF_BASE_SEC,
) -> dict[str, Any]:
    """첨부파일 한 건을 디스크에 저장하고 DB upsert 용 payload 를 반환한다.

    선택 알고리즘:
        1. `attachment['download_url']` 이 비어 있지 않으면 직접 GET 으로 받는다.
           - `httpx_client` 가 주어지면 그것을 재사용한다.
           - 없으면 `page.context.cookies()` 로부터 세션 쿠키를 복사한
             일회용 클라이언트를 만들어 사용한다.
        2. 그 외에는 `download_trigger` 를 page.expect_download 로 처리한다.
        3. 둘 다 없으면 ValueError.

    동일 경로에 파일이 이미 있으면 다운로드를 스킵하고 기존 파일의 sha256 을
    그대로 반환한다(재실행 안전성).

    Args:
        iris_announcement_id: 저장 디렉터리 분리에 사용할 IRIS 공고 ID.
        attachment: detail_scraper 의 정규화된 첨부파일 dict.
            아래 키 중 적어도 하나가 채워져 있어야 한다.
                - download_url    : 직접 GET 가능한 절대 URL
                - download_trigger: JS 트리거 dict
            그 외 'original_filename', 'file_ext' 도 사용한다.
        page: JS 트리거 다운로드 또는 쿠키 복제용 Playwright Page.
            JS 트리거가 있는데 page=None 이면 ValueError.
        httpx_client: 직접 GET 시 재사용할 httpx 클라이언트(여러 첨부를
            한 세션으로 받을 때 효율적). 미지정 시 일회용 생성.
        settings: 주입할 Settings. None 이면 `get_settings()`.
        max_attempts: 네트워크 재시도 횟수(>=1).
        backoff_base_sec: 지수 백오프 기본 단위(초).

    Returns:
        dict — 모듈 docstring 의 '반환 스키마' 참고.

    Raises:
        ValueError: 다운로드 수단이 부족하거나 트리거 정보가 비정상인 경우.
        httpx.HTTPError / PlaywrightError: 재시도 한도 내에서도 실패한 경우.
    """
    effective_settings = settings or get_settings()
    effective_settings.ensure_runtime_paths()
    safe_max_attempts = max(int(max_attempts), 1)

    original_filename_raw = str(attachment.get("original_filename") or "").strip()
    file_ext_hint = str(attachment.get("file_ext") or "").strip().lower().lstrip(".")
    download_url_value = attachment.get("download_url")
    download_trigger_value = attachment.get("download_trigger")

    if not download_url_value and not download_trigger_value:
        raise ValueError(
            "다운로드 수단이 없습니다(download_url, download_trigger 모두 비어 있음). "
            f"attachment={attachment!r}"
        )

    # 확장자 분류에 따른 로깅(다운로드 정책의 가시성 확보).
    classification = _classify_file_extension(file_ext_hint)
    if classification == "secondary":
        logger.warning(
            "비주력 확장자 다운로드: ext={!r} filename={!r} announcement={}",
            file_ext_hint,
            original_filename_raw,
            iris_announcement_id,
        )
    elif classification == "unknown":
        logger.error(
            "화이트리스트 외 확장자 다운로드: ext={!r} filename={!r} announcement={}",
            file_ext_hint,
            original_filename_raw,
            iris_announcement_id,
        )

    sanitized_filename = _sanitize_filename(
        original_filename_raw or "unnamed",
        fallback_ext=file_ext_hint if classification != "unknown" else None,
    )
    sanitized_dirname = _sanitize_dirname(iris_announcement_id)
    target_dir = effective_settings.download_dir / sanitized_dirname
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / sanitized_filename

    download_url_str: Optional[str] = (
        str(download_url_value) if download_url_value else None
    )

    # 동일 파일이 이미 존재 → 재다운로드 스킵, sha 만 다시 계산한다.
    if target_path.exists():
        logger.info(
            "기존 파일 재사용(다운로드 스킵): announcement={} path={}",
            iris_announcement_id,
            target_path,
        )
        existing_sha = await _compute_sha256(target_path)
        return _build_attachment_payload(
            original_filename=original_filename_raw or sanitized_filename,
            sanitized_filename=sanitized_filename,
            stored_path=target_path,
            file_ext_hint=file_ext_hint,
            sha256_value=existing_sha,
            download_url=download_url_str,
            reused_existing=True,
        )

    suggested_filename: Optional[str] = None
    owns_httpx_client = False
    active_httpx_client = httpx_client

    try:
        if download_url_str:
            if active_httpx_client is None:
                if page is None:
                    raise ValueError(
                        "직접 다운로드용 httpx_client 도 page 도 주어지지 않았습니다. "
                        "둘 중 하나가 필요합니다."
                    )
                active_httpx_client = await _build_httpx_client_from_context(
                    page.context, effective_settings
                )
                owns_httpx_client = True
            stored_path, suggested_filename = await _download_via_http_get(
                download_url=download_url_str,
                target_path=target_path,
                httpx_client=active_httpx_client,
                max_attempts=safe_max_attempts,
                backoff_base_sec=backoff_base_sec,
            )
        else:
            if page is None:
                raise ValueError(
                    "JS 트리거 다운로드를 하려면 page 가 필요합니다. "
                    "상세 페이지 위에 있는 Page 를 전달해주세요."
                )
            if not isinstance(download_trigger_value, dict):
                raise ValueError(
                    f"download_trigger 가 dict 가 아닙니다: {download_trigger_value!r}"
                )
            stored_path, suggested_filename = await _download_via_js_trigger(
                download_trigger=download_trigger_value,
                target_path=target_path,
                page=page,
                max_attempts=safe_max_attempts,
                backoff_base_sec=backoff_base_sec,
            )
    finally:
        if owns_httpx_client and active_httpx_client is not None:
            await active_httpx_client.aclose()

    sha256_value = await _compute_sha256(stored_path)

    # 원본 파일명이 비어 있던 경우 응답이 알려준 이름으로 보강한다.
    effective_original_filename = (
        original_filename_raw or suggested_filename or sanitized_filename
    )

    payload = _build_attachment_payload(
        original_filename=effective_original_filename,
        sanitized_filename=sanitized_filename,
        stored_path=stored_path,
        file_ext_hint=file_ext_hint,
        sha256_value=sha256_value,
        download_url=download_url_str,
        reused_existing=False,
    )

    logger.info(
        "다운로드 완료: announcement={} file={!r} size={} sha256={}",
        iris_announcement_id,
        sanitized_filename,
        payload["file_size"],
        sha256_value[:12],
    )
    return payload


async def download_attachments_for_announcement(
    detail_result: dict[str, Any],
    *,
    page: Page,
    settings: Optional[Settings] = None,
    max_attempts: int = DEFAULT_DOWNLOAD_MAX_ATTEMPTS,
    backoff_base_sec: float = DOWNLOAD_RETRY_BACKOFF_BASE_SEC,
) -> list[dict[str, Any]]:
    """한 공고의 모든 첨부파일을 순차 다운로드하는 편의 헬퍼.

    `detail_result` 는 `scrape_detail` 의 반환값을 그대로 받는다. 같은 세션을
    공유하기 위해 httpx 클라이언트를 한 번만 만들어 재사용한다.

    Args:
        detail_result: `scrape_detail` 결과 dict. 최소 'iris_announcement_id'
            와 'attachments' 키가 있어야 한다.
        page: 상세 페이지 위에 있는 Playwright Page(쿠키 복제 + JS 트리거 양쪽).
        settings: 주입할 Settings.
        max_attempts: 첨부파일별 재시도 횟수.
        backoff_base_sec: 지수 백오프 기본 단위(초).

    Returns:
        attachment 별로 download_attachment 가 만든 payload 의 리스트.
        실패한 첨부파일은 리스트에 포함되지 않는다(에러는 로그로만 남김).
    """
    iris_announcement_id = detail_result.get("iris_announcement_id")
    if not iris_announcement_id:
        raise KeyError(
            "detail_result 에 'iris_announcement_id' 가 반드시 포함되어야 합니다."
        )

    attachment_entries = detail_result.get("attachments") or []
    if not isinstance(attachment_entries, list):
        raise TypeError(
            f"detail_result['attachments'] 는 리스트여야 합니다: {attachment_entries!r}"
        )

    effective_settings = settings or get_settings()
    shared_httpx_client = await _build_httpx_client_from_context(
        page.context, effective_settings
    )
    collected_payloads: list[dict[str, Any]] = []

    try:
        for attachment_entry in attachment_entries:
            if not isinstance(attachment_entry, dict):
                logger.warning(
                    "비정상 attachment 항목 스킵(dict 아님): {!r}", attachment_entry
                )
                continue
            try:
                payload = await download_attachment(
                    iris_announcement_id=str(iris_announcement_id),
                    attachment=attachment_entry,
                    page=page,
                    httpx_client=shared_httpx_client,
                    settings=effective_settings,
                    max_attempts=max_attempts,
                    backoff_base_sec=backoff_base_sec,
                )
                collected_payloads.append(payload)
            except (httpx.HTTPError, PlaywrightError, OSError, ValueError) as exc:
                # 한 첨부 실패가 다른 첨부 다운로드를 막지 않게 한다.
                logger.error(
                    "첨부파일 다운로드 실패(스킵): announcement={} file={!r} ({}: {})",
                    iris_announcement_id,
                    attachment_entry.get("original_filename"),
                    type(exc).__name__,
                    exc,
                )
    finally:
        await shared_httpx_client.aclose()

    logger.info(
        "공고 {} 의 첨부 다운로드 완료: 성공 {}/{}건",
        iris_announcement_id,
        len(collected_payloads),
        len(attachment_entries),
    )
    return collected_payloads


__all__ = [
    "download_attachment",
    "download_attachments_for_announcement",
    "PRIMARY_FILE_EXTENSIONS",
    "SECONDARY_FILE_EXTENSIONS",
    "DEFAULT_DOWNLOAD_MAX_ATTEMPTS",
    "DOWNLOAD_RETRY_BACKOFF_BASE_SEC",
    "DOWNLOAD_EVENT_TIMEOUT_MS",
    "MAX_SANITIZED_FILENAME_BYTES",
]
