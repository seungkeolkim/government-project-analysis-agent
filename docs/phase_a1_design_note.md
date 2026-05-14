# Phase A-1 설계 노트 — 메일 인프라 + 관리자 UI

본 문서는 Phase A-1 (task 00104) 의 **첫 subtask 산출물** 이다. 코드 변경 없이
이후 subtask 들이 기반으로 삼을 기존 코드베이스 패턴을 직접 확인·정리하고,
첨부 문서 (`phase_a1_prompt.md`) 가 첫 subtask 에 위임한 "명세 일관성 결정"을
박아둔다. 이후 subtask 는 이 노트만 보면 구현 방향을 확정할 수 있다.

----

## 1. Admin router 등록 패턴

### 1-1. 기존 admin router 구조

- 단일 모듈: `app/web/routes/admin.py` (~2,070 라인). 라우터 정의는
  파일 상단에서 한 번에 끝난다.

  ```python
  router = APIRouter(
      prefix="/admin",
      tags=["admin"],
      dependencies=[Depends(admin_user_required)],
  )
  ```

  - **prefix = `/admin`** — 라우터 단위로 한 번만 선언. 모든 admin URL 은
    `/admin/<sub>` 형식.
  - **`dependencies=[Depends(admin_user_required)]`** — 라우터 레벨로 걸어
    GET / POST / PATCH 등 모든 라우트를 admin-only 로 고정.
  - POST 계열은 추가로 `dependencies=[Depends(ensure_same_origin)]` 를 라우트별
    데코레이터에 더해 가벼운 CSRF 방어 (`/admin/backup/settings`,
    `/admin/backup/run` 이 그대로 따르고 있음).
- 모듈은 `app/web/routes/__init__.py` 에서 `admin_router` 로 re-export 되며,
  `app/web/main.py:434` 의 `fastapi_app.include_router(admin_router)` 한 줄로
  앱에 mount 된다.
- HTML 라우트와 JSON 폴링 라우트가 **같은 모듈** 에 공존:
  - HTML — `response_class=HTMLResponse`, Jinja2 TemplateResponse 반환
    (`/admin/scrape`, `/admin/backup` 등).
  - JSON — `response_class=JSONResponse`, dict 직렬화 반환
    (`/admin/scrape/status` 가 대표 사례). Pydantic response model 은
    **사용하지 않는다** — dict 를 그대로 돌려주거나 `JSONResponse(content=...)`
    로 감싼다.

### 1-2. `admin_user_required` 의존성

`app/auth/dependencies.py:148`:

```python
def admin_user_required(
    user: User = Depends(current_user_required),
) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자만 접근할 수 있습니다.",
        )
    return user
```

- 비로그인 → `current_user_required` 가 먼저 401 (`로그인이 필요합니다.`).
- 로그인했지만 `is_admin=False` → 403.
- handler 본문에서 `user: User = Depends(admin_user_required)` 로 한 번 더
  Depend 시키면 핸들러 안에서 사용자 객체를 얻을 수 있다 (기존 라우트들이
  이미 그렇게 한다).

### 1-3. 응답 모델 컨벤션 (이 task 에서 따라야 할 것)

- **Pydantic response model 강제 안 함.** dict 직접 반환이 표준
  (`/admin/scrape/status`, `/canonical/{id}/progress` 모두 동일).
- 요청 schema 는 Pydantic `BaseModel` 사용 가능 (`progress.py` 의
  `ProgressCreateIn` / `ProgressUpdateIn` 참고). `field_validator` 로
  도메인 검증 후 핸들러에 위임하는 방식.
- 에러는 `HTTPException(status_code=..., detail="...한글 메시지...")`. 422 /
  400 / 403 / 404 / 409 / 500 분기는 라우트 본문에서 직접.

### 1-4. **첨부 명세의 path 와 기존 컨벤션의 차이 — 결정 필요**

첨부 `phase_a1_prompt.md` 는 JSON endpoint 경로를 다음과 같이 명시:

```
GET  /api/admin/email/settings
PUT  /api/admin/email/settings
POST /api/admin/email/test-send
GET  /api/admin/email/send-runs
```

그러나 기존 admin router 는 prefix 가 `/admin` 이며 `/api/admin/*` 사용 사례가
없다 (전수 grep 확인). 두 가지 선택지:

| 선택 | 설명 | 트레이드오프 |
|---|---|---|
| **A. 첨부 spec 그대로 (`/api/admin/email/*`)** | 새 라우터 모듈 `app/web/routes/admin_email.py` 를 만들고 prefix=`/api/admin/email` 로 분리. `admin_user_required` + `ensure_same_origin` 직접 부착. | 첨부 문서·README 갱신과 일관. 단, 기존 admin 라우트와 경로 prefix 가 다르게 갈라진다. |
| **B. 기존 컨벤션 (`/admin/email/*`)** | `app/web/routes/admin.py` 끝에 4개 endpoint 추가. | 기존과 일관. 첨부 문서·README 의 경로를 모두 `/admin/email/*` 로 바꿔 적어야 한다. |

**채택: 선택 A** — 첨부 문서가 명시한 경로를 그대로 따르되, 새 모듈
`app/web/routes/admin_email.py` 를 만들어 기존 admin.py 의 비대화도 피한다.
새 모듈은 자체 `APIRouter(prefix="/api/admin/email", ...)` 를 들고
`app/web/routes/__init__.py` 의 `admin_email_router` 로 re-export,
`app/web/main.py` 에서 `include_router(admin_email_router)` 추가.

> 이 결정이 깨지면 phase_a1_prompt 의 README.USER.md / docs/email_transport_options.md
> 갱신 항목도 함께 흔들리므로, 다른 subtask 가 이 노트를 우선으로 본다.

### 1-5. HTML 진입 위치 — 「시스템 관리」 탭 신규 sub_tab

