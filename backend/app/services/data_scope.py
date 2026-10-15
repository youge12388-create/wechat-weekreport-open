"""请求数据范围（DataScope）：dept_admin 只能访问其管辖部门子树的数据。

## 设计

角色三档（见 ``models/enums.LoginRole``）：admin 全公司、member 仅自己的事项、
dept_admin 限定部门子树。本模块只解决 dept_admin 的**数据范围**问题：

1. **范围解析**：沿用授权模型的「就近判定」——沿部门链找最近的显式设置，
   命中 ``login_role=dept_admin`` 的部门即为其管辖范围（含全部下级子部门）；
   个人级勾选（``employee.login_enabled`` + ``dept_admin``）时管辖自己所在部门
   的子树（跨部门领导场景）。解析结果每请求一次，随授权变更立即生效
   （与 AuthMiddleware 的回源复核共用一次部门链回溯语义）。

2. **注入方式**：``deps.get_db`` 把解析出的 DataScope 挂到 ``session.info``，
   ``view_service`` 的公共条件函数（``reportable_employee_conditions`` /
   ``non_exempt_report_condition`` / ``submitted_employee_ids`` 等）读取后自动
   叠加过滤 —— 看板、洞察、导出、AI 检索（含 Agent 工具）都复用这批函数，
   因此一处注入、全链路生效。直接写 ``select(WeeklyReport)`` 的路由需要
   显式叠加本模块的条件函数。

3. **失败安全**：任何拿不到会话/角色/员工的场景一律按全公司范围处理
   （admin 与测试环境的默认值）；dept_admin 的范围解析结果为空集合时，
   该请求什么都看不到 —— 宁可空，不可漏。

注意：member 的范围不在这里处理（中间件已把他限制在 /api/my-actions），
数据范围过滤只对 dept_admin 有意义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.enums import LoginRole
from app.models.weekly_report import WeeklyReport
from app.services import access_service

# session.info 里存 DataScope 的键（session.info 是每 Session 的用户字典，
# 接口层每请求一个新 Session，正好是请求级作用域）。
SCOPE_INFO_KEY = "data_scope"


@dataclass(frozen=True)
class DataScope:
    """一次请求的数据范围。

    ``kind="all"`` 表示全公司（admin / 未识别场景的默认值）；
    ``kind="subtree"`` 表示仅限 ``department_ids`` 及其下属员工的业务数据。
    """

    kind: str
    department_ids: tuple[int, ...] = ()

    @property
    def is_all(self) -> bool:
        return self.kind == "all"

    @property
    def is_scoped(self) -> bool:
        return self.kind != "all"

    # ------------------------------------------------------------------ #
    # 条件构造：三种查询形态各给一个，调用方按查询是否 join Employee 选用。
    # ------------------------------------------------------------------ #
    def employee_condition(self) -> Any:
        """作用于 Employee 主查询的条件。"""
        if self.is_all:
            return true()
        return Employee.department_id.in_(self.department_ids)

    def joined_report_condition(self) -> Any:
        """作用于「已 join Employee 的周报查询」的条件。

        outer join 也适用：员工为 NULL 的历史周报 department_id 为 NULL，
        IN 判定为 NULL → 不入选，即子树范围**不含**未归属员工的历史周报
        （无法证明它属于管辖部门，宁可不展示）。
        """
        if self.is_all:
            return true()
        return Employee.department_id.in_(self.department_ids)

    def report_employee_id_condition(self) -> Any:
        """作用于「未 join Employee 的周报查询」的条件（子查询实现）。"""
        if self.is_all:
            return true()
        scoped = select(Employee.id).where(
            Employee.department_id.in_(self.department_ids)
        )
        return WeeklyReport.employee_id.in_(scoped)


# 全公司范围：admin、member（被中间件另行限制）、测试与未挂中间件环境的默认值。
ALL_SCOPE = DataScope(kind="all")


def current_scope(session: Session) -> DataScope:
    """读取请求级数据范围；未注入（测试直调、脚本）一律全公司。"""
    scope = session.info.get(SCOPE_INFO_KEY)
    return scope if isinstance(scope, DataScope) else ALL_SCOPE


def report_in_scope(session: Session, report: WeeklyReport | None) -> bool:
    """判断周报是否在当前请求范围内；未归属员工的周报对子树范围不可见。"""
    if report is None:
        return False
    scope = current_scope(session)
    if scope.is_all:
        return True
    if report.employee_id is None:
        return False
    department_id = session.scalar(
        select(Employee.department_id).where(Employee.id == report.employee_id)
    )
    return department_id is not None and department_id in scope.department_ids


def managed_department_ids(session: Session, employee: Employee | None) -> tuple[int, ...]:
    """解析 dept_admin 的管辖部门子树（含自身与全部下级）。

    与 ``access_service.decide_access`` 的就近判定完全同构：
    个人级勾选优先，其后沿部门链取最近的显式设置。角色不是 dept_admin
    或链上没有 dept_admin 授权时返回空 —— 调用方（get_db）只在角色复核
    结果为 dept_admin 时才调用本函数，返回空即「该账号当前无管辖范围」。
    """
    if employee is None:
        return ()

    def _subtree(root_id: int | None) -> tuple[int, ...]:
        if root_id is None:
            return ()
        return tuple(sorted(access_service.department_subtree_ids(session, root_id)))

    # 个人级勾选优先于部门链（与 decide_access 一致）：跨部门领导被个人
    # 勾选为 dept_admin 时，管辖自己所在部门的子树。
    if employee.login_enabled:
        if employee.login_role == LoginRole.DEPT_ADMIN:
            return _subtree(employee.department_id)
        return ()

    for dept in access_service.department_ancestors(session, employee.department_id):
        if dept.login_enabled is None:
            continue  # 未设置 = 向上继承
        if dept.login_enabled and dept.login_role == LoginRole.DEPT_ADMIN:
            return _subtree(dept.id)
        # 最近的显式设置不是 dept_admin（admin / member / 显式禁止）：
        # 走到这里说明角色复核与授权行不一致，按无范围处理（失败安全）。
        return ()
    return ()


def resolve_session_scope(session: Session, employee: Employee | None) -> DataScope:
    """按员工解析 dept_admin 数据范围（角色由中间件先行复核）。"""
    return DataScope(kind="subtree", department_ids=managed_department_ids(session, employee))


def attach_session_scope(session: Session, request: Any) -> None:
    """在 get_db 依赖里把请求角色映射为数据范围，挂到 session.info。

    - role=dept_admin：解析管辖子树（可能为空 = 什么都看不到）
    - 其他角色（admin / member / 测试环境无会话）：不注入 = 全公司范围。
      member 的边界由 AuthMiddleware 白名单保证，不靠数据过滤。
    """
    role = getattr(request.state, "session_role", None)
    if role != LoginRole.DEPT_ADMIN:
        return
    user = getattr(request.state, "session_user", None)
    employee = (
        session.get(Employee, user.employee_id)
        if user is not None and user.employee_id is not None
        else None
    )
    session.info[SCOPE_INFO_KEY] = resolve_session_scope(session, employee)
