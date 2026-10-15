"""add ai analysis deleted_at

Revision ID: 9b7f2c4d1a6e
Revises: e42a1b7c9d30
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "9b7f2c4d1a6e"
down_revision = "e42a1b7c9d30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_analyses",
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_analyses", "deleted_at")
