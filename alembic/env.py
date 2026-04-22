"""Alembic 환경 설정.

DB 접속 URL 은 app.config.get_settings().db_url 에서 동적으로 주입한다.
alembic.ini 의 sqlalchemy.url 은 비워두고 이 파일이 override 한다.

pydantic-settings 는 .env 파일을 자동 로드하므로,
`alembic upgrade head` CLI 단독 실행 시에도 .env 가 반영된다.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings

# ORM 모델이 Base.metadata 에 등록되려면 반드시 import 해야 한다.
from app.db.models import Base  # noqa: F401
from app.db.models import Announcement, Attachment, CanonicalProject  # noqa: F401

# alembic.ini 의 [alembic] 섹션에 접근하는 config 객체
alembic_config = context.config

# alembic.ini 의 [loggers] 섹션으로 Python 표준 로깅을 초기화한다.
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# 앱 설정에서 DB URL 을 읽어 alembic config 에 주입한다.
# alembic.ini 의 sqlalchemy.url 은 비워두고 이 코드가 override 한다.
_settings = get_settings()
alembic_config.set_main_option("sqlalchemy.url", _settings.db_url)

# autogenerate 가 ORM 모델과 실제 DB 를 비교할 때 사용하는 메타데이터
target_metadata = Base.metadata


def _build_connect_args() -> dict[str, object]:
    """DB 방언별 연결 파라미터를 반환한다.

    SQLite 는 멀티스레드 환경(FastAPI + 스크래퍼)을 위해
    check_same_thread=False 가 필요하다.
    """
    if _settings.db_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def run_migrations_offline() -> None:
    """'offline' 모드로 migration 을 실행한다.

    실제 DB 연결 없이 SQL 스크립트만 출력한다.
    render_as_batch=True 로 SQLite의 ALTER TABLE 호환성을 보장한다.
    """
    url = alembic_config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite ALTER TABLE 호환성 — docs/db_portability.md 4번 항목
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """'online' 모드로 migration 을 실행한다.

    실제 DB 연결을 맺어 migration 을 적용한다.
    render_as_batch=True 로 SQLite의 ALTER TABLE 호환성을 보장한다.
    NullPool 을 사용해 migration 완료 후 즉시 연결을 반환한다.
    """
    connectable = create_engine(
        _settings.db_url,
        # migration 전용 연결 — 풀링 불필요
        poolclass=pool.NullPool,
        connect_args=_build_connect_args(),
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite ALTER TABLE 호환성 — docs/db_portability.md 4번 항목
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
