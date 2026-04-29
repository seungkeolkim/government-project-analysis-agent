# 시스템 관리자 운영 가이드

> 이 문서는 **일상 운영·트러블슈팅** 중심이다.
> 프로젝트 개요·설치 방법은 [README.md](README.md) 를 참고한다.
>
> **Docker 전용.** 호스트에서 `python -m app.cli` 를 직접 실행하는 방식은 지원하지 않는다.

---

## 목차

1. [초기 설치 요약](#초기-설치-요약)
2. [컨테이너 실행 유저 설정 (파일 owner 제어)](#컨테이너-실행-유저-설정-파일-owner-제어)
3. [첫 관리자 계정 생성](#첫-관리자-계정-생성)
4. [스크래퍼 실행 방법](#스크래퍼-실행-방법)
5. [웹 기반 수집 제어](#웹-기반-수집-제어)
6. [NTIS 수집 운영 특이사항](#ntis-수집-운영-특이사항)
7. [수집 파이프라인 동작 (증분 + delta + snapshot)](#수집-파이프라인-동작-증분--delta--snapshot)
8. [웹 UI 검색/필터/중복 그룹 보기](#웹-ui-검색필터중복-그룹-보기)
9. [관련성 판정 사용법](#관련성-판정-사용법)
10. [읽음 bulk 사용법](#읽음-bulk-사용법)
11. [즐겨찾기 / 동일 과제 사용법](#즐겨찾기--동일-과제-사용법)
12. [대시보드 사용법 (Phase 5b / task 00042)](#대시보드-사용법-phase-5b--task-00042)
13. [로그 해석](#로그-해석)
14. [웹 로그 레벨 제어와 원인 추적 (00030)](#웹-로그-레벨-제어와-원인-추적-00030)
15. [DB 관리](#db-관리)
16. [트러블슈팅](#트러블슈팅)
17. [정기 운영 체크리스트](#정기-운영-체크리스트)

---

## 초기 설치 요약

```bash
# 1) 환경변수 파일 생성
cp .env.example .env
# 컨테이너 실행 유저 설정 — 생성 파일 owner 를 호스트 유저로 맞추기 위해 필수 (§2)
echo "HOST_UID=$(id -u)" >> .env
echo "HOST_GID=$(id -g)" >> .env
# 필요 시 .env 편집 (DB_URL, REQUEST_DELAY_SEC 등)

# 2) sources.yaml 생성 (template 에서 복사, 이미 존재하면 덮어쓰지 않음)
sh scripts/bootstrap_sources.sh
# 필요 시 sources.yaml 편집 (수집 소스·페이지 수 등)
# sources.yaml 은 .gitignore 대상 — 브랜치 전환 시 로컬 수정이 보존된다

# 3) 이미지 빌드
docker compose build

# 4) 웹 UI 기동
docker compose up app
# → http://localhost:8000 접속
```

---

## 컨테이너 실행 유저 설정 (파일 owner 제어)

컨테이너가 `./data` 바인드 마운트에 생성하는 모든 파일(SQLite DB·다운로드·로그·백업 등)의
owner 가 **docker compose 를 실행하는 호스트 유저**가 되도록 `.env` 에 UID/GID 를 설정한다.
이 설정이 없으면 컨테이너가 root(uid=0) 로 실행되어 `./data/` 하위 파일이 `root:root`
소유로 생성된다 — 호스트에서 수정·삭제 시 `sudo` 가 필요해진다.

### 최초 설정

```bash
# 호스트 쉘에서 현재 유저의 UID/GID 를 .env 에 추가한다.
echo "HOST_UID=$(id -u)" >> .env
echo "HOST_GID=$(id -g)" >> .env
```

설정 후 이미지를 재빌드한 뒤 서비스를 기동한다
(`docker-compose.yml` 이 이미지 안에 COPY 되므로 재빌드가 필요하다):

```bash
docker compose build
docker compose up app
```

> **fallback 없음 — 반드시 명시 설정.** HOST_UID/HOST_GID 가 `.env` 에 없으면
> compose 가 `user: ":"` 로 해석해 기동이 실패하거나 root 로 실행된다.
> UID 자동 fallback(예: 1000)은 없다. macOS, 다중 계정 리눅스 등 UID 가
> 1000 이 아닌 환경에서도 반드시 `id -u` / `id -g` 값을 직접 입력해야 한다.

### 기존 root-owned 파일 복구 (일회성)

이 변경을 적용하기 이전에 컨테이너를 실행한 경우 `./data/` 하위 일부 파일이
`root:root` 소유로 남아 있을 수 있다. **먼저 현재 상태를 확인**한다:

```bash
ls -la ./data/
ls -la ./data/db/
ls -la ./data/logs/
ls -la ./data/downloads/
```

소유자가 `root` 인 항목이 있으면 아래 명령으로 호스트 유저 소유로 되돌린다
(`sudo` 가 필요하다):

```bash
sudo chown -R "$(id -u):$(id -g)" ./data/
```

복구 후 owner 가 호스트 유저로 바뀌었는지 다시 확인한다:

```bash
ls -la ./data/
```

> **복구 범위.** `./data/` 전체를 재귀 변경하므로 `data/backups/` · `data/downloads/` ·
> `data/logs/` 가 모두 포함된다. 특정 항목만 변경하려면 경로를 좁혀 실행한다.

### 서버 시간대 KST 설정 (Asia/Seoul)

본 프로젝트는 **KST 단일 운영** 을 전제한다 — 화면·로그·cron 의 모든 사용자
표시 / 평가 시각이 Asia/Seoul 기준이다. DB 저장은 **UTC tz-aware 유지** 컨벤션
이며, 표시·계산·입력·cron 의 사용 경계에서만 KST 변환이 적용된다 (task 00040).

#### 코드 레벨 KST 적용 (호스트 TZ 비의존)

다음 위치는 `app.timezone.KST` (= `ZoneInfo(\"Asia/Seoul\")`) 를 명시적으로
사용하므로, 컨테이너 `TZ` env 나 호스트 `/etc/localtime` 이 빠져 있어도 동작한다:

- **Jinja2 표시**: `kst_format` / `kst_date` 필터가 모든 timestamp 출력을 KST 로
  변환 (`app/web/template_filters.py`).
- **APScheduler cron**: `BackgroundScheduler(timezone=KST)` + `CronTrigger`
  / `IntervalTrigger` 모두 KST. 예) `0 3 * * *` = **매일 KST 03:00** (= UTC
  18:00 의 전날). `30 9 * * *` = **매일 KST 09:30**.
- **loguru 로그**: sink format 의 timestamp 가 KST suffix 와 함께 명시 출력 —
  `2026-04-28 09:30:45.123 KST | INFO | ...`.
- **외부 응답 파싱**: IRIS / NTIS 의 마감일·접수시작 텍스트는 KST 가정으로
  파싱 → UTC 로 변환 저장 (`app/cli.py::_parse_datetime_text`).

#### 호스트 환경 권장 설정 (선택)

코드 레벨 KST 가 이미 일관되므로 호스트 / 컨테이너 tz 설정은 **운영자 편의**
용도다. 권장 이유는 docker logs 외부의 OS 로그(`journalctl`, `syslog`) 와
시각이 일치해 트러블슈팅이 쉬워진다는 점.

- **컨테이너 `TZ` env**: `docker-compose.yml` 이 `app`·`scraper` 두 서비스에
  `TZ=${HOST_TZ:-Asia/Seoul}` 와 `/etc/localtime:/etc/localtime:ro` 바인드
  마운트를 주입한다 (clone 후 `docker compose up` 즉시 적용).
- **호스트 시계**: Linux 의 경우 `sudo timedatectl set-timezone Asia/Seoul`,
  macOS 는 시스템 환경설정 → 날짜 및 시간. `/etc/localtime` 바인드 마운트가
  호스트 tz 를 그대로 컨테이너로 전달하므로, 호스트가 KST 면 컨테이너도 KST.

#### jobstore 자동 재해석 (00040-4)

기존 스케줄러가 UTC 로 저장한 잡이 jobstore 에 남아 있어도, 웹 재기동 시
`app.scheduler.service.start()` 가 trigger.timezone 을 KST 로 자동 재해석하고
`reschedule_job` 으로 `next_run_time` 을 다시 계산한다. 운영자가 admin/schedule
탭에서 수동 재등록할 필요 없다 — 단, 자동 재해석 실패가 docker logs 의
`tz 재해석` 경고로 보이면 cron 표현식을 수동 재등록한다.

#### 다른 지역 타임존으로 운영 (예외)

코드 레벨 KST 는 고정이며 사용자별 / .env 설정으로 변경되지 않는다. 호스트
컨테이너 표시만 다른 tz 로 보고 싶다면 (운영 권장 X):

```bash
echo \"HOST_TZ=UTC\" >> .env               # 컨테이너 OS 시각만 UTC 로 회귀
echo \"HOST_TZ=Europe/Berlin\" >> .env     # 다른 IANA 존으로 오버라이드
docker compose up -d --force-recreate app
```

이 경우에도 화면·로그·cron 평가는 여전히 KST 다 (코드 레벨 고정). `date` 명령
출력만 호스트 OS tz 가 된다.

**revert 절차** (TZ env / `/etc/localtime` 마운트 모두 제거):
`docker-compose.yml` 의 두 서비스에서 `- TZ=${HOST_TZ:-Asia/Seoul}` 환경변수
라인과 `- /etc/localtime:/etc/localtime:ro` 볼륨 라인을 제거한 뒤
`docker compose up -d --force-recreate`. 화면·로그·cron 은 여전히 KST.

> **호스트 요구**: `/etc/localtime` 바인드 마운트는 Linux/macOS 의 기본 구성에
> 존재한다. 해당 경로가 없는 최소 설치 환경이라면 compose 가 기동 시 오류를
> 내므로, 볼륨 한 줄만 주석 처리하고 `HOST_TZ` 환경변수 주입만 유지한다
> (다만 python:3.12-slim 에는 tzdata 전체가 내장되어 있지 않아 이름 조회가
> 실패할 수 있어, 이때는 `docker/Dockerfile` 의 apt 설치 목록에 `tzdata` 를
> 추가하고 이미지를 재빌드한다).

#### 외부 응답 KST 가정 backfill (00040-5)

기존 IRIS / NTIS 응답이 KST 가정 적용 이전에 잘못 저장된 row 를 보정하는
일회성 스크립트가 있다. 운영 SQLite 점검 후 실행한다:

```bash
# 1) DB 백업 (운영자 책임)
docker compose run --rm app python scripts/backup_db.py

# 2) dry-run 으로 영향 범위 확인 (변경 없음)
docker compose run --rm app python scripts/backfill_kst_assumption.py

# 3) 결과를 검토한 뒤 실제 적용
docker compose run --rm app python scripts/backfill_kst_assumption.py --apply
```

스크립트는 idempotent — 두 번 실행해도 이미 KST 가정 변환된 row 는 skip 된다.

---

## 첫 관리자 계정 생성

Phase 1b 에서 자유 회원가입 + 세션 쿠키 인증이 추가되었다. 일반 사용자는
`/register` 폼에서 직접 가입할 수 있지만, **관리자(`is_admin=True`) 계정은
DB 컬럼만 존재하고 가입 폼으로는 만들 수 없다**. 운영자가 컨테이너 안에서
`scripts/create_admin.py` CLI 로 한 번 만들어 둔다.

```bash
# 대화형 (username/password/email 모두 prompt)
docker compose run --rm app python scripts/create_admin.py

# username 만 인자로 전달, 나머지는 prompt
docker compose run --rm app python scripts/create_admin.py root_user

# username + email 까지 인자로, password 만 prompt
docker compose run --rm app python scripts/create_admin.py root_user --email admin@example.com
```

**동작 요약**:

- 비밀번호는 항상 `getpass` 로 입력받아 두 번 일치 확인 (터미널에 표시되지 않음).
- bcrypt 해시(라운드 12)로 저장. 평문 저장 없음.
- 같은 username 이 이미 있으면 종료 코드 1 + 에러 메시지.
- 정책: username 은 영문 소문자/숫자/밑줄 3~64자, password 는 8자 이상.
- `is_admin=True` 로 생성된 사용자도 일반 로그인(`/login`) 으로 접속 — 관리자
  전용 화면은 Phase 2 부터 추가될 예정이며, 본 단계에서는 DB 의 플래그만 의미를
  갖는다.

**확인**:

```bash
sqlite3 ./data/db/app.sqlite3 \
  "SELECT id, username, is_admin FROM users WHERE is_admin = 1;"
```

> **컨테이너 내부 vs 호스트.** 위 예시는 Docker 사용을 전제로 한다. 호스트에서
> 직접 가상환경을 돌리는 개발 환경이라면 `docker compose run --rm app`
> 부분만 빼고 `python scripts/create_admin.py` 로 동일하게 사용한다.

### 만료된 세션 디버깅

세션 수명은 기본 30 일이다 (`app/auth/constants.py` 의
`SESSION_LIFETIME_DAYS`). 운영 중 임의 사용자의 세션을 즉시 만료시키려면
DB 의 `user_sessions.expires_at` 을 과거로 UPDATE 한다 — 다음 요청부터
해당 사용자는 비로그인으로 처리되며, 자동으로 로그인 페이지에서 다시
인증할 수 있다.

```bash
# 모든 세션 일괄 만료 (운영자 강제 로그아웃)
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE user_sessions SET expires_at = datetime('now', '-1 day');"

# 특정 사용자의 세션만 만료
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE user_sessions
     SET expires_at = datetime('now', '-1 day')
   WHERE user_id = (SELECT id FROM users WHERE username = 'root_user');"
```

만료된 row 자체는 자동으로 삭제되지 않는다 — 적극적인 cleanup 배치는 Phase 2
스케줄러에서 도입될 예정이다.

---

## 스크래퍼 실행 방법

### 기본 실행

```bash
docker compose --profile scrape run --rm scraper
```

모든 실행 파라미터는 `sources.yaml` 의 `scrape:` 섹션으로 제어한다. CLI 인자는 사용하지 않는다.
우선순위: **scrape: 전역 설정 > sources: 소스별 설정 > 코드 default (10페이지 / 200건)**

### 주요 파라미터 (sources.yaml)

| 필드 | 기본값 | 설명 |
|---|---|---|
| `scrape.active_sources` | `[]` | 실행할 소스 ID 목록. 비어 있으면 `enabled: true` 소스 전체 실행 |
| `scrape.max_pages` | `null` | 소스당 최대 페이지 수. null 이면 소스별 설정 → 코드 default(10) |
| `scrape.max_announcements` | `null` | 소스당 최대 공고 수. null 이면 소스별 설정 → 코드 default(200) |
| `scrape.skip_detail` | `false` | true 이면 목록 적재만 수행 (상세 생략) |
| `scrape.skip_attachments` | `false` | true 이면 첨부파일 다운로드 생략 |
| `scrape.dry_run` | `false` | true 이면 DB 쓰기 없이 수집 동작만 검증 |
| `scrape.log_level` | `null` | 로그 레벨 오버라이드. null 이면 .env 의 LOG_LEVEL 사용 |

### 활용 패턴

설정을 변경한 뒤 `docker compose --profile scrape run --rm scraper` 를 실행한다.

**빠른 검증 — 드라이런 (DB 쓰기 없음):**

```yaml
scrape:
  max_pages: 1
  dry_run: true
```

**특정 소스만 실행:**

```yaml
scrape:
  active_sources: [NTIS]
```

**목록·상세만 수집, 첨부파일 다운로드 생략:**

```yaml
scrape:
  skip_attachments: true
```

**목록만 수집 (상세·첨부 모두 생략):**

```yaml
scrape:
  skip_detail: true
```

**디버그 로그로 전체 수집:**

```yaml
scrape:
  log_level: DEBUG
```

**동시 실행.** 같은 명령을 여러 터미널에서 동시에 실행할 수 있다.
각 컨테이너가 `sources.yaml` 의 독립적인 복사본을 사용하므로 설정 경합이 발생하지 않는다.
단, 동일 DB에 동시 쓰기하므로 SQLite WAL 이 활성화되어 있는지 확인한다.

> **[00025 이후]** CLI 경로도 이제 `scrape_runs` 테이블에 `running` 상태 row 를
> 만들어 웹/스케줄 경로와 공용 lock 에 참여한다. 웹이나 스케줄이 이미 수집
> 중인 상태에서 CLI 를 기동하면 종료 코드 `2` 로 거부된다. 아래
> [웹 기반 수집 제어](#웹-기반-수집-제어) 참고.

---

## 웹 기반 수집 제어

Phase 2(Task 00025) 이후로는 관리자 페이지 `/admin` 에서 수집 실행·중단·스케줄
등록과 `sources.yaml` 편집을 브라우저로 처리할 수 있다. 기존 CLI 경로
(`docker compose --profile scrape run --rm scraper`) 는 회귀 없이 그대로 동작
하며, 두 경로는 `scrape_runs` 테이블의 `status='running'` row 하나로 공용
lock 을 건다 — 동시 실행은 자동으로 차단된다.

### 전제 조건

- 관리자 계정(`is_admin=True`) 이 필요하다. 자세한 내용은
  [첫 관리자 계정 생성](#첫-관리자-계정-생성) 참고.
- 호스트의 Docker 데몬 소켓을 app 컨테이너에 마운트한다. `docker-compose.yml`
  의 `app.volumes` 에 이미 선언되어 있다.
- 다음 두 환경변수를 `.env` 에 설정해야 웹 컨테이너가 호스트 dockerd 를
  정상적으로 호출한다.

```bash
# 호스트 docker 그룹의 gid — app 컨테이너 내부 프로세스가 이 gid 를 보조 그룹으로
# 들고 있어야 /var/run/docker.sock 에 접근할 수 있다.
HOST_DOCKER_GID=$(getent group docker | cut -d: -f3)

# 호스트의 프로젝트 루트 절대 경로 — docker compose 가 compose 파일 안의
# 상대경로(./app, ./data 등)를 호스트 기준으로 해석하는 기준이 된다.
HOST_PROJECT_DIR=$(pwd)
```

설정 후 `docker compose up app` 을 재기동하면 적용된다. 값이 잘못되었거나
비어 있으면 수동 시작 버튼을 눌렀을 때 `ComposeEnvironmentError` 또는
`Permission denied on /var/run/docker.sock` 이 flash 배지로 뜬다.

> **보안 경고.** `/var/run/docker.sock` 마운트는 사실상 호스트 root 권한 부여와
> 동등하다 (`docs/scrape_control_design.md §15.1`). FastAPI 는 절대 외부
> 네트워크에 노출하지 않는다 — 로컬 또는 내부 VPN 안에서만 접근 가능하게 둔다.

### 관리자 페이지 접근

관리자로 로그인하면 상단 네비에 **관리자** 링크가 보인다. 클릭하면
`/admin/scrape` 로 이동하고, 상단 탭(`수집 제어 · sources.yaml · 스케줄`) 으로
기능 간 전환한다. 비관리자 계정이 `/admin/*` 경로를 직접 요청하면 `403` 을
반환한다. 비로그인 요청은 `401` 이다.

### [수집 제어] 탭 (`/admin/scrape`)

- 현재 상태 블록: `running` 또는 `idle`. `running` 이면 id/pid/trigger/
  `started_at` · `active_sources` 가 표시되고 **중단** 버튼이 노출된다.
- **지금 시작** 폼: 실행할 소스를 체크박스로 고른다 (모두 미선택 = 전체).
  POST `/admin/scrape/start` 로 제출되면 서버는 내부적으로
  `docker compose --profile scrape run --rm scraper` 를 subprocess 로 띄우고
  `ScrapeRun` row 에 pid 를 기록한다.
- **중단** 버튼: POST `/admin/scrape/cancel` 이 SIGTERM 을 프로세스 그룹에
  전파하고, docker compose v2 가 이를 컨테이너의 `python -m app.cli` 까지
  릴레이한다. 스크래퍼는 현재 처리 중인 공고를 마무리한 뒤 정상 종료하고
  `status='cancelled'` 로 마감된다 (공고 단위 atomic 보장 유지).
- 최근 이력 테이블: 최근 20건의 ScrapeRun 요약. 각 행의 **로그** 링크는
  `/admin/scrape/runs/{id}/log` — subprocess 의 stdout/stderr 파일을
  `text/plain` 으로 반환한다(최대 1 MB, 초과 시 앞부분이 잘린다).
- 5초 폴링: 페이지에 심어진 인라인 JavaScript 가 `/admin/scrape/status` 를
  주기적으로 호출해 상태 블록과 최근 이력만 부분 갱신한다. 탭을 열어 둔
  상태에서 재로드 없이 진행 상황이 반영된다.

### [sources.yaml] 탭 (`/admin/sources/yaml`)

- 진입 시 호스트 `sources.yaml` 원본을 textarea 에 로드한다. 파일이 아직
  없으면 빈 상태로 시작하며, 저장 시 새로 생성된다.
- 저장 파이프라인: **YAML 구문 → Pydantic `SourcesConfig.model_validate`**.
  어느 단계라도 실패하면 원본을 손대지 않고, 실패 경로와 메시지를 화면 상단
  에 나열한 채 textarea 내용은 그대로 보존한다 (사용자가 입력을 잃지 않는다).
- 저장 직전에 `data/backups/sources/YYYYMMDD_HHMMSS.yaml` 로 원본을 백업한
  뒤, 같은 디렉터리의 임시 파일 → `os.replace` 패턴으로 원자적으로 덮어쓴다
  (bind mount 환경의 `EXDEV` 시에는 `open` + `fsync` fallback).
- **이미 실행 중인 수집에는 영향이 없다.** `entrypoint.sh` 가 각 수집 실행
  마다 `sources.yaml` 의 per-run 임시 복사본을 만들기 때문이다. 편집한 값은
  **다음** 수집 실행부터 반영된다.
- 편집 이력 열람 UI 는 Phase 5 범위다. 그때까지는 `data/backups/sources/`
  디렉터리의 파일을 직접 확인한다.

### [스케줄] 탭 (`/admin/schedule`)

- APScheduler `BackgroundScheduler` 를 웹 프로세스 내부에서 가동한다. 잡은
  SQLite `scheduler_jobs` 테이블에 저장되므로 **웹을 재기동해도 스케줄이
  자동 복원**된다. docker-compose 의 `restart: unless-stopped` 정책과
  결합해 실질적으로 연속 가동 상태가 유지된다.
- trigger 는 두 가지:
  - **cron 표현식** — 5-필드 cron(`분 시 일 월 요일`). 예) `0 3 * * *` 은 매일
    UTC 03:00.
  - **매 N시간 간단 모드** — 1~24 범위의 정수. 그 이상 주기는 cron 탭을 쓴다.
- 각 스케줄은 활성/비활성 토글 및 삭제가 가능하다. 토글 시 `next_run_time`
  이 재계산되며, 비활성은 잡을 삭제하지 않고 pause 상태로 둔다.
- 스케줄이 트리거되면 `ScrapeRun.trigger='scheduled'` 로 새 실행이 시작된다.
  마침 다른 수집이 진행 중이면 이번 주기는 건너뛰고 WARN 로그만 남긴 채
  다음 주기를 기다린다 — 스케줄 자체는 중단되지 않는다.
- **uvicorn 단일 워커 전제**. `docker-compose.yml` 의 `app.command` 가
  `--workers` 를 지정하지 않아 uvicorn 기본값(=1) 을 사용한다. `--workers 2`
  이상으로 변경하면 각 워커가 같은 잡을 독립적으로 실행해 중복 수집이
  발생하므로 변경하지 않는다.
- `misfire_grace_time=300s` + `coalesce=True` 로 재기동 직후 밀린 잡이 폭주
  하지 않게 보호한다 (놓친 주기를 한 번으로 합쳐 실행).

### 동시 실행 · lock 규칙

CLI / 수동(웹) / 스케줄 세 경로 모두 `scrape_runs.status='running'` row 하나로
lock 한다. 이미 실행 중이면 새 시도는 다음과 같이 처리된다.

- 웹 **지금 시작** 버튼 → flash 배지 `이미 다른 수집이 진행 중입니다.` (409 의미).
- CLI `docker compose --profile scrape run --rm scraper` → 종료 코드 `2` +
  stderr `이미 다른 수집이 진행 중입니다 (ScrapeRun id=..., trigger='manual', pid=...).`.
- 스케줄 트리거 → WARN 로그 후 건너뛰고 다음 주기 대기.

중단 버튼으로 `cancelled` 처리가 완료되면 즉시 새 실행을 받을 수 있다.

### startup stale cleanup

웹이 재기동될 때 `scrape_runs` 중 **`pid IS NULL`** 이거나 **해당 pid 프로세스가
호스트에 존재하지 않는** `running` row 는 `failed (stale ...)` 로 자동 정리된다.
pid 가 살아 있는 경우는 보수적으로 그대로 두므로, 의도치 않게 진행 중인 수집이
끊기는 일은 없다.

수동 정리가 필요하면 DB 를 직접 수정한다.

```bash
sqlite3 ./data/db/app.sqlite3 \
  "UPDATE scrape_runs SET status='failed', ended_at=datetime('now'), error_message='manual cleanup' WHERE status='running';"
```

### CLI 와 웹 병행

웹 UI 로 전환했다고 해서 기존 CLI 를 폐기할 필요는 없다. 아래 세 조합 모두
정상적으로 지원된다.

- **웹만 사용** — 평소 운영에 권장. 스케줄 등록 + 필요 시 수동 버튼.
- **CLI 만 사용** — 호스트 cron / systemd timer 에서 직접 호출. 이번 task 이후
  CLI 도 `ScrapeRun(trigger='cli')` row 를 기록하고 자기 pid 를 남기므로
  웹의 lock·stale cleanup 규칙에 자연스럽게 참여한다.
- **혼합** — 웹에서 스케줄·수동 조작을 하되, 디버깅이 필요할 때 호스트 쉘에서
  CLI 를 띄워 로그를 상세히 본다. 두 경로가 같은 lock 을 공유하므로 동시
  실행은 자동 차단된다.

### 관련 로그 예시

```
INFO  startup stale cleanup: 1건의 running ScrapeRun 을 failed 로 정리
INFO  APScheduler 기동: tablename=scheduler_jobs misfire_grace_time=300s max_instances=1 coalesce=True
INFO  관리자 'root_user' 의 수동 수집 시작: scrape_run_id=7 pid=12345 active_sources=['IRIS']
INFO  스케줄 수집 기동 완료: scrape_run_id=8 pid=12346 active_sources=(전체)
WARNING  스케줄 수집 건너뜀 — 이미 다른 수집 실행 중: scrape_run_id=7 trigger='manual'
INFO  SIGTERM 수신 — 중단 요청을 기록했습니다. 현재 공고(첨부 포함)를 마무리하고 다음 경계에서 종료합니다.
INFO  ScrapeRun 마감: id=7 status='cancelled' ended_at=...
```

---

## NTIS 수집 운영 특이사항

### 수집 범위 설정

NTIS 마감 공고가 74,000건+ 에 달하므로 `sources.yaml` 기본값을 보수적으로 설정했다 (5페이지 · 100건).
전체 수집이 필요하다면 `sources.yaml` 의 NTIS 소스 또는 전역 scrape 섹션을 수정한다:

```yaml
# sources.yaml
scrape:
  active_sources: [NTIS]
  max_pages: 50
  max_announcements: 5000
```

> **주의**: 위 설정은 수집에 오랜 시간이 걸린다. 실행 후 반드시 터미널을 유지하거나 nohup 등을 사용한다.

NTIS `request_delay_sec` 기본값은 2.0초다. 짧게 줄이면 차단 위험이 있다.

### 첨부파일 다운로드

NTIS 첨부파일은 **httpx POST** 직접 다운로드를 사용한다 (IRIS의 Playwright 경로와 다름).
Playwright 브라우저가 미설치된 환경에서도 NTIS 첨부파일은 정상 다운로드된다.

### canonical 매칭 (cross-source 중복 공고)

NTIS 목록 수집 시 공식 공고번호(ancmNo)를 알 수 없어 **fuzzy canonical key** 가 먼저 부여된다.
상세 수집 완료 후 공고번호가 확보되면 자동으로 **official canonical key** 로 승급된다.
IRIS와 동일 공고인 경우 같은 `canonical_group_id` 로 묶인다.

canonical 승급이 이뤄진 경우 로그에 아래 메시지가 출력된다:
```
INFO  canonical 재계산 완료(fuzzy→official): source=NTIS id=… ancm_no=…
```

---

## 수집 파이프라인 동작 (증분 + delta + snapshot)

스크래퍼를 반복 실행해도 DB는 초기화되지 않는다. 이전 수집 결과를 재사용해
네트워크 요청을 최소화하고, 본 테이블(`announcements` / `attachments`) 적용은
**수집 종료 시점의 단일 트랜잭션**에서 한 번에 일어나도록 설계되어 있다.
설계 상세는 `docs/snapshot_pipeline_design.md` (Phase 5a).

### 흐름 한눈에

```
1. ScrapeRun 시작 (running)
2. 공고 1건 수집:
     - 첨부 즉시 다운로드 → data/downloads/ 에 저장
     - delta_announcements / delta_attachments 에 INSERT (본 테이블은 0회 변경)
3. 수집 종료 시점에 단일 session_scope 트랜잭션:
     a. delta 전수 SELECT
     b. 본 테이블 announcements 와 4-branch 비교 (Phase 1a 로직 그대로 재사용)
     c. INSERT/UPDATE 적용 + (d) 분기 시 사용자 라벨링 reset
     d. 5종 카테고리 매핑 → scrape_snapshots UPSERT (같은 KST 날짜 1 row 머지)
     e. 같은 scrape_run_id 의 delta 전수 DELETE
4. ScrapeRun.status 마감 (completed / cancelled / failed / partial)
```

핵심은 **사용자가 보는 본 테이블 / snapshot 은 트랜잭션 commit 시점에만 변한다**
는 것이다. 도중 실패가 본 테이블이나 snapshot 에 새지 않는다.

### 4-branch 동작 분류 (apply 단계)

apply 트랜잭션 안에서 `delta_announcements` 의 각 row 에 대해 4개 분기 중
정확히 1개로 결정된다.

| 분기                   | 조건                                              | 본 테이블 동작                                        | 사용자 라벨링 reset |
| ---------------------- | ------------------------------------------------- | ----------------------------------------------------- | -------------------- |
| `created`              | 같은 (source_type, source_id) 의 is_current row 없음 | INSERT (신규 row)                                  | X (신규라 의미 없음) |
| `unchanged`            | 비교 4 필드 모두 동일                             | (변화 없음, 상세 재수집은 detail_fetched_at 으로 결정)| X                    |
| `status_transitioned`  | status 만 변경 (in-place UPDATE)                  | 기존 row 의 status 만 UPDATE                          | X (예측 가능한 전이) |
| `new_version`          | title/agency/deadline_at 변경 — 또는 첨부 sha256 변경 (2차 감지) | 구 row 봉인(is_current=False) + 신규 row INSERT | **O** (사용자 읽음/판정 reset) |

**비교 4 필드**: `title` · `status` · `agency` · `deadline_at`. `received_at`
(접수시작일) 은 비교 대상에서 제외 — 접수예정 상태에서 미기재→보완 패턴이
빈번하기 때문.

**2차 감지 (첨부 sha256 기반)**: 1차 action 이 `unchanged` 또는
`status_transitioned` 인 경로에서 같은 announcement 의 첨부 sha256 차이가
감지되면 `new_version` 으로 강등 + reset 발동 (apply 트랜잭션 안에서 같은
session 으로 처리).

### snapshot.payload 5종 카테고리 (사용자 원문 그대로)

apply 결과를 다음 5종 카테고리에 분류한다. 같은 KST 날짜의 후속 ScrapeRun
결과는 `merge_snapshot_payload` 가 머지한다.

| 카테고리                       | 채워지는 경우                              | payload 표현                                  |
| ------------------------------ | ------------------------------------------ | --------------------------------------------- |
| `new`                          | (a) created — 그날 처음 본 테이블에 INSERT | `int[]` (announcement_id, asc 정렬)           |
| `content_changed`              | (d) new_version (1차) 또는 2차 감지 reapply | `int[]`                                       |
| `transitioned_to_접수예정`     | (c) status_transitioned, to=`접수예정`    | `[{id, from}, ...]`                           |
| `transitioned_to_접수중`       | (c) status_transitioned, to=`접수중`      | `[{id, from}, ...]`                           |
| `transitioned_to_마감`         | (c) status_transitioned, to=`마감`        | `[{id, from}, ...]`                           |

### 머지 규칙 (같은 KST 날짜의 여러 ScrapeRun)

- `new` / `content_changed`: ID set union → asc 정렬.
- `transitioned_to_X`: announcement_id 단위로 통합 — **첫 from 유지 + 마지막
  to 갱신**. 최종 `from == to` 이면 카테고리 자체에서 제거 (실질 변화 없음).
- `counts`: 머지 후 5종 배열 길이를 1:1 로 반영.

> **참고: 머지 동작은 ScrapeRun 종료 시계열에 의존한다** (commutative 가
> 아니다). 예) `접수예정→접수중→마감` 으로 같은 공고가 흘러간 날의 snapshot
> 에는 `transitioned_to_마감` 에 `{id, from='접수예정'}` 만 남는다 — 중간
> 상태인 `transitioned_to_접수중` 에서는 사라진다.

### 종료 분기 (cancelled / failed / completed / partial)

| ScrapeRun.status | 트리거                                         | apply 호출 | delta clear                  | 본 테이블 / snapshot |
| ---------------- | ---------------------------------------------- | ---------- | ---------------------------- | -------------------- |
| `completed`      | 정상 종료, 실패 0                              | O (트랜잭션 commit) | 트랜잭션 안에서 비움 | 영구화               |
| `partial`        | 정상 종료, 일부 공고 실패                      | O           | 트랜잭션 안에서 비움 | 영구화               |
| `cancelled`      | SIGTERM 등 사용자 의도 중단                    | X (skip)   | 별도 트랜잭션으로 비움      | 변화 없음            |
| `failed` (apply 자체 실패) | apply 트랜잭션 도중 raise            | rollback   | rollback 으로 보존          | 변화 없음            |
| `failed` (수집 단계 예외) | apply 도달 전 orchestrator 예외      | X (skip)   | 별도 트랜잭션으로 비움      | 변화 없음            |

apply 트랜잭션 자체가 실패한 경우 (검증 11) 만 delta 가 보존되고, 다음
ScrapeRun 으로 같은 공고를 재수집해 정상 처리되는 경로가 살아있다.

### 변경 감지 비교 대상 필드

- **공고명(title)**, **상태(status)**, **마감일(deadline_at)**, **기관명(agency)**
- `received_at`(접수시작일)은 비교 대상에서 제외 (접수예정 상태에서 미기재→보완
  패턴이 빈번하기 때문).

### 이력 / snapshot 조회 (SQL)

```sql
-- 현재 유효 버전만 (목록 UI와 동일)
SELECT * FROM announcements WHERE is_current = 1;

-- 특정 공고의 전체 이력 (구버전 포함)
SELECT * FROM announcements
WHERE source_type = 'IRIS' AND source_announcement_id = '12345'
ORDER BY id;

-- 오늘(KST) 의 일자별 변화 요약
SELECT snapshot_date, payload FROM scrape_snapshots
ORDER BY snapshot_date DESC LIMIT 1;

-- 이번 ScrapeRun 의 staging 잔여 (정상이면 0)
SELECT scrape_run_id, COUNT(*) FROM delta_announcements GROUP BY scrape_run_id;
```

### 고아 첨부 파일 GC

apply 트랜잭션이 실패하거나 운영자가 본 테이블에서 attachment row 를 수동
삭제한 경우 `data/downloads/` 에 \"DB 가 참조하지 않는 고아 파일\" 이 누적
될 수 있다 (첨부 파일은 트랜잭션 보호 밖).

수동 실행 (먼저 `--dry-run` 으로 후보 검수 → 실제 삭제):

```bash
# 1) 후보 확인 (디스크 변경 없음). 컨테이너 안에서 실행할 것 — 호스트 직접 실행 금지.
docker compose --profile scrape run --rm scraper \
    python scripts/gc_orphan_attachments.py --dry-run

# 2) 검수 후 실제 삭제
docker compose --profile scrape run --rm scraper \
    python scripts/gc_orphan_attachments.py
```

**ScrapeRun running 가드**: 수집 진행 중에는 GC 가 거부된다 (종료 코드 2).
방금 다운로드된 파일이 잘못 삭제되는 것을 막는 안전장치다. 운영자가
의도적으로 우회하려면 `--force` 를 사용한다 (위험 — 권장하지 않음).

자동 일 1회 등록 (선택, KST 04:00 기본):

```bash
docker compose --profile scrape run --rm scraper python -c "
from app.scheduler.service import start, add_gc_orphan_cron_schedule
start()
summary = add_gc_orphan_cron_schedule()  # 기본 cron='0 4 * * *' (KST 04:00)
print(summary)
"
```

스케줄러 jobstore 가 SQLite 의 `scheduler_jobs` 테이블에 저장하므로 한 번
등록하면 컨테이너 재기동에도 자동 복원된다. 등록 후에는 관리자 페이지의
[스케줄] 탭에서 확인 가능 (수집 스케줄과 같은 목록에 노출).

---

## 웹 UI 검색/필터/중복 그룹 보기

웹 UI(`http://localhost:8000`)는 GET 쿼리 파라미터로 필터/정렬/페이지 상태를 보존한다.

### 쿼리 파라미터

| 파라미터 | 허용값 | 기본값 | 설명 |
|---------|--------|--------|------|
| `status` | `접수중` `접수예정` `마감` 또는 생략 | 전체 | 공고 상태 필터 |
| `source` | `IRIS` `NTIS` 등 소스 ID 또는 생략 | 전체 | 수집 소스 필터 |
| `search` | 임의 문자열 | - | 제목 부분 일치 검색 |
| `sort` | `received_desc` `deadline_asc` `title_asc` | `received_desc` | 정렬 기준 |
| `group` | `on` `off` | `off` | 중복 묶어 보기 토글 |
| `page` | 정수 | `1` | 페이지 번호 |

### 예시 URL

```
# 접수중 공고만, 마감일 가까운순
http://localhost:8000/?status=접수중&sort=deadline_asc

# NTIS 소스, 제목에 "나노" 포함
http://localhost:8000/?source=NTIS&search=나노

# 중복 묶어 보기 + 2페이지
http://localhost:8000/?group=on&page=2
```

### 중복 묶어 보기 (`group=on`)

같은 과제가 IRIS·NTIS 양쪽에 등록된 경우 1행으로 묶어 표시한다.
행 우측 배지(예: `3건`)를 클릭하면 소스별 개별 공고를 펼쳐볼 수 있다.
기본값(`group=off`)은 소스별로 각각 1행 표시하며, `동일 과제 N건` 배지로 중복 여부를 안내한다.

---

## 관련성 판정 사용법

로그인 사용자는 목록·상세 페이지에서 각 공고(canonical 과제 단위)의 관련성을 직접 판정할 수 있다.
비로그인 시 관련성 컬럼과 배지는 표시되지 않는다.

### 배지 색상

| 배지 | 의미 | 색상 |
|------|------|------|
| **관련** | 현재 사용자가 "관련" 으로 판정 | 초록 |
| **무관** | 현재 사용자가 "무관" 으로 판정 | 회색 |
| **미검토** | 아직 판정하지 않음 (테두리만) | 연한 회색 테두리 |

배지는 **현재 로그인한 사용자 본인**의 판정만 반영한다.
다른 사용자의 판정은 배지 색에 영향을 주지 않는다.

### 판정 방법

1. 목록 페이지에서 원하는 공고의 관련성 배지("미검토" / "관련" / "무관")를 클릭한다.
2. 판정 모달이 열리면 **관련** 또는 **무관** 라디오를 선택한다.
3. 필요하면 사유 입력란에 메모를 남긴다(선택).
4. **저장** 버튼을 누르면 페이지 새로고침 없이 배지가 즉시 갱신된다.
5. 판정을 취소하려면 **삭제** 버튼을 누른다(기존 판정이 있을 때만 표시).
6. 모달 밖을 클릭하거나 **취소** 버튼을 누르면 아무 변경 없이 닫힌다.

### 이력 툴팁

배지에 마우스를 올리면 현재 판정자 목록과 과거 이력(최대 3건)을 툴팁으로 확인할 수 있다.

| 아이콘 | 의미 |
|--------|------|
| 📝 | 공고 내용 변경으로 자동 이관됨 |
| ✏️ | 사용자가 판정을 변경함 |
| 🔧 | 관리자 조치 (Phase 5 이후) |

### 다중 사용자 주의사항

- 여러 사용자가 같은 공고에 각자 판정을 남길 수 있다. 서로의 판정에 영향을 주지 않는다.
- 본인 판정을 변경하면 이전 판정이 이력으로 이관되고, 새 판정이 저장된다.

---

## 읽음 bulk 사용법

로그인 사용자는 목록 페이지에서 여러 공고를 한 번에 읽음/안읽음 처리할 수 있다.

### 개별/페이지 선택

1. 각 공고 행 왼쪽의 체크박스를 클릭해 개별 선택한다.
2. 테이블 헤더의 체크박스를 클릭하면 현재 페이지 전체를 선택/해제한다.
3. 1개 이상 선택하면 테이블 위에 toolbar 가 나타난다.

### 툴바 버튼

| 버튼 | 동작 |
|------|------|
| **읽음** | 선택한 공고를 "읽음" 처리 (제목 볼드 해제) |
| **안읽음** | 선택한 공고를 "안읽음" 처리 (제목 볼드 복원) |
| **선택 해제** | 모든 선택 취소 |

ids 모드(개별 선택)는 해당 행의 읽음 상태를 페이지 새로고침 없이 즉시 반영한다.

### Gmail 스타일 필터 전체 선택

현재 페이지를 전체 선택하면 toolbar 에 아래 안내가 추가로 표시된다:

```
현재 필터 결과 전체 M건 선택
```

이 링크를 클릭하면 현재 필터 조건에 맞는 **모든 페이지의 공고 전체**를 선택 대상으로 삼는다.
"읽음" / "안읽음" 버튼 클릭 시 서버가 필터를 다시 적용해 전체를 일괄 처리한 뒤 페이지를 새로고침한다.

> **주의**: 한 번에 최대 5,000건 (환경변수 `MAX_BULK_MARK` 로 조정 가능)을 처리한다.
> 초과 시 422 오류가 반환되며 선택을 해제하지 않는다. 필터를 좁혀 재시도한다.

### MAX_BULK_MARK 환경변수

```bash
# .env
MAX_BULK_MARK=5000   # 기본값. 더 많은 건을 일괄 처리하려면 숫자를 늘린다.
```

변경 후에는 컨테이너를 재기동해야 반영된다:

```bash
docker compose restart app
```

---

## 즐겨찾기 / 동일 과제 사용법

로그인 사용자는 공고를 폴더별로 즐겨찾기하고, 동일 과제 묶음 정보를 목록·상세 양쪽에서 확인할 수 있다.

> **저장 단위 변경 (task 00037)**: 즐겨찾기는 이제 **공고(announcement) 단위**로 저장된다.
> 00036 에서 채택했던 \"canonical 과제 단위 1건\" 설계는 별표를 누른 바로 그 공고가 아닌 다른
> 공고가 대표로 등록되거나, 동일 과제의 다른 공고가 즐겨찾기 목록에서 보이지 않는 문제를 일으켜 폐기되었다.
> 기본값은 \"이 공고만 저장\" 이며, IRIS·NTIS 등 동일 과제 공고 전체를 한 번에 넣으려면 폴더 선택
> 모달의 \"동일 과제 공고 모두 저장\" 라디오를 선택한다.

### 별 아이콘 (☆ / ★)

목록 페이지와 상세 페이지의 제목 왼쪽에 별 아이콘이 표시된다 (로그인 사용자에게만 — canonical
매칭 여부와 무관하게 표시).

| 아이콘 | 의미 |
|--------|------|
| ☆ (빈 별) | 이 공고는 아직 즐겨찾기하지 않음 |
| ★ (채워진 별) | 이 공고가 즐겨찾기에 등록됨 (어느 폴더에든 저장된 상태) |

**즐겨찾기 추가 방법**:

1. 목록 또는 상세 페이지에서 제목 앞 ☆ 아이콘을 클릭한다.
2. 폴더 선택 모달이 열린다. 상단에 라디오 2개가 표시된다:
   - **이 공고만 저장** (기본) — 별을 누른 바로 그 공고 1건만 등록한다.
   - **동일 과제 공고 모두 저장** — 같은 canonical 그룹에 묶인 is_current 공고(IRIS·NTIS 등)를 모두 한꺼번에 등록한다. canonical 매칭이 없는 공고에서는 비활성화된다.
3. 저장할 폴더를 선택한다. 폴더가 없으면 \"새 그룹 이름\" 입력란에 이름을 입력하고 **그룹 추가** 또는 **서브그룹 추가** 버튼을 클릭한다.
4. **추가** 버튼을 누르면 즐겨찾기가 저장되고 해당 공고(들)의 별이 ★ 으로 바뀐다.
5. \"동일 과제 공고 모두 저장\" 을 선택했을 때는 같은 페이지에 표시된 같은 canonical 그룹 공고들의 별이 모두 ★ 로 동기화된다.
6. 이미 해당 폴더에 있는 공고는 다시 등록되지 않고 조용히 건너뛴다(오류 아님).

**즐겨찾기 제거 방법**:

- 목록 또는 상세 페이지: 채워진 별(★)을 클릭하면 **그 공고 1건만** 즉시 제거된다(동일 과제 형제는 유지).
- 즐겨찾기 탭(`/favorites`): 항목 행의 **제거** 버튼을 클릭한다.

### 즐겨찾기 탭 (`/favorites`)

상단 네비게이션의 **즐겨찾기** 링크(로그인 시에만 표시)를 클릭하면 전용 탭 페이지로 이동한다.

**좌 패널 — 폴더 트리 (task 00037 에서 indent 트리 뷰로 재설계)**:

| 동작 | 방법 |
|------|------|
| 폴더 선택 | 폴더 이름 클릭 → 우 패널에 해당 폴더의 공고 목록이 표시됨 |
| 서브그룹 접기 / 펼치기 | 루트 폴더 왼쪽의 ▾ / ▸ caret 을 클릭 (자식이 있는 루트에만 표시). 기본 상태는 \"펼침\". |
| 그룹(루트 폴더) 추가 | 하단 이름 입력 → **그룹 추가** 클릭 |
| 서브그룹(1단계 하위) 추가 | 루트 폴더 클릭 선택 → 이름 입력 → **서브그룹 추가** 클릭 |
| 폴더 이름 변경 | 폴더 행의 ✎ 아이콘 클릭 → 다이얼로그에서 변경 (항상 표시) |
| 폴더 삭제 | 폴더 행의 ✕ 아이콘 클릭 → 확인 다이얼로그 (cascade 개수 경고) |

> **폴더 깊이 제한**: 그룹 > 서브그룹 2단계까지만 허용된다. 서브그룹을 선택한 상태에서는 **서브그룹 추가** 버튼이 비활성화된다.

> **폴더 삭제 시 cascade (task 00037 변경)**: 그룹 폴더를 삭제하면 하위 서브그룹과 안에 담긴 공고가 **모두** 함께 삭제된다. 이전에 서브그룹이 루트로 \"격상\" 되던 동작은 제거되었다. 삭제 확인 다이얼로그에 \"하위 서브그룹 N개, 공고 M건이 함께 삭제됩니다\" 경고가 표시되며 되돌릴 수 없다.

**우 패널 — 공고 목록**:

좌 패널에서 폴더를 선택하면 해당 폴더에 저장된 즐겨찾기 항목이 공고 테이블로 표시된다.
관련성 배지(미검토 / 관련 / 무관), 소스, 상태, 마감일 컬럼과 함께 **이동 / 제거** 두 버튼이 동작 컬럼에 노출된다.

| 동작 | 설명 |
|------|------|
| **이동** (task 00037 신규) | 폴더 선택 모달을 \"이동 모드\" 로 재사용해 다른 폴더로 옮긴다. 같은 폴더로의 이동은 무시되고, 대상 폴더에 동일 공고가 이미 있으면 409 로 알려준다. 드래그 앤 드롭은 지원하지 않는다. |
| **제거** | 해당 공고 1건만 즐겨찾기에서 제거한다. |

**읽음 / 안읽음 표시 (task 00037 plan_review 추가요청)**:

즐겨찾기 테이블의 제목도 목록 페이지와 동일한 규칙으로 표시된다 — 아직 상세 페이지를 열람하지
않은(안 읽은) 공고는 제목이 **굵게(bold)** 표시되고, 이미 열람한(읽은) 공고는 일반 글자로 표시된다.

### 동일 과제 확인

같은 canonical 그룹에 IRIS·NTIS 등 복수의 공고가 묶인 경우 아래 두 가지 방법으로 확인할 수 있다.

**목록 페이지 — inline expand**:

1. 공고 행의 제목 옆 **동일 과제 N건 ▾** 배지를 클릭한다.
2. 해당 canonical 그룹의 다른 공고들이 행 아래 펼쳐진다.
3. 매칭 근거 배지가 함께 표시된다:
   - `[공식]` — 공고번호(ancmNo) 기반 정확 매칭
   - `[유사]` — 제목·기관·연도 조합 fuzzy 매칭 (canonical 승급 전 NTIS 공고에 주로 표시)
4. 다시 클릭하면 접힌다.

**상세 페이지 — '동일 과제' 섹션**:

공고 상세 페이지 하단에 **동일 과제 (N건)** 섹션이 표시된다 (비로그인도 확인 가능).
각 행을 클릭하면 해당 공고의 상세 페이지로 이동한다.

### canonical 매칭 품질에 대한 주의사항

NTIS 공고는 상세 수집 전 fuzzy 키로 묶였다가 상세 수집 후 공식 키로 승급되는 과정을 거친다.
이 과정에서 드물게 실제로는 다른 과제인 두 공고가 같은 canonical 그룹으로 묶이는 false-positive 가 발생할 수 있다.

> **권장**: 별 아이콘으로 즐겨찾기를 저장하기 전, 동일 과제 섹션에서 묶인 공고들의 제목을 확인하여 실제로 같은 과제인지 검토할 것을 권장한다.

false-positive 발견 시 Phase 5 의 `canonical_override` 기능(미구현)을 통해 그룹 분리가 가능하다.
현재는 별도 조치 없이 해당 항목의 즐겨찾기를 제거하거나 폴더에 별도 기재하여 관리한다.

### 비로그인 사용자

- 별 아이콘(☆/★) 과 즐겨찾기 탭은 표시되지 않는다.
- 동일 과제 expand(목록), 동일 과제 섹션(상세)은 비로그인 상태에서도 볼 수 있다.

---

## 대시보드 사용법 (Phase 5b / task 00042)

상단 네비게이션의 **대시보드** 링크는 비로그인 / 로그인 모두에게 노출된다.
페이지는 하루 단위 변화 누적을 두 시점 비교 형태로 보여 주고, 1개월 이내
접수 시작 / 마감 예정 공고를 함께 표시하며, 로그인 시에는 사용자별 라벨링
미처리 카운트와 ±15일 추이 차트를 추가로 노출한다.

설계 근거: `docs/dashboard_design.md` (페이지 § 번호로 인용 가능).

### 컨트롤 — 기준일 / 비교 대상

페이지 상단의 컨트롤 영역은 form GET 으로 동작한다 — 캘린더에서 일자를
클릭하거나 비교 모드 select 를 바꾸면 즉시 페이지 reload (사용자 원문
"자동 갱신 안 함, reload 해야 반영" 정책 그대로).

| 컨트롤 | 의미 |
|-------|-----|
| 기준일 캘린더 | 비교 구간의 끝점 (`to`). 기본값 = 오늘 KST. snapshot 이 있는 날짜만 클릭 가능 (활성 표시). |
| 비교 대상 select | `전날` / `전주` / `전월` / `전년` / `직접 선택` 5종. 직접 선택 시 비교일 캘린더가 함께 노출. |
| 비교일 캘린더 | `직접 선택` 모드에서만 표시. 비교 구간의 시작점 (`from`). 가용 일자만 클릭 가능. |

### 캘린더 활성 / 비활성 규칙 (중요)

캘린더의 활성 표시는 "변화가 있었던 날" 이 아니라 **"수집이 끝까지 돌아간 날"**
이다. Phase 5a 의 ScrapeRun 이 completed / partial 로 종료되면 변화 0건이어도
`scrape_snapshots` row 가 생성되어 캘린더에서 활성으로 보인다.

- 활성 (클릭 가능): `scrape_snapshots.snapshot_date` 에 해당 일자가 있다.
- 비활성 (흐리게 + click 무시): 해당 일자에 ScrapeRun 자체가 없거나 모두
  `failed` / `cancelled` 로 끝나 snapshot 이 INSERT 되지 않은 경우.

비교일이 가용하지 않으면 자동으로 가장 가까운 이전 snapshot 으로 fallback 되며,
A 섹션 상단에 노란 안내문이 표시된다 ("비교일 X 일자 snapshot 이 없어 Y 일자
snapshot 을 사용했습니다"). 비교일 이전 snapshot 이 아예 없으면 A 섹션은
"데이터 없음" 으로 표시되고 B 섹션은 정상 동작한다.

### A 섹션 — 공고의 변화 (diff 누적)

`(from, to]` 구간의 모든 snapshot 을 시간순 누적 머지해 5종 카테고리 카드로
표시한다. 사용자 원문 카테고리 그대로:

1. 신규 (`new`)
2. 내용 변경 (`content_changed`) — 같은 공고가 다른 카테고리에도 등장하면 작은
   배지 (예: `🔄 전이→접수중도`) 추가.
3. 전이 → 접수예정
4. 전이 → 접수중
5. 전이 → 마감

각 카드의 카운트 표시:

```
신규
기준일 12건  ↑ 4 (비교일 8건 대비)
```

- **기준일 N건** = `(from, to]` 누적 머지 후 `payload.counts[category_key]`.
- **비교일 M건** = 비교일 effective snapshot 의 단일 `payload.counts[category_key]`
  (fallback 적용 후 일자 — 안내문에 표기됨).
- **↑ X / ↓ X / 변동 없음** = `N − M` 의 부호.

카드 클릭 시 `<details>` native expand 로 아래 영역이 펼쳐지며, 누적 머지 결과
카테고리 ID 전체에 대해 announcements 메타 1회 IN 쿼리 로 fetch 한 행 list 가
표시된다. 행 형식 (사용자 원문 §6.2):

```
[IRIS] [접수예정] 2025년도 인공지능 대학원 지원사업 ...    마감 2025-06-30
[IRIS] [접수중]   스마트팜 기술개발 사업 (접수예정에서)    마감 2025-06-15
```

각 행은 `<a href="/announcements/{id}">` 단일 click target — 가운데 클릭은
표준 `<a href>` 동작으로 새 창에서 열린다. 마감일은 모두 `kst_date` 필터로
KST 표시.

### B 섹션 — 조만간 변화 예정 (1개월 이내)

`to` (기준일) 시점 기준 향후 30일 이내에:

- **조만간 접수될 공고** = `is_current=True` AND `status='접수예정'` AND
  `received_at BETWEEN to AND to+30 days`.
- **조만간 마감될 공고** = `is_current=True` AND `status='접수중'` AND
  `deadline_at BETWEEN to AND to+30 days`.

두 그룹은 가로 2열로 배치되며 (좁은 화면은 세로 스택), 정렬은 임박 순
(`received_at ASC` / `deadline_at ASC`).

`to` 가 KST 오늘보다 과거이면 회색 안내문이 노출된다:

> 기준일이 과거라 표시되는 정보는 현재 기준이며 정확하지 않을 수 있습니다.

이력 (`is_current=False`) row 활용은 본 task 범위 밖이라, 과거 시점의 활성
공고를 정확하게 재현하지는 않는다.

### 사용자 라벨링 위젯 (로그인 시)

로그인 사용자에게만 컨트롤 영역 아래에 4종 카운트 카드가 표시된다. 비로그인
은 영역 자체가 DOM 에 들어가지 않는다 (라우트가 위젯 쿼리도 skip — DEBUG 로그
"dashboard widgets skip (비로그인) — 4종 카운트 쿼리 실행 안 함" 확인 가능).

| # | 라벨 | 단위 | 범위 |
|---|------|------|------|
| 1 | 전체 미확인 공고 | announcement (읽음) | 날짜 무관 |
| 2 | 전체 미판정 관련성 | canonical (관련성) | 날짜 무관 |
| 3 | 기준일 변경 공고 중 내 미확인 | announcement (읽음) | `(from, to]` 머지된 announcement_ids 한정 |
| 4 | 기준일 변경 공고 중 내 미판정 | canonical (관련성) | `(from, to]` 머지된 canonical_ids 한정 |

읽음 = announcement 단위, 관련성 = canonical 단위 — 둘은 혼용하지 않는다
(PROJECT_NOTES 결정사항). 위젯 3·4 의 입력은 A 섹션이 이미 fetch 한 ID list
를 그대로 재사용해 추가 announcement 쿼리 없이 SELECT 1회로 카운트된다.

### 추이 차트 (보조)

기준일 ±15일 (총 31일, 양끝 포함) 범위의 일별 카운트 line chart 가 페이지
하단에 노출된다 — 신규 / 내용 변경 / 전이 (접수예정 + 접수중 + 마감 합산)
3 series. snapshot 이 없는 날짜는 0 으로 채워져 골짜기로 표시된다. x축 라벨은
KST 기준 'MM-DD' 형식.

차트 데이터는 서버에서 사전 계산해 페이지에 JSON 으로 임베드된다 (별도
fetch / API 호출 없음). 라이브러리는 `app/web/static/vendor/chart.min.js` 로
번들된 Chart.js v4.4.0 (MIT 라이선스 — 자세한 내용은 프로젝트 루트의 `NOTICE`
파일 참조).

### URL 직접 입력

`GET /dashboard?base_date=2026-04-29&compare_mode=prev_week` 처럼 직접 URL 을
조립해도 된다. query 파라미터 검증:

- `base_date`: KST date `YYYY-MM-DD`. 미지정 / 파싱 실패는 KST 오늘로 silent
  fallback (페이지가 깨지지 않도록).
- `compare_mode`: 5종 enum. 그 외 값은 400.
- `compare_date`: `compare_mode=custom` 일 때만 의미 — 비면 400.

### 페이지 새로고침 정책

대시보드는 **자동 갱신을 하지 않는다**. 새 ScrapeRun 이 끝나도 페이지는
이전 데이터를 보여 주며, 사용자가 reload (또는 컨트롤 변경 → 페이지 GET
submit) 시점에 갱신된다. 캘린더의 가용 날짜 set 도 페이지 reload 시점에만
서버에서 다시 계산된다.

### 비로그인 사용자 / 로그인 사용자 차이

| 영역 | 비로그인 | 로그인 |
|------|---------|--------|
| 컨트롤 (캘린더 / 비교 모드) | ✅ | ✅ |
| A 섹션 (공고 변화 카드 + expand) | ✅ | ✅ |
| B 섹션 (1개월 이내 활성 공고) | ✅ | ✅ |
| 사용자 라벨링 위젯 4종 | ❌ (영역 자체 미렌더 + 쿼리 skip) | ✅ |
| 추이 차트 | ✅ | ✅ |

### 트러블슈팅

- **추이 차트가 안 보임**: 브라우저 콘솔에 `dashboard_trend_chart: Chart.js 미로드`
  경고가 찍히면 `app/web/static/vendor/chart.min.js` 가 누락된 것이다. NOTICE
  파일에 박힌 출처 URL 에서 다시 받아 vendor 디렉터리에 두면 된다.
- **캘린더에 클릭 가능한 날짜가 하나도 없음**: ScrapeRun 이 한 번도 completed /
  partial 로 끝나지 않은 신규 환경이다. CLI 또는 관리자 페이지 [수집 제어] 탭
  에서 수집을 한 번 돌리면 그 날짜가 활성으로 표시된다.
- **비교일이 자동으로 다른 날짜로 fallback 됐다고 안내문이 뜸**: 정상 동작이다.
  사용자 원문 §4.2 의 fallback 정책 — 비교일 snapshot 이 없을 때 가장 가까운
  이전 snapshot 으로 자동 대체된다. 안내문에 실제 사용된 일자가 표기된다.

---

## 로그 해석

### 정상 수집 시 주요 로그 (Phase 5a delta + apply 흐름)

```
INFO  목록 수집 시작: source=IRIS max_pages=10
INFO  목록 수집 완료: source=IRIS 42건
INFO  ── [1/42] 공고 처리: source=IRIS id=12345
INFO  상세 수집 완료(ok): source=IRIS id=12345
INFO  첨부파일 수집 완료: source=IRIS id=12345 delta_attachments INSERT=3 실패=0
...
INFO  ── [5/42] 공고 처리: source=IRIS id=11111
INFO  상세 수집 생략(본 테이블에 unchanged + detail 보유): source=IRIS id=11111
...
INFO  소스 IRIS 완료(수집 단계): delta INSERT 성공 42건 / 실패 0건 |
      상세 성공 5건 / 실패 0건 / 생략(unchanged peek) 37건 |
      첨부 다운로드 성공 8건 / 실패 0건 (action_counts / 2차 감지 통계는 apply 단계에서 집계됩니다)

(수집 종료 시점 — 단일 트랜잭션)
INFO  apply_delta_to_main 완료: scrape_run_id=42 처리=42건
      actions={'created': 5, 'unchanged': 37}
      new=5 content_changed=0 transitions=0
      attachment_success=8 attachment_skipped=74 2차변경=0
INFO  ScrapeSnapshot 신규 INSERT: snapshot_date=2026-04-29
      counts={'new': 5, 'content_changed': 0, ...}
INFO  apply_delta_to_main 트랜잭션 commit 완료: status=completed
INFO  scrape 실행 완료: delta INSERT 성공 42건 / 실패 0건 | ...
      apply action 분포: 신규=5 변경없음=37 버전갱신=0 상태전이=0
      apply 2차 감지(첨부 변경)=0건 | final_status=completed
```

> **확인 포인트**:
> - 2회차 이후 실행에서 `변경없음=N` 이 전체 공고 수와 가까울수록 정상이다.
>   `신규=0 변경없음=42` 이면 모든 공고가 기존 데이터를 재사용하고 상세·첨부
>   재수집도 최소화된다.
> - 수집 단계 통계(`delta INSERT 성공 X건`) 와 apply 단계 통계
>   (`actions={...}`) 는 각각 다른 트랜잭션 / 다른 시점이라 키 이름이 분리
>   되어 있다 — apply 의 `actions` 가 사용자 입장의 \"실제 본 테이블 변화\".
> - `apply_delta_to_main 트랜잭션 commit 완료` 로그가 보이면 본 테이블 +
>   snapshot + delta 비움이 모두 영구화된 시점.

### 주의가 필요한 로그

| 로그                                                          | 의미                                                              | 대응                                                          |
| ------------------------------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------- |
| `WARNING 중단 요청 감지 — 남은 공고 N건 스킵`                 | SIGTERM 수신 — 공고 1건 마무리 후 다음 경계에서 중단              | 정상 동작. 종료 후 status='cancelled' + delta 비움 자동 처리 |
| `WARNING 수집 중단(cancelled) — apply 건너뜀 + delta 비움`    | cancelled 분기에서 본 테이블 / snapshot 미반영 + delta 만 비움    | 정상 — 사용자 원문 검증 2 그대로                              |
| `EXCEPTION apply_delta_to_main 트랜잭션 실패 — delta 보존`    | apply 도중 예외 → SQLAlchemy auto-rollback                        | 다음 ScrapeRun 으로 같은 공고 재수집해 자동 복구. 검증 11    |
| `WARNING detail_url 없음 — 상세 수집 스킵`                    | 목록에서 상세 URL을 추출하지 못함                                  | 해당 소스의 HTML 구조 변경 여부 확인                          |
| `ERROR delta INSERT 실패`                                     | delta 적재 실패 (DB 권한 / 디스크 용량 등 환경 문제)              | 로그 확인 후 재실행                                            |
| `WARNING delta INSERT 실패 source_announcement_id 목록`       | 특정 공고 처리 실패 — apply 단계는 정상 진행 (다른 공고는 영향 없음) | 실패 ID 로그 확인 후 다음 수집에서 자동 재시도              |
| `INFO 상태 전이 — in-place 갱신`                              | 동일 공고가 다른 상태로 재등장해 in-place UPDATE                  | 정상 동작. 빈번하게 발생한다면 `docs/status_transition_todo.md` 참고 |
| `INFO apply 2차 감지 — 첨부 변경으로 버전 갱신`               | 첨부 sha256 차이 감지 → reapply (사용자 라벨링 reset)             | 정상 동작                                                      |
| `WARNING GC 거부 — ScrapeRun id=N 가 'running' 입니다`        | 수집 중 GC 시도 거부 (종료 코드 2)                                 | 수집 종료 후 재실행하거나 자동 cron 으로 회피                |

---

## 웹 로그 레벨 제어와 원인 추적 (00030)

웹(`iris-agent-web`) 컨테이너의 로그는 `.env` 의 `LOG_LEVEL` 하나로 전체 상세도가 조절된다.
00030 에서 loguru↔stdlib 브리지가 추가되면서 uvicorn/starlette/fastapi/sqlalchemy/alembic 의
로그가 모두 같은 형식·같은 파일로 흘러온다.

### 레벨 선택 가이드

| LOG_LEVEL | 언제 | 보이는 것 |
|---|---|---|
| `INFO` (기본 권장) | 운영 | 로그인/세션 발급/HTTP 요청 1줄·수집 진행 요약·경고·에러 |
| `DEBUG` | 500 에러·인증 실패·수집 이상 등 **원인 추적** | INFO 전부 + 인증 의존성 분기·세션 검증·관리자 가드·DB 세션 open/commit/close·admin 라우트 진입 |
| `WARNING` | 로그 양 최소화 | 경고·에러만 |

설정 후 반드시 컨테이너를 재기동해야 반영된다:

```bash
# .env 편집
sed -i 's/^LOG_LEVEL=.*/LOG_LEVEL=DEBUG/' .env

# 재기동 (코드 변경 없으면 restart 로 충분)
docker compose restart app

# 적용 확인 — 기동 직후 첫 줄에 아래 메시지가 찍힌다.
docker logs iris-agent-web 2>&1 | head -5
# 예) 2026-04-23 ... | INFO     | req=- | app.logging_setup:configure_logging:...
#     - 로깅 초기화 완료: log_level=DEBUG diagnose=True stdlib_bridge=installed
```

### 로그 포맷 읽는 법

```
2026-04-23 16:38:20.145 | INFO     | req=Q4H8VYJO... | app.auth.routes:login_submit:259 - 로그인 성공: user_id=1
└──────────timestamp──┘ └─level──┘ └─request_id──┘ └──────module:function:line──────────┘  └──메시지──┘
```

- **timestamp**: 컨테이너 타임존(기본 Asia/Seoul, 00029) 기준.
- **level**: DEBUG/INFO/WARNING/ERROR/CRITICAL.
- **req=...**: HTTP 요청 한 건마다 발급되는 12자 ID. 같은 req 를 가진 로그들은 같은 요청에 속한다.
  요청 컨텍스트 밖(기동·스케줄러·CLI)에서는 `req=-` 로 찍힌다.
- **module:function:line**: 로그를 찍은 코드 위치. stdlib 브리지를 통해 들어온 uvicorn/alembic 로그도
  원 호출자 위치가 복원된다.

### 요청 단위로 로그 묶어 보기

500 에러 원인을 찾을 때는 해당 요청의 `req=...` 값으로 전후 로그를 한 번에 잘라 보는 것이 가장 빠르다:

```bash
# 1) 에러가 발생한 요청의 req 값을 찾는다
docker logs iris-agent-web 2>&1 | grep "미처리 예외"
# 예) ... | ERROR | req=9d4ecf87a930 | ...: 미처리 예외 발생: method=GET path=/admin/scrape ...

# 2) 그 req 로 해당 요청 전체 로그를 뽑는다 (진입→인증→DB→응답 흐름)
docker logs iris-agent-web 2>&1 | grep "req=9d4ecf87a930"
```

### 500 에러 시 반드시 확인할 DEBUG 로그 체크리스트 (LOG_LEVEL=DEBUG 전제)

순서대로 찍히는지 보면 어느 구간에서 멈췄는지 알 수 있다:

| 순서 | 로그 샘플 | 의미 |
|---|---|---|
| 1 | `request 진입: method=GET path=/admin/scrape ...` | 미들웨어가 요청을 받음 |
| 2 | `auth DB 세션 open` | 인증 의존성용 DB 세션 열림 |
| 3 | `current_user_optional: 세션 검증 성공 user_id=... is_admin=True ...` | 쿠키/세션 통과 |
| 4 | `admin_user_required: 통과 user_id=...` | 관리자 가드 통과 |
| 5 | `admin.scrape_control_page 진입: user_id=... has_flash=...` | 라우트 본체 진입 |
| 6 | `session_scope open` → `session_scope commit` → `session_scope close` | 관리자 페이지가 여는 DB 트랜잭션 |
| 7 | `admin.scrape_control_page DB 조회 완료: running=... recent_count=... sources_count=...` | DB 조회 완료 |
| 8 | `request 완료: method=GET path=/admin/scrape status=200 duration_ms=...` | 응답 송출 |
| 예외 | `request 실패(예외 전파): ...` → `미처리 예외 발생: ... exc_type=...` (+ stack trace) | 라우트 또는 하위 계층에서 예외 |

예컨대 `admin_user_required: 통과` 로그가 없고 `admin_user_required: 비관리자 로그인 → 403` 로그가
대신 나온다면 해당 사용자의 `is_admin` 이 꺼져 있다는 뜻이다. `admin.scrape_control_page 진입` 까지만
찍히고 `DB 조회 완료` 가 없다면 그 사이 DB 쿼리에서 예외가 난 것이다 (stack trace 가 곧이어 찍힘).

### DEBUG 시 주의

- `diagnose=True` 가 함께 켜져 traceback 에 **로컬 변수 값이 inline 으로 노출**된다. 이는 디버깅에는
  좋지만 세션 토큰·비밀번호·개인정보가 담긴 변수가 stderr 로 흘러나갈 수 있으니 운영에서는 DEBUG 를
  오래 켜 두지 않는다 (원인 파악 후 다시 INFO 로 복귀).
- sqlalchemy/alembic 로거는 log_level 과 무관하게 **최소 INFO 하한** 이 고정돼 있다. raw SQL 전체를
  보고 싶다면 `app/db/session.py` 의 `_build_engine` 에서 `echo=True` 로 바꾸고 재빌드·재기동해야
  한다 (운영에선 권장하지 않음 — 로그가 수천 줄로 불어난다).

---

## DB 관리

### DB 파일 위치

기본 경로: `./data/db/app.sqlite3` (호스트 볼륨 마운트)

`.env` 의 `DB_URL` 값으로 변경 가능하다.

### 백업

#### 자동 백업 스크립트 (권장)

일상 운영에서는 `scripts/backup_db.py` 를 사용한다 — SQLite 온라인 백업
(`sqlite3.connect().backup()`) 을 수행하므로 스크래퍼 실행 중에도 일관된
스냅샷이 만들어진다.

```bash
# 기본: data/backups/ 에 UTC 타임스탬프 파일명으로 저장, 최근 14개 보관
docker compose run --rm scraper python scripts/backup_db.py

# 보관 개수 / 저장 위치 변경 예
docker compose run --rm scraper python scripts/backup_db.py --keep 30
docker compose run --rm scraper python scripts/backup_db.py --dest /mnt/backups
```

**동작 요약**:

- 저장 위치: `./data/backups/` (기본, `--dest` 로 변경 가능)
- 파일명: `app.sqlite3.YYYYMMDDThhmmssZ.bak` (UTC 타임스탬프, 충돌 없음)
- 보관 정책: mtime 내림차순 정렬 후 최근 `--keep` 개(기본 **14**)만 유지,
  나머지는 자동 삭제 (= 매일 1회 실행 시 약 2주간 복원 지점 보장)
- `DB_URL` 이 `sqlite:///` 가 아니면(예: Postgres) INFO 로그만 남기고 skip
  (종료 코드 0)

**정기 실행 권장**: 스크래퍼 실행 직전 또는 직후, 호스트 cron / systemd timer
에서 하루 1회 실행을 권장한다.

```bash
# 예: /etc/cron.d/gov-project-backup
0 2 * * * cd /path/to/repo && docker compose run --rm scraper python scripts/backup_db.py >> /var/log/gov-backup.log 2>&1
```

#### 복원 절차

> **주의**: 복원 전 웹 서버(`app` 서비스)를 먼저 중지해 파일 잠금을 해제한다.

```bash
# 1) 웹 서버 중지 (스크래퍼 실행 중이면 종료 대기)
docker compose down app

# 2) 복원하려는 백업을 운영 경로로 덮어쓰기
cp ./data/backups/app.sqlite3.20260422T150000Z.bak ./data/db/app.sqlite3

# 3) 웹 서버 재기동
docker compose up app
```

복원 직후 웹 UI 에서 `/` 페이지가 정상 렌더링되는지 확인한다.

#### 수동 파일 복사 (소량·임시)

개발 단계나 일회성 백업은 그대로 `cp` 를 써도 된다 (SQLite 는 단일 파일):

```bash
cp ./data/db/app.sqlite3 ./data/db/app.sqlite3.bak.$(date +%Y%m%d)
```

다만 스크래퍼가 실행 중이면 WAL 로그와의 일관성이 깨질 수 있으므로 자동
스크립트를 권장한다.

> **Postgres 전환 시**: `scripts/backup_db.py` 는 SQLite 전용이다.
> Postgres 로 전환한다면 `pg_dump` / `pg_basebackup` 등 Postgres 공식 도구를
> 별도로 운영한다 (이 스크립트는 skip 으로 빠진다).

### DB 초기화 (전체 삭제 후 재시작)

```bash
# 데이터 완전 삭제 후 다음 실행 시 자동으로 스키마가 재생성된다
rm -f ./data/db/app.sqlite3
# sources.yaml 에서 max_pages: 1 설정 후 실행하면 스키마 생성 + 데이터 재수집
docker compose --profile scrape run --rm scraper
```

> **주의**: 삭제 전에 반드시 백업을 먼저 생성한다.

### 스키마 마이그레이션 (Alembic)

신규 코드로 업데이트한 후 컨테이너 기동 시 **자동으로** Alembic migration 이 적용된다.
별도 명령이 필요 없다.

**적용 전략 (자동 분기)**

| DB 상태 | 전략 | 효과 |
|---------|------|------|
| 빈 DB | `upgrade head` | baseline 스키마 전체 생성 |
| 기존 DB (Alembic 도입 전) | `stamp head` | 데이터 무변경, 리비전 레코드만 삽입 |
| Alembic 관리 DB | `upgrade head` | 신규 migration 적용, 없으면 no-op |

수동으로 Alembic 상태를 확인하려면:

```bash
# 현재 적용된 리비전 확인
docker compose run --rm scraper alembic current

# 적용 이력 확인
docker compose run --rm scraper alembic history --verbose

# 수동 upgrade (자동 적용 실패 시)
docker compose run --rm scraper alembic upgrade head
```

### canonical backfill (일회성)

기존 수집 데이터에 canonical_group_id가 채워지지 않은 경우(00013 적용 이전 수집분) 한 번 실행한다.
이미 채워진 row는 건너뛰므로 **멱등** — 실수로 두 번 실행해도 안전하다.

```bash
# 1) dry-run 으로 대상 건수 확인 (DB 변경 없음)
docker compose run --rm scraper python scripts/backfill_canonical.py --dry-run

# 2) 실제 실행 (200건마다 commit)
docker compose run --rm scraper python scripts/backfill_canonical.py --batch-size 200
```

신규 DB(00013 이후 설치)는 첫 수집 시부터 자동으로 canonical이 채워지므로 이 스크립트를 실행하지 않아도 된다.

### 이력 데이터 정리 (선택)

`is_current=False` 인 구버전 row가 누적될 경우 아래로 정리할 수 있다:

```bash
# 주의: 이 SQL은 되돌릴 수 없다. 백업 후 실행한다.
sqlite3 ./data/db/app.sqlite3 "DELETE FROM announcements WHERE is_current = 0;"
```

---

## 트러블슈팅

### admin 페이지에서 500 에러가 나는데 docker logs 가 비어 있다

00030 이전 버전에서 발생하던 증상. 현재 버전에서는 loguru↔stdlib 브리지가 설치돼 있어 500 에러가 나면
아래 두 줄이 반드시 남도록 돼 있다.

```
... | WARNING  | req=<...> | app.web.observability:...: request 실패(예외 전파): ...
... | ERROR    | req=<...> | app.web.observability:_log_unhandled_exception:... : 미처리 예외 발생: ...
Traceback (most recent call last):
  ...
```

로그가 여전히 비어 보인다면 다음을 순서대로 확인한다:

1. `LOG_LEVEL` 이 `CRITICAL` 또는 그 이상으로 설정돼 있어 에러 외 모든 로그가 숨겨졌는지.
2. 기동 첫 줄에 `로깅 초기화 완료: log_level=... diagnose=... stdlib_bridge=installed` 가 찍혔는지.
   없다면 `configure_logging()` 이 호출되지 않는 옛 이미지가 떠 있다는 뜻 — `docker compose build`
   후 재기동.
3. 컨테이너를 막 기동한 뒤라면 docker log 버퍼가 아직 비어있을 수 있다 —
   `docker logs -f iris-agent-web` 로 **실시간 follow** 상태에서 다시 요청해 본다.

구체적으로 어느 단계에서 막혔는지 좁히려면 `LOG_LEVEL=DEBUG` 로 재기동 후
"웹 로그 레벨 제어와 원인 추적" 섹션의 체크리스트를 따라간다.

### 기동 시 "sources.yaml 마운트 없음 — template 기본값으로 기동합니다" 경고가 출력된다

호스트 루트에 `sources.yaml` 이 없는 경우 entrypoint 가 이미지 내 template 을 폴백으로 사용해
계속 기동한다. 수집 파라미터를 실제 설정으로 반영하려면 아래 순서로 진행한다:

```bash
# 1) sources.yaml 생성
sh scripts/bootstrap_sources.sh

# 2) 필요 시 편집
vim sources.yaml   # 또는 선호하는 에디터

# 3) 컨테이너 재기동 (바인드 마운트가 갱신된 파일을 읽는다)
docker compose restart app
```

> `sources.yaml` 은 `.gitignore` 대상이므로 저장소에 포함되지 않는다.
> 처음 clone 한 후 또는 `./data` 를 초기화한 후에 이 경고가 나타날 수 있다.

### 공고 목록이 비어 있다

1. `sources.yaml` 에서 `scrape.log_level: DEBUG`, `scrape.max_pages: 1` 설정 후
   `docker compose --profile scrape run --rm scraper` 실행하여 수집 로그 확인
2. `목록 수집 완료: source=IRIS 0건` 이면 IRIS 사이트 응답 이상 → 브라우저에서 직접 확인
3. DB에 데이터는 있는데 웹 UI에 안 보이면 → `is_current=1` 조건 확인

```sql
SELECT COUNT(*) FROM announcements WHERE is_current = 1;
```

### 재실행해도 상세 수집이 계속 발생한다

- 종료 로그의 `apply action 분포: 신규=N 변경없음=N` 에서 `변경없음` 이 0에
  가까우면 변경 감지가 오동작 중이다.
- 수집 단계 로그의 `상세 수집 생략(본 테이블에 unchanged + detail 보유)` 가
  없고 매번 상세를 수집하면:
  - `detail_fetched_at` 이 NULL인 row가 있는지 확인 (상세가 아직 한 번도 수집 안 된 경우)
  - 비교 필드(title/status/deadline_at/agency)가 매 수집마다 달라지는지 확인
  - `peek_main_can_skip_detail` 의 read-only SELECT 가 본 테이블의 동일 row 를
    찾는지 확인 (Phase 5a 의 detail-skip 최적화는 본 테이블 기준)

```sql
SELECT id, title, deadline_at, detail_fetched_at
FROM announcements WHERE is_current = 1
ORDER BY id LIMIT 10;
```

> **[00007 이후]** deadline_at tz-naive/aware 불일치 및 문자열 공백 차이로 인한 false-positive 변경 감지가 수정됐다.
> 위 증상이 여전히 발생하면 `scrape.log_level: DEBUG` 로 실행하여 어떤 공고가 `created`/`new_version`으로 판정되는지 확인한다.

### DB 스키마 오류 (`no such column: is_current`)

기존 DB에 Alembic migration 이 적용되지 않은 경우다.
다음 명령으로 수동 적용한다:

```bash
docker compose run --rm scraper alembic upgrade head
```

그래도 해결되지 않으면 DB를 백업 후 삭제해 새로 생성한다.

### Docker 컨테이너에서 권한 오류

`./data/` 디렉터리의 소유자/권한을 확인한다:

```bash
ls -la ./data/
chmod -R 755 ./data/
```

### 로그에 `상태 전이 — in-place 갱신` 이 자주 나타난다

접수예정·접수중·마감 3개 상태를 순차 수집하므로 동일 공고가 다른 상태로 재등장하면 정상적으로 발생한다.
비정상적으로 많은 경우(예: 매 실행마다 같은 공고가 계속 상태 전이로 잡히는 경우) `docs/status_transition_todo.md` 를 참고한다.

### 첨부파일이 다운로드되지 않는다

1. 로그에서 `첨부 수집` 관련 라인을 확인한다.
2. `attachment_errors` 키가 있는지 DB에서 확인한다:
   ```sql
   SELECT id, source_announcement_id, json_extract(raw_metadata, '$.attachment_errors')
   FROM announcements
   WHERE is_current = 1 AND raw_metadata LIKE '%attachment_errors%';
   ```
3. Playwright 브라우저가 설치되어 있는지 확인한다:
   - `docker compose build` 후 `playwright install chromium` 스텝 로그 확인
4. `sources.yaml` 에서 `scrape.skip_attachments: false` 설정 후 재실행하면 이전에 실패한 항목을 재시도한다.

### 웹 UI에서 첨부파일 다운로드 링크가 404를 반환한다

`stored_path` 가 가리키는 파일이 실제로 존재하지 않는 경우다.
스크래퍼를 재실행해 파일을 다운로드하거나, DB의 해당 `attachments` 레코드가 유효한지 확인한다:

```sql
SELECT id, original_filename, stored_path FROM attachments
WHERE announcement_id = {공고_id};
```

---

## 정기 운영 체크리스트

### 매 수집 후 확인

- [ ] 종료 코드 0 확인 (`echo $?`)
- [ ] `scrape 실행 완료` 로그에서 `delta INSERT 실패` 건수 = 0 확인
- [ ] `scrape 실행 완료` 로그에서 `첨부 다운로드 실패` 건수 확인 (0이면 정상)
- [ ] `apply_delta_to_main 트랜잭션 commit 완료` 로그 확인 — 본 테이블 / snapshot 영구화 시점
- [ ] `delta_announcements` 잔여 0 확인:
      `sqlite3 ./data/db/app.sqlite3 "SELECT COUNT(*) FROM delta_announcements;"`
- [ ] 웹 UI(`http://localhost:8000`) 에서 최신 공고 표시 확인

### 주간 확인

- [ ] DB 파일 크기 확인 (`ls -lh ./data/db/app.sqlite3`)
- [ ] 구버전 row 누적 확인 (`SELECT COUNT(*) FROM announcements WHERE is_current=0;`)
- [ ] 로그에 반복 `WARNING` 메시지 없는지 확인
- [ ] `data/downloads/` 의 고아 파일 정리 — `--dry-run` 후보 검토 + 자동 cron
      (`add_gc_orphan_cron_schedule`) 등록 여부 확인. 등록되어 있으면 KST 04:00
      마다 자동 실행되며 별도 조치 불필요

### 업데이트 후 확인

- [ ] `docker compose build` 로 이미지 재빌드
- [ ] `docker compose run --rm scraper alembic current` 로 리비전이 `head` 임을 확인
- [ ] `sources.yaml` 에서 `scrape.dry_run: true`, `scrape.max_pages: 1` 설정 후
      `docker compose --profile scrape run --rm scraper` 로 기본 동작 확인
- [ ] 웹 UI 목록 페이지 정상 렌더링 확인
