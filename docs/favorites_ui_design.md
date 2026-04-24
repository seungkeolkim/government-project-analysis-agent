# Phase 3b — 즐겨찾기 폴더 UI + 동일과제 expand UI 설계

> **작성 범위**: Task 00036 (Phase 3b) subtask 00036-1 — Phase 1a의
> `FavoriteFolder` / `FavoriteEntry` 위에 즐겨찾기 폴더 CRUD UI,
> 공고 추가/삭제/조회 UI, 동일과제 expand UI를 얹는 전체 설계.
> **코드 구현은 이 문서가 아니라 subtask 00036-3 ~ 00036-7이 수행한다.**
>
> **⚠ 불일치 규약**: 설계 내용이 후속 subtask 구현과 불일치할 경우,
> 해당 subtask에서 실제 코드를 먼저 수정한 뒤 이 문서를 갱신한다.
> "코드 ← 문서" 방향이 아닌 "문서 ← 코드" 방향으로 동기화한다.
>
> 참고 문서:
> - [docs/relevance_ui_design.md](relevance_ui_design.md) — N+1 방지 패턴, ensure_same_origin, 모달 흐름
> - [docs/schema_phase1a.md](schema_phase1a.md) — FavoriteFolder §2.6 / FavoriteEntry §2.7 스키마
> - [docs/canonical_identity_design.md](canonical_identity_design.md) — canonical_key, key_scheme 상세
> - [docs/canonical_grouping_audit_00036.md](canonical_grouping_audit_00036.md) — canonical 묶음 품질 감사 결과 (00036-2 산출, 한계 §9에서 참조)

---

## 0. 범위 밖 (이 task에서 구현하지 않음)

| 항목 | 예정 Phase |
|------|-----------|
| 폴더 공유 / 이동 | 후속 |
| `canonical_overrides` 실행 로직 (false-positive 병합/분할) | 5 |
| `audit_logs` write | 5 |
| 이메일 발송 | 4 |
| 대시보드 | 4 |

---

## 1. 데이터 단위 규칙

| 기능 | 단위 | 테이블 | 비고 |
|------|------|--------|------|
| 즐겨찾기 저장 | canonical_project | `favorite_entries` | `canonical_project_id` 기준. IRIS/NTIS 어느 쪽이든 같은 canonical이면 한 번 |
| 폴더 계층 | depth 0 (루트) / depth 1 (서브그룹) | `favorite_folders` | depth 2 초과 금지 |
| 동일과제 확인 | canonical_project 그룹 | `announcements.canonical_group_id` | 같은 canonical_project에 연결된 모든 announcement |

**canonical 단위 별 상태 동기화**: IRIS announcement에서 별을 저장했을 때,
같은 canonical_project에 연결된 NTIS announcement에서도 채워진 별이 보여야 한다.
canonical_project_id가 동일하므로 `favorite_entries` 조회 시 자동으로 동기화된다.

---

## 2. depth 2 validator 상세 (ORM 이벤트 리스너 방식)

### 2.1 Phase 1a 기존 구현 현황

`app/db/models.py` 에 `_enforce_favorite_folder_depth()` 함수와
`@event.listens_for(FavoriteFolder, "before_insert")` /
`@event.listens_for(FavoriteFolder, "before_update")` 리스너가 이미 구현되어 있다.

### 2.2 validator 동작 원리

```
INSERT / UPDATE 직전 (before_insert / before_update 이벤트)
  └─ _enforce_favorite_folder_depth(session, target) 호출

    parent_id IS None  →  target.depth = 0  →  OK
    parent_id IS NOT None
      ├─ self-reference 검사 (target.id == target.parent_id → ValueError)
      ├─ session.get(FavoriteFolder, parent_id)  →  부모 조회 (flush 컨텍스트 안)
      │   없으면 → ValueError("부모 폴더를 찾을 수 없습니다")
      └─ parent_row.parent_id IS NOT None  →  ValueError("최대 2단계까지만")
         parent_row.parent_id IS None     →  target.depth = 1  →  OK
```

### 2.3 DB flush 전/후 검증 순서

