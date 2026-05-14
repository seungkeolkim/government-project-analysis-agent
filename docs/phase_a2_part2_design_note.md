# Phase A-2 Part 2 설계 노트 — 공고 포워딩 (HTML 빌더 + 라우터 + 모달 UI + 발송 이력)

> 산출물: 본 문서. **코드 변경 없음.** 첨부 `phase_a2_part2_prompt.md` "첫 subtask" 섹션의
> 모든 조사·결정 항목을 확정한다. 이후 subtask(00109-2 ~ 00109-12)는 본 노트의 결정을
> 그대로 따른다.

---

## 0. 탐사 요약 — 현재 코드베이스 상태

| 항목 | 현재 상태 |
| --- | --- |
| 공고 상세 핸들러 | `app/web/main.py::detail_page` (라인 773~908). `current_user_optional` 의존. 컨텍스트에 `announcement` / `canonical_id` / `current_user` / `user_organization_options` 등 주입. |
| 상세 템플릿 | `app/web/templates/detail.html`. 상단 `.detail-title-wrap` 에 제목 + 즐겨찾기 별 + 관련성 배지. 본문은 `viewers/<source_type>.html` (`iris` / `ntis` / `default` 3개만 존재, `_shared.html` 없음). 하단에 `_progress_section.html` → 관련성 판정 섹션 → 동일 과제 섹션 순. |
| 모달 패턴 | `_relevance_modal.html` / `_favorites_modal.html` — `base.html` 이 `</body>` 직전 `{% include %}`. 둘 다 `{% if current_user %}` 가드. `<dialog>` + `<form method="dialog">` + IIFE JS (`relevance.js` / `favorites.js`). `base.html` 이 `<script src>` 도 함께 로드. |
| 자동완성·chip 입력 | **기존 패턴 없음.** vanilla JS 로 신규 작성 필요. |
| `send_with_retry` | `app/email/sender.py`. 이미 `related_kind` / `related_id` / `requested_by_user_id` / `session` kwarg 를 받음 (키워드 전용). **보강 불필요.** 내부에서 `session.commit()` 을 직접 수행 (성공 시 1회 / 전체 실패 시 raise 직전 1회). |
| message builder | `app/email/message_builder.py` — `build_plain_text_message` 만 존재. multipart/HTML 빌더 신규 추가 필요. |
| admin 메일 라우터 | `app/web/routes/admin_email.py` — 라우터 레벨 `Depends(admin_user_required)` 로 **admin 전용**. prefix `/api/admin/email`. |
| SystemSetting 헬퍼 | `app.backup.service.get_setting` / `set_setting` 재사용. email 도메인은 `app/email/constants.py` 에 키 상수 + DEFAULT 상수. |
| `app/config.py::Settings` | `public_base_url` 류 필드 **없음**. |
| 라우터 wiring | `app/web/routes/__init__.py` 에서 router 객체 import → `main.py::create_app` 에서 `include_router`. |
| ORM 모델 | `EmailForwardLog` / `EmailForwardStatus` / `EmailSendRun` / `EmailSendRunStatus` 모두 Part 1 에서 완료. **스키마 변경 금지.** |
| 인증 의존성 | `current_user_optional` / `current_user_required` / `admin_user_required` / `ensure_same_origin` (`app/auth/dependencies.py`). |
| 조직 헬퍼 | `app.organizations.service.get_user_organization_ids(session, user_id)` → `list[int]`. `main.py::_load_user_organization_options` → `[{"id","name"}]`. |
| 테스트 픽스처 | `tests/conftest.py` 의 `db_session` / `test_engine`. 라우터 테스트는 `tests/web/test_admin_email_api.py` 패턴 (TestClient + `_register`/`_login`). |
| 시간 변환 | UTC 저장, 표시 직전 `app.timezone.format_kst` 또는 Jinja2 `kst_format` / `kst_date` 필터. |

---

## 1. (a) 공고 상세 페이지 — 버튼·섹션 배치

