"""NTIS 원문 공고 상태 라벨 → AnnouncementStatus 3분류 정규화 모듈.

탐사 결과(docs/ntis_site_exploration.md §2)에서 확정된 NTIS 상태 라벨:
    - 접수예정  (searchStatusList 코드: P)
    - 접수중    (searchStatusList 코드: B)
    - 마감      (searchStatusList 코드: Y)

이 세 값은 AnnouncementStatus 의 값과 1:1 대응한다.
따라서 매핑은 identity 이지만, 수집 시 발생할 수 있는 비-표준 공백·전각문자
혼입을 방어하기 위해 unicodedata.normalize('NFKC', …) + strip() 을 선적용한다.

원문 상태 라벨은 호출 측에서 raw_metadata 에 보존하는 책임을 진다.
이 모듈은 '정규화된 AnnouncementStatus 반환'만 담당한다.
"""

from __future__ import annotations

import unicodedata

from app.db.models import AnnouncementStatus

# ──────────────────────────────────────────────────────────────
# NTIS 원문 상태 라벨 상수 (00014-1 탐사 실측 기준)
# ──────────────────────────────────────────────────────────────

# searchStatusList 코드 P — 목록 페이지 <td>에 표시되는 한글 원문
NTIS_STATUS_RAW_SCHEDULED: str = "접수예정"

# searchStatusList 코드 B
NTIS_STATUS_RAW_RECEIVING: str = "접수중"

# searchStatusList 코드 Y
NTIS_STATUS_RAW_CLOSED: str = "마감"

# 정규화 후 원문 라벨 → AnnouncementStatus 매핑 테이블
# 키는 NFKC + strip 적용 후 값이므로 전각 문자 혼입 등에도 대응된다.
_NTIS_RAW_LABEL_TO_STATUS: dict[str, AnnouncementStatus] = {
    NTIS_STATUS_RAW_SCHEDULED: AnnouncementStatus.SCHEDULED,
    NTIS_STATUS_RAW_RECEIVING: AnnouncementStatus.RECEIVING,
    NTIS_STATUS_RAW_CLOSED: AnnouncementStatus.CLOSED,
}


def normalize_ntis_status(raw_label: str) -> AnnouncementStatus:
    """NTIS 목록 페이지의 원문 상태 라벨을 AnnouncementStatus 로 변환한다.

    처리 순서:
        1. unicodedata.normalize('NFKC', …) — 전각 문자·nbsp 등 통일
        2. str.strip() — 앞뒤 공백 제거
        3. 매핑 테이블 조회 → AnnouncementStatus 반환

    탐사에서 확인된 라벨은 '접수예정' / '접수중' / '마감' 3종뿐이며,
    이는 AnnouncementStatus 값과 완전히 일치한다.

    Args:
        raw_label: NTIS 목록 페이지 현황 셀에서 추출한 원문 문자열.
                   예: '접수중', '  마감 ', '接受中'(전각 혼입 가정).

    Returns:
        대응하는 AnnouncementStatus 멤버.

    Raises:
        ValueError: raw_label 이 알려진 3분류에 속하지 않는 경우.
                    이 경우 수집 파이프라인에서 해당 공고를 스킵해야 한다.
    """
    # NFKC 정규화: 전각 → 반각, nbsp → 공백 등 변환
    normalized = unicodedata.normalize("NFKC", raw_label).strip()

    status = _NTIS_RAW_LABEL_TO_STATUS.get(normalized)
    if status is None:
        known = list(_NTIS_RAW_LABEL_TO_STATUS.keys())
        raise ValueError(
            f"알 수 없는 NTIS 공고 상태 라벨: {raw_label!r} "
            f"(NFKC 정규화 후: {normalized!r}). "
            f"알려진 라벨: {known}"
        )
    return status


__all__ = [
    "normalize_ntis_status",
    "NTIS_STATUS_RAW_SCHEDULED",
    "NTIS_STATUS_RAW_RECEIVING",
    "NTIS_STATUS_RAW_CLOSED",
]
