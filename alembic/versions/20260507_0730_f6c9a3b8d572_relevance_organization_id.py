"""task 00085-2: RelevanceJudgment / RelevanceJudgmentHistory 에 organization_id 추가 및 단일 UNIQUE 적용

설계 근거
    docs/relevance_org_design.md (00085-1 의 설계 문서). 사용자 modify 턴 (2026-05-07T07:13)
    이 사전 확정한 결정 1 — UNIQUE 안 1 (단일 UNIQUE) 채택. partial unique index 사용 안 함.
    organization_id 의 의미는 "이 사용자가 어떤 조직 입장으로 한 판정인지" 라는 메타 정보.
    NULL 이면 개인 판정, 정수면 그 조직 입장의 판정.

변경 사항 요약
    1. relevance_judgments 테이블 변경:
       - organization_id (Integer, nullable) 컬럼 추가
       - FK fk_relevance_judgments_organization_id → organizations.id ON DELETE CASCADE 추가
         (조직 삭제 시 그 조직 입장으로 한 판정도 함께 사라짐 — user_organizations CASCADE 와 정합)
       - 기존 UNIQUE uq_relevance_project_user (canonical_project_id, user_id) 제거
       - 신규 UNIQUE uq_relevance_judgments_canonical_user_org
         (canonical_project_id, user_id, organization_id) 추가
       - INDEX ix_relevance_judgments_organization_id 추가 (조직별 판정 조회 / FK CASCADE 효율)
    2. relevance_judgment_history 테이블 변경:
       - organization_id (Integer, nullable) 컬럼 추가
       - FK fk_relevance_judgment_history_organization_id → organizations.id ON DELETE CASCADE 추가
       - INDEX ix_relevance_judgment_history_organization_id 추가
       - History 의 UNIQUE 는 원래 없으므로 제약 변경 없음.

기존 row 호환성
    migration 직후 기존 row 의 organization_id 는 NULL 로 자연 보존 — 모든 기존 판정은
    "본인 명의 (개인) 판정" 의 의미를 유지한다. 신규 UNIQUE
    (canonical_project_id, user_id, organization_id) 은 기존 (canonical, user) 1 행 보장
    상태에서 NULL 까지 포함해 즉시 만족된다. backfill / 데이터 변환 / 추가 SQL 불필요.

NULL UNIQUE semantics 주의
    SQLite·Postgres 모두 UNIQUE 제약에서 NULL 은 "서로 다름" 으로 취급된다. 따라서 신규
    UNIQUE (canonical, user, organization_id) 는 organization_id IS NULL 인 row 두 개를
    이론상 막지 못한다. 사용자 modify 턴이 이 단순화를 명시적으로 채택했고 (partial
    unique index 의 dialect 차이 회피), "사용자별 개인 판정 1 개" 보장은 repository 의
    set_relevance_judgment 가 기존 row 를 History 이관 후 교체하는 흐름으로 보장한다
    (00085-3 시그니처 확장에서 organization_id 키도 함께 매칭하도록 보강).

SQLite ↔ Postgres 이식성 (docs/db_portability.md §4)
    - 모든 DDL 변경은 op.batch_alter_table 로 감싼다 (env.py 가 render_as_batch=True 이지만
      drop_constraint / create_unique_constraint 가 ALTER TABLE 단독 실행되지 않도록 명시).
    - 모든 constraint / index 이름을 명시한다.
    - upgrade() 와 downgrade() 양방향 구현. downgrade 는 신규 UNIQUE 를 제거하고 기존
      uq_relevance_project_user 를 복원한다 — 단, 다운그레이드 시점에 기존 (canonical, user)
      에 대해 organization_id 가 다른 row 가 여러 개 존재하면 UNIQUE 복원이 실패할 수
      있다. 이 경우 운영자가 데이터 정리 후 재시도해야 한다 (downgrade 는 일반적으로
      개발/검증 경로에서만 사용).
    - partial unique index 는 사용하지 않으므로 dialect 차이 검증 부담 없음.

Revision ID: f6c9a3b8d572
Revises: e5b8f2a9c471
Create Date: 2026-05-07 07:30:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "f6c9a3b8d572"
down_revision: Union[str, None] = "e5b8f2a9c471"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """relevance_judgments 와 relevance_judgment_history 두 테이블에 organization_id
    컬럼 / FK / INDEX 를 추가하고, relevance_judgments 의 UNIQUE 를 단일 키로 교체한다.

    실행 순서:
        1. relevance_judgments 변경 (컬럼 추가 → 기존 UNIQUE 제거 → 신규 UNIQUE 추가
           → FK 추가 → INDEX 추가). 한 batch 블록에서 처리되어 SQLite 에서는 테이블
           재생성으로 일괄 적용된다.
        2. relevance_judgment_history 변경 (컬럼 추가 → FK 추가 → INDEX 추가).
    """

    # ── 1. relevance_judgments ─────────────────────────────────────────────────
    # 컬럼 추가, 기존 UNIQUE 제거, 신규 UNIQUE 추가, FK 추가, INDEX 추가를
    # 단일 batch_alter_table 안에서 한 번에 적용한다. SQLite 는 테이블 재생성으로
    # 처리되므로 중간 중복 스키마 / UNIQUE 위반 상태가 발생하지 않는다.
    with op.batch_alter_table("relevance_judgments") as batch_op:
        # organization_id 컬럼 추가 (nullable). 기존 row 는 NULL 로 자연 보존.
        batch_op.add_column(
            sa.Column("organization_id", sa.Integer(), nullable=True)
        )

        # 기존 UNIQUE 제거. baseline migration 에서 부여한 이름 그대로 사용한다
        # (uq_relevance_project_user — 짧은 명명이지만 운영 DB 와 호환을 위해 유지).
        batch_op.drop_constraint(
            "uq_relevance_project_user",
            type_="unique",
        )

        # 신규 단일 UNIQUE (canonical_project_id, user_id, organization_id).
        # NULL is distinct 라 organization_id IS NULL row 두 개를 이론상 못 막지만,
        # 실제 보장은 repository 의 set_relevance_judgment 트랜잭션이 담당한다.
        batch_op.create_unique_constraint(
            "uq_relevance_judgments_canonical_user_org",
            ["canonical_project_id", "user_id", "organization_id"],
        )

        # organizations.id 참조 FK. 조직 삭제 시 그 조직 입장의 판정도 CASCADE.
        batch_op.create_foreign_key(
            "fk_relevance_judgments_organization_id",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )

        # 조직별 판정 조회 + FK CASCADE 시 organizations.id 매칭 효율을 위한 INDEX.
        batch_op.create_index(
            "ix_relevance_judgments_organization_id",
            ["organization_id"],
        )

    # ── 2. relevance_judgment_history ──────────────────────────────────────────
    # History 도 organization_id 를 함께 보존해야 한다 — content_changed reset 시
    # row 단위로 모든 컬럼이 그대로 복사되는 패턴이라 컬럼만 추가하면 자동 동작.
    with op.batch_alter_table("relevance_judgment_history") as batch_op:
        batch_op.add_column(
            sa.Column("organization_id", sa.Integer(), nullable=True)
        )

        batch_op.create_foreign_key(
            "fk_relevance_judgment_history_organization_id",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )

        batch_op.create_index(
            "ix_relevance_judgment_history_organization_id",
            ["organization_id"],
        )


def downgrade() -> None:
    """upgrade 를 역순으로 되돌린다.

    실행 순서:
        1. relevance_judgment_history 에서 INDEX → FK → 컬럼 순으로 제거.
        2. relevance_judgments 에서 INDEX → FK → 신규 UNIQUE → 컬럼 제거 후
           기존 UNIQUE (canonical_project_id, user_id) 를 복원한다.

    주의:
        다운그레이드 시점에 동일 (canonical_project_id, user_id) 에 대해
        organization_id 만 다른 row 가 여럿 존재한다면 기존 UNIQUE 복원이 실패한다.
        이 경우 운영자가 데이터 정리 후 재시도해야 한다 (downgrade 는 일반적으로
        개발/검증 경로에서만 사용).
    """

    # ── 1. relevance_judgment_history 역변경 ──────────────────────────────────
    with op.batch_alter_table("relevance_judgment_history") as batch_op:
        batch_op.drop_index("ix_relevance_judgment_history_organization_id")
        batch_op.drop_constraint(
            "fk_relevance_judgment_history_organization_id",
            type_="foreignkey",
        )
        batch_op.drop_column("organization_id")

    # ── 2. relevance_judgments 역변경 ─────────────────────────────────────────
    with op.batch_alter_table("relevance_judgments") as batch_op:
        batch_op.drop_index("ix_relevance_judgments_organization_id")
        batch_op.drop_constraint(
            "fk_relevance_judgments_organization_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_relevance_judgments_canonical_user_org",
            type_="unique",
        )
        batch_op.drop_column("organization_id")

        # 기존 UNIQUE 복원 — 이름은 baseline 그대로 uq_relevance_project_user.
        batch_op.create_unique_constraint(
            "uq_relevance_project_user",
            ["canonical_project_id", "user_id"],
        )
