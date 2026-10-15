"""周报催交提醒。

设计前提：发消息是**会打扰到真人**的动作，撤回不了。所以：

1. 默认 dry_run=true，只返回「将要发给谁 + 卡片长什么样」，不真发
2. 真正投递必须显式 dry_run=false，且前端要先弹二次确认
3. 没有企微 userid 的员工自动跳过，并在 skipped 里说明原因
4. 同一员工同一周期在冷却期内不重复发送（避免一天催五遍），且每次真发都落审计日志
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import AdminOnly, get_db
from app.services.notification_service import build_notification_feed
from app.services.reminder_service import send_reminders
from app.services.data_scope import current_scope
from app.services.view_service import (
    parse_date,
    resolve_period,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class RemindRequest(BaseModel):
    start: str | None = Field(default=None, description="周期开始 YYYY-MM-DD")
    end: str | None = Field(default=None, description="周期结束 YYYY-MM-DD")
    employee_ids: list[int] | None = Field(
        default=None, description="指定提醒对象；留空=本周期全部未提交的人"
    )
    dry_run: bool = Field(default=True, description="true=只预览不发送")
    message: str | None = Field(default=None, description="自定义提醒话术")


@router.post("/remind")
def send_remind(
    payload: RemindRequest,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """生成/发送周报催交卡片。

    dry_run=true（默认）只预览；真正发送需要显式传 dry_run=false。
    """
    if payload.start and payload.end and not parse_date(payload.start):
        raise HTTPException(status_code=400, detail="start / end 格式应为 YYYY-MM-DD")

    employee_ids = payload.employee_ids
    scope = current_scope(session)
    if scope.is_scoped and employee_ids:
        from app.models.employee import Employee
        from sqlalchemy import select

        # dept_admin 只能催交自己管辖子树内的人；范围外的 id 静默剔除，
        # 与「默认目标自动按范围收口」保持同一语义
        scoped_ids = set(
            session.scalars(
                select(Employee.id).where(
                    Employee.id.in_(employee_ids), scope.employee_condition()
                )
            )
        )
        employee_ids = [emp_id for emp_id in employee_ids if emp_id in scoped_ids]

    start_date, end_date = resolve_period(payload.start, payload.end)
    return send_reminders(
        session,
        start_date,
        end_date,
        employee_ids=employee_ids,
        dry_run=payload.dry_run,
        message=payload.message,
        triggered_by="api:manual",
    )


@router.get("/remind/targets")
def list_remind_targets(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """预览：本周期会被提醒的人及卡片（用于前端确认弹窗）。"""
    start_date, end_date = resolve_period(start, end)
    return send_reminders(session, start_date, end_date, dry_run=True)


@router.get("/feed", dependencies=[AdminOnly])
def notification_feed(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """消息中心数据源：把同步报错、AI 故障与周期提醒汇总成一份可点开的清单。

    纯只读聚合，不落库、不改业务数据。「已读」由前端按「消息 id + 发生时间」记忆，
    后端不维护状态 —— 否则这个问题修好又复发时，旧记录会被误当成已读过。
    """
    if start and end and (not parse_date(start) or not parse_date(end)):
        raise HTTPException(status_code=400, detail="start / end 格式应为 YYYY-MM-DD")
    return build_notification_feed(session, start=start, end=end)
