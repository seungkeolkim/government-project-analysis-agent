# Task 00040 — 시각 처리 전수 조사 보고서 (timezone audit)

> 작성일: 2026-04-28 / 대상 SHA: `6a1f392` (`feature/00040-kst-display-consistency`)
> 본 보고서는 **읽기 전용** 산출물이다. 코드 변경은 없으며, 후속 subtask
> (00040-2 ~ 00040-6) 가 1차 근거로 사용한다.
>
> **경로 변경 안내 (task 00078, 2026-05-07)**: 본문에서 인용하는
> `scripts/backup_db.py`, `scripts/audit_canonical_false_positives.py`,
> `scripts/backfill_kst_assumption.py` 는 이후 `scripts/python/<name>.py` 로 이동되었다.
> 감사 시점의 행/위치 표기(`scripts/backup_db.py:221` 등)는 그대로 보존하고,
> 재실행 또는 재참조 시에는 신 경로(`scripts/python/...`) 기준으로 찾는다.

## §0 전제 / 컨벤션 한 줄 요약

- DB 저장은 **UTC tz-aware** 유지 (사용자 원문, 사용자 결정 옵션 B). 본 task 에서 **컨벤션 변경 없음**.
- 표시·계산·입력·cron 의 사용 경계에서만 **Asia/Seoul** 변환을 적용한다.
- `app/db/models.py::as_utc` (SQLite SELECT tz 손실 보정) 와 신설 예정 `app/timezone.py::to_kst` (UTC→KST 표시 변환) 는 **다른 레이어**다.
  - 비교/연산 직전에는 `as_utc` 로 양쪽을 tz-aware UTC 로 정규화.
  - 사용자 표시 직전에만 `to_kst` 로 KST tz-aware 변환.
  - 둘은 직렬 결합 (`as_utc` → 비교/저장 → 표시 직전 `to_kst`) 으로 사용한다.

---

## §1 요약 (항목별 건수와 위험도)

| 항목                                                              | 건수 / 위치                              | 위험도 | 후속 subtask |
| ----------------------------------------------------------------- | ---------------------------------------- | ------ | ------------ |
| `datetime.utcnow()` (naive) 호출                                  | **0건** (`docs/db_portability.md` 의 금지 안내 1건만 존재) | —      | —            |
| `datetime.now()` naive 호출                                       | **0건** (`tz=UTC`/`tz=timezone.utc` 100% 명시) | —      | —            |
| Jinja2 템플릿 `.strftime` 직접 호출                               | **8건** (`detail.html`×4, `list.html`×3, `favorites.html`×1) | **높음** — 표시 KST 변환 누락 | 00040-3      |
| Jinja2 템플릿 `{{ dt }}` 원시 출력                                | **4건** (`admin/control.html` started_at, `admin/_recent_runs_table.html` started_at·ended_at, `admin/schedule.html` next_run_time) | **높음** | 00040-3      |
| API/JSON `.isoformat()` 직접 직렬화                               | **5건** (`web/main.py` 4 필드, `routes/admin.py` started_at·ended_at, `routes/relevance.py` decided_at, `routes/favorites.py` ann_deadline_at) | 중간 — JSON 컨슈머 별도 결정 필요(현 시점 컨슈머 없음) | 00040-3 검토 |
| APScheduler `timezone=...`                                        | **3개소** (`scheduler/service.py:115, 303, 362`) 모두 `timezone.utc` | **높음** — 사용자 원문 결정과 정반대 | 00040-4      |
| loguru sink format `{time:...}`                                   | **1개소** (`logging_setup.py:170`) 명시적 tz 없음 → 호스트 로컬 tz 의존 | 중간 — TZ env 의존 끊으려면 KST 명시 필요 | 00040-4      |
| 외부 응답 파싱 KST 가정 누락                                      | **1개소** (`app/cli.py::_parse_datetime_text`, line 181~199) — naive parse 후 `tzinfo=timezone.utc` 무지성 부착 | **치명** — 모든 IRIS/NTIS row 가 9시간 후로 저장됨 | 00040-5      |
| `as_utc` (구 `_as_utc`) 호출 위치                                 | **1개소** (`app/auth/service.py:411`, UserSession.expires_at 비교). 정의는 `app/db/models.py:73` | 정상 — 추가 호출은 향후 비교 경로마다 필요 | 00040-2 가이드, 00040-4 |
| 잘못 저장된 row (영향 범위)                                       | **53건 / 53건** (현 운영 SQLite 스냅샷 기준 100%) — IRIS 31, NTIS 22 | **치명** | 00040-5 backfill |

