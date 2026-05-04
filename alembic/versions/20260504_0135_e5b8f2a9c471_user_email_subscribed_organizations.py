"""task 00049-1: users.email_subscribed 컬럼 추가 + organizations / user_organizations 테이블 신설

변경 사항 요약:
    1. users 테이블에 email_subscribed (Boolean, NOT NULL, default=True) 컬럼 추가.
       이메일 수신 토글 기능을 위한 컬럼. 기존 row 는 True(수신 의향) 로 backfill.
    2. organizations 테이블 신설 — 무제한 depth 의 트리 구조.
       parent_id FK 는 ON DELETE RESTRICT 로 자식이 있는 조직을 직접 삭제 불가하게 막는다.
    3. user_organizations 테이블 신설 — 사용자 ↔ 조직 M:N junction.
       사용자는 0개 이상의 조직에 속할 수 있다 (users.fk 컬럼 추가 금지 원칙 준수).
       사용자 또는 조직 삭제 시 매핑 row 도 CASCADE 삭제된다.

설계 근거:
    - 사용자 원문: "사용자는 0개 혹은 1개 이상의 조직에 포함될 수 있어.
      따라서 user table 의 fk 컬럼 추가로 가서는 안되고 별개의 조직 테이블이 필요해."
    - organizations.parent_id ON DELETE RESTRICT — 자식이 있는 부모 조직을 직접 삭제하면
      DB 레벨에서 오류 발생. app 레벨에서도 OrganizationHasChildrenError 로 먼저 막는다.
    - 루트(parent_id IS NULL) 간 동명 체크는 SQLite NULL 비교 특성상 UNIQUE 제약으로
      막을 수 없으므로 app-level SELECT 체크로 보강한다 (FavoriteFolder 동일 패턴).

SQLite ↔ Postgres 이식성:
    - users 컬럼 추가는 batch_alter_table 사용 (SQLite ALTER 호환).
    - organizations / user_organizations 는 신규 CREATE TABLE 이므로 batch 불필요.
    - 모든 constraint/index 이름은 uq_/fk_/ck_/ix_ prefix 규칙.
    - Boolean 컬럼은 sa.Boolean() — SQLite 0/1, Postgres true/false 자동 변환.

Revision ID: e5b8f2a9c471
Revises: d3f9a2b6c814
Create Date: 2026-05-04 01:35:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "e5b8f2a9c471"
down_revision: Union[str, None] = "d3f9a2b6c814"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """3가지 스키마 변경을 순차 적용한다.

    실행 순서:
        1. users 테이블에 email_subscribed 컬럼 추가 (batch_alter_table).
        2. organizations 테이블 신설 (self-referential, parent_id RESTRICT).
        3. user_organizations 테이블 신설 (M:N junction).
    """

    # ── 1. users.email_subscribed 컬럼 추가 ──────────────────────────────────
    # 기존 row 는 True(이메일 수신 의향) 로 backfill 한다.
    # SQLite ALTER TABLE 은 컬럼 추가 외의 DDL 변경을 지원하지 않으므로
    # batch_alter_table 을 사용한다.
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "email_subscribed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),  # SQLite: 1=True, Postgres: true
            )
        )

    # ── 2. organizations 테이블 신설 ────────────────────────────────────────
    # self-referential 트리 구조. parent_id 가 NULL 이면 루트 노드.
    # parent_id FK 는 ON DELETE RESTRICT — 자식이 있는 조직 삭제를 DB 가 차단.
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["organizations.id"],
            name="fk_organizations_parent_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_organizations_parent_id",
        "organizations",
        ["parent_id"],
    )

    # ── 3. user_organizations 테이블 신설 ───────────────────────────────────
    # 사용자 ↔ 조직 M:N junction. 사용자 삭제(CASCADE) 또는 조직 삭제(CASCADE) 시
    # 매핑 row 도 자동 제거된다.
    op.create_table(
        "user_organizations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_organizations_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_user_organizations_organization_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id",
            "organization_id",
            name="uq_user_organizations_user_org",
        ),
    )
    op.create_index(
        "ix_user_organizations_user_id",
        "user_organizations",
        ["user_id"],
    )
    op.create_index(
        "ix_user_organizations_organization_id",
        "user_organizations",
        ["organization_id"],
    )


def downgrade() -> None:
    """upgrade 를 역순으로 되돌린다.

    실행 순서:
        1. user_organizations 테이블 삭제.
        2. organizations 테이블 삭제.
        3. users 테이블에서 email_subscribed 컬럼 제거 (batch_alter_table).
    """

    # ── 1. user_organizations 테이블 삭제 ───────────────────────────────────
    op.drop_index("ix_user_organizations_organization_id", table_name="user_organizations")
    op.drop_index("ix_user_organizations_user_id", table_name="user_organizations")
    op.drop_table("user_organizations")

    # ── 2. organizations 테이블 삭제 ────────────────────────────────────────
    op.drop_index("ix_organizations_parent_id", table_name="organizations")
    op.drop_table("organizations")

    # ── 3. users.email_subscribed 컬럼 제거 ─────────────────────────────────
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("email_subscribed")
