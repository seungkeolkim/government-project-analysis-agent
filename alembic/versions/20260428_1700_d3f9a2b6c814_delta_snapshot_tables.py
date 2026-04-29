"""task 00041 (Phase 5a): delta_announcements / delta_attachments / scrape_snapshots 신설

수집 파이프라인을 delta 기반으로 재설계하기 위한 3 개 테이블을 baseline 위에
추가한다. 기존 `announcements` / `attachments` 스키마는 변경하지 않는다 —
사용자 원문 'Migration' 섹션의 "기존 announcements/attachments 스키마 변경
없음" 조건을 그대로 따른다.

설계 근거: docs/snapshot_pipeline_design.md §1·§4·§5·§6 (테이블 구조·constraint
이름 전수표·인덱스 전수표).

준수 원칙 (docs/db_portability.md):
    - JSON 컬럼은 sa.JSON() 범용 타입 (JSONB 금지).
    - 모든 시간 컬럼은 sa.DateTime(timezone=True). naive datetime 금지.
    - String 컬럼에 길이 N 명시.
    - 모든 constraint(FK/UNIQUE/CHECK)에 이름 부여 — uq_/fk_/ck_/ix_ prefix 규칙.
    - SQLite ALTER 호환을 위해 ALTER 가 필요한 경우 batch_alter_table 사용
      (본 migration 은 신규 CREATE TABLE 만 수행하므로 batch 불필요).

테이블 의도 요약:
    - delta_announcements: 매 ScrapeRun 동안 적재되는 공고 메타 staging.
      종료 시점 단일 트랜잭션에서 본 테이블에 4-branch 적용 후 비워진다.
    - delta_attachments: 같은 ScrapeRun 의 첨부 메타 staging. 파일 자체는
      data/downloads/ 에 즉시 떨어지고 DB 메타만 delta 경유.
    - scrape_snapshots: KST 날짜 단위 변화 요약. 같은 날 여러 ScrapeRun 결과는
      payload 머지로 1 row 에 모은다 (사용자 원문 머지 규칙).

생성 순서 (FK 의존성 기준):
    1. delta_announcements  (FK → scrape_runs)
    2. delta_attachments     (FK → delta_announcements)
    3. scrape_snapshots       (FK 없음 — 독립 요약 테이블)

downgrade 는 정확히 역순으로 drop_table 한다 (인덱스는 drop_table 이 함께 처리).

검증 절차 (docs/alembic_verification.md 의 3 tier 동일):
    1. 기존 SQLite (stamp 경로): 운영 DB 사본에 init_db → 데이터 무변경 확인.
    2. 빈 SQLite (baseline-bootstrap): 새 DB 에 alembic upgrade head →
       3 신규 테이블 생성 확인.
    3. Postgres syntax 호환: 본 migration 은 sqlite3 dialect 전용 SQL 을
       사용하지 않는다 (CREATE TABLE / 일반 인덱스 / sa.JSON / sa.Date 만 사용).

Revision ID: d3f9a2b6c814
Revises: c4a8d1e7b2f3
Create Date: 2026-04-28 17:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Alembic 식별자 ─────────────────────────────────────────────────────────────
revision: str = "d3f9a2b6c814"
down_revision: Union[str, None] = "c4a8d1e7b2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """3 개 신규 테이블(delta_announcements / delta_attachments / scrape_snapshots) 생성.

    기존 announcements / attachments 스키마를 건드리는 ALTER 는 한 줄도 없다.
    모든 제약과 인덱스 이름은 docs/snapshot_pipeline_design.md §5·§6 의 전수표를
    따른다 (uq_/fk_/ck_/ix_ prefix 규칙 — docs/db_portability.md §4).
    """

    # ── 1. delta_announcements ────────────────────────────────────────────────
    # 수집 단계에서 적재되는 공고 메타 staging. 매 ScrapeRun 종료 시 apply 후
    # 명시적 DELETE 로 비워진다 (사용자 원문 "delta 테이블은 매번 비움").
    #
    # 컬럼 설계 메모:
    #   - announcements 본 테이블과 호환되는 비교 필드(title/status/agency/
    #     deadline_at) 는 그대로 채워 4-branch 비교(Phase 1a 로직 재사용) 가
    #     동작하도록 한다.
    #   - status 는 delta 단계에서는 plain String(32) — 본 테이블의
    #     AnnouncementStatus Enum 으로 정규화하는 것은 apply 단계의 책임
    #     (00041-3) 이다. delta 가 raw 값을 받아내는 입구 역할을 함으로써
    #     소스(IRIS/NTIS) 별 잡음(공백 / 영문 / 다른 표기) 이 본 테이블에
    #     들어오기 전에 흡수된다 — 본 task plan_position 의 guidance
    #     "source 별 raw 값을 일단 받은 뒤 본 테이블 적용 시 정규화하는
    #     설계로 잡음 차단" 그대로.
    #   - 본 테이블과 달리 DB CHECK 도 부여하지 않는다 — raw 값이 들어오는
    #     입구이므로 도메인을 좁히면 어댑터에서 던진 비정형 값이 INSERT 시점에
    #     실패해 ScrapeRun 이 깨지게 된다. 정규화/도메인 검증은 apply 단계가
    #     수행한다.
    op.create_table(
        "delta_announcements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scrape_run_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column(
            "source_announcement_id",
            sa.String(128),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("agency", sa.String(255), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "deadline_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("detail_url", sa.Text(), nullable=True),
        sa.Column("detail_html", sa.Text(), nullable=True),
        sa.Column("detail_text", sa.Text(), nullable=True),
        sa.Column(
            "detail_fetched_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("detail_fetch_status", sa.String(16), nullable=True),
        # ancm_no: IRIS / NTIS 의 공식 공고번호 (canonical_key official scheme 재계산용).
        # 본 테이블 announcements 에는 직접 컬럼이 없고 raw_metadata 에 들어가지만,
        # delta 단계에서는 별도 컬럼으로 빼두어 apply 단계에서 _apply_canonical
        # 호출 시 인자로 전달하기 쉽게 한다.
        sa.Column("ancm_no", sa.String(64), nullable=True),
        sa.Column("raw_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["scrape_run_id"],
            ["scrape_runs.id"],
            name="fk_delta_announcements_scrape_run_id",
            ondelete="CASCADE",
        ),
    )
    # apply 단계에서 scrape_run_id 로 전수 조회한다.
    op.create_index(
        "ix_delta_announcements_scrape_run_id",
        "delta_announcements",
        ["scrape_run_id"],
    )
    # apply 단계가 본 테이블과 매칭할 때 (source_type, source_announcement_id)
    # 복합 키로 announcements row 를 빠르게 lookup 한다.
    op.create_index(
        "ix_delta_announcements_source_lookup",
        "delta_announcements",
        ["source_type", "source_announcement_id"],
    )

    # ── 2. delta_attachments ──────────────────────────────────────────────────
    # 수집 단계의 첨부 메타 staging. 매 ScrapeRun 종료 시 본 테이블 attachments
    # 에 sha256 기반 upsert 가 적용된 뒤 명시적 DELETE 로 비워진다.
    #
    # 컬럼 설계 메모:
    #   - 본 테이블 attachments 와 컬럼 의미가 1:1 대응되도록 맞춘다 (apply
    #     단계가 dict 기반으로 그대로 흘려 보낼 수 있도록).
    #   - 파일 자체는 data/downloads/{source_type}/{ann_id}/{filename} 에 이미
    #     떨어진 상태에서 stored_path 가 그 경로를 가리킨다. 파일은 트랜잭션
    #     보호 밖이므로 apply 가 rollback 되면 고아 파일이 남고 GC 가 정리한다
    #     (사용자 원문 + 설계 §11).
    #   - sha256 은 NULL 허용 — 다운로드 실패 시.
    op.create_table(
        "delta_attachments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "delta_announcement_id", sa.Integer(), nullable=False
        ),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column("file_ext", sa.String(16), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("download_url", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column(
            "downloaded_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["delta_announcement_id"],
            ["delta_announcements.id"],
            name="fk_delta_attachments_delta_announcement_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_delta_attachments_delta_announcement_id",
        "delta_attachments",
        ["delta_announcement_id"],
    )

    # ── 3. scrape_snapshots ───────────────────────────────────────────────────
    # KST 날짜 단위 변화 요약. 같은 KST 날짜의 후속 ScrapeRun 종료 시 payload
    # 머지로 1 row 에 누적된다 — UNIQUE(snapshot_date) 가 머지 기준 키다.
    #
    # 컬럼 설계 메모:
    #   - snapshot_date: sa.Date — 시간 정보 없는 날짜. SQLite Date 컬럼은
    #     timezone 정보를 보유하지 않으므로 호출자(00041-4 의 upsert) 가
    #     `app.timezone.now_kst().date()` 로 KST 변환 후 저장한다.
    #     사용자 원문 "snapshot_date 는 created_at 의 KST 날짜" 를 따른다.
    #   - created_at / updated_at: 일반 TIMESTAMPTZ. UTC 저장 — Phase 4 컨벤션.
    #   - payload: 5 종 카테고리 (new / content_changed / transitioned_to_접수예정/
    #     접수중/마감) + counts 를 담는 자유 스키마 JSON. 구조 정의는
    #     docs/snapshot_pipeline_design.md §10 에 있고 머지 규칙은 §9 에 있다.
    op.create_table(
        "scrape_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "snapshot_date",
            name="uq_scrape_snapshots_snapshot_date",
        ),
    )
    # snapshot_date 조회는 UNIQUE 가 implicit index 로 커버하므로 추가 인덱스
    # 없음. 5b 의 일자 범위 조회도 UNIQUE 인덱스로 충분히 커버된다.


def downgrade() -> None:
    """3 개 신규 테이블을 생성 역순으로 삭제한다 (FK 의존성 역순).

    인덱스는 drop_table 이 함께 처리하므로 별도 drop_index 불필요. 기존
    announcements / attachments / scrape_runs 는 건드리지 않는다 — 본
    migration 은 신규 테이블 추가만 수행했으므로 ALTER 복원이 없다.
    """

    op.drop_table("scrape_snapshots")
    op.drop_table("delta_attachments")
    op.drop_table("delta_announcements")
