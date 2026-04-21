# Canonical Identity 설계 (00013)

> 최종 작성: 2026-04-21  
> 상태: **최종본** (00013 전 subtask 완료 — 00013-1: IRIS 탐사, 00013-2: NTIS 탐사, 00013-3~6: 구현 완료)

---

## 1. 목표

IRIS·NTIS 같은 복수 포털이 동일 과제를 중복 게시하거나, 같은 포털 내에서 공고가 재등록되는 경우를 처리할 **canonical identity** 레이어를 도입한다.

- **같은 공고 = 같은 그룹**으로 묶어 중복 없이 저장
- 기존 `(source_type, source_announcement_id)` UPSERT·`is_current` 이력 구조를 그대로 유지
- cross-source 실매칭은 NTIS 구현(00014~) 이후 실데이터를 보고 확정

---

## 2. IRIS 공식 식별자 탐사 결과

탐사 일시: 2026-04-21  
탐사 방법: IRIS 목록 AJAX API(`/contents/retrieveBsnsAncmBtinSituList.do`) JSON 전체 필드 조사 + 상세 페이지(`div.tstyle_view`) HTML 라벨 점검

### 2-1. 목록 API JSON 전체 필드

```
ancmId, sorgnId, rcveStrDe, rcveEndDe, dDay, sorgnNm, ancmTl,
ancmNo, blngGovdSeNm, blngGovdSe, budJuriGovdSe, ancmDe,
pbofrTpSeNmLst, rcveSttSeNmLst, pbofrTpSeLst, rcveStt
```

### 2-2. 공식 식별자 후보 분석

| 필드 | 예시 | 판정 |
|------|------|------|
| `ancmId` | `'021054'` | IRIS 내부 ID. 이미 `source_announcement_id` 로 사용 중. source 내 고유 |
| `ancmNo` | `'산업통상부 공고 제2026-298호'` | **외부 공유 가능한 공식 공고번호**. 목록 API에 항상 존재 |
| `sorgnId` | `'10005'` | 전문기관 내부 코드. 외부 공유 의미 없음 |
| `과제관리번호` | — | **사업공고 단계에서 존재하지 않음** (과제 선정 후 부여) |

### 2-3. `ancmNo` 특성 및 한계

**장점**
- 목록 API 응답 전수(접수예정/접수중/마감 모든 상태)에서 비어있는 레코드 0건 확인 → 사실상 필수 필드
- 상세 페이지 `div.tstyle_view > li.write > strong` 라벨 '공고번호' 값과 일치 → 동일 값이 두 경로 모두 노출
- 정부 부처가 공식 발행하는 번호이므로 NTIS 등 타 포털에도 동일 번호가 게시될 가능성 높음 (cross-source 매칭 기반)

**주의사항**
1. **N:1 구조**: 하나의 `ancmNo`가 여러 `ancmId`에 매핑될 수 있음  
   - 예: `'과학기술정보통신부 공고 제 2026-0458호'` → `ancmId` 020527, 020526, 020525 세 건  
   - 즉, 공고번호는 "과제 묶음" 단위. `ancmId` 하나가 그 안의 세부과제 하나에 대응.
2. **포맷 불일치**: 공백·특수문자 표기가 레코드마다 다름  

   | ancmId | ancmNo 원문 |
   |--------|------------|
   | 020527 | `과학기술정보통신부 공고 제 2026-0458호` |
   | 020517 | `과학기술정보통신부 공고 제 2026 - 0411호` |
   | 021054 | `산업통상부 공고 제2026-298호` |

   → canonical_key 생성 전 정규화(공백 제거, 전각→반각 등)가 필수.

3. **재공고**: `div.tstyle_view` 내 `재공고 여부` 라벨(N/Y)이 존재. 재공고 시 동일 ancmNo 재사용 여부는 추가 관찰 필요.

### 2-4. 결론

