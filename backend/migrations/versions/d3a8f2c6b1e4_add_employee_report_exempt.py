"""add employee report exempt

Revision ID: d3a8f2c6b1e4
Revises: 9b7f2c4d1a6e
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d3a8f2c6b1e4"
down_revision = "9b7f2c4d1a6e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column(
            "report_exempt",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("employees", "report_exempt")
