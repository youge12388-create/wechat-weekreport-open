"""经营看板的数据组装（只读）。

设计纪律（对应方案文档第 4~7 节）：

* **主题与成果必须靠 ``Task`` 关联**，不按数组下标配对、不按标题字符串相等归并。
  ``TaskReportLink`` 是模型给出的 ``task_key`` 经规范化键聚合的结果，同一条任务
  跨周换说法也能归到一起；下标配对只会把「第 N 条重点」和「第 N 条成果」凑成一对，
  那是在制造原文里不存在的关系。
* **跨月延续只认 task_id**：同一个 Task 出现在多个月份才算同一主线在延续。
  标题相似不算 —— 两个部门各自说「优化流程」并不表示同一件事。
* **持续问题要求跨 ≥2 个自然月有证据**，单月出现不叫持续。
* 本模块**不做员工/部门排名、不生成完成百分比**；所有计数只表达信息覆盖。
* 口径与现有接口保持一致：复用 ``insight_service`` 的卡点/洞察/覆盖率计算，
  不在新路由里重写业务判断（避免同一事实两个页面两种说法）。
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import BlockerStatus, OperatingSummaryType, TaskStatus
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.processors.journal_parser import is_empty_resource_demand
from app.services import request_memo
from app.services.insight_service import (
    _source_overlaps_period,
    analysis_coverage,
    insights,
    management_action_items,
    resource_demand_candidate_condition,
    task_report_counts,
    task_sources,
)
from app.services.structured_data_service import (
    BLOCKER_ITEM_TYPES,
    RESOURCE_DEMAND_TASK_PREFIX,
)
from app.services.view_service import (
    non_exempt_report_condition,
    report_period_overlap_conditions,
    reportable_employee_ids,
    submitted_employee_ids,
)

# 主题最多返回多少条。超过后页面读不动，且末尾的都是只有一篇周报提及的碎项。
THEME_LIMIT = 8
# 每条主题最多带几条成果 / 依据，进提示词前也要收敛，避免提示词体积失控。
OUTCOME_PER_THEME_LIMIT = 3
EVIDENCE_PER_THEME_LIMIT = 3
EVIDENCE_SNIPPET_LIMIT = 500

# 关键发现的中文类型名。前端只渲染后端算好的结论，不自己映射。
DISCOVERY_KIND_LABELS = {
    "progress": "进展",
    "blocker": "卡点",
    "anomaly": "异常",
    "collaboration": "协作",
}

# insight.kind → 关键发现类型。insight_service 的判断保持不变，这里只是换标签。
_INSIGHT_DISCOVERY_KIND = {
    "blocker_growth": "anomaly",
    "long_blocker": "blocker",
    "cross_department": "collaboration",
    "stalled_plan": "blocker",
    "owner_missing": "collaboration",
    "employee_change": "anomaly",
}


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _iter_months(start: date, end: date) -> list[tuple[date, date, str]]:
    """把区间按自然月切分；首尾不完整的月份取实际起止日期。

    先按月切分再统计，不能把整段区间汇总后凭空拆成月份（方案 5.2）。
    """
    months: list[tuple[date, date, str]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        next_month = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
        months.append((max(cursor, start), min(next_month - timedelta(days=1), end), _month_key(cursor)))
        cursor = next_month
    return months


def _theme_rows(
    session: Session, start: date, end: date, *, department_id: int | None = None
) -> list[tuple[Task, ReportItem, WeeklyReport, Employee | None, Department | None]]:
    """本期出现过的主题（Task）及其事项行。

    一条 Task 可能命中多行（多篇周报、多个事项类型），调用方按 task_id 聚合。

    **资源需求事项被排除，但只排除那一条、不排除整条任务**：它不是「公司在推进的
    工作」，而是「需要某种支持」的陈述，放进主要工作方向会得到「资源需求：Discuss
    later」这种读不出信息的主线。判定按 ``ReportItem.task_key`` 的
    ``resource_demand:`` 前缀 —— 与 ``agent_tools.list_tasks`` 同一口径。
    **不能改判 ``Task.canonical_key``**：它经过 ``_canonical_key`` 已经把冒号和下划线
    剥掉，前缀匹配会恒真、等于没写。

    这里原来排除的是**整个任务**（``~Task.id.in_(resource_demand_task_ids)``），
    于是模型把一条资源需求挂到某个已有任务上时，那整条任务的真实事项一起消失。
    实测当前周期有 8 个这样的任务、共 18 条真实工作（其中一条任务下有 7 条）在
    「公司精力投向」「重点项目进展」「工作主线」里全都看不见 —— 少算比多算更难发现，
    因为少掉的内容不会在页面上留下痕迹。

    资源需求仍会以卡点形式出现在关键发现与需要处理里。
    """
    conditions = [
        *report_period_overlap_conditions(start, end),
        non_exempt_report_condition(session),
    ]
    if department_id is not None:
        conditions.append(Employee.department_id == department_id)

    def _load() -> list[Any]:
        return list(
            session.execute(
                select(Task, ReportItem, WeeklyReport, Employee, Department)
                .select_from(Task)
                .join(TaskReportLink, TaskReportLink.task_id == Task.id)
                .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
                .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
                .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
                .join(Department, Department.id == Employee.department_id, isouter=True)
                .where(
                    *conditions,
                    # SQL 三值逻辑：``task_key`` 为 NULL 时 ``NOT LIKE`` 结果是 NULL
                    # 而不是 TRUE，会把没有 task_key 的事项整行吃掉（实测丢掉 17 行 /
                    # 3 个参与人次）。必须把 NULL 显式放行 —— 与 view_service 里
                    # ``NOT IN`` 要单独 or 出 ``department_id IS NULL`` 是同一个坑。
                    or_(
                        ReportItem.task_key.is_(None),
                        ReportItem.task_key.not_like(f"{RESOURCE_DEMAND_TASK_PREFIX}%"),
                    ),
                )
                .order_by(Task.id, WeeklyReport.end_date, ReportItem.id)
            )
        )

    return _memo(session, ("theme_rows", start, end, department_id), _load)


def _is_completed_item(item: ReportItem) -> bool:
    # 口径必须与 ``_project_progress``（"已完成" in status）一致：status 是模型自由文本
    # （prompts.py 的枚举带「等」），「未完成」「待完成」都含「完成」二字 ——
    # 用子串「完成」判断会把明确没做完的事虚报成成果。
    return item.item_type in {"completed_items", "results"} or "已完成" in (item.status or "")


def _themes(
    rows: Iterable[tuple[Task, ReportItem, WeeklyReport, Employee | None, Department | None]],
    *,
    limit: int = THEME_LIMIT,
) -> list[dict[str, Any]]:
    """把事项行聚合成主题列表。

    人数与篇数只表达信息覆盖（多少人/多少篇周报提到这件事），不是工时也不是贡献。

    每条主题带 ``is_company_level``：**只有被多人或多篇确认过的才是「公司级进展」**，
    否则只是某个人的待办。实测开发库 318 条主题里 317 条只有 1 人 1 篇 ——
    不区分地把它们并排列出，「上周进展」实际是在罗列个人事项，而不是公司主线。
    """
    grouped: dict[int, dict[str, Any]] = {}
    for task, item, report, employee, department in rows:
        bucket = grouped.setdefault(
            task.id,
            {
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
                "duration_weeks": task.duration_weeks,
                "related_project": task.related_project,
                "first_seen": task.first_seen_date.isoformat() if task.first_seen_date else None,
                "last_seen": task.last_seen_date.isoformat() if task.last_seen_date else None,
                "_report_ids": set(),
                "_employees": set(),
                "_departments": set(),
                "_outcomes": [],
                "_evidence": [],
            },
        )
        bucket["_report_ids"].add(report.id)
        if employee is not None:
            bucket["_employees"].add(employee.name)
        if department is not None:
            bucket["_departments"].add(department.name)

        evidence = {
            "report_id": report.id,
            "employee_name": employee.name if employee else None,
            "department_name": department.name if department else None,
            "item_type": item.item_type,
            "source_text": (item.source_text or "")[:EVIDENCE_SNIPPET_LIMIT],
            "occurred_on": report.end_date.isoformat() if report.end_date else None,
        }
        if _is_completed_item(item) and len(bucket["_outcomes"]) < OUTCOME_PER_THEME_LIMIT:
            bucket["_outcomes"].append(
                {
                    "title": item.title,
                    "description": item.description,
                    "status": item.status,
                    "report_id": report.id,
                    "occurred_on": report.end_date.isoformat() if report.end_date else None,
                }
            )
        if len(bucket["_evidence"]) < EVIDENCE_PER_THEME_LIMIT:
            bucket["_evidence"].append(evidence)

    themes: list[dict[str, Any]] = []
    for bucket in grouped.values():
        themes.append(
            {
                "task_id": bucket["task_id"],
                "title": bucket["title"],
                "status": bucket["status"],
                "duration_weeks": bucket["duration_weeks"],
                "related_project": bucket["related_project"],
                "first_seen": bucket["first_seen"],
                "last_seen": bucket["last_seen"],
                # people_count = 提到该主题的人数（信息覆盖，不是投入）
                "people_count": len(bucket["_employees"]),
                "report_count": len(bucket["_report_ids"]),
                "departments": sorted(bucket["_departments"]),
                "outcomes": bucket["_outcomes"],
                "evidence": bucket["_evidence"],
                "source_report_ids": sorted(bucket["_report_ids"]),
                # 有成果即「可展示成果关系」；没有则前端改用上下两列，不画流向线。
                "has_reliable_outcomes": bool(bucket["_outcomes"]),
                # 公司级 = 不止一个人、或者不止一篇提到。单人事只能算「个人事项」。
                # 用 OR 而不是 AND：两人各写一篇（2 人 2 篇）是协作，
                # 一人写了两篇（1 人 2 篇）说明这件事跨周持续 —— 两者都比单篇更可信。
                "is_company_level": (
                    len(bucket["_employees"]) >= 2 or len(bucket["_report_ids"]) >= 2
                ),
            }
        )
    # 排序按「覆盖人数 → 篇数 → 跨周时长」；它是信息量排序，不是重要性或绩效排序。
    themes.sort(
        key=lambda item: (
            -int(item["people_count"]),
            -int(item["report_count"]),
            -int(item["duration_weeks"] or 0),
            item["title"] or "",
        )
    )
    return themes[:limit]


def _trackable_index(
    session: Session,
    start: date,
    end: date,
    *,
    limit: int = 100,
    candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """可纳入跟进的后端候选清单。

    这正是 ``POST /api/management-actions`` 用来校验来源的那份候选
    （``management_action_items``）。前端据此决定「纳入跟进」是否可点，
    点下去时直接把 ``track`` 里的字段回传即可 —— 前端不自己拼 source_key，
    否则一旦服务端换了键格式，按钮就会变成必然 400 的死按钮。
    """
    candidates = (
        candidates
        if candidates is not None
        else management_action_items(session, start, end, limit=limit)
    )
    return [
        {
            "title": item.get("title") or "",
            "source_type": item.get("source_type"),
            "source_id": item.get("source_id"),
            "source_key": item.get("source_key"),
            "href": item.get("target_href"),
        }
        for item in candidates
    ]


def _match_trackable(title: str | None, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """把一条发现匹配到候选。

    先精确匹配标题；再匹配「候选标题是发现标题的后缀」—— ``insights()`` 会把
    长期卡点包装成「长期未解决：X」，而候选里的标题是不带前缀的 ``X``，
    两者指同一件事，不能让它因此丢掉纳入跟进的入口。
    """
    if not title:
        return None
    for candidate in candidates:
        if candidate["title"] == title and candidate["source_key"]:
            return candidate
    for candidate in candidates:
        if (
            candidate["source_key"]
            and candidate["title"]
            and title.endswith(candidate["title"])
        ):
            return candidate
    return None


def _match_signal_trackable(
    signal: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """只把能无歧义映射到现有卡点/洞察的信号开放为管理跟进来源。"""
    title = str(signal.get("title") or "")
    signal_type = signal.get("signal_type")

    if signal_type == "shared_blocker":
        # 共享瓶颈标题会附加影响工作数；其唯一可跟进来源是同名卡点。
        target = title.partition("影响 ")[0].strip()
        matches = [
            candidate
            for candidate in candidates
            if candidate.get("source_type") == "blocker"
            and candidate.get("title") == target
            and candidate.get("source_key")
        ]
        return matches[0] if len(matches) == 1 else None

    if signal_type == "stalled_workstream":
        # 停滞信号加了周期说明；下游候选使用卡点原名。
        target = title.partition("未解决：")[2].strip()
        matches = [
            candidate
            for candidate in candidates
            if candidate.get("source_type") == "blocker"
            and candidate.get("title") == target
            and candidate.get("source_key")
        ]
        return matches[0] if len(matches) == 1 else None

    # 其他信号沿用周报发现的既有精确/后缀匹配规则；不猜测相似标题。
    matches = [
        candidate
        for candidate in candidates
        if candidate.get("source_key")
        and candidate.get("title")
        and (
            candidate["title"] == title
            or title.endswith(candidate["title"])
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _discovery_item(
    *,
    kind: str,
    source_kind: str,
    title: str | None,
    conclusion: str | None,
    reason: str | None,
    risk_level: str | None,
    evidence_count: int | None,
    departments: list[str],
    employees: list[str],
    period: dict[str, Any] | None,
    sources: list[dict[str, Any]],
    duration_weeks: int | None = None,
    matched: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """关键发现列表里每一条的统一形状。

    两个来源（结构化进展、insight 归纳）都经这里构造，字段集只有一个定义 ——
    否则两处各拼一份，迟早出现「某类发现的 track 字段不存在」这种前端崩溃。
    """
    return {
        "kind": kind,
        "kind_label": DISCOVERY_KIND_LABELS[kind],
        "source_kind": source_kind,
        "title": title,
        "conclusion": conclusion,
        "reason": reason,
        "risk_level": risk_level,
        "evidence_count": evidence_count,
        "departments": departments,
        "employees": employees,
        "period": period,
        "duration_weeks": duration_weeks,
        "sources": sources,
        "source_key": matched["source_key"] if matched else None,
        # 只有能映射到真实 blocker/insight 的才允许「纳入跟进」；
        # 前端按 can_track 决定按钮可点性，并原样回传 track。
        "can_track": matched is not None,
        "track": (
            {
                "source_type": matched["source_type"],
                "source_id": matched["source_id"],
                "source_key": matched["source_key"],
                "href": matched["href"],
            }
            if matched
            else None
        ),
    }


def _discoveries(
    session: Session, start: date, end: date, *, limit: int = 12
) -> list[dict[str, Any]]:
    """关键发现：复用 insight_service 的判断，换成方案要求的四类型标签。

    「纳入跟进」的可点性由**后端**判定并给出完整来源字段（``track``），
    前端只渲染结论 —— 没有候选来源的发现不给可执行按钮。

    **「没有需求」类记录在这里也要剔掉**：`insights()` 会把长期开放卡点原样抬上来，
    而其中包含「资源需求：Discuss later」这类员工没填的记录。它们在最坏的情况下会排在
    第一条（实测「上周」视图首条就是它），而同一页的总览已经把它过滤了 ——
    同一份数据在两个视图里说法不一致，比两处都错更难排查。
    """
    candidates = _trackable_index(session, start, end)
    items: list[dict[str, Any]] = []
    for raw in insights(session, start, end, limit=20):
        if is_empty_resource_demand(raw.get("title")):
            continue
        discovery_kind = _INSIGHT_DISCOVERY_KIND.get(raw.get("kind") or "", "anomaly")
        items.append(
            _discovery_item(
                kind=discovery_kind,
                source_kind=raw.get("kind") or "",
                title=raw.get("title"),
                conclusion=raw.get("conclusion"),
                reason=raw.get("reason"),
                risk_level=raw.get("risk_level"),
                evidence_count=raw.get("evidence_count"),
                departments=raw.get("departments") or [],
                employees=raw.get("employees") or [],
                period=raw.get("period"),
                duration_weeks=raw.get("duration_weeks"),
                sources=raw.get("sources") or [],
                matched=_match_trackable(raw.get("title"), candidates),
            )
        )
    return items[:limit]


def _progress_discoveries(
    session: Session, start: date, end: date, *, department_id: int | None = None, limit: int = 4
) -> list[dict[str, Any]]:
    """本期完成过的主线 —— 关键发现里的「进展」类。

    与模型归纳的结论不同，这里完全是结构化事实：任务在本期有完成类事项即成立，
    依据直接指向那篇周报。
    """
    rows = _theme_rows(session, start, end, department_id=department_id)
    grouped: dict[int, dict[str, Any]] = {}
    for task, item, report, employee, department in rows:
        if not _is_completed_item(item):
            continue
        bucket = grouped.setdefault(
            task.id,
            {"task_id": task.id, "title": task.title, "sources": [], "_report_ids": set(), "departments": set()},
        )
        if len(bucket["sources"]) < EVIDENCE_PER_THEME_LIMIT:
            bucket["sources"].append(
                {
                    "report_id": report.id,
                    "title": report.title,
                    "employee_id": employee.id if employee else None,
                    "employee_name": employee.name if employee else None,
                    "department_name": department.name if department else None,
                    "excerpt": (item.source_text or "")[:200],
                }
            )
        bucket["_report_ids"].add(report.id)
        if department is not None:
            bucket["departments"].add(department.name)

    items = [
        _discovery_item(
            kind="progress",
            source_kind="task_completed",
            title=bucket["title"],
            conclusion=f"本期有明确完成事项，依据来自 {len(bucket['_report_ids'])} 篇周报。",
            reason="结构化分析提取到完成类事项（completed_items / results）。",
            risk_level="低",
            evidence_count=len(bucket["_report_ids"]),
            departments=sorted(bucket["departments"]),
            employees=[],
            period={"start": start.isoformat(), "end": end.isoformat()},
            sources=bucket["sources"],
        )
        for bucket in grouped.values()
    ]
    items.sort(key=lambda item: (-int(item["evidence_count"]), item["title"] or ""))
    return items[:limit]


def _attention_items(
    session: Session,
    start: date,
    end: date,
    *,
    limit: int = 8,
    candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """需要你处理：已有管理跟进 + 可升级的高风险卡点。

    已纳入跟进的事项优先（它们是人工确认过的），其后才是候选洞察。
    """
    # 过滤「资源需求：Discuss later / 暂无需求」等占位项后再截断，避免噪音挤掉
    # 后续真正需要管理者处理的事项。
    if candidates is None:
        candidates = management_action_items(
            session, start, end, limit=max(limit * 3, limit)
        )
    actionable = [
        item for item in candidates if not is_empty_resource_demand(item.get("title"))
    ]
    return actionable[:limit]


def _coverage(session: Session, start: date, end: date) -> dict[str, Any]:
    coverage = analysis_coverage(session, start, end)
    total = int(coverage.get("reports_total") or 0)
    analyzed = int(coverage.get("analyzed_reports") or 0)
    return {
        **coverage,
        # 分析未跑完时页面必须显示「结论可能不完整」，不能让部分覆盖被当成全量结论。
        "partial": analyzed < total,
        "missing_analysis": max(total - analyzed, 0),
    }


def weekly_view(
    session: Session, start: date, end: date, *, department_id: int | None = None
) -> dict[str, Any]:
    """上周视图：主要工作方向 + 关键发现 + 需要处理。

    ``themes`` 刻意**不做后端过滤**，只带 ``is_company_level`` 标记：单人事也是事实，
    该看的人可能就想看；但页面必须能把「公司主线」和「个人事项」分开呈现，
    否则「上周进展」会退化成一串个人待办（实测 318 条里 317 条是单人事）。
    """
    rows = _theme_rows(session, start, end, department_id=department_id)
    themes = _themes(rows)
    discoveries = _progress_discoveries(session, start, end, department_id=department_id) + _discoveries(
        session, start, end
    )
    return {
        "themes": themes,
        "company_theme_count": sum(1 for theme in themes if theme["is_company_level"]),
        "discoveries": discoveries,
        "attention": _attention_items(session, start, end),
        "coverage": _coverage(session, start, end),
    }


# =============================================================================
# 每月视图：回答管理层的五个问题
#
# 五个问题与实现一一对应，改之前先看这张对照表：
#   ① 公司现在主要推进什么？      -> ``_mainlines``
#   ② 最近一个月发生了什么变化？  -> ``_monthly_changes``
#   ③ 哪些事情卡住了？            -> ``_stuck_work``
#   ④ 有哪些平时没发现的异常？    -> ``_anomalies``
#   ⑤ 哪些需要管理层介入？        -> ``_interventions`` + ``merge_summary_actions``
#
# 三条纪律与综合视图一致：
#   1. 每个结论都能追到周报 ID（``source_report_ids``），不做文案推测。
#   2. **「有数据」不等于「可比」**：需要基线的判断（新增 / 明显推进 / 恶化）
#      一律走 ``_comparability``；不可比就只陈述本月事实并说明原因。
#   3. 事实与判断分开说：``*_count`` 是事实，「已完成 / 停滞」是判断 —— 两者
#      都返回，页面按位置渲染，不把判断当成事实写进数字。
# =============================================================================

# 公司主线的准入门槛：同一业务对象至少这么多条事项才算非一次性提及。
MAINLINE_MIN_ITEMS = 2
MAINLINE_LIMIT = 8

# 公司主线**只**排除会议与行政后勤这类「每个部门都会做」的日常事务。
# 与 ``_is_activity_category``（给「疑似重复推进」用的那张更宽的表）刻意分开：
# 后者还会排除「中介管理」「学生申请全流程处理」这类真实业务过程 —— 实测用在
# 主线上会把公司实际在推的事删掉（3 个月窗口里少掉 2 条跨部门主线）。
# 两张表用途不同，不要合并成一张。
MAINLINE_ROUTINE_MARKERS = (
    "会议", "例会", "周会", "内部协作", "会务", "接待",
    "报销", "费用", "票据", "差旅", "台账", "归档",
    "办公", "绿植", "部门管理", "团队管理",
)

# 各清单的返回上限。**后端给全、前端决定先显示几条**：
# 页面是「一页简报」（要点区 + 五问，每问先显示 3 条，其余内联展开），
# 展开动作发生在浏览器里，所以这些清单必须把完整内容发过去 —— 若后端先截到 8 条，
# 「展开全部 26 条」就展不开，而标签/标题上的数字只能写「8」，与真实总量不符。
#
# 上限仍然保留（防止超大组织把响应撑爆）：超过 `MONTHLY_LIST_CAP` 时前端会如实
# 提示「另有 N 条未列出」，`total` 字段始终是真实总量。
MONTHLY_LIST_CAP = 50
CHANGE_LIMIT = MONTHLY_LIST_CAP
STUCK_LIMIT = MONTHLY_LIST_CAP
ANOMALY_LIMIT = MONTHLY_LIST_CAP
MAINLINE_LIMIT = MONTHLY_LIST_CAP
# 介入建议**刻意不放开**：「只留下真正需要领导处理的 3~5 件事」是产品要求，
# 不是版面妥协 —— 这一块不提供「展开全部」。
INTERVENTION_LIMIT = 5

# 卡点进入「卡住了」清单的最低持续周数：1 周只能算一次提及，不是「卡住」。
STUCK_MIN_WEEKS = 2

# 异常「多人反复提及同一类问题」的准入。门槛的意义是「这不只是某一个人的抱怨」：
# 单人未解决的问题走卡点清单，不占用异常名额。
ANOMALY_MIN_PEOPLE = 3
ANOMALY_MIN_REPORTS = 3

# 异常「某部门问题逐期增长」的回看窗口（自然周）。
ANOMALY_WINDOW_WEEKS = 6

# 变化类型 → (中文标签, 语气)。语气值与前端 Pill 对齐，前端只渲染不映射。
CHANGE_KINDS: dict[str, tuple[str, str]] = {
    "new": ("新增", "blue"),
    "progress": ("明显推进", "green"),
    "stalled": ("停滞", "amber"),
    "completed": ("已完成", "green"),
    "worsening": ("恶化", "red"),
    "ongoing": ("持续推进", "neutral"),
}

# 需要基线的变化类型：没有可对比月份时**不允许**给出这几类判断。
CHANGE_KINDS_NEEDING_BASELINE = {"new", "progress", "worsening"}

# 「停滞」的准入：本月至少这么多项未解决、且一项都没完成，才算卡住。
# 阈值是 2 而不是 1 —— 只有一条待确认事项的对象是个单点提及，不是「这件事停了」；
# 实测把它算进去会让清单从 4 条涨到 57 条，全是「某人这周有一件事待确认」。
CHANGE_STALLED_MIN_ISSUES = 2

# 「恶化」在完成与问题并存时的准入：未解决事项确实变多，且绝对量已经不小。
# 只比基线多 1 项不加这条，会把正常波动报成恶化。
CHANGE_WORSENING_MIN_ISSUES = 3

# 一条变化的入选门槛：本月事项数。单条事项不构成「变化」。
CHANGE_MIN_ITEMS = 2

# 变化清单的第二个门槛：本月至少要有这么多篇周报作为证据。
# 只有一个人提过一次的事，放在主线清单里可见就够了 —— 把它标成「停滞」或
# 「明显推进」是在用一篇周报的措辞代表整件工作的状态。
CHANGE_MIN_REPORTS = 2

# 规则版「建议动作」：AI 总结没生成时页面靠它给出可执行动作（并标明来源是规则）。
# 每条都要求能落到「找谁 / 定什么 / 批什么」，与提示词里对模型的要求一致。
INTERVENTION_RULE_ACTIONS = {
    "blocker": "指定唯一的跟进人并给出最晚关闭时间；到期仍无法关闭时决定降级或终止。",
    "dependency": "找被依赖方当场确定交付时间；对方给不出承诺时改由管理层定替代路径。",
    "worsening": "先核实口径与事实，再指定责任方和本周期内必须完成的处置动作。",
    "repeated_issue": "把这个问题收敛成一条明确的整改要求，指定负责人与验收时间。",
    "department_growth": "核对增长是否来自真实工作量，据此决定是否加人或改流程。",
    "duplicate_effort": "核对两边分工，明确唯一负责人，避免各自重复推进。",
    "status_conflict": "以最新事实为准更正状态，并把结论同步给相关方。",
}


def _is_routine_mainline(name: str) -> bool:
    """业务对象名是不是「每个部门都会做」的日常事务（会议 / 行政后勤）。

    只用于公司主线清单，不用于「疑似重复推进」（那张表更宽，见模块常量注释）。
    """
    compact = _NON_WORD.sub("", (name or "").strip())
    if not compact:
        return True
    return any(marker in compact for marker in MAINLINE_ROUTINE_MARKERS)


def _month_coverage(session: Session, months: list[tuple[date, date, str]]) -> dict[str, dict[str, Any]]:
    """每个自然月的分析覆盖（篇数 / 已分析 / 覆盖率）。

    归属按 ``end_date`` 落到**唯一**一个月：8/31~9/6 那篇只算 9 月。
    刻意不复用 ``_coverage_by_period`` —— 那个口径允许一篇周报同时落在两个重叠
    周期里（「这个周期有没有证据」），月份这样做会让各月篇数之和大于总篇数。
    """
    empty = {key: {"reports_total": 0, "analyzed_reports": 0, "ratio": 0.0} for _s, _e, key in months}
    if not months:
        return empty
    span_start, span_end = months[0][0], months[-1][1]

    def _load() -> dict[str, dict[str, Any]]:
        report_rows = session.execute(
            select(WeeklyReport.id, WeeklyReport.end_date, WeeklyReport.start_date)
            .select_from(WeeklyReport)
            .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
            .where(
                *report_period_overlap_conditions(span_start, span_end),
                non_exempt_report_condition(session),
            )
        )
        analyzed_ids = set(
            session.scalars(
                select(ReportItem.report_id)
                .select_from(ReportItem)
                .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
                .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
                .where(
                    *report_period_overlap_conditions(span_start, span_end),
                    (~resource_demand_candidate_condition()) | WeeklyReport.ai_checked_at.isnot(None),
                    non_exempt_report_condition(session),
                )
                .distinct()
            )
        )
        counters = {key: {"reports_total": 0, "analyzed_reports": 0} for _s, _e, key in months}
        # 每个月的**实际**起止（首尾月份是裁过的）。
        spans = [(month_start, month_end, key) for month_start, month_end, key in months]
        for report_id, end_date, start_date in report_rows:
            occurred = end_date or start_date
            if occurred is None:
                continue
            # 必须落在该月的**实际区间**内，而不只是月份相同：查「9/1~9/24」时，
            # 一个 9/21~9/27 的周期结束日在 9/27（区间之外），按月份名归到 9 月会把它
            # 算进一个「还没走完」的范围里。裁剪之后「篇数」= 在该区间内结束的周期。
            for month_start, month_end, key in spans:
                if month_start <= occurred <= month_end:
                    counters[key]["reports_total"] += 1
                    if report_id in analyzed_ids:
                        counters[key]["analyzed_reports"] += 1
                    break
        return {
            key: {
                **counts,
                "ratio": round(counts["analyzed_reports"] / counts["reports_total"], 4)
                if counts["reports_total"]
                else 0.0,
            }
            for key, counts in counters.items()
        }

    return _memo(session, ("month_coverage", tuple(months)), _load)


def _monthly_coverage_sum(month_payload: list[dict[str, Any]]) -> dict[str, Any]:
    """月度视图的顶部覆盖 = 逐月明细之和（按构造一致）。

    与 ``_coverage``（周期与区间重叠即计入）的区别见 ``monthly_view`` 里的注释：
    两个口径在跨月那一周会分歧，同一页出现两个数字比数字不精确更糟。
    """
    total = sum(int(item.get("report_total") or 0) for item in month_payload)
    analyzed = sum(int(item.get("analyzed_reports") or 0) for item in month_payload)
    return {
        "reports_total": total,
        "analyzed_reports": analyzed,
        "rate": round(analyzed / total * 100, 1) if total else 0,
        "partial": analyzed < total,
        "missing_analysis": max(total - analyzed, 0),
    }


def _baseline_for_latest(
    months: list[tuple[date, date, str]], coverage_by_month: dict[str, dict[str, Any]]
) -> tuple[str | None, dict[str, Any]]:
    """给「最近一个月」找一个可用的对比月（**不一定是紧邻的上月**）。

    紧邻的上月没做结构化分析时，继续往前找最近一个覆盖率达标、且与本月相差
    不超过门槛的月份；找不到就如实报告「不可比」并列出被跳过的月份与原因。

    为什么不退化成「用上月篇数硬比」：实测 7 月 110 篇 0 篇分析、9 月 82 篇 29 篇
    分析，硬比会把「这个月多分析了 29 篇」显示成「什么都在恶化」。
    """
    if len(months) < 2:
        return None, {
            "month": None,
            "comparable": False,
            "reason": "所选范围只有一个自然月，无法比较变化",
            "skipped": [],
        }
    latest_cov = coverage_by_month.get(months[-1][2]) or {}
    skipped: list[dict[str, Any]] = []
    for _start, _end, key in reversed(months[:-1]):
        comparison = _comparability(latest_cov, coverage_by_month.get(key) or {})
        if comparison["comparable"]:
            return key, {
                "month": key,
                "comparable": True,
                "reason": None,
                "current_coverage": comparison["current_coverage"],
                "previous_coverage": comparison["previous_coverage"],
                "min_coverage": comparison["min_coverage"],
                "skipped": skipped,
            }
        skipped.append(
            {
                "month": key,
                "reason": comparison["reason"],
                "coverage": (coverage_by_month.get(key) or {}).get("ratio", 0.0),
            }
        )
    return None, {
        "month": None,
        "comparable": False,
        "reason": skipped[-1]["reason"] if skipped else "范围内没有可对比的月份",
        "skipped": skipped,
    }


def _mainlines(
    session: Session,
    start: date,
    end: date,
    months: list[tuple[date, date, str]],
    *,
    limit: int | None = MAINLINE_LIMIT,
) -> dict[str, Any]:
    """公司在推进的工作主线（问题①）。

    身份是**业务对象**（``ReportItem.related_project``：学校、项目、机构、系统），
    不是 Task —— 模型对同一件事每次给出的 ``task_key`` 都不相同（开发库 2882 个
    Task 里绝大多数只出现一次），按 Task 数会得到「一个月几百条主线」。

    排序优先跨部门与跨周期，因为管理层要看的是「哪些事需要多个部门一起推、
    并且推了不止一个月」；单部门单月的事项排在后面，是不是要展开由页面决定。
    """
    month_keys = [key for _s, _e, key in months]
    rows = _theme_rows(session, start, end)
    grouped: dict[str, dict[str, Any]] = {}
    for _task, item, report, employee, department in rows:
        name = (item.related_project or "").strip()
        if not name or _is_routine_mainline(name):
            continue
        occurred = report.end_date or report.start_date
        month_key = _month_key(occurred) if occurred else None
        bucket = grouped.setdefault(
            name,
            {
                "name": name,
                "item_count": 0,
                "report_ids": set(),
                "employees": set(),
                "departments": set(),
                "months": set(),
                "progress_count": 0,
                "issue_count": 0,
                "latest": None,
            },
        )
        bucket["item_count"] += 1
        bucket["report_ids"].add(int(report.id))
        if employee is not None:
            bucket["employees"].add(employee.name)
        if department is not None:
            bucket["departments"].add(department.name)
        if month_key and month_key in month_keys:
            bucket["months"].add(month_key)
        if _is_completed_item(item):
            bucket["progress_count"] += 1
        if item.item_type in DEPENDENCY_ITEM_TYPES and not _is_noise_item(item):
            bucket["issue_count"] += 1
        # 最近一条证据决定「现在到什么阶段了」。同一篇周报里 idx 大的更晚，故按 id 比。
        marker = (occurred or date.min, int(item.id))
        if bucket["latest"] is None or marker > bucket["latest"][0]:
            bucket["latest"] = (
                marker,
                {
                    "title": item.title,
                    "status": item.status,
                    "item_type": item.item_type,
                    "report_id": int(report.id),
                    "month": month_key,
                    "department_name": department.name if department else None,
                    "employee_name": employee.name if employee else None,
                },
            )

    items: list[dict[str, Any]] = []
    for bucket in grouped.values():
        if int(bucket["item_count"]) < MAINLINE_MIN_ITEMS:
            continue
        departments = sorted(bucket["departments"])
        month_list = sorted(bucket["months"])
        report_ids = sorted(bucket["report_ids"])
        cross_department = len(departments) >= 2
        cross_month = len(month_list) >= 2
        items.append(
            {
                "name": bucket["name"],
                "item_count": int(bucket["item_count"]),
                "report_count": len(report_ids),
                "employee_count": len(bucket["employees"]),
                "departments": departments,
                "department_count": len(departments),
                "months": month_list,
                "month_count": len(month_list),
                "is_cross_department": cross_department,
                "is_cross_month": cross_month,
                "scope_label": "跨部门 · 跨月"
                if cross_department and cross_month
                else ("跨部门" if cross_department else ("跨月" if cross_month else "单部门单月")),
                "progress_count": int(bucket["progress_count"]),
                "issue_count": int(bucket["issue_count"]),
                "latest": bucket["latest"][1] if bucket["latest"] else None,
                "source_report_ids": report_ids,
            }
        )
    items.sort(
        key=lambda item: (
            0 if item["is_cross_department"] else 1,
            0 if item["is_cross_month"] else 1,
            -int(item["report_count"]),
            -int(item["item_count"]),
            item["name"],
        )
    )
    return {
        # limit=None 返回完整清单：部门索引与按部门过滤都要在**截断前**做，
        # 否则「该部门有 20 条主线」会只剩公司前 8 条里碰巧属于它的那几条。
        "items": items if limit is None else items[:limit],
        "total": len(items),
        "cross_department_count": sum(1 for item in items if item["is_cross_department"]),
        "cross_month_count": sum(1 for item in items if item["is_cross_month"]),
        "min_items": MAINLINE_MIN_ITEMS,
        # 口径写在后端由前端原样渲染：两处各写一句迟早对不上。
        "basis": (
            f"按业务对象（周报事项里的项目/院校/机构名）聚合，至少 {MAINLINE_MIN_ITEMS} 条事项；"
            "排序优先跨部门、跨月。名称来自周报原文，同一件事换名字写成两个对象时不会合并。"
        ),
    }


def _monthly_changes(
    session: Session,
    start: date,
    end: date,
    months: list[tuple[date, date, str]],
    coverage_by_month: dict[str, dict[str, Any]],
    *,
    limit: int | None = CHANGE_LIMIT,
) -> dict[str, Any]:
    """最近一个月发生了什么变化（问题②）。

    只对**在最近一个月有已分析事项**的业务对象判断，且把判断拆成两层：

    * 事实层：本月有几项完成、几项待解决（只看本月证据，不依赖任何基线）。
    * 判断层：新增 / 明显推进 / 恶化 —— **必须**有可对比的基线月份（见
      ``_baseline_for_latest``）。基线不可比时这几类一律不给，只保留事实层，
      并如实说明「哪个月被排除、为什么」。

    这套分层是被真实数据逼出来的：开发库 7 月 110 篇 0 篇分析、8 月 123 篇 0 篇
    分析、9 月 82 篇 29 篇分析。只看「有没有数据」会给 7 月凭空判出一堆「新增」。
    """
    month_keys = [key for _s, _e, key in months]
    empty: dict[str, Any] = {
        "items": [],
        "month": None,
        "month_label": None,
        "baseline": {"month": None, "comparable": False, "reason": "所选范围没有月份", "skipped": []},
        "silent": {"count": 0, "names": []},
        "unattributed_items": 0,
        "routine_items": 0,
        "basis": "",
    }
    if not months:
        return empty

    latest_key = month_keys[-1]
    latest_label = _month_label(months[-1])
    facts: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: {"progress": 0, "issues": 0, "ongoing": 0}))
    reports_of: dict[str, set[int]] = defaultdict(set)
    latest_reports: dict[str, set[int]] = defaultdict(set)
    departments_of: dict[str, set[str]] = defaultdict(set)
    unattributed = 0
    routine_items = 0
    for item, report, _employee, department, _task_id in _period_item_rows(session, start, end):
        name = (item.related_project or "").strip()
        if not name:
            unattributed += 1
            continue
        if _is_routine_mainline(name):
            routine_items += 1
            continue
        occurred = report.end_date or report.start_date
        if occurred is None:
            continue
        month_key = _month_key(occurred)
        if month_key not in month_keys:
            continue
        if _is_noise_item(item):
            # 「资源需求：Discuss later」这类占位不是进展也不是问题，不计入任何一侧。
            continue
        bucket = facts[name][month_key]
        if _is_completed_item(item):
            bucket["progress"] += 1
        elif item.item_type in DEPENDENCY_ITEM_TYPES:
            bucket["issues"] += 1
        elif item.item_type in ONGOING_ITEM_TYPES:
            bucket["ongoing"] += 1
        reports_of[name].add(int(report.id))
        if month_key == latest_key:
            latest_reports[name].add(int(report.id))
        if department is not None:
            departments_of[name].add(department.name)

    baseline_key, baseline = _baseline_for_latest(months, coverage_by_month)
    baseline_comparable = bool(baseline.get("comparable"))
    baseline_label = _month_label_from_key(baseline_key) if baseline_key else None

    items: list[dict[str, Any]] = []
    skipped_thin = 0
    for name, by_month in facts.items():
        latest_counts = by_month.get(latest_key)
        if not latest_counts:
            # 本月没有已分析证据的对象不进变化清单（「未再次提及」不等于「已完成」）。
            continue
        progress = int(latest_counts.get("progress", 0))
        issues = int(latest_counts.get("issues", 0))
        ongoing = int(latest_counts.get("ongoing", 0))
        if progress + issues + ongoing < CHANGE_MIN_ITEMS:
            # 一条事项不构成「这个月发生了变化」——它在主线清单里可见，
            # 但不够格进变化清单（否则清单上全是单点提及）。
            skipped_thin += 1
            continue
        if len(latest_reports.get(name, set())) < CHANGE_MIN_REPORTS:
            # 只有一篇周报作为证据时不判变化类型，理由见常量注释。
            # 这里数的是**本月**的篇数，不是整个范围的 —— 三个月里出现过两篇、
            # 但本月只有一篇，仍然不足以判断「这个月变了什么」。
            skipped_thin += 1
            continue
        base_counts = by_month.get(baseline_key) if baseline_key else None
        base_progress = int((base_counts or {}).get("progress", 0))
        base_issues = int((base_counts or {}).get("issues", 0))
        if baseline_comparable and base_counts is None:
            kind = "new"
        elif progress == 0 and issues >= CHANGE_STALLED_MIN_ISSUES:
            # 一件没完成、还压着多项未解决 —— 停滞。若未解决数比基线还多，是恶化。
            kind = "worsening" if (baseline_comparable and issues > base_issues) else "stalled"
        elif (
            issues >= CHANGE_WORSENING_MIN_ISSUES
            and baseline_comparable
            and issues > base_issues
        ):
            # 有完成也有问题，但未解决数确实在涨（且已经不是零星几条）。
            kind = "worsening"
        elif progress and not issues:
            kind = "completed"
        elif progress:
            kind = "progress" if (baseline_comparable and progress > base_progress) else "ongoing"
        else:
            # 没有完成、未解决数也没到停滞门槛：只有进行中的事项，不值得单列。
            skipped_thin += 1
            continue
        label, tone = CHANGE_KINDS[kind]
        report_ids = sorted(reports_of.get(name, set()))
        parts = [f"本月完成 {progress} 项、待解决 {issues} 项"]
        if ongoing:
            parts.append(f"另有 {ongoing} 项在推进")
        if baseline_comparable:
            parts.append(
                f"对比 {baseline_label}：完成 {base_progress} 项、待解决 {base_issues} 项"
            )
        # 可比时不再补一句「证据来自 N 篇」：卡片底部的来源行已经给出篇数，
        # 同一句话写两遍是这一页读起来冗长的主要来源之一。
        items.append(
            {
                "kind": kind,
                "kind_label": label,
                "tone": tone,
                "title": name,
                "detail": "；".join(parts) + "。",
                "month": latest_key,
                "departments": sorted(departments_of.get(name, set())),
                "progress_count": progress,
                "issue_count": issues,
                "ongoing_count": ongoing,
                "baseline_month": baseline_key,
                "baseline_progress": base_progress if baseline_comparable else None,
                "baseline_issues": base_issues if baseline_comparable else None,
                "needs_baseline": kind in CHANGE_KINDS_NEEDING_BASELINE,
                "source_report_ids": report_ids,
            }
        )

    kind_order = {"worsening": 0, "stalled": 1, "new": 2, "progress": 3, "completed": 4, "ongoing": 5}
    items.sort(
        key=lambda item: (
            kind_order.get(item["kind"], 9),
            -int(item["issue_count"]),
            -int(item["progress_count"]),
            item["title"],
        )
    )
    # 「基线月出现过、本月没有已分析证据」的对象：只报数，不判「已完成」。
    silent = sorted(
        name
        for name, by_month in facts.items()
        if baseline_key and by_month.get(baseline_key) and not by_month.get(latest_key)
    )
    basis = (
        "变化类型由代码判定，且本月至少要有 "
        f"{CHANGE_MIN_ITEMS} 条事项才入选：已完成 / 停滞 / 持续推进 只看本月证据；"
        f"新增 / 明显推进 / 恶化 需要可对比的基线月份（覆盖率 ≥ {COMPARABLE_MIN_COVERAGE:.0%}"
        f" 且两侧相差 < {COMPARABLE_MAX_COVERAGE_GAP:.0%}）。"
    )
    return {
        "items": items if limit is None else items[:limit],
        "total": len(items),
        "month": latest_key,
        "month_label": latest_label,
        "coverage": (coverage_by_month.get(latest_key) or {}).get("ratio", 0.0),
        "baseline": baseline,
        "silent": {"count": len(silent), "names": silent[:5]},
        "unattributed_items": unattributed,
        "routine_items": routine_items,
        "thin_items": skipped_thin,
        "basis": basis,
    }


def _month_label(month: tuple[date, date, str]) -> str:
    return f"{month[0].year} 年 {month[0].month} 月"


def _month_label_from_key(key: str | None) -> str | None:
    if not key or "-" not in key:
        return None
    year, month = key.split("-", 1)
    try:
        return f"{int(year)} 年 {int(month)} 月"
    except ValueError:
        return key


def _is_placeholder_demand(value: str | None) -> bool:
    """「资源需求：聚焦」这类填了等于没填的占位。

    ``is_empty_resource_demand`` 认的是那几张固定的无需求哨兵表，
    挡不住员工随手写的单词（实测「聚焦」「Discuss later」都被判成真需求）。
    这里补一道：剥掉「资源需求：」前缀后不足 ``len`` 个字、且不含任何具体对象的，
    当成没填 —— 它既不是问题，也读不出要公司做什么。
    """
    text = re.sub(r"^资源需求\s*[:：]\s*", "", (value or "").strip())
    if not text:
        return True
    if len(text) >= 6:
        return False
    # 短文本里出现这些词说明指向了具体对象，保留。
    return not any(marker in text for marker in ("系统", "网站", "设备", "人员", "预算", "场地", "账号"))


def _stuck_work(
    session: Session, start: date, end: date, *, limit: int | None = STUCK_LIMIT
) -> dict[str, Any]:
    """哪些事情卡住了（问题③）。

    两类来源，都带「持续多久 + 涉及谁 + 来源周报」：

    * 仍未关闭、持续 ≥ ``STUCK_MIN_WEEKS`` 周的卡点（持续时间来自 Blocker 自身的
      跨周证据，不是从措辞里猜的）。
    * 被 ≥2 项工作依赖的对象（``_bottlenecks`` 的同一份口径），它可能只有一两篇
      周报提到，但确实挡着多条线。

    「资源需求：Discuss later」这类员工没填的记录在两侧都被过滤 —— 否则
    「没人填需求」会被读成「公司被卡住了」。
    """
    items: list[dict[str, Any]] = []
    thin_blockers = 0
    blocker_rows = session.execute(
        select(Blocker, Task)
        .join(Task, Task.id == Blocker.task_id)
        .where(
            Blocker.status == BlockerStatus.OPEN,
            Blocker.duration_weeks >= STUCK_MIN_WEEKS,
            Blocker.last_seen_date >= start,
            Blocker.last_seen_date <= end,
        )
        .order_by(Blocker.duration_weeks.desc(), Blocker.last_seen_date.desc())
        .limit(40)
    ).all()
    source_map = task_sources(session, [task.id for _blocker, task in blocker_rows])
    # 证据篇数走真实计数（GROUP BY），不数 task_sources 截断后的展示列表 ——
    # 那个 limit 是给 AI 材料省 token 的，拿来当篇数会让「N 篇周报」永远封顶 5。
    evidence_counts = task_report_counts(
        session, [task.id for _blocker, task in blocker_rows], start=start, end=end
    )
    for blocker, task in blocker_rows:
        if is_empty_resource_demand(blocker.title) or _is_placeholder_demand(blocker.title):
            continue
        sources = [
            source
            for source in source_map.get(task.id, [])
            if _source_overlaps_period(source, start, end)
        ]
        if not sources:
            # 拿不到本范围内的来源就无法核对，不放进「卡住了」清单。
            continue
        # 证据只有一篇周报、又没有部门或风险等级支撑时，这是「某个人提过一次」，
        # 不是「这件事卡住了」。实测这类条目会占满清单前半段，把真正跨部门的依赖挤下去。
        # ``duration_weeks`` 由 Blocker 自己的首末出现日期算出，是真实的复现证据，
        # 所以持续到 ``BOTTLENECK_MIN_EVIDENCE`` 周以上时同样放行 —— 否则
        # 「一个人连续三周报告同一件事」会被当成单次提及丢掉。
        evidence_reports = {source["report_id"] for source in sources}
        # 篇数以真实计数为准（与展示列表取 max 兜底，宁可不少报）；
        # 「一个人连续三周报告同一件事」的放行判定也据此不再被截断误导。
        evidence_total = max(evidence_counts.get(task.id, 0), len(evidence_reports))
        if (
            evidence_total < BOTTLENECK_MIN_EVIDENCE
            and not any(source.get("department_name") for source in sources)
            and int(blocker.duration_weeks or 1) < BOTTLENECK_MIN_EVIDENCE
            and _risk_weight(blocker.risk_level) < _risk_weight("中")
        ):
            thin_blockers += 1
            continue
        months = sorted(
            {
                _month_key(date.fromisoformat(source["period_end"] or source["period_start"]))
                for source in sources
                if source.get("period_end") or source.get("period_start")
            }
        )
        departments = sorted(
            {source["department_name"] for source in sources if source.get("department_name")}
        )
        # 说的内容用**周报原文片段**，不用 ``blocker.description``：后者是模型写的
        # 概括，实测带「进度10%」这类原文没有的比例（周报里没有工时或完成率），
        # 直接展示等于把编造的数字放到「卡住了」清单上。原文片段可点回周报核对。
        evidence = next(
            (source for source in sources if (source.get("source_text") or "").strip()),
            sources[0],
        )
        excerpt = _clean_excerpt(evidence.get("source_text"))
        items.append(
            {
                "kind": "blocker",
                "title": blocker.title,
                "detail": excerpt
                or f"已持续 {int(blocker.duration_weeks or 1)} 周仍未关闭。",
                "duration_weeks": int(blocker.duration_weeks or 1),
                "occurrence_count": int(blocker.occurrence_count or 1),
                "risk_level": blocker.risk_level,
                "departments": departments,
                "department_count": len(departments),
                "months": months,
                "month_count": len(months),
                "last_seen": blocker.last_seen_date.isoformat() if blocker.last_seen_date else None,
                "evidence_count": evidence_total,
                "blocker_id": int(blocker.id),
                "source_report_ids": sorted({int(source["report_id"]) for source in sources}),
                "source_item_ids": [],
            }
        )

    for entry in _bottlenecks(session, start, end, limit=20)["items"]:
        # **不在这里重复「挡住几条主线」那道门槛**：那是「谁最拖慢公司」（信号排序）
        # 的准入，不是「这件事卡住了」的准入。两个部门各自在等同一个对象、只挡住
        # 一条主线时，它仍然是卡住的事 —— 实测加那道门槛会让这种条目整类消失。
        # 单次提及由下面这道更精确的过滤挡住。
        if (
            int(entry["evidence_count"]) < BOTTLENECK_MIN_EVIDENCE
            and int(entry["department_count"]) < BOTTLENECK_MIN_EVIDENCE
        ):
            thin_blockers += 1
            continue
        if any(_same_subject(entry["target"], existing["title"]) for existing in items):
            # 同一件事已经被卡点清单收录，不重复占位。
            continue
        span = _dependency_span(entry, start, end)
        items.append(
            {
                "kind": "dependency",
                "title": entry["target"],
                # 只写「有多少项工作在等它」——涉及几个部门、几篇周报由卡片的
                # 标签行与元信息行给出，同一批数字写两遍是这一页冗长的主要来源。
                "detail": f"{int(entry['workstream_count'])} 项工作依赖它。",
                # 被依赖对象的「持续多久」按它在多少个自然周里有证据算 —— 与卡点的
                # ``duration_weeks`` 口径一致（都是「有证据的周数」），管理层清单上的
                # 「持续时间」因此对两类来源都成立，不再只对卡点成立。
                "duration_weeks": span["period_count"],
                "first_seen": span["first_seen"],
                "last_seen": span["last_seen"],
                "occurrence_count": None,
                "risk_level": None,
                "departments": entry["departments"],
                "department_count": int(entry["department_count"]),
                "months": span["months"],
                "month_count": len(span["months"]),
                "evidence_count": int(entry["evidence_count"]),
                "blocker_id": None,
                "source_report_ids": entry["source_report_ids"],
                "source_item_ids": [
                    int(source["id"]) for source in entry.get("sources") or [] if source.get("id")
                ],
            }
        )

    items.sort(
        key=lambda item: (
            -_risk_weight(item["risk_level"]),
            -_breadth_weight(item["department_count"], item["evidence_count"]),
            -int(item["duration_weeks"] or 0),
            item["title"],
        )
    )
    return {
        "items": items if limit is None else items[:limit],
        "total": len(items),
        "min_weeks": STUCK_MIN_WEEKS,
        "thin_blockers": thin_blockers,
        "basis": (
            "「卡住了」= 仍未关闭且持续 ≥ "
            f"{STUCK_MIN_WEEKS} 周的卡点，或被 ≥{BOTTLENECK_MIN_EVIDENCE} 项工作依赖的对象。"
            "两者都要求至少 2 篇周报或明确的部门证据；只有一篇周报提到的事项不列入"
            f"（当前有 {thin_blockers} 条这样的记录）。"
        ),
    }


def _dependency_span(entry: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """一个被依赖对象的持续范围：有证据的自然周数、首末日期、涉及月份。

    按 ``(year, week)`` 去重，不按篇数 —— 同一周里三个人写同一件事只算持续一周。
    """
    weeks: set[tuple[int, int]] = set()
    months: set[str] = set()
    dates: list[date] = []
    for source in entry.get("sources") or []:
        raw = source.get("period_end") or source.get("period_start")
        if not raw:
            continue
        try:
            occurred = date.fromisoformat(raw)
        except ValueError:
            continue
        dates.append(occurred)
        weeks.add(occurred.isocalendar()[:2])
        months.add(_month_key(occurred))
    if not dates:
        return {"period_count": 0, "first_seen": None, "last_seen": None, "months": []}
    return {
        "period_count": len(weeks),
        "first_seen": min(dates).isoformat(),
        "last_seen": max(dates).isoformat(),
        "months": sorted(months),
    }


def _same_subject(left: str | None, right: str | None) -> bool:
    """两个名称是不是同一件事（用于去重）：去掉标点后一方包含另一方。"""
    first = _NON_WORD.sub("", (left or "").strip())
    second = _NON_WORD.sub("", (right or "").strip())
    if not first or not second:
        return False
    return first in second or second in first


def _repeated_issue_anomalies(
    session: Session, start: date, end: date, *, limit: int | None = 3
) -> list[dict[str, Any]]:
    """异常①：同一类问题被**很多人、跨多个周期**反复提及。

    这是「你自己翻周报很难发现」的那一类：单看每一篇都只是一件小事，
    合起来才是「公司有一类问题一直没人解决」。计数单位是人与篇，不是条数 ——
    一个人写三遍不代表问题更大。
    """
    buckets: dict[str, dict[str, Any]] = {}
    for item, report, employee, department, _task_id in _period_item_rows(session, start, end):
        if item.item_type not in DEPENDENCY_ITEM_TYPES or _is_noise_item(item):
            continue
        # 「资源需求：聚焦」这类填了等于没填的短文本不是「一类问题」，
        # 让它进统计会把「多少人反复提到某类困难」算进没填表的人。
        if _is_placeholder_demand(item.title) and "资源需求" in f"{item.title or ''}":
            continue
        matched = _issue_category(f"{item.title or ''} {item.description or ''}")
        if matched is None:
            continue
        label = matched[0]
        occurred = report.end_date or report.start_date
        bucket = buckets.setdefault(
            label,
            {"people": set(), "reports": set(), "departments": set(), "months": set(), "items": 0, "samples": []},
        )
        bucket["items"] += 1
        if employee is not None:
            bucket["people"].add(employee.name)
        bucket["reports"].add(int(report.id))
        if department is not None:
            bucket["departments"].add(department.name)
        if occurred is not None:
            bucket["months"].add(_month_key(occurred))
        if len(bucket["samples"]) < 3:
            bucket["samples"].append(
                {
                    "title": item.title,
                    "department_name": department.name if department else None,
                    "employee_name": employee.name if employee else None,
                    "report_id": int(report.id),
                    "occurred_on": occurred.isoformat() if occurred else None,
                }
            )

    items: list[dict[str, Any]] = []
    for label, bucket in buckets.items():
        people = len(bucket["people"])
        if people < ANOMALY_MIN_PEOPLE or len(bucket["reports"]) < ANOMALY_MIN_REPORTS:
            continue
        departments = sorted(bucket["departments"])
        months = sorted(bucket["months"])
        items.append(
            {
                "kind": "repeated_issue",
                "kind_label": "反复提及",
                "tone": "amber",
                # 标题只写「问题是什么」：分组标题已经写明「反复提及」，人数在
                # metrics 里 —— 同一句话在一张卡里出现三遍是这一页冗长的主要来源。
                "title": label,
                # 原因必须**逐条可区分**：只写一句通用判断会让清单里 5 条的原因
                # 一字不差（实测 5 条塌缩成 2 句），读者反而要回头找差异。
                # 这里的数字取本条自己的：涉及几个部门、多少篇、跨几个月。
                "detail": (
                    f"{len(departments)} 个部门、{len(bucket['reports'])} 篇周报"
                    f"跨 {len(months)} 个月反复提到它；单个部门解决不了。"
                ),
                "metrics": [
                    {"label": "提及人数", "value": people},
                    {"label": "周报篇数", "value": len(bucket["reports"])},
                    {"label": "涉及部门", "value": len(departments)},
                ],
                "people_count": people,
                "departments": departments,
                "months": months,
                "evidence_count": len(bucket["reports"]),
                "samples": bucket["samples"],
                "source_report_ids": sorted(bucket["reports"]),
            }
        )
    items.sort(key=lambda item: (-int(item["people_count"]), -int(item["evidence_count"]), item["title"]))
    return items if limit is None else items[:limit]


def _department_pressure_anomalies(
    session: Session, end: date, *, limit: int | None = 3
) -> list[dict[str, Any]]:
    """异常②：某个部门的问题事项逐期增长。

    与「长期趋势」同一道护栏：只有覆盖率达标（≥
    ``COMPARABLE_MIN_COVERAGE``）的周期参与比较，且方向按**覆盖率折算后的占比**
    判断 —— 否则「这一期多分析了几篇」会被读成「这个部门问题在变多」。
    """
    periods = _period_sequence(end, ANOMALY_WINDOW_WEEKS)
    if len(periods) < 2:
        return []
    coverage = _coverage_by_period(session, periods)
    usable = [
        (start, period_end, period_end.isoformat())
        for start, period_end in periods
        if coverage.get(period_end.isoformat(), {}).get("ratio", 0.0) >= COMPARABLE_MIN_COVERAGE
    ]
    if len(usable) < 2:
        return []
    counts: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for item, report, _employee, department, _task_id in _period_item_rows(
        session, periods[0][0], periods[-1][1]
    ):
        if department is None or item.item_type not in DEPENDENCY_ITEM_TYPES or _is_noise_item(item):
            continue
        if _is_placeholder_demand(item.title) and "资源需求" in f"{item.title or ''}":
            continue
        occurred = report.end_date or report.start_date
        if occurred is None:
            continue
        for period_start, period_end, label in usable:
            if period_start <= occurred <= period_end:
                counts[department.name][label].add(int(report.id))
                break

    items: list[dict[str, Any]] = []
    labels = [label for _s, _e, label in usable]
    for name, by_period in counts.items():
        series = [len(by_period.get(label, set())) for label in labels]
        present = [index for index, count in enumerate(series) if count > 0]
        if len(present) < 2:
            continue
        first_index, last_index = present[0], present[-1]
        first, last = series[first_index], series[last_index]
        if last <= first or last < BOTTLENECK_MIN_EVIDENCE:
            continue
        first_ratio = coverage.get(labels[first_index], {}).get("ratio", 0.0)
        last_ratio = coverage.get(labels[last_index], {}).get("ratio", 0.0)
        first_share = (first / first_ratio) if first_ratio else float(first)
        last_share = (last / last_ratio) if last_ratio else float(last)
        if last_share <= first_share:
            continue
        items.append(
            {
                "kind": "department_growth",
                "kind_label": "问题增长",
                "tone": "amber",
                "title": f"{name}的问题事项逐期增加",
                "detail": (
                    f"在分析覆盖达标的周期里，{name}提到的问题事项从 {first} 项增至 {last} 项"
                    f"（{', '.join(labels[first_index:last_index + 1])}）；"
                    "已按各周期覆盖率折算，不是「多分析了几篇」造成的。"
                ),
                "metrics": [
                    {"label": "首期", "value": first},
                    {"label": "末期", "value": last},
                    {"label": "观察周期", "value": len(labels)},
                ],
                "people_count": 0,
                "departments": [name],
                "months": sorted({label[:7] for label in labels}),
                "evidence_count": sum(series),
                "samples": [],
                "series": series,
                "periods": labels,
                "source_report_ids": sorted(
                    {report_id for label in labels for report_id in by_period.get(label, set())}
                ),
            }
        )
    items.sort(key=lambda item: (-int(item["series"][-1]), item["title"]))
    return items if limit is None else items[:limit]


def _friction_anomalies(
    session: Session, start: date, end: date, *, limit: int | None = 2
) -> list[dict[str, Any]]:
    """异常③④：组织摩擦里「平时不会注意」的两类 —— 疑似重复推进、状态描述不一致。

    两者都只是**待核对**的线索，不是定论，措辞必须保留「疑似 / 建议核对」。
    """
    frictions = _frictions(session, start, end)
    items: list[dict[str, Any]] = []
    for duplicate in frictions["duplicates"][:limit]:
        items.append(
            {
                "kind": "duplicate_effort",
                "kind_label": "疑似重复",
                "tone": "amber",
                # 分组标题已写「疑似重复」，标题只留业务对象名
                "title": duplicate["project"],
                "detail": (
                    f"{'、'.join(duplicate['departments'])} 在同一时期各自记了该业务对象的事项，"
                    "周报里看不到分工说明，建议核对是否重复投入。"
                ),
                "metrics": [
                    {"label": "涉及部门", "value": int(duplicate["department_count"])},
                    {"label": "证据条数", "value": int(duplicate["evidence_count"])},
                ],
                "people_count": 0,
                "departments": duplicate["departments"],
                "months": [],
                "evidence_count": int(duplicate["evidence_count"]),
                "samples": duplicate.get("sources") or [],
                "source_report_ids": duplicate["source_report_ids"],
            }
        )
    for conflict in frictions["conflicts"][:limit]:
        items.append(
            {
                "kind": "status_conflict",
                "kind_label": "状态不一致",
                "tone": "amber",
                "title": conflict["title"],
                "detail": (
                    f"{'、'.join(conflict['finished_by'])} 记录为已完成，"
                    f"{'、'.join(conflict['open_by'])} 仍记为待确认或未解决，建议核对当前状态。"
                ),
                "metrics": [
                    {"label": "认为已完成", "value": len(conflict["finished_by"])},
                    {"label": "仍记为未解决", "value": len(conflict["open_by"])},
                ],
                "people_count": 0,
                "departments": conflict["departments"],
                "months": [],
                "evidence_count": len(conflict["finished_by"]) + len(conflict["open_by"]),
                "samples": [],
                "source_report_ids": conflict["source_report_ids"],
            }
        )
    return items if limit is None else items[:limit]


def _anomalies(
    session: Session, start: date, end: date, *, limit: int | None = ANOMALY_LIMIT
) -> dict[str, Any]:
    """有哪些平时没发现的异常（问题④）。"""
    per_kind = None if limit is None else max(2, limit // 2)
    items = (
        _department_pressure_anomalies(session, end, limit=per_kind)
        + _repeated_issue_anomalies(session, start, end, limit=per_kind)
        + _friction_anomalies(session, start, end, limit=per_kind)
    )
    return {
        "items": items if limit is None else items[:limit],
        "total": len(items),
        "min_people": ANOMALY_MIN_PEOPLE,
        "basis": (
            f"四类异常：同一类问题被 ≥{ANOMALY_MIN_PEOPLE} 人反复提及、某部门问题事项逐期增长"
            f"（近 {ANOMALY_WINDOW_WEEKS} 个自然周，只统计覆盖率 ≥ {COMPARABLE_MIN_COVERAGE:.0%} 的周期）、"
            "疑似重复推进、状态描述不一致。都是待核对的线索，不是定论。"
        ),
    }


def _interventions(
    *,
    stuck: dict[str, Any],
    changes: dict[str, Any],
    anomalies: dict[str, Any],
    limit: int | None = INTERVENTION_LIMIT,
) -> dict[str, Any]:
    """需要管理层介入的 3~5 件事（问题⑤）。

    只吃三份已经算好的清单，**不接 Session**：它按纪律不引入新判断，
    拿不到数据库就不会有人不小心在这里再查一遍、绕过上面那三份清单。

    来源只有三处，且都已在上面的清单里出现过：卡住的（含被依赖对象）、
    正在恶化的、以及多人反复提及的异常。**不引入新判断** —— 管理层清单若与
    上面的清单对不上，用户就不知道该信哪一份。

    每条固定五段：原因（reason）/ 影响（impact）/ 责任方（owner）/
    持续时间（duration）/ 建议动作（suggested_action）。前四段由证据算出来，
    建议动作先给规则版，AI 总结生成后由 ``merge_summary_actions`` 换成模型写的。

    **排序按影响面而不是按来源类别**：卡点是「一件事没关掉」，被依赖对象是
    「多条线一起等它」——后者往往更值得管理层出手。实测按类别排序时，
    一条只有 1 篇周报、0 个部门证据的低风险卡点会排到「7 个部门、32 篇周报都
    在等院校反馈」前面，那份清单就失去意义了。
    """
    candidates: list[dict[str, Any]] = []
    for entry in stuck["items"]:
        departments = entry["departments"]
        evidence = int(entry.get("evidence_count") or 0)
        common = {
            "title": entry["title"],
            "reason": entry["detail"],
            "owner": _owners(departments),
            "evidence_count": evidence,
            "source_report_ids": entry["source_report_ids"],
        }
        if entry["kind"] == "blocker":
            candidates.append(
                {
                    **common,
                    "kind": "blocker",
                    "impact": _blocker_impact(entry),
                    "duration": f"已持续 {entry['duration_weeks']} 周",
                    "risk_level": entry["risk_level"],
                    "blocker_id": entry["blocker_id"],
                    "weight": (
                        _risk_weight(entry["risk_level"])
                        + _breadth_weight(len(departments), evidence)
                        + min(int(entry["duration_weeks"] or 0), 12)
                    ),
                }
            )
        else:
            weeks = int(entry.get("duration_weeks") or 0)
            candidates.append(
                {
                    **common,
                    "kind": "dependency",
                    "impact": f"{evidence} 篇周报记录了该依赖，涉及 {len(departments)} 个部门",
                    # 被依赖对象的持续时长按「有多少个自然周出现过证据」算，
                    # 与卡点同一口径。0 表示来源缺周期字段，才退回文字描述。
                    "duration": f"已持续 {weeks} 周" if weeks else "本范围内持续存在",
                    "risk_level": None,
                    "blocker_id": None,
                    "weight": 20 + _breadth_weight(len(departments), evidence) + weeks,
                }
            )
    for entry in changes.get("items") or []:
        if entry["kind"] != "worsening":
            continue
        departments = entry["departments"]
        evidence = len(entry["source_report_ids"])
        candidates.append(
            {
                "kind": "worsening",
                "title": entry["title"],
                "reason": entry["detail"],
                "impact": f"未解决事项 {entry['issue_count']} 项，从上月 {entry['baseline_issues']} 项增加",
                "owner": _owners(departments),
                "duration": f"已跨 {int(entry['baseline_month'] is not None) + 1} 个月",
                "risk_level": None,
                "blocker_id": None,
                "evidence_count": evidence,
                "source_report_ids": entry["source_report_ids"],
                "weight": 30 + _breadth_weight(len(departments), evidence),
            }
        )
    for entry in anomalies.get("items") or []:
        if entry["kind"] not in {"repeated_issue", "department_growth"}:
            continue
        departments = entry["departments"]
        evidence = int(entry["evidence_count"])
        if entry["kind"] == "repeated_issue":
            candidates.append(
                {
                    "kind": "repeated_issue",
                    "title": entry["title"],
                    "reason": entry["detail"],
                    "impact": f"{entry['people_count']} 人反复提及，涉及 {len(departments)} 个部门",
                    "owner": _owners(departments),
                    "duration": f"所选范围覆盖 {len(entry['months'])} 个月",
                    "risk_level": None,
                    "blocker_id": None,
                    "evidence_count": evidence,
                    "source_report_ids": entry["source_report_ids"],
                    "weight": (
                        25
                        + _breadth_weight(len(departments), evidence)
                        + min(int(entry["people_count"]), 12)
                    ),
                }
            )
        else:
            candidates.append(
                {
                    "kind": "department_growth",
                    "title": entry["title"],
                    "reason": entry["detail"],
                    "impact": f"该部门问题事项从 {entry['series'][0]} 项增至 {entry['series'][-1]} 项",
                    "owner": _owners(departments),
                    "duration": f"观察 {len(entry['periods'])} 个周期",
                    "risk_level": None,
                    "blocker_id": None,
                    "evidence_count": evidence,
                    "source_report_ids": entry["source_report_ids"],
                    "weight": 15
                    + _breadth_weight(len(departments), evidence)
                    + min(int(entry["series"][-1]), 10),
                }
            )

    # 同一批证据会被两套分类口径各描述一次：瓶颈按「对方是谁」（院校侧反馈），
    # 异常按「问题是什么」（院校沟通与反馈）。它们不是两件事 —— 实测这两条的
    # 来源周报重合度 30/30，直接排两行会让管理层以为有两个问题要管。
    #
    # 因此按**证据重合**合并（不是按名字）：重合的那条不新增行，而是把它的
    # 另一个视角作为佐证附到已入选的条目上。名字相似度做不到这件事 ——
    # 「院校侧反馈与确认」和「院校沟通与反馈」谁都不包含谁，但证据完全一样。
    items_out: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: (-int(item["weight"]), item["title"])):
        target = _find_overlapping(items_out, candidate)
        if target is not None:
            target["corroboration"].append(
                {"kind": candidate["kind"], "label": candidate["title"], "detail": candidate["reason"]}
            )
            # 证据面更宽的那次描述留下的字段更完整（人数 / 部门数），保留更强的权重。
            target["weight"] = max(int(target["weight"]), int(candidate["weight"]))
            continue
        candidate["corroboration"] = []
        items_out.append(candidate)
    deduped = items_out
    for index, item in enumerate(deduped[:limit], start=1):
        item["rank"] = index
        item["suggested_action"] = INTERVENTION_RULE_ACTIONS.get(item["kind"], "")
        item["action_source"] = "rule"
        item.pop("weight", None)
    return {
        "items": deduped if limit is None else deduped[:limit],
        "total": len(deduped),
        "basis": (
            "只从上面三份清单里挑，不新增判断：优先级为高风险卡点 → 正在恶化 → "
            "持续越久 / 影响面越大。建议动作先给规则版，AI 总结生成后替换为模型写的版本。"
        ),
    }


def _owners(departments: list[str]) -> str:
    """责任方文字。周报里没有责任人字段，只能落到部门；没有部门就如实说不知道。"""
    if not departments:
        return "原文未明确"
    return "、".join(departments)


# 证据重合达到这个比例就认为两套分类说的是同一件事。
#
# **分母取两者中较大的那个**，不是较小的：用较小值会让一个覆盖面大的问题
# 「吸收」掉覆盖面小但完全无关的问题 —— 实测「审批与对公付款」（9 篇）与
# 「学生材料与申请进度」（15 篇）有 5 篇重合，按 min 算命中 56% 就被错误合并，
# 而按 max 只有 33%。真实重复对的分布是断开的：同义命名对（院校沟通与反馈 /
# 院校侧反馈与确认、法务与合规审核 / 法务与律师审核、合作机构与中介回复 /
# 合作机构回复、学生材料与申请进度 / 学生与客户材料）实测 0.87~1.00，
# 而非同一件事的都在 0.35 以下 —— 所以 0.6 这条线两侧都留了余量。
_OVERLAP_RATIO = 0.6
# 少于这么多篇证据时不按重合度去重：小集合的重合率没有统计意义
# （一篇周报同时提到两件事就会算出 100%）。
_OVERLAP_MIN_EVIDENCE = 3


def _find_overlapping(
    accepted: list[dict[str, Any]], candidate: dict[str, Any]
) -> dict[str, Any] | None:
    """在已入选的介入条目里找与 ``candidate`` 说的是同一件事的那条。

    两条判定，任一成立即算同一件事：
    * 名称互为包含（``_same_subject``）；
    * **来源周报重合度** ≥ 阈值（分母取较大集合）—— 这是更可靠的一条，两套分类口径
      （按对方是谁 / 按问题是什么）命名完全不同但证据完全相同的情况只靠名字发现不了。
    """
    candidate_ids = {int(value) for value in candidate.get("source_report_ids") or []}
    for existing in accepted:
        if _same_subject(candidate["title"], existing["title"]):
            return existing
        existing_ids = {int(value) for value in existing.get("source_report_ids") or []}
        if (
            len(candidate_ids) >= _OVERLAP_MIN_EVIDENCE
            and len(existing_ids) >= _OVERLAP_MIN_EVIDENCE
        ):
            overlap = len(candidate_ids & existing_ids) / max(len(candidate_ids), len(existing_ids))
            if overlap >= _OVERLAP_RATIO:
                return existing
    return None


def merge_summary_actions(block: dict[str, Any] | None, summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 AI 总结里的「建议动作」并进确定性清单（问题⑤的第二层）。

    月度建议按生成时保存的 ref 身份匹配；旧结果没有身份表时只匹配相同 ref。
    只有完全不带 ref 的旧格式才使用来源与标题匹配。**只替换模型能负责的字段**（suggested_action 与 must_decide /
    ai_reason），原因 / 影响 / 责任方 / 持续时间保持代码算出来的值 —— 模型看不到
    卡点自身的跨周记录，让它写「已持续几周」等于让它猜数字。

    没有 AI 结论时原样返回（规则版动作仍然可执行），页面据此标注来源。
    """
    if not isinstance(block, dict):
        return block
    result = (summary or {}).get("result") if isinstance(summary, dict) else None
    attention = ((result or {}).get("management_attention") or []) if isinstance(result, dict) else []
    if not attention:
        return block
    # ref 索引：`IV3` → attention 里那一条。按月 ref 匹配优先于任何启发式 ——
    # 它不会因为模型改写了标题措辞就把动作贴到别的条目上。
    by_ref: dict[str, int] = {}
    by_identity: dict[str, int] = {}
    identities = (summary or {}).get("ref_identities") or {}
    for candidate_index, candidate in enumerate(attention):
        if not isinstance(candidate, dict):
            continue
        ref = str(candidate.get("ref") or "").strip().upper()
        if ref:
            by_ref.setdefault(ref, candidate_index)
            identity = identities.get(ref) if isinstance(identities, dict) else None
            if ref.startswith("IV") and identity:
                by_identity.setdefault(str(identity).strip(), candidate_index)
    claimed: set[int] = set()
    for index, item in enumerate(block.get("items") or [], start=1):
        if by_ref:
            # 缺少该对象的建议就是未入选，不能用共有周报猜它属于另一条建议。
            # 同一份周报包含多件事；按 ref 认领的建议也必须只消费一次。
            match_index = (
                by_identity.get(monthly_item_identity(item))
                if identities else by_ref.get(f"IV{index}")
            )
        else:
            match_index = _match_attention(item, attention, claimed)
        if match_index is None or match_index in claimed:
            continue
        claimed.add(match_index)
        candidate = attention[match_index]
        action = str(candidate.get("suggested_action") or "").strip()
        if not action:
            continue
        item["suggested_action"] = action
        item["action_source"] = "ai"
        item["ai_reason"] = str(candidate.get("reason") or "").strip() or None
        item["must_decide"] = str(candidate.get("must_decide") or "").strip() or None
    return block


