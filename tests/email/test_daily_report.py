"""``compute_aggregation_window`` + ``aggregate_snapshots`` + 공용 dataclass 단위 테스트.

검증 대상 (subtask 00125-3 / 00125-4 의 acceptance_criteria + design note §1·§2·
§4·§14):

    1. ``last_sent_at`` SystemSetting row 없음 → ``is_first_send=True`` ,
       ``fallback_days=7`` 적용, ``from_dt = now - 7d``.
    2. ``last_sent_at`` 있음 + 구간 내 snapshot 0 건 → ``AggregationWindow(snapshot_count=0)``
       반환 (0건이어도 SKIPPED 아님).
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

본문 빌더 (subtask 00125-5 의 acceptance_criteria + prompt §4·§검증 #6 + guidance
\"빈 카테고리 섹션 생략 / 50건 cap / KST YYYY-MM-DD HH:mm\"):

    K. ``build_daily_report_subject`` — UTC 입력 → KST 변환 후 ``YYYY-MM-DD HH:mm``
       포맷으로 prefix 와 함께 결합.
    L. ``build_daily_report_text_body`` — 모든 카테고리 채워진 payload → 5종
       섹션 + 카테고리당 카운트 + 공고 1줄에 detail_url/agency/마감일 포함.
    M. ``build_daily_report_text_body`` — 빈 카테고리는 섹션 자체 생략 (header /
       \"(0건)\" 도 노출되지 않음). 단, 요약 줄에는 0건이 그대로 노출.
    N. ``build_daily_report_text_body`` — ``is_first_send=True`` 면 헤더 박스에
       \"최초 발송 — 직전 N일치 포함\" 안내 노출.
    O. ``build_daily_report_text_body`` — 카테고리당 50건 초과 시 끝에
       ``\"외 N건 — 대시보드에서 확인\"`` 안내가 1줄 들어가고, 본문에 노출되는 줄은
       정확히 50개.
    P. ``build_daily_report_html_body`` — HTML 본문에 모든 5종 섹션 + escape 처리.
    Q. ``build_daily_report_html_body`` — 빈 카테고리는 HTML 섹션도 생략.
    R. ``build_daily_report_html_body`` — 50건 초과 시 ``\"외 N건\"`` 안내 노출.
    S. ``build_daily_report_html_body`` — title / agency 의 HTML 특수문자가
       이스케이프된다.

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
    TEST_SEND_LOOKBACK_HOURS,
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
# 2. last_sent_at 있음 + 구간 내 snapshot 0 → AggregationWindow(snapshot_count=0)
# ──────────────────────────────────────────────────────────────


def test_compute_window_returns_window_with_zero_count_when_no_snapshots_in_range(
    db_session: Session,
) -> None:
    """``last_sent_at`` 있음 + ``(last_sent_at, now]`` 안의 snapshot 0 건 → snapshot_count=0.

    0건이어도 None 을 반환하지 않고 AggregationWindow 를 반환한다.
    발송 여부 판단은 호출자(prepare_and_send_daily_report) 가 결정하지 않고,
    항상 빈 본문으로 발송된다.

    검증:
        - ``(last_sent_at, now]`` 구간 밖 (직전) row 는 카운트에서 제외.
        - ``from_dt`` 는 배타이므로 \"정확히 last_sent_at 시각\" 의 row 도 제외.
        - 결과적으로 0 건 → ``AggregationWindow(snapshot_count=0)`` 반환.
        - ``is_first_send=False``, ``from_dt`` 가 last_sent_at 과 일치.
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

    # 0건이어도 None 이 아닌 AggregationWindow 반환.
    assert window is not None, "0건이어도 AggregationWindow 를 반환해야 함"
    assert isinstance(window, AggregationWindow)
    assert window.snapshot_count == 0
    assert window.is_first_send is False
    assert window.fallback_days is None
    assert window.from_dt == last_sent
    assert window.to_dt == now


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
    구간 안 row 가 0건이면 AggregationWindow(snapshot_count=0) 를 반환해 빈
    본문으로 발송을 시도한다 — fallback 으로 늘리면 last_sent_at 갱신 정책이
    어긋나고 과거 snapshot 이 중복 집계된다 (design note §7).
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

    # 0건이어도 AggregationWindow 반환 — fallback 으로 확장하지 않음.
    assert window is not None, (
        "last_sent_at 있음 + 구간 안 0건 → AggregationWindow(snapshot_count=0) "
        "반환해야 함 (None 이 아님)"
    )
    assert window.snapshot_count == 0
    assert window.is_first_send is False
    assert window.from_dt == last_sent


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

    # task 00136-2 — 비전이(신규) 항목은 현재 상태만 채워지고 transition_from 은 None.
    assert new_summary.status_label == "접수예정"  # ann_new 는 SCHEDULED.
    assert new_summary.status_key == "scheduled"
    assert new_summary.transition_from is None
    assert new_summary.transition_from_key is None

    # task 00136-2 — 전이 항목은 payload 의 'from'(이전 상태)이 보존된다.
    receiving_summary = result.transitioned_to_receiving[0]
    assert receiving_summary.status_label == "접수중"  # ann_to_receiving 는 RECEIVING.
    assert receiving_summary.status_key == "receiving"
    assert receiving_summary.transition_from == "접수예정"  # payload 의 from.
    assert receiving_summary.transition_from_key == "scheduled"


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
# F-2. 전이 summary 는 transition_from / status / received_at 보존 (task 00136-2)
# ──────────────────────────────────────────────────────────────


