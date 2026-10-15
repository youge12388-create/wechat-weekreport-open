"""企业微信登录授权：谁可以登录、以什么角色登录。

## 授权模型（就近判定，整棵树维护，不产生逐人授权行）

沿「员工 → 所属部门 → 上级部门 → … → 根部门」逐级向上看，**第一个被显式设置的
开关说了算**：

  1. 员工个人级 `employee.login_denied`：显式禁止直接拒绝；`login_enabled=true`
     直接放行（用于跨部门领导），否则继续向上看部门链。
  2. 部门链上第一个 `login_enabled is not None` 的部门：
     * `True`  → 放行，角色取该部门（或更近的部门）的 `login_role`
     * `False` → 拒绝，**不再向上继承**
  3. 走到根仍没有任何显式设置 → 拒绝（默认不授权）。

「就近否决」而不是「任一祖先授权即放行」是刻意的：后者无法表达
「全公司开放、但某个部门除外」—— 公司一开，下面所有部门都被自动放行，
没有地方能拦住。授权的价值恰恰在于边界可表达。

授权数据只有部门行的两个字段（`login_enabled` / `login_role`）加员工的
两个同名字段：随部门数量增长，不随人数膨胀 —— 这是「不占位置」的关键。

## 与角色隔离的关系

登录授权只决定「能不能进系统」。进系统后数据范围仍是全公司（老板视角），
部门级数据隔离是后续 Sprint 的事。角色只区分「可写」与「只读」。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AccessDenyReason, EmployeeStatus, LoginRole


@dataclass(frozen=True)
class AccessDecision:
    """一次授权判定的结论。

    `allowed=False` 时 `reason` 必然有值，且能直接展示给人看 ——
    「为什么登不进去」必须直达用户，否则只能靠翻日志排查。
    """

    allowed: bool
    role: str | None = None
    reason: str | None = None
    detail: str | None = None
    # 让口令/扫码两条路径共用同一份结论展示：命中的是哪一级授权
    granted_by: str | None = None
    granted_by_name: str | None = None


def _normalized_role(value: str | None) -> str:
    """非法角色一律降级为 member（只读），不做「猜一个更宽松的」。"""
    return value if value in LoginRole.all() else LoginRole.MEMBER


def department_ancestors(session: Session, department_id: int | None) -> list[Department]:
    """自底向上返回部门链（含自身）。带环保护：数据异常时不能死循环。"""
    if department_id is None:
        return []
    chain: list[Department] = []
    seen: set[int] = set()
    current = session.get(Department, department_id)
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current)
        if current.parent_id is None:
            break
        current = session.get(Department, current.parent_id)
    return chain


def department_subtree_ids(session: Session, root_id: int) -> set[int]:
    """返回部门及其全部下级部门 id。

    授权按子树生效，所以界面统计「这个部门授权了所有人」必须按子树算，
    不能只看直属部门。
    """
    children: dict[int | None, list[int]] = {}
    for dept_id, parent_id in session.execute(
        select(Department.id, Department.parent_id)
    ):
        children.setdefault(parent_id, []).append(dept_id)

    result: set[int] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        stack.extend(children.get(current, []))
    return result


def _walk_departments(
    session: Session, department_id: int | None
) -> tuple[Department, bool] | None:
    """沿部门链找最近的显式设置：返回 (部门, 是否允许)。"""
    for dept in department_ancestors(session, department_id):
        if dept.login_enabled is not None:
            return dept, bool(dept.login_enabled)
    return None


def decide_access(session: Session, employee: Employee | None) -> AccessDecision:
    """判定某个员工是否可登录，以及登录后的角色。"""
    if employee is None:
        return AccessDecision(
            False, reason=AccessDenyReason.NOT_FOUND, detail="通讯录中没有这个账号"
        )
    if employee.status != EmployeeStatus.ACTIVE:
        return AccessDecision(
            False,
            reason=AccessDenyReason.INACTIVE,
            detail=f"当前状态：{employee.status}",
        )

    # 个人明确禁止优先于部门授权。这样管理员取消员工勾选后，
    # 不会因为上级部门仍然开放而继续以管理员身份进入。
    if employee.login_denied:
        return AccessDecision(
            False,
            reason=AccessDenyReason.DENIED_BY_EMPLOYEE,
            detail=f"员工「{employee.name}」被个人设置明确禁止",
        )

    if employee.login_enabled:
        return AccessDecision(
            True,
            role=_normalized_role(employee.login_role),
            granted_by="employee",
            granted_by_name=employee.name,
        )

    hit = _walk_departments(session, employee.department_id)
    if hit is None:
        return AccessDecision(
            False,
            reason=AccessDenyReason.NOT_AUTHORIZED,
            detail="未被任何部门或个人授权登录",
        )
    dept, allowed = hit
    if not allowed:
        return AccessDecision(
            False,
            reason=AccessDenyReason.DENIED_BY_DEPARTMENT,
            detail=f"被部门「{dept.name}」显式禁止",
        )
    # 角色同样就近取值：判定命中的那个部门说了算。
    return AccessDecision(
        True,
        role=_normalized_role(dept.login_role),
        granted_by="department",
        granted_by_name=dept.name,
    )


def find_employee_by_userid(session: Session, userid: str) -> Employee | None:
    """按企微 userid 找员工（external_id 即 userid，source=wecom）。"""
    return session.scalar(
        select(Employee).where(
            Employee.source == "wecom", Employee.external_id == str(userid)
        )
    )


def authorized_employee_ids(
    session: Session, *, include_employee_level: bool = True
) -> tuple[set[int], set[int]]:
    """一次性算全公司授权情况，返回 (可登录员工 id, 管理员 id)。

    给设置页「已授权 N 人」用。逐人调 decide_access 是 O(人数 × 部门深度)，
    这里改成先把部门的有效授权自顶向下传播一次再匹配员工，避免 N+1。
    """
    departments = session.scalars(select(Department)).all()
    by_id = {dept.id: dept for dept in departments}
    children: dict[int | None, list[int]] = {}
    for dept in departments:
        children.setdefault(dept.parent_id, []).append(dept.id)

    # 自顶向下传播：每个部门记 (是否允许, 角色)。None 表示还没被任何祖先决定。
    effective: dict[int, tuple[bool, str] | None] = {}
    roots = [dept for dept in departments if dept.parent_id not in by_id]
    queue: list[tuple[Department, tuple[bool, str] | None]] = [
        (root, None) for root in roots
    ]
    while queue:
        dept, inherited = queue.pop()
        if dept.login_enabled is None:
            # 未设置 = 继承最近的祖先结论；根部门也没设置时保持 None（拒绝）
            state = inherited
        elif dept.login_enabled:
            state = (True, _normalized_role(dept.login_role))
        else:
            state = (False, LoginRole.MEMBER)
        effective[dept.id] = state
        for child_id in children.get(dept.id, []):
            queue.append((by_id[child_id], state))

    allowed: set[int] = set()
    admins: set[int] = set()
    rows = session.execute(
        select(
            Employee.id,
            Employee.department_id,
            Employee.login_enabled,
            Employee.login_denied,
            Employee.login_role,
            Employee.status,
        )
    ).all()
    for emp_id, dept_id, emp_enabled, emp_denied, emp_role, status in rows:
        if status != EmployeeStatus.ACTIVE:
            continue
        if emp_denied:
            continue
        if emp_enabled:
            role = _normalized_role(emp_role)
            allowed.add(emp_id)
            if role == LoginRole.ADMIN:
                admins.add(emp_id)
            continue
        if not include_employee_level:
            continue
        state = effective.get(dept_id) if dept_id is not None else None
        if state and state[0]:
            allowed.add(emp_id)
            if state[1] == LoginRole.ADMIN:
                admins.add(emp_id)
    return allowed, admins
