"""Repository 레이어: 공고 증분 UPSERT 및 조회.

설계 원칙:
    - 모든 함수는 호출자가 전달한 `Session` 을 그대로 사용한다.
      트랜잭션 경계(commit/rollback)는 호출자가 `session_scope()` 등으로 제어한다.
      (리포지토리는 `flush()` 까지만 수행한다.)
    - "현재 버전" 기준: is_current=True. list/count/detail 조회는 이 조건을 기본으로 건다.
      get_announcement_by_id 는 PK 직접 접근이므로 과거 버전 참조도 허용한다.

증분 수집 전략 (변경 감지):
    `upsert_announcement` 는 `UpsertResult` 를 반환하며, CLI 오케스트레이터가
    상세 수집 여부를 결정하는 데 사용한다.

    1차 감지 비교 대상 필드 (_CHANGE_DETECTION_FIELDS):
      title, status, deadline_at, agency
    - title, status, deadline_at: 공고를 특정하는 핵심 3종
    - agency: 주관 기관 변경도 의미 있는 내용 변화로 판정
    - received_at: 미포함 — 접수예정 상태에서 수집 시 미기재 후 보완되는 경우가 많아,
      빈번한 불필요한 상세 재수집 트리거를 피하고자 제외

    4-branch 동작 요약:
        (a) 기존 is_current row 없음
            → INSERT (is_current=True), action="created", needs_detail_scraping=True
        (b) 기존 row 존재, 비교 필드 변경 없음
            → 아무것도 기록하지 않음, action="unchanged",
               needs_detail_scraping=(detail_fetched_at is None)
        (c) 기존 row 존재, changed_fields == {"status"} (상태 전이만)
            → 기존 row in-place UPDATE (상태만 갱신), action="status_transitioned",
               needs_detail_scraping=True.
               IRIS 접수예정/접수중/마감 3개 상태 수집이 활성화된 이후 정상 실행 경로.
               예: 접수예정으로 등록된 공고가 다음 수집 시 접수중으로 재등장하는 경우.
               ※ 리셋(아래) 대상 아님 — 사용자 원문 "status 단독 전이는 기존
                 in-place UPDATE 유지".
            ※ [NTIS 등 신규 크롤러 구현 시]:
               새 소스 어댑터를 추가할 때 status 값을 AnnouncementStatus Enum 으로
               정규화하는 로직을 반드시 포함해야 한다.
        (d) 기존 row 존재, 그 외 변경 (title/deadline_at/agency 변경, status 포함 여부 무관)
            → 기존 row is_current=False 봉인 + 신규 row INSERT (is_current=True),
               action="new_version", needs_detail_scraping=True, changed_fields 원본 유지.
               이력이 row 단위로 누적된다.
               ★ 사용자 라벨링 리셋(_reset_user_state_on_content_change) 을 동일
                 트랜잭션에서 호출한다 — 봉인된 old row 의 읽음 상태 초기화 +
                 canonical 의 관련성 판정 이관.

2차 변경 감지 (첨부 sha256 기반):
    사용자 원문: "UPSERT 시점에 첨부 sha256 모름. 첨부 다운로드 후 2차 변경 감지
    추가. 2차 감지 시 is_current 순환 + 리셋. 1차는 유지."
    - `compute_attachment_signature` / `snapshot_announcement_attachments` 로
      before/after signature 를 캡처.
    - `detect_attachment_changes(before, after)` 로 변경 여부 판정.
    - 변경이면 `reapply_version_with_reset(session, announcement_id)` 호출 — 2차 경로는
      status 단독 변경 분기에서는 트리거되지 않는다 (호출자가 보장).

트랜잭션 규약 (사용자 라벨링 atomic 보장):
    리셋(_reset_user_state_on_content_change) 은 UPSERT/재배치와 **동일 session**
    에서 수행된다. 따라서 리셋 중 예외가 발생하고 호출자가 rollback 하면
    UPSERT 도 함께 롤백된다 — "내용은 새 row 인데 읽음은 그대로" 같은 오염
    상태가 남지 않는다.

payload 규약:
    - `upsert_announcement(session, payload)` 의 `payload` 는
      `Announcement` 의 컬럼명을 키로 갖는 매핑이다.
      최소 요구 키: `source_announcement_id`, `source_type`, `title`, `status`.
      `status` 는 `AnnouncementStatus` Enum 또는 그 문자열 값("접수중"/"접수예정"/"마감") 중 하나.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, selectinload

from app.canonical import compute_canonical_key
from app.db.models import (
    Announcement,
    AnnouncementStatus,
    AnnouncementUserState,
    Attachment,
    CanonicalProject,
    DeltaAnnouncement,
    DeltaAttachment,
    FavoriteEntry,
    FavoriteFolder,
    RelevanceJudgment,
    RelevanceJudgmentHistory,
    SCRAPE_RUN_STATUSES,
    SCRAPE_RUN_TERMINAL_STATUSES,
    SCRAPE_RUN_TRIGGERS,
    ScrapeRun,
    ScrapeSnapshot,
    User,
)
from app.db.snapshot import (
    build_snapshot_payload,
    merge_snapshot_payload,
    normalize_payload,
)
from app.sources.config_schema import load_sources_config

# RelevanceJudgmentHistory.archive_reason 의 허용값(app-level 상수).
# DB CHECK 대신 여기서 관리 — 향후 관리자 이관·admin override 추가 시 확장한다.
_ARCHIVE_REASON_CONTENT_CHANGED: str = "content_changed"
_ARCHIVE_REASON_USER_OVERWRITE: str = "user_overwrite"
_ARCHIVE_REASON_ADMIN_OVERRIDE: str = "admin_override"
_ALLOWED_ARCHIVE_REASONS: frozenset[str] = frozenset(
    {
        _ARCHIVE_REASON_CONTENT_CHANGED,
        _ARCHIVE_REASON_USER_OVERWRITE,
        _ARCHIVE_REASON_ADMIN_OVERRIDE,
    }
)

# 공고에서 사용자가 payload 로 덮어쓰지 말아야 하는 자동 관리 컬럼들.
_ANNOUNCEMENT_PROTECTED_FIELDS: frozenset[str] = frozenset({"id", "scraped_at", "updated_at"})

# 허용되는 정렬 기준 값.
_ALLOWED_SORT_VALUES: frozenset[str] = frozenset({"received_desc", "deadline_asc", "title_asc"})

# 관련성 판정 verdict 허용값.
RELEVANCE_VERDICT_RELATED: str = "관련"
RELEVANCE_VERDICT_UNRELATED: str = "무관"
RELEVANCE_ALLOWED_VERDICTS: frozenset[str] = frozenset(
    {RELEVANCE_VERDICT_RELATED, RELEVANCE_VERDICT_UNRELATED}
)

# bulk 읽음 처리 상한 (환경변수 오버라이드 가능).
MAX_BULK_MARK: int = int(os.getenv("MAX_BULK_MARK", "5000"))

# 공고 payload 에 허용되는 컬럼 목록(화이트리스트).
_ANNOUNCEMENT_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "source_announcement_id",
        "source_type",
        "title",
        "agency",
        "status",
        "received_at",
        "deadline_at",
        "detail_url",
        "raw_metadata",
    }
)

# 상세 수집 결과 업데이트 시 허용되는 컬럼 목록.
_DETAIL_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "detail_html",
        "detail_text",
        "detail_fetched_at",
        "detail_fetch_status",
    }
)

# 변경 감지 비교 대상 필드.
# - title, status, deadline_at: 공고를 특정하는 핵심 필드
# - agency: 주관 기관 변경도 의미 있는 내용 변화로 판정
# received_at 은 의도적으로 제외 (접수예정 시 미기재 후 보완 패턴이 흔함)
_CHANGE_DETECTION_FIELDS: tuple[str, ...] = ("title", "status", "deadline_at", "agency")


def _apply_canonical(
    session: Session,
    announcement: Announcement,
    *,
    ancm_no: str | None,
    clean_payload: dict[str, Any],
) -> None:
    """canonical_key 를 계산하고 CanonicalProject 를 조회/생성하여 announcement 에 적용한다.

    예외 격리: 내부 오류 발생 시 canonical_group_id=NULL 을 유지한 채 경고 로그만 남긴다.
    공고 단위 UPSERT 흐름이 canonical 실패로 중단되지 않도록 보장한다.

    Args:
        session:       호출자 세션 (flush 포함, commit 은 호출자 책임).
        announcement:  canonical 필드를 적용할 Announcement 인스턴스.
        ancm_no:       IRIS 공식 공고번호(ancmNo). None 이면 fuzzy fallback 사용.
        clean_payload: _filter_payload 후 status 정규화가 완료된 payload dict.
                       title / agency / deadline_at 추출에 사용한다.
    """
    try:
        official_key_candidates = [ancm_no] if ancm_no else []
        result = compute_canonical_key(
            official_key_candidates=official_key_candidates,
            title=clean_payload.get("title") or "",
            agency=clean_payload.get("agency"),
            deadline_at=clean_payload.get("deadline_at"),
        )

        # canonical_key 로 기존 CanonicalProject 를 조회한다.
        cp = session.execute(
            select(CanonicalProject).where(CanonicalProject.canonical_key == result.canonical_key)
        ).scalar_one_or_none()

        if cp is None:
            # 신규 canonical group 생성 — representative 정보는 최초 수집 시각 기준으로 저장.
            cp = CanonicalProject(
                canonical_key=result.canonical_key,
                key_scheme=result.canonical_scheme,
                representative_title=clean_payload.get("title"),
                representative_agency=clean_payload.get("agency"),
            )
            session.add(cp)
            session.flush()  # PK(cp.id) 확보
            logger.debug(
                "canonical 신규 그룹 생성: key={} scheme={} group_id={}",
                result.canonical_key,
                result.canonical_scheme,
                cp.id,
            )
        else:
            logger.debug(
                "canonical 기존 그룹 매칭: key={} group_id={}",
                result.canonical_key,
                cp.id,
            )

        announcement.canonical_group_id = cp.id
        announcement.canonical_key = result.canonical_key
        announcement.canonical_key_scheme = result.canonical_scheme
        session.flush()

    except Exception as exc:
        logger.warning(
            "canonical 매칭 실패 — canonical_group_id=NULL 유지 (announcement.id={}): {}",
            announcement.id,
            exc,
        )


@dataclass
class UpsertResult:
    """upsert_announcement() 의 반환값.

    CLI 오케스트레이터(app/cli.py)가 상세 수집 여부를 결정하고 통계를 기록하는 데 사용한다.

    Attributes:
        announcement:          삽입되거나 갱신된 Announcement 인스턴스 (is_current=True).
        action:                수행된 작업 유형.
                               "created"            — 신규 레코드 삽입
                               "unchanged"          — 비교 필드 변경 없음
                               "status_transitioned"— 상태(status)만 in-place 갱신
                               "new_version"        — 이력 보존: 구 row 봉인 + 신규 row 삽입
        needs_detail_scraping: True 이면 상세 페이지를 새로 수집해야 한다.
                               - "created" / "status_transitioned" / "new_version": 항상 True
                               - "unchanged": detail_fetched_at 이 None 이면 True,
                                 아니면 False (기존 상세 데이터 재사용)
        changed_fields:        변경된 필드 이름의 집합.
                               action="status_transitioned" 이면 {"status"}.
                               action="new_version" 이면 변경된 필드들의 집합.
                               action="created"/"unchanged" 이면 빈 frozenset.
    """

    announcement: Announcement
    action: Literal["created", "unchanged", "status_transitioned", "new_version"]
    needs_detail_scraping: bool
    changed_fields: frozenset[str] = field(default_factory=frozenset)


def _coerce_status(value: Any) -> AnnouncementStatus:
    """payload 의 status 값을 `AnnouncementStatus` 로 변환한다.

    - 이미 Enum 인스턴스면 그대로 반환한다.
    - 문자열이면 한글 값("접수중"/"접수예정"/"마감") 으로 매칭한다.
    - 그 외는 `ValueError` 를 일으켜 잘못된 입력을 빠르게 노출시킨다.
    """
    if isinstance(value, AnnouncementStatus):
        return value
    if isinstance(value, str):
        for member in AnnouncementStatus:
            if member.value == value:
                return member
        try:
            return AnnouncementStatus[value]
        except KeyError as exc:
            raise ValueError(
                f"알 수 없는 공고 상태값: {value!r}. 허용값: {[m.value for m in AnnouncementStatus]}"
            ) from exc
    raise TypeError(f"status 는 AnnouncementStatus 또는 str 이어야 합니다. 입력 타입: {type(value).__name__}")


@dataclass
class CanonicalGroupRow:
    """canonical 그룹 단위 묶어보기 결과 단위.

    list_canonical_groups() 가 반환하는 원소 타입.
    canonical_group_id 가 있는 공고는 그룹별 대표 1건으로 묶이고,
    canonical_group_id 가 없는 고아 공고는 group_size=1 로 개별 단독 표현된다.
    """

    canonical_group_id: int | None
    canonical_key: str | None
    representative: Announcement
    group_size: int  # 그룹 내 is_current=True 공고 수 (고아는 1)


def _apply_common_filters(
    statement: Any,
    status: AnnouncementStatus | None,
    source: str | None,
    search: str | None,
) -> Any:
    """공통 필터(상태·소스·제목 검색)를 statement 에 적용한다.

    WHERE 절을 누적 방식으로 추가한다.
    이 함수는 is_current=True 조건을 추가하지 않는다 — 호출자가 직접 추가한다.

    Args:
        statement: SQLAlchemy select 또는 count statement.
        status:    AnnouncementStatus Enum, None 이면 전체.
        source:    source_type 문자열, None 이면 전체.
        search:    제목 부분일치 검색어, None 이면 검색 없음.

    Returns:
        WHERE 절이 추가된 statement.
    """
    if status is not None:
        statement = statement.where(Announcement.status == status)
    if source is not None:
        statement = statement.where(Announcement.source_type == source)
    if search is not None:
        normalized = search.strip()
        if normalized:
            # 사용자 원문: '제목 검색: SQL LIKE 부분일치로 충분' — title 단독 LIKE
            statement = statement.where(Announcement.title.ilike(f"%{normalized}%"))
    return statement


def _apply_sort_order(statement: Any, sort: str) -> Any:
    """정렬 조건을 statement 에 적용한다.

    Args:
        statement: SQLAlchemy select statement.
        sort:      정렬 기준. received_desc | deadline_asc | title_asc.

    Returns:
        ORDER BY 절이 추가된 statement.
    """
    if sort == "deadline_asc":
        # deadline_at NULL 은 뒤로
        return statement.order_by(
            (Announcement.deadline_at.is_(None)).asc(),
            Announcement.deadline_at.asc(),
            Announcement.id.desc(),
        )
    elif sort == "title_asc":
        return statement.order_by(
            Announcement.title.asc(),
            Announcement.id.desc(),
        )
    else:
        # received_desc (기본): 수집일 최신순, received_at NULL 은 뒤로
        return statement.order_by(
            (Announcement.received_at.is_(None)).asc(),  # NULL → 뒤로
            Announcement.received_at.desc(),
            Announcement.id.desc(),
        )


def _group_row_sort_key(ann: Announcement, sort: str) -> tuple:
    """Python 정렬용 비교 키를 반환한다.

    list_canonical_groups 에서 그룹 대표 Announcement 를 기준으로 정렬할 때 사용한다.

    Args:
        ann:  정렬 기준 Announcement (canonical 그룹 대표 또는 고아 공고).
        sort: 정렬 기준 문자열. received_desc | deadline_asc | title_asc.

    Returns:
        Python sorted() 에 전달할 수 있는 비교 가능한 tuple.
    """
    if sort == "deadline_asc":
        d = ann.deadline_at
        # None → 맨 뒤 (float("inf"))
        ts = d.timestamp() if d is not None else float("inf")
        return (ts, -ann.id)
    elif sort == "title_asc":
        return (ann.title or "", -ann.id)
    else:
        # received_desc: 최신(큰 timestamp) 우선, None → 맨 뒤
        r = ann.received_at
        if r is None:
            return (1, float("inf"), -ann.id)
        return (0, -r.timestamp(), -ann.id)


def _filter_payload(payload: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """payload 에서 허용된 컬럼만 추려 dict 로 반환한다.

    허용 목록에 없는 키는 조용히 무시한다.
    """
    return {key: value for key, value in payload.items() if key in allowed}


def _normalize_for_comparison(value: Any) -> Any:
    """변경 감지 비교 전 값을 정규화한다. DB 저장 데이터는 수정하지 않는다.

    - datetime: tz-aware 이면 naive UTC 로 변환, tz-naive 이면 그대로 반환한다.
      SQLite 가 tz-naive 로 돌려주는 datetime 과 payload 의 tz-aware UTC datetime 간
      불일치를 해소하여 false-positive 변경 감지를 방지한다.
    - str: 앞뒤 공백 제거. IRIS 응답에 포함될 수 있는 여백으로 인한 false-positive 방지.
    - 기타(None, Enum 등): 변경 없이 반환한다.

    접수예정 공고의 None 처리 동작:
      - deadline_at_text 가 공란인 접수예정 공고는 payload.deadline_at=None 으로 전달된다.
      - None 은 그대로 None 으로 반환되므로 DB 의 NULL 과 동일하게 비교된다.
      - 결과: 최초 수집 시 (a) INSERT, 재수집 시 deadline_at 이 여전히 None 이면 (b) unchanged,
        나중에 deadline_at 이 채워지면 changed_fields={deadline_at} → (d) new_version 봉인+INSERT.
      - received_at 은 _CHANGE_DETECTION_FIELDS 에서 의도적으로 제외되어 있어
        공란 보완 시에도 new_version 을 트리거하지 않는다.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            # tz-aware → naive UTC 로 변환 (비교 전용, 저장 데이터 불변)
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if type(value) is str:
        return value.strip()
    return value


