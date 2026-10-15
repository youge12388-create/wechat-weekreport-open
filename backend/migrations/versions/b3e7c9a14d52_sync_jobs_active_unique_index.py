"""sync_jobs 活跃任务唯一索引（并发重复入队兜底）

背景：``enqueue_sync_job`` 的幂等此前只靠应用层「先查再插」。两个进程同时穿过
那次检查时会各插一条 PENDING，同类型出现两条并行抓取（同一周期被拉两遍、
AI 分析重复计费）。本迁移把约束落到 DB 层：

    ``type`` 唯一，但仅约束 ``status IN ('pending', 'running')`` 的行。

- 只约束「未结束」的行：历史任务（success/failed/partial）照常累积，不受影响。
- 条件索引（partial index）SQLite 3.8+ 与 PostgreSQL 都支持，两种部署形态行为一致。
- 应用层 `_active_job` 检查**保留**：常规路径靠它直接返回原任务（不产生异常），
  索引只负责挡下并发穿透的那一次；冲突时应用层回查并返回已有任务。
- where 子句必须与 ``app/models/sync_job.py`` 的 ``_ACTIVE_STATUS_FILTER``
  逐字符一致，否则 autogenerate 会反复报差异。
- 建索引前必须无存量重复行，否则 upgrade 直接失败。执行前核对：
  ``SELECT type, COUNT(*) FROM sync_jobs WHERE status IN ('pending','running')
  GROUP BY type HAVING COUNT(*) > 1`` 应返回空结果。

Revision ID: b3e7c9a14d52
Revises: a7c4e1b90f23
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b3e7c9a14d52"
down_revision = "a7c4e1b90f23"
branch_labels = None
depends_on = None

# 必须与 app/models/sync_job.py 的 _ACTIVE_STATUS_FILTER 逐字符一致。
_ACTIVE_STATUS_FILTER = "status IN ('pending', 'running')"


def upgrade() -> None:
    op.create_index(
        "uq_sync_jobs_active_type",
        "sync_jobs",
        ["type"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_STATUS_FILTER),
        postgresql_where=sa.text(_ACTIVE_STATUS_FILTER),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_jobs_active_type", table_name="sync_jobs")
