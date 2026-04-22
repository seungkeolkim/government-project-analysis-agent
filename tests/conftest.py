"""pytest 전역 fixture.

테스트별로 격리된 SQLite DB 를 tmp_path 에 만들고 Alembic migration 으로
스키마를 올린다. 모든 테스트 fixture 는 `get_settings` / `get_engine` /
`_get_session_factory` 의 `@lru_cache` 를 매번 비워서 테스트 간 엔진/세션
공유가 일어나지 않게 한다.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session


@pytest.fixture
def _test_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """테스트별 고유 SQLite 파일 URL 을 환경변수에 주입한다.

    in-memory SQLite 는 connection 별로 DB 가 달라지므로 파일 기반을 쓴다.
    tmp_path 는 테스트 종료 시 자동 정리된다.
    """
    db_file = tmp_path / "test.sqlite3"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DB_URL", db_url)
    return db_url


@pytest.fixture
def test_engine(_test_db_url: str) -> Iterator[Engine]:
    """fresh Engine + Alembic upgrade head 적용된 SQLite 엔진을 제공한다.

    - lru_cache 가 씌워진 get_settings / get_engine / _get_session_factory 를
      비워 이전 테스트의 엔진이 새 테스트로 흘러가지 않게 한다.
    - init_db 를 호출해 baseline + Phase 1a migration 을 모두 올린다.
    - 테스트 종료 시 엔진 dispose 및 캐시 재정리.
    """
    # 이전 테스트의 캐시 제거 (동일 프로세스에서 순차 실행되므로 필요).
    from app.config import get_settings
    from app.db.session import reset_engine_cache, get_engine

    get_settings.cache_clear()
    reset_engine_cache()

    # init_db 가 baseline → phase1a 까지 upgrade 한다.
    from app.db.init_db import init_db

    engine = get_engine()
    init_db(engine)

    try:
        yield engine
    finally:
        engine.dispose()
        get_settings.cache_clear()
        reset_engine_cache()


@pytest.fixture
def db_session(test_engine: Engine) -> Iterator[Session]:
    """테스트용 ORM 세션. 테스트 종료 시 close."""
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
