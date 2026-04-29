"""사업공고 스크래퍼 오케스트레이터.

sources.yaml 의 scrape 섹션과 sources 목록을 읽어 공고 수집을 실행한다.
모든 실행 파라미터는 sources.yaml 과 .env 를 통해 제어하며, CLI 인자는 사용하지 않는다.

현재 구현 상태:
    - IRIS: 완전 구현 (list + detail + attachment 수집). 접수예정·접수중·마감 3개 상태 수집.
    - NTIS: stub (경고 로그 + 빈 결과 반환)

증분 수집 전략 (UpsertResult 기반):
    목록 UPSERT 후 `UpsertResult.needs_detail_scraping` 값으로 상세 수집 여부를 결정한다.
    - "created" / "new_version" / "status_transitioned": 항상 상세 수집
    - "unchanged" + detail 이미 있음: 상세 수집 생략 (기존 데이터 재사용)
    - "unchanged" + detail 없음: 상세 수집 진행

    첨부파일 수집은 상세 수집이 완료(또는 기존 detail_html 있음)된 공고에 대해 실행한다.
    이미 다운로드된 파일(로컬 FS 기준)은 sha256 만 계산하여 반환하고 재다운로드하지 않는다.

2차 변경 감지 (첨부 sha256 기반 — Phase 1a 추가):
    목록 UPSERT 시점에는 첨부 sha256 을 알 수 없으므로, 1차 감지는 기존 4필드만
    비교한다(title/status/deadline_at/agency). 첨부 다운로드 후 다시 signature 를
    비교해 '첨부 개수 / 기존 sha256 / 첨부 추가·삭제' 가 바뀌었는지 확인한다.
    2차 감지에서 변경이 확인되면 `reapply_version_with_reset` 으로 is_current 순환
    (구 row 봉인 + 신규 row INSERT) + 사용자 라벨링 리셋을 동일 트랜잭션에서 수행.

    발동 대상 분기:
        - 1차 action 이 'unchanged' 또는 'status_transitioned' 인 경로에서만 발동.
        - 'created' / 'new_version'(1차) 경로는 2차 감지를 건너뛴다 — 1차 감지가
          이미 버전 분기를 처리했고, 방금 INSERT 된 신규 row 는 첨부가 0 개라
          비교 기준(기존 첨부 세트)이 존재하지 않아 무조건 '변경' 판정이 되어
          row 중복이 생긴다.

    추가 가드:
        다운로드 실패가 한 건이라도 있으면 스킵(false-positive 방지). dry_run /
        skip_attachments 경로는 애초에 첨부 단계를 거치지 않으므로 트리거되지 않는다.

상태 전이 동작:
    동일 공고가 접수예정→접수중→마감 으로 상태가 바뀌면 status_transitioned 분기가 발동한다.
    상태만 변경된 경우 in-place UPDATE, title/deadline_at/agency 도 함께 바뀐 경우 이력 보존
    (기존 row 봉인 + 신규 INSERT). docs/status_transition_todo.md 참고.

실행 설정 (sources.yaml 의 scrape: 섹션):
    active_sources: 실행할 소스 ID 목록. 비어 있으면 enabled=true 소스 전체 실행.
    max_pages:       소스당 최대 페이지 수 (null → 소스별 설정 → 코드 default 10).
    max_announcements: 소스당 최대 공고 수 (null → 소스별 설정 → 코드 default 200).
    skip_detail:     True 면 상세 수집 건너뜀.
    skip_attachments: True 면 첨부파일 다운로드 건너뜀.
    dry_run:         True 면 DB 쓰기 건너뜀.
    log_level:       로그 레벨 오버라이드 (null → .env LOG_LEVEL).

실행 형태:
    docker compose --profile scrape run --rm scraper

우선순위:
    sources.yaml scrape 섹션 > sources.yaml 소스별 설정 > 코드 내부 default

종료 코드:
    0  : 정상(처리한 공고 수가 0 건이어도 정상 종료)
    1  : 부트스트랩 단계(init_db) 자체가 실패한 경우
    130: SIGINT (Ctrl+C) 로 중단된 경우
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from app.config import Settings, get_settings
from app.db.init_db import init_db
from app.db.repository import (
    DeltaApplyResult,
    apply_delta_to_main,
    append_delta_announcement_errors,
    clear_delta_for_run,
    create_scrape_run,
    finalize_scrape_run,
    get_running_scrape_run,
    insert_delta_announcement,
    insert_delta_attachment,
    peek_main_can_skip_detail,
    set_scrape_run_pid,
    update_delta_announcement_detail,
    upsert_scrape_snapshot,
)
from app.db.models import DeltaAnnouncement, ScrapeRun
from app.db.session import session_scope
from app.db.snapshot import build_snapshot_payload
from app.logging_setup import configure_logging
from app.scrape_control.constants import (
    SCRAPE_ACTIVE_SOURCES_ENV_VAR,
    SCRAPE_RUN_ID_ENV_VAR,
)
from app.scraper.attachment_downloader import scrape_attachments_for_announcement
from app.scraper.base import BaseSourceAdapter
from app.scraper.registry import get_adapter
from app.timezone import KST, now_kst
from app.sources.config_schema import (
    ScrapeRunConfig,
    SourceConfig,
    SourcesConfig,
    load_sources_config,
)

# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────

# 어댑터가 row['status'] 를 채우지 않은 비정상 케이스 대비 fallback. 정상 경로에서는 발생하지 않는다.
DEFAULT_STATUS_LABEL: str = "접수중"

# 코드 내부 default (CLI·sources.yaml 둘 다 없을 때 사용)
CODE_DEFAULT_MAX_PAGES: int = 10
CODE_DEFAULT_MAX_ANNOUNCEMENTS: int = 200

# 날짜 텍스트 → datetime 변환 시 시도할 포맷 후보.
_DATETIME_TEXT_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y.%m.%d",
)


# ──────────────────────────────────────────────────────────────
# 중단 요청 플래그 (Phase 2 / 00025 — SIGTERM handler 용)
# ──────────────────────────────────────────────────────────────

# SIGTERM 이 도착하면 이 플래그가 True 로 바뀐다. 공고 루프와 소스 루프의
# 시작 지점에서 이 값을 확인해 break 한다. 현재 처리 중인 공고(첨부 다운로드 포함)
# 는 끝까지 마무리해야 한다 — atomic 보장(UPSERT + 변경 감지 + 사용자 라벨링 리셋이
# 같은 트랜잭션) 을 깨지 않기 위해서다. 사용자 원문: "공고 마무리 후 종료".
# daemon 스레드 등에서 접근할 가능성은 없지만(cli 는 단일 async loop), list 로
# 감싸서 "in-place mutation" 만으로 상태를 전파할 수 있게 한다 — global 선언
# 을 각 함수에서 반복하지 않아도 된다.
_cancel_flag: list[bool] = [False]


def _handle_sigterm(signum: int, _frame: Any) -> None:
    """SIGTERM 시그널 핸들러.

    공고 한복판에서 강제 종료하지 않고 플래그만 세워 루프 경계에서 이탈하도록
    유도한다. 두 번 연속 SIGTERM 이 와도 첫 요청이 반복적으로 재확인되는 것만
    기록하고 동작은 동일하게 유지한다 (중복 핸들러 호출은 idempotent).
    """
    if _cancel_flag[0]:
        logger.warning(
            "SIGTERM 재수신 — 이미 중단 요청이 처리 중입니다. "
            "subprocess 는 현재 공고 마무리 후 종료합니다. signum={}",
            signum,
        )
        return
    _cancel_flag[0] = True
    logger.warning(
        "SIGTERM 수신 — 중단 요청을 기록했습니다. 현재 공고(첨부 포함)를 "
        "마무리하고 다음 경계에서 종료합니다. signum={}",
        signum,
    )


def _is_cancel_requested() -> bool:
    """공고/소스 루프에서 호출 — 중단 요청이 들어왔는지 확인."""
    return _cancel_flag[0]


def _reset_cancel_flag_for_tests() -> None:
    """테스트에서 플래그를 초기화할 때 사용한다. 운영 코드에서는 부르지 않는다."""
    _cancel_flag[0] = False


# ──────────────────────────────────────────────────────────────
# 데이터 변환 헬퍼
# ──────────────────────────────────────────────────────────────


def _parse_datetime_text(value: Optional[str]) -> Optional[datetime]:
    """날짜 텍스트를 KST 가정으로 파싱한 뒤 timezone-aware UTC datetime 으로 변환한다.

    배경 (task 00040-5):
        IRIS / NTIS 응답의 마감일·접수시작·등록일 텍스트는 모두 한국 현지 시각
        (Asia/Seoul) 의미다. 예를 들어 ``\"2026.05.01\"`` 은 \"KST 2026-05-01 자정\"
        을 의미하므로, UTC 컨벤션으로 저장하려면 ``2026-04-30T15:00:00+00:00`` 으로
        바꿔 넣어야 한다. 이전 구현은 naive 파싱 결과에 그대로 ``tzinfo=UTC`` 를
        부착해 9시간 오차로 저장하던 결함이 있었다 (audit §5).

    동작:
        - 빈/None 입력은 None 통과.
        - 구분자 ``/`` / ``.`` 는 ``-`` 로 정규화.
        - 포맷 후보를 순차 시도해 매칭되면 KST 가정 → UTC 로 변환해 반환.
        - 매칭되지 않으면 경고 로그 후 None.

    Returns:
        UTC tz-aware ``datetime`` 또는 None.
    """
    if not value:
        return None

    normalized_text = value.strip().replace("/", "-").replace(".", "-")
    for candidate_format in _DATETIME_TEXT_FORMATS:
        try:
            naive_dt = datetime.strptime(normalized_text, candidate_format)
        except ValueError:
            continue
        # IRIS / NTIS 가 보내는 텍스트는 모두 한국 현지 시각을 의미한다 (사용자 원문
        # \"외부 응답(IRIS/NTIS): KST 가정 → UTC 변환 저장\"). 시·분 정보가 없는
        # 'YYYY-MM-DD' 입력은 KST 자정이 되며 UTC 로는 전날 15:00 으로 저장된다.
        kst_aware = naive_dt.replace(tzinfo=KST)
        return kst_aware.astimezone(timezone.utc)

    logger.warning("날짜 텍스트 파싱 실패 — 무시: {!r}", value)
    return None


def _build_announcement_payload(row_metadata: dict[str, Any]) -> dict[str, Any]:
    """어댑터가 반환한 row 메타를 repository.upsert_announcement payload 로 변환한다.

    어댑터는 source_announcement_id / source_type 키를 이미 정규화하여 반환한다.

    ancm_no: IRIS 는 목록 단계에서 공식 공고번호(ancmNo)를 row 에 담아 반환한다.
             NTIS 는 목록 단계에서 알 수 없으므로 None 이다(상세 수집 후 subtask 8 에서 재계산).
             두 경우 모두 이 함수가 소스 무관하게 패스스루하여 repository 로 전달한다.
    """
    return {
        "source_announcement_id": row_metadata["source_announcement_id"],
        "source_type": row_metadata["source_type"],
        "title": row_metadata.get("title") or "(제목 미상)",
        "agency": row_metadata.get("agency"),
        "status": row_metadata.get("status") or DEFAULT_STATUS_LABEL,
        "received_at": _parse_datetime_text(row_metadata.get("received_at_text")),
        "deadline_at": _parse_datetime_text(row_metadata.get("deadline_at_text")),
        "detail_url": row_metadata.get("detail_url"),
        # repository._apply_canonical 이 canonical_key 계산에 사용하는 공식 공고번호.
        # 소스 무관하게 row_metadata 에서 꺼내 패스스루한다.
        "ancm_no": row_metadata.get("ancm_no"),
        "raw_metadata": {
            "list_row": {
                key: value
                for key, value in row_metadata.items()
                if key != "row_html"
            },
        },
    }


# ──────────────────────────────────────────────────────────────
# 첨부파일 수집 헬퍼
# ──────────────────────────────────────────────────────────────


@dataclass
class DeltaAttachmentStageResult:
    """단일 공고의 첨부 수집 → delta_attachments 적재 결과 (Phase 5a).

    Phase 1a 의 ``AttachmentStageResult`` 가 본 테이블 attachments 에 직접
    upsert 하던 시점의 통계를 담고, 동시에 2차 감지/reapply 까지 같은 세션에서
    수행했다. Phase 5a 에서는 본 테이블 직접 변경이 사라지고 모두 delta 로
    옮겨졌으므로 이 dataclass 는 다음만 다룬다:

    Attributes:
        download_success_count: 다운로드 성공해 delta_attachments 에 INSERT 된 첨부 수.
        download_failure_count: 다운로드 실패로 raw_metadata.attachment_errors 에 누적된 첨부 수.

    2차 감지(reapply_version_with_reset) 는 apply 단계로 이동했으므로 본
    dataclass 에는 더 이상 ``content_change_detected`` 가 없다 — apply 가
    DeltaApplyResult 의 attachment_content_change_count 로 보고한다.
    """

    download_success_count: int = 0
    download_failure_count: int = 0


async def _scrape_and_store_delta_attachments(
    *,
    delta_announcement_id: int,
    source_type: str,
    source_announcement_id: str,
    settings: Settings,
    adapter: Optional[BaseSourceAdapter] = None,
) -> DeltaAttachmentStageResult:
    """공고 한 건의 첨부를 다운로드해 delta_attachments 에 INSERT 한다.

    Phase 1a/2 의 ``_scrape_and_store_attachments`` 를 delta 흐름으로 이식한
    버전이다. 본 테이블 ``attachments`` 는 절대 건드리지 않으며, 2차 감지/
    reapply 도 수행하지 않는다 — apply 단계가 모두 처리한다 (검증 9 회귀 보존
    은 apply_delta_to_main 안에서 같은 트랜잭션에서 일어남).

    단계:
        1. 새 세션에서 ``DeltaAnnouncement`` 를 조회하고 detail_html 이 채워져
           있는지 확인. detail_html 이 없으면 첨부 수집 자체를 건너뛴다.
        2. 세션에서 expunge 한 뒤 비동기 컨텍스트에서
           ``scrape_attachments_for_announcement`` 호출 — 이 함수는 announcement
           기반의 5 개 필드(.id / .source_type / .source_announcement_id /
           .detail_html / .detail_url) 만 사용하므로 DeltaAnnouncement 도
           duck-typing 으로 그대로 쓸 수 있다. 결과의 success_entries 는
           Phase 1a 의 upsert_attachment payload 형식과 동일하다.
        3. 새 세션에서 성공 항목마다 ``insert_delta_attachment`` 를 호출해
           delta_attachments 에 INSERT. 오류 항목은
           ``append_delta_announcement_errors`` 로 delta.raw_metadata 에 누적
           — apply 단계가 이 메타를 그대로 본 테이블 raw_metadata 로 흘려 보내
           Phase 1a 와 의미적으로 동일한 표현이 된다.

    공고 단위 예외는 호출자(_run_source_announcements) 에서 격리한다.

    Args:
        delta_announcement_id:    부모 DeltaAnnouncement 의 PK.
        source_type:              로그 컨텍스트용 소스 유형.
        source_announcement_id:   로그 컨텍스트용 소스 공고 ID.
        settings:                 전역 설정.
        adapter:                  소스 어댑터.

    Returns:
        DeltaAttachmentStageResult.
    """
    stage = DeltaAttachmentStageResult()

    # 1. delta row 조회 + detail_html 존재 확인. ORM 인스턴스는 비동기 컨텍스트
    #    에서 안전하게 사용되도록 expunge 후 반환.
    delta_for_scraping: Optional[DeltaAnnouncement] = None
    with session_scope() as session:
        fresh_delta = session.get(DeltaAnnouncement, delta_announcement_id)
        if fresh_delta is None or not fresh_delta.detail_html:
            logger.debug(
                "첨부파일 수집 건너뜀: detail_html 없음 — delta_announcement_id={}",
                delta_announcement_id,
            )
            return stage
        session.expunge(fresh_delta)
        delta_for_scraping = fresh_delta

    # 2. 첨부 다운로드 (비동기 네트워크 I/O — 세션 밖). DeltaAnnouncement 가
    #    Announcement 와 동일한 5 개 필드를 노출하므로 duck-typing 으로 그대로 사용.
    att_result = await scrape_attachments_for_announcement(
        delta_for_scraping,
        settings=settings,
        adapter=adapter,
    )

    if not att_result.success_entries and not att_result.error_entries:
        logger.info(
            "첨부파일 수집 완료: source={} id={} 성공={} 실패={} (다운로드 시도 없음)",
            source_type,
            source_announcement_id,
            0,
            0,
        )
        return stage

    # 3. 결과를 delta_attachments / delta.raw_metadata 에 적재.
    with session_scope() as session:
        for entry in att_result.success_entries:
            insert_delta_attachment(
                session,
                delta_announcement_id=delta_announcement_id,
                payload=entry,
            )
            stage.download_success_count += 1

        if att_result.error_entries:
            append_delta_announcement_errors(
                session,
                delta_announcement_id,
                att_result.error_entries,
            )
            stage.download_failure_count += len(att_result.error_entries)

    logger.info(
        "첨부파일 수집 완료: source={} id={} delta_attachments INSERT={} 실패={}",
        source_type,
        source_announcement_id,
        stage.download_success_count,
        stage.download_failure_count,
    )
    return stage


# ──────────────────────────────────────────────────────────────
# 소스별 수집 실행
# ──────────────────────────────────────────────────────────────


async def _run_source_announcements(
    *,
    adapter: BaseSourceAdapter,
    settings: Settings,
    max_pages: int,
    max_announcements: int,
    skip_detail: bool,
    skip_attachments: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """단일 소스 어댑터로 목록·상세·첨부를 증분 수집하고 DB 에 적재한다.

    수집 순서: 목록 UPSERT → (조건부) 상세 수집 → (조건부) 첨부파일 수집.

    UpsertResult.needs_detail_scraping 을 기반으로 상세 수집 여부를 결정한다.
    - needs_detail_scraping=False: 비교 필드 변경 없고 기존 상세 데이터 있음 → 상세 생략
    - needs_detail_scraping=True: 신규·변경·상태전이·상세 미수집 → 상세 수집

    첨부파일 수집 조건: detail_html 이 DB 에 존재하고 skip_attachments=False 인 경우.
    공고 단위 예외는 격리 — 한 공고 실패가 같은 소스의 다음 공고를 중단시키지 않는다.
    첨부파일 수집 실패는 공고 목록·상세 UPSERT 성공에 영향을 주지 않는다.

    Args:
        adapter:           이미 열린(open) 소스 어댑터.
        settings:          전역 설정 (request_delay_sec 등).
        max_pages:         목록 페이지 순회 상한 (양의 정수).
        max_announcements: 소스당 최대 처리 공고 수 (양의 정수).
        skip_detail:       True 면 모든 상세 수집을 건너뛴다 (sources.yaml scrape.skip_detail).
        skip_attachments:  True 면 첨부파일 다운로드를 건너뛴다 (sources.yaml scrape.skip_attachments).
        dry_run:           True 면 DB 쓰기를 건너뛴다.

    Returns:
        Phase 5a 수집 단계 통계 dict — 본 테이블 4-branch 결과(action_counts /
        attachment_content_change_count 등) 는 모두 apply 단계에서 결정되므로
        포함되지 않는다.

        keys:
          - delta_inserted_count: 공고 1건 → delta_announcements INSERT 성공 수
          - delta_failed_count:  delta INSERT 단계에서 실패한 공고 수
          - failed_source_announcement_ids: 실패 공고의 source_announcement_id list
          - detail_success_count: detail 수집 성공 수 (delta 의 detail_html 채워짐)
          - detail_failure_count: detail 수집 실패 수
          - skipped_detail_count: peek_main_can_skip_detail 로 detail 수집을 건너뛴 수
          - attachment_download_success_count: delta_attachments INSERT 성공 첨부 수
          - attachment_download_failure_count: 다운로드 실패로 delta.raw_metadata 에
                                                attachment_errors 로 누적된 첨부 수
    """
    source_type = adapter.source_type

    # 목록 수집
    logger.info("목록 수집 시작: source={} max_pages={}", source_type, max_pages)
    aggregated_rows = await adapter.scrape_list(max_pages=max_pages)
    logger.info("목록 수집 완료: source={} {}건", source_type, len(aggregated_rows))

    target_rows = aggregated_rows[:max_announcements]
    if len(target_rows) < len(aggregated_rows):
        logger.info(
            "처리 대상 제한: source={} max_announcements={} → {}건만 처리",
            source_type, max_announcements, len(target_rows),
        )

    empty_summary: dict[str, Any] = {
        "delta_inserted_count": 0,
        "delta_failed_count": 0,
        "failed_source_announcement_ids": [],
        "detail_success_count": 0,
        "detail_failure_count": 0,
        "skipped_detail_count": 0,
        "attachment_download_success_count": 0,
        "attachment_download_failure_count": 0,
    }

    if not target_rows:
        logger.warning("처리할 공고가 없음: source={}", source_type)
        return empty_summary

    delta_inserted_count = 0
    delta_failed_count = 0
    failed_source_announcement_ids: list[str] = []
    detail_success_count = 0
    detail_failure_count = 0
    skipped_detail_count = 0
    attachment_download_success_count = 0
    attachment_download_failure_count = 0

    # 상세 수집 실제 요청 순서 인덱스 (지연 계산용)
    detail_request_index = 0

    # ScrapeRun id 는 _async_main 이 환경 또는 lock 으로 확보한 뒤 ENV 로 전달
    # 한다 — 본 함수는 그 id 를 import-time 헬퍼 _resolve_active_scrape_run_id
    # 로 가져온다 (테스트 격리 + 단일 source 에서도 일관 동작).
    scrape_run_id = _resolve_active_scrape_run_id()

    for row_index, row_metadata in enumerate(target_rows, start=1):
        # ── (0) SIGTERM 플래그 체크 ─────────────────────────────────────────
        # 공고 1건 내부에서는 중단하지 않는다 — delta INSERT 의 단위 트랜잭션을
        # 깨면 raw_metadata 와 attachments 가 어긋날 수 있다. 다음 공고로 넘어
        # 가기 전 경계에서만 break 한다.
        if _is_cancel_requested():
            remaining = len(target_rows) - row_index + 1
            logger.warning(
                "중단 요청 감지 — 남은 공고 {}건 스킵: source={}",
                remaining, source_type,
            )
            break

        source_announcement_id = row_metadata.get("source_announcement_id") or "(unknown)"
        detail_url: Optional[str] = row_metadata.get("detail_url")

        logger.info(
            "── [{}/{}] 공고 처리: source={} id={}",
            row_index, len(target_rows), source_type, source_announcement_id,
        )

        # ── (1) delta_announcements INSERT (목록 메타) ───────────────────────
        delta_announcement_id: Optional[int] = None
        try:
            payload = _build_announcement_payload(row_metadata)
            if dry_run:
                logger.info(
                    "[dry-run] insert_delta_announcement(skip): source={} id={}",
                    source_type, source_announcement_id,
                )
            else:
                with session_scope() as session:
                    delta_row = insert_delta_announcement(
                        session,
                        scrape_run_id=scrape_run_id,
                        payload=payload,
                    )
                    delta_announcement_id = delta_row.id
                logger.debug(
                    "delta_announcement INSERT: source={} id={} delta_id={}",
                    source_type, source_announcement_id, delta_announcement_id,
                )
            delta_inserted_count += 1
        except Exception as exc:
            delta_failed_count += 1
            failed_source_announcement_ids.append(str(source_announcement_id))
            logger.exception(
                "delta INSERT 실패(스킵): source={} id={} ({}: {})",
                source_type, source_announcement_id, type(exc).__name__, exc,
            )
            # delta INSERT 실패 시 상세·첨부 단계도 스킵
            continue

        # ── (2) detail 수집 ──────────────────────────────────────────────────
        # delta_announcement_has_detail: 이번 루프 종료 시점에 delta 에 detail_html
        # 이 있으면 True (다음 단계 attachments 의 detail_html 의존을 만족).
        delta_announcement_has_detail = False

        if skip_detail or dry_run:
            # skip_detail: 목록 적재만 요청됨. dry_run: DB 쓰기 없음.
            # 두 경우 모두 상세·첨부 수집 없이 다음 공고로.
            pass

        elif _can_skip_detail_against_main(
            source_type=source_type,
            source_announcement_id=source_announcement_id,
            payload=payload,
        ):
            # 본 테이블에 동일 비교 필드 + detail_html 이 있는 unchanged row 가
            # 존재 — apply 단계의 (b) unchanged 분기에서도 detail 재수집 불필요로
            # 판정될 row 다. 현 수집 단계에서 detail 다운로드를 생략한다.
            #
            # 단, delta 의 detail_html 은 비어 있게 되므로 apply 가 본 테이블에
            # detail 을 덮어쓰지 않는다. (apply_delta_to_main 이 delta.detail_*
            # 가 채워진 경우에만 upsert_announcement_detail 호출하도록 가드 되어
            # 있다 — repository 참조.)
            skipped_detail_count += 1
            delta_announcement_has_detail = True
            logger.info(
                "상세 수집 생략(본 테이블에 unchanged + detail 보유): source={} id={}",
                source_type, source_announcement_id,
            )

        elif not detail_url:
            logger.warning(
                "detail_url 없음 — 상세 수집 스킵: source={} id={}",
                source_type, source_announcement_id,
            )
            detail_failure_count += 1
            # detail_url 없으면 detail_html 도 없으므로 첨부도 불가.

        else:
            # ── 상세 수집 실행 ────────────────────────────────────────────────
            # 공고 간 요청 지연 (첫 번째 상세 요청은 건너뜀)
            if detail_request_index > 0:
                await asyncio.sleep(settings.request_delay_sec)
            detail_request_index += 1

            logger.info(
                "상세 수집 시작: source={} id={} url={}",
                source_type, source_announcement_id, detail_url,
            )
            detail_result = await adapter.scrape_detail(detail_url)

            try:
                with session_scope() as session:
                    detail_payload: dict[str, Any] = dict(detail_result)
                    # NTIS 의 ntis_ancm_no 가 detail 결과에 들어오면 delta.ancm_no
                    # 로 흘려 보낸다 — apply 단계가 이를 사용해 canonical 을
                    # fuzzy → official 로 승급한다.
                    ntis_ancm_no: Optional[str] = detail_payload.get("ntis_ancm_no")
                    if ntis_ancm_no:
                        detail_payload["ancm_no"] = ntis_ancm_no
                    update_delta_announcement_detail(
                        session,
                        delta_announcement_id,
                        detail_payload,
                    )
            except Exception as exc:
                logger.exception(
                    "delta detail 갱신 실패: source={} id={} ({}: {})",
                    source_type, source_announcement_id, type(exc).__name__, exc,
                )
                detail_failure_count += 1
                # detail 저장 실패 → detail_html 미보장 → 첨부 스킵
            else:
                fetch_status = detail_result.get("detail_fetch_status", "error")
                if fetch_status == "ok":
                    detail_success_count += 1
                    delta_announcement_has_detail = True
                    logger.info(
                        "상세 수집 완료(ok): source={} id={}",
                        source_type, source_announcement_id,
                    )
                else:
                    detail_failure_count += 1
                    logger.warning(
                        "상세 수집 결과 '{}': source={} id={}",
                        fetch_status, source_type, source_announcement_id,
                    )

        # ── (3) 첨부파일 수집 → delta_attachments 적재 ─────────────────────────
        # 조건: detail_html 이 delta 에 있고, 첨부 수집 건너뜀 플래그가 없고,
        # dry_run 도 아닐 것. delta_announcement_id 가 None 인 경우(dry_run) 는
        # 위에서 이미 pass 처리. 2차 감지/reapply 는 apply 단계로 이동했으므로
        # 본 단계에서는 다운로드 + delta INSERT 만 수행한다.
        if (
            not skip_attachments
            and not dry_run
            and delta_announcement_has_detail
            and delta_announcement_id is not None
        ):
            try:
                attachment_stage = await _scrape_and_store_delta_attachments(
                    delta_announcement_id=delta_announcement_id,
                    source_type=source_type,
                    source_announcement_id=source_announcement_id,
                    settings=settings,
                    adapter=adapter,
                )
                attachment_download_success_count += attachment_stage.download_success_count
                attachment_download_failure_count += attachment_stage.download_failure_count
            except Exception as exc:
                logger.exception(
                    "첨부파일 수집 실패(스킵, 공고 delta INSERT 성공은 유지됨): "
                    "source={} id={} ({}: {})",
                    source_type, source_announcement_id, type(exc).__name__, exc,
                )
                # 첨부 실패는 공고 성공 카운터에 영향 없음 — apply 단계가
                # delta.raw_metadata.attachment_errors 로부터 false-positive 를
                # 회피한다.
                attachment_download_failure_count += 1

    return {
        "delta_inserted_count": delta_inserted_count,
        "delta_failed_count": delta_failed_count,
        "failed_source_announcement_ids": failed_source_announcement_ids,
        "detail_success_count": detail_success_count,
        "detail_failure_count": detail_failure_count,
        "skipped_detail_count": skipped_detail_count,
        "attachment_download_success_count": attachment_download_success_count,
        "attachment_download_failure_count": attachment_download_failure_count,
    }


# 활성 ScrapeRun id 를 함수 단위에서 안전하게 노출하기 위한 모듈 레벨 cache.
# _async_main 이 ScrapeRun row 를 확보한 직후 set 하고, _run_source_announcements
# 가 delta INSERT 시 FK 인자로 사용한다. 테스트는 set/reset 헬퍼로 격리.
_active_scrape_run_id: list[Optional[int]] = [None]


def _set_active_scrape_run_id(run_id: int) -> None:
    """현재 실행 중인 ScrapeRun id 를 모듈 레벨에 등록한다 (_async_main 전용)."""
    _active_scrape_run_id[0] = run_id


def _resolve_active_scrape_run_id() -> int:
    """delta INSERT 시 사용할 활성 ScrapeRun id 를 반환한다.

    _async_main 이 _set_active_scrape_run_id 로 등록한 값이 있으면 그것을 쓴다.
    등록 전에 호출되면 ValueError 로 빠르게 실패시켜 잘못된 분리 호출을 막는다.
    """
    run_id = _active_scrape_run_id[0]
    if run_id is None:
        raise ValueError(
            "활성 ScrapeRun id 가 설정되지 않았습니다 — _async_main 이 "
            "_set_active_scrape_run_id() 를 먼저 호출해야 합니다."
        )
    return run_id


def _reset_active_scrape_run_id_for_tests() -> None:
    """테스트에서 모듈 레벨 캐시를 초기화한다. 운영 코드에서는 부르지 않는다."""
    _active_scrape_run_id[0] = None


def _can_skip_detail_against_main(
    *,
    source_type: str,
    source_announcement_id: str,
    payload: dict[str, Any],
) -> bool:
    """본 테이블의 unchanged + detail 보유 여부를 1회 SELECT 로 peek 한다.

    Phase 1a 의 ``upsert_result.needs_detail_scraping`` 분기를 delta 흐름에서도
    살리기 위한 read-only 우회로다. apply 단계의 4-branch 와 동일 비교 필드를
    사용하므로 의미적으로 1:1 일치한다.

    Args:
        source_type:            소스 유형 (예: 'IRIS').
        source_announcement_id: 공고 소스 ID.
        payload:                _build_announcement_payload 결과 (title/status/
                                agency/deadline_at 포함).

    Returns:
        True 이면 detail 수집을 건너뛴다.
    """
    with session_scope() as session:
        return peek_main_can_skip_detail(
            session,
            source_type=source_type,
            source_announcement_id=source_announcement_id,
            payload=payload,
        )


# ──────────────────────────────────────────────────────────────
# 오케스트레이션
# ──────────────────────────────────────────────────────────────


def _resolve_active_sources(
    scrape_config: ScrapeRunConfig,
    sources_cfg: SourcesConfig,
) -> list[SourceConfig]:
    """실행할 소스 목록을 결정한다.

    - scrape_config.active_sources 가 지정되면 해당 소스만 실행 (enabled 설정 무시).
    - 비어 있으면 sources.yaml 의 enabled=True 소스를 모두 실행한다.
    - sources.yaml 에 없는 소스 ID 는 ERROR 로그 후 건너뛴다.
    """
    if scrape_config.active_sources:
        result = []
        for source_id in scrape_config.active_sources:
            source_config = sources_cfg.get_source(source_id)
            if source_config is None:
                logger.error(
                    "sources.yaml 에 '{}' 소스가 없습니다. 이 소스는 건너뜁니다.",
                    source_id,
                )
            else:
                result.append(source_config)
        return result

    return sources_cfg.get_enabled_sources()


async def _orchestrate(
    *,
    settings: Settings,
    scrape_config: ScrapeRunConfig,
    sources_cfg: SourcesConfig,
) -> dict[str, Any]:
    """소스 목록 → 소스별 공고 수집 → DB 적재 흐름을 수행하고 통계 dict 를 반환한다.

    max_pages / max_announcements 는 sources.yaml scrape 섹션에서 읽는다.
    각 소스 루프에서 scrape 섹션 > sources.yaml 소스별 > 코드 default 우선순위로 유효 상한을 결정한다.

    부트스트랩 실패(init_db)는 호출자로 전파한다.
    소스 단위 예외는 격리 — 한 소스 실패가 다른 소스를 중단시키지 않는다.

    Phase 2(00025) 이후로 init_db 는 `_async_main` 에서 ScrapeRun 생성 전에
    먼저 호출하므로, 여기서는 idempotent 재호출로 안전하게 보강한다.
    """
    # (1) DB 스키마 보장 — _async_main 이 먼저 호출했을 가능성이 있지만,
    #     init_db 는 stamp vs upgrade 를 자동 분기하는 멱등 구현이라 안전.
    logger.info("DB 초기화 시작 — dry_run={}", scrape_config.dry_run)
    init_db()

    # (2) 활성 소스 목록 결정
    active_sources = _resolve_active_sources(scrape_config, sources_cfg)
    logger.info("활성 소스: {}", [s.id for s in active_sources])

    # (3) 소스별 실행 및 통계 집계 — Phase 5a 의 수집-단계 통계만 집계.
    # action_counts / attachment_content_change_count 같은 본 테이블 결과 통계는
    # apply_delta_to_main 의 DeltaApplyResult 가 별도로 보고한다.
    total_delta_inserted = 0
    total_delta_failed = 0
    total_failed_source_announcement_ids: list[str] = []
    total_detail_success = 0
    total_detail_failure = 0
    total_skipped_detail = 0
    total_attachment_download_success = 0
    total_attachment_download_failure = 0

    for source_config in active_sources:
        # ── (0) SIGTERM 플래그 체크 ─────────────────────────────────────────
        # 소스 단위에서도 경계 체크. 공고 루프가 break 한 뒤 이 체크에 진입한다.
        if _is_cancel_requested():
            remaining_sources = (
                len(active_sources) - active_sources.index(source_config)
            )
            logger.warning(
                "중단 요청 감지 — 남은 소스 {}건 스킵",
                remaining_sources,
            )
            break

        # scrape 섹션 > sources.yaml 소스별 > 코드 default 우선순위로 유효 상한 결정
        effective_max_pages: int = (
            scrape_config.max_pages if scrape_config.max_pages is not None
            else (source_config.max_pages if source_config.max_pages is not None
                  else CODE_DEFAULT_MAX_PAGES)
        )
        effective_max_announcements: int = (
            scrape_config.max_announcements if scrape_config.max_announcements is not None
            else (source_config.max_announcements if source_config.max_announcements is not None
                  else CODE_DEFAULT_MAX_ANNOUNCEMENTS)
        )

        logger.info(
            "── 소스 {} 수집 시작 (max_pages={} max_announcements={})",
            source_config.id, effective_max_pages, effective_max_announcements,
        )
        try:
            adapter = get_adapter(source_config, settings)
            async with adapter:
                source_stats = await _run_source_announcements(
                    adapter=adapter,
                    settings=settings,
                    max_pages=effective_max_pages,
                    max_announcements=effective_max_announcements,
                    skip_detail=scrape_config.skip_detail,
                    skip_attachments=scrape_config.skip_attachments,
                    dry_run=scrape_config.dry_run,
                )
        except Exception as exc:
            logger.exception(
                "소스 {} 수집 실패 — 다음 소스로 계속: ({}: {})",
                source_config.id, type(exc).__name__, exc,
            )
            continue

        total_delta_inserted += source_stats["delta_inserted_count"]
        total_delta_failed += source_stats["delta_failed_count"]
        total_failed_source_announcement_ids.extend(
            source_stats["failed_source_announcement_ids"]
        )
        total_detail_success += source_stats["detail_success_count"]
        total_detail_failure += source_stats["detail_failure_count"]
        total_skipped_detail += source_stats["skipped_detail_count"]
        total_attachment_download_success += source_stats[
            "attachment_download_success_count"
        ]
        total_attachment_download_failure += source_stats[
            "attachment_download_failure_count"
        ]

        logger.info(
            "소스 {} 완료(수집 단계): delta INSERT 성공 {}건 / 실패 {}건 | "
            "상세 성공 {}건 / 실패 {}건 / 생략(unchanged peek) {}건 | "
            "첨부 다운로드 성공 {}건 / 실패 {}건 "
            "(action_counts / 2차 감지 통계는 apply 단계에서 집계됩니다)",
            source_config.id,
            source_stats["delta_inserted_count"],
            source_stats["delta_failed_count"],
            source_stats["detail_success_count"],
            source_stats["detail_failure_count"],
            source_stats["skipped_detail_count"],
            source_stats["attachment_download_success_count"],
            source_stats["attachment_download_failure_count"],
        )

    return {
        "delta_inserted_count": total_delta_inserted,
        "delta_failed_count": total_delta_failed,
        "failed_source_announcement_ids": total_failed_source_announcement_ids,
        "detail_success_count": total_detail_success,
        "detail_failure_count": total_detail_failure,
        "skipped_detail_count": total_skipped_detail,
        "attachment_download_success_count": total_attachment_download_success,
        "attachment_download_failure_count": total_attachment_download_failure,
    }


# ──────────────────────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────────────────────


def _build_final_source_counts(
    summary: dict[str, Any],
    scrape_config: ScrapeRunConfig,
    apply_result: Optional[DeltaApplyResult] = None,
) -> dict[str, Any]:
    """ScrapeRun.source_counts 컬럼에 저장할 최종 요약 JSON 을 만든다.

    수집 단계의 요약(`summary` — _orchestrate 반환 dict) 과 apply 단계의 결과
    (`apply_result` — DeltaApplyResult 또는 None) 를 합쳐 자유 스키마 JSON 으로
    구성한다. apply 가 실행되지 않은 경우(cancelled / orchestrator-failed) 에도
    summary 만으로 호출 가능하다.

    Args:
        summary:        `_orchestrate` 의 반환 dict (수집 단계 통계).
        scrape_config:  이번 run 의 ScrapeRunConfig — active_sources 기록용.
        apply_result:   apply_delta_to_main 의 반환값. 없으면 0 으로 채운다.

    Returns:
        JSON 직렬화 가능한 dict — UI 가 마지막 실행의 결과 요약을 표시할 수
        있도록 active_sources / collection / apply / failed_source_announcement_ids
        섹션을 둔다.
    """
    apply_action_counts = (
        dict(apply_result.upsert_action_counts) if apply_result is not None else {}
    )
    return {
        "active_sources": list(scrape_config.active_sources),
        "collection": {
            "delta_inserted": summary.get("delta_inserted_count", 0),
            "delta_failed": summary.get("delta_failed_count", 0),
            "detail_success": summary.get("detail_success_count", 0),
            "detail_failure": summary.get("detail_failure_count", 0),
            "skipped_detail": summary.get("skipped_detail_count", 0),
            "attachment_download_success": summary.get(
                "attachment_download_success_count", 0
            ),
            "attachment_download_failure": summary.get(
                "attachment_download_failure_count", 0
            ),
        },
        "apply": {
            "executed": apply_result is not None,
            "delta_announcement_count": (
                apply_result.delta_announcement_count if apply_result else 0
            ),
            "action_counts": apply_action_counts,
            "new_announcement_ids": list(
                apply_result.new_announcement_ids if apply_result else []
            ),
            "content_changed_announcement_ids": list(
                apply_result.content_changed_announcement_ids if apply_result else []
            ),
            "transition_count": (
                len(apply_result.transitions) if apply_result else 0
            ),
            "attachment_success": (
                apply_result.attachment_success_count if apply_result else 0
            ),
            "attachment_skipped": (
                apply_result.attachment_skipped_count if apply_result else 0
            ),
            "attachment_content_change": (
                apply_result.attachment_content_change_count if apply_result else 0
            ),
        },
        "failed_source_announcement_ids": list(
            summary.get("failed_source_announcement_ids", [])
        ),
    }


def _resolve_final_cli_status(
    summary: dict[str, Any],
    *,
    cancel_requested: bool,
) -> str:
    """수집 결과 summary 로부터 ScrapeRun 최종 status 를 판정한다.

    apply 단계에서의 예외는 호출자(_async_main) 가 별도로 처리하므로, 여기서는
    수집 단계 통계만 본다 (apply 자체 실패는 호출자가 status='failed' 로
    덮어씀).

    우선순위 (수집 단계 결과 한정):
        1. cancel_requested=True → 'cancelled'.
        2. delta_inserted_count=0 이고 delta_failed_count>0 → 'failed'.
        3. delta_inserted_count>0 이고 delta_failed_count>0 → 'partial'.
        4. 그 외(실패 0) → 'completed'.
    """
    if cancel_requested:
        return "cancelled"
    delta_failed = int(summary.get("delta_failed_count", 0))
    delta_inserted = int(summary.get("delta_inserted_count", 0))
    if delta_failed > 0 and delta_inserted == 0:
        return "failed"
    if delta_failed > 0:
        return "partial"
    return "completed"


def _apply_active_sources_override(scrape_config: ScrapeRunConfig) -> ScrapeRunConfig:
    """``SCRAPE_ACTIVE_SOURCES`` 환경변수가 있으면 active_sources 를 덮어쓴다.

    웹/스케줄러가 subprocess 에 ``-e SCRAPE_ACTIVE_SOURCES=IRIS,NTIS`` 로 전달한
    값을 여기서 해석한다. 값이 없거나 공백만 있으면 sources.yaml 의 기존
    active_sources 를 그대로 사용한다.

    이 방식을 쓰는 이유(guidance): sources.yaml 은 entrypoint.sh 가 per-run 임시
    복사본을 만들기 때문에, 호출측에서 파일을 in-place 로 수정하면 동시 실행
    안전성이 깨진다. 환경변수 주입은 파일을 건드리지 않고 run 단위 파라미터를
    안전하게 전달한다.

    Args:
        scrape_config: sources.yaml 에서 파싱된 기본 scrape 설정.

    Returns:
        active_sources 가 env override 로 교체된 복제본(원본은 건드리지 않음).
        env 가 비어 있으면 원본 그대로.
    """
    raw = os.environ.get(SCRAPE_ACTIVE_SOURCES_ENV_VAR, "").strip()
    if not raw:
        return scrape_config

    override = [item.strip() for item in raw.split(",") if item.strip()]
    if not override:
        return scrape_config

    logger.info(
        "{} 환경변수 override 적용: {} → {}",
        SCRAPE_ACTIVE_SOURCES_ENV_VAR,
        list(scrape_config.active_sources),
        override,
    )
    return scrape_config.model_copy(update={"active_sources": override})


async def _async_main() -> int:
    """async 진입점. sources.yaml 의 scrape 설정을 읽어 수집을 실행한다.

    Phase 2(00025) 이후 동작:
        - SIGTERM handler 를 등록해 중단 요청 시 현재 공고 마무리 후 정상 종료.
        - SCRAPE_ACTIVE_SOURCES 환경변수가 있으면 active_sources 를 덮어쓴다.
        - SCRAPE_RUN_ID 환경변수가 주어지면(웹/스케줄러가 이미 INSERT 한 running
          row 를 이어받는 경로) 자체 create_scrape_run 을 건너뛰고 해당 id 를
          재사용한다. 주입된 id 가 무효(없음/terminal)면 exit(2).
        - SCRAPE_RUN_ID 가 없으면(순수 CLI 직접 실행) 기존과 동일하게 running
          row 가 있으면 거부하고 exit(2), 없으면 trigger='cli' 로 row INSERT +
          자기 pid 기록.
        - 정상/실패/부분/중단 판정 후 finalize_scrape_run 으로 마감.

    Returns:
        프로세스 종료 코드.
        - 0: completed/partial/cancelled (수집 자체는 정상 흐름으로 마무리된 경우)
        - 1: 부트스트랩 실패 또는 전역 예외
        - 2: lock 충돌 (이미 다른 수집이 실행 중) 또는 SCRAPE_RUN_ID 무효 주입
    """
    sources_cfg = load_sources_config()
    scrape_config = _apply_active_sources_override(sources_cfg.scrape)

    settings = get_settings()
    # sources.yaml 의 log_level 이 지정된 경우 in-place 로 덮어쓴다.
    if scrape_config.log_level:
        settings.log_level = scrape_config.log_level
    configure_logging(settings)
    settings.ensure_runtime_paths()

    # SIGTERM handler 등록 — 웹/스케줄러가 보낸 중단 요청을 받기 위함.
    # asyncio loop 에서는 loop.add_signal_handler 가 더 정돈된 방식이지만,
    # 우리는 공고 루프 본문에서 플래그를 폴링만 하면 되므로 기본 signal.signal
    # 이 충분하다. 등록은 프로세스당 1회 — 기존 SIGINT 처리는 main() 의
    # KeyboardInterrupt 경로가 담당한다.
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except ValueError as exc:
        # 메인 스레드가 아닌 경우(일부 테스트) ValueError. 테스트 격리 차원의
        # 경고만 남기고 진행한다.
        logger.warning("SIGTERM handler 등록 실패(무시): {}", exc)

    logger.info(
        "scrape 실행 시작: active_sources={} max_pages={} max_announcements={} "
        "skip_detail={} skip_attachments={} dry_run={}",
        scrape_config.active_sources or "(enabled 소스 전체)",
        scrape_config.max_pages,
        scrape_config.max_announcements,
        scrape_config.skip_detail,
        scrape_config.skip_attachments,
        scrape_config.dry_run,
    )

    # ── ScrapeRun lock + row 생성 (같은 트랜잭션) ───────────────────────────
    # 웹/스케줄러의 lock 과 동일한 규칙을 CLI 에서도 적용한다. running row 가
    # 하나라도 있으면 새 실행을 거부한다 — 기존 CLI 경로도 이 규칙을 따른다.
    # DB 초기화(init_db)는 ScrapeRun 테이블 접근 전 반드시 수행.
    try:
        init_db()
    except Exception as exc:
        logger.exception(
            "init_db 실패 — scrape_runs 접근 불가. ({}: {})",
            type(exc).__name__, exc,
        )
        return 1

    scrape_run_id: Optional[int] = None

    # ── 웹/스케줄러가 주입한 SCRAPE_RUN_ID 이어받기 (task 00034) ────────────────
    # 웹/스케줄러 경로는 start_scrape_run 이 이미 create_scrape_run 으로 running
    # row 를 INSERT 한 뒤 subprocess 를 기동한다. 이 env 가 주입됐는데 CLI 가
    # 기존 로직대로 get_running_scrape_run → create_scrape_run 을 수행하면
    #  (1) 방금 웹이 만든 row 를 '기존 실행' 으로 오판해 exit 2 하거나
    #  (2) running row 가 2 개가 되어 lock 의 의미가 깨진다.
    # 따라서 env 로 소유권을 이어받는 경우 새 row 를 만들지 않고 해당 row 를
    # 그대로 finalize 단계까지 사용한다.
    raw_run_id_env = os.environ.get(SCRAPE_RUN_ID_ENV_VAR, "").strip()
    candidate_run_id: Optional[int] = None
    if raw_run_id_env:
        try:
            candidate_run_id = int(raw_run_id_env)
        except ValueError:
            # guidance: 비어있거나 int() 실패면 기존 CLI 경로로 fallthrough.
            # 경고만 남기고 아래 기존 경로로 진입한다.
            logger.warning(
                "{} 환경변수가 정수가 아니므로 이어받기를 포기하고 기존 CLI 경로로 진행: "
                "value={!r}",
                SCRAPE_RUN_ID_ENV_VAR, raw_run_id_env,
            )
            candidate_run_id = None
    if candidate_run_id is not None:
        try:
            with session_scope() as session:
                owned_row = session.get(ScrapeRun, candidate_run_id)
                if owned_row is None or owned_row.status != "running":
                    status_repr = (
                        owned_row.status if owned_row is not None else "(missing)"
                    )
                    logger.error(
                        "{}={} 로 지정된 ScrapeRun 을 이어받을 수 없습니다 "
                        "(status={}). 이번 실행을 거부합니다.",
                        SCRAPE_RUN_ID_ENV_VAR, candidate_run_id, status_repr,
                    )
                    return 2
                # runner.py 가 기록한 pid 는 호스트 docker CLI 프로세스 pid 다.
                # 여기서 self pid 로 덮어쓰면 app 컨테이너 내부 PID 이므로
                # 호스트 관점의 kill -0 검사는 무의미해진다. 다만
                # cleanup_stale_running_runs 은 subprocess 가 살아있는 동안
                # 개입하지 않고 watcher 스레드가 종료 후 finalize 하는 설계
                # 이므로 실용상 문제 없다. CLI 직접 실행 경로와의 일관성
                # (자기 pid 기록) 을 맞추기 위해 덮어쓴다.
                set_scrape_run_pid(session, candidate_run_id, os.getpid())
                scrape_run_id = candidate_run_id
                logger.info(
                    "웹 기반 실행에 의해 ScrapeRun id={} 을(를) 이어받음 "
                    "(trigger={!r}). CLI 자체 create_scrape_run 은 건너뜁니다.",
                    candidate_run_id, owned_row.trigger,
                )
        except Exception as exc:
            logger.exception(
                "ScrapeRun 이어받기 실패 — ScrapeRun id={} ({}: {})",
                candidate_run_id, type(exc).__name__, exc,
            )
            return 1

    # env 로 이어받지 못한 경우에만 기존 CLI 경로(자체 create_scrape_run) 수행.
    if scrape_run_id is None:
        try:
            with session_scope() as session:
                existing_running = get_running_scrape_run(session)
                if existing_running is not None:
                    logger.error(
                        "이미 다른 수집이 진행 중입니다 (ScrapeRun id={} trigger={!r} "
                        "pid={}). 이번 CLI 실행은 거부됩니다.",
                        existing_running.id,
                        existing_running.trigger,
                        existing_running.pid,
                    )
                    return 2
                scrape_run_row = create_scrape_run(
                    session,
                    trigger="cli",
                    source_counts={"active_sources": list(scrape_config.active_sources)},
                )
                scrape_run_id = scrape_run_row.id
                # CLI 는 자신의 pid 를 기록한다. stale cleanup 이 pid 존재 여부를
                # 확인하므로, 비정상 종료 후 재기동 시 정확히 정리된다.
                set_scrape_run_pid(session, scrape_run_id, os.getpid())
        except Exception as exc:
            logger.exception(
                "ScrapeRun 생성 실패 — DB 문제를 먼저 해결하세요. ({}: {})",
                type(exc).__name__, exc,
            )
            return 1

    # ── 활성 ScrapeRun id 를 모듈 레벨에 등록 ────────────────────────────────
    # _run_source_announcements 가 delta INSERT 시 FK 인자로 사용한다.
    # finally 블록에서 _reset_active_scrape_run_id_for_tests 로 정리한다.
    _set_active_scrape_run_id(scrape_run_id)

    # ── 실행 + apply + finalize 보장 ────────────────────────────────────────
    # 단계:
    #   (1) _orchestrate 가 수집 단계만 수행 (delta INSERT). summary 반환.
    #   (2) _resolve_final_cli_status 가 수집 결과로 candidate status 산정.
    #   (3) candidate == 'cancelled' (SIGTERM): 별도 트랜잭션으로
    #       clear_delta_for_run 만 호출 — 본 테이블 / snapshot 변경 없음
    #       (검증 2 만족).
    #   (4) candidate ∈ {completed, partial}: session_scope 안에서
    #       apply_delta_to_main 호출. 트랜잭션 commit 시 본 테이블 / delta /
    #       (00041-4 에서 snapshot) 모두 영구화. 트랜잭션 자체가 raise 하면
    #       SQLAlchemy auto-rollback 으로 모두 원상복구 (검증 11 만족) + status
    #       를 'failed' 로 덮어쓰고 delta 보존(추가 clear 호출하지 않음).
    #   (5) orchestrator-level 예외(apply 도달 전): status='failed' + 별도
    #       트랜잭션으로 clear_delta_for_run.
    summary: Optional[dict[str, Any]] = None
    apply_result: Optional[DeltaApplyResult] = None
    final_status: str = "failed"
    final_error: Optional[str] = None
    exit_code: int = 1
    keyboard_interrupt_raised = False

    try:
        summary = await _orchestrate(
            settings=settings,
            scrape_config=scrape_config,
            sources_cfg=sources_cfg,
        )
        candidate_status = _resolve_final_cli_status(
            summary, cancel_requested=_is_cancel_requested()
        )

        if candidate_status == "cancelled":
            # SIGTERM 분기 — 검증 2: delta 비워짐 + 본 테이블 / snapshot 변경 없음.
            try:
                with session_scope() as cancel_session:
                    cleared = clear_delta_for_run(cancel_session, scrape_run_id)
                logger.warning(
                    "수집 중단(cancelled) — apply 건너뜀 + delta 비움 (deleted={})",
                    cleared,
                )
            except Exception as clear_exc:
                logger.exception(
                    "cancelled 분기 delta clear 실패: id={} ({}: {})",
                    scrape_run_id, type(clear_exc).__name__, clear_exc,
                )
            final_status = "cancelled"
        else:
            # completed / partial — apply 트랜잭션 단일 진입.
            try:
                with session_scope() as apply_session:
                    apply_result = apply_delta_to_main(
                        apply_session,
                        scrape_run_id=scrape_run_id,
                    )
                    # ── snapshot 생성/머지 (00041-4) ──────────────────────────
                    # apply 와 같은 트랜잭션에서 ScrapeSnapshot UPSERT 를 수행해
                    # \"본 테이블 적용 + delta 비움 + snapshot 생성\" 의 atomic
                    # 단위를 완성한다 (사용자 원문 \"수집 종료 시: 단일 트랜잭션\").
                    # apply 단계가 raise 하면 SQLAlchemy auto-rollback 으로
                    # snapshot UPSERT 도 함께 원상복구되어 검증 11 시나리오를
                    # 만족한다 (apply / snapshot 어느 단계가 raise 하든 동일).
                    # snapshot_date 는 종료 시점의 KST 날짜 — Phase 4 컨벤션.
                    snapshot_payload = build_snapshot_payload(apply_result)
                    upsert_scrape_snapshot(
                        apply_session,
                        snapshot_date=now_kst().date(),
                        new_payload=snapshot_payload,
                    )
                final_status = candidate_status
                logger.info(
                    "apply_delta_to_main 트랜잭션 commit 완료: status={}",
                    final_status,
                )
            except Exception as apply_exc:
                # 검증 11 — apply 트랜잭션 자체 실패. SQLAlchemy auto-rollback 으로
                # 본 테이블 / delta / (snapshot) 모두 원상복구. 추가 clear 는 하지
                # 않는다 — delta 가 보존되어 다음 ScrapeRun 또는 운영자 수동
                # 재시도 경로가 살아있다.
                final_status = "failed"
                final_error = f"apply 트랜잭션 실패 — {type(apply_exc).__name__}: {apply_exc}"
                logger.exception(
                    "apply_delta_to_main 트랜잭션 실패 — delta 보존, status=failed: "
                    "({}: {})",
                    type(apply_exc).__name__, apply_exc,
                )

        # completed/partial/cancelled 모두 '수집 자체는 정상 흐름' 이므로 exit 0.
        # failed 만 1.
        exit_code = 1 if final_status == "failed" else 0
    except KeyboardInterrupt:
        # Ctrl+C / SIGINT — SIGTERM 과 달리 중단 의사가 즉시적이지만,
        # 공고 단위 atomic 은 이미 깨졌을 가능성이 있다. status='cancelled' 로
        # 기록하고 main() 에서 130 반환하도록 재발생.
        # cancelled 분기와 동일하게 delta 만 비운다 (best-effort).
        keyboard_interrupt_raised = True
        final_status = "cancelled"
        final_error = "SIGINT (사용자 중단)"
        try:
            with session_scope() as cancel_session:
                clear_delta_for_run(cancel_session, scrape_run_id)
        except Exception as clear_exc:
            logger.exception(
                "SIGINT 분기 delta clear 실패: id={} ({}: {})",
                scrape_run_id, type(clear_exc).__name__, clear_exc,
            )
    except Exception as exc:
        # orchestrator/수집-단계 예외 — apply 도달 전. delta 가 incomplete 일
        # 가능성이 있어 별도 트랜잭션으로 비운다 (재시도하지 않음).
        final_status = "failed"
        final_error = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "오케스트레이션 중 전역 예외 — status=failed (apply 도달 전): ({}: {})",
            type(exc).__name__, exc,
        )
        try:
            with session_scope() as cancel_session:
                clear_delta_for_run(cancel_session, scrape_run_id)
        except Exception as clear_exc:
            logger.exception(
                "orchestrator-failed 분기 delta clear 실패: id={} ({}: {})",
                scrape_run_id, type(clear_exc).__name__, clear_exc,
            )
    finally:
        # ScrapeRun 마감 — 어떤 경로로 빠져나가도 한 번은 호출된다.
        # finalize_scrape_run 은 idempotent 이므로 중복 호출도 안전.
        try:
            with session_scope() as session:
                finalize_scrape_run(
                    session,
                    scrape_run_id,
                    status=final_status,
                    source_counts=(
                        _build_final_source_counts(summary, scrape_config, apply_result)
                        if summary is not None
                        else None
                    ),
                    error_message=final_error,
                )
        except Exception as fin_exc:
            logger.exception(
                "ScrapeRun finalize 실패(스킵): id={} ({}: {})",
                scrape_run_id, type(fin_exc).__name__, fin_exc,
            )
        # 모듈 레벨 활성 ScrapeRun id 정리 — 다음 실행이 stale 값을 보지 않도록.
        _reset_active_scrape_run_id_for_tests()

    if summary is not None:
        apply_action_counts = (
            apply_result.upsert_action_counts if apply_result is not None else {}
        )
        logger.info(
            "scrape 실행 완료: delta INSERT 성공 {}건 / 실패 {}건 | "
            "상세 성공 {}건 / 실패 {}건 / 생략(unchanged peek) {}건 | "
            "첨부 다운로드 성공 {}건 / 실패 {}건 | "
            "apply action 분포: 신규={} 변경없음={} 버전갱신={} 상태전이={} | "
            "apply 2차 감지(첨부 변경)={}건 | final_status={}",
            summary["delta_inserted_count"],
            summary["delta_failed_count"],
            summary["detail_success_count"],
            summary["detail_failure_count"],
            summary["skipped_detail_count"],
            summary["attachment_download_success_count"],
            summary["attachment_download_failure_count"],
            apply_action_counts.get("created", 0),
            apply_action_counts.get("unchanged", 0),
            apply_action_counts.get("new_version", 0),
            apply_action_counts.get("status_transitioned", 0),
            apply_result.attachment_content_change_count if apply_result else 0,
            final_status,
        )
        if summary["delta_failed_count"]:
            logger.warning(
                "delta INSERT 실패 source_announcement_id 목록: {}",
                summary["failed_source_announcement_ids"],
            )

    if keyboard_interrupt_raised:
        # main() 의 KeyboardInterrupt 처리 경로로 넘긴다(종료 코드 130).
        raise KeyboardInterrupt()

    return exit_code


def main() -> None:
    """OS 진입점. asyncio.run 으로 _async_main 을 실행하고 sys.exit 한다.

    SIGINT(Ctrl+C) 를 받으면 종료 코드 130 으로 종료한다.
    """
    try:
        exit_code = asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.warning("사용자 중단(SIGINT) 감지 — 종료 코드 130")
        sys.exit(130)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()


__all__ = [
    "main",
    "DeltaAttachmentStageResult",
    "DEFAULT_STATUS_LABEL",
    "CODE_DEFAULT_MAX_PAGES",
    "CODE_DEFAULT_MAX_ANNOUNCEMENTS",
]


# ──────────────────────────────────────────────────────────────
# 테스트 전용 헬퍼 export (운영 코드에서는 사용하지 않음)
# ──────────────────────────────────────────────────────────────
# _cancel_flag / _active_scrape_run_id 등 모듈 전역 상태를 테스트가
# 초기화할 수 있도록 공개한다. 외부에서 직접 조작하는 운영 코드는 없어야 한다.

__all__ += [
    "_apply_active_sources_override",
    "_build_final_source_counts",
    "_handle_sigterm",
    "_is_cancel_requested",
    "_reset_active_scrape_run_id_for_tests",
    "_reset_cancel_flag_for_tests",
    "_resolve_final_cli_status",
    "_set_active_scrape_run_id",
]
