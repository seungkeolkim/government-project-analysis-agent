"""조직 트리 CRUD 및 사용자-조직 M:N 매핑 서비스 레이어.

조직(Organization)은 parent_id 를 사용한 무제한 depth 트리 구조다.
트리 조회는 재귀 CTE 대신 메모리 빌드(전체 SELECT → parent_id 그룹화)로 수행한다 —
데이터 규모가 작은 로컬 환경 전제이므로 단순성을 택한다.

자식이 있는 조직 삭제 정책:
    - DB FK(ON DELETE RESTRICT)가 최종 방어선이다.
    - app 레벨에서 먼저 SELECT 로 자식 존재 여부를 확인하고
      OrganizationHasChildrenError 를 던져 친절한 메시지를 제공한다.

루트(parent_id IS NULL) 간 동명 체크:
    - SQLite 의 NULL 비교 특성상 UNIQUE(parent_id, name) 제약으로 루트 동명을 막을 수 없다.
    - INSERT 전 SELECT 로 (parent_id, name) 중복 체크 — FavoriteFolder 와 동일 패턴.

모든 함수는 호출자가 전달한 Session 을 그대로 사용하며,
commit/rollback 은 호출자가 제어한다 (flush 까지만 수행).
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from loguru import logger

from app.db.models import Organization, User, UserOrganization


# ──────────────────────────────────────────────────────────────
# 예외 타입
# ──────────────────────────────────────────────────────────────


class OrganizationNotFoundError(ValueError):
    """요청한 organization_id 가 존재하지 않을 때 발생."""


class OrganizationHasChildrenError(ValueError):
    """자식 조직이 있는 조직을 삭제하려 할 때 발생.

    DB FK(ON DELETE RESTRICT) 가 최종 방어선이지만,
    app 레벨에서 먼저 이 예외를 던져 사용자 친화적 메시지를 제공한다.
    """


class DuplicateOrganizationNameError(ValueError):
    """같은 부모(parent_id) 아래에 동일 이름의 조직이 이미 존재할 때 발생.

    루트(parent_id=None) 간 동명도 이 예외로 처리한다.
    """


class OrganizationInvalidMoveError(ValueError):
    """조직을 이동할 수 없는 위치로 이동하려 할 때 발생.

    자기 자신을 부모로 지정하거나, 자신의 후손 아래로 이동하려 할 때 발생한다.
    """


# ──────────────────────────────────────────────────────────────
# 조직 CRUD
# ──────────────────────────────────────────────────────────────


def list_all_organizations(session: Session) -> list[Organization]:
    """모든 조직을 id 오름차순으로 반환한다.

    메모리 트리 빌드(build_organization_tree)의 입력으로 사용할 수 있다.

    Args:
        session: 호출자 세션.

    Returns:
        Organization 인스턴스 목록 (id 오름차순).
    """
    rows = session.execute(
        select(Organization).order_by(Organization.id)
    ).scalars().all()
    return list(rows)


def build_organization_tree(organizations: list[Organization]) -> list[dict]:
    """Organization 목록을 중첩 트리 구조(dict)로 변환한다.

    재귀 CTE 없이 메모리에서 parent_id 를 기준으로 그룹화하여 트리를 구성한다.
    depth 는 무제한이다.

    Args:
        organizations: list_all_organizations 가 반환한 평탄한 목록.

    Returns:
        [
            {
                \"id\": int,
                \"name\": str,
                \"parent_id\": int | None,
                \"children\": [{...재귀...}],
            },
            ...
        ]
        루트 노드(parent_id=None) 가 최상위 리스트에 담긴다.
    """
    nodes: dict[int, dict] = {
        org.id: {
            "id": org.id,
            "name": org.name,
            "parent_id": org.parent_id,
            "children": [],
        }
        for org in organizations
    }
    roots: list[dict] = []
    for org in organizations:
        node = nodes[org.id]
        if org.parent_id is None:
            roots.append(node)
        elif org.parent_id in nodes:
            nodes[org.parent_id]["children"].append(node)
    return roots


def create_organization(
    session: Session,
    *,
    name: str,
    parent_id: int | None = None,
) -> Organization:
    """신규 조직을 생성해 반환한다.

    INSERT 전에 (parent_id, name) 조합이 이미 존재하는지 SELECT 로 검사한다.
    SQLite 에서 NULL 비교가 \"서로 다름\" 으로 취급되어 루트 동명을 UNIQUE 제약으로
    막을 수 없기 때문이다(FavoriteFolder 동일 패턴).

    Args:
        session: 호출자 세션.
        name: 조직명. 좌우 공백을 제거 후 저장한다.
        parent_id: 부모 조직 PK. None 이면 루트 노드.

    Returns:
        flush 완료된 Organization 인스턴스.

    Raises:
        OrganizationNotFoundError: parent_id 에 해당하는 조직이 없을 때.
        DuplicateOrganizationNameError: 동일 parent 아래 같은 이름이 이미 존재할 때.
    """
    stripped_name = name.strip()

    # 부모 존재 검증
    if parent_id is not None:
        parent = session.get(Organization, parent_id)
        if parent is None:
            raise OrganizationNotFoundError(
                f"부모 조직을 찾을 수 없습니다: parent_id={parent_id}"
            )

    # 동명 체크 — SQLite NULL 비교 한계로 인해 app-level SELECT 사용.
    if parent_id is None:
        duplicate = session.execute(
            select(Organization).where(
                Organization.parent_id.is_(None),
                Organization.name == stripped_name,
            )
        ).scalar_one_or_none()
    else:
        duplicate = session.execute(
            select(Organization).where(
                Organization.parent_id == parent_id,
                Organization.name == stripped_name,
            )
        ).scalar_one_or_none()

    if duplicate is not None:
        raise DuplicateOrganizationNameError(
            f"같은 위치에 동일한 조직명이 이미 존재합니다: {stripped_name!r}"
        )

    org = Organization(name=stripped_name, parent_id=parent_id)
    session.add(org)
    session.flush()
    logger.info(
        "조직 생성: id={} name={!r} parent_id={}", org.id, org.name, org.parent_id
    )
    return org


def _descendant_ids(orgs: list[Organization], target_id: int) -> set[int]:
    """target_id 의 모든 후손 조직 id 집합을 BFS 로 반환한다 (target 자신은 포함하지 않음).

    Args:
        orgs: list_all_organizations 가 반환한 평탄한 목록.
        target_id: 후손 집합을 구할 조직의 id.

    Returns:
        모든 후손 조직 id set (target 자신 미포함).
    """
    # parent_id → 직속 자식 id 목록 dict 구성
    children_map: dict[int, list[int]] = {}
    for org in orgs:
        if org.parent_id is not None:
            children_map.setdefault(org.parent_id, []).append(org.id)

    result: set[int] = set()
    queue = list(children_map.get(target_id, []))
    while queue:
        node_id = queue.pop()
        result.add(node_id)
        queue.extend(children_map.get(node_id, []))
    return result


def rename_organization(
    session: Session,
    organization_id: int,
    new_name: str,
) -> Organization:
    """조직명을 변경한다.

    새 이름(공백 제거 후)이 현재 이름과 동일하면 변경 없이 그대로 반환한다(no-op).
    같은 부모(parent_id) 아래 다른 조직과 이름이 충돌하면 DuplicateOrganizationNameError 를 발생시킨다.

    commit 은 호출자 책임.

    Args:
        session: 호출자 세션.
        organization_id: 이름을 변경할 조직 PK.
        new_name: 새 조직명. 좌우 공백을 제거 후 적용한다.

    Returns:
        flush 완료된 Organization 인스턴스.

    Raises:
        OrganizationNotFoundError: 해당 organization_id 가 없을 때.
        ValueError: 공백 제거 후 new_name 이 빈 문자열일 때.
        DuplicateOrganizationNameError: 같은 부모 아래 동명 조직이 이미 존재할 때.
    """
    org = session.get(Organization, organization_id)
    if org is None:
        raise OrganizationNotFoundError(
            f"조직을 찾을 수 없습니다: organization_id={organization_id}"
        )

    stripped_name = new_name.strip()
    if not stripped_name:
        raise ValueError("조직명은 빈 문자열일 수 없습니다.")

    # 현재 이름과 동일하면 no-op
    if org.name == stripped_name:
        return org

    # 동명 체크 — 같은 부모 아래 자신 외의 조직 중 동명 확인
    if org.parent_id is None:
        duplicate = session.execute(
            select(Organization).where(
                Organization.parent_id.is_(None),
                Organization.name == stripped_name,
                Organization.id != organization_id,
            )
        ).scalar_one_or_none()
    else:
        duplicate = session.execute(
            select(Organization).where(
                Organization.parent_id == org.parent_id,
                Organization.name == stripped_name,
                Organization.id != organization_id,
            )
        ).scalar_one_or_none()

    if duplicate is not None:
        raise DuplicateOrganizationNameError(
            f"같은 위치에 동일한 조직명이 이미 존재합니다: {stripped_name!r}"
        )

    old_name = org.name
    org.name = stripped_name
    session.flush()
    logger.info(
        "조직 이름 변경: id={} {!r} → {!r}", organization_id, old_name, org.name
    )
    return org


def move_organization(
    session: Session,
    organization_id: int,
    new_parent_id: int | None,
) -> Organization:
    """조직의 상위 조직(parent_id)을 변경한다.

    자기 자신을 부모로 지정하거나 자신의 후손 아래로 이동하면 OrganizationInvalidMoveError 를 발생시킨다.
    이동 대상 위치에 같은 이름의 조직이 있으면 DuplicateOrganizationNameError 를 발생시킨다.

    commit 은 호출자 책임.

    Args:
        session: 호출자 세션.
        organization_id: 이동할 조직 PK.
        new_parent_id: 새 부모 조직 PK. None 이면 루트로 이동.

    Returns:
        flush 완료된 Organization 인스턴스.

    Raises:
        OrganizationNotFoundError: organization_id 또는 new_parent_id 에 해당하는 조직이 없을 때.
        OrganizationInvalidMoveError: 자기 자신 부모 지정 또는 후손에게 이동 시.
        DuplicateOrganizationNameError: 이동 대상 위치에 같은 이름의 조직이 이미 존재할 때.
    """
    org = session.get(Organization, organization_id)
    if org is None:
        raise OrganizationNotFoundError(
            f"조직을 찾을 수 없습니다: organization_id={organization_id}"
        )

    # 자기 자신을 부모로 지정 금지
    if new_parent_id == organization_id:
        raise OrganizationInvalidMoveError(
            f"자기 자신을 부모 조직으로 지정할 수 없습니다: organization_id={organization_id}"
        )

    if new_parent_id is not None:
        # 부모 존재 검증
        new_parent = session.get(Organization, new_parent_id)
        if new_parent is None:
            raise OrganizationNotFoundError(
                f"부모 조직을 찾을 수 없습니다: new_parent_id={new_parent_id}"
            )

        # 순환 참조 검사 — new_parent_id 가 org 의 후손인지 확인
        all_orgs = list_all_organizations(session)
        descendants = _descendant_ids(all_orgs, organization_id)
        if new_parent_id in descendants:
            raise OrganizationInvalidMoveError(
                f"후손 조직으로 이동할 수 없습니다: "
                f"organization_id={organization_id} → new_parent_id={new_parent_id}"
            )

    # 이동 후 동명 충돌 검사
    if new_parent_id is None:
        duplicate = session.execute(
            select(Organization).where(
                Organization.parent_id.is_(None),
                Organization.name == org.name,
                Organization.id != organization_id,
            )
        ).scalar_one_or_none()
    else:
        duplicate = session.execute(
            select(Organization).where(
                Organization.parent_id == new_parent_id,
                Organization.name == org.name,
                Organization.id != organization_id,
            )
        ).scalar_one_or_none()

    if duplicate is not None:
        raise DuplicateOrganizationNameError(
            f"이동 대상 위치에 동일한 조직명이 이미 존재합니다: {org.name!r}"
        )

    old_parent_id = org.parent_id
    org.parent_id = new_parent_id
    session.flush()
    logger.info(
        "조직 이동: id={} parent_id {} → {}",
        organization_id,
        old_parent_id,
        new_parent_id,
    )
    return org


def delete_organization(session: Session, organization_id: int) -> None:
    """조직을 삭제한다. 자식이 있으면 OrganizationHasChildrenError 를 발생시킨다.

    DB FK(ON DELETE RESTRICT) 가 최종 방어선이지만,
    app 레벨에서 먼저 자식 존재 여부를 확인하여 사용자 친화적 오류를 제공한다.

    삭제된 조직의 user_organizations 매핑은 FK CASCADE 로 자동 제거된다.
    commit 은 호출자 책임.

    Args:
        session: 호출자 세션.
        organization_id: 삭제할 조직 PK.

    Raises:
        OrganizationNotFoundError: 해당 organization_id 가 없을 때.
        OrganizationHasChildrenError: 직속 자식 조직이 하나 이상 존재할 때.
    """
    org = session.get(Organization, organization_id)
    if org is None:
        raise OrganizationNotFoundError(
            f"조직을 찾을 수 없습니다: organization_id={organization_id}"
        )

    # 자식 존재 여부 확인 — DB RESTRICT 에 앞선 app-level 방어선.
    child_count_row = session.execute(
        select(Organization).where(Organization.parent_id == organization_id).limit(1)
    ).scalar_one_or_none()
    if child_count_row is not None:
        raise OrganizationHasChildrenError(
            f"하위 조직이 있는 조직은 삭제할 수 없습니다: "
            f"organization_id={organization_id} name={org.name!r}. "
            f"먼저 하위 조직을 모두 삭제해 주세요."
        )

    session.delete(org)
    session.flush()
    logger.info("조직 삭제: id={} name={!r}", organization_id, org.name)


# ──────────────────────────────────────────────────────────────
# 사용자-조직 매핑 조회 / 변경
# ──────────────────────────────────────────────────────────────


def get_user_organization_ids(session: Session, user_id: int) -> list[int]:
    """사용자가 속한 조직 ID 목록을 반환한다.

    Args:
        session: 호출자 세션.
        user_id: 조회 대상 사용자 PK.

    Returns:
        organization_id 정수 목록 (순서 미보장).
    """
    rows = session.execute(
        select(UserOrganization.organization_id).where(
            UserOrganization.user_id == user_id
        )
    ).scalars().all()
    return list(rows)


def set_user_organizations(
    session: Session,
    user_id: int,
    organization_ids: list[int],
) -> None:
    """사용자의 조직 소속을 주어진 목록으로 교체한다.

    기존 매핑을 전부 삭제하고 새 목록으로 INSERT 한다.
    organization_ids 가 빈 리스트이면 사용자를 모든 조직에서 제거한다.

    존재하지 않는 organization_id 가 포함된 경우 DB FK 에 의해 IntegrityError 가
    발생한다 — 호출자(라우트)가 적절한 HTTP 응답으로 변환한다.

    commit 은 호출자 책임.

    Args:
        session: 호출자 세션.
        user_id: 매핑을 변경할 사용자 PK.
        organization_ids: 새로 소속시킬 조직 PK 목록. 중복은 무시된다.

    Raises:
        sqlalchemy.exc.IntegrityError: 존재하지 않는 organization_id 포함 시.
    """
    # 기존 매핑 전부 삭제
    session.execute(
        delete(UserOrganization).where(UserOrganization.user_id == user_id)
    )

    # 중복 제거 후 삽입
    unique_ids = list(dict.fromkeys(organization_ids))
    for org_id in unique_ids:
        session.add(UserOrganization(user_id=user_id, organization_id=org_id))

    session.flush()
    logger.info(
        "사용자-조직 매핑 변경: user_id={} organization_ids={}",
        user_id,
        unique_ids,
    )


__all__ = [
    "DuplicateOrganizationNameError",
    "OrganizationHasChildrenError",
    "OrganizationInvalidMoveError",
    "OrganizationNotFoundError",
    "build_organization_tree",
    "create_organization",
    "delete_organization",
    "get_user_organization_ids",
    "list_all_organizations",
    "move_organization",
    "rename_organization",
    "set_user_organizations",
]
