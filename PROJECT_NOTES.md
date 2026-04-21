# PROJECT NOTES

## 프로젝트 개요

IRIS(한국연구재단 범부처통합연구지원시스템), NTIS 등 정부과제 사업공고를 자동 수집·저장하고 로컬 웹에서 목록/상세/첨부파일을 열람하게 해주는 **개인용 로컬 에이전트**. 첨부파일(pdf/hwp/hwpx/zip)에 상세 정보가 담겨 있으므로 첨부 수집이 핵심이다. 현재는 **IRIS·NTIS 모두 완전 구현** 상태이며, 두 소스 모두 **접수예정·접수중·마감 3개 상태 전체**를 순차 수집한다.

## 아키텍처

- **Runtime**: Python 3.11~3.12. 의존성 선언은 `pyproject.toml`(setuptools), 실제 설치는 pip 또는 Docker 빌드 단계에서 수행.
- **레이어 구조** (`app/` 패키지):
  - `app/cli.py` — 스크래퍼 오케스트레이터 엔트리포인트(`python -m app.cli`). CLI argparse 없음. `sources.yaml`의 `scrape:` 섹션에서 실행 파라미터를 읽고 enabled/active 소스를 순서대로 실행. 우선순위: `scrape:` 섹션 > 소스별 설정 > 코드 default.
  - `app/config.py` — pydantic-settings 기반 `Settings` 싱글턴(`get_settings()`). `.env` 로더, 런타임 디렉터리 생성(`ensure_runtime_paths`). `base_url` 필드 제거됨 — 소스별 URL은 `sources.yaml`에서만 관리.
  - `app/sources/` — `constants.py`(소스 ID 상수), `config_schema.py`(`ScrapeRunConfig`/`SourceConfig`/`SourcesConfig` Pydantic 모델, `load_sources_config()`). `ScrapeRunConfig`는 `sources.yaml`의 `scrape:` 섹션 스키마(active_sources·max_pages·max_announcements·skip_detail·skip_attachments·dry_run·log_level). `load_sources_config()`는 `SOURCES_CONFIG_PATH` 환경변수 경로를 우선 사용한다(entrypoint 복사본 격리용).
  - `app/logging_setup.py` — loguru 루트 로거 초기화.
  - `app/db/` — SQLAlchemy 2.0 ORM (`models.py` / `session.py` / `repository.py` / `init_db.py` / `migration.py`). 공고 UPSERT 키는 `(source_type, source_announcement_id)` 복합 키.
  - `app/scraper/base.py` — `BaseSourceAdapter` 추상 클래스. `scrape_list()`, `scrape_detail()` 추상 메서드. async context manager 지원.
  - `app/scraper/registry.py` — `get_adapter(source_config, settings)` 팩토리. lazy import로 순환 참조 방지.
  - `app/scraper/iris/` — `list_scraper.py`(httpx AJAX POST), `detail_scraper.py`(httpx+BS4 SSR), `adapter.py`(`IrisSourceAdapter`).
  - `app/scraper/ntis/` — `list_scraper.py`(httpx POST SSR), `detail_scraper.py`(httpx POST + BS4), `status_normalizer.py`(P/B/Y → AnnouncementStatus 정규화), `adapter.py`(`NtisSourceAdapter`): 완전 구현. 첨부는 httpx POST 직접 다운로드.
  - `app/canonical.py` — `compute_canonical_key()`: 공고 메타로부터 canonical_key 산출. 공식 키(`ancmNo` 정규화) 우선, 없으면 (제목 + 기관 + 마감연도) fuzzy fallback. 결과는 frozen dataclass `CanonicalKeyResult`(canonical_key, canonical_scheme).
  - `app/scraper/attachment_downloader.py` — Playwright headless 우선, httpx fallback 방식의 첨부파일 다운로더. `AttachmentDownloader` 클래스, JS onclick 클릭→실제 다운로드 흐름 처리. 이미 다운로드된 파일은 DB sha256 기반으로 건너뜀.
  - `app/scraper/attachment_paths.py` — 첨부 저장 경로 생성 유틸. `downloads/{source_type}/{announcement_slug}/` 폴더 구조. 파일명 정제(특수문자 제거, 중복 방지).
  - `app/web/` — FastAPI + Jinja2 템플릿(`templates/`) + 로컬 정적 CSS(`static/`). 목록·상세 라우트 모두 활성화.
    - 목록 라우트(`/`)는 URL query param(`status`, `source`, `q`, `sort`, `page`, `group`)으로 필터·정렬·페이지네이션·canonical 토글 상태를 유지. `sources.yaml`의 enabled 소스 목록을 동적으로 읽어 소스 필터 옵션을 자동 반영.
    - 필터: 상태(접수예정/접수중/마감/전체), 소스(sources.yaml 기반 동적), 제목 SQL LIKE 검색. 정렬: 수집일 최신순(기본)/마감일 가까운순(NULL 뒤)/공고명 가나다순.
    - Canonical 그룹 표시: 기본(group=0)은 각 row에 "동일 과제 N건" 배지. "묶어 보기"(group=1) 토글 시 canonical group 단위로 1행 + expand로 세부 소스 목록 표시.
    - `templates/viewers/<source_type>.html` — 소스 유형별 상세 viewer 템플릿. 없으면 `default.html` 폴백.
    - `templates/viewers/_shared.html` — 소스 공통 파셜(제목/상태 배지/소스 배지/마감일). ntis.html·iris.html에서 `{% include %}` 로 재사용.
    - `/announcements/{id}/attachments/{attachment_id}/download` — 로컬 파일을 클라이언트에 스트리밍하는 다운로드 엔드포인트. 경로 트래버설 방어 포함.
  - `app/services/` — 예약만 되어 있고 빈 패키지.
