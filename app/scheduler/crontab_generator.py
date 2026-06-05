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

대상 스케줄
-----------
1. 일반 공고 수집(N건): :mod:`app.scheduler.schedule_store` 영속 저장소.
   cron 모드 / interval('매 N시간') 모드 모두 지원.
2. DB 파일 백업(1건): SystemSetting ``backup.cron_expression``.
3. Daily Report 발송(1건): SystemSetting ``email.daily_report.enabled`` +
   ``email.daily_report.cron_expression`` (enabled 일 때만).
4. 고아 첨부 파일 GC(1건): 기본 ``0 4 * * *`` (:data:`DEFAULT_GC_ORPHAN_CRON`).

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

from sqlalchemy.orm import Session

from app.scheduler.constants import (
    DEFAULT_GC_ORPHAN_CRON,
    MAX_INTERVAL_HOURS,
)
from app.scheduler.schedule_store import (
    SCHEDULE_MODE_INTERVAL,
    GeneralScheduleRecord,
    list_general_schedule_records,
)

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


def _general_schedule_to_job(record: GeneralScheduleRecord) -> CrontabJob:
    """일반 수집 스케줄 레코드 1건을 :class:`CrontabJob` 으로 변환한다.

    interval 모드는 cron 표현식으로 변환하고, active_sources 가 있으면
    ``--sources`` 인자를 붙인다.

    Args:
        record: 활성 상태로 가정된 일반 수집 스케줄 레코드.

    Returns:
        렌더 가능한 :class:`CrontabJob`.

    Raises:
        ValueError: interval 변환 실패 등 표현식이 비정상인 경우.
    """
    if record.mode == SCHEDULE_MODE_INTERVAL:
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


def collect_general_schedule_jobs(session: Session) -> list[CrontabJob]:
    """일반 수집 스케줄 저장소에서 활성 잡들을 모아 반환한다.

    비활성(enabled=False) 항목과, cron 모드인데 표현식이 비정상인 항목은
    제외한다. interval 모드는 cron 표현식으로 변환한다.

    Args:
        session: ORM 세션.

    Returns:
        활성·유효한 일반 수집 :class:`CrontabJob` 리스트.
    """
    jobs: list[CrontabJob] = []
    for record in list_general_schedule_records(session):
        if not record.enabled:
            continue
        if record.mode == SCHEDULE_MODE_INTERVAL:
            if record.interval_hours is None:
                continue
        else:
            # cron 모드 — 빈/비정상 표현식은 제외(비활성과 동일 취급).
            if not _is_valid_cron_expression(record.cron_expression):
                continue
        jobs.append(_general_schedule_to_job(record))
    return jobs


def collect_system_jobs(session: Session) -> list[CrontabJob]:
    """백업·Daily Report·GC 시스템 잡을 SystemSetting 기준으로 모아 반환한다.

    - 백업: ``backup.cron_expression`` (없으면 기본값). 표현식이 정상이면 포함.
    - Daily Report: ``email.daily_report.enabled`` 가 True 이고 cron 이 정상일
      때만 포함.
    - GC: 기본 ``0 4 * * *`` 로 항상 포함.

    SystemSetting 접근은 ``app.backup.service.get_setting`` 헬퍼를 lazy import 해
    재사용한다(순환 import 회피).

    Args:
        session: ORM 세션.

    Returns:
        시스템 잡 :class:`CrontabJob` 리스트.
    """
    # 순환 import 회피를 위한 함수 내부 lazy import.
    from app.backup.constants import DEFAULT_BACKUP_CRON, SETTING_KEY_BACKUP_CRON
    from app.backup.service import get_setting
    from app.email.constants import (
        DEFAULT_DAILY_REPORT_CRON,
        SETTING_KEY_DAILY_REPORT_CRON,
        SETTING_KEY_DAILY_REPORT_ENABLED,
    )

    jobs: list[CrontabJob] = []

    # ── 백업 ──────────────────────────────────────────────────
    backup_cron = (get_setting(session, SETTING_KEY_BACKUP_CRON) or "").strip()
    if not backup_cron:
        backup_cron = DEFAULT_BACKUP_CRON
    if _is_valid_cron_expression(backup_cron):
        jobs.append(
            CrontabJob(
                comment="[DB 백업] backup",
                cron_expression=backup_cron,
                cli_arguments=["backup"],
                log_filename=LOG_FILENAME_BACKUP,
            )
        )

    # ── Daily Report (enabled 일 때만) ────────────────────────
    raw_enabled = get_setting(session, SETTING_KEY_DAILY_REPORT_ENABLED)
    daily_report_enabled = (raw_enabled or "").strip().lower() == "true"
    if daily_report_enabled:
        daily_report_cron = (
            get_setting(session, SETTING_KEY_DAILY_REPORT_CRON) or ""
        ).strip()
        if not daily_report_cron:
            daily_report_cron = DEFAULT_DAILY_REPORT_CRON
        if _is_valid_cron_expression(daily_report_cron):
            jobs.append(
                CrontabJob(
                    comment="[Daily Report] daily-report",
                    cron_expression=daily_report_cron,
                    cli_arguments=["daily-report"],
                    log_filename=LOG_FILENAME_DAILY_REPORT,
                )
            )

    # ── GC (기본값으로 항상 포함) ─────────────────────────────
    jobs.append(
        CrontabJob(
            comment="[고아 첨부 GC] gc",
            cron_expression=DEFAULT_GC_ORPHAN_CRON,
            cli_arguments=["gc"],
            log_filename=LOG_FILENAME_GC,
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
]
