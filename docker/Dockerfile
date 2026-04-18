# IRIS 사업공고 스크래퍼 및 로컬 웹 UI 실행용 Docker 이미지
#
# - Playwright 공식 이미지를 베이스로 사용하여 Chromium 등 브라우저 의존성을
#   OS 패키지 레벨에서 미리 포함한다. (pyproject 의 playwright>=1.44,<2.0 과 일치하는 태그)
# - noble(Ubuntu 24.04) 변형을 사용해 Python 3.12 를 확보한다.
#   jammy 변형은 Python 3.10 이라 pyproject 의 requires-python=">=3.11,<3.13" 과 충돌한다.
# - 하나의 이미지로 웹 UI(app 서비스)와 스크래퍼 CLI(scraper 서비스)를 모두 실행한다.
# - 실제 엔트리포인트(app.cli, app.web.main) 는 후속 subtask 에서 구현되며,
#   본 subtask 에서는 CMD 가 경로만 정확히 가리키는 placeholder 상태다.

FROM mcr.microsoft.com/playwright/python:v1.47.0-noble

# 파이썬/피프 런타임 기본 옵션
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

# 3) 파이썬 의존성 및 패키지 editable 설치.
#    editable(-e) 로 설치해야 compose 의 ./app 바인드 마운트와 함께
#    호스트의 소스 변경이 즉시 반영된다.
RUN pip install --upgrade pip
RUN pip install -e .

# 4) 런타임에 사용되는 로컬 디렉터리 기본 생성.
#    compose 의 ./data 볼륨이 이를 덮어쓰지만, 단독 컨테이너 실행 시에도 동작하도록 준비한다.
RUN mkdir -p /app/data/db /app/data/downloads

# FastAPI 로컬 웹 UI 노출 포트.
EXPOSE 8000

# 기본 CMD: FastAPI 로컬 웹 UI 기동.
#   - `app.web.main:app` 은 후속 subtask(web 구현) 에서 정의된다.
#   - compose 에서는 서비스별로 command 를 재정의하여 scraper CLI 를 실행할 수 있다.
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
