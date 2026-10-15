#!/usr/bin/env python
"""周报历史边界诊断：企微五月（或某月）之前的汇报记录到底存不存在。

背景：线上全量抓取（mode=all_history）从 1970 年起按月扫描，但库里最早
只到某个时间点（如 2026-05-25）。原因只可能是：

  A. 五月前的汇报挂在**其他 template_id** 下，被 ``WECOM_REPORT_TEMPLATE_IDS``
     过滤条件拦住（模板改版/重建会生成新 ID）；
  B. 企微侧对该自建应用可见的记录本来就只有这么多（公司五月才开始用汇报，
     或「汇报-可调用接口的应用」可见范围受限）。

本脚本逐月调用列表接口（get_record_list），分别统计：
  - 按当前配置的 template_id 过滤能拿到的条数（同步链路实际使用的口径）
  - 不带任何过滤能拿到的条数（企微侧真实可见的口径）

只在两者出现差异时，额外取一条详情，打印它实际归属的 template_id /
template_name，直接给出「旧模板 ID 是多少、该补进配置」的答案。

只读诊断：不写数据库、不触发同步、不修改任何业务数据。
列表接口按客户端内置节流限速；详情最多取 3 条。

线上运行方式（宝塔服务器，生产环境变量在 /etc/zhoubao/zhoubao.env）：

    cd /opt/zhoubao/current/scripts   # 若发布包未包含 scripts，则先上传本文件
    set -a; . /etc/zhoubao/zhoubao.env; set +a
    /opt/zhoubao/shared/venv/bin/python diagnose_wecom_history.py \
        --start 2025-01-01

判读：
  - 早期月份「不过滤 > 0 且 过滤 = 0」→ 原因 A：把打印出的旧 template_id
    追加进 WECOM_REPORT_TEMPLATE_IDS，再点一次「同步全部历史周报」。
  - 早期月份两列都是 0 → 原因 B：企微侧无数据，抓取端无解，
    去管理后台「汇报」核对最早记录时间。
  - 早期月份「过滤 > 0」但库里没有 → 同步环节有问题，拿 jobs 统计进一步排查。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.connectors.wecom.client import (  # noqa: E402
    JOURNAL_LIST_PATH,
    WeComClient,
)
from app.core.config import get_settings  # noqa: E402
from app.core.time_utils import to_epoch_seconds  # noqa: E402


def month_segments(start: date, end: date) -> list[tuple[date, date]]:
    """按自然月列出 [start, end] 内的月份片段（含首尾）。"""
    segments: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        segments.append((max(cursor, start), min(next_month - timedelta(days=1), end)))
        cursor = next_month
    return segments


def iter_ids_without_filter(
    client: WeComClient, seg_start: datetime, seg_end: datetime
) -> Iterator[str]:
    """不过滤口径：直接调列表接口，不带 filters。

    复刻 client.iter_report_ids 的翻页逻辑（cursor/endflag），仅去掉
    template 过滤，供诊断使用；沿用客户端内置的列表节流。
    """
    cursor = 0
    endflag = 0
    while not endflag:
        client._throttle_journal_list()
        body = {
            "starttime": to_epoch_seconds(seg_start),
            "endtime": to_epoch_seconds(seg_end),
            "cursor": cursor,
            "limit": 100,
        }
        data = client._raw_request("POST", JOURNAL_LIST_PATH, json_body=body)
        yield from data.get("journaluuid_list") or []
        endflag = int(data.get("endflag") or 0)
        next_cursor = int(data.get("next_cursor") or 0)
        if next_cursor <= cursor:
            break
        cursor = next_cursor


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断企微汇报历史边界（只读，不写库）")
    parser.add_argument(
        "--start", default="2025-01-01", help="起始日期（业务时区，默认 2025-01-01）"
    )
    parser.add_argument("--end", default=None, help="结束日期（业务时区，默认今天）")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.wecom_corp_id or not settings.wecom_corp_secret:
        print("未配置企业微信凭据（WECOM_CORP_ID / WECOM_CORP_SECRET）。", file=sys.stderr)
        return 2

    offset = settings.report_timezone_offset_hours
    start_day = date.fromisoformat(args.start)
    end_day = date.fromisoformat(args.end) if args.end else datetime.now().date()

    configured = settings.wecom_report_template_ids
    print(f"业务时区偏移：UTC{'+' if offset >= 0 else ''}{offset}")
    print(
        "当前配置的 template_id 过滤："
        + (", ".join(configured) if configured else "（空 = 不过滤）")
    )
    print(f"扫描范围：{start_day} ~ {end_day}（按月分段）")
    print("-" * 62)
    print(f"{'月份':<10}{'按模板过滤':>14}{'不过滤':>10}")
    print("-" * 62)

    blocked_months: list[tuple[str, str, int]] = []
    with WeComClient() as client:
        for seg_start_day, seg_end_day in month_segments(start_day, end_day):
            seg_start = datetime.combine(seg_start_day, time.min) - timedelta(
                hours=offset
            )
            seg_end = datetime.combine(seg_end_day, time.max) - timedelta(
                hours=offset
            )
            label = f"{seg_start_day:%Y-%m}"

            with_filter = sum(1 for _ in client.iter_report_ids(seg_start, seg_end))
            no_filter = sum(1 for _ in iter_ids_without_filter(client, seg_start, seg_end))
            print(f"{label:<10}{with_filter:>16}{no_filter:>12}")

            if no_filter > 0 and with_filter == 0:
                sample_id = next(
                    iter_ids_without_filter(client, seg_start, seg_end), None
                )
                if sample_id:
                    blocked_months.append((label, sample_id, no_filter))

    print("-" * 62)

    if not blocked_months:
        print(
            "结论：早期月份「不过滤」条数均为 0（或与「过滤」一致），\n"
            "说明企微侧对该应用可见的汇报记录最早就是目前库里这个时间点，\n"
            "抓取端无数据可补。请到企微管理后台「汇报」核对最早记录时间。"
        )
        return 0

    print("发现被 template_id 过滤挡掉的月份，取样详情：")
    with WeComClient() as client:
        for label, sample_id, count in blocked_months[:3]:
            info = (client.get_report_detail(sample_id).payload.get("info") or {})
            print(
                f"  {label}（被挡 {count} 条）样本 {sample_id}：\n"
                f"    template_id   = {info.get('template_id')}\n"
                f"    template_name = {info.get('template_name')}"
            )
    print(
        "\n结论：原因是模板过滤。把上面打印的旧 template_id 追加进\n"
        "WECOM_REPORT_TEMPLATE_IDS（多个用英文逗号分隔），改完配置重启\n"
        "API/Worker，再执行一次「同步全部历史周报」即可补齐。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
