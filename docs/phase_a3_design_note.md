# Phase A-3 — 단체 Daily Report 설계 노트 (task 00125-1)

본 문서는 task 00125 (Phase A-3 단체 Daily Report) 구현에 앞서 코드베이스를 탐사한 결과와 후속 subtask 가 따라야 할 의사결정을 정리한 **단일 진실 소스** 다. 본 subtask(00125-1) 는 코드 변경 0 — 본 문서 한 개만 산출한다. 작업지시서(`phase_a3_prompt.md`)와 PROJECT_NOTES.md, 그리고 실제 코드를 모두 대조한 결과만 적는다.

## 0. 잠재적 함정 (먼저 읽기 — 후속 coder 가 함정을 피하도록)

작업지시서 §"잠재적 함정" 을 그대로 인용한다. 후속 subtask 의 coder/reviewer 는 이 목록을 코드 작성·리뷰 직전에 한 번 더 읽는다.

1. **`email.daily_report.recipients` SystemSetting 신설 금지.** 수신자 명단은 `is_admin=True` 사용자의 email 자동 수집이며, 별도 설정 리스트를 두지 않는다. A-4 에서 `EmailSubscription` 도입 시 확장한다.
2. **마지막 발송 시각의 single source of truth 는 `SystemSetting["email.daily_report.last_sent_at"]`.** `EmailDailyReportRun.started_at` 의 MAX 로 대체하지 않는다. EmailDailyReportRun 는 이력 전용이다.
3. **본문 디자인은 골격만.** 픽셀 단위 디테일·HTML 시각 polish 에 매몰하지 않는다. 사용자가 실제 메일을 받아본 뒤 후속 task 로 디자인을 다듬는다.
4. **EmailSendRun 의 `(related_kind, related_id)` 복합 인덱스 추가 금지.** 본 task 범위 밖. 성능 이슈 발생 시 별도 task.
5. **`email.send_enabled` 게이트는 daily report 에도 적용한다.** A-1 의 `/admin/email/test-send` 는 의도적으로 게이트 제외였지만, daily report 의 test-send 는 본 발송과 동일 흐름이므로 게이트 통과 필요.
6. **cron timezone 은 KST 고정.** `BackgroundScheduler(timezone=KST)` 가 이미 KST 로 설정돼 있어 cron 표현식도 KST 기준으로 해석된다. UTC 변환·환산 코드를 추가하지 않는다.
7. **`scrape_snapshots` 시간 컬럼 부재 시 마이그레이션 분리.** — 확인 결과 §1 참고: 컬럼 **추가 불필요** (이미 `created_at`/`updated_at` 존재).

## 1. `scrape_snapshots` 의 시간 컬럼 확정

### 사실 (코드 확인 — `app/db/models.py` L1679 이하)

`ScrapeSnapshot` 모델에는 다음 시간 컬럼이 이미 존재한다:

| 컬럼 | 타입 | 의미 |
|------|------|------|
| `snapshot_date` | `Date` (NOT NULL) | KST 날짜 1 row UNIQUE. timezone 정보 없음. |
| `created_at` | `DateTime(timezone=True)` (NOT NULL, default=`_utcnow`) | 본 row 가 **최초 INSERT 된 시각 (UTC)**. |
| `updated_at` | `DateTime(timezone=True)` (NOT NULL, default+onupdate=`_utcnow`) | payload 가 **마지막으로 머지된 시각 (UTC)**. |

따라서 **신규 컬럼 추가 / Alembic migration 분리 불필요**. plan 의 subtask 00125-2 가 추가하는 신규 테이블 `email_daily_report_runs` 단독 migration 으로 충분하다.

### 결정 — 시간 윈도우 비교에 어느 컬럼을 쓸 것인가

작업지시서 §3 의사 코드는 `created_at` 을 명시한다 (`WHERE created_at > from_dt AND created_at <= to_dt`). 본 노트는 그 결정을 **그대로 따른다**. 단, 다음 시맨틱 차이를 기록해 후속 운영 중 사용자가 의식할 수 있게 한다.

**`created_at` (채택) — "그 발송 구간 안에 새로 만들어진 snapshot 만 누적"**
- 같은 KST 날짜에 여러 ScrapeRun 이 돌아 row 가 머지되는 경우, 첫 INSERT 시각만 본다.
- 장점: 시맨틱이 단순하다 — "구간 안에 새 row 가 생긴 적이 있는가" 가 곧 "그 row 의 payload 전체를 발송 후보로 본다" 와 1:1 매칭된다.
- 위험 케이스: 어떤 KST 일자 row 가 last_sent_at 이전에 만들어졌고, last_sent_at 이후 추가 머지가 발생한 경우 — `created_at` 필터가 그 row 를 제외해 추가 머지분이 누락된다. 정상 운영(평일 KST 09:00 발송 / 야간 새벽 + 정오 수집) 흐름에서는 발송 시각이 그 날 첫 수집보다 이르므로 실측 발생 가능성은 낮다.

