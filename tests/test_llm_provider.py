"""LLM Provider 传输层行为：usage 索取、退避重试、空流不算成功。

这些点都在「传输层」，与业务无关，因此用 httpx.MockTransport 直接打桩验证
「请求体长什么样、异常怎么映射」—— 不打网络，也不 mock 掉被测逻辑本身。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai.base import LLMResponseError, LLMRateLimitError
from app.ai.deepseek import DeepSeekProvider
from app.core.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "app_env": "test",
        "ai_api_key": "test-key",
        "ai_base_url": "https://api.deepseek.com",
        "ai_model": "deepseek-chat",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """拦掉退避等待并记录时长：测试不该真的睡，也不该为了加速而放宽生产校验下限。"""
    slept: list[float] = []
    monkeypatch.setattr("app.ai.deepseek.time.sleep", lambda s: slept.append(s))
    return slept


def _with_client(handler, overrides) -> DeepSeekProvider:
    """构造注入了 MockTransport 的 Provider（不真的打网络）。"""
    provider = DeepSeekProvider(_settings(**overrides))
    provider._client = httpx.Client(
        base_url="https://api.deepseek.com",
        transport=httpx.MockTransport(handler),
    )
    return provider


def _sse(*chunks: dict) -> str:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def test_stream_requests_usage_explicitly() -> None:
    """流式请求必须显式索取 usage。

    OpenAI 兼容接口在流式响应里默认不返回 usage，不索取就永远拿不到 token 数 ——
    Agent 是最贵的路径（多轮 + 上下文递增），用量账本却会显示 0。
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"choices": [{"delta": {"content": "好"}}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "prompt_cache_hit_tokens": 5,
                        "prompt_cache_miss_tokens": 2,
                    },
                },
            ),
        )

    provider = _with_client(handler, {})
    chunks = list(provider.chat_messages_stream([{"role": "user", "content": "hi"}]))

    assert captured.get("stream") is True
    assert captured.get("stream_options") == {"include_usage": True}
    assert "thinking" not in captured
    final = chunks[-1].result
    assert final is not None
    assert final.prompt_tokens == 7
    assert final.completion_tokens == 3
    assert final.prompt_cache_hit_tokens == 5
    assert final.prompt_cache_miss_tokens == 2
    assert final.http_request_count == 1


def test_stream_usage_can_be_disabled_for_incompatible_providers() -> None:
    """个别兼容实现不认 stream_options，必须能关掉（否则请求直接 400）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"choices": [{"delta": {"content": "好"}}]}),
        )

    provider = _with_client(handler, {"ai_stream_include_usage": False})
    list(provider.chat_messages_stream([{"role": "user", "content": "hi"}]))
    assert "stream_options" not in captured


def test_non_stream_requests_max_tokens() -> None:
    """非流式请求要带上 max_tokens：不设上限时超长输出会被上游截断，
    解析失败后按次数重试，每次都计费。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = _with_client(handler, {"ai_max_output_tokens": 1024})
    result = provider.chat_messages([{"role": "user", "content": "hi"}])
    assert captured["max_tokens"] == 1024
    assert result.http_request_count == 1


def test_thinking_mode_is_per_call_and_disabled_only_when_requested() -> None:
    """思考与 JSON 模式都只影响显式指定的单次调用。"""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 3,
                },
            },
        )

    provider = _with_client(handler, {})
    provider.chat("system", "regular request")
    result = provider.chat_with_options(
        "system", "report analysis", thinking=False, json_mode=True
    )

    assert "thinking" not in captured[0]
    assert "response_format" not in captured[0]
    assert captured[1]["thinking"] == {"type": "disabled"}
    assert captured[1]["response_format"] == {"type": "json_object"}
    assert result.prompt_cache_hit_tokens == 2
    assert result.prompt_cache_miss_tokens == 3
    assert result.http_request_count == 1


