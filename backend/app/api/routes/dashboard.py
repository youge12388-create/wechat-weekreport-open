from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.api.deps import AdminOnly, get_db
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.report_item import ReportItem
from app.models.report_score import ReportScore
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services.insight_service import (
    analysis_coverage,
    dashboard_summary,
    department_analysis_batch,
)
from app.services.period_summary_service import enqueue_period_summary, period_summary_state
from app.services.report_analysis_service import latest_unified_results
from app.services.data_scope import current_scope
from app.services.view_service import (
    department_employee_counts,
    exempt_department_ids,
    last_sync_at,
    non_exempt_report_condition,
    report_period_overlap_conditions,
    reportable_employee_conditions,
    reportable_employee_ids,
    resolve_period,
    serialize_department,
    submitted_employee_ids,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class PeriodSummaryRequest(BaseModel):
    start: str | None = None
    end: str | None = None
    force: bool = False


def _average_report_score(
    session: Session,
    start: date,
    end: date,
    *,
    department_id: int | None = None,
) -> float | None:
    """返回已完成统一分析周报的内容完整度参考分。

    ``ReportScore`` 可能残留在正文变化后的旧记录中，因此必须同时要求
    ``ai_checked_at`` 非空，避免把过期分数投影到首页。
    """
    query = (
        select(func.avg(ReportScore.total_score))
        .select_from(ReportScore)
        .join(WeeklyReport, WeeklyReport.id == ReportScore.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            *report_period_overlap_conditions(start, end),
            WeeklyReport.ai_checked_at.isnot(None),
            *reportable_employee_conditions(session),
        )
    )
    if department_id is not None:
        query = query.where(Employee.department_id == department_id)
    value = session.scalar(query)
    return round(float(value), 1) if value is not None else None


def _average_report_score_by_department(
    session: Session, start: date, end: date
) -> dict[int, float]:
    """一次算出各部门的内容完整度参考分。

    逐部门调用 ``_average_report_score(..., department_id=...)`` 是另一条
    按部门数的 N+1；口径必须完全一致（同样要求 ``ai_checked_at`` 非空），
    所以两边共用同一组 where 条件与 ``round(x, 1)`` 取值。
    """
    rows = session.execute(
        select(
            Employee.department_id,
            func.avg(ReportScore.total_score),
        )
        .select_from(ReportScore)
        .join(WeeklyReport, WeeklyReport.id == ReportScore.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id)
        .where(
            *report_period_overlap_conditions(start, end),
            WeeklyReport.ai_checked_at.isnot(None),
            *reportable_employee_conditions(session),
        )
        .group_by(Employee.department_id)
    )
    return {
        dept_id: round(float(value), 1)
        for dept_id, value in rows
        if dept_id is not None and value is not None
    }


def _submission_trend(
    session: Session,
    start: date,
    end: date,
    *,
    periods: int = 8,
) -> list[dict]:
    """返回当前筛选周期之前的等长统计周期提交趋势。

    员工基数使用当前在职且需提交周报的员工集合；项目当前周期规则已确认
    为自然周，因此 8 个周期默认对应近 8 周。分数缺失时返回 ``None``。
    """
    period_days = max((end - start).days + 1, 1)
    reportable_ids = reportable_employee_ids(session)
    employee_total = len(reportable_ids)
    result: list[dict] = []
    for offset in range(periods - 1, -1, -1):
        bucket_start = start - timedelta(days=period_days * offset)
        bucket_end = bucket_start + timedelta(days=period_days - 1)
        submitted = len(submitted_employee_ids(session, bucket_start, bucket_end) & reportable_ids)
        report_total = session.scalar(
            select(func.count(WeeklyReport.id))
            .select_from(WeeklyReport)
            .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
            .where(
                *report_period_overlap_conditions(bucket_start, bucket_end),
                *reportable_employee_conditions(session),
            )
        ) or 0
        result.append(
            {
                "period_start": bucket_start.isoformat(),
                "period_end": bucket_end.isoformat(),
                "submitted": submitted,
                "unsubmitted": max(employee_total - submitted, 0),
                "report_total": int(report_total),
                "average_score": _average_report_score(session, bucket_start, bucket_end),
            }
        )
    return result


def _abnormal_reports(session: Session, start: date, end: date, *, limit: int = 8) -> list[dict]:
    """投影本周期有可追溯分析证据的异常周报。

    异常只表示需要人工核对，不是绩效判断：信息不足、出现高风险事项，
    或存在未解决卡点/问题/外部依赖，三者满足其一即入选。
    """
    reports = list(
        session.execute(
            select(WeeklyReport, Employee, Department)
            .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(
                *report_period_overlap_conditions(start, end),
                WeeklyReport.ai_checked_at.isnot(None),
                non_exempt_report_condition(session),
            )
            .order_by(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc())
        )
    )
    if not reports:
        return []
    report_ids = [report.id for report, _, _ in reports]
    unified = latest_unified_results(session, report_ids)
    item_rows = session.execute(
        select(ReportItem.report_id, ReportItem.item_type, ReportItem.risk_level)
        .where(ReportItem.report_id.in_(report_ids))
    )
    item_flags: dict[int, set[str]] = {}
    for report_id, item_type, risk_level in item_rows:
        flags = item_flags.setdefault(report_id, set())
        if item_type == "risks" and risk_level == "高":
            flags.add("high_risk")
        if item_type in {"blockers", "problems", "dependencies"}:
            flags.add("blocker")

    candidates: list[tuple[int, dict]] = []
    reason_labels = {
        "thin": "信息不足",
        "high_risk": "存在高风险事项",
        "blocker": "存在卡点 / 问题 / 外部依赖",
    }
    priority = {"high_risk": 3, "blocker": 2, "thin": 1}
    for report, employee, department in reports:
        analysis = unified.get(report.id)
        if analysis is None:
            continue
        flags = set(item_flags.get(report.id, set()))
        if bool((analysis.get("result") or {}).get("is_thin")):
            flags.add("thin")
        if not flags:
            continue
        reasons = sorted(flags, key=lambda flag: priority[flag], reverse=True)
        candidates.append(
            (
                max(priority[flag] for flag in reasons),
                {
                    "id": report.id,
                    "employee_id": employee.id if employee else None,
                    "employee_name": employee.name if employee else None,
                    "department_name": department.name if department else None,
                    "title": report.title,
                    "submitted_at": report.submitted_at.isoformat() if report.submitted_at else None,
                    "reasons": [reason_labels[flag] for flag in reasons],
                    "href": f"/reports/{report.id}",
                },
            )
        )
    candidates.sort(
        key=lambda item: (
            item[0],
            item[1]["submitted_at"] or "",
            item[1]["id"],
        ),
        reverse=True,
    )
    return [item for _, item in candidates[:limit]]


@router.get("/overview")
def overview(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """本周概览（需求 11.1）。

    统计数字全部由数据库计算；未提交基线 = 应提交周报的在职员工
    （已确认口径，且已剔除免提交部门 —— 否则她们会永久拉低提交率）。
    """
    start_date, end_date = resolve_period(start, end)

    # 已提交只数「应提交」的人：免提交部门的人就算交了也不计入分子，
    # 否则会出现 submitted > employee_total 的怪数字
    reportable_ids = reportable_employee_ids(session)
    employee_total = len(reportable_ids)
    submitted = len(submitted_employee_ids(session, start_date, end_date) & reportable_ids)
    unsubmitted = employee_total - submitted

    item_period = report_period_overlap_conditions(start_date, end_date)
    item_base = (
        select(func.count(ReportItem.id))
        .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
        .where(*item_period, non_exempt_report_condition(session))
    )
    work_items_total = session.scalar(item_base) or 0
    done = session.scalar(
        item_base.where(ReportItem.item_type.in_(["completed_items", "results"]))
    ) or 0
    in_progress = session.scalar(
        item_base.where(ReportItem.item_type.in_(["ongoing_items", "next_week_plans"]))
    ) or 0
    pending_confirm = session.scalar(
        item_base.where(ReportItem.status == "待确认")
    ) or 0
    report_total = session.scalar(
        select(func.count(WeeklyReport.id))
        .select_from(WeeklyReport)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            *report_period_overlap_conditions(start_date, end_date),
            *reportable_employee_conditions(session),
        )
    ) or 0
    # 首页只统计本周期周报实际出现的开放卡点，避免把全部历史卡点堆到本周概览。
    active_blocker_base = select(func.count(distinct(Blocker.id))).join(Task).join(
        TaskReportLink, TaskReportLink.task_id == Task.id
    ).join(
        WeeklyReport, WeeklyReport.id == TaskReportLink.report_id
    ).where(
        Blocker.status == "OPEN",
        *item_period,
        non_exempt_report_condition(session),
    )
    blocker_total = session.scalar(active_blocker_base) or 0
    new_blockers = session.scalar(
        active_blocker_base.where(Blocker.first_seen_date >= start_date)
    ) or 0
    long_blockers = session.scalar(
        active_blocker_base.where(Blocker.duration_weeks >= 3)
    ) or 0
    high_risk_blockers = session.scalar(
        active_blocker_base.where(Blocker.risk_level == "高")
    ) or 0
    analysis = dashboard_summary(session, start_date, end_date)
    ai_period_summary = period_summary_state(session, start_date, end_date)

    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "employee_total": int(employee_total),
        "submitted": submitted,
        "unsubmitted": max(unsubmitted, 0),
        "submit_rate": round(submitted / employee_total * 100, 1) if employee_total else 0,
        "report_total": int(report_total),
        "average_score": _average_report_score(session, start_date, end_date),
        "submission_trend": _submission_trend(session, start_date, end_date),
        "abnormal_reports": _abnormal_reports(session, start_date, end_date),
        "work_item_total": int(work_items_total),
        "work_item_done": int(done),
        "work_item_in_progress": int(in_progress),
        "work_item_pending": int(pending_confirm),
        "blocker_total": int(blocker_total),
        "new_blockers": int(new_blockers),
        "long_blockers": int(long_blockers),
        "high_risk_blockers": int(high_risk_blockers),
        "analysis_coverage": analysis["coverage"],
        "top_items": analysis["top_items"],
        "sections": analysis["sections"],
        "current_blockers": analysis["current_blockers"],
        "action_items": analysis["action_items"],
        "period_comparison": analysis["period_comparison"],
        "trend": analysis["trend"],
        "insights": analysis["insights"],
        "ai_period_summary": ai_period_summary,
        # 数据新鲜度：最近一次成功同步（周报）的时间，前端据此提示「数据可能陈旧」
        "last_sync_at": last_sync_at(session, "weekly_reports").isoformat()
        if last_sync_at(session, "weekly_reports")
        else None,
    }


@router.post("/period-summary", dependencies=[AdminOnly])
def generate_period_summary(
    payload: PeriodSummaryRequest, session: Annotated[Session, Depends(get_db)]
) -> dict:
    """手动请求全员周报总结；实际模型调用由后台 Worker 消费。

    仅超管可用：总结内容覆盖全公司周报，对部门管理员开放等于变相泄露
    其他部门的工作内容。
    """
    start_date, end_date = resolve_period(payload.start, payload.end)
    return enqueue_period_summary(session, start_date, end_date, force=payload.force)


@router.get("/period-summary", dependencies=[AdminOnly])
def read_period_summary(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """只读查询周期总结状态与结论。

    「本周概览」需要先抓最新周报、再等后台总结，期间要反复看「到哪一步了」。
    只读接口让前端轮询不必重复提交模型任务（POST 会重置队列的 payload_hash）。
    """
    start_date, end_date = resolve_period(start, end)
    return period_summary_state(session, start_date, end_date)


@router.get("/status")
def dashboard_status(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """首页轮询用的轻量状态（AI 分析进度 + 周期总结进度）。

    首页在「本期还在等 AI 分析」或「周期总结生成中」时每 4 秒刷新一次。
    旧实现轮询的是整个 ``/overview``：一次两个重接口级别的查询（趋势、异常、
    洞察全都要重算），只为读两个会变的状态字段 —— 实测在 550 篇周报的数据量下
    这就是持续的高负载。这里只查两个计数与一个队列状态行。

    字段口径与 ``/overview`` 完全一致（同样走 ``analysis_coverage`` 与
    ``period_summary_state``），所以前端可以安全地用它替换整页刷新。
    """
    start_date, end_date = resolve_period(start, end)
    coverage = analysis_coverage(session, start_date, end_date)
    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "analysis_coverage": coverage,
        "ai_period_summary": period_summary_state(session, start_date, end_date),
    }


@router.get("/departments")
def department_stats(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    detail: bool = Query(
        default=True,
        description="是否附带 AI 分析类字段（事项数/卡点/温度/覆盖率）。"
        "首页只展示提交情况与参考分，传 false 可省去整轮分析。",
    ),
) -> dict:
    """部门统计（需求 11.2 表格数据源）。

    分两档返回：``submitted/unsubmitted/reports_count`` 等提交口径永远都有，
    需要跨全部事项、卡点与任务聚合出来的分析类字段（``work_item_*`` /
    ``blocker_total`` / ``department_status`` / ``analysis_coverage``）只在
    ``detail=true`` 时计算。首页根本渲染这些字段，却为此付了整轮分析的钱。
    """
    start_date, end_date = resolve_period(start, end)

    exempt_ids = exempt_department_ids(session)
    departments = session.scalars(
        select(Department).order_by(Department.id)
    ).all()
    emp_counts = department_employee_counts(session, exclude_dept_ids=exempt_ids)
    submitted_ids = submitted_employee_ids(session, start_date, end_date)
    emp_by_dept = {d.id: [] for d in departments}
    employee_rows = session.execute(
        select(Employee.department_id, Employee.id).where(
            *reportable_employee_conditions(session)
        )
    )
    for dept_id, emp_id in employee_rows:
        if dept_id in emp_by_dept:
            emp_by_dept[dept_id].append(emp_id)

    eff_end = func.coalesce(WeeklyReport.end_date, WeeklyReport.start_date)
    report_count_rows = session.execute(
        select(
            Employee.department_id,
            func.count(distinct(WeeklyReport.id)),
        )
        .join(WeeklyReport, WeeklyReport.employee_id == Employee.id)
        .where(
            *reportable_employee_conditions(session),
            WeeklyReport.start_date <= end_date,
            eff_end >= start_date,
        )
        .group_by(Employee.department_id)
    )
    report_counts = {dept_id: int(c) for dept_id, c in report_count_rows}

    analyses: dict[int, dict] = {}
    # 参考分始终计算：它只是一条分组聚合，而首页的部门进度条提示里要显示它
    # （detail=false 时省掉的是整轮部门分析，不是这一条便宜的统计）。
    average_scores = _average_report_score_by_department(session, start_date, end_date)
    if detail and departments:
        # 一次算出全部部门，而不是在下面的循环里逐部门各跑四组查询。
        analyses = department_analysis_batch(
            session, [d.id for d in departments], end_date, 1
        )

    # dept_admin：部门统计只出管辖子树内的部门
    scope = current_scope(session)
    if scope.is_scoped:
        scoped = set(scope.department_ids)
        departments = [dept for dept in departments if dept.id in scoped]

    items = []
    for dept in departments:
        emp_ids = emp_by_dept.get(dept.id, [])
        submitted = len(submitted_ids & set(emp_ids))
        employee_total = emp_counts.get(dept.id, 0)
        item = serialize_department(dept, employee_total, exempt=dept.id in exempt_ids)
        item.update(
            {
                "submitted": submitted,
                "unsubmitted": max(employee_total - submitted, 0),
                "reports_count": report_counts.get(dept.id, 0),
                "average_score": average_scores.get(dept.id),
            }
        )
        if detail:
            analysis = analyses.get(dept.id)
            item.update(
                {
                    "work_item_total": analysis["counts"]["total"] if analysis else 0,
                    "work_item_done": analysis["counts"]["completed"] if analysis else 0,
                    "work_item_in_progress": (
                        analysis["counts"]["ongoing"] if analysis else 0
                    ),
                    "blocker_total": len(analysis["blockers"]) if analysis else 0,
                    "department_status": analysis["status"] if analysis else "正常",
                    "analysis_coverage": analysis["coverage"]
                    if analysis
                    else {"reports_total": 0, "analyzed_reports": 0, "rate": 0},
                }
            )
        items.append(item)

    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "detail": detail,
        "items": items,
    }
