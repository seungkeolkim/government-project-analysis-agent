# Phase 2 — 웹 수집 제어 + 관리자 페이지 + 스케줄러 설계 노트

> **작성 범위**: Task 00025 (Phase 2) — 웹에서 `docker compose CLI` 없이
> 스크래핑을 실행/중단/스케줄할 수 있게 하고, `sources.yaml` 편집기와
> [스케줄] 탭을 관리자 전용 페이지에 추가한다.
>
> Phase 1a 에서 깔린 `scrape_runs` 테이블과 Phase 1b 의 `User.is_admin` / 세션
> 인증 / `ensure_same_origin` 체크를 그대로 **이어서** 사용한다. 이 문서는
> Task 00025 의 후속 subtask(00025-2 ~ 00025-7) 가 그대로 인용할 수 있는 §
> 번호 체계를 **안정적으로** 부여하는 것이 최우선 목적이다.
>
> 구현 본문(실제 함수 바디)은 포함하지 않는다. 각 모듈의 인터페이스
> 시그니처(함수 이름 + 파라미터 타입 + 반환 타입) 수준만 기술한다.

---

## §1. 스코프와 전제

### §1.1 이 task 에서 다루는 것

- `app/web/routes/admin.py` (신규) — 관리자 페이지의 진입점. 탭 3개:
  [수집 제어] / [sources.yaml] / [스케줄].
- `app/scrape_control/` 패키지 (신규) — 스크래퍼 subprocess 기동/중단,
  ScrapeRun row 갱신, stale cleanup 헬퍼.
- `app/auth/dependencies.py` — `admin_user_required` Depends 추가(기존
  `current_user_required` 위에 `is_admin` 검사 합류).
- `app/db/models.py` — `ScrapeRun` ORM 추가 (DB 테이블은 Phase 1a migration
  에서 이미 존재 — DDL 변경 없음).
- `app/cli.py` — SIGTERM handler 추가. 진행 중 공고 1건을 마무리(commit)한
  뒤 깨끗이 종료하고, ScrapeRun.status 를 `cancelled` 로 기록.
- `app/scheduler/` 패키지 (신규) — APScheduler BackgroundScheduler 래퍼.
  웹 프로세스 내부에서 가동하고 SQLite jobstore 로 재시작 복원.
- `app/web/templates/admin/` (신규) — 탭 3개의 Jinja2 템플릿.
- `docker-compose.yml` — `app` 서비스에 `/var/run/docker.sock` 바인드 마운트
  추가 + docker group gid 전달.
- `docker/Dockerfile` — docker CLI + compose v2 plugin 설치 (§5 비교 참조).
- `pyproject.toml` — `apscheduler>=3.10,<4.0` 의존성 추가.
- 문서: `docs/scrape_control_design.md`(본 문서), `README.USER.md` "웹 기반
  수집 제어" 섹션.

### §1.2 이 task 에서 다루지 않는 것 (범위 밖)

- **사용자 목록 / 계정 관리 UI** — Phase 5. 본 task 에서 `is_admin` 부여는
  `scripts/create_admin.py` + DB 직접 수정에만 의존한다.
- **AuditLog UI 열람** — Phase 5. 본 task 에서 AuditLog 는 쓰지 않는다.
- **sources.yaml 편집 이력 열람** — Phase 5. 백업은 이번 task 에서 만들되
  UI 조회는 Phase 5 가 담당한다.
- **CanonicalOverride / 병합·분할 UI** — Phase 5.
- **관련성 bulk mark / 즐겨찾기** — Phase 3a / 3b.
- **SMTP 발송 / 이메일 알림** — Phase 4.
- **다중 스크래퍼 병렬 실행** — 본 task 는 "running row 1개" 잠금으로
  명시적 금지. 향후 소스별 병렬이 필요해지면 lock 전략 재설계.
- **SSE / WebSocket** — 사용자 원문에 의해 5초 폴링만 사용.

### §1.3 절대 건드리지 않는 것 (회귀 금지)

- 기존 CLI 경로: `docker compose --profile scrape run --rm scraper` 가
  `ScrapeRun.trigger='cli'` 로 기록되며, 웹 UI 잠금과 **동등하게** 운영된다
  (§7.3 lock 규칙). 웹이 없어도 CLI 로 수집할 수 있는 운영 경로는 유지.
- 기존 비로그인 열람 경로(`/`, `/announcements/{id}`, `/attachments/...`).
- `Announcement` / `Attachment` / `User` / `AnnouncementUserState` ORM.
- Phase 1a baseline / phase1a migration 파일. `scrape_runs` 테이블 DDL.

---

## §2. 현재 구조 요약 (탐사 결과)

### §2.1 스크래퍼 실행 경로

`app/cli.py` → `_async_main()` → `_orchestrate()` → 소스별
`_run_source_announcements()` → 공고 1건 루프. 공고 1건 단위로
`session_scope()` 컨텍스트를 열고 try/except 로 격리한다 (Phase 1a 유지).

증분 4-branch UPSERT 와 변경 시 리셋은 repository 계층에서 모두 같은
트랜잭션으로 수행된다 — 본 task 는 이 원자성을 **깨지 않는다**.

### §2.2 scrape_runs 테이블 (Phase 1a, 이미 존재)

`alembic/versions/20260422_1500_b2c5e8f1a934_phase1a_new_tables.py` §12.

| 컬럼 | 타입 | 제약 |
|---|---|---|
| id | Integer PK |  |
| started_at | DateTime(tz) | NOT NULL |
| ended_at | DateTime(tz) | NULL |
| status | String(16) | CHECK in ('running','completed','cancelled','failed','partial') default 'running' |
| trigger | String(16) | CHECK in ('manual','scheduled','cli') NOT NULL |
| source_counts | JSON | NOT NULL |
| error_message | Text | NULL |
| pid | Integer | NULL |

인덱스: `ix_scrape_runs_started_at`, `ix_scrape_runs_status`.

### §2.3 관리자 게이트 현황

Phase 1b 에서 `User.is_admin` 컬럼만 도입되고, `admin_user` Depends 는
아직 없다. `app/auth/dependencies.py` 의 `current_user_required` 위에
얇게 덧대는 형태로 추가한다 (§10).

### §2.4 컨테이너 구조

