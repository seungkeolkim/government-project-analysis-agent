# government-project-analysis-agent

IRIS(한국연구재단 범부처통합연구지원시스템) 등 정부과제 공고를 자동 수집·저장하고,
로컬 웹에서 공고 목록/상세/첨부파일을 열람할 수 있도록 돕는 개인용 에이전트 프로젝트.

## 구성 개요

- **Scraper**: Playwright headless 브라우저로 IRIS 공고 목록과 상세, 첨부파일(pdf/hwp/hwpx/zip)을 수집
- **DB**: SQLAlchemy 기반 (기본 SQLite). 공고/첨부파일 메타 적재
- **Web**: FastAPI + Jinja2 로 로컬 열람 UI 제공
- **Config**: pydantic-settings 로 `.env` 환경변수 로드

현재 브랜치는 `feature/00005-iris-announcement-scraper` 이며,
이 커밋 시점에는 프로젝트 디렉터리 구조와 설정 스켈레톤만 포함한다.

## 디렉터리 구조

```
.
├── app/
│   ├── config.py             # pydantic-settings 기반 설정 로더
│   ├── logging_setup.py      # 로거 초기화 (loguru)
│   ├── db/                   # SQLAlchemy 모델·세션 (후속 구현)
│   ├── scraper/              # Playwright 스크래퍼 (후속 구현)
│   ├── services/             # 스크래핑→DB 적재 파이프라인 (후속 구현)
│   └── web/                  # FastAPI 라우트·템플릿 (후속 구현)
├── data/
│   ├── db/                   # 로컬 SQLite 파일 위치 (.gitignore)
│   └── downloads/            # 첨부파일 다운로드 위치 (.gitignore)
├── tests/
├── .env.example              # 환경변수 샘플
├── pyproject.toml            # 의존성 및 도구 설정
└── README.md
```

## 빠른 시작 (후속 subtask에서 상세화)

```bash
# 1) 환경변수 준비
cp .env.example .env

# 2) 가상환경 / 의존성은 Setup 단계에서 관리
#    (이 subtask에서는 설치를 수행하지 않는다)
```

세부 실행/배포 방법은 이후 subtask에서 `docker-compose.yml` 및 CLI 스크립트와 함께 보완된다.
