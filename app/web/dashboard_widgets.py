"""대시보드 사용자 라벨링 위젯 4종 빌더 (Phase 5b / task 00042-5).

배경 (사용자 원문):
    로그인한 사용자에게만 표시되는 4종 카운트 위젯을 단일 dict 로 묶어
    템플릿이 ``{% if widgets %}`` 한 번으로 렌더 / skip 을 결정할 수 있게 한다.
    비로그인 시는 라우트가 본 함수 자체를 호출하지 않아 위젯 영역 렌더 + 쿼리
    둘 다 건너뛴다 (검증 16 의 \"비로그인 시 위젯 쿼리 자체 skip\").

설계 근거 (``docs/dashboard_design.md``):
    - §8.1 4종 위젯 — (1) 전체 미확인 / (2) 전체 미판정 / (3) 기준일 한정 미확인 /
      (4) 기준일 한정 미판정.
    - §8.2 헬퍼 4종 — repository.py 의 ``count_unread_announcements_for_user`` /
      ``count_unjudged_canonical_for_user`` /
      ``count_unread_in_announcement_ids`` / ``count_unjudged_in_canonical_ids``.
    - §8.4 단위 구분 — 읽음=announcement (1·3), 관련성=canonical (2·4).

본 모듈은 4 호출을 한 함수로 묶고 결과를 frozen dataclass 로 반환한다 — 라우트
가 순서나 인자명을 잘못 매핑할 위험을 줄이고, 템플릿 측에서는 dict-style 접근만
하면 된다.

API 표면:
    - :class:`DashboardWidgetsData` — 4종 카운트 + 라벨 텍스트.
    - :func:`build_user_label_widgets` — 단일 진입점.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.repository import (
    count_unjudged_canonical_for_user,
    count_unjudged_in_canonical_ids,
    count_unread_announcements_for_user,
    count_unread_in_announcement_ids,
)


# ──────────────────────────────────────────────────────────────
# 라벨 — 사용자 원문 그대로
# ──────────────────────────────────────────────────────────────


WIDGET_LABEL_UNREAD_TOTAL: str = "전체 미확인 공고"
WIDGET_LABEL_UNJUDGED_TOTAL: str = "전체 미판정 관련성"
WIDGET_LABEL_UNREAD_IN_RANGE: str = "기준일 변경 공고 중 내 미확인"
WIDGET_LABEL_UNJUDGED_IN_RANGE: str = "기준일 변경 공고 중 내 미판정"


# ──────────────────────────────────────────────────────────────
# Public dataclass — UI 가 소비하는 형태
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DashboardWidgetsData:
    """대시보드 사용자 라벨링 위젯 4종 카운트 + 라벨 묶음.

    Attributes:
        unread_total_count:        위젯 1 — 전체 미확인 공고 수.
        unjudged_total_count:      위젯 2 — 전체 미판정 canonical 수.
        unread_in_range_count:     위젯 3 — 기준일 변경 공고 중 미확인 수.
        unjudged_in_range_count:   위젯 4 — 기준일 변경 공고 중 미판정 수.
        unread_total_label:        위젯 1 라벨 텍스트 (사용자 원문 그대로).
        unjudged_total_label:      위젯 2 라벨.
        unread_in_range_label:     위젯 3 라벨.
        unjudged_in_range_label:   위젯 4 라벨.

    카운트 4종은 별개의 단일 SELECT 쿼리로 산출되며, 본 dataclass 자체는 표시
    재구성에만 관여한다 (Python side 추가 집계 없음).
    """

    unread_total_count: int
    unjudged_total_count: int
    unread_in_range_count: int
    unjudged_in_range_count: int
    unread_total_label: str = WIDGET_LABEL_UNREAD_TOTAL
    unjudged_total_label: str = WIDGET_LABEL_UNJUDGED_TOTAL
    unread_in_range_label: str = WIDGET_LABEL_UNREAD_IN_RANGE
    unjudged_in_range_label: str = WIDGET_LABEL_UNJUDGED_IN_RANGE


# ──────────────────────────────────────────────────────────────
# 빌더 — 단일 진입점
# ──────────────────────────────────────────────────────────────


def build_user_label_widgets(
    session: Session,
    *,
    user_id: int,
    announcement_ids: Iterable[int],
    canonical_ids: Iterable[int],
) -> DashboardWidgetsData:
    """로그인 사용자의 4종 라벨링 위젯 카운트를 한 번에 산출한다.

    호출 순서 (총 4 SELECT):
        (1) ``count_unread_announcements_for_user(user_id)``
        (2) ``count_unjudged_canonical_for_user(user_id)``
        (3) ``count_unread_in_announcement_ids(user_id, announcement_ids)``
        (4) ``count_unjudged_in_canonical_ids(user_id, canonical_ids)``

    위젯 3·4 의 입력은 A 섹션 빌더 (``app.web.dashboard_section_a.build_section_a``)
    가 산출한 ``SectionAData.merged_announcement_ids`` /
    ``merged_canonical_group_ids`` 를 그대로 사용한다 — A 섹션이 이미 한 번의
    IN 쿼리로 announcement 메타를 fetch 했으므로, 위젯 3·4 가 같은 ID 들에 대해
    별도 announcement fetch 없이 사용자 단위 카운트만 한 번 더 SELECT 한다
    (사용자 원문 검증 15 \"announcement_ids 한 번의 IN 쿼리\").

    호출 규약:
        - 비로그인 (current_user is None) 경로는 본 함수를 호출하지 않는다 —
          라우트가 분기에서 None 체크 후 4 호출을 모두 skip 한다 (검증 16).
        - announcement_ids / canonical_ids 가 비어 있어도 본 함수는 그대로
          호출 가능 — 헬퍼 3·4 가 빈 리스트를 받아 쿼리 없이 0 을 반환한다.

    Args:
        session:          호출자 세션.
        user_id:          인증된 사용자 PK.
        announcement_ids: A 섹션 머지 결과 announcement_id union.
        canonical_ids:    A 섹션 머지 결과 announcement 의 canonical_group_id set.

    Returns:
        ``DashboardWidgetsData`` — 4종 카운트 + 라벨.
    """
    unread_total_count = count_unread_announcements_for_user(session, user_id=user_id)
    unjudged_total_count = count_unjudged_canonical_for_user(session, user_id=user_id)
    unread_in_range_count = count_unread_in_announcement_ids(
        session, user_id=user_id, announcement_ids=announcement_ids
    )
    unjudged_in_range_count = count_unjudged_in_canonical_ids(
        session, user_id=user_id, canonical_ids=canonical_ids
    )
    return DashboardWidgetsData(
        unread_total_count=unread_total_count,
        unjudged_total_count=unjudged_total_count,
        unread_in_range_count=unread_in_range_count,
        unjudged_in_range_count=unjudged_in_range_count,
    )


__all__ = [
    "DashboardWidgetsData",
    "WIDGET_LABEL_UNJUDGED_IN_RANGE",
    "WIDGET_LABEL_UNJUDGED_TOTAL",
    "WIDGET_LABEL_UNREAD_IN_RANGE",
    "WIDGET_LABEL_UNREAD_TOTAL",
    "build_user_label_widgets",
]
