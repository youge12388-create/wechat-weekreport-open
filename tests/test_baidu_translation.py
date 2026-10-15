from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.translation import baidu
from app.translation.baidu import BaiduTranslationProvider, split_translation_text
from app.translation.base import (
    TranslationAuthError,
    TranslationError,
    TranslationRateLimitError,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, body: dict[str, Any] | None = None):
        self.status_code = status_code
        self.body = body or {}

    def json(self) -> dict[str, Any]:
        return self.body


def _provider(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> BaiduTranslationProvider:
    settings = get_settings()
    values = {
        "baidu_translate_app_id": "test-app",
        "baidu_translate_api_key": "test-api-key",
        "baidu_translate_secret_key": "test-secret",
        "baidu_translate_base_url": "https://translation.example/",
        "baidu_translate_timeout": 3.0,
        **overrides,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return BaiduTranslationProvider(settings)


def _install_client(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[FakeResponse | Exception]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []
    clients: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any):
            clients.append(kwargs)

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def post(self, path: str, *, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
            requests.append({"path": path, "json": json, "headers": headers})
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(baidu.httpx, "Client", FakeClient)
    return requests, clients


def test_split_translation_text_preserves_all_characters_across_line_chunks() -> None:
    text = "abcde\nxy"
    chunks = split_translation_text(text, limit=4)

    assert chunks == ["abcd", "e\nxy"]
    assert "".join(chunks) == text
    assert split_translation_text("") == []


def test_translate_rejects_empty_text_and_missing_app_id_before_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _install_client(monkeypatch, [])
    provider = _provider(monkeypatch, baidu_translate_app_id=" ")

    with pytest.raises(ValueError, match="正文为空"):
        provider.translate("  ")
    with pytest.raises(TranslationAuthError, match="未配置百度翻译 AppID"):
        provider.translate("hello")
    assert requests == []


def test_translate_uses_bearer_key_and_joins_chunk_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, clients = _install_client(
        monkeypatch,
        [
            FakeResponse(body={"from": "en", "trans_result": [{"dst": "第一段"}]}),
            FakeResponse(body={"from": "fr", "trans_result": [{"dst": "第二段"}]}),
        ],
    )
    provider = _provider(monkeypatch)

    result = provider.translate("a" * 1801)

    assert result.content == "第一段第二段"
    assert result.source_language == "en"
    assert result.chunks == 2
    assert len(requests) == 2
    assert all(request["path"] == baidu.TRANSLATE_PATH for request in requests)
    assert all(request["headers"]["Authorization"] == "Bearer test-api-key" for request in requests)
    assert all(request["json"]["appid"] == "test-app" for request in requests)
    assert clients[0] == {"base_url": "https://translation.example", "timeout": 3.0}


def test_translate_signs_request_when_only_secret_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _install_client(
        monkeypatch,
        [FakeResponse(body={"trans_result": [{"dst": "翻译"}]})],
    )
    monkeypatch.setattr(baidu.secrets, "token_hex", lambda _size: "fixed-salt")
    provider = _provider(monkeypatch, baidu_translate_api_key="")

    result = provider.translate("hello")

    payload = requests[0]["json"]
    expected = hashlib.md5(
        b"test-apphellofixed-salttest-secret"
    ).hexdigest()
    assert result.content == "翻译"
    assert payload["salt"] == "fixed-salt"
    assert payload["sign"] == expected
    assert "Authorization" not in requests[0]["headers"]


def test_translate_requires_some_baidu_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, [])
    provider = _provider(monkeypatch, baidu_translate_api_key="", baidu_translate_secret_key=" ")

    with pytest.raises(TranslationAuthError, match="API Key 或密钥"):
        provider.translate("hello")


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, TranslationAuthError),
        (403, TranslationAuthError),
        (429, TranslationRateLimitError),
        (500, TranslationError),
    ],
)
def test_translate_maps_http_status_to_retryable_error_class(
    monkeypatch: pytest.MonkeyPatch, status: int, error_type: type[Exception]
) -> None:
    _install_client(monkeypatch, [FakeResponse(status_code=status)])
    provider = _provider(monkeypatch)

    with pytest.raises(error_type):
        provider.translate("hello")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"error_code": "54003", "error_msg": "too fast"}, "too fast"),
        ({"trans_result": []}, "返回为空"),
    ],
)
def test_translate_rejects_vendor_errors_and_empty_result(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], message: str
) -> None:
    _install_client(monkeypatch, [FakeResponse(body=body)])
    provider = _provider(monkeypatch)

    with pytest.raises(TranslationError, match=message):
        provider.translate("hello")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx.TimeoutException("timeout"), "请求超时"),
        (httpx.ConnectError("offline"), "无法连接百度翻译服务"),
    ],
)
def test_translate_wraps_transport_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    _install_client(monkeypatch, [error])
    provider = _provider(monkeypatch)

    with pytest.raises(TranslationError, match=message):
        provider.translate("hello")
