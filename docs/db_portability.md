# SQLite ↔ Postgres 이식성 원칙 (DB Portability)

> **적용 범위**: Phase 1a 이후 모든 Alembic migration 파일 및 ORM 모델 코드.
> 이 원칙을 위반하면 SQLite 환경에서는 동작하지만 Postgres 전환 시 즉시 깨진다.

---

## 1. ORM 타입

### JSON

- **허용**: `sqlalchemy.JSON` (범용 타입)
- **금지**: `sqlalchemy.dialects.postgresql.JSONB`

SQLite는 JSONB를 지원하지 않는다. `JSON` 범용 타입을 사용하면 SQLite에서는
TEXT로, Postgres에서는 JSON으로 자동 매핑된다.

```python
# 올바른 예 (현재 models.py가 이미 이 방식 사용)
from sqlalchemy import JSON
raw_metadata: Mapped[dict] = mapped_column(JSON, nullable=False)

# 금지
from sqlalchemy.dialects.postgresql import JSONB
raw_metadata: Mapped[dict] = mapped_column(JSONB)  # SQLite에서 깨짐
```

### 날짜/시간

- **허용**: `DateTime(timezone=True)` — timezone-aware UTC로 저장
- **금지**: naive datetime 저장 (`DateTime(timezone=False)` 또는 timezone 미지정)

Postgres는 `TIMESTAMPTZ`와 `TIMESTAMP`를 구분한다. naive datetime을 저장하면
Postgres에서 타임존 변환이 예측 불가능해진다.

```python
# 올바른 예 (현재 models.py가 이미 이 방식 사용)
from sqlalchemy import DateTime
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

# 금지
created_at: Mapped[datetime] = mapped_column(DateTime)  # timezone=False 기본값
```

Python 코드에서도 `datetime.now(tz=UTC)` 또는 `datetime.now(timezone.utc)`만 사용한다.
`datetime.utcnow()` (naive) 는 금지.

### 문자열

- 장문(제한 없음): `Text`
- 길이 제한이 있는 경우: `String(N)` — N을 반드시 명시
- 임의 생략(`String()`) 금지 — Postgres에서는 `TEXT`가 되어 의미가 달라질 수 있음

```python
title: Mapped[str] = mapped_column(Text, nullable=False)          # 장문
source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # 짧은 코드값
```

### UUID

- **허용**: `String(36)` — 하이픈 포함 UUID 문자열로 저장
- **금지**: `sqlalchemy.dialects.postgresql.UUID`

UUID가 필요하면 Python `uuid.uuid4()` 로 생성 후 `str()` 변환하여 저장한다.

---

## 2. 쿼리 스타일

### 대소문자 무시 검색

- **허용**: `func.lower(column).like(f"%{term.lower()}%")`
- **금지**: Postgres의 `ILIKE`, SQLite의 `COLLATE NOCASE`

```python
# 올바른 예
from sqlalchemy import func
stmt = select(Announcement).where(func.lower(Announcement.title).like(f"%{keyword.lower()}%"))

# 금지
stmt = select(Announcement).where(Announcement.title.ilike(f"%{keyword}%"))  # Postgres 전용
```

### 집계 함수

- **금지**: `GROUP_CONCAT` (SQLite), `STRING_AGG` (Postgres) 등 dialect 전용 집계 함수를
  `text()` 또는 `func.*`로 직접 호출하는 것
- **대안**: Python 레벨에서 처리. 집계가 필요한 경우 ORM으로 목록을 가져와
  Python에서 `", ".join(items)` 등으로 처리한다.

```python
# 금지
stmt = select(func.group_concat(Attachment.file_ext))  # SQLite 전용

# 허용
attachments = session.scalars(select(Attachment).where(...)).all()
ext_list = ", ".join(a.file_ext for a in attachments)  # Python에서 처리
```

### 트랜잭션 격리

- 기본 isolation level(READ COMMITTED)에서 동작하도록 작성한다.
- "이 트랜잭션 안에서는 SERIALIZABLE이 보장된다"는 식의 가정 금지.
- SQLite의 WAL 모드나 Postgres의 특정 격리 수준에만 의존하는 로직 금지.

---

## 3. 연결 문자열

### DB_URL 환경변수 단일 경로

- 접속 문자열은 반드시 `DB_URL` 환경변수를 통해 주입한다.
- `app.config.get_settings().db_url`이 유일한 접속 경로다.
- 코드 어디에도 `"sqlite:///..."` 같은 스키마를 하드코딩하지 않는다.

```python
# 금지
engine = create_engine("sqlite:///data/db/app.sqlite3")

# 올바른 예
from app.config import get_settings
engine = create_engine(get_settings().db_url)
```

### SQLite 전용 경로 설정 격리

SQLite에만 필요한 처리(파일 디렉터리 생성, `check_same_thread=False` 등)는
`app/config.py`의 `ensure_runtime_paths()`와 `app/db/session.py`의 `_build_engine()` 
**두 곳에서만** 수행한다.

