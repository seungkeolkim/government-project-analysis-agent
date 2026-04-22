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
