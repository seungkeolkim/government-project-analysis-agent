"""task 00104-3: 신규 테이블 email_send_runs 추가 (Phase A-1 — 메일 발송 이력).

설계 근거
    docs/phase_a1_design_note.md §4-1, §4-2, §4-5, §5, §9.
    Phase A-1 메일 인프라가 발송 시도 결과(성공/실패) 를 한 row 로 영속 기록할
    이력 테이블을 단독 migration 으로 추가한다. 한 번의 ``send_with_retry`` 호출
    이 1 row 를 생성하며, 재시도 횟수는 ``attempt_count`` 에 누적되어 저장되고
    중간 시도의 에러 메시지는 별도 row 로 분리하지 않고 마지막 시도의 예외만
    ``error_message`` 에 저장한다 (디자인 노트 §4-1).

변경 사항 요약
    1. ``email_send_runs`` 테이블 생성 (PK: id AUTOINCREMENT).
       - 본문/주제/수신자 메타 + transport/status 분류 + 시도 횟수/에러 메시지 +
         발송 컨텍스트 (requested_by_user_id / related_kind / related_id) +
         시간 추적 (created_at / sent_at).
       - ``status`` 는 ``native_enum=False`` + 명시 CHECK constraint
         ('sent'/'failed' 두 값) — Postgres ENUM 타입 미생성, SQLite/Postgres
         양쪽 호환 (db_portability §1, AnnouncementProgress 패턴과 동일).
       - ``transport_type`` / ``related_kind`` 는 plain String, CHECK 없음 —
         A-2/A-3 가 'forward', 'daily_report' 등 새 값을 ALTER 없이 추가할 수
         있도록 의도적으로 열어 둔다 (디자인 노트 §4-5).
       - ``requested_by_user_id`` FK users.id ON DELETE SET NULL, nullable —
         시스템 자동 발송 (향후 daily report) 대비 nullable, 사용자 탈퇴 시
         row 자체는 보존하고 외래키만 NULL 로 마스킹.
    2. 인덱스 3종 모두 ascending 으로 생성:
       - ``ix_email_send_runs_created_at`` (created_at) — 최근 이력 조회용.
         ORDER BY created_at DESC 는 조회 SQL 에서 명시한다 (디자인 노트 §5-3
         결정 — DESC expression index 는 SQLite 호환 우려가 있어 채택하지 않음).
       - ``ix_email_send_runs_status_created_at`` (status, created_at) —
         실패 이력만 빠르게 보기 위한 복합 인덱스 (향후 활용).
       - ``ix_email_send_runs_requested_by_user_id`` — 사용자별 조회 + FK
         CASCADE/SET NULL 매칭 효율 (향후 활용).

기존 row 영향
    신규 테이블만 생성하므로 기존 데이터 영향 없음. backfill 불필요.

다운그레이드
    인덱스 → 테이블 순으로 삭제. 발송 이력은 복구되지 않으며, downgrade 는
    개발/검증 경로에서만 사용을 권장한다.

SQLite ↔ Postgres 이식성 (docs/db_portability.md §1, §3, §4)
    - ``DateTime(timezone=True)`` — Postgres TIMESTAMPTZ / SQLite TEXT 양쪽 호환.
      DB 저장은 UTC tz-aware (PROJECT_NOTES 컨벤션 — \"DB 저장은 UTC, 표시 경계
      에서 KST\"). 사용자 화면 표시 직전에 ``app.timezone.format_kst`` 또는
      Jinja2 ``kst_format`` 필터로 KST 변환한다.
    - ``native_enum=False`` — Postgres ENUM 타입 미생성, CHECK constraint 만
      추가 (SQLite 도 동일).
    - 모든 FK / INDEX / CHECK constraint 이름 명시.
    - ``server_default=sa.text(\"CURRENT_TIMESTAMP\")`` — SQLite/Postgres 모두 지원.
      ORM Python default (``_utcnow``) 가 통상 우선 적용되며 raw INSERT 호환용.

Revision ID: e7f8b9a3c456
Revises: b8d7e2c45f01
Create Date: 2026-05-13 09:15:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "e7f8b9a3c456"
down_revision: Union[str, None] = "b8d7e2c45f01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """email_send_runs 테이블과 3 개의 인덱스를 생성한다.

    실행 순서:
        1. ``email_send_runs`` 테이블 생성 (FK / CHECK constraint 포함).
        2. ``created_at`` 단일 컬럼 인덱스.
        3. ``(status, created_at)`` 복합 인덱스.
        4. ``requested_by_user_id`` 단일 컬럼 인덱스.
    """

    # ── email_send_runs ──────────────────────────────────────────────────────
    # 한 발송 시도 (재시도 포함) 당 row 1 개. attempt_count 가 누적되어
    # 재시도 횟수를 표현하며, status 는 최종 결과 (sent / failed) 만 반영한다.
    op.create_table(
        "email_send_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # 받는 사람 이메일. RFC 5321 의 최대 320 자(local 64 + @ + domain 255)
        # 까지 수용. 단일 주소만 허용 — cc/bcc 는 Phase A-1 범위 밖.
        sa.Column("recipient", sa.String(320), nullable=False),
        # 제목. Admin API Pydantic schema 가 1 ≤ len ≤ 200 으로 제한하므로
        # 동일 길이로 컬럼 길이 고정.
        sa.Column("subject", sa.String(200), nullable=False),
        # 본문 앞 200 자 preview. 전체 본문 저장은 사이즈 부담 → preview 만
        # 보관해 \"발송 이력\" 목록의 빠른 확인용으로 사용한다.
        sa.Column("body_preview", sa.String(200), nullable=False),
        # transport 종류. 현재 코드에서 채워지는 유일한 값은 'm365_oauth' 이지만
        # CHECK constraint 는 두지 않는다 — 향후 옵션 C (Basic Auth SMTP) 가
        # ALTER 없이 새 값을 채울 수 있도록 의도적으로 열어 둔다 (디자인 노트 §4-5).
        sa.Column("transport_type", sa.String(32), nullable=False),
        # 발송 결과 enum: 'sent' / 'failed'. native_enum=False — Postgres ENUM
        # 타입 미생성, CHECK constraint 만 추가 (db_portability §1).
        sa.Column(
            "status",
            sa.Enum(
                "sent",
                "failed",
                name="email_send_run_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        # 실패 시 마지막 시도의 예외 메시지 ('ClassName: message' 형식).
        # 성공 row 는 NULL. 중간 시도의 에러는 본 컬럼에 저장하지 않으며 loguru
        # 로그에서 확인한다 (디자인 노트 §4-1 결정).
        sa.Column("error_message", sa.Text(), nullable=True),
        # 시도 횟수 (재시도 포함). 1차 시도만 성공한 경우 1, 1차 실패 + 2차
        # 성공이면 2. server_default 는 raw INSERT 호환용이며 ORM Python
        # default 가 통상 우선 적용된다.
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        # 발송 트리거 사용자. 시스템 자동 발송 (향후 daily report) 대비
        # nullable. 사용자 탈퇴 시 SET NULL 로 row 자체는 보존된다.
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        # 발송 컨텍스트 식별자 (A-1: 'test_send' 만 사용, A-2/A-3 에서 추가).
        # 어떤 외부 객체와 연결되는지를 짝(related_kind, related_id) 으로 표현
        # 한다 — 단일 FK 로 묶기 어려운 다형성 관계이므로 String + Integer 조합.
        sa.Column("related_kind", sa.String(32), nullable=True),
        # 발송 컨텍스트 객체 PK (related_kind 와 함께 의미). A-1 에서는 항상 NULL.
        sa.Column("related_id", sa.Integer(), nullable=True),
        # 발송 시도 시작 시각 (UTC tz-aware). DB 저장은 UTC 컨벤션이며
        # (PROJECT_NOTES 시각 처리), 사용자 화면 표시 직전에 KST 로 변환한다.
        # ORM Python default ``_utcnow`` 가 우선 적용되며 server_default 는
        # raw INSERT 호환 안전망.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # 발송 성공 시각 (UTC tz-aware). 실패 row 는 NULL. ORM 측에서 명시 set
        # 하므로 server_default 는 없다.
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # 발송 트리거 사용자 FK. 사용자 삭제 시 SET NULL 로 row 보존.
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_email_send_runs_requested_by_user_id",
            ondelete="SET NULL",
        ),
        # status enum DB 레벨 강제 — SQLAlchemy 의 sa.Enum(native_enum=False) 는
        # create_constraint 가 기본 False 라 CHECK 를 자동 생성하지 않으므로
        # 명시적으로 추가한다 (AnnouncementProgress 와 동일 패턴).
        sa.CheckConstraint(
            "status IN ('sent', 'failed')",
            name="ck_email_send_runs_status",
        ),
    )

    # ── 인덱스 ───────────────────────────────────────────────────────────────
    # 모두 ascending 으로 생성한다. ORDER BY created_at DESC 는 조회 SQL 에서
    # 명시한다 — SQLite 의 expression index 호환 우려를 피하기 위함 (디자인 노트
    # §5-3 결정). 인덱스 자체는 양방향 스캔이 가능해 DESC ORDER BY 도 효율적.

    # 최근 이력 조회용. 발송 이력 화면이 ORDER BY created_at DESC LIMIT 50 으로
    # 호출하는 SQL 의 핵심 인덱스.
    op.create_index(
        "ix_email_send_runs_created_at",
        "email_send_runs",
        ["created_at"],
    )

    # 실패 이력만 빠르게 보기 위한 복합 인덱스. status 가 선두 컬럼이라
    # WHERE status='failed' 필터의 효율이 높다. 향후 활용 (현재 status='all'
    # default 에서는 사용 빈도 낮음).
    op.create_index(
        "ix_email_send_runs_status_created_at",
        "email_send_runs",
        ["status", "created_at"],
    )

    # 사용자별 조회 + FK SET NULL 매칭 효율. A-1 단계에서는 화면 노출 없으나,
    # 사용자 탈퇴 처리 시 외래키 매칭 비용을 낮춰 두는 효과가 있다.
    op.create_index(
        "ix_email_send_runs_requested_by_user_id",
        "email_send_runs",
        ["requested_by_user_id"],
    )


def downgrade() -> None:
    """email_send_runs 테이블과 인덱스 3종을 삭제한다.

    실행 순서: 인덱스 3개 → 테이블 순으로 drop. 삭제된 발송 이력은 복구되지
    않으며, downgrade 는 일반적으로 개발/검증 경로에서만 사용한다.
    """

    op.drop_index(
        "ix_email_send_runs_requested_by_user_id",
        table_name="email_send_runs",
    )
    op.drop_index(
        "ix_email_send_runs_status_created_at",
        table_name="email_send_runs",
    )
    op.drop_index(
        "ix_email_send_runs_created_at",
        table_name="email_send_runs",
    )
    op.drop_table("email_send_runs")
