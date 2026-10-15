"""消息中心：把散落各处的报错与提醒汇总成一份可点开的清单。

存在的意义：同步失败、AI 欠费、待分析积压、周期总结没生成、有人没交周报……
这些提示以前直接铺在首页工具栏上，一条可信 IP 报错就能占掉两行并挤走业务指标；
而且它们分属不同页面，看完一处还得自己去别处找下一处。

这里只做**汇总与排序**，不产生任何新判断：
  - 同步类直接复用 sync_status（未恢复的失败才算故障，修好后自动消失）
  - AI 类复用 ai_capability_status + 分析队列 / 周期总结队列
  - 业务提醒（未提交人数）复用看板同一套口径
所以消息中心说「好了」，和首页、设置页说的必定是同一件事。

每条消息都带稳定 ``id`` 与 ``at``：前端按 ``id@at`` 记忆已读，
同一问题没变化时不会反复提醒；换了个失败原因则重新变回未读。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time_utils import utcnow
from app.models.analysis_queue import AnalysisQueue
from app.models.employee import Employee
from app.models.enums import AnalysisQueueStatus, ReminderStatus, SyncType
from app.models.reminder_log import (
    BATCH_FAILURE_DETAIL_PREFIX,
    LEGACY_RECIPIENT_UNDELIVERED_PREFIX,
    RECIPIENT_UNDELIVERED_DETAIL_PREFIX,
    ReminderLog,
)
from app.models.weekly_report import WeeklyReport
from app.services.ai_status_service import ai_capability_status, ai_last_call_failed
from app.services.management_action_message_service import unread_action_replies
from app.services.period_summary_service import period_summary_state
from app.services.runtime_config_service import runtime_settings
from app.services.sync_scheduler import sync_status
from app.services.view_service import (
    ai_report_condition,
    report_period_overlap_conditions,
    reportable_employee_ids,
    resolve_period,
    submitted_employee_ids,
)

# 排序权重：报错永远排在最前，其次是要人做决定的提醒，最后是纯知会。
_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}

_SYNC_TYPE_LABELS = {
    SyncType.DEPARTMENTS: "部门",
    SyncType.EMPLOYEES: "员工",
    SyncType.WEEKLY_REPORTS: "周报",
}
# sync_status 里三个同步类型各自挂在哪个键下
_SYNC_STATUS_KEYS = {
    SyncType.DEPARTMENTS: "departments",
    SyncType.EMPLOYEES: "employees",
    SyncType.WEEKLY_REPORTS: "reports",
}

SETTINGS_HREF = "/settings"


def _settings_href(category: str) -> str:
    """深链到系统设置的某个分类，省掉「打开设置页再自己找」的一步。"""
    return f"{SETTINGS_HREF}?category={category}"


def _item(
    *,
    item_id: str,
    kind: str,
    level: str,
    title: str,
    summary: str,
    href: str | None = None,
    action_label: str | None = None,
    hint: str | None = None,
    raw: str | None = None,
    at: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "kind": kind,
        "level": level,
        "title": title,
        "summary": summary,
        "hint": hint,
        "raw": raw,
        "at": at,
        "href": href,
        "action_label": action_label,
        "meta": meta or {},
    }


def _sync_failure_items(status: dict[str, Any]) -> list[dict[str, Any]]:
    """同步失败按「根因」合并，每类最多一条。

    同一个可信 IP 没加白，会让部门、员工、周报三个同步一起报 60020；
    如果原样铺三条一模一样的卡片，用户要读三遍同一句话才知道是一条问题
    —— 那正是消息中心想消掉的噪音。所以把 summary 相同的失败合成一条，
    标题里列出涉及的同步类型。

    只要某类之后成功过，后端就已经把它的 detail 清空了，因此修好即消失。
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for sync_type, key in _SYNC_STATUS_KEYS.items():
        detail = (status.get(key) or {}).get("last_error_detail")
        if not detail:
            continue
        summary = detail.get("summary") or detail.get("title") or "同步未成功"
        # 用「错误码 + 结论」做根因键：同一结论才合并，换了原因就是另一条消息。
        signature = (str(detail.get("vendor_code") or detail.get("code") or ""), summary)
        group = groups.setdefault(signature, {"types": [], "detail": detail, "at": None})
        group["types"].append(sync_type)
        # 组内取最早发生的那次：先坏的先报，时间线才对得上
        at = detail.get("at")
        if at and (group["at"] is None or at < group["at"]):
            group["at"] = at

    items: list[dict[str, Any]] = []
    for (code, summary), group in groups.items():
        types = sorted(group["types"])
        labels = "、".join(_SYNC_TYPE_LABELS.get(item, item) for item in types)
        detail = group["detail"]
        merged = len(types) > 1
        items.append(
            _item(
                item_id="sync_failure:" + ",".join(types),
                kind="sync_failure",
                level="error",
                title=f"{labels}同步失败",
                summary=summary,
                hint=detail.get("hint"),
                raw=detail.get("raw"),
                at=group["at"],
                href=_settings_href("sync"),
                action_label="去系统设置处理",
                meta={
                    "sync_types": types,
                    "merged": merged,
                    "vendor_code": detail.get("vendor_code") or code or None,
                    "egress_ip": detail.get("egress_ip"),
                    "job_id": detail.get("job_id"),
                },
            )
        )
    return items


