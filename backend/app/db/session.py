"""数据库引擎与会话。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _ensure_sqlite_parent_dir(db_url: str) -> None:
    """sqlite:///./data/app.db 这类相对路径时，先创建父目录。"""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return
    path_part = db_url[len(prefix) :]
    if path_part.startswith(":memory:"):
        return
    Path(path_part).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _apply_sqlite_pragmas(dbapi_connection, _record) -> None:
    """每个 SQLite 连接建立时设置 PRAGMA。

    默认的 ``journal_mode=delete`` 会让**写事务阻塞读**：同步 Worker 一边逐条
    提交，网页读接口就一边排队等待（实测读请求 0.47s → 0.83s，第二个写事务要等
    2.7s）。WAL 让读不阻塞写、写不阻塞读，是本项目「后台同步 + 前台查询」并存
    这一形态下最直接的改善。

    - ``journal_mode=WAL``：库级持久设置，写一次后对后续连接保持。
    - ``busy_timeout``：写-写冲突时等待而不是立刻抛 "database is locked"。
    - ``synchronous=NORMAL``：WAL 下断电最多丢最近若干事务，不会损坏库；
      这也是 SQLite 官方对 WAL 的推荐搭配。
    - ``foreign_keys=ON``：SQLite 默认不强制外键，与 PG 行为对齐。

    内存库（StaticPool / 测试）同样设一遍：它们不支持 WAL 也无害。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:  # noqa: BLE001  PRAGMA 失败不应让连接不可用
        pass
    finally:
        cursor.close()


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _ensure_sqlite_parent_dir(settings.db_url)
        kwargs: dict[str, object] = {"future": True, "echo": settings.db_echo}
        if settings.db_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if settings.db_url in ("sqlite://", "sqlite:///:memory:"):
                # 内存库必须复用同一连接，否则每个连接都是一个空库
                kwargs["poolclass"] = StaticPool
        _engine = create_engine(settings.db_url, **kwargs)
        if settings.db_url.startswith("sqlite"):
            event.listen(_engine, "connect", _apply_sqlite_pragmas)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings), autoflush=False, expire_on_commit=False
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文：正常提交，异常回滚并关闭。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """测试用：释放连接与缓存的引擎。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