- `app` 서비스: 포트 8000 FastAPI 웹. 현재 `/var/run/docker.sock` 이 **없음**.
- `scraper` 서비스: `profiles: [scrape]` 로 격리, `docker compose --profile
  scrape run --rm scraper` 로만 기동. 현재 `app` 과 동일 이미지.

두 서비스 모두 `./data`, `./app`, `./sources.yaml` 마운트를 공유한다.

### §2.5 sources.yaml 위치

- 호스트 `./sources.yaml` — 편집 대상 (.gitignore).
- 컨테이너 `/run/config/sources.yaml` — 컨테이너 내부 read-only 바인드.
- `entrypoint.sh` 가 per-run `mktemp` 복사본으로 격리하고
  `SOURCES_CONFIG_PATH` 주입.
- 백업 디렉터리: 이번 task 에서 신설 — `data/backups/sources/`.

---

## §3. 모듈 배치 (신규/수정 파일 지도)

후속 subtask 의 "건드릴 파일" 레퍼런스. **이 목록 밖의 파일은 변경하지
않는다** (기존 스크래퍼·viewer 템플릿·migration 등).

### §3.1 신규 생성

```
app/scrape_control/__init__.py           # 공개 심볼 re-export
app/scrape_control/runner.py             # subprocess 기동/중단, ScrapeRun 갱신
app/scrape_control/lock.py               # running row 잠금 + stale cleanup
app/scrape_control/logs.py               # subprocess stdout/stderr → 로그 파일
app/scrape_control/cancel.py             # SIGTERM 전파 헬퍼 (프로세스 그룹)

app/scheduler/__init__.py
app/scheduler/service.py                 # APScheduler wrapper (start/stop, add/remove/toggle)
app/scheduler/job_runner.py              # 스케줄 트리거 → runner.start_scrape_run 호출

app/web/routes/__init__.py               # routes 서브패키지 선언
app/web/routes/admin.py                  # APIRouter(prefix="/admin")
app/web/templates/admin/base.html        # 3개 탭 공통 레이아웃
app/web/templates/admin/control.html     # [수집 제어] 탭
app/web/templates/admin/sources.html     # [sources.yaml] 탭
app/web/templates/admin/schedule.html    # [스케줄] 탭

docs/scrape_control_design.md            # 본 문서

tests/scrape_control/__init__.py
tests/scrape_control/test_lock.py
tests/scrape_control/test_runner.py
tests/scrape_control/test_cancel_sigterm.py
tests/scheduler/__init__.py
tests/scheduler/test_service.py
tests/web/test_admin_routes.py
```

### §3.2 수정

```
app/auth/dependencies.py                 # admin_user_required Depends 추가
app/db/models.py                         # ScrapeRun ORM 추가 (DDL 변경 없음)
app/db/repository.py                     # §7.2 헬퍼 추가 (running row 조회 등)
app/cli.py                               # SIGTERM handler + ScrapeRun 기록 (trigger='cli')
app/web/main.py                          # admin_router mount + startup stale cleanup + scheduler.start()
app/web/templates/base.html              # 상단 네비 "관리자" 링크(로그인 + is_admin 시)

docker-compose.yml                       # app 서비스에 docker.sock 마운트 + group_add
docker/Dockerfile                        # docker CLI + compose plugin 설치

pyproject.toml                           # apscheduler 추가

README.USER.md                           # "웹 기반 수집 제어" 섹션
PROJECT_NOTES.md                         # MemoryUpdater 가 finalize 에서 갱신 (수동 수정 X)
```

### §3.3 Subtask 매핑

| subtask | 커버 범위 |
|---|---|
| 00025-1 | 본 문서 — 코드 변경 없음 |
| 00025-2 | §5 인프라(docker.sock 마운트 / docker CLI / apscheduler 의존성) |
| 00025-3 | §7 ScrapeRun ORM / §9 runner / §8 cli SIGTERM / §10 admin_user_required |
| 00025-4 | §11 /admin 라우트 + [수집 제어] 탭 + §7.4 startup stale cleanup |
| 00025-5 | §12 [sources.yaml] 탭 |
| 00025-6 | §13 [스케줄] 탭 + APScheduler 통합 |
| 00025-7 | README.USER.md "웹 기반 수집 제어" 섹션 |

---

## §4. 실행 아키텍처 — web 프로세스 + subprocess

### §4.1 요청 흐름 (수동 실행)

```
브라우저
   │ POST /admin/scrape/start    (Form: active_sources=[], trigger='manual')
   ▼
FastAPI (app 컨테이너, uvicorn)
   │ admin_user_required → current_user.is_admin 검증
   │ scrape_control.lock.acquire()  — running row 없음 확인
   │ scrape_control.runner.start_scrape_run(...)
   │     - ScrapeRun row INSERT (status='running', pid=NULL) — commit
   │     - subprocess.Popen([...], start_new_session=False, preexec_fn=os.setpgrp)
   │     - ScrapeRun.pid = popen.pid — commit
   │ (호출은 await 하지 않음 — background task 로 상태 watch)
   ▼
docker compose --profile scrape run --rm scraper
   │ (app 컨테이너 내부에서 호스트 dockerd 를 socket 으로 조작)
   ▼
호스트 dockerd
   │
   ▼ (신규 컨테이너 기동)
iris-agent-scraper (python -m app.cli)
   │ SIGTERM handler 등록, _async_main() 실행
   │ 공고 루프에서 KeyboardInterrupt/SIGTERM 을 확인
   ▼
수집 완료 → exit 0 → docker compose 가 컨테이너 제거 → subprocess 종료
   │
   ▼
runner.watch_subprocess() (백그라운드 Task) 이 subprocess.wait() 후
ScrapeRun.status 를 returncode 로 최종 분기:
    0   → 'completed'
    130 → 'cancelled' (SIGINT — 사용 안 함, SIGTERM 경로만 공식)
    -15 → 'cancelled' (SIGTERM 으로 종료된 경우)
    그 외 → 'failed' (error_message 기록)
```

### §4.2 중단 흐름