위험도 정의: `치명` = 사용자 원문 검증 항목을 지금 즉시 실패시키는 결함. `높음` = 표시 / cron 시각이 9시간 어긋나는 결함. `중간` = 정합성에 영향은 적으나 KST 단일 운영 컨벤션을 만족시키려면 후속 작업 필요.

---

## §2 datetime 호출 인벤토리 (Python)

### 2.1 `datetime.utcnow()` (naive)

전수 조사 결과 **0건**. `docs/db_portability.md:46` 에 "`datetime.utcnow()` (naive) 는 금지" 라는 컨벤션 안내가 있을 뿐 호출 자체는 없다.

### 2.2 `datetime.now()` naive (tz 인자 없는 호출)

전수 조사 결과 **0건**. 모든 호출이 `tz=UTC` 또는 `tz=timezone.utc` 를 명시한다.

(검색 명령: `rg -t py 'datetime\.now\(\)'` → 매칭 없음)

### 2.3 `datetime.now(tz=...)` 호출 (현황 — 변경 불필요, 참고 인벤토리)

저장 경로(write 측) 의 `datetime.now(tz=UTC)` 는 그대로 유지한다. 표시 경로에서 사용된다면 `now_kst()` (00040-2 신설 예정) 로 교체하지만, 아래 호출은 모두 **저장용** 이라 변경 대상이 아니다.

| 파일 / 라인                                       | 용도                                                    |
| ------------------------------------------------- | ------------------------------------------------------- |
| `app/db/models.py:70` (`_utcnow`)                 | SQLAlchemy `default`/`onupdate` 콜러블                  |
| `app/db/repository.py:747`                        | scrape_runs 비교용 now                                  |
| `app/db/repository.py:1664, 1747, 1973, 2033`     | upsert / status_transition / read 처리 시 now           |
| `app/db/repository.py:2246, 2315`                 | scrape_run end / status update                          |
| `app/auth/service.py:314`                         | 인증 세션 발급                                          |
| `app/scraper/iris/detail_scraper.py:216`          | `detail_fetched_at`                                     |
| `app/scraper/ntis/detail_scraper.py:332`          | `detail_fetched_at`                                     |
| `app/scraper/attachment_downloader.py` ×8         | `downloaded_at`, `attempted_at`                         |
| `app/sources/yaml_editor.py:335`                  | 백업 파일명 timestamp (`.strftime` 결합 — §3.2 참조)    |
| `scripts/backup_db.py:221`                        | DB 백업 파일명                                          |
| `tests/**`                                        | 테스트 fixture                                          |

판정: 모두 저장 / 비교 / 파일명 용도이므로 **수정 대상 아님**. 향후 헬퍼로 일관화하려면 `now_utc()` 로 단순 치환만 가능하나, 본 task 의 scope 밖.

### 2.4 `.strftime(...)` 호출 (Python)

