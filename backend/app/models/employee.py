from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Boolean, DateTime, ForeignKey, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import EmployeeStatus, LoginRole


class Employee(Base, TimestampMixin):
    """员工。

    需求第 5.2 节：不允许仅通过姓名关联员工。
    所有外部关联一律走 (source, external_id)。
    """

    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_employees_source_external_id"),
        Index("ix_employees_department_id", "department_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EmployeeStatus.ACTIVE
    )
    # 个人级免提交：与免提交部门一样，只影响应提交统计和催交，不删除历史周报。
    report_exempt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # ---- 登录授权 ----
    # 个人级允许：未明确禁止时，可作为跨部门追加授权；明确禁止由 login_denied 覆盖部门授权。
    # 刻意不做逐人一份授权行：整个部门授权只占部门表一个布尔位，不随人数膨胀。
    login_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # 个人明确禁止登录。False 的 login_enabled 仍表示“未单独授权，沿用部门设置”；
    # 这个字段让设置页取消勾选后可以真正覆盖部门授权。
    login_denied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # 登录后的角色；仅在本人可登录时有意义。
    login_role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LoginRole.MEMBER, server_default=LoginRole.MEMBER
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="wecom")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    department: Mapped["Department | None"] = relationship(back_populates="employees")
    weekly_reports: Mapped[list["WeeklyReport"]] = relationship(
        back_populates="employee"
    )
