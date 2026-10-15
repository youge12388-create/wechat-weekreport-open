"""登录与授权管理接口（仅管理员可用）。

**授权模型**（详见 `services/access_service`）：沿部门链就近判定；员工行的“允许登录”
取消后会写入个人明确拒绝，优先于部门授权。

**为什么授权数据只有一列布尔位**：部门授权按整棵子树生效，一个部门只占
一行两个字段（login_enabled + login_role），不产生逐人授权行。
一万人公司与一百人公司在这里的数据量相同 —— 授权信息不随人数膨胀。

接口分三块：
- `GET /overview`：开关状态、通讯录前提、已授权人数（供设置页头部）
- `GET /tree`：带继承后有效结论的授权树（前端一次拿全，不用逐节点请求）
- `PATCH /departments/{id}`、`PATCH /employees/{id}`：写单个授权位
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import AdminOnly, get_db
from app.connectors.wecom.login import (
    CALLBACK_PATH,
    normalize_callback_url,
    resolve_callback_url,
)
from app.core.config import get_settings
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import EmployeeStatus, LoginRole
from app.services.access_service import authorized_employee_ids
from app.services.runtime_config_service import (
    RuntimeConfigError,
    login_snapshot,
    runtime_settings,
    update_runtime_values,
)

# 整个路由都是管理工具（授权树会暴露谁有管理权限），只读成员也不该看：
# 中间件只保证「登录 + 只读不能写」，这里再收紧到「只有可写管理员」。
router = APIRouter(prefix="/api/access", tags=["access"], dependencies=[AdminOnly])


def _login_settings(session: Session):
    try:
        return runtime_settings(session)
    except RuntimeConfigError:
        return get_settings()


class DepartmentAccessUpdate(BaseModel):
    """部门授权：三态开关 + 角色。

    `enabled=None` 表示「未设置，向上继承」——必须与 false（显式禁止）区分，
    否则「全公司开放、某部门除外」无法表达。
    """

    enabled: bool | None = None
    role: str | None = Field(default=None, description="member 只读 / admin 可写")


class EmployeeAccessUpdate(BaseModel):
    enabled: bool | None = None
    role: str | None = Field(default=None)


class WeComLoginUpdate(BaseModel):
    enabled: bool | None = None
    redirect_uri: str | None = Field(default=None, max_length=500)
    frontend_url: str | None = Field(default=None, max_length=500)
    denied_mode: str | None = Field(default=None, description="show / redirect")


@router.get("/overview")
def access_overview(
    request: Request, session: Annotated[Session, Depends(get_db)]
) -> dict:
    """授权总览：开关、前提条件、已授权人数。

    `prerequisites` 里的每一项都对应一个真实的配置前提，缺哪一项都会让
    企业微信登录直接失败；这里一次性列出，避免管理员反复试错。
    """
    settings = _login_settings(session)
    allowed_ids, admin_ids = authorized_employee_ids(session)

    active_total = session.scalar(
        select(func.count())
        .select_from(Employee)
        .where(Employee.status == EmployeeStatus.ACTIVE)
    )
    dept_total = session.scalar(select(func.count()).select_from(Department))
    dept_configured = session.scalar(
        select(func.count())
        .select_from(Department)
        .where(Department.login_enabled.is_not(None))
    )

    prerequisites = [
        {
            "key": "corp_id",
            "label": "企业 ID 与自建应用 Secret",
            "ok": bool(settings.wecom_corp_id and settings.wecom_corp_secret),
            "hint": "系统设置 → 同步与抓取 → 企业微信凭据",
        },
        {
            "key": "agent_id",
            "label": "自建应用 AgentId",
            "ok": bool(settings.wecom_agent_id),
            "hint": "扫码登录与网页授权都需要 AgentId（WECOM_AGENT_ID）",
        },
        {
            "key": "contacts",
            "label": "已同步通讯录",
            "ok": bool(active_total),
            "hint": "系统设置 → 同步与抓取 → 部门与员工通讯录",
        },
        {
            "key": "redirect_uri",
            "label": "回调地址已配置",
            "ok": bool(settings.wecom_login_redirect_uri),
            "hint": "留空时按访问域名自动推导，开发够用；生产建议显式配置",
        },
    ]

    return {
        **login_snapshot(settings),
        "authorized_count": len(allowed_ids),
        "admin_count": len(admin_ids),
        "active_employee_total": int(active_total or 0),
        "department_total": int(dept_total or 0),
        "department_configured_total": int(dept_configured or 0),
        "prerequisites": prerequisites,
        "callback_path": CALLBACK_PATH,
        # 真正会下发给企微的地址（配置归一化后 / 未配置则按访问域名推导）。
        # 设置页展示它而不是只展示输入框内容：两者不一致时，管理员能当场看出来，
        # 而不是等同事扫码登不进来才发现。
        "effective_callback_url": resolve_callback_url(
            settings.wecom_login_redirect_uri, str(request.base_url)
        ),
    }


@router.get("/tree")
def access_tree(session: Annotated[Session, Depends(get_db)]) -> dict:
    """返回带「继承后有效结论」的组织树。

    前端只做渲染与勾选，**不在前端重算授权规则** —— 单点判定逻辑留在后端，
    否则前端算一套、后端算一套，迟早出现「界面显示能登录、实际登不进来」。
    """
    settings = _login_settings(session)
    departments = session.scalars(select(Department).order_by(Department.id)).all()
    employees = session.scalars(
        select(Employee)
        .where(Employee.status == EmployeeStatus.ACTIVE)
        .order_by(Employee.id)
    ).all()

    by_id = {dept.id: dept for dept in departments}

    # 自顶向下传播有效结论；根节点的 inherited 为 None（未授权）
    children: dict[int | None, list[int]] = {}
    for dept in departments:
        children.setdefault(dept.parent_id, []).append(dept.id)

    # 子树成员数一次算清：逐部门调 department_subtree_ids 会把整表扫 N 遍，
    # 部门一多就是 O(N²)（万人组织里这是设置页打不开的直接原因）。
    subtree_size: dict[int, int] = {dept.id: 0 for dept in departments}
    for emp in employees:
        # 员工只可能挂在叶子或中间部门，逐级向上累加到根，共用一次遍历
        cursor = emp.department_id
        guard: set[int] = set()
        while cursor is not None and cursor in subtree_size and cursor not in guard:
            guard.add(cursor)
            subtree_size[cursor] += 1
            parent = by_id[cursor].parent_id
            cursor = parent

    effective: dict[int, dict] = {}
    queue: list[tuple[Department, dict | None]] = [
        (dept, None) for dept in departments if dept.parent_id not in by_id
    ]
    while queue:
        dept, inherited = queue.pop()
        if dept.login_enabled is None:
            state = dict(inherited) if inherited else None
        elif dept.login_enabled:
            state = {
                "allowed": True,
                "role": dept.login_role
                if dept.login_role in LoginRole.all()
                else LoginRole.MEMBER,
                "source": "department",
                "source_id": dept.id,
                "source_name": dept.name,
            }
        else:
            state = {
                "allowed": False,
                "role": None,
                "source": "department",
                "source_id": dept.id,
                "source_name": dept.name,
            }
        effective[dept.id] = {
            "setting": dept.login_enabled,
            "role": dept.login_role,
            "effective": state,
        }
        for child_id in children.get(dept.id, []):
            queue.append((by_id[child_id], state))

    direct_count: dict[int, int] = {}
    for emp in employees:
        if emp.department_id is not None:
            direct_count[emp.department_id] = direct_count.get(emp.department_id, 0) + 1

    nodes = [
        {
            "id": dept.id,
            "name": dept.name,
            "parent_id": dept.parent_id,
            "external_id": dept.external_id,
            "status": dept.status,
            "subtree_employee_count": subtree_size.get(dept.id, 0),
            "employee_count": direct_count.get(dept.id, 0),
            **effective.get(
                dept.id,
                {
                    "setting": dept.login_enabled,
                    "role": dept.login_role,
                    "effective": None,
                },
            ),
        }
        for dept in departments
    ]

    employee_nodes = [
        {
            "id": emp.id,
            "name": emp.name,
            "external_id": emp.external_id,
            "position": emp.position,
            "department_id": emp.department_id,
            "setting": bool(emp.login_enabled),
            "denied": bool(emp.login_denied),
            "role": emp.login_role,
        }
        for emp in employees
    ]

    allowed_ids, admin_ids = authorized_employee_ids(session)
    return {
        "departments": nodes,
        "employees": employee_nodes,
        "authorized_employee_ids": sorted(allowed_ids),
        "admin_employee_ids": sorted(admin_ids),
        "enabled": settings.wecom_login_enabled,
    }


@router.patch("/departments/{department_id}")
def update_department_access(
    department_id: int,
    payload: DepartmentAccessUpdate,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    department = session.get(Department, department_id)
    if department is None:
        raise HTTPException(status_code=404, detail="部门不存在")
    fields = payload.model_fields_set
    if "enabled" in fields:
        department.login_enabled = payload.enabled
    if "role" in fields and payload.role is not None:
        if payload.role not in LoginRole.all():
            raise HTTPException(status_code=400, detail="角色只能是 member 或 admin")
        department.login_role = payload.role
    session.commit()
    return {"id": department.id, "setting": department.login_enabled, "role": department.login_role}


@router.patch("/employees/{employee_id}")
def update_employee_access(
    employee_id: int,
    payload: EmployeeAccessUpdate,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    fields = payload.model_fields_set
    if "enabled" in fields:
        was_denied = bool(employee.login_denied)
        employee.login_enabled = bool(payload.enabled)
        # 简单模式：取消勾选是明确禁止，不再静默回到部门继承。
        employee.login_denied = payload.enabled is False
        if payload.enabled is True and was_denied and "role" not in fields:
            # 重新开放曾被明确禁止的账号时，调用方若没指定角色则按最小权限恢复。
            employee.login_role = LoginRole.MEMBER
    if "role" in fields and payload.role is not None:
        if payload.role not in LoginRole.all():
            raise HTTPException(status_code=400, detail="角色只能是 member 或 admin")
        employee.login_role = payload.role
    session.commit()
    return {
        "id": employee.id,
        "setting": bool(employee.login_enabled),
        "denied": bool(employee.login_denied),
        "role": employee.login_role,
    }


@router.patch("/settings")
def update_wecom_login_settings(
    payload: WeComLoginUpdate, session: Annotated[Session, Depends(get_db)]
) -> dict:
    """企业微信登录开关与回调地址（运行期设置，保存后下一次登录即生效）。"""
    values: dict[str, str | None] = {}
    fields = payload.model_fields_set
    if "enabled" in fields and payload.enabled is not None:
        values["wecom_login_enabled"] = "true" if payload.enabled else "false"
    if "redirect_uri" in fields:
        raw = (payload.redirect_uri or "").strip()
        if raw:
            normalized = normalize_callback_url(raw)
            if not normalized:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "回调地址必须是完整的 http(s) 地址，"
                        f"例如 https://你的域名{CALLBACK_PATH}"
                    ),
                )
            # 只填域名时补上回调路径后再落库：存进去的必须是可直接下发的完整地址，
            # 否则错误配置要到同事扫码登不进来时才暴露。
            raw = normalized
        values["wecom_login_redirect_uri"] = raw
    if "frontend_url" in fields:
        values["wecom_login_frontend_url"] = (payload.frontend_url or "").strip()
    if "denied_mode" in fields and payload.denied_mode:
        if payload.denied_mode not in ("show", "redirect"):
            raise HTTPException(status_code=400, detail="denied_mode 只能是 show 或 redirect")
        values["wecom_login_denied_mode"] = payload.denied_mode
    if not values:
        return login_snapshot(_login_settings(session))
    try:
        update_runtime_values(session, values)
        session.commit()
    except RuntimeConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return login_snapshot(_login_settings(session))