### 1-1. '메일로 보내기' 버튼 위치
`detail.html` 의 `.detail-title-wrap` div 안, 즐겨찾기 별(`fav-star`) 다음 / 관련성 배지 앞.
`viewers/*.html` 은 **본문 전용** 이므로 건드리지 않는다 (`_shared.html` 신규 생성도 하지 않음 —
범위 최소화).

- 로그인 사용자: 활성 버튼. `data-canonical-id` / `data-announcement-title` / 발신 조직 옵션은
  버튼 또는 상위 컨테이너의 `data-*` 로 전달 (relevance.js 의 `data-*` 패턴 동일).
- 비로그인: `disabled` + `title="로그인 후 사용 가능"`.
- `canonical_id` 가 `None` 인 공고: 포워딩은 canonical 단위 API 이므로 버튼을 **렌더하지 않는다**
  (canonical 없는 공고는 발송 이력 섹션도 생략). detail.html 의 기존 `{% if canonical_id %}`
  분기와 동일 패턴.

### 1-2. '발송 이력' 섹션 위치
`detail.html` 하단, **동일 과제 섹션 다음** (페이지 맨 끝). `{% if canonical_id %}` 가드 안.
빈 상태 / 목록 모두 JS 가 `GET /api/canonical/{id}/forward-logs` 결과로 채운다 — SSR 하지 않고
클라이언트 렌더 (행 expand 가 추가 API 호출이라 일관성 측면에서 JS 렌더가 단순).

---

## 2. (b)(c) 모달 패턴 + 자동완성·chip 입력

### 2-1. 모달 골격
`_forward_modal.html` 신규. `_relevance_modal.html` 구조를 그대로 모방:
- `{% if current_user %}` 가드 (비로그인은 모달 자체를 렌더하지 않음 — 버튼이 disabled 이므로 무해).
- `<dialog id="forward-modal">` + `<form id="forward-form" method="dialog">`.
- `base.html` 이 `</body>` 직전 `{% include "_forward_modal.html" %}` + `<script src="/static/js/forward.js">` 추가.
- 발신 조직 옵션은 `user_organization_options` 컨텍스트(이미 detail_page 가 주입)를 `<select>` `<option>`
  으로 SSR (relevance 모달과 동일 — `<script>` 안 Jinja 리터럴 금지 컨벤션).

### 2-2. JS 구성
- `static/js/forward.js` — IIFE 1개. `base.html` 에서 무조건 로드되지만 `#forward-modal` 없으면
  early-return (relevance.js / favorites.js 와 동일).
- chip 자동완성도 같은 IIFE 안에서 처리 (별도 파일로 쪼개지 않음 — 모달 한 곳에서만 쓰임).
- 외부 라이브러리 / CDN 금지 (PROJECT_NOTES 컨벤션). vanilla JS 만.

### 2-3. CSS
기존 `static/css/style.css` 에 `.forward-modal__*` / `.forward-history__*` / `.chip-*` 클래스를
추가한다. 별도 `style.forward.css` 신규 생성하지 않음 — `base.html` 의 `<link>` 가 `style.css` /
`progress.css` 2개뿐이고, 모달 1개 추가에 파일을 더 늘릴 이유가 없다. (시각 디테일은 후속
iteration 에서 조정될 수 있음 — 첨부 prompt "A-2 가 다른 phase 와 다른 점" 참고.)

---

## 3. (d) `send_with_retry` 인터페이스

확인 완료 — `app/email/sender.py::send_with_retry` 시그니처:

```python
def send_with_retry(
    transport, message, *,
    max_retry_count: int,
    related_kind: str,
    related_id: int | None,
    requested_by_user_id: int | None,
    session: Session,
) -> EmailSendRun
```

`related_kind='forward'`, `related_id=forward_log.id`, `requested_by_user_id=sender_user_id` 를
그대로 넘기면 된다. **보강 불필요.**

