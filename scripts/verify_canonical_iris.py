"""IRIS canonical 매칭 동작 검증 스크립트 (IRIS 단독).

임시 SQLite DB(:memory:)에 fixture 를 삽입하여 canonical 매칭 동작을 재현 가능하게 검증한다.
프로덕션 DB 는 건드리지 않으며, 스크립트 종료 시 임시 DB 도 사라진다.

검증 시나리오
-----------
시나리오 A: 같은 ancmNo 재수집 (unchanged 분기)
    동일 (source_type, source_announcement_id, ancmNo) 공고를 두 번 upsert 한다.
    두 번째 upsert 는 action='unchanged' 이어야 하고,
    canonical_group_id 는 첫 번째 upsert 와 동일해야 한다.

시나리오 B: 같은 ancmNo 를 가진 다른 소스 ID 로 재등록
    IRIS 에서 공고가 사라졌다가 새 ancmId(source_announcement_id) 로 재등록되는 경우.
    ancmNo(공식 공고번호)는 동일하므로 두 row 는 같은 CanonicalProject 에 매칭돼야 한다.

    실데이터 재현이 어려우므로 fixture 로 대체한다.
    NOTE: 실데이터 기반 재등록 검증은 NTIS 구현 이후 수행 예정.

시나리오 C: ancmNo 없는 공고 (fuzzy fallback)
    ancmNo 가 없는 공고를 2건 upsert 한다. 제목·기관·마감연도가 같으면
    같은 canonical_group_id 에 묶여야 하고, 다르면 다른 group 에 분리돼야 한다.

시나리오 D: 내용 변경 후 new_version — canonical_group_id 승계
    공고의 제목이 바뀌면 new_version 분기가 실행된다.
    신규 row 는 구 row 의 canonical_group_id 를 그대로 승계해야 한다.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.repository import upsert_announcement  # noqa: E402

# ── 검증 결과 집계 ────────────────────────────────────────────────────────────

_pass_count = 0
_fail_count = 0


def _check(description: str, condition: bool) -> None:
    """단일 검증 조건을 평가하고 결과를 출력한다."""
    global _pass_count, _fail_count
    if condition:
        _pass_count += 1
        logger.info("  [PASS] {}", description)
    else:
        _fail_count += 1
        logger.error("  [FAIL] {}", description)


def _make_engine():
    """인메모리 SQLite 엔진을 생성하고 스키마를 초기화한다."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    return engine


# ── Fixture 헬퍼 ──────────────────────────────────────────────────────────────


def _payload(
    *,
    src_id: str,
    ancm_no: str | None = None,
    title: str = "2026년 AI 연구과제 공고",
    agency: str | None = "한국연구재단",
    status: str = "접수중",
    deadline_at: datetime | None = datetime(2026, 6, 30, tzinfo=UTC),
) -> dict:
    """테스트용 payload dict 를 생성한다."""
    return {
        "source_announcement_id": src_id,
        "source_type": "IRIS",
        "title": title,
        "agency": agency,
        "status": status,
        "deadline_at": deadline_at,
        "ancm_no": ancm_no,
    }


# ── 시나리오 A ─────────────────────────────────────────────────────────────────


def scenario_a(session: Session) -> None:
    """같은 ancmNo 재수집 — unchanged 분기, canonical_group_id 유지."""
    logger.info("\n[시나리오 A] 같은 ancmNo 재수집 (unchanged 분기)")

    p = _payload(src_id="IRIS-001", ancm_no="2026-0001호")

    # 첫 번째 수집
    r1 = upsert_announcement(session, p)
    session.commit()

    _check("첫 번째 action=created", r1.action == "created")
    _check("canonical_group_id 할당됨", r1.announcement.canonical_group_id is not None)
    _check("canonical_key_scheme=official", r1.announcement.canonical_key_scheme == "official")
    group_id = r1.announcement.canonical_group_id

    # 두 번째 수집 (동일 payload)
    r2 = upsert_announcement(session, p)
    session.commit()

    _check("두 번째 action=unchanged", r2.action == "unchanged")
    _check(
        "canonical_group_id 동일 유지",
        r2.announcement.canonical_group_id == group_id,
    )


# ── 시나리오 B ─────────────────────────────────────────────────────────────────


