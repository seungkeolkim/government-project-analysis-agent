# 중복 공고 판별 기준 분석 및 개선 방안 (00038)

> **작성**: Task 00038 subtask 00038-1
> **작성 일시**: 2026-04-28
> **DB 접근**: 읽기 전용 (`SessionLocal`). 어떤 row 도 변경하지 않음.
> **인용 스크립트**: `scripts/audit_canonical_false_positives.py` + 일회성 `python -c` 분석.
> **본 문서 범위**: 분석 + 권장 방안 도출까지. **코드 변경은 별도 task 에서 진행** (사용자 명시 제약).

---

## 1. 배경 및 문제의식

사용자가 두 사례를 직접 제시했다.

### 1-1. 실제 중복 (정상 묶음)

```
announcements.id : 18, 37
공고번호         : 산업통상부 공고 제2026-300호
공고명(공통)     : 2026년도 제조암묵지기반AI모델개발사업 신규지원 대상과제 공고
```

→ 동일 공고번호 + 동일 공고명. IRIS(021075) 와 NTIS(1262381) 가 **동일 과제**를 양쪽 포털에 게시한
cross-source 케이스. 현행 canonical 그룹핑(`canonical_group_id=17`) 이 의도대로 동작.

### 1-2. 허위 중복 (false-positive)

```
announcements.id : 5, 6
공고번호         : 과학기술정보통신부 공고 제2026 - 0498호
공고명 5         : 5극3특 연구개발특구 딥테크 지원(초기 스케일업)
공고명 6         : 5극3특 연구개발특구 딥테크 지원(기획형 창업)
```

→ 동일 공고번호 + **상이한 공고명·공고내용**. IRIS(021176) 와 IRIS(021175) 는 한 공고번호 아래에 게시된
**서로 다른 세부 사업**(초기 스케일업 / 기획형 창업). 현행 canonical 그룹핑(`canonical_group_id=5`) 은
이 두 건을 잘못 같은 그룹으로 묶고 있다.

### 1-3. 본 분석의 핵심 질문

1. 현행 canonical 로직은 무엇을 기준으로 묶는가? 왜 위 false-positive 가 발생하는가?
2. "동일 공고번호 + 동일 공고명" (방안 A) 은 충분한가?
3. 동일 공고번호 아래 서로 다른 세부 공고가 다수 존재할 때 무엇을 기준으로 분리해야 하는가?
4. 어떤 정규화·필드 조합이 cross-source 매칭을 유지하면서 false-positive 를 차단하는가?

---

## 2. 현행 canonical 그룹핑 로직 요약

상세 설계는 [`docs/canonical_identity_design.md`](canonical_identity_design.md) 참조. 본 문서는 분석에 필요한
부분만 재정리한다.

### 2-1. canonical_key 산출 (`app/canonical.py:46`)

```
공식 공고번호(ancmNo) 가 있으면 → canonical_key = "official:{NFKC + 공백제거(ancmNo)}"
없으면                          → canonical_key = "fuzzy:{title 정규화 50자}:{기관 정규화}:{마감연도}"
```

| scheme | 키 본문 | 입력 |
|--------|---------|------|
| `official` | `과학기술정보통신부공고제2026-0498호` | IRIS `ancmNo` 또는 NTIS 상세 본문에서 추출한 정규화 결과 |
| `fuzzy` | `{제목정규화50자}:{기관정규화}:{마감연도}` | ancmNo 부재 시 fallback |

### 2-2. 그룹핑 흐름 (`app/db/repository.py:159` `_apply_canonical`)

1. canonical_key 계산
2. `canonical_projects` 테이블에 동일 키가 있으면 그 row 의 id 를 `announcements.canonical_group_id` 로 사용
3. 없으면 `canonical_projects` INSERT 후 신규 id 사용

→ **canonical_key 가 같으면 무조건 같은 그룹**. 묶는 단위는 사실상 **공고번호(ancmNo)** 한 필드다.

