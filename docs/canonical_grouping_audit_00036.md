# canonical 묶음 품질 감사 (00036-2)

> **작성**: Task 00036 subtask 00036-2  
> **감사 일시**: 2026-04-24  
> **감사 스크립트**: `scripts/audit_canonical_false_positives.py`  
> **실행 명령**: `docker compose run --rm app python scripts/audit_canonical_false_positives.py --show-id 17 33 34 39`  
> **DB 접근**: 읽기 전용 (SessionLocal). 어떤 row 도 변경하지 않음.

---

## §1 전체 통계 (감사 시점 스냅샷)

| 항목 | 값 |
|------|-----|
| canonical_projects 테이블 rows | 62 |
| is_current 공고 총계 | 60 |
| canonical 미할당(orphan) | 0 |
| official scheme 공고 | 42 |
| fuzzy scheme 공고 | 18 |
| 단독(1건) 그룹 | 46 |
| 다중(2건+) 그룹 | 7 |
| 최대 그룹 크기 | 2 |

**그룹 크기 분포**:
- size=1: 46그룹
- size=2: 7그룹

---

## §2 fuzzy scheme 다중 그룹

```
감사 시점: 해당 그룹 없음 (0건)
```

fuzzy scheme 으로 묶인 공고 18건 모두 단독 그룹이다.
fuzzy false-positive 는 현재 데이터셋에서 관찰되지 않는다.

---

## §3 false-positive 후보 분석

### §3-1 자동 탐지 결과

```
§3 기준 (동일 source_type 2건+인 fuzzy 그룹): 해당 없음 (0건)
```

fuzzy 다중 그룹 자체가 0건이므로 자동 탐지 기준에는 해당 없음.

### §3-2 official 그룹 내 false-positive — 수동 확인 결과

official scheme 다중 그룹 7건을 수동 분류한 결과:

| canonical_group_id | canonical_key | ann ids | 분류 | 근거 |
|--------------------|---------------|---------|------|------|
| 1 | `official:과학기술정보통신부공고제2026-0498호` | 1, 2 | **정상 N:1** | 같은 ancmNo 아래 IRIS 세부과제 2건 (딥테크/창업). 동일 기관·마감일. |
| 14 | `official:과학기술정보통신부공고제2026-0485호` | 15, 37 | **정상 cross-source** | IRIS id=15 + NTIS id=37. 동일 제목·마감일(2026-04-30). |
| 15 | `official:과학기술정보통신부공고제2026-0484호` | 16, 38 | **정상 cross-source** | IRIS id=16 + NTIS id=38. 동일 제목·마감일(2026-04-30). |
| 16 | `official:산업통상부공고제2026-300호` | **17, 39** | **정상 cross-source** | IRIS id=17 + NTIS id=39. 동일 제목·마감일(2026-05-11). |
| 17 | `official:과학기술정보통신부공고제2026-0455호` | 18, 40 | **정상 cross-source** | IRIS id=18 + NTIS id=40. 한-스페인 공동연구사업. 동일 제목·마감일(2026-05-19). |
| 25 | `official:과학기술정보통신부공고제2026-0362호` | 26, 27 | **정상 N:1** | 같은 ancmNo 아래 IRIS 세부과제 2건 (강원1-1/강원1-2). 동일 기관·마감일. |
| **32** | `official:과학기술정보통신부공고제2026-0408호` | **33, 34** | **⚠ false-positive 후보** | 아래 §3-3 참조. |

### §3-3 canonical_group_id=32 상세 분석 (ann 33+34)

```
canonical_group_id=32
canonical_key=official:과학기술정보통신부공고제2026-0408호

id=33  src=NTIS/1260361   status=접수예정  deadline=2026-05-12
       title=2026년 AI 기반 대학 과학기술 혁신사업(중앙거점) 신규과제 공모
       agency='과학기술정보통신부'  scheme=official

id=34  src=NTIS/1260360   status=접수예정  deadline=2026-05-22
       title=2026년 AI 기반 대학 과학기술 혁신사업(AI4ST) 신규과제 공모
       agency='과학기술정보통신부'  scheme=official
```

