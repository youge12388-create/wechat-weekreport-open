"""验收项 7：按月分段、不跨年。"""

from __future__ import annotations

from datetime import date, datetime

from app.core.time_utils import (
    iso_week_range,
    period_range,
    split_time_range,
    utcnow,
)


def test_split_same_month() -> None:
    segments = split_time_range(datetime(2026, 9, 1), datetime(2026, 9, 30, 23, 59))
    assert len(segments) == 1
    assert segments[0][0] == datetime(2026, 9, 1)
    assert segments[0][1] == datetime(2026, 9, 30, 23, 59)


def test_split_across_months() -> None:
    segments = split_time_range(datetime(2026, 8, 20), datetime(2026, 10, 5))
    assert [s[0].strftime("%Y-%m") for s in segments] == ["2026-08", "2026-09", "2026-10"]
    # 每段都不得跨月
    for start, end in segments:
        assert (start.year, start.month) == (end.year, end.month)


def test_split_never_crosses_year_boundary() -> None:
    """企微按年分表，跨年区间查询返回空。分段结果必须按自然年切开。"""
    segments = split_time_range(datetime(2025, 11, 15), datetime(2026, 2, 10))
    assert segments
    for start, end in segments:
        assert start.year == end.year, f"分段跨年：{start} ~ {end}"
    assert segments[0][0] == datetime(2025, 11, 15)
    assert segments[-1][1].date() == date(2026, 2, 10)
    # 2025 段与 2026 段必须都存在
    years = {s[0].year for s in segments}
    assert years == {2025, 2026}


def test_split_each_segment_within_one_month_limit() -> None:
    """接口限制 starttime/endtime 间隔不超过一个月。"""
    segments = split_time_range(datetime(2026, 1, 1), datetime(2026, 12, 31))
    assert len(segments) == 12
    for start, end in segments:
        assert (end - start).days < 32


def test_split_invalid_range() -> None:
    assert split_time_range(datetime(2026, 9, 10), datetime(2026, 9, 1)) == []


def test_iso_week_range() -> None:
    # 2026-09-08 是周二
    assert iso_week_range(date(2026, 9, 8)) == (date(2026, 9, 7), date(2026, 9, 13))


def test_period_range_by_report_type() -> None:
    moment = datetime(2026, 9, 8, 10, 0)
    assert period_range(moment, "weekly") == (date(2026, 9, 7), date(2026, 9, 13))
    assert period_range(moment, "daily") == (date(2026, 9, 8), date(2026, 9, 8))
    assert period_range(moment, "monthly") == (date(2026, 9, 1), date(2026, 9, 30))
    assert period_range(moment, "other") == (date(2026, 9, 7), date(2026, 9, 13))
    # 报表明细之外：周期开始当天（周一）提交的周报归上一周，见下面的归类用例


def test_weekly_report_submitted_on_monday_counts_as_previous_week() -> None:
    """周一交的那篇 = 上一周的周报。

    没有这条规则时，周一才交上来的那篇会落进**本周**：本周被点亮成「已提交」，
    而周五催交名单里也不会再有这个人 —— 他本周确实还没交。
    """
    # 2026-09-07 是周一，09-13 是周日；上一周是 08-31 ~ 09-06
    monday = datetime(2026, 9, 7, 3, 0)  # UTC 03:00 = 北京 11:00
    assert period_range(monday, "weekly") == (date(2026, 8, 31), date(2026, 9, 6))


def test_monday_reports_are_classified_into_previous_period() -> None:
    """回溯按**业务日期**判定：周一 23:00（北京）也算上一周。

    直接用 UTC 日期会把东八区周一的 00:00~08:00 算成周日 —— 那已经是上一周了，
    于是同一天提交的周报会因为钟点不同而归到两个周期。
    """
    monday_night = datetime(2026, 9, 7, 15, 0)  # UTC 15:00 = 北京 23:00，仍是周一
    assert period_range(monday_night, "weekly") == (date(2026, 8, 31), date(2026, 9, 6))
    # 周二（09-08）提交的不在回溯范围内，回落到本周
    tuesday = datetime(2026, 9, 8, 3, 0)
    assert period_range(tuesday, "weekly") == (date(2026, 9, 7), date(2026, 9, 13))


def test_period_attribution_can_be_disabled() -> None:
    monday = datetime(2026, 9, 7, 3, 0)
    assert period_range(monday, "weekly", attribution_days=0) == (
        date(2026, 9, 7),
        date(2026, 9, 13),
    )


def test_period_attribution_applies_to_monthly_reports() -> None:
    """月报同理：月初第 1 天提交的算上一个月。"""
    first_day = datetime(2026, 10, 1, 3, 0)
    assert period_range(first_day, "monthly") == (date(2026, 9, 1), date(2026, 9, 30))
    assert period_range(first_day, "monthly", attribution_days=0) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )


def test_daily_reports_are_not_affected_by_attribution() -> None:
    """日报按天汇报，不存在跨天归属的问题。"""
    monday = datetime(2026, 9, 7, 3, 0)
    assert period_range(monday, "daily") == (date(2026, 9, 7), date(2026, 9, 7))


def test_utcnow_is_naive() -> None:
    assert utcnow().tzinfo is None