### 2-3. 설계상 N:1 허용

`docs/canonical_identity_design.md §2-3` 에서 명시: "하나의 `ancmNo` 가 여러 `ancmId` 에 매핑될 수 있다.
즉, 공고번호는 *과제 묶음* 단위. ancmId 하나가 그 안의 세부과제 하나에 대응."

이 설계 의도는 **"한 공고번호 안의 모든 세부 공고는 같은 과제 묶음"** 이라는 가정이었다. 본 task 의 false-positive
사례들은 이 가정이 **항상 성립하지는 않음**을 드러낸다.

---

## 3. DB 현황 — 현 시점 스냅샷 (2026-04-28)

### 3-1. 전체 통계

`docker compose run --rm app python scripts/audit_canonical_false_positives.py --show-id 5 6 18 37` 실행 결과 §1:

| 항목 | 값 |
|------|-----|
| canonical_projects rows | 52 |
| is_current 공고 총계 | 52 |
| canonical 미할당(orphan) | 0 |
| official scheme 공고 | 39 |
| fuzzy scheme 공고 | 13 |
| 단독(1건) 그룹 | 36 |
| 다중(2건+) 그룹 | 7 |
| 최대 그룹 크기 | 4 |

다중 그룹 크기 분포: size=2 6건, size=4 1건.

### 3-2. official scheme 다중 그룹 7건 — 수동 분류

`audit_canonical_false_positives.py` §4 출력 + 그룹 내 페어와이즈 제목 비교 (NFKC 정규화 후 정확일치 여부 +
`difflib.SequenceMatcher` 유사도) 를 한 표에 통합한다.

| group_id | canonical_key | ann ids | 분류 | 그룹 내 제목 정확일치(정규화) | 비고 |
|----------|---------------|---------|------|----------------------------|------|
| 5 | `official:과학기술정보통신부공고제2026-0498호` | 5, 6 | **⚠ false-positive** | 불일치 (sim=0.844) | 사용자 제시 사례. 5극3특 딥테크 sub-task 2건. |
| 15 | `official:과학기술정보통신부공고제2026-0485호` | 16, 35 | 정상 cross-source | 불일치 (sim=0.681) | NTIS title 에 `_(2026)대형가속기정책센터…` suffix 부착 — §5-1 참조 |
| 16 | `official:과학기술정보통신부공고제2026-0484호` | 17, 36 | 정상 cross-source | 일치 (sim=1.0) | IRIS+NTIS 가속기핵심기술개발사업 |
| 17 | `official:산업통상부공고제2026-300호` | **18, 37** | **정상 cross-source** | 일치 (sim=1.0) | 사용자 제시 사례. IRIS+NTIS 동일 제목·마감일. |
| 18 | `official:과학기술정보통신부공고제2026-0455호` | 19, 38 | 정상 cross-source | 일치 (sim=1.0) | 한-스페인 공동연구사업 |
| 20 | `official:과학기술정보통신부공고제2026-0444호` | 21, 47 | 정상 cross-source | 일치 (정확일치는 불일치, leading whitespace 차이) | AI반도체 K-클라우드 |
| 25 | `official:과학기술정보통신부공고제2026-0362호` | 26, 27, 28, 29 | **⚠ false-positive (4건 모두)** | 모두 불일치 (sim 0.37~0.69) | 강원1-1/1-2/4-1/5 — 동일 ancmNo 아래 별개 세부과제 4건 |

**요약**: 다중 그룹 7건 중 **5건은 정상 cross-source**, **2건(group 5, 25) 이 false-positive**.
group 25 는 1 그룹에 4건이 잘못 묶여 있어 실질적으로 4 개 별도 과제로 분리되어야 한다.

### 3-3. fuzzy scheme 다중 그룹

```
0건 (감사 §2 출력: "해당 그룹 없음")
```

fuzzy scheme 으로 묶인 13 건 모두 단독 그룹이다. fuzzy false-positive 는 현재 데이터셋에서 관찰되지 않는다.

