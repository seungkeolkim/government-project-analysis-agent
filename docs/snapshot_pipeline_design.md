# Phase 5a — delta 테이블 + 일자별 snapshot 인프라 설계 노트

> **작성 범위**: Task 00041 (Phase 5a) — 수집 파이프라인을 delta 기반으로
> 재설계하고 일자별(KST) `scrape_snapshots` 인프라를 도입한다. 대시보드 UI 와
> 차트 / 사용자별 위젯 / 비교 기준 드롭다운 / 캘린더 노출은 Phase 5b 의
> 범위로 명시 제외한다 (사용자 원문 "범위 밖" 항목 그대로).
>
> 본 문서는 `docs/scrape_control_design.md` / `docs/schema_phase1a.md` /
> `docs/db_portability.md` / `docs/canonical_identity_design.md` /
> `docs/status_transition_todo.md` 와 동일한 한국어·markdown 톤을 따른다.
> 후속 subtask (00041-2 ~ 00041-6) 가 코드/PR 설명에서 인용할 수 있도록
> § 번호와 헤더를 안정적으로 부여한다 — **이 문서가 살아 있는 동안 § 번호는
> 변경하지 않는다**. 새 절은 §15 이후로 append.
>
> 구현 본문(실제 함수 바디)은 포함하지 않는다. 각 모듈의 시그니처(이름 +
> 파라미터 + 반환 타입) 수준만 기술한다.

---

## §1. 스코프와 전제

### §1.1 이 task 에서 다루는 것 (5a)

- 수집 파이프라인을 **delta 단계 적재 → 종료 시점 단일 트랜잭션 apply** 로
  재배선한다. 본 테이블(`announcements` / `attachments`) 직접 UPSERT 는 사라진다.
- 일자별 변화 요약을 보존하는 **`scrape_snapshots`** 테이블을 신설한다.
  같은 KST 날짜의 여러 ScrapeRun 결과는 1 row 로 머지된다.
- **`merge_snapshot_payload(existing, new)`** 단독 함수 — 5종 카테고리
  (new / content_changed / transitioned_to_접수예정·접수중·마감) 머지 규칙 구현.
- **첨부 파일 GC 스크립트** (`scripts/gc_orphan_attachments.py`) — 본 테이블에
  더 이상 참조되지 않는 `data/downloads/` 파일을 정리한다.
- Migration: `delta_announcements`, `delta_attachments`, `scrape_snapshots`
  3 테이블 신설. 기존 `announcements` / `attachments` 스키마는 변경 없음.
- 문서: 본 문서, README.USER.md "수집 파이프라인 동작" 섹션 갱신.
- PROJECT_NOTES "수집 흐름" 갱신은 finalize 단계의 **MemoryUpdater 책임**
  이며 implementation subtask 들이 직접 다루지 않는다.

### §1.2 이 task 에서 다루지 않는 것 (범위 밖 — Phase 5b)

- 대시보드 UI 자체.
- 차트 라이브러리 번들링.
- 사용자별 위젯 (예: "내 미확인 N건").
- 비교 기준 드롭다운 (어제/지난주/지난달 등).
- 캘린더 + snapshot 가용 날짜 노출.
- snapshot.payload 의 ID 를 title/agency 로 풀어주는 JOIN 뷰
  (5b 의 dashboard view query 가 직접 처리).

### §1.3 절대 건드리지 않는 것 (회귀 금지)

- `announcements` / `attachments` 테이블 DDL — 본 task 의 migration 은
  **추가만** 한다.
- Phase 1a 의 4-branch 판정 로직 (`_upsert_announcement` /
  `_normalize_for_comparison` / `_detect_changes`) 의 시맨틱.
  **호출 시점만** delta apply 단계로 이동하고, 함수 시그니처와 변경 감지 비교
  필드(_CHANGE_DETECTION_FIELDS = title, status, deadline_at, agency)는 그대로
  재사용한다.
- Phase 1a 의 사용자 라벨링 reset (`_reset_user_state_on_content_change`) 호출
  규약 — `(d) new_version` 분기에서는 반드시, `(c) status_transitioned`
  분기에서는 호출하지 않는다.
- Phase 2 의 ScrapeRun lock (running row 1 개) 와 SIGTERM handler — 본 task 는
  이 위에 얹히는 형태이며 lock/시그널 정책을 변경하지 않는다.
- Phase 2 의 trigger 3종(`manual` / `scheduled` / `cli`).
- Phase 4 의 KST 컨벤션 — `snapshot_date` 는 ScrapeSnapshot.created_at 의
  **KST 날짜** 로 결정한다 (사용자 원문 그대로).
- Phase 0 의 db_portability.md — JSON 범용, `DateTime(timezone=True)`,
  batch_alter_table, constraint 이름 명시, downgrade 양방향 구현.

---

## §2. 현재 구조 요약 (탐사 결과)

### §2.1 수집 단계의 현재 흐름 (Phase 1a 이후)

`app/cli.py` 의 호출 순서:

```
_async_main()
   ├─ ScrapeRun lock 확보 (Phase 2)
   ├─ _orchestrate(...)
   │     └─ for source in active_sources:
   │           _run_source_announcements(adapter)
   │             ├─ for row in target_rows:
   │             │     ┌─ session_scope() #1 ─ upsert_announcement(payload)
   │             │     │     → 1차 4-branch 판정. (d) 분기에서 reset 동시 호출.
   │             │     ├─ session_scope() #2 ─ upsert_announcement_detail(...)
   │             │     ├─ session_scope() #3 ─ upsert_attachment(...)
   │             │     │     + 2차 감지 (첨부 sha256) → 변경이면
   │             │     │     reapply_version_with_reset(session, id)
   │             │     │     로 is_current 순환 + reset.
   │             │     └─ ...
   │             └─ 통계 누적
   └─ finalize_scrape_run(...)  ← ScrapeRun.status 마감
```

핵심은 **공고 1건마다 본 테이블에 직접 UPSERT 가 즉시 반영된다**는 것이다.
부분 실패(중간 SIGTERM, 예외) 가 발생하면 일부 공고만 적용된 상태로 본
테이블이 남고, 어떤 snapshot 에도 변화가 잡히지 않아 추후 일자별 비교에서
"보이지 않는 변경"이 누적된다.

### §2.2 4-branch 판정의 현재 시점

`upsert_announcement(session, payload)` 내부:

| 분기 | 조건 | 본 테이블 동작 | reset 호출 |
|---|---|---|---|
| (a) created | 기존 is_current row 없음 | INSERT | X |
| (b) unchanged | 비교 필드 변경 없음 | (변경 없음) | X |
| (c) status_transitioned | changed_fields == {"status"} | in-place UPDATE | **X** |
| (d) new_version | 그 외 변경 | 봉인 + 신규 INSERT | **O** (`_reset_user_state_on_content_change`) |

2차 감지(첨부 sha256 기반)는 (d) 와 동일 시맨틱(`reapply_version_with_reset`)
으로 트리거되며, 1차 action 이 `unchanged` / `status_transitioned` 인 경로
에서만 발동한다 (`created` / `new_version` 1차 경로에서 2차 감지를 건너뛰는
이유는 `app/cli.py` 모듈 docstring 의 "추가 가드" 절 참조).

### §2.3 ScrapeRun finalize 와 lock 해제

`finalize_scrape_run(session, run_id, *, status, source_counts, error_message)`
는 ended_at + status + source_counts + error_message 를 한 번에 세팅하며
`SCRAPE_RUN_TERMINAL_STATUSES` (completed / cancelled / failed / partial)
중 하나를 부여한다. CLI / 웹 / 스케줄러 모두 같은 헬퍼로 마감하며 idempotent
이다 (이미 terminal 이면 no-op).

### §2.4 첨부 파일 저장소

- 루트: `Settings.download_dir` (기본 `./data/downloads/`).
- 구조: `{download_dir}/{source_type}/{sanitized_source_announcement_id}/{sanitized_filename}`.
- `Attachment.stored_path` 컬럼이 위 절대/상대 경로를 보관한다.
- 다운로드는 즉시 파일시스템에 떨어진다 — DB 트랜잭션과 분리된다.

---

## §3. 모듈 배치 (신규/수정 파일 지도)

후속 subtask 의 "건드릴 파일" 레퍼런스. 이 목록 밖의 파일은 변경하지 않는다.

### §3.1 신규 생성

