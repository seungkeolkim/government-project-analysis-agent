"""SQLAlchemy ORM 모델 정의.

사업공고(Announcement)와 첨부파일(Attachment) 외에 Phase 1a(팀 공용 전환)에서
사용자 라벨링과 리셋 트랜잭션에 필요한 최소 모델(User / AnnouncementUserState /
RelevanceJudgment / RelevanceJudgmentHistory / FavoriteFolder)을 함께 정의한다.

스키마 변경은 Alembic migration(`alembic/versions/`) 이 전담한다.
`init_db.py` 는 Alembic Python API 로 stamp/upgrade 를 분기 실행한다.

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
    - Phase 1a 신규 모델 5종: 리셋·이관·validator 에 필요한 최소 범위만 ORM 으로
      선언한다. 나머지 8개 신규 테이블(user_sessions, favorite_entries,
      canonical_overrides, email_subscriptions, admin_email_targets, audit_logs,
      scrape_runs, attachment_analyses)은 migration 으로만 만들고 ORM 화는
      해당 기능 Phase 에서 처리한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)


def _utcnow() -> datetime:
    """현재 시각을 timezone-aware UTC 로 반환한다.

    SQLAlchemy 의 default/onupdate 콜러블로 사용한다.
    """
    return datetime.now(tz=UTC)


def as_utc(value: datetime) -> datetime:
    """naive ``datetime`` 을 UTC tz-aware 로, 이미 tz-aware 라면 그대로 반환한다.

    SQLAlchemy ``DateTime(timezone=True)`` 는 SQLite 백엔드에서 timezone 정보를
    저장하지 못해, INSERT 시 tz-aware 로 넣어도 SELECT 시점에는 naive
    ``datetime`` 으로 되돌아온다. 이 상태에서 ``datetime.now(tz=UTC)`` 같은
    tz-aware 시각과 직접 비교하면 ``TypeError: can't compare offset-naive and
    offset-aware datetimes`` 가 발생한다.

    본 헬퍼는 비교 직전에 양쪽 값을 UTC tz-aware 로 정규화하는 데 사용한다.
    저장된 값이 UTC 라는 프로젝트 컨벤션에 의존하므로, naive 값은 UTC tz 를
    부여해도 의미가 보존된다.

    원래 ``app.auth.service._as_utc`` 로 존재하던 private 헬퍼를 Phase 2 에서
    scrape_runs 비교 로직에서도 재사용하기 위해 공용 모듈로 승격했다.

    Args:
        value: tz-aware 또는 naive ``datetime``.

    Returns:
        tz-aware UTC ``datetime``.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class Base(DeclarativeBase):
    """모든 ORM 모델이 상속하는 공통 선언적 베이스.

    JSON 컬럼은 SQLite/Postgres 양쪽에서 자연스럽게 동작하도록
    SQLAlchemy 의 범용 `JSON` 타입을 사용한다.
    """


class AnnouncementStatus(StrEnum):
    """사업공고의 접수 상태.

    값은 한글 원문을 그대로 사용하여 화면 표시와 1:1 매칭되도록 한다.
    """

    RECEIVING = "접수중"
    SCHEDULED = "접수예정"
    CLOSED = "마감"


