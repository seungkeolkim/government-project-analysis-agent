# 00035-6 검증 시나리오 1~10 정적 확인 결과

검증 기준: `docs/relevance_ui_design.md` §8 시나리오 매핑표.
검증 방법: 소스 코드 정적 리뷰 (실서버 미가동).

---

## 결과 요약

| # | 시나리오 | 결과 | 담당 subtask |
|---|----------|------|-------------|
| 1 | 배지 클릭 → 모달 → 저장 → 목록 배지 갱신 (reload 없음) | ✓ 확인 | 00035-2 + 00035-3 |
| 2 | 판정 변경 시 이전 판정이 History 이관 (user_overwrite) | ✓ 확인 | 00035-2 |
| 3 | 공고 내용 변경 자동 이관 (content_changed) 회귀 없음 | ✓ 확인 | 00035-2 regression |
| 4 | 다른 사용자 판정 배지 툴팁 표시, 본인 배지 색 미영향 | ✓ 확인 | 00035-3 |
| 5 | 체크박스 페이지 선택 → 읽음 → bold 해제 (in-place) | ✓ 확인 | 00035-5 |
| 6 | 필터 전체 선택 → 페이지 넘는 공고 일괄 처리 | ✓ 확인 | 00035-4 + 00035-5 |
| 7 | 비로그인 시 관련성 컬럼 / 체크박스 전체 숨김 | ✓ 확인 | 00035-3 + 00035-5 |
| 8 | N+1 회귀 없음 (로그인 기준 추가 쿼리 2회) | ✓ 확인 | 00035-2 + 00035-3 |
| 9 | ensure_same_origin 없는 POST 거부 | ✓ 확인 | 00035-2 + 00035-4 |
| 10 | 여러 사용자 판정 공존, 각자 자기 것만 수정 가능 | ✓ 확인 | 00035-2 |

---

## 상세 확인 근거

### 시나리오 1 — 배지 클릭 → 모달 → 저장 → 목록 배지 갱신

- `_relevance_badge_macro.html:27`: `onclick="openRelevanceModal(this.closest('.rj-wrap')); event.stopPropagation();"` — 행 클릭과 충돌 방지 포함.
- `relevance.js:21`: `window.openRelevanceModal()` 정의, 기존 verdict/reason 으로 모달 초기화.
- `relevance.js:41`: `form.addEventListener('submit', ...)` → `fetch POST /canonical/{id}/relevance`.
- `relevance.js:69`: 성공 시 `updateBadge()` 호출 → DOM 인플레이스 갱신, `modal.close()`. 페이지 reload 없음.

### 시나리오 2 — 판정 변경 시 History 이관 (user_overwrite)

- `repository.py:1977`: 기존 판정 존재 시 `RelevanceJudgmentHistory INSERT` with `archive_reason=_ARCHIVE_REASON_USER_OVERWRITE`.
- 순서: History INSERT → flush → 기존 DELETE → flush → 신규 INSERT. UNIQUE 제약 안전.

### 시나리오 3 — content_changed 이관 회귀 없음

- `repository.py:98`: `_ARCHIVE_REASON_CONTENT_CHANGED = "content_changed"` — Phase 1a 구현체.
- 00035-2~5 에서 `set_relevance_judgment`, `delete_relevance_judgment` 신규 함수는 `user_overwrite` 만 사용. `content_changed` 경로(버전 갱신 로직)를 수정하지 않음.

### 시나리오 4 — 다른 사용자 판정 툴팁 표시

- `main.py:412`: `get_relevance_by_canonical_id_map()` — 모든 사용자 현재 판정 배치 조회.
- `_relevance_badge_macro.html:34`: `{% for rj in rj_list %}` — 전체 사용자 판정 툴팁 렌더.
- `_relevance_badge_macro.html:24`: 배지 색은 `my_rj` 단독 기준 (`my_rj.verdict`).
- `main.py:414`: `my_relevance_map = { cid: next((rj for rj in rjs if rj.user_id == current_user.id), None) ... }` — 현재 사용자 필터.

