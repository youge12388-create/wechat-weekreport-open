from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 `app` 包可导入
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import (  # noqa: E402,F401  确保全部模型注册到 metadata
    AIAnalysis,
    AgentFeedback,
    AgentMemory,
    AnalysisQueue,
    Blocker,
    Department,
    Employee,
    ManagementAction,
    ManagementActionEvent,
    RawRecord,
    ReminderLog,
    ReminderRun,
    ReportItem,
    ReportScore,
    ReportTranslation,
    PeriodSummaryQueue,
    SystemSetting,
    SyncJob,
    Task,
    TaskReportLink,
    WeeklyReport,
    WorkItem,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_db_url = get_settings().db_url

# SQLite 下父目录可能不存在，engine_from_config 不会自动创建
if _db_url.startswith("sqlite:///"):
    _path = Path(_db_url[len("sqlite:///") :])
    if not _path.name.startswith(":memory:"):
        _path.parent.mkdir(parents=True, exist_ok=True)

config.set_main_option("sqlalchemy.url", _db_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite 下 ALTER 需要 batch 模式
        user_module_prefix="sa.",  # 让 JSONB 的 astext_type=Text() 渲染成 sa.Text()
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            user_module_prefix="sa.",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