| 파일 : 라인                                  | 용도                                                                                                       | 처리 방침                                                                  |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `app/sources/yaml_editor.py:335`             | `datetime.now(tz=UTC).strftime(BACKUP_FILENAME_PATTERN)` — 백업 파일명                                       | **유지** (파일명은 호스트 운영자 시점 KST 기준이 자연스럽지만, 본 task 의 표시 경계가 아니라 파일시스템 키이므로 별도 결정. 후속 00040-5/00040-6 에서 결정 — 우선순위 낮음) |
| `scripts/backup_db.py:103`                   | `timestamp.strftime(_TIMESTAMP_FORMAT)` — DB 백업 파일명                                                   | 동일 — 보고에서만 명시, 본 task 변경 없음                                   |
| `scripts/audit_canonical_false_positives.py:194` | `row.deadline_at.strftime("%Y-%m-%d")` — 진단 스크립트 출력                                              | 진단 스크립트는 운영 화면이 아니므로 **scope 밖**. 단, `deadline_at` 자체가 misassumed-as-UTC 상태라 출력 일자가 1일 어긋날 수 있음 (00040-5 backfill 후 자연 해소) |

### 2.5 `datetime.strptime(...)` (외부 텍스트 → datetime)

`app/cli.py:193` 의 `_parse_datetime_text` 가 **유일** 한 외부 텍스트 파싱 진입점이다. 자세한 분석은 §5 참조.

---

## §3 Jinja2 템플릿 시각 출력 인벤토리

본 절은 후속 subtask 00040-3 의 교체 대상 체크리스트가 된다. **모든 항목은 파일:라인 단위**.

### 3.1 `.strftime("...")` 직접 호출 (8건)

| 파일 : 라인                                      | 코드                                                              | 표시 의도        | 후속 교체 (예시)                   |
| ------------------------------------------------ | ----------------------------------------------------------------- | ---------------- | ---------------------------------- |
| `app/web/templates/detail.html:79`               | `{{ announcement.received_at.strftime("%Y-%m-%d") }}`             | 접수 시작 일자   | `{{ announcement.received_at \| kst_date }}`   |
| `app/web/templates/detail.html:89`               | `{{ announcement.deadline_at.strftime("%Y-%m-%d %H:%M") }}`        | 마감일시         | `{{ announcement.deadline_at \| kst_format }}` |
| `app/web/templates/detail.html:111`              | `{{ announcement.detail_fetched_at.strftime("%Y-%m-%d %H:%M:%S") }} UTC` | 상세 수집 시각 (현재 " UTC" 라벨까지 명시) | `{{ ... \| kst_format("%Y-%m-%d %H:%M:%S") }} KST` (라벨도 KST 로) |
| `app/web/templates/detail.html:202`              | `{{ sib.deadline_at.strftime("%Y-%m-%d") }}`                       | 동일과제 sibling 마감일 | `{{ sib.deadline_at \| kst_date }}` |
| `app/web/templates/favorites.html:195`           | `{{ item.ann_deadline_at.strftime("%Y-%m-%d %H:%M") }}`            | 즐겨찾기 마감    | `{{ ... \| kst_format }}`          |
| `app/web/templates/list.html:200`                | `{{ gr.representative.deadline_at.strftime("%Y-%m-%d %H:%M") }}`   | 묶어보기 대표 마감 | `{{ ... \| kst_format }}`          |
| `app/web/templates/list.html:282`                | `{{ ann.deadline_at.strftime("%Y-%m-%d %H:%M") }}`                 | 분리 모드 마감   | `{{ ... \| kst_format }}`          |
| `app/web/templates/list.html:325`                | `{{ sib.deadline_at.strftime("%Y-%m-%d") }}`                       | 분리 모드 sibling 마감 | `{{ ... \| kst_date }}`             |

### 3.2 `{{ dt }}` 원시 출력 (4건, datetime 객체를 그대로 노출)

| 파일 : 라인                                                    | 코드                                              | 표시 의도            |
| -------------------------------------------------------------- | ------------------------------------------------- | -------------------- |
| `app/web/templates/admin/control.html:55`                      | `started_at={{ running.started_at }}`              | 진행 중 ScrapeRun 시작 시각 — `running` 은 `_serialize_scrape_run` 산출이라 **이미 ISO-8601 문자열**이지만, ISO 가 UTC 면 그대로는 KST 가 아님 |
| `app/web/templates/admin/_recent_runs_table.html:34`           | `<td>{{ run.started_at or '' }}</td>`              | 최근 이력 시작 시각  |
| `app/web/templates/admin/_recent_runs_table.html:35`           | `<td>{{ run.ended_at or '' }}</td>`                | 최근 이력 종료 시각  |
| `app/web/templates/admin/schedule.html:72`                     | `<td>{{ schedule.next_run_time or '—' }}</td>`     | 다음 예정 시각 (datetime 객체) |