def _detect_changes(
    existing: Announcement,
    clean_payload: dict[str, Any],
    comparison_fields: Iterable[str] = _CHANGE_DETECTION_FIELDS,
) -> frozenset[str]:
    """기존 레코드와 신규 payload 를 비교해 변경된 필드 이름의 집합을 반환한다.

    기본 비교 대상: title, status, deadline_at, agency (_CHANGE_DETECTION_FIELDS).
    `comparison_fields` 인자로 비교 필드 튜플을 교체할 수 있어, 2차 감지나
    향후 확장 시나리오에서 동일 비교 로직을 재사용할 수 있다.
    payload 에 해당 키가 없으면 해당 필드는 변경 없음으로 판정한다.
    양측 값은 _normalize_for_comparison 으로 정규화한 뒤 비교하여
    datetime tz-naive/aware 불일치 및 문자열 공백 차이로 인한 false-positive 를 제거한다.

    Args:
        existing:          DB 에서 조회한 기존 Announcement 인스턴스 (is_current=True).
        clean_payload:     _filter_payload + status 정규화가 완료된 payload dict.
        comparison_fields: 비교할 필드 이름 목록. 기본값은 1차 감지용 4필드.

    Returns:
        변경된 필드 이름의 frozenset. 변경이 없으면 빈 frozenset.
    """
    changed: set[str] = set()
    for field_name in comparison_fields:
        if field_name not in clean_payload:
            continue
        existing_val = _normalize_for_comparison(getattr(existing, field_name))
        new_val = _normalize_for_comparison(clean_payload[field_name])
        if existing_val != new_val:
            changed.add(field_name)
    return frozenset(changed)


# ── 첨부 signature / 2차 변경 감지 ───────────────────────────────────────────
# 사용자 원문: "목록→UPSERT→상세→첨부. UPSERT 시점에 첨부 sha256 모름.
# 해결: 첨부 다운로드 후 2차 변경 감지 추가. 2차 감지 시 is_current 순환 + 리셋.
# 1차는 유지."
#
# 흐름 (CLI 오케스트레이터 책임, subtask 00019-4):
#   1. 1차 upsert_announcement(session, payload) 호출 (기존 경로 유지)
#   2. 상세 수집 + 첨부 다운로드 전, compute_attachment_signature(announcement) 로
#      '다운로드 이전' signature 스냅샷
#   3. upsert_attachment 반복 호출 (sha256/크기/경로 최신화)
#   4. session.refresh(announcement, ['attachments']) 후 동일 함수로
#      'after' signature 획득
#   5. detect_attachment_changes(before, after) 로 변경 여부 판정
#   6. 변경이면 reapply_version_with_reset(session, announcement.id,
#                                          changed_fields=frozenset({'attachments'}))
#      호출 — is_current 순환 + 사용자 라벨링 리셋을 동일 트랜잭션에서 수행

@dataclass(frozen=True)
class AttachmentSignature:
    """공고 첨부의 비교 가능 스냅샷.

    Attributes:
        count: 전체 첨부 개수 (sha256 유무와 무관).
        sha256s: 확정된 sha256 hex 문자열 집합. sha256 이 None 인 첨부는
                 여기서 제외한다 — 다운로드 전 단계에서는 해시가 없을 수 있다.
        pending_without_sha256: sha256 이 아직 None 인 첨부 개수.
                                '해시는 모르지만 row 는 존재' 상태를 구분한다.
    """

    count: int
    sha256s: frozenset[str]
    pending_without_sha256: int = 0


@dataclass(frozen=True)
class AttachmentChange:
    """2차 감지 결과 — before/after AttachmentSignature 비교 산출물.

    Attributes:
        added: after 에만 있는 sha256 (새로 다운로드된 첨부).
        removed: before 에만 있는 sha256 (이전 세트에서 사라진 첨부).
        count_changed: 전체 개수가 달라졌는지.

    `changed` 프로퍼티가 True 이면 2차 리셋 트리거 대상.
    """

    added: frozenset[str]
    removed: frozenset[str]
    count_changed: bool

    @property
    def changed(self) -> bool:
        """첨부 기반으로 내용 변경이 감지되었는지 여부를 반환한다."""
        return bool(self.added) or bool(self.removed) or self.count_changed


@dataclass(frozen=True)
class HistoryWithUser:
    """get_relevance_history* 함수의 반환 단위.

    RelevanceJudgmentHistory 에 User relationship 이 없으므로 JOIN 결과를
    이 dataclass 로 묶어 N+1 없이 반환한다.
    """

    history: RelevanceJudgmentHistory
    username: str


def compute_attachment_signature(announcement: Announcement) -> AttachmentSignature:
    """Announcement 인스턴스의 현재 첨부 상태로부터 signature 를 계산한다.

    `announcement.attachments` 관계를 사용한다. 관계가 아직 로드되지 않았으면
    SQLAlchemy 가 필요한 시점에 쿼리를 발행한다(`lazy='selectin'` 기본).
    sha256 이 None 인 첨부는 `sha256s` 집합에서 제외하되 개수는 count 에 반영한다.

    Args:
        announcement: 첨부를 로드할 수 있는 Announcement 인스턴스.

    Returns:
        AttachmentSignature. 비교에는 `detect_attachment_changes` 를 사용한다.
    """
    attachments = list(announcement.attachments or [])
    sha_set: set[str] = set()
    pending = 0
    for attachment in attachments:
        if attachment.sha256:
            sha_set.add(attachment.sha256)
        else:
            pending += 1
    return AttachmentSignature(
        count=len(attachments),
        sha256s=frozenset(sha_set),
        pending_without_sha256=pending,
    )


def snapshot_announcement_attachments(
    session: Session,
    announcement_id: int,
) -> AttachmentSignature:
    """DB 에서 직접 첨부를 조회해 signature 를 만든다.

    ORM 캐시를 우회하고 최신 DB 상태를 집계하므로, 2차 감지의 'after' 스냅샷용으로
    쓰기 적합하다. 현재/이력 announcement 모두 대상이 될 수 있도록 is_current 조건은
    걸지 않는다 (호출자가 announcement_id 선택 책임).

    Args:
        session: 호출자 세션.
        announcement_id: 대상 Announcement PK.

    Returns:
        AttachmentSignature.
    """
    rows = session.execute(
        select(Attachment.sha256).where(Attachment.announcement_id == announcement_id)
    ).all()
    total = len(rows)
    sha_set: set[str] = set()
    pending = 0
    for (sha,) in rows:
        if sha:
            sha_set.add(sha)
        else:
            pending += 1
    return AttachmentSignature(
        count=total,
        sha256s=frozenset(sha_set),
        pending_without_sha256=pending,
    )


def detect_attachment_changes(
    before: AttachmentSignature,
    after: AttachmentSignature,
) -> AttachmentChange:
    """첨부 signature before/after 를 비교해 2차 감지 결과를 반환한다.

    순수 함수이며 session 을 필요로 하지 않는다. sha256 기반 집합 차집합으로
    added/removed 를 계산한다. sha256 이 결정되기 전인 첨부는 sha256s 집합에
    포함되지 않으므로 added/removed 에도 드러나지 않는다 — count_changed 로만
    감지된다.

    상위 로직 주의:
        status 단독 변경 분기((c))에서는 이 함수를 호출하지 않는다 — 사용자 원문
        "status 단독 전이는 기존 in-place UPDATE 유지" 를 따른다.

    Args:
        before: 첨부 다운로드 이전 signature.
        after: 첨부 다운로드 이후 signature.

    Returns:
        AttachmentChange. `.changed` 가 True 이면 2차 리셋 트리거.
    """
    return AttachmentChange(
        added=after.sha256s - before.sha256s,
        removed=before.sha256s - after.sha256s,
        count_changed=(before.count != after.count),
    )


# ── 사용자 라벨링 리셋 / 2차 감지용 is_current 순환 ───────────────────────────

def _reset_user_state_on_content_change(
    session: Session,
    old_announcement_id: int,
    *,
    archive_reason: str = _ARCHIVE_REASON_CONTENT_CHANGED,
) -> dict[str, int]:
    """내용 변경으로 봉인된 announcement 의 사용자 라벨링을 리셋한다.

    동작 (모두 호출자 세션 = 동일 트랜잭션 안에서 실행):
      (a) AnnouncementUserState(announcement_id=old) 전체를 is_read=False,
          read_at=NULL 로 UPDATE. updated_at 은 onupdate 로 자동 갱신.
      (b) old_announcement 의 canonical_group_id 를 찾아, 해당 canonical 의
          RelevanceJudgment 를 RelevanceJudgmentHistory 로 복사 후 원본 삭제.
          archive_reason 기본 'content_changed', archived_at=_utcnow.
      (c) FavoriteEntry 는 건드리지 않는다 — 사용자 원문 "FavoriteEntry 유지".

    경계 케이스:
      - old_announcement 가 DB 에 없으면 조용히 no-op (경고 로그만).
      - canonical_group_id 가 NULL 이면 (b) 단계 스킵 — 이관할 대상 자체가 없다.
      - User 가 아직 아무도 없거나 대상 레코드가 없으면 rowcount=0 으로 안전 no-op.

    호출 규약 — 사용자 원문 "변경 시 리셋 (status 단독 제외)":
      - (d) new_version 분기에서는 반드시 호출한다.
      - 2차 감지(첨부 기반 변경) reapply 경로에서도 호출한다.
      - (c) status_transitioned 분기에서는 호출하지 않는다.

    트랜잭션 경계:
      session.flush() 까지만 수행한다. commit 은 호출자 책임이며,
      이 함수 실행 도중이나 이후 예외가 발생하면 호출자가 rollback 을 걸면
      UPSERT(봉인+INSERT) 와 리셋이 함께 롤백된다 — atomic 보장.

    Args:
        session: 호출자 세션.
        old_announcement_id: 봉인된(is_current=False) Announcement.id.
        archive_reason: RelevanceJudgmentHistory.archive_reason 값.
                       _ALLOWED_ARCHIVE_REASONS 중 하나여야 한다.

    Returns:
        {"announcement_user_states_reset": N,
         "relevance_judgments_archived": M} 지표 dict. 감사/로그용.

    Raises:
        ValueError: archive_reason 이 허용값이 아닐 때.
    """
    if archive_reason not in _ALLOWED_ARCHIVE_REASONS:
        raise ValueError(
            f"archive_reason 은 {sorted(_ALLOWED_ARCHIVE_REASONS)} 중 하나여야 합니다. "
            f"입력: {archive_reason!r}"
        )

    # ── (a) AnnouncementUserState 읽음 리셋 ─────────────────────────────────
    # User 테이블이 비어있거나 매칭 row 가 없으면 rowcount=0 으로 자연스럽게 no-op.
    read_reset_result = session.execute(
        update(AnnouncementUserState)
        .where(AnnouncementUserState.announcement_id == old_announcement_id)
        .values(is_read=False, read_at=None)
    )
    read_reset_count = int(read_reset_result.rowcount or 0)

    # ── (b) RelevanceJudgment → History 이관 ────────────────────────────────
    old_announcement = session.get(Announcement, old_announcement_id)
    if old_announcement is None:
        logger.warning(
            "리셋 대상 Announcement 가 DB 에 없음: id={} — (b) 단계 스킵",
            old_announcement_id,
        )
        session.flush()
        return {
            "announcement_user_states_reset": read_reset_count,
            "relevance_judgments_archived": 0,
        }

    canonical_project_id = old_announcement.canonical_group_id
    if canonical_project_id is None:
        # canonical 미연결 공고 → RelevanceJudgment 는 FK 구조상 존재할 수 없으므로 스킵.
        session.flush()
        return {
            "announcement_user_states_reset": read_reset_count,
            "relevance_judgments_archived": 0,
        }

    judgments = list(
        session.execute(
            select(RelevanceJudgment).where(
                RelevanceJudgment.canonical_project_id == canonical_project_id
            )
        )
        .scalars()
        .all()
    )

    archived_count = 0
    archive_time = _utcnow_for_reset()
    for judgment in judgments:
        # 원본 판정 메타를 그대로 복사 — decided_at 은 원본 값 유지.
        history_row = RelevanceJudgmentHistory(
            canonical_project_id=judgment.canonical_project_id,
            user_id=judgment.user_id,
            verdict=judgment.verdict,
            reason=judgment.reason,
            decided_at=judgment.decided_at,
            archived_at=archive_time,
            archive_reason=archive_reason,
        )
        session.add(history_row)
        session.delete(judgment)
        archived_count += 1

    # flush 로 DB 에러(FK 위반 등)를 즉시 드러나게 한다.
    session.flush()

    if read_reset_count or archived_count:
        logger.info(
            "사용자 라벨링 리셋: announcement_id={} canonical={} "
            "reset_states={} archived_judgments={} reason={}",
            old_announcement_id,
            canonical_project_id,
            read_reset_count,
            archived_count,
            archive_reason,
        )

    return {
        "announcement_user_states_reset": read_reset_count,
        "relevance_judgments_archived": archived_count,
    }


def _utcnow_for_reset() -> datetime:
    """리셋 트랜잭션 안에서 사용할 현재 UTC 시각을 반환한다.

    models._utcnow 와 동일하지만 테스트가 monkeypatch 하기 쉽도록 여기서 래핑한다.
    """
    return datetime.now(tz=UTC)


def reapply_version_with_reset(
    session: Session,
    announcement_id: int,
    *,
    changed_fields: frozenset[str] = frozenset({"attachments"}),
    archive_reason: str = _ARCHIVE_REASON_CONTENT_CHANGED,
) -> UpsertResult:
    """is_current 순환 + 사용자 라벨링 리셋을 수행한다 (2차 감지 실행 헬퍼).

    사용자 원문의 "2차 감지 시 is_current 순환 + 리셋" 을 하나의 atomic 헬퍼로
    구현한다. 1차 감지의 (d) new_version 분기와 동일한 시맨틱이지만, payload 가
    아니라 **이미 DB 에 있는 현재 row 의 값 그대로** 신규 row 를 복제한다 —
    첨부 sha256 변경 등 '공고 본문은 같지만 첨부가 바뀐' 케이스에 적합하다.

    동작:
      1. announcement_id 의 현재 is_current=True row 조회. 없으면 ValueError.
      2. 기존 row 를 is_current=False 로 봉인.
      3. 동일 필드값으로 신규 Announcement row INSERT (is_current=True).
         canonical 3종(group_id/key/key_scheme) 과 detail 관련 필드도 승계.
         scraped_at 은 신규 시각, updated_at 은 onupdate 로 자동.
      4. `_reset_user_state_on_content_change(session, old_id)` 호출 —
         읽음 리셋 + 관련성 판정 이관.

    주의 — 첨부(Attachment) 재배치는 이 함수의 책임이 아니다:
      Attachment.announcement_id FK 가 old row 에 걸려 있다. is_current=False 로
      봉인만 하면 첨부는 old row 에 남는다. 2차 감지 호출자(CLI, subtask 00019-4)
      가 정책에 따라 첨부 재배치(update) 를 수행하거나 그대로 둔다.

    (c) status_transitioned 분기에서는 이 함수를 호출하지 않는다.

    Args:
        session: 호출자 세션.
        announcement_id: 현재 is_current=True 여야 하는 Announcement.id.
        changed_fields: UpsertResult.changed_fields 에 기록할 값.
                        2차 감지 기본값은 {'attachments'}.
        archive_reason: RelevanceJudgmentHistory.archive_reason. 기본 'content_changed'.

    Returns:
        UpsertResult(action='new_version', changed_fields=..., needs_detail_scraping=False).
        2차 감지 경로에서는 상세는 이미 있으므로 False.

    Raises:
        ValueError: announcement_id 의 is_current=True row 가 없을 때.
    """
    existing = session.execute(
        select(Announcement).where(
            Announcement.id == announcement_id,
            Announcement.is_current.is_(True),
        )
    ).scalar_one_or_none()

    if existing is None:
        raise ValueError(
            f"reapply 대상 announcement(id={announcement_id}) 의 is_current=True "
            f"row 를 찾을 수 없습니다."
        )

    old_announcement_id = existing.id

    # 승계할 필드 값을 먼저 캡처한다 — is_current=False 로 바꾸기 전.
    snapshot: dict[str, Any] = {
        "source_announcement_id": existing.source_announcement_id,
        "source_type": existing.source_type,
        "title": existing.title,
        "agency": existing.agency,
        "status": existing.status,
        "received_at": existing.received_at,
        "deadline_at": existing.deadline_at,
        "detail_url": existing.detail_url,
        "detail_html": existing.detail_html,
        "detail_text": existing.detail_text,
        "detail_fetched_at": existing.detail_fetched_at,
        "detail_fetch_status": existing.detail_fetch_status,
        "raw_metadata": dict(existing.raw_metadata or {}),
    }
    inherited_canonical_group_id = existing.canonical_group_id
    inherited_canonical_key = existing.canonical_key
    inherited_canonical_key_scheme = existing.canonical_key_scheme

    # 기존 row 봉인
    existing.is_current = False

    # 신규 row INSERT — 동일 필드 + 승계 canonical + is_current=True
    new_announcement = Announcement(**snapshot, is_current=True)
    session.add(new_announcement)
    session.flush()

    new_announcement.canonical_group_id = inherited_canonical_group_id
    new_announcement.canonical_key = inherited_canonical_key
    new_announcement.canonical_key_scheme = inherited_canonical_key_scheme
    session.flush()

    logger.debug(
        "reapply_version_with_reset: old_id={} new_id={} changed_fields={}",
        old_announcement_id,
        new_announcement.id,
        sorted(changed_fields),
    )

    # 동일 트랜잭션에서 사용자 라벨링 리셋
    _reset_user_state_on_content_change(
        session,
        old_announcement_id=old_announcement_id,
        archive_reason=archive_reason,
    )

    return UpsertResult(
        announcement=new_announcement,
        action="new_version",
        # 2차 경로는 이미 상세/첨부가 채워진 상태에서 호출되므로 재수집 불필요.
        needs_detail_scraping=False,
        changed_fields=frozenset(changed_fields),
    )


