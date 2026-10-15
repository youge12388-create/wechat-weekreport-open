"""会话令牌与登录凭据校验。

设计取舍：
- 不引入第三方依赖，用标准库 `hmac` 自签令牌（开发/内部部署足够，且不污染环境）。
- 令牌载荷只含签发时间、过期时间、主体、角色与会话来源，不含任何敏感信息；
  敏感比对一律用 `hmac.compare_digest` 做恒定时间比较，避免时序侧信道。
- 会话现在有两种来源（`LoginSource`）：
  * password：部署级管理密码，唯一超管，恒为 admin；
  * wecom：企业微信成员（网页授权 / 扫码），角色来自授权表。
  令牌只证明「这个人在某个时刻通过了认证」，**不**作为授权依据：
  wecom 会话在每个请求里由 `services/access_service` 重新核对授权，
  所以取消授权（或降级为只读）立刻生效，不必等令牌过期。
- 旧格式令牌（只有 sub/iat/exp，没有 role/src）仍被接受，等价于
  「sub == admin 的密码会话」——升级不该把所有在线用户踢下线。

密钥强度是**唯一**的安全边界：签名算法公开，谁拿到 `SESSION_SECRET` 谁就能自签
管理员会话。因此这里对弱密钥做了处理，而不是只打日志：
- 生产（APP_ENV=prod）弱密钥/默认密码 → 启动即抛错，拒绝带病上线；
- 开发（APP_ENV=dev）沿用仓库里的默认密钥时 → 本次进程改用**随机密钥**，
  使那个公开常量彻底失效（代价是重启后需重新登录）；
- 测试（APP_ENV=test）保持可预测，便于构造令牌。

注意：本模块不负责「拦截请求」，那一层在 `app/main.py` 的 AuthMiddleware。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.enums import LoginRole, LoginSource

logger = get_logger(__name__)

SESSION_COOKIE = "wb_session"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 小时
# 一次性 state（企业微信登录回调防篡改）的有效期：扫码可能需要几分钟，给足 10 分钟。
STATE_MAX_AGE = 60 * 10
# 允许 60 秒时钟漂移：签发时间落在未来更远的位置，视为伪造（旧实现只校验上界，
# 因此一个 iat 在未来的令牌永不失效）。
_CLOCK_SKEW_SECONDS = 60

# 仓库里公开的默认值 + 常见弱口令，一律视为「未配置」。
_INSECURE_SECRETS = frozenset(
    {
        "",
        "changeme",
        "secret",
        "dev-insecure-session-secret",
        "dev",
    }
)
_INSECURE_PASSWORDS = frozenset({"", "changeme", "admin", "password", "123456", "12345678"})
_MIN_SECRET_LENGTH = 32
_MIN_PASSWORD_LENGTH = 12

# 开发环境进程内随机密钥（只生成一次，保证同一进程签发/校验一致）。
_ephemeral_secret: str | None = None


def is_weak_secret(value: str | None) -> bool:
    secret = (value or "").strip()
    return secret in _INSECURE_SECRETS or len(secret) < _MIN_SECRET_LENGTH


def is_weak_password(value: str | None) -> bool:
    password = (value or "").strip()
    return password.lower() in _INSECURE_PASSWORDS or len(password) < _MIN_PASSWORD_LENGTH


def ensure_secure_settings(settings: Settings) -> list[str]:
    """启动期自检：返回非阻塞的告警列表，弱凭据在生产环境直接抛错。

    生产必须「配错就起不来」，而不是启动成功后在日志里留一行容易被忽略的警告 ——
    用默认密钥时，任何人算出签名即可伪造管理员会话，绕过密码访问全部接口。
    """
    warnings: list[str] = []
    weak_secret = is_weak_secret(settings.session_secret)
    weak_password = is_weak_password(settings.admin_password)

    if settings.app_env == "prod":
        problems: list[str] = []
        if weak_secret:
            problems.append(
                "SESSION_SECRET 未配置或强度不足（需 ≥32 位随机值）"
            )
        if weak_password:
            problems.append(
                "ADMIN_PASSWORD 未配置或强度不足（需 ≥12 位，且不能是 changeme 等常见口令）"
            )
        if problems:
            raise RuntimeError(
                "生产环境安全配置无效：" + "；".join(problems) + "。"
                "请通过环境变量覆盖后重启（见 .env.example）。"
            )
        if not settings.cookie_secure:
            warnings.append(
                "COOKIE_SECURE=false：会话 Cookie 会经明文 HTTP 传输，"
                "内网 HTTPS 部署时应设为 true。"
            )
    elif settings.app_env == "dev":
        if weak_secret:
            warnings.append(
                "SESSION_SECRET 未配置或强度不足：本次进程将使用随机密钥，"
                "重启后需重新登录（生产必须配置固定随机值）。"
            )
        if weak_password:
            warnings.append("ADMIN_PASSWORD 使用默认值，仅适用于本机开发。")
    return warnings


def signing_secret() -> str:
    """解析用于签名会话令牌的密钥。

    弱密钥在开发环境走进程内随机值，使仓库里公开的默认常量无法被用来伪造令牌。
    """
    global _ephemeral_secret
    settings = get_settings()
    configured = (settings.session_secret or "").strip()
    if not is_weak_secret(configured):
        return configured
    if settings.app_env == "prod":
        # 正常启动路径已在 ensure_secure_settings 拦截，这里兜底防止绕过 create_app。
        raise RuntimeError(
            "生产环境 SESSION_SECRET 未配置或强度不足，拒绝签发/校验会话。"
        )
    if settings.app_env == "test":
        # 测试保持可预测，便于用例自行构造令牌验证校验逻辑。
        return configured
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_urlsafe(48)
        logger.warning(
            "SESSION_SECRET 未配置或强度不足：已改用本次进程的随机密钥，"
            "重启后所有会话失效。生产部署请配置固定随机值。"
        )
    return _ephemeral_secret


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sign_payload(payload: dict[str, Any], secret: str | None = None) -> str:
    """对任意 JSON 载荷做自签，用于会话令牌与一次性 state。

    刻意不引入 JWT 依赖：格式是 `<base64url(json)>.<hmac-sha256 hex>`，
    载荷本身不加密（调用方不得放敏感信息）。
    """
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _sign(secret or signing_secret(), payload_b64.encode("ascii"))
    return f"{payload_b64}.{sig}"


def read_signed_payload(
    token: str | None,
    *,
    max_age: int,
    secret: str | None = None,
    require_exp: bool = True,
) -> dict[str, Any] | None:
    """校验并解出自签载荷；任何一步不通过都返回 None（失败安全）。"""
    if not token:
        return None
    try:
        payload_b64, sig = token.split(".", 1)
        expected = _sign(secret or signing_secret(), payload_b64.encode("ascii"))
    except (ValueError, RuntimeError):
        return None
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    now = time.time()
    try:
        issued = int(data.get("iat", 0))
    except (TypeError, ValueError):
        return None
    if issued > now + _CLOCK_SKEW_SECONDS:
        # 签发时间在未来 = 伪造（旧实现只查上界，这类令牌永不失效）。
        return None
    expires = data.get("exp")
    if isinstance(expires, (int, float)):
        deadline = int(expires)
    elif require_exp:
        return None
    else:
        # 兼容改造前签发的无 exp 令牌，按 iat + 约定上限判定。
        deadline = issued + max_age
    if now > deadline:
        return None
    return data


@dataclass(frozen=True)
class SessionUser:
    """已通过认证的会话主体。

    注意 `role` 只是**令牌签发时**的角色，实际权限以每个请求复核后的结论为准
    （见 app/main.py 的 AuthMiddleware）。
    """

    subject: str
    role: str
    source: str
    name: str
    employee_id: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == LoginRole.ADMIN


def create_session_token(
    subject: str = "admin",
    *,
    role: str = LoginRole.ADMIN,
    source: str = LoginSource.PASSWORD,
    name: str = "管理员",
    employee_id: int | None = None,
    max_age: int = SESSION_MAX_AGE,
) -> str:
    """生成签名会话令牌（含签发时间与过期时间）。"""
    issued = int(time.time())
    payload: dict[str, Any] = {
        "iat": issued,
        "exp": issued + max_age,
        "sub": subject,
        "role": role,
        "src": source,
        "name": name,
    }
    if employee_id is not None:
        payload["uid"] = employee_id
    return sign_payload(payload)


def read_session_token(token: str | None) -> SessionUser | None:
    """校验会话令牌并解出会话主体；任何异常都返回 None。"""
    data = read_signed_payload(
        token, max_age=SESSION_MAX_AGE, require_exp=False
    )
    if data is None:
        return None
    subject = str(data.get("sub") or "")
    if not subject:
        return None

    if "role" not in data and "src" not in data:
        # 旧格式令牌：只能是管理员密码会话。保留它是为了避免升级时
        # 把所有已登录用户踢下线；新会话一律带 role/src。
        if subject != "admin":
            return None
        return SessionUser(
            subject=subject,
            role=LoginRole.ADMIN,
            source=LoginSource.PASSWORD,
            name="管理员",
        )

    role = str(data.get("role") or "")
    source = str(data.get("src") or "")
    if role not in LoginRole.all() or source not in LoginSource.all():
        return None
    if source == LoginSource.PASSWORD:
        # 密码路径只有一个部署级超管，强制 admin：令牌被改成 member 也提权不了，
        # 反过来「密码会话被降级」会让老板把自己锁在外面。
        if subject != "admin" or role != LoginRole.ADMIN:
            return None
        return SessionUser(
            subject=subject, role=role, source=source, name=str(data.get("name") or "管理员")
        )

    employee_id = data.get("uid")
    if not isinstance(employee_id, int):
        return None
    name = str(data.get("name") or subject)
    return SessionUser(
        subject=subject, role=role, source=source, name=name, employee_id=employee_id
    )


def verify_session_token(token: str | None) -> bool:
    """校验会话令牌：结构 + 签名 + 有效期，全部失败安全（False）。"""
    return read_session_token(token) is not None


def verify_password(password: str) -> bool:
    """恒定时间比对登录密码。

    必须按 bytes 比对：`hmac.compare_digest` 对含非 ASCII 的 str 会抛 TypeError，
    于是「输入中文密码」这种输入会变成 500 而不是 401。
    """
    settings = get_settings()
    return hmac.compare_digest(
        (password or "").encode("utf-8"),
        (settings.admin_password or "").encode("utf-8"),
    )
