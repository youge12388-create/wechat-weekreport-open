"""API 冒烟：路由可达 + 外部系统异常要转成可读响应。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from app.main import create_app


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.delenv("WECOM_CORP_SECRET", raising=False)
    monkeypatch.setenv("DB_URL", "sqlite://")
    get_settings.cache_clear()
    dispose_engine()
    Base.metadata.create_all(get_engine(get_settings()))
    try:
        yield TestClient(create_app())
    finally:
        dispose_engine()
        get_settings.cache_clear()


def test_health_endpoint(api_client: TestClient) -> None:
    response = api_client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_sync_jobs_endpoint(api_client: TestClient) -> None:
    response = api_client.get("/api/sync/jobs?limit=5")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_sync_status_endpoint(api_client: TestClient) -> None:
    response = api_client.get("/api/sync/status")
    assert response.status_code == 200
    body = response.json()
    assert "auto_enabled" in body
    assert "reports" in body
    assert "pending_count" in body


def test_missing_credentials_returns_readable_error(api_client: TestClient) -> None:
    """凭据缺失不能返回 500 堆栈。"""
    response = api_client.post("/api/sync/wecom/departments")
    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "ConnectorAuthError"
    assert "WECOM_CORP_ID" in body["message"]


def test_invalid_limit_is_rejected(api_client: TestClient) -> None:
    assert api_client.get("/api/sync/jobs?limit=0").status_code == 422


def test_tests_never_read_real_env() -> None:
    """回归防护：测试不得读取项目根目录含真实凭据的 .env。

    这条一旦挂掉，说明 get_settings() 会带回真实 secret，
    测试就具备打线上接口的能力（曾真实发生过）。
    """
    settings = get_settings()
    assert settings.wecom_corp_id == "test_corp"
    assert settings.model_config.get("env_file") == ()
