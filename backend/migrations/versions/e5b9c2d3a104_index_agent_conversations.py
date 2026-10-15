"""index agent conversations

Revision ID: e5b9c2d3a104
Revises: d4f8a1c2b903
Create Date: 2026-09-16
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "e5b9c2d3a104"
down_revision = "d4f8a1c2b903"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_analyses",
        sa.Column("conversation_id", sa.String(length=100), nullable=True),
    )
    # 跨 SQLite / PostgreSQL 的一次性回填：不依赖任一数据库的 JSON 专有操作符。
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, result_json FROM ai_analyses "
            "WHERE analysis_type IN ('search', 'agent_search') AND result_json IS NOT NULL"
        )
    )
    for row in rows:
        data = row.result_json
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                continue
        conversation_id = data.get("conversation_id") if isinstance(data, dict) else None
        if conversation_id:
            bind.execute(
                sa.text("UPDATE ai_analyses SET conversation_id = :value WHERE id = :id"),
                {"value": str(conversation_id)[:100], "id": row.id},
            )
    op.create_index(
        "ix_ai_analyses_conversation",
        "ai_analyses",
        ["analysis_type", "conversation_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_analyses_conversation", table_name="ai_analyses")
    op.drop_column("ai_analyses", "conversation_id")
