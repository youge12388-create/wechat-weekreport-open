from __future__ import annotations

from sqlalchemy import Index, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONVariant, TimestampMixin


class ReportItem(Base, TimestampMixin):
    """周报统一分析结果中可查询的一条结构化事项。

    ``AIAnalysis.result_json`` 是可审计的权威快照；本表是其面向 SQL 查询的
    投影。重跑同一篇周报时由服务层整体替换，绝不修改原始周报正文。
    """

    __tablename__ = "report_items"
    __table_args__ = (
        UniqueConstraint("report_id", "item_type", "item_order", name="uq_report_items_report_type_order"),
        Index("ix_report_items_report_type", "report_id", "item_type"),
        Index("ix_report_items_employee_type", "employee_id", "item_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_reports.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    item_order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    related_project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    task_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mentioned_people: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
