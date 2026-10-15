from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONVariant, TimestampMixin


class AIEmbedding(Base, TimestampMixin):
    """周报正文的语义向量（OpenAI 兼容 /embeddings 端点产出）。

    设计取舍：

    - 一篇周报只保留**最新一条**向量（report_id 唯一）：向量是正文的派生数据，
      正文变了就按 source_hash 重算覆盖，历史向量没有查询价值。
    - 向量以 JSON 数组存储（JSONVariant，PG 下 JSONB），**禁用 JSONB 专有操作符**
      是项目硬约束；相似度在 Python 侧计算——周报量级（千篇以内）全量加载
      毫秒级完成，不需要 pgvector / 向量数据库。
    - ``source_hash`` 是标题+正文的哈希：正文未变时回填直接跳过，省 embedding 调用费。
    """

    __tablename__ = "ai_embeddings"
    __table_args__ = (Index("ix_ai_embeddings_model_source", "model", "source_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("weekly_reports.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    # JSON 数组的 float 列表（JSONVariant 在 PG 下是 JSONB，这里只用普通读写）。
    vector: Mapped[dict | list] = mapped_column(JSONVariant, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
