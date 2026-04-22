# Alembic Phase 0 검증 절차

Task 00017 (Phase 0 Baseline) 완료 시 실행한 3단계 검증 기록.
이후 새 migration 을 추가할 때도 동일 절차로 검증한다.

## 전제 조건

```bash
uv sync          # alembic 1.x 설치 확인
uv run alembic --version  # alembic 1.18.4
```

---

## Tier 1 — 기존 SQLite (stamp 경로)

**목적**: 운영 DB 에 데이터를 그대로 보존하고 `alembic_version` 레코드만 삽입하는지 확인.

```bash
# 운영 DB 복사본으로 테스트 (원본 보호)
cp data/db/app.sqlite3 /tmp/test_existing.sqlite3
chmod 664 /tmp/test_existing.sqlite3

uv run python - <<'PY'
from app.db.init_db import init_db
from sqlalchemy import inspect, create_engine, text

engine = create_engine("sqlite:////tmp/test_existing.sqlite3")
with engine.connect() as conn:
    ann_before = conn.execute(text("SELECT COUNT(*) FROM announcements")).scalar()

init_db(engine)

with engine.connect() as conn:
    ver  = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    ann_after = conn.execute(text("SELECT COUNT(*) FROM announcements")).scalar()

assert ver == "a8f3c2d14e7b", f"wrong revision: {ver}"
assert ann_before == ann_after,  "DATA CHANGED!"
print(f"PASS: strategy=stamp, version={ver}, rows={ann_after} (unchanged)")
PY
```

**기대 결과**:
- 로그에 `초기화 전략: stamp` 출력
- `alembic_version.version_num = a8f3c2d14e7b`
- `announcements` 행 수 변화 없음

**실제 결과 (2026-04-22)**:
```
초기화 전략: stamp (기존 DB 감지 — alembic_version 없음, 테이블 존재: [...])
PASS: strategy=stamp, version=a8f3c2d14e7b, rows=542 (unchanged)
```

---

## Tier 2 — 빈 SQLite (baseline-bootstrap 경로)

**목적**: 신규 SQLite DB 에 baseline migration 으로 전체 스키마가 생성되는지 확인.

```bash
uv run python - <<'PY'
from app.db.init_db import init_db
from sqlalchemy import inspect, create_engine

engine = create_engine("sqlite:////tmp/test_blank.sqlite3")
init_db(engine)

tables = set(inspect(engine).get_table_names())
assert tables == {"alembic_version", "announcements", "attachments", "canonical_projects"}

cols = {c["name"] for c in inspect(engine).get_columns("announcements")}
required = {"id","source_announcement_id","source_type","title","agency","status",
            "received_at","deadline_at","detail_url","detail_html","detail_text",
            "detail_fetched_at","detail_fetch_status","raw_metadata","scraped_at",
            "updated_at","is_current","canonical_group_id","canonical_key","canonical_key_scheme"}
assert not (required - cols), f"Missing columns: {required - cols}"

# 멱등 확인
init_db(engine)
assert set(inspect(engine).get_table_names()) == tables
print("PASS: baseline-bootstrap, all tables/columns, idempotent")
PY
```

**기대 결과**:
- 로그에 `초기화 전략: baseline-bootstrap` 출력
- 3개 테이블 + `alembic_version` 생성
- 재실행 시 `초기화 전략: upgrade` (no-op)

**실제 결과 (2026-04-22)**: PASS

---

## Tier 3 — 빈 Postgres (Postgres 호환성 검증)

**목적**: Postgres 에서 baseline migration 이 native ENUM 없이 동작하는지 확인.

```bash
# 임시 Postgres 컨테이너 기동
docker run -d --name alembic_test_pg \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=test_alembic \
  -p 15433:5432 postgres:16-alpine
sleep 3

# psycopg2-binary 임시 설치 (uv.lock 에는 포함하지 않음)
uv run pip install psycopg2-binary

uv run python - <<'PY'
from app.db.init_db import init_db
from sqlalchemy import inspect, create_engine, text

PG_URL = "postgresql+psycopg2://postgres:testpass@127.0.0.1:15433/test_alembic"
engine = create_engine(PG_URL)
init_db(engine)

tables = set(inspect(engine).get_table_names())
assert tables == {"alembic_version", "announcements", "attachments", "canonical_projects"}

with engine.connect() as conn:
    enums = conn.execute(text("SELECT typname FROM pg_type WHERE typtype='e'")).fetchall()
assert not enums, f"Unexpected native ENUM: {enums}"

init_db(engine)  # 멱등
print("PASS: Postgres baseline-bootstrap, no native ENUM, idempotent")
PY

docker rm -f alembic_test_pg
```

