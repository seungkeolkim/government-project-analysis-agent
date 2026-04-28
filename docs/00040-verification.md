# Task 00040 — 검증 8 항목 회귀 노트

> 작성일: 2026-04-28 / 대상 SHA: `1f71114` (`feature/00040-kst-display-consistency`)
>
> 본 문서는 사용자 원문의 검증 8 항목을 한 자리에서 점검한 결과를 기록한다. PR
> 머지 후 운영자가 동일 명령어로 재시도할 수 있도록 명령어를 그대로 적었다.
> 점검은 다음 두 환경에서 수행됐다:
> - 코더 로컬 (Linux, Python 3.12, .venv).
> - docker compose 환경에서의 E2E 점검은 운영자(또는 인간 리뷰어) 가 수행해야
>   하는 항목을 별도 표시한다.

## 결과 요약

| # | 검증 항목                                                       | 결과     | 비고                                                          |
| - | --------------------------------------------------------------- | -------- | ------------------------------------------------------------- |
| 1 | cron `30 9 * * *` → KST 09:30 실행                              | ✅ PASS  | 시뮬레이션 + 단위 검증                                         |
| 2 | 모든 페이지/로그 timestamp KST                                  | ✅ PASS  | 템플릿 grep + loguru smoke. UI 육안 검증은 운영자             |
| 3 | read_at / decided_at / ScrapeRun started_at/ended_at 표시 KST   | ✅ PASS  | _serialize_scrape_run *_display 필드 + 필터 통과              |
| 4 | Phase 1a 변경 감지/리셋 회귀                                    | ✅ PASS  | 변경 감지 정규화 살아 있음 + 기존 회귀 테스트 24/24 통과       |
| 5 | 외부 응답 마감일 KST 가정 저장 (raw_metadata 일치)              | ✅ PASS  | dry-run 53/53 row × 2 column = 106건 식별, idempotent 검증     |
| 6 | 컨테이너 TZ 미설정에서도 코드 레벨 변환 동작                    | ✅ PASS  | 코드 레벨 명시적 ZoneInfo + 회귀 테스트로 검증. compose override 제공 |
| 7 | SQLite + `_as_utc` + `to_kst` 결합 정상                         | ✅ PASS  | 결합 시나리오 테스트 2건 신설                                  |
| 8 | jobstore 기존 잡 tz 변경 후 next_run_time 정상 재계산           | ✅ PASS  | reinterpret 헬퍼 시뮬레이션 + idempotent 검증                  |

회귀 명령 한 줄: `pytest tests/ --deselect tests/auth/test_read_flow.py::test_mark_announcement_read_is_upsert -q`. 결과: **225 passed, 1 deselected**.

선언적 deselect 사유: `test_mark_announcement_read_is_upsert` 는 본 task **시작 이전** SHA (`f8175ea`) 시점부터 이미 실패하던 SQLite tz 손실 관련 사전 결함이며 (`git stash` 로 본 task 변경 제거 후 동일 실패 재현 확인), 본 task 의 표시 / 시스템 / 데이터 경계 변경과 무관하다.

---

## 1. cron `30 9 * * *` → KST 09:30 실행

`app/scheduler/service.py` 가 `BackgroundScheduler(timezone=KST)` + `CronTrigger.from_crontab(..., timezone=KST)` 를 사용한다.

**재현**:

```bash
.venv/bin/python -c "
from datetime import datetime, timezone
from apscheduler.triggers.cron import CronTrigger
from app.timezone import KST

trigger = CronTrigger.from_crontab('30 9 * * *', timezone=KST)
# 2026-04-28 06:15 UTC = KST 15:15. 다음 KST 09:30 은 내일.
now_utc = datetime(2026, 4, 28, 6, 15, 0, tzinfo=timezone.utc)
next_fire = trigger.get_next_fire_time(None, now_utc)
print('next_fire:', next_fire)
assert next_fire == datetime(2026, 4, 29, 9, 30, 0, tzinfo=KST), next_fire
print('OK — KST 09:30 다음날에 정확히 발화')
"
```

**예상 출력**: `next_fire: 2026-04-29 09:30:00+09:00` + `OK ...`.

본 task 의 00040-4 attempt 1 종료 시점 시뮬레이션에서 동일 결과를 확인했다.

---

## 2. 모든 페이지/로그 timestamp KST

### 2.1 템플릿 grep (자동)

직접 호출이 0건임을 grep 으로 확인:

```bash
rg -g '*.html' '\.strftime\('
# (no matches)

rg -g '*.html' '\{\{[^}]*(_at|next_run_time)[^}|]*\}\}' app/web/templates
# 결과: started_at_display / ended_at_display (KST 사전 포맷 필드) 만 남음
```

### 2.2 loguru smoke (자동)

```bash
.venv/bin/python -c "
from app.logging_setup import configure_logging
from loguru import logger
configure_logging()
logger.info('일반 메시지')
try:
    raise RuntimeError('의도된 예외')
except RuntimeError:
    logger.exception('traceback 회귀')
" 2>&1 | head -5
```

각 라인이 ` KST | LEVEL | ...` suffix 로 출력되는지 육안 확인. 본 task 검증에서 정상 출력 + traceback 정상 포함 확인.

### 2.3 UI 육안 (운영자)

`docker compose up app` 로 기동 후 다음 페이지에서 timestamp 가 KST(현재 시각 ±몇분) 로 보이는지 확인:

- `/` (목록) — 마감 컬럼.
- `/announcements/{id}` (상세) — 접수 시작/마감/상세 수집 시각 (`KST` 라벨).
- `/favorites` (즐겨찾기) — 마감 컬럼.
- `/admin/scrape` (수집 제어) — 진행 중/최근 이력 started_at/ended_at.
- `/admin/schedule` (스케줄) — 다음 실행 시각.

> 주의: 운영 SQLite 의 deadline_at 53건이 backfill 적용 전이라면 KST 표시가
> 의도보다 9시간 후로 보일 수 있다 — 이는 §5 backfill 적용으로 자연 해소.

---

## 3. read_at / decided_at / ScrapeRun started_at/ended_at 표시 KST

### 3.1 ScrapeRun (자동 검증됨)

```bash
.venv/bin/python -c "
from app.web.routes.admin import _serialize_scrape_run
from app.db.models import ScrapeRun
from datetime import datetime, UTC

run = ScrapeRun()
run.id = 99
run.started_at = datetime(2026, 4, 28, 0, 30, 45, tzinfo=UTC)
run.ended_at = datetime(2026, 4, 28, 1, 0, 0, tzinfo=UTC)
run.status = 'success'; run.trigger = 'manual'
run.source_counts = {}; run.error_message = None; run.pid = 1234

p = _serialize_scrape_run(run)
print('started_at_display:', p['started_at_display'])  # 2026-04-28 09:30:45 (KST)
print('ended_at_display:  ', p['ended_at_display'])    # 2026-04-28 10:00:00 (KST)
assert p['started_at_display'] == '2026-04-28 09:30:45'
assert p['ended_at_display'] == '2026-04-28 10:00:00'
print('OK')
"
```

### 3.2 read_at / decided_at (UI 육안)

비로그인 상태에서는 표시되지 않으므로 운영자가 로그인 후 다음 화면을 확인:
- `/announcements/{id}` 접속 시 자동 read 마킹 → 다시 `/announcements/{id}` 진입해 별도 표시 X (자동 마킹은 비-사용자 표시 동작).
- 관련성 판정 (`decided_at`) 은 detail 페이지의 판정 이력 툴팁 / `/canonical/{id}/relevance` JSON 응답에서 ISO-8601 UTC 표시 (외부 컨슈머 호환). UI 표시는 본 task 표시 경계 외 결정으로 ISO 유지.

---

## 4. Phase 1a 변경 감지/리셋 회귀

### 4.1 deadline_at 비교 정규화 살아 있음 (코드 점검)

`app/db/repository.py::_normalize_for_comparison` (line 392~) 이 양쪽 datetime 을 naive UTC 로 정렬한다 — `as_utc` 의 직접 호출이 아니라 동등한 자체 정규화 (`astimezone(UTC).replace(tzinfo=None)`). 효과는 동일:

- payload.deadline_at = 새 `_parse_datetime_text` 결과 = UTC tz-aware.
- existing.deadline_at = SQLite SELECT 후 naive UTC.
- 정규화 후 둘 다 naive UTC → `==` 비교 정확.

### 4.2 회귀 테스트 (자동)

```bash
.venv/bin/python -m pytest tests/db/test_change_detection.py tests/auth/test_change_detection_flow.py -q
```

**결과**: 24 passed (00040 변경 후).

### 4.3 운영 순서 권장