**중요 — commit 부수효과:** `send_with_retry` 는 내부에서 `session.commit()` 을 직접 호출한다
(성공 시 1회, 전체 재시도 실패 후 raise 직전 1회). 따라서 forwarding service 가 수신자 루프를
돌 때, **각 `send_with_retry` 호출이 forwarding service 의 session 을 commit 한다.** 이 사실이
아래 §6 의 commit 경계 설계를 좌우한다.

개별 send 실패 시 `send_with_retry` 는 마지막 예외를 **raise** 한다 (EmailSendRun row 는 이미
`failed` 로 commit 된 상태). forwarding service 는 이 예외를 **수신자별로 try/except 로 잡아서**
다음 수신자 처리를 계속한다.

---

## 4. (e)(f)(g) 신규 모듈 위치 결정

| 책임 | 위치 | 근거 |
| --- | --- | --- |
| Forward API endpoints | **`app/web/routes/forward.py` 신규** | `admin_email.py` 는 라우터 레벨 `admin_user_required` 로 admin 전용. 포워딩은 일반 로그인 사용자 + 비로그인 GET 혼재 → 분리가 자연스럽다. `progress.py` 가 동일 구조(엔드포인트별 권한 분기)의 선례. |
| Forwarding service | **`app/email/forwarding.py` 신규** | `sender.py` 는 "단일 함수 send_with_retry" 로 책임이 좁다. 트랜잭션 + 수신자 루프 + status 집계는 별도 service layer 가 맞다. 첨부 prompt 도 신규 파일로 명시. |
| Forward log repository 함수 | **`app/email/forwarding.py` 안에 함께 둔다** | 함수 2개(`list_forward_logs_for_canonical`, `get_forward_log_with_send_runs`)뿐이고 모두 `EmailForwardLog` / `EmailSendRun` 전용. `app/db/repository.py` 는 이미 4400줄로 비대 — 도메인 응집도 측면에서 `forwarding.py` 안에 두는 게 낫다. 별도 `forward_log_repository.py` 는 파일 2개로 쪼갤 만큼 양이 없어 over-engineering. message builder 확장은 `message_builder.py` 에. |

`app/web/routes/__init__.py` 에 `forward_router` import 추가, `main.py::create_app` 에서
`include_router(forward_router)` 추가.

---

## 5. (h) canonical → 최신 announcement 1건 선택 우선순위

`EmailForwardLog.canonical_project_id` 로 메일을 만들 때, 같은 canonical 에 `is_current=True` 인
Announcement 가 IRIS·NTIS 둘 다 존재할 수 있다. 메일 본문 컨텐츠로 쓸 1건을 다음 순서로 고른다:

1. `Announcement.canonical_group_id == canonical_project_id` **AND** `is_current == True` 인 row 조회.
2. 정렬: **source priority (IRIS 우선) → `scraped_at` 내림차순**.
   - source priority: `IRIS` 가 NTIS 보다 우선. 근거: IRIS 가 1차 수집원이고 상세 본문(`detail_html`)
     보유율이 높다. 동률이면 최근 수집(`scraped_at` desc).
3. 첫 row 를 사용. row 가 0건이면 `LookupError` (canonical 은 있으나 current announcement 없음 —
   비정상 상태).

> **사용자 결정 필요** 로도 §10 에 명시. 사용자가 "NTIS 우선" 또는 "수집일만으로" 를 원하면
> 정렬 키만 바꾸면 된다. 구현은 forwarding service 내부 헬퍼 `_pick_announcement_for_canonical`
> 한 곳에 격리해 변경 비용을 최소화한다.

---

## 6. forward_announcement 의 commit 경계 (명세)

`send_with_retry` 가 session 을 직접 commit 한다는 §3 의 사실 때문에, 트랜잭션을 다음 3단계로
나눈다. forwarding service 는 호출자(라우터)가 넘긴 `session` 을 그대로 쓴다.

