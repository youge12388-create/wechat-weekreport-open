"""Agent 可调用的周报检索工具。

分层约束（与整个项目一致）：工具**只读**，不写业务表、不调模型。
它们是「模型的手」，不是业务逻辑的第二个入口。

设计原则：工具的返回值必须是**可核对的事实**（篇数、周期、真实 ID）。
模型只能依据这些事实作答，编造空间越小，答案越可信。

范围有三档，含义固定：

- ``period``：用户在界面上选定的周期（默认）。
- ``recent``：以所选周期为基准再往前约 4 周。
- ``all``：全部历史周报（不限周期）。

检索范围是硬边界：工具不会因为没有命中而自动扩到其他周期。需要扩大时，用户
可以明确改选「全部周期」或新的日期区间。

**分页**：``search_reports`` 支持 ``offset``。没有它时只能看到命中的前 N 篇，
抽样偏差会被当成全量结论；有了它，模型可以按需把整个命中集翻完（只受步数约束），
并靠返回值里的 ``has_more`` / ``next_offset`` 判断自己是否读完了。

**结构化数据工具**：除正文检索外，另有两个「读系统已有结论」的工具
（``list_submissions`` 提交名单、``list_report_quality`` AI 评分）。
它们不是第二套业务逻辑，只是把看板/体检已算好的权威口径暴露给模型，
避免模型用「正文长度」去猜「谁没交」「谁写得好」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from sqlalchemy import case, distinct, false, func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.ai_analysis import AIAnalysis
from app.models.blocker import Blocker
from app.models.employee import Employee
from app.models.report_item import ReportItem
from app.models.report_translation import ReportTranslation
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services.data_scope import current_scope
from app.services.report_analysis_service import latest_unified_results
from app.services.report_translation_service import translation_source_hash
from app.services.structured_data_service import RESOURCE_DEMAND_TASK_PREFIX
from app.services.view_service import (
    ai_report_condition,
    department_name_map,
    report_ai_exempt,
    report_period_overlap_conditions,
    reportable_employee_conditions,
    submitted_employee_ids,
)

logger = get_logger(__name__)

SCOPE_PERIOD = "period"
SCOPE_RECENT = "recent"
SCOPE_ALL = "all"
SCOPE_ORDER = (SCOPE_PERIOD, SCOPE_RECENT, SCOPE_ALL)

RECENT_DAYS = 27
SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 20
SEARCH_MAX_OFFSET = 200
GET_REPORTS_MAX = 8
MAX_STEPS_DEFAULT = 6
EXCERPT_CHARS = 180
# 评分工具单次最多看多少篇；它只读已入库的分析结论，不触发模型，所以可以比
# 正文检索宽一些 —— 排名需要看到足够多的人才有意义。
QUALITY_MAX_REPORTS = 60
QUALITY_DEFAULT_LIMIT = 30
# 评分工具最多扫描多少条已有结论。它只读库、不调模型，所以放宽一些；
# 仍要设上限，避免某天库里堆了几万条分析后一次问答把内存拉满。
QUALITY_SCAN_MAX = 500
# 名单类工具最多列多少个名字。超出只给数量，避免把上下文塞满。
LIST_NAMES_MAX = 200

# 权威分析结论存放位置（与 report_analysis_service 的常量保持一致）。
REPORT_ANALYSIS_TYPE = "report_analysis"
REPORT_ANALYSIS_ENTITY = "weekly_report"

# 全量上下文最多预载多少篇。只在范围字符数不超阈值时才启用，这个上限用于兜住
# 「篇数极多但每篇极短」的畸形范围（否则仍会一次性塞爆上下文）。
FULL_CONTEXT_MAX_REPORTS = 300
# 卡点 / 任务统计工具的单次条数限制。
BLOCKERS_DEFAULT_LIMIT = 30
BLOCKERS_MAX_LIMIT = 100
TASKS_DEFAULT_LIMIT = 30
TASKS_MAX_LIMIT = 100
# 「长期卡点 / 停滞任务」的周数门槛，与 /api/blockers 的 long 口径保持一致。
LONG_RUNNING_WEEKS = 3


@dataclass
class ToolContext:
    """一次问答内所有工具调用共享的状态。

    它同时是可观测性载体：模型查了什么范围、扩大了没有、真正读了哪几篇，
    都记在这里，最后随答案一起返回给前端。
    """

    session: Session
    period_start: date
    period_end: date
    department_id: int | None = None
    employee_id: int | None = None
    keyword: str | None = None
    # 本轮用户问题原文：语义检索（向量）的查询文本。有关键词时不参与——
    # 关键词是严格证据筛选，语义只能做「浏览模式」的排序增强。
    question: str = ""
    lock_scope: bool = False
    allow_explicit_range_override: bool = False
    resolved_period: tuple[date, date] | None = None
    resolved_time_source: str | None = None
    max_read_chars: int = 12000
    # 用户在界面上选的档位。模型没显式给 scope 时按它来，否则「全部周期」
    # 这个选择会被工具的 period 默认值悄悄吃掉。
    default_scope: str = SCOPE_PERIOD
    searched_scopes: list[str] = field(default_factory=list)
    escalations: list[dict[str, str]] = field(default_factory=list)
    offered_report_ids: set[int] = field(default_factory=set)
    read_report_ids: list[int] = field(default_factory=list)
    # 读了但被字符预算截断的周报：必须回传给前端，避免「读了全文」的错觉。
    truncated_report_ids: list[int] = field(default_factory=list)
    read_chars: int = 0
    keyword_coverage: dict[str, Any] = field(default_factory=dict)
    keyword_translations: dict[int, str] = field(default_factory=dict)
    pagination_pending: bool = False
    coverage_gaps: list[str] = field(default_factory=list)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_reports",
            "description": (
                "按关键词检索周报，返回命中总数与每篇的摘要片段（不含全文）。"
                "摘要片段的来源见 excerpt_origin：ai_summary=入库时预计算的 AI 摘要，"
                "body=正文截取（关键词命中时在命中位置附近），translation=中文译文。"
                "关键词可留空，表示不限定主题、按提交时间列出；"
                "配置了 Embedding 服务时，留空检索按「与用户问题的语义相关度」排序。"
                "关键词一旦给定就是严格证据筛选，与语义排序互斥。"
                "用户问题里有明确时间段（如「9 月第一周」「上上周」）时，"
                "先把时间换算成日期，用 start/end 显式限定检索区间，"
                "不要把「9 月」这类时间词当关键词，也不要靠翻页盲找。"
                "只返回一页；返回值里的 matched_total 是命中总数，"
                "还有更多时用 has_more / next_offset 继续翻页，直到读完为止。"
                "若返回 truncated_reason=offset_cap，说明命中数超过分页上限、"
                "has_more=false 只代表「到此为止」而不是「全部读完」——"
                "此时必须如实说明样本不完整（例如先用关键词缩小范围再查），"
                "不能宣称已经看全。"
                "范围是硬边界；没有命中时说明无结果，不得改查其他周期。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": (
                            "检索关键词；留空表示不限关键词。"
                            "多个词用空格分隔，按「任一词命中」匹配。"
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": list(SCOPE_ORDER),
                        "description": (
                            "period=用户当前选定的周期（默认）；"
                            "recent=该周期再往前约 4 周；all=全部历史周报"
                        ),
                    },
                    "start": {
                        "type": "string",
                        "description": (
                            "显式检索区间开始日 YYYY-MM-DD，必须与 end 同时提供；"
                            "与范围档位取交集，不会越过用户在界面上选定的范围"
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "显式检索区间结束日 YYYY-MM-DD，必须与 start 同时提供"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"每页最多返回多少条摘要，默认 {SEARCH_DEFAULT_LIMIT}",
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "从第几条开始返回，默认 0。"
                            "翻页时把上一次返回的 next_offset 原样传回来。"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_reports",
            "description": (
                "按周报 ID 读取正文全文。只能读 search_reports 返回过的 ID，"
                f"其余会被丢弃。单次最多 {GET_REPORTS_MAX} 篇，且受总字符预算限制；"
                "返回值会标注哪几篇被截断、还剩多少预算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "report_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "要读取的周报 ID 列表",
                    }
                },
                "required": ["report_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_submissions",
            "description": (
                "查询某周期内「谁交了、谁没交」的权威名单（与看板同一口径，"
                "已排除离职与免提交人员）。回答「谁没提交 / 谁在偷懒」这类问题时"
                "必须用它，不要用正文长短或有没有被检索到来推断——"
                "没交周报的人本来就不会出现在正文检索结果里。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "周期开始日 YYYY-MM-DD；留空表示用户当前选定的周期",
                    },
                    "end": {
                        "type": "string",
                        "description": "周期结束日 YYYY-MM-DD；留空表示用户当前选定的周期",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["period", "all"],
                        "description": "period=本轮实际周期；all=所有已知周期",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_report_quality",
            "description": (
                "读取已入库的 AI 质量评分与体检标记（敷衍/重复），"
                "用于回答「谁写得好 / 谁写得敷衍 / 哪几篇像复制」这类问题。"
                "这是系统已有的 AI 参考评分，不要用正文长度自行判断质量。"
                "只有查询范围内全部周报都完成分析时才返回评分；"
                "否则返回覆盖缺口，不能据此排名或判断某人的全部周报。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": list(SCOPE_ORDER),
                        "description": "取值同 search_reports；默认用户当前选定的周期",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["score_asc", "score_desc"],
                        "description": (
                            "score_asc=低分在前（默认，便于找敷衍）；"
                            "score_desc=高分在前（便于找突出）"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"最多返回多少篇，默认 {QUALITY_DEFAULT_LIMIT}",
                    },
                    "report_id": {
                        "type": "integer",
                        "description": "用户只问某一篇周报评分时传该周报 ID；否则代表全范围排名",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_blockers",
            "description": (
                "卡点的真实统计：按部门给出卡点数量、高风险数与长期卡点数，"
                "并附最严重的若干条明细。回答「哪个部门卡点最多 / 有哪些高风险卡点 /"
                "哪些卡点拖了很久」必须用它——这是 SQL 聚合出的数字，"
                "不要靠读周报文本感觉哪个部门问题多。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["OPEN", "RESOLVED", "ALL"],
                        "description": "卡点状态，默认 OPEN（当前未解决）",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["period", "all"],
                        "description": "默认只统计本轮实际周期；只有用户明确要求全部未解决存量时才用 all",
                    },
                    "risk_level": {
                        "type": "string",
                        "enum": ["低", "中", "高"],
                        "description": "只看某个风险等级；留空表示不限",
                    },
                    "min_weeks": {
                        "type": "integer",
                        "description": f"只看持续至少多少周的卡点，例如 {LONG_RUNNING_WEEKS}",
                    },
                    "start": {
                        "type": "string",
                        "description": "周期开始日 YYYY-MM-DD；留空表示不限周期",
                    },
                    "end": {
                        "type": "string",
                        "description": "周期结束日 YYYY-MM-DD；留空表示不限周期",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"明细最多返回多少条，默认 {BLOCKERS_DEFAULT_LIMIT}",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": (
                "跨周任务及其推进状态，用于回答「哪些事情连续多周没有推进」"
                "「停滞的任务」。duration_weeks 由周报证据回算，不是推测；"
                "默认排除已完成任务。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_weeks": {
                        "type": "integer",
                        "description": f"至少持续多少周，默认 {LONG_RUNNING_WEEKS}",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "按任务标题过滤，可留空",
                    },
                    "include_completed": {
                        "type": "boolean",
                        "description": "是否包含已完成任务，默认 false",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"最多返回多少条，默认 {TASKS_DEFAULT_LIMIT}",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_periods",
            "description": "列出库里已有的汇报周期及各自周报篇数，用于确认数据覆盖范围。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_employees",
            "description": "按姓名关键字查员工及其部门，用于把口语化称呼对应到正式姓名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "姓名关键字，可留空"},
                    "department_id": {"type": "integer", "description": "限定部门 ID"},
                },
                "required": [],
            },
        },
    },
]


def _error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _scope_range(ctx: ToolContext, scope: str) -> tuple[date | None, date | None]:
    if scope == SCOPE_ALL:
        return None, None
    if scope == SCOPE_RECENT:
        return ctx.period_end - timedelta(days=RECENT_DAYS), ctx.period_end
    return ctx.period_start, ctx.period_end


def _base_conditions(
    ctx: ToolContext, start: date | None, end: date | None
) -> list[Any]:
    conditions: list[Any] = (
        [] if start is None or end is None else list(report_period_overlap_conditions(start, end))
    )
    conditions.extend(_report_context_conditions(ctx))
    return conditions


def _report_context_conditions(ctx: ToolContext) -> list[Any]:
    """所有周报工具共用的当前账号与显式部门/员工范围。"""
    conditions: list[Any] = [ai_report_condition(ctx.session)]
    if ctx.department_id is not None:
        conditions.append(Employee.department_id == ctx.department_id)
    if ctx.employee_id is not None:
        conditions.append(WeeklyReport.employee_id == ctx.employee_id)
    return conditions


def _keyword_tokens(keyword: str | None) -> list[str]:
    """把关键词切成词元。多个词之间按「任一命中」处理。

    SQL 层只有 LIKE，用整句话去匹配几乎必然落空；按空格切开分别匹配，
    更接近用户「关键词检索」的直觉（周报里少见完整出现提问原句）。
    """
    if not keyword:
        return []
    return [token for token in re.split(r"\s+", keyword.strip()) if token]


def _keyword_matches(
    ctx: ToolContext,
    rows: list[tuple[WeeklyReport, Employee | None]],
    keyword: str | None,
) -> tuple[list[tuple[WeeklyReport, Employee | None]], dict[str, Any]]:
    """按原文或有效中文译文做任一词命中，并记录译文覆盖缺口。"""
    tokens = _keyword_tokens(keyword)
    if not tokens:
        return rows, {
            "candidate_reports": len(rows),
            "matched_reports": len(rows),
            "translation_gaps": 0,
            "complete": True,
        }

    ids = [report.id for report, _ in rows]
    translations = ctx.session.scalars(
        select(ReportTranslation).where(
            ReportTranslation.report_id.in_(ids or [-1]),
            ReportTranslation.target_language == "zh",
        )
    ).all()
    by_report = {row.report_id: row for row in translations}
    wants_chinese = any(re.search(r"[\u3400-\u9fff]", token) for token in tokens)
    matched: list[tuple[WeeklyReport, Employee | None]] = []
    gaps = 0
    for report, employee in rows:
        original = f"{report.title or ''}\n{report.content or ''}"
        translation = by_report.get(report.id)
        translated = None
        if (
            report.content_language == "english"
            and translation is not None
            and translation.status == "success"
            and translation.source_hash == translation_source_hash(report)
        ):
            translated = translation.translated_content or ""
            if translated:
                ctx.keyword_translations[report.id] = translated
        if any(token.casefold() in original.casefold() for token in tokens):
            matched.append((report, employee))
        elif translated and any(token.casefold() in translated.casefold() for token in tokens):
            matched.append((report, employee))
        elif wants_chinese and report.content_language == "english" and not translated:
            gaps += 1
    return matched, {
        "candidate_reports": len(rows),
        "matched_reports": len(matched),
        "translation_gaps": gaps,
        "complete": gaps == 0,
    }


def _order_columns(keyword: str | None):
    """命中排序：标题命中优先，再按提交时间由新到旧。

    纯按时间排序时，「最相关」的旧周报会被最近提交的无关周报挤掉；把标题命中
    提到前面，等于用 SQL 能表达的最弱相关性信号做一次粗排。``submitted_at`` 的
    空值必须显式排到最后 —— SQLite 与 PG 对 NULL 的默认排序方向相反。
    """
    columns = []
    tokens = _keyword_tokens(keyword)
    if tokens:
        first = f"%{tokens[0]}%"
        columns.append(case((WeeklyReport.title.like(first), 0), else_=1))
    columns.append(WeeklyReport.submitted_at.is_(None))
    columns.append(WeeklyReport.submitted_at.desc())
    columns.append(WeeklyReport.id.desc())
    return columns


def _render_report_block(
    report: WeeklyReport,
    employee: Employee | None,
    dept_name: str | None,
    translated: str | None = None,
) -> str:
    period = (
        f"{report.start_date.isoformat()}~{report.end_date.isoformat()}"
        if report.start_date and report.end_date
        else (report.start_date.isoformat() if report.start_date else "-")
    )
    body = (report.content or report.title or "").strip() or "（无正文）"
    return (
        f"---\n周报ID: {report.id}\n"
        f"提交人: {employee.name if employee else '未知'}"
        f"{('（' + dept_name + '）') if dept_name else ''}\n"
        f"周期: {period}\n"
        f"标题: {report.title or '（无标题）'}\n"
        f"正文:\n{body}\n"
        + (f"有效中文译文（辅助匹配，不替代原文）:\n{translated}\n" if translated else "")
    )


def load_full_context(
    ctx: ToolContext, budget: int, scope: str | None = None
) -> dict[str, Any]:
    """把一个范围内的周报原文整体准备好，交给模型直接阅读。

    这是「小范围不做检索」的实现。关键词检索有两个固有损失：漏同义表达
    （「市场部」找不到写「推广/渠道」的同一批工作），以及命中不到非中文周报
    （实测库里 22% 正文是英文，而 LIKE 只匹配中文关键词）。单周期的全文实测
    约 3 万字符，装得下，所以小范围没有理由再去做有损检索。

    范围装不下（超预算或篇数过多）时返回 ``enabled=False``，
    由调用方回退到「工具检索 + 分页」，而不是悄悄截断。
    """
    scope = (scope or ctx.default_scope or SCOPE_PERIOD).strip().lower()
    if scope not in SCOPE_ORDER:
        scope = SCOPE_PERIOD
    start, end = _scope_range(ctx, scope)
    conditions = _base_conditions(ctx, start, end)
    rows = ctx.session.execute(
        select(WeeklyReport, Employee)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
        .order_by(*_order_columns(None))
    ).all()
    rows, keyword_coverage = _keyword_matches(ctx, rows, ctx.keyword)
    ctx.keyword_coverage = keyword_coverage

    dept_names = department_name_map(ctx.session)
    blocks: list[str] = []
    report_ids: list[int] = []
    total = 0
    for report, employee in rows:
        block = _render_report_block(
            report,
            employee,
            dept_names.get(employee.department_id) if employee else None,
            ctx.keyword_translations.get(report.id),
        )
        if total + len(block) > budget or len(blocks) >= FULL_CONTEXT_MAX_REPORTS:
            return {
                "enabled": False,
                "reason": "too_large",
                "scope": scope,
                "report_count": len(rows),
                "chars": total,
                "text": "",
                "report_ids": [],
                "keyword_coverage": keyword_coverage,
            }
        blocks.append(block)
        report_ids.append(report.id)
        total += len(block)

    if not blocks:
        return {
            "enabled": False,
            "reason": "no_keyword_match" if ctx.keyword else "empty",
            "scope": scope,
            "report_count": 0,
            "chars": 0,
            "text": "",
            "report_ids": [],
            "keyword_coverage": keyword_coverage,
        }

    return {
        "enabled": True,
        "reason": None,
        "scope": scope,
        "report_count": len(blocks),
        "chars": total,
        "text": "\n".join(blocks),
        "report_ids": report_ids,
        "keyword_coverage": keyword_coverage,
    }


def load_report_batches(ctx: ToolContext, budget: int) -> dict[str, Any]:
    """按本轮权限和范围取出全部原文，并将长篇拆段、短篇合批。"""
    start, end = _scope_range(ctx, ctx.default_scope)
    rows = list(ctx.session.execute(
        select(WeeklyReport, Employee)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*_base_conditions(ctx, start, end))
        .order_by(*_order_columns(None))
    ).all())
    rows, coverage = _keyword_matches(ctx, rows, ctx.keyword)
    ctx.keyword_coverage = coverage
    dept_names = department_name_map(ctx.session)
    batches: list[str] = []
    current = ""
    report_ids: list[int] = []
    limit = max(1000, budget)
    for report, employee in rows:
        report_ids.append(report.id)
        block = _render_report_block(
            report, employee,
            dept_names.get(employee.department_id) if employee else None,
            ctx.keyword_translations.get(report.id),
        )
        # 每个片段重复周报 ID，确保模型可以把事实追溯到同一篇原文。
        prefix = f"---\n周报ID: {report.id}（续篇）\n"
        size = limit - len(prefix) - 40
        parts = [block[i:i + size] for i in range(0, len(block), size)]
        for index, part in enumerate(parts, 1):
            entry = part if len(parts) == 1 else f"{prefix}片段 {index}/{len(parts)}\n{part}"
            if current and len(current) + len(entry) > limit:
                batches.append(current)
                current = ""
            current += entry
    if current:
        batches.append(current)
    return {
        "batches": batches,
        "report_ids": report_ids,
        "report_count": len(report_ids),
        "keyword_coverage": coverage,
    }


def _task_department_subquery(ctx: ToolContext):
    """每个任务归属的部门：取最近一次关联周报的提交人部门。

    一个任务可能跨部门出现，为「哪个部门卡点最多」这类计数不重复，
    按最近一次活动归属到一个部门，并把这个口径写进返回值。
    """
    ranked_query = (
        select(
            TaskReportLink.task_id.label("task_id"),
            Employee.department_id.label("department_id"),
            func.row_number()
            .over(
                partition_by=TaskReportLink.task_id,
                order_by=(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc()),
            )
            .label("rn"),
        )
        .select_from(TaskReportLink)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(*_report_context_conditions(ctx))
    )
    ranked = ranked_query.subquery()
    return (
        select(ranked.c.task_id, ranked.c.department_id)
        .where(ranked.c.rn == 1)
        .subquery()
    )



def _excerpt(text: str | None) -> str:
    body = " ".join((text or "").split())
    if len(body) <= EXCERPT_CHARS:
        return body
    return body[:EXCERPT_CHARS] + "…"


def _excerpt_around(text: str | None, tokens: list[str]) -> str:
    """关键词命中窗口：以第一个命中位置为中心截取，而不是固定取开头。

    周报常把关键事实写在正文中后段，固定前缀切片会整体错过命中词的上下文，
    模型只能看到「标题像相关」而看不到内容。无命中时退回前缀截取。
    """
    body = " ".join((text or "").split())
    if len(body) <= EXCERPT_CHARS:
        return body
    pos = -1
    folded = body.casefold()
    for token in tokens:
        idx = folded.find(token.casefold())
        if idx >= 0 and (pos < 0 or idx < pos):
            pos = idx
    if pos < 0:
        return _excerpt(body)
    start = max(0, pos - 60)
    return body[start : start + EXCERPT_CHARS] + "…"


def _semantic_order(ctx: ToolContext, reports: list[WeeklyReport]) -> dict[str, Any] | None:
    """浏览模式（无关键词）的语义排序：按「用户问题 ↔ 正文向量」余弦相似度降序。

    语义是排序增强而不是依赖：配置未齐、范围内没有向量、查询向量化失败，
    一律返回 None 静默降级回提交时间序，绝不阻断关键词检索主路径。
    有关键词时不得调用——关键词是严格证据筛选，语义排序会放宽范围。
    """
    if not ctx.question or not reports:
        return None
    from app.ai.embedding import get_embedding_client
    from app.services.embedding_service import cosine_similarity, load_vectors
    from app.services.runtime_config_service import runtime_settings

    settings = runtime_settings(ctx.session)
    client = get_embedding_client(settings)
    if not client.configured():
        return None
    capped = reports[: settings.ai_embedding_max_reports]
    vectors = load_vectors(ctx.session, [report.id for report in capped])
    if not vectors:
        return None
    try:
        query_vector = client.embed_texts([ctx.question])[0]
    except Exception as exc:  # noqa: BLE001 查询向量化失败必须降级
        logger.warning("语义检索查询向量化失败，本次按提交时间排序：%s", exc)
        return None
    scored: list[tuple[float, WeeklyReport]] = []
    for report in capped:
        entry = vectors.get(report.id)
        if entry is None:
            continue
        scored.append((cosine_similarity(query_vector, entry[1]), report))
    scored.sort(key=lambda item: item[0], reverse=True)
    rank = {report.id: index for index, (_, report) in enumerate(scored)}
    return {
        "model": client.settings.ai_embedding_model,
        "ranked": len(scored),
        "covered": len(vectors),
        "candidate_total": len(reports),
        "candidate_truncated": len(reports) > len(capped),
        "top_score": round(scored[0][0], 4) if scored else None,
        "_rank": rank,
    }


def _search_reports(ctx: ToolContext, args: dict[str, Any]) -> str:
    # 页面关键词是严格证据筛选；模型不能删掉或替换它来扩大材料。
    keyword = ctx.keyword or str(args.get("keyword") or "").strip() or None
    requested_scope = str(args.get("scope") or ctx.default_scope).strip().lower()
    if requested_scope not in SCOPE_ORDER:
        requested_scope = ctx.default_scope
    if ctx.lock_scope:
        requested_scope = ctx.default_scope

    # 显式日期区间：模型把用户问题里的时间表述换算成日期传进来。
    # 只传其一时直接报错 —— 补一半的默认值会让「查 9 月第一周」静默变成
    # 「查当前周期」，模型还以为查对了。
    raw_start = str(args.get("start") or "").strip()
    raw_end = str(args.get("end") or "").strip()
    explicit_start: date | None = None
    explicit_end: date | None = None
    if raw_start or raw_end:
        try:
            explicit_start = date.fromisoformat(raw_start)
            explicit_end = date.fromisoformat(raw_end)
        except ValueError:
            return _error("start/end 必须同时提供且为 YYYY-MM-DD 格式")
        if explicit_start > explicit_end:
            return _error("start 不能晚于 end")

    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else SEARCH_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = SEARCH_DEFAULT_LIMIT
    limit = max(1, min(limit, SEARCH_MAX_LIMIT))

    offset = args.get("offset")
    try:
        offset = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        offset = 0
    requested_offset = max(0, offset)
    # 必须记住「请求是否被 cap 截断」。否则会出现死循环：offset=220 被夹回 200，
    # 返回的仍是第 200 页，却又报 next_offset=220 / has_more=true —— 模型按提示词
    # 「翻到 has_more 为 false」，却永远翻不到，把剩余步数全烧在重复读同一页上。
    offset_capped = requested_offset > SEARCH_MAX_OFFSET
    offset = min(requested_offset, SEARCH_MAX_OFFSET)

    used_scope = requested_scope
    ctx.searched_scopes.append(used_scope)
    start, end = _scope_range(ctx, used_scope)
    if explicit_start is not None:
        # 已解析时间与页面范围取交集；未解析的口语时间允许模型给出的
        # 明确日期替换页面默认日期，人员、部门和数据权限仍由 ctx 锁定。
        if ctx.allow_explicit_range_override:
            start, end = explicit_start, explicit_end
            ctx.resolved_time_source = "model"
        else:
            start = max(start, explicit_start) if start else explicit_start
            end = min(end, explicit_end) if end else explicit_end
        if start is not None and end is not None:
            ctx.resolved_period = (start, end)
    # 交集为空必须用恒假条件短路：倒置区间直接喂给 overlap 条件时，
    # 跨度长的周报会两头都命中（start<=小日期 且 结束>=大日期），那是错数据。
    range_conflict = (
        explicit_start is not None and start is not None and start > end
    )
    conditions = _base_conditions(ctx, start, end)
    if range_conflict:
        conditions.append(false())
    stmt = (
        select(WeeklyReport, Employee)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
        .order_by(*_order_columns(keyword))
    )
    matching_rows, keyword_coverage = _keyword_matches(
        ctx, list(ctx.session.execute(stmt).all()), keyword
    )
    ctx.keyword_coverage = keyword_coverage
    tokens = _keyword_tokens(keyword)
    # 语义排序（浏览模式增强）：无关键词且 Embedding 服务可用时，把「与用户问题
    # 最相关」的周报排到前面，缓解关键词漏同义表达、英文周报漏检的固有损失。
    # 有关键词时绝不启用——关键词是严格证据筛选，语义排序等于放宽范围。
    semantic_info: dict[str, Any] | None = None
    if not tokens:
        semantic_info = _semantic_order(ctx, [report for report, _ in matching_rows])
        if semantic_info is not None:
            rank: dict[int, int] = semantic_info.pop("_rank")
            matching_rows.sort(key=lambda pair: rank.get(pair[0].id, len(rank) + 1))
    total = len(matching_rows)
    span_starts = [report.start_date for report, _ in matching_rows if report.start_date]
    span_ends = [report.end_date or report.start_date for report, _ in matching_rows if report.end_date or report.start_date]
    span_start = min(span_starts) if span_starts else start
    span_end = max(span_ends) if span_ends else end
    if range_conflict:
        # 交集为空时按「模型请求的区间」回报，而不是倒置后的空交集。
        span_start, span_end = explicit_start, explicit_end
    rows = matching_rows[offset : offset + limit]

    dept_names = department_name_map(ctx.session)
    # 预计算 AI 摘要：入库时每篇周报已算过一次高质量摘要，浏览模式的切片
    # 优先用它，信息密度远高于「前 180 字机械截取」。一次批量查询，无额外成本。
    analysis_results = latest_unified_results(ctx.session, [row[0].id for row in rows])
    reports: list[dict[str, Any]] = []
    for report, employee in rows:
        ctx.offered_report_ids.add(report.id)
        original = f"{report.title or ''}\n{report.content or ''}"
        original_matches = any(token.casefold() in original.casefold() for token in tokens)
        analysis = analysis_results.get(report.id) or {}
        analysis_summary = str((analysis.get("result") or {}).get("summary") or "").strip()
        if tokens and not original_matches:
            # 原文没命中（多为英文周报）：译文 → 命中关键词的 AI 摘要 → 原文前缀
            translated = ctx.keyword_translations.get(report.id)
            if translated:
                excerpt_text = _excerpt_around(translated, tokens)
                excerpt_origin = "translation"
            elif analysis_summary and any(
                token.casefold() in analysis_summary.casefold() for token in tokens
            ):
                excerpt_text = _excerpt_around(analysis_summary, tokens)
                excerpt_origin = "ai_summary"
            else:
                excerpt_text = _excerpt(report.content or report.title)
                excerpt_origin = "body"
        elif tokens:
            # 原文命中：在命中位置附近截取，保留关键词上下文
            excerpt_text = _excerpt_around(report.content or report.title, tokens)
            excerpt_origin = "body"
        elif analysis_summary:
            # 浏览模式：AI 摘要优先
            excerpt_text = _excerpt(analysis_summary)
            excerpt_origin = "ai_summary"
        else:
            excerpt_text = _excerpt(report.content or report.title)
            excerpt_origin = "body"
        reports.append(
            {
                "report_id": report.id,
                "employee": employee.name if employee else None,
                "department": dept_names.get(employee.department_id) if employee else None,
                "period_start": _iso(report.start_date),
                "period_end": _iso(report.end_date),
                "title": report.title,
                "excerpt": excerpt_text,
                "excerpt_origin": excerpt_origin,
                "excerpt_language": "zh_translation" if tokens and not original_matches else "original",
            }
        )

    consumed = offset + len(reports)
    # next_offset 绝不能超过 cap：超过就意味着「下一次调用会被夹回来、拿到同一页」。
    # 到顶时如实收尾（has_more=false）并说明原因，而不是伪造一个做不到的 next_offset。
    reachable = consumed <= SEARCH_MAX_OFFSET
    has_more = consumed < total and reachable
    truncated_reason: str | None = None
    if offset_capped:
        truncated_reason = "offset_cap"
    elif consumed < total and not reachable:
        truncated_reason = "offset_cap"
    ctx.pagination_pending = has_more
    if truncated_reason and "pagination_cap" not in ctx.coverage_gaps:
        ctx.coverage_gaps.append("pagination_cap")
    return json.dumps(
        {
            "scope": used_scope,
            "period": {"start": _iso(span_start), "end": _iso(span_end)},
            # 显式日期回显：观测面板与模型都能核对「实际按哪个区间检索」。
            "explicit_range": (
                {"start": _iso(explicit_start), "end": _iso(explicit_end)}
                if explicit_start is not None
                else None
            ),
            "keyword": keyword,
            "matched_total": total,
            "offset": offset,
            "returned": len(reports),
            # 这两个字段是「读完了没有」的唯一依据：模型据此决定翻页还是收尾，
            # 而不是凭感觉宣称「已经看全」。
            "has_more": has_more,
            "next_offset": consumed if has_more else None,
            # 命中集超过分页上限时如实标注：has_more=false 代表「到此为止」，
            # 不等于「数据集只有这些」，模型必须把这一点讲出来（禁止静默截断）。
            "truncated_reason": truncated_reason,
            "max_offset": SEARCH_MAX_OFFSET,
            "escalated_from": None,
            "keyword_coverage": keyword_coverage,
            # 语义排序元信息：null 表示未启用（未配置/无向量/失败降级），
            # 模型与前端据此知道「这页是按相关度还是按时间排的」。
            "semantic": (
                {
                    "model": semantic_info["model"],
                    "ranked": semantic_info["ranked"],
                    "covered": semantic_info["covered"],
                    "candidate_total": semantic_info["candidate_total"],
                    "candidate_truncated": semantic_info["candidate_truncated"],
                    "top_score": semantic_info["top_score"],
                }
                if semantic_info is not None
                else None
            ),
            "reports": reports,
        },
        ensure_ascii=False,
    )


def _get_reports(ctx: ToolContext, args: dict[str, Any]) -> str:
    raw_ids = args.get("report_ids")
    if not isinstance(raw_ids, list):
        return _error("report_ids 必须是数组")

    wanted: list[int] = []
    for item in raw_ids:
        try:
            wanted.append(int(item))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return _error("report_ids 里没有合法的整数 ID")

    # 只允许读 search_reports 真正返回过的 ID：模型编造的 ID 一律丢弃
    allowed = [report_id for report_id in wanted if report_id in ctx.offered_report_ids]
    rejected = [report_id for report_id in wanted if report_id not in ctx.offered_report_ids]
    allowed = allowed[:GET_REPORTS_MAX]

    payload: list[dict[str, Any]] = []
    truncated_ids: list[int] = []
    budget_exhausted = False
    dept_names = department_name_map(ctx.session)
    for report_id in allowed:
        report = ctx.session.get(WeeklyReport, report_id)
        if report is None or report_ai_exempt(ctx.session, report):
            continue
        employee = (
            ctx.session.get(Employee, report.employee_id)
            if report.employee_id
            else None
        )
        body = (report.content or report.title or "").strip()
        remaining = max(0, ctx.max_read_chars - ctx.read_chars)
        if remaining <= 0:
            # 预算用完就停：让模型知道「没读完」的原因是预算，而不是「没有这篇」。
            budget_exhausted = True
            break
        truncated = len(body) > remaining
        body = body[:remaining]
        ctx.read_chars += len(body)
        ctx.read_report_ids.append(report_id)
        if truncated:
            ctx.truncated_report_ids.append(report_id)
            truncated_ids.append(report_id)
        payload.append(
            {
                "report_id": report.id,
                "employee": employee.name if employee else None,
                "department": dept_names.get(employee.department_id) if employee else None,
                "period_start": _iso(report.start_date),
                "period_end": _iso(report.end_date),
                "title": report.title,
                "content": body,
                "content_truncated": truncated,
            }
        )

    if budget_exhausted or truncated_ids:
        if "character_budget" not in ctx.coverage_gaps:
            ctx.coverage_gaps.append("character_budget")

    return json.dumps(
        {
            "returned": len(payload),
            "rejected_ids": rejected,
            "truncated_ids": truncated_ids,
            "budget_remaining": max(0, ctx.max_read_chars - ctx.read_chars),
            "budget_exhausted": budget_exhausted,
            "reports": payload,
        },
        ensure_ascii=False,
    )


def _list_periods(ctx: ToolContext, args: dict[str, Any]) -> str:
    rows = ctx.session.execute(
        select(
            WeeklyReport.start_date,
            func.max(func.coalesce(WeeklyReport.end_date, WeeklyReport.start_date)),
            func.count(WeeklyReport.id),
        )
        .select_from(WeeklyReport)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*_report_context_conditions(ctx))
        .group_by(WeeklyReport.start_date)
        .order_by(WeeklyReport.start_date.desc())
        .limit(60)
    ).all()
    return json.dumps(
        {
            "periods": [
                {
                    "start": _iso(start),
                    "end": _iso(end),
                    "report_count": int(count),
                }
                for start, end, count in rows
            ]
        },
        ensure_ascii=False,
    )


def _list_employees(ctx: ToolContext, args: dict[str, Any]) -> str:
    stmt = select(Employee)
    keyword = str(args.get("keyword") or "").strip()
    department_id = args.get("department_id")
    conditions: list[Any] = []
    scope = current_scope(ctx.session)
    if scope.is_scoped:
        conditions.append(scope.employee_condition())
    if ctx.department_id is not None:
        conditions.append(Employee.department_id == ctx.department_id)
    if ctx.employee_id is not None:
        conditions.append(Employee.id == ctx.employee_id)
    if keyword:
        conditions.append(Employee.name.like(f"%{keyword}%"))
    if isinstance(department_id, int):
        conditions.append(Employee.department_id == department_id)
    stmt = stmt.where(*conditions)
    rows = ctx.session.scalars(stmt.order_by(Employee.name.asc()).limit(50)).all()
    dept_names = department_name_map(ctx.session)
    return json.dumps(
        {
            "employees": [
                {
                    "employee_id": employee.id,
                    "name": employee.name,
                    "department": dept_names.get(employee.department_id),
                }
                for employee in rows
            ]
        },
        ensure_ascii=False,
    )


def _submission_range(ctx: ToolContext, args: dict[str, Any]) -> tuple[date, date] | None:
    """解析 list_submissions 的周期；非法日期返回 None 由调用方报错。"""
    raw_start = str(args.get("start") or "").strip()
    raw_end = str(args.get("end") or "").strip()
    if not raw_start and not raw_end:
        return ctx.period_start, ctx.period_end
    try:
        start = date.fromisoformat(raw_start) if raw_start else ctx.period_start
        end = date.fromisoformat(raw_end) if raw_end else ctx.period_end
    except ValueError:
        return None
    if start > end:
        return None
    return start, end


def _list_submissions(ctx: ToolContext, args: dict[str, Any]) -> str:
    """谁交了、谁没交。口径与看板一致：应提交 = 在职且未免提交。

    这个工具存在的理由：没交周报的人**不会**出现在任何正文检索结果里，
    模型若只用 search_reports，永远无法回答「谁没交」，还会把「没被检索到」
    误当成「交了但内容少」。名单必须来自系统统计，不能靠推理。
    """
    period = _submission_range(ctx, args)
    if period is None:
        return _error("start/end 必须是 YYYY-MM-DD 且 start 不晚于 end")
    start, end = period

    conditions = list(reportable_employee_conditions(ctx.session))
    if ctx.department_id is not None:
        conditions.append(Employee.department_id == ctx.department_id)
    rows = ctx.session.execute(
        select(Employee.id, Employee.name, Employee.department_id)
        .where(*conditions)
        .order_by(Employee.name.asc())
    ).all()

    dept_names = department_name_map(ctx.session)
    submitted_ids = submitted_employee_ids(ctx.session, start, end)

    submitted: list[dict[str, Any]] = []
    unsubmitted: list[dict[str, Any]] = []
    for employee_id, name, department_id in rows:
        item = {"employee_id": employee_id, "name": name, "department": dept_names.get(department_id)}
        (submitted if employee_id in submitted_ids else unsubmitted).append(item)

    truncated = len(unsubmitted) > LIST_NAMES_MAX
    return json.dumps(
        {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "total": len(rows),
            "submitted_count": len(submitted),
            "unsubmitted_count": len(unsubmitted),
            "submitted": submitted[:LIST_NAMES_MAX],
            "unsubmitted": unsubmitted[:LIST_NAMES_MAX],
            "names_truncated": truncated,
        },
        ensure_ascii=False,
    )


def _quality_scope(ctx: ToolContext, requested: str) -> str:
    """评分工具的返回范围：只做校验，不自动放宽。

    评分是「按周期看人」的工具：当前周期没人被分析过时，悄悄放宽到近 4 周会把
    另一个周期的人混进来，排名就失去意义。范围一律如实回传，让模型自己说明。
    """
    if requested not in SCOPE_ORDER:
        return ctx.default_scope
    return requested


def _latest_analysis_subquery():
    """每篇周报最新一条分析（同一篇正文变过会有多条历史分析）。"""
    return (
        select(
            AIAnalysis.entity_id.label("report_id"),
            func.max(AIAnalysis.id).label("analysis_id"),
        )
        .where(
            AIAnalysis.entity_type == REPORT_ANALYSIS_ENTITY,
            AIAnalysis.analysis_type == REPORT_ANALYSIS_TYPE,
        )
        .group_by(AIAnalysis.entity_id)
        .subquery()
    )


def _list_report_quality(ctx: ToolContext, args: dict[str, Any]) -> str:
    """读取已经算好的 AI 评分与体检标记，用于质量类问题。

    权威结论在 ``ai_analyses``（report_analysis），这里在 Python 侧解析，
    不写 JSON 专有查询 —— 与项目「查询层不用 JSONB 操作符」的约定一致。

    **候选集必须以「有分析结论」为准，不能先按提交时间截断周报再去 join**：
    实测踩过这个坑 —— 先把最近 60 篇拿来 join，那些更早但已评分的周报全部落空，
    工具于是回答「一篇都没评分」，而库里其实有 20 篇。这正是它要消除的
    「静默地宣称没有数据」。
    """
    scope = _quality_scope(ctx, str(args.get("scope") or ctx.default_scope).strip().lower())
    sort = str(args.get("sort") or "score_asc").strip().lower()
    if sort not in ("score_asc", "score_desc"):
        sort = "score_asc"
    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else QUALITY_DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = QUALITY_DEFAULT_LIMIT
    limit = max(1, min(limit, QUALITY_MAX_REPORTS))
    report_id_raw = args.get("report_id")
    try:
        report_id = int(report_id_raw) if report_id_raw is not None else None
    except (TypeError, ValueError):
        return _error("report_id 必须是周报 ID")

    start, end = _scope_range(ctx, scope)
    conditions = _base_conditions(ctx, start, end)
    if report_id is not None:
        conditions.append(WeeklyReport.id == report_id)
    latest = _latest_analysis_subquery()

    total = int(ctx.session.scalar(
        select(func.count(WeeklyReport.id))
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
    ) or 0)
    scored_total = int(ctx.session.scalar(
        select(func.count(WeeklyReport.id))
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .join(latest, latest.c.report_id == WeeklyReport.id)
        .join(AIAnalysis, AIAnalysis.id == latest.c.analysis_id)
        .where(*conditions, AIAnalysis.status == "success", WeeklyReport.ai_checked_at.is_not(None))
    ) or 0)
    unscored_total = total - scored_total
    if unscored_total and report_id is None:
        return json.dumps({
            "scope": scope,
            "period": {"start": _iso(start), "end": _iso(end)},
            "total": total,
            "scored": scored_total,
            "unscored": unscored_total,
            "coverage_complete": False,
            "returned": 0,
            "items": [],
            "note": "本范围有周报尚未完成 AI 分析，不能给出评分、排名或重复度结论。请先补分析并等待完成。",
        }, ensure_ascii=False)

    if scored_total > QUALITY_SCAN_MAX and report_id is None:
        return json.dumps({
            "scope": scope,
            "period": {"start": _iso(start), "end": _iso(end)},
            "total": total,
            "scored": scored_total,
            "unscored": unscored_total,
            "coverage_complete": False,
            "scan_truncated": True,
            "returned": 0,
            "items": [],
            "note": "评分记录超过本次安全扫描上限，不能给出全范围排名。请缩小周期后重试。",
        }, ensure_ascii=False)

    scored_rows = ctx.session.execute(
        select(WeeklyReport, Employee, AIAnalysis.result_json)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .join(latest, latest.c.report_id == WeeklyReport.id)
        .join(AIAnalysis, AIAnalysis.id == latest.c.analysis_id)
        .where(*conditions, AIAnalysis.status == "success", WeeklyReport.ai_checked_at.is_not(None))
        .limit(QUALITY_SCAN_MAX + 1)
    ).all()
    if len(scored_rows) > QUALITY_SCAN_MAX and report_id is None:
        return json.dumps({
            "scope": scope,
            "period": {"start": _iso(start), "end": _iso(end)},
            "total": total,
            "scored": scored_total,
            "unscored": unscored_total,
            "coverage_complete": False,
            "scan_truncated": True,
            "returned": 0,
            "items": [],
            "note": "评分扫描结果被截断，不能据此排名。",
        }, ensure_ascii=False)

    dept_names = department_name_map(ctx.session)
    items: list[dict[str, Any]] = []
    for report, employee, result_json in scored_rows:
        result = result_json or {}
        score = result.get("score")
        # 这些 ID 确实交给了模型，必须登记为「可引用」，否则它引用一篇刚看到的
        # 评分结论时会被当成编造而丢弃（实测踩到）。登记后也能用 get_reports 读原文。
        ctx.offered_report_ids.add(report.id)
        items.append(
            {
                "report_id": report.id,
                "employee": employee.name if employee else None,
                "department": dept_names.get(employee.department_id) if employee else None,
                "period_start": _iso(report.start_date),
                "period_end": _iso(report.end_date),
                "analyzed": True,
                "score": score if isinstance(score, (int, float)) else None,
                "is_thin": bool(result.get("is_thin")),
                "thin_reason": str(result.get("thin_reason") or "")[:120],
                "is_duplicate": bool(result.get("is_duplicate")),
                "duplicate_score": result.get("duplicate_score"),
                "summary": _excerpt(str(result.get("summary") or ""))
                if result.get("summary")
                else None,
            }
        )

    # 缺分（理论上不会发生）排在最后，避免混进排名。
    items.sort(
        key=lambda item: (
            item["score"] is None,
            -(item["score"] or 0) if sort == "score_desc" else (item["score"] or 0),
            item["report_id"],
        )
    )
    selected = items[:limit]

    return json.dumps(
        {
            "scope": scope,
            "period": {"start": _iso(start), "end": _iso(end)},
            "total": total,
            "scored": scored_total,
            "unscored": unscored_total,
            "coverage_complete": True,
            "selection": "single_report" if report_id is not None else "ranking",
            "returned": len(selected),
            "sort": sort,
            # 有分数却超出扫描上限时如实说明，避免把「没扫到」说成「没有」。
            "scan_truncated": scored_total > len(items),
            "note": (
                "没有可用的评分结论，无法对质量排名"
                if scored_total == 0
                else None
            ),
            "items": selected,
        },
        ensure_ascii=False,
    )


def _int_arg(
    args: dict[str, Any], key: str, default: int, low: int, high: int
) -> int:
    value = args.get(key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


def _visible_task_ids(ctx: ToolContext):
    """至少有一条非免提交来源的任务 id。免提交来源不得泄漏到管理结论里。"""
    return (
        select(TaskReportLink.task_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(*_report_context_conditions(ctx))
    )


def _period_task_ids(ctx: ToolContext, start: date, end: date):
    return (
        select(TaskReportLink.task_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            *report_period_overlap_conditions(start, end),
            *_report_context_conditions(ctx),
        )
    )


def _list_blockers(ctx: ToolContext, args: dict[str, Any]) -> str:
    """卡点的权威统计：按部门计数、高风险、长期卡点。

    存在意义：需求 AC-16 明确要求「哪个部门卡点最多」必须**实际统计**，
    不能靠模型读文本感觉。这个工具把聚合放在 SQL 里做，模型只负责解读数字。
    计数口径写进返回值，避免跨部门任务被重复计数而无人察觉。
    """
    status = str(args.get("status") or "OPEN").strip().upper()
    if status not in {"OPEN", "RESOLVED", "ALL"}:
        status = "OPEN"
    risk = str(args.get("risk_level") or "").strip()
    min_weeks = _int_arg(args, "min_weeks", 0, 0, 200)
    limit = _int_arg(args, "limit", BLOCKERS_DEFAULT_LIMIT, 1, BLOCKERS_MAX_LIMIT)

    conditions: list[Any] = [Blocker.task_id.in_(_visible_task_ids(ctx))]
    if status != "ALL":
        conditions.append(Blocker.status == status)
    if risk in {"低", "中", "高"}:
        conditions.append(Blocker.risk_level == risk)
    if min_weeks > 0:
        conditions.append(Blocker.duration_weeks >= min_weeks)
    if ctx.department_id is not None:
        conditions.append(
            Blocker.task_id.in_(
                select(TaskReportLink.task_id)
                .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
                # 部门筛选本身要求员工归属明确，inner join（外连接 + 对连接表
                # where 等价于它，这里直接写语义）
                .join(Employee, Employee.id == WeeklyReport.employee_id)
                .where(Employee.department_id == ctx.department_id)
            )
        )
    period = _submission_range(ctx, args)
    blocker_scope = str(args.get("scope") or "period").strip().lower()
    explicit_dates = bool(args.get("start") or args.get("end"))
    if period is None:
        return _error("start/end 必须是 YYYY-MM-DD 且 start 不晚于 end")
    if explicit_dates or blocker_scope != "all":
        # 卡点统计默认使用本轮周期；仅明确要求「全部未解决存量」才不限周期。
        conditions.append(Blocker.task_id.in_(_period_task_ids(ctx, *period)))

    task_dept = _task_department_subquery(ctx)
    rows = ctx.session.execute(
        select(
            task_dept.c.department_id,
            func.count(Blocker.id),
            func.sum(case((Blocker.risk_level == "高", 1), else_=0)),
            func.sum(case((Blocker.duration_weeks >= LONG_RUNNING_WEEKS, 1), else_=0)),
        )
        .select_from(Blocker)
        .join(task_dept, task_dept.c.task_id == Blocker.task_id, isouter=True)
        .where(*conditions)
        .group_by(task_dept.c.department_id)
    ).all()

    dept_names = department_name_map(ctx.session)
    by_department = [
        {
            "department": dept_names.get(dept_id) or "（未归属部门）",
            "blocker_count": int(count or 0),
            "high_risk_count": int(high or 0),
            "long_running_count": int(long or 0),
        }
        for dept_id, count, high, long in rows
    ]
    by_department.sort(key=lambda item: (-item["blocker_count"], item["department"]))

    detail = ctx.session.execute(
        select(Blocker, Task, task_dept.c.department_id)
        .join(Task, Task.id == Blocker.task_id)
        .join(task_dept, task_dept.c.task_id == Blocker.task_id, isouter=True)
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
        .limit(limit)
    ).all()

    # 给每条卡点附上证据周报 ID：需求 3.2「AI 结论必须可追溯」要求统计结论
    # 也能点回原文，否则模型只能引用卡点 id（[#65]），而引用体系只认周报 ID，
    # 既拿不到可点来源，也存在「卡点 id 恰好等于某篇周报 id 时指向错周报」的风险。
    evidence: dict[int, list[int]] = {}
    if detail:
        evidence_query = (
            select(TaskReportLink.task_id, TaskReportLink.report_id)
            .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
            .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
            .where(TaskReportLink.task_id.in_([task.id for _, task, _ in detail]))
            .where(*_report_context_conditions(ctx))
        )
        if explicit_dates or blocker_scope != "all":
            evidence_query = evidence_query.where(
                *report_period_overlap_conditions(*period)
            )
        for task_id, report_id in ctx.session.execute(
            evidence_query.order_by(TaskReportLink.report_id.desc())
        ).all():
            ids = evidence.setdefault(task_id, [])
            if report_id not in ids:
                ids.append(report_id)
        # 这些 ID 是真实证据，登记为可引用，让模型能给出可点来源。
        for ids in evidence.values():
            ctx.offered_report_ids.update(ids[:5])

    items = [
        {
            "blocker_id": blocker.id,
            "task_id": task.id,
            "title": blocker.title,
            "description": _excerpt(blocker.description),
            "risk_level": blocker.risk_level,
            "status": blocker.status,
            "duration_weeks": blocker.duration_weeks,
            "occurrence_count": blocker.occurrence_count,
            "department": dept_names.get(dept_id) or "（未归属部门）",
            "first_seen_date": _iso(blocker.first_seen_date),
            "last_seen_date": _iso(blocker.last_seen_date),
            # 可引用的周报 ID（供 [#ID] 追溯用），最多 5 篇。
            "source_report_ids": evidence.get(task.id, [])[:5],
        }
        for blocker, task, dept_id in detail
    ]

    return json.dumps(
        {
            "status_filter": status,
            "scope": "period" if explicit_dates or blocker_scope != "all" else "all",
            "period": {"start": _iso(period[0]), "end": _iso(period[1])}
            if explicit_dates or blocker_scope != "all"
            else None,
            "total": sum(item["blocker_count"] for item in by_department),
            "by_department": by_department,
            "top_department": by_department[0]["department"] if by_department else None,
            "long_running_weeks_threshold": LONG_RUNNING_WEEKS,
            "attribution": "每个卡点按最近一次关联周报的提交人部门归属，同一卡点不重复计数",
            "returned": len(items),
            "items": items,
        },
        ensure_ascii=False,
    )


def _list_tasks(ctx: ToolContext, args: dict[str, Any]) -> str:
    """跨周任务统计：找出连续多周没有推进的任务。

    对应需求 5.4「跨周任务关联」与「哪些事情连续三周没有推进」。
    duration_weeks 是任务首次出现到最近出现跨越的周数，由周报证据回算，
    不是模型推测。
    """
    min_weeks = _int_arg(args, "min_weeks", LONG_RUNNING_WEEKS, 1, 200)
    limit = _int_arg(args, "limit", TASKS_DEFAULT_LIMIT, 1, TASKS_MAX_LIMIT)
    keyword = str(args.get("keyword") or "").strip()
    include_completed = bool(args.get("include_completed"))

    # 「没推进」的权威口径与 insight_service.stalled_plan 一致：
    # 必须连续在 ≥N 篇周报里以「下周计划」出现且仍未完成。
    # 只看 duration_weeks 会把「资源需求：No additional resources are required」
    # 这类任务也列进来 —— 实测踩到过，那不是没推进的工作。
    plan_evidence = (
        select(
            TaskReportLink.task_id.label("task_id"),
            func.count(distinct(WeeklyReport.id)).label("plan_reports"),
        )
        .select_from(TaskReportLink)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(ReportItem.item_type == "next_week_plans", *_report_context_conditions(ctx))
        .group_by(TaskReportLink.task_id)
        .subquery()
    )

    # 资源需求类任务：按 ReportItem.task_key 判定 —— 它保留了 `resource_demand:`
    # 前缀。**不能**改判 Task.canonical_key：经过 _canonical_key 后冒号与下划线
    # 都被剥掉（`resource_demand:xxx` → `resourcedemandxxx`），
    # `LIKE 'resource_demand:%'` 永远匹配不到任何行，排除条件会恒真、等于没写。
    resource_demand_task_ids = (
        select(TaskReportLink.task_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(
            ReportItem.task_key.like(f"{RESOURCE_DEMAND_TASK_PREFIX}%"),
            *_report_context_conditions(ctx),
        )
    )

    conditions: list[Any] = [
        Task.duration_weeks >= min_weeks,
        Task.id.in_(_visible_task_ids(ctx)),
        # 资源需求不是「未推进的工作」，它是「不需要资源」的陈述，必须排除。
        ~Task.id.in_(resource_demand_task_ids),
        Task.id.in_(select(plan_evidence.c.task_id)),
        plan_evidence.c.plan_reports >= 3,
    ]
    if not include_completed:
        # 默认排除已完成：问「没推进」时把已完成任务列出来会误导。
        conditions.append(Task.status != "COMPLETED")
    if keyword:
        conditions.append(Task.title.like(f"%{keyword}%"))
    if ctx.department_id is not None:
        conditions.append(
            Task.id.in_(
                select(TaskReportLink.task_id)
                .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
                .join(Employee, Employee.id == WeeklyReport.employee_id)
                .where(Employee.department_id == ctx.department_id)
            )
        )

    task_dept = _task_department_subquery(ctx)
    rows = ctx.session.execute(
        select(Task, task_dept.c.department_id, plan_evidence.c.plan_reports)
        .join(plan_evidence, plan_evidence.c.task_id == Task.id)
        .join(task_dept, task_dept.c.task_id == Task.id, isouter=True)
        .where(*conditions)
        .order_by(Task.duration_weeks.desc(), Task.last_seen_date.desc(), Task.id.desc())
        .limit(limit)
    ).all()

    dept_names = department_name_map(ctx.session)
    # 与 list_blockers 同理：附上证据周报 ID，统计结论也能点回原文（需求 3.2）。
    task_evidence: dict[int, list[int]] = {}
    if rows:
        for task_id, report_id in ctx.session.execute(
            select(TaskReportLink.task_id, TaskReportLink.report_id)
            .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
            .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
            .where(TaskReportLink.task_id.in_([task.id for task, _, _ in rows]))
            .where(*_report_context_conditions(ctx))
            .order_by(TaskReportLink.report_id.desc())
        ).all():
            ids = task_evidence.setdefault(task_id, [])
            if report_id not in ids:
                ids.append(report_id)
        for ids in task_evidence.values():
            ctx.offered_report_ids.update(ids[:5])

    items = [
        {
            "task_id": task.id,
            "title": task.title,
            "related_project": task.related_project,
            "status": task.status,
            "duration_weeks": task.duration_weeks,
            "department": dept_names.get(dept_id) or "（未归属部门）",
            "first_seen_date": _iso(task.first_seen_date),
            "last_seen_date": _iso(task.last_seen_date),
            "plan_report_count": int(plan_reports or 0),
            "source_report_ids": task_evidence.get(task.id, [])[:5],
        }
        for task, dept_id, plan_reports in rows
    ]

    return json.dumps(
        {
            "min_weeks": min_weeks,
            "include_completed": include_completed,
            "definition": (
                f"任务连续 ≥{min_weeks} 周出现，且至少 3 篇周报把它写进「下周计划」"
                "但仍未标记完成；已排除资源需求类条目"
            ),
            "returned": len(items),
            "items": items,
        },
        ensure_ascii=False,
    )


_HANDLERS: dict[str, Callable[[ToolContext, dict[str, Any]], str]] = {
    "search_reports": _search_reports,
    "get_reports": _get_reports,
    "list_submissions": _list_submissions,
    "list_report_quality": _list_report_quality,
    "list_blockers": _list_blockers,
    "list_tasks": _list_tasks,
    "list_periods": _list_periods,
    "list_employees": _list_employees,
}


def execute_tool(ctx: ToolContext, name: str, arguments_json: str) -> str:
    """执行一次工具调用，始终返回 JSON 字符串。

    任何失败都被转成结构化错误结果而不是往上抛：单次工具失败不该让整轮问答
    500，模型看到错误通常能自己换个方式重试。
    """
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as exc:
        return _error(f"参数不是合法 JSON：{exc}")
    if not isinstance(args, dict):
        return _error("参数必须是 JSON 对象")

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"未知工具：{name}")
    try:
        return handler(ctx, args)
    except Exception:  # 工具边界：失败必须变成可读结果，而不是 500
        logger.exception("Agent 工具 %s 执行失败", name)
        return _error("工具执行失败，请缩小范围后重试。")
