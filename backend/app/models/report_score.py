from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ReportScore(Base, TimestampMixin):
    """周报质量分的查询投影，明细维度用于趋势统计而非员工绩效判断。"""

    __tablename__ = "report_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_reports.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    total_score: Mapped[int] = mapped_column(Integer, nullable=False)
    completeness: Mapped[int | None] = mapped_column(Integer, nullable=True)
    specificity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    results: Mapped[int | None] = mapped_column(Integer, nullable=True)
    advancement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    planning: Mapped[int | None] = mapped_column(Integer, nullable=True)
    problem_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duplicate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
