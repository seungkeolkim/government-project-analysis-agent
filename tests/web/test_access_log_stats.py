"""app.web.access_log_stats 단위 테스트 (task 00073-2).

access_history_YYMMDD.log 파일을 tmp_path 에 직접 생성해 각 집계 함수를 검증한다.
외부 의존(FastAPI / DB / 네트워크) 없이 순수 파일 I/O 만 사용한다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.web.access_log_stats import (
    aggregate_daily_stats,
    aggregate_ip_history,
    get_recent_raw_rows,
    iter_log_file,
    iter_log_records_for_days,
)

# 테스트 기준일 (KST): 실제 날짜와 무관하게 결정론적으로 고정
_REF_DATE = date(2026, 5, 6)


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _write_log_file(log_dir: Path, target_date: date, entries: list[dict]) -> Path:
    """테스트용 access_history_YYMMDD.log 파일을 생성한다."""
    date_str = target_date.strftime("%y%m%d")
    log_file = log_dir / f"access_history_{date_str}.log"
    with log_file.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_file


def _entry(
    ip: str,
    accessed_at: str,
    path: str = "/",
    method: str = "GET",
    status_code: int = 200,
    user_id: str = "-",
) -> dict:
    """테스트용 로그 entry dict 를 만든다."""
    return {
        "ip_address": ip,
        "accessed_at": accessed_at,
        "path": path,
        "method": method,
        "user_agent": "TestBrowser/1.0",
        "user_id": user_id,
        "status_code": status_code,
    }


# ──────────────────────────────────────────────────────────────
# iter_log_file
# ──────────────────────────────────────────────────────────────


class TestIterLogFile:
    def test_yields_all_valid_entries(self, tmp_path: Path) -> None:
        """유효한 JSON-lines 파일의 모든 행을 순서대로 yield 한다."""
        entries = [
            _entry("1.2.3.4", "2026-05-06T10:00:00+09:00"),
            _entry("5.6.7.8", "2026-05-06T11:00:00+09:00"),
        ]
        log_file = _write_log_file(tmp_path, _REF_DATE, entries)
        result = list(iter_log_file(log_file))
        assert len(result) == 2
        assert result[0]["ip_address"] == "1.2.3.4"
        assert result[1]["ip_address"] == "5.6.7.8"

    def test_returns_empty_for_nonexistent_file(self, tmp_path: Path) -> None:
        """파일이 없으면 아무것도 yield 하지 않는다."""
        non_existent = tmp_path / "access_history_991231.log"
        assert list(iter_log_file(non_existent)) == []

    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        """파싱 불가 줄은 건너뛰고 유효한 줄만 반환한다."""
        log_file = tmp_path / "access_history_260506.log"
        log_file.write_text(
            '{"ip_address": "1.2.3.4", "accessed_at": "2026-05-06T10:00:00+09:00"}\n'
            "not valid json\n"
            '{"ip_address": "5.6.7.8", "accessed_at": "2026-05-06T11:00:00+09:00"}\n',
            encoding="utf-8",
        )
        result = list(iter_log_file(log_file))
        assert len(result) == 2
        assert result[0]["ip_address"] == "1.2.3.4"
        assert result[1]["ip_address"] == "5.6.7.8"

    def test_skips_empty_lines(self, tmp_path: Path) -> None:
        """빈 줄을 건너뛰고 유효한 줄만 반환한다."""
        log_file = tmp_path / "access_history_260506.log"
        log_file.write_text(
            '{"ip_address": "1.2.3.4", "accessed_at": "2026-05-06T10:00:00+09:00"}\n'
            "\n"
            '{"ip_address": "5.6.7.8", "accessed_at": "2026-05-06T11:00:00+09:00"}\n',
            encoding="utf-8",
        )
        result = list(iter_log_file(log_file))
        assert len(result) == 2

    def test_preserves_unicode_user_agent(self, tmp_path: Path) -> None:
        """한글 user_agent 가 ensure_ascii=False 로 기록된 경우도 정상 파싱한다."""
        entry = _entry("1.1.1.1", "2026-05-06T10:00:00+09:00")
        entry["user_agent"] = "한글브라우저/1.0"
        log_file = _write_log_file(tmp_path, _REF_DATE, [entry])
        result = list(iter_log_file(log_file))
        assert result[0]["user_agent"] == "한글브라우저/1.0"


# ──────────────────────────────────────────────────────────────
# iter_log_records_for_days
# ──────────────────────────────────────────────────────────────


class TestIterLogRecordsForDays:
    def test_yields_oldest_first(self, tmp_path: Path) -> None:
        """오래된 날 파일을 먼저 yield 해 시간 오름차순이 유지된다."""
        yesterday = _REF_DATE - timedelta(days=1)
        _write_log_file(tmp_path, yesterday, [_entry("1.1.1.1", "2026-05-05T10:00:00+09:00")])
        _write_log_file(tmp_path, _REF_DATE, [_entry("2.2.2.2", "2026-05-06T10:00:00+09:00")])

        records = list(
            iter_log_records_for_days(tmp_path, days=2, reference_kst_date=_REF_DATE)
        )
        assert len(records) == 2
        assert records[0]["ip_address"] == "1.1.1.1"  # 어제 먼저
        assert records[1]["ip_address"] == "2.2.2.2"  # 오늘 나중

    def test_skips_missing_days(self, tmp_path: Path) -> None:
        """파일이 없는 날은 건너뛴다."""
        yesterday = _REF_DATE - timedelta(days=1)
        _write_log_file(tmp_path, yesterday, [_entry("1.1.1.1", "2026-05-05T10:00:00+09:00")])
        # 오늘 파일은 생성하지 않음

        records = list(
            iter_log_records_for_days(tmp_path, days=2, reference_kst_date=_REF_DATE)
        )
        assert len(records) == 1
        assert records[0]["ip_address"] == "1.1.1.1"

    def test_returns_empty_if_no_files(self, tmp_path: Path) -> None:
        """로그 파일이 하나도 없으면 빈 iterator 를 반환한다."""
        records = list(
            iter_log_records_for_days(tmp_path, days=7, reference_kst_date=_REF_DATE)
        )
        assert records == []

    def test_days_1_reads_only_today(self, tmp_path: Path) -> None:
        """days=1 이면 오늘 파일만 읽는다."""
        yesterday = _REF_DATE - timedelta(days=1)
        _write_log_file(tmp_path, yesterday, [_entry("1.1.1.1", "2026-05-05T10:00:00+09:00")])
        _write_log_file(tmp_path, _REF_DATE, [_entry("2.2.2.2", "2026-05-06T10:00:00+09:00")])

        records = list(
            iter_log_records_for_days(tmp_path, days=1, reference_kst_date=_REF_DATE)
        )
        assert len(records) == 1
        assert records[0]["ip_address"] == "2.2.2.2"


# ──────────────────────────────────────────────────────────────
# aggregate_daily_stats
# ──────────────────────────────────────────────────────────────


class TestAggregateDailyStats:
    def test_returns_exactly_n_days(self) -> None:
        """기록이 없어도 항상 days 개의 행을 반환한다."""
        result = aggregate_daily_stats([], days=7, reference_kst_date=_REF_DATE)
        assert len(result) == 7

    def test_all_zeros_when_no_records(self) -> None:
        """기록이 없으면 모든 행이 0 이다."""
        result = aggregate_daily_stats([], days=3, reference_kst_date=_REF_DATE)
        for row in result:
            assert row["total_requests"] == 0
            assert row["unique_ips"] == 0

    def test_dates_are_ascending(self) -> None:
        """날짜가 오름차순으로 정렬된다."""
        result = aggregate_daily_stats([], days=5, reference_kst_date=_REF_DATE)
        dates = [r["date"] for r in result]
        assert dates == sorted(dates)

    def test_last_date_is_reference_date(self) -> None:
        """마지막 행의 날짜가 기준일(오늘)이다."""
        result = aggregate_daily_stats([], days=7, reference_kst_date=_REF_DATE)
        assert result[-1]["date"] == "2026-05-06"

    def test_counts_total_requests(self) -> None:
        """같은 날 record 수를 total_requests 로 집계한다."""
        records = [
            _entry("1.1.1.1", "2026-05-06T09:00:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T10:00:00+09:00"),
            _entry("2.2.2.2", "2026-05-06T11:00:00+09:00"),
        ]
        result = aggregate_daily_stats(records, days=1, reference_kst_date=_REF_DATE)
        assert result[0]["total_requests"] == 3

    def test_counts_unique_ips(self) -> None:
        """같은 날 고유 IP 수를 unique_ips 로 집계한다."""
        records = [
            _entry("1.1.1.1", "2026-05-06T09:00:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T10:00:00+09:00"),  # 중복 IP
            _entry("2.2.2.2", "2026-05-06T11:00:00+09:00"),
        ]
        result = aggregate_daily_stats(records, days=1, reference_kst_date=_REF_DATE)
        assert result[0]["unique_ips"] == 2

    def test_out_of_range_records_ignored(self) -> None:
        """집계 범위 밖 날짜의 record 는 무시한다."""
        # 8일 전 record — 7일 범위 밖
        old_record = _entry("3.3.3.3", "2026-04-28T09:00:00+09:00")
        result = aggregate_daily_stats([old_record], days=7, reference_kst_date=_REF_DATE)
        total = sum(r["total_requests"] for r in result)
        assert total == 0

    def test_multiple_days_bucketed_correctly(self) -> None:
        """서로 다른 날의 record 가 올바른 날짜 버킷에 집계된다."""
        yesterday = _REF_DATE - timedelta(days=1)
        records = [
            _entry("1.1.1.1", "2026-05-05T10:00:00+09:00"),  # 어제
            _entry("2.2.2.2", "2026-05-05T11:00:00+09:00"),  # 어제
            _entry("3.3.3.3", "2026-05-06T10:00:00+09:00"),  # 오늘
        ]
        result = aggregate_daily_stats(records, days=2, reference_kst_date=_REF_DATE)
        assert len(result) == 2
        yesterday_row = result[0]
        today_row = result[1]
        assert yesterday_row["date"] == yesterday.isoformat()
        assert yesterday_row["total_requests"] == 2
        assert yesterday_row["unique_ips"] == 2
        assert today_row["total_requests"] == 1
        assert today_row["unique_ips"] == 1


# ──────────────────────────────────────────────────────────────
# aggregate_ip_history
# ──────────────────────────────────────────────────────────────


class TestAggregateIpHistory:
    def test_single_ip_single_request(self) -> None:
        """record 가 1개면 visits=1, total_requests=1."""
        records = [_entry("1.1.1.1", "2026-05-06T10:00:00+09:00")]
        result = aggregate_ip_history(records, gap_minutes=30)
        assert len(result) == 1
        row = result[0]
        assert row["ip_address"] == "1.1.1.1"
        assert row["visits"] == 1
        assert row["total_requests"] == 1

    def test_within_gap_is_same_visit(self) -> None:
        """gap 이내 연속 요청은 같은 방문(visits=1)으로 집계된다."""
        records = [
            _entry("1.1.1.1", "2026-05-06T10:00:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T10:20:00+09:00"),  # 20분 후 → 30분 gap 이내
        ]
        result = aggregate_ip_history(records, gap_minutes=30)
        assert result[0]["visits"] == 1
        assert result[0]["total_requests"] == 2

    def test_beyond_gap_is_new_visit(self) -> None:
        """gap 을 초과한 요청은 새 방문으로 카운트된다."""
        records = [
            _entry("1.1.1.1", "2026-05-06T10:00:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T10:31:00+09:00"),  # 31분 후 → 30분 gap 초과
        ]
        result = aggregate_ip_history(records, gap_minutes=30)
        assert result[0]["visits"] == 2

    def test_exact_gap_is_not_new_visit(self) -> None:
        """간격이 gap_minutes 와 정확히 같으면 새 방문이 아니다 (초과여야 새 방문)."""
        records = [
            _entry("1.1.1.1", "2026-05-06T10:00:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T10:30:00+09:00"),  # 정확히 30분 = 초과 아님
        ]
        result = aggregate_ip_history(records, gap_minutes=30)
        assert result[0]["visits"] == 1

    def test_sorted_by_last_seen_descending(self) -> None:
        """last_seen 내림차순(최근 방문 먼저) 으로 정렬된다."""
        records = [
            _entry("1.1.1.1", "2026-05-06T09:00:00+09:00"),  # 이른 시각
            _entry("2.2.2.2", "2026-05-06T11:00:00+09:00"),  # 최근 시각
        ]
        result = aggregate_ip_history(records, gap_minutes=30)
        assert result[0]["ip_address"] == "2.2.2.2"
        assert result[1]["ip_address"] == "1.1.1.1"

    def test_first_and_last_seen_correct(self) -> None:
        """first_seen / last_seen 이 해당 IP 의 최초/최근 접근 시각과 일치한다."""
        records = [
            _entry("1.1.1.1", "2026-05-06T10:00:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T11:00:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T12:00:00+09:00"),
        ]
        result = aggregate_ip_history(records, gap_minutes=30)
        row = result[0]
        assert row["first_seen"] == "2026-05-06T10:00:00+09:00"
        assert row["last_seen"] == "2026-05-06T12:00:00+09:00"

    def test_60_minute_gap_user_story(self) -> None:
        """gap_minutes=60 은 사용자 원문의 '1시간 퉁' 동작과 동치다."""
        records = [
            _entry("1.1.1.1", "2026-05-06T10:00:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T10:59:00+09:00"),  # 59분 → 같은 방문
            _entry("1.1.1.1", "2026-05-06T12:00:00+09:00"),  # 61분 후 → 새 방문
        ]
        result = aggregate_ip_history(records, gap_minutes=60)
        assert result[0]["visits"] == 2
        assert result[0]["total_requests"] == 3

    def test_multiple_ips_separate_buckets(self) -> None:
        """서로 다른 IP 가 별도 버킷으로 집계된다."""
        records = [
            _entry("1.1.1.1", "2026-05-06T10:00:00+09:00"),
            _entry("2.2.2.2", "2026-05-06T10:05:00+09:00"),
            _entry("1.1.1.1", "2026-05-06T10:10:00+09:00"),
        ]
        result = aggregate_ip_history(records, gap_minutes=30)
        ip_map = {r["ip_address"]: r for r in result}
        assert ip_map["1.1.1.1"]["total_requests"] == 2
        assert ip_map["2.2.2.2"]["total_requests"] == 1

    def test_empty_records(self) -> None:
        """빈 iterable 은 빈 list 를 반환한다."""
        assert aggregate_ip_history([], gap_minutes=30) == []

    def test_skips_records_with_invalid_accessed_at(self) -> None:
        """accessed_at 파싱 실패 record 는 집계에서 제외된다."""
        records = [
            {"ip_address": "1.1.1.1", "accessed_at": "not-a-date"},
            _entry("2.2.2.2", "2026-05-06T10:00:00+09:00"),
        ]
        result = aggregate_ip_history(records, gap_minutes=30)
        assert len(result) == 1
        assert result[0]["ip_address"] == "2.2.2.2"


# ──────────────────────────────────────────────────────────────
# get_recent_raw_rows
# ──────────────────────────────────────────────────────────────


class TestGetRecentRawRows:
    def test_returns_most_recent_first(self, tmp_path: Path) -> None:
        """반환 순서가 최신 항목 먼저(역순)다."""
        entries = [
            _entry("1.1.1.1", f"2026-05-06T{h:02d}:00:00+09:00")
            for h in range(5)  # 00시 ~ 04시
        ]
        _write_log_file(tmp_path, _REF_DATE, entries)

        result = get_recent_raw_rows(tmp_path, limit=3, reference_kst_date=_REF_DATE)
        assert len(result) == 3
        # 04시가 먼저 나와야 한다
        assert result[0]["accessed_at"] == "2026-05-06T04:00:00+09:00"
        assert result[2]["accessed_at"] == "2026-05-06T02:00:00+09:00"

    def test_returns_empty_if_no_file(self, tmp_path: Path) -> None:
        """오늘 파일이 없으면 빈 리스트를 반환한다."""
        result = get_recent_raw_rows(tmp_path, reference_kst_date=_REF_DATE)
        assert result == []

    def test_returns_all_if_fewer_than_limit(self, tmp_path: Path) -> None:
        """전체 줄 수가 limit 보다 적으면 전부 반환한다."""
        entries = [_entry("1.1.1.1", "2026-05-06T10:00:00+09:00")]
        _write_log_file(tmp_path, _REF_DATE, entries)
        result = get_recent_raw_rows(tmp_path, limit=100, reference_kst_date=_REF_DATE)
        assert len(result) == 1

    def test_respects_limit(self, tmp_path: Path) -> None:
        """limit 개 이상이면 limit 개만 반환한다."""
        entries = [
            _entry("1.1.1.1", f"2026-05-06T{h:02d}:00:00+09:00")
            for h in range(10)
        ]
        _write_log_file(tmp_path, _REF_DATE, entries)
        result = get_recent_raw_rows(tmp_path, limit=5, reference_kst_date=_REF_DATE)
        assert len(result) == 5
