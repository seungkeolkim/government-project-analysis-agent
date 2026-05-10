"""Phase C — 공고 진행 상태 / 선점 repository.

설계 문서: ``docs/progress_org_design.md``.

핵심 책임:
    1. ``AnnouncementProgress`` CRUD (신규 작성 / 수정 / 삭제 / 조회).
    2. 선점 제약 — 한 canonical 당 ``status='진행'`` row 가 최대 1 개임을 보장.
       Phase B 패턴 (partial unique index 회피) 에 따라 DB UNIQUE 가 아니라
       app-level transactional 체크로 보장한다 (SQLite 단일 writer 가정).
    3. history 이관 — 사용자 변경 (``user_changed``) 또는 canonical 내용 변경
       감지 (``content_changed``) 시 기존 row 를 ``AnnouncementProgressHistory``
       로 archive 후 active 테이블에서 제거.
    4. summary 헬퍼 — 목록 페이지가 페이지당 추가 쿼리 1~2 개로 선점 조직 / 단계별
       카운터 / 본인 조직 활동 단계를 한꺼번에 조회할 수 있도록 한다.

트랜잭션 규약 (Phase B 와 동일 컨벤션):
    - 모든 함수는 호출자가 전달한 ``Session`` 을 그대로 사용한다.
    - flush 까지만 수행하고 commit 은 호출자가 결정한다.
    - 선점 제약 검증은 ``session.flush()`` 직전 SELECT 패턴으로 race condition
      을 회피한다 (SQLite 단일 writer 라 트랜잭션 안에서는 안전).

권한 규약 (Phase B 와 의도적 차이):
    - 본 repository 는 권한 검증을 수행하지 않는다 — 호출자(라우터) 가 본인
      소속 조직 검증 / 무소속 거부 / 외 조직 row 차단을 모두 결정한다.
    - "조직 멤버 누구나" 정책의 의미: row 작성자(``created_by_user_id``) 와
      modifier 가 달라도 같은 조직이면 변경 가능하다. 본 repository 는 그
      구분 없이 modifier_user_id 만 받아 ``created_by_user_id`` 를 갱신한다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementProgress,
    AnnouncementProgressArchiveReason,
    AnnouncementProgressHistory,
    AnnouncementProgressStatus,
    Organization,
    User,
    UserOrganization,
)


# ── 도메인 상수 ──────────────────────────────────────────────────────────────
#
# 진행 단계 우선순위 — 본인이 여러 조직에 소속되어 있고 그 중 여러 조직이 같은
# canonical 에 활동 row 를 가질 때 ``my_org_active`` 에 어떤 단계를 보고할지
# 결정하는 데 사용한다. "활동성이 강한 단계" 를 우선한다 — 진행 > 검토 > 관심 > 종료.
# task 00098: '종료' 도 본인 조직 강조가 가능해야 하므로 가장 낮은 우선순위로 포함.
_MY_ORG_ACTIVE_PRIORITY: tuple[AnnouncementProgressStatus, ...] = (
    AnnouncementProgressStatus.IN_PROGRESS,
    AnnouncementProgressStatus.REVIEW,
    AnnouncementProgressStatus.INTEREST,
    AnnouncementProgressStatus.DONE,
)


# ── 예외 ─────────────────────────────────────────────────────────────────────


class PreemptionConflict(Exception):
    """선점 제약 위반 — 다른 조직이 이미 ``status='진행'`` 인 상태에서
    본인 조직을 ``'진행'`` 으로 올리려는 시도가 있을 때 raise.

    라우터는 본 예외를 HTTP 409 Conflict 로 변환한다 (설계 문서 §3.1).

    Attributes:
        conflicting_organization_id: 이미 진행 중인 조직 PK.
        conflicting_organization_name: 사용자에게 표시할 조직명. JOIN 조회 결과가
            없으면 None — 라우터에서 fallback 메시지로 처리한다.
    """

    def __init__(
        self,
        conflicting_organization_id: int,
        conflicting_organization_name: str | None = None,
    ) -> None:
        """충돌 정보를 담아 예외 메시지를 구성한다."""
        self.conflicting_organization_id = conflicting_organization_id
        self.conflicting_organization_name = conflicting_organization_name
        if conflicting_organization_name:
            super().__init__(
                f"조직 {conflicting_organization_name!r} 가 이미 진행 또는 종료 중입니다 "
                f"(organization_id={conflicting_organization_id})."
            )
        else:
            super().__init__(
                f"organization_id={conflicting_organization_id} 가 이미 진행 또는 종료 중입니다."
            )


# ── 자료 구조 (summary 헬퍼 반환 타입) ──────────────────────────────────────


@dataclass(frozen=True)
class ProgressOrganizationRef:
    """선점 조직 참조 (id + name).

    ``ProgressSummary.in_progress_org`` 가 None 이 아닐 때 이 dataclass 가 채워진다.
    조직명이 비어 있는 비정상 데이터 방어를 위해 ``name`` 도 빈 문자열일 수 있다.
    """

    id: int
    name: str


@dataclass(frozen=True)
class ProgressSummary:
    """canonical_project 단위 진행 상태 요약.

    목록 셀 / hover 툴팁 / 다중 체크박스 필터에 필요한 데이터를 한 묶음으로
    제공한다. ``get_progress_summary_by_canonical_id_map`` 가 페이지당 1~2 회의
    쿼리로 N 개 canonical 에 대한 결과를 채워 N+1 회귀를 방지한다.

    Attributes:
        in_progress_org: ``status='진행'`` 인 조직 (있으면 1 개). 없으면 None.
        counter_review: ``status='검토'`` row 수 (단계별 분리, 종료 제외).
        counter_interest: ``status='관심'`` row 수.
        my_org_active: 본인 소속 조직 중 하나가 이 canonical 에 활동 row 를
            가지고 있을 때 그 단계 (``'관심'``/``'검토'``/``'진행'``/``'종료'`` 한글 문자열).
            여러 본인 조직이 동시에 활동 중이면 우선순위 진행>검토>관심>종료 으로
            가장 강한 단계 1 개.
            비로그인 (``user_id=None``) 또는 본인 활동 없으면 None.
        done_org: ``status='종료'`` 인 조직 (있으면 1 개, mutex 단일 보장).
            없으면 None. task 00098: UI 셀에서 '종료:팀명' 표시에 사용.
    """

    in_progress_org: ProgressOrganizationRef | None
    counter_review: int
    counter_interest: int
    my_org_active: str | None
    done_org: ProgressOrganizationRef | None = None


PROGRESS_SUMMARY_EMPTY: ProgressSummary = ProgressSummary(
    in_progress_org=None,
    counter_review=0,
    counter_interest=0,
    my_org_active=None,
    done_org=None,
)


# ── 내부 유틸 ────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """현재 시각을 timezone-aware UTC 로 반환한다.

    별도 헬퍼로 빼낸 이유: 테스트가 monkeypatch 로 시간 흐름을 제어할 수 있도록.
    """
    return datetime.now(tz=UTC)


def _coerce_status(value: AnnouncementProgressStatus | str) -> AnnouncementProgressStatus:
    """str / Enum 모두 허용해 ``AnnouncementProgressStatus`` 로 정규화한다.

    잘못된 값이면 ``ValueError``. 라우터·테스트가 한글 문자열로 넘기는 경우를
    위한 호환 계층.
    """
    if isinstance(value, AnnouncementProgressStatus):
        return value
    try:
        return AnnouncementProgressStatus(value)
    except ValueError as exc:
        raise ValueError(
            f"알 수 없는 progress status: {value!r}. "
            f"허용: {[member.value for member in AnnouncementProgressStatus]}"
        ) from exc


def _coerce_archive_reason(
    value: AnnouncementProgressArchiveReason | str,
) -> AnnouncementProgressArchiveReason:
    """str / Enum 모두 허용해 ``AnnouncementProgressArchiveReason`` 으로 정규화한다."""
    if isinstance(value, AnnouncementProgressArchiveReason):
        return value
    try:
        return AnnouncementProgressArchiveReason(value)
    except ValueError as exc:
        raise ValueError(
            f"알 수 없는 archive_reason: {value!r}. "
            f"허용: {[member.value for member in AnnouncementProgressArchiveReason]}"
        ) from exc


def _archive_progress_to_history(
    session: Session,
    current_row: AnnouncementProgress,
    *,
    archive_reason: AnnouncementProgressArchiveReason,
    archive_time: datetime | None = None,
) -> AnnouncementProgressHistory:
    """현재 row 를 history 로 복사한다.

    이 함수 자체는 active row 의 UPDATE / DELETE 를 수행하지 않는다 — 호출자가
    이어서 처리한다. ``created_at`` / ``updated_at`` 은 원본 값을 그대로 복사해
    "이관 시점에 살아있던 메타" 를 보존한다. ``archived_at`` 만 이관 시각으로
    채운다.

    Args:
        session: 호출자 세션.
        current_row: archive 대상이 되는 현재 row (영속 상태여야 함).
        archive_reason: ``user_changed`` 또는 ``content_changed``.
        archive_time: 테스트용 시각 오버라이드. None 이면 ``_utcnow()``.

    Returns:
        새로 생성된 ``AnnouncementProgressHistory``. flush 후 PK 가 채워진다.
    """
    history_row = AnnouncementProgressHistory(
        canonical_project_id=current_row.canonical_project_id,
        organization_id=current_row.organization_id,
        status=current_row.status,
        note=current_row.note,
        created_by_user_id=current_row.created_by_user_id,
        created_at=current_row.created_at,
        updated_at=current_row.updated_at,
        archived_at=archive_time or _utcnow(),
        archive_reason=archive_reason,
    )
    session.add(history_row)
    return history_row


# ── 선점 제약 ────────────────────────────────────────────────────────────────


def ensure_in_progress_unique(
    session: Session,
    *,
    canonical_project_id: int,
    organization_id: int,
) -> None:
    """선점 제약 (한 canonical 당 ``status='진행'`` 또는 ``'종료'`` 단일 조직) 을 검증한다.

    ``status`` 를 ``'진행'`` 또는 ``'종료'`` 로 바꾸려는 시점에 호출한다. 본인 조직
    (``organization_id``) 을 제외한 같은 canonical 의 다른 조직 row 중
    ``status IN ('진행', '종료')`` 인 row 가 존재하면 ``PreemptionConflict`` 를 raise 한다.

    '관심' / '검토' 는 선점 대상이 아니므로 이 함수를 호출하지 않는다.

    SQLite 단일 writer 가정하에 트랜잭션 안 SELECT → INSERT/UPDATE 사이에 다른
    writer 가 끼어들 수 없으므로 race condition 없이 안전하다 (``docs/db_portability.md``
    §3 + Phase B 컨벤션). Postgres 전환 시 같은 패턴이 race 가능성을 갖게 되지만,
    그 시점에 partial unique index 추가 또는 SELECT FOR UPDATE 보강을 별도 결정한다.

    Args:
        session: 호출자 세션.
        canonical_project_id: 검증 대상 canonical PK.
        organization_id: 본인 조직 PK — 이 조직의 row 는 충돌 검증에서 제외.

    Raises:
        PreemptionConflict: 다른 조직이 이미 진행 또는 종료 중일 때.
    """
    preempting_statuses = [
        AnnouncementProgressStatus.IN_PROGRESS,
        AnnouncementProgressStatus.DONE,
    ]
    conflicting = session.execute(
        select(AnnouncementProgress, Organization.name)
        .outerjoin(
            Organization, Organization.id == AnnouncementProgress.organization_id
        )
        .where(
            AnnouncementProgress.canonical_project_id == canonical_project_id,
            AnnouncementProgress.organization_id != organization_id,
            AnnouncementProgress.status.in_(preempting_statuses),
        )
        .limit(1)
    ).first()

    if conflicting is None:
        return

    existing_row, organization_name = conflicting
    raise PreemptionConflict(
        conflicting_organization_id=existing_row.organization_id,
        conflicting_organization_name=organization_name,
    )


# ── 조회 ─────────────────────────────────────────────────────────────────────


def get_progress(
    session: Session, canonical_project_id: int
) -> list[AnnouncementProgress]:
    """canonical 의 모든 조직별 현재 진행 상태 row 를 반환한다.

    상세 페이지의 "공고 진행 상태" 섹션 / 모달에서 풀어 표시할 때 사용한다.
    정렬 기준: 진행 단계 활동성이 강한 순 (진행 → 검토 → 관심 → 종료) 안에서는
    ``updated_at`` 내림차순 — UI 가 자체 정렬 필요시 재정렬한다.

    Args:
        session: 호출자 세션.
        canonical_project_id: 조회 대상 canonical PK.

    Returns:
        해당 canonical 의 ``AnnouncementProgress`` 리스트. 없으면 빈 리스트.
    """
    rows = session.execute(
        select(AnnouncementProgress)
        .where(AnnouncementProgress.canonical_project_id == canonical_project_id)
        .order_by(AnnouncementProgress.updated_at.desc())
    ).scalars().all()
    return list(rows)


def get_progress_by_id(
    session: Session, progress_id: int
) -> AnnouncementProgress | None:
    """PK 직접 조회. 라우터의 PATCH/DELETE 가 권한 검증 전 row 를 가져올 때 사용."""
    return session.get(AnnouncementProgress, progress_id)


def list_progress_history(
    session: Session, canonical_project_id: int
) -> list[AnnouncementProgressHistory]:
    """canonical 의 history row 를 archived_at 내림차순으로 반환한다.

    GET history 라우트가 비로그인 포함 모든 사용자에게 노출하는 데이터.
    """
    rows = session.execute(
        select(AnnouncementProgressHistory)
        .where(
            AnnouncementProgressHistory.canonical_project_id == canonical_project_id
        )
        .order_by(AnnouncementProgressHistory.archived_at.desc())
    ).scalars().all()
    return list(rows)


def get_progress_for_organization(
    session: Session,
    *,
    canonical_project_id: int,
    organization_id: int,
) -> AnnouncementProgress | None:
    """(canonical, organization) UNIQUE 키로 기존 row 를 조회한다.

    ``create_progress`` 가 "이미 row 가 있는지" 판정에 사용한다.
    """
    return session.execute(
        select(AnnouncementProgress).where(
            AnnouncementProgress.canonical_project_id == canonical_project_id,
            AnnouncementProgress.organization_id == organization_id,
        )
    ).scalar_one_or_none()


# ── CRUD — create / update / delete ─────────────────────────────────────────


def create_progress(
    session: Session,
    *,
    canonical_project_id: int,
    organization_id: int,
    status: AnnouncementProgressStatus | str,
    note: str | None,
    created_by_user_id: int | None,
    now: datetime | None = None,
) -> AnnouncementProgress:
    """진행 상태 row 를 생성한다.

    이미 같은 (canonical, organization) row 가 있으면 ``update_progress`` 에 위임
    한다 — 이 흐름은 라우터의 POST 가 멱등성 (이미 row 가 있으면 in-place 갱신)
    을 갖도록 한다.

    ``status='진행'`` 으로 올리려고 하면 호출 전에 ``ensure_in_progress_unique`` 를
    먼저 통과해야 한다 — 본 함수 안에서도 안전망으로 한 번 더 호출한다.

    Args:
        session: 호출자 세션.
        canonical_project_id: 진행 상태 대상 canonical PK.
        organization_id: 입장 표명 조직 PK.
        status: 4 단계 enum 또는 그 한글 문자열.
        note: 자유 메모. 빈 문자열은 None 으로 정규화하지 않고 그대로 저장한다.
        created_by_user_id: 작성자 (= "마지막 수정자") PK. None 허용 (예: 시스템
            도구 — 보통은 라우터가 current_user.id 를 넣는다).
        now: 시각 오버라이드 (테스트용).

    Returns:
        새로 생성되었거나 update 위임으로 갱신된 ``AnnouncementProgress``.

    Raises:
        PreemptionConflict: ``status='진행'`` 인데 다른 조직이 이미 진행 중일 때.
        ValueError: status 가 enum 도메인 밖일 때.
    """
    coerced_status = _coerce_status(status)
    now_ts = now or _utcnow()

    existing = get_progress_for_organization(
        session,
        canonical_project_id=canonical_project_id,
        organization_id=organization_id,
    )
    if existing is not None:
        # 같은 (canonical, organization) row 가 이미 있으면 update_progress 에 위임.
        # POST 의 멱등성 — 이미 row 가 있으면 history 이관 + UPDATE 흐름.
        return update_progress(
            session,
            progress_id=existing.id,
            status=coerced_status,
            note=note,
            modifier_user_id=created_by_user_id,
            now=now_ts,
        )

    # 신규 INSERT — '진행' 또는 '종료' 로 올릴 때 선점 제약 안전망.
    if coerced_status in {
        AnnouncementProgressStatus.IN_PROGRESS,
        AnnouncementProgressStatus.DONE,
    }:
        ensure_in_progress_unique(
            session,
            canonical_project_id=canonical_project_id,
            organization_id=organization_id,
        )

    new_row = AnnouncementProgress(
        canonical_project_id=canonical_project_id,
        organization_id=organization_id,
        status=coerced_status,
        note=note,
        created_by_user_id=created_by_user_id,
        created_at=now_ts,
        updated_at=now_ts,
    )
    session.add(new_row)
    session.flush()
    logger.debug(
        "AnnouncementProgress 신규 생성: id={} canonical={} organization_id={} status={}",
        new_row.id,
        canonical_project_id,
        organization_id,
        coerced_status.value,
    )
    return new_row


def update_progress(
    session: Session,
    *,
    progress_id: int,
    status: AnnouncementProgressStatus | str,
    note: str | None,
    modifier_user_id: int | None,
    now: datetime | None = None,
) -> AnnouncementProgress:
    """기존 row 를 history 이관 후 in-place UPDATE 한다.

    Phase B 의 ``user_changed`` 패턴 그대로 — archive 후 UPDATE. ``status='진행'``
    으로 변경하는 경우에만 선점 제약을 검증한다.

    Args:
        session: 호출자 세션.
        progress_id: 갱신 대상 row PK.
        status: 새 status (enum 또는 한글 문자열).
        note: 새 note. None 이면 NULL 로 저장.
        modifier_user_id: 갱신자 PK. ``created_by_user_id`` 가 이 값으로 갱신된다
            ("마지막 수정자" 메타). 같은 조직의 다른 멤버일 수 있다.
        now: 시각 오버라이드.

    Returns:
        갱신된 ``AnnouncementProgress``.

    Raises:
        ValueError: row 가 없거나 status enum 도메인 밖일 때.
        PreemptionConflict: ``status='진행'`` 인데 다른 조직이 이미 진행 중일 때.
    """
    coerced_status = _coerce_status(status)
    now_ts = now or _utcnow()

    current_row = session.get(AnnouncementProgress, progress_id)
    if current_row is None:
        raise ValueError(
            f"AnnouncementProgress(id={progress_id}) row 가 존재하지 않습니다."
        )

    # '진행' 또는 '종료' 로 변경할 때 선점 제약 검증.
    if coerced_status in {
        AnnouncementProgressStatus.IN_PROGRESS,
        AnnouncementProgressStatus.DONE,
    }:
        ensure_in_progress_unique(
            session,
            canonical_project_id=current_row.canonical_project_id,
            organization_id=current_row.organization_id,
        )

    # 1) 기존 메타를 history 로 archive — created_at / updated_at / status / note 모두
    #    원본 값으로 복사. archive_reason='user_changed'.
    _archive_progress_to_history(
        session,
        current_row,
        archive_reason=AnnouncementProgressArchiveReason.USER_CHANGED,
        archive_time=now_ts,
    )
    session.flush()

    # 2) in-place UPDATE — updated_at 은 onupdate 콜백이 자동 갱신하지만 명시적
    #    now_ts 를 같이 적어 테스트의 시간 오버라이드 시도와 정합.
    current_row.status = coerced_status
    current_row.note = note
    current_row.created_by_user_id = modifier_user_id
    current_row.updated_at = now_ts
    session.flush()

    logger.debug(
        "AnnouncementProgress 갱신: id={} status={} modifier={}",
        progress_id,
        coerced_status.value,
        modifier_user_id,
    )
    return current_row


def delete_progress(
    session: Session,
    *,
    progress_id: int,
    modifier_user_id: int | None,
    now: datetime | None = None,
) -> bool:
    """row 를 history 로 archive 후 active 테이블에서 삭제한다.

    Args:
        session: 호출자 세션.
        progress_id: 삭제 대상 row PK.
        modifier_user_id: 삭제 트리거 사용자 PK. archive 시 ``created_by_user_id``
            는 **갱신하지 않는다** — 이력에는 "마지막 수정자" 그대로 보존되어야
            한다 (사용자가 누구를 보고 만든 row 였는지 흔적 유지).
        now: 시각 오버라이드.

    Returns:
        True 이면 삭제 성공, False 이면 row 가 없어서 no-op.
    """
    # modifier_user_id 는 향후 감사 로그용 — 현재 archive 시에는 created_by_user_id
    # 를 덮어쓰지 않는다. 미사용 인자 경고 회피용 한 줄.
    _ = modifier_user_id

    now_ts = now or _utcnow()
    current_row = session.get(AnnouncementProgress, progress_id)
    if current_row is None:
        return False

    _archive_progress_to_history(
        session,
        current_row,
        archive_reason=AnnouncementProgressArchiveReason.USER_CHANGED,
        archive_time=now_ts,
    )
    session.flush()
    session.delete(current_row)
    session.flush()

    logger.debug(
        "AnnouncementProgress 삭제: id={} canonical={} organization_id={}",
        progress_id,
        current_row.canonical_project_id,
        current_row.organization_id,
    )
    return True


# ── content_changed reset ───────────────────────────────────────────────────


def reset_progress_for_canonical(
    session: Session,
    canonical_project_id: int,
    *,
    archive_reason: (
        AnnouncementProgressArchiveReason | str
    ) = AnnouncementProgressArchiveReason.CONTENT_CHANGED,
    archive_time: datetime | None = None,
) -> int:
    """canonical 내용 변경 감지 시 모든 활성 progress row 를 history 로 archive 후 삭제한다.

    Phase 1a 변경 감지 (title/status/agency/deadline_at 변경) 의 (d) new_version
    분기와 2차 감지 (첨부 sha256 기반) 경로 모두에서 호출된다 — 호출 위치는
    ``app/db/repository.py::_reset_user_state_on_content_change``. 본 함수는 그 hook
    한 줄에서 import 되어 호출된다.

    Args:
        session: 호출자 세션. flush 까지만 수행하며 commit 은 호출자 책임.
        canonical_project_id: 리셋 대상 canonical PK.
        archive_reason: 기본 ``content_changed``. 일반적으로 변경하지 않는다.
        archive_time: 시각 오버라이드 (테스트용).

    Returns:
        archive 된 row 수. 본 canonical 에 활성 row 가 없으면 0.
    """
    coerced_reason = _coerce_archive_reason(archive_reason)
    now_ts = archive_time or _utcnow()

    rows = session.execute(
        select(AnnouncementProgress).where(
            AnnouncementProgress.canonical_project_id == canonical_project_id
        )
    ).scalars().all()

    archived_count = 0
    for row in rows:
        _archive_progress_to_history(
            session,
            row,
            archive_reason=coerced_reason,
            archive_time=now_ts,
        )
        session.delete(row)
        archived_count += 1

    if archived_count:
        session.flush()
        logger.info(
            "AnnouncementProgress content_changed reset: canonical={} archived={} reason={}",
            canonical_project_id,
            archived_count,
            coerced_reason.value,
        )
    return archived_count


# ── summary 헬퍼 ─────────────────────────────────────────────────────────────


def _resolve_my_organization_ids(
    session: Session, user_id: int | None
) -> frozenset[int]:
    """user_id 의 소속 조직 PK 집합을 조회한다.

    user_id=None (비로그인) 또는 무소속이면 빈 집합. summary 헬퍼와 다중
    체크박스 필터 (``mine_in_progress`` / ``mine_in_review``) 양쪽에서 사용한다.
    """
    if user_id is None:
        return frozenset()
    rows = session.execute(
        select(UserOrganization.organization_id).where(
            UserOrganization.user_id == user_id
        )
    ).scalars().all()
    return frozenset(rows)


def get_progress_summary_by_canonical_id_map(
    session: Session,
    *,
    user_id: int | None,
    canonical_project_ids: Iterable[int],
) -> dict[int, ProgressSummary]:
    """canonical_project_id 별 진행 상태 요약 맵을 페이지당 1~2 개 쿼리로 반환한다.

    Phase B ``get_relevance_summary_by_canonical_id_map`` 패턴 그대로:
        - canonical_ids 가 비어 있으면 빈 dict (쿼리 없음).
        - 단일 SELECT 로 모든 active row + 조직명 JOIN → Python 레벨에서 canonical
          별로 버킷팅하면서 ``in_progress_org`` / 카운터 / ``my_org_active`` 를 계산.

    user_id=None (비로그인) 일 때는 ``my_org_active`` 가 항상 None 이며, 다른
    필드는 로그인 사용자와 동일한 결과를 반환한다 (사용자 원문 결정 — 비로그인 =
    로그인 동일 노출).

    Args:
        session: 호출자 세션.
        user_id: '본인' 기준 사용자 PK. 비로그인이면 None.
        canonical_project_ids: 요약 대상 canonical_project_id 들의 iterable.

    Returns:
        ``{canonical_project_id: ProgressSummary}`` 맵. row 가 하나라도 있는
        canonical 만 키로 포함된다. 호출자는
        ``map.get(cid, PROGRESS_SUMMARY_EMPTY)`` 패턴을 사용한다.
        canonical_project_ids 가 비어 있으면 빈 dict 반환 (쿼리 없음).
    """
    ids = list(canonical_project_ids)
    if not ids:
        return {}

    my_org_ids = _resolve_my_organization_ids(session, user_id)

    # 단일 SELECT — active progress + 조직명 JOIN (선점 라인 표시용 조직명 포함).
    rows = session.execute(
        select(AnnouncementProgress, Organization.name)
        .outerjoin(
            Organization, Organization.id == AnnouncementProgress.organization_id
        )
        .where(AnnouncementProgress.canonical_project_id.in_(ids))
    ).all()

    # canonical 별 버킷팅 + 카운터 + 본인 활동 단계 계산.
    in_progress_per_canonical: dict[int, ProgressOrganizationRef] = {}
    done_per_canonical: dict[int, ProgressOrganizationRef] = {}
    counter_review_per_canonical: dict[int, int] = {}
    counter_interest_per_canonical: dict[int, int] = {}
    my_org_actives_per_canonical: dict[int, set[AnnouncementProgressStatus]] = {}

    for progress_row, organization_name in rows:
        canonical_id = progress_row.canonical_project_id
        status = progress_row.status

        if status == AnnouncementProgressStatus.IN_PROGRESS:
            # 한 canonical 에 IN_PROGRESS 는 최대 1 개 (선점 제약). 여러 개가
            # 들어와도 첫 번째 row 를 사용한다 (defensive — 정상 데이터에선 1 개).
            if canonical_id not in in_progress_per_canonical:
                in_progress_per_canonical[canonical_id] = ProgressOrganizationRef(
                    id=progress_row.organization_id,
                    name=organization_name or "",
                )
        elif status == AnnouncementProgressStatus.DONE:
            # DONE 도 mutex 단일 조직 보장. 첫 번째 row 만 사용 (defensive).
            if canonical_id not in done_per_canonical:
                done_per_canonical[canonical_id] = ProgressOrganizationRef(
                    id=progress_row.organization_id,
                    name=organization_name or "",
                )
        elif status == AnnouncementProgressStatus.REVIEW:
            counter_review_per_canonical[canonical_id] = (
                counter_review_per_canonical.get(canonical_id, 0) + 1
            )
        elif status == AnnouncementProgressStatus.INTEREST:
            counter_interest_per_canonical[canonical_id] = (
                counter_interest_per_canonical.get(canonical_id, 0) + 1
            )

        # task 00098: DONE 단계도 본인 조직 강조(종료:팀명) 를 위해 my_org_active 에 포함.
        if user_id is not None and progress_row.organization_id in my_org_ids:
            my_org_actives_per_canonical.setdefault(canonical_id, set()).add(status)

    # row 가 한 건이라도 있는 canonical 만 결과에 넣는다.
    canonical_ids_seen = (
        set(in_progress_per_canonical)
        | set(done_per_canonical)
        | set(counter_review_per_canonical)
        | set(counter_interest_per_canonical)
        | set(my_org_actives_per_canonical)
    )
    summary_map: dict[int, ProgressSummary] = {}
    for canonical_id in canonical_ids_seen:
        active_statuses = my_org_actives_per_canonical.get(canonical_id, set())
        my_org_active_value: str | None = None
        if active_statuses:
            for candidate in _MY_ORG_ACTIVE_PRIORITY:
                if candidate in active_statuses:
                    my_org_active_value = candidate.value
                    break

        summary_map[canonical_id] = ProgressSummary(
            in_progress_org=in_progress_per_canonical.get(canonical_id),
            counter_review=counter_review_per_canonical.get(canonical_id, 0),
            counter_interest=counter_interest_per_canonical.get(canonical_id, 0),
            my_org_active=my_org_active_value,
            done_org=done_per_canonical.get(canonical_id),
        )

    return summary_map


# ── 목록 셀 expand 본문용 batch 조회 (task 00097-5) ─────────────────────────


@dataclass(frozen=True)
class ProgressRowDetail:
    """목록 셀 expand 행에 풀어 표시할 진행 상태 row 의 직렬화 단위.

    AnnouncementProgress + Organization + 마지막 수정자 username 을 single SELECT
    + LEFT JOIN 으로 미리 묶어두어 템플릿 측에서 lazy load 가 발생하지 않도록 한다.
    AnnouncementProgress 의 relationship lazy='select' 가 셀 단위로 N+1 을 일으키는
    회귀를 차단한다.

    Attributes:
        progress_id: AnnouncementProgress.id.
        organization_id: 표명 조직 PK.
        organization_name: 표시용 조직명. 비정상 데이터 방어로 None 허용.
        status_value: 한글 status enum 문자열 ('관심' / '검토' / '진행' / '종료').
        status_name: enum.name (interest / review / in_progress / done) — CSS 클래스 분기용.
        note: 자유 메모 텍스트. 빈 문자열 / None.
        updated_at: tz-aware UTC datetime — 템플릿이 kst_format 필터로 표시한다.
        last_modifier_username: 마지막 수정자 username. SET NULL 또는 미존재면 None.
    """

    progress_id: int
    organization_id: int
    organization_name: str | None
    status_value: str
    status_name: str
    note: str | None
    updated_at: datetime
    last_modifier_username: str | None


def get_progress_rows_by_canonical_id_map(
    session: Session,
    canonical_project_ids: Iterable[int],
) -> dict[int, list[ProgressRowDetail]]:
    """canonical_id 별로 표시용 ProgressRowDetail 리스트를 페이지당 1 회 SELECT 로 채운다.

    목록 셀 expand 본문 (조직별 단계·작성자·시점·note 풀어 표시) 을 server-side
    렌더하는 데 사용한다. Phase B ``get_relevance_summary_by_canonical_id_map`` 과
    동일한 N+1 회피 패턴 — IN clause 로 모든 canonical 을 한 번에 가져오고 Python
    레벨에서 canonical 별 버킷팅한다.

    정렬: 단계 활동성 우선순위 (진행 → 검토 → 관심 → 종료) 다음에 ``updated_at``
    내림차순. 셀 expand 가 펼쳐졌을 때 사용자 시선이 가장 강한 활동부터 자연스럽게
    위로 향하도록 한 결정 (설계 문서 §4.2 와 정합).

    Args:
        session: 호출자 세션.
        canonical_project_ids: 요약 대상 canonical_project_id 들의 iterable.

    Returns:
        ``{canonical_project_id: [ProgressRowDetail, ...]}`` 맵. row 가 하나라도
        있는 canonical 만 키로 포함된다. canonical_project_ids 가 비어 있으면 빈
        dict 반환 (쿼리 없음).
    """
    ids = list(canonical_project_ids)
    if not ids:
        return {}

    rows = session.execute(
        select(
            AnnouncementProgress,
            Organization.name,
            User.username,
        )
        .outerjoin(
            Organization, Organization.id == AnnouncementProgress.organization_id
        )
        .outerjoin(User, User.id == AnnouncementProgress.created_by_user_id)
        .where(AnnouncementProgress.canonical_project_id.in_(ids))
    ).all()

    # 활동성 우선순위 정렬 — Python 레벨에서 단순 sort.
    status_priority: dict[AnnouncementProgressStatus, int] = {
        AnnouncementProgressStatus.IN_PROGRESS: 0,
        AnnouncementProgressStatus.REVIEW: 1,
        AnnouncementProgressStatus.INTEREST: 2,
        AnnouncementProgressStatus.DONE: 3,
    }

    buckets: dict[int, list[ProgressRowDetail]] = {}
    for progress_row, organization_name, last_modifier_username in rows:
        detail = ProgressRowDetail(
            progress_id=progress_row.id,
            organization_id=progress_row.organization_id,
            organization_name=organization_name,
            status_value=progress_row.status.value,
            status_name=progress_row.status.name.lower(),
            note=progress_row.note,
            updated_at=progress_row.updated_at,
            last_modifier_username=last_modifier_username,
        )
        buckets.setdefault(progress_row.canonical_project_id, []).append(detail)

    # canonical 별로 (status_priority, -updated_at) 순 정렬.
    for canonical_id_value, detail_list in buckets.items():
        detail_list.sort(
            key=lambda detail: (
                status_priority.get(
                    AnnouncementProgressStatus(detail.status_value), 99
                ),
                -detail.updated_at.timestamp(),
            )
        )
    return buckets


# ── 다중 체크박스 필터 (task 00097-6 시나리오 15·16) ──────────────────────
#
# UI 라벨 ↔ URL 파라미터 키 매핑 (설계 문서 §8.1 — 영문 키 채택).
# 비로그인은 mine_* 두 키를 자동 무시 (silent drop, 시나리오 16).

PROGRESS_FILTER_NONE: str = "none"
PROGRESS_FILTER_OTHER_IN_PROGRESS: str = "other_in_progress"
PROGRESS_FILTER_MINE_IN_PROGRESS: str = "mine_in_progress"
PROGRESS_FILTER_MINE_IN_REVIEW: str = "mine_in_review"

PROGRESS_FILTER_ALL_KEYS: frozenset[str] = frozenset(
    {
        PROGRESS_FILTER_NONE,
        PROGRESS_FILTER_OTHER_IN_PROGRESS,
        PROGRESS_FILTER_MINE_IN_PROGRESS,
        PROGRESS_FILTER_MINE_IN_REVIEW,
    }
)

# 비로그인 컨텍스트에서만 silent drop 되는 옵션 — 본인 소속 조직 정보가 없으면
# mine_* 두 옵션은 의미가 없다 (사용자 결정 + 설계 문서 §8.2).
PROGRESS_FILTER_REQUIRES_LOGIN_KEYS: frozenset[str] = frozenset(
    {PROGRESS_FILTER_MINE_IN_PROGRESS, PROGRESS_FILTER_MINE_IN_REVIEW}
)


def sanitize_progress_filter_options(
    raw_options: Iterable[str] | None,
    *,
    is_authenticated: bool,
) -> frozenset[str]:
    """진행 상태 필터 query param 값을 정규화·검증한다.

    동작:
        - 알 수 없는 키 (PROGRESS_FILTER_ALL_KEYS 밖) 은 silent drop.
        - 비로그인 (``is_authenticated=False``) 은 mine_* 두 키 silent drop.
        - 빈 문자열·None 은 빈 frozenset.
        - 콤마 구분 형식 ('progress=A,B') 도 허용 — 각 토큰을 분리해 평탄화한다.

    Args:
        raw_options: query param 으로 들어온 값들 (각 원소는 'A' 또는 'A,B' 형태 가능).
        is_authenticated: 현재 사용자가 로그인 상태인지 여부.

    Returns:
        정규화된 옵션 키 frozenset. 호출자가 ``apply_progress_filter`` 에 그대로 전달.
    """
    if raw_options is None:
        return frozenset()
    flattened: set[str] = set()
    for entry in raw_options:
        if entry is None:
            continue
        for token in str(entry).split(","):
            token = token.strip()
            if not token:
                continue
            if token not in PROGRESS_FILTER_ALL_KEYS:
                # 알 수 없는 키 — silent drop (정상 사용자 호출 외 봇·구버전 URL 방어).
                continue
            if not is_authenticated and token in PROGRESS_FILTER_REQUIRES_LOGIN_KEYS:
                # 비로그인은 mine_* silent drop — 401 거부 아님 (설계 문서 §8.2).
                continue
            flattened.add(token)
    return frozenset(flattened)


def apply_progress_filter(
    statement,
    options: Iterable[str] | None,
    my_organization_ids: Iterable[int] | None,
):
    """진행 상태 다중 체크박스 필터를 announcements 쿼리에 적용한다.

    설계 문서 §8.4 의 EXISTS 서브쿼리 패턴. 옵션이 여러 개면 OR 결합 (다중 선택
    의미: '하나라도 만족').

    EXISTS 서브쿼리는 ``Announcement.canonical_group_id`` 와
    ``AnnouncementProgress.canonical_project_id`` 를 매칭한다 — 모델 컬럼명이 다르나
    같은 canonical_projects.id 를 가리킨다 (정합).

    Args:
        statement: SQLAlchemy select / count statement.
        options: sanitize_progress_filter_options 의 반환값 (또는 비어 있는 iterable).
        my_organization_ids: 본인 소속 조직 PK 집합. 비로그인 / 무소속이면 빈 iterable.
            mine_* 옵션은 sanitize 단계에서 이미 silent drop 되었으므로 본 함수에서는
            ``my_organization_ids`` 가 비어 있어도 안전하다.

    Returns:
        WHERE 절이 추가된 statement. options 가 비어 있으면 statement 그대로 반환.
    """
    options_set = set(options) if options is not None else set()
    if not options_set:
        return statement

    my_org_ids_list = list(my_organization_ids) if my_organization_ids is not None else []

    or_clauses = []

    if PROGRESS_FILTER_NONE in options_set:
        # 아무 조직도 status='진행' 이 아닌 canonical.
        or_clauses.append(
            ~exists().where(
                AnnouncementProgress.canonical_project_id == Announcement.canonical_group_id,
                AnnouncementProgress.status == AnnouncementProgressStatus.IN_PROGRESS,
            )
        )

    if PROGRESS_FILTER_OTHER_IN_PROGRESS in options_set:
        # 본인 소속 외 조직이 status='진행'. 무소속이면 'NOT IN (-1)' 로
        # 사실상 모든 진행 row 매칭 (다른 조직 = 모든 조직).
        excluded_org_ids = my_org_ids_list or [-1]
        or_clauses.append(
            exists().where(
                AnnouncementProgress.canonical_project_id == Announcement.canonical_group_id,
                AnnouncementProgress.status == AnnouncementProgressStatus.IN_PROGRESS,
                AnnouncementProgress.organization_id.notin_(excluded_org_ids),
            )
        )

    # mine_* 두 옵션은 sanitize 에서 비로그인이면 이미 drop. 안전망으로 my_org_ids 도 검사.
    if PROGRESS_FILTER_MINE_IN_PROGRESS in options_set and my_org_ids_list:
        or_clauses.append(
            exists().where(
                AnnouncementProgress.canonical_project_id == Announcement.canonical_group_id,
                AnnouncementProgress.status == AnnouncementProgressStatus.IN_PROGRESS,
                AnnouncementProgress.organization_id.in_(my_org_ids_list),
            )
        )

    if PROGRESS_FILTER_MINE_IN_REVIEW in options_set and my_org_ids_list:
        or_clauses.append(
            exists().where(
                AnnouncementProgress.canonical_project_id == Announcement.canonical_group_id,
                AnnouncementProgress.status == AnnouncementProgressStatus.REVIEW,
                AnnouncementProgress.organization_id.in_(my_org_ids_list),
            )
        )

    if not or_clauses:
        return statement
    return statement.where(or_(*or_clauses))


# ── 유저 소속 조직 노출 (라우터 / 필터에서 재사용) ──────────────────────────


def list_user_organization_ids(session: Session, user_id: int | None) -> list[int]:
    """``_resolve_my_organization_ids`` 의 list 버전.

    라우터나 필터 SQL 분기에서 ``IN ()`` clause 로 직접 사용하기 위한 공개 헬퍼.
    user_id=None 또는 무소속이면 빈 리스트 반환.
    """
    return list(_resolve_my_organization_ids(session, user_id))


__all__: Sequence[str] = (
    "PROGRESS_FILTER_ALL_KEYS",
    "PROGRESS_FILTER_MINE_IN_PROGRESS",
    "PROGRESS_FILTER_MINE_IN_REVIEW",
    "PROGRESS_FILTER_NONE",
    "PROGRESS_FILTER_OTHER_IN_PROGRESS",
    "PROGRESS_FILTER_REQUIRES_LOGIN_KEYS",
    "PROGRESS_SUMMARY_EMPTY",
    "PreemptionConflict",
    "ProgressOrganizationRef",
    "ProgressRowDetail",
    "ProgressSummary",
    "apply_progress_filter",
    "create_progress",
    "delete_progress",
    "ensure_in_progress_unique",
    "get_progress",
    "get_progress_by_id",
    "get_progress_for_organization",
    "get_progress_rows_by_canonical_id_map",
    "get_progress_summary_by_canonical_id_map",
    "list_progress_history",
    "list_user_organization_ids",
    "reset_progress_for_canonical",
    "sanitize_progress_filter_options",
    "update_progress",
)
