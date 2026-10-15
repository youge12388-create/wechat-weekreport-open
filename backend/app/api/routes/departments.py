from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.department import Department
from app.models.employee import Employee
from app.services.view_service import (
    department_employee_counts,
    exempt_department_ids,
    reportable_employee_conditions,
    resolve_period,
    serialize_department,
    submitted_employee_ids,
)
from app.services.insight_service import department_analysis
from app.services.data_scope import current_scope

router = APIRouter(prefix="/api/departments", tags=["departments"])


@router.get("")
def list_departments(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None, description="统计开始日期 YYYY-MM-DD"),
    end: str | None = Query(default=None, description="统计结束日期 YYYY-MM-DD"),
) -> list[dict]:
    """部门列表 + 各部门提交统计（需求 11.2 表格）。

    免提交部门不进基数（submitted/unsubmitted 恒为 0），但会带 exempt=true
    让前端标注，避免误以为「这个部门一个人都没交」。
    """
    start_date, end_date = resolve_period(start, end)
    exempt_ids = exempt_department_ids(session)
    departments = session.scalars(select(Department).order_by(Department.id)).all()
    # dept_admin：部门列表只出管辖子树（统计口径经公共条件函数自动收口）
    scope = current_scope(session)
    if scope.is_scoped:
        scoped = set(scope.department_ids)
        departments = [dept for dept in departments if dept.id in scoped]
    emp_counts = department_employee_counts(session, exclude_dept_ids=exempt_ids)
    submitted_ids = submitted_employee_ids(session, start_date, end_date)

    # 一次查出各部门「应提交」的员工，避免循环内查库
    dept_emps: dict[int, set[int]] = {d.id: set() for d in departments}
    for dept_id, emp_id in session.execute(
        select(Employee.department_id, Employee.id).where(
            *reportable_employee_conditions(session)
        )
    ):
        if dept_id in dept_emps:
            dept_emps[dept_id].add(emp_id)

    items = []
    for dept in departments:
        employee_count = emp_counts.get(dept.id, 0)
        submitted_count = len(submitted_ids & dept_emps.get(dept.id, set()))
        item = serialize_department(dept, employee_count, exempt=dept.id in exempt_ids)
        item.update(
            {
                "submitted": submitted_count,
                "unsubmitted": max(employee_count - submitted_count, 0),
                # AI 分析未上线，工作事项相关字段恒为 0，字段保留便于前端统一渲染
                "work_item_total": 0,
                "work_item_done": 0,
                "work_item_in_progress": 0,
            }
        )
        items.append(item)
    return items


@router.get("/{department_id}")
def get_department(
    department_id: int,
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    weeks: int = Query(default=1, ge=1, le=12),
) -> dict:
    """部门详情：基本信息 + 周期统计 + 在职员工（含是否已提交）。"""
    from sqlalchemy import distinct, func

    from app.models.employee import Employee
    from app.models.enums import EmployeeStatus
    from app.models.weekly_report import WeeklyReport
    from app.services.view_service import (
        active_employees_by_department,
        report_period_overlap_conditions,
        serialize_employee,
        submitted_employee_ids,
    )

    department = session.get(Department, department_id)
    scope = current_scope(session)
    if (
        department is None
        or (scope.is_scoped and department.id not in set(scope.department_ids))
    ):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="部门不存在")

    start_date, end_date = resolve_period(start, end)
    exempt = department_id in exempt_department_ids(session)
    emp_by_dept = active_employees_by_department(
        session, exclude_dept_ids=exempt_department_ids(session)
    )
    # 免提交部门不进统计基数：显示 0 而不是把全员记成「没交」
    emp_ids = [] if exempt else emp_by_dept.get(department_id, [])

    submitted_ids = submitted_employee_ids(session, start_date, end_date)
    eff_end = func.coalesce(WeeklyReport.end_date, WeeklyReport.start_date)
    reports_in_period = session.scalar(
        select(func.count(distinct(WeeklyReport.id))).where(
            WeeklyReport.employee_id.in_(emp_ids) if emp_ids else False,
            *report_period_overlap_conditions(start_date, end_date),
        )
    ) if emp_ids else 0

    employees = session.scalars(
        select(Employee).where(
            Employee.department_id == department_id,
            Employee.status == EmployeeStatus.ACTIVE,
        ).order_by(Employee.id)
    )

    analysis = department_analysis(session, department_id, end_date, weeks)
    return {
        **serialize_department(department, len(emp_ids), exempt=exempt),
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "stats": {
            "employee_total": len(emp_ids),
            "submitted": len(submitted_ids & set(emp_ids)),
            "unsubmitted": len(emp_ids) - len(submitted_ids & set(emp_ids)),
            "reports_count": int(reports_in_period or 0),
        },
        "employees": [
            {
                **serialize_employee(emp),
                "has_submitted": emp.id in submitted_ids,
            }
            for emp in employees
        ],
        "analysis": analysis,
    }