def _sync_queue_item(status: dict[str, Any]) -> list[dict[str, Any]]:
    pending = int(status.get("pending_count") or 0)
    running = int(status.get("running_count") or 0)
    if pending + running == 0:
        return []
    jobs = status.get("jobs") or []
    at = None
    if jobs:
        latest = jobs[0]
        at = latest.get("start_time") or latest.get("end_time")
    return [
        _item(
            item_id="sync_queue",
            kind="sync_queue",
            level="info",
            title="同步任务正在执行",
            summary=f"队列 {pending} 条待执行 / {running} 条执行中，完成后数据自动更新。",
            at=at,
            href="/dashboard",
            action_label="回管理简报",
            meta={"pending": pending, "running": running},
        )
    ]


def _ai_items(session: Session) -> list[dict[str, Any]]:
    status = ai_capability_status(session)
    items: list[dict[str, Any]] = []

    if ai_last_call_failed(status):
        items.append(
            _item(
                item_id="ai_call_failed",
                kind="ai_failure",
                level="error",
                title="上次 AI 调用失败",
                summary=status.get("last_error") or "模型调用返回失败，页面上的 AI 结论会保持旧值。",
                hint="常见原因：余额不足、Key 失效或被限流。可在「AI 检索」页查看用量与失败记录。",
                at=status.get("last_call_at"),
                href="/ai",
                action_label="查看 AI 状态",
                meta={"provider": status.get("provider"), "model": status.get("model")},
            )
        )

    if not status.get("configured"):
        items.append(
            _item(
                item_id="ai_unconfigured",
                kind="ai_unconfigured",
                level="warning",
                title="AI 能力未配置",
                summary=status.get("hint") or "尚未配置模型密钥，AI 总结与检索都不可用。",
                at=status.get("last_call_at"),
                href=_settings_href("ai"),
                action_label="去配置密钥",
            )
        )
    return items