class CanonicalProject(Base):
    """canonical 그룹 엔티티.

    동일 과제가 여러 포털(IRIS·NTIS) 또는 같은 포털 내 재등록으로 복수의
    Announcement row 로 수집될 때 이를 하나의 논리적 "과제"로 묶는 그룹 레코드.

    canonical_key 는 UNIQUE 제약을 가지며, 공고번호(official scheme) 또는
    제목·기관·마감연도 조합(fuzzy scheme) 중 하나로 구성된다.
    구체적인 키 생성 로직은 `app.canonical.compute_canonical_key` 참조.

    관계:
        announcements: 이 그룹에 속한 Announcement row 목록 (1:N).
    """

    __tablename__ = "canonical_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 정규화된 canonical key — source 무관하게 유일. UNIQUE 인덱스.
    canonical_key: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        unique=True,
        doc="정규화된 canonical key. 예: 'official:과학기술정보통신부공고제2026-0455호'",
    )

    # 키 생성 방식: 'official' 또는 'fuzzy'
    key_scheme: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="'official': 공고번호 기반, 'fuzzy': 제목·기관·연도 조합 fallback.",
    )

    # 대표 공고명 (최초 수집 시 저장)
    representative_title: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="최초 수집된 공고 제목. 그룹의 대표 표시 문자열.",
    )

    # 대표 주관기관명 (최초 수집 시 저장)
    representative_agency: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="최초 수집된 주관기관명.",
    )

    # 최초 생성 시각
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="canonical group 이 최초로 생성된 시각(UTC).",
    )

    # 최종 갱신 시각
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="레코드가 마지막으로 갱신된 시각(UTC).",
    )

    # 역관계: 이 그룹에 속한 Announcement row 목록
    announcements: Mapped[list[Announcement]] = relationship(
        "Announcement",
        back_populates="canonical_project",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return f"<CanonicalProject id={self.id} scheme={self.key_scheme!r} key={self.canonical_key[:40]!r}>"


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
    agency: Mapped[str | None] = mapped_column(
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
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="접수 시작 시각(UTC). 공고에 명시된 경우에만 채워진다.",
    )

    # 접수 마감 시각
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="접수 마감 시각(UTC). 정렬/임박 필터링용 인덱스 포함.",
    )

    # 상세 페이지 URL
    detail_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="공고 상세 페이지 URL.",
    )

    # ── 상세 페이지 수집 결과 (detail_scraper 가 채운다) ──────────────────────
    # DB 재생성 필요: 기존 SQLite 파일을 삭제 후 init_db 로 재생성.
    #   rm -f data/db/app.sqlite3 && python -m app.cli run

    # 공고 상세 영역(div.tstyle_view)의 원본 HTML. 없으면 NULL.
    detail_html: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="상세 페이지 본문 HTML(div.tstyle_view 섹션). detail_scraper 가 채운다.",
    )

    # 상세 HTML에서 추출한 정제 텍스트. 생략 가능하며 없으면 NULL.
    detail_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="detail_html 에서 BeautifulSoup 으로 추출한 가독성 텍스트.",
    )

    # 상세 수집 완료 시각 (UTC)
    detail_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="detail_scraper 가 상세 페이지를 수집 완료한 시각(UTC).",
    )

    # 상세 수집 결과 상태: 'ok' / 'empty' / 'error'
    detail_fetch_status: Mapped[str | None] = mapped_column(
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

    # ── canonical identity 레이어 ────────────────────────────────────────────
    # canonical_group_id: 이 공고가 속한 CanonicalProject 의 PK.
    #   NULL 허용 — 아직 canonical 매칭이 완료되지 않은 레코드(backfill 전 기존 데이터).
    #   이력(is_current=False) row 도 동일 canonical_group_id 를 보유한다.
    #   new_version 분기에서 신규 row 생성 시 이전 row 의 canonical_group_id 를 승계하는
    #   로직은 repository 계층(00013-5)에서 구현한다.
    canonical_group_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("canonical_projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="소속 CanonicalProject PK. NULL 이면 아직 canonical 매칭 미완료.",
    )

    # canonical_key: CanonicalProject.canonical_key 의 비정규화 복사본.
    #   JOIN 없이 canonical_key 로 직접 조회할 때 사용한다.
    canonical_key: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
        index=True,
        doc="canonical_group 의 정규화 키(비정규화). JOIN 없는 조회용.",
    )

    # canonical_key_scheme: 'official' 또는 'fuzzy'.
    canonical_key_scheme: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        doc="'official' 또는 'fuzzy'. canonical_key 생성 방식.",
    )

    # canonical_project 로의 N:1 관계
    canonical_project: Mapped[CanonicalProject | None] = relationship(
        "CanonicalProject",
        back_populates="announcements",
        lazy="selectin",
    )

    # 첨부파일과의 1:N 관계. 공고 삭제 시 첨부 레코드도 함께 삭제한다.
    attachments: Mapped[list[Attachment]] = relationship(
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
    file_size: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        doc="바이트 단위 파일 크기. 다운로드 실패/부분 저장 시 NULL.",
    )

    # 원본 다운로드 URL (POST 기반 다운로드는 NULL 가능)
    download_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="원본 다운로드 URL. IRIS 가 POST 다운로드만 제공하면 NULL.",
    )

    # 파일 전체의 SHA-256 해시 (hex 64자)
    sha256: Mapped[str | None] = mapped_column(
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


class User(Base):
    """팀 구성원 계정.

    Phase 1a 시점에는 실제 로그인 플로우가 아직 없지만, 리셋 트랜잭션에서
    `announcement_user_states.user_id` 및 `relevance_judgments.user_id` 가
    참조할 FK target 이 필요하므로 ORM 모델을 미리 정의한다.

    관계:
        announcement_states: 이 사용자의 공고별 읽음 상태 목록.
        relevance_judgments: 이 사용자의 현재 유효한 관련성 판정 목록.
        favorite_folders: 이 사용자가 만든 즐겨찾기 폴더 목록.
        user_organizations: 이 사용자가 속한 조직 매핑 목록.

    보안 메모:
        password_hash 는 해시 결과 문자열만 저장한다. 해싱 알고리즘(bcrypt 또는
        argon2) 선택은 Phase 1b 에서 확정한다. 본 모델은 컬럼 자리만 예약한다.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 로그인 ID — 소문자 ASCII + 숫자 + _ 만 허용할 예정 (Phase 1b validator).
    username: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        doc="로그인 ID. 전역 UNIQUE.",
    )

    # 해시 문자열. 알고리즘은 Phase 1b 에서 결정.
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="비밀번호 해시 결과 문자열. 알고리즘은 Phase 1b 결정(bcrypt/argon2).",
    )

    # 이메일 알림 수신 주소 — 없어도 계정 사용 가능하므로 nullable.
    email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        doc="이메일 알림 수신 주소. NULL 이면 이메일 알림 대상에서 제외.",
    )

    # 이메일 수신 동의 여부 — True 이면 알림 수신 대상, False 이면 수신 거부.
    email_subscribed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        doc="이메일 수신 동의 여부. False 이면 email 이 있어도 발송 대상에서 제외.",
    )

    # 관리자 권한 플래그 — Phase 2 관리자 화면에서 게이트로 사용.
    is_admin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc="관리자 권한 여부.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="계정 생성 시각(UTC).",
    )

    # 역관계 — 개별 리셋 로직이 user_id 기준으로 조회할 때 사용한다.
    announcement_states: Mapped[list[AnnouncementUserState]] = relationship(
        "AnnouncementUserState",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    relevance_judgments: Mapped[list[RelevanceJudgment]] = relationship(
        "RelevanceJudgment",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    favorite_folders: Mapped[list[FavoriteFolder]] = relationship(
        "FavoriteFolder",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    # Phase 1b 로 추가된 역관계. 사용자 삭제 시 세션도 함께 제거된다 (DB FK 가 CASCADE).
    # ORM 쪽에서도 cascade="all, delete-orphan" 을 두어 ORM 플러시 경로에서 일관성 유지.
    sessions: Mapped[list[UserSession]] = relationship(
        "UserSession",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    # task 00049: 사용자-조직 M:N 매핑. 사용자 삭제 시 매핑도 함께 제거.
    user_organizations: Mapped[list[UserOrganization]] = relationship(
        "UserOrganization",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return f"<User id={self.id} username={self.username!r} is_admin={self.is_admin}>"


class UserSession(Base):
    """로그인 세션 한 건.

    사용자가 로그인하면 서버가 `secrets.token_urlsafe(32)` 로 불투명 세션 ID 를
    발급해 쿠키로 내려주고, 같은 문자열을 이 테이블의 PK 로 저장한다.
    쿠키를 갖고 돌아온 요청은 `session_id` 로 이 row 를 조회해 현재 사용자를
    복원한다.

    설계 메모:
        - `session_id` 는 평문으로 저장한다. 로컬 전제 — DB 파일 자체가 신뢰
          경계 안이므로, 쿠키와 DB 값의 신뢰 수준이 동일하다. (사용자 원문:
          "세션 ID: secrets.token_urlsafe(32). DB 평문 (로컬 전제)".)
        - `expires_at` 이 현재 시각 이하이면 해당 세션은 만료된 것이다.
          만료된 세션은 즉시 삭제하지 않고 조회 시점에 로그인 실패로 취급한다.
          적극적 cleanup 은 Phase 2 의 스케줄러가 담당한다.
        - FK 는 `users.id` 로 CASCADE — 사용자를 삭제하면 세션도 자동 제거.
        - 테이블 DDL 은 Phase 1a migration
          (`20260422_1500_b2c5e8f1a934_phase1a_new_tables.py`) 에서 이미 생성되어
          있으므로 본 ORM 선언의 컬럼/인덱스/FK 이름은 DDL 과 **정확히 일치**해야
          한다. autogenerate diff 가 비어야 하는 것이 검증 기준.

    관계:
        user: 세션 소유자 사용자.
    """

    __tablename__ = "user_sessions"

    # 쿠키로 전달되는 불투명 토큰. Phase 1a DDL 에서 String(64) 로 정의되었다.
    session_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        doc="세션 식별자. secrets.token_urlsafe(32) 결과(≈43자).",
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_user_sessions_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="세션 소유자 사용자 PK.",
    )

    # 만료 시각(UTC). 이 값 이하의 시각에 도착한 요청은 로그인 실패로 처리한다.
    # 기본 유효기간은 app.auth.constants.SESSION_LIFETIME_DAYS (30일) 로,
    # 세션 발급 시점에 서비스 계층이 계산해 넣는다.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        doc="세션 만료 시각(UTC). 현재 시각 이하이면 만료.",
    )

    # 세션 발급 시각(UTC). 디버깅/감사용.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="세션이 발급된 시각(UTC).",
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="sessions",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        # session_id 는 토큰이므로 앞 8자만 보여준다 (보안 위험 최소화 + 식별).
        preview = self.session_id[:8] if self.session_id else ""
        return (
            f"<UserSession id={preview!r}... user_id={self.user_id} "
            f"expires_at={self.expires_at.isoformat() if self.expires_at else None!r}>"
        )


class AnnouncementUserState(Base):
    """공고 × 사용자 단위 상태(현재는 "읽음" 하나).

    내용 변경(status 단독 제외)이 감지되면 해당 announcement_id 의 모든 row 가
    `is_read=False`, `read_at=NULL` 로 리셋된다. 이 테이블은 UPSERT 와 동일한
    트랜잭션에서 atomic 하게 갱신되어야 한다(repository 계층 책임).

    UNIQUE: (announcement_id, user_id) 1건만 존재.
    """

    __tablename__ = "announcement_user_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    announcement_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "announcements.id",
            name="fk_announcement_user_states_announcement_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        doc="대상 공고 PK.",
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_announcement_user_states_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="상태 소유자 사용자 PK.",
    )

    is_read: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc="읽음 여부. 내용 변경 시 False 로 리셋된다.",
    )

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="마지막으로 읽은 시각(UTC). 읽지 않은 동안은 NULL.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="레코드 마지막 갱신 시각.",
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="announcement_states",
    )

    __table_args__ = (
        UniqueConstraint(
            "announcement_id",
            "user_id",
            name="uq_announcement_user_states_ann_user",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<AnnouncementUserState id={self.id} ann_id={self.announcement_id} "
            f"user_id={self.user_id} is_read={self.is_read}>"
        )


class RelevanceJudgment(Base):
    """canonical_project × user × organization 단위 현재 유효 판정.

    UNIQUE 키 (canonical_project_id, user_id, organization_id). organization_id 는
    "이 사용자가 어떤 조직 입장으로 한 판정인지" 를 나타내는 메타 정보로 NOT NULL 이다
    (task 00093 에서 개인 판정 슬롯 제거 — organization_id IS NULL row 를 삭제하고
    NOT NULL 제약을 적용했다).

    같은 사용자가 같은 canonical 에 대해 본인이 소속된 각 조직마다 row 1 개씩 가질 수
    있다 (UNIQUE 키 조합이 다름). 같은 조직 안의 다른 멤버가 만든 row 도 user_id 가
    달라 본 row 와 독립적이다.

    내용 변경(status 단독 제외) 감지 시 해당 canonical 의 모든 row 는
    `relevance_judgment_history` 로 이관되어 이 테이블에서 제거된다 (organization_id
    값을 그대로 복사하여 이력 보존).

    verdict 허용값: '관련', '무관' — DB CHECK 및 app-level 상수로 이중 강제.
    """

    __tablename__ = "relevance_judgments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    canonical_project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "canonical_projects.id",
            name="fk_relevance_judgments_canonical_project_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        doc="판정 대상 canonical_project PK.",
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_relevance_judgments_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="판정한 사용자 PK.",
    )

    # organization_id: 판정 주체 조직 PK. task 00093 에서 개인 판정 슬롯(IS NULL)을
    # 제거하고 NOT NULL 로 변경했다. 조직 삭제 시 그 조직 입장의 판정도 CASCADE 삭제.
    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_relevance_judgments_organization_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="판정 주체 조직 PK. 조직 판정만 허용 (NOT NULL).",
    )

    # verdict: '관련' | '무관'. 한글 2글자 + 여유로 String(8).
    verdict: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        doc="판정 결과. 허용값: '관련', '무관'.",
    )

    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="판정 이유(짧은 메모). 없으면 NULL.",
    )

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="판정 시각(UTC).",
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="relevance_judgments",
    )

    __table_args__ = (
        # task 00085 에서 (canonical, user) → (canonical, user, organization_id) 단일
        # UNIQUE 로 교체. partial unique index 는 사용하지 않는다 (SQLite·Postgres
        # 양쪽 호환성 단순화).
        UniqueConstraint(
            "canonical_project_id",
            "user_id",
            "organization_id",
            name="uq_relevance_judgments_canonical_user_org",
        ),
        CheckConstraint(
            "verdict IN ('관련', '무관')",
            name="ck_relevance_verdict",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<RelevanceJudgment id={self.id} "
            f"canonical={self.canonical_project_id} user_id={self.user_id} "
            f"organization_id={self.organization_id} "
            f"verdict={self.verdict!r}>"
        )


class RelevanceJudgmentHistory(Base):
    """이관된 과거 판정.

    새 판정/내용 변경 시 `relevance_judgments` 의 row 를 이 테이블로 복사한다.
    이 테이블은 append-only 로 취급하며, 이관된 레코드는 수정하지 않는다.

    organization_id 는 이관 시점의 RelevanceJudgment.organization_id 값을 그대로
    복사한다. task 00093 에서 개인 판정 슬롯 이력(IS NULL)을 삭제하고 NOT NULL 로
    변경했다.

    archive_reason 허용값(app-level 상수): 'content_changed', 'user_overwrite',
    'admin_override'. DB CHECK 는 유연성 위해 설치하지 않는다.
    """

    __tablename__ = "relevance_judgment_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    canonical_project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "canonical_projects.id",
            name="fk_relevance_judgment_history_canonical_project_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        doc="이관 시점의 canonical_project PK.",
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_relevance_judgment_history_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        doc="이관 시점의 사용자 PK.",
    )

    # organization_id: 이관 시점의 RelevanceJudgment.organization_id 값을 그대로 복사.
    # task 00093 에서 개인 판정 이력(IS NULL) 삭제 후 NOT NULL 제약 적용.
    # 조직 삭제 시 CASCADE 로 그 조직 입장의 history row 도 함께 사라진다.
    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_relevance_judgment_history_organization_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="이관 시점의 판정 주체 조직 PK (NOT NULL).",
    )

    verdict: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        doc="이관 시점의 판정 결과 값.",
    )

    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="이관 시점의 판정 이유. 원본 그대로 복사.",
    )

    # decided_at 은 원본 판정 시각 — 이관 시 덮어쓰지 않는다.
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="원본 판정 시각. 이관 시 덮어쓰지 않는다.",
    )

    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="이관 시각(UTC).",
    )

    archive_reason: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        doc="이관 사유. 'content_changed' / 'user_overwrite' / 'admin_override'.",
    )

    __table_args__ = (
        Index(
            "ix_relevance_judgment_history_canonical_user",
            "canonical_project_id",
            "user_id",
        ),
        Index(
            "ix_relevance_judgment_history_archived_at",
            "archived_at",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<RelevanceJudgmentHistory id={self.id} "
            f"canonical={self.canonical_project_id} user_id={self.user_id} "
            f"verdict={self.verdict!r} archive_reason={self.archive_reason!r}>"
        )


class FavoriteFolder(Base):
    """즐겨찾기 폴더. 최대 depth 2 (루트 + 1단 하위만 허용).

    depth 제약은 DB CHECK 대신 ORM @validates 로 강제한다(사용자 원문: "폴더
    depth 2 는 ORM validator"). parent 삭제 시 자식은 SET NULL 로 살아남는다.

    NOTE (루트 동명 허용 문제):
        UNIQUE (user_id, parent_id, name) 는 parent_id IS NULL 인 루트 폴더끼리
        동명을 허용한다(SQLite/Postgres 모두 NULL 을 "서로 다름"으로 취급).
        루트 동명 금지는 Phase 1b 에서 repository 계층에 app-check 로 보강한다.
    """

    __tablename__ = "favorite_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_favorite_folders_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="폴더 소유자 사용자 PK.",
    )

    parent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "favorite_folders.id",
            name="fk_favorite_folders_parent_id",
            ondelete="CASCADE",
        ),
        nullable=True,
        index=True,
        doc=(
            "부모 폴더 PK. NULL 이면 루트(depth=0). task 00037 부터 ondelete=CASCADE — "
            "부모 폴더가 삭제되면 자식 폴더(그리고 그 하위 FavoriteEntry) 도 DB "
            "레벨에서 연쇄 삭제된다 (이전의 SET NULL \"격상\" 동작 폐기)."
        ),
    )

    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        doc="폴더명.",
    )

    # depth: 0(루트) 또는 1(루트의 자식). validator 가 parent_id 기준으로 강제.
    depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        doc="폴더 깊이. 0 = 루트, 1 = 루트의 자식.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="생성 시각(UTC).",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="레코드 마지막 갱신 시각.",
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="favorite_folders",
    )

    # self-reference 관계: 부모 폴더 / 자식 폴더 목록.
    parent: Mapped[FavoriteFolder | None] = relationship(
        "FavoriteFolder",
        remote_side="FavoriteFolder.id",
        back_populates="children",
        foreign_keys=lambda: [FavoriteFolder.parent_id],
    )
    # task 00037 — 부모 폴더 삭제 시 자식 폴더도 ORM 레벨로 cascade 삭제한다.
    # FK ondelete=CASCADE 와 의미를 맞추되 SQLite 의 PRAGMA foreign_keys 설정
    # 여부와 무관하게 ORM 경로(session.delete) 로도 재귀 삭제가 보장되게 한다.
    # delete-orphan 은 쓰지 않는다 — 자식을 루트로 '이동'(parent_id=None) 하는
    # 정당한 UX 가 존재하므로 detach 만으로 삭제하면 안 되기 때문이다.
    children: Mapped[list[FavoriteFolder]] = relationship(
        "FavoriteFolder",
        back_populates="parent",
        foreign_keys=lambda: [FavoriteFolder.parent_id],
        cascade="all, delete",
    )
    entries: Mapped[list[FavoriteEntry]] = relationship(
        "FavoriteEntry",
        back_populates="folder",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "parent_id",
            "name",
            name="uq_favorite_folders_user_parent_name",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<FavoriteFolder id={self.id} user_id={self.user_id} "
            f"name={self.name!r} depth={self.depth} parent_id={self.parent_id}>"
        )


def _enforce_favorite_folder_depth(session: Session, target: FavoriteFolder) -> None:
    """FavoriteFolder insert/update 시 depth 2 계층을 강제한다.

    규칙:
        - parent_id 가 None → 루트(depth=0).
        - parent_id 가 주어지면 부모가 존재해야 하고, 그 부모의 `parent_id` 는
          None 이어야 한다(즉 부모는 루트). 이를 어기면 depth 2 초과가 되어
          ValueError.
        - 자기 자신을 부모로 지정 금지.

    target.depth 는 여기서 함께 계산하여 parent_id 와의 일관성을 보장한다.
    DB CHECK 가 없으므로 이 listener 가 유일한 방어선이다.

    session 이 flush 중이므로 세션 쿼리로 부모를 조회할 수 있다 —
    `@validates` 는 생성자 호출 시점에 돌아 session 이 없을 수 있어 불완전했다.
    """
    if target.parent_id is None:
        target.depth = 0
        return

    # 자기 자신을 부모로 지정 금지 (PK 가 이미 있을 때만 검사 가능).
    if target.id is not None and target.parent_id == target.id:
        raise ValueError("즐겨찾기 폴더는 자기 자신을 부모로 지정할 수 없습니다.")

    # 부모 조회 — flush 컨텍스트 안이므로 pending 객체도 인식한다.
    parent_row = session.get(FavoriteFolder, target.parent_id)
    if parent_row is None:
        raise ValueError(
            f"부모 폴더를 찾을 수 없습니다: parent_id={target.parent_id}"
        )

    if parent_row.parent_id is not None:
        # 부모가 이미 누군가의 자식 → depth 2 초과 → 거부.
        raise ValueError(
            "즐겨찾기 폴더는 최대 2단까지만 허용됩니다 "
            "(루트 또는 루트의 자식만 가능)."
        )

    target.depth = 1


@event.listens_for(FavoriteFolder, "before_insert")
def _favorite_folder_before_insert(
    _mapper: Any, connection: Any, target: FavoriteFolder
) -> None:
    """INSERT 직전 depth 제약 검사."""
    session = Session.object_session(target)
    if session is None:
        # session 없이 flush 경로에 진입하는 일은 없어야 함 — 방어적 체크.
        return
    _enforce_favorite_folder_depth(session, target)


@event.listens_for(FavoriteFolder, "before_update")
def _favorite_folder_before_update(
    _mapper: Any, connection: Any, target: FavoriteFolder
) -> None:
    """UPDATE(parent_id 변경 포함) 직전 depth 제약 검사."""
    session = Session.object_session(target)
    if session is None:
        return
    _enforce_favorite_folder_depth(session, target)


# ──────────────────────────────────────────────────────────────
# FavoriteEntry (Phase 3b / 00036 — 즐겨찾기 항목)
# ──────────────────────────────────────────────────────────────


class FavoriteEntry(Base):
    """즐겨찾기 항목. task 00037 부터 announcement 단위로 저장한다.

    00036 에서 확정되었던 canonical 단위 저장 설계는 task 00037 에서 공식 폐기되었다.
    사용자 원문 요구 — "별표를 누른 그 공고가 반드시 등록됨 / 동일 과제 여러 공고가
    즐겨찾기에 모두 보여야 함" — 를 충족하기 위해 FK 를 ``announcement_id`` 로 전환
    하고, "동일 과제 모두 저장" 은 라디오 버튼으로 canonical_group 의 모든
    is_current announcement 을 일괄 등록하여 구현한다.

    스키마 변경은 ``20260424_0900_c4a8d1e7b2f3_favorites_announcement_unit_cascade.py``
    migration 에서 수행되었다. 이 클래스는 그 신규 스키마에 ORM 을 얹는다.

    UNIQUE(folder_id, announcement_id) 로 동일 폴더에 같은 공고 중복 등록 금지.
    폴더 삭제(folder_id FK CASCADE) 또는 announcement 삭제(announcement_id FK CASCADE)
    시 항목도 연쇄 삭제된다.
    """

    __tablename__ = "favorite_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    folder_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "favorite_folders.id",
            name="fk_favorite_entries_folder_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        doc="소속 폴더 PK.",
    )

    announcement_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "announcements.id",
            name="fk_favorite_entries_announcement_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="즐겨찾기한 공고 PK. announcements.id 를 직접 가리킨다.",
    )

    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="즐겨찾기 추가 시각(UTC).",
    )

    folder: Mapped[FavoriteFolder] = relationship(
        "FavoriteFolder",
        back_populates="entries",
    )
    announcement: Mapped[Announcement] = relationship("Announcement")

    __table_args__ = (
        UniqueConstraint(
            "folder_id",
            "announcement_id",
            name="uq_favorite_entries_folder_announcement",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<FavoriteEntry id={self.id} folder_id={self.folder_id} "
            f"announcement_id={self.announcement_id}>"
        )


# ──────────────────────────────────────────────────────────────
# ScrapeRun (Phase 2 / 00025 — 수집 실행 1회 요약)
# ──────────────────────────────────────────────────────────────

# ScrapeRun.status / ScrapeRun.trigger 의 허용값.
# DB CHECK 제약(ck_scrape_runs_status / ck_scrape_runs_trigger)이 이 값 집합을
# 강제하며, ORM 쪽 service 레이어(app/scrape_control/)에서 INSERT 전 동일
# 도메인으로 검증한다 — 바깥에서 임의 문자열이 DB 까지 도달하지 않게 막는다.
SCRAPE_RUN_STATUSES: tuple[str, ...] = (
    "running",
    "completed",
    "cancelled",
    "failed",
    "partial",
)
SCRAPE_RUN_TRIGGERS: tuple[str, ...] = ("manual", "scheduled", "cli")

# terminal 상태(더 이상 진행 중이 아닌 상태) 집합. finalize 의 idempotent 체크에 사용.
SCRAPE_RUN_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "cancelled", "failed", "partial"}
)


class ScrapeRun(Base):
    """수집 실행 1회의 요약.

    Phase 1a migration(``20260422_1500_b2c5e8f1a934_phase1a_new_tables.py`` §12)
    에서 테이블 DDL 은 이미 생성되어 있고, Phase 2(00025) 에서 ORM 을 얹어
    수동(웹)/자동(스케줄)/CLI 3경로에서 공통으로 기록하도록 한다.

    설계:
        - running row 는 동시 1개만 존재(app 레벨 lock).
          create_scrape_run 이 사전 SELECT 로 검증한다.
        - pid 는 subprocess.Popen 이후 set_scrape_run_pid 로 주입한다.
          cli 경로에서는 자기 자신의 pid 를 넣어 stale cleanup 과 관리
          일관성을 맞춘다.
        - source_counts 는 JSON 으로 누적·최종 요약을 둘 다 담을 수 있는
          자유 스키마. 세부는 docs/scrape_control_design.md §7.5 참조.
        - status / trigger 은 좁은 문자열 도메인(위 상수). DB CHECK 가 있고
          ORM 컬럼의 server_default 역시 migration 과 1:1 일치시킨다.

    컬럼/인덱스 이름은 migration 과 정확히 일치해야 한다 (autogenerate diff 비움).
    """

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="수집 실행이 개시된 시각(UTC).",
    )

    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="수집이 종료된 시각(UTC). running 중에는 NULL.",
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        # migration DDL 이 server_default=sa.text(\"'running'\") 를 준 것과 1:1 일치시켜야
        # autogenerate diff 가 비는 상태가 유지된다 (docs/db_portability.md §4).
        server_default=text("'running'"),
        default="running",
        doc="실행 상태. SCRAPE_RUN_STATUSES 중 하나.",
    )

    trigger: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="실행 트리거. SCRAPE_RUN_TRIGGERS 중 하나.",
    )

    source_counts: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        doc="소스별/전체 수집 결과 요약 (자유 스키마 JSON). "
            "docs/scrape_control_design.md §7.5 참조.",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="실패/부분 성공 시의 요약 메시지. 진단용 — UI 에 노출.",
    )

    pid: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc="subprocess pid (웹 경로) 또는 cli 프로세스 pid. "
            "stale cleanup 에서 프로세스 존재 여부 확인에 사용.",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'cancelled', 'failed', 'partial')",
            name="ck_scrape_runs_status",
        ),
        CheckConstraint(
            "trigger IN ('manual', 'scheduled', 'cli')",
            name="ck_scrape_runs_trigger",
        ),
        Index("ix_scrape_runs_started_at", "started_at"),
        Index("ix_scrape_runs_status", "status"),
    )

    def is_terminal(self) -> bool:
        """현재 status 가 terminal(완료·중단·실패·부분) 상태인지 반환."""
        return self.status in SCRAPE_RUN_TERMINAL_STATUSES

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현."""
        return (
            f"<ScrapeRun id={self.id} status={self.status!r} "
            f"trigger={self.trigger!r} pid={self.pid}>"
        )


# ── Phase 5a (task 00041) — delta + snapshot 인프라 ─────────────────────────
# 수집 파이프라인을 delta 단계 적재 → 종료 시 단일 트랜잭션 apply 로 재배선하기
# 위한 3 개 테이블 ORM. migration 파일:
#   alembic/versions/20260428_1700_d3f9a2b6c814_delta_snapshot_tables.py
# 설계 근거: docs/snapshot_pipeline_design.md §4·§5·§6.


class DeltaAnnouncement(Base):
    """수집 단계에서 적재되는 공고 메타 staging.

    매 ScrapeRun 동안 어댑터가 공고 1 건을 수집할 때마다 이 테이블에 INSERT
    된다. 본 테이블 ``announcements`` 는 수집 중에 건드리지 않고, ScrapeRun
    종료 시점에 단일 트랜잭션(``apply_delta_to_main`` — 00041-3) 안에서

        1. delta_announcements 전수 조회
        2. announcements 본 테이블과 4-branch 비교 + INSERT/UPDATE
        3. 5 종 카테고리 매핑 + scrape_snapshots UPSERT
        4. 같은 scrape_run_id 의 delta 전수 DELETE

    를 수행한다 — 사용자 원문 "수집 종료 시: 단일 트랜잭션으로 (delta → 본
    테이블 4-branch UPSERT) + (snapshot 생성/UPSERT) + (delta 비우기)" 그대로.

    설계 메모 (docs/snapshot_pipeline_design.md §4.1):
        - 본 테이블 announcements 와 비교 가능한 핵심 필드(title / status /
          agency / deadline_at)를 그대로 갖는다.
        - status 는 plain String(32) 으로 둔다 — 본 테이블의 AnnouncementStatus
          Enum 으로 정규화하는 것은 apply 단계의 책임이다. 어댑터(IRIS / NTIS) 가
          내려주는 raw 값을 일단 받아내고, apply 단계의 ``_coerce_status`` 가
          한글 3 종 enum 으로 정규화한다. 정규화 실패 시 해당 공고만 apply 가
          격리해 다음 공고로 진행한다.
        - 매 ScrapeRun 종료 후 row 가 0 으로 리셋되므로 인덱스는 (scrape_run_id) +
          source lookup (source_type, source_announcement_id) 두 개만.
        - ON DELETE CASCADE 가 scrape_runs 에 걸려 있어 ScrapeRun row 삭제 시
          delta 도 자동 정리되지만, 운영 흐름상 ScrapeRun 은 삭제하지 않으므로
          실제 비움은 apply 단계의 명시적 DELETE 가 담당한다.
    """

    __tablename__ = "delta_announcements"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    scrape_run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "scrape_runs.id",
            name="fk_delta_announcements_scrape_run_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        doc="이 staging row 가 속한 ScrapeRun 의 PK. 매 ScrapeRun 종료 시"
            " apply 후 비워지는 단위 키.",
    )

    source_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="공고 수집 소스 유형. 예: 'IRIS', 'NTIS'. 본 테이블과 동일 의미.",
    )

    source_announcement_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        doc="수집 소스가 부여한 공고 고유 ID. apply 단계의 4-branch 매칭 키.",
    )

    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="공고 제목. 1차 변경 감지 비교 필드.",
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="어댑터가 내려준 raw 상태 문자열. 본 테이블의 AnnouncementStatus"
            " enum 으로 정규화하는 것은 apply 단계의 책임 — delta 는 raw 값"
            " 입구로 동작해 source 별 잡음을 본 테이블에 들이지 않는다.",
    )

    agency: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="주관/공고 기관명. 1차 변경 감지 비교 필드. 없으면 NULL.",
    )

    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="접수 시작 시각(UTC). 비교 제외 — announcements 와 동일 컨벤션.",
    )

    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="접수 마감 시각(UTC). 1차 변경 감지 비교 필드.",
    )

    detail_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="공고 상세 페이지 URL.",
    )

    detail_html: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="상세 페이지 본문 HTML. 상세 수집 성공 시 채워진다.",
    )

    detail_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="detail_html 에서 추출한 가독성 텍스트.",
    )

    detail_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="상세 수집 완료 시각(UTC).",
    )

    detail_fetch_status: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        doc="상세 수집 결과 상태. 'ok' / 'empty' / 'error'.",
    )

    ancm_no: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="IRIS / NTIS 공식 공고번호. apply 단계에서 _apply_canonical 의"
            " official scheme key 계산에 사용한다 (NTIS 는 상세 수집 후에만"
            " 확정되므로 None 도 흔히 들어온다).",
    )

    raw_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        doc="어댑터가 내려준 원본 메타 (raw row 등). apply 단계가 본 테이블의"
            " announcements.raw_metadata 로 그대로 흘려 보낸다.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="이 staging row 가 INSERT 된 시각(UTC).",
    )

    # apply 단계가 첨부 메타까지 한 번에 비교하기 위한 1:N 관계.
    # 본 테이블의 Announcement.attachments 와 동일 패턴 (selectin) 으로 로드한다.
    attachments: Mapped[list[DeltaAttachment]] = relationship(
        "DeltaAttachment",
        back_populates="delta_announcement",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        # apply 단계: scrape_run_id 로 전수 조회한다.
        Index(
            "ix_delta_announcements_scrape_run_id",
            "scrape_run_id",
        ),
        # apply 단계: 본 테이블 announcements 와 (source_type, source_announcement_id)
        # 복합 키로 매칭. UNIQUE 가 아닌 일반 인덱스 — 같은 ScrapeRun 안에서
        # 동일 (source_type, source_announcement_id) 가 두 번 들어오는 경우는
        # 정상이 아니지만 어댑터 버그를 막아내려면 고정 도메인 제약은 부담스럽다.
        Index(
            "ix_delta_announcements_source_lookup",
            "source_type",
            "source_announcement_id",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현."""
        return (
            f"<DeltaAnnouncement id={self.id} run={self.scrape_run_id} "
            f"source={self.source_type}/{self.source_announcement_id!r} "
            f"status={self.status!r}>"
        )


class DeltaAttachment(Base):
    """수집 단계에서 적재되는 첨부 메타 staging.

    어댑터가 첨부 1 건을 다운로드해 ``data/downloads/`` 에 떨어뜨린 직후 이
    테이블에 메타 row 를 INSERT 한다. 파일 자체는 트랜잭션 보호 밖이며, apply
    단계가 rollback 되면 디스크에는 파일이 남는다 — 후속 GC (00041-5) 가
    정리한다.

    설계 메모 (docs/snapshot_pipeline_design.md §4.2):
        - 본 테이블 ``attachments`` 와 컬럼 의미가 1:1 대응되도록 맞춘다 (apply
          단계가 dict 흐름으로 그대로 옮길 수 있도록).
        - sha256 은 NULL 허용 — 다운로드 실패 / 부분 저장 시. apply 단계의
          첨부 변경 감지(2차 감지) 가 sha256 NULL 인 row 는 sha256s 집합에서
          제외한다.
        - delta_announcement FK 는 CASCADE — apply 단계가 delta_announcements
          를 비우면 자동 cascade 로 함께 사라진다.
    """

    __tablename__ = "delta_attachments"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    delta_announcement_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "delta_announcements.id",
            name="fk_delta_attachments_delta_announcement_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        doc="이 staging 첨부가 속한 DeltaAnnouncement 의 PK.",
    )

    original_filename: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        doc="소스에 표기된 원본 파일명. 본 테이블 attachments 와 동일.",
    )

    stored_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="다운로드 후 저장된 로컬 파일 경로 (data/downloads/...).",
    )

    file_ext: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="파일 확장자 (소문자, 점 없이). 예: 'pdf', 'hwp'.",
    )

    file_size: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        doc="바이트 단위 파일 크기. 다운로드 실패 시 NULL.",
    )

    download_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="원본 다운로드 URL. POST 다운로드만 제공하는 소스는 NULL.",
    )

    sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="파일 전체의 SHA-256 해시(hex). apply 단계 2차 변경 감지의 핵심"
            " 비교 키. 다운로드 실패 시 NULL — 비교 대상에서 제외된다.",
    )

    downloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="첨부 다운로드 완료 시각(UTC).",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="이 staging row 가 INSERT 된 시각(UTC). downloaded_at 과는 별개로,"
            " 다운로드 후 DB 메타 적재까지의 지연을 분리해 추적할 수 있게 한다.",
    )

    delta_announcement: Mapped[DeltaAnnouncement] = relationship(
        "DeltaAnnouncement",
        back_populates="attachments",
    )

    __table_args__ = (
        Index(
            "ix_delta_attachments_delta_announcement_id",
            "delta_announcement_id",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현."""
        return (
            f"<DeltaAttachment id={self.id} "
            f"delta_announcement_id={self.delta_announcement_id} "
            f"name={self.original_filename!r} ext={self.file_ext!r}>"
        )


class ScrapeSnapshot(Base):
    """KST 날짜 단위 변화 요약. 일자별 1 row.

    같은 KST 날짜에 여러 ScrapeRun(수동 / 자동 / CLI) 이 종료되면 본 row 의
    payload 가 ``merge_snapshot_payload(existing, new)`` 로 머지된다 — 사용자
    원문 "snapshot UPSERT (같은 KST 날짜 1 row). 같은 날 여러 ScrapeRun 의 변화는
    머지" 그대로.

    설계 메모 (docs/snapshot_pipeline_design.md §4.3·§9·§10):
        - ``snapshot_date`` 는 ``Date`` 타입. SQLite 의 Date 컬럼은 timezone
          정보를 갖지 않으므로 호출자(00041-4 의 upsert) 가
          ``app.timezone.now_kst().date()`` 로 KST 변환 후 저장한다 — Phase 4
          컨벤션 준수.
        - ``created_at`` / ``updated_at`` 는 모두 ``DateTime(timezone=True)``,
          UTC 저장.
        - ``payload`` 는 5 종 카테고리(new / content_changed / transitioned_to_접수예정/
          접수중/마감) + counts 를 담는 자유 스키마 JSON. 구조는 §10, 머지 규칙은
          §9 에 있고, 머지 헬퍼는 ``app/db/snapshot.py`` 의 ``merge_snapshot_payload``
          단독 함수다 (00041-4 가 구현).
        - UNIQUE(snapshot_date) 가 implicit index 로 일자 lookup 을 커버한다 —
          별도 인덱스 없음.
    """

    __tablename__ = "scrape_snapshots"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    snapshot_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        doc="이 row 가 가리키는 KST 날짜. 호출자가 now_kst().date() 로 변환 후"
            " 저장한다 (Date 타입은 timezone 정보를 갖지 않음).",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="이 snapshot row 가 최초 INSERT 된 시각(UTC).",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="payload 가 마지막으로 머지된 시각(UTC). UPSERT 머지 시 자동 갱신.",
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        doc="5 종 카테고리(new / content_changed / transitioned_to_접수예정/"
            "접수중/마감) + counts 를 담는 자유 스키마 JSON."
            " 구조는 docs/snapshot_pipeline_design.md §10, 머지 규칙은 §9 참조.",
    )

    __table_args__ = (
        # 같은 KST 날짜 1 row 보장. 머지의 기준 키가 된다.
        UniqueConstraint(
            "snapshot_date",
            name="uq_scrape_snapshots_snapshot_date",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현."""
        return (
            f"<ScrapeSnapshot id={self.id} date={self.snapshot_date!s}>"
        )


# ──────────────────────────────────────────────────────────────
# Organization / UserOrganization (task 00049 — 조직 트리 + M:N 매핑)
# ──────────────────────────────────────────────────────────────


class Organization(Base):
    """조직 트리 노드.

    depth 는 무제한이며 parent_id 로 트리를 구성한다.
    루트 노드(최상위 조직)는 parent_id 가 NULL 이다.

    자식이 있는 조직을 직접 삭제하면 DB FK(ON DELETE RESTRICT) 가 오류를 발생시킨다.
    app 레벨에서는 삭제 전에 자식 존재 여부를 먼저 확인하고
    OrganizationHasChildrenError 를 던져 친절한 메시지를 제공한다.

    루트(parent_id IS NULL) 간 동명 체크는 SQLite 의 NULL 비교 특성상
    UNIQUE 제약으로 막을 수 없으므로 app 레벨 SELECT 체크로 보강한다
    (FavoriteFolder 동일 패턴 — models.py 주석 참조).

    관계:
        parent: 부모 조직 (None 이면 루트).
        children: 직속 자식 조직 목록.
        user_organizations: 이 조직에 속한 사용자 매핑 목록.
    """

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    parent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_organizations_parent_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
        index=True,
        doc="부모 조직 PK. NULL 이면 루트(최상위).",
    )

    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        doc="조직명.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="조직이 생성된 시각(UTC).",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="레코드가 마지막으로 갱신된 시각(UTC).",
    )

    # self-referential: remote_side=[id] 로 "parent_id → id" 방향을 명시한다.
    parent: Mapped[Organization | None] = relationship(
        "Organization",
        remote_side="Organization.id",
        back_populates="children",
        foreign_keys="[Organization.parent_id]",
        lazy="select",
    )
    children: Mapped[list[Organization]] = relationship(
        "Organization",
        back_populates="parent",
        foreign_keys="[Organization.parent_id]",
        lazy="selectin",
    )
    user_organizations: Mapped[list[UserOrganization]] = relationship(
        "UserOrganization",
        back_populates="organization",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<Organization id={self.id} name={self.name!r} parent_id={self.parent_id}>"
        )


class UserOrganization(Base):
    """사용자 ↔ 조직 M:N junction.

    사용자는 0개 이상의 조직에 속할 수 있다.
    사용자 삭제(CASCADE) 또는 조직 삭제(CASCADE) 시 이 매핑 row 도 자동 제거된다.

    UNIQUE(user_id, organization_id) 로 중복 매핑 방지.
    """

    __tablename__ = "user_organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_user_organizations_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="소속 사용자 PK.",
    )

    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_user_organizations_organization_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="소속 조직 PK.",
    )

    user: Mapped[User] = relationship(
        "User",
        back_populates="user_organizations",
    )
    organization: Mapped[Organization] = relationship(
        "Organization",
        back_populates="user_organizations",
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "organization_id",
            name="uq_user_organizations_user_org",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<UserOrganization id={self.id} user_id={self.user_id} "
            f"organization_id={self.organization_id}>"
        )


class SystemSetting(Base):
    """시스템 전역 설정을 key-value 형태로 영속하는 테이블.

    백업 정책(cron 표현식·최대 보관 수) 등 관리자가 UI 에서 변경하는 설정을
    DB 에 저장한다. 키 이름 상수는 ``app.backup.constants`` 에 정의한다.

    값은 평문 문자열로 저장하며, 숫자 등 해석은 호출 측이 담당한다.
    key 가 PRIMARY KEY 이므로 upsert 는 ``session.get`` + ``session.add`` 패턴으로 구현한다.
    """

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
        doc="설정 식별 키. 예: 'backup.cron_expression'",
    )

    value: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="설정 값. 문자열로 저장. 없으면 NULL.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="마지막으로 설정이 변경된 시각(UTC).",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return f"<SystemSetting key={self.key!r} value={self.value!r}>"


class BackupHistory(Base):
    """DB 백업 실행 이력 한 건을 나타내는 레코드.

    백업이 실행될 때마다(수동·스케줄 모두) 한 row 가 INSERT 된다.
    성공/실패 여부, 대상 파일, 생성된 백업 파일, 소요 시간, 총 크기를 기록한다.
    롤백/복원 기능은 범위 밖 — 이 테이블은 이력 조회 전용이다.

    target_files / backup_files 는 JSON 리스트로 저장한다.
    예: ``['data/db/app.sqlite3', 'data/db/boards.sqlite3']``
    """

    __tablename__ = "backup_history"

    __table_args__ = (
        Index("ix_backup_history_executed_at", "executed_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="백업이 실행된 시각(UTC).",
    )

    # 'scheduled' 또는 'manual'
    trigger: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="실행 트리거. 'scheduled': 스케줄 자동 실행, 'manual': 관리자 즉시 실행.",
    )

    # 백업 대상 DB 파일 경로 목록 (PROJECT_ROOT 기준 상대 경로)
    target_files: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="백업 대상 DB 파일 경로 목록. 예: ['data/db/app.sqlite3']",
    )

    # 생성된 백업 파일 경로 목록 (PROJECT_ROOT 기준 상대 경로)
    backup_files: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        doc="생성된 백업 파일 경로 목록.",
    )

    success: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        doc="백업 성공 여부.",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="실패 시 오류 메시지. 성공이면 NULL.",
    )

    duration_seconds: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        doc="백업 소요 시간(초).",
    )

    total_size_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        doc="생성된 백업 파일 총 크기(bytes). 실패 시 NULL.",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<BackupHistory id={self.id} trigger={self.trigger!r} "
            f"success={self.success} executed_at={self.executed_at}>"
        )


class AnnouncementProgressStatus(StrEnum):
    """공고 진행 상태 4 단계.

    값은 한글 원문을 그대로 사용한다 (PROJECT_NOTES 의 한글 enum 보존 컨벤션).

    의미:
        INTEREST    '관심' — 관심 공고 표시. 여러 조직 동시 가능.
        REVIEW      '검토' — 검토 단계. 여러 조직 동시 가능.
        IN_PROGRESS '진행' — 실제 진행 단계. 한 canonical 당 단일 조직 선점
                              (선점 제약은 repository 의 app-level transactional
                              체크가 보장 — partial unique index 는 사용하지 않음).
        DONE        '종료' — 종료 단계. 의미상 활동성으로 보지 않으며, 카운터·
                              필터에서 제외된다.
    """

    INTEREST = "관심"
    REVIEW = "검토"
    IN_PROGRESS = "진행"
    DONE = "종료"


class AnnouncementProgressArchiveReason(StrEnum):
    """진행 상태 row 가 history 로 이관된 사유.

    USER_CHANGED      'user_changed' — 사용자가 status/note 를 변경.
    CONTENT_CHANGED   'content_changed' — canonical 비교 4 필드 변경 감지로
                                          (Phase 1a §9) 일괄 reset 시 이관.
    """

    USER_CHANGED = "user_changed"
    CONTENT_CHANGED = "content_changed"


class AnnouncementProgress(Base):
    """canonical_project × organization 단위 현재 유효 진행 상태.

    같은 공고를 여러 조직이 모르고 중복 진행하는 사고를 방지하기 위해, canonical
    마다 조직별 진행 상태(관심/검토/진행/종료)를 표명·열람할 수 있게 한다 (Phase
    C, task 00097). UNIQUE 키 (canonical_project_id, organization_id) 로 한 조직
    이 한 canonical 에 row 1 개만 가진다.

    Phase B (RelevanceJudgment) 와의 차이:
        - row 단위 키에 user_id 가 없다 — "조직 입장 = 1 row" 로 정규화.
          작성자는 created_by_user_id 메타에 보존하며 권한 판정에는 사용하지 않는다.
        - 권한 = 조직 멤버 누구나 (Phase B 의 row 작성자 본인 한정과 의도적 분기).
          작성자 휴가/퇴사 시 다른 멤버가 변경할 수 있도록 한 결정.

    선점 제약:
        한 canonical 에 status='진행' row 가 최대 1 개. partial unique index 회피
        결정(docs/db_portability §3 + Phase B 패턴)에 따라 DB UNIQUE 가 아닌
        repository 의 app-level transactional 체크가 보장한다 (00097-3 책임).

    내용 변경 감지(content_changed) 시 해당 canonical 의 모든 row 는
    AnnouncementProgressHistory 로 이관되어 이 테이블에서 제거된다.
    """

    __tablename__ = "announcement_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    canonical_project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "canonical_projects.id",
            name="fk_announcement_progress_canonical_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="진행 상태 대상 canonical_project PK.",
    )

    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_announcement_progress_organization_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="진행 상태 표명 조직 PK. 조직 삭제 시 CASCADE 로 함께 사라진다.",
    )

    # status: 4 단계 한글 enum. native_enum=False — Postgres ENUM 타입 미생성,
    # CHECK constraint 만 추가 (db_portability §1).
    status: Mapped[AnnouncementProgressStatus] = mapped_column(
        Enum(
            AnnouncementProgressStatus,
            name="announcement_progress_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
        ),
        nullable=False,
        doc="진행 상태. '관심' / '검토' / '진행' / '종료' 중 하나.",
    )

    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="자유 메모. 없으면 NULL.",
    )

    # created_by_user_id: 마지막 수정자 메타. 권한 판정에는 사용하지 않는다
    # (조직 멤버 누구나 정책). 사용자 탈퇴 시 NULL 로 남고 row 자체는 보존.
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_announcement_progress_created_by_user_id",
            ondelete="SET NULL",
        ),
        nullable=True,
        doc="마지막 수정자 사용자 PK. 권한 판정에는 사용하지 않는다.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="최초 INSERT 시각(UTC).",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="마지막 UPDATE 시각(UTC). status / note 변경 시 자동 갱신.",
    )

    # 관계 — repository / 라우트가 lazy load 로 조직명·작성자명을 끌어올 때 사용.
    canonical_project: Mapped[CanonicalProject] = relationship(
        "CanonicalProject",
        foreign_keys=[canonical_project_id],
        lazy="select",
    )
    organization: Mapped[Organization] = relationship(
        "Organization",
        foreign_keys=[organization_id],
        lazy="select",
    )
    created_by: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[created_by_user_id],
        lazy="select",
    )

    __table_args__ = (
        UniqueConstraint(
            "canonical_project_id",
            "organization_id",
            name="uq_announcement_progress_canonical_org",
        ),
        # status 한글 enum DB 레벨 강제 (Enum 자체는 create_constraint=False 가 SA
        # 기본이라 CHECK 를 자동 추가하지 않는다 — relevance_judgments.verdict 와
        # 동일 패턴으로 명시 CHECK constraint).
        CheckConstraint(
            "status IN ('관심', '검토', '진행', '종료')",
            name="ck_announcement_progress_status",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<AnnouncementProgress id={self.id} "
            f"canonical={self.canonical_project_id} "
            f"organization_id={self.organization_id} "
            f"status={self.status!r}>"
        )


class AnnouncementProgressHistory(Base):
    """이관된 과거 진행 상태 row.

    AnnouncementProgress row 가 사용자 변경(user_changed) 또는 canonical 내용 변경
    감지(content_changed, Phase 1a §9) 로 갱신·삭제될 때 이전 값이 이 테이블로
    복사된다. 이 테이블은 append-only 로 취급하며 이관된 레코드는 수정하지 않는다.

    UNIQUE 없음 — 같은 (canonical, organization) 조합에 대해 시간 순으로 row 가
    누적된다. archive_reason 으로 이관 사유를 구분한다.

    조직이 삭제되면 그 조직 입장의 history 도 CASCADE 로 함께 사라진다 — 진행
    상태의 의미가 조직과 결합되어 있기 때문 (조직 사라짐 = 그 조직 입장 무의미).
    """

    __tablename__ = "announcement_progress_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    canonical_project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "canonical_projects.id",
            name="fk_announcement_progress_history_canonical_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="이관 시점의 canonical_project PK.",
    )

    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_announcement_progress_history_organization_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="이관 시점의 진행 상태 표명 조직 PK.",
    )

    status: Mapped[AnnouncementProgressStatus] = mapped_column(
        Enum(
            AnnouncementProgressStatus,
            name="announcement_progress_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
        ),
        nullable=False,
        doc="이관 시점의 진행 상태 값.",
    )

    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="이관 시점의 자유 메모. 원본 그대로 복사.",
    )

    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_announcement_progress_history_created_by_user_id",
            ondelete="SET NULL",
        ),
        nullable=True,
        doc="이관 시점의 마지막 수정자 사용자 PK.",
    )

    # created_at / updated_at 은 이관 시점에 원본 값을 그대로 복사한다 — 이관 시각
    # 자체는 archived_at 에 별도 기록.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="이관 시점의 원본 created_at(UTC). 이관 시 덮어쓰지 않는다.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="이관 시점의 원본 updated_at(UTC). 이관 시 덮어쓰지 않는다.",
    )

    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="history 로 이관된 시각(UTC).",
    )

    archive_reason: Mapped[AnnouncementProgressArchiveReason] = mapped_column(
        Enum(
            AnnouncementProgressArchiveReason,
            name="announcement_progress_archive_reason",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
        ),
        nullable=False,
        doc="이관 사유. 'user_changed' / 'content_changed'.",
    )

    canonical_project: Mapped[CanonicalProject] = relationship(
        "CanonicalProject",
        foreign_keys=[canonical_project_id],
        lazy="select",
    )
    organization: Mapped[Organization] = relationship(
        "Organization",
        foreign_keys=[organization_id],
        lazy="select",
    )
    created_by: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[created_by_user_id],
        lazy="select",
    )

    __table_args__ = (
        # status / archive_reason 한글 enum DB 레벨 강제. Postgres 에서 같은 schema
        # 안의 동명 CHECK constraint 충돌을 피하려고 history 쪽 CHECK 이름은 별도로
        # 부여한다.
        CheckConstraint(
            "status IN ('관심', '검토', '진행', '종료')",
            name="ck_announcement_progress_history_status",
        ),
        CheckConstraint(
            "archive_reason IN ('user_changed', 'content_changed')",
            name="ck_announcement_progress_history_archive_reason",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<AnnouncementProgressHistory id={self.id} "
            f"canonical={self.canonical_project_id} "
            f"organization_id={self.organization_id} "
            f"status={self.status!r} archive_reason={self.archive_reason!r}>"
        )


class EmailSendRunStatus(StrEnum):
    """메일 발송 시도의 최종 결과 (Phase A-1 / task 00104-3).

    한 번의 ``send_with_retry`` 호출이 EmailSendRun row 1 개를 만들며, 모든
    재시도가 끝난 뒤의 최종 결과만 본 enum 값으로 기록된다 (중간 시도 결과는
    별도 row 로 분리하지 않는다 — 디자인 노트 §4-1).

    값:
        SENT    'sent'   — 1차 시도 또는 재시도 중 어느 한 번이라도 성공.
        FAILED  'failed' — 모든 시도(1차 + max_retry_count 번) 가 실패.
    """

    SENT = "sent"
    FAILED = "failed"


class EmailSendRun(Base):
    """메일 발송 시도 이력 한 건 (Phase A-1 / task 00104-3).

    한 ``send_with_retry`` 호출 = 1 row 정책. 재시도 횟수는 ``attempt_count``
    에 누적되며, 중간 시도의 예외는 본 row 에 영구 저장하지 않고 loguru 로그
    로만 남긴다. 마지막 시도의 예외 메시지만 ``error_message`` 에 저장한다.

    설계 근거: docs/phase_a1_design_note.md §4-1, §4-2, §4-5, §9.

    Phase A-1 단계에서 ``related_kind`` 에 채워지는 값은 ``'test_send'`` 하나
    뿐이며, A-2 (공고 포워딩) 부터 ``'forward'`` 가, A-3 (daily report) 부터
    ``'daily_report'`` 가 추가된다. ``transport_type`` 도 현재는 ``'m365_oauth'``
    만 사용되며 향후 옵션 C (Basic Auth SMTP) 가 추가 가능하도록 양쪽 모두
    DB 레벨 CHECK constraint 를 두지 않는다 (디자인 노트 §4-5).

    시간 처리:
        - ``created_at`` / ``sent_at`` 은 UTC tz-aware 로 저장 (PROJECT_NOTES
          시각 컨벤션). 사용자 화면 표시 직전에 ``app.timezone.format_kst`` /
          Jinja2 ``kst_format`` 필터로 KST 변환.
        - ``sent_at`` 은 발송 성공 시각만 채우고 실패는 NULL — \"성공한 발송이
          언제 끝났는가\" 를 명확히 한다.
    """

    __tablename__ = "email_send_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    recipient: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        doc="받는 사람 이메일 주소. RFC 5321 최대 320 자.",
    )

    subject: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc="메일 제목. Admin API Pydantic schema 가 1 ≤ len ≤ 200 으로 강제.",
    )

    body_preview: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc="본문 앞 200 자 preview. 전체 본문 저장은 사이즈 부담 → preview 만 보관.",
    )

    transport_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc=(
            "발송 transport 종류. A-1 단계에서는 'm365_oauth' 만 사용. CHECK "
            "constraint 없음 — 옵션 C 추가 시 ALTER 없이 새 값 가능."
        ),
    )

    # status: 'sent' / 'failed' enum. native_enum=False — Postgres ENUM 타입
    # 미생성, CHECK constraint 만 추가 (AnnouncementProgress 와 동일 패턴).
    status: Mapped[EmailSendRunStatus] = mapped_column(
        Enum(
            EmailSendRunStatus,
            name="email_send_run_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
        ),
        nullable=False,
        doc="발송 결과. 'sent' / 'failed' 중 하나.",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "실패 시 마지막 시도의 예외 'ClassName: message' 문자열. 성공 row "
            "는 NULL. 중간 시도 에러는 본 컬럼 대신 loguru 로그에서 확인 "
            "(디자인 노트 §4-1)."
        ),
    )

    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        doc=(
            "총 시도 횟수 (1차 + 재시도 누적). 예: max_retry_count=2 일 때 "
            "최대 3 까지 가능."
        ),
    )

    requested_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_email_send_runs_requested_by_user_id",
            ondelete="SET NULL",
        ),
        nullable=True,
        doc=(
            "발송을 트리거한 사용자 PK. 시스템 자동 발송(향후 daily report) 대비 "
            "nullable. 사용자 탈퇴 시 SET NULL 로 row 자체는 보존."
        ),
    )

    related_kind: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        doc=(
            "발송 컨텍스트 식별자. A-1: 'test_send' 만 사용, A-2: 'forward', "
            "A-3: 'daily_report' 추가 예정. CHECK constraint 없음 (확장 여유)."
        ),
    )

    related_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        doc=(
            "related_kind 와 함께 외부 객체를 가리키는 PK. A-1 에서는 항상 NULL. "
            "A-2 부터 채워짐 (예: EmailForwardLog.id)."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc=(
            "발송 시도 시작 시각 (UTC tz-aware). 표시 직전에 KST 변환 "
            "(app.timezone.format_kst / Jinja2 kst_format 필터)."
        ),
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="발송 성공 시각 (UTC tz-aware). 실패 row 는 NULL.",
    )

    # 관계 — admin 'send-runs' API 응답에서 username 을 끌어올 때 사용. lazy=
    # 'select' 로 필요 시 1 회 추가 SELECT — 발송 이력 화면은 페이지당 50 row
    # 정도라 N+1 우려가 작다. 사용량이 더 늘어나면 selectinload 로 batch 화
    # 가능.
    requested_by: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[requested_by_user_id],
        lazy="select",
    )

    __table_args__ = (
        # status enum 한글 가독성을 위해 같은 SQL 을 migration 과 ORM 양쪽에
        # 중복 선언. SQLAlchemy 의 sa.Enum(native_enum=False) 는
        # create_constraint=False 가 기본이라 CHECK 를 자동 추가하지 않으므로
        # 명시적으로 둔다 (AnnouncementProgress 와 동일 패턴).
        CheckConstraint(
            "status IN ('sent', 'failed')",
            name="ck_email_send_runs_status",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<EmailSendRun id={self.id} recipient={self.recipient!r} "
            f"status={self.status!r} attempt_count={self.attempt_count}>"
        )


class EmailForwardStatus(StrEnum):
    """공고 포워딩 액션 전체의 결과 요약 (Phase A-2 Part 1 / task 00106).

    한 포워딩 요청에서 모든 수신자에 대한 발송이 끝난 뒤의 최종 결과를 기록한다.
    개별 수신자 단위 성공/실패 카운트는 ``EmailForwardLog.success_count`` /
    ``EmailForwardLog.failure_count`` 컬럼에 별도 저장한다.

    값은 영문 소문자 — 기술 상태 enum 컨벤션 (EmailSendRunStatus 와 동일).
    사용자 화면 표시용 레이블 변환은 Part 2 UI 레이어에서 처리한다.

    값:
        SUCCESS  'success' — 모든 수신자 발송 성공.
        PARTIAL  'partial' — 일부 성공·일부 실패 혼재.
        FAILED   'failed'  — 모든 수신자 발송 실패.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class EmailForwardLog(Base):
    """공고 포워딩 액션 이력 한 건 (Phase A-2 Part 1 / task 00106).

    사용자가 특정 공고를 여러 수신자에게 메일로 포워딩할 때 1 row 가 생성된다.
    개인정보·DB 크기 고려로 메시지 본문은 저장하지 않으며, 사용자가 추가 메시지를
    첨부했는지 여부만 ``has_additional_message`` boolean 으로 기록한다.

    ``EmailSendRun`` 과의 관계:
        - ``EmailSendRun.related_kind = 'forward'``, ``related_id = EmailForwardLog.id``
          로 연결된다 (Part 2 에서 row INSERT 시 application 코드가 채운다).
        - ``EmailSendRun`` 의 컬럼·인덱스·의미는 Part 1 에서 변경하지 않는다.

    설계 근거: docs/phase_a2_part1_design_note.md, docs/db_portability.md §3.

    시간 처리:
        - ``created_at`` / ``completed_at`` 은 UTC tz-aware 로 저장 (PROJECT_NOTES
          시각 컨벤션). 사용자 화면 표시 직전에 ``app.timezone.format_kst`` /
          Jinja2 ``kst_format`` 필터로 KST 변환.

    status 의 default 없음:
        포워딩 액션 시작 시점에는 어느 결과가 될지 알 수 없으므로, status 에는
        default 를 두지 않는다. row INSERT 시 application 코드(Part 2)가 명시적으로
        채워야 하는 의도된 제약이다.
    """

    __tablename__ = "email_forward_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 어떤 공고를 포워딩했는지. 공고 삭제 시 포워딩 이력도 함께 삭제(CASCADE).
    canonical_project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "canonical_projects.id",
            name="fk_email_forward_logs_canonical_project_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="포워딩 대상 공고 PK. 공고 삭제 시 CASCADE 로 이력도 삭제.",
    )

    # 발송을 트리거한 사용자. 사용자 탈퇴 시 SET NULL 로 row 자체는 보존.
    sender_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_email_forward_logs_sender_user_id",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
        doc="발송 트리거 사용자 PK. 사용자 삭제 시 SET NULL (row 보존).",
    )

    # 발송 시점 발송자의 조직 입장. 무소속/미지정이면 NULL.
    sender_organization_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "organizations.id",
            name="fk_email_forward_logs_sender_organization_id",
            ondelete="SET NULL",
        ),
        nullable=True,
        doc=(
            "발송 시점 발송자 조직 PK (Phase B/C 조직 단위 패턴과 일관). "
            "무소속·미지정이면 NULL. 조직 삭제 시 SET NULL."
        ),
    )

    subject: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        doc=(
            "메일 제목. 본문은 개인정보·DB 크기 고려로 미저장 — "
            "제목만 메타데이터로 보존."
        ),
    )

    # 사용자가 추가 메시지를 첨부했는지 여부만 기록. 본문 텍스트 자체는 미저장.
    has_additional_message: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc=(
            "사용자가 추가 메시지를 첨부했는지 여부. "
            "메시지 본문 자체는 개인정보 사유로 DB 에 저장하지 않는다."
        ),
    )

    # 수신자 이메일 주소 목록 (list of str). db_portability §1 — JSON 범용 타입 사용.
    recipient_addresses: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        doc=(
            "수신자 이메일 주소 목록 (list of str). "
            "빈 리스트는 DB 차원에서 허용, app-level 검증은 Part 2 담당."
        ),
    )

    # len(recipient_addresses) 의 denormalize. 통계·UI 표시용.
    recipient_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="수신자 수. recipient_addresses 길이를 denormalize 한 값.",
    )

    # 포워딩 결과 enum. native_enum=False — Postgres ENUM 타입 미생성,
    # CHECK constraint 만 추가 (db_portability §3 / EmailSendRun 동일 패턴).
    # default 없음 — INSERT 시 application 코드(Part 2)가 명시적으로 채워야 한다.
    status: Mapped[EmailForwardStatus] = mapped_column(
        Enum(
            EmailForwardStatus,
            name="email_forward_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
        ),
        nullable=False,
        doc="포워딩 결과. 'success' / 'partial' / 'failed' 중 하나. default 없음.",
    )

    success_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="발송 성공 수신자 수.",
    )

    failure_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="발송 실패 수신자 수.",
    )

    # 포워딩 트리거 시각 (UTC tz-aware). _utcnow 는 파일 내 공용 UTC 헬퍼.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="포워딩 트리거 시각 (UTC tz-aware).",
    )

    # 모든 send 완료 시각 (UTC tz-aware). 동기 루프라도 트리거/완료 시각이
    # 갈릴 수 있어 보존한다. 완료 전이거나 기록 생략 시 NULL.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="모든 send 완료 시각 (UTC tz-aware). 완료 전·기록 생략 시 NULL.",
    )

    # ── 단방향 relationship ──────────────────────────────────────────────────
    # EmailSendRun.requested_by 와 동일 패턴 — back_populates 없음, lazy="select".
    # 상대 모델 쪽에 collection 을 만들 명확한 활용 시나리오가 아직 없으므로
    # 단방향이 안전하다 (docs/phase_a2_part1_design_note.md §1-c).

    canonical_project: Mapped["CanonicalProject"] = relationship(
        "CanonicalProject",
        foreign_keys=[canonical_project_id],
        lazy="select",
    )

    sender_user: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[sender_user_id],
        lazy="select",
    )

    sender_organization: Mapped["Organization | None"] = relationship(
        "Organization",
        foreign_keys=[sender_organization_id],
        lazy="select",
    )

    __table_args__ = (
        # status enum DB 레벨 강제. SQLAlchemy 의 Enum(native_enum=False) 는
        # create_constraint=False 가 기본이라 CHECK 를 자동 추가하지 않으므로
        # 명시적으로 선언한다 (EmailSendRun / AnnouncementProgress 와 동일 패턴).
        # migration 파일에도 동일 CHECK 를 중복 선언한다.
        CheckConstraint(
            "status IN ('success', 'partial', 'failed')",
            name="ck_email_forward_logs_status",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<EmailForwardLog id={self.id} "
            f"canonical_project_id={self.canonical_project_id} "
            f"status={self.status!r} recipient_count={self.recipient_count}>"
        )


class EmailDailyReportStatus(StrEnum):
    """단체 Daily Report 발송 시도 1회의 최종 상태 (Phase A-3 / task 00125-2).

    한 번의 ``prepare_and_send_daily_report`` 호출이 EmailDailyReportRun row 를
    한 개 만든다. status 는 다음 5종을 거친다 (전이 흐름):

        IN_PROGRESS ──▶ SUCCESS / PARTIAL / FAILED / SKIPPED

    값:
        IN_PROGRESS  'in_progress' — INSERT 직후, 발송 루프 진행 중. row 가
                                     남아 있다는 사실이 \"트랜잭션 1단계 commit\"
                                     의 시그널 (이력 보존 + 동시성 가시화).
        SUCCESS      'success'     — 모든 수신자에게 발송 성공.
        PARTIAL      'partial'     — 일부 수신자 성공 / 일부 실패 혼재.
        FAILED       'failed'      — 모든 수신자 실패 또는 게이트 차단 등
                                     사전 단계에서 실패해 발송 자체가 안 됨.
        SKIPPED      'skipped'     — 누적 구간 내 scrape_snapshots 0건 등으로
                                     발송 자체를 skip. last_sent_at 갱신 없음
                                     (다음 잡이 같은 구간 + 신규 누적까지 처리).

    저장 형식: native_enum=False — Postgres ENUM 타입 미생성, CHECK constraint
    만 추가. EmailSendRunStatus / EmailForwardStatus 와 동일 패턴.
    """

    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class EmailDailyReportRun(Base):
    """단체 Daily Report 발송 시도 1회의 이력 (Phase A-3 / task 00125-2).

    docs/phase_a3_design_note.md §14 + phase_a3_prompt.md §"백엔드 변경 2" 인용.
    한 ``prepare_and_send_daily_report`` 호출이 본 row 1개를 만든다. 개별 수신자
    단위 발송 결과는 ``EmailSendRun`` 에 ``related_kind='daily_report'`` /
    ``related_id=<본 row.id>`` 로 연결된다 (EmailForwardLog 패턴과 동일 다형성
    관계).

    트랜잭션 3단계 (forwarding 의 prepare / run 분리 패턴 차용):
        1. row INSERT (status=IN_PROGRESS) + commit       (본 row 생성)
        2. 게이트 / 구간 계산 → SKIPPED / FAILED 분기 commit  또는 본 row 갱신
        3. 수신자별 발송 루프 + 최종 status / completed_at commit

    ``last_sent_at`` 의 single source of truth 는 SystemSetting 이며 본 row 의
    started_at MAX 로 대체하지 않는다 (디자인 노트 §0-2). 본 row 는 \"이력 전용\".

    시간 처리:
        - ``started_at`` / ``completed_at`` / ``aggregation_from`` /
          ``aggregation_to`` 모두 UTC tz-aware 로 저장 (PROJECT_NOTES 컨벤션 —
          \"DB 저장은 UTC, 표시 경계에서 KST\"). 화면 표시 직전에
          ``app.timezone.format_kst`` / Jinja2 ``kst_format`` 필터로 KST 변환.

    trigger 의 컬럼 타입:
        - String(20). 'scheduled' / 'manual_admin' / 'manual_test' 3 값만 들어
          가지만, CHECK constraint 는 두지 않는다 (EmailSendRun.related_kind /
          transport_type 과 동일한 확장 여유 패턴 — 디자인 노트 §0-3 의 \"본문
          디자인 골격만\" 원칙과 같은 결로 트리거 도메인도 확장 여유를 둔다).
    """

    __tablename__ = "email_daily_report_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 발송을 어떤 경로로 트리거했는지. 'scheduled'/'manual_admin'/'manual_test'.
    # CHECK constraint 없음 — 확장 여유 (EmailSendRun.related_kind 패턴).
    trigger: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        doc=(
            "트리거 종류. 'scheduled' / 'manual_admin' / 'manual_test'. CHECK "
            "constraint 없음 — 향후 새 트리거 추가 시 ALTER 없이 확장 가능."
        ),
    )

    # 발송 결과 enum. native_enum=False — Postgres ENUM 미생성, CHECK constraint
    # 만 추가 (EmailSendRunStatus / EmailForwardStatus 동일 패턴).
    status: Mapped[EmailDailyReportStatus] = mapped_column(
        Enum(
            EmailDailyReportStatus,
            name="email_daily_report_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
        ),
        nullable=False,
        doc=(
            "발송 결과. 'in_progress' / 'success' / 'partial' / 'failed' / "
            "'skipped' 중 하나. INSERT 직후 'in_progress' 로 채워진 뒤, 발송 "
            "루프 완료 시 최종 상태로 갱신된다."
        ),
    )

    # 누적 구간의 시작 (exclusive). SKIPPED 케이스에서 구간 자체가 결정되지 않은
    # 시점에 row 가 commit 되면 NULL 가능. 시작 결정 후에는 to 와 한 쌍으로 채워짐.
    aggregation_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "누적 구간 시작 시각 (UTC tz-aware, exclusive). last_sent_at 또는 "
            "fallback 의 결과. SKIPPED row 는 NULL 가능."
        ),
    )

    aggregation_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "누적 구간 끝 시각 (UTC tz-aware, inclusive). 통상 now_utc(). SKIPPED "
            "row 는 NULL 가능."
        ),
    )

    # 구간 내 scrape_snapshots row 수. 0 이면 SKIPPED, 그 외에는 aggregate 의
    # 입력 row 수와 일치.
    snapshot_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="누적 구간 내 scrape_snapshots row 수 (aggregate 입력 row 수).",
    )

    recipient_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="발송 대상 수신자 수 (admin email 또는 test recipient 1 등).",
    )

    success_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="발송 성공 수신자 수.",
    )

    failure_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="발송 실패 수신자 수.",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "사전 단계 실패 사유 또는 마지막 발송 시도의 에러 메시지. SUCCESS / "
            "SKIPPED row 는 NULL. 수신자별 상세 에러는 EmailSendRun.error_message "
            "에 보관된다 (forwarding 과 동일 책임 분리)."
        ),
    )

    # 발송 시도 시작 시각 (UTC tz-aware). server_default + Python default 양쪽
    # 둬, raw INSERT 호환 + ORM 우선 적용 모두 지원 (EmailSendRun 동일 패턴).
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc=(
            "본 run 트리거 시각 (UTC tz-aware). 표시 직전에 KST 변환 "
            "(app.timezone.format_kst / Jinja2 kst_format 필터)."
        ),
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "발송 루프 종료 시각 (UTC tz-aware). 트랜잭션 3단계 (최종 commit) "
            "시점에 채워진다. 진행 중이거나 사전 단계 실패 직전에는 NULL."
        ),
    )

    # manual 트리거 시 누가 눌렀는지 기록. scheduled 이면 NULL.
    # 사용자 탈퇴 시 SET NULL 로 row 자체는 이력으로 보존 (EmailForwardLog /
    # EmailSendRun 동일 패턴).
    requested_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_email_daily_report_runs_requested_by_user_id",
            ondelete="SET NULL",
        ),
        nullable=True,
        doc=(
            "manual 트리거 시 발송을 누른 사용자 PK. scheduled 트리거 또는 사용자 "
            "탈퇴 후에는 NULL."
        ),
    )

    # 관계 — admin UI 의 발송 이력 응답에서 username 을 끌어올 때 사용.
    # EmailSendRun.requested_by 와 동일 단방향 lazy='select' 패턴.
    requested_by: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[requested_by_user_id],
        lazy="select",
    )

    __table_args__ = (
        # status enum DB 레벨 강제 — SQLAlchemy 의 Enum(native_enum=False) 는
        # create_constraint=False 가 기본이라 CHECK 를 자동 생성하지 않으므로
        # 명시적으로 선언한다 (EmailSendRun / EmailForwardLog 와 동일 패턴).
        # migration 파일에도 동일 CHECK 를 중복 선언한다.
        CheckConstraint(
            "status IN ('in_progress', 'success', 'partial', 'failed', 'skipped')",
            name="ck_email_daily_report_runs_status",
        ),
        # 발송 이력 화면이 ORDER BY started_at DESC LIMIT 50 으로 조회하는 SQL 의
        # 핵심 인덱스. ascending 인덱스는 양방향 스캔이 가능해 DESC ORDER BY 도
        # 효율적이다 (EmailSendRun §5-3 결정 동일).
        Index(
            "ix_email_daily_report_runs_started_at",
            "started_at",
        ),
        # 실패/skipped 만 빠르게 필터링하는 보조 인덱스. 향후 활용.
        Index(
            "ix_email_daily_report_runs_status",
            "status",
        ),
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<EmailDailyReportRun id={self.id} trigger={self.trigger!r} "
            f"status={self.status!r} snapshot_count={self.snapshot_count} "
            f"recipient_count={self.recipient_count}>"
        )


__all__ = [
    "Base",
    "Announcement",
    "AnnouncementProgress",
    "AnnouncementProgressArchiveReason",
    "AnnouncementProgressHistory",
    "AnnouncementProgressStatus",
    "AnnouncementStatus",
    "AnnouncementUserState",
    "Attachment",
    "BackupHistory",
    "CanonicalProject",
    "DeltaAnnouncement",
    "DeltaAttachment",
    "EmailDailyReportRun",
    "EmailDailyReportStatus",
    "EmailForwardLog",
    "EmailForwardStatus",
    "EmailSendRun",
    "EmailSendRunStatus",
    "FavoriteEntry",
    "FavoriteFolder",
    "Organization",
    "RelevanceJudgment",
    "RelevanceJudgmentHistory",
    "ScrapeRun",
    "ScrapeSnapshot",
    "SCRAPE_RUN_STATUSES",
    "SCRAPE_RUN_TERMINAL_STATUSES",
    "SCRAPE_RUN_TRIGGERS",
    "SystemSetting",
    "User",
    "UserOrganization",
    "UserSession",
    "as_utc",
]
