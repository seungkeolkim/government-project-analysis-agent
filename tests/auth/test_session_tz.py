"""세션 만료 비교의 naive/aware 회귀 테스트.

SQLAlchemy ``DateTime(timezone=True)`` 는 SQLite 백엔드에서 timezone 을
저장하지 못해, INSERT 시 tz-aware 로 넣은 ``UserSession.expires_at`` 도
SELECT 시점에는 naive ``datetime`` 으로 되돌아온다.

회원가입 직후에는 SQLAlchemy ORM identity map 이 INSERT 직전의 객체를 그대로
들고 있어 ``expires_at`` 이 tz-aware 로 보이지만, 컨테이너 재기동이나
``session.expire_all()`` 로 캐시가 비워진 뒤 다시 SELECT 되면 naive 로
돌아온다. 이 상태에서 ``get_active_session`` 이 ``datetime.now(tz=UTC)`` 와
직접 비교하면 ``TypeError: can't compare offset-naive and offset-aware
datetimes`` 가 발생하고, ``current_user_optional`` → ``index_page`` 경로가
500 으로 떨어진다.

본 회귀 테스트는 SELECT 직후 ``expires_at`` 이 naive 로 되돌아오는 경로를
재현(``session.expire_all()``) 하고, 패치된 ``get_active_session`` 이 그
상태에서도 정상으로 ``UserSession`` 을 돌려주는지를 확인한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.auth.service import (
    create_session,
    create_user,
    get_active_session,
)
from app.db.models import UserSession


def _make_user_with_session(
    db_session: Session,
    *,
    username: str,
    issued_at: datetime,
) -> UserSession:
    """테스트 보조: 사용자 + 세션 1건을 만들고 commit 까지 끝낸 상태로 반환한다.

    commit 후에 호출자가 ``session.expire_all()`` 만 호출하면 다음 SELECT 는
    DB 에서 새로 읽어오므로 SQLite 의 naive ``expires_at`` 회귀 시나리오를
    바로 재현할 수 있다.
    """
    user = create_user(db_session, username=username, password="password_for_test_1")
    db_session.flush()
    user_session = create_session(db_session, user, now=issued_at)
    db_session.commit()
    return user_session


def test_get_active_session_handles_naive_expires_at_after_reload(
    db_session: Session,
) -> None:
    """SQLite reload 로 naive 로 돌아온 expires_at 도 정상 비교된다.

    재현 절차:
        1) create_session 으로 세션 발급 + commit.
        2) ``session.expire_all()`` 로 ORM identity map 을 비워, 다음 접근에서
           DB 로부터 다시 SELECT 되도록 강제한다.
        3) get_active_session 호출. 패치 전에는 TypeError 가 나면서 None 도
           아닌 예외가 propagate 되었지만, 패치 후에는 정상으로
           ``UserSession`` 을 반환해야 한다.
    """
    issued_at = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    user_session = _make_user_with_session(
        db_session, username="naive_user", issued_at=issued_at
    )
    session_id = user_session.session_id

    # SQLite 가 naive 로 되돌려주는 reload 경로를 재현한다.
    db_session.expire_all()

    # 만료 전 시각을 명시적으로 주입해 결과가 흔들리지 않게 한다.
    check_time = issued_at + timedelta(days=1)
    resolved = get_active_session(db_session, session_id, now=check_time)

    assert resolved is not None, (
        "SQLite reload 후 naive expires_at 비교가 실패해서는 안 된다 "
        "(get_active_session 이 None 이거나 TypeError 를 일으키면 회귀)."
    )
    assert resolved.session_id == session_id


def test_get_active_session_returns_none_for_expired_naive_expires_at(
    db_session: Session,
) -> None:
    """만료 시각 이후의 naive expires_at 도 정상으로 만료 판정된다.

    naive 보정이 비교 자체를 살리는 것뿐 아니라, "만료된 세션은 None" 이라는
    원래의 의미도 보존되는지를 별도 케이스로 확인한다.
    """
    issued_at = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    user_session = _make_user_with_session(
        db_session, username="expired_user", issued_at=issued_at
    )
    session_id = user_session.session_id
    expires_at = user_session.expires_at

    db_session.expire_all()

    # 만료 시각을 1초 넘긴 시점을 주입한다.
    check_time = expires_at + timedelta(seconds=1)
    assert get_active_session(db_session, session_id, now=check_time) is None


def test_get_active_session_handles_tz_aware_now_with_naive_stored_expires_at(
    db_session: Session,
) -> None:
    """now 인자를 생략(=tz-aware 자동 주입) 해도 naive stored 와 비교가 안전하다.

    실제 운영 경로에서는 ``current_user_optional`` 이 now 를 주입하지 않고
    ``get_active_session`` 을 호출하므로, ``_resolve_now`` 의 tz-aware 기본값과
    SQLite reload 후 naive ``expires_at`` 의 조합도 별도 케이스로 검증한다.
    """
    issued_at = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    user_session = _make_user_with_session(
        db_session, username="now_default_user", issued_at=issued_at
    )
    session_id = user_session.session_id

    db_session.expire_all()

    # now 를 생략해 _resolve_now 의 tz-aware 기본값을 사용. 만료 30일 전이라
    # 결과는 항상 활성 세션이어야 한다.
    resolved = get_active_session(db_session, session_id)
    assert resolved is not None
    assert resolved.session_id == session_id
