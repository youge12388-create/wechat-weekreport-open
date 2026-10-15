"""add management action conversation messages

Revision ID: a3f8c2e91d47
Revises: f7c2d8a41b93
Create Date: 2026-09-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f8c2e91d47"
down_revision: Union[str, None] = "f7c2d8a41b93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "management_action_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=False),
        sa.Column("author_role", sa.String(length=16), nullable=False),
        sa.Column("author_employee_id", sa.Integer(), nullable=True),
        sa.Column("author_name", sa.String(length=128), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("owner_read_at", sa.DateTime(), nullable=True),
        sa.Column("admin_read_at", sa.DateTime(), nullable=True),
        sa.Column("notify_status", sa.String(length=16), nullable=False),
        sa.Column("notify_audience", sa.String(length=16), nullable=True),
        sa.Column("notify_error", sa.Text(), nullable=True),
        sa.Column("notify_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["action_id"], ["management_actions.id"], ondelete="CASCADE",
            name=op.f("fk_management_action_messages_action_id_management_actions"),
        ),
        sa.ForeignKeyConstraint(
            ["author_employee_id"], ["employees.id"], ondelete="SET NULL",
            name=op.f("fk_management_action_messages_author_employee_id_employees"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_management_action_messages")),
    )
    op.create_index(
        "ix_management_action_messages_action_created",
        "management_action_messages",
        ["action_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_management_action_messages_action_created",
        table_name="management_action_messages",
    )
    op.drop_table("management_action_messages")