### 단계 1 — forward_log 선(先) commit
1. `_pick_announcement_for_canonical` 로 announcement 1건 확정 (없으면 `LookupError`).
2. `subject` 빈 값이면 `build_default_forward_subject(...)` 로 자동 생성.
3. `EmailForwardLog` row 신규 INSERT:
   - `canonical_project_id`, `sender_user_id`, `sender_organization_id`,
     `recipient_addresses` (list), `recipient_count` (len),
     `subject`, `has_additional_message` (bool — additional_message 가 비어있지 않은지),
     `created_at = now_utc()`.
   - `status` 는 **임시값 `EmailForwardStatus.FAILED`** (모델에 default 없음 — 명시 필수).
     단계 3 에서 실제 결과로 덮어쓴다.
   - `success_count = 0`, `failure_count = 0`.
4. `session.commit()` — `forward_log.id` 를 확보하고, 이후 send 루프 중 crash 가 나도
   "포워딩 시도가 있었다" 는 사실이 DB 에 남도록 한다.

### 단계 2 — 수신자별 발송 루프
- `text_body` / `html_body` 를 1회 빌드 (모든 수신자 동일 본문 — 수신자별 개인화 없음).
- 수신자 N명에 대해 1명씩:
  1. `build_multipart_message(recipient=수신자, ...)` 로 `EmailMessage` 생성.
  2. `send_with_retry(transport, message, session=session, max_retry_count=...,
     related_kind='forward', related_id=forward_log.id,
     requested_by_user_id=sender_user_id)` 호출.
     - **이 호출이 session 을 commit 한다** (EmailSendRun row 1개 + 단계 1 이후 변경분).
       단계 1 에서 이미 commit 했으므로 이 시점 미커밋 변경분은 없어 안전하다.
  3. 성공(`return`) → local `success_count += 1`.
  4. 실패(`send_with_retry` 가 raise) → `try/except Exception` 으로 잡고 local `failure_count += 1`,
     loguru 경고 로그, **다음 수신자로 계속**. EmailSendRun 은 `send_with_retry` 가 이미 `failed`
     로 commit 해 둠.

### 단계 3 — 결과 update commit
- `forward_log.status` 결정:
  - `success_count == N` → `SUCCESS`
  - `success_count == 0` → `FAILED`
  - 그 외 (혼재) → `PARTIAL`
- `forward_log.success_count` / `failure_count` / `completed_at = now_utc()` 갱신.
- `session.commit()`.
- `ForwardResult(forward_log_id, status, success_count, failure_count)` 반환.

### 예외 정책
| 상황 | 처리 |
| --- | --- |
| 빈 `recipients` | `ValueError("발송 대상자가 없습니다")` — 단계 1 진입 전 |
| canonical 의 current announcement 0건 | `LookupError` — 단계 1 |
| `sender_organization_id` 가 sender_user 소속 아님 | `PermissionError` — 단계 1 진입 전 (라우터에서 선검증하지만 service 도 방어) |
| 개별 send 실패 | 예외 전파 안 함. `failure_count` 증가 후 루프 계속 |
| `build_transport_from_settings` / SystemSetting 읽기 실패 등 send 루프 시작 전 예외 | 전파. 단계 1 의 forward_log 는 이미 `FAILED` 로 commit 되어 있으므로 추가 정리 불필요. 라우터가 5xx 로 변환 |

> **함정 회피:** 수신자 N명에게 To 헤더를 묶어 1통 발송하지 않는다. 수신자별 1건 — 프라이버시
> + per-recipient EmailSendRun 추적. (첨부 prompt 명시.)

---

## 7. (i) SystemSetting `app.public_base_url`

`app/config.py::Settings` 에 `public_base_url` 류 필드 **없음** (확인 완료). 메일 본문의 공고 상세
URL prefix 가 필요하므로:

- **결정: SystemSetting 신규 키 `app.public_base_url` 도입.** default `http://localhost:8000`.
- 키 상수는 `app/email/constants.py` 에 `SETTING_KEY_APP_PUBLIC_BASE_URL` /
  `DEFAULT_APP_PUBLIC_BASE_URL` 로 추가 (email 도메인 외 키지만, 메일 본문 전용 용도라 같은 모듈에
  둔다 — 별도 모듈 신설은 over-engineering).
