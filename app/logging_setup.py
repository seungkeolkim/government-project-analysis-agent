"""로깅 초기화 모듈.

loguru 를 루트 싱크로 사용하되, 가독성을 위해 포맷과 레벨만 설정한다.
앱이 커지면 파일 로테이션이나 JSON 로깅을 이곳에서 추가한다.
"""

from __future__ import annotations

import sys

from loguru import logger

from app.config import Settings, get_settings


def configure_logging(settings: Settings | None = None) -> None:
    """프로세스 시작 시 1회 호출해 loguru 싱크를 재설정한다.

    Args:
        settings: 주입할 설정. 없으면 `get_settings()` 를 사용한다.

    동작:
        - 기본 핸들러를 모두 제거한 뒤 stderr 로만 내보낸다.
        - 로그 레벨은 `settings.log_level` 값을 따른다.
    """
    effective = settings or get_settings()

    logger.remove()
    logger.add(
        sys.stderr,
        level=effective.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
            "| <level>{level: <8}</level> "
            "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
            "- <level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )
