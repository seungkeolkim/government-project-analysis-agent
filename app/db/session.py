"""SQLAlchemy 엔진과 세션 팩토리 모듈.

- 엔진은 프로세스당 1개만 생성한다(`get_engine` 의 lru_cache).
- 세션은 `session_scope()` 컨텍스트 매니저 또는 `SessionLocal()` 팩토리로 얻는다.
- 접속 문자열은 `app.config.get_settings().db_url` 에서 읽는다.
- SQLite 를 쓸 때는 다중 스레드(FastAPI + 스크래퍼) 환경을 고려해
  `check_same_thread=False` 를 설정한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from loguru import logger
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings


def _build_engine(settings: Settings) -> Engine:
    """설정값으로부터 SQLAlchemy 엔진을 생성한다.

    Args:
        settings: 접속 문자열을 포함한 전역 설정.

    Returns:
        생성된 `Engine` 인스턴스.

    Notes:
        - SQLite 접속 문자열에 한해 `check_same_thread=False` 를 주입한다.
        - echo 는 기본 비활성화(로그 과다 방지). 필요 시 log_level 로 제어한다.
    """
    connect_args: dict[str, object] = {}
    if settings.db_url.startswith("sqlite"):
        # FastAPI/스크래퍼가 동일 프로세스에서 서로 다른 스레드로 접속할 수 있다.
        connect_args["check_same_thread"] = False

    return create_engine(
        settings.db_url,
        echo=False,
        future=True,
        connect_args=connect_args,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """프로세스 수명 동안 공유할 엔진 싱글턴을 반환한다.

    테스트에서 DB URL 을 바꿔야 하면 `get_engine.cache_clear()` 및
    `get_settings.cache_clear()` 를 호출한다.
    """
    settings = get_settings()
    # 런타임 경로(특히 SQLite 파일의 부모 디렉터리)를 보장한다.
    settings.ensure_runtime_paths()
    return _build_engine(settings)


@lru_cache(maxsize=1)
def _get_session_factory() -> sessionmaker[Session]:
    """`sessionmaker` 팩토리를 싱글턴으로 반환한다."""
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def SessionLocal() -> Session:  # noqa: N802 - 외부에서 FastAPI 스타일로 쓸 수 있도록 PascalCase 유지
    """새 ORM 세션을 생성해 반환한다.

    사용자가 직접 `close()` 호출을 관리해야 한다.
    일반적으로는 `session_scope()` 컨텍스트 매니저 사용을 권장한다.
    """
    return _get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """트랜잭션 경계가 있는 세션 컨텍스트 매니저.

    - 블록 정상 종료 시 `commit()`
    - 예외 발생 시 `rollback()` 후 재발행
    - 어느 경우든 `close()` 보장

    Example:
        with session_scope() as session:
            session.add(obj)
    """
    # 00030-3 — 트랜잭션 lifecycle 추적용 DEBUG 로그. 요청 컨텍스트 안에서는
    # observability 미들웨어가 request_id 를 contextualize 해 두었기 때문에
    # 이 라인에도 동일한 req=... 가 찍혀 한 요청이 session_scope 를 몇 번
    # 여는지 쉽게 볼 수 있다. commit/rollback 은 별도 라인으로 남겨 어떤
    # 경로로 종료됐는지 분리한다.
    session = SessionLocal()
    logger.debug("session_scope open")
    try:
        yield session
        session.commit()
        logger.debug("session_scope commit")
    except Exception:
        session.rollback()
        logger.debug("session_scope rollback (예외 전파)")
        raise
    finally:
        session.close()
        logger.debug("session_scope close")


def reset_engine_cache() -> None:
    """엔진/세션팩토리 캐시를 모두 비운다.

    테스트 환경에서 DB URL 을 동적으로 바꾸거나, 런타임 설정 리로드 후
    새 엔진을 쓰고자 할 때 사용한다.
    """
    _get_session_factory.cache_clear()
    get_engine.cache_clear()


__all__ = [
    "get_engine",
    "SessionLocal",
    "session_scope",
    "reset_engine_cache",
]