**판단: official N:1 구조 내 세부과제 분리 케이스**

두 공고는 동일한 official canonical_key (`과학기술정보통신부공고제2026-0408호`)를 공유한다.
이는 하나의 공고번호 아래 두 개의 별개 세부과제가 묶인 **N:1 구조**이다 (IRIS docs §2-3 참조).

실질적인 차이점:
- 세부 명칭: `(중앙거점)` vs `(AI4ST)` — 다른 지원 유형
- 마감일: 2026-05-12 vs 2026-05-22 — 10일 차이
- source_type: 둘 다 NTIS (IRIS 측 대응 공고 수집 안 됨)

**왜 false-positive "후보"인가**:  
같은 ancmNo 아래 여러 세부과제가 묶이는 것은 official scheme 의 N:1 구조 설계 범위 내다.
그러나 마감일이 달라 별개 과제로 관리할 필요성이 있을 수 있다.
Phase 5 `canonical_overrides` 에서 split 여부를 사용자가 결정해야 한다.

**왜 지금 split 하지 않는가**:
- official key 가 동일한 한 알고리즘 수정 없이 split 불가
- split 시 재수집 중 동일 키로 재묶일 수 있음 (알고리즘 수정 필요)
- 사용자가 직접 canonical_overrides 로 override 해야 안전

---

## §4 정상 묶음 확인 (17·39)

ann 17(IRIS/021075) + ann 39(NTIS/1262381) 조합은 **정상 cross-source 묶음**이다.

```
id=17  src=IRIS/021075    status=접수중  deadline=2026-05-11
       title=2026년도 제조암묵지기반AI모델개발사업 신규지원 대상과제 공고
       canonical_key=official:산업통상부공고제2026-300호

id=39  src=NTIS/1262381   status=접수중  deadline=2026-05-11
       title=2026년도 제조암묵지기반AI모델개발사업 신규지원 대상과제 공고
       canonical_key=official:산업통상부공고제2026-300호
```

동일 제목, 동일 마감일, IRIS+NTIS cross-source. 설계 의도대로 동작.

---

## §5 결론 및 Phase 5 TODO

### 현재 상태 요약

| 분류 | 건수 | 비고 |
|------|------|------|
| 정상 단독 그룹 | 46 | 단일 공고 |
| 정상 cross-source 그룹 | 4 | IRIS+NTIS 동일 과제 |
| 정상 N:1 그룹 | 2 | 동일 ancmNo 아래 세부과제 (group_id=1, 25) |
| false-positive 후보 | 1 | group_id=32 (ann 33+34) |

### Phase 5 TODO

```
TODO split: canonical_group_id=32
  ann_ids: [33, 34]
  canonical_key: official:과학기술정보통신부공고제2026-0408호
  reason: 동일 ancmNo 아래 별개 세부과제 (중앙거점/AI4ST), 마감일 다름
  action: canonical_overrides split (Phase 5 구현 후 실행)
  주의: split 전 알고리즘 수준에서 sub-task 키 구분 방법 설계 필요
```

### 감사 반복 실행 방법

데이터가 추가된 후 재감사:
```bash
docker compose run --rm app python scripts/audit_canonical_false_positives.py
```

false-positive 후보 기준값 조정은 `scripts/audit_canonical_false_positives.py` 상단 상수 참조:
```python
SAME_SOURCE_TYPE_THRESHOLD: int = 2   # 초기 튜닝값
DEFAULT_TOP_N: int = 10               # 초기 튜닝값
```

---

> **관련 문서**:
> - [docs/canonical_identity_design.md §11](canonical_identity_design.md) — 알려진 false-positive 요약
> - [docs/favorites_ui_design.md §9.1](favorites_ui_design.md) — 즐겨찾기 UI 한계 단락
