"""task 00151: 새 canonical 매칭 로직으로 기존 DB 의 canonical_group_id 를 재계산하는 일회성 스크립트.

본 스크립트는 ``app/canonical.py`` 의 새 ``_normalize_official_title`` (NTIS suffix 보존)
과 ``app/db/repository.py::_apply_canonical`` 의 cross-source fallback 매칭 분기를
기존에 이미 적재된 ``announcements`` / ``canonical_projects`` 데이터에 일관되게 적용한다.

목적
----
00151-1 의 새 로직은 신규 수집부터만 적용되므로, 이미 잘못 묶인 announcements.id=173, 174
(공통 canonical_group_id=152) 을 분리하지 못한다. 본 스크립트는 운영 DB 의 모든
is_current=True row 에 대해 새 키를 재계산하고 동일한 매칭 로직(exact → cross-source
fallback → 신규 생성) 으로 canonical_group_id 를 다시 부여한다.

동작 흐름
---------
1. is_current=True row 전부에 대해 새 canonical_key / canonical_key_scheme 을 계산해 메모리에 저장.
2. 모든 is_current=True row 의 canonical_group_id 를 NULL 로 리셋하고 canonical_key / scheme 을
   1 단계에서 계산한 새 값으로 덮어쓴다. 이 단계가 끝난 뒤에는 ``Announcement.canonical_key``
   LIKE 쿼리(cross-source fallback 이 사용) 가 모든 row 에 대해 새 prefix 를 본다.
2. id 오름차순으로 각 row 를 처리해 canonical_group_id 를 재할당한다.
       a. canonical_projects.canonical_key 정확일치 매칭
       b. official scheme 이면 ``_find_cross_source_canonical_group`` (동일 ancmNo prefix
          + 단일 cross-source 후보 + NTIS suffix 절단 title 동치) 매칭
       c. 매칭 실패 시 신규 CanonicalProject 생성
3. is_current=False(이력) row 에 매칭되는 (source_type, source_announcement_id) 의
   현재 row 에서 canonical_group_id / canonical_key / canonical_key_scheme 을 전파한다.
4. 어떤 announcement 도 참조하지 않게 된 canonical_projects(고아 row) 를 삭제한다.

멱등성
------
두 번째 이상 실행 시 결과 상태(canonical_group_id 매핑, canonical_projects 의 key 집합)
가 변하지 않는다. 단계 2 에서 기존 row 의 canonical_group_id 를 일단 NULL 로 리셋하지만,
3 단계 매칭이 동일한 cp.id 로 다시 매칭시키므로 최종 매핑은 같다.

사용법
------
::

    # 호스트 직접 실행 (가상환경 활성 상태)
    python scripts/python/recompute_canonical_00151.py --dry-run
    python scripts/python/recompute_canonical_00151.py

    # 도커 컨테이너 안에서 실행 (운영 표준)
    docker compose run --rm app python scripts/python/recompute_canonical_00151.py --dry-run
    docker compose run --rm app python scripts/python/recompute_canonical_00151.py

옵션
----
``--dry-run``       : DB 에 commit 하지 않고 세션 안에서 변경을 시뮬레이션 후 rollback 한다.
``--db-url URL``    : SQLAlchemy DB URL. 생략 시 ``app.config.get_settings().db_url`` 사용.

주의
----
- 사용자가 사전에 DB 백업을 완료한 상태를 전제로 한다.
- 변경은 단일 트랜잭션으로 commit 한다. 중간 실패 시 rollback 으로 일관성을 유지한다.
- canonical_overrides 같은 외부 수동 매핑 테이블이 있다면 본 스크립트가 침범하지 않는다
  (현 schema 에는 canonical_overrides 가 ORM 으로 매핑되어 있지 않음).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가 — 본 파일은 scripts/python/ 아래에 위치하므로
# 루트까지 부모 3단계(파일 → scripts/python → scripts → 프로젝트 루트) 를 거슬러 올라간다.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.canonical import compute_canonical_key  # noqa: E402
from app.db.migration import run_migrations  # noqa: E402
from app.db.models import Announcement, Base, CanonicalProject  # noqa: E402
from app.db.repository import _find_cross_source_canonical_group  # noqa: E402


@dataclass
class RowSnapshot:
    """is_current=True row 한 건의 사전 스냅샷.

    Attributes:
        announcement:    재계산 대상 Announcement (세션 attached 상태).
        old_group_id:    재계산 직전의 canonical_group_id (None 일 수 있음).
        old_key:         재계산 직전의 canonical_key (None 일 수 있음).
        new_key:         새 로직으로 계산한 canonical_key.
        new_scheme:      새 로직으로 계산한 canonical_key_scheme ('official' | 'fuzzy').
    """

    announcement: Announcement
    old_group_id: int | None
    old_key: str | None
    new_key: str
    new_scheme: str


@dataclass
class RecomputeStats:
    """재계산 결과 집계.

    Attributes:
        current_total:        처리한 is_current=True row 수.
        history_total:        처리한 is_current=False row 수.
        cp_before:            처리 전 canonical_projects 수.
        cp_after:             처리 후 canonical_projects 수 (orphan 삭제 반영).
        cp_created:           새로 생성된 canonical_projects 수.
        cp_deleted_orphan:    삭제된 orphan canonical_projects 수.
        group_id_changed:     canonical_group_id 가 실제로 바뀐 row 수.
        key_changed:          canonical_key 가 실제로 바뀐 row 수.
    """

    current_total: int = 0
    history_total: int = 0
    cp_before: int = 0
    cp_after: int = 0
    cp_created: int = 0
    cp_deleted_orphan: int = 0
    group_id_changed: int = 0
    key_changed: int = 0
    changed_rows: list[tuple[int, int | None, int | None]] = field(default_factory=list)
    created_cp_ids: list[int] = field(default_factory=list)
    deleted_cp_ids: list[int] = field(default_factory=list)


def _extract_ancm_no(announcement: Announcement) -> str | None:
    """Announcement 에서 공식 공고번호(ancmNo) 를 복원한다.

    소스 우선순위 (production 의 두 ancm_no 공급 경로를 모두 커버):
      1. ``raw_metadata.list_row.ancm_no`` — IRIS 목록 어댑터가 채워두는 원본 ancmNo.
         (``app/cli.py`` 가 payload['ancm_no'] 로 패스스루하던 값과 동일.)
      2. ``announcement.canonical_key`` 의 official prefix — NTIS 처럼 목록에서
         ancm_no 가 없고 상세 단계의 ``recompute_canonical_with_ancm_no`` 호출로
         scheme 이 official 로 승급된 row 에 사용. 이 경로의 prefix 는 이미
         ``_normalize_official_key`` 결과이지만, 해당 정규화는 NFKC + 공백 제거에 대해
         idempotent 하므로 그대로 candidate 로 다시 넘겨도 동일한 키 prefix 가 산출된다.
      3. (legacy) raw_metadata 최상단의 ``ancm_no`` / ``ancmNo`` 키.
      4. 무엇도 못 찾으면 None — fuzzy fallback 으로 떨어진다.

    Args:
        announcement: 대상 Announcement 인스턴스.

    Returns:
        공백 제거된 공고번호 문자열. 없으면 None.
    """
    meta: dict = announcement.raw_metadata or {}

    # 1. raw_metadata.list_row.ancm_no — IRIS 목록 적재 산출물.
    list_row = meta.get("list_row") if isinstance(meta, dict) else None
    if isinstance(list_row, dict):
        raw = list_row.get("ancm_no")
        if raw:
            return str(raw).strip()

    # 2. 기존 canonical_key prefix 에서 복원 — NTIS 등 detail 승급 경로.
    canonical_key = announcement.canonical_key or ""
    canonical_scheme = announcement.canonical_key_scheme or ""
    if canonical_scheme == "official" and canonical_key.startswith("official:"):
        after_prefix = canonical_key[len("official:") :]
        separator_idx = after_prefix.find("::")
        if separator_idx > 0:
            return after_prefix[:separator_idx]

    # 3. legacy 최상단 키.
    legacy = meta.get("ancm_no") or meta.get("ancmNo")
    if legacy:
        return str(legacy).strip()

    return None


def _build_snapshots(session: Session) -> list[RowSnapshot]:
    """is_current=True row 전부에 대해 새 canonical_key 를 계산해 스냅샷을 만든다.

    이 단계는 DB 쓰기를 수행하지 않는다. 매칭 단계가 일관된 LIKE prefix 비교를
    수행할 수 있도록, 호출자는 본 함수가 반환한 스냅샷의 new_key 를 이후 단계에서
    각 row 에 일괄 기록한다.

    Args:
        session: 호출자 세션.

    Returns:
        id 오름차순으로 정렬된 RowSnapshot 리스트.
    """
    rows: list[Announcement] = list(
        session.execute(
            select(Announcement)
            .where(Announcement.is_current.is_(True))
            .order_by(Announcement.id.asc())
        ).scalars()
    )
    snapshots: list[RowSnapshot] = []
    for ann in rows:
        ancm_no = _extract_ancm_no(ann)
        result = compute_canonical_key(
            official_key_candidates=[ancm_no] if ancm_no else [],
            title=ann.title or "",
            agency=ann.agency,
            deadline_at=ann.deadline_at,
        )
        snapshots.append(
            RowSnapshot(
                announcement=ann,
                old_group_id=ann.canonical_group_id,
                old_key=ann.canonical_key,
                new_key=result.canonical_key,
                new_scheme=result.canonical_scheme,
            )
        )
    return snapshots


def _reset_current_canonical_state(session: Session, snapshots: list[RowSnapshot]) -> None:
    """모든 is_current=True row 의 canonical_group_id 를 NULL 로 리셋하고 키를 새 값으로 덮어쓴다.

    이후 매칭 단계가 ``Announcement.canonical_key LIKE '<ancm_no_prefix>%'`` 쿼리를
    실행할 때 모든 후보 row 의 canonical_key 가 동일한 새 prefix 를 노출하도록 보장한다.

    Args:
        session:   호출자 세션.
        snapshots: _build_snapshots 결과.
    """
    for snap in snapshots:
        snap.announcement.canonical_group_id = None
        snap.announcement.canonical_key = snap.new_key
        snap.announcement.canonical_key_scheme = snap.new_scheme
    session.flush()
    logger.info("canonical_group_id NULL 리셋 + canonical_key/scheme 새 값 적용: {} 건", len(snapshots))


def _reassign_groups(
    session: Session,
    snapshots: list[RowSnapshot],
    stats: RecomputeStats,
) -> None:
    """id 오름차순으로 각 row 에 canonical_group_id 를 재할당한다.

    매칭 로직은 ``app/db/repository.py::_apply_canonical`` 과 동일한 우선순위로 동작한다:

      1. ``canonical_projects.canonical_key`` 정확일치.
      2. official scheme 한정 ``_find_cross_source_canonical_group`` 매칭
         (동일 ancmNo prefix + 단일 cross-source 후보 + NTIS suffix 절단 title 동치).
      3. 매칭 실패 시 신규 ``CanonicalProject`` 생성.

    snapshots 가 id 오름차순으로 정렬되어 있으므로, IRIS row 가 NTIS partner 보다 먼저
    배치된 케이스(현행 데이터의 일반 패턴) 에서 NTIS row 처리 시점에 IRIS partner 의
    canonical_group_id 가 이미 채워져 있어 cross-source fallback 이 자연스럽게 동작한다.

    Args:
        session:   호출자 세션.
        snapshots: _build_snapshots 결과 (id 오름차순).
        stats:     누적 통계.
    """
    for snap in snapshots:
        ann = snap.announcement
        match_source = "new"

        # 1. canonical_projects 의 정확일치 매칭.
        cp = session.execute(
            select(CanonicalProject).where(CanonicalProject.canonical_key == snap.new_key)
        ).scalar_one_or_none()
        if cp is not None:
            match_source = "exact"

        # 2. cross-source fallback — official scheme 에서만 적용.
        if cp is None and snap.new_scheme == "official":
            partner_cp = _find_cross_source_canonical_group(
                session,
                current_announcement=ann,
                current_canonical_key=snap.new_key,
                current_title=ann.title or "",
            )
            if partner_cp is not None:
                cp = partner_cp
                match_source = "cross_source_fallback"

        # 3. 신규 CanonicalProject 생성.
        if cp is None:
            cp = CanonicalProject(
                canonical_key=snap.new_key,
                key_scheme=snap.new_scheme,
                representative_title=ann.title,
                representative_agency=ann.agency,
            )
            session.add(cp)
            session.flush()
            stats.cp_created += 1
            stats.created_cp_ids.append(cp.id)
            logger.debug(
                "canonical 신규 생성: ann.id={} key={} scheme={} group_id={}",
                ann.id,
                snap.new_key,
                snap.new_scheme,
                cp.id,
            )
        else:
            logger.debug(
                "canonical 매칭({}): ann.id={} key={} group_id={}",
                match_source,
                ann.id,
                snap.new_key,
                cp.id,
            )

        ann.canonical_group_id = cp.id
        session.flush()

        # 통계 — 키/그룹 변경 여부.
        if snap.old_group_id != cp.id:
            stats.group_id_changed += 1
            stats.changed_rows.append((ann.id, snap.old_group_id, cp.id))
        if snap.old_key != snap.new_key:
            stats.key_changed += 1


def _propagate_to_history(session: Session, stats: RecomputeStats) -> None:
    """is_current=False(이력) row 에 매칭되는 현재 row 의 canonical 필드를 전파한다.

    동일 (source_type, source_announcement_id) 의 is_current=True row 가 있으면 그
    row 의 canonical_group_id / canonical_key / canonical_key_scheme 을 그대로 복사한다.
    현재 row 가 없는 이력 row(드물지만 가능) 는 그대로 둔다.

    Args:
        session: 호출자 세션.
        stats:   누적 통계.
    """
    history_rows: list[Announcement] = list(
        session.execute(
            select(Announcement).where(Announcement.is_current.is_(False))
        ).scalars()
    )
    stats.history_total = len(history_rows)

    for ann in history_rows:
        current_row = session.execute(
            select(Announcement).where(
                Announcement.source_type == ann.source_type,
                Announcement.source_announcement_id == ann.source_announcement_id,
                Announcement.is_current.is_(True),
            )
        ).scalar_one_or_none()

        if current_row is not None and current_row.canonical_group_id is not None:
            ann.canonical_group_id = current_row.canonical_group_id
            ann.canonical_key = current_row.canonical_key
            ann.canonical_key_scheme = current_row.canonical_key_scheme

    session.flush()
    logger.info("is_current=False 이력 row 처리: {} 건", stats.history_total)


def _delete_orphan_canonical_projects(session: Session, stats: RecomputeStats) -> None:
    """어떤 announcement 도 참조하지 않는 canonical_projects 를 삭제한다.

    LEFT JOIN 으로 announcement 가 0 건인 cp 만 골라 삭제한다. ON DELETE 동작은
    ``ON DELETE SET NULL`` 이므로 본 단계 이전에 모든 announcement 가 새 cp 로
    재할당돼 있어야 한다 (`_reassign_groups` 완료 후 호출 전제).

    Args:
        session: 호출자 세션.
        stats:   누적 통계.
    """
    # cp.id 별 announcement 참조 수를 셋다.
    referenced_subq = (
        select(Announcement.canonical_group_id)
        .where(Announcement.canonical_group_id.is_not(None))
        .distinct()
    )
    orphan_cps: list[CanonicalProject] = list(
        session.execute(
            select(CanonicalProject).where(CanonicalProject.id.not_in(referenced_subq))
        ).scalars()
    )
    for cp in orphan_cps:
        stats.deleted_cp_ids.append(cp.id)
        session.delete(cp)
    stats.cp_deleted_orphan = len(orphan_cps)
    session.flush()
    if orphan_cps:
        logger.info("고아 canonical_projects 삭제: {} 건 (ids={})", stats.cp_deleted_orphan, stats.deleted_cp_ids)
    else:
        logger.info("고아 canonical_projects 없음.")


def _log_post_validation(session: Session, snapshots: list[RowSnapshot]) -> None:
    """173/174 분리, 동일 source 중복 그룹 부재, 그룹 17/121 멤버십 유지를 검증해 출력한다.

    스크립트 종료 직전 호출. acceptance_criteria 핵심 항목을 자동 검증한다.

    Args:
        session:   호출자 세션 (commit 직전 또는 직후).
        snapshots: 사전 스냅샷 (id 오름차순).
    """
    # 173/174 분리 검증.
    ann_173 = session.get(Announcement, 173)
    ann_174 = session.get(Announcement, 174)
    if ann_173 is not None and ann_174 is not None:
        logger.info(
            "[검증] ann.id=173 canonical_group_id={} / ann.id=174 canonical_group_id={} → 분리={}",
            ann_173.canonical_group_id,
            ann_174.canonical_group_id,
            ann_173.canonical_group_id != ann_174.canonical_group_id,
        )

    # 동일 source_type 의 다중 멤버 그룹(NTIS) 검증.
    multi_member_same_source = list(
        session.execute(
            select(
                Announcement.canonical_group_id,
                Announcement.source_type,
                func.count(Announcement.id).label("cnt"),
            )
            .where(Announcement.is_current.is_(True), Announcement.canonical_group_id.is_not(None))
            .group_by(Announcement.canonical_group_id, Announcement.source_type)
            .having(func.count(Announcement.id) > 1)
        ).all()
    )
    if multi_member_same_source:
        logger.warning(
            "[검증] 동일 source_type 다중 멤버 그룹 존재 (NTIS sub-business 분리 실패 가능): {}",
            multi_member_same_source,
        )
    else:
        logger.info("[검증] 동일 source_type 다중 멤버 그룹 없음 (NTIS sub-business 분리 OK).")

    # 그룹 17 / 121 cross-source 멤버십 유지 검증 (사고 이전 정상 묶음 보존).
    for known_pair_ids in ((17, 35), (133, 162)):
        a = session.get(Announcement, known_pair_ids[0])
        b = session.get(Announcement, known_pair_ids[1])
        if a is None or b is None:
            continue
        same_group = a.canonical_group_id == b.canonical_group_id and a.canonical_group_id is not None
        logger.info(
            "[검증] cross-source 쌍 ann.id={}({}) + ann.id={}({}) 같은 group? {} (group={}, {})",
            known_pair_ids[0],
            a.source_type,
            known_pair_ids[1],
            b.source_type,
            same_group,
            a.canonical_group_id,
            b.canonical_group_id,
        )


def run_recompute(db_url: str, dry_run: bool) -> RecomputeStats:
    """recompute 전체 흐름을 실행한다.

    Args:
        db_url:  SQLAlchemy 접속 문자열.
        dry_run: True 이면 세션 안에서 변경 후 rollback 한다.

    Returns:
        RecomputeStats — 처리 결과 집계.
    """
    logger.info("recompute_canonical_00151 시작: db_url={} dry_run={}", db_url, dry_run)

    connect_args: dict[str, object] = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(db_url, future=True, connect_args=connect_args)
    # 기존 DB 라면 migration 을 적용하고, 신규 DB 라면 스키마를 생성한다.
    run_migrations(engine)
    Base.metadata.create_all(engine)

    stats = RecomputeStats()

    with Session(engine) as session:
        try:
            # 사전 통계.
            stats.cp_before = int(
                session.execute(select(func.count(CanonicalProject.id))).scalar_one()
            )

            # 1. 새 키 계산 (DB 쓰기 없음).
            snapshots = _build_snapshots(session)
            stats.current_total = len(snapshots)
            logger.info("is_current=True 대상: {} 건 (cp_before={})", stats.current_total, stats.cp_before)

            # 2. canonical_group_id NULL 리셋 + canonical_key/scheme 새 값 적용.
            _reset_current_canonical_state(session, snapshots)

            # 3. group_id 재할당 (exact → cross-source fallback → 신규 생성).
            _reassign_groups(session, snapshots, stats)

            # 4. 이력 row 전파.
            _propagate_to_history(session, stats)

            # 5. 고아 canonical_projects 삭제.
            _delete_orphan_canonical_projects(session, stats)

            # 사후 통계.
            stats.cp_after = int(
                session.execute(select(func.count(CanonicalProject.id))).scalar_one()
            )

            # 검증 로그 (commit/rollback 직전 — session 상태 기준).
            _log_post_validation(session, snapshots)

            if dry_run:
                session.rollback()
                logger.info("dry-run 모드: 세션 rollback 완료. DB 영구 변경 없음.")
            else:
                session.commit()
                logger.info("commit 완료. DB 변경이 영구 반영되었습니다.")

        except Exception:
            session.rollback()
            logger.exception("recompute 중 오류 발생. 트랜잭션을 rollback 합니다.")
            raise

    logger.info("=== 처리 결과 요약 ===")
    logger.info("is_current=True row 처리: {} 건", stats.current_total)
    logger.info("is_current=False 이력 row 처리: {} 건", stats.history_total)
    logger.info("canonical_projects: {} → {} 건", stats.cp_before, stats.cp_after)
    logger.info("새로 생성된 canonical_projects: {} 건 (ids={})", stats.cp_created, stats.created_cp_ids)
    logger.info("삭제된 orphan canonical_projects: {} 건 (ids={})", stats.cp_deleted_orphan, stats.deleted_cp_ids)
    logger.info("canonical_group_id 가 변경된 row: {} 건", stats.group_id_changed)
    logger.info("canonical_key 가 변경된 row: {} 건", stats.key_changed)
    if stats.changed_rows:
        for ann_id, old_gid, new_gid in stats.changed_rows[:50]:
            logger.info("  ann.id={} : group_id {} → {}", ann_id, old_gid, new_gid)
        if len(stats.changed_rows) > 50:
            logger.info("  ... (총 {} 건 중 50건만 출력)", len(stats.changed_rows))

    return stats


def _parse_args() -> argparse.Namespace:
    """명령행 인수를 파싱한다."""
    parser = argparse.ArgumentParser(
        description=(
            "task 00151: 새 canonical 매칭 로직으로 기존 DB 의 canonical_group_id 를 재계산한다. "
            "173/174 분리 + 기존 cross-source 쌍 유지가 동시에 보장된다."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # dry-run — 변경 예정 row 수만 확인 (DB 미변경)
  python scripts/python/recompute_canonical_00151.py --dry-run

  # 실제 운영 DB 적용
  python scripts/python/recompute_canonical_00151.py

  # 임시 DB 로 검증
  python scripts/python/recompute_canonical_00151.py --db-url sqlite:///./data/db/test.sqlite3
""",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="DB 에 commit 하지 않고 세션 안에서 변경을 시뮬레이션 후 rollback 한다.",
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

    run_recompute(db_url=db_url, dry_run=args.dry_run)