- **IRIS 목록 API 구조** (00002 탐사 결과):
  - 엔드포인트: `POST https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituList.do`
  - 요청: `application/x-www-form-urlencoded` — `pageIndex`, `pageUnit=10`, `ancmPrg=ancmPre|ancmIng|ancmEnd` (접수예정·접수중·마감 순차 수집; `searchBtinSituCd=RCP` 는 API가 무시함 — `ancmPrg` 가 실제 필터 파라미터)
  - 응답 JSON 키: `listBsnsAncmBtinSitu`(목록), `paginationInfo`(페이지 정보)
  - 상세 URL 패턴: `/contents/retrieveBsnsAncmView.do?ancmId={ancmId}`
- **데이터 저장**:
  - DB: 기본 SQLite (`./data/db/app.sqlite3`). `DB_URL` 로 교체 가능하도록 SQLAlchemy 접속 문자열 방식.
  - 첨부파일은 `./data/downloads/{source_type}/{announcement_slug}/` 로컬 FS에 저장. DB엔 경로·sha256·파일명·크기만 기록.
  - `data/` 이하는 `.gitignore` 대상.
- **데이터 흐름** (현재 활성):
  1. CLI가 `init_db()` (멱등, 기존 DB 재사용) → `load_sources_config()` → enabled 소스 목록 결정
  2. 각 소스마다 `get_adapter()` → `adapter.scrape_list()` → 목록 수집. 각 공고 `upsert_announcement(...)` → `UpsertResult` 반환.
  3. `UpsertResult.needs_detail_scraping` 기준으로 상세 수집 여부 결정: `unchanged` + 상세 이미 있으면 생략, 신규/변경/상태전이이면 항상 수집.
  4. 웹 UI는 목록 조회 + 공고 클릭 → 상세 페이지(`/announcements/{id}`) 표시. viewer 템플릿은 `source_type` 별로 분기.
  5. **[00008 추가]** 상세 수집 후 첨부파일 다운로드 단계 실행: `AttachmentDownloader`가 Playwright headless로 각 첨부 링크를 클릭 → 다운로드 완료 후 DB Attachment row 생성.
