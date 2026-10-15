"""add operating summary queue

经营看板结构化 AI 总结的独立队列。与管理简报的 period_summary_queue 分开建表：
同一周期两者都要有结论（一个 Markdown 周期总览、一个结构化 JSON），
共用一张表会让任一侧重算把另一侧标记过期。

Revision ID: d7a1b4c9e2f3
Revises: e6f7a8b9c0d1
Create Date: 2026-09-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7a1b4c9e2f3"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operating_summary_queue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("summary_type", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("report_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operating_summary_queue")),
        sa.UniqueConstraint(
            "summary_type",
            "start_date",
            "end_date",
            "department_id",
            name="uq_operating_summary_scope",
        ),
    )
    op.create_index(
        "ix_operating_summary_queue_status_next_attempt",
        "operating_summary_queue",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operating_summary_queue_status_next_attempt",
        table_name="operating_summary_queue",
    )
    op.drop_table("operating_summary_queue")
