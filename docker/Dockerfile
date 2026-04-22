# IRIS 사업공고 목록 수집 및 로컬 웹 UI 실행용 Docker 이미지
#
# Playwright headless chromium 을 사용한 첨부파일 다운로드를 지원한다.
# python:3.12-slim 베이스 위에 playwright install --with-deps chromium 으로
# Chromium 및 시스템 의존성을 설치한다 (이미지 크기 ~400 MB 증가).

FROM python:3.12-slim

# 파이썬/pip 런타임 기본 옵션
#  - PYTHONUNBUFFERED: 로그가 버퍼링 없이 즉시 출력되도록 한다.
#  - PYTHONDONTWRITEBYTECODE: 컨테이너 내부에 .pyc 를 남기지 않는다.
#  - PIP_NO_CACHE_DIR: 빌드 이미지 크기 축소.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 컨테이너 내부 작업 디렉터리. 파이썬 패키지 `app` 는 /app/app 에 위치하게 된다.
WORKDIR /app

# 1) 의존성 선언부터 복사하여 코드 변경 시 레이어 캐시 무효화를 최소화한다.
COPY pyproject.toml README.md ./

# 2) 애플리케이션 소스 복사.
#    compose 개발 모드에서는 ./app 이 바인드 마운트로 덮어써지지만,
#    이미지 자체도 단독 실행이 가능하도록 복사해 둔다.
COPY app ./app

# 3) Alembic 설정 및 migration 파일 복사.
#    entrypoint.sh 가 `alembic upgrade head` 를 실행하므로
#    alembic.ini 와 alembic/ 디렉터리가 /app 아래에 있어야 한다.
#    init_db.py 가 PROJECT_ROOT / 'alembic.ini' 로 경로를 계산하며
#    PROJECT_ROOT = /app 이므로 /app/alembic.ini 가 기대 위치다.
COPY alembic.ini ./
COPY alembic ./alembic

# 4) 파이썬 의존성 및 패키지 editable 설치.
#    editable(-e) 로 설치해야 compose 의 ./app 바인드 마운트와 함께
#    호스트의 소스 변경이 즉시 반영된다.
RUN pip install --upgrade pip && pip install -e .

# 5) Playwright headless chromium 브라우저 설치.
#    --with-deps: python:3.12-slim 에 없는 시스템 라이브러리(libnss3, libatk,
#    libgbm 등)를 함께 설치한다. root 권한이 필요하며 이미지 크기가 ~400 MB 증가한다.
#    브라우저 바이너리는 /root/.cache/ms-playwright 에 저장된다.
#
#    빌드 후 smoke 테스트:
#      docker compose --profile scrape run --rm scraper \
#        python -c 'from playwright.async_api import async_playwright; print("ok")'
RUN playwright install --with-deps chromium

# 6) 런타임에 사용되는 로컬 디렉터리 기본 생성.
#    compose 의 ./data 볼륨이 이를 덮어쓰지만, 단독 컨테이너 실행 시에도 동작하도록 준비한다.
RUN mkdir -p /app/data/db /app/data/downloads

# 7) 엔트리포인트 스크립트.
#    sources.yaml per-run copy 격리 및 SOURCES_CONFIG_PATH 주입을 담당한다.
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# FastAPI 로컬 웹 UI 노출 포트.
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]

# 기본 CMD: FastAPI 로컬 웹 UI 기동.
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
