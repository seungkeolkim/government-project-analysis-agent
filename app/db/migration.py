"""DB 스키마 마이그레이션 헬퍼.

.. deprecated::
    Phase 0 (task 00017) 에서 Alembic 을 도입함으로써 이 함수는 더 이상
    `init_db.py` 에서 호출되지 않는다.
    이후 모든 스키마 변경은 `alembic/versions/` 아래 migration 파일로 관리한다.
    이 모듈은 히스토리 보존 목적으로 삭제하지 않는다.

원래 역할 (참고):
    Alembic 없이 컬럼 존재 여부를 확인하여 필요한 DDL 을 실행했다.
    6단계의 수작업 DDL 은 현재 운영 DB 에 모두 적용 완료된 상태이며,
    그 최종 스키마가 `alembic/versions/*_baseline_initial_schema.py` 에 캡처되어 있다.

적용 이력 (완료):
    1. iris_announcement_id → source_announcement_id 컬럼 이름 변경
    2. source_type VARCHAR(32) NOT NULL DEFAULT 'IRIS' 컬럼 추가
    3. is_current BOOLEAN NOT NULL DEFAULT 1 컬럼 추가 + 기존 row 초기화
    4. uq_announcement_source UNIQUE 인덱스 제거
    5. canonical_projects 테이블 생성 (00013 canonical identity 레이어)
    6. announcements.canonical_group_id / canonical_key / canonical_key_scheme 컬럼 추가
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import Engine, inspect, text


def run_migrations(engine: Engine) -> None:
    """누락된 스키마 변경을 순서대로 적용한다.

    모듈 docstring의 '적용 순서'를 참조한다.
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
                text("ALTER TABLE announcements ADD COLUMN source_type VARCHAR(32) NOT NULL DEFAULT 'IRIS'")
            )
            conn.commit()
            column_names.add("source_type")
            logger.info("마이그레이션: source_type 컬럼 추가 완료")

        # 3. is_current BOOLEAN 컬럼 추가
        #    기존 row 는 모두 현재 유효 버전이므로 is_current=1 로 초기화한다.
        if "is_current" not in column_names:
            logger.info("마이그레이션: is_current BOOLEAN NOT NULL 컬럼 추가")
            conn.execute(text("ALTER TABLE announcements ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT 1"))
            # ADD COLUMN ... DEFAULT 1 이 기존 row 를 자동으로 1 로 채우지만,
            # SQLite 버전에 따라 다를 수 있으므로 명시적으로 UPDATE 를 수행한다.
            conn.execute(text("UPDATE announcements SET is_current = 1 WHERE is_current IS NULL"))
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

        # 5. canonical_projects 테이블 생성 (00013 canonical identity 레이어)
        #    inspector 는 위에서 생성했으므로 테이블 목록을 새로 조회한다.
        if "canonical_projects" not in inspect(engine).get_table_names():
            logger.info("마이그레이션: canonical_projects 테이블 생성")
            conn.execute(
                text(
                    """
                    CREATE TABLE canonical_projects (
                        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                        canonical_key        VARCHAR(256) NOT NULL UNIQUE,
                        key_scheme           VARCHAR(16)  NOT NULL,
                        representative_title TEXT,
                        representative_agency VARCHAR(255),
                        created_at           DATETIME     NOT NULL,
                        updated_at           DATETIME     NOT NULL
                    )
                    """
                )
            )
            conn.commit()
            logger.info("마이그레이션: canonical_projects 테이블 생성 완료")

        # 6. announcements: canonical 관련 컬럼 3개 추가
        #    inspector 캐시를 피하기 위해 column_names 를 갱신한다.
        ann_columns = {col["name"] for col in inspect(engine).get_columns("announcements")}

        if "canonical_group_id" not in ann_columns:
            logger.info("마이그레이션: announcements.canonical_group_id INTEGER 컬럼 추가")
            conn.execute(text("ALTER TABLE announcements ADD COLUMN canonical_group_id INTEGER"))
            conn.commit()
            ann_columns.add("canonical_group_id")
            logger.info("마이그레이션: canonical_group_id 컬럼 추가 완료")

        if "canonical_key" not in ann_columns:
            logger.info("마이그레이션: announcements.canonical_key VARCHAR(256) 컬럼 추가")
            conn.execute(text("ALTER TABLE announcements ADD COLUMN canonical_key VARCHAR(256)"))
            conn.commit()
            ann_columns.add("canonical_key")
            logger.info("마이그레이션: canonical_key 컬럼 추가 완료")

        if "canonical_key_scheme" not in ann_columns:
            logger.info("마이그레이션: announcements.canonical_key_scheme VARCHAR(16) 컬럼 추가")
            conn.execute(text("ALTER TABLE announcements ADD COLUMN canonical_key_scheme VARCHAR(16)"))
            conn.commit()
            logger.info("마이그레이션: canonical_key_scheme 컬럼 추가 완료")


__all__ = ["run_migrations"]
