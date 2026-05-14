# Phase A-2 Part 1 설계 노트 — EmailForwardLog 테이블 신설

작성 기준: task 00106-1 (탐사 + design note), 코드 변경 없음.

---

## 1. 탐사 결과 요약

### (a) EmailForwardLog ORM 모델 배치 위치

`app/db/models.py` 에서 `EmailSendRunStatus` 클래스는 line 2345, `EmailSendRun` 클래스는 line 2361, `__all__` 선언은 line 2525 에 위치한다.

**결정**: `EmailForwardStatus` enum 클래스 → `EmailSendRunStatus` 바로 아래에 삽입. `EmailForwardLog` ORM 클래스 → `EmailSendRun` 클래스 바로 아래에 삽입. `__all__` 에 두 이름 추가.

카테고리 응집도 측면에서 이메일 발송 관련 클래스를 연속으로 배치하는 것이 자연스럽다. 파일 내 섹션 구분은 기존 클래스 순서(Announcement 계열 → 조직 계열 → 이메일 계열)를 따른다.

### (b) EmailForwardStatus enum 정의 위치

기존 enum 패턴 (탐사 결과):

```python
# 기존 패턴 — StrEnum 상속, 값은 도메인 컨벤션에 따라 영문 또는 한글
class AnnouncementStatus(StrEnum):          # 한글 값 (화면 표시용)
    ...

class AnnouncementProgressStatus(StrEnum):  # 한글 값 (화면 표시용)
    ...

class EmailSendRunStatus(StrEnum):           # 영문 값 (기술 상태)
    SENT = "sent"
    FAILED = "failed"
```

**결정**: `EmailForwardStatus` 도 동일하게 `StrEnum` 상속, 영문 값 사용 (기술 상태 enum 컨벤션). 정의 파일은 `app/db/models.py` 의 `EmailSendRunStatus` 바로 아래.

```python
class EmailForwardStatus(StrEnum):
    """포워딩 액션 전체의 결과 요약 (Phase A-2 Part 1 / task 00106).

    값은 영문 소문자 — 기술 상태 enum 컨벤션 (PROJECT_NOTES 참조).
    값:
        SUCCESS  'success' — 모든 수신자 발송 성공.
        PARTIAL  'partial' — 일부 성공·일부 실패 혼재.
        FAILED   'failed'  — 모든 수신자 발송 실패.
    """
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
```

ORM 컬럼 선언 시 `Enum(EmailForwardStatus, name="email_forward_status", values_callable=lambda enum_cls: [member.value for member in enum_cls], native_enum=False)` — `EmailSendRunStatus` 와 동일 패턴. `docs/db_portability.md §3` 준수.

### (c) relationship() 채택 여부

탐사 결과, `EmailSendRun` 은 단방향 relationship 1개를 둔다:

```python
requested_by: Mapped[User | None] = relationship(
    "User",
    foreign_keys=[requested_by_user_id],
    lazy="select",
)
```

`back_populates` 없음, `User` 쪽에 collection 미추가, 단방향 lazy="select" 패턴이다.

**결정**: `EmailForwardLog` 도 동일하게 단방향 relationship 을 채택한다. `EmailSendRun` 과의 일관성을 최우선으로 하며, `User` / `CanonicalProject` / `Organization` 쪽에 collection 을 만들 명확한 활용 시나리오가 아직 없다.

관계 선언 예정:
```python
canonical_project: Mapped[CanonicalProject] = relationship(
    "CanonicalProject",
    foreign_keys=[canonical_project_id],
    lazy="select",
)
sender_user: Mapped[User | None] = relationship(
    "User",
    foreign_keys=[sender_user_id],
    lazy="select",
)
sender_organization: Mapped[Organization | None] = relationship(
    "Organization",
    foreign_keys=[sender_organization_id],
    lazy="select",
)
```

### (d) Alembic 신규 migration down_revision 값

탐사 결과:

