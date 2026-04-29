"""app.web.routes.admin._summarize_source_counts 단위 테스트 (task 00045).

관리자 [수집 제어] 탭의 최근 실행 이력 카드가 ``ScrapeRun.source_counts`` 를
스크래퍼 로그 마지막 줄과 같은 5-segment 요약으로 표시하도록 직렬화 단계에서
사전 계산하는데, 그 계산을 담당하는 헬퍼의 표시 정합성/폴백 동작을 검증한다.

핵심 시나리오:
    1. 새 schema (``collection`` + ``apply``) full row → 5 segment 정확 출력.
    2. apply 가 실행되지 않은 row (apply.executed=False) → segment 5개 모두
       정상 출력하되 apply 통계는 0 으로.
    3. running row (``active_sources`` 만 존재) → 빈 리스트 (셀 비움).
    4. 빈/None source_counts → 빈 리스트.
    5. 누락 키가 있는 옛 row → raise 하지 않고 0 으로 폴백.

직접 ScrapeRun ORM 을 거치지 않고 dict 단위로 호출 가능한 순수 함수라
DB fixture 가 필요 없다. tests/db/conftest 의 인메모리 SQLite 패턴은
건드리지 않는다.
"""

from __future__ import annotations

from app.web.routes.admin import _summarize_source_counts


# ──────────────────────────────────────────────────────────────
# 정상 — 새 schema (_build_final_source_counts 실제 출력 형태)
# ──────────────────────────────────────────────────────────────


def test_summarize_full_new_schema_returns_five_segments() -> None:
    """사용자 원문 예시값(성공 52/실패 0/...)을 그대로 segment 로 재현한다."""
    source_counts = {
        "active_sources": ["IRIS"],
        "collection": {
            "delta_inserted": 52,
            "delta_failed": 0,
            "detail_success": 3,
            "detail_failure": 0,
            "skipped_detail": 49,
            "attachment_download_success": 6,
            "attachment_download_failure": 0,
        },
        "apply": {
            "executed": True,
            "delta_announcement_count": 52,
            "action_counts": {
                "created": 2,
                "unchanged": 49,
                "new_version": 0,
                "status_transitioned": 1,
            },
            "new_announcement_ids": [],
            "content_changed_announcement_ids": [],
            "transition_count": 1,
            "attachment_success": 6,
            "attachment_skipped": 0,
            "attachment_content_change": 0,
        },
        "failed_source_announcement_ids": [],
    }

    segments = _summarize_source_counts(source_counts)

    assert segments == [
        "성공 52건 / 실패 0건",
        "상세 성공 3건 / 실패 0건 / 생략(unchanged peek) 49건",
        "첨부 다운로드 성공 6건 / 실패 0건",
        "apply action 분포: 신규=2 변경없음=49 버전갱신=0 상태전이=1",
        "apply 2차 감지(첨부 변경)=0건",
    ]


def test_summarize_apply_not_executed_falls_back_to_zero() -> None:
    """apply 가 실행되지 않은 finalize 경로(예: cancelled/orchestrator-failed).

    _build_final_source_counts 는 apply 섹션을 executed=False + action_counts={}
    + 0-값으로 채워둔다. segment 5개는 그대로 출력되되 apply 쪽 숫자는 0.
    """
    source_counts = {
        "active_sources": ["IRIS"],
        "collection": {
            "delta_inserted": 10,
            "delta_failed": 1,
            "detail_success": 9,
            "detail_failure": 1,
            "skipped_detail": 0,
            "attachment_download_success": 4,
            "attachment_download_failure": 1,
        },
        "apply": {
            "executed": False,
            "delta_announcement_count": 0,
            "action_counts": {},
            "new_announcement_ids": [],
            "content_changed_announcement_ids": [],
            "transition_count": 0,
            "attachment_success": 0,
            "attachment_skipped": 0,
            "attachment_content_change": 0,
        },
        "failed_source_announcement_ids": [42],
    }

    segments = _summarize_source_counts(source_counts)

    assert segments == [
        "성공 10건 / 실패 1건",
        "상세 성공 9건 / 실패 1건 / 생략(unchanged peek) 0건",
        "첨부 다운로드 성공 4건 / 실패 1건",
        "apply action 분포: 신규=0 변경없음=0 버전갱신=0 상태전이=0",
        "apply 2차 감지(첨부 변경)=0건",
    ]


