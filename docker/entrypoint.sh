#!/bin/sh
# 컨테이너 엔트리포인트.
#
# sources.yaml 을 per-run 임시 복사본으로 격리하여 동시 실행 시 경합을 방지한다.
# SOURCES_CONFIG_PATH 환경변수를 복사본 경로로 설정한 뒤 CMD 또는 인자를 exec 한다.

set -eu

SOURCES_YAML_MOUNT="${SOURCES_YAML_MOUNT:-/run/config/sources.yaml}"

# 마운트된 sources.yaml 이 있으면 임시 디렉터리로 격리한다.
if [ -f "$SOURCES_YAML_MOUNT" ]; then
    umask 077
    _tmpdir="$(mktemp -d)"
    trap 'rm -rf "$_tmpdir"' EXIT INT TERM
    cp "$SOURCES_YAML_MOUNT" "$_tmpdir/sources.yaml"
    export SOURCES_CONFIG_PATH="$_tmpdir/sources.yaml"
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
else
    exec python -m app.cli
fi
