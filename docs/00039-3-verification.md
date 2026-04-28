# 00039-3 신규 canonical_key 동작 데이터 검증 결과

> **수집 일시**: 2026-04-28
> **DB 상태**: 사전 초기화 → 본 subtask 가 IRIS+NTIS 1 페이지씩 신규 수집
> **검증 대상**: 00039-1 에서 도입한 `official:{normalized_ancmNo}::{normalized_title}` 합성 키
> **인용 분석**: `docs/duplicate_detection_analysis.md` §3-1, §8-4

본 문서는 후속 문서 갱신(00039-4: design / audit / PROJECT_NOTES) 의 입력 자료다.

---

## 1. 스크래퍼 재수집 결과

```
스크래퍼 명령: docker compose --profile scrape run --rm scraper
범위: IRIS(max_pages=1) + NTIS(max_pages=1, max_announcements=100)
결과: 목록 성공 52건 / 실패 0건 | 상세 성공 52건 / 실패 0건 | 첨부 213건 저장
final_status: completed
```

분석 §3-1 의 모집단 규모(52건) 와 정확히 일치하여 비교 신뢰도가 높다.

## 2. audit_canonical_false_positives.py — 핵심 통계 비교

| 항목 | 분석 §3-1 (구 키) | 본 검증 (신 키) | 분석 §8-4 예측 | 일치 |
|------|-------------------|------------------|----------------|------|
| canonical_projects rows | 52 | **56** | 52 → 56 (+4) | ✓ |
| is_current 공고 | 52 | 52 | 52 | ✓ |
| official scheme | 39 | 39 | 39 | ✓ |
| fuzzy scheme | 13 | 13 | 13 | ✓ |
| 단독(1건) 그룹 | 36 | **42** | 36 + 6 (split) = 42 | ✓ |
| 다중(2건+) 그룹 | 7 | **5** | 7 - 2 (split) = 5 | ✓ |
| 최대 그룹 크기 | 4 | **2** | 4 → 2 (group 25 분리) | ✓ |

## 3. 다중 그룹 5건 — 모두 정상 cross-source

audit §4 출력 발췌. 모든 묶음이 IRIS+NTIS 동일 과제 페어이며, 신규 합성 키
하에서도 그대로 유지됨.

| canonical_key | 매칭 유형 | 비고 |
|---|---|---|
| `official:과학기술정보통신부공고제2026-0485호::2026년도대형가속기기술개발·진흥지원체계구축신규과제재공모` | NTIS suffix 정규화 | NTIS 측 title 에 `_(2026)2026년도 대형가속기정책센…` 부착부 절단 후 정확일치 |
| `official:과학기술정보통신부공고제2026-0484호::2026년도가속기핵심기술개발사업신규과제재공모` | 정확일치 cross-source | IRIS+NTIS title 동일 |
| `official:산업통상부공고제2026-300호::2026년도제조암묵지기반AI모델개발사업신규지원대상과제공고` | **사용자 제시 정상 사례** | 분석 §1-1 (제조암묵지) — 그대로 유지됨 |
| `official:과학기술정보통신부공고제2026-0455호::2026년도한-스페인공동연구사업신규과제공모` | 정확일치 cross-source | 한-스페인 공동연구 |
| `official:과학기술정보통신부공고제2026-0444호::2026년도AI반도체를활용한K-클라우드기술개발사업신규지원대상과제공고` | leading whitespace 정규화 | IRIS 측 ` 2026년도…` 의 선행 공백이 정규화로 흡수 |

## 4. False-positive 분리 — 사용자 제시 사례 직접 확인

분석 §1-2 / §3-2 가 false-positive 로 분류한 두 그룹은 신규 키 하에서 모두 분리됨.

### 4-1. 5극3특 연구개발특구 딥테크 (구 group 5)

```
ancmNo: 과학기술정보통신부공고제2026-0498호
새 canonical_group_id 2개:
  ▸ official:과학기술정보통신부공고제2026-0498호::5극3특연구개발특구딥테크지원(초기스케일업)
  ▸ official:과학기술정보통신부공고제2026-0498호::5극3특연구개발특구딥테크지원(기획형창업)
```

분석 §1-2 사용자 제시 케이스 — 구 키에서 한 그룹으로 묶이던 두 건이 합성 키
하에서 정확히 분리됨.

### 4-2. 강원 sub-task 4건 (구 group 25)

```
ancmNo: 과학기술정보통신부공고제2026-0362호
새 canonical_group_id 4개 (각 1건):
  ▸ 강원 1-1
  ▸ 강원 1-2
  ▸ 강원 4-1
  ▸ 강원 5
```

분석 §3-2 group 25 — 구 키에서 1 그룹/4건으로 묶이던 4 sub-task 모두
별개 그룹으로 분리됨.

