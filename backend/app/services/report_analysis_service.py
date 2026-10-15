"""每篇周报一次调用的统一结构化分析。

与旧 duplicate/quality 的两段式服务不同，这里把摘要、事项、卡点、计划、
风险、评分、重复度放进同一个模型调用里产出。权威结果落在
ai_analyses.report_analysis，派生标记回写 weekly_reports 供列表筛选，
页面只读库、不重复触发模型。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai import chat_with_options, get_provider
from app.ai.base import LLMError, LLMUsage
from app.ai.json_util import extract_json_object
from app.ai.prompts import (
    REPORT_ANALYSIS_BLOCKER_REFS_SECTION,
    REPORT_ANALYSIS_PROJECTS_SECTION,
    REPORT_ANALYSIS_PROMPT_VERSION,
    REPORT_ANALYSIS_SYSTEM,
    REPORT_ANALYSIS_USER_TEMPLATE,
)
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.ai_analysis import AIAnalysis
from app.models.blocker import Blocker
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus, BlockerStatus
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services.report_text import cleaned_body, report_body
from app.services.runtime_config_service import runtime_settings
from app.services.structured_data_service import (
    RESOURCE_DEMAND_TASK_PREFIX,
    materialize_report_analysis,
)
from app.services.view_service import department_name_map

logger = get_logger(__name__)

REPORT_ANALYSIS_TYPE = "report_analysis"
ENTITY_TYPE = "weekly_report"
MAX_ITEMS = 30

ITEM_KEYS = (
    "title",
    "description",
    "status",
    "related_project",
    "task_key",
    "mentioned_people",
    "confidence",
    "source_text",
)


def report_payload_hash(report: WeeklyReport) -> str:
    """参与分析输入的正文指纹。正文变化时指纹变化，旧 AI 结论立即失效。

    **刻意不含 `content_raw`**：它是未清洗的富文本，同一段文字只因为内联样式
    重排（`style="width:1496px"` 顺序变化）就会变，而送进模型的其实是清洗后的
    `content`。用 raw 参与哈希会让一次无意义的重排被判定为「内容有变」，
    于是清掉缓存、把 `ai_checked_at` 置空、白调一次模型。
    """
    payload = {
        "employee_id": report.employee_id,
        "external_id": report.external_id,
        "report_type": report.report_type,
        "title": report.title,
        "content": cleaned_body(report),
        "start_date": report.start_date.isoformat() if report.start_date else None,
        "end_date": report.end_date.isoformat() if report.end_date else None,
        "submitted_at": report.submitted_at.isoformat() if report.submitted_at else None,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _empty_result() -> dict[str, Any]:
    return {
        "summary": "正文为空，未生成摘要。",
        "score": 0,
        "is_thin": True,
        "thin_reason": "正文为空",
        "is_duplicate": False,
        "duplicate_score": 0,
        "duplicate_reason": "无内容可比对",
        "score_breakdown": {
            "completeness": None,
            "specificity": None,
            "results": None,
            "advancement": None,
            "planning": None,
            "problem_quality": None,
            "duplicate": None,
            "reason": "",
        },
        "completed_items": [],
        "ongoing_items": [],
        "results": [],
        "blockers": [],
        "problems": [],
        "dependencies": [],
        "next_week_plans": [],
        "risks": [],
    }


def _text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _clamp_int(value: Any, default: int = 0, max_value: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(max_value, parsed))


def _clamp_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, parsed))


def _people(value: Any) -> list[str]:
    if isinstance(value, list):
        result = []
        for item in value:
            text = _text(item, 100)
            if text:
                result.append(text)
        return result[:20]
    text = _text(value, 200)
    return [text] if text else []


def _normalize_task_ref(value: Any) -> int | None:
    """收敛 existing_task_ref：非正整数一律视为「没有引用」。

    缺字段（旧缓存/模型省略）等价于 null，不改变既有行为。
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        ref = int(value)
    except (TypeError, ValueError):
        return None
    return ref if ref > 0 else None


