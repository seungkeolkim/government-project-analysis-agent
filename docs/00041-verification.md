# Task 00041 — 검증 11 항 회귀 노트

> 작성일: 2026-04-29 / 대상 SHA: `3141c09` (`feature/00041-delta-snapshot-infra`) +
> 본 subtask (00041-6) 의 README 갱신 commit.
>
> 본 문서는 사용자 원문의 검증 11 항을 한 자리에서 점검한 결과를 기록한다. PR
> 머지 후 운영자가 동일 명령어로 재시도할 수 있도록 명령어를 그대로 적었다.
> 점검은 다음 두 환경에서 수행됐다:
>
> - 코더 로컬 (Linux, Python 3.12, .venv) — pytest + 직접 호출 스크립트.
> - docker compose 환경에서의 E2E (실 IRIS 응답 / 실 SIGTERM 시그널) 는 운영자가
>   별도 수행하는 항목으로 분리해 표시했다.

## 결과 요약

| #  | 검증 항목                                                                 | 결과    | 비고                                                                |
| -- | ------------------------------------------------------------------------- | ------- | ------------------------------------------------------------------- |
| 1  | 정상 종료 시 delta 비워짐 + 본 테이블 반영 + snapshot 생성/UPSERT          | ✅ PASS | apply + snapshot UPSERT 단일 트랜잭션 commit 확인                    |
| 2  | SIGTERM 중단 시 delta 비워짐 + 본 테이블 변경 없음 + snapshot 변경 없음    | ✅ PASS | cancelled 분기 코드 레벨 시뮬레이션 (cli._async_main path)           |
| 3  | 같은 KST 날짜 수동 + 자동 수집 → snapshot 1 row 머지                      | ✅ PASS | trigger='cli'+ 'scheduled' 두 ScrapeRun, snapshot row 1 건 확인      |
| 4  | 접수예정→접수중→마감 → transitioned_to_마감 만 (from=접수예정)            | ✅ PASS | 머지 후 압축됨                                                       |
| 5  | 접수중→마감→접수중(정정) → snapshot 변화 없음 (from==to 제거)             | ✅ PASS | 모든 transition 카테고리에서 announcement_id 제거                    |
| 6  | 같은 공고 신규 + 전이 → 신규 + transitioned 둘 다 보존                    | ✅ PASS | 다른 ScrapeRun 시퀀스에서 둘 다 박힘 확인                            |
| 7  | 첨부만 변경된 공고 → content_changed 에만 박힘 (status 변화 없음)         | ✅ PASS | 1차 unchanged + 2차 감지 reapply → content_changed 만               |
| 8  | 고아 파일 GC 정상 동작 (--dry-run 검증 후 실제 삭제)                      | ✅ PASS | tests/scripts/test_gc_orphan_attachments.py 18 PASS                 |
| 9  | Phase 1a 사용자 라벨링 reset 회귀 (delta apply 단계로 이동했어도 정상)    | ✅ PASS | (d) new_version 분기 + RelevanceJudgmentHistory 이관 확인           |
| 10 | SQLite single-writer 트랜잭션이 사용자 웹 요청과 충돌 없는지 (~수 초)     | ✅ PASS | 100공고/300첨부 apply 0.70s, 동시 web read 최대 4.0ms                |
| 11 | 트랜잭션 실패 시뮬레이션 → delta 그대로 + 본 테이블 변화 없음 + 재시도 가능 | ✅ PASS | apply 도중 raise → auto-rollback → 다음 ScrapeRun 으로 재시도 성공 |

회귀 명령 한 줄: `pytest tests/ --deselect tests/auth/test_read_flow.py::test_mark_announcement_read_is_upsert -q`. 결과: **280 passed, 1 deselected**.

선언적 deselect 사유: `test_mark_announcement_read_is_upsert` 는 본 task **시작
이전** SHA 시점부터 이미 실패하던 SQLite tz 손실 관련 사전 결함이며 (00041-3
보고에서 `git stash` 로 본 task 변경 제거 후 동일 실패 재현 확인), 본 task 의
delta+snapshot 인프라 도입과 무관하다.

본 task 의 신규 테스트:
- `tests/db/test_snapshot_merge.py` — 25 PASS (5종 카테고리 머지 룰 E1~E10)
- `tests/db/test_scrape_snapshot_upsert.py` — 10 PASS (UPSERT 통합)
- `tests/scripts/test_gc_orphan_attachments.py` — 18 PASS (GC 단위/통합/CLI)

