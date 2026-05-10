"""Phase C — 목록 셀 클릭 expand 본문 회귀 테스트 (task 00097-5 attempt 2).

검증 항목:
    1. 시나리오 12 — 목록 페이지 응답 본문에 .pg-wrap--clickable 마커 + 매크로의
       hidden expand <tr> 이 모두 포함되어 progress.js click 핸들러로 toggle
       될 수 있는 상태인지 확인.
    2. 시나리오 17 — get_progress_rows_by_canonical_id_map 가 canonical N 개를
       단일 SELECT 로 처리하는지 (before_cursor_execute 리스너로 SELECT 카운트).

테스트 픽스처는 tests/relevance / tests/progress 의 기존 패턴 재사용.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select

from app.db.models import (
    Announcement,
    AnnouncementProgress,
    AnnouncementProgressStatus,
    AnnouncementStatus,
    CanonicalProject,
    Organization,
    User,
)
from app.db.session import session_scope
from app.progress.repository import (
    create_progress,
    get_progress_rows_by_canonical_id_map,
)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """본 모듈의 모든 테스트가 공유하는 TestClient — 매 테스트 별 fresh DB."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


def _seed_canonical_with_progress(canonical_key: str) -> dict[str, int]:
    """canonical + announcement + 두 조직 + 진행 row 두 개 시드.

    Returns:
        {canonical_id, announcement_id, organization_a_id, organization_b_id}.
    """
    with session_scope() as session:
        canonical_project = CanonicalProject(
            canonical_key=canonical_key,
            key_scheme="official",
        )
        session.add(canonical_project)
        session.flush()

        organization_a = Organization(name="팀-expand-A")
        organization_b = Organization(name="팀-expand-B")
        session.add_all([organization_a, organization_b])
        session.flush()

        seeder = User(username=f"seeder-{canonical_key[-6:]}", password_hash="x")
        session.add(seeder)
        session.flush()

        announcement = Announcement(
            source_announcement_id=canonical_key,
            source_type="IRIS",
            title="진행 expand 회귀 공고",
            agency="기관-expand",
            status=AnnouncementStatus.RECEIVING,
            deadline_at=datetime.now(UTC) + timedelta(days=10),
            raw_metadata={},
            canonical_group_id=canonical_project.id,
            canonical_key=canonical_project.canonical_key,
            canonical_key_scheme="official",
            is_current=True,
        )
        session.add(announcement)
        session.flush()

        # A 조직: 진행, B 조직: 검토.
        create_progress(
            session,
            canonical_project_id=canonical_project.id,
            organization_id=organization_a.id,
            status=AnnouncementProgressStatus.IN_PROGRESS,
            note="A 진행 중",
            created_by_user_id=seeder.id,
        )
        create_progress(
            session,
            canonical_project_id=canonical_project.id,
            organization_id=organization_b.id,
            status=AnnouncementProgressStatus.REVIEW,
            note="B 검토 중",
            created_by_user_id=seeder.id,
        )

        return {
            "canonical_id": canonical_project.id,
            "announcement_id": announcement.id,
            "organization_a_id": organization_a.id,
            "organization_b_id": organization_b.id,
        }


# ── 시나리오 12 — 목록 페이지 DOM 마커 ─────────────────────────────────────


