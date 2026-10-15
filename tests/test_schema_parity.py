"""迁移建库的 Schema 必须与模型定义一致。

为什么需要这条守护：测试用 ``Base.metadata.create_all`` 建库，而生产用
Alembic 迁移。两者一旦分叉，**所有测试都在一个与生产不同的库结构上通过**，
缺陷就被系统性掩盖了 —— 索引曾经只写在迁移里（模型里没有），于是测试从
未验证过索引；反过来，如果模型新增了列却忘了写迁移，测试也照样绿。

这里用「模型建库 vs 迁移建库」逐个表比对列与索引名。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.core.config import get_settings
from app.db.base import Base

import app.models  # noqa: F401  确保全部模型已注册到 metadata

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"


@pytest.fixture
def migrated_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """在临时库里跑完整迁移，返回其 URL。"""
    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{db_path.as_posix()}"
    # migrations/env.py 的 URL 取自应用配置（get_settings().db_url），不是 alembic.ini，
    # 因此必须用环境变量指到临时库，否则迁移会作用在开发库上。
    monkeypatch.setenv("DB_URL", url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        get_settings.cache_clear()
    return url


def test_migration_schema_matches_models(migrated_db_url: str) -> None:
    migrated = inspect(create_engine(migrated_db_url))
    models_engine = create_engine("sqlite://")
    Base.metadata.create_all(models_engine)
    models = inspect(models_engine)

    migrated_tables = set(migrated.get_table_names()) - {"alembic_version"}
    model_tables = set(models.get_table_names())
    assert migrated_tables == model_tables, (
        "迁移与模型的表不一致："
        f"仅迁移有={sorted(migrated_tables - model_tables)}，"
        f"仅模型有={sorted(model_tables - migrated_tables)}"
    )

    for table in sorted(model_tables):
        migrated_cols = {c["name"] for c in migrated.get_columns(table)}
        model_cols = {c["name"] for c in models.get_columns(table)}
        assert migrated_cols == model_cols, (
            f"{table} 列不一致：仅迁移有={sorted(migrated_cols - model_cols)}，"
            f"仅模型有={sorted(model_cols - migrated_cols)}"
            "（模型加了字段但没写迁移时会出现）"
        )

        migrated_idx = {i["name"] for i in migrated.get_indexes(table)}
        model_idx = {i["name"] for i in models.get_indexes(table)}
        assert migrated_idx == model_idx, (
            f"{table} 索引不一致：仅迁移有={sorted(migrated_idx - model_idx)}，"
            f"仅模型有={sorted(model_idx - migrated_idx)}"
            "（索引只写在一侧时，跑迁移的环境和跑测试的环境性能/行为会不同）"
        )
