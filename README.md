# government-project-analysis-agent

IRIS(한국연구재단 범부처통합연구지원시스템), NTIS 등 정부과제 사업공고를 자동 수집·저장하고,
로컬 웹에서 공고 목록/상세/첨부파일을 열람할 수 있도록 돕는 개인용 에이전트 프로젝트.

> 현재 구현된 소스: **IRIS** (완전 구현), **NTIS** (stub — 향후 구현 예정)
> IRIS는 **접수예정·접수중·마감 3개 상태** 공고를 순차 수집한다.
> 증분 수집 전략 적용: 재실행 시 변경 없는 공고는 상세 재수집을 생략하고 기존 데이터를 재사용한다.

## 구성 개요

- **Scraper**: httpx 기반 소스 어댑터 플러그인 구조. IRIS 목록·상세 수집 완전 구현.
  첨부파일(pdf/hwp/hwpx/zip) 다운로드 지원 — Playwright headless 주 경로, httpx 폴백.
- **DB**: SQLAlchemy 2.0 + SQLite (기본). 공고와 첨부파일 메타데이터를 `source_type` 별로 적재.
  증분 수집 전략: `is_current` 플래그로 현재 버전과 이력(구버전)을 구분한다.
- **Web**: FastAPI + Jinja2 로 로컬 전용 열람 UI 및 첨부 다운로드 API 제공.
  소스 유형별 viewer 템플릿(`viewers/<source_type>.html`)으로 상세 표시 분기.
- **Config**: pydantic-settings 기반 `.env` 로더 + `sources.yaml` 소스별 파라미터 설정

## 디렉터리 구조

```
.
├── app/
│   ├── cli.py                  # 스크래퍼 오케스트레이터 CLI (python -m app.cli)
│   ├── config.py               # pydantic-settings 설정 로더
│   ├── logging_setup.py        # loguru 초기화
│   ├── db/                     # SQLAlchemy 모델 / 세션 / repository / migration
│   ├── scraper/
│   │   ├── base.py             # BaseSourceAdapter 추상 클래스
│   │   ├── registry.py         # source_type → 어댑터 매핑
│   │   ├── iris/               # IRIS 어댑터 (list/detail scraper)
│   │   └── ntis/               # NTIS 어댑터 (stub)
│   ├── sources/                # 소스 상수 + sources.yaml 스키마/로더
│   ├── services/               # (예약)
│   └── web/                    # FastAPI 라우트 / 템플릿 / 정적 리소스
│       └── templates/
│           └── viewers/        # 소스 유형별 상세 viewer 템플릿
├── data/
│   ├── db/                     # SQLite 파일 위치 (.gitignore)
│   └── downloads/              # 첨부파일 저장소 (.gitignore)
├── docker/
│   └── Dockerfile
├── tests/
├── .env.example
├── sources.yaml                # 소스별 파라미터 설정 (enabled, base_url, delay 등)
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

## 사전 준비

1. 저장소 루트에서 `.env` 파일을 준비한다.

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

2. 소스별 수집 파라미터는 `sources.yaml` 에서 관리한다.

```yaml
# sources.yaml (저장소 루트)
sources:
  - id: IRIS
    enabled: true
    base_url: https://www.iris.go.kr/...
    request_delay_sec: 1.5
    statuses:           # 수집할 상태 목록 (순차 루프)
      - 접수예정
      - 접수중
      - 마감
    # max_pages: 10          # 소스별 기본 페이지 상한 (CLI 인자 없을 때 사용, 생략 시 코드 default 10)
    # max_announcements: 200 # 소스별 기본 공고 수 상한 (생략 시 코드 default 200)
  - id: NTIS
    enabled: false   # stub — 구현 전까지 비활성화
    base_url: https://www.ntis.go.kr/
    request_delay_sec: 2.0
