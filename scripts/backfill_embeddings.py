"""语义向量回填脚本：为周报生成/更新 embedding。

用法（在项目根目录）：

    $PY scripts/backfill_embeddings.py            # 只补缺失与源已变更的（默认，dry-run 看统计）
    $PY scripts/backfill_embeddings.py --apply    # 真正写入
    $PY scripts/backfill_embeddings.py --all --apply

说明：
- 未配置 Embedding 服务（ai_embedding_base_url/api_key/model + 出站白名单）时直接退出。
- 首次全量回填受 BACKFILL_MAX_REPORTS 保险丝限制，分多次执行即可。
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.ai.embedding import get_embedding_client
from app.db.session import get_session_factory
from app.models.weekly_report import WeeklyReport
from app.services.embedding_service import ensure_report_embeddings


def main() -> int:
    parser = argparse.ArgumentParser(description="回填周报语义向量")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认只统计）")
    parser.add_argument(
        "--all", action="store_true", help="处理全部周报（默认仅近一年）"
    )
    args = parser.parse_args()

    session_factory = get_session_factory()
    session = session_factory()
    try:
        from app.services.runtime_config_service import runtime_settings

        client = get_embedding_client(runtime_settings(session))
        if not client.configured():
            print("语义检索未启用：" + client.unavailable_reason())
            return 1

        stmt = select(WeeklyReport).order_by(WeeklyReport.submitted_at.desc())
        if not args.all:
            from datetime import timedelta

            from app.core.time_utils import utcnow

            cutoff = (utcnow() - timedelta(days=365)).date()
            stmt = stmt.where(WeeklyReport.start_date >= cutoff)
        reports = list(session.scalars(stmt))
        print(f"候选 {len(reports)} 篇（apply={args.apply}）")
        if not args.apply:
            from app.services.embedding_service import BACKFILL_MAX_REPORTS, embedding_source_hash

            from app.models.ai_embedding import AIEmbedding

            existing = {
                int(report_id): str(source_hash)
                for report_id, source_hash in session.execute(
                    select(AIEmbedding.report_id, AIEmbedding.source_hash)
                ).all()
            }
            missing = sum(
                1
                for report in reports
                if existing.get(report.id) != embedding_source_hash(report)
            )
            print(
                f"dry-run：待回填 {missing} 篇（单次上限 {BACKFILL_MAX_REPORTS}）。"
                "加 --apply 执行。"
            )
            return 0
        result = ensure_report_embeddings(session, reports)
        print(result)
        return 0 if result.get("enabled") else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
