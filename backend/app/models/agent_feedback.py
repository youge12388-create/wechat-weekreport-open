"""Agent / RAG 回答的本人反馈；不重复保存问题或答案正文。"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AgentFeedback(Base, TimestampMixin):
    __tablename__ = "agent_feedback"
    __table_args__ = (
        UniqueConstraint("analysis_id", "reviewer_key", name="uq_agent_feedback_analysis_reviewer"),
        Index("ix_agent_feedback_reviewer_created", "reviewer_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("ai_analyses.id", ondelete="CASCADE"), nullable=False
    )
    # 企业微信用户使用员工 ID；密码管理员使用当前部署的 admin 身份。
    reviewer_key: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
