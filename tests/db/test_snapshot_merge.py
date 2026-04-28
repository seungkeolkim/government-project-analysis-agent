"""snapshot.payload 5종 카테고리 머지 룰 유닛 테스트 (Phase 5a / task 00041-4).

검증 대상: ``app/db/snapshot.py`` 의 ``merge_snapshot_payload`` /
``build_snapshot_payload`` / ``normalize_payload`` 순수 함수.

설계 문서 ``docs/snapshot_pipeline_design.md`` §9.5 의 E1 ~ E10 엣지 케이스를
1:1 로 매핑한 테스트다. 사용자 원문의 검증 1·3·4·5·6·7 시나리오를 직접 인용한다.

규약:
    - DB session 의존 없음 — 순수 dict 입력 / 출력 확인.
    - 입력 dict 는 mutate 되지 않아야 한다 (순수 함수).
    - 결과의 ID 리스트는 announcement_id asc 정렬 (재현 가능성).
    - counts 는 5종 배열 길이를 1:1 로 반영 (입력 counts 무시).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import pytest

from app.db.snapshot import (
    CATEGORY_CONTENT_CHANGED,
    CATEGORY_NEW,
    TRANSITION_TO_LABELS,
    build_snapshot_payload,
    merge_snapshot_payload,
    normalize_payload,
)


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────


def _empty_payload() -> dict[str, Any]:
    """5종 카테고리가 모두 비어 있는 정규형 payload 를 반환한다."""
    return {
        CATEGORY_NEW: [],
        CATEGORY_CONTENT_CHANGED: [],
        "transitioned_to_접수예정": [],
        "transitioned_to_접수중": [],
        "transitioned_to_마감": [],
        "counts": {
            CATEGORY_NEW: 0,
            CATEGORY_CONTENT_CHANGED: 0,
            "transitioned_to_접수예정": 0,
            "transitioned_to_접수중": 0,
            "transitioned_to_마감": 0,
        },
    }


# build_snapshot_payload 입력용 가짜 DeltaApplyResult — duck-typing.
@dataclass
class _FakeTransition:
    announcement_id: int
    status_from: str
    status_to: str


@dataclass
class _FakeApplyResult:
    new_announcement_ids: list[int]
    content_changed_announcement_ids: list[int]
    transitions: list[_FakeTransition]


# ──────────────────────────────────────────────────────────────
# normalize_payload — 정규화 + 깊은 복사 + counts 재계산
# ──────────────────────────────────────────────────────────────


def test_normalize_empty_payload_fills_categories() -> None:
    """None 또는 {} 입력에 대해 5종 카테고리 + counts 가 0 으로 채워진다."""
    norm_none = normalize_payload(None)
    norm_empty = normalize_payload({})
    expected = _empty_payload()
    assert norm_none == expected
    assert norm_empty == expected


def test_normalize_does_not_mutate_input() -> None:
    """순수 함수: 입력 dict 가 변경되지 않는다."""
    given = {CATEGORY_NEW: [3, 1, 2]}
    snapshot_before = copy.deepcopy(given)
    normalize_payload(given)
    assert given == snapshot_before


def test_normalize_sorts_id_lists_ascending() -> None:
    """new / content_changed 의 ID 리스트가 asc 로 정렬된다 (재현 가능성)."""
    given = {
        CATEGORY_NEW: [42, 1, 10],
        CATEGORY_CONTENT_CHANGED: [99, 5],
    }
    norm = normalize_payload(given)
    assert norm[CATEGORY_NEW] == [1, 10, 42]
    assert norm[CATEGORY_CONTENT_CHANGED] == [5, 99]


def test_normalize_recomputes_counts_from_array_lengths() -> None:
    """counts 입력이 거짓이라도 배열 길이로 재계산된다 (truth source = 배열)."""
    given = {
        CATEGORY_NEW: [1, 2, 3],
        # 의도적으로 거짓 counts 를 넣어 본다.
        "counts": {
            CATEGORY_NEW: 999,
            CATEGORY_CONTENT_CHANGED: 999,
            "transitioned_to_접수예정": 999,
            "transitioned_to_접수중": 999,
            "transitioned_to_마감": 999,
        },
    }
    norm = normalize_payload(given)
    assert norm["counts"][CATEGORY_NEW] == 3
    assert norm["counts"][CATEGORY_CONTENT_CHANGED] == 0


def test_normalize_rejects_unknown_category_key() -> None:
    """오타 / 변경 카테고리 키가 들어오면 ValueError 로 빠르게 실패한다."""
    given = {"transitioned_to_invalid": []}
    with pytest.raises(ValueError, match="알 수 없는 카테고리 키"):
        normalize_payload(given)


def test_normalize_rejects_transition_entry_without_id() -> None:
    """transition 항목에 id 키가 빠지면 ValueError."""
    given = {"transitioned_to_접수중": [{"from": "접수예정"}]}
    with pytest.raises(ValueError, match="id/from 키가 누락"):
        normalize_payload(given)


def test_normalize_rejects_transition_entry_without_from() -> None:
    """transition 항목에 from 키가 빠지면 ValueError."""
    given = {"transitioned_to_접수중": [{"id": 1}]}
    with pytest.raises(ValueError, match="id/from 키가 누락"):
        normalize_payload(given)


def test_normalize_sorts_transition_entries_by_id() -> None:
    """transition entry 도 announcement_id asc 정렬."""
    given = {
        "transitioned_to_마감": [
            {"id": 7, "from": "접수중"},
            {"id": 1, "from": "접수예정"},
            {"id": 4, "from": "접수중"},
        ],
    }
    norm = normalize_payload(given)
    assert [entry["id"] for entry in norm["transitioned_to_마감"]] == [1, 4, 7]


# ──────────────────────────────────────────────────────────────
# merge_snapshot_payload — 사용자 원문 머지 규칙 (E1~E10)
# ──────────────────────────────────────────────────────────────


def test_merge_e1_single_run_creates_new_only() -> None:
    """E1: 단일 ScrapeRun 정상 종료 — new=[42], 나머지 빈 배열 (검증 1)."""
    new_payload = {CATEGORY_NEW: [42]}
    merged = merge_snapshot_payload(None, new_payload)

    assert merged[CATEGORY_NEW] == [42]
    assert merged[CATEGORY_CONTENT_CHANGED] == []
    for label in TRANSITION_TO_LABELS:
        assert merged[f"transitioned_to_{label}"] == []
    assert merged["counts"][CATEGORY_NEW] == 1
    assert merged["counts"]["transitioned_to_마감"] == 0


def test_merge_e2_same_day_two_runs_set_union_for_new() -> None:
    """E2: 같은 날 다른 공고 신규 — new 가 set union (검증 3)."""
    run1 = {CATEGORY_NEW: [42]}
    run2 = {CATEGORY_NEW: [43]}
    merged = merge_snapshot_payload(run1, run2)
    assert merged[CATEGORY_NEW] == [42, 43]
    assert merged["counts"][CATEGORY_NEW] == 2


def test_merge_e2_set_union_dedupes_overlapping_ids() -> None:
    """E2 보강: new 카테고리에서 중복 ID 가 있어도 set union 으로 중복 제거."""
    run1 = {CATEGORY_NEW: [10, 20]}
    run2 = {CATEGORY_NEW: [20, 30]}
    merged = merge_snapshot_payload(run1, run2)
    assert merged[CATEGORY_NEW] == [10, 20, 30]


def test_merge_e4_3_step_transition_keeps_first_from_last_to() -> None:
    """E4 (검증 4): 접수예정→접수중→마감.

    run1: t_접수중=[{77,'접수예정'}]
    run2: t_마감=[{77,'접수중'}]
    → 결과 t_마감=[{77,'접수예정'}], t_접수중 에서 77 제거.
    """
    run1 = {"transitioned_to_접수중": [{"id": 77, "from": "접수예정"}]}
    run2 = {"transitioned_to_마감": [{"id": 77, "from": "접수중"}]}
    merged = merge_snapshot_payload(run1, run2)

    assert merged["transitioned_to_접수중"] == []
    assert merged["transitioned_to_마감"] == [{"id": 77, "from": "접수예정"}]
    assert merged["transitioned_to_접수예정"] == []
    assert merged["counts"]["transitioned_to_마감"] == 1
    assert merged["counts"]["transitioned_to_접수중"] == 0


def test_merge_e5_status_correction_drops_to_no_change() -> None:
    """E5 (검증 5): 접수중→마감→접수중 정정 — from==to 면 둘 다에서 제거.

    run1: t_마감=[{99,'접수중'}]
    run2: t_접수중=[{99,'마감'}]
    → 머지 후 from='접수중' to='접수중' 이므로 항목 제거.
    """
    run1 = {"transitioned_to_마감": [{"id": 99, "from": "접수중"}]}
    run2 = {"transitioned_to_접수중": [{"id": 99, "from": "마감"}]}
    merged = merge_snapshot_payload(run1, run2)

    for label in TRANSITION_TO_LABELS:
        assert merged[f"transitioned_to_{label}"] == [], (
            f"99 가 transitioned_to_{label} 에 남아 있으면 안 된다: "
            f"{merged[f'transitioned_to_{label}']}"
        )
    # counts 도 모두 0.
    for label in TRANSITION_TO_LABELS:
        assert merged["counts"][f"transitioned_to_{label}"] == 0


def test_merge_e6_new_plus_transition_keeps_both() -> None:
    """E6 (검증 6): 신규 + 전이 동시 — 둘 다 유지.

    run1: new=[101]
    run2: t_마감=[{101,'접수중'}]
    """
    run1 = {CATEGORY_NEW: [101]}
    run2 = {"transitioned_to_마감": [{"id": 101, "from": "접수중"}]}
    merged = merge_snapshot_payload(run1, run2)

    assert merged[CATEGORY_NEW] == [101]
    assert merged["transitioned_to_마감"] == [{"id": 101, "from": "접수중"}]


def test_merge_e7_attachment_only_change_goes_to_content_changed() -> None:
    """E7 (검증 7): 첨부만 변경 — content_changed 에만, transition 빈 배열."""
    run1 = {CATEGORY_CONTENT_CHANGED: [250]}
    merged = merge_snapshot_payload(None, run1)
    assert merged[CATEGORY_CONTENT_CHANGED] == [250]
    for label in TRANSITION_TO_LABELS:
        assert merged[f"transitioned_to_{label}"] == []


def test_merge_e8_repeated_content_changed_id_unioned() -> None:
    """E8: 같은 공고가 같은 날 (d) → 또 (d) — content_changed set union."""
    run1 = {CATEGORY_CONTENT_CHANGED: [500]}
    run2 = {CATEGORY_CONTENT_CHANGED: [500]}
    merged = merge_snapshot_payload(run1, run2)
    assert merged[CATEGORY_CONTENT_CHANGED] == [500]


def test_merge_e9_new_then_content_changed_keeps_both() -> None:
    """E9: 같은 날 (a) → 다른 ScrapeRun 의 (d) — 둘 다 유지.

    run1.new=[777], run2.content_changed=[777] → 둘 다 박힘.
    5b 의 표시 측에서 disjoint set view 는 본 task 범위 밖.
    """
    run1 = {CATEGORY_NEW: [777]}
    run2 = {CATEGORY_CONTENT_CHANGED: [777]}
    merged = merge_snapshot_payload(run1, run2)
    assert merged[CATEGORY_NEW] == [777]
    assert merged[CATEGORY_CONTENT_CHANGED] == [777]


def test_merge_e10_three_hop_transition_chain() -> None:
    """E10: 3-hop 이상 전이 — 접수예정→접수중→마감→접수중.

    단계별 머지로 확인:
        ① t_접수중=[{1,'접수예정'}]
        ② t_마감=[{1,'접수예정'}]  (first from 유지)
        ③ from='접수예정' to='접수중' → t_접수중=[{1,'접수예정'}], t_마감 비움.
    """
    step1 = merge_snapshot_payload(
        None,
        {"transitioned_to_접수중": [{"id": 1, "from": "접수예정"}]},
    )
    step2 = merge_snapshot_payload(
        step1,
        {"transitioned_to_마감": [{"id": 1, "from": "접수중"}]},
    )
    step3 = merge_snapshot_payload(
        step2,
        {"transitioned_to_접수중": [{"id": 1, "from": "마감"}]},
    )
    # 최종 결과: t_접수중 에 from='접수예정' 으로 1 이 남고, t_마감 에서는 사라짐.
    assert step3["transitioned_to_접수중"] == [{"id": 1, "from": "접수예정"}]
    assert step3["transitioned_to_마감"] == []
    assert step3["transitioned_to_접수예정"] == []
    assert step3["counts"]["transitioned_to_접수중"] == 1
    assert step3["counts"]["transitioned_to_마감"] == 0


# ──────────────────────────────────────────────────────────────
# 입력 보호 / counts 재계산 / 빈 ScrapeRun
# ──────────────────────────────────────────────────────────────


def test_merge_does_not_mutate_inputs() -> None:
    """순수 함수: existing 과 new 둘 다 mutate 되지 않는다."""
    existing = {CATEGORY_NEW: [10], "transitioned_to_마감": [{"id": 5, "from": "접수중"}]}
    new = {CATEGORY_NEW: [20], "transitioned_to_접수중": [{"id": 5, "from": "마감"}]}
    existing_snapshot = copy.deepcopy(existing)
    new_snapshot = copy.deepcopy(new)

    merge_snapshot_payload(existing, new)

    assert existing == existing_snapshot
    assert new == new_snapshot


def test_merge_recomputes_counts_correctly_after_transition_purge() -> None:
    """from==to 제거 후 counts 가 0 으로 재계산되어야 한다 (truth source = 배열)."""
    run1 = {"transitioned_to_마감": [{"id": 99, "from": "접수중"}]}
    run2 = {"transitioned_to_접수중": [{"id": 99, "from": "마감"}]}
    merged = merge_snapshot_payload(run1, run2)
    for label in TRANSITION_TO_LABELS:
        assert merged["counts"][f"transitioned_to_{label}"] == 0


def test_merge_empty_new_returns_existing_normalized() -> None:
    """new 가 비어 있어도 머지는 빈 카테고리를 채운 정규형을 반환한다."""
    existing = {CATEGORY_NEW: [1, 2]}
    merged = merge_snapshot_payload(existing, None)
    assert merged[CATEGORY_NEW] == [1, 2]
    assert merged[CATEGORY_CONTENT_CHANGED] == []
    assert merged["counts"][CATEGORY_NEW] == 2


# ──────────────────────────────────────────────────────────────
# build_snapshot_payload — DeltaApplyResult → §10 dict 변환
# ──────────────────────────────────────────────────────────────


def test_build_payload_categorizes_apply_result_correctly() -> None:
    """apply_result 의 5종 분류가 그대로 카테고리 키로 흘러간다."""
    fake = _FakeApplyResult(
        new_announcement_ids=[3, 1, 2],
        content_changed_announcement_ids=[10],
        transitions=[
            _FakeTransition(announcement_id=100, status_from="접수예정", status_to="접수중"),
            _FakeTransition(announcement_id=200, status_from="접수중", status_to="마감"),
            _FakeTransition(announcement_id=50, status_from="접수예정", status_to="접수중"),
        ],
    )
    payload = build_snapshot_payload(fake)

    assert payload[CATEGORY_NEW] == [1, 2, 3]
    assert payload[CATEGORY_CONTENT_CHANGED] == [10]
    assert payload["transitioned_to_접수중"] == [
        {"id": 50, "from": "접수예정"},
        {"id": 100, "from": "접수예정"},
    ]
    assert payload["transitioned_to_마감"] == [
        {"id": 200, "from": "접수중"},
    ]
    assert payload["transitioned_to_접수예정"] == []
    assert payload["counts"][CATEGORY_NEW] == 3
    assert payload["counts"]["transitioned_to_접수중"] == 2
    assert payload["counts"]["transitioned_to_마감"] == 1


def test_build_payload_empty_apply_result_yields_normalized_empty() -> None:
    """빈 apply_result 도 정규형(빈 배열 + counts=0) 으로 반환된다 (설계 §10.3)."""
    empty = _FakeApplyResult(
        new_announcement_ids=[],
        content_changed_announcement_ids=[],
        transitions=[],
    )
    payload = build_snapshot_payload(empty)
    assert payload == _empty_payload()


def test_build_payload_rejects_invalid_status_to() -> None:
    """TransitionRecord.status_to 가 허용 라벨(접수예정/접수중/마감) 이 아니면 ValueError."""
    fake = _FakeApplyResult(
        new_announcement_ids=[],
        content_changed_announcement_ids=[],
        transitions=[
            _FakeTransition(announcement_id=1, status_from="접수중", status_to="알수없음"),
        ],
    )
    with pytest.raises(ValueError, match="status_to"):
        build_snapshot_payload(fake)


def test_build_payload_then_merge_for_two_consecutive_runs() -> None:
    """E6 통합: build → merge 가 같은 날 두 ScrapeRun 결과를 보존한다.

    run1: 신규 등록 (created)
    run2: 같은 announcement 에 대한 status 전이 (status_transitioned)
    → snapshot.new 에 id 유지 + transitioned_to_마감 에도 id 유지 (검증 6).
    """
    run1_payload = build_snapshot_payload(
        _FakeApplyResult(
            new_announcement_ids=[101],
            content_changed_announcement_ids=[],
            transitions=[],
        )
    )
    run2_payload = build_snapshot_payload(
        _FakeApplyResult(
            new_announcement_ids=[],
            content_changed_announcement_ids=[],
            transitions=[
                _FakeTransition(
                    announcement_id=101, status_from="접수중", status_to="마감"
                ),
            ],
        )
    )
    merged = merge_snapshot_payload(run1_payload, run2_payload)
    assert merged[CATEGORY_NEW] == [101]
    assert merged["transitioned_to_마감"] == [{"id": 101, "from": "접수중"}]
