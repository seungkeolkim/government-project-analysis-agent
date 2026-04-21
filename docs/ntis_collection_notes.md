# NTIS 수집 특이사항 노트

NTIS(국가R&D정보서비스) 수집 구현(task 00014)에서 드러난 특이사항을 정리한다.
전반적인 사이트 구조 탐사 결과는 `docs/ntis_site_exploration.md` 를 참조한다.

---

## 1. 수집 방식: httpx 단독 (Playwright 불필요)

NTIS 목록·상세 페이지는 SSR HTML(POST 응답)이므로 httpx + BeautifulSoup 만으로 수집된다.
IRIS가 Playwright primary + httpx fallback 구조인 것과 달리, NTIS는 Playwright를 전혀 사용하지 않는다.

| 단계 | 방법 |
|---|---|
| 목록 수집 | httpx POST `/rndgate/eg/un/ra/mng.do` |
| 상세 수집 | httpx POST 동일 엔드포인트 (roRndUid + flag=rndView) |
| 첨부 다운로드 | httpx POST `/rndgate/eg/cmm/file/download.do` (wfUid + roTextUid) |

---

## 2. 상태 코드 매핑

NTIS HTML 상에는 상태 코드(P/B/Y)가 파라미터로 사용되고, 목록 TD에는 한글 라벨이 표시된다.

| NTIS 원문 라벨 | AnnouncementStatus | 목록 수집 파라미터 |
|---|---|---|
| 접수예정 | `SCHEDULED` | `ancmPrgCd=P` |
| 접수중 | `RECEIVING` | `ancmPrgCd=B` |
| 마감 | `CLOSED` | `ancmPrgCd=Y` |

- 정규화 함수: `app/scraper/ntis/status_normalizer.normalize_ntis_status()`
- NFKC 정규화 적용 — `\xa0`(nbsp) 혼입 방어
- 알 수 없는 라벨: `ValueError` → 해당 행 스킵

---

## 3. 공고 볼륨 특이사항

탐사 시점(2026-04-21) 기준:

| 상태 | 공고 수 |
|---|---|
| 접수예정 | 약 300건 |
| 접수중 | 약 400건 |
| 마감 | **74,809건** |

마감 공고가 매우 많으므로 `sources.yaml` 기본값을 `max_pages: 5` / `max_announcements: 100` 으로 설정했다.
마감 공고 전수 수집이 필요한 경우 `--max-pages`, `--max-announcements` 로 직접 지정한다.

---

## 4. 첨부파일 다운로드 패턴

NTIS 상세 페이지의 첨부 링크는 `<a onclick="fn_download('WF_UID', 'RO_TEXT_UID')">` 형태다.
다운로드는 httpx POST로 처리되며, Playwright가 필요 없다.

```
POST /rndgate/eg/cmm/file/download.do
Content-Type: application/x-www-form-urlencoded

wfUid=<WF_UID>&roTextUid=<RO_TEXT_UID>
```

- `wfUid` / `roTextUid` 는 `AttachmentLinkInfo.atc_doc_id` / `atc_file_id` 로 저장된다.
- `AttachmentLinkInfo.download_method = "POST"`, `post_data = {"wfUid": ..., "roTextUid": ...}`
- Referer 헤더: `NtisSourceAdapter.build_download_referer()` → `https://www.ntis.go.kr/rndgate/eg/un/ra/view.do?roRndUid={id}`

IRIS 첨부는 `atc_doc_id` + `atc_file_id` 쿼리 파라미터 GET 방식이므로 두 소스의 첨부 URL 구조가 다르다.
`AttachmentDownloader` 는 소스를 모르며, `download_method` 필드 분기로만 처리한다.

---

## 5. 공식 공고번호(ancmNo) 파싱 및 canonical 승급

NTIS 목록 단계에서는 공식 공고번호를 알 수 없어 **fuzzy canonical key** 가 먼저 부여된다.
상세 수집 후 `div.new_ntis_contents` 내 "공고번호" 텍스트를 파싱해 ancmNo 를 획득한다.

### 정규화 파이프라인 (`_extract_ancm_no`)

```
원문 예시: "과학기술정보통신부 공고 제\xa0–\xa02026-0455 호"
  1. NFKC 정규화
  2. en/em-dash(–, —) → ASCII 하이픈(-) 치환
  3. 모든 공백 제거 (re.sub(r'\s+', '', ...))
결과: "과학기술정보통신부공고제2026-0455호"
```

이 결과는 `canonical.py._normalize_official_key()` 의 NFKC+공백제거와 동일하게 수렴한다.
IRIS `ancmNo` 와 같은 공고이면 같은 `official:...` canonical key 가 생성된다.

### canonical 승급 경로

```
목록 수집 → fuzzy canonical 부여 (ancm_no=None)
  ↓
상세 수집 성공 + ntis_ancm_no 확보
  ↓
recompute_canonical_with_ancm_no() 호출
  ↓
official canonical key 로 승급 → CanonicalProject upsert/merge
  ↓
IRIS 동일 공고와 canonical_group_id 공유
```

### 알려진 예외 케이스

- **개별공고**: 공고번호 텍스트 패턴이 통합공고와 다를 수 있어 `ntis_ancm_no = None` 으로 fallback.
  이 경우 fuzzy canonical key 로 남으며, IRIS cross-source 매칭은 제목+기관+연도 기반이 된다.
- **ancmNo 없는 행**: `detail_html` 내에 공고번호 텍스트가 없으면 `ntis_ancm_no = None`. 마찬가지로 fuzzy 유지.

---

## 6. 수집 로그 확인 포인트

정상 수집 시 소스별 로그 패턴:

```
INFO  목록 수집 시작: source=NTIS status=접수중 page=1/N
INFO  목록 수집 완료: source=NTIS 42건
INFO  [1/42] 공고 처리: source=NTIS id=1262378
INFO  신규 공고 등록: source=NTIS id=1262378
INFO  상세 수집 완료(ok): source=NTIS id=1262378
INFO  canonical 재계산 완료(fuzzy→official): source=NTIS id=1262378 ancm_no=과학기술정보통신부공고제2026-0455호
INFO  첨부 수집 완료: source=NTIS id=1262378 성공=5 실패=0 생략(이미 존재)=0
```

canonical 재계산 로그가 없으면 상세 파싱에서 ancmNo 를 찾지 못한 것이다 (개별공고 등).

---

## 7. 트러블슈팅

### NTIS 목록 0건 수집

1. `--log-level DEBUG` 로 실행해 httpx 요청/응답 상태 확인
2. `totalCount` hidden input 파싱 실패 시 `list_scraper._extract_total_count()` 점검
3. NTIS 사이트 점검 여부 확인 (브라우저에서 `https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do` 직접 접근)

### NTIS 첨부 다운로드 실패

```sql
SELECT id, source_announcement_id,
       json_extract(raw_metadata, '$.attachment_errors')
FROM announcements
WHERE source_type = 'NTIS' AND is_current = 1
  AND raw_metadata LIKE '%attachment_errors%';
```

`download_method=POST` 경로 실패인지 확인하려면 `--log-level DEBUG` 로 재실행한다.
NTIS 첨부는 Playwright 미사용 — Playwright 관련 오류는 NTIS 첨부 원인이 아니다.

### canonical 승급이 안 됨

- 상세 수집 성공(`detail_fetch_status='ok'`) 후에도 canonical 재계산 로그가 없다면:
  - 해당 공고가 ancmNo 없는 개별공고일 가능성
  - `--log-level DEBUG` 로 `ntis_ancm_no` 값을 확인
  - `detail_html` 에서 "공고번호" 섹션이 존재하는지 확인
