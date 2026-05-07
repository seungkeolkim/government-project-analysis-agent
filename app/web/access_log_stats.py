"""접근 이력 로그 파서·집계 유틸리티 (task 00073-2).

00073-1 이 기록한 access_history_YYMMDD.log (JSON-lines) 파일들을 읽어
다음 세 가지 뷰를 만드는 순수 함수 모듈이다.

- **일별 접근 통계**: 최근 N일 날짜별 총 요청 수·고유 IP 수 (빈 날 = 0 으로 채움)
- **IP별 방문 이력**: 세션 비활성 타임아웃 기반 방문 횟수 + 최초/최근 시각
- **오늘 최근 원본 로그**: 오늘 파일의 마지막 N줄 (최신 순)

## 설계 원칙
- FastAPI / SQLAlchemy / DB 의존 없음 — 순수 함수 + 파일 I/O.
- 멀티파일 reader 는 generator 로 구현해 메모리를 묶지 않는다.
- 테스트 가능성을 위해 "오늘 날짜" 가 필요한 함수는 reference_kst_date 파라미터를 받는다.

## 방문 집계 알고리즘 (aggregate_ip_history)
같은 IP 의 record 를 시간 오름차순 정렬 후, 직전 record 와의 간격이
gap_minutes 를 초과하면 새 방문(visit)으로 카운트한다. 첫 record 는 항상 새 방문.
이 방식이 사용자 원문의 "1시간 퉁" 요청보다 더 자연스럽다.
1시간 고정 윈도우가 필요하면 gap_minutes=60 으로 동일한 동작을 구현할 수 있다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from loguru import logger

from app.timezone import now_utc, to_kst


# ──────────────────────────────────────────────────────────────
# IP 필터 헬퍼
# ──────────────────────────────────────────────────────────────


def filter_records_by_ip(
    records: Iterable[dict],
    *,
    ip_list: list[str],
    mode: str,
) -> Iterator[dict]:
    """IP 목록과 모드에 따라 record 스트림을 필터링한다.

    ip_list 가 비어 있으면 필터를 적용하지 않고 모든 record 를 그대로 yield 한다.
    mode 는 라우트에서 이미 'include' | 'exclude' 로 정규화된 값이 전달된다고 가정한다.

    Args:
        records: 필터링할 record iterable. 소모(exhaust) 됨.
        ip_list: 필터 대상 IP 주소 목록. 비어 있으면 필터 없음.
        mode:    'include' — ip_list 에 있는 IP 의 record 만 yield.
                 'exclude' — ip_list 에 없는 IP 의 record 만 yield.

    Yields:
        필터 조건을 만족하는 record dict.
    """
    if not ip_list:
        yield from records
        return

    ip_set = set(ip_list)
    for record in records:
        ip = record.get("ip_address", "-")
        if mode == "exclude":
            if ip not in ip_set:
                yield record
        else:  # include
            if ip in ip_set:
                yield record


# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# get_recent_raw_rows 기본 반환 행 수 — 오늘 로그 tail 조회 안전망
_RECENT_ROWS_DEFAULT_LIMIT: int = 100


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _kst_today(reference_kst_date: date | None) -> date:
    """오늘 KST 날짜를 반환한다. 테스트용 주입 날짜가 있으면 그것을 사용한다."""
    if reference_kst_date is not None:
        return reference_kst_date
    kst_now = to_kst(now_utc())
    assert kst_now is not None
    return kst_now.date()


def _log_file_path_for_date(log_dir: Path, target_date: date) -> Path:
    """날짜에 해당하는 로그 파일 경로를 반환한다.

    파일명 형식: access_history_YYMMDD.log (예: access_history_260506.log).
    """
    date_str = target_date.strftime("%y%m%d")
    return log_dir / f"access_history_{date_str}.log"


def _parse_accessed_at(raw: str) -> datetime | None:
    """ISO 8601 접근 시각 문자열을 datetime 으로 파싱한다.

    파싱 실패 시 None 을 반환해 호출자가 해당 record 를 건너뛰게 한다.

    Args:
        raw: ISO 8601 형식 문자열 (예: \"2026-05-06T12:00:00+09:00\").

    Returns:
        파싱된 datetime, 또는 실패 시 None.
    """
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────────────────────────
# 파일 reader
# ──────────────────────────────────────────────────────────────


def iter_log_file(log_file: Path) -> Iterator[dict]:
    """JSON-lines 로그 파일 한 개를 순회하며 파싱된 dict 를 yield 한다.

    파일이 없으면 아무것도 yield 하지 않는다.
    빈 줄과 파싱 실패 줄은 WARNING 으로 기록하고 건너뛴다.

    Args:
        log_file: 읽을 로그 파일 경로.

    Yields:
        행당 파싱된 dict.
    """
    if not log_file.exists():
        return

    with log_file.open(encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    yield record
                else:
                    logger.warning(
                        "접근 이력 로그 파싱 스킵 (dict 아님): file={} line={}",
                        log_file.name,
                        lineno,
                    )
            except json.JSONDecodeError:
                logger.warning(
                    "접근 이력 로그 파싱 실패: file={} line={} content={!r}",
                    log_file.name,
                    lineno,
                    line[:80],
                )


def iter_log_records_for_days(
    log_dir: Path,
    days: int,
    *,
    reference_kst_date: date | None = None,
) -> Iterator[dict]:
    """최근 N일치 접근 이력 record 를 시간 오름차순으로 yield 한다.

    KST 날짜 기준으로 오늘부터 (today - days + 1) 까지의 파일을 오래된 날부터
    순차적으로 읽어 yield 한다. 파일이 없는 날은 건너뛴다.

    Args:
        log_dir:             로그 파일 디렉터리.
        days:                조회할 일 수. 1 이면 오늘만.
        reference_kst_date:  테스트용 기준 날짜. None 이면 오늘 KST.

    Yields:
        각 로그 record dict.
    """
    today = _kst_today(reference_kst_date)
    # range(days-1, -1, -1): 가장 오래된 날(days-1일 전)부터 오늘(0일 전)까지 순서대로
    for delta in range(days - 1, -1, -1):
        target_date = today - timedelta(days=delta)
        log_file = _log_file_path_for_date(log_dir, target_date)
        yield from iter_log_file(log_file)


# ──────────────────────────────────────────────────────────────
# 집계 함수
# ──────────────────────────────────────────────────────────────


def aggregate_daily_stats(
    records: Iterable[dict],
    *,
    days: int = 7,
    reference_kst_date: date | None = None,
) -> list[dict]:
    """일별 접근 통계를 집계한다.

    최근 days 일 범위를 기준으로, 기록이 없는 날도 0 으로 채워 항상 days 개의
    행을 날짜 오름차순으로 반환한다.

    날짜 기준은 각 record 의 accessed_at 필드 KST 날짜다.
    accessed_at 파싱 실패 또는 범위 밖 날짜의 record 는 무시한다.

    Args:
        records:             집계할 record iterable. 소모(exhaust) 됨.
        days:                집계 일 수 (기본 7).
        reference_kst_date:  테스트용 기준 날짜. None 이면 오늘 KST.

    Returns:
        날짜 오름차순 list. 각 항목::

            {"date": "YYYY-MM-DD", "total_requests": N, "unique_ips": M}
    """
    today = _kst_today(reference_kst_date)

    # 집계 대상 날짜 범위 (오래된 날부터 오늘까지)
    date_range = [today - timedelta(days=d) for d in range(days - 1, -1, -1)]
    date_str_set = {d.isoformat() for d in date_range}

    # 날짜별 카운터 초기화 (빈 날 = 0 보장)
    request_counts: dict[str, int] = {d.isoformat(): 0 for d in date_range}
    unique_ip_sets: dict[str, set[str]] = {d.isoformat(): set() for d in date_range}

    for record in records:
        raw_accessed_at = record.get("accessed_at", "")
        dt = _parse_accessed_at(raw_accessed_at)
        if dt is None:
            continue
        # accessed_at 이 +09:00 tz-aware 이면 .date() 가 KST 날짜를 반환한다.
        record_date_str = dt.date().isoformat()
        if record_date_str not in date_str_set:
            # 집계 범위 밖 날짜는 무시
            continue
        request_counts[record_date_str] += 1
        ip = record.get("ip_address", "-")
        unique_ip_sets[record_date_str].add(ip)

    return [
        {
            "date": d.isoformat(),
            "total_requests": request_counts[d.isoformat()],
            "unique_ips": len(unique_ip_sets[d.isoformat()]),
        }
        for d in date_range
    ]


def aggregate_ip_history(
    records: Iterable[dict],
    *,
    gap_minutes: int,
) -> list[dict]:
    """IP별 방문 이력을 집계한다.

    같은 IP 의 record 를 시간 오름차순으로 정렬한 뒤, 직전 record 와의 시간 간격이
    gap_minutes 를 초과하면 새 방문(visit)으로 카운트한다. 첫 record 는 항상 새 방문.

    gap_minutes=30 : 30분 비활성 → 새 방문 (기본 설정, 더 자연스러운 집계)
    gap_minutes=60 : 1시간 고정 윈도우 (사용자 원문의 "1시간 퉁" 동작과 동치)

    Args:
        records:     집계할 record iterable. 소모(exhaust) 됨.
        gap_minutes: 세션 비활성 판단 기준 (분). 라우트가 settings 에서 주입한다.

    Returns:
        최근 방문 순(last_seen 내림차순) list. 각 항목::

            {
                "ip_address":     str,   # 접근 IP
                "first_seen":     str,   # 최초 접근 시각 (원본 ISO 8601 문자열)
                "last_seen":      str,   # 최근 접근 시각 (원본 ISO 8601 문자열)
                "total_requests": int,   # 총 요청 수
                "visits":         int,   # 방문 횟수 (세션 타임아웃 기반)
            }
    """
    gap_delta = timedelta(minutes=gap_minutes)

    # IP → [(accessed_at datetime, 원본 문자열)] 묶기
    ip_records: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for record in records:
        ip = record.get("ip_address", "-")
        raw_at = record.get("accessed_at", "")
        dt = _parse_accessed_at(raw_at)
        if dt is None:
            continue
        ip_records[ip].append((dt, raw_at))

    result: list[dict] = []
    for ip, entries in ip_records.items():
        # 시간 오름차순 정렬
        entries.sort(key=lambda e: e[0])

        total_requests = len(entries)
        first_seen_str = entries[0][1]
        last_seen_str = entries[-1][1]

        # 방문 횟수: 직전 record 와 간격이 gap_delta 를 초과하면 새 방문
        visits = 0
        prev_dt: datetime | None = None
        for current_dt, _ in entries:
            if prev_dt is None or (current_dt - prev_dt) > gap_delta:
                visits += 1
            prev_dt = current_dt

        result.append(
            {
                "ip_address": ip,
                "first_seen": first_seen_str,
                "last_seen": last_seen_str,
                "total_requests": total_requests,
                "visits": visits,
            }
        )

    # last_seen 내림차순 정렬 — ISO 8601 KST 문자열은 사전순 == 시간순
    result.sort(key=lambda r: r["last_seen"], reverse=True)
    return result


def get_recent_raw_rows(
    log_dir: Path,
    *,
    limit: int = _RECENT_ROWS_DEFAULT_LIMIT,
    reference_kst_date: date | None = None,
    ip_list: list[str] | None = None,
    filter_mode: str = "include",
) -> list[dict]:
    """오늘 접근 이력 로그의 최근 N줄을 최신 순으로 반환한다.

    오늘 파일이 없으면 빈 리스트를 반환한다.
    ip_list 가 주어지면 필터 적용 후 limit 슬라이싱 — "필터된 결과 중 최신 N건" 이 된다.
    MVP: 파일 전체를 메모리에 읽은 뒤 tail slice (1MB 미만 가정).

    Args:
        log_dir:             로그 파일 디렉터리.
        limit:               반환할 최대 줄 수. 기본 _RECENT_ROWS_DEFAULT_LIMIT.
        reference_kst_date:  테스트용 기준 날짜. None 이면 오늘 KST.
        ip_list:             IP 필터 목록. None 또는 빈 리스트면 필터 없음.
        filter_mode:         'include' | 'exclude'. 라우트에서 정규화된 값을 전달한다.

    Returns:
        최신 항목이 앞에 오는 list of dict (최대 limit 개).
    """
    today = _kst_today(reference_kst_date)
    log_file = _log_file_path_for_date(log_dir, today)
    rows = list(iter_log_file(log_file))
    # IP 필터 적용 후 limit 슬라이싱 → "필터된 결과 중 최신 N건" 보장
    if ip_list:
        rows = list(filter_records_by_ip(rows, ip_list=ip_list, mode=filter_mode))
    return list(reversed(rows[-limit:]))


__all__ = [
    "aggregate_daily_stats",
    "aggregate_ip_history",
    "filter_records_by_ip",
    "get_recent_raw_rows",
    "iter_log_file",
    "iter_log_records_for_days",
]
