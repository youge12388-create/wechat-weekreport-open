"""人工管理事项：把 AI/结构化候选转成可推进、可审计的跟进。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import AdminOnly, CurrentActor, CurrentSession, get_db
from app.core.time_utils import utcnow
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import ManagementActionAuthor, ManagementActionPriority, ManagementActionStatus
from app.models.management_action import (
    ManagementAction,
    ManagementActionEvent,
    ManagementActionMessage,
)
from app.models.weekly_report import WeeklyReport
from app.services import management_action_message_service as message_service
from app.services import management_action_view as view_service
from app.services.insight_service import management_action_items

router = APIRouter(prefix="/api/management-actions", tags=["management-actions"], dependencies=[AdminOnly])

SourceType = Literal["blocker", "insight"]
_TERMINAL_STATUSES = {
    ManagementActionStatus.RESOLVED,
    ManagementActionStatus.CANCELLED,
}
_STATUS_TRANSITIONS: dict[str, set[str]] = {
    ManagementActionStatus.OPEN: {
        ManagementActionStatus.OPEN,
        ManagementActionStatus.IN_PROGRESS,
        ManagementActionStatus.WAITING,
        ManagementActionStatus.RESOLVED,
        ManagementActionStatus.CANCELLED,
    },
    ManagementActionStatus.IN_PROGRESS: {
        ManagementActionStatus.IN_PROGRESS,
        ManagementActionStatus.OPEN,
        ManagementActionStatus.WAITING,
        ManagementActionStatus.RESOLVED,
        ManagementActionStatus.CANCELLED,
    },
    ManagementActionStatus.WAITING: {
        ManagementActionStatus.WAITING,
        ManagementActionStatus.OPEN,
        ManagementActionStatus.IN_PROGRESS,
        ManagementActionStatus.RESOLVED,
        ManagementActionStatus.CANCELLED,
    },
    # 解决/取消后允许重新打开，但必须留下状态事件。
    ManagementActionStatus.RESOLVED: {
        ManagementActionStatus.RESOLVED,
        ManagementActionStatus.OPEN,
        ManagementActionStatus.IN_PROGRESS,
        ManagementActionStatus.WAITING,
    },
    ManagementActionStatus.CANCELLED: {
        ManagementActionStatus.CANCELLED,
        ManagementActionStatus.OPEN,
        ManagementActionStatus.IN_PROGRESS,
        ManagementActionStatus.WAITING,
    },
}


class CreateManagementActionRequest(BaseModel):
    source_type: SourceType
    source_id: int | None = Field(default=None, ge=1)
    source_key: str | None = Field(default=None, min_length=1, max_length=500)
    start: date
    end: date
    priority: str = ManagementActionPriority.MEDIUM
    owner_employee_id: int | None = Field(default=None, ge=1)
    owner_department_id: int | None = Field(default=None, ge=1)
    due_date: date | None = None
    next_step: str | None = Field(default=None, max_length=5000)
    note: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def validate_request(self) -> "CreateManagementActionRequest":
        if self.start > self.end:
            raise ValueError("start 不能晚于 end")
        if self.priority not in ManagementActionPriority.all():
            raise ValueError("priority 只支持 high / medium / low")
        if self.source_type == "blocker" and self.source_id is None:
            raise ValueError("blocker 来源必须提供 source_id")
        if self.source_type == "insight" and not self.source_key:
            raise ValueError("insight 来源必须提供 source_key")
        return self


class UpdateManagementActionRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    priority: str | None = None
    status: str | None = None
    owner_employee_id: int | None = Field(default=None, ge=1)
    owner_department_id: int | None = Field(default=None, ge=1)
    due_date: date | None = None
    next_step: str | None = Field(default=None, max_length=5000)
    note: str | None = Field(default=None, max_length=5000)


class StatusRequest(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=5000)
    # 默认通知负责人：他被告知要跟进这件事，状态被改却不告诉他，
    # 等于把他排除在外。可以在界面上取消（例如只是修正填错的备注）。
    notify: bool = True


# 状态的中文名。与前端 STATUS_OPTIONS 保持一致；放在后端是因为**通知正文由后端组装**，
# 不能让推送内容依赖前端是否加载了正确的映射表。
_STATUS_LABELS = {
    ManagementActionStatus.OPEN: "待处理",
    ManagementActionStatus.IN_PROGRESS: "处理中",
    ManagementActionStatus.WAITING: "等待外部反馈",
    ManagementActionStatus.RESOLVED: "已解决",
    ManagementActionStatus.CANCELLED: "已取消",
}


def _validate_owner(
    session: Session,
    owner_employee_id: int | None,
    owner_department_id: int | None,
) -> None:
    if owner_employee_id is not None and session.get(Employee, owner_employee_id) is None:
        raise HTTPException(status_code=400, detail="负责人不存在")
    if owner_department_id is not None and session.get(Department, owner_department_id) is None:
        raise HTTPException(status_code=400, detail="责任部门不存在")


def _load_visible_action(
    session: Session, action_id: int, actor: "CurrentSession"
) -> ManagementAction:
    """按可见性取出事项；不可见与不存在都返回 404（理由见 view_service）。"""
    action = view_service.load_visible_action(
        session, action_id, is_admin=actor.is_admin, employee_id=actor.employee_id
    )
    if action is None:
        raise HTTPException(status_code=404, detail="管理事项不存在")
    return action


def _candidate(
    session: Session,
    payload: CreateManagementActionRequest,
) -> dict:
    candidates = management_action_items(
        session, payload.start, payload.end, limit=100
    )
    for item in candidates:
        if payload.source_type != item.get("source_type"):
            continue
        if payload.source_type == "blocker" and item.get("source_id") == payload.source_id:
            return item
        if payload.source_type == "insight" and item.get("source_key") == payload.source_key:
            return item
    raise HTTPException(
        status_code=400,
        detail="该管理事项没有本周期可回看的周报证据，不能建立跟进",
    )


def _snapshot(payload: CreateManagementActionRequest, candidate: dict) -> dict:
    return {
        "captured_at": utcnow().isoformat(),
        "period": {"start": payload.start.isoformat(), "end": payload.end.isoformat()},
        "source_type": candidate.get("source_type"),
        "source_key": candidate.get("source_key"),
        "source_id": candidate.get("source_id"),
        "source_task_id": candidate.get("source_task_id"),
        "title": candidate.get("title"),
        "summary": candidate.get("summary"),
        "risk_level": candidate.get("risk_level"),
        "duration_weeks": candidate.get("duration_weeks"),
        "departments": candidate.get("departments", []),
        "source_count": candidate.get("source_count", 0),
        "source_report_ids": candidate.get("source_report_ids", []),
        "sources": candidate.get("sources", []),
        "source_href": candidate.get("target_href"),
    }


def _event(
    action: ManagementAction,
    *,
    event_type: str,
    from_status: str | None = None,
    to_status: str | None = None,
    note: str | None = None,
    changed_fields: dict | None = None,
) -> ManagementActionEvent:
    return ManagementActionEvent(
        action=action,
        actor="admin",
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        note=note,
        changed_fields=changed_fields,
    )


def _apply_status(
    session: Session,
    action: ManagementAction,
    new_status: str,
    *,
    note: str | None = None,
) -> None:
    if new_status not in ManagementActionStatus.all():
        raise HTTPException(
            status_code=400,
            detail="status 只支持 OPEN / IN_PROGRESS / WAITING / RESOLVED / CANCELLED",
        )
    allowed = _STATUS_TRANSITIONS.get(action.status, set())
    if new_status not in allowed:
        raise HTTPException(status_code=400, detail=f"不允许从 {action.status} 流转到 {new_status}")
    if new_status == action.status:
        if note:
            action.events.append(_event(action, event_type="note", note=note))
        return
    previous = action.status
    action.status = new_status
    if new_status in _TERMINAL_STATUSES:
        action.closed_at = utcnow()
        action.closed_by = "admin"
    else:
        action.closed_at = None
        action.closed_by = None
    action.updated_by = "admin"
    action.events.append(
        _event(
            action,
            event_type="status_changed",
            from_status=previous,
            to_status=new_status,
            note=note,
        )
    )


@router.get("")
def list_management_actions(
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
    status: str | None = Query(default=None),
    owner_employee_id: int | None = Query(default=None, ge=1),
    overdue: bool = Query(default=False),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    if (start is None) != (end is None):
        raise HTTPException(status_code=400, detail="start 与 end 必须同时传入")
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="start 不能晚于 end")
    if status and status not in ManagementActionStatus.all():
        raise HTTPException(status_code=400, detail="不支持的管理事项状态")

    query = select(ManagementAction).order_by(
        ManagementAction.due_date.asc(), ManagementAction.updated_at.desc()
    )
    if status:
        query = query.where(ManagementAction.status == status)
    if owner_employee_id is not None:
        query = query.where(ManagementAction.owner_employee_id == owner_employee_id)
    # 只读成员只看得到指派给自己的事项：管理跟进里可能有别人负责的事，
    # 它们既不该出现在他的列表里，也不该被这个接口当作「可以点进去看看」的入口。
    if not actor.is_admin:
        if actor.employee_id is None:
            return {"items": [], "total": 0, "unread_total": 0}
        query = query.where(ManagementAction.owner_employee_id == actor.employee_id)
    actions = list(session.scalars(query))
    today = date.today()
    current_source_keys: set[str] = set()
    if start and end:
        # 跟进事项可能是在上一周期建立的，但卡点仍持续到本周期；不能只按
        # 创建时快照周期过滤，否则老板会看不到正在推进的事项。
        current_source_keys = {
            str(item["source_key"])
            for item in management_action_items(session, start, end, limit=200)
            if item.get("source_key")
        }
    view = "admin" if actor.is_admin else "member"
    reader = "admin" if actor.is_admin else "owner"
    visible: list[ManagementAction] = []
    for action in actions:
        snapshot = action.source_snapshot or {}
        if start and end:
            # 当前周期仍有同一来源时保留；否则再按创建时快照周期回看历史。
            if action.source_key not in current_source_keys:
                period = snapshot.get("period") or {}
                try:
                    source_start = date.fromisoformat(period["start"])
                    source_end = date.fromisoformat(period["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if source_end < start or source_start > end:
                    continue
        if overdue and (
            action.due_date is None
            or action.due_date >= today
            or action.status in _TERMINAL_STATUSES
        ):
            continue
        visible.append(action)
        if len(visible) >= limit:
            break
    unread = message_service.unread_counts(
        session, [action.id for action in visible], reader=reader
    )
    # 列表也带时间线：编辑器从首页/卡点详情打开时即可直接回看变更依据，
    # 不需要再发一次详情请求。整批一次序列化：消息条数/最后一条都是批量取的，
    # 逐个调用会让这个接口的查询数随事项数线性增长。
    result = view_service.serialize_actions(
        session,
        visible,
        include_events=actor.is_admin,
        view=view,
        unread=unread,
    )
    unread_total = sum(unread.values())
    return {"items": result, "total": len(result), "unread_total": unread_total}


@router.post("")
def create_management_action(
    payload: CreateManagementActionRequest,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    _validate_owner(session, payload.owner_employee_id, payload.owner_department_id)
    candidate = _candidate(session, payload)
    source_key = str(candidate["source_key"])
    existing = session.scalar(
        select(ManagementAction).where(
            ManagementAction.source_type == payload.source_type,
            ManagementAction.source_key == source_key,
        )
    )
    if existing is not None:
        return {**view_service.serialize_action(session, existing, include_events=True), "created": False}

    source_blocker_id = candidate.get("source_id") if payload.source_type == "blocker" else None
    action = ManagementAction(
        source_type=payload.source_type,
        source_key=source_key,
        source_task_id=candidate.get("source_task_id"),
        source_blocker_id=source_blocker_id,
        title=str(candidate.get("title") or "管理事项"),
        description=candidate.get("summary"),
        priority=payload.priority,
        status=ManagementActionStatus.OPEN,
        owner_employee_id=payload.owner_employee_id,
        owner_department_id=payload.owner_department_id,
        due_date=payload.due_date,
        next_step=payload.next_step,
        note=payload.note,
        source_snapshot=_snapshot(payload, candidate),
        created_by="admin",
        updated_by="admin",
    )
    action.events.append(
        _event(
            action,
            event_type="created",
            to_status=action.status,
            changed_fields={
                "source_type": action.source_type,
                "source_key": action.source_key,
                "title": action.title,
            },
        )
    )
    session.add(action)
    session.commit()
    session.refresh(action)
    return {**view_service.serialize_action(session, action, include_events=True), "created": True}


@router.get("/{action_id}")
def get_management_action(
    action_id: int,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    action = _load_visible_action(session, action_id, actor)
    reader = "admin" if actor.is_admin else "owner"
    unread = message_service.unread_counts(session, [action.id], reader=reader)
    return view_service.serialize_action(
        session,
        action,
        include_events=actor.is_admin,
        view=reader,
        unread=unread.get(action.id, 0),
    )


@router.patch("/{action_id}")
def update_management_action(
    action_id: int,
    payload: UpdateManagementActionRequest,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    action = session.get(ManagementAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="管理事项不存在")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return view_service.serialize_action(session, action, include_events=True)
    if "priority" in changes and changes["priority"] not in ManagementActionPriority.all():
        raise HTTPException(status_code=400, detail="priority 只支持 high / medium / low")
    _validate_owner(
        session,
        changes.get("owner_employee_id", action.owner_employee_id),
        changes.get("owner_department_id", action.owner_department_id),
    )
    if "status" in changes:
        _apply_status(session, action, changes.pop("status"), note=changes.get("note"))
    changed_fields: dict = {}
    for field, value in changes.items():
        if field not in {
            "title",
            "description",
            "priority",
            "owner_employee_id",
            "owner_department_id",
            "due_date",
            "next_step",
            "note",
        }:
            continue
        old_value = getattr(action, field)
        if old_value != value:
            changed_fields[field] = {
                "from": old_value.isoformat() if isinstance(old_value, date) else old_value,
                "to": value.isoformat() if isinstance(value, date) else value,
            }
            setattr(action, field, value)
    if changed_fields:
        action.updated_by = "admin"
        action.events.append(_event(action, event_type="updated", changed_fields=changed_fields))
    session.commit()
    session.refresh(action)
    return view_service.serialize_action(session, action, include_events=True)


@router.post("/{action_id}/status")
def update_management_action_status(
    action_id: int,
    payload: StatusRequest,
    actor: Annotated[CurrentSession, CurrentActor],
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """改推进状态。

    **状态变更会通知负责人**（可关）：他被告知「这件事要你跟进」，
    却发现它被悄悄改成「已解决」时，只能靠自己去翻页面才知道 —— 那等于
    把他排除在这件事之外。确认办结与退回重开对他都是要紧信息。

    通知走的是会话消息（同一套投递、冷却与审计），所以在站内也**留下一条记录**：
    双方都能回看到「这条状态是什么时候、被谁改的」，不必依赖通知本身。
    """
    action = session.get(ManagementAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="管理事项不存在")
    previous = action.status
    _apply_status(session, action, payload.status, note=payload.note)

    delivery = None
    if payload.notify and action.owner_employee_id and previous != action.status:
        # 消息正文要说清「改成了什么」，而不是只说「有更新」：
        # 负责人在手机上一眼要能判断这件事还需不需要他动手。
        label = _STATUS_LABELS.get(action.status, action.status)
        text = f"状态已更新为「{label}」。"
        if payload.note:
            text += f"\n说明：{payload.note}"
        message = message_service.append_message(
            session,
            action,
            author_role=ManagementActionAuthor.ADMIN,
            body=text,
            author_employee_id=actor.employee_id,
            author_name=actor.name,
        )
        session.flush()
        result = message_service.notify(
            session,
            action,
            message,
            audience=message_service.NotificationAudience.OWNER,
            operator_employee_id=actor.employee_id,
            # 改状态是明确的人工动作，不该被「10 分钟前刚发过消息」的冷却挡住
            # ——那会让他永远收不到办结通知。
            force=True,
        )
        delivery = result.to_dict()

    session.commit()
    session.refresh(action)
    payload_out = view_service.serialize_action(session, action, include_events=True)
    # 字段始终存在（未通知时为 None）：调用方可以无条件读取，
    # 不必靠「键在不在」去猜是否执行过通知。
    payload_out["status_notice"] = delivery
    return payload_out


# --------------------------------------------------------------------------- #
# 沟通会话
# --------------------------------------------------------------------------- #
class MessageCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=message_service.BODY_MAX_CHARS)
    # 默认通知对方；操作者可以主动取消（例如只是补一句说明，不想再打扰他）。
    notify: bool = True


class NotifyRequest(BaseModel):
    audience: str | None = Field(default=None, description="owner / admin，默认推给负责人")
    force: bool = Field(default=False, description="跳过冷却，用于修好企微可见范围后重发")


def post_action_message(
    session: Session,
    action: ManagementAction,
    *,
    author_role: str,
    payload: MessageCreateRequest,
    author_employee_id: int | None,
    author_name: str | None,
    view: str,
) -> dict:
    """发消息（管理员侧与负责人侧共用）。

    返回结构与 GET messages 一致，前端发完可直接替换本地会话，不用再拉一次。
    """
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    try:
        message, delivery = message_service.post_message(
            session,
            action,
            author_role=author_role,
            body=body,
            author_employee_id=author_employee_id,
            author_name=author_name,
            notify_peer=payload.notify,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    session.refresh(message)
    return {
        "message": message_service.message_view(message, view),
        "delivery": delivery.to_dict() if delivery else None,
        "readiness": message_service.notify_readiness(session, action),
    }


@router.get("/{action_id}/messages")
def list_action_messages(
    action_id: int,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    """**只读**读取会话。

    轮询走这个接口：把「标记已读」放进 GET 会让未读在用户根本没看内容时
    就被清空（前端每 20 秒轮询一次）。已读必须由显式的 POST 触发。
    """
    action = _load_visible_action(session, action_id, actor)
    reader = "admin" if actor.is_admin else "owner"
    unread = message_service.unread_counts(session, [action.id], reader=reader)
    owner = (
        session.get(Employee, action.owner_employee_id)
        if action.owner_employee_id
        else None
    )
    payload = {
        "items": message_service.thread(session, action, view=reader),
        "unread_count": unread.get(action.id, 0),
        "owner_name": owner.name if owner else None,
    }
    # 投递前提只给管理侧：负责人看到「某个管理员的企微收不到」既帮不上忙，
    # 也不该知道内部有哪些人参与。
    if actor.is_admin:
        payload["readiness"] = message_service.notify_readiness(session, action)
    return payload


@router.post("/{action_id}/messages")
def create_action_message(
    action_id: int,
    payload: MessageCreateRequest,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    action = _load_visible_action(session, action_id, actor)
    return post_action_message(
        session,
        action,
        author_role=ManagementActionAuthor.ADMIN if actor.is_admin else ManagementActionAuthor.OWNER,
        payload=payload,
        author_employee_id=actor.employee_id,
        author_name=actor.name,
        view="admin" if actor.is_admin else "member",
    )


@router.post("/{action_id}/messages/read")
def mark_action_messages_read(
    action_id: int,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    """显式标记「对方发来的消息」为已读。"""
    action = _load_visible_action(session, action_id, actor)
    reader = "admin" if actor.is_admin else "owner"
    updated = message_service.mark_read(session, action, reader=reader)
    session.commit()
    return {"updated": updated, "unread_count": 0}


@router.post("/{action_id}/notify")
def notify_action(
    action_id: int,
    payload: NotifyRequest,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    """单独催办 / 重发通知。

    存在的理由：投递失败（例如企微应用可见范围没覆盖到人）修好之后，
    需要一个「再发一次」的动作，否则只能靠对方下次自己打开页面。
    """
    action = _load_visible_action(session, action_id, actor)
    audience = payload.audience or message_service.NotificationAudience.OWNER
    if audience not in message_service.NotificationAudience.all():
        raise HTTPException(status_code=400, detail="audience 只支持 owner / admin")
    last = session.scalars(
        select(ManagementActionMessage)
        .where(ManagementActionMessage.action_id == action.id)
        .order_by(ManagementActionMessage.id.desc())
    ).first()
    if last is None:
        raise HTTPException(status_code=400, detail="该事项还没有消息，先发一条再通知")
    result = message_service.notify(
        session,
        action,
        last,
        audience=audience,
        operator_employee_id=actor.employee_id,
        force=payload.force,
    )
    session.commit()
    return {"delivery": result.to_dict()}


@router.post("/{action_id}/messages/{message_id}/recall")
def recall_action_message(
    action_id: int,
    message_id: int,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    """撤回这条消息已经推送出去的企业微信卡片。

    只给管理侧：员工没有撤回语义 —— 他发的消息是「我说过的话」，
    收回它等于篡改记录，而管理侧撤回的是「打扰到他人的那张卡片」。
    """
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可以撤回通知")
    action = _load_visible_action(session, action_id, actor)
    message = session.get(ManagementActionMessage, message_id)
    if message is None or message.action_id != action.id:
        raise HTTPException(status_code=404, detail="消息不存在")
    result = message_service.recall_message(
        session, message, recalled_by_role=ManagementActionAuthor.ADMIN
    )
    session.commit()
    session.refresh(message)
    return {
        "recall": result.to_dict(),
        "message": message_service.message_view(message, "admin"),
    }