- 읽기: `get_setting(session, SETTING_KEY_APP_PUBLIC_BASE_URL) or DEFAULT_APP_PUBLIC_BASE_URL`
  (다른 email 키와 동일 fallback 패턴). 별도 admin UI 입력란은 Part 2 범위 밖 — 운영자가 필요
  시 DB 직접 수정 또는 후속 task. **신규 Alembic seed migration 금지** (코드 fallback 만).
- 상세 URL 조립: `f"{base_url.rstrip('/')}/announcements/{announcement.id}"`.

---

## 8. (j) HTML 메일 본문 인라인 CSS mockup

전부 인라인 `style="..."`. 외부 폰트 / CDN 이미지 금지. 모바일 친화 1열 레이아웃.

```
<div style="max-width:600px;margin:0 auto;padding:24px;
            font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
            color:#333;line-height:1.6;">

  <!-- 1. 공고 제목 (h2, 상세 페이지 링크) -->
  <h2 style="font-size:20px;margin:0 0 16px;">
    <a href="{detail_url}" style="color:#333;text-decoration:none;">{title}</a>
  </h2>

  <!-- 2. 메타 박스 (옅은 회색 #f5f5f5) -->
  <table style="width:100%;background:#f5f5f5;border-radius:6px;
                padding:12px 16px;font-size:14px;border-collapse:collapse;">
    <tr><td style="color:#888;width:90px;padding:2px 0;">발주기관</td><td>{agency}</td></tr>
    <tr><td style="color:#888;padding:2px 0;">상태</td><td>{status}</td></tr>
    <tr><td style="color:#888;padding:2px 0;">마감일</td><td>{deadline} {D-N if any}</td></tr>
    <tr><td style="color:#888;padding:2px 0;">예산</td><td>{budget_if_any}</td></tr>
  </table>

  <!-- 3. 공고 요약 (summary 또는 detail_text 첫 ~300자 + "...". 본문 없으면 섹션 생략) -->
  <div style="margin:16px 0;font-size:14px;">{summary_truncated}</div>

  <!-- 4. 보낸 사람 메시지 (있으면) — 좌측 굵은 회색 border 인용 박스 -->
  <div style="border-left:4px solid #888;padding:8px 14px;margin:16px 0;
              background:#fafafa;font-size:14px;">
    <div style="color:#888;font-size:12px;margin-bottom:4px;">보낸 사람 메시지</div>
    {additional_message}
  </div>

  <!-- 5. CTA 버튼 (진한 회색 #444) -->
  <div style="margin:24px 0;">
    <a href="{detail_url}"
       style="display:inline-block;background:#444;color:#fff;
              padding:10px 20px;border-radius:6px;text-decoration:none;
              font-size:14px;">공고 상세 보기</a>
  </div>

  <!-- 6. footer -->
  <hr style="border:none;border-top:1px solid #e0e0e0;margin:24px 0;">
  <div style="font-size:12px;color:#999;">
    보낸 사람: {sender_display}<br>
    이 메일은 정부사업 모니터링 시스템에서 발송되었습니다.
    회신은 발송자에게 직접 부탁드립니다.
  </div>
</div>
```

색상 팔레트: 텍스트 `#333`, 메타 박스 배경 `#f5f5f5`, CTA 버튼 `#444`, 보조 텍스트 `#888`/`#999`.
회사 CI 가 없으므로 의도적으로 중립 grayscale.

> 예산(`budget`) 필드: `Announcement` 모델에 전용 컬럼이 없다. `raw_metadata` JSON 에 있을 수
> 있으나 소스별로 키가 제각각 → **예산 행은 값이 확인될 때만 표시, 없으면 행 생략**. 무리하게
> raw_metadata 를 파싱하지 않는다 (00109-2 에서 단순하게: `raw_metadata.get("budget")` 류
> best-effort, 없으면 skip).

