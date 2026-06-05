"""스케줄 SSOT(``scheduled_jobs`` 테이블) 위의 데이터 접근 계층 (task 00157).

배경
----
task 155·156 을 거치며 스케줄 SSOT 가 ``system_settings`` JSON 키와 기동 시
설치되는 OS crontab(외부 파일)로 이원화돼, 관리자 직관성·백업 편의가 떨어졌다.
task 00157 은 모든 스케줄 트리거(공고 수집·백업·Daily Report·GC)를 단일 관계형
테이블 :class:`app.db.models.ScheduledJob` 로 모아 SSOT 를 DB 한 곳으로 되돌린다.

이 모듈은 그 테이블 위의 **유일한 데이터 접근 진입점**이다. crontab 생성기와 admin
라우트(00157-2)가 system_settings 의 여러 키를 직접 읽던 것을, 앞으로는 이 store 의
통합 API(list/get/add/toggle/delete/upsert) 한 곳만 호출하게 만든다.

잡 종류(job_kind)별 카디널리티
------------------------------
- ``scrape_general``: 여러 row(N건). 관리자가 cron/interval + active_sources 를
  자유롭게 추가/삭제한다 → :func:`add_general_schedule` / :func:`delete_scheduled_job`.
- ``backup`` / ``daily_report`` / ``gc``: 각각 단일 row(싱글턴). 트리거 cron 과
  enabled 만 갱신한다 → :func:`upsert_singleton_schedule`. 존재하지 않으면 기본값으로
  시드된다(:func:`ensure_default_seed_jobs`).

설계 메모
---------
- 모든 함수는 호출자가 넘긴 :class:`sqlalchemy.orm.Session` 위에서 동작하며
  ``commit`` 하지 않는다. 트랜잭션 경계는 호출 측(``session_scope``)이 관리한다
  (기존 schedule_store / backup.service 의 계약과 동일).
- ``autoflush=False`` 세션에서 add→read 가 연달아 일어날 때(예: admin 이 스케줄을
  추가하고 곧바로 crontab 생성기가 다시 읽는 경우) 직전 INSERT 가 식별 맵에 잡히고
  대리 키 id 가 채워지도록 쓰기 직후 ``flush`` 한다. flush 는 트랜잭션을 닫지
  않으므로 commit 계약과 충돌하지 않는다.
- cron 표현식 검증은 system crontab 의 단일 진실 검증기
  :func:`app.scheduler.crontab_generator.validate_cron_expression` 를 재사용한다
  (요일 보정 없이 필드 범위만 검증·정규화).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ScheduledJob
from app.scheduler.constants import (
    DEFAULT_GC_ORPHAN_CRON,
    JOB_KIND_BACKUP,
    JOB_KIND_DAILY_REPORT,
    JOB_KIND_GC,
    JOB_KIND_SCRAPE_GENERAL,
    MAX_INTERVAL_HOURS,
    SINGLETON_JOB_KINDS,
    TRIGGER_TYPE_CRON,
    TRIGGER_TYPE_INTERVAL,
)
from app.scheduler.crontab_generator import (
    CronExpressionError,
    validate_cron_expression,
)
from app.timezone import now_utc


class ScheduledJobConfigError(ValueError):
    """scheduled_jobs 입력값이 올바르지 않을 때 발생한다.

    cron 표현식 오류, interval 범위 위반, 알 수 없는 job_kind/trigger_type 등 사용자
    입력 검증 실패에 사용한다. UI 에서 flash 메시지로 노출하기 좋도록 ``ValueError``
    를 상속한다.
    """


@dataclass
class ScheduledJobRecord:
    """scheduled_jobs row 1건을 표현하는 typed 레코드.

    ORM 모델을 그대로 노출하지 않고 이 dataclass 로 환원해, 소비자(crontab 생성기·
    admin 라우트)가 세션 만료/지연 로딩에 영향받지 않는 안정적인 값 객체를 받게 한다.

    Attributes:
        id:              스케줄 식별자(대리 키).
        job_kind:        잡 종류. ``app.scheduler.constants.JOB_KIND_*``.
        trigger_type:    :data:`~app.scheduler.constants.TRIGGER_TYPE_CRON` 또는
                         :data:`~app.scheduler.constants.TRIGGER_TYPE_INTERVAL`.
        cron_expression: cron 트리거의 표준 5-필드 표현식. interval 이면 None.
        interval_hours:  interval 트리거의 '매 N시간' 정수. cron 이면 None.
        active_sources:  scrape_general 전용 source id 리스트. 빈 리스트면 전체.
        enabled:         활성 여부. False 면 crontab 라인에서 제외.
    """

    id: int
    job_kind: str
    trigger_type: str
    cron_expression: str | None = None
    interval_hours: int | None = None
    active_sources: list[str] = field(default_factory=list)
    enabled: bool = True


# ──────────────────────────────────────────────────────────────
# 내부 헬퍼 — 검증/정규화/변환
# ──────────────────────────────────────────────────────────────


def _to_record(model: ScheduledJob) -> ScheduledJobRecord:
    """ORM :class:`ScheduledJob` 을 :class:`ScheduledJobRecord` 로 환원한다.

    Args:
        model: DB 에서 로드된 ORM 인스턴스.

    Returns:
        세션 독립적인 값 객체. active_sources 가 NULL 이면 빈 리스트로 정규화한다.
    """
    raw_sources = model.active_sources or []
    return ScheduledJobRecord(
        id=model.id,
        job_kind=model.job_kind,
        trigger_type=model.trigger_type,
        cron_expression=model.cron_expression,
        interval_hours=model.interval_hours,
        active_sources=[str(source) for source in raw_sources],
        enabled=bool(model.enabled),
    )


def _normalize_active_sources(active_sources: list[str] | None) -> list[str]:
    """active_sources 입력을 공백 제거·빈 토큰 제거한 리스트로 정규화한다.

    Args:
        active_sources: 원본 source id 목록(None 허용).

    Returns:
        앞뒤 공백이 제거되고 빈 문자열이 걸러진 source id 리스트. None 이면 빈
        리스트(= 전체 수집).
    """
    if not active_sources:
        return []
    return [source.strip() for source in active_sources if source.strip()]


def _validate_cron_expression(cron_expression: str | None) -> str:
    """cron 트리거 표현식을 단일 진실 검증기로 검증·정규화한다.

    Args:
        cron_expression: 사용자 입력 cron 표현식.

    Returns:
        공백이 단정하게 정리된 5-필드 표현식.

    Raises:
        ScheduledJobConfigError: 표현식이 비었거나 crontab 규약을 위반한 경우.
    """
    if not cron_expression or not cron_expression.strip():
        raise ScheduledJobConfigError("cron 트리거는 cron_expression 이 필요합니다.")
    try:
        return validate_cron_expression(cron_expression)
    except CronExpressionError as exc:
        # crontab 검증기의 메시지를 그대로 노출하되, 저장소 계층 예외 타입으로 변환한다.
        raise ScheduledJobConfigError(str(exc)) from exc


def _validate_interval_hours(interval_hours: int | None) -> int:
    """interval 트리거의 '매 N시간' 값이 1~MAX_INTERVAL_HOURS 범위인지 검증한다.

    Args:
        interval_hours: '매 N시간' 정수.

    Returns:
        검증을 통과한 정수.

    Raises:
        ScheduledJobConfigError: 양의 정수가 아니거나 :data:`MAX_INTERVAL_HOURS` 를
            초과한 경우.
    """
    if not isinstance(interval_hours, int) or interval_hours <= 0:
        raise ScheduledJobConfigError(
            f"interval_hours 는 양의 정수여야 합니다 (입력: {interval_hours!r})."
        )
    if interval_hours > MAX_INTERVAL_HOURS:
        raise ScheduledJobConfigError(
            f"interval 은 최대 {MAX_INTERVAL_HOURS}시간까지입니다. 그 이상은 "
            f"cron 표현식을 사용하세요 (입력: {interval_hours}시간)."
        )
    return interval_hours


# ──────────────────────────────────────────────────────────────
# 조회 API
# ──────────────────────────────────────────────────────────────


def list_scheduled_jobs(
    session: Session, *, job_kind: str | None = None
) -> list[ScheduledJobRecord]:
    """scheduled_jobs 전체(또는 특정 job_kind)를 id 오름차순으로 반환한다.

    crontab 생성기(00157-2)가 모든 잡을 한 번에 읽는 진입점이다. 비활성 항목도
    포함하므로(필터링은 호출 측 책임), crontab 생성기가 enabled 로 거른다.

    Args:
        session: ORM 세션.
        job_kind: 특정 종류만 조회하려면 지정. None 이면 전체.

    Returns:
        저장 id 순서를 보존한 :class:`ScheduledJobRecord` 리스트.
    """
    statement = select(ScheduledJob)
    if job_kind is not None:
        statement = statement.where(ScheduledJob.job_kind == job_kind)
    statement = statement.order_by(ScheduledJob.id)
    return [_to_record(model) for model in session.scalars(statement).all()]


def list_general_schedules(session: Session) -> list[ScheduledJobRecord]:
    """일반 공고 수집(``scrape_general``) 스케줄만 모아 반환한다.

    admin 스케줄 페이지가 표시하는 N건 목록의 read API 다(백업/Daily Report/GC
    싱글턴은 제외).

    Args:
        session: ORM 세션.

    Returns:
        scrape_general :class:`ScheduledJobRecord` 리스트.
    """
    return list_scheduled_jobs(session, job_kind=JOB_KIND_SCRAPE_GENERAL)


def get_scheduled_job(
    session: Session, job_id: int
) -> ScheduledJobRecord | None:
    """id 로 단일 스케줄을 조회한다.

    Args:
        session: ORM 세션.
        job_id: 조회할 스케줄 id.

    Returns:
        해당 레코드. 없으면 None.
    """
    model = session.get(ScheduledJob, job_id)
    return _to_record(model) if model is not None else None


def get_singleton_schedule(
    session: Session, job_kind: str
) -> ScheduledJobRecord | None:
    """싱글턴 잡(backup/daily_report/gc)의 단일 row 를 조회한다.

    동일 종류 row 가 여러 개라면 id 가 가장 작은(가장 먼저 시드된) 1건을 반환한다.
    싱글턴 불변식은 시드/upsert 가 보장하지만, 방어적으로 첫 row 만 노출한다.

    Args:
        session: ORM 세션.
        job_kind: 싱글턴 종류(:data:`~app.scheduler.constants.SINGLETON_JOB_KINDS`).

    Returns:
        해당 싱글턴 레코드. 없으면 None.
    """
    rows = list_scheduled_jobs(session, job_kind=job_kind)
    return rows[0] if rows else None


# ──────────────────────────────────────────────────────────────
# 쓰기 API — 일반 수집(N건)
# ──────────────────────────────────────────────────────────────


def add_general_schedule(
    session: Session,
    *,
    trigger_type: str,
    cron_expression: str | None = None,
    interval_hours: int | None = None,
    active_sources: list[str] | None = None,
    enabled: bool = True,
) -> ScheduledJobRecord:
    """일반 공고 수집(``scrape_general``) 스케줄 1건을 신설해 저장한다.

    cron 트리거는 ``cron_expression``, interval 트리거는 ``interval_hours`` 가
    필요하며 입력은 즉시 검증한다.

    Args:
        session: ORM 세션.
        trigger_type: :data:`~app.scheduler.constants.TRIGGER_TYPE_CRON` 또는
            :data:`~app.scheduler.constants.TRIGGER_TYPE_INTERVAL`.
        cron_expression: cron 트리거일 때의 표준 5-필드 표현식.
        interval_hours: interval 트리거일 때의 '매 N시간' 정수.
        active_sources: 수집할 source id 목록. None/빈 리스트면 전체.
        enabled: 활성 여부. 기본 True.

    Returns:
        저장된 :class:`ScheduledJobRecord` (id 부여 완료).

    Raises:
        ScheduledJobConfigError: trigger_type 가 부정확하거나 트리거별 필수 입력이
            누락/범위 위반인 경우.
    """
    normalized_sources = _normalize_active_sources(active_sources)

    if trigger_type == TRIGGER_TYPE_CRON:
        validated_expression = _validate_cron_expression(cron_expression)
        model = ScheduledJob(
            job_kind=JOB_KIND_SCRAPE_GENERAL,
            trigger_type=TRIGGER_TYPE_CRON,
            cron_expression=validated_expression,
            interval_hours=None,
            active_sources=normalized_sources,
            enabled=enabled,
        )
    elif trigger_type == TRIGGER_TYPE_INTERVAL:
        validated_hours = _validate_interval_hours(interval_hours)
        model = ScheduledJob(
            job_kind=JOB_KIND_SCRAPE_GENERAL,
            trigger_type=TRIGGER_TYPE_INTERVAL,
            cron_expression=None,
            interval_hours=validated_hours,
            active_sources=normalized_sources,
            enabled=enabled,
        )
    else:
        raise ScheduledJobConfigError(
            f"알 수 없는 trigger_type: {trigger_type!r} "
            f"({TRIGGER_TYPE_CRON!r} 또는 {TRIGGER_TYPE_INTERVAL!r})."
        )

    session.add(model)
    session.flush()  # 대리 키 id 를 즉시 확보한다.
    return _to_record(model)


def set_scheduled_job_enabled(
    session: Session, job_id: int, *, enabled: bool
) -> ScheduledJobRecord:
    """스케줄의 활성/비활성 상태를 토글한다.

    Args:
        session: ORM 세션.
        job_id: 토글할 스케줄 id.
        enabled: 설정할 활성 상태.

    Returns:
        갱신된 :class:`ScheduledJobRecord`.

    Raises:
        ScheduledJobConfigError: 해당 id 의 스케줄이 없는 경우.
    """
    model = session.get(ScheduledJob, job_id)
    if model is None:
        raise ScheduledJobConfigError(
            f"토글할 스케줄을 찾을 수 없습니다: id={job_id!r}."
        )
    model.enabled = enabled
    session.flush()
    return _to_record(model)


def delete_scheduled_job(session: Session, job_id: int) -> bool:
    """id 로 스케줄을 삭제한다.

    Args:
        session: ORM 세션.
        job_id: 삭제할 스케줄 id.

    Returns:
        실제로 삭제됐으면 True, 해당 id 가 없으면 False.
    """
    model = session.get(ScheduledJob, job_id)
    if model is None:
        return False
    session.delete(model)
    session.flush()
    return True


# ──────────────────────────────────────────────────────────────
# 쓰기 API — 싱글턴(backup / daily_report / gc)
# ──────────────────────────────────────────────────────────────


def upsert_singleton_schedule(
    session: Session,
    *,
    job_kind: str,
    cron_expression: str | None = None,
    interval_hours: int | None = None,
    enabled: bool | None = None,
) -> ScheduledJobRecord:
    """싱글턴 잡(backup/daily_report/gc)의 트리거/활성 상태를 갱신(없으면 생성)한다.

    싱글턴은 종류당 1건만 존재하므로, 기존 row 가 있으면 in-place 갱신하고 없으면
    새로 만든다. ``cron_expression`` / ``interval_hours`` / ``enabled`` 는 모두
    선택적이며, None 이면 기존 값을 유지한다(부분 갱신).

    Args:
        session: ORM 세션.
        job_kind: 싱글턴 종류(:data:`~app.scheduler.constants.SINGLETON_JOB_KINDS`).
        cron_expression: 변경할 cron 표현식. 지정 시 cron 트리거로 전환·검증한다.
        interval_hours: 변경할 '매 N시간'. 지정 시 interval 트리거로 전환·검증한다.
        enabled: 변경할 활성 상태. None 이면 기존 값 유지.

    Returns:
        갱신/생성된 :class:`ScheduledJobRecord`.

    Raises:
        ScheduledJobConfigError: job_kind 가 싱글턴이 아니거나, cron/interval 을 동시
            지정했거나, 신규 생성인데 트리거 정보가 전혀 없거나, 입력이 범위를
            위반한 경우.
    """
    if job_kind not in SINGLETON_JOB_KINDS:
        raise ScheduledJobConfigError(
            f"싱글턴 잡이 아닙니다: {job_kind!r} "
            f"(허용: {', '.join(SINGLETON_JOB_KINDS)})."
        )
    if cron_expression is not None and interval_hours is not None:
        raise ScheduledJobConfigError(
            "cron_expression 과 interval_hours 는 동시에 지정할 수 없습니다."
        )

    model = (
        session.scalars(
            select(ScheduledJob)
            .where(ScheduledJob.job_kind == job_kind)
            .order_by(ScheduledJob.id)
        ).first()
    )

    # 트리거 변경분 계산(지정된 쪽만 검증·반영, 둘 다 None 이면 기존 유지).
    new_trigger_type: str | None = None
    new_cron: str | None = None
    new_interval: int | None = None
    if cron_expression is not None:
        new_trigger_type = TRIGGER_TYPE_CRON
        new_cron = _validate_cron_expression(cron_expression)
    elif interval_hours is not None:
        new_trigger_type = TRIGGER_TYPE_INTERVAL
        new_interval = _validate_interval_hours(interval_hours)

    if model is None:
        # 신규 생성 — 트리거 정보가 반드시 있어야 한다.
        if new_trigger_type is None:
            raise ScheduledJobConfigError(
                f"싱글턴 {job_kind!r} 신규 생성에는 cron_expression 또는 "
                "interval_hours 가 필요합니다."
            )
        model = ScheduledJob(
            job_kind=job_kind,
            trigger_type=new_trigger_type,
            cron_expression=new_cron,
            interval_hours=new_interval,
            active_sources=None,
            enabled=True if enabled is None else enabled,
        )
        session.add(model)
    else:
        if new_trigger_type is not None:
            model.trigger_type = new_trigger_type
            model.cron_expression = new_cron
            model.interval_hours = new_interval
        if enabled is not None:
            model.enabled = enabled

    session.flush()
    return _to_record(model)


# 싱글턴 잡의 기본 시드 정의(존재하지 않을 때만 생성). cron 표현식 기본값은 각
# 도메인 상수와 동일 값을 사용한다. backup/daily_report 의 비-스케줄 설정은
# system_settings 에 그대로 남고, 여기서는 '언제 실행할지'(트리거)만 시드한다.
_SINGLETON_SEED_DEFINITIONS: tuple[tuple[str, str, bool], ...] = (
    # (job_kind, 기본 cron, 기본 enabled)
    (JOB_KIND_BACKUP, "0 3 * * *", True),
    (JOB_KIND_DAILY_REPORT, "0 9 * * 1-5", False),
    (JOB_KIND_GC, DEFAULT_GC_ORPHAN_CRON, True),
)


def ensure_default_seed_jobs(session: Session) -> int:
    """싱글턴 잡(backup/daily_report/gc)이 없으면 기본값으로 1건씩 시드한다(멱등).

    신규 설치(빈 DB)나 운영자가 실수로 싱글턴 row 를 지운 경우에도 기동 시 crontab
    이 완전 복원되도록 기본 트리거를 보장한다. 이미 존재하는 종류는 건드리지 않으므로
    여러 번 호출해도 중복이 생기지 않는다.

    Args:
        session: ORM 세션.

    Returns:
        새로 시드한 row 수.
    """
    inserted = 0
    for job_kind, default_cron, default_enabled in _SINGLETON_SEED_DEFINITIONS:
        existing = session.scalars(
            select(ScheduledJob.id).where(ScheduledJob.job_kind == job_kind)
        ).first()
        if existing is not None:
            continue
        session.add(
            ScheduledJob(
                job_kind=job_kind,
                trigger_type=TRIGGER_TYPE_CRON,
                cron_expression=default_cron,
                interval_hours=None,
                active_sources=None,
                enabled=default_enabled,
                created_at=now_utc(),
                updated_at=now_utc(),
            )
        )
        inserted += 1
    if inserted:
        session.flush()
    return inserted


__all__ = [
    "ScheduledJobConfigError",
    "ScheduledJobRecord",
    "add_general_schedule",
    "delete_scheduled_job",
    "ensure_default_seed_jobs",
    "get_scheduled_job",
    "get_singleton_schedule",
    "list_general_schedules",
    "list_scheduled_jobs",
    "set_scheduled_job_enabled",
    "upsert_singleton_schedule",
]
