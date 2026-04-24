# Phase 3a — 관련성 판정 UI + 읽음 bulk 데이터 흐름 설계

> **작성 범위**: Task 00035 (Phase 3a) subtask 00035-1 — Phase 1a의
> `RelevanceJudgment` / `RelevanceJudgmentHistory` / `AnnouncementUserState`
> 위에 관련성 판정 UI와 읽음 bulk 기능을 얹는 전체 설계.
> **코드 구현은 이 문서가 아니라 subtask 00035-2 ~ 00035-5 가 수행한다.**
>
> 참고 문서:
> - [docs/canonical_identity_design.md](canonical_identity_design.md) — canonical_key, canonical_projects 스키마
> - [docs/schema_phase1a.md](schema_phase1a.md) — RelevanceJudgment / History / AnnouncementUserState 상세
> - [docs/auth_ui_design.md](auth_ui_design.md) — current_user_required / ensure_same_origin / N+1 패턴

---

## 0. 범위 밖 (이 task에서 구현하지 않음)

| 항목 | 예정 Phase |
|------|-----------|
| 즐겨찾기 폴더 / `favorite_entries` | 3b |
| 동일과제 섹션 (상세 페이지) | 3b |
| `audit_logs` write | 5 |
| 이메일 발송 | 4 |
| 대시보드 | 4 |
| `canonical_overrides` 실행 로직 | 5 |

---

## 1. 데이터 단위 규칙

| 기능 | 단위 | 테이블 | 비고 |
|------|------|--------|------|
| 읽음 상태 | announcement | `announcement_user_states` | IRIS / NTIS 행별 독립. Phase 1b 구조 유지 |
| 관련성 판정 | canonical_project | `relevance_judgments` | `(canonical_project_id, user_id)` UNIQUE |
| 판정 이력 | canonical_project | `relevance_judgment_history` | 이관된 과거 판정 보존 |

목록 row는 `announcement` 단위이지만 관련성 배지는 `canonical_project` 기준이다.
→ 페이지 로드 시 canonical_project_id 집합을 추출하고 batch SELECT로 판정 맵을 주입한다.

---

## 2. 관련성 배지 / 툴팁 데이터 흐름

### 2.1 목록 페이지 (index_page) 흐름

```
1. list_announcements / list_canonical_groups 로 현재 페이지 공고 목록 조회 (기존)

2. (로그인 시만) canonical_project_id 집합 추출
     - flat 모드:  {ann.canonical_group_id for ann in announcement_items if ann.canonical_group_id}
     - group 모드: {gr.representative.canonical_group_id for gr in groups if gr.representative.canonical_group_id}

3. get_relevance_by_canonical_id_map(session, ids=canonical_ids)
     → {canonical_project_id: RelevanceJudgment}   # 전체 사용자 current 판정
     # 비로그인 시 빈 dict, 쿼리 없음

4. get_relevance_history_by_canonical_id_map(session, ids=canonical_ids, limit=3)
     → {canonical_project_id: list[RelevanceJudgmentHistory]}   # 툴팁용 최근 3건
     # 비로그인 시 빈 dict, 쿼리 없음

5. 두 맵 + 기존 read_id_set 을 templates context에 주입
     → 템플릿이 배지 / 툴팁 / 읽음 bold 렌더
```

**N+1 방지**: Phase 1b의 `get_read_announcement_id_set` 패턴과 동일. 페이지당
추가 쿼리는 최대 2회 (relevance current + history). 비로그인 시 모두 skip.

### 2.2 신규 헬퍼 시그니처 (subtask 00035-2 구현)

```python
def get_relevance_by_canonical_id_map(
    session: Session,
    ids: Iterable[int],
) -> dict[int, RelevanceJudgment]:
    """canonical_project_id → current RelevanceJudgment 맵을 한 쿼리로 반환한다.

    전체 사용자의 current 판정을 반환한다 (사용자 필터 없음).
    판정이 없는 canonical_project_id 는 맵에서 제외된다.
    ids 가 비어 있으면 쿼리 없이 빈 dict 반환."""

def get_relevance_history_by_canonical_id_map(
    session: Session,
    ids: Iterable[int],
    limit: int = 3,
) -> dict[int, list[RelevanceJudgmentHistory]]:
    """canonical_project_id → 최근 history list 맵을 한 쿼리로 반환한다.

    archived_at DESC 기준으로 최대 limit 건씩 (모든 사용자).
    이력이 없는 canonical_project_id 는 빈 리스트로 포함된다.
    ids 가 비어 있으면 빈 dict 반환."""
```

