from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.ai.base import LLMAuthError, LLMError, LLMRateLimitError
from app.api.routes import (
    access,
    ai,
    auth,
    blockers,
    config,
    dashboard,
    departments,
    employees,
    exports,
    health,
    insights,
    management_actions,
    my_actions,
    notifications,
    operating_dashboard,
    reports,
    settings as settings_routes,
    sync,
)
from app.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorPermissionError,
    ConnectorRateLimitError,
)
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.security import (
    SESSION_COOKIE,
    SessionUser,
    ensure_secure_settings,
    read_session_token,
)
from app.db.session import get_session_factory
from app.models.employee import Employee
from app.models.enums import LoginRole, LoginSource
from app.services import access_service
from app.translation.base import (
    TranslationAuthError,
    TranslationError,
    TranslationRateLimitError,
)

logger = get_logger(__name__)

# 鉴权中间件放行的公开路径：健康检查、登录/登出/会话探测、非敏感配置、文档。
_AUTH_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/auth/wecom",
    "/api/config",
    "/docs",
    "/openapi.json",
    "/redoc",
)

# member 角色（企业微信登录的普通同事）**只能**访问这个前缀。
#
# 这是一处安全边界收口。在此之前 member 可以读全公司数据（老板视角）：43 个 GET
# 端点全部放行，包括 `/api/reports`（全公司每篇周报正文）、`/api/employees`（通讯录）、
# `/api/dashboard/*`（部门提交率与经营指标），以及 `/api/exports/reports`
# （全量 CSV 导出）。那意味着**任何**企业微信能登录的同事，只要打开浏览器就能
# 看到整个公司的周报与人事数据 —— 这是隐私泄漏，不是「只读」。
#
# 现在改为白名单：不在名单里的路径一律 403，与请求方法无关（GET 也拦）。
# 名单里只保留负责人自己的事项与其中的沟通 —— 这是他在站内唯一需要的东西。
#
# 为什么可以这么收：部门负责人的可写权限走的是 `login_role=admin`（见 access_service），
# 那条路径不受本名单限制；member 语义就是「普通同事」，他不需要公司级数据。
#
# **往 my_actions.router 新增端点前必须重新评审这一处** —— 它是全站唯一的
# member 可访问命名空间，加一个「看别人事项」或「看全公司列表」的端点
# 就等于把这个口子重新打开。
_MEMBER_ALLOWED_PREFIXES = ("/api/my-actions",)

# dept_admin（部门管理员）可访问的命名空间：本部门数据面 + 本部门管理操作。
#
# 数据层面的部门隔离**不在中间件做**（中间件只拦命名空间），而是由
# `services/data_scope` 在每个请求里解析管辖部门子树并注入查询条件
# （见 deps.get_db → view_service 公共条件函数）。中间件这一层保证的是：
# dept_admin 碰不到公司级能力的命名空间 —— 系统设置、登录授权（另有
# AdminOnly 依赖双重拦截）、经营看板、全员周期总结、管理事项等。
#
# 与 member 白名单同样的纪律：往这些 router 新增端点前必须重新评审本名单。
_DEPT_ADMIN_ALLOWED_PREFIXES = (
    "/api/my-actions",
    "/api/reports",
    "/api/dashboard",
    "/api/employees",
    "/api/departments",
    "/api/blockers",
    "/api/insights",
    "/api/exports",
    "/api/ai",
    "/api/sync",
    "/api/notifications",
)