기존 `app/web/templates/admin/base.html` 의 2-level 탭 네비 (line 30~64) 는
다음 구조:

```
[공고 수집 제어]   ← top_tab=scrape_group
  [수집 제어] [sources.yaml] [스케줄]
[조직 관리]        ← top_tab=org_group
  [조직 구성] [사용자 관리]
[이용 통계]        ← top_tab=usage_group (단일 sub)
[시스템 관리]      ← top_tab=system_group
  [시스템 백업]
```

A-1 의 메일 설정 / 테스트 발송 / 발송 이력 3 섹션은 **시스템 관리 top_tab 의
새 sub_tab `email` 로 추가** 한다. base.html 의 `{% elif top_tab == 'system_group' %}`
블록 (line 59~63) 에 sub_tab `email` 링크 한 줄을 더한다.

- HTML 라우트: `GET /admin/email` (HTML 페이지) — 기존 admin router 또는
  새 admin_email_router 어느 쪽에 두어도 무관하나, 1-4 의 결정과 정합성을
  유지하기 위해 admin_email_router 안에 두지 않고 **`app/web/routes/admin.py`
  안에 기존 backup_page 와 같은 패턴으로 추가** 한다 (HTML 은 `/admin/*`,
  JSON 은 `/api/admin/email/*` 로 분리). 페이지는 3 섹션 골격만 SSR 로 그리고
  실제 데이터 fetch / submit 은 JS 가 `/api/admin/email/*` JSON endpoint 호출.
- 새 template 파일: `app/web/templates/admin/email.html`. backup.html (~215 라인) 의
  섹션 구조 (`<section class="admin-section">`, `<h3 class="admin-section__heading">`,
  `<p class="admin-state__muted">`, `<form class="admin-form">`,
  `<button class="admin-button admin-button--primary">`) 를 그대로 따라 시각적
  일관성 확보.

----

## 2. SystemSetting (00094) 사용 패턴

### 2-1. 데이터 모델

`app/db/models.py:1908` 의 `SystemSetting`:

| column | type | 비고 |
|---|---|---|
| `key` | `String(128)` PRIMARY KEY | 예: `"backup.cron_expression"` |
| `value` | `Text` nullable | 평문 문자열 저장. 숫자/bool 등은 호출 측이 해석. |
| `updated_at` | `DateTime(timezone=True)` NOT NULL | default/onupdate=`_utcnow` |

테이블 자체는 alembic `c2d3e4f5a6b7_backup_system_tables` (2026-05-08) 에서
이미 생성됨. **email.* 키는 row 추가만 필요** — 테이블 ALTER 불필요.

### 2-2. service / repository

`SystemSetting` 전용 service / repository 모듈은 **없다**. 백업 도메인 코드
`app/backup/service.py:128` 에 같이 들어 있다:

```python
def get_setting(session: Session, key: str) -> str | None:
    row = session.get(SystemSetting, key)
    return row.value if row is not None else None


def set_setting(session: Session, key: str, value: str | None) -> None:
    row = session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=value, updated_at=now_utc()))
    else:
        row.value = value
        row.updated_at = now_utc()
```

- upsert 는 `session.get(SystemSetting, key)` 후 row 가 없으면 add, 있으면
  value/updated_at 갱신. `session.flush()` 는 호출하지 않으며 commit 은
  호출자 (`session_scope()` context manager) 가 담당.
- 키 이름은 `app/backup/constants.py` 의 `SETTING_KEY_BACKUP_*` 상수에
  모아져 있음.

### 2-3. A-1 에서 채택할 SystemSetting 코드 위치 결정

- 백업 도메인에 묶지 않고 **email 도메인 전용** 으로 둔다 — 모듈 응집을
  유지하기 위함. 새 위치:

  ```
  app/email/
    constants.py      # SETTING_KEY_EMAIL_*, DEFAULT_EMAIL_*
    config.py         # load_m365_oauth_config() — get_setting 으로 7개 키 읽어 dataclass 반환
  ```

  `app/backup/service.py` 의 `get_setting` / `set_setting` 두 함수는 그대로
  **재사용** (import 해서 사용). 별도 헬퍼를 만들 필요 없음. 향후 backup /
  email 외 도메인이 또 추가되면 `app/system_setting/service.py` 같은 공용
  모듈로 끌어올리는 리팩터링을 검토할 수 있으나, 본 task 범위 밖.

### 2-4. Default seed 추가 방법 — 결정

기존 패턴 (백업) 은 SystemSetting row 를 명시적으로 seed 하지 **않고**,
조회 시점에 코드 레벨 fallback:

```python
cron = get_setting(session, SETTING_KEY_BACKUP_CRON) or DEFAULT_BACKUP_CRON
```

(`app/backup/service.py:359`)

첨부 phase_a1_prompt 는 "seed 는 application 첫 기동 시 또는 별도 migration
script 로 INSERT" 로 두 선택지 제시. 두 가지를 비교:

| 선택 | 설명 | 트레이드오프 |
|---|---|---|
| **A. fallback only (backup 과 동일)** | row 가 없으면 코드의 `DEFAULT_*` 상수로 응답. row 가 생기는 시점은 `PUT /api/admin/email/settings` 가 사용자 입력으로 첫 저장할 때. | 기존 backup 패턴과 일관. seed migration 작성 없음 — migration 면적 최소화. 단, `GET /api/admin/email/settings` 의 응답 의미가 \"DB row 가 아닌 default\" 와 \"실제 저장된 값\" 을 코드 한 곳에서 구별해야 함. |
| **B. Alembic data migration 으로 7개 row 미리 INSERT** | upgrade() 에서 `op.bulk_insert` 로 7개 default row 를 삽입. downgrade 에서 `op.execute("DELETE ...")`. | 첫 기동 직후에도 SystemSetting row 가 항상 존재해서 조회 일관성 ↑. 단 기존 패턴과 불일치하고, seed-only data migration 은 운영 중 다시 돌릴 일이 없음. |