### 2.3 배지 표시 규칙

| 상태 | 배지 | 색상 |
|------|------|------|
| 현재 사용자 `verdict='관련'` | 관련 | 초록 (`#16a34a`) |
| 현재 사용자 `verdict='무관'` | 무관 | 회색 (`#6b7280`) |
| 판정 없음 (미검토) | 빈 배지 (테두리만) | 연한 회색 테두리 |
| 비로그인 | 컬럼 전체 숨김 | — |

배지는 판정 없는 canonical도 반드시 표시한다. `null` 상태가 아닌 empty 배지로 렌더.
다른 사용자의 판정은 배지 색에 영향을 주지 않는다 — 현재 로그인 사용자 기준으로 배지를 결정한다.

### 2.4 툴팁 데이터 구조

마우스오버(`:hover` 또는 `mouseenter`) 시 표시:

```
현재 판정 (있을 경우):
  [관련 / 무관]  "사유 텍스트"  판정자: username  decided_at

이력 최근 3건:
  [관련 / 무관]  "사유 텍스트"  판정자: username
  보관 시각: archived_at  사유: [content_changed=📝 / user_overwrite=✏️ / admin_override=🔧]
```

**`archive_reason` 아이콘 매핑** (DB 허용값 → 아이콘):

| DB 값 | 의미 | 아이콘 |
|-------|------|--------|
| `content_changed` | 공고 내용 변경으로 자동 이관 | 📝 |
| `user_overwrite` | 사용자가 판정을 변경함 | ✏️ |
| `admin_override` | 관리자 조치 (Phase 5 이후) | 🔧 |

---

## 3. 관련성 엔드포인트 인터페이스

보안 공통: **모든 POST / DELETE 엔드포인트는 `current_user_required` + `ensure_same_origin`** 을 적용한다.
(`ensure_same_origin` 은 `app/auth/dependencies.py` — Phase 1b에서 구현됨.)

### 3.1 POST /canonical/{id}/relevance

판정 저장. 기존 판정이 있으면 이관(user_overwrite) 후 새 판정 INSERT. **원자적.**

```
Body: application/json
{
  "verdict": "관련" | "무관",
  "reason": "사유 (선택, 빈 문자열 허용)"
}

200: { "canonical_project_id": int, "verdict": str, "decided_at": "<ISO 8601 UTC>" }
400: verdict 값 오류
401: 비로그인
404: canonical_project 없음
```

**이관 트랜잭션 (단일 session, 동일 commit):**

```
1. canonical_projects.id == path id 존재 확인 → 없으면 404
2. relevance_judgments 조회 WHERE canonical_project_id=id AND user_id=current_user.id
3. 기존 row 있으면:
     relevance_judgment_history INSERT (archive_reason='user_overwrite', archived_at=utcnow)
     기존 row DELETE
4. relevance_judgments INSERT (verdict, reason, decided_at=utcnow)
5. commit
```

다른 사용자의 판정은 건드리지 않는다 — WHERE 절에 `user_id=current_user.id` 를 항상 적용.

### 3.2 DELETE /canonical/{id}/relevance

본인 판정을 이관(user_overwrite) 후 삭제.

```
204: No Content (성공)
401: 비로그인
404: canonical_project 없음 또는 본인 판정 없음
```

**트랜잭션:**

```
1. 본인 판정 조회 → 없으면 404
2. relevance_judgment_history INSERT (archive_reason='user_overwrite')
3. relevance_judgments row DELETE
4. commit
```

### 3.3 GET /canonical/{id}/relevance/history

판정 이력 조회 (모든 사용자).

```
Auth: current_user_required (비로그인은 배지 컬럼이 숨겨지므로 접근 없음)
쿼리 파라미터: limit (기본 10, 최대 50)

200: {
  "history": [
    {
      "verdict": "관련",
      "reason": "...",
      "decided_at": "<ISO 8601 UTC>",
      "archived_at": "<ISO 8601 UTC>",
      "archive_reason": "user_overwrite",
      "username": "alice"
    },
    ...
  ]
}
404: canonical_project 없음
```

