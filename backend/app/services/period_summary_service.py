"""全员周期周报的 AI 汇总。

默认**一次读完**：范围内正文总量不超过 ``ai_period_summary_full_context_chars``
时，把全部原文放进同一次请求。分批 map-reduce 会切断跨篇关联（同一事项出现在
多个部门、卡点与下周计划互相呼应），而这正是管理层看总览的意义。
范围实在装不下（如跨月批量汇总）才回退到分批，且结果里如实标注 ``full_context=false``，
绝不静默截断后还宣称「全量」。
"""

from __future__ import annotations

import calendar
import hashlib
import json
import time
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import chat_with_options, get_provider
from app.ai.base import LLMError, LLMUsage
from app.ai.prompts import (
    PERIOD_SUMMARY_BATCH_SYSTEM,
    PERIOD_SUMMARY_PROMPT_VERSION,
    PERIOD_SUMMARY_SYSTEM,
)
from app.core.time_utils import utcnow
from app.models.ai_analysis import AIAnalysis
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus, AnalysisQueueStatus
from app.models.period_summary_queue import PeriodSummaryQueue
from app.models.weekly_report import WeeklyReport
from app.services.report_text import report_body
from app.services.runtime_config_service import runtime_settings
from app.services.view_service import ai_report_condition, report_period_overlap_conditions

ENTITY_TYPE = "weekly_report_period"
ANALYSIS_TYPE = "period_summary"
PROMPT_VERSION = PERIOD_SUMMARY_PROMPT_VERSION

# 单次全量请求最多包含多少篇；再多就回退到分批，避免把一次请求堆成巨型 payload。
FULL_CONTEXT_MAX_REPORTS = 300

# 分批回退路径使用的提示词（导入自 app.ai.prompts，版本随 PERIOD_SUMMARY_PROMPT_VERSION 走）
_BATCH_SYSTEM = PERIOD_SUMMARY_BATCH_SYSTEM
_FINAL_SYSTEM = PERIOD_SUMMARY_SYSTEM


def _period_prompt_context(start: date, end: date, local_today: date) -> dict[str, str]:
    """把日期范围转换成模型可理解的本周/上周/本月语义。"""
    current_week_start = local_today - timedelta(days=local_today.weekday())
    current_week_end = current_week_start + timedelta(days=6)
    previous_week_start = current_week_start - timedelta(days=7)
    previous_week_end = current_week_start - timedelta(days=1)
    current_month_start = local_today.replace(day=1)
    current_month_end = local_today.replace(
        day=calendar.monthrange(local_today.year, local_today.month)[1]
    )

    if (start, end) == (current_week_start, current_week_end):
        return {"label": "本周", "focus": "本周重点", "next": "下周计划"}
    if (start, end) == (previous_week_start, previous_week_end):
        return {"label": "上周", "focus": "上周重点", "next": "本周计划"}
    if (start, end) == (current_month_start, current_month_end):
        # 月中汇总的来源仍是逐周周报，原文里的“下周计划”通常并不等于“下月计划”。
        # 用“后续计划”保留原始时间语义，避免为了标题强行改写成不存在的月度承诺。
        return {"label": "本月", "focus": "本月重点", "next": "后续计划"}
    return {"label": "所选周期", "focus": "本期重点", "next": "下期计划"}


def _period_prompt_instruction(
    start: date, end: date, *, offset_hours: int, now: datetime | None = None
) -> str:
    current = now or utcnow()
    local_today = (current + timedelta(hours=offset_hours)).date()
    context = _period_prompt_context(start, end, local_today)
    return (
        f"周期语义：{context['label']}。"
        f"重点标题固定为“## {context['focus']}”，"
        f"计划标题固定为“## {context['next']}”。"
    )



def _reports_for_period(session: Session, start: date, end: date) -> list[tuple[WeeklyReport, Employee | None, Department | None]]:
    return list(
        session.execute(
            select(WeeklyReport, Employee, Department)
            .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
            .join(Department, Employee.department_id == Department.id, isouter=True)
            .where(
                *report_period_overlap_conditions(start, end),
                WeeklyReport.report_type == "weekly",
                ai_report_condition(session),
            )
            .order_by(Employee.name, WeeklyReport.submitted_at, WeeklyReport.id)
        )
    )


def _report_body(report: WeeklyReport) -> str:
    """交给模型的正文：统一走 report_text，确保与其它 AI 路径同一口径。"""
    return report_body(report)