> **IRIS의 외부 공식 식별자는 `ancmNo` (공고번호)이다.**  
> 단, 1:1 고유 키가 아니라 "공고 묶음" 단위이므로,  
> canonical_key = `official:{ancmNo_normalized}` 형태로 사용하면 *그룹* 단위 중복 처리가 가능하다 (cross-source prefix 정책은 §4-1 참조).  
> `ancmId` 수준의 1:1 매칭에는 기존 `source_announcement_id`를 그대로 사용한다.

---

## 3. NTIS 공식 식별자 탐사 결과

탐사 일시: 2026-04-21  
탐사 방법: NTIS 국가R&D통합공고 목록 HTML 파싱 + 상세 페이지(`/rndgate/eg/un/ra/view.do?roRndUid=...`) 텍스트 추출

### 3-1. NTIS 목록·상세 구조

**목록 페이지**: `https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do`  
응답 방식: **HTML 렌더링** (IRIS와 달리 JSON API 없음)  
목록 표시 필드:

| 필드 | 예시 | 비고 |
|------|------|------|
| 순번 | `76599` | 목록 표시용 순번. sequential. URL 파라미터 아님 |
| 현황 | `접수중` | 접수예정/접수중/마감 |
| 공고명 | `2026년도 한-스페인 공동연구사업 신규과제 공모` | 제목 |
| 부처명 | `과학기술정보통신부` | 주관부처 |
| 접수일 | `2026.04.17` | |
| 마감일 | `2026.05.19` | |
| checkbox `value` | `1262378` | **= roRndUid** (상세 페이지 URL 파라미터) |

**상세 페이지**: `/rndgate/eg/un/ra/view.do?roRndUid={roRndUid}`

상세 페이지에 표시되는 구조화 필드:

| 라벨 | 예시 | 비고 |
|------|------|------|
| 공고형태 | `통합공고` | 통합공고 vs 개별공고 |
| 부처명 | `과학기술정보통신부` | |
| 공고기관명 | `한국연구재단` | |
| 공고일 | `2026.04.17` | |
| 접수일 | `2026.04.17` | |
| 마감일 | `2026.05.19` | |
| 공고유형 | `본공고` | 본공고/재공고 |
| 공고금액 | `15 억원` | |
| 사업명 | `국가간협력기반조성` | |

**공식 공고번호 위치**: 구조화 필드 없음. 상세 페이지 공고 본문 텍스트에 포함.  
예) `'과학기술정보통신부 공고 제 2026-0455 호'` (공백·줄바꿈 포함)

### 3-2. NTIS 공식 식별자 후보 분석

| 필드 | 예시 | 판정 |
|------|------|------|
| `roRndUid` | `1262378` | NTIS 내부 primary key. `source_announcement_id`로 사용 예정 |
| `순번` | `76599` | 누적 sequential ID. source_announcement_id 후보이나 roRndUid가 더 명확한 PK |
| 공식 공고번호 | `'과학기술정보통신부 공고 제 2026-0455 호'` | **상세 HTML 본문 텍스트에 포함.** 구조화 필드 없음 |
| `과제관리번호` | — | 사업공고 단계에서 미부여. IRIS와 동일하게 없음 |

### 3-3. Cross-source 교차 검증 (샘플 1건)

동일 공고: **2026년도 한-스페인 공동연구사업 신규과제 공모**

| 항목 | IRIS | NTIS |
|------|------|------|
| 내부 ID | `ancmId=020640` | `roRndUid=1262378` (순번 76599) |
| 공식 공고번호 | `ancmNo='과학기술정보통신부 공고 제2026-0455호'` (구조화 필드) | 상세 본문 텍스트 `'과학기술정보통신부 공고 제 2026-0455 호'` |
| 주관기관 | `한국연구재단` | `한국연구재단` |
| 접수기간 | `2026.04.17 ~ 2026.05.19` | `2026.04.17 ~ 2026.05.19` |
| IRIS 연동 | — | 상세 페이지에 **"IRIS 바로가기 ▶"** 링크 + "자세한 내용은 IRIS 사업공고에서 확인" 안내 |

**결론**: 공고번호 값이 완전히 일치 (공백·포맷 차이 제외). 정규화(공백 제거) 시 cross-source 매칭 가능.

