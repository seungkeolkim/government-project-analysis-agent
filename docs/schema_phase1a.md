# Phase 1a — 신규 13개 테이블 스키마 설계

> **작성 범위**: Task 00019 (Phase 1a) — 로컬 웹의 "개인용 → 팀 공용" 확장을
> 위한 신규 DB 레이어 전체 설계. **UI·인증 흐름·관리자 기능은 본 문서의 범위가
> 아님** (각각 Phase 1b / 2 / 3a / 3b / 4 / 5).
> 이 문서는 Phase 1a의 Alembic migration subtask가 그대로 참조하는 구현 근거다.

> **준수 원칙**: `docs/db_portability.md` 전 항목. 모든 JSON 컬럼은 범용 `sa.JSON`,
> 모든 시간 컬럼은 `DateTime(timezone=True)`, 모든 constraint에 이름 부여,
> `native_enum=False`, 문자열 enum 값은 코드에서 `StrEnum`으로 관리.

---

## 0. 전제 및 명명 규칙

- **baseline 상태**: `alembic/versions/20260422_1010_a8f3c2d14e7b_baseline_initial_schema.py`
  에 의해 `canonical_projects / announcements / attachments` 3개 테이블이 존재한다.
  본 문서의 13개 테이블은 이 baseline 위에 **추가**된다 (기존 3개 테이블은 변경 없음).
- **Constraint 이름 prefix 규칙** (Postgres 호환을 위해 필수):
  - PRIMARY KEY: `pk_{table}` — Alembic `create_table` 에서는 자동 생성되는 경우가 많아
    복합 PK일 때만 명시적 선언을 권장. 단일 컬럼 PK는 Alembic 기본 명명(`{table}_pkey`)
    을 허용하지만, 복합 PK는 반드시 `pk_{table}` 형태로 이름을 준다.
  - UNIQUE: `uq_{table}_{컬럼목록}`
  - FOREIGN KEY: `fk_{table}_{참조컬럼}` — `ondelete` 정책은 각 테이블 섹션에 명시
  - CHECK: `ck_{table}_{의미}`
  - INDEX: `ix_{table}_{컬럼목록}`
- **JSON 타입**: `sa.JSON()`만 사용. `dialects.postgresql.JSONB` 금지.
- **시간 컬럼**: 전부 `sa.DateTime(timezone=True)`. Python 기본값은 `_utcnow()`
  (= `datetime.now(tz=UTC)`).
- **문자열 enum**: `String(N)` + `StrEnum` + 문서의 "허용값" 목록. DB CHECK 제약은
  도메인이 매우 좁고 고정된 경우에만(예: `verdict`, `action`, `status`).
- **`FavoriteFolder.depth`는 DB CHECK가 아니라 ORM validator로 강제한다**
  (§6의 depth 2 제약 참조). SQLite에서 재귀 CHECK는 이식성이 나쁘다.
- **`User.password_hash`**: 컬럼명만 먼저 확정. 실제 해싱 알고리즘(bcrypt/argon2)은
  Phase 1b에서 결정한다. 본 문서에서는 "미정(bcrypt 또는 argon2)"으로만 표기.

---

## 1. 테이블 일람 (13개)

| # | 테이블 | 목적 | 실ORM 모델 필요? (Phase 1a 기준) |
|---|--------|------|----------------------------------|
| 1 | `users` | 팀 구성원 계정 | O (리셋 트랜잭션에서 user_id 조회 필요) |
| 2 | `user_sessions` | 로그인 세션 토큰 | X (Phase 1b에서 ORM화) |
| 3 | `announcement_user_states` | 공고별 사용자 읽음 상태 | O (리셋 대상) |
| 4 | `relevance_judgments` | canonical 단위 관련/무관 판정 | O (이관 대상) |
| 5 | `relevance_judgment_history` | 판정 이관 이력 | O (이관 대상) |
| 6 | `favorite_folders` | 즐겨찾기 폴더 (depth 2) | O (depth validator만) |
| 7 | `favorite_entries` | 폴더에 담긴 canonical | X (Phase 3b에서 ORM화) |
| 8 | `canonical_overrides` | 관리자 병합/분할 기록 | X (Phase 5) |
| 9 | `email_subscriptions` | 사용자별 이메일 구독 | X (Phase 4) |
| 10 | `admin_email_targets` | 관리자 공지 수신 대상 | X (Phase 4) |
| 11 | `audit_logs` | 감사 로그 | X (Phase 2 이후) |
| 12 | `scrape_runs` | 수집 실행 이력 | X (Phase 2에서 ORM화) |
| 13 | `attachment_analyses` | (placeholder) 첨부 분석 결과 | X (미래) |

> **"실ORM 모델 필요?"**는 Phase 1a의 변경 감지·리셋·이관 로직에 직접 쓰는지를
> 기준으로 한다. **migration은 13개 전부 생성**한다 (DDL은 Phase 1a에서 확정).
> 실ORM 모델 추가 대상은 `users / announcement_user_states / relevance_judgments /
> relevance_judgment_history / favorite_folders` 5개.

---

## 2. 테이블 상세

### 2.1 `users`

사용자(계정) 엔티티. Phase 1a 시점에는 실제 로그인 플로우가 없지만, 리셋
트랜잭션이 `announcement_user_states.user_id` 를 다루기 때문에 FK target이 필요하다.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `username` | `String(64)` | NO | — | 로그인 ID. UNIQUE |
| `password_hash` | `String(255)` | NO | — | 해시 문자열. 알고리즘은 Phase 1b 결정 (bcrypt/argon2 중 미정) |
| `email` | `String(255)` | YES | — | 이메일 알림 대상. Phase 4에서 사용 |
| `is_admin` | `Boolean` | NO | `false` | 관리자 권한 |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | 생성 시각 |

