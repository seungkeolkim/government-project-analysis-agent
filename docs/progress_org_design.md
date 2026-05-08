# Task 00097 — 조직 단위 공고 진행 상태 / 선점 설계 (Phase C)

> **작성 범위**: Task 00097 subtask 00097-1 — `AnnouncementProgress` /
> `AnnouncementProgressHistory` 테이블 신설을 통한 "canonical 단위 조직별 진행 상태
> 표명·선점" 시스템의 데이터 모델·UI·migration 설계 정리. 본 subtask 는 코드 변경
> 0, 문서 작성 + README.USER.md cleanup 만 수행한다. 실제 구현은 후속 subtask
> 00097-2 ~ 00097-6 가 수행한다.
>
> 참고 문서:
> - [docs/relevance_org_design.md](relevance_org_design.md) — Phase B (조직 단위 관련성
>   판정) 의 단일 UNIQUE / 비로그인 동일 노출 / 무소속 작성 불가 / app-level 검증
>   패턴. 본 문서는 그 위에 "선점 제약 + 4단계 status enum + 조직 멤버 누구나 권한"
>   를 얹는 형태로 설계한다.
> - [docs/db_portability.md](db_portability.md) §3 §4 — Alembic migration 의
>   `batch_alter_table` + constraint 이름 명시 + downgrade 양방향 구현 + partial
>   unique index 회피 규칙.
> - [docs/canonical_identity_design.md](canonical_identity_design.md) — canonical_project
>   단위 처리 컨벤션 (관련성 = canonical, 읽음 = announcement 그대로 적용).
> - [docs/schema_phase1a.md](schema_phase1a.md) — `canonical_projects` /
>   `relevance_judgments` 등의 기존 스키마 패턴 (history 이관 컬럼 / FK ondelete /
>   UNIQUE 명명 규칙).

---

## 1. 결정 요약 (사용자 사전 확정 — 본 subtask 에서 흔들지 않음)

본 설계의 핵심 결정은 사용자 원문 prompt 의 "핵심 결정" 절에서 **사실로 확정** 된
항목이다. 본 문서와 후속 subtask 는 이 9 결정을 흔들지 않는다.

| # | 항목 | 결정 |
|---|------|------|
| 1 | 테이블 구조 | 신규 `AnnouncementProgress` + `AnnouncementProgressHistory` 2 개. `relevance_judgments` 와 별개 테이블. |
| 2 | 단위 | **canonical** (Phase B 컨벤션 동일) — `(canonical_project_id, organization_id)` 단일 UNIQUE. |
| 3 | status enum | 4단계 `'관심'` / `'검토'` / `'진행'` / `'종료'`. 한글 enum + `native_enum=False`. |
| 4 | 선점 제약 | `'진행'` 단계만 한 canonical 당 단일 조직 선점. `'관심'` / `'검토'` 는 여러 조직 동시 가능. |
| 5 | 전이 | 모든 4단계 사이 양방향 자유 전이 — 진행/종료 에서 롤백 가능 (실수 복구). |
| 6 | 자동 release | 자동 release / stale 처리 **없음**. 오프라인 협의로 해소. |
| 7 | history 이관 | Phase B `user_changed` 패턴 그대로. content_changed reset 시 `content_changed` 사유로 함께 이관. |
| 8 | 권한 | **조직 멤버 누구나** — Phase B 의 "작성자 본인 한정" 과 의도적으로 다름. row 작성자 무관, 같은 조직 멤버 누구나 작성·수정·삭제 가능. |
| 9 | 필터 UI | **다중 체크박스** (4 옵션) — 다중 선택 시 OR. URL `?progress=...` |

본 phase 의 **유일한 미결정 항목** 은 §4.3 의 "빈 셀 표시 (`'—'` vs 완전 빈 공간)" 이며,
본 문서 §4.3 에서 결론을 명시한다.

---

## 2. 데이터 모델

### 2.1 `announcement_progress` 컬럼 명세

| 컬럼 | 타입 | nullable | 비고 |
|------|------|----------|------|
| `id` | Integer PK autoincrement | NO | |
| `canonical_project_id` | Integer FK `canonical_projects.id` ON DELETE CASCADE | NO | |
| `organization_id` | Integer FK `organizations.id` ON DELETE CASCADE | NO | 무소속 row 자체 불가 — 작성 단계에서 422. |
| `status` | Enum(`'관심'`, `'검토'`, `'진행'`, `'종료'`, `name="announcement_progress_status"`, `native_enum=False`) | NO | DB CHECK 자동 추가 (db_portability §1 enum 한글값 보존). |
| `note` | Text | YES | 자유 메모. NULL = 미작성. |
| `created_by_user_id` | Integer FK `users.id` ON DELETE SET NULL | YES | "마지막 수정자" 메타. 권한 판정에는 사용하지 않음 (조직 멤버 누구나 정책 — §3). |
| `created_at` | DateTime(timezone=True) default `datetime.now(UTC)` | NO | 최초 INSERT 시점. |
| `updated_at` | DateTime(timezone=True) default·onupdate `datetime.now(UTC)` | NO | 마지막 UPDATE 시점. |

UNIQUE / INDEX:

| 종류 | 이름 | 컬럼 |
|------|------|------|
| UNIQUE | `uq_announcement_progress_canonical_org` | `(canonical_project_id, organization_id)` |
| INDEX | `ix_announcement_progress_canonical_id` | `(canonical_project_id)` |
| INDEX | `ix_announcement_progress_organization_id` | `(organization_id)` |
| INDEX | `ix_announcement_progress_status` | `(status)` — 진행 단계 SELECT 가 잦음 (선점 검증 + 필터). |

### 2.2 `announcement_progress_history` 컬럼 명세

`announcement_progress` 와 동일 컬럼 + 다음 추가:

| 컬럼 | 타입 | nullable | 비고 |
|------|------|----------|------|
| `archived_at` | DateTime(timezone=True) default `datetime.now(UTC)` | NO | history 로 이관된 시점. |
| `archive_reason` | Enum(`'user_changed'`, `'content_changed'`, `name="announcement_progress_archive_reason"`, `native_enum=False`) | NO | `user_changed` = 사용자가 status / note 를 바꿈. `content_changed` = canonical 내용 변경 감지 (Phase 1a §9) 로 인한 일괄 reset. |

UNIQUE 없음 (이력 누적). INDEX:

| 종류 | 이름 | 컬럼 |
|------|------|------|
| INDEX | `ix_announcement_progress_history_canonical_id` | `(canonical_project_id)` |
| INDEX | `ix_announcement_progress_history_organization_id` | `(organization_id)` |
| INDEX | `ix_announcement_progress_history_archived_at` | `(archived_at)` |

### 2.3 status enum 값

```python
ANNOUNCEMENT_PROGRESS_STATUSES = ("관심", "검토", "진행", "종료")
```

`native_enum=False` 로 SQLAlchemy 가 DB CHECK constraint 를 자동 추가한다 (Postgres
에서도 native enum type 을 만들지 않아 dialect 호환). 한글 enum 보존 컨벤션은
`PROJECT_NOTES.md` 의 한글 enum 보존 결정과 정합.

### 2.4 본인이 같은 canonical 에 가질 수 있는 row 조합

| 조합 | 가능 여부 | 비고 |
|------|-----------|------|
| 본인 소속 조직 A 의 row 1 개 | O | UNIQUE `(canonical, A)` 1 슬롯 |
| 본인 소속 조직 A 의 row 2 개 이상 | X | 같은 키 충돌 |
| 본인 소속 조직 A + B 의 row 각 1 개 (복수 조직 소속) | O | 서로 다른 키 |
| 본인 소속 외 조직 row 작성 | X | 권한 정책 §3 — 422 |
| 무소속 사용자가 임의의 조직 row 작성 | X | 권한 정책 §3 — 422 |

Phase B 와 다른 점: row 단위 키에 `user_id` 가 **없다**. 같은 조직에 대해 여러
사용자가 row 를 만들 수 없으며 (UNIQUE 충돌), 같은 조직 안의 누가 마지막 수정자
인지는 `created_by_user_id` 메타로만 보존한다. 즉 "조직 입장 = 1 row" 로 정규화된다.

이는 Phase B (조직 의견 분기를 row 분리로 표현) 와 의도적으로 다른 모델이다 — Phase
C 는 "공식 입장 표명" 의 의미가 강해 조직 내부 의견 분기를 row 로 표현하지 않고,
조직 안의 협의 결과를 한 row 에 반영한다.

---

## 3. 권한·전이 정책

### 3.1 권한 매트릭스

| 행위 | 허용 조건 |
|------|-----------|
| `POST /canonical/{id}/progress` (신규 row 작성) | 로그인 사용자가 body 의 `organization_id` 에 소속되어 있어야 함. 무소속이면 422. 작성자 = `created_by_user_id`. |
| `PATCH /canonical/{id}/progress/{progress_id}` (수정) | 본인이 row 의 `organization_id` 에 소속되어 있어야 함. **row 작성자 본인일 필요 없음** — Phase B 와 의도적으로 다름. 외 조직 row → 403. 무소속 → 422. |
| `DELETE /canonical/{id}/progress/{progress_id}` (삭제) | PATCH 와 동일 — 같은 조직 멤버 누구나. 외 조직 → 403. 무소속 → 422. |
| `GET /canonical/{id}/progress/history` (이력 조회) | 비로그인 허용 (Phase B GET history 비로그인 허용 패턴). |

이 결정의 의도: 진행 상태가 **조직 단위 의사결정** 이라 협업이 필요. 작성자
휴가/퇴사여도 다른 멤버가 변경 가능해야 함. `created_by_user_id` 는 "마지막 수정자"
표시 용도의 메타 정보이며 권한 판정에는 사용하지 않는다.

검증 위반 응답 매핑 (후속 API subtask 00097-4 가 따른다):

| 위반 | HTTP status |
|------|-------------|
| 비로그인 POST/PATCH/DELETE | 401 |
| 무소속 사용자 POST/PATCH/DELETE | 422 |
| 본인 소속 외 조직의 progress_id 를 PATCH/DELETE 시도 | 403 |
| body 의 `organization_id` 가 본인 소속이 아님 (POST) | 422 |
| 모르는 organization_id (DB FK 위반) | 422 |
| 모르는 progress_id | 404 |
| canonical_project 없음 | 404 |
| 선점 제약 위반 (다른 조직이 이미 `'진행'`) | 409 (+ "조직 X 가 이미 진행 중입니다" 안내) |

> 409 vs 422 — 선점 제약 위반은 "현재 자원 상태 충돌" 의미가 강하므로 **409 Conflict**
> 를 채택한다 (RFC 7231 §6.5.8). 본 문서는 409 로 확정하고 후속 API subtask 가 그대로
> 따른다.

### 3.2 status 전이 정책

- 4단계 (`'관심'` ↔ `'검토'` ↔ `'진행'` ↔ `'종료'`) 사이 **양방향 자유 전이**.
  진행 → 종료 후 다시 진행으로 되돌아가도 무방 (실수 복구).
- `'진행'` 으로 올리는 시점에만 **선점 제약** 검증 — 다른 조직이 이미 `'진행'` 인
  row 가 있으면 거부 (409).
- 변경 시 기존 row 를 history 로 이관 (`archive_reason='user_changed'`) + in-place
  UPDATE (Phase B `user_changed` 패턴 그대로).

---