### 3-4. NTIS 공고 수집 방식의 함의

NTIS 통합공고는 IRIS를 원본으로 참조하므로 다음과 같은 계층이 성립한다:

```
IRIS (원본) ←── NTIS 통합공고 ("IRIS 바로가기 ▶")
```

- **NTIS 통합공고**: IRIS ancmNo와 매핑 가능. ancmNo 기반 canonical_key로 cross-source 그룹핑.
- **NTIS 개별공고**: IRIS와 무관한 독립 공고. ancmNo 파싱이 어려우면 fuzzy key 사용.
- NTIS에서 공고번호를 구조화 필드로 추출하려면 상세 HTML 본문 텍스트 파싱 필요 (정규식: `[부처명]\s*공고\s*제\s*[\d\-]+\s*호`).

### 3-5. 결론

> **NTIS의 공식 공고번호는 별도 구조화 필드 없이 상세 HTML 본문 텍스트에 포함된다.**  
> 목록 API JSON도 없어 HTML 파싱이 필수다.  
> IRIS와 동일한 공고번호를 공유하므로 정규화 후 cross-source 매칭이 가능하나,  
> **1차 canonical_key는 IRIS ancmNo 기반(구조화 필드)을 신뢰 기준으로 삼고,**  
> NTIS는 `roRndUid`(= source_announcement_id)를 이미 알려진 canonical group에 연결하는 방식이 안전하다.

---

## 4. canonical_key 설계 방향

### 4-1. 공식 키 (official scheme)

공고번호(`ancmNo`)가 있으면 정규화 후 `official:` prefix를 붙여 canonical_key로 사용한다.

```
canonical_key = f"official:{normalized_ancm_no}"
예) "official:과학기술정보통신부공고제2026-0455호"
```

**source_prefix 정책 결정 (NTIS 탐사 반영)**

IRIS와 NTIS가 동일한 공고번호를 공유함이 확인되었다 (§3-3). 따라서:
- 공고번호에 이미 부처명이 포함되어 있어 `IRIS:` / `NTIS:` 소스 prefix는 불필요.
- 대신 `official:` prefix로 소스 무관하게 통일한다.
- IRIS에서 `ancmNo` 필드로 직접 추출하거나, NTIS 상세 본문 파싱으로 추출한 번호를 동일 정규화 후 같은 키로 매핑.

**`ancmNo` 정규화 규칙**
1. 모든 공백 제거
2. 전각 문자 → 반각 변환
3. 구분자 통일: ` - `, `－`, `–` → `-`
4. 소문자 통일 (한글은 해당 없음)
5. 접두어 패턴 제거 불필요 (부처명이 canonical 판별에 유효 정보이므로 유지)

**예시 정규화**

| 원문 | 정규화 결과 |
|------|------------|
| `'과학기술정보통신부 공고 제 2026-0455 호'` | `'과학기술정보통신부공고제2026-0455호'` |
| `'과학기술정보통신부 공고 제 2026 - 0411호'` | `'과학기술정보통신부공고제2026-0411호'` |
| `'산업통상부 공고 제2026-298호'` | `'산업통상부공고제2026-298호'` |

### 4-2. fuzzy 키 (fuzzy scheme, fallback)

공고번호가 없거나 파싱 불가인 경우의 fallback.

```
canonical_key = f"fuzzy:{normalized_title}:{normalized_agency}:{deadline_year}"
```

**정규화 규칙**
- `normalized_title`: 공백·특수문자 제거, 연도·연번 등 가변 부분 제거 후 앞 50자
- `normalized_agency`: 주관기관명 공백 제거, 법인격 접미사(`(재)`, `주식회사` 등) 제거
- `deadline_year`: 접수마감일 연도 4자리 (없으면 `0000`)

### 4-3. scheme 판별 우선순위

```python
if ancm_no and ancm_no.strip():
    scheme = "official"
    canonical_key = f"official:{normalize_ancm_no(ancm_no)}"
else:
    scheme = "fuzzy"
    canonical_key = f"fuzzy:{normalize_title(title)}:{normalize_agency(agency)}:{deadline_year}"
```

