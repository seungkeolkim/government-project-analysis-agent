"""조직 트리 JSON 직렬화(export) / 역직렬화(import) 단위 테스트.

테스트 커버리지:
    export:
        - 조직이 없을 때 빈 배열 반환
        - 한국어 조직명이 유니코드 이스케이프 없이 직렬화
        - indent=2 pretty-print 확인
        - 부모-자식 계층 구조가 children 배열로 올바르게 중첩
    import:
        - 기본 트리 교체
        - export-import 왕복(roundtrip) 검증
        - user_organizations 이름 경로 일치 매핑 재생성
        - user_organizations 이름 경로 불일치 시 조용히 드롭
        - 일부만 드롭되고 나머지는 보존
        - 통계(total_organizations, dropped_user_org_count, affected_user_count) 반환
        - 유효하지 않은 JSON 입력 시 ValueError
        - 최상위가 list 가 아닐 때 ValueError
        - name 필드 누락 시 ValueError
        - 같은 레벨 동명 조직 시 DuplicateOrganizationNameError
        - 빈 배열 import 시 조직 전체 삭제
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Organization, User, UserOrganization
from app.organizations.io import (
    export_organization_tree_json,
    import_organization_tree_json,
)
from app.organizations.service import (
    DuplicateOrganizationNameError,
    create_organization,
    list_all_organizations,
)


# ── 헬퍼 ────────────────────────────────────────────────────────────────────


def _make_org(session: Session, name: str, parent_id: int | None = None) -> Organization:
    """테스트용 조직을 생성해 flush 하고 반환한다."""
    return create_organization(session, name=name, parent_id=parent_id)


def _make_user(session: Session, username: str) -> User:
    """테스트용 사용자를 생성해 flush 하고 반환한다."""
    user = User(username=username, password_hash="hash")
    session.add(user)
    session.flush()
    return user


def _make_user_org(session: Session, user_id: int, org_id: int) -> UserOrganization:
    """테스트용 사용자-조직 매핑을 생성해 flush 하고 반환한다."""
    uo = UserOrganization(user_id=user_id, organization_id=org_id)
    session.add(uo)
    session.flush()
    return uo


def _get_user_org_count(session: Session, user_id: int) -> int:
    """특정 사용자의 user_organizations 행 수를 반환한다."""
    rows = session.execute(
        select(UserOrganization).where(UserOrganization.user_id == user_id)
    ).scalars().all()
    return len(rows)


# ── export 테스트 ─────────────────────────────────────────────────────────────


def test_export_empty_tree(db_session: Session) -> None:
    """조직이 없을 때 export 는 빈 배열 JSON 을 반환한다."""
    result = export_organization_tree_json(db_session)
    assert json.loads(result) == []


def test_export_preserves_korean_names(db_session: Session) -> None:
    """한국어 조직명이 유니코드 이스케이프 없이 직렬화된다."""
    _make_org(db_session, "한국어조직")
    result = export_organization_tree_json(db_session)
    assert "한국어조직" in result
    # ensure_ascii=False 가 적용됐으면 \\u 이스케이프가 없어야 한다.
    assert "\\u" not in result


def test_export_pretty_print(db_session: Session) -> None:
    """반환된 JSON 문자열이 indent=2 로 pretty-print 됐는지 확인한다."""
    _make_org(db_session, "조직A")
    result = export_organization_tree_json(db_session)
    # indent 가 적용됐으면 개행 문자가 포함된다.
    assert "\n" in result


def test_export_hierarchy_structure(db_session: Session) -> None:
    """부모-자식 관계가 children 배열로 올바르게 중첩된다."""
    parent = _make_org(db_session, "부모")
    _make_org(db_session, "자식", parent_id=parent.id)

    result = export_organization_tree_json(db_session)
    tree = json.loads(result)

    assert len(tree) == 1
    assert tree[0]["name"] == "부모"
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["name"] == "자식"


# ── import 테스트 ─────────────────────────────────────────────────────────────


def test_import_basic_tree(db_session: Session) -> None:
    """기본 트리 JSON 을 import 하면 DB 에 조직이 올바르게 삽입된다."""
    json_text = json.dumps(
        [
            {
                "name": "루트",
                "children": [
                    {"name": "자식A", "children": []},
                    {"name": "자식B", "children": []},
                ],
            }
        ],
        ensure_ascii=False,
    )

    stats = import_organization_tree_json(db_session, json_text)

    orgs = list_all_organizations(db_session)
    names = {org.name for org in orgs}
    assert names == {"루트", "자식A", "자식B"}
    assert stats["total_organizations"] == 3


def test_import_replaces_existing_tree(db_session: Session) -> None:
    """기존 트리가 있을 때 import 하면 기존 트리가 완전히 교체된다."""
    _make_org(db_session, "이전조직")

    new_tree = json.dumps([{"name": "새조직", "children": []}], ensure_ascii=False)
    import_organization_tree_json(db_session, new_tree)

    orgs = list_all_organizations(db_session)
    names = {org.name for org in orgs}
    assert names == {"새조직"}
    assert "이전조직" not in names


def test_import_export_roundtrip(db_session: Session) -> None:
    """export 후 import 하면 트리 구조(이름, 계층)가 동일하게 복원된다."""
    parent = _make_org(db_session, "루트")
    _make_org(db_session, "자식1", parent_id=parent.id)
    _make_org(db_session, "자식2", parent_id=parent.id)

    exported = export_organization_tree_json(db_session)
    import_organization_tree_json(db_session, exported)

    tree = json.loads(export_organization_tree_json(db_session))
    assert len(tree) == 1
    assert tree[0]["name"] == "루트"
    assert len(tree[0]["children"]) == 2
    child_names = {c["name"] for c in tree[0]["children"]}
    assert child_names == {"자식1", "자식2"}


def test_import_restores_matching_user_org_mappings(db_session: Session) -> None:
    """import 후 이름 경로가 유지된 user_organizations 행은 새 조직 id 로 재매핑된다."""
    parent = _make_org(db_session, "부서")
    child = _make_org(db_session, "팀", parent_id=parent.id)
    user = _make_user(db_session, "user1")
    _make_user_org(db_session, user.id, child.id)

    # 동일한 트리를 export → import (조직 id 는 바뀌지만 이름 경로는 동일).
    exported = export_organization_tree_json(db_session)
    stats = import_organization_tree_json(db_session, exported)

    assert _get_user_org_count(db_session, user.id) == 1
    assert stats["dropped_user_org_count"] == 0
    assert stats["affected_user_count"] == 0


def test_import_drops_orphaned_user_org_mappings(db_session: Session) -> None:
    """import 시 사라진 조직의 user_organizations 매핑이 조용히 삭제된다."""
    old_org = _make_org(db_session, "없어질조직")
    user = _make_user(db_session, "user1")
    _make_user_org(db_session, user.id, old_org.id)

    # 완전히 다른 트리로 교체.
    new_tree = json.dumps(
        [{"name": "완전히새로운조직", "children": []}], ensure_ascii=False
    )
    stats = import_organization_tree_json(db_session, new_tree)

    assert _get_user_org_count(db_session, user.id) == 0
    assert stats["dropped_user_org_count"] == 1
    assert stats["affected_user_count"] == 1


def test_import_partial_drop(db_session: Session) -> None:
    """import 시 일부 매핑만 사라지면 남은 매핑은 보존되고 사라진 것만 드롭된다."""
    keep_org = _make_org(db_session, "유지조직")
    drop_org = _make_org(db_session, "삭제될조직")
    user = _make_user(db_session, "user1")
    _make_user_org(db_session, user.id, keep_org.id)
    _make_user_org(db_session, user.id, drop_org.id)

    # keep_org 만 남기는 트리로 교체.
    new_tree = json.dumps(
        [{"name": "유지조직", "children": []}], ensure_ascii=False
    )
    stats = import_organization_tree_json(db_session, new_tree)

    assert _get_user_org_count(db_session, user.id) == 1
    assert stats["dropped_user_org_count"] == 1
    assert stats["affected_user_count"] == 1


def test_import_multiple_users_affected(db_session: Session) -> None:
    """여러 사용자의 매핑이 드롭될 때 affected_user_count 가 올바르게 집계된다."""
    old_org = _make_org(db_session, "사라질조직")
    user1 = _make_user(db_session, "user1")
    user2 = _make_user(db_session, "user2")
    _make_user_org(db_session, user1.id, old_org.id)
    _make_user_org(db_session, user2.id, old_org.id)

    new_tree = json.dumps([{"name": "전혀다른조직", "children": []}], ensure_ascii=False)
    stats = import_organization_tree_json(db_session, new_tree)

    assert stats["dropped_user_org_count"] == 2
    assert stats["affected_user_count"] == 2


def test_import_returns_stats(db_session: Session) -> None:
    """import 반환값에 통계 정보가 올바르게 포함된다."""
    json_text = json.dumps(
        [
            {"name": "조직A", "children": []},
            {
                "name": "조직B",
                "children": [{"name": "하위", "children": []}],
            },
        ],
        ensure_ascii=False,
    )
    stats = import_organization_tree_json(db_session, json_text)

    assert stats["total_organizations"] == 3
    assert stats["dropped_user_org_count"] == 0
    assert stats["affected_user_count"] == 0


def test_import_empty_json_array(db_session: Session) -> None:
    """빈 배열을 import 하면 조직이 없는 상태가 된다."""
    _make_org(db_session, "기존조직")

    stats = import_organization_tree_json(db_session, "[]")

    assert list_all_organizations(db_session) == []
    assert stats["total_organizations"] == 0


def test_import_invalid_json_raises(db_session: Session) -> None:
    """유효하지 않은 JSON 입력 시 ValueError(json.JSONDecodeError) 가 발생한다."""
    with pytest.raises(ValueError):
        import_organization_tree_json(db_session, "not valid json {{{")


def test_import_non_list_root_raises(db_session: Session) -> None:
    """최상위가 list 가 아닌 JSON 입력 시 ValueError 가 발생한다."""
    with pytest.raises(ValueError):
        import_organization_tree_json(db_session, '{"name": "조직"}')


def test_import_missing_name_raises(db_session: Session) -> None:
    """name 필드 없는 노드 포함 시 ValueError 가 발생한다."""
    json_text = json.dumps([{"children": []}], ensure_ascii=False)
    with pytest.raises(ValueError):
        import_organization_tree_json(db_session, json_text)


def test_import_empty_name_raises(db_session: Session) -> None:
    """공백만 있는 name 을 가진 노드 포함 시 ValueError 가 발생한다."""
    json_text = json.dumps([{"name": "   ", "children": []}], ensure_ascii=False)
    with pytest.raises(ValueError):
        import_organization_tree_json(db_session, json_text)


def test_import_duplicate_name_same_level_raises(db_session: Session) -> None:
    """같은 레벨에 동명 조직이 있는 JSON 입력 시 DuplicateOrganizationNameError 가 발생한다."""
    json_text = json.dumps(
        [
            {"name": "중복", "children": []},
            {"name": "중복", "children": []},
        ],
        ensure_ascii=False,
    )
    with pytest.raises(DuplicateOrganizationNameError):
        import_organization_tree_json(db_session, json_text)
