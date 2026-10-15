"""SQLAlchemy 声明式基类与跨库兼容类型。

PG-ready 约定（当前跑 SQLite，后续切 PostgreSQL 不改模型）：
  - JSON：PostgreSQL 下自动使用 JSONB，其他库退化为 JSON。
    查询层 **禁止** 使用 JSONB 专有操作符（@> / ? 等），否则 SQLite 无法执行。
  - 主键：PostgreSQL 用 BIGINT；SQLite 的 AUTOINCREMENT 只认 INTEGER，故做 variant。
  - datetime：全部存 UTC naive，见 core.time_utils.utcnow。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, JSON, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.time_utils import utcnow

# 索引 / 约束命名约定，保证迁移可重复生成且 PG 下可读
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# PostgreSQL -> JSONB；SQLite / 其他 -> JSON
JSONVariant = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