`canonical_key` + `scheme` 두 필드를 함께 저장해 나중에 fuzzy 매칭 품질을 평가할 수 있게 한다.

---

## 5. Schema 옵션 비교

| | (A) announcements 컬럼 추가 | (B) canonical_projects 별도 테이블 |
|---|---|---|
| **구조** | `announcements.canonical_key`, `canonical_group_id` 컬럼 추가 | `canonical_projects(id, canonical_key, ...)` 테이블 + `announcements.canonical_project_id FK` |
| **장점** | JOIN 없이 단순 쿼리, 마이그레이션 단순 | 그룹 레벨 메타(대표 제목, first_seen_at) 별도 관리 가능. UI에서 그룹 단위 표시 용이 |
| **단점** | canonical 그룹 메타(대표 제목 등)를 별도로 집계해야 함. `canonical_group_id`를 자체 관리(UUID 등) | JOIN 필요. 마이그레이션 복잡도 소폭 증가 |
| **N:1 표현** | `canonical_group_id`로 자체 그룹핑 | FK로 자연스럽게 표현 |
| **cross-source 확장** | canonical_key 컬럼 비교로 가능 | canonical_projects에 source 무관한 그룹 row 1개 → 확장 자연스러움 |

---

## 6. 권고 결정

### Schema: (B) canonical_projects 테이블

**선택 근거 (NTIS 탐사 결과 반영)**
- IRIS 탐사에서 `ancmNo`가 N:1(여러 ancmId → 하나의 ancmNo)임이 확인됨 → 그룹 엔티티를 1st-class로 두는 것이 자연스러움
- NTIS 탐사에서 동일 공고가 양쪽 포털에 존재함이 확인됨 → `canonical_projects` row 1개가 IRIS·NTIS 양쪽 `announcements` row를 아우르는 구조가 자연스러움
- `canonical_key = 'official:과학기술정보통신부공고제2026-0455호'` 하나로 IRIS(020640)·NTIS(1262378) 두 row를 묶는 그룹 엔티티 필요
- 향후 UI에서 canonical 그룹 단위로 검색·표시할 때 GROUP BY 대신 FK JOIN으로 직접 처리 가능
- 복잡도 트레이드오프: 마이그레이션이 (A) 대비 약간 복잡하지만, 기존 `announcements` 스키마 변경은 FK 컬럼 추가 1개로 제한됨

**canonical_projects 테이블 초안**

```sql
CREATE TABLE canonical_projects (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key    TEXT NOT NULL UNIQUE,  -- 정규화된 공식 키 또는 fuzzy 키
    canonical_scheme TEXT NOT NULL,         -- 'official' | 'fuzzy'
    representative_title TEXT,              -- 대표 공고명 (최초 수집 시 저장)
    first_seen_at    DATETIME NOT NULL,
    updated_at       DATETIME NOT NULL
);
```

**announcements 테이블 변경**

```sql
ALTER TABLE announcements
    ADD COLUMN canonical_project_id INTEGER REFERENCES canonical_projects(id);
```

- `canonical_project_id` NULL 허용: 아직 canonical 매칭이 안 된 레코드 허용
- 기존 `(source_type, source_announcement_id)` UPSERT / `is_current` 이력 구조 변경 없음

### 흐름 요약

```
새 공고 수집
  └─ canonical_key 계산 (official 우선, fuzzy fallback)
       └─ canonical_projects 에 canonical_key 존재?
            ├─ YES → 기존 row id를 canonical_project_id 로 사용
            └─ NO  → canonical_projects INSERT → 새 id 획득
  └─ announcements UPSERT (기존 로직 그대로)
       + canonical_project_id 설정
```

---

### 최종 선택 — 실제 구현 컬럼명 (00013-4 확정)

**`canonical_projects` 테이블** (`app/db/models.py: CanonicalProject`)

