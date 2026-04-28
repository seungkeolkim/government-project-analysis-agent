"""로깅 초기화 모듈.

loguru 를 루트 싱크로 사용하되, 표준 라이브러리(stdlib) ``logging`` 을 통해
로그를 남기는 서드파티(uvicorn / starlette / fastapi / sqlalchemy / alembic 등)
도 동일한 sink 로 흘러가도록 ``InterceptHandler`` 브리지를 설치한다.

호출 규칙:
    - 웹 프로세스는 ``app.web.main.create_app()`` 최상단에서 한 번 호출한다.
    - CLI 진입점(``app.cli``, ``scripts/*``) 은 각자 본인의 ``main()`` 시작 시
      호출한다.
    - ``configure_logging()`` 은 **idempotent** — 중복 호출되어도 마지막 호출의
      결과만 남는다 (``logger.remove()`` 로 기존 loguru 핸들러를 비우고,
      stdlib root 핸들러도 교체).

왜 브리지가 필요한가:
    ``uvicorn`` 은 FastAPI 라우트에서 발생한 미처리 예외를 stdlib ``logging``
    의 ``uvicorn.error`` 채널로 기록한다. loguru 는 stdlib 와 별도 시스템이라
    브리지가 없으면 이 로그(및 stack trace)가 loguru sink 로 나오지 않아
    docker logs 가 비어 보이게 된다. ``InterceptHandler`` 를 root logger 에
    부착해 모든 stdlib 레코드를 loguru 로 재방출한다.
"""

from __future__ import annotations

import logging
import sys

from loguru import logger

from app.config import Settings, get_settings
# task 00040-4 — 모든 로그 record 의 timestamp 를 명시적 KST 로 찍는다.
# ``app.timezone`` 이 KST 단일 진실 소스 (호스트 컨테이너 ``TZ`` env 미설정에서도
# 코드 레벨 변환이 동작하도록).
from app.timezone import KST

# stdlib logging 을 흡수해 loguru 로 다시 내보내는 핸들러에 주의가 필요한
# 로거 이름들. uvicorn 계열은 자체 ``default`` logging config 가 있어 직접
# StreamHandler 를 달아두기 때문에, 핸들러를 비우고 propagate=True 로 바꿔
# root 의 InterceptHandler 로만 흘러가게 강제해야 로그가 두 번 찍히지 않는다.
_UVICORN_LOGGER_NAMES: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "uvicorn.asgi",
)

# 너무 시끄러워질 수 있는 서드파티 로거는 log_level 과 무관하게 최소 레벨을
# 상향 고정한다. DEBUG 일 때도 raw SQL 전부를 쏟지는 않게 해 가독성을 지킨다.
_NOISY_LOGGER_NAMES: tuple[str, ...] = (
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "sqlalchemy.dialects",
    "alembic.runtime.migration",
)


def _patch_record_with_kst_time(record: dict) -> None:
    """모든 loguru record 에 KST 변환된 timestamp 문자열을 부착한다.

    배경 (task 00040-4):
        loguru 의 ``{time:...}`` 토큰은 ``record[\"time\"]`` 의 호스트 로컬 tz 를
        그대로 사용한다. 컨테이너에 ``TZ=Asia/Seoul`` env (00029) 가 있으면
        의도대로 KST 로 찍히지만, env 가 빠진 호스트(개발자 노트북 등)에서는
        호스트 tz 로 찍힌다. 사용자 원문 검증 ⑥ \"컨테이너 TZ 미설정에서도
        코드 레벨 변환 동작\" 을 만족시키기 위해 patcher 단계에서 명시적으로
        ``astimezone(KST)`` 를 적용한 결과를 ``record['extra']['kst_time_text']``
        로 미리 만들어 둔다.

    포맷 정밀도:
        기존 sink format ``{time:YYYY-MM-DD HH:mm:ss.SSS}`` 의 millisecond
        정밀도를 그대로 유지한다. ``%f`` 는 microsecond(6자리)이므로 1000 으로
        나눠 millisecond 3자리로 자른다.

    Args:
        record: loguru 가 emit 직전에 patcher 로 넘겨주는 record dict.
            ``record['time']`` 은 timezone-aware ``datetime`` (loguru 내부에서
            호스트 로컬 tz 가 부착된 상태).
    """
    kst_time = record["time"].astimezone(KST)
    record["extra"]["kst_time_text"] = (
        kst_time.strftime("%Y-%m-%d %H:%M:%S")
        + f".{kst_time.microsecond // 1000:03d}"
    )


