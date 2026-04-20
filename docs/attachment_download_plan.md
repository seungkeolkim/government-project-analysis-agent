# IRIS 첨부파일 다운로드 방안 설계 문서

> **참고**: 이 문서는 IRIS 소스 adapter 관련 첨부 수집 설계를 다루는 IRIS 전용 설계 문서다.

작성일: 2026-04-20  
작성 근거: task 00003 subtask 00003-3  
작성자: Coder Agent (00003-3)

---

## 구현 상태 (task 00008 완료 기준, 2026-04-20)

> **설계 의도 vs. 실제 구현 차이 요약**

| 항목 | 설계 문서(4절) 권장안 | 실제 구현 선택 | 이유 |
|---|---|---|---|
| 다운로드 방식 | 옵션 A — httpx 직접 GET (세션 없이 동작 실측) | **Playwright headless 주 경로, httpx 폴백** | 사용자가 'headless 브라우저 제어 필수'를 명시 요청 |
| Docker 이미지 | 변경 불필요 | Playwright chromium 추가 (~400MB 증가) | 위와 동일 |
| 저장 경로 | `downloads/<ancmId>/` | `downloads/{source_type}/{source_announcement_id}/` | 소스 확장성 고려(NTIS 등) |
| CLI 플래그 | `--skip-attachments` 추가 | **구현됨** | 설계 문서 9-2절 그대로 |
| DB UPSERT 키 | `(announcement_id, original_filename)` | **구현됨** | 설계 문서 6절 그대로 |
| 실패 기록 | `raw_metadata.attachment_errors` | **구현됨** | 설계 문서 8절 그대로 |

### 구현된 모듈 위치

| 역할 | 파일 |
|---|---|
| 파일명·경로 정제 유틸 | `app/scraper/attachment_paths.py` |
| 첨부 다운로더 (Playwright 주 / httpx 폴백) | `app/scraper/attachment_downloader.py` |
| DB UPSERT·조회 헬퍼 | `app/db/repository.py` (`upsert_attachment`, `get_attachment_by_id`, 등) |
| CLI 오케스트레이션 | `app/cli.py` (`_scrape_and_store_attachments`, `--skip-attachments`) |
| 웹 첨부 목록·다운로드 API | `app/web/main.py` (`GET /attachments/{id}/download`) |
| 웹 첨부 목록 템플릿 | `app/web/templates/detail.html` |

---

## 1. 배경 — 왜 첨부 다운로드가 별도 설계가 필요한가

IRIS 공고 상세 페이지의 첨부파일 링크는 **평범한 `<a href="...pdf">` 형태가 아니다**.
모든 첨부 링크는 아래와 같이 JavaScript onclick 함수 호출 형태로 숨겨져 있다.

```html
<a class="file_down"
   href="javascript:f_bsnsAncm_downloadAtchFile('atchDocId','atchFileId','파일명','파일크기');">
    파일명
</a>
```

따라서 단순 HTML 파싱으로는 다운로드 URL을 얻을 수 없고, 함수 파라미터를 추출한 뒤
별도의 HTTP 요청으로 파일을 내려받아야 한다.

---

## 2. 조사 방법 & 실제 관찰 결과

### 2-1. 탐사 방법

00003-1 subtask에서 `detail_scraper.py`로 수집·저장한 `detail_html`(DB `announcements.detail_html`)을
BeautifulSoup으로 파싱하여 `div.add_file_list a.file_down` 요소를 추출하고
`href` 속성의 `f_bsnsAncm_downloadAtchFile(...)` 인자를 정규식으로 분리했다.

### 2-2. 공고 020640 (2026년도 한-스페인 공동연구사업 신규과제 공모)

첨부 파일 5건 확인. 모두 **공통 atchDocId**(공고 단위)와 **개별 atchFileId**(파일 단위)를 가진다.

