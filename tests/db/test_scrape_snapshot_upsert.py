"""ScrapeSnapshot UPSERT 통합 테스트 (Phase 5a / task 00041-4).

검증 대상: ``app/db/repository.py`` 의 ``upsert_scrape_snapshot`` /
``get_scrape_snapshot_by_date``.

설계 §9.6·§10.1·§10.2 의 동작:
    - 같은 KST 날짜의 row 가 없으면 신규 INSERT (정규형 payload 로 저장).
    - 있으면 ``merge_snapshot_payload`` 로 머지 후 in-place UPDATE.
    - 빈 ScrapeRun 도 5종 카테고리가 빈 배열로 채워진 row 가 만들어진다.
    - UNIQUE(snapshot_date) 가 같은 날짜 중복 row 를 차단한다.

본 파일은 실 DB 세션을 사용한다 (tests/conftest.py 의 ``db_session`` fixture).
순수 머지 룰 자체는 test_snapshot_merge.py 가 검증하므로, 여기서는 DB 경로의
INSERT vs UPDATE 분기와 트랜잭션 atomic 동작에만 초점을 둔다.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ScrapeSnapshot
from app.db.repository import (
    get_scrape_snapshot_by_date,
    upsert_scrape_snapshot,
)
from app.db.snapshot import (
    CATEGORY_CONTENT_CHANGED,
    CATEGORY_NEW,
)


_TEST_DATE = date(2026, 4, 28)
_TEST_DATE_OTHER = date(2026, 4, 29)


def test_upsert_inserts_new_row_with_normalized_payload(db_session: Session) -> None:
    """같은 KST 날짜에 row 가 없으면 신규 INSERT — 빈 카테고리도 정규형으로 채움."""
    payload_in = {CATEGORY_NEW: [10, 20]}

    snapshot = upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload=payload_in,
    )
    db_session.commit()

    assert snapshot.id is not None
    assert snapshot.snapshot_date == _TEST_DATE
    assert snapshot.payload[CATEGORY_NEW] == [10, 20]
    assert snapshot.payload[CATEGORY_CONTENT_CHANGED] == []  # 정규화 — 빈 카테고리 채움
    assert snapshot.payload["transitioned_to_마감"] == []
    assert snapshot.payload["counts"][CATEGORY_NEW] == 2
    assert snapshot.payload["counts"][CATEGORY_CONTENT_CHANGED] == 0


def test_upsert_merges_payload_when_row_exists(db_session: Session) -> None:
    """같은 KST 날짜의 후속 호출은 기존 row 를 머지 UPDATE 한다 (검증 3)."""
    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [10]},
    )
    db_session.commit()

    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [20], CATEGORY_CONTENT_CHANGED: [30]},
    )
    db_session.commit()

    rows = list(
        db_session.execute(select(ScrapeSnapshot).where(ScrapeSnapshot.snapshot_date == _TEST_DATE))
        .scalars()
    )
    # UNIQUE 제약상 row 는 정확히 1 건이어야 한다.
    assert len(rows) == 1
    merged_payload = rows[0].payload
    assert merged_payload[CATEGORY_NEW] == [10, 20]
    assert merged_payload[CATEGORY_CONTENT_CHANGED] == [30]
    assert merged_payload["counts"][CATEGORY_NEW] == 2
    assert merged_payload["counts"][CATEGORY_CONTENT_CHANGED] == 1


def test_upsert_independent_dates_create_separate_rows(db_session: Session) -> None:
    """다른 KST 날짜는 독립 row — 머지 대상 아님."""
    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [1]},
    )
    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE_OTHER,
        new_payload={CATEGORY_NEW: [2]},
    )
    db_session.commit()

    snap_today = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    snap_other = get_scrape_snapshot_by_date(db_session, _TEST_DATE_OTHER)

    assert snap_today is not None and snap_other is not None
    assert snap_today.id != snap_other.id
    assert snap_today.payload[CATEGORY_NEW] == [1]
    assert snap_other.payload[CATEGORY_NEW] == [2]


def test_upsert_transition_chain_collapses_to_single_to(db_session: Session) -> None:
    """검증 4 회귀: 같은 announcement 의 접수예정→접수중→마감 머지 후 t_마감 만 남음."""
    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={
            "transitioned_to_접수중": [{"id": 77, "from": "접수예정"}],
        },
    )
    db_session.commit()

    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={
            "transitioned_to_마감": [{"id": 77, "from": "접수중"}],
        },
    )
    db_session.commit()

    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is not None
    assert snap.payload["transitioned_to_접수중"] == []
    assert snap.payload["transitioned_to_마감"] == [{"id": 77, "from": "접수예정"}]


def test_upsert_correction_round_trip_purges_to_no_change(db_session: Session) -> None:
    """검증 5 회귀: 접수중→마감→접수중(정정) — 머지 후 transition 모두 비움."""
    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={"transitioned_to_마감": [{"id": 99, "from": "접수중"}]},
    )
    db_session.commit()

    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={"transitioned_to_접수중": [{"id": 99, "from": "마감"}]},
    )
    db_session.commit()

    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is not None
    for label in ("접수예정", "접수중", "마감"):
        assert snap.payload[f"transitioned_to_{label}"] == []
    for label in ("접수예정", "접수중", "마감"):
        assert snap.payload["counts"][f"transitioned_to_{label}"] == 0


def test_upsert_new_plus_transition_keeps_both(db_session: Session) -> None:
    """검증 6 회귀: 같은 공고 신규 + 전이 동시 — 둘 다 보존."""
    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [101]},
    )
    db_session.commit()

    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={"transitioned_to_마감": [{"id": 101, "from": "접수중"}]},
    )
    db_session.commit()

    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is not None
    assert snap.payload[CATEGORY_NEW] == [101]
    assert snap.payload["transitioned_to_마감"] == [{"id": 101, "from": "접수중"}]


def test_upsert_empty_run_creates_normalized_empty_row(db_session: Session) -> None:
    """빈 ScrapeRun (5종 모두 비어 있음) 도 row 를 만든다 (설계 §10.3)."""
    snapshot = upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={},
    )
    db_session.commit()

    assert snapshot.id is not None
    assert snapshot.payload[CATEGORY_NEW] == []
    assert snapshot.payload[CATEGORY_CONTENT_CHANGED] == []
    for label in ("접수예정", "접수중", "마감"):
        assert snapshot.payload[f"transitioned_to_{label}"] == []
        assert snapshot.payload["counts"][f"transitioned_to_{label}"] == 0


def test_upsert_rolls_back_on_transaction_failure(db_session: Session) -> None:
    """트랜잭션 실패 시 SQLAlchemy auto-rollback — snapshot row 가 영구화되지 않는다.

    검증 11 의 일반화 — apply_delta_to_main 트랜잭션 실패 시 snapshot 도 함께
    원상복구되어야 한다 (apply 와 같은 session 에서 호출되므로 자동 보장).
    """
    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [777]},
    )
    # 여기서 commit 하지 않고 의도적으로 rollback 한다 — apply 트랜잭션 실패 대응.
    db_session.rollback()

    # 새 query 로 재확인 — rollback 후 row 가 남아 있으면 안 된다.
    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is None


def test_get_by_date_returns_none_when_missing(db_session: Session) -> None:
    """존재하지 않는 날짜는 None 반환."""
    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is None


def test_upsert_id_lists_remain_sorted_after_multiple_runs(db_session: Session) -> None:
    """무작위 순서로 들어와도 머지 후 카테고리 ID 가 asc 정렬되어 있어야 한다."""
    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [50, 3, 17]},
    )
    db_session.commit()

    upsert_scrape_snapshot(
        db_session,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [25, 1]},
    )
    db_session.commit()

    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is not None
    assert snap.payload[CATEGORY_NEW] == [1, 3, 17, 25, 50]