- **UNIQUE**: `uq_users_username (username)`
- **INDEX**: `ix_users_email (email)` — Phase 4 이메일 역조회. NULL 허용이므로 부분
  인덱스가 이상적이지만 SQLite 호환을 위해 일반 인덱스.
- **비고**:
  - `username`은 프로젝트 내부 관용상 소문자 ASCII + 숫자 + `_` 만 허용할 예정
    (Phase 1b에서 validator 추가).
  - `password_hash` 길이 255는 argon2·bcrypt 어느 쪽이든 여유 있음.

### 2.2 `user_sessions`

로그인 세션. 세션 ID는 쿠키/토큰으로 전달되는 불투명 문자열.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `session_id` | `String(64)` | NO | — | PK. 랜덤 문자열(예: `secrets.token_urlsafe`) |
| `user_id` | `Integer` | NO | — | FK → `users.id` |
| `expires_at` | `DateTime(tz=True)` | NO | — | 만료 시각(UTC) |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | 세션 발급 시각 |

- **PK**: `session_id` (single column). `pk_user_sessions` 는 Alembic 자동 명명
  허용.
- **FK**: `fk_user_sessions_user_id (user_id) → users.id ON DELETE CASCADE`
  (사용자 삭제 시 세션도 일괄 제거).
- **INDEX**:
  - `ix_user_sessions_user_id (user_id)` — 사용자별 세션 나열
  - `ix_user_sessions_expires_at (expires_at)` — 만료 cleanup 배치용

### 2.3 `announcement_user_states`

공고 1건에 대한 사용자별 상태(현재는 "읽음" 하나). **내용 변경 시 리셋 대상**.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `announcement_id` | `Integer` | NO | — | FK → `announcements.id` |
| `user_id` | `Integer` | NO | — | FK → `users.id` |
| `is_read` | `Boolean` | NO | `false` | 읽음 여부 |
| `read_at` | `DateTime(tz=True)` | YES | — | 마지막 읽음 시각 (읽지 않은 동안 NULL) |
| `updated_at` | `DateTime(tz=True)` | NO | `_utcnow()`, `onupdate=_utcnow()` | 마지막 갱신 |

- **UNIQUE**: `uq_announcement_user_states_ann_user (announcement_id, user_id)`
- **FK**:
  - `fk_announcement_user_states_announcement_id → announcements.id ON DELETE CASCADE`
  - `fk_announcement_user_states_user_id → users.id ON DELETE CASCADE`
- **INDEX**:
  - `ix_announcement_user_states_user_id (user_id)` — 사용자별 "읽지 않은 공고" 조회
  - UNIQUE 가 implicit index로 `(announcement_id, user_id)` 조회를 커버
- **리셋 규칙 (§9 참조)**: 대상 `announcement_id` 의 모든 row에 대해
  `is_read=False, read_at=NULL` 로 UPDATE.

### 2.4 `relevance_judgments`

**canonical_project 단위** 관련성 판정(현재 유효). 같은 canonical + 같은 user
조합은 단 1건만 유효.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `canonical_project_id` | `Integer` | NO | — | FK → `canonical_projects.id` |
| `user_id` | `Integer` | NO | — | FK → `users.id` |
| `verdict` | `String(16)` | NO | — | 허용값: `관련`, `무관` (한글 원문) |
| `reason` | `Text` | YES | — | 사용자가 적은 짧은 메모 |
| `decided_at` | `DateTime(tz=True)` | NO | `_utcnow()` | 판정 시각 |

- **UNIQUE**: `uq_relevance_judgments_canonical_user (canonical_project_id, user_id)`
- **CHECK**: `ck_relevance_judgments_verdict (verdict IN ('관련','무관'))`
- **FK**:
  - `fk_relevance_judgments_canonical_project_id → canonical_projects.id ON DELETE CASCADE`
  - `fk_relevance_judgments_user_id → users.id ON DELETE CASCADE`
- **INDEX**:
  - `ix_relevance_judgments_user_id (user_id)` — 사용자별 판정 리스트
  - UNIQUE 가 `(canonical_project_id, user_id)` 조회 커버
- **리셋 규칙 (§9 참조)**: 대상 canonical 의 모든 row를 `relevance_judgment_history` 로
  이관 후 원본 row 삭제.

### 2.5 `relevance_judgment_history`

`relevance_judgments` 의 과거 판정 보존. 내용 변경(`archive_reason='content_changed'`)
이나 사용자 재판정 시 원본 row를 이 테이블로 옮긴다.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `canonical_project_id` | `Integer` | NO | — | 이관 시점의 canonical id |
| `user_id` | `Integer` | NO | — | 이관 시점의 user id |
| `verdict` | `String(16)` | NO | — | 이관 시점 값 |
| `reason` | `Text` | YES | — | 이관 시점 값 |
| `decided_at` | `DateTime(tz=True)` | NO | — | 원래 판정 시각 (덮어쓰지 않음) |
| `archived_at` | `DateTime(tz=True)` | NO | `_utcnow()` | 이관 시각 |
| `archive_reason` | `String(32)` | NO | — | 허용값: `content_changed`, `user_overwrite`, `admin_override` |

