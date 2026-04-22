# Alembic 도입 설계 노트 (Phase 0 — baseline 캡처)

> **작성 목적**: 현재 DB 초기화 흐름 전수 파악 후, 각 진입점을 Alembic 기반으로
> 재구성할 때 어떻게 바꿀지 설계를 기록한다.
> 이후 subtask(3~8)는 이 문서를 구현 참고서로 사용한다.

---

## 1. 현재 초기화 흐름 전체 지도

### 진입점별 호출 경로

```
[docker/entrypoint.sh]
  └─ exec python -m app.cli
        └─ app/cli.py: main() → _async_main() → _orchestrate()
              └─ init_db()   ← app/db/init_db.py

[uvicorn app.web.main:app]
  └─ app/web/main.py: 모듈 import 시 create_app() 즉시 실행
        └─ init_db()   ← app/db/init_db.py

[python -m app.db.init_db]  (수동/테스트용)
  └─ app/db/init_db.py: main() → init_db()
```

### `init_db()` 내부 순서 (현재)

```
init_db(engine=None)
  1. get_settings().ensure_runtime_paths()   ← SQLite 디렉터리 생성
  2. run_migrations(engine)                  ← app/db/migration.py: 6단계 DDL 수작업
  3. Base.metadata.create_all(bind=engine)   ← 아직 없는 테이블/인덱스 생성 (멱등)
  4. return engine
```

### `run_migrations()` 6단계 (app/db/migration.py)

모두 멱등(재실행 안전). **현재 운영 DB에는 전부 적용 완료된 상태**임.

| 단계 | 내용 |
|------|------|
| 1 | `iris_announcement_id` → `source_announcement_id` 컬럼 이름 변경 |
| 2 | `announcements.source_type VARCHAR(32)` 컬럼 추가 (DEFAULT 'IRIS') |
| 3 | `announcements.is_current BOOLEAN NOT NULL DEFAULT 1` 컬럼 추가 |
| 4 | `uq_announcement_source` UNIQUE 인덱스 제거 (이력 보존 전환) |
| 5 | `canonical_projects` 테이블 신규 생성 |
| 6 | `announcements.canonical_group_id / canonical_key / canonical_key_scheme` 3개 컬럼 추가 |

### 현재 스키마 최종 상태 (모든 6단계 적용 후)

#### `canonical_projects`

| 컬럼 | 타입 | 제약 |
|------|------|------|
| `id` | `INTEGER` | PK, AUTOINCREMENT |
| `canonical_key` | `VARCHAR(256)` | NOT NULL, UNIQUE |
| `key_scheme` | `VARCHAR(16)` | NOT NULL |
| `representative_title` | `TEXT` | NULL 허용 |
| `representative_agency` | `VARCHAR(255)` | NULL 허용 |
| `created_at` | `DATETIME` (timezone=True) | NOT NULL |
| `updated_at` | `DATETIME` (timezone=True) | NOT NULL |

#### `announcements`

| 컬럼 | 타입 | 제약/인덱스 |
|------|------|------------|
| `id` | `INTEGER` | PK |
| `source_announcement_id` | `VARCHAR(128)` | NOT NULL, INDEX(`ix_source_announcement_id`) |
| `source_type` | `VARCHAR(32)` | NOT NULL, DEFAULT 'IRIS' |
| `title` | `TEXT` | NOT NULL |
| `agency` | `VARCHAR(255)` | NULL 허용 |
| `status` | `VARCHAR` (Enum) | NOT NULL, INDEX |
| `received_at` | `DATETIME` (tz=True) | NULL 허용 |
| `deadline_at` | `DATETIME` (tz=True) | NULL 허용, INDEX |
| `detail_url` | `TEXT` | NULL 허용 |
| `detail_html` | `TEXT` | NULL 허용 |
| `detail_text` | `TEXT` | NULL 허용 |
| `detail_fetched_at` | `DATETIME` (tz=True) | NULL 허용 |
| `detail_fetch_status` | `VARCHAR(16)` | NULL 허용 |
| `raw_metadata` | `JSON` | NOT NULL |
| `scraped_at` | `DATETIME` (tz=True) | NOT NULL |
| `updated_at` | `DATETIME` (tz=True) | NOT NULL |
| `is_current` | `BOOLEAN` | NOT NULL, DEFAULT 1, INDEX |
| `canonical_group_id` | `INTEGER` | NULL 허용, FK → `canonical_projects.id` SET NULL, INDEX |
| `canonical_key` | `VARCHAR(256)` | NULL 허용, INDEX |
| `canonical_key_scheme` | `VARCHAR(16)` | NULL 허용 |