```
브라우저
   │ POST /admin/scrape/cancel
   ▼
FastAPI
   │ admin_user_required
   │ scrape_control.cancel.request_cancel(scrape_run_id)
   │     - SELECT scrape_runs WHERE id=? AND status='running'
   │     - ScrapeRun.ended_at 은 아직 건드리지 않음
   │     - os.killpg(os.getpgid(pid), SIGTERM)
   │         → 프로세스 그룹 전체로 전파
   │         → docker compose (CLI) 가 이를 받아 관리 컨테이너에 SIGTERM 릴레이
   │         → 컨테이너 PID 1 (python -m app.cli) 이 SIGTERM 수신
   ▼
app.cli 의 SIGTERM handler:
   │ cancel_requested = True 로 플래그만 세팅 (공고 1건 한복판에서 중단 금지)
   │ 현재 공고의 첨부 다운로드 완료 → upsert 트랜잭션 commit
   │ 다음 루프 진입 시 플래그 확인 → break
   │ orchestrator return → ScrapeRun 이 아직 열려 있으므로 status='cancelled' 기록
   │ exit 0
```

### §4.3 경계 조건

- **subprocess detach 금지**: `start_new_session=True` 는 SIGTERM 전파가
  깨지므로 사용하지 않는다. 프로세스 그룹은 `preexec_fn=os.setpgrp`
  (POSIX) 로 두고, 부모(app 프로세스)가 `killpg` 로 보낸다.
- **app 프로세스가 재시작되면 subprocess 는 고아**: web 프로세스가 이미
  죽으면 SIGTERM 을 못 보내므로, subprocess 도 자체 종료하지 않으면
  scrape_runs 에 pid 는 남지만 웹이 관리하지 못하는 상태가 된다. 이
  케이스가 §7.4 stale cleanup 의 대상.
- **docker compose CLI 가 SIGTERM 을 받으면 내부적으로 관리 컨테이너에
  signal 을 전달한다** (compose v2 공식 동작). compose v1 은 동작이 달라
  우리는 compose v2 plugin 으로만 설치한다 (§5).
- **웹 응답과 subprocess 수명 분리**: `/admin/scrape/start` 는 ScrapeRun
  INSERT + Popen 기동까지만 동기 처리하고 즉시 응답한다. subprocess
  종료 watcher 는 `asyncio.create_task` 또는 별도 스레드로 띄운다.
  사용자는 5초 폴링으로 진행을 확인한다.

---

## §5. 호스트 docker 접근 — docker CLI in app 이미지 + socket mount

### §5.1 선택지 비교

| 옵션 | 설치 명령 | 이미지 크기 증가 | 비고 |
|---|---|---|---|
| (A) `docker.io` (Debian 저장소) | `apt-get install -y docker.io docker-compose-plugin` 가 이미지마다 가능하지 않음 — Debian bookworm 기본 저장소에는 `docker-compose-plugin` 이 없어 compose v1 스크립트(`docker-compose`) 로 떨어진다. compose v2 는 Docker 공식 저장소를 추가해야만 얻을 수 있다 | ~200 MB | dockerd 서버까지 들어오며 불필요한 용량 — 우리는 클라이언트만 필요 |
| (B) `docker-ce-cli` + `docker-compose-plugin` (Docker 공식 저장소) | ```curl -fsSL https://download.docker.com/linux/debian/gpg \| gpg --dearmor -o /etc/apt/keyrings/docker.gpg && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list && apt-get update && apt-get install -y docker-ce-cli docker-compose-plugin``` | ~80–100 MB | 클라이언트만. compose v2 plugin 공식 지원. **선택** |
| (C) `docker compose` plugin 단독 tarball | GitHub release 에서 `docker-compose` static binary 다운로드 | ~50 MB | `docker` CLI 가 없으면 compose 가 `docker` 를 호출하므로 함께 필요. CLI 없이는 불가 |

**결정: (B)**. 이유:
- compose v2 는 Docker 공식 저장소에서만 일관된 버전으로 얻을 수 있고, SIGTERM
  전파 동작 호환성이 검증되어 있다.
- (A) 대비 이미지 용량을 약 100 MB 절약.
- (C) 는 plugin 단독 설치가 가능하지만 `docker` CLI 바이너리가 별도로
  필요해 결국 (B) 와 유사한 크기가 된다.

### §5.2 `/var/run/docker.sock` 마운트

```yaml
# docker-compose.yml  app 서비스 추가분
services:
  app:
    # ...
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    group_add:
      - "${HOST_DOCKER_GID:-999}"
```

- `HOST_DOCKER_GID` 는 호스트의 `getent group docker` gid 값.
  `.env.example` 에 문서화하고, 미지정 시 Debian 기본값 999 로 fallback.
- app 컨테이너 안에서는 `docker ps` 로 호스트의 컨테이너 목록이 보여야
  정상 동작 확인. 실패 시 "Permission denied on docker.sock" 에러는
  `HOST_DOCKER_GID` 값이 호스트와 불일치한다는 신호.

### §5.3 docker-in-docker 와의 차이 (회피 근거)

- **docker-in-docker(DinD)** 는 컨테이너 안에 dockerd 를 또 띄우는 방식.
  privileged mode 가 필요하고, 호스트 네트워크/볼륨과 분리되므로 현재
  compose 가 관리하는 `iris-agent-scraper` 컨테이너와 `./data` 볼륨 공유가
  깨진다.
- **호스트 docker.sock 마운트(선택)** 는 app 컨테이너가 호스트 dockerd 를
  조작해 `iris-agent-scraper` 를 기동한다. 데이터/설정 마운트가 호스트 기준
  으로 일관되게 적용된다.
- **compose 프로젝트 인식**: `docker compose` 는 기본적으로 `COMPOSE_FILE` +
  `COMPOSE_PROJECT_NAME` 환경변수 또는 cwd 의 `docker-compose.yml` 로
  프로젝트를 식별한다. app 컨테이너 안에 `docker-compose.yml` 이 **없으므로**
  `-f /host/project/docker-compose.yml -p iris-agent` 플래그를 명시해야 한다.
  `HOST_PROJECT_DIR` 를 env 로 주입하고, app 컨테이너에
  `${HOST_PROJECT_DIR}:/host/project:ro` 를 마운트해 접근한다.
- 즉 subprocess 실제 명령은 다음 형태가 된다:
  ```
  docker -H unix:///var/run/docker.sock compose \
    -f /host/project/docker-compose.yml \
    -p iris-agent \
    --profile scrape run --rm scraper
  ```
  구체 명령 포맷은 §9 runner 시그니처에서 재확인한다.

