"""add feedback linked to own AI answer audits

Revision ID: f2c8a13d9e74
Revises: b8d5e2f6a9c1
Create Date: 2026-09-30
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2c8a13d9e74"
down_revision: Union[str, None] = "b8d5e2f6a9c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("reviewer_key", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["ai_analyses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_id", "reviewer_key", name="uq_agent_feedback_analysis_reviewer"
        ),
    )
    op.create_index(
        "ix_agent_feedback_reviewer_created",
        "agent_feedback",
        ["reviewer_key", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_feedback_reviewer_created", table_name="agent_feedback")
    op.drop_table("agent_feedback")