def test_list_page_renders_clickable_wrap_and_hidden_expand_row(
    client: TestClient,
) -> None:
    """목록 페이지 응답 본문에 .pg-wrap--clickable + hidden expand <tr> 둘 다 노출."""
    seed = _seed_canonical_with_progress("official:expand-1")
    announcement_id = seed["announcement_id"]

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # 셀 자체는 클릭 가능 마커 + role/aria 속성 포함.
    assert "pg-wrap--clickable" in body
    assert 'role="button"' in body
    assert 'aria-controls="progress-expand-row-a-' in body
    assert 'data-expand-target="progress-expand-row-a-' in body

    # hidden expand <tr> 이 announcement.id 기준 id 로 렌더되고 있어야 한다.
    assert f'id="progress-expand-row-a-{announcement_id}"' in body
    # 시작 상태는 inline style="display:none" — JS 가 toggle 한다.
    assert 'class="pg-expand-row"' in body
    assert 'style="display:none"' in body

    # expand 본문에 두 조직명이 모두 들어갔는지 + status 배지·메타 라인 확인.
    assert "팀-expand-A" in body
    assert "팀-expand-B" in body
    assert "pg-expand-row__list" in body
    assert "pg-expand-row__org" in body
    # 진행 단계 배지 (.pg-status-badge--in_progress) 가 expand 본문에 있어야 한다.
    assert "pg-status-badge--in_progress" in body
    assert "pg-status-badge--review" in body


def test_list_page_group_mode_renders_expand_row(client: TestClient) -> None:
    """group_mode (?group=on) 에서도 expand <tr> 이 representative.id 기반으로 렌더."""
    seed = _seed_canonical_with_progress("official:expand-group-1")
    announcement_id = seed["announcement_id"]

    response = client.get("/?group=on")
    assert response.status_code == 200
    body = response.text

    # group_mode 의 expand id 는 representative.id 기반 — 본 시드는 announcement 1 개라
    # 같은 PK 가 representative.
    assert f'id="progress-expand-row-g-{announcement_id}"' in body
    assert "pg-wrap--clickable" in body
    assert "팀-expand-A" in body


def test_empty_progress_cell_renders_em_dash_without_expand_row(
    client: TestClient,
) -> None:
    """진행 row 가 없는 canonical 셀은 em dash + .pg-wrap--clickable 미부여."""
    # canonical 만 만들고 progress row 는 만들지 않는다.
    with session_scope() as session:
        canonical_project = CanonicalProject(
            canonical_key="official:expand-empty",
            key_scheme="official",
        )
        session.add(canonical_project)
        session.flush()
        announcement = Announcement(
            source_announcement_id="EMPTY-1",
            source_type="IRIS",
            title="진행 없음 공고",
            agency="기관A",
            status=AnnouncementStatus.RECEIVING,
            deadline_at=datetime.now(UTC) + timedelta(days=5),
            raw_metadata={},
            canonical_group_id=canonical_project.id,
            canonical_key=canonical_project.canonical_key,
            canonical_key_scheme="official",
            is_current=True,
        )
        session.add(announcement)
        session.commit()

    response = client.get("/")
    assert response.status_code == 200
    body = response.text

    # em dash 표시 클래스가 있고 클릭 가능 마커는 없어야 한다 (빈 셀 = 클릭 불가).
    assert 'class="pg-empty"' in body
    # 빈 셀 hover 툴팁도 미렌더 — '상세는 셀을 클릭하면 펼쳐집니다.' 문구 미노출.
    assert "상세는 셀을 클릭하면 펼쳐집니다." not in body


# ── 시나리오 17 — N+1 회귀 가드 ────────────────────────────────────────────