## 4. 표시 UI (목록 셀 / 상세 페이지 / 모달 / hover 툴팁)

### 4.1 목록 셀 표시 정책 표

`MINE_ACTIVE` = 본인 소속 조직 중 하나가 이 canonical 에 row 를 가지고 있고
그 status 가 `'관심'` / `'검토'` / `'진행'` 중 하나 (`'종료'` 는 활동성으로 보지 않음).

`OCCUPYING_ORG` = 이 canonical 에 `status='진행'` row 가 있는 조직 (있으면 1 개).

| 케이스 | 큰 글씨 (선점 라인) | small text (카운터 라인) |
|--------|---------------------|--------------------------|
| 선점 있음 + 본인 조직이 선점 | 🚩 **AAA팀: 진행** (조직명 강조 — 색상 또는 굵게) | `검토 N · 관심 M` |
| 선점 있음 + 다른 조직이 선점 | 🚩 BBB팀: 진행 | `검토 N · 관심 M` |
| 선점 없음 + 카운터 1 이상 | (생략 — 라인 자체 없음) | `검토 N · 관심 M` |
| 선점 없음 + MINE_ACTIVE 만 (관심·검토) | (생략) | `검토 N · 관심 M` (본인 조직 부분 강조) |
| 선점 없음 + 카운터 0 (아무도 활동 안 함) | (생략) | **§4.3 결정 적용** |

추가 규칙:

- 카운터 표기에서 `'종료'` 는 **포함하지 않는다** — "현재 어느 조직이 검토·관심 중
  인가" 가 메시지의 핵심이라 종료 row 는 의미 없음.
- 선점 조직이 본인 소속이면 조직명을 색상 강조 + bold (Phase B 의 본인 row 강조와
  동일 컨벤션).
- 본인 소속 조직 중 하나라도 `'관심'` / `'검토'` 단계에 있으면 small text 카운터
  에서 본인 조직 부분을 강조한다.
- 카운터는 인라인 직접 노출 (Phase B `✅/❌` 패턴 동일 — 폭 여유 시 `검토 N · 관심 M`
  형태로 그대로 표시. 폭 부족 시 `검토 N` / `관심 M` 만 한 줄 + hover 툴팁으로 분리).

### 4.2 셀 클릭 expand

셀 클릭 시 `<details>` expand 로 row 안에서 조직별 상세를 풀어 표시한다 (Phase B
의 동일 과제 expand 패턴과 시각적으로 통일).

```
[선점 라인]
  🚩 AAA팀: 진행

[조직별 상세 풀어 표시]
  AAA팀  — 진행 — 마지막 수정: alice / 2026-05-08 10:23 — note "Phase 2 진입"
  BBB팀  — 검토 — 마지막 수정: bob   / 2026-05-07 14:05 — note "사전조사 중"
  CCC팀  — 관심 — 마지막 수정: carol / 2026-05-06 09:00 — note ""
```

### 4.3 빈 셀 표시 결정 (본 phase 의 유일한 미결정 항목 → 결정)

선택지 두 가지:

1. `"—"` (em dash) 단일 문자 표시 — "데이터 없음" 의 시각적 placeholder.
2. **완전 빈 공간** — row 높이만 유지하고 셀 내부는 공백.

**결정 — 안 1 (`"—"`) 채택**.

이유:

- 목록 페이지 다른 셀 (마감일·기관 등) 도 빈 값에 `—` 를 사용 (`relevance_ui_design.md`
  와 정합). 신규 컬럼만 빈 공간을 두면 시각적 일관성이 깨진다.
- 사용자 입장에서 "이 컬럼이 비활성인가" / "데이터가 없는가" 를 즉시 구분하기 어렵다
  — `—` 는 "비어 있음" 을 명시적으로 전달한다.
- 빈 공간은 행 그라디언트·zebra-stripe 디자인을 깨뜨릴 수 있어 CSS 유지보수 부담이
  커진다.

후속 UI subtask 00097-5 가 이 결정을 그대로 구현한다.

### 4.4 hover 툴팁 (Phase B 의 fixed 레이어 패턴 — 00088 재사용)

목록 셀의 `overflow:hidden` 클리핑을 받지 않는 viewport 기준 fixed 레이어. 위/아래
자동 반전 + 좌우 viewport 클램핑.

| hover 대상 | 툴팁 내용 |
|-----------|-----------|
| 🚩 선점 라인 | "조직명: AAA팀 / 단계: 진행 / 마지막 수정: alice / 시점: 2026-05-08 10:23" |
| `검토 N · 관심 M` 카운터 | "검토 단계 조직: 본부, AI팀 / 관심 단계 조직: CCC팀, DDD팀" (단계별 분리 나열) |
| `—` (빈 셀) | 노출 안 함 (hover 시에도 툴팁 없음) |

### 4.5 상세 페이지 인라인 섹션 와이어프레임

진행 상태 섹션은 **모달이 아닌 상세 페이지 인라인** 으로 변경한다. 목록 페이지에는
입력 UI 없음 (보기·expand 만).

```
─────────────────────────────────────────────────────────────────────────
공고 진행 상태
─────────────────────────────────────────────────────────────────────────

[ 우리 조직의 입장 ]
  본부 (AAA팀)  — 진행 ▾   [ 저장 ] [ 삭제 ]
                          마지막 수정: alice / 2026-05-08 10:23
                          ┌────────────────────────────────────────┐
                          │ Phase 2 진입 결정. 협업 부서 확정 필요. │
                          └────────────────────────────────────────┘

  AI팀          — (입장 표명 안 됨)
                  [ 우리 조직 입장 표명하기 ]

[ 다른 조직 입장 ]
  BBB팀         — 검토   — 마지막 수정: bob   / 2026-05-07 14:05
                  note: "사전조사 중"
  CCC팀         — 관심   — 마지막 수정: carol / 2026-05-06 09:00
                  note: ""
─────────────────────────────────────────────────────────────────────────
```

