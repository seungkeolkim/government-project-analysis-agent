"""task 00157-1: 스케줄 SSOT 전용 테이블 ``scheduled_jobs`` 신설 + 데이터 이관.

설계 근거
    task 155·156 을 거치며 스케줄 SSOT 가 ``system_settings`` JSON 키
    (``scheduler.general_schedules`` 등)와 기동 시 설치되는 OS crontab(외부 파일)로
    이원화돼, 관리자 직관성·백업 편의가 떨어졌다(백업 시 cron 파일을 따로 챙겨야 함).

    이 마이그레이션은 모든 스케줄 트리거(공고 수집·백업·Daily Report·GC)를 단일
    관계형 테이블 ``scheduled_jobs`` 로 모아 SSOT 를 DB 한 곳으로 되돌린다. 기동 시
    이 테이블만 읽어 crontab 을 재생성하므로(소비자 전환은 00157-2), 외부 cron 파일을
    별도로 백업·휴대할 필요가 없어진다.

    주의: 155/156 에서 drop 된 APScheduler jobstore 테이블명은 ``scheduler_jobs``(r)
    였고, 본 SSOT 테이블은 task 제목대로 ``scheduled_jobs``(d) 다. 철자·의미가 모두
    달라 충돌하지 않으며, pickle/job_state 같은 APScheduler 잔재를 되살리지 않는
    순수 도메인 테이블이다.

데이터 이관(멱등)
    테이블 CREATE 직후
    :func:`app.scheduler.scheduled_job_migration.migrate_system_settings_to_scheduled_jobs`
    가 ``system_settings`` 의 일반 수집/백업/Daily Report 스케줄을 row 로 무손실
    이관하고, backup/daily_report/gc 싱글턴 기본 시드를 보장한다. 메일/백업의
    비-스케줄 설정(SMTP·max_count·수신자 등)은 ``system_settings`` 에 그대로 둔다.

순서 보장 (down_revision)
    직전 head 인 ``b7c8d9e0f1a2``(레거시 scheduler_jobs 드롭) 위에 단일 선형
    리비전으로 얹는다. 브랜치를 만들지 않아 ``alembic heads`` 가 하나만 나온다.

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-06-05 05:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.scheduler.constants import SCHEDULED_JOBS_TABLENAME
from app.scheduler.scheduled_job_migration import (
    migrate_system_settings_to_scheduled_jobs,
)

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME: str = "ix_scheduled_jobs_job_kind"


def upgrade() -> None:
    """``scheduled_jobs`` 테이블을 생성하고 기존 스케줄 트리거를 이관/시드한다."""
    op.create_table(
        SCHEDULED_JOBS_TABLENAME,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_kind", sa.String(length=32), nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False),
        sa.Column("cron_expression", sa.Text(), nullable=True),
        sa.Column("interval_hours", sa.Integer(), nullable=True),
        sa.Column("active_sources", sa.JSON(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        _INDEX_NAME, SCHEDULED_JOBS_TABLENAME, ["job_kind"], unique=False
    )

    # CREATE 직후 멱등 데이터 이관 + 싱글턴 기본 시드(빈 system_settings 도 안전).
    migrate_system_settings_to_scheduled_jobs(op.get_bind())


def downgrade() -> None:
    """``scheduled_jobs`` 테이블과 인덱스를 제거한다(대칭 역연산)."""
    op.drop_index(_INDEX_NAME, table_name=SCHEDULED_JOBS_TABLENAME)
    op.drop_table(SCHEDULED_JOBS_TABLENAME)
