"""scripts/create_admin.py 의 핵심 로직 테스트.

subprocess 대신 ``main()`` 과 ``create_admin_account()`` 를 직접 호출하고,
prompt 헬퍼(_prompt_username / _prompt_password_confirmed /
_prompt_email_optional) 를 monkeypatch 로 대체한다 (guidance 제안).
실행 가능한 CLI 인지보다 **is_admin=True 로 계정이 생성되는지 + 중복/오류
분기가 올바른 종료 코드를 돌려주는지** 를 pin 한다.

scripts/ 는 패키지가 아니라서 ``import scripts.create_admin`` 이 불가하다.
파일 경로로 importlib 로드해서 test 모듈 이름으로 매핑한다.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy.orm import Session

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_create_admin_module() -> ModuleType:
    """``scripts/create_admin.py`` 를 임의 이름 모듈로 로드한다.

    scripts/ 가 패키지가 아니어서 ``import scripts.create_admin`` 은 실패한다.
    importlib 로 파일 경로를 직접 지정해 로드하고 sys.modules 에 캐시한다.
    """
    module_name = "scripts_create_admin_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]

    script_path = _PROJECT_ROOT / "scripts" / "create_admin.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ──────────────────────────────────────────────────────────────
# 핵심 로직 — create_admin_account 직접 호출
# ──────────────────────────────────────────────────────────────


def test_create_admin_account_creates_admin_row(db_session: Session) -> None:
    """create_admin_account 호출 시 is_admin=True 사용자가 DB 에 생성된다."""
    from app.auth.service import verify_password
    from app.db.models import User

    module = _load_create_admin_module()

    created = module.create_admin_account(
        username="root_user",
        password="super_admin_password_1",
        email="admin@example.com",
    )
    assert created.id is not None
    assert created.username == "root_user"
    assert created.is_admin is True
    assert created.email == "admin@example.com"
    # 해시가 제대로 적용되었는지 (평문 저장 아님)
    assert verify_password("super_admin_password_1", created.password_hash) is True

    # 별도 세션으로 재조회해 실제 commit 되었는지 확인.
    reloaded = db_session.query(User).filter_by(username="root_user").one()
    assert reloaded.is_admin is True


def test_create_admin_account_duplicate_raises(db_session: Session) -> None:
    """동일 username 으로 두 번 호출하면 DuplicateUsernameError 가 올라온다."""
    from app.auth.service import DuplicateUsernameError

    module = _load_create_admin_module()

    module.create_admin_account(
        username="only_admin",
        password="first_password_1",
    )
    with pytest.raises(DuplicateUsernameError):
        module.create_admin_account(
            username="only_admin",
            password="second_password_2",
        )


def test_create_admin_account_invalid_password_raises(db_session: Session) -> None:
    """비밀번호 정책 위반(8자 미만)은 PasswordPolicyError 로 전파된다."""
    from app.auth.service import PasswordPolicyError

    module = _load_create_admin_module()

    with pytest.raises(PasswordPolicyError):
        module.create_admin_account(
            username="short_password_admin",
            password="short",  # 8자 미만
        )


# ──────────────────────────────────────────────────────────────
# main() — argv + helper 들을 monkeypatch 로 대체
# ──────────────────────────────────────────────────────────────


def test_main_success_with_all_args(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """argv 로 username/email 을 받고 password prompt 만 patch 해서 main() 호출.

    종료 코드 0 이 돌아오고 DB 에 is_admin=True 사용자가 남는다.
    """
    from app.db.models import User

    module = _load_create_admin_module()

    monkeypatch.setattr(
        sys, "argv",
        ["scripts/create_admin.py", "main_admin", "--email", "main@example.com"],
    )
    monkeypatch.setattr(
        module, "_prompt_password_confirmed",
        lambda: "main_admin_password_1",
    )

    exit_code = module.main()
    assert exit_code == 0

    created = db_session.query(User).filter_by(username="main_admin").one()
    assert created.is_admin is True
    assert created.email == "main@example.com"


def test_main_prompts_username_when_missing(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """username 인자 생략 시 _prompt_username() 을 통해 받는다.

    email 인자도 생략 시 _prompt_email_optional() 로 처리 — 빈 입력을 흉내내
    None 을 반환하도록 patch.
    """
    from app.db.models import User

    module = _load_create_admin_module()

    # argv 에 username / --email 없음 → 양쪽 prompt 경로
    monkeypatch.setattr(sys, "argv", ["scripts/create_admin.py"])
    monkeypatch.setattr(module, "_prompt_username", lambda: "prompted_admin")
    monkeypatch.setattr(
        module, "_prompt_password_confirmed",
        lambda: "prompted_password_1",
    )
    monkeypatch.setattr(module, "_prompt_email_optional", lambda: None)

    exit_code = module.main()
    assert exit_code == 0

    created = db_session.query(User).filter_by(username="prompted_admin").one()
    assert created.is_admin is True
    assert created.email is None


def test_main_returns_1_on_duplicate(
    db_session: Session,  # noqa: ARG001 - fixture 가 DB 엔진을 준비만 함
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이미 같은 username 이 있으면 main() 이 1 을 반환한다."""
    module = _load_create_admin_module()

    # 먼저 같은 이름의 사용자를 하나 만들어 둔다.
    module.create_admin_account(username="dup_admin", password="first_password_1")

    monkeypatch.setattr(
        sys, "argv", ["scripts/create_admin.py", "dup_admin"]
    )
    monkeypatch.setattr(
        module, "_prompt_password_confirmed",
        lambda: "second_password_2",
    )
    monkeypatch.setattr(module, "_prompt_email_optional", lambda: None)

    exit_code = module.main()
    assert exit_code == 1


