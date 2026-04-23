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

# 2b) 운영자 CLI 스크립트(create_admin.py 등) 노출.
COPY scripts ./scripts

# 3) Alembic 설정 및 migration 파일 복사.
#    entrypoint.sh 가 `alembic upgrade head` 를 실행하므로
#    alembic.ini 와 alembic/ 디렉터리가 /app 아래에 있어야 한다.
#    init_db.py 가 PROJECT_ROOT / 'alembic.ini' 로 경로를 계산하며
#    PROJECT_ROOT = /app 이므로 /app/alembic.ini 가 기대 위치다.
COPY alembic.ini ./
COPY alembic ./alembic

# 3b) sources.yaml.template 복사.
#     entrypoint.sh 가 sources.yaml 마운트 없이 기동될 때 폴백으로 사용한다.
#     실제 sources.yaml 은 compose 의 바인드 마운트가 주입하며, 이 template 은
#     마운트가 누락된 경우에만 활성화된다.
COPY sources.yaml.template ./

# 3c) docker-compose.yml 이미지 내 복사 (Phase 2 / 00025).
#     app 컨테이너의 runner 가 subprocess 로 `docker compose ...` 를 호출할 때
#     참조할 compose 파일. `-f /app/docker-compose.yml` 로 명시 전달한다.
#     compose CLI 는 이 파일을 **읽기만** 하고, 실제 mount/service 관리는
#     호스트 dockerd 가 수행한다(docker.sock 마운트로 원격 조작).
#     상대 경로(예: ./app)는 `docker compose --project-directory $HOST_PROJECT_DIR`
#     를 통해 HOST 기준 절대경로로 해석된다. docs/scrape_control_design.md §5.3 참조.
COPY docker-compose.yml ./

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

# 6) docker CLI + compose v2 plugin 설치 (Phase 2 / 00025 웹 수집 제어용).
#    - 웹이 subprocess 로 `docker compose --profile scrape run --rm scraper` 를 호출해
#      스크래퍼 컨테이너를 기동/중단/스케줄하려면 app 컨테이너 안에 docker CLI 가 있어야 한다.
#    - Debian bookworm 기본 저장소의 `docker.io` 패키지를 그대로 사용한다(검증 시간 단축을
#      위해 Docker 공식 apt 저장소 등록은 생략). 이 패키지는 docker 데몬도 함께 설치하지만,
#      컨테이너 내부에서는 dockerd 를 기동하지 않는다 — docker-in-docker 가 아니라
#      호스트의 /var/run/docker.sock 을 compose 바인드 마운트로 받아 호스트 dockerd 를
#      원격 조작하는 방식이다. docker-compose.yml 및 docs/scrape_control_design.md §5 참조.
#    - docker.io 는 compose v2 플러그인을 포함하지 않으므로, 공식 compose v2 바이너리를
#      /usr/libexec/docker/cli-plugins 에 직접 배치해 `docker compose` 서브커맨드를 활성화한다.
#      (compose v1 의 `docker-compose` 대신 v2 의 `docker compose` 를 요구하는 사용자 원문 준수.
#       v2 는 SIGTERM 을 관리 컨테이너로 전파하는 공식 동작이 검증되어 있어 본 task 의
#       중단 흐름에 필수 — docs/scrape_control_design.md §6.3 참조.)
#    - 이미지 크기 증가: docker.io(~80 MB) + compose plugin(~60 MB) ≈ 140 MB.
#    - **보안 경고**: /var/run/docker.sock 마운트는 호스트 root 권한 부여와 동등하다
#      (docs/scrape_control_design.md §15.1). FastAPI 의 외부 노출 금지 전제가 이 구성으로
#      더 강하게 요구된다.
#
# ARG 로 버전을 외부 주입 가능하게 하되, 기본값은 reproducible build 를 위해 고정한다.
ARG DOCKER_COMPOSE_PLUGIN_VERSION=v2.29.7
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        docker.io \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /usr/libexec/docker/cli-plugins && \
    curl --fail --silent --show-error --location \
        "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_PLUGIN_VERSION}/docker-compose-linux-$(uname -m)" \
        -o /usr/libexec/docker/cli-plugins/docker-compose && \
    chmod +x /usr/libexec/docker/cli-plugins/docker-compose

# 7) 런타임에 사용되는 로컬 디렉터리 기본 생성.
#    compose 의 ./data 볼륨이 이를 덮어쓰지만, 단독 컨테이너 실행 시에도 동작하도록 준비한다.
RUN mkdir -p /app/data/db /app/data/downloads

# 8) 엔트리포인트 스크립트.
#    sources.yaml per-run copy 격리 및 SOURCES_CONFIG_PATH 주입을 담당한다.
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# FastAPI 로컬 웹 UI 노출 포트.
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]

# 기본 CMD: FastAPI 로컬 웹 UI 기동.
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
