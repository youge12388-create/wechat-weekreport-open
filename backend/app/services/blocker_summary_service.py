"""周期卡点 / 资源需求的 AI 总结。

与 ``period_summary_service`` 同一套队列模式，但输入是**结构化后的卡点清单**
（Blocker + 原文依据片段）而不是周报原文：一个周期的卡点通常只有几十条以内，
始终一次调用，没有分批回退路径。

分层说明：卡点与依据的查询口径与 ``api/routes/blockers.py`` 的列表接口保持一致
（免提交人员的来源不进入总结），但查询实现在本服务内，供 Worker 独立使用。
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.ai import get_provider
from app.ai.prompts import BLOCKER_SUMMARY_PROMPT_VERSION, BLOCKER_SUMMARY_SYSTEM
from app.core.time_utils import utcnow
from app.models.ai_analysis import AIAnalysis
from app.models.blocker import Blocker
from app.models.blocker_summary_queue import BlockerSummaryQueue
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus, AnalysisQueueStatus
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services.runtime_config_service import runtime_settings
from app.services.view_service import (
    non_exempt_report_condition,
    report_period_overlap_conditions,
)

ENTITY_TYPE = "blocker_period"
ANALYSIS_TYPE = "blocker_summary"
PROMPT_VERSION = BLOCKER_SUMMARY_PROMPT_VERSION

# 单个卡点最多带几条原文依据进提示词（按最近优先保留）。
EVIDENCE_PER_BLOCKER_LIMIT = 3
# 单条依据片段最多多少字符。
EVIDENCE_SNIPPET_LIMIT = 500


def _blockers_for_period(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    """本周期出现过的卡点（开放 + 已解决），每条带原文依据。

    口径与处理中心列表一致：只统计有非免提交来源的卡点，避免免提交
    人员的内容泄漏进总结。已解决卡点也纳入（带状态标注），让模型能
    读出「哪些已经解决、哪些还在烧」。
    """
    period_conditions = report_period_overlap_conditions(start, end)
    rows = list(
        session.execute(
            select(Blocker, Task)
            .join(Task, Task.id == Blocker.task_id)
            .where(
                Blocker.task_id.in_(
                    select(TaskReportLink.task_id)
                    .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
                    .where(*period_conditions, non_exempt_report_condition(session))
                )
            )
            .order_by(
                # 开放优先，再按风险 高→中→低、持续周数、ID；中文列直接 desc 排不出语义序
                case((Blocker.status == "OPEN", 0), else_=1),
                case(
                    (Blocker.risk_level == "高", 3),
                    (Blocker.risk_level == "中", 2),
                    else_=1,
                ).desc(),
                Blocker.duration_weeks.desc(),
                Blocker.id,
            )
        )
    )
    if not rows:
        return []
    evidence = _evidence_rows(session, [task.id for _, task in rows], period_conditions)
    items: list[dict[str, Any]] = []
    for blocker, task in rows:
        items.append(
            {
                "id": blocker.id,
                "title": blocker.title,
                "description": blocker.description,
                "status": blocker.status,
                "risk_level": blocker.risk_level,
                "duration_weeks": blocker.duration_weeks,
                "occurrence_count": blocker.occurrence_count,
                "first_seen_date": blocker.first_seen_date.isoformat()
                if blocker.first_seen_date
                else None,
                "last_seen_date": blocker.last_seen_date.isoformat()
                if blocker.last_seen_date
                else None,
                "related_project": task.related_project,
                "updated_at": blocker.updated_at.isoformat()
                if blocker.updated_at
                else None,
                "sources": evidence.get(task.id, []),
            }
        )
    return items


def _evidence_rows(
    session: Session,
    task_ids: list[int],
    period_conditions: tuple[Any, ...],
    *,
    per_blocker_limit: int = EVIDENCE_PER_BLOCKER_LIMIT,
) -> dict[int, list[dict[str, Any]]]:
    """卡点的原文依据：每篇周报只保留一条（与列表接口同一去重口径）。

    每个卡点最多保留 ``per_blocker_limit`` 条依据。不设上限时，一个持续半年的卡点
    会把所有历史周报的原文都拼进提示词（每篇最多 500 字符），单条卡点就能吃掉几万
    字符 —— 提示词体积不受控，而这条路径没有分批回退，超限就是失败。
    查询已按 `end_date desc` 排序，所以保留的是**最近**的几条，恰好是最能说明
    当前状态的那部分。
    """
    if not task_ids:
        return {}
    query = (
        select(
            TaskReportLink.task_id,
            WeeklyReport.id,
            Employee.name,
            Department.name,
            ReportItem.source_text,
        )
        .select_from(TaskReportLink)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .join(WeeklyReport, WeeklyReport.id == TaskReportLink.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .join(Department, Department.id == Employee.department_id, isouter=True)
        .where(
            TaskReportLink.task_id.in_(task_ids),
            *period_conditions,
            non_exempt_report_condition(session),
        )
        .order_by(WeeklyReport.end_date.desc(), WeeklyReport.id.desc())
    )
    result: dict[int, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    seen: dict[int, set[int]] = {task_id: set() for task_id in task_ids}
    for task_id, report_id, employee_name, department_name, source_text in session.execute(query):
        if report_id in seen[task_id] or len(result[task_id]) >= per_blocker_limit:
            continue
        seen[task_id].add(report_id)
        result[task_id].append(
            {
                "report_id": report_id,
                "employee_name": employee_name,
                "department_name": department_name,
                "source_text": source_text,
            }
        )
    return result


def _payload_hash(items: list[dict[str, Any]]) -> str:
    payload = {
        "prompt_version": PROMPT_VERSION,
        "blockers": [
            {
                "id": item["id"],
                "title": item["title"],
                "description": item["description"],
                "status": item["status"],
                "risk_level": item["risk_level"],
                "duration_weeks": item["duration_weeks"],
                "occurrence_count": item["occurrence_count"],
                "updated_at": item["updated_at"],
                "sources": item["sources"],
            }
            for item in items
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _blocker_block(item: dict[str, Any]) -> str:
    """单个卡点在提示词里的文本块。"""
    status_label = "已解决" if item["status"] == "RESOLVED" else "开放"
    lines = [
        "---",
        f"标题: {item['title']}",
        f"状态: {status_label}",
        f"风险等级: {item['risk_level']}",
        f"持续: {item['duration_weeks']} 周，出现 {item['occurrence_count']} 次",
    ]
    if item["description"]:
        lines.append(f"描述: {item['description']}")
    if item["related_project"]:
        lines.append(f"项目: {item['related_project']}")
    sources = item["sources"]
    if sources:
        lines.append("原文依据:")
        for source in sources:
            text = (source.get("source_text") or "").strip()[:EVIDENCE_SNIPPET_LIMIT]
            lines.append(
                f"- 周报ID: {source['report_id']}，"
                f"提交人 {source.get('employee_name') or '未知'}"
                f"（{source.get('department_name') or '未分配'}）：{text}"
            )
    else:
        lines.append("原文依据: 无")
    return "\n".join(lines)


def _blocker_context_text(
    items: list[dict[str, Any]], budget: int
) -> dict[str, Any]:
    """把卡点清单拼成一次请求的正文，**受字符预算约束**。

    与 ``period_summary_service._full_context_text`` 同一纪律：绝不静默截断成
    残缺的「全量」。装得下就是全量；装不下就按优先级取前 N 条，并把
    ``included``/``total``/``truncated`` 如实带回结论，由前端显示「仅总结前 N 条」。

    取的是**排在前面的**卡点 —— ``_blockers_for_period`` 已按「开放优先 → 风险
    高到低 → 持续周数」排好序，所以被舍掉的恰好是最不紧急的那些。

    为什么必须有一个上限：这条路径没有分批回退（卡点总结的价值就在跨卡点关联，
    分批会把关联切断），所以超限的代价不是「慢」而是「整轮失败」。实测 60 个卡点、
    每个两条依据就有 8.7 万字符，而上游一旦拒绝，队列会按重试次数把同一份超长
    payload 反复重发，每次都计费。
    """
    blocks: list[str] = []
    total_chars = 0
    included = 0
    truncated = False
    for item in items:
        block = _blocker_block(item)
        # 第一个卡点就超预算时也要收下：至少让它有一条完整内容可总结，
        # 而不是返回空正文让模型去猜。
        if blocks and total_chars + len(block) > budget:
            truncated = True
            break
        blocks.append(block)
        total_chars += len(block)
        included += 1
    return {
        "text": "\n".join(blocks),
        "included": included,
        "total": len(items),
        "truncated": truncated,
        "chars": total_chars,
    }


def _queue_for_period(session: Session, start: date, end: date) -> BlockerSummaryQueue | None:
    return session.scalar(
        select(BlockerSummaryQueue).where(
            BlockerSummaryQueue.start_date == start,
            BlockerSummaryQueue.end_date == end,
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


def blocker_summary_state(session: Session, start: date, end: date) -> dict[str, Any]:
    items = _blockers_for_period(session, start, end)
    current_hash = _payload_hash(items)
    queue = _queue_for_period(session, start, end)
    base: dict[str, Any] = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "blockers_total": len(items),
        "processed_blockers": 0,
        "status": "not_generated",
        "stale": False,
        "summary": None,
        "created_at": None,
        "model": None,
        "last_error": None,
        "source_report_ids": [],
        # 覆盖范围：covered_blockers < blockers_total 时前端必须显示
        # 「仅总结前 N 条」，不能让局部结论被当成全量结论（与周期总览的
        # full_context 同一纪律）。
        "covered_blockers": None,
        "truncated": False,
        "context_chars": None,
    }
    if not items:
        return {**base, "status": "empty"}
    if queue is None:
        return base
    stale = queue.payload_hash != current_hash
    base.update(
        {
            "queue_id": queue.id,
            "processed_blockers": queue.blocker_count,
            "status": "stale" if stale else queue.status,
            "stale": stale,
            "last_error": queue.last_error if not stale else None,
        }
    )
    if not stale and queue.status == AnalysisQueueStatus.SUCCESS:
        analysis = _latest_summary(session, queue.id)
        if analysis is not None:
            data = analysis.result_json or {}
            covered = data.get("covered_blockers")
            base.update(
                {
                    "summary": data.get("summary"),
                    "processed_blockers": int(
                        data.get("processed_blockers") or queue.blocker_count
                    ),
                    "created_at": analysis.created_at.isoformat()
                    if analysis.created_at
                    else None,
                    "model": analysis.model_name,
                    "source_report_ids": data.get("source_report_ids") or [],
                    "covered_blockers": int(covered) if covered is not None else None,
                    "truncated": bool(data.get("context_truncated")),
                    "context_chars": data.get("context_chars"),
                }
            )
        else:
            base["status"] = "failed"
            base["last_error"] = "未找到已完成的卡点总结结果。"
    return base


def enqueue_blocker_summary(
    session: Session, start: date, end: date, *, force: bool = False
) -> dict[str, Any]:
    items = _blockers_for_period(session, start, end)
    if not items:
        return blocker_summary_state(session, start, end)
    payload_hash = _payload_hash(items)
    queue = _queue_for_period(session, start, end)
    if queue is None:
        queue = BlockerSummaryQueue(
            start_date=start,
            end_date=end,
            payload_hash=payload_hash,
            blocker_count=len(items),
            status=AnalysisQueueStatus.PENDING,
        )
        session.add(queue)
    elif (
        force
        or queue.payload_hash != payload_hash
        or queue.status in (AnalysisQueueStatus.FAILED, AnalysisQueueStatus.PENDING)
    ):
        queue.payload_hash = payload_hash
        queue.blocker_count = len(items)
        queue.status = AnalysisQueueStatus.PENDING
        queue.attempts = 0
        queue.next_attempt_at = None
        queue.last_error = None
    session.commit()
    return blocker_summary_state(session, start, end)


def _write_summary(
    session: Session,
    queue: BlockerSummaryQueue,
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
            prompt_version=PROMPT_VERSION,
            result_json=result if error is None else {**result, "error": error},
            input_data={
                "period": {
                    "start": queue.start_date.isoformat(),
                    "end": queue.end_date.isoformat(),
                },
                "payload_hash": queue.payload_hash,
                "blocker_count": queue.blocker_count,
            },
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status=status,
            error_message=error,
            created_at=utcnow(),
        )
    )


def process_blocker_summary_item(queue_id: int) -> dict[str, Any]:
    """消费一条已领取的卡点总结任务；Worker 线程各自持有独立 Session。"""
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        queue = session.get(BlockerSummaryQueue, queue_id)
        if queue is None or queue.status != AnalysisQueueStatus.RUNNING:
            return {"queue_id": queue_id, "status": "skipped"}
        items = _blockers_for_period(session, queue.start_date, queue.end_date)
        current_hash = _payload_hash(items)
        # 入队到消费间卡点有变化：放回 pending，下轮按最新全量数据处理。
        if current_hash != queue.payload_hash:
            queue.payload_hash = current_hash
            queue.blocker_count = len(items)
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = None
            session.commit()
            return {"queue_id": queue_id, "status": "requeued"}
        if not items:
            queue.status = AnalysisQueueStatus.SUCCESS
            queue.last_error = None
            session.commit()
            return {"queue_id": queue_id, "status": "empty"}

        settings = runtime_settings(session)
        provider = get_provider(settings)
        context = _blocker_context_text(
            items, settings.ai_blocker_summary_context_chars
        )
        started = time.monotonic()
        # 覆盖范围必须写进提示词：否则模型不知道自己只看到了一部分，
        # 会把「前 N 条的情况」当成「本期的全部情况」来下结论。
        coverage_note = (
            f"（共 {context['total']} 条，本次提供其中前 {context['included']} 条，"
            "其余因篇幅未提供）"
            if context["truncated"]
            else f"（共 {context['total']} 条，已完整提供）"
        )
        response = provider.chat(
            BLOCKER_SUMMARY_SYSTEM,
            f"统计周期：{queue.start_date.isoformat()} ~ {queue.end_date.isoformat()}\n"
            f"以下是本周期全部卡点 / 资源需求{coverage_note}"
            f"，含原文依据，不需要再检索：\n{context['text']}",
        )
        summary_text = response.content.strip()
        duration_ms = int(round((time.monotonic() - started) * 1000))

        result = {
            "summary": summary_text or "模型未返回有效卡点总结。",
            "processed_blockers": len(items),
            "source_report_ids": sorted(
                {
                    source["report_id"]
                    for item in items
                    for source in item["sources"]
                }
            ),
            "blocker_count": len(items),
            "full_context": True,
            # 如实标注覆盖范围：截断时前端必须显示「仅总结前 N 条」，
            # 不能让局部结论被当成全量结论。
            "covered_blockers": context["included"],
            "context_truncated": bool(context["truncated"]),
            "context_chars": int(context["chars"]),
        }
        _write_summary(
            session,
            queue,
            result,
            AIAnalysisStatus.SUCCESS,
            response.model or settings.ai_model,
            int(response.prompt_tokens or 0) or None,
            int(response.completion_tokens or 0) or None,
            duration_ms,
        )
        queue.status = AnalysisQueueStatus.SUCCESS
        queue.last_error = None
        session.commit()
        return {"queue_id": queue_id, "status": "success", "processed_blockers": len(items)}
    except Exception as exc:  # noqa: BLE001 队列任务必须将任意失败落库并可重试
        session.rollback()
        queue = session.get(BlockerSummaryQueue, queue_id)
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
        _write_summary(session, queue, {}, AIAnalysisStatus.FAILED, None, None, None, None, str(exc))
        session.commit()
        return {"queue_id": queue_id, "status": "failed", "error": str(exc)[:500]}
    finally:
        session.close()