### §5.4 운영 시 재빌드 조건

- Dockerfile 에 docker CLI 를 추가하면 기존 `iris-agent:dev` 이미지가
  바뀌므로 `docker compose build` 가 반드시 필요. 기존 이미지 캐시는
  compose v2 plugin 을 설치하는 RUN 레이어에서 무효화된다.

---

## §6. subprocess signal 전파 (SIGTERM → compose → scraper 컨테이너)

### §6.1 전제

- 부모: app 컨테이너 안 uvicorn worker. FastAPI 요청 핸들러가
  `subprocess.Popen` 을 호출하고 pid 를 ScrapeRun.pid 에 기록한다.
- 자식: `docker compose run --rm scraper` 프로세스(= docker CLI).
- 손자: 그 안에서 기동된 `iris-agent-scraper` 컨테이너 PID 1 (`python -m app.cli`).

### §6.2 프로세스 그룹 전략

- `Popen(..., preexec_fn=os.setpgrp)` — 자식을 새 프로세스 그룹 leader 로
  만든다. pgid == pid.
- 중단 시 `os.killpg(ScrapeRun.pid, signal.SIGTERM)` 호출 — 부모 프로세스
  그룹 전체(docker CLI 및 그 자식)에 SIGTERM.
- docker CLI (compose v2) 가 SIGTERM 을 받으면 관리 컨테이너에 SIGTERM 을
  **릴레이**한다(compose v2 공식 동작). 컨테이너 PID 1 의 `python -m app.cli`
  이 수신하게 된다.

### §6.3 `app.cli` 쪽 수신

- `signal.signal(SIGTERM, _on_sigterm)` 등록.
- 핸들러는 전역 flag 만 세팅 (`_cancel_requested = True`). 실제 루프 이탈은
  공고 단위 체크에서 수행한다. 공고 1건의 upsert 트랜잭션을 **중간에 끊지
  않는다** — Phase 1a 의 atomic 보장(변경 시 리셋 + UPSERT 같은 트랜잭션)을
  깨지 않기 위함.
- 현재 공고의 첨부 다운로드 + 2차 감지 + commit 까지 끝낸 뒤 `break`.
- orchestrator return 후 `_async_main()` 은 정상 exit(0). ScrapeRun watcher
  가 returncode=0 이지만 `_cancel_requested` 가 세팅되었음을 파일 또는
  별도 메커니즘으로 전달할 수 없으므로, **상태 판정은 부모(runner) 측
  killpg 호출 여부로 결정**한다 (§9.2 `_expected_status`).

### §6.4 수신 지연이 받을 수 있는 이유

- 첨부 다운로드는 Playwright headless chromium 프로세스 때문에 한 공고
  처리에 수십 초 걸릴 수 있다. 사용자에겐 "중단 요청 수락" 만 즉시
  응답하고, 실제 `ScrapeRun.status='cancelled'` 반영은 watcher 가 process
  종료를 본 뒤에 한다. UI 는 5초 폴링으로 자연스럽게 갱신된다.

### §6.5 SIGKILL 사용 여부

- 사용하지 않는다. SIGKILL 은 공고 1건의 트랜잭션을 중간에 끊어 UPSERT
  무결성을 깬다(특히 2차 감지 direction). 사용자 원문 요구: "공고 마무리
  후 종료".
- 관리자가 정말 프로세스를 죽이고 싶다면 호스트 쉘에서
  `docker kill iris-agent-scraper` 를 직접 해야 한다 — 운영 노트 수준으로
  README.USER.md 에 적는다.

---

## §7. ScrapeRun ORM + 동시성 (lock, stale cleanup)

### §7.1 `ScrapeRun` ORM (`app/db/models.py`)

DB 테이블은 이미 존재. ORM 선언만 추가.

```python
class ScrapeRun(Base):
    """수집 실행 1회 요약. Phase 2 에서 CLI/수동/스케줄 3경로 공통으로 기록."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    source_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

컬럼명/인덱스명/CHECK 이름은 phase1a migration 과 정확히 일치.

### §7.2 repository 헬퍼 (`app/db/repository.py` 추가)

```python
def get_running_scrape_run(session: Session) -> ScrapeRun | None:
    """status='running' 인 row 를 0 또는 1개 반환. 2개 이상이면 stale 상태."""

def list_recent_scrape_runs(session: Session, *, limit: int = 20) -> list[ScrapeRun]:
    """started_at 내림차순 최근 N개."""

def create_scrape_run(
    session: Session, *, trigger: str, source_counts: dict[str, Any] | None = None,
) -> ScrapeRun:
    """status='running' 신규 row INSERT. pid 는 이후 set_scrape_run_pid 로 주입."""

def set_scrape_run_pid(session: Session, run_id: int, pid: int) -> None:
    """Popen 이후 pid 기록 (row 단위 UPDATE)."""

