"""SQLAlchemy ORM 모델 정의.

IRIS 사업공고(Announcement)와 첨부파일(Attachment)을 표현한다.
Alembic 은 도입하지 않으며, 초기 DDL 은 `init_db.py` 의 `create_all` 로 생성한다.

설계 메모:
    - 공고의 고유 식별자는 IRIS 에서 부여하는 `iris_announcement_id` 이며 UNIQUE 제약으로 보호한다.
    - 상태값은 '접수중/접수예정/마감' 세 가지만 취급한다(문자열 Enum).
    - 시간 컬럼은 모두 timezone-aware UTC 로 저장한다(`DateTime(timezone=True)`).
    - `raw_metadata` 는 IRIS 에서 수집한 임의의 부가 필드를 손실 없이 보존하기 위한 JSON 컬럼이다.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
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
    """IRIS 공고의 접수 상태.

    값은 한글 원문을 그대로 사용하여 화면 표시와 1:1 매칭되도록 한다.
    """

    RECEIVING = "접수중"
    SCHEDULED = "접수예정"
    CLOSED = "마감"


class Announcement(Base):
    """IRIS 사업공고 한 건을 나타내는 레코드.

    동일한 `iris_announcement_id` 가 들어오면 UPSERT(갱신) 대상이 되도록
    UNIQUE 제약을 건다. 화면 필터링과 마감 임박 정렬을 위해 `status`
    와 `deadline_at` 에도 인덱스를 둔다.
    """

    __tablename__ = "announcements"

    # 내부 PK (표시/정렬 용도)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # IRIS 에서 부여하는 공고 ID (외부 식별자, 중복 방지)
    iris_announcement_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
        index=True,
        doc="IRIS 시스템이 부여한 공고 고유 ID. UPSERT 키.",
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
            f"<Announcement id={self.id} iris_id={self.iris_announcement_id!r} "
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
