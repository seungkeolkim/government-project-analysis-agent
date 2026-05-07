# 시스템 관리자 운영 가이드

> 일상 운영·트러블슈팅 중심 문서. 프로젝트 개요·설치 흐름은 [README.md](README.md) 를,
> 아키텍처 결정 근거는 [PROJECT_NOTES.md](PROJECT_NOTES.md) 를 참고한다.
>
> **Docker 전용.** 호스트에서 `python -m app.cli` 직접 실행은 지원하지 않는다.
> compose 호출은 모두 wrapper `./compose.sh <dev|prod>` 를 거친다.

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
./compose.sh dev build
./compose.sh dev up app
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
./compose.sh dev build
./compose.sh dev up app
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
docker compose run --rm app python scripts/python/create_admin.py

# username + email 인자, password 만 prompt
docker compose run --rm app python scripts/python/create_admin.py root_user --email admin@example.com
```

정책: username 영문 소문자/숫자/밑줄 3~64자, password 8자 이상. bcrypt(rounds=12) 해시
저장. 같은 username 이 이미 있으면 종료 코드 1.

확인:

```bash
sqlite3 ./data/db/app.sqlite3 "SELECT id, username, is_admin FROM users WHERE is_admin = 1;"
```

### dev / prod 모드 (compose.sh)

| 모드   | 사용 compose 파일                                    | 동작                                                   |
| ------ | ---------------------------------------------------- | ------------------------------------------------------ |
| `dev`  | `docker-compose.yml` + `docker-compose.dev.yml`      | uvicorn `--reload` 활성. `./app/` 바인드 마운트로 코드 변경 자동 반영 |
| `prod` | `docker-compose.yml` 단독                            | 이미지 코드 고정, reloader 끔                          |

`./compose.sh <mode>` 뒤의 인자는 그대로 `docker compose` 에 전달된다. 직접 `docker
compose` 명령은 모드 조합 오용을 막기 위해 사용하지 말 것.

### 운영 스크립트 위치

- 사용자 실행 shell: 프로젝트 루트의 `./compose.sh`, `./bootstrap_sources.sh`.
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

`/admin/schedule` 탭에서 cron 표현식 또는 매 N시간 간단 모드로 등록. APScheduler
잡은 SQLite `scheduler_jobs` 테이블에 저장되므로 **웹 재기동에도 자동 복원**된다.

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
본 테이블 4-branch 적용 + (b) `scrape_snapshots` UPSERT(KST 날짜당 1 row) + (c) 같은
scrape_run_id 의 delta DELETE 가 한 번에 일어난다. **본 테이블·snapshot 은 트랜잭션
commit 시점에만** 변한다 — 도중 SIGTERM·예외가 새지 않는다.

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
SELECT snapshot_date, payload FROM scrape_snapshots ORDER BY snapshot_date DESC LIMIT 1;
SELECT scrape_run_id, COUNT(*) FROM delta_announcements GROUP BY scrape_run_id;  -- 정상이면 0
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

로그인 사용자는 공고(canonical 과제) 단위로 **관련 / 무관** 판정을 남길 수 있다. 배지
색: 관련(초록), 무관(회색), 미검토(테두리만).

배지 클릭 → 모달 → 라디오 선택 + (선택) 사유 입력 → **저장**. 페이지 reload 없이 즉시
반영. 판정 변경 시 이전 판정은 자동으로 이력으로 이관된다 (덮어쓰기 아님). 다른
사용자의 판정은 본인 배지 색에 영향 없음 — hover 툴팁에만 노출.

#### 조직 단위 판정 (task 00085)

같은 과제에 대해 **개인 입장** 외에 **조직 입장**으로도 판정을 남길 수 있다. 모달의
"판정 주체" 라디오에서 **개인 / 조직** 을 고르고, 조직을 고르면 본인이 속한 조직 중에서
선택한다. UNIQUE 키는 `(canonical, user, organization_id)` 단일 키이므로 같은 사용자가
같은 과제에 대해 **개인 row 1 개 + 본인이 속한 각 조직마다 row 1 개**까지 가질 수
있다 (조직별 의견이 분기해도 그대로 데이터에 표현된다).

**모달 동작 — 소속 케이스별**

- **무소속**: 조직 라디오 자체 비활성 + tooltip "소속된 조직이 없습니다." 안내.
- **단일 조직 소속**: 조직 라디오 라벨에 조직명이 직접 표시됨 (드롭다운 없음).
- **복수 조직 소속**: 조직 라디오 선택 시 드롭다운으로 조직 선택.

**큰 배지 표시 우선순위**: 본인 개인 row 가 있으면 그 verdict 가 큰 배지로,
없으면 본인이 만든 조직 row 중 가장 최근 것 (`decided_at` DESC). 둘 다 없으면 미검토.
hover 툴팁에는 본인이 만든 조직 row 가 모두 나열된다 (조직명 + verdict + 사유).

**카운터 (`✅ N ❌ M ❓ K`)**: 본인 row (개인 + 본인 조직 row 모두) 는 제외하고, 다른
사용자·조직의 모든 row 가 카운트 대상이다 (verdict 별 분리). 같은 조직 안에 여러 사용자
row 가 있으면 각각 1 카운트 — 조직 의견 분기 자체가 카운트에 그대로 반영된다.

**권한**: 본인이 만든 row 만 본인이 수정·삭제할 수 있다. 같은 조직 동료가 만든 row 는
본인이 건드릴 수 없으며, 본인이 같은 조직 입장으로 새 row 를 만들면 트리플 키가 달라
별개의 row 로 공존한다 (충돌 처리 없음).

**상세 페이지 풀어 표시**: 공고 상세 하단의 "관련성 판정" 섹션에 (1) 본인 판정 (개인 +
본인 조직) + 본인 row 마다 [수정][삭제] 버튼, (2) 다른 사용자·조직 판정 — 작성자 / 조직명 /
verdict / 시점 / 사유 가 행으로 풀어 표시된다.

**비로그인 노출**: 로그인 사용자와 **동일** — 카운터·OTHERS 행·조직명 모두 그대로
보인다. 단, 본인 작성·수정·삭제 영역은 비활성 (배지가 readonly span 으로 렌더되어
클릭 불가). 로그인 후에 라벨링이 가능하다.

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
INFO  ScrapeSnapshot 신규 INSERT: snapshot_date=2026-04-29 counts={'new': 5, ...}
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
| `Permission denied on /var/run/docker.sock` (수동 시작 클릭 시) | `HOST_DOCKER_GID` 가 빠지거나 잘못된 값. `getent group docker | cut -d: -f3` 결과로 `.env` 갱신 → `./compose.sh dev up app` 재기동 |
| `ComposeEnvironmentError` flash | `HOST_PROJECT_DIR` 미설정. `pwd` 결과를 `.env` 에 추가 |
| `./data/` 하위 파일이 root:root 소유 | `HOST_UID`/`HOST_GID` 빠진 채 기동된 적이 있음. `sudo chown -R "$(id -u):$(id -g)" ./data/` + `.env` 보강 + 재빌드 |
| 컨테이너 안에서 사용자명이 `I have no name!` | UID 변경 후 재빌드 안 됨. `docker compose build` 다시 |
| dev 모드인데 코드 변경 자동 반영 안 됨 | `./compose.sh dev` 가 아닌 `prod` 로 떠 있음 / 이전 prod 컨테이너 잔존. `./compose.sh dev down` 후 `./compose.sh dev up app` |
| 기동 시 `sources.yaml 마운트 없음 — template 기본값으로 기동합니다` 경고 | 호스트에 `sources.yaml` 부재. `sh ./bootstrap_sources.sh` 후 편집 → `docker compose restart app` |

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

- [ ] `./compose.sh dev build` 또는 `prod build` 로 이미지 재빌드
- [ ] `docker compose run --rm scraper alembic current` 가 `head` 인지 확인
- [ ] `scrape.dry_run: true, max_pages: 1` 로 한 번 돌려 기본 동작 확인
- [ ] 웹 UI `/` 정상 렌더링 확인
- [ ] 관리자 로그인 후 `/admin/scrape` 진입 가능 확인 (라우터 가드 회귀 점검)
