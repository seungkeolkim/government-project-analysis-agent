"""task 00150-1: scrape_snapshots 를 매 ScrapeRun 마다 1 row INSERT 로 전환한다.

설계 근거
    사용자 원문 task 00150 — "snapshot 과 payload 는 scrape 실행시 하루치를
    계속 업데이트 하는게 아니라, 매 scrape 실행마다 새로이 만들어져야 한다.
    따라서 updated 는 의미가 없다."

    기존 설계는 UNIQUE(snapshot_date) 로 KST 날짜당 1 row 만 유지하고, 같은
    날의 후속 ScrapeRun 종료 시 ``merge_snapshot_payload`` 로 payload 를
    누적 머지하는 UPSERT 방식이었다. 이 방식 때문에 row 의 ``created_at`` 이
    그 KST 일자의 첫 ScrapeRun 종료 시각(예: 09:00)에 고정되어, daily report
    의 ``(last_sent_at, now]`` 시간 윈도우 필터가 같은 날 늦은 시각(예: 16:02)
    에 종료된 ScrapeRun 의 diff 결과를 0건으로 잡는 회귀가 발생했다 (사용자가
    보고한 10번 메일 / 14:40~16:10 윈도우 케이스).

    본 마이그레이션은 다음을 적용한다.

    1. ``scrape_snapshots`` 의 UNIQUE(``snapshot_date``) 제약 제거 — 같은 KST
       날짜에 여러 row 존재 허용.
    2. ``scrape_run_id INTEGER NOT NULL`` 컬럼 + ``scrape_runs.id`` FK
       (``ON DELETE CASCADE``) 추가 + UNIQUE(``scrape_run_id``) — 1 ScrapeRun
       = 1 snapshot row 보장. 또한 같은 ScrapeRun 에 대해 snapshot 이 중복
       INSERT 되는 회귀를 DB 가 막는다.
    3. ``snapshot_date`` 단순 인덱스 추가 — UNIQUE 가 제공하던 implicit
       인덱스를 잃지 않도록 캘린더/dashboard 의 KST 날짜 조회 인덱스 유지.

기존 row 백필
    기존 ``scrape_snapshots`` row 는 ``scrape_run_id`` 가 없다 (구 스키마에
    컬럼 자체가 없음). 본 마이그레이션은 동일 ``snapshot_date`` 의 가장 최근
    completed/partial ``ScrapeRun`` 의 id 를 매핑한다. 매핑 후보가 없는 row
    (그날 ScrapeRun 이 모두 failed/cancelled 였거나 ScrapeRun 자체가 0건)는
    DELETE 한다 — 어차피 dashboard 캘린더 가용 set 의 KST 날짜 인덱스가 끊긴
    "고아 snapshot" 이라 손실 위험 없음.

    백필은 본 migration 의 ``op.execute(sa.text(...))`` 로 수행하며 SQLite /
    Postgres 양쪽 호환되는 표현만 사용한다.

다운그레이드
    UNIQUE(``snapshot_date``) 를 다시 추가하고 ``scrape_run_id`` 컬럼 / FK /
    UNIQUE / 인덱스를 모두 제거한다. 같은 KST 날짜에 2 row 이상 누적된
    데이터는 downgrade 시 UNIQUE 위반으로 실패할 수 있다 — 이 경우 수동으로
    중복 row 를 정리한 뒤 downgrade 를 재실행해야 한다 (downgrade 는 개발 /
    검증 경로 전용).

SQLite ↔ Postgres 이식성 (docs/db_portability.md §1, §3, §4)
    - 모든 DDL 변경은 ``op.batch_alter_table`` 컨텍스트에서 수행 — SQLite 가
      ALTER TABLE DROP CONSTRAINT / ALTER COLUMN NULLABLE / ADD FK 를
      지원하지 않으므로 batch 안에서 테이블 재생성 방식으로 처리된다.
    - ``DateTime(timezone=True)`` 컬럼 / ``sa.JSON()`` 등 기존 컬럼은 건드리지
      않는다 — 신규 컬럼 / 제약만 추가·제거한다.
    - 모든 신규 constraint / index 에 이름을 명시한다.
    - 백필 SQL 은 dialect 중립 표현 (correlated subquery + IS NULL / IN) 만
      사용한다.

검증 절차 (docs/db_portability.md §4 3단계)
    1. 기존 운영 DB 사본에 신규 migration 적용 — 백필 + UNIQUE 제거 +
       scrape_run_id NOT NULL 완료 확인.
    2. 빈 SQLite 에 alembic upgrade head — baseline 부터 head 까지 통과.
    3. Postgres syntax 호환 정적 검토 — DateTime(timezone=True), 이름 명시
       constraint, dialect 비의존 SQL 확인.

Revision ID: d6b2e8c1a9f7
Revises: c5a8d1e7b9f4
Create Date: 2026-05-26 08:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "d6b2e8c1a9f7"
down_revision: Union[str, None] = "c5a8d1e7b9f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """scrape_snapshots 를 매 ScrapeRun 마다 1 row INSERT 로 전환한다.

    실행 순서:
        1. ``scrape_run_id INTEGER`` 를 nullable 로 추가 (batch 밖) — 백필 SQL
           이 값을 채울 수 있는 임시 상태.
        2. 백필: 동일 ``snapshot_date`` 의 가장 최근 completed/partial
           ScrapeRun.id 로 ``scrape_run_id`` 를 UPDATE.
        3. 같은 ScrapeRun.id 가 여러 snapshot row 에 매핑되는 경우 (이론상
           발생 가능 — 백필 매핑 키가 snapshot_date 단위) 의 중복 row 는 가장
           작은 id 1개만 남기고 나머지 DELETE — UNIQUE(scrape_run_id) 위반
           방지.
        4. 백필 불가능한 row (scrape_run_id IS NULL — 매칭 ScrapeRun 0건)
           DELETE.
        5. batch_alter_table 안에서 한 번에:
              - UNIQUE(snapshot_date) 드롭
              - scrape_run_id NOT NULL 승격
              - FK(scrape_runs.id, ON DELETE CASCADE) 추가
              - UNIQUE(scrape_run_id) 추가
              - ix_scrape_snapshots_scrape_run_id 인덱스 추가
              - ix_scrape_snapshots_snapshot_date 인덱스 추가
    """

    # ── 1. scrape_run_id 컬럼을 nullable 로 추가 ──────────────────────────────
    # batch 밖에서 add_column 만 먼저 적용한다 — SQLite 가 ALTER TABLE
    # ADD COLUMN nullable 은 지원하므로 batch 가 필요 없고, 이후 백필 SQL 이
    # 같은 트랜잭션에서 값을 채울 수 있다.
    with op.batch_alter_table(
        "scrape_snapshots",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column("scrape_run_id", sa.Integer(), nullable=True)
        )

    # ── 2. 백필 — snapshot_date 별 최근 completed/partial ScrapeRun 매핑 ─────
    # 같은 KST 날짜에 종료된 가장 최근의 정상 ScrapeRun(completed/partial)을
    # snapshot.scrape_run_id 로 매핑한다. ``ended_at`` 이 NULL 인 (= running)
    # row 는 후보에서 제외 — terminal 상태인 ScrapeRun 만 후보로 본다.
    #
    # ``snapshot_date`` 는 KST 기준이지만 ``ended_at`` 은 UTC tz-aware 다.
    # 백필은 정확한 시각 매칭이 아니라 "같은 KST 날짜에 끝난 run 중 가장
    # 최근" 정도의 휴리스틱이면 충분하므로, 단순히 ScrapeRun 의 ended_at 을
    # DATE(...) 로 잘라 비교한다. 시차로 인한 경계 row 가 한두 건 매핑 누락
    # 되더라도 다음 단계 5 에서 행이 삭제되어 NOT NULL 제약을 만족시킨다.
    op.execute(
        sa.text(
            "UPDATE scrape_snapshots SET scrape_run_id = ("
            " SELECT id FROM scrape_runs"
            " WHERE scrape_runs.status IN ('completed', 'partial')"
            " AND scrape_runs.ended_at IS NOT NULL"
            " AND DATE(scrape_runs.ended_at) = DATE(scrape_snapshots.snapshot_date)"
            " ORDER BY scrape_runs.ended_at DESC"
            " LIMIT 1"
            ")"
        )
    )

    # ── 3. 같은 scrape_run_id 가 여러 row 에 매핑된 경우 정리 ────────────────
    # 이론상 거의 발생하지 않지만(백필 휴리스틱이 snapshot_date → 최근 run 1:1),
    # ended_at 이 다른 날 자정 직후로 넘어가는 등 경계 케이스에서 동일
    # ScrapeRun.id 가 인접한 두 snapshot row 에 매핑될 가능성을 방어한다.
    # 같은 scrape_run_id 그룹 안에서 PK 최솟값만 남기고 나머지 삭제 —
    # UNIQUE(scrape_run_id) 위반 방지.
    op.execute(
        sa.text(
            "DELETE FROM scrape_snapshots"
            " WHERE scrape_run_id IS NOT NULL"
            " AND id NOT IN ("
            " SELECT min_id FROM ("
            " SELECT MIN(id) AS min_id FROM scrape_snapshots"
            " WHERE scrape_run_id IS NOT NULL"
            " GROUP BY scrape_run_id"
            " ) AS dedup"
            ")"
        )
    )

    # ── 4. 백필 실패 row 삭제 (scrape_run_id IS NULL) ─────────────────────────
    # 그날 ScrapeRun 이 0건이거나 모두 failed/cancelled 였던 "고아 snapshot".
    # dashboard 캘린더 가용 set 의 KST 날짜 인덱스가 이미 끊긴 상태라 손실
    # 위험 없음.
    op.execute(
        sa.text("DELETE FROM scrape_snapshots WHERE scrape_run_id IS NULL")
    )

    # ── 5. UNIQUE 드롭 + scrape_run_id NOT NULL 승격 + FK / UNIQUE / 인덱스 ──
    # 하나의 batch 안에서 SQLite 가 테이블 재생성을 1회 수행하도록 묶는다.
    # 중간 상태가 발생하지 않도록 모든 변경을 한 블록에 둔다.
    with op.batch_alter_table(
        "scrape_snapshots",
        schema=None,
    ) as batch_op:
        # 기존 UNIQUE 제거 — 같은 KST 날짜에 여러 row 허용.
        batch_op.drop_constraint(
            "uq_scrape_snapshots_snapshot_date",
            type_="unique",
        )

        # scrape_run_id NOT NULL 승격 — 백필 + 고아 정리 이후 NULL row 가
        # 남아 있지 않으므로 안전.
        batch_op.alter_column(
            "scrape_run_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

        # scrape_runs FK — 잡 삭제 시 snapshot 도 함께 사라지도록 CASCADE.
        batch_op.create_foreign_key(
            "fk_scrape_snapshots_scrape_run_id",
            "scrape_runs",
            ["scrape_run_id"],
            ["id"],
            ondelete="CASCADE",
        )

        # 1 ScrapeRun = 1 snapshot row 보장.
        batch_op.create_unique_constraint(
            "uq_scrape_snapshots_scrape_run_id",
            ["scrape_run_id"],
        )

        # scrape_run_id 조회용 인덱스 (UNIQUE 가 이미 implicit 인덱스를
        # 제공하지만 SQLite/Postgres 호환을 위해 명시).
        batch_op.create_index(
            "ix_scrape_snapshots_scrape_run_id",
            ["scrape_run_id"],
        )

        # 기존 UNIQUE 가 제공하던 KST 날짜 조회용 implicit 인덱스를
        # 보완한다. 캘린더 / dashboard reduce 머지가 ORDER BY snapshot_date
        # ASC 로 조회하므로 명시적 인덱스 필요.
        batch_op.create_index(
            "ix_scrape_snapshots_snapshot_date",
            ["snapshot_date"],
        )


def downgrade() -> None:
    """업그레이드를 역방향으로 되돌린다.

    실행 순서:
        1. scrape_run_id 관련 인덱스 / UNIQUE / FK 드롭, snapshot_date 단순
           인덱스 드롭, UNIQUE(snapshot_date) 복원.
        2. scrape_run_id 컬럼 드롭.

    주의:
        같은 KST 날짜에 multi-row 가 존재하는 상태에서 downgrade 하면
        UNIQUE(snapshot_date) 복원 단계에서 실패한다. 그 경우 운영자가 수동
        으로 중복을 정리한 뒤 downgrade 를 재실행해야 한다 (downgrade 는
        개발 / 검증 경로 전용).
    """

    # ── 1. 신규 인덱스 / UNIQUE / FK 드롭 + 기존 UNIQUE 복원 ──────────────────
    with op.batch_alter_table(
        "scrape_snapshots",
        schema=None,
    ) as batch_op:
        batch_op.drop_index("ix_scrape_snapshots_snapshot_date")
        batch_op.drop_index("ix_scrape_snapshots_scrape_run_id")
        batch_op.drop_constraint(
            "uq_scrape_snapshots_scrape_run_id",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_scrape_snapshots_scrape_run_id",
            type_="foreignkey",
        )
        # 기존 UNIQUE 복원 — 중복 row 가 있으면 여기서 실패한다.
        batch_op.create_unique_constraint(
            "uq_scrape_snapshots_snapshot_date",
            ["snapshot_date"],
        )

    # ── 2. scrape_run_id 컬럼 드롭 ────────────────────────────────────────────
    with op.batch_alter_table(
        "scrape_snapshots",
        schema=None,
    ) as batch_op:
        batch_op.drop_column("scrape_run_id")
