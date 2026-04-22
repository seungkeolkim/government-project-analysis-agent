# government-project-analysis-agent

IRIS(한국연구재단 범부처통합연구지원시스템), NTIS 등 정부과제 사업공고를 자동 수집·저장하고,
로컬 웹에서 공고 목록/상세/첨부파일을 열람할 수 있도록 돕는 개인용 에이전트 프로젝트.

> 현재 구현된 소스: **IRIS** (완전 구현), **NTIS** (완전 구현)
> 두 소스 모두 **접수예정·접수중·마감 3개 상태** 공고를 순차 수집한다.
> 증분 수집 전략 적용: 재실행 시 변경 없는 공고는 상세 재수집을 생략하고 기존 데이터를 재사용한다.

## 구성 개요

- **Scraper**: httpx 기반 소스 어댑터 플러그인 구조. IRIS·NTIS 목록·상세 수집 완전 구현.
  첨부파일(pdf/hwp/hwpx/zip) 다운로드 지원 — IRIS: Playwright headless 주 경로·httpx 폴백, NTIS: httpx POST 직접 다운로드.
- **DB**: SQLAlchemy 2.0 + SQLite (기본) / Postgres 호환. 공고와 첨부파일 메타데이터를 `source_type` 별로 적재.
  증분 수집 전략: `is_current` 플래그로 현재 버전과 이력(구버전)을 구분한다.
  스키마 마이그레이션은 **Alembic** 으로 관리한다 (`alembic/versions/`). 기동 시 자동 적용.
- **Web**: FastAPI + Jinja2 로 로컬 전용 열람 UI 및 첨부 다운로드 API 제공.
  소스 유형별 viewer 템플릿(`viewers/<source_type>.html`)으로 상세 표시 분기.
- **Config**: pydantic-settings 기반 `.env` 로더 + `sources.yaml` 소스별 파라미터 설정

## 디렉터리 구조

```
.
├── app/
│   ├── cli.py                  # 스크래퍼 오케스트레이터 진입점
│   ├── config.py               # pydantic-settings 설정 로더
│   ├── logging_setup.py        # loguru 초기화
│   ├── db/                     # SQLAlchemy 모델 / 세션 / repository / migration
│   ├── scraper/
│   │   ├── base.py             # BaseSourceAdapter 추상 클래스
│   │   ├── registry.py         # source_type → 어댑터 매핑
│   │   ├── iris/               # IRIS 어댑터 (list/detail scraper)
│   │   └── ntis/               # NTIS 어댑터 (list/detail scraper, httpx)
│   ├── sources/                # 소스 상수 + sources.yaml 스키마/로더
│   ├── services/               # (예약)
│   └── web/                    # FastAPI 라우트 / 템플릿 / 정적 리소스
│       └── templates/
│           └── viewers/        # 소스 유형별 상세 viewer 템플릿
├── data/
│   ├── db/                     # SQLite 파일 위치 (.gitignore)
│   └── downloads/              # 첨부파일 저장소 (.gitignore)
├── docker/
│   ├── Dockerfile
│   └── entrypoint.sh           # 컨테이너 시작 스크립트 (sources.yaml per-run copy 격리)
├── tests/
├── .env.example
├── sources.yaml                # 소스별 파라미터 + 전역 수집 실행 설정
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

## 사전 준비

> **Docker 전용.** 호스트에서 `python -m app.cli` 를 직접 실행하는 방식은 지원하지 않는다.
> 모든 실행은 `docker compose` 를 통해서만 수행한다.

### 1) `.env` 파일 생성

```bash
cp .env.example .env
```

필요에 따라 아래 값을 조정한다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `REQUEST_DELAY_SEC` | `1.5` | 소스별 `request_delay_sec` 미지정 시 fallback(초) |
| `USER_AGENT` | (빈값) | 커스텀 User-Agent. 비우면 어댑터 기본값 |
| `DOWNLOAD_DIR` | `./data/downloads` | 첨부파일 저장 루트 |
| `DB_URL` | `sqlite:///./data/db/app.sqlite3` | DB 접속 문자열 |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |

> `.env` 는 `.gitignore` 대상이므로 커밋되지 않는다.

### 2) `sources.yaml` 수집 설정

