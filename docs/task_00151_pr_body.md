# PR 본문 초안 — task 00151: 173/174 공고 canonical_id 충돌 분리

## 요약

서로 다른 두 NTIS 공고(announcements.id=173, 174)가 같은 canonical_group_id=152 로 잘못 묶이는 사고가 발생했다. 본 PR 은 (a) canonical_key 합성 로직을 수정해 같은 사고가 재발하지 않도록 막고, (b) cross-source fallback 매칭을 정제해 기존 IRIS↔NTIS 정상 쌍은 보존하며, (c) 일회성 재계산 스크립트로 운영 DB(`data/db/app.sqlite3`) 의 173/174 분리 및 부수적 데이터 정정을 적용했다.

## 왜 두 공고가 같은 canonical_id 로 묶였는가 (근본 원인)

ann 173 과 ann 174 는 둘 다 NTIS 가 게시한 공고로,

- **ann 173** title: `2026년도 나노소재기술개발사업 신규과제 4차 재공모_(2026)국가전략기술미래소재기술개발(미래소재) 공고`
- **ann 174** title: `2026년도 나노소재기술개발사업 신규과제 4차 재공모_(2026)글로벌공급망첨단소재기술개발-나노커넥트 공고`

같은 공식 공고번호(`과학기술정보통신부 공고 제2026-0627호`) 아래 게시된 **서로 다른 sub-business 두 건** 이다. 두 row 의 차이는 `_(2026)` 뒤에 오는 sub-business 식별자(`국가전략기술미래소재기술개발(미래소재) 공고` vs `글로벌공급망첨단소재기술개발-나노커넥트 공고`) 에 있다.

`app/canonical.py::_normalize_official_title` 에는 다음 정규식이 있었다:

```python
_NTIS_TITLE_SUFFIX = re.compile(r"\s*_\([0-9]{4}\).*$")
```

이 정규식은 NTIS 통합공고가 표시 변이로 부착하는 `_(YYYY)<사업명>` suffix 를 **무조건 절단**해 canonical_key 본문에서 제거했다. 동기는 docs/duplicate_detection_analysis.md §5-1 의 분석으로, IRIS title (`...신규과제 재공모`) 과 NTIS title (`...신규과제 재공모 _(2026)2026년도 대형가속기정책센터 신규과제 재공모`) 이 같은 과제임에도 표면만 다른 케이스를 흡수하려는 것이었다.

그러나 173/174 는 단순 표시 변이가 아니라 **서로 다른 sub-business 공고가 같은 ancmNo 아래에서 별도 row 로 게시**되는 케이스였다. suffix 를 절단하자 두 title 의 정규화 결과가 모두 `2026년도나노소재기술개발사업신규과제4차재공모` 로 동일해져, canonical_key 가 같아지고 두 row 가 같은 CanonicalProject(`id=152`) 에 매칭됐다.

| 단계 | ann 173 | ann 174 |
|---|---|---|
| 원본 title (NFKC) | `...4차 재공모_(2026)국가전략기술미래소재기술개발(미래소재) 공고` | `...4차 재공모_(2026)글로벌공급망첨단소재기술개발-나노커넥트 공고` |
| 구 logic: suffix 절단 후 | `...4차재공모` | `...4차재공모` |
| 구 canonical_key | `official:과학기술정보통신부공고제2026-0627호::2026년도나노소재기술개발사업신규과제4차재공모` | 위와 **동일** |

같은 패턴(같은 ancmNo + NTIS 의 sub-business 가 둘 이상) 이 만들어지는 사고는 본 사례 외에는 발견되지 않았지만, 운영 DB 의 ancmNo `과학기술정보통신부 공고 제2026-0601호` 아래에도 IRIS umbrella 1건(ann 133) + NTIS sub-business 2건(ann 156 "-플랫폼형", ann 162 "_(2026)... 소재HUB") 의 비슷한 구조가 있다(이 경우 156 의 suffix 는 NTIS pattern 아니라 직접 충돌은 없었음). 본 PR 의 로직은 이 변형 케이스도 안정적으로 다룬다.