- 본인 소속 조직별로 한 row.
- 본인 소속 조직에 row 가 이미 있으면 인라인 컨트롤 (status select + note textarea
  + [저장] [삭제]). 같은 조직 멤버 누구나 변경 가능 (§3.1).
- 본인 소속 조직에 row 가 없으면 `[ 우리 조직 입장 표명하기 ]` 버튼 → 클릭 시 인라인
  폼 노출 (status 선택 + note 입력 + 저장).
- 다른 조직 row 는 read-only — 작성자명·시점·note 는 그대로 표시.

### 4.6 새 row 작성 — 조직 드롭다운 (복수 조직 소속 케이스)

상세 페이지에서 본인이 여러 조직에 소속된 경우, 각 조직별로 row 슬롯이 풀어져
있으므로 **각 슬롯의 [표명하기] 버튼** 이 곧 그 조직 입장 작성으로 직결된다 — Phase
B 모달의 조직 선택 드롭다운은 **불필요** 하다 (UI 상 이미 조직별로 나뉘어 있어
드롭다운으로 다시 고를 일이 없음).

즉 §4.5 의 "AI팀 — (입장 표명 안 됨) [ 우리 조직 입장 표명하기 ]" 슬롯이 곧 AI팀
입장의 신규 row 작성 폼이다.

(Phase B 의 "조직 드롭다운 + 라디오" 패턴은 모달 단일 폼이라 필요했음 — Phase C 는
인라인 섹션이 조직별로 구조적으로 분할되어 드롭다운이 redundant.)

### 4.7 비로그인 노출

Phase B 와 동일 — 로그인 사용자와 동일 노출.

- 카운터·🚩 선점 조직명·다른 조직 row 모두 그대로 보임.
- 본인 소속 조직 슬롯·`[표명하기]` 버튼·인라인 컨트롤은 미표시 (또는 readonly).
- hover 툴팁도 동일 노출.

---

## 5. 선점 제약 — app-level transactional 체크 (의사코드)

### 5.1 partial unique index 회피 이유

"한 canonical 에 `status='진행'` row 가 최대 1 개" 는 SQL 표준 UNIQUE 만으로 강제
불가 (`(canonical, organization)` UNIQUE 는 `'진행'` 조건을 거를 수 없다). 후보:

1. **Postgres partial unique index** — `CREATE UNIQUE INDEX ... WHERE status='진행'`.
   SQLite 도 partial index 는 지원하지만 dialect-specific 표현이 들어가 migration
   syntax 가 갈라진다. `docs/db_portability.md` §3 + Phase B `relevance_org_design.md`
   §3 (단일 UNIQUE 채택) 결정 동일하게 **회피**.
2. **app-level transactional 체크** — 트랜잭션 안 SELECT → 없을 때만 INSERT/UPDATE
   → flush. SQLite 는 단일 writer 라 race condition 없음.

본 phase 는 **(2) app-level 체크** 를 채택한다.

### 5.2 의사코드

```python
def upsert_progress(
    session,
    canonical_project_id: int,
    organization_id: int,
    new_status: str,           # '관심' / '검토' / '진행' / '종료'
    note: str | None,
    actor_user_id: int,
) -> AnnouncementProgress:
    """진행 상태 upsert + 선점 제약 검증 + history 이관.

    `new_status='진행'` 으로 올리는 경우만 같은 canonical 의 다른 organization_id
    중 `status='진행'` 이 있는지 확인하고, 있으면 PreemptionConflict 예외를 던진다
    (라우터가 409 로 변환).
    """
    # 1) 선점 제약 검증 — '진행' 으로 올리는 경우만.
    if new_status == "진행":
        existing_progress_row = session.execute(
            select(AnnouncementProgress)
            .where(
                AnnouncementProgress.canonical_project_id == canonical_project_id,
                AnnouncementProgress.organization_id != organization_id,
                AnnouncementProgress.status == "진행",
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing_progress_row is not None:
            raise PreemptionConflict(
                conflicting_organization_id=existing_progress_row.organization_id,
            )

    # 2) 본인 조직의 기존 row 조회 (UNIQUE 키).
    current_row = session.execute(
        select(AnnouncementProgress).where(
            AnnouncementProgress.canonical_project_id == canonical_project_id,
            AnnouncementProgress.organization_id == organization_id,
        )
    ).scalar_one_or_none()

    # 3) 있으면 history 이관 + UPDATE, 없으면 INSERT.
    if current_row is not None:
        archive_progress_to_history(
            session,
            current_row,
            archive_reason="user_changed",
        )
        current_row.status = new_status
        current_row.note = note
        current_row.created_by_user_id = actor_user_id
        # updated_at 은 onupdate 콜백이 자동 갱신
    else:
        current_row = AnnouncementProgress(
            canonical_project_id=canonical_project_id,
            organization_id=organization_id,
            status=new_status,
            note=note,
            created_by_user_id=actor_user_id,
        )
        session.add(current_row)

    session.flush()  # 같은 트랜잭션 안에서 race 없음 (SQLite 단일 writer)
    return current_row
```