**대안 — `updated_at` (불채택)**
- "구간 안에 머지된 적이 있는 row 모두" 를 잡아 누락이 없다.
- 단점: 같은 row 가 last_sent_at 이전 카테고리 + 이후 카테고리 양쪽을 포함하면 직전 발송에서 이미 보낸 항목까지 재포함된다.

운영 데이터를 보고 실제로 누락이 일어나면 그 시점에 `updated_at` 으로 전환하는 후속 task 로 분리한다. 본 task 는 prompt 의사 코드 그대로 `created_at` 으로 구현한다.

### Repository 헬퍼 추가 (subtask 00125-4 안에서 함께 처리)

기존 `list_snapshots_in_range(from_exclusive, to_inclusive)` (`app/db/repository.py` L4024) 는 **`snapshot_date` 기반**이라 시간 단위 비교가 안 된다. 신규 헬퍼를 추가한다:

```python
def list_snapshots_created_in_range(
    session: Session,
    *,
    from_exclusive: datetime,
    to_inclusive: datetime,
) -> list[ScrapeSnapshot]:
    """``(from_dt, to_dt]`` UTC 시간 구간 안에 created_at 이 들어 있는 ScrapeSnapshot 리스트.
    ORDER BY created_at ASC — 누적 머지의 시간순 보장."""
```

이 헬퍼는 daily report 도메인 전용이므로 `app/db/repository.py` 가 아닌 `app/email/daily_report.py` 안에 둬도 무방하지만, 회귀 보호 + 재사용 여지를 위해 **`app/db/repository.py` 에 둔다** (기존 `list_snapshots_in_range` 옆).

## 2. `merge_snapshot_payload` 재사용 결정

### 사실

- `app/db/snapshot.py::merge_snapshot_payload(existing, new)` 가 이미 5종 카테고리 머지를 완전히 처리한다 (작업지시서 §3 의 머지 규칙과 1:1 일치).
- 대시보드 `app/web/dashboard_section_a.py::build_section_a` 가 같은 함수를 `functools.reduce` 패턴으로 사용 중 (`reduce(merge_snapshot_payload, payloads, normalize_payload(None))`).
- 머지 규칙 회귀 보호 테스트: `tests/db/test_snapshot_merge.py` (Phase 5a / task 00041) 가 검증 4·5·6·7 케이스를 이미 커버.

### 결정 — **신규 머지 함수 작성 금지, 기존 `merge_snapshot_payload` 를 reduce 로 재사용**

subtask 00125-4 의 `aggregate_snapshots(session, window)` 는 다음 패턴을 그대로 따른다:

```python
from functools import reduce
from app.db.snapshot import merge_snapshot_payload, normalize_payload

snapshots = list_snapshots_created_in_range(session,
                                            from_exclusive=window.from_dt,
                                            to_inclusive=window.to_dt)
merged_payload = reduce(
    merge_snapshot_payload,
    (snapshot.payload for snapshot in snapshots),
    normalize_payload(None),  # 초깃값 = 정규형 빈 dict
)
```

대시보드 `build_section_a` 와 100% 동일 reduce 패턴. 결과는 정규형 dict (5종 카테고리 + counts). 이후 announcement_id union → `list_announcements_by_ids` 1회 SELECT → AnnouncementSummary 변환 흐름도 대시보드와 같다.

## 3. 5종 카테고리 키 — 정확한 표기

`app/db/snapshot.py` 가 노출하는 상수를 그대로 import 한다. 신규 문자열 리터럴 신설 금지.

```python
from app.db.snapshot import (
    CATEGORY_NEW,                # "new"
    CATEGORY_CONTENT_CHANGED,    # "content_changed"
    TRANSITION_TO_LABELS,        # ("접수예정", "접수중", "마감")
)
```

전이 카테고리 키는 `f"transitioned_to_{label}"` 패턴이다 (`app/db/snapshot._transition_key` 가 private 이라 import 하지 않고 본 모듈에서 동일 형식으로 재구성 — `dashboard_section_a.py::_transition_key` 와 동일 미러). 키 표기 정합성:

| dataclass 필드 | payload 키 |
|----------------|------------|
| `AggregatedSnapshotPayload.new` | `"new"` |
| `AggregatedSnapshotPayload.content_changed` | `"content_changed"` |
| `AggregatedSnapshotPayload.transitioned_to_received_scheduled` | `"transitioned_to_접수예정"` |
| `AggregatedSnapshotPayload.transitioned_to_receiving` | `"transitioned_to_접수중"` |
| `AggregatedSnapshotPayload.transitioned_to_closed` | `"transitioned_to_마감"` |

dataclass 필드명은 영문(`received_scheduled`/`receiving`/`closed`), payload 키는 한글 — `AnnouncementStatus` enum 의 영문 name(lowercase) / 한글 value 분리와 같은 컨벤션이다.

## 4. 첫 발송 fallback_days

### 사실 / 권장

- 작업지시서 §"핵심 결정": "첫 발송 (last_sent_at NULL) 의 fallback — 직전 7일치 snapshot 을 보냄".
- 7일이 적절한지 판단할 수 있는 운영 데이터는 본 task 시점에 부족 (실제 운영 첫 발송 시점에서 사용자가 결정할 영역).
- snapshot 데이터가 얼마나 오래된 게 있는지는 사용자 환경마다 다르므로 "직전 7일" 이 항상 0건이 아니라는 보장은 없다. 0건이면 `compute_aggregation_window` 가 `None` 을 반환해 status=SKIPPED 로 처리되며 last_sent_at 은 갱신되지 않는다 — 다음 잡이 자동으로 같은 fallback 을 재시도한다.

### 결정

`compute_aggregation_window(session, *, now, fallback_days=7)` 의 **기본값 7** 로 시작한다. 후속 task 로 운영자가 SystemSetting 화 또는 코드 상수 조정. **본 task 범위에서는 코드 상수 default**.

## 5. 수신자 (admin email) 수집 정책

### 사실

- `User.is_admin` (Boolean, NOT NULL).
- `User.email` (String(255), nullable — 없을 수 있음).
- `User.email_subscribed` (Boolean, NOT NULL, default True, server_default "1").
- 작업지시서 §"핵심 결정" — "is_admin=True 사용자의 email 자동 수집".
- 작업지시서 §"첫 subtask" — "is_admin=True AND email IS NOT NULL 만? email_subscribed=True 도 조건에 포함?".

### 결정

수신자 후보 = `is_admin=True AND email IS NOT NULL AND email IS NOT '' AND email_subscribed=True`.

근거:
- `email IS NOT NULL` — 발송 불가능한 row 차단 (자명).
- `email = ''` 빈 문자열도 동일 차단 — `email` 컬럼이 `String(255), nullable=True` 라 빈 문자열로도 저장될 수 있다 (`settings` 라우트가 사용자 입력을 그대로 받는 경로 존재).
- `email_subscribed=True` 포함 — settings 에서 사용자가 본인 의지로 수신 거부 토글한 경우 admin 이라도 발송 대상에서 제외한다. settings 라우트가 이미 토글 UI 를 제공하고 있어 의미 있는 옵트아웃 시그널.

### email 미설정 admin 처리

- "이메일 미설정 / email_subscribed=False" 인 admin 은 **수신자 명단에서 silent skip** (해당 사용자만 빠지고 발송 자체는 진행).
- `GET /admin/email/daily-report/settings` 응답에 `admin_count_without_email: int` 필드를 두어 UI 가 노란 경고 박스로 노출 (작업지시서 §7 spec 그대로).
- "자세히 보기" expand 영역에는 admin 사용자별 (username, email | "(미설정)", email_subscribed) 리스트를 표시 — 운영자가 누가 수신/제외되는지 한눈에 본다.

### 헬퍼 함수 시그니처

`app/email/daily_report.py` 에 같이 둔다:

```python
def collect_admin_recipient_emails(session: Session) -> list[str]:
    """is_admin=True + email NOT NULL/'' + email_subscribed=True 인 사용자의 email 목록.
    중복 제거 + 정렬된 리스트. 빈 결과 가능 — 호출자가 빈 발송 케이스를 처리한다."""

def collect_admin_recipient_overview(session: Session) -> AdminRecipientOverview:
    """admin 사용자 전수의 (username, email, email_subscribed, eligible) 4-튜플 리스트 + 카운터.
    /admin/email/daily-report/settings 응답 직렬화용."""
```

## 6. 본문 빌더 (subject + text + html) — message_builder 재사용

### 사실 / 결정