def test_aggregate_transition_summary_preserves_from_status_received_at(
    db_session: Session,
) -> None:
    """전이 카테고리 summary 가 transition_from·상태·received_at 을 모두 보존한다.

    task 00136-2: ``aggregate_snapshots`` 가 transition payload 의 ``from``
    (이전 상태)을 버리지 않고 ``AnnouncementSummary.transition_from`` 으로
    넘겨야 메일에서 '이전상태 → 현재상태' 전이가 표시된다. 현재 상태
    (status_label/status_key)·접수 일시(received_at)도 함께 보존되는지 확인한다.
    """
    announcement = _insert_announcement(
        db_session,
        source_announcement_id="TR",
        status=AnnouncementStatus.RECEIVING,
    )
    # received_at 은 헬퍼가 채우지 않으므로 직접 설정한다.
    announcement.received_at = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    db_session.flush()

    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={
            "transitioned_to_접수중": [
                {"id": announcement.id, "from": "접수예정"}
            ],
        },
    )
    db_session.commit()

    window = _aggregation_window(
        from_dt=datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = aggregate_snapshots(db_session, window)

    assert len(result.transitioned_to_receiving) == 1
    summary = result.transitioned_to_receiving[0]
    # 이전 상태(payload 의 from) 가 보존된다.
    assert summary.transition_from == "접수예정"
    assert summary.transition_from_key == "scheduled"
    # 현재 상태 — Announcement.status 에서.
    assert summary.status_label == "접수중"
    assert summary.status_key == "receiving"
    # 접수 일시 — Announcement.received_at 에서.
    assert summary.received_at == datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)


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


# ──────────────────────────────────────────────────────────────
# 본문 빌더 — subject / text / html
# ──────────────────────────────────────────────────────────────


def _build_announcement_summary(
    *,
    announcement_id: int = 1,
    title: str = "테스트 공고",
    source_type: str = "iris",
    agency: str | None = "기관A",
    deadline_at: datetime | None = None,
    detail_url: str = "https://example.com/announcements/1",
    canonical_project_id: int | None = None,
    status_label: str = "접수중",
    status_key: str | None = "receiving",
    transition_from: str | None = None,
    transition_from_key: str | None = None,
    received_at: datetime | None = None,
) -> AnnouncementSummary:
    """본문 빌더 테스트용 ``AnnouncementSummary`` 인스턴스 빌더.

    DB 를 거치지 않고 dataclass 만으로 빌더 동작을 검증한다 — 본문 빌더는
    AggregatedSnapshotPayload 만 보고 동작하므로 ORM row 가 필요 없다.

    task 00136-2 에서 추가된 ``status_label`` / ``status_key`` /
    ``transition_from`` / ``transition_from_key`` / ``received_at`` 도 인자로
    받는다. 비전이 항목 기본값은 현재 상태 '접수중' 이고, 전이 항목 테스트는
    ``transition_from`` 을 명시해 호출한다.
    """
    return AnnouncementSummary(
        announcement_id=announcement_id,
        canonical_project_id=canonical_project_id,
        title=title,
        source_type=source_type,
        agency=agency,
        deadline_at=deadline_at,
        detail_url=detail_url,
        status_label=status_label,
        status_key=status_key,
        transition_from=transition_from,
        transition_from_key=transition_from_key,
        received_at=received_at,
    )


def _build_full_payload() -> AggregatedSnapshotPayload:
    """5종 카테고리에 각 1건씩 채워진 payload — 본문 빌더 일반 케이스용."""
    return AggregatedSnapshotPayload(
        new=[
            _build_announcement_summary(
                announcement_id=11,
                title="신규 사업",
                agency="새기관",
                detail_url="https://example.com/announcements/11",
                deadline_at=datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
            )
        ],
        content_changed=[
            _build_announcement_summary(
                announcement_id=12,
                title="변경된 사업",
                agency=None,
                detail_url="https://example.com/announcements/12",
            )
        ],
        transitioned_to_received_scheduled=[
            _build_announcement_summary(
                announcement_id=13,
                title="접수예정 사업",
                detail_url="https://example.com/announcements/13",
                status_label="접수예정",
                status_key="scheduled",
                transition_from="접수중",
                transition_from_key="receiving",
            )
        ],
        transitioned_to_receiving=[
            _build_announcement_summary(
                announcement_id=14,
                title="접수중 사업",
                detail_url="https://example.com/announcements/14",
                status_label="접수중",
                status_key="receiving",
                transition_from="접수예정",
                transition_from_key="scheduled",
            )
        ],
        transitioned_to_closed=[
            _build_announcement_summary(
                announcement_id=15,
                title="마감 사업",
                detail_url="https://example.com/announcements/15",
                status_label="마감",
                status_key="closed",
                transition_from="접수중",
                transition_from_key="receiving",
            )
        ],
        total_count=5,
    )


def _window_for_body(
    *,
    is_first_send: bool = False,
    fallback_days: int | None = None,
    snapshot_count: int = 3,
) -> AggregationWindow:
    """본문 빌더 테스트용 고정 시각 ``AggregationWindow``.

    from_dt / to_dt 는 KST 변환 검증을 쉽게 하기 위해 UTC 00:00 / 00:00 다음 날
    같은 깔끔한 시각으로 고정한다 — KST 변환 후 09:00 으로 표시된다.
    """
    return AggregationWindow(
        from_dt=datetime(2026, 5, 18, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, tzinfo=UTC),
        snapshot_count=snapshot_count,
        is_first_send=is_first_send,
        fallback_days=fallback_days,
    )


# ──────────────────────────────────────────────────────────────
# K. build_daily_report_subject — KST 변환 포맷
# ──────────────────────────────────────────────────────────────


def test_build_subject_uses_kst_yyyy_mm_dd_hh_mm() -> None:
    """제목 시각 표기는 KST 변환 후 ``YYYY-MM-DD HH:mm`` 포맷.

    UTC 2026-05-18 00:00 → KST 2026-05-18 09:00.
    UTC 2026-05-19 00:00 → KST 2026-05-19 09:00.
    """
    from app.email.message_builder import build_daily_report_subject

    window = _window_for_body()
    subject = build_daily_report_subject(window)

    assert subject == (
        "[정부사업 모니터링] Daily Report — 2026-05-18 09:00 ~ 2026-05-19 09:00"
    )


def test_build_subject_does_not_change_for_first_send() -> None:
    """``is_first_send`` 가 True 여도 제목 자체는 동일하다 (안내는 본문에만).

    design note §6 결정 — 제목까지 \"최초 발송\" 을 늘리지 않는다.
    """
    from app.email.message_builder import build_daily_report_subject

    window_first = _window_for_body(is_first_send=True, fallback_days=7)
    window_normal = _window_for_body()

    assert build_daily_report_subject(window_first) == (
        build_daily_report_subject(window_normal)
    )


