"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers — Alembic 이 사용하는 식별자
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    """스키마를 한 단계 앞으로 적용한다."""
    # SQLite ALTER TABLE 호환성을 위해 batch_alter_table 을 사용한다.
    # 참고: docs/db_portability.md 4번 항목
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """스키마를 한 단계 이전으로 롤백한다."""
    ${downgrades if downgrades else "pass"}
