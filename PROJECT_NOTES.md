# PROJECT NOTES

## 프로젝트 개요

IRIS(한국연구재단 범부처통합연구지원시스템) 등 정부과제 공고를 자동 수집·저장하고 로컬 웹에서 목록/상세/첨부파일을 열람하게 해주는 **개인용 로컬 에이전트**. 첨부파일(pdf/hwp/hwpx/zip)에 상세 정보가 담겨 있으므로 첨부 수집이 핵심이다. 대상 URL은 `https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituListView.do` 이며 현 시점에는 **접수중** 상태 공고만 수집한다.

## 아키텍처

- **Runtime**: Python 3.11~3.12. 의존성 선언은 `pyproject.toml`(setuptools), 실제 설치는 pip 또는 Docker 빌드 단계에서 수행.
- **레이어 구조** (`app/` 패키지):
  - `app/cli.py` — 스크래퍼 오케스트레이터 엔트리포인트(`python -m app.cli run`). 목록→상세→첨부 다운로드→UPSERT 흐름을 공고 단위로 예외 격리하며 실행.
  - `app/config.py` — pydantic-settings 기반 `Settings` 싱글턴(`get_settings()`). `.env` 로더, 런타임 디렉터리 생성(`ensure_runtime_paths`).
  - `app/logging_setup.py` — loguru 루트 로거 초기화.
  - `app/db/` — SQLAlchemy 2.0 ORM (`models.py` / `session.py` / `repository.py` / `init_db.py`).
  - `app/scraper/` — `list_scraper` + `detail_scraper` + `downloader`. 모두 Playwright headless Chromium 기반. 숨겨진 첨부 DOM까지 DOM 이벤트 트리거로 추출.
  - `app/web/` — FastAPI + Jinja2 템플릿(`templates/`) + 로컬 정적 CSS(`static/`). 외부 CDN 의존 없음.
  - `app/services/` — 현재는 예약만 되어 있고 빈 패키지.
- **데이터 저장**:
  - DB: 기본 SQLite (`./data/db/app.sqlite3`). `DB_URL` 로 교체 가능하도록 SQLAlchemy 접속 문자열 방식.
  - 첨부파일: `./data/downloads/<announcement_slug>/...` 로컬 FS. DB에는 `stored_path`, `sha256`, 크기 등 메타만 저장.
  - `data/` 이하는 `.gitignore` 대상(실제 데이터는 호스트에만 존재).
- **데이터 흐름**:
  1. CLI가 `init_db()` → Playwright 브라우저 부팅 → `scrape_list` 로 목록 수집
  2. 각 공고마다 `upsert_announcement` (기본 메타) → `scrape_detail` → `upsert_announcement` (raw_metadata 보강)
  3. `download_attachments_for_announcement` 실행 후 결과를 `upsert_attachment` 로 적재
  4. 웹 UI는 동일한 DB/FS를 읽기 전용으로 조회하고 첨부를 FileResponse로 스트리밍.
- **도메인 모델**: `Announcement` 1:N `Attachment`. 공고 UPSERT 키는 `iris_announcement_id` (UNIQUE). 상태는 `AnnouncementStatus` Enum(`접수중/접수예정/마감`, 한글 원문 값).
- **실행 환경**: Docker Compose가 기본. 두 서비스(`app` = FastAPI UI, `scraper` = 1회성 CLI, `profiles: [scrape]` 로 격리)가 같은 이미지 `iris-agent:dev` 를 공유한다. `./data` 와 `./app` 을 바인드 마운트해 데이터 영속성 + 개발 편의성(코드 바인드)을 확보.
- **외부 의존성**: Playwright(Chromium), httpx, SQLAlchemy 2.0, alembic(선언만, 실제 사용 X), FastAPI, uvicorn[standard], Jinja2, python-multipart, pydantic 2 + pydantic-settings, python-dotenv, loguru.

## 컨벤션