> **race 가능성 분석**: SQLite 는 트랜잭션 시작 시 BEGIN IMMEDIATE / EXCLUSIVE 모드로
> 단일 writer 만 허용 (WAL 모드에서도 writer 1 명). 위 SELECT → INSERT 사이에 다른
> writer 가 끼어들 수 없다. Postgres 전환 시 동일 패턴이 race condition 가능성을
> 갖게 되지만, 그 시점에 partial unique index 추가 또는 `SELECT FOR UPDATE` 패턴을
> 별도 결정 — 본 phase 의 결정 범위 밖 (`db_portability.md` §5 의 "FOR UPDATE 사용
> 시 application-level 직렬화 보완" 정책 그대로).

### 5.3 history 이관 헬퍼

```python
def archive_progress_to_history(
    session,
    current_row: AnnouncementProgress,
    archive_reason: str,  # 'user_changed' | 'content_changed'
) -> None:
    """현재 row 를 history 로 복사. 호출자가 이후 UPDATE/DELETE 한다."""
    history_row = AnnouncementProgressHistory(
        canonical_project_id=current_row.canonical_project_id,
        organization_id=current_row.organization_id,
        status=current_row.status,
        note=current_row.note,
        created_by_user_id=current_row.created_by_user_id,
        created_at=current_row.created_at,
        updated_at=current_row.updated_at,
        # archived_at 은 default 콜백이 자동 채움
        archive_reason=archive_reason,
    )
    session.add(history_row)
```

---

## 6. content_changed reset 회귀 (Phase 1a 변경 감지)

`canonical_projects` 의 비교 4 필드 (title·status·agency·deadline_at) 변경 감지 시
해당 canonical 의 모든 `announcement_progress` row 가 `announcement_progress_history`
로 이관된다 (`archive_reason='content_changed'`). Phase B `RelevanceJudgment` 와 동일
패턴.

후속 repository subtask 00097-3 가 다음을 보장한다 (검증 시나리오 8).

- 같은 canonical 에 여러 조직의 progress row 가 공존하는 상태에서 content_changed
  발동 → **모든 row** 가 history 로 이관 + `archive_reason='content_changed'` 정확히
  보존.
- 이관 후 `announcement_progress` 는 빈 상태가 되고 (해당 canonical 에 한해), UI 가
  목록 셀에서 즉시 `—` 빈 셀로 반영.
- progress 의 row 는 `RelevanceJudgment` 와 별개 테이블이므로 `apply_delta_to_main` /
  `migrate_to_history` 호출 시점에 progress 도 함께 이관해야 함을 잊지 말 것 (회귀
  테스트 필수 — 매 phase 의 흔한 누락 지점).

---

## 7. 쿼리 최적화 — `get_progress_summary_by_canonical_id_map`

목록 페이지에서 N+1 회귀 없도록 페이지당 추가 쿼리 1~2 개 고정. Phase B
`get_relevance_summary_by_canonical_id_map` 패턴을 그대로 모방한다.

### 7.1 시그니처

```python
def get_progress_summary_by_canonical_id_map(
    session,
    user_id: int | None,           # None = 비로그인
    canonical_ids: Sequence[int],
) -> dict[int, ProgressSummary]:
    """canonical_id 별 표시용 요약 dict.

    반환 dict 의 value 는 ProgressSummary dataclass:
      - occupying_organization_id: int | None        # status='진행' 조직 (있으면)
      - occupying_organization_name: str | None      # 조직명
      - count_review: int                            # status='검토' row 수
      - count_interest: int                          # status='관심' row 수
      - mine_status: str | None                      # 본인 소속 조직 row 의 status
                                                       (있으면. 본인이 여러 소속이면
                                                       활동성 우선순위 진행>검토>관심>종료
                                                       로 가장 강한 단계 1 개).
      - mine_organization_id: int | None             # 위 mine_status 가 어느 조직인지.
    """
```

### 7.2 쿼리 묶음 전략

(1) 선점 조직 + 카운터 — 한 번의 GROUP BY 쿼리:

```sql
SELECT
  canonical_project_id,
  status,
  COUNT(*)                                                   AS row_count,
  MIN(organization_id) FILTER (WHERE status='진행')          AS occupying_org_id
FROM announcement_progress
WHERE canonical_project_id IN (:canonical_ids)
GROUP BY canonical_project_id, status
```

(SQLite·Postgres 둘 다 `FILTER` 미지원 dialect 분기 회피 위해 실제 구현은 단순
GROUP BY + Python 후처리 — `db_portability.md` §2 의 `func.lower()` + Python
후처리 패턴 동일 컨벤션.)

조직명 join 은 `IN (occupying_org_ids)` 단일 IN 쿼리 1 회.

(2) 본인 소속 조직 row — 로그인 시에만:

```sql
SELECT canonical_project_id, organization_id, status
FROM announcement_progress
WHERE canonical_project_id IN (:canonical_ids)
  AND organization_id IN (:my_org_ids)
```

비로그인 시 (2) skip → 페이지당 추가 쿼리 1 개. 로그인 시 1~2 개.

---

## 8. 진행 상태 필터 — 다중 체크박스

목록 페이지 필터 신규 — URL query param `progress`. **다중 체크박스 UI** (사용자
사전 확정).

### 8.1 체크박스 옵션 + URL 파라미터 매핑

| UI 라벨 | URL 파라미터 키 | 의미 |
|---------|-----------------|------|
| "선점 미발생" | `none` | 아무 조직도 `status='진행'` 이 아닌 canonical |
| "다른 조직이 진행" | `other_in_progress` | 본인 소속 외 조직이 `status='진행'` (충돌 회피용 핵심) |
| "내 조직이 진행" | `mine_in_progress` | 본인 소속 조직 중 하나가 `status='진행'` |
| "내 조직 검토 중" | `mine_in_review` | 본인 소속 조직 중 하나가 `status='검토'` |

**URL 파라미터 키 — 영문** 채택 결정 (한글 vs 영문 prompt 의 미결정 항목).

이유:

- URL 인코딩 시 한글은 percent-encoding 으로 늘어나 URL 가독성·복사 가능성이 떨어
  진다 (`?progress=%EC%84%A0%EC%A0%90%EB%AF%B8%EB%B0%9C%EC%83%9D` vs `?progress=none`).
- 다른 query param (`status=접수중`, `source=IRIS`) 이 한글·영문 혼용 — 이미 일관성
  부족. 신규 파라미터부터 영문으로 통일하는 방향.
- 다중 선택 시 `?progress=none,mine_in_review` 처럼 콤마 구분, parser 가 단순.

다중 선택 시 **OR** 의미 — 해당 조건 중 **하나라도 만족** 하는 canonical. AND /
복잡 boolean 조합은 **범위 밖** (사용자 사전 확정).

URL 예:

- `?progress=none` — 선점 미발생만.
- `?progress=mine_in_progress,mine_in_review` — 본인 조직이 진행 중이거나 검토 중인 공고.
- `?progress=` (빈 문자열) — 필터 미적용 (전체).
- `progress` 파라미터 자체 없음 — 필터 미적용 (전체).

### 8.2 비로그인 사용자 동작

- "선점 미발생" / "다른 조직이 진행" 체크박스만 활성.
- "내 조직이 진행" / "내 조직 검토 중" 체크박스 `disabled` + tooltip "로그인 후 사용
  가능". URL 에 `mine_in_progress` 가 와도 비로그인 컨텍스트에서는 자동 무시 (서버
  측 sanitize) — 401 거부가 아니라 silent drop (필터의 다른 조건은 적용).