- **도메인 모델**: `Announcement` 1:N `Attachment`. 공고 UPSERT 키는 `(source_type, source_announcement_id)` 복합 UNIQUE. `is_current=True`인 row가 현재 버전. 상태는 `AnnouncementStatus` Enum(`접수중/접수예정/마감`, 한글 원문 값). Attachment는 `(announcement_id, original_filename)` 복합 UNIQUE + `sha256` 중복 체크.
- **Canonical 레이어**: `CanonicalProject` 테이블(schema B — 별도 테이블)이 `Announcement` 1:N을 FK로 묶는 그룹. `canonical_key` UNIQUE 컬럼(예: `official:과학기술정보통신부공고제2026-0455호`). 각 Announcement는 `canonical_project_id` FK로 참조. IRIS 공식 식별자는 `ancmNo`(공고번호 — 목록 API에 항상 존재). 탐사 결과는 `docs/canonical_identity_design.md` 참조.
- **Backfill/검증 스크립트**: `scripts/backfill_canonical.py`(기존 IRIS 데이터 일회성 canonical 재계산), `scripts/verify_canonical_iris.py`(IRIS 단독 중복 케이스 검증), `scripts/verify_canonical_cross_source.py`(IRIS↔NTIS cross-source 매칭 결과 샘플 출력 및 false positive/negative 검증).
- **증분 UPSERT 4-branch**: (a) 신규 → INSERT is_current=True. (b) 변경 없음 → no-op (상세 있으면 상세도 생략). (c) 상태 전이만 → in-place UPDATE. (d) 그 외 변경 → 기존 row 봉인(is_current=False) + 신규 row INSERT (이력 누적). 비교 필드: `title, status, deadline_at, agency` (`received_at` 제외 — 접수예정 시 공란 보완 많아 잡음 유발).
- **상태 전이**: `docs/status_transition_todo.md` — IRIS 3개 상태 순차 수집 활성화됨(items 1·2 구현 완료). NTIS 등 신규 크롤러 구현 시 참고.
- **실행 환경**: Docker Compose 전용 (`docker compose --profile scrape run --rm scraper`). 호스트 Python 직접 실행 완전 제거. `app`(FastAPI UI)·`scraper`(1회성 배치, `profiles: [scrape]`)가 같은 이미지를 공유. 바인드 마운트: `./data`, `./app`, `./sources.yaml:/run/config/sources.yaml:ro`. `docker/entrypoint.sh`가 마운트된 `sources.yaml`을 per-run 임시 디렉터리(`mktemp`)로 복사하고 `SOURCES_CONFIG_PATH` 환경변수를 주입한 뒤 `python -m app.cli`를 exec — 동시 다중 실행 시 설정 경합 방지. 실행 파라미터는 CLI 인자 없이 `sources.yaml`의 `scrape:` 섹션으로만 제어.
- **외부 의존성**: httpx(목록·상세 수집), BeautifulSoup4(상세 HTML 파싱), pyyaml(sources.yaml 로드), SQLAlchemy 2.0, FastAPI, uvicorn[standard], Jinja2, pydantic 2 + pydantic-settings, python-dotenv, loguru, **playwright**(첨부 다운로드 — headless Chromium). Dockerfile에 `playwright install chromium --with-deps` 포함.
- **첨부 다운로드 설계**: `docs/attachment_download_plan.md` 에 상세 설계 기록. 첨부 링크는 `javascript:f_bsnsAncm_downloadAtchFile(...)` 형태 — Playwright가 해당 링크를 클릭하면 브라우저 `download` 이벤트로 파일 수신. httpx fallback(직접 POST)은 JS 우회 불가 시 사용 못할 수 있음.

## 컨벤션