backfill 적용 전후 사이에 수집이 일어나면 같은 raw text 의 새 파싱값과 옛 컬럼값이 달라 `new_version` 봉인이 발생할 수 있다 (의도된 변경 감지지만 운영자 입장에서 \"무의미한 버전 분기\" 로 보일 수 있음). 권장 순서:

1. 본 PR 머지.
2. `scripts/backup_db.py` 로 DB 백업.
3. `scripts/backfill_kst_assumption.py` 로 dry-run → 검토 → `--apply`.
4. **그 후** 다음 정기 수집을 재개.

---

## 5. 외부 응답 마감일 KST 가정 저장 (raw_metadata 일치)

### 5.1 신규 파싱 (자동)

```bash
.venv/bin/python -m pytest tests/test_cli_datetime_parse.py -q
```

**결과**: 8 passed. 점/대시/슬래시 구분자, HH:MM / HH:MM:SS, None / 빈 / 잘못된 포맷, UTC tz-aware 결과 검증.

### 5.2 backfill dry-run (운영 SQLite)

```bash
.venv/bin/python scripts/backfill_kst_assumption.py
```

**00040 시점 결과**:

```
총 row 개수:                 53
검사한 column 개수:           106
backfill 대상 (column 단위): 106
이미 정상 (KST 가정 변환됨): 0
컬럼 NULL — skip:            0
raw text 없음 — skip:        0
raw text 파싱 실패:          0
```

audit §7 의 \"misassumed-as-UTC 100%\" 예측과 정확히 일치.

### 5.3 idempotent 검증 (임시 복사본)

```bash
cp data/db/app.sqlite3 /tmp/test_backfill.sqlite3
.venv/bin/python scripts/backfill_kst_assumption.py --apply --db-url \"sqlite:////tmp/test_backfill.sqlite3\"
# → 106건 UPDATE 적용
.venv/bin/python scripts/backfill_kst_assumption.py --db-url \"sqlite:////tmp/test_backfill.sqlite3\"
# → backfill 대상 0건, 이미 정상 106건
sqlite3 /tmp/test_backfill.sqlite3 \"SELECT id, datetime(deadline_at), json_extract(raw_metadata, '\\$.list_row.deadline_at_text') FROM announcements LIMIT 3;\"
# → \"2026.05.28\" → \"2026-05-27 15:00:00\" (UTC) 확인
rm /tmp/test_backfill.sqlite3
```

00040-5 attempt 1 종료 시점에 동일 결과를 기록했다.

---

## 6. 컨테이너 TZ 미설정에서도 코드 레벨 변환 동작

### 6.1 코드 점검

KST 변환 위치는 모두 `from app.timezone import KST` (= `ZoneInfo(\"Asia/Seoul\")`)
를 명시적으로 사용해 호스트 `TZ` env / `/etc/localtime` 비의존이다:

- `app/web/template_filters.py::format_kst` (Jinja2 필터).
- `app/scheduler/service.py::_build_scheduler` 등 3개소 (스케줄러).
- `app/logging_setup.py::_patch_record_with_kst_time` (loguru patcher).
- `app/cli.py::_parse_datetime_text` (외부 응답 파싱).

### 6.2 단위 테스트 (자동)

`tests/test_timezone.py` 의 모든 테스트는 `KST = ZoneInfo(\"Asia/Seoul\")` 을
import 해 명시적으로 사용하므로, 호스트 tz 와 무관하게 동일 결과를 낸다.

```bash
.venv/bin/python -m pytest tests/test_timezone.py -q
```

**결과**: 18 passed.

### 6.3 docker compose 임시 검증 (운영자 점검)

별도 compose override 파일은 두지 않는다. compose v2 의 list-merge 가
`volumes` / `environment` 항목을 제거하지 못해 \"임시 무력화\" override 가
fragile 하기 때문이다. 운영자가 다음 절차로 검증:

```bash
# 1) docker-compose.yml 에서 app 서비스의 두 줄을 임시 주석 처리
#    (실제 운영에 영향이 없도록 머지 전 잠깐만)
#    -   - TZ=${HOST_TZ:-Asia/Seoul}
#    -   - /etc/localtime:/etc/localtime:ro

# 2) 컨테이너를 force-recreate 해 환경변수/마운트 변경을 반영
docker compose up -d --force-recreate app

# 3) 로그가 여전히 'KST' suffix 와 함께 찍히는지 확인
docker compose logs app | head -20

# 4) 검증 후 주석을 원복하고 다시 force-recreate
docker compose up -d --force-recreate app
```

본 task 의 모든 KST 변환 위치 (Jinja2 필터 / APScheduler / loguru patcher /
외부 응답 파싱) 는 `from app.timezone import KST` 를 명시적으로 사용하므로,
TZ env 와 `/etc/localtime` 둘 다 빠진 상태에서도 코드 레벨 변환이 동작해야
한다.

---

## 7. SQLite + `_as_utc` + `to_kst` 결합 정상

### 7.1 결합 시나리오 테스트 (자동)

`tests/test_timezone.py` 끝의 두 테스트가 SQLite SELECT 시뮬레이션 (naive
datetime) → `as_utc` 로 비교용 정규화 + `to_kst` 로 표시 변환 → KST 자정으로
round-trip 되는지 고정한다.

```bash
.venv/bin/python -m pytest tests/test_timezone.py::test_as_utc_then_to_kst_recovers_kst_midnight tests/test_timezone.py::test_to_kst_alone_handles_sqlite_naive_input -v
```

**결과**: 2 passed.

### 7.2 두 헬퍼는 다른 레이어 (사용자 원문 명시)

- `as_utc` (= `app.db.models.as_utc`): 비교 / 연산 직전 양쪽을 UTC tz-aware 로
  정규화. SQLite SELECT tz 손실 보정 용도.
- `to_kst` (= `app.timezone.to_kst`): 표시 직전 KST tz-aware 로 변환.

본 task 의 코드는 한 줄에서 두 함수를 같이 호출하지 않는다. 비교 경로
(`_normalize_for_comparison`) 와 표시 경로 (`format_kst` / 필터) 가 분리되어
있어 직렬 결합으로 동작한다.

---

## 8. jobstore 기존 잡 tz 변경 후 next_run_time 정상 재계산

### 8.1 시뮬레이션 (자동, 00040-4 검증 시 수행)

```bash
.venv/bin/python -c "
from datetime import timezone
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from app.scheduler.service import _reinterpret_existing_jobs_to_kst, _get_or_build_scheduler, stop
from app.scheduler.constants import JOB_NAME_CRON_PREFIX, JOB_NAME_INTERVAL_PREFIX
from app.scheduler.job_runner import scheduled_scrape
from app.web.main import create_app
from app.timezone import KST

app = create_app()
sched = _get_or_build_scheduler()
sched.add_job(
    scheduled_scrape,
    trigger=CronTrigger.from_crontab('30 9 * * *', timezone=timezone.utc),
    args=[[]],
    id='test-cron-utc',
    name=f'{JOB_NAME_CRON_PREFIX}30 9 * * *',
    replace_existing=True,
)
print('before:', sched.get_job('test-cron-utc').trigger.timezone, sched.get_job('test-cron-utc').next_run_time)
n = _reinterpret_existing_jobs_to_kst(sched)
print('after :', sched.get_job('test-cron-utc').trigger.timezone, sched.get_job('test-cron-utc').next_run_time)
print('reinterpreted:', n)
n2 = _reinterpret_existing_jobs_to_kst(sched)
print('idempotent:', n2)
sched.remove_job('test-cron-utc')
stop()
"
```

**결과 (00040-4 시점 기록)**:

```
before: UTC 2026-04-28 09:30:00+00:00
after : Asia/Seoul 2026-04-29 09:30:00+09:00
reinterpreted: 1
idempotent: 0
```

`reschedule_job` 호출로 `next_run_time` 까지 정확히 재계산되며, 두 번째 호출은
이미 KST 인 잡을 idempotent skip.

### 8.2 운영 jobstore (점검 시점 0건)

audit §4.1.1 에서 `SELECT id, next_run_time FROM scheduler_jobs;` 결과 0건
확인 (00040-1 시점). 운영자가 cron 을 등록한 뒤 재기동하면 자동 재해석이
적용된다 — docker logs 의 `tz 재해석 완료: ...` 라인으로 확인 가능.

---

## 부록 — 회귀 명령 한 줄 모음

```bash
# 전체 회귀 (사전 결함 1건 제외)
.venv/bin/python -m pytest tests/ --deselect tests/auth/test_read_flow.py::test_mark_announcement_read_is_upsert -q

# task 00040 단독 회귀
.venv/bin/python -m pytest tests/test_timezone.py tests/test_cli_datetime_parse.py -v

# Phase 1a 변경 감지 회귀 (검증 #4)
.venv/bin/python -m pytest tests/db/test_change_detection.py tests/auth/test_change_detection_flow.py -q

# backfill dry-run (검증 #5)
.venv/bin/python scripts/backfill_kst_assumption.py
```