| # | atchDocId | atchFileId | 파일명 | 크기 |
|---|-----------|------------|--------|------|
| 1 | `0OpIl0OfKA+G8maW5qOq1g==` | `iihwlTGtyyIXF3GmORLK6w==` | 붙임1. 한-스페인 신규과제 공모 국문 공고문.pdf | 381.6 KB |
| 2 | `0OpIl0OfKA+G8maW5qOq1g==` | `YlelKkXLKy0wwUpuqaUO2g==` | 붙임2. 한-스페인 신규과제 공모 영문 공고문.pdf | 359.4 KB |
| 3 | `0OpIl0OfKA+G8maW5qOq1g==` | `HGviYPZKaCJ069xj6BFUKg==` | 붙임3. 제출서류 양식.zip | 285.7 KB |
| 4 | `0OpIl0OfKA+G8maW5qOq1g==` | `VV410Bph8VHENf96mHXbuA==` | 붙임4. FAQ.pdf | 451.9 KB |
| 5 | `0OpIl0OfKA+G8maW5qOq1g==` | `gT0LvpG8jLJMfFM6Zisbkw==` | 붙임5. IRIS 이용 관련 매뉴얼 별첨자료.zip | 17.6 MB |

### 2-3. 공고 020895 (2026년도 한-일 인력교류사업 신규과제 공고)

첨부 파일 2건:

| # | atchDocId | atchFileId | 파일명 | 크기 |
|---|-----------|------------|--------|------|
| 1 | `hku4LNaQDX+Km0iIdIRD7g==` | `G05EqofHLyX5bRcCdIJ+3Q==` | 붙임01 한-일 인력교류 신규과제 공고문.hwpx | 113.4 KB |
| 2 | `hku4LNaQDX+Km0iIdIRD7g==` | `d6hwb9m5zYtESrLV7QVW/A==` | 붙임02 한-일 인력교류 제출양식.zip | 183.8 KB |

**관찰 결과 패턴 정리:**

- `atchDocId`: 공고 단위로 공유(같은 공고의 모든 첨부가 동일한 값)
- `atchFileId`: 파일 단위로 고유
- 두 값 모두 Base64 인코딩된 문자열(길이 24자 내외, `==` 패딩 포함)
- 파일명은 `href`의 함수 인자 3번째, 크기(바이트)는 4번째 파라미터

### 2-4. 실제 다운로드 플로우 탐사 (2026-04-20 실측)

IRIS 상세 페이지의 JavaScript를 분석한 결과 다운로드 플로우는 2단계다:

**1단계 — 확인 AJAX (선택적):**
```
POST https://www.iris.go.kr/comm/file/retrieveCheckFileDownload.do
Content-Type: application/x-www-form-urlencoded
Body: atchFileId=<encoded>&atchDocId=<encoded>
응답: {} (빈 JSON)
```

**2단계 — 실제 파일 다운로드:**
```
GET https://www.iris.go.kr/comm/file/fileDownload.do
  ?atchDocId=<url_encoded_base64>
  &atchFileId=<url_encoded_base64>
```

**실측 결과 (세션 쿠키 없이 직접 GET 요청):**
```
HTTP/1.1 200 OK
Content-Type: application/octet-stream;charset=UTF-8
Content-Disposition: attachment; filename="%EB%B6%99%EC%9E%84..."  (URL 인코딩된 한글 파일명)
Content-Length: 390801
```

> **핵심 발견: Playwright 없이 httpx 직접 GET만으로 파일 바이너리 취득 가능.**  
> 세션 인증 없이 `atchDocId` + `atchFileId` 파라미터만 있으면 파일이 내려온다.  
> 단, JSESSIONID 쿠키는 응답 헤더에 새로 발급되므로, 연속 다운로드 시 쿠키를 유지하면 서버 부하 관점에서 유리하다.

파일명은 `Content-Disposition` 헤더에 URL 인코딩된 UTF-8 한글로 제공된다:
```
filename="%EB%B6%99%EC%9E%841. 2026년도 한-스페인 ... 국문 공고문.pdf"
→ urllib.parse.unquote() 로 복원: "붙임1. 2026년도 한-스페인 ... 국문 공고문.pdf"
```

---

## 3. 후보 기술 비교

### 옵션 A — httpx만으로 직접 POST → GET

**방법:**
1. `detail_html`에서 BeautifulSoup + 정규식으로 `atchDocId`, `atchFileId`, 파일명, 파일크기 추출
2. (선택) `retrieveCheckFileDownload.do` POST
3. `fileDownload.do?atchDocId=...&atchFileId=...` GET → 바이너리 스트림 저장

**장점:**
- Playwright/Chromium 의존성 없음 → Docker 이미지 크기 최소 유지
- 속도 빠름 (브라우저 기동 없음)
- 이미 `detail_scraper.py`의 httpx 클라이언트 패턴 재사용 가능
- **실측으로 세션 없이도 파일 취득 가능함을 확인**

