from __future__ import annotations

from sqlalchemy import Index, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class TaskReportLink(Base, TimestampMixin):
    """任务与一条周报结构化事项的可追溯关联。"""

    __tablename__ = "task_report_links"
    __table_args__ = (
        UniqueConstraint("report_item_id", name="uq_task_report_links_report_item"),
        Index("ix_task_report_links_task_report", "task_id", "report_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    report_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_reports.id", ondelete="CASCADE"), nullable=False
    )
    report_item_id: Mapped[int] = mapped_column(
        ForeignKey("report_items.id", ondelete="CASCADE"), nullable=False
    )
    match_method: Mapped[str] = mapped_column(String(32), nullable=False, default="model_key")
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
