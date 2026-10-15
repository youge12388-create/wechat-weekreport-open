from __future__ import annotations

from datetime import date

from sqlalchemy import Index, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Task(Base, TimestampMixin):
    """跨周聚合的工作事项；仅保存由周报事项可追溯出来的状态。"""

    __tablename__ = "tasks"

    __table_args__ = (
        Index("ix_tasks_status_last_seen", "status", "last_seen_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    related_project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    first_seen_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_seen_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    duration_weeks: Mapped[int] = mapped_column(nullable=False, default=1)
