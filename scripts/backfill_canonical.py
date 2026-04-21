"""기존 IRIS 데이터 canonical 재계산 일회성 backfill 스크립트.

canonical_group_id 가 NULL 인 Announcement row 를 일괄 처리하여
compute_canonical_key 로 canonical_key 를 계산하고 CanonicalProject 를 조회/생성한다.

처리 순서:
    1. is_current=True row 부터 처리: canonical_key 계산 → CanonicalProject 조회/생성
    2. is_current=False row(이력): 같은 (source_type, source_announcement_id) 의 is_current=True row
       에서 canonical_group_id 를 전파. is_current=True row 가 없으면 독립적으로 계산.

사용법:
    python scripts/backfill_canonical.py [--dry-run] [--batch-size N] [--db-url URL]

옵션:
    --dry-run           DB 를 수정하지 않고 처리 예상 건수만 출력한다.
    --batch-size N      한 번에 commit 할 건수 (기본값: 200).
    --db-url URL        접속할 SQLAlchemy DB URL.
                        생략 시 app.config.get_settings().db_url 을 사용한다.

주의:
    이 스크립트는 운영 DB 에 직접 쓴다. 실행 전 반드시 --dry-run 으로 먼저 확인하라.
    NTIS 공고도 source_type='NTIS' 로 수집되어 있다면 함께 처리된다.
    실데이터 기반 재등록(ancmId 변경) 검증은 NTIS 구현 이후에 수행한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가 (scripts/ 에서 app 패키지를 임포트하기 위해)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.canonical import compute_canonical_key  # noqa: E402
from app.db.migration import run_migrations  # noqa: E402
from app.db.models import Announcement, Base, CanonicalProject  # noqa: E402


def _extract_ancm_no(announcement: Announcement) -> str | None:
    """Announcement.raw_metadata 에서 공식 공고번호(ancmNo)를 추출한다.

    IRIS 수집 데이터는 raw_metadata 에 원본 API 응답 필드를 저장한다.
    'ancmNo' 키 또는 'ancm_no' 키를 순서대로 탐색한다.

    Args:
        announcement: 대상 Announcement 인스턴스.

    Returns:
        공고번호 문자열. 없거나 공란이면 None.
    """
    meta: dict = announcement.raw_metadata or {}
    # 목록 API 원본 키 우선
    ancm_no = meta.get("ancmNo") or meta.get("ancm_no") or None
    return str(ancm_no).strip() if ancm_no else None


def _get_or_create_canonical_project(
    session: Session,
    *,
    canonical_key: str,
    canonical_scheme: str,
    representative_title: str | None,
    representative_agency: str | None,
    dry_run: bool,
) -> CanonicalProject | None:
    """canonical_key 로 CanonicalProject 를 조회하거나 신규 생성한다.

    dry_run=True 이면 DB 에 쓰지 않고 None 을 반환한다.

    Args:
        session:               현재 세션.
        canonical_key:         조회/생성할 canonical_key.
        canonical_scheme:      'official' 또는 'fuzzy'.
        representative_title:  대표 공고명.
        representative_agency: 대표 주관기관.
        dry_run:               True 이면 실제 쓰기 없이 None 반환.

    Returns:
        조회 또는 생성된 CanonicalProject. dry_run 이면 None.
    """
    if dry_run:
        return None

    cp = session.execute(
        select(CanonicalProject).where(CanonicalProject.canonical_key == canonical_key)
    ).scalar_one_or_none()

    if cp is None:
        cp = CanonicalProject(
            canonical_key=canonical_key,
            key_scheme=canonical_scheme,
            representative_title=representative_title,
            representative_agency=representative_agency,
        )
        session.add(cp)
        session.flush()
        logger.debug("CanonicalProject 생성: key={} scheme={} id={}", canonical_key, canonical_scheme, cp.id)
    else:
        logger.debug("CanonicalProject 기존 매칭: key={} id={}", canonical_key, cp.id)

    return cp


def _process_current_rows(
    session: Session,
    batch_size: int,
    dry_run: bool,
) -> tuple[int, int, int]:
    """is_current=True 이면서 canonical_group_id 가 NULL 인 row 를 처리한다.

    Args:
        session:    현재 세션.
        batch_size: 일정 간격으로 commit 할 건수.
        dry_run:    True 이면 DB 쓰기 없이 통계만 집계.

    Returns:
        (처리_건수, 성공_건수, 실패_건수) 튜플.
    """
    rows: list[Announcement] = list(
        session.execute(
            select(Announcement).where(
                Announcement.canonical_group_id.is_(None),
                Announcement.is_current.is_(True),
            )
        ).scalars()
    )

    total = len(rows)
    success_count = 0
    fail_count = 0

    logger.info("is_current=True 대상 {} 건 처리 시작 (dry_run={})", total, dry_run)

    for idx, ann in enumerate(rows, start=1):
        try:
            ancm_no = _extract_ancm_no(ann)
            result = compute_canonical_key(
                official_key_candidates=[ancm_no] if ancm_no else [],
                title=ann.title or "",
                agency=ann.agency,
                deadline_at=ann.deadline_at,
            )

            cp = _get_or_create_canonical_project(
                session,
                canonical_key=result.canonical_key,
                canonical_scheme=result.canonical_scheme,
                representative_title=ann.title,
                representative_agency=ann.agency,
                dry_run=dry_run,
            )

            if not dry_run and cp is not None:
                ann.canonical_group_id = cp.id
                ann.canonical_key = result.canonical_key
                ann.canonical_key_scheme = result.canonical_scheme

            success_count += 1

        except Exception as exc:
            fail_count += 1
            logger.warning("canonical 계산 실패 (ann.id={}): {}", ann.id, exc)
            continue

        # 일정 건수마다 commit 하여 메모리 부담과 잠금 시간을 줄인다.
        if not dry_run and idx % batch_size == 0:
            session.commit()
            logger.info("중간 commit 완료: {} / {} 건", idx, total)

    if not dry_run:
        session.commit()

    logger.info(
        "is_current=True 처리 완료: 성공={} 실패={} / 전체={}",
        success_count,
        fail_count,
        total,
    )
    return total, success_count, fail_count


def _propagate_to_history_rows(
    session: Session,
    batch_size: int,
    dry_run: bool,
) -> tuple[int, int, int]:
    """is_current=False(이력) row 에 canonical_group_id 를 전파한다.

    같은 (source_type, source_announcement_id) 의 is_current=True row 의
    canonical_group_id 를 이력 row 에 복사한다.
    is_current=True row 가 없거나 canonical_group_id 가 여전히 NULL 인 경우
    이력 row 에 대해 독립적으로 canonical_key 를 계산한다.

    Args:
        session:    현재 세션.
        batch_size: 중간 commit 간격.
        dry_run:    True 이면 DB 쓰기 없이 통계만.

    Returns:
        (처리_건수, 성공_건수, 실패_건수) 튜플.
    """
    history_rows: list[Announcement] = list(
        session.execute(
            select(Announcement).where(
                Announcement.canonical_group_id.is_(None),
                Announcement.is_current.is_(False),
            )
        ).scalars()
    )

    total = len(history_rows)
    success_count = 0
    fail_count = 0

    logger.info("is_current=False 이력 대상 {} 건 처리 시작 (dry_run={})", total, dry_run)

    for idx, ann in enumerate(history_rows, start=1):
        try:
            # 같은 소스 ID 의 현재 row 에서 canonical_group_id 를 상속 시도
            current_row = session.execute(
                select(Announcement).where(
                    Announcement.source_type == ann.source_type,
                    Announcement.source_announcement_id == ann.source_announcement_id,
                    Announcement.is_current.is_(True),
                )
            ).scalar_one_or_none()

            if current_row is not None and current_row.canonical_group_id is not None:
                # is_current=True row 의 canonical_group_id 를 승계
                if not dry_run:
                    ann.canonical_group_id = current_row.canonical_group_id
                    ann.canonical_key = current_row.canonical_key
                    ann.canonical_key_scheme = current_row.canonical_key_scheme
                logger.debug(
                    "이력 row canonical 승계: ann.id={} → group_id={}",
                    ann.id,
                    current_row.canonical_group_id,
                )
            else:
                # 현재 row 가 없거나 canonical 이 없으면 독립 계산
                ancm_no = _extract_ancm_no(ann)
                result = compute_canonical_key(
                    official_key_candidates=[ancm_no] if ancm_no else [],
                    title=ann.title or "",
                    agency=ann.agency,
                    deadline_at=ann.deadline_at,
                )
                cp = _get_or_create_canonical_project(
                    session,
                    canonical_key=result.canonical_key,
                    canonical_scheme=result.canonical_scheme,
                    representative_title=ann.title,
                    representative_agency=ann.agency,
                    dry_run=dry_run,
                )
                if not dry_run and cp is not None:
                    ann.canonical_group_id = cp.id
                    ann.canonical_key = result.canonical_key
                    ann.canonical_key_scheme = result.canonical_scheme

            success_count += 1

        except Exception as exc:
            fail_count += 1
            logger.warning("이력 row canonical 실패 (ann.id={}): {}", ann.id, exc)
            continue

        if not dry_run and idx % batch_size == 0:
            session.commit()
            logger.info("이력 중간 commit 완료: {} / {} 건", idx, total)

    if not dry_run:
        session.commit()

    logger.info(
        "is_current=False 이력 처리 완료: 성공={} 실패={} / 전체={}",
        success_count,
        fail_count,
        total,
    )
    return total, success_count, fail_count


def run_backfill(db_url: str, batch_size: int, dry_run: bool) -> None:
    """backfill 전체 흐름을 실행한다.

    1. is_current=True row 처리 (canonical_key 계산 + CanonicalProject 조회/생성)
    2. is_current=False row 처리 (is_current=True 에서 canonical_group_id 전파)

    Args:
        db_url:     SQLAlchemy 접속 문자열.
        batch_size: 중간 commit 간격 (건수).
        dry_run:    True 이면 실제 DB 쓰기 없이 통계만 출력.
    """
    logger.info("backfill 시작: db_url={} batch_size={} dry_run={}", db_url, batch_size, dry_run)

    connect_args: dict[str, object] = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(db_url, future=True, connect_args=connect_args)

    # 기존 DB 에 migration 이 적용되지 않았을 경우를 대비해 migration 을 먼저 실행한다.
    run_migrations(engine)
    # 신규 DB 라면 create_all 로 최신 스키마를 생성한다.
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        # 1단계: is_current=True row
        total_current, ok_current, fail_current = _process_current_rows(
            session, batch_size=batch_size, dry_run=dry_run
        )

        # 2단계: is_current=False row (이력)
        total_hist, ok_hist, fail_hist = _propagate_to_history_rows(
            session, batch_size=batch_size, dry_run=dry_run
        )

    # 최종 요약
    total = total_current + total_hist
    ok = ok_current + ok_hist
    fail = fail_current + fail_hist

    logger.info(
        "backfill 완료: 전체={} 성공={} 실패={} (dry_run={})",
        total,
        ok,
        fail,
        dry_run,
    )

    if dry_run:
        logger.info("dry-run 모드: DB 변경 없음. 위 통계는 예상치입니다.")


def _parse_args() -> argparse.Namespace:
    """명령행 인수를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="기존 Announcement 데이터에 canonical_group_id 를 일괄 채우는 일회성 스크립트.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # dry-run 으로 대상 건수 확인
  python scripts/backfill_canonical.py --dry-run

  # 실제 실행 (운영 DB)
  python scripts/backfill_canonical.py --batch-size 200

  # 임시 DB 로 검증
  python scripts/backfill_canonical.py --db-url sqlite:///./data/db/test_canonical.sqlite3 --batch-size 100
""",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="DB 를 수정하지 않고 예상 처리 건수만 출력한다.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        metavar="N",
        help="중간 commit 간격 (건수). 기본값: 200.",
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=None,
        metavar="URL",
        help="SQLAlchemy DB URL. 생략 시 app.config.get_settings().db_url 을 사용한다.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    db_url = args.db_url
    if db_url is None:
        from app.config import get_settings

        settings = get_settings()
        settings.ensure_runtime_paths()
        db_url = settings.db_url

    run_backfill(db_url=db_url, batch_size=args.batch_size, dry_run=args.dry_run)