def monthly_item_identity(item: dict[str, Any] | None) -> str:
    """一条月度事实的**稳定身份**（业务对象名 / 部门名）。

    用来校验「模型写的那句话」是否还属于当前这一条。**不能只按位置贴文字**：
    简报过期时我们仍会展示它（第 28h 条的决定 —— 过期不等于作废），而那时页面拿到的是
    **重新算过**的清单，排序可能已经变了（共性问题按部门数 / 篇数排，新周报入库就会动）。
    实测把前两条对调后，「院校沟通与反馈」那句话被贴到了「审批与对公付款」下面 ——
    结论挂错对象比没有结论更糟。
    """
    if not isinstance(item, dict):
        return ""
    for key in ("title", "name"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def merge_monthly_narrative(
    payload: dict[str, Any] | None, summary: dict[str, Any] | None
) -> dict[str, Any] | None:
    """把 AI 写的月度简报贴到确定性 payload 上。

    **每条事实一句人话，由模型写、按 ref 贴回**（方案 A：模型说话、代码给数）。
    没有这一步时，各张卡的正文是代码拼的计数串（「7 个部门 16 人提及 已持续 2 周
    32 篇周报」）—— 项目负责人对这一版的评价是「你列出来的都是什么狗屎」。

    三条纪律：

    1. **只贴文字，不改事实**：清单条目本身逐字不动，代码算出的数字仍在下发字段里
       （页面把它排到句子下面当依据行），所以每句话都能核对。
    2. **按「身份」贴，不按位置**：生成时把 `ref → 业务对象名` 一起存下来
       （`ref_identities`），贴文字时按对象名认领。**按位置贴是错的** —— 简报过期时
       页面用的是重新算过的清单，排序可能已经变了，实测会把「院校沟通与反馈」那句话
       挂到「审批与对公付款」下面。
    3. **贴不上就承认**：`items_missing` 记下没写到的条数，页面据此如实提示，
       而不是假装每条都有结论。

    `ref_identities` 缺席（本次改动之前生成的旧简报）时退回按位置贴。是否能在页面展示，
    由总结状态决定；过期简报仍会被 stale 门槛隐藏。

    与 ``merge_summary_actions`` 一样**在路由层调用、不进缓存**：缓存的是确定性骨架，
    把 AI 结论放进去会让「模型结论变了但骨架没变」的请求拿到旧简报。
    """
    if not isinstance(payload, dict):
        return payload
    result = (summary or {}).get("result") if isinstance(summary, dict) else None
    if not isinstance(result, dict):
        return payload

    # ref -> 一句人话
    texts: dict[str, str] = {}
    department_cards: dict[str, dict[str, Any]] = {}
    for entry in result.get("items") or []:
        if not isinstance(entry, dict):
            continue
        ref = str(entry.get("ref") or "").strip().upper()
        text = " ".join(str(entry.get("text") or "").split())
        if ref and text:
            texts[ref] = text
        if ref.startswith("DP") and any(entry.get(key) for key in ("headline", "progress", "blocker")):
            department_cards[ref] = entry

    # ref -> 生成时的业务对象名（由 worker 与结果一起落库）。
    identities: dict[str, str] = {}
    raw_identities = (summary or {}).get("ref_identities") if isinstance(summary, dict) else None
    if isinstance(raw_identities, dict):
        identities = {
            str(ref).strip().upper(): str(name).strip()
            for ref, name in raw_identities.items()
            if str(ref).strip() and str(name).strip()
        }

    headline = str(result.get("headline") or "").strip()
    highlights: list[dict[str, Any]] = []
    for item in result.get("highlights") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if not title and not detail:
            continue
        kind = str(item.get("kind") or "").strip().lower()
        highlights.append(
            {
                "title": title or detail[:40],
                "detail": detail,
                "kind": kind if kind in {"progress", "risk", "decision"} else "progress",
                "source_report_ids": [
                    int(value) for value in (item.get("source_report_ids") or [])
                ],
            }
        )
    if not headline and not highlights and not texts and not department_cards:
        return payload

    # **按身份索引**：每个编号段（SI/DP/ML…）各自一张「对象名 → 那句话」表。
    # 用编号段前缀区分，避免不同卡片里同名的对象互相抢。
    text_by_identity: dict[str, dict[str, str]] = {}
    for ref, text in texts.items():
        identity = identities.get(ref)
        if identity:
            text_by_identity.setdefault(ref[:2], {}).setdefault(identity, text)

    # 逐条贴上模型写的话。贴不上就计入 `missing`，由页面如实提示。
    attached = 0
    missing = 0
    used: set[tuple[str, str]] = set()

    def _lookup(prefix: str, item: dict[str, Any]) -> str | None:
        """这一条对应的模型原话。

        有身份表时**只认身份**（位置仅供参考）：身份对不上说明这条在生成之后被重排/
        换过，宁可不贴也不能把别人的结论挂上去。没有身份表（旧简报）才退回按位置。
        """
        if text_by_identity:
            identity = monthly_item_identity(item)
            text = text_by_identity.get(prefix, {}).get(identity)
            if text and (prefix, identity) not in used:
                used.add((prefix, identity))
                return text
            return None
        return None

    def _attach(block: dict[str, Any] | None, ref_prefix: str) -> None:
        nonlocal attached, missing
        if not isinstance(block, dict):
            return
        for index, item in enumerate(block.get("items") or [], start=1):
            if not isinstance(item, dict):
                continue
            text = _lookup(ref_prefix, item)
            if text is None and not text_by_identity:
                # 旧简报（没有身份表）：沿用按位置贴
                text = texts.get(f"{ref_prefix}{index}")
            if text:
                item["ai_text"] = text
                attached += 1
            else:
                missing += 1

    for key, prefix in (
        ("mainlines", "ML"),
        ("changes", "CH"),
        ("stuck", "ST"),
        ("anomalies", "AN"),
        ("interventions", "IV"),
    ):
        _attach(payload.get(key), prefix)

    # 介入卡的兜底：模型偶尔只写了 `management_attention`（带 ref 的结构化动作）
    # 而漏掉 `items` 里的 `IV*`，那张卡的正文就会退回计数串。这里用**模型自己写的**
    # reason 顶上 —— 内容仍是模型说的，不是代码拼的。
    # 身份校验同样适用：只有 `management_attention` 那条的 ref 在身份表里对得上
    # 当前这条对象时才用（避免同样的错挂）。
    by_ref_actions = {
        str(item.get("ref") or "").strip().upper(): item
        for item in (result.get("management_attention") or [])
        if isinstance(item, dict) and str(item.get("ref") or "").strip()
    }
    for index, item in enumerate((payload.get("interventions") or {}).get("items") or [], start=1):
        if item.get("ai_text"):
            continue
        ref = f"IV{index}"
        if identities:
            identity = monthly_item_identity(item)
            fallback = next(
                (candidate for saved_ref, candidate in by_ref_actions.items()
                 if saved_ref.startswith("IV") and identities.get(saved_ref) == identity),
                None,
            )
        else:
            fallback = by_ref_actions.get(ref)
        reason = str((fallback or {}).get("reason") or "").strip()
        if reason:
            item["ai_text"] = reason
            attached += 1
            missing = max(missing - 1, 0)

    # 共性问题与部门简报是**数组**，不是 {items: [...]} 结构；同样按身份贴。
    for index, item in enumerate(payload.get("shared_issues") or [], start=1):
        text = _lookup("SI", item)
        if text is None and not text_by_identity:
            text = texts.get(f"SI{index}")
        if text:
            item["ai_text"] = text
            attached += 1
        else:
            missing += 1
    for index, brief in enumerate(payload.get("department_briefs") or [], start=1):
        card = next(
            (entry for ref, entry in department_cards.items() if identities.get(ref) == brief.get("name")),
            None,
        )
        allowed = set(((summary or {}).get("department_report_ids") or {}).get(brief.get("name")) or [])
        if card and allowed:
            source_ids = [
                int(value) for value in card.get("source_report_ids") or []
                if str(value).isdigit() and int(value) in allowed
            ]
            fields = {
                key: " ".join(str(card.get(key) or "").split())
                for key in ("headline", "progress", "blocker", "suggested_action")
            }
            if source_ids and fields["headline"] and (fields["progress"] or fields["blocker"]):
                brief["ai_card"] = {**fields, "source_report_ids": list(dict.fromkeys(source_ids))}
                attached += 1
                continue
        text = _lookup("DP", brief)
        if text is None and not text_by_identity:
            text = texts.get(f"DP{index}")
        if text:
            brief["ai_summary"] = text
            attached += 1
        else:
            missing += 1

    selected_mainlines: list[str] = []
    for ref in result.get("mainline_refs") or []:
        ref = str(ref).strip().upper()
        name = identities.get(ref) if ref.startswith("ML") else None
        if name and name not in selected_mainlines:
            selected_mainlines.append(name)
        if len(selected_mainlines) == 6:
            break

    payload["narrative"] = {
        "headline": headline,
        "change_summary": str(result.get("change_summary") or "").strip(),
        "mainline_summary": str(result.get("mainline_summary") or "").strip(),
        "mainline_names": selected_mainlines,
        "coverage_note": str(result.get("coverage_note") or "").strip() or None,
        "highlights": highlights,
        "items_written": attached,
        "items_missing": missing,
        "generated_at": (summary or {}).get("created_at"),
        "model": (summary or {}).get("model"),
    }
    return payload


def merge_monthly_department_cards(
    payload: dict[str, Any], state: dict[str, Any] | None
) -> dict[str, Any]:
    """Attach independent cards by department ID, never by position or display name."""
    items = {
        item.get("department_id"): item
        for item in (state or {}).get("items") or []
        if isinstance(item, dict) and isinstance(item.get("department_id"), int)
    }
    for brief in payload.get("department_briefs") or []:
        item = items.get(brief.get("department_id"))
        if item is None:
            continue
        brief["ai_status"] = {
            "status": item.get("status"),
            "analysis_total": item.get("analysis_total", 0),
            "analysis_completed": item.get("analysis_completed", 0),
            "analysis_failed": item.get("analysis_failed", 0),
        }
        # An old full-month result may still be attached above. Only a current
        # independent card may replace it; the UI labels historical company data.
        if item.get("status") == "current" and isinstance(item.get("card"), dict):
            brief["ai_card"] = item["card"]
            brief.pop("ai_summary", None)
    return payload


def _match_attention(
    item: dict[str, Any], attention: list[Any], claimed: set[int]
) -> int | None:
    """在 AI 的 attention 列表里找这条介入建议对应的那一项。"""
    ids = {int(value) for value in (item.get("source_report_ids") or [])}
    for index, candidate in enumerate(attention):
        if index in claimed or not isinstance(candidate, dict):
            continue
        candidate_ids = set()
        for value in candidate.get("source_report_ids") or []:
            try:
                candidate_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        if ids and candidate_ids and ids & candidate_ids:
            return index
    for index, candidate in enumerate(attention):
        if index in claimed or not isinstance(candidate, dict):
            continue
        if _same_subject(str(candidate.get("title") or ""), item.get("title")):
            return index
    return None


def _clean_excerpt(value: str | None, *, limit: int = 200) -> str:
    """把周报原文片段整理成一句可展示的引文。

    只做两件机械的事：合并空白、去掉行首的列表编号（「2、」「3.」）。编号是原文
    的排版产物，单独展示「2、新华国采合作，进度10%」会让人以为还有第 1 条被吞了。
    不改写内容 —— 引文必须与周报原文逐字可核对。
    """
    text = " ".join((value or "").split())
    text = re.sub(r"^[\s（(【\[]*(?:\d{1,2}\s*[、.．)）\]】]|[①-⑳])\s*", "", text)
    return text[:limit]


def _risk_weight(risk_level: str | None) -> int:
    """风险等级在介入优先级里的权重。高风险是「会出合规/时限问题」，单独加码。"""
    return {"高": 60, "中": 25}.get(risk_level or "", 0)


def _breadth_weight(department_count: int, evidence_count: int) -> int:
    """影响面权重：涉及多少部门 + 有多少篇周报记过它。

    两个维度都夹紧上限（部门最多算 8 个、证据最多算 30 篇），避免一个超大对象
    靠数量把所有其他事项永久挤在清单外。
    """
    return min(int(department_count), 8) * 12 + min(int(evidence_count), 30)


def _blocker_impact(entry: dict[str, Any]) -> str:
    """卡点的影响描述：不说「涉及 0 个部门」这种无信息量的话。"""
    departments = entry["departments"]
    evidence = int(entry["evidence_count"])
    parts = [f"仍未关闭，已持续 {int(entry['duration_weeks'])} 周"]
    if departments:
        parts.append(f"涉及 {len(departments)} 个部门")
    parts.append(f"{evidence} 篇周报记录过它")
    return "；".join(parts)


# 一条问题被 ≥ 这么多部门同时涉及，就不是某个部门自己的问题，而是公司级共性问题。
# 实测开发库 23 个部门条目里 19 个被 ≥3 个部门重复计数 —— 这些若留在部门卡片里，
# 会让「某部门需要关注的条目数」只反映「这个部门的人写了多少周报」，而不是它有多少麻烦。
# 这是本项目里的老毛病（瓶颈排序注释里写过「一个人写得详细会把出现次数放大」），
# 部门切分必须按同一条纪律处理，否则切出来的数字没有分辨力。
SHARED_ISSUE_MIN_DEPARTMENTS = 3

# 部门卡片里「在推」最多列几条。超过就只列未解决最多的那几条 + 「另有 N 条」：
# 部门卡片的用途是「这个部门在忙什么、卡在哪」，不是完整台账。
BRIEF_MAINLINE_LIMIT = 5
# 部门卡片里「在等谁 / 自己卡住」各最多列几条。
BRIEF_WAITING_LIMIT = 4


def _issue_departments(item: dict[str, Any] | None) -> list[str]:
    """一条结论涉及哪些部门。

    两个来源的字段名不同：卡点 / 异常 / 主线用 ``departments`` 列表，
    介入清单把部门写在 ``owner`` 文字里（中文顿号分隔）。这里统一成一个口径，
    调用方不需要知道差异；``owner`` 是「原文未明确」时返回空列表，
    不产出一个名叫「原文未明确」的幽灵部门。
    """
    if not isinstance(item, dict):
        return []
    departments = item.get("departments")
    if isinstance(departments, list) and departments:
        return [str(name).strip() for name in departments if str(name).strip()]
    owner = str(item.get("owner") or "")
    return [
        name.strip()
        for name in owner.split("、")
        if name.strip() and name.strip() != "原文未明确"
    ]


def _shared_issues(
    *,
    stuck: dict[str, Any],
    anomalies: dict[str, Any],
    interventions: dict[str, Any],
    min_departments: int = SHARED_ISSUE_MIN_DEPARTMENTS,
) -> list[dict[str, Any]]:
    """公司级共性问题：被 ≥ ``min_departments`` 个部门同时涉及的条目。

    这是本页最有价值的一块：它把「某个部门自己的麻烦」和「全公司一起面对的麻烦」
    分开。实测「院校沟通与反馈」被 7 个部门同时提到、「审批与对公付款」5 个、
    「学生材料与申请进度」4 个 —— 这些放进任何一个部门卡片都会让人以为那是该部门的
    失职，而它们其实需要公司层面的动作（定牵头方、改流程）。

    同一件事可能同时出现在卡点、异常、介入三份清单里（三套分类口径），这里按**标题**
    去重合并成一条，并保留最完整的那份字段。
    """
    buckets: dict[str, dict[str, Any]] = {}
    # **主线不进这张表**：一条主线被多个部门共同推进是正常协作（也正是它成为
    # 公司主线的原因），不是「公司级的麻烦」。实测把主线算进来会让共性问题从
    # 9 条虚高到 18 条，其中 8 条是「中介管理」「品牌宣传」这类正常运行的主线。
    sources: list[tuple[str, str, list[dict[str, Any]]]] = [
        (
            "blocker",
            "卡点",
            [item for item in (stuck.get("items") or []) if item["kind"] == "blocker"],
        ),
        (
            "dependency",
            "被依赖",
            [item for item in (stuck.get("items") or []) if item["kind"] == "dependency"],
        ),
        ("anomaly", "反复出现", anomalies.get("items") or []),
        ("intervention", "管理层介入", interventions.get("items") or []),
    ]
    for _kind, kind_label, items in sources:
        for item in items:
            departments = _issue_departments(item)
            if len(departments) < min_departments:
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            if not title:
                continue
            bucket = buckets.setdefault(
                title,
                {
                    "title": title,
                    "departments": set(),
                    "kinds": [],
                    "duration_weeks": None,
                    "evidence_count": 0,
                    "people_count": 0,
                    "source_report_ids": set(),
                    "blocker_id": None,
                    "rank": None,
                    "suggested_action": None,
                    "action_source": None,
                },
            )
            bucket["departments"].update(departments)
            if kind_label not in bucket["kinds"]:
                bucket["kinds"].append(kind_label)
            weeks = item.get("duration_weeks")
            if weeks:
                bucket["duration_weeks"] = max(int(bucket["duration_weeks"] or 0), int(weeks))
            bucket["evidence_count"] = max(
                int(bucket["evidence_count"]), int(item.get("evidence_count") or 0)
            )
            bucket["people_count"] = max(
                int(bucket["people_count"]), int(item.get("people_count") or 0)
            )
            bucket["source_report_ids"].update(
                int(value) for value in item.get("source_report_ids") or []
            )
            if not bucket["blocker_id"] and item.get("blocker_id"):
                bucket["blocker_id"] = int(item["blocker_id"])
            # 介入清单里有 rank 与建议动作，是三份里最可执行的一份，优先保留。
            rank = item.get("rank")
            if rank and (bucket["rank"] is None or int(rank) < int(bucket["rank"])):
                bucket["rank"] = int(rank)
            if item.get("suggested_action") and not bucket["suggested_action"]:
                bucket["suggested_action"] = item["suggested_action"]
                bucket["action_source"] = item.get("action_source")

    items: list[dict[str, Any]] = []
    for bucket in buckets.values():
        departments = sorted(bucket["departments"])
        items.append(
            {
                "title": bucket["title"],
                "departments": departments,
                "department_count": len(departments),
                "kinds": bucket["kinds"],
                "duration_weeks": bucket["duration_weeks"],
                "evidence_count": int(bucket["evidence_count"]),
                "people_count": int(bucket["people_count"]),
                "blocker_id": bucket["blocker_id"],
                "rank": bucket["rank"],
                "suggested_action": bucket["suggested_action"],
                "action_source": bucket["action_source"],
                "source_report_ids": sorted(bucket["source_report_ids"]),
            }
        )
    items.sort(
        key=lambda item: (
            -int(item["department_count"]),
            -int(item["people_count"]),
            -int(item["evidence_count"]),
            item["title"],
        )
    )
    # 三套分类口径会给同一件事两个名字（瓶颈按「对方是谁」→「院校侧反馈与确认」，
    # 异常按「问题是什么」→「院校沟通与反馈」）。名字谁都不包含谁，但来源周报几乎
    # 完全一样 —— 与 ``_interventions`` 用同一套按证据重合的合并，否则这张表会
    # 出现四对「看起来是两个问题、其实是一件事」的条目。
    merged: list[dict[str, Any]] = []
    for item in items:
        target = _find_overlapping(merged, item)
        if target is None:
            merged.append(item)
            continue
        target["departments"] = sorted(set(target["departments"]) | set(item["departments"]))
        target["department_count"] = len(target["departments"])
        target["kinds"] = sorted(set(target["kinds"]) | set(item["kinds"]))
        target["evidence_count"] = max(
            int(target["evidence_count"]), int(item["evidence_count"])
        )
        target["people_count"] = max(int(target["people_count"]), int(item["people_count"]))
        target["source_report_ids"] = sorted(
            set(target["source_report_ids"]) | set(item["source_report_ids"])
        )
        if not target["duration_weeks"] and item["duration_weeks"]:
            target["duration_weeks"] = item["duration_weeks"]
        if not target["suggested_action"] and item["suggested_action"]:
            target["suggested_action"] = item["suggested_action"]
            target["action_source"] = item["action_source"]
        # 另一个命名作为别名保留，页面可显示「（又称 X）」，不静默丢掉一种说法
        target.setdefault("aliases", []).append(item["title"])
    for item in merged:
        item.setdefault("aliases", [])
    merged.sort(
        key=lambda item: (
            -int(item["department_count"]),
            -int(item["people_count"]),
            -int(item["evidence_count"]),
            item["title"],
        )
    )
    return merged


def _open_blockers_by_department(
    session: Session, start: date, end: date
) -> dict[str, dict[str, Any]]:
    """每个部门「未关闭卡点」的真实计数（2026-09-28 替换旧的 ``open_issues``）。

    旧口径按主线问题行求和：不看解决状态、同一问题每被提及一次计一次、
    跨部门整桶归属（实测中亚市场自己提出的 15 行被整个记成 28 项——
    Example University 主线的 8 行同时记给了三家）。新口径直接数 Blocker 表：有生命周期
    闭环（OPEN/休眠/归档/已解决），每条都能点进卡点详情看到来源周报，
    满足项目负责人 的「卡点可溯源」。

    条件与处理中心 `tab=blockers&filter=open` + 周期筛选**完全一致**：
    OPEN + 周期内有卡点类周报证据；部门归属 = 该卡点的任务在窗口内有
    本部门员工的周报证据行（不限事项类型，与处理中心的部门筛选同口径）。
    保证部门卡徽标数字与点过去看到的列表条数相等。
    """
    period_conditions = [
        WeeklyReport.report_type == "weekly",
        non_exempt_report_condition(session),
        *report_period_overlap_conditions(start, end),
    ]
    # 周期内有卡点类证据的任务（= 处理中心 weekly_only 的口径）
    blocker_tasks = set(
        session.scalars(
            select(TaskReportLink.task_id)
            .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
            .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
            .where(ReportItem.item_type.in_(BLOCKER_ITEM_TYPES), *period_conditions)
        )
    )
    # 部门归属行：本部门员工在窗口内写过的证据行（不限事项类型），
    # 落在未关闭卡点的任务上即计入该部门。
    rows = session.execute(
        select(Department.id, Department.name, Blocker.id)
        .select_from(TaskReportLink)
        .join(Blocker, Blocker.task_id == TaskReportLink.task_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .join(Employee, Employee.id == ReportItem.employee_id)
        .join(Department, Department.id == Employee.department_id)
        .where(
            Blocker.status == BlockerStatus.OPEN,
            TaskReportLink.task_id.in_(blocker_tasks),
            *period_conditions,
        )
        .distinct()
    ).all()
    grouped: dict[str, dict[str, Any]] = {}
    for department_id, department_name, blocker_id in rows:
        entry = grouped.setdefault(
            department_name, {"department_id": int(department_id), "blocker_ids": set()}
        )
        entry["blocker_ids"].add(int(blocker_id))
    return {
        name: {"department_id": entry["department_id"], "count": len(entry["blocker_ids"])}
        for name, entry in grouped.items()
    }


def _department_briefs(
    *,
    shared_issues: list[dict[str, Any]],
    mainlines: dict[str, Any],
    changes: dict[str, Any],
    stuck: dict[str, Any],
    anomalies: dict[str, Any],
    interventions: dict[str, Any],
    open_blockers: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """部门简报：每个部门一张卡（问题①~⑤ 在部门这一侧的切片）。

    与上一版「部门 × 五列计数」的区别是本轮重构的核心：

    * **公司级共性问题不塞进部门卡片**。被 ≥``SHARED_ISSUE_MIN_DEPARTMENTS`` 个部门
      同时涉及的条目属于 ``_shared_issues``，部门卡片里不再展开（只留计数提示）——
      上一版把它们在每个部门各计一次，导致「需介入 6」的差别只反映「谁的周报写得多」
      （实测中亚市场那 6 条全是共性问题，横跨 7/5/4/4/3/3 个部门）。
    * **排序用本部门真实的未关闭卡点数**（``open_blockers``：Blocker 表 OPEN 且窗口内
      有证据的卡点，按证据行员工部门归属，与卡点处理中心同口径、可逐条溯源）。
      2026-09-28 替换旧的 open_issues 主线问题行求和——那个数字不看解决状态、
      重复提及重复计、跨部门整桶归属。

    条目内容全部来自上面已算好的结论，这里只做归属与排序，**不新增判断**。
    """
    # **按证据重合判定共性问题，不按名字**：共性问题条目与部门卡片里的条目是两套
    # 命名（「合作机构与中介回复」vs「合作机构回复」、「院校沟通与反馈」vs
    # 「院校侧反馈与确认」），名字包含关系判定不了 —— 实测「合作机构回复」不包含在
    # 「合作机构与中介回复」里（中间插了「与中介」），于是共性问题照样留在部门卡里。
    # 复用 ``_find_overlapping``（来源周报重合 ≥60%，分母取较大集合）才判得准。

    def _is_shared(title: str, report_ids: list[int]) -> bool:
        probe = {"title": title, "source_report_ids": list(report_ids)}
        return _find_overlapping(shared_issues, probe) is not None

    buckets: dict[str, dict[str, Any]] = {}

    def _bucket(name: str) -> dict[str, Any]:
        return buckets.setdefault(
            name,
            {
                "name": name,
                "mainlines": [],
                "changes": [],
                "blockers": [],
                "waiting": [],
                "anomalies": [],
                "interventions": [],
            },
        )

    for item in mainlines.get("items") or []:
        departments = item["departments"]
        for name in departments:
            _bucket(name)["mainlines"].append(
                {
                    "title": item["name"],
                    "scope_label": item["scope_label"],
                    "progress_count": int(item["progress_count"]),
                    "issue_count": int(item["issue_count"]),
                    "report_count": int(item["report_count"]),
                    "latest": item.get("latest"),
                    "source_report_ids": item["source_report_ids"],
                    # **专属还是协同**：跨部门主线会在每个参与部门的卡里都出现
                    # （实测前四张卡的头部三条全是同样的 Example University / 学生申请全流程处理），
                    # 不标注的话读者会以为四个部门在推四件不同的事，而看不出
                    # 「只有这个部门在推」的是什么。专属的排序在前。
                    "is_solo": len(departments) == 1,
                    "other_department_count": max(len(departments) - 1, 0),
                }
            )
    for item in changes.get("items") or []:
        for name in item["departments"]:
            _bucket(name)["changes"].append(
                {
                    "title": item["title"],
                    "kind": item["kind"],
                    "kind_label": item["kind_label"],
                    "tone": item["tone"],
                    "issue_count": int(item["issue_count"]),
                    "progress_count": int(item["progress_count"]),
                    "source_report_ids": item["source_report_ids"],
                }
            )
    for item in stuck.get("items") or []:
        entry = {
            "title": item["title"],
            "detail": item["detail"],
            "risk_level": item["risk_level"],
            "duration_weeks": item["duration_weeks"],
            "evidence_count": int(item["evidence_count"]),
            "blocker_id": item["blocker_id"],
            "source_report_ids": item["source_report_ids"],
            # 共性问题在部门卡片里不展开，只在结论里提示「另有 N 件公司级共性问题」
            "is_shared": _is_shared(item["title"], item["source_report_ids"]),
        }
        for name in item["departments"]:
            _bucket(name)["waiting" if item["kind"] == "dependency" else "blockers"].append(entry)
    for item in anomalies.get("items") or []:
        entry = {
            "title": item["title"],
            "kind_label": item["kind_label"],
            "tone": item["tone"],
            "people_count": int(item["people_count"]),
            "evidence_count": int(item["evidence_count"]),
            "source_report_ids": item["source_report_ids"],
            "is_shared": _is_shared(item["title"], item["source_report_ids"]),
        }
        for name in item["departments"]:
            _bucket(name)["anomalies"].append(entry)
    for item in interventions.get("items") or []:
        entry = {
            "rank": int(item["rank"]),
            "title": item["title"],
            "impact": item["impact"],
            "suggested_action": item["suggested_action"],
            "action_source": item["action_source"],
            "source_report_ids": item["source_report_ids"],
            "is_shared": _is_shared(item["title"], item["source_report_ids"]),
        }
        for name in _issue_departments(item):
            _bucket(name)["interventions"].append(entry)

    rows: list[dict[str, Any]] = []
    for name, bucket in buckets.items():
        own_mainlines = bucket["mainlines"]
        own_blockers = [entry for entry in bucket["blockers"] if not entry["is_shared"]]
        own_waiting = [entry for entry in bucket["waiting"] if not entry["is_shared"]]
        own_anomalies = [entry for entry in bucket["anomalies"] if not entry["is_shared"]]
        own_interventions = [
            entry for entry in bucket["interventions"] if not entry["is_shared"]
        ]
        # 按**条目去重**计数，不按出现次数：同一件共性问题会同时出现在卡点 / 异常 /
        # 介入三份清单里（实测同一部门会被数 3 次，17 这个数字里实际只有 6 件）。
        shared_titles_here = {
            entry["title"]
            for key in ("blockers", "waiting", "anomalies", "interventions")
            for entry in bucket[key]
            if entry["is_shared"]
        }
        shared_count = len(shared_titles_here)
        # 结论句与排序的主数字：本部门参与的未关闭卡点数（Blocker 表真实口径，
        # 与卡点处理中心同条件、可点进去逐条溯源）。不用主线问题行求和 ——
        # 那个数字不看解决状态且跨部门整桶归属（2026-09-28 项目负责人 追问口径后废弃）。
        blocker_stat = open_blockers.get(name) or {}
        open_blocker_count = int(blocker_stat.get("count") or 0)
        # 专属的排前面：部门卡要回答「这个部门自己在做什么」，而不是先把
        # 与别的部门共同推进的事（那些在每张卡里都一样）铺满。
        own_mainlines.sort(
            key=lambda entry: (
                0 if entry["is_solo"] else 1,
                -int(entry["issue_count"]),
                -int(entry["progress_count"]),
                entry["title"],
            )
        )
        solo_count = sum(1 for entry in own_mainlines if entry["is_solo"])
        own_waiting.sort(key=lambda entry: (-int(entry["duration_weeks"] or 0), entry["title"]))
        own_blockers.sort(key=lambda entry: (-int(entry["duration_weeks"] or 0), entry["title"]))
        parts = [f"在推 {len(own_mainlines)} 条主线（{solo_count} 条只有本部门）"]
        if open_blocker_count:
            parts.append(f"{open_blocker_count} 项卡点未关闭")
        if own_waiting:
            parts.append(f"{len(own_waiting)} 件事在等别人")
        if shared_count:
            parts.append(f"{shared_count} 件属公司级共性问题")
        if not own_mainlines and not own_waiting:
            parts.append("所选范围内没有已分析的公司主线")
        rows.append(
            {
                "name": name,
                "department_id": blocker_stat.get("department_id"),
                "conclusion": "，".join(parts) + "。",
                "open_blockers": open_blocker_count,
                "shared_issue_count": shared_count,
                "mainlines": own_mainlines[:BRIEF_MAINLINE_LIMIT],
                "mainline_total": len(own_mainlines),
                "solo_mainline_count": solo_count,
                "blockers": own_blockers[:BRIEF_WAITING_LIMIT],
                "waiting": own_waiting[:BRIEF_WAITING_LIMIT],
                "changes": bucket["changes"],
                "anomalies": own_anomalies,
                "interventions": own_interventions,
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row["open_blockers"]),
            -len(row["waiting"]),
            -int(row["shared_issue_count"]),
            row["name"],
        )
    )
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def monthly_view(
    session: Session, start: date, end: date, *, department_id: int | None = None
) -> dict[str, Any]:
    """每月视图：按管理层的五个问题组织数据（见本节顶部的对照表）。

    与旧实现的差别：不再逐月输出「工作主题」与「部门逐月计数」——那两块合计
    占了页面大半篇幅，却只是把事项换个维度重排（用户反馈「信息含量太低」）。
    现在每月只保留篇数与覆盖率（判断可信度的前提），信息量都放在五个问题上。

    **先算完整清单再切片**：部门索引要按部门过滤全部条目，若先按各块的 limit 截断，
    「该部门有 N 条」会只数到公司前几条里碰巧属于它的那些。这与 ``_bottlenecks`` 的
    既有做法一致（共享完整计算，limit 只在返回时截断）。
    """
    months = _iter_months(start, end)
    coverage_by_month = _month_coverage(session, months)
    month_payload = [
        {
            "month": key,
            "start": month_start.isoformat(),
            "end": month_end.isoformat(),
            "label": f"{month_start.year} 年 {month_start.month} 月",
            "short_label": f"{month_start.month} 月",
            "report_total": int(coverage_by_month.get(key, {}).get("reports_total", 0)),
            "analyzed_reports": int(coverage_by_month.get(key, {}).get("analyzed_reports", 0)),
            "coverage": float(coverage_by_month.get(key, {}).get("ratio", 0.0)),
        }
        for month_start, month_end, key in months
    ]

    def _slice(block: dict[str, Any], limit: int) -> dict[str, Any]:
        return {**block, "items": block["items"][:limit]}

    full_mainlines = _mainlines(session, start, end, months, limit=None)
    full_changes = _monthly_changes(session, start, end, months, coverage_by_month, limit=None)
    full_stuck = _stuck_work(session, start, end, limit=None)
    full_anomalies = _anomalies(session, start, end, limit=None)
    full_interventions = _interventions(
        stuck=full_stuck, changes=full_changes, anomalies=full_anomalies, limit=None
    )
    # **共性问题与部门卡片只能引用「页面上真的列出来」的那几条介入**：介入清单按
    # 产品要求只展示前 ``INTERVENTION_LIMIT`` 条且不提供「展开全部」，而共性问题块
    # 与部门卡片的写法是「已列入『需介入』#N，动作在那里」。用完整清单算 rank 会让
    # 第 6 条及以后指向一个页面上不存在的名次（实测出现「已列入 #6 / #7」，而那块
    # 只有 5 条）—— 读者按名次去找必然找不到，比不给指针更糟。
    visible_interventions = _slice(full_interventions, INTERVENTION_LIMIT)
    shared_issues = _shared_issues(
        stuck=full_stuck,
        anomalies=full_anomalies,
        interventions=visible_interventions,
    )
    department_briefs = _department_briefs(
        shared_issues=shared_issues,
        mainlines=full_mainlines,
        changes=full_changes,
        stuck=full_stuck,
        anomalies=full_anomalies,
        interventions=visible_interventions,
        open_blockers=_open_blockers_by_department(session, start, end),
    )
    # A brief without an open blocker still belongs to a real department. The
    # old blocker-only ID prevented it from receiving its own AI card/status.
    department_ids_by_name: dict[str, list[int]] = defaultdict(list)
    for department_id, name in session.execute(select(Department.id, Department.name)):
        department_ids_by_name[name].append(int(department_id))
    for brief in department_briefs:
        ids = department_ids_by_name.get(brief["name"], [])
        brief["department_id"] = ids[0] if len(ids) == 1 else None

    def _mark_shared(block: dict[str, Any], limit: int) -> dict[str, Any]:
        """给条目打上「已归入公司级共性问题」的标记，**但不移出清单**。

        早先的做法是把它们移出去，结果「卡住了」只剩 3 条、「异常」只剩 1 条 ——
        管理者扫一眼会以为这两块没事，而真相是 6/9 和 8/9 都被移走了。
        **计数必须诚实**：条目留在原清单里（数字照实），前端把已归入共性问题的那几条
        压成一行指针（不重复部门列表、人数、篇数），细节统一在共性问题块读。
        """
        marked = [
            {**item, "is_shared": _find_overlapping(shared_issues, item) is not None}
            for item in block["items"]
        ]
        return {**block, "items": marked[:limit]}

    # **公司主线只留跨部门的**：项目负责人对问题①的原话是「直接展示 5~8 条真正的
    # 跨部门/跨周期工作主线」。单部门主线属于那个部门自己的事，放在部门简报里
    # （否则同一件事在公司层与部门层各出现一次，正是「冗长」的来源）。
    # 完整清单仍算出来供部门简报使用，只是公司层这一块按跨部门过滤后展示。
    cross_mainlines = [
        item for item in full_mainlines["items"] if item["is_cross_department"]
    ]
    company_mainlines = {
        **full_mainlines,
        "items": cross_mainlines[:MAINLINE_LIMIT],
        "total": len(cross_mainlines),
        "single_department_total": full_mainlines["total"] - len(cross_mainlines),
    }

    return {
        "months": month_payload,
        "mainlines": company_mainlines,
        "changes": _slice(full_changes, CHANGE_LIMIT),
        "stuck": _mark_shared(full_stuck, STUCK_LIMIT),
        "anomalies": _mark_shared(full_anomalies, ANOMALY_LIMIT),
        # **介入清单不过滤**：它回答的是「领导要做什么」，公司级问题恰恰是最需要
        # 领导拍板的（院校沟通与反馈牵动 7 个部门）。把它过滤掉就本末倒置了。
        # 与共性问题的重复由前端处理：凡已列入介入的条目在共性问题块里只给指针。
        "interventions": _slice(full_interventions, INTERVENTION_LIMIT),
        "shared_issues": shared_issues,
        # **刻意不叫 `departments`**：那个键在已裁掉的部门视图里是另一种形状
        # （OperatingDepartmentCard[]），复用会让「同名不同形」整页崩的历史重演。
        "department_briefs": department_briefs,
        # **顶部覆盖数 = 逐月明细之和**，按构造保证一致。
        #
        # 原先顶部走 `_coverage`（周期**与区间有重叠**即计入），逐月走 `_month_coverage`
        # （按周期**结束日**归到唯一一个月）—— 两个口径在跨月的那一周必然分歧：
        # 实测「上月」档顶部显示 154 篇，而下面逐月明细只有 123 篇，差 31 篇正好是
        # 8/31~9/6 那一周的周报（它们的结束日在 9 月，不算进 8 月）。
        # 同一页两个数字打架，读者只能两个都不信。
        #
        # 月度视图的口径以逐月为准：它的「篇数」是「这个自然月里结束的周期」。
        "coverage": _monthly_coverage_sum(month_payload),
    }




# =============================================================================
# 综合视图（单页看板）：全部由确定性规则计算，不调模型
#
# 三条纪律，与方案第 7 节的「必须由代码计算」一致：
#   1. 每个数字都能追到证据（周报 ID / 事项 ID），不做文案推测。
#   2. 只有 ≥2 条证据的关系才升级成「瓶颈 / 摩擦 / 持续」；单次提及不进结论区。
#   3. 上一周期没有完成分析的模块**不给环比**（见 ``_period_delta``）——
#      拿「零」当基线会得出「本期暴涨」这种与事实相反的结论。
# =============================================================================

# 依赖对象归一表：把「依赖办公室确认」「等待办公室发布」这类表述收敛成同一个
# 被依赖对象，才能回答「谁在拖慢最多项目」。顺序即优先级，前面的先匹配。
# 只做规则匹配、不调模型：归一错了会让管理者去找错部门，必须可复核 ——
# 返回值里带 matched_keywords 与来源事项，页面能展开核对。
DEPENDENCY_TARGET_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("办公室确认与发布", ("办公室",)),
    ("法务与律师审核", ("法务", "律师", "合规")),
    ("审批与对公付款", ("审批", "对公汇款", "需求单", "汇款", "报销", "付款到账")),
    ("服务器与设备保障", ("服务器", "电脑", "笔记本", "设备", "token", "订阅", "工位")),
    # 按「对方是谁」分类，不按「对方在哪一行」。院校与机构分列：
    # 一个是校方，一个是中介/顾问，两者的推进方式完全不同。
    ("院校侧反馈与确认", ("院校", "大学", "学校", "校方", "学院")),
    ("合作机构回复", ("机构", "中介", "伙伴", "合作方", "顾问")),
    ("招生与应用团队协作", ("招生团队", "申请部", "申请团队", "admissions team", "招生部")),
    ("学生与客户材料", ("学生", "客户", "监护", "材料")),
)

# 「资源需求：」是事项标题的前缀（由解析层加上去），不是需求内容本身。
# 匹配依赖对象前必须剥掉，否则每条资源需求都会因为前缀里的「资源」二字
# 被误判成「服务器与设备保障」（实测这一步能把 10 项虚高打回真实分类）。
_RESOURCE_PREFIX = re.compile(r"^资源需求\s*[:：]\s*")

# 比较事项标题时先剥离的字符：空白与标点。中文汉字与字母数字都保留 ——
# 两件事是不是同一件，靠剩下的字符二元组判断（见 ``_is_same_work_item``）。
_NON_WORD = re.compile(r"[\s\W_]+", re.UNICODE)

# 精力投向的归类规则。回答的是「公司的注意力主要在哪几类事上」，所以按业务职能
# 归类而不是按标题相似度聚类。**顺序即优先级，先命中先归类** —— 例如
# 「俄罗斯市场招聘」同时含「市场」与「招聘」，它实际是人员招聘，故招聘规则更靠前。
FOCUS_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("行政与合规事务", ("行政", "人事", "财务", "法务", "合规", "合同", "盖章", "oa", "审批", "报销", "发票", "手册", "交接", "办公", "采购", "社保")),
    ("招聘与团队建设", ("招聘", "面试", "候选人", "简历", "入职", "团队")),
    ("内部系统与数字化", ("系统", "官网", "网站", "平台", "ai", "工具", "数据", "服务器", "sop", "流程", "看板", "信息化", "开发", "需求文档", "技术")),
    ("国际医疗与展会", ("医疗", "医院", "患者", "就医", "科技游", "健康", "转诊", "医生", "展会", "会展")),
    ("学生申请交付", ("学生", "申请", "材料", "录取", "签证", "入读", "退费", "监护", "教务", "成绩单", "注册")),
    ("院校合作与项目交付", ("院校", "大学", "学校", "校方", "学院", "合作", "预科", "课程", "项目", "伙伴", "签约", "协议", "实习", "夏令营", "秋令营", "中介")),
    ("招生与市场拓展", ("招生", "市场", "渠道", "机构", "宣传", "品牌", "推广", "拜访", "咨询", "提案", "洽谈", "销售", "宣讲", "中介", "代表处")),
)

# 持续问题的归类规则。与精力投向不同：这里要能跨周匹配同一个「问题主题」，
# 所以按问题本身的语义归类，且要覆盖资源需求类文案（它们是唯一跨周存在的问题证据）。
ISSUE_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("办公设备与工具账号", ("电脑", "笔记本", "token", "canva", "capcut", "设备", "订阅", "账号", "工位")),
    ("法务与合规审核", ("法务", "律师", "合规", "协议审核", "合同审核")),
    ("审批与对公付款", ("审批", "汇款", "需求单", "报销", "发票", "付款", "到账")),
    ("院校沟通与反馈", ("院校", "大学", "学校", "校方", "学院")),
    ("合作机构与中介回复", ("机构", "中介", "伙伴", "合作方", "顾问", "渠道")),
    ("学生材料与申请进度", ("学生", "材料", "监护", "录取", "签证", "退费", "入读")),
    ("培训与资料支持", ("培训", "资料", "手册", "展会", "会展")),
    ("人力与招聘支持", ("招聘", "人手", "人力", "简历", "面试")),
)

# 依赖类事项：这些 item_type 表达「被什么挡住」，是瓶颈模块的输入。
DEPENDENCY_ITEM_TYPES = {"dependencies", "blockers", "problems", "risks"}


def _is_noise_item(item: ReportItem) -> bool:
    """「没有需求」这类记录不构成任何问题，不能进瓶颈与趋势。

    复用的是解析层同一份哨兵表（``is_empty_resource_demand``）：
    「资源需求：Discuss later」「目前暂时不需要公司提供额外支持」是员工没填或明确
    说不需要，把它们算成公司被拖慢的原因等于凭空造问题（开发库实测长期问题全是这种）。
    """
    blob = f"{item.title or ''} {item.description or ''}"
    if (item.task_key or "").startswith(RESOURCE_DEMAND_TASK_PREFIX) or "资源需求" in blob:
        return is_empty_resource_demand(item.title or item.description or "")
    return False