def scenario_b(session: Session) -> None:
    """같은 ancmNo 가 다른 소스 ID 로 재등록 — 동일 canonical_group 에 매칭.

    NOTE: IRIS 재등록(ancmId 가 바뀌는 경우)의 실데이터 검증은
    NTIS 구현 이후 실데이터로 최종 점검할 예정. 여기서는 fixture 로 대체한다.
    """
    logger.info("\n[시나리오 B] 같은 ancmNo 다른 소스ID 재등록 (fixture)")

    ancm_no = "2026-0002호"
    p_orig = _payload(src_id="IRIS-002", ancm_no=ancm_no, title="원본 공고")
    p_rereg = _payload(src_id="IRIS-003", ancm_no=ancm_no, title="재등록 공고")

    r1 = upsert_announcement(session, p_orig)
    session.commit()
    group_id = r1.announcement.canonical_group_id

    r2 = upsert_announcement(session, p_rereg)
    session.commit()

    _check("원본 공고 action=created", r1.action == "created")
    _check("재등록 공고 action=created", r2.action == "created")
    _check("원본 canonical_group_id 할당됨", group_id is not None)
    _check(
        "재등록 공고가 같은 canonical_group 에 매칭됨",
        r2.announcement.canonical_group_id == group_id,
    )
    _check(
        "두 공고 canonical_key 동일",
        r1.announcement.canonical_key == r2.announcement.canonical_key,
    )


# ── 시나리오 C ─────────────────────────────────────────────────────────────────


def scenario_c(session: Session) -> None:
    """ancmNo 없는 공고 — fuzzy fallback.

    제목·기관·마감연도가 같으면 같은 그룹, 다르면 다른 그룹.
    """
    logger.info("\n[시나리오 C] ancmNo 없는 공고 (fuzzy fallback)")

    deadline = datetime(2026, 6, 30, tzinfo=UTC)

    p1 = _payload(
        src_id="IRIS-004",
        ancm_no=None,
        title="2026년 바이오 과제 공고",
        agency="생명공학연구원",
        deadline_at=deadline,
    )
    p2 = _payload(
        src_id="IRIS-005",
        ancm_no=None,
        title="2026년 바이오 과제 공고",
        agency="생명공학연구원",
        deadline_at=deadline,
    )
    p3 = _payload(
        src_id="IRIS-006",
        ancm_no=None,
        title="2026년 소재 과제 공고",  # 제목 다름
        agency="생명공학연구원",
        deadline_at=deadline,
    )

    r1 = upsert_announcement(session, p1)
    r2 = upsert_announcement(session, p2)
    r3 = upsert_announcement(session, p3)
    session.commit()

    _check("p1 canonical_key_scheme=fuzzy", r1.announcement.canonical_key_scheme == "fuzzy")
    _check("p2 canonical_key_scheme=fuzzy", r2.announcement.canonical_key_scheme == "fuzzy")
    _check(
        "제목·기관·연도 같으면 같은 canonical_group",
        r1.announcement.canonical_group_id == r2.announcement.canonical_group_id,
    )
    _check(
        "제목 다르면 다른 canonical_group",
        r1.announcement.canonical_group_id != r3.announcement.canonical_group_id,
    )


# ── 시나리오 D ─────────────────────────────────────────────────────────────────


def scenario_d(session: Session) -> None:
    """내용 변경 new_version — canonical_group_id 승계."""
    logger.info("\n[시나리오 D] 내용 변경 new_version — canonical_group_id 승계")

    p_v1 = _payload(src_id="IRIS-007", ancm_no="2026-0007호", title="원본 제목")
    r1 = upsert_announcement(session, p_v1)
    session.commit()
    group_id = r1.announcement.canonical_group_id

    # 제목 변경 → new_version 분기
    p_v2 = dict(p_v1)
    p_v2["title"] = "수정된 제목"
    r2 = upsert_announcement(session, p_v2)
    session.commit()

    _check("v1 action=created", r1.action == "created")
    _check("v2 action=new_version", r2.action == "new_version")
    _check(
        "v2 가 v1 의 canonical_group_id 를 승계",
        r2.announcement.canonical_group_id == group_id,
    )
    _check(
        "v2 canonical_key 동일 (같은 ancmNo)",
        r2.announcement.canonical_key == r1.announcement.canonical_key,
    )


# ── 메인 ──────────────────────────────────────────────────────────────────────


def main() -> int:
    """검증 스크립트 진입점. 실패 건수를 종료코드로 반환한다."""
    logger.info("IRIS canonical 매칭 검증 시작")

    engine = _make_engine()

    with Session(engine) as session:
        scenario_a(session)
        scenario_b(session)
        scenario_c(session)
        scenario_d(session)

    logger.info("\n========================================")
    logger.info("검증 결과: PASS={} FAIL={}", _pass_count, _fail_count)

    if _fail_count == 0:
        logger.info("모든 시나리오 통과.")
    else:
        logger.error("{} 개 시나리오가 실패했습니다.", _fail_count)

    logger.info("========================================")
    logger.info(
        "\n체크리스트 (수동 확인 항목)\n"
        "  □ 실데이터 기반 재등록(ancmId 변경) 검증 — NTIS 구현 이후 수행\n"
        "  □ ancmNo 공란 케이스가 실제 운영 데이터에서 발생하는지 확인\n"
        "  □ 재공고(재공고여부=Y) 시 ancmNo 재사용 여부 확인"
    )

    return _fail_count


if __name__ == "__main__":
    sys.exit(main())