| 파일명 | revision | down_revision |
|---|---|---|
| `20260422_1010_a8f3c2d14e7b_...py` | `a8f3c2d14e7b` | `None` |
| `20260422_1500_b2c5e8f1a934_...py` | `b2c5e8f1a934` | `a8f3c2d14e7b` |
| `20260424_0900_c4a8d1e7b2f3_...py` | `c4a8d1e7b2f3` | `b2c5e8f1a934` |
| `20260428_1700_d3f9a2b6c814_...py` | `d3f9a2b6c814` | `c4a8d1e7b2f3` |
| `20260504_0135_e5b8f2a9c471_...py` | `e5b8f2a9c471` | `d3f9a2b6c814` |
| `20260507_0730_f6c9a3b8d572_...py` | `f6c9a3b8d572` | `e5b8f2a9c471` |
| `20260508_0500_a9c1b2d3e4f5_...py` | `a9c1b2d3e4f5` | `f6c9a3b8d572` |
| `20260508_0700_c2d3e4f5a6b7_...py` | `c2d3e4f5a6b7` | `a9c1b2d3e4f5` |
| `20260508_0900_b8d7e2c45f01_...py` | `b8d7e2c45f01` | `c2d3e4f5a6b7` |
| `20260513_0915_e7f8b9a3c456_...py` | `e7f8b9a3c456` | `b8d7e2c45f01` |

**현재 head revision: `e7f8b9a3c456`** (파일명 `20260513_0915_e7f8b9a3c456_email_send_runs.py`)

**신규 migration 의 `down_revision = "e7f8b9a3c456"`** 으로 지정한다.

파일명 컨벤션: 타임스탬프 기반 (`YYYYMMDD_HHMM_<8자리 hex>_<slug>.py`). 신규 파일명 예: `20260514_NNNN_<hash>_email_forward_logs.py`.

### (e) 복합 인덱스 (canonical_project_id, created_at) 의 op.create_index 표현

기존 migration `20260513_0915_e7f8b9a3c456_email_send_runs.py` 의 결정을 그대로 따른다:

> "모두 ascending 으로 생성한다. ORDER BY created_at DESC 는 조회 SQL 에서 명시한다 — SQLite 의 expression index 호환 우려를 피하기 위함."

**결정: sort order 명시하지 않는다 (기본 ASC).** Postgres 는 DESC expression index 를 지원하지만 SQLite 는 ASC 인덱스 역방향 스캔으로 ORDER BY ... DESC 를 충분히 처리한다. 실제 쿼리에서 `ORDER BY created_at DESC` 를 명시하면 양쪽 DB 모두 인덱스를 효과적으로 활용할 수 있다.

```python
op.create_index(
    "ix_email_forward_logs_canonical_project_id_created_at",
    "email_forward_logs",
    ["canonical_project_id", "created_at"],
    # sort order 명시 없음 — SQLite/Postgres 공통 ASC. 조회 SQL 에서 DESC 명시.
)
```

### (f) JSON 컬럼 import path

`app/db/models.py` 파일 상단에 이미 `from sqlalchemy import JSON` 이 있다 (line 41). 기존 `Announcement.raw_metadata` 도 `JSON` 범용 타입을 사용한다.

**결정**: `sqlalchemy.JSON` (범용 타입) 을 사용한다. `sqlalchemy.dialects.postgresql.JSONB` 는 SQLite 미지원이므로 금지 (`docs/db_portability.md §1`).

ORM 컬럼 선언:
```python
recipient_addresses: Mapped[list] = mapped_column(
    JSON,
    nullable=False,
    doc="수신자 이메일 주소 목록 (list of str). 빈 리스트는 DB 차원에서 허용, app-level 검증은 Part 2.",
)
```

### (g) 테스트 fixture 사용 계획

`tests/conftest.py` 에는 다음 세 fixture 가 정의되어 있다:

- `_test_db_url`: tmp_path 기반 고유 SQLite 파일 URL 환경변수 주입
- `test_engine`: lru_cache 정리 + `init_db()` 실행 (baseline → head migration 전체 적용)
- `db_session`: `SessionLocal()` 로 ORM 세션 제공, 테스트 종료 시 close

**계획**: `tests/email/test_email_forward_log_model.py` 는 `db_session` fixture 를 직접 사용한다. 별도 `tests/email/conftest.py` 는 만들지 않는다.

FK 의존 모델(User, CanonicalProject, Organization) 은 각 테스트 함수 내에서 최소 필드만 채워 INSERT 한다. 기존 `test_sender_retry.py` 패턴과 마찬가지로 전용 fake 클래스 없이 ORM 모델 직접 사용.

---

## 2. 최종 구현 설계

### 2.1 EmailForwardLog 테이블 스키마

