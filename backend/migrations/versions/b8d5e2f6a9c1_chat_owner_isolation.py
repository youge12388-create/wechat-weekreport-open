"""chat owner isolation

AI 聊天记录按人隔离：ai_analyses 增加 owner_employee_id 列。
- 企微会话产生的聊天（search / agent_search）记录归属人员工 id；
- 密码超管（无员工身份）与迁移前的历史聊天为 NULL，语义是「归超管所有」；
- 非聊天类分析（report_analysis / summary 等）不使用该列。

Revision ID: b8d5e2f6a9c1
Revises: a9c4e1f7b2d8
Create Date: 2026-09-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8d5e2f6a9c1"
down_revision: Union[str, None] = "a9c4e1f7b2d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_analyses",
        sa.Column("owner_employee_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_ai_analyses_owner",
        "ai_analyses",
        ["analysis_type", "owner_employee_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_analyses_owner", table_name="ai_analyses")
    op.drop_column("ai_analyses", "owner_employee_id")
