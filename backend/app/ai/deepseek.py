"""DeepSeek（OpenAI 兼容 chat/completions）实现。

只负责「把消息发出去、把结果拿回来」，不做任何业务判断、不写库。
换厂商只需替换本文件或新增一个 LLMProvider 实现。

同时提供两种调用形态：

- 非流式（``chat_messages``）：Agent 的工具回合用它，因为工具调用必须拿到完整结果。
- 流式（``chat_messages_stream``）：把上游 SSE 的增量正文原样转发给上层。

工具调用的参数**不做解析**，原样交给上层 —— 解析失败是业务可观测事件，不是传输层的事。
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Iterator
from typing import Any

import httpx

from app.ai.base import (
    LLMAuthError,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponseError,
    LLMResult,
    LLMStreamChunk,
    Message,
    ToolCall,
    ToolSchema,
)
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CHAT_PATH = "/chat/completions"


class DeepSeekProvider(LLMProvider):
    """DeepSeek / OpenAI 兼容接口。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.Client | None = None

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.settings.ai_base_url.rstrip("/"),
                timeout=self.settings.ai_timeout,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> DeepSeekProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 请求组装 ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self.settings.ai_api_key:
            # 与企微 Connector 一致：缺凭据要给可操作的提示，而不是 500 堆栈
            raise LLMAuthError(
                "未配置 DeepSeek API Key。请前往系统设置 → AI 与翻译录入密钥，"
                "或通过 AI_API_KEY 环境变量提供部署兜底值。"
            )
        return {
            "Authorization": f"Bearer {self.settings.ai_api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        stream: bool,
        thinking: bool | None = None,
        json_mode: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.ai_model,
            "messages": messages,
            "temperature": self.settings.ai_temperature,
            "stream": stream,
        }
        if thinking is not None:
            payload["thinking"] = {
                "type": "enabled" if thinking else "disabled"
            }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.settings.ai_max_output_tokens:
            # 不设上限时，超长结构化输出可能被上游在中途截断，
            # 于是 extract_json_object 解析失败 -> 按次数重试 -> 每次都计费。
            payload["max_tokens"] = self.settings.ai_max_output_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stream:
            # 关键：OpenAI 兼容接口在流式响应里**默认不返回 usage**，
            # 必须显式索取，否则 token 用量永远是 None —— 而 Agent 是所有路径里
            # 最贵的（多轮 + 上下文递增），用量账本却显示 0，正是治理要防的盲区。
            # 个别兼容实现不认这个字段，可用 ai_stream_include_usage=false 关掉。
            if self.settings.ai_stream_include_usage:
                payload["stream_options"] = {"include_usage": True}
        return payload

    def _backoff_seconds(self, attempt: int) -> float:
        """指数退避 + 抖动。

        旧实现在重试之间**不等待**：429 或上游 5xx 会在毫秒内被连打三次，
        必然还是同样的错误，等于把重试预算白烧掉。
        """
        base = self.settings.ai_retry_base_seconds
        delay = base * (2 ** max(attempt - 1, 0))
        # 抖动避免多个 Worker 同频重试形成尖峰
        return min(delay, self.settings.ai_retry_max_seconds) * (
            0.75 + random.random() * 0.5
        )

    @staticmethod
    def _retry_or_raise(
        response: httpx.Response, attempt: int, attempts: int
    ) -> LLMError | None:
        """把 HTTP 状态映射成「重试」或「直接抛」。返回异常对象表示应重试。"""
        if response.status_code in (401, 403):
            raise LLMAuthError(
                f"模型鉴权失败（HTTP {response.status_code}）：请检查 AI_API_KEY 是否有效。",
                http_request_count=attempt,
            )
        if response.status_code == 429:
            if attempt < attempts:
                logger.warning("模型限流，准备重试（第 %s/%s 次）", attempt, attempts)
                return LLMRateLimitError("模型限流", http_request_count=attempt)
            raise LLMRateLimitError("模型限流，请稍后重试", http_request_count=attempt)
        if response.status_code >= 500:
            if attempt < attempts:
                logger.warning(
                    "模型服务异常 HTTP %s（第 %s/%s 次）",
                    response.status_code,
                    attempt,
                    attempts,
                )
                return LLMError(
                    f"模型服务异常 {response.status_code}",
                    http_request_count=attempt,
                )
            raise LLMError(
                f"模型服务异常（HTTP {response.status_code}）",
                http_request_count=attempt,
            )
        if response.status_code >= 400:
            # 上游错误体不直接回给调用方：它可能含请求上下文，
            # 只在日志里留一份，对外给通用说明。
            logger.warning(
                "模型调用失败 HTTP %s：%s", response.status_code, response.text[:500]
            )
            raise LLMError(
                f"模型调用失败（HTTP {response.status_code}）：请检查模型名与参数是否被上游接受。",
                http_request_count=attempt,
            )
        return None

    @staticmethod
    def _parse(
        body: dict[str, Any], *, http_request_count: int | None = None
    ) -> LLMResult:
        usage = body.get("usage") or {} if isinstance(body, dict) else {}
        usage_fields = {
            "http_request_count": http_request_count,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
            "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
        }
        try:
            choice = body["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(
                f"模型返回结构异常：{str(body)[:300]}", **usage_fields
            ) from exc

        # finish_reason=length 意味着输出被 max_tokens 拦腰截断。结构化调用方
        # 拿到残缺 JSON 只会报「未找到 JSON 对象」，完全看不出真病因；
        # 且重试同样会截断，白烧重试预算。必须在这里直接报出可操作的错误。
        if choice.get("finish_reason") == "length":
            raise LLMResponseError(
                "模型输出因达到 max_tokens 上限被截断（finish_reason=length）。"
                "请调大 AI_MAX_OUTPUT_TOKENS 环境变量后重试。",
                **usage_fields,
            )

        tool_calls: list[ToolCall] = []
        for index, item in enumerate(message.get("tool_calls") or []):
            function = item.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=str(function.get("arguments") or "{}"),
                )
            )

        return LLMResult(
            content=(content or "").strip(),
            model=str(body.get("model") or ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            prompt_cache_hit_tokens=usage.get("prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=usage.get("prompt_cache_miss_tokens"),
            http_request_count=http_request_count,
            tool_calls=tool_calls,
        )

    # ---- 非流式 -----------------------------------------------------------

    def chat_messages(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> LLMResult:
        return self._chat_messages(messages, tools, thinking=None, json_mode=None)

    def chat_with_options(
        self,
        system: str,
        user: str,
        *,
        thinking: bool | None = None,
        json_mode: bool | None = None,
    ) -> LLMResult:
        """单轮文本请求的 DeepSeek 专属选项。"""
        return self._chat_messages(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=None,
            thinking=thinking,
            json_mode=json_mode,
        )

    def _chat_messages(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        *,
        thinking: bool | None,
        json_mode: bool | None,
    ) -> LLMResult:
        payload = self._payload(
            messages, tools, stream=False, thinking=thinking, json_mode=json_mode
        )
        headers = self._headers()
        attempts = self.settings.ai_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._ensure_client().post(
                    CHAT_PATH, json=payload, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                logger.warning("模型调用超时（第 %s/%s 次）", attempt, attempts)
                if attempt < attempts:
                    time.sleep(self._backoff_seconds(attempt))
                continue
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"模型服务连接失败：{exc}", http_request_count=attempt
                ) from exc

            retry = self._retry_or_raise(response, attempt, attempts)
            if retry is not None:
                last_error = retry
                # 退避后再试：毫秒内连打三次 429 只会拿到同样的 429
                time.sleep(self._backoff_seconds(attempt))
                continue
            try:
                body = response.json()
            except ValueError as exc:
                raise LLMResponseError(
                    "模型服务返回内容不是有效 JSON。",
                    http_request_count=attempt,
                ) from exc
            return self._parse(body, http_request_count=attempt)

        raise LLMError(
            f"模型调用失败：{last_error}", http_request_count=attempts
        )

    # ---- 流式 -------------------------------------------------------------

    def chat_messages_stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[LLMStreamChunk]:
        """把上游 SSE 原样转成 LLMStreamChunk。

        工具调用在流式响应里是**分片**下发的（按 index 增量拼 id / name / arguments），
        所以这里必须累积到流结束再交付完整结果。
        """
        payload = self._payload(messages, tools, stream=True)
        headers = self._headers()
        attempts = self.settings.ai_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            collected: list[str] = []
            buffered: dict[int, dict[str, str]] = {}
            model_name = ""
            prompt_tokens: int | None = None
            completion_tokens: int | None = None
            prompt_cache_hit_tokens: int | None = None
            prompt_cache_miss_tokens: int | None = None
            # 记录是否真的收到过 SSE 数据帧。上游 200 但返回 HTML/JSON 错误页时，
            # 行里不会有 "data:"，循环静默走完 —— 旧实现会把这种情况当成「成功但回答为空」，
            # 状态显示绿色成功、也不进「上次调用失败」，故障被完全掩盖。
            saw_data_frame = False

            try:
                with self._ensure_client().stream(
                    "POST", CHAT_PATH, json=payload, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        retry = self._retry_or_raise(response, attempt, attempts)
                        if retry is not None:
                            last_error = retry
                            time.sleep(self._backoff_seconds(attempt))
                            continue

                    for line in response.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            saw_data_frame = True
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        saw_data_frame = True

                        model_name = chunk.get("model") or model_name
                        usage = chunk.get("usage") or {}
                        if usage:
                            prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                            completion_tokens = usage.get(
                                "completion_tokens", completion_tokens
                            )
                            prompt_cache_hit_tokens = usage.get(
                                "prompt_cache_hit_tokens", prompt_cache_hit_tokens
                            )
                            prompt_cache_miss_tokens = usage.get(
                                "prompt_cache_miss_tokens", prompt_cache_miss_tokens
                            )

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}

                        text = delta.get("content")
                        if text:
                            collected.append(text)
                            yield LLMStreamChunk(text=text)

                        for item in delta.get("tool_calls") or []:
                            index = int(item.get("index") or 0)
                            slot = buffered.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            if item.get("id"):
                                slot["id"] = str(item["id"])
                            function = item.get("function") or {}
                            if function.get("name"):
                                slot["name"] += str(function["name"])
                            if function.get("arguments"):
                                slot["arguments"] += str(function["arguments"])
            except httpx.TimeoutException as exc:
                last_error = exc
                logger.warning("模型流式调用超时（第 %s/%s 次）", attempt, attempts)
                if attempt < attempts:
                    time.sleep(self._backoff_seconds(attempt))
                continue
            except httpx.HTTPError as exc:
                raise LLMError(
                    f"模型服务连接失败：{exc}", http_request_count=attempt
                ) from exc

            content = "".join(collected).strip()
            if not saw_data_frame:
                # 200 但没有任何 SSE 帧：几乎一定是网关/代理返回了错误页或空响应。
                # 必须报错，不能被当成「成功但没说话」—— 否则状态页显示绿色成功，
                # 运维根本看不到故障。
                last_error = LLMResponseError(
                    "模型流式响应没有任何数据帧（上游可能返回了错误页或非 SSE 内容）",
                    http_request_count=attempt,
                )
                logger.warning(
                    "模型流式响应无数据帧（第 %s/%s 次）", attempt, attempts
                )
                if attempt < attempts:
                    time.sleep(self._backoff_seconds(attempt))
                    continue
                raise last_error

            tool_calls = [
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
                for index, slot in sorted(buffered.items())
            ]
            yield LLMStreamChunk(
                done=True,
                result=LLMResult(
                    content=content,
                    model=model_name or self.settings.ai_model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    prompt_cache_hit_tokens=prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens=prompt_cache_miss_tokens,
                    http_request_count=attempt,
                    tool_calls=tool_calls,
                ),
            )
            return

        raise LLMError(
            f"模型调用失败：{last_error}", http_request_count=attempts
        )
