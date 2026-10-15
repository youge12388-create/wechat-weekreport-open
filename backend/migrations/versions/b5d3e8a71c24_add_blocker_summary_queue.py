"""add blocker summary queue

Revision ID: b5d3e8a71c24
Revises: e5b9c2d3a104
Create Date: 2026-09-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5d3e8a71c24"
down_revision: Union[str, None] = "e5b9c2d3a104"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blocker_summary_queue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("blocker_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blocker_summary_queue")),
        sa.UniqueConstraint("start_date", "end_date", name="uq_blocker_summary_queue_period"),
    )
    op.create_index(
        "ix_blocker_summary_queue_status_next_attempt",
        "blocker_summary_queue",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_blocker_summary_queue_status_next_attempt", table_name="blocker_summary_queue")
    op.drop_table("blocker_summary_queue")
