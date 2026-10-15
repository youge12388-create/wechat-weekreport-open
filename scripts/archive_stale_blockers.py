"""存量卡点一次性迁移到生命周期状态机（负责人已确认口径：只保留近三周有证据的）。

用法（务必先 dry-run，确认汇总后再 --apply）：
    python scripts/archive_stale_blockers.py
    python scripts/archive_stale_blockers.py --apply
    python scripts/archive_stale_blockers.py --apply --sample 20

规则与运行态完全一致（不引入第二套口径）：
  - 对全部 Blocker 按其周报证据重跑状态推导（structured_data_service.recompute
    同一路径）：
      * 近三周（blocker_silence_archive_weeks，默认 3）无新证据 → ARCHIVED；
      * 近三周有证据 → 按「高风险 / 两个周期以上 / 跨员工」升级 OPEN，否则 CANDIDATE；
      * task 已 COMPLETED → RESOLVED（保持既有语义）；
      * 手动 RESOLVED 不被静默覆盖，仅高风险新证据重开。
  - 幂等：重复执行结果一致。

本脚本不删除任何记录，只更新 blockers.status；生产执行前需负责人单独确认。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from sqlalchemy import select

from app.db.session import session_scope
from app.models.blocker import Blocker
from app.services.structured_data_service import recompute_blocker_status


def main() -> None:
    parser = argparse.ArgumentParser(description="存量卡点迁移到生命周期状态机")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写入新状态；缺省只 dry-run 输出清单",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=15,
        help="dry-run 输出里逐条列出的样例条数（默认 15）",
    )
    args = parser.parse_args()

    changes: list[dict[str, object]] = []
    unchanged = 0
    with session_scope() as session:
        blocker_ids = list(session.scalars(select(Blocker.id).order_by(Blocker.id)))
        for blocker_id in blocker_ids:
            blocker = session.get(Blocker, blocker_id)
            if blocker is None:  # 重算链路可能删除无证据卡点
                continue
            before = blocker.status
            recompute_blocker_status(session, blocker_id, ignore_manual=False)
            session.flush()
            after = blocker.status
            if after != before:
                changes.append(
                    {
                        "id": blocker.id,
                        "title": blocker.title,
                        "risk_level": blocker.risk_level,
                        "last_seen_date": str(blocker.last_seen_date),
                        "from": before,
                        "to": after,
                    }
                )
            else:
                unchanged += 1
        if not args.apply:
            session.rollback()

    summary = {
        "dry_run": not args.apply,
        "checked": len(changes) + unchanged,
        "changed": len(changes),
        "unchanged": unchanged,
        "to_status": dict(Counter(str(item["to"]) for item in changes)),
        "sample": changes[: args.sample],
    }

    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    print(payload)
    if not args.apply:
        print(
            "\n以上为 dry-run 预览，未写入任何数据。确认后加 --apply 执行。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