plain text 본문은 첨부 prompt §"plain text 본문 구성" 의 템플릿 그대로.

---

## 9. (k)(l)(m) 미리보기 / 자동완성 / 발송 이력 캐시

### (k) 미리보기 — **클라이언트 mock**
서버 endpoint(`POST .../forward/preview`) 도입하지 않는다 (over-engineering). 모달의 "본문
미리보기" 토글 클릭 시, JS 가 현재 textarea 내용 + 페이지에 이미 있는 공고 메타(제목/기관/상태/
마감)를 조합해 `<iframe srcdoc="...">` 로 간이 HTML 을 렌더한다. 미리보기 영역 상단에 안내 한 줄:
"실제 발송 본문과 다를 수 있습니다 — 참고용 미리보기입니다." 기본 접힘 상태.

### (l) 자동완성 debounce / chip 입력 키
- debounce: **250ms**.
- chip 확정 키: **Enter, 콤마(`,`)**. (Tab 은 폼 이동 접근성을 깨므로 chip 확정에 쓰지 않음.)
- 외부 이메일 직접 입력: 간단 regex(`/^[^@\s]+@[^@\s]+\.[^@\s]+$/`) 검증, 실패 시 입력칸 빨간 border.
- chip 최대 50개 (서버 제한과 일치). 초과 시 입력 차단 + 인라인 안내.
- chip 데이터는 항상 **이메일 주소** (내부 사용자 선택 시에도 email 이 chip value).

### (m) 발송 이력 expand 캐시
행 expand 시 `GET .../forward-logs/{id}/sends` 를 **첫 클릭 시 1회만 호출하고 결과를 DOM 에
보관**. 두 번째 펼침부터는 캐시된 DOM 을 toggle 만 한다. (발송 결과는 immutable — 한 번 끝난
포워딩의 EmailSendRun 은 바뀌지 않으므로 재요청 불필요.)

---

## 10. API 스펙 요약 (00109-5 / 00109-6 가 구현)

신규 라우터 `app/web/routes/forward.py`, prefix 없음 (`progress.py` 처럼 path 를 풀로 명시).

| Endpoint | 권한 | 비고 |
| --- | --- | --- |
| `POST /api/canonical/{canonical_id}/forward` | `current_user_required` + `ensure_same_origin` | recipients 1~50, subject ≤200(None 허용), additional_message ≤5000, sender_organization_id 본인 소속 검증(아니면 403). canonical 없으면 404. 성공 200 + `{forward_log_id,status,success_count,failure_count}`. |
| `GET /api/canonical/{canonical_id}/forward-logs` | 비로그인 허용 (`current_user_optional`) | `limit` default 50 / max 200. `recipient_addresses` 는 응답에서 제외. |
| `GET /api/canonical/{canonical_id}/forward-logs/{forward_log_id}/sends` | 비로그인 허용 | `EmailSendRun.related_kind='forward' AND related_id={forward_log_id}` 조회. |
| `GET /api/users/search?q=&limit=10` | `current_user_required` | `q` 1~50자, username/email 부분일치. `email IS NOT NULL` 만. `limit` default 10 / max 30. 정렬 **단순 `ORDER BY username`** (정확일치 우선정렬은 over-engineering — 도입 안 함). |

- `User` 모델에 `is_active` 컬럼 **없음** → 자동완성에서 active 필터 미적용 (첨부 prompt: "없으면 무시").
- `EmailSendRun` 의 `(related_kind, related_id)` 조회는 인덱스 없이 풀스캔. **Part 2 에서 인덱스
  추가 금지** (스키마 변경 = 별도 task). 로컬 규모라 실측 성능 이슈 발생 전까지 방치.

---

## 11. 사용자 결정 필요 (design note 마지막 — 미해결 의문점)

