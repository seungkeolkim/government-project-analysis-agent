"""첫 관리자 계정 생성 CLI.

사용자 원문의 "첫 admin: scripts/create_admin.py CLI" 요구사항을 구현한다.
Phase 1b 에서 자유 회원가입이 열리지만 ``is_admin=True`` 사용자는 관리자
UI (Phase 2+) 가 활성화되기 전까지 DB 를 직접 만지지 않는 한 생성되지
않는다. 본 스크립트는 운영자가 컨테이너 안에서 한 줄로 첫 관리자 계정을
만들 수 있도록 한다.

동작 순서:
    (1) 로깅 초기화 (``configure_logging``).
    (2) 설정 로드 + ``ensure_runtime_paths()`` 로 DB 파일 디렉터리 보장.
    (3) ``init_db()`` 로 Alembic HEAD 까지 보장 (신규 DB 도 안전).
    (4) username 은 argparse positional 이거나, 생략 시 stdin prompt.
    (5) password 는 항상 :mod:`getpass` 로 2회 입력받아 일치 검증.
    (6) email 은 ``--email`` 인자 또는 선택 prompt (엔터 치면 생략).
    (7) :func:`app.auth.service.create_user` 를 ``is_admin=True`` 로 호출.
        중복/정책 위반이면 종료 코드 1 + 에러 로그.

설계 메모:
    - 핵심 로직은 :func:`create_admin_account` 에 분리해 테스트가 직접
      호출할 수 있게 한다 (guidance 제안). main() 은 prompt·파싱·세션
      관리만 담당한다.
    - ``session_scope()`` 를 써서 예외 발생 시 자동 rollback + close 보장.
      중복/정책 예외도 session_scope 가 한 번 rollback 한 뒤 전파되므로
      caller 가 별도 rollback 을 할 필요 없다.
    - getpass 는 TTY 가 없을 때 경고를 띄우며 동작하므로 Docker 컨테이너
      내부에서도 문제없이 쓰인다. 테스트에서는 의존성 주입(인자로 넘긴
      호출 함수)으로 대체할 수 있다.

실행 예:
    docker compose run --rm web python scripts/create_admin.py
    python scripts/create_admin.py root_user
    python scripts/create_admin.py root_user --email admin@example.com

종료 코드:
    0  : 관리자 계정 생성 성공.
    1  : 중복 username / 입력 정책 위반 / 기타 오류.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가 (scripts/ 에서 app 패키지를 임포트하기 위해)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger  # noqa: E402

from app.auth.service import (  # noqa: E402
    DuplicateUsernameError,
    PasswordPolicyError,
    UsernamePolicyError,
    create_user,
)
from app.config import get_settings  # noqa: E402
from app.db.init_db import init_db  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402


# ──────────────────────────────────────────────────────────────
# 핵심 로직 (테스트에서 직접 호출)
# ──────────────────────────────────────────────────────────────


def create_admin_account(
    *,
    username: str,
    password: str,
    email: str | None = None,
) -> User:
    """관리자(is_admin=True) 계정을 하나 생성한다.

    :func:`app.auth.service.create_user` 를 호출해 username/password 를 검증·
    정규화하고 bcrypt 해시를 저장한다. 트랜잭션 경계는 내부의
    :func:`session_scope` 가 담당하므로 호출자는 commit/rollback 을 고려할
    필요가 없다.

    Args:
        username: 원문 username 입력.
        password: 원문 비밀번호.
        email:    이메일(선택). None 또는 빈 문자열이면 저장하지 않는다.

    Returns:
        생성된 ``User`` 인스턴스. session 종료 후에도 필드 접근이 가능하도록
        ``expire_on_commit=False`` 가 전역 sessionmaker 에 적용되어 있다.

    Raises:
        DuplicateUsernameError: 이미 존재하는 username.
        UsernamePolicyError:    username 정책 위반.
        PasswordPolicyError:    password 정책 위반.
        ValueError:             email 형식 오류.
    """
    with session_scope() as session:
        user = create_user(
            session,
            username=username,
            password=password,
            email=email,
            is_admin=True,
        )
    return user


# ──────────────────────────────────────────────────────────────
# 입력 prompt 헬퍼 (테스트에서 fn 대체 가능)
# ──────────────────────────────────────────────────────────────


def _prompt_username(input_fn: Callable[[str], str] = input) -> str:
    """대화형으로 username 을 받는다. 빈 입력은 거절."""
    raw = input_fn("관리자 아이디(username): ").strip()
    if not raw:
        raise UsernamePolicyError("username 이 비어 있습니다.")
    return raw


def _prompt_password_confirmed(
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> str:
    """getpass 로 비밀번호를 2회 입력받아 일치하는지 확인한다.

    일치하지 않으면 PasswordPolicyError 를 발생시킨다 — 재시도는 상위에서
    결정한다 (여기서는 단발성). 빈 입력도 정책 위반으로 처리된다.
    """
    first = getpass_fn("비밀번호: ")
    second = getpass_fn("비밀번호 확인: ")
    if first != second:
        raise PasswordPolicyError("비밀번호 확인이 일치하지 않습니다.")
    if not first:
        raise PasswordPolicyError("비밀번호가 비어 있습니다.")
    return first


def _prompt_email_optional(
    input_fn: Callable[[str], str] = input,
) -> str | None:
    """이메일 prompt — 엔터로 건너뛸 수 있다."""
    raw = input_fn("이메일(선택, 엔터로 건너뛰기): ").strip()
    return raw if raw else None


# ──────────────────────────────────────────────────────────────
# argparse / main
# ──────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI 인자 파서를 구성한다."""
    parser = argparse.ArgumentParser(
        prog="python scripts/create_admin.py",
        description="첫 관리자(is_admin=True) 계정을 생성한다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python scripts/create_admin.py\n"
            "  python scripts/create_admin.py root_user\n"
            "  python scripts/create_admin.py root_user --email admin@example.com\n"
        ),
    )
    parser.add_argument(
        "username",
        nargs="?",
        default=None,
        help="관리자 아이디. 생략하면 stdin 으로 prompt.",
    )
    parser.add_argument(
        "--email",
        default=None,
        metavar="EMAIL",
        help="관리자 이메일 (선택). 생략 시 prompt 로 물어보며 빈 입력 가능.",
    )
    return parser


