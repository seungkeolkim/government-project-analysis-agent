"""리셋 중 예외 → UPSERT 도 함께 롤백되는 atomic 경계 검증 (Phase 1a).

사용자 원문: '사용자 라벨링이 유일한 돌이킬 수 없는 자산'.
리셋 + UPSERT 는 반드시 같은 트랜잭션에서 원자적으로 처리되어야 한다 —
리셋 도중 예외가 발생하면 '신규 row 생성' 과 '구 row is_current=False 봉인' 모두
롤백되어 DB 에는 원본 상태만 남아야 한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.models import (
    Announcement,
    AnnouncementStatus,
    AnnouncementUserState,
    RelevanceJudgment,
    RelevanceJudgmentHistory,
    User,
)
from app.db.session import session_scope
from app.db import repository as repo_mod
from app.db.repository import upsert_announcement


def test_reset_exception_rolls_back_upsert(
    test_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_reset_user_state_on_content_change 가 예외를 던지면 UPSERT 도 함께 롤백.

    시나리오:
      1. User + 공고(CREATED) + 사용자 상태(is_read=True) + 관련성 판정을 커밋.
      2. monkeypatch 로 _reset_user_state_on_content_change 를 강제 예외로 교체.
      3. 같은 공고에 title 을 바꾼 payload 로 UPSERT 시도 → new_version 분기 진입 →
         리셋 호출 지점에서 RuntimeError.
      4. session_scope 가 rollback 하여 DB 는 원본 상태만 남아야 한다:
         - Announcement row 1건 (is_current=True, title 원본)
         - AnnouncementUserState.is_read=True 유지
         - RelevanceJudgment 1건 유지, History 0건
    """

    # ── (1) fixture 준비 + commit ──────────────────────────────────────────
    with session_scope() as setup_session:
        user = User(
            username="atomic_user",
            password_hash="dummy",
            email=None,
            is_admin=False,
            created_at=datetime.now(tz=UTC),
        )
        setup_session.add(user)
        setup_session.flush()
        user_id = user.id

        first = upsert_announcement(
            setup_session,
            {
                "source_announcement_id": "ATOMIC-1",
                "source_type": "IRIS",
                "title": "원본 공고",
                "status": AnnouncementStatus.RECEIVING,
                "agency": "기관A",
                "deadline_at": datetime(2026, 5, 31, tzinfo=UTC),
                "raw_metadata": {},
                "ancm_no": "ATOMIC-NO",
            },
        )
        original_id = first.announcement.id
        canonical_id = first.announcement.canonical_group_id

        setup_session.add(
            AnnouncementUserState(
                announcement_id=original_id,
                user_id=user_id,
                is_read=True,
                read_at=datetime.now(tz=UTC),
            )
        )
        setup_session.add(
            RelevanceJudgment(
                canonical_project_id=canonical_id,
                user_id=user_id,
                verdict="관련",
                reason="atomic test",
                decided_at=datetime.now(tz=UTC),
            )
        )
        # session_scope 가 commit 까지 처리

    # ── (2) 리셋 함수에 예외 주입 ──────────────────────────────────────────
    def _broken_reset(*_args, **_kwargs):
        raise RuntimeError("시뮬레이션: 리셋 실패")

    monkeypatch.setattr(
        repo_mod, "_reset_user_state_on_content_change", _broken_reset
    )

    # ── (3) 같은 공고에 title 변경으로 UPSERT 시도 → 예외 발생 기대 ────────
    with pytest.raises(RuntimeError, match="시뮬레이션: 리셋 실패"):
        with session_scope() as broken_session:
            upsert_announcement(
                broken_session,
                {
                    "source_announcement_id": "ATOMIC-1",
                    "source_type": "IRIS",
                    "title": "수정된 공고 (커밋되면 안 됨)",
                    "status": AnnouncementStatus.RECEIVING,
                    "agency": "기관A",
                    "deadline_at": datetime(2026, 5, 31, tzinfo=UTC),
                    "raw_metadata": {},
                    "ancm_no": "ATOMIC-NO",
                },
            )

    # ── (4) 롤백 검증 — 새 세션에서 DB 실제 상태 조회 ──────────────────────
    with session_scope() as check_session:
        rows = list(
            check_session.execute(
                select(Announcement)
                .where(Announcement.source_announcement_id == "ATOMIC-1")
                .order_by(Announcement.id.asc())
            ).scalars()
        )
        assert len(rows) == 1, (
            f"롤백 실패 — rows={len(rows)} "
            f"(UPSERT 의 봉인·신규 INSERT 가 rollback 되지 않음)"
        )
        assert rows[0].id == original_id
        assert rows[0].is_current is True
        assert rows[0].title == "원본 공고", (
            "rollback 이 되지 않아 신규 row 가 반영됐거나 in-place UPDATE 가 됨"
        )

        state = check_session.execute(
            select(AnnouncementUserState).where(
                AnnouncementUserState.announcement_id == original_id
            )
        ).scalar_one()
        assert state.is_read is True, "읽음 리셋이 rollback 되지 않음"

        active = list(
            check_session.execute(select(RelevanceJudgment)).scalars()
        )
        history = list(
            check_session.execute(select(RelevanceJudgmentHistory)).scalars()
        )
        assert len(active) == 1, "RelevanceJudgment 가 삭제되었으나 롤백되지 않음"
        assert len(history) == 0, "이관된 RelevanceJudgmentHistory 가 롤백되지 않음"
