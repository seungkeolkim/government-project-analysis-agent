# NTIS 사이트 구조 탐사 결과 (00014-1)

> 작성: 2026-04-21  
> 탐사 방법: httpx 직접 요청 (1~2 rps 이하) + BeautifulSoup HTML 파싱  
> 대상 URL: `https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do` (목록), `view.do?roRndUid=…` (상세)

---

## 1. 목록 페이지 구조

### 1-1. 렌더링 방식

**SSR (Server-Side Rendering)** — IRIS와 달리 JSON API가 없다.

```bash
# curl 재현 — 첫 페이지 GET
curl -L -b '' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36' \
  'https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do'
# → status 200, text/html; charset=UTF-8, body: 완전한 HTML 렌더링
```

```bash
# curl 재현 — 2페이지 POST
curl -L -X POST \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'flag=&searchFormList=&searchStatusList=&searchDeptList=&pageIndex=2' \
  'https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do'
# → status 200, text/html — 페이지 이동도 SSR
```

검증: 응답 JSON 파싱 시도 → `JSONDecodeError` (HTML 응답 확인).

### 1-2. 목록 DOM 구조

목록 테이블 선택자: `table thead tr th` 에 `순번`, `현황`, `공고명` 포함.

각 공고 행 구조:
```html
<tr>
  <td><input type="checkbox" name="selectCheckList" value="{roRndUid}" /></td>
  <td>{순번}</td>            <!-- 누적 sequential -->
  <td>{현황}</td>            <!-- 접수예정 / 접수중 / 마감 -->
  <td>
    <a href="/rndgate/eg/un/ra/view.do?roRndUid={roRndUid}&flag=rndList"
       onclick="javascript:fn_view('{roRndUid}'); return false;">
      {공고명}
    </a>
  </td>
  <td>{부처명}</td>
  <td>{접수일}</td>   <!-- YYYY.MM.DD -->
  <td>{마감일}</td>   <!-- YYYY.MM.DD -->
  <td>{D-day}</td>
</tr>
```

핵심: `value="{roRndUid}"` = 상세 페이지 URL 파라미터 = NTIS 내부 PK.

### 1-3. 페이지네이션 POST 파라미터

엔드포인트: `POST https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do`  
Content-Type: `application/x-www-form-urlencoded`

| 파라미터 | 역할 | 예시 |
|---------|------|------|
| `pageIndex` | 1-based 페이지 번호 | `1`, `2`, … |
| `searchStatusList` | 상태 필터 코드 | `P` / `B` / `Y` / `""` |
| `searchFormList` | 공고형태 필터 코드 | `28801` / `28802` / `""` |
| `searchDeptList` | 부처 필터 (쉼표 구분) | `""` (전체) |
| `flag` | 내부 플래그 | `""` |

응답 HTML 내 `<input type="hidden" name="totalCount" value="{N}" />` 에서 전체 건수 파악.  
페이지당 10건 기본 (고정).

---

## 2. 상태 필터 코드 (실측 검증)

공고현황 필터 테이블 `#searchStatusListBtn` 내 `<p id="{코드}">`:

| UI 라벨 | searchStatusList 코드 | 실측 건수 (2026-04-21) |
|---------|----------------------|----------------------|
| 전체 | `""` (빈 문자열) | 76,602 |
| 접수예정 | `P` | 6 |
| 접수중 | `B` | 46 |
| 마감 | `Y` | 74,809 |

> **주의**: IRIS의 `ancmPrg=ancmPre|ancmIng|ancmEnd` 코드와 완전히 다름.  
> NTIS는 `P` / `B` / `Y` 단일 문자 코드.

---

## 3. 공고형태 필터 코드 (실측 검증)

| UI 라벨 | searchFormList 코드 | 실측 건수 |
|--------|-------------------|---------|
| 전체 | `""` | 76,602 |
| 통합공고 | `28801` | 9,549 |
| 개별공고 | `28802` | 45,005 |

> **통합공고**: IRIS 원본 공고를 NTIS가 집계한 것. 상세에 "IRIS 바로가기 ▶" 링크 포함.  
> **개별공고**: IRIS와 무관한 독립 공고.

---

## 4. 상세 페이지 구조

### 4-1. URL 패턴

```
GET https://www.ntis.go.kr/rndgate/eg/un/ra/view.do?roRndUid={roRndUid}&flag=rndList
```

로그인 없이 게스트 접근 가능. 리다이렉트 없음.

### 4-2. 구조화 메타 필드 (div.summary1/summary2 기반 span 패턴)