# 汇总型事项的表述特征：一条事项把「好几件事都等着同一个对象」合并写出来。
# 它是有价值的证据（说明影响面广），但**不是一条独立的工作主线** ——
# 它列举的那些事项往往在别处已经各自记过一条，直接计数会把影响面算成两倍。
# 只在**没有业务对象**时才这样判：有业务对象的事项身份就是那个对象，
# 描述里出现「多项材料」不改变它属于某个项目这一事实。
_AGGREGATE_MARKERS = ("多项", "若干", "等均", "等多个", "等均等待")


def _is_aggregate_item(item: ReportItem) -> bool:
    text = f"{item.title or ''} {item.description or ''}"
    return any(marker in text for marker in _AGGREGATE_MARKERS)


def _task_project_index(rows: Iterable[Any]) -> dict[int, str]:
    """task_id → 该任务下出现过的业务对象名。

    一条 Task 的条目里，往往只有一部分填了 ``related_project``（模型在同一任务的
    不同事项上填得不一致，实测 75 个依赖任务里有 5 个如此）。身份必须**先按整条
    任务定一次**，再分配给它的每条条目 —— 逐条事项各自判断会出两个问题：

    * **同一任务被算成两条主线**：填了对象的那条产出 ``project:院校拓展``，
      没填的那条退回 ``task:239``，两条键指向同一件事，影响面凭空 +1
      （实测院校侧 19 → 实为 18，法务 6 → 5，招生协作 3 → 2）。
    * **同一项目显示成两个名字**：项目进展里一条按对象名、一条按任务标题，
      同一件事在两个条目下。

    取该任务下**出现次数最多**的对象名；并列时取字典序最小的，保证同一份数据
    每次算出的结果一致（看板必须可复现）。
    """
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _item, _report, _employee, _department, task_id in rows:
        if task_id is None:
            continue
        project = (_item.related_project or "").strip()
        if project:
            counts[int(task_id)][project] += 1
    return {
        task_id: sorted(projects.items(), key=lambda entry: (-entry[1], entry[0]))[0][0]
        for task_id, projects in counts.items()
        if projects
    }