def finalize_scrape_run(
    session: Session, run_id: int, *,
    status: str,
    source_counts: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    """ended_at=now, status, source_counts, error_message 세팅. status 는
    'completed'|'cancelled'|'failed'|'partial' 중 하나."""

def fail_stale_running_runs(session: Session) -> int:
    """웹 startup 시 호출. pid 가 NULL 이거나 해당 pid 프로세스가 존재하지 않는
    running row 를 'failed' + error_message='stale (web restart)' 로 마감한다.
    반환값은 정리된 row 수."""
```

### §7.3 Lock 전략

- **Rule 1 — Single running row**: 어떤 경로(CLI/수동/스케줄) 든 기동 전에
  `get_running_scrape_run()` 이 None 임을 확인해야 한다. 존재하면
  HTTP 409 Conflict (웹) 또는 exit(2) (CLI) 로 거부.
- **Rule 2 — Atomic insert**: `create_scrape_run()` 과 "running row 없음"
  확인은 같은 트랜잭션(`session_scope`) 에서 수행한다. SQLite 의 기본
  serialized isolation 으로 insert 직전까지 SELECT-then-INSERT race 를
  방지한다. Postgres 전환 시 advisory lock 로 업그레이드하는 훅을
  주석으로 남긴다.
- **Rule 3 — CLI/웹 공통**: `app.cli` 도 `_async_main` 진입 초반에
  `create_scrape_run(trigger='cli')` 를 호출해 같은 lock 을 사용한다. 이로써
  "웹이 running 중일 때 누가 CLI 로도 기동했다" 케이스도 차단된다. CLI
  실행 종료 시 `finalize_scrape_run` 을 호출.

### §7.4 Startup stale cleanup

- `app/web/main.py` 의 FastAPI startup event 훅에서
  `fail_stale_running_runs(session)` 를 호출한다.
- 판정 규칙:
  - `pid IS NULL` — Popen 직전 크래시. 무조건 stale.
  - `pid IS NOT NULL` — `os.kill(pid, 0)` (signal 0) 로 존재 확인.
    `OSError(ESRCH)` 이면 stale.
  - pid 가 존재하더라도 app 이 재시작된 후에는 watcher 를 복원하지 못하므로
    **안전한 쪽으로 stale 판정**한다 — "app 재시작 이후의 running row 는
    모두 failed" 를 정책으로 삼는다. 프로세스가 실제로는 돌고 있더라도
    현재 웹 인스턴스가 중단 버튼을 통해 잡을 수 없기 때문에 고아가 된다.
  - `error_message='stale (web restart)'`.
- 이 정리는 scheduler 기동 **이전** 에 끝내야 한다 — 스케줄러가 running
  충돌로 skip 하지 않도록.

### §7.5 source_counts 스키마

```json
{
  "active_sources": ["IRIS", "NTIS"],
  "per_source": {
    "IRIS": {
      "list_success": 0, "list_failure": 0,
      "detail_success": 0, "detail_failure": 0, "skipped_detail": 0,
      "attachment_success": 0, "attachment_failure": 0, "attachment_skipped": 0,
      "action_counts": {"created": 0, "unchanged": 0, "new_version": 0, "status_transitioned": 0}
    }
  }
}
```

CLI 가 이미 생성하는 summary dict 를 재사용한다.

---

## §8. `app/cli.py` SIGTERM handler

### §8.1 합류 지점

`_async_main()` 진입부에 다음을 추가:
1. `signal.signal(signal.SIGTERM, _handle_sigterm)` 등록.
2. `create_scrape_run(trigger='cli')` 로 ScrapeRun row 생성.
3. orchestrator 시작.

### §8.2 인터페이스

```python
_cancel_event: asyncio.Event | None = None

def _handle_sigterm(signum: int, frame: Any) -> None:
    """SIGTERM 핸들러 — _cancel_event 를 set 한다. 루프 본문이 확인한다."""

def _should_cancel() -> bool:
    """공고 루프에서 호출. True 면 break."""
```

### §8.3 루프 체크 지점

- `_run_source_announcements` 의 `for row_index, row_metadata in ...` 루프
  **최상단**에서 `_should_cancel()` 확인 → True 면 break.
- 공고 1건 내부(첨부 루프 등)에서는 **확인하지 않는다**. 공고 단위 commit
  보장을 위함.
- `_orchestrate()` 의 소스 루프에서도 소스 1개가 끝날 때마다 확인 가능.

### §8.4 종료 판정 (CLI 측)

- 정상 종료: `finalize_scrape_run(status='completed')`.
- `_cancel_event.is_set()` 가 True 면 `status='cancelled'`.
- failure_count 가 success 대비 유의미(절반 이상 등) → `status='partial'`.
- 부트스트랩 실패 / 전역 예외 → `status='failed'` + error_message.
  `_async_main` 의 기존 종료 코드 체계(0/1/130)는 그대로 유지.

### §8.5 회귀 주의

- 기존 `KeyboardInterrupt` (SIGINT) 경로는 현 동작 유지. watcher(§9.2)가
  returncode 130 을 보면 `cancelled` 로 기록하되, SIGINT 는 사용자 원문에서
  **핵심 경로가 아니므로** finalize 호출은 best-effort.

---

## §9. `app/scrape_control/runner.py` 인터페이스

### §9.1 공개 시그니처

```python
@dataclass
class StartResult:
    """start_scrape_run 의 반환값. ScrapeRun id + subprocess pid."""
    scrape_run_id: int
    pid: int

def start_scrape_run(
    active_sources: list[str],
    *,
    trigger: Literal["manual", "scheduled"],
    initiator_user_id: int | None = None,
) -> StartResult:
    """웹 요청/스케줄 트리거로 스크래퍼 subprocess 를 기동한다.

    순서:
        1. lock.acquire_or_raise()  — 409 / RuntimeError 발생 가능
        2. create_scrape_run(trigger=trigger, source_counts={'active_sources': ...})
        3. subprocess.Popen(compose_command(active_sources), preexec_fn=os.setpgrp)
        4. set_scrape_run_pid(run_id, popen.pid)
        5. asyncio.create_task(_watch_subprocess(run_id, popen))
        6. return StartResult(...)

    active_sources=[] 이면 sources.yaml 의 enabled 전체 실행 (scrape 섹션과
    동일 의미). 지정하면 해당 source id 만 실행하도록 sources.yaml 의
    scrape.active_sources 를 per-run 덮어쓴다 (§9.3)."""

def compose_command(active_sources: list[str]) -> list[str]:
    """subprocess.Popen 에 전달할 argv. §5.3 포맷.
    환경: HOST_PROJECT_DIR, COMPOSE_PROJECT_NAME 를 참조."""

async def _watch_subprocess(run_id: int, popen: subprocess.Popen[bytes]) -> None:
    """subprocess.wait() 후 returncode 에 따라 finalize_scrape_run 호출.
    예외 발생 시도 status='failed' + error_message 기록."""
```

### §9.2 returncode → status 매핑 (`_expected_status`)

| 경로 | returncode | status |
|---|---|---|
| 정상 종료 | 0 | `completed` (CLI 가 이미 finalize 했으면 no-op) |
| SIGTERM 을 웹이 보냈고 CLI 가 정상 마무리 | 0 | `cancelled` (cancel.py 가 플래그 남김) |
| SIGTERM 에 의해 -15 로 종료 | -15 | `cancelled` |
| SIGKILL | -9 | `failed` error_message='killed' (운영자 개입 흔적) |
| 기타 | nonzero | `failed` + stderr 마지막 N바이트 저장 |

CLI 가 자체적으로 `finalize_scrape_run` 을 이미 호출한 경우에는 watcher
쪽에서 중복 호출이 일어나면 안 된다 — `finalize_scrape_run` 가 idempotent
하게 "이미 terminal 이면 no-op" 가이드를 넣는다.

### §9.3 active_sources 주입 방식

- 웹은 sources.yaml 의 원본을 건드리지 않는다.
- runner 가 per-run tempdir 을 만들고 원본 sources.yaml 을 복사한 뒤
  `scrape.active_sources` 필드만 덮어쓴 임시 yaml 을 생성한다.
- subprocess 의 `-e SOURCES_YAML_MOUNT=/run/config/sources.override.yaml` 과
  함께 임시 yaml 을 추가 바인드 마운트한다.
- 또는 더 간단하게 subprocess 에 `-e ACTIVE_SOURCES_OVERRIDE=IRIS,NTIS` 환경
  변수만 넘기고 `load_sources_config()` 가 이 환경변수를 읽어 scrape 섹션을
  in-memory 오버라이드하는 방식도 가능. 구현 선택은 00025-3 에서
  확정한다.

### §9.4 subprocess stdout/stderr 저장

- `Popen(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)`.
- 별도 백그라운드 스레드가 라인 단위로 읽어
  `data/logs/scrape_runs/{run_id}.log` 로 append. 파일은 ScrapeRun 의
  id 기반이므로 UI 는 `/admin/scrape/runs/{id}/log` 엔드포인트로 tail 하게
  한다 (Phase 2 에서는 단순 파일 전체 노출, Phase 5 에서 tail 개선 가능).
- 디스크 폭주 방지: 파일 크기 제한은 두지 않고, 대신 오래된 로그는 ScrapeRun
  row 정리와 동기화해서 (Phase 5 정책) 회수한다.

---

## §10. `admin_user_required` Depends

### §10.1 위치

`app/auth/dependencies.py` 에 추가.

```python
def admin_user_required(
    user: User = Depends(current_user_required),
) -> User:
    """비로그인 401, 로그인했지만 is_admin=False 면 403."""
```

### §10.2 실패 응답

- 비로그인: `current_user_required` 가 이미 401 HTTPException.
- `is_admin=False`: `HTTPException(status_code=403, detail='관리자만 접근할 수 있습니다.')`.
  HTML 요청에서는 전역 exception handler 로 `403.html` 렌더 (또는 기본
  `detail` JSON 으로 일단 노출 — 00025-4 에서 결정).

### §10.3 POST 라우트는 `ensure_same_origin` 을 **병용**한다

`admin_user_required` 가 쿠키 기반 인증만 보장하므로, CSRF 성격의 cross-site
form submit 을 막기 위해 POST 는 `ensure_same_origin` 도 함께 의존시킨다.
기존 auth 라우트와 동일한 패턴.

---

## §11. `/admin` 라우트 + [수집 제어] 탭 UI

### §11.1 라우트 테이블

| 메서드 | 경로 | 의존성 | 응답 |
|---|---|---|---|
| GET | `/admin` | `admin_user_required` | 302 → `/admin/scrape` |
| GET | `/admin/scrape` | `admin_user_required` | HTML (탭: 수집 제어) |
| GET | `/admin/scrape/status` | `admin_user_required` | JSON (5초 폴링용) |
| POST | `/admin/scrape/start` | `admin_user_required`, `ensure_same_origin` | 302 or JSON |
| POST | `/admin/scrape/cancel` | `admin_user_required`, `ensure_same_origin` | 302 or JSON |
| GET | `/admin/scrape/runs/{id}/log` | `admin_user_required` | text/plain |

sources.yaml 편집 탭, 스케줄 탭의 엔드포인트는 §12, §13 에서 정의.

### §11.2 상태 JSON 응답 스키마 (`GET /admin/scrape/status`)

```json
{
  "running": { "id": 42, "started_at": "...", "pid": 12345, "trigger": "manual" } | null,
  "recent": [
    {
      "id": 41, "started_at": "...", "ended_at": "...",
      "status": "completed", "trigger": "scheduled",
      "summary": {"created": 3, "new_version": 1, "unchanged": 40, "status_transitioned": 2,
                  "attachment_success": 5, "attachment_failure": 0}
    }
  ]
}
```

### §11.3 템플릿

- `admin/base.html` — 상단 탭 3개 네비 (현재 활성 표시).
- `admin/control.html` — 현재 상태 / 시작 폼(소스 체크박스, 전체 default)/
  중단 버튼 / 최근 N개 이력 테이블. 5초 폴링은 서버 사이드 렌더된 JS 로
  `fetch('/admin/scrape/status')` 후 상태 블록만 교체.
- 폴링이 아니라 새로고침으로도 충분히 동작해야 한다 (JS 없이 기본 UX).

### §11.4 시작 폼 검증

- `active_sources` 체크박스: 아무것도 체크 안 되면 "전체 소스" 의미.
- 체크된 id 가 `sources.yaml` 의 등록 소스 목록에 없으면 400 거부.

---

## §12. [sources.yaml] 탭 — 편집기

### §12.1 라우트

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/admin/sources/yaml` | HTML (textarea + 저장 버튼) |
| POST | `/admin/sources/yaml` | 302 or HTML (성공: 리다이렉트 + flash / 실패: 에러 + 원본 유지) |

### §12.2 저장 시 파이프라인

```
Form body "content"
  │
  ▼
YAML syntax 검증 — yaml.safe_load. 실패 시 4xx 에러 메시지 (위치·원인 포함 시 baseline)
  │
  ▼
Pydantic 검증 — SourcesConfig.model_validate(raw)
  │
  ▼
백업 작성:
  data/backups/sources/YYYYMMDD_HHMMSS.yaml ← 기존 sources.yaml 사본
  (백업 디렉터리는 startup 에서 mkdir -p)
  │
  ▼
원자적 쓰기:
  tmp = sources.yaml.new
  write(tmp, content)
  os.replace(tmp, sources.yaml)   ← POSIX atomic rename
  │
  ▼
성공 시 302 /admin/sources/yaml?saved=1
```

### §12.3 실패 케이스

- YAML syntax 에러 → 4xx + 원문 textarea 유지 + 에러 배지.
- Pydantic 에러 → 4xx + 원문 textarea 유지 + pydantic `e.errors()` dump.
- 파일 쓰기 실패 → 5xx + 백업 남긴 것도 roll forward 없음 (그대로 둠).

### §12.4 편집 후 즉시 반영?

- `sources.yaml` 은 FastAPI startup 에서 `get_available_source_ids()` 로 캐시
  되어 있다. Phase 2 에서는 **재시작 후 반영** 을 기본으로 하고, 편집 성공
  flash 에 "재시작 후 반영됨" 고지한다.
- scrape subprocess 는 매 실행마다 `load_sources_config()` 로 읽으므로
  영향 없이 반영된다.

---

## §13. [스케줄] 탭 — APScheduler 통합

### §13.1 APScheduler 선택 근거

후보:

| 후보 | 장점 | 단점 | 선택 여부 |
|---|---|---|---|
| **APScheduler 3.x BackgroundScheduler + SQLAlchemyJobStore** | 웹 프로세스 내부에서 가동. DB(SQLite) 에 job 저장으로 재시작 후 복원. cron 표현식 지원. 추가 서비스 프로세스 불필요 | 단일 웹 프로세스 전제. 웹 다중 워커 시 중복 실행 주의 — `coalesce=True`, `max_instances=1` 로 제어 | **선택** |
| Celery beat | 강력하지만 broker(Redis 등) 필요. 로컬 팀 전용에 과함 | 인프라 부담 | 기각 |
| OS cron + `docker compose run` | 이미 CLI 로 가능 | 웹 UI 에서 관리 불가 — 사용자 원문 요구 불충족 | 기각 |
| systemd timer | 호스트 환경 의존 | Docker-first 원칙과 충돌 | 기각 |

### §13.2 APScheduler 구성

- `BackgroundScheduler` — web 프로세스 내부에서 별도 스레드.
- Jobstore: `SQLAlchemyJobStore(url=<DB_URL>, tablename='scheduler_jobs')`.
  alembic migration 은 **추가하지 않는다** — APScheduler 가 첫 기동 시
  테이블을 자동 생성한다. 테이블명을 `scheduler_jobs` 로 고정해 이
  관례를 PROJECT_NOTES 에 노출.
- Executor: 기본 `ThreadPoolExecutor(max_workers=1)`.
- `coalesce=True` + `max_instances=1` — 웹 재시작 동안 밀린 예약 실행은
  한 번으로 병합, 동일 job 동시 실행 금지.
- startup: `app/web/main.py` startup event 에서 `scheduler.service.start()`.
  shutdown event 에서 `scheduler.service.stop(wait=False)`.

### §13.3 서비스 인터페이스 (`app/scheduler/service.py`)

```python
def start() -> None:
    """BackgroundScheduler 기동 (idempotent). web startup 에서 호출."""

def stop(*, wait: bool = False) -> None:
    """스케줄러 중단. 진행 중 job 이 있으면 wait 여부에 따라 기다림."""

@dataclass
class ScheduleSummary:
    job_id: str
    cron_expression: str
    active_sources: list[str]
    enabled: bool
    next_run_time: datetime | None

def list_schedules() -> list[ScheduleSummary]:
    """등록된 스케줄 목록 (next_run_time 포함)."""

def add_cron_schedule(
    *,
    cron_expression: str,
    active_sources: list[str],
    enabled: bool = True,
) -> ScheduleSummary:
    """cron 표현식으로 스케줄 추가. 내부에서 CronTrigger.from_crontab() 사용."""

def add_interval_schedule(
    *,
    hours: int,
    active_sources: list[str],
    enabled: bool = True,
) -> ScheduleSummary:
    """'매 N시간' 간단 모드. IntervalTrigger(hours=N) 를 cron 과 동일 방식으로 등록."""

def toggle_schedule(job_id: str, *, enabled: bool) -> ScheduleSummary:
    """pause_job / resume_job wrapper."""

def delete_schedule(job_id: str) -> None:
    """remove_job wrapper."""
```

### §13.4 Job 함수 (`app/scheduler/job_runner.py`)

```python
def scheduled_scrape(active_sources: list[str]) -> None:
    """APScheduler 가 호출하는 최상위 job. runner.start_scrape_run(trigger='scheduled').

    running row 가 있으면 건너뛰고 WARNING 로그(coalesce=True 로 이후 중복 방지).
    APScheduler 가 이 함수를 pickle 하므로 job_runner 모듈 경로는 안정적으로 유지."""
```

### §13.5 [스케줄] 탭 라우트

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/admin/schedule` | HTML (현재 스케줄 목록 + 추가 폼) |
| POST | `/admin/schedule` | 302 (cron 또는 interval 둘 중 하나의 필드 세트) |
| POST | `/admin/schedule/{job_id}/toggle` | 302 |
| POST | `/admin/schedule/{job_id}/delete` | 302 |

### §13.6 재시작 복원

- APScheduler 가 SQLAlchemyJobStore 에서 job 을 자동 로드.
- 단, "주기적으로 실행하는 job 정의" 자체가 pickle 되어 저장되므로
  `scheduled_scrape` 함수 경로(`app.scheduler.job_runner.scheduled_scrape`) 는
  리팩터 시 절대 변경해서는 안 된다.
- 변경이 필요할 때는 migration 형태로 `scheduler_jobs` 를 clear + 재등록한다.

---

## §14. 검증 시나리오 매트릭스

| # | 시나리오 | 확인 지점 |
|---|---|---|
| 1 | 관리자 로그인 후 "지금 시작" → ScrapeRun row 생성, pid 기록, /status 가 running 반영 | §4.1, §11.2 |
| 2 | 비관리자로 `/admin/*` 접근 → 403 | §10.2 |
| 3 | 중단 버튼 → SIGTERM 전파 → 현재 공고 commit 후 `cancelled` | §6, §8 |
| 4 | 중단 직후 재시작 → 즉시 허용 (lock 해제) | §7.3 Rule 1 |
| 5 | running 중 시작 버튼 → 409 | §7.3 Rule 1 |
| 6 | sources.yaml: 정상 / YAML 실패 / Pydantic 실패 | §12.2, §12.3 |
| 7 | 스케줄 등록 → 트리거 시 trigger='scheduled' 로 ScrapeRun 생성 | §13.4 |
| 8 | 웹 재시작 후 스케줄 복원 | §13.6 |
| 9 | 기존 CLI 경로 회귀: `docker compose --profile scrape run --rm scraper` 그대로 동작 | §7.3 Rule 3 |
| 10 | Stale cleanup: web 재시작 후 running 남은 row → `failed (stale)` | §7.4 |

---

## §15. 범위 밖이지만 운영 시 인지 필요

### §15.1 `/var/run/docker.sock` 마운트는 호스트 root 권한 부여와 동등하다

**핵심 경고**. `app` 컨테이너에 `/var/run/docker.sock` 을 마운트하는 순간,
그 컨테이너 안의 어떤 프로세스도 `docker run --privileged -v /:/host` 같은
명령으로 호스트 파일시스템 전체를 root 권한으로 읽고 쓸 수 있다. 즉 **docker
그룹 = root 그룹**이다. 이는 Docker 의 공식 보안 가이드가 경고하는 항목이며
회피할 수 없다(socket 프로토콜의 특성상 권한 분리 불가).

운영상 의미:

- FastAPI 가 외부 네트워크에 노출되면 안 된다는 기존 전제가 **강화**된다.
  로컬 루프백 또는 VPN 망 내부로만 접근 가능해야 하며, 내부 인증 경계만
  믿고 외부 공개하는 실수를 피해야 한다.
- 웹 프로세스의 코드 취약점(SSRF, path traversal, RCE) 이 생기면 호스트
  root 탈취로 이어진다. 본 프로젝트는 이 리스크를 받아들이는 것을 전제로
  하고 있다.
- 대안으로 "socket proxy" (예: Tecnativa/docker-socket-proxy) 를 통해 compose
  API 만 노출하는 방식이 있으나 Phase 2 범위 밖.

→ README.USER.md 에도 동일 경고 문구를 눈에 띄게 포함한다(subtask 00025-7).

### §15.2 subprocess 고아 가능성

웹 프로세스가 죽으면 자식 `docker compose run` 프로세스가 고아가 된다.
호스트 `init` 이 reaping 을 하지만 스크래퍼 컨테이너는 계속 돌 수 있다.
웹이 재시작되면 §7.4 stale cleanup 이 ScrapeRun row 를 `failed` 로 닫지만
실제 컨테이너가 실행 중이라면 DB 상태와 현실이 어긋날 수 있다.

운영 가이드: 웹 재시작 전에 `docker ps | grep iris-agent-scraper` 로 남은
컨테이너가 있는지 확인 → 있다면 `docker kill` 로 정리.

### §15.3 스케줄 중복 실행 (웹 다중 워커)

APScheduler BackgroundScheduler 는 단일 프로세스 전제. 운영자가 uvicorn 을
`--workers 2` 이상으로 띄우면 각 워커가 자체 scheduler 를 기동해 동일
시각에 중복 실행될 수 있다. 본 task 는 단일 워커 전제로만 설계한다.
docker-compose.yml 의 uvicorn 인자에 `--workers 1` 을 명시하고 README 에
기록한다.

### §15.4 SQLite + APScheduler 동시성

SQLite 는 writer 단일 전제이므로, 스케줄러가 job 상태를 기록하는 동안
다른 요청이 쓰기하려 들면 짧은 busy timeout 이 발생할 수 있다. 현재
`DB_URL` 기본 SQLite + `check_same_thread=False` 설정이면 기본 동작이
허용된다. 필요 시 `PRAGMA busy_timeout=3000` 을 `init_db` 에 주입하는 후속
튜닝을 권장(Phase 5 에서 고려).

### §15.5 CLI 경로도 동일 lock 에 참여한다

Phase 2 이후로는 `app.cli` 도 `create_scrape_run(trigger='cli')` 를 통해
lock 에 참여한다. 이는 **회귀** 가 아니라 의도된 확장이다 — 웹이 돌리는
중에 관리자가 컨테이너 쉘에서 `docker compose --profile scrape run scraper`
를 실행하면 CLI 가 즉시 exit(2) 로 거절된다. README.USER.md 에 "수집 중
이중 실행 차단" 으로 명시한다.

---

## §16. 부록 — 파일별 subtask 책임 요약

```
00025-2 infra:
    docker-compose.yml           app 서비스 docker.sock 마운트 + group_add
    docker/Dockerfile            docker-ce-cli + compose plugin 설치
    pyproject.toml               apscheduler 추가
    .env.example                 HOST_DOCKER_GID, HOST_PROJECT_DIR 문서화

00025-3 backend core:
    app/db/models.py             ScrapeRun ORM 추가
    app/db/repository.py         §7.2 헬퍼 7종
    app/auth/dependencies.py     admin_user_required
    app/scrape_control/*         runner / lock / logs / cancel 4모듈
    app/cli.py                   SIGTERM handler + create/finalize ScrapeRun

00025-4 admin 페이지 + 수집 제어 탭:
    app/web/routes/admin.py      router 등록 + /admin/scrape/* 엔드포인트
    app/web/main.py              router mount + startup stale cleanup
    app/web/templates/admin/     base.html + control.html
    app/web/templates/base.html  상단 네비 "관리자" 링크

00025-5 sources.yaml 편집기:
    app/web/routes/admin.py      /admin/sources/yaml (GET/POST)
    app/web/templates/admin/     sources.html
    (data/backups/sources/ 디렉터리는 런타임 자동 생성)

00025-6 스케줄:
    app/scheduler/*              service.py + job_runner.py
    app/web/routes/admin.py      /admin/schedule/* 엔드포인트
    app/web/templates/admin/     schedule.html
    app/web/main.py              scheduler.start() / stop() 훅

00025-7 문서:
    README.USER.md               "웹 기반 수집 제어" 섹션 (CLI 병행 가능 명시)
    (PROJECT_NOTES.md 는 MemoryUpdater 담당 — 본 subtask 에서는 건드리지 않음)
```

본 문서의 § 번호는 이후 subtask 가 코드/PR 설명에서 인용할 수 있도록
**이 문서가 살아 있는 동안 변경하지 않는다**. 새 절은 §17 이후로 append.
