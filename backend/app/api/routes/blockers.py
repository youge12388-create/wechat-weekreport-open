from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.deps import AdminOnly, get_db
from app.models.blocker import Blocker
from app.models.employee import Employee
from app.models.enums import BlockerStatus
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services.blocker_summary_service import (
    blocker_summary_state,
    enqueue_blocker_summary,
)
from app.services.structured_data_service import (
    BLOCKER_ITEM_TYPES,
    RESOURCE_DEMAND_TASK_PREFIX,
    recompute_blocker_status,
)
from app.services.view_service import (
    non_exempt_report_condition,
    report_period_overlap_conditions,
    resolve_period,
)
from app.services.data_scope import current_scope

router = APIRouter(prefix="/api/blockers", tags=["blockers"])


def _task_in_scope(session: Session, task_id: int) -> bool:
    """dept_admin 只能查看证据来源在其管辖部门子树内的卡点。

    列表接口经 non_exempt_report_condition（含范围条件）自动收口；
    详情是单点查询，必须单独守卫，否则换个 id 就绕过了列表过滤。
    """
    scope = current_scope(session)
    if scope.is_all:
        return True
    return (
        session.scalar(
            select(TaskReportLink.task_id)
            .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
            .where(
                TaskReportLink.task_id == task_id,
                scope.report_employee_id_condition(),
            )
            .limit(1)
        )
        is not None
    )


def _sources(
    session: Session,
    task_ids: list[int],
    start=None,
    end=None,
    weekly_only=False,
) -> dict[int, list[dict]]:
    if not task_ids:
        return {}
    query = select(
        TaskReportLink.task_id,
        WeeklyReport.id,
        WeeklyReport.title,
        WeeklyReport.start_date,
        WeeklyReport.end_date,
        Employee.name,
        ReportItem.source_text,
        ReportItem.task_key,
    ).select_from(TaskReportLink)
    query = (
        query
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            TaskReportLink.task_id.in_(task_ids),
            non_exempt_report_condition(session),
        )
    )
    if start is not None and end is not None:
        query = query.where(*report_period_overlap_conditions(start, end))
    if weekly_only:
        query = query.where(
            WeeklyReport.report_type == "weekly",
            ReportItem.item_type.in_(BLOCKER_ITEM_TYPES),
        )
    rows = session.execute(query.order_by(WeeklyReport.end_date.desc(), WeeklyReport.id.desc()))
    result: dict[int, list[dict]] = {task_id: [] for task_id in task_ids}
    seen: dict[int, set[int]] = {task_id: set() for task_id in task_ids}
    for task_id, report_id, title, start, end, employee_name, source_text, task_key in rows:
        if report_id in seen[task_id]:
            continue
        seen[task_id].add(report_id)
        result[task_id].append(
            {
                "report_id": report_id,
                "title": title,
                "employee_name": employee_name,
                "period_start": start.isoformat() if start else None,
                "period_end": end.isoformat() if end else None,
                "source_text": source_text,
                "source_kind": (
                    "resource_need"
                    if task_key and task_key.startswith(RESOURCE_DEMAND_TASK_PREFIX)
                    else "ai"
                ),
            }
        )
    return result


def _serialize(blocker: Blocker, task: Task, sources: list[dict]) -> dict:
    # 资源需求是确定的原文字段候选，不等同于模型推断的卡点。这个标记让处理
    # 中心能明确显示来源和「待确认」，同时不改变旧 Blocker 返回结构。
    resource_source = any(
        source.get("source_kind") == "resource_need" for source in sources
    )
    # 候选层（CANDIDATE）的资源需求同样是「待管理员确认」的一次性提及。
    pending_confirmation = resource_source and blocker.status in (
        BlockerStatus.OPEN,
        BlockerStatus.CANDIDATE,
    )
    return {
        "id": blocker.id,
        "task_id": task.id,
        "title": blocker.title,
        "description": blocker.description,
        "status": blocker.status,
        "risk_level": blocker.risk_level,
        "first_seen_date": blocker.first_seen_date.isoformat() if blocker.first_seen_date else None,
        "last_seen_date": blocker.last_seen_date.isoformat() if blocker.last_seen_date else None,
        "duration_weeks": blocker.duration_weeks,
        "occurrence_count": blocker.occurrence_count,
        "related_project": task.related_project,
        "task_status": task.status,
        "source_kind": "resource_need" if resource_source else "ai",
        "source_field": "资源需求" if resource_source else None,
        "confirmation_status": "pending" if pending_confirmation else (
            "resolved" if blocker.status == BlockerStatus.RESOLVED else "confirmed"
        ),
        "pending_confirmation": pending_confirmation,
        "sources": sources,
    }


