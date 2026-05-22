"""task 00131-2: 신규 테이블 scheduler_job_claims 추가 (스케줄 job single-flight 가드).

설계 근거
    task 00131 — cron 중복 실행 버그 수정. subtask 00131-1 이 flock 으로
    '컨테이너당 스케줄러 인스턴스 1개' 를 보장했고, 본 subtask 00131-2 는
    그 위에 defense-in-depth 로 'job 의 부수효과는 동일 스케줄 주기에 1회만'
    을 강제하는 job-level single-flight 가드를 추가한다.

    가드는 ``(job_name, slot_key)`` UNIQUE 제약을 이용한 claim 방식이다.
    동일 trigger 시각에 거의 동시에 발사된 2~3개의 job 호출은 모두 같은
    ``slot_key`` (시각을 고정 시간창으로 내림한 버킷) 를 계산하고, 본 테이블에
    row 를 INSERT 한다. 먼저 성공한 단 한 호출만 claim 을 얻고 나머지는
    IntegrityError 로 거절돼 job 을 건너뛴다. SQLite 단일 writer 환경에서
    원자적이다 (가드 로직은 ``app/scheduler/job_guard.py``).

변경 사항 요약
    1. ``scheduler_job_claims`` 테이블 생성 (PK: id AUTOINCREMENT).
       - ``job_name`` (String(64)) — 가드 대상 job 의 논리적 이름.
       - ``slot_key`` (String(64)) — 예정 fire-time 을 시간창으로 내림한 버킷 키.
       - ``claimed_at`` (DateTime(timezone=True), NOT NULL, server_default
         CURRENT_TIMESTAMP) — claim 성사 시각, 오래된 row 정리 기준.
       - ``claimed_by_pid`` (Integer, NULL) — claim 을 획득한 프로세스 pid (진단용).
    2. UNIQUE 제약 ``uq_scheduler_job_claims_job_name_slot_key`` —
       (job_name, slot_key) 조합당 1건. single-flight 의 원자성 근거.
    3. 인덱스 ``ix_scheduler_job_claims_claimed_at`` (claimed_at) —
       보관 기간 경과 row 정리(claimed_at < cutoff) 가속용.

기존 row 영향
    신규 테이블만 생성하므로 기존 데이터 영향 없음. backfill 불필요.

다운그레이드
    인덱스 → 테이블 순으로 삭제. claim row 는 휘발성 가드 데이터라 삭제돼도
    운영 이력 손실이 아니다.

SQLite ↔ Postgres 이식성 (docs/db_portability.md §1, §3, §4)
    - ``DateTime(timezone=True)`` — Postgres TIMESTAMPTZ / SQLite TEXT 양쪽
      호환. DB 저장은 UTC tz-aware (PROJECT_NOTES 컨벤션).
    - 모든 UNIQUE / INDEX constraint 이름 명시.
    - ``server_default=sa.text("CURRENT_TIMESTAMP")`` — SQLite/Postgres 모두
      지원. ORM Python default (_utcnow) 가 통상 우선 적용되며 본 server_default
      는 raw INSERT 호환 안전망 (EmailDailyReportRun 동일 패턴).
    - 신규 테이블 추가만 있어 ``batch_alter_table`` 컨텍스트 불필요.

Revision ID: b3d9e1f7c264
Revises: d4e7c8a9b021
Create Date: 2026-05-22 01:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "b3d9e1f7c264"
down_revision: Union[str, None] = "d4e7c8a9b021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """scheduler_job_claims 테이블과 인덱스를 생성한다.

    실행 순서:
        1. ``scheduler_job_claims`` 테이블 생성 (UNIQUE 제약 포함).
        2. ``claimed_at`` 단일 컬럼 인덱스 (오래된 claim row 정리 가속용).
    """

    # ── scheduler_job_claims ─────────────────────────────────────────────────
    # 스케줄 job 의 동일 주기 중복 실행 방지 claim. (job_name, slot_key) 가
    # UNIQUE 라, 동일 trigger 주기에 발사된 2~3개 호출 중 먼저 INSERT 에
    # 성공한 단 한 호출만 claim 을 얻는다.
    op.create_table(
        "scheduler_job_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # 가드 대상 job 의 논리적 이름 (app.scheduler.job_guard 의 JOB_NAME_* 상수).
        sa.Column("job_name", sa.String(64), nullable=False),
        # 예정 fire-time 을 고정 시간창(기본 60초)으로 내림한 버킷 키.
        # 동일 trigger 주기의 중복 호출들이 같은 값을 계산하도록 설계된 결정적 문자열.
        sa.Column("slot_key", sa.String(64), nullable=False),
        # claim 성사 시각 (UTC tz-aware). ORM Python default (_utcnow) 가 통상
        # 우선 적용되며 server_default 는 raw INSERT 호환 안전망.
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # claim 을 획득한 프로세스 pid (진단용, NULL 허용).
        sa.Column("claimed_by_pid", sa.Integer(), nullable=True),
        # ── UNIQUE constraint ────────────────────────────────────────────────
        # single-flight 의 원자성 근거 — (job_name, slot_key) 조합당 1건.
        # 두 번째 호출의 INSERT 는 IntegrityError 로 거절된다.
        sa.UniqueConstraint(
            "job_name",
            "slot_key",
            name="uq_scheduler_job_claims_job_name_slot_key",
        ),
    )

    # ── 인덱스 ───────────────────────────────────────────────────────────────
    # 보관 기간 경과 row 정리 SQL(DELETE WHERE claimed_at < cutoff) 가속용.
    op.create_index(
        "ix_scheduler_job_claims_claimed_at",
        "scheduler_job_claims",
        ["claimed_at"],
    )


def downgrade() -> None:
    """scheduler_job_claims 테이블과 인덱스를 삭제한다.

    실행 순서: 인덱스 → 테이블 순으로 drop. claim row 는 휘발성 가드
    데이터라 삭제돼도 운영 이력 손실이 아니다.
    """

    op.drop_index(
        "ix_scheduler_job_claims_claimed_at",
        table_name="scheduler_job_claims",
    )
    op.drop_table("scheduler_job_claims")