### 3-4. 사용자 제시 케이스 검증 (`--show-id 5 6 18 37` 출력 발췌)

```
canonical_group_id=5
  id=5  src=IRIS/021176  status=접수예정 deadline=2026-05-21
        title=5극3특 연구개발특구 딥테크 지원(초기 스케일업)
        agency='연구개발특구진흥재단'  scheme=official
        canonical_key=official:과학기술정보통신부공고제2026-0498호
  id=6  src=IRIS/021175  status=접수예정 deadline=2026-05-21
        title=5극3특 연구개발특구 딥테크 지원(기획형 창업)
        agency='연구개발특구진흥재단'  scheme=official
        canonical_key=official:과학기술정보통신부공고제2026-0498호

canonical_group_id=17
  id=18 src=IRIS/021075   status=접수중   deadline=2026-05-11
        title=2026년도 제조암묵지기반AI모델개발사업 신규지원 대상과제 공고
        agency='한국산업기술기획평가원'  scheme=official
        canonical_key=official:산업통상부공고제2026-300호
  id=37 src=NTIS/1262381  status=접수중   deadline=2026-05-11
        title=2026년도 제조암묵지기반AI모델개발사업 신규지원 대상과제 공고
        agency='산업통상부'  scheme=official
        canonical_key=official:산업통상부공고제2026-300호
```

→ 사용자 제시 사례가 본 분석의 모집단(현 DB 다중 그룹)에 그대로 포함되어 있다.

---

## 4. 공고번호(ancmNo) 정규화 적정성 점검

### 4-1. 동일 공고번호의 표기 변이 (raw `list_row.ancm_no` 추출)

| ann.id | source | 원문 ancm_no | 정규화 결과 |
|--------|--------|--------------|--------------|
| 5 | IRIS/021176 | `'과학기술정보통신부 공고 제2026 - 0498호'` | `과학기술정보통신부공고제2026-0498호` |
| 6 | IRIS/021175 | `'과학기술정보통신부 공고 제2026 - 0498호'` | 동일 |
| 16 | IRIS/021118 | `'과학기술정보통신부 공고 제2026 -0485호'` | `과학기술정보통신부공고제2026-0485호` |
| 17 | IRIS/021117 | `'과학기술정보통신부 공고 제2026-0484호'` | `과학기술정보통신부공고제2026-0484호` |
| 18 | IRIS/021075 | `'산업통상부 공고 제2026-300호'` | `산업통상부공고제2026-300호` |
| 26~29 | IRIS | `'과학기술정보통신부 공고 제2026 - 0362호'` | `과학기술정보통신부공고제2026-0362호` |
| 35~38, 47 | NTIS | (목록에 없음 — 상세 추출 후 승급) | NTIS detail_scraper 가 NFKC + dash 통일 + 공백 제거하여 IRIS 와 동일한 정규화 결과로 매칭 |

**판정**: NFKC + 공백 제거 정규화는 `'2026 - 0498'` / `'2026 -0485'` / `'2026-0484'` 같은 공백 차이를 모두
정확히 동일 키로 통일한다. 또한 `\xa0` + en-dash 케이스(canonical_identity_design.md §10-2 시나리오 H) 도 흡수
검증됨. **공고번호 정규화 자체는 추가 변경 불필요**하다는 결론.

### 4-2. 결론

> 공고번호 정규화 규칙은 현행 그대로 유지한다. 본 task 의 개선 포인트는 **공고번호 단독 그룹핑을 보강**하는
> 추가 필드(공고명) 도입이다.

---

## 5. 공고명(title) 정규화 적정성 점검

방안 A·B 모두 공고명 비교를 핵심 축으로 사용하므로, 정규화 규칙 정의가 분석의 전제 조건이 된다.

### 5-1. NTIS 제목의 사업명 suffix 패턴

`source_type='NTIS'` 22 건 중 2 건이 다음 패턴을 띤다.

