"""DB 스키마 초기화 스크립트.

Alembic 없이 `Base.metadata.create_all` 로 DDL 을 생성한다.
- 모듈을 직접 실행하면 CLI 진입점으로 동작한다: `python -m app.db.init_db`
- 다른 코드에서는 `init_db()` 함수를 호출하면 된다.

멱등성:
    `create_all` 은 이미 존재하는 테이블을 건드리지 않는다.
    스키마 변경이 필요해지면 이후 Alembic 도입을 검토한다.
"""

from __future__ import annotations

import argparse

from loguru import logger
from sqlalchemy import Engine, inspect

from app.config import get_settings
from app.db.migration import run_migrations
from app.db.models import Base

# 모델 클래스들을 import 해야 `Base.metadata` 에 테이블이 등록된다.
# (사용하지 않더라도 부작용을 위해 반드시 import 해야 한다.)
from app.db.models import Announcement, Attachment  # noqa: F401
from app.db.session import get_engine
from app.logging_setup import configure_logging


def init_db(engine: Engine | None = None) -> Engine:
    """누락된 스키마를 마이그레이션하고, 등록된 모든 모델의 테이블을 생성한다.

    순서:
        1. run_migrations: 기존 DB 스키마를 최신으로 업그레이드한다 (멱등).
        2. create_all: 아직 없는 테이블/인덱스를 생성한다 (멱등).

    Args:
        engine: 사용할 엔진. 생략하면 `get_engine()` 이 반환하는 싱글턴을 쓴다.

    Returns:
        실제로 사용된 `Engine` 인스턴스 (CLI 에서 로깅 용도로 재사용).
    """
    effective_engine = engine or get_engine()

    # 런타임 경로(예: SQLite 파일의 부모 디렉터리)를 한 번 더 보장한다.
    get_settings().ensure_runtime_paths()

    # 기존 DB 컬럼 변경/추가 마이그레이션 (create_all 전에 실행해야 한다)
    run_migrations(effective_engine)

    existing_before = set(inspect(effective_engine).get_table_names())
    logger.info(
        "DB 초기화 시작: dialect={dialect} url_masked={url} "
        "existing_tables={tables}",
        dialect=effective_engine.dialect.name,
        url=_mask_db_url(str(effective_engine.url)),
        tables=sorted(existing_before),
    )

    Base.metadata.create_all(bind=effective_engine)

    existing_after = set(inspect(effective_engine).get_table_names())
    created = sorted(existing_after - existing_before)
    logger.info(
        "DB 초기화 완료: created_tables={created} all_tables={tables}",
        created=created,
        tables=sorted(existing_after),
    )

    return effective_engine


def _mask_db_url(url: str) -> str:
    """DB URL 의 비밀번호 부분을 마스킹해 로그용 문자열을 만든다.

    SQLite 등 비밀번호가 없는 URL 은 그대로 반환한다.
    """
    if "@" not in url or "://" not in url:
        return url
    scheme_and_rest = url.split("://", 1)
    if len(scheme_and_rest) != 2:
        return url
    scheme, rest = scheme_and_rest
    if "@" not in rest:
        return url
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _pwd = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://{creds}@{host}"


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 생성한다.

    현재는 옵션이 없지만, 추후 `--drop` 등의 플래그를 넣을 여지를 둔다.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.db.init_db",
        description="DB 스키마를 생성한다 (create_all, 멱등).",
    )
    return parser


def main() -> None:
    """CLI 진입점. 로깅을 설정한 뒤 `init_db()` 를 호출한다."""
    _build_arg_parser().parse_args()
    configure_logging()
    init_db()


if __name__ == "__main__":
    main()
