"""登录与登出。

两种登录方式：

1. **管理密码**（`POST /api/auth/login`）：部署级单密码，恒为 admin。
   企微配置坏掉、回调域名没配好、或需要跨部门超管视角时，它是唯一入口，
   因此**不做成可选**。
2. **企业微信登录**（`GET /api/auth/wecom/start` → 企微 → `.../callback`）：
   企业成员用企微身份登录。两种入口指向同一套回调：
   - `mode=oauth`：在企业微信客户端内打开，走网页授权，免扫码；
   - `mode=qr`（默认）：PC 浏览器打开企业微信扫码登录页。
   `mode=auto` 会按 UA 是否为企业微信客户端自动选择。

登录成功后谁能进、以什么角色进，由 `services/access_service` 按部门树判定
（见该模块注释）。

## state 的防篡改与「登录 CSRF」

state 是自签令牌（`sign_payload`），并额外与一枚 HttpOnly Cookie 里的随机串绑定：

- 没有签名 ⇒ 攻击者造不出 state（不能把任意的 code 塞给我们换会话）；
- 只签不绑 Cookie ⇒ 仍存在登录 CSRF：攻击者可以自己发一次登录拿到合法 state，
  再把 `?code=<攻击者的code>&state=<合法state>` 链接发给受害者，让受害者的浏览器
  登录成攻击者的账号（虽然拿不到受害者数据，但会污染审计与操作归属）。
  绑上一次性 nonce 后，受害者浏览器里没有对应的 Cookie，回调直接拒绝。
"""

from __future__ import annotations

import html
import json
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.connectors.wecom.client import WeComClient
from app.connectors.wecom.login import (
    WeComLoginError,
    build_oauth_url,
    build_qr_login_url,
    resolve_callback_url,
    resolve_userid,
)
from app.core.config import get_settings
from urllib.parse import quote

from app.core.login_throttle import LoginRateLimiter
from app.core.logging import get_logger
from app.core.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    STATE_MAX_AGE,
    create_session_token,
    read_session_token,
    read_signed_payload,
    sign_payload,
    verify_password,
)
from app.models.employee import Employee
from app.models.enums import AccessDenyReason, LoginRole, LoginSource
from app.services import access_service
from app.services.runtime_config_service import RuntimeConfigError, runtime_settings

logger = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 进程内实例：按配置初始化一次。改配置需重启（与其它安全项一致）。
_limiter = LoginRateLimiter(
    max_failures=get_settings().login_max_failures,
    window_seconds=get_settings().login_lockout_seconds,
    max_global_failures=get_settings().login_max_global_failures,
)

# state 绑定的 Cookie 名
STATE_COOKIE = "wb_login_state"

# 拒绝原因 -> 给用户看的一句话 + 处置建议
_DENY_MESSAGES: dict[str, tuple[str, str]] = {
    AccessDenyReason.NOT_FOUND: (
        "你的企业微信账号不在本系统通讯录中。",
        "请先让管理员在「系统设置 → 同步与抓取」同步一次部门与员工通讯录，"
        "再确认你的账号已同步进来。",
    ),
    AccessDenyReason.INACTIVE: (
        "你的账号当前不是在职状态，无法登录。",
        "若状态有误（例如被误标为疑似离职），请联系管理员在员工列表核对。",
    ),
    AccessDenyReason.NOT_AUTHORIZED: (
        "你还没有被授权访问本系统。",
        "请让管理员在「系统设置 → 登录与授权」中勾选你所在的部门。",
    ),
    AccessDenyReason.DENIED_BY_DEPARTMENT: (
        "你所在的部门被显式禁止访问本系统。",
        "请让管理员在「系统设置 → 登录与授权」中检查该部门的开关。",
    ),
    AccessDenyReason.DENIED_BY_EMPLOYEE: (
        "你的个人访问权限已关闭。",
        "请让管理员在「系统设置 → 登录与授权」中重新勾选你的账号。",
    ),
    AccessDenyReason.LOGIN_DISABLED: (
        "管理员暂时关闭了企业微信登录。",
        "请使用访问密码登录，或稍后再试。",
    ),
    AccessDenyReason.NOT_MEMBER: (
        "当前扫码的账号不是本企业成员。",
        "请用你的企业微信（企业成员身份）重新扫码。",
    ),
    AccessDenyReason.INVALID_STATE: (
        "登录链接已失效或不是本机发起的。",
        "请回到登录页重新点击「企业微信登录」。",
    ),
    AccessDenyReason.FAILED: (
        "企业微信登录失败。",
        "请稍后重试；若持续失败，请让管理员检查企业微信配置。",
    ),
}


