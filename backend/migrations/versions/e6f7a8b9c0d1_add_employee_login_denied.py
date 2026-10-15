"""allow an employee authorization to explicitly override department access"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "c4d9e1a7b235"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("login_denied", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("employees", "login_denied")
