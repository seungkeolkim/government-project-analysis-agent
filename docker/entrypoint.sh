#!/bin/sh
# 컨테이너 엔트리포인트.
#
# 두 단계로 동작한다(task 00155 — system cron 전환).
#
#   Phase A) root 권한 초기화 (컨테이너가 root 로 기동된 app 서비스만 해당)
#     - system cron 데몬은 root 로만 띄울 수 있다. cron 데몬을 먼저 기동한 뒤,
#       gosu 로 HOST_UID:HOST_GID 비루트로 권한을 강등해 **자기 자신을 재실행**한다.
#     - 그래야 이후 마이그레이션·crontab 설치·웹 서버가 모두 HOST_UID 로 수행되어
#       생성 파일 owner 가 호스트 유저로 유지된다(00134 등 파일 owner 컨벤션).
#     - cron 잡(공고 수집)이 docker.sock 으로 scraper 를 띄우려면 실행 유저가 docker
#       그룹에 속해야 한다. cron 은 initgroups(/etc/group 기준)로 보조 그룹을 적용하고
#       gosu 강등도 /etc/group 기준이므로, 여기서 HOST_DOCKER_GID 그룹을 만들고 실행
#       유저를 가입시킨다(compose 의 group_add 만으로는 gosu 강등 후 유실됨).
#     - scraper 서비스는 docker-compose.yml 의 `user:` 지시어로 처음부터 비루트(HOST_UID)
#       로 기동되므로 이 Phase A 에 진입하지 않는다 → cron 데몬을 띄우지 않는다.
#
#   Phase B) 비루트(HOST_UID) 실행
#     - sources.yaml 을 per-run 임시 복사본으로 격리하여 동시 실행 시 경합을 방지한다.
#       SOURCES_CONFIG_PATH 환경변수를 복사본 경로로 설정한 뒤 CMD/인자를 exec 한다.
#     - alembic 마이그레이션을 적용한다.
#     - ENABLE_CRON=1 이면 DB 의 스케줄 설정을 읽어 현재 유저 crontab 에 설치한다.
#       (cron 데몬은 Phase A 에서 이미 떠 있으며, 설치된 crontab 을 폴링으로 집어든다.)
#
# sources.yaml 우선순위:
#   1. compose 바인드 마운트 (/run/config/sources.yaml) — 정상 경로
#   2. 이미지 내 template (/app/sources.yaml.template) — 마운트 누락 시 폴백
#      (호스트에서 sources.yaml 을 생성하지 않은 경우. 기본값으로 동작하며
#       실제 설정 반영을 위해 ./bootstrap_sources.sh 실행 후 재기동을 권장한다)

set -eu

# ─────────────────────────────────────────────────────────────────────────────
# Phase A — root 권한 초기화 + 비루트 강등 (root 로 기동된 경우에만).
# ─────────────────────────────────────────────────────────────────────────────
if [ "$(id -u)" = "0" ]; then
    _run_uid="${HOST_UID:-1000}"
    _run_gid="${HOST_GID:-1000}"

    # ENABLE_CRON=1 (app 서비스) 일 때만 cron 데몬을 띄우고 docker 그룹 멤버십을
    # /etc/group 에 반영한다. 그 외(예: 단발 명령)에는 권한 강등만 수행한다.
    if [ "${ENABLE_CRON:-0}" = "1" ]; then
        _docker_gid="${HOST_DOCKER_GID:-999}"
        # HOST_UID 에 대응하는 /etc/passwd 유저명(보통 appuser). 빌드 시 주입한 HOST_UID
        # 와 런타임 UID 가 다르면 비어 있을 수 있다(아래 경고 참조).
        _run_user="$(id -nu "$_run_uid" 2>/dev/null || true)"

        # HOST_DOCKER_GID 에 해당하는 그룹이 없으면 만든다(이름 hostdocker).
        if ! getent group "$_docker_gid" >/dev/null 2>&1; then
            groupadd -g "$_docker_gid" hostdocker 2>/dev/null || true
        fi
        _docker_group_name="$(getent group "$_docker_gid" | cut -d: -f1)"

        # 실행 유저를 docker 그룹에 가입시킨다(cron initgroups + gosu 강등 양쪽에서
        # docker.sock 접근 권한이 유지되도록).
        if [ -n "$_run_user" ] && [ -n "$_docker_group_name" ]; then
            usermod -aG "$_docker_group_name" "$_run_user" 2>/dev/null || true
        else
            printf '[entrypoint] WARNING: HOST_UID(%s) 유저명을 찾지 못해 docker 그룹 가입을 건너뜁니다.\n' "$_run_uid" >&2
            printf '[entrypoint] HOST_UID 변경 후 docker compose build 를 실행했는지 확인하세요.\n' >&2
        fi

        # cron 데몬 기동(root 필요). Debian cron 은 기본적으로 백그라운드로 데몬화한다.
        # 잡은 crontab 소유자(=Phase B 에서 설치할 HOST_UID)로 setuid 실행된다.
        # cron 기동 실패는 비치명적으로 처리해 웹 기동은 계속되게 한다.
        printf '[entrypoint] cron 데몬 기동\n' >&2
        cron || printf '[entrypoint] WARNING: cron 데몬 기동 실패 — 스케줄 작업이 동작하지 않을 수 있습니다.\n' >&2
    fi

    # HOST_UID:HOST_GID 로 권한을 강등해 자기 자신을 재실행한다(이후는 Phase B).
    # gosu 는 현재 환경변수를 그대로 보존하므로 ENABLE_CRON/HOST_PROJECT_DIR 등이
    # Phase B 로 전달된다.
    printf '[entrypoint] 권한 강등: uid=%s gid=%s 로 재실행\n' "$_run_uid" "$_run_gid" >&2
    exec gosu "${_run_uid}:${_run_gid}" "$0" "$@"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase B — 비루트(HOST_UID) 실행.
# ─────────────────────────────────────────────────────────────────────────────

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
    echo "[entrypoint] 설정 반영은: sh ./bootstrap_sources.sh 실행 후 컨테이너 재기동" >&2
    umask 077
    _tmpdir="$(mktemp -d)"
    trap 'rm -rf "$_tmpdir"' EXIT INT TERM
    cp "$SOURCES_YAML_TEMPLATE" "$_tmpdir/sources.yaml"
    export SOURCES_CONFIG_PATH="$_tmpdir/sources.yaml"
fi

# alembic 명령을 직접 실행할 때는 migration 자동 적용을 건너뛴다
# (예: docker compose run ... alembic upgrade head — 재귀 방지). 이 단발 명령
# 경로에서는 crontab (재)설치도 하지 않는다(웹 정상 기동 경로에서만 설치).
if [ "${1:-}" != "alembic" ]; then
    alembic upgrade head || exit 1

    # ENABLE_CRON=1 (app 서비스) 일 때만, 마이그레이션 직후 DB 의 스케줄 설정을
    # 읽어 현재 유저(HOST_UID) crontab 에 설치한다. cron 데몬은 Phase A 에서 이미
    # 떠 있으며 설치된 crontab 을 폴링으로 집어든다. crontab 설치 실패는 비치명적
    # 으로 처리해 웹 서버 기동은 계속되게 한다.
    if [ "${ENABLE_CRON:-0}" = "1" ]; then
        printf '[entrypoint] crontab 설치(스케줄 설정 → cron)\n' >&2
        python -m app.scheduler.crontab_installer \
            || printf '[entrypoint] WARNING: crontab 설치 실패 — 웹은 계속 기동합니다.\n' >&2
    fi
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
else
    exec python -m app.cli
fi