모든 수집 실행 파라미터는 `sources.yaml` 로 제어한다. CLI 인자는 사용하지 않는다.

```yaml
# sources.yaml (저장소 루트)
scrape:
  active_sources: []       # 비어 있으면 enabled: true 인 소스 전체 실행
                           # 특정 소스만 실행: active_sources: [NTIS]
  max_pages: null          # null 이면 소스별 설정 → 코드 default(10) 사용
  max_announcements: null  # null 이면 소스별 설정 → 코드 default(200) 사용
  skip_detail: false       # true 이면 목록 적재만 수행 (상세 생략)
  skip_attachments: false  # true 이면 첨부파일 다운로드 생략
  dry_run: false           # true 이면 DB 쓰기 없이 수집만 검증
  # log_level: DEBUG       # 주석 해제 시 .env 의 LOG_LEVEL 보다 우선 적용

sources:
  - id: IRIS
    enabled: true
    base_url: https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituListView.do
    request_delay_sec: 1.5
    statuses:
      - 접수예정
      - 접수중
      - 마감
  - id: NTIS
    enabled: true
    base_url: https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do
    request_delay_sec: 2.0
    max_pages: 5           # 마감 공고 7만 건+ 대비 보수적 기본값
    max_announcements: 100
```

`enabled: false` 인 소스는 자동으로 건너뛴다. `scrape.active_sources` 로 특정 소스만 실행할 수 있다.

## DB / Alembic 마이그레이션

스키마 변경 이력은 `alembic/versions/` 아래 migration 파일로 관리한다.
**기동 시 자동 적용** — 별도 명령이 필요 없다.

| 상황 | 동작 |
|------|------|
| 빈 DB (최초 배포) | `alembic upgrade head` — baseline 스키마 전체 생성 |
| 기존 DB (Alembic 도입 전) | `alembic stamp head` — 데이터 무변경, 리비전 레코드만 삽입 |
| Alembic 관리 DB | `alembic upgrade head` — 신규 migration 적용 (없으면 no-op) |

수동 조작이 필요한 경우:

```bash
# 현재 리비전 확인
docker compose run --rm scraper alembic current

# 신규 migration 생성 (스키마 변경 시)
docker compose run --rm scraper alembic revision --autogenerate -m "컬럼 추가"

# head 로 올리기
docker compose run --rm scraper alembic upgrade head

# 한 단계 롤백
docker compose run --rm scraper alembic downgrade -1
```

> Postgres 로 전환할 때는 `.env` 의 `DB_URL` 만 변경하면 된다.
> `psycopg2-binary` 또는 `psycopg` 를 추가로 설치해야 한다.

## 실행 방법

Docker 와 docker compose v2 이상이 설치되어 있어야 한다.

### 1) 이미지 빌드 (최초 1회 또는 코드 변경 후)

```bash
docker compose build
```

### 2) 웹 UI 기동

```bash
docker compose up app
```

- 기동 후 브라우저에서 <http://localhost:8000> 로 접속한다.
- 종료는 `Ctrl+C` 또는 별도 터미널에서 `docker compose down`.

### 3) 스크래퍼 실행

스크래퍼는 장기 상주 서비스가 아니므로 `scrape` 프로파일로 분리되어 있다.
**실행 파라미터는 모두 `sources.yaml` 의 `scrape:` 섹션에서 설정한다.**

```bash
docker compose --profile scrape run --rm scraper
```

`sources.yaml` 에서 `enabled: true` 인 소스를 `scrape.active_sources` 순서 또는
설정 파일 순서대로 처리해 SQLite DB 에 적재한다.
재실행 시 변경 없는 공고는 상세 재수집을 생략하고 기존 데이터를 재사용한다(증분 수집).

**동시 실행.** 같은 명령을 여러 터미널에서 동시에 실행해도 각 컨테이너가
`sources.yaml` 의 독립적인 복사본을 사용하므로 설정 경합이 발생하지 않는다.

#### 자주 쓰는 설정 패턴

설정을 변경한 뒤 `docker compose --profile scrape run --rm scraper` 를 실행한다.

```yaml
# NTIS 만 소량 검증 (드라이런)
scrape:
  active_sources: [NTIS]
  max_pages: 1
  dry_run: true

# IRIS 전체 수집, 첨부 생략
scrape:
  active_sources: [IRIS]
  skip_attachments: true

# 전체 소스 실사 수집
scrape:
  active_sources: []
  max_pages: null
  max_announcements: null
  skip_detail: false
  skip_attachments: false
  dry_run: false
```

수집된 데이터는 호스트의 `./data/db/app.sqlite3` 와 `./data/downloads/{source_type}/{announcement_id}/`
에 영속 저장된다. 컨테이너를 지워도 데이터는 유지된다.

## 접속 URL 및 데이터 위치

- **웹 UI**: <http://localhost:8000>
  - `/` — 공고 목록 (상태/검색 필터, 페이지네이션)
  - `/announcements/{id}` — 공고 상세 + 첨부파일 목록
  - `/attachments/{id}/download` — 첨부파일 다운로드
  - `/announcements`, `/announcements/{id}.json` — JSON API
  - `/register`, `/login` — 회원가입/로그인 페이지 (Phase 1b)
  - `/auth/register`, `/auth/login`, `/auth/logout` — 인증 처리 엔드포인트 (POST)
  - `/auth/me` — 현재 사용자 JSON (비로그인이면 `{"user": null}`)
- **SQLite DB**: `./data/db/app.sqlite3`
- **첨부파일 저장소**: `./data/downloads/{source_type}/{announcement_id}/`

### 사용자 인증 (Phase 1b)

자유 회원가입 기반의 세션 쿠키 인증이 활성화되어 있다.

- **비로그인 열람 가능.** 목록(`/`)·상세(`/announcements/{id}`)·첨부 다운로드는
  로그인 없이 그대로 사용한다.
- **로그인 시 추가 기능.**
  - 목록에서 안 읽은 공고는 **굵게**, 읽은 건 보통 굵기로 표시.
  - 상세 페이지 진입 시 자동으로 "읽음" 처리 (announcement 단위로 기록).
- **회원가입.** 우상단 네비의 "회원가입" 또는 `/register` 로 직접 접근.
  username/password (선택: email) 만 입력하면 가입과 동시에 자동 로그인.
- **첫 관리자 계정.** 운영자가 `scripts/create_admin.py` CLI 로 생성한다.
  자세한 절차는 [README.USER.md](README.USER.md) 의 *첫 관리자 계정 생성*
  섹션 참조.

## 증분 수집 전략

스크래퍼를 반복 실행해도 DB를 초기화하지 않으며, 이미 수집된 데이터를 최대한 재사용한다.

| 케이스 | 동작 |
|---|---|
| **신규 공고** | 새로 INSERT |
| **변경 없음** (공고명·상태·마감일·기관 동일) | 기존 데이터 재사용, 상세 재수집 생략 |
| **내용 변경** (공고명·마감일·기관 등 변경) | 기존 row 이력으로 보존(`is_current=False`), 신규 버전 INSERT |
| **상태 전이만** (접수예정→접수중 등) | 기존 row 상태 in-place 갱신, 상세 재수집 |

일상 운영·트러블슈팅은 **[README.USER.md](README.USER.md)** 에서 확인한다.

## 주의사항

- **Docker 전용.** 호스트 Python 직접 실행(`python -m app.cli`, `uvicorn` 등)은 지원하지 않는다.
  모든 실행은 `docker compose` 를 통해서만 수행한다.
- **로컬 전용.** Phase 1b 에서 자유 회원가입 기반 세션 인증이 추가되었지만
  여전히 팀 내부 로컬망 사용을 전제로 한다. 외부 인터넷에 직접 노출하지 말 것
  (HTTPS 종단 미적용, CSRF 토큰 미발급, 비밀번호 정책 최소만 강제).
- **차단 방지.** 각 소스가 동일 IP 의 과도한 요청을 차단할 수 있다. `sources.yaml` 의
  `request_delay_sec` (기본 1.5초) 를 너무 짧게 설정하지 말고, `max_pages` 로 범위를 제한해서 사용할 것.
- **User-Agent/봇 정책.** 대상 사이트의 이용 약관, `robots.txt`, 저작권 정책을 확인하고,
  수집한 데이터는 개인 연구·분석 용도로만 활용한다. 재배포·상업적 이용은 금지.