---

## 4. 판정 모달 UI 흐름

```
배지 클릭
  └─ event.stopPropagation()  ← row 클릭과 충돌 방지 필수
  └─ 모달 열기
       ├─ 관련 / 무관 라디오 (기존 verdict 기본 선택. 미판정이면 선택 없음)
       ├─ 사유 textarea (현재 reason 미리 채움, 선택 입력)
       └─ 저장 / 취소 버튼

저장 버튼 클릭
  └─ fetch POST /canonical/{canonical_id}/relevance   (JSON body)
       ├─ 성공 → 모달 닫기 + 해당 배지 DOM 갱신 (full reload 없음)
       └─ 실패 → 모달 내 에러 메시지 표시

취소 버튼 / 모달 바깥 클릭
  └─ 모달 닫기
```

**상세 페이지 재사용**: 동일 모달 위젯을 Jinja2 partial(`_relevance_modal.html`)
또는 공통 CSS class로 상세 페이지(`detail.html`)에서 그대로 재사용한다.

---

## 5. 읽음 bulk 데이터 흐름

### 5.1 엔드포인트

```
POST /announcements/bulk-mark-read
POST /announcements/bulk-mark-unread
Auth: current_user_required + ensure_same_origin
Content-Type: application/json
```

**요청 body — 두 가지 모드:**

```json
// 모드 1 — 명시적 id 목록 (현재 페이지 선택)
{ "mode": "ids", "ids": [101, 102, 103] }

// 모드 2 — 필터 전체 선택 (아래 §5.2에서 결정한 방식)
{
  "mode": "filter",
  "filter": {
    "status": "접수중",
    "source": "IRIS",
    "search": "AI",
    "group": "off"
  }
}
```

**응답:**

```json
200: { "updated_count": 42 }
400: mode 누락, ids 비어 있음, filter 파싱 실패
401: 비로그인
422: filter 결과가 MAX_BULK_MARK 초과
```

### 5.2 "필터 전체 선택" 구현 방식 결정

#### 검토한 세 가지 방식

| 방식 | 개요 | 장점 | 단점 |
|------|------|------|------|
| **세션 저장** | 서버 세션에 현재 필터 상태를 저장해 두었다가 bulk 요청 시 재활용 | 클라이언트 전송 최소 | 상태 동기화 복잡. SSR 세션 인프라 추가 필요. bookmark/뒤로가기와 비일관 |
| **filter JSON 재전송** (채택) | 클라이언트가 현재 URL 쿼리 파라미터를 그대로 body에 담아 전송. 서버가 재쿼리해 id 추출 | Stateless. 별도 저장 없음. bookmark와 일관 | 요청 시점과 페이지 로드 시점 사이에 DB가 변경될 수 있음 (§5.2 한계 참조) |
| **hidden field** | 페이지 로드 시 id 목록을 HTML hidden input에 박아 두고 form submit | 구현 단순 | 수천 건 공고 시 HTML 비대. 페이지 로드 이후 수집된 신규 공고 누락 |

#### 최종 결정: **filter JSON 재전송**

서버가 stateless 상태를 유지하고, 클라이언트가 현재 URL의 쿼리 파라미터를 그대로
JSON body로 재전송한다. 서버는 이 파라미터로 페이지네이션 없이 전체 id를 추출해
bulk UPDATE를 수행한다.

**채택 이유:**
1. 서버에 추가 세션/상태 저장이 없어 구현이 단순하다.
2. 현재 URL의 쿼리스트링이 필터의 단일 진실 소스이므로 bookmark / 뒤로가기와 일관된다.
3. hidden field 방식보다 HTML 페이로드가 작고, 대량 공고에서도 안정적이다.

**한계 (허용 가능으로 판단):**
- 페이지 로드 이후 다른 사용자가 새 공고를 수집하면 "필터 결과 전체 M건" 표시와
  실제 bulk UPDATE 대상이 다를 수 있다.
- 해결책: 서버가 `updated_count`를 응답하고 UI가 "N건 처리됨" 확인 메시지를 표시.
  사용자는 새로고침으로 최신 상태를 확인할 수 있다.

