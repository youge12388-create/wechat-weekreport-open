from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import AnalysisQueueStatus

# 全公司范围在库里的表示。**不能用 NULL**：SQLite 与 PostgreSQL 都把 NULL 视为
# 互不相等，唯一约束会因此完全失效（同一周期能插进任意多条全公司任务）。
COMPANY_SCOPE_ID = 0


class OperatingSummaryQueue(Base, TimestampMixin):
    """经营看板结构化 AI 总结的后台任务位。

    与 ``PeriodSummaryQueue`` **刻意分开建表**，不共用也不加兼容字段：
    同一周期两者都要有结论，但一个给管理简报的 Markdown 周期总览、另一个给
    经营看板的结构化 JSON。共用一张表意味着任一侧重算都会把另一侧的结论标记成
    过期甚至覆盖，而唯一约束只能按周期一条 —— 那是两个功能的耦合事故。

    缓存键 = ``summary_type + start_date + end_date + department_id``，
    覆盖方案要求的三种作用域：

    * ``weekly_company``  全公司上周经营总结
    * ``monthly_company`` 全公司跨月经营总结（跨月切分在服务层按月展开后合并）
    * ``department``      指定部门的经营总结

    ``payload_hash`` 是本次输入事实（工作事项 + 卡点 + 原文依据）的指纹：
    事实新增、状态变化或依据正文变化都会让成功结果变为过期，
    绝不把旧结论伪装成当前数据（与 ``BlockerSummaryQueue`` 同一纪律）。
    """

    __tablename__ = "operating_summary_queue"
    __table_args__ = (
        UniqueConstraint(
            "summary_type",
            "start_date",
            "end_date",
            "department_id",
            name="uq_operating_summary_scope",
        ),
        Index(
            "ix_operating_summary_queue_status_next_attempt",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    summary_type: Mapped[str] = mapped_column(String(32), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 0 = 全公司（见 COMPANY_SCOPE_ID）；其余为 departments.id
    department_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=COMPANY_SCOPE_ID
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 本期参与总结的周报篇数，前端据此展示「基于 N 篇周报」。
    report_count: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AnalysisQueueStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["COMPANY_SCOPE_ID", "OperatingSummaryQueue"]
