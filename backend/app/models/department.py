from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import DepartmentStatus, LoginRole


class Department(Base, TimestampMixin):
    """部门。

    external_id 为企业微信部门 ID；parent_external_id 保留外部父级 ID，
    使同步时无需依赖本表插入顺序即可建立层级。
    """

    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_departments_source_external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    parent_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="wecom")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DepartmentStatus.ACTIVE
    )
    # 登录授权开关：**整棵子树**（本部门及全部下级部门）生效。
    # 三态，缺一不可：
    #   None  = 未设置，向上继承最近的显式设置（默认，不占语义）
    #   True  = 允许登录（授权只占这一位，不产生逐人授权行）
    #   False = 显式禁止，本子树内不再向上继承
    # 只有布尔位无法表达「全公司开放、某部门除外」—— 公司一开，
    # 下面所有部门都被自动放行，没有地方能拦住。
    login_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 该部门（及子树）成员登录后的角色：member 只读 / admin 可读写。
    # 放在部门上是刻意为之：授权只随部门数量增长，不随人数膨胀。
    login_role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LoginRole.MEMBER, server_default=LoginRole.MEMBER
    )
    # 最近一次在数据源中出现的时刻。
    # 全量同步时不再出现的部门不会立即删除，只由同步服务判定是否置为 inactive，
    # 避免分页失败等偶发问题误删组织架构。
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    parent: Mapped[Department | None] = relationship(remote_side=[id])
    employees: Mapped[list["Employee"]] = relationship(back_populates="department")
