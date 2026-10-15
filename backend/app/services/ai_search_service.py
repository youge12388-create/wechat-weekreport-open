"""AI 周报检索问答（管理层用自然语言查周报）。

流程：**先检索、后作答** —— 不在库里做向量检索（SQLite 无 pgvector），
而是「按周期/部门/员工/关键词捞候选原文 -> 交模型归纳 -> 带引用来源返回」。
周报量级在几百篇时，这条路比硬上 embedding 便宜且更可控。

检索范围（scope）有两种，语义必须分清：

- ``period``（默认）：只看指定周期内重叠的周报，与看板口径一致。
- ``all``：不限周期，检索全部历史周报。范围越大越容易被篇数/字符预算挡住，
  所以命中总数（matched_total）与实际阅读篇数（read_count）必须如实回报，
  绝不允许把「只读了最近 30 篇」伪装成「全局都看过了」。
  另外 ``all`` 的候选集**按周期轮转**（每周期取最近一篇），否则字符预算会全部
  落在最近一周 —— 名字叫「全部周期」，实际只看了一周。

关键词是严格证据筛选。没有命中时返回明确的无结果，不会改喂最近周报；
否则界面筛选范围与模型实际看到的材料会不一致。

两条安全约束：
1. 模型**只能引用本次喂给它的候选周报 ID**，防止它编造一个不存在的 ID。
2. 每次问答都写 AIAnalysis 留痕（问题/答案/来源/模型/版本），可追溯。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai import get_provider
from app.ai.base import LLMError
from app.ai.json_util import extract_json_object
from app.ai.prompts import (
    SEARCH_PROMPT_VERSION,
    SEARCH_SYSTEM,
    SEARCH_USER_TEMPLATE,
)
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.ai_analysis import AIAnalysis
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus
from app.models.weekly_report import WeeklyReport
from app.services.chat_history_access import capture_access_scope, history_row_visible
from app.services.runtime_config_service import runtime_settings
from app.services.view_service import (
    ai_report_condition,
    department_name_map,
    report_period_overlap_conditions,
    resolve_period,
)

logger = get_logger(__name__)

# AIAnalysis 的 entity_id 是非空整数，一次「检索问答」不对应单个实体，用 0 占位
SEARCH_ENTITY_TYPE = "search_query"
SEARCH_ENTITY_ID = 0
SEARCH_ANALYSIS_TYPE = "search"

# 检索范围取值。写死成元组是为了让 API 层能复用同一份校验口径。
SEARCH_SCOPE_PERIOD = "period"
SEARCH_SCOPE_ALL = "all"
SEARCH_SCOPES = (SEARCH_SCOPE_PERIOD, SEARCH_SCOPE_ALL)

# 全局检索的单篇配额下限。没有下限时，周期数一多每篇会被压成几十个字符，
# 等于既没广度也没内容。
MIN_REPORT_CHARS = 400


def normalize_scope(scope: str | None) -> str:
    """校验检索范围。非法值直接报错，不静默降级成某个默认范围。"""
    value = (scope or SEARCH_SCOPE_PERIOD).strip().lower()
    if value not in SEARCH_SCOPES:
        raise ValueError("scope 只能是 period 或 all")
    return value


def _keyword_condition(keyword: str):
    like = f"%{keyword}%"
    return or_(WeeklyReport.title.like(like), WeeklyReport.content.like(like))


def _base_conditions(
    start: date | None,
    end: date | None,
    scope: str,
    department_id: int | None,
    employee_id: int | None,
) -> list[Any]:
    """构造候选集的基础过滤条件。

    ``scope=all`` 时刻意不加周期条件 —— 这就是「全局检索」与「周期检索」的
    唯一区别；其余筛选（部门、员工）在两种范围下都生效。
    """
    conditions: list[Any] = (
        [] if scope == SEARCH_SCOPE_ALL else list(report_period_overlap_conditions(start, end))
    )
    if department_id is not None:
        conditions.append(Employee.department_id == department_id)
    if employee_id is not None:
        conditions.append(WeeklyReport.employee_id == employee_id)
    return conditions


def _match_summary(
    session: Session, conditions: list[Any], keyword: str | None
) -> tuple[int, date | None, date | None]:
    """命中总数与命中数据的实际跨度。

    这条聚合查询的存在意义是消灭「静默截断」：调用方必须能说清
    「总共命中多少篇、这次真正读了多少篇」，而不是只给一个没有分母的数字。
    """
    stmt = (
        select(
            func.count(WeeklyReport.id),
            func.min(WeeklyReport.start_date),
            func.max(func.coalesce(WeeklyReport.end_date, WeeklyReport.start_date)),
        )
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
    )
    if keyword:
        stmt = stmt.where(_keyword_condition(keyword))
    row = session.execute(stmt).one()
    return int(row[0] or 0), row[1], row[2]


def _candidate_reports(
    session: Session,
    conditions: list[Any],
    keyword: str | None,
    limit: int,
    scope: str = SEARCH_SCOPE_PERIOD,
) -> list[WeeklyReport]:
    if scope == SEARCH_SCOPE_ALL:
        return _all_scope_candidates(session, conditions, keyword, limit)

    stmt = (
        select(WeeklyReport)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
    )
    if keyword:
        stmt = stmt.where(_keyword_condition(keyword))

    return list(
        session.scalars(
            stmt.order_by(
                WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc()
            )
            .limit(limit)
        )
    )


def _all_scope_candidates(
    session: Session,
    conditions: list[Any],
    keyword: str | None,
    limit: int,
) -> list[WeeklyReport]:
    """全局检索的候选集：每个周期取最近 1 篇，周期从新到旧。

    实测过的问题：12000 字符预算大约只够读 6 篇。若按提交时间倒序取候选，
    这 6 篇会**全部落在最近一周**，用户点「全部周期」得到的其实是「上周」。
    按周期轮转能让有限的预算跨越整个时间范围，这才是「全局」该有的样子。

    用窗口函数而不是 N 次查询：``start_date`` 就是周期标识（每个自然周一个值），
    SQLite 与 PostgreSQL 都支持 ``ROW_NUMBER() OVER (PARTITION BY ...)``。
    """
    ranked = (
        select(
            WeeklyReport.id.label("report_id"),
            func.row_number()
            .over(
                partition_by=WeeklyReport.start_date,
                order_by=(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc()),
            )
            .label("rank_in_period"),
        )
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
    )
    if keyword:
        ranked = ranked.where(_keyword_condition(keyword))
    ranked_subquery = ranked.subquery()

    stmt = (
        select(WeeklyReport)
        .join(ranked_subquery, WeeklyReport.id == ranked_subquery.c.report_id)
        .where(ranked_subquery.c.rank_in_period == 1)
        .order_by(
            # 「空周期排最后」必须显式写出来：SQLite 与 PG 对 NULL 的默认排序方向相反
            WeeklyReport.start_date.is_(None),
            WeeklyReport.start_date.desc(),
            WeeklyReport.submitted_at.desc(),
            WeeklyReport.id.desc(),
        )
        .limit(limit)
    )
    return list(session.scalars(stmt))


def _render_context(
    session: Session,
    reports: list[WeeklyReport],
    budget: int,
    per_report_cap: int | None = None,
) -> tuple[str, bool, list[int]]:
    """把候选周报渲染成给模型看的文本。

    超出字符预算就停止追加（并标记 truncated），避免成本失控与超上下文。
    ``per_report_cap`` 是单篇配额：全局检索时用它给每篇封顶，否则一两篇超长
    周报会把整个预算吃光，周期覆盖度直接塌回一周。
    """
    dept_names = department_name_map(session)
    employees = {
        emp.id: emp
        for emp in session.scalars(
            select(Employee).where(
                Employee.id.in_({r.employee_id for r in reports if r.employee_id})
            )
        )
    } if reports else {}

    chunks: list[str] = []
    used = 0
    truncated = False
    included: list[int] = []

    for report in reports:
        employee = employees.get(report.employee_id) if report.employee_id else None
        dept_name = dept_names.get(employee.department_id) if employee else None
        period = (
            f"{report.start_date.isoformat()}~{report.end_date.isoformat()}"
            if report.start_date and report.end_date
            else (report.start_date.isoformat() if report.start_date else "-")
        )
        body = (report.content or report.title or "").strip() or "（无正文）"
        if per_report_cap is not None and len(body) > per_report_cap:
            body = body[:per_report_cap] + "\n（本篇正文过长，已按单篇配额截断）"
        chunk = (
            f"---\n周报ID: {report.id}\n"
            f"提交人: {employee.name if employee else '未知'}"
            f"{('（' + dept_name + '）') if dept_name else ''}\n"
            f"周期: {period}\n"
            f"标题: {report.title or '（无标题）'}\n"
            f"正文:\n{body}\n"
        )
        if used + len(chunk) > budget:
            # 预算不够也要保住第一篇：否则「预算太小」会被下游误读成
            # 「库里没有周报」，那是两个完全不同的问题。
            if not included:
                chunks.append(chunk[:budget])
                included.append(report.id)
            truncated = True
            break
        chunks.append(chunk)
        used += len(chunk)
        included.append(report.id)

    return "\n".join(chunks), truncated, included


def chat_owner_condition(owner_employee_id: int | None):
    """聊天归属过滤条件：超管（None）只看 owner 为 NULL 的记录。

    严格隔离：包括超管在内，每个人只能看到自己的对话；密码超管看到的是
    自己产生的会话 + 迁移前的历史会话（owner 为 NULL）。
    """
    if owner_employee_id is None:
        return AIAnalysis.owner_employee_id.is_(None)
    return AIAnalysis.owner_employee_id == owner_employee_id


def _log(
    session: Session,
    payload: dict[str, Any],
    status: str,
    model: str | None,
    error: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    owner_employee_id: int | None = None,
) -> None:
    session.add(
        AIAnalysis(
            entity_type=SEARCH_ENTITY_TYPE,
            entity_id=SEARCH_ENTITY_ID,
            analysis_type=SEARCH_ANALYSIS_TYPE,
            conversation_id=payload.get("conversation_id"),
            # 聊天归属：企微会话记员工 id；密码超管为 None（归超管所有）
            owner_employee_id=owner_employee_id,
            model_provider=runtime_settings(session).ai_provider,
            model_name=model,
            prompt_version=SEARCH_PROMPT_VERSION,
            result_json=payload
            if error is None
            else {**payload, "error": error},
            # 模型按 token 计费：不落库，这笔账就永远查不到
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status=status,
            error_message=error,
            created_at=utcnow(),
        )
    )
    session.commit()


def _conversation_context(
    session: Session,
    conversation_id: str | None,
    limit: int = 4,
    owner_employee_id: int | None = None,
) -> list[dict[str, str]]:
    """读取同一会话的最近问答，供追问理解上下文。

    会话上下文按归属人过滤：否则用户 B 复用用户 A 的 conversation_id
    就能把 A 的问答灌进自己的上下文，聊天隔离形同虚设。
    """
    if not conversation_id:
        return []
    rows = session.scalars(
        select(AIAnalysis)
        .where(
            AIAnalysis.analysis_type == SEARCH_ANALYSIS_TYPE,
            AIAnalysis.conversation_id == conversation_id,
            chat_owner_condition(owner_employee_id),
            AIAnalysis.deleted_at.is_(None),
        )
        .order_by(AIAnalysis.id.desc())
        .limit(limit)
    )
    turns: list[dict[str, str]] = []
    for row in rows:
        if not history_row_visible(session, row):
            continue
        data = row.result_json or {}
        question = str(data.get("question") or "").strip()
        answer = str(data.get("answer") or "").strip()
        if question and answer:
            turns.append({"question": question, "answer": answer[:1800]})
        if len(turns) >= limit:
            break
    return list(reversed(turns))


def search_reports(
    session: Session,
    question: str,
    start: str | None = None,
    end: str | None = None,
    department_id: int | None = None,
    employee_id: int | None = None,
    keyword: str | None = None,
    conversation_id: str | None = None,
    scope: str | None = None,
    owner_employee_id: int | None = None,
) -> dict[str, Any]:
    """用自然语言检索周报并返回带引用的答案。

    ``scope="all"`` 时不限周期（全局检索），此时 start/end 不参与过滤，
    回报给前端的 period 是**命中数据的实际跨度**，没有命中则为 None。

    ``owner_employee_id``：聊天归属（企微员工 id，超管为 None），用于
    会话上下文回放与聊天记录隔离；检索范围本身由 session.info 上的
    DataScope 收口（dept_admin 只能检索到管辖部门的周报）。
    """
    settings = runtime_settings(session)
    scope = normalize_scope(scope)
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    access_snapshot = capture_access_scope(
        session, department_id=department_id, employee_id=employee_id
    )

    if scope == SEARCH_SCOPE_ALL:
        start_date: date | None = None
        end_date: date | None = None
    else:
        start_date, end_date = resolve_period(start, end)

    keyword = (keyword or "").strip() or None
    conditions = _base_conditions(
        start_date, end_date, scope, department_id, employee_id
    )
    conditions.append(ai_report_condition(session))

    matched_total, span_start, span_end = _match_summary(session, conditions, keyword)
    fallback = False  # 保留历史响应字段，但严格关键词范围下永远不自动回退。

    reports = _candidate_reports(
        session, conditions, keyword, settings.ai_search_max_reports, scope
    )
    per_report_cap: int | None = None
    if scope == SEARCH_SCOPE_ALL and reports:
        # 公平配额：全局检索追求的是「跨越时间范围」。不封顶的话，一篇超长周报
        # 就能吃掉整个预算（实测 14 个周期最后只读到 2 个）。
        per_report_cap = max(
            MIN_REPORT_CHARS, settings.ai_max_input_chars // len(reports)
        )
    context, _, included = _render_context(
        session, reports, settings.ai_max_input_chars, per_report_cap
    )

    read_count = len(included)
    # 只要没读完命中集（篇数上限或字符预算任一条先到）就算截断，
    # 让前端能说出「命中 137 篇，本次阅读最近 30 篇」这种可核对的话。
    truncated = read_count < matched_total

    if scope == SEARCH_SCOPE_ALL:
        period_payload: dict[str, Any] = {
            "start": span_start.isoformat() if span_start else None,
            "end": span_end.isoformat() if span_end else None,
        }
        period_text = (
            f"全部周期（{period_payload['start']} ~ {period_payload['end']}）"
            if span_start and span_end
            else "全部周期"
        )
    else:
        period_payload = {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        }
        period_text = f"{period_payload['start']} ~ {period_payload['end']}"

    base_payload: dict[str, Any] = {
        "question": question,
        "conversation_id": conversation_id,
        "scope": scope,
        "period": period_payload,
        # 与 Agent 落库口径一致：历史会话按部门隔离依赖这个字段
        "department_id": department_id,
        "employee_id": employee_id,
        "access_scope": access_snapshot,
        "candidate_ids": included,
        "matched_total": matched_total,
        "read_count": read_count,
        "truncated": truncated,
        "fallback": fallback,
    }

    if not included:
        # 没有候选就没必要花钱问模型，直接给确定答案
        no_result_answer = (
            "当前筛选条件下没有找到任何周报，换个周期或部门再试。"
            if scope != SEARCH_SCOPE_ALL
            else "库里还没有可检索的周报，先同步一次再看。"
        )
        payload = {
            **base_payload,
            "answer": no_result_answer,
            "sources": [],
            "not_found": True,
            "model": None,
            "prompt_version": SEARCH_PROMPT_VERSION,
        }
        # 仍然留一条成功记录：用户需要能在聊天记录里回看这次检索，且不应
        # 因为没有候选周报而误以为请求没有发出。
        _log(session, payload, AIAnalysisStatus.SUCCESS, None, owner_employee_id=owner_employee_id)
        return payload

    user_prompt = SEARCH_USER_TEMPLATE.format(
        question=question,
        period=period_text,
        count=len(included),
        reports=context,
    )
    if truncated:
        user_prompt += (
            f"\n\n注意：本次共命中 {matched_total} 篇，只提供了其中最近的 "
            f"{read_count} 篇。只能基于这 {read_count} 篇作答，"
            "涉及「全部」「一共」这类整体判断时，必须说明样本不完整。"
        )
    previous_turns = _conversation_context(
        session, conversation_id, owner_employee_id=owner_employee_id
    )
    if previous_turns:
        user_prompt += (
            "\n\n此前同一会话的问答（仅用于理解‘上面/刚才’等指代；事实仍以本次候选周报为准）：\n"
            + "\n".join(
                f"用户：{turn['question']}\n助手：{turn['answer']}"
                for turn in previous_turns
            )
        )

    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    try:
        provider = get_provider(settings)
        result = provider.chat(SEARCH_SYSTEM, user_prompt)
        model_name = result.model or settings.ai_model
        prompt_tokens = result.prompt_tokens
        completion_tokens = result.completion_tokens
        parsed = extract_json_object(result.content)
    except (LLMError, ValueError) as exc:
        logger.warning("周报检索问答失败：%s", exc)
        # 调用失败通常拿不到 usage；若在返回解析阶段失败，仍把已发生的用量记下来
        _log(
            session,
            base_payload,
            AIAnalysisStatus.FAILED,
            model_name,
            str(exc),
            prompt_tokens,
            completion_tokens,
            owner_employee_id=owner_employee_id,
        )
        raise

    raw_sources = parsed.get("sources") or []
    allowed = set(included)
    sources: list[dict[str, Any]] = []
    report_index = {r.id: r for r in reports}
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        try:
            report_id = int(item.get("report_id"))
        except (TypeError, ValueError):
            continue
        # 只接受本次上下文里的 ID，模型编造的一律丢弃
        if report_id not in allowed:
            continue
        report = report_index[report_id]
        sources.append(
            {
                "report_id": report_id,
                "title": report.title,
                "employee_id": report.employee_id,
                "period_start": report.start_date.isoformat()
                if report.start_date
                else None,
                "period_end": report.end_date.isoformat() if report.end_date else None,
                "reason": str(item.get("reason") or "")[:200],
            }
        )
        if len(sources) >= 5:
            break

    answer = str(parsed.get("answer") or "").strip() or "模型未给出有效回答。"
    not_found = bool(parsed.get("not_found", False)) or not sources

    payload = {
        **base_payload,
        "answer": answer,
        "sources": sources,
        "not_found": not_found,
        "raw": json.dumps(parsed, ensure_ascii=False)[:2000],
    }
    _log(
        session,
        payload,
        AIAnalysisStatus.SUCCESS,
        model_name,
        None,
        prompt_tokens,
        completion_tokens,
        owner_employee_id=owner_employee_id,
    )
    return {
        **base_payload,
        "answer": answer,
        "sources": sources,
        "not_found": not_found,
        "model": model_name,
        "prompt_version": SEARCH_PROMPT_VERSION,
    }