## 무엇을 바꿨는가

### 1. canonical_key 합성에서 NTIS suffix 절단 제거 (`app/canonical.py`)

- `_normalize_official_title` 의 `_NTIS_TITLE_SUFFIX` 절단 단계를 제거했다. canonical_key 본문은 **full title** 을 보존한다. → 173/174 의 정규화 title 이 서로 달라지고 canonical_key 가 결정론적으로 분리된다.
- 절단 헬퍼는 `strip_ntis_business_suffix` 라는 외부 노출 함수로 옮겨, cross-source 매칭의 prefix 비교 전용으로 남겼다.

### 2. cross-source fallback 매칭을 `_apply_canonical` 보조 분기로 도입 (`app/db/repository.py`)

`_normalize_official_title` 가 suffix 를 보존하게 되면 IRIS title 과 NTIS title (suffix 부착) 은 canonical_key 자체가 다르다. 같은 과제임에도 별 group 으로 떨어지는 false-negative 를 막기 위해 `_apply_canonical` 에 보조 매칭 단계를 추가했다.

**매칭 우선순위 (새 로직)**

1. `canonical_projects.canonical_key` 정확일치.
2. official scheme 한정 — `_find_cross_source_canonical_group`:
   - 같은 ancmNo prefix(`official:<X>::%`) 의 다른 is_current row 중,
   - NTIS suffix 절단 형태(`strip_ntis_business_suffix`) 가 본 row 와 **동치인 후보** 를 source 별로 모은다.
   - **same source** 의 동치 후보가 있으면 거부 (umbrella 1건이 NTIS sub-business 여러 건과 모두 strip 동치인 다대일 케이스 차단).
   - **cross source** 의 동치 후보가 정확히 1건이고 그 후보의 `canonical_group_id` 가 채워져 있으면 그 group 공유.
3. 매칭 실패 시 신규 `CanonicalProject` 생성.

### 3. cross-source fallback 정제 — title strip 기준 분류

00151-1 의 초기 구현은 **"같은 ancmNo prefix 의 same source 가 1건이라도 있으면 거부"** 라는 조건을 썼다. 이 보수적 가드는 173/174 분리는 가능하게 했지만, 운영 DB 의 group 121 (ancmNo 0601호: IRIS 133 + NTIS 162) 처럼 같은 ancmNo 아래 다른 NTIS sub-business (ann 156 "-플랫폼형") 가 같이 있는 케이스에서 ann 162 의 매칭을 막아 group 121 의 IRIS+NTIS 쌍이 깨지는 회귀가 있었다.

본 PR 은 가드를 **title strip 기준으로 정제** 했다. same source 후보 중 *strip-title 동치인 것만* 거부 조건으로 본다.

- ann 156 의 strip 은 `...3차재공모-플랫폼형` 이고 ann 162 의 strip 은 `...3차재공모` 라 동치가 아니다 → ann 156 은 ann 162 의 same-source 가드에 끼지 않는다.
- ann 162 의 cross-source 동치 후보는 ann 133 (IRIS, strip `...3차재공모`) 한 건이므로 매칭 성공. → group 121 유지.
- ann 173 / 174 는 동일 ancmNo 아래 IRIS 가 아예 없어 cross-source 동치 후보가 0건. → 매칭 실패, 각자 새 group. → 분리 유지.
- 가상의 케이스 (IRIS 1 + NTIS 2 가 *모두* strip 동치): IRIS 입장에서는 cross-source 동치 후보가 2건이라 모호하므로 거부, NTIS 입장에서는 same-source 동치 후보(다른 NTIS) 가 있으므로 거부 — 셋 다 분리.

### 4. 단위 회귀 테스트 (`tests/test_canonical*.py`)

