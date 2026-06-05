"""모든 스케줄을 단일하게 읽어 system crontab 텍스트로 렌더링하는 순수 생성기
(task 00155-2).

목적
----
APScheduler(웹 프로세스 내부 SW 스케줄러)를 걷어내고, 컨테이너 기동 시
스케줄 설정을 읽어 **실제 OS crontab** 에 등록하는 cron 전환(task 00155)의
핵심 부품이다. 활성화된 모든 스케줄을 모아 cron 데몬이 그대로 설치할 수 있는
crontab 텍스트(헤더 + 잡 라인들)를 만든다.

cron 데몬은 각 잡을 매 주기 **독립 프로세스**로 새로 띄우므로, 한 잡의 실패가
다음 주기나 다른 잡으로 전파되지 않는다. 이로써 error.log 18b0d4249dbf 의
'database is locked → APScheduler 스레드 사망 → 전 스케줄 정지' 라는
single-point-of-failure 가 구조적으로 사라진다.

대상 스케줄 (task 00157 — 단일 SSOT 전환)
------------------------------------------
task 00157 부터 모든 스케줄 트리거는 단일 관계형 테이블 ``scheduled_jobs``
(:mod:`app.scheduler.scheduled_job_store`)에서만 읽는다. 더 이상 system_settings
의 여러 키(``scheduler.general_schedules`` / ``backup.cron_expression`` /
``email.daily_report.*``)를 참조하지 않는다 — 기동 시 이 단일 테이블만 보고
crontab 을 완전 복원하므로 외부 cron 파일을 별도로 들고 다닐 필요가 없다.

1. 일반 공고 수집(N건): ``scheduled_jobs`` 의 ``scrape_general`` row 들.
   cron / interval('매 N시간') 트리거 모두 지원.
2. DB 파일 백업(1건): ``scheduled_jobs`` 의 ``backup`` 싱글턴 row.
3. Daily Report 발송(1건): ``scheduled_jobs`` 의 ``daily_report`` 싱글턴 row
   (해당 row 의 ``enabled`` 가 True 일 때만 포함).
4. 고아 첨부 파일 GC(1건): ``scheduled_jobs`` 의 ``gc`` 싱글턴 row.

메일/백업의 **비-스케줄 설정**(SMTP 자격증명·``backup.max_count``·Daily Report
수신자 등)은 여전히 system_settings 에 남아 있고, 본 생성기는 그것을 읽지 않는다.

각 잡 라인은 :mod:`app.scheduler.run_job` CLI(00155-1)를 호출한다.

핵심 설계 결정 (guidance)
-------------------------
- **요일 보정 금지**: system cron 은 표준 crontab 요일(0=일·1=월…, 1-5=월~금)을
  그대로 해석한다. APScheduler 시절의 요일 보정
  (:func:`app.scheduler.cron.build_cron_trigger`)을 여기에 재적용하면 안 된다.
  저장된 5-필드 cron 표현식을 **있는 그대로** 출력한다.
- **interval → cron**: '매 N시간' 은 ``0 */N * * *`` 로 변환한다
  (1 ≤ N ≤ :data:`MAX_INTERVAL_HOURS`).
- **cron 환경 비움 대비**: cron 은 환경변수를 거의 비운 채 잡을 띄우므로, 각 잡
  라인은 프로젝트 디렉터리로 ``cd`` 한 뒤 ``.env`` 를 source 해
  HOST_PROJECT_DIR 등 필수 환경을 로딩하고 나서 CLI 를 호출한다. stdout/stderr
  는 ``data/logs`` 하위 로그로 redirect 한다.
- **순수성**: 본 모듈의 렌더 함수들은 컨테이너·cron·실시간 시계에 의존하지
  않는 순수 함수다. 단위 테스트가 crontab 문자열을 직접 검증할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.scheduler.constants import (
    MAX_INTERVAL_HOURS,
    TRIGGER_TYPE_INTERVAL,
)

if TYPE_CHECKING:
    # 타입 힌트 전용 import. 런타임에는 :mod:`app.scheduler.scheduled_job_store` 가
    # 본 모듈(validate_cron_expression)에 의존하므로, 모듈 최상위에서 store 를
    # import 하면 순환 import 가 된다. 소비 함수 안에서 lazy import 한다.
    from app.scheduler.scheduled_job_store import ScheduledJobRecord

# cron 데몬이 호출하는 작업 CLI 모듈 경로(00155-1). 잡 라인은 이 모듈을
# ``python -m`` 으로 실행한다.
RUN_JOB_MODULE: str = "app.scheduler.run_job"

# 컨테이너 TZ 기본값. crontab 상단에 ``CRON_TZ`` 로 명시해 cron 이 KST 로
# 스케줄을 해석하게 한다(컨테이너 /etc/localtime 이 Asia/Seoul 이어도 명시).
DEFAULT_CRON_TIMEZONE: str = "Asia/Seoul"

# 각 잡 종류별 로그 파일명. data/logs 하위에 redirect 된다.
LOG_FILENAME_SCRAPE: str = "cron-scrape.log"
LOG_FILENAME_BACKUP: str = "cron-backup.log"
LOG_FILENAME_DAILY_REPORT: str = "cron-daily-report.log"
LOG_FILENAME_GC: str = "cron-gc.log"

# 자동 생성 표식 — 운영자가 손으로 편집하면 다음 기동 때 덮어쓰인다는 경고.
_CRONTAB_HEADER_BANNER: str = (
    "# 이 crontab 은 app.scheduler.crontab_generator 가 자동 생성합니다.\n"
    "# 직접 편집하지 마세요 — 컨테이너 재기동/스케줄 변경 시 덮어쓰입니다."
)


@dataclass(frozen=True)
class CrontabEnvironment:
    """crontab 잡 라인이 cron 의 빈 환경에서 동작하기 위한 실행 컨텍스트.

    순수 생성기가 컨테이너 사정을 모른 채로도 동작하도록, 경로·실행기 등
    런타임 의존 값을 명시적으로 주입받는다. 컨테이너 기동 시(00155-3)는 실제
    값을, 단위 테스트는 고정 값을 넣어 출력 문자열을 직접 검증한다.

    Attributes:
        project_dir:       프로젝트 루트 절대경로. 잡 라인이 이 경로로 ``cd``
                           한 뒤 ``.env`` 를 source 한다(상대경로 해석 기준).
        python_executable: python 실행기 절대경로. cron 의 PATH 는 빈약하므로
                           절대경로를 쓴다(예: /usr/local/bin/python).
        log_dir:           잡 stdout/stderr 를 redirect 할 디렉터리 절대경로.
        env_file:          source 할 환경 파일. project_dir 기준 상대경로 또는
                           절대경로. 기본 ``.env``.
        timezone:          crontab 상단 ``CRON_TZ`` 값. 기본 Asia/Seoul.
        extra_environment: crontab 상단에 추가로 박을 ``KEY=VALUE`` 환경(예:
                           docker 바이너리를 찾기 위한 PATH). cron 환경 비움
                           대비로 00155-3 이 주입한다.
    """

    project_dir: str
    python_executable: str
    log_dir: str
    env_file: str = ".env"
    timezone: str = DEFAULT_CRON_TIMEZONE
    extra_environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CrontabJob:
    """crontab 한 줄로 렌더될 정규화된 잡 1건.

    일반 수집/백업/Daily Report/GC 가 모두 이 공통 형태로 환원된 뒤 동일한
    렌더러를 거친다.

    Attributes:
        comment:         잡 식별용 주석(예: "[공고 수집] cron sources=iris,ntis").
        cron_expression: 표준 5-필드 표현식. 보정 없이 그대로 출력된다.
        cli_arguments:   ``python -m app.scheduler.run_job`` 뒤에 붙을 인자
                         토큰들(예: ["scrape", "--sources", "iris,ntis"]).
        log_filename:    stdout/stderr 를 redirect 할 로그 파일명.
    """

    comment: str
    cron_expression: str
    cli_arguments: list[str]
    log_filename: str


class CronExpressionError(ValueError):
    """표준 5-필드 cron 표현식이 crontab 규약에 맞지 않을 때 발생한다.

    task 00155-4 — APScheduler 의 ``build_cron_trigger`` (요일 보정 + 파싱) 를
    제거하면서, admin 라우트(스케줄/백업/Daily Report)가 cron 표현식을 검증할
    단일 진실 함수가 필요해졌다. system cron 은 표준 crontab 규약(0=일·1=월,
    1-5=월~금)을 그대로 해석하므로, 요일 보정 없이 필드별 범위만 검증한다.

    ``ValueError`` 를 상속해 admin 라우트가 flash 메시지/422 로 변환하기 쉽다.
    """


# 각 cron 필드의 (사람-친화 라벨, 최소값, 최대값, 허용 별칭 맵).
# 요일 별칭은 sun=0..sat=6 으로 매핑하되, 숫자 범위는 0~7 을 허용해 7 도
# 일요일로 받아들인다(표준 crontab 규약). 월 별칭은 jan=1..dec=12.
_CRON_MONTH_ALIASES: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_CRON_DAY_OF_WEEK_ALIASES: dict[str, int] = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_CRON_FIELD_SPECS: tuple[tuple[str, int, int, dict[str, int] | None], ...] = (
    ("분", 0, 59, None),
    ("시", 0, 23, None),
    ("일", 1, 31, None),
    ("월", 1, 12, _CRON_MONTH_ALIASES),
    ("요일", 0, 7, _CRON_DAY_OF_WEEK_ALIASES),
)


def _resolve_cron_value(
    token: str,
    *,
    label: str,
    low: int,
    high: int,
    aliases: dict[str, int] | None,
) -> int:
    """단일 cron 값 토큰(숫자 또는 별칭)을 정수로 해석하고 범위를 검증한다.

    Args:
        token:   해석할 단일 값(예: ``"5"``, ``"mon"``).
        label:   에러 메시지용 필드 라벨(예: ``"요일"``).
        low:     이 필드가 허용하는 최소 정수.
        high:    이 필드가 허용하는 최대 정수.
        aliases: 허용 별칭 맵(월/요일). 없으면 None.

    Returns:
        검증을 통과한 정수 값.

    Raises:
        CronExpressionError: 별칭이 아니고 정수도 아니거나 범위를 벗어난 경우.
    """
    normalized = token.strip()
    if aliases is not None and normalized.lower() in aliases:
        return aliases[normalized.lower()]
    try:
        number = int(normalized)
    except ValueError as exc:
        raise CronExpressionError(
            f"{label} 필드 값이 올바르지 않습니다: {token!r}."
        ) from exc
    if not (low <= number <= high):
        raise CronExpressionError(
            f"{label} 필드 값 {number} 가 허용 범위({low}~{high})를 벗어났습니다."
        )
    return number


def _validate_cron_element(
    element: str,
    *,
    label: str,
    low: int,
    high: int,
    aliases: dict[str, int] | None,
) -> None:
    """콤마 리스트의 단일 항목(``*``/범위/스텝/단일 값)을 검증한다.

    허용 형태: ``*``, ``*/step``, ``a-b``, ``a-b/step``, ``a`` (단일 값/별칭).

    Raises:
        CronExpressionError: 스텝이 양의 정수가 아니거나, 범위 시작>끝, 또는
            값이 필드 범위를 벗어난 경우.
    """
    base = element
    if "/" in element:
        base, _, step_text = element.partition("/")
        if not step_text.isdigit() or int(step_text) == 0:
            raise CronExpressionError(
                f"{label} 필드의 스텝 값이 올바르지 않습니다: {element!r} "
                "(스텝은 양의 정수여야 합니다)."
            )

    if base == "*":
        return

    if "-" in base:
        start_text, _, end_text = base.partition("-")
        start_value = _resolve_cron_value(
            start_text, label=label, low=low, high=high, aliases=aliases
        )
        end_value = _resolve_cron_value(
            end_text, label=label, low=low, high=high, aliases=aliases
        )
        if start_value > end_value:
            raise CronExpressionError(
                f"{label} 범위의 시작({start_value})이 끝({end_value})보다 큽니다."
            )
        return

    _resolve_cron_value(base, label=label, low=low, high=high, aliases=aliases)


def validate_cron_expression(cron_expression: str) -> str:
    """표준 5-필드 system crontab 표현식을 검증하고 정규화(공백 정리)한다.

    APScheduler 의 ``build_cron_trigger`` 를 대체하는 순수 검증 함수다. 요일
    보정은 적용하지 않고(system cron 규약 그대로), 필드 개수(5)와 각 필드의
    값 범위만 검증한다. 콤마 리스트·범위(``a-b``)·스텝(``*/n``, ``a-b/n``)·월/요일
    별칭(JAN/MON 등)을 지원한다.

    Args:
        cron_expression: 검증할 cron 표현식.

    Returns:
        필드 사이 중복 공백이 단일 공백으로 정리된 5-필드 표현식.

    Raises:
        CronExpressionError: 필드 개수가 5가 아니거나, 어떤 필드의 값/범위/스텝이
            crontab 규약을 위반한 경우.
    """
    fields = cron_expression.split()
    if len(fields) != 5:
        raise CronExpressionError(
            f"cron 표현식은 5개 필드(분 시 일 월 요일)여야 합니다 "
            f"(입력: {cron_expression!r})."
        )

    for value, (label, low, high, aliases) in zip(fields, _CRON_FIELD_SPECS):
        for element in value.split(","):
            if element == "":
                raise CronExpressionError(
                    f"{label} 필드에 빈 항목이 있습니다 (입력: {value!r})."
                )
            _validate_cron_element(
                element, label=label, low=low, high=high, aliases=aliases
            )

    return " ".join(fields)


def interval_hours_to_cron_expression(interval_hours: int) -> str:
    """'매 N시간' interval 을 동등한 표준 cron 표현식으로 변환한다.

    ``0 */N * * *`` 규칙을 따른다: 매시 0분에, N시간 간격(0시 기준)으로 발화한다.
    예) N=6 → ``0 */6 * * *`` (00·06·12·18시). N=1 → ``0 * * * *`` (매시).

    Args:
        interval_hours: '매 N시간' 정수. 1 ≤ N ≤ :data:`MAX_INTERVAL_HOURS`.

    Returns:
        동등한 표준 cron 표현식 문자열.

    Raises:
        ValueError: N 이 1~:data:`MAX_INTERVAL_HOURS` 범위를 벗어난 경우.
    """
    if not isinstance(interval_hours, int) or interval_hours <= 0:
        raise ValueError(
            f"interval_hours 는 양의 정수여야 합니다 (입력: {interval_hours!r})."
        )
    if interval_hours > MAX_INTERVAL_HOURS:
        raise ValueError(
            f"interval 은 최대 {MAX_INTERVAL_HOURS}시간까지입니다 "
            f"(입력: {interval_hours}시간)."
        )
    # N==1 은 ``*/1`` 대신 표준적인 ``*`` 로 단순화한다(동일 의미, 더 관용적).
    if interval_hours == 1:
        return "0 * * * *"
    return f"0 */{interval_hours} * * *"


def _format_active_sources(active_sources: list[str]) -> str:
    """active_sources 리스트를 CLI ``--sources`` 값(콤마 결합)으로 만든다.

    Args:
        active_sources: source id 목록.

    Returns:
        콤마로 결합한 문자열(예: "iris,ntis"). 빈 리스트면 빈 문자열.
    """
    return ",".join(active_sources)


def _general_schedule_to_job(record: "ScheduledJobRecord") -> CrontabJob:
    """일반 수집 스케줄 레코드 1건을 :class:`CrontabJob` 으로 변환한다.

    interval 트리거는 cron 표현식으로 변환하고, active_sources 가 있으면
    ``--sources`` 인자를 붙인다.

    Args:
        record: 활성 상태로 가정된 ``scrape_general`` 스케줄 레코드.

    Returns:
        렌더 가능한 :class:`CrontabJob`.

    Raises:
        ValueError: interval 변환 실패 등 표현식이 비정상인 경우.
    """
    if record.trigger_type == TRIGGER_TYPE_INTERVAL:
        cron_expression = interval_hours_to_cron_expression(
            record.interval_hours if record.interval_hours is not None else 0
        )
        mode_label = f"interval 매{record.interval_hours}시간"
    else:
        cron_expression = (record.cron_expression or "").strip()
        mode_label = "cron"

    cli_arguments = ["scrape"]
    sources_value = _format_active_sources(record.active_sources)
    if sources_value:
        cli_arguments += ["--sources", sources_value]
        sources_label = sources_value
    else:
        sources_label = "(전체)"

    comment = f"[공고 수집] {mode_label} sources={sources_label}"
    return CrontabJob(
        comment=comment,
        cron_expression=cron_expression,
        cli_arguments=cli_arguments,
        log_filename=LOG_FILENAME_SCRAPE,
    )


def _build_job_command(job: CrontabJob, environment: CrontabEnvironment) -> str:
    """잡 1건의 셸 커맨드(환경 로딩 래퍼 + CLI 호출 + 로그 redirect)를 만든다.

    cron 은 환경을 비운 채 잡을 띄우므로, project_dir 로 ``cd`` 한 뒤 ``.env`` 를
    source 해 HOST_PROJECT_DIR 등을 로딩하고 나서 CLI 를 호출한다. stdout/stderr
    는 ``log_dir`` 하위 파일에 append redirect 한다.

    Args:
        job: 렌더할 잡.
        environment: 실행 컨텍스트(경로/실행기/로그 디렉터리 등).

    Returns:
        crontab 시각 필드 뒤에 올 셸 커맨드 문자열.
    """
    # set -a / set +a 사이에서 .env 를 source 하면 해당 파일의 KEY=VALUE 가 모두
    # export 되어 하위 python 프로세스(및 그 안의 docker compose 호출)에 전달된다.
    cli_invocation_tokens = [
        environment.python_executable,
        "-m",
        RUN_JOB_MODULE,
        *job.cli_arguments,
    ]
    cli_invocation = " ".join(cli_invocation_tokens)
    log_path = f"{environment.log_dir}/{job.log_filename}"
    return (
        f"cd {environment.project_dir} && "
        f"set -a && . {environment.env_file} && set +a && "
        f"{cli_invocation} >> {log_path} 2>&1"
    )


def render_crontab(
    jobs: list[CrontabJob], environment: CrontabEnvironment
) -> str:
    """정규화된 잡 리스트와 실행 컨텍스트로 완성된 crontab 텍스트를 만든다.

    순수 함수다 — 동일 입력에 동일 출력이며, 컨테이너·실시간 시계에 의존하지
    않는다. 단위 테스트가 이 출력 문자열을 직접 검증한다.

    출력 구조::

        # 자동 생성 배너
        CRON_TZ=Asia/Seoul
        SHELL=/bin/bash
        <extra_environment KEY=VALUE 라인들>

        # [잡 주석]
        <cron 표현식> <환경 로딩 래퍼 + CLI + 로그 redirect>
        ...

    Args:
        jobs: 렌더할 잡 리스트(이미 활성·유효 표현식만 걸러진 상태).
        environment: 실행 컨텍스트.

    Returns:
        끝에 개행 1개를 포함한 crontab 텍스트(빈 잡이면 헤더만).
    """
    lines: list[str] = [_CRONTAB_HEADER_BANNER]
    lines.append(f"CRON_TZ={environment.timezone}")
    # cron 기본 셸은 /bin/sh 다. .env source 등 bash 친화 구문을 안전히 쓰기
    # 위해 /bin/bash 로 명시한다.
    lines.append("SHELL=/bin/bash")
    # 00155-3 이 주입한 추가 환경(예: docker 바이너리 PATH)을 키 정렬해 박는다.
    for key in sorted(environment.extra_environment):
        lines.append(f"{key}={environment.extra_environment[key]}")

    for job in jobs:
        lines.append("")  # 잡 사이 가독성을 위한 빈 줄.
        lines.append(f"# {job.comment}")
        command = _build_job_command(job, environment)
        lines.append(f"{job.cron_expression} {command}")

    # crontab 은 마지막 줄도 개행으로 끝나야 일부 cron 구현이 마지막 라인을
    # 누락하지 않는다.
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────
# DB 에서 모든 스케줄을 모아 잡 리스트로 환원하는 빌더
# ──────────────────────────────────────────────────────────────


def _is_valid_cron_expression(cron_expression: str | None) -> bool:
    """cron 표현식이 비어있지 않고 5-필드인지 가볍게 검증한다.

    비활성/빈 cron 스케줄을 crontab 라인에서 제외하기 위한 가드다. 필드 값의
    의미적 유효성까지 보지는 않는다(그건 cron 데몬이 판단).

    Args:
        cron_expression: 검사할 표현식(None 허용).

    Returns:
        비어있지 않은 5-필드면 True.
    """
    if not cron_expression:
        return False
    return len(cron_expression.split()) == 5


def _singleton_cron_expression(record: "ScheduledJobRecord") -> str:
    """싱글턴 잡 레코드(backup/daily_report/gc)의 트리거를 cron 표현식으로 환산한다.

    싱글턴은 운영상 cron 트리거로만 쓰이지만, store 는 interval 도 허용하므로
    방어적으로 interval 도 cron 으로 변환한다.

    Args:
        record: backup/daily_report/gc 싱글턴 레코드.

    Returns:
        crontab 라인에 쓸 5-필드 cron 표현식(공백 정리). 트리거가 비정상이면 빈 문자열.
    """
    if record.trigger_type == TRIGGER_TYPE_INTERVAL and record.interval_hours is not None:
        return interval_hours_to_cron_expression(record.interval_hours)
    return (record.cron_expression or "").strip()


def collect_general_schedule_jobs(session: Session) -> list[CrontabJob]:
    """일반 수집(``scrape_general``) 스케줄에서 활성 잡들을 모아 반환한다.

    비활성(enabled=False) 항목과, cron 트리거인데 표현식이 비정상인 항목은
    제외한다. interval 트리거는 cron 표현식으로 변환한다. 소스는 오직
    ``scheduled_jobs`` 테이블이다(SSOT).

    Args:
        session: ORM 세션.

    Returns:
        활성·유효한 일반 수집 :class:`CrontabJob` 리스트.
    """
    # 순환 import 회피를 위한 함수 내부 lazy import(store 가 본 모듈에 의존).
    from app.scheduler.scheduled_job_store import list_general_schedules

    jobs: list[CrontabJob] = []
    for record in list_general_schedules(session):
        if not record.enabled:
            continue
        if record.trigger_type == TRIGGER_TYPE_INTERVAL:
            if record.interval_hours is None:
                continue
        else:
            # cron 트리거 — 빈/비정상 표현식은 제외(비활성과 동일 취급).
            if not _is_valid_cron_expression(record.cron_expression):
                continue
        jobs.append(_general_schedule_to_job(record))
    return jobs


def collect_system_jobs(session: Session) -> list[CrontabJob]:
    """백업·Daily Report·GC 싱글턴 잡을 ``scheduled_jobs`` 기준으로 모아 반환한다.

    task 00157 부터 system_settings 의 여러 키 대신 단일 SSOT 테이블
    ``scheduled_jobs`` 의 싱글턴 row 만 읽는다.

    - 백업: ``backup`` 싱글턴이 enabled 이고 cron 이 정상이면 포함.
    - Daily Report: ``daily_report`` 싱글턴이 enabled 이고 cron 이 정상일 때만 포함.
    - GC: ``gc`` 싱글턴이 enabled 이고 cron 이 정상이면 포함.

    싱글턴 row 는 alembic upgrade(기동 직전)와 store 시드가 멱등 보장하므로,
    DB 만 있으면 기동 시 이 잡들이 완전 복원된다. row 가 비정상적으로 부재하면
    해당 잡만 조용히 생략한다(생성기는 read-only 순수 함수를 유지).

    Args:
        session: ORM 세션.

    Returns:
        시스템 잡 :class:`CrontabJob` 리스트.
    """
    # 순환 import 회피를 위한 함수 내부 lazy import.
    from app.scheduler.constants import (
        JOB_KIND_BACKUP,
        JOB_KIND_DAILY_REPORT,
        JOB_KIND_GC,
    )
    from app.scheduler.scheduled_job_store import get_singleton_schedule

    # (job_kind, 주석, CLI 인자, 로그 파일명) — 동일 규약으로 일괄 환원한다.
    singleton_specs: tuple[tuple[str, str, list[str], str], ...] = (
        (JOB_KIND_BACKUP, "[DB 백업] backup", ["backup"], LOG_FILENAME_BACKUP),
        (
            JOB_KIND_DAILY_REPORT,
            "[Daily Report] daily-report",
            ["daily-report"],
            LOG_FILENAME_DAILY_REPORT,
        ),
        (JOB_KIND_GC, "[고아 첨부 GC] gc", ["gc"], LOG_FILENAME_GC),
    )

    jobs: list[CrontabJob] = []
    for job_kind, comment, cli_arguments, log_filename in singleton_specs:
        record = get_singleton_schedule(session, job_kind)
        if record is None or not record.enabled:
            continue
        cron_expression = _singleton_cron_expression(record)
        if not _is_valid_cron_expression(cron_expression):
            continue
        jobs.append(
            CrontabJob(
                comment=comment,
                cron_expression=cron_expression,
                cli_arguments=cli_arguments,
                log_filename=log_filename,
            )
        )

    return jobs


def build_crontab_jobs(session: Session) -> list[CrontabJob]:
    """모든 스케줄(일반 수집 + 백업 + Daily Report + GC)을 모아 잡 리스트로 만든다.

    Args:
        session: ORM 세션.

    Returns:
        crontab 으로 렌더 가능한 전체 :class:`CrontabJob` 리스트.
    """
    return collect_general_schedule_jobs(session) + collect_system_jobs(session)


def generate_crontab_text(
    session: Session, environment: CrontabEnvironment
) -> str:
    """DB 의 모든 스케줄을 읽어 완성된 crontab 텍스트를 생성한다.

    컨테이너 기동 시(00155-3) 호출하는 최상위 진입점이다.
    :func:`build_crontab_jobs` 로 잡을 모으고 :func:`render_crontab` 로 렌더한다.

    Args:
        session: ORM 세션.
        environment: 실행 컨텍스트(경로/실행기/로그 디렉터리 등).

    Returns:
        cron 데몬이 그대로 설치할 수 있는 crontab 텍스트.
    """
    jobs = build_crontab_jobs(session)
    return render_crontab(jobs, environment)


__all__ = [
    "CronExpressionError",
    "CrontabEnvironment",
    "CrontabJob",
    "DEFAULT_CRON_TIMEZONE",
    "LOG_FILENAME_BACKUP",
    "LOG_FILENAME_DAILY_REPORT",
    "LOG_FILENAME_GC",
    "LOG_FILENAME_SCRAPE",
    "RUN_JOB_MODULE",
    "build_crontab_jobs",
    "collect_general_schedule_jobs",
    "collect_system_jobs",
    "generate_crontab_text",
    "interval_hours_to_cron_expression",
    "render_crontab",
    "validate_cron_expression",
]