#### 서버 측 상한 (MAX_BULK_MARK)

무제한 bulk를 방지하기 위해 환경변수 또는 상수로 상한을 설정한다:

```python
# app/web/constants.py (신규) 또는 app/scrape_control/constants.py 확장
import os

MAX_BULK_MARK: int = int(os.getenv("MAX_BULK_MARK", "5000"))
```

filter 조건 id 추출 결과가 `MAX_BULK_MARK`를 초과하면 422 반환.
`ids` 모드에서도 len(ids) > MAX_BULK_MARK 이면 422.

### 5.3 서버 id 추출 헬퍼 (subtask 00035-4 구현)

```python
def get_announcement_ids_by_filter(
    session: Session,
    *,
    status: AnnouncementStatus | None = None,
    source: str | None = None,
    search: str | None = None,
    group_mode: bool = False,
    limit: int,
) -> list[int]:
    """필터 조건에 맞는 announcement id 목록을 반환한다.

    group_mode=True 시 각 canonical group의 대표 announcement id만 반환한다.
    결과 수가 limit 초과 시 LimitExceededError를 발생시킨다.
    페이지네이션(offset)은 적용하지 않는다 — 전체 매칭 id가 필요하다."""
```

### 5.4 bulk UPSERT 헬퍼 (subtask 00035-4 구현)

```python
def bulk_mark_announcements(
    session: Session,
    *,
    user_id: int,
    announcement_ids: Iterable[int],
    is_read: bool,
    now: datetime | None = None,
) -> int:
    """announcement_ids에 대해 AnnouncementUserState를 일괄 UPSERT한다.

    is_read=True 시 read_at=now 설정. is_read=False 시 read_at=None.
    row 없으면 INSERT, 있으면 UPDATE. 변경된 row 수를 반환한다."""
```

---

## 6. UI 컴포넌트 배치

### 6.1 목록 테이블 컬럼 구성

| 컬럼 | 표시 조건 | 비고 |
|------|-----------|------|
| 체크박스 | 로그인 시만 | `data-ann-id` attribute |
| 제목 (링크) | 항상 | 읽음 bold/normal class 유지 |
| 기관 | 항상 | |
| 상태 | 항상 | |
| 마감일 | 항상 | |
| 소스 | 항상 | |
| 관련성 배지 | 로그인 시만 | `data-canonical-id` attribute |

헤더 체크박스: 현재 페이지 전체 선택/해제 토글.
관련성 컬럼 헤더: `<th>` 숨김 처리도 함께 (로그인/비로그인 분기).

### 6.2 bulk 선택 툴바

체크박스 1개 이상 선택 시 테이블 상단에 sticky 툴바 표시:

```
[N개 선택]  ·  [읽음 표시]  ·  [안읽음 표시]  ·  [선택 해제]
```

헤더 체크박스로 페이지 전체 선택 후 추가 안내 표시:

```
현재 페이지 N건 선택됨. [현재 필터 결과 전체 M건 선택] 링크
```

"현재 필터 결과 전체 M건 선택" 링크 클릭 시:
- UI 내부적으로 `mode=filter` 로 전환 (선택 집합 = 현재 필터 파라미터).
- M은 현재 `total` context 값 (index_page가 이미 계산해 노출함).

### 6.3 JS 이벤트 흐름 요약

```
체크박스 click
  → 선택 집합(Set<annId>) 갱신
  → 툴바 표시/숨김 + "N개 선택" 카운트 갱신

헤더 체크박스 click
  → 현재 페이지 전체 id 선택/해제
  → "필터 전체 선택" 링크 표시 여부 결정

"필터 전체 선택" 링크 click
  → 내부 상태를 filter 모드로 전환 (selectAll = true)
  → 툴바 "M건 선택" 표시

"읽음 표시" / "안읽음 표시" 버튼 click
  → mode=ids 이면:   POST /announcements/bulk-mark-{read|unread}  { "mode": "ids", "ids": [...] }
  → mode=filter 이면: POST /announcements/bulk-mark-{read|unread}  { "mode": "filter", "filter": {...} }
  → 성공 시: 해당 행 읽음 class 토글, 툴바 초기화, "N건 처리됨" 알림

배지 click
  → event.stopPropagation()   ← row click과 충돌 방지
  → 판정 모달 열기

row click (td 클릭 — 배지 / 체크박스 제외)
  → /announcements/{id} 이동
```