```
① Python 객체 생성 (FavoriteFolder(...))
   - 이 시점에서는 @validates 가 없으므로 제약 검사 없음
   - depth 기본값 0 이 설정됨

② session.add(folder)
   - 아직 flush 되지 않음. 제약 검사 없음

③ session.flush()  또는  session.commit() 시작
   └─ SQLAlchemy ORM 이 before_insert 이벤트 발생
      └─ _enforce_favorite_folder_depth 호출
         ├─ session.get() 으로 부모 조회 가능 (flush 이전이지만 동일 session 내 pending 객체 포함)
         ├─ 제약 위반 → ValueError 발생 → flush 중단 → session rollback 필요
         └─ 제약 통과 → target.depth 자동 설정 → flush 계속

④ DB에 INSERT SQL 실행 (flush 완료)

⑤ session.commit() → 트랜잭션 확정
```

**핵심 주의사항**: `_enforce_favorite_folder_depth` 는 `before_insert` / `before_update` 시점(flush 중)에 실행된다. `@validates("parent_id")` 방식은 생성자 시점에 호출되어 session이 없을 수 있어 부모 조회가 불완전하다. 현재 구현은 이벤트 리스너 방식으로 session이 항상 존재하는 flush 시점에 검사한다.

**ValueError 처리**: `before_insert` / `before_update` 에서 ValueError 발생 시 SQLAlchemy는 해당 flush를 중단하고 session을 invalid 상태로 만든다. API 레이어는 이 ValueError를 catch하여 400 응답으로 변환해야 한다.

### 2.4 API 에러 응답

```
POST /favorites/folders  body: {name: "sub", parent_id: 서브그룹의_id}
  → flush 중 ValueError("최대 2단계까지만")
  → API 레이어 catch → HTTP 400 {"error": "폴더는 최대 2단계까지만 허용됩니다."}
```

---

## 3. 폴더 엔드포인트 인터페이스

보안 공통: **모든 POST / PATCH / DELETE 엔드포인트는 `current_user_required` + `ensure_same_origin`** 을 적용한다.
자기 폴더만 수정/삭제 가능. 남의 폴더 id → 404.

### 3.1 GET /favorites/folders

현재 사용자의 폴더 트리를 반환한다.

```
Auth: current_user_required

200: {
  "folders": [
    {
      "id": 1,
      "name": "AI 과제",
      "depth": 0,
      "parent_id": null,
      "children": [
        {"id": 3, "name": "머신러닝", "depth": 1, "parent_id": 1, "children": []}
      ]
    },
    ...
  ]
}
401: 비로그인
```

### 3.2 POST /favorites/folders

```
Body: {"name": "폴더명", "parent_id": null | int}

201: {"id": int, "name": str, "depth": int, "parent_id": int | null}
400: depth 초과 ("폴더는 최대 2단계까지만 허용됩니다.")
400: 이름 비어 있음 / 길이 초과
401: 비로그인
404: parent_id 가 존재하지 않거나 남의 폴더
```

### 3.3 PATCH /favorites/folders/{id}

```
Body: {"name": "새 이름"}

200: {"id": int, "name": str, "depth": int, "parent_id": int | null}
400: 이름 비어 있음
401: 비로그인
404: 존재하지 않거나 남의 폴더
```

### 3.4 DELETE /favorites/folders/{id}

폴더 삭제. `favorite_folders.parent_id` FK ondelete=CASCADE이므로 자식 폴더도 함께 삭제된다.
자식 폴더 삭제 시 `favorite_entries` FK ondelete=CASCADE로 항목도 함께 삭제된다.

```
204: No Content
401: 비로그인
404: 존재하지 않거나 남의 폴더
```

---

## 4. 공고(FavoriteEntry) 엔드포인트 인터페이스

### 4.1 POST /favorites/entries

```
Body: {"folder_id": int, "canonical_project_id": int}

201: {"id": int, "folder_id": int, "canonical_project_id": int, "added_at": "<ISO 8601 UTC>"}
400: 같은 폴더에 같은 canonical 중복 (uq_favorite_entries_folder_canonical)
401: 비로그인
404: folder_id 가 존재하지 않거나 남의 폴더
404: canonical_project_id 가 존재하지 않음
```

### 4.2 DELETE /favorites/entries/{id}

```
204: No Content
401: 비로그인
404: 항목이 존재하지 않거나 남의 항목
```

