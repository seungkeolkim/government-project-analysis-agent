"""baseline: initial schema — canonical_projects, announcements, attachments

이 migration 은 현재 운영 스키마를 Alembic baseline 으로 캡처한다.
app/db/migration.py 의 6단계 수작업 DDL 이 이미 적용된 최종 상태를 기준으로 한다.

핵심 가정:
    기존 운영 DB(app.sqlite3)에는 이 upgrade() 가 실행되지 않는다.
    기존 DB 는 `alembic stamp head` 로 리비전 레코드만 삽입한다 — 스키마/데이터 변화 0.
    upgrade() 는 신규 DB(빈 SQLite 또는 Postgres) 에서만 실제로 실행된다.

테이블 생성 순서: canonical_projects → announcements → attachments
(FK 의존성 순서)

Revision ID: a8f3c2d14e7b
Revises:
Create Date: 2026-04-22 10:10:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "a8f3c2d14e7b"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """신규 DB 에 canonical_projects → announcements → attachments 를 생성한다.

    각 테이블에는 FK constraint 이름을 명시한다 (docs/db_portability.md 4번).
    인덱스는 create_table 이후 별도 op.create_index 로 생성한다.
    """

    # ── 1. canonical_projects ────────────────────────────────────────────────
    # announcements.canonical_group_id 가 FK 로 참조하므로 먼저 생성한다.
    op.create_table(
        "canonical_projects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("canonical_key", sa.String(256), nullable=False),
        sa.Column("key_scheme", sa.String(16), nullable=False),
        sa.Column("representative_title", sa.Text(), nullable=True),
        sa.Column("representative_agency", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # canonical_key 는 cross-source 유일 식별자 — UNIQUE 제약 명시
        sa.UniqueConstraint("canonical_key", name="uq_canonical_projects_canonical_key"),
    )

    # ── 2. announcements ─────────────────────────────────────────────────────
    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_announcement_id", sa.String(128), nullable=False),
        # source_type: Python default='IRIS'. server_default 로 신규/기존 row 호환.
        sa.Column(
            "source_type",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'IRIS'"),
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("agency", sa.String(255), nullable=True),
        # status: native_enum=False — SQLite/Postgres 양쪽에서 VARCHAR 로 저장
        # (Postgres 전용 ENUM 타입 금지 — docs/db_portability.md 1번)
        sa.Column(
            "status",
            sa.Enum("접수중", "접수예정", "마감", name="announcement_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail_url", sa.Text(), nullable=True),
        sa.Column("detail_html", sa.Text(), nullable=True),
        sa.Column("detail_text", sa.Text(), nullable=True),
        sa.Column("detail_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail_fetch_status", sa.String(16), nullable=True),
        # raw_metadata: 범용 JSON 타입 (JSONB 금지 — docs/db_portability.md 1번)
        sa.Column("raw_metadata", sa.JSON(), nullable=False),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # is_current: server_default="true" — SQLite 3.23+ 및 Postgres 양쪽에서 동작
        # sa.text("1") 은 Postgres BOOLEAN 타입에서 허용되지 않으므로 "true" 사용
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("canonical_group_id", sa.Integer(), nullable=True),
        sa.Column("canonical_key", sa.String(256), nullable=True),
        sa.Column("canonical_key_scheme", sa.String(16), nullable=True),
        # FK constraint 이름 명시 (Postgres 호환 — docs/db_portability.md 4번)
        sa.ForeignKeyConstraint(
            ["canonical_group_id"],
            ["canonical_projects.id"],
            name="fk_announcements_canonical_group_id",
            ondelete="SET NULL",
        ),
    )
    # announcements 인덱스 — models.py 의 index=True / __table_args__ 이름과 일치
    op.create_index(
        # __table_args__: Index("ix_announcement_source", "source_type", "source_announcement_id")
        "ix_announcement_source",
        "announcements",
        ["source_type", "source_announcement_id"],
    )
    op.create_index(
        # source_announcement_id column index=True → ix_announcements_source_announcement_id
        "ix_announcements_source_announcement_id",
        "announcements",
        ["source_announcement_id"],
    )
    op.create_index(
        # status column index=True → ix_announcements_status
        "ix_announcements_status",
        "announcements",
        ["status"],
    )
    op.create_index(
        # deadline_at column index=True → ix_announcements_deadline_at
        "ix_announcements_deadline_at",
        "announcements",
        ["deadline_at"],
    )
    op.create_index(
        # is_current column index=True → ix_announcements_is_current
        "ix_announcements_is_current",
        "announcements",
        ["is_current"],
    )
    op.create_index(
        # canonical_group_id column index=True → ix_announcements_canonical_group_id
        "ix_announcements_canonical_group_id",
        "announcements",
        ["canonical_group_id"],
    )
    op.create_index(
        # canonical_key column index=True → ix_announcements_canonical_key
        "ix_announcements_canonical_key",
        "announcements",
        ["canonical_key"],
    )

    # ── 3. attachments ───────────────────────────────────────────────────────
    # announcements 가 먼저 존재해야 FK 를 걸 수 있다.
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("announcement_id", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("file_ext", sa.String(16), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("download_url", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False),
        # FK constraint 이름 명시 (Postgres 호환 — docs/db_portability.md 4번)
        sa.ForeignKeyConstraint(
            ["announcement_id"],
            ["announcements.id"],
            name="fk_attachments_announcement_id",
            ondelete="CASCADE",
        ),
    )
    # attachments 인덱스 — models.py 의 index=True / __table_args__ 이름과 일치
    op.create_index(
        # announcement_id column index=True → ix_attachments_announcement_id
        "ix_attachments_announcement_id",
        "attachments",
        ["announcement_id"],
    )
    op.create_index(
        # __table_args__: Index("ix_attachments_announcement_filename", ...)
        "ix_attachments_announcement_filename",
        "attachments",
        ["announcement_id", "original_filename"],
    )


def downgrade() -> None:
    """모든 테이블을 FK 의존성 역순으로 삭제한다 (attachments → announcements → canonical_projects).

    인덱스는 drop_table 이 함께 처리하므로 별도 drop_index 불필요.
    """
    op.drop_table("attachments")
    op.drop_table("announcements")
    op.drop_table("canonical_projects")
