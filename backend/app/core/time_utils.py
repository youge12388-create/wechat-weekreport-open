"""时间工具。

两条来自企业微信官方约束的硬规则：

1. `oa/journal/get_record_list` 的 starttime / endtime 间隔 **不能超过一个月**；
2. 社区已确认企微汇报记录按年分表，**跨年区间查询会返回空列表**。

因此所有时间区间在进入 Connector 之前必须先切分为「不跨年、不跨月」的片段。
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

from app.core.config import get_settings

__all__ = [
    "utcnow",
    "to_naive_utc",
    "from_timestamp",
    "to_epoch_seconds",
    "business_date",
    "iso_week_range",
    "custom_week_range",
    "period_rule_label",
    "period_range",
    "split_time_range",
]


def utcnow() -> datetime:
    """当前 UTC 时间，naive（不含 tzinfo）。

    全库统一约定：所有 datetime 列存储 **UTC naive**，读取方按需转本地时区。
    这样 SQLite 与 PostgreSQL 行为一致，不会出现跨库时区漂移。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_epoch_seconds(value: datetime) -> int:
    """把「UTC naive」datetime 转成 Unix 时间戳（秒）。

    必须显式补 UTC 时区再取 timestamp()：对 naive 值直接调用 datetime.timestamp()
    会按**本机时区**解释，同一个时间窗在 UTC 服务器与东八区服务器上会差 8 小时。
    企业微信按绝对时间戳过滤，这个偏差会让窗口整体前移，导致周期末尾提交的周报
    永远抓不到（而部署在 UTC 服务器上时完全无症状，所以很难在测试里发现）。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def to_naive_utc(value: datetime) -> datetime:
    """把 aware datetime 转为 UTC naive；naive 输入原样返回（假定已是 UTC）。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def from_timestamp(ts: int | float | str | None) -> datetime | None:
    """企业微信返回的时间戳是 unix 秒。空值 / 非法值返回 None，不抛异常。"""
    if ts is None or ts == "":
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def iso_week_range(day: date) -> tuple[date, date]:
    """返回 day 所在自然周（周一 ~ 周日）的起止日期。"""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def business_date(value: datetime) -> date:
    """把 UTC naive 时刻换算成**业务时区**的日期。

    周期归属是按「天」判定的（例如周一批交算上周），而 report_time 是接口给的
    绝对时刻。直接用 UTC 日期，东八区 00:00~08:00 提交的汇报会被算到前一天，
    恰好在周一与月初这两个周期边界上判错。
    """
    offset = get_settings().report_timezone_offset_hours
    return (value + timedelta(hours=offset)).date()


def custom_week_range(day: date, start_weekday: int) -> tuple[date, date]:
    """返回以 start_weekday 为起点的自定义周（start_weekday: 0=周一 .. 6=周日）。

    例：start_weekday=2（周三）-> 本周三 ~ 下周二。
    """
    offset = (day.weekday() - start_weekday) % 7
    start = day - timedelta(days=offset)
    return start, start + timedelta(days=6)


def _month_range(day: date) -> tuple[date, date]:
    last_day = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last_day)


def period_rule_label() -> str:
    """当前生效的汇报周期规则的人类可读标签（用于前端透出，避免静默假设）。"""
    settings = get_settings()
    rule = settings.report_period_rule
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    if rule == "month":
        return "自然月"
    if rule == "custom_week":
        start = weekday_names[settings.report_custom_week_start % 7]
        end = weekday_names[(settings.report_custom_week_start + 6) % 7]
        return f"自定义周（{start}~{end}）"
    return "自然周（周一~周日）"


def _previous_month_range(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    previous_end = first - timedelta(days=1)
    return previous_end.replace(day=1), previous_end


def _shift_period_back(
    start: date, end: date, rule: str
) -> tuple[date, date]:
    """把周期整体往前挪一格（上一次交的那一份）。"""
    if rule == "month":
        return _previous_month_range(start)
    return start - timedelta(days=7), end - timedelta(days=7)


def period_range(
    report_time: datetime,
    report_type: str,
    *,
    attribution_days: int | None = None,
) -> tuple[date, date]:
    """由汇报时间推导汇报周期。

    企业微信汇报接口 **不提供** 周期字段，只有 report_time。
    因此 start_date / end_date 是推导值而非接口原值，且推导规则**必须可配置**
    （AGENTS.md 7.1：原 ISO 周假设待确认，禁止写死）：
      - daily   -> 当天
      - 其他    -> 由 report_period_rule 决定：
                    week        -> 自然周（周一~周日）
                    month       -> 自然月
                    custom_week -> 自定义起始星期的周

    另有一条**周期归属回溯**（`attribution_days`）：周期开始后的前 N 天内提交，
    归到上一个周期。周报通常周五到周日晚间交，但那是习惯不是截止；周一才交上来的
    那一篇写的是上周的工作，按提交日所在自然周归类会落进本周 —— 本周被凭空算成
    「已提交」，而本人本周确实还没交（周五的催交名单会漏掉他）。

    这是**归类口径**：周一交的那篇本来就属于上一周期。
    默认 N=1（周一归上一周期）；设 0 表示不做回溯，按提交当天算。
    """
    settings = get_settings()
    grace = (
        settings.report_period_attribution_days
        if attribution_days is None
        else attribution_days
    )
    day = business_date(report_time)
    if report_type == "daily":
        # 日报按天汇报，天然没有「跨天归属」的问题，回溯不适用。
        return day, day
    if report_type == "monthly":
        rule = "month"
    else:
        rule = settings.report_period_rule
    if rule == "month":
        start, end = _month_range(day)
    elif rule == "custom_week":
        start, end = custom_week_range(day, settings.report_custom_week_start)
    else:
        start, end = iso_week_range(day)
    if grace > 0 and (day - start).days < grace:
        return _shift_period_back(start, end, rule)
    return start, end


def split_time_range(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """把 [start, end] 切成若干「不跨年、不跨月」的片段（闭开区间语义由调用方决定）。

    返回 [(seg_start, seg_end), ...]，每段保证：
      - seg_start.year == seg_end_internal.year（不跨年）
      - 落在同一个自然月内
      - seg_start <= seg_end
    """
    if end < start:
        return []

    segments: list[tuple[datetime, datetime]] = []
    cursor = start

    while cursor <= end:
        # 当月最后一天的 23:59:59
        last_day = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = cursor.replace(
            day=last_day, hour=23, minute=59, second=59, microsecond=0
        )
        seg_end = min(month_end, end)
        segments.append((cursor, seg_end))

        # 进入下个月 1 号 00:00:00
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1, day=1)
        cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)

    return segments
