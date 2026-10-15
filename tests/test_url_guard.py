from __future__ import annotations

import pytest

from app.core.url_guard import host_allowed, normalize_base_url, validate_ai_base_url


def test_normalize_base_url_trims_whitespace_and_trailing_slashes() -> None:
    assert normalize_base_url("  https://api.example.com/v1///  ") == "https://api.example.com/v1"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("  ", "不能为空"),
        ("ftp://api.example.com", "必须以 http:// 或 https:// 开头"),
        ("https:///v1", "缺少主机名"),
    ],
)
def test_normalize_base_url_rejects_invalid_syntax(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_base_url(value)


def test_host_allowlist_supports_exact_and_subdomain_rules_without_suffix_bypass() -> None:
    allowed = [" API.DEEPSEEK.COM ", ".example.org", ""]

    assert host_allowed("api.deepseek.com", allowed)
    assert host_allowed("example.org", allowed)
    assert host_allowed("model.example.org", allowed)
    assert not host_allowed("evil-api.deepseek.com.attacker.tld", allowed)
    assert not host_allowed("", allowed)
    assert not host_allowed("other.test", [])


def test_validate_ai_base_url_allows_unset_and_normalizes_allowed_host() -> None:
    assert validate_ai_base_url(None, []) is None
    assert validate_ai_base_url(" https://model.example.org/v1/ ", [".example.org"]) == (
        "https://model.example.org/v1"
    )


def test_validate_ai_base_url_reports_disallowed_host_and_empty_allowlist() -> None:
    with pytest.raises(ValueError, match="不允许的 Base URL 主机：attacker.test"):
        validate_ai_base_url("https://attacker.test/v1", ["api.deepseek.com"])

    with pytest.raises(ValueError, match="（未配置）"):
        validate_ai_base_url("https://api.deepseek.com/v1", [])