```python
# app/db/session.py — 올바른 격리 패턴
if settings.db_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False
```

다른 모듈에서 `db_url.startswith("sqlite")` 체크를 추가하지 않는다.

### Alembic env.py

`alembic/env.py`에서도 `get_settings().db_url`로 동적 주입한다.
`alembic.ini`의 `sqlalchemy.url`을 직접 채우지 않는다.

---

## 4. Alembic Migration

### ALTER TABLE — batch_alter_table 필수

SQLite는 `ALTER TABLE DROP COLUMN`, `ALTER TABLE RENAME COLUMN` 등 대부분의
ALTER 문을 지원하지 않는다. 모든 DDL 변경은 `batch_alter_table`로 감싼다.

```python
# 올바른 예
def upgrade() -> None:
    with op.batch_alter_table("announcements") as batch_op:
        batch_op.drop_column("old_column")
        batch_op.add_column(sa.Column("new_column", sa.String(64), nullable=True))

# 금지
def upgrade() -> None:
    op.drop_column("announcements", "old_column")  # SQLite에서 실패
```

### Constraint 이름 명시

CHECK constraint, UNIQUE constraint, FOREIGN KEY에는 반드시 이름을 부여한다.
이름이 없으면 Postgres에서 자동 생성된 이름이 SQLite와 달라
`batch_alter_table` 사용 시 DROP이 불가능해진다.

```python
# 올바른 예
sa.UniqueConstraint("source_type", "source_id", name="uq_announcements_source"),
sa.ForeignKeyConstraint(["canonical_group_id"], ["canonical_projects.id"],
                        name="fk_announcements_canonical_group", ondelete="SET NULL"),
sa.CheckConstraint("status IN ('접수중','접수예정','마감')", name="ck_announcements_status"),

# 금지 (이름 없음)
sa.UniqueConstraint("source_type", "source_id"),
```

### upgrade() / downgrade() 양방향 구현

모든 migration은 `upgrade()`와 `downgrade()`를 함께 구현한다.
`downgrade()`를 `pass`로 두거나 `raise NotImplementedError`로 처리하면
검증 롤백 테스트가 불가능해진다.

```python
def upgrade() -> None:
    op.add_column("announcements", sa.Column("new_field", sa.String(64), nullable=True))

def downgrade() -> None:
    with op.batch_alter_table("announcements") as batch_op:
        batch_op.drop_column("new_field")
```

### Migration 실행 순서

새 migration을 추가할 때는 `docs/alembic_verification.md`의 검증 절차를 따른다.

---

## 5. 동시성

### commit 후 재조회 패턴

"방금 `session.add(obj); session.commit()` 했으니 바로 조회하면 반영돼 있다"는
가정을 하지 않는다. commit 직후 같은 세션에서 읽으면 stale 캐시가 반환될 수 있다.

최신 상태가 필요하면 commit 후 **새 세션**에서 명시적으로 재조회한다.

```python
# 올바른 예
with session_scope() as session:
    session.add(new_obj)
# 세션이 닫혔으므로 아래는 새 쿼리를 발행한다
with session_scope() as session:
    fresh = session.get(MyModel, new_obj.id)  # 최신 상태 보장

# 주의 — commit 후 같은 세션의 캐시 조회
with session_scope() as session:
    session.add(new_obj)
    session.flush()
    same_obj = session.get(MyModel, new_obj.id)  # 캐시 히트 가능, DB 상태와 다를 수 있음
```

### 낙관적 잠금 / SELECT FOR UPDATE

Postgres에서 `SELECT FOR UPDATE`가 필요한 로직을 작성할 때,
SQLite에서는 FOR UPDATE가 무시(또는 에러)될 수 있음을 인식하고
애플리케이션 레벨 직렬화(단일 스레드 쓰기 보장 등)로 보완한다.

---

## 체크리스트

Phase 1a 이후 모든 migration 파일 및 ORM 코드 PR에서 머지 전 아래를 self-review한다.

- [ ] JSON 컬럼에 `JSONB` 대신 `JSON` 범용 타입을 사용했는가?
- [ ] 모든 datetime 컬럼이 `DateTime(timezone=True)`인가? naive datetime이 없는가?
- [ ] `String` 컬럼에 길이 `N`을 명시했는가?
- [ ] 대소문자 무시 검색에 `ILIKE`/`COLLATE NOCASE` 대신 `func.lower()` + `LIKE`를 사용했는가?
- [ ] dialect 전용 집계 함수(`GROUP_CONCAT`, `STRING_AGG`)를 직접 호출하지 않는가?
- [ ] DB_URL을 코드에 하드코딩하지 않고 `get_settings().db_url`을 사용하는가?
- [ ] `ALTER TABLE` DDL을 `batch_alter_table`로 감쌌는가?
- [ ] 모든 constraint에 이름이 있는가?
- [ ] `downgrade()`가 구현되어 있는가?
- [ ] commit 후 재조회가 필요한 경우 새 세션에서 처리하는가?