def main() -> int:
    """OS 진입점. 종료 코드를 반환한다 (sys.exit 는 ``if __name__`` 에서).

    Returns:
        0 = 성공, 1 = 중복/정책/기타 오류.
    """
    configure_logging()
    args = _build_arg_parser().parse_args()

    # 런타임 경로(SQLite 파일 디렉터리) 보장 + 스키마 HEAD upgrade.
    # 신규 호스트에서 최초 실행될 때에도 안전하게 돌아가도록 init_db 를
    # 한 번 부른다 (멱등).
    settings = get_settings()
    settings.ensure_runtime_paths()
    init_db()

    # username: 인자 있으면 그대로, 없으면 prompt.
    try:
        username = args.username if args.username else _prompt_username()
    except UsernamePolicyError as exc:
        logger.error("username 입력 오류: {}", exc)
        return 1

    # password: 항상 getpass 로 2회 확인.
    try:
        password = _prompt_password_confirmed()
    except PasswordPolicyError as exc:
        logger.error("비밀번호 입력 오류: {}", exc)
        return 1

    # email: --email 인자 있으면 그대로, 없으면 prompt (빈 입력 허용).
    if args.email is not None:
        # 인자에 빈 문자열이 들어오면 None 으로 정규화.
        email: str | None = args.email.strip() or None
    else:
        email = _prompt_email_optional()

    # 실제 생성 — 예외를 세부 종류별로 분기해 종료 코드를 결정한다.
    try:
        created_user = create_admin_account(
            username=username,
            password=password,
            email=email,
        )
    except DuplicateUsernameError as exc:
        logger.error("이미 존재하는 username 입니다: {!r} ({})", username, exc)
        return 1
    except (UsernamePolicyError, PasswordPolicyError, ValueError) as exc:
        logger.error("입력 검증 실패: {}", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - 외부 알림 성격의 최후 방어선
        logger.error("관리자 계정 생성 중 예기치 못한 오류: {} ({})",
                     type(exc).__name__, exc)
        return 1

    logger.info(
        "관리자 계정 생성 완료: id={} username={!r} email={!r} is_admin=True",
        created_user.id,
        created_user.username,
        created_user.email,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