def _payload_hash(rows: list[tuple[WeeklyReport, Employee | None, Department | None]]) -> str:
    # 只哈希「真正决定提示词的东西」：提示词版本、正文（清洗后）、标题、提交人。
    # 用 content_raw 参与哈希会让一次正文没变的富文本重排也判定为「内容有变」。
    payload = {
        "prompt_version": PROMPT_VERSION,
        "reports": [
            {
                "id": report.id,
                "employee_id": report.employee_id,
                "updated_at": report.updated_at.isoformat() if report.updated_at else None,
                "body": _report_body(report),
                "title": report.title,
            }
            for report, _, _ in rows
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _chunk_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True) or [text]:
        rest = line
        while rest:
            space = limit - len(current)
            if space <= 0:
                parts.append(current)
                current = ""
                space = limit
            current += rest[:space]
            rest = rest[space:]
            if len(current) >= limit:
                parts.append(current)
                current = ""
    if current:
        parts.append(current)
    return parts or ["（无正文）"]


def _source_batches(
    rows: list[tuple[WeeklyReport, Employee | None, Department | None]], budget: int
) -> list[str]:
    """每份周报的每一段正文都保留 report id，组装成不超预算的批次。"""
    batches: list[str] = []
    current = ""
    body_limit = max(budget - 350, 1000)
    for report, employee, department in rows:
        body = _report_body(report)
        header = (
            f"周报ID: {report.id}\n"
            f"提交人: {employee.name if employee else '未知'}\n"
            f"部门: {department.name if department else '未分配'}\n"
            f"标题: {report.title or '无标题'}\n"
        )
        for index, segment in enumerate(_chunk_text(body, body_limit), start=1):
            entry = f"---\n{header}正文片段 {index}:\n{segment}\n"
            if current and len(current) + len(entry) > budget:
                batches.append(current)
                current = ""
            current += entry
    if current:
        batches.append(current)
    return batches


def _group_texts(items: list[str], budget: int) -> list[str]:
    groups: list[str] = []
    current = ""
    for item in items:
        part = f"---\n{item}\n"
        if current and len(current) + len(part) > budget:
            groups.append(current)
            current = ""
        # 极端模型输出过长时继续分段，不丢弃内容。
        if len(part) > budget:
            if current:
                groups.append(current)
                current = ""
            groups.extend(_chunk_text(part, budget))
        else:
            current += part
    if current:
        groups.append(current)
    return groups


def _full_context_text(
    rows: list[tuple[WeeklyReport, Employee | None, Department | None]], budget: int
) -> dict[str, Any]:
    """把这个周期的全部原文拼成**一次请求**的正文。

    装得下就 ``enabled=True``；超预算或篇数过多返回 ``enabled=False``（``reason`` 说明原因），
    由调用方回退到分批汇总。两种情况都不会截断正文，``chars`` 始终是本周期正文的真实总量
    （超限时也继续统计，否则运维看到 1 万字符会想不通为什么没走一次全量）。
    """
    def _block_for(
        report: WeeklyReport, employee: Employee | None, department: Department | None
    ) -> str:
        body = _report_body(report)
        return (
            "---\n"
            f"周报ID: {report.id}\n"
            f"提交人: {employee.name if employee else '未知'}\n"
            f"部门: {department.name if department else '未分配'}\n"
            f"标题: {report.title or '无标题'}\n"
            f"正文:\n{body}\n"
        )

    blocks: list[str] = []
    total = 0
    over_budget = False
    for report, employee, department in rows:
        if over_budget:
            # 已经确定装不下：只继续累计长度用于如实报告，不再拼字符串白占内存。
            total += len(_block_for(report, employee, department))
            continue
        block = _block_for(report, employee, department)
        total += len(block)
        if total > budget or len(blocks) >= FULL_CONTEXT_MAX_REPORTS:
            over_budget = True
            continue
        blocks.append(block)

    if over_budget:
        return {
            "enabled": False,
            "reason": "too_large",
            "report_count": len(rows),
            "chars": total,
            "text": "",
        }
    if not blocks:
        return {
            "enabled": False,
            "reason": "empty",
            "report_count": 0,
            "chars": 0,
            "text": "",
        }
    return {
        "enabled": True,
        "reason": None,
        "report_count": len(rows),
        "chars": total,
        "text": "\n".join(blocks),
    }


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


def _queue_for_period(session: Session, start: date, end: date) -> PeriodSummaryQueue | None:
    return session.scalar(
        select(PeriodSummaryQueue).where(
            PeriodSummaryQueue.start_date == start,
            PeriodSummaryQueue.end_date == end,
        )
    )


def period_summary_state(session: Session, start: date, end: date) -> dict[str, Any]:
    rows = _reports_for_period(session, start, end)
    current_hash = _payload_hash(rows)
    queue = _queue_for_period(session, start, end)
    base: dict[str, Any] = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "reports_total": len(rows),
        "processed_reports": 0,
        "status": "not_generated",
        "stale": False,
        "summary": None,
        "created_at": None,
        "model": None,
        "last_error": None,
        # 生成口径：true=一次读完全部原文；false=范围过大走分批；null=还没有结论。
        "full_context": None,
        "context_chars": None,
        # 结论里 [#编号] 引用过的周报 ID：前端据此把引用渲染成可点来源，
        # 没拿到这个集合时引用只能显示成不可点的灰色标记。
        "source_report_ids": [],
    }
    if not rows:
        return {**base, "status": "empty"}
    if queue is None:
        return base
    stale = queue.payload_hash != current_hash
    base.update(
        {
            "queue_id": queue.id,
            "processed_reports": queue.report_count,
            "status": "stale" if stale else queue.status,
            "stale": stale,
            "last_error": queue.last_error if not stale else None,
        }
    )
    if not stale and queue.status == AnalysisQueueStatus.SUCCESS:
        analysis = _latest_summary(session, queue.id)
        if analysis is not None:
            data = analysis.result_json or {}
            base.update(
                {
                    "summary": data.get("summary"),
                    "processed_reports": int(data.get("processed_reports") or queue.report_count),
                    "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
                    "model": analysis.model_name,
                    "full_context": data.get("full_context"),
                    "context_chars": data.get("context_chars"),
                    "source_report_ids": data.get("source_report_ids") or [],
                }
            )
        else:
            base["status"] = "failed"
            base["last_error"] = "未找到已完成的 AI 汇总结果。"
    return base