- `tests/test_canonical.py::DIFFERENT_KEY_CASES` 에 173/174 분리 케이스 추가.
- `tests/test_canonical_cross_source_fallback.py` 에 in-memory 통합 시나리오 추가:
  - group 17 / 121 cross-source 쌍 유지 (parametrize),
  - NTIS 가 IRIS 보다 먼저 들어와도 매칭,
  - 173/174 분리,
  - IRIS umbrella + NTIS 두 sub-business 케이스 (group 121 변형) 에서 strip 동치인 쌍만 유지,
  - 173/174 가 있는 ancmNo 에 IRIS 가 나중에 들어와도 셋 다 분리.

총 21 케이스 모두 통과.

### 5. 운영 DB 정정 — 일회성 재계산 스크립트 (`scripts/python/recompute_canonical_00151.py`)

새 로직은 신규 수집부터 적용되므로, 이미 채워진 173/174 의 group_id 를 정정하려면 운영 DB 재계산이 필요하다. 일회성 스크립트를 작성해 적용했다.

**처리 흐름** (단일 트랜잭션, 멱등):

1. 모든 `is_current=True` row 에 대해 새 `compute_canonical_key` 로 canonical_key/scheme 을 재계산.
2. 모든 대상 row 의 `canonical_group_id` 를 NULL 로 리셋, canonical_key/scheme 을 새 값으로 덮어쓴다 (LIKE prefix 쿼리가 일관된 새 prefix 를 보도록).
3. id 오름차순으로 각 row 에 대해 `_apply_canonical` 과 동일한 매칭 (exact → cross-source fallback → 신규 생성) 적용.
4. `is_current=False` 이력 row 에 매칭되는 현재 row 에서 canonical 필드 전파.
5. 어떤 announcement 도 더 이상 참조하지 않는 `canonical_projects` 삭제.

**사용법**
```bash
# dry-run (변경 미반영)
python scripts/python/recompute_canonical_00151.py --dry-run

# 실제 운영 DB 적용
python scripts/python/recompute_canonical_00151.py
```

## 적용 결과 (영향 받은 row 수)

운영 DB(`data/db/app.sqlite3`) 적용 직후:

| 항목 | 수치 |
|---|---|
| 처리한 `is_current=True` row | 190 건 |
| 처리한 `is_current=False` 이력 row | 28 건 |
| 새로 생성된 canonical_projects | 12 건 (id 184~195) |
| 삭제된 orphan canonical_projects | 20 건 (사고 row group 152 + 사전 누적 orphan 9건 + 본 적용 후 새 orphan 10건) |
| canonical_projects 총수 | 183 → 175 |
| `canonical_group_id` 가 변경된 announcement row | 12 건 |
| `canonical_key` 가 변경된 announcement row | 14 건 |

**173/174 분리 결과**

```sql
sqlite> SELECT id, canonical_group_id FROM announcements WHERE id IN (173, 174);
173 | 190
174 | 191
```

**기존 cross-source 묶음 유지** (ancmNo 0485호 IRIS+NTIS, 0601호 IRIS+NTIS, 외 다수)

```sql
sqlite> SELECT id, source_type, canonical_group_id FROM announcements WHERE id IN (17, 35, 133, 162);
17  | IRIS | 17
35  | NTIS | 17    -- 그룹 17 유지
133 | IRIS | 121
162 | NTIS | 121   -- 그룹 121 유지
```

**동일 source_type 다중 멤버 그룹 부재 검증**

```sql
sqlite> SELECT canonical_group_id, COUNT(*) FROM announcements
        WHERE is_current=1 AND source_type='NTIS'
        GROUP BY canonical_group_id HAVING COUNT(*)>1;
(빈 결과)
```

**추가로 정정된 12 건**

대부분 singleton 그룹의 stale canonical_key (과거 다른 ancm_no 로 적재됐다가 이후 list_row.ancm_no 가 갱신됐지만 canonical_key 는 갱신되지 않았던 데이터 드리프트 케이스). 새 로직이 현재 list_row.ancm_no + 현재 title 기준으로 키를 다시 만들어 일관성을 회복했다.

