"""건의사항 게시판 전용 SQLAlchemy 엔진과 세션 팩토리.

본 모듈은 메인 DB(:mod:`app.db.session`) 와 동일한 패턴(엔진 lru_cache + 세션
팩토리 + ``session_scope`` 컨텍스트 매니저)을 제공하지만, 접속 문자열은
``Settings.suggestions_db_url`` 에서 읽어 별도 SQLite 파일을 가리킨다.

설계 메모:
    - 두 DB 는 같은 프로세스 안에서 공존한다. 라우트는 메인 DB 세션과 본
      모듈의 세션을 동시에 들고 cross-DB 작성자 유효성 헬퍼를 호출한다.
    - SQLite 다중 스레드(웹 + 스크래퍼) 환경을 고려해
      ``check_same_thread=False`` 를 주입한다.
    - 테이블 자체는 :func:`init_suggestions_db` 가 ``Base.metadata.create_all``
      로 생성한다 — 메인 DB 의 Alembic 흐름과 분리되어 있어 메인 DB reset
      시에도 본 DB 파일은 영향을 받지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from loguru import logger
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.suggestions.models import Base


def _build_suggestions_engine(settings: Settings) -> Engine:
    """건의사항 DB 용 SQLAlchemy 엔진을 생성한다.

    Args:
        settings: ``suggestions_db_url`` 을 포함한 전역 설정.

    Returns:
        생성된 ``Engine`` 인스턴스.

    Notes:
        - SQLite 접속 문자열에 한해 ``check_same_thread=False`` 를 주입한다.
        - echo 는 기본 비활성화한다.
    """
    connect_args: dict[str, object] = {}
    if settings.suggestions_db_url.startswith("sqlite"):
        # 메인 DB 와 동일한 이유로 다중 스레드 접근을 허용한다.
        connect_args["check_same_thread"] = False

    return create_engine(
        settings.suggestions_db_url,
        echo=False,
        future=True,
        connect_args=connect_args,
    )


@lru_cache(maxsize=1)
def get_suggestions_engine() -> Engine:
    """건의사항 DB 엔진 싱글턴을 반환한다.

    프로세스 수명 동안 한 번만 생성된다. 테스트에서 DB URL 을 바꿔야 할 때는
    :func:`reset_suggestions_engine_cache` 와 ``get_settings.cache_clear()`` 를
    함께 호출한다.
    """
    settings = get_settings()
    # SQLite 파일의 부모 디렉터리가 존재하는지 보장한다.
    settings.ensure_runtime_paths()
    return _build_suggestions_engine(settings)


@lru_cache(maxsize=1)
def _get_suggestions_session_factory() -> sessionmaker[Session]:
    """``sessionmaker`` 팩토리를 싱글턴으로 반환한다."""
    return sessionmaker(
        bind=get_suggestions_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def SuggestionsSessionLocal() -> Session:  # noqa: N802 — FastAPI 컨벤션상 PascalCase 유지
    """건의사항 DB 의 새 ORM 세션을 생성해 반환한다.

    호출자가 ``close()`` 를 책임진다. 일반적으로는
    :func:`suggestions_session_scope` 컨텍스트 매니저 사용을 권장한다.
    """
    return _get_suggestions_session_factory()()


@contextmanager
def suggestions_session_scope() -> Iterator[Session]:
    """건의사항 DB 용 트랜잭션 경계 컨텍스트 매니저.

    - 블록 정상 종료 시 ``commit()``
    - 예외 발생 시 ``rollback()`` 후 재발행
    - 어느 경우든 ``close()`` 보장

    Example:
        with suggestions_session_scope() as session:
            session.add(suggestion)
    """
    session = SuggestionsSessionLocal()
    logger.debug("suggestions_session_scope open")
    try:
        yield session
        session.commit()
        logger.debug("suggestions_session_scope commit")
    except Exception:
        session.rollback()
        logger.debug("suggestions_session_scope rollback (예외 전파)")
        raise
    finally:
        session.close()
        logger.debug("suggestions_session_scope close")


def reset_suggestions_engine_cache() -> None:
    """엔진/세션팩토리 캐시를 모두 비운다.

    테스트 환경에서 DB URL 을 동적으로 바꾸거나 런타임 설정 리로드 후 새 엔진을
    쓰고자 할 때 사용한다.
    """
    _get_suggestions_session_factory.cache_clear()
    get_suggestions_engine.cache_clear()


def init_suggestions_db() -> Engine:
    """건의사항 DB 의 테이블을 멱등하게 보장한다.

    별도 DB 파일이라 메인 DB 의 Alembic 흐름과 분리되어 있다. 테이블 수가 적고
    스키마가 단순해 ``Base.metadata.create_all`` 로 충분하다 — 이미 존재하는
    테이블은 건드리지 않으므로 반복 호출에 안전하다.

    Returns:
        실제로 사용된 ``Engine`` 인스턴스.
    """
    engine = get_suggestions_engine()
    logger.info(
        "건의사항 DB 초기화 시작: dialect={dialect}",
        dialect=engine.dialect.name,
    )
    Base.metadata.create_all(engine)
    logger.info("건의사항 DB 초기화 완료")
    return engine


__all__ = [
    "get_suggestions_engine",
    "SuggestionsSessionLocal",
    "suggestions_session_scope",
    "reset_suggestions_engine_cache",
    "init_suggestions_db",
]
