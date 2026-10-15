from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import false, func, select, true
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.weekly_report import WeeklyReport
from app.services.view_service import (
    department_name_map,
    parse_date,
    reportable_employee_conditions,
    serialize_employee,
    serialize_report,
    submitted_employee_ids,
)
from app.services.insight_service import employee_analysis
from app.services.data_scope import current_scope

router = APIRouter(prefix="/api/employees", tags=["employees"])


def _employee_in_scope(session: Session, employee: Employee | None) -> bool:
    """dept_admin 只能访问管辖部门子树内的员工档案。"""
    scope = current_scope(session)
    if scope.is_all:
        return True
    return (
        employee is not None
        and employee.department_id is not None
        and employee.department_id in scope.department_ids
    )


class EmployeeUpdate(BaseModel):
    report_exempt: bool


class EmployeeExemptionItem(BaseModel):
    id: int
    report_exempt: bool


class EmployeeExemptionUpdate(BaseModel):
    updates: list[EmployeeExemptionItem]


def _require_period(start: str | None, end: str | None) -> tuple[date, date]:
    """解析 start/end，非法直接 400（不做静默兜底，避免统计口径漂移）。"""
    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date is None or end_date is None:
        raise HTTPException(
            status_code=400,
            detail="submit_status 需要合法的 start / end（格式 YYYY-MM-DD）",
        )
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end 不得早于 start")
    return start_date, end_date