작업지시서 §4 "본문 빌더 — `app/email/message_builder.py` 확장" 에 따라 **신규 모듈 신설하지 않고 기존 `app/email/message_builder.py` 안에 함수 추가**.

추가 함수 3종:
- `build_daily_report_subject(window: AggregationWindow) -> str`
- `build_daily_report_text_body(*, window, payload) -> str`
- `build_daily_report_html_body(*, window, payload) -> str`

HTML 디자인은 forward 의 `build_forward_html_body` / `_build_sender_info_html_block` 인라인 CSS grayscale 스타일을 그대로 차용한다 (디자인 일관성 + 메일 클라이언트 호환성 검증된 패턴 재사용). 5종 카테고리는 모듈 상수 `_DAILY_REPORT_CATEGORY_DESCRIPTORS` 로 표기 (라벨·이모지·payload 키) 를 한 자리에 둔다 — 대시보드 `SECTION_A_CATEGORY_DESCRIPTORS` 와 같은 패턴. 단, daily report 의 라벨/이모지는 작업지시서 §4 본문 spec 그대로 (🆕 신규 / 📝 내용 변경 / ✅ 접수예정 전이 / ▶ 접수중 전이 / 🚫 마감 전이).

빈 카테고리 섹션은 **HTML/Text 양쪽에서 섹션 자체 생략** — `if not items: continue`.

카테고리당 표시 cap = **50건** (작업지시서 §4 명시). 초과 시 섹션 끝에 "외 N건 — 대시보드에서 확인" 안내 1줄.

### subject 포맷

`f"[정부사업 모니터링] Daily Report — {KST.format_kst(from_dt, '%Y-%m-%d %H:%M')} ~ {KST.format_kst(to_dt, '%Y-%m-%d %H:%M')}"`

- `from_dt`/`to_dt` 는 UTC 저장이지만 subject 표시는 KST 변환 (PROJECT_NOTES 컨벤션 — 모든 사용자 표시 시각은 KST).
- `is_first_send=True` 인 경우는 작업지시서 §4 의 "최초 발송 — 직전 7일치 포함" 안내를 본문 헤더 박스에서만 노출 (subject 까지 길게 늘리지 않음).

## 7. 발송 서비스 트랜잭션 / 게이트 / last_sent_at 정책

### 트랜잭션 3단계 — forwarding 의 prepare/run 분리를 그대로 차용

`app/email/forwarding.py` 가 task 00120 에서 prepare / run 으로 분리한 패턴을 따른다. 다만 daily report 는 manual 트리거 시에도 즉시 응답 후 BackgroundTasks 위임이 필수는 아니다 (수신자 수 N ≤ admin 수, 통상 한 자릿수) — 단일 함수 `prepare_and_send_daily_report` 안에서 단계 1→2→3 을 동기로 수행해도 충분하다.

| 단계 | 내용 | 트랜잭션 경계 |
|------|------|--------------|
| 1 | `EmailDailyReportRun` row INSERT (status=IN_PROGRESS) + commit | row + commit |
| 2-a | `is_email_sending_enabled()` 게이트 확인 | row 갱신 commit 불필요 |
| 2-b | 실패 시: status=FAILED, completed_at=now_utc(), error_message="메일 전송 비활성화" commit → `EmailSendingDisabledError` raise → 라우터 503 변환 | commit + raise |
| 2-c | 게이트 통과 시: `compute_aggregation_window` 호출 | read-only |
| 2-d | window=None 이면: status=SKIPPED, completed_at=now_utc() commit + 빠른 종료 | commit + 반환 |
| 3 | `aggregate_snapshots(session, window)` → payload | read-only |
| 4 | `build_daily_report_subject/text/html` 1회 | pure compute |
| 5 | 수신자별 1통씩 `build_multipart_message` → `send_with_retry`. 각각 EmailSendRun row 생성 (related_kind=`daily_report`, related_id=run_id) | send_with_retry 가 자기 commit |
| 6 | run.status, success_count, failure_count, completed_at 최종 commit | commit |
| 7 | SUCCESS/PARTIAL 이고 trigger != manual_test 이면 `SystemSetting["email.daily_report.last_sent_at"] = now_utc().isoformat()` set_setting commit | commit |

### `last_sent_at` 갱신 정책표 (작업지시서 §5 인용)