**채택: A (fallback only)** — 기존 패턴 일관성 우선. 단, default 상수는
`app/email/constants.py` 한 곳에 모아 둔다. `client_secret` 만 default 가 빈
문자열 (가지지 않는 게 안전 — 화면 mask 표시도 빈 값 분기). 다만 본
task 의 **subtask 00104-2** 가 "msal 의존성 추가 + SystemSetting email.* 7개
키 seed" 로 명시되어 있으므로, **00104-2 는 코드 상의 default 상수 7개를
도입하는 작업으로 해석한다** (migration 으로 row 를 미리 만들지 않음).
이 해석은 첫 subtask 의 결정으로 박는다 — 00104-2 의 Coder 는 이 노트를
근거로 fallback 방식을 채택한다.

### 2-5. A-1 SystemSetting 키 7개 (첨부 그대로 옮김)

```
email.transport.type            string  default "m365_oauth"
email.m365.tenant_id            string  default ""           (IT 제공값)
email.m365.client_id            string  default ""           (IT 제공값)
email.m365.client_secret        string  default ""  *secret  (IT 제공값, 평문 저장)
email.m365.sender_address       string  default "gov-agent-noreply@innodep.com"
email.from_display_name         string  default "정부사업 모니터링 봇"
email.max_retry_count           int     default "2"          (str 로 저장, int(...) 캐스트)
```

- `email.max_retry_count` 의 DB 저장 형식: SystemSetting.value 는 Text 이므로
  문자열 `"2"` 로 저장하고 호출 측이 `int()` 캐스트. 잘못된 값은
  `int(value)` 가 던지는 `ValueError` 를 catch 해 `DEFAULT_EMAIL_MAX_RETRY_COUNT`
  (=2) 로 폴백 — backup 의 `_get_max_count_from_db` 와 동일 방어 패턴.
- `email.m365.client_secret` 의 응답 mask 규칙은 4-3 참조.

----

## 3. 「시스템 관리」 탭 frontend 패턴

### 3-1. 템플릿 형태

- **Jinja2 SSR** — 별도 SPA / React / Vue 없음. `Jinja2Templates(directory=...)` 의
  `TemplateResponse(request, "admin/<file>.html", context)` 로 페이지를 그린다
  (`app/web/routes/admin.py:1936`).
- 모든 admin 페이지는 `admin/base.html` 을 `{% extends %}` 하고
  `{% block admin_content %}` 안에 본문을 그린다. 상단/하단 탭과 flash 박지는
  base 가 그려준다.
- 페이지 단위 KST 시각 표시는 Jinja 필터 `kst_format`. backup.html 의
  `{{ (h.executed_at | kst_format) or '—' }}` 가 표준 사용 패턴.

### 3-2. 시각 일관성용 CSS 클래스 (반드시 따라야 함)

`app/web/static/css/style.css` 의 `.admin-*` 계열 (line 1059~):

| 클래스 | 용도 |
|---|---|
| `.admin-section` | 한 섹션을 둘러싸는 박스. h3 + 본문 묶음. |
| `.admin-section__heading` | 섹션 제목 (`<h3>`). |
| `.admin-state__muted` | 보조 설명 텍스트 (작은 회색 글씨). 섹션 부제·empty state 모두 이걸 쓴다. |
| `.admin-flash` + `--success` / `--error` / `--warning` | 상단/섹션내 안내 박지. |
| `.admin-form` (+ `--start`) | form 컨테이너. flex column 정렬 + 라벨 간 gap. |
| `.admin-form` 내부 `<label class="admin-schedule__text">` | input + label 묶음 (backup 페이지가 이 패턴 사용). |
| `.admin-schedule__input` | text/number input. |
| `.admin-button` + `--primary` / `--secondary` / `--danger` | 버튼. disabled 상태 분기 있음. |
| `.admin-table` | 결과 테이블. thead 강조 + tbody 셀 padding. |
| `.admin-badge` + `--running` (초록) / `--idle` (회색) | 상태 배지. backup history 의 성공/실패 표시가 사용. |

A-1 의 3 섹션 모두 위 클래스를 그대로 사용한다 — 새 CSS 클래스 도입을
**피한다** (필요 시에만 추가). 신규 추가가 필요한 경우 후보:

- 메일 설정 form 의 client_secret "변경 토글" 버튼 — 기존 `.admin-button--secondary`
  를 작은 사이즈로 재사용 (border + 회색 톤). 별도 CSS 없이 inline style 이나
  기존 클래스 조합으로 처리 권장.
- 테스트 발송 결과의 성공/실패 박스 — 이미 `.admin-flash--success` /
  `.admin-flash--error` 가 정확히 같은 의미라 그대로 사용.
- 발송 이력 status 배지 — `.admin-badge--running` (sent 초록) /
  `.admin-badge--idle` (failed 회색) 또는 빨간 톤이 필요하면 `--danger` 계열
  하나만 추가. 시각 컨벤션에 맞춰 신규 클래스를 추가하는 경우 본 노트의 결정
  근거로 한 줄 주석을 단다.

### 3-3. JS 위치 / 패턴

- `app/web/static/js/<name>.js` 평탄 디렉터리. 빌드 시스템 없음 — 브라우저가
  바로 fetch.
- 기존 JS 가 사용하는 패턴 (`progress.js`, `relevance.js`, `bulk.js`):
  - vanilla JS (ES2020+). 프레임워크 없음.
  - HTTP 호출은 `fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({...}) })`.
  - CSRF 안전성은 same-origin POST 가 cookie 를 자동 송신 + `ensure_same_origin`
    가 Origin/Referer 검증. JS 측 추가 토큰 헤더 없음.