- **언어/문서**: 모든 모듈 docstring과 주석은 **한국어**. 외부 공개용이 아니므로 격식보다 명확성 우선.
- **타입힌트**: 모든 공개 함수에 타입 어노테이션. `from __future__ import annotations` 를 파일 상단에 둔다.
- **설정 접근**: 직접 `os.environ` 읽지 말고 `app.config.get_settings()` 싱글턴을 통해 접근.
- **시각 처리**: DB의 모든 시간 컬럼은 timezone-aware UTC. 소스가 모호하면 timezone 추정 없이 UTC로 보존하고 원문 텍스트는 `raw_metadata` JSON에 함께 저장한다.
- **UPSERT**: 공고는 `iris_announcement_id` 기준, 첨부는 `(announcement_id, original_filename)` + `sha256` 비교. 재실행 시 중복 생성 금지.
- **예외 격리**: 스크래퍼 파이프라인은 **공고 1건 단위**로 try/except. 한 공고의 실패가 전체 실행을 중단시키지 않는다.
- **웹 보안 경계**: FastAPI는 인증이 없고 외부 노출하지 않는 것을 전제. 첨부 다운로드는 반드시 `download_dir` 하위로 경로 트래버설 방어.
- **로깅**: loguru. 레벨은 `LOG_LEVEL` (`DEBUG/INFO/WARNING/ERROR/CRITICAL`).
- **패키지 경로 컨벤션**: uvicorn 경로는 `app.web.main:app`, CLI는 `python -m app.cli`. 재구조화 시 compose의 command도 함께 조정 필요.
- **lint/format**: ruff (line-length 110, target py311, E501 무시). mypy는 옵션 dev 의존성으로만 설치.
- **테스트**: `tests/` 디렉터리는 존재하지만 현재 **unit/e2e/integration 모두 비활성화**(`config_override.testing.*.enabled=false`). 품질 게이트는 README의 수동 검증 체크리스트.
- **커밋 메시지**: `[{task_id}][tg:{requester}] {subtask_id}: {요약}` 형식으로 통일.
- **데이터/비밀**: `.env` 는 커밋 금지. `.env.example` 만 관리.

## 주요 결정

- **Playwright headless + 첨부 DOM 이벤트 트리거**: IRIS의 첨부 링크 중 일부가 숨겨진 DOM/JS 이벤트를 통해 노출되므로 HTTP-only 스크래핑 불가. Chromium headless로 실제 이벤트를 발동시키는 방식 채택.
- **Alembic 도입 보류 → `create_all` 로 초기 DDL**: 스키마가 아직 1 이터레이션이고 로컬 SQLite 단일 배포라 마이그레이션 오버헤드가 과함. 필요 시점에만 alembic을 실제로 활성화.
- **SQLite 기본 + `DB_URL` 로 교체 가능**: 배포/공유 부담이 없는 단일 파일 DB가 개인용 목적에 맞음. Postgres 전환은 접속 문자열만 바꾸면 되도록 ORM 쪽은 JSON 범용 타입 사용.
- **첨부 바이너리는 DB 밖 FS에 저장**: hwp/zip이 수십 MB가 될 수 있어 BLOB 컬럼은 부적절. DB엔 경로/해시만. 중복/변경은 `sha256` 으로 판정.
- **요청 지연/페이지 상한 기본값 보수적 설정**: 기본 `REQUEST_DELAY_SEC=1.5`, `list_scraper` 의 안전 상한 50페이지. 차단 회피를 기본 정책으로 삼는다.
- **FastAPI 로컬 전용, 인증 없음**: 개인 로컬 열람 UI라서 auth 미구현. 대신 외부 노출 금지를 문서/주석 곳곳에 명시하고 첨부 경로 트래버설만 방어.
- **상태값을 한글 원문 그대로 Enum 값으로 보존**: IRIS 노출 텍스트와 1:1 매칭되어 필터/표시 변환 비용을 줄이기 위함.
- **Docker `scraper` 서비스를 `profiles: [scrape]` 로 격리**: 스크래퍼는 1회성 배치라 기본 `compose up` 에서 기동되면 안 됨. 명시적 `--profile scrape run --rm` 으로만 실행.
- **외부 CDN 의존 없는 정적 리소스**: 오프라인/격리 환경에서도 UI가 그대로 동작하도록 CSS를 `app/web/static/` 패키지 내부에 둔다.

## 최근 변경 이력

- [00005] IRIS 사업공고 스크래퍼 + DB + 로컬 FastAPI 웹을 한 번에 부트스트랩(Playwright headless, Docker Compose, SQLite). 프로젝트 전체 초기 구조 확립. — 2026-04-18
