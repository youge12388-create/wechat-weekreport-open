"""API 依赖。

权限分两层，职责不同，不要互相替代：
- 网关层 `AuthMiddleware`（app/main.py）负责「所有 /api/* 必须登录」，
  以及「只读角色不能写」。它是统一入口，漏配即全站失守。
- 这里的 `require_admin` 负责「这个端点只有可写管理员能看」——
  纯管理工具类接口（如登录授权树）即便只读也不该给普通成员看。

角色以**中间件复核后**的值（`request.state.session_role`）为准，而不是令牌里的角色：
企业微信会话的授权与角色每个请求都会回源核对，令牌里的角色可能已经过期
（例如刚被从可写降为只读）。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.connectors.wecom.client import WeComClient
from app.db.session import session_scope
from app.models.enums import LoginRole
from app.services import data_scope


def get_db(request: Request) -> Iterator[Session]:
    """请求级数据库会话。

    会话创建后立即解析并注入数据范围（dept_admin 的部门子树）：
    view_service / insight_service / agent_tools 的公共条件函数读取
    ``session.info`` 上的 DataScope 自动叠加过滤。测试环境的依赖覆写
    不经过这里，范围恒为全公司 —— 与既有测试行为兼容；dept_admin 的
    范围过滤由 middleware 环境的专项测试覆盖。
    """
    with session_scope() as session:
        data_scope.attach_session_scope(session, request)
        yield session


def get_wecom_client() -> Iterator[WeComClient]:
    client = WeComClient()
    try:
        yield client
    finally:
        client.close()


def require_admin(request: Request) -> None:
    """仅可写管理员可用；其余一律 403。

    测试环境（app_env=test）不挂鉴权中间件，此处直接放行，避免既有接口测试
    全部要先伪造会话 —— 鉴权与角色逻辑本身由 test_auth / test_wecom_login
    在开启中间件的环境下单独验证。
    """
    from app.core.config import get_settings

    if get_settings().app_env == "test":
        return
    if getattr(request.state, "session_role", None) != LoginRole.ADMIN:
        raise HTTPException(status_code=403, detail="该功能仅可写管理员可用")


class CurrentSession:
    """当前请求的操作者身份。

    `employee_id` 是**企业微信会话**才有的员工 id；用访问密码登录的管理员
    没有员工身份（恒为 None），所以这里不能假设「登录了就有员工 id」——
    跟进会话要据此区分「谁发的消息」，以及判断负责人是否就是本人。
    """

    def __init__(self, *, is_admin: bool, employee_id: int | None, name: str | None) -> None:
        self.is_admin = is_admin
        self.employee_id = employee_id
        self.name = name


def get_current_session(request: Request) -> CurrentSession:
    """从网关注入的会话状态取操作者身份。

    取 `request.state` 而不是重新解析 Cookie：中间件已经做过认证与**回源复核**，
    令牌里的角色可能已经过期（例如刚被从可写降为只读），在这里再解析一次
    等于把过期的结论又用一遍。

    测试环境不挂中间件（`app_env=test`），此时按管理员处理 —— 这保持了
    既有接口测试的写法（它们不带会话 Cookie），鉴权本身由
    `test_wecom_login.py` 在开启中间件的环境下验证。
    """
    from app.core.config import get_settings

    settings = get_settings()
    user = getattr(request.state, "session_user", None)
    if user is None:
        # 中间件未挂载（测试）或公开路径：按管理员放行，与 require_admin 同一约定。
        is_admin = settings.app_env == "test"
        return CurrentSession(is_admin=is_admin, employee_id=None, name="管理员" if is_admin else None)
    role = getattr(request.state, "session_role", None) or user.role
    return CurrentSession(
        is_admin=role == LoginRole.ADMIN,
        employee_id=user.employee_id,
        name=user.name,
    )


# 用法：router = APIRouter(dependencies=[AdminOnly]) 或单端点 dependencies=[AdminOnly]
AdminOnly = Depends(require_admin)
CurrentActor = Depends(get_current_session)

DbSession = Session
WeComClientDep = WeComClient
