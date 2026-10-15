"""用已缓存的模型分析结果重放本地投影（零模型调用、幂等）。

用途：投影逻辑升级（如卡点空话过滤、任务键规则调整）后，把存量
Task / Blocker / ReportItem 与新口径对齐。只重写派生行，不改周报正文，
不调用模型、不产生任何费用；重复执行结果一致。

与 ``backfill_analysis.py`` 的区别：那个脚本会真实重跑模型（计费）；
本脚本只消费 ``ai_analyses`` 里已成功的 ``report_analysis`` 结果_json，
在本地重新执行 ``materialize_report_analysis``。

用法（在项目根目录）：
    python scripts/replay_report_projection.py --dry-run   # 只看会重放多少篇
    python scripts/replay_report_projection.py             # 执行重放
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import func, select  # noqa: E402

from app.db.session import get_session_factory  # noqa: E402
from app.models.ai_analysis import AIAnalysis  # noqa: E402
from app.models.enums import AIAnalysisStatus  # noqa: E402
from app.models.weekly_report import WeeklyReport  # noqa: E402
from app.services.structured_data_service import (  # noqa: E402
    materialize_report_analysis,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="只统计会重放的篇数，不写库"
    )
    args = parser.parse_args()

    session = get_session_factory()()
    total = session.scalar(
        select(func.count())
        .select_from(AIAnalysis)
        .where(
            AIAnalysis.entity_type == "weekly_report",
            AIAnalysis.analysis_type == "report_analysis",
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
        )
    )
    print(f"共有 {total} 篇已成功的结构化分析缓存")
    if args.dry_run:
        session.close()
        return 0

    rows = session.execute(
        select(AIAnalysis, WeeklyReport)
        .join(WeeklyReport, WeeklyReport.id == AIAnalysis.entity_id)
        .where(
            AIAnalysis.entity_type == "weekly_report",
            AIAnalysis.analysis_type == "report_analysis",
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
        )
        .order_by(WeeklyReport.id)
    ).all()
    done = 0
    for analysis, report in rows:
        materialize_report_analysis(
            session, report, analysis.result_json or {}, analysis.prompt_version
        )
        done += 1
        if done % 100 == 0:
            print(f"  已重放 {done}/{len(rows)} 篇")
    session.commit()
    print(f"重放完成：{done} 篇投影已按当前代码口径重建")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
