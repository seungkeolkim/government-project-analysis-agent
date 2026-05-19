"""``compute_aggregation_window`` + 공용 dataclass 단위 테스트 (task 00125-3).

검증 대상 (subtask 00125-3 의 acceptance_criteria + design note §1·§4·§14):

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

DB:
    tests/conftest.py 의 ``test_engine`` + ``db_session`` fixture 사용.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.backup.service import set_setting
from app.db.models import ScrapeSnapshot
from app.db.snapshot import normalize_payload
from app.email.constants import SETTING_KEY_DAILY_REPORT_LAST_SENT_AT
from app.email.daily_report import (
    AggregatedSnapshotPayload,
    AggregationWindow,
    AnnouncementSummary,
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


# pytest 가 모듈 단독 실행 가능하도록.
if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