- **언어/문서**: 모든 모듈 docstring과 주석은 **한국어**. 외부 공개용이 아니므로 격식보다 명확성 우선.
- **타입힌트**: 모든 공개 함수에 타입 어노테이션. `from __future__ import annotations` 를 파일 상단에 둔다.
- **설정 접근**: 직접 `os.environ` 읽지 말고 `app.config.get_settings()` 싱글턴을 통해 접근.
- **시각 처리**: DB의 모든 시간 컬럼은 timezone-aware UTC. 소스가 모호하면 timezone 추정 없이 UTC로 보존하고 원문 텍스트는 `raw_metadata` JSON에 함께 저장한다.
- **UPSERT**: 공고는 `(source_type, source_announcement_id)` 복합 키 기준, 첨부는 `(announcement_id, original_filename)` + `sha256` 비교. 재실행 시 중복 생성 금지. 변경된 경우에만 신규 row 생성(이력 보존), 상태 전이만인 경우는 in-place UPDATE.
- **예외 격리**: 스크래퍼 파이프라인은 **공고 1건 단위**로 try/except. 한 공고의 실패가 전체 실행을 중단시키지 않는다.
- **웹 보안 경계**: FastAPI는 인증이 없고 외부 노출하지 않는 것을 전제. 첨부 다운로드는 반드시 `download_dir` 하위로 경로 트래버설 방어.
- **로깅**: loguru. 레벨은 `LOG_LEVEL` (`DEBUG/INFO/WARNING/ERROR/CRITICAL`).
- **패키지 경로 컨벤션**: uvicorn 경로는 `app.web.main:app`, 스크래퍼 CLI는 `python -m app.cli`(서브커맨드 없음, CLI 인자 없음). 재구조화 시 `docker/entrypoint.sh`와 compose의 command도 함께 조정 필요.
- **로컬 Python 직접 실행 금지**: 호스트에서 `python -m app.cli` 실행은 지원하지 않는다. Docker Compose 경유만 지원. README에서도 관련 예시 모두 제거됨.
- **lint/format**: ruff (line-length 110, target py311, E501 무시). mypy는 옵션 dev 의존성으로만 설치.
- **테스트**: `tests/` 디렉터리는 존재하지만 현재 **unit/e2e/integration 모두 비활성화**(`config_override.testing.*.enabled=false`). 품질 게이트는 README의 수동 검증 체크리스트.
- **커밋 메시지**: `[{task_id}][tg:{requester}] {subtask_id}: {요약}` 형식으로 통일.
- **데이터/비밀**: `.env` 는 커밋 금지. `.env.example` 만 관리.

## 주요 결정