```
alembic/versions/2026XXXX_XXXX_<rev>_delta_snapshot_tables.py
                                              # delta_announcements, delta_attachments, scrape_snapshots

app/db/models.py                              # ORM 모델 3개 추가 (DeltaAnnouncement / DeltaAttachment / ScrapeSnapshot)
app/db/repository.py                          # delta apply + snapshot 머지 헬퍼 (§7, §8, §9)
app/db/snapshot.py                            # merge_snapshot_payload 단독 함수 (§9.3)
                                              # — repository.py 에서 import 해서 호출.
                                              # 단독 모듈로 분리하는 이유: 유닛 테스트가
                                              # session 없이 import 1줄로 가능하도록.

scripts/gc_orphan_attachments.py              # §11 GC 스크립트 (CLI entry point)

tests/db/test_delta_apply.py                  # delta → 본 테이블 4-branch 회귀 + 트랜잭션 롤백
tests/db/test_snapshot_merge.py               # merge_snapshot_payload 5종 카테고리 머지 룰
tests/db/test_scrape_snapshot_upsert.py       # 같은 KST 날짜 UPSERT vs 신규 INSERT
tests/scripts/test_gc_orphan_attachments.py   # --dry-run / 실제 삭제 / 본 테이블 참조 보존

docs/snapshot_pipeline_design.md              # 본 문서
docs/00041-verification.md                    # 11 항 종합 회귀 결과 기록 (00041-6)
```

### §3.2 수정

```
app/cli.py                                    # _run_source_announcements 의 본 테이블 직접 UPSERT 를
                                              # delta INSERT 로 재배선 (§7.1 ~ §7.3).
                                              # _async_main 의 finalize 직전에 apply_delta_to_main
                                              # 호출 추가 (§7.4).
app/scraper/attachment_downloader.py          # 첨부 다운로드 후 DB 메타 적재를 attachments 본 테이블이
                                              # 아니라 delta_attachments 로 변경 (§7.2).
                                              # 파일 다운로드 자체는 그대로 data/downloads/ 즉시 저장.

app/scheduler/job_runner.py                   # (선택) 일 1회 GC job 등록 — §11.4 참조.

README.USER.md                                # "수집 파이프라인 동작" 섹션 신설/갱신 (00041-6).
PROJECT_NOTES.md                              # MemoryUpdater 가 finalize 에서 갱신 — 본 task 에서 직접 수정 X.
```

### §3.3 Subtask 매핑

| subtask | 커버 범위 | 인용 § |
|---|---|---|
| 00041-1 | 본 문서 — 코드 변경 없음 | 전체 |
| 00041-2 | Alembic migration + ORM 3종 | §4, §5, §6 |
| 00041-3 | delta apply 로직 (수집 시 delta 적재 + 종료 시 본 테이블 4-branch UPSERT) | §7, §8 |
| 00041-4 | snapshot 생성/머지 (merge_snapshot_payload + ScrapeSnapshot UPSERT) | §9, §10 |
| 00041-5 | scripts/gc_orphan_attachments.py | §11 |
| 00041-6 | README.USER.md "수집 파이프라인 동작" + 11 항 회귀 | §13, §14 |

---

## §4. 신규 테이블 스키마

> **준수 원칙** (`docs/db_portability.md` §1 ~ §4 그대로):
> - 모든 시간 컬럼: `DateTime(timezone=True)`.
> - JSON 컬럼: `sa.JSON()` 범용 — `JSONB` 금지.
> - 모든 constraint: 이름 부여 (`pk_/uq_/fk_/ck_/ix_` prefix 규칙, schema_phase1a.md §0).
> - `String(N)` 의 N 명시.
> - migration 은 `batch_alter_table` 사용 + `upgrade()` / `downgrade()` 양방향.

### §4.1 `delta_announcements`

수집 단계에서 적재되는 공고 메타. **매 ScrapeRun 종료 시 비워진다.**

| 컬럼 | 타입 | NULL | Default | 비고 |
|---|---|---|---|---|
| `id` | `Integer` | NO | AUTO | PK |
| `scrape_run_id` | `Integer` | NO | — | FK → `scrape_runs.id` ON DELETE CASCADE |
| `source_type` | `String(32)` | NO | — | `IRIS` / `NTIS` 등 |
| `source_announcement_id` | `String(128)` | NO | — | 소스가 부여한 공고 ID |
| `title` | `Text` | NO | — | 1차 비교 필드 |
| `status` | `String(32)` | NO | — | 본 테이블 enum 과 달리 plain String. raw 값 입구 역할 — apply 단계가 정규화. CHECK 없음. |
| `agency` | `String(255)` | YES | — | 1차 비교 필드 |
| `received_at` | `DateTime(tz=True)` | YES | — | 비교 제외 (announcements 와 동일) |
| `deadline_at` | `DateTime(tz=True)` | YES | — | 1차 비교 필드 |
| `detail_url` | `Text` | YES | — | |
| `detail_html` | `Text` | YES | — | 상세 수집 결과 (있을 때만 채움) |
| `detail_text` | `Text` | YES | — | |
| `detail_fetched_at` | `DateTime(tz=True)` | YES | — | |
| `detail_fetch_status` | `String(16)` | YES | — | |
| `ancm_no` | `String(64)` | YES | — | canonical_key 재계산용 (NTIS 상세 후 확정값 포함) |
| `raw_metadata` | `JSON` | NO | `{}` | 어댑터가 내려준 원본 메타 |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | delta INSERT 시각 |

- **CHECK**: 없음 — delta 는 raw 값 입구 역할이라 도메인을 좁히지 않는다 (apply 단계에서 `_coerce_status` 가 정규화).
- **FK**: `fk_delta_announcements_scrape_run_id (scrape_run_id) → scrape_runs.id ON DELETE CASCADE`
- **INDEX**:
  - `ix_delta_announcements_scrape_run_id (scrape_run_id)` — apply 단계 전수 조회.
  - `ix_delta_announcements_source_lookup (source_type, source_announcement_id)` —
    apply 단계가 본 테이블 row 를 매칭할 때 source 키로 빠른 조회.

> **삭제 정책**: apply 트랜잭션 마지막 단계에서 해당 `scrape_run_id` 의 row 를
> 전수 DELETE. ON DELETE CASCADE 가 아닌 명시적 DELETE 를 사용한다 — 트랜잭션
> 안에서 명시적으로 비워야 "delta 비움" 단계가 추적 가능해진다 (사용자 원문
> "delta 비우기" 가 명시적 단계로 보존되도록).

### §4.2 `delta_attachments`

수집 단계에서 적재되는 첨부 메타. delta_announcements 와 함께 비워진다.

