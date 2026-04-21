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

    비교 대상 필드: title, status, deadline_at, agency
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
            ※ [NTIS 등 신규 크롤러 구현 시]:
               새 소스 어댑터를 추가할 때 status 값을 AnnouncementStatus Enum 으로
               정규화하는 로직을 반드시 포함해야 한다.
        (d) 기존 row 존재, 그 외 변경 (title/deadline_at/agency 변경, status 포함 여부 무관)
            → 기존 row is_current=False 봉인 + 신규 row INSERT (is_current=True),
               action="new_version", needs_detail_scraping=True, changed_fields 원본 유지.
               이력이 row 단위로 누적된다.

payload 규약:
    - `upsert_announcement(session, payload)` 의 `payload` 는
      `Announcement` 의 컬럼명을 키로 갖는 매핑이다.
      최소 요구 키: `source_announcement_id`, `source_type`, `title`, `status`.
      `status` 는 `AnnouncementStatus` Enum 또는 그 문자열 값("접수중"/"접수예정"/"마감") 중 하나.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Announcement, AnnouncementStatus, Attachment

# 공고에서 사용자가 payload 로 덮어쓰지 말아야 하는 자동 관리 컬럼들.
_ANNOUNCEMENT_PROTECTED_FIELDS: frozenset[str] = frozenset({"id", "scraped_at", "updated_at"})

# 공고 payload 에 허용되는 컬럼 목록(화이트리스트).
_ANNOUNCEMENT_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "source_announcement_id",
    "source_type",
    "title",
    "agency",
    "status",
    "received_at",
    "deadline_at",
    "detail_url",
    "raw_metadata",
})

# 상세 수집 결과 업데이트 시 허용되는 컬럼 목록.
_DETAIL_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "detail_html",
    "detail_text",
    "detail_fetched_at",
    "detail_fetch_status",
})

# 변경 감지 비교 대상 필드.
# - title, status, deadline_at: 공고를 특정하는 핵심 필드
# - agency: 주관 기관 변경도 의미 있는 내용 변화로 판정
# received_at 은 의도적으로 제외 (접수예정 시 미기재 후 보완 패턴이 흔함)
_CHANGE_DETECTION_FIELDS: tuple[str, ...] = ("title", "status", "deadline_at", "agency")


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
                f"알 수 없는 공고 상태값: {value!r}. "
                f"허용값: {[m.value for m in AnnouncementStatus]}"
            ) from exc
    raise TypeError(f"status 는 AnnouncementStatus 또는 str 이어야 합니다. 입력 타입: {type(value).__name__}")


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
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if type(value) is str:
        return value.strip()
    return value


def _detect_changes(
    existing: Announcement,
    clean_payload: dict[str, Any],
) -> frozenset[str]:
    """기존 레코드와 신규 payload 를 비교해 변경된 필드 이름의 집합을 반환한다.

    비교 대상 필드: title, status, deadline_at, agency (_CHANGE_DETECTION_FIELDS 참조).
    payload 에 해당 키가 없으면 해당 필드는 변경 없음으로 판정한다.
    양측 값은 _normalize_for_comparison 으로 정규화한 뒤 비교하여
    datetime tz-naive/aware 불일치 및 문자열 공백 차이로 인한 false-positive 를 제거한다.

    Args:
        existing:      DB 에서 조회한 기존 Announcement 인스턴스 (is_current=True).
        clean_payload: _filter_payload + status 정규화가 완료된 payload dict.

    Returns:
        변경된 필드 이름의 frozenset. 변경이 없으면 빈 frozenset.
    """
    changed: set[str] = set()
    for field_name in _CHANGE_DETECTION_FIELDS:
        if field_name not in clean_payload:
            continue
        existing_val = _normalize_for_comparison(getattr(existing, field_name))
        new_val = _normalize_for_comparison(clean_payload[field_name])
        if existing_val != new_val:
            changed.add(field_name)
    return frozenset(changed)


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
        session.flush()
        return UpsertResult(
            announcement=existing,
            action="status_transitioned",
            needs_detail_scraping=True,
            changed_fields=changed_fields,
        )

    # ── (d) 내용 변경: 이력 보존 — 기존 row 봉인 + 신규 row INSERT ─────────────
    existing.is_current = False
    # 신규 row 에는 payload 값 + is_current=True 를 설정한다.
    # scraped_at 은 새 수집 시각으로 기록한다 (모델 default=_utcnow 가 처리).
    new_announcement = Announcement(**clean_payload, is_current=True)
    session.add(new_announcement)
    session.flush()
    return UpsertResult(
        announcement=new_announcement,
        action="new_version",
        needs_detail_scraping=True,
        changed_fields=changed_fields,
    )


