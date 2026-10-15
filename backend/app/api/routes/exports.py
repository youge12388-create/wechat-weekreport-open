"""运营导出：未提交名单、周报列表 -> CSV（UTF-8 BOM，Excel 友好）。"""

from __future__ import annotations

import csv
from io import StringIO
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.employee import Employee
from app.models.report_item import ReportItem
from app.models.weekly_report import WeeklyReport
from app.services.view_service import (
    department_name_map,
    report_period_overlap_conditions,
    reportable_employee_conditions,
    resolve_period,
    submitted_employee_ids,
)

router = APIRouter(prefix="/api/exports", tags=["exports"])

# Excel/WPS 会把以这些字符开头的单元格当**公式**执行。导出的内容里
# 标题、姓名、岗位都来自外部数据（企微汇报正文与通讯录），任何人写一条
# `=HYPERLINK(...)` / DDE 形式的标题，就会被打开导出文件的人执行。
# 前置单引号是 Excel 认可的「强制文本」写法，肉眼几乎不可见。
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# 单次导出最多多少行：防止「导出全部」把整张表读进内存生成超大 CSV。
EXPORT_MAX_ROWS = 20000


def _csv_cell(value: object) -> str:
    """把单元格转成不会被 Excel 当公式执行的文本。"""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _csv_response(
    filename: str, header: list[str], rows: list[list[str]]
) -> Response:
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        # 统一在这里消毒：所有导出路径都经过本函数，不需要各调用点自己记得处理。
        writer.writerow([_csv_cell(cell) for cell in row])
    # utf-8-sig 会在文件头写入 BOM，Excel 才能正确识别中文，避免乱码
    content = buf.getvalue().encode("utf-8-sig")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/unsubmitted")
def export_unsubmitted(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> Response:
    """导出本周期未提交名单（在职应提交、本周期无周报的人）。"""
    start_date, end_date = resolve_period(start, end)
    submitted = submitted_employee_ids(session, start_date, end_date)
    conditions = list(reportable_employee_conditions(session))
    if submitted:
        conditions.append(Employee.id.notin_(submitted))
    employees = session.scalars(
        select(Employee).where(*conditions).order_by(Employee.id)
    ).all()
    dept_names = department_name_map(session)
    rows = [
        [
            e.name,
            e.external_id,
            dept_names.get(e.department_id) or "",
            e.position or "",
        ]
        for e in employees
    ]
    return _csv_response(
        f"unsubmitted_{start_date.isoformat()}_{end_date.isoformat()}.csv",
        ["姓名", "企微ID", "部门", "岗位"],
        rows,
    )


@router.get("/reports")
def export_reports(
    session: Annotated[Session, Depends(get_db)],
    all_history: bool = Query(default=False),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    department_id: int | None = Query(default=None),
    report_type: str | None = Query(default=None),
    work_state: str | None = Query(default=None),
    content_language: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> Response:
    """导出周报；all_history=true 时忽略页面筛选，导出全部已同步历史。"""
    start_date = end_date = None
    conditions = [WeeklyReport.report_type == "weekly"] if all_history else []
    if not all_history:
        start_date, end_date = resolve_period(start, end)
        conditions.extend(report_period_overlap_conditions(start_date, end_date))
    if not all_history and department_id is not None:
        conditions.append(Employee.department_id == department_id)
    if not all_history and report_type:
        conditions.append(WeeklyReport.report_type == report_type)
    if not all_history and work_state is not None:
        if work_state == "done":
            item_types = ("completed_items", "results")
        elif work_state == "ongoing":
            item_types = ("ongoing_items", "next_week_plans")
        else:
            raise HTTPException(status_code=400, detail="work_state 只支持 done / ongoing")
        conditions.append(
            WeeklyReport.id.in_(
                select(ReportItem.report_id).where(ReportItem.item_type.in_(item_types))
            )
        )
    if not all_history and q:
        # 关键字也是外部输入，限制长度避免超长 LIKE 打满数据库
        like = f"%{q[:200]}%"
        conditions.append(
            or_(WeeklyReport.title.like(like), WeeklyReport.content.like(like))
        )
    if not all_history and content_language is not None:
        if content_language != "english":
            raise HTTPException(status_code=400, detail="content_language 目前只支持 english")
        conditions.append(WeeklyReport.content_language == "english")
    # dept_admin：只导出管辖部门子树的周报（未归属员工的历史周报不含在内）
    from app.services.data_scope import current_scope

    scope = current_scope(session)
    if scope.is_scoped:
        conditions.append(scope.joined_report_condition())
    reports = session.scalars(
        select(WeeklyReport)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
        .order_by(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc())
        # 上限护栏：不加 limit 时，「导出全部」会把整张表读进内存并一次生成
        # 几百 MB 的 CSV（单 worker 部署下直接拖垮服务）。超过上限明确报错，不能静默少导。
        .limit(EXPORT_MAX_ROWS + 1)
    ).all()
    if len(reports) > EXPORT_MAX_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"历史周报超过 {EXPORT_MAX_ROWS} 篇，请联系管理员使用分批导出。",
        )
    dept_names = department_name_map(session)
    emp_ids = {r.employee_id for r in reports if r.employee_id}
    emp_map = {
        e.id: e
        for e in session.scalars(select(Employee).where(Employee.id.in_(emp_ids)))
    }
    rows = []
    for r in reports:
        emp = emp_map.get(r.employee_id) if r.employee_id else None
        rows.append(
            [
                str(r.id),
                emp.name if emp else "",
                dept_names.get(emp.department_id) if emp else "",
                r.report_type,
                r.title or "",
                r.start_date.isoformat() if r.start_date else "",
                r.end_date.isoformat() if r.end_date else "",
                r.submitted_at.isoformat() if r.submitted_at else "",
                r.source,
                r.content,
            ]
        )
    filename = (
        "weekly_reports_all_history.csv"
        if all_history
        else f"reports_{start_date.isoformat()}_{end_date.isoformat()}.csv"
    )
    return _csv_response(
        filename,
        ["周报ID", "员工", "部门", "类型", "标题", "周期起", "周期止", "提交时间", "来源", "周报原文"],
        rows,
    )
