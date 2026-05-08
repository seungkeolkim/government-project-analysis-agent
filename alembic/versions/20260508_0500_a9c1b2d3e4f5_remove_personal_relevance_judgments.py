"""task 00093-1: relevance_judgments / relevance_judgment_history 에서 개인 판정(organization_id IS NULL) row 를 삭제하고 organization_id 컬럼을 NOT NULL 로 변경한다.

설계 근거
    사용자 원문 "개인 판정 기능과 관련 로직들(crud, display, backend api)을 아예 없애줘."
    조직 판정(organization_id 가 정수인 row)만 이후에도 유지한다. 기존 개인 판정 row 는
    복구 불필요 — DELETE 후 NOT NULL 제약을 적용해 이후 개인 판정 INSERT 를 DB 레벨에서 차단.

변경 사항 요약
    1. relevance_judgments 에서 organization_id IS NULL 인 row 를 삭제한다.
    2. relevance_judgment_history 에서 organization_id IS NULL 인 row 를 삭제한다.
    3. relevance_judgments.organization_id 컬럼을 NOT NULL 로 변경한다
       (batch_alter_table — SQLite 재생성 방식 경유).
    4. relevance_judgment_history.organization_id 컬럼을 NOT NULL 로 변경한다.

다운그레이드
    organization_id 를 다시 nullable 로 돌린다. 삭제된 개인 판정 row 는 복구되지 않는다.
    downgrade 는 개발/검증 경로에서만 사용하는 것을 권장한다.

Revision ID: a9c1b2d3e4f5
Revises: f6c9a3b8d572
Create Date: 2026-05-08 05:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "a9c1b2d3e4f5"
down_revision: Union[str, None] = "f6c9a3b8d572"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """개인 판정 row 를 삭제하고 organization_id 를 NOT NULL 로 바꾼다.

    실행 순서:
        1. relevance_judgments 에서 organization_id IS NULL row 삭제.
        2. relevance_judgment_history 에서 organization_id IS NULL row 삭제.
        3. relevance_judgments.organization_id → NOT NULL (batch_alter_table).
        4. relevance_judgment_history.organization_id → NOT NULL (batch_alter_table).
    """

    # ── 1. 개인 판정 row 삭제 — relevance_judgments ───────────────────────────
    op.execute(
        sa.text("DELETE FROM relevance_judgments WHERE organization_id IS NULL")
    )

    # ── 2. 개인 판정 이력 row 삭제 — relevance_judgment_history ──────────────
    op.execute(
        sa.text("DELETE FROM relevance_judgment_history WHERE organization_id IS NULL")
    )

    # ── 3. relevance_judgments.organization_id → NOT NULL ─────────────────────
    # batch_alter_table 를 사용해 SQLite 에서도 안전하게 NOT NULL 제약을 적용한다.
    # batch 블록 안에서 기존 FK / INDEX / UNIQUE 를 모두 재선언해야 SQLite 테이블
    # 재생성 시 누락되지 않는다.
    with op.batch_alter_table(
        "relevance_judgments",
        schema=None,
    ) as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    # ── 4. relevance_judgment_history.organization_id → NOT NULL ─────────────
    with op.batch_alter_table(
        "relevance_judgment_history",
        schema=None,
    ) as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.Integer(),
            nullable=False,
        )


def downgrade() -> None:
    """organization_id 를 다시 nullable 로 되돌린다.

    주의: 삭제된 개인 판정 row 는 복구되지 않는다. downgrade 는 스키마만 원래대로
    되돌리며, 데이터 복구는 별도 작업이 필요하다.
    """

    # ── 1. relevance_judgment_history.organization_id → nullable ─────────────
    with op.batch_alter_table(
        "relevance_judgment_history",
        schema=None,
    ) as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    # ── 2. relevance_judgments.organization_id → nullable ────────────────────
    with op.batch_alter_table(
        "relevance_judgments",
        schema=None,
    ) as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