@router.get("")
def list_employees(
    session: Annotated[Session, Depends(get_db)],
    department_id: int | None = Query(default=None),
    status: str | None = Query(default=None, description="active/resigned/..."),
    keyword: str | None = Query(default=None, description="姓名/工号关键字"),
    report_exempt: bool | None = Query(
        default=None, description="true=只看免提交人员 / false=只看需提交人员"
    ),
    reportable: bool | None = Query(
        default=None,
        description="true=只看应交周报名单（在职且个人/部门均未免提交），与统计/催交同口径",
    ),
    start: str | None = Query(default=None, description="周期开始 YYYY-MM-DD，配合 submit_status 使用"),
    end: str | None = Query(default=None, description="周期结束 YYYY-MM-DD，配合 submit_status 使用"),
    submit_status: str | None = Query(
        default=None, description="submitted=本周期已提交 / unsubmitted=本周期未提交"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """员工列表。

    submit_status（配合 start/end）供首页「未提交名单」使用，统计口径与看板
    完全一致：周期重叠判定 + 未提交基线 = 在职员工。两条约定：

    - 传 submit_status 时**必须**同时传 start/end，否则 400（不静默取默认周）
    - 传 submit_status 且未显式传 status 时，只统计在职员工：
      离职员工不该出现在催交名单里
    - 传 submit_status 时**一律**排除免提交部门：免写周报的人不该被催
    """
    conditions = []
    if department_id is not None:
        conditions.append(Employee.department_id == department_id)
    if keyword:
        like = f"%{keyword}%"
        conditions.append(Employee.name.like(like))
    if report_exempt is not None:
        conditions.append(Employee.report_exempt.is_(report_exempt))

    period: tuple[date, date] | None = None
    submitted_ids: set[int] = set()
    if submit_status:
        if submit_status not in ("submitted", "unsubmitted"):
            raise HTTPException(
                status_code=400, detail="submit_status 只支持 submitted / unsubmitted"
            )
        period = _require_period(start, end)
        # 在职（或指定状态）+ 排除免提交部门
        conditions.extend(reportable_employee_conditions(session, status=status))
        submitted_ids = submitted_employee_ids(session, period[0], period[1])
        if submit_status == "submitted":
            # 空集合单独处理，避免 SQLAlchemy 生成空 IN 表达式
            conditions.append(
                Employee.id.in_(submitted_ids) if submitted_ids else false()
            )
        else:
            conditions.append(
                Employee.id.notin_(submitted_ids) if submitted_ids else true()
            )
    elif reportable:
        # 应交周报名单：复用统计/催交的同一口径函数，保证「名单」和
        # 「未提交统计」永远不会出现两套人数。名单闭环：
        # 企微同步入职自动进名单、离职自动出名单，免提交开关做人工豁免。
        conditions.extend(reportable_employee_conditions(session, status=status))
    elif status:
        conditions.append(Employee.status == status)

    # dept_admin：通讯录只出管辖部门子树内的员工
    scope = current_scope(session)
    if scope.is_scoped:
        conditions.append(scope.employee_condition())

    total = session.scalar(
        select(func.count()).select_from(Employee).where(*conditions)
    )

    # 最近一次提交时间 + 累计篇数（一次取回本页员工）
    last_report_subq = (
        select(
            WeeklyReport.employee_id,
            func.count(WeeklyReport.id).label("reports_total"),
            func.max(WeeklyReport.submitted_at).label("last_submitted_at"),
        )
        .group_by(WeeklyReport.employee_id)
        .subquery()
    )

    rows = session.execute(
        select(
            Employee,
            last_report_subq.c.last_submitted_at,
            last_report_subq.c.reports_total,
        )
        .outerjoin(last_report_subq, last_report_subq.c.employee_id == Employee.id)
        .where(*conditions)
        .order_by(Employee.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    dept_names = department_name_map(session)
    items = []
    for employee, last_submitted_at, reports_total in rows:
        item = serialize_employee(employee, dept_names.get(employee.department_id))
        item["last_report_at"] = (
            last_submitted_at.isoformat() if last_submitted_at else None
        )
        item["reports_total"] = int(reports_total or 0)
        if period is not None:
            item["has_submitted"] = employee.id in submitted_ids
        items.append(item)

    return {
        "items": items,
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
    }


@router.get("/{employee_id}/reports")
def list_employee_reports(
    employee_id: int,
    session: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict]:
    employee = session.get(Employee, employee_id)
    if employee is None or not _employee_in_scope(session, employee):
        raise HTTPException(status_code=404, detail="员工不存在")
    reports = session.scalars(
        select(WeeklyReport)
        .where(WeeklyReport.employee_id == employee_id)
        .order_by(WeeklyReport.submitted_at.desc())
        .limit(limit)
    ).all()
    dept_names = department_name_map(session)
    return [
        serialize_report(r, employee, dept_names.get(employee.department_id))
        for r in reports
    ]


@router.get("/{employee_id}/analysis")
def get_employee_analysis(
    employee_id: int,
    session: Annotated[Session, Depends(get_db)],
    end: str | None = Query(default=None, description="截止日期 YYYY-MM-DD"),
    weeks: int = Query(default=8, ge=8, le=12),
) -> dict:
    employee = session.get(Employee, employee_id)
    if employee is None or not _employee_in_scope(session, employee):
        raise HTTPException(status_code=404, detail="员工不存在")
    # 复用周期解析以保证日期非法时统一返回 400；只使用截止日。
    _, end_date = _require_period("2000-01-01", end) if end else (None, date.today())
    return employee_analysis(session, employee_id, end_date, weeks)


@router.patch("/report-exemptions")
def update_report_exemptions(
    payload: EmployeeExemptionUpdate,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """批量更新个人免提交状态；设置页保存差异项，不做逐人请求。"""
    scope = current_scope(session)
    updated: list[Employee] = []
    for item in payload.updates:
        employee = session.get(Employee, item.id)
        if employee is None or not _employee_in_scope(session, employee):
            raise HTTPException(status_code=404, detail=f"员工不存在：{item.id}")
        employee.report_exempt = item.report_exempt
        updated.append(employee)
    session.commit()
    dept_names = department_name_map(session)
    return {
        "updated": len(updated),
        "items": [
            serialize_employee(employee, dept_names.get(employee.department_id))
            for employee in updated
        ],
    }


@router.patch("/{employee_id}")
def update_employee(
    employee_id: int,
    payload: EmployeeUpdate,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """更新员工的管理开关；当前只开放个人级免提交周报。"""
    employee = session.get(Employee, employee_id)
    if employee is None or not _employee_in_scope(session, employee):
        raise HTTPException(status_code=404, detail="员工不存在")
    employee.report_exempt = payload.report_exempt
    session.commit()
    dept = session.get(Department, employee.department_id) if employee.department_id else None
    return serialize_employee(employee, dept.name if dept else None)


@router.get("/{employee_id}")
def get_employee(
    employee_id: int,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    employee = session.get(Employee, employee_id)
    if employee is None or not _employee_in_scope(session, employee):
        raise HTTPException(status_code=404, detail="员工不存在")

    dept = session.get(Department, employee.department_id) if employee.department_id else None
    reports_count = session.scalar(
        select(func.count(WeeklyReport.id)).where(
            WeeklyReport.employee_id == employee_id
        )
    )

    return {
        **serialize_employee(employee, dept.name if dept else None),
        "reports_total": int(reports_count or 0),
    }
