"""E2E 테스트용 DB 시드 헬퍼 (task 00043-4).

대시보드의 4개 점검 항목 (task 00043) 을 모두 자극하기 위한 최소 fixture 데이터를
한 함수로 묶어 시드한다.

시드 구조:
    - **9일치 ScrapeSnapshot** (2026-04-21 ~ 2026-04-29):
      각 일자 ``i`` 의 payload 는 ``new=[i]`` 1건. compare=2026-04-21,
      base=2026-04-29 비교 시 누적 대상이 정확히 4/22~4/29 (8 일) 임을 화면에서
      확인할 수 있다. (subtask 00043-1 의 (from, to] 시맨틱 회귀가 라이브 페이지
      에서도 작동함을 보장.)

    - **2 건의 \"조만간 접수될 공고\"** (status=접수예정, received_at 이 ``[base, base+30)``
      안):
      * received_at = 2026-04-30 KST → D-1 라벨.
      * received_at = 2026-05-04 KST → D-5 라벨.

    - **2 건의 \"조만간 마감될 공고\"** (status=접수중, deadline_at 이 같은 구간):
      * deadline_at = 2026-05-02 KST → D-3 라벨.
      * deadline_at = 2026-05-10 KST → D-11 라벨.

설계 메모:
    - 9개 announcement (id=1..9) 와 추가 4개 (id=10..13) 를 명시적으로 INSERT
      하지만, snapshot.payload 는 announcement_id 정합성을 SQL 레벨에서 강제하지
      않는다 (JSON 컬럼). 따라서 snapshot 의 ``new=[i]`` 가 실제 announcement
      를 가리킬 필요가 없지만, A 섹션의 expand items 가 announcement 메타를
      JOIN 으로 끌어오므로 실제 announcement 1..9 를 함께 INSERT 해야 \"공고 i\"
      라는 행 표시가 채워진다.

    - 모든 announcement 는 ``is_current=True`` 로 INSERT — repository 헬퍼들이
      이 조건을 강제한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    CanonicalProject,
    ScrapeSnapshot,
)
from app.db.snapshot import normalize_payload


# 9일치 snapshot 의 시드 일자 (오름차순 — index i+1 = announcement_id i+1).
SEED_SNAPSHOT_DATES: tuple[date, ...] = tuple(
    date(2026, 4, 21 + offset_days) for offset_days in range(9)
)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """UTC tz-aware ``datetime`` 빠른 생성 헬퍼.

    KST 자정 → UTC 환산: KST 하루 = UTC 의 전날 15:00.
    예: KST 2026-04-30 00:00 = UTC 2026-04-29 15:00.

    Args:
        year/month/day/hour/minute: ``datetime`` 의 각 필드 그대로.

    Returns:
        UTC tz-aware ``datetime``.
    """
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _insert_canonical_with_announcement(
    session: Session,
    *,
    canonical_key: str,
    title: str,
    source_announcement_id: str,
    status: AnnouncementStatus,
    received_at: datetime | None,
    deadline_at: datetime | None,
) -> Announcement:
    """canonical 1개 + is_current=True announcement 1개 INSERT 헬퍼.

    Args:
        session:                호출자 세션.
        canonical_key:          canonical 의 unique key (테스트 fixture 자체 정한 값).
        title:                  공고 제목 (UI 행 표시용).
        source_announcement_id: 소스 측 ID (UNIQUE 제약 회피용으로 매번 유일).
        status:                 ``AnnouncementStatus`` Enum 값.
        received_at:            접수 시작 시각 (UTC tz-aware) 또는 None.
        deadline_at:            마감 시각 (UTC tz-aware) 또는 None.

    Returns:
        flush 된 ``Announcement`` (PK 가 채워진 상태).
    """
    canonical = CanonicalProject(
        canonical_key=canonical_key,
        key_scheme="official",
        representative_title=title,
    )
    session.add(canonical)
    session.flush()

    announcement = Announcement(
        source_type="IRIS",
        source_announcement_id=source_announcement_id,
        title=title,
        agency="테스트기관",
        status=status,
        received_at=received_at,
        deadline_at=deadline_at,
        scraped_at=_utc(2026, 4, 1, 0, 0),
        canonical_group_id=canonical.id,
        canonical_key=canonical_key,
    )
    session.add(announcement)
    session.flush()
    return announcement


def _insert_snapshot(
    session: Session,
    *,
    snapshot_date_obj: date,
    payload_dict: dict[str, Any],
) -> ScrapeSnapshot:
    """``snapshot_date`` 일자에 ``payload`` 를 정규형으로 INSERT 한다.

    Args:
        session:           호출자 세션.
        snapshot_date_obj: snapshot_date (KST date).
        payload_dict:      ``payload`` 원본 dict (정규화 전).

    Returns:
        flush 된 ``ScrapeSnapshot``.
    """
    normalized = normalize_payload(payload_dict)
    snapshot = ScrapeSnapshot(
        snapshot_date=snapshot_date_obj,
        payload=normalized,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def seed_dashboard_e2e_data(session: Session) -> None:
    """대시보드 E2E 검증에 필요한 최소 시드 데이터를 한 번에 INSERT 한다.

    호출 규약:
        - 빈 DB 위에서 호출되어야 한다 (Alembic upgrade head 가 끝난 직후).
        - 본 함수는 flush 까지만 수행 — 호출자가 ``commit()`` 한다.

    시드 결과 (announcement.id 는 INSERT 순서대로 1..13):
        1..9: A 섹션 누적 머지 카드의 expand items 로 보이는 9개 공고
              (snapshot 일자 i 에 ``new=[i]`` 로 등장).
        10..11: B 섹션 \"조만간 접수될 공고\" 후보 (status=접수예정).
        12..13: B 섹션 \"조만간 마감될 공고\" 후보 (status=접수중).

    Args:
        session: 호출자 세션.
    """
    # ── A 섹션용 — 9개 announcement (id=1..9) ────────────────────────
    # snapshot 의 new=[i] 가 가리키는 announcement 를 실제로 INSERT 해야 A 섹션
    # expand items 가 메타와 함께 표시된다.
    for index in range(1, 10):
        _insert_canonical_with_announcement(
            session,
            canonical_key=f"e2e-key-{index:03d}",
            title=f"공고 {index}",
            source_announcement_id=f"E2E-A-{index:03d}",
            # 접수중 상태로 둬도 A 섹션 카드는 정상 — payload.new 카테고리만 본다.
            status=AnnouncementStatus.RECEIVING,
            received_at=None,
            deadline_at=None,
        )

    # ── B 섹션 \"조만간 접수될 공고\" — 2 건 (id=10, 11) ────────────────
    # base_date = 2026-04-29 → 구간 [2026-04-29 KST, 2026-05-29 KST).
    # received_at = 2026-04-30 KST 00:00 → D-1 (today=2026-04-29 일 때).
    _insert_canonical_with_announcement(
        session,
        canonical_key="e2e-key-soon-open-001",
        title="접수예정 공고 D-1",
        source_announcement_id="E2E-O-001",
        status=AnnouncementStatus.SCHEDULED,
        received_at=_utc(2026, 4, 29, 15, 0),  # KST 2026-04-30 00:00.
        deadline_at=_utc(2026, 5, 14, 15, 0),  # KST 2026-05-15 (참조용).
    )
    # received_at = 2026-05-04 KST 00:00 → D-5.
    _insert_canonical_with_announcement(
        session,
        canonical_key="e2e-key-soon-open-002",
        title="접수예정 공고 D-5",
        source_announcement_id="E2E-O-002",
        status=AnnouncementStatus.SCHEDULED,
        received_at=_utc(2026, 5, 3, 15, 0),  # KST 2026-05-04 00:00.
        deadline_at=_utc(2026, 5, 20, 15, 0),
    )

    # ── B 섹션 \"조만간 마감될 공고\" — 2 건 (id=12, 13) ────────────────
    # deadline_at = 2026-05-02 KST 00:00 → D-3.
    _insert_canonical_with_announcement(
        session,
        canonical_key="e2e-key-soon-close-001",
        title="마감예정 공고 D-3",
        source_announcement_id="E2E-C-001",
        status=AnnouncementStatus.RECEIVING,
        received_at=_utc(2026, 4, 1, 0, 0),
        deadline_at=_utc(2026, 5, 1, 15, 0),  # KST 2026-05-02 00:00.
    )
    # deadline_at = 2026-05-10 KST 00:00 → D-11.
    _insert_canonical_with_announcement(
        session,
        canonical_key="e2e-key-soon-close-002",
        title="마감예정 공고 D-11",
        source_announcement_id="E2E-C-002",
        status=AnnouncementStatus.RECEIVING,
        received_at=_utc(2026, 4, 1, 0, 0),
        deadline_at=_utc(2026, 5, 9, 15, 0),  # KST 2026-05-10 00:00.
    )

    # ── 9개 ScrapeSnapshot — 일자 i 에 announcement i 가 new 로 등장 ───
    for offset_days, snapshot_date_obj in enumerate(SEED_SNAPSHOT_DATES):
        announcement_id_for_day = offset_days + 1  # 1..9.
        _insert_snapshot(
            session,
            snapshot_date_obj=snapshot_date_obj,
            payload_dict={"new": [announcement_id_for_day]},
        )


__all__ = [
    "SEED_SNAPSHOT_DATES",
    "seed_dashboard_e2e_data",
]
