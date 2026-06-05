# 시스템 관리자 운영 가이드

> 일상 운영·트러블슈팅 중심 문서. 프로젝트 개요·설치 흐름은 [README.md](README.md) 를,
> 아키텍처 결정 근거는 [PROJECT_NOTES.md](PROJECT_NOTES.md) 를 참고한다.
>
> **Docker 전용.** 호스트에서 `python -m app.cli` 직접 실행은 지원하지 않는다.
> 서버 구동은 `./run_compose.sh`, 관리자 도구는 `./run_admin.sh` 를 사용한다.

---

## 목차

1. [시작하기](#시작하기)
2. [스크래퍼 실행](#스크래퍼-실행)
3. [DB 관리](#db-관리)
4. [사용자 기능 안내](#사용자-기능-안내)
5. [관리자 기능](#관리자-기능)
6. [로그와 디버깅](#로그와-디버깅)
7. [트러블슈팅](#트러블슈팅)
8. [정기 운영 체크리스트](#정기-운영-체크리스트)

---

## 시작하기

### 초기 설치 (요약)

```bash
# 1) .env 생성 — 호스트마다 다른 값을 반드시 직접 채운다.
cp .env.example .env
{
  echo "HOST_UID=$(id -u)"
  echo "HOST_GID=$(id -g)"
  echo "HOST_DOCKER_GID=$(getent group docker | cut -d: -f3)"
  echo "HOST_PROJECT_DIR=$(pwd)"
} >> .env

# 2) sources.yaml 생성 (template 복사). 이미 있으면 덮어쓰지 않음.
sh ./bootstrap_sources.sh

# 3) 이미지 빌드 + 웹 UI 기동
./run_compose.sh build
./run_compose.sh up
# → http://localhost:8000
```

각 변수의 역할과 채우는 법은 다음 절들에서 설명한다. `.env.example` 위쪽 섹션
순서대로 등장하므로 파일을 위에서 아래로 읽으며 따라가면 된다.

### 필수 호스트 통합 변수

`.env.example` §1 "호스트 통합 변수 (필수)" 섹션에 모인 변수들이다. 자동 fallback
없이 빠지면 기동 실패·권한 오류로 이어진다 (`HOST_TZ` 만 예외 — 다음 절 참조).

| 변수 | 용도 | 빠지면 |
|------|------|--------|
| `HOST_UID` / `HOST_GID` | `./data/` 에 컨테이너가 만드는 파일 owner 를 호스트 유저로 맞춤 | root 로 실행되어 호스트에서 `sudo` 없이는 수정·삭제 불가 |
| `HOST_DOCKER_GID` | app 컨테이너 프로세스가 docker 그룹에 속해 `/var/run/docker.sock` 접근 | 웹 "지금 시작" 클릭 시 `Permission denied on docker.sock` |
| `HOST_PROJECT_DIR` | compose 파일의 상대경로를 호스트 기준으로 해석 | `ComposeEnvironmentError` flash |

UID/GID 변경 시 `/etc/passwd` 등록이 빌드 시점 ARG 로 처리되므로 **재빌드 필수**:

```bash
./run_compose.sh build
./run_compose.sh up
```

이미 root 소유로 생긴 파일이 있다면 일회성 복구:

```bash
sudo chown -R "$(id -u):$(id -g)" ./data/
```

> **보안 경고.** docker.sock 마운트는 사실상 호스트 root 권한 부여와 동등하다.
> FastAPI 는 절대 외부 네트워크에 노출하지 말 것 — 로컬 또는 내부 VPN 안에서만 접근.

### 서버 타임존 (KST 단일 운영)

화면·로그·cron 모든 사용자 표시 시각이 `Asia/Seoul`. DB 저장은 UTC tz-aware, 경계에서만
KST 변환. `app/timezone.py` 가 명시적 `ZoneInfo("Asia/Seoul")` 을 사용하므로 호스트 /
컨테이너 TZ env 가 빠져 있어도 표시 시각은 정상이다 (`docker-compose.yml` 이
`TZ=${HOST_TZ:-Asia/Seoul}` + `/etc/localtime` 바인드 마운트를 자동 주입).

호스트 시각 정렬 권장 (Linux: `sudo timedatectl set-timezone Asia/Seoul`). 다른 지역 tz
로 컨테이너 OS 시각만 바꾸려면 `HOST_TZ=UTC` 등을 `.env` 에 명시 — 코드 레벨 KST 는
그대로다.

### 첫 관리자 계정 생성

자유 회원가입은 항상 `is_admin=False`. 관리자(`is_admin=True`) 계정은 컨테이너 안에서
CLI 로 한 번 만든다. 관리자 페이지 진입에 필수.

```bash
# 대화형 (username/password/email 모두 prompt)
./run_admin.sh create-admin

# username + email 인자, password 만 prompt
./run_admin.sh create-admin root_user --email admin@example.com
```

정책: username 영문 소문자/숫자/밑줄 3~64자, password 8자 이상. bcrypt(rounds=12) 해시
저장. 같은 username 이 이미 있으면 종료 코드 1.

확인:

```bash
sqlite3 ./data/db/app.sqlite3 "SELECT id, username, is_admin FROM users WHERE is_admin = 1;"
```

### 실행 명령 (run_compose.sh)

| 서브커맨드 | 동작 |
|-----------|------|
| `up` | 앱 서버 기동 (포어그라운드, 코드 변경 자동 반영) |
| `up -d` | 앱 서버 백그라운드 기동 |
| `down` | 서비스 중지 및 컨테이너 제거 |
| `build` | 이미지 빌드 |
| `logs -f app` | 앱 로그 실시간 확인 |
| `scrape` | 스크래퍼 1회 실행 후 종료 |

직접 `docker compose` 명령은 사용하지 않는다 — wrapper 스크립트를 통해서만 실행한다.

### 운영 스크립트 위치

- 사용자 실행 shell: 프로젝트 루트의 `./run_compose.sh`, `./run_admin.sh`, `./bootstrap_sources.sh`.
- Python 운영 스크립트: `scripts/python/<name>.py` — 모두 `docker compose run` 으로 호출.
- 이미지 안에서 자동 실행되는 shell: `docker/entrypoint.sh`.

---

## 스크래퍼 실행

### 실행 경로 3종 (모두 같은 lock 공유)

| 경로       | 트리거 | 사용 시점                                            |
| ---------- | ------ | ---------------------------------------------------- |
| **CLI**    | 호스트 쉘에서 직접  | 디버깅, 호스트 cron / systemd timer 통합 |
| **웹 UI**  | `/admin/scrape` 의 "지금 시작" 버튼 | 평소 운영의 1순위 |
| **스케줄** | APScheduler cron / 매 N시간 | 자동화                                  |

세 경로 모두 `scrape_runs.status='running'` row 1개로 lock 한다. 이미 실행 중이면:

- CLI → 종료 코드 `2` + stderr 메시지.
- 웹 → flash 배지 `이미 다른 수집이 진행 중입니다.`.
- 스케줄 → WARN 로그 + 다음 주기로 skip.

### CLI 실행

```bash
docker compose --profile scrape run --rm scraper
```

모든 실행 파라미터는 `sources.yaml` 의 `scrape:` 섹션으로 제어한다 (CLI 인자 없음).
우선순위: **scrape: 전역 설정 > 소스별 설정 > 코드 default (10페이지 / 200건)**.

| 필드 | 기본값 | 설명 |
|------|-------|------|
| `scrape.active_sources` | `[]` | 실행할 소스 ID. 비어 있으면 `enabled: true` 소스 전체 |
| `scrape.max_pages` | `null` | 소스당 최대 페이지 수 |
| `scrape.max_announcements` | `null` | 소스당 최대 공고 수 |
| `scrape.skip_detail` | `false` | true 면 목록만 적재 |
| `scrape.skip_attachments` | `false` | true 면 첨부 다운로드 생략 |
| `scrape.dry_run` | `false` | true 면 DB 쓰기 없이 동작 검증 |
| `scrape.log_level` | `null` | 비면 `.env` 의 `LOG_LEVEL` 사용 |

자주 쓰는 패턴:

```yaml
# 빠른 검증 (DB 쓰기 없음)
scrape: { max_pages: 1, dry_run: true }

# 특정 소스만
scrape: { active_sources: [NTIS] }

# 목록만 (상세·첨부 모두 생략)
scrape: { skip_detail: true }

# 디버그 로그
scrape: { log_level: DEBUG }
```

설정한 뒤 sources.yaml 을 저장하면 **다음** 수집부터 반영된다 (실행 중인 수집은 영향
없음 — entrypoint 가 sources.yaml 의 per-run 임시 복사본을 사용하기 때문).

### 웹 UI 수동 시작·중단

관리자 로그인 후 상단 네비의 **관리자** → `/admin/scrape` 진입. 비관리자는 403,
비로그인은 401.

- **지금 시작**: 실행할 소스를 체크박스로 선택(모두 미선택 = 전체) → POST 가
  `docker compose --profile scrape run --rm scraper` 를 subprocess 로 띄우고 pid 기록.
- **중단**: SIGTERM 을 프로세스 그룹에 전파. 스크래퍼는 현재 처리 중인 공고를 마무리
  한 뒤 `status='cancelled'` 로 종료 (공고 단위 atomic 보장 유지).
- **로그**: 최근 이력 행의 **로그** 링크 = `/admin/scrape/runs/{id}/log`. subprocess
  stdout/stderr 파일을 `text/plain` 으로 반환 (최대 1 MB, 초과 시 앞부분 절단).
- **5초 폴링**: 페이지 인라인 JS 가 `/admin/scrape/status` 를 주기 호출 — 재로드 없이
  진행 상황 반영.

### 스케줄 등록

`/admin/schedule` 탭에서 cron 표현식 또는 매 N시간 간단 모드로 등록. 스케줄은 DB
의 `scheduled_jobs` 테이블(스케줄 SSOT)에 저장되고, 컨테이너 기동 시 이 테이블만
읽어 OS crontab 을 재생성하므로 **재기동에도 자동 복원**된다 (DB 만 백업하면 충분 —
별도 cron 파일을 챙길 필요 없음).

- cron 은 KST 기준 5필드. 예: `0 3 * * *` = 매일 KST 03:00, `30 9 * * *` = 매일 KST 09:30.
- 매 N시간 모드는 1~24 정수만 허용. 그 이상 주기는 cron 으로.
- 활성/비활성 토글은 잡을 삭제하지 않고 pause. 토글 시 `next_run_time` 재계산.
- `misfire_grace_time=300s` + `coalesce=True` — 재기동 직후 밀린 잡이 한꺼번에 폭주
  하는 것을 방지 (놓친 주기는 1회로 합쳐 실행).
- **uvicorn 단일 워커 전제** — `--workers 2` 이상으로 늘리면 같은 잡이 워커마다 중복
  실행되니 변경하지 않는다.

### 동시 수집 시도 시 동작

| 시점 | 동작 |
|------|------|
| 웹 "지금 시작" 클릭 시 다른 실행 중 | flash `이미 다른 수집이 진행 중입니다.` |
| CLI 진입 시 다른 실행 중 | 종료 코드 `2` + 진행 중 ScrapeRun id/trigger/pid 메시지 |
| 스케줄 트리거 시 다른 실행 중 | WARN 로그 후 skip, 스케줄 자체는 유지 |

웹 재기동 시 `pid IS NULL` 또는 호스트에 pid 가 없는 `running` row 는 자동으로
`failed (stale ...)` 정리된다 (살아 있는 pid 는 보수적으로 그대로 둠).

수동 정리:

```bash
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE scrape_runs SET status='failed', ended_at=datetime('now'),
   error_message='manual cleanup' WHERE status='running';"
```

### 수집 파이프라인 동작

수집 중에는 본 테이블 직접 UPSERT 가 없다 — `delta_announcements` /
`delta_attachments` 에만 INSERT 한다. ScrapeRun 종료 시점에 단일 트랜잭션으로 (a) delta →
본 테이블 4-branch 적용 + (b) `scrape_snapshots` 에 신규 row 1 개 INSERT (매 ScrapeRun 마다
1 row, 머지 없음) + (c) 같은 scrape_run_id 의 delta DELETE 가 한 번에 일어난다. **본 테이블·
snapshot 은 트랜잭션 commit 시점에만** 변한다 — 도중 SIGTERM·예외가 새지 않는다.

`scrape_snapshots` 는 `scrape_run_id` FK 가 NOT NULL + UNIQUE 라서 **1 ScrapeRun = 1 row**
가 DB 제약으로 보장된다. 같은 KST 날짜에 ScrapeRun 이 여러 번 끝나면 같은 `snapshot_date`
의 row 가 여러 개 생기며, 각 row 의 `created_at` 은 그 ScrapeRun 의 diff 산출 시각
(= apply 트랜잭션 commit 시각) 을 정확히 가리킨다. `updated_at` 컬럼은 호환성 위해 남아
있으나 INSERT 전용 흐름에서는 실질적으로 갱신되지 않는다 — 시점 비교는 항상 `created_at`
을 본다. Daily Report 의 `(last_sent_at, now]` 시간 필터와 dashboard A 섹션의 `(from, to]`
구간 머지 모두 이 보장 위에서 동작한다.

**4-branch 분류** (apply 단계 단일 공고 처리):

| 분기                  | 조건                                | 본 테이블 동작 | 사용자 라벨링 reset |
| --------------------- | ----------------------------------- | -------------- | ------------------- |
| `created`             | 신규 (is_current row 없음)         | INSERT         | X                   |
| `unchanged`           | 비교 4 필드 동일                    | (변화 없음)    | X                   |
| `status_transitioned` | status 만 변경                      | in-place UPDATE | X                  |
| `new_version`         | title/agency/deadline_at 변경 또는 첨부 sha256 변경 | 봉인 + 신규 INSERT | **O** |

비교 4 필드: `title`·`status`·`agency`·`deadline_at`. `received_at` 은 접수예정 단계의
미기재→보완 패턴이 잦아 비교에서 제외.

**ScrapeRun 종료 분기**:

| status | 의미 | apply | delta | 본 테이블 / snapshot |
|--------|------|-------|-------|----------------------|
| `completed` / `partial` | 정상 종료 (`partial` 은 일부 공고 실패) | O | 트랜잭션 안 비움 | 영구화 |
| `cancelled` | SIGTERM 사용자 의도 중단 | skip | 별도 트랜잭션 비움 | 변화 없음 |
| `failed` (apply) | apply 도중 예외 → rollback | rollback | rollback 으로 보존 | 변화 없음 |
| `failed` (수집) | apply 도달 전 예외 | skip | 별도 트랜잭션 비움 | 변화 없음 |

apply 자체 실패만 delta 가 보존되어 다음 ScrapeRun 으로 자동 복구된다.

**이력 / snapshot 조회**:

```sql
SELECT * FROM announcements WHERE is_current = 1;                         -- 현재 유효 버전
SELECT * FROM announcements WHERE source_type='IRIS' AND source_announcement_id='12345' ORDER BY id;  -- 이력 포함
SELECT id, scrape_run_id, snapshot_date, created_at, payload FROM scrape_snapshots
 WHERE snapshot_date = '2026-05-26' ORDER BY created_at ASC;              -- 그 날의 모든 row (같은 날 ScrapeRun 여러 번 = row 여러 개)
SELECT scrape_run_id, COUNT(*) FROM delta_announcements GROUP BY scrape_run_id;  -- 정상이면 0
```

### 수집 결과 보는 법 — `scrape_runs.source_counts` vs `scrape_snapshots.payload`

같은 ScrapeRun 을 두 컬럼에서 조회하면 내용이 꽤 다르게 보인다. 의도된 설계이며, 두
컬럼의 **목적과 범위가 다르기 때문**이다. 어떤 질문에 어느 컬럼을 봐야 하는지 정리한다.

| 컬럼 | 단위 | 무엇을 담는가 | 언제 보는가 |
|------|------|---------------|-------------|
| `scrape_runs.source_counts` | ScrapeRun 1 회 | 이 run 의 **raw 실행 통계** — 수집 단계 카운터(`collection.*`) + apply 단계 결과(`apply.*`) + 실패 공고 ID 리스트 | "이 run 이 정상 종료됐는가 / 무엇을 처리했는가" 진단용 |
| `scrape_snapshots.payload` | ScrapeRun 1 회 (= 1 row) | 이 run 의 **diff 결과 5종 카테고리** — `new` / `content_changed` / `transitioned_to_접수예정·접수중·마감` + `counts` | "이 run 으로 인해 dashboard 카드와 메일 본문에 어떤 공고가 올라가는가" 표시용 |

**`source_counts` 의 구조** (`_build_final_source_counts` 가 채움):

- `active_sources`: 이번 run 이 돌린 소스 ID 리스트.
- `collection.*`: 수집 단계 카운터 — `delta_inserted` / `delta_failed` / `detail_success` /
  `detail_failure` / `skipped_detail` / `attachment_download_success` /
  `attachment_download_failure`. 첨부 다운로드·상세 페이지 호출 같은 **raw I/O** 통계가
  들어간다.
- `apply.executed`: apply 단계가 실제 실행됐는지 (cancelled / orchestrator-failed 면 false).
- `apply.action_counts`: 4-branch 분류 카운트 — `created` / `unchanged` / `new_version` /
  `status_transitioned`. **본 테이블에 적용된 row 수**.
- `apply.new_announcement_ids` / `apply.content_changed_announcement_ids`: 신규 / 내용 변경
  공고 ID 리스트.
- `apply.transition_count`: 상태 전이 건수 (row 단위 합계).
- `apply.attachment_success` / `apply.attachment_skipped` / `apply.attachment_content_change`:
  apply 단계의 첨부 적용 통계.
- `failed_source_announcement_ids`: 수집 단계에서 실패한 공고 ID 리스트.

**`payload` 의 구조** (`build_snapshot_payload` 가 채움):

- `new`: `int[]` (asc 정렬) — 신규 등장 공고 ID.
- `content_changed`: `int[]` (asc 정렬) — title / agency / deadline_at 중 하나라도 바뀐 공고
  ID. 같은 공고가 `transitioned_to_*` 에도 동시에 들어갈 수 있다 (양립 가능).
- `transitioned_to_접수예정` / `transitioned_to_접수중` / `transitioned_to_마감`: 각
  `{id, from}` 객체 배열. status 단독 전이만 들어간다 (4-branch 의 `status_transitioned`
  분기). title 변경을 동반한 전이는 `new_version` 으로 분류되어 여기 안 들어간다.
- `counts`: 위 5 종 길이를 그대로 반영한 dict.

**왜 둘이 달라 보이는가** — 같은 ScrapeRun 이라도:

1. `source_counts` 는 `detail_failure` / `attachment_skipped` 같은 **raw 단계 카운터**까지
   포함하지만 `payload` 는 본 테이블에 들어간 diff 만 본다. 수집은 됐지만 본 테이블에
   안 들어간 공고는 `payload` 에 없다.
2. `source_counts.apply.transition_count` 는 **row 단위 합계** (3 종 to_label 통합), `payload`
   의 `transitioned_to_*` 3 종은 to 별 그룹핑이라 분포가 다르게 보인다.
3. `source_counts.apply.new_announcement_ids` 와 `payload.new` 는 같은 run 내에서 동일한
   ID 집합을 가지나, `payload.new` 는 asc 정렬·`int[]` 정규형이고 `source_counts.*` 쪽은
   삽입 순서대로 들어 있다.
4. dashboard 가 사용자에게 보여 주는 카드는 항상 **`payload` 기반** 이며, 여러 ScrapeRun
   의 `payload` 가 `(from, to]` 구간에서 시간순 reduce 머지된다 (같은 announcement_id 가
   여러 카테고리에 들어가면 머지 단계에서 정정 / 제거된다). `source_counts` 는 dashboard
   계산에 쓰이지 않는다.

**자주 쓰는 SQL 예시**:

```sql
-- 어떤 run 이 정상 종료됐고 무엇을 처리했는지 (source_counts 사용)
SELECT id, trigger_source, status, started_at, ended_at,
       json_extract(source_counts, '$.collection.delta_inserted') AS delta_inserted,
       json_extract(source_counts, '$.apply.action_counts')      AS action_counts,
       json_extract(source_counts, '$.apply.transition_count')   AS transition_count
  FROM scrape_runs
 WHERE id = 88;

-- 그 run 이 dashboard / 메일에 어떤 변화를 올렸는가 (payload 사용)
SELECT s.id, s.scrape_run_id, s.snapshot_date, s.created_at,
       json_extract(s.payload, '$.counts') AS counts
  FROM scrape_snapshots AS s
 WHERE s.scrape_run_id = 88;
```

### NTIS 운영 특이사항

- **마감 공고가 74,000건+** 으로 매우 많아 sources.yaml 기본값을 보수적으로(5페이지·100건)
  잡았다. 전체 수집이 필요하면 `max_pages` / `max_announcements` 를 명시적으로 늘린다.
- 첨부는 **httpx POST 직접 다운로드** (IRIS 의 Playwright 경로와 다름). Playwright 미설치
  환경에서도 NTIS 첨부는 정상 동작한다.
- 목록 단계에서는 공식 공고번호(ancmNo) 를 모르므로 fuzzy canonical key 가 부여되고,
  상세 수집 후 자동으로 official 키로 승급된다. 승급 시 로그:
  `INFO  canonical 재계산 완료(fuzzy→official): source=NTIS id=… ancm_no=…`.
  승급이 일어나지 않는 경우는 **개별공고**(공고형태=개별) 또는 본문에서 공고번호
  파싱 실패 — fuzzy 그룹으로 남고 cross-source 매칭이 약해진다.

### 고아 첨부파일 GC

apply 트랜잭션이 실패하거나 운영자가 attachments 행을 수동 삭제하면 `data/downloads/`
에 DB 가 참조하지 않는 고아 파일이 누적될 수 있다 (첨부 파일은 트랜잭션 보호 밖).

```bash
# 1) 후보 검수 (디스크 변경 없음)
docker compose --profile scrape run --rm scraper \
    python scripts/python/gc_orphan_attachments.py --dry-run

# 2) 실제 삭제
docker compose --profile scrape run --rm scraper \
    python scripts/python/gc_orphan_attachments.py
```

수집 진행 중에는 GC 가 거부된다 (종료 코드 2 — `--force` 로 우회 가능, 권장 X).

자동 일 1회 등록 (KST 04:00 기본):

```bash
docker compose --profile scrape run --rm scraper python -c "
from app.scheduler.service import start, add_gc_orphan_cron_schedule
start()
print(add_gc_orphan_cron_schedule())
"
```

---

## DB 관리

### DB 파일 위치

- 메인: `./data/db/app.sqlite3` (`.env` 의 `DB_URL` 로 변경 가능)
- 게시판 공유 DB: `./data/db/boards.sqlite3` — 건의사항·공지사항 게시글 보존용
  (메인 DB 리셋과 무관). Alembic 관리 밖, 앱 기동 시 자체 초기화.

### 백업

`scripts/python/backup_db.py` 가 SQLite 온라인 백업(`sqlite3.backup()`)을 수행하므로
스크래퍼 실행 중에도 일관된 스냅샷이 만들어진다.

```bash
docker compose run --rm scraper python scripts/python/backup_db.py
# 옵션: --keep 30 (보관 개수) / --dest /mnt/backups (저장 위치)
```

- 저장 위치 기본: `./data/backups/`
- 파일명: `app.sqlite3.YYYYMMDDThhmmssZ.bak` (UTC 타임스탬프)
- 보관: 최근 14개 (mtime 기준), 나머지 자동 삭제
- `DB_URL` 이 SQLite 가 아니면(예: Postgres) skip + 종료 코드 0

호스트 cron 등록 권장:

```bash
# /etc/cron.d/gov-project-backup
0 2 * * * cd /path/to/repo && \
  docker compose run --rm scraper python scripts/python/backup_db.py \
  >> /var/log/gov-backup.log 2>&1
```

### 복원

```bash
docker compose down app                                              # 1) 잠금 해제
cp ./data/backups/app.sqlite3.20260422T150000Z.bak ./data/db/app.sqlite3   # 2) 덮어쓰기
docker compose up app                                                # 3) 재기동
```

복원 직후 `/` 가 정상 렌더링되는지 확인.

### DB 초기화

```bash
# 백업 먼저!
docker compose run --rm scraper python scripts/python/backup_db.py
rm -f ./data/db/app.sqlite3
docker compose --profile scrape run --rm scraper      # 기동 시 스키마 자동 생성 + 신규 수집
```

게시판 DB(`boards.sqlite3`) 는 별도 파일이라 메인 초기화 시 영향받지 않는다.

### Alembic 마이그레이션

기동 시 자동 적용 (별도 명령 불필요).

| DB 상태                 | 동작                                            |
| ----------------------- | ----------------------------------------------- |
| 빈 DB                   | `upgrade head` — baseline 스키마 전체 생성      |
| Alembic 도입 전 기존 DB | `stamp head` — 데이터 무변경, 리비전만 삽입     |
| Alembic 관리 DB         | `upgrade head` — 신규 migration 적용 (없으면 no-op) |

수동 확인 / 적용:

```bash
docker compose run --rm scraper alembic current             # 현재 리비전
docker compose run --rm scraper alembic history --verbose   # 이력
docker compose run --rm scraper alembic upgrade head        # 수동 upgrade
docker compose run --rm scraper alembic downgrade -1        # 한 단계 롤백
```

migration 추가 시 검증 절차는 `docs/db_portability.md §4` 참조.

### 일회성 백필 스크립트

#### canonical 재계산 (구버전 데이터)

기존 수집분에 `canonical_group_id` 가 비어 있는 row 가 있다면 한 번 실행. 멱등.

```bash
docker compose run --rm scraper python scripts/python/backfill_canonical.py --dry-run
docker compose run --rm scraper python scripts/python/backfill_canonical.py --batch-size 200
```

#### KST 가정 보정 (구버전 데이터)

외부 응답 날짜를 UTC 로 잘못 저장한 row 가 있다면 한 번 실행. 멱등.

```bash
docker compose run --rm app python scripts/python/backup_db.py
docker compose run --rm app python scripts/python/backfill_kst_assumption.py            # dry-run
docker compose run --rm app python scripts/python/backfill_kst_assumption.py --apply    # 실제 적용
```

### 이력 row 정리 (선택)

`is_current=False` 누적이 부담스러우면:

```bash
# 백업 후 실행. 되돌릴 수 없음.
sqlite3 ./data/db/app.sqlite3 "DELETE FROM announcements WHERE is_current = 0;"
```

---

## 사용자 기능 안내

> 모든 라우트는 로컬망 전제. 비로그인 열람이 기본 — 목록·상세·첨부 다운로드는 누구나
> 가능. 로그인 시 추가되는 기능을 아래에 정리한다.

### 회원가입 / 로그인 / 세션

- **회원가입**: 우상단 네비 `회원가입` 또는 `/register`. username/password (선택 email).
  가입 즉시 자동 로그인. 자유 가입 — 항상 `is_admin=False`.
- **로그인 / 로그아웃**: `/login`, `/auth/logout`. 세션 쿠키 HttpOnly+SameSite=Lax,
  `Secure=False` (로컬 HTTP 전제).
- **세션 수명**: 기본 30일 (`app/auth/constants.py::SESSION_LIFETIME_DAYS`).

세션 강제 만료(운영자가 강제 로그아웃 필요할 때):

```bash
# 모든 세션
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE user_sessions SET expires_at = datetime('now', '-1 day');"

# 특정 사용자만
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE user_sessions SET expires_at = datetime('now', '-1 day')
   WHERE user_id = (SELECT id FROM users WHERE username = 'root_user');"
```

만료 row 자체는 자동 삭제되지 않는다.

### 개인 설정 (`/settings`)

로그인 사용자 메뉴. 4개 섹션이 한 페이지에 표시된다.

| 섹션 | 동작 |
|------|------|
| **이메일 변경** | 새 이메일 입력 → 저장. 비어 있으면 NULL 로 저장 (이메일 알림 자동 비활성). |
| **이메일 알림 토글** | 켬 = 게시판 수용여부 결정 등 알림 수신. 이메일이 비어 있으면 노란 경고 배지 표시. |
| **비밀번호 변경** | 현재 비밀번호 + 새 비밀번호 2회. 현재 비밀번호 검증 실패 시 422. |
| **소속 조직 선택** | 0개 이상 다중 선택 (admin/organizations 트리 기반). 빈 선택은 모든 조직 해제. |

### 공고 목록 검색 / 필터 / 중복 묶음

`/` 또는 `http://localhost:8000` 의 GET 쿼리 파라미터로 상태가 보존된다.

| 파라미터 | 허용값 | 기본 |
|----------|--------|------|
| `status` | `접수중` `접수예정` `마감` | 전체 |
| `source` | `IRIS` `NTIS` 등 소스 ID | 전체 |
| `search` | 문자열 (제목 부분 일치) | — |
| `sort` | `received_desc` `deadline_asc` `title_asc` | `received_desc` |
| `group` | `on` / `off` | `off` |
| `page` | 정수 | `1` |

**중복 묶어 보기 (`group=on`)**: 같은 과제가 IRIS·NTIS 양쪽에 등록되면 1행으로 묶고
우측 배지(`3건`) 클릭으로 소스별 펼치기. `group=off` 는 소스별 1행씩 표시 + `동일
과제 N건` 배지로 중복 안내.

### 관련성 판정

로그인 사용자는 본인이 속한 **조직 입장**으로 공고(canonical 과제) 단위 **관련 /
무관** 판정을 남길 수 있다 (개인 입장 판정은 task 00093 에서 제거되었다).
UNIQUE 키 `(canonical_project_id, user_id, organization_id)` 단일 — 본인이 속한 각
조직마다 row 1 개씩. 무소속 사용자는 작성·수정·삭제 영역이 비활성 (비로그인 사용자
와 동일 노출).

배지 색: 관련(초록), 무관(회색), 미검토(테두리만). 큰 배지에 표시되는 verdict 는
**본인이 만든 조직 row 중 `decided_at` 이 가장 최근인 row** 의 값이다. 본인 row 가
하나도 없으면 미검토 (빈 배지).

배지 클릭 → 모달 → 조직 선택 + 라디오 선택 + (선택) 사유 입력 → **저장**. 페이지
reload 없이 즉시 반영. 판정 변경 시 이전 판정은 자동으로 이력으로 이관된다
(덮어쓰기 아님).

배지 또는 카운터 hover → **viewport 기준 fixed 레이어 툴팁** (task 00088). 공고 셀의
`overflow:hidden` 클리핑을 받지 않으며 위/아래 자동 반전 + 좌우 viewport 클램핑.
본인이 만든 조직 row 또는 다른 사용자·조직 row 중 **하나라도 있으면** 노출 — 본인
row 가 없어도 OTHERS 가 있으면 그대로 보인다.

#### 조직 단위 판정 (task 00085)

같은 canonical 에 대해 본인이 속한 조직마다 row 1 개씩 가질 수 있다. 모달의 조직
선택 동작:

- **무소속**: 조직 선택 자체 비활성 + 안내 "소속된 조직이 없습니다."
- **단일 조직 소속**: 조직명이 라벨에 직접 표시됨 (드롭다운 없음).
- **복수 조직 소속**: 드롭다운으로 본인 소속 조직 중 하나 선택.

같은 조직의 다른 멤버가 만든 row 는 본인 row 와 독립이며, 같은 조직 입장에서도
사용자별 의견이 분기하면 그대로 데이터에 표현된다 (UNIQUE 키에 `user_id` 가
포함되므로 충돌하지 않음).

**큰 배지 우선순위**: 본인이 만든 조직 row 중 `decided_at` 이 가장 최근인 1 개의
verdict. 없으면 미검토. hover 툴팁(fixed 레이어, 00088)에는 본인이 만든 조직 row 가
모두 최신순으로 나열된다 (조직명 + verdict + 사유).

**카운터 (`✅ N ❌ M`)**: ❓ (미검토) 카운터는 노출하지 않는다 (task 00087 에서 백엔드
필드·프론트 표시 모두 제거). 집계 범위는 **본인 + 타인 row 전부** 가 포함된다 (task
00090 에서 others-only 였던 집계를 mine_personal + mine_organization + others 전체로
확장). 같은 조직 안에 여러 사용자 row 가 있으면 각각 1 카운트 — 조직 의견 분기가
카운트에 그대로 반영된다.

**모달 동작 (task 00089 / 00091)**: 모달 하단에 본인이 만든 판정 목록이 행으로 표시
되고 사유가 있는 행에는 사유 텍스트가 그대로 노출된다. 각 행 우측의 **X 버튼** 클릭
시 `window.confirm()` 다이얼로그로 삭제 확인 후 즉시 삭제. 저장 흐름은 조직 선택 +
verdict 라디오 + 사유 입력 → **저장** 만 — 별도 '판정 취소' 버튼은 두지 않는다 (삭제
는 모달 하단의 본인 판정 목록 X 로만 수행).

**권한**: 본인이 만든 row 만 본인이 수정·삭제할 수 있다 (Phase B 컨벤션 — 작성자 본인
한정). 같은 조직 동료가 만든 row 는 본인이 건드릴 수 없으며, 본인이 같은 조직 입장
으로 새 row 를 만들면 트리플 키가 달라 별개의 row 로 공존한다.

**상세 페이지 풀어 표시**: 공고 상세 하단의 "관련성 판정" 섹션에 (1) 본인이 만든
조직 row + 본인 row 마다 [수정][삭제] 버튼, (2) 다른 사용자·조직 판정 — 작성자 /
조직명 / verdict / 시점 / 사유 가 행으로 풀어 표시된다.

**비로그인 노출**: 로그인 사용자와 **동일** — 카운터·OTHERS 행·조직명 모두 그대로
보인다. 단, 본인 작성·수정·삭제 영역은 비활성 (배지가 readonly span 으로 렌더되어
클릭 불가). 로그인 후에 라벨링이 가능하다.

### 공고 진행 상태 (task 00097, Phase C)

같은 공고를 여러 조직이 모르고 중복 진행하는 사고를 방지하기 위해, canonical 단위로
**조직별 진행 상태**를 표명·열람할 수 있다 (관련성 판정과는 별개 시스템).
설계 근거는 `docs/progress_org_design.md`.

**4 단계 status enum** — 한글 값 그대로 저장된다.

- **관심**: 관심 공고 표시. 여러 조직 동시 가능.
- **검토**: 검토 단계. 여러 조직 동시 가능.
- **진행**: 실제 진행 단계. **한 canonical 당 단일 조직 선점** — 다른 조직이 이미
  '진행' 인 상태에서 본인 조직을 '진행' 으로 올리려고 하면 409 + "조직 X 가 이미
  진행 중입니다" 안내. 선점은 partial unique index 가 아닌 repository app-level
  transactional 체크가 보장한다 (`docs/db_portability.md` §3 회피 결정 + SQLite
  단일 writer 가정).
- **종료**: 종료 단계. 카운터·필터·본인 활동 단계 어디에도 노출하지 않는다 (의미
  없음). 다른 단계로 자유 롤백 가능 — 모든 4 단계 사이 양방향 전이 허용.

**권한 = 조직 멤버 누구나** (관련성 판정의 'row 작성자 본인만' 과 의도적으로 다름).
본인이 row 의 organization_id 에 소속되어 있기만 하면 작성자가 누구든 수정·삭제
가능 — 작성자 휴가/퇴사 시 다른 멤버가 변경할 수 있도록 한 협업 결정. 무소속
사용자는 작성·수정·삭제 모두 거부 (422). 본인 소속 외 조직 row 변경 시도 → 403.
`created_by_user_id` 는 "마지막 수정자" 메타로만 보존된다.

**목록 셀 표시**:

- 선점 라인: 🚩 + 조직명 + ': 진행' 큰 글씨. 본인 조직이 진행이면 조직명 강조 색
  + underline.
- 카운터 라인: `검토 N · 관심 M` (0 인 항목 생략, 둘 다 0 이면 라인 미렌더). 본인
  조직이 활동 중인 단계는 파란색·굵게 강조.
- 빈 셀 (선점 없음 + 카운터 0/0): em dash `—`.
- hover 툴팁 (Phase B 의 viewport 기준 fixed 레이어, 00088 패턴 재사용) — 선점
  조직명 + 단계별 카운트 요약. 셀 클릭 시 조직별 단계·작성자·시점·note 가
  hidden `<tr>` expand 로 펼쳐진다 (Phase 3b 의 동일 과제 expand 와 시각적 일관).

**상세 페이지 인라인 섹션**:

- 본인 소속 조직마다 row 슬롯 풀어 표시 — 이미 row 가 있으면 status select(4 옵션) +
  note textarea + [저장][삭제]. 없으면 `[ 우리 조직 입장 표명하기 ]` 버튼 → 인라인
  폼 펼침 → status 선택 + note + 저장.
- 다른 조직 row 는 read-only — 조직명 + 단계 배지 + 마지막 수정자 + 시점 + note.
- 비로그인은 본인 영역 미렌더 (안내 문구만), 다른 조직 row 는 그대로 노출.
- 무소속 사용자는 "소속된 조직이 없습니다" 안내.
- 저장·삭제 후 페이지 reload — 선점 충돌 / 권한 거부 / 무소속 등의 4xx 응답 시
  row 안 feedback 박스에 한국어 detail 표시 (페이지 이동 없음, 입력값 보존).

**다중 체크박스 필터** (`?progress=...`):

| UI 라벨 | URL 파라미터 키 | 의미 |
|---------|-----------------|------|
| 선점 미발생 | `none` | 아무 조직도 진행 단계가 아닌 canonical |
| 다른 조직이 진행 | `other_in_progress` | 본인 외 조직이 진행 단계 (충돌 회피용 핵심) |
| 내 조직이 진행 | `mine_in_progress` | 본인 소속 조직이 진행 단계 |
| 내 조직 검토 중 | `mine_in_review` | 본인 소속 조직이 검토 단계 |

- URL 키는 영문 채택 (한글 percent-encoding 회피). 다중 선택 시 **OR** 의미 — 해당
  조건 중 하나라도 만족하는 canonical. AND / 복잡 boolean 은 범위 밖.
- URL 형식 두 종류 모두 허용: `?progress=A&progress=B` (HTML 폼 표준) 또는
  `?progress=A,B` (가독성 좋은 콤마 형식). 서버가 자동 평탄화.
- 비로그인 사용자: "내 조직 ..." 두 옵션 disabled + "로그인 후 사용 가능" 안내.
  URL 에 `mine_*` 키가 직접 와도 서버가 silent drop (401 거부 아님 — 다른 조건은
  적용).
- 페이지네이션 정합 — `progress` 파라미터가 페이지 링크에 보존된다.

**비로그인 노출**: 로그인 사용자와 **동일** — 카운터·선점 조직명·다른 조직 row 모두
그대로 보인다. 변경 영역만 비활성. 사용자 결정 (Phase B 와 동일 정책).

**API 엔드포인트**:

| 메서드 | 경로 | 권한 |
|--------|------|------|
| `POST` | `/canonical/{id}/progress` | current_user_required + 본인 소속 조직 검증 (외부 조직 / 무소속 → 422) |
| `PATCH` | `/canonical/{id}/progress/{progress_id}` | 같은 조직 멤버 누구나 (외부 조직 → 403, 무소속 → 422). 선점 충돌 → 409. |
| `DELETE` | `/canonical/{id}/progress/{progress_id}` | PATCH 와 동일 권한 |
| `GET` | `/canonical/{id}/progress/history` | 비로그인 허용 (Phase B GET history 패턴) |

`content_changed` reset (canonical 의 title/agency/deadline_at 변경 감지) 시
`announcement_progress` 도 history 로 일괄 이관 (`archive_reason='content_changed'`)
— 관련성 판정과 동일 hook (`_reset_user_state_on_content_change`) 에서 함께
처리된다.

### 읽음 일괄 처리

목록 페이지에서 체크박스로 선택 → 툴바의 **읽음 / 안읽음 / 선택 해제** 버튼.

- 개별 / 페이지 전체 선택 (헤더 체크박스).
- 페이지 전체 선택 시 툴바에 `현재 필터 결과 전체 M건 선택` 링크 노출 — 클릭 시 서버가
  필터를 재적용해 모든 페이지의 공고를 일괄 처리한 뒤 페이지를 새로고침.
- 한 번에 최대 5,000건 (`MAX_BULK_MARK` 환경변수로 조정). 초과 시 422 + 선택 유지.

```bash
# .env 변경 후 docker compose restart app
MAX_BULK_MARK=10000
```

### 즐겨찾기 / 동일 과제

저장 단위는 **공고(announcement)** 1건. 같은 canonical 그룹의 다른 공고는 별도로
저장된다.

목록·상세 제목 왼쪽 별 아이콘(`☆ / ★`, 로그인 사용자만) 클릭 → 폴더 선택 모달:

- **이 공고만 저장** (기본) — 클릭한 공고 1건만 등록.
- **동일 과제 공고 모두 저장** — 같은 canonical 그룹의 is_current 공고 전체를 한꺼번에
  등록. canonical 매칭이 없으면 비활성.

이미 해당 폴더에 있는 공고는 조용히 skip (오류 아님). ★ 클릭 시 **그 공고 1건만** 제거.

**즐겨찾기 탭 (`/favorites`)** — 좌측 폴더 트리(그룹 > 서브그룹 2단계, caret 접기·펼치기,
✎ 이름 변경 / ✕ 삭제) + 우측 공고 테이블(관련성 배지·소스·상태·마감일 + **이동 / 제거**
버튼, 안 읽은 공고는 제목 굵게). 폴더 삭제 시 하위 서브그룹·공고가 모두 cascade 삭제
되며 다이얼로그에 영향 범위가 표시된다.

**동일 과제 확인 (비로그인 가능)** — 목록 행의 `동일 과제 N건 ▾` 배지 인라인 expand,
또는 상세 페이지 하단 `동일 과제 (N건)` 섹션. 매칭 근거 배지 `[공식]`(공고번호 정확)
/ `[유사]`(fuzzy). fuzzy 매칭에서 드물게 false-positive 가 발생 가능 — 즐겨찾기 저장
전 제목을 확인할 것을 권장한다 (알려진 패턴: `docs/canonical_identity_design.md §11`).

### 공고 포워딩 (메일로 보내기)

공고 상세 페이지 제목 줄의 **✉ 메일로 보내기** 버튼(즐겨찾기 별 옆)으로 해당 공고를
"검토 요청" 메일로 다른 사람에게 보낼 수 있다. **로그인 사용자라면 누구나** 사용
가능하며 조직 소속 여부와 무관하다. 비로그인 상태에서는 버튼이 비활성화된다.

버튼을 누르면 **공고 메일 보내기** 모달이 열린다:

- **받는 사람** — 이름·이메일을 입력하면 내부 사용자(이메일이 등록된 사용자)가
  자동완성 드롭다운으로 뜨고, 선택하면 chip 으로 추가된다. 외부 이메일은 직접 입력
  후 Enter 또는 콤마로 chip 추가. chip 우상단 X, 또는 입력칸이 빈 상태에서 Backspace
  로 제거. 최대 50명.
- **발신 조직** — **본인이 소속된 조직만** 발신 조직으로 지정할 수 있다. 소속이 1개면
  자동 선택되고, 여러 개면 dropdown 으로 고른다. **무소속 사용자**는 발신 조직 없이
  개인 자격으로 발송되며, 발송 이력에 `(개인)` 으로 표시된다.
- **제목** — 공고 제목 기반 기본값이 채워져 있으며 수정 가능. 비우고 보내면 서버가
  기본 제목을 다시 생성한다.
- **추가 메시지** — 수신자에게 함께 전달할 메모(선택, 최대 5,000자). 메일 본문에는
  들어가지만 DB 에는 저장되지 않는다.
- **본문 미리보기** — 토글하면 메일 HTML 본문의 대략적인 모습을 보여 준다. 공고
  요약·발송자 정보 등 서버만 아는 값은 빠진 참고용 미리보기로, 실제 발송 본문과
  완전히 같지는 않다.

**발송**을 누르면 수신자에게 1명씩 개별 발송되며(다른 수신자 명단은 노출되지 않는다),
완료 후 모달에 `성공 N명 / 실패 M명` 결과가 표시되고 잠시 후 자동으로 닫힌다.

#### 발송 이력 보는 법

공고 상세 페이지 하단의 **발송 이력** 섹션에서 그 공고가 언제 누구에게 발송됐는지
확인할 수 있다 (비로그인도 조회 가능). 한 번도 발송된 적 없으면 안내 문구만 표시된다.

각 행은 발송 시각(KST) / 발송자·발신 조직 / 수신자 수 / 상태(✅ 성공 · ⚠️ 부분 ·
❌ 실패) / 제목 / 추가 메시지 유무를 보여 준다. 행을 클릭하면 펼쳐져 수신자별 발송
결과(받는 사람 / 성공·실패 / 시도 횟수 / 에러 메시지 / 발송 시각)가 나온다. 일부
또는 전체가 실패한 경우 이 펼침 화면의 **에러 메시지**에서 원인을 확인한다.

### 대시보드

상단 네비의 **대시보드** 링크 (비로그인 가능). 두 시점 비교 + 임박 공고 + (로그인 시)
사용자별 라벨링 카운트와 추이 차트.

- **컨트롤**: 기준일 캘린더(`to`) + 비교 대상(전날/전주/전월/전년/직접 선택). 캘린더는
  `scrape_snapshots.snapshot_date` 가 있는 날만 클릭 가능 — 변화 0건이어도 ScrapeRun 이
  completed/partial 로 끝났으면 활성. 비교일 부재 시 가장 가까운 이전 snapshot 으로
  자동 fallback + 노란 안내문.
- **A 섹션 (공고 변화)**: `(from, to]` 구간 snapshot 을 시간순 누적 머지 → 5종 카드
  (신규 / 내용 변경 / 전이 → 접수예정·접수중·마감). 카드 클릭 시 `<details>` expand.
- **B 섹션 (임박 공고)**: 향후 30일 접수예정 / 마감예정 공고를 가로 2열로 임박 순.
  `to` 가 과거면 `현재 기준이라 정확하지 않을 수 있습니다` 안내.
- **사용자 라벨링 위젯 (로그인)**: 전체 미확인 / 전체 미판정 / 기준일 변경 중 미확인 /
  기준일 변경 중 미판정 4종 카드.
- **추이 차트**: 기준일 ±15일 일별 카운트 line chart. Chart.js v4 로컬 번들.

대시보드는 **자동 갱신 안 함**. 새 ScrapeRun 이 끝나도 reload 또는 컨트롤 변경 전까지
이전 데이터를 보여 준다.

### 게시판 — 건의사항 / 공지사항

footer 의 두 버튼으로 진입. 메인 DB 와 분리된 `boards.sqlite3` 에 저장되어 메인 리셋
시에도 유지된다. 삭제는 모두 소프트 삭제 (`deleted_at IS NULL` 필터). 메인 DB reset
으로 작성자 FK 가 끊긴 행은 작성자 표기를 익명으로 처리.

- **건의사항 (`/suggestions`)**: 로그인 사용자 작성. 제목·본문 + (선택) 비밀글
  (작성자 본인 + 관리자만 조회). 댓글 작성/수정/삭제 가능 (자기 댓글만, 댓글 삭제만
  하드 삭제). 게시글 삭제는 작성자 본인 또는 관리자, 수정은 작성자 본인만. 관리자는
  **수용 여부** 라디오 + **예상 개발일** + **피드백** 본문을 별도 폼으로 저장.
- **공지사항 (`/notices`)**: 관리자만 작성·수정·삭제. 댓글·비밀글·수용여부 없음.

---

## 관리자 기능

관리자 로그인 후 상단 네비 **관리자** 클릭. 라우터 레벨 `admin_user_required` 가
걸려 있어 비관리자는 403, 비로그인은 401.

### 공고 수집 제어 (탭)

- **수집 제어** (`/admin/scrape`): 위 [스크래퍼 실행](#스크래퍼-실행) 절 참조.
- **sources.yaml** (`/admin/sources/yaml`): 호스트 sources.yaml 을 textarea 로 편집.
  저장 파이프라인 = YAML 구문 → Pydantic `SourcesConfig.model_validate`. 어느 단계
  실패해도 원본 보존 + 오류 메시지 화면 상단. 저장 직전 `data/backups/sources/
  YYYYMMDD_HHMMSS.yaml` 자동 백업 + 원자적 쓰기. **편집은 다음 수집부터 반영**
  (실행 중인 수집은 per-run 임시 복사본 사용).
- **스케줄** (`/admin/schedule`): 위 [스케줄 등록](#스케줄-등록) 절 참조.

### 조직 관리

`/admin/organizations`. 트리 구조 (재귀 CTE 대신 메모리 빌드 — 로컬 소규모 데이터
전제). 동작:

- **추가 / 삭제**: 자식·멤버가 있으면 삭제 거부 (`ON DELETE RESTRICT` + app-level 체크).
- **이름 변경 (rename)**: 같은 부모 아래 동명 충돌 시 거부.
- **이동 (move)**: 자기 자신 / 후손으로 이동 차단. 트리 위치 변경 후 user_organizations
  매핑은 그대로 유지 (FK 그대로).
- **export**: `GET /admin/organizations/export` → JSON 파일 다운로드.
- **import**: JSON 업로드 → 트리 전체 교체. user_organizations 는 **이름 경로 기반
  재매핑** (이름 경로가 사라진 매핑은 드롭, 영향받은 사용자 수·드롭 수가 flash 로
  안내됨). FK 깨짐 방지를 위한 핵심 가드.

### 사용자 관리

`/admin/users`. 전체 사용자 목록. 행마다:

- **비밀번호 변경** — 임시 비밀번호 발급용. 본인 동의 절차는 별도.
- **조직 변경** — 0개 이상 다중 선택.
- **계정 삭제** — `DELETE /admin/users/{id}`. 즐겨찾기·열람내역·관련성 검토를 함께 정리.

### 이용 통계

`/admin/usage`. ASGI 미들웨어가 `data/access_logs/{date}.jsonl` 에 7개 항목(IP·시각·
경로·method·UA·user_id·status_code) 을 기록 → 이 페이지가 일별·IP별·세션 집계로 표시.

- 패널 3종: 최근 7일 일별 접근, IP별 접근 이력, 오늘 최근 로그 원본.
- **IP 필터**: `?ips=1.2.3.4,5.6.7.8&mode=include|exclude` 쿼리 파라미터로 특정 IP 만
  포함 / 제외하고 전체 집계 재수행. 입력창 + Reset 버튼 UI 제공. 필터 상태는 URL
  쿼리스트링으로 유지 (페이지 이탈 시 초기화).
- DB 미사용 — 로그 파일 위치는 `ACCESS_LOG_DIR` 환경변수로 변경 가능.

### 게시판 관리자 액션

- 건의사항: 수용 여부 / 예상 개발일 / 관리자 피드백 저장. 작성자 무관 삭제 허용
  (수정은 작성자 전용).
- 공지사항: 작성·수정·삭제 모두 관리자 전용.

### 메일 발송 설정 (Phase A-1, task 00104)

「시스템 관리」 → 「메일 발송」 (`/admin/email`) 에서 자격증명 입력 + 테스트 발송 +
이력 확인을 모두 수행한다. **자격증명은 SystemSetting 에 저장** 되며 환경변수는
사용하지 않는다. 변경은 다음 발송부터 즉시 반영된다.

#### 입력해야 하는 값 (IT 셋업 결과를 옮겨 받기)

IT 팀이 Azure AD app registration (M365 OAuth XOAUTH2) 셋업 완료 후 알려주는
3 개 값과 발신 mailbox 주소 1 개, 총 4 개를 「메일 설정」 폼에 입력한다.

| 폼 라벨 | SystemSetting 키 | 의미 |
|---|---|---|
| Directory (tenant) ID | `email.m365.tenant_id` | Azure AD Directory ID (UUID) |
| Application (client) ID | `email.m365.client_id` | Azure AD app registration 의 client ID (UUID) |
| Client secret | `email.m365.client_secret` | client secret 평문. **응답에는 항상 마지막 4자 mask** (`****abcd`) 만 노출. 변경 시 [변경] 토글을 켜고 새 값 입력. 빈 값/누락 PUT 은 기존 값을 그대로 유지. |
| 발신 mailbox | `email.m365.sender_address` | IT 가 SendAs 권한을 부여한 메일박스 주소. default = `gov-agent-noreply@innodep.com`. |
| From 표시명 | `email.from_display_name` | 수신자 메일 클라이언트에 보이는 이름. default = `정부사업 모니터링 봇`. |
| 재시도 횟수 | `email.max_retry_count` | 1 차 시도 실패 후 추가 재시도 횟수 (0~5, default 2). 재시도 간 2 초 단순 sleep. |

저장 직후 폼은 새 값으로 다시 채워지며, client secret 토글은 자동 OFF 로 돌아간다.

#### 「테스트 발송」 사용법

「테스트 발송」 섹션에서 본인 메일로 plain text 한 통을 보낸다.

1. **받는 사람**: 본인 회사 메일 주소 (예: `seungkeol_kim@innodep.com`).
2. **제목**: default `A-1 테스트 발송` 그대로 두거나 수정.
3. **본문**: 빈 칸이면 placeholder 안내 문구. 임의 텍스트 입력 가능.
4. **발송** 클릭 → 발송 중 spinner 표시 → 결과 박스:
   - 성공: 초록 박스 `발송 성공 (send_run_id: 123). 수신함과 정크메일 폴더를 모두 확인해주세요.`
   - 실패: 빨간 박스 `발송 실패 (HTTP <status>): <예외 클래스명>: <메시지> (send_run_id=N)` + 발송 이력 안내.

실패 시에도 EmailSendRun row 가 남으므로 같은 페이지의 「발송 이력」 섹션에서
원인을 확인할 수 있다.

#### 「발송 이력」 보는 법

최근 50 건의 발송 시도가 시각(KST) 내림차순으로 표시된다. 컬럼:

- **시각 (KST)**: 발송 시도 시작 시각.
- **받는 사람** / **제목**: 입력값. 긴 제목은 자르고 hover 시 tooltip 으로 전체 노출.
- **상태**: ✅ 성공 / ❌ 실패.
- **시도 횟수**: 1 차 + 재시도 누적 (예: 2 = 1 차 실패 + 2 차 성공).
- **에러**: 실패 row 만. 마지막 시도의 예외 메시지 첫 줄 + tooltip 으로 전체.
- **발송자**: `requested_by_user_id` 의 username. 시스템 자동 발송 (Daily Report 예약 발송)
  이면 `(자동)` 으로 표시.

「상태 필터」 드롭다운으로 전체 / 성공만 / 실패만 전환. 우측 **새로고침** 버튼으로
수동 갱신. 「테스트 발송」 직후에는 페이지가 자동 새로고침되어 방금 발송한 row 가
즉시 보인다 (`email-test-send-completed` custom event).

#### 디버깅 순서 (테스트 발송 실패 시)

테스트 발송이 빨간 박스로 떨어지면 아래 순서로 점검한다:

1. **자격증명 4 개 값** — 「메일 설정」 폼의 tenant_id / client_id / client_secret /
   sender_address 가 IT 가 전달한 값과 정확히 일치하는지 (앞뒤 공백·오타 확인).
   client_secret 은 mask 만 보이므로 [변경] 토글 → 다시 붙여 넣기 → 저장.
2. **「발송 이력」 의 `error_message`** — 실패 row 의 에러 컬럼 hover 로 전체 예외
   확인. 메시지 패턴별 단서:
   - `M365 OAuth token 발급 실패: error='invalid_client' ...` → client_secret 만료
     또는 client_id / tenant_id 불일치.
   - `M365 OAuth XOAUTH2 SMTP 인증 실패: code=535 ...` → 토큰은 받았지만 SMTP
     서버가 거부. SendAs 권한이 sender_address 에 부여되지 않았을 가능성.
   - `smtplib.SMTPRecipientsRefused` / `SMTPSenderRefused` → 수신자 / 발신자 도메인
     문제. 회사 정책상 외부 도메인 발송 제한 가능성.
3. **서버 로그 (loguru)** — `./run_compose.sh logs app | grep -i email` 로 본 발송
   시도 직전후의 DEBUG/WARNING 라인 확인. msal / smtplib 가 어느 단계에서 실패
   했는지 stack trace 와 함께 노출됨.
4. **IT 측 셋업 재확인** — Azure AD app registration 상태 (Active),
   `SMTP.SendAsApp` Application 권한 부여 + admin consent 완료, 발신 mailbox 에
   대한 SendAs 권한 부여. 회사 Conditional Access 정책에서 본 app 이 차단되어
   있지 않은지도 확인.

#### 참고 메모: 다른 transport 옵션은?

> **참고**: 회사 M365 정책상 OAuth 경로가 막힌 환경이라면, port 25 + IP 기반
> inbound connector (옵션 A) 도 기술적으로는 가능합니다. 자세한 비교는
> [`docs/email_transport_options.md`](docs/email_transport_options.md) 참조.
> 현재 구현은 옵션 B (M365 OAuth XOAUTH2) 단독이며, 옵션 A 는 spoofing 위험 +
> 회사 IT 정책상 IP 단독 허용 불가로 폐기되었습니다.

#### Daily Report (단체 자동 발송)

「메일 발송」 페이지의 「메일 발송 설정」 카드 아래 「Daily Report」 카드에서, 운영자가
cron 으로 예약한 시각마다 "마지막 발송 이후 누적된 공고 변화"를 정리한 메일을 관리자
전원에게 자동 발송한다. 수신자 명단은 `is_admin=True` 사용자의 이메일을 **자동 수집**
하므로 별도 입력란이 없다 (이메일 미설정·이메일 알림 거부 admin 은 자동 제외 + 카드에
경고로 표시).

> **「메일 발송 설정」 의 활성화 토글이 꺼져 있으면 Daily Report 도 발송되지 않는다.**
> 자동 잡·테스트 발송·지금 발송 모두 같은 게이트를 통과해야 한다 (게이트 OFF 시 503).

##### 활성화 / cron 작성법

1. **Daily Report 활성화** 체크박스 ON.
2. **Cron 표현식** 입력 — 5필드 cron, **KST 기준**으로 해석된다. 예:
   - `0 9 * * 1-5` — 평일 매일 09:00 (기본값)
   - `30 8 * * *` — 매일 08:30
   - `*/5 * * * *` — 5분마다 (검증용. 확인 후 반드시 운영값으로 복원)
3. **저장** → `scheduled_jobs` 싱글턴에 반영되고 crontab 이 재설치되며 「다음 실행 예측」 에 다음 발화 시각이 표시된다.

- 잘못된 cron 표현식은 저장 시 거부된다 (422 + 원인 안내).
- 활성화 ON 인데 cron 이 비어 있으면 거부된다 — 의도적으로 끄려면 활성화 OFF 로 저장.
- 잡(cron/enabled 트리거)은 DB `scheduled_jobs` 테이블(스케줄 SSOT)의 `daily_report` 싱글턴에 저장되어 **재기동에도 자동 복원**된다 (기동 시 crontab 재생성).

누적 구간은 항상 `(마지막 발송 시각, 지금]`. 첫 발송(마지막 발송 시각 미설정)은 직전
7일치 snapshot 을 대상으로 한다. 구간 내 snapshot 변화가 0건이면 발송을 건너뛰고
(상태 `skipped`) 마지막 발송 시각을 그대로 둬, 다음 발송이 같은 구간 + 신규 누적까지
한 번에 처리한다 — cron 패턴이 어떻든 빠지는 변화 없이 누적된다.

##### 테스트 발송 / 지금 발송

- **테스트 발송**: 「받는 사람」 에 임의 주소 1개를 입력하고 [테스트 발송]. 입력 주소는
  [저장] 으로 기억된다 (다음에 비워 두면 저장된 주소로 발송). 본 발송과 동일한 누적
  구간 계산을 거치지만 **마지막 발송 시각을 갱신하지 않아** 실제 발송 주기를 망가뜨리지
  않는다.
- **지금 admin 에게만 발송**: 스케줄과 무관하게 즉시 관리자 전원에게 발송한다. confirm
  다이얼로그로 수신자 명단을 확인한 뒤 진행된다. 정상 발송이면 마지막 발송 시각이
  갱신되어 다음 자동 발송 구간이 이어진다.

##### 발송 이력 보는 법

페이지 하단 「Daily Report 발송 이력」 테이블에서 각 실행을 확인한다. 컬럼은 시각(KST) /
트리거(예약 · 지금발송 · 테스트) / 상태 / 누적 구간 / snapshot 수 / 성공·실패 수.

상태 아이콘: ✅ 성공 · ⚠️ 부분 성공 · ❌ 실패 · ⏭ 건너뜀(snapshot 0건) · ⏳ 진행 중.

행을 클릭하면 펼쳐져 수신자별 발송 결과(받는 사람 / 성공·실패 / 시도 횟수 / 에러
메시지)가 표시된다 — 일부 또는 전체가 실패한 경우 이 펼침 화면의 에러 메시지에서
원인을 확인한다 (메일 설정 자체 문제는 위 [디버깅 순서](#디버깅-순서-테스트-발송-실패-시) 참조).

##### 트러블슈팅 — 마지막 발송 시각 수동 reset

발송 구간이 꼬였거나(예: 잘못된 시각이 저장됨) 다시 직전 7일치부터 보내고 싶을 때는
`email.daily_report.last_sent_at` 설정값을 비우면 다음 발송이 첫 발송으로 처리된다.

```bash
# 마지막 발송 시각을 비움 → 다음 발송은 직전 7일치(첫 발송 fallback)로 처리
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE system_settings SET value = '' WHERE key = 'email.daily_report.last_sent_at';"

# 특정 시각으로 직접 지정 (ISO-8601 UTC) — 그 시각 이후 누적만 발송하고 싶을 때
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE system_settings SET value = '2026-05-20T00:00:00+00:00'
   WHERE key = 'email.daily_report.last_sent_at';"

# 현재 저장된 값 확인
sqlite3 ./data/db/app.sqlite3 \
  "SELECT value FROM system_settings WHERE key = 'email.daily_report.last_sent_at';"
```

값은 ISO-8601 UTC 문자열이다. 빈 값 · 행 없음 · 파싱 불가 값은 모두 "첫 발송 전" 으로
간주되어 직전 7일치 fallback 이 적용된다. 변경은 다음 발송부터 반영된다.

##### 트러블슈팅 — 0건 발송 / snapshot 0건 점검

발송 이력에 ⏭ (snapshot 0건) 으로 표시되거나, 메일이 실제로 나갔어도 본문이 "변화
0건" 으로 발송된 경우 점검 순서:

1. **메일 발송 게이트** — `/admin/email` 「메일 발송 설정」 카드의 **활성화 토글** 이 ON
   인지 확인. OFF 면 자동 잡·테스트·지금 발송 모두 503 으로 막힌다 (별도 발송 실패
   로그도 안 남는다).
2. **누적 구간 (`last_sent_at`)** — 페이지 하단 발송 이력의 「누적 구간」 컬럼이 의도한
   범위를 가리키는지 확인. 너무 좁은 구간이면 위 [마지막 발송 시각 수동
   reset](#트러블슈팅-마지막-발송-시각-수동-reset) 절차로 비우거나 명시 지정.
3. **그 구간 안에 snapshot 이 실제로 존재하는가** — `scrape_snapshots.created_at` 기준
   으로 조회한다. `snapshot_date` 가 같은 날이어도 `created_at` 이 구간 밖이면 0 건으로
   잡힌다 (각 row 는 그 ScrapeRun 종료 시각으로 박혀 있다, 후속 ScrapeRun 이 들어와도
   기존 row 의 `created_at` 은 갱신되지 않는다).

   ```sql
   -- 발송 이력의 누적 구간 (UTC) 안에 잡힌 snapshot row 수동 확인
   SELECT id, scrape_run_id, snapshot_date, created_at,
          json_extract(payload, '$.counts') AS counts
     FROM scrape_snapshots
    WHERE created_at >  '2026-05-26T05:40:00+00:00'  -- last_sent_at (KST 14:40 → UTC 05:40)
      AND created_at <= '2026-05-26T07:10:00+00:00'  -- now           (KST 16:10 → UTC 07:10)
    ORDER BY created_at ASC;
   ```

4. **수신자 풀** — 발송 시점에 `is_admin=True` 이며 이메일이 등록되어 있고 이메일 알림
   토글이 ON 인 사용자가 1 명 이상인지. `/admin/email` 「Daily Report」 카드의 수신자
   미리보기에 경고가 떠 있으면 거기에 원인이 표시된다.
5. **「테스트 발송」 으로 본 발송 흐름 재현** — 위 1-4 에 문제가 없는데 0 건이 계속
   잡히면 「Daily Report」 카드 하단의 [테스트 발송] 으로 동일한 누적 구간 계산을
   재현해 본다. `last_sent_at` 을 갱신하지 않으므로 실제 발송 주기에는 영향 없다.

---

## 로그와 디버깅

### LOG_LEVEL 가이드

`.env` 의 `LOG_LEVEL` 하나로 웹·스크래퍼 컨테이너의 상세도가 일괄 조정된다.
loguru ↔ stdlib 브리지가 부착돼 있어 uvicorn / starlette / fastapi / sqlalchemy /
alembic 로그가 모두 같은 형식·같은 sink 로 흘러온다.

| LOG_LEVEL | 용도 | 보이는 것 |
|-----------|------|-----------|
| `INFO` (기본) | 평소 운영 | 로그인 / HTTP 요청 1줄 / 수집 진행 요약 / 경고 / 에러 |
| `DEBUG` | 500 에러·인증 실패·수집 이상 추적 | INFO 전부 + 인증 분기·세션 검증·관리자 가드·DB 세션 open/commit/close·라우트 진입 |
| `WARNING` | 로그 양 최소화 | 경고·에러만 |

변경 후 컨테이너 재기동 필요:

```bash
sed -i 's/^LOG_LEVEL=.*/LOG_LEVEL=DEBUG/' .env
docker compose restart app
docker logs iris-agent-web 2>&1 | head -3
# → ... 로깅 초기화 완료: log_level=DEBUG diagnose=True stdlib_bridge=installed
```

### 로그 포맷 읽기

```
2026-04-23 16:38:20.145 | INFO     | req=Q4H8VYJO... | app.auth.routes:login_submit:259 - 로그인 성공: user_id=1
└──────timestamp───────┘ └─level──┘ └─request_id──┘ └─────module:function:line──────┘ └─메시지─┘
```

- timestamp: KST.
- req=...: HTTP 요청마다 발급되는 12자 ID. 같은 req 의 로그는 같은 요청 컨텍스트.
  요청 밖(기동·스케줄러·CLI) 은 `req=-`.
- module:function:line: 로그를 찍은 코드 위치. stdlib 브리지가 원 호출자 위치를 복원.

### 요청 단위 추적

500 에러 진단 시:

```bash
# 1) 미처리 예외 로그에서 req 값 추출
docker logs iris-agent-web 2>&1 | grep "미처리 예외"

# 2) 같은 req 의 전 흐름을 잘라내기 (진입 → 인증 → DB → 응답)
docker logs iris-agent-web 2>&1 | grep "req=9d4ecf87a930"
```

### 500 에러 진단 체크리스트 (LOG_LEVEL=DEBUG 전제)

순서대로 찍히는지 보면 어디서 멈췄는지 알 수 있다.

| 순서 | 로그 샘플 | 의미 |
|------|----------|------|
| 1 | `request 진입: method=GET path=/admin/scrape ...` | 미들웨어 통과 |
| 2 | `auth DB 세션 open` | 인증 의존성용 세션 |
| 3 | `current_user_optional: 세션 검증 성공 user_id=...` | 쿠키/세션 통과 |
| 4 | `admin_user_required: 통과 user_id=...` | 관리자 가드 통과 |
| 5 | `admin.scrape_control_page 진입: ...` | 라우트 본체 진입 |
| 6 | `session_scope open` → `commit` → `close` | 라우트가 여는 DB 트랜잭션 |
| 7 | `request 완료: ... status=200 duration_ms=...` | 응답 송출 |
| 예외 | `request 실패(예외 전파): ...` + stack trace | 라우트 또는 하위 계층에서 예외 |

`admin_user_required: 비관리자 로그인 → 403` 이 보이면 사용자 `is_admin` 확인.
라우트 진입까지만 찍히고 DB 조회 완료가 없다면 그 사이 SQL 에서 예외 (stack trace 가
바로 따라 찍힘).

> **DEBUG 시 주의**: `diagnose=True` 가 함께 켜져 traceback 에 로컬 변수 값이 inline
> 으로 노출된다 — 세션 토큰·비밀번호가 stderr 로 흘러나갈 수 있어 운영에서는 오래
> 켜 두지 않는다. raw SQL 전체를 보려면 `app/db/session.py` 의 `_build_engine` 에서
> `echo=True` (운영 권장 X — 로그가 수천 줄로 늘어남).

### 정상 수집 시 주요 로그

```
INFO  목록 수집 시작: source=IRIS max_pages=10
INFO  목록 수집 완료: source=IRIS 42건
INFO  소스 IRIS 완료(수집 단계): delta INSERT 성공 42건 / 실패 0건 ...
INFO  apply_delta_to_main 완료: actions={'created': 5, 'unchanged': 37} ...
INFO  ScrapeSnapshot INSERT: scrape_run_id=88 snapshot_date=2026-04-29 counts={'new': 5, ...}
INFO  apply_delta_to_main 트랜잭션 commit 완료: status=completed
```

- 2회차 이후 `unchanged=N` 이 전체 공고 수와 가까울수록 정상 (증분 수집).
- 수집 단계와 apply 단계는 다른 트랜잭션이라 키가 분리된다 — `actions={...}` 가
  사용자 입장의 "본 테이블 변화".
- `apply_delta_to_main 트랜잭션 commit 완료` = 본 테이블 + snapshot + delta 비움이
  모두 영구화된 시점.

### 주의가 필요한 로그

| 로그 | 의미 | 대응 |
|------|------|------|
| `WARNING 중단 요청 감지 — 남은 공고 N건 스킵` | SIGTERM 수신, 다음 경계에서 종료 | 정상. status='cancelled' + delta 비움 자동 |
| `WARNING 수집 중단(cancelled) — apply 건너뜀 + delta 비움` | cancelled 분기, 본 테이블 미반영 | 정상 |
| `EXCEPTION apply_delta_to_main 트랜잭션 실패 — delta 보존` | apply 도중 예외 → auto-rollback | 다음 ScrapeRun 으로 자동 복구 |
| `WARNING detail_url 없음 — 상세 수집 스킵` | 목록에서 상세 URL 추출 실패 | 해당 소스 HTML 구조 변경 여부 확인 |
| `ERROR delta INSERT 실패` | 환경 문제 (권한·디스크 등) | 로그 확인 후 재실행 |
| `INFO 상태 전이 — in-place 갱신` | 같은 공고가 다른 상태로 재등장 | 정상. 비정상적으로 잦으면 `docs/status_transition_todo.md` |
| `INFO apply 2차 감지 — 첨부 변경으로 버전 갱신` | 첨부 sha256 차이 감지 | 정상 |
| `WARNING GC 거부 — ScrapeRun id=N 가 'running' 입니다` | 수집 중 GC 시도 | 종료 후 재실행 |

---

## 트러블슈팅

증상 → 원인 → 조치 순서로 정리.

### 환경·기동

| 증상 | 원인 / 조치 |
|------|------------|
| `Permission denied on /var/run/docker.sock` (수동 시작 클릭 시) | `HOST_DOCKER_GID` 가 빠지거나 잘못된 값. `getent group docker | cut -d: -f3` 결과로 `.env` 갱신 → `./run_compose.sh up` 재기동 |
| `HOST_PROJECT_DIR 가 설정되지 않았습니다` (수집 실패 / app 컨테이너 재시작 반복) | `.env` 의 `HOST_PROJECT_DIR` 미설정 → `pwd` 결과를 `.env` 에 추가. **`.env` 에 값이 있는데도 에러가 난다면** app 컨테이너가 이 값을 빈 상태로 생성된 것 — `env_file` 은 컨테이너 **생성 시점** 에 1회만 평가되므로 `.env` 수정만으로는 반영되지 않는다. `./run_compose.sh down && ./run_compose.sh up` 으로 컨테이너를 재생성해야 `env_file` 의 `.env` 값이 다시 주입된다. (task 00143 부터 빈/미설정 상태에서는 app 부팅이 startup 단계에서 fail-fast 로 중단된다 — `docker logs iris-agent-web` 에 CRITICAL 로그가 남고 컨테이너가 재시작 루프에 빠지므로, 수집 이력에서 뒤늦게 발견되던 silent-failure 가 사라졌다.) |
| `./data/` 하위 파일이 root:root 소유 | `HOST_UID`/`HOST_GID` 빠진 채 기동된 적이 있음. `sudo chown -R "$(id -u):$(id -g)" ./data/` + `.env` 보강 + 재빌드 |
| 컨테이너 안에서 사용자명이 `I have no name!` | UID 변경 후 재빌드 안 됨. `docker compose build` 다시 |
| 코드 변경이 자동 반영 안 됨 | 컨테이너가 실행 중인지 확인. `./run_compose.sh logs -f app` 으로 uvicorn reload 로그 확인 |
| 기동 시 `sources.yaml 마운트 없음 — template 기본값으로 기동합니다` 경고 | 호스트에 `sources.yaml` 부재. `sh ./bootstrap_sources.sh` 후 편집 → `docker compose restart app` |

> **참고 — `docker-compose.yml` 의 `./.env:${HOST_PROJECT_DIR}/.env:ro` 마운트.**
> app 서비스의 `.env` 마운트 타겟이 흔한 `/app/.env` 가 아니라
> `${HOST_PROJECT_DIR}/.env` 인 것은 의도된 정상 설정이다. app 컨테이너는
> 호스트 docker.sock 으로 inner `docker compose` 를 호출하면서
> `--project-directory $HOST_PROJECT_DIR` 를 명시하는데, compose 는 변수
> 보간용 `.env` 를 이 project-directory 기준으로 찾고 그 탐색은 app 컨테이너
> **내부 파일시스템** 에서 일어난다. 따라서 호스트 `.env` 를 컨테이너 안의
> `$HOST_PROJECT_DIR/.env` 경로에 그대로 노출해야 inner compose 의 변수
> 보간(`${HOST_UID}`·`${HOST_GID}` 등)이 성공한다. `/app/.env` 로 바꾸면
> inner compose 가 `.env` 를 찾지 못해 수집 컨테이너 기동이 깨진다. 자세한
> 근거는 [`docs/scrape_control_design.md` §5.3](docs/scrape_control_design.md) 참조.

### 수집

| 증상 | 원인 / 조치 |
|------|------------|
| 공고 목록이 비어 있음 | 1) `scrape.log_level: DEBUG, max_pages: 1` 로 한 번 실행 → `목록 수집 완료: ... 0건` 이면 사이트 응답 이상. 2) DB 에 데이터는 있는데 안 보이면 `is_current=1` 로 SELECT 해 확인 |
| 재실행해도 매번 상세 수집 발생 | `actions={...}` 의 `unchanged=0` 에 가깝다면 변경 감지 오동작. `detail_fetched_at` NULL row / 비교 4 필드 (title·status·deadline_at·agency) 가 매번 다른지 확인 |
| 첨부 다운로드 실패 | 1) 로그의 `첨부 수집` 라인 확인. 2) `raw_metadata.attachment_errors` SELECT 로 원인 키 확인. 3) (IRIS 만) Playwright 미설치 — `docker compose build` 의 `playwright install chromium --with-deps` 스텝 확인. 4) `scrape.skip_attachments: false` 로 재시도 |
| NTIS 목록 0건 수집 | `--log-level DEBUG` 로 재실행 → `totalCount` 파싱 실패면 NTIS HTML 구조 변경. 브라우저로 `https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do` 직접 접근해 사이트 점검 여부 확인 |
| canonical 승급이 안 됨 (NTIS) | 상세 수집 성공(`detail_fetch_status='ok'`) 후에도 `canonical 재계산 완료` 로그가 없다면 개별공고이거나 본문 공고번호 파싱 실패. `--log-level DEBUG` 로 `ntis_ancm_no` 값 확인. fuzzy 그룹으로 남는 것은 정상이며 cross-source 매칭만 약해진다 |
| 첨부 다운로드 후 `data/downloads/` 에 고아 파일 | apply 실패 또는 attachments 행 수동 삭제 흔적. `gc_orphan_attachments.py --dry-run` → 검수 → 실제 삭제 |
| `상태 전이 — in-place 갱신` 로그가 비정상적으로 많음 | 매 실행마다 같은 공고가 계속 상태 전이로 잡히면 `docs/status_transition_todo.md` 참고 |

### 웹 / DB

| 증상 | 원인 / 조치 |
|------|------------|
| admin 페이지 500 인데 docker logs 비어 있음 | `LOG_LEVEL` 이 `CRITICAL` 이상이거나 옛 이미지가 떠 있음. 1) 기동 첫 줄에 `로깅 초기화 완료: ... stdlib_bridge=installed` 확인 (없으면 `docker compose build` 후 재기동). 2) `docker logs -f iris-agent-web` 실시간 follow 로 다시 요청. 3) `LOG_LEVEL=DEBUG` 로 [500 에러 체크리스트](#500-에러-진단-체크리스트-log_leveldebug-전제) 적용 |
| DB 스키마 오류 (`no such column: is_current` 등) | Alembic 미적용. `docker compose run --rm scraper alembic upgrade head`. 그래도 안 되면 백업 후 DB 삭제·재생성 |
| `/attachments/{id}/download` 가 404 | `stored_path` 가 가리키는 파일이 실제로 없음. 스크래퍼 재실행 또는 DB 정리 |
| 웹 UI 페이지가 깨짐 (CSS/JS 깨짐) | 외부 CDN 미사용 — 로컬 정적 파일 캐시 무효화 필요. 브라우저 캐시 강제 새로고침 (Ctrl+Shift+R) |
| Chart.js 미로드 (대시보드 추이 차트 안 보임) | `app/web/static/vendor/chart.min.js` 누락. NOTICE 의 출처 URL 에서 다시 받아 vendor 디렉터리에 둠 |
| 캘린더에 클릭 가능 날짜 0 | ScrapeRun 이 한 번도 completed/partial 로 끝나지 않음. 수집 1회 돌리면 활성화 |
| 비교일이 자동 fallback 됐다는 노란 안내 | 정상 동작. 비교일 snapshot 부재 시 가장 가까운 이전 snapshot 으로 자동 대체 |

---

## 정기 운영 체크리스트

### 매 수집 후

- [ ] 종료 코드 0 확인 (`echo $?`)
- [ ] `apply_delta_to_main 트랜잭션 commit 완료` 로그 — 본 테이블 / snapshot 영구화 시점
- [ ] `delta INSERT 실패` / `첨부 다운로드 실패` 건수 = 0 확인
- [ ] delta 잔여 0 확인:
      `sqlite3 ./data/db/app.sqlite3 "SELECT COUNT(*) FROM delta_announcements;"`
- [ ] 웹 UI 에서 최신 공고 표시 확인

### 주간

- [ ] DB 파일 크기 (`ls -lh ./data/db/app.sqlite3`)
- [ ] 구버전 row 누적 (`SELECT COUNT(*) FROM announcements WHERE is_current=0;`)
- [ ] 반복 `WARNING` 메시지 부재 확인
- [ ] 고아 첨부 파일 — `gc_orphan_attachments.py --dry-run` 검토 또는 자동 cron
      (`add_gc_orphan_cron_schedule`) 등록 여부 확인
- [ ] DB 백업 보관 개수 (기본 14개) — `ls -lh ./data/backups/`

### 업데이트 후

- [ ] `./run_compose.sh build` 로 이미지 재빌드
- [ ] `docker compose run --rm scraper alembic current` 가 `head` 인지 확인
- [ ] `scrape.dry_run: true, max_pages: 1` 로 한 번 돌려 기본 동작 확인
- [ ] 웹 UI `/` 정상 렌더링 확인
- [ ] 관리자 로그인 후 `/admin/scrape` 진입 가능 확인 (라우터 가드 회귀 점검)