| ann.id | 원문 title |
|--------|------------|
| 32 | `'2026년 글로벌 클러스터 RBD_(2026)2026년 글로벌 클러스터 RBD'` |
| 35 | `'2026년도 대형가속기 기술개발·진흥 지원체계 구축 신규과제 재공모 _(2026)2026년도 대형가속기정책센터 신규과제 재공모'` |

패턴 정형: `<원본 공고명> _(YYYY)<사업명/공고명>`. NTIS 가 통합공고를 표시할 때 사업명을 함께 부착하는 표시 규칙으로
추정된다. `_(YYYY)` 이후가 부착부이며 IRIS 측 공고명과 직접 비교하면 정확일치가 깨진다.

**관측된 영향**: group 15 (ann 16+35) 가 IRIS 와 NTIS 모두 동일 ancmNo 임에도 NTIS 측 title 만 길어 정확일치 실패
(`sim=0.681`). 만약 정규화 없이 "공고번호 + 공고명 정확일치"를 그대로 적용하면 이 정상 cross-source 묶음이 깨진다.

### 5-2. 그 외 표기 변이

| ann.id | 변이 |
|--------|------|
| 16 (IRIS) | 말미 trailing space `'…재공모 '` |
| 21 (IRIS) | 시작 leading space `' 2026년도 AI반도체…'` |

→ 이런 차이는 NFKC + 공백 전체 제거 정규화로 흡수 가능.

### 5-3. 권장 title 정규화 규칙

```
1. NFKC 유니코드 정규화
2. NTIS suffix 제거: `\s*_\([0-9]{4}\).*$`  (말미의 ' _(YYYY)…' 부분 절단)
3. 모든 공백 제거 (re.sub(r'\s+', '', ...))
4. (선택) 앞 N 자만 유지 (현행 fuzzy 키와 동일하게 50자) — 정확일치 검사에서는 필요 없음
```

규칙 2 의 정규식 검증:
- ann 32 → `'2026년 글로벌 클러스터 RBD'` (suffix 제거 후) → `'2026년글로벌클러스터RBD'`
- ann 35 → `'2026년도 대형가속기 기술개발·진흥 지원체계 구축 신규과제 재공모 '` → `'2026년도대형가속기기술개발·진흥지원체계구축신규과제재공모'`
- ann 16 (IRIS) → `'2026년도 대형가속기 기술개발·진흥 지원체계 구축 신규과제 재공모 '` → `'2026년도대형가속기기술개발·진흥지원체계구축신규과제재공모'`

→ ann 16(IRIS) 와 ann 35(NTIS) 가 정규화 후 정확일치. 정상 cross-source 묶음 유지 확인.

---

## 6. 방안 비교 — 평가 4 축

각 방안을 `false-positive 위험 / false-negative 위험 / 마이그레이션 비용 / 재공고·승계 영향` 4 축으로 비교한다.

### 6-1. 방안 A — `ancmNo 정규화 일치 AND title 정규화 일치`

```
canonical_key = "official:{normalized_ancmNo}::{normalized_title}"
```

(ancmNo 부재 시 fuzzy fallback 은 현행 그대로)

**현 데이터 시뮬레이션 결과** (모든 group 에 대해 방안 A 적용 시 분리/유지)

| group_id | size | 방안 A 적용 후 | 비고 |
|----------|------|----------------|------|
| 5  | 2 | **2 그룹으로 분리** | 5극3특 딥테크 sub-task 분리 (사용자 의도 일치) |
| 15 | 2 | 1 그룹 유지 | NTIS suffix 정규화로 매칭 |
| 16 | 2 | 1 그룹 유지 | IRIS+NTIS 가속기핵심 |
| 17 | 2 | 1 그룹 유지 | IRIS+NTIS 제조암묵지 (사용자 제시 케이스) |
| 18 | 2 | 1 그룹 유지 | IRIS+NTIS 한-스페인 |
| 20 | 2 | 1 그룹 유지 | leading whitespace 정규화로 매칭 |
| 25 | 4 | **4 그룹으로 분리** | 강원1-1/1-2/4-1/5 모두 분리 |
| 그 외 단독 그룹 36건 | — | 변경 없음 | |

