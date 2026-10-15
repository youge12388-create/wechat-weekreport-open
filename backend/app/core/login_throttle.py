"""登录尝试限速。

为什么需要：系统只有一个共享管理员密码，接口本身不限速 —— 在线暴力破解是可行的，
而且失败尝试此前没有任何审计。这里做**进程内**计数，理由是单管理员、单实例的内部系统：

- 计数按「客户端 IP」与「全局」两条线各自累计，避免两种情况：
  ① 攻击者换 IP 绕过（全局线兜底）；
  ② 单台设备输错几次就把所有人锁在门外（按 IP 线单独计）。
- 只对**失败**计数：成功即清零，正常使用完全不受影响。
- 冷却窗口过后自动放行，不做永久封禁（内部系统不能因为没人记得密码就永久锁死）。

局限（如实记录）：进程内状态在重启后归零，多进程/多实例部署时每个进程各自计数。
对当前的单进程部署足够；若将来横向扩容，需要换成共享存储（Redis 或数据库表）。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    """一次滑动窗口内的失败时间戳。"""

    failures: list[float] = field(default_factory=list)

    def _prune(self, now: float, window: float) -> None:
        cutoff = now - window
        if self.failures and self.failures[0] < cutoff:
            self.failures = [ts for ts in self.failures if ts >= cutoff]

    def add(self, now: float, window: float) -> None:
        self._prune(now, window)
        self.failures.append(now)

    def count(self, now: float, window: float) -> int:
        self._prune(now, window)
        return len(self.failures)

    def retry_after(self, now: float, window: float) -> int:
        """最早一次失败滑出窗口所需秒数（这一刻起计数回到限值以下）。"""
        self._prune(now, window)
        if not self.failures:
            return 0
        return max(1, math.ceil(self.failures[0] + window - now))


class LoginRateLimiter:
    """滑动窗口的失败计数限速器。"""

    def __init__(
        self,
        *,
        max_failures: int,
        window_seconds: float,
        max_global_failures: int,
    ) -> None:
        self.max_failures = max(1, max_failures)
        self.window_seconds = max(1.0, window_seconds)
        self.max_global_failures = max(1, max_global_failures)
        self._by_client: dict[str, _Bucket] = {}
        self._global = _Bucket()
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> int | None:
        """被限速时返回还需等待的秒数，否则 None。"""
        now = time.time()
        with self._lock:
            bucket = self._by_client.get(client)
            if bucket and bucket.count(now, self.window_seconds) >= self.max_failures:
                return bucket.retry_after(now, self.window_seconds)
            if self._global.count(now, self.window_seconds) >= self.max_global_failures:
                return self._global.retry_after(now, self.window_seconds)
            return None

    def record_failure(self, client: str) -> None:
        now = time.time()
        with self._lock:
            self._by_client.setdefault(client, _Bucket()).add(now, self.window_seconds)
            self._global.add(now, self.window_seconds)
            # 顺手清理已无有效失败的客户端，避免字典随访问者增多无限增长。
            if len(self._by_client) > 512:
                for key in [
                    key
                    for key, item in self._by_client.items()
                    if not item.count(now, self.window_seconds)
                ]:
                    self._by_client.pop(key, None)

    def record_success(self, client: str) -> None:
        """成功登录清零该客户端失败数（全局计数保留，仍能挡住换 IP 的撞库）。"""
        with self._lock:
            self._by_client.pop(client, None)