처리 방침 (00040-3 가이드용):
- `running.started_at`, `run.started_at`, `run.ended_at` 은 라우트(`app/web/routes/admin.py:122-123`) 가 `.isoformat()` 으로 ISO-8601 문자열로 만들어 넘긴다. 즉 **현 코드 경로에서 템플릿이 받는 값은 datetime 이 아니라 str** 이다. KST 적용은 두 가지 경로 중 하나로:
  1. 라우트에서 `format_kst(run.started_at)` 결과를 같은 키로 넘기고 템플릿은 그대로 출력 (권장, 00040-3 에서 수행).
  2. 템플릿에서 fromisoformat → kst_format 하는 매크로 도입 — 비추.
- `schedule.next_run_time` 은 `ScheduleSummary.next_run_time` 으로 **datetime 객체** 가 그대로 전달된다 (`app/scheduler/service.py:75`). 템플릿에서 `| kst_format` 필터로 직접 변환 가능.

### 3.3 `.isoformat()` 호출 (라우트/직렬화 경로 — 표시 경로의 우회 노출)

`isoformat()` 은 datetime → str 직렬화이며, 그 자체로 KST 변환은 아니다. JSON 응답이라 해도 사용자 화면 컨슈머(JS) 가 표시한다면 KST 변환이 필요하다.

| 파일 : 라인                                          | 코드                                                                        | 노출 경로                                                |
| ---------------------------------------------------- | --------------------------------------------------------------------------- | -------------------------------------------------------- |
| `app/web/main.py:121-128`                            | `received_at`, `deadline_at`, `detail_fetched_at`, `scraped_at`, `updated_at` | `/announcements` JSON                                    |
| `app/web/routes/admin.py:122-123`                    | `started_at`, `ended_at`                                                    | `/admin/scrape/status` JSON + 템플릿 컨텍스트            |
| `app/web/routes/relevance.py:142-143`                | `decided_at`                                                                | `/canonical/{id}/relevance` JSON                         |
| `app/web/routes/favorites.py:596`                    | `ann_deadline_at`                                                           | `/favorites/...` JSON                                    |

방침:
- 본 task 의 사용자 원문 검증 ③ "read_at / decided_at / ScrapeRun started_at/ended_at 표시 KST" 는 화면(템플릿) 기준이므로, **JSON 직렬화 형식은 변경하지 않는 것이 안전**하다 (외부 컨슈머의 ISO-8601 UTC 파싱과 호환). 단, `/admin/scrape/status` 폴링 응답을 JS 가 그대로 화면에 박아 넣고 있다면 JS 측 변환도 함께 점검해야 한다 — 00040-3 에서 `app/web/templates/admin/control.html` JS 본문 점검 필요 (현재 잠깐 본 결과 폴링 후 테이블을 서버 사이드 렌더 결과로 교체하는 구조라 JS 재포맷은 없을 가능성 큼).
- 따라서 이번 task 에서 ISO 직렬화는 **유지**, 템플릿 출력만 KST 필터 경유로 교체한다는 결정이 일관성을 깨지 않는다.

---

## §4 APScheduler / loguru 현재 tz 동작

### 4.1 APScheduler

`app/scheduler/service.py` 에서 timezone 설정이 들어가는 3개소:

