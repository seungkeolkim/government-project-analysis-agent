"""canonical 묶음 false-positive 감사 스크립트.

프로덕션 DB 에 연결하여 canonical_projects 그룹의 품질을 점검한다.
DB 는 읽기 전용으로만 접근하며 어떤 row 도 변경하지 않는다.

실행:
    docker compose run --rm app python scripts/audit_canonical_false_positives.py
    docker compose run --rm app python scripts/audit_canonical_false_positives.py --top-n 20
    docker compose run --rm app python scripts/audit_canonical_false_positives.py --show-id 33 34

감사 내용
---------
1. 전체 통계     — canonical_projects 수, is_current 공고 수, 그룹별 분포
2. Fuzzy 그룹   — scheme='fuzzy' 이고 is_current 공고 2건 이상인 그룹 목록
3. False-positive 후보 — 같은 fuzzy 그룹 안에서 동일 source_type 의 서로 다른 공고가
                         2건 이상 존재하는 경우 (recompute 미완료 등 정상 케이스도 있으나
                         실제 다른 과제가 잘못 묶인 경우도 포함될 수 있음)
4. Official 다중 그룹 — scheme='official' 이고 is_current 공고 2건 이상인 그룹 목록
                        (동일 공고번호로 묶인 과제. N:1 구조이므로 정상 가능)
5. 개별 공고 조회    — --show-id 로 특정 announcement ID 의 canonical 정보 출력
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.models import Announcement, CanonicalProject  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

# ── 임계값 상수 (초기 튜닝값 — Phase 5 에서 조정) ────────────────────────────

# fuzzy 그룹 내 같은 source_type 공고가 이 수 이상이면 false-positive 후보로 마킹
SAME_SOURCE_TYPE_THRESHOLD: int = 2  # 초기 튜닝값

# false-positive 후보 상위 출력 개수 기본값
DEFAULT_TOP_N: int = 10  # 초기 튜닝값

# official 다중 그룹 출력 개수 기본값
DEFAULT_OFFICIAL_MULTI_N: int = 10  # 초기 튜닝값


# ── 데이터 클래스 ────────────────────────────────────────────────────────────


class AnnouncementRow:
    """감사용 공고 행 요약."""

    __slots__ = (
        "ann_id", "source_type", "source_announcement_id",
        "title", "agency", "status", "deadline_at",
        "canonical_group_id", "canonical_key_scheme", "canonical_key",
    )

    def __init__(self, ann: Announcement) -> None:
        """Announcement ORM 객체에서 필요한 필드만 추출한다."""
        self.ann_id = ann.id
        self.source_type = ann.source_type
        self.source_announcement_id = ann.source_announcement_id
        self.title = ann.title
        self.agency = ann.agency or ""
        self.status = ann.status
        self.deadline_at = ann.deadline_at
        self.canonical_group_id = ann.canonical_group_id
        self.canonical_key_scheme = ann.canonical_key_scheme or ""
        self.canonical_key = ann.canonical_key or ""


# ── 쿼리 헬퍼 ────────────────────────────────────────────────────────────────


def fetch_all_current_announcements(session: Session) -> list[AnnouncementRow]:
    """is_current=True 인 공고 전체를 읽어 AnnouncementRow 목록으로 반환한다.

    대용량 DB 에서는 메모리에 주의. 현재 로컬 팀 공용 규모에서는 문제없다.
    """
    stmt = (
        select(Announcement)
        .where(Announcement.is_current.is_(True))
        .order_by(Announcement.id)
    )
    rows = session.scalars(stmt).all()
    return [AnnouncementRow(r) for r in rows]


def fetch_canonical_project_count(session: Session) -> int:
    """canonical_projects 전체 수를 반환한다."""
    return session.scalar(select(func.count()).select_from(CanonicalProject)) or 0


def fetch_announcement_by_ids(
    session: Session, ann_ids: list[int]
) -> list[AnnouncementRow]:
    """특정 id 목록에 해당하는 announcement row 를 반환한다. is_current 무관."""
    if not ann_ids:
        return []
    stmt = select(Announcement).where(Announcement.id.in_(ann_ids)).order_by(Announcement.id)
    return [AnnouncementRow(r) for r in session.scalars(stmt).all()]


# ── 통계 분석 ────────────────────────────────────────────────────────────────


def build_group_map(
    rows: list[AnnouncementRow],
) -> dict[int, list[AnnouncementRow]]:
    """canonical_group_id → AnnouncementRow 목록 맵을 만든다.

    canonical_group_id 가 None 인 row 는 제외한다(아직 매칭 미완료 공고).
    """
    group_map: dict[int, list[AnnouncementRow]] = defaultdict(list)
    for row in rows:
        if row.canonical_group_id is not None:
            group_map[row.canonical_group_id].append(row)
    return dict(group_map)


def find_fuzzy_multi_groups(
    group_map: dict[int, list[AnnouncementRow]],
) -> list[tuple[int, list[AnnouncementRow]]]:
    """fuzzy scheme 이고 is_current 공고 2건 이상인 그룹을 내림차순으로 반환한다."""
    result = []
    for gid, members in group_map.items():
        if len(members) < 2:
            continue
        if any(m.canonical_key_scheme == "fuzzy" for m in members):
            result.append((gid, members))
    result.sort(key=lambda x: len(x[1]), reverse=True)
    return result


def find_official_multi_groups(
    group_map: dict[int, list[AnnouncementRow]],
) -> list[tuple[int, list[AnnouncementRow]]]:
    """official scheme 이고 is_current 공고 2건 이상인 그룹을 내림차순으로 반환한다."""
    result = []
    for gid, members in group_map.items():
        if len(members) < 2:
            continue
        if all(m.canonical_key_scheme == "official" for m in members):
            result.append((gid, members))
    result.sort(key=lambda x: len(x[1]), reverse=True)
    return result


def classify_false_positive_candidates(
    fuzzy_multi: list[tuple[int, list[AnnouncementRow]]],
) -> list[tuple[int, list[AnnouncementRow], str]]:
    """fuzzy 다중 그룹 중 false-positive 후보를 분류한다.

    기준: 같은 source_type 의 서로 다른 공고가 SAME_SOURCE_TYPE_THRESHOLD 이상.
    같은 source_type 의 공고가 여러 개라면 실제 다른 과제가 묶였을 가능성이 높다.
    (다른 source_type 조합 — 예: IRIS+NTIS — 은 cross-source 정상 매칭 가능)

    반환: (group_id, members, reason) 목록.
    """
    candidates = []
    for gid, members in fuzzy_multi:
        # source_type 별 카운트
        source_counts: dict[str, int] = defaultdict(int)
        for m in members:
            source_counts[m.source_type] += 1

        reasons = []
        for src, cnt in source_counts.items():
            if cnt >= SAME_SOURCE_TYPE_THRESHOLD:
                reasons.append(
                    f"동일 source_type={src!r} 공고 {cnt}건 (기준 {SAME_SOURCE_TYPE_THRESHOLD}건)"
                )

        if reasons:
            candidates.append((gid, members, " | ".join(reasons)))

    return candidates


# ── 출력 헬퍼 ────────────────────────────────────────────────────────────────


def _fmt_deadline(row: AnnouncementRow) -> str:
    """마감일을 짧은 문자열로 반환한다."""
    if row.deadline_at is None:
        return "없음"
    return row.deadline_at.strftime("%Y-%m-%d")


def print_group_members(members: list[AnnouncementRow], indent: str = "  ") -> None:
    """그룹 멤버 목록을 사람이 읽기 좋게 출력한다."""
    for m in members:
        title_trunc = (m.title[:60] + "…") if len(m.title) > 60 else m.title
        logger.info(
            "{}id={:<5} src={}/{:<10} status={} deadline={} title={}",
            indent,
            m.ann_id,
            m.source_type,
            m.source_announcement_id,
            m.status,
            _fmt_deadline(m),
            title_trunc,
        )
        logger.info(
            "{}       agency={!r}  scheme={}",
            indent,
            m.agency,
            m.canonical_key_scheme,
        )
        logger.info(
            "{}       canonical_key={}",
            indent,
            m.canonical_key,
        )


# ── 섹션 출력 ────────────────────────────────────────────────────────────────


def print_section_header(title: str) -> None:
    """구분선과 섹션 제목을 출력한다."""
    logger.info("")
    logger.info("=" * 70)
    logger.info("{}", title)
    logger.info("=" * 70)


def print_stats(
    session: Session,
    rows: list[AnnouncementRow],
    group_map: dict[int, list[AnnouncementRow]],
) -> None:
    """전체 통계를 출력한다."""
    print_section_header("§1 전체 통계")

    cp_count = fetch_canonical_project_count(session)
    total_current = len(rows)
    orphan_count = sum(1 for r in rows if r.canonical_group_id is None)
    official_count = sum(
        1 for r in rows if r.canonical_key_scheme == "official" and r.canonical_group_id is not None
    )
    fuzzy_count = sum(
        1 for r in rows if r.canonical_key_scheme == "fuzzy" and r.canonical_group_id is not None
    )

    single_groups = sum(1 for members in group_map.values() if len(members) == 1)
    multi_groups = sum(1 for members in group_map.values() if len(members) >= 2)
    max_group_size = max((len(m) for m in group_map.values()), default=0)

    logger.info("canonical_projects 테이블 rows: {}", cp_count)
    logger.info("is_current 공고 총계: {}", total_current)
    logger.info("  canonical 미할당(orphan): {}", orphan_count)
    logger.info("  official scheme: {}", official_count)
    logger.info("  fuzzy   scheme:  {}", fuzzy_count)
    logger.info("그룹 분포 (canonical_group_id 기준)")
    logger.info("  단독(1건) 그룹: {}", single_groups)
    logger.info("  다중(2건+) 그룹: {}", multi_groups)
    logger.info("  최대 그룹 크기: {}", max_group_size)

    # 그룹 크기별 분포
    size_dist: dict[int, int] = defaultdict(int)
    for members in group_map.values():
        size_dist[len(members)] += 1
    for size in sorted(size_dist):
        logger.info("  size={}: {}그룹", size, size_dist[size])


def print_fuzzy_multi_groups(
    fuzzy_multi: list[tuple[int, list[AnnouncementRow]]],
    top_n: int,
) -> None:
    """fuzzy 다중 그룹 전체 목록을 출력한다."""
    print_section_header(
        f"§2 fuzzy scheme 다중 그룹 (is_current 2건 이상) — 상위 {top_n}개"
    )

    if not fuzzy_multi:
        logger.info("해당 그룹 없음.")
        return

    logger.info("총 {} 그룹 발견 (아래는 상위 {} 개)", len(fuzzy_multi), top_n)
    for gid, members in fuzzy_multi[:top_n]:
        canonical_key = members[0].canonical_key if members else ""
        logger.info("")
        logger.info(
            "▸ canonical_group_id={} | size={} | key={}",
            gid,
            len(members),
            canonical_key,
        )
        print_group_members(members)


def print_false_positive_candidates(
    candidates: list[tuple[int, list[AnnouncementRow], str]],
    top_n: int,
) -> None:
    """false-positive 후보 목록을 출력한다."""
    print_section_header(
        f"§3 false-positive 후보 (fuzzy + 동일 source_type {SAME_SOURCE_TYPE_THRESHOLD}건+) — 상위 {top_n}개"
    )

    if not candidates:
        logger.info("false-positive 후보 없음. 기준: {}", f"동일 source_type >= {SAME_SOURCE_TYPE_THRESHOLD}")
        return

    logger.info(
        "총 {} 그룹이 기준을 충족 (아래는 상위 {} 개)",
        len(candidates),
        top_n,
    )
    for gid, members, reason in candidates[:top_n]:
        logger.info("")
        logger.info(
            "▸ [후보] canonical_group_id={} | size={} | 이유: {}",
            gid,
            len(members),
            reason,
        )
        print_group_members(members)


def print_official_multi_groups(
    official_multi: list[tuple[int, list[AnnouncementRow]]],
    top_n: int,
) -> None:
    """official 다중 그룹을 출력한다. N:1 구조이므로 대부분 정상."""
    print_section_header(
        f"§4 official scheme 다중 그룹 (is_current 2건 이상, 참고용) — 상위 {top_n}개"
    )

    if not official_multi:
        logger.info("해당 그룹 없음.")
        return

    logger.info(
        "총 {} 그룹. official:ancmNo 는 N:1 구조이므로 대부분 정상. 참고용 출력.",
        len(official_multi),
    )
    for gid, members in official_multi[:top_n]:
        canonical_key = members[0].canonical_key if members else ""
        logger.info("")
        logger.info(
            "▸ canonical_group_id={} | size={} | key={}",
            gid,
            len(members),
            canonical_key,
        )
        print_group_members(members)


def print_specific_announcements(
    session: Session,
    ann_ids: list[int],
) -> None:
    """특정 announcement id 의 canonical 정보를 출력한다."""
    print_section_header(f"§5 특정 공고 조회 (ids={ann_ids})")

    rows = fetch_announcement_by_ids(session, ann_ids)
    if not rows:
        logger.info("해당 id 의 공고를 찾을 수 없음.")
        return

    # 같은 그룹끼리 묶어서 출력
    found_ids = {r.ann_id for r in rows}
    missing = set(ann_ids) - found_ids
    if missing:
        logger.info("조회 실패한 id: {}", sorted(missing))

    group_buckets: dict[int | None, list[AnnouncementRow]] = defaultdict(list)
    for r in rows:
        group_buckets[r.canonical_group_id].append(r)

    for gid, members in group_buckets.items():
        logger.info("")
        logger.info("canonical_group_id={}", gid)
        print_group_members(members)

        # 같은 그룹의 다른 공고들도 보여준다
        if gid is not None:
            stmt = (
                select(Announcement)
                .where(
                    Announcement.canonical_group_id == gid,
                    Announcement.is_current.is_(True),
                    Announcement.id.not_in([m.ann_id for m in members]),
                )
                .order_by(Announcement.id)
            )
            siblings = [AnnouncementRow(r) for r in session.scalars(stmt).all()]
            if siblings:
                logger.info("  ↳ 같은 그룹의 다른 is_current 공고:")
                print_group_members(siblings, indent="    ")


def print_todo_phase5(
    candidates: list[tuple[int, list[AnnouncementRow], str]],
    fuzzy_multi: list[tuple[int, list[AnnouncementRow]]],
) -> None:
    """Phase 5 TODO 항목을 출력한다."""
    print_section_header("§6 Phase 5 TODO (canonical_overrides 로 해결할 항목)")
    logger.info(
        "false-positive 후보 {} 그룹을 Phase 5 canonical_overrides 에서 처리 예정.",
        len(candidates),
    )
    if candidates:
        for gid, members, reason in candidates:
            logger.info("  TODO split: canonical_group_id={} | reason={}", gid, reason)
    logger.info("")
    logger.info("fuzzy 다중 그룹 전체 {} 건 중 false-positive 후보: {} 건", len(fuzzy_multi), len(candidates))
    logger.info(
        "나머지 {} 건은 정상 묶음(다른 source_type 간 cross-source 또는 재공고)으로 판단.",
        len(fuzzy_multi) - len(candidates),
    )
    logger.info("")
    logger.info("실행 필요 없음. 이 스크립트는 readonly 감사 전용.")


# ── 메인 ─────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    """명령행 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="canonical 묶음 false-positive 감사 (읽기 전용)"
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        metavar="N",
        help=f"각 섹션의 상위 N 건만 출력 (기본값: {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--show-id",
        type=int,
        nargs="+",
        default=[],
        metavar="ID",
        help="특정 announcement id 의 canonical 정보를 §5 에서 출력",
    )
    return parser.parse_args()