- **목록·상세 수집은 httpx(HTTP-only), 첨부 수집만 Playwright**: IRIS 목록·상세 페이지는 SSR이라 httpx만으로 충분(탐사로 확인됨). 첨부 링크는 `javascript:` onclick으로만 노출되어 HTTP 직접 접근 불가 → Playwright headless로만 처리. 이 구분이 수집 단계 안정성을 높이고 의존성 무게를 줄인다.
- **Alembic 도입 보류 → `create_all` 로 초기 DDL**: 스키마가 아직 1 이터레이션이고 로컬 SQLite 단일 배포라 마이그레이션 오버헤드가 과함. 필요 시점에만 alembic을 실제로 활성화.
- **SQLite 기본 + `DB_URL` 로 교체 가능**: 배포/공유 부담이 없는 단일 파일 DB가 개인용 목적에 맞음. Postgres 전환은 접속 문자열만 바꾸면 되도록 ORM 쪽은 JSON 범용 타입 사용.
- **첨부 바이너리는 DB 밖 FS에 저장**: hwp/zip이 수십 MB가 될 수 있어 BLOB 컬럼은 부적절. DB엔 경로/해시만. 중복/변경은 `sha256` 으로 판정.
- **페이지/공고 수집 상한을 sources.yaml·코드 기본값 2단계 우선순위로 관리**: 코드 기본값은 `max_pages=10, max_announcements=200`. 우선순위: `sources.yaml scrape:` 섹션 전역값 > 소스별 설정 > 코드 default. CLI 인자 단계는 00016에서 제거됨. 기본값을 보수적으로 유지해 차단 회피를 기본 정책으로 삼는 기조는 유지.
- **FastAPI 로컬 전용, 인증 없음**: 개인 로컬 열람 UI라서 auth 미구현. 대신 외부 노출 금지를 문서/주석 곳곳에 명시하고 첨부 경로 트래버설만 방어.
- **상태값을 한글 원문 그대로 Enum 값으로 보존**: IRIS 노출 텍스트와 1:1 매칭되어 필터/표시 변환 비용을 줄이기 위함.
- **증분 수집 전략 — DB 초기화 금지**: 스크래핑 재실행 시 기존 DB를 유지하고 변경된 공고만 갱신. `init_db()`는 `create_all` 멱등 DDL만 수행하고 데이터를 지우지 않음. 이력 보존 및 불필요한 세부 수집 방지가 목적.
- **`received_at`을 변경 감지 비교 필드에서 제외**: 접수예정 상태에서 수집 시 공란이었다가 이후 보완되는 경우가 많아 잡음 트리거를 피하기 위해 의도적으로 제외. 핵심 비교 필드는 `title, status, deadline_at, agency` 4개로 고정.
- **Docker `scraper` 서비스를 `profiles: [scrape]` 로 격리**: 스크래퍼는 1회성 배치라 기본 `compose up` 에서 기동되면 안 됨. 명시적 `--profile scrape run --rm` 으로만 실행.
- **날짜 기반 수집 중단 조건 제거**: 정부 공고 사이트가 마감일을 `2200-01-01` 등 허위 값으로 기재하는 사례가 많아 날짜로 중단 여부를 판단하면 오작동 가능성이 큼. `list_scraper` 에서 날짜 기반 break 조건을 전부 제거하고 `max_pages` / `max_announcements` 상한만으로 수집을 제어한다.
- **외부 CDN 의존 없는 정적 리소스**: 오프라인/격리 환경에서도 UI가 그대로 동작하도록 CSS를 `app/web/static/` 패키지 내부에 둔다.
- **Canonical 스키마 — (B) 별도 테이블 선택**: `announcements`에 컬럼을 추가하는 (A)보다 `canonical_projects` 별도 테이블 + FK 참조(N:1) 구조(B)를 선택. 이유: cross-source 그룹 단위 검색·UI 표시 시 JOIN 없이 그룹 메타를 바로 조회할 수 있고, 나중에 그룹 수준 필드를 확장하기 쉽다.
- **IRIS 공식 식별자는 `ancmNo`**: 목록 API 전수 필드에서 비어 있는 레코드 0건. 1:1 고유 키가 아니라 "공고 묶음" 단위(하나의 공고번호에 여러 세부과제 ancmId가 대응). canonical_key 생성 전 NFKC 정규화 + 공백 제거 필수. 탐사 결과는 `docs/canonical_identity_design.md §2` 참조.
- **`compute_canonical_key` 공식 키 우선 + fuzzy fallback**: official 키(ancmNo 정규화)를 우선 사용하고, 없으면 (제목 불용어 제거 + 주관기관 정규화 + 마감연도) 조합. NTIS cross-source 실매칭에서도 동일 로직 재사용 확인(fuzzy 매칭 → official 승급 경로 검증됨). fuzzy 임계값·튜닝 근거는 `docs/canonical_identity_design.md` 참조.
- **NTIS는 httpx 단독 수집 (Playwright 불필요)**: 목록·상세·첨부 모두 httpx POST SSR로 처리. IRIS와 달리 AJAX API 없음. 첨부도 `wfUid + roTextUid` 파라미터로 httpx POST 직접 다운로드 가능(JS onclick 우회 불필요). `AttachmentDownloader`는 소스별 링크 추출 분기를 adapter 계층에서 처리하고 downloader 자체는 소스를 모름.
- **sources.yaml에 credentials 슬롯(optional)**: 로그인이 필요한 소스를 위한 `credentials.username`/`credentials.password` 필드를 `SourceConfig`에 예약. NTIS는 게스트 수집이 가능하므로 현재 미사용. 실제 값은 `.env`로만 주입하고 yaml에는 키 이름만 남긴다.

