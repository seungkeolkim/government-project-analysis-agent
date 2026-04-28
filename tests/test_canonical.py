"""compute_canonical_key 회귀 테스트.

이 파일은 ``docs/duplicate_detection_analysis.md`` 가 식별한 두 부류의 케이스를
결정적 단위 테스트로 고정한다.

1. **false-positive 분리** — 동일 공고번호 + 다른 공고명 인 경우 서로 다른
   canonical_key 가 나와야 한다 (분석 §3-2 group 5, 25).
2. **cross-source 유지** — 동일 공고번호 + 동일/등가 공고명 인 경우 같은
   canonical_key 가 나와야 한다 (분석 §3-2 group 16, 17, 18, 20, 그리고 NTIS
   suffix 정규화로 매칭되는 group 15).

두 입력의 canonical_key 가 같은지/다른지를 비교하는 형태로 작성하여
title 정규화의 세부 변경(예: 추가 정규식)에 강건하게 한다. 단, 한 건은
키 포맷(`official:` prefix 와 `::` 구분자) 자체를 직접 assert 하여 합성 키
포맷의 회귀를 잡는다.

ancmNo 변이는 분석 §4-1 표에 기재된 원문을 그대로 사용한다.
"""

from __future__ import annotations

import pytest

from app.canonical import compute_canonical_key


# ──────────────────────────────────────────────────────────────
# 공통 헬퍼 — 테스트용 입력 빌더
# ──────────────────────────────────────────────────────────────


def _key(ancm_no: str, title: str) -> str:
    """compute_canonical_key 를 호출해 canonical_key 문자열만 돌려주는 헬퍼.

    agency / deadline_at 은 official 분기 결과에 영향을 주지 않으므로
    회귀 테스트 가독성을 위해 None 으로 고정한다.

    Args:
        ancm_no: official_key_candidates 의 단일 후보로 넘길 공고번호 원문.
        title:   공고 제목 원문.

    Returns:
        canonical_key 문자열 (scheme 은 별도 fuzzy fallback 테스트에서 검증).
    """
    return compute_canonical_key(
        official_key_candidates=[ancm_no],
        title=title,
        agency=None,
        deadline_at=None,
    ).canonical_key


# ──────────────────────────────────────────────────────────────
# 1) 키 포맷 회귀 — prefix 와 '::' 구분자 직접 assert
# ──────────────────────────────────────────────────────────────


def test_official_key_format_uses_prefix_and_double_colon_separator() -> None:
    """official 분기 키 포맷이 'official:{ancmNo}::{title}' 형태인지 고정한다.

    분석 §8-2 가 명시한 합성 키 포맷의 회귀를 잡는 단일 케이스.
    내용 비교가 아니라 구조(prefix / 구분자) 자체를 검사하므로 정규화 규칙
    변경에 영향을 받지 않는다.
    """
    result = compute_canonical_key(
        official_key_candidates=["산업통상부 공고 제2026-300호"],
        title="2026년도 제조암묵지기반AI모델개발사업 신규지원 대상과제 공고",
        agency=None,
        deadline_at=None,
    )

    assert result.canonical_scheme == "official"
    assert result.canonical_key.startswith("official:")
    # '::' 는 ancmNo 와 title 사이의 구분자. 분석 §8-2 의 가정(ancmNo 가 단일
    # 콜론을 포함하지 않음) 위에서 정확히 1회 등장해야 한다.
    assert result.canonical_key.count("::") == 1
    # 'official:' prefix 의 콜론을 제외하면 단일 콜론은 더 이상 등장하지 않는다.
    body = result.canonical_key[len("official:") :]
    assert ":" not in body.replace("::", "")


# ──────────────────────────────────────────────────────────────
# 2) cross-source 유지 — 같은 키가 나와야 하는 케이스
# ──────────────────────────────────────────────────────────────

