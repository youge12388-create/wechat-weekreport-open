from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time_utils import utcnow
from app.db.base import Base, JSONVariant
from app.models.enums import SyncJobStatus

# 「未结束任务」的判定条件。partial unique index 与应用层 `_active_job` 必须同口径，
# 否则 DB 挡下的行与应用层认出的行不是同一批，幂等语义就会自相矛盾。
# 该字符串必须与迁移 b3e7c9a14d52 中的字面量逐字符一致，否则 autogenerate 会反复报差异。
_ACTIVE_STATUS_FILTER = (
    f"status IN ('{SyncJobStatus.PENDING}', '{SyncJobStatus.RUNNING}')"
)


class SyncJob(Base):
    """同步任务日志（需求第 6.4 节 + 第 19 节）。

    记录：同步开始时间、结束时间、成功数量、失败数量、失败原因、状态。
    error 字段为结构化列表：[{external_id, code, message}, ...]，
    单条数据异常不中断批次（需求第 6.4 节）。
    """

    __tablename__ = "sync_jobs"

    __table_args__ = (
        Index("ix_sync_jobs_status", "status"),
        Index("ix_sync_jobs_type_end_time", "type", "end_time"),
        # 同一类型同时最多一个未结束任务。应用层 `enqueue_sync_job` 已做幂等检查，
        # 这里是**不依赖进程**的最终防线：多 Worker / 多实例并发入队时，
        # 两个进程都通过应用层检查的情况下，第二条 INSERT 会被 DB 挡下。
        # 条件索引（partial index）SQLite 3.8+ 与 PostgreSQL 都支持，
        # 只约束 PENDING/RUNNING 的行，历史任务照常累积，不受影响。
        Index(
            "uq_sync_jobs_active_type",
            "type",
            unique=True,
            sqlite_where=text(_ACTIVE_STATUS_FILTER),
            postgresql_where=text(_ACTIVE_STATUS_FILTER),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SyncJobStatus.RUNNING
    )
    start_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    # 本次同步的入参（时间区间、模板过滤等），便于复现问题
    params: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