def upsert_announcement(session: Session, payload: Mapping[str, Any]) -> UpsertResult:
    """공고 한 건을 증분 UPSERT 한다 (4-branch 변경 감지).

    동작 (분기 상세는 모듈 docstring 참조):
        (a) 기존 is_current row 없음 → INSERT, action="created"
        (b) 변경 없음 → 무변경, action="unchanged"
        (c) status 만 변경 → in-place UPDATE, action="status_transitioned"
        (d) 그 외 변경 → 기존 row 봉인(is_current=False) + 신규 row INSERT, action="new_version"

    id / scraped_at / updated_at 은 payload 로 덮어쓰지 않는다.
    commit 은 호출자 책임이며, 여기서는 PK 확보를 위해 flush() 만 수행한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        payload: 공고 속성을 담은 매핑.
                 최소 `source_announcement_id`, `source_type`, `title`, `status` 필요.

    Returns:
        UpsertResult (announcement, action, needs_detail_scraping, changed_fields).

    Raises:
        KeyError: 필수 키가 payload 에 없을 때.
        ValueError: `status` 값이 허용 범위를 벗어났을 때.
    """
    for required_key in ("source_announcement_id", "source_type"):
        if required_key not in payload:
            raise KeyError(f"payload 에 {required_key!r} 가 반드시 포함되어야 합니다.")

    source_announcement_id = payload["source_announcement_id"]
    source_type = payload["source_type"]

    # ancm_no: IRIS 공식 공고번호(ancmNo). _ANNOUNCEMENT_ALLOWED_FIELDS 밖이므로
    # canonical 계산에만 쓰고 announcements 컬럼에는 직접 기록하지 않는다.
    ancm_no: str | None = payload.get("ancm_no") or None

    # 허용 컬럼만 추출 + status 정규화.
    clean_payload = _filter_payload(payload, _ANNOUNCEMENT_ALLOWED_FIELDS)
    if "status" in clean_payload:
        clean_payload["status"] = _coerce_status(clean_payload["status"])

    # 현재 유효 버전(is_current=True) 한정으로 조회한다.
    existing = session.execute(
        select(Announcement).where(
            Announcement.source_type == source_type,
            Announcement.source_announcement_id == source_announcement_id,
            Announcement.is_current.is_(True),
        )
    ).scalar_one_or_none()

    # ── (a) 신규 공고 ──────────────────────────────────────────────────────────
    if existing is None:
        announcement = Announcement(**clean_payload, is_current=True)
        session.add(announcement)
        session.flush()
        _apply_canonical(session, announcement, ancm_no=ancm_no, clean_payload=clean_payload)
        return UpsertResult(
            announcement=announcement,
            action="created",
            needs_detail_scraping=True,
            changed_fields=frozenset(),
        )

    # ── 변경 감지 ─────────────────────────────────────────────────────────────
    changed_fields = _detect_changes(existing, clean_payload)

    # ── (b) 변경 없음 ──────────────────────────────────────────────────────────
    if not changed_fields:
        # 상세가 아직 수집되지 않은 row 는 detail scraping 이 필요하다.
        needs_detail = existing.detail_fetched_at is None
        # canonical_group_id 가 아직 없으면 기회적으로 채운다 (backfill 전 기존 데이터).
        if existing.canonical_group_id is None:
            _apply_canonical(session, existing, ancm_no=ancm_no, clean_payload=clean_payload)
        return UpsertResult(
            announcement=existing,
            action="unchanged",
            needs_detail_scraping=needs_detail,
            changed_fields=frozenset(),
        )

    # ── (c) 상태 전이: status 만 변경 ─────────────────────────────────────────
    # 동일 공고가 다른 상태(예: 접수예정→접수중, 접수중→마감)로 재등장할 때 발동한다.
    if changed_fields == frozenset({"status"}):
        existing.status = clean_payload["status"]
        # detail_url 이 함께 내려온 경우 갱신한다 (동일 공고, URL 불변이 보통이지만 방어적으로).
        if "detail_url" in clean_payload:
            existing.detail_url = clean_payload["detail_url"]
        # canonical_group_id 가 아직 없으면 채운다. 이미 있으면 건드리지 않는다.
        if existing.canonical_group_id is None:
            _apply_canonical(session, existing, ancm_no=ancm_no, clean_payload=clean_payload)
        session.flush()
        return UpsertResult(
            announcement=existing,
            action="status_transitioned",
            needs_detail_scraping=True,
            changed_fields=changed_fields,
        )

    # ── (d) 내용 변경: 이력 보존 — 기존 row 봉인 + 신규 row INSERT ─────────────
    # 기존 row 의 canonical_group_id 를 신규 row 에 승계한다 (같은 공고의 이력이므로 같은 그룹).
    inherited_canonical_group_id = existing.canonical_group_id
    inherited_canonical_key = existing.canonical_key
    inherited_canonical_key_scheme = existing.canonical_key_scheme
    old_announcement_id = existing.id

    existing.is_current = False
    # 신규 row 에는 payload 값 + is_current=True 를 설정한다.
    # scraped_at 은 새 수집 시각으로 기록한다 (모델 default=_utcnow 가 처리).
    new_announcement = Announcement(**clean_payload, is_current=True)
    session.add(new_announcement)
    session.flush()

    if inherited_canonical_group_id is not None:
        # 구 row 의 canonical group 을 그대로 승계한다.
        new_announcement.canonical_group_id = inherited_canonical_group_id
        new_announcement.canonical_key = inherited_canonical_key
        new_announcement.canonical_key_scheme = inherited_canonical_key_scheme
        session.flush()
        logger.debug(
            "canonical 승계(new_version): key={} group_id={}",
            inherited_canonical_key,
            inherited_canonical_group_id,
        )
    else:
        # 구 row 에 canonical 이 없었으면 신규 row 에 대해 새로 계산한다.
        _apply_canonical(session, new_announcement, ancm_no=ancm_no, clean_payload=clean_payload)

    # ── 사용자 라벨링 리셋 (동일 트랜잭션) ────────────────────────────────────
    # 사용자 원문: "변경 시 리셋 (status 단독 제외)".
    # (c) status_transitioned 분기는 여기 도달하지 않으므로 리셋이 불필요.
    # 이 리셋은 UPSERT 와 같은 session 에서 수행되어, 호출자가 commit 하기 전
    # 예외가 발생하면 INSERT/UPDATE 도 함께 롤백된다 (atomic 경계 보장).
    _reset_user_state_on_content_change(
        session,
        old_announcement_id=old_announcement_id,
        archive_reason=_ARCHIVE_REASON_CONTENT_CHANGED,
    )

    return UpsertResult(
        announcement=new_announcement,
        action="new_version",
        needs_detail_scraping=True,
        changed_fields=changed_fields,
    )


def get_announcement_by_id(
    session: Session,
    announcement_id: int,
) -> Announcement | None:
    """내부 PK(`id`)로 공고 한 건을 조회한다.

    is_current 필터 없이 PK 로 직접 접근하므로 과거 버전(이력) 레코드도 반환할 수 있다.

    Args:
        session:          호출자가 제어하는 SQLAlchemy 세션.
        announcement_id:  `Announcement.id` (내부 PK, 자동 증가).

    Returns:
        해당 `Announcement` 인스턴스, 없으면 None.
    """
    return session.execute(
        select(Announcement).where(Announcement.id == announcement_id)
    ).scalar_one_or_none()


def upsert_announcement_detail(
    session: Session,
    source_announcement_id: str,
    detail_fields: Mapping[str, Any],
    *,
    source_type: str = "IRIS",
) -> Announcement | None:
    """공고 한 건의 상세 수집 결과 필드를 갱신한다.

    is_current=True 인 현재 버전 row 만 대상으로 한다.
    허용 컬럼: `detail_html`, `detail_text`, `detail_fetched_at`, `detail_fetch_status`.
    대상 공고가 없으면 None 을 반환한다(목록 UPSERT 가 선행되어야 한다).
    commit 은 호출자 책임이며, 여기서는 flush 까지만 수행한다.

    Args:
        session:                호출자가 제어하는 SQLAlchemy 세션.
        source_announcement_id: 갱신 대상 공고의 소스 ID.
        detail_fields:          `_DETAIL_ALLOWED_FIELDS` 키를 담은 매핑.
        source_type:            공고 소스 유형. 기본값 'IRIS'.

    Returns:
        갱신된 `Announcement` 또는 None(공고가 DB에 없는 경우).
    """
    existing = session.execute(
        select(Announcement).where(
            Announcement.source_type == source_type,
            Announcement.source_announcement_id == source_announcement_id,
            Announcement.is_current.is_(True),
        )
    ).scalar_one_or_none()

    if existing is None:
        return None

    clean_fields = _filter_payload(detail_fields, _DETAIL_ALLOWED_FIELDS)
    for field_name, field_value in clean_fields.items():
        setattr(existing, field_name, field_value)

    session.flush()
    return existing


def recompute_canonical_with_ancm_no(
    session: Session,
    source_announcement_id: str,
    *,
    source_type: str,
    ancm_no: str,
) -> bool:
    """상세 수집 후 공식 공고번호가 확보된 경우 canonical_key 를 official scheme 으로 재계산한다.

    NTIS 목록 단계에서는 공식 공고번호를 알 수 없어 fuzzy canonical 이 먼저 부여된다.
    상세 페이지 파싱 후 ntis_ancm_no 가 확보되면 이 함수를 호출해 official key 로 승급한다.

    canonical_key_scheme 이 이미 'official' 인 공고는 건드리지 않는다.
    현재 버전(is_current=True) row 만 대상으로 한다.

    Args:
        session:                호출자가 제어하는 SQLAlchemy 세션.
        source_announcement_id: 갱신 대상 공고의 소스 ID.
        source_type:            공고 소스 유형.
        ancm_no:                상세 파싱에서 추출한 공식 공고번호.

    Returns:
        True 이면 canonical 이 재계산됨. False 이면 이미 official 이거나 공고를 찾지 못함.
    """
    existing = session.execute(
        select(Announcement).where(
            Announcement.source_type == source_type,
            Announcement.source_announcement_id == source_announcement_id,
            Announcement.is_current.is_(True),
        )
    ).scalar_one_or_none()

    if existing is None:
        logger.debug(
            "canonical 재계산 대상 없음(is_current row 미존재): source={} id={}",
            source_type, source_announcement_id,
        )
        return False

    # 이미 official scheme 이면 재계산 불필요
    if existing.canonical_key_scheme == "official":
        logger.debug(
            "canonical 재계산 스킵(이미 official): source={} id={} key={}",
            source_type, source_announcement_id, existing.canonical_key,
        )
        return False

    # 기존 필드로 clean_payload 재구성 (title/agency/deadline_at 은 목록 단계에서 이미 저장됨)
    clean_payload: dict[str, Any] = {
        "title": existing.title or "",
        "agency": existing.agency,
        "deadline_at": existing.deadline_at,
        "status": existing.status,
    }

    prev_key = existing.canonical_key
    _apply_canonical(session, existing, ancm_no=ancm_no, clean_payload=clean_payload)

    logger.info(
        "canonical 재계산(fuzzy→official): source={} id={} prev_key={} new_key={} ancm_no={}",
        source_type, source_announcement_id, prev_key, existing.canonical_key, ancm_no,
    )
    return True


def list_announcements(
    session: Session,
    status: AnnouncementStatus | str | None = None,
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    source: str | None = None,
    sort: str = "received_desc",
) -> list[Announcement]:
    """공고 목록을 조회한다 (현재 버전 is_current=True 한정).

    정렬 (sort 파라미터):
        - 'received_desc' (기본): 수집일(received_at) 최신순. NULL 은 뒤로.
        - 'deadline_asc':         마감일 임박순. NULL 은 뒤로.
        - 'title_asc':            공고명 가나다순.

    기존 호출부 호환성:
        새 파라미터(source, sort)는 기본값을 가지므로 기존 호출은 변경 없이 동작한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        status:  AnnouncementStatus 또는 문자열 필터. None 이면 전체.
        limit:   페이지 크기. 1 이상이어야 한다.
        offset:  건너뛸 레코드 수(페이지네이션).
        search:  제목 부분일치(LIKE) 검색어. 공백은 무시(strip).
        source:  source_type 필터. None 이면 전체.
        sort:    정렬 기준. 허용값: received_desc | deadline_asc | title_asc.

    Returns:
        조건에 맞는 Announcement 리스트 (is_current=True 만 포함).
    """
    # is_current=True 만 표시한다 — 이력(구버전) row 는 목록에 노출하지 않는다.
    statement = select(Announcement).where(Announcement.is_current.is_(True))

    status_enum = _coerce_status(status) if status is not None else None
    statement = _apply_common_filters(statement, status_enum, source, search)
    statement = _apply_sort_order(statement, sort)

    safe_limit = max(int(limit), 1)
    safe_offset = max(int(offset), 0)
    statement = statement.offset(safe_offset).limit(safe_limit)

    return list(session.execute(statement).scalars().all())


def count_announcements(
    session: Session,
    status: AnnouncementStatus | str | None = None,
    search: str | None = None,
    source: str | None = None,
) -> int:
    """list_announcements 와 동일 조건의 전체 개수를 반환한다 (is_current=True 한정).

    목록 페이지네이션에서 '총 N건' 표시/페이지 수 계산에 사용한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        status:  상태 필터. None 이면 전체.
        search:  제목 부분일치 검색어. None 이면 검색 없음.
        source:  source_type 필터. None 이면 전체.

    Returns:
        조건에 맞는 공고 수.
    """
    statement = (
        select(func.count()).select_from(Announcement).where(Announcement.is_current.is_(True))
    )

    status_enum = _coerce_status(status) if status is not None else None
    statement = _apply_common_filters(statement, status_enum, source, search)

    return int(session.execute(statement).scalar_one())