def _workstream_key(
    item: ReportItem,
    task_id: int | None,
    task_projects: dict[int, str] | None = None,
) -> str | None:
    """一条事项归属的工作主线键。

    优先业务对象（``related_project``）：它是稳定的、可跨人跨周复用的业务实体
    （学校、项目、系统）。事项自己没填时，用**所属任务**已确定的业务对象兜底
    （见 ``_task_project_index``），这样同一任务不会既产出 ``project:`` 又产出
    ``task:`` 两个键。只有整条任务都没有业务对象时才退回 task id。
    **刻意不用事项标题兜底** —— 那会让「一件事被写成三个近义标题」冒充三条主线，
    把影响面凭空放大（实测「院校反馈」会因此从 7 项虚高到 20 项）。

    既没有业务对象、任务也没有、又是汇总型表述的事项返回 None，由调用方计入
    unattributed：如实标注「这条依赖无法归属到单一主线」，而不是让它假装成一条新主线。
    """
    project = (item.related_project or "").strip()
    if project:
        return f"project:{project}"
    if task_id is not None:
        inherited = (task_projects or {}).get(int(task_id))
        if inherited:
            return f"project:{inherited}"
        if not _is_aggregate_item(item):
            return f"task:{int(task_id)}"
    return None


# 成果 / 进行中类型：用于判断一条主线本期是否真的往前走。
PROGRESS_ITEM_TYPES = {"completed_items", "results"}
ONGOING_ITEM_TYPES = {"ongoing_items", "next_week_plans"}

# 单条关系至少要有几条证据才算「瓶颈」；1 条只能算一次提及，不是瓶颈。
BOTTLENECK_MIN_EVIDENCE = 2

# 环比与趋势的**可比性门槛**。只判「有没有分析数据」远远不够：
# 实测上期 27 篇里只有 5 篇被分析（19%）、本期 24 篇全部被分析（100%），
# 拿 5 篇的绝对数与 24 篇比大小，每个问题主题都会显示成「连续扩大」——
# 那是「这一周多分析了 19 篇」，不是问题在变多。同一个根因还会让概要卡报出
# 「工作主线 +50」这种数字。两侧覆盖率都达标、且差值不大，才允许出数字。
COMPARABLE_MIN_COVERAGE = 0.8
COMPARABLE_MAX_COVERAGE_GAP = 0.2

# 趋势口径说明。写在后端由前端原样渲染 —— 两处各写一句会让人以为统计方式不同。
# 阈值插进来而不是再写一遍 80%：两处各写一个数字迟早对不上。
TREND_BASIS = (
    "按「问题主题 × 周期」统计提到该主题的周报篇数；"
    f"只统计分析覆盖达到 {COMPARABLE_MIN_COVERAGE:.0%} 的周期，"
    "覆盖不足的周期不参与比较（否则「多分析了几篇」会被读成「问题在变多」）。"
)
# 「工作主线」的准入门槛：同一业务对象本期至少出现这么多条事项才算非一次性提及。
# 门槛写在这里而不是散在各处，指标卡、项目列表与提示词都用它。
WORKSTREAM_MIN_ITEMS = 2

# 综合视图里各列表的返回上限。
# 精力投向**不在其中**：它的类别数由 FOCUS_CATEGORY_RULES 决定（8 个以内），
# 截断只会让真实类别被并进「其他工作」而显示错（见 _focus_distribution）。
BOTTLENECK_LIMIT = 6
PROJECT_LIMIT = 6
TREND_LIMIT = 6
# 趋势看板使用更长的历史窗口，复用已有结构化分析结果；不触发新的 AI 分析。
# 17 周覆盖当前已入库的历史周报，低覆盖周期仍由 _issue_trends 过滤掉。
TREND_LOOKBACK_WEEKS = 17
SIGNAL_LIMIT = 5
# 单个依赖对象最多带几条来源，避免页面与提示词体积失控。
DEPENDENCY_EVIDENCE_LIMIT = 4


def _memo(session: Session, key: Any, factory: Any) -> Any:
    """一次视图调用内的记忆化。

    瓶颈、摩擦、信号、项目四块都要同一批事项明细，不缓存会把同一条查询打四五遍。
    落到 ``request_memo`` 上，因此**任何写入都会让缓存失效** —— 同一 Session 被复用
    （测试与脚本里常见）时，插入数据后再调用不会拿到写入前的旧数字。
    ``_reset_memo`` 在 public 视图入口清空，保证每次「打开一次视图」都从干净状态开始。
    """
    return request_memo.memoize(session, (_MEMO_KEY, key), factory)


def _reset_memo(session: Session) -> None:
    request_memo.invalidate(session)


# 与 request_memo 的键空间分开：这里的每一条都带视图入口的显式重置语义。
_MEMO_KEY = "operating_dashboard.memo"