## 5. SQL 직접 sanity check — 동일 ancmNo 묶음 분포

```sql
WITH official_rows AS (
  SELECT
    id, title, canonical_group_id,
    SUBSTR(canonical_key, 10, INSTR(canonical_key, '::') - 10) AS ancm_normalized
  FROM announcements
  WHERE is_current = 1
    AND canonical_key_scheme = 'official'
    AND canonical_key LIKE 'official:%::%'
)
SELECT ancm_normalized, COUNT(*), COUNT(DISTINCT canonical_group_id), COUNT(DISTINCT title)
FROM official_rows
GROUP BY ancm_normalized
HAVING COUNT(*) >= 2
ORDER BY COUNT(DISTINCT title) DESC;
```

| ancm_normalized | rows | distinct_groups | distinct_titles | 해석 |
|---|---|---|---|---|
| 과학기술정보통신부공고제2026-0362호 | 4 | 4 | 4 | 4건 모두 분리 — group 25 false-positive 해소 |
| 과학기술정보통신부공고제2026-0444호 | 2 | **1** | 2 | leading whitespace 정규화로 동일 그룹 유지 |
| 과학기술정보통신부공고제2026-0485호 | 2 | **1** | 2 | NTIS suffix 정규화로 동일 그룹 유지 |
| 과학기술정보통신부공고제2026-0498호 | 2 | 2 | 2 | 2건 분리 — group 5 false-positive 해소 |
| 과학기술정보통신부공고제2026-0455호 | 2 | 1 | 1 | 정확일치 cross-source 유지 |
| 과학기술정보통신부공고제2026-0484호 | 2 | 1 | 1 | 정확일치 cross-source 유지 |
| 산업통상부공고제2026-300호 | 2 | 1 | 1 | 정확일치 cross-source 유지 |

요약: distinct_titles ≥ 2 인 4 건 중 2건은 정규화로 흡수(`distinct_groups=1`),
2건은 의도대로 분리(`distinct_groups=N`). distinct_titles=1 인 3건은 cross-source
정확일치로 유지. **현 데이터셋에서 false-positive 0건, false-negative 0건.**

## 6. verify 스크립트 결과

| 스크립트 | 결과 | 비고 |
|----------|------|------|
| `scripts/verify_canonical_iris.py` | **PASS=16 / FAIL=2** | 시나리오 B 의 fixture 가 구 contract 기반(같은 ancmNo + 다른 title 도 같은 그룹) 이라 신 키에서는 2건 FAIL. 분석 §8-5 가 명시한 의도된 동작 — fixture 갱신은 후속 작업 |
| `scripts/verify_canonical_cross_source.py` | **PASS=15 / FAIL=0** | E/F/G/H 전 시나리오 통과 — fuzzy → official 승급, 표기 변이 ancmNo, agency 차이로 fuzzy 분리, 모두 정상 |

### 6-1. verify_canonical_iris.py 시나리오 B 의 의미

scenario_b 는 `ancmId 변경 시 같은 canonical_group 유지` 회귀를 가드한다.
fixture 의 두 입력이 `title="원본 공고"` / `title="재등록 공고"` 로 다르므로,
신규 합성 키 하에서는 의도대로 다른 그룹이 된다.

분석 §8-5 결정과 일치: "재공고에서 공고명을 부분 변경하는 케이스: 같은 ancmNo
라도 새 그룹이 생성됨. 이는 의도된 동작(사용자가 허위 중복으로 정의한 케이스
유형) 으로 본다."

후속 단계(scope 외):
- scenario_b 의 두 fixture 를 동일 title 로 통일하여 원래 의도("ancmId 회전 시
  canonical 유지") 를 신 contract 위에서 다시 검증하도록 갱신.
- 또는 별도 시나리오 추가로 "다른 title → 다른 group" 분리 동작을 명시적으로
  회귀 가드.

## 7. 결론

- 분석 §3-1 / §8-4 의 모든 예측 수치가 실데이터에서 그대로 재현됨.
- 사용자 제시 false-positive 사례(group 5: 5극3특 딥테크) 와 추가 식별된
  false-positive(group 25: 강원 4건) 모두 분리됨.
- 사용자 제시 정상 cross-source(group 17: 제조암묵지) 와 NTIS suffix /
  leading whitespace 변이 케이스 모두 동일 그룹 유지됨.
- backfill 스크립트는 본 task 에서 실행하지 않음 (DB 가 빈 상태에서 신규
  수집되어 처음부터 새 키로 적재됨).
- verify_canonical_iris.py 의 scenario B 2 FAIL 은 분석 문서가 사전에
  의도한 동작 변경에 따른 fixture 회귀이며, 후속 단계에서 fixture 만 갱신하면
  해소됨.
