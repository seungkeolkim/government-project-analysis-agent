"""건의사항 게시판 ORM 모델 정의 (별도 DB 파일 전용).

본 모듈은 건의사항 게시판(task 00051) 전용 declarative base 와 모델을 정의한다.
메인 DB(app.sqlite3) 와 격리된 별도 SQLite 파일에 저장되며, 메인 DB 의 reset
이 게시글 데이터에 영향을 주지 않도록 설계되었다.

설계 메모:
    - 별도 DB 파일을 쓰므로 메인 DB 의 ``users`` 테이블에 cross-DB FK 를 걸 수 없다.
      대신 ``Suggestion.author_user_id`` / ``SuggestionComment.author_user_id`` 는
      단순 정수 컬럼으로 ``users.id`` 값을 보존한다. "이 author_user_id 가 메인 DB
      에 살아있는가" 판정은 :mod:`app.suggestions.author_validity` 의 batch
      헬퍼가 담당한다.
    - 메인 DB 가 reset 되어 author_user_id 가 가리키던 user row 가 사라져도
      게시글 자체 row 는 그대로 보존된다 — 단지 "작성자 정보" 가 분리되어
      사라진 형태가 된다. 라우트 단계에서 cross-DB 헬퍼로 alive 여부를
      판정한 뒤 (a) 비관리자에게는 결과에서 제외, (b) 관리자에게는 작성자명을
      NULL 로 마스킹한다.
    - SQLite/Postgres 양쪽에서 동일하게 동작하도록 ``JSON`` 타입을 쓰지 않고,
      Enum 은 ``native_enum=False`` 로 선언해 SQLite 의 CHECK 제약 + Postgres
      의 VARCHAR 양쪽 호환을 확보한다. ``batch_alter_table`` (Alembic) 호환을
      위해 named constraint 를 사용한다.
    - ``suggestion_comments.suggestion_id`` 는 ON DELETE CASCADE 로 게시글 삭제
      시 댓글도 함께 정리된다.
    - ``acceptance_status`` 는 관리자가 입력한 값만 의미가 있으며 기본값은
      "검토중" — 미입력 상태를 화면에 그대로 보여주기 위함이다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
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
    """현재 시각을 timezone-aware UTC ``datetime`` 으로 반환한다.

    SQLAlchemy ``default``/``onupdate`` 콜러블로 사용한다.
    """
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """건의사항 게시판 전용 declarative base.

    메인 DB 의 :class:`app.db.models.Base` 와 ``metadata`` 가 분리되어 있다.
    덕분에 ``Base.metadata.create_all(engine)`` 로 본 패키지의 테이블만 따로
    생성할 수 있고, Alembic 이 메인 DB 마이그레이션을 돌릴 때도 본 패키지의
    테이블이 영향받지 않는다.
    """


class AcceptanceStatus(StrEnum):
    """관리자가 부여하는 건의사항 수용 상태.

    값은 한글 원문을 그대로 사용해 화면 표시와 1:1 로 매칭한다. 기본값은
    "검토중" 으로, 관리자가 모달을 통해 결정을 내리기 전까지 유지된다.
    """

    PENDING = "검토중"
    ACCEPTED = "수용"
    PARTIAL = "일부수용"
    REJECTED = "거절"


class Suggestion(Base):
    """건의사항 게시글 한 건.

    필수 입력: ``title``, ``body``, ``password_hash``, ``is_secret``.
    선택 입력: ``author_name``, ``contact_email``.

    관리자 전용 필드: ``acceptance_status``, ``acceptance_reason``,
    ``expected_completion_date``. 모두 게시글 생성 시점에는 기본값으로 채워지고
    이후 관리자가 모달을 통해 수정한다.

    작성자 식별:
        ``author_user_id`` 는 메인 DB ``users.id`` 값을 보존한다. 별도 DB 파일이라
        FK 를 걸 수 없으므로 alive 여부 판정은
        :func:`app.suggestions.author_validity.get_alive_user_ids` 가 batch
        쿼리로 수행한다. ``None`` 인 경우는 정상 흐름에서는 발생하지 않지만
        (작성 시 항상 로그인 필수), 데이터 보존성 차원에서 NULL 도 허용하고
        라우트에서 "alive 도 orphan 도 아닌 별 케이스" 로 처리한다.
    """

    __tablename__ = "suggestions"

    __table_args__ = (
        # 목록 화면은 최신 글 우선이라 created_at 정렬을 자주 쓴다.
        Index("ix_suggestions_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # 작성자 사용자 ID. 메인 DB users.id 값과 매칭되지만 cross-DB 라 FK 불가.
    # NULL 허용 — 메인 DB reset 등으로 user 가 사라진 후 표시 정합성 차원에서
    # 별 케이스로 처리한다(가시성은 관리자만, 작성자 표시는 NULL).
    author_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
        doc="메인 DB users.id 값. cross-DB FK 불가능, 단순 정수 보존.",
    )

    # 선택 입력: 작성자명 (로그인 사용자명과 별개로 표기명 입력 가능)
    author_name: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="선택 입력 작성자명. 로그인 username 과 별개의 표기명.",
    )

    # 선택 입력: 상세 피드백 인터렉션을 위한 연락처 이메일
    contact_email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="선택 입력 연락처 이메일.",
    )

    # 필수 입력: 제목
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="게시글 제목.",
    )

    # 필수 입력: 본문
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="게시글 본문.",
    )

    # 필수 입력: 게시글별 비밀번호 (향후 수정·삭제 권한용으로 해시 저장)
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="게시글별 비밀번호의 해시 결과 문자열. 평문 저장 금지.",
    )

    # 필수 입력: 비밀글 여부
    is_secret: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        doc="비밀글 여부. True 면 작성자 본인 또는 관리자만 본문을 열람할 수 있다.",
    )

    # 관리자가 부여하는 수용 상태. 기본값 "검토중".
    acceptance_status: Mapped[AcceptanceStatus] = mapped_column(
        Enum(
            AcceptanceStatus,
            name="suggestion_acceptance_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
        ),
        nullable=False,
        default=AcceptanceStatus.PENDING,
        server_default=AcceptanceStatus.PENDING.value,
        doc="관리자가 부여하는 수용 상태. 기본값은 검토중.",
    )

    # 관리자 사유 텍스트 (수용·일부수용·거절 어느 분기든 입력 가능)
    acceptance_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="관리자가 입력한 수용 결정 사유.",
    )

    # 수용 / 일부 수용일 때만 입력하는 예상 완료일 (캘린더)
    expected_completion_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        doc="관리자 수용 시 예상 처리 완료일. 거절·검토중인 경우 NULL.",
    )

    # 작성 시각 (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="게시글 작성 시각(UTC).",
    )

    # 마지막 갱신 시각 (관리자 수용여부 수정 등으로 갱신)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="레코드 마지막 갱신 시각(UTC).",
    )

    # 소프트 삭제 시각 (UTC). NULL이면 활성 레코드.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="소프트 삭제 시각(UTC). NULL이면 활성 레코드, 값이 있으면 삭제된 레코드.",
    )

    # 댓글과의 1:N 관계. 게시글 삭제 시 댓글도 함께 정리한다.
    comments: Mapped[list[SuggestionComment]] = relationship(
        "SuggestionComment",
        back_populates="suggestion",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<Suggestion id={self.id} title={self.title[:20]!r} "
            f"is_secret={self.is_secret} status={self.acceptance_status.value!r}>"
        )


class SuggestionComment(Base):
    """건의사항 게시글에 달린 댓글 한 건.

    대댓글은 지원하지 않는다(task 00051 원문 — "대댓글 기능은 우선 없이 하자").
    작성은 로그인 사용자에 한해 허용되지만, 메인 DB reset 으로 author_user_id
    가 가리키던 user 가 사라질 수 있으므로 컬럼은 NULL 허용으로 둔다.
    """

    __tablename__ = "suggestion_comments"

    __table_args__ = (
        # 게시글 단위 댓글 목록 조회를 위한 보조 인덱스 (시간 정렬 포함).
        Index(
            "ix_suggestion_comments_suggestion_created",
            "suggestion_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # 소속 게시글 FK. 게시글 삭제 시 댓글도 같이 제거.
    suggestion_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "suggestions.id",
            name="fk_suggestion_comments_suggestion_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
        doc="소속 건의사항 게시글의 PK.",
    )

    # 작성자 사용자 ID. 메인 DB users.id 와 매칭되지만 cross-DB 라 FK 불가.
    # NULL 허용 — 메인 DB reset 후 표시 정합성 차원에서 별 케이스로 처리한다.
    author_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
        doc="댓글 작성자 사용자 ID. cross-DB FK 불가능, 단순 정수 보존.",
    )

    # 댓글 본문
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="댓글 본문.",
    )

    # 작성 시각 (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="댓글 작성 시각(UTC).",
    )

    # 마지막 갱신 시각 (UTC). 작성 시 created_at 과 동일, 본문 수정 시 onupdate 로 자동 갱신.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        doc="댓글 마지막 갱신 시각(UTC). 작성 시 created_at 과 동일, 본문 수정 시 onupdate 로 자동 갱신.",
    )

    # 소프트 삭제 시각 (UTC). NULL이면 활성 레코드.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="소프트 삭제 시각(UTC). NULL이면 활성 레코드, 값이 있으면 삭제된 레코드.",
    )

    # 역관계 (Suggestion.comments 와 대응)
    suggestion: Mapped[Suggestion] = relationship(
        "Suggestion",
        back_populates="comments",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return (
            f"<SuggestionComment id={self.id} suggestion_id={self.suggestion_id} "
            f"author_user_id={self.author_user_id}>"
        )


__all__ = [
    "Base",
    "AcceptanceStatus",
    "Suggestion",
    "SuggestionComment",
]