| 컬럼 | 타입 | 제약 | 설명 |
|------|------|------|------|
| `id` | INTEGER | PK AUTOINCREMENT | 내부 PK |
| `canonical_key` | VARCHAR(256) | NOT NULL UNIQUE | 정규화된 canonical key |
| `key_scheme` | VARCHAR(16) | NOT NULL | `'official'` 또는 `'fuzzy'` |
| `representative_title` | TEXT | NULL 허용 | 최초 수집 시 저장된 대표 공고명 |
| `representative_agency` | VARCHAR(255) | NULL 허용 | 최초 수집된 주관기관명 |
| `created_at` | DATETIME | NOT NULL | 그룹 최초 생성 시각(UTC) |
| `updated_at` | DATETIME | NOT NULL | 최종 갱신 시각(UTC) |

**`announcements` 테이블 추가 컬럼** (`app/db/models.py: Announcement`)

| 컬럼 | 타입 | 제약 | 설명 |
|------|------|------|------|
| `canonical_group_id` | INTEGER | NULL 허용, FK → `canonical_projects.id` ON DELETE SET NULL | 소속 그룹 PK |
| `canonical_key` | VARCHAR(256) | NULL 허용 | canonical_key 비정규화 복사본 (JOIN-free 조회용) |
| `canonical_key_scheme` | VARCHAR(16) | NULL 허용 | `'official'` 또는 `'fuzzy'` |

- `canonical_group_id` NULL → 아직 canonical 매칭 미완료 (기존 데이터 backfill 전)
- `is_current=False` 이력 row 도 `canonical_group_id` 보유. `new_version` 분기 시 승계 로직은 00013-5에서 구현.
- migration 은 `app/db/migration.py` 단계 5·6으로 멱등 적용 (기존 DB 자동 업그레이드).

---

## 7. 미결 사항

### 00014-1 탐사에서 추가 확인된 사항 (2026-04-21)

- **`\xa0` (non-breaking space) + en-dash(`–`, U+2013) 혼용 실측**: `roRndUid=1262576` 공고번호 원문 = `'과학기술정보통신부 공고 제2026\xa0–\xa00484호'`. 기존 정규화 규칙(공백 제거 + 대시 통일)에 `unicodedata.normalize('NFKC', ...)` 선적용이 필수임을 확인.
- **NTIS 목록: SSR HTML** (JSON API 없음). IRIS와 달리 POST `mng.do` + HTML 파싱 방식.
- **NTIS 첨부 다운로드: httpx POST 직접 가능** — `POST /rndgate/eg/cmm/file/download.do` with `wfUid` + `roTextUid`. Playwright 불필요. IRIS와 상이하므로 adapter 레벨 분기 필요.
- **NTIS 로그인 불필요** — 게스트 수집 가능. credentials 슬롯 추가 불필요.
- **상태 필터 코드**: `searchStatusList` = `P`(접수예정) / `B`(접수중) / `Y`(마감). IRIS `ancmPrg` 코드와 무관.
- 자세한 사항: `docs/ntis_site_exploration.md` 참조.

### 00013 에서 확정된 사항

- ✅ 같은 `ancmNo` → 같은 canonical group. `ancmId`별 row는 `announcements`에 유지. (`official:` scheme)
- ✅ `ancmNo` 정규화: NFKC + 공백 전체 제거 (`app/canonical.py: _normalize_official_key`)
- ✅ fuzzy fallback: 제목 정규화 50자 + 기관 정규화 + 마감연도 조합 (`fuzzy:` scheme)
- ✅ NTIS `source_announcement_id` = `roRndUid` (URL PK로 명확)

### NTIS 구현 이후 결정할 사항

- **재공고(재공고 여부=Y) 처리**: 동일 `ancmNo` 재사용 여부 — 실데이터 관찰 필요
- **NTIS 공고번호 파싱 정규식 정교화**: 상세 HTML 본문에서 추출 (`[부처명]\s*공고\s*제\s*[\d\-]+\s*호`)
- **IRIS 재등록(ancmId 변경) 실데이터 검증**: `scripts/verify_canonical_iris.py` 시나리오 B는 현재 fixture 대체 — NTIS 구현 이후 실데이터로 최종 점검
- **ancmNo 공란 케이스**: 현재 샘플에서 0건이나, 마감 대량 페이지에서 추가 확인 권장