def _client_key(request: Request) -> str:
    """取客户端标识。

    反向代理后 request.client 是代理地址，所以优先看 X-Forwarded-For 第一段；
    该头可伪造，因此它只用于「按客户端分桶」，**全局线仍然兜底** ——
    伪造头能绕过单 IP 限制，但绕不过全局限制。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _login_settings(session: Session):
    """读取叠加了设置页覆盖的运行期配置（登录相关项可在设置页改）。"""
    try:
        return runtime_settings(session)
    except RuntimeConfigError:
        # 库不可读/密钥缺失时退回部署默认值：登录页不能因为配置读取失败就打不开。
        logger.warning("运行时配置读取失败，登录使用部署默认值")
        return get_settings()


def _set_session_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_MAX_AGE,
        path="/",
    )


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    client = _client_key(request)
    wait = _limiter.retry_after(client)
    if wait is not None:
        logger.warning("登录被限速：client=%s，需等待 %ss", client, wait)
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请在 {wait} 秒后重试。",
            headers={"Retry-After": str(wait)},
        )

    if not verify_password(payload.password):
        _limiter.record_failure(client)
        logger.warning("登录失败：client=%s", client)
        raise HTTPException(status_code=401, detail="密码错误")

    _limiter.record_success(client)
    token = create_session_token()
    _set_session_cookie(response, token, get_settings().cookie_secure)
    # 直接构造返回值，不要回读 request 的 Cookie：那一刻响应还没下发，
    # 请求里带的是**上一个**会话（或没有会话）。
    return {
        "authenticated": True,
        "user": {
            "authenticated": True,
            "name": "管理员",
            "role": LoginRole.ADMIN,
            "is_admin": True,
            "source": LoginSource.PASSWORD,
            "employee_id": None,
        },
    }


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(STATE_COOKIE, path="/")
    return {"authenticated": False}


def _session_payload(request: Request, session: Session) -> dict:
    """当前会话的公开信息（供前端决定显示什么、隐藏什么）。"""
    user = read_session_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return {"authenticated": False}
    if user.source == LoginSource.PASSWORD:
        return {
            "authenticated": True,
            "name": user.name,
            "role": user.role,
            "is_admin": True,
            "source": user.source,
            "employee_id": None,
        }
    # 企微会话：每次请求都回源复核（取消授权/离职立刻生效，不用等令牌过期）
    employee = (
        session.get(Employee, user.employee_id)
        if user.employee_id is not None
        else None
    )
    decision = access_service.decide_access(session, employee)
    if not decision.allowed:
        return {
            "authenticated": False,
            "reason": decision.reason,
            "message": _deny_message(decision.reason)[0],
        }
    return {
        "authenticated": True,
        "name": user.name,
        "role": decision.role,
        "is_admin": decision.role == LoginRole.ADMIN,
        "source": user.source,
        "employee_id": user.employee_id,
    }


def _deny_message(reason: str | None) -> tuple[str, str]:
    return _DENY_MESSAGES.get(reason or "", _DENY_MESSAGES[AccessDenyReason.FAILED])


@router.get("/me")
def me(
    request: Request, session: Annotated[Session, Depends(get_db)]
) -> dict:
    return _session_payload(request, session)


# --------------------------------------------------------------------------- #
# 企业微信登录
# --------------------------------------------------------------------------- #


def _frontend_origin(request: Request, settings) -> str:
    """登录回调后跳回前端的地址。

    优先用显式配置（生产跨域/多域名时必须配）；否则按请求 Host 推导，
    本机开发不用手工配就能跑通。
    """
    configured = (settings.wecom_login_frontend_url or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _login_target(request: Request, settings, next_path: str) -> str:
    """生成登录成功后的目标地址。

    同域部署使用相对路径，避免反向代理后的内部 Host（例如
    ``127.0.0.1:8010``）被拼进浏览器跳转地址。只有显式配置了前端地址时，
    才生成绝对地址，用于前后端分域部署。
    """
    configured = (settings.wecom_login_frontend_url or "").strip()
    if not configured:
        return next_path
    return f"{configured.rstrip('/')}{next_path}"


def _callback_url(request: Request, settings) -> str:
    """回调地址：域名必须与企微后台配置的可信域名一致（含端口）。

    地址先过 `normalize_callback_url`：管理员只填域名（照抄企微后台的「可信域名」）
    时补上回调路径。不补的后果不是「报错」，而是**静默**丢 code —— 企微会把
    `?code=...` 回跳到站点根，前端 `/` 再重定向丢掉 query，用户扫码一圈回到登录页，
    看起来像「授权没生效」。
    """
    return resolve_callback_url(settings.wecom_login_redirect_uri, str(request.base_url))


def _safe_next(value: str | None) -> str:
    """只接受站内相对路径，避免开放重定向（`//evil.com` 会被浏览器当外链）。"""
    if not value:
        return "/dashboard"
    if not value.startswith("/") or value.startswith("//"):
        return "/dashboard"
    return value


def _denied_response(
    reason: str | None,
    detail: str | None,
    *,
    settings=None,
    frontend: str | None = None,
) -> Response:
    """未授权人员的落地：默认给可读页面，可配置成静默退回登录页。

    默认**明确告知**而不是丢回登录页：成员只看到「登录失败」会反复重试
    并来找管理员，而真正需要改设置的是管理员 —— 页面直接说清原因与找谁。
    """
    if (
        settings is not None
        and frontend
        and settings.wecom_login_denied_mode == "redirect"
    ):
        return RedirectResponse(
            url=f"{frontend}/login?denied={quote(reason or 'failed')}", status_code=303
        )
    return _denied_page(reason, detail)


def _denied_page(reason: str | None, detail: str | None = None) -> HTMLResponse:
    title, hint = _deny_message(reason)
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>无法登录 · 周报平台</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#F4F6F8; color:#181B27;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .card {{ width:min(92vw,420px); background:#fff; border:1px solid rgba(20,30,50,.06);
           border-radius:16px; padding:28px; box-shadow:0 8px 30px rgba(20,30,50,.05); }}
  h1 {{ font-size:16px; margin:0 0 10px; }}
  p {{ font-size:13px; line-height:1.7; color:#5C6579; margin:0 0 8px; }}
  code {{ background:#F4F6F8; padding:1px 5px; border-radius:5px; font-size:12px; }}
  a {{ display:inline-block; margin-top:14px; font-size:13px; color:#259D8D; text-decoration:none; }}
</style></head>
<body><div class="card">
  <h1>{title}</h1>
  <p>{hint}</p>
  {f'<p>技术细节：<code>{detail}</code></p>' if detail else ''}
  <a href="/login">返回登录页</a>
</div></body></html>"""
    return HTMLResponse(content=body, status_code=403)


