"""ScrapeSnapshot.payload 의 5종 카테고리 머지 헬퍼 (Phase 5a / task 00041).

설계 근거: ``docs/snapshot_pipeline_design.md`` §9·§10.

본 모듈은 **순수 함수 (DB 의존 없음)** 만 노출한다 — DB session 이 필요한
``upsert_scrape_snapshot`` 은 ``app/db/repository.py`` 가 담당하고, 이 모듈은
JSON dict 만 다룬다. 단독 모듈로 분리한 이유:

- 머지 규칙(검증 4·5·6·7) 을 유닛 테스트가 1줄 import 로 검증할 수 있게.
- 5b 의 dashboard view 가 머지 결과 형식만 알면 되도록 책임을 좁힌다.
- apply_delta_to_main 트랜잭션 안의 ScrapeSnapshot UPSERT 가 동일 입력 dict
  로 호출될 수 있도록 ``DeltaApplyResult → payload dict`` 변환도 함께 둔다.

공개 API:
    - :data:`CATEGORY_NEW`, :data:`CATEGORY_CONTENT_CHANGED`,
      :data:`TRANSITION_TO_LABELS` — 카테고리 키 상수 (사용자 원문 그대로).
    - :func:`build_snapshot_payload` — DeltaApplyResult 를 §10 구조의 dict 로 변환.
    - :func:`merge_snapshot_payload` — 기존 payload + 새 payload 머지.
    - :func:`normalize_payload` — 누락된 카테고리를 빈 컨테이너로 채운 정규형 반환.
"""

from __future__ import annotations

from typing import Any

from app.db.models import AnnouncementStatus

# ──────────────────────────────────────────────────────────────
# 카테고리 키 상수 — 사용자 원문 / 설계 §10.1 그대로
# ──────────────────────────────────────────────────────────────

# (a) created — 그날 처음 본 테이블에 INSERT 된 announcement_id 배열.
CATEGORY_NEW: str = "new"

# (d) new_version (1차) 또는 2차 감지(첨부 변경) → reapply 된 announcement_id 배열.
CATEGORY_CONTENT_CHANGED: str = "content_changed"

# (c) status_transitioned 분기에서 발생한 status 단독 전이의 to 별 카테고리 키.
# - 키 형태: ``transitioned_to_<한글 status>``.
# - 한글이 키에 들어가지만 JSON 직렬화에서 ASCII 가 아닐 뿐, dict 키로 정상 동작한다.
TRANSITION_TO_LABELS: tuple[str, ...] = (
    AnnouncementStatus.SCHEDULED.value,  # "접수예정"
    AnnouncementStatus.RECEIVING.value,  # "접수중"
    AnnouncementStatus.CLOSED.value,     # "마감"
)


def _transition_key(to_label: str) -> str:
    """``to_label`` 한글을 ``transitioned_to_<to_label>`` 키로 만든다."""
    return f"transitioned_to_{to_label}"


# 5종 카테고리 정규형 키 전수 (정렬 / 검증 / 빈 카테고리 채우기에 사용).
# new + content_changed + 3개 transition.
_ALL_CATEGORY_KEYS: tuple[str, ...] = (
    CATEGORY_NEW,
    CATEGORY_CONTENT_CHANGED,
    *(_transition_key(label) for label in TRANSITION_TO_LABELS),
)


# ──────────────────────────────────────────────────────────────
# 정규화 / DeltaApplyResult → payload 변환
# ──────────────────────────────────────────────────────────────


