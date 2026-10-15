"""周报内容质量检测（大模型判定敷衍 / 内容过少）。

与重复检测是两个不同维度，互不替代，因此分开落 AIAnalysis（analysis_type 不同）：
- 重复检测：这篇**和本人历史**相比，是不是换汤不换药
- 质量检测：这篇**本身**有没有实质内容

批量体检时两者都跑，结论分别回写 WeeklyReport.ai_duplicate_flag / ai_quality_flag。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import get_provider
from app.ai.base import LLMError
from app.ai.json_util import extract_json_object
from app.ai.prompts import (
    QUALITY_PROMPT_VERSION,
    QUALITY_SYSTEM,
    QUALITY_USER_TEMPLATE,
)
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.ai_analysis import AIAnalysis
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus
from app.models.weekly_report import WeeklyReport
from app.services.runtime_config_service import runtime_settings

logger = get_logger(__name__)

QUALITY_ANALYSIS_TYPE = "quality_check"
ENTITY_TYPE = "weekly_report"


def _log_quality(
    session: Session,
    report_id: int,
    result: dict[str, Any],
    status: str,
    model_name: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    error: str | None = None,
) -> None:
    settings = runtime_settings(session)
    session.add(
        AIAnalysis(
            entity_type=ENTITY_TYPE,
            entity_id=report_id,
            analysis_type=QUALITY_ANALYSIS_TYPE,
            model_provider=settings.ai_provider,
            model_name=model_name,
            prompt_version=QUALITY_PROMPT_VERSION,
            result_json=result if error is None else {**result, "error": error},
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status=status,
            error_message=error,
            created_at=utcnow(),
        )
    )
    session.commit()


def check_report_quality(session: Session, report_id: int) -> dict[str, Any]:
    """判定单篇周报是否敷衍，结论落到 AIAnalysis 并返回。"""
    settings = runtime_settings(session)
    report = session.get(WeeklyReport, report_id)
    if report is None:
        raise ValueError(f"周报 {report_id} 不存在")
    from app.services.view_service import report_ai_exempt

    if report_ai_exempt(session, report):
        raise ValueError("免提交人员不参与 AI 分析")

    employee = session.get(Employee, report.employee_id) if report.employee_id else None
    body = (report.content or report.title or "").strip()

    if not body:
        # 空正文无需问模型 —— 「一片空白」本身就是最确定的敷衍，省一次调用
        result = {
            "report_id": report_id,
            "is_thin": True,
            "score": 0,
            "reason": "正文为空",
            "model": None,
            "prompt_version": QUALITY_PROMPT_VERSION,
        }
        _log_quality(session, report_id, result, AIAnalysisStatus.SUCCESS)
        return result

    period = (
        f"{report.start_date.isoformat()}~{report.end_date.isoformat()}"
        if report.start_date and report.end_date
        else "-"
    )
    budget = settings.ai_max_input_chars
    current_body = body if len(body) <= budget else body[:budget]

    user_prompt = QUALITY_USER_TEMPLATE.format(
        employee=employee.name if employee else "未知",
        period=period,
        current=current_body,
    )

    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    try:
        provider = get_provider(settings)
        llm_result = provider.chat(QUALITY_SYSTEM, user_prompt)
        model_name = llm_result.model or settings.ai_model
        prompt_tokens = llm_result.prompt_tokens
        completion_tokens = llm_result.completion_tokens
        parsed = extract_json_object(llm_result.content)
    except (LLMError, ValueError) as exc:
        logger.warning("质量检测失败（周报 %s）：%s", report_id, exc)
        _log_quality(
            session,
            report_id,
            {"report_id": report_id},
            AIAnalysisStatus.FAILED,
            model_name,
            prompt_tokens,
            completion_tokens,
            str(exc),
        )
        raise

    try:
        score = int(parsed.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    result = {
        "report_id": report_id,
        "is_thin": bool(parsed.get("is_thin", False)),
        "score": score,
        "reason": str(parsed.get("reason") or "")[:300],
        "model": model_name,
        "prompt_version": QUALITY_PROMPT_VERSION,
        "raw": json.dumps(parsed, ensure_ascii=False)[:1000],
    }
    _log_quality(
        session,
        report_id,
        result,
        AIAnalysisStatus.SUCCESS,
        model_name,
        prompt_tokens,
        completion_tokens,
    )
    return result


def latest_quality_flags(
    session: Session, report_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """批量取每篇周报最近一次质量检测结论（用于列表打标 / 详情展示）。"""
    if not report_ids:
        return {}
    rows = session.execute(
        select(AIAnalysis)
        .where(
            AIAnalysis.entity_type == ENTITY_TYPE,
            AIAnalysis.analysis_type == QUALITY_ANALYSIS_TYPE,
            AIAnalysis.entity_id.in_(report_ids),
        )
        .order_by(AIAnalysis.id.desc())
    ).scalars()

    flags: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.entity_id in flags:
            continue  # 已按 id desc，先出现的是最新的
        data = row.result_json or {}
        flags[row.entity_id] = {
            "is_thin": bool(data.get("is_thin", False)),
            "score": int(data.get("score") or 0),
            "reason": data.get("reason"),
            "checked_at": row.created_at.isoformat() if row.created_at else None,
        }
    return flags