---

## 8. IRIS·NTIS 식별자 비교 요약표

| 항목 | IRIS | NTIS |
|------|------|------|
| 내부 ID | `ancmId` | `roRndUid` |
| 목록 응답 방식 | JSON (POST AJAX) | HTML 렌더링 |
| 공식 공고번호 | `ancmNo` (구조화 필드, 항상 존재) | 상세 본문 텍스트에 포함 (별도 필드 없음) |
| 과제관리번호 | 없음 (선정 후 부여) | 없음 (선정 후 부여) |
| source_announcement_id | `ancmId` | `roRndUid` |
| canonical_key 추출 용이성 | ★★★ 높음 — 필드 직접 사용 | ★☆☆ 낮음 — HTML 파싱 필요 |
| cross-source 연결 | — | 상세에 "IRIS 바로가기" 링크 |

---

## 9. 구현 완료 요약 (00013)

### 구현된 파일

| 파일 | 역할 |
|------|------|
| `app/canonical.py` | `compute_canonical_key()` 유틸 — official 우선, fuzzy fallback |
| `app/db/models.py` | `CanonicalProject` ORM 모델 + `Announcement` canonical 컬럼 3개 |
| `app/db/migration.py` | 단계 5 (`canonical_projects` CREATE) + 단계 6 (컬럼 3개 ADD) |
| `app/db/repository.py` | `_apply_canonical()` 헬퍼 + `upsert_announcement()` 4-branch 통합 |
| `app/scraper/iris/list_scraper.py` | `ancmNo` → `ancm_no` 추출 추가 |
| `scripts/verify_canonical_iris.py` | IRIS 단독 검증 스크립트 (18/18 PASS) |
| `scripts/backfill_canonical.py` | 기존 데이터 일회성 backfill 스크립트 |

### 수집 파이프라인 canonical 적용 규칙

| branch | canonical 처리 |
|--------|--------------|
| (a) created | 신규 CanonicalProject 생성 or 기존 그룹 매칭 |
| (b) unchanged | `canonical_group_id=NULL` 인 경우만 기회적 backfill |
| (c) status_transitioned | `canonical_group_id=NULL` 인 경우만 기회적 backfill |
| (d) new_version | 구 row의 `canonical_group_id` 승계. 없으면 신규 계산 |

### canonical_key 포맷

```
official:{NFKC + 공백제거(ancmNo)}
예) "official:과학기술정보통신부공고제2026-0455호"

fuzzy:{제목정규화50자}:{기관정규화}:{마감연도}
예) "fuzzy:2026년바이오과제공고:생명공학연구원:2026"
```

### IRIS 단독 검증 시나리오 (scripts/verify_canonical_iris.py)

| 시나리오 | 검증 내용 | 결과 |
|----------|-----------|------|
| A | 같은 `ancmNo` 재수집 (unchanged 분기) | PASS |
| B | 같은 `ancmNo` 다른 `ancmId` 재등록 (fixture) | PASS |
| C | `ancmNo` 없는 공고 fuzzy fallback | PASS |
| D | 내용 변경 new_version — `canonical_group_id` 승계 | PASS |

> 시나리오 B의 실데이터 재등록 검증은 NTIS 구현 이후 수행 예정.

### 기존 데이터 backfill (scripts/backfill_canonical.py)

한 번만 실행하면 된다. 멱등 설계 — 이미 `canonical_group_id`가 채워진 row는 건너뛴다.

```bash
# 1) dry-run 으로 대상 건수 먼저 확인
python scripts/backfill_canonical.py --dry-run

# 2) 실제 실행
python scripts/backfill_canonical.py --batch-size 200
```

신규 DB는 첫 수집 시부터 canonical이 자동으로 채워지므로 backfill 불필요.