def get_group_size_map(session: Session, canonical_group_ids: set[int]) -> dict[int, int]:
    """canonical_group_id 별 is_current=True 공고 수를 한 번의 쿼리로 반환한다.

    N+1 쿼리 없이 그룹별 배지 표시(동일 과제 N건)를 위한 집계에 사용한다.

    Args:
        session:              호출자가 제어하는 SQLAlchemy 세션.
        canonical_group_ids:  조회할 canonical_group_id 집합.

    Returns:
        {canonical_group_id: count} 매핑. 데이터 없으면 빈 dict.
    """
    if not canonical_group_ids:
        return {}
    rows = session.execute(
        select(
            Announcement.canonical_group_id,
            func.count(Announcement.id).label("cnt"),
        )
        .where(
            Announcement.is_current.is_(True),
            Announcement.canonical_group_id.in_(canonical_group_ids),
        )
        .group_by(Announcement.canonical_group_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def list_canonical_groups(
    session: Session,
    status: AnnouncementStatus | str | None = None,
    source: str | None = None,
    search: str | None = None,
    sort: str = "received_desc",
    limit: int = 50,
    offset: int = 0,
) -> list[CanonicalGroupRow]:
    """canonical 그룹 단위로 공고 목록을 조회한다 (묶어 보기 모드).

    canonical_group_id 가 있는 공고는 그룹별 대표 1건으로 묶고,
    canonical_group_id 가 없는 고아 공고는 개별 단독 그룹으로 나열한다.

    대표 선택 기준: received_at 최신순, 동률이면 id 최신순.
    필터(status/source/search)는 대표 공고의 속성에 적용된다.
    group_size 는 그룹 전체의 is_current=True 공고 수(필터 무관)이다.

    정렬과 페이지네이션은 두 쿼리(그룹 대표 + 고아) 결과를 Python에서 합산 후 적용한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        status:  상태 필터(대표 공고 기준). None 이면 전체.
        source:  소스 유형 필터(대표 공고 기준). None 이면 전체.
        search:  제목 부분일치 검색어(대표 공고 기준). None 이면 검색 없음.
        sort:    정렬 기준. received_desc | deadline_asc | title_asc.
        limit:   결과 최대 건수(페이지네이션).
        offset:  건너뛸 건수(페이지네이션).

    Returns:
        CanonicalGroupRow 리스트. 정렬·페이지네이션 적용됨.
    """
    status_enum = _coerce_status(status) if status is not None else None

    # ── window function 서브쿼리: 그룹별 대표 선택 + group_size 집계 ─────────────
    # row_number() 로 그룹 내 대표를 선택하고, count() 로 그룹 전체 크기를 구한다.
    ranked_subq = (
        select(
            Announcement.id.label("ann_id"),
            Announcement.canonical_group_id.label("gid"),
            func.count(Announcement.id)
            .over(partition_by=Announcement.canonical_group_id)
            .label("grp_size"),
            func.row_number()
            .over(
                partition_by=Announcement.canonical_group_id,
                order_by=[
                    (Announcement.received_at.is_(None)).asc(),  # NULL 뒤로
                    Announcement.received_at.desc(),
                    Announcement.id.desc(),
                ],
            )
            .label("rn"),
        )
        .where(
            Announcement.is_current.is_(True),
            Announcement.canonical_group_id.is_not(None),
        )
        .subquery()
    )

    # rn=1 인 대표 행만 추린다.
    rep_subq = (
        select(
            ranked_subq.c.ann_id,
            ranked_subq.c.gid,
            ranked_subq.c.grp_size,
        )
        .where(ranked_subq.c.rn == 1)
        .subquery()
    )

    # 대표 Announcement 조인 + 사용자 필터 적용
    grouped_stmt = select(Announcement, rep_subq.c.grp_size).join(
        rep_subq, Announcement.id == rep_subq.c.ann_id
    )
    grouped_stmt = _apply_common_filters(grouped_stmt, status_enum, source, search)
    grouped_rows: list[tuple[Announcement, int]] = list(
        session.execute(grouped_stmt).all()
    )

    # ── 고아 공고 (canonical_group_id IS NULL): 각각 단독 그룹 ─────────────────
    orphan_stmt = select(Announcement).where(
        Announcement.is_current.is_(True),
        Announcement.canonical_group_id.is_(None),
    )
    orphan_stmt = _apply_common_filters(orphan_stmt, status_enum, source, search)
    orphan_rows: list[Announcement] = list(session.execute(orphan_stmt).scalars().all())

    # ── 합산 → Python 정렬 → 페이지네이션 ──────────────────────────────────────
    all_rows: list[CanonicalGroupRow] = [
        CanonicalGroupRow(
            canonical_group_id=ann.canonical_group_id,
            canonical_key=ann.canonical_key,
            representative=ann,
            group_size=cnt,
        )
        for ann, cnt in grouped_rows
    ] + [
        CanonicalGroupRow(
            canonical_group_id=None,
            canonical_key=None,
            representative=ann,
            group_size=1,
        )
        for ann in orphan_rows
    ]

    all_rows.sort(key=lambda row: _group_row_sort_key(row.representative, sort))

    safe_offset = max(int(offset), 0)
    safe_limit = max(int(limit), 1)
    return all_rows[safe_offset : safe_offset + safe_limit]


def count_canonical_groups(
    session: Session,
    status: AnnouncementStatus | str | None = None,
    source: str | None = None,
    search: str | None = None,
) -> int:
    """list_canonical_groups 와 동일 조건의 전체 그룹 수를 반환한다.

    페이지네이션에서 총 그룹 수 계산에 사용한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        status:  상태 필터(대표 공고 기준). None 이면 전체.
        source:  소스 유형 필터(대표 공고 기준). None 이면 전체.
        search:  제목 부분일치 검색어(대표 공고 기준). None 이면 검색 없음.

    Returns:
        조건에 맞는 canonical 그룹 수 (고아 공고 + 그룹 대표 합산).
    """
    status_enum = _coerce_status(status) if status is not None else None

    # 그룹 대표 수 — ranked_subq 재사용
    ranked_subq = (
        select(
            Announcement.id.label("ann_id"),
            Announcement.canonical_group_id.label("gid"),
            func.row_number()
            .over(
                partition_by=Announcement.canonical_group_id,
                order_by=[
                    (Announcement.received_at.is_(None)).asc(),
                    Announcement.received_at.desc(),
                    Announcement.id.desc(),
                ],
            )
            .label("rn"),
        )
        .where(
            Announcement.is_current.is_(True),
            Announcement.canonical_group_id.is_not(None),
        )
        .subquery()
    )
    rep_subq = (
        select(ranked_subq.c.ann_id, ranked_subq.c.gid)
        .where(ranked_subq.c.rn == 1)
        .subquery()
    )

    grouped_count_stmt = (
        select(func.count())
        .select_from(Announcement)
        .join(rep_subq, Announcement.id == rep_subq.c.ann_id)
    )
    grouped_count_stmt = _apply_common_filters(grouped_count_stmt, status_enum, source, search)
    grouped_count = int(session.execute(grouped_count_stmt).scalar_one())

    # 고아 공고 수
    orphan_count_stmt = (
        select(func.count())
        .select_from(Announcement)
        .where(
            Announcement.is_current.is_(True),
            Announcement.canonical_group_id.is_(None),
        )
    )
    orphan_count_stmt = _apply_common_filters(orphan_count_stmt, status_enum, source, search)
    orphan_count = int(session.execute(orphan_count_stmt).scalar_one())

    return grouped_count + orphan_count


def get_available_source_ids() -> list[str]:
    """sources.yaml 에 등록된 전체 소스 ID 목록을 반환한다.

    활성화(enabled=True) 여부와 무관하게 모든 소스를 반환한다.
    필터 UI 에서 소스 목록을 동적으로 생성할 때 사용한다.

    Returns:
        소스 ID 문자열 목록. 예: ['IRIS', 'NTIS']
    """
    config = load_sources_config()
    return [source.id for source in config.sources]


# ── 첨부파일(Attachment) UPSERT 키 기준 허용 필드 ────────────────────────────────────
# downloaded_at 은 NOT NULL 이므로 payload 에 항상 포함해야 한다.
_ATTACHMENT_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "announcement_id",
        "original_filename",
        "stored_path",
        "file_ext",
        "file_size",
        "download_url",
        "sha256",
        "downloaded_at",
    }
)


def upsert_attachment(
    session: Session,
    payload: Mapping[str, Any],
) -> tuple[Attachment, bool]:
    """첨부파일 한 건을 증분 UPSERT 한다.

    UPSERT 키: `(announcement_id, original_filename)`.
    기존 레코드가 있으면 `sha256` 을 비교한다.
        - 동일 → 스킵(기존 레코드 반환, upserted=False)
        - 변경 → 저장 경로·크기·해시·다운로드 시각을 in-place UPDATE (upserted=True)
    기존 레코드가 없으면 INSERT (upserted=True).

    `downloaded_at` 은 NOT NULL 컬럼이므로 payload 에 반드시 포함해야 한다.
    commit 은 호출자 책임이며, 여기서는 flush 까지만 수행한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        payload: 첨부파일 속성 매핑.
                 필수 키: `announcement_id`, `original_filename`, `stored_path`,
                          `file_ext`, `downloaded_at`.

    Returns:
        (Attachment 인스턴스, upserted 여부).
        upserted=True: 신규 삽입 또는 변경된 경우. False: sha256 동일로 스킵.

    Raises:
        KeyError: 필수 키가 payload 에 없을 때.
    """
    for required_key in ("announcement_id", "original_filename", "stored_path", "file_ext", "downloaded_at"):
        if required_key not in payload:
            raise KeyError(f"첨부파일 payload 에 {required_key!r} 가 반드시 포함되어야 합니다.")

    clean = _filter_payload(payload, _ATTACHMENT_ALLOWED_FIELDS)
    announcement_id: int = clean["announcement_id"]
    original_filename: str = clean["original_filename"]

    existing = session.execute(
        select(Attachment).where(
            Attachment.announcement_id == announcement_id,
            Attachment.original_filename == original_filename,
        )
    ).scalar_one_or_none()

    # ── 신규 삽입 ─────────────────────────────────────────────────────────────
    if existing is None:
        attachment = Attachment(**clean)
        session.add(attachment)
        session.flush()
        return attachment, True

    # ── sha256 비교 — 동일하면 스킵 ───────────────────────────────────────────
    incoming_sha256 = clean.get("sha256")
    if incoming_sha256 and existing.sha256 == incoming_sha256:
        return existing, False

    # ── 변경됨 → in-place UPDATE ──────────────────────────────────────────────
    for field_name in ("stored_path", "file_ext", "file_size", "download_url", "sha256", "downloaded_at"):
        if field_name in clean:
            setattr(existing, field_name, clean[field_name])

    session.flush()
    return existing, True


def get_attachment_by_id(
    session: Session,
    attachment_id: int,
) -> Attachment | None:
    """내부 PK 로 첨부파일 레코드를 조회한다.

    Args:
        session:       호출자가 제어하는 SQLAlchemy 세션.
        attachment_id: 첨부파일 내부 PK.

    Returns:
        `Attachment` 인스턴스 또는 None.
    """
    return session.get(Attachment, attachment_id)


def get_attachment_by_announcement_and_filename(
    session: Session,
    announcement_id: int,
    original_filename: str,
) -> Attachment | None:
    """공고 ID와 원본 파일명으로 첨부파일 레코드를 조회한다.

    존재하지 않으면 None 을 반환한다.

    Args:
        session:           호출자가 제어하는 SQLAlchemy 세션.
        announcement_id:   소속 공고의 내부 PK.
        original_filename: 원본 파일명 (소스가 제공하는 그대로).

    Returns:
        `Attachment` 인스턴스 또는 None.
    """
    return session.execute(
        select(Attachment).where(
            Attachment.announcement_id == announcement_id,
            Attachment.original_filename == original_filename,
        )
    ).scalar_one_or_none()


def get_attachments_by_announcement(
    session: Session,
    announcement_id: int,
) -> list[Attachment]:
    """공고 ID에 속한 모든 첨부파일 레코드를 반환한다.

    결과는 `id` 오름차순으로 정렬된다.

    Args:
        session:         호출자가 제어하는 SQLAlchemy 세션.
        announcement_id: 소속 공고의 내부 PK.

    Returns:
        `Attachment` 리스트. 첨부파일이 없으면 빈 리스트.
    """
    return list(
        session.execute(
            select(Attachment)
            .where(Attachment.announcement_id == announcement_id)
            .order_by(Attachment.id.asc())
        )
        .scalars()
        .all()
    )


# ──────────────────────────────────────────────────────────────
# 사용자 읽음 상태 (Phase 1b)
# ──────────────────────────────────────────────────────────────


def get_read_announcement_id_set(
    session: Session,
    *,
    user_id: int,
    announcement_ids: Iterable[int],
) -> set[int]:
    """주어진 사용자가 '이미 읽은' 공고 id 집합을 한 번의 쿼리로 반환한다.

    목록 페이지의 bold/normal 분기를 위한 N+1 방지 헬퍼다. 현재 페이지에
    보이는 announcement id 집합만 IN 절로 질의하므로, 페이지당 추가 쿼리는
    정확히 1회다. 매칭되는 AnnouncementUserState 가 `is_read=True` 인 row 의
    `announcement_id` 만 모아서 반환한다.

    호출 규약:
        - user_id 는 인증된 사용자 PK (비로그인 경로는 이 함수 자체를
          호출하지 않는다 — 라우트에서 분기).
        - announcement_ids 가 비어 있거나 None-only 이면 쿼리 없이 빈 set.

    Args:
        session:           호출자 세션.
        user_id:           조회 대상 사용자 PK.
        announcement_ids:  현재 페이지에 보이는 announcement id 들.

    Returns:
        ``is_read=True`` 가 확정된 announcement_id 집합. 미존재/미읽음은
        포함되지 않는다.
    """
    # Iterable 을 한 번만 소비해도 되도록 list 로 고정한 뒤 None 제거.
    announcement_id_list = [aid for aid in announcement_ids if aid is not None]
    if not announcement_id_list:
        return set()

    rows = session.execute(
        select(AnnouncementUserState.announcement_id).where(
            AnnouncementUserState.user_id == user_id,
            AnnouncementUserState.announcement_id.in_(announcement_id_list),
            AnnouncementUserState.is_read.is_(True),
        )
    ).all()
    return {row[0] for row in rows}


def mark_announcement_read(
    session: Session,
    *,
    user_id: int,
    announcement_id: int,
    now: datetime | None = None,
) -> AnnouncementUserState:
    """공고 상세 진입 시 (announcement_id, user_id) 단위로 읽음 상태를 UPSERT 한다.

    동작:
        - 매칭되는 AnnouncementUserState 가 있으면 ``is_read=True`` 로 갱신하고
          ``read_at`` 을 ``now`` 로 덮어쓴다. ``updated_at`` 은 onupdate 로 자동.
        - 없으면 새 row 를 INSERT 한다.

    announcement 단위 (IRIS 공고를 읽었어도 동일 canonical 의 NTIS 공고
    row 는 영향을 받지 않음) — FK 가 announcement_id 이므로 자연스럽게 성립.

    호출 규약:
        - 호출자(라우트)가 announcement 의 존재를 먼저 확인해야 한다 (FK 위반
          방지). 상세 페이지 라우트는 404 분기 이후에만 이 함수를 부른다.
        - commit 은 호출자 책임. 이 함수는 flush 까지만 수행한다.

    Args:
        session:         호출자 세션.
        user_id:         인증된 사용자 PK.
        announcement_id: 읽음 처리할 공고 PK.
        now:             read_at 에 찍을 시각(UTC). None 이면 _utcnow.

    Returns:
        읽음 상태가 적용된 ``AnnouncementUserState`` 인스턴스.
    """
    now_ts = now if now is not None else datetime.now(tz=UTC)

    existing = session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == announcement_id,
            AnnouncementUserState.user_id == user_id,
        )
    ).scalar_one_or_none()

    if existing is None:
        # 신규 row — UNIQUE(ann_id, user_id) 제약에 의해 중복 시 에러는
        # 호출자의 동시성 레이어가 처리한다 (웹 요청당 세션 독립이므로 실제
        # 충돌 가능성은 낮다).
        created = AnnouncementUserState(
            announcement_id=announcement_id,
            user_id=user_id,
            is_read=True,
            read_at=now_ts,
        )
        session.add(created)
        session.flush()
        return created

    # 기존 row 가 있는 경우: is_read 를 True 로 고정하고 read_at 을 갱신.
    # Phase 1a 의 내용 변경 리셋으로 is_read=False 로 돌아간 row 도 자연스럽게
    # 재활성화된다.
    existing.is_read = True
    existing.read_at = now_ts
    session.flush()
    return existing


def resolve_announcement_ids_by_filter(
    session: Session,
    *,
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
) -> list[int]:
    """필터 조건에 맞는 is_current=True 공고 id 목록을 반환한다.

    list_announcements 와 동일한 필터를 적용하되, 페이지네이션과 정렬 없이
    전체 id 를 반환한다. bulk 읽음 처리의 "filter" 모드에서 사용한다.

    Args:
        session: 호출자 세션.
        status:  상태 값 문자열. None 이면 전체.
        source:  source_type 문자열. None 이면 전체.
        search:  제목 부분일치 검색어. None 이면 검색 없음.

    Returns:
        조건에 맞는 announcement id 리스트 (순서 미보장).
    """
    status_enum = _coerce_status(status) if status is not None else None
    statement = select(Announcement.id).where(Announcement.is_current.is_(True))
    statement = _apply_common_filters(statement, status_enum, source, search)
    return list(session.execute(statement).scalars().all())


def bulk_mark_announcements_read(
    session: Session,
    *,
    user_id: int,
    announcement_ids: list[int],
    now: datetime | None = None,
) -> int:
    """announcement_ids 에 대해 AnnouncementUserState 를 일괄 read=True 로 UPSERT 한다.

    기존 row 가 있으면 is_read=True + read_at 갱신. 없으면 INSERT.
    변경(INSERT 또는 UPDATE)된 row 수를 반환한다.

    Args:
        session:          호출자 세션.
        user_id:          인증된 사용자 PK.
        announcement_ids: 읽음 처리할 공고 PK 목록.
        now:              read_at 에 찍을 시각. None 이면 UTC 현재 시각.

    Returns:
        실제로 INSERT 또는 업데이트된 row 수.
    """
    if not announcement_ids:
        return 0

    now_ts = now if now is not None else datetime.now(tz=UTC)

    existing_rows = session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.user_id == user_id,
            AnnouncementUserState.announcement_id.in_(announcement_ids),
        )
    ).scalars().all()

    existing_map = {row.announcement_id: row for row in existing_rows}
    updated = 0

    new_rows: list[AnnouncementUserState] = []
    for aid in announcement_ids:
        if aid in existing_map:
            row = existing_map[aid]
            if not row.is_read or row.read_at != now_ts:
                row.is_read = True
                row.read_at = now_ts
                updated += 1
        else:
            new_rows.append(
                AnnouncementUserState(
                    announcement_id=aid,
                    user_id=user_id,
                    is_read=True,
                    read_at=now_ts,
                )
            )

    if new_rows:
        session.add_all(new_rows)
        updated += len(new_rows)

    if updated:
        session.flush()
    return updated


def bulk_mark_announcements_unread(
    session: Session,
    *,
    user_id: int,
    announcement_ids: list[int],
    now: datetime | None = None,
) -> int:
    """announcement_ids 에 대해 AnnouncementUserState 를 일괄 read=False 로 UPSERT 한다.

    기존 row 가 있으면 is_read=False + read_at=None 갱신. 없으면 INSERT.
    변경(INSERT 또는 업데이트)된 row 수를 반환한다.

    Args:
        session:          호출자 세션.
        user_id:          인증된 사용자 PK.
        announcement_ids: 읽지 않음 처리할 공고 PK 목록.
        now:              타임스탬프 오버라이드 (사용 안 함 — 인터페이스 일관성 유지).

    Returns:
        실제로 INSERT 또는 업데이트된 row 수.
    """
    if not announcement_ids:
        return 0

    existing_rows = session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.user_id == user_id,
            AnnouncementUserState.announcement_id.in_(announcement_ids),
        )
    ).scalars().all()

    existing_map = {row.announcement_id: row for row in existing_rows}
    updated = 0

    new_rows: list[AnnouncementUserState] = []
    for aid in announcement_ids:
        if aid in existing_map:
            row = existing_map[aid]
            if row.is_read:
                row.is_read = False
                row.read_at = None
                updated += 1
        else:
            new_rows.append(
                AnnouncementUserState(
                    announcement_id=aid,
                    user_id=user_id,
                    is_read=False,
                    read_at=None,
                )
            )

    if new_rows:
        session.add_all(new_rows)
        updated += len(new_rows)

    if updated:
        session.flush()
    return updated


