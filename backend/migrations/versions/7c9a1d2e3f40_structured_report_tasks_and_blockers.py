"""structured report tasks and blockers

Revision ID: 7c9a1d2e3f40
Revises: 5ccebbba5d64
Create Date: 2026-09-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.base import JSONVariant


revision: str = "7c9a1d2e3f40"
down_revision: Union[str, None] = "5ccebbba5d64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("canonical_key", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("related_project", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("first_seen_date", sa.Date(), nullable=True),
        sa.Column("last_seen_date", sa.Date(), nullable=True),
        sa.Column("duration_weeks", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("canonical_key", name=op.f("uq_tasks_canonical_key")),
    )
    op.create_table(
        "report_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=True),
        sa.Column("item_type", sa.String(length=32), nullable=False),
        sa.Column("item_order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("related_project", sa.String(length=255), nullable=True),
        sa.Column("task_key", sa.String(length=500), nullable=True),
        sa.Column("mentioned_people", JSONVariant, nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(length=8), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="SET NULL", name=op.f("fk_report_items_employee_id_employees")),
        sa.ForeignKeyConstraint(["report_id"], ["weekly_reports.id"], ondelete="CASCADE", name=op.f("fk_report_items_report_id_weekly_reports")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_items")),
        sa.UniqueConstraint("report_id", "item_type", "item_order", name="uq_report_items_report_type_order"),
    )
    op.create_table(
        "report_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("total_score", sa.Integer(), nullable=False),
        sa.Column("completeness", sa.Integer(), nullable=True),
        sa.Column("specificity", sa.Integer(), nullable=True),
        sa.Column("results", sa.Integer(), nullable=True),
        sa.Column("advancement", sa.Integer(), nullable=True),
        sa.Column("planning", sa.Integer(), nullable=True),
        sa.Column("problem_quality", sa.Integer(), nullable=True),
        sa.Column("duplicate", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["weekly_reports.id"], ondelete="CASCADE", name=op.f("fk_report_scores_report_id_weekly_reports")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_scores")),
        sa.UniqueConstraint("report_id", name=op.f("uq_report_scores_report_id")),
    )
    op.create_table(
        "task_report_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("report_item_id", sa.Integer(), nullable=False),
        sa.Column("match_method", sa.String(length=32), nullable=False),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["weekly_reports.id"], ondelete="CASCADE", name=op.f("fk_task_report_links_report_id_weekly_reports")),
        sa.ForeignKeyConstraint(["report_item_id"], ["report_items.id"], ondelete="CASCADE", name=op.f("fk_task_report_links_report_item_id_report_items")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE", name=op.f("fk_task_report_links_task_id_tasks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_report_links")),
        sa.UniqueConstraint("report_item_id", name="uq_task_report_links_report_item"),
    )
    op.create_table(
        "blockers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("risk_level", sa.String(length=8), nullable=False),
        sa.Column("first_seen_date", sa.Date(), nullable=True),
        sa.Column("last_seen_date", sa.Date(), nullable=True),
        sa.Column("duration_weeks", sa.Integer(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE", name=op.f("fk_blockers_task_id_tasks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blockers")),
        sa.UniqueConstraint("task_id", name=op.f("uq_blockers_task_id")),
    )
    op.create_index("ix_report_items_report_type", "report_items", ["report_id", "item_type"], unique=False)
    op.create_index("ix_report_items_employee_type", "report_items", ["employee_id", "item_type"], unique=False)
    op.create_index("ix_tasks_status_last_seen", "tasks", ["status", "last_seen_date"], unique=False)
    op.create_index("ix_task_report_links_task_report", "task_report_links", ["task_id", "report_id"], unique=False)
    op.create_index("ix_blockers_status_risk_last", "blockers", ["status", "risk_level", "last_seen_date"], unique=False)
    op.create_index("ix_weekly_reports_employee_period", "weekly_reports", ["employee_id", "start_date", "end_date"], unique=False)
    op.create_index("ix_employees_department_id", "employees", ["department_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_employees_department_id", table_name="employees")
    op.drop_index("ix_weekly_reports_employee_period", table_name="weekly_reports")
    op.drop_index("ix_blockers_status_risk_last", table_name="blockers")
    op.drop_index("ix_task_report_links_task_report", table_name="task_report_links")
    op.drop_index("ix_tasks_status_last_seen", table_name="tasks")
    op.drop_index("ix_report_items_employee_type", table_name="report_items")
    op.drop_index("ix_report_items_report_type", table_name="report_items")
    op.drop_table("blockers")
    op.drop_table("task_report_links")
    op.drop_table("report_scores")
    op.drop_table("report_items")
    op.drop_table("tasks")