class InterceptHandler(logging.Handler):
    """stdlib ``logging`` 레코드를 loguru 로 재방출하는 핸들러.

    loguru 공식 문서의 "Entirely compatible with standard logging" 섹션
    구현을 그대로 따른다. depth 를 계산해 loguru 가 ``{name}:{function}:{line}``
    포맷을 찍을 때 실제 호출자(stdlib 내부 프레임이 아닌 원래 코드) 위치를
    가리키도록 한다.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """stdlib LogRecord 를 동일 레벨 · 동일 메시지로 loguru 에 전달한다.

        - 레벨 매핑: 이름이 loguru 에 등록돼 있으면 그대로, 없으면 정수 레벨로
          fallback (WARN/FATAL 처럼 loguru 에 없는 이름 방어).
        - depth 계산: logging 모듈 내부 프레임을 스킵해 호출 지점을 되찾는다.
        - ``record.exc_info`` 는 그대로 전달해 loguru 가 traceback 을 포함한
          다채로운 포맷을 만들도록 한다.
        """
        # 1) 레벨 매핑 — 이름 기반이 깨지면 정수 레벨로 폴백.
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 2) 호출 프레임 깊이 계산 — loguru 가 찍는 {name}:{function}:{line} 을
        #    stdlib logging 내부 프레임이 아닌 실제 호출자 위치로 맞춘다.
        #    Python 3.12 의 ``logging.currentframe`` 은 ``sys._getframe(1)`` 로
        #    ``emit`` 프레임 자체를 돌려주기 때문에, 먼저 한 번은 무조건 walk
        #    하고(``depth == 0`` 조건) 그 뒤부터 logging 모듈 프레임을 계속
        #    건너뛴다. 이 패턴은 loguru 최신 README 레시피와 동일하다.
        frame: object | None = logging.currentframe()
        depth = 0
        while frame is not None and (
            depth == 0 or frame.f_code.co_filename == logging.__file__
        ):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _install_stdlib_bridge(level_name: str) -> None:
    """stdlib root logger 를 비우고 ``InterceptHandler`` 하나만 부착한다.

    Args:
        level_name: loguru 와 동일한 레벨 문자열 (DEBUG/INFO/WARNING/ERROR).

    - 기존 root handler 는 모두 제거해 중복 출력을 막는다.
    - root 레벨은 주어진 값과 동일하게 낮춘다 (레벨은 ``logger`` 선에서 먼저
      필터되므로, DEBUG 로그를 보려면 root 도 DEBUG 여야 한다).
    - uvicorn 계열 로거는 자체 StreamHandler 를 추가로 달기 때문에 명시적으로
      청소 + ``propagate=True`` 로 전환한다.
    """
    numeric_level = logging.getLevelName(level_name)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root_logger = logging.getLogger()
    root_logger.handlers = [InterceptHandler()]
    root_logger.setLevel(numeric_level)

    for logger_name in _UVICORN_LOGGER_NAMES:
        target = logging.getLogger(logger_name)
        target.handlers = []
        target.propagate = True
        # 레벨은 root 와 일치시켜 DEBUG 가 막히지 않게 한다.
        target.setLevel(numeric_level)

    # sqlalchemy / alembic 은 DEBUG 일 때 raw SQL / bind 값까지 쏟아 과도하다.
    # root 레벨보다 한 단계 위(INFO) 로 고정해 디버그 로그 전체 가독성을 지킨다.
    for noisy_name in _NOISY_LOGGER_NAMES:
        noisy = logging.getLogger(noisy_name)
        noisy.setLevel(max(numeric_level, logging.INFO))


def configure_logging(settings: Settings | None = None) -> None:
    """프로세스 시작 시 1회 호출해 loguru + stdlib 로깅을 재설정한다.

    Args:
        settings: 주입할 설정. 없으면 ``get_settings()`` 를 사용한다.

    동작:
        1. loguru 기본 핸들러를 모두 제거하고 stderr 싱크를 새로 부착한다.
        2. ``settings.log_level`` 값을 loguru 싱크 레벨로 사용한다.
        3. ``DEBUG`` 일 때만 ``diagnose=True`` 로 traceback 에 변수 값을 inline
           으로 표시한다 (민감 값 유출 방지를 위해 INFO 이상에서는 False).
        4. ``InterceptHandler`` 를 stdlib root 에 설치해 uvicorn/starlette/
           fastapi/sqlalchemy/alembic 로그가 모두 loguru 로 흐르게 한다.
        5. uvicorn 계열 로거의 자체 핸들러를 제거하고 propagate=True 로 바꿔
           중복 출력을 방지한다.
        6. 부트스트랩이 끝나면 적용된 레벨을 INFO 로 한 줄 남겨, ``LOG_LEVEL``
           환경변수가 실제로 반영됐는지 docker logs 에서 육안 검증할 수 있게 한다.

    Idempotent 하다 — ``logger.remove()`` 와 root handler 재설정이 기존 상태를
    덮어쓰므로 중복 호출이 안전하다.
    """
    effective = settings or get_settings()
    level_name = effective.log_level  # 이미 대문자 정규화돼 있음.

    # DEBUG 일 때만 diagnose 를 켠다. 운영 레벨(INFO 이상) 에서는 traceback 의
    # 로컬 변수가 stderr 로 흘러나가지 않도록 False.
    diagnose_flag = level_name == "DEBUG"

    logger.remove()
    # 00030-2 — HTTP 미들웨어에서 ``logger.contextualize(request_id=...)`` 로
    # 주입한 요청 ID 를 모든 로그 라인에 자동 노출하도록 기본값을 ``"-"`` 로
    # 둔다. 요청 컨텍스트 밖(초기화·스케줄러·CLI)에서는 ``req=-`` 로 찍혀
    # "요청 기반 로그가 아님" 을 명확히 한다. ``logger.configure(extra=...)`` 은
    # 전역 기본 extras 를 덮어쓰므로 현재 사용 중인 다른 extra 키가 있다면
    # 함께 나열해야 하지만, 본 레포지토리에는 아직 없다.
    #
    # task 00040-4 — patcher 로 모든 record 에 KST 변환된 timestamp 문자열
    # ``extra[kst_time_text]`` 를 부착해 sink format 에서 그대로 출력한다.
    # ``{time:...}`` 토큰을 사용하면 호스트 tz 의존성이 남으므로 사용하지
    # 않는다. patcher 등록은 모든 record 적용을 보장하는 가장 단순한 경로.
    logger.configure(
        extra={"request_id": "-", "kst_time_text": "-"},
        patcher=_patch_record_with_kst_time,
    )
    logger.add(
        sys.stderr,
        level=level_name,
        format=(
            # extra[kst_time_text] 는 patcher 가 모든 record 에 부착한다
            # (호스트 TZ 비의존). suffix 'KST' 로 의도된 표시 tz 를 명시한다.
            "<green>{extra[kst_time_text]} KST</green> "
            "| <level>{level: <8}</level> "
            "| <magenta>req={extra[request_id]}</magenta> "
            "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
            "- <level>{message}</level>"
        ),
        backtrace=True,
        diagnose=diagnose_flag,
        enqueue=False,
    )

    _install_stdlib_bridge(level_name)

    # 적용된 레벨을 남겨 .env 의 LOG_LEVEL 설정이 실제 반영됐는지 docker logs
    # 상단에서 바로 확인할 수 있게 한다. DEBUG 일 때 diagnose 가 활성화됐다는
    # 사실도 함께 노출해 운영자가 로그 포맷 차이를 이해할 수 있도록 한다.
    logger.info(
        "로깅 초기화 완료: log_level={} diagnose={} stdlib_bridge=installed",
        level_name,
        diagnose_flag,
    )


__all__ = ["configure_logging", "InterceptHandler", "_patch_record_with_kst_time"]