| 평가 축 | 판정 |
|---------|------|
| false-positive 위험 | **낮음** — 두 필드 모두 일치해야 묶이므로 보수적. 현 데이터 false-positive 0건 (5, 25 모두 분리). |
| false-negative 위험 | **낮음** (단, 정규화 정의에 의존) — title 정규화에 NTIS suffix 제거 + NFKC + 공백 제거 포함 시 cross-source 5쌍 모두 매칭. NTIS 외 새 표기 패턴 출현 시 정규화 규칙 추가 필요. |
| 마이그레이션 비용 | **중** — `canonical_key` 정의 변경에 따라 기존 `canonical_projects` 와 `announcements.canonical_key/canonical_group_id` 전체 재계산 필요. 멱등 backfill 스크립트 1회 실행으로 처리 가능 (현행 `scripts/backfill_canonical.py` 와 유사 구조). |
| 재공고·승계 영향 | 재공고(공고유형=Y) 가 동일 ancmNo + 동일 title 로 재게시되면 같은 그룹으로 묶임 (정상). title 이 변경되면 별도 그룹 — 이는 의도적 분리로 본다. `is_current=False` 이력 row 의 `canonical_group_id` 승계는 기존 `_apply_canonical` 4-branch 로직(unchanged/new_version/created/status_transitioned) 을 그대로 사용 가능. |

### 6-2. 방안 B — `ancmNo 정규화 일치 AND SequenceMatcher(title) ≥ θ`

```python
if ancm_no 일치 and SequenceMatcher(None, norm_title(t1), norm_title(t2)).ratio() >= θ:
    같은 그룹
```

현 데이터셋의 페어와이즈 유사도 분포(§3-2 표 + group 25 6쌍 sim 0.37~0.69):

| 케이스 | sim |
|--------|-----|
| 정상 cross-source (group 16, 17, 18, 20) | 1.000 |
| 정상 cross-source group 15 (NTIS suffix 미정규화 시) | 0.681 |
| **false-positive** group 5 (ann 5+6) | 0.844 |
| **false-positive** group 25 ann (26,29) | 0.689 |
| **false-positive** group 25 그 외 5쌍 | 0.37~0.46 |

| 평가 축 | 판정 |
|---------|------|
| false-positive 위험 | **중~높음** — 임계값을 낮게 잡으면 group 5(0.844) 가 묶임. 높게 잡으면 group 15(0.681) 등 NTIS suffix 케이스가 분리됨. **가용 cut-off 영역이 0.69 ↔ 0.84 로 좁고 case 가 적어 안전 보증 어려움**. 데이터 증가 시 회귀 위험. |
| false-negative 위험 | NTIS suffix 등을 정규화 함수에 넣으면 sim 1.0 으로 올라가 임계값에 안전. 즉, 정규화가 **여전히** 필요. 그렇다면 임계값 도입의 추가 가치는 미미. |
| 마이그레이션 비용 | A 와 동일. 추가로 임계값 결정 / 추후 튜닝 운영 부담. |
| 재공고·승계 영향 | 미세한 title 변형(예: "공고" ↔ "재공고") 을 같은 그룹으로 흡수할 수 있다는 이론적 장점. 그러나 false-positive 와 trade-off. |

> `docs/canonical_identity_design.md §10-3` 도 SequenceMatcher 도입을 보류한 동일 결론을 이미 기록.

### 6-3. 방안 C — `ancmNo + 기타 필드 조합` (마감일 / 기관)

#### C-1. ancmNo + 마감일

| group | 마감일 분포 | C-1 적용 결과 | 분류 |
|-------|-------------|---------------|------|
| 5 (false-pos) | `[2026-05-21]` (동일) | 1 그룹 유지 | **false-positive 미해소** ✗ |
| 25 (false-pos) | `[2026-04-23, 2026-04-24]` (혼재) | 부분 분리 (26+29 / 27+28) | 부분 해소 — 27+28 은 여전히 false-pos |
| 16, 17, 18, 20 (정상 cross-source) | 동일 | 1 그룹 유지 | 정상 |

→ **group 5 false-positive 가 해소되지 않으므로 C-1 단독으로는 부적합**.

#### C-2. ancmNo + 기관(agency)

| group | 기관 | C-2 적용 결과 | 분류 |
|-------|------|---------------|------|
| 5 (false-pos) | `['연구개발특구진흥재단']` (동일) | 1 그룹 유지 | **false-positive 미해소** ✗ |
| 25 (false-pos) | `['연구개발특구진흥재단']` (동일) | 1 그룹 유지 | **false-positive 미해소** ✗ |
| 15, 16, 17, 18, 20 (정상 cross-source) | IRIS≠NTIS (예: `한국산업기술기획평가원` vs `산업통상부`) | **분리됨 — cross-source 깨짐** ✗ |

→ **C-2 는 false-positive 도 해소 못 하면서 cross-source 매칭을 깨뜨림. 부적합**.

#### C-3. ancmNo + (마감일 OR 기관) 등 기타 OR 조합

조합을 OR/AND 로 더 묶어도, **공고명을 비교하지 않는 한 group 5 (동일 ancmNo, 동일 마감일, 동일 기관, 다른 공고명)
유형은 절대 분리되지 않는다**. 사용자가 사례로 든 false-positive 의 본질이 "공고명만 다르다" 는 점이므로,
공고명 비교를 회피하는 조합은 이 케이스를 잡지 못한다.

| 평가 축 | 판정 |
|---------|------|
| false-positive 위험 | **높음** — 사용자가 가장 우려한 group 5 유형을 잡지 못함. |
| false-negative 위험 | C-2 는 cross-source 다 깨짐. C-1 은 마감일 정확일치 강제로 일부 정상 묶음 깨질 위험. |
| 마이그레이션 비용 | A 와 비슷하거나 더 큼. |
| 재공고·승계 영향 | 재공고로 마감일 변경되면 같은 과제도 분리됨. |

→ **C 단독은 권장하지 않는다**. 단, 미래 fuzzy scheme 의 false-positive 가 관찰되면 fuzzy 키에 추가 필드를 보조로
검토할 여지는 있다 (현재는 fuzzy 다중 그룹 0 건이라 시급하지 않음).

---

## 7. 동일 공고번호 안에 2개 이상 공고가 있는 경우의 처리

사용자 원문에서 명시적으로 논의를 요청한 사항.

### 7-1. 현황 — 동일 ancmNo 다중 공고 케이스

| group_id | ancmNo | 건수 | 성격 |
|----------|--------|------|------|
| 5 | `과학기술정보통신부공고제2026-0498호` | 2 (ann 5, 6) | IRIS sub-task 2건 (분야별: 초기스케일업/기획형창업) |
| 25 | `과학기술정보통신부공고제2026-0362호` | 4 (ann 26~29) | IRIS sub-task 4건 (지역·유형별: 강원1-1/1-2/4-1/5) |
| 그 외 다중 그룹 | — | 모두 1 ancmNo 당 1 IRIS + 1 NTIS (cross-source) | 정상 |

### 7-2. 정책 옵션

세 가지 운영 정책이 가능하다.

| 정책 | 설명 | 장점 | 단점 |
|------|------|------|------|
| (P1) **분리** | 공고번호가 같아도 공고명이 다르면 별도 그룹. | 사용자 화면에서 각 sub-task 가 개별 카드로 보임. UI/검색·즐겨찾기·분석 모두 sub-task 단위 처리. | 동일 공고번호의 모든 sub-task 를 한 번에 보고 싶을 때 별도 메커니즘 필요 (예: `ancmNo` 검색). |
| (P2) **묶음 유지** | 현행과 동일 — 공고번호만 같으면 한 그룹. | "공고 단위" 로 한 번에 추적 가능. | 사용자 제시 false-positive 경험 그대로 발생. 즐겨찾기·관심 표시도 sub-task 구분 없이 묶임. |
| (P3) **혼합** | canonical_group 은 sub-task 단위로 분리(P1) 하되, 별도 `bundle_id`(공고번호 기반) 컬럼을 두어 sub-task 묶음을 보조 표시. | UI 에서 두 view(sub-task 단위 / 공고번호 단위) 를 모두 제공 가능. | 데이터 모델 1단 추가. 구현 복잡도 증가. 본 task 범위 밖. |

### 7-3. 권장

> **P1 (분리)** 를 1차 권장. 본 task 는 **canonical_group 의 정의를 "동일 과제(공고번호 + 공고명)"** 로 좁힌다.
> P3 의 묶음 view 는 별도 작업으로 가치가 인정될 때 추가한다 — 본 task 의 acceptance 와 사용자 원문 의도(공고명까지
> 일치해야 같은 과제) 모두 충족.

---

## 8. 권장 방안

### 8-1. 결론

> **방안 A — `공고번호(정규화) + 공고명(정규화) 정확일치` 채택을 권장한다.**
>
> - 현 데이터 시뮬레이션에서 정상 cross-source 5건은 모두 유지, 사용자 제시 false-positive 사례(group 5) 는 정확히 분리,
>   추가 false-positive(group 25, 4건 전체) 도 함께 분리됨.
> - 임계값·튜닝이 없는 결정적 매칭이라 동작 예측·디버깅 용이.
> - 사용자 원문 "단순히 공고번호 일치 + 공고명 일치 로 가는것도 방법일 것 같은데 검토해봐" 의도와 부합.

### 8-2. 권장 canonical_key 포맷 (구현 사양 — 별도 task 진행 전제)

```
official scheme:
  canonical_key = "official:{normalized_ancmNo}::{normalized_title}"

fuzzy scheme (ancmNo 부재 시 — 현행 그대로):
  canonical_key = "fuzzy:{normalized_title_50}:{normalized_agency}:{deadline_year}"
```

`::` 구분자는 ancmNo 와 title 사이에 사용 (ancmNo 자체가 단일 콜론 ':' 을 포함하지 않으므로 충돌 없음).

### 8-3. 정규화 규칙 (확정안)

| 필드 | 규칙 |
|------|------|
| ancmNo (official) | NFKC + 공백 전체 제거 (현행 `_normalize_official_key` 그대로) |
| title (official 비교용 신규) | NFKC → NTIS suffix 제거 (`\s*_\([0-9]{4}\).*$`) → 공백 전체 제거. **앞 N 자 truncation 미적용** (정확일치이므로 길이 제한 불필요). |
| title (fuzzy 그대로) | NFKC + 공백/특수문자 제거 + 앞 50 자 (현행 `_normalize_fuzzy_title` 유지) |
| agency (fuzzy 그대로) | 현행 유지 |

### 8-4. 마이그레이션 영향

| 영향 범위 | 내용 |
|-----------|------|
| `canonical_projects` | 신규 키 정의로 row 분리 발생. 현 데이터 기준 기존 7건 다중 그룹 중 group 5(2 건) 와 group 25(4 건) 가 추가 그룹으로 분리됨 → **canonical_projects rows: 52 → 56 (+4)** 예상. 그 외 그룹은 변동 없음. |
| `announcements.canonical_group_id` | 위 두 그룹 소속 6건의 `canonical_group_id` 갱신 필요. 그 외 46건은 동일. |
| `is_current=False` 이력 row | 동일 규칙으로 backfill. 승계(`new_version`) 로직은 기존 그대로 동작. |
| backfill 스크립트 | 기존 `scripts/backfill_canonical.py` 패턴 재사용 권장 (멱등). dry-run 옵션 유지. |
| 검증 스크립트 | 기존 `scripts/audit_canonical_false_positives.py`, `scripts/verify_canonical_iris.py`, `scripts/verify_canonical_cross_source.py` 회귀 테스트 통과 확인 필수. |

