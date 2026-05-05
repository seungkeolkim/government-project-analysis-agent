#!/bin/sh
# 컨테이너 엔트리포인트.
#
# sources.yaml 을 per-run 임시 복사본으로 격리하여 동시 실행 시 경합을 방지한다.
# SOURCES_CONFIG_PATH 환경변수를 복사본 경로로 설정한 뒤 CMD 또는 인자를 exec 한다.
#
# sources.yaml 우선순위:
#   1. compose 바인드 마운트 (/run/config/sources.yaml) — 정상 경로
#   2. 이미지 내 template (/app/sources.yaml.template) — 마운트 누락 시 폴백
#      (호스트에서 sources.yaml 을 생성하지 않은 경우. 기본값으로 동작하며
#       실제 설정 반영을 위해 bootstrap_sources.sh 실행 후 재기동을 권장한다)

set -eu

# 빌드 시 HOST_UID 가 주입되지 않았거나 다른 장비에서 --no-cache 없이 이미지를 재사용한 경우,
# 현재 UID 에 대응하는 /etc/passwd 엔트리가 없어 whoami 가 실패하고
# 프롬프트에 "I have no name!" 이 표시된다. 자동 패치는 root 권한 없이 불가능하므로
# 명확한 경고 메시지만 출력하고 기동은 계속한다.
if ! id -nu "$(id -u)" >/dev/null 2>&1; then
    printf '[entrypoint] WARNING: 현재 UID(%s) 에 대응하는 /etc/passwd 엔트리가 없습니다.\n' "$(id -u)" >&2
    printf '[entrypoint] HOST_UID 변경 후 docker compose build 를 실행했는지 확인하세요.\n' >&2
fi

SOURCES_YAML_MOUNT="${SOURCES_YAML_MOUNT:-/run/config/sources.yaml}"
SOURCES_YAML_TEMPLATE="/app/sources.yaml.template"

# 마운트된 sources.yaml 이 있으면 임시 디렉터리로 격리한다.
if [ -f "$SOURCES_YAML_MOUNT" ]; then
    umask 077
    _tmpdir="$(mktemp -d)"
    trap 'rm -rf "$_tmpdir"' EXIT INT TERM
    cp "$SOURCES_YAML_MOUNT" "$_tmpdir/sources.yaml"
    export SOURCES_CONFIG_PATH="$_tmpdir/sources.yaml"
elif [ -f "$SOURCES_YAML_TEMPLATE" ]; then
    # 마운트가 없거나 디렉터리인 경우: 이미지 내 template 을 폴백으로 사용한다.
    # 호스트에서 sources.yaml 을 생성하지 않은 최초 기동 상황에 해당한다.
    echo "[entrypoint] WARNING: sources.yaml 마운트 없음 — template 기본값으로 기동합니다." >&2
    echo "[entrypoint] 설정 반영은: sh scripts/bootstrap_sources.sh 실행 후 컨테이너 재기동" >&2
    umask 077
    _tmpdir="$(mktemp -d)"
    trap 'rm -rf "$_tmpdir"' EXIT INT TERM
    cp "$SOURCES_YAML_TEMPLATE" "$_tmpdir/sources.yaml"
    export SOURCES_CONFIG_PATH="$_tmpdir/sources.yaml"
fi

# alembic 명령을 직접 실행할 때는 migration 자동 적용을 건너뛴다
# (예: docker compose run ... alembic upgrade head — 재귀 방지)
if [ "${1:-}" != "alembic" ]; then
    alembic upgrade head || exit 1
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
else
    exec python -m app.cli
fi
