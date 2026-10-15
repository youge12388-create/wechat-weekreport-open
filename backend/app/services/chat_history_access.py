"""当前权限下读取 Agent / RAG 聊天审计的公共规则。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.services.data_scope import DataScope, current_scope


def capture_access_scope(
    session: Session,
    *,
    department_id: int | None = None,
    employee_id: int | None = None,
) -> dict[str, Any]:
    """记录生成本轮回答时实际允许读取的数据部门范围。"""
    scope = current_scope(session)
    if department_id is not None and scope.is_scoped and department_id not in scope.department_ids:
        raise ValueError("指定部门不存在或无权访问")

    selected_employee: Employee | None = None
    if employee_id is not None:
        selected_employee = session.get(Employee, employee_id)
        if (
            selected_employee is None
            or selected_employee.department_id is None
            or (scope.is_scoped and selected_employee.department_id not in scope.department_ids)
        ):
            raise ValueError("指定人员不存在或无权访问")
        if department_id is not None and selected_employee.department_id != department_id:
            raise ValueError("指定人员与部门范围不一致")

    if employee_id is not None:
        return {
            "kind": "employee",
            "employee_id": employee_id,
            "department_id": selected_employee.department_id if selected_employee else None,
        }
    if department_id is not None:
        return {"kind": "departments", "department_ids": [department_id]}
    if scope.is_all:
        return {"kind": "all"}
    return {"kind": "departments", "department_ids": list(scope.department_ids)}


def history_row_visible(session: Session, row: Any) -> bool:
    """只在当前权限覆盖生成时范围时回放或显示聊天审计。"""
    scope = current_scope(session)
    if scope.is_all:
        return True

    data = row.result_json if isinstance(row.result_json, dict) else {}
    snapshot = data.get("access_scope")
    if not isinstance(snapshot, dict):
        # 旧记录没有权限快照。仅当旧载荷明确标出部门或人员且能由当前范围证明安全时显示。
        department_id = data.get("department_id")
        employee_id = data.get("employee_id")
        if isinstance(department_id, int):
            return department_id in scope.department_ids
        if isinstance(employee_id, int):
            employee = session.get(Employee, employee_id)
            return bool(employee and employee.department_id in scope.department_ids)
        return False

    kind = snapshot.get("kind")
    if kind == "all":
        return False
    if kind == "departments":
        department_ids = snapshot.get("department_ids")
        return (
            isinstance(department_ids, list)
            and bool(department_ids)
            and all(isinstance(item, int) and item in scope.department_ids for item in department_ids)
        )
    if kind == "employee":
        employee_id = snapshot.get("employee_id")
        department_id = snapshot.get("department_id")
        if not isinstance(employee_id, int) or not isinstance(department_id, int):
            return False
        employee = session.get(Employee, employee_id)
        return bool(
            employee
            and employee.department_id == department_id
            and department_id in scope.department_ids
        )
    return False


def scope_snapshot(session: Session) -> dict[str, Any]:
    """供审计与评测使用的当前账号数据范围快照。"""
    scope: DataScope = current_scope(session)
    if scope.is_all:
        return {"kind": "all"}
    return {"kind": "departments", "department_ids": list(scope.department_ids)}