### 4.3 GET /favorites/folders/{id}/entries

```
Auth: current_user_required

쿼리 파라미터:
  page (기본 1), per_page (기본 20, 최대 100)

200: {
  "entries": [
    {
      "id": int,
      "canonical_project_id": int,
      "added_at": "<ISO 8601 UTC>",
      "announcement": {
        "id": int,
        "title": str,
        "agency": str,
        "status": str,
        "deadline_at": "<ISO 8601 UTC> | null",
        "source_type": str,
        "relevance_verdict": "관련" | "무관" | null
      }
    },
    ...
  ],
  "total": int,
  "page": int,
  "per_page": int
}
401: 비로그인
404: 폴더가 존재하지 않거나 남의 폴더
```

각 entry의 `announcement` 는 해당 canonical_project에 속한 is_current=True 공고 중
가장 최근 것을 대표로 표시한다. 관련성 배지(relevance_verdict)도 함께 주입한다.

---

## 5. 별 아이콘 + 폴더 선택 모달 데이터 흐름

### 5.1 목록 페이지 별 상태 주입 흐름

```
1. 기존 list_announcements 로 현재 페이지 공고 목록 조회

2. (로그인 시만) canonical_project_id 집합 추출
     {ann.canonical_group_id for ann in page_items if ann.canonical_group_id}

3. get_favorite_canonical_id_set(user_id, canonical_ids)
     → set[int]  # 현재 사용자가 즐겨찾기한 canonical_project_id 집합
     # 비로그인 시 빈 set, 쿼리 없음

4. favorite_canonical_ids 를 templates context 에 주입
     → 템플릿이 채워진 별(★) / 빈 별(☆) 렌더
```

### 5.2 신규 헬퍼 시그니처 (subtask 00036-3 구현)

```python
def get_favorite_canonical_id_set(
    session: Session,
    user_id: int,
    canonical_ids: Iterable[int],
) -> set[int]:
    """user_id 가 즐겨찾기한 canonical_project_id 집합을 한 쿼리로 반환한다.

    favorite_entries 테이블에서 folder_id 를 통해 user_id 를 역조회하여
    canonical_ids 범위 내에서 즐겨찾기된 것만 반환한다.
    canonical_ids 가 비어 있으면 쿼리 없이 빈 set 반환.
    비로그인(user_id=None) 케이스는 호출 전에 skip 처리한다."""

def get_siblings_by_canonical_id_map(
    session: Session,
    canonical_ids: Iterable[int],
) -> dict[int, list[dict]]:
    """canonical_project_id → 동일 canonical group 소속 다른 announcement 목록 맵.

    각 announcement dict: {id, title, source_type, status, deadline_at,
                           collected_at, canonical_scheme}
    canonical_ids 가 비어 있으면 빈 dict 반환.
    N+1 방지: canonical_ids 집합을 IN 절로 한 쿼리 처리.
    canonical_scheme 은 announcements.canonical_key_scheme 컬럼값('official'/'fuzzy')."""
```

### 5.3 별 아이콘 UI 흐름

```
별 아이콘 클릭 (로그인 시만 표시)
  └─ event.stopPropagation()  ← row 클릭과 충돌 방지 필수
  └─ 폴더 선택 모달 열기
       ├─ GET /favorites/folders  → 폴더 트리 렌더
       ├─ 이미 즐겨찾기된 폴더: 체크 표시
       ├─ 폴더 선택 또는 "새 폴더 생성" 버튼
       └─ 저장 버튼

저장 버튼 클릭
  └─ POST /favorites/entries  {folder_id, canonical_project_id}
       ├─ 성공 → 모달 닫기 + 별 DOM 갱신 (채워진 별로)
       └─ 실패 → 모달 내 에러 메시지

별 아이콘 (이미 즐겨찾기된 canonical)
  └─ 채워진 별(★) 클릭 → 폴더 선택 모달 (즐겨찾기 제거 옵션 포함)
```

**canonical 단위 동기화**: 별 상태는 `canonical_project_id` 기준이므로
IRIS announcement에서 저장 → NTIS announcement의 별도 자동으로 채워진다.
페이지 로드 시 `get_favorite_canonical_id_set` 결과가 양쪽 모두에 적용되기 때문이다.