```

`enabled: false` 인 소스는 자동으로 건너뛴다. `--source IRIS` 플래그로 특정 소스만 실행할 수 있다.

> **참고**: 웹 UI 상의 상태별 필터링(접수예정/접수중/마감 탭 등) 및 소스 필터는 NTIS 구현 이후 task 에서 추가 예정이다. 이번 task(00012)는 스크래퍼 수집 범위 확장만 포함한다.

## 실행 방법 — Docker Compose (권장)

Docker 와 docker compose v2 이상이 설치되어 있어야 한다.

### 1) 웹 UI 기동

```bash
docker compose up app
```

- 최초 실행 시 이미지가 자동 빌드된다(Playwright 베이스 이미지 + 파이썬 의존성).
- 기동 후 브라우저에서 <http://localhost:8000> 로 접속한다.
- 종료는 `Ctrl+C` 또는 별도 터미널에서 `docker compose down`.

### 2) 스크래퍼 1회 실행

웹 UI 와 스크래퍼는 같은 이미지를 공유하지만, 스크래퍼는 장기 실행 서비스가
아니므로 `scrape` 프로파일로 분리되어 있다. 필요할 때만 아래 명령으로 수행한다.

```bash
docker compose --profile scrape run --rm scraper
```

`sources.yaml` 에서 `enabled: true` 인 소스를 순서대로 처리해 SQLite DB 에 적재한다.
재실행 시 변경 없는 공고는 상세 재수집을 생략하고 기존 데이터를 재사용한다(증분 수집).

**CLI 인자는 `docker compose run` 뒤에 바로 붙이면 된다** — `python -m app.cli run` 을 직접 입력할 필요 없다.
인자를 생략하면 소스당 최대 **10 페이지 · 200 건** 이 수집된다(코드 default).
`sources.yaml` 에 소스별 `max_pages` / `max_announcements` 를 설정하면 그 값이 default 로 쓰인다.
우선순위: **CLI 인자 > sources.yaml 소스별 설정 > 코드 default (10 / 200)**

```bash
# 기본값으로 전체 수집 (소스당 최대 10페이지 · 200건)
docker compose --profile scrape run --rm scraper

# 페이지·공고 수 직접 지정
docker compose --profile scrape run --rm scraper --max-pages 3 --max-announcements 50

# 드라이런 — DB/파일 쓰기 없이 수집만 검증 (1페이지만)
docker compose --profile scrape run --rm scraper --max-pages 1 --dry-run

# 특정 소스만 실행
docker compose --profile scrape run --rm scraper --source IRIS

# 첨부파일 다운로드 없이 목록·상세만 수집
docker compose --profile scrape run --rm scraper --skip-attachments
```

수집된 데이터는 호스트의 `./data/db/app.sqlite3` 와 `./data/downloads/{source_type}/{announcement_id}/`
에 영속 저장된다. 컨테이너를 지워도 데이터는 유지된다.

## 실행 방법 — 로컬(비 Docker)

Docker 없이 로컬 파이썬 환경에서 실행할 수도 있다. Python 3.11 이상이 필요하다.

```bash
# 1) 가상환경 생성 및 활성화 (예: venv)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2) 프로젝트를 editable 로 설치
pip install -e .

# 3) Playwright Chromium 브라우저 설치 (첨부파일 다운로드에 필요)
playwright install chromium

# 4) 환경변수 준비 (한 번만)
cp .env.example .env

# 5) 스크래퍼 실행 — 공고 수집 (sources.yaml 의 enabled 소스 전체)
#    인자 생략 시: 소스당 최대 10페이지 · 200건 (코드 default)
python -m app.cli run

# 페이지·공고 수 직접 지정
python -m app.cli run --max-pages 3 --max-announcements 50

# 드라이런 — DB/파일 쓰기 없이 1페이지만 검증
python -m app.cli run --max-pages 1 --dry-run

# IRIS 만 지정해서 실행
python -m app.cli run --source IRIS

# 첨부파일 다운로드 없이 목록·상세만 수집
python -m app.cli run --skip-attachments

