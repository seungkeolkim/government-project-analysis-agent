"""DB 스키마 초기화 스크립트.

Alembic 기반 stamp vs upgrade 분기로 멱등하게 스키마를 보장한다.

전략 결정 (3가지 상황):
    1. alembic_version 테이블이 없고 다른 테이블도 없음 (빈 DB)
       → alembic upgrade head — baseline migration 으로 전체 스키마 생성
    2. alembic_version 테이블이 없지만 다른 테이블이 있음 (기존 운영 DB)
       → alembic stamp head — 스키마/데이터를 건드리지 않고 리비전 레코드만 삽입
    3. alembic_version 테이블이 있음 (이미 Alembic 관리 DB)
       → alembic upgrade head — 신규 migration 이 있으면 적용, 없으면 no-op

모든 경로는 멱등하게 재실행 가능하다.
기존 DB 의 데이터는 절대 변경/삭제하지 않는다.

외부 호출 API:
    `init_db()` 시그니처는 변경하지 않는다.
    app.cli 와 app.web.main 의 호출 라인은 그대로 유지된다.

CLI:
    python -m app.db.init_db
"""

from __future__ import annotations

import argparse

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from loguru import logger
from sqlalchemy import Engine, inspect

from app.config import PROJECT_ROOT, get_settings
from app.db.session import get_engine
from app.logging_setup import configure_logging


def _build_alembic_config(engine: Engine) -> AlembicConfig:
    """alembic.ini 를 로드하고 접속 URL 을 engine 에서 주입한 Config 를 반환한다.

    alembic.ini 의 sqlalchemy.url 은 비어 있으므로 engine.url 로 override 한다.
    이렇게 하면 init_db(some_engine) 처럼 특정 엔진을 주입했을 때도
    Alembic 이 동일한 DB 에 접속한다.

    Args:
        engine: 대상 DB 에 이미 연결된 SQLAlchemy Engine.

    Returns:
        실행 준비된 AlembicConfig 인스턴스.
    """
    alembic_ini_path = PROJECT_ROOT / "alembic.ini"
    config = AlembicConfig(str(alembic_ini_path))
    # engine.url 을 문자열로 변환 — password masking 되지 않은 원본 URL 필요
    config.set_main_option("sqlalchemy.url", str(engine.url))
    return config


def _apply_alembic_migrations(engine: Engine) -> str:
    """DB 상태에 따라 stamp 또는 upgrade 전략을 선택하고 Alembic migration 을 적용한다.

    전략 결정 순서:
        1. alembic_version 테이블이 있으면 → upgrade head (Alembic 관리 DB)
        2. 다른 테이블이 하나라도 있으면 → stamp head (기존 운영 DB)
        3. 테이블이 전혀 없으면 → upgrade head (신규/빈 DB)

    Args:
        engine: 대상 DB 엔진.

    Returns:
        실제 사용된 전략 문자열: "upgrade" | "stamp" | "baseline-bootstrap"
    """
    db_inspector = inspect(engine)
    table_names = set(db_inspector.get_table_names())
    has_alembic_version_table = "alembic_version" in table_names

    alembic_cfg = _build_alembic_config(engine)

    if has_alembic_version_table:
        # 이미 Alembic 이 관리하는 DB — 신규 migration 적용 (없으면 no-op)
        strategy = "upgrade"
        logger.info(
            "초기화 전략: upgrade (alembic_version 테이블 감지 — Alembic 관리 DB)"
        )
        alembic_command.upgrade(alembic_cfg, "head")

    elif table_names:
        # alembic_version 은 없지만 다른 테이블이 있음 → 기존 운영 DB
        # create_all 또는 run_migrations 로 스키마를 만들었던 DB.
        # 데이터를 건드리지 않고 리비전 레코드만 등록한다.
        strategy = "stamp"
        logger.info(
            "초기화 전략: stamp (기존 DB 감지 — alembic_version 없음, "
            "테이블 존재: {tables})",
            tables=sorted(table_names),
        )
        alembic_command.stamp(alembic_cfg, "head")

    else:
        # 완전히 빈 DB — baseline migration 으로 전체 스키마를 생성한다
        strategy = "baseline-bootstrap"
        logger.info(
            "초기화 전략: baseline-bootstrap (빈 DB 감지 — 전체 스키마 생성)"
        )
        alembic_command.upgrade(alembic_cfg, "head")

    return strategy


def init_db(engine: Engine | None = None) -> Engine:
    """스키마를 Alembic 기반으로 멱등하게 보장한다.

    stamp vs upgrade 분기:
        - 신규 DB (테이블 없음) → alembic upgrade head (baseline 스키마 생성)
        - 기존 DB (alembic_version 없음, 테이블 있음) → alembic stamp head (리비전만 등록)
        - Alembic 관리 DB (alembic_version 있음) → alembic upgrade head (신규 migration 적용)

    외부 호출 시그니처는 변경하지 않는다 (cli.py, web/main.py 호출 라인 유지).

    Args:
        engine: 사용할 엔진. 생략하면 `get_engine()` 이 반환하는 싱글턴을 쓴다.

    Returns:
        실제로 사용된 `Engine` 인스턴스.
    """
    effective_engine = engine or get_engine()

    # 런타임 경로(예: SQLite 파일의 부모 디렉터리)를 한 번 더 보장한다.
    get_settings().ensure_runtime_paths()

    logger.info(
        "DB 초기화 시작: dialect={dialect} url_masked={url}",
        dialect=effective_engine.dialect.name,
        url=_mask_db_url(str(effective_engine.url)),
    )

    strategy = _apply_alembic_migrations(effective_engine)

    logger.info(
        "DB 초기화 완료: strategy={strategy} dialect={dialect}",
        strategy=strategy,
        dialect=effective_engine.dialect.name,
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
    """CLI 인자 파서를 생성한다."""
    parser = argparse.ArgumentParser(
        prog="python -m app.db.init_db",
        description="DB 스키마를 Alembic 으로 보장한다 (stamp/upgrade, 멱등).",
    )
    return parser


def main() -> None:
    """CLI 진입점. 로깅을 설정한 뒤 `init_db()` 를 호출한다."""
    _build_arg_parser().parse_args()
    configure_logging()
    init_db()


if __name__ == "__main__":
    main()