# ──────────────────────────────────────────────────────────────
# 관련성 판정 (Phase 3a / 00035-2)
# ──────────────────────────────────────────────────────────────


def get_canonical_project_by_id(
    session: Session,
    canonical_project_id: int,
) -> CanonicalProject | None:
    """PK 로 CanonicalProject 를 조회한다. 없으면 None."""
    return session.execute(
        select(CanonicalProject).where(CanonicalProject.id == canonical_project_id)
    ).scalar_one_or_none()


def get_relevance_judgment(
    session: Session,
    *,
    canonical_project_id: int,
    user_id: int,
) -> RelevanceJudgment | None:
    """특정 유저의 특정 canonical 에 대한 현재 판정을 반환한다. 없으면 None."""
    return session.execute(
        select(RelevanceJudgment).where(
            RelevanceJudgment.canonical_project_id == canonical_project_id,
            RelevanceJudgment.user_id == user_id,
        )
    ).scalar_one_or_none()


def get_relevance_by_canonical_id_map(
    session: Session,
    canonical_project_ids: Iterable[int],
) -> dict[int, list[RelevanceJudgment]]:
    """canonical_project_id 목록에 대한 현재 판정을 bulk 조회한다.

    N+1 방지: IN 절 단일 쿼리 + selectinload(RelevanceJudgment.user).
    반환: {canonical_project_id: [RelevanceJudgment, ...]} — 판정 없는 id 는 키 없음.
    """
    ids = list(canonical_project_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(RelevanceJudgment)
        .where(RelevanceJudgment.canonical_project_id.in_(ids))
        .options(selectinload(RelevanceJudgment.user))
    ).scalars().all()
    result: dict[int, list[RelevanceJudgment]] = {}
    for rj in rows:
        result.setdefault(rj.canonical_project_id, []).append(rj)
    return result


def get_relevance_history_by_canonical_id_map(
    session: Session,
    canonical_project_ids: Iterable[int],
) -> dict[int, list[HistoryWithUser]]:
    """canonical_project_id 목록에 대한 판정 히스토리를 bulk 조회한다.

    RelevanceJudgmentHistory 에 User relationship 이 없으므로 JOIN 사용.
    반환: {canonical_project_id: [HistoryWithUser, ...]} — 히스토리 없는 id 는 키 없음.
    """
    ids = list(canonical_project_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(RelevanceJudgmentHistory, User.username)
        .join(User, User.id == RelevanceJudgmentHistory.user_id)
        .where(RelevanceJudgmentHistory.canonical_project_id.in_(ids))
        .order_by(RelevanceJudgmentHistory.archived_at.desc())
    ).all()
    result: dict[int, list[HistoryWithUser]] = {}
    for hist, username in rows:
        result.setdefault(hist.canonical_project_id, []).append(
            HistoryWithUser(history=hist, username=username)
        )
    return result


def get_relevance_history(
    session: Session,
    *,
    canonical_project_id: int,
) -> list[HistoryWithUser]:
    """단일 canonical 의 판정 히스토리를 최신순으로 반환한다."""
    rows = session.execute(
        select(RelevanceJudgmentHistory, User.username)
        .join(User, User.id == RelevanceJudgmentHistory.user_id)
        .where(RelevanceJudgmentHistory.canonical_project_id == canonical_project_id)
        .order_by(RelevanceJudgmentHistory.archived_at.desc())
    ).all()
    return [HistoryWithUser(history=hist, username=username) for hist, username in rows]


