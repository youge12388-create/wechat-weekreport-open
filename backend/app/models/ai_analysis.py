from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time_utils import utcnow
from app.db.base import Base, JSONVariant
from app.models.enums import AIAnalysisStatus


class AIAnalysis(Base):
    """AI 分析结果（需求第 5.6 节）。

    目的：避免每次打开页面重新调用模型。
    同时承担需求第 19 节「AI 日志」的职责（status / error_message）。
    """

    __tablename__ = "ai_analyses"
    __table_args__ = (
        Index("ix_ai_analyses_conversation", "analysis_type", "conversation_id", "id"),
        Index("ix_ai_analyses_owner", "analysis_type", "owner_employee_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int] = mapped_column(nullable=False)
    analysis_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Agent / RAG 会话 ID 提升为普通列，避免历史列表与上下文回放扫描 JSON 全表。
    conversation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 聊天归属人（企微会话的员工 id）。严格隔离：每个人（含超管）只能看到
    # 自己的对话。密码超管没有员工身份，其会话与**迁移前的历史聊天**该列
    # 为 NULL —— 语义是「归超管所有」，与迁移前只有超管能登录看这些记录
    # 的现状对齐。非聊天类分析（report_analysis 等）不使用该列。
    owner_employee_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    # 分析入参快照（分析依赖的原文全文在 weekly_reports / raw_records，可重新读取）。
    # 这里只存复现所需的最小元信息，避免把原文再复制一份。
    input_data: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    # 单次模型调用耗时（毫秒），供 AI 成本与排障审计。
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 用量留痕：模型按 token 计费，不落库就等于这笔账永远查不到
    prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AIAnalysisStatus.SUCCESS
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 聊天历史软删除：保留 AI 用量与审计记录，只从用户历史列表隐藏。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
