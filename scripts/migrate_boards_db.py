"""운영자용 수동 마이그레이션 스크립트 (task 00056).

``suggestions.sqlite3`` → ``boards.sqlite3`` 파일 이름 변경을 웹 서버 기동 없이
독립적으로 실행한다. 웹 startup 에서 자동으로 실행되는 것과 동일한 헬퍼를 호출하므로
멱등하다 — 이미 완료된 환경에서 실행해도 안전하다.

사용법::

    python scripts/migrate_boards_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가한다 (스크립트 단독 실행 지원)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger

from app.suggestions.migration import migrate_suggestions_to_boards


def main() -> None:
    """마이그레이션 헬퍼를 실행하고 결과를 표준 출력에 보고한다."""
    logger.info("boards DB 마이그레이션 스크립트 시작")
    try:
        migrate_suggestions_to_boards()
        logger.info("boards DB 마이그레이션 스크립트 완료")
    except Exception as exc:
        logger.error("boards DB 마이그레이션 실패: {exc}", exc=exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
