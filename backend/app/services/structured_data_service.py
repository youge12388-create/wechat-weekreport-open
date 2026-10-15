"""把单篇 ``report_analysis`` JSON 投影为可统计、可溯源的关系数据。

这里不调用模型。任务关联优先使用同一次模型返回的 ``task_key``，缺失时才
退回到项目名与标题的规范化键，因此不会破坏「每篇周报一次模型调用」约束。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.time_utils import business_date, utcnow

from app.models.blocker import Blocker
from app.models.ai_analysis import AIAnalysis
from app.models.enums import AIAnalysisStatus, BlockerStatus, TaskStatus
from app.models.report_item import ReportItem
from app.models.report_score import ReportScore
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.processors.journal_parser import extract_resource_demand, is_empty_resource_demand

ITEM_TYPES = (
    "completed_items",
    "ongoing_items",
    "results",
    "blockers",
    "problems",
    "dependencies",
    "next_week_plans",
)
BLOCKER_ITEM_TYPES = {"blockers", "problems", "dependencies", "risks"}
RISK_RANK = {"低": 1, "中": 2, "高": 3}
RESOURCE_DEMAND_TASK_PREFIX = "resource_demand:"


def _canonical_key(item: dict[str, Any]) -> str:
    source = item.get("task_key") or " ".join(
        part for part in (item.get("related_project"), item.get("title")) if part
    )
    compact = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(source).lower())
    return compact[:450]


def _report_date(report: WeeklyReport) -> date | None:
    return report.end_date or report.start_date or (
        report.submitted_at.date() if report.submitted_at else None
    )


def _get_or_create_task(session: Session, item: ReportItem) -> Task | None:
    # 模型有时会把「资源需求」原文复述为普通 blockers，且不返回 task_key。
    # 先尝试复用历史确定性资源候选对应的 Task，避免同一卡点跨周被拆成两条。
    resource_value = extract_resource_demand(
        session.scalar(select(WeeklyReport.content_raw).where(WeeklyReport.id == item.report_id))
    )
    if resource_value and item.item_type in BLOCKER_ITEM_TYPES:
        target = _resource_compact(resource_value)
        for candidate in (item.source_text, item.title, item.description):
            compact = _resource_compact(candidate or "")
            if not compact or not (compact == target or target in compact or compact in target):
                continue
            resource_task = session.scalar(
                select(Task)
                .join(TaskReportLink, TaskReportLink.task_id == Task.id)
                .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
                .where(ReportItem.task_key == _resource_task_key(resource_value))
                .order_by(Task.id)
            )
            if resource_task is not None:
                return resource_task
            break
    key = _canonical_key(
        {
            "task_key": item.task_key,
            "related_project": item.related_project,
            "title": item.title,
        }
    )
    if not key:
        return None
    task = session.scalar(select(Task).where(Task.canonical_key == key))
    if task is not None:
        return task
    task = Task(
        canonical_key=key,
        title=item.title,
        related_project=item.related_project,
        status=TaskStatus.UNKNOWN,
    )
    session.add(task)
    session.flush()
    return task


def _item_rows_for_task(session: Session, task_id: int) -> list[tuple[ReportItem, WeeklyReport]]:
    return list(
        session.execute(
            select(ReportItem, WeeklyReport)
            .join(TaskReportLink, TaskReportLink.report_item_id == ReportItem.id)
            .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
            .where(TaskReportLink.task_id == task_id)
            .order_by(WeeklyReport.end_date, WeeklyReport.start_date, WeeklyReport.id, ReportItem.id)
        )
    )


def _is_completed(item: ReportItem) -> bool:
    # status 是模型自由文本（枚举带「等」）：「未完成」「待完成」都含「完成」，
    # 必须认「已完成」而非「完成」，否则任务状态会被虚报成 COMPLETED。
    return item.item_type in {"completed_items", "results"} or "已完成" in (item.status or "")


def _is_ongoing(item: ReportItem) -> bool:
    # 同理：「未进行」含「进行」，按「进行中」认定，宁可落入 UNKNOWN 也不误报进行中。
    return item.item_type == "ongoing_items" or "进行中" in (item.status or "")


def _derive_blocker_status(
    evidence: list[tuple[ReportItem, WeeklyReport, date | None]],
    *,
    task_status: str,
    existing_blocker: Blocker | None,
    today: date,
) -> str:
    """按周报证据推导卡点生命周期状态，幂等可重放。

    升级规则（负责人已确认）：高风险直接正式；被 >=2 个不同周报周期或
    >=2 名员工提及即升级。单次低/中风险提及只进候选层，不再淹没处理中心。
    静默降档只作用于非 RESOLVED 状态；手动/任务完成的 RESOLVED 不会被
    重算覆盖，仅当高风险新证据出现时才允许重开。
    """
    settings = get_settings()
    if task_status == TaskStatus.COMPLETED:
        return BlockerStatus.RESOLVED
    periods = {occurred for _, _, occurred in evidence if occurred is not None}
    employees = {report.employee_id for _, report, _ in evidence if report.employee_id}
    highest_risk = max(
        (item.risk_level or "低" for item, _, _ in evidence),
        key=lambda value: RISK_RANK.get(value, 1),
    )
    upgraded = (
        RISK_RANK.get(highest_risk, 1) >= RISK_RANK["高"]
        or len(periods) >= settings.blocker_upgrade_min_evidence
        or len(employees) >= settings.blocker_upgrade_min_evidence
    )
    derived = BlockerStatus.OPEN if upgraded else BlockerStatus.CANDIDATE
    if existing_blocker is not None and existing_blocker.status == BlockerStatus.RESOLVED:
        if derived == BlockerStatus.OPEN and highest_risk == "高":
            return BlockerStatus.OPEN
        return BlockerStatus.RESOLVED
    last_seen = max((occurred for _, _, occurred in evidence if occurred is not None), default=None)
    if last_seen is not None:
        silent_weeks = (today - last_seen).days // 7
        if silent_weeks >= settings.blocker_silence_archive_weeks:
            return BlockerStatus.ARCHIVED
        if silent_weeks >= settings.blocker_silence_dormant_weeks and derived == BlockerStatus.OPEN:
            return BlockerStatus.DORMANT
    return derived


def _recalculate_task(session: Session, task_id: int) -> None:
    task = session.get(Task, task_id)
    if task is None:
        return
    rows = _item_rows_for_task(session, task_id)
    if not rows:
        blocker = session.scalar(select(Blocker).where(Blocker.task_id == task_id))
        if blocker is not None:
            session.delete(blocker)
        session.delete(task)
        return

    dated = [(item, report, _report_date(report)) for item, report in rows]
    dates = [occurred for _, _, occurred in dated if occurred is not None]
    first_seen, last_seen = (min(dates), max(dates)) if dates else (None, None)
    latest_date = last_seen
    latest_items = [
        item for item, _, occurred in dated if latest_date is None or occurred == latest_date
    ]
    if any(item.item_type in BLOCKER_ITEM_TYPES for item in latest_items):
        task.status = TaskStatus.BLOCKED
    elif any(_is_completed(item) for item in latest_items):
        task.status = TaskStatus.COMPLETED
    elif any(_is_ongoing(item) for item in latest_items):
        task.status = TaskStatus.IN_PROGRESS
    elif latest_items and all(item.item_type == "next_week_plans" for item in latest_items):
        task.status = TaskStatus.NEW
    else:
        task.status = TaskStatus.UNKNOWN
    task.first_seen_date = first_seen
    task.last_seen_date = last_seen
    task.duration_weeks = (
        ((last_seen - first_seen).days // 7) + 1 if first_seen and last_seen else 1
    )
    latest_title = next((item.title for item in reversed(latest_items) if item.title), task.title)
    task.title = latest_title
    task.related_project = next(
        (item.related_project for item in reversed(latest_items) if item.related_project),
        task.related_project,
    )

    evidence = [entry for entry in dated if entry[0].item_type in BLOCKER_ITEM_TYPES]
    blocker = session.scalar(select(Blocker).where(Blocker.task_id == task_id))
    if not evidence:
        if blocker is not None:
            session.delete(blocker)
        return
    evidence_dates = [occurred for _, _, occurred in evidence if occurred is not None]
    evidence_first = min(evidence_dates) if evidence_dates else None
    evidence_last = max(evidence_dates) if evidence_dates else None
    latest_evidence = next((item for item, _, _ in reversed(evidence) if item.title), evidence[-1][0])
    highest_risk = max(
        (item.risk_level or "低" for item, _, _ in evidence),
        key=lambda value: RISK_RANK.get(value, 1),
    )
    if blocker is None:
        blocker = Blocker(task_id=task_id, title=latest_evidence.title)
        session.add(blocker)
    blocker.title = latest_evidence.title
    blocker.description = latest_evidence.description
    blocker.status = _derive_blocker_status(
        evidence,
        task_status=task.status,
        existing_blocker=blocker,
        today=business_date(utcnow()),
    )
    blocker.risk_level = highest_risk
    blocker.first_seen_date = evidence_first
    blocker.last_seen_date = evidence_last
    blocker.duration_weeks = (
        ((evidence_last - evidence_first).days // 7) + 1
        if evidence_first and evidence_last
        else 1
    )
    blocker.occurrence_count = len({report.id for _, report, _ in evidence})


def _add_item(
    session: Session,
    report: WeeklyReport,
    item_type: str,
    item_order: int,
    raw: dict[str, Any],
    *,
    risk_level: str | None = None,
) -> ReportItem | None:
    title = str(raw.get("title") or "").strip()[:500]
    source_text = str(raw.get("source_text") or "").strip()
    if not title or not source_text:
        return None
    item = ReportItem(
        report_id=report.id,
        employee_id=report.employee_id,
        item_type=item_type,
        item_order=item_order,
        title=title,
        description=str(raw.get("description") or "").strip()[:4000] or None,
        status=str(raw.get("status") or "").strip()[:32] or None,
        related_project=str(raw.get("related_project") or "").strip()[:255] or None,
        task_key=str(raw.get("task_key") or "").strip()[:300] or None,
        mentioned_people=raw.get("mentioned_people") if isinstance(raw.get("mentioned_people"), list) else [],
        confidence=raw.get("confidence") if isinstance(raw.get("confidence"), (int, float)) else None,
        source_text=source_text,
        risk_level=risk_level,
    )
    session.add(item)
    return item


def _resource_task_key(value: str) -> str:
    """为确定的资源需求候选生成稳定的跨周关联键。"""
    compact = _resource_compact(value)
    # ReportItem.task_key 的现有投影上限为 300 字符，预留前缀空间避免写入时
    # 被截断后把两个长资源需求错误合并。
    return f"{RESOURCE_DEMAND_TASK_PREFIX}{compact[:280]}"


def _resource_compact(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value.casefold())


def _existing_task_for_resource(
    session: Session, report: WeeklyReport, value: str
) -> Task | None:
    """若模型已用同一原文建立事项，复用其 Task，避免出现两张卡点卡片。"""
    target = _resource_compact(value)
    if not target:
        return None
    rows = session.execute(
        select(Task, ReportItem)
        .join(TaskReportLink, TaskReportLink.task_id == Task.id)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .where(TaskReportLink.report_id == report.id)
    )
    for task, item in rows:
        if (item.task_key or "").startswith(RESOURCE_DEMAND_TASK_PREFIX):
            continue
        for candidate in (item.source_text, item.title, item.description):
            compact = _resource_compact(candidate or "")
            if compact and (compact == target or target in compact or compact in target):
                return task
    return None


def _resource_title(value: str) -> str:
    """用资源需求首句作为可读标题，但描述/来源仍保留完整字段内容。"""
    # 先移除开头序号再截取首句，否则 ``1.事项`` 会先被句号切成孤零零的
    # ``1``，列表里只能看到「资源需求：待确认的资源支持」。
    value_without_marker = re.sub(r"^\s*\d+\s*[.)、:：-]\s*", "", value).strip()
    first = re.split(r"[\n。.!！？?；;:：]", value_without_marker, maxsplit=1)[0].strip()
    # 表单里常见以 ``1.``/``1、`` 开头的多项需求；序号不是标题内容，
    # 去掉后卡点列表才不会出现「资源需求：1」这类无法判断的条目。
    first = re.sub(r"^\s*\d+\s*[.)、:：-]\s*", "", first).strip()
    if not first or re.fullmatch(r"\d+", first):
        first = "待确认的资源支持"
    return (f"资源需求：{first or value}")[:500]


def materialize_resource_demand(
    session: Session, report: WeeklyReport
) -> bool:
    """把企微「资源需求」字段投影为可查询的待确认卡点。

    资源需求是确定的表单字段，不应等待 AI 才可见；AI 后续成功时仍会调用本
    函数，因此重复执行安全。候选使用 ``resource_demand:`` 任务键，与模型产出
    的同一报告事项分开标记，同时共用既有 Task/Blocker 聚合和来源链路。
    返回值表示本次是否存在有效资源需求。
    """
    existing_items = list(
        session.scalars(
            select(ReportItem).where(
                ReportItem.report_id == report.id,
                ReportItem.item_type == "blockers",
                ReportItem.task_key.like(f"{RESOURCE_DEMAND_TASK_PREFIX}%"),
            )
        )
    )
    value = extract_resource_demand(report.content_raw or report.content)
    expected_source = f"资源需求：{value}" if value else ""
    expected_key = _resource_task_key(value) if value else ""
    expected_title = _resource_title(value) if value else ""
    # 回填/重复同步的常见路径：候选没有变化时保持原 item/link/blocker 主键，
    # 避免无意义的删除重建，也保护其它模块可能保存的来源引用。
    if (
        value
        and len(existing_items) == 1
        and existing_items[0].task_key == expected_key
        and existing_items[0].source_text == expected_source
    ):
        # 标题规则升级或历史数据由旧版本生成时，仅更新派生展示字段，
        # 保留 ReportItem / Task / Blocker 主键及管理跟进引用。
        if existing_items[0].title != expected_title:
            existing_items[0].title = expected_title
            existing_items[0].description = value[:4000]
            session.flush()
            task_ids = set(
                session.scalars(
                    select(TaskReportLink.task_id).where(
                        TaskReportLink.report_item_id == existing_items[0].id
                    )
                )
            )
            for task_id in task_ids:
                _recalculate_task(session, task_id)
        return True
    if not value and not existing_items:
        return False

    old_task_ids: set[int] = set()
    if existing_items:
        old_task_ids = set(
            session.scalars(
                select(TaskReportLink.task_id).where(
                    TaskReportLink.report_item_id.in_(
                        [item.id for item in existing_items]
                    )
                )
            )
        )
        session.execute(
            delete(TaskReportLink).where(
                TaskReportLink.report_item_id.in_([item.id for item in existing_items])
            )
        )
        session.execute(
            delete(ReportItem).where(
                ReportItem.id.in_([item.id for item in existing_items])
            )
        )
        session.flush()

    affected_task_ids = set(old_task_ids)
    if value:
        # 调用方可能使用 autoflush=False；模型事项刚在同一事务中加入时，先
        # 落库再计算顺序，避免与模型的 blockers[0] 触发唯一约束。
        session.flush()
        max_order = session.scalar(
            select(func.max(ReportItem.item_order)).where(
                ReportItem.report_id == report.id,
                ReportItem.item_type == "blockers",
            )
        )
        next_order = int(max_order) + 1 if max_order is not None else 0
        item = _add_item(
            session,
            report,
            "blockers",
            next_order,
            {
                "title": _resource_title(value),
                "description": value[:4000],
                "status": "待确认",
                "task_key": _resource_task_key(value),
                "confidence": 1.0,
                "source_text": f"资源需求：{value}",
            },
        )
        if item is not None:
            session.flush()
            task = _existing_task_for_resource(session, report, value)
            task = task or _get_or_create_task(session, item)
            if task is not None:
                session.add(
                    TaskReportLink(
                        task_id=task.id,
                        report_id=report.id,
                        report_item_id=item.id,
                        match_method="resource_demand",
                        match_confidence=1.0,
                    )
                )
                affected_task_ids.add(task.id)
                session.flush()

    for task_id in affected_task_ids:
        _recalculate_task(session, task_id)
    return bool(value)


def backfill_resource_demands(
    session: Session,
    start: date | None = None,
    end: date | None = None,
    limit: int = 5000,
) -> dict[str, int]:
    """为历史周报补齐确定的资源需求候选，不调用模型。

    只处理 ``weekly`` 周报，默认限制批量大小，避免误把全量历史数据一次性
    压入内存。调用方负责提交事务；返回扫描和命中数量便于审计。
    """
    from app.services.view_service import non_exempt_report_condition

    query = select(WeeklyReport).where(
        WeeklyReport.report_type == "weekly",
        non_exempt_report_condition(session),
    )
    if start is not None and end is not None:
        from app.services.view_service import report_period_overlap_conditions

        query = query.where(*report_period_overlap_conditions(start, end))
    reports = session.scalars(
        query.order_by(WeeklyReport.id).limit(max(1, min(limit, 10000)))
    ).all()
    matched = 0
    for report in reports:
        if materialize_resource_demand(session, report):
            matched += 1
    return {"scanned": len(reports), "matched": matched}


def materialize_report_analysis(
    session: Session,
    report: WeeklyReport,
    result: dict[str, Any],
    prompt_version: str | None,
) -> None:
    """替换一篇周报的派生事项、评分并局部刷新受影响的任务和卡点。"""
    old_task_ids = set(
        session.scalars(
            select(TaskReportLink.task_id).where(TaskReportLink.report_id == report.id)
        )
    )
    session.execute(delete(TaskReportLink).where(TaskReportLink.report_id == report.id))
    session.execute(delete(ReportItem).where(ReportItem.report_id == report.id))
    session.flush()

    items: list[tuple[ReportItem, dict[str, Any]]] = []
    for item_type in ITEM_TYPES:
        raw_items = result.get(item_type) or []
        if not isinstance(raw_items, list):
            continue
        for item_order, raw in enumerate(raw_items):
            if isinstance(raw, dict):
                item = _add_item(session, report, item_type, item_order, raw)
                if item is not None:
                    items.append((item, raw))
    risks = result.get("risks") or []
    if isinstance(risks, list):
        for item_order, raw in enumerate(risks):
            if isinstance(raw, dict):
                risk_item = _add_item(
                    session,
                    report,
                    "risks",
                    item_order,
                    {**raw, "status": "风险"},
                    risk_level=str(raw.get("level") or "中"),
                )
                if risk_item is not None:
                    items.append((risk_item, raw))
    session.flush()

    affected_task_ids = set(old_task_ids)
    for item, raw in items:
        # P2 降噪：模型判定「不需要管理层介入」的卡点类提及只存 ReportItem
        # 备查，不再生成 Task/Blocker 淹没处理中心。缺字段（旧缓存）视为需要。
        if item.item_type in BLOCKER_ITEM_TYPES and raw.get("actionable") is False:
            continue
        # 模型会把「资源需求：Discuss later」这类占位回答复述成卡点/风险
        # （实测标题固定为「资源需求未明确」）。资源需求直出层判空后，噪音
        # 会从模型通道换个标题回来，因此按标题用同一份哨兵表再拦一道 ——
        # 与经营看板排除占位卡点是同一口径。非卡点类事项不受影响。
        if item.item_type in BLOCKER_ITEM_TYPES and is_empty_resource_demand(item.title):
            continue
        # P1 语义去重：模型判定与本人已有卡点是同一件事（即使措辞不同）时，
        # 直接复用该 Task，跳过脆弱的规范键精确匹配。
        task: Task | None = None
        ref = raw.get("existing_task_ref")
        if isinstance(ref, int) and not isinstance(ref, bool) and ref > 0:
            task = session.get(Task, ref)
        from_ref = task is not None
        if task is None:
            task = _get_or_create_task(session, item)
        if task is None:
            continue
        session.add(
            TaskReportLink(
                task_id=task.id,
                report_id=report.id,
                report_item_id=item.id,
                match_method=(
                    "model_ref"
                    if from_ref
                    else ("model_key" if item.task_key else "normalized_title")
                ),
                match_confidence=item.confidence,
            )
        )
        affected_task_ids.add(task.id)

    breakdown = result.get("score_breakdown") or {}
    score = session.scalar(select(ReportScore).where(ReportScore.report_id == report.id))
    if score is None:
        score = ReportScore(report_id=report.id, total_score=0)
        session.add(score)
    score.total_score = int(result.get("score") or 0)
    for field in (
        "completeness",
        "specificity",
        "results",
        "advancement",
        "planning",
        "problem_quality",
        "duplicate",
    ):
        value = breakdown.get(field) if isinstance(breakdown, dict) else None
        setattr(score, field, int(value) if isinstance(value, int) else None)
    score.reason = (breakdown.get("reason") if isinstance(breakdown, dict) else None) or result.get("thin_reason")
    score.prompt_version = prompt_version

    session.flush()
    for task_id in affected_task_ids:
        _recalculate_task(session, task_id)

    # 资源需求字段是确定的卡点来源。放在模型结果之后，保证每次模型重跑都会
    # 保留该候选，即便模型遗漏了 blockers 数组；同时不增加第二次模型调用。
    materialize_resource_demand(session, report)


def backfill_cached_analyses(session: Session, limit: int = 100) -> int:
    """把 P0 已成功但尚未投影的数据补入新表，不触发模型调用。"""
    from app.services.view_service import non_exempt_report_condition

    rows = session.execute(
        select(AIAnalysis, WeeklyReport)
        .join(WeeklyReport, WeeklyReport.id == AIAnalysis.entity_id)
        .outerjoin(ReportScore, ReportScore.report_id == WeeklyReport.id)
        .where(
            AIAnalysis.entity_type == "weekly_report",
            AIAnalysis.analysis_type == "report_analysis",
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
            ReportScore.id.is_(None),
            non_exempt_report_condition(session),
        )
        .order_by(AIAnalysis.id)
        .limit(limit)
    )
    count = 0
    for analysis, report in rows:
        materialize_report_analysis(
            session, report, analysis.result_json or {}, analysis.prompt_version
        )
        count += 1
    return count


def recompute_blocker_status(
    session: Session, blocker_id: int, *, ignore_manual: bool = False
) -> Blocker | None:
    """按当前证据重算单个卡点状态。

    ``ignore_manual=True`` 用于「重新打开」：绕过手动 RESOLVED 的保护，
    让状态回到证据推导值（候选/正式/休眠/归档）。
    """
    blocker = session.get(Blocker, blocker_id)
    if blocker is None:
        return None
    task = session.get(Task, blocker.task_id)
    if task is None:
        return blocker
    evidence = [
        (item, report, _report_date(report))
        for item, report in _item_rows_for_task(session, blocker.task_id)
        if item.item_type in BLOCKER_ITEM_TYPES
    ]
    blocker.status = _derive_blocker_status(
        evidence,
        task_status=task.status,
        existing_blocker=None if ignore_manual else blocker,
        today=business_date(utcnow()),
    )
    return blocker


def sweep_blocker_lifecycle(session: Session, limit: int | None = None) -> dict[str, int]:
    """时间驱动的卡点状态刷新（静默降档 / 新证据复活）。

    状态全部由证据推导，重放任意次结果一致；由 Worker 每轮队列与同步
    调度后调用，也可手动触发。RESOLVED 不参与刷新（不被静默覆盖）。
    """
    task_ids = list(
        session.scalars(
            select(Blocker.task_id).where(Blocker.status != BlockerStatus.RESOLVED)
        )
    )
    if limit is not None:
        task_ids = task_ids[:limit]
    for task_id in task_ids:
        _recalculate_task(session, task_id)
    return {"swept": len(task_ids)}