| 라인 | 코드                                                       | 의미                                                  |
| ---- | ---------------------------------------------------------- | ----------------------------------------------------- |
| 115  | `BackgroundScheduler(..., timezone=timezone.utc)`           | 스케줄러 글로벌 tz                                    |
| 303  | `CronTrigger.from_crontab(cron_expression, timezone=timezone.utc)` | cron 트리거의 평가 tz                                |
| 362  | `IntervalTrigger(hours=hours, timezone=timezone.utc)`       | interval 트리거의 시작 기준 tz                        |

→ 사용자 원문 결정과 **정반대**. `cron 30 9 * * *` 가 현재는 UTC 09:30 = KST 18:30 에 실행된다. 후속 00040-4 에서 3개소 모두 `ZoneInfo("Asia/Seoul")` 로 교체.

### 4.1.1 jobstore 기존 잡 재해석

`SQLAlchemyJobStore` 는 `scheduler_jobs` 테이블에 trigger 직렬화(pickle 추정) + `next_run_time` 을 저장한다. 운영 스냅샷을 점검:

```text
sqlite> SELECT id, next_run_time FROM scheduler_jobs;
(0 rows)
```

→ 현재 등록된 스케줄이 **0건** 이다. tz 교체 후 재해석 / 일회성 재계산 risk 는 **현 시점 없음**. 단, README/검증 8 항목에는 "tz 교체 후 등록된 잡이 KST 기준으로 다음 실행을 잡는지" 회귀 1건이 필요 (00040-6 의 ⑧ 검증 항목으로 수행).

### 4.2 loguru

`app/logging_setup.py:170` 의 sink format:

```text
"<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | ..."
```

`{time:...}` 은 loguru 가 record 의 `time` (aware datetime — `loguru` 내부에서 `datetime.now().astimezone()` 으로 호스트 로컬 tz 적용) 을 포맷한다. 즉:

- 컨테이너에 `TZ=Asia/Seoul` (00029 에서 적용) 가 있으면 KST 로 찍힘.
- TZ env 미설정 호스트(개발자 macOS / 노트북 등) 에서는 호스트 tz 로 찍힘.

사용자 원문 "컨테이너 TZ env(00029) 유지하되 코드는 명시적 ZoneInfo (TZ 비의존)" 에 따라, sink format 에 **명시적 tz 지정**이 필요. loguru 는 포맷 토큰에 timezone 지정을 직접 지원하지 않으므로 다음 중 하나로 처리:

- (a) 포맷에서 `{time}` 을 제거하고 patcher 로 `record["extra"]["kst_time"] = record["time"].astimezone(KST)` 한 뒤 `{extra[kst_time]:YYYY-MM-DD HH:mm:ss.SSS}` 사용 — loguru 표준 패턴.
- (b) `logger.add(..., format=lambda record: f"{record['time'].astimezone(KST):...}")` — 람다 포맷터.
- 권장: (a). 00040-4 가 결정.

### 4.3 컨테이너 TZ env (00029)

`docker-compose.yml` / `docker/` 에 `TZ=Asia/Seoul` 가 설정돼 있다 (이전 task 00029). 본 task 에서는 **유지**하되, 코드 레벨에서 KST 변환을 명시적으로 수행하므로 host TZ 미설정에서도 동작해야 한다는 사용자 원문 조건을 만족한다.

---

## §5 외부 응답 파싱 tz 가정 (IRIS / NTIS / attachment downloader)

### 5.1 IRIS

- `app/scraper/iris/list_scraper.py:288-301` — `received_at_text` (rcveStrDe) / `deadline_at_text` (rcveEndDe) 는 **원문 문자열** 그대로 row dict 에 담는다 (예: `'2026.05.28'`). 시·분 정보 없음.
- `app/scraper/iris/detail_scraper.py:216` — `fetched_at = datetime.now(tz=timezone.utc)` (수집 시각, KST 가정과 무관).

### 5.2 NTIS

- `app/scraper/ntis/list_scraper.py:338-351` — `received_at_text` / `deadline_at_text` 동일 형식 (`'YYYY.MM.DD'`).
- `app/scraper/ntis/detail_scraper.py:332` — `fetched_at = datetime.now(tz=timezone.utc)`.

