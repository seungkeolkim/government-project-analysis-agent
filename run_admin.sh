#!/bin/sh
# 관리자 도구 전용 wrapper.
#
# 서브커맨드에 따라 관리자 작업용 docker compose run 을 호출한다.
# compose 파일은 이 스크립트와 같은 디렉터리의 docker-compose.yml 을 사용한다.
#
# 사용법: ./run_admin.sh <subcommand> [args...]

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

usage() {
    cat <<'USAGE' >&2
사용법: ./run_admin.sh <subcommand> [args...]

서브커맨드:
  create-admin [args...]  관리자 계정 생성 (미입력 항목은 대화형으로 질문)
                          예: ./run_admin.sh create-admin
                              ./run_admin.sh create-admin root_user --email admin@example.com
USAGE
}

if [ "$#" -lt 1 ]; then
    usage
    exit 2
fi

subcommand="$1"
shift

case "$subcommand" in
    create-admin)
        # app 컨테이너 내에서 create_admin.py 를 실행한다. 실행 후 컨테이너를 제거한다.
        exec docker compose -f "$COMPOSE_FILE" run --rm app python scripts/python/create_admin.py "$@"
        ;;
    *)
        printf 'ERROR: 알 수 없는 서브커맨드: %s\n' "$subcommand" >&2
        usage
        exit 2
        ;;
esac
