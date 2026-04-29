"""대시보드 A 섹션 — (from, to] 누적 머지 + 카테고리 카드 + expand 빌더 (Phase 5b / task 00042-3).

배경 (사용자 원문):
    A 섹션은 ``(from, to]`` 구간 안의 모든 ScrapeSnapshot 을 시간순 누적 머지해
    5종 카테고리 카드 + 클릭 expand 리스트로 보여 준다. 머지 함수는 신규 작성
    금지 — Phase 5a 의 ``app.db.snapshot.merge_snapshot_payload`` 를 reduce 패턴
    으로 그대로 재사용 (사용자 원문 주의사항 \"신규 머지 함수 금지\").

설계 근거 (``docs/dashboard_design.md``):
    - §5.2 reduce 의사 코드 — ``reduce(merge_snapshot_payload, payloads, None)``.
    - §5.3 카운트 정합성 — 카드의 카운트는 머지 결과 ``payload.counts`` 합산으로
      읽는다 (ID 리스트 길이가 아님 — 회귀 가드).
    - §6.1 카드 표시 형식 — \"기준일 N건 ↑/↓ X (비교일 M건 대비)\". guidance 의
      \"비교일 단일 snapshot 의 counts vs 누적 머지 후 counts 의 차이로 계산\"
      에 따라 ``N = 누적 머지 후 counts``, ``M = 비교일 단일 snapshot.counts``
      로 해석한다.
    - §6.2 expand 표시 형식 — 행 형식 (소스 / status / 제목 / 마감일 + 전이 from
      / 내용 변경 중복 배지) 그대로.
    - §4.2 fallback — 비교일이 가용 set 에 없으면 ``find_nearest_previous_snapshot_date``
      로 가장 가까운 이전 일자로 대체 + 안내문. 이전 snapshot 전무면 \"데이터
      없음\".

API 표면:
    - :class:`SectionACategoryCard` — 카드 1개의 표시 데이터 (카운트 + 증감 +
      expand items).
    - :class:`SectionAFallback` — fallback 안내문 dict (UI 가 그대로 렌더).
    - :class:`SectionAData` — 라우트가 템플릿에 전달하는 dict 표현의 dataclass.
    - :func:`build_section_a` — A 섹션의 모든 데이터를 조립하는 단일 진입점.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from functools import reduce
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Announcement, ScrapeSnapshot
from app.db.repository import (
    find_nearest_previous_snapshot_date,
    get_scrape_snapshot_by_date,
    list_announcements_by_ids,
    list_snapshots_in_range,
)
from app.db.snapshot import (
    CATEGORY_CONTENT_CHANGED,
    CATEGORY_NEW,
    TRANSITION_TO_LABELS,
    merge_snapshot_payload,
    normalize_payload,
)


# ──────────────────────────────────────────────────────────────
# 카테고리 표시 메타 (key, label, expand 헤더 prefix, 중복 배지 한글 어미)
# ──────────────────────────────────────────────────────────────


def _transition_key(to_label: str) -> str:
    """``app.db.snapshot._transition_key`` 와 동일한 키 형식 (private 미러).

    ``app.db.snapshot._transition_key`` 가 모듈 private 라 import 하지 않고,
    같은 형식 (``transitioned_to_<한글 status>``) 으로 미러한다. 본 모듈도
    이 키를 5종 카테고리 키 1:1 매핑에만 사용하므로 상수화 비용을 줄인다.
    """
    return f"transitioned_to_{to_label}"


# 5종 카테고리 키 + 라벨 — UI 카드 / expand 헤더에 그대로 노출.
# 순서가 사용자 원문 \"신규 / 내용 변경 / 전이 → 접수예정 / 전이 → 접수중 /
# 전이 → 마감\" 과 일치하도록 명시 정렬.
SECTION_A_CATEGORY_DESCRIPTORS: tuple[dict[str, str], ...] = (
    {
        "key": CATEGORY_NEW,
        "label": "신규",
        "duplicate_badge": "🆕 신규에도",
        "is_transition": "false",
    },
    {
        "key": CATEGORY_CONTENT_CHANGED,
        "label": "내용 변경",
        "duplicate_badge": "📝 내용 변경에도",
        "is_transition": "false",
    },
    {
        "key": _transition_key(TRANSITION_TO_LABELS[0]),  # 접수예정
        "label": f"전이 → {TRANSITION_TO_LABELS[0]}",
        "duplicate_badge": f"🔄 전이→{TRANSITION_TO_LABELS[0]}도",
        "is_transition": "true",
    },
    {
        "key": _transition_key(TRANSITION_TO_LABELS[1]),  # 접수중
        "label": f"전이 → {TRANSITION_TO_LABELS[1]}",
        "duplicate_badge": f"🔄 전이→{TRANSITION_TO_LABELS[1]}도",
        "is_transition": "true",
    },
    {
        "key": _transition_key(TRANSITION_TO_LABELS[2]),  # 마감
        "label": f"전이 → {TRANSITION_TO_LABELS[2]}",
        "duplicate_badge": f"🔄 전이→{TRANSITION_TO_LABELS[2]}도",
        "is_transition": "true",
    },
)


# ──────────────────────────────────────────────────────────────
# Public dataclass — UI 가 소비하는 형태
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SectionAExpandItem:
    """A 섹션 카테고리 expand 영역의 한 행.

    Attributes:
        announcement_id:    상세 링크용 PK.
        title:              공고 제목.
        source_type:        \"IRIS\" / \"NTIS\" 등 — 배지 표시.
        status_label:       현재 status 한글값 (접수중 / 접수예정 / 마감).
        agency:             주관 기관명 (없으면 None).
        deadline_at:        마감 시각 UTC tz-aware datetime — 템플릿이 ``kst_date``
                            필터로 표시.
        canonical_group_id: 위젯 4번 (canonical 미판정 카운트) 가 활용. None 가능.
        transition_from:    전이 카테고리 행에서만 의미 — \"X 에서\" 표기. plain
                            카테고리는 None.
        duplicate_badges:   내용 변경 행에서 같은 announcement 가 다른 카테고리
                            에도 등장하면 표시할 배지 텍스트 list. 중복 없으면
                            빈 list.
    """

    announcement_id: int
    title: str
    source_type: str
    status_label: str
    agency: str | None
    deadline_at: datetime | None
    canonical_group_id: int | None
    transition_from: str | None
    duplicate_badges: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SectionACategoryCard:
    """A 섹션 5종 카테고리 카드 1개의 표시 데이터.

    Attributes:
        category_key:    payload 카테고리 키 (`new` / `content_changed` / `transitioned_to_X`).
        category_label:  카드 헤더 한글 라벨.
        is_transition:   전이 카테고리 여부 (expand 행이 ``transition_from`` 을
                         가지는지 결정).
        base_count:      누적 머지 후 ``payload.counts[category_key]`` (= N).
        compare_count:   비교일 단일 snapshot.payload.counts[category_key] (= M).
                         비교일 effective snapshot 자체가 없으면 None — 카드 우측
                         의 \"비교일 M건 대비\" 영역은 \"—\" 로 표시.
        delta:           ``base_count - compare_count``. compare_count is None
                         이면 None.
        delta_direction: \"up\" (base > compare) / \"down\" (base < compare) /
                         \"flat\" (동일). compare 가 None 이면 None.
        items:           expand 영역에 렌더할 ``SectionAExpandItem`` list.
                         정렬: announcement.id 오름차순.
    """

    category_key: str
    category_label: str
    is_transition: bool
    base_count: int
    compare_count: int | None
    delta: int | None
    delta_direction: str | None
    items: list[SectionAExpandItem]


@dataclass(frozen=True)
class SectionAFallback:
    """비교일 fallback 안내문 데이터.

    Attributes:
        applied:                  fallback 발동 여부.
        message:                  UI 가 그대로 표시하는 한국어 안내문 (사용자
                                  원문 §4.3 (a) 또는 (b) 그대로). applied=False
                                  일 때는 빈 문자열.
        requested_compare_date:   사용자가 요청한 비교일 (또는 드롭다운 산출).
        effective_compare_date:   실제 사용된 snapshot_date (없으면 None →
                                  \"데이터 없음\" 분기).
        is_no_data:               비교일 이전 snapshot 전무 분기 — A 섹션은
                                  카드/expand 자체를 \"데이터 없음\" 으로 대체.
    """

    applied: bool
    message: str
    requested_compare_date: date
    effective_compare_date: date | None
    is_no_data: bool


@dataclass(frozen=True)
class SectionAData:
    """A 섹션 전체 컨텍스트 — ``dashboard.html`` 의 section_a placeholder 가 사용.

    Attributes:
        cards:                  5종 카테고리 카드 list (사용자 원문 순서 고정).
        fallback:               비교일 fallback 안내문 데이터.
        merged_announcement_ids: 5종 카테고리 ID 의 union (위젯 3·4 가
                                재사용). canonical_group_ids 도 본 list 의 메타
                                fetch 결과에서 도출되어 위젯 4 가 재사용한다.
        merged_canonical_group_ids: announcement_id 들의 ``canonical_group_id``
                                중 None 이 아닌 것의 set list.
    """

    cards: list[SectionACategoryCard]
    fallback: SectionAFallback
    merged_announcement_ids: list[int]
    merged_canonical_group_ids: list[int]


# ──────────────────────────────────────────────────────────────
# 빌더 — 단일 진입점
# ──────────────────────────────────────────────────────────────


def build_section_a(
    session: Session,
    *,
    base_date: date,
    requested_compare_date: date,
) -> SectionAData:
    """A 섹션의 모든 데이터를 조립한다 (라우트의 단일 진입점).

    호출 시점은 라우트 ``dashboard_page`` 가 ``(from, to)`` 산출을 마친 직후다.
    본 함수의 ``base_date`` / ``requested_compare_date`` 는 그 산출 결과의
    ``to_date`` / ``from_date`` 와 1:1 매핑된다.

    동작 순서 (``docs/dashboard_design.md §6.3``):
        1. ``requested_compare_date`` 의 snapshot 존재 여부 확인.
        2. 없으면 ``find_nearest_previous_snapshot_date`` 로 fallback. 직전
           snapshot 도 없으면 \"데이터 없음\" 분기.
        3. ``effective_compare_date`` 결정 후 ``(effective_compare_date, base_date]``
           구간의 snapshot list 를 ``list_snapshots_in_range`` 로 fetch.
        4. ``reduce(merge_snapshot_payload, payloads, None)`` 로 누적 머지 —
           merge 의 normalize_payload 가 None 입력을 빈 정규형으로 변환해 주므로
           초깃값을 None 으로 두면 첫 reduce step 이 ``merge(None, first)`` =
           ``normalize(first)`` 로 idempotent.
        5. 머지 결과 5종 카테고리 ID union → ``list_announcements_by_ids`` 1회.
        6. 카테고리별 카드 데이터 조립 (counts 비교 + expand items).

    Args:
        session:                호출자 세션.
        base_date:              기준일 (KST date) — ``to_date``.
        requested_compare_date: 사용자가 요청한 비교일 (KST date) — ``from_date``
                                의 산출 결과. 사용자 원문 \"(from, to] 구간\" 의
                                from 점이라 누적 머지 자체에는 포함되지 않지만,
                                fallback 안내문에는 \"요청된 비교일\" 표시용으로
                                들어간다.

    Returns:
        ``SectionAData`` — 카드 list + fallback + 위젯 재사용용 ID list.
    """
    # ── (1) (2) fallback 적용 — effective_compare_date 결정 ───────────────
    effective_compare_date, fallback_message, fallback_applied, is_no_data = (
        _resolve_effective_compare_date(
            session, requested_compare_date=requested_compare_date
        )
    )

    fallback = SectionAFallback(
        applied=fallback_applied,
        message=fallback_message,
        requested_compare_date=requested_compare_date,
        effective_compare_date=effective_compare_date,
        is_no_data=is_no_data,
    )

    # ── \"데이터 없음\" 분기 — A 섹션 카드를 모두 0 으로 채우고 expand 비움 ─
    if is_no_data:
        empty_cards = _build_empty_cards()
        return SectionAData(
            cards=empty_cards,
            fallback=fallback,
            merged_announcement_ids=[],
            merged_canonical_group_ids=[],
        )

    # ── (3) (from, to] 구간 snapshot list ─────────────────────────────────
    # effective_compare_date 가 보장되어 있다 (None 이면 위에서 is_no_data 분기로 빠짐).
    assert effective_compare_date is not None
    snapshots_in_range = list_snapshots_in_range(
        session,
        from_exclusive=effective_compare_date,
        to_inclusive=base_date,
    )

    # ── (4) reduce 누적 머지 ──────────────────────────────────────────────
    # merge_snapshot_payload 는 None 입력을 normalize_payload 로 빈 정규형으로
    # 만들어 주므로 reduce 초깃값을 None 으로 두면 첫 step 이 자연스럽게
    # \"merge(None, first) == normalize(first)\" 로 동작한다 (사용자 원문 검증
    # 14 \"단일 snapshot 비교와 일관\" 회귀 시나리오 정합).
    merged_payload: dict[str, Any] = reduce(
        merge_snapshot_payload,
        (snapshot.payload for snapshot in snapshots_in_range),
        normalize_payload(None),
    )

    # ── (5) ID union → announcements 한 번의 IN 쿼리 ─────────────────────
    announcement_id_union = _collect_announcement_id_union(merged_payload)
    announcement_meta_list = list_announcements_by_ids(
        session, announcement_ids=announcement_id_union
    )
    announcement_meta_map: dict[int, Announcement] = {
        ann.id: ann for ann in announcement_meta_list
    }

    # ── (6) 카테고리별 카드 + expand items 조립 ──────────────────────────
    compare_payload = _fetch_compare_payload(session, effective_compare_date)
    cards = _build_cards(
        merged_payload=merged_payload,
        compare_payload=compare_payload,
        announcement_meta_map=announcement_meta_map,
    )

    canonical_group_ids = sorted({
        ann.canonical_group_id
        for ann in announcement_meta_list
        if ann.canonical_group_id is not None
    })

    return SectionAData(
        cards=cards,
        fallback=fallback,
        merged_announcement_ids=sorted(announcement_id_union),
        merged_canonical_group_ids=canonical_group_ids,
    )


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────────────────────


def _resolve_effective_compare_date(
    session: Session,
    *,
    requested_compare_date: date,
) -> tuple[date | None, str, bool, bool]:
    """비교일 fallback 정책 (``docs/dashboard_design.md §4.2``) 을 적용한다.

    분기:
        (a) 요청된 비교일에 snapshot 이 있다 → 그대로 사용. fallback 미발동.
        (b) 없지만 그 이전 snapshot 이 있다 → 가장 가까운 이전 snapshot 사용 +
            안내문 발동.
        (c) 비교일 이전 snapshot 전무 → effective=None, is_no_data=True.

    Returns:
        ``(effective_compare_date, message, fallback_applied, is_no_data)``.
        fallback_applied 와 is_no_data 는 상호 배타 — fallback_applied=True
        는 (b), is_no_data=True 는 (c). 둘 다 False 는 (a).
    """
    requested_snapshot = get_scrape_snapshot_by_date(session, requested_compare_date)
    if requested_snapshot is not None:
        return requested_compare_date, "", False, False

    nearest_previous = find_nearest_previous_snapshot_date(
        session, target_date=requested_compare_date
    )
    if nearest_previous is None:
        # (c) 비교일 이전 snapshot 전무.
        return None, "", False, True

    # (b) fallback 발동 — 사용자 원문 §4.3 (a) 안내문 그대로.
    message = (
        f"비교일 {requested_compare_date.isoformat()} 일자 snapshot 이 없어 "
        f"{nearest_previous.isoformat()} 일자 snapshot 을 사용했습니다."
    )
    return nearest_previous, message, True, False


def _fetch_compare_payload(
    session: Session,
    effective_compare_date: date,
) -> dict[str, Any]:
    """비교일 단일 snapshot.payload 를 정규형으로 fetch.

    카드의 ``compare_count`` (M) 산출에 사용한다. 호출 시점에 effective_compare_date
    가 가용 set 에 들어 있음이 보장되므로 None 분기는 일어나지 않지만, 방어적
    으로 None 도 빈 정규형으로 정규화한다.
    """
    compare_snapshot = get_scrape_snapshot_by_date(session, effective_compare_date)
    if compare_snapshot is None:
        # 이론상 도달 불가 (resolve 함수가 None 을 걸러 줌). 방어적 정규형.
        return normalize_payload(None)
    return normalize_payload(compare_snapshot.payload)


def _collect_announcement_id_union(merged_payload: dict[str, Any]) -> set[int]:
    """5종 카테고리 ID 를 union 해 단일 set 으로 만든다.

    ``new`` / ``content_changed`` 는 int list, transition 3종은 ``[{id, from},
    ...]`` 형식이라 두 가지 모두 다룬다.
    """
    union: set[int] = set()
    union.update(merged_payload.get(CATEGORY_NEW, []))
    union.update(merged_payload.get(CATEGORY_CONTENT_CHANGED, []))
    for to_label in TRANSITION_TO_LABELS:
        for entry in merged_payload.get(_transition_key(to_label), []):
            # entry 는 {\"id\": int, \"from\": str} (정규형 보장).
            union.add(int(entry["id"]))
    return union


def _build_empty_cards() -> list[SectionACategoryCard]:
    """\"데이터 없음\" 분기에서 5종 카드를 모두 0 으로 채워서 반환한다.

    UI 측에서 카드 placeholder 를 안정적으로 그리되 카운트 / expand 가 비어
    있도록 한다 (사용자 원문 §4.3 (b) 의 \"A 섹션 데이터 없음\" 시각 처리).
    """
    cards: list[SectionACategoryCard] = []
    for descriptor in SECTION_A_CATEGORY_DESCRIPTORS:
        cards.append(
            SectionACategoryCard(
                category_key=descriptor["key"],
                category_label=descriptor["label"],
                is_transition=descriptor["is_transition"] == "true",
                base_count=0,
                compare_count=None,
                delta=None,
                delta_direction=None,
                items=[],
            )
        )
    return cards


def _build_cards(
    *,
    merged_payload: dict[str, Any],
    compare_payload: dict[str, Any],
    announcement_meta_map: dict[int, Announcement],
) -> list[SectionACategoryCard]:
    """5종 카테고리 카드 데이터를 조립한다.

    카운트 정합성 (사용자 원문 주의사항):
        \"카운트는 머지 결과의 ID 리스트 길이가 아닌 머지된 payload 의 counts
        합산\" — base_count 는 ``merged_payload[\"counts\"][category_key]`` 에서,
        compare_count 는 ``compare_payload[\"counts\"][category_key]`` 에서 직접
        가져온다. ID 리스트의 ``len(items)`` 으로 표시하지 않는다.

    중복 배지:
        내용 변경 행에 한해 같은 announcement_id 가 다른 카테고리에도 등장하면
        해당 카테고리의 ``duplicate_badge`` 텍스트를 추가한다 (사용자 원문 §6.2).
    """
    merged_counts = merged_payload.get("counts", {})
    compare_counts = compare_payload.get("counts", {})

    # 카테고리별 ID set — 중복 배지 산출에 사용.
    category_id_sets: dict[str, set[int]] = {}
    for descriptor in SECTION_A_CATEGORY_DESCRIPTORS:
        key = descriptor["key"]
        if descriptor["is_transition"] == "true":
            ids = {int(entry["id"]) for entry in merged_payload.get(key, [])}
        else:
            ids = {int(announcement_id) for announcement_id in merged_payload.get(key, [])}
        category_id_sets[key] = ids

    cards: list[SectionACategoryCard] = []
    for descriptor in SECTION_A_CATEGORY_DESCRIPTORS:
        key = descriptor["key"]
        is_transition = descriptor["is_transition"] == "true"

        base_count = int(merged_counts.get(key, 0))
        compare_count = int(compare_counts.get(key, 0)) if compare_counts else 0
        delta = base_count - compare_count
        if delta > 0:
            direction = "up"
        elif delta < 0:
            direction = "down"
        else:
            direction = "flat"

        items = _build_expand_items_for_category(
            descriptor=descriptor,
            merged_payload=merged_payload,
            announcement_meta_map=announcement_meta_map,
            category_id_sets=category_id_sets,
        )

        cards.append(
            SectionACategoryCard(
                category_key=key,
                category_label=descriptor["label"],
                is_transition=is_transition,
                base_count=base_count,
                compare_count=compare_count,
                delta=delta,
                delta_direction=direction,
                items=items,
            )
        )
    return cards


def _build_expand_items_for_category(
    *,
    descriptor: dict[str, str],
    merged_payload: dict[str, Any],
    announcement_meta_map: dict[int, Announcement],
    category_id_sets: dict[str, set[int]],
) -> list[SectionAExpandItem]:
    """카테고리 1개의 expand 행 list 를 만든다.

    ``new`` / ``content_changed`` 는 int list 를 그대로 행으로 변환하고,
    transition 3종은 entry 의 ``from`` 을 ``transition_from`` 필드에 그대로
    옮긴다. 메타가 ``announcement_meta_map`` 에 없는 경우 (DB 에서 삭제된
    announcement 등) 는 행을 건너뛴다 — 사용자 원문 §6.2 의 행 표시는 표시
    메타가 있어야 의미가 있어서, 메타 fetch 누락분은 자연스럽게 빠진다.
    """
    key = descriptor["key"]
    is_transition = descriptor["is_transition"] == "true"
    is_content_changed_card = key == CATEGORY_CONTENT_CHANGED

    items: list[SectionAExpandItem] = []
    if is_transition:
        # transition entry: [{id, from}, ...].
        for entry in merged_payload.get(key, []):
            announcement_id = int(entry["id"])
            announcement = announcement_meta_map.get(announcement_id)
            if announcement is None:
                continue
            items.append(
                SectionAExpandItem(
                    announcement_id=announcement.id,
                    title=announcement.title,
                    source_type=announcement.source_type,
                    status_label=announcement.status.value,
                    agency=announcement.agency,
                    deadline_at=announcement.deadline_at,
                    canonical_group_id=announcement.canonical_group_id,
                    transition_from=str(entry["from"]),
                    duplicate_badges=_collect_duplicate_badges(
                        announcement_id=announcement_id,
                        own_category_key=key,
                        category_id_sets=category_id_sets,
                    )
                    if is_content_changed_card  # 내용 변경 카드만 중복 배지 — transition 은 false 이므로 사실상 빈 list.
                    else [],
                )
            )
    else:
        for announcement_id in merged_payload.get(key, []):
            announcement = announcement_meta_map.get(int(announcement_id))
            if announcement is None:
                continue
            duplicate_badges = (
                _collect_duplicate_badges(
                    announcement_id=int(announcement_id),
                    own_category_key=key,
                    category_id_sets=category_id_sets,
                )
                if is_content_changed_card
                else []
            )
            items.append(
                SectionAExpandItem(
                    announcement_id=announcement.id,
                    title=announcement.title,
                    source_type=announcement.source_type,
                    status_label=announcement.status.value,
                    agency=announcement.agency,
                    deadline_at=announcement.deadline_at,
                    canonical_group_id=announcement.canonical_group_id,
                    transition_from=None,
                    duplicate_badges=duplicate_badges,
                )
            )
    # 표시 안정성을 위해 announcement_id 오름차순 정렬.
    items.sort(key=lambda item: item.announcement_id)
    return items


def _collect_duplicate_badges(
    *,
    announcement_id: int,
    own_category_key: str,
    category_id_sets: dict[str, set[int]],
) -> list[str]:
    """``announcement_id`` 가 자기 카테고리 외 어디에 또 등장하는지 배지 텍스트
    list 를 만든다.

    사용자 원문 §6.2: 내용 변경 행에서만 표시 — 본 함수는 호출 측에서 \"내용
    변경 카드일 때만 호출\" 가드되므로 own_category_key 는 사실상 ``content_changed``
    하나에 한정되지만, 일반화된 형태로 둬서 추후 다른 카테고리 중복 표시가
    필요해지면 호출 측만 바꾸면 된다.
    """
    badges: list[str] = []
    for descriptor in SECTION_A_CATEGORY_DESCRIPTORS:
        other_key = descriptor["key"]
        if other_key == own_category_key:
            continue
        if announcement_id in category_id_sets.get(other_key, set()):
            badges.append(descriptor["duplicate_badge"])
    return badges


__all__ = [
    "SECTION_A_CATEGORY_DESCRIPTORS",
    "SectionACategoryCard",
    "SectionAData",
    "SectionAExpandItem",
    "SectionAFallback",
    "build_section_a",
]
