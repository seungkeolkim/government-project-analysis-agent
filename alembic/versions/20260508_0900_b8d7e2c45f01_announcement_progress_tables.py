"""task 00097-2: 신규 테이블 announcement_progress / announcement_progress_history 추가 (Phase C — 조직 단위 공고 진행 상태 / 선점).

설계 근거
    docs/progress_org_design.md (00097-1 의 설계 문서). canonical 단위로 조직별
    진행 상태(관심/검토/진행/종료) 를 표명·열람할 수 있도록 두 테이블을 신설한다.
    "한 canonical 에 status='진행' 인 row 가 최대 1 개" 선점 제약은 partial unique
    index 회피 결정(db_portability §3 + Phase B 컨벤션)에 따라 DB 레벨이 아닌
    repository 의 app-level transactional 체크(00097-3 책임)로 보장한다. 따라서
    본 migration 은 (canonical_project_id, organization_id) 단일 UNIQUE 만 둔다.

변경 사항 요약
    1. announcement_progress 테이블 생성:
       - id (PK), canonical_project_id (FK CASCADE NOT NULL INDEX),
         organization_id (FK CASCADE NOT NULL INDEX),
         status (NOT NULL, native_enum=False CHECK in ('관심','검토','진행','종료')),
         note (Text nullable), created_by_user_id (FK SET NULL nullable),
         created_at / updated_at (UTC tz-aware NOT NULL, server_default=CURRENT_TIMESTAMP).
       - UNIQUE (canonical_project_id, organization_id) — 한 조직 = 한 row.
    2. announcement_progress_history 테이블 생성:
       - announcement_progress 와 동일 컬럼 + archived_at (NOT NULL, server_default)
         + archive_reason (NOT NULL, native_enum=False CHECK in
         ('user_changed','content_changed')).
       - UNIQUE 없음 (이력 누적). canonical_project_id / organization_id INDEX.

기존 row 영향
    신규 테이블만 생성하므로 기존 데이터 영향 없음. backfill 불필요.

다운그레이드
    두 테이블을 DROP 한다. 진행 상태 데이터·이력은 복구되지 않으며, downgrade 는
    개발/검증 경로에서만 사용을 권장한다.

SQLite ↔ Postgres 이식성 (docs/db_portability.md §1, §3, §4)
    - DateTime(timezone=True) — Postgres TIMESTAMPTZ / SQLite TEXT 양쪽 호환.
    - native_enum=False — Postgres 에서 별도 ENUM 타입을 만들지 않고 CHECK
      constraint 만 추가 (SQLite 도 동일).
    - 모든 FK / UNIQUE / INDEX / CHECK constraint 이름 명시.
    - server_default=sa.text(\"CURRENT_TIMESTAMP\") — SQLite·Postgres 모두 지원하는
      표준 표현. SQLite 는 naive UTC string 으로 들어가지만 ORM 의 as_utc 헬퍼가
      비교 시 정규화한다.
    - render_as_batch=True 가 env.py 에 켜져 있으나, create_table 자체는 ALTER 가
      아니라 batch 가 필요 없다. drop_table 도 마찬가지.

Revision ID: b8d7e2c45f01
Revises: c2d3e4f5a6b7
Create Date: 2026-05-08 09:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "b8d7e2c45f01"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """announcement_progress 와 announcement_progress_history 두 테이블을 생성한다.

    실행 순서:
        1. announcement_progress (현재 유효 row).
        2. announcement_progress_history (이관된 과거 row).
    두 테이블 사이에는 FK 의존성이 없으므로 순서는 무관하지만, 가독성을 위해
    "현재 → 이력" 순서로 만든다.
    """

    # ── 1. announcement_progress ──────────────────────────────────────────────
    # canonical 단위 조직별 현재 유효 진행 상태. UNIQUE (canonical, organization)
    # 으로 한 조직이 한 canonical 에 row 1 개만 가진다.
    op.create_table(
        "announcement_progress",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # canonical_project FK — 그룹이 사라지면 소속 진행 상태도 함께 제거.
        sa.Column("canonical_project_id", sa.Integer(), nullable=False),
        # organization FK — 조직이 사라지면 그 조직 입장의 진행 상태도 함께 제거.
        sa.Column("organization_id", sa.Integer(), nullable=False),
        # status: 4 단계 한글 enum. native_enum=False 로 SQLite·Postgres 양쪽에서
        # CHECK constraint 만 추가 (Postgres ENUM 타입 미생성).
        sa.Column(
            "status",
            sa.Enum(
                "관심",
                "검토",
                "진행",
                "종료",
                name="announcement_progress_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        # created_by_user_id: "마지막 수정자" 메타. 권한 판정에는 사용하지 않는다
        # (조직 멤버 누구나 수정·삭제 가능 정책 — Phase B 와 의도적으로 다름).
        # 사용자 탈퇴 시 NULL 로 남기고 row 자체는 보존한다.
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        # created_at / updated_at: tz-aware UTC. server_default 는 raw INSERT
        # 호환용 (실제로는 ORM Python default=_utcnow 가 우선 동작).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["canonical_project_id"],
            ["canonical_projects.id"],
            name="fk_announcement_progress_canonical_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_announcement_progress_organization_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_announcement_progress_created_by_user_id",
            ondelete="SET NULL",
        ),
        # 한 조직 = 한 canonical 에 row 1 개. partial unique index (status='진행')
        # 은 사용하지 않으며, 선점 제약은 repository 의 app-level transactional
        # 체크가 보장한다 (docs/progress_org_design.md §5).
        sa.UniqueConstraint(
            "canonical_project_id",
            "organization_id",
            name="uq_announcement_progress_canonical_org",
        ),
        # status enum 값 강제 — SQLAlchemy 2.0 의 sa.Enum(native_enum=False) 는
        # create_constraint 가 기본 False 라 CHECK 를 자동 생성하지 않는다 (기존
        # announcements.status 와 동일). 한글 enum 보존 컨벤션 + DB 레벨 안전망을
        # 위해 명시적으로 CHECK constraint 를 추가한다.
        sa.CheckConstraint(
            "status IN ('관심', '검토', '진행', '종료')",
            name="ck_announcement_progress_status",
        ),
    )

    # canonical_project_id INDEX — 목록 페이지에서 canonical 단위 요약 조회에 사용.
    op.create_index(
        "ix_announcement_progress_canonical_id",
        "announcement_progress",
        ["canonical_project_id"],
    )
    # organization_id INDEX — 조직별 진행 row 조회 + FK CASCADE 매칭 효율.
    op.create_index(
        "ix_announcement_progress_organization_id",
        "announcement_progress",
        ["organization_id"],
    )

    # ── 2. announcement_progress_history ──────────────────────────────────────
    # 이관된 과거 row. 사용자 변경(user_changed) 또는 canonical 내용 변경 감지
    # (content_changed, Phase 1a §9) 시 announcement_progress 의 row 가 이 테이블로
    # 복사된다. UNIQUE 없음 — 같은 (canonical, organization) 조합으로 시간 순 누적.
    op.create_table(
        "announcement_progress_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("canonical_project_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "관심",
                "검토",
                "진행",
                "종료",
                name="announcement_progress_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        # created_at / updated_at: 이관 시점에는 원본 값을 그대로 복사한다
        # (server_default 는 raw INSERT fallback 용 — 실제 호출 경로에서는 항상
        # 명시 값이 들어간다).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # archived_at: history 로 이관된 시각 (UTC). server_default 로 raw INSERT
        # 호환 + ORM Python default=_utcnow 가 우선 동작.
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # archive_reason: 'user_changed' = 사용자가 status/note 를 바꿈,
        # 'content_changed' = canonical 비교 4 필드 변경 감지로 일괄 reset.
        sa.Column(
            "archive_reason",
            sa.Enum(
                "user_changed",
                "content_changed",
                name="announcement_progress_archive_reason",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["canonical_project_id"],
            ["canonical_projects.id"],
            name="fk_announcement_progress_history_canonical_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_announcement_progress_history_organization_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_announcement_progress_history_created_by_user_id",
            ondelete="SET NULL",
        ),
        # status enum 강제 — announcement_progress 와 동일하지만 Postgres 에서
        # 같은 schema 안에 동명 CHECK constraint 가 다수 존재하지 않도록 별도
        # 이름을 부여한다.
        sa.CheckConstraint(
            "status IN ('관심', '검토', '진행', '종료')",
            name="ck_announcement_progress_history_status",
        ),
        # archive_reason enum 강제.
        sa.CheckConstraint(
            "archive_reason IN ('user_changed', 'content_changed')",
            name="ck_announcement_progress_history_archive_reason",
        ),
    )

    op.create_index(
        "ix_announcement_progress_history_canonical_id",
        "announcement_progress_history",
        ["canonical_project_id"],
    )
    op.create_index(
        "ix_announcement_progress_history_organization_id",
        "announcement_progress_history",
        ["organization_id"],
    )


def downgrade() -> None:
    """announcement_progress_history 와 announcement_progress 를 삭제한다.

    실행 순서:
        1. announcement_progress_history (인덱스 → 테이블).
        2. announcement_progress (인덱스 → 테이블).

    주의: 삭제된 진행 상태와 이관 이력은 복구되지 않는다. downgrade 는 일반적으로
    개발/검증 경로에서만 사용한다.
    """

    # ── 1. announcement_progress_history (인덱스 먼저 삭제) ───────────────────
    op.drop_index(
        "ix_announcement_progress_history_organization_id",
        table_name="announcement_progress_history",
    )
    op.drop_index(
        "ix_announcement_progress_history_canonical_id",
        table_name="announcement_progress_history",
    )
    op.drop_table("announcement_progress_history")

    # ── 2. announcement_progress (인덱스 먼저 삭제) ───────────────────────────
    op.drop_index(
        "ix_announcement_progress_organization_id",
        table_name="announcement_progress",
    )
    op.drop_index(
        "ix_announcement_progress_canonical_id",
        table_name="announcement_progress",
    )
    op.drop_table("announcement_progress")
