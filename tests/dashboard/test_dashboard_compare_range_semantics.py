"""대시보드 비교 구간 (from, to] 시맨틱 회귀 테스트 (task 00043-1).

배경 (사용자 원문):
    \"예를 들어 4/21 ~ 4/29 변화 측정이면 scrape_snapshots은 4/22 ~ 4/29 를
    누적해줘. 현재 4/21 ~ 4/28 누적인 느낌이 들어. 확인 후 내가 희망하는 기간
    비교가 이미 맞게 되어있다면 수정은 없어도 돼.\"

검증 의도:
    A 섹션이 누적 머지에 사용하는 ScrapeSnapshot 구간이 ``(from, to]`` (from
    배타 / to 포함) 라는 반-open 구간임을 코드와 회귀 테스트로 못박는다.
    사용자 예시 (compare_mode='custom', compare_date=2026-04-21,
    base_date=2026-04-29) 시:

        - effective_compare_date == 2026-04-21 (fallback 미발동).
        - 누적 대상 snapshot_date set == {2026-04-22, ..., 2026-04-29} 8 개.
        - 4/21 은 비교 baseline 으로 분리 사용되며 누적 대상에서 제외.
        - 4/29 는 기준일로 누적 대상에 포함.

    위 시맨틱은 ``app.db.repository.list_snapshots_in_range`` 의 SQL
    ``WHERE snapshot_date > :from_exclusive AND snapshot_date <= :to_inclusive``
    그리고 ``app.web.dashboard_section_a.build_section_a`` 의 호출부
    ``list_snapshots_in_range(from_exclusive=effective_compare_date,
    to_inclusive=base_date)`` 두 곳에서 보장된다.

    회귀 시나리오:
        - 누군가 ``from_exclusive`` 를 ``from_inclusive`` 로 바꾸면 4/21 의
          announcement 1 이 누적에 흡수되어 base_count 가 9 가 된다 — 본
          테스트가 8 임을 단언해 회귀를 잡는다.
        - 누군가 ``to_inclusive`` 를 ``to_exclusive`` 로 바꾸면 4/29 의
          announcement 9 가 빠져 base_count 가 7 이 된다 — 동일하게 잡힌다.

본 테스트 모듈은 사용자 원문 \"맞게 되어있다면 수정 없음\" 을 만족하는 회귀
가드만 추가한다 (코드 변경 없음).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    ScrapeSnapshot,
)
from app.db.repository import list_snapshots_in_range
from app.db.snapshot import CATEGORY_NEW, normalize_payload
from app.web.dashboard_section_a import build_section_a


# ---------------------------------------------------------------------------
# fixtures — tests/dashboard/test_dashboard_section_a.py 와 동일 패턴
# ---------------------------------------------------------------------------


@pytest.fixture
def session(test_engine: Engine) -> Iterator[Session]:
    """test_engine 위에 ORM 세션. 테스트 종료 시 close.

    conftest.py 의 db_session fixture 와 같지만, 테스트 모듈이 자체 fixture
    이름을 갖는 것이 dashboard 디렉터리 컨벤션이라 ``session`` 으로 노출한다
    (test_dashboard_section_a.py 와 동일).
    """
    from app.db.session import SessionLocal

    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()


def _insert_canonical_with_announcement(
    session: Session,
    *,
    announcement_index: int,
) -> Announcement:
    """canonical 1개 + is_current=True announcement 1개를 INSERT 한다.

    ``announcement_index`` 는 외부에서 의미를 부여하는 1 부터 시작하는 정수다
    (예: 9개 announcement 를 만들면 1..9). canonical_key / source_announcement_id
    는 인덱스로 유일성을 보장한다.

    Args:
        session:            호출자 세션.
        announcement_index: 시퀀스 번호 — title / canonical_key / source_announcement_id
                            에 모두 zero-padded 3 자리로 박힌다.

    Returns:
        flush 된 ``Announcement`` (PK 가 채워진 상태).
    """
    canonical = CanonicalProject(
        canonical_key=f"key-{announcement_index:03d}",
        key_scheme="official",
        representative_title=f"공고 {announcement_index}",
    )
    session.add(canonical)
    session.flush()

    announcement = Announcement(
        source_type="IRIS",
        source_announcement_id=f"A-{announcement_index:03d}",
        title=f"공고 {announcement_index}",
        agency="테스트기관",
        status=AnnouncementStatus.RECEIVING,
        received_at=None,
        deadline_at=None,
        scraped_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        canonical_group_id=canonical.id,
        canonical_key=f"key-{announcement_index:03d}",
    )
    session.add(announcement)
    session.flush()
    return announcement


def _insert_snapshot_with_single_new(
    session: Session,
    *,
    snapshot_date_obj: date,
    new_announcement_id: int,
) -> ScrapeSnapshot:
    """``snapshot_date`` 일자에 ``new=[announcement_id]`` 1건 짜리 snapshot 을 INSERT.

    A 섹션 누적 머지 결과의 ``new`` 카테고리가 일자별로 정확히 어떤
    announcement 가 흡수됐는지 확인하기 쉽도록, 각 일자마다 서로 다른
    announcement 1개만 들어가는 minimal payload 를 사용한다.

    Args:
        session:               호출자 세션.
        snapshot_date_obj:     snapshot_date (KST date).
        new_announcement_id:   해당 snapshot 의 ``new`` 리스트에 들어갈 ID.

    Returns:
        flush 된 ``ScrapeSnapshot``.
    """
    payload = normalize_payload({"new": [new_announcement_id]})
    snapshot = ScrapeSnapshot(
        snapshot_date=snapshot_date_obj,
        payload=payload,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


# ---------------------------------------------------------------------------
# 사용자 원문 예시 — compare=4/21, base=4/29 → 누적 대상 {4/22..4/29}
# ---------------------------------------------------------------------------


class TestCompareRangeSemanticsUserExample:
    """사용자 원문 \"4/21 ~ 4/29 변화 측정이면 scrape_snapshots 은 4/22 ~ 4/29\"
    회귀 가드.

    9개 announcement (id=1..9) + 9개 snapshot (4/21..4/29, 각 일자에 announcement_id
    하나씩 ``new`` 로 등장) 를 만들고 build_section_a 를 호출한다. 누적 결과의
    ``new`` 카테고리에 흡수된 announcement_id 집합을 통해 어떤 일자가 누적
    대상이었는지 역으로 추적한다.
    """

    def test_compare_range_excludes_compare_date_includes_base_date(
        self, session: Session
    ) -> None:
        """비교일(4/21) 은 누적 baseline 이라 제외, 기준일(4/29) 은 포함.

        9일 (4/21..4/29) snapshot 을 fixture 로 만들고, 비교 구간 (4/21, 4/29]
        의 누적 결과가 정확히 4/22..4/29 의 8개 snapshot 을 머지한 것임을
        확인한다.
        """
        # ── 1. 9개 announcement INSERT (id=1..9) ─────────────────────────
        # Auto-increment PK 라 INSERT 순서대로 id 가 1..9 로 부여된다 (SQLite/Postgres 공통).
        for index in range(1, 10):
            announcement = _insert_canonical_with_announcement(
                session, announcement_index=index
            )
            assert announcement.id == index, (
                f"sanity: announcement_index={index} 이 PK={index} 로 INSERT 돼야 함 — "
                f"실제 id={announcement.id}. 다른 fixture/테스트가 announcements 테이블을 "
                f"오염시켰는지 확인."
            )

        # ── 2. 9일치 snapshot INSERT — 일자 i 에 announcement_id i 를 new 로 ─
        # snapshot_date = 4/21 + (i-1) days, new = [i].
        compare_date = date(2026, 4, 21)
        base_date = date(2026, 4, 29)
        for offset_days in range(0, 9):  # 0..8
            snapshot_date_obj = compare_date + timedelta(days=offset_days)
            announcement_id_for_day = offset_days + 1  # 1..9
            _insert_snapshot_with_single_new(
                session,
                snapshot_date_obj=snapshot_date_obj,
                new_announcement_id=announcement_id_for_day,
            )

        # ── 3. 빌더 호출 — 사용자 예시 그대로 ────────────────────────────
        result = build_section_a(
            session,
            base_date=base_date,
            requested_compare_date=compare_date,
        )

        # ── 4. fallback 미발동 — 비교일(4/21) snapshot 이 가용 set 에 있음 ──
        assert result.fallback.is_no_data is False
        assert result.fallback.applied is False
        assert result.fallback.effective_compare_date == compare_date

        # ── 5. 핵심 단언: 누적 대상은 4/22..4/29 (8 개 snapshot) ────────
        # 4/21 의 announcement 1 은 비교 baseline 이라 누적 결과에서 제외.
        # 4/29 의 announcement 9 는 기준일이라 누적 결과에 포함.
        new_card = next(
            card for card in result.cards if card.category_key == CATEGORY_NEW
        )
        # base_count 는 머지된 payload.counts.new 에서 직접 — 8 이어야 한다.
        assert new_card.base_count == 8, (
            f"누적 대상 snapshot 수가 8 (4/22..4/29) 이 아닙니다 — "
            f"base_count={new_card.base_count}. (from, to] 반-open 시맨틱 회귀 가능성 "
            f"(예: from_exclusive→from_inclusive 로 바뀌면 9, "
            f"to_inclusive→to_exclusive 로 바뀌면 7)."
        )
        # compare_count 는 비교일 단일 snapshot 의 counts.new — 4/21 의 [1] 1 개.
        assert new_card.compare_count == 1
        assert new_card.delta == 7
        assert new_card.delta_direction == "up"

        # ── 6. 어느 announcement_id 가 누적됐는지 정밀 단언 ────────────
        # 4/22..4/29 → announcement_id 2..9 가 흡수돼야 한다 (4/21 의 1 은 제외).
        merged_ids = sorted(item.announcement_id for item in new_card.items)
        assert merged_ids == [2, 3, 4, 5, 6, 7, 8, 9]
        # SectionAData.merged_announcement_ids 도 같은 set 이어야 한다.
        assert result.merged_announcement_ids == [2, 3, 4, 5, 6, 7, 8, 9]
        # 4/21 의 announcement 1 이 누적에 끼지 않았음을 직접 확인.
        assert 1 not in result.merged_announcement_ids

    def test_list_snapshots_in_range_returns_eight_dates_for_user_example(
        self, session: Session
    ) -> None:
        """저레벨 헬퍼 직접 호출 — (4/21, 4/29] 은 정확히 4/22..4/29 8 개 일자 반환.

        ``list_snapshots_in_range`` 자체의 SQL 시맨틱 회귀 가드. build_section_a
        의 호출 인자 (from_exclusive=effective_compare_date, to_inclusive=base_date)
        결합과는 별개로, 헬퍼 한 함수의 ``WHERE snapshot_date > :from
        AND snapshot_date <= :to`` 가 의도대로 작동하는지 단독 검증한다.
        """
        compare_date = date(2026, 4, 21)
        base_date = date(2026, 4, 29)

        # 9일치 snapshot 만 INSERT (announcement INSERT 는 본 테스트에서 불필요 —
        # snapshot.payload 는 announcement_id 정합성 체크 없이 저장된다).
        for offset_days in range(0, 9):
            snapshot_date_obj = compare_date + timedelta(days=offset_days)
            payload = normalize_payload({"new": [offset_days + 1]})
            session.add(
                ScrapeSnapshot(snapshot_date=snapshot_date_obj, payload=payload)
            )
        session.flush()

        snapshots = list_snapshots_in_range(
            session,
            from_exclusive=compare_date,
            to_inclusive=base_date,
        )

        snapshot_dates = [snapshot.snapshot_date for snapshot in snapshots]
        # 정확히 4/22..4/29 8 개 일자, 오름차순 정렬.
        expected_dates = [
            date(2026, 4, 22),
            date(2026, 4, 23),
            date(2026, 4, 24),
            date(2026, 4, 25),
            date(2026, 4, 26),
            date(2026, 4, 27),
            date(2026, 4, 28),
            date(2026, 4, 29),
        ]
        assert snapshot_dates == expected_dates
        # 비교일 자체는 결과에 포함되지 않는다 (반-open).
        assert compare_date not in snapshot_dates
        # 기준일은 결과에 포함된다 (closed).
        assert base_date in snapshot_dates