- **FK**:
  - `fk_relevance_judgment_history_canonical_project_id → canonical_projects.id ON DELETE CASCADE`
  - `fk_relevance_judgment_history_user_id → users.id ON DELETE CASCADE`
- **CHECK**: `ck_relevance_judgment_history_archive_reason (archive_reason IN
  ('content_changed','user_overwrite','admin_override'))`
- **INDEX**:
  - `ix_relevance_judgment_history_canonical_user (canonical_project_id, user_id)` —
    과거 판정 조회
  - `ix_relevance_judgment_history_archived_at (archived_at)` — 최근 이관 나열

### 2.6 `favorite_folders`

사용자별 즐겨찾기 폴더. **depth 2 제한** — 최상위 폴더(depth=0) 아래에 1단계
하위 폴더(depth=1)만 허용. 그 아래는 만들 수 없다.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `user_id` | `Integer` | NO | — | FK → `users.id` |
| `parent_id` | `Integer` | YES | — | FK → `favorite_folders.id` (self). NULL이면 루트 |
| `name` | `String(128)` | NO | — | 폴더명 |
| `depth` | `Integer` | NO | `0` | ORM validator가 0 또는 1만 허용 |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |
| `updated_at` | `DateTime(tz=True)` | NO | `_utcnow()`, `onupdate=_utcnow()` | |

- **UNIQUE**: `uq_favorite_folders_user_parent_name (user_id, parent_id, name)` —
  같은 사용자의 같은 부모 아래에 동명 폴더 불가.
  > 주의: SQLite/Postgres 모두 UNIQUE 에서 NULL 은 "서로 다른 값"으로 취급되어
  > `parent_id IS NULL` 인 루트 폴더 동명 중복을 완전히 막지는 못한다.
  > **Phase 1b에서 repository 레벨 app-check 로 루트 동명 검사를 보강**한다
  > (본 문서 범위 밖의 후속 조치). migration에서는 이 UNIQUE 만 선언.
- **FK**:
  - `fk_favorite_folders_user_id → users.id ON DELETE CASCADE`
  - `fk_favorite_folders_parent_id → favorite_folders.id ON DELETE CASCADE`
    (부모 삭제 시 자식까지 함께 제거 — depth 2라 깊지 않아 안전)
- **CHECK**: `ck_favorite_folders_depth (depth IN (0, 1))` — DDL 에도 최소한
  2단 제한을 DB 레벨로 박아 두고, ORM validator가 "parent의 depth + 1 == self.depth"
  관계까지 추가로 강제한다. (depth 값의 범위는 DB, 부모-자식 관계 일관성은 ORM.)
- **INDEX**:
  - `ix_favorite_folders_user_id (user_id)`
  - `ix_favorite_folders_parent_id (parent_id)`

**ORM validator 의사코드**:

```python
from sqlalchemy.orm import validates

class FavoriteFolder(Base):
    @validates("parent_id", "depth")
    def _validate_depth(self, key, value):
        # parent 가 지정되면 parent.depth == 0 이어야 하고 self.depth 는 1이어야 한다.
        # parent 가 없으면(NULL) self.depth 는 0이어야 한다.
        # 세부 구현은 Phase 1a subtask 2에서 확정.
        ...
```

### 2.7 `favorite_entries`

폴더에 담긴 canonical. **리셋 대상이 아님** — 사용자가 직접 제거하기 전까지는
내용 변경과 무관하게 유지된다(§9 참조).

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `folder_id` | `Integer` | NO | — | FK → `favorite_folders.id` |
| `canonical_project_id` | `Integer` | NO | — | FK → `canonical_projects.id` |
| `added_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |

- **UNIQUE**: `uq_favorite_entries_folder_canonical (folder_id, canonical_project_id)` —
  같은 폴더에 같은 canonical 중복 금지.
- **FK**:
  - `fk_favorite_entries_folder_id → favorite_folders.id ON DELETE CASCADE`
  - `fk_favorite_entries_canonical_project_id → canonical_projects.id ON DELETE CASCADE`
- **INDEX**:
  - `ix_favorite_entries_canonical_project_id (canonical_project_id)` — 특정
    canonical을 어느 폴더들이 담고 있는지 역조회

### 2.8 `canonical_overrides`

관리자가 canonical 그룹을 수동으로 병합/분할한 기록. **실제 실행 로직은 Phase 5**.
Phase 1a에서는 테이블만 만든다.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `action` | `String(16)` | NO | — | 허용값: `merge`, `split` |
| `source_ids` | `JSON` | NO | — | 구조는 §7.2 참조 |
| `decided_by` | `Integer` | NO | — | FK → `users.id` (관리자) |
| `reason` | `Text` | YES | — | 사유 메모 |
| `decided_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |

- **CHECK**: `ck_canonical_overrides_action (action IN ('merge', 'split'))`
- **FK**: `fk_canonical_overrides_decided_by → users.id ON DELETE RESTRICT` —
  감사 추적상 관리자 삭제를 쉽게 허용하지 않는다.
- **INDEX**:
  - `ix_canonical_overrides_decided_by (decided_by)`
  - `ix_canonical_overrides_decided_at (decided_at)`

### 2.9 `email_subscriptions`

사용자별 이메일 알림 구독. 필터 조건은 JSON. **실제 발송은 Phase 4**.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `user_id` | `Integer` | NO | — | FK → `users.id` |
| `filter_config` | `JSON` | NO | `{}` | 구조는 §7.1 참조 |
| `is_active` | `Boolean` | NO | `true` | 일시 중지용 |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |
| `updated_at` | `DateTime(tz=True)` | NO | `_utcnow()`, `onupdate=_utcnow()` | |

