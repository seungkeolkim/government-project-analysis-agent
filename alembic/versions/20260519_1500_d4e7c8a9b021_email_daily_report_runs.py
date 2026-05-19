"""task 00125-2: 신규 테이블 email_daily_report_runs 추가 (Phase A-3 / 단체 Daily Report).

설계 근거
    docs/phase_a3_design_note.md §14 + phase_a3_prompt.md §"백엔드 변경 2".
    Phase A-3 단체 Daily Report 의 발송 시도 이력 테이블을 단독 migration 으로
    추가한다. 비즈니스 로직(window 계산·aggregation·발송 서비스) 과 admin API /
    UI 는 후속 subtask (00125-3 ~ 00125-9) 의 영역이므로 본 migration 에는
    포함하지 않는다 — 신규 테이블 + 인덱스 + CHECK constraint 만 생성한다.

    ``EmailSendRun.related_kind`` 의 새 값 ``'daily_report'`` 는 application
    레벨 enum 으로 처리하며 DB 변경이 없다 (디자인 노트 §14 + EmailSendRun
    의 ``related_kind`` 컬럼이 plain String + CHECK 미부여인 의도된 확장 여유).

변경 사항 요약
    1. ``email_daily_report_runs`` 테이블 생성 (PK: id AUTOINCREMENT).
       - 트리거 종류(trigger), 최종 상태(status enum 5종), 누적 구간
         (aggregation_from / aggregation_to, NULL 가능),
         카운터 4 종(snapshot_count / recipient_count / success_count /
         failure_count, server_default '0'),
         에러 메시지(error_message Text, NULL 가능),
         시간 2 개(started_at NOT NULL + server_default CURRENT_TIMESTAMP,
         completed_at NULL),
         발송 트리거 사용자 (requested_by_user_id, FK users.id, ondelete=SET NULL).
       - ``status`` 는 ``native_enum=False`` + 명시 CHECK constraint
         ('in_progress'/'success'/'partial'/'failed'/'skipped') — Postgres
         ENUM 타입 미생성, SQLite/Postgres 양쪽 호환 (db_portability §1, §3,
         EmailSendRun / EmailForwardLog 동일 패턴).
       - ``trigger`` 는 plain String(20), CHECK 없음 — EmailSendRun.related_kind
         와 동일한 확장 여유 패턴 (디자인 노트 §14).
       - ``requested_by_user_id`` FK users.id ON DELETE SET NULL, nullable —
         scheduled 트리거이거나 사용자 탈퇴 시 row 자체는 보존하고 FK 만
         NULL 로 마스킹.
    2. 인덱스 2 종 (디자인 노트 §14 / prompt §2):
       - ``ix_email_daily_report_runs_started_at`` (started_at) — 발송 이력
         화면이 ORDER BY started_at DESC LIMIT 50 으로 조회하는 SQL 의 핵심
         인덱스. ascending 인덱스는 양방향 스캔이 가능해 DESC ORDER BY 도
         효율적 (EmailSendRun §5-3 결정 동일).
       - ``ix_email_daily_report_runs_status`` (status) — 실패/skipped 만
         빠르게 필터링하는 보조 인덱스. 향후 활용.

기존 row 영향
    신규 테이블만 생성하므로 기존 데이터 영향 없음. backfill 불필요.
    ``EmailSendRun.related_kind`` 의 새 값 'daily_report' 는 application
    레벨 enum 이라 DB 마이그레이션 없음.

다운그레이드
    인덱스 → 테이블 순으로 삭제. 삭제된 daily report 이력은 복구되지 않으며,
    downgrade 는 개발/검증 경로에서만 사용을 권장한다.

SQLite ↔ Postgres 이식성 (docs/db_portability.md §1, §3, §4)
    - ``DateTime(timezone=True)`` — Postgres TIMESTAMPTZ / SQLite TEXT 양쪽
      호환. DB 저장은 UTC tz-aware (PROJECT_NOTES 컨벤션).
    - ``native_enum=False`` — Postgres ENUM 타입 미생성, CHECK constraint
      만 추가.
    - 모든 FK / INDEX / CHECK constraint 이름 명시.
    - ``server_default=sa.text("CURRENT_TIMESTAMP")`` — SQLite/Postgres 모두
      지원. ORM Python default (_utcnow) 가 통상 우선 적용되며 본 server_default
      는 raw INSERT 호환 안전망 (EmailSendRun 동일 패턴).
    - 신규 테이블 추가만 있어 ``batch_alter_table`` 컨텍스트 불필요.

검증 절차 (docs/db_portability.md §4 3단계)
    1. 기존 운영 DB 에 신규 migration 적용 — ``alembic upgrade head`` 에러 없음.
    2. 빈 SQLite 에 ``alembic upgrade head`` — baseline 부터 순차 적용 성공.
    3. Postgres syntax 호환 정적 검토 — DateTime(timezone=True),
       native_enum=False, 이름 명시 constraint 확인.
    (단위 테스트 conftest 의 test_engine fixture 가 2번 절차를 자동 검증한다.)

Revision ID: d4e7c8a9b021
Revises: f8a2b3c4d5e6
Create Date: 2026-05-19 15:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "d4e7c8a9b021"
down_revision: Union[str, None] = "f8a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """email_daily_report_runs 테이블과 인덱스 2종을 생성한다.

    실행 순서:
        1. ``email_daily_report_runs`` 테이블 생성 (FK / CHECK constraint 포함).
        2. ``started_at`` 단일 컬럼 인덱스 (이력 화면 최근순 조회용).
        3. ``status`` 단일 컬럼 인덱스 (실패/skipped 필터링 보조).
    """

    # ── email_daily_report_runs ──────────────────────────────────────────────
    # 한 ``prepare_and_send_daily_report`` 호출 = row 1 개. 개별 수신자 단위 발송
    # 결과는 EmailSendRun 에 ``related_kind='daily_report'`` / ``related_id=<본
    # row.id>`` 로 연결되며 (forwarding 패턴 동일), 본 row 에는 전체 결과 요약
    # (status), 집계(snapshot_count / recipient_count / success_count /
    # failure_count), 메타(trigger / 시간 / 누적 구간 / 트리거 사용자) 만 저장한다.
    op.create_table(
        "email_daily_report_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # 트리거 종류. 'scheduled' / 'manual_admin' / 'manual_test' 3 값.
        # CHECK constraint 없음 — EmailSendRun.related_kind 와 동일한 확장 여유
        # 패턴 (디자인 노트 §14).
        sa.Column("trigger", sa.String(20), nullable=False),
        # 발송 결과 enum. native_enum=False — Postgres ENUM 미생성, CHECK constraint
        # 만 추가 (EmailSendRun / EmailForwardLog 동일 패턴).
        # INSERT 직후 'in_progress' 로 채워지며, 발송 루프 완료 시 최종 상태로 갱신.
        # default 없음 — INSERT 시 application 코드가 명시적으로 채운다.
        sa.Column(
            "status",
            sa.Enum(
                "in_progress",
                "success",
                "partial",
                "failed",
                "skipped",
                name="email_daily_report_status",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        # 누적 구간 시작 (exclusive). SKIPPED 케이스에서 구간이 정해지기 전 commit
        # 되면 NULL. 결정 후에는 to 와 한 쌍으로 채워진다.
        sa.Column(
            "aggregation_from",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # 누적 구간 끝 (inclusive). 통상 now_utc(). SKIPPED 케이스 NULL 가능.
        sa.Column(
            "aggregation_to",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # 구간 내 scrape_snapshots row 수. server_default 는 raw INSERT 호환 안전망.
        sa.Column(
            "snapshot_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # 발송 대상 수신자 수. server_default 는 raw INSERT 안전망.
        sa.Column(
            "recipient_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # 발송 성공 수신자 수. server_default 는 raw INSERT 안전망.
        sa.Column(
            "success_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # 발송 실패 수신자 수. server_default 는 raw INSERT 안전망.
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # 사전 단계 실패 사유 또는 마지막 발송 시도의 에러 메시지. SUCCESS / SKIPPED
        # row 는 NULL. 수신자별 상세 에러는 EmailSendRun.error_message 에 보관된다
        # (forwarding 과 동일 책임 분리).
        sa.Column("error_message", sa.Text(), nullable=True),
        # 본 run 트리거 시각 (UTC tz-aware). ORM Python default (_utcnow) 가
        # 통상 우선 적용되며 server_default 는 raw INSERT 호환 안전망 (EmailSendRun
        # 동일 패턴).
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # 발송 루프 종료 시각 (UTC tz-aware). 진행 중이거나 사전 단계 실패 직전에는
        # NULL. ORM 측에서 명시 set 하므로 server_default 없음.
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # manual 트리거 시 발송을 누른 사용자 PK. scheduled 또는 사용자 탈퇴 후
        # NULL. EmailSendRun.requested_by_user_id / EmailForwardLog.sender_user_id
        # 와 동일한 SET NULL 패턴.
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        # ── FK constraint ────────────────────────────────────────────────────
        # 사용자 삭제 → FK 만 NULL 로 마스킹, row 보존.
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_email_daily_report_runs_requested_by_user_id",
            ondelete="SET NULL",
        ),
        # ── CHECK constraint ─────────────────────────────────────────────────
        # status enum DB 레벨 강제 — SQLAlchemy 의 sa.Enum(native_enum=False) 는
        # create_constraint=False 가 기본이라 CHECK 를 자동 추가하지 않으므로
        # 명시적으로 선언한다 (EmailSendRun / EmailForwardLog 동일 패턴).
        # ORM 의 EmailDailyReportRun.__table_args__ 와 동일 이름·동일 SQL 로
        # 중복 선언한다.
        sa.CheckConstraint(
            "status IN ('in_progress', 'success', 'partial', 'failed', 'skipped')",
            name="ck_email_daily_report_runs_status",
        ),
    )

    # ── 인덱스 ───────────────────────────────────────────────────────────────
    # 모두 ascending 으로 생성한다. ORDER BY started_at DESC 는 조회 SQL 에서
    # 명시한다 — SQLite 의 expression index 호환 우려를 피하기 위함 (EmailSendRun
    # §5-3 결정 동일). 인덱스 자체는 양방향 스캔이 가능해 DESC ORDER BY 도 효율적.

    # 최근 이력 조회용. 발송 이력 화면이 ORDER BY started_at DESC LIMIT 50 으로
    # 호출하는 SQL 의 핵심 인덱스.
    op.create_index(
        "ix_email_daily_report_runs_started_at",
        "email_daily_report_runs",
        ["started_at"],
    )

    # 실패/skipped 만 빠르게 필터링하는 보조 인덱스. status='in_progress' 가 비정상
    # 적으로 오래 남아 있는 row 추적에도 유용.
    op.create_index(
        "ix_email_daily_report_runs_status",
        "email_daily_report_runs",
        ["status"],
    )


def downgrade() -> None:
    """email_daily_report_runs 테이블과 인덱스 2종을 삭제한다.

    실행 순서: 인덱스 2 개 → 테이블 순으로 drop. 삭제된 daily report 이력은
    복구되지 않으며, downgrade 는 일반적으로 개발/검증 경로에서만 사용한다.
    """

    op.drop_index(
        "ix_email_daily_report_runs_status",
        table_name="email_daily_report_runs",
    )
    op.drop_index(
        "ix_email_daily_report_runs_started_at",
        table_name="email_daily_report_runs",
    )
    op.drop_table("email_daily_report_runs")