### 8-5. 알려진 trade-off

1. **재공고에서 공고명을 부분 변경하는 케이스**: 같은 ancmNo 라도 새 그룹이 생성됨. 이는 의도된 동작(사용자가
   허위 중복으로 정의한 케이스 유형) 으로 본다. 만약 운영 중 "재공고 시 사소한 제목 변경(예: 마감일 포함)" 이
   다수 관찰되면 추후 별도 보정 로직(canonical_overrides merge 또는 정규화 추가) 으로 처리.
2. **NTIS 외 신규 source 추가 시**: title 표기 변이 패턴 재조사 필요. 신규 정규화 규칙 추가 형태로 점진 보강.
3. **`canonical_overrides` Phase 5 의 split TODO 가 알고리즘 수준에서 자동 해소됨**:
   `docs/canonical_grouping_audit_00036.md §3-3` 의 group 32 (ann 33,34) split TODO 는 현 시점 DB 에는 더 이상
   존재하지 않으나(이력 row 가 아니라 새 데이터셋), 동일 패턴 출현 시 알고리즘이 자동 분리하므로 manual override
   필요성이 줄어든다.

---

## 9. 후속 작업 제안 (본 task 종료 후)

본 subtask 의 acceptance 는 **본 문서 작성 1 건**으로 충족된다. 후속 코드 변경은 사용자 확인 후 별도 task 로 진행한다.
다음 작업 단계를 사전 정리한다.

1. **canonical_key 정의 변경**: `app/canonical.py:compute_canonical_key` 에 title 정규화 + 합성 키 로직 추가.
   `_normalize_official_title` 헬퍼 신규 추가.
2. **repository 변경 없음 예상**: `_apply_canonical` 의 흐름은 유지. `compute_canonical_key` 의 반환값만 새 형식이
   되도록 한다.
3. **Backfill**: `scripts/backfill_canonical.py` 를 신규 키 정의로 1 회 실행. dry-run 으로 group 분리 수 사전 확인.
4. **검증**: `scripts/verify_canonical_iris.py` 시나리오 A~D + `scripts/verify_canonical_cross_source.py` E~H +
   `scripts/audit_canonical_false_positives.py` 재실행. group 5, 25 가 분리되었는지 확인.
5. **테스트 추가**: `tests/test_canonical.py` 등에 false-positive 분리 케이스 (5/6, 26/27/28/29) 와 cross-source
   유지 케이스 (16/35 NTIS suffix 정규화) 회귀 테스트 신설.
6. **문서 갱신**:
   - `docs/canonical_identity_design.md §4-1, §11`: 신규 키 포맷 + 알려진 false-positive 해소 반영.
   - `docs/canonical_grouping_audit_00036.md`: §5 Phase 5 TODO 항목 업데이트(알고리즘 해결로 표시).
   - `PROJECT_NOTES.md`: 본 변경 요약 추가.
7. **(선택) UI 영향 점검**: 즐겨찾기·검색·중복 표시 UI 가 `canonical_group_id` 단위로 동작하는 부분이 있다면 그룹 수 증가에 따른 영향 점검.

---

## 10. 참고

- `docs/canonical_identity_design.md` §2-3 (N:1 구조), §4-1 (official key 정규화), §11 (알려진 false-positive)
- `docs/canonical_grouping_audit_00036.md` §3-2 (다중 그룹 수동 분류), §5 (Phase 5 TODO)
- `app/canonical.py` `compute_canonical_key`, `_normalize_official_key`, `_normalize_fuzzy_title`
- `app/db/repository.py:159` `_apply_canonical` (UPSERT 4-branch 통합)
- `app/db/repository.py:1072` `recompute_canonical_with_ancm_no` (fuzzy → official 승급)
- `scripts/audit_canonical_false_positives.py` 임계값 상수 + 분류 기준
