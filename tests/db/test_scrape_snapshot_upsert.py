"""ScrapeSnapshot INSERT 통합 테스트 (task 00150-1).

검증 대상: ``app/db/repository.py`` 의 ``insert_scrape_snapshot`` /
``get_scrape_snapshot_by_date``.

task 00150-1 에서 snapshot 저장이 \"같은 KST 날짜 1 row UPSERT + 머지\" 에서
\"매 ScrapeRun 마다 1 row INSERT\" 로 전환됐다. 본 모듈은 그 새 시맨틱을 회귀
보호한다:

    - 매 호출마다 row 가 1개씩 증가한다 (머지 발생 안 함).
    - 같은 KST 날짜의 multi-row 가 각자 다른 ``created_at`` 을 갖는다.
    - ``get_scrape_snapshot_by_date`` 가 multi-row 환경에서 ``merge_snapshot_payload``
      reduce 결과로 단일 가상 row 를 반환한다 (dashboard reduce 경로 호환성).
    - UNIQUE(``scrape_run_id``) 가 \"같은 run 에 대한 snapshot 중복 INSERT\" 를
      DB 가 막는다 — 호출부 회귀의 최종 방어선.

순수 머지 룰 자체는 ``test_snapshot_merge.py`` 가 검증하므로, 여기서는 DB 경로
의 INSERT 동작 + 트랜잭션 atomic + multi-row reduce 에만 초점을 둔다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ScrapeRun, ScrapeSnapshot
from app.db.repository import (
    get_scrape_snapshot_by_date,
    insert_scrape_snapshot,
)
from app.db.snapshot import (
    CATEGORY_CONTENT_CHANGED,
    CATEGORY_NEW,
)


_TEST_DATE = date(2026, 4, 28)
_TEST_DATE_OTHER = date(2026, 4, 29)


def _make_scrape_run(session: Session, *, ended_at: datetime | None = None) -> ScrapeRun:
    """테스트용 정상 종료 ScrapeRun 1건을 INSERT 한다.

    ``insert_scrape_snapshot`` 이 NOT NULL FK 로 ``scrape_run_id`` 를 요구하므로
    각 snapshot 마다 별도 ScrapeRun 을 만들어 전달한다.
    UNIQUE(``scrape_run_id``) 제약을 만족하려면 snapshot 1건당 ScrapeRun 1건이
    필요하다.

    Args:
        session: 호출자 세션.
        ended_at: ScrapeRun.ended_at. None 이면 현재 시각.

    Returns:
        flush 된 ``ScrapeRun`` 인스턴스. id 가 채워져 있다.
    """
    ended = ended_at if ended_at is not None else datetime.now(tz=UTC)
    scrape_run = ScrapeRun(
        started_at=ended - timedelta(minutes=1),
        ended_at=ended,
        status="completed",
        trigger="cli",
        source_counts={},
    )
    session.add(scrape_run)
    session.flush()
    return scrape_run


def test_insert_creates_row_with_normalized_payload(db_session: Session) -> None:
    """신규 INSERT — 빈 카테고리도 정규형으로 채움."""
    scrape_run = _make_scrape_run(db_session)

    snapshot = insert_scrape_snapshot(
        db_session,
        scrape_run_id=scrape_run.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [10, 20]},
    )
    db_session.commit()

    assert snapshot.id is not None
    assert snapshot.scrape_run_id == scrape_run.id
    assert snapshot.snapshot_date == _TEST_DATE
    assert snapshot.payload[CATEGORY_NEW] == [10, 20]
    # 빈 카테고리 채움 — view 가 KeyError 없이 0건도 표시.
    assert snapshot.payload[CATEGORY_CONTENT_CHANGED] == []
    assert snapshot.payload["transitioned_to_마감"] == []
    assert snapshot.payload["counts"][CATEGORY_NEW] == 2
    assert snapshot.payload["counts"][CATEGORY_CONTENT_CHANGED] == 0


def test_insert_same_date_creates_separate_rows_with_distinct_created_at(
    db_session: Session,
) -> None:
    """같은 KST 날짜의 후속 호출은 머지하지 않고 별개 row 를 INSERT 한다 (task 00150 회귀 가드).

    이전 (UPSERT 머지) 설계에서는 같은 ``snapshot_date`` 의 2번째 호출이 기존
    row 의 payload 를 in-place UPDATE 했지만, task 00150-1 이후로는 별개 row 로
    INSERT 된다. 각 row 의 ``created_at`` 이 별개라서 daily report 시간 필터
    회귀가 자연 해소된다.

    검증:
        - 같은 ``snapshot_date`` 에 row 2건이 생긴다 (1건이 머지 UPDATE 가 아님).
        - 두 row 의 ``created_at`` 이 의도된 시각으로 별개 저장됐다.
        - 두 row 의 ``scrape_run_id`` 가 별개 (1 run = 1 snapshot 보장).
    """
    earlier = datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC)
    later = datetime(2026, 4, 28, 7, 2, 0, tzinfo=UTC)

    run_earlier = _make_scrape_run(db_session, ended_at=earlier)
    snap_earlier = insert_scrape_snapshot(
        db_session,
        scrape_run_id=run_earlier.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [10]},
    )
    # ORM Python default 가 적용되지 않도록 created_at 을 직접 주입한다 —
    # 테스트가 정확한 시각 비교를 하기 위함.
    snap_earlier.created_at = earlier
    db_session.flush()

    run_later = _make_scrape_run(db_session, ended_at=later)
    snap_later = insert_scrape_snapshot(
        db_session,
        scrape_run_id=run_later.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [20], CATEGORY_CONTENT_CHANGED: [30]},
    )
    snap_later.created_at = later
    db_session.flush()
    db_session.commit()

    rows = list(
        db_session.execute(
            select(ScrapeSnapshot)
            .where(ScrapeSnapshot.snapshot_date == _TEST_DATE)
            .order_by(ScrapeSnapshot.created_at.asc())
        )
        .scalars()
    )
    # 머지 UPDATE 가 아니라 별개 INSERT — 2건이어야 한다.
    assert len(rows) == 2
    assert rows[0].scrape_run_id == run_earlier.id
    assert rows[1].scrape_run_id == run_later.id
    # 각 row 의 payload 는 자기 호출의 입력만 들어 있다 (머지 발생 안 함).
    assert rows[0].payload[CATEGORY_NEW] == [10]
    assert rows[0].payload[CATEGORY_CONTENT_CHANGED] == []
    assert rows[1].payload[CATEGORY_NEW] == [20]
    assert rows[1].payload[CATEGORY_CONTENT_CHANGED] == [30]
    # created_at 이 별개 시각으로 저장됐다.
    from app.db.models import as_utc

    assert as_utc(rows[0].created_at) == earlier
    assert as_utc(rows[1].created_at) == later


def test_insert_independent_dates_create_separate_rows(db_session: Session) -> None:
    """다른 KST 날짜는 당연히 독립 row."""
    run_a = _make_scrape_run(db_session)
    run_b = _make_scrape_run(db_session)

    insert_scrape_snapshot(
        db_session,
        scrape_run_id=run_a.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [1]},
    )
    insert_scrape_snapshot(
        db_session,
        scrape_run_id=run_b.id,
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


def test_get_by_date_reduce_merges_multi_row_payload(db_session: Session) -> None:
    """``get_scrape_snapshot_by_date`` 가 multi-row 를 reduce 머지해 단일 가상 결과를 반환.

    task 00150-1 이후 같은 KST 날짜에 row 가 여러 개 존재할 수 있다. dashboard
    의 \"비교 baseline 일자의 누적 상태\" 호출 의도를 유지하려면 같은 날의 모든
    row 를 ``merge_snapshot_payload`` 로 reduce 머지한 결과가 반환되어야 한다.
    """
    run_a = _make_scrape_run(
        db_session, ended_at=datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC)
    )
    snap_a = insert_scrape_snapshot(
        db_session,
        scrape_run_id=run_a.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [10, 20]},
    )
    snap_a.created_at = datetime(2026, 4, 28, 0, 0, 0, tzinfo=UTC)
    db_session.flush()

    run_b = _make_scrape_run(
        db_session, ended_at=datetime(2026, 4, 28, 7, 2, 0, tzinfo=UTC)
    )
    snap_b = insert_scrape_snapshot(
        db_session,
        scrape_run_id=run_b.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [30], CATEGORY_CONTENT_CHANGED: [99]},
    )
    snap_b.created_at = datetime(2026, 4, 28, 7, 2, 0, tzinfo=UTC)
    db_session.flush()
    db_session.commit()

    merged = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert merged is not None
    # reduce 머지 — new union, content_changed 도 합산.
    assert merged.payload[CATEGORY_NEW] == [10, 20, 30]
    assert merged.payload[CATEGORY_CONTENT_CHANGED] == [99]
    assert merged.payload["counts"][CATEGORY_NEW] == 3
    assert merged.payload["counts"][CATEGORY_CONTENT_CHANGED] == 1


def test_insert_empty_run_creates_normalized_empty_row(db_session: Session) -> None:
    """빈 ScrapeRun (5종 모두 비어 있음) 도 row 를 만든다 (설계 §10.3)."""
    scrape_run = _make_scrape_run(db_session)
    snapshot = insert_scrape_snapshot(
        db_session,
        scrape_run_id=scrape_run.id,
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


def test_insert_rolls_back_on_transaction_failure(db_session: Session) -> None:
    """트랜잭션 실패 시 SQLAlchemy auto-rollback — snapshot row 가 영구화되지 않는다.

    apply_delta_to_main 트랜잭션 실패 시 snapshot 도 함께 원상복구되어야 한다
    (apply 와 같은 session 에서 호출되므로 자동 보장).
    """
    scrape_run = _make_scrape_run(db_session)
    insert_scrape_snapshot(
        db_session,
        scrape_run_id=scrape_run.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [777]},
    )
    # 의도적으로 commit 하지 않고 rollback — apply 트랜잭션 실패를 모사.
    db_session.rollback()

    # rollback 후 row 가 남아 있지 않다.
    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is None


def test_get_by_date_returns_none_when_missing(db_session: Session) -> None:
    """존재하지 않는 날짜는 None 반환."""
    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is None


def test_insert_unique_scrape_run_id_blocks_duplicate(db_session: Session) -> None:
    """같은 ScrapeRun.id 에 대한 snapshot 중복 INSERT 는 DB UNIQUE 제약이 막는다.

    task 00150-1 의 1 ScrapeRun = 1 snapshot row 회귀 가드. 호출부 코드 실수로
    같은 ScrapeRun 컨텍스트에서 ``insert_scrape_snapshot`` 이 2번 호출되어도
    데이터가 중복 누적되지 않도록 DB 가 막는다.
    """
    scrape_run = _make_scrape_run(db_session)
    insert_scrape_snapshot(
        db_session,
        scrape_run_id=scrape_run.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [1]},
    )
    db_session.commit()

    # 같은 scrape_run_id 로 두 번째 INSERT → UNIQUE 위반 IntegrityError.
    with pytest.raises(IntegrityError):
        insert_scrape_snapshot(
            db_session,
            scrape_run_id=scrape_run.id,
            snapshot_date=_TEST_DATE,
            new_payload={CATEGORY_NEW: [2]},
        )
        db_session.commit()
    db_session.rollback()


def test_insert_id_list_remains_sorted_within_single_row(db_session: Session) -> None:
    """무작위 순서로 들어와도 정규화 후 카테고리 ID 가 asc 정렬되어 있어야 한다."""
    scrape_run = _make_scrape_run(db_session)
    insert_scrape_snapshot(
        db_session,
        scrape_run_id=scrape_run.id,
        snapshot_date=_TEST_DATE,
        new_payload={CATEGORY_NEW: [50, 3, 17, 1, 25]},
    )
    db_session.commit()

    snap = get_scrape_snapshot_by_date(db_session, _TEST_DATE)
    assert snap is not None
    assert snap.payload[CATEGORY_NEW] == [1, 3, 17, 25, 50]
