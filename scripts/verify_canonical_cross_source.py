"""Cross-source canonical 매칭 검증 스크립트 (IRIS ↔ NTIS).

임시 SQLite DB(:memory:)에 fixture 를 삽입하여 cross-source canonical 매칭 동작을
재현 가능하게 검증한다. 프로덕션 DB 는 건드리지 않으며, 스크립트 종료 시 임시 DB 도 사라진다.

fixture 기준 공고: "2026년도 한-스페인 공동연구사업 신규과제 공모"
    IRIS ancmId=020640, ancmNo='과학기술정보통신부 공고 제2026-0455호'
    NTIS roRndUid=1262378, 상세 파싱 ntis_ancm_no='과학기술정보통신부공고제2026-0455호'
    출처: docs/canonical_identity_design.md §3-3, docs/ntis_site_exploration.md §4-3

검증 시나리오
-----------
시나리오 E: IRIS official 키 + NTIS 목록 단계(fuzzy) → 서로 다른 canonical group
    NTIS 목록 단계에서는 공고번호를 알 수 없어 fuzzy canonical 이 부여된다.
    IRIS ancmNo 기반 official key 와는 다른 그룹에 배치됨을 확인한다.

시나리오 F: IRIS official 키 + NTIS 상세 후 재계산(official) → 같은 canonical group
    NTIS 상세 수집 후 ntis_ancm_no 확보 → recompute_canonical_with_ancm_no 호출 →
    IRIS와 동일한 official key 로 승급되어 같은 그룹에 묶임을 확인한다.
    (실제 수집 파이프라인의 canonical 승급 경로 재현)

시나리오 G: False-positive 방어 — 유사한 제목의 다른 공고 fuzzy key 충돌 여부
    완전히 다른 공고가 제목 앞부분 50자가 우연히 같아 fuzzy key 가 충돌하는지 확인한다.
    의도적으로 충돌시킨 케이스(title 앞 50자 동일, agency 다름)로 분리됨을 검증한다.

시나리오 H: False-negative 방어 — 전각·공백 변이 동일 ancmNo 매칭
    공고번호에 전각문자·공백·en-dash 변이가 있어도 NFKC 정규화 후 같은 official key 로
    매핑됨을 확인한다. (탐사 §4-3: roRndUid=1262576, 공고번호에 \\xa0+en-dash 혼용 실측)
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
from app.db.repository import recompute_canonical_with_ancm_no, upsert_announcement  # noqa: E402

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

# 탐사에서 실측된 cross-source 동일 공고 fixture 데이터 (docs/canonical_identity_design.md §3-3)
_SHARED_TITLE = "2026년도 한-스페인 공동연구사업 신규과제 공모"
_SHARED_AGENCY = "한국연구재단"
_SHARED_DEADLINE = datetime(2026, 5, 19, tzinfo=UTC)
# IRIS ancmNo 원문 (목록 API 구조화 필드)
_IRIS_ANCM_NO_RAW = "과학기술정보통신부 공고 제2026-0455호"
# NTIS 상세 파싱 결과 (ntis_ancm_no, 이미 NFKC+공백제거 정규화 완료 — detail_scraper 산출물)
_NTIS_ANCM_NO_NORMALIZED = "과학기술정보통신부공고제2026-0455호"


def _iris_payload(*, ancm_no: str | None = _IRIS_ANCM_NO_RAW) -> dict:
    """IRIS fixture payload 를 생성한다."""
    return {
        "source_announcement_id": "020640",
        "source_type": "IRIS",
        "title": _SHARED_TITLE,
        "agency": _SHARED_AGENCY,
        "status": "접수중",
        "deadline_at": _SHARED_DEADLINE,
        "ancm_no": ancm_no,
    }


def _ntis_list_payload() -> dict:
    """NTIS 목록 단계 payload (ancm_no=None — 목록에서는 공고번호 미확보)."""
    return {
        "source_announcement_id": "1262378",
        "source_type": "NTIS",
        "title": _SHARED_TITLE,
        "agency": _SHARED_AGENCY,
        "status": "접수중",
        "deadline_at": _SHARED_DEADLINE,
        "ancm_no": None,
    }


# ── 시나리오 E ─────────────────────────────────────────────────────────────────


def scenario_e(session: Session) -> None:
    """IRIS(official) + NTIS 목록(fuzzy) → 서로 다른 canonical group.

    NTIS 목록 단계에서는 공식 공고번호를 알 수 없어 fuzzy canonical 이 부여된다.
    이 단계에서는 cross-source 매칭이 이루어지지 않으므로 서로 다른 그룹에 배치된다.
    이는 '아직 매칭되지 않은' 상태이지, false-positive 도 false-negative 도 아니다.
    상세 수집 후 시나리오 F 에서 official key 로 승급된다.
    """
    logger.info("\n[시나리오 E] IRIS(official) + NTIS 목록(fuzzy) → 서로 다른 그룹")

    iris_r = upsert_announcement(session, _iris_payload())
    ntis_r = upsert_announcement(session, _ntis_list_payload())
    session.commit()

    iris_group = iris_r.announcement.canonical_group_id
    ntis_group = ntis_r.announcement.canonical_group_id

    _check("IRIS canonical_key_scheme=official", iris_r.announcement.canonical_key_scheme == "official")
    _check("NTIS(목록) canonical_key_scheme=fuzzy", ntis_r.announcement.canonical_key_scheme == "fuzzy")
    _check(
        "목록 단계에서는 서로 다른 canonical group (아직 매칭 전)",
        iris_group != ntis_group,
    )

    logger.info(
        "  IRIS canonical_key  : {}", iris_r.announcement.canonical_key
    )
    logger.info(
        "  NTIS(목록) canonical_key: {}", ntis_r.announcement.canonical_key
    )


# ── 시나리오 F ─────────────────────────────────────────────────────────────────


def scenario_f(session: Session) -> None:
    """NTIS 상세 수집 후 recompute_canonical_with_ancm_no → IRIS 와 같은 canonical group.

    실제 수집 파이프라인의 canonical 승급 경로:
        1. NTIS 목록 수집 → fuzzy canonical
        2. NTIS 상세 수집 → ntis_ancm_no 확보
        3. recompute_canonical_with_ancm_no(ntis_ancm_no) → official key 로 승급
        4. IRIS official key 와 동일한 그룹에 매칭됨
    """
    logger.info("\n[시나리오 F] NTIS 상세 후 canonical 재계산 → IRIS 와 같은 그룹")

    # IRIS 공고 먼저 수집 (official canonical 설정됨)
    iris_r = upsert_announcement(session, _iris_payload())
    session.commit()
    iris_group = iris_r.announcement.canonical_group_id
    iris_key = iris_r.announcement.canonical_key

    # NTIS 목록 단계 수집 (fuzzy canonical)
    ntis_r = upsert_announcement(session, _ntis_list_payload())
    session.commit()
    ntis_fuzzy_key = ntis_r.announcement.canonical_key
    ntis_id = ntis_r.announcement.id

    _check("NTIS 목록 단계: fuzzy scheme", ntis_r.announcement.canonical_key_scheme == "fuzzy")
    _check("NTIS 목록 단계: IRIS 와 다른 그룹", ntis_r.announcement.canonical_group_id != iris_group)

    # NTIS 상세 수집 후 ntis_ancm_no 확보 → canonical 재계산
    recomputed = recompute_canonical_with_ancm_no(
        session,
        "1262378",
        source_type="NTIS",
        ancm_no=_NTIS_ANCM_NO_NORMALIZED,
    )
    session.commit()

    # DB 에서 최신 상태 재조회
    session.expire_all()
    ntis_ann = session.get(type(ntis_r.announcement), ntis_id)

    _check("recompute_canonical_with_ancm_no 가 True 반환", recomputed is True)
    _check("재계산 후 NTIS canonical_key_scheme=official", ntis_ann.canonical_key_scheme == "official")
    _check(
        "재계산 후 NTIS canonical_key 가 IRIS 와 동일",
        ntis_ann.canonical_key == iris_key,
    )
    _check(
        "재계산 후 NTIS 가 IRIS 와 같은 canonical group 에 매칭됨",
        ntis_ann.canonical_group_id == iris_group,
    )

    logger.info("  IRIS canonical_key : {}", iris_key)
    logger.info("  NTIS fuzzy_key(전) : {}", ntis_fuzzy_key)
    logger.info("  NTIS official_key(후): {}", ntis_ann.canonical_key)
    logger.info("  IRIS group_id={} NTIS group_id={}", iris_group, ntis_ann.canonical_group_id)


# ── 시나리오 G ─────────────────────────────────────────────────────────────────


def scenario_g(session: Session) -> None:
    """False-positive 방어: 제목 앞 50자가 같아도 agency 다르면 다른 fuzzy key.

    fuzzy key = {title_50자}:{agency}:{deadline_year}
    agency 가 다르면 fuzzy key 가 달라 별도 그룹에 배치됨 → false-positive 없음.
    """
    logger.info("\n[시나리오 G] False-positive 방어: agency 다른 유사 제목 공고")

    # 제목 앞 50자를 의도적으로 동일하게 구성 (50자 경계 테스트)
    long_title = "2026년도 한-스페인 공동연구사업 신규과제 공모 — 과학기술정보통신부 지원"
    # 위 제목의 정규화된 50자: 특수문자·공백 제거 후 앞 50자
    # '2026년도한스페인공동연구사업신규과제공모과학기술정보통신부지원'[:50]

    p1 = {
        "source_announcement_id": "FP-001",
        "source_type": "IRIS",
        "title": long_title,
        "agency": "한국연구재단",
        "status": "접수중",
        "deadline_at": datetime(2026, 5, 19, tzinfo=UTC),
        "ancm_no": None,
    }
    p2 = {
        "source_announcement_id": "FP-002",
        "source_type": "NTIS",
        "title": long_title,
        "agency": "국가과학기술연구회",  # agency 다름 → fuzzy key 다름
        "status": "접수중",
        "deadline_at": datetime(2026, 5, 19, tzinfo=UTC),
        "ancm_no": None,
    }

    r1 = upsert_announcement(session, p1)
    r2 = upsert_announcement(session, p2)
    session.commit()

    _check("p1 canonical_key_scheme=fuzzy", r1.announcement.canonical_key_scheme == "fuzzy")
    _check("p2 canonical_key_scheme=fuzzy", r2.announcement.canonical_key_scheme == "fuzzy")
    _check(
        "agency 다른 유사 제목 → 다른 canonical group (false-positive 없음)",
        r1.announcement.canonical_group_id != r2.announcement.canonical_group_id,
    )

    logger.info("  p1 fuzzy_key: {}", r1.announcement.canonical_key)
    logger.info("  p2 fuzzy_key: {}", r2.announcement.canonical_key)


# ── 시나리오 H ─────────────────────────────────────────────────────────────────


def scenario_h(session: Session) -> None:
    """False-negative 방어: 전각·공백·en-dash 변이 동일 ancmNo → 같은 official key.

    탐사 §4-3 실측 케이스: roRndUid=1262576 공고번호 원문
        '과학기술정보통신부 공고 제2026\\xa0–\\xa00484호'
    NFKC 정규화 + dash 통일 + 공백 제거 후: '과학기술정보통신부공고제2026-0484호'

    동일 공고가 소스마다 공백 표기가 달라도 official key 가 일치함을 확인한다.
    """
    logger.info("\n[시나리오 H] False-negative 방어: 표기 변이 ancmNo → 같은 official key")

    # IRIS 측: '과학기술정보통신부 공고 제2026-0484호' (공백 포함 원문)
    iris_ancm_no_v1 = "과학기술정보통신부 공고 제2026-0484호"
    # NTIS 측: '\xa0' + en-dash 변이 포함 원문 (탐사 실측값)
    ntis_ancm_no_v2 = "과학기술정보통신부 공고 제2026\xa0–\xa00484호"

    p_iris = {
        "source_announcement_id": "020641",
        "source_type": "IRIS",
        "title": "2026년도 소재·부품·장비 핵심기술 개발사업 공모",
        "agency": "한국산업기술진흥원",
        "status": "접수중",
        "deadline_at": datetime(2026, 5, 31, tzinfo=UTC),
        "ancm_no": iris_ancm_no_v1,
    }
    p_ntis_list = {
        "source_announcement_id": "1262576",
        "source_type": "NTIS",
        "title": "2026년도 소재·부품·장비 핵심기술 개발사업 공모",
        "agency": "한국산업기술진흥원",
        "status": "접수중",
        "deadline_at": datetime(2026, 5, 31, tzinfo=UTC),
        "ancm_no": None,
    }

    r_iris = upsert_announcement(session, p_iris)
    r_ntis = upsert_announcement(session, p_ntis_list)
    session.commit()

    iris_group = r_iris.announcement.canonical_group_id

    # NTIS 상세 수집 결과: ntis_ancm_no (detail_scraper 정규화 산출물)
    # detail_scraper._extract_ancm_no: NFKC + en/em-dash→'-' + 공백 제거
    ntis_ancm_no_from_detail = "과학기술정보통신부공고제2026-0484호"

    recomputed = recompute_canonical_with_ancm_no(
        session,
        "1262576",
        source_type="NTIS",
        ancm_no=ntis_ancm_no_from_detail,
    )
    session.commit()

    session.expire_all()
    ntis_ann = session.get(type(r_ntis.announcement), r_ntis.announcement.id)

    _check("recompute 성공", recomputed is True)
    _check(
        "표기 변이 ancmNo 정규화 후 IRIS official key 와 동일",
        ntis_ann.canonical_key == r_iris.announcement.canonical_key,
    )
    _check(
        "표기 변이 → 같은 canonical group 에 매칭됨 (false-negative 없음)",
        ntis_ann.canonical_group_id == iris_group,
    )

    logger.info("  IRIS ancmNo 원문  : {!r}", iris_ancm_no_v1)
    logger.info("  NTIS ancmNo 원문  : {!r}", ntis_ancm_no_v2)
    logger.info("  detail_scraper 정규화 결과: {!r}", ntis_ancm_no_from_detail)
    logger.info("  IRIS canonical_key: {}", r_iris.announcement.canonical_key)
    logger.info("  NTIS canonical_key: {}", ntis_ann.canonical_key)


# ── 메인 ──────────────────────────────────────────────────────────────────────


def main() -> int:
    """검증 스크립트 진입점. 실패 건수를 종료코드로 반환한다."""
    logger.info("Cross-source canonical 매칭 검증 시작 (IRIS ↔ NTIS)")

    engine = _make_engine()

    with Session(engine) as session:
        scenario_e(session)

    # 시나리오 F 는 별도 세션에서 실행 (E 의 데이터와 source_announcement_id 충돌 방지)
    with Session(engine) as session:
        scenario_f(session)

    with Session(engine) as session:
        scenario_g(session)

    with Session(engine) as session:
        scenario_h(session)

    logger.info("\n========================================")
    logger.info("검증 결과: PASS={} FAIL={}", _pass_count, _fail_count)

    if _fail_count == 0:
        logger.info("모든 시나리오 통과.")
    else:
        logger.error("{} 개 시나리오가 실패했습니다.", _fail_count)

    logger.info("========================================")
    logger.info(
        "\n체크리스트 (수동 확인 항목)\n"
        "  □ 실데이터 기반 NTIS 목록 수집 + 상세 canonical 승급 E2E 확인\n"
        "  □ NTIS 개별공고(통합공고 아닌 경우) fuzzy canonical 품질 점검\n"
        "  □ 재공고(공고유형=재공고) 시 ancmNo 재사용 여부 실데이터 확인"
    )

    return _fail_count


if __name__ == "__main__":
    sys.exit(main())
