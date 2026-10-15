from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ReportTranslation(Base, TimestampMixin):
    """单篇周报的译文缓存；原文变动以 source_hash 判定失效。"""

    __tablename__ = "report_translations"
    __table_args__ = (
        UniqueConstraint("report_id", "target_language", name="uq_report_translation_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_reports.id", ondelete="CASCADE"), nullable=False
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_language: Mapped[str] = mapped_column(String(16), nullable=False, default="zh")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="baidu_translate")
    translated_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
