"""周报催交的共享业务逻辑。

API 手动发送与后台自动发送必须走同一套目标计算、冷却和审计逻辑，
否则很容易出现“页面上显示会提醒，定时任务却漏人或重复发”的偏差。
"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape
from typing import Any

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from app.connectors import base as cb
from app.connectors.wecom.client import WeComClient
from app.core.config import Settings, get_settings
from app.core.time_utils import utcnow
from app.models.employee import Employee
from app.models.enums import ReminderChannel, ReminderStatus
from app.models.reminder_log import (
    BATCH_FAILURE_DETAIL_PREFIX,
    RECIPIENT_UNDELIVERED_DETAIL_PREFIX,
    ReminderLog,
)
from app.services.runtime_config_service import runtime_settings
from app.services.view_service import (
    department_name_map,
    reportable_employee_conditions,
    submitted_employee_ids,
)


def last_sent_at(session: Session, emp_id: int, start: date, end: date):
    """该员工该周期最近一次成功催交的时间（用于冷却判断与前端提示）。"""
    row = session.scalars(
        select(ReminderLog)
        .where(
            ReminderLog.employee_id == emp_id,
            ReminderLog.period_start == start,
            ReminderLog.period_end == end,
            ReminderLog.status == ReminderStatus.SENT,
        )
        .order_by(ReminderLog.created_at.desc())
    ).first()
    return row.created_at if row else None


def resolve_remind_targets(
    session: Session,
    start_date: date,
    end_date: date,
    employee_ids: list[int] | None,
) -> tuple[list[Employee], list[dict[str, Any]]]:
    """算出提醒对象：显式指定 > 本周期未提交（已排除个人/部门免提交）。"""
    conditions = list(reportable_employee_conditions(session))
    if employee_ids:
        conditions.append(Employee.id.in_(employee_ids))
    else:
        submitted = submitted_employee_ids(session, start_date, end_date)
        conditions.append(Employee.id.notin_(submitted) if submitted else true())

    employees = session.scalars(
        select(Employee).where(*conditions).order_by(Employee.id)
    ).all()

    targets: list[Employee] = []
    skipped: list[dict[str, Any]] = []
    for emp in employees:
        if not emp.external_id:
            skipped.append(
                {"id": emp.id, "name": emp.name, "reason": "没有企微 userid，无法投递"}
            )
            continue
        targets.append(emp)
    return targets, skipped


def render_card_block(template: str, start_date: date, end_date: date) -> list[str]:
    """把一段文案模板渲染成卡片行；最后一行由调用方转成高亮行。

    占位符：`{period}`（2026-09-07 ~ 2026-09-13）/ `{start}` / `{end}`。
    未知占位符原样保留 —— 宁可让管理员在预览里看到 `{peroid}` 打错，也不要
    静默吃掉一段话，让人以为模板已生效。
    """
    period_text = f"{start_date.isoformat()} ~ {end_date.isoformat()}"
    rendered = (
        template.replace("{period}", period_text)
        .replace("{start}", start_date.isoformat())
        .replace("{end}", end_date.isoformat())
    )
    return [line.strip() for line in rendered.splitlines() if line.strip()]


# 企微 textcard 的 description 上限 512 字符（超出由企微自动截断，可能切在
# 高亮标签中间）。这里显式收口，宁可在结尾加省略号也不让上游切坏 HTML。
CARD_DESCRIPTION_LIMIT = 512
CARD_TITLE_LIMIT = 128


def build_remind_card(
    start_date: date,
    end_date: date,
    custom: str | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, str]:
    """拼企微文本卡片。

    默认中英双语：库里既有中文周报也有英文周报（实测约 22% 正文几乎不含汉字），
    只发中文等于对这批人发了一张看不懂的卡片。两种语言的文案都能在
    「系统设置 → 周报规则」里改，改完下一次发送即生效。

    卡片是**逐人**投递的，所以正文里刻意不出现其他人的姓名：写入「张三、李四
    等 8 人未提交」等于把催交名单发给每个收件人，谁被催了一览无余。
    """
    settings = settings or get_settings()
    period_text = f"{start_date.isoformat()} ~ {end_date.isoformat()}"
    if custom:
        lines = render_card_block(custom, start_date, end_date)
        description = "".join(
            f'<div class="normal">{escape(line, quote=False)}</div>'
            for line in lines
        )
    else:
        blocks = [
            render_card_block(settings.remind_card_body_zh, start_date, end_date),
            render_card_block(settings.remind_card_body_en, start_date, end_date),
        ]
        html: list[str] = []
        for lines in blocks:
            if not lines:
                continue
            for line in lines[:-1]:
                html.append(
                    f'<div class="normal">{escape(line, quote=False)}</div>'
                )
            html.append(
                f'<div class="highlight">{escape(lines[-1], quote=False)}</div>'
            )
        description = "".join(html) or f'<div class="highlight">{period_text}</div>'
    if len(description) > CARD_DESCRIPTION_LIMIT:
        # 只保留完整 div 片段，避免长度截断把 HTML 标签切成半截。
        shortened = description[: CARD_DESCRIPTION_LIMIT - 1]
        last_open = shortened.rfind("<div")
        last_close = shortened.rfind("</div>")
        if last_open > last_close:
            shortened = shortened[:last_open]
        description = shortened.rstrip() + "…"
    return {
        "title": (settings.remind_card_title or "周报提醒")[:CARD_TITLE_LIMIT],
        "description": description,
        "url": settings.remind_card_url,
        "btn_text": "去填写",
    }


def target_view(employee: Employee, last_sent) -> dict[str, Any]:
    return {
        "id": employee.id,
        "name": employee.name,
        "userid": employee.external_id,
        "department_name": None,
        "last_reminded_at": last_sent.isoformat() if last_sent else None,
    }


def _split_invalid(result: dict[str, Any]) -> set[str]:
    """企微返回的无效收件人（不进应用可见范围 / 不在通讯录 / 无权限）。

    官方文档：部分收件人无效时**整体仍算发送成功**，只在响应里列出无效的人。
    因此「errcode=0」不等于「所有人都收到了」—— 只按 errcode 记日志，就会出现
    「页面显示已提醒 20 人，实际只有 1 人收到」，而且事后无从核对是谁没收到。
    返回小写集合：企微把 userid 统一转小写返回。
    """
    raw = result.get("invaliduser") or ""
    if isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        items = str(raw).split("|")
    return {item.strip().lower() for item in items if item.strip()}


def send_reminders(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    employee_ids: list[int] | None = None,
    dry_run: bool = True,
    message: str | None = None,
    triggered_by: str = "api:manual",
) -> dict[str, Any]:
    """生成预览或真实发送，并把每次真实投递结果写入 ReminderLog。"""
    settings = runtime_settings(session)
    targets, skipped = resolve_remind_targets(
        session, start_date, end_date, employee_ids
    )

    if not dry_run and targets:
        cutoff = utcnow() - timedelta(hours=settings.remind_cooldown_hours)
        kept: list[Employee] = []
        for emp in targets:
            last = last_sent_at(session, emp.id, start_date, end_date)
            if last and last >= cutoff:
                hours = (utcnow() - last).total_seconds() / 3600
                skipped.append(
                    {
                        "id": emp.id,
                        "name": emp.name,
                        "reason": f"冷却期内已提醒（约 {hours:.0f} 小时前）",
                    }
                )
            else:
                kept.append(emp)
        targets = kept

    dept_names = department_name_map(session)
    preview: dict[str, Any] = {
        "dry_run": dry_run,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "target_count": len(targets),
        "targets": [
            {
                **target_view(emp, last_sent_at(session, emp.id, start_date, end_date)),
                "department_name": dept_names.get(emp.department_id),
            }
            for emp in targets
        ],
        "skipped": skipped,
        "card": build_remind_card(
            start_date, end_date, message, settings=settings
        ),
        "sent": False,
        "error": None,
        "triggered_by": triggered_by,
        # 显式结果，调用方不必靠「有没有 result 字段」反推：
        #   no_targets = 没有可发对象（全已提交 / 都在冷却期 / 全部免提交）
        #   failed     = 请求没被企微受理（网络 / 鉴权 / 权限），重试有意义
        #   sent       = 请求被受理；是否人人收到看 result.delivered_count
        "outcome": "no_targets",
    }

    if dry_run or not targets:
        return preview

    try:
        with WeComClient() as client:
            result = client.send_text_card(
                user_ids=[emp.external_id or "" for emp in targets],
                title=preview["card"]["title"],
                description=preview["card"]["description"],
                url=preview["card"]["url"],
                btn_text=preview["card"]["btn_text"],
            )
    except cb.ConnectorError as exc:
        preview["error"] = str(exc)
        preview["outcome"] = "failed"
        for emp in targets:
            session.add(
                ReminderLog(
                    employee_id=emp.id,
                    period_start=start_date,
                    period_end=end_date,
                    channel=ReminderChannel.WECOM_TEXT_CARD,
                    status=ReminderStatus.FAILED,
                    detail=f"{BATCH_FAILURE_DETAIL_PREFIX}{str(exc)[:500]}",
                    triggered_by=triggered_by,
                )
            )
        session.commit()
        return preview

    # 逐人核对投递结果：errcode=0 只代表请求被接受，invaliduser 里的才是真的没送到。
    invalid = _split_invalid(result)
    delivered: list[Employee] = []
    undelivered: list[dict[str, Any]] = []
    for emp in targets:
        userid = (emp.external_id or "").lower()
        if userid and userid in invalid:
            undelivered.append(
                {
                    "id": emp.id,
                    "name": emp.name,
                    "reason": "企微未投递（不在应用可见范围或通讯录中，请检查该应用的可见范围）",
                }
            )
            continue
        delivered.append(emp)

    # 请求已被企微受理。`sent` 表达的是「请求发出去了」，不等于人人都收到 ——
    # 是否人人收到看 result.delivered_count / undelivered，两个字段各说一件事。
    preview["sent"] = True
    preview["outcome"] = "sent"
    preview["result"] = {
        "errcode": result.get("errcode"),
        "errmsg": result.get("errmsg"),
        "invaliduser": result.get("invaliduser", ""),
        "delivered_count": len(delivered),
        "undelivered": undelivered,
    }
    if undelivered:
        # 请求成功但有人没收到：这是需要管理员处理的问题，不是成功提示。
        preview["error"] = (
            f"{len(undelivered)} 人未被企微投递（{('、'.join(item['name'] for item in undelivered[:5]))}"
            f"{' 等' if len(undelivered) > 5 else ''}），请检查自建应用的可见范围。"
        )
        preview["skipped"] = [
            *preview.get("skipped", []),
            *[
                {"id": item["id"], "name": item["name"], "reason": item["reason"]}
                for item in undelivered
            ],
        ]

    for emp in delivered:
        session.add(
            ReminderLog(
                employee_id=emp.id,
                period_start=start_date,
                period_end=end_date,
                channel=ReminderChannel.WECOM_TEXT_CARD,
                status=ReminderStatus.SENT,
                detail="ok",
                triggered_by=triggered_by,
            )
        )
    for item in undelivered:
        session.add(
            ReminderLog(
                employee_id=item["id"],
                period_start=start_date,
                period_end=end_date,
                channel=ReminderChannel.WECOM_TEXT_CARD,
                status=ReminderStatus.FAILED,
                detail=f"{RECIPIENT_UNDELIVERED_DETAIL_PREFIX}{item['reason']}",
                triggered_by=triggered_by,
            )
        )
    session.commit()
    return preview
