#!/bin/sh
# docker compose 진입점 wrapper.
#
# 첫 번째 인자(mode) 에 따라 어떤 compose 파일 조합으로 docker compose 를
# 호출할지를 결정한 뒤, 나머지 인자는 docker compose 에 그대로 전달한다.
#
# 사용 예시:
#   scripts/compose.sh dev up app
#   scripts/compose.sh dev --profile scrape run --rm scraper
#   scripts/compose.sh prod up app
#   scripts/compose.sh prod build
#
# mode:
#   dev  — docker-compose.yml + docker-compose.dev.yml (uvicorn --reload 활성화).
#          호스트 ./app 바인드 마운트와 짝을 이뤄 코드 변경이 자동 반영된다.
#   prod — docker-compose.yml 만 사용 (이미지에 고정된 코드로 단일 프로세스 기동).
#
# 동작 규칙:
#   - mode 가 누락되었거나 dev/prod 외의 값이면 사용법을 출력하고 exit 2.
#   - mode 분기 후에는 `exec docker compose ...` 로 PID 를 인계한다.
#   - 환경변수 주입, 사용자명 입력, 색상 출력 등 부가기능은 의도적으로 두지 않는다.

set -eu

usage() {
    cat <<'USAGE' >&2
사용법: scripts/compose.sh <mode> [docker compose args...]

  mode:
    dev  — docker-compose.yml + docker-compose.dev.yml (개발: 코드 변경 자동 반영)
    prod — docker-compose.yml 만 사용 (운영: 이미지 코드 고정)

예시:
  scripts/compose.sh dev up app
  scripts/compose.sh dev --profile scrape run --rm scraper
  scripts/compose.sh prod up app
  scripts/compose.sh prod build
USAGE
}

if [ "$#" -lt 1 ]; then
    usage
    exit 2
fi

mode="$1"
shift

case "$mode" in
    dev)
        exec docker compose -f docker-compose.yml -f docker-compose.dev.yml "$@"
        ;;
    prod)
        exec docker compose -f docker-compose.yml "$@"
        ;;
    *)
        echo "ERROR: 알 수 없는 mode: $mode" >&2
        usage
        exit 2
        ;;
esac