@router.get("")
def list_blockers(
    session: Annotated[Session, Depends(get_db)],
    status: str | None = Query(default="OPEN"),
    risk_level: str | None = Query(default=None),
    long_only: bool = Query(default=False),
    new_only: bool = Query(default=False),
    scope: str | None = Query(
        default=None,
        description="快捷筛选：all/open/long/new/high/resolved",
    ),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    weekly_only: bool = Query(default=False),
    count_only: bool = Query(default=False),
    department_id: int | None = Query(
        default=None,
        description="按部门筛选：卡点任务的证据里有该部门员工的周报行",
    ),
) -> dict:
    valid_scopes = {
        "all",
        "open",
        "long",
        "new",
        "high",
        "resolved",
        "candidate",
        "dormant",
        "archived",
    }
    if scope and scope not in valid_scopes:
        raise HTTPException(
            status_code=400,
            detail="scope 只支持 all / open / long / new / high / resolved / "
            "candidate / dormant / archived",
        )
    conditions = []
    # 保持旧调用默认只看开放卡点；快捷 scope 显式覆盖默认状态。
    effective_status = status
    if effective_status in {"", "ALL", "all"}:
        effective_status = None
    if scope == "all":
        effective_status = None
    elif scope == "open":
        effective_status = BlockerStatus.OPEN
    elif scope == "resolved":
        effective_status = BlockerStatus.RESOLVED
    elif scope == "candidate":
        effective_status = BlockerStatus.CANDIDATE
    elif scope == "dormant":
        effective_status = BlockerStatus.DORMANT
    elif scope == "archived":
        effective_status = BlockerStatus.ARCHIVED
    elif scope in {"long", "new", "high"}:
        effective_status = BlockerStatus.OPEN
    if effective_status:
        if effective_status not in {
            BlockerStatus.OPEN,
            BlockerStatus.RESOLVED,
            BlockerStatus.CANDIDATE,
            BlockerStatus.DORMANT,
            BlockerStatus.ARCHIVED,
        }:
            raise HTTPException(
                status_code=400,
                detail="status 只支持 OPEN / RESOLVED / CANDIDATE / DORMANT / ARCHIVED",
            )
        conditions.append(Blocker.status == effective_status)
    if risk_level:
        if risk_level not in {"低", "中", "高"}:
            raise HTTPException(status_code=400, detail="risk_level 只支持 低 / 中 / 高")
        conditions.append(Blocker.risk_level == risk_level)
    is_long = long_only or scope == "long"
    is_new = new_only or scope == "new"
    if is_long:
        conditions.append(Blocker.duration_weeks >= 3)
    if scope == "high":
        conditions.append(Blocker.risk_level == "高")
    if weekly_only:
        evidence = (
            select(TaskReportLink.task_id)
            .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
            .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
            .where(
                WeeklyReport.report_type == "weekly",
                ReportItem.item_type.in_(BLOCKER_ITEM_TYPES),
                non_exempt_report_condition(session),
            )
        )
        if start or end:
            evidence = evidence.where(*report_period_overlap_conditions(*resolve_period(start, end)))
        conditions.append(Blocker.task_id.in_(evidence))
    # 「本周新增」需要周期边界；未显式传入时使用项目统一默认周期。
    period = resolve_period(start, end) if start or end or is_new else None
    if is_new and period:
        conditions.extend(
            [
                Blocker.first_seen_date >= period[0],
                Blocker.first_seen_date <= period[1],
            ]
        )
    if period:
        conditions.append(
            Blocker.task_id.in_(
                select(TaskReportLink.task_id)
                .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
                .where(
                    *report_period_overlap_conditions(*period),
                    non_exempt_report_condition(session),
                )
            )
        )
    if department_id is not None:
        # 部门归属 = 卡点任务的证据里有该部门员工的周报行（不限事项类型）。
        # 有周期时限定窗口内证据 —— 与经营看板部门卡「N 项卡点未关闭」徽标
        # 同口径（operating_dashboard_service._open_blockers_by_department），
        # 点过去的列表条数必须与徽标数字相等。
        dept_evidence = (
            select(TaskReportLink.task_id)
            .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
            .join(Employee, Employee.id == ReportItem.employee_id)
            .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
            .where(
                Employee.department_id == department_id,
                WeeklyReport.report_type == "weekly",
                non_exempt_report_condition(session),
            )
        )
        if period:
            dept_evidence = dept_evidence.where(
                *report_period_overlap_conditions(*period)
            )
        conditions.append(Blocker.task_id.in_(dept_evidence))
    else:
        # 无周期筛选时也只展示至少有一条非免提交来源的卡点；历史归档仍可
        # 通过周报/原始证据查询，管理处理中心不应泄漏免提交来源。
        conditions.append(
            Blocker.task_id.in_(
                select(TaskReportLink.task_id)
                .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
                .where(non_exempt_report_condition(session))
            )
        )
    if count_only:
        return {"total": session.scalar(select(func.count()).select_from(Blocker).where(*conditions)) or 0}
    rows = list(
        session.execute(
            select(Blocker, Task)
            .join(Task, Task.id == Blocker.task_id)
            .where(*conditions)
            .order_by(
                case(
                    (Blocker.risk_level == "高", 3),
                    (Blocker.risk_level == "中", 2),
                    else_=1,
                ).desc(),
                Blocker.duration_weeks.desc(),
                Blocker.last_seen_date.desc(),
            )
        )
    )
    source_map = _sources(
        session, [task.id for _, task in rows], *(period or (None, None)),
        weekly_only=weekly_only,
    )
    return {
        "items": [
            _serialize(blocker, task, source_map.get(task.id, []))
            for blocker, task in rows
        ]
    }


