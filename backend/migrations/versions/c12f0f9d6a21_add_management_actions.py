"""add manual management action follow-up loop

Revision ID: c12f0f9d6a21
Revises: 7c9a1d2e3f40
Create Date: 2026-09-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.base import JSONVariant


revision: str = "c12f0f9d6a21"
down_revision: Union[str, None] = "7c9a1d2e3f40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "management_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=500), nullable=False),
        sa.Column("source_task_id", sa.Integer(), nullable=True),
        sa.Column("source_blocker_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("next_step", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("owner_employee_id", sa.Integer(), nullable=True),
        sa.Column("owner_department_id", sa.Integer(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_snapshot", JSONVariant, nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.Column("closed_by", sa.String(length=128), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
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
            ["source_task_id"], ["tasks.id"], ondelete="SET NULL",
            name=op.f("fk_management_actions_source_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["source_blocker_id"], ["blockers.id"], ondelete="SET NULL",
            name=op.f("fk_management_actions_source_blocker_id_blockers"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_employee_id"], ["employees.id"], ondelete="SET NULL",
            name=op.f("fk_management_actions_owner_employee_id_employees"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_department_id"], ["departments.id"], ondelete="SET NULL",
            name=op.f("fk_management_actions_owner_department_id_departments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_management_actions")),
        sa.UniqueConstraint(
            "source_type", "source_key", name="uq_management_actions_source"
        ),
    )
    op.create_index(
        "ix_management_actions_status_due",
        "management_actions",
        ["status", "due_date"],
        unique=False,
    )
    op.create_table(
        "management_action_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("changed_fields", JSONVariant, nullable=True),
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
            name=op.f("fk_management_action_events_action_id_management_actions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_management_action_events")),
    )
    op.create_index(
        "ix_management_action_events_action_created",
        "management_action_events",
        ["action_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_management_action_events_action_created",
        table_name="management_action_events",
    )
    op.drop_table("management_action_events")
    op.drop_index("ix_management_actions_status_due", table_name="management_actions")
    op.drop_table("management_actions")