# (case_id, ancm_no_a, title_a, ancm_no_b, title_b)
SAME_KEY_CASES: list[tuple[str, str, str, str, str]] = [
    (
        # 분석 §1-1, §3-2 group 17. 사용자 제시 정상 cross-source.
        "group_17_iris_ntis_manufacturing_tacit_knowledge",
        "산업통상부 공고 제2026-300호",
        "2026년도 제조암묵지기반AI모델개발사업 신규지원 대상과제 공고",
        "산업통상부 공고 제2026-300호",
        "2026년도 제조암묵지기반AI모델개발사업 신규지원 대상과제 공고",
    ),
    (
        # 분석 §3-2 group 16. IRIS+NTIS 가속기핵심기술개발사업 — title 정확일치.
        "group_16_iris_ntis_accelerator_core",
        "과학기술정보통신부 공고 제2026-0484호",
        "2026년도 가속기핵심기술개발사업 신규과제 공고",
        "과학기술정보통신부 공고 제2026-0484호",
        "2026년도 가속기핵심기술개발사업 신규과제 공고",
    ),
    (
        # 분석 §5-1, §3-2 group 15. NTIS suffix '_(2026)<사업명>' 절단으로 매칭.
        "group_15_iris_ntis_large_accelerator_with_ntis_suffix",
        "과학기술정보통신부 공고 제2026 -0485호",
        "2026년도 대형가속기 기술개발·진흥 지원체계 구축 신규과제 재공모 ",
        "과학기술정보통신부 공고 제2026 -0485호",
        "2026년도 대형가속기 기술개발·진흥 지원체계 구축 신규과제 재공모 _(2026)2026년도 대형가속기정책센터 신규과제 재공모",
    ),
    (
        # 분석 §5-2, §3-2 group 20. IRIS 측 title 의 leading whitespace 가 정규화로 흡수.
        "group_20_leading_whitespace_normalized",
        "과학기술정보통신부 공고 제2026-0444호",
        " 2026년도 AI반도체 K-클라우드 활용·확산 지원사업",
        "과학기술정보통신부 공고 제2026-0444호",
        "2026년도 AI반도체 K-클라우드 활용·확산 지원사업",
    ),
    (
        # 분석 §4-1 표 — ancmNo 표기 변이('2026 - 0498' / '2026-0498') 가
        # NFKC + 공백 제거로 같은 키가 됨을 확인 (title 동일 가정).
        "ancm_no_whitespace_variant_normalized",
        "과학기술정보통신부 공고 제2026 - 0498호",
        "샘플 공고명",
        "과학기술정보통신부공고제2026-0498호",
        "샘플 공고명",
    ),
]


@pytest.mark.parametrize(
    ("case_id", "ancm_no_a", "title_a", "ancm_no_b", "title_b"),
    SAME_KEY_CASES,
    ids=[case[0] for case in SAME_KEY_CASES],
)
def test_official_key_groups_cross_source_pairs(
    case_id: str,
    ancm_no_a: str,
    title_a: str,
    ancm_no_b: str,
    title_b: str,
) -> None:
    """동일 ancmNo + 정규화 후 동일한 title 두 입력은 같은 canonical_key 를 갖는다.

    분석 §3-2 가 '정상 cross-source' 로 분류한 그룹들이 신규 합성 키
    하에서도 그대로 묶이는지 회귀로 고정한다.
    """
    key_a = _key(ancm_no_a, title_a)
    key_b = _key(ancm_no_b, title_b)
    assert key_a == key_b, f"[{case_id}] expected identical keys but got {key_a!r} vs {key_b!r}"


# ──────────────────────────────────────────────────────────────
# 3) false-positive 분리 — 다른 키가 나와야 하는 케이스
# ──────────────────────────────────────────────────────────────

# group 5 : 5극3특 연구개발특구 딥테크 지원 — 동일 ancmNo 아래 sub-task 2건 (분석 §1-2).
_GROUP_5_ANCM_NO = "과학기술정보통신부 공고 제2026 - 0498호"
_GROUP_5_TITLE_INITIAL_SCALEUP = "5극3특 연구개발특구 딥테크 지원(초기 스케일업)"
_GROUP_5_TITLE_PLANNING_STARTUP = "5극3특 연구개발특구 딥테크 지원(기획형 창업)"

# group 25 : 동일 ancmNo 아래 강원 1-1 / 1-2 / 4-1 / 5 의 sub-task 4건 (분석 §3-2).
# 분석 문서가 4건 모두 별개 과제라고 결론지었으므로 6쌍 모두 다른 키여야 한다.
_GROUP_25_ANCM_NO = "과학기술정보통신부 공고 제2026 - 0362호"
_GROUP_25_TITLE_GANGWON_1_1 = "2026년도 강원 1-1 신규과제 공고"
_GROUP_25_TITLE_GANGWON_1_2 = "2026년도 강원 1-2 신규과제 공고"
_GROUP_25_TITLE_GANGWON_4_1 = "2026년도 강원 4-1 신규과제 공고"
_GROUP_25_TITLE_GANGWON_5 = "2026년도 강원 5 신규과제 공고"