**단점:**
- IRIS가 향후 다운로드에 로그인 세션 필수화할 경우 대응 필요
- 서버가 Referer 체크를 강화하면 403 발생 가능성 있음

**리스크:**
- 현재 세션 없이 동작하지만 IRIS 정책 변경 가능성 있음
- `atchDocId`/`atchFileId` 파라미터가 Base64라 만료 토큰일 수도 있음(실측에서는 만료 없음 확인)

---

### 옵션 B — Playwright headless 브라우저로 링크 클릭

**방법:**
1. Playwright로 상세 페이지 (`retrieveBsnsAncmView.do?ancmId=...`) 로드
2. `page.expect_download()` 컨텍스트 안에서 `a.file_down` 클릭
3. 다운로드 완료 후 저장 경로로 이동

**장점:**
- JavaScript 환경을 그대로 재현하므로 미래 IRIS 변경에 강건
- `f_bsnsAncm_downloadAtchFile` 함수 내부가 변경돼도 대응 가능

**단점:**
- Chromium 의존성 → Docker 이미지 수백 MB 증가
- 브라우저 기동 오버헤드 (공고당 수 초)
- headless 환경에서 다운로드 경로 제어가 복잡(`accept_downloads=True` 필수)
- `docker/Dockerfile` 변경 필요 (`playwright install --with-deps chromium`)

**리스크:**
- IRIS가 봇 감지를 강화하면 headless 브라우저도 차단 가능
- 세션 유지, 세션 만료 대응 로직 추가 필요

---

### 옵션 C — 혼합 (링크 파싱은 httpx, 파일 취득은 옵션 A/B 선택)

**방법:**
- 링크 파라미터 추출: `detail_html` 정적 파싱 (이미 00003-1에서 DB 저장됨)
- 파일 취득: A(httpx)를 우선 시도 → 실패 시 B(Playwright) fallback

**장점:** 유연성 최대  
**단점:** 복잡도 높음, 불필요한 이중 구현 위험

---

## 4. 권장안과 근거

**설계 시 권장안: 옵션 A — httpx 직접 GET 다운로드**  
**실제 구현 선택: Playwright headless 주 경로, httpx 폴백 (사용자 요청 반영)**

설계 단계에서는 실측 결과(세션 없이 httpx GET만으로 파일 취득 가능)를 근거로 옵션 A를 권장했다.
그러나 task 00008에서 사용자가 "DOM이 숨겨져 있었으니 브라우저 제어 headless 방식 사용"을 명시적으로 요청하여,
Playwright를 주 경로로 채택하고 httpx를 폴백으로 유지하는 방식으로 구현되었다.

실측에서 httpx만으로도 파일 취득이 가능함은 여전히 사실이므로,
IRIS가 향후 세션·Referer 검증을 강화하더라도 현재 구현의 httpx 폴백 경로에서도 대응 가능하다.

**httpx 폴백이 동작하는 조건 (현재 구현 기준):**
- Playwright 기동 실패(브라우저 바이너리 없음, 환경 오류)
- `a.file_down` DOM 요소를 찾지 못한 경우
- `page.expect_download()` 타임아웃 발생

**httpx 방식이 차단될 경우 판단 기준:**
- `fileDownload.do` 응답이 301/302 로그인 페이지로 리다이렉트될 때
- `Content-Length` 가 예상 파일 크기보다 현저히 작거나 Content-Type이 `text/html`로 변경될 때
- 위 상황에서는 전체 흐름을 Playwright 전용으로 전환하면 해결된다

---

### 옵션 B(Playwright) 구현 내역 (task 00008에서 주 경로로 채택)

필요 브라우저: `chromium`  
headless 여부: `headless=True`  
다운로드 디렉터리: `context = browser.new_context(accept_downloads=True)`  

```python
async with async_playwright() as pw:
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(accept_downloads=True)
    page = await context.new_page()
    await page.goto(detail_url)
    async with page.expect_download() as download_info:
        await page.click("a.file_down >> nth=0")
    download = await download_info.value
    await download.save_as(save_path)
    await browser.close()
```

Docker 이미지에서 브라우저 설치:
```dockerfile
RUN pip install playwright && playwright install --with-deps chromium
```
→ `docker/Dockerfile`의 `RUN pip install -e .` 다음에 추가

