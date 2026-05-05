# IRIS 사업공고 목록 수집 및 로컬 웹 UI 실행용 Docker 이미지
#
# Playwright headless chromium 을 사용한 첨부파일 다운로드를 지원한다.
# python:3.12-slim 베이스 위에 playwright install --with-deps chromium 으로
# Chromium 및 시스템 의존성을 설치한다 (이미지 크기 ~400 MB 증가).
#
# ─── 레이어 순서 원칙: 변경 빈도 오름차순 ────────────────────────────────────
# docker compose build 시 상위 레이어가 캐시 hit 되도록, 거의 변경되지 않는
# 무거운 레이어를 가장 위로 모은다. 특히 chromium 다운로드(>200 MB)와 apt
# 설치(~140 MB) 는 애플리케이션 소스 변경에 의해 절대 무효화되지 않도록 한다.
#
#   Layer 1) 시스템 패키지 + docker CLI/compose plugin   ← 거의 변경 없음
#   Layer 2) playwright 패키지 + chromium 브라우저       ← ARG 버전 고정
#   Layer 3) pyproject.toml / README.md + editable 설치  ← 의존성 변경 시
#   Layer 4) 애플리케이션 소스(app, scripts, alembic, …) ← 가장 자주 변경
#   Layer 5) 런타임 디렉터리 생성 및 권한 부여
#
# playwright/chromium 버전은 ARG 로 고정해 두 ARG 값이 모두 같은 한 빌드
# 산출물이 재사용된다. PR 리뷰가 "운영 핀"(Dockerfile ARG) 과 "의존성 호환
# 범위"(pyproject.toml 의 `playwright>=1.44,<2.0`) 를 분리해서 검토할 수 있도록
# 의도적으로 두 곳에 나눠 두었다.

FROM python:3.12-slim

# 파이썬/pip 런타임 기본 옵션
#  - PYTHONUNBUFFERED: 로그가 버퍼링 없이 즉시 출력되도록 한다.
#  - PYTHONDONTWRITEBYTECODE: 컨테이너 내부에 .pyc 를 남기지 않는다.
#  - PIP_NO_CACHE_DIR: 빌드 이미지 크기 축소.
#  - PLAYWRIGHT_BROWSERS_PATH: playwright install 이전에 선언해야 이 경로로 바이너리가
#    설치된다. /root/.cache 대신 /opt/playwright 에 설치해 비특권 UID 에서도 접근 가능.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# 컨테이너 실행 유저 UID/GID (docker-compose.yml 의 build.args 로 주입).
# 기본값 1000 은 python:3.12-slim 베이스에서 미점유이지만,
# 향후 베이스 이미지 변경에 대비해 이후 Layer 5a 에서 idempotent useradd 로 처리한다.
ARG HOST_UID=1000
ARG HOST_GID=1000

# 컨테이너 내부 작업 디렉터리. 파이썬 패키지 `app` 는 /app/app 에 위치하게 된다.
WORKDIR /app

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — 시스템 패키지 + docker CLI + compose v2 plugin (Phase 2 / 00025).
# ─────────────────────────────────────────────────────────────────────────────
# - 웹이 subprocess 로 `docker compose --profile scrape run --rm scraper` 를 호출해
#   스크래퍼 컨테이너를 기동/중단/스케줄하려면 app 컨테이너 안에 docker CLI 가 있어야 한다.
# - Debian bookworm 기본 저장소의 `docker.io` 패키지를 그대로 사용한다(검증 시간 단축을
#   위해 Docker 공식 apt 저장소 등록은 생략). 이 패키지는 docker 데몬도 함께 설치하지만,
#   컨테이너 내부에서는 dockerd 를 기동하지 않는다 — docker-in-docker 가 아니라
#   호스트의 /var/run/docker.sock 을 compose 바인드 마운트로 받아 호스트 dockerd 를
#   원격 조작하는 방식이다. docker-compose.yml 및 docs/scrape_control_design.md §5 참조.
# - docker.io 는 compose v2 플러그인을 포함하지 않으므로, 공식 compose v2 바이너리를
#   /usr/libexec/docker/cli-plugins 에 직접 배치해 `docker compose` 서브커맨드를 활성화한다.
#   (compose v1 의 `docker-compose` 대신 v2 의 `docker compose` 를 요구하는 사용자 원문 준수.
#    v2 는 SIGTERM 을 관리 컨테이너로 전파하는 공식 동작이 검증되어 있어 본 task 의
#    중단 흐름에 필수 — docs/scrape_control_design.md §6.3 참조.)
# - 이미지 크기 증가: docker.io(~80 MB) + compose plugin(~60 MB) ≈ 140 MB.
# - **보안 경고**: /var/run/docker.sock 마운트는 호스트 root 권한 부여와 동등하다
#   (docs/scrape_control_design.md §15.1). FastAPI 의 외부 노출 금지 전제가 이 구성으로
#   더 강하게 요구된다.
#
# ARG 로 버전을 외부 주입 가능하게 하되, 기본값은 reproducible build 를 위해 고정한다.
ARG DOCKER_COMPOSE_PLUGIN_VERSION=v2.29.7
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        docker.io \
        docker-cli \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /usr/libexec/docker/cli-plugins && \
    curl --fail --silent --show-error --location \
        "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_PLUGIN_VERSION}/docker-compose-linux-$(uname -m)" \
        -o /usr/libexec/docker/cli-plugins/docker-compose && \
    chmod +x /usr/libexec/docker/cli-plugins/docker-compose

# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — Playwright 패키지 + headless chromium 브라우저.
# ─────────────────────────────────────────────────────────────────────────────
# 캐시 안정성을 위해 playwright 버전을 ARG 로 고정한다.
#  - 단독 패키지로 먼저 설치하므로 pyproject.toml 변경이 이 레이어를 무효화하지 않는다.
#  - playwright 패키지가 호환 chromium revision 을 결정하므로 chromium 도 자동 고정.
#  - 기본값 1.58.0 은 pyproject.toml 의 `playwright>=1.44,<2.0` 호환 범위 내에서
#    uv.lock 이 현재 resolve 하는 버전과 일치(2026-01 릴리스). 의존성 호환 범위는
#    pyproject.toml 에 그대로 두어, 운영 핀과 호환 범위를 분리 관리한다.
#
# --with-deps: python:3.12-slim 에 없는 시스템 라이브러리(libnss3, libatk,
# libgbm 등)를 함께 설치한다. root 권한이 필요하며 이미지 크기가 ~400 MB 증가한다.
# 브라우저 바이너리는 PLAYWRIGHT_BROWSERS_PATH(/opt/playwright) 에 저장된다.
#
# 빌드 후 smoke 테스트:
#   docker compose --profile scrape run --rm scraper \
#     python -c 'from playwright.async_api import async_playwright; print("ok")'
#
# 대문자 X (chmod a+rX): 디렉터리 및 이미 실행 비트가 있는 파일에만 x 추가
# (바이너리 실행권 보존). 비특권 UID 로 컨테이너가 기동될 때 chromium
# 바이너리에 접근 가능하도록 한다.
ARG PLAYWRIGHT_VERSION=1.58.0
RUN pip install --upgrade pip && \
    pip install "playwright==${PLAYWRIGHT_VERSION}" && \
    playwright install --with-deps chromium && \
    chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — pyproject.toml 의존성 + editable 설치.
# ─────────────────────────────────────────────────────────────────────────────
# 의존성 선언만 먼저 복사하여 소스 변경 시 이 RUN 레이어가 무효화되지 않도록 한다.
# README.md 는 pyproject.toml 의 `readme = "README.md"` 설정 때문에 빌드에 필요하다.
COPY pyproject.toml README.md ./

# editable 설치(`pip install -e .`) 는 setuptools 의 packages.find 가 디스크에서
# `app/__init__.py` 를 찾을 수 있어야 metadata 가 생성된다. 스텁을 먼저 만들어
# 의존성만 설치하고, 실제 소스는 뒤의 `COPY app ./app` 가 이 스텁을 덮어쓴다.
# editable 의 .pth 는 /app 을 sys.path 에 추가하므로 런타임에는 덮어쓴 실제
# 파일이 import 된다. compose 의 ./app 바인드 마운트와도 자연스럽게 호환된다.
#
# 이미 위 레이어에서 playwright==${PLAYWRIGHT_VERSION} 이 설치되어 있고,
# pyproject.toml 의 `playwright>=1.44,<2.0` 을 만족하므로 pip 가
# "Requirement already satisfied" 로 통과한다(다운로드/재설치 없음).
RUN mkdir -p app && touch app/__init__.py && \
    pip install -e .

# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — 애플리케이션 소스 및 빌드 시 함께 묶이는 보조 파일들 (가장 자주 변경).
# ─────────────────────────────────────────────────────────────────────────────
# compose 개발 모드에서는 ./app 이 바인드 마운트로 덮어써지지만, 이미지 자체도
# 단독 실행이 가능하도록 복사해 둔다. (앞서 만든 스텁 app/__init__.py 를 덮어쓴다.)
COPY app ./app

