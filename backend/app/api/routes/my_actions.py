"""负责人侧（只读成员）的「发给我的事项」与沟通。

这是**全站唯一允许 member 角色写入的命名空间**（见 app/main.py 的
`_MEMBER_WRITABLE_PREFIXES`）。因此本模块有三条硬纪律：

1. **每个端点都必须做所有者校验**：事项的 `owner_employee_id` 必须等于当前会话
   员工，否则一律 404。中间件只保证「这个前缀可写」，不保证「写的是自己的事」——
   所有权是这里自己守的。
2. **视图一律用 member**：`note`（内部备注）、来源快照、状态变更轨迹都不外泄。
3. **只允许发消息与标记已读**，不允许改状态、改负责人、删事项。往本模块新增
   端点等于扩大只读角色的权限，必须重新评审中间件那处例外。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentActor, CurrentSession, get_db
from app.models.employee import Employee
from app.models.enums import ManagementActionAuthor, ManagementActionStatus
from app.models.management_action import ManagementAction
from app.services import management_action_message_service as message_service
from app.services import management_action_view as view_service

router = APIRouter(prefix="/api/my-actions", tags=["my-actions"])

_OPEN_STATUSES = ManagementActionStatus.all() - {
    ManagementActionStatus.RESOLVED,
    ManagementActionStatus.CANCELLED,
}


class ReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=message_service.BODY_MAX_CHARS)


class ReadRequest(BaseModel):
    """标记已读无需参数；保留空模型便于未来扩展（如「只标记到某条为止」）。"""


def _require_employee(
    session: Session, actor: CurrentSession
) -> int:
    """取出当前会话的员工 id；没有员工身份就没有「我的事项」。

    访问密码登录的管理员本来就有管理侧页面，这里明确拒绝而不是返回空列表 ——
    否则他会以为「我没有被指派的事项」，而不是「这个入口不是给我的」。
    """
    if actor.employee_id is None:
        raise HTTPException(
            status_code=400,
            detail="当前登录方式没有员工身份，无法查看「发给我的事项」。",
        )
    if session.get(Employee, actor.employee_id) is None:
        raise HTTPException(status_code=404, detail="员工记录不存在")
    return actor.employee_id


def _load_owned(session: Session, action_id: int, employee_id: int) -> ManagementAction:
    action = view_service.load_visible_action(
        session, action_id, is_admin=False, employee_id=employee_id
    )
    if action is None:
        raise HTTPException(status_code=404, detail="事项不存在或未指派给你")
    return action


@router.get("")
def list_my_actions(
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
    status: str | None = Query(default=None, description="OPEN / IN_PROGRESS / WAITING / RESOLVED / CANCELLED"),
    include_closed: bool = Query(default=False, description="包含已解决与已取消"),
    # 预防性上限：单人事项量正常很小，但接口没有边界的话，历史数据堆积后
    # 一次全量序列化没有退路。未读总数单独统计、不受截断影响，红点仍准确。
    limit: int = Query(default=200, ge=1, le=1000, description="最多返回条数"),
) -> dict:
    """指派给我的事项。

    默认只列未结束的：已解决的事项堆在顶部会让「现在还有什么要我处理」这条
    唯一有价值的信息被淹没。需要回看历史时传 `include_closed=true`。
    """
    employee_id = _require_employee(session, actor)
    query = select(ManagementAction).where(
        ManagementAction.owner_employee_id == employee_id
    )
    if status:
        if status not in ManagementActionStatus.all():
            raise HTTPException(status_code=400, detail="不支持的状态")
        query = query.where(ManagementAction.status == status)
    elif not include_closed:
        query = query.where(ManagementAction.status.in_(_OPEN_STATUSES))
    actions = list(
        session.scalars(
            query.order_by(ManagementAction.due_date.asc(), ManagementAction.id.desc())
            .limit(limit)
        )
    )
    unread = message_service.unread_counts(
        session, [action.id for action in actions], reader="owner"
    )
    items = view_service.serialize_actions(
        session, actions, view="member", unread=unread
    )
    # 未读总数一并统计「已结束但还没读」的事项：列表默认不显示它们，
    # 但漏掉会让红点比列表里能看到的数量少，显得对不上。
    return {
        "items": items,
        "total": len(items),
        "unread_total": message_service.total_unread(
            session, reader="owner", employee_id=employee_id
        ),
    }


@router.get("/{action_id}")
def get_my_action(
    action_id: int,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    employee_id = _require_employee(session, actor)
    action = _load_owned(session, action_id, employee_id)
    unread = message_service.unread_counts(session, [action.id], reader="owner")
    return view_service.serialize_action(
        session, action, view="member", unread=unread.get(action.id, 0)
    )


@router.get("/{action_id}/messages")
def list_my_messages(
    action_id: int,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    """只读读取会话（轮询用它，已读必须走显式 POST）。"""
    employee_id = _require_employee(session, actor)
    action = _load_owned(session, action_id, employee_id)
    unread = message_service.unread_counts(session, [action.id], reader="owner")
    return {
        "items": message_service.thread(session, action, view="member"),
        "unread_count": unread.get(action.id, 0),
        "owner_name": actor.name,
    }


@router.post("/{action_id}/messages")
def reply_to_action(
    action_id: int,
    payload: ReplyRequest,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    """负责人回话。

    消息始终**推送管理侧**（本会话参与过的企微管理员 + 设置页配置的接收人），
    而不是推给「所有管理员」—— 谁参与过这件事，谁才收它的回复。
    """
    employee_id = _require_employee(session, actor)
    action = _load_owned(session, action_id, employee_id)
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    try:
        message, delivery = message_service.post_message(
            session,
            action,
            author_role=ManagementActionAuthor.OWNER,
            body=body,
            author_employee_id=employee_id,
            author_name=actor.name,
            notify_peer=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    session.refresh(message)
    return {
        "message": message_service.message_view(message, "member"),
        # 投递结果对负责人不展示细节，只说「是否已通知到管理侧」，
        # 避免把「某个管理员的企微收不到」这类内部问题暴露给他。
        "notified": bool(delivery and delivery.status == "sent"),
        "unread_count": 0,
    }


@router.post("/{action_id}/messages/read")
def mark_my_messages_read(
    action_id: int,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    employee_id = _require_employee(session, actor)
    action = _load_owned(session, action_id, employee_id)
    updated = message_service.mark_read(session, action, reader="owner")
    session.commit()
    return {"updated": updated, "unread_count": 0}