```html
<div class="summary1">
  <ul>
    <li><span>공고형태 : </span>통합공고</li>
    <li><span>부처명 : </span>과학기술정보통신부</li>
    <li style="width:400px"><span>공고기관명 : </span>한국연구재단</li>
  </ul>
</div>
<div class="summary2">
  <ul>
    <li><span>공고일 : </span>2026.04.17</li>
    <li><span>접수일 : </span>2026.04.17</li>
    <li><span>마감일 :</span>2026.05.19</li>
    <li><span>접수마감시간 :</span>18:00</li>
  </ul>
</div>
<div class="summary1">
  <ul>
    <li><span>공고유형 : </span>본공고</li>
    <li><span>공고금액 : </span>15<span class="unit">억원</span></li>
    <li><span>문의처 : </span>042-869-7803</li>
    <li><span>사업명 : </span>국가간협력기반조성</li>
  </ul>
</div>
```

파싱 전략: `span.get_text(strip=True)` + `:` split + `next_sibling` 텍스트.

### 4-3. 공고 본문 텍스트 (공고번호 포함)

공고 본문: `div.se-contents` 내에 SSR 렌더링.

공고번호 파싱 결과 샘플:

| roRndUid | 공고형태 | 공고번호 원문 | 정규화 후 |
|----------|---------|------------|---------|
| 1262378 | 통합 | `과학기술정보통신부 공고 제2026-0455호` | `과학기술정보통신부공고제2026-0455호` |
| 1262576 | 통합 | `과학기술정보통신부 공고 제2026\xa0–\xa00484호` | `과학기술정보통신부공고제2026-0484호` |
| 1262381 | 개별 | `산업통상부 공고 제2026-300호` | `산업통상부공고제2026-300호` |
| 1262577 | 개별 | `과학기술정보통신부 공고 제2026\xa0–\xa00485호` | `과학기술정보통신부공고제2026-0485호` |

**정규식**:
```python
import re
import unicodedata

ANCM_NO_PATTERN = re.compile(
    r'[가-힣]+\s*공고\s*제\s*[\d\s\-–—–—\xa0]+\s*호'
)

def extract_ancm_no(text: str) -> str | None:
    """div.se-contents 텍스트에서 공고번호를 추출한다."""
    m = ANCM_NO_PATTERN.search(text)
    if not m:
        return None
    raw = m.group(0)
    # NFKC 정규화 후 모든 공백·특수 대시를 제거
    normalized = unicodedata.normalize('NFKC', raw)
    normalized = re.sub(r'[\s –—\-]+', '-', normalized)
    normalized = re.sub(r'\s+', '', normalized)
    # 앞뒤 대시 제거
    normalized = normalized.strip('-')
    return normalized
```

> **주의**: `\xa0` (non-breaking space), `–` (U+2013 en-dash), `—` (U+2014 em-dash) 혼용 사례 확인.  
> IRIS `ancmNo` 필드와 달리 구조화 필드 없음 — 항상 본문 텍스트 파싱이 필요.  
> 공고번호를 포함하지 않는 공고 존재 가능 (fuzzy fallback 필수).

---

## 5. 로그인 / 인증

**결론: 로그인 불필요. 게스트 수집 가능.**

- 목록 GET/POST: 쿠키/세션 없이 200 응답
- 상세 GET: 쿠키/세션 없이 200 응답, 리다이렉트 없음
- 첨부 다운로드 POST: 쿠키/세션 없이 200 + `application/octet-stream` 응답 확인

> `sources.yaml` credentials 슬롯 불필요 (subtask 5는 no-op).

---

## 6. 첨부파일 다운로드 패턴

### 6-1. 링크 HTML

```html
<a href="/rndgate/eg/cmm/file/download.do"
   onclick="javascript:fn_fileDownload('1369403', '20260417151534778OL3HN6F8T2'); return false;">
  붙임1. 공고문.pdf
</a>
```

### 6-2. JavaScript 함수 (view.do)

```javascript
function fn_fileDownload(attachSn, roTextUid) {
    $('input[name="wfUid"]').val(attachSn);
    $('input[name="roTextUid"]').val(roTextUid);
    $.fileDownload($("#download").prop('action'), {
        httpMethod: "POST",
        data: $("#download").serialize()
    });
}
```

다운로드 폼:
```html
<form id="download" method="post" target="_top" action="/rndgate/eg/cmm/file/download.do">
    <input type="hidden" name="wfUid" value="" />
    <input type="hidden" name="roTextUid" value="" />
</form>
```

### 6-3. HTTP 직접 재현 (실측 성공)

```bash
curl -X POST \
  -H 'Referer: https://www.ntis.go.kr/rndgate/eg/un/ra/view.do?roRndUid=1262378' \
  -d 'wfUid=1369403&roTextUid=20260417151534778OL3HN6F8T2' \
  'https://www.ntis.go.kr/rndgate/eg/cmm/file/download.do'
# → status 200, Content-Type: application/octet-stream;charset=UTF-8
# → Content-Disposition: attachment; filename="%EB%B6%99%EC%9E%841. ...pdf"
# → 390,801 bytes (PDF 바이너리)
```

**결론: Playwright 없이 httpx POST 한 번으로 다운로드 가능.**