# ──────────────────────────────────────────────────────────────
# 빈 표시 — running row / None / 빈 dict
# ──────────────────────────────────────────────────────────────


def test_summarize_running_row_returns_empty_list() -> None:
    """running row 의 source_counts 는 active_sources 만 있다 — 빈 리스트.

    아직 finalize 되지 않은 row 에 대해 셀에 0 0 0 을 박아 두면 사용자가
    완료된 것으로 오해할 수 있어, segment 자체를 비워 [수집 제어] 탭이
    아무것도 그리지 않도록 한다.
    """
    source_counts = {"active_sources": ["IRIS"]}
    assert _summarize_source_counts(source_counts) == []


def test_summarize_empty_dict_returns_empty_list() -> None:
    """빈 dict 면 표시할 정보가 없으므로 빈 리스트."""
    assert _summarize_source_counts({}) == []


def test_summarize_legacy_row_without_collection_or_apply_returns_empty() -> None:
    """옛 schema(collection/apply 가 모두 없는 row) 도 빈 리스트로 폴백.

    구체적인 옛 schema 형태가 무엇이든 — totals 같은 무관한 키만 있는 row 든 —
    표시할 새 정보가 없으므로 셀을 비우는 편이 잘못된 0 표시를 보여주는 것보다
    안전하다.
    """
    source_counts = {"totals": {"success": 5, "failure": 0}}
    assert _summarize_source_counts(source_counts) == []


# ──────────────────────────────────────────────────────────────
# 부분 누락 — raise 금지 + 0 폴백
# ──────────────────────────────────────────────────────────────


def test_summarize_partial_collection_only_falls_back_to_zero_on_missing_keys() -> None:
    """collection 만 있고 apply 가 비어 있어도 raise 하지 않고 0 으로 폴백."""
    source_counts = {
        "collection": {
            # delta_inserted 만 있고 나머지 키는 없다 — 0 으로 폴백되어야 한다.
            "delta_inserted": 7,
        },
        "apply": {},
    }

    segments = _summarize_source_counts(source_counts)

    assert segments == [
        "성공 7건 / 실패 0건",
        "상세 성공 0건 / 실패 0건 / 생략(unchanged peek) 0건",
        "첨부 다운로드 성공 0건 / 실패 0건",
        "apply action 분포: 신규=0 변경없음=0 버전갱신=0 상태전이=0",
        "apply 2차 감지(첨부 변경)=0건",
    ]


def test_summarize_handles_non_int_count_values_without_raising() -> None:
    """카운트 자리에 None/문자열/float 가 들어 있어도 raise 하지 않는다.

    옛 row 가 손상돼 있을 수 있다는 가정 — UI 가 통째로 500 이 나는 것보다
    의미 있는 표시(0 폴백) 가 낫다.
    """
    source_counts = {
        "collection": {
            "delta_inserted": "12",       # str — int 환산 가능
            "delta_failed": None,          # None
            "detail_success": 3.0,         # float
            "detail_failure": "오류",       # int 환산 불가
            "skipped_detail": True,        # bool (int 의 서브클래스)
            "attachment_download_success": 0,
            "attachment_download_failure": 0,
        },
        "apply": {
            "action_counts": "이상한값",     # dict 가 아님 — 무시되어 0 폴백
            "attachment_content_change": 0,
        },
    }

    segments = _summarize_source_counts(source_counts)

    # 타입 안전 변환: 문자열 → int, None → 0, float → int, bool → 0/1.
    assert segments[0] == "성공 12건 / 실패 0건"
    assert segments[1] == "상세 성공 3건 / 실패 0건 / 생략(unchanged peek) 1건"
    # action_counts 가 dict 가 아니면 모두 0 으로 폴백.
    assert segments[3] == "apply action 분포: 신규=0 변경없음=0 버전갱신=0 상태전이=0"
