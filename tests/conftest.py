from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.time_utils import utcnow
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_session_factory


@pytest.fixture(autouse=True)
def wecom_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """隔离真实配置：测试绝不读取项目根目录的 .env，也绝不访问外部网络。

    背景：pydantic-settings 在环境变量缺失时会回退读 env_file。若根目录存在
    含真实凭据的 .env，测试就会拿真实 secret 去打线上接口（曾真实发生过）。
    这里清空 env_file + 注入假凭据，做双重隔离。
    """
    from app.core.config import Settings

    monkeypatch.setenv("WECOM_CORP_ID", "test_corp")
    monkeypatch.setenv("WECOM_CORP_SECRET", "test_secret")
    # 测试环境关闭鉴权中间件：既有接口测试不携带会话 Cookie，
    # 否则会被中间件统一拦截成 401。鉴权逻辑由 test_auth.py 单独开启验证。
    monkeypatch.setenv("APP_ENV", "test")
    # 配置加密密钥文件指向测试临时目录：否则第一次读写运行时配置就会在仓库里
    # 生成 data/.config_secret.key（不该出现在工作区里）。
    monkeypatch.setenv(
        "CONFIG_KEY_FILE", str(Path(__file__).resolve().parent / ".tmp_config_secret.key")
    )
    monkeypatch.setattr(
        Settings,
        "model_config",
        {**Settings.model_config, "env_file": ()},
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """每个用例一个独立内存库。

    用内存库而非临时文件：当前环境禁止删除操作，pytest 清理 basetemp 会失败。
    """
    monkeypatch.setenv("DB_URL", "sqlite://")
    get_settings.cache_clear()
    dispose_engine()

    engine = get_engine(get_settings())
    Base.metadata.create_all(engine)
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
        dispose_engine()
        get_settings.cache_clear()


@pytest.fixture
def fixed_now() -> datetime:
    """固定「当前时间」，避免测试依赖真实时钟。"""
    return datetime(2026, 9, 8, 10, 0, 0)


@pytest.fixture
def last_week_range(fixed_now: datetime) -> tuple[datetime, datetime]:
    return fixed_now - timedelta(days=7), fixed_now