| 컬럼 | 타입 | NULL | Default | 비고 |
|---|---|---|---|---|
| `id` | `Integer` | NO | AUTO | PK |
| `delta_announcement_id` | `Integer` | NO | — | FK → `delta_announcements.id` ON DELETE CASCADE |
| `original_filename` | `String(512)` | NO | — | |
| `stored_path` | `Text` | NO | — | `data/downloads/...` 절대/상대 경로 (즉시 다운로드 결과) |
| `file_ext` | `String(16)` | NO | — | |
| `file_size` | `BigInteger` | YES | — | |
| `download_url` | `Text` | YES | — | |
| `sha256` | `String(64)` | YES | — | 변경 감지 / 중복 판정용 (다운로드 실패 시 NULL) |
| `downloaded_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |

- **FK**: `fk_delta_attachments_delta_announcement_id (delta_announcement_id) → delta_announcements.id ON DELETE CASCADE`
- **INDEX**: `ix_delta_attachments_delta_announcement_id (delta_announcement_id)`

> 매 ScrapeRun 종료 후 row 가 0 으로 리셋되므로 인덱스 부담은 무시할 수준이다
> (사용자 원문 "delta 테이블은 매번 비움. 인덱스 부담 거의 없음").

### §4.3 `scrape_snapshots`

KST 날짜 단위 변화 요약. 같은 KST 날짜에 여러 ScrapeRun 종료 시 1 row 에 머지된다.

| 컬럼 | 타입 | NULL | Default | 비고 |
|---|---|---|---|---|
| `id` | `Integer` | NO | AUTO | PK |
| `snapshot_date` | `Date` | NO | — | KST 날짜. UNIQUE |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | 첫 INSERT 시각 (UTC 저장) |
| `updated_at` | `DateTime(tz=True)` | NO | `_utcnow()`, `onupdate=_utcnow()` | 마지막 머지 시각 |
| `payload` | `JSON` | NO | `{}` | 5종 카테고리 변화 본문 — 구조는 §10 |

- **UNIQUE**: `uq_scrape_snapshots_snapshot_date (snapshot_date)` — 같은 KST 날짜 1 row.
- **INDEX**: 이미 UNIQUE 가 implicit index 로 `snapshot_date` 조회를 커버. 추가 인덱스
  없음(5b 의 일자 범위 조회는 인덱스로 충분히 커버됨).

> **`snapshot_date` 는 `Date` 타입(자정 정보 없음)** 으로 둔다. 사용자 원문
> "snapshot_date 는 created_at 의 KST 날짜" 를 따른다. KST 자정 변환 로직은
> §10.2 가 정의하며, `app.timezone.now_kst().date()` 를 사용한다.

### §4.4 ondelete 정책 요약

| 관계 | 정책 | 이유 |
|---|---|---|
| `scrape_runs → delta_announcements` | CASCADE | 수집 실행이 어떤 이유로 통째 삭제되면(설계상 거의 없지만) 잔여 delta 도 함께 정리 |
| `delta_announcements → delta_attachments` | CASCADE | delta 비우기 시 자동 cascade |
| `scrape_snapshots` | (FK 없음) | 본 테이블 의존이 없는 요약 row |

---

## §5. 제약 이름 전수표 (Alembic migration 에서 그대로 사용)

| 테이블 | 유형 | 이름 |
|---|---|---|
| delta_announcements | FK | `fk_delta_announcements_scrape_run_id` |
| delta_attachments | FK | `fk_delta_attachments_delta_announcement_id` |
| scrape_snapshots | UNIQUE | `uq_scrape_snapshots_snapshot_date` |

`pk_*` 는 단일 컬럼 PK 라 Alembic 자동 명명을 허용한다 (schema_phase1a.md §0
규칙과 일치).

---

## §6. 인덱스 전수표

| 테이블 | 이름 | 컬럼 | 용도 |
|---|---|---|---|
| delta_announcements | `ix_delta_announcements_scrape_run_id` | `scrape_run_id` | apply 단계 전수 조회 |
| delta_announcements | `ix_delta_announcements_source_lookup` | `source_type, source_announcement_id` | 본 테이블 매칭 |
| delta_attachments | `ix_delta_attachments_delta_announcement_id` | `delta_announcement_id` | apply 단계 첨부 매칭 |
| scrape_snapshots | (UNIQUE 가 implicit index 제공) | `snapshot_date` | 일자 조회 / 머지 lookup |

---

## §7. 파이프라인 처리 흐름

### §7.1 ScrapeRun 시작 (변경 없음)

Phase 2 의 `start_scrape_run` 또는 CLI 진입 경로가 그대로 동작한다.
`scrape_runs.status='running'` 인 row 1 개를 보장 (lock 규칙 — Phase 2 §7.3).

### §7.2 공고 1건 처리 (변경: 본 테이블 → delta)

기존 (`Phase 1a`):

```
session_scope() ─ upsert_announcement(payload)   # 본 테이블 INSERT/UPDATE
session_scope() ─ upsert_announcement_detail(...) # 본 테이블 UPDATE
session_scope() ─ upsert_attachment(...)          # 본 테이블 INSERT
                  + 2차 감지 reapply_version_with_reset
```

변경 후 (`Phase 5a`):

```
첨부 다운로드 (data/downloads/ 즉시 저장 — 변경 없음)

session_scope() ─ insert_delta_announcement(scrape_run_id, payload)
                  → delta_announcements 에 INSERT, delta_announcement.id 확보
                  → 상세 수집 결과(detail_html / detail_text / fetch_status)도
                    같은 row 에 채워 넣는다 (목록·상세·첨부 = 한 row)
                  → 첨부 1건마다 insert_delta_attachment(delta_announcement_id, ...)
                  → 본 테이블은 건드리지 않는다.
```

핵심:

- **본 테이블(`announcements` / `attachments`) 은 수집 중 0회 변경된다.**
  사용자 입장의 일관성: 사용자 웹이 보는 데이터는 ScrapeRun 종료 시점에만
  바뀐다.
- 4-branch 판정 / 2차 감지 / reset 은 이 단계에서 **일어나지 않는다**.
  delta apply 단계로 이동했다 (§8).
- 첨부 파일 자체는 즉시 `data/downloads/` 에 다운로드되어 디스크에 남는다 —
  트랜잭션 보호 밖. 트랜잭션이 실패해 delta 가 폐기되면 파일은 고아가 되며,
  다음 GC 사이클(§11) 에서 정리된다.

### §7.3 공고 1건 단위 격리 (변경 없음)

Phase 1a 와 마찬가지로 한 공고 처리 실패가 같은 소스의 다음 공고를
중단시키지 않는다. 첨부 다운로드 실패도 delta 단계에서 격리된다 — `sha256`
NULL 인 row 가 delta_attachments 에 남고, apply 단계의 2차 감지에서 false-
positive 를 만들지 않도록 §8.3 가 보장한다.

### §7.4 ScrapeRun 종료 시 단일 트랜잭션 (delta apply + snapshot)

`_async_main` 의 finalize 단계 직전에 새 헬퍼 호출:

```python
apply_delta_to_main(
    session,
    scrape_run_id=scrape_run_id,
    snapshot_date=now_kst().date(),  # KST 기준 (Phase 4 컨벤션)
)
```

이 함수는 **하나의 `session_scope()` (= 하나의 SQLite write 트랜잭션) 안에서**
다음 단계를 순차 실행한다 (§8 가 상세):

1. 해당 `scrape_run_id` 의 delta_announcements 와 delta_attachments 를 모두 읽는다.
2. 본 테이블의 `announcements` 와 4-branch 비교 → action / changed_fields 수집.
3. INSERT/UPDATE 적용 + 사용자 라벨링 reset (Phase 1a 로직 재사용).
4. 4-branch 결과를 5종 카테고리(§10.1) 로 매핑 → 새 `payload`.
5. 같은 KST 날짜의 ScrapeSnapshot 이 있으면 `merge_snapshot_payload(existing, new)` 로
   머지, 없으면 신규 INSERT.
6. 해당 scrape_run 의 delta_announcements / delta_attachments 를 명시적 DELETE.

트랜잭션이 실패하면 SQLAlchemy 가 자동 rollback — 본 테이블 / snapshot /
delta 모두 트랜잭션 시작 직전 상태로 되돌아간다 (사용자 원문 "delta 그대로,
본 테이블 변화 없음, 다음 수집에서 재시도 가능").

### §7.5 finalize_scrape_run 호출

apply 트랜잭션이 정상 commit 되면 별도 트랜잭션으로 `finalize_scrape_run(...,
status=...)` 를 호출해 ScrapeRun 을 마감한다. apply 가 예외로 실패한 경우는
trigger 별 종료 코드 처리에 따라 `status='failed'` + `error_message` 에
원인을 박는다 (§12.1).

> **lock 해제 시점**: ScrapeRun.status 가 terminal 로 바뀌는 시점이 곧 lock
> 해제 시점이다 (Phase 2 의 lock 규칙). apply 트랜잭션 commit → finalize
> commit 의 짧은 간극 동안에는 row.status 가 여전히 `running` 이다 — 두 번째
> 트랜잭션이 커밋되어야 새 ScrapeRun 이 시작될 수 있다.

---

## §8. delta apply 로직 상세 (§7.4 의 1 ~ 3 단계)

### §8.1 신규 헬퍼 시그니처

`app/db/repository.py` 에 추가한다.

```python
@dataclass
class DeltaApplyResult:
    """apply_delta_to_main 의 반환값.

    snapshot 머지 단계(§9) 가 이 값을 받아 카테고리 payload 를 구성한다.
    """
    new_announcement_ids: list[int]            # (a) created — 본 테이블에 새로 INSERT 된 announcements.id
    content_changed_announcement_ids: list[int]  # (d) new_version + 2차 감지 — content 변경이 일어난 announcements.id
    transition_records: list[TransitionRecord]   # (c) status_transitioned — from/to 함께
    upsert_actions: dict[str, int]               # action_counts (created/unchanged/new_version/status_transitioned)


@dataclass(frozen=True)
class TransitionRecord:
    """status 단독 전이 1건의 기록.

    announcement_id: 전이가 적용된 본 테이블 row 의 id (in-place UPDATE 후의 id).
    status_from: 적용 전 본 테이블 status 값 (한글 문자열).
    status_to: 적용 후 status 값.
    """
    announcement_id: int
    status_from: str
    status_to: str