- A-1 신규 JS 파일: `app/web/static/js/admin_email.js` — 3 섹션 모두를 묶는
  단일 파일. 섹션별 init 함수 (initSettingsSection / initTestSendSection /
  initSendRunsSection) 로 분리. base.html 에서 sub_tab='email' 일 때만
  `<script src="/static/js/admin_email.js">` 를 include (admin/email.html
  template 의 페이지 끝에 단일 `<script>` 태그).

### 3-4. flash / PRG 패턴

- 기존 admin POST → GET redirect (PRG) 형식: `RedirectResponse(url=f"/admin/...?flash=...&flash_level=...", status_code=303)`.
- A-1 의 4개 JSON endpoint 는 **PRG 가 아닌 fetch 응답 기반**. flash 는
  서버에서 만들지 않고 JS 가 응답 status / 본문을 보고 섹션 내부에
  `.admin-flash` 박스를 동적으로 그린다.
- HTML 페이지 `GET /admin/email` 자체는 단순 SSR — flash 쿼리 파라미터를
  받지 않아도 된다 (모든 사용자 작업이 JS fetch 로 처리되므로).

----

## 4. API 명세 일관성 결정 (첨부가 첫 subtask 에 위임한 항목)

### 4-1. EmailSendRun.attempt_count / error_message 의 의미

첨부 문서 검증 #4 가 "첫 에러 / 마지막 에러 / 누적" 중 결정 위임.

**채택**:

- `attempt_count` (Integer NOT NULL default 1) — **전체 시도 횟수 누적**.
  1차 시도 + 재시도 횟수의 합. 예) max_retry_count=2 일 때 1차 + 재시도 2회
  = `attempt_count=3` 까지 가능.
- `error_message` (Text nullable) — **마지막 시도의 예외 메시지만**.
  중간 시도 에러는 row 에 영구 저장하지 않고 loguru 에만 남긴다 (서버 로그
  로 디버깅 가능). 최종 결과가 success 인 경우 `error_message=None`.
- `status='sent'` 인데 attempt_count > 1 이면 "중간에 실패했다가 재시도 끝에
  성공" 을 의미. error_message 는 NULL.
- `status='failed'` 인 경우 error_message 는 마지막 (= 최후) 예외의
  `f"{type(exc).__name__}: {exc}"` 형식 문자열.

근거:

- 단순함 우선 — "어떤 시점의 에러를 저장할 것인가" 의 분기를 없앤다.
- 디버깅 시 "처음 어떤 에러가 났는가" 가 궁금하면 loguru log 의 시각으로
  EmailSendRun.created_at 근방을 보면 된다 — DB row 1개당 string 1개 한도
  로 정보를 단순화하는 게 운영상 가독성이 더 높다.
- 첨부 문서 검증 #4 의 "또는 최종 NULL" 옵션은 채택하지 않는다 — 실패한
  row 의 error_message 가 NULL 이면 status 만 보고 원인 파악 불가능.

테스트 시나리오 (subtask 00104-10) 가 본 결정을 그대로 따라야 한다:

- 첫 시도 실패 + 두 번째 시도 성공 → `status='sent'`, `attempt_count=2`,
  `error_message=None`.
- max_retry_count 초과 → `status='failed'`, `attempt_count=max_retry_count+1`,
  `error_message="ClassName: <마지막 예외 메시지>"`, 마지막 예외가 그대로
  호출자로 전파됨.

### 4-2. EmailSendRun 의 sent_at

- `sent_at` (DateTime nullable) — 마지막 성공 시도의 종료 시각 (UTC).
  실패 row 는 NULL. 첨부 문서와 동일.

### 4-3. `client_secret` API 노출 정책

- **저장은 평문**. SystemSetting.value 컬럼에 그대로. A-1 범위 결정.
- `GET /api/admin/email/settings` 응답의 `client_secret_masked` 형식:

  | 저장 값 | 응답 |
  |---|---|
  | NULL / 빈 문자열 / `""` | `null` |
  | 4자 이하 (예: `"abc"`) | `"****"` (마지막 자릿수 노출하지 않음 — 너무 짧으면 값 추정 위험) |
  | 5자 이상 | 마지막 4자 노출, 앞쪽은 `****` 1개 prefix. 예: `"abcdef1234"` → `"****1234"` |

  첨부 문서 예시 `"****abcd"` 와 일치. 첨부는 prefix/suffix 길이를 명시하지 않
  았으므로 본 노트에서 위와 같이 박는다 (보안적으로 4자 미만이면 mask 전체로
  바꾸는 게 합리적).

- `PUT /api/admin/email/settings` 의 `client_secret` 필드:
  - body 에 키 자체가 없거나 빈 문자열 (`""`) → **기존 값 유지**. SystemSetting
    의 row 를 건드리지 않는다.
  - 명시적으로 새 값이 들어와야만 update.
  - 검증: 별도 길이/문자 제한 없음 (IT 가 전달한 값 그대로 받아야 하므로).
- 평문 저장 DB-level 암호화는 **A-1 범위 밖**. PROJECT_NOTES 의 보안 후속
  작업으로 표기만 (subtask 00104-14 에서 처리).

### 4-4. 재시도 정책 세부

- 재시도 횟수 = `email.max_retry_count` SystemSetting (default 2). 0 허용
  (재시도 안 함). UI 의 number input min=0 max=5 (첨부 문서).
- 재시도 간 backoff = **2 초 단순 sleep** (`time.sleep(2)`). exponential
  backoff 미도입.
- 재시도는 transport.send() 가 던지는 **모든 Exception** 에 대해 발동.
  HTTPError / SMTPException / RuntimeError 모두 동일하게 catch.
- max_retry_count 초과 시 마지막 예외가 그대로 호출자로 raise. EmailSendRun
  row 는 `status='failed'`, `error_message="...마지막..."`, `sent_at=None`
  로 commit 된 후 raise.
