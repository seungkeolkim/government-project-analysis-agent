# Task 00085 — 조직 단위 관련성 판정 설계

> **작성 범위**: Task 00085 subtask 00085-1 — `RelevanceJudgment` /
> `RelevanceJudgmentHistory` 에 `organization_id` 컬럼을 추가해 "조직 입장의 판정" 을
> 표현하기 위한 데이터 모델·UI·migration 설계 정리. 본 subtask 는 코드 변경 0,
> 문서만 작성한다. 실제 구현은 후속 subtask 00085-2 ~ 00085-6 가 수행한다.
>
> 참고 문서:
> - [docs/relevance_ui_design.md](relevance_ui_design.md) — 기존 관련성 배지/툴팁/모달
>   데이터 흐름. 본 문서는 그 위에 "조직 단위 row" 와 "다른 사용자·조직 카운터" 를
>   얹는 형태로 변경한다.
> - [docs/schema_phase1a.md](schema_phase1a.md) §2.4 / §2.5 — `relevance_judgments` /
>   `relevance_judgment_history` 의 기존 스키마.
> - [docs/db_portability.md](db_portability.md) §4 — Alembic migration 의
>   `batch_alter_table` + constraint 이름 명시 + downgrade 양방향 구현 규칙.
> - [docs/canonical_identity_design.md](canonical_identity_design.md) — canonical_project
>   단위 처리 컨벤션.

---

## 1. 결정 요약 (사용자 modify 턴 사전 확정)

본 설계의 핵심 결정 4 가지는 사용자 원문 modify 턴 (2026-05-07T07:13:11+00:00) 에서
**사실로 확정** 되었다. 본 문서와 후속 subtask 는 이 4 결정을 흔들지 않는다.

| # | 항목 | 결정 |
|---|------|------|
| 1 | UNIQUE 제약 | **안 1 (단일 UNIQUE)** — `(canonical_project_id, user_id, organization_id)`. partial unique index 사용 안 함. |
| 2 | 본인 큰 배지 우선순위 | **개인 우선** — 본인 개인 row 가 있으면 그 verdict 를 큰 배지로. 없으면 본인이 만든 조직 row 중 `decided_at` 최신 1 개. |
| 3 | 비로그인 노출 범위 | **로그인과 동일 노출** — 다른 사용자·조직의 작성자명·조직명·verdict·시점·사유 모두 표시. 본인 영역 (작성/수정/삭제 UI) 만 비활성. |
| 4 | 카운터 집계 | **관련/무관/미검토 분리** (✅ N ❌ M ❓ K). 본인 row (개인 + 본인이 만든 조직 row) 는 카운터에서 제외. 같은 조직에 다른 사용자가 만든 row 는 각각 1 카운트. |

이 결정의 함의로, modify 턴이 명시한 다음 항목은 **plan 에서 제거**되어 있다 —
본 문서도 이 항목들을 다루지 않는다.

- 같은 조직 row 충돌 응답 (409/422 + "이미 작성자 X" 안내) — 안 1 에서는 충돌 자체가
  발생할 수 없다.
- 조직 row ownership transfer — 안 1 에서는 row 작성자가 곧 ownership 이며 다른
  멤버가 같은 조직 row 를 또 만들 수 있다 (서로 다른 `user_id` 라 UNIQUE 비충돌).
- 사용자 원문 검증 5번 (같은 조직 row 충돌 시나리오) — 후속 검증 subtask (00085-6)
  에서도 검증 항목 1·2·3·4·6~14 만 다룬다.

---

## 2. 안 1 의 데이터 모델 의미

### 2.1 `organization_id` 컬럼의 의미 재정의

`organization_id` 는 "이 사용자가 어떤 조직 입장으로 한 판정인지" 라는 **메타 정보**
이다. "조직 1 개당 평가 1 개" (안 2 의 의도) 가 아니다.

- `organization_id IS NULL` → 사용자 본인 명의 (개인) 의 판정.
- `organization_id IS NOT NULL` → 해당 조직 입장으로 표명한 판정. 작성자는
  `user_id` 본인이며, 같은 조직 안의 다른 멤버가 같은 canonical 에 다른 판정을
  표명할 수 있다 (`user_id` 가 달라 UNIQUE 비충돌).