---

## 5. 저장 경로 / 파일명 규약

### 기본 경로 형식

```
./data/downloads/{source_type}/{source_announcement_id}/{sanitized_filename}
```

`source_type` 과 `source_announcement_id` 는 모두 `sanitize_path_component()` 로 정제되어 경로 구분자·특수문자가 `_` 로 치환된다.

예시 (IRIS, ancmId=020640):
```
./data/downloads/IRIS/020640/붙임1._2026년도_한-스페인_공동연구사업_신규과제_공모_국문_공고문.pdf
```

> **설계 문서와의 차이**: 원 설계는 `downloads/<ancmId>/` 였으나, NTIS 등 다른 소스 추가 시 ID 충돌을 방지하기 위해 `downloads/{source_type}/{id}/` 로 구현되었다.

### 파일명 정제 규칙 (경로 트래버설 방어)

다음 규칙을 순서대로 적용한다:

1. `Content-Disposition` 헤더의 `filename=` 값을 `urllib.parse.unquote()`로 UTF-8 디코딩
2. 디코딩 실패 시 `f_bsnsAncm_downloadAtchFile` 3번째 파라미터(파일명)를 원문 사용
3. 경로 트래버설 방어:
   - `..` 포함 경로 조각 제거 (`pathlib.Path(name).name` 으로 basename만 추출)
   - `/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|` 를 `_`로 치환
   - 선행 `.` 제거 (숨김 파일 방지)
4. 공백은 `_`로 치환 (셸 처리 편의)
5. 파일명 최대 길이: 200자 초과 시 잘라내고 확장자 보존

```python
import re
from pathlib import Path
from urllib.parse import unquote

def sanitize_filename(raw_name: str) -> str:
    """다운로드 파일명을 경로 트래버설 안전한 형태로 정제한다."""
    name = Path(unquote(raw_name)).name          # basename만 추출
    name = re.sub(r'[/\\:*?"<>|]', '_', name)   # 금지 문자 치환
    name = name.replace(' ', '_')                # 공백 치환
    name = name.lstrip('.')                      # 선행 점 제거
    if len(name) > 200:
        stem, ext = Path(name).stem, Path(name).suffix
        name = stem[:200 - len(ext)] + ext
    return name or 'attachment'
```

---

## 6. DB 반영 — Attachment 모델 매핑

기존 `Attachment` 모델(`app/db/models.py`)이 그대로 사용 가능하다.

| Attachment 컬럼 | 첨부 다운로드 시 채울 값 |
|---|---|
| `announcement_id` | `Announcement.id` (FK) |
| `original_filename` | `f_bsnsAncm_downloadAtchFile` 3번째 파라미터 (파일명 원문) |
| `stored_path` | `./data/downloads/<ancmId>/<sanitized_filename>` |
| `file_ext` | 파일명에서 추출한 확장자 소문자 (예: `pdf`, `hwpx`, `zip`) |
| `file_size` | 4번째 파라미터(바이트 수) 또는 `Content-Length` 응답 헤더 |
| `download_url` | `https://www.iris.go.kr/comm/file/fileDownload.do?atchDocId=...&atchFileId=...` |
| `sha256` | 저장 완료 후 파일 해시 계산 |
| `downloaded_at` | 저장 완료 시각 (UTC) |

**UPSERT 키**: `(announcement_id, original_filename)` 로 기존 레코드 조회 → `sha256` 비교 → 동일하면 스킵, 변경됐으면 갱신 (PROJECT_NOTES.md 결정 준수).

---

## 7. 재시도·차단 회피 정책

- **요청 지연**: 공고당 `settings.request_delay_sec` (기본 1.5초) + 균등분포 지터 0.5~1.5초
- **파일당 지연**: 같은 공고 내 첨부 파일 간 0.5초 이상 추가 지연
- **세션 쿠키 유지**: `detail_scraper.py`에서 사용한 httpx.AsyncClient를 attachment_downloader에도 재사용하거나, 같은 Client 인스턴스로 상세 페이지 조회 → 파일 다운로드 순으로 연결하여 JSESSIONID 쿠키가 자동 유지되도록 함
- **Referer**: `https://www.iris.go.kr/contents/retrieveBsnsAncmView.do?ancmId={ancmId}` 로 설정 (상세 페이지에서 클릭한 것처럼 보이게)
- **User-Agent**: `detail_scraper.py`와 동일한 Chrome UA 사용

