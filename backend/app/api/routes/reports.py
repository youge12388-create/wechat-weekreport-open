from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.ai_analysis import AIAnalysis
from app.models.department import Department
from app.models.employee import Employee
from app.models.weekly_report import WeeklyReport
from app.models.report_item import ReportItem
from app.services.duplicate_service import latest_duplicate_flags
from app.services.data_scope import current_scope, report_in_scope
from app.services.quality_service import latest_quality_flags
from app.services.analysis_queue_service import queue_state
from app.services.report_analysis_service import (
    REPORT_ANALYSIS_TYPE,
    latest_unified_analysis,
    latest_unified_results,
)
from app.services.report_translation_service import translation_state, translate_report
from app.services.view_service import (
    department_name_map,
    report_period_overlap_conditions,
    resolve_period,
    serialize_report,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("")
def list_reports(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    all_history: bool = Query(default=False),
    department_id: int | None = Query(default=None),
    employee_id: int | None = Query(default=None),
    report_type: str | None = Query(default=None),
    q: str | None = Query(default=None, description="标题/内容关键字"),
    ai_flag: str | None = Query(
        default=None, description="AI 体检筛选：duplicate|thin|normal|unchecked"
    ),
    work_state: str | None = Query(
        default=None, description="结构化事项筛选：done|ongoing"
    ),
    content_language: str | None = Query(
        default=None, description="正文语言筛选：english"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """周报列表（需求 12 节），支持日期/部门/员工/类型/关键字筛选与分页。"""
    conditions = []
    if not all_history:
        start_date, end_date = resolve_period(start, end)
        conditions.extend(report_period_overlap_conditions(start_date, end_date))
    if department_id is not None:
        conditions.append(Employee.department_id == department_id)
    if employee_id is not None:
        conditions.append(WeeklyReport.employee_id == employee_id)
    if report_type:
        conditions.append(WeeklyReport.report_type == report_type)
    if q:
        like = f"%{q}%"
        conditions.append(
            or_(
                WeeklyReport.title.like(like),
                WeeklyReport.content.like(like),
            )
        )

    # AI 体检结论筛选。判定基准是体检回写到周报表的冗余字段，而不是去 join
    # ai_analyses —— 列表是高频接口，没必要让它每次都扛一遍分析明细表。
    if ai_flag is not None:
        if ai_flag == "duplicate":
            conditions.append(WeeklyReport.ai_duplicate_flag.is_(True))
        elif ai_flag == "thin":
            conditions.append(WeeklyReport.ai_quality_flag.is_(True))
        elif ai_flag == "normal":
            conditions.append(WeeklyReport.ai_checked_at.isnot(None))
            conditions.append(WeeklyReport.ai_duplicate_flag.is_(False))
            conditions.append(WeeklyReport.ai_quality_flag.is_(False))
        elif ai_flag == "unchecked":
            conditions.append(WeeklyReport.ai_checked_at.is_(None))
        else:
            raise HTTPException(
                status_code=400,
                detail="ai_flag 只支持 duplicate / thin / normal / unchecked",
            )

    if work_state is not None:
        if work_state == "done":
            item_types = ("completed_items", "results")
        elif work_state == "ongoing":
            item_types = ("ongoing_items", "next_week_plans")
        else:
            raise HTTPException(status_code=400, detail="work_state 只支持 done / ongoing")
        # 先按事项表筛出周报 ID，再查询周报主体，避免多事项造成列表重复。
        conditions.append(
            WeeklyReport.id.in_(
                select(ReportItem.report_id).where(ReportItem.item_type.in_(item_types))
            )
        )

    if content_language is not None:
        if content_language != "english":
            raise HTTPException(status_code=400, detail="content_language 目前只支持 english")
        conditions.append(WeeklyReport.content_language == "english")

    # dept_admin：只看管辖部门子树的周报（查询已 join Employee，直接按部门过滤）
    scope = current_scope(session)
    if scope.is_scoped:
        conditions.append(scope.joined_report_condition())

    base_query = select(WeeklyReport).join(
        Employee, WeeklyReport.employee_id == Employee.id, isouter=True
    )
    where_clause = conditions

    total = session.scalar(
        select(func.count()).select_from(WeeklyReport).join(
            Employee, WeeklyReport.employee_id == Employee.id, isouter=True
        ).where(*where_clause)
    )

    reports = session.scalars(
        base_query.where(*where_clause)
        .order_by(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    dept_names = department_name_map(session)
    report_ids = [r.id for r in reports]
    employee_ids = {r.employee_id for r in reports if r.employee_id}

    employees: dict[int, Employee] = {}
    if employee_ids:
        employees = {
            emp.id: emp
            for emp in session.scalars(
                select(Employee).where(Employee.id.in_(employee_ids))
            )
        }

    # 每个周报的工作事项数量 / AI 处理状态
    wi_map: dict[int, int] = {}
    if report_ids:
        wi_rows = session.execute(
            select(ReportItem.report_id, func.count(ReportItem.id))
            .where(ReportItem.report_id.in_(report_ids))
            .group_by(ReportItem.report_id)
        )
        wi_map = {sid: int(c) for sid, c in wi_rows}

    ai_map: dict[int, str] = {}
    if report_ids:
        ai_rows = session.execute(
            select(AIAnalysis.entity_id, AIAnalysis.analysis_type, AIAnalysis.status)
            .where(
                AIAnalysis.entity_type == "weekly_report",
                AIAnalysis.entity_id.in_(report_ids),
            )
        )
        for entity_id, analysis_type, status in ai_rows:
            if entity_id not in ai_map and analysis_type == "extract_work_items":
                ai_map[entity_id] = "processed" if status == "success" else "failed"
            elif entity_id not in ai_map and analysis_type == REPORT_ANALYSIS_TYPE:
                ai_map[entity_id] = "processed" if status == "success" else "failed"

    dup_flags = latest_duplicate_flags(session, report_ids)
    quality_flags = latest_quality_flags(session, report_ids)
    unified = latest_unified_results(session, report_ids)

    items = []
    for report in reports:
        employee = employees.get(report.employee_id)
        dept_name = (
            dept_names.get(employee.department_id) if employee else None
        )
        if report.id in unified and report.ai_checked_at is not None:
            unified_result = unified[report.id]["result"]
            checked_at = unified[report.id]["checked_at"]
            dup_flags[report.id] = {
                "is_duplicate": bool(unified_result.get("is_duplicate")),
                "score": int(unified_result.get("duplicate_score") or 0),
                "reason": unified_result.get("duplicate_reason"),
                "checked_at": checked_at,
            }
            quality_flags[report.id] = {
                "is_thin": bool(unified_result.get("is_thin")),
                "score": int(unified_result.get("score") or 0),
                "reason": unified_result.get("thin_reason"),
                "checked_at": checked_at,
            }
        elif report.ai_checked_at is None:
            # 内容已变化但尚未重跑：旧分析结论不算数，宁可显示“未检测”
            dup_flags.pop(report.id, None)
            quality_flags.pop(report.id, None)
        item = serialize_report(report, employee, dept_name)
        item.update(
            {
                "work_item_count": wi_map.get(report.id, 0),
                "ai_status": ai_map.get(report.id, "not_processed"),
                # None = 还没检测过（不要显示成「正常」，那是骗人）
                "duplicate": dup_flags.get(report.id),
                "quality": quality_flags.get(report.id),
            }
        )
        items.append(item)

    return {
        "items": items,
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
        "period": None if all_history else {"start": start_date.isoformat(), "end": end_date.isoformat()},
    }


@router.get("/{report_id}")
def get_report(report_id: int, session: Annotated[Session, Depends(get_db)]) -> dict:
    """周报详情（需求 13 节）：原始数据与 AI 数据明确分离展示。"""
    report = session.get(WeeklyReport, report_id)
    if not report_in_scope(session, report):
        # 范围外按 404 处理：不向越权者泄露「这篇周报存在」
        raise HTTPException(status_code=404, detail="周报不存在")

    employee = session.get(Employee, report.employee_id) if report.employee_id else None
    dept_name = None
    if employee and employee.department_id:
        dept = session.get(Department, employee.department_id)
        dept_name = dept.name if dept else None

    work_item_count = session.scalar(
        select(func.count(ReportItem.id)).where(ReportItem.report_id == report.id)
    )
    analyses = session.scalars(
        select(AIAnalysis)
        .where(
            AIAnalysis.entity_type == "weekly_report",
            AIAnalysis.entity_id == report.id,
        )
        .order_by(AIAnalysis.id.desc())
    ).all()

    analysis = None
    unified_row = (
        latest_unified_analysis(session, report_id)
        if report.ai_checked_at is not None
        else None
    )
    if unified_row is not None:
        analysis = {
            "id": unified_row.id,
            "analysis_type": unified_row.analysis_type,
            "status": unified_row.status,
            "model_name": unified_row.model_name,
            "model_provider": unified_row.model_provider,
            "prompt_version": unified_row.prompt_version,
            "duration_ms": unified_row.duration_ms,
            "created_at": unified_row.created_at.isoformat()
            if unified_row.created_at
            else None,
            "result": unified_row.result_json or {},
        }

    unified_map = latest_unified_results(session, [report_id])

    latest_dup = latest_duplicate_flags(session, [report_id])
    latest_q = latest_quality_flags(session, [report_id])
    if report_id in unified_map and report.ai_checked_at is not None:
        unified_result = unified_map[report_id]["result"]
        checked_at = unified_map[report_id]["checked_at"]
        latest_dup[report_id] = {
            "is_duplicate": bool(unified_result.get("is_duplicate")),
            "score": int(unified_result.get("duplicate_score") or 0),
            "reason": unified_result.get("duplicate_reason"),
            "checked_at": checked_at,
        }
        latest_q[report_id] = {
            "is_thin": bool(unified_result.get("is_thin")),
            "score": int(unified_result.get("score") or 0),
            "reason": unified_result.get("thin_reason"),
            "checked_at": checked_at,
        }
    elif report.ai_checked_at is None:
        latest_dup.pop(report_id, None)
        latest_q.pop(report_id, None)

    return {
        **serialize_report(report, employee, dept_name),
        "duplicate": latest_dup.get(report_id),
        "quality": latest_q.get(report_id),
        "analysis": analysis,
        "analysis_queue": queue_state(session, report_id),
        "employee": (
            {
                "name": employee.name,
                "external_id": employee.external_id,
                "position": employee.position,
                "department_name": dept_name,
            }
            if employee
            else None
        ),
        "content": report.content,
        "content_raw": report.content_raw,
        "translation": translation_state(session, report),
        "work_item_count": int(work_item_count or 0),
        "ai_analyses": [
            {
                "analysis_type": a.analysis_type,
                "status": a.status,
                "model_provider": a.model_provider,
                "model_name": a.model_name,
                "prompt_version": a.prompt_version,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in analyses
        ],
    }


@router.post("/{report_id}/translation")
def translate_report_route(
    report_id: int,
    session: Annotated[Session, Depends(get_db)],
    force: bool = Query(default=False),
) -> dict:
    """按需翻译单篇周报；译文缓存，正文不覆盖。"""
    report = session.get(WeeklyReport, report_id)
    if not report_in_scope(session, report):
        raise HTTPException(status_code=404, detail="周报不存在")
    try:
        return translate_report(session, report_id, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
