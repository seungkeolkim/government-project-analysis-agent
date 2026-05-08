"""task 00094-1: DB 백업 시스템을 위한 system_settings, backup_history 테이블 추가.

설계 근거
    사용자 원문 "스케쥴에 따라 파일을 백업하고 관리 가능하도록 해줘."
    - ``system_settings``: 백업 cron 표현식·최대 보관 수 등 관리자 설정을 key-value 로 영속.
    - ``backup_history``: 백업 실행 이력(성공/실패, 대상 파일, 크기, 소요 시간)을 기록.

변경 사항 요약
    1. ``system_settings`` 테이블 생성 (PRIMARY KEY: key).
    2. ``backup_history`` 테이블 생성 (PK: id AUTOINCREMENT).
       - ``target_files``, ``backup_files`` 컬럼은 JSON 리스트.
       - ``ix_backup_history_executed_at`` 인덱스 포함.

다운그레이드
    두 테이블을 DROP 한다. 기록된 이력은 복구되지 않는다.

Revision ID: c2d3e4f5a6b7
Revises: a9c1b2d3e4f5
Create Date: 2026-05-08 07:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "a9c1b2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """system_settings 와 backup_history 테이블을 생성한다."""

    # ── 1. system_settings ────────────────────────────────────────────────────
    # 관리자가 UI 에서 변경하는 설정을 key-value 로 영속한다.
    # key 가 PRIMARY KEY 이므로 별도 UNIQUE 제약은 불필요하다.
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name="pk_system_settings"),
    )

    # ── 2. backup_history ─────────────────────────────────────────────────────
    # 백업 실행 이력 한 건 = 한 row.
    # target_files / backup_files 는 JSON 리스트 (dialect-neutral sa.JSON).
    op.create_table(
        "backup_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("target_files", sa.JSON(), nullable=False),
        sa.Column("backup_files", sa.JSON(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("total_size_bytes", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_backup_history"),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'manual')",
            name="ck_backup_history_trigger",
        ),
    )
    # executed_at 인덱스 — 이력 목록 내림차순 조회에 사용
    op.create_index(
        "ix_backup_history_executed_at",
        "backup_history",
        ["executed_at"],
    )


def downgrade() -> None:
    """backup_history 와 system_settings 테이블을 삭제한다.

    주의: 삭제된 이력·설정은 복구되지 않는다.
    downgrade 는 개발/검증 경로에서만 사용을 권장한다.
    """

    # ── 1. backup_history (인덱스 먼저 삭제) ─────────────────────────────────
    op.drop_index("ix_backup_history_executed_at", table_name="backup_history")
    op.drop_table("backup_history")

    # ── 2. system_settings ────────────────────────────────────────────────────
    op.drop_table("system_settings")