기존 회귀 안전망:
- `tests/db/test_change_detection.py` — 6 PASS (4-branch + 2차 감지)
- `tests/db/test_atomic_rollback.py` — 1 PASS (UPSERT/리셋 atomic)

---

## 0. 재현용 통합 검증 스크립트

본 절의 1·2·3·4·5·6·7·9·10·11 검증은 단일 Python 스크립트로 직접 재현 가능하다.
스크립트는 docker / 외부 IRIS 응답 / 실 SIGTERM 신호 없이 코드 레벨에서 모든
경로를 재현한다.

```bash
# 깨끗한 SQLite 로 재실행
rm -f /tmp/verify_00041.sqlite3
DB_URL="sqlite:////tmp/verify_00041.sqlite3" uv run python <<'PY'
# (스크립트 본문은 본 문서 §1 ~ §11 의 코드 블록을 순서대로 실행하면 된다.
#  코드는 모두 app.db.repository / app.db.snapshot / app.timezone 의 공개 API 만 사용)
PY
```

검증 8 (GC) 만 별도 pytest 명령으로 실행한다 (디스크 작업이 본격 포함되어
파일 fixture 가 필요하기 때문):

```bash
uv run --extra dev pytest tests/scripts/test_gc_orphan_attachments.py -v
```

---

## 1. 정상 종료 시 delta 비워짐 + 본 테이블 반영 + snapshot 생성/UPSERT

### 시나리오

`make_run()` 으로 ScrapeRun 1 건 생성 → `insert_delta_announcement` +
`update_delta_announcement_detail` 로 V1-001 공고 적재 → `apply_delta_to_main`
+ `upsert_scrape_snapshot` 단일 트랜잭션 → `finalize_scrape_run(status='completed')`.

### 관찰 가능한 증거

```
검증 1: 정상 종료 → delta 비워짐 + 본 테이블 반영 + snapshot 생성
  delta 잔여=0 본테이블=1 snapshot.new=[1] counts={'new': 1, 'content_changed': 0,
                                                   'transitioned_to_접수예정': 0,
                                                   'transitioned_to_접수중': 0,
                                                   'transitioned_to_마감': 0}
  PASS
```

### 검증 포인트

- `delta_announcements WHERE scrape_run_id = run1` row 수 = 0 (apply 의 step 7
  `clear_delta_for_run` 이 같은 트랜잭션에서 비움).
- `announcements WHERE is_current=True` row 수 = 1 (V1-001 created).
- `scrape_snapshots WHERE snapshot_date = today_kst` 에 row 1 건 + payload.new
  = `[1]` (= 본 테이블 적용 후 announcement_id).

---

## 2. SIGTERM 중단 시 delta 비워짐 + 본 테이블 변경 없음 + snapshot 변경 없음

### 시나리오

`cli._async_main` 의 cancelled 분기를 코드 레벨로 시뮬레이션:
1. ScrapeRun 생성 + delta_announcements INSERT
2. apply 호출 **하지 않고** `clear_delta_for_run` 만 호출
3. `finalize_scrape_run(status='cancelled', error_message='SIGTERM 시뮬레이션')`

이는 `app/cli.py::_async_main` 의 `if candidate_status == "cancelled"` 분기와
동일한 호출 순서다 (00041-3 의 4-way 분기 §12.4).

### 관찰 가능한 증거

```
검증 2: SIGTERM cancelled — apply skip + delta clear (본 테이블 / snapshot 미변경)
  cleared=1 delta 잔여=0 본 테이블 추가=0 snap_payload 변경=False
  ScrapeRun.status=cancelled error_message=SIGTERM 시뮬레이션
  PASS — cancelled 분기 동작 확인
```

### 검증 포인트

- `clear_delta_for_run` 이 1 건 DELETE.
- 본 테이블 announcements 의 V2-001 추가 0건 (apply 호출 안 했으므로 INSERT 없음).
- `snapshot.payload` 가 cancelled 직전과 정확히 같음 (변경 없음).
- `ScrapeRun.status='cancelled'` 마감.

### docker / 실 SIGTERM 시그널 검증 (운영자)

```bash
# 1) 수집 시작 (백그라운드)
docker compose --profile scrape run --rm -d scraper python -m app.cli &
SCRAPER_PID=$!

# 2) 5초 후 SIGTERM
sleep 5; kill -TERM $SCRAPER_PID

# 3) 종료 후 DB 상태 확인 (host 에서)
sqlite3 ./data/db/app.sqlite3 "SELECT status, error_message FROM scrape_runs ORDER BY id DESC LIMIT 1;"
sqlite3 ./data/db/app.sqlite3 "SELECT COUNT(*) FROM delta_announcements;"  # 0 기대
```