# ──────────────────────────────────────────────────────────────
# L. text body — 모든 카테고리 채워진 payload → 5종 섹션 + 1줄 포맷
# ──────────────────────────────────────────────────────────────


def test_build_text_body_includes_all_five_section_headers_and_lines() -> None:
    """5종 카테고리가 모두 채워지면 모든 섹션 헤더 + 공고 1줄 모두 노출.

    검증 (task 00136-2 — text 본문 공고 1줄 포맷 통일):
        - 5종 섹션 헤더 각각 ``"{emoji} {label} ({N}건)"`` 형식 포함.
        - 공고 1줄에 출처 ``[IRIS]`` / 상태 / ``title`` / ``detail_url`` /
          ``agency`` / 접수·마감 일시 포함.
        - agency 가 None 이면 ``-`` 으로 표시.
        - received_at / deadline_at 이 None 이면 ``-`` 표시.
        - 전이 항목은 ``이전상태→현재상태`` 로 표시.
    """
    from app.email.message_builder import build_daily_report_text_body

    text = build_daily_report_text_body(
        window=_window_for_body(snapshot_count=5),
        payload=_build_full_payload(),
    )

    # 5종 섹션 헤더 — 카테고리당 1건씩이므로 모두 ``(1건)``.
    assert "🆕 신규 공고 (1건)" in text
    assert "📝 내용 변경 (1건)" in text
    assert "✅ 접수예정 전이 (1건)" in text
    assert "▶ 접수중 전이 (1건)" in text
    assert "🚫 마감 전이 (1건)" in text

    # 공고 1줄 (신규) — 출처 배지 / 상태 / title / url / agency 모두 포함.
    assert (
        "- [IRIS] 접수중 | 신규 사업 (https://example.com/announcements/11)" in text
    )
    assert "새기관" in text
    # 마감일 — KST 변환 (UTC 18:00 → KST 03:00 익일이지만 datetime 변환 검증).
    # UTC 2026-06-01 18:00 → KST 2026-06-02 03:00.
    assert "마감일 2026-06-02 03:00" in text

    # agency=None 인 변경 사업 → ``-`` 로 표시. received/deadline 도 None → ``-``.
    assert (
        "- [IRIS] 접수중 | 변경된 사업 (https://example.com/announcements/12) "
        "— - — 접수 - — 마감일 -"
    ) in text

    # 전이 항목은 '이전상태→현재상태' 로 표시 (접수중→마감 전이 사업).
    assert "접수중→마감 | 마감 사업" in text


def test_build_text_body_summary_line_includes_all_five_zero_counts() -> None:
    """요약 줄은 5종 모두 노출 (빈 카테고리도 ``0건`` 으로 표기).

    섹션은 빈 카테고리를 생략하지만 요약 줄은 5종 모두 보여 줘야 운영자가
    \"0건이 정상\" 인지 한눈에 파악할 수 있다.
    """
    from app.email.message_builder import build_daily_report_text_body

    # new 만 1건, 나머지 4종은 빈 list.
    payload = AggregatedSnapshotPayload(
        new=[_build_announcement_summary(announcement_id=1, title="단일")],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=1,
    )
    text = build_daily_report_text_body(window=_window_for_body(), payload=payload)

    # 요약 줄 — 5종 모두 짧은 라벨로 노출 (변경 0건 / 접수예정 0건 / ...).
    expected_summary = (
        "🆕 신규 1건 · 📝 변경 0건 · ✅ 접수예정 0건 · ▶ 접수중 0건 · 🚫 마감 0건"
    )
    assert expected_summary in text


# ──────────────────────────────────────────────────────────────
# M. text body — 빈 카테고리 섹션은 생략
# ──────────────────────────────────────────────────────────────


def test_build_text_body_omits_empty_category_sections(_=None) -> None:
    """빈 카테고리 4종의 섹션 헤더는 본문에 등장하지 않는다.

    요약 줄에서는 ``0건`` 으로 표기되지만, 섹션 자체 (``label (0건)``) 는
    절대 본문에 나타나지 않아야 한다 — design note §6 \"섹션 자체 생략\".
    """
    from app.email.message_builder import build_daily_report_text_body

    payload = AggregatedSnapshotPayload(
        new=[_build_announcement_summary(title="단일")],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=1,
    )
    text = build_daily_report_text_body(window=_window_for_body(), payload=payload)

    # 신규 섹션은 있다.
    assert "🆕 신규 공고 (1건)" in text
    # 빈 4종은 섹션 자체 미노출 — ``(0건)`` 으로도 절대 나오면 안 된다.
    assert "내용 변경 (0건)" not in text
    assert "접수예정 전이 (0건)" not in text
    assert "접수중 전이 (0건)" not in text
    assert "마감 전이 (0건)" not in text


# ──────────────────────────────────────────────────────────────
# N. text body — is_first_send 안내
# ──────────────────────────────────────────────────────────────


def test_build_text_body_includes_first_send_notice_when_window_marks_first() -> None:
    """``is_first_send=True`` 면 헤더에 ``"최초 발송 — 직전 N일치 포함"`` 안내 노출.

    fallback_days 가 본문 안내 문구에 정수로 들어간다.
    """
    from app.email.message_builder import build_daily_report_text_body

    window = _window_for_body(is_first_send=True, fallback_days=7)
    text = build_daily_report_text_body(
        window=window,
        payload=_build_full_payload(),
    )

    assert "최초 발송" in text
    assert "직전 7일치" in text


def test_build_text_body_omits_first_send_notice_when_normal() -> None:
    """``is_first_send=False`` 인 일반 발송에는 \"최초 발송\" 안내가 없다."""
    from app.email.message_builder import build_daily_report_text_body

    text = build_daily_report_text_body(
        window=_window_for_body(is_first_send=False),
        payload=_build_full_payload(),
    )
    assert "최초 발송" not in text


# ──────────────────────────────────────────────────────────────
# O. text body — 50건 cap + 외 N건
# ──────────────────────────────────────────────────────────────


