"""AI 分析后台 Worker 入口。

用法：
    python -m app.worker            # 常驻轮询
    python -m app.worker --once     # 处理一轮后退出（测试 / 手动）
"""

from __future__ import annotations

import argparse
import time

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.services.analysis_queue_service import run_worker_once
from app.services.reminder_scheduler import maybe_send_scheduled_reminders
from app.services.structured_data_service import sweep_blocker_lifecycle
from app.services.sync_scheduler import (
    maybe_enqueue_scheduled_sync,
    recover_interrupted_jobs,
    run_pending_sync_jobs,
)

logger = get_logger(__name__)

# 运行期租约校验的时间间隔（不做每轮全表扫；进程启动时已有一次无条件回收）。
_LEASE_CHECK_INTERVAL_SECONDS = 300
_last_lease_check = 0.0
# 历史投影上一次执行的时间戳（0 表示本进程还没跑过）。
_last_backfill_at = 0.0


def _reap_stale_jobs() -> None:
    """运行期回收卡死的任务：只回收超过租约时长的，避免误伤正在跑的任务。"""
    global _last_lease_check
    now = time.monotonic()
    if now - _last_lease_check < _LEASE_CHECK_INTERVAL_SECONDS:
        return
    _last_lease_check = now
    with session_scope() as session:
        recover_interrupted_jobs(
            session,
            max_age_minutes=get_settings().worker_job_lease_minutes,
        )


def _should_run_backfill() -> bool:
    """历史投影是否该跑：间隔可配，0 表示只在进程启动时跑一次。

    它不是常规工作，而是数据修复（把旧的 AI 结论投影到新表）。旧实现挂在每轮
    3 秒轮询里，等于常驻一条重查询 + 最多 100 篇物化，且拿不到 ReportScore 的
    周报会被无限重复物化。
    """
    global _last_backfill_at
    interval = get_settings().ai_backfill_interval_seconds
    now = time.monotonic()
    if interval <= 0:
        due = _last_backfill_at == 0.0
    else:
        due = now - _last_backfill_at >= interval
    if due:
        _last_backfill_at = now
    return due



def main() -> None:
    parser = argparse.ArgumentParser(description="周报 AI 分析 Worker")
    parser.add_argument(
        "--once", action="store_true", help="只处理一轮后退出"
    )
    parser.add_argument(
        "--sync-once",
        action="store_true",
        help="只跑一轮同步调度（用于本地验证 / 服务器手动触发）",
    )
    parser.add_argument("--concurrency", type=int, default=None)
    args = parser.parse_args()

    if args.once:
        # 手动跑一轮时把历史投影一起做掉：这是「一次跑完就能收工」的场景。
        result = run_worker_once(concurrency=args.concurrency, run_backfill=True)
        logger.info("worker once 完成：%s", result)
        return

    if args.sync_once:
        with session_scope() as session:
            recover_interrupted_jobs(session)
            queued = maybe_enqueue_scheduled_sync(session)
            results = run_pending_sync_jobs(session, limit=3)
            swept = sweep_blocker_lifecycle(session)
        logger.info(
            "同步调度 once 完成：入队=%s，执行=%s，卡点刷新=%s",
            queued,
            results,
            swept,
        )
        return

    settings = get_settings()
    logger.info(
        "AI worker 启动：并发=%s，轮询间隔=%ss",
        args.concurrency or settings.ai_worker_concurrency,
        settings.ai_worker_poll_seconds,
    )
    # 启动即无条件回收：上一进程若在任务执行中被杀，它的 RUNNING 行会永久挡住
    # 同类型同步（幂等入队把 RUNNING 当作活跃任务）。此刻旧进程必然已不在，
    # 因此启动时的回收不会误伤任何在跑的任务。
    with session_scope() as session:
        recovered = recover_interrupted_jobs(session)
    if any(recovered.values()):
        logger.warning("启动时回收了中断任务：%s", recovered)

    while True:
        try:
            _reap_stale_jobs()
            # 同步队列优先：手动点击或定时任务一旦入队，尽快处理。
            with session_scope() as session:
                maybe_enqueue_scheduled_sync(session)
                run_pending_sync_jobs(session, limit=1)
                maybe_send_scheduled_reminders(session)
                # 静默降档/复活由证据推导、幂等，挂在每轮循环里兜底执行。
                sweep_blocker_lifecycle(session)
            run_worker_once(
                concurrency=args.concurrency, run_backfill=_should_run_backfill()
            )
        except Exception as exc:  # noqa: BLE001 Worker 循环不能因单轮异常退出
            logger.error("worker 单轮异常：%s", exc)
        time.sleep(settings.ai_worker_poll_seconds)


if __name__ == "__main__":
    main()
