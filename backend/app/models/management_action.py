from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONVariant, TimestampMixin
from app.models.enums import ActionNotifyStatus, ManagementActionPriority, ManagementActionStatus


class ManagementAction(Base, TimestampMixin):
    """老板确认后的人工跟进事项。

    这是 AI 候选（Blocker / Insight）的独立投影，不能把负责人、截止日等
    人工信息直接放在会被 AI 重算的业务表上。source_snapshot 保存创建时的
    证据快照，保证来源重跑或消失后仍能审计当时为什么建立跟进。
    """

    __tablename__ = "management_actions"
    __table_args__ = (
        UniqueConstraint("source_type", "source_key", name="uq_management_actions_source"),
        Index("ix_management_actions_status_due", "status", "due_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(500), nullable=False)
    source_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    source_blocker_id: Mapped[int | None] = mapped_column(
        ForeignKey("blockers.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ManagementActionPriority.MEDIUM
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ManagementActionStatus.OPEN
    )
    owner_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    owner_department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_snapshot: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    events: Mapped[list["ManagementActionEvent"]] = relationship(
        back_populates="action",
        cascade="all, delete-orphan",
        order_by="ManagementActionEvent.created_at",
    )
    messages: Mapped[list["ManagementActionMessage"]] = relationship(
        back_populates="action",
        cascade="all, delete-orphan",
        order_by="ManagementActionMessage.created_at",
    )


class ManagementActionEvent(Base, TimestampMixin):
    """管理事项不可变（应用层约定）的变更时间线。"""

    __tablename__ = "management_action_events"
    __table_args__ = (
        Index("ix_management_action_events_action_created", "action_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    action_id: Mapped[int] = mapped_column(
        ForeignKey("management_actions.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="admin")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_fields: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)

    action: Mapped[ManagementAction] = relationship(back_populates="events")


class ManagementActionMessage(Base, TimestampMixin):
    """跟进事项上的双向沟通消息。

    与 `ManagementActionEvent` 的分工：事件记录**状态变更**（谁把状态从 A 改到 B），
    消息记录**对话内容**。两者都是只增不改的审计流，不互相替代 —— 把聊天塞进
    events 会让时间线里混着系统事件与人的发言，两边都读不清。

    已读状态用两个可空时刻列表达（`owner_read_at` / `admin_read_at`），
    而不是逐人一张读状态表：跟进事项是几十条量级、参与方恒为「管理侧 + 一名负责人」，
    两个列就够；代价是**多个管理员共享一个已读位**（谁会先读掉就是谁），
    这在当前「一位老板 + 少量部门管理员」的部署下可接受，真要多管理员独立未读
    需另拆读状态表。

    投递结果与消息内容同表：企微只负责通知、不承载会话，所以「这条消息有没有
    送达」是这条消息自身的属性，拆表只会让「为什么他没收到」多一次 join。
    """

    __tablename__ = "management_action_messages"
    __table_args__ = (
        Index(
            "ix_management_action_messages_action_created",
            "action_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    action_id: Mapped[int] = mapped_column(
        ForeignKey("management_actions.id", ondelete="CASCADE"), nullable=False
    )
    author_role: Mapped[str] = mapped_column(String(16), nullable=False)
    # 访问密码登录的管理员没有员工身份（employee_id 为 None），此时只留姓名快照。
    author_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    # 发送时的人名快照：员工离职、改名、被合并后，对话里仍要显示当时的发言人。
    author_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    owner_read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    admin_read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notify_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ActionNotifyStatus.NOT_ATTEMPTED
    )
    # 这次投递面向哪一侧。冷却按 (事项, 受众) 分别计时，所以必须记下来
    # ——否则「刚给员工发过」会拦住「现在要给管理员发」。
    notify_audience: Mapped[str | None] = mapped_column(String(16), nullable=True)
    notify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 每次成功投递拿到的企微 msgid。撤回必须靠它，而**只有发送当次响应里才有**
    # —— 事后再查是查不到这个 id 的，所以必须落库。
    # 存成列表而不是单值：同一条消息可能被「重新推送」多次，聊天窗口里会有
    # 多张卡片，撤回时要把它们都收回来，否则用户以为撤干净了、对方还留着旧的。
    notify_msgids: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    # 撤回状态。企微侧撤回与站内标记是同一动作的两半：先调企微，成功后才标记，
    # 否则会出现「站内显示已撤回、对方聊天窗口里还躺着那张卡片」。
    recalled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recalled_by_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 撤回失败的原因（超过 24 小时、企微报错、没有 msgid）——必须能显示出来，
    # 否则用户点了撤回、卡片还在，却不知道为什么。
    recall_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notify_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    action: Mapped[ManagementAction] = relationship(back_populates="messages")