def test_build_text_body_caps_category_at_50_and_appends_overflow_notice() -> None:
    """카테고리당 50건 초과 → 본문 줄 정확히 50개 + ``\"외 N건\"`` 안내 1줄.

    검증:
        - ``- {title} (url) — ...`` 패턴으로 시작하는 줄 = 정확히 50개.
        - 51번째 줄 위치에 ``"... 외 N건 — 대시보드에서 확인"`` 1줄 등장.
        - 섹션 헤더 카운트는 cap 적용 전 원본 건수 (``(60건)``) 그대로 노출.
    """
    from app.email.message_builder import build_daily_report_text_body

    items = [
        _build_announcement_summary(
            announcement_id=100 + index,
            title=f"공고 {index}",
            detail_url=f"https://example.com/announcements/{100 + index}",
        )
        for index in range(60)
    ]
    payload = AggregatedSnapshotPayload(
        new=items,
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=60,
    )
    text = build_daily_report_text_body(window=_window_for_body(), payload=payload)

    # 섹션 헤더는 원본 60건 그대로.
    assert "🆕 신규 공고 (60건)" in text
    # 공고 1줄 (``- [출처] 상태 | {title}`` 형식) 개수 — 정확히 50.
    item_line_count = sum(
        1 for line in text.splitlines() if line.startswith("- ") and "| 공고 " in line
    )
    assert item_line_count == 50
    # 초과 안내 — 60 - 50 = 10 건.
    assert "... 외 10건 — 대시보드에서 확인" in text


def test_build_text_body_no_overflow_notice_when_exactly_at_cap() -> None:
    """정확히 cap (50건) 인 케이스는 ``\"외 N건\"`` 안내가 없다.

    overflow == 0 일 때 안내문이 출력되면 ``\"외 0건\"`` 같은 보기 흉한 문구가 나온다.
    """
    from app.email.message_builder import build_daily_report_text_body

    items = [
        _build_announcement_summary(
            announcement_id=200 + index,
            title=f"X{index}",
            detail_url=f"https://example.com/x/{index}",
        )
        for index in range(50)
    ]
    payload = AggregatedSnapshotPayload(
        new=items,
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=50,
    )
    text = build_daily_report_text_body(window=_window_for_body(), payload=payload)

    assert "외 0건" not in text
    assert "대시보드에서 확인" not in text


# ──────────────────────────────────────────────────────────────
# P. html body — 모든 5종 섹션 + 카운트
# ──────────────────────────────────────────────────────────────


def test_build_html_body_includes_all_five_section_headers() -> None:
    """HTML 본문에 5종 카테고리 섹션 헤더 + 카운트가 모두 포함된다.

    HTML 구조 단편:
        - ``<h2>Daily Report</h2>`` 헤더
        - ``"구간 (KST)"`` 메타 라벨
        - 각 카테고리의 h3 안에 ``"{emoji} {label} ({N}건)"`` 텍스트
        - 공고당 ``<li>`` 안에 ``href="{detail_url}"`` 의 ``<a>`` 태그
    """
    from app.email.message_builder import build_daily_report_html_body

    html_body = build_daily_report_html_body(
        window=_window_for_body(snapshot_count=5),
        payload=_build_full_payload(),
    )

    assert "<h2" in html_body and "Daily Report</h2>" in html_body
    assert "구간 (KST)" in html_body
    assert "2026-05-18 09:00 ~ 2026-05-19 09:00" in html_body
    # 5종 섹션 헤더 (h3 안에).
    assert "🆕 신규 공고 (1건)" in html_body
    assert "📝 내용 변경 (1건)" in html_body
    assert "✅ 접수예정 전이 (1건)" in html_body
    assert "▶ 접수중 전이 (1건)" in html_body
    assert "🚫 마감 전이 (1건)" in html_body
    # 공고 link href.
    assert 'href="https://example.com/announcements/11"' in html_body


# ──────────────────────────────────────────────────────────────
# Q. html body — 빈 카테고리 섹션은 생략
# ──────────────────────────────────────────────────────────────


def test_build_html_body_omits_empty_category_sections() -> None:
    """빈 4종 카테고리의 HTML 섹션도 본문에 등장하지 않는다.

    빈 카테고리 섹션이 HTML 에서 빈 ``<div>`` 등으로 남으면 결과 HTML 이
    난잡해진다 — 빌더는 빈 list 카테고리에 대해 빈 문자열을 합쳐 섹션 자체를
    제거해야 한다.
    """
    from app.email.message_builder import build_daily_report_html_body

    payload = AggregatedSnapshotPayload(
        new=[_build_announcement_summary(title="단독")],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=1,
    )
    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=payload
    )

    assert "🆕 신규 공고 (1건)" in html_body
    # 빈 4종 카테고리의 섹션 헤더는 절대 등장하면 안 된다.
    assert "내용 변경 (0건)" not in html_body
    assert "접수예정 전이 (0건)" not in html_body
    assert "접수중 전이 (0건)" not in html_body
    assert "마감 전이 (0건)" not in html_body


# ──────────────────────────────────────────────────────────────
# R. html body — 50건 cap + 외 N건 안내
# ──────────────────────────────────────────────────────────────


def test_build_html_body_caps_at_50_and_appends_overflow_li() -> None:
    """HTML 본문도 50건 cap + ``\"외 N건\"`` 안내 1행 노출.

    검증:
        - ``<a href=...``  로 시작하는 공고 1줄 ``<li>`` 가 정확히 50개.
        - ``"외 10건 — 대시보드에서 확인"`` 텍스트 포함.
    """
    from app.email.message_builder import build_daily_report_html_body

    items = [
        _build_announcement_summary(
            announcement_id=300 + index,
            title=f"HTML 공고 {index}",
            detail_url=f"https://example.com/h/{index}",
        )
        for index in range(60)
    ]
    payload = AggregatedSnapshotPayload(
        new=items,
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=60,
    )
    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=payload
    )

    # cap 적용 — anchor href 가 정확히 50개.
    assert html_body.count('href="https://example.com/h/') == 50
    # 초과 안내 (60 - 50 = 10).
    assert "외 10건 — 대시보드에서 확인" in html_body


# ──────────────────────────────────────────────────────────────
# S. html body — title / agency 의 HTML 특수문자 escape
# ──────────────────────────────────────────────────────────────


