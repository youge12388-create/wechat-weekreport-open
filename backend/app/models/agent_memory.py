from __future__ import annotations

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AgentMemory(Base, TimestampMixin):
    """管理员确认的 Agent 长期记忆。

    这里只保存稳定规则、术语和偏好；周报事实仍从业务表实时查询，避免形成第二份真源。
    """

    __tablename__ = "agent_memories"
    __table_args__ = (Index("ix_agent_memories_enabled_category", "enabled", "category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