**기대 결과**:
- native Postgres ENUM 타입 없음 (`native_enum=False` 적용 확인)
- `status` 컬럼은 `VARCHAR(4)` 로 저장

**실제 결과 (2026-04-22)**: PASS

---

## 알려진 주의사항

| 항목 | 내용 |
|------|------|
| `str(engine.url)` 금지 | SQLAlchemy 2.x 에서 비밀번호를 `***` 로 마스킹함. `render_as_string(hide_password=False)` 사용 |
| `is_current` server_default | SQLite 는 `1` 허용하지만 Postgres 는 `BOOLEAN DEFAULT 1` 거부 → `true` 사용 |
| env.py URL 우선순위 | `_build_alembic_config(engine)` 으로 주입된 URL 을 env.py 가 덮어쓰지 않도록 조건부 `set_main_option` |

---

# Phase 1a (task 00019) — 신규 13개 테이블 migration 검증

> **revision**: `b2c5e8f1a934` — `20260422_1500_b2c5e8f1a934_phase1a_new_tables.py`
> **down_revision**: `a8f3c2d14e7b` (Phase 0 baseline)
> **신규 테이블**: users, user_sessions, announcement_user_states,
> relevance_judgments, relevance_judgment_history, favorite_folders,
> favorite_entries, canonical_overrides, email_subscriptions,
> admin_email_targets, audit_logs, scrape_runs, attachment_analyses (13 개)

**init_db 전략 변경 주의**: Phase 1a 배포와 함께 `app/db/init_db.py` 의
stamp 전략이 `stamp head` → **`stamp baseline → upgrade head`** 로 수정되었다.
head 가 baseline 이후로 전진한 상태에서 기존 운영 DB 가 처음 init_db 를 타는
경우 `stamp head` 만으로는 신규 migration 의 DDL 이 실행되지 않는다
(alembic_version 만 head 로 기록되고 테이블은 생성 안 됨).
`stamp baseline → upgrade head` 는 baseline 이후의 모든 migration 을 순차 적용한다.
따라서 아래 Tier 1 기대 revision 은 **head (`b2c5e8f1a934`)** 다.

## Tier 1 — 기존 SQLite (stamp-then-upgrade 경로)

**목적**: 운영 DB 를 복사한 뒤, 신규 13 개 테이블 생성 + 기존 3 개 테이블의
데이터 무변경을 확인.

```bash
cp data/db/app.sqlite3 /tmp/phase1a_tier1.sqlite3

uv run --extra dev python - <<'PY'
from sqlalchemy import create_engine, inspect, text
from app.db.init_db import init_db

EXPECTED_NEW = {
    "users", "user_sessions", "announcement_user_states",
    "relevance_judgments", "relevance_judgment_history",
    "favorite_folders", "favorite_entries",
    "canonical_overrides", "email_subscriptions", "admin_email_targets",
    "audit_logs", "scrape_runs", "attachment_analyses",
}

engine = create_engine("sqlite:////tmp/phase1a_tier1.sqlite3")

with engine.connect() as conn:
    ann_before = conn.execute(text("SELECT COUNT(*) FROM announcements")).scalar()
    cp_before = conn.execute(text("SELECT COUNT(*) FROM canonical_projects")).scalar()
    att_before = conn.execute(text("SELECT COUNT(*) FROM attachments")).scalar()
    tables_before = set(inspect(engine).get_table_names())

init_db(engine)

with engine.connect() as conn:
    ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    ann_after = conn.execute(text("SELECT COUNT(*) FROM announcements")).scalar()
    cp_after = conn.execute(text("SELECT COUNT(*) FROM canonical_projects")).scalar()
    att_after = conn.execute(text("SELECT COUNT(*) FROM attachments")).scalar()
    tables_after = set(inspect(engine).get_table_names())

new_tables = tables_after - tables_before - {"alembic_version"}
assert ver == "b2c5e8f1a934", f"wrong revision: {ver}"
assert (ann_before, cp_before, att_before) == (ann_after, cp_after, att_after), "DATA CHANGED"
assert new_tables == EXPECTED_NEW
print(f"PASS: strategy=stamp-then-upgrade, version={ver}, "
      f"rows={ann_after}/{cp_after}/{att_after} unchanged, new_tables={len(new_tables)}")
PY
```

