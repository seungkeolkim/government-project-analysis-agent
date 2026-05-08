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
        exec docker compose -f "$COMPOSE_FILE" up app "$@"
        ;;
    down)
        exec docker compose -f "$COMPOSE_FILE" down "$@"
        ;;
    build)
        exec docker compose -f "$COMPOSE_FILE" build "$@"
        ;;
    logs)
        exec docker compose -f "$COMPOSE_FILE" logs "$@"
        ;;
    restart)
        exec docker compose -f "$COMPOSE_FILE" restart "$@"
        ;;
    ps)
        exec docker compose -f "$COMPOSE_FILE" ps "$@"
        ;;
    scrape)
        # scraper 서비스를 1회 실행 후 컨테이너를 제거한다.
        exec docker compose -f "$COMPOSE_FILE" --profile scrape run --rm scraper "$@"
        ;;
    *)
        printf 'ERROR: 알 수 없는 서브커맨드: %s\n' "$subcommand" >&2
        usage
        exit 2
        ;;
esac