### 5.3 attachment downloader

`app/scraper/attachment_downloader.py` 의 `downloaded_at` / `attempted_at` 은 모두 `datetime.now(tz=timezone.utc)` 로, 다운로드 시각이라 KST 가정과 무관하다.

### 5.4 텍스트 → datetime 변환 진입점

`app/cli.py::_parse_datetime_text` (line 181~199) 가 **유일** 한 외부 텍스트 파싱이며, 본 task 의 핵심 결함이다.

```python
# app/cli.py:181-199 (현재 코드)
def _parse_datetime_text(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized_text = value.strip().replace("/", "-").replace(".", "-")
    for candidate_format in _DATETIME_TEXT_FORMATS:
        try:
            naive_dt = datetime.strptime(normalized_text, candidate_format)
        except ValueError:
            continue
        return naive_dt.replace(tzinfo=timezone.utc)   # ← 결함
    logger.warning("날짜 텍스트 파싱 실패 — 무시: {!r}", value)
    return None
```

문제: IRIS·NTIS 가 표시한 `'2026.05.28'` 은 **KST 자정** 의미인데, `replace(tzinfo=timezone.utc)` 는 이를 그대로 UTC tz-aware 로 부착해 버려, 의미상 9시간 후로 저장된다.

기대 동작 (00040-5 가이드):

```python
# 사용자 원문 "외부 응답(IRIS/NTIS): KST 가정 → UTC 변환 저장"
naive_dt = datetime.strptime(normalized_text, candidate_format)
kst_dt = naive_dt.replace(tzinfo=KST)
return kst_dt.astimezone(UTC)    # 저장은 UTC tz-aware
```

호출 위치 (모든 row 가 이 경로를 탄다):
- `app/cli.py:217-218` — `received_at`, `deadline_at` 두 필드 모두.

후속 subtask 00040-5 가:
1. `_parse_datetime_text` 를 KST 가정 → UTC 변환으로 교체 (또는 신설 `app/timezone.py` 의 헬퍼 사용).
2. 잘못 저장된 row backfill — `scripts/backfill_kst_assumption.py` 작성.
3. raw_metadata 의 `list_row.deadline_at_text` / `received_at_text` 를 1차 근거로 대조.

---

## §6 `as_utc` 호출 위치 (KST 결합 식별)

### 6.1 정의

`app/db/models.py:73-97` `as_utc(value: datetime) -> datetime`. naive 면 `tzinfo=UTC` 부착, 이미 aware 면 그대로 반환.

### 6.2 호출 위치 (전수)

| 파일 : 라인                  | 호출 코드                                                                  | KST 결합 필요 여부                                                                                          |
| ---------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `app/auth/service.py:411`    | `if as_utc(user_session.expires_at) <= as_utc(now_ts):`                   | **No** — 비교 로직 (만료 검사) 이라 KST 변환 불필요. tz-aware UTC 양쪽 정렬만 하면 충분.                       |

→ 현 시점 다른 `as_utc` 호출 위치는 없다. 단, 후속 subtask 들이 다음 경로에 새로 추가할 가능성 있음:
- ScrapeRun 비교 / cleanup (`app/db/repository.py` 의 stale cleanup 등) — 이미 PROJECT_NOTES.md 가 안내.
- KST 표시 경로에서 ORM 인스턴스의 datetime 필드를 출력할 때 — SQLite SELECT 후 naive 가 돼 있으면, **`to_kst` 가 내부적으로 naive → UTC 가정 → KST 변환** 을 처리하도록 사용자 원문이 명시. 즉 표시 경로는 `to_kst` 한 번으로 충분 (별도 `as_utc` 결합 호출 없이).

### 6.3 두 헬퍼의 결합 사용 패턴 (사용자 원문 명시)

