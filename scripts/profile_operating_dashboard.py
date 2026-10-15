"""经营看板视图的单次耗时剖析（只读，不改任何数据）。

背景（2026-09-23 加载性能排查）：看板冷启动一度要 3~4 秒，而 SQL 只占约 90 ms ——
时间几乎全花在「把同一批数据反复组装成 ORM 对象」上。这个脚本就是当时用来定位
热点的工具，保留下来供后续改动前先摸底。

用法（在项目根目录，先设好 PYTHONPATH=<项目根>/backend）：
    python scripts/profile_operating_dashboard.py [overview|weekly|monthly] [--sql]

输出：
  * 单次 ``build_view`` 的总耗时与 payload 字段
  * cProfile 按 tottime 排序的前 25 个函数
  * 加 ``--sql`` 时另附每条 SQL 的耗时与总条数
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time

from sqlalchemy import event

from app.db.session import get_session_factory
from app.services import operating_dashboard_service as dashboard
from app.services.view_service import resolve_period


def main() -> None:
    argv = sys.argv[1:]
    view = next((a for a in argv if not a.startswith("-")), "overview")
    show_sql = "--sql" in argv
    start, end = resolve_period(None, None)
    print(f"view={view} period={start}~{end}")

    statements: list[tuple[str, float]] = []

    with get_session_factory()() as session:
        if show_sql:
            def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
                conn.info.setdefault("_sql_start", []).append(time.perf_counter())

            def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
                pending = conn.info.setdefault("_sql_start", [])
                if pending:
                    statements.append((statement, (time.perf_counter() - pending.pop(0)) * 1000))

            event.listen(session.get_bind(), "before_cursor_execute", _before)
            event.listen(session.get_bind(), "after_cursor_execute", _after)

        profiler = cProfile.Profile()
        t0 = time.perf_counter()
        profiler.enable()
        payload = dashboard.build_view(session, view, start, end)
        profiler.disable()
        print(f"total {(time.perf_counter() - t0) * 1000:.0f} ms, keys={sorted(payload)}")

    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("tottime").print_stats(25)
    print(stream.getvalue())

    if show_sql:
        total_ms = sum(ms for _, ms in statements)
        print(f"SQL count={len(statements)} total={total_ms:.0f} ms")
        for statement, ms in sorted(statements, key=lambda row: -row[1])[:25]:
            print(f"  {ms:7.1f} ms  {' '.join(statement.split())[:150]}")


if __name__ == "__main__":
    main()