# (case_id, ancm_no_a, title_a, ancm_no_b, title_b)
DIFFERENT_KEY_CASES: list[tuple[str, str, str, str, str]] = [
    (
        # 분석 §1-2, §3-2 group 5 — 사용자 제시 false-positive.
        "group_5_deeptech_initial_scaleup_vs_planning_startup",
        _GROUP_5_ANCM_NO,
        _GROUP_5_TITLE_INITIAL_SCALEUP,
        _GROUP_5_ANCM_NO,
        _GROUP_5_TITLE_PLANNING_STARTUP,
    ),
    # group 25 — 강원 sub-task 4건 (1-1, 1-2, 4-1, 5) 의 6 쌍 모두 분리.
    (
        "group_25_gangwon_1_1_vs_1_2",
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_1_1,
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_1_2,
    ),
    (
        "group_25_gangwon_1_1_vs_4_1",
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_1_1,
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_4_1,
    ),
    (
        "group_25_gangwon_1_1_vs_5",
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_1_1,
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_5,
    ),
    (
        "group_25_gangwon_1_2_vs_4_1",
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_1_2,
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_4_1,
    ),
    (
        "group_25_gangwon_1_2_vs_5",
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_1_2,
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_5,
    ),
    (
        "group_25_gangwon_4_1_vs_5",
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_4_1,
        _GROUP_25_ANCM_NO,
        _GROUP_25_TITLE_GANGWON_5,
    ),
]


@pytest.mark.parametrize(
    ("case_id", "ancm_no_a", "title_a", "ancm_no_b", "title_b"),
    DIFFERENT_KEY_CASES,
    ids=[case[0] for case in DIFFERENT_KEY_CASES],
)
def test_official_key_separates_same_ancm_no_with_different_titles(
    case_id: str,
    ancm_no_a: str,
    title_a: str,
    ancm_no_b: str,
    title_b: str,
) -> None:
    """동일 ancmNo + 다른 title 두 입력은 서로 다른 canonical_key 를 갖는다.

    분석 §1-2 / §3-2 가 'false-positive' 로 분류한 그룹들이 신규 합성 키
    하에서 정확히 분리되는지 회귀로 고정한다.
    """
    key_a = _key(ancm_no_a, title_a)
    key_b = _key(ancm_no_b, title_b)
    assert key_a != key_b, f"[{case_id}] expected different keys but both were {key_a!r}"


# ──────────────────────────────────────────────────────────────
# 4) fuzzy fallback 회귀 — official 후보 부재 시 동작 유지
# ──────────────────────────────────────────────────────────────


def test_fuzzy_fallback_when_no_official_candidates() -> None:
    """official_key_candidates 가 비어 있으면 fuzzy scheme 으로 폴백한다.

    합성 키 도입 후에도 fuzzy 분기는 변경되지 않아야 함을 고정한다.
    """
    result = compute_canonical_key(
        official_key_candidates=[],
        title="공식 번호 없는 공고",
        agency="과학기술정보통신부",
        deadline_at=None,
    )

    assert result.canonical_scheme == "fuzzy"
    assert result.canonical_key.startswith("fuzzy:")
    # fuzzy 키 본문은 ':' 로 구분된 (title:agency:year) 3 요소이므로
    # 'official:' 형태의 prefix 가 우연히 끼어들지 않았는지도 확인.
    assert not result.canonical_key.startswith("official:")


def test_fuzzy_fallback_when_only_blank_official_candidate() -> None:
    """공백/빈 문자열 official 후보는 무시되고 fuzzy 로 폴백한다.

    `_normalize_official_key` 가 공백만 있는 입력에 대해 빈 문자열을 돌려주므로
    loop 가 다음 후보로 넘어가야 하며, 후보가 모두 무효하면 fuzzy 로 떨어진다.
    """
    result = compute_canonical_key(
        official_key_candidates=["", "   "],
        title="공식 번호 없는 공고",
        agency="과학기술정보통신부",
        deadline_at=None,
    )

    assert result.canonical_scheme == "fuzzy"