def normalize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """payload dict 를 §10.1 정규형으로 채워서 반환한다.

    누락된 5종 카테고리는 빈 list 로, ``counts`` 도 5종 0 으로 채운다. ID 리스트와
    transition 항목은 깊게 복사하여 반환 dict 가 입력을 mutate 하지 않도록 한다.

    호출 의도:
        - merge / upsert 함수가 입력 dict 의 결손 키를 신경쓰지 않게 한다.
        - 신규 INSERT 시에도 빈 카테고리가 명시적으로 들어가 5b 의 view 가
          `KeyError` 없이 0 건을 표시할 수 있게 한다.

    Args:
        payload: 기존 ScrapeSnapshot.payload (또는 None — 빈 dict 와 동치).

    Returns:
        §10.1 구조의 새 dict — 5종 카테고리 + counts 가 모두 채워져 있다.

    Raises:
        ValueError: 알려지지 않은 카테고리 키가 들어 있을 때 (오타 보호).
        ValueError: transition 항목에 ``id`` / ``from`` 키가 없을 때.
    """
    raw = dict(payload or {})

    # 알려지지 않은 top-level 키는 깊은 검증 전에 빠르게 거른다 (counts 는 별도 처리).
    allowed_top_keys = set(_ALL_CATEGORY_KEYS) | {"counts"}
    unknown_keys = set(raw.keys()) - allowed_top_keys
    if unknown_keys:
        raise ValueError(
            "snapshot payload 에 알 수 없는 카테고리 키가 있습니다: "
            f"{sorted(unknown_keys)}. 허용 키: {sorted(allowed_top_keys)}"
        )

    normalized: dict[str, Any] = {}

    # new / content_changed: int[] — 깊은 복사 + asc 정렬.
    for plain_key in (CATEGORY_NEW, CATEGORY_CONTENT_CHANGED):
        ids = raw.get(plain_key) or []
        # 정렬은 머지 결과의 재현성을 보장한다 (같은 입력 → 같은 dict).
        normalized[plain_key] = sorted(int(value) for value in ids)

    # transition 3종: [{id, from}, ...] — id / from 키 검증 + 깊은 복사 + asc 정렬.
    for to_label in TRANSITION_TO_LABELS:
        key = _transition_key(to_label)
        entries = raw.get(key) or []
        validated: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or "id" not in entry or "from" not in entry:
                raise ValueError(
                    f"transition 카테고리 {key!r} 의 항목에 id/from 키가 누락되었습니다: "
                    f"entry={entry!r}"
                )
            validated.append(
                {"id": int(entry["id"]), "from": str(entry["from"])}
            )
        validated.sort(key=lambda record: record["id"])
        normalized[key] = validated

    # counts 는 정규형 배열 길이에서 재계산한다 (입력 counts 는 무시 — 설계 §9.4).
    normalized["counts"] = _build_counts(normalized)

    return normalized


def _build_counts(payload: dict[str, Any]) -> dict[str, int]:
    """5종 카테고리의 길이를 1:1 로 반영한 counts dict 를 만든다."""
    return {key: len(payload[key]) for key in _ALL_CATEGORY_KEYS}