### 5.4 별 상태 표시 규칙

| 상태 | 아이콘 | 조건 |
|------|--------|------|
| 비로그인 | 숨김 | `current_user` 없음 |
| 미저장 | ☆ (빈 별) | canonical_project_id가 favorite_canonical_ids에 없음 |
| 저장됨 | ★ (채워진 별) | canonical_project_id가 favorite_canonical_ids에 있음 |
| canonical 없음 | 숨김 | canonical_group_id IS NULL |

---

## 6. 동일과제 expand UI 데이터 흐름

### 6.1 목록 페이지 expand 흐름

```
1. 기존 목록 조회 (기존 "N건" 배지 쿼리 재사용)
     canonical_group_id 가 있는 공고에 "동일 과제 N건" 배지 표시

2. (expand 클릭 시) JS가 GET /announcements/{id}/siblings 요청
     └─ 서버: get_siblings_by_canonical_id_map 로 해당 canonical 의 다른 공고 반환

3. 또는 (페이지 로드 시 batch 방식) 목록 조회와 함께 미리 siblings 맵을 주입
     - 현재 페이지 canonical_ids → get_siblings_by_canonical_id_map 한 번
     - 결과를 JSON으로 페이지에 embed (data-* 또는 <script type=\"application/json\"> 방식)
     - expand 클릭 시 서버 추가 요청 없이 즉시 렌더 (N+1 방지)
```

**권장: 페이지 로드 시 batch 사전 주입 방식**. 추가 HTTP 요청 없이 expand가 즉시 열린다.
`<script type="application/json" id="siblings-data">` 블록으로 embed. Jinja2 태그 리터럴 삽입 금지.

### 6.2 expand row 렌더 구조

```
[동일 과제 3건 ▼] 배지 클릭
  └─ row 바로 아래 inline expand 패널 표시
       ┌──────────────────────────────────────────────┐
       │  제목          소스    상태    마감일   수집시점   매칭 근거  │
       │  AI 과제 공고   NTIS    접수중  2026-05  2026-04   [공식]     │
       │  AI 과제 재공고 IRIS    마감    2025-12  2025-11   [공식]     │
       └──────────────────────────────────────────────┘
```

### 6.3 매칭 근거 배지 규칙

| 배지 | 조건 | 의미 |
|------|------|------|
| `[공식]` | `announcements.canonical_key_scheme == 'official'` | 공고번호(ancmNo) 기반 공식 키 일치 |
| `[유사]` | `announcements.canonical_key_scheme == 'fuzzy'` | 제목·기관·연도 조합 fuzzy 매칭 |

`canonical_key_scheme` 은 `announcements` 테이블의 비정규화 복사 컬럼이다
(`app/db/models.py:canonical_key_scheme`).

### 6.4 상세 페이지 "동일 과제" 섹션

상세 페이지 하단에 고정 섹션으로 표시한다. 비로그인 포함 항상 노출.

```
=== 동일 과제 ===

같은 canonical group 에 속한 다른 공고 목록:

[제목]           [소스]  [상태]  [마감일]  [수집시점]  [매칭 근거]
AI 과제 NTIS판   NTIS    접수중  2026-05  2026-04    [공식]        → 클릭 시 해당 상세 이동
AI 과제 재공고   IRIS    마감    2025-12  2025-11    [공식]        → 클릭 시 해당 상세 이동
```

자기 자신(현재 보는 announcement)은 목록에서 제외한다.
canonical_group_id가 없는 공고에서는 이 섹션을 표시하지 않는다.

---

## 7. 즐겨찾기 전용 탭 페이지 (/favorites)

### 7.1 레이아웃

```
┌──────────────────────────────────────────────────────────────┐
│ 네비바: [공고 목록] [즐겨찾기★ ← 로그인 시만] [수집 제어] [로그인/로그아웃] │
└──────────────────────────────────────────────────────────────┘
┌───────────────────┬──────────────────────────────────────────┐
│  폴더 트리 (좌)    │  공고 목록 (우)                           │
│                   │                                          │
│  + 그룹 추가      │  [기존 목록 UI 재사용 + "즐겨찾기 제거" 버튼]│
│                   │                                          │
│  ▼ AI 과제 (루트) │  제목 | 기관 | 상태 | 마감일 | 소스 | 관련성 │
│    머신러닝        │  ...                                     │
│    딥러닝          │                  [즐겨찾기 제거]          │
│  ▶ 정부정책 (루트) │                                          │
│                   │                                          │
│  + 서브그룹 추가   │  (서브그룹 선택 시 비활성)                │
└───────────────────┴──────────────────────────────────────────┘
```

