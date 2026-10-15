"""Embedding 客户端：OpenAI 兼容 ``/embeddings`` 端点。

与 ``deepseek.py`` 同一层（只传输、不业务判断、不写库）。独立于 chat Provider：
embedding 服务商通常与 chat 不是同一家，配置与出站白名单都分开。

安全边界与 chat 一致：出站主机必须在部署侧 ``ai_embedding_allowed_hosts``
白名单内，设置页放不开这道闸。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.base import LLMAuthError, LLMError, LLMRateLimitError
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.url_guard import host_allowed

logger = get_logger(__name__)

EMBEDDINGS_PATH = "/embeddings"


class EmbeddingClient:
    """OpenAI 兼容 embeddings 客户端。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.Client | None = None

    def configured(self) -> bool:
        """三项配置齐备 + 出站主机在白名单内，语义检索才可用。"""
        s = self.settings
        if not (s.ai_embedding_base_url and s.ai_embedding_api_key and s.ai_embedding_model):
            return False
        return host_allowed(_host(s.ai_embedding_base_url), s.ai_embedding_allowed_hosts)

    def unavailable_reason(self) -> str:
        """未启用时给管理员的可操作提示（不出现在用户面）。"""
        s = self.settings
        if not (s.ai_embedding_base_url and s.ai_embedding_api_key and s.ai_embedding_model):
            return "语义检索未配置：请在系统设置补全 Embedding 服务地址、密钥与模型。"
        if not host_allowed(_host(s.ai_embedding_base_url), s.ai_embedding_allowed_hosts):
            return (
                f"语义检索出站主机 { _host(s.ai_embedding_base_url) } 不在 "
                "ai_embedding_allowed_hosts 白名单内，需部署侧配置后才能启用。"
            )
        return ""

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.settings.ai_embedding_base_url.rstrip("/"),
                timeout=self.settings.ai_timeout,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化；分批按 ``ai_embedding_batch_size`` 拆发，返回顺序与输入一致。"""
        if not self.configured():
            raise LLMAuthError(self.unavailable_reason())
        if not texts:
            return []
        vectors: list[list[float]] = []
        batch = self.settings.ai_embedding_batch_size
        for start in range(0, len(texts), batch):
            vectors.extend(self._request(texts[start : start + batch]))
        return vectors

    def _request(self, texts: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": self.settings.ai_embedding_model, "input": texts}
        try:
            response = self._ensure_client().post(EMBEDDINGS_PATH, json=body)
        except httpx.TimeoutException as exc:
            raise LLMError(f"Embedding 请求超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Embedding 请求失败：{exc}") from exc

        if response.status_code == 401:
            raise LLMAuthError("Embedding API Key 无效或缺失。")
        if response.status_code == 429:
            raise LLMRateLimitError("Embedding 服务触发限流。")
        if response.status_code >= 400:
            raise LLMError(
                f"Embedding 服务返回 {response.status_code}：{response.text[:300]}"
            )
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise LLMError("Embedding 返回结构不符合预期（data 缺失或数量不一致）。")
        # 按上游约定的 index 归位，防止个别实现乱序返回。
        ordered: list[tuple[int, list[float]]] = []
        for item in data:
            embedding = item.get("embedding")
            index = int(item.get("index", 0))
            if not isinstance(embedding, list) or not embedding:
                raise LLMError("Embedding 返回缺少向量内容。")
            ordered.append((index, [float(value) for value in embedding]))
        ordered.sort(key=lambda pair: pair[0])
        return [vector for _, vector in ordered]


def _host(base_url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(base_url).hostname or ""


_client: EmbeddingClient | None = None


def get_embedding_client(settings: Settings | None = None) -> EmbeddingClient:
    """进程级单例；换配置的场景（测试/运行时设置变更）直接传 settings 新建。"""
    global _client
    if settings is not None:
        return EmbeddingClient(settings)
    if _client is None:
        _client = EmbeddingClient()
    return _client