def test_build_html_body_escapes_announcement_fields() -> None:
    """공고 제목 / detail_url 에 HTML 특수문자가 들어와도 안전하게 escape 된다.

    HTML injection 방어 — ``<script>`` 가 그대로 본문에 나오면 안 되고,
    ``&lt;script&gt;`` 로 escape 되어야 한다. 공유 렌더러가 공고명·detail_url
    을 ``html.escape`` 처리한다.
    """
    from app.email.message_builder import build_daily_report_html_body

    payload = AggregatedSnapshotPayload(
        new=[
            _build_announcement_summary(
                title="<script>alert(1)</script>",
                agency="기관 & 부서",
                detail_url="https://example.com/x?a=1&b=2",
            )
        ],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=1,
    )
    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=payload
    )

    # script 태그 원문이 직접 들어가면 안 된다.
    assert "<script>alert(1)</script>" not in html_body
    # 이스케이프된 형태로 등장해야 한다 (공유 렌더러가 공고명을 escape).
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_body
    # detail_url 의 & 도 href 에서 &amp; 로 escape (quote=True).
    # 발주기관(agency)은 공유 렌더러의 행 마크업에 출력되지 않으므로
    # (대시보드 expand 행과 동일 — 출처/상태/공고명/날짜만 노출) escape
    # 검증 대상에서 제외한다.
    assert 'href="https://example.com/x?a=1&amp;b=2"' in html_body


# ──────────────────────────────────────────────────────────────
# T. html body — 폭 ~2배 확장 + 공유 렌더러 적용 (task 00136-2)
# ──────────────────────────────────────────────────────────────


def test_build_html_body_uses_widened_max_width() -> None:
    """HTML 본문 컨테이너의 max-width 가 1375px 로 확장된다.

    task 00141 — 날짜 셀 185px → 400px 확장(+215px)에 맞춰 전체 컨테이너도
    1160px → 1375px 로 늘려 다른 컬럼이 눌리지 않도록 한다.
    """
    from app.email.message_builder import build_daily_report_html_body

    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=_build_full_payload()
    )

    # 확장된 max-width 값이 본문에 포함된다.
    assert "max-width:1375px" in html_body
    # 기존 600px 는 더 이상 데일리 리포트 본문에 남아 있지 않다.
    assert "max-width:600px" not in html_body


def test_build_html_body_renders_source_badge_via_shared_renderer() -> None:
    """각 공고 항목이 공유 렌더러의 출처 배지 마크업으로 렌더된다.

    task 00136-2 — 메일 항목을 대시보드와 같은 출처 배지([IRIS] 등) 포맷으로
    통일한다. 공유 렌더러(``app.rendering.announcement_row``)의 출처 배지는
    ``text-transform:uppercase`` 인라인 CSS 와 출처 문자열을 가진다.
    """
    from app.email.message_builder import build_daily_report_html_body

    payload = AggregatedSnapshotPayload(
        new=[_build_announcement_summary(title="IRIS 공고", source_type="IRIS")],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=1,
    )
    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=payload
    )

    # 공유 렌더러 출처 배지 — uppercase 변환 인라인 CSS + 출처 문자열.
    assert "text-transform:uppercase" in html_body
    assert ">IRIS</span>" in html_body


def test_build_html_body_renders_transition_prev_and_current_status() -> None:
    """전이 카테고리 항목은 이전→현재 상태 배지 + 화살표가 함께 노출된다.

    task 00136-2 — 사용자 원문 \"어디서 어디로 바뀌었는지\" 를 메일에서도 보여
    준다. 공유 렌더러는 transition_from 이 있으면 이전 상태 배지와 ``→``
    화살표를 현재 상태 배지 앞에 렌더한다.
    """
    from app.email.message_builder import build_daily_report_html_body

    payload = AggregatedSnapshotPayload(
        new=[],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[
            _build_announcement_summary(
                title="전이 공고",
                status_label="접수중",
                status_key="receiving",
                transition_from="접수예정",
                transition_from_key="scheduled",
            )
        ],
        transitioned_to_closed=[],
        total_count=1,
    )
    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=payload
    )

    # 이전 상태 배지 + 화살표 + 현재 상태 배지.
    assert ">접수예정</span>" in html_body
    assert ">접수중</span>" in html_body
    assert "→</span>" in html_body


def test_build_html_body_renders_received_at_datetime() -> None:
    """공고 항목에 접수 일시가 KST 문자열로 노출된다.

    task 00136-2 — 사용자 원문 \"각종 날짜정보\". 공유 렌더러는 접수 일시를
    ``접수 YYYY-MM-DD HH:MM:SS`` (KST) 로 마감 일시 왼쪽에 표시한다.
    UTC 2026-05-20 00:00 → KST 2026-05-20 09:00:00.
    """
    from app.email.message_builder import build_daily_report_html_body

    payload = AggregatedSnapshotPayload(
        new=[
            _build_announcement_summary(
                title="접수 일시 공고",
                received_at=datetime(2026, 5, 20, 0, 0, 0, tzinfo=UTC),
            )
        ],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=1,
    )
    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=payload
    )

    assert "접수 2026-05-20 09:00:00" in html_body


# ──────────────────────────────────────────────────────────────
# U. html body — 카테고리 섹션이 옅은 테두리 박스로 감싸진다 (task 00139-2)
# ──────────────────────────────────────────────────────────────


def test_build_html_body_category_section_wrapped_in_border_box() -> None:
    """각 카테고리 섹션이 옅은 테두리(#e0e0e0) 박스 ``<td>`` 로 감싸진다.

    task 00139-2 — 사용자 원문 \"각 섹션마다 대시보드처럼 매우 옅은 테두리를
    가진 표로 감싸서 읽기 편하게 해줘\".

    검증:
        - ``<td>`` 에 ``border:1px solid #e0e0e0;border-radius:6px`` 가 인라인.
        - 섹션이 2개이면 그 패턴도 2회 이상 등장한다.
        - 섹션 박스 안에 내부 padding(``12px 16px``)이 적용된다.
        - 헤더(h3)·공고 행·overflow 안내가 모두 박스 안에 들어간다.
    """
    from app.email.message_builder import build_daily_report_html_body

    payload = AggregatedSnapshotPayload(
        new=[_build_announcement_summary(announcement_id=1, title="신규 공고")],
        content_changed=[_build_announcement_summary(announcement_id=2, title="변경 공고")],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=2,
    )
    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=payload
    )

    # <td> 에 border:1px solid #e0e0e0;border-radius:6px 가 인라인으로 붙어야 함.
    # (footer <hr> 의 border:1px solid #e0e0e0 과는 구분되는 패턴)
    section_box_pattern = "border:1px solid #e0e0e0;border-radius:6px"
    assert section_box_pattern in html_body, (
        "섹션 박스 <td> 에 border:1px solid #e0e0e0;border-radius:6px 가 없음"
    )
    # 섹션 2개이므로 패턴도 2회 이상.
    assert html_body.count(section_box_pattern) >= 2, (
        f"섹션이 2개인데 박스 패턴이 {html_body.count(section_box_pattern)}회 — 2 이상이어야 함"
    )
    # 박스 내부 padding.
    assert "padding:12px 16px" in html_body

    # 빈 카테고리(3종)는 박스가 없으므로 패턴 횟수 == 2.
    assert html_body.count(section_box_pattern) == 2


