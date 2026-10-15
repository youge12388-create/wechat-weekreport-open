"""add message recall support

Revision ID: c4d9e1a7b235
Revises: a3f8c2e91d47
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.base import JSONVariant


revision: str = "c4d9e1a7b235"
down_revision: Union[str, None] = "a3f8c2e91d47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 全部可空：存量消息本来就没有 msgid（撤不回来），也不影响读写。
    op.add_column(
        "management_action_messages",
        sa.Column("notify_msgids", JSONVariant, nullable=True),
    )
    op.add_column(
        "management_action_messages",
        sa.Column("recalled_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "management_action_messages",
        sa.Column("recalled_by_role", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "management_action_messages",
        sa.Column("recall_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("management_action_messages", "recall_error")
    op.drop_column("management_action_messages", "recalled_by_role")
    op.drop_column("management_action_messages", "recalled_at")
    op.drop_column("management_action_messages", "notify_msgids")
