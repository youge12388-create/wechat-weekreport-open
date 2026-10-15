"""企业微信登录：网页授权（应用内免扫码）与扫码登录（电脑浏览器）。

接口事实（已核对官方文档，勿凭记忆修改）：

  - **扫码登录**（PC 浏览器）：
      https://login.work.weixin.qq.com/wwlogin/sso/login
        ?login_type=CorpApp&appid=<CorpID>&agentid=<AgentId>
        &redirect_uri=<urlencode>&state=<urlencode>&lang=zh
    自建应用的 redirect_uri 域名必须配置为该应用的
    「Web 网页授权回调域名 / OAuth 可信域名」，且**端口也要一致**。
    回调带回 `?code=...&state=...`。

  - **网页授权**（企业微信客户端内打开，免扫码静默登录）：
      https://open.weixin.qq.com/connect/oauth2/authorize
        ?appid=<CorpID>&redirect_uri=<urlencode>&response_type=code
        &scope=snsapi_base&agentid=<AgentId>&state=<urlencode>#wechat_redirect
    域名必须配到该应用的「可信域名」。

  - **换取身份**：`GET /cgi-bin/auth/getuserinfo?access_token=&code=`
    企业成员返回 `userid`（互通/上下游企业会带 `CorpId/` 前缀），
    非本企业成员只返回 `openid` / `external_userid`，**没有 userid** ——
    这正是「扫码的人不是本企业员工」的判定依据。
    code 一次性、5 分钟过期，用第二次会拿到 40029。

凭据与通讯录同步共用 WECOM_CORP_ID / WECOM_CORP_SECRET / WECOM_AGENT_ID。

本模块只负责「拼链接 + 换取 userid」，不做任何授权判定 ——
谁能进系统由 services/access_service 决定（Connector 只取数，见 AGENTS.md 第 4 节）。
"""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

from app.connectors import base as cb
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

QR_LOGIN_URL = "https://login.work.weixin.qq.com/wwlogin/sso/login"
OAUTH_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"

# 本系统接收企微 code 的路径。redirect_uri 必须指到这里，
# 指到别的路径（尤其是站点根）会拿不到 code。
CALLBACK_PATH = "/api/auth/wecom/callback"

# code 无效 / 已用过 / 已过期：重试无用（换 token 也一样），必须回登录页重新发起。
# 参考：官方文档错误码 40029 invalid code。
INVALID_CODE_CODES = {40029}


class WeComLoginError(cb.ConnectorError):
    """登录链路失败：附带给用户看的一句话结论与处置建议。"""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def normalize_callback_url(value: str | None, *, default_path: str = CALLBACK_PATH) -> str:
    """把管理员填写的回调地址归一化成可以下发给企微的 redirect_uri。

    真实事故（2026-09-18 线上）让这条规则成为必需：企微后台登记的是**域名**
    （「Web 网页授权及 JS-SDK 可信域名」），管理员照抄到设置页就变成只填域名。
    系统原样下发后，企微把 `?code=...&state=...` 回跳到**站点根**，而前端 `/`
    会 `redirect("/dashboard")` 丢掉 query —— code 永远到不了回调接口，
    用户扫码一圈回到登录页。授权勾没勾根本轮不到判定。

    规则：
    - 只填域名或只带 `/` → 补上回调路径；
    - 已带路径 → 原样保留（企微只校验域名，路径可以用自定义的）；
    - 去掉 query 与 fragment：企微回跳时自己会拼 `?code=...`，
      留着会让参数拼在错误的位置；
    - 非法值（非 http(s)、无主机、相对地址）→ 返回空串，由调用方按「未配置」
      处理或直接拒绝保存，**不猜一个看起来能用的结果**。
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    path = parts.path or default_path
    if path == "/":
        path = default_path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def resolve_callback_url(configured: str | None, base_url: str) -> str:
    """有效回调地址：显式配置优先（并归一化），否则按访问域名推导。"""
    normalized = normalize_callback_url(configured)
    if normalized:
        return normalized
    return f"{base_url.rstrip('/')}{CALLBACK_PATH}"


def build_qr_login_url(
    settings: Settings, redirect_uri: str, state: str, lang: str = "zh"
) -> str:
    """构造扫码登录链接（PC 浏览器打开即显示企业微信二维码）。

    redirect_uri 必须先整体 URLEncode：里面有 `://` 和 `=`，
    直接拼会让外层 query 解析错位（典型表现是回调域名被截断，
    企业微信报「页面域名与登录授权域名不一致」）。
    """

    query = urlencode(
        {
            "login_type": "CorpApp",
            "appid": settings.wecom_corp_id,
            "agentid": settings.wecom_agent_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "lang": lang,
        }
    )
    # state 里的 token 是 base64url，字符集安全；这里不再二次转义，
    # 因为 urlencode 已经把它的 `-`/`_` 原样保留（都是安全字符）。
    return f"{QR_LOGIN_URL}?{query}"


def build_oauth_url(
    settings: Settings,
    redirect_uri: str,
    state: str,
    scope: str = "snsapi_base",
) -> str:
    """构造网页授权链接（在企业微信客户端内打开可免扫码静默登录）。"""
    query = urlencode(
        {
            "appid": settings.wecom_corp_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "agentid": settings.wecom_agent_id,
            "state": state,
        }
    )
    return f"{OAUTH_AUTHORIZE_URL}?{query}#wechat_redirect"


def resolve_userid(client, code: str) -> dict:
    """用 code 换取企业成员身份。

    返回值区分两种情况（调用方据此给出不同的拒绝原因）：
    - `{"userid": "..."}`                        本企业成员
    - `{"userid": None, "openid": ...}`          非本企业成员（扫码的是外部人员）
    链路失败抛 WeComLoginError。
    """
    if not code:
        raise WeComLoginError("缺少授权 code", hint="请重新发起登录。")

    try:
        data = client.get_login_userinfo(code)
    except cb.ConnectorAuthError as exc:
        raise WeComLoginError(
            f"换取企业微信身份失败：{exc}",
            hint="请确认 WECOM_CORP_ID / WECOM_CORP_SECRET 配置正确，"
            "且自建应用有读取成员信息的权限。",
        ) from exc
    except cb.ConnectorError as exc:
        raise WeComLoginError(f"换取企业微信身份失败：{exc}") from exc

    userid = str(data.get("userid") or "").strip()
    if userid:
        # 上下游/互联企业的 userid 形如 `CorpId/userid`，本地通讯录存的是裸 userid。
        if "/" in userid:
            userid = userid.split("/", 1)[1]
        return {"userid": userid, "raw": data}
    return {
        "userid": None,
        "openid": data.get("openid"),
        "external_userid": data.get("external_userid"),
        "raw": data,
    }
