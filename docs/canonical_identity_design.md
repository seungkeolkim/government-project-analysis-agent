# Canonical Identity 설계 (00013)

> 최종 작성: 2026-04-21  
> 상태: 초안 (00013-1 산출물 — NTIS 섹션은 00013-2 에서 추가)

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
> canonical_key = `IRIS:{ancmNo_normalized}` 형태로 사용하면 *그룹* 단위 중복 처리가 가능하다.  
> `ancmId` 수준의 1:1 매칭에는 기존 `source_announcement_id`를 그대로 사용한다.

---

## 3. canonical_key 설계 방향

### 3-1. 공식 키 (official scheme)

`ancmNo`가 있으면 정규화 후 source prefix를 붙여 canonical_key로 사용한다.

```
canonical_key = f"{source_prefix}:{normalized_ancm_no}"
예) "IRIS:산업통상부공고제2026-298호"
```

**`ancmNo` 정규화 규칙 (후보)**
1. 모든 공백 제거
2. 전각 문자 → 반각 변환
3. 구분자 통일: ` - `, `－`, `–` → `-`
4. 소문자 통일 (한글은 해당 없음)
5. 접두어 패턴 제거 불필요 (부처명이 canonical 판별에 유효 정보이므로 유지)

**source_prefix 정책 (초안)**
- IRIS: `IRIS`
- NTIS: `NTIS` (00013-2 탐사 후 cross-source 일치 여부 결정)
- cross-source 공통 공고번호가 확인되면 부처명 기반 prefix로 통일 가능 (`MOE:`, `MSIT:` 등)

### 3-2. fuzzy 키 (fuzzy scheme, fallback)

`ancmNo`가 없거나 신뢰할 수 없는 경우의 fallback.

```
canonical_key = f"fuzzy:{normalized_title}:{normalized_agency}:{deadline_year}"
```

**정규화 규칙**
- `normalized_title`: 공백·특수문자 제거, 연도·연번 등 가변 부분 제거 후 앞 50자
- `normalized_agency`: 주관기관명 공백 제거, 법인격 접미사(`(재)`, `주식회사` 등) 제거
- `deadline_year`: 접수마감일 연도 4자리 (없으면 `0000`)

### 3-3. scheme 판별 우선순위

```
if ancmNo is not None and ancmNo.strip():
    scheme = "official"
    canonical_key = f"{source_prefix}:{normalize_ancm_no(ancmNo)}"
else:
    scheme = "fuzzy"
    canonical_key = f"fuzzy:{normalize_title(title)}:{normalize_agency(agency)}:{deadline_year}"
```

`canonical_key` + `scheme` 두 필드를 함께 저장해 나중에 fuzzy 매칭 품질을 평가할 수 있게 한다.

---

## 4. Schema 옵션 비교

| | (A) announcements 컬럼 추가 | (B) canonical_projects 별도 테이블 |
|---|---|---|
| **구조** | `announcements.canonical_key`, `canonical_group_id` 컬럼 추가 | `canonical_projects(id, canonical_key, ...)` 테이블 + `announcements.canonical_project_id FK` |
| **장점** | JOIN 없이 단순 쿼리, 마이그레이션 단순 | 그룹 레벨 메타(대표 제목, first_seen_at) 별도 관리 가능. UI에서 그룹 단위 표시 용이 |
| **단점** | canonical 그룹 메타(대표 제목 등)를 별도로 집계해야 함. `canonical_group_id`를 자체 관리(UUID 등) | JOIN 필요. 마이그레이션 복잡도 소폭 증가 |
| **N:1 표현** | `canonical_group_id`로 자체 그룹핑 | FK로 자연스럽게 표현 |
| **cross-source 확장** | canonical_key 컬럼 비교로 가능 | canonical_projects에 source 무관한 그룹 row 1개 → 확장 자연스러움 |

---

## 5. 권고 결정 (초안)

### Schema: (B) canonical_projects 테이블

**선택 근거**
- IRIS 탐사에서 `ancmNo`가 N:1(여러 ancmId → 하나의 ancmNo)임이 확인됨 → 그룹 엔티티를 1st-class로 두는 것이 자연스러움
- NTIS 등 추가 소스 대응 시, `canonical_projects` row 1개가 여러 소스의 공고를 아우르는 구조로 자연스럽게 확장됨
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

## 6. 미결 사항 (00013-2 이후 결정)

- NTIS 공고 식별자 필드 포맷 (공고번호 표기 방식이 IRIS와 일치하는가?)
- cross-source canonical_key prefix 정책 확정 (소스별 prefix vs 부처 코드 기반)
- `ancmNo` 정규화 후 같은 ancmNo를 가진 IRIS ancmId들을 하나의 canonical group으로 묶을지, 각각 별개 group으로 둘지 (현재 권고: 같은 ancmNo → 같은 group, ancmId별 row는 announcements에 유지)
- 재공고(재공고 여부=Y) 처리: 동일 ancmNo 재사용 여부 확인 후 결정