- `send_with_retry` 의 SQLAlchemy Session 인자 — DB 트랜잭션 경계는 **본
  함수가 자체 commit** 한다 (호출자 트랜잭션과 분리). 이는 발송 실패 시에도
  EmailSendRun row 가 남도록 보장하는 데 핵심. 호출자가 자기 트랜잭션 안에서
  본 함수를 호출하는 경우, 본 함수가 별도 `session_scope()` 또는 nested
  savepoint 로 row 를 별도 commit 한다.

  **결정**: 호출자가 session 을 주입하지 않고, `send_with_retry` 내부에서
  `with session_scope() as session: ...` 로 자체 세션을 연다. signature
  에서 `session` 인자를 제거하고, 대신 `session_scope` factory 를 import 해
  사용. 이 결정은 첨부 문서의 sender.py 의사 signature 와 살짝 다른 부분이며
  본 노트가 우선한다 — 호출자 트랜잭션과 발송 실패 row 의 영속성을 격리하는
  게 운영상 더 안전.

### 4-5. transport_type / related_kind 도메인 값

- `transport_type` 컬럼은 String. A-1 에서 채워질 유일한 값은 `"m365_oauth"`.
  CHECK constraint 는 두지 않음 — 향후 옵션 C 추가 시 ALTER 없이 새 값을
  채울 수 있도록.
- `related_kind` 컬럼은 String nullable. A-1 에서 `"test_send"` 만 채움.
  CHECK constraint 없음 — A-2 의 `"forward"`, A-3 의 `"daily_report"` 가
  ALTER 없이 추가될 수 있도록.

----

## 5. Alembic migration 추가 절차

### 5-1. 파일 위치 / 명명

- 디렉터리: `alembic/versions/`.
- 최근 파일 명명 규칙 (실제 파일명 기준):

  ```
  20260508_0900_b8d7e2c45f01_announcement_progress_tables.py
  └──날짜─┘ └시각┘ └revision id┘ └slug──────────────────┘
  ```

  - 날짜 = `YYYYMMDD` (KST 작성일).
  - 시각 = `HHMM`.
  - revision id = 12자 hex (Alembic 기본 길이).
  - slug = snake_case, 영문.

- 현재 head revision = `b8d7e2c45f01` (announcement_progress_tables). A-1 의
  EmailSendRun migration 은 `down_revision = "b8d7e2c45f01"`.
- A-1 migration 파일명 (예시): `20260513_<HHMM>_<hash>_email_send_runs.py`.
  hash 는 `python -c "import secrets; print(secrets.token_hex(6))"` 등으로 생성.
  (Coder agent 가 결정).

### 5-2. migration 본문 스타일

`alembic/versions/20260508_0900_b8d7e2c45f01_*.py` 의 패턴을 그대로 따른다:

- 모듈 docstring 에 변경 요약 + 다운그레이드 정책 + 이식성 메모.
- `upgrade()` / `downgrade()` 함수에 한국어 docstring 1줄 이상.
- 모든 FK / UNIQUE / INDEX / CHECK constraint 에 **명시 이름**:

  ```python
  sa.ForeignKeyConstraint(
      ["requested_by_user_id"], ["users.id"],
      name="fk_email_send_runs_requested_by_user_id",
      ondelete="SET NULL",
  )
  ```

- `DateTime(timezone=True)` 컬럼은 `server_default=sa.text("CURRENT_TIMESTAMP")`
  로 raw INSERT 호환성 확보. ORM 의 `default=_utcnow` 가 통상적으로 우선
  적용되므로 server_default 는 안전망 역할.
- `Enum` 컬럼은 `native_enum=False` + 명시 CHECK constraint:

  ```python
  sa.Column(
      "status",
      sa.Enum("sent", "failed",
              name="email_send_run_status",
              native_enum=False),
      nullable=False,
  ),
  sa.CheckConstraint(
      "status IN ('sent', 'failed')",
      name="ck_email_send_runs_status",
  ),
  ```

  (announcement_progress migration 의 라인 83~94, 144~147 가 표준 사례.)

### 5-3. 인덱스

첨부 문서가 명시한 3 인덱스:

```python
op.create_index(
    "ix_email_send_runs_created_at",
    "email_send_runs",
    [sa.text("created_at DESC")],
)
op.create_index(
    "ix_email_send_runs_status_created_at",
    "email_send_runs",
    ["status", sa.text("created_at DESC")],
)
op.create_index(
    "ix_email_send_runs_requested_by_user_id",
    "email_send_runs",
    ["requested_by_user_id"],
)
```

> 주의: `sa.text("created_at DESC")` 는 SQLite/Postgres 모두 expression index
> 지원. 다만 SQLite 의 expression index 는 3.9 이상에서만 동작 — 이미 다른
> 마이그레이션이 동일 패턴을 쓰지 않으므로, 호환성 우려가 있으면 단순
> ascending 인덱스 (`["created_at"]`) 로 두고 조회 SQL 에서 `ORDER BY
> created_at DESC` 만 명시해도 SQLite/Postgres 모두 효율적이다. **채택: 단순
> ascending 인덱스 + ORDER BY 측에서 DESC**. 인덱스 정의가 dialect 독립적이
> 어 안전.

### 5-4. 바인드 마운트 정책 (00095)

- `docker-compose.yml` 의 `app` / `scraper` 서비스가 모두 alembic 디렉터리를
  바인드 마운트 (line 110, 173):

  ```yaml
  - ./alembic:/app/alembic:ro
  - ./alembic.ini:/app/alembic.ini:ro
  ```

- `:ro` 이므로 컨테이너 안에서 `alembic revision` 으로 새 revision 파일을
  생성할 수 없다. **호스트에서** alembic CLI 를 실행하거나, 일회성으로 `:ro`
  를 제거한 dev 모드로 띄워서 생성한다.
