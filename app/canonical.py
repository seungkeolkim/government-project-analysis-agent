"""공고 canonical key 산출 유틸리티.

동일 과제가 IRIS·NTIS 등 여러 포털에 중복 등록되는 경우를 하나의 canonical_project 로
묶기 위해 canonical_key 를 계산한다.

우선 순위:
1. official 키  — 정부가 공식 발급한 공고번호(ancmNo 등) **정규화 결과 + 공고명 정규화 결과**
                  의 합성 키. 동일 공고번호 아래 서로 다른 세부 공고가 게시되는 false-positive
                  사례(`docs/duplicate_detection_analysis.md` §1-2) 를 분리하기 위함.
                  여러 소스가 동일한 공고번호를 공유하므로 소스 접두사 없이 'official:' 만 사용.
2. fuzzy 키    — 공식 번호가 없을 때 제목·주관기관·마감연도 조합으로 근사 식별.

반환 타입 `CanonicalKeyResult` 는 frozen dataclass 이므로 해시·비교가 가능하다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

# ──────────────────────────────────────────────────────────────
# 공개 타입
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CanonicalKeyResult:
    """canonical_key 계산 결과.

    Attributes:
        canonical_key:    DB 에 저장할 canonical key 문자열.
                          예) 'official:산업통상부공고제2026-300호::2026년도제조암묵지기반AI모델개발사업신규지원대상과제공고'
                              'fuzzy:인공지능핵심원천기술개발:과학기술정보통신부:2026'
        canonical_scheme: 사용된 방식. 'official' 또는 'fuzzy'.
    """

    canonical_key: str
    canonical_scheme: str  # 'official' | 'fuzzy'


# ──────────────────────────────────────────────────────────────
# 공개 함수
# ──────────────────────────────────────────────────────────────


def compute_canonical_key(
    *,
    official_key_candidates: list[str],
    title: str,
    agency: str | None,
    deadline_at: datetime | None,
) -> CanonicalKeyResult:
    """공고 메타로부터 canonical_key 와 scheme 을 산출한다.

    Args:
        official_key_candidates: 공식 공고번호 후보 목록(예: [ancmNo]).
                                 소스별로 추출할 수 있는 값을 그대로 넘긴다.
                                 NTIS 처럼 구조적 필드가 없으면 빈 리스트([])를 넘긴다.
        title:       공고 제목. official 합성 키와 fuzzy 키 모두에서 정규화하여 사용한다.
        agency:      주관기관명. None 이면 빈 문자열로 대체.
        deadline_at: 마감 시각(timezone-aware UTC). None 이면 연도를 '0000' 으로 처리.

    Returns:
        CanonicalKeyResult — canonical_key 와 canonical_scheme 을 담은 frozen dataclass.
    """
    for candidate in official_key_candidates:
        normalized_ancm_no = _normalize_official_key(candidate)
        if normalized_ancm_no:
            # '::' 구분자 — ancmNo 정규화 결과는 단일 콜론 ':' 을 포함하지 않으므로 충돌이 없다
            # (분석 §8-2 명시).
            normalized_title = _normalize_official_title(title)
            return CanonicalKeyResult(
                canonical_key=f"official:{normalized_ancm_no}::{normalized_title}",
                canonical_scheme="official",
            )

    fuzzy = _build_fuzzy_key(title=title, agency=agency, deadline_at=deadline_at)
    return CanonicalKeyResult(canonical_key=f"fuzzy:{fuzzy}", canonical_scheme="fuzzy")


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────

# 공공기관 법인격 접미사 — 주관기관 정규화 시 제거 대상
_AGENCY_SUFFIXES = re.compile(
    r"(재단법인|사단법인|주식회사|유한회사|합자회사|합명회사|농업회사법인|영농조합법인)\s*",
)

# fuzzy 제목에서 제거할 문자: 공백·구두점·특수문자
_FUZZY_STRIP = re.compile(r"[\s\W]+", re.UNICODE)

# fuzzy 제목 최대 길이 (자모 기준 — 충분한 식별력 + 키 크기 제한)
_FUZZY_TITLE_MAX_LEN = 50

# NTIS 통합공고가 제목 말미에 부착하는 사업명 suffix 패턴.
# 예) '... 신규과제 재공모 _(2026)2026년도 대형가속기정책센터 신규과제 재공모'
# leading `\s*` 가 매칭되어야 하므로 NFKC 직후, 공백 전체 제거 이전 단계에 적용한다.
#
# task 00151 변경 (2026-05-28):
#   원래 _normalize_official_title 이 이 패턴을 무조건 절단하여 canonical_key 본문
#   에서 suffix 부분을 제거했다. 그러나 NTIS 가 동일 ancmNo 아래 서로 다른
#   sub-business 공고를 별도 row 로 게시하는 케이스(ann 173/174 — ancmNo
#   2026-0627호 아래 '국가전략기술미래소재기술개발(미래소재)' vs
#   '글로벌공급망첨단소재기술개발-나노커넥트')에서는 suffix 가 sub-business 식별자
#   역할을 하므로 절단하면 서로 다른 공고가 같은 canonical_key 로 잘못 묶인다.
#
#   현재 canonical_key 합성에서는 이 패턴을 **절단하지 않는다** — full title 을
#   그대로 사용해 sub-business 별로 분리되는 결정론적 키를 만든다. cross-source
#   매칭(IRIS title prefix + NTIS title with suffix 가 동일 과제인 케이스)은
#   `app/db/repository.py::_apply_canonical` 의 fallback 매칭 분기가 담당한다.
#   여기서는 fallback 매칭의 prefix 비교 헬퍼(`strip_ntis_business_suffix`) 가
#   여전히 이 정규식을 사용하므로 정의 자체는 보존한다.
_NTIS_TITLE_SUFFIX = re.compile(r"\s*_\([0-9]{4}\).*$")


def _normalize_official_key(raw_key: str) -> str:
    """공식 공고번호를 정규화한다.

    NFKC 유니코드 정규화 후 모든 공백 문자를 제거한다.
    빈 문자열이거나 공백만 있으면 빈 문자열을 반환한다.

    Args:
        raw_key: 원본 공고번호 문자열.

    Returns:
        정규화된 문자열. 유효하지 않으면 빈 문자열.
    """
    if not raw_key or not raw_key.strip():
        return ""
    normalized = unicodedata.normalize("NFKC", raw_key)
    return re.sub(r"\s+", "", normalized)


def _normalize_official_title(title: str) -> str:
    """official canonical_key 합성에 쓰는 공고명 정규화.

    처리 순서:
    1. NFKC 유니코드 정규화 — 전각/반각, 합성 한글 등 표기 변이 흡수.
    2. 모든 공백 문자 제거 — leading/trailing whitespace, 단어 사이 공백 변이 흡수.

    task 00151 변경 (2026-05-28):
      이전 버전은 NFKC 직후 `_NTIS_TITLE_SUFFIX` 절단 단계를 두어 NTIS 통합공고가
      말미에 부착하는 `_(YYYY)<사업명>` 부분을 일괄 제거했다. 그러나 동일 ancmNo
      아래 NTIS 가 서로 다른 sub-business 공고를 별개 row 로 게시하는 케이스(173/174)
      에서는 그 suffix 가 sub-business 식별자라 잘못 합쳐졌다. 이제 canonical_key
      는 sub-business 별 분리를 위해 full title 을 보존하고, 동일 과제의 IRIS↔NTIS
      cross-source 묶음 유지는 `_apply_canonical` 의 fallback 매칭이 담당한다.

    빈 문자열/None 입력은 빈 문자열을 반환한다 (호출 측에서 이 경우
    동일 ancmNo 끼리는 묶이는 결과가 됨 — 사실상 현행 official 키와 동일 동작).

    Args:
        title: 원본 공고 제목.

    Returns:
        정규화된 제목 문자열. 정확일치 비교를 전제로 하므로 길이 제한은 두지 않는다.
    """
    if not title:
        return ""
    nfkc = unicodedata.normalize("NFKC", title)
    return re.sub(r"\s+", "", nfkc)


def strip_ntis_business_suffix(title: str) -> str:
    """NTIS 통합공고 title 말미의 `_(YYYY)<사업명>` suffix 를 절단한 정규화 결과를 반환한다.

    fallback cross-source 매칭(`app/db/repository.py::_apply_canonical`)에서
    IRIS title 과 NTIS title 의 prefix 동치성 비교에 사용한다. 본 함수는
    canonical_key 생성에는 더 이상 관여하지 않으며(태스크 00151 이후) 매칭 전용
    헬퍼이다.

    처리 순서:
      1. NFKC 정규화
      2. `_NTIS_TITLE_SUFFIX` 매칭 시 suffix 절단
      3. 공백 전체 제거

    빈 문자열/None 입력은 빈 문자열을 반환한다.

    Args:
        title: 원본 공고 제목.

    Returns:
        suffix 절단·공백 제거를 거친 정규화 문자열. cross-source 매칭에서
        IRIS title 의 결과와 같으면 같은 과제로 본다.
    """
    if not title:
        return ""
    nfkc = unicodedata.normalize("NFKC", title)
    without_ntis_suffix = _NTIS_TITLE_SUFFIX.sub("", nfkc)
    return re.sub(r"\s+", "", without_ntis_suffix)


def _build_fuzzy_key(
    *,
    title: str,
    agency: str | None,
    deadline_at: datetime | None,
) -> str:
    """fuzzy 키 본문을 조립한다.

    형식: ``{normalized_title}:{normalized_agency}:{deadline_year}``

    Args:
        title:       공고 제목.
        agency:      주관기관명. None 이면 빈 문자열.
        deadline_at: 마감 시각. None 이면 연도를 '0000' 으로 처리.

    Returns:
        콜론으로 구분된 세 요소를 이어 붙인 문자열.
    """
    normalized_title = _normalize_fuzzy_title(title)
    normalized_agency = _normalize_agency(agency)
    deadline_year = str(deadline_at.year) if deadline_at else "0000"
    return f"{normalized_title}:{normalized_agency}:{deadline_year}"


def _normalize_fuzzy_title(title: str) -> str:
    """제목을 fuzzy 키용으로 정규화한다.

    NFKC 정규화 → 공백·구두점·특수문자 제거 → 앞 50자 취득.
    """
    nfkc = unicodedata.normalize("NFKC", title or "")
    stripped = _FUZZY_STRIP.sub("", nfkc)
    return stripped[:_FUZZY_TITLE_MAX_LEN]


def _normalize_agency(agency: str | None) -> str:
    """주관기관명을 fuzzy 키용으로 정규화한다.

    법인격 접미사 제거 → NFKC → 공백·구두점 제거.
    None 이면 빈 문자열을 반환한다.
    """
    if not agency:
        return ""
    without_suffix = _AGENCY_SUFFIXES.sub("", agency)
    nfkc = unicodedata.normalize("NFKC", without_suffix)
    return _FUZZY_STRIP.sub("", nfkc)


__all__ = ["CanonicalKeyResult", "compute_canonical_key", "strip_ntis_business_suffix"]
