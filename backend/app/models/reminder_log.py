from __future__ import annotations

from datetime import date

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


# detail 的轻量结构化前缀：不新增数据库列，也能区分「整批请求未受理」与
# 「请求成功但某个收件人不可投递」。消息中心不能再靠错误文案里的关键词猜类型。
BATCH_FAILURE_DETAIL_PREFIX = "batch_failure:"
RECIPIENT_UNDELIVERED_DETAIL_PREFIX = "recipient_undelivered:"
LEGACY_RECIPIENT_UNDELIVERED_PREFIX = "企微未投递（"


class ReminderLog(Base, TimestampMixin):
    """催交提醒留痕（P2-7）。

    每次真发（dry_run=false）成功或失败后落一条；用于审计与冷却去重。
    同一员工 + 同一周期 + 冷却期内的重复发送会被拦截。
    """

    __tablename__ = "reminder_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="wecom_text_card")
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # sent / failed
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # created_at / updated_at 由 TimestampMixin 提供

    def __repr__(self) -> str:
        return f"<ReminderLog id={self.id} emp={self.employee_id} status={self.status}>"