- `entrypoint.sh` 가 컨테이너 기동 시 `alembic upgrade head` 를 자동 수행
  하므로, **호스트에서 파일을 작성 → `docker compose up` → 자동 upgrade**
  순서.
- Coder agent 는 호스트 파일시스템에서 직접 파일을 작성하면 충분 (alembic CLI
  실행 불필요 — revision id 와 down_revision 을 손으로 박는다).

### 5-5. ORM 모델 등록

`alembic/env.py` 의 import 블록 (line 20~33) 에 신규 모델을 추가해야
autogenerate 가 인식한다. 단, A-1 의 이 migration 은 hand-written 이므로
autogenerate 와 무관 — env.py 수정은 **불필요**.

대신 `app/db/models.py` 에 `class EmailSendRun(Base): ...` 를 선언하면
SQLAlchemy 의 metadata 가 인식하므로 ORM 사용은 가능. 단, 후속 subtask
(transport / sender / API) 가 EmailSendRun 모델을 사용하므로 migration
subtask (00104-3) 와 별개로 ORM 클래스를 어디 두느냐가 결정 포인트.

**채택**: `EmailSendRun` ORM 은 `app/db/models.py` 안에 다른 모델들과
함께 선언 (BackupHistory / AnnouncementProgress 와 같은 패턴). 별도
`app/email/models.py` 는 만들지 않는다 — 다른 도메인 모델 (backup,
suggestions) 도 단일 `models.py` 에 모여 있어서 일관성이 더 중요.

----

## 6. 패키지 의존성 — msal 추가

### 6-1. 형식

- `pyproject.toml` (line 5~95) 의 `[project] dependencies` 리스트가 단일
  진실 소스. `requirements.txt` 는 존재하지 않는다.
- 항목 형식: `"패키지명>=하한,<상한"` (예: `"loguru>=0.7,<1.0"`). 메이저
  락에 상한을 박는다.
- msal 추가 위치: `dependencies` 리스트 끝에 새 항목 추가. 그룹 주석으로
  "Phase A-1 — M365 OAuth 메일 발송" 같은 마커 한 줄 더하면 가독성 ↑.

  ```toml
  # Phase A-1 — M365 OAuth XOAUTH2 SMTP 발송용 Microsoft Authentication Library.
  # ConfidentialClientApplication + in-memory token cache 만 사용한다.
  "msal>=1.28,<2.0",
  ```

### 6-2. uv.lock 갱신

- 프로젝트 루트에 `uv.lock` 파일이 존재 (size 미확인). uv 또는 pip-tools 가
  관리. 의존성 변경 시 lock 파일 재생성이 필요할 수 있다.
- 정확한 갱신 명령: `uv sync` 또는 `uv lock` — Coder 가 lock 파일을 직접
  수정하지 말고 **변경 의도만 pyproject.toml 에 기록**. 실제 lock 갱신은
  Setup Agent / 사용자가 별도로 수행 (Coder 가 패키지 매니저를 실행하지
  않는다는 본 task 의 제약과 정합).

  **결정**: 00104-2 의 Coder 는 `pyproject.toml` 만 수정하고 `uv.lock` 은
  건드리지 않는다. 사용자가 `uv lock` 또는 `uv sync` 로 동기화. 만약 빌드
  / 컨테이너 기동 단계에서 `uv sync` 가 자동 호출되면 추가 작업 불필요 —
  Docker 이미지의 `entrypoint.sh` 또는 `docker/Dockerfile` 가 어떻게 패키지
  를 인스톨하는지 별도 확인이 필요하나, **본 노트 범위 밖** (Setup 단계
  책임).

### 6-3. import 위치

- `app/email/transport/m365_oauth.py` 에서만 import:

  ```python
  import msal
  ...
  app_obj = msal.ConfidentialClientApplication(
      client_id=config.client_id,
      client_credential=config.client_secret,
      authority=f"https://login.microsoftonline.com/{config.tenant_id}",
  )
  result = app_obj.acquire_token_for_client(
      scopes=["https://outlook.office365.com/.default"],
  )
  if "access_token" not in result:
      raise RuntimeError(f"msal token 발급 실패: {result.get('error_description')}")
  access_token = result["access_token"]
  ```

- msal 의 in-memory token cache 는 ConfidentialClientApplication 인스턴스
  단위. **send() 호출마다 새 인스턴스를 만들면 캐시 효과 없음** — 모듈 수준
  싱글턴으로 하나 만들지, 인스턴스를 매번 만들지 결정 필요.

  **채택**: send() 마다 `ConfidentialClientApplication` 을 새로 생성한다.
  근거: SystemSetting 의 자격증명을 사용자가 UI 에서 변경할 수 있으며, 매번
  최신 값을 읽어 인스턴스를 만드는 게 일관성 ↑. 토큰 캐시 효과는 잃지만 한
  번 발급된 토큰은 약 1시간 유효하며 A-1 의 운영 빈도 (수동 테스트 발송 +
  A-3 daily report) 에서 캐시 적중률 의미가 크지 않다. A-3 daily report 의
  발송 루프에서 캐시 활용이 필요하면 그 시점에 별도 리팩터링 — A-1 에서는
  단순한 매 호출 인스턴스화로 충분.

----

## 7. `app/timezone.py` 인터페이스

`app/timezone.py` (확인 완료):