def test_thinking_mode_can_be_enabled_for_selected_call() -> None:
    """调用方也能为单次请求显式恢复思考模式。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "deepseek-flash", "choices": [{"message": {"content": "ok"}}]},
        )

    provider = _with_client(handler, {})
    provider.chat_with_options("system", "complex request", thinking=True)

    assert captured["thinking"] == {"type": "enabled"}


def test_zero_max_output_tokens_omits_field() -> None:
    """置 0 表示不限制，不能把 max_tokens=0 发出去（有的上游会当成不准输出）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "m", "choices": [{"message": {"content": "ok"}}]},
        )

    provider = _with_client(handler, {"ai_max_output_tokens": 0})
    provider.chat_messages([{"role": "user", "content": "hi"}])
    assert "max_tokens" not in captured


def test_default_max_output_tokens_is_8192() -> None:
    """默认 2048 会让大周报的结构化 JSON 输出被上游截断，
    表现为「未找到 JSON 对象」的迷惑报错（生产实锤过），所以默认必须是 8192。"""
    assert Settings(_env_file=None).ai_max_output_tokens == 8192


def test_truncated_output_raises_actionable_error() -> None:
    """finish_reason=length 说明输出被 max_tokens 截断。

    此时 JSON 必然残缺，下游只会报「未找到 JSON 对象」；且重试同样截断，
    不能进重试循环 —— 必须直接抛出指向 AI_MAX_OUTPUT_TOKENS 的错误。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {
                        "message": {"content": '{"summary":"本期…'},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 2048},
            },
        )

    provider = _with_client(handler, {"ai_max_retries": 2})
    with pytest.raises(LLMResponseError) as excinfo:
        provider.chat_messages([{"role": "user", "content": "hi"}])
    assert "AI_MAX_OUTPUT_TOKENS" in str(excinfo.value)
    assert excinfo.value.http_request_count == 1
    assert excinfo.value.prompt_tokens == 100
    assert excinfo.value.completion_tokens == 2048


def test_empty_stream_is_an_error_not_a_silent_success() -> None:
    """200 但没有任何 SSE 数据帧（网关错误页）必须报错。

    旧实现会静默走完循环、返回空回答并记为成功 —— 状态页显示绿色，
    运维看不到任何故障迹象。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content="<html>502 Bad Gateway</html>",
        )

    provider = _with_client(handler, {"ai_max_retries": 0})
    with pytest.raises(LLMResponseError):
        list(provider.chat_messages_stream([{"role": "user", "content": "hi"}]))


def test_rate_limit_retries_then_raises() -> None:
    """429 应重试，次数用尽后抛可重试的限流错误（不是静默成功）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    provider = _with_client(handler, {"ai_max_retries": 2})
    with pytest.raises(LLMRateLimitError) as excinfo:
        provider.chat_messages([{"role": "user", "content": "hi"}])
    assert calls["n"] == 3, "应重试到 ai_max_retries+1 次"
    assert excinfo.value.http_request_count == 3


def test_upstream_error_body_is_not_echoed_to_caller() -> None:
    """上游 4xx 原文不进异常消息：它可能含请求上下文，只应留在日志里。"""
    from app.ai.base import LLMError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "SECRET-INTERNAL-DETAIL"}},
        )

    provider = _with_client(handler, {"ai_max_retries": 0})
    with pytest.raises(LLMError) as excinfo:
        provider.chat_messages([{"role": "user", "content": "hi"}])
    assert "SECRET-INTERNAL-DETAIL" not in str(excinfo.value)
    assert "400" in str(excinfo.value), "仍要保留状态码便于排查"


def test_stream_timeout_retries_with_backoff(no_sleep: list[float]) -> None:
    """流式超时应重试而不是直接放弃，且重试之间要有等待。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("boom", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"choices": [{"delta": {"content": "ok"}}]}),
        )

    provider = _with_client(handler, {"ai_max_retries": 1})
    chunks = list(provider.chat_messages_stream([{"role": "user", "content": "hi"}]))

    assert chunks[-1].result is not None
    assert chunks[-1].result.content == "ok"
    assert chunks[-1].result.http_request_count == 2
    assert no_sleep, "重试之间必须有退避等待"
    assert all(s > 0 for s in no_sleep)