## 최근 변경 이력

- [00016] 스크래퍼 config 구조 재정비: CLI argparse 전면 제거, sources.yaml scrape: 섹션으로 실행 제어, docker-compose에 sources.yaml bind mount 추가, entrypoint.sh per-run 복사본 격리(동시 실행 안전), IRIS base_url 중복 상수 일원화(sources.yaml 단일 소스), README에서 로컬 Python 실행 예시 전면 삭제. — 2026-04-21
- [00015] 웹 UI 필터/검색/정렬·canonical 그룹 표시 구현: 상태·소스·제목 SQL LIKE 필터, 3종 정렬, canonical 배지 및 묶어 보기 토글(URL query param 상태 유지), 페이지네이션 state 보존, README.USER.md 사용법 섹션 추가. — 2026-04-21
- [00014] NTIS 스크래퍼 실제 구현: httpx SSR 목록·상세 수집, NTIS 상태 P/B/Y 정규화, 첨부 httpx POST 직접 다운로드, `NtisSourceAdapter` 완전 구현, cross-source canonical 매칭 검증(fuzzy→official 승급), attachment downloader 소스 분기 adapter 계층 처리, NTIS viewer 템플릿·`_shared.html` 파셜, `sources.yaml` NTIS `enabled: true`. — 2026-04-21
- [00013] Canonical identity 레이어 도입: `canonical_projects` 테이블(schema B), `compute_canonical_key()`(IRIS ancmNo 공식 키 우선/fuzzy fallback), 수집 파이프라인 canonical 매칭 통합, backfill·검증 스크립트, `docs/canonical_identity_design.md` 작성. — 2026-04-21
- [00012] IRIS 3개 상태 순차 수집: `ancmPrg=ancmPre|ancmIng|ancmEnd` 루프로 접수예정·접수중·마감 전체 수집. `status_transitioned` 경로 정상 운영 경로로 전환(in-place UPDATE, INFO 로그). `sources.yaml`에 `statuses` 필드 고정. `.env.example` BASE_URL 제거. — 2026-04-21
- [00011] 스크래퍼 CLI 인자 지원: compose entrypoint/command 분리로 `docker compose run --rm scraper --max-pages N --max-announcements N` 형태 인자 전달 가능. `SourceConfig`에 `max_pages`·`max_announcements` Optional 필드 추가, 3단계 우선순위(CLI>yaml>default) 적용, list_scraper 날짜 기반 중단 조건 제거·상한 하드코딩 제거, README 예시 갱신. — 2026-04-21
- [00008] 첨부파일 다운로드 전면 구현: Playwright headless 기반 다운로더(`attachment_downloader.py`) 추가, `downloads/{source_type}/{slug}/` 폴더 저장, DB Attachment 1:N 연결, 웹 상세 뷰에 첨부 목록·로컬 다운로드 엔드포인트 추가, Docker에 Playwright 의존성 통합. — 2026-04-20
- [00007] 변경 감지 false-positive 수정: `_normalize_for_comparison` 헬퍼 추가로 `deadline_at` tz-naive/aware 불일치 및 title·agency 문자열 공백 차이로 인한 불필요한 상세 재수집 방지. CLI 종료 로그에 `action 분포(신규/변경없음/버전갱신/상태전이)` 추가. — 2026-04-20
- [00006] DB 증분 수집 전략 구현: 재실행 시 DB 재사용, 변경 감지 4-branch UPSERT, 상태 전이 TODO 문서화(`docs/status_transition_todo.md`), 시스템 관리자용 `README.USER.md` 신규 작성. — 2026-04-20
- [00004] 스크래퍼 소스 범용화: `source_type` DB 필드 도입·복합 UPSERT 키, `sources.yaml` 소스 파라미터 설정, `BaseSourceAdapter` 플러그인 구조, IRIS 어댑터 완전 구현·NTIS stub 추가, 프론트엔드 소스 배지 및 viewer 템플릿 분기, README/PROJECT_NOTES 갱신. — 2026-04-20