| 심볼 | 시그니처 | 비고 |
|---|---|---|
| `KST` | `ZoneInfo("Asia/Seoul")` 상수 | 모듈 싱글턴 |
| `to_kst(value: datetime \| None) -> datetime \| None` | naive → UTC 부착 후 KST | None 통과 |
| `now_utc() -> datetime` | UTC tz-aware | 저장용 |
| `now_kst() -> datetime` | KST tz-aware | 표시용 |
| `format_kst(value, fmt=DEFAULT_KST_FORMAT) -> str` | `"%Y-%m-%d %H:%M"` 기본 | None → `""` |
| `kst_date_boundaries(target_date: date) -> tuple[datetime, datetime]` | UTC tz-aware [start, end) | 일자 GROUP BY |
| `DEFAULT_KST_FORMAT` | `"%Y-%m-%d %H:%M"` 상수 | |

A-1 사용:

- `EmailSendRun.created_at` / `sent_at` 은 **UTC 저장**:

  ```python
  from app.timezone import now_utc
  EmailSendRun(created_at=now_utc(), ...)
  ```

  단, `_utcnow` (models.py) 와 `now_utc` (timezone.py) 가 같은 값을 돌려주므로
  ORM Python default 는 `_utcnow` 그대로 사용 가능 (다른 모델과 일관).

- API 응답에서 KST 표시 문자열이 필요하면 `format_kst(value)` 호출. 단,
  4 개 endpoint 의 응답 spec 은 ISO-8601 UTC 문자열을 권장 (기존 `/admin/scrape/status`
  와 동일 — `value.isoformat() if value else None`). 표시용 변환은 frontend
  JS 에서 수행 — `kst_format` Jinja 필터는 HTML 페이지에서만 사용. **결정**:
  JSON 응답은 ISO-8601 UTC (frontend 변환), HTML 페이지는 `kst_format` 필터.

----

## 8. loguru 사용 패턴

- 모든 모듈 상단에 `from loguru import logger` 한 줄.
- 사용 형식: `logger.<level>("문장 {} {}", arg1, arg2)`. f-string 대신 placeholder
  를 쓰는 게 컨벤션 — lazy formatting 으로 disable level 에서 비용 절감.
- 레벨 컨벤션 (기존 코드 관찰):
  - `DEBUG` — 함수 진입/종료, 분기 결정, 외부 호출 직전 (예: `logger.debug("admin.scrape_control_page 진입: user_id={} page={}", ...)`).
  - `INFO` — 사용자에게 의미 있는 성공 이벤트 (백업 완료, 비밀번호 변경 등).
  - `WARNING` — 비정상이지만 진행 가능한 상태 (백업 파일 누락, 스케줄러 미기동 등).
  - `ERROR` / `EXCEPTION` — 실패. `logger.exception(...)` 은 `traceback` 자동 포함.
- `app/logging_setup.py` 의 `configure_logging()` 이 stdlib logging → loguru
  라우팅을 설정. FastAPI / SQLAlchemy / Alembic 의 stdlib 로그도 loguru 로
  포워딩됨.

A-1 권장 로그 라인:

- `app/email/transport/m365_oauth.py`:
  - `logger.debug("M365 OAuth 토큰 발급 시작: tenant_id={}", config.tenant_id[:8])` (tenant_id 앞 8자만).
  - `logger.exception("M365 OAuth send 실패: {}: {}", type(exc).__name__, exc)` 는 호출자 (sender.py) 에서 retry 루프 안에 둔다 — transport 자체는 raise 만.
- `app/email/sender.py`:
  - `logger.info("메일 발송 성공: recipient={} attempt={} run_id={}", recipient, attempt, run.id)`.
  - `logger.warning("메일 발송 실패 (재시도 {}/{}): {}: {}", attempt, max_retry, type(exc).__name__, exc)`.
  - `logger.error("메일 발송 최종 실패: recipient={} attempt={} run_id={} error={}: {}", ...)`.
- `app/web/routes/admin_email.py`:
  - 4 endpoint 각각 진입 / 성공 / 실패에 DEBUG / INFO / WARNING 1 줄씩.

⚠️ client_secret / access_token 등 자격증명은 **로그에 절대 출력 금지**.
tenant_id 와 client_id 는 prefix 일부만 (`[:8]`) 출력해도 무방 (UUID-like
이므로 누설 위험 낮음).

----

## 9. Enum native_enum=False 사례

`app/db/models.py` 의 `AnnouncementProgressStatus` (line 2030) +
`AnnouncementProgress.status` 컬럼 (line 2116) 이 표준 패턴:

```python
class EmailSendRunStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
```

ORM 컬럼:

```python
status: Mapped[EmailSendRunStatus] = mapped_column(
    Enum(
        EmailSendRunStatus,
        name="email_send_run_status",
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
        native_enum=False,
    ),
    nullable=False,
    doc="발송 결과. 'sent' / 'failed' 중 하나.",
)
```

`__table_args__` 에 CHECK constraint 명시 (`announcement_progress.status` 와
동일 패턴, models.py:2178~):

```python
__table_args__ = (
    CheckConstraint(
        "status IN ('sent', 'failed')",
        name="ck_email_send_runs_status",
    ),
)
```

migration 본문 (5-2) 의 CHECK constraint 와 정확히 같은 SQL.

----

## 10. `client_secret` 평문 저장 정책 (A-1 결정)

- 4-3 의 mask 규칙 그대로. 저장은 평문. 응답은 mask.
- DB-level 암호화 (Fernet 등) 는 **별도 후속 작업** — 본 task 범위 밖. PROJECT_NOTES
  의 보안 후속 TODO 에 한 줄 추가 (subtask 00104-14 의 책임). 한 줄 예시:

  > **TODO (보안)**: SystemSetting `email.m365.client_secret` 은 현재 평문 저장.
  > 별도 secret 관리 / DB-level 암호화 (Fernet symmetric key) 도입 검토.

----

## 11. 「app/email/」 기존 디렉터리 처리

워크트리에 `app/email/` 디렉터리 + `app/email/transport/` 가 이미 존재
(`__pycache__` 뿐, 추적된 파일 0개). `git ls-files app/email/` 결과 비어
있음. 이전 시도의 잔재일 가능성이 높다.