def _normalize_item(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        return {
            key: ([] if key == "mentioned_people" else None)
            for key in ITEM_KEYS
        }
    return {
        "title": _text(raw.get("title"), 300),
        "description": _text(raw.get("description"), 1000),
        "status": _text(raw.get("status"), 50),
        "related_project": _text(raw.get("related_project"), 200),
        "task_key": _text(raw.get("task_key"), 300),
        "mentioned_people": _people(raw.get("mentioned_people")),
        "confidence": _clamp_float(raw.get("confidence")),
        "source_text": _text(raw.get("source_text"), 2000),
        # 默认 True：旧缓存/模型省略该字段时保持既有行为（仅显式 false 才降噪）。
        "actionable": raw.get("actionable") is not False,
        "existing_task_ref": _normalize_task_ref(raw.get("existing_task_ref")),
    }


def _normalize_risk(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        return {"title": "", "description": "", "level": "低", "source_text": ""}
    level = _text(raw.get("level"), 10)
    if level not in {"低", "中", "高"}:
        level = "中"
    return {
        "title": _text(raw.get("title"), 300),
        "description": _text(raw.get("description"), 1000),
        "level": level,
        "source_text": _text(raw.get("source_text"), 2000),
        "actionable": raw.get("actionable") is not False,
        "existing_task_ref": _normalize_task_ref(raw.get("existing_task_ref")),
    }


def _normalize_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [_normalize_item(item) for item in raw[:MAX_ITEMS]]


def _normalize_score_breakdown(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_result()["score_breakdown"]
    limits = {
        "completeness": 15,
        "specificity": 20,
        "results": 20,
        "advancement": 20,
        "planning": 10,
        "problem_quality": 5,
        "duplicate": 10,
    }
    return {
        **{key: _clamp_int(raw.get(key), default=0, max_value=limit) for key, limit in limits.items()},
        "reason": _text(raw.get("reason"), 1000),
    }


def normalize_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """把模型输出收敛为稳定结构：数组非列表/缺字段都不会让页面崩。"""
    result = _empty_result()
    result.update(
        {
            "summary": _text(parsed.get("summary"), 2000),
            "score": _clamp_int(parsed.get("score")),
            "is_thin": bool(parsed.get("is_thin", False)),
            "thin_reason": _text(parsed.get("thin_reason"), 300),
            "is_duplicate": bool(parsed.get("is_duplicate", False)),
            "duplicate_score": _clamp_int(parsed.get("duplicate_score")),
            "duplicate_reason": _text(parsed.get("duplicate_reason"), 300),
            "score_breakdown": _normalize_score_breakdown(parsed.get("score_breakdown")),
        }
    )
    for key in (
        "completed_items",
        "ongoing_items",
        "results",
        "blockers",
        "problems",
        "dependencies",
        "next_week_plans",
    ):
        result[key] = _normalize_items(parsed.get(key))
    risks = parsed.get("risks")
    result["risks"] = (
        [_normalize_risk(item) for item in risks[:MAX_ITEMS]]
        if isinstance(risks, list)
        else []
    )
    return result


def latest_unified_analysis(
    session: Session, report_id: int
) -> AIAnalysis | None:
    return session.scalars(
        select(AIAnalysis)
        .where(
            AIAnalysis.entity_type == ENTITY_TYPE,
            AIAnalysis.entity_id == report_id,
            AIAnalysis.analysis_type == REPORT_ANALYSIS_TYPE,
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
        )
        .order_by(AIAnalysis.id.desc())
        .limit(1)
    ).first()


def current_analysis_report_ids(session: Session, reports: list[WeeklyReport]) -> set[int]:
    """Reports with a successful analysis for their current input and current display markers."""
    marked: dict[int, str] = {}
    for report in reports:
        if report.ai_checked_at is None:
            continue
        current_hash = report_payload_hash(report)
        if report.analysis_payload_hash == current_hash:
            marked[report.id] = current_hash
    if not marked:
        return set()
    return {
        int(report_id)
        for report_id, input_data in session.execute(
            select(AIAnalysis.entity_id, AIAnalysis.input_data).where(
                AIAnalysis.entity_type == ENTITY_TYPE,
                AIAnalysis.analysis_type == REPORT_ANALYSIS_TYPE,
                AIAnalysis.status == AIAnalysisStatus.SUCCESS,
                AIAnalysis.entity_id.in_(marked),
            )
        )
        if isinstance(input_data, dict)
        and input_data.get("content_hash") == marked.get(int(report_id))
    }


def latest_unified_results(
    session: Session, report_ids: list[int]
) -> dict[int, dict[str, Any]]:
    if not report_ids:
        return {}
    rows = session.scalars(
        select(AIAnalysis)
        .where(
            AIAnalysis.entity_type == ENTITY_TYPE,
            AIAnalysis.analysis_type == REPORT_ANALYSIS_TYPE,
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
            AIAnalysis.entity_id.in_(report_ids),
        )
        .order_by(AIAnalysis.id.desc())
    )
    results: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.entity_id not in results:
            results[row.entity_id] = {
                "result": row.result_json or {},
                "checked_at": row.created_at.isoformat() if row.created_at else None,
            }
    return results


def clear_report_analysis_cache(session: Session, report: WeeklyReport) -> None:
    """正文变化 / 强制重跑前清掉列表标记，避免页面读到旧结论。"""
    report.ai_checked_at = None
    report.analysis_payload_hash = None
    report.ai_duplicate_flag = None
    report.ai_duplicate_score = None
    report.ai_quality_flag = None


def _history_text(reports: list[WeeklyReport]) -> str:
    if not reports:
        return "（无历史周报）"
    chunks: list[str] = []
    for report in reports:
        period = (
            f"{report.start_date.isoformat()}~{report.end_date.isoformat()}"
            if report.start_date and report.end_date
            else "-"
        )
        body = report_body(report)
        chunks.append(f"---\n周期: {period}\n正文:\n{body}\n")
    return "\n".join(chunks)


# 回灌给模型的「已登记项目名称」条数上限。这是让 related_project 跨周一致的关键
# （每次调用都是独立的，不回灌就等于要求它在看不见上次写了什么的情况下起出同名），
# 但它的作用只是**提示可选名称**，不是给模型看全量数据，所以按使用频次取前一批即可。
KNOWN_PROJECT_NAME_LIMIT = 120


def _known_project_names(session: Session, *, limit: int = KNOWN_PROJECT_NAME_LIMIT) -> list[str]:
    """库中已出现过的业务对象名称，按使用频次排序。

    只取名称、不取正文：体积小、不泄漏内容，且模型只需要「用哪个词」这一点信息。

    用频次排序而不是最近使用：频次高的就是公司反复在推的主线，最值得保持一致。
    排除资源需求类任务 —— 它们的标题形如「资源需求：Discuss later」，被当成项目名
    回灌会引导模型把占位文案写成项目名。
    """
    rows = session.execute(
        select(ReportItem.related_project, func.count(ReportItem.id))
        .join(TaskReportLink, TaskReportLink.report_item_id == ReportItem.id)
        .where(
            ReportItem.related_project.isnot(None),
            or_(
                ReportItem.task_key.is_(None),
                ReportItem.task_key.not_like(f"{RESOURCE_DEMAND_TASK_PREFIX}%"),
            ),
        )
        .group_by(ReportItem.related_project)
        .order_by(func.count(ReportItem.id).desc(), ReportItem.related_project)
        .limit(limit)
    ).all()
    return [str(name).strip() for name, _count in rows if str(name).strip()]


BLOCKER_REF_LOOKBACK_WEEKS = 8
BLOCKER_REF_LIMIT = 30


def _open_blocker_candidates(
    session: Session, report: WeeklyReport, *, limit: int = BLOCKER_REF_LIMIT
) -> list[dict[str, Any]]:
    """该员工近期未关闭（未归档/未解决）的卡点，作为 existing_task_ref 候选。

    只取标题与编号（Task.id），体积可控；限定本员工，避免跨人误归并。
    """
    if not report.employee_id:
        return []
    anchor = report.end_date or report.start_date or date.today()
    since = anchor - timedelta(weeks=BLOCKER_REF_LOOKBACK_WEEKS)
    rows = session.execute(
        select(Task.id, Blocker.title, Blocker.last_seen_date)
        .join(Blocker, Blocker.task_id == Task.id)
        .join(TaskReportLink, TaskReportLink.task_id == Task.id)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .where(
            ReportItem.employee_id == report.employee_id,
            Blocker.status.not_in([BlockerStatus.ARCHIVED, BlockerStatus.RESOLVED]),
            Blocker.last_seen_date >= since,
        )
        .distinct()
        .order_by(Blocker.last_seen_date.desc(), Task.id)
        .limit(limit)
    ).all()
    return [
        {"task_id": task_id, "title": title, "last_seen": last_seen}
        for task_id, title, last_seen in rows
    ]


def _blocker_refs_section(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return ""
    lines = []
    for candidate in candidates:
        seen = (
            candidate["last_seen"].isoformat()
            if getattr(candidate["last_seen"], "isoformat", None)
            else str(candidate["last_seen"])
        )
        lines.append(f"#{candidate['task_id']} {candidate['title']}（最近出现 {seen}）")
    return REPORT_ANALYSIS_BLOCKER_REFS_SECTION.format(items="\n".join(lines))


def _write_analysis(
    session: Session,
    report_id: int,
    result: dict[str, Any],
    status: str,
    model_name: str | None,
    prompt_version: str,
    input_data: dict[str, Any],
    duration_ms: int | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    error: str | None = None,
) -> None:
    settings = runtime_settings(session)
    session.add(
        AIAnalysis(
            entity_type=ENTITY_TYPE,
            entity_id=report_id,
            analysis_type=REPORT_ANALYSIS_TYPE,
            model_provider=settings.ai_provider,
            model_name=model_name,
            prompt_version=prompt_version,
            result_json=result if error is None else {**result, "error": error},
            input_data=input_data,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status=status,
            error_message=error,
            created_at=utcnow(),
        )
    )


def analyze_report(
    session: Session,
    report_id: int,
    force: bool = False,
    *,
    queue_attempt: int | None = None,
) -> dict[str, Any]:
    """对单篇周报执行一次统一结构化分析（调用前先查库，命中直接返回）。"""
    settings = runtime_settings(session)
    report = session.get(WeeklyReport, report_id)
    if report is None:
        raise ValueError(f"周报 {report_id} 不存在")
    from app.services.view_service import report_ai_exempt

    if report_ai_exempt(session, report):
        raise ValueError("免提交人员不参与 AI 分析；取消免提交后可重新扫描")

    current_hash = report_payload_hash(report)
    if (
        not force
        and report.ai_checked_at is not None
        and report.analysis_payload_hash == current_hash
    ):
        cached = latest_unified_analysis(session, report_id)
        if cached is not None:
            materialize_report_analysis(
                session, report, cached.result_json or {}, cached.prompt_version
            )
            session.commit()
            return {**(cached.result_json or {}), "cached": True}

    employee = (
        session.get(Employee, report.employee_id) if report.employee_id else None
    )
    period = (
        f"{report.start_date.isoformat()}~{report.end_date.isoformat()}"
        if report.start_date and report.end_date
        else "-"
    )
    body = report_body(report, placeholder="")
    input_base = {"content_hash": current_hash}

    if not body:
        result = _empty_result()
        _write_analysis(
            session,
            report.id,
            result,
            AIAnalysisStatus.SUCCESS,
            None,
            REPORT_ANALYSIS_PROMPT_VERSION,
            input_base,
            0,
        )
        report.ai_checked_at = utcnow()
        report.analysis_payload_hash = current_hash
        report.ai_duplicate_flag = False
        report.ai_duplicate_score = 0
        report.ai_quality_flag = True
        materialize_report_analysis(
            session, report, result, REPORT_ANALYSIS_PROMPT_VERSION
        )
        session.commit()
        return {**result, "cached": False}

    history = (
        list(
            session.scalars(
                select(WeeklyReport)
                .where(
                    WeeklyReport.employee_id == report.employee_id,
                    WeeklyReport.id != report.id,
                )
                .order_by(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc())
                .limit(settings.ai_duplicate_lookback)
            )
        )
        if report.employee_id
        else []
    )

    dept_name = None
    if employee and employee.department_id:
        dept_name = department_name_map(session).get(employee.department_id)
    department_part = f"（{dept_name}）" if dept_name else ""

    history_text = _history_text(history)
    budget = settings.ai_max_input_chars
    # 正文本身也必须受预算约束：只截历史、不截正文时，单篇超长周报会整篇发出，
    # 直接撑爆模型上下文 -> 报错 -> 按 ai_analysis_max_attempts 重试（每次都计费）。
    # 截断必须**如实标注**，不能让模型把半篇当成完整正文（需求第 24 节禁止静默截断）。
    body_truncated = False
    if len(body) > budget:
        body = body[:budget] + "\n\n（本篇正文过长，已按单篇预算截断）"
        body_truncated = True
    remaining = max(0, budget - len(body))
    if len(history_text) > remaining:
        history_text = history_text[:remaining]

    # 已登记名称放在历史之后，并从**剩余预算里**分：它属于「提升一致性」的附加信息，
    # 不该挤掉正文与历史这两项判断重复度必需的内容。预算不足时按条截断，
    # 宁可少给几个名称，也不能把提示词撑爆（撑爆会报错并按次数重试，每次都计费）。
    known_projects = _known_project_names(session)
    projects_section = ""
    if known_projects:
        project_budget = max(0, budget - len(body) - len(history_text))
        if project_budget > 40:
            listing = "、".join(known_projects)
            if len(listing) > project_budget:
                listing = listing[:project_budget]
            projects_section = REPORT_ANALYSIS_PROJECTS_SECTION.format(names=listing)

    # 本人近期未关闭卡点：existing_task_ref 的候选清单，同样从剩余预算里分。
    blocker_refs_section = ""
    blocker_candidates = _open_blocker_candidates(session, report)
    if blocker_candidates:
        refs_budget = max(0, budget - len(body) - len(history_text) - len(projects_section))
        section = _blocker_refs_section(blocker_candidates)
        if refs_budget > 40 and len(section) <= refs_budget:
            blocker_refs_section = section
        elif refs_budget > 40:
            blocker_refs_section = section[:refs_budget]

    user_prompt = REPORT_ANALYSIS_USER_TEMPLATE.format(
        employee=employee.name if employee else "未知",
        department=department_part,
        period=period,
        title=report.title or "",
        current=body,
        history=history_text,
        projects=projects_section,
        blocker_refs=blocker_refs_section,
    )
    input_data = {
        **input_base,
        "history_report_ids": [r.id for r in history],
        "context_chars": len(user_prompt),
        "body_chars": len(body),
        "body_truncated": body_truncated,
        "known_project_count": len(known_projects),
        "blocker_ref_count": len(blocker_candidates),
    }
    if queue_attempt is not None:
        input_data["queue_attempt"] = queue_attempt

    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None
    usage = LLMUsage()
    try:
        provider = get_provider(settings)
        started = time.monotonic()
        llm_result = chat_with_options(
            provider,
            REPORT_ANALYSIS_SYSTEM,
            user_prompt,
            thinking=False,
            json_mode=True,
            usage=usage,
        )
        duration_ms = int(round((time.monotonic() - started) * 1000))
        model_name = llm_result.model or settings.ai_model
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        parsed = extract_json_object(llm_result.content)
    except (LLMError, ValueError) as exc:
        logger.warning("统一分析失败（周报 %s）：%s", report_id, exc)
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        input_data["usage"] = usage.as_metadata()
        _write_analysis(
            session,
            report.id,
            {},
            AIAnalysisStatus.FAILED,
            model_name,
            REPORT_ANALYSIS_PROMPT_VERSION,
            input_data,
            duration_ms,
            prompt_tokens,
            completion_tokens,
            str(exc),
        )
        session.commit()
        raise

    result = normalize_result(parsed)
    result["raw"] = json.dumps(parsed, ensure_ascii=False)[:4000]
    input_data["usage"] = usage.as_metadata()
    _write_analysis(
        session,
        report.id,
        result,
        AIAnalysisStatus.SUCCESS,
        model_name,
        REPORT_ANALYSIS_PROMPT_VERSION,
        input_data,
        duration_ms,
        prompt_tokens,
        completion_tokens,
    )

    report.ai_checked_at = utcnow()
    report.analysis_payload_hash = current_hash
    report.ai_duplicate_flag = bool(result.get("is_duplicate"))
    report.ai_duplicate_score = int(result.get("duplicate_score") or 0)
    report.ai_quality_flag = bool(result.get("is_thin"))
    materialize_report_analysis(
        session, report, result, REPORT_ANALYSIS_PROMPT_VERSION
    )
    session.commit()
    return {**result, "cached": False}
