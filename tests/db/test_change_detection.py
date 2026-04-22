"""변경 감지 4-branch + 2차 감지 + 리셋/이관 유닛 테스트 (Phase 1a).

사용자 원문 요구 검증:
    - 1차 감지 비교 필드(title/status/deadline_at/agency).
    - status 단독 전이는 in-place UPDATE + 리셋 없음.
    - 그 외 내용 변경 시 is_current 순환 + 리셋·이관.
    - 첨부 다운로드 후 2차 감지 → reapply_version_with_reset.

테스트 픽스처 규약:
    - 가짜 User(username='test_u1') INSERT 후 id 확보.
    - 공고 UPSERT 는 repository.upsert_announcement 로 실제 경로를 탄다.
    - AnnouncementUserState / RelevanceJudgment 는 직접 INSERT 해 '이미 읽음 /
      판정 존재' 상태를 만든다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    AnnouncementUserState,
    Attachment,
    RelevanceJudgment,
    RelevanceJudgmentHistory,
    User,
)
from app.db.repository import (
    compute_attachment_signature,
    detect_attachment_changes,
    reapply_version_with_reset,
    snapshot_announcement_attachments,
    upsert_announcement,
)


# ── fixture helpers ──────────────────────────────────────────────────────────

def _make_user(session: Session, username: str = "test_u1") -> User:
    """테스트용 User 를 하나 생성해 세션에 flush 하고 반환한다."""
    user = User(
        username=username,
        password_hash="dummy-hash",
        email=None,
        is_admin=False,
        created_at=datetime.now(tz=UTC),
    )
    session.add(user)
    session.flush()
    return user


def _payload_for(
    source_id: str,
    *,
    title: str = "원본 공고",
    status: AnnouncementStatus = AnnouncementStatus.RECEIVING,
    agency: str | None = "기관A",
    deadline_at: datetime | None = None,
    ancm_no: str | None = None,
) -> dict[str, Any]:
    """upsert_announcement payload 를 구성하는 헬퍼."""
    return {
        "source_announcement_id": source_id,
        "source_type": "IRIS",
        "title": title,
        "status": status,
        "agency": agency,
        "deadline_at": deadline_at or datetime(2026, 5, 31, tzinfo=UTC),
        "raw_metadata": {},
        "ancm_no": ancm_no or f"ANCM-{source_id}",
    }


def _seed_user_state_and_judgment(
    session: Session, user: User, announcement: Announcement, *, is_read: bool = True
) -> None:
    """공고 × 사용자 읽음 상태 + canonical 관련성 판정을 한 세트로 심는다."""
    session.add(
        AnnouncementUserState(
            announcement_id=announcement.id,
            user_id=user.id,
            is_read=is_read,
            read_at=datetime.now(tz=UTC) if is_read else None,
        )
    )
    assert announcement.canonical_group_id is not None, (
        "canonical_group_id 가 없으면 RelevanceJudgment FK 를 걸 수 없다. "
        "upsert_announcement 가 canonical 매칭에 실패한 경우 테스트 fixture 를 재검토하라."
    )
    session.add(
        RelevanceJudgment(
            canonical_project_id=announcement.canonical_group_id,
            user_id=user.id,
            verdict="관련",
            reason="유닛 테스트",
            decided_at=datetime.now(tz=UTC),
        )
    )
    session.flush()


# ── (1) test_created_new ─────────────────────────────────────────────────────

def test_created_new(db_session: Session) -> None:
    """기존 is_current row 가 없으면 신규 INSERT + action='created'."""
    result = upsert_announcement(db_session, _payload_for("CREATED-1"))

    assert result.action == "created"
    assert result.needs_detail_scraping is True
    assert result.changed_fields == frozenset()
    assert result.announcement.id is not None
    assert result.announcement.is_current is True

    # 한 건만 존재
    rows = list(
        db_session.execute(
            select(Announcement).where(
                Announcement.source_announcement_id == "CREATED-1"
            )
        ).scalars()
    )
    assert len(rows) == 1


# ── (2) test_unchanged_no_detail_rescrape ────────────────────────────────────

def test_unchanged_no_detail_rescrape(db_session: Session) -> None:
    """비교 필드 변경 없고 detail 도 이미 있으면 needs_detail_scraping=False."""
    payload = _payload_for("UNCH-1")
    first = upsert_announcement(db_session, payload)

    # detail 을 이미 수집한 것처럼 가장
    first.announcement.detail_html = "<p>detail</p>"
    first.announcement.detail_fetched_at = datetime.now(tz=UTC)
    first.announcement.detail_fetch_status = "ok"
    db_session.flush()

    # 같은 payload 로 재수집
    second = upsert_announcement(db_session, payload)

    assert second.action == "unchanged"
    assert second.needs_detail_scraping is False
    assert second.changed_fields == frozenset()
    # 동일한 row 를 가리켜야 한다 (신규 INSERT 없음)
    assert second.announcement.id == first.announcement.id


# ── (3) test_status_transitioned_no_reset ────────────────────────────────────

def test_status_transitioned_no_reset(db_session: Session) -> None:
    """status 단독 변경(접수중→마감) 은 in-place UPDATE + 리셋 안 됨.

    사용자 원문: 'status 단독 전이는 기존 in-place UPDATE 유지'.
    """
    user = _make_user(db_session)
    first = upsert_announcement(db_session, _payload_for("STTR-1"))
    announcement = first.announcement
    original_id = announcement.id
    canonical_id = announcement.canonical_group_id

    # 읽음 + 관련성 판정 심기
    _seed_user_state_and_judgment(db_session, user, announcement, is_read=True)

    # status 만 변경
    second = upsert_announcement(
        db_session,
        _payload_for("STTR-1", status=AnnouncementStatus.CLOSED),
    )

    assert second.action == "status_transitioned"
    assert second.changed_fields == frozenset({"status"})
    assert second.announcement.id == original_id, "status 전이는 in-place UPDATE"

    # 읽음 상태 유지 (리셋 안 됨)
    state = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == original_id,
            AnnouncementUserState.user_id == user.id,
        )
    ).scalar_one()
    assert state.is_read is True, "status 전이 시 읽음이 리셋되면 안 됨"

    # RelevanceJudgment 이관 안 됨 — 현재 유효 1건, History 0건
    active = list(
        db_session.execute(
            select(RelevanceJudgment).where(
                RelevanceJudgment.canonical_project_id == canonical_id,
                RelevanceJudgment.user_id == user.id,
            )
        ).scalars()
    )
    history = list(db_session.execute(select(RelevanceJudgmentHistory)).scalars())
    assert len(active) == 1
    assert len(history) == 0


# ── (4) test_new_version_triggers_reset ──────────────────────────────────────

def test_new_version_triggers_reset(db_session: Session) -> None:
    """내용 변경(title 등) → 기존 row 봉인 + 신규 INSERT + 사용자 라벨링 리셋.

    검증:
      - AnnouncementUserState.is_read: True → False, read_at=None
      - RelevanceJudgment: 원본 삭제 + History 에 1건 (archive_reason='content_changed')
    """
    user = _make_user(db_session)
    first = upsert_announcement(db_session, _payload_for("NV-1"))
    original_id = first.announcement.id
    canonical_id = first.announcement.canonical_group_id

    _seed_user_state_and_judgment(db_session, user, first.announcement, is_read=True)

    # title 변경
    second = upsert_announcement(
        db_session,
        _payload_for("NV-1", title="수정된 공고"),
    )

    assert second.action == "new_version"
    assert second.changed_fields == frozenset({"title"})
    assert second.announcement.id != original_id

    # 봉인된 구 row 확인
    old = db_session.get(Announcement, original_id)
    assert old is not None and old.is_current is False

    # 신규 row 는 canonical 승계
    assert second.announcement.canonical_group_id == canonical_id
    assert second.announcement.is_current is True

    # (a) 읽음 리셋
    state = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == original_id
        )
    ).scalar_one()
    assert state.is_read is False
    assert state.read_at is None

    # (b) 관련성 판정 이관
    active = list(db_session.execute(select(RelevanceJudgment)).scalars())
    history = list(db_session.execute(select(RelevanceJudgmentHistory)).scalars())
    assert len(active) == 0, "원본 RelevanceJudgment 삭제 안 됨"
    assert len(history) == 1
    hist_row = history[0]
    assert hist_row.archive_reason == "content_changed"
    assert hist_row.canonical_project_id == canonical_id
    assert hist_row.user_id == user.id


# ── (5) test_attachment_signature_change_triggers_second_pass ────────────────

def test_attachment_signature_change_triggers_second_pass(db_session: Session) -> None:
    """첨부 sha256 변경 시 detect_attachment_changes → reapply_version_with_reset.

    흐름:
      - 기존 첨부 2개 심기 (sha_old_1, sha_old_2)
      - before signature 캡처
      - 첨부 1개의 sha 를 sha_new_1 로 교체
      - after signature 캡처 → detect → changed=True
      - reapply_version_with_reset → 봉인 + 신규 row + 리셋
    """
    user = _make_user(db_session)
    first = upsert_announcement(db_session, _payload_for("SEC-1"))
    announcement = first.announcement
    original_id = announcement.id
    canonical_id = announcement.canonical_group_id

    # 사용자 상태/판정 + 첨부 2개
    _seed_user_state_and_judgment(db_session, user, announcement, is_read=True)
    db_session.add(
        Attachment(
            announcement_id=original_id,
            original_filename="a.pdf",
            stored_path="/tmp/a.pdf",
            file_ext="pdf",
            sha256="sha_old_1",
            downloaded_at=datetime.now(tz=UTC),
        )
    )
    db_session.add(
        Attachment(
            announcement_id=original_id,
            original_filename="b.pdf",
            stored_path="/tmp/b.pdf",
            file_ext="pdf",
            sha256="sha_old_2",
            downloaded_at=datetime.now(tz=UTC),
        )
    )
    db_session.flush()

    # before signature 캡처 (ORM 관계 로드 후)
    db_session.refresh(announcement, ["attachments"])
    signature_before = compute_attachment_signature(announcement)
    assert signature_before.count == 2
    assert signature_before.sha256s == frozenset({"sha_old_1", "sha_old_2"})

    # 첨부 a.pdf 의 sha 를 교체
    att_a = db_session.execute(
        select(Attachment).where(
            Attachment.announcement_id == original_id,
            Attachment.original_filename == "a.pdf",
        )
    ).scalar_one()
    att_a.sha256 = "sha_new_1"
    db_session.flush()

    # after signature + 비교
    signature_after = snapshot_announcement_attachments(db_session, original_id)
    change = detect_attachment_changes(signature_before, signature_after)
    assert change.changed
    assert change.added == frozenset({"sha_new_1"})
    assert change.removed == frozenset({"sha_old_1"})
    assert change.count_changed is False

    # reapply_version_with_reset → is_current 순환 + 리셋
    result = reapply_version_with_reset(db_session, original_id)
    assert result.action == "new_version"
    assert result.needs_detail_scraping is False
    assert result.changed_fields == frozenset({"attachments"})
    assert result.announcement.id != original_id
    assert result.announcement.canonical_group_id == canonical_id

    # 구 row 봉인
    old = db_session.get(Announcement, original_id)
    assert old is not None and old.is_current is False

    # 읽음 리셋
    state = db_session.execute(
        select(AnnouncementUserState).where(
            AnnouncementUserState.announcement_id == original_id
        )
    ).scalar_one()
    assert state.is_read is False

    # 관련성 판정 이관
    active = list(db_session.execute(select(RelevanceJudgment)).scalars())
    history = list(db_session.execute(select(RelevanceJudgmentHistory)).scalars())
    assert len(active) == 0
    assert len(history) == 1
    assert history[0].archive_reason == "content_changed"


# ── 보조: 가짜 User 없이도 리셋이 no-op 으로 안전해야 함 ────────────────────

def test_new_version_reset_is_safe_without_users(db_session: Session) -> None:
    """User 테이블이 비어 있어도 new_version 리셋이 예외 없이 no-op."""
    first = upsert_announcement(db_session, _payload_for("EMPTY-1"))
    second = upsert_announcement(
        db_session,
        _payload_for("EMPTY-1", title="다른제목"),
    )
    assert second.action == "new_version"
    # 사용자 없으므로 AnnouncementUserState / RelevanceJudgment 0건 유지
    assert (
        len(list(db_session.execute(select(AnnouncementUserState)).scalars())) == 0
    )
    assert len(list(db_session.execute(select(RelevanceJudgment)).scalars())) == 0
    assert (
        len(list(db_session.execute(select(RelevanceJudgmentHistory)).scalars())) == 0
    )