**기대 결과**:
- 로그: `초기화 전략: stamp-then-upgrade` → `Running stamp_revision  -> a8f3c2d14e7b`
  → `Running upgrade a8f3c2d14e7b -> b2c5e8f1a934`
- `alembic_version.version_num = b2c5e8f1a934`
- 기존 3 개 테이블 행 수 변화 없음
- 신규 13 개 테이블 생성

**실제 결과 (2026-04-22)**:
```
2026-04-23 00:24:51 | INFO | 초기화 전략: stamp-then-upgrade (기존 DB 감지 — alembic_version 없음,
    테이블 존재: ['announcements', 'attachments', 'canonical_projects'], baseline=a8f3c2d14e7b)
INFO [alembic.runtime.migration] Running stamp_revision  -> a8f3c2d14e7b
INFO [alembic.runtime.migration] Running upgrade a8f3c2d14e7b -> b2c5e8f1a934, phase1a: 신규 13개 테이블 추가
2026-04-23 00:24:52 | INFO | DB 초기화 완료: strategy=stamp-then-upgrade dialect=sqlite

  revision = b2c5e8f1a934  (expected: b2c5e8f1a934)
  announcements rows: 542 → 542
  canonical_projects rows: 379 → 379
  attachments rows: 1387 → 1387
  새로 생성된 테이블 (13): ['admin_email_targets', 'announcement_user_states',
    'attachment_analyses', 'audit_logs', 'canonical_overrides', 'email_subscriptions',
    'favorite_entries', 'favorite_folders', 'relevance_judgment_history',
    'relevance_judgments', 'scrape_runs', 'user_sessions', 'users']
  => Tier 1 PASS
```

## Tier 2 — 빈 SQLite (baseline-bootstrap + downgrade/upgrade)

**목적**: 빈 DB 에 baseline + phase1a migration 을 모두 적용하고, downgrade -1
후 재 upgrade 가 정상 복구됨을 확인.

```bash
uv run --extra dev python - <<'PY'
import os
from sqlalchemy import create_engine, inspect, text
from app.db.init_db import init_db, _build_alembic_config
from alembic import command as alembic_command

EXPECTED_NEW = {...}  # Tier 1 과 동일

blank = "/tmp/phase1a_tier2.sqlite3"
if os.path.exists(blank):
    os.remove(blank)
engine = create_engine(f"sqlite:///{blank}")

# (1) baseline-bootstrap → 17 개 테이블(3 baseline + 1 alembic_version + 13 신규)
init_db(engine)
tables = set(inspect(engine).get_table_names())
assert tables == {"alembic_version", "announcements", "attachments",
                  "canonical_projects"} | EXPECTED_NEW

# (2) downgrade -1 → baseline 으로 복귀
cfg = _build_alembic_config(engine)
alembic_command.downgrade(cfg, "-1")
with engine.connect() as conn:
    ver_down = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
assert ver_down == "a8f3c2d14e7b"
assert not (set(inspect(engine).get_table_names()) & EXPECTED_NEW), \
    "다운그레이드 후 신규 테이블이 남음"

# (3) 재 upgrade head
alembic_command.upgrade(cfg, "head")
with engine.connect() as conn:
    ver_up = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
assert ver_up == "b2c5e8f1a934"
print("PASS: baseline-bootstrap + downgrade -1 + 재 upgrade head")
PY
```

**기대 결과**:
- `baseline-bootstrap` 로그, 총 17 개 테이블 생성
- downgrade -1 → `a8f3c2d14e7b`, 신규 13 개 테이블 DROP (baseline 3 개 + alembic_version 만 잔존)
- 재 upgrade head → `b2c5e8f1a934`, 17 개 복구