def test_build_html_body_empty_category_has_no_border_box() -> None:
    """빈 카테고리는 테두리 박스를 포함해 섹션 전체가 사라진다.

    task 00139-2 — items 가 빈 list 이면 ``""`` 를 반환해 섹션(테두리 박스 포함)이
    통째로 생략됨을 검증한다.
    """
    from app.email.message_builder import build_daily_report_html_body

    payload = AggregatedSnapshotPayload(
        new=[_build_announcement_summary(title="단독 신규")],
        content_changed=[],
        transitioned_to_received_scheduled=[],
        transitioned_to_receiving=[],
        transitioned_to_closed=[],
        total_count=1,
    )
    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=payload
    )

    section_box_pattern = "border:1px solid #e0e0e0;border-radius:6px"
    # 섹션이 1개(신규 공고)이므로 박스 패턴도 정확히 1회.
    assert html_body.count(section_box_pattern) == 1


# ──────────────────────────────────────────────────────────────
# fixed_lookback_hours — 테스트 발송 전용 고정 구간
# ──────────────────────────────────────────────────────────────


def test_compute_window_fixed_lookback_hours_ignores_last_sent_at(
    db_session: Session,
) -> None:
    """``fixed_lookback_hours`` 가 주어지면 ``last_sent_at`` 을 완전히 무시한다.

    last_sent_at 이 1일 전으로 설정돼 있어도, fixed_lookback_hours=24 로 호출하면
    ``from_dt = now - 24h`` 고정 구간이 반환된다.

    검증:
        - ``from_dt`` == ``now - timedelta(hours=24)``
        - ``is_first_send`` is False (fixed 구간이므로 항상 False)
        - ``fallback_days`` is None
        - snapshot_count 는 fixed 구간 안의 row 수
    """
    last_sent = datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    expected_from_dt = now - timedelta(hours=24)

    # last_sent_at 을 매우 오래 전 값으로 설정 — fixed 구간이면 이 값을 무시해야 함.
    set_setting(db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT, last_sent.isoformat())

    # 24h 구간 안에 snapshot 1건.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=now - timedelta(hours=12),
        snapshot_date_iso="2026-05-18",
        payload={"new": [9001]},
    )
    db_session.commit()

    window = compute_aggregation_window(
        db_session,
        now=now,
        fixed_lookback_hours=24,
    )

    assert window is not None
    assert isinstance(window, AggregationWindow)
    assert window.from_dt == expected_from_dt, (
        "fixed_lookback_hours=24 이면 from_dt 는 now - 24h 여야 함"
    )
    assert window.to_dt == now
    assert window.is_first_send is False
    assert window.fallback_days is None
    assert window.snapshot_count == 1


def test_compute_window_fixed_lookback_hours_without_last_sent_at(
    db_session: Session,
) -> None:
    """``last_sent_at`` 가 없어도 ``fixed_lookback_hours`` 가 있으면 고정 구간 사용.

    last_sent_at 부재 시 기본 로직은 is_first_send=True + fallback_days 구간으로
    빠지지만, fixed_lookback_hours 가 주어지면 SystemSetting 자체를 조회하지 않고
    고정 구간을 쓴다.
    """
    now = datetime(2026, 5, 19, 6, 0, 0, tzinfo=UTC)
    # last_sent_at SystemSetting row 없음 — 의도적으로 set 하지 않는다.

    window = compute_aggregation_window(
        db_session,
        now=now,
        fixed_lookback_hours=TEST_SEND_LOOKBACK_HOURS,
    )

    expected_from_dt = now - timedelta(hours=TEST_SEND_LOOKBACK_HOURS)
    assert window is not None
    assert window.from_dt == expected_from_dt
    assert window.is_first_send is False
    assert window.fallback_days is None


def test_compute_window_fixed_lookback_hours_returns_zero_count_if_no_snapshots(
    db_session: Session,
) -> None:
    """``fixed_lookback_hours`` 구간 안에 snapshot 이 0건이어도 AggregationWindow 반환.

    고정 구간도 정규 구간과 마찬가지로 0건 snapshot → AggregationWindow(snapshot_count=0).
    """
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    # 구간(now-24h, now] 밖 snapshot 하나 — 제외돼야 함.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=now - timedelta(hours=48),
        snapshot_date_iso="2026-05-17",
        payload={"new": [9999]},
    )
    db_session.commit()

    window = compute_aggregation_window(
        db_session,
        now=now,
        fixed_lookback_hours=24,
    )

    assert window is not None
    assert window.snapshot_count == 0


# ──────────────────────────────────────────────────────────────
# V. 누적 snapshot 생성 시각 목록 (task 00142)
# ──────────────────────────────────────────────────────────────


def _window_with_snapshot_created_ats(
    snapshot_created_ats: list[datetime],
) -> AggregationWindow:
    """본문 빌더 테스트용 — 주어진 스냅샷 생성 시각 목록을 가진 ``AggregationWindow``.

    ``snapshot_count`` 는 목록 길이와 일치시켜 정규 흐름
    (``compute_aggregation_window``)과 동일한 불변식을 유지한다.

    Args:
        snapshot_created_ats: 누적 snapshot 들의 생성 시각 (UTC tz-aware).

    Returns:
        ``snapshot_created_ats`` / ``snapshot_count`` 가 채워진
        ``AggregationWindow``. from_dt / to_dt 는 고정 시각.
    """
    return AggregationWindow(
        from_dt=datetime(2026, 5, 18, 0, 0, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 0, 0, tzinfo=UTC),
        snapshot_count=len(snapshot_created_ats),
        is_first_send=False,
        fallback_days=None,
        snapshot_created_ats=snapshot_created_ats,
    )