복합 인덱스: `ix_announcement_source (source_type, source_announcement_id)`

#### `attachments`

| 컬럼 | 타입 | 제약/인덱스 |
|------|------|------------|
| `id` | `INTEGER` | PK |
| `announcement_id` | `INTEGER` | NOT NULL, FK → `announcements.id` CASCADE, INDEX |
| `original_filename` | `VARCHAR(512)` | NOT NULL |
| `stored_path` | `TEXT` | NOT NULL |
| `file_ext` | `VARCHAR(16)` | NOT NULL |
| `file_size` | `BIGINT` | NULL 허용 |
| `download_url` | `TEXT` | NULL 허용 |
| `sha256` | `VARCHAR(64)` | NULL 허용 |
| `downloaded_at` | `DATETIME` (tz=True) | NOT NULL |

복합 인덱스: `ix_attachments_announcement_filename (announcement_id, original_filename)`

---

## 2. Alembic 도입 후 각 진입점 변경 설계

### 2-1. `alembic/` 디렉터리 (신규 — subtask 3)

```
alembic/
  alembic.ini          ← sqlalchemy.url 는 빈 값 또는 placeholder (env.py가 override)
  env.py               ← get_settings().db_url 을 동적 주입
  versions/
    0001_baseline.py   ← 현재 스키마 1:1 캡처 (subtask 4)
```

**`alembic/env.py` 핵심 패턴**:

```python
from app.config import get_settings

config.set_main_option("sqlalchemy.url", get_settings().db_url)
```

- `alembic.ini`의 `sqlalchemy.url`은 절대 하드코딩하지 않는다.
- `env.py`에서 `app.config.get_settings().db_url`로 동적 주입.
- SQLite의 경우 `connect_args={"check_same_thread": False}` 도 env.py에서 주입.

### 2-2. `app/db/init_db.py` — stamp vs upgrade 분기 (subtask 5)

**외부 API `init_db()` 시그니처 및 반환값은 변경하지 않는다.**

내부 구현만 교체. 새 흐름:

```
init_db(engine=None)
  1. get_settings().ensure_runtime_paths()
  2. alembic_upgrade_or_stamp(engine)  ← 새로 작성할 헬퍼
  3. return engine
  (Base.metadata.create_all 제거, run_migrations 호출 제거)
```

**`alembic_upgrade_or_stamp()` 로직**:

```
상황 A: alembic_version 테이블이 없고, 테이블도 하나도 없다 (완전히 새 DB)
  → alembic upgrade head  ← baseline migration으로 스키마 생성

상황 B: alembic_version 테이블이 없지만, 다른 테이블이 존재한다 (기존 운영 DB)
  → alembic stamp head    ← 스키마는 이미 최신. 리비전 레코드만 삽입. 절대 DROP/CREATE 금지.

상황 C: alembic_version 테이블이 있다 (이미 Alembic 관리 중)
  → alembic upgrade head  ← 멱등. 이미 최신이면 아무 일도 하지 않음.
```

이 세 경우 모두 멱등하게 재실행 가능해야 한다.

**구현 방식**:  
`alembic.config.Config`와 `alembic.command.upgrade` / `alembic.command.stamp`를
Python API로 직접 호출한다. 서브프로세스 호출 방식은 피한다.

```python
from alembic import command
from alembic.config import Config

def _run_alembic_upgrade(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "head")

def _run_alembic_stamp(alembic_cfg: Config) -> None:
    command.stamp(alembic_cfg, "head")
```

### 2-3. `app/db/migration.py` — run_migrations()의 역할 변화 (subtask 5)

