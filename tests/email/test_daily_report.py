"""``compute_aggregation_window`` + ``aggregate_snapshots`` + 공용 dataclass 단위 테스트.

검증 대상 (subtask 00125-3 / 00125-4 의 acceptance_criteria + design note §1·§2·
§4·§14):

    1. ``last_sent_at`` SystemSetting row 없음 → ``is_first_send=True`` ,
       ``fallback_days=7`` 적용, ``from_dt = now - 7d``.
    2. ``last_sent_at`` 있음 + 구간 내 snapshot 0 건 → None 반환.
    3. ``last_sent_at`` 있음 + 구간 내 snapshot N 건 → ``AggregationWindow``
       (``snapshot_count=N`` , ``is_first_send=False`` , ``fallback_days=None``).

추가 회귀 보호 (가드 — design note §1 의 \"created_at 채택\" + guidance \"parsing
실패 시 NULL 과 동일 취급\"):

    4. ``last_sent_at`` 빈 문자열 (``DEFAULT_DAILY_REPORT_LAST_SENT_AT``) → 첫
       발송 분기와 동일.
    5. ``last_sent_at`` 파싱 실패 (손상 값) → 첫 발송 분기로 회복.
    6. ``last_sent_at`` 있음 + 구간 내 snapshot 0 + fallback 구간 7일에는 row
       존재 → None (fallback 으로 늘어나지 않음 — 일반 발송 모드는 last_sent_at
       만 본다).
    7. 구간 경계 시맨틱 — ``from_dt`` 는 배타, ``to_dt`` 는 포함.
    8. dataclass 기본 정책 — ``frozen=False`` 임을 확인 (인스턴스 필드 mutate
       가능).

``aggregate_snapshots`` (subtask 00125-4 의 acceptance_criteria + prompt §3):

    A. 단일 snapshot payload → 5종 카테고리 직접 추출 + ``AnnouncementSummary``
       필드 매핑 검증.
    B. 2개 snapshot 연속 머지 → ``new`` set union (검증 6 형식의 회귀 가드).
    C. 2개 snapshot 연속 머지 → transition \"first from 유지 + last to 갱신\"
       (prompt §3 검증 4 의 머지 규칙).
    D. ``from == to`` 가 발생한 정정 케이스 → 어떤 카테고리에도 포함되지 않음
       (prompt §3 검증 5 의 머지 규칙).
    E. 메타 누락 announcement_id → silent skip (dashboard 패턴).
    F. ``is_current=False`` 인 history row 도 포함 (subtask guidance 명시).
    G. ``detail_url`` 은 announcement row 의 PK 기준으로 조립 + SystemSetting
       ``app.public_base_url`` 사용.
    H. 빈 payload 구간 (snapshot 0건) 도 안전하게 빈 ``AggregatedSnapshotPayload``
       반환 (defensive 가드).
    I. ``total_count`` 는 5 리스트 길이의 합 — 중복 제거 없음.
    J. announcement 일괄 조회는 단일 SELECT 로 처리 (N+1 회귀 가드 —
       guidance \"announcement 일괄 조회는 단일 SELECT 로 N+1 회피\").

DB:
    tests/conftest.py 의 ``test_engine`` + ``db_session`` fixture 사용.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.backup.service import set_setting
from app.db.models import Announcement, AnnouncementStatus, ScrapeSnapshot
from app.db.snapshot import normalize_payload
from app.email.constants import (
    DEFAULT_APP_PUBLIC_BASE_URL,
    SETTING_KEY_APP_PUBLIC_BASE_URL,
    SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
)
from app.email.daily_report import (
    AggregatedSnapshotPayload,
    AggregationWindow,
    AnnouncementSummary,
    aggregate_snapshots,
    compute_aggregation_window,
)


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _insert_snapshot_with_created_at(
    session: Session,
    *,
    created_at: datetime,
    snapshot_date_iso: str,
    payload: dict | None = None,
) -> ScrapeSnapshot:
    """ScrapeSnapshot 을 명시적 ``created_at`` 으로 INSERT 한다.

    ScrapeSnapshot.created_at 은 ORM ``default=_utcnow`` 를 갖지만 호출자가
    명시 값을 전달하면 그 값이 그대로 INSERT 된다 (default 는 미전달 시에만
    적용). 테스트는 발송 구간 경계의 직전/직후를 정밀히 재현하기 위해 created_at
    을 직접 주입한다.

    Args:
        session:           대상 ORM 세션.
        created_at:       UTC tz-aware datetime. 본 row 의 ``created_at`` 컬럼
                          에 그대로 들어간다.
        snapshot_date_iso: ``YYYY-MM-DD`` 문자열. UNIQUE 제약을 만족해야 하므로
                          테스트 케이스마다 다른 값을 전달한다.
        payload:          ScrapeSnapshot.payload 에 들어갈 dict. None 이면 빈
                          payload 정규형으로 채운다.

    Returns:
        flush 된 ``ScrapeSnapshot`` 인스턴스.
    """
    from datetime import date

    snapshot = ScrapeSnapshot(
        snapshot_date=date.fromisoformat(snapshot_date_iso),
        created_at=created_at,
        payload=normalize_payload(payload),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


# ──────────────────────────────────────────────────────────────
# 1. last_sent_at NULL → 첫 발송 + fallback_days=7
# ──────────────────────────────────────────────────────────────


def test_compute_window_first_send_uses_fallback_days(db_session: Session) -> None:
    """``last_sent_at`` SystemSetting row 없음 → 직전 7일 구간 + is_first_send=True.

    검증:
        - ``is_first_send`` is True
        - ``fallback_days`` == 7 (default)
        - ``from_dt`` == ``now - 7d`` (UTC tz-aware)
        - ``to_dt`` == ``now``
        - ``snapshot_count`` >= 1 (fallback 7일 안에 row 가 1건 이상이면 정상)
    """
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)

    # fallback 7일 구간 안의 snapshot 1건 (now - 1일 시점).
    _insert_snapshot_with_created_at(
        db_session,
        created_at=now - timedelta(days=1),
        snapshot_date_iso="2026-05-18",
        payload={"new": [101]},
    )
    db_session.commit()

    # last_sent_at SystemSetting 은 의도적으로 set 하지 않는다 (row 부재).
    window = compute_aggregation_window(db_session, now=now)

    assert window is not None, "fallback 7일 안에 row 가 있으면 None 이 아니어야 함"
    assert isinstance(window, AggregationWindow)
    assert window.is_first_send is True
    assert window.fallback_days == 7
    assert window.from_dt == now - timedelta(days=7)
    assert window.to_dt == now
    assert window.snapshot_count == 1


def test_compute_window_first_send_custom_fallback_days(db_session: Session) -> None:
    """``fallback_days`` 인자를 명시적으로 전달하면 그 값이 적용된다.

    default 7 의존을 끊고 \"인자가 from_dt 계산에 반영되는가\" 만 검증한다.
    """
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)

    # custom fallback 3일 안에 row 1건.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=now - timedelta(days=2),
        snapshot_date_iso="2026-05-17",
        payload={"new": [201]},
    )
    db_session.commit()

    window = compute_aggregation_window(db_session, now=now, fallback_days=3)

    assert window is not None
    assert window.is_first_send is True
    assert window.fallback_days == 3
    assert window.from_dt == now - timedelta(days=3)
    assert window.snapshot_count == 1


# ──────────────────────────────────────────────────────────────
# 2. last_sent_at 있음 + 구간 내 snapshot 0 → None
# ──────────────────────────────────────────────────────────────


def test_compute_window_returns_none_when_no_snapshots_in_range(
    db_session: Session,
) -> None:
    """``last_sent_at`` 있음 + ``(last_sent_at, now]`` 안의 snapshot 0 건 → None.

    검증:
        - ``(last_sent_at, now]`` 구간 밖 (직전) row 는 카운트에서 제외.
        - ``from_dt`` 는 배타이므로 \"정확히 last_sent_at 시각\" 의 row 도 제외.
        - 결과적으로 0 건 → ``None`` 반환.
    """
    last_sent = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)

    # row 1: 구간 시작 직전 (배타이므로 제외되어야 함).
    _insert_snapshot_with_created_at(
        db_session,
        created_at=last_sent - timedelta(minutes=1),
        snapshot_date_iso="2026-05-17",
        payload={"new": [301]},
    )
    # row 2: from_dt 와 정확히 같은 시각 (배타 → 제외).
    _insert_snapshot_with_created_at(
        db_session,
        created_at=last_sent,
        snapshot_date_iso="2026-05-18",
        payload={"new": [302]},
    )

    set_setting(
        db_session,
        SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
        last_sent.isoformat(),
    )
    db_session.commit()

    window = compute_aggregation_window(db_session, now=now)

    assert window is None, (
        "from_dt 배타 / 구간 밖 row 만 존재 → None 이어야 함"
    )


# ──────────────────────────────────────────────────────────────
# 3. last_sent_at 있음 + 구간 내 snapshot N → AggregationWindow
# ──────────────────────────────────────────────────────────────


def test_compute_window_counts_snapshots_in_range(db_session: Session) -> None:
    """``last_sent_at`` 있음 + 구간 내 N건 → AggregationWindow(snapshot_count=N).

    검증:
        - ``is_first_send`` is False
        - ``fallback_days`` is None
        - ``from_dt`` == 파싱된 last_sent_at
        - ``to_dt`` == now
        - ``snapshot_count`` == 3 (구간 안 3건)
        - 구간 밖 (직전) row 1건은 카운트에 포함되지 않음
        - ``to_dt`` 와 정확히 같은 시각 row 는 포함 (closed 경계)
    """
    last_sent = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)

    # 구간 밖 (배타 경계 직전) — 제외.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=last_sent - timedelta(seconds=1),
        snapshot_date_iso="2026-05-17",
        payload={"new": [401]},
    )
    # 구간 안 row 3건. ScrapeSnapshot 의 UNIQUE(snapshot_date) 를 만족하기 위해
    # 각 row 의 snapshot_date 를 서로 다른 일자(2026-05-19/20/21) 로 둔다.
    for index, hour_offset in enumerate([3, 12, 23], start=1):
        _insert_snapshot_with_created_at(
            db_session,
            created_at=last_sent + timedelta(hours=hour_offset),
            snapshot_date_iso=f"2026-05-{17 + index + 1:02d}",
            payload={"new": [410 + index]},
        )
    # to_dt 와 정확히 같은 시각 — closed 경계라 포함.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=now,
        snapshot_date_iso="2026-05-22",
        payload={"new": [499]},
    )

    set_setting(
        db_session,
        SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
        last_sent.isoformat(),
    )
    db_session.commit()

    window = compute_aggregation_window(db_session, now=now)

    assert window is not None
    assert window.is_first_send is False
    assert window.fallback_days is None
    assert window.from_dt == last_sent
    assert window.to_dt == now
    # 구간 안 3건 + to_dt 경계 1건 = 4
    assert window.snapshot_count == 4


# ──────────────────────────────────────────────────────────────
# 4. last_sent_at 빈 문자열 → 첫 발송 분기
# ──────────────────────────────────────────────────────────────


def test_compute_window_empty_last_sent_at_treated_as_first_send(
    db_session: Session,
) -> None:
    """``last_sent_at`` row 가 빈 문자열 (default 값) 이면 첫 발송 분기로 빠진다.

    ``DEFAULT_DAILY_REPORT_LAST_SENT_AT = \"\"`` 이라 행 자체는 있고 값만 빈
    경우가 운영 중 발생한다. 그 케이스가 row 없음(None) 과 동일하게 동작해야
    한다.
    """
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)

    _insert_snapshot_with_created_at(
        db_session,
        created_at=now - timedelta(hours=1),
        snapshot_date_iso="2026-05-18",
        payload={"new": [501]},
    )
    set_setting(db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT, "")
    db_session.commit()

    window = compute_aggregation_window(db_session, now=now)

    assert window is not None
    assert window.is_first_send is True
    assert window.fallback_days == 7
    assert window.snapshot_count == 1


# ──────────────────────────────────────────────────────────────
# 5. last_sent_at 파싱 실패 → 첫 발송 분기로 회복
# ──────────────────────────────────────────────────────────────


def test_compute_window_invalid_last_sent_at_falls_back_to_first_send(
    db_session: Session,
) -> None:
    """ISO-8601 로 파싱 불가능한 손상 값 → 첫 발송 분기로 회복 + warning.

    설계 결정 (guidance \"parsing 실패 시 NULL 과 동일 취급\"):
        잘못된 값으로 무한 SKIPPED 가 발생하지 않게 fallback. 운영자가 직접
        SystemSetting 을 수정하다 깨뜨린 경우 등을 방어한다.
    """
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)

    _insert_snapshot_with_created_at(
        db_session,
        created_at=now - timedelta(hours=2),
        snapshot_date_iso="2026-05-18",
        payload={"new": [601]},
    )
    # 손상 값 — fromisoformat 가 ValueError.
    set_setting(
        db_session,
        SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
        "not-a-valid-iso-datetime",
    )
    db_session.commit()

    window = compute_aggregation_window(db_session, now=now)

    assert window is not None
    assert window.is_first_send is True
    assert window.fallback_days == 7


# ──────────────────────────────────────────────────────────────
# 6. last_sent_at 모드는 fallback 으로 구간을 늘리지 않는다
# ──────────────────────────────────────────────────────────────


def test_compute_window_does_not_expand_to_fallback_when_last_sent_present(
    db_session: Session,
) -> None:
    """``last_sent_at`` 모드는 구간이 0건이어도 fallback 으로 확장하지 않는다.

    fallback_days 분기는 \"last_sent_at 부재\" 케이스 전용. 정상 운영 모드에서
    구간 안 row 가 0건이면 그대로 SKIPPED 처리되어야 한다 — fallback 으로 늘리면
    last_sent_at 갱신 정책이 어긋난다 (design note §7).
    """
    # last_sent_at 은 가까운 과거 1시간 전. 그 구간 안 row 없음.
    last_sent = datetime(2026, 5, 19, 11, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)

    # fallback 7일 안 (그러나 last_sent 보다는 과거) — last_sent 모드에서는 무시.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=now - timedelta(days=3),
        snapshot_date_iso="2026-05-16",
        payload={"new": [701]},
    )

    set_setting(
        db_session,
        SETTING_KEY_DAILY_REPORT_LAST_SENT_AT,
        last_sent.isoformat(),
    )
    db_session.commit()

    window = compute_aggregation_window(db_session, now=now)

    assert window is None, (
        "last_sent_at 있음 + 구간 안 0건 → None (fallback 으로 확장하지 않음)"
    )


# ──────────────────────────────────────────────────────────────
# 7. 시간 비교 — naive now 입력도 UTC tz-aware 로 정규화
# ──────────────────────────────────────────────────────────────


def test_compute_window_normalizes_naive_now_to_utc(db_session: Session) -> None:
    """naive ``now`` 가 들어와도 UTC tz-aware 로 자동 정규화되어 비교 안전.

    SQLite 백엔드 + DateTime(timezone=True) 가 SELECT 시 tz 정보를 잃는 환경
    이라 비교 직전 ``as_utc`` 로 정규화하는 게 본 모듈의 컨벤션. 호출자가
    실수로 naive 를 넘겨도 TypeError 가 나지 않아야 한다.
    """
    naive_now = datetime(2026, 5, 19, 0, 0, 0)  # tzinfo=None
    aware_now = naive_now.replace(tzinfo=UTC)

    _insert_snapshot_with_created_at(
        db_session,
        created_at=aware_now - timedelta(hours=1),
        snapshot_date_iso="2026-05-18",
        payload={"new": [801]},
    )
    db_session.commit()

    # naive 입력 — 예외 없이 처리되어야 함.
    window = compute_aggregation_window(db_session, now=naive_now)

    assert window is not None
    # to_dt 는 UTC tz-aware 로 정규화되어 있어야 비교 가능.
    assert window.to_dt.tzinfo is not None
    assert window.to_dt == aware_now


# ──────────────────────────────────────────────────────────────
# 8. dataclass 정책 — frozen=False (mutate 가능)
# ──────────────────────────────────────────────────────────────


def test_dataclasses_are_not_frozen() -> None:
    """3종 dataclass 모두 ``frozen=False`` 라 필드 mutate 가 가능하다.

    subtask guidance: \"dataclass 는 @dataclass(frozen=False) — 사용 시점에 mutate
    안함\". 정책 자체는 mutate 안하지만, frozen=True 로 설정되어 향후 디버깅
    / 보정 경로가 막히는 일을 막기 위한 회귀 가드.
    """
    window = AggregationWindow(
        from_dt=datetime(2026, 5, 18, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, tzinfo=UTC),
        snapshot_count=1,
        is_first_send=False,
        fallback_days=None,
    )
    # FrozenInstanceError 가 안 나야 한다.
    window.snapshot_count = 2
    assert window.snapshot_count == 2

    summary = AnnouncementSummary(
        announcement_id=1,
        canonical_project_id=None,
        title="t",
        source_type="iris",
        agency=None,
        deadline_at=None,
        detail_url="https://example.com/a/1",
    )
    summary.title = "변경"
    assert summary.title == "변경"

    payload = AggregatedSnapshotPayload(
        new=[],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=0,
    )
    payload.total_count = 5
    assert payload.total_count == 5


# ──────────────────────────────────────────────────────────────
# 9. AggregatedSnapshotPayload 필드 매핑 회귀 가드
# ──────────────────────────────────────────────────────────────


def test_aggregated_snapshot_payload_field_names() -> None:
    """5종 카테고리 dataclass 필드명이 design note §3 매핑과 일치한다.

    필드명 오타가 본문 빌더 (subtask 00125-5) 의 카테고리 매핑을 어긋나게
    하는 사고를 막는 가드. 필드명을 바꿔야 하면 design note §3 표와 본 테스트
    + 본문 빌더의 매핑 dict 를 같이 갱신해야 한다.
    """
    field_names = set(AggregatedSnapshotPayload.__dataclass_fields__.keys())
    expected = {
        "new",
        "content_changed",
        "transitioned_to_received_scheduled",
        "transitioned_to_receiving",
        "transitioned_to_closed",
        "total_count",
    }
    assert field_names == expected


# ──────────────────────────────────────────────────────────────
# aggregate_snapshots — 누적 머지 + AnnouncementSummary 빌드
# ──────────────────────────────────────────────────────────────


def _insert_announcement(
    session: Session,
    *,
    source_announcement_id: str,
    title: str = "테스트 공고",
    source_type: str = "IRIS",
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
    agency: str | None = "기관A",
    deadline_at: datetime | None = None,
    canonical_group_id: int | None = None,
    is_current: bool = True,
) -> Announcement:
    """테스트용 ``Announcement`` row 를 빠르게 INSERT 한다.

    payload 가 참조하는 announcement_id 와 DB row 의 PK 가 일치해야 본문에 공고
    메타가 들어가므로, 테스트는 본 헬퍼로 row 를 만든 뒤 반환된 ``ann.id`` 를
    snapshot payload 에 그대로 박아 넣는다.

    필수 nullable=False 컬럼만 채우고 나머지는 ORM default 에 맡긴다 (raw_metadata
    = ``default=dict``, scraped_at / updated_at = ``default=_utcnow``).
    """
    announcement = Announcement(
        source_announcement_id=source_announcement_id,
        source_type=source_type,
        title=title,
        status=status,
        agency=agency,
        deadline_at=deadline_at,
        canonical_group_id=canonical_group_id,
        is_current=is_current,
    )
    session.add(announcement)
    session.flush()
    return announcement


def _aggregation_window(
    *,
    from_dt: datetime,
    to_dt: datetime,
    snapshot_count: int = 1,
) -> AggregationWindow:
    """단순한 ``AggregationWindow`` 인스턴스 빌더 — 테스트 내 sugar.

    ``aggregate_snapshots`` 는 window 의 ``from_dt`` / ``to_dt`` 만 SELECT 경계
    로 사용하므로 나머지 필드는 임의값으로도 무방하다.
    """
    return AggregationWindow(
        from_dt=from_dt,
        to_dt=to_dt,
        snapshot_count=snapshot_count,
        is_first_send=False,
        fallback_days=None,
    )


# ──────────────────────────────────────────────────────────────
# A. 단일 snapshot payload → 5종 카테고리 직접 추출
# ──────────────────────────────────────────────────────────────


def test_aggregate_single_snapshot_extracts_all_five_categories(
    db_session: Session,
) -> None:
    """단일 snapshot 의 5종 카테고리가 모두 ``AggregatedSnapshotPayload`` 에 1:1.

    검증:
        - 5종 카테고리 모두 채워진 payload 1건 → 각 dataclass 필드의 length 1.
        - ``AnnouncementSummary`` 의 모든 필드가 ORM row 에서 정확히 채워진다.
        - ``detail_url`` 은 ``SETTING_KEY_APP_PUBLIC_BASE_URL`` 또는 default 상수
          기반으로 조립된다.
        - ``total_count`` 는 5종 list 길이의 단순 합.
    """
    # 공고 5건 — 5종 카테고리에 1건씩 배치.
    ann_new = _insert_announcement(db_session, source_announcement_id="N-1",
                                   title="신규 공고",
                                   status=AnnouncementStatus.SCHEDULED,
                                   agency="기관-신규",
                                   deadline_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                                   canonical_group_id=None)
    ann_changed = _insert_announcement(db_session, source_announcement_id="C-1",
                                       title="내용 변경 공고",
                                       status=AnnouncementStatus.RECEIVING)
    ann_to_scheduled = _insert_announcement(db_session, source_announcement_id="T-S",
                                            status=AnnouncementStatus.SCHEDULED)
    ann_to_receiving = _insert_announcement(db_session, source_announcement_id="T-R",
                                            status=AnnouncementStatus.RECEIVING)
    ann_to_closed = _insert_announcement(db_session, source_announcement_id="T-C",
                                         status=AnnouncementStatus.CLOSED)

    # public_base_url SystemSetting 명시 (default 가 아니라 시그널값으로 확인).
    set_setting(
        db_session,
        SETTING_KEY_APP_PUBLIC_BASE_URL,
        "https://internal.example.com",
    )

    # snapshot 1건 — 5종 카테고리 모두 채움.
    payload = normalize_payload({
        "new": [ann_new.id],
        "content_changed": [ann_changed.id],
        "transitioned_to_접수예정": [{"id": ann_to_scheduled.id, "from": "접수중"}],
        "transitioned_to_접수중": [{"id": ann_to_receiving.id, "from": "접수예정"}],
        "transitioned_to_마감": [{"id": ann_to_closed.id, "from": "접수중"}],
    })
    snapshot_at = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)
    _insert_snapshot_with_created_at(
        db_session,
        created_at=snapshot_at,
        snapshot_date_iso="2026-05-18",
        payload=payload,
    )
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    assert isinstance(result, AggregatedSnapshotPayload)
    assert len(result.new) == 1
    assert len(result.content_changed) == 1
    assert len(result.transitioned_to_received_scheduled) == 1
    assert len(result.transitioned_to_receiving) == 1
    assert len(result.transitioned_to_closed) == 1
    assert result.total_count == 5

    # 신규 공고 summary 의 필드 매핑 정확성.
    new_summary = result.new[0]
    assert isinstance(new_summary, AnnouncementSummary)
    assert new_summary.announcement_id == ann_new.id
    assert new_summary.title == "신규 공고"
    assert new_summary.source_type == "IRIS"
    assert new_summary.agency == "기관-신규"
    assert new_summary.deadline_at == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    assert new_summary.canonical_project_id is None
    # detail_url 은 SystemSetting public_base_url + 본 row 의 PK.
    assert new_summary.detail_url == (
        f"https://internal.example.com/announcements/{ann_new.id}"
    )

    # 카테고리별 announcement_id 가 의도대로 들어갔는지 확인.
    assert result.content_changed[0].announcement_id == ann_changed.id
    assert result.transitioned_to_received_scheduled[0].announcement_id == ann_to_scheduled.id
    assert result.transitioned_to_receiving[0].announcement_id == ann_to_receiving.id
    assert result.transitioned_to_closed[0].announcement_id == ann_to_closed.id


# ──────────────────────────────────────────────────────────────
# B. 2개 snapshot 머지 → new ID set union
# ──────────────────────────────────────────────────────────────


def test_aggregate_two_snapshots_unions_new_ids(db_session: Session) -> None:
    """두 snapshot 의 ``new`` ID 가 set union 으로 머지된다 (중복 제거 + asc).

    검증 6 형식의 회귀 가드 — \"신규 + 신규\" 가 누락 없이 합쳐지는지.
    """
    ann_a = _insert_announcement(db_session, source_announcement_id="A")
    ann_b = _insert_announcement(db_session, source_announcement_id="B")
    ann_c = _insert_announcement(db_session, source_announcement_id="C")

    # snapshot 1 (이른 시각): new = [A, B]
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 9, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={"new": [ann_a.id, ann_b.id]},
    )
    # snapshot 2 (늦은 시각): new = [B, C] — B 는 set union 으로 중복 제거.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 15, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-19",
        payload={"new": [ann_b.id, ann_c.id]},
    )
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    # 3건 (A/B/C) 정확히 — B 가 양쪽 snapshot 에 있어도 1번만.
    assert [summary.announcement_id for summary in result.new] == sorted(
        [ann_a.id, ann_b.id, ann_c.id]
    )
    assert result.total_count == 3


# ──────────────────────────────────────────────────────────────
# C. 2개 snapshot 머지 → transition first from 유지 + last to 갱신
# ──────────────────────────────────────────────────────────────


def test_aggregate_two_snapshots_transition_first_from_last_to(
    db_session: Session,
) -> None:
    """접수예정→접수중→마감 머지 시 first from 유지 + last to 갱신.

    prompt §3 검증 4 의 머지 규칙 — 본 함수가 ``merge_snapshot_payload`` 를
    그대로 reduce 로 재사용함을 보장하는 회귀 가드.
    """
    ann = _insert_announcement(
        db_session,
        source_announcement_id="T",
        status=AnnouncementStatus.CLOSED,
    )

    # snapshot 1: 접수예정 → 접수중 으로 전이.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 9, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={
            "transitioned_to_접수중": [{"id": ann.id, "from": "접수예정"}],
        },
    )
    # snapshot 2: 접수중 → 마감 으로 전이.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 18, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-19",
        payload={
            "transitioned_to_마감": [{"id": ann.id, "from": "접수중"}],
        },
    )
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    # 마감 카테고리에 1건만 — 접수중 카테고리는 비어야 함.
    assert len(result.transitioned_to_closed) == 1
    assert result.transitioned_to_closed[0].announcement_id == ann.id
    assert result.transitioned_to_receiving == []
    assert result.transitioned_to_received_scheduled == []
    assert result.new == []
    assert result.content_changed == []
    assert result.total_count == 1


# ──────────────────────────────────────────────────────────────
# D. from == to 정정 케이스 → 어떤 카테고리에도 안 들어감
# ──────────────────────────────────────────────────────────────


def test_aggregate_two_snapshots_from_equals_to_removed(db_session: Session) -> None:
    """접수중→마감→접수중 정정 시나리오 — from == to 라 모두 제거.

    prompt §3 검증 5 의 머지 규칙. ``_merge_transitions`` 가 final to 가 first
    from 과 동일하면 entry 자체를 drop 함을 회귀 보호.
    """
    ann = _insert_announcement(
        db_session,
        source_announcement_id="REVERT",
        status=AnnouncementStatus.RECEIVING,
    )

    # snapshot 1: 접수중 → 마감 (전이 발생).
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 9, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={
            "transitioned_to_마감": [{"id": ann.id, "from": "접수중"}],
        },
    )
    # snapshot 2: 마감 → 접수중 (정정).
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 18, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-19",
        payload={
            "transitioned_to_접수중": [{"id": ann.id, "from": "마감"}],
        },
    )
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    # 어떤 카테고리에도 ann.id 가 들어가지 않아야 한다.
    assert result.transitioned_to_received_scheduled == []
    assert result.transitioned_to_receiving == []
    assert result.transitioned_to_closed == []
    assert result.new == []
    assert result.content_changed == []
    assert result.total_count == 0


# ──────────────────────────────────────────────────────────────
# E. 메타 누락 announcement_id → silent skip
# ──────────────────────────────────────────────────────────────


def test_aggregate_skips_summaries_for_missing_announcement_meta(
    db_session: Session,
) -> None:
    """payload 에 있지만 DB 에 없는 announcement_id 는 결과에서 빠진다.

    DB 정리 / 외부 ID 오염 등으로 일부 메타가 없는 케이스를 방어한다. 메타가
    있는 row 만 본문에 들어가야 의미가 있어 silent skip — dashboard
    ``_build_expand_items_for_category`` 와 동일 정책.
    """
    ann_present = _insert_announcement(db_session, source_announcement_id="P")
    missing_announcement_id = 99_999  # DB 에 없는 PK.

    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={"new": [ann_present.id, missing_announcement_id]},
    )
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    # 메타 있는 row 1건만 — 누락 id 는 결과에서 사라진다.
    assert [summary.announcement_id for summary in result.new] == [ann_present.id]
    assert result.total_count == 1


# ──────────────────────────────────────────────────────────────
# F. is_current=False history row 도 포함
# ──────────────────────────────────────────────────────────────


def test_aggregate_includes_history_rows_with_is_current_false(
    db_session: Session,
) -> None:
    """``is_current=False`` history row 도 결과에 들어간다.

    prompt §3 주의사항: \"is_current 필터하지 말고 id 로 직접 SELECT (이력 row
    포함)\". new_version 분기로 만들어진 history row 가 daily report 본문에서
    배제되면 \"내용 변경 전 버전\" 정보가 사라져 누락이 된다.
    """
    history_row = _insert_announcement(
        db_session,
        source_announcement_id="H",
        title="이력 row 제목",
        is_current=False,
    )

    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={"content_changed": [history_row.id]},
    )
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    assert [summary.announcement_id for summary in result.content_changed] == [
        history_row.id
    ]
    assert result.content_changed[0].title == "이력 row 제목"


# ──────────────────────────────────────────────────────────────
# G. detail_url — public_base_url SystemSetting 없을 때 default 사용
# ──────────────────────────────────────────────────────────────


def test_aggregate_detail_url_falls_back_to_default_public_base_url(
    db_session: Session,
) -> None:
    """``SETTING_KEY_APP_PUBLIC_BASE_URL`` row 없음 → ``DEFAULT_APP_PUBLIC_BASE_URL``
    로 detail_url 조립.

    forwarding 의 fallback 패턴과 동일 — row 가 없어도 발송이 막히지 않게.
    """
    ann = _insert_announcement(db_session, source_announcement_id="D")

    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={"new": [ann.id]},
    )
    # SETTING_KEY_APP_PUBLIC_BASE_URL 의 row 는 생성하지 않는다.
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    expected_url = f"{DEFAULT_APP_PUBLIC_BASE_URL}/announcements/{ann.id}"
    assert result.new[0].detail_url == expected_url


# ──────────────────────────────────────────────────────────────
# H. 빈 구간 — defensive 가드
# ──────────────────────────────────────────────────────────────


def test_aggregate_returns_empty_payload_when_window_has_no_snapshots(
    db_session: Session,
) -> None:
    """구간 안에 snapshot 이 0건이어도 빈 ``AggregatedSnapshotPayload`` 반환.

    정상 흐름에서는 ``compute_aggregation_window`` 가 None 을 반환해 본 함수가
    호출되지 않지만, 호출자가 잘못된 window 를 만들어 직접 호출해도 예외 없이
    빈 payload 를 반환해야 한다.
    """
    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    assert result.new == []
    assert result.content_changed == []
    assert result.transitioned_to_received_scheduled == []
    assert result.transitioned_to_receiving == []
    assert result.transitioned_to_closed == []
    assert result.total_count == 0


# ──────────────────────────────────────────────────────────────
# I. total_count — 5종 list 길이의 합 (중복 제거 없음)
# ──────────────────────────────────────────────────────────────


def test_aggregate_total_count_includes_duplicates_across_categories(
    db_session: Session,
) -> None:
    """같은 announcement 가 \"내용 변경\" + \"전이\" 양쪽에 있으면 ``total_count``
    는 2 로 합산된다 (중복 제거 없음 — design note §3).
    """
    ann = _insert_announcement(
        db_session,
        source_announcement_id="DUP",
        status=AnnouncementStatus.CLOSED,
    )

    # 단일 snapshot 에서 같은 id 가 content_changed + 전이 양쪽에 등장.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={
            "content_changed": [ann.id],
            "transitioned_to_마감": [{"id": ann.id, "from": "접수중"}],
        },
    )
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    assert [s.announcement_id for s in result.content_changed] == [ann.id]
    assert [s.announcement_id for s in result.transitioned_to_closed] == [ann.id]
    # 같은 공고가 양쪽 카테고리에 있어도 total_count 는 2.
    assert result.total_count == 2


# ──────────────────────────────────────────────────────────────
# J. N+1 회귀 가드 — announcement 일괄 조회는 단일 SELECT
# ──────────────────────────────────────────────────────────────


def test_aggregate_does_not_issue_n_plus_one_announcement_selects(
    db_session: Session,
) -> None:
    """announcement 수가 늘어도 ``announcements`` 테이블 SELECT 는 1회뿐.

    guidance \"announcement 일괄 조회는 단일 SELECT 로 N+1 회피\" 의 회귀 가드.
    공고 row 가 1건이든 10건이든 같은 단일 IN 쿼리가 발생해야 한다.
    """
    announcement_ids: list[int] = []
    for index in range(10):
        announcement = _insert_announcement(
            db_session,
            source_announcement_id=f"N-{index}",
        )
        announcement_ids.append(announcement.id)

    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={"new": announcement_ids},
    )
    db_session.commit()

    announcement_select_count = [0]

    def _count_announcement_selects(
        conn, cursor, statement, parameters, context, executemany
    ):
        """``announcements`` 테이블을 FROM 하는 SELECT 만 카운트한다."""
        if statement and "FROM announcements" in statement:
            announcement_select_count[0] += 1

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _count_announcement_selects)
    try:
        window = _aggregation_window(
            from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
            to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
        )
        result = aggregate_snapshots(db_session, window)
    finally:
        event.remove(engine, "before_cursor_execute", _count_announcement_selects)

    assert len(result.new) == 10
    # 일괄 IN 쿼리 1회만 — N+1 이면 announcement_select_count == 10 이 된다.
    assert announcement_select_count[0] == 1, (
        f"announcements SELECT 가 {announcement_select_count[0]} 회 발생했다 — "
        "단일 IN 쿼리(1회) 기대. N+1 회귀 가능성."
    )


# pytest 가 모듈 단독 실행 가능하도록.
if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
