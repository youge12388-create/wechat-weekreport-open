"""add hot-path indexes

补齐三处高频查询缺的索引：

- ``weekly_reports.template_id``：同步时每篇周报都要按 template_id 做两次
  ``GROUP BY ... LIMIT 1`` 聚合来补正分类；该列此前无索引，回填 365 篇是 O(N²) 全表扫。
- ``sync_jobs(status)`` / ``sync_jobs(type, end_time)``：``/api/sync/status`` 与
  通知中心轮询每次都要扫 PENDING/RUNNING 计数与「最新一条任务」，无索引时随日志表增长变慢。
- ``analysis_queue(status, next_attempt_at)``：Worker 每轮轮询都按这两个字段取待办。

Revision ID: a7c4e1b90f23
Revises: f1b2c3d4e5a6
Create Date: 2026-09-16
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "a7c4e1b90f23"
down_revision = "f1b2c3d4e5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_weekly_reports_template_id", "weekly_reports", ["template_id"], unique=False
    )
    op.create_index(
        "ix_sync_jobs_status", "sync_jobs", ["status"], unique=False
    )
    op.create_index(
        "ix_sync_jobs_type_end_time", "sync_jobs", ["type", "end_time"], unique=False
    )
    op.create_index(
        "ix_analysis_queue_status_next_attempt",
        "analysis_queue",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_period_summary_queue_status_next_attempt",
        "period_summary_queue",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_period_summary_queue_status_next_attempt", table_name="period_summary_queue")
    op.drop_index("ix_analysis_queue_status_next_attempt", table_name="analysis_queue")
    op.drop_index("ix_sync_jobs_type_end_time", table_name="sync_jobs")
    op.drop_index("ix_sync_jobs_status", table_name="sync_jobs")
    op.drop_index("ix_weekly_reports_template_id", table_name="weekly_reports")