- `run_migrations()` 함수 자체는 **Phase 0에서 삭제하지 않는다**.
- `init_db()` 내부에서 `run_migrations()` 호출만 제거한다.
- 함수 상단 docstring에 "Alembic 도입으로 이 함수는 더 이상 호출되지 않는다.
  Phase 0 이후 migration은 alembic/versions/ 로 관리한다"는 주석을 추가한다.
- 완전 삭제는 향후 Phase에서 결정한다.

**근거**: 6단계 DDL은 모두 운영 DB에 이미 반영되어 있다.
baseline migration이 그 결과 스키마를 캡처하므로, 이 함수를 다시 실행할 이유가 없다.

### 2-4. `docker/entrypoint.sh` — alembic 자동 적용 (subtask 6)

현재 entrypoint는 DB 초기화를 직접 담당하지 않는다.
`python -m app.cli` 내부에서 `init_db()`가 호출되는 구조.

변경 방향 (두 가지 옵션 중 택일):

**옵션 A**: entrypoint.sh에 `alembic upgrade head` 명령을 추가 (Shell 레벨 보장)

```sh
# sources.yaml 격리 (기존 로직) 후 ...
alembic upgrade head          # ← 추가: 매 기동 시 migration 자동 적용
exec python -m app.cli        # ← 기존
```

**옵션 B**: init_db() 내부에서만 처리 (Python 레벨 보장)

현재도 `app.cli._orchestrate()`가 `init_db()`를 호출하므로
init_db()가 Alembic upgrade를 수행하면 entrypoint.sh 수정 없이도 동작.

**권장**: 두 진입점(entrypoint.sh + init_db()) 양쪽에서 처리.
entrypoint.sh에 추가하면 웹 서버 기동 시에도 동일하게 적용 가능하며,
"기동 전 schema 보장"이 코드 진입 전에 이미 완료된다.

> 주의: entrypoint.sh에서 `alembic` 명령이 path에 있어야 한다.
> docker 이미지 빌드 시 alembic이 설치된 venv를 사용하는지 확인 필요 (subtask 6에서 검증).

### 2-5. `app/web/main.py` — 웹 기동 훅 (subtask 5)

`create_app()` → `init_db()` 호출 경로는 **변경하지 않는다**.
`init_db()` 내부 구현이 Alembic 기반으로 바뀌면 자동으로 upgrade가 적용된다.

```python
# app/web/main.py (기존, 유지)
def create_app(settings=None):
    ...
    init_db()    ← 이 줄은 그대로. 내부 구현만 변경됨.
    ...
```

### 2-6. `app/config.py` — 변경 없음

`get_settings().db_url` 은 그대로 사용.
`ensure_runtime_paths()` 의 `if url.startswith("sqlite")` 조건화 로직도 유지.

---

## 3. Baseline migration 파일 설계 (subtask 4)

파일명: `alembic/versions/0001_<hash>_baseline_initial_schema.py`

### upgrade() 함수 구현 방법

`alembic revision --autogenerate`를 베이스로 하되, 반드시 수동으로 검토·수정한다.
자동 생성 결과가 현재 ORM 모델과 정확히 일치하는지 `alembic check` 또는
`alembic upgrade head`(운영 DB 대상) 로 "변화 0" 을 확인해야 한다.

주요 고려사항:
- `Enum` 타입은 `native_enum=False`이므로 `VARCHAR`로 생성된다.
- `JSON` 타입은 SQLite에서 `JSON` (혹은 TEXT)로, Postgres에서 `JSON`으로 매핑된다.
- `CHECK` constraint, FK에 이름을 명시적으로 부여한다 (Postgres 호환).
- SQLite에서 `batch_alter_table`이 필요한 경우는 `downgrade()`에서 주로 발생한다.

### downgrade() 함수 구현

완전한 롤백을 위해 모든 테이블과 인덱스를 `DROP` 한다.

```python
def downgrade() -> None:
    op.drop_table("attachments")
    op.drop_table("announcements")
    op.drop_table("canonical_projects")
```

### baseline migration 주의사항