def set_relevance_judgment(
    session: Session,
    *,
    canonical_project_id: int,
    user_id: int,
    verdict: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> RelevanceJudgment:
    """판정을 저장한다. 기존 판정이 있으면 History 이관 후 새 판정으로 교체한다.

    트랜잭션 순서: History INSERT → flush → 기존 DELETE → flush → 신규 INSERT.
    순서를 바꾸면 uq_relevance_project_user UNIQUE 제약 위반이 발생한다.

    Args:
        session: 호출자 세션.
        canonical_project_id: 판정 대상 CanonicalProject PK.
        user_id: 판정 주체 User PK.
        verdict: RELEVANCE_ALLOWED_VERDICTS 중 하나.
        reason: 판정 사유 (선택).
        now: 타임스탬프 오버라이드 (테스트용). None 이면 datetime.now(UTC).

    Returns:
        새로 삽입된 RelevanceJudgment.

    Raises:
        ValueError: verdict 가 RELEVANCE_ALLOWED_VERDICTS 에 없을 때.
    """
    if verdict not in RELEVANCE_ALLOWED_VERDICTS:
        raise ValueError(
            f"알 수 없는 verdict: {verdict!r}. 허용: {sorted(RELEVANCE_ALLOWED_VERDICTS)}"
        )
    now_ts = now or datetime.now(UTC)

    existing = get_relevance_judgment(
        session, canonical_project_id=canonical_project_id, user_id=user_id
    )

    if existing is not None:
        # 1) History INSERT
        hist = RelevanceJudgmentHistory(
            canonical_project_id=existing.canonical_project_id,
            user_id=existing.user_id,
            verdict=existing.verdict,
            reason=existing.reason,
            decided_at=existing.decided_at,
            archived_at=now_ts,
            archive_reason=_ARCHIVE_REASON_USER_OVERWRITE,
        )
        session.add(hist)
        session.flush()
        # 2) 기존 DELETE
        session.delete(existing)
        session.flush()

    # 3) 신규 INSERT
    new_judgment = RelevanceJudgment(
        canonical_project_id=canonical_project_id,
        user_id=user_id,
        verdict=verdict,
        reason=reason,
        decided_at=now_ts,
    )
    session.add(new_judgment)
    session.flush()
    return new_judgment


def delete_relevance_judgment(
    session: Session,
    *,
    canonical_project_id: int,
    user_id: int,
    now: datetime | None = None,
) -> bool:
    """판정을 삭제한다. 삭제 전 History 이관(user_overwrite).

    Args:
        session: 호출자 세션.
        canonical_project_id: 판정 대상 CanonicalProject PK.
        user_id: 판정 주체 User PK.
        now: 타임스탬프 오버라이드 (테스트용).

    Returns:
        True 이면 삭제 성공, False 이면 판정이 없어서 no-op.
    """
    existing = get_relevance_judgment(
        session, canonical_project_id=canonical_project_id, user_id=user_id
    )
    if existing is None:
        return False

    now_ts = now or datetime.now(UTC)
    hist = RelevanceJudgmentHistory(
        canonical_project_id=existing.canonical_project_id,
        user_id=existing.user_id,
        verdict=existing.verdict,
        reason=existing.reason,
        decided_at=existing.decided_at,
        archived_at=now_ts,
        archive_reason=_ARCHIVE_REASON_USER_OVERWRITE,
    )
    session.add(hist)
    session.flush()
    session.delete(existing)
    session.flush()
    return True


# ──────────────────────────────────────────────────────────────
# ScrapeRun — Phase 2 / 00025 수집 실행 제어 (docs/scrape_control_design.md §7)
# ──────────────────────────────────────────────────────────────
#
# 모든 ScrapeRun 헬퍼는 기존 repository 규약을 따른다:
#   - 호출자가 전달한 session 을 그대로 사용한다 (새 세션을 열지 않는다).
#   - flush 까지만 수행하고 commit 은 호출자의 session_scope 가 담당한다.
#   - status / trigger 도메인은 app/db/models.py 상수로 고정(외부 문자열 차단).


def _validate_scrape_run_trigger(trigger: str) -> str:
    """trigger 문자열이 DB CHECK 제약과 동일한 도메인에 속하는지 검증한다.

    service 레이어에서 동일 검증을 중복 수행하지만, repository 직접 호출에서도
    안전하도록 여기서도 최종 방어선을 둔다. 불일치면 ``ValueError``.
    """
    if trigger not in SCRAPE_RUN_TRIGGERS:
        raise ValueError(
            f"알 수 없는 ScrapeRun.trigger 값: {trigger!r}. "
            f"허용: {sorted(SCRAPE_RUN_TRIGGERS)}"
        )
    return trigger


def _validate_scrape_run_status(status: str) -> str:
    """status 문자열이 DB CHECK 제약과 동일한 도메인인지 검증한다."""
    if status not in SCRAPE_RUN_STATUSES:
        raise ValueError(
            f"알 수 없는 ScrapeRun.status 값: {status!r}. "
            f"허용: {sorted(SCRAPE_RUN_STATUSES)}"
        )
    return status


def get_running_scrape_run(session: Session) -> ScrapeRun | None:
    """status='running' 인 ScrapeRun 을 조회한다.

    정상 운영에서는 0 또는 1개. 2개 이상이라면 stale cleanup 이 필요한 상태이며,
    본 함수는 ``started_at`` 내림차순 첫 번째 row 를 반환한다 (가장 최근에
    기동된 실행을 활성 lock 으로 간주).

    Args:
        session: 호출자 세션.

    Returns:
        진행 중인 ``ScrapeRun`` 또는 ``None``.
    """
    return session.execute(
        select(ScrapeRun)
        .where(ScrapeRun.status == "running")
        .order_by(ScrapeRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_recent_scrape_runs(
    session: Session,
    *,
    limit: int = 20,
) -> list[ScrapeRun]:
    """최근 ScrapeRun 을 started_at 내림차순으로 반환한다.

    관리자 UI 의 [수집 제어] 탭에서 최근 이력을 표시하는 데 쓴다.

    Args:
        session: 호출자 세션.
        limit:   반환할 최대 row 수. 양의 정수.

    Returns:
        최근 ``ScrapeRun`` 목록 (최신순). 데이터가 없으면 빈 리스트.
    """
    if limit <= 0:
        raise ValueError(f"limit 는 양의 정수여야 합니다: {limit!r}")
    return list(
        session.execute(
            select(ScrapeRun)
            .order_by(ScrapeRun.started_at.desc())
            .limit(limit)
        ).scalars()
    )


def create_scrape_run(
    session: Session,
    *,
    trigger: str,
    source_counts: dict[str, Any] | None = None,
) -> ScrapeRun:
    """running 상태의 ScrapeRun row 를 생성한다.

    Lock 의무:
        호출자는 이 함수를 부르기 전에 :func:`get_running_scrape_run` 으로
        현재 running row 가 없음을 확인해야 한다. 동일 세션·동일 트랜잭션에서
        수행하면 SELECT→INSERT 사이 race 가 방지된다 (SQLite 는 기본
        serialized, Postgres 전환 시 advisory lock 으로 업그레이드 고려).
        본 함수 자체는 중복 running row 를 강제로 막지 않는다 — 호출자 책임.

    Args:
        session:       호출자 세션.
        trigger:       'manual' / 'scheduled' / 'cli' 중 하나. 그 외는 ValueError.
        source_counts: 초기 JSON (예: {\"active_sources\": [\"IRIS\"]}).
                       None 이면 빈 dict.

    Returns:
        flush 완료된 ``ScrapeRun`` 인스턴스 (id 할당됨).
    """
    _validate_scrape_run_trigger(trigger)
    row = ScrapeRun(
        trigger=trigger,
        status="running",
        source_counts=source_counts if source_counts is not None else {},
        # started_at 은 모델의 default=_utcnow 가 채운다.
    )
    session.add(row)
    session.flush()
    logger.info(
        "ScrapeRun 생성: id={} trigger={} source_counts={}",
        row.id, row.trigger, row.source_counts,
    )
    return row


def set_scrape_run_pid(session: Session, run_id: int, pid: int) -> None:
    """ScrapeRun.pid 를 갱신한다.

    subprocess.Popen 직후 호출해 중단 경로(``os.killpg``) 가 쓸 pid 를 기록한다.
    cli 경로는 자기 자신의 pid 를 기록해 stale cleanup 로직과 일관성을 맞춘다.

    Args:
        session: 호출자 세션.
        run_id:  대상 ScrapeRun PK.
        pid:     양의 정수. 0 이하면 ValueError.

    Raises:
        ValueError: run_id 에 해당하는 row 가 없거나 pid 가 비정상값.
    """
    if pid <= 0:
        raise ValueError(f"pid 는 양의 정수여야 합니다: {pid!r}")
    row = session.get(ScrapeRun, run_id)
    if row is None:
        raise ValueError(f"ScrapeRun id={run_id} 를 찾을 수 없습니다.")
    row.pid = pid
    session.flush()


def finalize_scrape_run(
    session: Session,
    run_id: int,
    *,
    status: str,
    source_counts: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> bool:
    """ScrapeRun 을 terminal 상태로 마감한다 (idempotent).

    동작:
        - 대상 row 가 이미 terminal 이면 변경 없이 False 반환.
          cli 가 자체 finalize 한 뒤 웹의 watcher 가 subprocess 종료를 보고
          재호출하는 케이스를 안전하게 처리하기 위함 (설계 문서 §9.2).
        - running 이면 ended_at=_utcnow(), status/source_counts/error_message
          를 업데이트하고 True 반환.

    Args:
        session:       호출자 세션.
        run_id:        대상 ScrapeRun PK.
        status:        SCRAPE_RUN_TERMINAL_STATUSES 중 하나. running 은 금지.
        source_counts: 최종 요약. None 이면 기존 값 유지.
        error_message: 실패·부분 성공 시 진단 메시지.

    Returns:
        실제로 마감이 적용되었으면 True. 이미 terminal 이었으면 False.

    Raises:
        ValueError: run_id 없음 / status 가 허용 값이 아님 / status='running' 을 전달.
    """
    _validate_scrape_run_status(status)
    if status == "running":
        raise ValueError(
            "finalize_scrape_run 에 status='running' 을 전달할 수 없습니다. "
            "terminal 상태(completed/cancelled/failed/partial) 중 하나를 넘기세요."
        )

    row = session.get(ScrapeRun, run_id)
    if row is None:
        raise ValueError(f"ScrapeRun id={run_id} 를 찾을 수 없습니다.")

    if row.is_terminal():
        # 이미 마감된 row — 호출자가 안심하고 재호출할 수 있도록 no-op.
        logger.debug(
            "finalize_scrape_run skip (이미 terminal): id={} 기존 status={!r} "
            "재요청 status={!r}",
            run_id, row.status, status,
        )
        return False

    row.status = status
    row.ended_at = datetime.now(tz=UTC)
    if source_counts is not None:
        row.source_counts = source_counts
    if error_message is not None:
        row.error_message = error_message
    session.flush()
    logger.info(
        "ScrapeRun 마감: id={} status={!r} ended_at={} error_message={!r}",
        row.id, row.status,
        row.ended_at.isoformat() if row.ended_at else None,
        error_message,
    )
    return True


def fail_stale_running_runs(
    session: Session,
    *,
    pid_alive_checker: Any = None,
) -> int:
    """web startup 시점의 stale cleanup — 미관리 상태의 running row 를 failed 로 정리한다.

    판정 규칙 (사용자 원문: "pid 없는 running row 를 failed 로 정리"):
        - pid IS NULL 이면 Popen 직전에 죽은 실행 — 무조건 stale.
        - pid IS NOT NULL 이지만 해당 프로세스가 존재하지 않으면 stale.
          기본 체커(``pid_alive_checker=None``) 는 ``os.kill(pid, 0)`` 시도 후
          ``ProcessLookupError`` 를 stale 로, 그 외 예외는 살아있는 것으로 해석.
        - pid 가 살아 있어도 **다른 프로세스** 일 수 있지만(재부팅 후 동일 pid 재할당),
          본 stale cleanup 은 안전 쪽으로 "살아있으면 그대로 두기" 를 택한다.
          완전한 orphan 감지는 향후 호스트 측 컨테이너 상태 조회로 보강 가능.

    모든 매칭 row 는 status='failed', ended_at=_utcnow(),
    error_message='stale (web restart)' 로 마감된다.

    Args:
        session:           호출자 세션.
        pid_alive_checker: 테스트 주입용 콜러블 ``(pid: int) -> bool``.
                           True 면 살아있는 것으로 간주. None 이면 os.kill 기본 구현.

    Returns:
        stale 로 마감된 row 수.
    """
    import os

    def _default_checker(pid: int) -> bool:
        """os.kill(pid, 0) 으로 프로세스 존재 여부 확인.

        Linux/macOS 에서는 존재하면 no-op, 없으면 ProcessLookupError.
        권한 문제(PermissionError) 는 "살아 있지만 내가 못 건드림" 으로 해석.
        """
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # 다른 유저의 프로세스 — 존재는 함. 보수적으로 살아있다고 본다.
            return True
        except OSError:
            # 기타 예외는 stale 로 취급해 복구 가능하게 한다.
            return False

    checker = pid_alive_checker if pid_alive_checker is not None else _default_checker

    running_rows = session.execute(
        select(ScrapeRun).where(ScrapeRun.status == "running")
    ).scalars().all()

    cleaned = 0
    now_ts = datetime.now(tz=UTC)
    for row in running_rows:
        is_stale = False
        if row.pid is None:
            is_stale = True
            reason = "pid_null"
        elif not checker(row.pid):
            is_stale = True
            reason = f"pid_{row.pid}_not_found"
        else:
            reason = "pid_alive"

        if not is_stale:
            logger.warning(
                "ScrapeRun id={} 는 pid={} 가 살아 있어 stale cleanup 에서 건너뜁니다. "
                "수동으로 상태를 확인하세요.",
                row.id, row.pid,
            )
            continue

        row.status = "failed"
        row.ended_at = now_ts
        row.error_message = f"stale (web restart, {reason})"
        cleaned += 1
        logger.info(
            "ScrapeRun id={} 를 stale 로 마감: pid={} reason={}",
            row.id, row.pid, reason,
        )

    if cleaned:
        session.flush()
    return cleaned


# ──────────────────────────────────────────────────────────────
# 즐겨찾기 (Phase 3b / 00036)
# ──────────────────────────────────────────────────────────────


def get_favorite_announcement_id_set(
    session: Session,
    *,
    user_id: int,
    announcement_ids: Iterable[int],
) -> set[int]:
    """user_id 가 즐겨찾기한 announcement_id 집합을 1회 쿼리로 반환한다.

    task 00037 부터 FavoriteEntry 가 announcement 단위로 저장되므로, 목록 페이지의
    별 아이콘 표시용 N+1 방지 헬퍼도 announcement_id 기준으로 동작한다.
    현재 페이지에 보이는 announcement_id 집합만 IN 절로 질의한다.

    호출 규약:
        - 비로그인 경로에서는 이 함수를 호출하지 않는다 (라우트에서 분기).
        - announcement_ids 가 비어 있으면 쿼리 없이 빈 set.

    Args:
        session:          호출자 세션.
        user_id:          인증된 사용자 PK.
        announcement_ids: 현재 페이지에 보이는 announcement.id 들.

    Returns:
        user_id 가 임의 폴더에 즐겨찾기한 announcement_id 집합.
        즐겨찾기되지 않은 id 는 포함되지 않는다.
    """
    id_list = [aid for aid in announcement_ids if aid is not None]
    if not id_list:
        return set()

    rows = session.execute(
        select(FavoriteEntry.announcement_id)
        .join(FavoriteFolder, FavoriteEntry.folder_id == FavoriteFolder.id)
        .where(
            FavoriteFolder.user_id == user_id,
            FavoriteEntry.announcement_id.in_(id_list),
        )
        .distinct()
    ).all()
    return {row[0] for row in rows}


def get_favorite_entry_map(
    session: Session,
    *,
    user_id: int,
    announcement_ids: Iterable[int],
) -> dict[int, int]:
    """announcement_id → entry_id 매핑 반환 (현재 사용자의 즐겨찾기).

    task 00037 부터 announcement 단위로 저장되므로, 이 맵의 키는 announcement.id 다.
    같은 공고가 여러 폴더에 담겨 있을 때는 가장 오래된(id 최솟값) entry_id 를 반환한다.
    별 아이콘의 초기 ``data-entry-id`` 설정 및 채워진 별(★) 판단에 사용한다.

    Args:
        session:          호출자 세션.
        user_id:          현재 로그인 사용자 PK.
        announcement_ids: 조회할 announcement.id 목록.

    Returns:
        ``{announcement_id: entry_id}``
        즐겨찾기되지 않은 announcement_id 는 포함되지 않는다.
    """
    id_list = [aid for aid in announcement_ids if aid is not None]
    if not id_list:
        return {}

    rows = session.execute(
        select(
            FavoriteEntry.announcement_id,
            func.min(FavoriteEntry.id).label("entry_id"),
        )
        .join(FavoriteFolder, FavoriteEntry.folder_id == FavoriteFolder.id)
        .where(
            FavoriteFolder.user_id == user_id,
            FavoriteEntry.announcement_id.in_(id_list),
        )
        .group_by(FavoriteEntry.announcement_id)
    ).all()
    return {announcement_id: entry_id for announcement_id, entry_id in rows}


def get_current_sibling_announcement_ids(
    session: Session,
    *,
    announcement_id: int,
) -> list[int]:
    """동일 canonical 그룹의 is_current=True 공고 id 전체 목록을 반환한다.

    \"동일 과제 공고 모두 저장\" 라디오 처리 시 POST /favorites/entries 에서 사용된다.
    사용자 원문 #4 \"별표를 누른 그 공고가 반드시 등록\" 취지에 따라, 반환 리스트에는
    요청에 사용된 ``announcement_id`` 자체도 반드시 포함된다(호출자가 이를 검증한다).

    canonical_group_id 가 NULL 이거나 is_current 공고가 하나뿐이면 ``[announcement_id]``
    한 건만 반환한다(즉, \"모두 저장\" 을 눌렀어도 자기 자신 1건과 동일).

    Args:
        session:         호출자 세션.
        announcement_id: 기준이 되는 공고 PK (별표를 누른 그 공고).

    Returns:
        is_current=True 인 동일 canonical 그룹 공고 id 목록. 오름차순 정렬.
        대응 announcement 가 없거나 canonical_group_id 가 NULL 이면
        ``[announcement_id]`` 단일 원소 리스트.
    """
    ann = session.get(Announcement, announcement_id)
    if ann is None:
        # 호출자는 이미 announcement 존재를 검증했을 가능성이 높으나, 방어적으로 처리.
        return [announcement_id]

    if ann.canonical_group_id is None:
        # canonical 매칭이 아직 안 된 공고 — \"동일 과제\" 의 대상이 없다.
        return [announcement_id]

    rows = session.execute(
        select(Announcement.id)
        .where(
            Announcement.canonical_group_id == ann.canonical_group_id,
            Announcement.is_current.is_(True),
        )
        .order_by(Announcement.id)
    ).all()

    ids = [row[0] for row in rows]
    # 방어적 보증 — is_current 가 False 로 바뀐 경계 상황이어도 요청 공고는 반드시 포함.
    if announcement_id not in ids:
        ids.append(announcement_id)
        ids.sort()
    return ids


def count_folder_delete_cascade(
    session: Session,
    *,
    folder_id: int,
) -> dict[str, int]:
    """폴더 삭제 시 cascade 로 함께 지워질 서브폴더·공고 수를 미리 집계한다.

    /favorites UI 의 \"삭제 확인 모달\" 에 \"하위 서브그룹 N개, 공고 M건이 함께
    삭제됩니다\" 경고를 표시하기 위한 헬퍼. 폴더 depth 2 제약 덕에 루트 아래
    서브폴더 id 를 한 번에 IN 으로 가져온 뒤, 두 번째 쿼리로 본인 + 서브폴더
    전체에 속한 FavoriteEntry 수를 센다.

    Args:
        session:   호출자 세션.
        folder_id: 삭제 대상 루트 폴더 PK.

    Returns:
        ``{\"subfolder_count\": int, \"entry_count\": int}``

        - subfolder_count: 직접 자식 서브폴더 수 (depth 2 제약으로 손자는 존재하지 않음)
        - entry_count:     folder_id 본인 + 자식 서브폴더에 담긴 FavoriteEntry 총합
    """
    subfolder_ids = [
        row[0]
        for row in session.execute(
            select(FavoriteFolder.id).where(
                FavoriteFolder.parent_id == folder_id,
            )
        ).all()
    ]

    # 본인 폴더 + 자식 서브폴더를 하나의 IN 절로 — entry 수 단일 쿼리 집계.
    target_folder_ids = [folder_id, *subfolder_ids]
    entry_count: int = session.execute(
        select(func.count()).select_from(FavoriteEntry).where(
            FavoriteEntry.folder_id.in_(target_folder_ids)
        )
    ).scalar_one()

    return {
        "subfolder_count": len(subfolder_ids),
        "entry_count": entry_count,
    }


def get_siblings_by_canonical_id_map(
    session: Session,
    canonical_ids: Iterable[int],
) -> dict[int, list[dict]]:
    """canonical_project_id 목록에 대한 is_current 공고 목록을 batch 조회한다.

    동일과제 expand UI(00036-5) 와 detail 페이지 "동일 과제" 섹션을 위한
    N+1 방지 헬퍼다. IN 절 단일 쿼리로 여러 canonical 의 공고를 한 번에 가져온다.

    대표 공고 제외는 호출자 책임 — 이 함수는 모든 is_current 공고를 반환한다.

    Args:
        session:       호출자 세션.
        canonical_ids: 조회할 canonical_project_id 목록.

    Returns:
        ``{canonical_project_id: [{"id": ..., "title": ..., "source_type": ...,
        "deadline_at": ..., "status": ...}, ...]}``
        공고가 없는 canonical_id 는 키가 존재하지 않는다.
    """
    ids = [cid for cid in canonical_ids if cid is not None]
    if not ids:
        return {}

    rows = session.execute(
        select(
            Announcement.id,
            Announcement.canonical_group_id,
            Announcement.title,
            Announcement.source_type,
            Announcement.deadline_at,
            Announcement.status,
            Announcement.canonical_key_scheme,
        ).where(
            Announcement.canonical_group_id.in_(ids),
            Announcement.is_current.is_(True),
        )
    ).all()

    result: dict[int, list[dict]] = {}
    for ann_id, cid, title, source_type, deadline_at, status, key_scheme in rows:
        result.setdefault(cid, []).append(
            {
                "id": ann_id,
                "title": title,
                "source_type": source_type,
                "deadline_at": deadline_at,
                "status": status,
                "canonical_key_scheme": key_scheme,
            }
        )
    return result


__all__ = [
    "UpsertResult",
    "CanonicalGroupRow",
    "AttachmentSignature",
    "AttachmentChange",
    "HistoryWithUser",
    "upsert_announcement",
    "upsert_announcement_detail",
    "recompute_canonical_with_ancm_no",
    "compute_attachment_signature",
    "snapshot_announcement_attachments",
    "detect_attachment_changes",
    "reapply_version_with_reset",
    "get_announcement_by_id",
    "list_announcements",
    "count_announcements",
    "get_group_size_map",
    "list_canonical_groups",
    "count_canonical_groups",
    "get_available_source_ids",
    "upsert_attachment",
    "get_attachment_by_id",
    "get_attachment_by_announcement_and_filename",
    "get_attachments_by_announcement",
    "get_read_announcement_id_set",
    "mark_announcement_read",
    "MAX_BULK_MARK",
    "resolve_announcement_ids_by_filter",
    "bulk_mark_announcements_read",
    "bulk_mark_announcements_unread",
    "RELEVANCE_VERDICT_RELATED",
    "RELEVANCE_VERDICT_UNRELATED",
    "RELEVANCE_ALLOWED_VERDICTS",
    "get_canonical_project_by_id",
    "get_relevance_judgment",
    "get_relevance_by_canonical_id_map",
    "get_relevance_history_by_canonical_id_map",
    "get_relevance_history",
    "set_relevance_judgment",
    "delete_relevance_judgment",
    "get_running_scrape_run",
    "list_recent_scrape_runs",
    "create_scrape_run",
    "set_scrape_run_pid",
    "finalize_scrape_run",
    "fail_stale_running_runs",
    "get_favorite_announcement_id_set",
    "get_favorite_entry_map",
    "get_current_sibling_announcement_ids",
    "count_folder_delete_cascade",
    "get_folder_tree_for_user",
    "list_favorites_with_announcements",
    "get_siblings_by_canonical_id_map",
]


def get_folder_tree_for_user(
    session: Session,
    *,
    user_id: int,
) -> list[dict]:
    """사용자의 즐겨찾기 폴더 전체를 트리 구조로 반환한다.

    루트(depth=0) → 자식(depth=1) 순으로 정렬된다.
    favorites.html 좌 사이드바 SSR 에 사용한다.

    Args:
        session: 호출자 세션.
        user_id: 폴더 소유자 사용자 PK.

    Returns:
        [{"id": int, "name": str, "depth": int, "children": [...]}, ...]
    """
    rows = session.execute(
        select(FavoriteFolder)
        .where(FavoriteFolder.user_id == user_id)
        .order_by(FavoriteFolder.depth, FavoriteFolder.created_at)
    ).scalars().all()

    nodes: dict[int, dict] = {
        f.id: {"id": f.id, "name": f.name, "depth": f.depth, "children": []}
        for f in rows
    }
    roots: list[dict] = []
    for f in rows:
        node = nodes[f.id]
        if f.parent_id is None:
            roots.append(node)
        elif f.parent_id in nodes:
            nodes[f.parent_id]["children"].append(node)
    return roots


def list_favorites_with_announcements(
    session: Session,
    *,
    folder_id: int,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """폴더 내 즐겨찾기 항목 목록 (announcement 상세 + canonical 그룹 제목 포함).

    task 00037 부터 FavoriteEntry 는 announcement 단위로 저장된다. 따라서 각 item
    의 공고 메타는 해당 announcement 에서 직접 가져오고, 동일 과제 canonical
    그룹 제목은 대응 CanonicalProject(있을 경우) 에서 함께 채운다.

    쿼리 구조 (N+1 방지):
        1. FavoriteEntry COUNT
        2. FavoriteEntry 페이지네이션 SELECT (added_at DESC)
        3. Announcement IN(announcement_ids) 배치 SELECT
        4. CanonicalProject IN(canonical_group_ids) 배치 SELECT (canonical 이
           매칭된 announcement 만 대상)

    Args:
        session:   호출자 세션.
        folder_id: 조회할 폴더 PK.
        page:      1-based 페이지 번호.
        page_size: 페이지당 항목 수.

    Returns:
        (items, total_count)

        items 각 원소 키:
            - entry_id:             FavoriteEntry PK
            - announcement_id:      공고 PK (이 엔트리가 가리키는 실제 공고)
            - ann_id:               announcement_id 의 별칭 (템플릿 호환용)
            - ann_title:            공고 제목 (Announcement.title)
            - canonical_title:      canonical 그룹 대표 제목 (없으면 ann_title 대체)
            - ann_agency:           주관 기관명
            - ann_source_type:      'IRIS' / 'NTIS' 등
            - ann_status:           AnnouncementStatus enum
            - ann_deadline_at:      마감 시각
            - canonical_project_id: 대응 CanonicalProject PK (announcement 의
                                    canonical_group_id; None 일 수 있음)
            - added_at:             즐겨찾기 추가 시각
    """
    total: int = session.execute(
        select(func.count()).select_from(FavoriteEntry).where(
            FavoriteEntry.folder_id == folder_id
        )
    ).scalar_one()

    if total == 0:
        return [], 0

    entries = session.execute(
        select(FavoriteEntry)
        .where(FavoriteEntry.folder_id == folder_id)
        .order_by(FavoriteEntry.added_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    announcement_ids = [e.announcement_id for e in entries]

    # announcement 배치 조회 — 엔트리가 가리키는 공고 메타.
    ann_map: dict[int, Announcement] = {}
    if announcement_ids:
        ann_rows = session.execute(
            select(Announcement).where(Announcement.id.in_(announcement_ids))
        ).scalars().all()
        ann_map = {a.id: a for a in ann_rows}

    # canonical_project 배치 조회 — announcement.canonical_group_id 가 있는 것만.
    canonical_ids = [
        a.canonical_group_id
        for a in ann_map.values()
        if a.canonical_group_id is not None
    ]
    cp_map: dict[int, CanonicalProject] = {}
    if canonical_ids:
        cp_rows = session.execute(
            select(CanonicalProject).where(CanonicalProject.id.in_(canonical_ids))
        ).scalars().all()
        cp_map = {cp.id: cp for cp in cp_rows}

    items: list[dict] = []
    for e in entries:
        ann = ann_map.get(e.announcement_id)
        if ann is None:
            # ondelete=CASCADE 로 보통 발생하지 않는 경계 — 방어적으로 빈 row 내보낸다.
            items.append(
                {
                    "entry_id": e.id,
                    "announcement_id": e.announcement_id,
                    "ann_id": e.announcement_id,
                    "ann_title": None,
                    "canonical_title": None,
                    "ann_agency": None,
                    "ann_source_type": None,
                    "ann_status": None,
                    "ann_deadline_at": None,
                    "canonical_project_id": None,
                    "added_at": e.added_at,
                }
            )
            continue

        cp = cp_map.get(ann.canonical_group_id) if ann.canonical_group_id else None
        # canonical 대표 제목이 있으면 그걸, 없으면 공고 제목으로 보완.
        # favorites 테이블 제목 컬럼은 canonical_title 로 계속 표시한다(템플릿 호환).
        canonical_title = (
            cp.representative_title if cp and cp.representative_title else ann.title
        )

        items.append(
            {
                "entry_id": e.id,
                "announcement_id": ann.id,
                # ann_id: 기존 템플릿이 참조하는 별칭(announcement_id 와 동일 값).
                "ann_id": ann.id,
                "ann_title": ann.title,
                "canonical_title": canonical_title,
                "ann_agency": ann.agency,
                "ann_source_type": ann.source_type,
                "ann_status": ann.status,
                "ann_deadline_at": ann.deadline_at,
                "canonical_project_id": ann.canonical_group_id,
                "added_at": e.added_at,
            }
        )

    return items, total


# ──────────────────────────────────────────────────────────────
# Phase 5a (task 00041) — delta 적재 + apply_delta_to_main
# ──────────────────────────────────────────────────────────────
#
# 설계 근거: docs/snapshot_pipeline_design.md §7·§8.
#
# 흐름:
#   1. 수집 단계 (cli._run_source_announcements):
#      - 공고 1건마다 insert_delta_announcement → update_delta_announcement_detail
#        (상세 수집 후) → insert_delta_attachment (첨부 1건마다) 호출.
#      - 본 테이블 announcements / attachments 는 절대 건드리지 않는다.
#
#   2. 수집 종료 단계 (cli._async_main finalize 직전):
#      - status == 'cancelled' (SIGTERM) 또는 orchestrator-level failed:
#          별도 트랜잭션에서 clear_delta_for_run(scrape_run_id) 만 호출.
#          본 테이블·snapshot 변경 없음 — 검증 2 만족.
#      - status == 'completed' / 'partial':
#          단일 session_scope 안에서 apply_delta_to_main(scrape_run_id) 호출.
#          이 함수가 (a) 4-branch UPSERT, (b) 2차 감지, (c) 사용자 라벨링 reset
#          (Phase 1a 호환 — upsert_announcement / reapply_version_with_reset 가
#          그대로 호출됨), (d) delta DELETE 를 같은 트랜잭션에서 수행한다.
#          (snapshot UPSERT 는 00041-4 에서 같은 트랜잭션에 통합 추가될 예정.)
#      - apply 트랜잭션 자체가 raise 하면 SQLAlchemy auto-rollback 으로 본
#        테이블 / snapshot / delta 모두 원상복구 — 검증 11 만족.


# delta_announcements 에 INSERT 가능한 컬럼 화이트리스트 (id / created_at 제외).
_DELTA_ANNOUNCEMENT_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "scrape_run_id",
        "source_type",
        "source_announcement_id",
        "title",
        "status",
        "agency",
        "received_at",
        "deadline_at",
        "detail_url",
        "detail_html",
        "detail_text",
        "detail_fetched_at",
        "detail_fetch_status",
        "ancm_no",
        "raw_metadata",
    }
)

# delta_announcements 의 detail-stage 필드 — update_delta_announcement_detail 화이트리스트.
_DELTA_DETAIL_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "detail_html",
        "detail_text",
        "detail_fetched_at",
        "detail_fetch_status",
        "ancm_no",
    }
)

# delta_attachments INSERT 화이트리스트 (id / created_at 제외).
_DELTA_ATTACHMENT_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "delta_announcement_id",
        "original_filename",
        "stored_path",
        "file_ext",
        "file_size",
        "download_url",
        "sha256",
        "downloaded_at",
    }
)


@dataclass(frozen=True)
class TransitionRecord:
    """status 단독 전이 1건의 기록.

    apply 단계의 (c) status_transitioned 분기에서 만들어진다. snapshot.payload 의
    transitioned_to_X 카테고리 항목 ``{id, from}`` 으로 직렬화될 raw 값이다.

    Attributes:
        announcement_id: 본 테이블 적용 후 시점의 announcements.id (in-place
                         UPDATE 라 적용 전후 동일).
        status_from: 적용 직전 본 테이블 status 값 (한글: 접수예정/접수중/마감).
        status_to:   적용 후 status 값 (delta 가 정규화되기 전 값을 본 테이블의
                     enum 으로 강제한 결과 — 결국 한글 3종 중 하나).
    """

    announcement_id: int
    status_from: str
    status_to: str


@dataclass
class DeltaApplyResult:
    """``apply_delta_to_main`` 의 반환값.

    snapshot 머지 단계(00041-4) 가 본 결과의 5종 카테고리 ID 를 그대로 가져다
    payload 를 구성하므로, 카테고리 정의는 docs/snapshot_pipeline_design.md
    §9.1 과 1:1 일치한다.

    Attributes:
        new_announcement_ids: (a) created — apply 시점에 본 테이블에 처음
                               INSERT 된 announcements.id 목록. asc 정렬.
        content_changed_announcement_ids: (d) new_version (1차) 또는 2차 감지로
                               reapply 된 announcements.id 목록. asc 정렬.
                               같은 announcement 가 2차 감지로 두 번 들어가지
                               않도록 set 으로 dedup 한 뒤 정렬한다.
        transitions: (c) status_transitioned 분기에서 발생한 status 단독 전이.
                     announcement_id 별로 1 개씩.
        upsert_action_counts: 1차 4-branch action 분포.
                              keys: created / unchanged / new_version /
                              status_transitioned. 통계 / 로그용.
        attachment_success_count: delta_attachments → 본 테이블 attachments 로
                                  새로 INSERT 되었거나 변경 UPDATE 된 첨부 수.
        attachment_skipped_count: sha256 동일로 본 테이블 attachments 가
                                   변경되지 않고 스킵된 첨부 수.
        attachment_content_change_count: 2차 감지로 reapply_version_with_reset
                                          이 발동한 announcement 수.
        delta_announcement_count: 이번 apply 가 처리한 delta_announcements row 수.
    """

    new_announcement_ids: list[int] = field(default_factory=list)
    content_changed_announcement_ids: list[int] = field(default_factory=list)
    transitions: list[TransitionRecord] = field(default_factory=list)
    upsert_action_counts: dict[str, int] = field(default_factory=dict)
    attachment_success_count: int = 0
    attachment_skipped_count: int = 0
    attachment_content_change_count: int = 0
    delta_announcement_count: int = 0


# ── delta INSERT / UPDATE 헬퍼 ─────────────────────────────────────────────


def insert_delta_announcement(
    session: Session,
    *,
    scrape_run_id: int,
    payload: Mapping[str, Any],
) -> DeltaAnnouncement:
    """공고 1건을 delta_announcements 에 INSERT 한다.

    수집 단계가 본 테이블 ``announcements`` 대신 호출한다 — 4-branch 판정은
    apply 단계로 미뤄지고, delta 는 raw 값을 그대로 보존한다.

    호출 규약:
        - ``scrape_run_id`` 는 caller 가 별도 인자로 명시 (payload 안에 같이
          넣어도 동작하지만, 의도를 분명히 하기 위해 분리).
        - status 정규화는 하지 않는다 — apply 단계의 ``_coerce_status`` 가
          본 테이블에 INSERT/UPDATE 할 때 정규화한다.
        - commit 은 호출자 책임. 본 함수는 flush 까지만 수행한다.

    Args:
        session:       호출자 세션.
        scrape_run_id: 적재가 속한 ScrapeRun 의 PK.
        payload:       어댑터가 만든 delta_announcement 컬럼 매핑.
                       최소 키: source_type, source_announcement_id, title,
                       status. 그 외 컬럼은 _DELTA_ANNOUNCEMENT_ALLOWED_FIELDS
                       화이트리스트로 필터된다.

    Returns:
        flush 후 id 가 부여된 ``DeltaAnnouncement`` 인스턴스.

    Raises:
        KeyError: 필수 키(source_type/source_announcement_id/title/status)가 없을 때.
    """
    for required_key in ("source_type", "source_announcement_id", "title", "status"):
        if required_key not in payload:
            raise KeyError(
                f"delta_announcement payload 에 {required_key!r} 가 반드시 포함되어야 합니다."
            )

    clean = _filter_payload(payload, _DELTA_ANNOUNCEMENT_ALLOWED_FIELDS)
    # raw_metadata 가 빠져 있으면 빈 dict 로 정규화 (모델 컬럼이 NOT NULL).
    clean.setdefault("raw_metadata", {})

    delta_row = DeltaAnnouncement(scrape_run_id=scrape_run_id, **clean)
    session.add(delta_row)
    session.flush()
    return delta_row


def update_delta_announcement_detail(
    session: Session,
    delta_announcement_id: int,
    detail_fields: Mapping[str, Any],
) -> DeltaAnnouncement | None:
    """delta_announcements 의 상세 수집 결과 필드를 갱신한다.

    detail_html / detail_text / detail_fetched_at / detail_fetch_status / ancm_no
    만 갱신 가능하다 (그 외 키는 화이트리스트 _DELTA_DETAIL_ALLOWED_FIELDS
    로 무시된다 — payload 오염 방지).

    Args:
        session:               호출자 세션.
        delta_announcement_id: 갱신 대상 delta row 의 PK.
        detail_fields:         상세 수집 결과 매핑.

    Returns:
        갱신된 ``DeltaAnnouncement`` 인스턴스. 대상이 없으면 ``None``.
    """
    delta_row = session.get(DeltaAnnouncement, delta_announcement_id)
    if delta_row is None:
        return None

    clean = _filter_payload(detail_fields, _DELTA_DETAIL_ALLOWED_FIELDS)
    for field_name, field_value in clean.items():
        setattr(delta_row, field_name, field_value)
    session.flush()
    return delta_row


def append_delta_announcement_errors(
    session: Session,
    delta_announcement_id: int,
    error_entries: list[dict[str, Any]],
) -> DeltaAnnouncement | None:
    """delta_announcements.raw_metadata.attachment_errors 에 오류 항목을 누적한다.

    수집 단계의 첨부 다운로드가 실패한 경우 호출자가 본 함수로 원본 메타에
    오류를 기록한다 (Phase 1a 의 announcements.raw_metadata.attachment_errors
    와 동일 의도, 다만 적재 위치가 delta 일 뿐).

    apply 단계에서 raw_metadata 그대로 본 테이블 announcements.raw_metadata
    로 흘려 넣어진다.

    Args:
        session:               호출자 세션.
        delta_announcement_id: 대상 delta row PK.
        error_entries:         오류 dict 리스트 (key: original_filename / atc_file_id /
                               error / attempted_at 등 — 자유 스키마).

    Returns:
        갱신된 ``DeltaAnnouncement`` 또는 None.
    """
    if not error_entries:
        # no-op — 호출자 편의를 위해 빈 리스트도 허용.
        return session.get(DeltaAnnouncement, delta_announcement_id)

    delta_row = session.get(DeltaAnnouncement, delta_announcement_id)
    if delta_row is None:
        return None

    merged = dict(delta_row.raw_metadata or {})
    existing_errors = list(merged.get("attachment_errors", []))
    existing_errors.extend(error_entries)
    merged["attachment_errors"] = existing_errors
    delta_row.raw_metadata = merged
    session.flush()
    return delta_row


def insert_delta_attachment(
    session: Session,
    *,
    delta_announcement_id: int,
    payload: Mapping[str, Any],
) -> DeltaAttachment:
    """첨부 1건을 delta_attachments 에 INSERT 한다.

    delta 는 매 ScrapeRun 종료 시 비워지는 staging 이라 sha256 기반 중복 판정을
    하지 않는다 — apply 단계에서 본 테이블 attachments 의 sha256 비교가 그
    역할을 담당한다.

    Args:
        session:               호출자 세션.
        delta_announcement_id: 부모 DeltaAnnouncement 의 PK.
        payload:               첨부 메타 매핑. 필수 키: original_filename /
                                stored_path / file_ext / downloaded_at.
                                다운로드 실패 시 호출 자체를 생략한다 (호출자
                                책임) — 본 함수는 항상 실제 INSERT 를 시도한다.

    Returns:
        flush 후 id 가 부여된 ``DeltaAttachment`` 인스턴스.

    Raises:
        KeyError: 필수 키가 누락되었을 때.
    """
    for required_key in ("original_filename", "stored_path", "file_ext", "downloaded_at"):
        if required_key not in payload:
            raise KeyError(
                f"delta_attachment payload 에 {required_key!r} 가 반드시 포함되어야 합니다."
            )

    clean = _filter_payload(payload, _DELTA_ATTACHMENT_ALLOWED_FIELDS)
    delta_attachment = DeltaAttachment(
        delta_announcement_id=delta_announcement_id, **clean
    )
    session.add(delta_attachment)
    session.flush()
    return delta_attachment


def clear_delta_for_run(session: Session, scrape_run_id: int) -> int:
    """해당 ScrapeRun 의 delta_announcements + delta_attachments 를 전수 DELETE.

    cancelled / orchestrator-failed 분기에서 별도 트랜잭션으로 호출되어 검증 2
    의 "delta 비워짐" 을 보장한다. apply_delta_to_main 도 마지막 단계에서
    동일 의도로 호출하지만, 거기서는 같은 트랜잭션 내에서 동작한다.

    동작:
        - delta_attachments 는 delta_announcements 에 ON DELETE CASCADE 가
          걸려 있어 자동 cascade 되지만, SQLite 의 FK enforcement 가 ON
          이어야 한다. 안전성을 위해 명시적으로 attachments 를 먼저 DELETE
          한 뒤 announcements 를 DELETE 한다.

    Args:
        session:       호출자 세션.
        scrape_run_id: 비울 ScrapeRun 의 PK.

    Returns:
        삭제된 delta_announcements row 수 (delta_attachments 수는 별도 집계 X).
    """
    # 자식부터 (delta_attachments WHERE delta_announcement_id IN (... 이번 run ...))
    session.execute(
        delete(DeltaAttachment).where(
            DeltaAttachment.delta_announcement_id.in_(
                select(DeltaAnnouncement.id).where(
                    DeltaAnnouncement.scrape_run_id == scrape_run_id
                )
            )
        )
    )

    # 부모.
    delete_result = session.execute(
        delete(DeltaAnnouncement).where(
            DeltaAnnouncement.scrape_run_id == scrape_run_id
        )
    )
    session.flush()
    deleted = int(delete_result.rowcount or 0)
    if deleted:
        logger.info(
            "delta 비움: scrape_run_id={} 삭제된 delta_announcements={}",
            scrape_run_id,
            deleted,
        )
    return deleted


# ── apply_delta_to_main — 4-branch + 2차 감지 + reset + delta DELETE ───────


def _delta_payload_to_announcement_payload(
    delta: DeltaAnnouncement,
) -> dict[str, Any]:
    """DeltaAnnouncement 인스턴스를 upsert_announcement payload dict 로 변환한다.

    upsert_announcement 는 ``ancm_no`` 를 별도 키로 받기 때문에 DeltaAnnouncement
    의 컬럼을 그대로 펼쳐 넘기되 detail 관련 필드(detail_html / detail_text /
    detail_fetched_at / detail_fetch_status) 는 ``upsert_announcement`` 의
    화이트리스트(_ANNOUNCEMENT_ALLOWED_FIELDS) 에 포함되지 않으므로 제외한다.

    detail 필드 적용은 apply 단계에서 별도 ``upsert_announcement_detail`` 로
    수행한다.

    Args:
        delta: delta_announcements 의 한 row.

    Returns:
        upsert_announcement 가 받을 수 있는 dict.
    """
    return {
        "source_announcement_id": delta.source_announcement_id,
        "source_type": delta.source_type,
        "title": delta.title,
        "agency": delta.agency,
        # status 는 raw 값 그대로 — _coerce_status 가 정규화한다.
        "status": delta.status,
        "received_at": delta.received_at,
        "deadline_at": delta.deadline_at,
        "detail_url": delta.detail_url,
        "ancm_no": delta.ancm_no,
        "raw_metadata": dict(delta.raw_metadata or {}),
    }


def _has_attachment_errors(delta: DeltaAnnouncement) -> bool:
    """delta.raw_metadata.attachment_errors 에 오류 항목이 있는지 확인한다.

    2차 감지 false-positive 방지 가드 — Phase 1a 의 ``failure_count == 0``
    조건과 동일 의도. 다운로드 실패가 있으면 본 테이블 attachments 가
    실제보다 줄어든 것처럼 보여 reapply 가 잘못 발동될 수 있다.
    """
    raw_metadata = delta.raw_metadata or {}
    errors = raw_metadata.get("attachment_errors") or []
    return bool(errors)


def _peek_main_status(
    session: Session,
    *,
    source_type: str,
    source_announcement_id: str,
) -> str | None:
    """본 테이블의 is_current=True row 의 status 를 한 번에 조회한다.

    apply 단계에서 (c) status_transitioned 분기의 ``status_from`` 을 캡처할 때
    사용된다. ``upsert_announcement`` 가 호출되기 직전 시점의 status 를 읽어
    이후 in-place UPDATE 가 status 를 바꾸기 전에 from 을 확보한다.

    Args:
        session:                호출자 세션.
        source_type:            예: ``IRIS``.
        source_announcement_id: 소스 측 공고 ID.

    Returns:
        is_current=True row 가 있으면 한글 status 문자열, 없으면 None.
    """
    row_status = session.execute(
        select(Announcement.status).where(
            Announcement.source_type == source_type,
            Announcement.source_announcement_id == source_announcement_id,
            Announcement.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if row_status is None:
        return None
    # AnnouncementStatus enum 인 경우 .value, 문자열인 경우 그대로.
    if isinstance(row_status, AnnouncementStatus):
        return row_status.value
    return str(row_status)


def peek_main_can_skip_detail(
    session: Session,
    *,
    source_type: str,
    source_announcement_id: str,
    payload: Mapping[str, Any],
) -> bool:
    """본 테이블 안내 row 가 이번 row 와 동일하고 detail 이 이미 있는지 확인한다.

    수집 단계의 detail 스크랩 생략 최적화를 위한 read-only peek. apply 단계의
    4-branch 판정과 동일 비교 로직(_detect_changes / _normalize_for_comparison)
    을 사용한다 — 이로써 "스킵 가능 여부" 의 시맨틱이 apply 의 (b) unchanged
    분기와 1:1 일치한다.

    Phase 1a 의 ``upsert_result.needs_detail_scraping`` 과 동일 시맨틱이지만,
    본 테이블에 INSERT/UPDATE 를 일으키지 않는 read-only 변형이다 — apply 가
    아직 도착하지 않은 수집 단계에서 안전하게 호출할 수 있다.

    Args:
        session:                호출자 세션.
        source_type:            예: ``IRIS``.
        source_announcement_id: 소스 측 공고 ID.
        payload:                현재 수집 row 의 4-branch 비교 필드를 담은 매핑
                                (title / status / agency / deadline_at).

    Returns:
        True 이면 detail 수집을 생략해도 안전하다 (본 테이블에 unchanged 인
        is_current row 가 있고 detail_html 도 채워져 있음).
        False 이면 detail 을 다시 받아야 한다 (신규 / 변경 / detail 미보유).
    """
    existing = session.execute(
        select(Announcement).where(
            Announcement.source_type == source_type,
            Announcement.source_announcement_id == source_announcement_id,
            Announcement.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    if existing.detail_fetched_at is None or not existing.detail_html:
        return False

    # status 는 비교 전 정규화 (payload 가 raw 문자열이면 enum 으로).
    clean = dict(payload)
    if "status" in clean:
        try:
            clean["status"] = _coerce_status(clean["status"])
        except (TypeError, ValueError):
            # raw 가 비정상이면 변경으로 간주(보수적으로 detail 재수집).
            return False
    changed = _detect_changes(existing, clean)
    return not changed


def apply_delta_to_main(
    session: Session,
    *,
    scrape_run_id: int,
) -> DeltaApplyResult:
    """delta → 본 테이블 4-branch UPSERT + 2차 감지 + reset + delta DELETE.

    docs/snapshot_pipeline_design.md §7.4·§8 의 단일 트랜잭션 본문이다.
    호출자(``cli._async_main``) 가 ``session_scope()`` 안에서 부르고, 본 함수가
    return 한 뒤 같은 session 으로 ``upsert_scrape_snapshot`` (00041-4) 을 추가
    호출하면 모든 작업이 같은 트랜잭션에 묶인다.

    동작 (호출자 session 에서):
        1. ``DeltaAnnouncement`` (+ selectinload attachments) 전수 조회 —
           이번 ``scrape_run_id`` 한정.
        2. 각 delta 마다:
           a. 본 테이블의 기존 status 캡처 (``_peek_main_status``) — (c) 분기의
              ``status_from`` 용.
           b. ``upsert_announcement(session, payload)`` — Phase 1a 의 4-branch
              판정 + (d) 분기의 사용자 라벨링 reset 이 그대로 발동된다.
           c. ``announcement_id`` 확정 (=upsert_result.announcement.id).
           d. delta_attachments 를 본 테이블 attachments 에 sha256 기반 upsert.
           e. 다운로드 실패가 없는 경우만 2차 감지 (signature_before vs
              signature_after) → 변경 시 ``reapply_version_with_reset`` 로
              is_current 순환 + 사용자 라벨링 reset.
        3. 카테고리 누적: created → new, new_version/2차 감지 → content_changed,
           status_transitioned → transitions(from/to).
        4. ``clear_delta_for_run(scrape_run_id)`` — 같은 트랜잭션 안에서
           delta 비우기. 트랜잭션 실패 시 SQLAlchemy auto-rollback 으로 delta 도
           원상복구된다 (검증 11).

    트랜잭션 경계:
        - 본 함수는 flush 까지만 한다. commit 은 호출자 책임.
        - 한 delta row 의 처리 중 예외가 발생하면 raise 가 호출자까지 전파
          되어 트랜잭션 전체가 rollback 된다 — 검증 11 의 "트랜잭션 실패 시
          delta 그대로, 본 테이블 변화 없음" 을 보장한다.

    공고 1건당 비용:
        본 함수는 1차 / 2차 감지를 분리 호출(설계 §8.4 권장안) 하지만, delta
        에 이미 첨부 메타까지 다 들어 있으므로 N+1 query 는 발생하지 않는다.
        DeltaAnnouncement.attachments relationship 이 selectinload 로 묶여
        있어 첫 SELECT 한 번에 모든 첨부가 로드된다.

    Args:
        session:        호출자 세션.
        scrape_run_id:  apply 대상 ScrapeRun 의 PK.

    Returns:
        ``DeltaApplyResult`` — 5종 카테고리 ID 리스트 + transition 기록 +
        action 분포 + 첨부 카운터 + 처리한 delta 수.
    """
    # 1. delta 전수 조회 — selectinload 로 attachments 까지 한 번에 로드.
    deltas: list[DeltaAnnouncement] = list(
        session.execute(
            select(DeltaAnnouncement)
            .options(selectinload(DeltaAnnouncement.attachments))
            .where(DeltaAnnouncement.scrape_run_id == scrape_run_id)
            .order_by(DeltaAnnouncement.id.asc())
        )
        .scalars()
        .all()
    )

    result = DeltaApplyResult(delta_announcement_count=len(deltas))

    # dedup 용 set — content_changed 에 1차 (d) 와 2차 감지가 동시에 들어가도
    # 같은 announcement_id 가 두 번 박히지 않게 한다.
    new_id_set: set[int] = set()
    content_changed_id_set: set[int] = set()
    transitions_buffer: list[TransitionRecord] = []
    action_counts: dict[str, int] = {}

    for delta in deltas:
        # 2-a. (c) 분기에서 쓸 status_from 을 미리 캡처.
        status_from = _peek_main_status(
            session,
            source_type=delta.source_type,
            source_announcement_id=delta.source_announcement_id,
        )

        # 2-b. 1차 4-branch UPSERT — Phase 1a 의 시맨틱 그대로 (reset 포함).
        payload = _delta_payload_to_announcement_payload(delta)
        upsert_result = upsert_announcement(session, payload)
        action = upsert_result.action
        action_counts[action] = action_counts.get(action, 0) + 1

        # 2-b'. detail 필드는 별도로 본 테이블에 적용한다 — upsert_announcement
        # 의 화이트리스트(_ANNOUNCEMENT_ALLOWED_FIELDS) 에 detail_* 가 포함되지
        # 않기 때문이다. (a) created / (d) new_version 신규 row 와 (c) in-place
        # UPDATE row 모두에 detail_html 등을 채워 다음 수집의 detail 스킵
        # 최적화(_peek_main_can_skip_detail) 가 정확히 작동하도록 한다.
        # delta 에 detail 이 안 채워졌으면(예: skip_detail/실패) NULL 그대로 둔다.
        if delta.detail_fetched_at is not None or delta.detail_html is not None:
            upsert_announcement_detail(
                session,
                delta.source_announcement_id,
                {
                    "detail_html": delta.detail_html,
                    "detail_text": delta.detail_text,
                    "detail_fetched_at": delta.detail_fetched_at,
                    "detail_fetch_status": delta.detail_fetch_status,
                },
                source_type=delta.source_type,
            )

        announcement_id = upsert_result.announcement.id

        # 2-b''. NTIS 의 fuzzy → official canonical 승급은 1차 4-branch 가 본
        # 테이블의 (b) unchanged 분기에서 ancm_no 변경을 감지하지 못하므로
        # (ancm_no 가 _CHANGE_DETECTION_FIELDS 에 없음) 별도 호출로 보강한다.
        # recompute 헬퍼는 이미 official scheme 인 row 를 건드리지 않으므로
        # 매 호출이 idempotent 다.
        if delta.ancm_no:
            recompute_canonical_with_ancm_no(
                session,
                delta.source_announcement_id,
                source_type=delta.source_type,
                ancm_no=delta.ancm_no,
            )

        # 2-c. 1차 카테고리 분류.
        if action == "created":
            new_id_set.add(announcement_id)
        elif action == "new_version":
            content_changed_id_set.add(announcement_id)
        elif action == "status_transitioned":
            # status_to 는 본 테이블의 정규화된 enum 값 (한글 3종) 으로 통일.
            normalized_to = upsert_result.announcement.status
            status_to_value = (
                normalized_to.value
                if isinstance(normalized_to, AnnouncementStatus)
                else str(normalized_to)
            )
            transitions_buffer.append(
                TransitionRecord(
                    announcement_id=announcement_id,
                    # status_from 이 None 이면 created 분기여야 하지만 안전 fallback.
                    status_from=status_from or status_to_value,
                    status_to=status_to_value,
                )
            )
        # action == 'unchanged' 는 어떤 카테고리에도 들어가지 않는다.

        # 2-d. delta_attachments → 본 테이블 attachments sha256 기반 upsert.
        signature_before = snapshot_announcement_attachments(
            session, announcement_id
        )
        for delta_att in delta.attachments:
            # 다운로드 실패로 sha256 이 NULL 인 경우는 본 테이블에 그대로
            # INSERT 하지 않는다(부분 데이터로 본 테이블 신뢰성을 깨지 않기
            # 위함). 단 stored_path 가 명시되어 있고 실제로 파일이 디스크에
            # 있는 정상 케이스만 본 테이블 적재 대상이다.
            if not delta_att.sha256:
                # 다운로드 실패 첨부 — 본 테이블 미반영. raw_metadata 의
                # attachment_errors 가 이미 채워져 있으므로 다음 수집에서
                # 재시도가 자연스럽게 일어난다.
                continue
            attachment_payload = {
                "announcement_id": announcement_id,
                "original_filename": delta_att.original_filename,
                "stored_path": delta_att.stored_path,
                "file_ext": delta_att.file_ext,
                "file_size": delta_att.file_size,
                "download_url": delta_att.download_url,
                "sha256": delta_att.sha256,
                "downloaded_at": delta_att.downloaded_at,
            }
            _, was_upserted = upsert_attachment(session, attachment_payload)
            if was_upserted:
                result.attachment_success_count += 1
            else:
                result.attachment_skipped_count += 1

        # 2-e. 2차 감지 — false-positive 가드 (다운로드 실패 0 + 1차 action 적격).
        secondary_allowed = action in ("unchanged", "status_transitioned")
        if secondary_allowed and not _has_attachment_errors(delta):
            signature_after = snapshot_announcement_attachments(
                session, announcement_id
            )
            change = detect_attachment_changes(signature_before, signature_after)
            if change.changed:
                reapply_result = reapply_version_with_reset(
                    session,
                    announcement_id,
                    changed_fields=frozenset({"attachments"}),
                )
                content_changed_id_set.add(reapply_result.announcement.id)
                result.attachment_content_change_count += 1
                logger.info(
                    "apply 2차 감지 — 첨부 변경으로 버전 갱신: "
                    "delta_id={} source={}/{} new_announcement_id={} "
                    "added={} removed={} count_changed={}",
                    delta.id,
                    delta.source_type,
                    delta.source_announcement_id,
                    reapply_result.announcement.id,
                    len(change.added),
                    len(change.removed),
                    change.count_changed,
                )

    # 3. 카테고리 결과를 dataclass 로 정리 — id 기준 asc 정렬 (재현성 + 머지 안정성).
    result.new_announcement_ids = sorted(new_id_set)
    result.content_changed_announcement_ids = sorted(content_changed_id_set)
    # transitions 도 announcement_id asc 정렬.
    result.transitions = sorted(transitions_buffer, key=lambda t: t.announcement_id)
    result.upsert_action_counts = action_counts

    # 4. delta 비움 — 같은 트랜잭션이라 apply 전체가 rollback 되면 함께 되돌아간다.
    clear_delta_for_run(session, scrape_run_id)

    logger.info(
        "apply_delta_to_main 완료: scrape_run_id={} 처리={}건 "
        "actions={} new={} content_changed={} transitions={} "
        "attachment_success={} attachment_skipped={} 2차변경={}",
        scrape_run_id,
        len(deltas),
        action_counts,
        len(result.new_announcement_ids),
        len(result.content_changed_announcement_ids),
        len(result.transitions),
        result.attachment_success_count,
        result.attachment_skipped_count,
        result.attachment_content_change_count,
    )
    return result


# ──────────────────────────────────────────────────────────────
# ScrapeSnapshot UPSERT — 같은 KST 날짜 1 row + payload 머지
# ──────────────────────────────────────────────────────────────
#
# 설계 근거: docs/snapshot_pipeline_design.md §9.6·§10.
#
# 호출 위치:
#   - app/cli.py 의 _async_main 안 apply_session 트랜잭션에서 apply_delta_to_main
#     반환 직후 호출된다. 같은 session 으로 호출하므로 본 테이블·delta·snapshot
#     모두 단일 atomic 단위에 묶인다.
#
# 동시성 고려:
#   - ScrapeRun lock (Phase 2 — running row 1개) 으로 동시 ScrapeRun 이 차단되어
#     같은 snapshot_date 에 대한 race 는 사실상 발생하지 않는다.
#   - 그래도 SELECT-then-INSERT/UPDATE 패턴으로 단일 트랜잭션 안에서 원자성을
#     확보한다. UNIQUE(snapshot_date) (00041-2 migration) 가 최후 방어선.
#
# 머지는 본 모듈이 아니라 app/db/snapshot.py 의 순수 함수 merge_snapshot_payload
# 가 담당한다 — DB session 의존을 떼어내 유닛 테스트가 1줄 import 로 가능하도록.


def upsert_scrape_snapshot(
    session: Session,
    *,
    snapshot_date: date,
    new_payload: dict[str, Any],
) -> ScrapeSnapshot:
    """같은 KST 날짜의 ScrapeSnapshot 이 있으면 머지, 없으면 신규 INSERT 한다.

    동작 (호출자 session 사용 — flush 까지만 수행, commit 은 호출자 책임):
        1. SELECT scrape_snapshots WHERE snapshot_date = :snapshot_date
        2. 매칭 row 가 없으면 → INSERT (payload=normalize_payload(new_payload)).
           normalize_payload 가 5종 카테고리를 빈 배열로 채워 5b 의 view 가
           KeyError 없이 0 건도 표시할 수 있게 한다 (설계 §10.3).
        3. 매칭 row 가 있으면 → merge_snapshot_payload(existing, new) 결과로
           UPDATE. updated_at 은 모델의 onupdate 가 자동 갱신.

    트랜잭션 경계:
        본 함수는 flush 까지만 수행한다. 호출자(_async_main 의 apply_session)
        가 commit 시점을 통제하므로, apply_delta_to_main 과 같은 트랜잭션에
        묶여 일관성이 보장된다 — apply 가 raise 하면 SQLAlchemy auto-rollback
        으로 snapshot 도 함께 원상복구되어 검증 11 시나리오를 만족한다.

    빈 ScrapeRun (5종 카테고리 모두 빈 배열) 처리:
        설계 §10.3 권장안에 따라 신규 INSERT 시에도 row 를 만든다 — 같은 날의
        후속 ScrapeRun 이 머지할 대상이 되며, 5b 의 캘린더가 \"이 날 수집 시도가
        있었음\" 을 표시할 수 있다.

    Args:
        session:        호출자 세션.
        snapshot_date:  KST 기준 날짜 (date 객체). 호출자가
                        ``app.timezone.now_kst().date()`` 로 계산해 전달한다.
        new_payload:    이번 ScrapeRun 의 ``build_snapshot_payload(apply_result)``
                        결과. 카테고리가 누락되어 있으면 normalize_payload 가
                        빈 컨테이너로 채운다.

    Returns:
        flush 후 id 가 부여된 ``ScrapeSnapshot`` 인스턴스 (신규 INSERT 또는
        머지 UPDATE 결과).
    """
    existing_row = session.execute(
        select(ScrapeSnapshot).where(ScrapeSnapshot.snapshot_date == snapshot_date)
    ).scalar_one_or_none()

    if existing_row is None:
        # 신규 INSERT — 빈 카테고리도 명시적으로 정규형으로 채운다.
        normalized = normalize_payload(new_payload)
        snapshot = ScrapeSnapshot(
            snapshot_date=snapshot_date,
            payload=normalized,
        )
        session.add(snapshot)
        session.flush()
        logger.info(
            "ScrapeSnapshot 신규 INSERT: snapshot_date={} counts={}",
            snapshot_date.isoformat(),
            normalized.get("counts", {}),
        )
        return snapshot

    # 기존 row → merge_snapshot_payload 결과로 UPDATE.
    merged = merge_snapshot_payload(existing_row.payload, new_payload)
    existing_row.payload = merged
    session.flush()
    logger.info(
        "ScrapeSnapshot 머지 UPDATE: snapshot_date={} counts={}",
        snapshot_date.isoformat(),
        merged.get("counts", {}),
    )
    return existing_row


def get_scrape_snapshot_by_date(
    session: Session,
    snapshot_date: date,
) -> ScrapeSnapshot | None:
    """``snapshot_date`` 의 ScrapeSnapshot row 를 조회한다 (없으면 None).

    UNIQUE(snapshot_date) 제약상 0 또는 1 건만 존재한다. 5b 의 dashboard view
    에서 \"오늘의 변화 요약\" 을 빠르게 가져올 때 사용한다.

    Args:
        session:       호출자 세션.
        snapshot_date: 조회할 KST 날짜.

    Returns:
        ``ScrapeSnapshot`` 또는 None.
    """
    return session.execute(
        select(ScrapeSnapshot).where(ScrapeSnapshot.snapshot_date == snapshot_date)
    ).scalar_one_or_none()


def list_available_snapshot_dates(session: Session) -> list[date]:
    """``scrape_snapshots`` 의 ``snapshot_date`` 전체를 오름차순으로 반환한다.

    Phase 5b (task 00042-2) 의 캘린더 컴포넌트와 ``GET
    /dashboard/api/snapshot-dates`` JSON API 가 공유하는 단일 헬퍼다. UNIQUE
    제약상 한 KST 날짜당 0 또는 1 건이라 결과 list 는 자연스럽게 중복이
    없으며, 정렬은 SQL ``ORDER BY snapshot_date ASC`` 로 처리한다.

    캘린더 가용 날짜 판정 정책 (``docs/dashboard_design.md §4.1``):
        본 함수가 반환하는 날짜 == \"캘린더에서 활성 (클릭 가능)\" 이다.
        Phase 5a 의 ``upsert_scrape_snapshot`` 이 ScrapeRun completed/partial
        종료 시 변화 0건이어도 row 를 INSERT 하므로, \"수집은 됐지만 변화 0건\"
        인 날도 본 함수의 결과에 포함되어 캘린더에서 활성으로 보인다 (디자인
        의도). 반대로 그날 ScrapeRun 이 모두 failed/cancelled 였거나 실행 자체
        가 없었던 날만 비활성으로 표시된다.

    Args:
        session: 호출자 세션.

    Returns:
        snapshot_date(KST date) 들의 오름차순 list. 비어 있으면 빈 list.
    """
    rows = session.execute(
        select(ScrapeSnapshot.snapshot_date).order_by(ScrapeSnapshot.snapshot_date.asc())
    ).all()
    return [row[0] for row in rows]