def main() -> int:
    """감사 스크립트 진입점. 항상 0 을 반환한다 (readonly, 오류 없으면 정상 종료)."""
    args = parse_args()

    logger.info("canonical 묶음 false-positive 감사 시작")
    logger.info("DB: SessionLocal (읽기 전용 접근)")

    session = SessionLocal()
    try:
        # 1. 전체 is_current 공고 로드
        rows = fetch_all_current_announcements(session)
        group_map = build_group_map(rows)

        # 2. 전체 통계
        print_stats(session, rows, group_map)

        # 3. fuzzy 다중 그룹
        fuzzy_multi = find_fuzzy_multi_groups(group_map)
        print_fuzzy_multi_groups(fuzzy_multi, args.top_n)

        # 4. false-positive 후보
        candidates = classify_false_positive_candidates(fuzzy_multi)
        print_false_positive_candidates(candidates, args.top_n)

        # 5. official 다중 그룹 (참고용)
        official_multi = find_official_multi_groups(group_map)
        print_official_multi_groups(official_multi, args.top_n)

        # 6. 특정 공고 조회 (--show-id)
        if args.show_id:
            print_specific_announcements(session, args.show_id)

        # 7. Phase 5 TODO
        print_todo_phase5(candidates, fuzzy_multi)

        logger.info("")
        logger.info("감사 완료. DB 변경 없음.")
        return 0

    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