### 7.2 폴더 트리 CRUD UI 흐름

**그룹 추가 (루트 생성)**:
```
"+ 그룹 추가" 버튼 클릭
  └─ 인라인 이름 입력 또는 모달
  └─ POST /favorites/folders {name, parent_id: null}
  └─ 성공 → 트리 갱신
```

**서브그룹 추가 (자식 생성)**:
```
루트 폴더 선택 상태에서만 활성화
서브그룹 선택 시 "서브그룹 추가" 버튼 비활성화 (depth 1은 자식 불가)

"+ 서브그룹 추가" 클릭
  └─ POST /favorites/folders {name, parent_id: 선택된_루트_id}
  └─ depth 초과 시 400 → 버튼은 원래 비활성이므로 방어 처리
```

**이름 변경**:
```
폴더 더블클릭 또는 컨텍스트 메뉴 → 인라인 편집
PATCH /favorites/folders/{id} {name}
성공 → 트리 내 이름 갱신
```

**삭제**:
```
삭제 버튼 클릭
  └─ 확인 모달: "폴더와 하위 항목이 모두 삭제됩니다. 계속하시겠습니까?"
  └─ 확인 → DELETE /favorites/folders/{id}
  └─ cascade: 하위 폴더 + FavoriteEntry 모두 DB에서 자동 삭제
  └─ 성공 → 트리 갱신 + 우측 목록 초기화
```

### 7.3 우측 목록에서 즐겨찾기 제거

```
폴더 클릭 → 우측에 해당 폴더의 공고 목록 표시 (기존 목록 UI 재사용)
각 row: 기존 컬럼 (제목/기관/상태/마감일/소스/관련성) + "제거" 버튼

"제거" 버튼 클릭
  └─ event.stopPropagation()  ← row 클릭 방지
  └─ DELETE /favorites/entries/{entry_id}
  └─ 성공 → 해당 row DOM 제거
```

---

## 8. 쿼리 최적화 요약

| 추가 쿼리 | 조건 | 횟수 |
|-----------|------|------|
| `get_favorite_canonical_id_set` | 로그인 + canonical_ids 있을 때 | 1 |
| `get_siblings_by_canonical_id_map` | 목록에 canonical_ids 있을 때 | 1 |
| 합계 (로그인 기준 기존 대비) | Phase 3a (+2) 기반 추가 | **+2** |

비로그인 시: `get_favorite_canonical_id_set` skip. siblings 맵은 expand용이므로 유지.

**N+1 방지 원칙**:
- 별 상태: 페이지 단위 IN 쿼리 1회 (Phase 1b `get_read_announcement_id_set` 패턴 동형)
- siblings: 페이지 단위 canonical_ids IN 쿼리 1회로 전체 맵 수집
- 폴더 트리: 루트+자식 한 쿼리 (user_id 필터 단순 SELECT)

---

## 9. 한계 및 known concerns

### 9.1 canonical 묶음 품질 (false-positive)

`canonical_key_scheme='fuzzy'` 로 묶인 그룹에는 실제로 다른 과제임에도
제목·기관·연도 유사도로 잘못 묶인 false-positive가 존재할 수 있다.

감사 결과는 [docs/canonical_grouping_audit_00036.md](canonical_grouping_audit_00036.md) 참조.
false-positive 해소(merge/split)는 Phase 5 `canonical_overrides` 범위이며,
현재 Phase 3b에서는 **읽기 전용 표시**만 한다.
이 한계는 동일과제 expand UI의 `[유사]` 배지로 사용자에게 명시한다.

### 9.2 루트 폴더 동명 허용

