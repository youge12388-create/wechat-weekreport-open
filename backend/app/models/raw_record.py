from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time_utils import utcnow
from app.db.base import Base, JSONVariant
from app.models.enums import ProcessStatus


class RawRecord(Base):
    """外部系统原始数据（需求第 5.3 节）。

    这是所有数据源的统一入口。V1：
        source = wecom, source_type = weekly_report
    后续 approval / document / oa_task / message 直接复用本表。

    规则：
      - raw_payload 一旦写入即为不可变事实，AI 结果永远不回写本表；
      - content_hash 用于在重复同步时判断 payload 是否发生变化，
        未变化则跳过处理，变化则回退 process_status 触发重新处理。
    """

    __tablename__ = "raw_records"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_type",
            "external_id",
            name="uq_raw_records_source_type_external_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    process_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProcessStatus.PENDING
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
