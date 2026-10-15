"""管理事项的对外视图与可见性判定。

**为什么单独一个模块**：管理侧（`/api/management-actions`）与负责人侧
（`/api/my-actions`）看的是同一张表、同一批字段，只有「哪些字段该给他看」
不同。如果两侧各写一份序列化，迟早出现「管理侧加了字段、负责人侧忘了剥」
这类偏差 —— 而要剥的恰恰是内部备注这种不该外泄的内容。所以两个路由都从这里取视图。

字段剥离规则见 `serialize_action` 的 `view="member"` 分支。
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.management_action import ManagementAction
from app.models.weekly_report import WeeklyReport
from app.services import management_action_message_service as message_service

View = Literal["admin", "member"]

# 负责人视角**不得出现**的字段。列在这里而不是逐个 pop，是为了让「新增字段
# 默认怎么处理」这个问题有一个明确答案：不在白名单里的敏感字段必须在此显式声明。
_MEMBER_HIDDEN_FIELDS = (
    "note",  # 老板写的内部判断，员工看到「这人响应太慢」是事故
    "source_snapshot",  # 含其他同事的周报片段与部门名
    "source_stale",
    "source_href",  # 指向管理侧页面
    "source_key",
    "source_task_id",
    "source_blocker_id",
    "source_id",
    "owner_department_id",
    "owner_department_name",
    "closed_at",
    "closed_by",
    "created_by",
    "updated_by",
)


def source_stale(session: Session, action: ManagementAction) -> bool:
    """来源是否已失效（卡点被删或周报不在了）。

    只对管理侧有意义：它提示「这条跟进引用的原始证据查不到了」，
    负责人拿这个信息也没法处理。
    """
    if action.source_type == "blocker":
        return (
            action.source_blocker_id is None
            or session.get(Blocker, action.source_blocker_id) is None
        )
    snapshot = action.source_snapshot or {}
    report_ids = [int(item) for item in snapshot.get("source_report_ids", []) if item]
    if not report_ids:
        return True
    return (
        session.scalar(
            select(WeeklyReport.id).where(WeeklyReport.id.in_(report_ids)).limit(1)
        )
        is None
    )


def load_visible_action(
    session: Session, action_id: int, *, is_admin: bool, employee_id: int | None
) -> ManagementAction | None:
    """按可见性取出事项；不可见返回 None，由调用方转成 404。

    **不可见与不存在返回同一个结果**：404 而不是 403。403 等于确认
    「这个 id 确实有一条别人的跟进事项」，会把事项数量与 id 空间泄漏给
    无关成员；对前端来说两种情况也都只能显示「找不到」。
    """
    action = session.get(ManagementAction, action_id)
    if action is None:
        return None
    if not is_admin and action.owner_employee_id != employee_id:
        return None
    return action


def serialize_actions(
    session: Session,
    actions: list[ManagementAction],
    *,
    include_events: bool = False,
    view: View = "admin",
    unread: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """批量序列化 —— 列表页走这个入口。

    单独做一个批量函数而不是循环调 `serialize_action`：后者每事项要查两次
    消息表（条数 + 最后一条），列表 N 条就是 2N 次查询。这里一次取回，
    让查询数与事项数无关（第 22 条的口径）。
    """
    if not actions:
        return []
    action_ids = [action.id for action in actions]
    counts = message_service.message_counts(session, action_ids)
    lasts = message_service.last_messages(session, action_ids)
    unread_map = unread or {}
    return [
        _serialize_with(
            session,
            action,
            include_events=include_events and view == "admin",
            view=view,
            unread=unread_map.get(action.id, 0),
            message_count=counts.get(action.id, 0),
            last=lasts.get(action.id),
        )
        for action in actions
    ]


def serialize_action(
    session: Session,
    action: ManagementAction,
    *,
    include_events: bool = False,
    view: View = "admin",
    unread: int | None = None,
) -> dict[str, Any]:
    """单条序列化（详情页用）。"""
    counts = message_service.message_counts(session, [action.id])
    return _serialize_with(
        session,
        action,
        include_events=include_events and view == "admin",
        view=view,
        unread=unread or 0,
        message_count=counts.get(action.id, 0),
        last=message_service.last_message(session, action.id),
    )


def _serialize_with(
    session: Session,
    action: ManagementAction,
    *,
    include_events: bool,
    view: View,
    unread: int,
    message_count: int,
    last: dict[str, Any] | None,
) -> dict[str, Any]:
    """真正组装返回结构；消息相关数据由调用方传入，避免这里再查库。"""
    owner = session.get(Employee, action.owner_employee_id) if action.owner_employee_id else None
    owner_department = (
        session.get(Department, action.owner_department_id)
        if action.owner_department_id
        else (owner.department if owner else None)
    )
    snapshot = action.source_snapshot or {}
    item: dict[str, Any] = {
        "id": action.id,
        "source_type": action.source_type,
        "source_key": action.source_key,
        "source_id": action.source_blocker_id,
        "source_task_id": action.source_task_id,
        "source_blocker_id": action.source_blocker_id,
        "title": action.title,
        "description": action.description,
        "priority": action.priority,
        "status": action.status,
        "owner_employee_id": action.owner_employee_id,
        "owner_name": owner.name if owner else None,
        "owner_department_id": action.owner_department_id
        or (owner.department_id if owner else None),
        "owner_department_name": owner_department.name if owner_department else None,
        "due_date": action.due_date.isoformat() if action.due_date else None,
        "next_step": action.next_step,
        "note": action.note,
        "source_snapshot": snapshot,
        "source_stale": source_stale(session, action),
        "source_href": snapshot.get("source_href")
        or (
            f"/blockers/{action.source_blocker_id}"
            if action.source_blocker_id
            else "/insights"
        ),
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "updated_at": action.updated_at.isoformat() if action.updated_at else None,
        "closed_at": action.closed_at.isoformat() if action.closed_at else None,
        "message_count": message_count,
        "unread_count": unread,
        "last_message": last,
    }
    if include_events and view == "admin":
        item["events"] = [
            {
                "id": event.id,
                "actor": event.actor,
                "event_type": event.event_type,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "note": event.note,
                "changed_fields": event.changed_fields,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in action.events
        ]
    else:
        item["events"] = []

    if view == "member":
        for field in _MEMBER_HIDDEN_FIELDS:
            item.pop(field, None)
    return item
