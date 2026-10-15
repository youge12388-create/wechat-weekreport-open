from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SystemSetting(Base, TimestampMixin):
    """管理员可运行时更新的配置。

    value_encrypted 只保存 Fernet 密文；接口和日志都不读取、也不输出明文。
    """

    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
