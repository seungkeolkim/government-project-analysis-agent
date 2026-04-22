"""phase1a: 신규 13개 테이블 추가 (팀 공용 전환 DB 레이어)

사용자 라벨링(읽음/관련성 판정/즐겨찾기)과 팀 운영(인증/감사/수집 이력/이메일)
을 위한 13개 테이블을 baseline 위에 추가한다. 기존 테이블(`canonical_projects`,
`announcements`, `attachments`)은 변경하지 않는다.

설계 근거:
    docs/schema_phase1a.md — 각 테이블의 필드/제약/인덱스 상세.
    docs/db_portability.md — JSON 범용 타입, DateTime(timezone=True),
        native_enum=False, 제약 이름 명시, ondelete 정책.

생성 순서 (FK 의존성 기준):
    1.  users
    2.  user_sessions
    3.  announcement_user_states
    4.  relevance_judgments
    5.  relevance_judgment_history
    6.  favorite_folders (self-reference FK 포함)
    7.  favorite_entries
    8.  canonical_overrides
    9.  email_subscriptions
    10. admin_email_targets
    11. audit_logs
    12. scrape_runs
    13. attachment_analyses

downgrade 는 정확히 역순으로 drop_table 한다.

Revision ID: b2c5e8f1a934
Revises: a8f3c2d14e7b
Create Date: 2026-04-22 15:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "b2c5e8f1a934"
down_revision: Union[str, None] = "a8f3c2d14e7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """baseline 위에 13개 신규 테이블을 FK 의존성 순서로 생성한다.

    모든 제약(UNIQUE/FK/CHECK)과 인덱스에 `uq_/fk_/ck_/ix_` prefix 로
    이름을 명시한다(SQLite ↔ Postgres 호환 — docs/db_portability.md §4).

    모든 시간 컬럼은 `DateTime(timezone=True)`.
    모든 JSON 컬럼은 `sa.JSON()` 범용 타입(JSONB 금지).
    """

    # ── 1. users ──────────────────────────────────────────────────────────────
    # 팀 구성원 계정. Phase 1a 에는 실제 로그인 흐름이 없지만 리셋 트랜잭션이
    # 참조할 FK target 이 필요하므로 먼저 생성한다.
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False),
        # password_hash: 해싱 알고리즘(bcrypt/argon2)은 Phase 1b 에서 결정.
        sa.Column("password_hash", sa.String(255), nullable=False),
        # email: 이메일 알림 대상. 없어도 계정 사용 가능하므로 nullable.
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # ── 2. user_sessions ─────────────────────────────────────────────────────
    # 로그인 세션. session_id 는 서버가 발급하는 랜덤 문자열(secrets.token_urlsafe).
    op.create_table(
        "user_sessions",
        # session_id 는 쿠키로 전달되는 불투명 토큰 — 충분한 길이로 확보.
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_sessions_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index(
        "ix_user_sessions_expires_at", "user_sessions", ["expires_at"]
    )

    # ── 3. announcement_user_states ──────────────────────────────────────────
    # 공고 × 사용자 단위 읽음 상태. 내용 변경 시 is_read=False 로 리셋된다.
    op.create_table(
        "announcement_user_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("announcement_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "is_read",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["announcement_id"],
            ["announcements.id"],
            name="fk_announcement_user_states_announcement_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_announcement_user_states_user_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "announcement_id",
            "user_id",
            name="uq_announcement_user_states_ann_user",
        ),
    )
    op.create_index(
        "ix_announcement_user_states_user_id",
        "announcement_user_states",
        ["user_id"],
    )

    # ── 4. relevance_judgments ───────────────────────────────────────────────
    # canonical_project × user 단위 관련/무관 판정(현재 유효).
    # 내용 변경 시 relevance_judgment_history 로 이관 후 이 테이블에서 삭제된다.
    op.create_table(
        "relevance_judgments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("canonical_project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        # verdict: '관련' | '무관' 두 값만 허용. String(8) 은 한글 2글자 + 여유.
        sa.Column("verdict", sa.String(8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["canonical_project_id"],
            ["canonical_projects.id"],
            name="fk_relevance_judgments_canonical_project_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_relevance_judgments_user_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "canonical_project_id",
            "user_id",
            name="uq_relevance_project_user",
        ),
        sa.CheckConstraint(
            "verdict IN ('관련', '무관')",
            name="ck_relevance_verdict",
        ),
    )
    op.create_index(
        "ix_relevance_judgments_user_id",
        "relevance_judgments",
        ["user_id"],
    )

    # ── 5. relevance_judgment_history ────────────────────────────────────────
    # 이관된 과거 판정. 새 판정/내용 변경 시 원본을 이 테이블로 복사한다.
    op.create_table(
        "relevance_judgment_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("canonical_project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        # decided_at: 원래 판정 시각 — 이관 시 덮어쓰지 않는다.
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        # archive_reason: 'content_changed' | 'user_overwrite' | 'admin_override'
        # 허용값은 app-level 상수로 관리 (DB CHECK 은 스키마 유연성 위해 생략).
        sa.Column("archive_reason", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["canonical_project_id"],
            ["canonical_projects.id"],
            name="fk_relevance_judgment_history_canonical_project_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_relevance_judgment_history_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_relevance_judgment_history_canonical_user",
        "relevance_judgment_history",
        ["canonical_project_id", "user_id"],
    )
    op.create_index(
        "ix_relevance_judgment_history_archived_at",
        "relevance_judgment_history",
        ["archived_at"],
    )

    # ── 6. favorite_folders ──────────────────────────────────────────────────
    # 즐겨찾기 폴더. 최대 2단(root + 하위 1단). depth 제약은 ORM validator 가
    # 담당하므로 DB CHECK 은 설치하지 않는다 (사용자 원문: "폴더 depth 2 는
    # ORM validator"). parent_id 가 삭제되어도 자식은 남기 위해 SET NULL.
    op.create_table(
        "favorite_folders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "depth",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_favorite_folders_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["favorite_folders.id"],
            name="fk_favorite_folders_parent_id",
            ondelete="SET NULL",
        ),
        # 같은 사용자의 같은 부모 아래에 동명 폴더 금지.
        # 주의: parent_id NULL 은 SQLite/Postgres 모두 "서로 다름"으로 취급되므로
        # 루트 동명 금지는 별도 app-level 검사가 필요(Phase 1b 보강).
        sa.UniqueConstraint(
            "user_id",
            "parent_id",
            "name",
            name="uq_favorite_folders_user_parent_name",
        ),
    )
    op.create_index(
        "ix_favorite_folders_user_id", "favorite_folders", ["user_id"]
    )
    op.create_index(
        "ix_favorite_folders_parent_id", "favorite_folders", ["parent_id"]
    )

    # ── 7. favorite_entries ──────────────────────────────────────────────────
    # 폴더에 담긴 canonical. 내용 변경 시 리셋하지 않는다(사용자 의도 유지).
    op.create_table(
        "favorite_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("folder_id", sa.Integer(), nullable=False),
        sa.Column("canonical_project_id", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["favorite_folders.id"],
            name="fk_favorite_entries_folder_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_project_id"],
            ["canonical_projects.id"],
            name="fk_favorite_entries_canonical_project_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "folder_id",
            "canonical_project_id",
            name="uq_favorite_entries_folder_canonical",
        ),
    )
    op.create_index(
        "ix_favorite_entries_canonical_project_id",
        "favorite_entries",
        ["canonical_project_id"],
    )

    # ── 8. canonical_overrides ───────────────────────────────────────────────
    # 관리자가 canonical 그룹을 병합/분할한 기록. 실 실행 로직은 Phase 5.
    op.create_table(
        "canonical_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        # decided_by: RESTRICT — 관리자 감사 이력 보존을 위해 사용자 삭제 불가.
        sa.Column("decided_by", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name="fk_canonical_overrides_decided_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "action IN ('merge', 'split')",
            name="ck_canonical_overrides_action",
        ),
    )
    op.create_index(
        "ix_canonical_overrides_decided_by",
        "canonical_overrides",
        ["decided_by"],
    )
    op.create_index(
        "ix_canonical_overrides_decided_at",
        "canonical_overrides",
        ["decided_at"],
    )

    # ── 9. email_subscriptions ───────────────────────────────────────────────
    # 사용자별 이메일 알림 구독. filter_config 키 구조는 docs/schema_phase1a.md §7.1.
    op.create_table(
        "email_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("filter_config", sa.JSON(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_email_subscriptions_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_email_subscriptions_user_id",
        "email_subscriptions",
        ["user_id"],
    )
    op.create_index(
        "ix_email_subscriptions_is_active",
        "email_subscriptions",
        ["is_active"],
    )

    # ── 10. admin_email_targets ──────────────────────────────────────────────
    # 관리자 공지/오류 알림 수신자. users 외부(팀 공용 주소)도 포함 가능.
    op.create_table(
        "admin_email_targets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", name="uq_admin_email_targets_email"),
    )
    op.create_index(
        "ix_admin_email_targets_is_active",
        "admin_email_targets",
        ["is_active"],
    )

    # ── 11. audit_logs ───────────────────────────────────────────────────────
    # 사용자/시스템 액션 감사 로그. payload 스키마는 action 별로 다르므로 JSON.
    # actor_user_id 는 사용자 탈퇴 후에도 로그 유지를 위해 SET NULL (nullable).
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        # target_id: 혼합 타입(int id / session_id prefix / uuid 문자열 등)
        # 대응을 위해 문자열로 저장.
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_audit_logs_actor_user_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"]
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index(
        "ix_audit_logs_target", "audit_logs", ["target_type", "target_id"]
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # ── 12. scrape_runs ──────────────────────────────────────────────────────
    # 수집 실행 1회 요약. status/trigger 는 좁은 도메인이므로 DB CHECK 로 강제.
    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("source_counts", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'cancelled', 'failed', 'partial')",
            name="ck_scrape_runs_status",
        ),
        sa.CheckConstraint(
            "trigger IN ('manual', 'scheduled', 'cli')",
            name="ck_scrape_runs_trigger",
        ),
    )
    op.create_index(
        "ix_scrape_runs_started_at", "scrape_runs", ["started_at"]
    )
    op.create_index("ix_scrape_runs_status", "scrape_runs", ["status"])

    # ── 13. attachment_analyses ──────────────────────────────────────────────
    # (placeholder) 첨부 분석 결과. Phase 1a 에서는 테이블만 만들고 INSERT 없음.
    # 사용자 원문에 따라 모든 컬럼 nullable. status 는 NULL 또는 허용값만 가능.
    op.create_table(
        "attachment_analyses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=True),
        sa.Column("structured_metadata", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.String(32), nullable=True),
        sa.Column("model_version", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["attachments.id"],
            name="fk_attachment_analyses_attachment_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "attachment_id",
            name="uq_attachment_analyses_attachment_id",
        ),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('pending', 'success', 'failed')",
            name="ck_attachment_analyses_status",
        ),
    )
    op.create_index(
        "ix_attachment_analyses_status",
        "attachment_analyses",
        ["status"],
    )


def downgrade() -> None:
    """13개 신규 테이블을 생성 역순으로 삭제한다 (FK 의존성 역순).

    인덱스는 drop_table 이 함께 처리하므로 별도 drop_index 불필요.
    baseline 테이블(canonical_projects/announcements/attachments)은 건드리지 않는다.
    """
    op.drop_table("attachment_analyses")
    op.drop_table("scrape_runs")
    op.drop_table("audit_logs")
    op.drop_table("admin_email_targets")
    op.drop_table("email_subscriptions")
    op.drop_table("canonical_overrides")
    op.drop_table("favorite_entries")
    op.drop_table("favorite_folders")
    op.drop_table("relevance_judgment_history")
    op.drop_table("relevance_judgments")
    op.drop_table("announcement_user_states")
    op.drop_table("user_sessions")
    op.drop_table("users")