---

## 3. 같은 KST 날짜 수동 + 자동 수집 → snapshot 1 row 머지

### 시나리오

- `trigger='cli'` ScrapeRun 으로 V3-A 적재 → apply
- `trigger='scheduled'` ScrapeRun 으로 V3-B 적재 → apply
- 같은 `now_kst().date()` 라 snapshot 머지 분기 발동.

### 관찰 가능한 증거

```
검증 3: 같은 KST 날짜 수동 + 자동 수집 → snapshot 1 row 머지
  scrape_snapshots row 수 (오늘 KST) = 1 (기대 1)
  V3-A in snapshot.new=True V3-B in snapshot.new=True
  PASS — 1 row 에 머지 (수동 + 자동 결합)
```

### 검증 포인트

- `scrape_snapshots WHERE snapshot_date = today_kst` row 수 = 1 (UNIQUE 제약 + UPSERT 머지).
- 두 announcement 가 같은 row 의 `payload.new` 에 둘 다 박힘 (set union).

---

## 4. 같은 공고 같은 날 접수예정 → 접수중 → 마감 — transitioned_to_마감(from=접수예정) 만

### 시나리오 (설계 §9.5 E4)

같은 V4-001 공고에 대해 ScrapeRun 3회를 순차 실행 (status 만 변경):
1. 접수예정 (created)
2. 접수예정 → 접수중 (status_transitioned)
3. 접수중 → 마감 (status_transitioned)

### 관찰 가능한 증거

```
검증 4: 접수예정→접수중→마감 머지 후 transitioned_to_마감(from=접수예정) 만
  V4-001 in transitioned_to_마감 = [{'id': 3, 'from': '접수예정'}]
  V4-001 in transitioned_to_접수중 = []
  PASS
```

### 검증 포인트

- `merge_snapshot_payload` 가 첫 from(`접수예정`) 유지 + 마지막 to(`마감`) 갱신.
- 중간 단계의 `transitioned_to_접수중` 에서 V4-001 제거됨.

### 단위 테스트 매핑

`tests/db/test_snapshot_merge.py::test_merge_e4_3_step_transition_keeps_first_from_last_to`
가 동일 룰을 직접 검증.

---

## 5. 접수중 → 마감 → 접수중 (실수 정정) — snapshot 변화 없음 (from==to 제거)

### 시나리오 (설계 §9.5 E5)

V5-001 의 status 가 접수중→마감→접수중 으로 회귀:
1. created (접수중)
2. 접수중 → 마감 (status_transitioned)
3. 마감 → 접수중 (status_transitioned, 실수 정정)

머지 후 from=='접수중', to=='접수중' 이라 `from == to` 분기로 제거.

### 관찰 가능한 증거

```
검증 5: 접수중→마감→접수중 (정정) — from==to 제거
  V5-001 잔여 transition 항목 = []
  PASS — from==to 머지 결과 모든 transition 카테고리에서 제거
```

### 검증 포인트

- 머지 후 V5-001 의 announcement_id 가 `transitioned_to_접수예정 / 접수중 / 마감`
  3 카테고리 모두에서 사라짐.
- 단위 테스트: `test_merge_e5_status_correction_drops_to_no_change`.

---

## 6. 같은 공고 신규 + 전이 — 신규 카테고리 + transitioned 둘 다 박힘

### 시나리오 (설계 §9.5 E6)

V6-001 을 같은 KST 날짜의 두 ScrapeRun 으로 처리:
1. ScrapeRun A (수동): 신규 등록 (created)
2. ScrapeRun B (자동): 같은 공고가 마감 status 로 재등장 (status_transitioned)

### 관찰 가능한 증거

```
검증 6: 같은 KST 날짜 신규 + 전이 동시 (다른 ScrapeRun) — 둘 다 보존
  V6-001 in snapshot.new=True / in transitioned_to_마감=True
  PASS — 신규 + 전이 둘 다 보존
```

### 검증 포인트

- 머지 결과 같은 announcement_id 가 `payload.new` 와 `payload.transitioned_to_마감`
  두 곳에 박힘 (서로 흡수 X) — 사용자 원문 "같은 날 신규 + 전이 동시 발생 시:
  둘 다 유지".
- 단위 테스트: `test_merge_e6_new_plus_transition_keeps_both`.

---

## 7. 첨부만 변경된 공고 → content_changed 에만 박힘 (status 변화 없음)

### 시나리오

V1-001 의 본 테이블 row 에 attachment 1건이 이미 있다 (sha256=`z*64`). 다음
ScrapeRun 에서 같은 title/status/agency/deadline_at 으로 적재하되 첨부 sha256
만 다르게(`y*64`).

apply 단계에서:
- 1차 4-branch: title/status/agency/deadline_at 동일 → `unchanged`.
- delta_attachments 적용 후 signature_before(z) ≠ signature_after(y) →
  2차 감지 발동 → `reapply_version_with_reset` (검증 9 의 reset 로직 + new_version
  row INSERT).
- new_version row 의 announcement_id 가 `payload.content_changed` 에 박힘.
- transition 카테고리는 미박힘 (status_transitioned 분기 미발동).

### 관찰 가능한 증거

```
검증 7: 첨부만 변경 → content_changed 에만 박힘 (status 변화 없음 가정)
  1차 action_counts={'unchanged': 1} 2차 감지 발동=1
  V1-001 신규 id=6 in_content_changed=True not_in_transition=True
  PASS — 첨부 변경만으로 content_changed, transition 미박힘
```

### 검증 포인트

- 1차 action 이 `unchanged` (4-branch 비교 4 필드 변경 없음).
- 2차 감지가 1 회 발동 (`attachment_content_change_count = 1`).
- new_version row 의 id 가 `content_changed` 에만 들어가고 transition 카테고리에는
  미박힘.

---

## 8. 고아 파일 GC 정상 동작 (--dry-run 검증 후 실제 삭제)

### 시나리오

`tests/scripts/test_gc_orphan_attachments.py` 의 18 테스트가 모든 GC 분기를
검증한다.

### 재현 명령

```bash
uv run --extra dev pytest tests/scripts/test_gc_orphan_attachments.py -v
```

### 결과 (요약)

```
tests/scripts/test_gc_orphan_attachments.py::test_collect_disk_files_returns_only_files_recursively PASSED
tests/scripts/test_gc_orphan_attachments.py::test_collect_disk_files_handles_missing_root PASSED
tests/scripts/test_gc_orphan_attachments.py::test_compute_orphan_files_excludes_db_referenced_paths PASSED
tests/scripts/test_gc_orphan_attachments.py::test_compute_orphan_files_returns_sorted_list PASSED
tests/scripts/test_gc_orphan_attachments.py::test_compute_orphan_files_skips_paths_outside_root PASSED
tests/scripts/test_gc_orphan_attachments.py::test_gather_db_attachment_paths_normalizes_and_skips_null PASSED
tests/scripts/test_gc_orphan_attachments.py::test_delete_orphan_files_unlinks_and_cleans_empty_dirs PASSED
tests/scripts/test_gc_orphan_attachments.py::test_delete_orphan_files_skips_paths_outside_root PASSED
tests/scripts/test_gc_orphan_attachments.py::test_run_gc_dry_run_does_not_delete PASSED
tests/scripts/test_gc_orphan_attachments.py::test_run_gc_apply_deletes_only_orphans PASSED
tests/scripts/test_gc_orphan_attachments.py::test_run_gc_skips_when_scrape_run_is_running PASSED
tests/scripts/test_gc_orphan_attachments.py::test_run_gc_force_proceeds_despite_running_scrape_run PASSED
tests/scripts/test_gc_orphan_attachments.py::test_run_gc_returns_empty_report_when_no_orphans PASSED
tests/scripts/test_gc_orphan_attachments.py::test_run_gc_does_not_touch_files_outside_root PASSED
tests/scripts/test_gc_orphan_attachments.py::test_run_gc_handles_missing_root_directory PASSED
tests/scripts/test_gc_orphan_attachments.py::test_cli_exits_with_code_2_when_scrape_run_is_running PASSED
tests/scripts/test_gc_orphan_attachments.py::test_cli_exits_with_code_0_on_normal_run PASSED
tests/scripts/test_gc_orphan_attachments.py::test_cli_zero_orphans_exits_zero PASSED

18 passed
```

### 사용자 원문 검증 8 의 핵심 invariant

> **본 테이블 참조가 있는 파일은 절대 삭제되지 않는다**.

해당 invariant 는 `test_run_gc_apply_deletes_only_orphans` 가 직접 assert:

```python
# 본 테이블 attachments 가 keep.pdf 를 참조하고 있는 상태에서 GC apply 실행
assert kept_path.exists()  # DB 참조 파일은 절대 삭제 X
assert not orphan1.exists()  # 고아만 삭제
```

