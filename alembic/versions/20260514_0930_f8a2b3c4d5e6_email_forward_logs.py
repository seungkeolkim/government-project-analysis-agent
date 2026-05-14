"""task 00106-3: 신규 테이블 email_forward_logs 추가 (Phase A-2 Part 1).

설계 근거
    docs/phase_a2_part1_design_note.md, docs/db_portability.md §3, §4.
    Phase A-2 Part 1 — 공고 포워딩 이력 테이블을 단독 migration 으로 추가한다.
    비즈니스 로직·API·UI 는 A-2 Part 2 의 영역이므로 본 migration 에는 포함하지 않는다.

변경 사항 요약
    1. ``email_forward_logs`` 테이블 생성 (PK: id AUTOINCREMENT).
       - 포워딩 대상 공고 (canonical_project_id), 발송자 (sender_user_id),
         발송자 조직 (sender_organization_id), 메일 제목 (subject),
         추가 메시지 존재 여부 (has_additional_message Boolean — 본문 미저장),
         수신자 목록 (recipient_addresses JSON), 수신자 수 (recipient_count),
         결과 enum (status), 성공/실패 카운트, 시간 2개.
       - ``status`` 는 ``native_enum=False`` + 명시 CHECK constraint
         ('success'/'partial'/'failed' 세 값) — Postgres ENUM 타입 미생성,
         SQLite/Postgres 양쪽 호환 (db_portability §1, §3).
       - ``status`` default 없음 — 포워딩 시작 시점에는 결과를 알 수 없으므로
         application 코드(Part 2)가 INSERT 시 명시적으로 채운다.
       - ``created_at`` server_default 없음 — application 측 now_utc() 처리.
         (has_additional_message / success_count / failure_count 는 server_default
         를 두어 raw INSERT 안전망을 확보한다 — EmailSendRun 패턴 일관.)
    2. 인덱스 3종:
       - ``ix_email_forward_logs_canonical_project_id`` (canonical_project_id) —
         "이 공고의 포워딩 이력" 조회용. 외래키 매칭 효율 포함.
       - ``ix_email_forward_logs_sender_user_id`` (sender_user_id) —
         "내가 보낸 포워딩 목록" 조회용.
       - ``ix_email_forward_logs_canonical_project_id_created_at``
         (canonical_project_id, created_at) — 공고 상세 페이지 "최근 발송 이력"
         정렬 조회용. DESC ORDER BY 는 조회 SQL 에서 명시한다 (SQLite 의 expression
         index 호환 우려 — design note §1-e, EmailSendRun §5-3 결정 동일).

기존 row 영향
    신규 테이블만 생성하므로 기존 데이터 영향 없음. backfill 불필요.

다운그레이드
    인덱스 3개 → 테이블 순으로 삭제. 포워딩 이력은 복구되지 않으며,
    downgrade 는 개발/검증 경로에서만 사용을 권장한다.

SQLite ↔ Postgres 이식성 (docs/db_portability.md §1, §3, §4)
    - ``sa.JSON`` — SQLite TEXT / Postgres JSON 자동 매핑 (JSONB 미사용).
    - ``DateTime(timezone=True)`` — Postgres TIMESTAMPTZ / SQLite TEXT 양쪽 호환.
    - ``native_enum=False`` — Postgres ENUM 타입 미생성, CHECK constraint 만 추가.
    - 모든 FK / INDEX / CHECK constraint 이름 명시.
    - 신규 테이블 추가만 있어 ``batch_alter_table`` 컨텍스트 불필요.

검증 절차 (docs/db_portability.md §4 3단계)
    1. 기존 운영 DB 에 신규 migration 적용 — alembic upgrade head 에러 없음.
    2. 빈 SQLite 에 alembic upgrade head — baseline 부터 순차 적용 성공.
    3. Postgres syntax 호환 정적 검토 — sa.JSON, DateTime(timezone=True),
       native_enum=False, 이름 명시 constraint 확인.
    (단위 테스트 conftest 의 test_engine fixture 가 2번 절차를 자동 검증한다.)

Revision ID: f8a2b3c4d5e6
Revises: e7f8b9a3c456
Create Date: 2026-05-14 09:30:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "f8a2b3c4d5e6"
down_revision: Union[str, None] = "e7f8b9a3c456"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """email_forward_logs 테이블과 인덱스 3종을 생성한다.

    실행 순서:
        1. ``email_forward_logs`` 테이블 생성 (FK / CHECK constraint 포함).
        2. ``canonical_project_id`` 단일 인덱스.
        3. ``sender_user_id`` 단일 인덱스.
        4. ``(canonical_project_id, created_at)`` 복합 인덱스.
    """

    # ── email_forward_logs ───────────────────────────────────────────────────
    # 한 포워딩 요청 = 1 row. 개별 수신자 단위 발송 결과는 EmailSendRun 에 기록하고,
    # 본 row 에는 전체 결과 요약(status), 집계(success_count/failure_count), 메타만 저장한다.
    op.create_table(
        "email_forward_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # 어떤 공고를 포워딩했는지. 공고 삭제 시 CASCADE 로 이력도 삭제.
        sa.Column("canonical_project_id", sa.Integer(), nullable=False),
        # 발송을 트리거한 사용자. nullable — 사용자 탈퇴 시 SET NULL 로 row 보존.
        sa.Column("sender_user_id", sa.Integer(), nullable=True),
        # 발송 시점 발송자 조직. 무소속/미지정이면 NULL.
        sa.Column("sender_organization_id", sa.Integer(), nullable=True),
        # 메일 제목. 본문 자체는 개인정보·DB 크기 고려로 미저장.
        sa.Column("subject", sa.String(200), nullable=False),
        # 사용자가 추가 메시지를 첨부했는지 여부만 기록. 본문 텍스트는 미저장.
        # server_default 는 raw INSERT 호환 안전망 (ORM Python default=False 가 통상 우선).
        sa.Column(
            "has_additional_message",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # 수신자 이메일 주소 목록 (list of str). sa.JSON — db_portability §1.
        # SQLite 는 TEXT 로, Postgres 는 JSON 으로 자동 매핑. JSONB 금지.
        sa.Column("recipient_addresses", sa.JSON(), nullable=False),
        # len(recipient_addresses) 의 denormalize. 통계·UI 표시용.
        sa.Column("recipient_count", sa.Integer(), nullable=False),
        # 포워딩 결과 enum: 'success' / 'partial' / 'failed'.
        # native_enum=False — Postgres ENUM 타입 미생성, CHECK constraint 만 추가.
        # default 없음 — INSERT 시 application 코드(Part 2)가 명시적으로 채워야 한다.
        sa.Column(
            "status",
            sa.Enum(
                "success",
                "partial",
                "failed",
                name="emailforwardstatus",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        # 성공 수신자 수. server_default 는 raw INSERT 안전망.
        sa.Column(
            "success_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # 실패 수신자 수. server_default 는 raw INSERT 안전망.
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # 포워딩 트리거 시각 (UTC tz-aware). server_default 없음 —
        # application 측 now_utc() 가 전담한다 (ORM created_at default=_utcnow).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # 모든 send 완료 시각 (UTC tz-aware). 완료 전·기록 생략 시 NULL.
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # ── FK constraints ────────────────────────────────────────────────────
        # 공고 삭제 → 포워딩 이력 CASCADE 삭제.
        sa.ForeignKeyConstraint(
            ["canonical_project_id"],
            ["canonical_projects.id"],
            name="fk_email_forward_logs_canonical_project_id",
            ondelete="CASCADE",
        ),
        # 사용자 삭제 → FK 만 NULL 로 마스킹, row 보존.
        sa.ForeignKeyConstraint(
            ["sender_user_id"],
            ["users.id"],
            name="fk_email_forward_logs_sender_user_id",
            ondelete="SET NULL",
        ),
        # 조직 삭제 → FK 만 NULL 로 마스킹, row 보존.
        sa.ForeignKeyConstraint(
            ["sender_organization_id"],
            ["organizations.id"],
            name="fk_email_forward_logs_sender_organization_id",
            ondelete="SET NULL",
        ),
        # ── CHECK constraint ──────────────────────────────────────────────────
        # SQLAlchemy 의 sa.Enum(native_enum=False) 는 create_constraint=False 가
        # 기본이라 CHECK 를 자동 추가하지 않으므로 명시적으로 선언한다.
        # ORM 의 EmailForwardLog.__table_args__ 와 동일 이름·동일 SQL 로 중복 선언.
        sa.CheckConstraint(
            "status IN ('success', 'partial', 'failed')",
            name="ck_email_forward_logs_status",
        ),
    )

    # ── 인덱스 ───────────────────────────────────────────────────────────────
    # 모두 ascending 으로 생성한다. ORDER BY ... DESC 는 조회 SQL 에서 명시한다 —
    # SQLite 의 expression index 호환 우려를 피하기 위함 (design note §1-e,
    # EmailSendRun §5-3 결정 동일). 인덱스 자체는 양방향 스캔이 가능해
    # DESC ORDER BY 도 효율적으로 처리된다.

    # "이 공고의 포워딩 이력" 조회용. FK CASCADE 매칭 효율 포함.
    op.create_index(
        "ix_email_forward_logs_canonical_project_id",
        "email_forward_logs",
        ["canonical_project_id"],
    )

    # "내가 보낸 포워딩 목록" 조회용. FK SET NULL 매칭 효율 포함.
    op.create_index(
        "ix_email_forward_logs_sender_user_id",
        "email_forward_logs",
        ["sender_user_id"],
    )

    # 공고 상세 페이지 "최근 발송 이력" 정렬 조회용 복합 인덱스.
    # canonical_project_id 가 선두 컬럼이라 공고 필터 후 created_at 정렬이 효율적.
    op.create_index(
        "ix_email_forward_logs_canonical_project_id_created_at",
        "email_forward_logs",
        ["canonical_project_id", "created_at"],
    )


def downgrade() -> None:
    """email_forward_logs 테이블과 인덱스 3종을 삭제한다.

    실행 순서: 인덱스 3개 → 테이블 순으로 drop. 삭제된 포워딩 이력은 복구되지
    않으며, downgrade 는 일반적으로 개발/검증 경로에서만 사용한다.
    """

    op.drop_index(
        "ix_email_forward_logs_canonical_project_id_created_at",
        table_name="email_forward_logs",
    )
    op.drop_index(
        "ix_email_forward_logs_sender_user_id",
        table_name="email_forward_logs",
    )
    op.drop_index(
        "ix_email_forward_logs_canonical_project_id",
        table_name="email_forward_logs",
    )
    op.drop_table("email_forward_logs")