- **FK**: `fk_email_subscriptions_user_id → users.id ON DELETE CASCADE`
- **INDEX**:
  - `ix_email_subscriptions_user_id (user_id)`
  - `ix_email_subscriptions_is_active (is_active)` — 활성 구독만 순회할 때

### 2.10 `admin_email_targets`

관리자 공지/오류 알림 수신자 목록. `users` 외부(예: 팀 공용 주소)도 포함 가능.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `email` | `String(255)` | NO | — | 수신 이메일 |
| `label` | `String(64)` | YES | — | 표시용 별칭 |
| `is_active` | `Boolean` | NO | `true` | |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |

- **UNIQUE**: `uq_admin_email_targets_email (email)`
- **INDEX**: `ix_admin_email_targets_is_active (is_active)`

### 2.11 `audit_logs`

사용자/시스템 액션 감사 로그. payload는 action 별로 스키마가 다르므로 JSON.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `actor_user_id` | `Integer` | YES | — | FK → `users.id`. 시스템(배치) 액션은 NULL |
| `action` | `String(64)` | NO | — | 점 표기 이벤트명. 예시는 §7.3 |
| `target_type` | `String(32)` | YES | — | 예: `announcement`, `canonical_project`, `user`, `favorite_folder` |
| `target_id` | `String(64)` | YES | — | 대상 엔티티의 PK. 문자열로 저장(혼합 타입 대비) |
| `payload` | `JSON` | NO | `{}` | action별 부가 정보 |
| `created_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |

- **FK**: `fk_audit_logs_actor_user_id → users.id ON DELETE SET NULL` — 사용자
  탈퇴 후에도 감사 로그 자체는 남긴다.
- **INDEX**:
  - `ix_audit_logs_actor_user_id (actor_user_id)`
  - `ix_audit_logs_action (action)`
  - `ix_audit_logs_target (target_type, target_id)` — 대상 기반 감사 추적
  - `ix_audit_logs_created_at (created_at)` — 최근순 나열

### 2.12 `scrape_runs`

수집 실행 1회의 요약. 수동/스케줄/CLI 트리거 모두 같은 테이블에 기록.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `started_at` | `DateTime(tz=True)` | NO | `_utcnow()` | |
| `ended_at` | `DateTime(tz=True)` | YES | — | 완료/실패 시 채움 |
| `status` | `String(16)` | NO | `running` | 허용값: `running`, `completed`, `cancelled`, `failed`, `partial` |
| `trigger` | `String(16)` | NO | — | 허용값: `manual`, `scheduled`, `cli` |
| `source_counts` | `JSON` | NO | `{}` | `{"IRIS": {"list": 120, "detail": 100, "attachments": 87}, "NTIS": {...}}` |
| `error_message` | `Text` | YES | — | 실패 시 요약 메시지 |
| `pid` | `Integer` | YES | — | 실행 프로세스 PID (cancel 용) |

- **CHECK**:
  - `ck_scrape_runs_status (status IN ('running','completed','cancelled','failed','partial'))`
  - `ck_scrape_runs_trigger (trigger IN ('manual','scheduled','cli'))`
- **INDEX**:
  - `ix_scrape_runs_started_at (started_at)` — 최근 실행 나열
  - `ix_scrape_runs_status (status)` — `running` 필터링

### 2.13 `attachment_analyses` (placeholder)

첨부파일 분석(파싱 + LLM 추출) 결과를 담을 자리. **Phase 1a에서는 테이블만
만들고 아무도 INSERT 하지 않는다.** 실구현은 미래 phase.

| 컬럼 | 타입 | NULL | Default | 비고 |
|------|------|------|---------|------|
| `id` | `Integer` | NO | AUTO | PK |
| `attachment_id` | `Integer` | NO | — | FK → `attachments.id`. UNIQUE (1:1) |
| `full_text` | `Text` | YES | — | 추출 전체 텍스트 |
| `structured_metadata` | `JSON` | YES | — | 구조는 §7.4. 없으면 NULL |
| `summary` | `Text` | YES | — | LLM 요약 |
| `parser_version` | `String(32)` | YES | — | 텍스트 추출 파서 버전 |
| `model_version` | `String(64)` | YES | — | 요약/구조화에 쓴 모델 식별자 |
| `status` | `String(16)` | YES | — | 허용값: `pending`, `success`, `failed` |
| `analyzed_at` | `DateTime(tz=True)` | YES | — | 분석 완료 시각 |

- **UNIQUE**: `uq_attachment_analyses_attachment_id (attachment_id)`
- **FK**: `fk_attachment_analyses_attachment_id → attachments.id ON DELETE CASCADE`
- **CHECK**: `ck_attachment_analyses_status (status IS NULL OR status IN
  ('pending','success','failed'))` — `status` 가 NULL 허용이므로 IS NULL 분기 포함.
- **INDEX**: `ix_attachment_analyses_status (status)` — 미처리 큐 조회용

---

## 3. FK 관계 그래프

```
┌──────────────────────┐
│ canonical_projects   │  (baseline)
└──────────────────────┘
  ▲   ▲   ▲
  │   │   └────────────────────── favorite_entries.canonical_project_id  (CASCADE)
  │   └─────────────────────────── relevance_judgments.canonical_project_id (CASCADE)
  │                                relevance_judgment_history.canonical_project_id (CASCADE)
  │
