"""请求内的只读结果记忆化：作用域是一个 Session，**写入即刻失效**。

背景（2026-09-23 加载性能排查）：经营看板与首页的组装会把同一批事实算好几遍 ——
一次 ``GET /api/operating-dashboard?view=overview`` 里 ``insights()`` 被三个消费者
（关键发现、需要你处理、总览信号）各调一次，而每次都要把全部任务关联实例化成
ORM 对象（开发库 4304 条实测 585 ms，SQL 本身只占 60 ms）。**贵的是组装而不是查询**，
重复组装就是「点开要等三四秒」的直接原因。

作用域与失效：
* 生产环境一个请求一个 Session（``api/deps.get_db``），缓存天然按请求隔离；
* **任何写入（flush / commit / rollback）都让整个缓存立即失效** —— 测试与脚本会复用
  同一个 Session，写完再读必须看到新结果，不能拿到写入前的快照。监听写入事件比
  「每个调用方记得在写完后手动清」可靠：漏掉一处就会得到静默的旧数字；
* 只读路径不失效，所以同一次请求里的多个消费者共享同一份计算。

**缓存的是事实，不是结论**：这里只放「从库里读出来的东西」，不放业务判定结果。
调用方拿到的是同一个对象，必须只读使用（现有调用方都只做计数与投影）。
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from sqlalchemy import event
from sqlalchemy.orm import Session

_CACHE_KEY = "request_memo.cache"
_LISTENER_KEY = "request_memo.write_listener"
# 任何一条写入路径都会让缓存失真，所以四个事件都要监听：
# flush 覆盖「新增/修改过 SQL 但还没提交」，commit/rollback 覆盖事务收尾。
# after_soft_rollback 是 SAVEPOINT 回滚（入队幂等等路径会用到）。
_WRITE_EVENTS = ("after_flush", "after_commit", "after_rollback", "after_soft_rollback")

T = TypeVar("T")


def _drop_cache(session: Session, *_: Any, **__: Any) -> None:
    session.info.pop(_CACHE_KEY, None)


def _ensure_write_invalidation(session: Session) -> None:
    """给这个 Session 挂上「写入即清缓存」的监听（每个 Session 只挂一次）。"""
    if session.info.get(_LISTENER_KEY):
        return
    session.info[_LISTENER_KEY] = True
    for event_name in _WRITE_EVENTS:
        event.listen(session, event_name, _drop_cache)


def memoize(session: Session, key: Any, factory: Callable[[], T]) -> T:
    """按 ``key`` 缓存 ``factory()`` 的结果；同一 Session 内只算一次。"""
    _ensure_write_invalidation(session)
    cache: dict[Any, Any] = session.info.setdefault(_CACHE_KEY, {})
    if key not in cache:
        cache[key] = factory()
    return cache[key]


def invalidate(session: Session) -> None:
    """主动清空缓存（用于「同一 Session 被当成两次独立调用」的显式边界）。"""
    session.info.pop(_CACHE_KEY, None)
