"""일반 공고 수집 스케줄의 영속 저장소 (task 00155-2).

배경
----
기존에는 ``/admin/schedule`` 의 일반 공고 수집 스케줄(cron/interval 표현식 +
active_sources)이 APScheduler jobstore(``scheduler_jobs`` 테이블)에만 존재했다.
즉 잡 직렬화 안에만 묻혀 있어, 컨테이너 기동 시 스케줄을 읽어 OS crontab 으로
렌더하려는 cron 전환(task 00155) 입장에서는 **외부에서 읽을 source of truth 가
없었다**.

이 모듈은 그 일반 수집 스케줄을 DB 에 영속화하는 단일 진실 저장소다. Alembic
신규 테이블을 만들지 않기 위해 :class:`app.db.models.SystemSetting` 의 단일 키
(:data:`SETTING_KEY_GENERAL_SCHEDULES`)에 **JSON 리스트** 형태로 저장한다.

저장 레코드 1건은 다음 필드를 가진다::

    id              : 스케줄 식별자(uuid hex). add 시 자동 생성.
    mode            : "cron" 또는 "interval".
    cron_expression : mode == "cron" 일 때의 표준 5-필드 crontab 표현식.
                      interval 모드면 None.
    interval_hours  : mode == "interval" 일 때의 '매 N시간' 정수.
                      cron 모드면 None.
    active_sources  : 트리거 시 수집할 source id 목록(빈 리스트 = 전체 enabled).
    enabled         : 활성 여부. False 면 crontab 라인에서 제외된다.

이 저장소의 read API(:func:`list_general_schedule_records`)는 00155-3 의 crontab
생성기와 00155-4 의 admin 라우트 재배선이 함께 공유한다. 쓰기 API(add/delete/
toggle)는 00155-4 가 admin 라우트에서 호출한다.

설계 메모
---------
- 모든 함수는 호출자가 넘긴 :class:`sqlalchemy.orm.Session` 위에서 동작하며
  ``commit`` 하지 않는다. 트랜잭션 경계는 호출 측(``session_scope``)이 관리한다
  (``app.backup.service.get_setting/set_setting`` 의 기존 계약과 동일).
- APScheduler 에 **의존하지 않는다**. 후속 subtask 가 APScheduler 를 완전히
  제거해도 이 모듈은 그대로 동작한다.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import SystemSetting
from app.scheduler.constants import (
    MAX_INTERVAL_HOURS,
    SETTING_KEY_GENERAL_SCHEDULES,
)
from app.timezone import now_utc

# 스케줄 모드 식별 문자열. crontab 생성기·admin 라우트와 공유한다.
SCHEDULE_MODE_CRON: str = "cron"
SCHEDULE_MODE_INTERVAL: str = "interval"

# 표준 crontab 표현식의 필드 개수(분 시 일 월 요일).
_CRON_FIELD_COUNT: int = 5


class ScheduleConfigError(ValueError):
    """일반 수집 스케줄 입력값이 올바르지 않을 때 발생한다.

    cron 표현식의 필드 개수 오류, interval 범위 위반 등 사용자 입력 검증 실패에
    사용한다. UI 에서 flash 메시지로 노출하기 좋도록 ``ValueError`` 를 상속한다.
    """


@dataclass
class GeneralScheduleRecord:
    """일반 공고 수집 스케줄 1건을 표현하는 영속 레코드.

    Attributes:
        id:              스케줄 식별자(uuid hex). 신규 생성 시 자동 부여.
        mode:            :data:`SCHEDULE_MODE_CRON` 또는
                         :data:`SCHEDULE_MODE_INTERVAL`.
        cron_expression: cron 모드의 표준 5-필드 표현식. interval 모드면 None.
        interval_hours:  interval 모드의 '매 N시간' 정수. cron 모드면 None.
        active_sources:  트리거 시 수집할 source id 목록. 빈 리스트면 전체.
        enabled:         활성 여부. False 면 crontab 라인에서 제외.
    """

    id: str
    mode: str
    cron_expression: str | None = None
    interval_hours: int | None = None
    active_sources: list[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화 가능한 dict 로 변환한다.

        Returns:
            SystemSetting 의 JSON 리스트에 저장할 수 있는 순수 dict.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GeneralScheduleRecord":
        """저장된 JSON dict 1건을 레코드로 역직렬화한다.

        과거 버전이 일부 필드를 누락했더라도 안전하도록 ``get`` 으로 방어한다.

        Args:
            raw: SystemSetting JSON 리스트의 원소 dict.

        Returns:
            역직렬화된 :class:`GeneralScheduleRecord`.
        """
        active_sources = raw.get("active_sources") or []
        return cls(
            id=str(raw.get("id", "")),
            mode=str(raw.get("mode", SCHEDULE_MODE_CRON)),
            cron_expression=raw.get("cron_expression"),
            interval_hours=raw.get("interval_hours"),
            active_sources=[str(source) for source in active_sources],
            enabled=bool(raw.get("enabled", True)),
        )


def _normalize_active_sources(active_sources: list[str] | None) -> list[str]:
    """active_sources 입력을 공백 제거·빈 토큰 제거한 리스트로 정규화한다.

    Args:
        active_sources: 원본 source id 목록(None 허용).

    Returns:
        앞뒤 공백이 제거되고 빈 문자열이 걸러진 source id 리스트. None 이면
        빈 리스트(= 전체 수집).
    """
    if not active_sources:
        return []
    return [source.strip() for source in active_sources if source.strip()]


def _validate_cron_expression(cron_expression: str) -> str:
    """cron 모드 표현식의 필드 개수를 검증하고 정규화(공백 정리)한다.

    system cron 규약을 그대로 따르므로 요일 보정(build_cron_trigger)은 적용하지
    않는다. 필드 개수(5)만 검증하고, 필드 사이 중복 공백은 단일 공백으로 정리해
    crontab 출력이 일관되게 한다.

    Args:
        cron_expression: 사용자 입력 cron 표현식.

    Returns:
        공백이 단정하게 정리된 5-필드 표현식.

    Raises:
        ScheduleConfigError: 표현식이 비었거나 필드 개수가 5가 아닌 경우.
    """
    fields = cron_expression.split()
    if len(fields) != _CRON_FIELD_COUNT:
        raise ScheduleConfigError(
            f"cron 표현식은 5개 필드(분 시 일 월 요일)여야 합니다 "
            f"(입력: {cron_expression!r})."
        )
    return " ".join(fields)


def _validate_interval_hours(interval_hours: int) -> int:
    """interval 모드의 '매 N시간' 값이 1~MAX_INTERVAL_HOURS 범위인지 검증한다.

    Args:
        interval_hours: '매 N시간' 정수.

    Returns:
        검증을 통과한 정수.

    Raises:
        ScheduleConfigError: 양의 정수가 아니거나 :data:`MAX_INTERVAL_HOURS` 를
            초과한 경우.
    """
    if not isinstance(interval_hours, int) or interval_hours <= 0:
        raise ScheduleConfigError(
            f"interval_hours 는 양의 정수여야 합니다 (입력: {interval_hours!r})."
        )
    if interval_hours > MAX_INTERVAL_HOURS:
        raise ScheduleConfigError(
            f"interval 은 최대 {MAX_INTERVAL_HOURS}시간까지입니다. 그 이상은 "
            f"cron 표현식을 사용하세요 (입력: {interval_hours}시간)."
        )
    return interval_hours


# ──────────────────────────────────────────────────────────────
# 저장/조회 헬퍼 — SystemSetting JSON 리스트 한 칸을 읽고 쓴다.
# ──────────────────────────────────────────────────────────────


def _load_raw_records(session: Session) -> list[dict[str, Any]]:
    """SystemSetting 에서 일반 수집 스케줄 JSON 리스트를 로드한다.

    값이 없거나(키 미존재) JSON 파싱에 실패하면 빈 리스트로 취급한다. 손상된
    값으로 인해 crontab 생성·admin 라우트가 통째로 죽지 않도록 방어적이다.

    Args:
        session: ORM 세션.

    Returns:
        저장된 원본 dict 리스트. 없거나 손상되면 빈 리스트.
    """
    row = session.get(SystemSetting, SETTING_KEY_GENERAL_SCHEDULES)
    if row is None or row.value in (None, ""):
        return []
    try:
        parsed = json.loads(row.value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _save_records(session: Session, records: list[GeneralScheduleRecord]) -> None:
    """레코드 리스트를 SystemSetting JSON 으로 직렬화해 upsert 한다.

    ``commit`` 하지 않는다 — 트랜잭션 경계는 호출 측(``session_scope``)이
    관리한다. 단, 세션 팩토리가 ``autoflush=False`` 이므로, 한 트랜잭션에서
    add→read 또는 add→add 가 연달아 일어날 때(예: admin 이 스케줄을 추가하고
    곧바로 crontab 생성기가 다시 읽는 경우) 직전 INSERT 가 식별 맵에 잡히도록
    저장 직후 ``flush`` 한다. flush 는 트랜잭션을 닫지 않으므로 commit 계약과
    충돌하지 않는다.

    Args:
        session: ORM 세션.
        records: 저장할 스케줄 레코드 전체.
    """
    serialized = json.dumps(
        [record.to_dict() for record in records],
        ensure_ascii=False,
    )
    row = session.get(SystemSetting, SETTING_KEY_GENERAL_SCHEDULES)
    if row is None:
        session.add(
            SystemSetting(
                key=SETTING_KEY_GENERAL_SCHEDULES,
                value=serialized,
                updated_at=now_utc(),
            )
        )
    else:
        row.value = serialized
        row.updated_at = now_utc()
    session.flush()


# ──────────────────────────────────────────────────────────────
# 공개 CRUD API
# ──────────────────────────────────────────────────────────────


def list_general_schedule_records(session: Session) -> list[GeneralScheduleRecord]:
    """저장된 일반 수집 스케줄 전체를 레코드 리스트로 반환한다.

    crontab 생성기(00155-3)와 admin 라우트(00155-4)가 공유하는 read API 다.

    Args:
        session: ORM 세션.

    Returns:
        저장 순서를 보존한 :class:`GeneralScheduleRecord` 리스트. 비활성 항목도
        포함하므로(필터링은 호출 측 책임), crontab 생성기가 enabled 로 거른다.
    """
    return [GeneralScheduleRecord.from_dict(raw) for raw in _load_raw_records(session)]


def get_general_schedule_record(
    session: Session, schedule_id: str
) -> GeneralScheduleRecord | None:
    """id 로 단일 스케줄 레코드를 조회한다.

    Args:
        session: ORM 세션.
        schedule_id: 조회할 스케줄 id.

    Returns:
        해당 레코드. 없으면 None.
    """
    for record in list_general_schedule_records(session):
        if record.id == schedule_id:
            return record
    return None


def add_general_schedule_record(
    session: Session,
    *,
    mode: str,
    active_sources: list[str] | None = None,
    cron_expression: str | None = None,
    interval_hours: int | None = None,
    enabled: bool = True,
) -> GeneralScheduleRecord:
    """일반 수집 스케줄 1건을 신설해 저장한다.

    id 는 uuid hex 로 자동 생성한다. cron 모드는 ``cron_expression``, interval
    모드는 ``interval_hours`` 가 필요하며, 입력은 즉시 검증한다.

    Args:
        session: ORM 세션.
        mode: :data:`SCHEDULE_MODE_CRON` 또는 :data:`SCHEDULE_MODE_INTERVAL`.
        active_sources: 수집할 source id 목록. None/빈 리스트면 전체.
        cron_expression: cron 모드일 때의 표준 5-필드 표현식.
        interval_hours: interval 모드일 때의 '매 N시간' 정수.
        enabled: 활성 여부. 기본 True.

    Returns:
        저장된 :class:`GeneralScheduleRecord` (id 부여 완료).

    Raises:
        ScheduleConfigError: mode 가 부정확하거나 모드별 필수 입력이 누락/범위
            위반인 경우.
    """
    normalized_sources = _normalize_active_sources(active_sources)

    if mode == SCHEDULE_MODE_CRON:
        if not cron_expression:
            raise ScheduleConfigError("cron 모드는 cron_expression 이 필요합니다.")
        validated_expression = _validate_cron_expression(cron_expression)
        record = GeneralScheduleRecord(
            id=uuid.uuid4().hex,
            mode=SCHEDULE_MODE_CRON,
            cron_expression=validated_expression,
            interval_hours=None,
            active_sources=normalized_sources,
            enabled=enabled,
        )
    elif mode == SCHEDULE_MODE_INTERVAL:
        if interval_hours is None:
            raise ScheduleConfigError(
                "interval 모드는 interval_hours 가 필요합니다."
            )
        validated_hours = _validate_interval_hours(interval_hours)
        record = GeneralScheduleRecord(
            id=uuid.uuid4().hex,
            mode=SCHEDULE_MODE_INTERVAL,
            cron_expression=None,
            interval_hours=validated_hours,
            active_sources=normalized_sources,
            enabled=enabled,
        )
    else:
        raise ScheduleConfigError(
            f"알 수 없는 스케줄 모드: {mode!r} "
            f"({SCHEDULE_MODE_CRON!r} 또는 {SCHEDULE_MODE_INTERVAL!r})."
        )

    records = list_general_schedule_records(session)
    records.append(record)
    _save_records(session, records)
    return record


def delete_general_schedule_record(session: Session, schedule_id: str) -> bool:
    """id 로 스케줄 레코드를 삭제한다.

    Args:
        session: ORM 세션.
        schedule_id: 삭제할 스케줄 id.

    Returns:
        실제로 삭제됐으면 True, 해당 id 가 없으면 False.
    """
    records = list_general_schedule_records(session)
    remaining = [record for record in records if record.id != schedule_id]
    if len(remaining) == len(records):
        return False
    _save_records(session, remaining)
    return True


def set_general_schedule_enabled(
    session: Session, schedule_id: str, *, enabled: bool
) -> GeneralScheduleRecord:
    """스케줄의 활성/비활성 상태를 토글한다.

    Args:
        session: ORM 세션.
        schedule_id: 토글할 스케줄 id.
        enabled: 설정할 활성 상태.

    Returns:
        갱신된 :class:`GeneralScheduleRecord`.

    Raises:
        ScheduleConfigError: 해당 id 의 스케줄이 없는 경우.
    """
    records = list_general_schedule_records(session)
    target: GeneralScheduleRecord | None = None
    for record in records:
        if record.id == schedule_id:
            record.enabled = enabled
            target = record
            break
    if target is None:
        raise ScheduleConfigError(
            f"토글할 스케줄을 찾을 수 없습니다: id={schedule_id!r}."
        )
    _save_records(session, records)
    return target


__all__ = [
    "GeneralScheduleRecord",
    "SCHEDULE_MODE_CRON",
    "SCHEDULE_MODE_INTERVAL",
    "ScheduleConfigError",
    "add_general_schedule_record",
    "delete_general_schedule_record",
    "get_general_schedule_record",
    "list_general_schedule_records",
    "set_general_schedule_enabled",
]