---

## 7. 쿼리 최적화 요약

| 추가 쿼리 | 조건 | 횟수 |
|-----------|------|------|
| `get_read_announcement_id_set` | 로그인 + 목록 있을 때 (Phase 1b 기존) | 1 |
| `get_relevance_by_canonical_id_map` | 로그인 + canonical_ids 있을 때 | 1 |
| `get_relevance_history_by_canonical_id_map` | 로그인 + canonical_ids 있을 때 | 1 |
| **합계** | 로그인 기준 기존 대비 | **+2** |

비로그인 시: read / relevance 쿼리 모두 skip. 추가 쿼리 0.

---

## 8. 검증 시나리오 → subtask 매핑

원문 검증 시나리오 1~10을 subtask별로 매핑한다.

| # | 시나리오 | 담당 subtask |
|---|----------|-------------|
| 1 | 배지 클릭 → 모달 → 저장 → 목록 갱신 | 00035-2 (엔드포인트) + 00035-3 (UI) |
| 2 | 판정 변경 시 이전이 History 이관 (user_overwrite) | 00035-2 |
| 3 | 공고 내용 변경 자동 이관 (content_changed) 회귀 | 00035-2 (regression 확인) |
| 4 | 다른 사용자 판정 배지/툴팁 보임 | 00035-3 |
| 5 | 체크박스 페이지 선택 → 읽음 → bold 해제 | 00035-5 |
| 6 | 필터 전체 선택 → 페이지 넘는 것도 일괄 처리 | 00035-4 (헬퍼) + 00035-5 (UI) |
| 7 | 비로그인 시 관련성 컬럼 / 체크박스 숨김 | 00035-3 + 00035-5 |
| 8 | N+1 회귀 없음 (목록당 추가 쿼리 2~3개) | 00035-2 + 00035-3 |
| 9 | ensure_same_origin 없는 POST 거부 | 00035-2 + 00035-4 |
| 10 | 여러 사용자 판정 공존, 각자 자기 것만 수정 | 00035-2 |

---

## 9. 주의사항

1. **시간 비교**: `as_utc()` 함수 (`app/db/models.py`) 사용.
   SQLite는 `DateTime(timezone=True)` 컬럼을 SELECT 시 naive datetime으로 반환하므로,
   `datetime.now(tz=UTC)` 등 tz-aware 값과 비교 전 반드시 `as_utc(value)` 적용.

2. **Jinja2 `<script>` 블록 내 Jinja 태그 리터럴 삽입 금지** (Task 00031 선례).
   JS에서 서버 값이 필요한 경우:
   - `data-*` attribute로 HTML에 주입 후 JS에서 `dataset.*` 읽기.
   - 또는 `<script type="application/json" id="page-data">...</script>` 블록 경유.

3. **배지 클릭 ↔ row 클릭 충돌**: 배지 `<button>` 클릭 핸들러에
   `event.stopPropagation()` 필수. 체크박스 클릭도 동일.

4. **미검토 배지**: 판정이 없는 canonical도 빈 배지를 표시한다.
   `relevance_map.get(canonical_id)` 가 `None`이어도 배지 `<span>` 요소 자체는 렌더.

5. **`archive_reason` 값**: DB 허용값은 `content_changed` / `user_overwrite` / `admin_override`.
   원문의 `user_changed`는 `user_overwrite`를 가리킨다 (`docs/schema_phase1a.md` §2.5 참조).

---

## 10. 후속 subtask 의존 순서

```
00035-1 (본 문서 — 설계 확정)
    ↓
00035-2  relevance repository 헬퍼 + 엔드포인트 (POST/DELETE/GET history)
    ↓
00035-3  relevance UI — 목록 배지/툴팁 + 판정 모달 + 상세 재사용
    ↓
00035-4  읽음 bulk repository 헬퍼 + 엔드포인트 (bulk-mark-read/unread)
    ↓
00035-5  읽음 bulk UI — 체크박스 + 툴바 + Gmail 스타일 필터 전체 선택
    ↓
00035-6  README.USER.md 사용법 추가 + 검증 시나리오 1~10 통합 확인
```
