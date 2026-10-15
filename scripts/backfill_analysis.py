"""批量补跑历史周报的结构化分析（真实计费，运行前请确认）。

用途一：库里大量周报只有「资源需求」的确定性投影、没有模型结构化分析
（`ai_checked_at IS NULL`）。没分析就没有事项数据，看板的跨周判断、长期趋势、
精力投向都测不出来 —— 实测只有 1 个周期有分析时，「连续 N 周未解决」这类信号
几乎只能捡到写法固定的资源需求占位。

用途二：**换提示词后刷新已有结论**（`--rerun-version`）。改提示词会让新旧两版
产出并存，实测同一所学校在旧版有 27 种写法、新版 8 种，混在一起时跨周匹配会把
同一件事算成两件。

**成本边界**（每一条都是被真实账单教出来的）：

* 默认只补「从未分析过」的周报，**不会**重跑已有的。
* ``--force`` 重跑**全部**（含已分析）—— 先看 ``--dry-run`` 的数字再决定。
* ``--rerun-version v1`` 只重跑「最新一次分析是 v1」的那批，这是刷新提示词时
  真正该用的档位（``--force`` 会把 449 篇全跑一遍，差 5 倍的钱）。
* 运行前检查队列：若周期/经营/卡点总结队列有待办，**默认拒绝启动** ——
  worker 每轮会顺带领取它们，那是本次授权之外的计费。

用法：
    python scripts/backfill_analysis.py --dry-run                    # 只看待补多少篇
    python scripts/backfill_analysis.py --batch 40                   # 补未分析的
    python scripts/backfill_analysis.py --rerun-version v1 --dry-run # 看要重跑多少篇
    python scripts/backfill_analysis.py --rerun-version v1           # 重跑这批（真实计费）
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import func, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.models.ai_analysis import AIAnalysis  # noqa: E402
from app.models.analysis_queue import AnalysisQueue  # noqa: E402
from app.models.blocker_summary_queue import BlockerSummaryQueue  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.operating_summary_queue import OperatingSummaryQueue  # noqa: E402
from app.models.period_summary_queue import PeriodSummaryQueue  # noqa: E402
from app.models.weekly_report import WeeklyReport  # noqa: E402
from app.services.analysis_queue_service import (  # noqa: E402
    enqueue_report,
    run_worker_once,
)
from app.services.view_service import (  # noqa: E402
    ai_report_condition,
    report_period_overlap_conditions,
)

ANALYSIS_TYPE = "report_analysis"

# 补跑覆盖的周期边界。刻意用「库里的真实最早/最晚周期」而不是写死日期：
# 新同步进来的周期应自动被纳入下一次补跑。
SPAN_START = date(2000, 1, 1)
SPAN_END = date(2100, 1, 1)


def _report_query(session, *, rerun_version: str | None, force: bool):
    """选出本次要处理的周报。

    **计数与取件共用这一个函数**：两处各写一套筛选条件，迟早出现「说好 85 篇、
    实际跑 449 篇」这种差 5 倍钱的偏差（本脚本上一版就是这样的）。
    """
    conditions = [
        *report_period_overlap_conditions(SPAN_START, SPAN_END),
        ai_report_condition(session),
    ]
    stmt = (
        select(WeeklyReport)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
    )
    if rerun_version:
        # 「最新一次分析是某个版本」＝ 该篇 max(ai_analyses.id) 那一行是它。
        # 不能简单查「存在某版本的行」：混版期间一篇会同时有 v1 与 v2 两行。
        latest = (
            select(AIAnalysis.entity_id, func.max(AIAnalysis.id).label("aid"))
            .where(AIAnalysis.analysis_type == ANALYSIS_TYPE)
            .group_by(AIAnalysis.entity_id)
            .subquery()
        )
        stmt = (
            stmt.join(latest, latest.c.entity_id == WeeklyReport.id)
            .join(AIAnalysis, AIAnalysis.id == latest.c.aid)
            .where(AIAnalysis.prompt_version == rerun_version)
        )
    elif not force:
        stmt = stmt.where(WeeklyReport.ai_checked_at.is_(None))
    return stmt.order_by(WeeklyReport.end_date.asc(), WeeklyReport.id.asc())


def _count(session, *, rerun_version: str | None, force: bool) -> int:
    subquery = _report_query(session, rerun_version=rerun_version, force=force).subquery()
    return int(session.scalar(select(func.count()).select_from(subquery)) or 0)


def _other_queue_pending(session) -> dict[str, int]:
    """其他总结队列的待办数。它们每轮会被 worker 顺带领取，是额外计费。"""
    counts: dict[str, int] = {}
    for name, model in (
        ("周期总结", PeriodSummaryQueue),
        ("经营总结", OperatingSummaryQueue),
        ("卡点总结", BlockerSummaryQueue),
    ):
        pending = int(
            session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.status.in_(["pending", "running"]))
            )
            or 0
        )
        if pending:
            counts[name] = pending
    return counts


def _token_totals(session) -> tuple[int, int, int]:
    row = session.execute(
        select(
            func.count(AIAnalysis.id),
            func.coalesce(func.sum(AIAnalysis.prompt_tokens), 0),
            func.coalesce(func.sum(AIAnalysis.completion_tokens), 0),
        ).where(AIAnalysis.analysis_type == ANALYSIS_TYPE)
    ).one()
    return int(row[0]), int(row[1]), int(row[2])


def _describe(args: argparse.Namespace) -> str:
    if args.rerun_version:
        return f"重跑「最新分析为 {args.rerun_version}」的周报"
    if args.force:
        return "重跑全部周报（--force：含已分析的，**成本最高**）"
    return "补跑尚未分析的周报"


def main() -> int:
    parser = argparse.ArgumentParser(description="批量补跑历史周报结构化分析")
    parser.add_argument("--batch", type=int, default=40, help="每批入队篇数（默认 40）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="重跑全部（含已分析）—— 换提示词时通常应该用 --rerun-version 限定批次",
    )
    parser.add_argument(
        "--rerun-version",
        default=None,
        help="只重跑「最新一次分析是该提示词版本」的周报，例如 v1",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计篇数，不调用模型")
    parser.add_argument("--max-batches", type=int, default=0, help="最多跑几批（0=不限）")
    parser.add_argument(
        "--allow-other-queues",
        action="store_true",
        help="允许在其他总结队列有待办时启动（那些任务会被顺带消费并计费）",
    )
    args = parser.parse_args()

    if args.force and args.rerun_version:
        print("--force 与 --rerun-version 不能同时用：前者是全量、后者是限定批次。")
        return 2

    get_settings()  # 触发配置校验（缺密钥会在这里暴露）
    session = get_session_factory()()

    total = _count(session, rerun_version=args.rerun_version, force=args.force)
    calls, prompt_tokens, completion_tokens = _token_totals(session)
    print(f"任务：{_describe(args)}")
    print(f"本次将处理：{total} 篇")
    print(f"当前账本：{ANALYSIS_TYPE} 累计 {calls} 次调用，"
          f"prompt={prompt_tokens} completion={completion_tokens}")

    other = _other_queue_pending(session)
    if other:
        print(f"\n注意：其他总结队列有待办 {other}")
        print("      worker 每轮会顺带领取它们，那是本次授权之外的计费。")
        if not args.allow_other_queues:
            print("      如确认要一起跑，请加 --allow-other-queues；否则先清空这些队列。")
            session.close()
            return 2

    if args.dry_run:
        print("\n（--dry-run：未调用模型）")
        session.close()
        return 0
    if total == 0:
        print("\n没有需要处理的周报。")
        session.close()
        return 0

    print(f"\n开始，每批 {args.batch} 篇。Ctrl-C 可随时中断，已完成的批已落库。\n")

    analyzed_at_start = calls
    batches = 0
    started = time.monotonic()
    try:
        while True:
            if args.max_batches and batches >= args.max_batches:
                print(f"\n已达 --max-batches={args.max_batches}，停止。")
                break
            reports = session.scalars(
                _report_query(
                    session, rerun_version=args.rerun_version, force=args.force
                ).limit(args.batch)
            ).all()
            if not reports:
                break

            queued = 0
            # 重跑档必须 force：队列行已是 success，不 force 会「保持不变」直接跳过。
            # 补跑档用 force=False，保持幂等（内容未变且已成功的不会被重复消费）。
            force_enqueue = bool(args.rerun_version or args.force)
            for report in reports:
                if enqueue_report(session, report, force=force_enqueue) is not None:
                    queued += 1
            session.commit()
            if queued == 0:
                # 一篇都没入队（全部被免提交口径挡掉）：如实说明并退出，
                # 否则这里会变成死循环。
                print("本批无可入队周报（可能全部属于免提交人员），停止。")
                break

            # 消费到队列空为止。**只关心报告队列**：其他队列已在启动前检查过。
            while True:
                result = run_worker_once()
                if int(result.get("claimed") or 0) <= 0:
                    break

            batches += 1
            elapsed = time.monotonic() - started
            remaining = _count(
                session, rerun_version=args.rerun_version, force=args.force
            )
            # 进度用**数据库真值**算，不用 worker 返回的 processed 累加：
            # 那个字段是「本轮领到的条数」，与「真正写入成功的篇数」不是一回事
            # （实测一批 10 篇全部成功，按它累加只报出 5 篇）。
            calls_now, pt_now, ct_now = _token_totals(session)
            done = calls_now - analyzed_at_start
            print(
                f"批 {batches}: 本次完成 {done} 篇 | 剩余 {remaining} 篇 | "
                f"耗时 {elapsed/60:.1f} 分 | token prompt={pt_now} completion={ct_now}"
            )
            if remaining == 0:
                break
    except KeyboardInterrupt:
        print("\n已中断（Ctrl-C）。已完成的批次都已落库，重跑本脚本会自动跳过它们。")
    finally:
        session.close()

    total_elapsed = time.monotonic() - started
    session = get_session_factory()()
    try:
        _, pt, ct = _token_totals(session)
        print(f"\n结束：耗时 {total_elapsed/60:.1f} 分钟。")
        print(f"剩余待处理：{_count(session, rerun_version=args.rerun_version, force=args.force)} 篇")
        print(f"账本：{ANALYSIS_TYPE} 累计 {_token_totals(session)[0]} 次调用，"
              f"prompt={pt} completion={ct}")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