def _analysis_backlog_items(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    """本周期 AI 分析进度：失败优先于待处理，两类都存在时只报失败那条。"""
    rows = session.execute(
        select(AnalysisQueue.status, func.count(AnalysisQueue.id))
        .select_from(AnalysisQueue)
        .join(WeeklyReport, WeeklyReport.id == AnalysisQueue.report_id)
        .where(
            *report_period_overlap_conditions(start, end),
            # ai_report_condition 是一条完整的 OR 表达式，必须整体传入；
            # 用 * 展开会把 OR 拆成多个参数，where 之间变成 AND，结果恒为空。
            ai_report_condition(session),
        )
        .group_by(AnalysisQueue.status)
    ).all()
    counts = {str(row[0]): int(row[1]) for row in rows}
    failed = counts.get(AnalysisQueueStatus.FAILED, 0)
    waiting = counts.get(AnalysisQueueStatus.PENDING, 0) + counts.get(
        AnalysisQueueStatus.RUNNING, 0
    )

    items: list[dict[str, Any]] = []
    if failed:
        items.append(
            _item(
                item_id=f"analysis_failed:{start.isoformat()}",
                kind="analysis_failed",
                level="warning",
                title=f"{failed} 篇周报 AI 分析失败",
                summary="这些周报的人工结论暂时缺失，会影响本期洞察与总结的完整度。",
                hint="可重启 app.worker 后重跑；仍未成功时先看「AI 检索」页的最近失败原因。",
                href=f"/reports?start={start.isoformat()}&end={end.isoformat()}&ai_flag=unchecked",
                action_label="查看未分析周报",
                meta={"failed": failed, "waiting": waiting},
            )
        )
        return items

    if waiting:
        items.append(
            _item(
                item_id=f"analysis_waiting:{start.isoformat()}",
                kind="analysis_waiting",
                level="info",
                title=f"{waiting} 篇周报待分析",
                summary="已进入后台分析队列，Worker 跑完后本页数据自动更新。",
                href=f"/dashboard?start={start.isoformat()}&end={end.isoformat()}",
                action_label="回管理简报",
                meta={"waiting": waiting},
            )
        )
    return items


def _period_summary_items(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    state = period_summary_state(session, start, end)
    status = state.get("status")
    if status == "empty":
        return []
    period_start = start.isoformat()
    href = f"/dashboard?start={period_start}&end={end.isoformat()}"

    if status == AnalysisQueueStatus.FAILED:
        return [
            _item(
                item_id=f"period_summary_failed:{period_start}",
                kind="period_summary",
                level="warning",
                title="本期 AI 总结生成失败",
                summary=state.get("last_error") or "汇总任务执行失败，未产出总结。",
                hint="确认 app.worker 常驻且密钥可用后，可在首页重新生成本期总结。",
                href=href,
                action_label="去首页重试",
            )
        ]
    if status in (AnalysisQueueStatus.PENDING, AnalysisQueueStatus.RUNNING):
        return [
            _item(
                item_id=f"period_summary_running:{period_start}",
                kind="period_summary",
                level="info",
                title="本期 AI 总结生成中",
                summary="后台正在汇总本期周报，完成后首页「AI 本周洞察」会直接显示结论。",
                href=href,
                action_label="回管理简报",
            )
        ]
    if state.get("stale"):
        return [
            _item(
                item_id=f"period_summary_stale:{period_start}",
                kind="period_summary",
                level="info",
                title="本期 AI 总结已过期",
                summary="本期周报有新增或改动，旧总结不再代表当前内容。",
                hint="下次抓取本周周报后会自动按最新正文重算；也可在首页「本周概览」立即重算。",
                href=href,
                action_label="去重新生成",
            )
        ]
    if status == "not_generated":
        # 总览在周报抓取后自动生成，所以这一条通常只在自动链路没跑到时出现。
        return [
            _item(
                item_id=f"period_summary_missing:{period_start}",
                kind="period_summary",
                level="info",
                title="本期还没有 AI 总结",
                summary=f"本期已入库 {state.get('reports_total', 0)} 篇周报，尚未生成整体结论。",
                hint="确认 app.worker 常驻后抓取本期周报即可自动生成。",
                href=href,
                action_label="去生成",
            )
        ]
    return []


def _unsubmitted_items(session: Session, start: date, end: date) -> list[dict[str, Any]]:
    """未提交提醒：口径与首页提交率完全一致（在职 - 免提交 - 已提交）。"""
    reportable = reportable_employee_ids(session)
    if not reportable:
        return []
    submitted = len(submitted_employee_ids(session, start, end) & reportable)
    missing = len(reportable) - submitted
    if missing <= 0:
        return []
    return [
        _item(
            item_id=f"unsubmitted:{start.isoformat()}",
            kind="unsubmitted",
            level="warning",
            title=f"{missing} 人还没交本期周报",
            summary=f"本期应提交 {len(reportable)} 人，已提交 {submitted} 人。",
            hint="可在首页「处理未提交名单」里按部门催交，或生成企微提醒预览。",
            href=f"/dashboard?start={start.isoformat()}&end={end.isoformat()}",
            action_label="去处理",
            meta={"unsubmitted": missing, "reportable": len(reportable)},
        )
    ]


def _action_reply_items(session: Session) -> list[dict[str, Any]]:
    """负责人回复了跟进事项：提醒老板去处理。

    按**事项**聚合而不是逐条消息：一条跟进对话里连回三条不该变成三条待办，
    老板需要知道的是「哪几件事有人在等我」。
    """
    unread = unread_action_replies(session)
    if not unread:
        return []
    total = sum(item["count"] for item in unread)
    # 取最近一条回复的时间作为排序依据：新的回复应该把卡片顶上来。
    latest = max((item["last_at"] for item in unread if item["last_at"]), default=None)
    titles = [item["title"] for item in unread[:3]]
    more = len(unread) - len(titles)
    summary = "、".join(titles) + (f" 等 {len(unread)} 件事" if more > 0 else "")
    return [
        _item(
            item_id="action_reply:unread",
            kind="action_reply",
            level="warning",
            title=f"{len(unread)} 件跟进事项有 {total} 条新回复",
            summary=summary,
            hint="都是负责人回给你的进展或问题，点开逐条查看并回复。",
            href="/blockers?tab=actions",
            action_label="去处理",
            at=latest,
            meta={"unread": total, "actions": len(unread)},
        )
    ]


def _reminder_delivery_items(
    session: Session, start: date, end: date
) -> list[dict[str, Any]]:
    """本期催交里企微没投递出去的人 —— 这类问题只有管理员能修。

    典型场景：自建应用的可见范围没包含某些部门，企微接口照样返回 errcode=0，
    于是页面上写着「已提醒 20 人」，实际只有一个人收到。不主动报出来，
    这件事就只能靠收件人一句「我没收到」才发现。
    """
    rows = session.execute(
        select(
            ReminderLog.id,
            ReminderLog.employee_id,
            ReminderLog.status,
            ReminderLog.detail,
            ReminderLog.triggered_by,
            ReminderLog.created_at,
        )
        .where(
            # 催交记录按完整周期写入；精确匹配，避免相邻/自定义周期的历史投递
            # 混进当前消息中心。
            ReminderLog.period_start == start,
            ReminderLog.period_end == end,
            ReminderLog.status.in_((ReminderStatus.FAILED, ReminderStatus.SENT)),
        )
        .order_by(ReminderLog.created_at.desc(), ReminderLog.id.desc())
    ).all()

    # 同一员工同一周期只认最新一次：修复可见范围后重发成功，旧 FAILED 就不再是
    # 当前告警；重复失败也不能把人数和姓名重复累计。
    latest_by_employee: dict[int, Any] = {}
    for row in rows:
        latest_by_employee.setdefault(row.employee_id, row)
    latest_rows = list(latest_by_employee.values())
    undelivered = [
        row
        for row in latest_rows
        if row.status == ReminderStatus.FAILED
        and _is_recipient_undelivered(row.detail)
    ]

    items: list[dict[str, Any]] = []

    # 整批请求未被企微受理时也要进消息中心，但只能作为整批失败，不能伪装成
    # 「部分收件人没收到」。send_reminders 写入了结构化前缀，升级前的 81013/14
    # 记录也不会命中逐人前缀，因此不会再被误报成 partial。
    latest = rows[0] if rows else None
    if (
        latest
        and latest.status == ReminderStatus.FAILED
        and _is_batch_failure(latest.detail)
    ):
        raw_detail = _strip_detail_prefix(latest.detail, BATCH_FAILURE_DETAIL_PREFIX)
        items.append(
            _item(
                item_id=f"reminder_failure:{start.isoformat()}",
                kind="reminder_failure",
                level="error",
                title="催交发送失败",
                summary=f"企业微信未受理：{raw_detail or '未知原因'}",
                hint="确认企业微信凭据、网络和应用权限后，可重新打开催交预览并发送。",
                raw=raw_detail or latest.detail,
                at=latest.created_at.isoformat() if latest.created_at else None,
                href=_settings_href("rules"),
                action_label="去周报规则处理",
                meta={"triggered_by": latest.triggered_by},
            )
        )

    if not undelivered:
        return items

    names = session.scalars(
        select(Employee.name)
        .where(Employee.id.in_({row.employee_id for row in undelivered}))
        .order_by(Employee.id)
    ).all()
    preview = "、".join(names[:8])
    if len(names) > 8:
        preview += f" 等 {len(names)} 人"
    items.append(
        _item(
            item_id=f"reminder_undelivered:{start.isoformat()}",
            kind="reminder_undelivered",
            level="warning",
            title=f"{len(undelivered)} 人没收到催交提醒",
            summary=f"企业微信未投递：{preview}。",
            hint="到企业微信管理后台确认该自建应用的「可见范围」包含这些人所属的部门，"
            "否则接口返回成功、消息不会送达。",
            href=_settings_href("rules"),
            action_label="去周报规则查看",
            at=max(
                (row.created_at for row in undelivered if row.created_at),
                default=None,
            ).isoformat()
            if any(row.created_at for row in undelivered)
            else None,
            meta={"undelivered": len(undelivered)},
        )
    )
    return items


def _is_recipient_undelivered(detail: str | None) -> bool:
    """识别逐人不可投递记录，兼容修复前已写入的旧 detail。"""
    return bool(
        detail
        and (
            detail.startswith(RECIPIENT_UNDELIVERED_DETAIL_PREFIX)
            or detail.startswith(LEGACY_RECIPIENT_UNDELIVERED_PREFIX)
        )
    )


def _is_batch_failure(detail: str | None) -> bool:
    return bool(detail and detail.startswith(BATCH_FAILURE_DETAIL_PREFIX))


def _strip_detail_prefix(detail: str | None, prefix: str) -> str:
    if not detail:
        return ""
    return detail[len(prefix) :] if detail.startswith(prefix) else detail


def _auto_sync_item(session: Session) -> list[dict[str, Any]]:
    if runtime_settings(session).sync_auto_enabled:
        return []
    return [
        _item(
            item_id="auto_sync_disabled",
            kind="auto_sync",
            level="info",
            title="自动同步未开启",
            summary="当前只能手动抓取周报；定时抓取需要开启开关并保证 app.worker 常驻。",
            href=_settings_href("sync"),
            action_label="去开启",
        )
    ]


def build_notification_feed(
    session: Session,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """汇总当前所有待处理消息，按「报错 → 提醒 → 知会」排序返回。"""
    start_date, end_date = resolve_period(start, end)
    status = sync_status(session)

    items: list[dict[str, Any]] = []
    items += _sync_failure_items(status)
    items += _ai_items(session)
    items += _analysis_backlog_items(session, start_date, end_date)
    items += _period_summary_items(session, start_date, end_date)
    items += _reminder_delivery_items(session, start_date, end_date)
    items += _action_reply_items(session)
    items += _unsubmitted_items(session, start_date, end_date)
    items += _sync_queue_item(status)
    items += _auto_sync_item(session)

    # 排序：等级优先，同级内新消息在前；没有时间的（队列、开关类）垫底。
    items.sort(
        key=lambda item: (
            _LEVEL_ORDER.get(str(item["level"]), 3),
            -_timestamp(item["at"]) if item["at"] else float("inf"),
        )
    )

    counts = {"error": 0, "warning": 0, "info": 0}
    for item in items:
        level = str(item["level"])
        if level in counts:
            counts[level] += 1

    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "generated_at": utcnow().isoformat(),
        "total": len(items),
        "counts": counts,
        "items": items,
    }


def _timestamp(value: Any) -> float:
    """把消息的 ISO 时间串折成可比较的数字；解析不了就当成最早，不抛错。"""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0
