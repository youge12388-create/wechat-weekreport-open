"""add dashboard feedback

经营看板结论的人工效度判定表。看板的自动化测试只能证明「代码按规则跑」，
证明不了「规则符合现实」；这张表是让「准不准」变成可统计事实的唯一来源。

Revision ID: a9c4e1f7b2d8
Revises: d7a1b4c9e2f3
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9c4e1f7b2d8"
down_revision: Union[str, None] = "d7a1b4c9e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dashboard_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("module", sa.String(length=32), nullable=False),
        sa.Column("signal_key", sa.String(length=500), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "signal_key", "reviewer", name="uq_dashboard_feedback_signal_reviewer"
        ),
    )
    op.create_index(
        "ix_dashboard_feedback_module_period",
        "dashboard_feedback",
        ["module", "period_start", "period_end"],
    )


def downgrade() -> None:
    op.drop_index("ix_dashboard_feedback_module_period", table_name="dashboard_feedback")
    op.drop_table("dashboard_feedback")