def _login_complete_page(target: str) -> HTMLResponse:
    """让企微内置浏览器确认会话生效后再进入业务页。

    企业微信 Windows 客户端的内置浏览器偶尔会在 ``Set-Cookie`` 与 303 跳转
    同一个响应里先执行跳转、后持久化 Cookie。直接落到业务页时首批 API 请求因此
    可能仍是未登录状态；刷新后 Cookie 已落盘，看起来就成了“必须刷新一次”。

    回调先返回一个同源 HTML，让响应完整结束，再轮询公开的 ``/api/auth/me``。
    只有服务端已经读到会话时才进入目标页；网络短暂异常或 Cookie 落盘稍慢都会自动
    重试。目标经过 ``_safe_next`` 且用 JSON 编码嵌入脚本，避免开放跳转和脚本注入。
    """
    target_json = json.dumps(target, ensure_ascii=False).replace("</", "<\\/")
    target_href = html.escape(target, quote=True)
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>登录成功 · 周报平台</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#F4F6F8; color:#181B27;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .card {{ width:min(92vw,420px); background:#fff; border:1px solid rgba(20,30,50,.06);
           border-radius:16px; padding:28px; box-shadow:0 8px 30px rgba(20,30,50,.05); text-align:center; }}
  .spinner {{ width:28px; height:28px; margin:0 auto 18px; border:3px solid #D9E8E5;
              border-top-color:#259D8D; border-radius:50%; animation:spin .8s linear infinite; }}
  @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
  h1 {{ font-size:16px; margin:0 0 10px; }}
  p {{ font-size:13px; line-height:1.7; color:#5C6579; margin:0; }}
  a {{ display:none; margin-top:16px; font-size:13px; color:#259D8D; text-decoration:none; }}
</style></head>
<body><div class="card">
  <div class="spinner" aria-hidden="true"></div>
  <h1>登录成功，正在进入系统</h1>
  <p id="status">正在确认登录状态，请稍候…</p>
  <a id="continue" href="{target_href}">继续进入</a>
</div>
<script>
(() => {{
  const target = {target_json};
  const status = document.getElementById("status");
  const link = document.getElementById("continue");
  let attempts = 0;
  const verify = async () => {{
    attempts += 1;
    try {{
      const response = await fetch("/api/auth/me", {{
        credentials: "same-origin",
        cache: "no-store",
        headers: {{ Accept: "application/json" }}
      }});
      const session = response.ok ? await response.json() : null;
      if (session && session.authenticated) {{
        window.location.replace(target);
        return;
      }}
    }} catch (_) {{
      // 内置浏览器切回前台或网络瞬断时继续重试。
    }}
    if (attempts < 24) {{
      window.setTimeout(verify, 250);
      return;
    }}
    status.textContent = "自动进入暂未完成，请点击下方按钮继续。";
    link.style.display = "inline-block";
  }};
  window.setTimeout(verify, 150);
}})();
</script></body></html>"""
    return HTMLResponse(
        content=body,
        status_code=200,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/wecom/start")
def wecom_start(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    mode: str = "auto",
    next: str | None = None,
) -> Response:
    """发起企业微信登录：302 到企微的授权/扫码页。"""
    settings = _login_settings(session)
    if not settings.wecom_login_enabled:
        return _denied_page(AccessDenyReason.LOGIN_DISABLED)
    if not settings.wecom_corp_id:
        return _denied_page(
            AccessDenyReason.FAILED, "缺少 WECOM_CORP_ID，无法发起企业微信登录"
        )
    if not settings.wecom_agent_id:
        # 扫码登录必须带 agentid；缺了它企微会直接报参数错误，
        # 在这里拦住能给出「去配哪个环境变量」的明确指引。
        return _denied_page(
            AccessDenyReason.FAILED,
            "缺少 WECOM_AGENT_ID：企业微信登录必须指定自建应用 AgentId",
        )

    resolved_mode = _resolve_mode(mode, request)
    issued = int(time.time())
    nonce = secrets.token_urlsafe(16)
    state_token = sign_payload(
        {
            "iat": issued,
            "exp": issued + STATE_MAX_AGE,
            "n": nonce,
            "next": _safe_next(next),
            "m": resolved_mode,
        }
    )
    redirect_uri = _callback_url(request, settings)
    if resolved_mode == "qr":
        target = build_qr_login_url(settings, redirect_uri, state_token)
    else:
        target = build_oauth_url(settings, redirect_uri, state_token)

    response = RedirectResponse(url=target, status_code=302)
    # 与 state 绑定的随机串：回调必须带上同一枚 Cookie，否则判定为跨站发起的登录
    response.set_cookie(
        key=STATE_COOKIE,
        value=nonce,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=STATE_MAX_AGE,
        path="/",
    )
    return response


def _resolve_mode(mode: str, request: Request) -> str:
    """auto：企业微信客户端内用网页授权（免扫码），其余一律扫码。"""
    if mode in ("qr", "oauth"):
        return mode
    ua = (request.headers.get("user-agent") or "").lower()
    return "oauth" if "wxwork" in ua else "qr"


@router.get("/wecom/callback")
def wecom_callback(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    code: str | None = None,
    state: str | None = None,
) -> Response:
    """企业微信回调：校验 state → 换取 userid → 判定授权 → 下发会话。"""
    settings = _login_settings(session)
    frontend = _frontend_origin(request, settings)

    def denied(reason: str | None, detail: str | None = None) -> Response:
        return _denied_response(
            reason, detail, settings=settings, frontend=frontend
        )

    payload = read_signed_payload(state, max_age=STATE_MAX_AGE)
    if payload is None:
        return denied(AccessDenyReason.INVALID_STATE)
    cookie_nonce = request.cookies.get(STATE_COOKIE) or ""
    if not cookie_nonce or not secrets.compare_digest(
        str(payload.get("n") or ""), cookie_nonce
    ):
        # 登录 CSRF 防线：state 合法但与本机发起的那次不符
        logger.warning("企业微信回调 state 与 Cookie 不匹配，拒绝")
        return denied(AccessDenyReason.INVALID_STATE)

    if not settings.wecom_login_enabled:
        return denied(AccessDenyReason.LOGIN_DISABLED)

    client = WeComClient(settings)
    try:
        identity = resolve_userid(client, code or "")
    except WeComLoginError as exc:
        logger.warning("企业微信登录失败：%s", exc)
        return denied(AccessDenyReason.FAILED, str(exc))
    finally:
        client.close()

    userid = identity.get("userid")
    if not userid:
        logger.info("企业微信登录被拒：非本企业成员 openid=%s", identity.get("openid"))
        return denied(AccessDenyReason.NOT_MEMBER)

    employee = access_service.find_employee_by_userid(session, userid)
    decision = access_service.decide_access(session, employee)
    if not decision.allowed:
        logger.info(
            "企业微信登录被拒：userid=%s reason=%s", userid, decision.reason
        )
        return denied(decision.reason, decision.detail)

    token = create_session_token(
        subject=userid,
        role=decision.role or LoginRole.MEMBER,
        source=LoginSource.WECOM,
        name=employee.name if employee else userid,
        employee_id=employee.id if employee else None,
    )
    response = _login_complete_page(
        _login_target(request, settings, _safe_next(payload.get("next")))
    )
    _set_session_cookie(response, token, settings.cookie_secure)
    response.delete_cookie(STATE_COOKIE, path="/")
    logger.info(
        "企业微信登录成功：userid=%s name=%s role=%s（授权来源：%s）",
        userid,
        employee.name if employee else userid,
        decision.role,
        decision.granted_by_name or decision.granted_by,
    )
    return response
