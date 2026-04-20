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
        3. is_current BOOLEAN NOT NULL DEFAULT 1 컬럼 추가 + 기존 row 초기화
        4. uq_announcement_source UNIQUE 인덱스 제거
           (이력 보존 모델 전환 — is_current=True 유일성을 앱 레벨로 이관)

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
            column_names.add("source_type")
            logger.info("마이그레이션: source_type 컬럼 추가 완료")

        # 3. is_current BOOLEAN 컬럼 추가
        #    기존 row 는 모두 현재 유효 버전이므로 is_current=1 로 초기화한다.
        if "is_current" not in column_names:
            logger.info("마이그레이션: is_current BOOLEAN NOT NULL 컬럼 추가")
            conn.execute(
                text(
                    "ALTER TABLE announcements "
                    "ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT 1"
                )
            )
            # ADD COLUMN ... DEFAULT 1 이 기존 row 를 자동으로 1 로 채우지만,
            # SQLite 버전에 따라 다를 수 있으므로 명시적으로 UPDATE 를 수행한다.
            conn.execute(
                text("UPDATE announcements SET is_current = 1 WHERE is_current IS NULL")
            )
            conn.commit()
            column_names.add("is_current")
            logger.info("마이그레이션: is_current 컬럼 추가 및 기존 row 초기화 완료")

        # 4. uq_announcement_source UNIQUE 인덱스 제거
        #    이력 보존 모델 전환에 따라 동일 (source_type, source_announcement_id) 에
        #    여러 row(이력)가 존재할 수 있으므로 UNIQUE 제약을 제거한다.
        #    유일성(is_current=True row 당 1개)은 repository 계층에서 앱 레벨로 보장한다.
        #
        #    SQLite 는 ALTER TABLE DROP CONSTRAINT 를 지원하지 않으므로
        #    인덱스 이름으로 직접 DROP INDEX 를 실행한다.
        existing_index_names = {idx["name"] for idx in inspector.get_indexes("announcements")}
        if "uq_announcement_source" in existing_index_names:
            logger.info("마이그레이션: uq_announcement_source UNIQUE 인덱스 제거 (이력 보존 전환)")
            conn.execute(text("DROP INDEX IF EXISTS uq_announcement_source"))
            conn.commit()
            logger.info("마이그레이션: uq_announcement_source 인덱스 제거 완료")


__all__ = ["run_migrations"]
