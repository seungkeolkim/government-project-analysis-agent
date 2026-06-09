"""정렬 로직 단위 테스트 (task 00158-1).

검증 대상:
    - app/db/repository.py 의 _apply_sort_order, _group_row_sort_key,
      _ALLOWED_SORT_VALUES, list_announcements 기본 정렬값.
    - app/web/main.py 의 _coerce_sort_query, _ALLOWED_SORT_VALUES.

정렬키 명명 규약 (00158-2 프론트 헤더 링크가 이 이름을 그대로 사용한다):
    공고 수집일 = collected_desc / collected_asc   (COALESCE(updated_at, scraped_at))
    모집 시작일 = received_desc  / received_asc    (received_at)
    모집 마감일 = deadline_desc  / deadline_asc    (deadline_at)
    기본값      = collected_desc
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db.models import Announcement, AnnouncementStatus
from app.db.repository import (
    _ALLOWED_SORT_VALUES as REPO_ALLOWED_SORT_VALUES,
    _group_row_sort_key,
    list_announcements,
)
from app.web.main import (
    _ALLOWED_SORT_VALUES as WEB_ALLOWED_SORT_VALUES,
    _coerce_sort_query,
)


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _dt(offset_hours: int) -> datetime:
    """_BASE_TIME 에서 offset_hours 만큼 더한 datetime 을 반환한다."""
    return _BASE_TIME + timedelta(hours=offset_hours)


def _make_ann(
    *,
    source_id: str,
    title: str = "공고",
    updated_at: datetime | None = None,
    scraped_at: datetime | None = None,
    received_at: datetime | None = None,
    deadline_at: datetime | None = None,
    status: AnnouncementStatus | None = None,
) -> SimpleNamespace:
    """_group_row_sort_key 테스트용 경량 공고 객체를 생성한다.

    SQLAlchemy ORM 없이 필요한 속성만 갖는 SimpleNamespace 를 반환한다.
    nullable=False 제약은 DB 레벨이므로 여기서는 None 을 허용해 fallback 경로를 테스트할 수 있다.
    """
    return SimpleNamespace(
        id=abs(hash(source_id)) % 10000 + 1,
        title=title,
        updated_at=updated_at,
        scraped_at=scraped_at,
        received_at=received_at,
        deadline_at=deadline_at,
        status=status,
    )


# ---------------------------------------------------------------------------
# _coerce_sort_query 단위 테스트
# ---------------------------------------------------------------------------


class TestCoerceSortQuery:
    """_coerce_sort_query 의 기본값 및 허용값 검증."""

    def test_none_returns_collected_desc(self) -> None:
        """sort 미지정(None) 시 기본값이 collected_desc 이어야 한다."""
        assert _coerce_sort_query(None) == "collected_desc"

    def test_empty_string_returns_collected_desc(self) -> None:
        """빈 문자열 입력 시 기본값이 collected_desc 이어야 한다."""
        assert _coerce_sort_query("") == "collected_desc"

    def test_whitespace_returns_collected_desc(self) -> None:
        """공백 문자열 입력 시 기본값이 collected_desc 이어야 한다."""
        assert _coerce_sort_query("   ") == "collected_desc"

    @pytest.mark.parametrize(
        "sort_val",
        [
            "collected_desc",
            "collected_asc",
            "received_desc",
            "received_asc",
            "deadline_desc",
            "deadline_asc",
            "status_asc",
            "status_desc",
            "title_asc",
        ],
    )
    def test_allowed_values_pass_through(self, sort_val: str) -> None:
        """허용 값은 그대로 반환되어야 한다."""
        assert _coerce_sort_query(sort_val) == sort_val

    def test_unknown_value_raises_400(self) -> None:
        """허용 목록에 없는 값은 HTTPException(400) 을 발생시켜야 한다."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _coerce_sort_query("unknown_sort")
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# _ALLOWED_SORT_VALUES 동기화 테스트
# ---------------------------------------------------------------------------