아래는 본 노트에서 합리적 default 를 정했으나, 사용자가 다르게 원할 수 있어 명시한다.
**구현은 default 로 진행하고**, 사용자가 검토 후 바꾸길 원하면 후속 iteration 으로 조정.

1. **HTML 메일 디자인 mockup** (§8) — 현재 grayscale 중립 디자인(`#333`/`#f5f5f5`/`#444`).
   회사 CI / 로고 / 색상을 넣고 싶은지? (현재: 외부 이미지 금지로 로고 없음.)
2. **미리보기 방식** (§9-k) — 클라이언트 mock 으로 시작. 실제 발송 본문과 100% 일치하는
   서버 사이드 미리보기가 필요하면 후속 task 로 `POST .../forward/preview` endpoint 추가.
3. **IRIS / NTIS 우선순위** (§5) — 같은 canonical 에 둘 다 있을 때 **IRIS 우선 → scraped_at 최신**.
   "NTIS 우선" 또는 "수집일만으로" 를 원하면 변경.
4. **첨부 파일명 목록 표시 여부** — 메일 본문에 `announcement.attachments` 의 **파일명 목록만**
   텍스트로 표시할지 여부. **현재 결정: 표시하지 않음** (첨부 prompt 범위 밖 + 사용자 결정 영역).
   첨부 파일 자체를 메일에 묶는 것은 명확히 범위 밖.
5. **default subject 포맷** — `[정부사업 모니터링] 공고 검토 요청: {title}`. title 100자 초과 시
   truncate("…"). 문구 변경 원하면 `build_default_forward_subject` 한 곳만 수정.

---

## 12. 범위 밖 (이번 task 에서 하지 말 것)

- **신규 Alembic migration 생성** — Part 1 에서 스키마 완료. 컬럼/인덱스/테이블 신설 일체 금지.
  `app.public_base_url` 도 SystemSetting 코드 fallback 으로만 처리 (seed migration 없음).
- **`EmailSendRun` 의 `(related_kind, related_id)` 인덱스 추가** — 스키마 변경이라 후속 task.
- **수신자 N명 To 헤더 묶음 1통 발송** — 수신자별 1건 발송 고정.
- **수신자별 본문 개인화** — 모든 수신자 동일 본문 (To 헤더만 다름).
- **외부 폰트 / CDN 이미지** — 인라인 CSS + system 폰트만.
- **첨부 파일 본문 첨부** — 메일에 announcement 첨부파일을 묶지 않음.
- **미리보기 서버 endpoint** — 클라이언트 mock 으로 시작.
- **Daily report / APScheduler / 개인화 필터 / 발송 예약·큐** — A-3 / A-4 영역.
- **`docs/email_transport_options.md` 갱신** — Transport 변경 없음.

---

## 13. subtask 별 산출물 매핑 (참고)

| subtask | 산출물 | 본 노트 근거 절 |
| --- | --- | --- |
| 00109-2 | `message_builder.py` 확장 (multipart/HTML/text 빌더 + default subject + base_url) | §3, §7, §8 |
| 00109-3 | `app/email/forwarding.py` — `forward_announcement` + `ForwardRequest`/`ForwardResult` | §4, §5, §6 |
| 00109-4 | `forwarding.py` 내 repository 함수 2개 | §4, §10 |
| 00109-5 | `app/web/routes/forward.py` 신규 + `main.py` 마운트 | §10 |
| 00109-6 | `GET /api/users/search` | §10 |
| 00109-7 | `tests/email/test_forwarding.py` + `tests/web/test_forward_routes.py` | §6 예외정책, §10 |
| 00109-8 | `_forward_modal.html` + 버튼 + `base.html` include | §1-1, §2 |
| 00109-9 | `forward.js` chip 입력 + 자동완성 | §2-2, §9-l |
| 00109-10 | 발송 이력 섹션 + expand | §1-2, §9-m |
| 00109-11 | `README.USER.md` 갱신 | — |
| 00109-12 | 통합 검증 + UI 다듬기 | — |
