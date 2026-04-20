"""SQLAlchemy ORM 모델 정의.

사업공고(Announcement)와 첨부파일(Attachment)을 표현한다.
Alembic 은 도입하지 않으며, 초기 DDL 은 `init_db.py` 의 `create_all` 로 생성한다.
기존 DB 스키마 변경은 `app/db/migration.py` 의 `run_migrations` 가 처리한다.

설계 메모:
    - 증분 수집 이력 보존 모델:
      동일 (source_type, source_announcement_id) 에 대해 여러 row 가 존재할 수 있다.
      `is_current=True` 인 row 가 현재 유효한 최신 버전이며, `is_current=False` 인 row 는
      이전 버전(이력)이다. DB 레벨 UNIQUE 제약 대신 repository 계층이 앱 레벨에서
      "is_current=True row 는 (source_type, source_announcement_id) 당 최대 1개" 를 보장한다.

      이력 보존 동작:
        - 상태(status) 만 변경: is_current row 를 in-place UPDATE (status_transitioned)
        - 그 외 비교 필드(title/deadline_at/agency) 변경: 기존 row 를 is_current=False 로
          봉인하고 신규 row 를 INSERT (new_version). 이력이 row 단위로 누적된다.

    - 소스마다 독립된 ID 공간을 가지므로 (source_type, source_announcement_id) 복합
      인덱스를 조회 성능용으로 유지한다.
    - 상태값은 '접수중/접수예정/마감' 세 가지만 취급한다(문자열 Enum).
    - 시간 컬럼은 모두 timezone-aware UTC 로 저장한다(`DateTime(timezone=True)`).
    - `raw_metadata` 는 수집 소스에서 내려온 임의의 부가 필드를 손실 없이 보존한다.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


def _utcnow() -> datetime:
    """현재 시각을 timezone-aware UTC 로 반환한다.

    SQLAlchemy 의 default/onupdate 콜러블로 사용한다.
    """
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    """모든 ORM 모델이 상속하는 공통 선언적 베이스.

    JSON 컬럼은 SQLite/Postgres 양쪽에서 자연스럽게 동작하도록
    SQLAlchemy 의 범용 `JSON` 타입을 사용한다.
    """


class AnnouncementStatus(str, enum.Enum):
    """사업공고의 접수 상태.

    값은 한글 원문을 그대로 사용하여 화면 표시와 1:1 매칭되도록 한다.
    """

    RECEIVING = "접수중"
    SCHEDULED = "접수예정"
    CLOSED = "마감"


class Announcement(Base):
    """사업공고 한 건을 나타내는 레코드.

    동일 (source_type, source_announcement_id) 에 대해 이력이 다중 row 로 누적될 수 있다.
    `is_current=True` 인 row 가 현재 유효한 버전이다.
    화면 필터링과 마감 임박 정렬을 위해 `status`, `deadline_at`, `is_current` 에 인덱스를 둔다.
    """

    __tablename__ = "announcements"

    # (source_type, source_announcement_id) 복합 인덱스 — 조회 성능용 (UNIQUE 아님).
    # is_current=True row 의 유일성은 repository 계층(app-level)에서 보장한다.
    __table_args__ = (
        Index(
            "ix_announcement_source",
            "source_type",
            "source_announcement_id",
        ),
    )

    # 내부 PK (표시/정렬 용도)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 수집 소스가 부여하는 공고 ID (source_type 과 함께 UPSERT 키)
    source_announcement_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        doc="수집 소스가 부여한 공고 고유 ID. source_type 과 함께 UPSERT 키.",
    )

    # 공고 수집 소스 유형 (app.sources.constants.SOURCE_TYPE_* 중 하나)
    source_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="IRIS",
        doc="공고 수집 소스 유형. 예: 'IRIS', 'NTIS'.",
    )

    # 공고 제목
    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="공고 제목. 상세 페이지의 대표 텍스트.",
    )

    # 주관/공고 기관명 (일부 공고는 미표기일 수 있어 nullable)
    agency: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc="주관/공고 기관명. 없으면 NULL.",
    )

    # 접수 상태
    status: Mapped[AnnouncementStatus] = mapped_column(
        Enum(
            AnnouncementStatus,
            name="announcement_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
        ),
        nullable=False,
        index=True,
        doc="접수중 / 접수예정 / 마감 중 하나.",
    )

    # 접수 시작 시각 (알 수 없으면 NULL)
    received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="접수 시작 시각(UTC). 공고에 명시된 경우에만 채워진다.",
    )

    # 접수 마감 시각
    deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="접수 마감 시각(UTC). 정렬/임박 필터링용 인덱스 포함.",
    )

    # 상세 페이지 URL
    detail_url: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="공고 상세 페이지 URL.",
    )

    # ── 상세 페이지 수집 결과 (detail_scraper 가 채운다) ──────────────────────
    # DB 재생성 필요: 기존 SQLite 파일을 삭제 후 init_db 로 재생성.
    #   rm -f data/db/app.sqlite3 && python -m app.cli run

    # 공고 상세 영역(div.tstyle_view)의 원본 HTML. 없으면 NULL.
    detail_html: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="상세 페이지 본문 HTML(div.tstyle_view 섹션). detail_scraper 가 채운다.",
    )

    # 상세 HTML에서 추출한 정제 텍스트. 생략 가능하며 없으면 NULL.
    detail_text: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="detail_html 에서 BeautifulSoup 으로 추출한 가독성 텍스트.",
    )

    # 상세 수집 완료 시각 (UTC)
    detail_fetched_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="detail_scraper 가 상세 페이지를 수집 완료한 시각(UTC).",
    )

    # 상세 수집 결과 상태: 'ok' / 'empty' / 'error'
    detail_fetch_status: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        doc="상세 수집 결과 상태. 'ok': 본문 확인, 'empty': 본문 없음, 'error': 수집 실패.",
    )

    # IRIS 에서 수집한 원본 부가 메타데이터 (JSON)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        doc="파싱되지 않은 원본 메타 필드를 손실 없이 보존하기 위한 JSON 컬럼.",
    )

    # 스크래핑 시각 (최초 수집)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="이 공고를 최초로 수집한 시각.",
    )

    # 최종 갱신 시각 (UPSERT 시 onupdate)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="레코드가 마지막으로 갱신된 시각.",
    )

    # 현재 유효 버전 여부.
    # True: 현재 유효한 최신 row. False: 이력(구버전) row.
    # (source_type, source_announcement_id) 당 is_current=True row 는 최대 1개.
    # 유일성은 DB 제약 대신 repository 계층이 보장한다.
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        index=True,
        doc="현재 유효 버전 여부. False 이면 이력(구버전) 레코드.",
    )

    # 첨부파일과의 1:N 관계. 공고 삭제 시 첨부 레코드도 함께 삭제한다.
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment",
        back_populates="announcement",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<Announcement id={self.id} source={self.source_type!r} "
            f"src_id={self.source_announcement_id!r} "
            f"status={self.status.value!r} title={self.title[:20]!r}>"
        )


class Attachment(Base):
    """공고에 첨부된 단일 파일을 나타내는 레코드.

    실제 파일 바이너리는 로컬 파일시스템(`stored_path`)에 저장하고,
    DB 에는 파일을 식별·검증·표시하기 위한 메타정보만 둔다.
    동일 공고의 동일 파일 재수집 시 `sha256` 비교로 중복 판정한다.
    """

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 소속 공고 FK (공고 삭제 시 같이 제거)
    announcement_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("announcements.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="소속 공고의 내부 PK.",
    )

    # IRIS 상에 표기된 원본 파일명 (확장자 포함)
    original_filename: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        doc="IRIS 에 표기된 원본 파일명.",
    )

    # 로컬에 저장된 절대 또는 프로젝트 기준 경로
    stored_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="로컬 파일시스템에 저장된 경로.",
    )

    # 확장자 (pdf / hwp / hwpx / zip 등, 소문자 권장)
    file_ext: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="파일 확장자(소문자, 점 없이). 예: 'pdf', 'hwp', 'hwpx', 'zip'.",
    )

    # 바이트 단위 파일 크기 (알 수 없으면 NULL)
    file_size: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        doc="바이트 단위 파일 크기. 다운로드 실패/부분 저장 시 NULL.",
    )

    # 원본 다운로드 URL (POST 기반 다운로드는 NULL 가능)
    download_url: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="원본 다운로드 URL. IRIS 가 POST 다운로드만 제공하면 NULL.",
    )

    # 파일 전체의 SHA-256 해시 (hex 64자)
    sha256: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        doc="파일 전체의 SHA-256 해시(hex). 중복/변경 판정용.",
    )

    # 다운로드 완료 시각
    downloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="다운로드가 완료된 시각.",
    )

    # 역관계 (Announcement.attachments 와 대응)
    announcement: Mapped[Announcement] = relationship(
        "Announcement",
        back_populates="attachments",
    )

    __table_args__ = (
        # 동일 공고 안에서 동일 파일명이 2개 이상 들어오는 경우가 드물지 않으므로
        # 파일명만으로 UNIQUE 를 걸지 않는다. 대신 조회 성능을 위해 복합 인덱스만 둔다.
        Index("ix_attachments_announcement_filename", "announcement_id", "original_filename"),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<Attachment id={self.id} ann_id={self.announcement_id} "
            f"name={self.original_filename!r} ext={self.file_ext!r}>"
        )


__all__ = [
    "Base",
    "Announcement",
    "AnnouncementStatus",
    "Attachment",
]
