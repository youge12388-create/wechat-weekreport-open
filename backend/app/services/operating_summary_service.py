"""经营看板的结构化 AI 总结（独立队列，不与管理简报共享缓存）。

与 ``period_summary_service`` 的三点关键差异，每一点都是为了不互相污染：

1. **队列/缓存键包含 ``summary_type + start + end + department_id``**。
   管理简报按 ``(start, end)`` 唯一，经营看板同一周期还有「上月 / 本月 / 部门」多个
   作用域，共用一张表必然互相覆盖。
2. **输出是版本化 JSON Schema**，不是 Markdown。前端按字段稳定渲染，
   不做标题拼接（方案 5.3 明确要求）。
3. **输入是结构化事实 + 原文依据**（任务、事项、卡点、原文片段），不是把
   全部周报原文再喂一遍。理由是这些事实已经由单篇分析产出并落库，
   重复送原文既贵又会让模型有机会重新发明已经被结构化过的结论。

过期判定沿用全站纪律：``payload_hash`` 覆盖输入事实，事实变化即标记过期，
绝不把旧结论当作当前结论展示。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai import get_provider
from app.ai.json_util import extract_json_object
from app.ai.prompts import (
    OPERATING_DEPARTMENT_SYSTEM,
    OPERATING_MONTHLY_SYSTEM,
    OPERATING_MONTHLY_DEPARTMENTS_PROMPT_VERSION,
    OPERATING_MONTHLY_PROMPT_VERSION,
    OPERATING_SUMMARY_PROMPT_VERSION,
    OPERATING_WEEKLY_SYSTEM,
)
from app.core.time_utils import utcnow
from app.models.analysis_queue import AnalysisQueue
from app.models.ai_analysis import AIAnalysis
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus, AnalysisQueueStatus, OperatingSummaryType
from app.models.operating_summary_queue import COMPANY_SCOPE_ID, OperatingSummaryQueue
from app.models.weekly_report import WeeklyReport
from app.services import operating_dashboard_service as dashboard
from app.services.report_analysis_service import (
    ENTITY_TYPE as REPORT_ANALYSIS_ENTITY_TYPE,
    REPORT_ANALYSIS_TYPE,
    current_analysis_report_ids,
    report_payload_hash,
)
from app.services.runtime_config_service import runtime_settings
from app.services.view_service import (
    non_exempt_report_condition,
    report_period_overlap_conditions,
)

ENTITY_TYPE = "operating_dashboard"
ANALYSIS_TYPE = "operating_summary"
PROMPT_VERSION = OPERATING_SUMMARY_PROMPT_VERSION


def _prompt_version(summary_type: str) -> str:
    if summary_type == OperatingSummaryType.MONTHLY_DEPARTMENTS:
        return OPERATING_MONTHLY_DEPARTMENTS_PROMPT_VERSION
    return OPERATING_MONTHLY_PROMPT_VERSION if summary_type == OperatingSummaryType.MONTHLY_COMPANY else PROMPT_VERSION

SYSTEM_BY_TYPE = {
    OperatingSummaryType.WEEKLY_COMPANY: OPERATING_WEEKLY_SYSTEM,
    OperatingSummaryType.MONTHLY_COMPANY: OPERATING_MONTHLY_SYSTEM,
    OperatingSummaryType.DEPARTMENT: OPERATING_DEPARTMENT_SYSTEM,
}

# 每个作用域允许的来源周报 ID 上限。模型只能引用这里出现过的 ID，
# 超出部分直接丢弃而不是当作「可疑引用」放行。
MAX_SOURCE_REPORT_IDS = 400

# 逐篇摘要进材料时的**单篇**截断长度。摘要已经过模型压缩（实测 150~250 字），
# 超长的按这个长度截断；被截断的篇数在材料里如实计入。
DIGEST_MAX_CHARS = 200

# 每月简报材料的**总字符预算**（所有分段合起来）。
#
# 为什么必须有一个总预算：各分段的条数上限是分开定的（每块 `MONTHLY_LIST_CAP` 条），
# 但块数多、条目文本长，合起来会随数据量增长 —— 而撞上模型上下文上限的代价很重：
# 请求失败后队列会按重试次数反复重发同一份超长 payload，每次都计费。
# 实测「近半年」（449 篇周报）材料 8.9 万字符 / 4.87 万 prompt token，已接近
# 64K 上下文的可接受区间；再翻一倍就会硬失败。
#
# 逐篇摘要是**从总预算里扣**的（见 `_digest_budget`），不是各定各的上限 ——
# 各定各的会让「其他块涨了、摘要没减」这种组合悄悄超限。
MATERIAL_TOTAL_CHARS = 90000

# 逐篇摘要至少要留出这么多。低于它摘要就失去意义（它是「卡在哪、谁在等谁」
# 这类细节的唯一来源），宁可让总材料略微超预算也要保住这个下限。
DIGEST_MIN_CHARS = 6000


def summary_type_for_view(view: str, *, has_department: bool) -> str:
    """视图 + 是否限定部门 → 总结作用域类型。"""
    if has_department:
        return OperatingSummaryType.DEPARTMENT
    if view == "monthly":
        return OperatingSummaryType.MONTHLY_COMPANY
    return OperatingSummaryType.WEEKLY_COMPANY


def _scope_department_id(department_id: int | None) -> int:
    return COMPANY_SCOPE_ID if department_id is None else int(department_id)


def _report_ids_for_period(
    session: Session, start: date, end: date, *, department_id: int | None
) -> list[int]:
    conditions = [
        *report_period_overlap_conditions(start, end),
        non_exempt_report_condition(session),
    ]
    query = (
        select(WeeklyReport.id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(*conditions)
    )
    if department_id is not None:
        query = query.where(Employee.department_id == department_id)
    return sorted(int(row) for row in session.scalars(query).all())


def monthly_analysis_readiness(session: Session, report_ids: list[int]) -> dict[str, Any]:
    """Only current, successful single-report analyses can support a monthly conclusion."""
    if not report_ids:
        return {
            "analysis_ready": False,
            "analysis_total": 0,
            "analysis_completed": 0,
            "analysis_failed": 0,
            "analysis_processing": 0,
            "analysis_waiting": 0,
        }
    reports = session.scalars(
        select(WeeklyReport).where(WeeklyReport.id.in_(report_ids))
    ).all()
    current_reports = {
        int(report.id): report_payload_hash(report)
        for report in reports
    }
    completed_ids = current_analysis_report_ids(session, reports)
    completed = len(completed_ids)
    queues = {
        int(report_id): (str(payload_hash or ""), str(status or ""))
        for report_id, payload_hash, status in session.execute(
            select(AnalysisQueue.report_id, AnalysisQueue.payload_hash, AnalysisQueue.status).where(
                AnalysisQueue.report_id.in_(report_ids)
            )
        )
    }
    incomplete_ids = set(current_reports) - completed_ids
    failed = sum(
        queues.get(report_id) == (current_reports[report_id], AnalysisQueueStatus.FAILED)
        for report_id in incomplete_ids
    )
    processing = sum(
        queues.get(report_id, (None, None))[0] == current_reports[report_id]
        and queues.get(report_id, (None, None))[1]
        in (AnalysisQueueStatus.PENDING, AnalysisQueueStatus.RUNNING)
        for report_id in incomplete_ids
    )
    return {
        "analysis_ready": completed == len(report_ids),
        "analysis_total": len(report_ids),
        "analysis_completed": completed,
        "analysis_failed": failed,
        "analysis_processing": processing,
        "analysis_waiting": len(report_ids) - completed - failed - processing,
    }


def _department_facts_payload(
    session: Session, start: date, end: date, *, department_id: int
) -> dict[str, Any]:
    """部门作用域的输入事实。

    从 ``weekly_view`` 按部门过滤取主题，再补上该部门的卡点与协作依赖 ——
    **不再经由 ``operating_dashboard_service.department_view``**：那个页面视图已裁掉，
    但部门 AI 结论是独立能力（提示词与输出 Schema 都不同），必须自己取数。

    卡点按「该部门成员本周报里的依赖类事项」聚合，与总览的瓶颈口径同源；
    这样「部门卡点」和公司瓶颈说的是同一批事实，不会两个页面两种说法。
    """
    weekly = dashboard.weekly_view(session, start, end, department_id=department_id)
    department = session.get(Department, department_id)
    dependencies = dashboard._bottlenecks(session, start, end, department_id=department_id)
    return {
        "themes": weekly.get("themes") or [],
        "outcomes": [
            outcome
            for theme in (weekly.get("themes") or [])
            for outcome in (theme.get("outcomes") or [])
        ],
        "blockers": dependencies.get("items") or [],
        "department": department.name if department is not None else None,
    }


def _digest_budget(base_material: dict[str, Any]) -> int:
    """逐篇摘要能用多少字符 —— 从总预算里扣掉其他分段之后剩下的。

    **不能给摘要单定一个固定上限**：那样「其他分段随数据量涨了、摘要没减」这种
    组合会悄悄把总材料顶过上下文上限。这里按实际的其他分段体积来扣，
    并保一个下限（低于它摘要就失去意义）。
    """
    base_size = len(json.dumps(base_material, ensure_ascii=False, default=str))
    return max(MATERIAL_TOTAL_CHARS - base_size, DIGEST_MIN_CHARS)


def _period_report_digests(
    session: Session,
    start: date,
    end: date,
    *,
    department_id: int | None,
    budget: int | None = None,
) -> list[dict[str, Any]]:
    """所选周期内每篇周报的一句话摘要（来自单篇结构化分析）。

    为什么每月简报必须有它：五块结论把同一批事项按「在推什么 / 变了什么 / 卡在哪」
    重排了三遍，但**单篇周报里那些只出现一次的具体事**（谁在等哪所学校的答复、
    哪笔款没到）在任何一块里都不成形 —— 模型只能看到聚合后的名词，写不出细节，
    于是结论就只能停在「院校沟通与反馈」这种抽象标题上。这是「看完没得到信息」的
    直接成因之一。摘要已经由单篇分析产出并落库，这里只是把它们读出来，不额外调模型。
    """
    query = (
        select(Employee.name, Department.name, WeeklyReport.id, AIAnalysis.result_json)
        .join(WeeklyReport, WeeklyReport.id == AIAnalysis.entity_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .join(Department, Department.id == Employee.department_id, isouter=True)
        .where(
            AIAnalysis.entity_type == REPORT_ANALYSIS_ENTITY_TYPE,
            AIAnalysis.analysis_type == REPORT_ANALYSIS_TYPE,
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
            WeeklyReport.id.in_(
                select(WeeklyReport.id).where(*report_period_overlap_conditions(start, end))
            ),
        )
        .order_by(WeeklyReport.id)
    )
    if department_id is not None:
        query = query.where(Employee.department_id == department_id)
    limit = DIGEST_MIN_CHARS if budget is None else max(int(budget), 0)
    digests: list[dict[str, Any]] = []
    used = 0
    truncated = 0
    for employee_name, department_name, report_id, result in session.execute(query).all():
        inner = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else result
        summary = " ".join(str((inner or {}).get("summary") or "").split())
        if not summary:
            continue
        summary = summary[:DIGEST_MAX_CHARS]
        # 篇数可能远超预算（跨月或全历史）。**超预算的条目如实计数**，不静默丢弃 ——
        # 模型据此才知道自己没读全，提示词里也要求它说明覆盖。
        if used + len(summary) > limit:
            truncated += 1
            continue
        used += len(summary)
        digests.append(
            {
                "report_id": int(report_id),
                "employee": employee_name,
                "department": department_name,
                "summary": summary,
            }
        )
    if truncated:
        digests.append({"未纳入的周报摘要": truncated, "原因": "篇幅预算已用尽"})
    return digests


def _monthly_material(
    session: Session, start: date, end: date, *, department_id: int | None,
    legacy_digest_budget: bool = False,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """每月简报的材料：确定性事实 + 逐篇摘要。

    事实部分刻意**换成中文键、去掉应用层字段**（`source_report_ids` 之类只留必要的），
    因为原样把 payload 丢给模型时，它会照着字段名复述（材料写 `mainlines.items`，
    结论就写「主线」），这正是「不说人话」的来源之一。
    """

    def ids(values: Any, limit: int = 8) -> list[int]:
        return [int(value) for value in (values or [])[:limit]]

    payload = payload if payload is not None else dashboard.monthly_view(session, start, end, department_id=department_id)

    months = [
        {
            "月份": item["label"],
            "周报篇数": item["report_total"],
            "其中已分析": item["analyzed_reports"],
            "分析覆盖率": f"{round(float(item['coverage']) * 100)}%",
        }
        for item in payload.get("months") or []
    ]
    # **每条带一个 ref**：模型要为每个 ref 写一句人话（输出里的 `items`），合并时按 ref
    # 贴回对应条目。用 ref 而不是标题匹配，是因为模型会改写标题措辞 —— 按标题匹配
    # 一旦对不上，那条就悄悄退回计数句，而页面上看不出来是漏了（第 28h 条踩过同类问题）。
    #
    # **数字一律收进「依据数字」子对象**：实测把 `未解决事项` / `涉及部门` 这类字段放在
    # 平级时，模型会照着念成「本月完成 12 项，还有 2 项没解决」「卡住了 44 项工作，
    # 涉及 7 个部门」—— 那正是要消灭的计数语言。收进一个明确命名为「依据数字」的对象后，
    # 它们是「给页面核对用的证据」而不是「要复述的内容」。
    mainlines = [
        {
            "ref": f"ML{index}",
            "在做的事": item["name"],
            "对象": item["name"],
            "最新进展": (item.get("latest") or {}).get("title"),
            "状态": (item.get("latest") or {}).get("status"),
            # **刻意不给完成/未解决条数**：页面在依据行里已经显示它们，给了模型就会
            # 念成「完成 12 项，还有 2 项没解决」（实测 14/84 条这么写）。要模型写的是
            # 「这件事现在到什么阶段了」，那需要的是最新进展原文，不是计数。
            "依据数字": {"涉及部门": item["departments"]},
            "来源周报": ids(item["source_report_ids"]),
        }
        for index, item in enumerate((payload.get("mainlines") or {}).get("items") or [], start=1)
    ]
    changes = [
        {
            "ref": f"CH{index}",
            "变化类型": item["kind_label"],
            "对象": item["title"],
            # **不给 `detail`**：那是代码拼的计数句（「本月完成 8 项、待解决 0 项；
            # 另有 1 项在推进。」），给了模型它只会删几个字再抄回来 —— 实测改完仍
            # 有 8 条以「本月完成 N 项」开头。要它写的是「这个月实际动了什么」，
            # 那需要从「逐篇周报摘要」里读，不是从计数句里改写。
            "依据数字": {"涉及部门": item["departments"]},
            "来源周报": ids(item["source_report_ids"]),
        }
        for index, item in enumerate((payload.get("changes") or {}).get("items") or [], start=1)
    ]
    stuck = [
        {
            "ref": f"ST{index}",
            "卡住的对象": item["title"],
            "对象": item["title"],
            # 同上：`说明` 里的「N 项工作依赖它」是页面兜底用的计数句，不进材料原文，
            # 只给模型「这件事压着多少条线」这个量级提示，措辞由它自己写。
            "压住的工作项数": int(item.get("workstream_count") or 0) or None,
            "高风险": item.get("risk_level") == "高",
            "依据数字": {
                "持续周数": item.get("duration_weeks"),
                "涉及部门": item["departments"],
                "证据篇数": item["evidence_count"],
            },
            "来源周报": ids(item["source_report_ids"]),
        }
        for index, item in enumerate((payload.get("stuck") or {}).get("items") or [], start=1)
    ]
    anomalies = [
        {
            "ref": f"AN{index}",
            "类型": item["kind_label"],
            "标题": item["title"],
            "对象": item["title"],
            "说明": item["detail"],
            # 具体事例是模型写出细节的唯一依据：只有聚合数字时，它只能写抽象名词。
            "具体事例": [
                {
                    "标题": sample.get("title"),
                    "部门": sample.get("department_name"),
                    "提交人": sample.get("employee_name"),
                    "周报": sample.get("report_id"),
                }
                for sample in (item.get("samples") or [])[:4]
            ],
            "依据数字": {
                "涉及部门": item["departments"],
                "证据篇数": item.get("evidence_count"),
            },
            "来源周报": ids(item["source_report_ids"]),
        }
        for index, item in enumerate((payload.get("anomalies") or {}).get("items") or [], start=1)
    ]
    shared_issues = [
        {
            "ref": f"SI{index}",
            "问题": item["title"],
            "对象": item["title"],
            "别称": item.get("aliases") or [],
            "出现在哪几类清单": item.get("kinds") or [],
            "依据数字": {
                "涉及部门": item["departments"],
                "提及人数": item.get("people_count"),
                "涉及周报数": item.get("evidence_count"),
                "持续周数": item.get("duration_weeks"),
            },
        }
        for index, item in enumerate(payload.get("shared_issues") or [], start=1)
    ]
    # 部门卡只能引用本部门成员的周报。跨部门主线的来源 ID 会混在一起，
    # 不能直接拿 brief.mainlines[*].source_report_ids 当部门证据。
    department_report_ids: dict[str, list[int]] = {}
    for department_name, report_id in session.execute(
        select(Department.name, WeeklyReport.id)
        .join(Employee, Employee.id == WeeklyReport.employee_id)
        .join(Department, Department.id == Employee.department_id)
        .where(
            *report_period_overlap_conditions(start, end),
            non_exempt_report_condition(session),
        )
        .order_by(Department.name, WeeklyReport.id)
    ):
        department_report_ids.setdefault(department_name, []).append(int(report_id))
    department_report_sets = {name: set(values) for name, values in department_report_ids.items()}

    def department_sources(entry: dict[str, Any], allowed: set[int]) -> list[int]:
        return [report_id for report_id in ids(entry.get("source_report_ids")) if report_id in allowed]

    departments = [
        {
            "ref": f"DP{index}",
            "部门": brief["name"],
            "对象": brief["name"],
            "本部门来源周报": department_report_ids.get(brief["name"], []),
            "在推的事": [
                {
                    "对象": entry["title"],
                    "最新进展": (entry.get("latest") or {}).get("title"),
                    "最新阶段": (entry.get("latest") or {}).get("status"),
                    "最新进展所属部门": (entry.get("latest") or {}).get("department_name"),
                    "来源周报": department_sources(
                        entry, department_report_sets.get(brief["name"], set())
                    ),
                }
                for entry in brief["mainlines"]
            ],
            "在等别人": [
                {
                    "对象": entry["title"],
                    "具体情况": entry.get("detail"),
                    "来源周报": department_sources(
                        entry, department_report_sets.get(brief["name"], set())
                    ),
                }
                for entry in brief["waiting"]
            ],
            "自己卡住": [
                {
                    "对象": entry["title"],
                    "具体情况": entry.get("detail"),
                    "来源周报": department_sources(
                        entry, department_report_sets.get(brief["name"], set())
                    ),
                }
                for entry in brief["blockers"]
            ],
            "本月变化": [
                {
                    "对象": entry["title"],
                    "变化类型": entry["kind_label"],
                    "来源周报": department_sources(
                        entry, department_report_sets.get(brief["name"], set())
                    ),
                }
                for entry in brief["changes"][:2]
            ],
            "管理层待处理": [
                {
                    "对象": entry["title"],
                    "影响": entry["impact"],
                    "来源周报": department_sources(
                        entry, department_report_sets.get(brief["name"], set())
                    ),
                }
                for entry in brief["interventions"][:2]
            ],
            "依据数字": {
                "未关闭卡点数": brief["open_blockers"],
                "还有几条主线未列出": max(int(brief["mainline_total"]) - len(brief["mainlines"]), 0),
            },
        }
        for index, brief in enumerate(payload.get("department_briefs") or [], start=1)
    ]
    interventions = [
        {
            "ref": f"IV{index}",
            "标题": item["title"],
            "对象": item["title"],
            "原因": item["reason"],
            "影响": item["impact"],
            "依据数字": {"责任方": item["owner"], "持续": item["duration"]},
            "来源周报": ids(item["source_report_ids"], 12),
        }
        for index, item in enumerate((payload.get("interventions") or {}).get("items") or [], start=1)
    ]
    coverage = payload.get("coverage") or {}
    material = {
        "统计周期": f"{start.isoformat()} ~ {end.isoformat()}",
        "覆盖情况": {
            "范围内周报": coverage.get("reports_total"),
            "已分析": coverage.get("analyzed_reports"),
            "分析覆盖率": f"{coverage.get('rate')}%",
            "是否有周报未分析": bool(coverage.get("partial")),
            "未分析篇数": coverage.get("missing_analysis"),
        },
        "逐月覆盖": months,
        "公司级跨部门工作": mainlines,
        "本月变化": changes,
        "卡住的事": stuck,
        "异常": anomalies,
        "公司级共性问题": shared_issues,
        "各部门情况": departments,
        "需要管理层介入的候选": interventions,
    }
    # 逐篇摘要按**扣掉以上分段的剩余预算**来取（见 ``_digest_budget``）。
    # 放在最后是为了让预算按真实体积算：先读摘要再组装其他块，就无从知道剩多少。
    # Department-card material no longer consumes the company digest budget.
    # The old budget is retained only to verify a v14 cache against its inputs.
    budget_material = material if legacy_digest_budget else {
        key: value for key, value in material.items() if key != "各部门情况"
    }
    material["逐篇周报摘要"] = _period_report_digests(
        session,
        start,
        end,
        department_id=department_id,
        budget=_digest_budget(budget_material),
    )
    return material

# 每月简报材料里带 `ref` 的分段。**新增带 ref 的分段必须登记在这里** ——
# `ref_identities` 靠它收集「编号 → 业务对象名」，合并时按名字校验归属
# （第 28i 条：简报过期时页面用的是重算过的清单，只按位置贴会把 A 的话挂到 B 上）。
# 「重点项目」属于周度总结的材料（``_weekly_projects_material``），登记在同一处：
# `ref_identities` 对所有作用域统一收集，月度材料里没有这个块时自然跳过。
MONTHLY_REF_BLOCKS: tuple[str, ...] = (
    "公司级跨部门工作",
    "本月变化",
    "卡住的事",
    "异常",
    "公司级共性问题",
    "各部门情况",
    "需要管理层介入的候选",
    # 以下五块属于周度总结的材料（``_weekly_*_material``），登记在同一处：
    # `ref_identities` 对所有作用域统一收集，月度材料里没有这些块时自然跳过。
    "重点项目",
    "关键瓶颈",
    "组织摩擦",
    "长期趋势",
    "关注信号",
)

# 关注信号材料只取总览前几名：首屏与弹窗能展示的就是这些，
# 全量 100 条进材料只会推高 token，还稀释模型对头部信号的注意力。
_ATTENTION_MATERIAL_LIMIT = 8


def ref_identities(facts: dict[str, Any]) -> dict[str, str]:
    """材料里每个 `ref` 对应的业务对象名。

    与结论一起落库，展示时用它校验「这句话还属不属于这一条」。
    带「键」的分段（关注信号）用稳定键做身份：展示标题里嵌着数字
    （「影响 15 项工作」），新周报一入库数字就变，按标题贴回会让
    AI 结论全部脱落回规则串。
    """
    identities: dict[str, str] = {}
    for block in MONTHLY_REF_BLOCKS:
        for item in facts.get(block) or []:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref") or "").strip().upper()
            identity = str(item.get("键") or item.get("对象") or "").strip()
            if ref and identity:
                identities[ref] = identity
    return identities


def _weekly_projects_material(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    """周度总结的「重点项目」材料：ref 编号 + 中文事实。

    与 ``_monthly_material`` 同一纪律：数字收进「依据数字」（给页面核对，
    不是给模型复述的），每条带 ref，模型为每个 ref 写一句人话，页面按名字
    贴回项目卡片（``dashboard.merge_project_conclusions``）。
    事项明细（``最近在做什么``）是模型写出细节的唯一依据 —— 只有聚合数字时，
    它只能写抽象名词。
    """
    progress = dashboard._project_progress(session, start, end)
    material: list[dict[str, Any]] = []
    for index, item in enumerate(progress.get("items") or [], start=1):
        material.append(
            {
                "ref": f"PR{index}",
                "项目": item["name"],
                "对象": item["name"],
                # 分类标签：提示词明确禁止把它原样抄进正文。
                "本期状态": item["status_label"],
                "最近在做什么": [
                    {
                        "事项": entry.get("title"),
                        "状态": entry.get("status"),
                        "部门": entry.get("department_name"),
                        "提交人": entry.get("employee_name"),
                        "周报": entry.get("report_id"),
                    }
                    for entry in (item.get("evidence") or [])
                ],
                "依据数字": {
                    "覆盖篇数": item.get("report_count"),
                    "参与人数": item.get("employee_count"),
                    "涉及部门": item.get("departments") or [],
                    "已持续周数": item.get("duration_weeks") or None,
                },
                "来源周报": [int(rid) for rid in (item.get("source_report_ids") or [])[:12]],
            }
        )
    return material


def _weekly_bottlenecks_material(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    """周度总结的「关键瓶颈」材料：总览「影响面最大的瓶颈」逐条带 ref。

    与其他周度材料同一纪律：数字收进「依据数字」，来源事项给模型写
    「谁被挡住、在等它什么」的细节。瓶颈名（target）是稳定的业务对象名、
    不含随数据变化的数字，页面按它贴回（``merge_ai_item_notes`` 的 BN*）。
    """
    bottlenecks = dashboard._bottlenecks(session, start, end)
    material: list[dict[str, Any]] = []
    for index, item in enumerate(bottlenecks.get("items") or [], start=1):
        material.append(
            {
                "ref": f"BN{index}",
                "对象": item["target"],
                "类型": "运行瓶颈",
                "涉及部门": item.get("departments") or [],
                "依据数字": {
                    "影响工作数": item.get("workstream_count"),
                    "涉及部门数": item.get("department_count"),
                    "参与人数": item.get("employee_count"),
                    "证据篇数": item.get("evidence_count"),
                },
                "来源事项": [
                    {
                        "事项": source.get("title") or source.get("description") or "",
                        "部门": source.get("department_name"),
                        "周报": source.get("report_id"),
                    }
                    for source in (item.get("sources") or [])[:4]
                    if isinstance(source, dict)
                ],
                "来源周报": [
                    int(rid) for rid in (item.get("source_report_ids") or [])[:12]
                ],
            }
        )
    return material


def _weekly_frictions_material(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    """周度总结的「组织摩擦」材料：疑似重复推进 + 状态描述不一致。

    重复推进带 ``判定依据``（``same_item_evidence``：两侧原文 + 部门 + 周报），
    那是模型写出「具体撞在哪件事上」的唯一依据 —— 只给部门名，它只能写抽象句。
    这两类都是**待核对**的关系，不是定论，提示词要求保留「疑似」「建议核对」字样。
    """
    frictions = dashboard._frictions(session, start, end)
    material: list[dict[str, Any]] = []
    for index, item in enumerate(frictions.get("duplicates") or [], start=1):
        material.append(
            {
                "ref": f"FR{index}",
                "对象": item["project"],
                "类型": "疑似重复推进",
                "涉及部门": item.get("departments") or [],
                "判定依据": [
                    {
                        "部门": entry.get("department_name"),
                        "事项": entry.get("title"),
                        "周报": entry.get("report_id"),
                    }
                    for entry in (item.get("same_item_evidence") or [])[:6]
                ],
                "依据数字": {
                    "涉及部门数": item.get("department_count"),
                    "参与人数": item.get("employee_count"),
                    "证据篇数": item.get("evidence_count"),
                },
                "来源周报": [int(rid) for rid in (item.get("source_report_ids") or [])[:12]],
            }
        )
    for index, item in enumerate(frictions.get("conflicts") or [], start=1):
        material.append(
            {
                "ref": f"FC{index}",
                "对象": item["title"],
                "类型": "状态描述不一致",
                "记录为已完成": item.get("finished_by") or [],
                "仍记录未解决": item.get("open_by") or [],
                "涉及部门": item.get("departments") or [],
                "来源周报": [int(rid) for rid in (item.get("source_report_ids") or [])[:12]],
            }
        )
    return material


def _weekly_trends_material(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    """周度总结的「长期趋势」材料。

    周期口径与总览页一致（``TREND_LOOKBACK_WEEKS`` 个周期），只收**已通过
    覆盖率门槛**的 series —— ``insufficient`` 里的主题证据不足，模型不得谈它的方向。
    各周期篇数收进「依据数字」，模型写模式含义，不复述条数。
    """
    trends = dashboard._issue_trends(
        session, dashboard._period_sequence(end, dashboard.TREND_LOOKBACK_WEEKS)
    )
    material: list[dict[str, Any]] = []
    for index, item in enumerate(trends.get("series") or [], start=1):
        material.append(
            {
                "ref": f"TR{index}",
                "对象": item["title"],
                "问题主题": item["title"],
                "方向": item.get("direction"),
                "依据数字": {
                    "各周期篇数": item.get("counts") or [],
                    "周期": item.get("periods") or [],
                    "合计篇数": item.get("total_evidence"),
                },
                "来源周报": [int(rid) for rid in (item.get("source_report_ids") or [])[:20]],
            }
        )
    return material


def _weekly_attention_material(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    """周度总结的「关注信号」材料：总览「今天值得关注」按分数取前几名。

    与其他周度材料同一纪律：**规则版结论不进材料**（模型会照抄计数句），
    数字收进「依据数字」，来源事项给模型写「具体在等什么、该先核对谁」的细节。
    模型为每条信号写「为什么值得管理层看一眼、建议先核对什么」，
    页面按稳定键贴回信号列表与弹窗（``dashboard.merge_ai_item_notes`` 的 AT*）。

    「对象」是稳定对象名（不含随数据变化的数字），「键」是
    ``signal_type:对象名``，两者与 ref 一起落库：贴回按「键」校验归属 ——
    展示标题「影响 15 项工作」在下一次同步后可能变成 16 项，键不变。
    """
    signals = dashboard._attention_signals(
        session, start, end, limit=_ATTENTION_MATERIAL_LIMIT
    )
    material: list[dict[str, Any]] = []
    for index, item in enumerate(signals.get("items") or [], start=1):
        signal_key = str(item.get("signal_key") or "")
        material.append(
            {
                "ref": f"AT{index}",
                "对象": signal_key.partition(":")[2] or item.get("title") or "",
                "键": signal_key,
                "信号类型": item.get("label"),
                "依据数字": {
                    "影响工作数": item.get("impact_workstreams"),
                    "涉及部门数": item.get("impact_departments"),
                    "持续周期数": item.get("duration_periods"),
                    "依据篇数": item.get("evidence_count"),
                },
                "来源事项": [
                    {
                        "事项": source.get("title") or source.get("description") or "",
                        "部门": source.get("department_name"),
                        "周报": source.get("report_id"),
                    }
                    for source in (item.get("sources") or [])[:4]
                    if isinstance(source, dict)
                ],
                "来源周报": [
                    int(rid) for rid in (item.get("source_report_ids") or [])[:12]
                ],
            }
        )
    return material


def _facts_payload(
    session: Session,
    view: str,
    start: date,
    end: date,
    *,
    department_id: int | None,
) -> dict[str, Any]:
    """本次总结的输入事实（同时用于算 payload_hash 和拼提示词）。

    周度视图除工作主线外，还带上**已经算好的确定性统计**（瓶颈 / 摩擦 / 趋势）。
    理由：这些正是「哪里值得关注」的证据，模型拿不到它们就只能凭标题重猜一遍，
    而提示词明确禁止它自己重新数。带上之后，结论里的每个数字都能对上一个可核对的条目。
    加上它们也让 ``payload_hash`` 覆盖这些事实 —— 瓶颈变了结论就该重算。
    """
    if view == "monthly":
        # 每月视图交给模型的**不是界面 payload**，而是压成中文说明的材料：
        # 界面 payload 里的字段名（items / source_report_ids / rate）会让模型照着复述，
        # 写出来就是「在推 N 条主线」这种统计口径。材料的构成见 ``_monthly_material``，
        # 它同时进 ``payload_hash`` —— 材料变了简报就该重算。
        material = _monthly_material(session, start, end, department_id=department_id)
        return material if material is not None else {}
    if view == "department" and department_id is not None:
        payload = _department_facts_payload(
            session, start, end, department_id=department_id
        )
    else:
        weekly = dashboard.weekly_view(session, start, end, department_id=department_id)
        payload = {
            **weekly,
            "bottlenecks": dashboard._bottlenecks(session, start, end)["items"],
            "frictions": dashboard._frictions(session, start, end),
            "trends": dashboard._issue_trends(session, dashboard._period_sequence(end, 4)),
            "workstreams": dashboard._workstream_objects(session, start, end),
            # 重点项目 / 组织摩擦 / 长期趋势材料（带 ref）：模型为每条写一句人话，
            # 页面按名字贴回对应卡片。项目负责人 2026-09-24 要求：卡片正文由
            # 大模型分析，不是规则模板。
            "重点项目": _weekly_projects_material(session, start, end)
            if department_id is None
            else [],
            "关键瓶颈": _weekly_bottlenecks_material(session, start, end)
            if department_id is None
            else [],
            "组织摩擦": _weekly_frictions_material(session, start, end)
            if department_id is None
            else [],
            "长期趋势": _weekly_trends_material(session, start, end)
            if department_id is None
            else [],
            "关注信号": _weekly_attention_material(session, start, end)
            if department_id is None
            else [],
        }
    return payload


def _payload_hash(facts: dict[str, Any], summary_type: str, start: date, end: date, department_id: int) -> str:
    if summary_type == OperatingSummaryType.MONTHLY_COMPANY:
        facts = {key: value for key, value in facts.items() if key != "各部门情况"}
    serialized = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "prompt_version": _prompt_version(summary_type),
                "summary_type": summary_type,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "department_id": department_id,
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(serialized.encode("utf-8"))
    return digest.hexdigest()


def _legacy_monthly_hash(facts: dict[str, Any], start: date, end: date) -> str:
    """Verify an unchanged v14 full result without granting it a fabricated new hash."""
    digest = hashlib.sha256()
    digest.update(json.dumps({
        "prompt_version": "v14",
        "summary_type": OperatingSummaryType.MONTHLY_COMPANY,
        "start": start.isoformat(), "end": end.isoformat(),
        "department_id": COMPANY_SCOPE_ID,
    }, sort_keys=True).encode("utf-8"))
    digest.update(json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()


def _user_message(
    summary_type: str,
    start: date,
    end: date,
    facts: dict[str, Any],
    *,
    department_name: str | None = None,
    truncated: bool = False,
) -> str:
    scope_line = {
        OperatingSummaryType.WEEKLY_COMPANY: "统计范围：全公司",
        OperatingSummaryType.MONTHLY_COMPANY: "统计范围：全公司，跨自然月",
        OperatingSummaryType.DEPARTMENT: f"统计范围：部门「{department_name or '未指定'}」",
    }[summary_type]
    coverage = "（以下材料已包含该范围内全部可用事实）" if not truncated else (
        "（以下材料因篇幅只包含优先级最高的一部分，请在结论中如实说明覆盖不完整）"
    )
    # 材料说明必须与真实 payload 一致：说错了会让模型去找不存在的字段
    # （``themes`` / ``outcomes`` / ``mainlines``），然后在结论里写「材料未提供」。
    if summary_type == OperatingSummaryType.MONTHLY_COMPANY:
        facts = {key: value for key, value in facts.items() if key != "各部门情况"}
        materials = (
            "以下是全公司周报自动分析出的确定性事实，按「覆盖情况 / 逐月覆盖 / 公司级跨部门工作 / "
            "本月变化 / 卡住的事 / 异常 / 公司级共性问题 / 需要管理层介入的候选 / "
            "逐篇周报摘要」分段给出。条目带 ref 编号，只挑有管理价值的重点写入输出。"
            "数字可直接引用，不要重新统计；每条事实都带来源周报编号，可作为引用依据，不需要再检索。"
            "**逐篇周报摘要**是各部门成员自己写的事，写「是什么事、卡在哪」时优先从这里取细节："
        )
    else:
        materials = (
            "以下是该范围内已经结构化的工作主线、成果、卡点、重点项目与原文依据，"
            "不需要再检索："
        )
    return (
        f"统计周期：{start.isoformat()} ~ {end.isoformat()}\n"
        f"{scope_line}{coverage}\n"
        f"{materials}\n{json.dumps(facts, ensure_ascii=False, default=str)}"
    )


def _validate_sources(result: dict[str, Any], allowed: set[int]) -> dict[str, Any]:
    """丢弃不在本次输入范围内的来源 ID。

    模型可能凭记忆写出看似合理的周报 ID。放着不管会让前端渲染出指向
    **无关周报**的引用（方案要求「所有重要结论均能打开至少一篇真实来源」，
    指向错的那篇比没有引用更糟），所以这里直接过滤。
    """
    def _clean(ids: Any) -> list[int]:
        if not isinstance(ids, list):
            return []
        cleaned: list[int] = []
        for value in ids:
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                continue
            if candidate in allowed and candidate not in cleaned:
                cleaned.append(candidate)
        return cleaned

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: _clean(value) if key == "source_report_ids" else _walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(result)


def _collect_source_ids(result: dict[str, Any]) -> list[int]:
    found: set[int] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "source_report_ids" and isinstance(value, list):
                    for item in value:
                        try:
                            found.add(int(item))
                        except (TypeError, ValueError):
                            continue
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(result)
    return sorted(found)


def _queue_row(
    session: Session, summary_type: str, start: date, end: date, department_id: int
) -> OperatingSummaryQueue | None:
    return session.scalar(
        select(OperatingSummaryQueue).where(
            OperatingSummaryQueue.summary_type == summary_type,
            OperatingSummaryQueue.start_date == start,
            OperatingSummaryQueue.end_date == end,
            OperatingSummaryQueue.department_id == department_id,
        )
    )


def _latest_summary(session: Session, queue_id: int) -> AIAnalysis | None:
    return session.scalars(
        select(AIAnalysis)
        .where(
            AIAnalysis.entity_type == ENTITY_TYPE,
            AIAnalysis.entity_id == queue_id,
            AIAnalysis.analysis_type == ANALYSIS_TYPE,
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
        )
        .order_by(AIAnalysis.id.desc())
        .limit(1)
    ).first()


def queue_snapshot(
    session: Session,
    view: str,
    start: date,
    end: date,
    *,
    department_id: int | None = None,
) -> dict[str, Any]:
    """轻量轮询版的状态：只读队列行，不重算事实指纹。

    ``summary_state`` 为了判 stale 必须先重算整份看板事实（秒级）再 sha256；
    前端轮询每 4 秒打一次，等于每个开着的面板在持续制造全量重算 —— 而
    轮询真正关心的只有 status 翻转（pending/running → 其它）。翻转之后的
    stale 判定与结果合并由那次触发的全量 ``summary_state`` 完成，这里把
    ``stale`` 恒报 False、不带 result：宁可字段少，不为它花两秒。
    """
    summary_type = summary_type_for_view(view, has_department=department_id is not None)
    scope_id = _scope_department_id(department_id)
    report_ids = _report_ids_for_period(session, start, end, department_id=department_id)

    base: dict[str, Any] = {
        "summary_type": summary_type,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "department_id": None if scope_id == COMPANY_SCOPE_ID else scope_id,
        "reports_total": len(report_ids),
        "status": "not_generated",
        "stale": False,
        "result": None,
        "created_at": None,
        "model": None,
        "last_error": None,
        "source_report_ids": [],
        "generated_report_count": None,
        "queue_updated_at": None,
    }
    if summary_type == OperatingSummaryType.MONTHLY_COMPANY:
        base.update(monthly_analysis_readiness(session, report_ids))
    if not report_ids:
        return {**base, "status": "empty"}

    queue = _queue_row(session, summary_type, start, end, scope_id)
    if queue is None:
        return base
    base.update(
        {
            "queue_id": queue.id,
            # 直接回队列行的状态。SUCCESS 但结论缺失（failed 语义）、stale 等
            # 精细判定交给翻转后触发的那次 summary_state —— 轮询不需要它们。
            "status": queue.status,
            "last_error": queue.last_error,
            "queue_updated_at": queue.updated_at.isoformat() if queue.updated_at else None,
        }
    )
    return base


def summary_state(
    session: Session,
    view: str,
    start: date,
    end: date,
    *,
    department_id: int | None = None,
) -> dict[str, Any]:
    """经营看板总结的只读状态。

    只读是硬要求：面板每几秒轮询一次，若这里顺带入队，等于把正在跑的任务
    一直打回待处理（管理简报踩过这个坑，见 AGENTS 第 18 条）。
    """
    summary_type = summary_type_for_view(view, has_department=department_id is not None)
    scope_id = _scope_department_id(department_id)
    facts = _facts_payload(session, view, start, end, department_id=department_id)
    current_hash = _payload_hash(facts, summary_type, start, end, scope_id)
    report_ids = _report_ids_for_period(session, start, end, department_id=department_id)

    base: dict[str, Any] = {
        "summary_type": summary_type,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "department_id": None if scope_id == COMPANY_SCOPE_ID else scope_id,
        "reports_total": len(report_ids),
        "status": "not_generated",
        "stale": False,
        "result": None,
        "created_at": None,
        "model": None,
        "last_error": None,
        "source_report_ids": [],
        "generated_report_count": None,
        "queue_updated_at": None,
    }
    if summary_type == OperatingSummaryType.MONTHLY_COMPANY:
        base.update(monthly_analysis_readiness(session, report_ids))
    if not report_ids:
        return {**base, "status": "empty"}

    queue = _queue_row(session, summary_type, start, end, scope_id)
    if queue is None:
        return base
    analysis = _latest_summary(session, queue.id)
    compatible_v14 = False
    if summary_type == OperatingSummaryType.MONTHLY_COMPANY and analysis is not None and analysis.prompt_version == "v14":
        legacy_facts = _monthly_material(
            session, start, end, department_id=department_id, legacy_digest_budget=True
        ) or {}
        compatible_v14 = queue.payload_hash == _legacy_monthly_hash(legacy_facts, start, end)
    prompt_stale = (
        summary_type == OperatingSummaryType.MONTHLY_COMPANY
        and analysis is not None
        and analysis.prompt_version != _prompt_version(summary_type)
    )
    stale = ((queue.payload_hash != current_hash or prompt_stale) and not compatible_v14) or (
        summary_type == OperatingSummaryType.MONTHLY_COMPANY and base.get("analysis_ready") is False
    )
    # 状态优先级：stale 且队列仍在跑（PENDING/RUNNING）时如实报队列状态 ——
    # 前端靠 pending/running 决定要不要轮询与显示「生成中」；此时 stale 仍为
    # True、旧结论照常返回，页面能同时表达「正在重算」与「当前显示的是
    # 上一版事实」。队列不在跑时才用 "stale" 作为展示状态。
    if stale and queue.status in (AnalysisQueueStatus.PENDING, AnalysisQueueStatus.RUNNING):
        display_status = queue.status
    elif stale:
        display_status = "stale"
    else:
        display_status = queue.status
    base.update(
        {
            "queue_id": queue.id,
            "status": display_status,
            "stale": stale,
            "last_error": queue.last_error if not stale else None,
            "queue_updated_at": queue.updated_at.isoformat() if queue.updated_at else None,
        }
    )
    # **过期时也把上一次已完成的结论保留在 API 响应里**以兼容现有读取方；经营看板
    # 前端必须依据 stale 状态隐藏旧结论，并给出重新生成入口。
    #
    # 原先过期就返回 `result: None`，页面于是整块退回代码拼的计数串 —— 项目负责人
    # 看到的现象是「你刚刚改好了一些，现在又改回去了」（实测就是我把提示词升版、
    # 旧结论立刻被判过期造成的）。**过期不等于结论作废**：它只是基于上一版事实，
    # 过期内容仅保留在 API 中供兼容/核对，页面不能继续把它作为本期结论展示；同时
    # 状态如实标记 `stale=true` 并给出结论自身的 `prompt_version`。
    #
    # 注意：**只有「未过期且应有结论却没有」才算 failed**。过期时找不到旧结论是正常的
    # （还没生成过），不能报成失败 —— 否则一次事实变化会把页面从「过期」直接推成红色错误。
    if analysis is not None:
        data = analysis.result_json or {}
        base.update(
            {
                "result": data.get("result"),
                "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
                "model": analysis.model_name,
                "source_report_ids": data.get("source_report_ids") or [],
                # 结论自身的生成版本：页面据此说明「这版结论是按哪版提示词写的」。
                "prompt_version": (analysis.prompt_version or ""),
                # 生成时每条事实的身份（编号 → 业务对象名）。合并时按它校验归属，
                # **不能按位置贴** —— 过期时清单可能已重排（第 28i 条）。
                "ref_identities": data.get("ref_identities") or {},
                "department_report_ids": data.get("department_report_ids") or {},
                # 生成时覆盖的篇数。与当前的 `reports_total` 一比就知道
                # 「这份结论之后又入库了多少篇」—— 过期提示靠它说清结论有多旧。
                "generated_report_count": queue.report_count,
            }
        )
    elif not stale and queue.status == AnalysisQueueStatus.SUCCESS:
        base["status"] = "failed"
        base["last_error"] = "未找到已完成的经营总结结果。"
    return base


def enqueue_summary(
    session: Session,
    view: str,
    start: date,
    end: date,
    *,
    department_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """把经营看板总结排进后台队列；已完成的相同范围不会重复调用模型。"""
    summary_type = summary_type_for_view(view, has_department=department_id is not None)
    scope_id = _scope_department_id(department_id)
    facts = _facts_payload(session, view, start, end, department_id=department_id)
    report_ids = _report_ids_for_period(session, start, end, department_id=department_id)
    if not report_ids:
        return summary_state(session, view, start, end, department_id=department_id)
    if summary_type == OperatingSummaryType.MONTHLY_COMPANY and not monthly_analysis_readiness(
        session, report_ids
    )["analysis_ready"]:
        return summary_state(session, view, start, end, department_id=department_id)

    payload_hash = _payload_hash(facts, summary_type, start, end, scope_id)
    queue = _queue_row(session, summary_type, start, end, scope_id)
    if queue is None:
        queue = OperatingSummaryQueue(
            summary_type=summary_type,
            start_date=start,
            end_date=end,
            department_id=scope_id,
            payload_hash=payload_hash,
            report_count=len(report_ids),
            status=AnalysisQueueStatus.PENDING,
        )
        session.add(queue)
    elif (
        force
        or queue.payload_hash != payload_hash
        or queue.status in (AnalysisQueueStatus.FAILED, AnalysisQueueStatus.PENDING)
    ):
        # 重排时**不更新 payload_hash 与 report_count**：summary_state 的 stale
        # 判定与「结论基于 N 篇」展示都取自这两个字段。若在这里就写成当前值，
        # 重算进行期间页面会拿到 stale=False + 旧结论 —— 正是文件头纪律禁止的
        # 「把旧结论当当前结论展示」。process_summary_item 完成时会按最新事实
        # 重算 hash 并回填篇数；hash 仍有差异会先放回待处理，代价只是一轮空转。
        queue.status = AnalysisQueueStatus.PENDING
        queue.attempts = 0
        queue.next_attempt_at = None
        queue.last_error = None
    try:
        session.commit()
    except IntegrityError:
        # 并发双击 / 两个管理员同时点「生成」：唯一约束兜住了重复行，
        # 第二个请求在这里降级为「更新既有行」而不是 500。
        session.rollback()
        queue = _queue_row(session, summary_type, start, end, scope_id)
        if queue is None:
            raise
        queue.status = AnalysisQueueStatus.PENDING
        queue.attempts = 0
        queue.next_attempt_at = None
        queue.last_error = None
        session.commit()
    return summary_state(session, view, start, end, department_id=department_id)


def _period_view_for_type(summary_type: str) -> str:
    if summary_type == OperatingSummaryType.MONTHLY_COMPANY:
        return "monthly"
    if summary_type == OperatingSummaryType.DEPARTMENT:
        return "department"
    return "weekly"


def _write_summary(
    session: Session,
    queue: OperatingSummaryQueue,
    result: dict[str, Any],
    status: str,
    model_name: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    duration_ms: int | None,
    error: str | None = None,
) -> None:
    settings = runtime_settings(session)
    session.add(
        AIAnalysis(
            entity_type=ENTITY_TYPE,
            entity_id=queue.id,
            analysis_type=ANALYSIS_TYPE,
            model_provider=settings.ai_provider,
            model_name=model_name,
            prompt_version=_prompt_version(queue.summary_type),
            result_json=result if error is None else {**result, "error": error},
            input_data={
                "summary_type": queue.summary_type,
                "period": {
                    "start": queue.start_date.isoformat(),
                    "end": queue.end_date.isoformat(),
                },
                "department_id": queue.department_id,
                "payload_hash": queue.payload_hash,
                "report_count": queue.report_count,
            },
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status=status,
            error_message=error,
            created_at=utcnow(),
        )
    )


def process_summary_item(queue_id: int) -> dict[str, Any]:
    """消费一条已领取的经营看板总结任务；Worker 线程各自持有独立 Session。"""
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        queue = session.get(OperatingSummaryQueue, queue_id)
        if queue is None or queue.status != AnalysisQueueStatus.RUNNING:
            return {"queue_id": queue_id, "status": "skipped"}
        if queue.summary_type == OperatingSummaryType.MONTHLY_DEPARTMENTS:
            session.close()
            from app.services.monthly_department_cards_service import process_cards_item
            return process_cards_item(queue_id)

        view = _period_view_for_type(queue.summary_type)
        department_id = None if queue.department_id == COMPANY_SCOPE_ID else queue.department_id
        facts = _facts_payload(
            session, view, queue.start_date, queue.end_date, department_id=department_id
        )
        report_ids = _report_ids_for_period(
            session, queue.start_date, queue.end_date, department_id=department_id
        )
        current_hash = _payload_hash(
            facts, queue.summary_type, queue.start_date, queue.end_date, queue.department_id
        )
        # 入队到消费之间事实有变化：放回待处理，下轮按最新事实生成，
        # 不用旧事实产出一份立刻过期的结论。
        if current_hash != queue.payload_hash:
            queue.payload_hash = current_hash
            queue.report_count = len(report_ids)
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = None
            session.commit()
            return {"queue_id": queue_id, "status": "requeued"}
        if not report_ids:
            queue.status = AnalysisQueueStatus.SUCCESS
            queue.last_error = None
            session.commit()
            return {"queue_id": queue_id, "status": "empty"}
        if queue.summary_type == OperatingSummaryType.MONTHLY_COMPANY and not monthly_analysis_readiness(
            session, report_ids
        )["analysis_ready"]:
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = utcnow() + timedelta(minutes=2)
            session.commit()
            return {"queue_id": queue_id, "status": "waiting_analysis"}

        department_name = None
        if department_id is not None:
            department = session.get(Department, department_id)
            department_name = department.name if department is not None else None

        settings = runtime_settings(session)
        provider = get_provider(settings)
        allowed_sources = set(report_ids[:MAX_SOURCE_REPORT_IDS])
        user_message = _user_message(
            queue.summary_type,
            queue.start_date,
            queue.end_date,
            facts,
            department_name=department_name,
        )
        started = time.monotonic()
        response = provider.chat(SYSTEM_BY_TYPE[queue.summary_type], user_message)
        duration_ms = int(round((time.monotonic() - started) * 1000))

        raw_result = extract_json_object(response.content)
        result = _validate_sources(raw_result, allowed_sources)
        if queue.summary_type == OperatingSummaryType.MONTHLY_COMPANY:
            # Monthly results normalize omitted optional
            # fields so a valid no-discovery result remains distinguishable from an
            # older cached schema that must be regenerated.
            result.setdefault("headline", "")
            result.setdefault("change_summary", "")
            result.setdefault("mainline_summary", "")
            result.setdefault("mainline_refs", [])
            result.setdefault("coverage_note", "")
            result.setdefault("highlights", [])
            result.setdefault("items", [])
            result.setdefault("management_attention", [])
        source_ids = _collect_source_ids(result)
        payload = {
            "result": result,
            "summary_type": queue.summary_type,
            "source_report_ids": source_ids,
            "report_count": len(report_ids),
            "prompt_version": _prompt_version(queue.summary_type),
            # **生成时每条事实的身份**（编号 → 业务对象名），与结论一起落库。
            # 展示时按它校验归属：简报过期后页面用的是重算过的清单，排序可能已变化，
            # 只按位置贴会把 A 的话挂到 B 上（第 28i 条）。
            "ref_identities": ref_identities(facts),
            "department_report_ids": {
                item["部门"]: item.get("本部门来源周报") or []
                for item in facts.get("各部门情况") or []
            } if queue.summary_type == OperatingSummaryType.MONTHLY_COMPANY else {},
        }
        _write_summary(
            session,
            queue,
            payload,
            AIAnalysisStatus.SUCCESS,
            response.model or settings.ai_model,
            int(response.prompt_tokens or 0) or None,
            int(response.completion_tokens or 0) or None,
            duration_ms,
        )
        queue.status = AnalysisQueueStatus.SUCCESS
        queue.last_error = None
        queue.report_count = len(report_ids)
        session.commit()
        return {"queue_id": queue_id, "status": "success", "source_count": len(source_ids)}
    except Exception as exc:  # noqa: BLE001 队列任务必须把任意失败落库并可重试
        session.rollback()
        queue = session.get(OperatingSummaryQueue, queue_id)
        if queue is None:
            return {"queue_id": queue_id, "status": "failed", "error": str(exc)[:500]}
        queue.attempts = (queue.attempts or 0) + 1
        settings = runtime_settings(session)
        if queue.attempts >= settings.ai_analysis_max_attempts:
            queue.status = AnalysisQueueStatus.FAILED
        else:
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = utcnow() + timedelta(
                seconds=settings.ai_analysis_retry_base_seconds
                * (2 ** max(queue.attempts - 1, 0))
            )
        queue.last_error = str(exc)[:2000]
        _write_summary(
            session, queue, {}, AIAnalysisStatus.FAILED, None, None, None, None, str(exc)
        )
        session.commit()
        return {"queue_id": queue_id, "status": "failed", "error": str(exc)[:500]}
    finally:
        session.close()


__all__ = [
    "enqueue_summary",
    "process_summary_item",
    "queue_snapshot",
    "summary_state",
    "summary_type_for_view",
    "ENTITY_TYPE",
    "ANALYSIS_TYPE",
]