def test_get_progress_rows_by_canonical_id_map_uses_single_select(
    test_engine: Engine,
) -> None:
    """canonical 5 개에 대한 detail rows 조회가 SELECT 1 회만 발행됨을 보장한다.

    설계 문서 §7.2 + Phase B 패턴 동일. before_cursor_execute 리스너로 SELECT 만
    카운트.
    """
    canonical_ids: list[int] = []
    with session_scope() as session:
        seeder = User(username="rows-seeder", password_hash="x")
        session.add(seeder)
        session.flush()
        seeder_id = seeder.id
        for index in range(5):
            canonical_project = CanonicalProject(
                canonical_key=f"official:rows-batch-{index}",
                key_scheme="official",
            )
            session.add(canonical_project)
            session.flush()
            canonical_ids.append(canonical_project.id)
            organization = Organization(name=f"팀-rows-{index}")
            session.add(organization)
            session.flush()
            create_progress(
                session,
                canonical_project_id=canonical_project.id,
                organization_id=organization.id,
                status=AnnouncementProgressStatus.IN_PROGRESS,
                note=f"진행-{index}",
                created_by_user_id=seeder_id,
            )

    # SELECT 카운터 — engine 에 before_cursor_execute 리스너로 SELECT 만 집계.
    select_count = 0

    def _count_select(conn, cursor, statement, parameters, context, executemany):
        """SELECT 문만 카운트. INSERT/UPDATE/DELETE 는 무시한다."""
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(test_engine, "before_cursor_execute", _count_select)
    try:
        with session_scope() as session:
            rows_map = get_progress_rows_by_canonical_id_map(
                session, canonical_project_ids=canonical_ids
            )
    finally:
        event.remove(test_engine, "before_cursor_execute", _count_select)

    # canonical 5 개 모두 채워졌고 SELECT 는 1 회만 발행.
    assert len(rows_map) == 5
    assert select_count == 1, (
        f"N+1 회귀 의심 — canonical 5 개 처리에 SELECT {select_count} 회 발행됨. "
        "단일 SELECT + JOIN 패턴이 깨졌는지 확인하세요."
    )

    # 단일 row 의 메타 필드도 검증 — relationship lazy load 없이 채워졌는지.
    sample_canonical_id = canonical_ids[0]
    sample_rows = rows_map[sample_canonical_id]
    assert len(sample_rows) == 1
    assert sample_rows[0].organization_name is not None
    assert sample_rows[0].status_value == "진행"
    assert sample_rows[0].status_name == "in_progress"
    assert sample_rows[0].last_modifier_username == "rows-seeder"


def test_get_progress_rows_by_canonical_id_map_sorts_by_priority(
    test_engine: Engine,
) -> None:
    """동일 canonical 안에서 단계 활동성 우선순위 (진행 → 검토 → 관심 → 종료) 정렬.

    정렬 로직만 검증하므로 mutex 제약을 우회해 AnnouncementProgress 를 직접 삽입한다.
    (DONE + IN_PROGRESS 를 동일 canonical 에 심는 것은 정상 운영에서는 불가하지만,
    정렬 회귀 가드를 위해 repository 레이어를 우회하는 것이 의도적 설계다.)
    """
    with session_scope() as session:
        from datetime import UTC, datetime

        seeder = User(username="sort-seeder", password_hash="x")
        session.add(seeder)
        session.flush()
        canonical_project = CanonicalProject(
            canonical_key="official:rows-sort",
            key_scheme="official",
        )
        session.add(canonical_project)
        session.flush()
        organizations = [
            Organization(name=f"팀-정렬-{label}")
            for label in ("진행", "검토", "관심", "종료")
        ]
        session.add_all(organizations)
        session.flush()
        statuses = [
            AnnouncementProgressStatus.DONE,
            AnnouncementProgressStatus.INTEREST,
            AnnouncementProgressStatus.IN_PROGRESS,
            AnnouncementProgressStatus.REVIEW,
        ]
        now = datetime.now(UTC)
        # mutex 제약 없이 직접 삽입 — 정렬 회귀 테스트 전용.
        for organization, status_enum in zip(organizations, statuses, strict=True):
            session.add(
                AnnouncementProgress(
                    canonical_project_id=canonical_project.id,
                    organization_id=organization.id,
                    status=status_enum,
                    note="",
                    created_by_user_id=seeder.id,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.flush()
        canonical_id_value = canonical_project.id

    with session_scope() as session:
        rows_map = get_progress_rows_by_canonical_id_map(
            session, canonical_project_ids=[canonical_id_value]
        )

    sorted_rows = rows_map[canonical_id_value]
    # 활동성 우선순위 — 진행 → 검토 → 관심 → 종료.
    assert [row.status_value for row in sorted_rows] == ["진행", "검토", "관심", "종료"]