> 이 원칙은 Phase 1a 이후 모든 migration과 ORM 코드 PR의 self-review 체크리스트로 사용한다.

---

## 부록: Phase 1a 자체 점검 체크리스트

> Phase 1a (task 00019) 로 13 개 신규 테이블·ORM 모델·변경 감지 확장·사용자
> 라벨링 리셋 로직이 추가되었다. 위 6절 체크리스트에 더해 **이번 phase 특유의
> 항목**을 별도로 확인한다. 이후 유사 규모의 DB 레이어 확장 PR 에서도
> 참고 체크리스트로 재사용한다.

### JSON 컬럼 확장 여지 문서화

Phase 1a 의 JSON 컬럼은 모두 `sa.JSON()` 범용 타입으로 선언되었다. 값 구조가
기능 phase 에 따라 진화할 예정이므로 **각 컬럼의 예상 키 / 구조** 를
설계 문서에 명시해 두어야 한다.

- [ ] `EmailSubscription.filter_config` 예상 키 — docs/schema_phase1a.md §7.1
- [ ] `CanonicalOverride.source_ids` merge/split 구조 — docs/schema_phase1a.md §7.2
- [ ] `AuditLog.payload` action 별 예시 — docs/schema_phase1a.md §7.3
- [ ] `AttachmentAnalysis.structured_metadata` 예상 키 — docs/schema_phase1a.md §7.4

### FavoriteFolder depth 2 제약 (ORM validator)

사용자 원문 규약: **"폴더 depth 2 는 ORM validator"** — DB CHECK 로 강제하지 않는다.

- [ ] `favorite_folders` 에 `depth` 를 제한하는 DB CHECK 가 *없는지* 확인
- [ ] `FavoriteFolder` 에 `before_insert` / `before_update` event listener 가
      등록되어 있는지 확인 (session 이 없는 `@validates` 는 부모 조회 불가)
- [ ] 유닛 테스트에서 손자 생성·자기참조·없는 부모·UPDATE 경로의 depth 2 초과
      케이스가 모두 ValueError 로 거부되는지 확인

### FK ondelete 규칙

Phase 1a 의 FK 삭제 정책은 docs/schema_phase1a.md §3 에 요약되어 있다. migration
파일과 ORM 모델이 같은 규칙을 따르는지 점검한다.

- [ ] `users → user_sessions / announcement_user_states / relevance_judgments /
      relevance_judgment_history / favorite_folders / email_subscriptions` = **CASCADE**
- [ ] `users → canonical_overrides.decided_by` = **RESTRICT** (감사 이력 보존)
- [ ] `users → audit_logs.actor_user_id` = **SET NULL** (nullable, 탈퇴 후 로그 유지)
- [ ] `announcements → announcement_user_states` = **CASCADE**
- [ ] `canonical_projects → relevance_judgments / relevance_judgment_history /
      favorite_entries` = **CASCADE**
- [ ] `attachments → attachment_analyses` = **CASCADE** (1:1, UNIQUE)
- [ ] `favorite_folders.parent_id → favorite_folders.id` = **SET NULL** (self-FK)

### Migration 양방향 실행

- [ ] `alembic upgrade head` 로 baseline → phase1a 까지 순차 적용됨을 확인
- [ ] `alembic downgrade -1` 로 신규 13 개 테이블이 모두 DROP 되고
      alembic_version 이 `a8f3c2d14e7b` (baseline) 로 되돌아감을 확인
- [ ] 다시 `alembic upgrade head` 를 실행해 멱등성 + 재현 가능성 확인
- [ ] 세부 실행 로그는 docs/alembic_verification.md 의 Phase 1a 섹션 참조

### 2차 변경 감지 + 리셋 유닛 테스트

사용자 라벨링이 "유일한 돌이킬 수 없는 자산" 이므로 리셋·이관 로직의 정확성과
트랜잭션 atomic 경계를 테스트로 반드시 증명한다.

- [ ] `tests/db/test_change_detection.py` — 변경 감지 4-branch + 2차 감지 + 리셋
      6 테스트 PASS
- [ ] `tests/db/test_atomic_rollback.py` — 리셋 중 예외 시 UPSERT 도 함께 롤백
      1 테스트 PASS
- [ ] 실행: `uv run --extra dev pytest tests/db -v` (또는 Docker:
      `docker compose run --rm scraper pytest tests/db -v`)
- [ ] 1차 action 이 `created` / `new_version` 인 경로에서는 2차 감지가
      **발동하지 않음** (app/cli.py 의 `upsert_action` gating) — row 중복 방지

### init_db stamp 전략

- [ ] `init_db()` 가 "alembic_version 없음 + 기타 테이블 존재" 경로에서
      **baseline 으로 stamp → head 까지 upgrade** 하는지 확인
      (단순 `stamp head` 는 head 가 baseline 이후로 전진한 시점에 신규 DDL 을
       통째로 건너뛰는 rollout 버그를 일으킨다)
- [ ] 로그에 `초기화 전략: stamp-then-upgrade` 가 출력됨을 확인
