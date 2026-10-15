"""大模型抽象层。

职责边界（与 Connector 平行、互不相干）：

    Connector 负责「把外部系统的数据原样取回来」，**不调 AI**。
    本层负责「把已经入库的业务数据交给模型，再把结论带回来」，
    **不直接写业务表** —— 落库由上层 Service 决定（目前统一写 AIAnalysis 留痕）。

错误体系刻意与 `app/connectors/base.py` 同构：调用方（API 层）用同一套
「哪种错误 -> 什么 HTTP 状态码」的映射心智处理外部系统故障。

新增模型厂商：实现 `LLMProvider` 即可，`ai_provider` 配置切换，业务代码零改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """大模型调用基础异常。"""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        http_request_count: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        prompt_cache_hit_tokens: int | None = None,
        prompt_cache_miss_tokens: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_request_count = http_request_count
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_cache_hit_tokens = prompt_cache_hit_tokens
        self.prompt_cache_miss_tokens = prompt_cache_miss_tokens


class LLMAuthError(LLMError):
    """API Key 缺失或无效：重试无用，需要人工配置。"""

    retryable = False


class LLMRateLimitError(LLMError):
    """触发限流：可重试。"""

    retryable = True


class LLMResponseError(LLMError):
    """模型返回了无法解析/不符合约定的内容。"""

    retryable = True


Message = dict[str, Any]
"""OpenAI 兼容的消息。role 可能是 system / user / assistant / tool。"""

ToolSchema = dict[str, Any]
"""OpenAI 兼容的 tools 声明（{"type": "function", "function": {...}}）。"""


@dataclass
class ToolCall:
    """模型发起的一次工具调用请求。

    ``arguments`` 保留模型的**原始 JSON 字符串**：解析失败属于业务判断
    （要记进可观测的检索步骤里），不该被吞在传输层。
    """

    id: str
    name: str
    arguments: str


@dataclass
class LLMResult:
    """一次模型调用的产物。"""

    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    http_request_count: int | None = None
    # 模型要求调用的工具（Agent 循环用）。为空表示这一回合是最终回答。
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class LLMUsage:
    """一次业务分析中多个模型请求的用量合计；缺失字段保持未知而不伪报为 0。"""

    model_call_count: int = 0
    _totals: dict[str, int] = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "http_request_count": 0,
        }
    )
    _known: dict[str, bool] = field(
        default_factory=lambda: {
            "prompt_tokens": True,
            "completion_tokens": True,
            "prompt_cache_hit_tokens": True,
            "prompt_cache_miss_tokens": True,
            "http_request_count": True,
        }
    )

    def record(self, usage_source: object) -> None:
        """记录一次 Provider 调用的结果或异常中的用量。"""
        self.model_call_count += 1
        for key in self._totals:
            value = getattr(usage_source, key, None)
            if value is None:
                self._known[key] = False
                continue
            self._totals[key] += max(0, int(value))

    def value(self, key: str) -> int | None:
        if self.model_call_count == 0:
            return None
        return self._totals[key] if self._known[key] else None

    @property
    def prompt_tokens(self) -> int | None:
        return self.value("prompt_tokens")

    @property
    def completion_tokens(self) -> int | None:
        return self.value("completion_tokens")

    def as_metadata(self) -> dict[str, int | None]:
        return {
            "model_call_count": self.model_call_count,
            "http_request_count": self.value("http_request_count"),
            "prompt_cache_hit_tokens": self.value("prompt_cache_hit_tokens"),
            "prompt_cache_miss_tokens": self.value("prompt_cache_miss_tokens"),
        }


@dataclass
class LLMStreamChunk:
    """流式调用的一帧。

    ``text`` 是增量正文；``done=True`` 的那一帧一定带 ``result``，
    里面是完整结果（含 tool_calls 与 usage），供调用方决定下一步。
    """

    text: str = ""
    done: bool = False
    result: LLMResult | None = None


class LLMProvider(ABC):
    """大模型调用抽象。

    两个入口：

    - ``chat_messages(messages, tools)``：多轮入口，支持工具调用（Agent 用）。
    - ``chat(system, user)``：单轮纯文本便捷入口，既有调用方全部走这里。

    新能力一律以**新增方法**的方式加，不改 ``chat`` 的签名 —— Agent 这条新链路
    出问题时旧链路完全不受影响，可以单独回滚。
    """

    @abstractmethod
    def chat_messages(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> LLMResult:
        """发送多轮消息，返回模型回合结果（可能带工具调用）。"""
        raise NotImplementedError

    def chat(self, system: str, user: str) -> LLMResult:
        """发送一条系统指令 + 一条用户消息，返回模型文本。"""
        return self.chat_messages(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )

    def chat_with_options(
        self,
        system: str,
        user: str,
        *,
        thinking: bool | None = None,
        json_mode: bool | None = None,
    ) -> LLMResult:
        """发送单轮文本请求，并可指定厂商支持的思考模式。

        默认实现忽略厂商专属选项，保持 Fake 和其他兼容 Provider 可直接复用。
        """
        if json_mode:
            raise LLMError("当前模型 Provider 不支持按调用启用 JSON 输出模式")
        return self.chat(system, user)

    def chat_messages_stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[LLMStreamChunk]:
        """流式多轮调用。

        默认实现退化为「一次性返回」：这样任何只实现了 ``chat_messages`` 的
        厂商（含测试替身）都能直接被流式链路使用，不必各写一遍。
        """
        result = self.chat_messages(messages, tools)
        if result.content:
            yield LLMStreamChunk(text=result.content)
        yield LLMStreamChunk(done=True, result=result)