---

## 8. 실패 모드와 기록 방식

다운로드 실패 시 `Attachment` row를 생성하지 않는다.  
대신 해당 공고의 `raw_metadata` JSON에 아래 형식으로 오류를 누적한다:

```json
{
  "attachment_errors": [
    {
      "original_filename": "붙임1. ... 공고문.pdf",
      "atch_file_id": "iihwlTGtyyIXF3GmORLK6w==",
      "error": "HTTP 403: Forbidden",
      "attempted_at": "2026-04-20T03:00:00Z"
    }
  ]
}
```

`attachment_errors` 키는 기존 `raw_metadata`에 병합(merge)한다. `upsert_announcement` 또는 별도 helper 함수로 `raw_metadata` 갱신.

---

## 9. 구현 완료 내역 (task 00008)

> 이전에 "다음 task 구현 체크리스트"였던 섹션. task 00008에서 모두 구현 완료.

### 9-1. 모듈 구현 위치

| 역할 | 파일 | 주요 공개 API |
|---|---|---|
| 첨부 링크 추출·다운로드 | `app/scraper/attachment_downloader.py` | `extract_attachment_links()`, `scrape_attachments_for_announcement()` |
| 파일명·경로 정제 | `app/scraper/attachment_paths.py` | `sanitize_filename()`, `build_attachment_path()` |

`scrape_attachments_for_announcement(announcement)` 가 공고 한 건의 첨부파일을 모두 다운로드하고
성공/실패 결과(`AttachmentScrapeResult`)를 반환한다.

### 9-2. CLI 플래그 (구현됨)

`app/cli.py`의 `run` 서브커맨드에 아래 플래그가 추가되었다:

```bash
python -m app.cli run --skip-attachments   # 첨부파일 다운로드 단계를 건너뛴다
```

요약 로그에 `attachment_success/failure/skipped_count` 3종 카운터가 집계된다.

### 9-3. Docker / pyproject 변경 (구현됨)

사용자의 "브라우저 headless" 명시 요청에 따라 Playwright를 주 경로로 채택하여 아래가 실제로 변경되었다.

`pyproject.toml`:
```toml
"playwright>=1.44,<2.0",
```

`docker/Dockerfile` (`pip install -e .` 이후에 추가):
```dockerfile
RUN playwright install --with-deps chromium
```

`docker-compose.yml` (scraper 서비스):
```yaml
shm_size: '256m'   # Chromium /dev/shm 크래시 방지
```

### 9-4. DB 처리 흐름 (구현됨)

`app/cli.py`의 `_scrape_and_store_attachments()` 헬퍼가 아래 순서를 처리한다:

1. 공고 fresh 조회 → `session.expunge()` (비동기 다운로드 중 세션 안전 해제)
2. `scrape_attachments_for_announcement()` 비동기 호출
3. 새 세션 오픈: 성공 항목 → `upsert_attachment()`, 실패 항목 → `raw_metadata.attachment_errors` 병합

UPSERT 키: `(announcement_id, original_filename)` — sha256 동일하면 스킵, 다르면 갱신.

### 9-5. 검증 결과 (구현 당시 실측 근거 재확인)

- `retrieveCheckFileDownload.do` POST는 구현에 포함하지 않음 (실측 결과 불필요).
- `atchDocId`/`atchFileId` 토큰은 실측 범위 내에서 만료 없음 확인.
- Playwright 주 경로가 동작하지 않을 경우 httpx 폴백이 자동 실행된다.

---

## 참고 — IRIS 첨부파일 다운로드 URL 구조

```
베이스 도메인: https://www.iris.go.kr

다운로드 엔드포인트:
  GET /comm/file/fileDownload.do
    ?atchDocId={urllib.parse.quote(atchDocId, safe='')}
    &atchFileId={urllib.parse.quote(atchFileId, safe='')}

확인 엔드포인트 (선택):
  POST /comm/file/retrieveCheckFileDownload.do
  Body (form): atchFileId=...&atchDocId=...
  응답: {} (빈 JSON)

첨부 파라미터 소스:
  div.add_file_list > ul.add_file > li > a.file_down[href]
  href 값: "javascript:f_bsnsAncm_downloadAtchFile('<atchDocId>','<atchFileId>','<파일명>','<크기(bytes)>')"
```