def apply_delta_to_main(
    session: Session,
    *,
    scrape_run_id: int,
    snapshot_date: date,
) -> DeltaApplyResult:
    """delta_announcements / delta_attachments 를 본 테이블에 4-branch 적용한다.

    동작 (모두 같은 session 내, 호출자가 commit):
      1. delta_announcements WHERE scrape_run_id 전수 조회.
      2. 각 row 마다 upsert_announcement(session, payload) 호출 (Phase 1a 시맨틱 그대로).
         - status_from 캡처: (c)/(d) 분기 진입 직전의 본 테이블 status 값.
         - DeltaApplyResult 의 5종 분류로 결과 누적.
      3. delta_attachments → 본 테이블 attachments 적용 (sha256 기반 upsert).
         첨부만 변경된 케이스를 위해 2차 감지(reapply_version_with_reset) 도 같은
         session 안에서 수행 — Phase 1a 의 2차 감지 가드(`upsert_action`
         allowlist) 를 그대로 재사용.
      4. delta_announcements / delta_attachments WHERE scrape_run_id 전수 DELETE.

    트랜잭션 경계:
      session.flush 까지만 수행. commit/rollback 은 호출자 책임. 호출자(`_async_main`)
      는 본 함수 호출과 snapshot 생성/머지(§9) 를 같은 session_scope 안에 묶어
      atomic 보장한다.
    """
```

### §8.2 4-branch 판정 시점 이동의 영향 범위

기존 `upsert_announcement(session, payload)` 의 동작은 그대로다 — Phase 1a 의
4-branch + reset 로직을 함수 본문 그대로 호출한다. 변하는 것은 **호출 시점**:

- 기존: 공고 1건 수집 직후 즉시 호출 (수집 중).
- 변경: 수집 종료 후 delta 를 apply 하는 단계에서 호출 (수집 종료 직후).

이로 인해 영향 받는 항목:

| 항목 | 영향 | 처리 |
|---|---|---|
| 변경 감지 비교 필드 (`_CHANGE_DETECTION_FIELDS`) | **변동 없음** | 그대로 사용 |
| (c) status 단독 전이 — in-place UPDATE | **시맨틱 동일** | apply 단계에서 동일하게 발생 |
| (d) new_version — 봉인 + 신규 INSERT | **시맨틱 동일** | apply 단계에서 동일하게 발생 |
| reset (`_reset_user_state_on_content_change`) | **시맨틱 동일** | (d) 분기에서 그대로 호출 |
| 2차 감지 (`reapply_version_with_reset`) | **호출 위치만 이동** | apply 단계의 첨부 적용 직후 호출 |
| reset 의 atomic 보장 | **트랜잭션 경계가 더 커진다** | 단일 트랜잭션 안에서 모두 수행되므로 더 강해진다 |
| 사용자 웹 요청과 충돌 | **확률 감소** | 본 테이블 직접 변경 횟수가 N→1 로 줄어듦. 단 트랜잭션 길이는 길어짐 — §12.2 |
| `announcement_id` 결정 시점 | **apply 후로 이동** | snapshot.payload 에 박힐 ID 도 apply 결과에서 수집 (사용자 원문 그대로) |

### §8.3 첨부만 변경된 케이스 (2차 감지)

사용자 원문 검증 7번: "첨부만 변경된 공고 → content_changed 에만 박힘
(status 변화 없음 가정)".

처리 순서 (apply 트랜잭션 내):

1. delta_announcements row → `upsert_announcement(...)` 호출 → 1차 action 결정
   (대부분 `unchanged` 가 된다 — title/status/deadline_at/agency 변동이 없으므로).
2. delta_attachments row 들을 본 테이블 attachments 에 sha256 기반 upsert 적용.
3. signature_before (apply 시작 직전 본 테이블의 sha256 집합) vs
   signature_after (apply 후) 비교 → 변경이면 `reapply_version_with_reset(session,
   announcement_id)` 호출.
4. 2차 감지가 `new_version` 을 발생시킨 경우 그 announcement_id 를
   `content_changed_announcement_ids` 에 포함한다 (§9.1 의 분류 규칙).

> **2차 감지 가드 재확인**: Phase 1a 의 가드 (`upsert_action ∈ {unchanged,
> status_transitioned}` 일 때만 2차 감지 발동 + 다운로드 실패 0 + 다운로드
> 시도 > 0) 를 apply 단계에서도 그대로 유지한다. created / 1차 new_version
> 경로에서 2차를 또 트리거하면 row 가 중복 발생하므로 발동하지 않는다.
> apply 단계에서는 created / 1차 new_version 도 발생할 수 있으므로 가드는
> 필수다.

### §8.4 1차 / 2차 변경 감지 통합

사용자 원문 주의: "Phase 1a 의 1차/2차 변경 감지 로직 통합: delta 안에 첨부
메타까지 다 들어있으니 한 번에 비교".

apply 단계는 delta 에 이미 첨부 sha256 이 채워져 있다 (수집 단계가 다운로드
완료 후 메타를 적재). 따라서 본 테이블 비교 시 `_CHANGE_DETECTION_FIELDS` 4
필드 + 첨부 sha256 차이를 **하나의 비교 패스**로 결정할 수도 있다.

**권장안: 1차/2차 분리는 유지** (시맨틱 보존). 이유:

- Phase 1a 의 4-branch 시맨틱 — 특히 (c) status_transitioned 의 in-place
  UPDATE / no-reset — 가 status 단독 변경을 올바르게 처리하려면 첨부 비교를
  분리해야 한다.
- 분기 통합은 표면적인 단순화에 그치고, status 단독 전이 + 첨부 변경 동시
  발생 같은 결합 분기에서 시맨틱이 모호해진다.
- 2차 감지 가드(`upsert_action ∈ {unchanged, status_transitioned}`) 도 1차
  결과를 알아야 한다.

따라서 구현은 **1차 → 첨부 적용 → 2차 감지** 순서를 그대로 유지하되,
공고 1건당 새 session 을 열지 않고 **apply 트랜잭션 안에서 모두 처리**한다.

### §8.5 `announcement_id` 결정 시점

snapshot.payload 에 박힐 announcement_id 는 **apply 후 본 테이블에 INSERT 된
neue row 의 id** 다 (사용자 원문 그대로). DeltaApplyResult 가 이 id 를
field 별로 누적해 §9 가 그대로 사용한다.

- (a) created → 새로 INSERT 된 row 의 id.
- (d) new_version → INSERT 된 신규 row(`is_current=True`) 의 id (구 row 가
  아니다).
- 2차 감지 new_version → reapply 후의 신규 row 의 id.
- (c) status_transitioned → in-place UPDATE 된 기존 row 의 id (변동 없음).
- (b) unchanged → 본 테이블에 변화 없으므로 어떤 카테고리에도 박지 않는다.

---

## §9. 5종 카테고리 매핑 + snapshot 생성

### §9.1 분류 규칙 (DeltaApplyResult → snapshot.payload)

apply 결과를 5종으로 매핑한다 (사용자 원문 "5종 분류" 그대로).

| 카테고리 | 본 테이블 적용 결과 | payload 표현 |
|---|---|---|
| `new` | (a) created — 그날 처음 본 테이블에 INSERT | `int[]` (announcement_id 배열) |
| `content_changed` | (d) new_version 또는 2차 감지(첨부 변경) → reapply | `int[]` (announcement_id 배열) |
| `transitioned_to_접수예정` | (c) status_from != "접수예정" AND status_to == "접수예정" | `[{id, from}, ...]` |
| `transitioned_to_접수중` | (c) status_to == "접수중" 이고 from != "접수중" | `[{id, from}, ...]` |
| `transitioned_to_마감` | (c) status_to == "마감" 이고 from != "마감" | `[{id, from}, ...]` |

> **(b) unchanged**: 어떤 카테고리에도 들어가지 않는다.
>
> **(d) new_version 의 status 도 동시에 바뀐 경우**: content_changed 에만
> 들어간다. 사용자 원문은 transition 카테고리를 "(c) status 단독 전이" 로
> 한정하지 않았으나, 머지 규칙(§9.2 (5)/(6)) 의 "신규 + 전이 둘 다 유지" 와
> 검증 6 ("같은 공고 신규 + 전이 → 신규 + transitioned 둘 다 박힘") 의 대칭을
> 위해 **(d) 는 content_changed 전용**으로 분류한다. (c) 는 transition 전용.
> 이 분류는 `snapshot_date` 단위 머지 룰의 일관성을 결정하므로 고정이다.

### §9.2 같은 ScrapeRun 안에서의 동시 발생

같은 공고가 같은 ScrapeRun 안에서 (a) + (c) 또는 (a) + (d) 를 동시에 만족할
수는 없다 — apply 단계에서 announcement 1건은 4-branch 중 정확히 1개로 결정
된다. 따라서 ScrapeRun 단위 결과에서는 한 announcement_id 가 정확히 1 개
카테고리(또는 0개) 에만 박힌다.

**검증 6 (신규 + 전이 동시)** 은 같은 KST 날짜 안 **여러 ScrapeRun** 에서 한
공고가 (a) → (c) 순서로 등장하는 케이스다. 이 경우 첫 ScrapeRun 결과에는
new 만, 두 번째 ScrapeRun 결과에는 transitioned_to_X 만 있고, 머지(§9.4) 가
둘 다 보존한다.

### §9.3 `merge_snapshot_payload` 단독 함수 시그니처

`app/db/snapshot.py` 에 단독 모듈로 분리. session 미의존, 순수 함수 (유닛
테스트 1줄 import).

```python
def merge_snapshot_payload(
    existing: dict,
    new: dict,
) -> dict:
    """기존 snapshot.payload 와 이번 ScrapeRun 의 새 payload 를 머지한다.

    카테고리별 머지 규칙 (사용자 원문 그대로):
      - new: ID set union.
      - content_changed: ID set union.
      - transitioned_to_X (3종): 같은 announcement_id 가 여러 to 카테고리에
        박히면 첫 from 유지 + 마지막 to 갱신. 최종 from == to 면 전이 자체를
        제거 (실질 변화 없음).

    검증 4 시나리오 (접수예정 → 접수중 → 마감):
      ScrapeRun1: transitioned_to_접수중 = [{id, from='접수예정'}]
      ScrapeRun2: transitioned_to_마감 = [{id, from='접수중'}]
      → 머지 후: transitioned_to_마감 = [{id, from='접수예정'}],
                  transitioned_to_접수중 에서 id 제거.

    검증 5 시나리오 (접수중 → 마감 → 접수중 정정):
      ScrapeRun1: transitioned_to_마감 = [{id, from='접수중'}]
      ScrapeRun2: transitioned_to_접수중 = [{id, from='마감'}]
      → 머지 결과 from='접수중', to='접수중' → 제거.
        transitioned_to_마감 / transitioned_to_접수중 모두에서 id 제거.

    counts 는 머지 후 5종 배열 길이를 기준으로 재계산한다 — 입력 counts 는
    무시한다 (truth source 는 배열).

    순수 함수: existing 과 new 모두 수정하지 않는다 — 새 dict 를 반환한다.

    Args:
      existing: 기존 ScrapeSnapshot.payload (없으면 빈 카테고리로 채운 dict).
      new:      이번 ScrapeRun 의 DeltaApplyResult 를 §10 구조로 직렬화한 dict.

    Returns:
      머지된 payload dict (§10 구조 그대로).

    Raises:
      ValueError: 카테고리 키가 §10 의 정의를 벗어나거나, transition 항목에
                  id/from 키가 없을 때.
    """