def get_announcement_by_id(
    session: Session,
    announcement_id: int,
) -> Optional[Announcement]:
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
) -> Optional[Announcement]:
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


def list_announcements(
    session: Session,
    status: Optional[AnnouncementStatus | str] = None,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
) -> list[Announcement]:
    """공고 목록을 조회한다 (현재 버전 is_current=True 한정).

    정렬:
        - `deadline_at ASC NULLS LAST` 로 마감이 임박한 공고가 먼저 오게 한다.
        - 동률이면 `id DESC` 로 최근 수집된 공고를 우선한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        status: `AnnouncementStatus` 또는 그 문자열 값으로 필터. `None` 이면 전체.
        limit: 페이지 크기. 1 이상이어야 한다.
        offset: 건너뛸 레코드 수(페이지네이션).
        search: 제목/기관명에 대한 부분일치(LIKE) 검색어. 공백은 무시(strip).

    Returns:
        조건에 맞는 `Announcement` 리스트 (is_current=True 만 포함).
    """
    # is_current=True 만 표시한다 — 이력(구버전) row 는 목록에 노출하지 않는다.
    statement = select(Announcement).where(Announcement.is_current.is_(True))

    if status is not None:
        statement = statement.where(Announcement.status == _coerce_status(status))

    if search is not None:
        normalized_search = search.strip()
        if normalized_search:
            like_pattern = f"%{normalized_search}%"
            statement = statement.where(
                or_(
                    Announcement.title.ilike(like_pattern),
                    Announcement.agency.ilike(like_pattern),
                )
            )

    # deadline_at NULL 값은 뒤로 보낸다.
    statement = statement.order_by(
        (Announcement.deadline_at.is_(None)).asc(),
        Announcement.deadline_at.asc(),
        Announcement.id.desc(),
    )

    safe_limit = max(int(limit), 1)
    safe_offset = max(int(offset), 0)
    statement = statement.offset(safe_offset).limit(safe_limit)

    return list(session.execute(statement).scalars().all())


def count_announcements(
    session: Session,
    status: Optional[AnnouncementStatus | str] = None,
    search: Optional[str] = None,
) -> int:
    """`list_announcements` 와 동일 조건의 전체 개수를 반환한다 (is_current=True 한정).

    목록 페이지네이션에서 '총 N건' 표시/페이지 수 계산에 사용한다.
    """
    statement = select(func.count()).select_from(Announcement).where(
        Announcement.is_current.is_(True)
    )

    if status is not None:
        statement = statement.where(Announcement.status == _coerce_status(status))

    if search is not None:
        normalized_search = search.strip()
        if normalized_search:
            like_pattern = f"%{normalized_search}%"
            statement = statement.where(
                or_(
                    Announcement.title.ilike(like_pattern),
                    Announcement.agency.ilike(like_pattern),
                )
            )

    return int(session.execute(statement).scalar_one())



# ── 첨부파일(Attachment) UPSERT 키 기준 허용 필드 ────────────────────────────────────
# downloaded_at 은 NOT NULL 이므로 payload 에 항상 포함해야 한다.
_ATTACHMENT_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "announcement_id",
    "original_filename",
    "stored_path",
    "file_ext",
    "file_size",
    "download_url",
    "sha256",
    "downloaded_at",
})


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
) -> Optional[Attachment]:
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
) -> Optional[Attachment]:
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
    "upsert_announcement",
    "upsert_announcement_detail",
    "get_announcement_by_id",
    "list_announcements",
    "count_announcements",
    "upsert_attachment",
    "get_attachment_by_id",
    "get_attachment_by_announcement_and_filename",
    "get_attachments_by_announcement",
]