class TestAllowedSortValuesSync:
    """repository._ALLOWED_SORT_VALUES 와 main._ALLOWED_SORT_VALUES 가 일치하는지 확인한다."""

    def test_repo_and_web_allowed_values_are_in_sync(self) -> None:
        """두 모듈의 허용 정렬값 집합이 동일해야 한다."""
        assert frozenset(WEB_ALLOWED_SORT_VALUES) == REPO_ALLOWED_SORT_VALUES

    def test_collected_keys_present(self) -> None:
        """collected_desc / collected_asc 가 허용 목록에 포함되어야 한다."""
        assert "collected_desc" in REPO_ALLOWED_SORT_VALUES
        assert "collected_asc" in REPO_ALLOWED_SORT_VALUES

    def test_received_bidirectional(self) -> None:
        """received_desc / received_asc 양방향이 모두 포함되어야 한다."""
        assert "received_desc" in REPO_ALLOWED_SORT_VALUES
        assert "received_asc" in REPO_ALLOWED_SORT_VALUES

    def test_deadline_bidirectional(self) -> None:
        """deadline_desc / deadline_asc 양방향이 모두 포함되어야 한다."""
        assert "deadline_desc" in REPO_ALLOWED_SORT_VALUES
        assert "deadline_asc" in REPO_ALLOWED_SORT_VALUES

    def test_status_bidirectional(self) -> None:
        """status_asc / status_desc 양방향이 모두 포함되어야 한다."""
        assert "status_asc" in REPO_ALLOWED_SORT_VALUES
        assert "status_desc" in REPO_ALLOWED_SORT_VALUES


# ---------------------------------------------------------------------------
# _group_row_sort_key 단위 테스트 (Python 정렬 경로)
# ---------------------------------------------------------------------------


class TestGroupRowSortKey:
    """_group_row_sort_key 의 Python 정렬 키 생성 검증."""

    def test_collected_desc_orders_by_updated_at(self) -> None:
        """collected_desc: updated_at 최신 항목이 앞에 정렬되어야 한다."""
        ann_old = _make_ann(source_id="old", updated_at=_dt(1), scraped_at=_dt(0))
        ann_new = _make_ann(source_id="new", updated_at=_dt(5), scraped_at=_dt(0))

        key_old = _group_row_sort_key(ann_old, "collected_desc")
        key_new = _group_row_sort_key(ann_new, "collected_desc")

        # desc 이므로 최신(key_new) 이 더 작은 tuple 값 → sorted() 에서 앞에 온다
        assert key_new < key_old

    def test_collected_asc_orders_by_updated_at(self) -> None:
        """collected_asc: updated_at 오래된 항목이 앞에 정렬되어야 한다."""
        ann_old = _make_ann(source_id="old", updated_at=_dt(1), scraped_at=_dt(0))
        ann_new = _make_ann(source_id="new", updated_at=_dt(5), scraped_at=_dt(0))

        key_old = _group_row_sort_key(ann_old, "collected_asc")
        key_new = _group_row_sort_key(ann_new, "collected_asc")

        # asc 이므로 오래된(key_old) 이 더 작은 tuple 값 → sorted() 에서 앞에 온다
        assert key_old < key_new

    def test_collected_fallback_to_scraped_at_when_updated_at_is_none(self) -> None:
        """collected_*: updated_at 이 None 이면 scraped_at 으로 fallback 해야 한다."""
        # updated_at=None, scraped_at=_dt(3) → 수집일 = _dt(3)
        ann_fallback = _make_ann(
            source_id="fallback",
            updated_at=None,
            scraped_at=_dt(3),
        )
        # updated_at=_dt(3) → 수집일 = _dt(3) (동일)
        ann_direct = _make_ann(
            source_id="direct",
            updated_at=_dt(3),
            scraped_at=_dt(0),
        )
        # 수집일이 동일하므로 null_flag(0) 도 동일. id tiebreak 만 다름.
        # 두 키의 null_flag(첫 원소)와 sort_value(두 번째 원소)가 같아야 한다.
        key_fallback = _group_row_sort_key(ann_fallback, "collected_desc")
        key_direct = _group_row_sort_key(ann_direct, "collected_desc")

        assert key_fallback[0] == 0, "null_flag 이 0 이어야 한다 (값 있음)"
        assert key_direct[0] == 0
        assert key_fallback[1] == key_direct[1], "scraped_at fallback 값이 updated_at 과 동일해야 한다"

    def test_collected_none_sorts_last(self) -> None:
        """collected_desc: updated_at, scraped_at 모두 None 이면 맨 뒤로 보내야 한다."""
        ann_null = _make_ann(source_id="null_ann", updated_at=None, scraped_at=None)
        ann_valid = _make_ann(source_id="valid_ann", updated_at=_dt(1), scraped_at=_dt(0))

        key_null = _group_row_sort_key(ann_null, "collected_desc")
        key_valid = _group_row_sort_key(ann_valid, "collected_desc")

        assert key_null > key_valid, "NULL 수집일은 유효값보다 뒤에 와야 한다"

    def test_received_asc_orders_ascending(self) -> None:
        """received_asc: received_at 오름차순 정렬이어야 한다."""
        ann_early = _make_ann(source_id="early", received_at=_dt(1))
        ann_late = _make_ann(source_id="late", received_at=_dt(5))

        key_early = _group_row_sort_key(ann_early, "received_asc")
        key_late = _group_row_sort_key(ann_late, "received_asc")

        assert key_early < key_late

    def test_deadline_desc_orders_descending(self) -> None:
        """deadline_desc: deadline_at 내림차순 정렬이어야 한다."""
        ann_near = _make_ann(source_id="near", deadline_at=_dt(2))
        ann_far = _make_ann(source_id="far", deadline_at=_dt(10))

        key_near = _group_row_sort_key(ann_near, "deadline_desc")
        key_far = _group_row_sort_key(ann_far, "deadline_desc")

        # desc 이므로 멀리 있는(key_far) 이 더 작은 tuple 값 → sorted() 에서 앞에
        assert key_far < key_near

    def test_status_asc_orders_scheduled_receiving_closed(self) -> None:
        """status_asc: 접수예정 → 접수중 → 마감 우선순위 순으로 정렬되어야 한다."""
        ann_scheduled = _make_ann(
            source_id="scheduled", status=AnnouncementStatus.SCHEDULED
        )
        ann_receiving = _make_ann(
            source_id="receiving", status=AnnouncementStatus.RECEIVING
        )
        ann_closed = _make_ann(source_id="closed", status=AnnouncementStatus.CLOSED)

        key_scheduled = _group_row_sort_key(ann_scheduled, "status_asc")
        key_receiving = _group_row_sort_key(ann_receiving, "status_asc")
        key_closed = _group_row_sort_key(ann_closed, "status_asc")

        # 접수예정 < 접수중 < 마감 (tuple 값이 작을수록 sorted() 에서 앞에 온다)
        assert key_scheduled < key_receiving < key_closed

    def test_status_desc_reverses_order(self) -> None:
        """status_desc: 마감 → 접수중 → 접수예정 역순으로 정렬되어야 한다."""
        ann_scheduled = _make_ann(
            source_id="scheduled", status=AnnouncementStatus.SCHEDULED
        )
        ann_receiving = _make_ann(
            source_id="receiving", status=AnnouncementStatus.RECEIVING
        )
        ann_closed = _make_ann(source_id="closed", status=AnnouncementStatus.CLOSED)

        key_scheduled = _group_row_sort_key(ann_scheduled, "status_desc")
        key_receiving = _group_row_sort_key(ann_receiving, "status_desc")
        key_closed = _group_row_sort_key(ann_closed, "status_desc")

        # 마감 < 접수중 < 접수예정 (역순)
        assert key_closed < key_receiving < key_scheduled


