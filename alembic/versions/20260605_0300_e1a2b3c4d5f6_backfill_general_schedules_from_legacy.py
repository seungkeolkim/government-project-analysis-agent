"""task 00156-1: 레거시 scheduler_jobs 일반 수집 스케줄을 신규 SSOT 로 멱등 백필.

설계 근거
    task 155 가 일반 공고 수집 스케줄의 SSOT 를 레거시 APScheduler jobstore
    테이블(``scheduler_jobs``)에서 신규 SystemSetting JSON 키
    (``scheduler.general_schedules``)로 옮겼다. 그러나 155-4(commit f88a45e)가
    jobstore.py/APScheduler 를 제거하면서 그 테이블에만 있던 기존 일반 수집
    스케줄(예: ``0 1 * * *``, ``0 13 * * *``)을 신규 저장소로 옮기지 않아,
    사용자 화면·crontab 에서 '소리없이 유실' 되고 ``scheduler_jobs`` 에는
    유령 row 로 남는 정합성 오류가 발생했다.

    이 마이그레이션은 그 유실분을 신규 SSOT 로 **1회 멱등 백필**해 복구한다.
    entrypoint 가 기동 시 ``alembic upgrade head`` 를 먼저 수행한 뒤 crontab 을
    재생성하므로, 여기서 신규 저장소를 채우면 직후 crontab 재생성이 복구된
    스케줄을 반영한다. 실제 변환/멱등 로직은
    :func:`app.scheduler.legacy_backfill.backfill_general_schedules_from_legacy`
    가 담당한다(단위 테스트 가능).

백업 / Daily Report 는 별도 백필 불필요
    백업(``backup.cron_expression``)과 Daily Report(``email.daily_report.*``)는
    본래부터 SystemSetting 이 SSOT 이고,
    :func:`app.scheduler.crontab_generator.collect_system_jobs` 가 그
    SystemSetting 만 읽는다. 즉 task 155 전환에서도 유실이 없었으며, 레거시
    ``scheduler_jobs`` 의 backup-db/daily-report row 는 단순 미러일 뿐이라
    여기서 백필하지 않는다(백필 함수가 ``cron:`` / ``interval:`` name 잡만
    대상으로 삼아 backup-cron:/daily-report-cron:/gc-orphan-cron: 을 제외한다).

멱등성
    - ``scheduler_jobs`` 테이블이 없으면 no-op.
    - dedupe 키 (mode, cron_expression/interval_hours, sorted active_sources)로
      이미 존재하는 레코드(사용자가 추가한 ``15 11 * * *`` 등)는 다시 넣지 않음.
    - 두 번 실행해도 두 번째는 0건 추가.

Revision ID: e1a2b3c4d5f6
Revises: d6b2e8c1a9f7
Create Date: 2026-06-05 03:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.scheduler.legacy_backfill import backfill_general_schedules_from_legacy

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "e1a2b3c4d5f6"
down_revision: Union[str, None] = "d6b2e8c1a9f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """레거시 scheduler_jobs 의 일반 수집 스케줄을 신규 SSOT 로 백필한다.

    실제 로직은 ``backfill_general_schedules_from_legacy`` 가 담당하며, 이
    함수는 테이블 부재/중복을 모두 멱등하게 처리한다(테이블이 없으면 no-op).
    """
    backfill_general_schedules_from_legacy(op.get_bind())


def downgrade() -> None:
    """데이터 백필이라 안전한 downgrade 가 없다 — 의도적으로 no-op.

    백필로 복구된 스케줄은 사용자가 실제로 사용하는 운영 데이터이므로,
    downgrade 에서 신규 저장소를 임의로 비우지 않는다(원본인 레거시 row 가
    어느 것이었는지 식별자를 보존하지 않으므로 정확한 역연산도 불가능하다).
    """
    pass