def build_snapshot_payload(apply_result: Any) -> dict[str, Any]:
    """``DeltaApplyResult`` 를 §10.1 구조의 payload dict 로 변환한다.

    apply_delta_to_main 의 한 번 실행 결과만 다룬다 (머지가 아닌 단일 ScrapeRun
    payload 생성). transition 분류는 apply_result.transitions 가 이미 (c)
    분기 — status 단독 전이 — 만 포함하므로 그대로 사용한다.

    구조 (설계 §10.1):
        - new: int[] (asc 정렬)
        - content_changed: int[] (asc 정렬)
        - transitioned_to_접수예정/접수중/마감: [{id, from}, ...] (asc 정렬)
        - counts: 5종 길이를 그대로 반영

    호출 의도:
        - apply 트랜잭션 안에서 ``upsert_scrape_snapshot`` 의 ``new_payload``
          인자로 그대로 전달된다.
        - DeltaApplyResult 의 dataclass 의존을 본 모듈 안에 가두어, 5b 가
          payload dict 만 보고도 같은 형식을 재구성할 수 있게 한다.

    Args:
        apply_result: ``app.db.repository.DeltaApplyResult`` 인스턴스. duck-typing
                      으로 받기 때문에 ``new_announcement_ids`` /
                      ``content_changed_announcement_ids`` /
                      ``transitions`` 속성만 노출하면 동작한다.

    Returns:
        §10.1 정규형 dict — 5종 카테고리 + counts. 빈 ScrapeRun 도 빈 배열로
        구성된 정규형을 반환한다 (설계 §10.3).
    """
    payload: dict[str, Any] = {
        CATEGORY_NEW: sorted(int(announcement_id) for announcement_id in apply_result.new_announcement_ids),
        CATEGORY_CONTENT_CHANGED: sorted(
            int(announcement_id)
            for announcement_id in apply_result.content_changed_announcement_ids
        ),
    }

    # transition 3종 카테고리: to_label 별 분배 + asc 정렬.
    by_to_label: dict[str, list[dict[str, Any]]] = {
        _transition_key(label): [] for label in TRANSITION_TO_LABELS
    }
    for transition in apply_result.transitions:
        # apply_delta_to_main 가 채워주는 status_to 는 한글 3종이라 키가 정확히 매칭된다.
        # 혹시 비정상 값이 들어와도 by_to_label 가 빈 dict 라 KeyError 로 명시적 실패.
        to_key = _transition_key(transition.status_to)
        if to_key not in by_to_label:
            raise ValueError(
                "TransitionRecord.status_to 가 허용 라벨이 아닙니다: "
                f"{transition.status_to!r} (announcement_id={transition.announcement_id})"
            )
        by_to_label[to_key].append(
            {"id": int(transition.announcement_id), "from": str(transition.status_from)}
        )

    for to_key, entries in by_to_label.items():
        entries.sort(key=lambda record: record["id"])
        payload[to_key] = entries

    payload["counts"] = _build_counts(payload)
    return payload


# ──────────────────────────────────────────────────────────────
# merge_snapshot_payload — 5종 카테고리 머지 (사용자 원문 머지 규칙)
# ──────────────────────────────────────────────────────────────


def merge_snapshot_payload(
    existing: dict[str, Any] | None,
    new: dict[str, Any] | None,
) -> dict[str, Any]:
    """기존 snapshot.payload 와 이번 ScrapeRun 의 새 payload 를 머지한다.

    사용자 원문 머지 규칙 (설계 §9.3·§9.4):
        - ``new``: ID set union → asc 정렬.
        - ``content_changed``: ID set union → asc 정렬.
        - ``transitioned_to_X`` (3종): announcement_id 단위로 통합 머지 —
          첫 from 유지 + 마지막 to 갱신. 최종 from == to 면 전이 자체를 제거.
        - ``counts``: 머지 후 5종 배열 길이로 재계산 (입력 counts 무시).

    검증 4 (접수예정→접수중→마감) 시나리오:
        existing.transitioned_to_접수중 = [{77, '접수예정'}]
        new.transitioned_to_마감 = [{77, '접수중'}]
        → 결과 transitioned_to_마감 = [{77, '접수예정'}], 접수중 에서 77 제거.

    검증 5 (접수중→마감→접수중 정정) 시나리오:
        existing.transitioned_to_마감 = [{99, '접수중'}]
        new.transitioned_to_접수중 = [{99, '마감'}]
        → 머지 후 from='접수중' to='접수중' → 둘 다에서 99 제거.

    검증 6 (신규 + 전이 동시) 시나리오:
        existing.new = [101]
        new.transitioned_to_마감 = [{101, '접수중'}]
        → 결과 new=[101], transitioned_to_마감=[{101,'접수중'}] 둘 다 유지.

    순수 함수: ``existing`` 과 ``new`` 모두 mutate 하지 않는다. 새 dict 를 반환한다.

    Args:
        existing: 기존 ScrapeSnapshot.payload (또는 None).
        new:      이번 ScrapeRun 의 ``build_snapshot_payload`` 결과 (또는 None).

    Returns:
        머지된 정규형 payload dict (§10.1 그대로).

    Raises:
        ValueError: 카테고리 키 / transition entry 가 정의를 벗어나는 경우
                    (``normalize_payload`` 가 raise 한다).
    """
    existing_norm = normalize_payload(existing)
    new_norm = normalize_payload(new)

    merged: dict[str, Any] = {}

    # (1) new — set union + asc.
    merged[CATEGORY_NEW] = sorted(
        set(existing_norm[CATEGORY_NEW]) | set(new_norm[CATEGORY_NEW])
    )

    # (2) content_changed — set union + asc.
    merged[CATEGORY_CONTENT_CHANGED] = sorted(
        set(existing_norm[CATEGORY_CONTENT_CHANGED]) | set(new_norm[CATEGORY_CONTENT_CHANGED])
    )

    # (3) transition 3종 통합 머지.
    transition_buckets = _merge_transitions(existing_norm, new_norm)
    for key, entries in transition_buckets.items():
        merged[key] = entries

    # (4) counts 재계산.
    merged["counts"] = _build_counts(merged)

    return merged


