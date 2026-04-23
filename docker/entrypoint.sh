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

# 00032 — docker CLI 조기 진단 (수집 제어 기능의 의존성).
# 바이너리가 없어도 웹 UI 자체는 기동하므로 exit 하지 않고 경고만 남긴다.
# 실제 에러는 수집 트리거 시 runner.py 의 _resolve_docker_binary() 가 발생시킨다.
_docker_found="${DOCKER_BINARY:-$(command -v docker 2>/dev/null || true)}"
if [ -z "$_docker_found" ] || ! [ -x "$_docker_found" ]; then
    echo "[entrypoint] WARNING: docker CLI 를 찾지 못했습니다(경로: ${_docker_found:-없음}). 수집 트리거가 동작하지 않습니다. 이미지를 재빌드하세요: docker compose build app && docker compose up -d app" >&2
fi
unset _docker_found

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
