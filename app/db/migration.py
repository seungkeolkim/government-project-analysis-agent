"""DB 스키마 마이그레이션 헬퍼.

Alembic 없이 컬럼 존재 여부를 확인하여 필요한 DDL 을 실행한다.
멱등성 보장: 이미 존재하는 컬럼/이름을 재생성하지 않는다.

`init_db.py` 의 `Base.metadata.create_all` 호출 **이전** 에 실행해야
기존 DB 를 새 스키마로 자동 업그레이드할 수 있다.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import Engine, inspect, text


def run_migrations(engine: Engine) -> None:
    """누락된 스키마 변경을 순서대로 적용한다.

    적용 순서:
        1. iris_announcement_id → source_announcement_id 컬럼 이름 변경
        2. source_type VARCHAR(32) NOT NULL DEFAULT 'IRIS' 컬럼 추가

    announcements 테이블이 존재하지 않으면 아무것도 하지 않는다
    (신규 DB 는 create_all 이 처리한다).

    Args:
        engine: 마이그레이션을 적용할 SQLAlchemy 엔진.
    """
    inspector = inspect(engine)
    if "announcements" not in inspector.get_table_names():
        # 신규 DB — create_all 이 최신 스키마로 생성한다.
        return

    with engine.connect() as conn:
        column_names = {col["name"] for col in inspector.get_columns("announcements")}

        # 1. iris_announcement_id → source_announcement_id
        if "iris_announcement_id" in column_names and "source_announcement_id" not in column_names:
            logger.info("마이그레이션: iris_announcement_id 컬럼을 source_announcement_id 로 이름 변경")
            conn.execute(
                text("ALTER TABLE announcements RENAME COLUMN iris_announcement_id TO source_announcement_id")
            )
            conn.commit()
            # 컬럼 목록 갱신
            column_names.discard("iris_announcement_id")
            column_names.add("source_announcement_id")
            logger.info("마이그레이션: source_announcement_id 이름 변경 완료")

        # 2. source_type 컬럼 추가
        if "source_type" not in column_names:
            logger.info("마이그레이션: source_type VARCHAR(32) 컬럼 추가 (DEFAULT 'IRIS')")
            conn.execute(
                text(
                    "ALTER TABLE announcements "
                    "ADD COLUMN source_type VARCHAR(32) NOT NULL DEFAULT 'IRIS'"
                )
            )
            conn.commit()
            logger.info("마이그레이션: source_type 컬럼 추가 완료")


__all__ = ["run_migrations"]
