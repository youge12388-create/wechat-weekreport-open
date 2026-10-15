"""周报重复检测（大模型判定）。

为什么用模型而不是字符串相似度：复制粘贴的人会改几个字、调语序，
纯文本相似度很容易被绕过；而「是否敷衍重复」本质是个语义判断。
代价是每次判定要花钱，所以只与同一员工**最近 N 篇**比对（ai_duplicate_lookback）。
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
    DUPLICATE_PROMPT_VERSION,
    DUPLICATE_SYSTEM,
    DUPLICATE_USER_TEMPLATE,
)
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.ai_analysis import AIAnalysis
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus
from app.models.weekly_report import WeeklyReport
from app.services.report_text import report_body
from app.services.runtime_config_service import runtime_settings

logger = get_logger(__name__)

DUPLICATE_ANALYSIS_TYPE = "duplicate_check"
ENTITY_TYPE = "weekly_report"


def _history_text(reports: list[WeeklyReport]) -> str:
    chunks = []
    for report in reports:
        period = (
            f"{report.start_date.isoformat()}~{report.end_date.isoformat()}"
            if report.start_date and report.end_date
            else "-"
        )
        # 必须与 current 分支同口径（report_body，优先清洗后的 content、
        # 为空才现场剥离 content_raw）。此前这里退化为 `content or title`，
        # 有两个后果：① content 为空时丢掉 raw 里可恢复的正文；
        # ② 退到标题——同模板周报标题常常一样（如「周报汇报」），
        #    拿标题当正文喂给模型会判出**假重复**。
        body = report_body(report)
        chunks.append(f"---\n周报ID: {report.id}\n周期: {period}\n正文:\n{body}\n")
    return "\n".join(chunks)


def check_report_duplicate(session: Session, report_id: int) -> dict[str, Any]:
    """判定某篇周报是否与其历史周报重复，并落库留痕。"""
    settings = runtime_settings(session)
    report = session.get(WeeklyReport, report_id)
    if report is None:
        raise ValueError(f"周报 {report_id} 不存在")
    from app.services.view_service import report_ai_exempt

    if report_ai_exempt(session, report):
        raise ValueError("免提交人员不参与 AI 分析")

    employee = session.get(Employee, report.employee_id) if report.employee_id else None
    current_period = (
        f"{report.start_date.isoformat()}~{report.end_date.isoformat()}"
        if report.start_date and report.end_date
        else "-"
    )

    history = (
        list(
            session.scalars(
                select(WeeklyReport)
                .where(
                    WeeklyReport.employee_id == report.employee_id,
                    WeeklyReport.id != report.id,
                )
                .order_by(WeeklyReport.submitted_at.desc(), WeeklyReport.id.desc())
                .limit(settings.ai_duplicate_lookback)
            )
        )
        if report.employee_id
        else []
    )

    base: dict[str, Any] = {
        "report_id": report_id,
        "compared_with": [r.id for r in history],
    }

    if not history:
        result = {
            **base,
            "is_duplicate": False,
            "score": 0,
            "reason": "无历史周报可比对",
            "model": None,
            "prompt_version": DUPLICATE_PROMPT_VERSION,
        }
        session.add(
            AIAnalysis(
                entity_type=ENTITY_TYPE,
                entity_id=report_id,
                analysis_type=DUPLICATE_ANALYSIS_TYPE,
                model_provider=settings.ai_provider,
                model_name=None,
                prompt_version=DUPLICATE_PROMPT_VERSION,
                result_json=result,
                # 无历史可比对 = 没调用模型 = 没有用量开销
                prompt_tokens=None,
                completion_tokens=None,
                status=AIAnalysisStatus.SUCCESS,
                created_at=utcnow(),
            )
        )
        session.commit()
        return result

    budget = settings.ai_max_input_chars
    # 与单篇分析同一口径：正文本身也要受预算约束，否则超长周报会把请求撑爆
    # （报错后按重试次数反复计费）。截断如实标注，不静默。
    current_body = report_body(report)
    if len(current_body) > budget:
        current_body = current_body[:budget] + "\n\n（本篇正文过长，已按单篇预算截断）"
    history_body = _history_text(history)
    remaining = max(0, budget - len(current_body))
    if len(history_body) > remaining:
        history_body = history_body[:remaining]

    user_prompt = DUPLICATE_USER_TEMPLATE.format(
        employee=employee.name if employee else "未知",
        period=current_period,
        current=current_body,
        history=history_body,
    )

    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    try:
        provider = get_provider(settings)
        llm_result = provider.chat(DUPLICATE_SYSTEM, user_prompt)
        model_name = llm_result.model or settings.ai_model
        prompt_tokens = llm_result.prompt_tokens
        completion_tokens = llm_result.completion_tokens
        parsed = extract_json_object(llm_result.content)
    except (LLMError, ValueError) as exc:
        logger.warning("重复检测失败（周报 %s）：%s", report_id, exc)
        session.add(
            AIAnalysis(
                entity_type=ENTITY_TYPE,
                entity_id=report_id,
                analysis_type=DUPLICATE_ANALYSIS_TYPE,
                model_provider=settings.ai_provider,
                model_name=model_name,
                prompt_version=DUPLICATE_PROMPT_VERSION,
                result_json={**base, "error": str(exc)},
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                status=AIAnalysisStatus.FAILED,
                error_message=str(exc),
                created_at=utcnow(),
            )
        )
        session.commit()
        raise

    try:
        score = int(parsed.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    result = {
        **base,
        "is_duplicate": bool(parsed.get("is_duplicate", False)),
        "score": score,
        "reason": str(parsed.get("reason") or "")[:300],
        "model": model_name,
        "prompt_version": DUPLICATE_PROMPT_VERSION,
        "raw": json.dumps(parsed, ensure_ascii=False)[:1000],
    }
    session.add(
        AIAnalysis(
            entity_type=ENTITY_TYPE,
            entity_id=report_id,
            analysis_type=DUPLICATE_ANALYSIS_TYPE,
            model_provider=settings.ai_provider,
            model_name=model_name,
            prompt_version=DUPLICATE_PROMPT_VERSION,
            result_json=result,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status=AIAnalysisStatus.SUCCESS,
            created_at=utcnow(),
        )
    )
    session.commit()
    return result


def latest_duplicate_flags(
    session: Session, report_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """批量取每篇周报最近一次重复检测结论（用于列表打标）。"""
    if not report_ids:
        return {}
    rows = session.execute(
        select(AIAnalysis)
        .where(
            AIAnalysis.entity_type == ENTITY_TYPE,
            AIAnalysis.analysis_type == DUPLICATE_ANALYSIS_TYPE,
            AIAnalysis.entity_id.in_(report_ids),
        )
        .order_by(AIAnalysis.id.desc())
    ).scalars()

    flags: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.entity_id in flags:
            continue  # 已按 id desc，先出现的就是最新的
        data = row.result_json or {}
        flags[row.entity_id] = {
            "is_duplicate": bool(data.get("is_duplicate", False)),
            "score": int(data.get("score") or 0),
            "reason": data.get("reason"),
            "checked_at": row.created_at.isoformat() if row.created_at else None,
        }
    return flags
