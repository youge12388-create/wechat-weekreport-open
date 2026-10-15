"""自动催交调度与后台状态。

只在设置的业务日、业务时刻之后触发；同周期已有成功自动记录时不重复发送。
发送前若周报同步仍在排队/执行，则等待同步结束，避免把刚提交的人误报为未提交。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.enums import ReminderRunStatus, SyncJobStatus, SyncType
from app.models.reminder_run import ReminderRun
from app.models.sync_job import SyncJob
from app.services.reminder_service import resolve_remind_targets, send_reminders
from app.services.runtime_config_service import runtime_settings
from app.services.sync_scheduler import current_period_date_range

logger = get_logger(__name__)

AUTO_TRIGGER_PREFIX = "auto:weekly_due"
FAILED_RETRY_SECONDS = 15 * 60
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def period_attribution_label(
    days: int,
    *,
    period_rule: str = "week",
    custom_week_start: int = 0,
) -> str:
    """把周期归属天数说成人话（前端直接展示，不重算规则）。

    自然周下管理员关心的是「周一交的算不算上周」；月度和自定义周不能
    硬编码成周一，否则设置页会把实际生效口径说错。
    """
    if days <= 0:
        return "按提交当天所在周期归类（不做回溯）"
    if period_rule == "month":
        return f"每月前 {days} 天提交的报告归上一月"
    if days >= 7:
        return "周期开始后的前 7 天都归上一周期"
    first = WEEKDAY_LABELS[custom_week_start % 7]
    last = WEEKDAY_LABELS[(custom_week_start + days - 1) % 7]
    span = first if days == 1 else f"{first}~{last}"
    return f"{span}提交的周报归上一周期"


def trigger_key(start, end) -> str:
    return f"{AUTO_TRIGGER_PREFIX}:{start.isoformat()}:{end.isoformat()}"


def _business_now(now: datetime | None, offset_hours: int) -> datetime:
    return (now or utcnow()) + timedelta(hours=offset_hours)


def next_run_at(session: Session, now: datetime | None = None) -> datetime | None:
    """返回下一次计划运行时间（UTC naive，供前端统一格式化）。"""
    settings = runtime_settings(session)
    local = _business_now(now, settings.report_timezone_offset_hours)
    days_ahead = (settings.remind_auto_weekday - local.weekday()) % 7
    candidate = datetime.combine(
        local.date() + timedelta(days=days_ahead),
        time(hour=settings.remind_auto_hour),
    )
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate - timedelta(hours=settings.report_timezone_offset_hours)


def _due_context(session: Session, now: datetime | None):
    settings = runtime_settings(session)
    local = _business_now(now, settings.report_timezone_offset_hours)
    if local.weekday() != settings.remind_auto_weekday:
        return None
    if local.hour < settings.remind_auto_hour:
        return None
    # 用与抓取、看板同一个周期推导入口，别在这里另写一份自然周假设：
    # 周期规则改成自然月/自定义周时，催交名单必须跟着走，否则会去催一个
    # 与页面上不同的周期。
    start, end = current_period_date_range(session, now)
    return settings, start, end


def maybe_send_scheduled_reminders(
    session: Session, now: datetime | None = None
) -> dict[str, Any]:
    """Worker 每轮调用；未到时间或已完成时安全跳过。"""
    context = _due_context(session, now)
    settings = runtime_settings(session)
    if not settings.remind_auto_enabled:
        return {"status": "disabled", "next_run_at": next_run_at(session, now)}
    if context is None:
        return {"status": "not_due", "next_run_at": next_run_at(session, now)}

    settings, start, end = context
    active_sync = session.scalars(
        select(SyncJob)
        .where(
            SyncJob.type == SyncType.WEEKLY_REPORTS,
            SyncJob.status.in_((SyncJobStatus.PENDING, SyncJobStatus.RUNNING)),
        )
        .limit(1)
    ).first()
    if active_sync is not None:
        return {
            "status": "waiting_for_sync",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
        }

    key = trigger_key(start, end)
    current = now or utcnow()
    run = session.scalar(
        select(ReminderRun).where(ReminderRun.trigger_key == key)
    )
    if run is not None and run.status in (
        ReminderRunStatus.SUCCESS,
        ReminderRunStatus.SKIPPED,
        # 部分投递也算本轮已执行：重试不会让「不在应用可见范围」的人变得可投递，
        # 反而会把已经收到的人再打扰一遍（冷却只能挡住 24 小时内的重复）。
        # 结论以 last_error 呈现，交给管理员去调可见范围。
        ReminderRunStatus.PARTIAL,
    ):
        return {
            "status": "already_sent",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
        }
    if run is not None:
        reference = run.finished_at or run.started_at
        if reference and reference >= current - timedelta(seconds=FAILED_RETRY_SECONDS):
            return {
                "status": "retry_wait",
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "last_error": run.error,
            }

    if run is None:
        run = ReminderRun(
            trigger_key=key,
            period_start=start,
            period_end=end,
            status=ReminderRunStatus.RUNNING,
            target_count=0,
            skipped_count=0,
            started_at=current,
        )
        session.add(run)
    else:
        run.status = ReminderRunStatus.RUNNING
        run.started_at = current
        run.finished_at = None
        run.error = None
    session.commit()

    try:
        result = send_reminders(
            session,
            start,
            end,
            dry_run=False,
            triggered_by=key,
        )
    except Exception as exc:  # noqa: BLE001 自动任务必须落失败状态并允许后续重试
        run.status = ReminderRunStatus.FAILED
        run.error = str(exc)[:2000]
        run.finished_at = utcnow()
        session.commit()
        logger.warning("自动催交异常：周期 %s ~ %s，%s", start, end, exc)
        return {
            "status": "failed",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "error": str(exc)[:500],
        }

    run.target_count = int(result.get("target_count") or 0)
    run.skipped_count = len(result.get("skipped") or [])
    run.error = result.get("error")
    run.finished_at = utcnow()
    outcome = str(result.get("outcome") or "no_targets")
    delivered = int((result.get("result") or {}).get("delivered_count") or 0)

    if outcome == "failed":
        # 请求根本没被企微受理（网络 / 鉴权 / 权限），重试有意义。
        run.status = ReminderRunStatus.FAILED
        session.commit()
        logger.warning(
            "自动催交失败：周期 %s ~ %s，%s 人，错误=%s",
            start, end, run.target_count, result.get("error"),
        )
        return {
            "status": "failed",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "error": result.get("error"),
        }
    if outcome == "sent" and delivered < run.target_count:
        # 请求被受理、但有人送不到（不在应用可见范围 / 已不在通讯录）。
        # **与 FAILED 分开**：重试不会让人变得可投递，反而会打扰已收到的人
        # （冷却只能挡住 24 小时内的重复）。结论交给管理员去调可见范围。
        run.status = ReminderRunStatus.PARTIAL
        session.commit()
        logger.warning(
            "自动催交部分送达：周期 %s ~ %s，目标 %s 人，实际送达 %s 人，%s",
            start, end, run.target_count, delivered, result.get("error"),
        )
        return {
            "status": "partial",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "target_count": run.target_count,
            "delivered_count": delivered,
            "error": result.get("error"),
        }
    if outcome == "sent":
        run.status = ReminderRunStatus.SUCCESS
        session.commit()
        logger.info("自动催交完成：周期 %s ~ %s，送达 %s 人", start, end, delivered)
        return {
            "status": "sent",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "target_count": run.target_count,
            "delivered_count": delivered,
        }
    return _no_target_result(session, run, start, end)


def _no_target_result(
    session: Session, run: ReminderRun, start, end
) -> dict[str, Any]:
    """本轮没有可发对象（全部已提交 / 都在冷却期 / 全部免提交）。

    它是「本轮没什么可做」，既不是失败也不是成功送达，状态与日志都要和
    上面两条分支区分开：否则一个「全员已交」的正常周一会被记成 failed。
    """
    run.status = ReminderRunStatus.SKIPPED
    session.commit()
    logger.info("自动催交无需发送：周期 %s ~ %s，目标 0 人", start, end)
    return {
        "status": "no_targets",
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "target_count": 0,
    }


def reminder_status(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """给设置页返回配置、下一次运行和最近一次自动执行结果。"""
    settings = runtime_settings(session)
    start, end = current_period_date_range(session, now)
    targets, _skipped = resolve_remind_targets(session, start, end, None)

    latest_run = session.scalars(
        select(ReminderRun)
        .order_by(ReminderRun.started_at.desc(), ReminderRun.id.desc())
        .limit(1)
    ).first()

    return {
        "auto_enabled": settings.remind_auto_enabled,
        "weekday": settings.remind_auto_weekday,
        "weekday_label": WEEKDAY_LABELS[settings.remind_auto_weekday],
        "hour": settings.remind_auto_hour,
        # 周期归属天数：周期开始后前 N 天内提交仍归上一周期。
        # 连同人话说明一起返回，前端直接展示、不重算规则（避免界面与实际各算一套）。
        "attribution_days": settings.report_period_attribution_days,
        "attribution_label": period_attribution_label(
            settings.report_period_attribution_days,
            period_rule=settings.report_period_rule,
            custom_week_start=settings.report_custom_week_start,
        ),
        "period_rule": settings.report_period_rule,
        # 催交文案：设置页可直接编辑，改完下一次发送即生效（运行时配置）。
        "card_title": settings.remind_card_title,
        "card_body_zh": settings.remind_card_body_zh,
        "card_body_en": settings.remind_card_body_en,
        "card_url": settings.remind_card_url,
        # 跟进沟通中「负责人回复了」要通知到的企微 userid（逗号分隔）。
        # 参与过会话的企微管理员会被自动纳入，这里是补充 —— 访问密码登录的
        # 管理员没有员工身份，不配这一项就收不到任何回复通知。
        "action_notify_admin_userids": settings.action_notify_admin_userids,
        "next_run_at": next_run_at(session, now).isoformat()
        if next_run_at(session, now)
        else None,
        "current_period": {"start": start.isoformat(), "end": end.isoformat()},
        "current_target_count": len(targets),
        "last_run_at": latest_run.finished_at.isoformat()
        if latest_run and latest_run.finished_at
        else None,
        "last_status": latest_run.status if latest_run else None,
        "last_error": latest_run.error if latest_run else None,
        "last_target_count": latest_run.target_count if latest_run else None,
    }
