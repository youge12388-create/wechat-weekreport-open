"""P2 管理洞察：只基于已结构化的周报数据做可审计聚合，不调用模型。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from hashlib import sha1
from typing import Any, Iterable, NamedTuple

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.management_action import ManagementAction
from app.models.report_item import ReportItem
from app.models.report_score import ReportScore
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services import request_memo
from app.services.structured_data_service import RESOURCE_DEMAND_TASK_PREFIX
from app.services.view_service import (
    non_exempt_report_condition,
    report_period_overlap_conditions,
)

DONE_TYPES = {"completed_items", "results"}
ONGOING_TYPES = {"ongoing_items", "next_week_plans"}
BLOCKER_TYPES = {"blockers", "problems", "dependencies", "risks"}
# 首页只展示能直接推动管理动作的聚合洞察；个人事项量变化和责任字段缺口
# 留在洞察中心，避免把数据缺失或数量波动误读成管理结论。
HOME_INSIGHT_KINDS = {"long_blocker", "blocker_growth", "cross_department", "stalled_plan"}


def insight_source_key(kind: str, title: str, start: date, end: date) -> str:
    """返回洞察候选的稳定来源键，避免把长标题直接塞进唯一键。"""
    digest = sha1(title.encode("utf-8")).hexdigest()[:16]
    return f"insight:{kind}:{start.isoformat()}:{end.isoformat()}:{digest}"


def period_before(start: date, weeks: int = 4) -> tuple[date, date]:
    """取得同长度的前序自然日区间，用于同比，避免依赖数据库日期函数。"""
    end = start - timedelta(days=1)
    return end - timedelta(days=weeks * 7 - 1), end


def previous_period(start: date, end: date) -> tuple[date, date]:
    """返回当前区间之前、长度相同的连续区间。

    ``period_before`` 是历史洞察使用的固定四周比较口径，首页看板需要的是
    「本周期 vs 紧邻上一个同长度周期」，因此单独保留这个 helper，避免改变
    既有洞察的语义。
    """
    days = max((end - start).days + 1, 1)
    previous_end = start - timedelta(days=1)
    return previous_end - timedelta(days=days - 1), previous_end


def trailing_period(end: date, weeks: int) -> tuple[date, date]:
    return end - timedelta(days=weeks * 7 - 1), end


# _item_rows 的请求内缓存键（session.info）：首页一次渲染里，本周期与前序周期的
# 事项行会被 dashboard_summary / insights / period_comparison 各要一遍。
_ITEM_ROWS_CACHE_KEY = "insight_service.item_rows"


def _item_rows(
    session: Session,
    start: date,
    end: date,
    *,
    employee_id: int | None = None,
    department_id: int | None = None,
) -> list[tuple[ReportItem, WeeklyReport, Employee | None, Department | None]]:
    """取周期内的事项行（含周报/员工/部门）。

    结果按 (start, end, employee_id, department_id) 缓存在 ``session.info`` 上：
    首页一个请求里本周期与前序周期各会被取两遍（dashboard_summary 自己一遍、
    ``insights`` 与 ``period_comparison`` 再各一遍），而这里每次都要把
    ReportItem/WeeklyReport/Employee/Department 四个实体完整实例化一遍 ——
    实测正是这类组装而非 SQL 本身构成了首页的主要耗时。
    缓存作用域天然是「一次请求一个 Session」，跨请求不会读到旧数据。

    返回的是同一个列表对象，调用方**只读**（现有调用方都只做计数与投影）。
    """
    cache_key = (start, end, employee_id, department_id)
    cache: dict = session.info.setdefault(_ITEM_ROWS_CACHE_KEY, {})
    if cache_key in cache:
        return cache[cache_key]

    query = (
        select(ReportItem, WeeklyReport, Employee, Department)
        .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .join(Department, Department.id == Employee.department_id, isouter=True)
        .where(
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        )
    )
    if employee_id is not None:
        query = query.where(WeeklyReport.employee_id == employee_id)
    if department_id is not None:
        query = query.where(Employee.department_id == department_id)
    rows = list(session.execute(query))
    cache[cache_key] = rows
    return rows


def resource_demand_candidate_condition():
    """「资源需求」直接投影出的卡点候选（非 AI 分析产物）的判定条件。

    原文字段候选（例如「资源需求」直接投影的待确认卡点）也会进入 ReportItem，
    但它不等于 AI 已完成分析。排除带 resource_demand: 前缀的候选，避免仅有这条
    确定性卡点时把整篇未分析周报错误标为 100%。其余 ReportItem 仍保留旧兼容
    口径：测试/历史数据可能只有结构化投影而没有 ai_checked_at；真正由统一分析
    产出的周报同时满足 ai_checked_at。
    """
    return (
        (ReportItem.item_type == "blockers")
        & ReportItem.task_key.is_not(None)
        & ReportItem.task_key.like(f"{RESOURCE_DEMAND_TASK_PREFIX}%")
    )


def analysis_coverage(
    session: Session, start: date, end: date, *, employee_id: int | None = None,
    department_id: int | None = None,
) -> dict:
    report_query = (
        select(func.count(WeeklyReport.id))
        .select_from(WeeklyReport)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        )
    )
    item_query = (
        select(func.count(func.distinct(ReportItem.report_id)))
        .select_from(ReportItem)
        .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            *report_period_overlap_conditions(start, end),
            (~resource_demand_candidate_condition())
            | WeeklyReport.ai_checked_at.isnot(None),
            non_exempt_report_condition(session),
        )
    )
    if employee_id is not None:
        report_query = report_query.where(WeeklyReport.employee_id == employee_id)
        item_query = item_query.where(WeeklyReport.employee_id == employee_id)
    if department_id is not None:
        report_query = report_query.where(Employee.department_id == department_id)
        item_query = item_query.where(Employee.department_id == department_id)
    reports_total = int(session.scalar(report_query) or 0)
    analyzed_reports = int(session.scalar(item_query) or 0)
    return {
        "reports_total": reports_total,
        "analyzed_reports": analyzed_reports,
        "rate": round(analyzed_reports / reports_total * 100, 1) if reports_total else 0,
    }


def analysis_coverage_batch(
    session: Session, department_ids: Iterable[int], start: date, end: date
) -> dict[int, dict]:
    """一次算清多个部门的分析覆盖率（口径与 ``analysis_coverage`` 逐部门一致）。

    逐部门调用是 O(部门数) 条 SQL，部门多了设置页/看板直接变慢；两处口径必须
    完全一致，所以共享同一个条件构造。
    """
    ids = sorted({int(i) for i in department_ids})
    if not ids:
        return {}
    report_rows = session.execute(
        select(Employee.department_id, func.count(WeeklyReport.id))
        .select_from(WeeklyReport)
        .join(Employee, Employee.id == WeeklyReport.employee_id)
        .where(
            Employee.department_id.in_(ids),
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        )
        .group_by(Employee.department_id)
    )
    item_rows = session.execute(
        select(Employee.department_id, func.count(func.distinct(ReportItem.report_id)))
        .select_from(ReportItem)
        .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id)
        .where(
            Employee.department_id.in_(ids),
            *report_period_overlap_conditions(start, end),
            (~resource_demand_candidate_condition())
            | WeeklyReport.ai_checked_at.isnot(None),
            non_exempt_report_condition(session),
        )
        .group_by(Employee.department_id)
    )
    totals = {dept_id: int(count) for dept_id, count in report_rows}
    analyzed = {dept_id: int(count) for dept_id, count in item_rows}
    result: dict[int, dict] = {}
    for dept_id in ids:
        reports_total = totals.get(dept_id, 0)
        analyzed_reports = analyzed.get(dept_id, 0)
        result[dept_id] = {
            "reports_total": reports_total,
            "analyzed_reports": analyzed_reports,
            "rate": round(analyzed_reports / reports_total * 100, 1)
            if reports_total
            else 0,
        }
    return result


def _active_blocker_ids(session: Session, start: date, end: date) -> set[int]:
    """取在指定周期有周报证据的开放卡点。

    首页的卡点口径必须和 ``dashboard.py`` 既有的 ``blocker_total`` 一致：
    只统计 OPEN 卡点，且通过任务-周报关联确认该卡点在周期内实际出现。

    按 (start, end) 在一次请求内只查一遍：它被 ``_active_blocker_counts``
    （本期与前序周期各一次）与 ``_current_blocker_projection`` 反复调用，
    开发库实测 5 次共 86 ms。返回新 set，避免调用方就地修改污染缓存。
    """
    return set(
        request_memo.memoize(
            session, ("active_blocker_ids", start, end), lambda: _load_active_blocker_ids(session, start, end)
        )
    )


def _load_active_blocker_ids(session: Session, start: date, end: date) -> frozenset[int]:
    rows = session.scalars(
        select(Blocker.id)
        .join(Task, Task.id == Blocker.task_id)
        .join(TaskReportLink, TaskReportLink.task_id == Task.id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .where(
            Blocker.status == "OPEN",
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        )
        .distinct()
    )
    return frozenset(int(blocker_id) for blocker_id in rows)


def _active_blocker_counts(session: Session, start: date, end: date) -> dict[str, int]:
    """返回周期内开放卡点与高风险开放卡点数量。"""
    blocker_ids = _active_blocker_ids(session, start, end)
    if not blocker_ids:
        return {"blockers": 0, "high_risk_blockers": 0}
    high_risk = session.scalar(
        select(func.count(Blocker.id)).where(
            Blocker.id.in_(blocker_ids), Blocker.risk_level == "高"
        )
    ) or 0
    return {
        "blockers": len(blocker_ids),
        "high_risk_blockers": int(high_risk),
    }


def period_comparison(session: Session, start: date, end: date) -> dict:
    """比较当前周期与紧邻的同长度历史周期。

    只投影现有结构化数据（事项、任务、卡点），不调用模型，也不修改任何
    业务表字段。``current``/``previous`` 保留两侧原值，``deltas`` 使用
    ``current - previous``，方便前端同时展示基线和变化。
    """
    previous_start, previous_end = previous_period(start, end)
    current_counts = item_counts(_item_rows(session, start, end))
    previous_counts = item_counts(_item_rows(session, previous_start, previous_end))
    current_blockers = _active_blocker_counts(session, start, end)
    previous_blockers = _active_blocker_counts(session, previous_start, previous_end)

    current = {
        "completed": int(current_counts["completed"]),
        "in_progress": int(current_counts["ongoing"]),
        "blockers": int(current_blockers["blockers"]),
        "high_risk_blockers": int(current_blockers["high_risk_blockers"]),
    }
    previous = {
        "completed": int(previous_counts["completed"]),
        "in_progress": int(previous_counts["ongoing"]),
        "blockers": int(previous_blockers["blockers"]),
        "high_risk_blockers": int(previous_blockers["high_risk_blockers"]),
    }
    deltas = {key: current[key] - previous[key] for key in current}
    # Keep the flat aliases for callers that adopted the first dashboard contract
    # before the nested current/previous shape was finalized.
    return {
        "previous_period": {
            "start": previous_start.isoformat(),
            "end": previous_end.isoformat(),
        },
        "current": current,
        "previous": previous,
        "deltas": deltas,
        "completed_delta": deltas["completed"],
        "in_progress_delta": deltas["in_progress"],
        "blocker_delta": deltas["blockers"],
        "high_risk_delta": deltas["high_risk_blockers"],
    }


def item_counts(rows: Iterable[tuple[ReportItem, WeeklyReport, Employee | None, Department | None]]) -> dict:
    counts = defaultdict(int)
    for item, *_ in rows:
        counts[item.item_type] += 1
    return {
        "total": sum(counts.values()),
        "completed": sum(counts[k] for k in DONE_TYPES),
        "ongoing": sum(counts[k] for k in ONGOING_TYPES),
        "blockers": sum(counts[k] for k in BLOCKER_TYPES),
        "by_type": dict(counts),
    }


def source_for_items(
    rows: Iterable[tuple[ReportItem, WeeklyReport, Employee | None, Department | None]],
    *, limit: int = 5,
) -> list[dict]:
    seen: set[int] = set()
    sources: list[dict] = []
    for item, report, employee, department in rows:
        if report.id in seen:
            continue
        seen.add(report.id)
        sources.append({
            "report_id": report.id,
            "title": report.title,
            "employee_id": employee.id if employee else None,
            "employee_name": employee.name if employee else None,
            "department_name": department.name if department else None,
            "period_start": report.start_date.isoformat() if report.start_date else None,
            "period_end": report.end_date.isoformat() if report.end_date else None,
            "source_text": item.source_text,
        })
        if len(sources) >= limit:
            break
    return sources


def task_sources(session: Session, task_ids: Iterable[int], *, limit: int = 5) -> dict[int, list[dict]]:
    task_ids = list(task_ids)
    if not task_ids:
        return {}
    rows = session.execute(
        select(TaskReportLink.task_id, ReportItem, WeeklyReport, Employee, Department)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .join(Department, Department.id == Employee.department_id, isouter=True)
        .where(
            TaskReportLink.task_id.in_(task_ids),
            non_exempt_report_condition(session),
        )
        .order_by(WeeklyReport.end_date.desc(), WeeklyReport.id.desc())
    )
    result: dict[int, list[dict]] = {task_id: [] for task_id in task_ids}
    seen: dict[int, set[int]] = {task_id: set() for task_id in task_ids}
    for task_id, item, report, employee, department in rows:
        if report.id in seen[task_id] or len(result[task_id]) >= limit:
            continue
        seen[task_id].add(report.id)
        result[task_id].append({
            "report_id": report.id, "title": report.title,
            "employee_id": employee.id if employee else None,
            "employee_name": employee.name if employee else None,
            "department_name": department.name if department else None,
            "period_start": report.start_date.isoformat() if report.start_date else None,
            "period_end": report.end_date.isoformat() if report.end_date else None,
            "source_text": item.source_text,
        })
    return result


def task_report_counts(
    session: Session,
    task_ids: Iterable[int],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[int, int]:
    """每个任务被多少篇**不同**周报记录过 —— 证据篇数的权威口径。

    ``task_sources`` 的 ``limit`` 是给 AI 材料省 token 的**展示截断**；拿截断后的
    列表数篇数，会让「N 篇周报记录过它」永远封顶 5，与瓶颈侧（数真实的
    report_id 集合）同页两个口径，介入清单的排序因此失真。展示截断和证据
    统计必须分开：这里一条 GROUP BY 直接给真实计数，可选按周期重叠过滤。
    """
    ids = list(task_ids)
    if not ids:
        return {}
    conditions: list[Any] = [TaskReportLink.task_id.in_(ids), non_exempt_report_condition(session)]
    if start is not None and end is not None:
        conditions.extend(report_period_overlap_conditions(start, end))
    rows = session.execute(
        select(TaskReportLink.task_id, func.count(distinct(WeeklyReport.id)))
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .where(*conditions)
        .group_by(TaskReportLink.task_id)
    ).all()
    return {task_id: int(count) for task_id, count in rows}


class TaskFact(NamedTuple):
    """``task_rows`` 里任务那一档：只带洞察判定用得到的字段。

    刻意不是 ORM 实体：把 5 个实体整行实例化只是为了读下面这几个字段，
    实测 4304 条关联要 585 ms，而同样的列投影只要 156 ms —— 页面等的是组装，不是 SQL。
    """

    id: int
    title: str
    status: str
    duration_weeks: int
    first_seen_date: date | None
    last_seen_date: date | None


class ItemFact(NamedTuple):
    """事项那一档（只用 item_type 与 mentioned_people 做判定）。"""

    item_type: str
    mentioned_people: list | None


class ReportFact(NamedTuple):
    id: int


class EmployeeFact(NamedTuple):
    id: int


class DepartmentFact(NamedTuple):
    name: str


def task_rows(
    session: Session, *, employee_id: int | None = None, department_id: int | None = None
) -> list[tuple[TaskFact, ItemFact, ReportFact, EmployeeFact | None, DepartmentFact | None]]:
    """任务-事项-周报关联（按列投影的轻量行，形状与原来一致：``row[0].title`` 等）。

    调用方只会读这几个字段（``insights`` 判定跨部门/多人/停滞、``employee_analysis``
    序列化），所以不需要完整实体。**注意：返回的不是可回写的 ORM 对象**，
    要改数据请另走 ORM 查询。
    """
    query = (
        select(
            Task.id,
            Task.title,
            Task.status,
            Task.duration_weeks,
            Task.first_seen_date,
            Task.last_seen_date,
            ReportItem.item_type,
            ReportItem.mentioned_people,
            WeeklyReport.id,
            WeeklyReport.employee_id,
            Department.name,
        )
        .select_from(Task)
        .join(TaskReportLink, TaskReportLink.task_id == Task.id)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .join(Department, Department.id == Employee.department_id, isouter=True)
        .where(non_exempt_report_condition(session))
    )
    if employee_id is not None:
        query = query.where(WeeklyReport.employee_id == employee_id)
    if department_id is not None:
        query = query.where(Employee.department_id == department_id)
    return [
        (
            TaskFact(task_id, title, status, duration_weeks, first_seen, last_seen),
            ItemFact(item_type, mentioned_people),
            ReportFact(report_id),
            EmployeeFact(row_employee_id) if row_employee_id is not None else None,
            DepartmentFact(department_name) if department_name is not None else None,
        )
        for (
            task_id,
            title,
            status,
            duration_weeks,
            first_seen,
            last_seen,
            item_type,
            mentioned_people,
            report_id,
            row_employee_id,
            department_name,
        ) in session.execute(query)
    ]


def trend(session: Session, end: date, weeks: int, *, employee_id: int | None = None, department_id: int | None = None) -> list[dict]:
    start, _ = trailing_period(end, weeks)
    rows = _item_rows(session, start, end, employee_id=employee_id, department_id=department_id)
    grouped: dict[date, list] = defaultdict(list)
    for row in rows:
        report_start = row[1].start_date
        if report_start:
            grouped[report_start].append(row)
    scores_query = (
        select(WeeklyReport.start_date, ReportScore.total_score)
        .join(ReportScore, ReportScore.report_id == WeeklyReport.id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        )
    )
    if employee_id is not None:
        scores_query = scores_query.where(WeeklyReport.employee_id == employee_id)
    if department_id is not None:
        scores_query = scores_query.where(Employee.department_id == department_id)
    scores: dict[date, list[int]] = defaultdict(list)
    for period_start, score in session.execute(scores_query):
        if period_start:
            scores[period_start].append(int(score))
    result = []
    cursor = start
    while cursor <= end:
        bucket = grouped.get(cursor, [])
        counts = item_counts(bucket)
        values = scores.get(cursor, [])
        result.append({
            "period_start": cursor.isoformat(),
            "completed": counts["completed"], "ongoing": counts["ongoing"],
            "blockers": counts["blockers"],
            "long_blockers": 0,
            "quality_score": round(sum(values) / len(values), 1) if values else None,
            "analyzed_reports": len({row[1].id for row in bucket}),
        })
        cursor += timedelta(days=7)
    # 长期卡点按最后出现周期计入对应周，避免每周重复累计同一张卡点。
    eligible_task_ids = (
        select(TaskReportLink.task_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .where(non_exempt_report_condition(session))
    )
    blocker_query = select(Blocker).where(
        Blocker.duration_weeks >= 3,
        Blocker.task_id.in_(eligible_task_ids),
    )
    if employee_id is not None or department_id is not None:
        relevant_task_ids = {row[0].id for row in task_rows(session, employee_id=employee_id, department_id=department_id)}
        if not relevant_task_ids:
            return result
        blocker_query = blocker_query.where(Blocker.task_id.in_(relevant_task_ids))
    blockers = session.scalars(blocker_query).all()
    for blocker in blockers:
        if blocker.last_seen_date:
            key = blocker.last_seen_date - timedelta(days=blocker.last_seen_date.weekday())
            for point in result:
                if point["period_start"] == key.isoformat():
                    point["long_blockers"] += 1
    return result


def _source_overlaps_period(source: dict, start: date, end: date) -> bool:
    """判断一个来源快照是否与周期重叠。缺少周期时保守地视为可用。"""
    source_start = (
        date.fromisoformat(source["period_start"])
        if source.get("period_start")
        else None
    )
    source_end = (
        date.fromisoformat(source["period_end"])
        if source.get("period_end")
        else None
    )
    return not (
        source_end is not None
        and source_end < start
        or source_start is not None
        and source_start > end
    )


def _current_blocker_projection(
    session: Session, start: date, end: date, *, limit: int | None = 5
) -> list[dict]:
    """序列化当前周期开放卡点，并只保留周期内的周报来源。

    ``limit`` 只在返回时截断：完整投影按 (start, end) 在一次请求内算一遍，
    因为同一次请求里的两个消费者传的 limit 不同（需要处理的候选要全部，
    首页行动区只要 5 条），要的却是同一份投影。
    """
    full = request_memo.memoize(
        session,
        ("current_blocker_projection", start, end),
        lambda: _build_current_blocker_projection(session, start, end),
    )
    return full if limit is None else full[:limit]


def _build_current_blocker_projection(
    session: Session, start: date, end: date
) -> list[dict]:
    current_blocker_ids = _active_blocker_ids(session, start, end)
    if not current_blocker_ids:
        return []
    current_blocker_rows = list(
        session.execute(
            select(Blocker, Task)
            .join(Task, Task.id == Blocker.task_id)
            .where(Blocker.id.in_(current_blocker_ids))
        )
    )
    risk_rank = {"高": 3, "中": 2, "低": 1}
    current_blocker_rows.sort(
        key=lambda row: (
            risk_rank.get(row[0].risk_level, 0),
            row[0].duration_weeks or 0,
            row[0].last_seen_date or date.min,
        ),
        reverse=True,
    )
    source_map = task_sources(session, [task.id for _, task in current_blocker_rows])
    result: list[dict] = []
    for blocker, task in current_blocker_rows:
        sources = [
            source
            for source in source_map.get(task.id, [])
            if _source_overlaps_period(source, start, end)
        ]
        # A blocker without a traceable source is not actionable and should not be
        # promoted into the dashboard's management action area.
        if not sources:
            continue
        result.append(
            {
                "id": blocker.id,
                "task_id": task.id,
                "title": blocker.title,
                "description": blocker.description,
                "risk_level": blocker.risk_level,
                "duration_weeks": blocker.duration_weeks,
                "occurrence_count": blocker.occurrence_count,
                "sources": sources,
            }
        )
    return result


def management_action_items(
    session: Session,
    start: date,
    end: date,
    *,
    limit: int = 5,
    insight_rows: list[dict] | None = None,
) -> list[dict]:
    """为首页生成可回看的管理行动候选。

    卡点优先，其次是有本周期来源的规则化洞察。所有条目都带来源数量和
    跳转地址；没有当前周期证据的历史洞察不会进入首页行动区。

    ``limit`` 只在返回时截断：完整的候选清单按 (start, end) 在一次请求内构建一遍。
    同一次请求里它被「关键发现」「需要你处理」「总览信号」与跟进入口校验各调一次，
    传的 limit 从 5 到 100 不等，而每次都要重新序列化全部开放卡点并逐个匹配来源。
    ``insight_rows`` 显式传入时按老路子直接算 —— 那是调用方已经算好、不想再查的路径。
    """
    if limit <= 0:
        return []
    if insight_rows is not None:
        return _build_action_items(session, start, end, insight_rows=insight_rows)[:limit]
    return request_memo.memoize(
        session,
        ("management_action_items", start, end),
        lambda: _build_action_items(session, start, end, insight_rows=None),
    )[:limit]


def _build_action_items(
    session: Session,
    start: date,
    end: date,
    *,
    insight_rows: list[dict] | None = None,
) -> list[dict]:
    """候选清单的完整构建（不按 limit 截断）。

    洞察候选取全量：候选越多只会让清单更长，不会改变前面已排定的顺序
    （卡点先入、洞察按原顺序补位），所以按 limit 截断与旧实现逐 limit 计算一致。
    """
    actions: list[dict] = []
    current_blockers = _current_blocker_projection(session, start, end, limit=None)
    blocker_titles = {item["title"] for item in current_blockers}
    for blocker in current_blockers:
        sources = blocker["sources"]
        departments = sorted(
            {
                source["department_name"]
                for source in sources
                if source.get("department_name")
            }
        )
        source_count = len({source["report_id"] for source in sources})
        summary = blocker["description"] or (
            f"已持续 {blocker['duration_weeks']} 周，最近有 {source_count} 篇周报记录。"
        )
        actions.append(
            {
                "kind": "blocker",
                "title": blocker["title"],
                "summary": summary,
                "risk_level": blocker["risk_level"],
                "duration_weeks": blocker["duration_weeks"],
                "departments": departments,
                "source_count": source_count,
                "target_href": f"/blockers/{blocker['id']}",
                "source_type": "blocker",
                "source_id": blocker["id"],
                "source_task_id": blocker["task_id"],
                "source_key": f"blocker:{blocker['id']}",
                "source_report_ids": sorted({source["report_id"] for source in sources}),
                "sources": sources,
            }
        )

    # ``insights`` already applies source validation. Re-check the selected period
    # here because some insight kinds intentionally inspect a longer history.
    selected_insights = (
        insight_rows
        if insight_rows is not None
        else insights(session, start, end, limit=None)
    )
    for insight in selected_insights:
        sources = [
            source
            for source in insight.get("sources", [])
            if _source_overlaps_period(source, start, end)
        ]
        if not sources:
            continue
        title = insight.get("title") or "管理事项"
        # Long-blocker insights are already represented by the actionable blocker
        # above; retain other insight kinds without duplicating that card.
        if insight.get("kind") == "long_blocker" and any(
            title.endswith(blocker_title) for blocker_title in blocker_titles
        ):
            continue
        if any(action["title"] == title for action in actions):
            continue
        report_id = sources[0].get("report_id")
        actions.append(
            {
                "kind": insight.get("kind") or "insight",
                "title": title,
                "summary": insight.get("conclusion") or insight.get("reason") or "",
                "risk_level": insight.get("risk_level") or "低",
                "duration_weeks": int(insight.get("duration_weeks") or 0),
                "departments": sorted(
                    {
                        source["department_name"]
                        for source in sources
                        if source.get("department_name")
                    }
                ),
                "source_count": len({source["report_id"] for source in sources}),
                "target_href": f"/reports/{report_id}" if report_id else "/insights",
                "source_type": "insight",
                "source_id": None,
                "source_task_id": None,
                "source_key": insight_source_key(
                    insight.get("kind") or "insight", title, start, end
                ),
                "source_report_ids": sorted({source["report_id"] for source in sources}),
                "sources": sources,
            }
        )

    risk_rank = {"高": 3, "中": 2, "低": 1}
    actions.sort(
        key=lambda action: (
            risk_rank.get(action["risk_level"], 0),
            action["duration_weeks"],
            action["source_count"],
        ),
        reverse=True,
    )
    return actions


def dashboard_summary(session: Session, start: date, end: date) -> dict:
    rows = _item_rows(session, start, end)
    counts = item_counts(rows)
    all_insight_rows = insights(session, start, end, limit=None)
    insight_rows = all_insight_rows[:20]
    current_blocker_ids = _active_blocker_ids(session, start, end)
    action_rows = _build_action_items(
        session, start, end, insight_rows=insight_rows
    )
    # 纳入跟进后，事项在管理跟进区承接；洞察卡只显示尚未接手的信号。
    # 用稳定来源键而非标题匹配，避免同名卡点误伤。终态也保留跟进记录，
    # 不应在下一次刷新时重新变成首页待处理信号。
    def insight_key(insight: dict) -> str:
        return (
            f"blocker:{insight['blocker_id']}"
            if insight.get("kind") == "long_blocker" and insight.get("blocker_id") is not None
            else insight_source_key(insight["kind"], insight["title"], start, end)
        )

    candidate_keys = {
        insight_key(insight) for insight in all_insight_rows
        if insight.get("kind") in HOME_INSIGHT_KINDS
    } | {
        action["source_key"] for action in action_rows
    }
    tracked_keys = set(session.scalars(
        select(ManagementAction.source_key).where(
            ManagementAction.source_key.in_(candidate_keys)
        )
    )) if candidate_keys else set()
    # 先按本周期证据筛选，再截首页数量；历史卡点不能占满前 20 条候选。
    home_insight_rows = [
        insight for insight in all_insight_rows
        if insight.get("kind") in HOME_INSIGHT_KINDS
        and insight_key(insight) not in tracked_keys
        and (
            insight.get("blocker_id") in current_blocker_ids
            if insight.get("kind") == "long_blocker"
            else any(
                _source_overlaps_period(source, start, end)
                for source in insight.get("sources", [])
            )
        )
    ]
    section_types = {
        "work_summary": DONE_TYPES,
        "focus_work": ONGOING_TYPES,
        "needs": BLOCKER_TYPES,
        "next_plans": {"next_week_plans"},
    }

    def section_items(section_rows: list[tuple]) -> list[dict]:
        return [
            {
                "title": row[0].title,
                "description": row[0].description,
                "status": row[0].status,
                "related_project": row[0].related_project,
                "sources": source_for_items([row]),
            }
            for row in section_rows[:5]
        ]

    sections: dict[str, dict] = {}
    for key, types in section_types.items():
        section_rows = [row for row in rows if row[0].item_type in types]
        sections[key] = {"count": len(section_rows), "items": section_items(section_rows)}

    completed = [row for row in rows if row[0].item_type in DONE_TYPES]
    top_items = [
        {
            "title": row[0].title, "description": row[0].description,
            "related_project": row[0].related_project, "item_type": row[0].item_type,
            "sources": source_for_items([row]),
        }
        for row in completed[:5]
    ]
    # 首页只投影本周期周报中出现的开放卡点；历史卡点仍可在卡点中心查看。
    current_blockers = _current_blocker_projection(session, start, end, limit=5)
    return {
        "coverage": analysis_coverage(session, start, end),
        "counts": counts,
        "top_items": top_items,
        "sections": sections,
        "current_blockers": current_blockers,
        "action_items": [
            action for action in action_rows
            if action["source_key"] not in tracked_keys
        ][:5],
        "period_comparison": period_comparison(session, start, end),
        "insights": home_insight_rows[:5],
        "trend": trend(session, end, 8),
    }


def insights(session: Session, start: date, end: date, *, limit: int | None = 5) -> list[dict]:
    """生成确定性洞察。每条在返回前都校验至少一个来源。

    **完整计算按 (start, end) 在一次请求内只做一遍，``limit`` 只在返回时截断**：
    同一次请求里有三个消费者（首页概览、经营看板的「关键发现」与「需要你处理」候选），
    它们传的 limit 各不相同，要的却是同一份计算。这份计算最贵的一步是扫描全部任务
    关联（开发库 4304 条实测 585 ms）—— 重复算三遍就是页面上看到的数秒等待。
    缓存完整列表后 ``limit`` 之间只差一次切片，与旧实现（``result[:limit]``）结果一致。
    """
    return _all_insights(session, start, end)[:limit]


def _all_insights(session: Session, start: date, end: date) -> list[dict]:
    return request_memo.memoize(
        session, ("insights", start, end), lambda: _build_insights(session, start, end)
    )


def _build_insights(session: Session, start: date, end: date) -> list[dict]:
    """``insights`` 的完整（未按 limit 截断）计算。只读，不写任何表。"""
    result: list[dict] = []
    blockers = list(session.execute(
        select(Blocker, Task).join(Task).where(
            Blocker.status == "OPEN", Blocker.duration_weeks >= 3
        ).order_by(Blocker.duration_weeks.desc(), Blocker.risk_level.desc())
    ))
    source_map = task_sources(session, [task.id for _, task in blockers])
    for blocker, task in blockers:
        sources = source_map.get(task.id, [])
        if sources:
            result.append({
                "kind": "long_blocker", "title": f"长期未解决：{blocker.title}",
                "blocker_id": blocker.id,
                "conclusion": f"该问题已持续 {blocker.duration_weeks} 周，最近仍有记录，建议确认负责人和处理节点。",
                "risk_level": blocker.risk_level, "evidence_count": blocker.occurrence_count,
                "duration_weeks": blocker.duration_weeks,
                "departments": sorted({s["department_name"] for s in sources if s["department_name"]}),
                "employees": sorted({s["employee_name"] for s in sources if s["employee_name"]}),
                "period": {"start": blocker.first_seen_date.isoformat() if blocker.first_seen_date else None, "end": blocker.last_seen_date.isoformat() if blocker.last_seen_date else None},
                "reason": "连续多个周报周期出现，且尚未标记完成。", "sources": sources,
            })

    previous_start, previous_end = period_before(start)
    current_rows = _item_rows(session, start, end)
    previous_rows = _item_rows(session, previous_start, previous_end)
    current_count = item_counts(current_rows)["blockers"]
    previous_count = item_counts(previous_rows)["blockers"]
    if current_count >= 2 and current_count > previous_count * 1.5:
        sources = source_for_items([r for r in current_rows if r[0].item_type in BLOCKER_TYPES])
        if sources:
                result.append({
                    "kind": "blocker_growth", "title": "本期问题事项明显增加",
                    "conclusion": f"本期识别到 {current_count} 条问题/依赖/风险事项，前 4 周同口径为 {previous_count} 条。",
                    "risk_level": "中", "evidence_count": current_count, "duration_weeks": 0, "departments": sorted({s["department_name"] for s in sources if s["department_name"]}),
                "employees": sorted({s["employee_name"] for s in sources if s["employee_name"]}),
                "period": {"start": start.isoformat(), "end": end.isoformat()}, "reason": "与前序四周相比，问题类事项数量上升。", "sources": sources,
            })

    linked = task_rows(session)
    by_task: dict[int, list] = defaultdict(list)
    for row in linked:
        by_task[row[0].id].append(row)

    # 一次把所有可能用到的来源查出来（task_sources 本来就支持批量），
    # 而不是在每个任务里各查一次 —— 后者在任务多时是典型的 N+1：
    # 首页每次渲染都会把所有任务的来源逐条查一遍。
    candidate_task_ids: set[int] = set()
    for task_id, rows in by_task.items():
        depts = {row[4].name for row in rows if row[4]}
        people = {row[3].id for row in rows if row[3]}
        explicit_people = any(row[1].mentioned_people for row in rows)
        task = rows[0][0]
        plan_rows = [row for row in rows if row[1].item_type == "next_week_plans"]
        if (
            len(depts) >= 2
            or (len(people) >= 2 and not explicit_people)
            or (
                task.status != "COMPLETED"
                and task.duration_weeks >= 3
                and len({r[2].id for r in plan_rows}) >= 3
            )
        ):
            candidate_task_ids.add(task_id)
    sources_by_task = task_sources(session, candidate_task_ids)

    for task_id, rows in by_task.items():
        depts = {row[4].name for row in rows if row[4]}
        if len(depts) >= 2:
            task = rows[0][0]
            sources = sources_by_task.get(task_id, [])
            if sources:
                result.append({
                    "kind": "cross_department", "title": f"跨部门共同事项：{task.title}",
                    "conclusion": f"该事项在 {len(depts)} 个部门的周报中出现，建议统一确认协作接口。",
                    "risk_level": "中", "evidence_count": len({row[2].id for row in rows}), "duration_weeks": task.duration_weeks,
                    "departments": sorted(depts), "employees": sorted({s["employee_name"] for s in sources if s["employee_name"]}),
                    "period": {"start": task.first_seen_date.isoformat() if task.first_seen_date else None, "end": task.last_seen_date.isoformat() if task.last_seen_date else None},
                    "reason": "同一结构化任务关联了多个部门。", "sources": sources,
                })

        # 只有多人记录、模型也没有抽出提及负责人时，才提示“未识别明确负责人”。
        # 这是数据缺口提示，不把“未提及姓名”武断地解释为无人负责。
        people = {row[3].id for row in rows if row[3]}
        explicit_people = any(row[1].mentioned_people for row in rows)
        if len(people) >= 2 and not explicit_people:
            task = rows[0][0]
            sources = sources_by_task.get(task_id, [])
            if sources:
                result.append({
                    "kind": "owner_missing", "title": f"未识别明确负责人：{task.title}",
                    "conclusion": "多人周报关联到同一事项，但结构化结果未提取到明确负责人的提及信息，建议人工确认。",
                    "risk_level": "低", "evidence_count": len({row[2].id for row in rows}), "duration_weeks": task.duration_weeks,
                    "departments": sorted(depts), "employees": sorted({s["employee_name"] for s in sources if s["employee_name"]}),
                    "period": {"start": task.first_seen_date.isoformat() if task.first_seen_date else None, "end": task.last_seen_date.isoformat() if task.last_seen_date else None},
                    "reason": "多人关联且没有显式提及负责人；这不是“无人负责”的判定。", "sources": sources,
                })

    # 长期计划未推进：只取仍非完成状态、且至少三周出现过下周计划的任务。
    for task_id, rows in by_task.items():
        task = rows[0][0]
        plan_rows = [row for row in rows if row[1].item_type == "next_week_plans"]
        if task.status != "COMPLETED" and task.duration_weeks >= 3 and len({r[2].id for r in plan_rows}) >= 3:
            sources = sources_by_task.get(task_id, [])
            if sources:
                result.append({
                    "kind": "stalled_plan", "title": f"计划连续出现未见完成：{task.title}",
                    "conclusion": f"该计划已连续记录 {task.duration_weeks} 周，尚未关联到明确完成事项。",
                    "risk_level": "中", "evidence_count": len({r[2].id for r in plan_rows}), "duration_weeks": task.duration_weeks,
                    "departments": sorted({s["department_name"] for s in sources if s["department_name"]}),
                    "employees": sorted({s["employee_name"] for s in sources if s["employee_name"]}),
                    "period": {"start": task.first_seen_date.isoformat() if task.first_seen_date else None, "end": task.last_seen_date.isoformat() if task.last_seen_date else None},
                    "reason": "同一任务持续以“下周计划”记录，且未标记完成。", "sources": sources,
                })

    # 员工状态变化：只比较结构化事项数量，且保持中性陈述，不推断个人原因。
    current_by_employee: dict[int, list] = defaultdict(list)
    previous_by_employee: dict[int, list] = defaultdict(list)
    for row in current_rows:
        if row[2]:
            current_by_employee[row[2].id].append(row)
    for row in previous_rows:
        if row[2]:
            previous_by_employee[row[2].id].append(row)
    for employee_id, prior_rows in previous_by_employee.items():
        now_rows = current_by_employee.get(employee_id, [])
        prior_completed = item_counts(prior_rows)["completed"]
        now_completed = item_counts(now_rows)["completed"]
        if prior_completed >= 4 and 0 < now_completed <= prior_completed * 0.4:
            employee = now_rows[0][2]
            sources = source_for_items(now_rows)
            if employee and sources:
                result.append({
                    "kind": "employee_change", "title": f"工作事项数量出现变化：{employee.name}",
                    "conclusion": f"本期识别到 {now_completed} 条完成/成果事项，前 4 周为 {prior_completed} 条。建议结合实际项目周期关注。",
                    "risk_level": "低", "evidence_count": len({r[1].id for r in now_rows}), "duration_weeks": 0,
                    "departments": [sources[0]["department_name"]] if sources[0]["department_name"] else [], "employees": [employee.name],
                    "period": {"start": start.isoformat(), "end": end.isoformat()},
                    "reason": "结构化事项数量较前序周期有明显变化，不对原因作推断。", "sources": sources,
                })
    # 若实际没有可追溯洞察，返回空而非制造结论。
    return result


def employee_analysis(session: Session, employee_id: int, end: date, weeks: int) -> dict:
    start, _ = trailing_period(end, weeks)
    rows = _item_rows(session, start, end, employee_id=employee_id)
    tasks = task_rows(session, employee_id=employee_id)
    current_blockers = [r for r in tasks if r[1].item_type in BLOCKER_TYPES and r[0].status != "COMPLETED"]
    long_tasks = [r for r in tasks if r[0].duration_weeks >= 3 and r[0].status != "COMPLETED"]
    # 批量取来源：序列化的任务最多各 10 条，逐条查就是几十次往返。
    serialized_blockers = list({r[0].id: r for r in current_blockers}.values())[:10]
    serialized_long = list({r[0].id: r for r in long_tasks}.values())[:10]
    sources_by_task = task_sources(
        session, {r[0].id for r in (*serialized_blockers, *serialized_long)}
    )

    def serialize_task(row: tuple) -> dict:
        task = row[0]
        return {"task_id": task.id, "title": task.title, "status": task.status, "duration_weeks": task.duration_weeks, "sources": sources_by_task.get(task.id, [])}
    completed = item_counts(rows)["completed"]
    ongoing = item_counts(rows)["ongoing"]
    summary = f"近 {weeks} 周已识别 {completed} 条完成/成果事项、{ongoing} 条进行中事项。"
    if current_blockers:
        summary += f" 当前有 {len({r[0].id for r in current_blockers})} 个带问题、依赖或风险证据的事项需要关注。"
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "coverage": analysis_coverage(session, start, end, employee_id=employee_id),
        "trend": trend(session, end, weeks, employee_id=employee_id),
        "summary": summary,
        "current_blockers": [serialize_task(r) for r in serialized_blockers],
        "long_tasks": [serialize_task(r) for r in serialized_long],
    }


def _department_item_counts(by_type: dict[str, int]) -> dict:
    """按 item_type 的计数还原成 ``item_counts`` 的同一形状。

    看板与部门页都读 ``counts.total / completed / ongoing``；``by_type`` 也保留，
    否则结构上与逐部门路径不一致（调用方会拿到 KeyError）。
    """
    return {
        "total": sum(by_type.values()),
        "completed": sum(by_type.get(key, 0) for key in DONE_TYPES),
        "ongoing": sum(by_type.get(key, 0) for key in ONGOING_TYPES),
        "blockers": sum(by_type.get(key, 0) for key in BLOCKER_TYPES),
        "by_type": dict(by_type),
    }


def department_analysis_batch(
    session: Session, department_ids: Iterable[int], end: date, weeks: int = 1
) -> dict[int, dict]:
    """一次算出多个部门的分析结论（不含 trend）。

    逐部门调用 ``department_analysis`` 时，每个部门都要跑
    「事项行 + 覆盖率 + 卡点 + 来源」四组查询，N 个部门就是 4N 条 SQL 且每条都把
    ``ReportItem + WeeklyReport + Employee + Department`` 四个实体实例化一遍
    （实测 13 个部门 267 条 SQL，耗时几乎全在 ORM 组装而非 SQL 本身）。
    这里把前两组改成按部门 GROUP BY，卡点一次取回后按部门分桶，
    来源走一次批量查询 —— 查询数与部门数脱钩。

    返回结构与 ``department_analysis`` 逐部门路径完全一致（``trend`` 除外，
    因为它天然是单部门序列，由调用方按需补）。
    """
    start, _ = trailing_period(end, weeks)
    ids = sorted({int(i) for i in department_ids})
    if not ids:
        return {}

    type_rows = session.execute(
        select(
            Employee.department_id,
            ReportItem.item_type,
            func.count(ReportItem.id),
        )
        .select_from(ReportItem)
        .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id)
        .where(
            Employee.department_id.in_(ids),
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        )
        .group_by(Employee.department_id, ReportItem.item_type)
    )
    by_type: dict[int, dict[str, int]] = defaultdict(dict)
    for dept_id, item_type, count in type_rows:
        by_type[dept_id][item_type] = int(count)

    coverages = analysis_coverage_batch(session, ids, start, end)

    blocker_rows = session.execute(
        select(Employee.department_id, Blocker, Task)
        .select_from(Blocker)
        .join(Task, Task.id == Blocker.task_id)
        .join(TaskReportLink, TaskReportLink.task_id == Task.id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id)
        .where(
            Employee.department_id.in_(ids),
            Blocker.status == "OPEN",
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        )
    )
    # 同一任务可能被多篇周报引用：按部门内 task.id 去重，与逐部门路径一致。
    task_maps: dict[int, dict[int, tuple]] = defaultdict(dict)
    for dept_id, blocker, task in blocker_rows:
        task_maps[dept_id][task.id] = (blocker, task)
    # 来源一次批量取回：task_sources 本就按 task_id 分组，结果与逐部门调用相同。
    all_task_ids = {task_id for mapping in task_maps.values() for task_id in mapping}
    sources_by_task = task_sources(session, all_task_ids)

    period = {"start": start.isoformat(), "end": end.isoformat()}
    result: dict[int, dict] = {}
    for dept_id in ids:
        counts = _department_item_counts(by_type.get(dept_id, {}))
        coverage = coverages.get(
            dept_id, {"reports_total": 0, "analyzed_reports": 0, "rate": 0}
        )
        blockers = [
            {
                "id": blocker.id,
                "title": blocker.title,
                "risk_level": blocker.risk_level,
                "duration_weeks": blocker.duration_weeks,
                "sources": sources_by_task.get(task.id, []),
            }
            for blocker, task in task_maps.get(dept_id, {}).values()
        ]
        status = (
            "正常"
            if not blockers
            else (
                "注意"
                if any(
                    b["risk_level"] == "高" or b["duration_weeks"] >= 3 for b in blockers
                )
                else "关注"
            )
        )
        result[dept_id] = {
            "period": period,
            "coverage": coverage,
            "counts": counts,
            "status": status,
            "blockers": blockers[:10],
            "summary": (
                f"本周期已分析 {coverage['analyzed_reports']} 篇周报，"
                f"识别 {counts['completed']} 条完成/成果事项和 {len(blockers)} 个当前卡点。"
            ),
        }
    return result


def department_analysis(session: Session, department_id: int, end: date, weeks: int) -> dict:
    """单个部门的分析结论（部门详情页用）。

    走 ``department_analysis_batch`` 的同一套实现，保证两处口径不会各算一套；
    差异只有 ``trend`` —— 它是单部门的时间序列，天然只在这个路径下需要。
    """
    batch = department_analysis_batch(session, [department_id], end, weeks)
    analysis = batch.get(
        department_id,
        {
            "period": {
                "start": trailing_period(end, weeks)[0].isoformat(),
                "end": end.isoformat(),
            },
            "coverage": {"reports_total": 0, "analyzed_reports": 0, "rate": 0},
            "counts": _department_item_counts({}),
            "status": "正常",
            "blockers": [],
            "summary": "本周期已分析 0 篇周报，识别 0 条完成/成果事项和 0 个当前卡点。",
        },
    )
    return {
        **analysis,
        "trend": trend(session, end, weeks, department_id=department_id),
    }