class AuthMiddleware(BaseHTTPMiddleware):
    """网关层强制身份与权限：未登录 401，越权写入 403。

    两层判断，顺序不能反：

    1. **认证**：会话 Cookie 是不是我们签发的、是否过期。
    2. **授权**：企业微信会话每次请求都回源复核 —— 取消部门授权、员工离职或
       被显式禁止后立刻失效，不必等 12 小时令牌过期。这一层是必要的：
       令牌只证明「该用户在某时刻通过了认证」，把它当授权依据等于把
       撤销操作推迟到令牌到期。

    角色决定命名空间边界：member 只能访问「发给我的事项」；dept_admin 另有
    本部门数据面（数据范围在查询层由 data_scope 收口）；admin 不受限。
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in _AUTH_PUBLIC_PREFIXES
        ):
            return await call_next(request)

        user = read_session_token(request.cookies.get(SESSION_COOKIE))
        if user is None:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "未登录或会话已过期"},
            )

        role = user.role
        if user.source == LoginSource.WECOM:
            try:
                checked = _recheck_wecom_session(user)
            except Exception as exc:  # noqa: BLE001
                logger.warning("会话权限复核失败（%s）：%s", user.subject, exc)
                # 复核不了不等于授权通过，也不等于撤销：503 让前端提示稍后重试，
                # 而不是把用户踢回登录页（DB 抖动时不该产生一次「被登出」）。
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "AuthorizationCheckFailed",
                        "message": "会话权限校验暂不可用，请稍后重试",
                    },
                )
            if checked is None:
                # 会话本身有效，但已不再被授权：必须按未登录处理，
                # 前端会回到登录页并由 /api/auth/me 给出具体原因。
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "AccessRevoked",
                        "message": "你的访问权限已被取消，请重新登录或联系管理员",
                    },
                )
            role = checked

        if role != LoginRole.ADMIN:
            # 主题是部署级外观配置：读无敏感性（所有登录者都应按公司主题渲染
            # UI），写仍是超管专属（settings 路由 AdminOnly 拦截）。这里只对
            # 非管理员放行 GET，避免前端每次启动读主题的 403 噪音。
            if request.method == "GET" and path == "/api/settings/theme":
                request.state.session_user = user
                request.state.session_role = role
                return await call_next(request)
            # member / dept_admin 都走命名空间白名单，但名单不同：
            # member 只有「发给我的事项」；dept_admin 另有本部门数据面
            # （数据范围由 data_scope 在查询层收口，见 deps.get_db）。
            if role == LoginRole.DEPT_ADMIN:
                allowed_prefixes = _DEPT_ADMIN_ALLOWED_PREFIXES
                message = (
                    "当前账号为部门管理员，只能访问本部门数据。"
                    "公司级功能（系统设置 / 经营看板等）仅超级管理员可用。"
                )
            else:
                allowed_prefixes = _MEMBER_ALLOWED_PREFIXES
                message = (
                    "当前账号为只读成员，只能访问「发给我的事项」。"
                    "需要查看经营数据请找管理员开通部门管理员角色。"
                )
            if not any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in allowed_prefixes
            ):
                return JSONResponse(
                    status_code=403,
                    content={"error": "Forbidden", "message": message},
                )
        # 把**复核后**的角色交给下游依赖（见 api/deps.require_admin）：
        # 令牌里的角色可能是旧的，下游据此做细粒度鉴权才不会用到过期结论。
        request.state.session_user = user
        request.state.session_role = role
        return await call_next(request)


def _recheck_wecom_session(user: SessionUser) -> str | None:
    """回源复核企业微信会话的授权状态：返回最新角色，不再授权时返回 None。

    每次请求一次点查询 + 部门链回溯。数据量级是「一人 × 部门深度」，
    走的是主键与外键索引，代价远低于一次真实业务查询；
    换来的是撤销授权立即生效（不必等令牌过期，也不必维护黑名单表）。
    """
    factory = get_session_factory()
    session: Session = factory()
    try:
        employee = (
            session.get(Employee, user.employee_id)
            if user.employee_id is not None
            else None
        )
        decision = access_service.decide_access(session, employee)
        return decision.role if decision.allowed else None
    finally:
        session.close()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    # 生产环境弱凭据直接拒绝启动（不是只打日志）：用默认密钥时任何人都能自签
    # 管理员会话，绕过密码访问全部接口。
    for message in ensure_secure_settings(settings):
        logger.warning("⚠️ 安全配置提示：%s", message)

    is_prod = settings.app_env == "prod"
    app = FastAPI(
        title="Enterprise AI - Weekly Report",
        version="0.1.0",
        description="Sprint 1：企业微信周报数据采集与入库",
        # 生产关闭交互式文档：/docs 与 /openapi.json 会未经鉴权暴露完整 API 面
        # （所有管理端变更端点、参数名与 Schema），对攻击者是现成的靶图。
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(config.router)
    app.include_router(access.router)
    app.include_router(ai.router)
    app.include_router(insights.router)
    app.include_router(management_actions.router)
    app.include_router(my_actions.router)
    app.include_router(blockers.router)
    app.include_router(sync.router)
    app.include_router(departments.router)
    app.include_router(employees.router)
    app.include_router(reports.router)
    app.include_router(settings_routes.router)
    # 主题读取路由：无 AdminOnly，任何登录角色可读（写仍在 settings.router）
    app.include_router(settings_routes.theme_read_router)
    app.include_router(dashboard.router)
    app.include_router(operating_dashboard.router)
    app.include_router(notifications.router)
    app.include_router(exports.router)
    # 测试环境（app_env=test）不挂鉴权中间件，避免破坏既有接口测试；
    # 真实运行（dev/prod）必须强制鉴权。
    if settings.app_env != "test":
        app.add_middleware(AuthMiddleware)
    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """把外部系统异常转成前端可直接展示的响应（需求第 24 节第 13 条）。

    不做处理的话，凭据缺失/权限不足会以 500 堆栈的形式暴露给调用方，
    前端拿不到可用的错误信息。
    """

    status_map: dict[type[ConnectorError], int] = {
        ConnectorPermissionError: 403,
        ConnectorRateLimitError: 429,
        ConnectorAuthError: 502,
    }

    @app.exception_handler(ConnectorError)
    async def _connector_error_handler(
        request: Request, exc: ConnectorError
    ) -> JSONResponse:
        status_code = 502
        for exc_type, code in status_map.items():
            if isinstance(exc, exc_type):
                status_code = code
                break
        logger.warning("外部系统错误 [%s] %s", status_code, exc)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": type(exc).__name__,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        )

    @app.exception_handler(LLMError)
    async def _llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
        """模型故障同样不能抛 500 堆栈 —— 缺 Key 要给出可操作的提示。

        状态码必须区分三类，否则排查时会被误导：
        - Key 缺失/无效（LLMAuthError）：这是**本机配置**问题，重试无用 -> 503
        - 限流（LLMRateLimitError）：稍后可重试 -> 429
        - 其余（返回结构异常等）：确属服务端故障 -> 502
        三者若混成同一个 502，运维会以为 DeepSeek 挂了，实际只是我们没配 Key。
        """
        if isinstance(exc, LLMRateLimitError):
            status_code = 429
        elif isinstance(exc, LLMAuthError):
            status_code = 503
        else:
            status_code = 502
        logger.warning("大模型调用错误 [%s] %s", status_code, exc)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": type(exc).__name__,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        )

    @app.exception_handler(TranslationError)
    async def _translation_error_handler(
        request: Request, exc: TranslationError
    ) -> JSONResponse:
        if isinstance(exc, TranslationRateLimitError):
            status_code = 429
        elif isinstance(exc, TranslationAuthError):
            status_code = 503
        else:
            status_code = 502
        logger.warning("翻译服务错误 [%s] %s", status_code, exc)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": type(exc).__name__,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        )

    from app.services.view_service import InvalidPeriodError

    @app.exception_handler(InvalidPeriodError)
    async def _invalid_period_handler(
        request: Request, exc: InvalidPeriodError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": "InvalidPeriod", "message": str(exc)},
        )


app = create_app()