같은 조직 안에서 의견이 분기할 수 있고, 그 분기가 그대로 데이터에 표현된다.
"조직 의견이 분산된다" 는 본 안의 단점이 아니라 **본 안이 표현하려는 사실** 이다.

### 2.2 본인이 같은 canonical 에 가질 수 있는 row 조합

안 1 에서 한 명의 사용자는 같은 canonical 에 대해 다음 조합의 row 를 가질 수 있다.

| 조합 | 가능 여부 | 비고 |
|------|-----------|------|
| 개인 row 1 개 (`organization_id IS NULL`) | O | UNIQUE `(canonical, user, NULL)` 1 슬롯 |
| 본인이 소속된 조직 A 의 row 1 개 | O | UNIQUE `(canonical, user, A)` 1 슬롯 |
| 본인이 소속된 조직 A 의 row 2 개 이상 | X | 같은 키 충돌 |
| 본인이 소속된 조직 A + B 의 row 각 1 개 (복수 조직 소속) | O | 서로 다른 키 |
| 본인 개인 + 본인 조직 A + 본인 조직 B 모두 | O | 서로 다른 키. 후속 UI 가 처리해야 할 케이스. |

같은 조직의 다른 멤버가 같은 canonical 에 만든 row 도 **본인 row 와는 독립** 이다
(`user_id` 가 다르므로 UNIQUE 비충돌). 즉 "조직 A 의 row" 는 작성자 별로 여러 row 가
공존할 수 있다.

### 2.3 권한 정책 (변경 없음 — 사용자 원문 그대로 유지)

| 행위 | 허용 조건 |
|------|-----------|
| 개인 판정 작성/수정/삭제 | 본인 row 본인이 작성·수정·삭제 (기존 동일) |
| 조직 판정 작성 | 본인이 해당 조직에 소속되어 있어야 함. 서버측 `user_organizations` 검증 + UI 비활성. 무소속 사용자는 라디오 자체 비활성 + 서버 자동 거부. |
| 조직 판정 수정·삭제 | row 작성자 본인만. 같은 조직의 다른 멤버여도 본인이 만든 row 가 아니면 **403** (ownership transfer 없음). |

검증 위반 응답 매핑 (후속 API subtask 00085-4 가 따른다):

| 위반 | HTTP status |
|------|-------------|
| 비로그인 POST/DELETE | 401 |
| 본인 소속 외 조직 id 로 작성 시도 | 422 |
| 다른 사용자가 만든 row 를 수정·삭제 시도 | 403 |
| 모르는 organization_id (DB FK 위반) | 422 |
| canonical_project 없음 | 404 |

---

## 3. 표시 정책 (큰 배지 / hover 툴팁 / 카운터 / 상세 페이지 행)

### 3.1 케이스별 표시 정책 표

`MINE` = 본인이 작성한 row (개인 row 또는 본인이 만든 조직 row).
`OTHERS` = 본인이 작성하지 않은 모든 row (다른 사용자의 개인 row + 다른 사용자의 조직 row).

| 케이스 | 큰 배지 | hover 툴팁 (목록 셀) | 카운터 (✅ N ❌ M ❓ K) | 상세 페이지 행 |
|--------|---------|----------------------|--------------------------|----------------|
| 본인 개인 row 만 있음 | 개인 row 의 verdict | "(내 개인 판정 verdict / decided_at / reason)" | OTHERS 만 카운트 | 본인 영역 1 행 + OTHERS 각 행 |
| 본인 개인 row 없고 본인 조직 row 1 개 | 그 조직 row 의 verdict | "조직명 — verdict — decided_at — reason" | OTHERS 만 카운트 | 본인 영역 1 행 + OTHERS 각 행 |
| 본인 개인 row 없고 본인 조직 row 2 개 이상 | 본인 조직 row 중 `decided_at` **최신 1 개** 의 verdict | 본인 조직 row **전체** 를 최신순 나열 (조직명 + verdict + decided_at) | OTHERS 만 카운트 | 본인 영역에 row 별 행 + OTHERS 각 행 |
| 본인 개인 row + 본인 조직 row 다수 | **개인 row 의 verdict 우선** | 개인 row 표시 + 본인 조직 row 도 최신순 나열 (참고용) | OTHERS 만 카운트 | 본인 영역에 row 별 행 (개인 1 + 조직 N) + OTHERS 각 행 |
| 본인 row 가 하나도 없고 OTHERS 만 있음 | 미검토 (빈 배지) | "(다른 사용자 / 조직 평가 N건)" 요약 | OTHERS 만 카운트 | 본인 영역 비어 있음 (작성 버튼만) + OTHERS 각 행 |
| 비로그인 + OTHERS 만 있음 | 미검토 (빈 배지) | OTHERS 요약 (작성자명·조직명 그대로 노출) | OTHERS 만 카운트 (= 결정 4) | 본인 영역 자리 자체 미표시 또는 "로그인 후 작성" 안내. OTHERS 각 행은 동일 노출. |
| 비로그인 + 아무 row 없음 | 미검토 (빈 배지) | (없음) | 0/0/0 | 본인 영역 미표시. |