def enqueue_period_summary(
    session: Session, start: date, end: date, *, force: bool = False
) -> dict[str, Any]:
    rows = _reports_for_period(session, start, end)
    if not rows:
        return period_summary_state(session, start, end)
    payload_hash = _payload_hash(rows)
    queue = _queue_for_period(session, start, end)
    if queue is None:
        queue = PeriodSummaryQueue(
            start_date=start,
            end_date=end,
            payload_hash=payload_hash,
            report_count=len(rows),
            status=AnalysisQueueStatus.PENDING,
        )
        session.add(queue)
    elif (
        force
        or queue.payload_hash != payload_hash
        or queue.status in (AnalysisQueueStatus.FAILED, AnalysisQueueStatus.PENDING)
    ):
        queue.payload_hash = payload_hash
        queue.report_count = len(rows)
        queue.status = AnalysisQueueStatus.PENDING
        queue.attempts = 0
        queue.next_attempt_at = None
        queue.last_error = None
    session.commit()
    return period_summary_state(session, start, end)


def _write_summary(
    session: Session,
    queue: PeriodSummaryQueue,
    result: dict[str, Any],
    status: str,
    model_name: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    duration_ms: int | None,
    error: str | None = None,
    *,
    usage: LLMUsage | None = None,
    queue_attempt: int | None = None,
) -> None:
    settings = runtime_settings(session)
    input_data = {
        "period": {
            "start": queue.start_date.isoformat(),
            "end": queue.end_date.isoformat(),
        },
        "payload_hash": queue.payload_hash,
        "report_count": queue.report_count,
    }
    if usage is not None:
        input_data["usage"] = usage.as_metadata()
    if queue_attempt is not None:
        input_data["queue_attempt"] = queue_attempt
    session.add(
        AIAnalysis(
            entity_type=ENTITY_TYPE,
            entity_id=queue.id,
            analysis_type=ANALYSIS_TYPE,
            model_provider=settings.ai_provider,
            model_name=model_name,
            prompt_version=PROMPT_VERSION,
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


def process_period_summary_item(queue_id: int) -> dict[str, Any]:
    """消费一条已领取的周期总结任务；Worker 线程各自持有独立 Session。"""
    from app.db.session import get_session_factory

    session = get_session_factory()()
    usage = LLMUsage()
    model_name: str | None = None
    try:
        queue = session.get(PeriodSummaryQueue, queue_id)
        if queue is None or queue.status != AnalysisQueueStatus.RUNNING:
            return {"queue_id": queue_id, "status": "skipped"}
        rows = _reports_for_period(session, queue.start_date, queue.end_date)
        current_hash = _payload_hash(rows)
        # 入队到消费间有新同步：放回 pending，下轮按最新全量数据处理。
        if current_hash != queue.payload_hash:
            queue.payload_hash = current_hash
            queue.report_count = len(rows)
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = None
            session.commit()
            return {"queue_id": queue_id, "status": "requeued"}
        if not rows:
            queue.status = AnalysisQueueStatus.SUCCESS
            queue.last_error = None
            session.commit()
            return {"queue_id": queue_id, "status": "empty"}

        settings = runtime_settings(session)
        provider = get_provider(settings)
        period_instruction = _period_prompt_instruction(
            queue.start_date,
            queue.end_date,
            offset_hours=settings.report_timezone_offset_hours,
        )
        started = time.monotonic()

        # 首选：一次把全部原文交给模型。分批会把同一件事在不同部门的表述割开，
        # 总览也就读不出「谁在等谁」这类跨篇关系。
        full = _full_context_text(rows, settings.ai_period_summary_full_context_chars)
        if full["enabled"]:
            response = chat_with_options(
                provider,
                PERIOD_SUMMARY_SYSTEM,
                f"{period_instruction}\n"
                f"统计周期：{queue.start_date.isoformat()} ~ {queue.end_date.isoformat()}\n"
                f"以下是本期全部 {full['report_count']} 篇周报原文"
                f"（共 {full['chars']} 字符，已完整提供，不需要再检索）：\n{full['text']}",
                thinking=False,
                usage=usage,
            )
            summary_text = response.content.strip()
            model_name = response.model or settings.ai_model
            batch_count = 1
        else:
            # 回退：范围装不下，先分批提事实，再逐层合并到一次最终请求。
            batches = _source_batches(rows, settings.ai_period_summary_chunk_chars)
            summaries: list[str] = []
            for index, batch in enumerate(batches, start=1):
                response = chat_with_options(
                    provider,
                    _BATCH_SYSTEM,
                    f"{period_instruction}\n"
                    f"统计周期：{queue.start_date.isoformat()} ~ {queue.end_date.isoformat()}\n"
                    f"第 {index}/{len(batches)} 批原文：\n{batch}",
                    thinking=False,
                    usage=usage,
                )
                summaries.append(response.content.strip())
                model_name = response.model or model_name or settings.ai_model

            # 多层归并，直到全部批次可以在一次最终请求内表达；每层都完整消费上一层输出。
            while len(summaries) > 1:
                next_level: list[str] = []
                groups = _group_texts(summaries, settings.ai_period_summary_chunk_chars)
                for group in groups:
                    response = chat_with_options(
                        provider,
                        _FINAL_SYSTEM,
                        f"{period_instruction}\n待合并的周报分批摘要：\n{group}",
                        thinking=False,
                        usage=usage,
                    )
                    next_level.append(response.content.strip())
                    model_name = response.model or model_name or settings.ai_model
                summaries = next_level

            final_response = chat_with_options(
                provider,
                _FINAL_SYSTEM,
                f"{period_instruction}\n"
                f"统计周期：{queue.start_date.isoformat()} ~ {queue.end_date.isoformat()}\n"
                f"全量周报的合并摘要：\n{summaries[0] if summaries else '原文未提供可总结内容。'}",
                thinking=False,
                usage=usage,
            )
            summary_text = final_response.content.strip()
            model_name = final_response.model or model_name or settings.ai_model
            batch_count = len(batches)

        result = {
            "summary": summary_text or "模型未返回有效周度总结。",
            "processed_reports": len(rows),
            "source_report_ids": [report.id for report, _, _ in rows],
            "batch_count": batch_count,
            # 如实告知「一次读全」还是「分批汇总」：前者才允许对跨篇关系下结论。
            "full_context": bool(full["enabled"]),
            "context_chars": int(full["chars"]),
            "context_reason": full["reason"],
        }
        duration_ms = int(round((time.monotonic() - started) * 1000))
        _write_summary(
            session,
            queue,
            result,
            AIAnalysisStatus.SUCCESS,
            model_name,
            usage.prompt_tokens,
            usage.completion_tokens,
            duration_ms,
            usage=usage,
            queue_attempt=(queue.attempts or 0) + 1,
        )
        queue.status = AnalysisQueueStatus.SUCCESS
        queue.last_error = None
        session.commit()
        return {"queue_id": queue_id, "status": "success", "processed_reports": len(rows)}
    except Exception as exc:  # noqa: BLE001 队列任务必须将任意失败落库并可重试
        session.rollback()
        queue = session.get(PeriodSummaryQueue, queue_id)
        if queue is None:
            return {"queue_id": queue_id, "status": "failed", "error": str(exc)}
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
            session,
            queue,
            {},
            AIAnalysisStatus.FAILED,
            model_name,
            usage.prompt_tokens,
            usage.completion_tokens,
            None,
            str(exc),
            usage=usage,
            queue_attempt=queue.attempts,
        )
        session.commit()
        return {"queue_id": queue_id, "status": "failed", "error": str(exc)[:500]}
    finally:
        session.close()
