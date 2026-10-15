"""批量 AI 体检：把「每篇都要人点一次按钮」变成「一键扫完」。

三条硬约束，每一条都是从「省钱 / 可控」出发：
1. **幂等**：默认只扫没体检过的（ai_checked_at IS NULL）。重复点按钮不会重复烧同一份钱。
2. **限量**：单次最多扫 ai_scan_max_reports 篇，并明确告诉调用方还剩多少篇没扫，
   避免有人在一个月的数据上手滑点一下就把预算打穿。
3. **容错**：单篇失败只记录不中断 —— 一篇周报让模型抽风，不该让整批体检结果为零。

关于失败重试的说明：某篇只要有一项检测失败，就不会更新 ai_checked_at，
下次扫描会整篇重来（而不是只补跑失败项）。原因是不引入额外的半完成状态标记，
换取逻辑简单可靠 —— 失败本属少数派，重跑一次的成本远低于维护一套断点续跑状态机。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.base import LLMError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.employee import Employee
from app.models.weekly_report import WeeklyReport
from app.services.duplicate_service import check_report_duplicate
from app.services.quality_service import check_report_quality
from app.services.view_service import (
    ai_report_condition,
    report_period_overlap_conditions,
    resolve_period,
)

logger = get_logger(__name__)


def scan_reports(
    session: Session,
    start: str | None = None,
    end: str | None = None,
    department_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """对周期内周报做批量体检，返回统计结果。

    force=True 会连已体检过的重跑一遍（用于更换模型或升级 prompt 后刷新结论）。
    """
    settings = get_settings()
    start_date, end_date = resolve_period(start, end)

    conditions = [*report_period_overlap_conditions(start_date, end_date), ai_report_condition(session)]
    if department_id is not None:
        conditions.append(Employee.department_id == department_id)
    if not force:
        conditions.append(WeeklyReport.ai_checked_at.is_(None))

    pending = list(
        session.scalars(
            select(WeeklyReport)
            .join(Employee, WeeklyReport.employee_id == Employee.id, isouter=True)
            .where(*conditions)
            .order_by(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc())
        )
    )
    batch = pending[: settings.ai_scan_max_reports]

    scanned = 0
    duplicate_hits = 0
    quality_hits = 0
    failed: list[dict[str, Any]] = []

    for report in batch:
        try:
            duplicate = check_report_duplicate(session, report.id)
            quality = check_report_quality(session, report.id)
        except (LLMError, ValueError) as exc:
            # 单篇抽风记下来继续下一篇，整批结果不能因此归零
            logger.warning("批量体检：周报 %s 失败，跳过继续：%s", report.id, exc)
            failed.append({"report_id": report.id, "error": str(exc)})
            continue

        report.ai_checked_at = utcnow()
        report.ai_duplicate_flag = bool(duplicate.get("is_duplicate"))
        report.ai_duplicate_score = int(duplicate.get("score") or 0)
        report.ai_quality_flag = bool(quality.get("is_thin"))
        session.commit()

        scanned += 1
        if report.ai_duplicate_flag:
            duplicate_hits += 1
        if report.ai_quality_flag:
            quality_hits += 1

    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "scanned": scanned,
        "duplicate_hits": duplicate_hits,
        "quality_hits": quality_hits,
        "failed_count": len(failed),
        "failed": failed[:20],
        "remaining": max(len(pending) - len(batch), 0),
        "force": force,
    }