```

### §9.4 머지 알고리즘 (§9.3 의 본문 의사 코드)

5종 카테고리 머지 규칙을 다음 순서로 적용한다.

**(1) `new`** — `set(existing.new) | set(new.new)` → 정렬된 list.

**(2) `content_changed`** — `set(existing.content_changed) | set(new.content_changed)` → 정렬된 list.

**(3) transition (3개 카테고리 통합 처리)** — 머지 본질이 카테고리 횡단 이동
이므로 3개를 한꺼번에 다룬다.

```
# 의사코드
def _merge_transitions(existing_payload, new_payload):
    """3개 transitioned_to_X 카테고리를 announcement_id 단위로 통합 머지."""
    by_id: dict[int, dict] = {}
    # ① existing 을 by_id 에 적재 (id, from, to)
    for to_label in ("접수예정", "접수중", "마감"):
        key = f"transitioned_to_{to_label}"
        for entry in existing_payload.get(key, []):
            by_id[entry["id"]] = {
                "id": entry["id"],
                "from": entry["from"],
                "to": to_label,
            }
    # ② new 항목으로 갱신 — first from 유지 + last to 갱신
    for to_label in ("접수예정", "접수중", "마감"):
        key = f"transitioned_to_{to_label}"
        for entry in new_payload.get(key, []):
            ann_id = entry["id"]
            if ann_id in by_id:
                # 이전 머지 결과 위에 새 전이 적용
                # from 은 첫 번째 머지 항목의 from 유지 (전이 체인의 시작점)
                by_id[ann_id]["to"] = to_label
            else:
                by_id[ann_id] = {
                    "id": ann_id,
                    "from": entry["from"],
                    "to": to_label,
                }
    # ③ 최종 from == to 인 항목 제거 (실질 변화 없음)
    purged = {ann_id: rec for ann_id, rec in by_id.items()
              if rec["from"] != rec["to"]}
    # ④ to 별로 다시 분배
    output: dict[str, list[dict]] = {
        "transitioned_to_접수예정": [],
        "transitioned_to_접수중": [],
        "transitioned_to_마감": [],
    }
    for rec in purged.values():
        key = f"transitioned_to_{rec['to']}"
        output[key].append({"id": rec["id"], "from": rec["from"]})
    # ⑤ 각 list 를 announcement_id asc 로 정렬 (재현 가능성)
    for key in output:
        output[key].sort(key=lambda e: e["id"])
    return output
```

**(4) counts 재계산** — 5종 배열 길이를 그대로 사용.

```python
counts = {
    "new": len(merged["new"]),
    "content_changed": len(merged["content_changed"]),
    "transitioned_to_접수예정": len(merged["transitioned_to_접수예정"]),
    "transitioned_to_접수중": len(merged["transitioned_to_접수중"]),
    "transitioned_to_마감": len(merged["transitioned_to_마감"]),
}
```

### §9.5 머지 엣지 케이스 (5종 × 합치기)

| # | 시나리오 | 머지 입력 | 머지 결과 |
|---|---|---|---|
| E1 | 검증 1 — 단일 ScrapeRun 정상 종료 | new={42}, content_changed={}, transitioned_*={} | `payload.new=[42]`, 나머지 빈 배열 |
| E2 | 검증 3 — 같은 날 2회 수집 (서로 다른 공고) | run1.new={42}, run2.new={43} | `new=[42, 43]` (set union) |
| E3 | 검증 3 — 같은 날 2회 수집 (같은 공고가 또 created 될 수는 없음) | — | apply 단계 (a) 분기는 본 테이블에 같은 (source_type, source_announcement_id) is_current row 가 없을 때만 발생. 같은 날 두 번째 수집에는 이미 row 가 있으므로 발생 불가 → 머지 충돌 없음 |
| E4 | 검증 4 — 접수예정→접수중→마감 | run1.transitioned_to_접수중=[{77, '접수예정'}], run2.transitioned_to_마감=[{77, '접수중'}] | 머지 후 `transitioned_to_마감=[{77, '접수예정'}]`, transitioned_to_접수중 에서 77 제거 |
| E5 | 검증 5 — 접수중→마감→접수중(정정) | run1.transitioned_to_마감=[{99, '접수중'}], run2.transitioned_to_접수중=[{99, '마감'}] | 머지 후 from='접수중', to='접수중' → 둘 다에서 99 제거 (실질 변화 없음) |
| E6 | 검증 6 — 신규 + 전이 동시 | run1.new={101}, run2.transitioned_to_마감=[{101, '접수중'}] | `new=[101]`, `transitioned_to_마감=[{101, '접수중'}]` 둘 다 유지 |
| E7 | 검증 7 — 첨부만 변경 | run1.content_changed={250} (status 동일) | `content_changed=[250]`, transition 카테고리 빈 배열 |
| E8 | 같은 공고가 같은 날 (d) → 또 (d) | run1.content_changed={500}, run2.content_changed={500} | `content_changed=[500]` (set union) |
| E9 | 같은 공고가 같은 날 (a) → (d) | 같은 ScrapeRun 안이면 §9.2 에 따라 발생 불가. 다른 ScrapeRun 안이면 (a) 는 첫 등장이므로 두 번째 ScrapeRun 에서 (d) 가 발생할 수 있다. | run1.new={777}, run2.content_changed={777} → `new=[777]`, `content_changed=[777]` 둘 다 유지 (E6 와 같은 처리). 5b 의 표시 측에서 중복 노출은 disjoint set view 로 정리 — 본 task 범위 밖 |
| E10 | 한 공고 transition 3 hop 이상 (예: 접수예정→접수중→마감→접수중) | run1.t_접수중=[{1,'접수예정'}], run2.t_마감=[{1,'접수중'}], run3.t_접수중=[{1,'마감'}] | 단계별 머지: ①→t_접수중[{1,'접수예정'}], ②→t_마감[{1,'접수예정'}], ③→from='접수예정' to='접수중'. 결과 `t_접수중=[{1,'접수예정'}]`, t_마감 에서 1 제거 |

### §9.6 ScrapeSnapshot UPSERT 시그니처

```python
def upsert_scrape_snapshot(
    session: Session,
    *,
    snapshot_date: date,
    new_payload: dict,
) -> ScrapeSnapshot:
    """같은 KST 날짜의 row 가 있으면 머지, 없으면 신규 INSERT.

    동작 (호출자 session 사용):
      1. SELECT scrape_snapshots WHERE snapshot_date = :snapshot_date
      2. row 없음 → INSERT, payload=new_payload (단, new_payload 도 빈 카테고리
                            기본값을 채워 정규화한다 — _normalize_payload).
      3. row 있음 → existing.payload 를 _normalize_payload 후
                    merge_snapshot_payload(existing, new_payload) 결과로
                    UPDATE. updated_at 은 onupdate 가 자동 갱신.

    트랜잭션 경계: 호출자가 commit. session.flush 까지만 수행.

    Args:
      session: 호출자 세션.
      snapshot_date: KST 기준 날짜 (date 객체).
      new_payload: §10 구조의 dict.

    Returns:
      ScrapeSnapshot 인스턴스 (신규/머지 결과).
    """