IRIS와의 차이:

| 항목 | IRIS | NTIS |
|------|------|------|
| onclick | `javascript:f_bsnsAncm_downloadAtchFile(ancmId, fileSeq, ...)` | `javascript:fn_fileDownload(attachSn, roTextUid)` |
| 다운로드 방식 | Playwright 클릭 필요 (직접 HTTP 우회 불가) | httpx POST 직접 가능 |
| POST 파라미터 | 알 수 없는 내부 파라미터 (JS 처리) | `wfUid` + `roTextUid` |
| 엔드포인트 | `/contents/downloadBsnsAncmAtchFile.do` | `/rndgate/eg/cmm/file/download.do` |

파싱 정규식 (onclick에서 파라미터 추출):
```python
DOWNLOAD_ONCLICK_PATTERN = re.compile(
    r"fn_fileDownload\('(\d+)',\s*'([^']+)'\)"
)
```

---

## 7. 공식 과제 식별자

| 항목 | 값 | 설명 |
|------|---|------|
| `roRndUid` | `1262378` | NTIS 내부 PK. checkbox value 및 URL 파라미터. → `source_announcement_id` |
| 순번 | `76599` | 누적 sequential 순번. 내부 순서 파악용이나 PK로 부적합. |
| 공식 공고번호 | 상세 본문 텍스트 파싱 필요 | IRIS `ancmNo`와 동일 번호 공유 확인 (cross-source 매칭 기반) |
| 과제관리번호 | 없음 | 사업공고 단계에서 미부여 (IRIS와 동일) |

---

## 8. 수집 전략 결론

| 항목 | 결론 |
|------|------|
| 렌더링 방식 | **SSR** (JSON API 없음) |
| 수집 도구 | **httpx 전용** (Playwright 불필요) |
| 로그인 | **불필요** (게스트 수집 가능) |
| 첨부 다운로드 | **httpx POST** (`/rndgate/eg/cmm/file/download.do`, wfUid + roTextUid) |
| 상태 필터 | `searchStatusList`: `P`=접수예정 / `B`=접수중 / `Y`=마감 |
| 페이지네이션 | `POST mng.do` + `pageIndex` |
| 공고 식별자 | `roRndUid` = `source_announcement_id` |
| 공고번호 추출 | `div.se-contents` 본문 텍스트 정규식 파싱 (`\xa0`, en-dash, em-dash 처리 필수) |
| credentials | **불필요** (subtask 5 no-op) |

---

## 9. canonical_identity_design.md §3 대비 신규 발견 사항

§3에서 기술된 내용은 대부분 정확. 이번 실측에서 추가로 확인된 사항:

1. **`\xa0` (non-breaking space) 및 en-dash(`–`, U+2013) 혼용**: 공고번호에 일반 공백 대신 `\xa0`와 en-dash가 혼재하는 케이스 실측 (`roRndUid=1262576`). 정규화 시 `unicodedata.normalize('NFKC', ...)` + 대시 통일 처리 필수.

2. **공고번호 미포함 케이스 가능**: `roRndUid=1262576`의 경우 `div.se-contents` 파싱에서 공고번호 추출 성공했으나, 일부 공고는 본문 텍스트 형식이 달라 파싱 실패할 수 있음 → fuzzy fallback 중요.

3. **첨부 직접 POST 가능**: §3에서 "Playwright 필요 여부 미확정"이었으나, 실측으로 **httpx POST 직접 다운로드 가능**임이 확인됨. AttachmentDownloader adapter 레벨에서 NTIS 전용 httpx 경로 추가 필요.

4. **상태 코드 확인**: P/B/Y 코드 실측. `canonical_identity_design.md`에 언급 없었음.

---

## 10. 이후 subtask 설계 근거

| subtask | 탐사 근거 |
|---------|---------|
| 00014-2 (상태 매핑) | NTIS 원문 `접수예정`/`접수중`/`마감` → AnnouncementStatus 직접 1:1 매핑 가능. 별도 변환 코드 단순. |
| 00014-3 (목록 스크래퍼) | httpx POST `mng.do` + `pageIndex`/`searchStatusList` 파라미터. BeautifulSoup HTML 파싱. |
| 00014-4 (상세 스크래퍼) | httpx GET `view.do?roRndUid=…`. `div.se-contents` 본문, summary div 구조화 필드. 공고번호 정규식 파싱. |
| 00014-5 (credentials) | **no-op** — 로그인 불필요. |
| 00014-7 (첨부 다운로더) | NTIS: httpx POST `wfUid+roTextUid` 직접 → adapter 레벨에서 분기. downloader 자체는 소스 무관 유지. |
| 00014-8 (cross-source 매칭) | NTIS 공고번호 파싱 후 normalize → IRIS ancmNo normalize와 동일 키 → `official:…` canonical_key 매칭. `\xa0`/dash 처리가 핵심 변수. |