def _merge_transitions(
    existing_norm: dict[str, Any],
    new_norm: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """3개 transitioned_to_X 카테고리를 announcement_id 단위로 통합 머지한다.

    설계 §9.4 (3) 의 의사 코드를 그대로 코드화한다:

        ① existing 항목으로 by_id 채움 (id, from, to).
        ② new 항목으로 갱신 — 같은 id 가 있으면 first from 유지 + last to 갱신.
        ③ from == to 인 항목 제거 (실질 변화 없음).
        ④ to 별로 다시 분배 + asc 정렬.

    입력은 ``normalize_payload`` 를 통과한 정규형이라고 가정한다 — entry 의
    id / from 키는 이미 검증되어 있다.

    Args:
        existing_norm: 정규화된 기존 payload.
        new_norm:      정규화된 새 payload.

    Returns:
        ``{transition_key: [{id, from}, ...]}`` 형태의 dict (3 카테고리 모두 포함).
        from == to 가 발생한 announcement_id 는 어떤 카테고리에도 들어가지 않는다.
    """
    # ① / ② — by_id[announcement_id] = {"id", "from", "to"}.
    # existing 우선 적재 → new 가 같은 id 를 만나면 from 은 유지 + to 만 갱신
    # (사용자 원문: "첫 from 유지 + 마지막 to 갱신").
    by_id: dict[int, dict[str, Any]] = {}

    for to_label in TRANSITION_TO_LABELS:
        for entry in existing_norm[_transition_key(to_label)]:
            by_id[entry["id"]] = {
                "id": entry["id"],
                "from": entry["from"],
                "to": to_label,
            }

    for to_label in TRANSITION_TO_LABELS:
        for entry in new_norm[_transition_key(to_label)]:
            announcement_id = entry["id"]
            if announcement_id in by_id:
                # 이미 머지된 entry 가 있으면 last to 갱신만 적용한다 — first from 유지.
                by_id[announcement_id]["to"] = to_label
            else:
                by_id[announcement_id] = {
                    "id": announcement_id,
                    "from": entry["from"],
                    "to": to_label,
                }

    # ③ from == to 제거 — 실질 변화 없는 정정 케이스 (검증 5).
    purged = {
        announcement_id: record
        for announcement_id, record in by_id.items()
        if record["from"] != record["to"]
    }

    # ④ to 별 재분배 + asc 정렬.
    output: dict[str, list[dict[str, Any]]] = {
        _transition_key(label): [] for label in TRANSITION_TO_LABELS
    }
    for record in purged.values():
        target_key = _transition_key(record["to"])
        output[target_key].append(
            {"id": record["id"], "from": record["from"]}
        )
    for entries in output.values():
        entries.sort(key=lambda record: record["id"])
    return output


__all__ = [
    "CATEGORY_NEW",
    "CATEGORY_CONTENT_CHANGED",
    "TRANSITION_TO_LABELS",
    "build_snapshot_payload",
    "merge_snapshot_payload",
    "normalize_payload",
]