### 3.2 큰 배지 우선순위 의사코드 (개인 우선)

```
def pick_primary_verdict(my_personal, my_org_rows_desc_by_decided_at):
    # my_personal: RelevanceJudgment | None  (organization_id IS NULL & user_id == me)
    # my_org_rows_desc_by_decided_at: list[RelevanceJudgment]
    #   (organization_id IS NOT NULL & user_id == me, decided_at DESC)
    if my_personal is not None:
        return my_personal.verdict, "personal"
    if my_org_rows_desc_by_decided_at:
        return my_org_rows_desc_by_decided_at[0].verdict, "organization"
    return None, "none"  # 미검토
```

큰 배지 색상 매핑 (기존 `relevance_ui_design.md` §2.3 그대로 유지):

| 상태 | 배지 | 색상 |
|------|------|------|
| `verdict='관련'` | 관련 | 초록 (#16a34a) |
| `verdict='무관'` | 무관 | 회색 (#6b7280) |
| 본인 row 없음 (개인·조직 모두 0) | 빈 배지 (테두리만) | 연한 회색 테두리 |

### 3.3 카운터 집계 정의 (안 1 의 자연스러운 동작)

`my_user_id` 기준으로 `relevance_judgments` 의 row 를 다음과 같이 분류한다.

```
mine_rows  = [r for r in rows if r.user_id == my_user_id]
other_rows = [r for r in rows if r.user_id != my_user_id]
counter_relevant   = sum(1 for r in other_rows if r.verdict == '관련')
counter_irrelevant = sum(1 for r in other_rows if r.verdict == '무관')
counter_unreviewed = (canonical 의 다른 활성 사용자 수 - 본인 외에 row 가 있는 user_id 수) ??
```

> **`❓` (미검토) 카운터의 정의 — 후속 repository subtask 가 결정** :
> 본 문서에서는 두 후보를 명시하고, 구현 시 (1) 의 의미를 채택한다고 본다.
> (2) 는 "활성 사용자 수" 라는 추가 의존이 생겨 단가가 비싸기 때문이다.
>
> 1. **(채택)** OTHERS 가 표명한 판정 중 verdict 가 `'관련'/'무관'` 외의 값일 때 카운트.
>    DB CHECK 가 `'관련','무관'` 만 허용하므로 사실상 항상 0. 카운터 표기는 ✅ N ❌ M
>    형태가 되며 ❓ 부분은 OTHERS 가 row 를 *아예 만들지 않은* 의미가 아니라 표시
>    여백을 위한 0 자리 노출이다. UI 가 ❓ 0 을 보여줄지 숨길지는 UI subtask 가 결정.
> 2. (대안 — 구현 비용으로 미채택) `canonical` 단위에서 row 를 만들지 않은 사용자 수.
>    "활성 사용자" 의 정의가 모호하고 (예: 탈퇴 / 휴면 / 신규 가입 모두 포함?), 추가
>    쿼리가 필요하다.

**같은 조직 다중 사용자 row** 처리: 안 1 에서는 같은 조직에 대해 user_A 가 '관련',
user_B 가 '무관' 처럼 row 가 분기될 수 있는데, 카운터는 row 단위로 각각 1 씩 더한다
(✅ +1 그리고 ❌ +1). "조직 1 개 = 1 표" 로 정규화하지 **않는다** (안 2 의 의도였음).

### 3.4 카운터 인라인 / expand 동작 (UI subtask 0085-5 가 구현)

목록 셀의 폭 여유 기준은 기존 `relevance_ui_design.md` 와 동일하게 유지한다.

- 폭 여유 (`>= 120px`) → 카운터를 인라인 직접 노출 (`✅ 3 ❌ 1 ❓ 0`).
- 폭 부족 → 큰 배지 옆에 "···" 또는 (3+1+0) 같은 합산 chip 으로 줄이고 클릭 시 모달
  expand 로 카운터 분리 표기.

hover 시 본인 소속 조직 평가 툴팁은 §3.1 표의 "hover 툴팁" 컬럼대로 동작한다 —
"본인이 만든 조직 row" 만 나열한다. OTHERS 의 조직 row 는 hover 에 노출하지 않으며,
상세 페이지에서 풀어 본다.

### 3.5 상세 페이지 (`detail.html`) 관련성 섹션 행 구성

다음 3 그룹을 시각적으로 구분해 표시한다 (예: 섹션 제목 또는 구분선).

```
[ 본인 판정 ]
  - 개인       — verdict / decided_at / reason            [수정] [삭제]
  - 조직 A     — verdict / decided_at / reason            [수정] [삭제]
  - 조직 B     — verdict / decided_at / reason            [수정] [삭제]

[ 다른 사용자·조직 판정 ]
  - 작성자: alice (개인)         — 관련 / 2026-05-01 14:00 / "기관 전략 부합"
  - 작성자: alice (조직: 본부)   — 무관 / 2026-04-29 10:00 / ""
  - 작성자: bob   (조직: 본부)   — 관련 / 2026-04-30 09:00 / ""
```

- 본인 영역의 각 행에만 `[수정] [삭제]` 버튼이 있다 (row 작성자 본인 = 로그인 사용자).
- "다른 사용자·조직 판정" 영역은 비로그인 시에도 동일하게 표시된다 (결정 3).
- 같은 조직 (예: "본부") 입장으로 alice 와 bob 이 각각 row 를 만든 케이스는 두 행으로
  풀어서 표시한다 — 안 1 의 자연스러운 표현.

---

## 4. 입력 모달 와이어프레임

기존 `_relevance_modal.html` 의 verdict 라디오 / reason textarea / 저장 / 취소 버튼은
유지하고, 그 위에 **'판정 주체'** 라디오를 추가한다. 조직 드롭다운은 라디오 선택에
따라 동적으로 표시한다.

### 4.1 와이어프레임 — 무소속 사용자 (조직 0 개 소속)

```
┌──────────────────────────── 관련성 판정 ────────────────────────────┐
│                                                                      │
│  판정 주체:                                                          │
│   (●) 개인      ( ) 조직 [비활성 — 소속 조직 없음]                    │
│                                                                      │
│  판정:    ( ) 관련    ( ) 무관                                        │
│                                                                      │
│  사유 (선택):                                                        │
│   ┌────────────────────────────────────────────────────────────┐     │
│   │                                                            │     │
│   └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│   [ 저장 ]    [ 판정 취소 ]    [ 닫기 ]                               │
└──────────────────────────────────────────────────────────────────────┘
```

- '조직' 라디오 자체가 `disabled` (회색 처리) + tooltip "소속된 조직이 없습니다".
- 서버에서도 `organization_id` 가 본인 소속이 아니면 422 응답.

### 4.2 와이어프레임 — 단일 조직 소속 (예: "본부")

```
┌──────────────────────────── 관련성 판정 ────────────────────────────┐
│                                                                      │
│  판정 주체:                                                          │
│   (●) 개인      ( ) 조직: 본부                                       │
│                                                                      │
│  판정:    ( ) 관련    ( ) 무관                                        │
│                                                                      │
│  사유 (선택):                                                        │
│   ┌────────────────────────────────────────────────────────────┐     │
│   │                                                            │     │
│   └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│   [ 저장 ]    [ 판정 취소 ]    [ 닫기 ]                               │
└──────────────────────────────────────────────────────────────────────┘
```

- '조직' 옵션 라벨에 조직명을 직접 노출 (`조직: 본부`). 드롭다운은 표시하지 않음.
- 라디오를 '조직' 으로 옮기면 hidden field `organization_id=<본부 id>` 가 자동 세팅.

### 4.3 와이어프레임 — 복수 조직 소속 (예: "본부", "AI팀")

```
┌──────────────────────────── 관련성 판정 ────────────────────────────┐
│                                                                      │
│  판정 주체:                                                          │
│   (●) 개인      ( ) 조직                                              │
│                                                                      │
│   ↳ 조직 선택:  [ 본부          ▼ ]                                   │
│                  └────────────────────                               │
│                  │ 본부                                              │
│                  │ AI팀                                              │
│                                                                      │
│  판정:    ( ) 관련    ( ) 무관                                        │
│                                                                      │
│  사유 (선택):                                                        │
│   ┌────────────────────────────────────────────────────────────┐     │
│   │                                                            │     │
│   └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│   [ 저장 ]    [ 판정 취소 ]    [ 닫기 ]                               │
└──────────────────────────────────────────────────────────────────────┘
```

- '조직' 라디오 선택 시에만 조직 드롭다운(`<select>`) 이 표시된다.
- 드롭다운 옵션은 `current_user.user_organizations` 에서 가져온다 (서버측 컨텍스트
  주입). 첫 옵션은 placeholder ("조직 선택...") 가 아니라 default 로 첫 조직을 선택해
  사용자가 라디오 토글만으로 곧바로 저장 가능하도록 한다.
- 본인이 만든 기존 row 가 있는 canonical 에서 모달을 열면 — 해당 row 의
  `organization_id` 가 사전 선택된 상태로 모달이 뜨고 (수정 모드), `[판정 취소]`
  버튼이 활성화된다.

### 4.4 모달 ↔ 엔드포인트 매핑 (참고)

후속 API subtask (00085-4) 가 다음 형태로 구현한다 — 본 설계 문서는 인터페이스만
선언하고, 충돌 응답 로직은 두지 않는다 (안 1 채택으로 불필요).

```
POST /canonical/{id}/relevance
Body: { "verdict": "관련"|"무관", "reason": "...", "organization_id": <int|null> }
  - organization_id: NULL 이면 개인 판정. 정수면 본인 소속 조직 검증 후 INSERT/이관.
  - UNIQUE 키 (canonical, user, organization_id) 단일이라 충돌 응답 로직 없음 —
    같은 키 row 가 이미 있으면 기존 row 를 History 이관 후 새 row 로 교체.

DELETE /canonical/{id}/relevance
Body: { "organization_id": <int|null> }
  - organization_id 로 어떤 row 를 지울지 지정. NULL = 본인 개인 row.
  - 작성자 본인이 아닌 row 를 지정하면 → 403 (안 1 의 ownership 정책).

GET /canonical/{id}/relevance/history
  - 비로그인 허용 (결정 3). 개인 + 조직 row 모두 응답. organization_id 도 응답에 포함.
```

---

## 5. Migration 영향 범위 + 기존 row 호환성

### 5.1 변경되는 DDL

migration 파일 1 개로 다음 변경을 한 트랜잭션에 묶는다 (subtask 00085-2 구현).

#### `relevance_judgments` 변경

- 컬럼 추가: `organization_id INTEGER NULL`
- FK 추가: `fk_relevance_judgments_organization_id (organization_id) → organizations(id) ON DELETE CASCADE`
- 기존 UNIQUE 제거: `uq_relevance_project_user (canonical_project_id, user_id)`
- 신규 UNIQUE 추가: `uq_relevance_judgments_canonical_user_org (canonical_project_id, user_id, organization_id)`
- 인덱스 추가: `ix_relevance_judgments_organization_id (organization_id)` — 조직별 판정
  나열 / 조직 삭제 시 CASCADE 효율을 위해.

#### `relevance_judgment_history` 변경

- 컬럼 추가: `organization_id INTEGER NULL`
- FK 추가: `fk_relevance_judgment_history_organization_id (organization_id) → organizations(id) ON DELETE CASCADE`
- 인덱스 추가: `ix_relevance_judgment_history_organization_id (organization_id)`
- History 의 UNIQUE 는 원래도 없으므로 제약 변경 없음. content_changed reset 시 row
  를 그대로 복사할 수 있도록 컬럼만 추가한다.

> 주의 — **CASCADE 정책**: 조직 삭제 시 관련 판정 row 도 같이 사라진다. 조직 삭제는
> `app/organizations/service.py` 의 `delete_organization()` 이 자식 조직 존재 여부를
> 사전 체크하지만 (UI), DB 레벨 안전망으로는 CASCADE 가 합리적이다 (기존
> `user_organizations` 도 동일 정책). 본 설계는 CASCADE 를 채택한다 — 조직이 사라지면
> "그 조직 입장의 판정" 도 의미를 잃기 때문.

### 5.2 Constraint / 인덱스 이름 (Postgres 호환 명시)

| 위치 | 종류 | 이름 |
|------|------|------|
| `relevance_judgments` | UNIQUE (제거) | `uq_relevance_project_user` ← 기존 |
| `relevance_judgments` | UNIQUE (신규) | `uq_relevance_judgments_canonical_user_org` |
| `relevance_judgments` | FK (신규) | `fk_relevance_judgments_organization_id` |
| `relevance_judgments` | INDEX (신규) | `ix_relevance_judgments_organization_id` |
| `relevance_judgment_history` | FK (신규) | `fk_relevance_judgment_history_organization_id` |
| `relevance_judgment_history` | INDEX (신규) | `ix_relevance_judgment_history_organization_id` |

> 기존 UNIQUE 이름은 schema 1a 시점에 `uq_relevance_project_user` 로 짧게 명명되어
> 있다 (`app/db/models.py:837`). schema_phase1a.md 의 명명 규칙 (`uq_{table}_{컬럼}`)
> 과 약간 다르나 **기존 이름은 유지** 하고 (변경 시 SQLite 환경 stamped DB 가 깨질
> 위험 회피), 신규 UNIQUE 이름만 표준 규칙에 맞춰 `uq_relevance_judgments_canonical_user_org`
> 로 부여한다. 후속 migration subtask 가 그대로 따른다.

### 5.3 batch_alter_table 절차 (`docs/db_portability.md` §4 인용)

SQLite 는 `ALTER TABLE DROP CONSTRAINT` 를 직접 지원하지 않으므로 모든 DDL 변경을
`op.batch_alter_table` 로 감싼다. 단일 UNIQUE 채택으로 partial unique index 검증 (기존
prompt 가 언급한 dialect 차이 검증) 은 **불필요** 해졌고, db_portability.md §4 의
일반 절차만 따르면 된다.

```python
# upgrade() 골격 (subtask 00085-2 가 구체화)
def upgrade() -> None:
    with op.batch_alter_table("relevance_judgments") as batch_op:
        batch_op.add_column(
            sa.Column("organization_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_relevance_judgments_organization_id",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint("uq_relevance_project_user", type_="unique")
        batch_op.create_unique_constraint(
            "uq_relevance_judgments_canonical_user_org",
            ["canonical_project_id", "user_id", "organization_id"],
        )
        batch_op.create_index(
            "ix_relevance_judgments_organization_id",
            ["organization_id"],
        )
    with op.batch_alter_table("relevance_judgment_history") as batch_op:
        batch_op.add_column(
            sa.Column("organization_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_relevance_judgment_history_organization_id",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_relevance_judgment_history_organization_id",
            ["organization_id"],
        )

def downgrade() -> None:
    # 신규 UNIQUE → 기존 UNIQUE 복원, 컬럼 / FK / INDEX 제거 (역순).
    ...  # 후속 subtask 가 구체화. partial index 가 없으므로 단순.
```

migration 검증 3 단계 (db_portability.md §4 인용):

1. **기존 SQLite (stamp 경로)** — 운영 DB 사본에 `init_db()` → `alembic_version` 만
   갱신, 데이터 무변경 확인. 신규 컬럼이 추가되고 기존 row 의 `organization_id`
   는 NULL 로 채워진다.
2. **빈 SQLite (baseline-bootstrap)** — 새 DB 에 `alembic upgrade head` → 신규 컬럼·UNIQUE·FK·INDEX 모두 생성됨.
3. **Postgres syntax 호환** — `op.batch_alter_table` 로 감싸고, partial index / dialect
   전용 표현 없음. constraint 이름 모두 명시. UNIQUE 의 NULL 동작은 SQLite·Postgres
   양쪽에서 "NULL 끼리는 서로 다른 값" 으로 동일하게 취급되어 동작이 일관 (안 1 의
   장점).

### 5.4 기존 row 호환성

migration 직후 기존 `relevance_judgments` / `relevance_judgment_history` row 는
`organization_id IS NULL` 로 자연 보존된다. 의미는 다음과 같다.

- 기존 모든 row = "본인 명의 (개인) 판정" — 사용자 의도와 일치.
- 신규 UNIQUE `(canonical, user, organization_id)` 도 즉시 만족 (기존
  `(canonical, user)` UNIQUE 의 강한 제약을 그대로 흡수, NULL 까지 포함해 같은
  키 충돌 없음).
- 후속 repository 의 `set_relevance_judgment` 시그니처에 `organization_id=None` 기본값
  을 두면 기존 호출자는 코드 수정 없이도 그대로 동작 가능 (00085-3 가 결정).

migration 으로 인한 데이터 손실 / 의미 왜곡 / 추가 backfill 모두 **없음**.

---

## 6. content_changed reset 회귀 (Phase 1a 변경 감지)

`canonical_projects` 의 내용 변경 (status 단독 제외) 감지 시 해당 canonical 의 모든
`relevance_judgments` row 가 `relevance_judgment_history` 로 이관된다 (Phase 1a §9).
새 컬럼 `organization_id` 도 history 에 그대로 복사되어야 한다.

후속 repository subtask 의 회귀 테스트가 다음을 보장한다 (검증 13).

- `organization_id IS NULL` (개인) row 와 `organization_id IS NOT NULL` (조직) row 가
  동일 canonical 에 공존하는 상태에서 content_changed 가 발동 → 두 row 모두 history
  로 이관 + `organization_id` 값 정확히 복사.
- 이관 후 `relevance_judgments` 는 빈 상태가 되고 (해당 canonical 에 한해), UI 가
  미검토 빈 배지로 즉시 반영.

이관 로직 자체는 컬럼 한 개 추가만으로 자연스럽게 동작한다 (Phase 1a 의
`migrate_to_history(...)` 이 row 단위로 모든 컬럼을 복사). 후속 subtask 는 _코드 변경
없이_ 회귀 테스트 케이스만 추가하면 된다.

`as_utc` 헬퍼 사용 (`app/db/models.py:74`) 도 기존과 동일하게 유지 — `archived_at` /
`decided_at` 비교 시 tz-aware 통일.

---

## 7. 본 문서가 다루지 않는 항목 (다음 subtask 로 넘김)

본 설계는 후속 subtask 가 그대로 구현 근거로 사용한다. **본 문서가 정의하지 않은**
항목은 후속 subtask 구현 시점에 결정한다.

| 항목 | 위임 subtask |
|------|-------------|
| ORM 모델 컬럼 정의 / `__table_args__` 갱신 / `organization` relationship | 00085-2 |
| `set_relevance_judgment` / `delete_relevance_judgment` / `get_relevance_judgment` 시그니처에 `organization_id` 추가 | 00085-3 |
| 신규 헬퍼 `get_relevance_summary_by_canonical_id_map(user_id, canonical_ids)` 의 SQL 분기 (로그인 / 비로그인) | 00085-3 |
| API 라우터의 본인 소속 검증 / 작성자 본인만 수정·삭제 / 비로그인 GET history 허용 | 00085-4 |
| `_relevance_modal.html` 의 라디오 + 드롭다운 / `_relevance_badge_macro.html` 의 큰 배지·카운터·툴팁 / `detail.html` 행 풀어 표시 | 00085-5 |
| 검증 항목 1·2·3·4·6~14 회귀 테스트 + README.USER.md "관련성 판정 — 조직 단위 판정" 섹션 | 00085-6 |

---

## 8. 검증 시나리오 → subtask 매핑

사용자 원문 검증 14 항목 중 5번 (같은 조직 row 충돌) 은 안 1 채택으로 불가능 시나리오
가 되어 제거된다. 나머지 13 항목을 후속 subtask 에 매핑한다.

| # | 시나리오 | 담당 subtask |
|---|----------|-------------|
| 1 | 개인 판정 신규/수정/삭제 정상 | 00085-3 (repo) + 00085-4 (route) |
| 2 | 본인 소속 조직 판정 신규/수정/삭제 정상 | 00085-3 + 00085-4 |
| 3 | 본인 소속 외 조직 판정 시도 → 422 | 00085-4 |
| 4 | 무소속 사용자 조직 판정 시도 → UI 비활성 + 서버 거부 | 00085-4 + 00085-5 |
| ~~5~~ | ~~같은 조직 row 충돌~~ — 안 1 채택으로 제거 | — |
| 6 | 조직 동료가 만든 row 를 본인이 수정·삭제 시도 → 403 | 00085-4 |
| 7 | 본인 판정 변경 시 기존 row 가 History 로 이관 (`organization_id` 포함) | 00085-3 |
| 8 | 목록 셀 본인 배지 + 카운터 정상 렌더 | 00085-5 |
| 9 | hover 시 내 조직 평가 툴팁 표시 | 00085-5 |
| 10 | 비로그인 시 카운터 + OTHERS 정보 동일 노출, 본인 영역만 비활성 | 00085-5 + 00085-4 (history GET 허용) |
| 11 | 상세 페이지 모든 행 풀어 표시, 본인 row 만 수정·삭제 버튼 | 00085-5 |
| 12 | 입력 모달 '판정 주체' 라디오 — 무소속/단일/복수 조직 케이스 | 00085-5 |
| 13 | content_changed reset 시 `organization_id` 있는 row 도 History 이관 | 00085-3 (회귀 테스트) |
| 14 | N+1 회귀 없음 (페이지당 추가 쿼리 1~2 개로 고정) | 00085-3 + 00085-5 |

---

## 9. 주의사항 (사용자 원문 그대로 유지)

1. **canonical 단위** — 관련성 = canonical, 읽음 = announcement (PROJECT_NOTES 컨벤션
   유지).
2. **`as_utc()` 헬퍼 사용** — `archived_at` / `decided_at` 비교 시 tz-aware 통일.
   `datetime.utcnow()` (naive) 금지.
3. **Jinja2 `<script>` 블록 안 Jinja 태그 리터럴 금지** — 서버 값은 `data-*` attribute
   또는 `<script type="application/json">` 경유.
4. **`ensure_same_origin` + `current_user_required`** — 본인 영역의 모든 POST/DELETE
   에 유지. GET history 만 비로그인 허용 (결정 3).
5. **N+1 제거 헬퍼 패턴 유지** — `get_relevance_summary_by_canonical_id_map` 는 페이지
   1 회 호출로 본인 row + 본인 조직 row + OTHERS 카운터를 묶어서 반환한다.
6. **한국어 주석 / 변수·함수명 축약 금지** — 반년 뒤의 본인을 위한 가독성.

---

## 10. 후속 subtask 의존 순서

```
00085-1 (본 문서 — 설계 확정)
    ↓
00085-2  Alembic migration + ORM 모델 (organization_id 컬럼 + 신규 UNIQUE + FK + INDEX)
    ↓
00085-3  repository 헬퍼 (set/delete/get 시그니처 확장 + summary 헬퍼 신규 + content_changed reset 회귀)
    ↓
00085-4  API 라우터 (POST/DELETE/GET 에 organization_id + 본인 소속 검증 + 작성자 본인만)
    ↓
00085-5  UI (입력 모달 / 본인 큰 배지 개인 우선 / 카운터 / 상세 페이지 풀어 표시 / 비로그인 동일 노출)
    ↓
00085-6  회귀 검증 + tests/relevance/ 보강 + README.USER.md "관련성 판정 — 조직 단위 판정" 섹션 신설
```
