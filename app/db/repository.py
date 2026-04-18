"""Repository 레이어: 공고/첨부파일 UPSERT 및 조회.

설계 원칙:
    - 모든 함수는 호출자가 전달한 `Session` 을 그대로 사용한다.
      트랜잭션 경계(commit/rollback)는 호출자가 `session_scope()` 등으로 제어한다.
      (리포지토리는 `flush()` 까지만 수행한다.)
    - 공고의 중복 판정 기준: `iris_announcement_id` (UNIQUE 컬럼).
    - 첨부파일의 중복 판정 기준:
      `(announcement_id, original_filename, download_url)` 복합키.
      `download_url` 이 NULL 인 경우도 동일 레코드로 취급하도록 `IS NULL` 로 매칭한다.

payload 규약:
    - `upsert_announcement(session, payload)` 의 `payload` 는
      `Announcement` 의 컬럼명을 키로 갖는 매핑이다.
      최소 요구 키: `iris_announcement_id`, `title`, `status`.
      `status` 는 `AnnouncementStatus` Enum 또는 그 문자열 값("접수중"/"접수예정"/"마감") 중 하나.
    - `upsert_attachment(session, announcement_id, payload)` 의 `payload` 는
      `Attachment` 의 컬럼명을 키로 갖는 매핑이다.
      최소 요구 키: `original_filename`, `stored_path`, `file_ext`.
      `download_url` 은 생략 가능(그 경우 NULL 로 간주).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Announcement, AnnouncementStatus, Attachment

# 공고에서 사용자가 payload 로 덮어쓰지 말아야 하는 자동 관리 컬럼들.
_ANNOUNCEMENT_PROTECTED_FIELDS: frozenset[str] = frozenset({"id", "scraped_at", "updated_at"})

# 첨부파일의 자동 관리 컬럼.
_ATTACHMENT_PROTECTED_FIELDS: frozenset[str] = frozenset({"id", "announcement_id", "downloaded_at"})

# 공고 payload 에 허용되는 컬럼 목록(화이트리스트).
_ANNOUNCEMENT_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "iris_announcement_id",
    "title",
    "agency",
    "status",
    "received_at",
    "deadline_at",
    "detail_url",
    "raw_metadata",
})

# 첨부파일 payload 에 허용되는 컬럼 목록(화이트리스트).
_ATTACHMENT_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "original_filename",
    "stored_path",
    "file_ext",
    "file_size",
    "download_url",
    "sha256",
})


def _coerce_status(value: Any) -> AnnouncementStatus:
    """payload 의 status 값을 `AnnouncementStatus` 로 변환한다.

    - 이미 Enum 인스턴스면 그대로 반환한다.
    - 문자열이면 한글 값("접수중"/"접수예정"/"마감") 으로 매칭한다.
    - 그 외는 `ValueError` 를 일으켜 잘못된 입력을 빠르게 노출시킨다.
    """
    if isinstance(value, AnnouncementStatus):
        return value
    if isinstance(value, str):
        # 한글 값 매칭 (Enum 의 value 가 한글이므로)
        for member in AnnouncementStatus:
            if member.value == value:
                return member
        # name 으로도 허용 ("RECEIVING" 등)
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

    허용 목록에 없는 키는 조용히 무시한다(호출자의 오타는
    `TypeError(Announcement() unexpected kwarg)` 대신 조용한 무시로 덮어쓴다).
    """
    return {key: value for key, value in payload.items() if key in allowed}


def upsert_announcement(session: Session, payload: Mapping[str, Any]) -> Announcement:
    """공고 한 건을 UPSERT 한다.

    동작:
        - `iris_announcement_id` 로 기존 레코드를 찾는다.
        - 없으면 INSERT, 있으면 payload 의 필드로 덮어쓴다.
        - `id`/`scraped_at`/`updated_at` 은 payload 로 덮어쓰지 않는다.
        - commit 은 호출자 책임이며, 여기서는 PK 확보를 위해 `flush()` 만 수행한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        payload: 공고 속성을 담은 매핑. 최소 `iris_announcement_id`, `title`, `status` 필요.

    Returns:
        삽입되거나 갱신된 `Announcement` 인스턴스(세션에 부착된 상태, `flush()` 완료).

    Raises:
        KeyError: 필수 키(`iris_announcement_id`)가 payload 에 없을 때.
        ValueError: `status` 값이 허용 범위를 벗어났을 때.
    """
    if "iris_announcement_id" not in payload:
        raise KeyError("payload 에 'iris_announcement_id' 가 반드시 포함되어야 합니다.")

    iris_announcement_id = payload["iris_announcement_id"]

    # 허용 컬럼만 추출 + status 정규화.
    clean_payload = _filter_payload(payload, _ANNOUNCEMENT_ALLOWED_FIELDS)
    if "status" in clean_payload:
        clean_payload["status"] = _coerce_status(clean_payload["status"])

    # 기존 레코드 조회.
    existing = session.execute(
        select(Announcement).where(Announcement.iris_announcement_id == iris_announcement_id)
    ).scalar_one_or_none()

    if existing is None:
        announcement = Announcement(**clean_payload)
        session.add(announcement)
    else:
        # UPSERT: 자동 관리 컬럼(및 키)을 제외한 모든 필드 덮어쓰기.
        for field_name, field_value in clean_payload.items():
            if field_name in _ANNOUNCEMENT_PROTECTED_FIELDS:
                continue
            if field_name == "iris_announcement_id":
                # 키 자체는 바꾸지 않는다(where 절과 동일 값이므로 의미도 없음).
                continue
            setattr(existing, field_name, field_value)
        announcement = existing

    # 새 레코드의 PK 확보와 UPDATE 타이밍을 맞추기 위해 flush.
    session.flush()
    return announcement