이 migration은 **신규 DB에만 실제 DDL이 실행**된다.
기존 운영 DB는 `alembic stamp head`로 리비전만 기록되므로
upgrade()의 DDL이 실제 실행되지 않는다 — 그래서 데이터/스키마 변화가 0이 된다.

---

## 4. pyproject.toml 의존성 추가 (subtask 3)

```toml
[tool.poetry.dependencies]
alembic = ">=2.0,<3.0"   # SQLAlchemy 2.x 계열과 호환
```

또는 `requirements.txt`가 사용된다면:
```
alembic>=2.0,<3.0
```

프로젝트의 패키지 매니저 방식을 subtask 3에서 확인 후 결정.

---

## 5. 검증 시나리오 (subtask 7에서 실행)

### 시나리오 1: 기존 운영 DB (data/db/app.sqlite3)

```
1. cp data/db/app.sqlite3 data/db/app.sqlite3.bak  (백업)
2. sha256sum data/db/app.sqlite3 > /tmp/before.hash
3. sqlite3 data/db/app.sqlite3 .schema > /tmp/before.schema
4. alembic upgrade head                             (stamp head가 내부적으로 실행되어야 함)
5. sha256sum data/db/app.sqlite3 > /tmp/after.hash
6. sqlite3 data/db/app.sqlite3 .schema > /tmp/after.schema
7. diff /tmp/before.hash /tmp/after.hash            (파일 해시 변화 0 확인)
8. diff /tmp/before.schema /tmp/after.schema        (스키마 변화 0 확인)
```

기대 결과: diff 없음. `alembic current` → `0001...baseline (head)` 표시.

### 시나리오 2: 빈 SQLite

```
DB_URL="sqlite:////tmp/test_blank.sqlite3" alembic upgrade head
sqlite3 /tmp/test_blank.sqlite3 .schema   → 3개 테이블 + 인덱스 생성 확인
```

### 시나리오 3: 임시 Postgres

```
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:15 --name pg_test
DB_URL="postgresql://postgres:test@localhost:5433/postgres" alembic upgrade head
(검증 후)
docker stop pg_test
```

`docker-compose.yml`에 postgres 서비스를 추가하지 않는다.

---

## 6. Phase 0에서 하지 않는 것 (범위 밖)

- 신규 테이블 추가 (Phase 1a)
- 기존 스키마 변경
- `run_migrations()` 완전 삭제
- `create_all` 코드 완전 삭제 (Phase 0에서는 `init_db()` 내부에서만 제거)
- Postgres 상시 운영 전환 (`docker-compose.yml` 영구 추가 금지)
- 데이터 이관 스크립트

---

## 7. 이후 Phase에서 migration 추가 시 기준

1. `alembic revision -m "설명"` 으로 새 파일 생성
2. `upgrade()` / `downgrade()` 양방향 구현 필수
3. `ALTER TABLE` 계열은 반드시 `batch_alter_table`로 감싸기 (SQLite 호환)
4. 검증은 `docs/alembic_verification.md` 절차를 따른다

---

## 8. 관련 파일 참조 맵

| 파일 | 현재 역할 | Phase 0 후 역할 |
|------|-----------|-----------------|
| `app/db/init_db.py` | `run_migrations` + `create_all` | Alembic `upgrade/stamp` 분기 (외부 API 유지) |
| `app/db/migration.py` | 수작업 DDL 6단계 | 호출 제거 (함수 보존, 주석 추가) |
| `app/db/session.py` | 엔진 싱글턴 | 변경 없음 |
| `app/db/models.py` | ORM 모델 정의 | 변경 없음 (Alembic autogenerate 소스로 사용) |
| `app/config.py` | `db_url` 제공 | 변경 없음 |
| `app/web/main.py` | `create_app()` → `init_db()` | 변경 없음 (내부 동작만 변함) |
| `app/cli.py` | `_orchestrate()` → `init_db()` | 변경 없음 (내부 동작만 변함) |
| `docker/entrypoint.sh` | sources.yaml 격리 후 `python -m app.cli` | `alembic upgrade head` 줄 추가 |
| `alembic/` | (없음) | 신규 생성 (subtask 3) |
| `alembic/versions/0001_*.py` | (없음) | baseline migration (subtask 4) |
