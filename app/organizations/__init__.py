"""조직(Organization) 도메인 패키지.

조직 트리 CRUD 와 사용자-조직 M:N 매핑 관리를 담는다.

구성 모듈:
    - ``service``: 조직 생성/삭제/트리 조회 + 사용자-조직 매핑 변경·조회.

후속 UI 라우트(00049-2 개인 설정, 00049-3/4 관리자 페이지)가 이 패키지의
service 함수를 직접 호출한다.
"""

from __future__ import annotations

from app.organizations.service import (
    DuplicateOrganizationNameError,
    OrganizationHasChildrenError,
    OrganizationNotFoundError,
    build_organization_tree,
    create_organization,
    delete_organization,
    get_user_organization_ids,
    list_all_organizations,
    set_user_organizations,
)

__all__ = [
    "DuplicateOrganizationNameError",
    "OrganizationHasChildrenError",
    "OrganizationNotFoundError",
    "build_organization_tree",
    "create_organization",
    "delete_organization",
    "get_user_organization_ids",
    "list_all_organizations",
    "set_user_organizations",
]