| trigger | status | last_sent_at 갱신? | 이유 |
|---------|--------|---------------------|------|
| scheduled | SUCCESS / PARTIAL | ✅ 갱신 | 정상 발송 |
| scheduled | SKIPPED | ❌ 유지 | 다음 잡이 같은 구간 + 신규 누적까지 처리 |
| scheduled | FAILED | ❌ 유지 | 재시도를 위해 |
| manual_admin | SUCCESS / PARTIAL | ✅ 갱신 | scheduled 와 동등 |
| manual_admin | SKIPPED / FAILED | ❌ 유지 | 동일 사유 |
| manual_test | * (모든 상태) | ❌ 항상 유지 | 본 발송 구간을 망가뜨리지 않게 |

이 표는 단위 테스트 12 케이스 중 7~12 번에 1:1 매핑되어 검증된다.

### 게이트 — daily report 의 test-send 도 게이트 통과 필요

A-1 의 `/admin/email/test-send` 는 `is_email_sending_enabled` 게이트에서 의도적으로 제외였다(00116). daily report 의 test-send 는 본 발송 동작을 그대로 검증하는 게 목적이므로 게이트 필수. 게이트 비활성 상태에서 호출 시 503 + 안내 메시지. EmailDailyReportRun row 는 INSERT 후 게이트 실패 분기에서 status=FAILED 로 commit 되어 이력에 남는다.

## 8. APScheduler 잡 등록 / 복원 / 변경 / 제거 — 4 케이스

### 사실

- 기존 `app/scheduler/service.py` 가 두 패턴을 보여 준다:
  1. **일반 cron/interval 스케줄** (`add_cron_schedule` / `add_interval_schedule` / `toggle_schedule` / `delete_schedule`): 사용자가 임의로 N개 등록 가능. job_id 는 uuid prefix.
  2. **백업 잡** (`register_backup_cron_schedule` / `remove_backup_cron_schedule` / `ensure_backup_cron_registered` / `get_backup_schedule_summary`): **항상 1건 고정 ID** (`JOB_ID_BACKUP="backup-db"`). 기존 잡이 있으면 reschedule, 없으면 add.

daily report 는 백업과 동일 패턴 — 항상 1건만 존재. 따라서 백업 잡 함수를 그대로 미러한다.

### 추가 모듈/상수 (subtask 00125-7)

`app/scheduler/constants.py` 에 추가:

```python
JOB_ID_DAILY_REPORT: Final[str] = "daily-report"
JOB_NAME_DAILY_REPORT_PREFIX: Final[str] = "daily-report-cron:"
```

`app/scheduler/job_runner.py` 에 추가:

```python
def scheduled_daily_report_job() -> None:
    """pickle-safe top-level. 내부에서 session_scope + build_transport +
    collect_admin_recipient_emails + prepare_and_send_daily_report.
    모든 예외 swallow + logger.exception (scheduled_backup_job 패턴)."""
```

`app/scheduler/service.py` 에 추가:

```python
def register_daily_report_cron_schedule(
    cron_expression: str | None = None,
    *,
    enabled: bool | None = None,
) -> ScheduleSummary | None:
    """enabled=False 또는 cron 빈 값 → remove + None.
    그 외 → add or reschedule (백업 패턴). 인자 None 이면 SystemSetting 에서 직접 로드."""

def remove_daily_report_cron_schedule() -> None:
    """잡이 없어도 no-op."""

def get_daily_report_schedule_summary() -> ScheduleSummary | None:
    """JOB_ID_DAILY_REPORT 의 ScheduleSummary 단독 조회."""

def ensure_daily_report_cron_registered() -> None:
    """startup 시 SystemSetting 기반 복원 — ensure_backup_cron_registered 와 같은 라인.
    enabled=False 면 등록 안 함 (잡스토어에 자동 복원된 잡이 있다면 제거하지 않고 그대로 둔다 —
    SystemSetting 토글이 source of truth 라 잡스토어 복원 vs 신규 등록을 구분).
    실제로 startup 흐름에서 enabled 가 False 면 register_daily_report_cron_schedule(enabled=False)
    호출로 통일해, jobstore 복원분도 함께 정리한다."""
```

### `list_general_schedules` 갱신

기존 `list_general_schedules()` 는 `JOB_ID_BACKUP` 만 제외하고 있다 (`app/scheduler/service.py` L420). daily report 잡도 일반 스케줄 목록에서 빠져야 하므로 같은 함수에 `JOB_ID_DAILY_REPORT` 제외도 추가한다.

### startup 복원

`app/web/main.py::create_app()` 에서 `ensure_backup_cron_registered()` 바로 다음 줄에 `ensure_daily_report_cron_registered()` 호출 추가. 백업과 같은 try/except 패턴으로 startup 실패 방어.

### 4 케이스 검증 매트릭스 (subtask 00125-7 의 테스트)