| ann.id | 변경 이유 |
|---|---|
| 58, 98, 123, 129, 146, 205, 216 | 과거 `NR_/II_/KT_` 프리픽스로 적재된 stale ancm_no 가 현재 list_row 의 정식 ancmNo 로 갱신 |
| 157, 210 | 현재 title 이 과거 저장 시점과 미세 변경 (`_(2026)(연장공고)` 추가 / `수정` 추가 등) |
| 206 | 특수문자 `&` 가 raw title 보존 결과 키에 반영됨 |
| 173, 174 | 본 사고 분리 |

각 건 모두 **singleton group** 이라 cross-source 짝을 깨뜨리지 않는다.

**멱등성**

재계산 스크립트를 두 번째로 실행(`--dry-run`)하면 모든 카운터가 0:

```
새로 생성된 canonical_projects: 0
삭제된 orphan canonical_projects: 0
canonical_group_id 가 변경된 row: 0
canonical_key 가 변경된 row: 0
```

## 앞으로 어떻게 동작할 것인가

새 수집 시점에 `_apply_canonical` 이 동일한 우선순위(exact → cross-source title-aware fallback → 신규)를 적용한다. 운영에서 흔한 패턴별 동작:

| 입력 패턴 | 새 동작 |
|---|---|
| 동일 ancmNo 의 IRIS 1건 + NTIS 1건 (suffix 부착) | exact 불일치 → cross-source fallback: cross-source 동치 후보 1건, same-source 동치 없음 → 같은 group 공유. **그룹 17 / 121 패턴 유지.** |
| 동일 ancmNo 의 NTIS 2건 sub-business (IRIS 없음) | cross-source 동치 후보 0건 → 매칭 실패 → 각자 새 group. **173/174 패턴 분리.** |
| 동일 ancmNo 의 IRIS umbrella 1건 + NTIS 2건 (한 NTIS 는 IRIS strip 과 동치, 한 NTIS 는 다른 strip) | strip 동치인 NTIS 만 IRIS umbrella 와 같은 group. 비동치 NTIS 는 별 group. **133/156/162 패턴 유지.** |
| 동일 ancmNo 의 IRIS 1건 + NTIS 2건 (둘 다 IRIS strip 과 동치) | IRIS 입장 cross-source 동치 후보 2건 → 모호로 거부. NTIS 입장 same-source 동치 후보 있음 → 거부. **셋 다 분리** (보수적). |

각 패턴은 단위/통합 테스트로 고정돼 있다 (`tests/test_canonical.py`, `tests/test_canonical_cross_source_fallback.py`).

## 롤백 안내

사용자가 DB 백업을 사전에 확보한 상태에서 진행했다. 본 PR 의 DB 변경을 되돌리려면:

1. (DB) 사용자 측 백업으로 `data/db/app.sqlite3` 복원.
2. (코드) `git revert` 또는 코드 측 변경 되돌림.

코드 변경만 되돌리고 DB 는 그대로 두는 경우, 신규 수집은 다시 구 로직으로 동작하지만 본 PR 이 분리한 173/174 의 group_id 는 그대로 다른 값으로 남는다 (수집 시 cron 으로 동일 ancmNo 의 같은 sub-business row 가 다시 들어오면 새 logic 없이는 다시 충돌할 수 있음).

## 영향 받지 않는 영역

- `canonical_overrides` 같은 수동 매핑 테이블은 현 schema 에 ORM 매핑된 것이 없어 본 PR 의 재계산에서 침범되지 않는다.
- `is_current=False` 이력 row 는 매칭되는 현재 row 에서 canonical 필드를 그대로 전파받는다 (28 건 처리).
- `representative_title` / `representative_agency` 는 신규 cp 생성 시 최초 row 의 값으로 채워지고 기존 cp 는 그대로 유지된다.
