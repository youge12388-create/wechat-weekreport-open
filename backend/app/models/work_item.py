from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class WorkItem(Base, TimestampMixin):
    """工作事项（需求第 5.5 节）。

    这是 **跨数据源复用** 的核心公共模型，不是周报专用表：
        V1:         source_type = weekly_report
        后续扩展:    oa_task / message / approval / application ...

    source_id 指向来源实体（周报场景下为 weekly_reports.id）。
    不做 unique 约束：AI 重跑提取时由服务层先删除旧结果再写入，
    避免唯一约束导致重跑失败。
    """

    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[int | None] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    project_id: Mapped[int | None] = mapped_column(nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # AI 置信度 0~1；人工创建的事项为 None
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