### 8.3 와이어프레임

```
[ 진행 상태 필터 ]
  □ 선점 미발생
  □ 다른 조직이 진행
  □ 내 조직이 진행          (비로그인 시 disabled + "로그인 후 사용 가능")
  □ 내 조직 검토 중          (비로그인 시 disabled + "로그인 후 사용 가능")
  [ 적용 ]   [ 초기화 ]
```

- 적용 클릭 → URL 쿼리스트링 갱신 + 페이지 reload (또는 fetch 갱신).
- 초기화 클릭 → 4 옵션 모두 해제 + `progress` 파라미터 제거.
- 페이지네이션 정합 — `progress` 가 변경되면 `page=1` 로 리셋.

### 8.4 SQL 분기 (의사코드)

```python
def apply_progress_filter(query, options: set[str], my_org_ids: set[int]):
    """progress 파라미터에 따라 announcements 쿼리에 EXISTS 서브쿼리를 추가."""
    or_clauses = []
    if "none" in options:
        # canonical 에 status='진행' row 가 없는 경우
        or_clauses.append(
            ~exists()
            .where(
                AnnouncementProgress.canonical_project_id == Announcement.canonical_project_id,
                AnnouncementProgress.status == "진행",
            )
        )
    if "other_in_progress" in options:
        or_clauses.append(
            exists()
            .where(
                AnnouncementProgress.canonical_project_id == Announcement.canonical_project_id,
                AnnouncementProgress.status == "진행",
                AnnouncementProgress.organization_id.notin_(my_org_ids or [-1]),
            )
        )
    if "mine_in_progress" in options and my_org_ids:
        or_clauses.append(
            exists()
            .where(
                AnnouncementProgress.canonical_project_id == Announcement.canonical_project_id,
                AnnouncementProgress.status == "진행",
                AnnouncementProgress.organization_id.in_(my_org_ids),
            )
        )
    if "mine_in_review" in options and my_org_ids:
        or_clauses.append(
            exists()
            .where(
                AnnouncementProgress.canonical_project_id == Announcement.canonical_project_id,
                AnnouncementProgress.status == "검토",
                AnnouncementProgress.organization_id.in_(my_org_ids),
            )
        )
    if or_clauses:
        query = query.where(or_(*or_clauses))
    return query
```

비로그인 + `mine_*` 조건은 `my_org_ids` 가 비어 OR clause 자체가 추가되지 않는다
(silent drop).

---

## 9. Migration 영향 범위

### 9.1 변경되는 DDL — 신규 테이블 2 개

migration 파일 1 개로 두 테이블을 한 트랜잭션에 만든다 (subtask 00097-2 구현).

```python
def upgrade() -> None:
    # 1) announcement_progress
    op.create_table(
        "announcement_progress",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("canonical_project_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "관심", "검토", "진행", "종료",
                name="announcement_progress_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["canonical_project_id"], ["canonical_projects.id"],
            name="fk_announcement_progress_canonical_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_announcement_progress_organization_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"],
            name="fk_announcement_progress_created_by_user_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "canonical_project_id", "organization_id",
            name="uq_announcement_progress_canonical_org",
        ),
    )
    op.create_index(
        "ix_announcement_progress_canonical_id",
        "announcement_progress", ["canonical_project_id"],
    )
    op.create_index(
        "ix_announcement_progress_organization_id",
        "announcement_progress", ["organization_id"],
    )
    op.create_index(
        "ix_announcement_progress_status",
        "announcement_progress", ["status"],
    )

    # 2) announcement_progress_history (UNIQUE 없음, 동일 컬럼 + archived_at + archive_reason)
    op.create_table(
        "announcement_progress_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("canonical_project_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "관심", "검토", "진행", "종료",
                name="announcement_progress_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "archive_reason",
            sa.Enum(
                "user_changed", "content_changed",
                name="announcement_progress_archive_reason",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["canonical_project_id"], ["canonical_projects.id"],
            name="fk_announcement_progress_history_canonical_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_announcement_progress_history_organization_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"],
            name="fk_announcement_progress_history_created_by_user_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_announcement_progress_history_canonical_id",
        "announcement_progress_history", ["canonical_project_id"],
    )
    op.create_index(
        "ix_announcement_progress_history_organization_id",
        "announcement_progress_history", ["organization_id"],
    )
    op.create_index(
        "ix_announcement_progress_history_archived_at",
        "announcement_progress_history", ["archived_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_announcement_progress_history_archived_at",
                  table_name="announcement_progress_history")
    op.drop_index("ix_announcement_progress_history_organization_id",
                  table_name="announcement_progress_history")
    op.drop_index("ix_announcement_progress_history_canonical_id",
                  table_name="announcement_progress_history")
    op.drop_table("announcement_progress_history")
    op.drop_index("ix_announcement_progress_status",
                  table_name="announcement_progress")
    op.drop_index("ix_announcement_progress_organization_id",
                  table_name="announcement_progress")
    op.drop_index("ix_announcement_progress_canonical_id",
                  table_name="announcement_progress")
    op.drop_table("announcement_progress")
```