def test_main_returns_1_on_password_mismatch(
    db_session: Session,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """비밀번호 2회 입력이 일치하지 않을 때 main() 은 1 을 반환한다.

    실제 helper 가 PasswordPolicyError 를 raise 하는 상황을 흉내낸다.
    """
    module = _load_create_admin_module()

    def raise_mismatch() -> str:
        raise module.PasswordPolicyError("비밀번호 확인이 일치하지 않습니다.")

    monkeypatch.setattr(
        sys, "argv", ["scripts/create_admin.py", "mismatch_admin"]
    )
    monkeypatch.setattr(module, "_prompt_password_confirmed", raise_mismatch)
    monkeypatch.setattr(module, "_prompt_email_optional", lambda: None)

    exit_code = module.main()
    assert exit_code == 1


def test_main_returns_1_on_short_password(
    db_session: Session,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_prompt_password_confirmed 는 통과했지만 create_user 의 validate_password 가
    8자 미만을 거절해 main() 이 1 을 반환한다."""
    module = _load_create_admin_module()

    monkeypatch.setattr(
        sys, "argv", ["scripts/create_admin.py", "weak_pw_admin"]
    )
    monkeypatch.setattr(module, "_prompt_password_confirmed", lambda: "short")
    monkeypatch.setattr(module, "_prompt_email_optional", lambda: None)

    exit_code = module.main()
    assert exit_code == 1


def test_main_returns_1_on_empty_username(
    db_session: Session,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """username prompt 에서 빈 입력을 받아 UsernamePolicyError 가 난 경우."""
    module = _load_create_admin_module()

    def raise_empty_username() -> str:
        raise module.UsernamePolicyError("username 이 비어 있습니다.")

    monkeypatch.setattr(sys, "argv", ["scripts/create_admin.py"])
    monkeypatch.setattr(module, "_prompt_username", raise_empty_username)
    # password / email prompt 에 도달하기 전에 종료되지만 방어적으로 설정.
    monkeypatch.setattr(
        module, "_prompt_password_confirmed", lambda: "never_used_password_1"
    )
    monkeypatch.setattr(module, "_prompt_email_optional", lambda: None)

    exit_code = module.main()
    assert exit_code == 1