def test_compute_window_collects_snapshot_created_ats_in_ascending_order(
    db_session: Session,
) -> None:
    """``compute_aggregation_window`` 가 구간 내 snapshot 의 created_at 을
    created_at 오름차순으로 ``snapshot_created_ats`` 에 모은다 (task 00142).

    검증:
        - ``snapshot_created_ats`` 길이 == ``snapshot_count``.
        - INSERT 순서와 무관하게 created_at 오름차순으로 정렬된다.
        - 추가 쿼리 없이 기존 list 결과를 재사용한다 (값 일치만 검증).
    """
    from app.timezone import format_kst

    last_sent = datetime(2026, 5, 19, 9, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    set_setting(
        db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT, last_sent.isoformat()
    )

    # 일부러 created_at 이 늦은 row 를 먼저 INSERT — ASC 정렬을 검증하기 위함.
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 19, 11, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-19",
        payload={"new": [9101]},
    )
    _insert_snapshot_with_created_at(
        db_session,
        created_at=datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC),
        snapshot_date_iso="2026-05-18",
        payload={"new": [9102]},
    )
    db_session.commit()

    window = compute_aggregation_window(db_session, now=now)

    assert window.snapshot_count == 2
    assert len(window.snapshot_created_ats) == 2
    # created_at 오름차순 — 10:00 UTC 가 먼저, 11:00 UTC 가 나중.
    # KST 변환 후 비교 (SQLite 가 naive 로 돌려줘도 format_kst 가 UTC 가정 정규화).
    formatted = [
        format_kst(value, "%Y-%m-%d %H:%M:%S")
        for value in window.snapshot_created_ats
    ]
    assert formatted == ["2026-05-19 19:00:00", "2026-05-19 20:00:00"]


def test_compute_window_snapshot_created_ats_empty_when_no_snapshots(
    db_session: Session,
) -> None:
    """구간 내 snapshot 이 0건이면 ``snapshot_created_ats`` 는 빈 list (task 00142)."""
    last_sent = datetime(2026, 5, 19, 11, 0, 0, tzinfo=UTC)
    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    set_setting(
        db_session, SETTING_KEY_DAILY_REPORT_LAST_SENT_AT, last_sent.isoformat()
    )

    window = compute_aggregation_window(db_session, now=now)

    assert window.snapshot_count == 0
    assert window.snapshot_created_ats == []


def test_aggregation_window_snapshot_created_ats_defaults_to_empty_list() -> None:
    """``snapshot_created_ats`` 를 생략하고 ``AggregationWindow`` 를 만들면 빈 list.

    기존 테스트/호출 코드가 키워드 일부만으로 ``AggregationWindow`` 를 생성해도
    TypeError 없이 빈 list 기본값이 적용됨을 가드한다 (task 00142).
    """
    window = AggregationWindow(
        from_dt=datetime(2026, 5, 18, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, tzinfo=UTC),
        snapshot_count=0,
        is_first_send=False,
        fallback_days=None,
    )
    assert window.snapshot_created_ats == []
    # 별도 인스턴스 간에 list 가 공유되지 않아야 한다 (default_factory 보장).
    other = AggregationWindow(
        from_dt=datetime(2026, 5, 18, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, tzinfo=UTC),
        snapshot_count=0,
        is_first_send=False,
        fallback_days=None,
    )
    window.snapshot_created_ats.append(datetime(2026, 5, 18, tzinfo=UTC))
    assert other.snapshot_created_ats == []


def test_build_text_body_lists_snapshot_created_ats_in_kst() -> None:
    """text 본문이 '누적 snapshot: N건' 줄 하위에 각 생성 시각을 KST 글머리로 나열.

    검증 (task 00142 — 사용자 원문 형식):
        - UTC 입력이 KST(+9h)로 변환된다.
        - 'YYYY-MM-DD HH:MM:SS' 초 단위까지 표기.
        - '누적 snapshot: N건' 줄 바로 다음 줄들에 들여쓴 ``* 시각`` 으로 등장.
    """
    from app.email.message_builder import build_daily_report_text_body

    # UTC 2026-04-24 16:11:27 → KST 2026-04-25 01:11:27
    # UTC 2026-04-25 04:09:31 → KST 2026-04-25 13:09:31 (사용자 원문 예시와 일치)
    window = _window_with_snapshot_created_ats(
        [
            datetime(2026, 4, 24, 16, 11, 27, tzinfo=UTC),
            datetime(2026, 4, 25, 4, 9, 31, tzinfo=UTC),
        ]
    )
    text = build_daily_report_text_body(window=window, payload=_build_full_payload())

    lines = text.splitlines()
    count_index = lines.index("누적 snapshot: 2건")
    # 누적 snapshot 줄 바로 다음 두 줄이 KST 시각 글머리.
    assert lines[count_index + 1] == "  * 2026-04-25 01:11:27"
    assert lines[count_index + 2] == "  * 2026-04-25 13:09:31"


def test_build_text_body_omits_snapshot_list_when_zero() -> None:
    """snapshot 0건이면 text 본문에 시각 글머리 줄이 전혀 없다 (task 00142).

    빈 글머리(``*``)나 빈 줄이 추가로 생기지 않아야 한다.
    """
    from app.email.message_builder import build_daily_report_text_body

    window = _window_with_snapshot_created_ats([])
    text = build_daily_report_text_body(window=window, payload=_build_full_payload())

    assert "누적 snapshot: 0건" in text
    # 시각 글머리 줄(``  * ``)이 전혀 없어야 한다.
    assert not any(line.startswith("  * ") for line in text.splitlines())


