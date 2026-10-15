"""出站 Base URL 校验（AI 密钥外发的唯一闸门）。

为什么必须有白名单：`ai_base_url` 是设置页可写的**任意** URL，而下一次 AI 调用
会把**已保存的 API Key** 放进 `Authorization` 头发到该地址。因此
「PATCH base_url 改成自己的主机 → 点一次连通性测试」就等于把公司的真实模型
密钥外发出去；允许 `http://` 时它还会明文过网，内网地址也因此可达。

所以出站目标必须是**部署侧显式声明过**的主机（环境变量 `AI_ALLOWED_BASE_HOSTS`），
而不是设置页里可以随手改的字段 —— 若白名单本身能在设置页修改，它就不是控制措施。
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = ["normalize_base_url", "host_allowed", "validate_ai_base_url"]


def normalize_base_url(value: str) -> str:
    """规整并做**语法**校验（不含主机白名单）；不合法直接抛 ValueError。"""
    normalized = (value or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("Base URL 不能为空。")
    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头。")
    if not parsed.hostname:
        raise ValueError("Base URL 缺少主机名。")
    return normalized


def host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """主机是否在白名单内。

    支持两种写法：精确主机（`api.deepseek.com`）与前缀通配（`.example.com`
    匹配其任意子域）。刻意不做后缀包含匹配 —— `evil-api.deepseek.com.attacker.tld`
    这类域名会骗过后缀包含。
    """
    target = (host or "").strip().lower()
    if not target:
        return False
    for entry in allowed_hosts or []:
        candidate = (entry or "").strip().lower()
        if not candidate:
            continue
        if candidate.startswith("."):
            suffix = candidate[1:]
            if target == suffix or target.endswith("." + suffix):
                return True
        elif target == candidate:
            return True
    return False


def validate_ai_base_url(value: str | None, allowed_hosts: list[str]) -> str | None:
    """校验设置页提交的 Base URL：语法 + 主机白名单。"""
    if value is None:
        return None
    normalized = normalize_base_url(value)
    host = urlparse(normalized).hostname or ""
    if not host_allowed(host, allowed_hosts):
        allowed = "、".join(allowed_hosts) or "（未配置）"
        raise ValueError(
            f"不允许的 Base URL 主机：{host}。允许的主机：{allowed}。"
            "如需接入自建或其它兼容服务，请由运维在部署侧通过 "
            "AI_ALLOWED_BASE_HOSTS 显式添加，而不是在设置页填写。"
        )
    return normalized