# ---------------------------------------------------------------------------
# list_announcements 기본 정렬값 통합 테스트 (DB 경로)
# ---------------------------------------------------------------------------


class TestListAnnouncementsDefaultSort:
    """list_announcements 의 기본 sort 인자가 collected_desc 인지 확인한다.

    DB 에 공고 3건을 inserted 해서 updated_at 순서대로 정렬되는지 검증한다.
    """

    def _insert_announcement(
        self,
        session: Session,
        *,
        source_id: str,
        updated_at: datetime,
        scraped_at: datetime,
    ) -> Announcement:
        """테스트용 Announcement 를 DB 에 삽입하고 반환한다."""
        ann = Announcement(
            source_announcement_id=source_id,
            source_type="IRIS",
            title=f"공고 {source_id}",
            status=AnnouncementStatus.RECEIVING,
            is_current=True,
        )
        # ORM default 를 우회해 명시적 시각 설정
        ann.updated_at = updated_at
        ann.scraped_at = scraped_at
        session.add(ann)
        session.flush()
        return ann

    def test_default_sort_is_collected_desc(self, db_session: Session) -> None:
        """sort 미지정 시 공고 수집(업데이트)일 내림차순으로 정렬되어야 한다."""
        t1 = _dt(1)  # 가장 오래됨
        t2 = _dt(5)  # 중간
        t3 = _dt(10)  # 가장 최신

        ann1 = self._insert_announcement(
            db_session, source_id="ann1", updated_at=t1, scraped_at=t1
        )
        ann2 = self._insert_announcement(
            db_session, source_id="ann2", updated_at=t2, scraped_at=t2
        )
        ann3 = self._insert_announcement(
            db_session, source_id="ann3", updated_at=t3, scraped_at=t3
        )
        db_session.commit()

        # sort 인자 없이 호출 → 기본값 collected_desc 적용
        results = list_announcements(db_session, limit=10)
        ids = [r.id for r in results]

        # collected_desc: 최신(t3) 먼저 → ann3, ann2, ann1 순서
        assert ids.index(ann3.id) < ids.index(ann2.id) < ids.index(ann1.id)

    def test_explicit_collected_desc_sort(self, db_session: Session) -> None:
        """sort='collected_desc' 명시 시 공고 수집일 최신순으로 정렬되어야 한다."""
        t1 = _dt(1)
        t3 = _dt(10)

        ann_old = self._insert_announcement(
            db_session, source_id="b_old", updated_at=t1, scraped_at=t1
        )
        ann_new = self._insert_announcement(
            db_session, source_id="b_new", updated_at=t3, scraped_at=t3
        )
        db_session.commit()

        results = list_announcements(db_session, sort="collected_desc", limit=10)
        ids = [r.id for r in results]

        assert ids.index(ann_new.id) < ids.index(ann_old.id)

    def test_scraped_at_fallback_in_collected_sort(self, db_session: Session) -> None:
        """updated_at 과 scraped_at 이 동일하게 세팅된 공고도 collected_desc 로 올바른 위치에 정렬된다.

        DB 레벨에서 nullable=False 이므로 실제로 NULL 을 삽입할 수 없다.
        대신 scraped_at 만 높은 값을 갖는 공고가 collected_desc 기준으로
        updated_at 이 높은 공고 뒤에 오지 않음을 확인한다.
        """
        # ann_a: updated_at=t3(최신), scraped_at=t1
        # ann_b: updated_at=t1(과거), scraped_at=t3
        # collected_desc 는 COALESCE(updated_at, scraped_at) → 각각 t3, t1
        # → ann_a 가 앞에 오고 ann_b 가 뒤에 와야 한다
        t1 = _dt(1)
        t3 = _dt(10)

        ann_a = self._insert_announcement(
            db_session, source_id="c_a", updated_at=t3, scraped_at=t1
        )
        ann_b = self._insert_announcement(
            db_session, source_id="c_b", updated_at=t1, scraped_at=t3
        )
        db_session.commit()

        results = list_announcements(db_session, sort="collected_desc", limit=10)
        ids = [r.id for r in results]

        assert ids.index(ann_a.id) < ids.index(ann_b.id), (
            "updated_at 최신인 ann_a 가 ann_b 보다 앞에 와야 한다"
        )


