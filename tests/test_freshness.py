"""数据新鲜度与周期规则透出：last_sync_at + /api/config。

依赖 conftest 的 autouse fixture（APP_ENV=test、env_file 隔离）保证不碰真实配置。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.main import create_app
from app.models.enums import SyncJobStatus
from app.models.sync_job import SyncJob


@pytest.fixture
def freshness_client(monkeypatch: pytest.MonkeyPatch):
    # autouse wecom_env 已设 APP_ENV=test / env_file=()，这里只切内存库并建表
    monkeypatch.setenv("DB_URL", "sqlite://")
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": ()}
    )
    get_settings.cache_clear()
    dispose_engine()
    engine = get_engine(get_settings())
    Base.metadata.create_all(engine)
    session = get_session_factory()()
    try:
        yield TestClient(create_app()), session
    finally:
        session.close()
        dispose_engine()
        get_settings.cache_clear()


def test_config_exposes_period_rule(freshness_client) -> None:
    client, _ = freshness_client
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["period_rule"] == "week"
    assert "自然周" in body["period_rule_label"]


def test_overview_returns_last_sync_when_present(freshness_client) -> None:
    client, session = freshness_client
    session.add(
        SyncJob(
            source="wecom",
            type="weekly_reports",
            status=SyncJobStatus.SUCCESS,
            start_time=datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None),
            end_time=datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc).replace(tzinfo=None),
        )
    )
    session.commit()

    resp = client.get("/api/dashboard/overview")
    assert resp.status_code == 200
    assert resp.json()["last_sync_at"] is not None


def test_overview_last_sync_none_without_sync(freshness_client) -> None:
    client, _ = freshness_client
    resp = client.get("/api/dashboard/overview")
    assert resp.status_code == 200
    assert resp.json()["last_sync_at"] is None
