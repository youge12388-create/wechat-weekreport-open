"""周报 AI 分析的后台队列。

同步只负责入队；分析由独立 Worker 进程消费。队列位按 report 唯一，
正文指纹变化会把旧任务重置，保证「只分析新增/修改，不重扫历史」。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.db.session import get_session_factory, session_scope
from app.models.analysis_queue import AnalysisQueue
from app.models.blocker_summary_queue import BlockerSummaryQueue
from app.models.enums import AnalysisQueueStatus
from app.models.operating_summary_queue import OperatingSummaryQueue
from app.models.period_summary_queue import PeriodSummaryQueue
from app.models.weekly_report import WeeklyReport
from app.services.report_analysis_service import (
    clear_report_analysis_cache,
    current_analysis_report_ids,
    report_payload_hash,
)
from app.services.runtime_config_service import runtime_settings
from app.services.view_service import (
    ai_report_condition,
    report_ai_exempt,
    report_period_overlap_conditions,
)

logger = get_logger(__name__)

# When the worker has fewer threads than queue kinds, rotate which long-running
# summary queue gets first access so one continuously busy queue cannot starve
# the others. This is process-local scheduling only; the database CAS claims
# remain the cross-worker source of truth.
_summary_queue_rotation = 0


def enqueue_report(
    session: Session, report: WeeklyReport, force: bool = False
) -> AnalysisQueue | None:
    """为单篇周报建立/重置待分析任务，幂等：内容未变且已成功则不动。"""
    current_hash = report_payload_hash(report)
    if report_ai_exempt(session, report):
        # 免提交期间仍会同步原文；若正文已经不同于上次分析，先清掉过期
        # 的冗余标记，但不建立/消费 AI 任务。取消免提交后，普通扫描即可
        # 发现 ``ai_checked_at`` 为空并重新入队，历史 AIAnalysis 仍保留。
        if report.ai_checked_at is not None and report.analysis_payload_hash != current_hash:
            clear_report_analysis_cache(session, report)
        return None
    row = session.scalar(
        select(AnalysisQueue).where(AnalysisQueue.report_id == report.id)
    )
    if (
        row is not None
        and row.status == AnalysisQueueStatus.SUCCESS
        and not force
        and report.id not in current_analysis_report_ids(session, [report])
    ):
        force = True

    if force or row is None or row.payload_hash != current_hash:
        clear_report_analysis_cache(session, report)

    if row is None:
        row = AnalysisQueue(
            report_id=report.id,
            payload_hash=current_hash,
            status=AnalysisQueueStatus.PENDING,
        )
        session.add(row)
    elif (
        force
        or row.payload_hash != current_hash
        or row.status in (AnalysisQueueStatus.PENDING, AnalysisQueueStatus.FAILED)
    ):
        row.payload_hash = current_hash
        row.status = AnalysisQueueStatus.PENDING
        row.attempts = 0
        row.next_attempt_at = None
        row.last_error = None
    # 同一内容已成功分析：保持不变，避免重复消费
    session.flush()
    return row


def enqueue_period_reports(
    session: Session,
    start: date,
    end: date,
    department_id: int | None = None,
    force: bool = False,
    limit: int | None = None,
    employee_id: int | None = None,
) -> list[AnalysisQueue]:
    rows, _examined, _matched = _enqueue_period_reports_impl(
        session,
        start,
        end,
        department_id=department_id,
        employee_id=employee_id,
        force=force,
        limit=limit,
    )
    return rows


def _enqueue_period_reports_impl(
    session: Session,
    start: date,
    end: date,
    department_id: int | None,
    employee_id: int | None,
    force: bool,
    limit: int,
) -> tuple[list[AnalysisQueue], int, int]:
    """返回（本轮入队行，已检视篇数，区间内非免提交总篇数）。

    「已检视」含被跳过的篇目；超限中断后，未检视篇数 = matched - examined。
    """
    from app.models.employee import Employee

    if limit is None:
        limit = runtime_settings(session).ai_scan_max_reports
    limit = max(1, int(limit))

    conditions = [*report_period_overlap_conditions(start, end), ai_report_condition(session)]
    if department_id is not None:
        conditions.append(Employee.department_id == department_id)
    if employee_id is not None:
        conditions.append(WeeklyReport.employee_id == employee_id)
    reports = session.scalars(
        select(WeeklyReport)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
        .order_by(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc())
    ).all()
    if not reports:
        return [], 0, 0

    queue_rows: dict[int, AnalysisQueue] = {
        row.report_id: row
        for row in session.scalars(
            select(AnalysisQueue).where(AnalysisQueue.report_id.in_([r.id for r in reports]))
        )
    }
    completed_ids = current_analysis_report_ids(session, reports) if not force else set()

    rows: list[AnalysisQueue] = []
    examined = 0
    processed = 0
    for report in reports:
        existing = queue_rows.get(report.id)
        if (
            not force
            and existing is not None
            and existing.payload_hash == report_payload_hash(report)
            and (
                existing.status in (AnalysisQueueStatus.PENDING, AnalysisQueueStatus.RUNNING)
                or (existing.status == AnalysisQueueStatus.SUCCESS and report.id in completed_ids)
            )
        ):
            # 已在队列或已按当前正文分析成功：不需要新的模型调用，不占预算。
            examined += 1
            continue
        if processed >= limit:
            break
        examined += 1
        processed += 1
        row = enqueue_report(session, report, force=force)
        if row is not None:
            rows.append(row)
    return rows, examined, len(reports)


def enqueue_period_reports_with_remaining(
    session: Session,
    start: date,
    end: date,
    department_id: int | None = None,
    force: bool = False,
    limit: int | None = None,
    employee_id: int | None = None,
) -> tuple[list[AnalysisQueue], int]:
    """同 ``enqueue_period_reports``，额外返回「因超限未检视的篇数」。

    调用方据此如实告知用户本轮只排了一部分，避免把半批当成全量。
    注意区间由 ``ai_report_condition`` 过滤，**不含免提交人员**；已在队列或
    已分析成功的篇目不占上限，因此重复点击 ``remaining`` 会持续收敛到 0。
    """
    if limit is None:
        limit = runtime_settings(session).ai_scan_max_reports
    limit = max(1, int(limit))

    rows, examined, matched = _enqueue_period_reports_impl(
        session,
        start,
        end,
        department_id=department_id,
        employee_id=employee_id,
        force=force,
        limit=limit,
    )
    return rows, max(0, matched - examined)


def enqueue_missing_period_reports(
    session: Session,
    start: date,
    end: date,
    department_id: int | None = None,
) -> list[AnalysisQueue]:
    """只为周期内尚无队列记录的周报建队列，供历史安全回填使用。

    与 ``enqueue_period_reports(force=False)`` 不同，本函数不会重置已有的
    pending / running / failed 任务，也不会清理已成功分析的缓存；重复执行只
    会返回空列表。这让「补齐历史漏建队列」不会打断正在运行的 Worker。
    """
    from app.models.employee import Employee

    conditions = [*report_period_overlap_conditions(start, end), ai_report_condition(session)]
    if department_id is not None:
        conditions.append(Employee.department_id == department_id)
    query = (
        select(WeeklyReport)
        .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
        .where(*conditions)
        .order_by(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc())
    )
    reports = session.scalars(query).all()
    existing_ids = set(session.scalars(select(AnalysisQueue.report_id)))
    return [
        row
        for report in reports
        if report.id not in existing_ids
        and (row := enqueue_report(session, report, force=False)) is not None
    ]


def queue_state(
    session: Session, report_id: int
) -> dict[str, Any] | None:
    row = session.scalar(
        select(AnalysisQueue).where(AnalysisQueue.report_id == report_id)
    )
    if row is None:
        return None
    return {
        "status": row.status,
        "attempts": row.attempts,
        "next_attempt_at": row.next_attempt_at.isoformat()
        if row.next_attempt_at
        else None,
        "last_error": row.last_error,
    }


def _claim_batch(session: Session, limit: int) -> list[int]:
    now = utcnow()
    rows = session.scalars(
        select(AnalysisQueue)
        .join(WeeklyReport, AnalysisQueue.report_id == WeeklyReport.id)
        .where(
            ai_report_condition(session),
            AnalysisQueue.status == AnalysisQueueStatus.PENDING,
            or_(
                AnalysisQueue.next_attempt_at.is_(None),
                AnalysisQueue.next_attempt_at <= now,
            ),
        )
        .order_by(AnalysisQueue.id)
        .limit(limit)
        # PostgreSQL 多 Worker 时跳过已被其它 Worker 锁住的行；SQLite 会安全忽略该子句。
        .with_for_update(skip_locked=True)
    ).all()
    claimed_ids: list[int] = []
    for row in rows:
        # 即使底层数据库不支持 row lock（SQLite），条件更新也保证只有真正把
        # pending 改为 running 的进程才会返回该任务，不能两边同时调模型。
        result = session.execute(
            update(AnalysisQueue)
            .where(
                AnalysisQueue.id == row.id,
                AnalysisQueue.status == AnalysisQueueStatus.PENDING,
            )
            .values(status=AnalysisQueueStatus.RUNNING)
        )
        if result.rowcount:
            claimed_ids.append(row.id)
    if claimed_ids:
        session.commit()
    return claimed_ids


def _claim_period_summary_batch(session: Session, limit: int) -> list[int]:
    """领取全员周期总结任务；与单篇分析队列相同的 CAS 领取语义。"""
    now = utcnow()
    rows = session.scalars(
        select(PeriodSummaryQueue)
        .where(
            PeriodSummaryQueue.status == AnalysisQueueStatus.PENDING,
            or_(
                PeriodSummaryQueue.next_attempt_at.is_(None),
                PeriodSummaryQueue.next_attempt_at <= now,
            ),
        )
        .order_by(PeriodSummaryQueue.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claimed_ids: list[int] = []
    for row in rows:
        result = session.execute(
            update(PeriodSummaryQueue)
            .where(
                PeriodSummaryQueue.id == row.id,
                PeriodSummaryQueue.status == AnalysisQueueStatus.PENDING,
            )
            .values(status=AnalysisQueueStatus.RUNNING)
        )
        if result.rowcount:
            claimed_ids.append(row.id)
    if claimed_ids:
        session.commit()
    return claimed_ids


def _claim_blocker_summary_batch(session: Session, limit: int) -> list[int]:
    """领取周期卡点总结任务；与周期总览相同的 CAS 领取语义。"""
    now = utcnow()
    rows = session.scalars(
        select(BlockerSummaryQueue)
        .where(
            BlockerSummaryQueue.status == AnalysisQueueStatus.PENDING,
            or_(
                BlockerSummaryQueue.next_attempt_at.is_(None),
                BlockerSummaryQueue.next_attempt_at <= now,
            ),
        )
        .order_by(BlockerSummaryQueue.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claimed_ids: list[int] = []
    for row in rows:
        result = session.execute(
            update(BlockerSummaryQueue)
            .where(
                BlockerSummaryQueue.id == row.id,
                BlockerSummaryQueue.status == AnalysisQueueStatus.PENDING,
            )
            .values(status=AnalysisQueueStatus.RUNNING)
        )
        if result.rowcount:
            claimed_ids.append(row.id)
    if claimed_ids:
        session.commit()
    return claimed_ids


# 经营看板总结的 RUNNING 租约时长。模型调用实测几十秒，留足一个数量级的余量；
# Worker 在调用期间被杀/重启后，队列行会停在 RUNNING 永远无人消费 ——
# 唯一出路本来是等 hash 变化或人工 force，这里按租约自动放回待处理。
OPERATING_SUMMARY_LEASE_SECONDS = 15 * 60


def reclaim_stale_operating_summaries(session: Session) -> int:
    """把租约过期的 RUNNING 经营总结任务放回待处理，返回回收条数。

    只按 ``updated_at`` 判断（CAS 领取与状态变更都会刷新它），不区分
    「真的还在跑」与「跑超时」：超过租约仍认 RUNNING，代价最多是同一任务
    被两个 Worker 各跑一次 —— 比永远卡死好。正常完成的任务 status 已离开
    RUNNING，不受影响。
    """
    cutoff = utcnow() - timedelta(seconds=OPERATING_SUMMARY_LEASE_SECONDS)
    result = session.execute(
        update(OperatingSummaryQueue)
        .where(
            OperatingSummaryQueue.status == AnalysisQueueStatus.RUNNING,
            OperatingSummaryQueue.updated_at < cutoff,
        )
        .values(status=AnalysisQueueStatus.PENDING, next_attempt_at=None)
    )
    if result.rowcount:
        session.commit()
    return result.rowcount


def _claim_operating_summary_batch(session: Session, limit: int) -> list[int]:
    """领取经营看板结构化总结任务；与周期总览相同的 CAS 领取语义。"""
    now = utcnow()
    rows = session.scalars(
        select(OperatingSummaryQueue)
        .where(
            OperatingSummaryQueue.status == AnalysisQueueStatus.PENDING,
            or_(
                OperatingSummaryQueue.next_attempt_at.is_(None),
                OperatingSummaryQueue.next_attempt_at <= now,
            ),
        )
        .order_by(OperatingSummaryQueue.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claimed_ids: list[int] = []
    for row in rows:
        result = session.execute(
            update(OperatingSummaryQueue)
            .where(
                OperatingSummaryQueue.id == row.id,
                OperatingSummaryQueue.status == AnalysisQueueStatus.PENDING,
            )
            .values(status=AnalysisQueueStatus.RUNNING)
        )
        if result.rowcount:
            claimed_ids.append(row.id)
    if claimed_ids:
        session.commit()
    return claimed_ids


def _claim_worker_batch(
    session: Session, workers: int
) -> tuple[list[int], dict[str, list[int]]]:
    """Claim no more work than the executor can start immediately.

    Queue rows are marked RUNNING when claimed. Claiming more rows than available
    threads makes tasks waiting inside ``ThreadPoolExecutor`` look like they have
    started, so the UI can report "generating" while no model call is running.
    Keep one slot for report analysis whenever there is more than one worker and
    rotate among the three summary queues for the remaining slots.
    """
    global _summary_queue_rotation

    workers = max(1, workers)
    summary_claimers = (
        ("period", _claim_period_summary_batch),
        ("blocker", _claim_blocker_summary_batch),
        ("operating", _claim_operating_summary_batch),
    )
    summary_ids = {kind: [] for kind, _ in summary_claimers}

    if workers == 1:
        # With a single thread, share it fairly among report analysis and all
        # summary types instead of allowing either class to starve indefinitely.
        task_claimers = (
            ("report", lambda: _claim_batch(session, limit=1)),
            *(
                (kind, lambda claim=claim: claim(session, limit=1))
                for kind, claim in summary_claimers
            ),
        )
        for offset in range(len(task_claimers)):
            index = (_summary_queue_rotation + offset) % len(task_claimers)
            kind, claim = task_claimers[index]
            claimed = claim()
            if claimed:
                _summary_queue_rotation = (index + 1) % len(task_claimers)
                if kind == "report":
                    return claimed, summary_ids
                summary_ids[kind] = claimed
                return [], summary_ids
        _summary_queue_rotation = (_summary_queue_rotation + 1) % len(task_claimers)
        return [], summary_ids

    summary_budget = min(len(summary_claimers), workers - 1)
    start_index = _summary_queue_rotation % len(summary_claimers)
    claimed_summary_count = 0
    last_claimed_index: int | None = None
    for offset in range(len(summary_claimers)):
        if claimed_summary_count >= summary_budget:
            break
        index = (start_index + offset) % len(summary_claimers)
        kind, claim = summary_claimers[index]
        claimed = claim(session, limit=1)
        if claimed:
            summary_ids[kind] = claimed
            claimed_summary_count += len(claimed)
            last_claimed_index = index

    if last_claimed_index is not None:
        _summary_queue_rotation = (last_claimed_index + 1) % len(summary_claimers)
    else:
        _summary_queue_rotation = (start_index + 1) % len(summary_claimers)

    report_ids = _claim_batch(session, limit=workers - claimed_summary_count)
    return report_ids, summary_ids


def _backoff_seconds(attempts: int) -> float:
    base = get_settings().ai_analysis_retry_base_seconds
    return base * (2 ** max(attempts - 1, 0))


def process_queue_item(queue_id: int) -> dict[str, Any]:
    """消费一个已标记 running 的队列项；每个线程持有独立 Session。"""
    session = get_session_factory()()
    try:
        queue = session.get(AnalysisQueue, queue_id)
        if queue is None or queue.status != AnalysisQueueStatus.RUNNING:
            return {"queue_id": queue_id, "status": "skipped"}
        report = session.get(WeeklyReport, queue.report_id)
        if report is None:
            raise ValueError(f"周报 {queue.report_id} 不存在")
        if report_ai_exempt(session, report):
            queue.status = AnalysisQueueStatus.PENDING
            queue.last_error = None
            session.commit()
            return {
                "queue_id": queue_id,
                "status": "skipped",
                "reason": "免提交人员不参与 AI 分析",
            }

        from app.services.report_analysis_service import analyze_report

        analyze_report(
            session,
            report.id,
            force=False,
            queue_attempt=(queue.attempts or 0) + 1,
        )
        queue = session.get(AnalysisQueue, queue_id)
        if queue is not None and queue.status == AnalysisQueueStatus.RUNNING:
            queue.status = AnalysisQueueStatus.SUCCESS
            queue.last_error = None
            session.commit()
        return {"queue_id": queue_id, "status": "success"}
    except Exception as exc:  # noqa: BLE001 Worker 必须把失败落回队列，不能静默
        session.rollback()
        queue = session.get(AnalysisQueue, queue_id)
        if queue is None:
            return {"queue_id": queue_id, "status": "failed", "error": str(exc)}
        queue.attempts = (queue.attempts or 0) + 1
        max_attempts = get_settings().ai_analysis_max_attempts
        if queue.attempts >= max_attempts:
            queue.status = AnalysisQueueStatus.FAILED
        else:
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = utcnow() + timedelta(
                seconds=_backoff_seconds(queue.attempts)
            )
        queue.last_error = str(exc)[:2000]
        session.commit()
        return {
            "queue_id": queue_id,
            "status": "failed",
            "attempts": queue.attempts,
            "error": str(exc)[:500],
        }
    finally:
        session.close()


def run_worker_once(
    concurrency: int | None = None, *, run_backfill: bool = False
) -> dict[str, Any]:
    """跑一轮：领取任务并按配置并发分析。concurrency=1 时逐条执行，便于测试。

    ``run_backfill`` 默认 False：历史投影是**一次性数据修复**，而不是每轮要跑的
    常规工作。旧实现每轮都跑（默认轮询间隔 3 秒），等于把一条反连接 + 最多
    100 篇的重物化当作常驻后台负载；而且永远拿不到 ReportScore 的那些周报
    会被无限重复物化。Worker 现在按 ``ai_backfill_interval_seconds`` 触发它。
    """
    settings = get_settings()
    workers = concurrency or settings.ai_worker_concurrency
    with session_scope() as session:
        backfilled = 0
        # 每轮先回收上一进程崩掉留下的 RUNNING 僵尸（租约过期即放回待处理）。
        reclaimed = reclaim_stale_operating_summaries(session)
        if reclaimed:
            logger.warning("operating_summary 回收 %d 条过期 RUNNING 任务", reclaimed)
        if run_backfill:
            # 升级后历史成功分析需要投影成 P1 派生数据；只读已有 JSON，零模型调用。
            from app.services.structured_data_service import backfill_cached_analyses

            backfilled = backfill_cached_analyses(session, limit=workers * 20)
        # The worker may host report, period, blocker and operating-summary jobs.
        # Claim a batch whose total size fits the pool so every RUNNING row has a
        # thread ready to execute it instead of waiting invisibly in the executor.
        queue_ids, summary_ids = _claim_worker_batch(session, workers)
        period_queue_ids = summary_ids["period"]
        blocker_queue_ids = summary_ids["blocker"]
        operating_queue_ids = summary_ids["operating"]

    if not queue_ids and not period_queue_ids and not blocker_queue_ids and not operating_queue_ids:
        return {
            "claimed": 0,
            "processed": 0,
            "period_claimed": 0,
            "period_processed": 0,
            "blocker_claimed": 0,
            "blocker_processed": 0,
            "operating_claimed": 0,
            "operating_processed": 0,
            "failed": 0,
            "backfilled": backfilled,
        }

    tasks: list[tuple[str, int]] = [("report", queue_id) for queue_id in queue_ids]
    tasks.extend(("period", queue_id) for queue_id in period_queue_ids)
    tasks.extend(("blocker", queue_id) for queue_id in blocker_queue_ids)
    tasks.extend(("operating", queue_id) for queue_id in operating_queue_ids)

    def _process(task: tuple[str, int]) -> dict[str, Any]:
        kind, queue_id = task
        if kind == "period":
            from app.services.period_summary_service import process_period_summary_item

            return process_period_summary_item(queue_id)
        if kind == "blocker":
            from app.services.blocker_summary_service import process_blocker_summary_item

            return process_blocker_summary_item(queue_id)
        if kind == "operating":
            from app.services.operating_summary_service import process_summary_item

            return process_summary_item(queue_id)
        return process_queue_item(queue_id)

    if workers == 1:
        results = [_process(task) for task in tasks]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_process, tasks))

    failed = sum(1 for item in results if item.get("status") == "failed")
    return {
        "claimed": len(queue_ids),
        "processed": len(queue_ids),
        "period_claimed": len(period_queue_ids),
        "period_processed": len(period_queue_ids),
        "blocker_claimed": len(blocker_queue_ids),
        "blocker_processed": len(blocker_queue_ids),
        "operating_claimed": len(operating_queue_ids),
        "operating_processed": len(operating_queue_ids),
        "failed": failed,
        "backfilled": backfilled,
    }
