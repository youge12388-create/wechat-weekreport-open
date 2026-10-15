"""AI 能力接口：周报检索问答 + 重复检测。

所有端点都走网关鉴权（AuthMiddleware），且每次调用都会写 AIAnalysis 留痕。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, inspect, literal, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import AdminOnly, CurrentActor, get_db
from app.db.session import get_session_factory
from app.models.ai_analysis import AIAnalysis
from app.models.agent_memory import AgentMemory
from app.models.analysis_queue import AnalysisQueue
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus
from app.models.enums import AnalysisQueueStatus
from app.core.time_utils import utcnow
from app.models.weekly_report import WeeklyReport
from app.ai.prompts import AGENT_PROMPT_VERSION
from app.models.agent_feedback import AgentFeedback
from app.services.analysis_queue_service import (
    enqueue_missing_period_reports,
    enqueue_period_reports_with_remaining,
    enqueue_report,
    queue_state,
)
from app.services.ai_search_service import (
    SEARCH_ANALYSIS_TYPE,
    chat_owner_condition,
    search_reports,
)
from app.services.ai_status_service import ai_capability_status
from app.services.agent_service import AGENT_ANALYSIS_TYPE, run_agent, stream_agent
from app.services.chat_history_access import capture_access_scope, history_row_visible, scope_snapshot
from app.services.insight_service import analysis_coverage
from app.services.agent_memory_service import validate_memory
from app.services.agent_tools import MAX_STEPS_DEFAULT
from app.services.structured_data_service import backfill_resource_demands
from app.services.data_scope import report_in_scope
from app.services.view_service import (
    non_exempt_report_condition,
    report_period_overlap_conditions,
    resolve_period,
)

router = APIRouter(prefix="/api/ai", tags=["ai"])


class SearchRequest(BaseModel):
    question: str = Field(min_length=1, description="自然语言问题")
    conversation_id: str | None = Field(default=None, max_length=100)
    # period=只看指定周期（默认，与看板口径一致）；all=不限周期检索全部历史周报
    scope: str | None = Field(default=None, description="检索范围：period / all")
    start: str | None = None
    end: str | None = None
    department_id: int | None = None
    employee_id: int | None = None
    keyword: str | None = None


class ScanRequest(BaseModel):
    start: str | None = None
    end: str | None = None
    department_id: int | None = None
    employee_id: int | None = None
    force: bool = False


class BackfillRequest(BaseModel):
    """历史队列回填参数；默认只回填当前默认周期，避免误扫全量历史。"""

    start: str | None = None
    end: str | None = None
    department_id: int | None = None


class AgentRequest(BaseModel):
    """Agent 问答参数。scope 与 /search 保持一致：period（默认）/ all。"""

    question: str = Field(min_length=1, description="自然语言问题")
    conversation_id: str | None = Field(default=None, max_length=100)
    scope: str | None = None
    start: str | None = None
    end: str | None = None
    department_id: int | None = None
    employee_id: int | None = None
    keyword: str | None = None
    surface: Literal["operating_dashboard"] | None = None
    dashboard_view: Literal["weekly", "monthly", "department"] | None = None
    # 上限从 8 放宽到 12：search_reports 现在可以翻页，需要更多回合才能读完命中集。
    max_steps: int | None = Field(default=None, ge=1, le=12)


class HistoryVisibilityRequest(BaseModel):
    """聊天历史软删除 / 恢复请求。"""

    deleted: bool


class AgentFeedbackRequest(BaseModel):
    category: Literal["scope", "fact", "evidence", "omission", "other"]
    reason: str | None = Field(default=None, max_length=2000)


def _agent_feedback_table_available(session: Session) -> bool:
    """反馈迁移单独授权前，历史页仍可在旧数据库结构上正常读取。"""
    return inspect(session.get_bind()).has_table(AgentFeedback.__tablename__)


class AgentMemoryCreateRequest(BaseModel):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    category: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=2000)
    source: Literal["manual", "conversation"] = "manual"
    enabled: bool = True


class AgentMemoryUpdateRequest(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    category: str | None = Field(default=None, min_length=1, max_length=32)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None


def _memory_dict(row: AgentMemory) -> dict:
    return {
        "id": row.id,
        "key": row.key,
        "category": row.category,
        "content": row.content,
        "source": row.source,
        "enabled": row.enabled,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# 长期记忆是管理员维护的全局规则，任何角色都只经模型间接消费，不开放读写。
@router.get("/memories", dependencies=[AdminOnly])
def list_agent_memories(
    session: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    rows = session.scalars(
        select(AgentMemory).order_by(AgentMemory.enabled.desc(), AgentMemory.updated_at.desc())
    )
    return [_memory_dict(row) for row in rows]


@router.post("/memories", status_code=201, dependencies=[AdminOnly])
def create_agent_memory(
    payload: AgentMemoryCreateRequest,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    key = payload.key.strip()
    if session.scalar(select(AgentMemory.id).where(AgentMemory.key == key)) is not None:
        raise HTTPException(status_code=409, detail="记忆标识已存在")
    try:
        category, content = validate_memory(payload.category, payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = AgentMemory(
        key=key,
        category=category,
        content=content,
        source=payload.source.strip() or "manual",
        enabled=payload.enabled,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="记忆标识已存在") from exc
    session.refresh(row)
    return _memory_dict(row)


@router.patch("/memories/{memory_id}", dependencies=[AdminOnly])
def update_agent_memory(
    memory_id: int,
    payload: AgentMemoryUpdateRequest,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    row = session.get(AgentMemory, memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="长期记忆不存在")
    next_key = payload.key.strip() if payload.key is not None else row.key
    duplicate = session.scalar(
        select(AgentMemory.id).where(AgentMemory.key == next_key, AgentMemory.id != row.id)
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="记忆标识已存在")
    try:
        category, content = validate_memory(
            payload.category if payload.category is not None else row.category,
            payload.content if payload.content is not None else row.content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.key = next_key
    row.category = category
    row.content = content
    if payload.enabled is not None:
        row.enabled = payload.enabled
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="记忆标识已存在") from exc
    session.refresh(row)
    return _memory_dict(row)


@router.delete("/memories/{memory_id}", status_code=204, dependencies=[AdminOnly])
def delete_agent_memory(
    memory_id: int,
    session: Annotated[Session, Depends(get_db)],
) -> None:
    row = session.get(AgentMemory, memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="长期记忆不存在")
    session.delete(row)
    session.commit()


@router.get("/status")
def ai_status(session: Annotated[Session, Depends(get_db)]) -> dict:
    """AI 能力可用性自检。

    存在的意义：不让用户「点了才知道不能用」。返回配置状态 + 最近一次调用结果，
    前端据此渲染「去配置 Key」的引导或「上次为什么失败」，而不是一句没头没尾的「请求失败」。
    结论由 ai_status_service 统一产出 —— 消息中心读的是同一份，两个页面不会各说一套。
    """
    return ai_capability_status(session)


@router.get("/usage", dependencies=[AdminOnly])
def ai_usage(session: Annotated[Session, Depends(get_db)]) -> dict:
    """AI 用量概览：调用次数、成功/失败、累计 token。

    刻意**不做金额换算** —— 厂商单价随时会调整，今天写死在代码里的单价，
    明天算出来的钱就是错的。这里只提供可核对的账本，届时乘官方当期单价即可。
    """
    total_row = session.execute(
        select(
            func.count(AIAnalysis.id),
            func.coalesce(func.sum(AIAnalysis.prompt_tokens), 0),
            func.coalesce(func.sum(AIAnalysis.completion_tokens), 0),
        )
    ).one()
    status_rows = session.execute(
        select(AIAnalysis.status, func.count(AIAnalysis.id)).group_by(AIAnalysis.status)
    ).all()
    type_rows = session.execute(
        select(
            AIAnalysis.analysis_type,
            func.count(AIAnalysis.id),
            func.coalesce(func.sum(AIAnalysis.prompt_tokens), 0),
            func.coalesce(func.sum(AIAnalysis.completion_tokens), 0),
        ).group_by(AIAnalysis.analysis_type)
    ).all()

    total_calls = int(total_row[0])
    prompt_tokens = int(total_row[1])
    completion_tokens = int(total_row[2])
    status_counts = {str(row[0]): int(row[1]) for row in status_rows}
    return {
        "total_calls": total_calls,
        "success": status_counts.get(AIAnalysisStatus.SUCCESS, 0),
        "failed": status_counts.get(AIAnalysisStatus.FAILED, 0),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "by_type": [
            {
                "analysis_type": str(row[0]),
                "calls": int(row[1]),
                "prompt_tokens": int(row[2]),
                "completion_tokens": int(row[3]),
            }
            for row in type_rows
        ],
    }


@router.post("/search")
def search(
    payload: SearchRequest,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[object, CurrentActor],
) -> dict:
    """自然语言检索周报，返回答案 + 可点开的引用来源。

    检索范围按会话数据范围收口（dept_admin 只能命中管辖部门的周报）；
    聊天记录归属当前登录人。
    """
    try:
        return search_reports(
            session,
            question=payload.question,
            conversation_id=payload.conversation_id,
            scope=payload.scope,
            start=payload.start,
            end=payload.end,
            department_id=payload.department_id,
            employee_id=payload.employee_id,
            keyword=payload.keyword,
            owner_employee_id=getattr(actor, "employee_id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _agent_kwargs(payload: AgentRequest, owner_employee_id: int | None) -> dict:
    return {
        "question": payload.question,
        "conversation_id": payload.conversation_id,
        "scope": payload.scope,
        "start": payload.start,
        "end": payload.end,
        "department_id": payload.department_id,
        "employee_id": payload.employee_id,
        "keyword": payload.keyword,
        "max_steps": payload.max_steps or MAX_STEPS_DEFAULT,
        "surface": payload.surface,
        "dashboard_view": payload.dashboard_view,
        "owner_employee_id": owner_employee_id,
    }


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _log_agent_stream_failure(
    session: Session,
    payload: AgentRequest,
    owner_employee_id: int | None,
    error: Exception,
) -> int | None:
    """记录流开始前的失败；客户端只收到通用错误。"""
    try:
        session.rollback()
        try:
            access = capture_access_scope(
                session,
                department_id=payload.department_id,
                employee_id=payload.employee_id,
            )
        except Exception:
            access = scope_snapshot(session)
        row = AIAnalysis(
            entity_type="search_query",
            entity_id=0,
            analysis_type=AGENT_ANALYSIS_TYPE,
            conversation_id=payload.conversation_id,
            owner_employee_id=owner_employee_id,
            prompt_version=AGENT_PROMPT_VERSION,
            result_json={
                "question": payload.question,
                "conversation_id": payload.conversation_id,
                "department_id": payload.department_id,
                "employee_id": payload.employee_id,
                "scope": payload.scope,
                "period": {"start": payload.start, "end": payload.end},
                "access_scope": access,
            },
            status=AIAnalysisStatus.FAILED,
            error_message=str(error)[:2000],
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        session.rollback()
        from app.core.logging import get_logger

        get_logger(__name__).exception("Agent 流式失败审计写入失败")
        return None


def _open_stream_session() -> Session:
    """单独开一个会话给 SSE。

    刻意不复用请求级 Depends(get_db)：SSE 的生成器是在响应阶段才被消费的，
    而请求级依赖的退出时机与它并不重合，直接复用会出现「流到一半 session closed」。
    """
    return get_session_factory()()


@router.post("/agent")
def agent_once(
    payload: AgentRequest,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[object, CurrentActor],
) -> dict:
    """同步跑完一轮 Agent（非流式）。前端拿流式，这个入口留给脚本与测试。"""
    try:
        return run_agent(
            session, **_agent_kwargs(payload, getattr(actor, "employee_id", None))
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/agent/stream")
def agent_stream(
    payload: AgentRequest,
    request: Request,
    actor: Annotated[object, CurrentActor],
) -> StreamingResponse:
    """流式跑一轮 Agent：检索步骤与答案增量都实时下发（SSE）。

    SSE 使用独立 DB 会话（见 _open_stream_session），必须把当前会话的
    数据范围与聊天归属显式带给它，否则范围过滤与隔离会在这条链路上失效。
    """

    def event_stream() -> Iterator[str]:
        session = _open_stream_session()
        try:
            # 与 get_db 同一约定：把中间件复核后的角色/身份映射为数据范围
            from app.services.data_scope import attach_session_scope

            attach_session_scope(session, request)
            for event in stream_agent(
                session, **_agent_kwargs(payload, getattr(actor, "employee_id", None))
            ):
                yield _sse(event)
        except ValueError as exc:
            analysis_id = _log_agent_stream_failure(
                session, payload, getattr(actor, "employee_id", None), exc
            )
            yield _sse({"type": "error", "message": "本次问答未能完成，请检查查询范围后重试。", "analysis_id": analysis_id})
        except Exception as exc:  # 流已经开始，只能用事件体面收尾，不能再改状态码
            from app.core.logging import get_logger

            get_logger(__name__).exception("Agent 流式请求失败")
            analysis_id = _log_agent_stream_failure(
                session, payload, getattr(actor, "employee_id", None), exc
            )
            yield _sse({"type": "error", "message": "本次问答未能完成，请稍后重试；如持续出现请联系管理员。", "analysis_id": analysis_id})
        finally:
            session.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 让反向代理别缓冲，否则前端会一次性收到全部内容，失去流式意义
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/duplicate/check")
def enqueue_analysis(
    payload: dict, session: Annotated[Session, Depends(get_db)]
) -> dict:
    """兼容入口：旧的「检测重复」改为把周报加入统一分析队列。

    统一分析一次调用即产出重复/质量/事项/评分，因此不再单独跑一次重复模型。
    """
    report_id = payload.get("report_id")
    report = session.get(WeeklyReport, report_id)
    if not report_in_scope(session, report):
        raise HTTPException(status_code=404, detail="周报不存在")
    try:
        if enqueue_report(session, report, force=False) is None:
            raise ValueError("免提交人员不参与 AI 分析；取消免提交后可重新扫描")
        session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"report_id": report_id, "status": "queued"}


@router.get("/coverage")
def coverage(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    department_id: int | None = Query(default=None),
    employee_id: int | None = Query(default=None),
) -> dict:
    """本周期分析覆盖率（轻量只读）。

    供消费分析结果的页面（经营看板/部门/员工/洞察）的补分析按钮显示与
    轮询；口径与洞察/看板共用 ``analysis_coverage``，传 ``department_id``
    按部门统计，传 ``employee_id`` 按员工统计。``queue`` 是区间内分析
    队列的按状态计数，前端据此区分「分析中（不可再点）」与「可点击补
    分析（含失败重试）」。
    """
    start_date, end_date = resolve_period(start, end)
    payload = analysis_coverage(
        session,
        start_date,
        end_date,
        department_id=department_id,
        employee_id=employee_id,
    )
    payload["period"] = {"start": start_date.isoformat(), "end": end_date.isoformat()}
    payload["remaining"] = max(
        0, payload["reports_total"] - payload["analyzed_reports"]
    )
    queue_conditions = [
        *report_period_overlap_conditions(start_date, end_date),
        non_exempt_report_condition(session),
    ]
    if department_id is not None:
        queue_conditions.append(Employee.department_id == department_id)
    if employee_id is not None:
        queue_conditions.append(WeeklyReport.employee_id == employee_id)
    queue_counts = {"pending": 0, "running": 0, "failed": 0}
    for status, count in session.execute(
        select(AnalysisQueue.status, func.count())
        .join(WeeklyReport, WeeklyReport.id == AnalysisQueue.report_id)
        .join(Employee, Employee.id == WeeklyReport.employee_id, isouter=True)
        .where(*queue_conditions)
        .group_by(AnalysisQueue.status)
    ).all():
        key = status.value if hasattr(status, "value") else str(status)
        if key in queue_counts:
            queue_counts[key] = int(count)
    payload["queue"] = queue_counts
    return payload


@router.post("/scan", dependencies=[AdminOnly])
def scan(payload: ScanRequest, session: Annotated[Session, Depends(get_db)]) -> dict:
    """一键批量分析：把周期内周报加入后台 Worker 队列，立即返回。

    单次排队上限为 ``ai_scan_max_reports``：每个排队项都是一次真实模型调用，
    不限量的话点一下就能把整月预算烧穿。超限时返回 ``remaining``，
    让用户知道本轮只排了一部分，而不是以为全部排上了。
    """
    start_date, end_date = resolve_period(payload.start, payload.end)
    rows, remaining = enqueue_period_reports_with_remaining(
        session,
        start_date,
        end_date,
        department_id=payload.department_id,
        employee_id=payload.employee_id,
        force=payload.force,
    )
    resource_backfill = backfill_resource_demands(session, start_date, end_date)
    session.commit()
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "queued": status_counts.get(AnalysisQueueStatus.PENDING, 0),
        "running": status_counts.get(AnalysisQueueStatus.RUNNING, 0),
        "already_done": status_counts.get(AnalysisQueueStatus.SUCCESS, 0),
        "failed": status_counts.get(AnalysisQueueStatus.FAILED, 0),
        "force": payload.force,
        "total": len(rows),
        # 超限未排队的篇数：0 表示本轮已覆盖区间内全部周报
        "remaining": remaining,
        "limited": remaining > 0,
        "resource_candidates": resource_backfill["matched"],
    }


@router.post("/queue/backfill", dependencies=[AdminOnly])
def backfill_queue(
    payload: BackfillRequest, session: Annotated[Session, Depends(get_db)]
) -> dict:
    """补齐历史周报中缺失的队列行，不重置已有任务或触发模型调用。

    这是数据修复入口而不是「强制重跑」入口；需要重跑时仍使用 ``/scan`` 的
    ``force=true``，避免用户误操作导致大量重复调用和费用。
    """
    start_date, end_date = resolve_period(payload.start, payload.end)
    rows = enqueue_missing_period_reports(
        session,
        start_date,
        end_date,
        department_id=payload.department_id,
    )
    resource_backfill = backfill_resource_demands(session, start_date, end_date)
    session.commit()
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "backfilled": len(rows),
        "queued": status_counts.get(AnalysisQueueStatus.PENDING, 0),
        "total": len(rows),
        "resource_candidates": resource_backfill["matched"],
    }


@router.post("/reports/{report_id}/analyze")
def analyze_report_route(
    report_id: int,
    session: Annotated[Session, Depends(get_db)],
    force: bool = Query(default=False),
) -> dict:
    """单篇周报加入后台统一分析队列。"""
    report = session.get(WeeklyReport, report_id)
    if not report_in_scope(session, report):
        raise HTTPException(status_code=404, detail="周报不存在")
    if enqueue_report(session, report, force=force) is None:
        raise HTTPException(
            status_code=400,
            detail="免提交人员不参与 AI 分析；取消免提交后可重新扫描",
        )
    session.commit()
    return {"report_id": report_id, "queue": queue_state(session, report_id)}


@router.get("/queue", dependencies=[AdminOnly])
def list_queue(
    session: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict:
    """后台分析队列：任务是否处理完、失败几次，页面据此显示状态。"""
    rows = session.execute(
        select(AnalysisQueue, WeeklyReport, Employee)
        .join(WeeklyReport, AnalysisQueue.report_id == WeeklyReport.id)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(non_exempt_report_condition(session))
        .order_by(AnalysisQueue.id.desc())
        .limit(limit)
    ).all()
    totals_rows = session.execute(
        select(AnalysisQueue.status, func.count(AnalysisQueue.id))
        .join(WeeklyReport, AnalysisQueue.report_id == WeeklyReport.id)
        .where(non_exempt_report_condition(session))
        .group_by(AnalysisQueue.status)
    ).all()
    totals = {str(status): int(count) for status, count in totals_rows}
    return {
        "totals": totals,
        "items": [
            {
                "id": queue.id,
                "report_id": queue.report_id,
                "employee_name": employee.name if employee else None,
                "title": report.title,
                "status": queue.status,
                "attempts": queue.attempts,
                "next_attempt_at": queue.next_attempt_at.isoformat()
                if queue.next_attempt_at
                else None,
                "last_error": queue.last_error,
            }
            for queue, report, employee in rows
        ],
    }


@router.get("/history")
def search_history(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[dict]:
    """最近若干次检索问答记录（便于回溯「之前问过什么」）。

    同时包含一次 RAG 检索（search）与 Agent 多轮检索（agent_search）：
    对用户来说它们都是「我上次问过的问题」，不该因为换了实现就从历史里消失。

    聊天按人严格隔离（含超管）：企微会话只看 ``owner_employee_id`` 等于
    本人员工 id 的会话；密码超管只看 owner 为 NULL 的会话（自己的 +
    迁移前的历史记录）。
    """
    from app.core.security import read_session_token, SESSION_COOKIE

    session_user = getattr(request.state, "session_user", None)
    if session_user is None:
        # 测试环境不挂中间件：退回直接解析 Cookie，仍保持隔离语义
        session_user = read_session_token(request.cookies.get(SESSION_COOKIE))
    owner_id = session_user.employee_id if session_user is not None else None

    visible = (
        AIAnalysis.analysis_type.in_([SEARCH_ANALYSIS_TYPE, AGENT_ANALYSIS_TYPE]),
        AIAnalysis.deleted_at.is_(None),
        chat_owner_condition(owner_id),
    )
    rows = list(session.scalars(select(AIAnalysis).where(*visible).order_by(AIAnalysis.id.desc())))
    grouped_rows: dict[str, list[AIAnalysis]] = {}
    unsafe_groups: set[str] = set()
    for row in rows:
        key = f"conversation:{row.conversation_id}" if row.conversation_id else f"legacy:{row.id}"
        grouped_rows.setdefault(key, []).append(row)
        if not history_row_visible(session, row):
            unsafe_groups.add(key)

    safe_groups = [
        (max(row.id for row in group), key, group)
        for key, group in grouped_rows.items()
        if key not in unsafe_groups
    ]
    safe_groups.sort(key=lambda item: item[0], reverse=True)
    recent = safe_groups[:limit]
    if not recent:
        return []

    analysis_ids = [row.id for _, _, group in recent for row in group[:100]]
    reviewer_key = f"employee:{owner_id}" if owner_id is not None else "admin"
    feedback_by_analysis = (
        {
            item.analysis_id: item
            for item in session.scalars(
                select(AgentFeedback).where(
                    AgentFeedback.analysis_id.in_(analysis_ids or [-1]),
                    AgentFeedback.reviewer_key == reviewer_key,
                )
            )
        }
        if _agent_feedback_table_available(session)
        else {}
    )

    def serialize(row: AIAnalysis) -> dict:
        data = row.result_json or {}
        return {
            "id": row.id,
            "analysis_id": row.id,
            "kind": row.analysis_type,
            "question": data.get("question"),
            "conversation_id": data.get("conversation_id"),
            "department_id": data.get("department_id"),
            "employee_id": data.get("employee_id"),
            "employee_name": data.get("employee_name"),
            "answer": data.get("answer"),
            "not_found": data.get("not_found"),
            "scope": data.get("scope"),
            "period": data.get("period"),
            "candidate_ids": data.get("candidate_ids") or [],
            "matched_total": data.get("matched_total"),
            "read_count": data.get("read_count"),
            "full_context": data.get("full_context"),
            "batch_read": data.get("batch_read"),
            "truncated_report_ids": data.get("truncated_report_ids") or [],
            "truncated": bool(data.get("truncated", False)),
            "fallback": bool(data.get("fallback", False)),
            "steps": data.get("steps") or [],
            "escalations": data.get("escalations") or [],
            "sources": data.get("sources") or [],
            "valid_citation_ids": data.get("valid_citation_ids") or [],
            "resolved_scope": data.get("resolved_scope"),
            "keyword_coverage": data.get("keyword_coverage"),
            "coverage_status": data.get("coverage_status"),
            "coverage_gaps": data.get("coverage_gaps") or [],
            "usage": data.get("usage"),
            "evidence_type": data.get("evidence_type"),
            "feedback": (
                {
                    "category": feedback_by_analysis[row.id].category,
                    "reason": feedback_by_analysis[row.id].reason,
                    "created_at": feedback_by_analysis[row.id].created_at.isoformat()
                    if feedback_by_analysis[row.id].created_at
                    else None,
                }
                if row.id in feedback_by_analysis
                else None
            ),
            "prompt_version": row.prompt_version,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "model": row.model_name,
            "status": row.status,
        }

    items = []
    for _, key, group in recent:
        recent_rows = sorted(group, key=lambda row: row.id, reverse=True)[:100]
        turns = [serialize(row) for row in reversed(recent_rows)]
        if not turns:
            continue
        latest = {**turns[-1]}
        latest["question"] = turns[0].get("question")
        latest["turns"] = turns
        items.append(latest)
    return items


@router.patch("/history/{history_id}")
def update_history_visibility(
    history_id: int,
    payload: HistoryVisibilityRequest,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """软删除或恢复整段聊天会话；不删除 AI 审计与 token 记录。

    只能操作自己的会话：别人的 history_id 一律 404。
    """
    from app.core.security import read_session_token, SESSION_COOKIE

    session_user = getattr(request.state, "session_user", None)
    if session_user is None:
        session_user = read_session_token(request.cookies.get(SESSION_COOKIE))
    owner_id = session_user.employee_id if session_user is not None else None

    row = session.get(AIAnalysis, history_id)
    if (
        row is None
        or row.analysis_type not in {SEARCH_ANALYSIS_TYPE, AGENT_ANALYSIS_TYPE}
        or not (
            row.owner_employee_id == owner_id
            if owner_id is not None
            else row.owner_employee_id is None
        )
    ):
        raise HTTPException(status_code=404, detail="聊天记录不存在")
    if not history_row_visible(session, row):
        raise HTTPException(status_code=404, detail="聊天记录不存在")
    conversation_id = row.conversation_id
    condition = [
        AIAnalysis.analysis_type.in_([SEARCH_ANALYSIS_TYPE, AGENT_ANALYSIS_TYPE]),
        chat_owner_condition(owner_id),
    ]
    if conversation_id:
        condition.append(AIAnalysis.conversation_id == conversation_id)
    else:
        condition.append(AIAnalysis.id == row.id)
    conversation_rows = list(session.scalars(select(AIAnalysis).where(*condition)))
    if not conversation_rows or any(not history_row_visible(session, item) for item in conversation_rows):
        raise HTTPException(status_code=404, detail="聊天记录不存在")
    deleted_at = utcnow() if payload.deleted else None
    result = session.execute(
        update(AIAnalysis)
        .where(*condition)
        .values(deleted_at=deleted_at)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return {"id": row.id, "deleted": payload.deleted, "affected": result.rowcount}


def _feedback_reviewer_key(owner_id: int | None) -> str:
    return f"employee:{owner_id}" if owner_id is not None else "admin"


def _owned_history_row(
    history_id: int, request: Request, session: Session
) -> tuple[AIAnalysis, str]:
    from app.core.security import read_session_token, SESSION_COOKIE

    session_user = getattr(request.state, "session_user", None)
    if session_user is None:
        session_user = read_session_token(request.cookies.get(SESSION_COOKIE))
    owner_id = session_user.employee_id if session_user is not None else None
    row = session.get(AIAnalysis, history_id)
    if (
        row is None
        or row.analysis_type not in {SEARCH_ANALYSIS_TYPE, AGENT_ANALYSIS_TYPE}
        or row.status != AIAnalysisStatus.SUCCESS
        or row.deleted_at is not None
        or not (
            row.owner_employee_id == owner_id
            if owner_id is not None
            else row.owner_employee_id is None
        )
        or not history_row_visible(session, row)
    ):
        raise HTTPException(status_code=404, detail="聊天记录不存在")
    if not _agent_feedback_table_available(session):
        raise HTTPException(
            status_code=503,
            detail="反馈功能尚未启用，请联系管理员完成反馈表迁移。",
        )
    return row, _feedback_reviewer_key(owner_id)


@router.get("/history/{history_id}/feedback")
def get_agent_feedback(
    history_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> dict | None:
    """读取本人对本人答案的反馈。"""
    _, reviewer_key = _owned_history_row(history_id, request, session)
    feedback = session.scalar(
        select(AgentFeedback).where(
            AgentFeedback.analysis_id == history_id,
            AgentFeedback.reviewer_key == reviewer_key,
        )
    )
    if feedback is None:
        return None
    return {
        "category": feedback.category,
        "reason": feedback.reason,
        "created_at": feedback.created_at.isoformat() if feedback.created_at else None,
    }


@router.post("/history/{history_id}/feedback")
def submit_agent_feedback(
    history_id: int,
    payload: AgentFeedbackRequest,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """提交或更新本人反馈；只引用审计 ID，不重复存答案或周报。"""
    _, reviewer_key = _owned_history_row(history_id, request, session)
    feedback = session.scalar(
        select(AgentFeedback).where(
            AgentFeedback.analysis_id == history_id,
            AgentFeedback.reviewer_key == reviewer_key,
        )
    )
    if feedback is None:
        feedback = AgentFeedback(
            analysis_id=history_id,
            reviewer_key=reviewer_key,
            category=payload.category,
            reason=payload.reason.strip() if payload.reason else None,
        )
        session.add(feedback)
    else:
        feedback.category = payload.category
        feedback.reason = payload.reason.strip() if payload.reason else None
    session.commit()
    return {
        "analysis_id": history_id,
        "category": feedback.category,
        "reason": feedback.reason,
        "created_at": feedback.created_at.isoformat() if feedback.created_at else None,
    }
