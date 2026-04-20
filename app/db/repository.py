"""Repository 레이어: 공고 UPSERT 및 조회.

설계 원칙:
    - 모든 함수는 호출자가 전달한 `Session` 을 그대로 사용한다.
      트랜잭션 경계(commit/rollback)는 호출자가 `session_scope()` 등으로 제어한다.
      (리포지토리는 `flush()` 까지만 수행한다.)
    - 공고의 중복 판정 기준: `iris_announcement_id` (UNIQUE 컬럼).

payload 규약:
    - `upsert_announcement(session, payload)` 의 `payload` 는
      `Announcement` 의 컬럼명을 키로 갖는 매핑이다.
      최소 요구 키: `iris_announcement_id`, `title`, `status`.
      `status` 는 `AnnouncementStatus` Enum 또는 그 문자열 값("접수중"/"접수예정"/"마감") 중 하나.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Announcement, AnnouncementStatus

# 공고에서 사용자가 payload 로 덮어쓰지 말아야 하는 자동 관리 컬럼들.
_ANNOUNCEMENT_PROTECTED_FIELDS: frozenset[str] = frozenset({"id", "scraped_at", "updated_at"})

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

    허용 목록에 없는 키는 조용히 무시한다.
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
        limit: 페이지 크기. 1 이상이어야 한다.
        offset: 건너뛸 레코드 수(페이지네이션).
        search: 제목/기관명에 대한 부분일치(LIKE) 검색어. 공백은 무시(strip).

    Returns:
        조건에 맞는 `Announcement` 리스트.
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


__all__ = [
    "upsert_announcement",
    "list_announcements",
    "count_announcements",
]
