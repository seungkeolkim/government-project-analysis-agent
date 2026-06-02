"""공지사항 게시판 ORM 모델 정의 (task 00056).

공지사항은 건의사항과 동일한 DB 파일(boards.sqlite3) 을 공유한다.
테이블을 같은 파일 안에 두기 위해 :class:`app.suggestions.models.Base` 를 그대로
가져와 ``Notice(Base)`` 로 선언한다 — 별도 Base / 엔진 / 세션 생성 금지.

건의사항(Suggestion) 대비 차이점:
    - 비밀글 없음 (``is_secret`` 컬럼 없음)
    - 비밀번호 없음 (``password_hash`` 컬럼 없음)
    - 수용 여부 / 예상 개발일 없음
    - 댓글 없음 (relationship 없음)
    - 글쓰기는 관리자 전용 — 모델 레벨 제약은 없고 라우트에서 강제

작성자 식별:
    ``author_user_id`` 는 메인 DB ``users.id`` 값을 보존한다. cross-DB FK 불가.
    관리자 전용 게시판이므로 author_user_id 가 사라지는 경우가 흔치 않아,
    suggestions 의 고아 정책(apply_orphan_policy_*)은 도입하지 않는다.
    대신 ``author_name`` 을 작성 시점에 저장해 그대로 표시한다.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.suggestions.models import BODY_FORMAT_PLAIN, Base, _utcnow


class Notice(Base):
    """공지사항 게시글 한 건.

    작성자명(``author_name``)은 작성 시점의 사용자 username 을 저장해 둔다.
    메인 DB reset 으로 사용자가 사라져도 표기명이 유지되므로 고아 정책 불필요.

    필수 입력: ``title``, ``body``, ``author_user_id``, ``author_name``.
    """

    __tablename__ = "notices"

    __table_args__ = (
        # 목록 화면은 최신 글 우선이라 created_at 정렬을 자주 쓴다.
        Index("ix_notices_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # 작성자 사용자 ID. 메인 DB users.id 와 매칭되지만 cross-DB 라 FK 불가.
    # NULL 허용 — 데이터 보존성 차원이며, 정상 흐름에서는 항상 정수가 들어온다.
    author_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
        doc="메인 DB users.id 값. cross-DB FK 불가능, 단순 정수 보존.",
    )

    # 작성 시점의 사용자명 저장 — 이후 메인 DB 가 바뀌어도 표기명 유지
    author_name: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="작성 시점 사용자명. 메인 DB 변경 후에도 표기명 보존용.",
    )

    # 제목
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="공지사항 제목.",
    )

    # 본문
    body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc="공지사항 본문. body_format 에 따라 평문 또는 정화된 HTML 이 저장된다.",
    )

    # 본문 저장 포맷 판별 (task 00153 — 리치 텍스트 도입).
    # 건의사항과 동일 정책: DB nullable + server_default('plain'), ORM NOT NULL.
    # 'plain' = 평문(자동 escape + pre-wrap), 'html' = 서버 정화된 리치 텍스트.
    body_format: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=BODY_FORMAT_PLAIN,
        server_default=BODY_FORMAT_PLAIN,
        doc="본문 저장 포맷. 'plain'=평문, 'html'=정화된 리치 텍스트. 기본값 'plain'.",
    )

    # 작성 시각 (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        doc="공지사항 작성 시각(UTC).",
    )

    # 마지막 갱신 시각. 최초 작성 시 NULL, 수정 시에만 onupdate 로 채워진다.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=_utcnow,
        doc="레코드 마지막 갱신 시각(UTC). NULL이면 수정 이력 없음.",
    )

    # 소프트 삭제 시각 (UTC). NULL이면 활성 레코드.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        doc="소프트 삭제 시각(UTC). NULL이면 활성 레코드, 값이 있으면 삭제된 레코드.",
    )

    def __repr__(self) -> str:
        """디버깅 편의용 문자열 표현을 반환한다."""
        return f"<Notice id={self.id} title={self.title[:20]!r}>"


__all__ = ["Notice"]
