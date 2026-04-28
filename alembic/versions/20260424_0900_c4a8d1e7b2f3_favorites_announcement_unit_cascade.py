"""task 00037: FavoriteEntry 를 announcement 단위로 전환 + 폴더 cascade 삭제

00036 에서 확정되었던 "FavoriteEntry 는 canonical 단위" 설계를 본 migration 에서
공식 폐기한다. 사용자 원문(#4) — "별표를 누른 그 공고가 반드시 등록됨 / 동일 과제
여러 공고가 즐겨찾기에 모두 보여야 함" — 의도를 충족하기 위해 저장 단위를
``canonical_project_id`` 에서 ``announcement_id`` 로 전환한다.

동시에 사용자 원문(#2) — "그룹 삭제 시 하위 서브그룹 및 공고를 모두 cascade
삭제(격상 없음)" — 를 DB 레벨에서 보장하기 위해 ``favorite_folders.parent_id``
FK 의 ``ondelete`` 를 ``SET NULL`` 에서 ``CASCADE`` 로 변경한다.

변경 사항 요약
    1. favorite_folders.parent_id FK: ondelete SET NULL → CASCADE
       (자식 폴더 격상 없이, 부모 폴더 삭제 시 자식·손자까지 연쇄 삭제된다)
    2. favorite_entries 테이블 재설계:
       - ``canonical_project_id`` (FK → canonical_projects) 제거
       - ``announcement_id`` (FK → announcements, ondelete=CASCADE) 신설
       - UNIQUE 제약: (folder_id, canonical_project_id) → (folder_id, announcement_id)
       - 인덱스: ix_favorite_entries_canonical_project_id
                → ix_favorite_entries_announcement_id

데이터 이관 (upgrade)
    각 기존 FavoriteEntry.canonical_project_id (= CanonicalProject.id) 에 대해
    동일 canonical 그룹(announcements.canonical_group_id 일치) 에서 ``is_current=True``
    인 announcement 중 ``MIN(id)`` 를 대표로 선택하여 ``announcement_id`` 로 이관한다.
    대표 announcement 를 찾지 못한 row (canonical 은 있으나 is_current=True 공고가
    없는 상태) 는 삭제한다 — 사용자 원문 "별표를 누른 그 공고가 반드시 등록됨" 취지상
    대표 공고 없는 canonical 참조는 유지해도 의미가 없다.

데이터 복원 (downgrade)
    역방향으로, 각 FavoriteEntry.announcement_id 에 대해 대응 Announcement 의
    ``canonical_group_id`` 를 ``canonical_project_id`` 로 복원한다. canonical_group_id
    가 NULL 이었던 row 는 복원 경로가 없으므로 삭제한다.

SQLite ↔ Postgres 이식성
    ``docs/db_portability.md §4`` 에 따라 모든 DDL 변경은 ``batch_alter_table`` 로
    감싼다 (render_as_batch=True 가 env.py 에 이미 설정됨). 이관 쿼리의 boolean
    비교는 ``sa.bindparam(..., type_=sa.Boolean())`` 로 값만 주입하여 dialect
    렌더링을 SQLAlchemy 에 맡긴다(SQLite 는 0/1, Postgres 는 t/f 로 자동 변환).

Revision ID: c4a8d1e7b2f3
Revises: b2c5e8f1a934
Create Date: 2026-04-24 09:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "c4a8d1e7b2f3"
down_revision: Union[str, None] = "b2c5e8f1a934"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """폴더 cascade 전환 + FavoriteEntry 를 announcement 단위로 재설계한다.

    실행 순서:
        1. favorite_folders.parent_id FK ondelete: SET NULL → CASCADE
        2. favorite_entries 에 announcement_id (nullable) 컬럼 추가
        3. 기존 canonical_project_id → announcement_id 로 데이터 이관
        4. 이관 실패(대표 is_current announcement 없음) row 삭제
        5. favorite_entries 재구성: canonical_project_id 관련 제약/컬럼 제거,
           announcement_id 에 NOT NULL + FK(CASCADE) + UNIQUE + 인덱스 부여
    """

    # ── 1. favorite_folders.parent_id FK: SET NULL → CASCADE ───────────────────
    # 부모 폴더 삭제 시 자식 폴더 및 그 하위 FavoriteEntry(folder FK 도 이미 CASCADE)
    # 까지 DB 레벨로 연쇄 삭제하여 "격상 없음" 을 보장한다.
    with op.batch_alter_table("favorite_folders") as batch_op:
        batch_op.drop_constraint(
            "fk_favorite_folders_parent_id",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_favorite_folders_parent_id",
            "favorite_folders",
            ["parent_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # ── 2. favorite_entries 에 announcement_id (nullable) 컬럼 추가 ────────────
    # 이관 전이라 기존 row 에는 값이 없으므로 일단 nullable 로 둔다.
    # FK / UNIQUE / 인덱스는 이관 + orphan 삭제 이후 Step 5 에서 한 번에 부여한다.
    with op.batch_alter_table("favorite_entries") as batch_op:
        batch_op.add_column(
            sa.Column("announcement_id", sa.Integer(), nullable=True)
        )

    # ── 3. 데이터 이관: canonical_project_id → 대표 announcement.id ─────────────
    # 대표 announcement = announcements.canonical_group_id = fe.canonical_project_id
    #                     AND is_current = True 중 MIN(id).
    # correlated subquery — SQLite / Postgres 모두 지원.
    op.execute(
        sa.text(
            "UPDATE favorite_entries SET announcement_id = ("
            "SELECT MIN(a.id) FROM announcements a "
            "WHERE a.canonical_group_id = favorite_entries.canonical_project_id "
            "AND a.is_current = :is_current_true)"
        ).bindparams(
            sa.bindparam("is_current_true", True, sa.Boolean())
        )
    )

    # ── 4. 대응 announcement 가 없는 고아 FavoriteEntry 삭제 ────────────────────
    # 대표 공고가 없는 canonical 참조는 전환 이후 재현 불가 + 사용자 의도상 무의미.
    op.execute(
        sa.text(
            "DELETE FROM favorite_entries WHERE announcement_id IS NULL"
        )
    )

    # ── 5. favorite_entries 재구성 ─────────────────────────────────────────────
    # 하나의 batch 블록에서 기존 canonical 관련 제약/컬럼 제거 + 신규 FK/UNIQUE/
    # 인덱스 부여 + announcement_id NOT NULL 승격 을 함께 수행한다.
    # SQLite 는 테이블 재생성으로 처리되므로 중간 중복 스키마 상태가 발생하지 않는다.
    with op.batch_alter_table("favorite_entries") as batch_op:
        # 기존 인덱스·제약·컬럼 제거
        batch_op.drop_index("ix_favorite_entries_canonical_project_id")
        batch_op.drop_constraint(
            "uq_favorite_entries_folder_canonical",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_favorite_entries_canonical_project_id",
            type_="foreignkey",
        )
        batch_op.drop_column("canonical_project_id")

        # announcement_id NOT NULL 승격 (Step 4 에서 NULL row 모두 제거됨)
        batch_op.alter_column(
            "announcement_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

        # 신규 FK / UNIQUE / 인덱스
        batch_op.create_foreign_key(
            "fk_favorite_entries_announcement_id",
            "announcements",
            ["announcement_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_favorite_entries_folder_announcement",
            ["folder_id", "announcement_id"],
        )
        batch_op.create_index(
            "ix_favorite_entries_announcement_id",
            ["announcement_id"],
        )


def downgrade() -> None:
    """upgrade 를 역방향으로 되돌린다.

    실행 순서:
        1. favorite_entries 에 canonical_project_id (nullable) 컬럼 추가
        2. announcement.canonical_group_id 를 favorite_entries.canonical_project_id
           로 복원
        3. canonical_group_id 가 NULL 이었던 row 는 복원 불가 → 삭제
        4. favorite_entries 재구성: announcement_id 관련 제약/컬럼 제거,
           canonical_project_id 에 NOT NULL + FK(CASCADE) + UNIQUE + 인덱스 복원
        5. favorite_folders.parent_id FK ondelete: CASCADE → SET NULL 복원
    """

    # ── 1. canonical_project_id (nullable) 컬럼 추가 ───────────────────────────
    with op.batch_alter_table("favorite_entries") as batch_op:
        batch_op.add_column(
            sa.Column("canonical_project_id", sa.Integer(), nullable=True)
        )

    # ── 2. announcement.canonical_group_id 로 backfill ─────────────────────────
    op.execute(
        sa.text(
            "UPDATE favorite_entries SET canonical_project_id = ("
            "SELECT a.canonical_group_id FROM announcements a "
            "WHERE a.id = favorite_entries.announcement_id)"
        )
    )

    # ── 3. canonical_group_id 가 NULL 인 공고를 참조하던 row 는 삭제 ────────────
    # 대응 canonical 이 없어 복원 불가 — UNIQUE(folder_id, canonical_project_id)
    # NOT NULL 제약으로 진입할 수 없으므로 먼저 제거한다.
    op.execute(
        sa.text(
            "DELETE FROM favorite_entries WHERE canonical_project_id IS NULL"
        )
    )

    # ── 4. favorite_entries 재구성 (announcement_id → canonical_project_id) ───
    with op.batch_alter_table("favorite_entries") as batch_op:
        batch_op.drop_index("ix_favorite_entries_announcement_id")
        batch_op.drop_constraint(
            "uq_favorite_entries_folder_announcement",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_favorite_entries_announcement_id",
            type_="foreignkey",
        )
        batch_op.drop_column("announcement_id")

        batch_op.alter_column(
            "canonical_project_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

        batch_op.create_foreign_key(
            "fk_favorite_entries_canonical_project_id",
            "canonical_projects",
            ["canonical_project_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_favorite_entries_folder_canonical",
            ["folder_id", "canonical_project_id"],
        )
        batch_op.create_index(
            "ix_favorite_entries_canonical_project_id",
            ["canonical_project_id"],
        )

    # ── 5. favorite_folders.parent_id FK: CASCADE → SET NULL 복원 ─────────────
    with op.batch_alter_table("favorite_folders") as batch_op:
        batch_op.drop_constraint(
            "fk_favorite_folders_parent_id",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_favorite_folders_parent_id",
            "favorite_folders",
            ["parent_id"],
            ["id"],
            ondelete="SET NULL",
        )
