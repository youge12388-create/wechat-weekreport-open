"""历史周报修复 CLI。

用法（务必先 dry-run，确认清单后再 --apply）：
    python scripts/repair_reports.py
    python scripts/repair_reports.py --apply
    python scripts/repair_reports.py --compact   # 只要汇总数量

修复四档，全部只改字段、不删除记录、不覆盖 RawRecord.raw_payload、不调用模型：
  1. employee_links  补 WeeklyReport.employee_id（缺失时按提交人建 MISSING 占位员工）
  2. classifications 按同 template_id 的已知样本纠正 report_type / template_name
  3. periods         按当前周期规则重算 start_date / end_date（含「周一交的归上一周」）
  4. languages       按清洗后正文重判 content_language（历史数据被富文本 HTML 标记误判成英文）
"""

from __future__ import annotations

import argparse
import json
import sys

from app.db.session import session_scope
from app.services.report_repair_service import repair_reports


def main() -> None:
    parser = argparse.ArgumentParser(description="修复历史周报关联与分类")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写入修复结果；缺省只 dry-run 输出清单",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="只输出汇总数量，不输出逐条变更清单",
    )
    args = parser.parse_args()

    with session_scope() as session:
        result = repair_reports(session, dry_run=not args.apply)

    if args.compact:
        # 四档必须在场：漏掉任何一档，都会让跑的人以为「这一类没什么要修的」。
        result = {
            "dry_run": result["dry_run"],
            "employee_links": {
                key: result["employee_links"][key]
                for key in ("checked", "changed", "unresolved")
            },
            "classifications": {
                key: result["classifications"][key]
                for key in ("checked", "changed", "unresolved")
            },
            "periods": {
                key: result["periods"][key] for key in ("checked", "changed")
            },
            "languages": {
                key: result["languages"][key] for key in ("checked", "changed")
            },
        }

    payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    print(payload)


if __name__ == "__main__":
    main()