### 9.2 Constraint / 인덱스 이름 (Postgres 호환 명시)

| 위치 | 종류 | 이름 |
|------|------|------|
| `announcement_progress` | UNIQUE | `uq_announcement_progress_canonical_org` |
| `announcement_progress` | FK | `fk_announcement_progress_canonical_id` |
| `announcement_progress` | FK | `fk_announcement_progress_organization_id` |
| `announcement_progress` | FK | `fk_announcement_progress_created_by_user_id` |
| `announcement_progress` | INDEX | `ix_announcement_progress_canonical_id` |
| `announcement_progress` | INDEX | `ix_announcement_progress_organization_id` |
| `announcement_progress` | INDEX | `ix_announcement_progress_status` |
| `announcement_progress_history` | FK | `fk_announcement_progress_history_canonical_id` |
| `announcement_progress_history` | FK | `fk_announcement_progress_history_organization_id` |
| `announcement_progress_history` | FK | `fk_announcement_progress_history_created_by_user_id` |
| `announcement_progress_history` | INDEX | `ix_announcement_progress_history_canonical_id` |
| `announcement_progress_history` | INDEX | `ix_announcement_progress_history_organization_id` |
| `announcement_progress_history` | INDEX | `ix_announcement_progress_history_archived_at` |

### 9.3 기존 row 영향 — backfill 불필요

신규 테이블 2 개만 생성한다. 기존 테이블 (`canonical_projects` /
`relevance_judgments` / `announcements` 등) 의 컬럼·constraint 변경 **없음**. 기존
row 의 보강·전환·재계산이 필요 없으므로 **backfill 스크립트 불필요**.

### 9.4 Migration 검증 3 단계 (`db_portability.md` §4 인용)

1. **기존 SQLite (stamp 경로)** — 운영 DB 사본에 `init_db()` → `alembic upgrade head`
   로 신규 테이블 2 개 생성, 기존 데이터 무변경 확인.
2. **빈 SQLite (baseline-bootstrap)** — 새 DB 에 `alembic upgrade head` → 신규
   테이블·UNIQUE·FK·INDEX 모두 생성됨 + Phase 1a 까지 모든 테이블 함께 생성.
3. **Postgres syntax 호환** — `op.create_table` + `native_enum=False` + constraint
   이름 모두 명시. dialect 전용 표현 (partial unique index 등) 없음.

---

## 10. 본 문서가 다루지 않는 항목 (다음 subtask 로 넘김)

| 항목 | 위임 subtask |
|------|-------------|
| ORM 모델 클래스 (`AnnouncementProgress` / `AnnouncementProgressHistory`) `__table_args__` / relationship | 00097-2 |
| `announcement_progress_status` 한글 enum 의 SQLAlchemy `Enum` 정의 + `native_enum=False` 적용 | 00097-2 |
| `upsert_progress` / `delete_progress` / `get_progress_summary_by_canonical_id_map` 시그니처 + 선점 제약 SELECT 의 정확한 SQL | 00097-3 |
| `archive_progress_to_history` 헬퍼 + content_changed reset 회귀 테스트 | 00097-3 |
| API 라우터 (POST/PATCH/DELETE/GET history) + 본인 소속 조직 검증 + 선점 제약 → 409 변환 | 00097-4 |
| 목록 셀 매크로 (`_progress_cell_macro.html` 등) + 상세 페이지 인라인 섹션 + hover 툴팁 (fixed 레이어, 00088 패턴) | 00097-5 |
| 다중 체크박스 필터 UI + 진행 상태 필터 SQL 분기 + 비로그인 silent drop | 00097-6 |

---

## 11. 검증 시나리오 → subtask 매핑

사용자 원문 검증 17 항목을 후속 subtask 에 매핑한다.

| # | 시나리오 | 담당 subtask |
|---|----------|-------------|
| 1 | 본인 소속 조직 진행 row 신규/수정/삭제 정상 (4단계 모두) | 00097-3 (repo) + 00097-4 (route) |
| 2 | 본인 소속 외 조직 row 작성/수정 시도 → 403 | 00097-4 |
| 3 | 무소속 사용자 작성 시도 → 422 | 00097-4 |
| 4 | `status='진행'` 선점 — 다른 조직이 이미 `'진행'` 인 상태에서 본인 `'진행'` 시도 → 409 + 안내 | 00097-3 (repo 의 PreemptionConflict) + 00097-4 (route → 409) |
| 5 | 선점 조직이 `'종료'` / 다른 단계로 내려가면 다른 조직이 `'진행'` 으로 올릴 수 있음 | 00097-3 |
| 6 | 양방향 롤백 — 진행→검토→관심→종료→검토 등 모든 전이 가능 | 00097-3 + 00097-4 |
| 7 | status 변경 시 history 이관 (이전 status, note, archived_at 정확히 보존) | 00097-3 (회귀 테스트) |
| 8 | `content_changed` reset (Phase 1a) 시 `announcement_progress` 도 history 로 이관 | 00097-3 (Phase 1a 변경 감지 통합 테스트) |
| 9 | 같은 조직 동료가 만든 row 를 본인이 수정·삭제 가능 (조직 멤버 누구나 권한) | 00097-3 + 00097-4 |
| 10 | 목록 셀 — 선점 / 미선점 / 빈 셀 3 케이스 정상 렌더, 본인 조직 활동 강조 동작 | 00097-5 |
| 11 | 카운터 인라인 직접 노출 + hover 툴팁 (fixed 레이어, Phase B 패턴) | 00097-5 |
| 12 | 셀 클릭 expand 로 조직별 상세 표시 | 00097-5 |
| 13 | 상세 페이지 "진행 상태" 섹션 — 본인 조직 row 만 변경 컨트롤 노출, 다른 조직 row 는 read-only | 00097-5 |
| 14 | 비로그인 — 카운터·선점 조직명·다른 조직 row 동일 노출, 변경 영역만 비활성 | 00097-4 (history GET 허용) + 00097-5 |
| 15 | 진행 상태 필터 — 4 옵션 정상 동작, 다중 선택 시 OR, URL 파라미터 + 페이지네이션 정합 | 00097-6 |
| 16 | 비로그인 시 "내 조직 ..." 체크박스 disabled | 00097-6 |
| 17 | N+1 회귀 없음 — 페이지당 추가 쿼리 1~2 개 고정 | 00097-3 + 00097-6 |