SQLite/Postgres 모두 UNIQUE 제약에서 `parent_id IS NULL` 인 루트 폴더끼리
동명을 허용한다. `uq_favorite_folders_user_parent_name` 은 NULL을 "서로 다름"으로
취급하기 때문이다.
app 레벨 중복 검사(Phase 1b 보강 예정)가 없으면 같은 이름의 루트 폴더가 여러 개
생성될 수 있다. Phase 3b 구현에서 POST /favorites/folders 에 루트 동명 app-check를
포함한다.

### 9.3 즐겨찾기 공고의 내용 변경

`favorite_entries`는 `announcements.canonical_group_id` 변경(리셋)에 영향받지 않는다.
공고 내용이 바뀌어도 즐겨찾기는 그대로 유지된다.
우측 목록에서 표시되는 announcement는 `is_current=True` 행이므로
최신 내용으로 자동 갱신된다.

---

## 10. 주의사항 (전 subtask 공통)

1. **시간 비교**: `_as_utc()` 함수 (`app/db/models.py`) 사용. SQLite는 `DateTime(timezone=True)` 컬럼을 SELECT 시 naive datetime으로 반환하므로, tz-aware 값과 비교 전 반드시 `_as_utc(value)` 적용.

2. **Jinja2 `<script>` 블록 내 Jinja 태그 리터럴 삽입 금지** (Task 00031 선례). JS에서 서버 값이 필요할 때는 `data-*` attribute 또는 `<script type="application/json" id="page-data">` 블록을 사용한다.

3. **별 클릭 ↔ row 클릭 충돌**: 별 아이콘 클릭 핸들러에 `event.stopPropagation()` 필수. "즐겨찾기 제거" 버튼도 동일.

4. **depth 2 초과 UI 비활성**: 서브그룹 선택 시 "서브그룹 추가" 버튼을 disabled로 설정한다. API 레이어에도 400 방어선이 있지만 UI에서 미리 막는다.

5. **비로그인**: 별 아이콘 및 즐겨찾기 탭은 로그인 시에만 표시. 동일과제 expand 배지와 상세 섹션은 비로그인에서도 노출한다.

---

## 11. 후속 subtask 의존 순서

```
00036-1 (본 문서 — 설계 확정)
    ↓
00036-2  canonical 묶음 품질 감사 (readonly) → docs/canonical_grouping_audit_00036.md 생성
    ↓
00036-3  FavoriteEntry ORM + depth 2 validator 재정리 + repository 헬퍼 + unit 테스트
    ↓
00036-4  /favorites/folders, /favorites/entries API 라우트 + 권한·Origin·에러 매핑
    ↓
00036-5  목록 동일과제 inline expand + 상세 "동일 과제" 섹션 (비로그인 포함)
    ↓
00036-6  목록·상세 별 아이콘 + 폴더 선택 모달(트리 + 새 폴더 생성) + canonical 단위 상태 동기화
    ↓
00036-7  /favorites 전용 탭 페이지 — 좌 폴더 트리(CRUD) + 우 공고 목록
    ↓
00036-8  README.USER.md 즐겨찾기/동일과제 사용법 + 10개 검증 시나리오 통합 확인
```

---

## 12. 검증 시나리오 → subtask 매핑

| # | 시나리오 | 담당 subtask |
|---|----------|-------------|
| 1 | 별 → 폴더 선택 → 저장 → 즐겨찾기 탭 확인 | 00036-6 (모달) + 00036-7 (탭) |
| 2 | 같은 canonical 을 IRIS 에서 저장 → NTIS 에서 채워진 별 | 00036-6 |
| 3 | depth 2 초과 시도 → 400, UI 비활성 | 00036-4 (API) + 00036-7 (UI 비활성) |
| 4 | 폴더 삭제 cascade (모달 확인) | 00036-7 |
| 5 | 다른 사용자 폴더 id → 404 | 00036-4 |
| 6 | 목록 "N건" 클릭 → expand | 00036-5 |
| 7 | 상세 "동일 과제" 섹션 + 매칭 근거 | 00036-5 |
| 8 | 비로그인 시 즐겨찾기 탭/별 숨김, expand/섹션은 보임 | 00036-5 + 00036-7 |
| 9 | 공고 내용 변경 후 FavoriteEntry 유지 (Phase 1a 리셋 제외 회귀) | 00036-8 |
| 10 | N+1 회귀 없음 | 00036-3 (쿼리) + 00036-8 (검증) |
