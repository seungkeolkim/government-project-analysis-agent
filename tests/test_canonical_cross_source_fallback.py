"""_apply_canonical 의 cross-source fallback 매칭 회귀 테스트 (task 00151).

task 00151 변경 이후 canonical 매칭은 두 단계로 동작한다.

1. canonical_key 정확일치 — 동일한 합성 키를 가진 announcement 끼리는 같은
   CanonicalProject 를 공유한다. NTIS suffix(`_(YYYY)<사업명>`) 가 sub-business
   식별자 역할을 하므로 canonical_key 본문에 그대로 포함된다.
2. cross-source fallback — official scheme 에서 canonical_key 가 다르더라도
   `app/db/repository.py::_find_cross_source_canonical_group` 가 다음 조건을
   모두 만족할 때 동일 group 으로 묶는다.
   - 같은 ancmNo prefix(`official:<X>::`).
   - same source_type 의 다른 announcement 가 없다 (sub-business 분기 가드).
   - cross source_type 후보가 정확히 1건이고, NTIS suffix 절단 후 title 이
     동치.

본 파일은 다음 시나리오를 in-memory SQLite + upsert_announcement 흐름으로
검증한다.

A. **그룹 17** (IRIS=17 + NTIS=35, ancmNo 2026-0485호) — 정상 cross-source 묶음.
   IRIS title 과 NTIS title 의 prefix 가 같지만 NTIS 측에 sub-business suffix
   가 부착된 케이스. fallback 매칭으로 같은 canonical_group_id 를 공유해야
   한다. (이전에는 `_normalize_official_title` 의 suffix 절단으로 canonical_key
   자체가 같았으나, 새 로직에서는 키가 다르고 fallback 이 묶는다.)

B. **그룹 121** (IRIS=133 + NTIS=162, ancmNo 2026-0601호) — A 와 동일 패턴의
   또 다른 cross-source 묶음. NTIS suffix 가 부착된 정상 매칭.

C. **그룹 152** (NTIS=173 + NTIS=174, ancmNo 2026-0627호) — task 00151 의 사고
   대상. 동일 ancmNo + same source_type(NTIS) + 서로 다른 sub-business 두
   건. fallback 의 "same source 가드" 로 인해 매칭이 거부되어야 하고, 두 row
   는 서로 다른 canonical_group_id 를 가져야 한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.db.repository import upsert_announcement


# ──────────────────────────────────────────────────────────────
# 테스트 데이터 — 실제 운영 DB row 의 ancmNo / title 을 그대로 사용.
# ──────────────────────────────────────────────────────────────


_GROUP_17_ANCM_NO = "과학기술정보통신부 공고 제2026-0485호"
_GROUP_17_IRIS_TITLE = "2026년도 대형가속기 기술개발·진흥 지원체계 구축 신규과제 재공모 "
_GROUP_17_NTIS_TITLE = (
    "2026년도 대형가속기 기술개발·진흥 지원체계 구축 신규과제 재공모 "
    "_(2026)2026년도 대형가속기정책센터 신규과제 재공모"
)

_GROUP_121_ANCM_NO = "과학기술정보통신부 공고 제2026-0601호"
_GROUP_121_IRIS_TITLE = "2026년도 나노소재기술개발사업 신규과제 3차 재공모"
_GROUP_121_NTIS_TITLE = (
    "2026년도 나노소재기술개발사업 신규과제 3차 재공모"
    "_(2026)국가전략기술미래소재기술개발-소재HUB"
)

_GROUP_152_ANCM_NO = "과학기술정보통신부 공고 제2026-0627호"
_GROUP_152_NTIS_TITLE_173 = (
    "2026년도 나노소재기술개발사업 신규과제 4차 재공모"
    "_(2026)국가전략기술미래소재기술개발(미래소재) 공고"
)
_GROUP_152_NTIS_TITLE_174 = (
    "2026년도 나노소재기술개발사업 신규과제 4차 재공모"
    "_(2026)글로벌공급망첨단소재기술개발-나노커넥트 공고"
)

# 그룹 121 변형 — 동일 ancmNo 아래 IRIS umbrella 1건 + NTIS sub-business 2건.
# 운영 DB 실측: ann 133 (IRIS umbrella), ann 156 (NTIS, "-플랫폼형" suffix
# 는 NTIS pattern 이 아님 → strip 결과가 IRIS 와 다름), ann 162 (NTIS, "_(YYYY)X"
# NTIS suffix 부착 → strip 결과가 IRIS 와 동치).
_GROUP_121_VARIANT_NTIS_TITLE_PLATFORM = (
    "2026년도 나노소재기술개발사업 신규과제 3차 재공모-플랫폼형"
)


def _payload(
    *,
    source_type: str,
    source_announcement_id: str,
    title: str,
    ancm_no: str,
    agency: str = "주관기관",
) -> dict:
    """upsert_announcement 에 넘길 최소 payload 빌더.

    deadline_at 은 임의 미래 시각으로 고정 (변경 감지·상태 분기와 무관한 테스트
    이므로 결정값으로 충분).
    """
    return {
        "source_announcement_id": source_announcement_id,
        "source_type": source_type,
        "title": title,
        "agency": agency,
        "status": "접수중",
        "deadline_at": datetime(2026, 12, 31, tzinfo=UTC),
        "raw_metadata": {},
        "ancm_no": ancm_no,
    }


# ──────────────────────────────────────────────────────────────
# 1) cross-source fallback — 같은 group 으로 묶여야 하는 케이스.
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case_id", "ancm_no", "iris_title", "ntis_title"),
    [
        (
            "group_17_large_accelerator_with_ntis_suffix",
            _GROUP_17_ANCM_NO,
            _GROUP_17_IRIS_TITLE,
            _GROUP_17_NTIS_TITLE,
        ),
        (
            "group_121_nano_material_third_recall_with_ntis_suffix",
            _GROUP_121_ANCM_NO,
            _GROUP_121_IRIS_TITLE,
            _GROUP_121_NTIS_TITLE,
        ),
    ],
    ids=["group_17", "group_121"],
)
def test_cross_source_fallback_groups_iris_and_ntis_with_business_suffix(
    db_session: Session,
    case_id: str,
    ancm_no: str,
    iris_title: str,
    ntis_title: str,
) -> None:
    """IRIS title + NTIS title (with `_(YYYY)<사업명>` suffix) 가 같은 group.

    task 00151 변경 이후 두 입력의 canonical_key 본문(suffix 보존)은 다르지만,
    `_find_cross_source_canonical_group` fallback 이 동일 ancmNo + title prefix
    매칭을 통해 같은 CanonicalProject 로 묶어야 한다.
    """
    # IRIS 가 먼저 들어온 뒤 NTIS 가 등록되는 운영 순서를 그대로 재현.
    iris_result = upsert_announcement(
        db_session,
        _payload(
            source_type="IRIS",
            source_announcement_id=f"IRIS-{case_id}",
            title=iris_title,
            ancm_no=ancm_no,
        ),
    )
    ntis_result = upsert_announcement(
        db_session,
        _payload(
            source_type="NTIS",
            source_announcement_id=f"NTIS-{case_id}",
            title=ntis_title,
            ancm_no=ancm_no,
        ),
    )

    iris_ann = iris_result.announcement
    ntis_ann = ntis_result.announcement

    assert iris_ann.canonical_group_id is not None, "IRIS 측 canonical_group_id 가 채워져야 한다."
    assert ntis_ann.canonical_group_id is not None, "NTIS 측 canonical_group_id 가 채워져야 한다."
    assert iris_ann.canonical_group_id == ntis_ann.canonical_group_id, (
        f"[{case_id}] cross-source fallback 매칭 실패: "
        f"IRIS group={iris_ann.canonical_group_id}, NTIS group={ntis_ann.canonical_group_id}"
    )
    # canonical_key 자체는 suffix 보존으로 서로 다르다 — 일관성 검증.
    assert iris_ann.canonical_key != ntis_ann.canonical_key, (
        f"[{case_id}] suffix 보존으로 두 announcement.canonical_key 는 달라야 한다 "
        f"(같은 group_id 공유는 fallback 매칭의 결과)."
    )


def test_cross_source_fallback_works_when_ntis_inserted_before_iris(
    db_session: Session,
) -> None:
    """fallback 매칭은 등록 순서에 무관해야 한다 — NTIS 가 먼저 들어오는 케이스.

    실제 운영에서는 IRIS 가 먼저 도착하는 게 일반적이지만, 순서 의존적 매칭이면
    재계산 / 재처리 시 그룹이 깨질 수 있으므로 양쪽 순서 모두 보장해야 한다.
    """
    ntis_result = upsert_announcement(
        db_session,
        _payload(
            source_type="NTIS",
            source_announcement_id="NTIS-order-reversed",
            title=_GROUP_121_NTIS_TITLE,
            ancm_no=_GROUP_121_ANCM_NO,
        ),
    )
    iris_result = upsert_announcement(
        db_session,
        _payload(
            source_type="IRIS",
            source_announcement_id="IRIS-order-reversed",
            title=_GROUP_121_IRIS_TITLE,
            ancm_no=_GROUP_121_ANCM_NO,
        ),
    )

    assert iris_result.announcement.canonical_group_id == ntis_result.announcement.canonical_group_id


# ──────────────────────────────────────────────────────────────
# 2) sub-business 분리 — task 00151 사고 케이스 (173/174).
# ──────────────────────────────────────────────────────────────


def test_same_source_subbusiness_announcements_get_separate_groups(
    db_session: Session,
) -> None:
    """동일 ancmNo + same source_type(NTIS) 의 sub-business 2건은 분리되어야 한다.

    task 00151 의 실제 사고 입력(173, 174). 두 NTIS row 가 같은
    canonical_group_id 로 잘못 묶이지 않아야 한다.
    """
    result_173 = upsert_announcement(
        db_session,
        _payload(
            source_type="NTIS",
            source_announcement_id="1267598",
            title=_GROUP_152_NTIS_TITLE_173,
            ancm_no=_GROUP_152_ANCM_NO,
        ),
    )
    result_174 = upsert_announcement(
        db_session,
        _payload(
            source_type="NTIS",
            source_announcement_id="1267597",
            title=_GROUP_152_NTIS_TITLE_174,
            ancm_no=_GROUP_152_ANCM_NO,
        ),
    )

    ann_173 = result_173.announcement
    ann_174 = result_174.announcement

    assert ann_173.canonical_group_id is not None
    assert ann_174.canonical_group_id is not None
    assert ann_173.canonical_group_id != ann_174.canonical_group_id, (
        "173/174 는 별개 sub-business 이므로 서로 다른 canonical_group_id 를 가져야 한다."
    )
    assert ann_173.canonical_key != ann_174.canonical_key, (
        "173/174 의 canonical_key 본문(NTIS suffix 보존) 자체가 달라야 한다."
    )


def test_iris_umbrella_with_ntis_subbusinesses_preserves_matching_pair_only(
    db_session: Session,
) -> None:
    """동일 ancmNo 아래 IRIS umbrella 1건 + NTIS sub-business 2건 케이스.

    운영 DB 실측 케이스(ann 133 / 156 / 162, ancmNo 2026-0601호) 회귀.

    - IRIS title 과 NTIS title strip 이 동치인 한 쌍(133, 162) 은 같은 group 으로 유지.
    - 다른 NTIS sub-business(156, "-플랫폼형" suffix — NTIS pattern 아님) 는
      IRIS umbrella 와 title strip 이 다르므로 cross-source 매칭 후보에서
      배제되고, 자기 group 으로 떨어진다.

    같은 strip 을 공유하는 same-source 가 없으므로(156 strip ≠ 162 strip ≠ IRIS strip)
    matching pair 만 안전히 유지된다.
    """
    iris_result = upsert_announcement(
        db_session,
        _payload(
            source_type="IRIS",
            source_announcement_id="IRIS-0601",
            title=_GROUP_121_IRIS_TITLE,
            ancm_no=_GROUP_121_ANCM_NO,
        ),
    )
    ntis_platform_result = upsert_announcement(
        db_session,
        _payload(
            source_type="NTIS",
            source_announcement_id="NTIS-0601-platform",
            title=_GROUP_121_VARIANT_NTIS_TITLE_PLATFORM,
            ancm_no=_GROUP_121_ANCM_NO,
        ),
    )
    ntis_hub_result = upsert_announcement(
        db_session,
        _payload(
            source_type="NTIS",
            source_announcement_id="NTIS-0601-hub",
            title=_GROUP_121_NTIS_TITLE,
            ancm_no=_GROUP_121_ANCM_NO,
        ),
    )

    iris_group = iris_result.announcement.canonical_group_id
    platform_group = ntis_platform_result.announcement.canonical_group_id
    hub_group = ntis_hub_result.announcement.canonical_group_id

    assert iris_group is not None
    assert platform_group is not None
    assert hub_group is not None

    # IRIS umbrella 와 strip 이 일치하는 NTIS(-소재HUB) 는 같은 group.
    assert iris_group == hub_group, (
        "IRIS umbrella 와 strip-title 이 동치인 NTIS(-소재HUB) 는 같은 group 이어야 한다. "
        f"(IRIS={iris_group}, NTIS-hub={hub_group})"
    )
    # strip 이 다른 NTIS(-플랫폼형) 는 IRIS umbrella 와 별개 group.
    assert platform_group != iris_group, (
        "IRIS strip 과 다른 NTIS(-플랫폼형) 는 별개 group 이어야 한다. "
        f"(IRIS={iris_group}, NTIS-platform={platform_group})"
    )
    # 두 NTIS sub-business 도 서로 다른 group.
    assert platform_group != hub_group, (
        "서로 다른 NTIS sub-business 는 서로 다른 group 이어야 한다."
    )


def test_iris_arriving_after_two_ntis_subbusinesses_stays_separate(
    db_session: Session,
) -> None:
    """173/174 가 먼저 등록된 상태에서 IRIS 가 같은 ancmNo 로 들어와도 모두 분리.

    NTIS 두 sub-business 가 이미 candidates 에 있으면 fallback 의 "cross-source
    후보가 정확히 1건" 조건이 깨지므로, IRIS 는 새 group 으로 떨어진다. (만약
    이 가드가 없다면 IRIS 가 NTIS 중 어느 쪽과 합쳐질지 모호한 결과가 나온다.)
    """
    result_173 = upsert_announcement(
        db_session,
        _payload(
            source_type="NTIS",
            source_announcement_id="1267598",
            title=_GROUP_152_NTIS_TITLE_173,
            ancm_no=_GROUP_152_ANCM_NO,
        ),
    )
    result_174 = upsert_announcement(
        db_session,
        _payload(
            source_type="NTIS",
            source_announcement_id="1267597",
            title=_GROUP_152_NTIS_TITLE_174,
            ancm_no=_GROUP_152_ANCM_NO,
        ),
    )
    iris_result = upsert_announcement(
        db_session,
        _payload(
            source_type="IRIS",
            source_announcement_id="IRIS-0627-mock",
            title="2026년도 나노소재기술개발사업 신규과제 4차 재공모",
            ancm_no=_GROUP_152_ANCM_NO,
        ),
    )

    iris_group = iris_result.announcement.canonical_group_id
    group_173 = result_173.announcement.canonical_group_id
    group_174 = result_174.announcement.canonical_group_id

    assert iris_group is not None and group_173 is not None and group_174 is not None
    assert len({iris_group, group_173, group_174}) == 3, (
        "동일 ancmNo 아래 IRIS 1건 + NTIS sub-business 2건 시나리오에서 "
        f"세 row 모두 별개 group 이어야 한다 (got iris={iris_group}, "
        f"173={group_173}, 174={group_174})."
    )