┌──────────────────────┐
│ announcements        │  (baseline)  ── canonical_group_id → canonical_projects.id (SET NULL)
└──────────────────────┘
  ▲
  └─── announcement_user_states.announcement_id  (CASCADE)

┌──────────────────────┐
│ attachments          │  (baseline)  ── announcement_id → announcements.id (CASCADE)
└──────────────────────┘
  ▲
  └─── attachment_analyses.attachment_id  (CASCADE, UNIQUE 1:1)

┌──────────────────────┐
│ users                │
└──────────────────────┘
  ▲  ▲  ▲  ▲  ▲  ▲  ▲
  │  │  │  │  │  │  └── user_sessions.user_id  (CASCADE)
  │  │  │  │  │  └───── announcement_user_states.user_id  (CASCADE)
  │  │  │  │  └──────── relevance_judgments.user_id  (CASCADE)
  │  │  │  └─────────── relevance_judgment_history.user_id  (CASCADE)
  │  │  └────────────── favorite_folders.user_id  (CASCADE)
  │  └───────────────── email_subscriptions.user_id  (CASCADE)
  │
  ├── canonical_overrides.decided_by  (RESTRICT)
  └── audit_logs.actor_user_id  (SET NULL, nullable)

┌──────────────────────┐
│ favorite_folders     │  ── parent_id → favorite_folders.id (CASCADE, self)
└──────────────────────┘
  ▲
  └── favorite_entries.folder_id  (CASCADE)
