from __future__ import annotations

from datetime import date

from sqlalchemy import Index, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Blocker(Base, TimestampMixin):
    """由任务中的问题、依赖、风险聚合出的当前/历史卡点。"""

    __tablename__ = "blockers"

    __table_args__ = (
        Index("ix_blockers_status_risk_last", "status", "risk_level", "last_seen_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False, default="低")
    first_seen_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_seen_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    duration_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
