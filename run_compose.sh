#!/bin/sh
# 서버 구동 전용 wrapper.
#
# 서브커맨드에 따라 docker compose 를 호출한다.
# compose 파일은 이 스크립트와 같은 디렉터리의 docker-compose.yml 을 사용한다.
#
# 사용법: ./run_compose.sh <subcommand> [args...]

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

# 모든 docker compose 호출에 `--project-directory "$SCRIPT_DIR"` 를 명시한다(00134).
#
# docker compose 는 compose 파일 안의 `${VAR}` 보간 값을 (1) 호출 쉘 환경과
# (2) 프로젝트 디렉터리의 .env 파일에서 찾는다. project-directory 를 명시하지
# 않으면 호출 시점의 CWD/compose 버전에 따라 보간에 쓰이는 .env 탐색 위치가
# 달라질 수 있고, 그 결과 `${HOST_PROJECT_DIR}` 같은 값이 빈 문자열로 떨어져
# (docker-compose.yml 의 .env 마운트 타겟 등이) 어긋나는 간헐적 버그가 있었다.
# project-directory 를 프로젝트 루트로 고정하면 호출 CWD 와 무관하게 보간이
# 항상 프로젝트 루트의 .env 를 사용하므로 동작이 결정론적이 된다.
#
# 아래 각 서브커맨드는 `-f "$COMPOSE_FILE" --project-directory "$SCRIPT_DIR"`
# 를 동일하게 명시한다. (경로에 공백이 있을 수 있어 단어 분리를 피하려고
# 변수로 묶지 않고 각 호출에 따옴표로 직접 전개한다.)

usage() {
    cat <<'USAGE' >&2
사용법: ./run_compose.sh <subcommand> [args...]

서브커맨드:
  up [args...]       앱 서버 기동 (기본 대상: app 서비스)
                     예: ./run_compose.sh up            # 포어그라운드
                         ./run_compose.sh up -d         # 백그라운드
  down [args...]     서비스 중지 및 컨테이너 제거
                     예: ./run_compose.sh down
  build [args...]    이미지 빌드
                     예: ./run_compose.sh build
                         ./run_compose.sh build --no-cache
  logs [args...]     로그 출력
                     예: ./run_compose.sh logs -f app
  restart [args...]  서비스 재시작
                     예: ./run_compose.sh restart app
  ps                 실행 중인 서비스 목록
                     예: ./run_compose.sh ps
  scrape [args...]   스크래퍼 1회 실행 후 종료
                     예: ./run_compose.sh scrape
USAGE
}

if [ "$#" -lt 1 ]; then
    usage
    exit 2
fi

subcommand="$1"
shift

case "$subcommand" in
    up)
        # 기본 대상 서비스는 app. -d 를 포함한 추가 인자는 그대로 위임된다.
        exec docker compose -f "$COMPOSE_FILE" --project-directory "$SCRIPT_DIR" up app "$@"
        ;;
    down)
        exec docker compose -f "$COMPOSE_FILE" --project-directory "$SCRIPT_DIR" down "$@"
        ;;
    build)
        exec docker compose -f "$COMPOSE_FILE" --project-directory "$SCRIPT_DIR" build "$@"
        ;;
    logs)
        exec docker compose -f "$COMPOSE_FILE" --project-directory "$SCRIPT_DIR" logs "$@"
        ;;
    restart)
        exec docker compose -f "$COMPOSE_FILE" --project-directory "$SCRIPT_DIR" restart "$@"
        ;;
    ps)
        exec docker compose -f "$COMPOSE_FILE" --project-directory "$SCRIPT_DIR" ps "$@"
        ;;
    scrape)
        # scraper 서비스를 1회 실행 후 컨테이너를 제거한다.
        exec docker compose -f "$COMPOSE_FILE" --project-directory "$SCRIPT_DIR" --profile scrape run --rm scraper "$@"
        ;;
    *)
        printf 'ERROR: 알 수 없는 서브커맨드: %s\n' "$subcommand" >&2
        usage
        exit 2
        ;;
esac