| 케이스 | 기대 동작 |
|--------|-----------|
| `register_daily_report_cron_schedule(cron, enabled=True)` (잡 없음) | add_job |
| `register_daily_report_cron_schedule(cron2, enabled=True)` (잡 있음) | reschedule (trigger 교체 + next_run_time 재계산) + name 갱신 |
| `register_daily_report_cron_schedule(enabled=False)` (잡 있음) | remove_job + None 반환 |
| `register_daily_report_cron_schedule(...)` 후 재기동 | SQLAlchemyJobStore 자동 복원 — `ensure_daily_report_cron_registered` 가 no-op |

## 9. Admin API 5종 — endpoint 시그니처

작업지시서 §7 그대로. 라우터는 **`app/web/routes/admin_email.py` 안에 함께 추가** — 별도 `admin_daily_report.py` 신설하지 않는다 (현 `admin_email.py` 가 이미 admin-only 라우터 + email 도메인이라 응집도가 더 높다).

```
GET    /api/admin/email/daily-report/settings
PUT    /api/admin/email/daily-report/settings
POST   /api/admin/email/daily-report/test-send
POST   /api/admin/email/daily-report/send-now
GET    /api/admin/email/daily-report/runs?limit=50
GET    /api/admin/email/daily-report/runs/{run_id}/sends
```

권한: `admin_user_required` (라우터 레벨 dependency 이미 존재). PUT/POST 는 추가로 `ensure_same_origin`. URL prefix `/api/admin/email/daily-report/...` 는 기존 `/api/admin/email/{settings,test-send,send-runs}` 와 path 충돌 없다 (모두 fixed sub-path).

### settings 응답에 포함할 추가 필드

```json
{
  "enabled": true,
  "cron_expression": "0 9 * * 1-5",
  "last_sent_at": "2026-05-19T00:00:00+00:00",
  "test_recipient": "ops@example.com",
  "next_run_at": "2026-05-20T00:00:00+09:00",
  "admin_emails": [
    {"username": "alice", "email": "alice@...", "email_subscribed": true,  "eligible": true},
    {"username": "bob",   "email": null,         "email_subscribed": true,  "eligible": false}
  ],
  "admin_count_eligible": 1,
  "admin_count_without_email": 1,
  "admin_count_unsubscribed": 0
}
```

`next_run_at` 는 `get_daily_report_schedule_summary().next_run_time` (APScheduler Job 의 `next_run_time`). 잡이 비활성이면 None.

### POST send-now / test-send 응답 스키마 통일

```json
{
  "run_id": 123,
  "status": "success|partial|failed|skipped",
  "snapshot_count": 7,
  "recipient_count": 3,
  "success_count": 3,
  "failure_count": 0,
  "error_message": null
}
```

## 10. Frontend — email.html 에 신규 카드 + 발송 이력 섹션

### 사실

- `app/web/templates/admin/email.html` 의 현재 구성: 섹션 0(메일 발송 활성화 토글) → 섹션 1(메일 설정 form) → 섹션 2(테스트 발송) → 섹션 3(발송 이력).
- JS 는 `static/js/admin_email.js` 한 파일에 모든 fetch 로직 집중.

### 결정 — Daily Report 카드 위치

섹션 0(메일 발송 활성화 토글) **위가 아닌 아래**, 섹션 1(메일 설정) 위쪽에 신규 섹션 추가는 혼란. 작업지시서 §8 도 "메일 발송 설정 카드 아래" 로 명시. 최종 순서:

1. 섹션 0 — 메일 전송 활성화 토글 (기존)
2. **섹션 1 — Daily Report 카드 (신규)**
3. 섹션 2 — 메일 설정 form (기존)
4. 섹션 3 — 테스트 발송 (기존)
5. 섹션 4 — 발송 이력 (기존)
6. **섹션 5 — Daily Report 발송 이력 (신규)**

### 「테스트 발송」 받는 사람 입력 UX

- **단일 input 으로 시작** (작업지시서 §"첫 subtask" 명시 — chip 식 거부).
- placeholder/value 는 SystemSetting 의 `test_recipient` 로 채움 (한 번 입력하면 다음 진입 시 자동 채워짐).
- 「저장」 버튼은 카드 form 의 전역 「저장」 과 통합 — 별도 저장 버튼 두지 않는다. 테스트 버튼 클릭 시 현재 input 값으로 즉시 발송 (저장 + 발송 분리).

### 「지금 admin 에게만 발송」 confirm 다이얼로그