def test_build_html_body_lists_snapshot_created_ats_in_kst() -> None:
    """HTML 본문 회색 메타 박스에 각 스냅샷 생성 시각이 KST 목록으로 노출된다.

    검증 (task 00142):
        - UTC → KST(+9h) 변환 + 초 단위 표기.
        - '누적 snapshot' 메타 행 value 안에 ``* 시각`` 글머리 줄로 등장.
    """
    from app.email.message_builder import build_daily_report_html_body

    window = _window_with_snapshot_created_ats(
        [
            datetime(2026, 4, 24, 16, 11, 27, tzinfo=UTC),
            datetime(2026, 4, 25, 4, 9, 31, tzinfo=UTC),
        ]
    )
    html_body = build_daily_report_html_body(
        window=window, payload=_build_full_payload()
    )

    assert "누적 snapshot" in html_body
    assert "2건" in html_body
    # KST 변환된 시각이 글머리(``* ``)와 함께 본문에 등장.
    assert "* 2026-04-25 01:11:27" in html_body
    assert "* 2026-04-25 13:09:31" in html_body


def test_build_html_body_omits_snapshot_list_when_zero() -> None:
    """snapshot 0건이면 HTML 본문 메타 박스에 시각 목록이 전혀 없다 (task 00142)."""
    from app.email.message_builder import build_daily_report_html_body

    window = _window_with_snapshot_created_ats([])
    html_body = build_daily_report_html_body(
        window=window, payload=_build_full_payload()
    )

    # '누적 snapshot' 행의 'N건' 은 그대로, 시각 글머리 줄(``* ``)은 없다.
    assert "0건" in html_body
    assert "* 2026-" not in html_body


def test_build_html_body_meta_label_cell_uses_nowrap() -> None:
    """회색 메타 박스 라벨 셀에 ``white-space:nowrap`` 가 적용된다 (task 00142).

    '누적 snapshot' 라벨이 ``width:90px`` 고정 셀 안에서 2줄로 잘리던 문제를
    nowrap 으로 해결한다 — 라벨 td 의 인라인 style 에 nowrap 이 들어가야 한다.
    """
    from app.email.message_builder import build_daily_report_html_body

    html_body = build_daily_report_html_body(
        window=_window_for_body(), payload=_build_full_payload()
    )

    # 라벨 td 의 인라인 style — width:90px + white-space:nowrap 가 함께 적용.
    assert (
        "width:90px;padding:2px 0;vertical-align:top;white-space:nowrap;"
        in html_body
    )


# ──────────────────────────────────────────────────────────────
# T. 메일 푸터 문구 — 시스템 URL + 데스크톱 최적화 + 수신 거부 안내 (task 00145)
# ──────────────────────────────────────────────────────────────


def test_build_text_body_footer_shows_new_three_line_notice() -> None:
    """text 본문 푸터가 신규 3줄 문구이며 첫 줄 끝 괄호에 시스템 URL 이 들어간다.

    검증 (task 00145):
        - 첫 줄은 '...발송되었습니다. (URL)' — 마침표 뒤 공백 1칸 + 괄호 URL.
          URL 앞에 시스템 이름을 붙이지 않는다.
        - 둘째 줄은 데스크톱 최적화 안내.
        - 셋째 줄은 계정설정 기반 메일 수신 거부 안내.
        - 옛 문구('수신 거부는 시스템 관리자에게 문의')는 더 이상 등장하지 않는다.
    """
    from app.email.message_builder import build_daily_report_text_body

    text = build_daily_report_text_body(
        window=_window_for_body(),
        payload=_build_full_payload(),
        public_base_url="http://192.168.0.10:8000",
    )

    assert (
        "이 메일은 정부사업 모니터링 시스템에서 발송되었습니다. "
        "(http://192.168.0.10:8000)"
    ) in text
    assert "이 이메일은 데스크톱 환경에 최적화 되어 있습니다." in text
    assert (
        "메일 수신을 희망하지 않으시는 분은 "
        "\"계정설정\" → \"이메일 알림 수신\"을 해제하시면 됩니다."
    ) in text
    # 옛 푸터 문구는 제거됐다.
    assert "수신 거부는 시스템 관리자에게 문의해 주세요." not in text


def test_build_html_body_footer_shows_new_three_line_notice() -> None:
    """HTML 본문 푸터도 동일한 3줄 문구로 바뀌고 첫 줄에 시스템 URL 이 들어간다.

    검증 (task 00145):
        - 첫 줄 끝 괄호에 시스템 URL, <br> 로 줄 구분.
        - 큰따옴표(\"계정설정\")는 HTML 에서 따옴표 문자 그대로 노출.
        - 옛 문구가 더 이상 등장하지 않는다.
    """
    from app.email.message_builder import build_daily_report_html_body

    html_body = build_daily_report_html_body(
        window=_window_for_body(),
        payload=_build_full_payload(),
        public_base_url="http://192.168.0.10:8000",
    )

    assert (
        "이 메일은 정부사업 모니터링 시스템에서 발송되었습니다. "
        "(http://192.168.0.10:8000)<br>"
    ) in html_body
    assert "이 이메일은 데스크톱 환경에 최적화 되어 있습니다.<br>" in html_body
    assert (
        "메일 수신을 희망하지 않으시는 분은 "
        "\"계정설정\" → \"이메일 알림 수신\"을 해제하시면 됩니다."
    ) in html_body
    assert "수신 거부는 시스템 관리자에게 문의해 주세요." not in html_body


def test_build_html_body_footer_escapes_system_url() -> None:
    """HTML 푸터의 시스템 URL 은 ``html.escape`` 로 이스케이프되어 노출된다.

    URL 에 ``&`` 등 HTML 특수문자가 포함돼도 안전하게 표시되어야 한다.
    """
    from app.email.message_builder import build_daily_report_html_body

    html_body = build_daily_report_html_body(
        window=_window_for_body(),
        payload=_build_full_payload(),
        public_base_url="http://192.168.0.10:8000/?a=1&b=2",
    )

    # & 가 &amp; 로 이스케이프된 형태로 등장.
    assert "(http://192.168.0.10:8000/?a=1&amp;b=2)" in html_body
    # 이스케이프되지 않은 raw & 형태는 푸터 URL 로 등장하지 않는다.
    assert "?a=1&b=2)" not in html_body


# pytest 가 모듈 단독 실행 가능하도록.
if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
