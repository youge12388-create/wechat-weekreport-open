"""add reminder runs

Revision ID: f1b2c3d4e5a6
Revises: d3a8f2c6b1e4
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f1b2c3d4e5a6"
down_revision = "d3a8f2c6b1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminder_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trigger_key", sa.String(length=200), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminder_runs")),
        sa.UniqueConstraint("trigger_key", name=op.f("uq_reminder_runs_trigger_key")),
    )


def downgrade() -> None:
    op.drop_table("reminder_runs")