- **권장: confirm 필요.** 메일 발송은 외부 영향이 큰 액션이라 우발 클릭 방지 가치가 높다.
- 메시지: `현재 admin {N}명에게 즉시 발송됩니다. 계속할까요? (수신자: alice, bob, ...)`
- `window.confirm(...)` 사용 — 별도 custom modal 신설하지 않음 (관련성 모달 X 삭제 패턴 §"커스텀 모달 중첩 없이 OS 네이티브 UI 사용" 과 동일 컨벤션).

### 상태 / 트리거 아이콘 매핑

| 도메인 | 값 | 표시 |
|--------|----|------|
| status | success / partial / failed / skipped / in_progress | ✅ / ⚠️ / ❌ / ⏭ / ⏳ |
| trigger | scheduled / manual_admin / manual_test | "예약" / "지금 발송" / "테스트" |

### 발송 이력 expand UI

작업지시서 §8 — forward 의 expand 패턴 (수신자별 EmailSendRun 결과) 과 동일. JS 측 fetch URL 만 다름 (`/runs/{run_id}/sends`). HTML 구조·CSS 클래스명은 동일하게 재사용 권장 — 신규 클래스 신설 최소화.

## 11. 본문 빌더 카테고리당 50건 cap

작업지시서 §4 명시 — 50 으로 시작. 사용자 후속 task 로 수정 가능. 본 task 범위에서는 **모듈 상수 `_DAILY_REPORT_CATEGORY_ITEM_CAP = 50`** 으로 두고 SystemSetting 화하지 않는다 (운영 데이터 확인 전 SystemSetting 화는 over-engineering).

## 12. EmailSendRun 의 `(related_kind, related_id)` 인덱스 — 추가 안 함

작업지시서 §"잠재적 함정" 4 + §"범위 밖" 명시. daily report 의 sends 조회는 풀스캔이라도 로컬 규모(admin 수 한 자릿수, SKIPPED 가 많아 row 수도 낮음)에서 실측 문제 없음. 인덱스 추가가 필요해지면 별도 task.

## 13. 다음 실행 예측 표시 (`next_run_at`)

- APScheduler `Job.next_run_time` (UTC tz-aware datetime). paused job 은 None.
- `get_daily_report_schedule_summary()` 의 `ScheduleSummary.next_run_time` 그대로 사용.
- UI 표시는 KST 변환 (PROJECT_NOTES 컨벤션 — `kst_format` / JS `en-CA + Asia/Seoul`).

## 14. SystemSetting 키 + EmailDailyReportRun ORM (subtask 00125-2 가 다룸)

작업지시서 §1·§2 그대로. 본 노트에서 다음 한 가지만 확정해 둔다.

### `EmailDailyReportRun.requested_by_user_id` 의 ondelete

`EmailSendRun.requested_by_user_id` / `EmailForwardLog.sender_user_id` 와 동일하게 **`ondelete="SET NULL"`** (사용자 탈퇴 시 row 자체는 이력으로 보존). 작업지시서 §2 명시.

### `EmailSendRun.related_kind` 새 값 — `"daily_report"`

`app/email/constants.py` 에 상수 추가:

```python
RELATED_KIND_DAILY_REPORT: str = "daily_report"
```

DB 레벨 변경 없음 (application 레벨 enum). 기존 `RELATED_KIND_TEST_SEND` / `RELATED_KIND_FORWARD` 옆에 추가.

## 15. 사용자 결정 필요 (Plan 승인 후 진행하기 전 확인 권장 — 단, plan 승인 단계에서 이미 의도가 합의됐다면 default 채택 후 운영 중 조정)

다음 항목은 운영 데이터 / 사용자 취향이 들어가는 영역이라 default 를 두되 후속 task 로 조정 가능:

| 항목 | default | 조정 시 영향 범위 |
|------|---------|------------------|
| `fallback_days` (첫 발송) | 7 | `app/email/daily_report.py::compute_aggregation_window` 의 기본 인자 1줄 |
| 카테고리당 cap | 50 | `app/email/message_builder._DAILY_REPORT_CATEGORY_ITEM_CAP` 상수 1곳 |
| `email_subscribed=False` admin 의 포함 여부 | 제외 (default 채택) | `collect_admin_recipient_emails` 쿼리 1곳 |
| 「지금 admin 에게만 발송」 confirm 메시지 노출 형태 | `window.confirm` | JS 1곳 |
| 본문 HTML 디자인 디테일 | grayscale 골격 (forward 와 동일) | message_builder 의 HTML 빌더 함수 |
| `created_at` vs `updated_at` for aggregation window | `created_at` (prompt 의사 코드 채택) | repository 신규 헬퍼 1곳 + 단위 테스트 |

