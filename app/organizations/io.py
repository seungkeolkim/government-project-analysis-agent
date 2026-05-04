"""조직 트리 JSON 직렬화(export) / 역직렬화(import) 서비스.

import 시 조직 수 변경(추가/삭제/ID 변경)으로 user_organizations 와의
FK 관계가 깨지는 경우, 에러를 내지 않고 해당 매핑 행을 자동으로 삭제하는
방어 로직을 포함한다.

모든 함수는 호출자가 전달한 Session 을 그대로 사용하며,
commit/rollback 은 호출자가 제어한다 (flush 까지만 수행).
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import Organization, UserOrganization
from app.organizations.service import (
    DuplicateOrganizationNameError,
    build_organization_tree,
    list_all_organizations,
)


def export_organization_tree_json(session: Session) -> str:
    """조직 트리 전체를 pretty-print JSON 문자열로 직렬화해 반환한다.

    한국어 조직명이 유니코드 이스케이프 없이 그대로 출력된다.
    반환된 문자열은 import_organization_tree_json 의 입력으로 바로 사용할 수 있다.

    Args:
        session: 호출자 세션.

    Returns:
        indent=2, ensure_ascii=False 로 직렬화된 JSON 문자열.
        조직이 없으면 \"[]\" 를 반환한다.
    """
    orgs = list_all_organizations(session)
    tree = build_organization_tree(orgs)
    return json.dumps(tree, ensure_ascii=False, indent=2)


def _build_org_name_path_map(orgs: list[Organization]) -> dict[int, tuple[str, ...]]:
    """Organization 목록으로부터 org_id → 루트부터의 이름 경로 매핑을 빌드한다.

    동일 위치 동명이 없다는 invariant 위에서 tuple[str, ...] 이 unique key 가 된다.

    Args:
        orgs: list_all_organizations 가 반환한 평탄한 목록.

    Returns:
        {org_id: (\"루트명\", ..., \"자신명\"), ...} 형태의 딕셔너리.
    """
    id_to_org: dict[int, Organization] = {org.id: org for org in orgs}
    path_cache: dict[int, tuple[str, ...]] = {}

    def _resolve(org_id: int) -> tuple[str, ...]:
        """org_id 의 이름 경로를 재귀적으로 계산한다."""
        if org_id in path_cache:
            return path_cache[org_id]
        org = id_to_org[org_id]
        if org.parent_id is None:
            path: tuple[str, ...] = (org.name,)
        else:
            path = _resolve(org.parent_id) + (org.name,)
        path_cache[org_id] = path
        return path

    return {org_id: _resolve(org_id) for org_id in id_to_org}


def _insert_nodes_recursive(
    session: Session,
    nodes: list[dict[str, Any]],
    parent_id: int | None,
    parent_path: tuple[str, ...],
    name_path_to_id: dict[tuple[str, ...], int],
) -> None:
    """JSON 노드 목록을 재귀적으로 organizations 테이블에 INSERT 한다.

    name 과 children 만 읽고 id / parent_id 필드는 무시한다.
    INSERT 후 flush 로 확정된 org.id 를 name_path_to_id 에 기록한다.

    Args:
        session: 호출자 세션.
        nodes: 처리할 JSON 노드 목록 (같은 depth / 같은 parent 레벨).
        parent_id: 삽입할 부모 조직 PK. None 이면 루트 레벨.
        parent_path: 부모까지의 이름 경로.
        name_path_to_id: 채워질 이름 경로 → 새 org_id 매핑 (out-parameter).

    Raises:
        ValueError: name 필드 누락 또는 공백 제거 후 빈 문자열.
        DuplicateOrganizationNameError: 같은 레벨에 동명 조직이 있을 때.
    """
    seen_names: set[str] = set()
    for node in nodes:
        raw_name = node.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(
                f"조직 노드에 유효한 'name' 필드가 없습니다: {node!r}"
            )
        name = raw_name.strip()
        if name in seen_names:
            raise DuplicateOrganizationNameError(
                f"같은 레벨에 동일한 조직명이 중복됩니다: {name!r}"
            )
        seen_names.add(name)

        org = Organization(name=name, parent_id=parent_id)
        session.add(org)
        session.flush()  # id 확정

        current_path = parent_path + (name,)
        name_path_to_id[current_path] = org.id

        children = node.get("children", [])
        if children:
            _insert_nodes_recursive(
                session, children, org.id, current_path, name_path_to_id
            )


def import_organization_tree_json(
    session: Session,
    json_text: str,
) -> dict[str, int]:
    """JSON 텍스트를 파싱하여 조직 트리를 DB에 교체한다.

    처리 순서:
    1. 현재 조직 트리로부터 org_id → 이름 경로 맵을 빌드한다.
    2. user_organizations 의 (user_id, 이름 경로) 스냅샷을 메모리에 저장한다.
    3. user_organizations / organizations 행 전체를 삭제한다.
    4. JSON 에서 새 트리를 INSERT — name_path_to_id 맵을 채운다.
    5. 스냅샷에서 이름 경로가 새 트리에 존재하는 매핑만 user_organizations 에 재삽입.
       이름 경로가 사라진 매핑은 조용히 드롭 (FK 에러 없음).
    6. 통계를 반환한다.

    commit/rollback 은 호출자 책임.

    Args:
        session: 호출자 세션.
        json_text: export_organization_tree_json 이 생성한 JSON 문자열 (또는 동형 포맷).
                   최상위는 list 여야 한다.

    Returns:
        {
            \"total_organizations\": 새로 삽입된 조직 총수,
            \"dropped_user_org_count\": 이름 경로 불일치로 삭제된 user_organizations 행 수,
            \"affected_user_count\": 하나 이상의 매핑이 삭제된 사용자 수,
        }

    Raises:
        json.JSONDecodeError: json_text 가 유효한 JSON 이 아닐 때.
        ValueError: 최상위가 list 가 아닐 때, 또는 name 필드 누락/빈값.
        DuplicateOrganizationNameError: 같은 레벨에 동명 조직 존재 시.
    """
    # ── 1. JSON 파싱 ──────────────────────────────────────────────
    # json.JSONDecodeError 는 ValueError 서브클래스이므로 그대로 전파한다.
    nodes: Any = json.loads(json_text)
    if not isinstance(nodes, list):
        raise ValueError(
            f"import JSON 최상위는 list 여야 합니다. 실제 타입: {type(nodes).__name__}"
        )

    # ── 2. user_organizations 스냅샷 ─────────────────────────────
    existing_orgs = list_all_organizations(session)
    org_id_to_name_path = _build_org_name_path_map(existing_orgs)

    user_org_rows = session.execute(
        select(UserOrganization.user_id, UserOrganization.organization_id)
    ).all()
    # organization_id 가 알려진 경로를 가진 행만 포함 (고아 행은 스킵).
    user_org_snapshot: list[tuple[int, tuple[str, ...]]] = [
        (row.user_id, org_id_to_name_path[row.organization_id])
        for row in user_org_rows
        if row.organization_id in org_id_to_name_path
    ]
    logger.info(
        "import 스냅샷: user_organizations {} 행 이름 경로로 변환",
        len(user_org_snapshot),
    )

    # ── 3. 기존 테이블 전체 삭제 ──────────────────────────────────
    # user_organizations 먼저 삭제하여 FK 의존을 제거한다.
    session.execute(delete(UserOrganization))
    # SQLite 에서 FK enforcement 가 비활성화(기본값)되어 있으나,
    # 명시적으로 user_organizations 를 먼저 삭제해 의도를 명확히 한다.
    session.execute(delete(Organization))
    session.flush()

    # ── 4. 새 트리 INSERT ─────────────────────────────────────────
    name_path_to_id: dict[tuple[str, ...], int] = {}
    _insert_nodes_recursive(session, nodes, None, (), name_path_to_id)
    total_organizations = len(name_path_to_id)
    logger.info("import: 조직 {} 개 삽입 완료", total_organizations)

    # ── 5. user_organizations 재생성 + 드롭 카운트 집계 ──────────
    dropped_count = 0
    affected_users: set[int] = set()

    for user_id, name_path in user_org_snapshot:
        new_org_id = name_path_to_id.get(name_path)
        if new_org_id is None:
            # 이름 경로가 새 트리에 없음 — FK 에러 없이 조용히 드롭.
            dropped_count += 1
            affected_users.add(user_id)
            logger.debug(
                "user_organizations 드롭: user_id={} path={}", user_id, name_path
            )
        else:
            session.add(UserOrganization(user_id=user_id, organization_id=new_org_id))

    session.flush()
    logger.info(
        "import 완료: 총 조직={}, 드롭된 user_org={}, 영향받은 사용자={}",
        total_organizations,
        dropped_count,
        len(affected_users),
    )

    return {
        "total_organizations": total_organizations,
        "dropped_user_org_count": dropped_count,
        "affected_user_count": len(affected_users),
    }


__all__ = [
    "export_organization_tree_json",
    "import_organization_tree_json",
]
