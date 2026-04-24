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

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
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
    """canonical_project × user 단위 현재 유효 판정.

    (canonical_project_id, user_id) 조합당 1건만 존재한다.
    내용 변경(status 단독 제외) 감지 시 해당 canonical 의 모든 row 는
    `relevance_judgment_history` 로 이관되어 이 테이블에서 제거된다.

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
        UniqueConstraint(
            "canonical_project_id",
            "user_id",
            name="uq_relevance_project_user",
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
            f"verdict={self.verdict!r}>"
        )


class RelevanceJudgmentHistory(Base):
    """이관된 과거 판정.

    새 판정/내용 변경 시 `relevance_judgments` 의 row 를 이 테이블로 복사한다.
    이 테이블은 append-only 로 취급하며, 이관된 레코드는 수정하지 않는다.

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


__all__ = [
    "Base",
    "Announcement",
    "AnnouncementStatus",
    "AnnouncementUserState",
    "Attachment",
    "CanonicalProject",
    "FavoriteEntry",
    "FavoriteFolder",
    "RelevanceJudgment",
    "RelevanceJudgmentHistory",
    "ScrapeRun",
    "SCRAPE_RUN_STATUSES",
    "SCRAPE_RUN_TERMINAL_STATUSES",
    "SCRAPE_RUN_TRIGGERS",
    "User",
    "UserSession",
    "as_utc",
]
