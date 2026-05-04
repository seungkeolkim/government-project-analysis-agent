"""조직 rename / move 서비스 함수 단위 테스트.

테스트 커버리지:
    rename:
        - 정상 이름 변경
        - 같은 부모 아래 동명 충돌 (DuplicateOrganizationNameError)
        - 자기 자신 동명으로 rename — no-op (오류 없음)
        - 대상 조직 없음 (OrganizationNotFoundError)
        - 공백만 있는 이름 (ValueError)
    move:
        - 루트 → 자식 정상 이동
        - 자식 → 루트 정상 이동
        - 자식 → 다른 부모 정상 이동
        - 자기 자신을 부모로 지정 (OrganizationInvalidMoveError)
        - 후손에게 이동 거부 — 순환 참조 방지 (OrganizationInvalidMoveError)
        - 이동 대상 위치의 동명 충돌 (DuplicateOrganizationNameError)
        - 대상 조직 없음 (OrganizationNotFoundError)
        - 존재하지 않는 부모로 이동 (OrganizationNotFoundError)
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.models import Organization
from app.organizations.service import (
    DuplicateOrganizationNameError,
    OrganizationInvalidMoveError,
    OrganizationNotFoundError,
    create_organization,
    move_organization,
    rename_organization,
)


# ── 헬퍼 ────────────────────────────────────────────────────────────────────


def _make_org(
    session: Session,
    name: str,
    parent_id: int | None = None,
) -> Organization:
    """테스트용 조직을 생성해 flush 하고 반환한다."""
    return create_organization(session, name=name, parent_id=parent_id)


# ── rename 테스트 ─────────────────────────────────────────────────────────────


def test_rename_organization_success(db_session: Session) -> None:
    """정상적인 이름 변경 — 반환된 인스턴스의 name 이 새 이름으로 갱신된다."""
    org = _make_org(db_session, "기존조직")
    result = rename_organization(db_session, org.id, "새조직")
    assert result.id == org.id
    assert result.name == "새조직"


def test_rename_organization_strips_whitespace(db_session: Session) -> None:
    """좌우 공백을 제거한 이름으로 저장된다."""
    org = _make_org(db_session, "조직A")
    result = rename_organization(db_session, org.id, "  새이름  ")
    assert result.name == "새이름"


def test_rename_organization_duplicate_name_error(db_session: Session) -> None:
    """같은 루트 레벨에 동명 조직이 있으면 DuplicateOrganizationNameError."""
    _make_org(db_session, "조직A")
    org_b = _make_org(db_session, "조직B")
    with pytest.raises(DuplicateOrganizationNameError):
        rename_organization(db_session, org_b.id, "조직A")


def test_rename_organization_duplicate_name_under_same_parent(db_session: Session) -> None:
    """같은 부모 아래 동명 조직이 있으면 DuplicateOrganizationNameError."""
    parent = _make_org(db_session, "부모")
    _make_org(db_session, "자식A", parent_id=parent.id)
    child_b = _make_org(db_session, "자식B", parent_id=parent.id)
    with pytest.raises(DuplicateOrganizationNameError):
        rename_organization(db_session, child_b.id, "자식A")


def test_rename_organization_same_name_noop(db_session: Session) -> None:
    """현재 이름과 동일한 이름으로 rename 하면 오류 없이 그대로 반환한다."""
    org = _make_org(db_session, "조직A")
    result = rename_organization(db_session, org.id, "조직A")
    assert result.id == org.id
    assert result.name == "조직A"


def test_rename_organization_not_found_error(db_session: Session) -> None:
    """존재하지 않는 organization_id 로 rename 시 OrganizationNotFoundError."""
    with pytest.raises(OrganizationNotFoundError):
        rename_organization(db_session, 9999, "새이름")


def test_rename_organization_empty_name_error(db_session: Session) -> None:
    """공백 제거 후 빈 문자열이 되는 이름으로 rename 시 ValueError."""
    org = _make_org(db_session, "조직A")
    with pytest.raises(ValueError):
        rename_organization(db_session, org.id, "   ")


# ── move 테스트 ───────────────────────────────────────────────────────────────


def test_move_organization_root_to_child(db_session: Session) -> None:
    """루트 조직을 다른 루트의 자식으로 이동 — parent_id 가 새 부모 id 로 갱신된다."""
    parent = _make_org(db_session, "부모조직")
    target = _make_org(db_session, "이동조직")
    result = move_organization(db_session, target.id, parent.id)
    assert result.parent_id == parent.id


def test_move_organization_child_to_root(db_session: Session) -> None:
    """자식 조직을 루트(parent_id=None)로 이동."""
    parent = _make_org(db_session, "부모조직")
    child = _make_org(db_session, "자식조직", parent_id=parent.id)
    result = move_organization(db_session, child.id, None)
    assert result.parent_id is None


def test_move_organization_to_different_parent(db_session: Session) -> None:
    """자식 조직을 다른 부모 아래로 이동."""
    parent_a = _make_org(db_session, "부모A")
    parent_b = _make_org(db_session, "부모B")
    child = _make_org(db_session, "자식조직", parent_id=parent_a.id)
    result = move_organization(db_session, child.id, parent_b.id)
    assert result.parent_id == parent_b.id


def test_move_organization_self_parent_error(db_session: Session) -> None:
    """자기 자신을 부모로 지정하면 OrganizationInvalidMoveError."""
    org = _make_org(db_session, "조직A")
    with pytest.raises(OrganizationInvalidMoveError):
        move_organization(db_session, org.id, org.id)


def test_move_organization_circular_reference_direct_child(db_session: Session) -> None:
    """직속 자식에게 이동 시 OrganizationInvalidMoveError (순환 참조 방지)."""
    root = _make_org(db_session, "루트")
    child = _make_org(db_session, "자식", parent_id=root.id)
    with pytest.raises(OrganizationInvalidMoveError):
        move_organization(db_session, root.id, child.id)


def test_move_organization_circular_reference_grandchild(db_session: Session) -> None:
    """손자 조직에게 이동 시 OrganizationInvalidMoveError (다단계 순환 참조 방지)."""
    root = _make_org(db_session, "루트")
    child = _make_org(db_session, "자식", parent_id=root.id)
    grandchild = _make_org(db_session, "손자", parent_id=child.id)
    with pytest.raises(OrganizationInvalidMoveError):
        move_organization(db_session, root.id, grandchild.id)


def test_move_organization_duplicate_name_at_destination(db_session: Session) -> None:
    """이동 대상 위치에 동명 조직이 이미 있으면 DuplicateOrganizationNameError."""
    parent = _make_org(db_session, "부모")
    _make_org(db_session, "중복조직", parent_id=parent.id)
    # 루트에 동명 조직을 만들고 parent 아래로 이동 시도
    root_same_name = _make_org(db_session, "중복조직")
    with pytest.raises(DuplicateOrganizationNameError):
        move_organization(db_session, root_same_name.id, parent.id)


def test_move_organization_not_found_error(db_session: Session) -> None:
    """존재하지 않는 organization_id 로 move 시 OrganizationNotFoundError."""
    with pytest.raises(OrganizationNotFoundError):
        move_organization(db_session, 9999, None)


def test_move_organization_parent_not_found_error(db_session: Session) -> None:
    """존재하지 않는 parent_id 로 move 시 OrganizationNotFoundError."""
    org = _make_org(db_session, "조직A")
    with pytest.raises(OrganizationNotFoundError):
        move_organization(db_session, org.id, 9999)
