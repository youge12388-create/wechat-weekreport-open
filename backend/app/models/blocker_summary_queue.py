from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Index, Date, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import AnalysisQueueStatus


class BlockerSummaryQueue(Base, TimestampMixin):
    """周期卡点 AI 总结的后台任务位。

    与 ``PeriodSummaryQueue`` 同一套状态机：任务按周期唯一；payload_hash
    是该周期全部卡点（含原文依据）的指纹，卡点新增、解决或依据正文变化后
    会把成功结果标记为过期，绝不把旧总结伪装成当前数据。
    """

    __tablename__ = "blocker_summary_queue"
    __table_args__ = (
        UniqueConstraint("start_date", "end_date", name="uq_blocker_summary_queue_period"),
        Index("ix_blocker_summary_queue_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    blocker_count: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AnalysisQueueStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
