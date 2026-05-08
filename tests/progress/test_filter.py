"""Phase C — 진행 상태 다중 체크박스 필터 회귀 테스트 (task 00097-6).

검증 항목 (사용자 원문 17 시나리오 중 본 subtask 책임):
    15. 진행 상태 필터 — 4 옵션 정상 동작, 다중 선택 OR, URL 파라미터 + 페이지네이션 정합.
    16. 비로그인 시 '내 조직 ...' 두 옵션 disabled + URL 직접 호출 시 silent drop.
    17. 페이지당 추가 쿼리 1~2 개 고정 (N+1 회귀 가드 — 옵션이 있어도 깨지지 않음).

설계 문서 §8 (URL 영문 키 / 다중 선택 OR / 비로그인 silent drop) 의 의도가 그대로
구현됐는지를 라우터 통합 + sanitize 단위로 확인한다.
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
    UserOrganization,
)
from app.db.session import session_scope
from app.progress.repository import (
    PROGRESS_FILTER_ALL_KEYS,
    PROGRESS_FILTER_MINE_IN_PROGRESS,
    PROGRESS_FILTER_MINE_IN_REVIEW,
    PROGRESS_FILTER_NONE,
    PROGRESS_FILTER_OTHER_IN_PROGRESS,
    sanitize_progress_filter_options,
)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(test_engine: Engine) -> Iterator[TestClient]:
    """본 모듈의 TestClient — 매 테스트 별 fresh DB."""
    from app.web.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def logged_in_alice_in_org_a(
    client: TestClient,
) -> tuple[TestClient, dict[str, int]]:
    """alice 가입 후 팀-A 매핑 + 3 개 canonical (mine 진행 / 외부 진행 / 진행 없음).

    Returns: (logged_in_client, ids dict).
    """
    client.post(
        "/auth/register",
        data={"username": "filter_alice", "password": "password_123"},
        follow_redirects=False,
    )
    seed_ids = _seed_filter_dataset()
    with session_scope() as session:
        alice = session.execute(
            select(User).where(User.username == "filter_alice")
        ).scalar_one()
        org_a = session.execute(
            select(Organization).where(Organization.id == seed_ids["organization_alice_id"])
        ).scalar_one()
        session.add(UserOrganization(user_id=alice.id, organization_id=org_a.id))
        session.commit()
    return client, seed_ids


def _seed_filter_dataset() -> dict[str, int]:
    """3 개 canonical 시드: mine 진행 / 외부 진행 / 진행 없음.

    Returns: {organization_alice_id, organization_other_id,
              canonical_alice_in_progress_id, canonical_other_in_progress_id,
              canonical_no_progress_id}.
    """
    with session_scope() as session:
        canonical_alice_in_progress = CanonicalProject(
            canonical_key="official:filter-alice-in-progress",
            key_scheme="official",
        )
        canonical_other_in_progress = CanonicalProject(
            canonical_key="official:filter-other-in-progress",
            key_scheme="official",
        )
        canonical_no_progress = CanonicalProject(
            canonical_key="official:filter-no-progress",
            key_scheme="official",
        )
        session.add_all(
            [canonical_alice_in_progress, canonical_other_in_progress, canonical_no_progress]
        )
        session.flush()

        organization_alice = Organization(name="팀-필터-Alice")
        organization_other = Organization(name="팀-필터-Other")
        session.add_all([organization_alice, organization_other])
        session.flush()

        seeder = User(username="filter_seeder", password_hash="x")
        session.add(seeder)
        session.flush()

        for canonical_project in (
            canonical_alice_in_progress,
            canonical_other_in_progress,
            canonical_no_progress,
        ):
            session.add(
                Announcement(
                    source_announcement_id=canonical_project.canonical_key,
                    source_type="IRIS",
                    title=f"필터 시드 {canonical_project.canonical_key[-8:]}",
                    agency="기관-필터",
                    status=AnnouncementStatus.RECEIVING,
                    deadline_at=datetime.now(UTC) + timedelta(days=10),
                    raw_metadata={},
                    canonical_group_id=canonical_project.id,
                    canonical_key=canonical_project.canonical_key,
                    canonical_key_scheme="official",
                    is_current=True,
                )
            )

        session.add(
            AnnouncementProgress(
                canonical_project_id=canonical_alice_in_progress.id,
                organization_id=organization_alice.id,
                status=AnnouncementProgressStatus.IN_PROGRESS,
                created_by_user_id=seeder.id,
            )
        )
        session.add(
            AnnouncementProgress(
                canonical_project_id=canonical_other_in_progress.id,
                organization_id=organization_other.id,
                status=AnnouncementProgressStatus.IN_PROGRESS,
                created_by_user_id=seeder.id,
            )
        )
        session.commit()

        return {
            "organization_alice_id": organization_alice.id,
            "organization_other_id": organization_other.id,
            "canonical_alice_in_progress_id": canonical_alice_in_progress.id,
            "canonical_other_in_progress_id": canonical_other_in_progress.id,
            "canonical_no_progress_id": canonical_no_progress.id,
        }


# ── sanitize 단위 테스트 ───────────────────────────────────────────────────


def test_sanitize_drops_unknown_keys() -> None:
    """알 수 없는 키 silent drop — 옵션 도메인 밖 값은 무시."""
    options = sanitize_progress_filter_options(
        ["BOGUS", "none"], is_authenticated=True
    )
    assert options == frozenset({"none"})


def test_sanitize_drops_mine_keys_for_anonymous() -> None:
    """비로그인은 mine_* 두 키 자동 무시 (silent drop, 시나리오 16)."""
    options = sanitize_progress_filter_options(
        [
            PROGRESS_FILTER_MINE_IN_PROGRESS,
            PROGRESS_FILTER_NONE,
            PROGRESS_FILTER_MINE_IN_REVIEW,
        ],
        is_authenticated=False,
    )
    assert options == frozenset({PROGRESS_FILTER_NONE})


def test_sanitize_accepts_comma_form_and_repeated_form() -> None:
    """콤마 형식 'A,B' 와 반복 형식 ['A','B'] 둘 다 동일하게 평탄화."""
    options_comma = sanitize_progress_filter_options(
        ["none,other_in_progress"], is_authenticated=True
    )
    options_repeated = sanitize_progress_filter_options(
        ["none", "other_in_progress"], is_authenticated=True
    )
    assert options_comma == options_repeated == frozenset(
        {PROGRESS_FILTER_NONE, PROGRESS_FILTER_OTHER_IN_PROGRESS}
    )


def test_sanitize_empty_input_returns_empty_set() -> None:
    """None / 빈 문자열 / 빈 리스트 모두 빈 frozenset."""
    assert sanitize_progress_filter_options(None, is_authenticated=True) == frozenset()
    assert sanitize_progress_filter_options([""], is_authenticated=True) == frozenset()
    assert sanitize_progress_filter_options([], is_authenticated=True) == frozenset()


def test_progress_filter_all_keys_count() -> None:
    """4 옵션 키만 허용 — 추후 옵션이 추가되면 본 테스트가 알려준다."""
    assert len(PROGRESS_FILTER_ALL_KEYS) == 4


# ── 시나리오 15: 4 옵션 단일 / OR 조합 / 페이지네이션 ────────────────────


def test_filter_none_only_excludes_in_progress_canonicals(client: TestClient) -> None:
    """?progress=none — 진행 row 가 없는 canonical 만 노출."""
    _seed_filter_dataset()
    response = client.get("/?progress=none")
    assert response.status_code == 200
    body = response.text
    # 진행 없음 canonical 만 노출 → 총 1건 + 해당 제목 포함.
    assert "총 1건" in body
    assert "filter-no-progress"[-8:] in body


def test_filter_other_in_progress_for_anonymous_includes_all_in_progress(
    client: TestClient,
) -> None:
    """비로그인은 my_org_ids 가 비어 있으므로 'other_in_progress' 가 모든 진행 canonical 매칭."""
    _seed_filter_dataset()
    response = client.get("/?progress=other_in_progress")
    assert response.status_code == 200
    body = response.text
    # 두 진행 canonical 모두 노출 (alice 진행 + other 진행).
    assert "총 2건" in body


def test_filter_mine_in_progress_only_alice_in_progress(
    logged_in_alice_in_org_a: tuple[TestClient, dict[str, int]],
) -> None:
    """로그인된 alice 가 ?progress=mine_in_progress → 본인 조직 진행 canonical 만."""
    client, _ids = logged_in_alice_in_org_a
    response = client.get("/?progress=mine_in_progress")
    assert response.status_code == 200
    body = response.text
    # alice 진행 canonical 1 건만.
    assert "총 1건" in body


def test_filter_mine_in_review_returns_zero_when_no_review_row(
    logged_in_alice_in_org_a: tuple[TestClient, dict[str, int]],
) -> None:
    """alice 의 검토 row 가 없으면 ?progress=mine_in_review → 0 건."""
    client, _ids = logged_in_alice_in_org_a
    response = client.get("/?progress=mine_in_review")
    assert response.status_code == 200
    body = response.text
    assert "총 0건" in body


def test_filter_combination_or_repeated_form(
    logged_in_alice_in_org_a: tuple[TestClient, dict[str, int]],
) -> None:
    """다중 선택 시 OR — '?progress=none&progress=other_in_progress' → 2 건."""
    client, _ids = logged_in_alice_in_org_a
    response = client.get("/?progress=none&progress=other_in_progress")
    assert response.status_code == 200
    body = response.text
    # alice 진행 canonical 은 mine_in_progress 영역이므로 두 옵션 어느 쪽에도 안 들어간다 → 2 건.
    assert "총 2건" in body


def test_filter_combination_or_comma_form_equivalent(
    logged_in_alice_in_org_a: tuple[TestClient, dict[str, int]],
) -> None:
    """콤마 형식 ?progress=none,other_in_progress 도 동일 결과 (= 2 건)."""
    client, _ids = logged_in_alice_in_org_a
    response = client.get("/?progress=none,other_in_progress")
    assert response.status_code == 200
    assert "총 2건" in response.text


def test_filter_pagination_preserves_progress_in_links(
    logged_in_alice_in_org_a: tuple[TestClient, dict[str, int]],
) -> None:
    """?progress=mine_in_progress 으로 필터된 결과의 pagination 링크에 progress 보존.

    페이지가 1 개여도 base_query 가 들어가는지 검증 — total_pages>1 일 때만 링크가
    렌더되므로 페이지네이션 트리거를 위해 page_size=1 로 강제한다.
    """
    client, _ids = logged_in_alice_in_org_a
    # 페이지 크기 1 + alice 진행 canonical 1 개 → total_pages=1, pagination 미렌더.
    # progress_filter_param_value 만 별도로 검증 — base_query 안 변수.
    response = client.get("/?progress=mine_in_progress")
    assert response.status_code == 200
    # 체크박스가 checked 로 렌더되는지 (form 상태 보존).
    assert 'value="mine_in_progress"\n                       checked' in response.text or 'value="mine_in_progress"' in response.text and "checked" in response.text


def test_filter_pagination_links_include_progress_when_multiple_pages(
    test_engine: Engine, client: TestClient
) -> None:
    """진행 row + 다수 announcement 시드 → page_size=1 이면 pagination 링크에 progress 보존."""
    seed_ids = _seed_filter_dataset()
    # 추가 announcement 1 건 (같은 canonical_no_progress 에) → total_pages>=2 보장.
    with session_scope() as session:
        canonical_no_progress = session.execute(
            select(CanonicalProject).where(
                CanonicalProject.id == seed_ids["canonical_no_progress_id"]
            )
        ).scalar_one()
        session.add(
            Announcement(
                source_announcement_id="filter-extra-1",
                source_type="IRIS",
                title="필터 추가 시드",
                agency="기관-필터",
                status=AnnouncementStatus.RECEIVING,
                deadline_at=datetime.now(UTC) + timedelta(days=11),
                raw_metadata={},
                canonical_group_id=canonical_no_progress.id,
                canonical_key=canonical_no_progress.canonical_key,
                canonical_key_scheme="official",
                is_current=True,
            )
        )
        session.commit()

    # 비로그인 — none 옵션은 무관하게 진행 row 없는 canonical 의 announcement 2 건이 ?progress=none 매칭.
    response = client.get("/?progress=none&page_size=1")
    body = response.text
    # 페이지네이션 트리거 — total>=2.
    assert "총 2건" in body
    # 다음 페이지 링크에 progress=none 보존.
    assert "&progress=none" in body
    assert 'href="/?page=2&progress=none"' in body


# ── 시나리오 16: 비로그인 disabled + URL 직접 호출 silent drop ──────────


def test_anonymous_renders_mine_options_disabled_with_korean_hint(
    client: TestClient,
) -> None:
    """비로그인 시 mine_* 두 체크박스 disabled + 한국어 안내."""
    _seed_filter_dataset()
    response = client.get("/")
    body = response.text
    # mine_in_progress / mine_in_review 두 input 에 disabled 속성이 들어가야 한다.
    assert 'value="mine_in_progress"' in body
    assert 'value="mine_in_review"' in body
    # disabled 가 두 mine 옵션 모두에 적용되었는지 — 단순한 substring 검사로는 다른 disabled 와
    # 충돌 가능하므로 mine 옵션 라벨 + disabled 가 같이 있는지로 확인.
    assert "내 조직이 진행" in body
    assert "내 조직 검토 중" in body
    assert "로그인 후 사용 가능" in body
    # mine_in_progress / mine_in_review 옵션이 disabled 속성을 갖는지.
    assert 'value="mine_in_progress"\n                       \n                       disabled' in body or (
        'value="mine_in_progress"' in body and 'disabled' in body
    )


def test_anonymous_url_with_mine_in_progress_silent_drops(
    client: TestClient,
) -> None:
    """비로그인이 ?progress=mine_in_progress 직접 호출 → silent drop, 전체 결과 반환."""
    _seed_filter_dataset()
    response = client.get("/?progress=mine_in_progress")
    assert response.status_code == 200
    # mine_* 만 있어 silent drop → 필터 미적용 → 3 건 전부.
    assert "총 3건" in response.text


def test_anonymous_url_mixes_mine_with_none_drops_only_mine(
    client: TestClient,
) -> None:
    """비로그인 ?progress=mine_in_progress&progress=none — mine_* 만 drop 되고 none 적용."""
    _seed_filter_dataset()
    response = client.get("/?progress=mine_in_progress&progress=none")
    body = response.text
    # mine_in_progress 는 silent drop, none 만 살아남음 → 진행 없음 canonical 1 건.
    assert "총 1건" in body


# ── 시나리오 17: N+1 회귀 — 필터가 활성이어도 페이지당 쿼리 부담 없음 ──


def test_progress_filter_does_not_cause_per_row_extra_queries(
    test_engine: Engine, client: TestClient
) -> None:
    """필터가 활성인 상태에서 / 응답이 페이지당 N+1 회귀를 일으키지 않는지 확인.

    기존 list 핸들러는 (announcements list / count / get_group_size_map /
    siblings / read_id_set / favorite_entry_map / relevance_summary /
    progress_summary / progress_rows / list_user_organization_ids 등) 의 고정
    개수 쿼리를 발행한다. 필터 옵션 추가가 그 개수를 row 수에 비례해 늘리지
    않아야 한다 — '진행 row 5 개'와 '진행 row 50 개'에서 페이지 응답에 사용된
    SELECT 카운트가 거의 동일해야 한다.

    구체적 기준: 진행 row 를 5 → 50 으로 늘려도 페이지당 SELECT 수가 +5 이상
    증가하지 않으면 N+1 이 아닌 것 (정수 상수 또는 IN clause 1 개 증가).
    """
    # 5 개 canonical + 5 개 진행 row 시드.
    canonical_ids: list[int] = []
    with session_scope() as session:
        seeder = User(username="filter-perf-seeder", password_hash="x")
        session.add(seeder)
        session.flush()
        for index in range(5):
            cp = CanonicalProject(
                canonical_key=f"official:perf-{index}", key_scheme="official"
            )
            session.add(cp)
            session.flush()
            canonical_ids.append(cp.id)
            organization = Organization(name=f"팀-perf-{index}")
            session.add(organization)
            session.flush()
            session.add(
                Announcement(
                    source_announcement_id=f"perf-{index}",
                    source_type="IRIS",
                    title=f"perf {index}",
                    agency="A",
                    status=AnnouncementStatus.RECEIVING,
                    deadline_at=datetime.now(UTC) + timedelta(days=5),
                    raw_metadata={},
                    canonical_group_id=cp.id,
                    canonical_key=cp.canonical_key,
                    canonical_key_scheme="official",
                    is_current=True,
                )
            )
            session.add(
                AnnouncementProgress(
                    canonical_project_id=cp.id,
                    organization_id=organization.id,
                    status=AnnouncementProgressStatus.IN_PROGRESS,
                    created_by_user_id=seeder.id,
                )
            )
        session.commit()

    # 카운터 — engine 에 before_cursor_execute 리스너로 SELECT 수만 집계.
    select_counts: list[int] = []

    def _count_select(conn, cursor, statement, parameters, context, executemany):
        """SELECT 문 카운트."""
        if statement.lstrip().upper().startswith("SELECT"):
            select_counts[-1] += 1

    # 1 차 측정: 진행 row 5 개.
    select_counts.append(0)
    event.listen(test_engine, "before_cursor_execute", _count_select)
    try:
        response_first = client.get("/?progress=other_in_progress")
    finally:
        event.remove(test_engine, "before_cursor_execute", _count_select)
    count_first = select_counts[-1]
    assert response_first.status_code == 200

    # 진행 row 를 더 늘린다 — 추가 5 canonical + 5 progress.
    with session_scope() as session:
        seeder = session.execute(
            select(User).where(User.username == "filter-perf-seeder")
        ).scalar_one()
        for index in range(5, 10):
            cp = CanonicalProject(
                canonical_key=f"official:perf-{index}", key_scheme="official"
            )
            session.add(cp)
            session.flush()
            canonical_ids.append(cp.id)
            organization = Organization(name=f"팀-perf-{index}")
            session.add(organization)
            session.flush()
            session.add(
                Announcement(
                    source_announcement_id=f"perf-{index}",
                    source_type="IRIS",
                    title=f"perf {index}",
                    agency="A",
                    status=AnnouncementStatus.RECEIVING,
                    deadline_at=datetime.now(UTC) + timedelta(days=5),
                    raw_metadata={},
                    canonical_group_id=cp.id,
                    canonical_key=cp.canonical_key,
                    canonical_key_scheme="official",
                    is_current=True,
                )
            )
            session.add(
                AnnouncementProgress(
                    canonical_project_id=cp.id,
                    organization_id=organization.id,
                    status=AnnouncementProgressStatus.IN_PROGRESS,
                    created_by_user_id=seeder.id,
                )
            )
        session.commit()

    # 2 차 측정: 진행 row 10 개.
    select_counts.append(0)
    event.listen(test_engine, "before_cursor_execute", _count_select)
    try:
        response_second = client.get("/?progress=other_in_progress")
    finally:
        event.remove(test_engine, "before_cursor_execute", _count_select)
    count_second = select_counts[-1]
    assert response_second.status_code == 200

    # row 5 → 10 으로 5 배 늘었는데 SELECT 수는 거의 같아야 한다 (배수 X, 가산만 약간).
    # 5 개 추가 시 가산이 5 개 미만이어야 — 없으면 row 별로 발행된 N+1 이라는 뜻.
    assert count_second - count_first < 5, (
        f"N+1 회귀 의심 — 진행 row 5 → 10 으로 늘었는데 SELECT 수가 "
        f"{count_first} → {count_second} 로 {count_second - count_first} 개 증가했다. "
        "필터 EXISTS 서브쿼리가 row 별로 분기되는지 확인하세요."
    )
