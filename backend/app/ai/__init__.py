"""AI 层入口：按配置选择具体大模型实现。"""

from __future__ import annotations

import inspect

from app.ai.base import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMResult,
    LLMProvider,
    LLMStreamChunk,
    LLMUsage,
    ToolCall,
)
from app.core.config import Settings
from app.services.runtime_config_service import runtime_settings

__all__ = [
    "LLMAuthError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMProvider",
    "LLMResult",
    "LLMStreamChunk",
    "LLMUsage",
    "ToolCall",
    "get_provider",
    "chat_with_options",
]


def get_provider(settings: Settings | None = None):
    """按 ai_provider 配置返回实现。fake 用于测试与无 Key 的本地开发。"""
    settings = settings or runtime_settings()
    if settings.ai_provider == "fake":
        from app.ai.fake import FakeProvider

        return FakeProvider()
    if settings.ai_provider == "deepseek":
        from app.ai.deepseek import DeepSeekProvider

        return DeepSeekProvider(settings)
    raise LLMError(f"未支持的 ai_provider：{settings.ai_provider}")


def chat_with_options(
    provider: LLMProvider,
    system: str,
    user: str,
    *,
    thinking: bool | None = None,
    json_mode: bool | None = None,
    usage: LLMUsage | None = None,
) -> LLMResult:
    """使用单轮 Provider 选项，并兼容尚未实现可选方法的旧测试替身。"""
    method = getattr(provider, "chat_with_options", None)
    try:
        if callable(method):
            options = {"thinking": thinking}
            if json_mode is not None:
                try:
                    parameters = inspect.signature(method).parameters.values()
                    supports_json_mode = any(
                        parameter.name == "json_mode"
                        or parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters
                    )
                except (TypeError, ValueError):
                    supports_json_mode = False
                if supports_json_mode:
                    options["json_mode"] = json_mode
            result = method(system, user, **options)
        else:
            result = provider.chat(system, user)
    except Exception as exc:
        if usage is not None:
            usage.record(exc)
        raise
    if usage is not None:
        usage.record(result)
    return result