---

## 12. 주의사항 (사용자 원문 그대로 유지)

1. **canonical 단위** — 진행 상태 = canonical, 읽음 = announcement (PROJECT_NOTES
   컨벤션 유지).
2. **`as_utc()` 헬퍼 사용** — `archived_at` / `created_at` / `updated_at` 비교 시
   tz-aware 통일. `datetime.utcnow()` (naive) 금지.
3. **Jinja2 `<script>` 블록 안 Jinja 태그 리터럴 금지** — 서버 값은 `data-*`
   attribute 또는 `<script type="application/json">` 경유.
4. **`ensure_same_origin` + `current_user_required`** — 본인 영역의 모든
   POST/PATCH/DELETE 에 유지. GET history 만 비로그인 허용.
5. **N+1 제거 헬퍼 패턴 유지** — `get_progress_summary_by_canonical_id_map` 는
   페이지당 1~2 회 호출로 선점 조직 + 카운터 + 본인 조직 row 를 묶어서 반환.
6. **한글 enum 보존** — DB CHECK constraint (`native_enum=False`) 로 Postgres·SQLite
   양쪽 호환.
7. **partial unique index 사용 안 함** — `db_portability.md` §3 + Phase B 컨벤션
   동일. app-level transactional 체크 (§5).
8. **새 컬럼·테이블의 모든 시간은 UTC tz-aware 저장, 표시 KST 변환** —
   `app/timezone.py` 의 `_as_utc` / `_to_kst` 헬퍼 경유.
9. **실행 스크립트는 `./run_compose.sh` / `./run_admin.sh`** (`./compose.sh` 폐기).
   alembic migration 추가 시 호스트 바인드 마운트로 자동 반영, 이미지 재빌드 불필요.
10. **한국어 주석 / 변수·함수명 축약 금지** — 반년 뒤의 본인을 위한 가독성.

---

## 13. Phase B 와의 차이점 (의도적 분기)

| 항목 | Phase B (relevance, 00085~00093) | Phase C (progress, 00097) |
|------|----------------------------------|---------------------------|
| UNIQUE 키 | `(canonical, user, organization)` 단일 | `(canonical, organization)` 단일 — `user_id` 없음 |
| row 단위 의미 | "이 사용자가 이 조직 입장으로 한 판정" — 같은 조직 다중 사용자 row 가능 | "이 조직의 입장" — 조직당 1 row, 작성자는 메타로만 |
| 권한 | row **작성자 본인만** 수정·삭제 | **조직 멤버 누구나** (작성자 무관) — 협업 의사결정 패턴 |
| 입력 UI | 모달 (verdict + 사유 + 조직 선택 드롭다운) | 상세 페이지 인라인 섹션 (조직별 슬롯 풀어 표시 — 드롭다운 redundant) |
| 단계 | 2단계 (`'관련'` / `'무관'`) | 4단계 (`'관심'` / `'검토'` / `'진행'` / `'종료'`) |
| 선점 제약 | 없음 | `'진행'` 단계만 한 canonical 당 단일 조직 |
| 비로그인 | 동일 노출, 본인 영역만 비활성 | 동일 — 본인 영역만 비활성 |
| 무소속 | 작성·수정·삭제 거부 (422) | 동일 |
| history 이관 | `RelevanceJudgmentHistory` 동일 패턴 | `AnnouncementProgressHistory` 동일 패턴 + `archive_reason` enum 추가 |
| filter UI | 큰 배지 색·hover 툴팁만 (필터 항목 없음) | 다중 체크박스 4 옵션 (URL `?progress=...`) |

> 핵심 분기 의도: Phase B 는 "사용자의 사적 판단" 이라 row 작성자 본인 한정.
> Phase C 는 "조직 단위 공식 입장 표명" 이라 협업 — 작성자 휴가/퇴사 시 다른 멤버
> 가 변경 가능해야 한다.

---

## 14. 후속 subtask 의존 순서

```
00097-1 (본 문서 — 설계 + README.USER.md cleanup)
    ↓
00097-2  Alembic migration + ORM 모델 (announcement_progress + announcement_progress_history)
    ↓
00097-3  repository (CRUD + 선점 제약 + history 이관 + summary 헬퍼 + content_changed reset 회귀)
    ↓
00097-4  API 라우터 (POST/PATCH/DELETE/GET history) + 권한 검증 + 서버측 테스트
    ↓
00097-5  UI (목록 셀 + 상세 페이지 인라인 섹션 + hover 툴팁)
    ↓
00097-6  UI (다중 체크박스 필터) + 회귀 종합 검증 (e2e)
```
