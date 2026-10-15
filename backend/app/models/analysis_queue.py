from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import AnalysisQueueStatus


class AnalysisQueue(Base, TimestampMixin):
    """单篇周报的后台 AI 分析队列。

    每条周报只有一个队列位（report_id 唯一）：同步发现新增或正文变化时，
    把对应行重置为 pending，由独立 Worker 消费。分析结论本身写入
    ai_analyses（report_analysis），本表只负责「哪些还没跑 / 跑到哪一步 / 失败几次」。
    """

    __tablename__ = "analysis_queue"

    __table_args__ = (
        Index("ix_analysis_queue_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_reports.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AnalysisQueueStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    # 失败退避后的可重试时间；为空表示立即可取
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