```

`_normalize_payload(payload)` 헬퍼는 5종 카테고리 키가 누락된 경우 빈 배열로
채워 머지 함수가 동일 형태를 가정할 수 있게 한다.

### §9.7 `apply_delta_to_main` 과 snapshot 머지의 결합

같은 `session_scope()` 안에서 호출되며, 호출자(`_async_main`) 가 commit 한다.
실패 시 SQLAlchemy 의 자동 rollback 으로 본 테이블 / snapshot / delta 모두
원상 복구된다.

```python
# _async_main 내부 의사 코드
with session_scope() as session:
    apply_result = apply_delta_to_main(
        session,
        scrape_run_id=scrape_run_id,
        snapshot_date=snapshot_date,  # KST today
    )
    new_payload = build_snapshot_payload_from_apply_result(apply_result)
    upsert_scrape_snapshot(
        session,
        snapshot_date=snapshot_date,
        new_payload=new_payload,
    )
# 이 시점에서 commit 완료. 이후 finalize_scrape_run 별도 트랜잭션.
```

---

## §10. snapshot.payload 구조 (사용자 원문 그대로)

### §10.1 전체 형태

```json
{
  "counts": {
    "new": 12,
    "content_changed": 8,
    "transitioned_to_접수예정": 1,
    "transitioned_to_접수중": 5,
    "transitioned_to_마감": 7
  },
  "new": [123, 456],
  "content_changed": [789, 234],
  "transitioned_to_접수예정": [{"id": 100, "from": "접수중"}],
  "transitioned_to_접수중":   [{"id": 789, "from": "접수예정"}],
  "transitioned_to_마감":     [{"id": 234, "from": "접수중"}]
}
```

- `new` / `content_changed`: `int[]` (announcement_id, asc 정렬).
- `transitioned_to_X`: `[{id: int, from: "접수예정"|"접수중"|"마감"}]`.
  - `id` 는 본 테이블 적용 후 시점의 announcement_id (§8.5).
  - `from` 은 적용 직전 본 테이블 status 값 (한글). status 가 한글이라
    payload 는 비-ASCII 를 포함한다 — JSON 저장에 문제 없음.
- `counts`: 5종 배열 길이를 1:1 로 반영. 머지 시 재계산 (§9.4).

> ID만 박고 title/agency/deadline_at 은 5b 의 dashboard view 가 announcements
> JOIN 으로 풀어서 보여준다 (사용자 원문). 본 task 는 ID 의 정확성과 머지
> 일관성에만 책임을 진다.

### §10.2 `snapshot_date` 결정 (KST)

- ScrapeRun 종료 시점의 **KST 날짜** 를 사용한다 (사용자 원문 "snapshot_date
  는 created_at 의 KST 날짜").
- 결정 헬퍼: `app.timezone.now_kst().date()` 호출 결과.
- KST 자정 경계가 ScrapeRun 종료 직전 / 직후 사이에 끼는 경우(아주 드문
  경우): 이번 ScrapeRun 이 끝난 시각의 KST 날짜를 그대로 채택한다 — 단일
  ScrapeRun 의 결과는 1 개 snapshot 에만 박힌다 (분할 적재 금지). 다음
  ScrapeRun 부터는 새 KST 날짜의 snapshot 에 들어간다.
- 본 테이블의 `Announcement.scraped_at` (UTC 저장) 와 `ScrapeSnapshot.created_at`
  (UTC 저장) 는 별개의 컬럼이며, snapshot.snapshot_date 만 KST date 로 둔다.

### §10.3 빈 ScrapeRun 의 처리

`apply_result` 의 5종 카테고리가 모두 빈 경우(=실질 변화 없음):

- 사용자 원문은 빈 snapshot 을 명시 금지하지 않는다.
- 권장안: **그래도 snapshot row 는 만든다**. 같은 날 후속 ScrapeRun 이 머지할
  대상이 되며, 5b 의 캘린더가 "이 날 수집 시도가 있었음" 을 표시할 수 있다.
- 빈 payload 는 5종 카테고리 빈 배열 + counts=0 으로 채운다 (`_normalize_payload`).

---

## §11. 첨부 고아 파일 GC

### §11.1 정책

- **고아 파일 정의**: `data/downloads/` 아래 실제로 존재하지만, 본 테이블
  `attachments.stored_path` (또는 정규화된 동일 표현) 으로 참조되지 않는
  파일.
- 고아가 발생하는 경로:
  1. apply 트랜잭션이 실패해 delta 가 폐기된 경우 — 다운로드된 파일은 남고
     DB 에는 어떤 row 도 박히지 않는다.
  2. (d) new_version 시 기존 row 가 봉인되며 첨부도 그대로 남지만, 후속
     수집에서 본 테이블 row 가 결국 정리되는 시나리오 (현재 구현은 봉인된
     row 의 첨부를 수동 삭제하지 않는다 — 이력 보존). 본 task 는 이 케이스를
     **고아로 다루지 않는다**: 봉인된 announcement 가 attachments.stored_path
     로 여전히 가리키고 있으면 GC 대상 아님. (사용자 원문은 "DB 에 없는 파일"
     을 고아로 한정.)
  3. 운영자가 수동으로 본 테이블에서 attachment row 를 지운 경우 — DB 참조가
     사라지므로 GC 대상.
- delta_attachments.stored_path 는 GC 가 본 테이블 참조와 비교하지 **않는다**:
  delta 는 매 ScrapeRun 종료 시 비워지므로, GC 시점에 살아있는 delta row 가
  있어도 곧 사라질 임시 상태일 뿐이다. 단, GC 가 ScrapeRun 진행 중에 돌면
  방금 다운로드된 파일을 잘못 지울 수 있다 — §11.3 의 동시성 가드 참조.

### §11.2 스크립트 시그니처 (`scripts/gc_orphan_attachments.py`)

```python
"""고아 첨부 파일 GC 스크립트.

사용법:
    python -m scripts.gc_orphan_attachments [--dry-run]
                                            [--root data/downloads]

동작:
    1. download_dir 의 모든 파일 경로 수집 (재귀).
    2. SELECT attachments.stored_path FROM attachments — 본 테이블 참조 set.
    3. 디스크 set − DB set = 고아 set.
    4. --dry-run 이면 path 와 size 만 stdout 출력. 실제 삭제 안 함.
       --dry-run 미지정 시 unlink + 빈 디렉터리 cleanup.

종료 코드:
    0: 정상 (고아 0건이어도 정상)
    1: 디렉터리 접근 실패 등 환경 오류
    2: 진행 중 ScrapeRun 이 있어 GC 가 거부된 경우 (--force 없이)
"""
```

### §11.3 동시성 가드

GC 가 수집 중에 돌면 다음 위험이 있다:

- 다운로드 직후, delta_attachments INSERT 직전 — 파일은 디스크에 있고 DB 에는
  아직 없다. GC 가 읽으면 "DB 에 없는 파일" 이라고 잘못 판정할 수 있다.

권장 가드:

1. GC 시작 시 `get_running_scrape_run(session)` 호출 — running row 가 있으면
   `exit(2)` (`--force` 옵션이 있을 때만 진행).
2. 운영 시 APScheduler job 으로 등록할 때(§11.4) 도 lock 충돌 시 skip 한다.

> SIGTERM 등으로 ScrapeRun 이 비정상 종료되면 lock 은 다음 웹 startup 의 stale
> cleanup 이 풀어준다 (Phase 2 §7.4). 그동안 GC 는 거부된다 — 사용자가 수동
> `--force` 로만 진행할 수 있다.

### §11.4 APScheduler 일 1회 자동 실행 (선택)

- `app/scheduler/job_runner.py` 에 `gc_orphan_attachments_job()` 추가 (선택
  구현 — 00041-5 가 결정).
- cron `0 4 * * *` (KST 04:00 = UTC 19:00) 권장. 새벽 시간대로 수집 충돌 가능
  성을 낮춘다.
- 실행 결과는 ScrapeRun 과 별개이므로 audit_logs 또는 별도 로그 파일에 기록.

### §11.5 검증 (사용자 원문 8번)

- `--dry-run` 으로 후보 출력 → 운영자가 수동 검수 → 실제 실행.
- 본 테이블 참조가 있는 파일은 절대 삭제되지 않아야 함 (단위 테스트 +
  통합 테스트).

---

## §12. 트랜잭션 시간 추정 + SQLite write lock 영향

### §12.1 트랜잭션 길이 추정

apply 트랜잭션은 다음 작업을 직렬 수행한다 (1 회 ScrapeRun = N 공고 가정):

| 단계 | 비용 추정 (N=200, 평균 첨부 5건) | 누적 |
|---|---|---|
| delta 전수 SELECT (ix_*_scrape_run_id) | <100ms | <100ms |
| 본 테이블 4-branch 매칭 + 적용 (N 회 SELECT + UPSERT) | ~50ms × 200 = ~10s | ~10s |
| 첨부 적용 (N×5 회 sha256 비교 + UPSERT) | ~5ms × 1000 = ~5s | ~15s |
| (d) reset 호출 (변경 공고만 — 보통 N의 5~10%) | ~30ms × 20 = ~0.6s | ~15.6s |
| 5종 카테고리 수집 + merge_snapshot_payload | <50ms (메모리 연산) | ~15.7s |
| ScrapeSnapshot UPSERT | <50ms | ~15.8s |
| delta 전수 DELETE | <100ms | ~15.9s |

> 위 수치는 실측이 아니라 **상한 가이드**. 실제는 SQLite WAL 모드 + 디스크
> 캐시에 따라 1~5초 이내가 될 가능성이 높다. 200 공고 / 1000 첨부는 일 단위
> 운영의 거의 최대값이다.

사용자 원문 검증 10번 ("~수 초 안에 완료") 에 부합한다.

### §12.2 SQLite write lock 영향

- SQLite 는 single writer. apply 트랜잭션이 진행 중이면 사용자 웹의 쓰기
  요청(읽음 토글, 관련성 판정 등) 은 `database is locked` 또는 busy timeout
  대기에 빠진다.
- 기본 `PRAGMA busy_timeout=3000` (Phase 2 §15.4 검토 항목) 이 설정되어 있으면
  3초까지 자연 대기 후 정상 진행된다. 위 §12.1 의 누적 16초가 그대로 lock 을
  점유하면 사용자 측에 lock timeout 이 발생할 수 있다.
- **완화책 1**: apply 트랜잭션을 너무 길게 만들지 않도록, 공고 1건당 비용을
  최소화한다. 본 task 의 권장은 SELECT/INSERT 의 chunked 처리가 아니라 **하나의
  트랜잭션 유지** + 공고 처리에 N+1 query 를 피하는 것 (selectinload + bulk
  upsert).
- **완화책 2**: 사용자 읽기 요청은 SQLite 의 reader/writer 분리(WAL)로 영향
  받지 않는다 — 읽기는 lock 동안에도 가능. 사용자 웹의 핵심 경로(목록 조회,
  상세 보기) 는 영향 없음.
- **완화책 3 (장기)**: Postgres 전환 시 advisory lock 으로 대체. 현 task 는
  SQLite 전제.

### §12.3 트랜잭션 실패 시뮬레이션 (검증 11)

apply 또는 snapshot 단계에서 의도적으로 예외를 raise 했을 때:

| 상태 | 기대 |
|---|---|
| `announcements` 본 테이블 | 트랜잭션 시작 직전 상태 그대로 |
| `attachments` 본 테이블 | 그대로 |
| `scrape_snapshots` row | 신규 INSERT 였으면 미생성, 머지 UPDATE 였으면 변경 미반영 |
| `delta_announcements / delta_attachments` | 그대로 (DELETE 도 롤백) |
| `scrape_runs.status` | finalize 단계가 별도 트랜잭션이므로 별개 처리 — apply 실패 시 `failed` + error_message 박아 마감 |
| `data/downloads/` 의 파일 | 그대로 (FS 는 트랜잭션 보호 밖) — 다음 GC 에서 정리 가능 |
| 다음 ScrapeRun | 같은 delta 가 살아 있으므로 그대로 재시도 가능 |

> **단, 같은 delta 를 그대로 재시도하면** 본 테이블의 4-branch 결과가 한 번 더
> 결정된다. (a) created → 그대로 / (d) new_version → 같은 변경이 다시 감지되어
> 동일 결과 / (c) status_transitioned → status 가 같으면 (b) unchanged 로
> 빠진다. 즉 재시도가 row 중복을 만들지 않는다.
>
> **재시도가 다음 ScrapeRun 에서 이루어질 때 delta_announcements 의 scrape_run_id
> 는 어떻게 되는가?** apply 가 실패해 rollback 되면 이전 scrape_run_id 의 delta
> 는 살아있다. 다음 ScrapeRun 시작 시 새 scrape_run_id 가 발급된다 — 이전
> scrape_run_id 의 delta 는 그대로 남는다. apply 단계는 `WHERE scrape_run_id =
> :current_run_id` 로 좁혀 동작하므로 이전 run 의 delta 는 자동 합류되지
> 않는다.
>
> **권장**: apply 단계 진입 시점에 "현재 run 외 다른 run 의 살아있는 delta"
> 가 있으면 경고 로그 + 자동 수습한다 — 별도 트랜잭션에서 이전 run 의 delta
> 를 본 테이블에 적용한 뒤 비우거나, 명시적 cleanup 스크립트를 운영자가
> 돌리도록. 본 task 는 **권장안**으로만 두고 실 구현은 00041-3 의 판단에 맡긴다
> (사용자 원문이 자동 수습을 요구하지 않으며, 단순화 우선이 안전).

### §12.4 KeyboardInterrupt / SIGTERM 시점별 효과

| 시점 | delta | 본 테이블 | snapshot | 회복 |
|---|---|---|---|---|
| 수집 중 SIGTERM (검증 2) | 일부 row 만 적재 후 중단. apply 에 도달 못함 | 변경 없음 | 변경 없음 | 다음 수집에서 새 ScrapeRun. 이전 run 의 delta 는 §12.3 권장안 적용 시 자동 정리 |
| apply 트랜잭션 한복판 SIGTERM | rollback (SIGTERM 이 commit 직전이면 commit 완료 가능 — 이 경우 정상 종료로 본다) | rollback | rollback | 같은 delta 재시도 가능 |
| apply commit 직후 finalize 직전 SIGTERM | 이미 commit | 적용됨 | 적용됨 | finalize 만 누락. 다음 startup stale cleanup 이 ScrapeRun 을 `failed (stale)` 로 마감 |

검증 2 ("SIGTERM 중단 시 delta 비워짐 + 본 테이블 변경 없음 + snapshot 변경
없음"):

- "delta 비워짐" 의 정확한 의미는 **현재 run 의 delta 가 apply 단계까지
  도달하지 못한 채 ScrapeRun 이 cancelled** 라는 것이다. 이 상태에서는 delta
  가 그대로 남는다 — 사용자 원문이 검증 2 에 "delta 비워짐" 을 명시했으나
  Phase 1a/2 의 "공고 단위 atomic + 현재 공고 마무리 후 종료" 시맨틱과 함께
  읽으면, **다음 ScrapeRun 시작 전에 이전 run 의 살아있는 delta 를 정리**하는
  것이 사용자 원문의 의도로 보인다.
- 권장안: ScrapeRun 의 상태가 `cancelled / failed` 로 마감될 때, 같은
  `scrape_run_id` 의 delta_announcements / delta_attachments 를 **별도
  트랜잭션으로 비우는** 후처리(`finalize_scrape_run` 의 후속 또는 startup
  cleanup) 를 추가한다. 이로써 검증 2 의 "delta 비워짐" 이 보장된다.
- 본 권장은 §12.3 의 재시도 친화성과 충돌한다 (cancel 의 delta 를 비우면
  재시도 불가). 따라서 **명확한 분기 (00041-3 에서 확정)**:
  - **`cancelled` (SIGTERM)**: apply 단계 자체를 건너뛰고, 별도 트랜잭션으로
    delta 만 비운다. 본 테이블 / snapshot 변화 없음. 검증 2 만족.
  - **`completed` / `partial`**: 단일 트랜잭션 안에서 apply (4-branch + 2차
    감지 + reset) → snapshot UPSERT (00041-4) → delta DELETE 를 모두 수행.
    트랜잭션 commit 시 모두 영구화된다. 검증 1·3·4·5·6·7·9 만족.
  - **`failed` (apply 트랜잭션 자체 실패 — verification 11)**: SQLAlchemy
    auto-rollback 으로 본 테이블 / snapshot / delta 모두 원상복구. delta 가
    보존되므로 다음 ScrapeRun 또는 운영자 수동 재시도가 가능. **이 분기에
    서는 추가 clear 를 호출하지 않는다.**
  - **`failed` (orchestrator/수집-단계 예외 — apply 도달 전)**: 별도 트랜잭션
    으로 delta 만 비운다 (수집 자체가 깨졌으므로 delta 가 incomplete →
    apply 안전성 없음 → clear 가 깨끗). 운영자는 다음 ScrapeRun 으로 재
    수집한다.
- 즉 "delta 보존" 은 verification 11 의 좁은 시나리오 (apply 트랜잭션 안에서
  의도적/실수로 raise) 에만 해당하고, cancelled / orchestrator-failed 에서는
  delta 가 비워진다. 이 두 분기는 finalize 단계에서 별도 트랜잭션으로
  처리되므로 SQLAlchemy auto-rollback 의 영향권 밖이다.

---

## §13. 검증 시나리오 매트릭스 (사용자 원문 11항)

| # | 시나리오 | 확인 지점 |
|---|---|---|
| 1 | 정상 종료 시 delta 비워짐 + 본 테이블 반영 + snapshot 생성/UPSERT | §7.4, §9, §12.3 |
| 2 | SIGTERM 중단 시 delta 비워짐 + 본 테이블 변경 없음 + snapshot 변경 없음 | §12.4 (cancelled 분기) |
| 3 | 같은 날 수동 + 자동 수집 → snapshot 1 row 머지 | §9.6, §10.2, E2 |
| 4 | 접수예정→접수중→마감 → transitioned_to_마감 만 (from=접수예정) | §9.4 (E4) |
| 5 | 접수중→마감→접수중(정정) → snapshot 변화 없음 (from=to 제거) | §9.4 (E5) |
| 6 | 신규 + 전이 동시 → 신규 + transitioned 둘 다 | §9.4 (E6) |
| 7 | 첨부만 변경 → content_changed 만 (status 변화 없음) | §8.3, §9.1 |
| 8 | 고아 파일 GC 정상 동작 (--dry-run + 실제) | §11.2, §11.5 |
| 9 | Phase 1a 사용자 라벨링 reset 회귀 (delta apply 단계로 이동했어도 정상 발동) | §8.2, §1.3 |
| 10 | SQLite single-writer 트랜잭션 vs 사용자 웹 요청 충돌 (~수 초 안에 완료) | §12.1, §12.2 |
| 11 | 트랜잭션 실패 시뮬레이션 → delta 그대로, 본 테이블 변화 없음, 다음 수집에서 재시도 | §12.3 (failed 분기) |

00041-6 가 위 11 항을 실제 회귀 실행한 결과를 `docs/00041-verification.md`
로 기록한다 (subtask plan 의 strategy_note 그대로).

---

## §14. 위험 요소 + 권장 대응

### §14.1 트랜잭션 길이 vs 사용자 충돌

- §12.1 의 추정으로 16초 상한. 보통 1~5초 예상.
- 운영 가이드: 사용자 웹이 동시에 쓰는 경우 read 는 영향 없음. 쓰기 (읽음
  토글, 관련성 판정) 는 `PRAGMA busy_timeout=3000` 으로 자연 대기.
- 만성적 lock timeout 이 발생하면 Postgres 전환을 검토.

### §14.2 첨부 파일 고아 누적

- apply 실패 시 다운로드된 파일이 디스크에 남는다. GC 가 정리.
- 일 1회 GC (선택) 가 없으면 디스크 사용량이 점진 증가할 수 있다.

### §14.3 (d) new_version 시 봉인된 announcement 의 첨부

- Phase 1a 의 시맨틱 (이력 누적): 봉인된 announcement_id 는 그대로 살아있고,
  attachments 도 그 row 에 남는다.
- §11.1 정의에 따라 GC 는 이 첨부를 고아로 보지 않는다 (봉인 row 가 본
  테이블에서 여전히 stored_path 를 보유).
- 5b 또는 미래 phase 가 "봉인된 announcement 의 첨부 보존 정책" 을 추가하면
  본 §11.1 정의를 갱신해야 한다.

### §14.4 같은 KST 날짜 안에서의 머지 누적

- 머지는 commutative + associative 가 아니다 — transition 의 from 은 첫 머지
  순서에 의존한다 (첫 from 유지 + 마지막 to 갱신). 따라서 ScrapeRun 종료
  순서가 머지 결과에 영향을 미친다.
- 이는 사용자 원문이 명시한 룰 (검증 4) 과 일치한다 — 시계열 순서가 의미를
  가지므로 의도된 동작이다.
- 단, ScrapeRun 종료 순서가 시계열과 어긋날 수 있는 시나리오 (예: 동시
  실행은 lock 으로 차단되지만, 같은 KST 날짜 자정 직전 / 직후) 에서는
  운영자가 이상하게 느낄 수 있다. README.USER.md 가 이 동작을 명시한다
  (00041-6).

### §14.5 트랜잭션 실패 시 delta 처리 일관성

- §12.3 / §12.4 의 분기 (cancelled → 비움 / failed → 보존) 는 본 문서의
  권장안. 00041-3 가 최종 결정한다.
- 결정 후 README.USER.md 와 본 문서를 **동기화**한다.

### §14.6 5종 카테고리 정의 변경 위험

- 현재 정의는 사용자 원문에 고정되어 있다. 5b 가 카테고리를 추가/삭제하면
  머지 함수와 payload 스키마를 동시에 갱신해야 한다.
- 본 task 의 `merge_snapshot_payload` 는 **알 수 없는 카테고리 키를 만나면
  ValueError** 를 raise 하도록 구현하여, 호환성 깨짐을 빠르게 노출시킨다.

---

## §15. 부록 — 후속 subtask 가 본 문서를 참조하는 방식

- **00041-2 (Alembic migration + ORM)**: §4 (테이블)·§5 (constraint)·§6
  (인덱스) 를 그대로 migration 파일에 옮긴다. ORM 모델 3종(`DeltaAnnouncement`,
  `DeltaAttachment`, `ScrapeSnapshot`) 은 §4 의 컬럼을 그대로 선언하고 Phase
  1a 의 다른 ORM 처럼 `relationship`/`back_populates` 만 최소 추가한다.

- **00041-3 (delta apply 로직)**: §7 의 처리 흐름과 §8 의 시그니처를 그대로
  구현. 1차/2차 감지 통합 권장안(§8.4) 을 따른다 (분리 유지). §12.3 / §12.4
  의 cancelled / failed 분기를 결정해 코드와 README.USER.md 에 반영.

- **00041-4 (snapshot 생성/머지)**: §9 의 머지 알고리즘을 `app/db/snapshot.py`
  에 단독 함수로 구현. §9.5 의 E1~E10 을 유닛 테스트 케이스로 매핑. §9.6
  upsert_scrape_snapshot 을 같은 트랜잭션에 통합.

- **00041-5 (GC 스크립트)**: §11 그대로. §11.3 의 동시성 가드 필수.

- **00041-6 (README.USER.md + 검증)**: §13 의 11 항을 그대로 회귀 실행.
  결과를 `docs/00041-verification.md` 에 기록. README.USER.md 의 "수집
  파이프라인 동작" 섹션을 delta + snapshot 흐름으로 다시 쓴다. PROJECT_NOTES
  갱신은 finalize 의 MemoryUpdater 책임.

본 문서의 §1 ~ §15 는 task 00041 가 살아 있는 동안 변경하지 않는다. 새 절은
§16 이후로 append.
