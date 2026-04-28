"""KST 표시·계산용 timezone 헬퍼 모듈 (task 00040).

배경 (사용자 원문 task 00040):
    프로젝트 컨벤션은 "DB 저장은 UTC tz-aware 유지, 사용 경계(표시·계산·입력·
    cron) 에서 Asia/Seoul 변환" 이다. 본 모듈은 그 사용 경계에서 호출할 공용
    헬퍼 API 를 단일 모듈로 모아 제공한다. 후속 subtask
    (Jinja2 필터 / APScheduler / 외부 응답 파싱 / backfill / 검증) 가 본
    모듈의 함수만 import 해 KST 변환을 일관되게 적용한다.

설계 결정:
    - ``ZoneInfo("Asia/Seoul")`` 은 표준 라이브러리만 사용한다(Python 3.11+).
      추가 의존성 없음. 컨테이너 ``TZ=Asia/Seoul`` env 가 빠진 환경에서도
      코드 레벨에서 명시적으로 KST 를 사용하므로 호스트 tz 비의존이다.
    - 본 모듈은 ``app.db.models`` 를 import 하지 않는다. 의존 방향이 반대로,
      ``app.db.models.as_utc`` 가 본 모듈을 사용하지도, 본 모듈이 ORM 코드를
      쓰지도 않는다. 두 헬퍼는 다른 레이어이므로 상호 참조하지 않고 호출
      측에서 직렬 결합한다 (아래 결합 사용 패턴 참조).
    - ``now_utc`` 는 ``app.db.models._utcnow`` 의 공용 버전이지만 본 task 에서
      기존 호출부를 일괄 교체하지 않는다. 회귀 면적을 분리하기 위해 후속
      subtask 에서 경계별로 점진 도입한다.

``app.db.models.as_utc`` 와의 결합 사용 패턴:
    SQLite 백엔드는 ``DateTime(timezone=True)`` 컬럼이라도 SELECT 시 tz 정보를
    잃은 naive ``datetime`` 으로 돌려준다 (``app/db/models.py:73`` 의 ``as_utc``
    docstring 참조). 따라서 비교/연산이 필요한 경로에서는 ``as_utc`` 로 양쪽을
    UTC tz-aware 로 정규화하고, 사용자 표시 직전에는 ``to_kst`` / ``format_kst``
    로 KST tz-aware 변환을 수행한다. 두 헬퍼는 직렬 결합이며 한 줄에서 같이
    호출되지는 않는다::

        # 비교 경로 (저장값 vs 현재 시각)
        if as_utc(row.expires_at) <= as_utc(now_utc()):
            ...

        # 표시 경로 (템플릿/로그 직전)
        return format_kst(row.scraped_at)

API 표면 (사용자 원문 그대로):
    - :data:`KST` — ``ZoneInfo("Asia/Seoul")`` 싱글턴.
    - :func:`to_kst` — 어떤 입력(None/naive/aware) 이든 KST tz-aware 로 정규화.
    - :func:`now_utc` — 저장용 현재 시각 (UTC tz-aware).
    - :func:`now_kst` — 표시용 현재 시각 (KST tz-aware).
    - :func:`format_kst` — KST 문자열 포맷팅. None 은 빈 문자열로.
    - :func:`kst_date_boundaries` — KST 날짜의 [00:00, 24:00) 구간을 UTC
      tz-aware 한 쌍으로. Phase 5 의 일자 GROUP BY 기반.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────


# 프로젝트 단일 표시 timezone. 사용자 원문 결정 — 사용자별/.env 설정은 없으며
# 운영 인스턴스 단위로 KST 고정이다. ZoneInfo 인스턴스는 immutable·thread-safe
# 라 모듈 수준 싱글턴으로 노출한다.
KST: ZoneInfo = ZoneInfo("Asia/Seoul")


# ──────────────────────────────────────────────────────────────
# 시각 정규화
# ──────────────────────────────────────────────────────────────


def to_kst(value: datetime | None) -> datetime | None:
    """입력 ``datetime`` 을 KST tz-aware 로 정규화한다.

    입력 형태별 처리:
        - ``None`` → ``None`` 그대로 반환 (호출 측의 None-guard 부담을 줄임).
        - naive ``datetime`` → 프로젝트 컨벤션상 "DB 저장은 UTC" 이므로 **UTC
          가정** 으로 ``tzinfo=UTC`` 를 부착한 뒤 KST 로 변환한다. SQLite SELECT
          로 tz 가 손실된 naive 값도 동일하게 처리되어 실용적이다.
        - tz-aware ``datetime`` (UTC / KST / 그 외 임의 tz) → ``astimezone(KST)``
          로 KST tz-aware 로 변환한다.

    이 함수는 사용자 표시 직전에만 호출하는 것이 원칙이다. 비교/저장 경로에서
    KST 로 변환하면 컨벤션이 흐트러진다 — 비교는 UTC tz-aware 양쪽으로
    정렬해 수행한다 (``app.db.models.as_utc`` 와의 결합 사용 패턴 참조).

    Args:
        value: 변환할 ``datetime`` 또는 ``None``.

    Returns:
        KST tz-aware ``datetime``. ``value`` 가 None 이면 None.
    """
    if value is None:
        return None

    # naive 입력은 프로젝트 컨벤션 "저장값은 UTC" 가정에 따라 UTC tzinfo 부착.
    # SQLite SELECT 로 tz 가 손실된 컬럼값을 그대로 받아도 의미가 보존된다.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(KST)


# ──────────────────────────────────────────────────────────────
# 현재 시각
# ──────────────────────────────────────────────────────────────


def now_utc() -> datetime:
    """저장용 현재 시각을 UTC tz-aware 로 반환한다.

    ``app.db.models._utcnow`` 의 공용 버전이며 동치 결과를 돌려준다. 본 task
    에서 기존 ``_utcnow`` / ``datetime.now(tz=UTC)`` 호출부를 일괄 교체하지는
    않는다 — 회귀 면적을 분리하기 위해 후속 subtask 가 경계별로 점진 도입한다.

    Returns:
        UTC tz-aware ``datetime``.
    """
    return datetime.now(tz=UTC)


def now_kst() -> datetime:
    """표시용 현재 시각을 KST tz-aware 로 반환한다.

    저장 / 비교 경로에서는 :func:`now_utc` 를 사용하고, 사용자 표시·로그 경계
    에서만 본 함수를 호출한다.

    Returns:
        KST tz-aware ``datetime``.
    """
    return datetime.now(tz=KST)


# ──────────────────────────────────────────────────────────────
# 표시 포맷
# ──────────────────────────────────────────────────────────────


# 기본 표시 포맷. 화면/로그에서 가장 자주 쓰는 분 단위 KST 표현.
# 초 단위가 필요한 경우 호출자가 ``fmt`` 인자를 지정한다.
DEFAULT_KST_FORMAT: str = "%Y-%m-%d %H:%M"


def format_kst(value: datetime | None, fmt: str = DEFAULT_KST_FORMAT) -> str:
    """``datetime`` 을 KST 기준 문자열로 포맷한다.

    None 처리: ``value`` 가 ``None`` 이면 ``""`` (빈 문자열) 을 반환한다. 가이드
    상 fallback 텍스트(예: "—") 분기는 호출 측 템플릿에서 ``"" or "—"`` 또는
    Jinja ``default`` 필터로 처리하도록 위임해, 헬퍼는 단일 책임(포맷)만 갖는다.

    Args:
        value: 포맷할 ``datetime``. None 허용.
        fmt:   ``strftime`` 포맷 문자열. 기본값은 분 단위 (``"%Y-%m-%d %H:%M"``).

    Returns:
        ``value`` 를 KST tz-aware 로 변환한 뒤 ``strftime(fmt)`` 결과. None 입력
        은 ``""``.
    """
    if value is None:
        return ""

    kst_value = to_kst(value)
    # to_kst 는 None 입력에서만 None 을 반환하므로 위 가드 이후엔 항상 datetime.
    assert kst_value is not None
    return kst_value.strftime(fmt)


# ──────────────────────────────────────────────────────────────
# 일자 경계 (Phase 5 GROUP BY 기반)
# ──────────────────────────────────────────────────────────────


def kst_date_boundaries(target_date: date) -> tuple[datetime, datetime]:
    """KST 날짜 ``target_date`` 의 [00:00, 24:00) 을 UTC tz-aware 구간으로 반환.

    Phase 5 (snapshot+대시보드) 의 일자 GROUP BY 가 본 함수를 사용한다. DB 의
    ``DateTime(timezone=True)`` 컬럼은 UTC 로 저장되므로, "KST 기준 어느 하루"
    를 SQL ``WHERE col >= start AND col < end`` 로 잘라내려면 양쪽 경계를 UTC
    tz-aware 로 변환해 둘 필요가 있다. 단순히 ``DATE(col)`` 를 쓰면 9시간 차이
    때문에 KST 일자 경계와 어긋난다.

    예시 (2026-04-28 KST 기준):
        - 시작: ``2026-04-28 00:00 +09:00`` → ``2026-04-27 15:00 UTC``
        - 종료: ``2026-04-29 00:00 +09:00`` → ``2026-04-28 15:00 UTC``
        - SQL: ``WHERE col >= '2026-04-27 15:00:00+00:00'``
                ``AND col < '2026-04-28 15:00:00+00:00'``

    Args:
        target_date: KST 기준 날짜 (``datetime.date``).

    Returns:
        ``(start_utc, end_utc)`` 튜플. 둘 다 UTC tz-aware 이며 ``start_utc`` 는
        해당 KST 날짜의 자정, ``end_utc`` 는 그 다음 KST 날짜의 자정에 해당한다.
        반-open 구간 ``[start_utc, end_utc)`` 형태로 사용한다.
    """
    # KST 자정 — naive datetime 에 ``tzinfo=KST`` 를 부착해 만든다. ``date`` 입력은
    # tz 정보가 없으므로 ``datetime.combine`` 이 가장 의도가 명확하다.
    start_kst = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        0,
        0,
        0,
        tzinfo=KST,
    )
    end_kst = start_kst + timedelta(days=1)

    # 저장 경로(UTC) 와 직접 비교할 수 있도록 UTC tz-aware 로 정렬한다.
    return start_kst.astimezone(UTC), end_kst.astimezone(UTC)


__all__ = [
    "DEFAULT_KST_FORMAT",
    "KST",
    "format_kst",
    "kst_date_boundaries",
    "now_kst",
    "now_utc",
    "to_kst",
]