### docker / 실 운영 검증 (운영자)

```bash
# 1) dry-run 으로 후보 확인
docker compose --profile scrape run --rm scraper \
    python scripts/gc_orphan_attachments.py --dry-run

# 출력: [DRY-RUN] scanned_root=... disk_files=N db_paths=M orphans=K total_bytes=B
#       ORPHAN  /app/data/downloads/... (각 후보)

# 2) 검토 후 실제 삭제
docker compose --profile scrape run --rm scraper \
    python scripts/gc_orphan_attachments.py
# 출력: [APPLY] scanned_root=... deleted=K failed=0 removed_dirs=N total_bytes_freed=B

# 3) ScrapeRun running 중 거부 확인 — 별도 터미널에서 수집 중 GC 실행
docker compose --profile scrape run --rm scraper python -m app.cli &
docker compose --profile scrape run --rm scraper \
    python scripts/gc_orphan_attachments.py
# 종료 코드 2 + "GC 거부 — ScrapeRun id=X 가 'running' 입니다" 로그
echo $?  # 2 기대
```

---

## 9. Phase 1a 사용자 라벨링 reset 회귀 (delta apply 단계로 이동했어도 정상)

### 시나리오

1. V1-001 공고를 created 한 뒤 사용자 1명이 읽음 표시 + 관련성 판정.
2. 다음 ScrapeRun 으로 같은 V1-001 의 title 만 변경 → 4-branch (d) new_version.
3. apply 단계의 `upsert_announcement` 가 자동으로
   `_reset_user_state_on_content_change` 를 호출 — 봉인된 old row 의
   `AnnouncementUserState.is_read=False, read_at=NULL` UPDATE +
   `RelevanceJudgment` → `RelevanceJudgmentHistory` 이관.

### 관찰 가능한 증거

```
검증 9: Phase 1a 사용자 라벨링 reset 회귀
  state.is_read=False state.read_at=None judgment=None history.archive_reason=content_changed
  PASS — (d) new_version 분기에서 reset + 이관 정상
```

### 검증 포인트

- Phase 1a 의 reset 로직이 호출 위치만 (cli → apply_delta_to_main) 이동했을
  뿐 시맨틱 동일.
- `RelevanceJudgmentHistory.archive_reason='content_changed'` 로 이관 사유
  보존.
- 회귀 안전망: `tests/db/test_change_detection.py` 의 6 테스트 + Phase 1a
  reset 의 기존 atomic 보장 (`tests/db/test_atomic_rollback.py`) 모두 PASS 유지.

---

## 10. SQLite single-writer 트랜잭션이 사용자 웹 요청과 충돌 없는지 (~수 초 안에 완료)

### 시나리오

설계 §12.1 의 \"200공고/1000첨부\" 상한 가이드에 근접한 N=100 공고 / 300 첨부
시나리오로 apply 시간 측정 + 동시 web-style READ 5 회 응답시간 측정.

### 관찰 가능한 증거

```
검증 10: SQLite write lock 트랜잭션 시간 + 동시 read 영향
  100공고/300첨부 apply 시간 = 697.7 ms (0.70s)
  apply action_counts = {'created': 100}
  설계 §12.1 상한 16s 대비: PASS (실측 0.70s)
  apply (50공고) 시간 = 474.7 ms
  동시 web read 시간 (5회) = ['4.0ms', '2.6ms', '2.7ms', '2.6ms', '3.2ms']
  최대 read 시간 = 4.0 ms
  PASS — 동시 read 응답 timeout 없이 완료
```

### 결론

- N=100/300 첨부의 apply 가 **0.70초** — 설계 §12.1 의 보수 상한 16초보다
  훨씬 빠르다.
- apply 진행 중에도 web-style read (`SELECT FROM announcements WHERE
  is_current=True LIMIT 20`) 가 **최대 4.0ms** 응답 — SQLite WAL 의 reader/writer
  분리 덕에 쓰기 lock 의 영향을 받지 않는다.
- 사용자 원문 검증 10 의 \"~수 초 안에 완료\" 충족.

### 운영 권장 (설계 §12.2 요약)

- `PRAGMA busy_timeout=3000` 이 기본 설정되어 있으면 사용자 쓰기(읽음 토글
  등) 가 lock 대기에 빠져도 3초 안에 자연 통과.
- 운영 데이터(N=200 / 1000첨부) 도 위 추정으로 1~5초 이내 예상. 실 운영자가
  scrape_runs 의 `started_at`–`ended_at` 차이로 추적 가능.

