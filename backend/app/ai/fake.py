"""假模型：测试与「还没有 API Key」时的替身。

刻意做成可注入固定回复 + 记录调用历史，这样上层 Service 的解析逻辑
（尤其是要求模型输出 JSON 的场景）可以在不花钱、不联网的前提下被完整测试。

Agent 循环需要「第几次调用返回什么」的脚本能力，所以这里除了固定回复之外
还支持 ``tool_call_script``：按顺序吐工具调用，脚本用尽后才给最终回答。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from app.ai.base import (
    LLMProvider,
    LLMResult,
    LLMStreamChunk,
    Message,
    ToolCall,
    ToolSchema,
)


@dataclass
class FakeProvider(LLMProvider):
    """按预设回复应答，并记录收到的提示词。"""

    response: str = "{}"
    model: str = "fake-model"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    http_request_count: int | None = None
    calls: list[dict[str, str]] = field(default_factory=list)
    option_calls: list[dict[str, Any]] = field(default_factory=list)
    # Agent 循环脚本：每调用一次 chat_messages 消费一项；空列表表示这一回合直接回答
    tool_call_script: list[list[ToolCall]] = field(default_factory=list)
    message_calls: list[dict[str, Any]] = field(default_factory=list)
    # 流式输出时每帧的字符数；设为 0 表示一次性吐完
    stream_chunk_size: int = 0

    def _result(self, content: str | None = None, **kwargs: Any) -> LLMResult:
        return LLMResult(
            content=self.response if content is None else content,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            prompt_cache_hit_tokens=self.prompt_cache_hit_tokens,
            prompt_cache_miss_tokens=self.prompt_cache_miss_tokens,
            http_request_count=self.http_request_count,
            **kwargs,
        )

    def chat(self, system: str, user: str) -> LLMResult:
        self.calls.append({"system": system, "user": user})
        return self._result()

    def chat_with_options(
        self,
        system: str,
        user: str,
        *,
        thinking: bool | None = None,
        json_mode: bool | None = None,
    ) -> LLMResult:
        options = {"thinking": thinking}
        if json_mode is not None:
            options["json_mode"] = json_mode
        self.option_calls.append(options)
        return self.chat(system, user)

    def chat_messages(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> LLMResult:
        self.message_calls.append({"messages": list(messages), "tools": tools})
        # 不带工具清单就调不了工具 —— 与真实协议一致，所以收尾回合必须走 response
        if tools and self.tool_call_script:
            scripted = self.tool_call_script.pop(0)
            if scripted:
                return self._result(content="", tool_calls=scripted)
        return self._result()

    def chat_messages_stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[LLMStreamChunk]:
        result = self.chat_messages(messages, tools)
        size = self.stream_chunk_size
        if result.content:
            if size and size > 0:
                for start in range(0, len(result.content), size):
                    yield LLMStreamChunk(text=result.content[start : start + size])
            else:
                yield LLMStreamChunk(text=result.content)
        yield LLMStreamChunk(done=True, result=result)