class BlockerSummaryRequest(BaseModel):
    start: str | None = None
    end: str | None = None
    force: bool = False


class BlockerStatusRequest(BaseModel):
    action: str  # resolve（手动标记解决）| reopen（按证据重新推导）


@router.post("/{blocker_id}/status", dependencies=[AdminOnly])
def update_blocker_status(
    blocker_id: int,
    payload: BlockerStatusRequest,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """管理员手动变更卡点生命周期状态。

    resolve：置为 RESOLVED；推导链路会尊重手动解决（仅高风险新证据重开）。
    reopen：忽略手动解决标记，按当前证据重新推导状态（复活）。
    """
    if payload.action not in {"resolve", "reopen"}:
        raise HTTPException(status_code=400, detail="action 只支持 resolve / reopen")
    row = session.execute(
        select(Blocker, Task).join(Task, Task.id == Blocker.task_id).where(Blocker.id == blocker_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="卡点不存在")
    blocker, _task = row
    if payload.action == "resolve":
        blocker.status = BlockerStatus.RESOLVED
    else:
        recompute_blocker_status(session, blocker.id, ignore_manual=True)
    session.commit()
    session.refresh(blocker)
    return {"id": blocker.id, "status": blocker.status}


@router.get("/period-summary", dependencies=[AdminOnly])
def read_blocker_summary(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """只读查询周期卡点总结状态与结论。

    与 `/api/dashboard/period-summary` 同一纪律：轮询必须走 GET，
    POST 会重置队列，前端每几秒轮询一次不能等于反复重新生成。
    """
    start_date, end_date = resolve_period(start, end)
    return blocker_summary_state(session, start_date, end_date)


@router.post("/period-summary", dependencies=[AdminOnly])
def generate_blocker_summary(
    payload: BlockerSummaryRequest, session: Annotated[Session, Depends(get_db)]
) -> dict:
    """手动请求周期卡点总结；实际模型调用由后台 Worker 消费。"""
    start_date, end_date = resolve_period(payload.start, payload.end)
    return enqueue_blocker_summary(session, start_date, end_date, force=payload.force)


@router.get("/{blocker_id}")
def get_blocker(
    blocker_id: int,
    session: Annotated[Session, Depends(get_db)],
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> dict:
    if (start is None) != (end is None):
        raise HTTPException(status_code=400, detail="start 与 end 必须同时传入")
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="start 不能晚于 end")
    row = session.execute(
        select(Blocker, Task)
        .join(Task, Task.id == Blocker.task_id)
        .where(Blocker.id == blocker_id)
    ).first()
    if row is None or not _task_in_scope(session, row[1].id):
        # 范围外按 404：不向越权者泄露卡点存在与否
        raise HTTPException(status_code=404, detail="卡点不存在")
    blocker, task = row
    sources = _sources(session, [task.id], start, end).get(task.id, [])
    return _serialize(blocker, task, sources)