# ---------------------------------------------------------------------------
# list_announcements 접수 상태 정렬 통합 테스트 (SQL ORDER BY 경로)
# ---------------------------------------------------------------------------


class TestListAnnouncementsStatusSort:
    """list_announcements 의 status_asc / status_desc 정렬을 DB 경로로 검증한다.

    접수예정(1) → 접수중(2) → 마감(3) 우선순위로 SQL case() 정렬이 적용되는지 확인한다.
    """

    def _insert_announcement(
        self,
        session: Session,
        *,
        source_id: str,
        status: AnnouncementStatus,
    ) -> Announcement:
        """지정한 접수 상태를 갖는 테스트용 Announcement 를 DB 에 삽입하고 반환한다."""
        ann = Announcement(
            source_announcement_id=source_id,
            source_type="IRIS",
            title=f"공고 {source_id}",
            status=status,
            is_current=True,
        )
        ann.updated_at = _BASE_TIME
        ann.scraped_at = _BASE_TIME
        session.add(ann)
        session.flush()
        return ann

    def test_status_asc_orders_scheduled_first(self, db_session: Session) -> None:
        """status_asc: 접수예정 → 접수중 → 마감 순으로 정렬되어야 한다."""
        ann_closed = self._insert_announcement(
            db_session, source_id="s_closed", status=AnnouncementStatus.CLOSED
        )
        ann_scheduled = self._insert_announcement(
            db_session, source_id="s_scheduled", status=AnnouncementStatus.SCHEDULED
        )
        ann_receiving = self._insert_announcement(
            db_session, source_id="s_receiving", status=AnnouncementStatus.RECEIVING
        )
        db_session.commit()

        results = list_announcements(db_session, sort="status_asc", limit=10)
        ids = [r.id for r in results]

        assert (
            ids.index(ann_scheduled.id)
            < ids.index(ann_receiving.id)
            < ids.index(ann_closed.id)
        )

    def test_status_desc_orders_closed_first(self, db_session: Session) -> None:
        """status_desc: 마감 → 접수중 → 접수예정 역순으로 정렬되어야 한다."""
        ann_scheduled = self._insert_announcement(
            db_session, source_id="t_scheduled", status=AnnouncementStatus.SCHEDULED
        )
        ann_receiving = self._insert_announcement(
            db_session, source_id="t_receiving", status=AnnouncementStatus.RECEIVING
        )
        ann_closed = self._insert_announcement(
            db_session, source_id="t_closed", status=AnnouncementStatus.CLOSED
        )
        db_session.commit()

        results = list_announcements(db_session, sort="status_desc", limit=10)
        ids = [r.id for r in results]

        assert (
            ids.index(ann_closed.id)
            < ids.index(ann_receiving.id)
            < ids.index(ann_scheduled.id)
        )
