#!/usr/bin/env python
"""手动同步企业微信数据（需求第 6.4 节：能手动重新同步）。

运行：
    python scripts/sync_wecom.py --target all
    python scripts/sync_wecom.py --target departments
    python scripts/sync_wecom.py --target reports --days 30
    python scripts/sync_wecom.py --target reports --start 2026-08-01 --end 2026-08-31

注意：周报同步依赖员工与部门已入库（用于关联 employee_id），
     首次执行请直接用 --all。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.connectors.base import ConnectorError, ConnectorPermissionError  # noqa: E402
from app.connectors.wecom.client import WeComClient  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.core.time_utils import utcnow  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.services.department_sync import sync_departments  # noqa: E402
from app.services.employee_sync import sync_employees  # noqa: E402
from app.services.weekly_report_sync import sync_weekly_reports  # noqa: E402

TARGETS = ("departments", "employees", "reports")


def ensure_schema() -> None:
    """确保数据库结构为最新。"""
    # 脚本可从任意目录运行，script_location 必须绝对化
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")


def main() -> int:
    parser = argparse.ArgumentParser(description="同步企业微信数据")
    parser.add_argument(
        "--target",
        choices=[*TARGETS, "all"],
        default="all",
        help="同步目标，默认 all",
    )
    parser.add_argument("--days", type=int, default=None, help="周报回溯天数")
    parser.add_argument("--start", type=str, default=None, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--skip-migrate", action="store_true", help="跳过 migration")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    if not args.skip_migrate:
        ensure_schema()

    targets = TARGETS if args.target == "all" else (args.target,)

    start = (
        datetime.strptime(args.start, "%Y-%m-%d") if args.start else None
    )
    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else None
    if args.days and start is None:
        end = end or utcnow()
        start = end - timedelta(days=args.days)

    results = {}
    try:
        with WeComClient() as client, session_scope() as session:
            if "departments" in targets:
                results["departments"] = sync_departments(
                    session, client, triggered_by="cli"
                ).as_dict()
            if "employees" in targets:
                results["employees"] = sync_employees(
                    session, client, triggered_by="cli"
                ).as_dict()
            if "reports" in targets:
                results["reports"] = sync_weekly_reports(
                    session, start, end, client, triggered_by="cli"
                ).as_dict()
    except ConnectorError as exc:
        print(f"\n同步失败：{exc}", file=sys.stderr)
        if isinstance(exc, ConnectorPermissionError):
            print(
                "请确认自建应用已加入管理后台「汇报 - 可调用接口的应用」，"
                "且已配置可信 IP。",
                file=sys.stderr,
            )
        print("可先运行：python scripts/probe_wecom.py 定位问题。", file=sys.stderr)
        return 1

    print(json.dumps(results, ensure_ascii=False, indent=2))

    failed = sum(r["failed_count"] for r in results.values())
    for name, r in results.items():
        if r["status"] != "success":
            print(f"\n[!] {name} 状态：{r['status']}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