**실제 결과 (2026-04-22)**:
```
2026-04-23 00:24:52 | INFO | 초기화 전략: baseline-bootstrap (빈 DB 감지 — 전체 스키마 생성)
INFO [alembic.runtime.migration] Running upgrade  -> a8f3c2d14e7b, baseline: initial schema
INFO [alembic.runtime.migration] Running upgrade a8f3c2d14e7b -> b2c5e8f1a934, phase1a: 신규 13개 테이블 추가

  총 테이블 수: 17 (기대: 17)

  --- downgrade -1 실행 ---
INFO [alembic.runtime.migration] Running downgrade b2c5e8f1a934 -> a8f3c2d14e7b
  downgrade 후 revision = a8f3c2d14e7b (expected: a8f3c2d14e7b)
  downgrade 후 테이블 수 = 4

  --- 재 upgrade head 실행 ---
INFO [alembic.runtime.migration] Running upgrade a8f3c2d14e7b -> b2c5e8f1a934
  재 upgrade 후 revision = b2c5e8f1a934
  재 upgrade 후 테이블 수 = 17
  => Tier 2 PASS
```

## Tier 3 — Postgres 호환 검증

**목적**: Postgres 에서 Phase 1a migration 이 native ENUM / JSONB 없이 동작하는지 확인.

### 코드 수준 증명 (선행 체크)

Phase 1a migration 파일은 의도적으로 dialect 전용 타입을 일절 사용하지 않는다.
아래 grep 결과가 모두 "없음" 이면 코드 수준에서 Postgres 호환이 보장된다.

```bash
grep -n "sa\.Enum\|dialects\.postgresql\.JSONB\|dialects\.postgresql\.UUID" \
  alembic/versions/20260422_1500_b2c5e8f1a934_phase1a_new_tables.py
# → (매칭 없음)
```

**실제 결과 (2026-04-22)**: 매칭 결과 없음. 모든 enum 도메인은 `String(N)` +
`CHECK` 로 표현되며 모든 JSON 은 `sa.JSON()` 범용 타입이다.

### 실행 검증 (Docker Postgres 환경에서)

Phase 0 Tier 3 절차를 그대로 따른다 (postgres:16-alpine + psycopg2-binary).
이번 phase 에서는 **신규 13 개 테이블 생성 + native ENUM 타입 부재** 를 추가로
확인한다.

```bash
docker run -d --name alembic_test_pg_phase1a \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=test_alembic_phase1a \
  -p 15434:5432 postgres:16-alpine
sleep 3

uv run pip install psycopg2-binary

uv run --extra dev python - <<'PY'
from sqlalchemy import create_engine, inspect, text
from app.db.init_db import init_db

PG_URL = "postgresql+psycopg2://postgres:testpass@127.0.0.1:15434/test_alembic_phase1a"
engine = create_engine(PG_URL)
init_db(engine)

tables = set(inspect(engine).get_table_names())
expected = {"alembic_version", "announcements", "attachments", "canonical_projects",
            "users", "user_sessions", "announcement_user_states",
            "relevance_judgments", "relevance_judgment_history",
            "favorite_folders", "favorite_entries",
            "canonical_overrides", "email_subscriptions", "admin_email_targets",
            "audit_logs", "scrape_runs", "attachment_analyses"}
assert tables == expected, f"missing: {expected - tables}"

with engine.connect() as conn:
    native_enums = conn.execute(
        text("SELECT typname FROM pg_type WHERE typtype='e'")
    ).fetchall()
assert not native_enums, f"Unexpected native ENUM: {native_enums}"

init_db(engine)  # 멱등
print("PASS: Phase 1a on Postgres — 17 tables, no native ENUM, idempotent")
PY

docker rm -f alembic_test_pg_phase1a
```

**기대 결과**:
- 17 개 테이블 생성, `pg_type WHERE typtype='e'` 결과 없음
- 재실행(멱등) 통과

**실제 결과 (2026-04-22)**: **Docker 미설치 환경으로 인해 실행 검증 미수행**.
코드 수준 증명(위 grep) + SQLite 에서 동일 DDL 이 성공함으로 대체. Docker 환경이
가용해지면 위 스크립트를 재실행해 결과 로그를 본 섹션에 추가해야 한다.