## 16. 후속 subtask 구성 매핑 (plan 과 본 노트 일치 확인)

| subtask | 본 노트 §| 핵심 산출물 |
|---------|---------|-------------|
| 00125-1 | (본 노트) | `docs/phase_a3_design_note.md` |
| 00125-2 | §14, §1 | SystemSetting 키 상수 + EmailDailyReportRun ORM + Alembic migration (신규 테이블만 — `scrape_snapshots` 변경 없음) |
| 00125-3 | §1, §3 | `compute_aggregation_window` + dataclass 3종 + 단위 테스트 + 신규 repository 헬퍼 `list_snapshots_created_in_range` |
| 00125-4 | §2, §3 | `aggregate_snapshots` 구현 (reduce + merge_snapshot_payload 재사용) + 누적 머지 회귀 테스트 |
| 00125-5 | §6, §11 | 본문 빌더 3종 + 빈 카테고리 생략 / 50건 cap 테스트 |
| 00125-6 | §5, §7 | `prepare_and_send_daily_report` + 게이트 + last_sent_at 정책 + 12 케이스 단위 테스트 |
| 00125-7 | §8 | APScheduler 잡 등록/복원/제거 + 4 케이스 테스트 + startup 복원 |
| 00125-8 | §9 | Admin API 5종 + 6 케이스 통합 테스트 |
| 00125-9 | §10 | email.html 카드 + 발송 이력 섹션 |
| 00125-10 | (전 노트) | PROJECT_NOTES.md / README.USER.md 갱신 |

## 17. 변경 없음 / 신설 모듈 요약 (후속 coder 의 navigation 보조)

### 신설
- `app/email/daily_report.py` — 도메인 service (window 계산, aggregation, recipient 수집, prepare_and_send)
- `tests/email/test_daily_report.py` — 단위 테스트 12 케이스 + window·aggregation·builder
- `tests/web/test_admin_daily_report_api.py` — 통합 테스트 6 케이스
- `tests/scheduler/test_daily_report_schedule.py` — 스케줄러 4 케이스
- Alembic migration 1개 (`email_daily_report_runs` 신규 테이블)

### 확장 (기존 파일에 추가)
- `app/email/constants.py` — daily report SystemSetting 키 4종 + `RELATED_KIND_DAILY_REPORT`
- `app/db/models.py` — `EmailDailyReportStatus` enum + `EmailDailyReportRun` 모델
- `app/db/repository.py` — `list_snapshots_created_in_range` 신규 헬퍼
- `app/email/message_builder.py` — daily report subject/text/html 빌더 3종
- `app/scheduler/constants.py` — `JOB_ID_DAILY_REPORT` + `JOB_NAME_DAILY_REPORT_PREFIX`
- `app/scheduler/job_runner.py` — `scheduled_daily_report_job` (pickle-safe top-level)
- `app/scheduler/service.py` — `register_daily_report_cron_schedule` / `remove_daily_report_cron_schedule` / `get_daily_report_schedule_summary` / `ensure_daily_report_cron_registered` + `list_general_schedules` 의 daily-report 제외 필터 확장
- `app/web/routes/admin_email.py` — daily report endpoint 5종 + 1 (sends expand)
- `app/web/main.py::create_app` — `ensure_daily_report_cron_registered()` 호출 1줄 추가 (백업 호출 다음)
- `app/web/templates/admin/email.html` — Daily Report 카드 + 발송 이력 섹션 신규 2섹션
- `app/web/static/js/admin_email.js` — Daily Report 카드 / 이력 fetch + UI 로직 추가 (또는 별도 `admin_daily_report.js` 분리 — 코드량 보고 결정)

### 변경 없음 (회귀 보호 — 후속 coder 가 건드리지 말 것)
- `app/db/snapshot.py::merge_snapshot_payload` 와 그 단위 테스트 — daily report 가 그대로 import 해 reduce 로 재사용한다.
- `app/web/dashboard_section_a.py` — daily report 와 대시보드는 분리. daily report 가 대시보드 헬퍼를 직접 호출하지 않는다 (시간 단위 vs 날짜 단위 차이로 헬퍼가 호환되지 않음).
- `app/email/forwarding.py` — 트랜잭션 패턴만 참고. daily report 는 별도 도메인 service.
- 기존 `app/email/constants.py` 의 7개 SystemSetting 키 / default 상수. daily report 키 4종은 그 옆에 추가만 한다.