> `_as_utc(SQLite SELECT 보정)와 to_kst(KST 변환)는 다른 레이어. 결합 사용.

본 보고서가 명시: 비교/연산 직전에는 `as_utc` 로 양쪽을 tz-aware UTC 로 정규화, 표시 직전에는 `to_kst` 로 KST 변환. `to_kst` 의 입력 처리에 "naive → UTC 가정" 이 포함되므로 표시 경로는 단독 호출로 충분 — 즉 두 헬퍼는 직렬 결합 (저장→비교→표시) 이지 한 줄에서 동시 호출되지 않는다.

---

## §7 데이터 영향 범위 추정

운영 SQLite (`data/db/app.sqlite3`) 스냅샷을 SELECT 로 가시화한 결과:

```sql
SELECT COUNT(*) AS total,
       COUNT(deadline_at) AS has_deadline,
       COUNT(received_at) AS has_received
FROM announcements;
-- → 53 | 53 | 53

SELECT source_type, COUNT(*) FROM announcements GROUP BY source_type;
-- → IRIS 31, NTIS 22

SELECT COUNT(*) FROM announcements
WHERE deadline_at IS NOT NULL
  AND strftime('%H:%M:%S', deadline_at) <> '00:00:00';
-- → 0   (모든 deadline_at 이 자정)

SELECT id, source_type, datetime(deadline_at) AS dl,
       json_extract(raw_metadata, '$.list_row.deadline_at_text') AS dl_raw
FROM announcements LIMIT 5;
-- 1|IRIS|2026-05-28 00:00:00|2026.05.28
-- 2|IRIS|2026-05-28 00:00:00|2026.05.28
-- 3|IRIS|2026-05-26 00:00:00|2026.05.26
-- ...
```

판정:
- `deadline_at` / `received_at` 의 **53/53건 전부** 가 `'YYYY-MM-DD 00:00:00 UTC'` 형식이다.
- `raw_metadata.list_row.deadline_at_text` 의 `'YYYY.MM.DD'` 와 컬럼값을 비교하면 **컬럼은 그 날짜의 0시(UTC)** = **KST 9시** 로, 의도된 KST 자정에서 **+9 시간 오차**.
- 즉 **misassumed-as-UTC 가정 일치율 100%**. 모든 IRIS/NTIS row 가 동일 결함 패턴을 갖는다.

(가정 명시) 운영 SQLite 가 본 작업 시점의 사실상 단일 인스턴스인 점, 그리고 외부 응답 파싱 경로가 `_parse_datetime_text` 단일 진입점이라는 점을 들어 위 비율을 전체 추정치로 사용한다.

backfill 전략 (00040-5 가이드용):
- `raw_metadata.list_row.deadline_at_text` / `received_at_text` 를 1차 근거 (원문 보존된 KST 가정 텍스트) 로 재파싱.
- 1차 근거가 결측이거나 형식이 달라 재파싱이 실패할 경우, 보조 규칙으로 **컬럼값 - 9 시간** 을 적용 (모든 row 가 자정인 현 상태에서는 1차 근거 결측 row 가 없을 가능성이 높음).
- 다른 datetime 컬럼 (`scraped_at`, `detail_fetched_at`, `updated_at`, `started_at`, `ended_at`, `expires_at` 등) 은 모두 `datetime.now(tz=UTC)` 경로로 저장돼 backfill **대상 아님**.

scrape_runs / scheduler_jobs / 사용자 액션 시간 (`read_at`, `decided_at`) 등 다른 테이블은 외부 텍스트 파싱 경로를 거치지 않으므로 영향 범위 **외**.

---

> **참고**: task 00040 의 8개 subtask(타임존 헬퍼 신설·Jinja2 필터·APScheduler tz·외부 응답 KST
> 가정·README.USER 갱신 등) 는 본 audit 후 모두 구현·머지되었다. 본 문서는 해당 작업의 **근거 자료**
> 로만 보존된다. 현재 시각 처리 컨벤션은 `PROJECT_NOTES.md` "컨벤션 — 시각 처리" 절을 참고한다.
