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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.canonical import compute_canonical_key
from app.db.models import (
    Announcement,
    AnnouncementStatus,
    AnnouncementUserState,
    Attachment,
    CanonicalProject,
    RelevanceJudgment,
    RelevanceJudgmentHistory,
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


__all__ = [
    "UpsertResult",
    "CanonicalGroupRow",
    "AttachmentSignature",
    "AttachmentChange",
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
]