### 시나리오 5 — 체크박스 읽음 → bold 해제

- `bulk.js:204`: ids 모드 성공 시 `link.classList.remove('announcement-title-link--unread'); link.classList.add('announcement-title-link--read');` — in-place 갱신.
- `bulk.js:187`: `fetch(endpoint, { credentials: 'same-origin', ... })` — 세션 쿠키 전송.

### 시나리오 6 — 필터 전체 선택 → 전 페이지 일괄 처리

- `bulk.js:169`: `filterAllMode` 시 `mode: 'filter'` body 전송.
- `bulk.py` `_resolve_ids()`: `resolve_announcement_ids_by_filter()` 호출 — 페이지네이션 없이 전체 id 추출.
- `bulk.js:200`: filter 모드 성공 → `location.reload()`.

### 시나리오 7 — 비로그인 시 컬럼 숨김

- `list.html:128`: `{% if current_user %}<th class="col-check">` — Jinja2 조건, colspan 정확.
- `list.html:139`: `{% if current_user %}<th class="col-relevance">` 동일.
- `list.html:220`: empty row `colspan="{% if current_user %}7{% else %}5{% endif %}"`.
- `_relevance_modal.html:10`: `{% if current_user %}` 로 모달 전체 비렌더.
- `base.html:68`: `{% include "_relevance_modal.html" %}` — 비로그인 시 빈 문자열.

### 시나리오 8 — N+1 회귀 없음

- `main.py` group 모드(~390) + flat 모드(~490) 양쪽 모두 canonical_id 집합을 먼저 수집한 뒤 `get_relevance_by_canonical_id_map()` 1회, `get_relevance_history_by_canonical_id_map()` 1회 호출.
- 비로그인: `else` 분기에서 모두 `{}` 리턴 — 쿼리 0.
- 로그인: 기존 `get_read_announcement_id_set()` 1회 + relevance 2회 = 총 3회 추가 쿼리.

### 시나리오 9 — ensure_same_origin 미준수 POST 거부

- `relevance.py:49`: `POST /canonical/{id}/relevance` — `Depends(ensure_same_origin)`.
- `relevance.py:85`: `DELETE /canonical/{id}/relevance` — `Depends(ensure_same_origin)`.
- `bulk.py:110`: `POST /announcements/bulk-mark-read` — `Depends(ensure_same_origin)`.
- `bulk.py:133`: `POST /announcements/bulk-mark-unread` — `Depends(ensure_same_origin)`.
- `dependencies.py:183`: `ensure_same_origin()` 은 Origin 헤더 → Referer fallback → 없으면 통과(로컬 전제).

### 시나리오 10 — 여러 사용자 판정 공존

- `repository.py:1973`: `get_relevance_judgment()` 는 `WHERE canonical_project_id=? AND user_id=?` 로 현재 사용자 행만 조회.
- `delete_relevance_judgment()` 동일 조건.
- 다른 사용자의 `RelevanceJudgment` 행은 건드리지 않는다.

---

## 발견된 결함

### 결함 1 (경미, 비기능) — 응답 JSON 키 이름 불일치

- **위치**: `app/web/routes/bulk.py:120,128,143,151`
- **현상**: 응답 body 가 `{"updated": N}` 이나 설계 문서(`docs/relevance_ui_design.md` §5.1)는 `{"updated_count": N}` 을 명세.
- **영향**: `bulk.js` 의 성공 경로가 응답 body 를 사용하지 않으므로 (filter mode → `location.reload()`, ids mode → DOM 갱신) 사용자 가시 동작에 영향 없음.
- **담당**: 00035-4 에서 발생. 00035-6 에서 수정하지 않음.
- **권장 조치**: 후속 subtask 에서 `"updated_count"` 로 키 이름 통일 또는 설계 문서를 `"updated"` 로 업데이트.