**처리 결정**:

- subtask 00104-4 (Transport ABC) 가 `app/email/__init__.py` /
  `app/email/transport/__init__.py` / `app/email/transport/base.py` 를 새로
  만들면 자연스럽게 채워진다.
- `app/email/__pycache__` 와 `app/email/transport/__pycache__` 는 git
  무시 대상 (`.gitignore` 가 `**/__pycache__` 처리한다고 가정). 별도 정리
  불필요.

----

## 12. 분리 원칙 / 후속 subtask 흐름 정리

| subtask | 산출물 | scope 경계 |
|---|---|---|
| **00104-1** (본 노트) | `docs/phase_a1_design_note.md` | 코드 변경 0 |
| 00104-2 | `pyproject.toml` + msal 라인, `app/email/constants.py` (default 상수 7개) | uv.lock 안 건드림. SystemSetting row INSERT 안 함 (4-2 결정). |
| 00104-3 | `alembic/versions/<...>_email_send_runs.py` + `app/db/models.py` 의 EmailSendRun 클래스 | 다른 migration 영역 건드리지 않음 |
| 00104-4 | `app/email/__init__.py`, `app/email/transport/__init__.py`, `app/email/transport/base.py`, `app/email/config.py` | 외부 호출 / msal import 없음 — ABC + dataclass 만 |
| 00104-5 | `app/email/transport/m365_oauth.py` | sender / API 미연결 |
| 00104-6 | `app/email/transport/factory.py` | |
| 00104-7 | `app/email/message_builder.py` | plain text 전용 |
| 00104-8 | `app/email/sender.py` (send_with_retry) | 트랜잭션 경계 = 4-4 결정대로 자체 session |
| 00104-9 | `app/web/routes/admin_email.py` + `app/web/routes/__init__.py` re-export + `app/web/main.py` mount + `app/web/routes/admin.py` 의 `/admin/email` HTML 라우트 + `app/web/templates/admin/email.html` 골격 + `app/web/templates/admin/base.html` 의 sub_tab 링크 | 4 JSON endpoint + HTML 페이지 |
| 00104-10 | `tests/email/test_m365_oauth.py`, `tests/email/test_sender_retry.py`, `tests/web/test_admin_email_api.py` | 외부 의존 mock |
| 00104-11~13 | `app/web/static/js/admin_email.js` 의 섹션별 init 함수, email.html 의 각 section 마크업, 필요 시 `style.css` 의 신규 클래스 1~2 개 | 각 섹션 단독 |
| 00104-14 | `docs/email_transport_options.md` (신규), `README.USER.md` 의 메일 발송 섹션 추가, PROJECT_NOTES 갱신 (MemoryUpdater 책임이지만 같은 PR 에서) | 문서만 |

위 표의 "subtask 1 → 14" 흐름이 흔들리면 다른 subtask 의 Coder 가 이 노트의
범위 밖 작업을 침범할 수 있다. 본 노트는 각 subtask 의 scope 안내 역할.

----

## 13. 사용자 결정 필요 (escalate 후보)

이 노트는 모든 결정을 박았다고 가정하고 후속 subtask 를 진행할 수 있도록
설계됐다. 다만 다음 두 가지는 사용자가 "다르게 하고 싶다" 할 가능성이 있어
명시한다:

1. **API path prefix `/api/admin/email/*` vs `/admin/email/*`** (§1-4 의 선택 A
   채택). 사용자가 "기존 컨벤션 일관성 위해 `/admin/*` 로 통일하자" 라고
   할 수 있다. 만약 그렇다면 첨부 문서의 예시 응답 path 와 README.USER.md
   서술도 함께 수정.

2. **SystemSetting default 의 seed 방식 — fallback only vs data migration**
   (§2-4 의 선택 A 채택). 사용자가 "관리자가 처음 들어왔을 때부터 row 가
   존재했으면 좋겠다" 라고 하면 선택 B 로 전환 — 추가 alembic migration 1개.

다른 결정 (재시도 정책, error_message 의미, client_secret mask 규칙,
transport_type CHECK constraint 부재, msal 인스턴스 재사용 여부 등) 은 운영
상 큰 영향이 없거나 첨부 문서가 위임한 항목이라 본 노트의 결정을 그대로
가져가도 무방.

----

## 14. 참조 파일 (구현 시 빠르게 보기)

| 항목 | 파일 / 라인 |
|---|---|
| admin 라우터 + admin_user_required | `app/web/routes/admin.py:164`, `app/auth/dependencies.py:148` |
| SystemSetting 모델 | `app/db/models.py:1908` |
| get_setting / set_setting | `app/backup/service.py:128`, `:142` |
| BackupHistory 모델 (이력 row 패턴) | `app/db/models.py:1945` 근처 |
| `AnnouncementProgress` (enum + CHECK + FK 패턴) | `app/db/models.py:2063`, migration `alembic/versions/20260508_0900_b8d7e2c45f01_*.py` |
| 시스템 관리 sub_tab base | `app/web/templates/admin/base.html:38`, `:59` |
| backup.html (UI 일관성 레퍼런스) | `app/web/templates/admin/backup.html` |
| timezone 헬퍼 | `app/timezone.py:60`, `:105`, `:118`, `:140` |
| admin CSS 클래스 | `app/web/static/css/style.css:1059` (`.admin-section` 부터) |
| Pydantic + JSONResponse 라우터 예시 | `app/web/routes/progress.py:36`, `:87`, `:279` |
| 의존성 선언 | `pyproject.toml:18~61` |
| Alembic env / target_metadata | `alembic/env.py:20~52` |
| `uv.lock` 갱신 정책 | 본 노트 §6-2 (Coder 가 건드리지 않음) |
| Docker 바인드 마운트 | `docker-compose.yml:110, :173` |
