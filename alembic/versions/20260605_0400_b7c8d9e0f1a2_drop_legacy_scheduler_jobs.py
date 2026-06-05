"""task 00156-2: 고아(orphan) 레거시 ``scheduler_jobs`` 테이블 드롭.

설계 근거
    task 155 가 일반 공고 수집 스케줄의 SSOT 를 레거시 APScheduler jobstore
    테이블(``scheduler_jobs``)에서 신규 SystemSetting JSON 키
    (``scheduler.general_schedules``)로 옮겼고, 155-4(commit f88a45e)가
    ``app/scheduler/jobstore.py``(JsonSchedulerJobStore)와 APScheduler 자체를
    제거했다. 그 결과 ``scheduler_jobs`` 테이블은 **어떤 런타임 코드도 더 이상
    읽거나 쓰지 않는 고아 테이블**이 되었다.

    이 고아 테이블이 남아 있으면, 운영자가 ``sqlite3`` 로 DB 를 들여다볼 때
    여전히 ``0 1 * * *`` / ``0 13 * * *`` 같은 옛 row 가 보여 '화면엔 없는데
    DB 엔 있다'는 정합성 혼선을 반복해서 일으킨다(사용자 원문 보고의 핵심
    증상). 00156-1 백필이 그 row 들의 스케줄을 신규 SSOT 로 이미 복구했으므로,
    이제 안전하게 테이블을 제거해 'DB = SystemSetting 단일 SSOT' 상태로
    정리한다.

순서 보장 (down_revision)
    반드시 00156-1 백필 마이그레이션(``e1a2b3c4d5f6``) 뒤에 와야 한다. 백필이
    먼저 ``scheduler_jobs`` 를 읽어 신규 저장소로 복구한 **다음에** 본 드롭이
    실행되어야 유실이 발생하지 않는다. 그래서 ``down_revision`` 을 백필
    리비전으로 둬 리니어 체인(... → e1a2b3c4d5f6 → b7c8d9e0f1a2)을 만든다.

멱등성
    - 테이블이 존재할 때만 드롭한다(inspector 로 확인). 신규/빈 DB 처럼 테이블이
      애초에 없으면 no-op 으로 통과한다.
    - ``alembic upgrade head`` 재실행은 alembic_version 이 이미 head 이므로
      본 마이그레이션을 다시 돌리지 않는다(전체 흐름도 멱등).

Revision ID: b7c8d9e0f1a2
Revises: e1a2b3c4d5f6
Create Date: 2026-06-05 04:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

from app.scheduler.constants import SCHEDULER_JOBS_TABLENAME

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "e1a2b3c4d5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """고아 ``scheduler_jobs`` 테이블을 (존재할 때만) 드롭한다.

    SQLite·PostgreSQL 모두 ``DROP TABLE`` 이 그 테이블에 딸린 인덱스
    (``ix_scheduler_jobs_next_run_time`` / ``ix_scheduler_jobs_trigger_type``)를
    자동으로 함께 제거하므로 인덱스를 따로 드롭하지 않는다.
    """
    inspector = inspect(op.get_bind())
    if SCHEDULER_JOBS_TABLENAME in inspector.get_table_names():
        op.drop_table(SCHEDULER_JOBS_TABLENAME)


def downgrade() -> None:
    """다운그레이드는 의도적으로 no-op 이다.

    이 테이블은 더 이상 어떤 코드도 사용하지 않는 레거시 jobstore 잔재이고,
    그 안에 있던 스케줄 데이터는 00156-1 백필로 신규 SSOT 에 이미 복구됐다.
    따라서 되돌릴 때 빈 고아 테이블을 다시 만들 실익이 없어 재생성하지 않는다.
    """
    pass