def upsert_attachment(
    session: Session,
    announcement_id: int,
    payload: Mapping[str, Any],
) -> Attachment:
    """첨부파일 한 건을 UPSERT 한다.

    중복 판정 기준은 `(announcement_id, original_filename, download_url)` 이다.
    `download_url` 이 None 인 payload 는 `download_url IS NULL` 인 레코드와 매칭된다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        announcement_id: 소속 공고의 내부 PK (`Announcement.id`).
        payload: 첨부파일 속성을 담은 매핑.
            최소 `original_filename`, `stored_path`, `file_ext` 가 필요하다.
            `download_url` 은 생략/None 가능.

    Returns:
        삽입되거나 갱신된 `Attachment` 인스턴스(`flush()` 완료).

    Raises:
        KeyError: 필수 키(`original_filename`)가 payload 에 없을 때.
    """
    if "original_filename" not in payload:
        raise KeyError("payload 에 'original_filename' 가 반드시 포함되어야 합니다.")

    original_filename: str = payload["original_filename"]
    download_url: Optional[str] = payload.get("download_url")

    clean_payload = _filter_payload(payload, _ATTACHMENT_ALLOWED_FIELDS)

    # 복합키로 기존 레코드 조회.
    # NULL 비교는 `= NULL` 이 아닌 `IS NULL` 을 써야 한다.
    if download_url is None:
        download_url_filter = Attachment.download_url.is_(None)
    else:
        download_url_filter = Attachment.download_url == download_url

    existing = session.execute(
        select(Attachment).where(
            Attachment.announcement_id == announcement_id,
            Attachment.original_filename == original_filename,
            download_url_filter,
        )
    ).scalar_one_or_none()

    if existing is None:
        attachment = Attachment(announcement_id=announcement_id, **clean_payload)
        session.add(attachment)
    else:
        # 자동 관리 컬럼과 복합키 구성 컬럼을 제외한 나머지를 갱신한다.
        # download_url 과 original_filename 은 키의 일부라 여기서 바꾸면 의미가 어긋난다.
        for field_name, field_value in clean_payload.items():
            if field_name in _ATTACHMENT_PROTECTED_FIELDS:
                continue
            if field_name in ("original_filename", "download_url"):
                continue
            setattr(existing, field_name, field_value)
        attachment = existing

    session.flush()
    return attachment


def list_announcements(
    session: Session,
    status: Optional[AnnouncementStatus | str] = None,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
) -> list[Announcement]:
    """공고 목록을 조회한다.

    정렬:
        - `deadline_at ASC NULLS LAST` 로 마감이 임박한 공고가 먼저 오게 한다.
        - 동률이면 `id DESC` 로 최근 수집된 공고를 우선한다.

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        status: `AnnouncementStatus` 또는 그 문자열 값으로 필터. `None` 이면 전체.
        limit: 페이지 크기. 1 이상이어야 한다. 너무 큰 값은 호출자가 조절한다.
        offset: 건너뛸 레코드 수(페이지네이션).
        search: 제목/기관명에 대한 부분일치(LIKE) 검색어. 공백은 무시(strip).

    Returns:
        조건에 맞는 `Announcement` 리스트. 첨부파일은 relationship lazy=selectin 에
        의해 동일 쿼리 라운드트립 안에서 함께 로딩된다.
    """
    statement = select(Announcement)

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
    # SQLite 는 nulls_last 를 지원하지만 이식성을 위해 CASE WHEN 을 대신 사용한다.
    statement = statement.order_by(
        # NULL 이면 1 (뒤쪽), 아니면 0 (앞쪽)
        (Announcement.deadline_at.is_(None)).asc(),
        Announcement.deadline_at.asc(),
        Announcement.id.desc(),
    )

    # 음수/0 방어: limit/offset 은 비정상 입력이어도 안전한 값으로 보정한다.
    safe_limit = max(int(limit), 1)
    safe_offset = max(int(offset), 0)
    statement = statement.offset(safe_offset).limit(safe_limit)

    return list(session.execute(statement).scalars().all())


def count_announcements(
    session: Session,
    status: Optional[AnnouncementStatus | str] = None,
    search: Optional[str] = None,
) -> int:
    """`list_announcements` 와 동일 조건의 전체 개수를 반환한다.

    목록 페이지네이션에서 '총 N건' 표시/페이지 수 계산에 사용한다.
    """
    statement = select(func.count()).select_from(Announcement)

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


def get_announcement(
    session: Session,
    announcement_id: int,
) -> Optional[tuple[Announcement, list[Attachment]]]:
    """내부 PK 로 공고와 첨부파일 목록을 함께 반환한다.

    첨부파일은 `id` 오름차순으로 정렬한다(UI 상의 안정적인 표시 순서).

    Args:
        session: 호출자가 제어하는 SQLAlchemy 세션.
        announcement_id: `Announcement.id`.

    Returns:
        - 공고가 존재하면 `(Announcement, [Attachment, ...])` 튜플.
        - 존재하지 않으면 `None`.
    """
    announcement = session.get(Announcement, announcement_id)
    if announcement is None:
        return None

    attachments = list(
        session.execute(
            select(Attachment)
            .where(Attachment.announcement_id == announcement_id)
            .order_by(Attachment.id.asc())
        )
        .scalars()
        .all()
    )
    return announcement, attachments


__all__ = [
    "upsert_announcement",
    "upsert_attachment",
    "list_announcements",
    "count_announcements",
    "get_announcement",
]