def _match_rule(text: str, rules: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[str, str] | None:
    """按规则表匹配标签；返回 (标签, 命中的关键词)。没有命中返回 None。"""
    haystack = _RESOURCE_PREFIX.sub("", text or "").lower()
    if not haystack:
        return None
    for label, keywords in rules:
        for keyword in keywords:
            if keyword in haystack:
                return label, keyword
    return None


def _issue_category(text: str) -> tuple[str, str] | None:
    """把一条问题事项归到一个稳定的问题主题上（长期趋势用）。"""
    return _match_rule(text, ISSUE_CATEGORY_RULES)


def _focus_category(text: str) -> tuple[str, str]:
    """把一条工作事项归到一类业务职能上；无法归类时进「其他工作」。"""
    matched = _match_rule(text, FOCUS_CATEGORY_RULES)
    return matched if matched else ("其他工作", "")


def _period_sequence(end: date, weeks: int) -> list[tuple[date, date]]:
    """以 ``end`` 结尾、连续 ``weeks`` 个自然周（含 end 所在周）。"""
    aligned_end = end
    return [
        (aligned_end - timedelta(days=7 * (index + 1) - 1), aligned_end - timedelta(days=7 * index))
        for index in range(weeks - 1, -1, -1)
    ]


def _period_item_rows(
    session: Session, start: date, end: date, *, department_id: int | None = None
) -> list[tuple[ReportItem, WeeklyReport, Employee | None, Department | None, int | None]]:
    """本期全部事项行（含资源需求类）。

    与 ``_theme_rows`` 的差别：这里**不排除资源需求、也不要求 Task 关联**。
    综合视图要看「本期出现过哪些问题」，而资源需求恰好是唯一跨周存在问题证据，
    在 ``_theme_rows`` 里被刻意排除（那条排除是给「主要工作方向」用的）。

    ``department_id`` 供部门作用域复用同一套口径（部门的 AI 结论要按部门过滤）。
    """
    def _load() -> list[tuple[ReportItem, WeeklyReport, Employee | None, Department | None, int | None]]:
        conditions = [
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        ]
        if department_id is not None:
            conditions.append(Employee.department_id == department_id)
        rows = session.execute(
            select(ReportItem, WeeklyReport, Employee, Department, TaskReportLink.task_id)
            .select_from(ReportItem)
            .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
            .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .join(TaskReportLink, TaskReportLink.report_item_id == ReportItem.id, isouter=True)
            .where(*conditions)
            .order_by(ReportItem.id)
        )
        return list(rows)

    return _memo(session, ("item_rows", start, end, department_id), _load)


def _bottlenecks(
    session: Session,
    start: date,
    end: date,
    *,
    limit: int = BOTTLENECK_LIMIT,
    department_id: int | None = None,
) -> dict[str, Any]:
    """最大运行瓶颈：按「被依赖对象」聚合，而不是按出现次数排序。

    排序依据是**影响的主线数**，其次才持续周期与涉及人数 —— 一个人写得详细
    会把出现次数放大，按次数排会让「话多」压过「影响面大」。

    ``limit`` 只在返回时截断：完整聚合按 (start, end, 部门) 在一次请求内算一遍。
    同一次请求里有两个消费者（总览只列 6 条、信号排序要 20 条做筛选），
    它们要的是同一份聚合，重算一遍就是白等一遍。
    """
    full = _memo(
        session,
        ("bottlenecks", start, end, department_id),
        lambda: _build_bottlenecks(session, start, end, department_id=department_id),
    )
    # 在共享聚合的切片上打标。截断位置与 severity 都只由条目自身内容决定，
    # 所以两次调用对同一条目写出的值是同一个，就地打标不会让先返回的那份变样。
    items = full["items"][:limit]
    for index, item in enumerate(items, start=1):
        item["rank"] = index
        item["severity"], item["severity_tone"] = _severity_for_bottleneck(item)
    return {**full, "items": items, "total": len(items)}


def _build_bottlenecks(
    session: Session, start: date, end: date, *, department_id: int | None = None
) -> dict[str, Any]:
    """按 (周期, 部门) 的完整瓶颈聚合（不截断），交由 ``_bottlenecks`` 切片。"""
    rows = _period_item_rows(session, start, end, department_id=department_id)
    # 身份先按整条任务定一次，再分配给它的每条事项：否则同一任务里「填了业务对象」
     # 与「没填」的两条会各自产出一个主线键，把影响面算大（见 _task_project_index）。
    task_projects = _task_project_index(rows)
    groups: dict[str, dict[str, Any]] = {}
    unattributed = 0
    for item, report, employee, department, task_id in rows:
        if item.item_type not in DEPENDENCY_ITEM_TYPES:
            continue
        if _is_noise_item(item):
            continue
        text = f"{item.title or ''} {item.description or ''}"
        matched = _match_rule(text, DEPENDENCY_TARGET_RULES)
        if matched is None:
            continue
        label, keyword = matched
        bucket = groups.setdefault(
            label,
            {
                "target": label,
                "matched_keywords": set(),
                "workstreams": set(),
                "departments": set(),
                "employees": set(),
                "report_ids": set(),
                "task_ids": set(),
                "items": [],
                "unattributed": 0,
            },
        )
        if keyword:
            bucket["matched_keywords"].add(keyword)
        workstream = _workstream_key(item, task_id, task_projects)
        if workstream is None:
            # 既没有业务对象也没有任务：无法归属，如实计数并在返回值里标注，
            # 不静默丢弃、也不算进影响面。
            bucket["unattributed"] += 1
            unattributed += 1
        else:
            bucket["workstreams"].add(workstream)
        if task_id is not None:
            bucket["task_ids"].add(int(task_id))
        bucket["report_ids"].add(report.id)
        if department is not None:
            bucket["departments"].add(department.name)
        if employee is not None:
            bucket["employees"].add(employee.name)
        if len(bucket["items"]) < DEPENDENCY_EVIDENCE_LIMIT:
            bucket["items"].append(
                {
                    "id": item.id,
                    "title": item.title,
                    "description": (item.description or "")[:EVIDENCE_SNIPPET_LIMIT],
                    "status": item.status,
                    "item_type": item.item_type,
                    "risk_level": item.risk_level,
                    "project": item.related_project,
                    "report_id": report.id,
                    "employee_name": employee.name if employee else None,
                    "department_name": department.name if department else None,
                    "period_start": report.start_date.isoformat() if report.start_date else None,
                    "period_end": report.end_date.isoformat() if report.end_date else None,
                    "occurred_on": report.end_date.isoformat() if report.end_date else None,
                }
            )

    items: list[dict[str, Any]] = []
    for bucket in groups.values():
        evidence_count = len(bucket["report_ids"])
        workstream_count = len(bucket["workstreams"])
        # 准入：≥2 篇周报提到，**或者**已经挡到 ≥2 项工作。后者是为了
        # 「一个人在一篇周报里报告四个项目都被同一件事挡住」这种情况 ——
        # 它只有 1 篇证据，但影响面确实跨了多个项目，漏掉才是失真。
        if evidence_count < BOTTLENECK_MIN_EVIDENCE and workstream_count < 2:
            continue
        items.append(
            {
                "target": bucket["target"],
                "workstream_count": workstream_count,
                "department_count": len(bucket["departments"]),
                "employee_count": len(bucket["employees"]),
                "evidence_count": evidence_count,
                "matched_keywords": sorted(bucket["matched_keywords"]),
                "departments": sorted(bucket["departments"]),
                "employees": sorted(bucket["employees"]),
                "sources": bucket["items"],
                "source_report_ids": sorted(bucket["report_ids"]),
                "workstream_ids": sorted(bucket["task_ids"]),
                "unattributed_count": bucket["unattributed"],
            }
        )
    items.sort(
        key=lambda item: (
            -int(item["workstream_count"]),
            -int(item["department_count"]),
            -int(item["evidence_count"]),
            item["target"],
        )
    )
    return {
        "items": items,
        "total": len(items),
        # 无法归属到业务对象也拿不到任务的事项。如实计数：既不算影响面，也不装作没发生。
        "unattributed_count": unattributed,
    }


def _severity_for_bottleneck(item: dict[str, Any]) -> tuple[str, str]:
    """瓶颈等级只由「影响面」决定，不由措辞决定。"""
    if item["workstream_count"] >= 4 or item["department_count"] >= 3:
        return "高影响", "red"
    if item["workstream_count"] >= 2 or item["department_count"] >= 2:
        return "中高影响", "amber"
    return "中影响", "amber"


# 「疑似重复推进」的**反向**规则：这些名字是**活动类别**，不是业务对象。
# 每个部门都会招聘、都要开会、都有报销 —— 两个部门都写了「招聘」是正常的，
# 不是「同一件事被做了两遍」。实测当期 20 条重复提醒里有 13 条是这类误报
# （招聘 / 周例会 / 费用报销 / 部门管理 / 网站 / 品牌宣传…），误报率 65%。
# 判定放在读取侧而不是让模型少输出：模型的 `related_project` 在个别时候就是
# 会落到类别上，这里兜住比反复改提示词更可靠，也与本模块「读取侧过滤」的既有做法一致。
ACTIVITY_CATEGORY_MARKERS = (
    "会议",
    "例会",
    "周会",
    "内部协作",
    "部门管理",
    "团队管理",
    "招聘",
    "面试",
    "入职",
    "培训",
    "报销",
    "费用",
    "票据",
    "合同归档",
    "文件归档",
    "资料整理",
    "筛选",
    "拓展",
    "宣传",
    "品牌",
    "网站",
    "官网",
    "流程",
    "管理",
    "板块",
    "支持",
    "参与",
    "跟进",
    "沟通",
)


def _is_activity_category(name: str) -> bool:
    """业务对象名是不是一个「活动类别」。

    命中即排除出重复推进的候选：类别内部各做各的事是组织常态，把它报成
    「疑似重复」会让这个模块每周产出十几条假警，真正的重复（如两个部门
    各自办同一场展会）被淹没。
    """
    compact = _NON_WORD.sub("", (name or "").strip())
    if not compact:
        return True
    return any(marker in compact for marker in ACTIVITY_CATEGORY_MARKERS)


def _duplicates(rows: list[Any], *, start: date, end: date) -> list[dict[str, Any]]:
    """疑似重复投入：同一**业务对象**、同一周期、**不同部门**在推进同一件事。

    只提示「建议核对分工」，不说「重复劳动」—— 周报无法证明两边的实际工作量，
    也无法证明两人之间没沟通过。

    三道门槛，每一道都对应实测过的误报：

    1. **必须是跨部门**。同一部门两人各写几条同一项目是正常分工，
       算成重复会每周报假警。
    2. **名字必须是业务对象，不是活动类别**（``_is_activity_category``）。
       「招聘」「周例会」「费用报销」这类是每个部门都会有的常规活动，
       两个部门都提到它们是必然的，不构成「重复投入」。
    3. **两侧必须在描述同一件事**（``_is_same_work_item``）。共用业务对象名
       不等于同一件事：实测「大连枫叶申请」下的 4 个部门分别在推广项目、
       做宣传册、做市场经理培训、跟进学生申请 —— 标准分工，被报成了重复。
    """
    grouped: dict[str, dict[str, Any]] = {}
    for item, report, employee, department, _task_id in rows:
        if item.item_type in {"dependencies"}:
            continue  # 依赖不是「推进动作」，不参与重复判定
        if _is_noise_item(item):
            continue
        name = (item.related_project or "").strip()
        if not name:
            continue
        if _is_activity_category(name):
            continue
        bucket = grouped.setdefault(
            name,
            {"project": name, "departments": set(), "employees": set(), "items": [], "report_ids": set()},
        )
        bucket["report_ids"].add(report.id)
        if department is not None:
            bucket["departments"].add(department.name)
        if employee is not None:
            bucket["employees"].add(employee.name)
        if len(bucket["items"]) < DEPENDENCY_EVIDENCE_LIMIT:
            bucket["items"].append(
                {
                    "id": item.id,
                    "title": item.title,
                    "status": item.status,
                    "item_type": item.item_type,
                    "report_id": report.id,
                    "employee_name": employee.name if employee else None,
                    "department_name": department.name if department else None,
                    "period_start": report.start_date.isoformat() if report.start_date else None,
                    "period_end": report.end_date.isoformat() if report.end_date else None,
                    "occurred_on": report.end_date.isoformat() if report.end_date else None,
                    "source_text": (item.source_text or item.description or "")[:EVIDENCE_SNIPPET_LIMIT],
                }
            )

    # 同一件事被多个部门推进时，先看它是不是**同一个任务的协作**：
    # 只要有一个任务同时覆盖了两个部门，就是已存在协作关系，不是「疑似重复」。
    # 反之（例如两个部门各自建了一个任务做同一场展会）才值得提示核对分工 ——
    # 这正是「湾一会国际医疗展会」的真实情况。
    task_departments_by_project: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for item, _report, _employee, department, task_id in rows:
        name = (item.related_project or "").strip()
        if not name or task_id is None or department is None:
            continue
        task_departments_by_project[name][int(task_id)].add(department.name)

    results: list[dict[str, Any]] = []
    for bucket in grouped.values():
        # 跨部门是硬门槛：同一部门内多人做同一项目属于正常分工。
        if len(bucket["departments"]) < 2:
            continue
        shared = any(
            len(depts) >= 2
            for depts in task_departments_by_project.get(bucket["project"], {}).values()
        )
        if shared:
            continue  # 同一任务跨部门 = 已存在的协作，不是重复推进
        # 第三道门槛：**不同部门的两条事项要确实在说同一件事**。
        # 只共用对象名不算 —— 实测「大连枫叶申请」下 4 个部门分别在推广项目、
        # 做英文宣传册、给市场经理培训、跟进学生申请，那是标准分工。
        cross_department = _cross_department_same_item(bucket["items"])
        if not cross_department:
            continue
        results.append(
            {
                "project": bucket["project"],
                "department_count": len(bucket["departments"]),
                "employee_count": len(bucket["employees"]),
                "departments": sorted(bucket["departments"]),
                "employees": sorted(bucket["employees"]),
                "evidence_count": len(bucket["report_ids"]),
                "sources": bucket["items"],
                "source_report_ids": sorted(bucket["report_ids"]),
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                # 判定依据必须能看到：系统说「这两个部门在做同一件事」，
                # 得让人看到它凭哪两条原文如此判断。
                "same_item_evidence": cross_department,
            }
        )
    results.sort(
        key=lambda item: (
            -int(item["department_count"]),
            -int(item["employee_count"]),
            item["project"],
        )
    )
    return results


def _cross_department_same_item(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """找出「来自不同部门、且确实在说同一件事」的事项对。

    返回证据对（两侧原文 + 部门 + 周报 ID），没有则返回 None —— 调用方据此排除
    这条重复提醒。

    为什么必须逐对比较而不是只数部门：`related_project` 相同只说明两件事挂在
    同一个业务对象下，而一个业务对象下面可以有很多**不同的**工作。实测
    「大连枫叶申请」的 4 个部门分别在推广项目、做英文宣传册、做市场经理培训、
    跟进学生申请 —— 全是分工，却因为共享对象名被报成「疑似重复推进」。

    用 ``_is_same_work_item``（标题二元组包含度）而不是更复杂的语义判断：
    与「状态不一致」共用同一套判定，两个模块对「同一件事」的理解必须一致，
    否则同一条事项在一个模块算同一件事、在另一个模块不算，页面自相矛盾。
    """
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if not left.get("department_name") or not right.get("department_name"):
                continue
            if left["department_name"] == right["department_name"]:
                continue
            if not _is_same_work_item(left.get("title"), right.get("title")):
                continue
            pair = {
                "left": {
                    "title": left.get("title"),
                    "department_name": left.get("department_name"),
                    "employee_name": left.get("employee_name"),
                    "report_id": left.get("report_id"),
                },
                "right": {
                    "title": right.get("title"),
                    "department_name": right.get("department_name"),
                    "employee_name": right.get("employee_name"),
                    "report_id": right.get("report_id"),
                },
            }
            if pair not in pairs:
                pairs.append(pair)
    return pairs or None


def _title_grams(text: str | None) -> set[str]:
    """事项标题的字符二元组，用于判断两条事项是不是「同一件事」。

    中文不依赖分词也能近似（「院校反馈」与「院校反馈材料」共享大部分 gram），
    中英混排同样适用。标点与空格先剥离，避免「A、B」与「A B」被当成不同。
    """
    compact = _NON_WORD.sub("", (text or "").casefold())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


# 「同一件事」的判定门槛。用**包含度**（重叠二元组 / 较短标题的二元组数）而不是
# Jaccard：周报里同一件事常见的写法是「一句话 + 补充」，例如
# 「院校电邮基本没回复」与「院校电邮回复率低」、「服务器升级」与「服务器升级需尽快审批」。
# Jaccard 会因为这些补充部分把相似度压到 0.5 以下（实测 0.36 / 0.44）而漏检，
# 包含度能量出「短的那个基本被长的包住」这个事实（0.57 / 1.0）。
# 误报方向也已实测：同一对象下两件不同的事（转发出入境消息 / 咨询材料原件）
# 包含度仅 0.25，仍被挡在门槛外。
# 另外要求至少共享 3 个 gram —— 短标题（「没人」）gram 太少，
# 不设这个下限会把任意两条两字事项算成同一件。
SAME_ITEM_MIN_SIMILARITY = 0.5
SAME_ITEM_MIN_SHARED_GRAMS = 3


def _is_same_work_item(left: str | None, right: str | None) -> bool:
    left_grams, right_grams = _title_grams(left), _title_grams(right)
    if not left_grams or not right_grams:
        return False
    shared = left_grams & right_grams
    if len(shared) < SAME_ITEM_MIN_SHARED_GRAMS:
        return False
    return len(shared) / min(len(left_grams), len(right_grams)) >= SAME_ITEM_MIN_SIMILARITY


def _status_item_state(item: ReportItem) -> str | None:
    """一条事项在「状态一致性」口径下属于哪一侧：已完成 / 未解决 / 与判断无关。

    依赖类（dependencies 等）默认算「未解决」，但状态写成已完成时算已完成 ——
    依赖被答复了就是解决了，这正是两侧最值得对比的情形。
    """
    status = item.status or ""
    if item.item_type in PROGRESS_ITEM_TYPES or "已完成" in status:
        return "finished"
    if status in {"待确认", "未解决"} or item.item_type in DEPENDENCY_ITEM_TYPES:
        return "open"
    return None


def _status_conflicts(rows: list[Any]) -> list[dict[str, Any]]:
    """状态不一致：**同一件事**被不同的人给出互相矛盾的状态。

    两步判断，缺一不可：

    1. 按业务对象分桶 —— 同一件事必然挂在同一个业务对象下，这一步只是缩小范围。
    2. **再要求两条事项本身是同一件事**（标题二元组相似度）。共用一个对象名不等于
       同一件事：实测「转发广州出入境消息给华南理工大学 2 名学生」（已完成）与
       「联系华南理工大学咨询学生申请材料原件」（进行中）都带「华南理工大学」，
       只按对象分组会把它们判成矛盾 —— 当期唯一一条冲突就是这么来的，准确率 0/1。

    第二层是后加的：最初的理由是「按 task_key 分组结果恒为 0」（模型对同一件事
    每次给的 key 都不同，见 ``_workstream_key`` 的说明）。但按对象分组把门槛降到了
    「共用一个名字」，于是误报。用标题相似度而不是 task_key，才能同时避开这两个坑。

    刻意要求「跨人」：同一个人在同一篇周报里既写计划又写完成是正常的上下文差异，
    算成冲突会每天报一堆假阳性。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    departments: dict[str, str | None] = {}
    for item, report, employee, department, _task_id in rows:
        if _is_noise_item(item):
            continue
        name = (item.related_project or "").strip()
        if not name:
            continue
        state = _status_item_state(item)
        if state is None:
            continue
        who = employee.name if employee else "未知"
        grouped.setdefault(name, []).append(
            {
                "who": who,
                "department": department.name if department else None,
                "state": state,
                "title": item.title or name,
                "report_id": report.id,
                "period_start": report.start_date.isoformat() if report.start_date else None,
                "period_end": report.end_date.isoformat() if report.end_date else None,
                "occurred_on": report.end_date.isoformat() if report.end_date else None,
            }
        )
        departments[who] = department.name if department else departments.get(who)

    results: list[dict[str, Any]] = []
    for name, entries in grouped.items():
        finished = [entry for entry in entries if entry["state"] == "finished"]
        opened = [entry for entry in entries if entry["state"] == "open"]
        if not finished or not opened:
            continue
        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for entry in finished:
            for other in opened:
                if entry["who"] == other["who"]:
                    continue
                if not _is_same_work_item(entry["title"], other["title"]):
                    continue
                matched.append((entry, other))
        if not matched:
            continue
        finished_by = {entry["who"] for entry, _ in matched}
        open_by = {other["who"] for _, other in matched}
        # 同一个人既在完成侧又在未解决侧 = 他自己表述有歧义，不算「跨人不一致」。
        # 逐对匹配下这个人可能只是碰巧两侧都有条目，所以按人剔除而不是整条丢掉。
        finished_by -= open_by
        if not finished_by:
            continue
        involved = [entry for pair in matched for entry in pair]
        results.append(
            {
                "name": name,
                "title": next(
                    (other["title"] for _, other in matched), name
                ),
                "finished_by": sorted(finished_by),
                "open_by": sorted(open_by),
                "departments": sorted(
                    {
                        value
                        for value in (departments.get(who) for who in finished_by | open_by)
                        if value
                    }
                ),
                "source_report_ids": sorted({entry["report_id"] for entry in involved}),
                # 判定依据必须能看到：只说「这两条是同一件事」不够，要给出两边原文，
                # 让人自己核对这个判定对不对（这是本轮准确性收口的要求）。
                "evidence": [
                    {
                        "side": "finished",
                        "employee_name": entry["who"],
                        "title": entry["title"],
                        "report_id": entry["report_id"],
                        "period_start": entry["period_start"],
                        "period_end": entry["period_end"],
                        "occurred_on": entry["occurred_on"],
                    }
                    for entry, _ in matched
                ]
                + [
                    {
                        "side": "open",
                        "employee_name": other["who"],
                        "title": other["title"],
                        "report_id": other["report_id"],
                        "period_start": other["period_start"],
                        "period_end": other["period_end"],
                        "occurred_on": other["occurred_on"],
                    }
                    for _, other in matched
                ],
            }
        )
    results.sort(key=lambda item: (-len(item["departments"]), item["title"] or ""))
    return results


def _frictions(session: Session, start: date, end: date) -> dict[str, Any]:
    """组织摩擦：两类**有结构化证据**的关系，不做情绪推断。

    刻意**不含「多部门反复确认」**：它原本是把瓶颈列表里 `department_count >= 2`
    的那几条换个名字再列一遍（实测当期 5 条与瓶颈列表完全同源）。同一批事实在两张卡
    里出现，读起来像发现了 5 处摩擦，实际是同一件事说了两遍 —— 「需要多个部门反复
    沟通」这个信息由「最大的运行瓶颈」卡的部门数列承担，不需要第二处。

    按 (start, end) 在一次请求内只算一遍：总览卡与信号排序都要这份结果。
    """
    return _memo(
        session, ("frictions", start, end), lambda: _build_frictions(session, start, end)
    )


def _build_frictions(session: Session, start: date, end: date) -> dict[str, Any]:
    rows = _period_item_rows(session, start, end)
    duplicates = _duplicates(rows, start=start, end=end)
    conflicts = _status_conflicts(rows)
    case = duplicates[0] if duplicates else None
    return {
        "duplicates": duplicates,
        "conflicts": conflicts,
        "counts": {
            "duplicate_effort": len(duplicates),
            "status_conflict": len(conflicts),
        },
        "case": case,
    }


def _focus_distribution(
    session: Session, start: date, end: date
) -> dict[str, Any]:
    """公司精力投向：按「员工 × 类别」的唯一参与关系算，不按事项条数。

    同一个人一周在某类别写了 5 条也只算 1 个参与人次 —— 否则写得详细的人
    权重会被放大好几倍。这是「人员参与度」，不是工时占比：周报里没有工时。

    **不截断、不把尾部类别并进「其他工作」**：类别数由 ``FOCUS_CATEGORY_RULES``
    决定（固定 8 个以内），本来就不需要为了版面砍。此前按前面 6 名截断、把其余
    并进「其他工作」，会把真实类别显示错 —— 实测「国际医疗与展会 4.5%」与
    「招聘与团队建设 3.4%」被并走之后，「其他工作」从真实的 14.6% 显示成 22.5%，
    于是饼图里第二大的一块有一半是显示产物。**「其他工作」是规则表兜不住的归类
    结果，不是「剩下的一堆」**；用户按它去判断「公司在这块投入多少」会直接读错。
    """
    rows = _theme_rows(session, start, end)
    categories: dict[str, dict[str, Any]] = {}
    # 「谁参与了这个类别」——每人每类别只计一次。
    participants: dict[str, set[int]] = defaultdict(set)
    for task, item, _report, employee, department in rows:
        label, keyword = _focus_category(
            f"{task.title or ''} {item.related_project or ''} {item.title or ''}"
        )
        bucket = categories.setdefault(
            label,
            {
                "label": label,
                "keywords": set(),
                "employees": set(),
                "departments": set(),
                "projects": defaultdict(int),
                "report_ids": set(),
            },
        )
        if keyword:
            bucket["keywords"].add(keyword)
        if employee is not None:
            bucket["employees"].add(employee.name)
            participants[label].add(employee.id)
        if department is not None:
            bucket["departments"].add(department.name)
        bucket["report_ids"].add(item.report_id)
        if item.related_project:
            bucket["projects"][item.related_project] += 1

    total = sum(len(ids) for ids in participants.values())
    items: list[dict[str, Any]] = []
    for label, bucket in categories.items():
        people_entries = len(participants.get(label, set()))
        items.append(
            {
                "label": label,
                "people_entries": people_entries,
                "share": round(people_entries / total * 100, 1) if total else 0.0,
                "employee_count": len(bucket["employees"]),
                "department_count": len(bucket["departments"]),
                "report_count": len(bucket["report_ids"]),
                "matched_keywords": sorted(bucket["keywords"]),
                "top_projects": [
                    name
                    for name, _count in sorted(
                        bucket["projects"].items(), key=lambda entry: (-entry[1], entry[0])
                    )[:3]
                ],
            }
        )
    items.sort(key=lambda item: (-int(item["people_entries"]), item["label"]))
    return {
        "total_entries": total,
        "categories": items,
        # 口径写在后端，前端只渲染：两处各写一句迟早出现「页面说投入、其实不是」。
        "basis": "按「员工 × 工作类别」的唯一参与人次计算，同一人多条事项只计一次；不是工时占比。",
    }


def _project_trend_text(
    series: dict[str, dict[str, int]], period_labels: list[str]
) -> str | None:
    """项目逐周活跃度的一句话趋势。

    只描述「记录量」的变化，不说「进度变快/变慢」—— 周报篇数不等于推进速度。
    序列里没有任何分析数据时返回 None（例如以任务标题兜底命名的项目，
    ``_project_weekly_activity`` 只按 related_project 统计，可能查不到它），
    此时结论里不出现趋势句，避免「明明有 N 篇周报却说近 4 周没有记录」的自相矛盾。
    """
    if not period_labels:
        return None
    last = series.get(period_labels[-1], {}).get("report_count")
    prev = series.get(period_labels[-2], {}).get("report_count") if len(period_labels) >= 2 else None
    if last is None and prev is None:
        return None
    if last is not None and prev is None:
        return f"记录集中在最近一个周期（{last} 篇）"
    if last is None:
        return "最近一个周期还没有新的记录"
    if last > prev:
        return f"最近一周的记录在增加（{last} 篇，上一周 {prev} 篇）"
    if last < prev:
        return f"最近一周的记录比上一周少（{last} 篇，上一周 {prev} 篇）"
    return f"最近两周的记录量持平（各 {last} 篇）"


def _project_conclusion(
    *,
    status_label: str,
    name: str,
    departments: list[str],
    employee_count: int,
    report_count: int,
    duration_weeks: int,
    series: dict[str, dict[str, int]],
    period_labels: list[str],
) -> str:
    """项目条目的「说人话」结论。

    与 attention 信号的 conclusion 同一纪律：只拼已经算出的事实
    （状态、篇数、人数、部门、持续周数、逐周记录量），不推断商业含义、
    不给百分比。负责人点开卡片应该先读到一句能直接转述的话，而不是一排裸计数。
    """
    dept_text = "、".join(departments) if departments else "未知部门"
    trend = _project_trend_text(series, period_labels)
    duration_text = f"这件事已经持续了 {duration_weeks} 周" if duration_weeks > 0 else ""

    if status_label == "存在重复推进":
        text = (
            f"{dept_text} 在同一时期各自推进「{name}」，本期共 {report_count} 篇周报提到，"
            "但没有看到协作分工的描述，建议先核对分工，避免两边做重复劳动。"
        )
    elif status_label == "本期有成果":
        text = (
            f"「{name}」本期有已完成的事项落地，共 {report_count} 篇周报提到、"
            f"{employee_count} 人参与，主要由 {dept_text} 在推进"
        )
        if duration_text:
            text += f"；{duration_text}"
        if trend:
            text += f"；{trend}"
        text += "。"
    elif status_label == "持续推进":
        text = f"「{name}」本期仍在推进中，{report_count} 篇周报提到，主要由 {dept_text} 在做"
        if duration_text:
            text += f"；{duration_text}"
        if trend:
            text += f"；{trend}"
        text += "。"
    elif status_label == "计划中":
        text = f"「{name}」目前只看到计划安排，还没有实际完成的记录"
        if trend:
            text += f"；{trend}"
        text += "，建议关注是否按计划落地。"
    else:  # 仅有提及
        text = (
            f"周报里只是提到「{name}」这个名字，没有可判断进展的完成或推进记录，"
            "暂时看不出这件事走到了哪一步。"
        )
    return text


def _project_progress(
    session: Session, start: date, end: date, *, limit: int = PROJECT_LIMIT
) -> dict[str, Any]:
    """重点项目进展：项目级当前状态 + 逐周活跃度 + 一句话结论。

    条目名同样**按任务先定一次身份**（``_task_project_index``）：同一个任务里
    部分事项填了业务对象、部分没填时，逐条取名会让同一件事同时以「院校拓展」和
    「该任务标题」两个条目出现 —— 项目列表里看不出它们是一件事。
    """
    rows = _theme_rows(session, start, end)
    periods = _period_sequence(end, 4)
    period_labels = [item[1].isoformat() for item in periods]
    duplicates = {item["project"] for item in _duplicates(_period_item_rows(session, start, end), start=start, end=end)}
    task_projects = _task_project_index(
        (item, report, employee, department, task.id)
        for task, item, report, employee, department in rows
    )
    # 活跃度序列提前算：结论的趋势句要按它拼（2026-09-24 项目负责人要求
    # 项目卡片像经营信号一样「说人话」，而不是只给一排计数）。
    activity = _project_weekly_activity(session, periods)

    grouped: dict[str, dict[str, Any]] = {}
    for task, item, report, employee, department in rows:
        name = (
            item.related_project
            or task_projects.get(int(task.id))
            or task.title
            or ""
        ).strip()
        if not name:
            continue
        bucket = grouped.setdefault(
            name,
            {
                "name": name,
                "employees": set(),
                "departments": set(),
                "task_ids": set(),
                "report_ids": set(),
                "has_progress": False,
                "has_ongoing": False,
                "has_plan": False,
                "duration_weeks": 0,
                "evidence": [],
            },
        )
        bucket["task_ids"].add(task.id)
        bucket["report_ids"].add(report.id)
        bucket["duration_weeks"] = max(int(bucket["duration_weeks"] or 0), int(task.duration_weeks or 0))
        if employee is not None:
            bucket["employees"].add(employee.name)
        if department is not None:
            bucket["departments"].add(department.name)
        if item.item_type in PROGRESS_ITEM_TYPES or "已完成" in (item.status or ""):
            bucket["has_progress"] = True
        elif item.item_type in ONGOING_ITEM_TYPES:
            bucket["has_ongoing"] = True
            if "计划" in (item.status or ""):
                bucket["has_plan"] = True
        if len(bucket["evidence"]) < EVIDENCE_PER_THEME_LIMIT:
            bucket["evidence"].append(
                {
                    "title": item.title,
                    "status": item.status,
                    "item_type": item.item_type,
                    "report_id": report.id,
                    "employee_name": employee.name if employee else None,
                    "department_name": department.name if department else None,
                }
            )

    items: list[dict[str, Any]] = []
    for bucket in grouped.values():
        name = bucket["name"]
        if name in duplicates:
            status_label, tone = "存在重复推进", "amber"
        elif bucket["has_progress"]:
            status_label, tone = "本期有成果", "green"
        elif bucket["has_ongoing"]:
            status_label, tone = "持续推进", "accent"
        elif bucket["has_plan"]:
            status_label, tone = "计划中", "neutral"
        else:
            status_label, tone = "仅有提及", "neutral"
        items.append(
            {
                "name": name,
                "status_label": status_label,
                "tone": tone,
                "conclusion": _project_conclusion(
                    status_label=status_label,
                    name=name,
                    departments=sorted(bucket["departments"]),
                    employee_count=len(bucket["employees"]),
                    report_count=len(bucket["report_ids"]),
                    duration_weeks=int(bucket["duration_weeks"] or 0),
                    series=activity.get(name, {}),
                    period_labels=period_labels,
                ),
                "duration_weeks": int(bucket["duration_weeks"] or 0),
                "employee_count": len(bucket["employees"]),
                "department_count": len(bucket["departments"]),
                "departments": sorted(bucket["departments"]),
                "report_count": len(bucket["report_ids"]),
                "source_report_ids": sorted(bucket["report_ids"]),
                "evidence": bucket["evidence"],
            }
        )
    items.sort(
        key=lambda item: (
            # 「存在重复」与「本期有成果」都值得先看，再按覆盖篇数
            0 if item["status_label"] == "存在重复推进" else 1,
            -int(item["report_count"]),
            -int(item["employee_count"]),
            item["name"],
        )
    )
    items = items[:limit]

    # 逐周活跃度：**只统计有结构化分析的那些周期**，没有分析数据的周期返回 null，
    # 页面据此显示「—」而不是 0（0 会被读成「这周没人做」）。
    # 序列已在函数开头算过（结论的趋势句要按它拼），这里只做挂载。
    for item in items:
        series = activity.get(item["name"], {})
        item["activity"] = [
            {
                "period_end": label,
                "report_count": series.get(label, {}).get("report_count"),
                "has_data": label in series,
            }
            for label in period_labels
        ]
    return {
        "items": items,
        "periods": period_labels,
        "analyzed_periods": sorted(
            {label for label, series in activity.items() for label in series}
        ) if activity else [],
    }


def _project_weekly_activity(
    session: Session, periods: list[tuple[date, date]]
) -> dict[str, dict[str, dict[str, int]]]:
    """项目 × 周期 → 结构化事项数与篇数。

    一次查回全部周期再分桶，查询数不随项目数与周期数增长。
    """
    if not periods:
        return {}
    span_start, span_end = periods[0][0], periods[-1][1]

    def _load() -> list[Any]:
        return list(
            session.execute(
                select(ReportItem.related_project, WeeklyReport.id, WeeklyReport.end_date, WeeklyReport.start_date)
                .select_from(ReportItem)
                .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
                .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
                .where(
                    *report_period_overlap_conditions(span_start, span_end),
                    non_exempt_report_condition(session),
                    ReportItem.related_project.isnot(None),
                )
            )
        )

    rows = _memo(session, ("project_weekly_activity", span_start, span_end), _load)
    bounds = [(start, end, end.isoformat()) for start, end in periods]
    result: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    seen: dict[tuple[str, str], set[int]] = defaultdict(set)
    for project, report_id, end_date, start_date in rows:
        name = (project or "").strip()
        if not name:
            continue
        occurred = end_date or start_date
        if occurred is None:
            continue
        for period_start, period_end, label in bounds:
            if period_start <= occurred <= period_end:
                bucket = result[name].setdefault(label, {"report_count": 0, "item_count": 0})
                bucket["item_count"] += 1
                if report_id not in seen[(name, label)]:
                    seen[(name, label)].add(report_id)
                    bucket["report_count"] += 1
                break
    return {name: dict(by_period) for name, by_period in result.items()}


def _issue_trends(
    session: Session, periods: list[tuple[date, date]], *, limit: int = TREND_LIMIT
) -> dict[str, Any]:
    """长期趋势：同一问题主题在连续周期里被多少篇周报提到。

    计数单位是**篇数**而不是条数：一篇周报把同一问题写三遍不代表问题更大。

    三条硬护栏，都是被真实数据逼出来的：

    1. **覆盖率不达标的周期不参与比较**。未分析的周期没有事项数据，把它算成 0
       会让「本周刚开始分析」显示成「问题连续扩大」。但只挡「一篇都没分析」远远
       不够：实测上期 27 篇里只分析了 5 篇（19%）、本期 24 篇全分析（100%），
       拿 5 的绝对数和 24 比大小，几乎每个主题都变成「上升」——
       而按占比算（100% → 46%）「院校沟通与反馈」其实在下降。
       因此门槛是**覆盖率**，且两侧差值也要受限（见 ``COMPARABLE_MIN_COVERAGE``）。
    2. **只有 ≥2 个「可比较的周期」才进入趋势表** —— 一个点画不出趋势。
    3. 参与比较的周期用**同一套门槛**筛出来，不允许「有的周期宽松、有的严格」。
    """
    if not periods:
        return {
            "periods": [], "series": [], "insufficient": [],
            "analyzed_periods": [], "basis": TREND_BASIS,
        }
    span_start, span_end = periods[0][0], periods[-1][1]
    coverage = _coverage_by_period(session, periods)
    # 先用绝对门槛筛掉低覆盖周期；两侧差值那一条在下面按相邻周期再判一次。
    usable = [
        (start, end, end.isoformat())
        for start, end in periods
        if coverage.get(end.isoformat(), {}).get("ratio", 0.0) >= COMPARABLE_MIN_COVERAGE
    ]
    labels = [label for _s, _e, label in usable]
    skipped = [
        {
            "period": end.isoformat(),
            "coverage": coverage.get(end.isoformat(), {}).get("ratio", 0.0),
            "analyzed": coverage.get(end.isoformat(), {}).get("analyzed", 0),
            "total": coverage.get(end.isoformat(), {}).get("total", 0),
        }
        for _start, end in periods
        if coverage.get(end.isoformat(), {}).get("ratio", 0.0) < COMPARABLE_MIN_COVERAGE
    ]
    if len(usable) < 2:
        return {
            "periods": labels,
            "series": [],
            "insufficient": [],
            "analyzed_periods": labels,
            "skipped_periods": skipped,
            "reason": (
                "只有不足两个周期的分析覆盖达到 "
                f"{COMPARABLE_MIN_COVERAGE:.0%}，还看不出趋势。"
            ),
            "basis": TREND_BASIS,
        }

    def _load() -> list[Any]:
        return list(
            session.execute(
                select(
                    ReportItem.title,
                    ReportItem.description,
                    ReportItem.item_type,
                    WeeklyReport.id,
                    WeeklyReport.end_date,
                    WeeklyReport.start_date,
                )
                .select_from(ReportItem)
                .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
                .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
                .where(
                    *report_period_overlap_conditions(span_start, span_end),
                    non_exempt_report_condition(session),
                )
            )
        )

    rows = _memo(session, ("issue_trends", span_start, span_end), _load)
    series: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    # 每个主题留下「哪篇周报、原文怎么说」：趋势信号（growing_issue）此前只带
    # 各周期篇数，弹窗既没有证据周报也没有原文 —— 负责人点进去全是计数串
    # （2026-09-30 反馈）。原文片段是模型写「人话」与页面证据卡的素材。
    evidence: dict[str, dict[int, tuple[date, str]]] = defaultdict(dict)
    for title, description, item_type, report_id, end_date, start_date in rows:
        if item_type not in DEPENDENCY_ITEM_TYPES:
            continue
        text = (title or description or "").strip()
        matched = _issue_category(f"{title or ''} {description or ''}")
        if matched is None:
            continue
        label = matched[0]
        occurred = end_date or start_date
        if occurred is None:
            continue
        for period_start, period_end, period_label in usable:
            if period_start <= occurred <= period_end:
                series[label][period_label].add(report_id)
                existing = evidence[label].get(report_id)
                if existing is None:
                    evidence[label][report_id] = (occurred, text)
                elif not existing[1] and text:
                    evidence[label][report_id] = (existing[0], text)
                break

    items: list[dict[str, Any]] = []
    insufficient: list[str] = []
    for label, by_period in series.items():
        counts = [len(by_period.get(period_label, set())) for period_label in labels]
        present = [count for count in counts if count > 0]
        if len(present) < 2:
            # 只在一个周期出现过 —— 可能是偶发，也可能是本周才补的分析。
            # 如实归入「证据不足」，不画成趋势。
            insufficient.append(label)
            continue
        first, last = present[0], present[-1]
        # 方向用**占比**而不是绝对篇数判：两个周期的分析覆盖不同时，篇数变多
        # 可能只是「多分析了几篇」。覆盖率已经过门槛（≥80%），但两侧仍可能有
        # 20 个百分点的差异，绝对数在这个差值内不足以支撑方向判断。
        present_indexes = [index for index, count in enumerate(counts) if count > 0]
        first_ratio = coverage.get(labels[present_indexes[0]], {}).get("ratio", 0.0)
        last_ratio = coverage.get(labels[present_indexes[-1]], {}).get("ratio", 0.0)
        first_share = (first / first_ratio) if first_ratio else float(first)
        last_share = (last / last_ratio) if last_ratio else float(last)
        if last_share > first_share:
            direction, tone = "上升", "amber"
        elif last_share < first_share:
            direction, tone = "下降", "green"
        else:
            direction, tone = "持平", "neutral"
        items.append(
            {
                "title": label,
                "counts": counts,
                "periods": labels,
                "direction": direction,
                "tone": tone,
                "total_evidence": sum(counts),
                "first_count": first,
                "last_count": last,
                # 基数随数字返回：11 篇提到它，是「24 篇里的 11 篇」还是
                # 「27 篇里的 11 篇」，对读法影响很大。
                "coverage": [
                    coverage.get(period_label, {}).get("ratio", 0.0) for period_label in labels
                ],
                "denominators": [
                    coverage.get(period_label, {}).get("analyzed", 0) for period_label in labels
                ],
                # AI 结论引用纪律：每条结论必须能追到原文。趋势主题的来源是
                # 各周期提到它的周报并集（截断到前 20 个），材料与页面共用。
                "source_report_ids": sorted(
                    {rid for ids in by_period.values() for rid in ids}
                )[:20],
                # 原文片段（最新在前，按报告去重）：给趋势信号的证据卡与 AI 材料用。
                "evidence": [
                    {"report_id": report_id, "text": text}
                    for report_id, (_occurred, text) in sorted(
                        evidence[label].items(),
                        key=lambda entry: (-entry[1][0].toordinal(), entry[0]),
                    )
                    if text
                ][:_SIGNAL_EVIDENCE_LIMIT],
            }
        )
    items.sort(
        key=lambda item: (
            0 if item["direction"] == "上升" else 1,
            -int(item["total_evidence"]),
            item["title"],
        )
    )
    return {
        "periods": labels,
        "series": items[:limit],
        "insufficient": insufficient,
        "analyzed_periods": labels,
        # 被门槛挡下的周期要如实列出：用户看到「只有两个周期」时，
        # 需要知道另外两个是因为覆盖不足被排除的，而不是「根本没有数据」。
        "skipped_periods": skipped,
        "basis": TREND_BASIS,
    }


# 趋势类信号的证据卡上限与原文片段长度。证据篇数仍用真实总量（total_evidence），
# 这里只截「展示哪些卡」，与 task_sources 的 limit 同一性质。
_SIGNAL_EVIDENCE_LIMIT = 6
_SIGNAL_SNIPPET_LIMIT = 120


def _report_signal_sources(
    session: Session, report_ids: list[int], evidence_texts: dict[int, str] | None = None
) -> dict[str, Any]:
    """趋势类信号的证据投影：来源周报卡 + 真实涉及部门。

    趋势主题的计数单位是「提到它的周报篇数」，与事项级卡点不同 —— 证据的最小
    单位是周报本身。此前 ``_attention_signals`` 组装 ``growing_issue`` 信号时把
    证据整个扔掉（部门硬编码 0、来源为空），弹窗只剩一串数字。

    ``evidence_texts`` 是该主题命中的原文片段（来自 ``_issue_trends`` 的
    ``evidence``），有片段时卡片标题显示片段而非周报标题 —— 那才是能核对
    「谁提到了这个问题」的信息。部门数按证据周报的真实部门去重。
    """
    ids = sorted({int(value) for value in report_ids})
    if not ids:
        return {"sources": [], "departments": []}
    texts = evidence_texts or {}

    def _load() -> dict[str, Any]:
        rows = session.execute(
            select(WeeklyReport, Employee, Department)
            .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(
                WeeklyReport.id.in_(ids),
                non_exempt_report_condition(session),
            )
            .order_by(WeeklyReport.end_date.desc(), WeeklyReport.id.desc())
        ).all()
        sources: list[dict[str, Any]] = []
        departments: list[str] = []
        seen_departments: set[str] = set()
        for report, employee, department in rows:
            if department and department.name and department.name not in seen_departments:
                seen_departments.add(department.name)
                departments.append(department.name)
            snippet = str(texts.get(report.id) or "").strip()
            sources.append(
                {
                    "id": report.id,
                    "report_id": report.id,
                    "title": snippet[:_SIGNAL_SNIPPET_LIMIT] or report.title,
                    "employee_name": employee.name if employee else None,
                    "department_name": department.name if department else None,
                    "period_start": report.start_date.isoformat() if report.start_date else None,
                    "period_end": report.end_date.isoformat() if report.end_date else None,
                }
            )
        return {"sources": sources[:_SIGNAL_EVIDENCE_LIMIT], "departments": departments}

    # 片段文本进记忆键：两个主题的证据周报可能重叠（同一篇周报提到两个问题），
    # 只按 report_ids 做键会把第一个主题的片段错配给第二个。
    return _memo(
        session,
        (
            "signal_report_sources",
            tuple(ids),
            tuple(texts.get(report_id) or "" for report_id in ids),
        ),
        _load,
    )


def _attention_signals(
    session: Session, start: date, end: date, *, limit: int = SIGNAL_LIMIT
) -> dict[str, Any]:
    """今天值得关注：先由代码算分排序，再取前几名。

    排序分（attention_score，0~100）表达的是「值得关注程度」，不是业务重要性 ——
    周报里没有营收与战略目标，假装知道商业价值是编数据。分项见下表。
    """
    signals: list[dict[str, Any]] = []

    bottleneck_data = _bottlenecks(session, start, end, limit=20)
    for item in bottleneck_data["items"]:
        workstreams = int(item["workstream_count"])
        if workstreams < 2:
            # 只挡住一条主线：这是「某件事卡住了」，不是「什么在拖慢公司」。
            # 它已经出现在持续问题与待处理里，不占用首屏名额。
            continue
        signals.append(
            _signal(
                signal_type="shared_blocker",
                title=f"{item['target']}影响 {workstreams} 项工作",
                conclusion=(
                    f"{item['target']} 在本期被 {workstreams} 项工作依赖，"
                    f"涉及 {item['department_count']} 个部门、{item['evidence_count']} 篇周报。"
                ),
                label="高影响",
                tone="red" if workstreams >= 4 else "amber",
                impact_workstreams=workstreams,
                impact_departments=item["department_count"],
                duration_periods=1,
                growing=False,
                evidence_count=item["evidence_count"],
                sources=item["sources"],
                source_report_ids=item["source_report_ids"],
                object_name=item["target"],
            )
        )

    frictions = _frictions(session, start, end)
    for duplicate in frictions["duplicates"]:
        signals.append(
            _signal(
                signal_type="duplicate_effort",
                title=f"疑似重复推进：{duplicate['project']}",
                conclusion=(
                    f"{'、'.join(duplicate['departments'])} 在同一周期提交了同一业务对象的"
                    f"{duplicate['evidence_count']} 条事项，未看到协作分工描述，建议核对分工。"
                ),
                label="组织效率",
                tone="amber",
                impact_workstreams=1,
                impact_departments=duplicate["department_count"],
                duration_periods=1,
                growing=False,
                evidence_count=duplicate["evidence_count"],
                sources=duplicate["sources"],
                source_report_ids=duplicate["source_report_ids"],
                object_name=duplicate["project"],
            )
        )
    for conflict in frictions["conflicts"]:
        signals.append(
            _signal(
                signal_type="status_conflict",
                title=f"状态描述不一致：{conflict['title']}",
                conclusion=(
                    f"{'、'.join(conflict['finished_by'])} 记录为已完成，"
                    f"{'、'.join(conflict['open_by'])} 仍记录为待确认或未解决，建议核对当前状态。"
                ),
                label="状态不一致",
                tone="amber",
                impact_workstreams=1,
                impact_departments=len(conflict["departments"]),
                duration_periods=1,
                growing=False,
                evidence_count=len(conflict["finished_by"]) + len(conflict["open_by"]),
                sources=[],
                source_report_ids=conflict["source_report_ids"],
                object_name=conflict["title"],
            )
        )
    # 持续未解决：开放卡点里持续时间最长的（跨周证据来自 Blocker 自身的持续周期）。
    long_blockers = list(
        session.execute(
            select(Blocker, Task)
            .join(Task, Task.id == Blocker.task_id)
            .where(
                Blocker.status == BlockerStatus.OPEN,
                Blocker.duration_weeks >= 3,
                # 「今天值得关注」只呈现所选周期内仍有证据的未解决问题。
                # 更早的 OPEN 记录可能已经失效，仍可在卡点中心和历史周报中查看。
                Blocker.last_seen_date >= start,
                Blocker.last_seen_date <= end,
            )
            .order_by(Blocker.duration_weeks.desc(), Blocker.last_seen_date.desc())
            .limit(20)
        )
    )
    source_map = task_sources(session, [task.id for _blocker, task in long_blockers])
    # stalled 信号谈的是「连续 N 周被记录」，篇数取全部历史而不是截断后的 5 篇展示列表。
    stalled_evidence = task_report_counts(session, [task.id for _blocker, task in long_blockers])
    emitted_stalled = 0
    for blocker, task in long_blockers:
        if emitted_stalled >= 2:
            break  # 首屏只给最常见的两条长期问题，其余进列表
        # 「资源需求：Discuss later」这类是员工没填，不是持续未解的问题（见
        # ``is_empty_resource_demand``）。Blocker 行同样要过滤：这条数据比
        # ReportItem 更早，全靠读取侧兜住。
        if is_empty_resource_demand(blocker.title):
            continue
        sources = source_map.get(task.id, [])
        if not sources:
            continue
        emitted_stalled += 1
        signals.append(
            _signal(
                signal_type="stalled_workstream",
                title=f"连续 {blocker.duration_weeks} 周未解决：{blocker.title}",
                conclusion=(
                    f"该问题已连续 {blocker.duration_weeks} 周出现且仍未关闭，"
                    f"最近一次出现在 {_date_text(blocker.last_seen_date)}，建议指定跟进人或拆解下一步。"
                ),
                label="持续停滞",
                tone="red" if blocker.duration_weeks >= 5 else "amber",
                impact_workstreams=1,
                impact_departments=len({source.get("department_name") for source in sources if source.get("department_name")}),
                duration_periods=int(blocker.duration_weeks or 1),
                growing=False,
                evidence_count=max(stalled_evidence.get(task.id, 0), len(sources)),
                sources=sources,
                source_report_ids=[int(source["report_id"]) for source in sources if source.get("report_id")],
                object_name=blocker.title,
            )
        )

    # 趋势上升的问题主题（只取已经过「分析周期」护栏的口径）
    trend_data = _issue_trends(session, _period_sequence(end, 4))
    for item in trend_data["series"]:
        if item["direction"] != "上升":
            continue
        present_periods = [
            label for label, count in zip(item["periods"], item["counts"]) if count > 0
        ]
        # 证据必须跟出去：来源周报、真实涉及部门、原文片段。此前这里三者全空，
        # 弹窗只剩「篇数 7→7→9→8」和硬编码的「涉及部门 0」—— 没有任何可核对的东西。
        report_ids = [int(value) for value in item.get("source_report_ids") or []]
        evidence_data = _report_signal_sources(
            session,
            report_ids,
            {
                int(entry["report_id"]): str(entry.get("text") or "")
                for entry in item.get("evidence") or []
                if isinstance(entry, dict) and entry.get("report_id") is not None
            },
        )
        signals.append(
            _signal(
                signal_type="growing_issue",
                title=f"{item['title']}连续扩大",
                conclusion=(
                    f"在已完成分析的 {len(present_periods)} 个周期里，提到该问题的周报篇数为 "
                    f"{' → '.join(str(count) for count, label in zip(item['counts'], item['periods']) if count > 0)}"
                    f"，影响范围在扩大。"
                ),
                label="趋势上升",
                tone="amber",
                impact_workstreams=item["last_count"],
                impact_departments=len(evidence_data["departments"]),
                duration_periods=len(present_periods),
                growing=True,
                evidence_count=item["total_evidence"],
                sources=evidence_data["sources"],
                source_report_ids=report_ids,
                object_name=item["title"],
            )
        )

    for signal in signals:
        signal["attention_score"] = _attention_score(signal)
    signals.sort(key=lambda item: (-int(item["attention_score"]), item["title"]))
    return {"items": signals[:limit], "total": len(signals)}


def _signal(
    *,
    signal_type: str,
    title: str,
    conclusion: str,
    label: str,
    tone: str,
    impact_workstreams: int,
    impact_departments: int,
    duration_periods: int,
    growing: bool,
    evidence_count: int,
    sources: list[dict[str, Any]],
    source_report_ids: list[int],
    object_name: str | None = None,
) -> dict[str, Any]:
    """全部信号统一形状；字段只有一个定义，避免某类信号缺字段让前端崩。

    ``signal_key`` 是**稳定键**（``signal_type:业务对象名``）。展示标题里嵌着
    数字（「影响 15 项工作」「连续 4 周未解决」），新周报一入库数字就变、标题
    就变 —— AI 结论如果按展示标题贴回，会在每次数据更新后全部脱落回规则串。
    """
    return {
        "signal_type": signal_type,
        "title": title,
        "signal_key": f"{signal_type}:{object_name or title}",
        "conclusion": conclusion,
        "label": label,
        "tone": tone,
        "impact_workstreams": int(impact_workstreams),
        "impact_departments": int(impact_departments),
        "duration_periods": int(duration_periods),
        "growing": bool(growing),
        "evidence_count": int(evidence_count),
        "sources": sources,
        "source_report_ids": sorted({int(value) for value in source_report_ids}),
    }


def _attention_score(signal: dict[str, Any]) -> int:
    """值得关注程度 0~100：影响范围 35 + 持续时间 25 + 跨部门 15 + 恶化 15 + 证据 10。

    刻意不做「业务重要性」评分：没有营收/目标数据时那只能是编的。
    """
    impact = min(int(signal["impact_workstreams"]), 4) / 4 * 35
    duration = min(int(signal["duration_periods"]), 5) / 5 * 25
    cross = min(int(signal["impact_departments"]), 3) / 3 * 15
    worsening = 15 if signal["growing"] else 0
    evidence = min(int(signal["evidence_count"]), 5) / 5 * 10
    return int(round(impact + duration + cross + worsening + evidence))


def _date_text(value: date | None) -> str:
    return value.isoformat() if value else "未知日期"


def _activity_series(
    session: Session, periods: list[tuple[date, date]]
) -> list[dict[str, Any]]:
    """整体工作活跃度：逐周期周报篇数与已分析篇数（都是真实计数）。"""
    if not periods:
        return []
    span_start, span_end = periods[0][0], periods[-1][1]
    rows = session.execute(
        select(
            WeeklyReport.id,
            WeeklyReport.end_date,
            WeeklyReport.start_date,
            WeeklyReport.ai_checked_at,
        )
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            *report_period_overlap_conditions(span_start, span_end),
            non_exempt_report_condition(session),
        )
    )
    bounds = [(start, end, end.isoformat()) for start, end in periods]
    counters: dict[str, dict[str, int]] = {
        label: {"reports": 0, "analyzed": 0} for _s, _e, label in bounds
    }
    for _report_id, end_date, start_date, checked_at in rows:
        occurred = end_date or start_date
        if occurred is None:
            continue
        for period_start, period_end, label in bounds:
            if period_start <= occurred <= period_end:
                counters[label]["reports"] += 1
                if checked_at is not None:
                    counters[label]["analyzed"] += 1
                break
    return [
        {
            "period_end": label,
            "label": f"{int(label[5:7])}/{int(label[8:10])}",
            "reports": counters[label]["reports"],
            "analyzed": counters[label]["analyzed"],
        }
        for _s, _e, label in bounds
    ]


def coverage_ratio(coverage: dict[str, Any]) -> float:
    """覆盖率 = 已分析篇数 / 周报篇数。0 表示没有任何可比较的结构化事实。"""
    total = int(coverage.get("reports_total") or 0)
    analyzed = int(coverage.get("analyzed_reports") or 0)
    return analyzed / total if total else 0.0


def _coverage_by_period(
    session: Session, periods: list[tuple[date, date]]
) -> dict[str, dict[str, Any]]:
    """每个周期的分析覆盖情况（可比性的前提，不只是「有没有数据」）。

    返回 ``analyzed`` / ``total`` / ``ratio`` 三项而不是一个篇数：只看篇数会漏掉
    「5 篇 / 27 篇」这种低覆盖，而那正是「连续扩大」假结论的来源。
    """
    if not periods:
        return {}

    key = ("coverage_by_period", tuple(periods))

    def _load() -> dict[str, dict[str, Any]]:
        span_start, span_end = periods[0][0], periods[-1][1]
        report_rows = session.execute(
            select(WeeklyReport.id, WeeklyReport.start_date, WeeklyReport.end_date)
            .select_from(WeeklyReport)
            .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
            .where(
                *report_period_overlap_conditions(span_start, span_end),
                non_exempt_report_condition(session),
            )
        )
        analyzed_report_ids = set(
            session.scalars(
                select(ReportItem.report_id)
                .select_from(ReportItem)
                .join(WeeklyReport, WeeklyReport.id == ReportItem.report_id)
                .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
                .where(
                    *report_period_overlap_conditions(span_start, span_end),
                    (~resource_demand_candidate_condition())
                    | WeeklyReport.ai_checked_at.isnot(None),
                    non_exempt_report_condition(session),
                )
                .distinct()
            )
        )
        counters = {
            end.isoformat(): {"analyzed": 0, "total": 0} for _start, end in periods
        }
        for report_id, report_start, report_end in report_rows:
            if report_start is None:
                continue
            effective_end = report_end or report_start
            for period_start, period_end in periods:
                if report_start > period_end or effective_end < period_start:
                    continue
                bucket = counters[period_end.isoformat()]
                bucket["total"] += 1
                if report_id in analyzed_report_ids:
                    bucket["analyzed"] += 1
        return {
            label: {
                **counts,
                "ratio": round(counts["analyzed"] / counts["total"], 4)
                if counts["total"]
                else 0.0,
            }
            for label, counts in counters.items()
        }

    return _memo(session, key, _load)


def _comparability(
    current: dict[str, Any], previous: dict[str, Any]
) -> dict[str, Any]:
    """两个周期的比较是否成立。

    这是本模块最重要的一道闸：**「有数据」不等于「可比」**。旧实现只判
    `analyzed_reports > 0`，于是上期 5 篇（19%）、本期 24 篇（100%）也被当成可比，
    环比和趋势一起给出方向相反的结论（实测「院校沟通与反馈」占比从 100% 掉到
    46%，却被报成「5 → 11 连续扩大」）。

    返回结构化结果（两侧覆盖率 + 不可比原因），由页面如实展示 —— 只说「不可比」
    而不说为什么，用户就不知道要去补什么。
    """
    current_ratio = coverage_ratio(current)
    previous_ratio = coverage_ratio(previous)
    basis = {
        "current_coverage": round(current_ratio, 4),
        "previous_coverage": round(previous_ratio, 4),
        "min_coverage": COMPARABLE_MIN_COVERAGE,
    }
    if not int(previous.get("reports_total") or 0):
        return {"comparable": False, "reason": "上期无周报数据", **basis}
    if previous_ratio <= 0:
        return {"comparable": False, "reason": "上期未做结构化分析", **basis}
    if previous_ratio < COMPARABLE_MIN_COVERAGE:
        return {
            "comparable": False,
            "reason": f"上期覆盖 {previous_ratio:.0%}，不可比",
            **basis,
        }
    if current_ratio < COMPARABLE_MIN_COVERAGE:
        return {
            "comparable": False,
            "reason": f"本期覆盖 {current_ratio:.0%}，不可比",
            **basis,
        }
    # 差值用 >= 而不是 >：门槛本身（20 个百分点）就该算不可比。
    # 比较前先消掉浮点误差 —— `1.0 - 0.8` 在 IEEE754 下是 0.19999996，
    # 直接比会让「刚好 20 个百分点」的两个周期被放行，等于门槛悄悄放宽一档。
    gap = abs(current_ratio - previous_ratio)
    if gap >= COMPARABLE_MAX_COVERAGE_GAP - 1e-9:
        return {
            "comparable": False,
            "reason": f"两侧覆盖相差 {gap:.0%}，不可比",
            **basis,
        }
    return {"comparable": True, "reason": None, **basis}


def _period_delta(
    current: int,
    previous: int,
    *,
    blocked: str | None,
    label: str,
    basis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """环比：两侧不可比时**不给数字**，只如实说明原因。

    ``blocked`` 是必需参数而不是可选 —— 每个调用点都必须先回答「这一对比较成不
    成立」，而不是顺手拿一个差值。旧签名只判「上期有没有分析数据」，
    挡不住「上期 19% vs 本期 100%」这种不可比（实测它让概要卡报出「+50」）。
    """
    if blocked:
        return {"available": False, "text": blocked, "tone": "neutral", **(basis or {})}
    delta = current - previous
    tone = "green" if delta > 0 else ("amber" if delta < 0 else "neutral")
    sign = f"+{delta}" if delta > 0 else str(delta)
    return {
        "available": True,
        "value": delta,
        "text": f"较{label} {sign}",
        "tone": tone,
        **(basis or {}),
    }


def _current_period_members(session: Session, start: date, end: date) -> dict[str, int]:
    """本期口径：应提交人数与已提交人数（用于概要卡）。"""
    reportable_ids = reportable_employee_ids(session)
    submitted_ids = submitted_employee_ids(session, start, end)
    # 周期查询会返回免提交员工和非在职员工的历史提交；提交率分子必须与
    # 当前应提交名单取交集，否则免提交者交了报告时会被算进提交人数。
    return {
        "reportable": len(reportable_ids),
        "submitted": len(reportable_ids & submitted_ids),
    }


def _workstream_objects(
    session: Session, start: date, end: date
) -> dict[str, Any]:
    """本期有实质事项的业务对象数（「工作主线」指标的分子）。

    统计单位是**业务对象**（``ReportItem.related_project``：学校、项目、系统…），
    不是 ``Task``。原因是被实测逼出来的：开发库 318 个 Task 里有 317 个只出现过
    一次 —— 模型对同一件事每次给出的 ``task_key`` 都不同，按 Task 数当主线数会得到
    「本周 318 条主线」这种数字，比事实大一个量级。

    业务对象去重后的计数与事实吻合（实测 124 个对象 / 480 条事项），且「非一次性
    提及」这个准入门槛（≥2 条事项）本身可核对。
    """
    def _load() -> dict[str, Any]:
        aggregate: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"item_count": 0, "report_ids": set()}
        )
        for item, report, _employee, _department, _task_id in _period_item_rows(session, start, end):
            name = (item.related_project or "").strip()
            if name:
                aggregate[name]["item_count"] += 1
                aggregate[name]["report_ids"].add(int(report.id))
        return dict(aggregate)

    counts = _memo(session, ("workstream_objects", start, end), _load)
    substantive = [
        {
            "name": name,
            "item_count": int(data["item_count"]),
            "source_report_ids": sorted(int(value) for value in data["report_ids"]),
        }
        for name, data in counts.items()
        if int(data["item_count"]) >= WORKSTREAM_MIN_ITEMS
    ]
    substantive.sort(key=lambda entry: (-entry["item_count"], entry["name"]))
    return {
        "total": len(substantive),
        "objects_total": len(counts),
        "min_items": WORKSTREAM_MIN_ITEMS,
        "top": substantive[:5],
        "items": substantive,
    }


def _previous_period_metrics(session: Session, start: date, end: date) -> dict[str, int]:
    """上一周期的环比基数（主线总数 + 信号总数），带进程级 TTL 快照。

    环比必须把上一周期整个算一遍 —— 单次 overview 因此覆盖两个完整周期窗口，
    成本接近翻倍。但这两个数字对同一 (start, end) 的任何调用方都相同，
    且上一周期早已固化、基本不再变化：按周期键缓存 15 秒（与路由层读缓存
    同节奏），miss 才真正付一次双窗口成本。**测试环境必须绕过**：测试直接调
    服务函数、每个用例换库，进程级缓存会跨用例串数据。
    """
    def _compute() -> dict[str, int]:
        return {
            "workstreams": _workstream_objects(session, start, end)["total"],
            "signals": _attention_signals(session, start, end, limit=50)["total"],
        }

    if get_settings().app_env == "test":
        return _compute()

    key = (start, end)
    now = time.monotonic()
    with _previous_metrics_lock:
        cached = _previous_metrics_cache.get(key)
        if cached and now - cached[0] <= _PREVIOUS_METRICS_TTL_SECONDS:
            return dict(cached[1])
        if cached:
            _previous_metrics_cache.pop(key, None)
    metrics = _compute()
    with _previous_metrics_lock:
        if len(_previous_metrics_cache) >= 32:
            _previous_metrics_cache.pop(next(iter(_previous_metrics_cache)), None)
        _previous_metrics_cache[key] = (time.monotonic(), dict(metrics))
    return metrics


def clear_previous_metrics_cache() -> None:
    """清空环比基数快照；入队/数据变更路径调用，避免跨窗口旧数字。"""
    with _previous_metrics_lock:
        _previous_metrics_cache.clear()


# 环比基数的进程级快照：键是上一周期 (start, end)，TTL 与路由层读缓存对齐。
_PREVIOUS_METRICS_TTL_SECONDS = 15.0
_previous_metrics_cache: dict[tuple[date, date], tuple[float, dict[str, int]]] = {}
_previous_metrics_lock = threading.Lock()


def _overview_metrics(
    session: Session,
    start: date,
    end: date,
    *,
    workstreams: dict[str, Any],
    signals: int,
    coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    """概要卡的四个指标。每个都带口径与环比可信度，不拼形容词。

    依赖结构化事实的三个指标共用**同一道可比性判定**：否则会出现「篇数能环比、
    工作主线不能」这种页面内部自相矛盾的状态。
    """
    previous_start = start - timedelta(days=7)
    previous_end = end - timedelta(days=7)
    previous_coverage = analysis_coverage(session, previous_start, previous_end)
    comparability = _comparability(coverage, previous_coverage)
    previous_available = bool(comparability["comparable"])
    previous_metrics = (
        _previous_period_metrics(session, previous_start, previous_end)
        if previous_available
        else {"workstreams": 0, "signals": 0}
    )
    previous_workstreams = previous_metrics["workstreams"]
    previous_signals = previous_metrics["signals"]
    # 每个指标的基数随数字一起返回：只说「58 条主线」而不说「基于 24 篇已分析
    # 周报」，用户无法判断这个数字该信几分。
    coverage_basis = {
        "analyzed_reports": int(coverage.get("analyzed_reports") or 0),
        "reports_total": int(coverage.get("reports_total") or 0),
        "coverage": round(coverage_ratio(coverage), 4),
    }
    members = _current_period_members(session, start, end)
    reported_rate = (
        round(members["submitted"] / members["reportable"] * 100, 1)
        if members["reportable"]
        else 0.0
    )
    # 篇数环比**不受分析覆盖门槛限制**：它只依赖接口入库的周报，与是否跑过 AI
    # 无关 —— 哪怕上期只分析了 5 篇，篇数对比依然成立。这正是「覆盖率」与
    # 「可比性」的区别：覆盖率低不等于篇数不可比，但会让依赖事实的指标不可比。
    previous_reports = int(previous_coverage.get("reports_total") or 0)
    reports_delta = _period_delta(
        int(coverage.get("reports_total") or 0),
        previous_reports,
        blocked=None if previous_reports > 0 else "上期无周报数据",
        label="上期",
        basis={"coverage": coverage_basis["coverage"]},
    )
    return [
        {
            "key": "members",
            "label": "团队成员",
            "value": members["reportable"],
            "unit": "人",
            "sub": f"本期提交 {members['submitted']} 人",
            # 这个指标来自通讯录与提交记录，不依赖分析覆盖；基数仍给出，
            # 让页面能用同一套方式渲染所有指标（少一个字段就得写特例）。
            "basis": coverage_basis,
            "delta": {"available": False, "text": "按通讯录快照", "tone": "neutral"},
        },
        {
            "key": "reports",
            "label": "周报提交",
            "value": int(coverage.get("reports_total") or 0),
            "unit": "篇",
            "sub": f"提交率 {reported_rate}%（已分析 {int(coverage.get('analyzed_reports') or 0)} 篇）",
            "basis": coverage_basis,
            "delta": reports_delta,
        },
        {
            "key": "themes",
            "label": "工作主线",
            "value": int(workstreams["total"]),
            "unit": "条",
            "sub": f"本期出现 {WORKSTREAM_MIN_ITEMS} 条以上事项的业务对象（共 {workstreams['objects_total']} 个）",
            "basis": coverage_basis,
            "delta": _period_delta(
                int(workstreams["total"]),
                previous_workstreams,
                blocked=comparability["reason"],
                label="上期",
                basis={
                    "current_coverage": comparability["current_coverage"],
                    "previous_coverage": comparability["previous_coverage"],
                },
            ),
        },
        {
            "key": "signals",
            "label": "需要关注的问题",
            "value": int(signals),
            "unit": "项",
            "sub": "由规则从证据算出",
            "basis": coverage_basis,
            "delta": _period_delta(
                int(signals),
                previous_signals,
                blocked=comparability["reason"],
                label="上期",
                basis={
                    "current_coverage": comparability["current_coverage"],
                    "previous_coverage": comparability["previous_coverage"],
                },
            ),
        },
    ]


def _period_label(start: date, end: date) -> str:
    """周期档位文字。与首页口径一致：周五截止前默认上周。"""
    from app.services.view_service import default_period

    default_start, default_end = default_period()
    if (start, end) == (default_start, default_end):
        return "本周"
    if start.day <= 7 and start.month == end.month:
        return "本月"
    if (end - start).days == 6:
        return "上周" if end < default_end else "本周"
    return "自定义范围"


def merge_ai_item_notes(
    payload: dict[str, Any] | None, summary: dict[str, Any] | None
) -> dict[str, Any] | None:
    """把 AI 写的逐条结论按名字贴回总览各卡片；没有 AI 结论的条目保留规则版。

    覆盖六类卡片（都来自总结材料里的 ref）：
    - ``projects.items``（PR*，按 ``name`` 匹配）
    - ``bottlenecks.items``（BN*，按 ``target`` 匹配 —— 瓶颈名是稳定的业务对象名）
    - ``frictions.duplicates``（FR*，按 ``project`` 匹配）
    - ``frictions.conflicts``（FC*，按 ``title`` 匹配）
    - ``trends.series``（TR*，按 ``title`` 匹配）
    - ``attention.items`` / ``attention.all_items``（AT*，按 **signal_key** 匹配，
      旧总结的身份存的是展示标题时回退按 ``title`` 匹配）

    与 ``merge_monthly_narrative`` 同一纪律：
    - **按 ``ref_identities`` 校验归属，不按位置贴** —— 简报过期重算后清单可能重排，
      只按位置贴会把 A 的结论挂到 B 上（第 28i 条）。
    - **过期不等于结论作废**：AI 结论存在就贴回（页面另有整体过期提示），
      没有就继续用规则版，页面始终有内容可读。
    - 条目带 ``conclusion`` / ``conclusion_source``（``ai`` / ``rules``），
      前端据此标注结论来源。
    """
    if not isinstance(payload, dict):
        return payload
    if isinstance(summary, dict):
        result = summary.get("result")
        result = result if isinstance(result, dict) else {}
        # 兼容 v8 的 project_notes：v9 起统一为 item_notes。
        notes = result.get("item_notes") or result.get("project_notes") or []
        identities = summary.get("ref_identities") or {}
    else:
        notes, identities = [], {}
    # 键是 (ref 前缀, 业务对象名)。**不能只用名字**：同一个业务对象完全可能
    # 同时是「重点项目」（PR）和「疑似重复推进」（FR），按名字做键会让两份
    # 结论互相覆盖 —— 实测国际医疗展就同时出现在两边。
    by_ref: dict[tuple[str, str], str] = {}
    if isinstance(notes, list):
        for note in notes:
            if not isinstance(note, dict):
                continue
            ref = str(note.get("ref") or "").strip().upper()
            text = str(note.get("text") or "").strip()
            identity = str(identities.get(ref) or "").strip()
            # 只认材料发出去过的编号前缀（PR/BN/FR/FC/TR/AT + 序号），其余当模型编造丢弃
            if (
                len(ref) >= 3
                and ref[:2] in {"PR", "BN", "FR", "FC", "TR", "AT"}
                and ref[2:].isdigit()
                and text
                and identity
            ):
                by_ref[(ref[:2], identity)] = text

    def _attach(
        items: Any, key: str, prefixes: tuple[str, ...], fallback_key: str | None = None
    ) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get(key) or "")
            ai_text = next(
                (by_ref[(prefix, name)] for prefix in prefixes if name and (prefix, name) in by_ref),
                None,
            )
            if ai_text is None and fallback_key:
                # 旧总结的身份存的是展示标题（里面嵌着数字），数据一变就贴不上 ——
                # 改用稳定键正是为了治这个。title 兜底只服务还没重算的旧结论。
                legacy = str(item.get(fallback_key) or "")
                ai_text = next(
                    (by_ref[(prefix, legacy)] for prefix in prefixes if legacy and (prefix, legacy) in by_ref),
                    None,
                )
            if ai_text:
                item["conclusion"] = ai_text
                item["conclusion_source"] = "ai"
            else:
                item.setdefault("conclusion_source", "rules")

    projects = payload.get("projects")
    if isinstance(projects, dict):
        _attach(projects.get("items"), "name", ("PR",))
    bottlenecks = payload.get("bottlenecks")
    if isinstance(bottlenecks, dict):
        _attach(bottlenecks.get("items"), "target", ("BN",))
    frictions = payload.get("frictions")
    if isinstance(frictions, dict):
        _attach(frictions.get("duplicates"), "project", ("FR",))
        _attach(frictions.get("conflicts"), "title", ("FC",))
    trends = payload.get("trends")
    if isinstance(trends, dict):
        _attach(trends.get("series"), "title", ("TR",))
    # 关注信号：列表（首屏前几名）与 all_items（弹窗可打开的全集）都要贴 ——
    # 弹窗展示的就是 all_items 里那条，漏了它弹窗还是规则版。
    # 按稳定键匹配：展示标题里的数字（影响 N 项 / 连续 N 周）随数据变化，
    # 按标题贴会在每次新周报入库后把全部 AI 结论打回规则串。
    attention = payload.get("attention")
    if isinstance(attention, dict):
        _attach(attention.get("items"), "signal_key", ("AT",), fallback_key="title")
        _attach(attention.get("all_items"), "signal_key", ("AT",), fallback_key="title")
    return payload


def overview_view(session: Session, start: date, end: date) -> dict[str, Any]:
    """图片版单页看板：一屏回答「哪里值得关注」。

    **本函数不调模型、也不读 AI 结论**：所有数字都是确定性规则算出来的。
    一句话总览来自路由层附带的 ``summary``（已缓存的 AI 结论），
    与权威的过期判定共用同一份状态，避免在这里再造一套。
    AI 内容只作为补充呈现，缺了它整页依然成立 —— 这是刻意的：模型不可用时
    看板不该变成空白页。
    """
    rows = _theme_rows(session, start, end)
    themes = _themes(rows)
    coverage = _coverage(session, start, end)
    signals = _attention_signals(session, start, end, limit=100)
    # 跟进按钮的匹配范围与 POST 校验保持一致（最多 100 条候选），而页面仍只
    # 展示前 8 条。复用这次查询，避免既限制匹配能力又额外增加一次数据库查询。
    action_candidates = management_action_items(session, start, end, limit=100)
    action_items = _attention_items(
        session, start, end, candidates=action_candidates
    )
    trackable_candidates = _trackable_index(
        session, start, end, candidates=action_candidates
    )
    for signal in signals["items"]:
        candidate = _match_signal_trackable(signal, trackable_candidates)
        signal["can_track"] = candidate is not None
        signal["track"] = (
            {
                "source_type": candidate["source_type"],
                "source_id": candidate["source_id"],
                "source_key": candidate["source_key"],
                "href": candidate["href"],
            }
            if candidate
            else None
        )
    activity_periods = _period_sequence(end, 4)
    trend_periods = _period_sequence(end, TREND_LOOKBACK_WEEKS)
    return {
        "period_label": _period_label(start, end),
        "coverage": coverage,
        "metrics": _overview_metrics(
            session,
            start,
            end,
            workstreams=_workstream_objects(session, start, end),
            signals=signals["total"],
            coverage=coverage,
        ),
        "activity": {
            "periods": [end.isoformat() for _s, end in activity_periods],
            "points": _activity_series(session, activity_periods),
            "basis": "每个周期实际入库的周报篇数与其中完成结构化分析的篇数。",
        },
        "attention": {
            **signals,
            # 首屏仍只展示既定的前 5 条；概要指标的明细弹层需要完整问题列表。
            "items": signals["items"][:SIGNAL_LIMIT],
            "all_items": signals["items"],
        },
        "workstreams": _workstream_objects(session, start, end),
        "themes": themes,
        "focus": _focus_distribution(session, start, end),
        "bottlenecks": _bottlenecks(session, start, end),
        "frictions": _frictions(session, start, end),
        "projects": _project_progress(session, start, end),
        "trends": _issue_trends(session, trend_periods),
        "attention_items": action_items,
    }


def build_view(
    session: Session,
    view: str,
    start: date,
    end: date,
    *,
    department_id: int | None = None,
) -> dict[str, Any]:
    """统一的经营看板只读组装入口。

    只服务「页面会渲染」的视图。**`department` 不在其中**（该界面已裁掉）——
    它现在会走到 ``ValueError``；路由层的 ``Literal`` 会先一步拦成 422，
    所以这里不需要为它保留一个没有消费方的分支。
    """
    _reset_memo(session)
    if view == OperatingSummaryType.WEEKLY_COMPANY or view == "weekly":
        payload = weekly_view(session, start, end, department_id=department_id)
    elif view == OperatingSummaryType.MONTHLY_COMPANY or view == "monthly":
        payload = monthly_view(session, start, end, department_id=department_id)
    elif view == "overview":
        payload = overview_view(session, start, end)
    else:
        raise ValueError(f"不支持看板视图：{view}")
    return {"view": view, **payload}


__all__ = [
    "build_view",
    "monthly_view",
    "weekly_view",
    "overview_view",
    "DISCOVERY_KIND_LABELS",
    "THEME_LIMIT",
]
