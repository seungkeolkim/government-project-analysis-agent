"""IRIS / NTIS 외부 응답 KST 가정 미적용 row 일회성 backfill (task 00040-5).

배경:
    이전에는 ``app/cli.py::_parse_datetime_text`` 가 외부 응답 텍스트(예:
    ``\"2026.05.01\"``)를 naive 로 파싱한 뒤 그대로 ``tzinfo=UTC`` 를 부착했다.
    하지만 IRIS / NTIS 가 보내는 텍스트는 모두 한국 현지 시각(Asia/Seoul) 의미
    이므로, 의도된 \"KST 자정\" 이 \"UTC 자정\" 으로 저장되어 9시간 후로 어긋났다.
    audit (docs/timezone_audit.md §5 / §7) 가 운영 SQLite 53/53 row 모두에서
    misassumed-as-UTC 패턴을 확인했다.

    task 00040-5 가 ``_parse_datetime_text`` 를 KST 가정 → UTC 변환으로 수정
    했고, 본 스크립트는 이미 잘못 저장된 기존 row 를 동일 로직으로 일회성
    재계산 후 UPDATE 한다.

식별 조건 (guidance 명시):
    raw_metadata.list_row.deadline_at_text = '2026.05.01' 일 때,
        - 컬럼 = '2026-05-01T00:00:00+00:00' → **misapply** (UTC 가정), backfill 대상.
        - 컬럼 = '2026-04-30T15:00:00+00:00' → 정상 (이미 KST 가정 변환됨), skip.

동작 (dry-run 이 기본):
    - announcements 전 row 를 순회한다.
    - ``received_at`` / ``deadline_at`` 컬럼별로 raw_metadata 의 동일 텍스트
      필드에서 새로 ``_parse_datetime_text`` 를 호출해 \"기대값\" 을 계산.
    - SQLite SELECT 가 tz 를 떨어뜨려 naive 로 돌려준 컬럼값을 ``as_utc`` 로
      UTC tz-aware 로 정규화해 기대값과 비교.
    - 두 값이 다르면 backfill 대상. 같으면 idempotent skip.
    - raw text 가 없으면(또는 파싱 실패) skip + 경고 로그.
    - --apply 가 켜져 있을 때만 UPDATE 한다. dry-run 은 통계만 출력.

사용법:
    # 1) DB 백업 (운영자 책임):
    python scripts/backup_db.py
    # 2) dry-run 으로 영향 범위 확인:
    python scripts/backfill_kst_assumption.py
    # 3) 만족하면 실제 적용:
    python scripts/backfill_kst_assumption.py --apply

옵션:
    --apply             DB 에 실제 UPDATE 를 수행한다. 생략 시 dry-run.
    --batch-size N      중간 commit 간격 (기본 100).
    --db-url URL        접속할 SQLAlchemy DB URL.
                        생략 시 ``app.config.get_settings().db_url``.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가 (scripts/ 에서 app 패키지를 임포트하기 위해).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.cli import _parse_datetime_text  # noqa: E402
from app.db.models import Announcement, as_utc  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402


# 처리 대상 컬럼 매핑 — (ORM 컬럼 이름, raw_metadata.list_row 의 텍스트 키).
# 본 task 의 사용자 원문은 \"마감일·접수시작·등록일\" 을 명시했지만, 현재 코드의
# 실제 저장 경로는 ``_build_announcement_payload`` 에서 ``received_at`` /
# ``deadline_at`` 두 개만 ``_parse_datetime_text`` 를 거친다 (audit §2.5).
# \"등록일\" 등 추가 필드가 향후 들어오면 이 튜플에 한 줄씩 추가하면 된다.
_TARGET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("received_at", "received_at_text"),
    ("deadline_at", "deadline_at_text"),
)


@dataclass
class FixCandidate:
    """단일 row · 단일 컬럼의 backfill 후보 정보.

    Attributes:
        announcement_id: Announcement.id
        column_name:     수정할 컬럼명 (``received_at`` / ``deadline_at``).
        raw_text:        raw_metadata.list_row 의 원문 텍스트.
        before_value:    현재 컬럼 값 (UTC tz-aware 로 정규화된 datetime).
        after_value:     새로 계산된 기대 값 (UTC tz-aware datetime).
    """

    announcement_id: int
    column_name: str
    raw_text: str
    before_value: datetime
    after_value: datetime


@dataclass
class ScanStats:
    """스캔 결과 통계.

    Attributes:
        total_rows:           순회한 row 총 개수.
        fix_candidates:       backfill 대상 (column 단위 카운트).
        already_correct:      이미 KST 가정 변환된 column 카운트.
        skipped_no_raw_text:  raw text 가 없어 검증 불가능한 column 카운트.
        skipped_null_column:  컬럼이 NULL 인 column 카운트 (raw text 유무 무관).
        parse_failures:       raw text 가 있으나 새 파서가 실패한 column 카운트.
    """

    total_rows: int = 0
    fix_candidates: int = 0
    already_correct: int = 0
    skipped_no_raw_text: int = 0
    skipped_null_column: int = 0
    parse_failures: int = 0


def _extract_raw_text(announcement: Announcement, key: str) -> str | None:
    """``raw_metadata.list_row[key]`` 텍스트를 안전하게 꺼낸다.

    저장 형식은 ``_build_announcement_payload`` 가 만든 ``{\"list_row\": {...}}``
    구조다 (``app/cli.py:223``). raw_metadata 가 None 이거나 키가 없으면 None.

    Args:
        announcement: 대상 ORM row.
        key:          ``\"received_at_text\"`` / ``\"deadline_at_text\"``.

    Returns:
        텍스트 (str) 또는 None.
    """
    meta = announcement.raw_metadata or {}
    list_row = meta.get("list_row") if isinstance(meta, dict) else None
    if not isinstance(list_row, dict):
        return None
    value = list_row.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _scan_for_candidates(session: Session) -> tuple[list[FixCandidate], ScanStats]:
    """모든 announcements row 를 순회해 backfill 대상 후보를 수집한다.

    각 row · 컬럼별로:
        1. 현재 컬럼 값을 ``as_utc`` 로 UTC tz-aware 로 정규화 (SQLite SELECT
           tz 손실 보정).
        2. raw_metadata 의 동일 키에서 텍스트를 꺼내 새 ``_parse_datetime_text``
           로 기대값 계산.
        3. before/after 가 다르면 FixCandidate 추가.

    raw text 누락 / 파싱 실패 / 컬럼 NULL 은 모두 skip + 통계 누적.
    """
    candidates: list[FixCandidate] = []
    stats = ScanStats()

    for announcement in session.execute(select(Announcement)).scalars():
        stats.total_rows += 1
        for column_name, raw_key in _TARGET_COLUMNS:
            current_value = getattr(announcement, column_name)
            raw_text = _extract_raw_text(announcement, raw_key)

            if current_value is None:
                stats.skipped_null_column += 1
                continue

            if raw_text is None:
                stats.skipped_no_raw_text += 1
                logger.debug(
                    "raw text 없음 — skip: id={} column={}",
                    announcement.id, column_name,
                )
                continue

            expected = _parse_datetime_text(raw_text)
            if expected is None:
                stats.parse_failures += 1
                logger.warning(
                    "raw text 파싱 실패 — skip: id={} column={} raw={!r}",
                    announcement.id, column_name, raw_text,
                )
                continue

            # SQLite 가 tz 를 떨어뜨려 naive 로 돌려준 값을 UTC tz-aware 로 정렬
            # (app.db.models.as_utc 의 결합 사용 패턴).
            before_aware = as_utc(current_value)
            if before_aware == expected:
                stats.already_correct += 1
                continue

            stats.fix_candidates += 1
            candidates.append(
                FixCandidate(
                    announcement_id=announcement.id,
                    column_name=column_name,
                    raw_text=raw_text,
                    before_value=before_aware,
                    after_value=expected,
                )
            )

    return candidates, stats


def _apply_candidates(
    session: Session,
    candidates: list[FixCandidate],
    *,
    batch_size: int,
) -> None:
    """후보들을 실제 UPDATE 한다 (--apply 모드 전용).

    동일 announcement_id 의 두 컬럼이 함께 후보일 수 있으므로, id → row 캐시
    를 두어 한 row 당 한 번만 SELECT 한다. ``batch_size`` 마다 중간 commit.
    """
    row_cache: dict[int, Announcement] = {}

    def _get_row(announcement_id: int) -> Announcement | None:
        """캐시에서 row 를 가져오거나 SELECT 후 캐시에 적재."""
        if announcement_id in row_cache:
            return row_cache[announcement_id]
        row = session.execute(
            select(Announcement).where(Announcement.id == announcement_id)
        ).scalar_one_or_none()
        if row is not None:
            row_cache[announcement_id] = row
        return row

    applied = 0
    for candidate in candidates:
        row = _get_row(candidate.announcement_id)
        if row is None:
            logger.warning(
                "row 가 사라졌음 (스캔 후 삭제?) — skip: id={}",
                candidate.announcement_id,
            )
            continue
        setattr(row, candidate.column_name, candidate.after_value)
        applied += 1

        if applied % batch_size == 0:
            session.commit()
            logger.info("중간 commit 완료: {} / {}", applied, len(candidates))

    if applied:
        session.commit()
    logger.info("총 {} 건 UPDATE 적용 완료", applied)


def _format_candidate_line(candidate: FixCandidate) -> str:
    """dry-run 출력의 한 줄 표현. id, 컬럼, before/after, raw text 포함.

    UTC 표기로 통일해 사용자 원문 raw_metadata 텍스트와 9시간 차이를 시각
    적으로 확인할 수 있도록 한다 (KST 표시는 표시 경계에서만 사용).
    """
    return (
        f"id={candidate.announcement_id} "
        f"column={candidate.column_name} "
        f"raw={candidate.raw_text!r} "
        f"before={candidate.before_value.isoformat()} "
        f"after={candidate.after_value.isoformat()}"
    )


def _print_stats(stats: ScanStats, candidates: list[FixCandidate]) -> None:
    """스캔 통계와 (dry-run 시) 후보 목록을 보기 좋게 출력한다."""
    logger.info("=" * 60)
    logger.info("스캔 통계")
    logger.info("=" * 60)
    logger.info("총 row 개수:                 {}", stats.total_rows)
    logger.info(
        "검사한 column 개수:           {}",
        stats.total_rows * len(_TARGET_COLUMNS),
    )
    logger.info("backfill 대상 (column 단위): {}", stats.fix_candidates)
    logger.info("이미 정상 (KST 가정 변환됨): {}", stats.already_correct)
    logger.info("컬럼 NULL — skip:            {}", stats.skipped_null_column)
    logger.info("raw text 없음 — skip:        {}", stats.skipped_no_raw_text)
    logger.info("raw text 파싱 실패:          {}", stats.parse_failures)
    logger.info("=" * 60)
    if candidates:
        logger.info("backfill 후보 (column 단위):")
        for candidate in candidates:
            logger.info("  - {}", _format_candidate_line(candidate))


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점. 인자 파싱 → 스캔 → (선택적으로) 적용.

    Returns:
        프로세스 exit code. 정상이면 0, 미처리 예외나 인자 오류는 비-0.
    """
    parser = argparse.ArgumentParser(
        description=(
            "외부 응답 텍스트의 KST 가정 미적용 row 를 재계산해 보정하는 일회성 "
            "backfill. 실행 전에 반드시 scripts/backup_db.py 로 DB 백업을 권장."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제로 DB 에 UPDATE 를 적용한다. 생략 시 dry-run (변경 없음).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="중간 commit 간격 (기본 100).",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help=(
            "접속할 SQLAlchemy DB URL. 생략 시 app.config.get_settings().db_url "
            "(.env 또는 기본 SQLite 파일)."
        ),
    )
    args = parser.parse_args(argv)

    configure_logging()

    if args.db_url:
        engine_url = args.db_url
    else:
        from app.config import get_settings

        engine_url = get_settings().db_url

    logger.info(
        "backfill_kst_assumption 시작: dry_run={} db_url={!r} batch_size={}",
        not args.apply, engine_url, args.batch_size,
    )

    engine = create_engine(engine_url)
    try:
        with Session(engine) as session:
            candidates, stats = _scan_for_candidates(session)
            _print_stats(stats, candidates)

            if not candidates:
                logger.info("backfill 대상 없음 — 종료.")
                return 0

            if not args.apply:
                logger.info(
                    "dry-run 모드 — DB 에 변경하지 않음. --apply 를 붙여 다시 실행."
                )
                return 0

            logger.info("=" * 60)
            logger.info("--apply 모드 — UPDATE 시작")
            logger.info("=" * 60)
            _apply_candidates(session, candidates, batch_size=args.batch_size)
    finally:
        engine.dispose()

    logger.info("backfill_kst_assumption 정상 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