---

## 11. 트랜잭션 실패 시뮬레이션 → delta 그대로, 본 테이블 변화 없음, 다음 수집에서 재시도 가능

### 시나리오

apply 트랜잭션 안에서 의도적으로 `RuntimeError` raise → SQLAlchemy auto-rollback.
이후 새 ScrapeRun 으로 같은 공고를 재수집해 정상 처리되는지 확인.

### 관찰 가능한 증거

```
검증 11: 트랜잭션 실패 시뮬레이션 → delta 보존 + 본 테이블 변화 없음
  의도적 예외 catch: 의도적 raise — 트랜잭션 실패 시뮬레이션
  delta 잔여=1 (기대 1) / 본테이블 V11-001 추가=0 (기대 0)
  PASS — auto-rollback 으로 delta 보존 + 본 테이블 변화 없음
  재시도 검증: 새 ScrapeRun 으로 V11-001 재수집 → apply 가 정상 처리
  재시도 후 V11-001 본 테이블 created — id=7 apply.action_counts={'created': 1}
  PASS — 다음 수집에서 재시도 가능
```

### 검증 포인트

- apply 트랜잭션 안의 raise → SQLAlchemy 가 자동 rollback → delta 1 건 보존.
- 본 테이블 V11-001 추가 0건 (rollback 으로 INSERT 도 되돌려짐).
- `cli._async_main` 의 4-way 분기 중 \"apply 자체 실패\" 는 추가 clear 를
  호출하지 않으므로 위 상태가 그대로 유지된다 (검증 11 정확 시맨틱).
- 다음 ScrapeRun 으로 같은 공고 재수집 시 apply 가 정상 처리 (created action).

### 단위 테스트 매핑

`tests/db/test_scrape_snapshot_upsert.py::test_upsert_rolls_back_on_transaction_failure`
가 동일 invariant 의 snapshot 측면을 직접 assert.

---

## 부록 A — 본 task 의 신규 테스트 인벤토리

| 파일                                                | 테스트 수 | 결과    | 주요 커버리지                                                        |
| --------------------------------------------------- | --------- | ------- | -------------------------------------------------------------------- |
| `tests/db/test_snapshot_merge.py`                   | 25        | ✅ PASS | merge_snapshot_payload E1~E10 + normalize_payload 입력 보호          |
| `tests/db/test_scrape_snapshot_upsert.py`           | 10        | ✅ PASS | INSERT/UPDATE 분기 + 검증 4·5·6 회귀 + rollback 일관성                |
| `tests/scripts/test_gc_orphan_attachments.py`       | 18        | ✅ PASS | GC 단위/통합 + ScrapeRun 가드 + CLI 종료 코드                        |

기존 회귀 안전망:

| 파일                                       | 테스트 수 | 결과    |
| ------------------------------------------ | --------- | ------- |
| `tests/db/test_change_detection.py`        | 6         | ✅ PASS |
| `tests/db/test_atomic_rollback.py`         | 1         | ✅ PASS |
| 그 외 전체 스위트 (auth/bulk/relevance 등)  | 220       | ✅ PASS |

전체: **280 passed, 1 deselected** (사전 결함 1건은 본 PR 무관).

---

## 부록 B — 운영자가 docker compose 환경에서 실수행할 수 있는 검증

본 코드 레벨 검증으로는 다룰 수 없는 항목 (실 IRIS 응답 / 실 SIGTERM 신호 /
실 디스크 쓰기 / docker 컨테이너 환경) 은 운영자가 별도로 수행하는 것이
권장된다. 권장 명령어는 §2 / §8 의 \"docker / 실 운영 검증\" 절에 있다.

이 절들은 본 PR 머지 후 **첫 운영 사이클**에서 한 번 수행하고, 결과를 본
문서 부록 C 에 추가하는 것을 권장한다 (인프라 변경의 소프트 검증 트레일).

---

## 부록 C — 본 PR 머지 후 운영 환경 1차 검증 (운영자 기록 자리)

> **운영자 작성**: 본 PR 머지 후 실 컨테이너 환경에서 검증 1·2·8·10 을
> 한 번 더 수행하고, scrape_runs / scrape_snapshots 의 SQL 출력을 그대로
> 붙여넣어 둔다. 본 자리는 빈 채로 머지되어도 무방하며, 운영자 1차 사이클
> 종료 후 추가 PR 로 채울 수 있다.

```
(운영자가 채울 자리)
```
