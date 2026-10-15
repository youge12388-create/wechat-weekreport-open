"""百度大模型文本翻译 API Provider。"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.translation.base import (
    TranslationAuthError,
    TranslationError,
    TranslationRateLimitError,
    TranslationResult,
)

TRANSLATE_PATH = "/ait/api/aiTextTranslate"
MAX_CHUNK_CHARS = 1800


def split_translation_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """优先按行分块，单行过长再硬切；绝不截断正文。"""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True) or [text]:
        remaining = line
        while remaining:
            room = limit - len(current)
            if room <= 0:
                chunks.append(current)
                current = ""
                room = limit
            take = remaining[:room]
            current += take
            remaining = remaining[room:]
            if len(current) >= limit:
                chunks.append(current)
                current = ""
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


class BaiduTranslationProvider:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _auth_headers_and_payload(self, text: str) -> tuple[dict[str, str], dict[str, Any]]:
        app_id = self.settings.baidu_translate_app_id.strip()
        if not app_id:
            raise TranslationAuthError("未配置百度翻译 AppID，请在系统设置中录入。")
        payload: dict[str, Any] = {
            "appid": app_id,
            "from": "auto",
            "to": "zh",
            "q": text,
            "model_type": "llm",
            "reference": "将企业内部周报准确、自然地翻译为中文；保留项目名、专有名词、数字与列表结构。",
        }
        api_key = self.settings.baidu_translate_api_key.strip()
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}, payload
        secret_key = self.settings.baidu_translate_secret_key.strip()
        if not secret_key:
            raise TranslationAuthError("未配置百度翻译 API Key 或密钥，请在系统设置中录入。")
        salt = secrets.token_hex(8)
        sign_source = f"{app_id}{text}{salt}{secret_key}"
        payload.update(
            {
                "salt": salt,
                "sign": hashlib.md5(sign_source.encode("utf-8")).hexdigest(),  # noqa: S324 百度协议指定 MD5 签名
            }
        )
        return {}, payload

    def translate(self, text: str) -> TranslationResult:
        text = (text or "").strip()
        if not text:
            raise ValueError("周报正文为空，无法翻译。")
        translated: list[str] = []
        source_language: str | None = None
        chunks = split_translation_text(text)
        try:
            with httpx.Client(
                base_url=self.settings.baidu_translate_base_url.rstrip("/"),
                timeout=self.settings.baidu_translate_timeout,
            ) as client:
                for chunk in chunks:
                    headers, payload = self._auth_headers_and_payload(chunk)
                    response = client.post(
                        TRANSLATE_PATH,
                        json=payload,
                        headers={"Content-Type": "application/json", **headers},
                    )
                    if response.status_code in (401, 403):
                        raise TranslationAuthError("百度翻译鉴权失败，请检查 AppID 与密钥。")
                    if response.status_code == 429:
                        raise TranslationRateLimitError("百度翻译限流，请稍后重试。")
                    if response.status_code >= 400:
                        raise TranslationError(f"百度翻译服务异常（HTTP {response.status_code}）。")
                    body = response.json()
                    if body.get("error_code"):
                        message = str(body.get("error_msg") or "未知错误")[:200]
                        raise TranslationError(f"百度翻译失败：{message}")
                    result = body.get("trans_result") or []
                    destination = "".join(
                        str(item.get("dst") or "")
                        for item in result
                        if isinstance(item, dict)
                    )
                    if not destination:
                        raise TranslationError("百度翻译返回为空。")
                    translated.append(destination)
                    source_language = source_language or body.get("from")
        except httpx.TimeoutException as exc:
            raise TranslationError("百度翻译请求超时，请稍后重试。") from exc
        except httpx.HTTPError as exc:
            raise TranslationError("无法连接百度翻译服务。") from exc
        return TranslationResult(
            content="".join(translated),
            source_language=str(source_language) if source_language else None,
            chunks=len(chunks),
        )
