#!/bin/sh
# sources.yaml 초기 설정 부트스트랩 스크립트.
#
# sources.yaml.template 을 sources.yaml 로 복사한다.
# sources.yaml 이 이미 존재하면 덮어쓰지 않는다 (idempotent).
#
# sources.yaml 은 .gitignore 대상이므로 브랜치 전환 시 유지된다.
#
# 사용법:
#   sh ./bootstrap_sources.sh
#
# 초기 설정 순서:
#   1. sh ./bootstrap_sources.sh
#   2. sources.yaml 을 환경에 맞게 편집
#   3. docker compose up app

# 이 스크립트가 위치한 디렉터리(= 프로젝트 루트) 를 기준으로 경로를 계산한다.
# 사용자 실행 스크립트는 프로젝트 루트에 직접 배치되어 있으므로 SCRIPT_DIR 이
# 곧 프로젝트 루트다 (00078 에서 scripts/ 분리 시 루트로 승격됨).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

TEMPLATE_PATH="$PROJECT_ROOT/sources.yaml.template"
TARGET_PATH="$PROJECT_ROOT/sources.yaml"

if [ ! -f "$TEMPLATE_PATH" ]; then
    echo "ERROR: template 파일을 찾을 수 없습니다: $TEMPLATE_PATH" >&2
    exit 1
fi

# -n 플래그: 이미 존재하는 파일은 덮어쓰지 않는다 (idempotent).
if cp -n "$TEMPLATE_PATH" "$TARGET_PATH" 2>/dev/null; then
    echo "sources.yaml 생성 완료: $TARGET_PATH"
    echo "sources.yaml 을 환경에 맞게 편집한 뒤 docker compose up app 을 실행하세요."
else
    echo "sources.yaml 이 이미 존재합니다 — 덮어쓰지 않았습니다: $TARGET_PATH"
fi