```

**ondelete 정책 요약**:

| 관계 | 정책 | 이유 |
|------|------|------|
| `users` → `user_sessions / announcement_user_states / relevance_judgments / relevance_judgment_history / favorite_folders / email_subscriptions` | CASCADE | 사용자 탈퇴 시 개인화 데이터 정리 |
| `users` → `canonical_overrides.decided_by` | RESTRICT | 감사 추적 보존 — 삭제를 막아 관리자가 먼저 override 레코드를 정리하도록 |
| `users` → `audit_logs.actor_user_id` | SET NULL | 사용자 탈퇴 후에도 로그는 남김 |
| `announcements` → `announcement_user_states` | CASCADE | 공고 삭제(현재는 없음) 시 함께 제거 |
| `canonical_projects` → `relevance_judgments / relevance_judgment_history / favorite_entries` | CASCADE | canonical 삭제 시 모든 참조 정리 |
| `attachments` → `attachment_analyses` | CASCADE | 첨부 재수집 시 기존 분석 자동 무효화 |
| `favorite_folders` → `favorite_folders` (self) / `favorite_entries` | CASCADE | 폴더 트리 일괄 제거 |

---

## 4. 제약 이름 전수표 (Alembic migration에서 그대로 사용)

| 테이블 | constraint 유형 | 이름 |
|--------|-----------------|------|
| users | UNIQUE | `uq_users_username` |
| user_sessions | FK | `fk_user_sessions_user_id` |
| announcement_user_states | UNIQUE | `uq_announcement_user_states_ann_user` |
| announcement_user_states | FK | `fk_announcement_user_states_announcement_id` |
| announcement_user_states | FK | `fk_announcement_user_states_user_id` |
| relevance_judgments | UNIQUE | `uq_relevance_judgments_canonical_user` |
| relevance_judgments | CHECK | `ck_relevance_judgments_verdict` |
| relevance_judgments | FK | `fk_relevance_judgments_canonical_project_id` |
| relevance_judgments | FK | `fk_relevance_judgments_user_id` |
| relevance_judgment_history | CHECK | `ck_relevance_judgment_history_archive_reason` |
| relevance_judgment_history | FK | `fk_relevance_judgment_history_canonical_project_id` |
| relevance_judgment_history | FK | `fk_relevance_judgment_history_user_id` |
| favorite_folders | UNIQUE | `uq_favorite_folders_user_parent_name` |
| favorite_folders | CHECK | `ck_favorite_folders_depth` |
| favorite_folders | FK | `fk_favorite_folders_user_id` |
| favorite_folders | FK | `fk_favorite_folders_parent_id` |
| favorite_entries | UNIQUE | `uq_favorite_entries_folder_canonical` |
| favorite_entries | FK | `fk_favorite_entries_folder_id` |
| favorite_entries | FK | `fk_favorite_entries_canonical_project_id` |
| canonical_overrides | CHECK | `ck_canonical_overrides_action` |
| canonical_overrides | FK | `fk_canonical_overrides_decided_by` |
| email_subscriptions | FK | `fk_email_subscriptions_user_id` |
| admin_email_targets | UNIQUE | `uq_admin_email_targets_email` |
| audit_logs | FK | `fk_audit_logs_actor_user_id` |
| scrape_runs | CHECK | `ck_scrape_runs_status` |
| scrape_runs | CHECK | `ck_scrape_runs_trigger` |
| attachment_analyses | UNIQUE | `uq_attachment_analyses_attachment_id` |
| attachment_analyses | CHECK | `ck_attachment_analyses_status` |
| attachment_analyses | FK | `fk_attachment_analyses_attachment_id` |

---

## 5. 인덱스 전수표

| 테이블 | 인덱스 이름 | 컬럼 | 용도 |
|--------|-------------|------|------|
| users | `ix_users_email` | `email` | 이메일 역조회 |
| user_sessions | `ix_user_sessions_user_id` | `user_id` | 사용자별 세션 |
| user_sessions | `ix_user_sessions_expires_at` | `expires_at` | 만료 cleanup |
| announcement_user_states | `ix_announcement_user_states_user_id` | `user_id` | 사용자별 읽지 않은 공고 |
| relevance_judgments | `ix_relevance_judgments_user_id` | `user_id` | 사용자별 판정 리스트 |
| relevance_judgment_history | `ix_relevance_judgment_history_canonical_user` | `canonical_project_id, user_id` | 과거 판정 조회 |
| relevance_judgment_history | `ix_relevance_judgment_history_archived_at` | `archived_at` | 최근 이관 나열 |
| favorite_folders | `ix_favorite_folders_user_id` | `user_id` | 사용자별 폴더 트리 |
| favorite_folders | `ix_favorite_folders_parent_id` | `parent_id` | 트리 순회 |
| favorite_entries | `ix_favorite_entries_canonical_project_id` | `canonical_project_id` | canonical→폴더 역조회 |
| canonical_overrides | `ix_canonical_overrides_decided_by` | `decided_by` | 관리자별 이력 |
| canonical_overrides | `ix_canonical_overrides_decided_at` | `decided_at` | 최근 override 나열 |
| email_subscriptions | `ix_email_subscriptions_user_id` | `user_id` | 사용자별 구독 |
| email_subscriptions | `ix_email_subscriptions_is_active` | `is_active` | 활성 구독 순회 |
| admin_email_targets | `ix_admin_email_targets_is_active` | `is_active` | 활성 대상 순회 |
| audit_logs | `ix_audit_logs_actor_user_id` | `actor_user_id` | 사용자별 감사 |
| audit_logs | `ix_audit_logs_action` | `action` | 액션 필터 |
| audit_logs | `ix_audit_logs_target` | `target_type, target_id` | 대상 기반 감사 |
| audit_logs | `ix_audit_logs_created_at` | `created_at` | 최근 나열 |
| scrape_runs | `ix_scrape_runs_started_at` | `started_at` | 최근 실행 |
| scrape_runs | `ix_scrape_runs_status` | `status` | running 필터 |
| attachment_analyses | `ix_attachment_analyses_status` | `status` | 처리 큐 |

---

## 6. 허용값(enum) 목록

`native_enum=False` 전제로, DB에는 `String(N)` + `CHECK` 로 박고 Python에서는
`StrEnum` 으로 다룬다(후속 Phase에서 StrEnum 클래스 도입).

| 컬럼 | 허용값 |
|------|--------|
| `relevance_judgments.verdict` | `관련`, `무관` |
| `relevance_judgment_history.archive_reason` | `content_changed`, `user_overwrite`, `admin_override` |
| `canonical_overrides.action` | `merge`, `split` |
| `scrape_runs.status` | `running`, `completed`, `cancelled`, `failed`, `partial` |
| `scrape_runs.trigger` | `manual`, `scheduled`, `cli` |
| `attachment_analyses.status` | `pending`, `success`, `failed` (NULL 허용) |
| `favorite_folders.depth` | `0`, `1` (DB CHECK + ORM validator 모두) |

---

## 7. JSON 컬럼 구조 — 확장 여지

### 7.1 `email_subscriptions.filter_config`

**예상 최상위 키** (Phase 4에서 확정):

| 키 | 타입 | 의미 |
|----|------|------|
| `status` | `string[] \| null` | 허용 상태(`접수중` / `접수예정` / `마감`). NULL/생략 시 전체 |
| `source` | `string[] \| null` | 허용 소스(`IRIS`, `NTIS`). NULL/생략 시 전체 |
| `agency_keywords` | `string[]` | 기관명 부분 일치 키워드(대소문자 무시). OR 결합 |
| `title_keywords` | `string[]` | 제목 부분 일치 키워드. OR 결합 |
| `canonical_group_ids` | `integer[]` | 즐겨찾기처럼 특정 canonical만 구독 |
| `deadline_within_days` | `integer \| null` | 마감이 N일 이내인 공고만 |
| `only_new_or_changed` | `boolean` | true면 내용 변경/신규만. 기본 true |
| `schedule` | `object` | `{"cadence": "daily"\|"weekly", "hour_utc": 0..23, "dow": 0..6}` 등 |

**예시**:

```json
{
  "status": ["접수중", "접수예정"],
  "source": ["IRIS"],
  "agency_keywords": ["과학기술정보통신부"],
  "title_keywords": ["AI", "인공지능"],
  "deadline_within_days": 14,
  "only_new_or_changed": true,
  "schedule": {"cadence": "daily", "hour_utc": 0}
}
```

- 빈 배열(`[]`)과 키 생략은 **의미가 다르다**. 빈 배열은 "명시적으로 아무것도
  허용 안 함(=결과 0건)", 키 생략/NULL은 "제한 없음"으로 해석. Phase 4에서 이
  규칙을 코드에 반영.

### 7.2 `canonical_overrides.source_ids`

`action` 값에 따라 구조가 다르다.

**`action == "merge"`** — 여러 canonical을 하나로 합침:

```json
{
  "ids": [12, 45, 77],
  "target_canonical_key": "official:과학기술정보통신부공고제2026-0455호",
  "keep_id": 12
}
```

- `ids`: 병합 대상 canonical_project id 목록(전체). `keep_id`는 이 중 살아남을 id.
- `target_canonical_key`는 병합 후 `canonical_projects.canonical_key` 로 쓸 값.
- 실행 로직(Phase 5)은 나머지 id의 `announcements`를 `keep_id` 로 재연결한다.

**`action == "split"`** — 한 canonical을 여러 개로 분리:

```json
{
  "from": 42,
  "into": [
    {
      "title": "분리된 과제 A",
      "canonical_key": "fuzzy:분리된과제A|...|2026",
      "keep_announcements": [101, 103, 107]
    },
    {
      "title": "분리된 과제 B",
      "canonical_key": "fuzzy:분리된과제B|...|2026",
      "keep_announcements": [102, 105]
    }
  ]
}
```

- `from`: 분리 원본 canonical id.
- `into[*].keep_announcements`: 새 canonical로 옮길 `announcements.id` 목록.
- 실행 로직(Phase 5)은 `into` 항목마다 새 `canonical_projects` row를 만들고
  해당 announcement들의 `canonical_group_id` 를 갱신한다.

### 7.3 `audit_logs.payload` — action별 예시

`action` 값은 `.` 구분 계층 이벤트명으로 통일. 예시 테이블:

| action | target_type / target_id | payload 예시 |
|--------|--------------------------|--------------|
| `user.login` | `user / 3` | `{"session_id": "abc...", "ip": "10.0.0.5", "user_agent": "..."}` |
| `user.logout` | `user / 3` | `{"session_id": "abc..."}` |
| `user.login_failed` | `null / null` | `{"username_attempted": "alice", "ip": "10.0.0.5"}` |
| `relevance.judge` | `canonical_project / 42` | `{"verdict": "관련", "reason": "기계학습 주제"}` |
| `relevance.archived` | `canonical_project / 42` | `{"archived_ids": [17, 23], "archive_reason": "content_changed"}` |
| `favorite.folder.create` | `favorite_folder / 9` | `{"name": "AI", "parent_id": null, "depth": 0}` |
| `favorite.folder.rename` | `favorite_folder / 9` | `{"from": "AI", "to": "인공지능"}` |
| `favorite.move` | `favorite_entry / 30` | `{"canonical_project_id": 42, "from_folder_id": 9, "to_folder_id": 11}` |
| `canonical.override` | `canonical_project / 42` | `{"action": "merge", "source_ids": {...§7.2}}` |
| `scrape.run.started` | `scrape_run / 5` | `{"trigger": "manual", "requested_sources": ["IRIS"]}` |
| `scrape.run.completed` | `scrape_run / 5` | `{"status": "completed", "source_counts": {"IRIS": {...}}}` |
| `scrape.announcement.changed` | `announcement / 1024` | `{"changed_fields": ["title", "deadline_at"], "reset_counts": {"announcement_user_states": 3, "relevance_judgments_archived": 1}}` |
| `admin.email_target.add` | `admin_email_target / 4` | `{"email": "ops@example.com", "label": "운영팀"}` |

**규약**:

- `payload` 의 모든 datetime 값은 ISO 8601 UTC 문자열로 저장(예: `"2026-04-22T14:13:12+00:00"`).
- 민감 정보(비밀번호 평문, 세션 토큰 원문)는 절대 payload에 넣지 않는다.
  `session_id` 가 들어가는 경우는 서버 내부에서만 다루는 id의 일부 prefix 또는 hash.

### 7.4 `attachment_analyses.structured_metadata` — 예상 키 (placeholder)

**Phase 1a에서는 아무도 INSERT 하지 않는다.** 아래는 미래 phase가 채울
가능성이 있는 키의 예시로, migration 시점에는 "빈 dict / NULL" 중 하나만 허용.

| 키 | 타입 | 의미 |
|----|------|------|
| `amount_max` | `number \| null` | 최대 지원 금액(원) |
| `amount_min` | `number \| null` | 최소 지원 금액(원) |
| `deadline_applied` | `string (ISO8601) \| null` | 문서에 적힌 접수 마감 — `announcements.deadline_at`와 교차 검증용 |
| `contact_email` | `string \| null` | 담당자 이메일 |
| `contact_phone` | `string \| null` | 담당자 전화 |
| `required_docs` | `string[]` | 제출 서류 목록 |
| `eligibility` | `string[]` | 지원 자격 요약 문장 목록 |
| `keywords` | `string[]` | LLM 추출 키워드 |

**예시**:

```json
{
  "amount_max": 500000000,
  "amount_min": null,
  "deadline_applied": "2026-05-20T15:00:00+09:00",
  "contact_email": "pm@nrf.re.kr",
  "required_docs": ["사업계획서", "예산서", "참여연구자 이력서"],
  "eligibility": ["국내 대학 소속 연구자", "박사학위 취득 후 7년 이내"],
  "keywords": ["인공지능", "의료영상"]
}
```

이 키 목록은 **계약(contract)이 아니며**, 미래 phase에서 정식화될 때까지는 어떤
코드도 이 키의 존재를 가정하지 않는다.

---

## 8. 시간 컬럼 전수 확인 (db_portability.md 1.2 준수)

모든 시간 컬럼이 `DateTime(timezone=True)` 인지 한 번에 확인:

| 테이블 | 시간 컬럼 |
|--------|-----------|
| users | `created_at` |
| user_sessions | `expires_at`, `created_at` |
| announcement_user_states | `read_at` (NULL 허용), `updated_at` |
| relevance_judgments | `decided_at` |
| relevance_judgment_history | `decided_at`, `archived_at` |
| favorite_folders | `created_at`, `updated_at` |
| favorite_entries | `added_at` |
| canonical_overrides | `decided_at` |
| email_subscriptions | `created_at`, `updated_at` |
| admin_email_targets | `created_at` |
| audit_logs | `created_at` |
| scrape_runs | `started_at`, `ended_at` (NULL 허용) |
| attachment_analyses | `analyzed_at` (NULL 허용) |

전부 `DateTime(timezone=True)`. naive datetime 컬럼 없음.

---

## 9. 변경 감지·리셋 영향 요약

Phase 1a의 **변경 감지 확장 + 리셋**이 위 테이블들에 미치는 영향을 요약한다.
상세 구현은 후속 subtask (`repository.py`) 에서.

### 9.1 변경 감지 비교 필드

| 단계 | 비교 필드 |
|------|-----------|
| 1차 감지 (목록 UPSERT 직후, 기존) | `title`, `status`, `deadline_at`, `agency` |
| 2차 감지 (첨부 다운로드 후, 신규) | 1차의 4필드 + **첨부 개수 + 기존 sha256 대비 신규 sha256 + 첨부 추가/삭제** |

**단독 status 전이**: 1차 감지에서 오직 `status`만 달라진 경우는 **in-place UPDATE**
를 유지한다. `is_current` 순환(기존 row 봉인 + 신규 INSERT)을 일으키지 않으며,
**리셋도 수행하지 않는다**. (status 단독 전이는 접수예정→접수중 등 예측 가능한
라이프사이클 전이이므로 사용자의 읽음·판정을 초기화할 이유가 없다.)

**그 외 변경 (1차에서 status 외 필드 변경, 또는 2차에서 첨부 signature 변경)**:
`is_current` 순환과 동시에 아래 9.2 리셋 로직이 **같은 트랜잭션**에서 실행된다.

### 9.2 리셋 동작

대상: 변경이 감지된 `announcements` row 의 canonical_project.

| 테이블 | 동작 |
|--------|------|
| `announcement_user_states` | 해당 `announcement_id` 의 모든 row를 `is_read=False, read_at=NULL` 로 UPDATE |
| `relevance_judgments` | 해당 `canonical_project_id` 의 모든 row를 `relevance_judgment_history` 로 복사 후 원본 DELETE. `archive_reason='content_changed'`, `archived_at=_utcnow()` |
| `relevance_judgment_history` | INSERT만. 이미 이관된 레코드는 손대지 않는다 |
| `favorite_entries` | **손대지 않는다**. 사용자는 자신이 원할 때까지 즐겨찾기를 유지한다. (cf. 즐겨찾기는 "관심이 있다"의 플래그이지 "내용 승인"은 아님) |
| 기타 테이블 | 영향 없음 |

### 9.3 트랜잭션 경계

UPSERT(기존 row 봉인 + 신규 INSERT) 와 위 리셋 동작은 **같은 트랜잭션** 안에서
atomic 하게 처리한다. 중간에 예외가 발생하면 **UPSERT 자체도 롤백**되어야
한다 (그렇지 않으면 "내용은 새 row에 저장됐는데 읽음 상태는 그대로" 같은 오염된
상태가 남는다). 검증은 subtask 6의 유닛 테스트가 담당한다.

### 9.4 Phase 1b 전 임시 조치

현재 `users` 테이블에는 실제 row가 없다(Phase 1a 종료 시점에도 없음). 그래서
`announcement_user_states / relevance_judgments` 는 실운용에서 비어 있다. 리셋
로직은 **비어 있어도 NO-OP로 정상 동작**해야 하며, 유닛 테스트는 "가짜 User"를
트랜잭션 내에서 생성해 리셋이 실제로 값을 바꾸는지 검증한다. (subtask 6)

---

## 10. Phase 1a 범위 밖 (본 문서에서도 확정하지 않음)

- 인증 흐름(로그인 화면·세션 쿠키 발급·CSRF·rate limit) → Phase 1b
- 읽음 토글 UI → Phase 1b
- 관련성 판정 UI·bulk 판정 → Phase 3a
- 즐겨찾기 UI (드래그앤드롭·이동 등) → Phase 3b
- 관리자 화면·수집 제어·스케줄러 → Phase 2
- 이메일 발송기/SMTP 연결 → Phase 4
- `canonical_overrides` 실제 실행 로직 → Phase 5
- `attachment_analyses` 실제 파이프라인 → 별도 future phase
- 폴더 "루트 동명 금지" 강화(app-level) → Phase 1b repository

---

## 11. 후속 subtask가 이 문서를 참조하는 방식

- **subtask 2 (Alembic migration + 최소 ORM)**: §2 (필드)·§4 (제약 이름)·§5 (인덱스)·
  §6 (허용값) 를 그대로 migration 파일에 옮긴다. ORM 모델은 §1 표의 "실ORM 모델
  필요? = O" 인 5개 테이블만 만든다. `FavoriteFolder`는 §2.6 의 validator 의사코드를
  실제 코드로 구현한다.
- **subtask 3 (repository 리셋/이관)**: §9 의 트랜잭션 경계와 리셋 규약을 그대로 구현.
  `_upsert_announcement` 파라미터화 시 비교 필드는 §9.1 표 참조.
- **subtask 4 (CLI 2차 감지)**: §9.1 의 "2차 감지" 시점을 CLI 오케스트레이터에 통합.
- **subtask 6 (유닛 테스트)**: §9 의 모든 분기(단독 status 전이 / 1차 변경 / 2차 변경
  / 리셋 중 예외) 를 테스트 케이스로 매핑.
- **subtask 7 (문서 갱신)**: §0 의 전제(13개 추가 + 기존 무변경)를 `db_portability.md`
  자체점검 체크리스트 부록과 `alembic_verification.md` 신규 검증 기록에 반영.