# 6) 별도 터미널에서 웹 UI 기동
uvicorn app.web.main:app --host 0.0.0.0 --port 8000
```

브라우저에서 <http://localhost:8000> 로 접속한다.

## 접속 URL 및 데이터 위치

- **웹 UI**: <http://localhost:8000>
  - `/` — 공고 목록 (상태/검색 필터, 페이지네이션)
  - `/announcements/{id}` — 공고 상세 + 첨부파일 목록
  - `/attachments/{id}/download` — 첨부파일 다운로드
  - `/announcements`, `/announcements/{id}.json` — JSON API
- **SQLite DB**: `./data/db/app.sqlite3`
- **첨부파일 저장소**: `./data/downloads/{source_type}/{announcement_id}/`

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

- **로컬 전용.** FastAPI 백엔드는 인증·권한 제어가 없다. 외부에 노출하지 말 것.
- **차단 방지.** 각 소스가 동일 IP 의 과도한 요청을 차단할 수 있다. `sources.yaml` 의
  `request_delay_sec` (기본 1.5초) 를 너무 짧게 설정하지 말고, `--max-pages` 로 범위를 제한해서 사용할 것.
- **User-Agent/봇 정책.** 대상 사이트의 이용 약관, `robots.txt`, 저작권 정책을 확인하고,
  수집한 데이터는 개인 연구·분석 용도로만 활용한다. 재배포·상업적 이용은 금지.
- **첨부파일 크기/시간.** HWP/HWPX/ZIP 첨부가 많은 공고는 다운로드에 시간이 걸릴 수 있다.
  네트워크 장애로 일부 첨부가 실패해도 CLI 는 공고 단위로 예외를 격리해 전체 흐름을
  중단시키지 않는다. 실패한 항목은 로그에서 확인한 뒤 재실행하면 UPSERT 된다.
- **개인정보/민감정보.** 본 프로젝트는 각 소스의 공개 공고만 수집하지만, 저장된 첨부파일에
  사업 담당자 연락처 등이 포함될 수 있다. `./data/` 디렉터리 공유에 유의할 것.

## 최종 수동 검증 체크리스트

마이그레이션이 끝난 직후 또는 새로운 환경에 배포할 때 아래 항목을 순서대로 확인한다.
(자동화된 E2E 테스트는 기본 비활성화되어 있어 이 체크리스트가 최종 게이트 역할을 한다.)

1. **환경 구성**
   - [ ] `.env` 가 생성되어 있고 `DB_URL`, `DOWNLOAD_DIR` 이 의도한 값이다.
   - [ ] `sources.yaml` 에서 수집할 소스가 `enabled: true` 로 설정되어 있다.
   - [ ] `./data/db`, `./data/downloads` 디렉터리가 존재한다(비어 있어도 됨).
2. **Docker 빌드 & 기동**
   - [ ] `docker compose build` 가 에러 없이 완료된다.
   - [ ] `docker compose up app` 후 <http://localhost:8000> 가 200 을 반환한다.
   - [ ] 초기 목록 페이지에 "공고가 없습니다" 빈 상태 UI 가 정상 렌더링된다.
3. **스크래퍼 드라이런**
   - [ ] `docker compose --profile scrape run --rm scraper --max-pages 1 --dry-run`
         실행 시 목록 파싱 로그가 정상 출력되고 DB/파일 쓰기 없이 exit code 0 으로 종료된다.
4. **스크래퍼 실제 실행 및 증분 동작 확인**
   - [ ] `docker compose --profile scrape run --rm scraper --max-pages 1` 실행 후
         `./data/db/app.sqlite3` 가 생성된다.
   - [ ] 같은 명령을 재실행하면 변경 없는 공고는 `상세 수집 생략(변경 없음)` 로그가 출력되고,
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
6. **로컬(비 Docker) 경로도 실행 가능한지 샘플 확인**
   - [ ] `pip install -e . && playwright install chromium` 이후
         `python -m app.cli run --max-pages 1 --dry-run` 이 동작한다.
   - [ ] `uvicorn app.web.main:app --host 0.0.0.0 --port 8000` 로 동일 UI 가 기동된다.
7. **데이터 정리**
   - [ ] 재설치/초기화가 필요할 때 `./data/db/app.sqlite3` 와 `./data/downloads/` 를 삭제하면
         깨끗한 상태로 되돌아간다(그 외의 전역 상태는 없다).

## 라이선스 / 사용 주의

이 저장소는 개인 연구/학습용 프로토타입이며, 외부 재배포를 전제로 하지 않는다.
수집 대상 사이트의 이용 약관, 저작권, 개인정보보호법을 준수한 범위 내에서만 사용한다.