- **첨부파일 크기/시간.** HWP/HWPX/ZIP 첨부가 많은 공고는 다운로드에 시간이 걸릴 수 있다.
  네트워크 장애로 일부 첨부가 실패해도 스크래퍼는 공고 단위로 예외를 격리해 전체 흐름을
  중단시키지 않는다. 실패한 항목은 로그에서 확인한 뒤 재실행하면 UPSERT 된다.
- **개인정보/민감정보.** 본 프로젝트는 각 소스의 공개 공고만 수집하지만, 저장된 첨부파일에
  사업 담당자 연락처 등이 포함될 수 있다. `./data/` 디렉터리 공유에 유의할 것.

## 최종 수동 검증 체크리스트

마이그레이션이 끝난 직후 또는 새로운 환경에 배포할 때 아래 항목을 순서대로 확인한다.
(자동화된 E2E 테스트는 기본 비활성화되어 있어 이 체크리스트가 최종 게이트 역할을 한다.)

1. **환경 구성**
   - [ ] `.env` 가 생성되어 있고 `DB_URL`, `DOWNLOAD_DIR` 이 의도한 값이다.
   - [ ] `sources.yaml` 에서 수집할 소스가 `enabled: true` 로 설정되어 있다.
   - [ ] `sources.yaml` 의 `scrape:` 섹션이 의도한 파라미터로 설정되어 있다.
   - [ ] `./data/db`, `./data/downloads` 디렉터리가 존재한다(비어 있어도 됨).
2. **Docker 빌드 & 기동**
   - [ ] `docker compose build` 가 에러 없이 완료된다.
   - [ ] `docker compose up app` 후 <http://localhost:8000> 가 200 을 반환한다.
   - [ ] 초기 목록 페이지에 "공고가 없습니다" 빈 상태 UI 가 정상 렌더링된다.
3. **스크래퍼 드라이런**
   - [ ] `sources.yaml` 의 `scrape.dry_run: true`, `scrape.max_pages: 1` 설정 후
         `docker compose --profile scrape run --rm scraper` 실행 시
         목록 파싱 로그가 정상 출력되고 DB/파일 쓰기 없이 exit code 0 으로 종료된다.
4. **스크래퍼 실제 실행 및 증분 동작 확인**
   - [ ] `scrape.dry_run: false`, `scrape.max_pages: 1` 설정 후 실행하면
         `./data/db/app.sqlite3` 가 생성된다.
   - [ ] 같은 설정으로 재실행하면 변경 없는 공고는 `상세 수집 생략(변경 없음)` 로그가 출력되고,
         신규 공고만 새로 수집된다(증분 수집 동작 확인).
   - [ ] 재실행 후 웹 UI 공고 목록에 중복 공고가 생기지 않는다.
5. **웹 UI 기능 확인**
   - [ ] `/` 목록에서 수집된 공고들이 페이지네이션과 함께 표시된다.
   - [ ] 공고 행에 소스 배지(`IRIS` / `NTIS` 등)가 표시된다.
   - [ ] 상태 필터(`?status=OPEN` 등) 와 검색어가 동작한다.
   - [ ] 공고 제목 클릭 시 상세 페이지(`/announcements/{id}`)가 열리고 메타/첨부 목록이 보인다.
   - [ ] 상세 메타 테이블에 "소스" 행이 표시되고 source_type 배지가 렌더링된다.
   - [ ] 첨부파일 다운로드 링크(`/attachments/{id}/download`) 에서 실제 파일이 내려온다.
   - [ ] `/announcements.json`, `/announcements/{id}.json` 이 JSON 을 반환한다.
6. **데이터 정리**
   - [ ] 재설치/초기화가 필요할 때 `./data/db/app.sqlite3` 와 `./data/downloads/` 를 삭제하면
         깨끗한 상태로 되돌아간다(그 외의 전역 상태는 없다).

## 라이선스 / 사용 주의

이 저장소는 개인 연구/학습용 프로토타입이며, 외부 재배포를 전제로 하지 않는다.
수집 대상 사이트의 이용 약관, 저작권, 개인정보보호법을 준수한 범위 내에서만 사용한다.
