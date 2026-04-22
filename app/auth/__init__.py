"""인증(회원가입/로그인/세션) 패키지.

이 패키지는 Phase 1b 에서 추가된 첫 실사용자 플로우를 담는다.
구성 모듈:
    - ``constants``: 쿠키 이름, 세션 수명, 비밀번호/아이디 정책 상수.
    - ``service``: 비밀번호 해시/검증, 사용자 생성, 세션 발급·조회·삭제.
    - ``dependencies``: FastAPI ``Depends`` 용 current_user 두 버전 + same-origin
      체크 헬퍼.

라우트(`app/auth/routes.py`)와 FastAPI mount 는 다음 subtask(00021-3) 에서
추가된다. 본 패키지는 해당 subtask 가 import 할 수 있는 형태로 공개 심볼을
`__all__` 에 나열한다.
"""

from __future__ import annotations

from app.auth import constants
from app.auth.dependencies import (
    current_user_optional,
    current_user_required,
    ensure_same_origin,
)
from app.auth.service import (
    DuplicateUsernameError,
    InvalidCredentialsError,
    PasswordPolicyError,
    UsernamePolicyError,
    authenticate,
    create_session,
    create_user,
    delete_session,
    get_active_session,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)

__all__ = [
    "constants",
    # service
    "DuplicateUsernameError",
    "InvalidCredentialsError",
    "PasswordPolicyError",
    "UsernamePolicyError",
    "authenticate",
    "create_session",
    "create_user",
    "delete_session",
    "get_active_session",
    "hash_password",
    "validate_password",
    "validate_username",
    "verify_password",
    # dependencies
    "current_user_optional",
    "current_user_required",
    "ensure_same_origin",
]
