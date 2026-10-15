from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Index, Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class WeeklyReport(Base, TimestampMixin):
    """周报（需求第 5.4 节）。

    幂等控制：unique (source, external_id)，external_id 为企业微信 journal_uuid。
    溯源：raw_record_id 必填。

    content        = 清洗后的正文（用于展示与 AI 输入）
    content_raw    = 未经任何处理的接口原文
    二者分离，保证「原始周报永远不可被 AI 结果覆盖」（需求第 24 节第 10 条）。

    start_date / end_date 为 **推导值**：企业微信汇报接口不返回汇报周期，
    仅返回 report_time，周期由 report_time 按 ISO 周/自然日/自然月推导（见 time_utils.period_range）。
    """

    __tablename__ = "weekly_reports"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_weekly_reports_source_external_id"),
        Index("ix_weekly_reports_employee_period", "employee_id", "start_date", "end_date"),
        Index("ix_weekly_reports_template_id", "template_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False, default="weekly")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 仅用于查询筛选的本地派生值，不取代原文，也不按员工国籍推断。
    content_language: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="wecom")
    raw_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_records.id", ondelete="SET NULL"), nullable=True
    )
    # 汇报表单标识，用于区分同一企业下的日报 / 周报 / 月报模板
    template_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # AI 体检结论的冗余缓存。权威结果永远在 ai_analyses 表（含模型/prompt 版本/理由），
    # 这里只为「列表按 AI 标记筛选排序」服务，避免每次都去 join ai_analyses。
    ai_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ai_duplicate_flag: Mapped[bool | None] = mapped_column(nullable=True)
    ai_duplicate_score: Mapped[int | None] = mapped_column(nullable=True)
    ai_quality_flag: Mapped[bool | None] = mapped_column(nullable=True)
    # 最近一次成功 AI 分析对应的正文指纹。正文变化后会被清空，避免读到旧结论。
    analysis_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    employee: Mapped["Employee | None"] = relationship(back_populates="weekly_reports")
    raw_record: Mapped["RawRecord | None"] = relationship()