```
테이블명: email_forward_logs

컬럼:
  id                    Integer PK autoincrement
  canonical_project_id  Integer FK → canonical_projects.id NOT NULL CASCADE INDEX
  sender_user_id        Integer FK → users.id nullable SET NULL INDEX
  sender_organization_id Integer FK → organizations.id nullable SET NULL
  subject               String(200) NOT NULL
  has_additional_message Boolean NOT NULL default False
  recipient_addresses   JSON NOT NULL
  recipient_count       Integer NOT NULL
  status                Enum('success','partial','failed') NOT NULL native_enum=False length=20
  success_count         Integer NOT NULL default 0
  failure_count         Integer NOT NULL default 0
  created_at            DateTime(timezone=True) NOT NULL
  completed_at          DateTime(timezone=True) nullable

인덱스:
  ix_email_forward_logs_canonical_project_id          (canonical_project_id) — 컬럼 정의에서 index=True
  ix_email_forward_logs_sender_user_id                (sender_user_id) — 컬럼 정의에서 index=True
  ix_email_forward_logs_canonical_project_id_created_at (canonical_project_id, created_at) — op.create_index 별도
```

### 2.2 now_utc() import

```python
from app.timezone import now_utc
```

`created_at` 컬럼의 Python default 는 `default=now_utc` 로 지정한다. models.py 내 로컬 `_utcnow` 와 동치이나, 공용 모듈 `app.timezone.now_utc` 를 신규 모델에서 사용하는 것이 모듈 계층상 더 명확하다.

> 단, `app/db/models.py` 가 `_utcnow` 를 이미 쓰는 기존 코드를 건드리지 않는다 — `EmailForwardLog` 에서만 `now_utc` 를 사용하면 된다.

### 2.3 Alembic migration 구조

```python
revision = "<신규 hash>"
down_revision = "e7f8b9a3c456"

def upgrade():
    op.create_table("email_forward_logs", ...)
    op.create_index("ix_email_forward_logs_canonical_project_id_created_at", ...)

def downgrade():
    op.drop_index("ix_email_forward_logs_canonical_project_id_created_at", ...)
    op.drop_table("email_forward_logs")
```

단일 컬럼 인덱스 2개 (`canonical_project_id`, `sender_user_id`) 는 `op.create_table` 의 컬럼 정의에 `index=True` 로 포함하거나 별도 `op.create_index` 로 추가한다. 기존 `email_send_runs` migration 이 별도 `op.create_index` 를 사용한 패턴을 따라 명시적으로 분리하는 쪽을 채택한다 (이름 명시 강제 — `docs/db_portability.md §4` 체크리스트).

신규 테이블 추가만이므로 `batch_alter_table` 불필요 (`docs/db_portability.md §4` — ALTER 없는 CREATE TABLE 은 SQLite/Postgres 양쪽에서 직접 동작).

---

## 3. db_portability.md 인용 준수 사항

### §3 (Enum native_enum=False)

`docs/db_portability.md §3` 는 enum 컬럼에 `native_enum=False` 를 명시하도록 요구한다. `EmailForwardStatus` 컬럼은 아래와 같이 선언한다:

```python
status: Mapped[EmailForwardStatus] = mapped_column(
    Enum(
        EmailForwardStatus,
        name="email_forward_status",
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
        native_enum=False,
    ),
    nullable=False,
)
```

Postgres ENUM 타입을 생성하지 않고 CHECK constraint 만 추가 — migration 에서 명시적 `sa.CheckConstraint` 로 강제한다 (`EmailSendRun` 과 동일 패턴).

### §4 (Migration 3단계 검증 절차)

신규 migration 적용 전/후 아래 3단계 검증을 거친다:

1. **기존 운영 DB (Alembic 최신 head) 에 신규 migration 적용**
   - `alembic upgrade head` 에러 없이 완료 확인
   - `email_forward_logs` 테이블 및 인덱스 3개 생성 확인

2. **빈 SQLite 에 baseline-bootstrap 재현**
   - 새 DB 파일에서 `alembic upgrade head` 실행 → baseline 부터 신규 migration 까지 순차 적용 성공 확인
   - 단위 테스트 `test_engine` fixture 가 이 절차를 자동 수행 (`init_db` → `alembic upgrade head`)

3. **Postgres syntax 호환성 정적 검토**
   - `JSON` 범용 타입 사용 확인 (`JSONB` 금지)
   - `DateTime(timezone=True)` 사용 확인 (naive datetime 금지)
   - 모든 FK / INDEX / CHECK constraint 이름 명시 확인
   - `batch_alter_table` 불필요 확인 (신규 테이블 생성만, ALTER 없음)

---

## 4. 사용자 결정 필요 사항

없음. 첨부 문서 `phase_a2_part1_table_prompt.md` 가 모든 설계 결정을 사전 확정했다. 탐사 결과 기존 코드와의 일관성 판단이 필요한 항목도 모두 위 §1 에서 결론을 냈다.