# 운영자 CLI 스크립트(create_admin.py 등) 노출.
COPY scripts ./scripts

# Alembic 설정 및 migration 파일 복사.
# entrypoint.sh 가 `alembic upgrade head` 를 실행하므로 alembic.ini 와
# alembic/ 디렉터리가 /app 아래에 있어야 한다. init_db.py 가
# PROJECT_ROOT / 'alembic.ini' 로 경로를 계산하며 PROJECT_ROOT = /app 이므로
# /app/alembic.ini 가 기대 위치다.
COPY alembic.ini ./
COPY alembic ./alembic

# sources.yaml.template 복사.
# entrypoint.sh 가 sources.yaml 마운트 없이 기동될 때 폴백으로 사용한다.
# 실제 sources.yaml 은 compose 의 바인드 마운트가 주입하며, 이 template 은
# 마운트가 누락된 경우에만 활성화된다.
COPY sources.yaml.template ./

# docker-compose.yml 이미지 내 복사 (Phase 2 / 00025).
# app 컨테이너의 runner 가 subprocess 로 `docker compose ...` 를 호출할 때
# 참조할 compose 파일. `-f /app/docker-compose.yml` 로 명시 전달한다.
# compose CLI 는 이 파일을 **읽기만** 하고, 실제 mount/service 관리는
# 호스트 dockerd 가 수행한다(docker.sock 마운트로 원격 조작).
# 상대 경로(예: ./app)는 `docker compose --project-directory $HOST_PROJECT_DIR`
# 를 통해 HOST 기준 절대경로로 해석된다. docs/scrape_control_design.md §5.3 참조.
COPY docker-compose.yml ./

# 엔트리포인트 스크립트.
# sources.yaml per-run copy 격리 및 SOURCES_CONFIG_PATH 주입을 담당한다.
COPY docker/entrypoint.sh /app/entrypoint.sh

# ─────────────────────────────────────────────────────────────────────────────
# Layer 5a — 컨테이너 실행 유저 생성 (호스트 UID/GID 대응).
# ─────────────────────────────────────────────────────────────────────────────
# 호스트의 UID:GID 로 컨테이너가 기동될 때 /etc/passwd 에 엔트리가 없으면
# `whoami` 가 실패하고 프롬프트에 "I have no name!" 이 표시된다.
#
# idempotent 분기: 이미 해당 GID/UID 가 존재하면 그대로 사용 (베이스 이미지 변경 대비).
#   -M : home 디렉터리 생성 생략 (HOME=/app 이미 존재)
#   -N : 동명 primary group 생성 생략 (위에서 groupadd 한 GID 를 primary 로 사용)
RUN set -eux; \
    if ! getent group "${HOST_GID}" >/dev/null; then \
        groupadd -g "${HOST_GID}" appuser; \
    fi; \
    if ! getent passwd "${HOST_UID}" >/dev/null; then \
        useradd -u "${HOST_UID}" -g "${HOST_GID}" -d /app -s /bin/bash -M -N appuser; \
    fi

# ─────────────────────────────────────────────────────────────────────────────
# Layer 5 — 런타임 디렉터리 및 권한.
# ─────────────────────────────────────────────────────────────────────────────
# - 런타임 데이터 디렉터리 기본 생성: compose 의 ./data 볼륨이 이를 덮어쓰지만,
#   단독 컨테이너 실행 시에도 동작하도록 준비한다. data/logs 를 명시 생성하여
#   비특권 UID 기동 시에도 로그 디렉터리가 존재하도록 한다.
# - /app 하위 전체: 코드/패키지를 임의 UID 가 읽을 수 있도록 a+rX (쓰기 비트는 건드리지 않음).
# - /app/data 하위: SQLite DB·다운로드·로그 등 런타임 쓰기 대상. a+rwX 로 추가 허용.
#   compose 바인드 마운트(./data)가 있을 때는 호스트 디렉터리 권한이 우선하나,
#   단독 컨테이너 실행 시에도 비특권 UID 가 파일을 생성할 수 있어야 한다.
RUN mkdir -p /app/data/db /app/data/downloads /app/data/logs && \
    chmod +x /app/entrypoint.sh && \
    chmod -R a+rX /app && \
    chmod -R a+rwX /app/data

# FastAPI 로컬 웹 UI 노출 포트.
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]

# 기본 CMD: FastAPI 로컬 웹 UI 기동.
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
