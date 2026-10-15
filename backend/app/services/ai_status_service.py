"""AI 能力可用性自检。

单独抽成服务而不是留在路由里：系统设置页与消息中心都要读取同一份结论。
「有没有配 Key」和「上次调用是不是失败」必须只有一处判断，
否则两个页面各说一套，排查时反而更糊涂。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ai_analysis import AIAnalysis
from app.models.enums import AIAnalysisStatus
from app.services.runtime_config_service import runtime_settings


def ai_capability_status(session: Session) -> dict[str, Any]:
    """返回配置状态 + 最近一次调用结果，供页面渲染引导或失败原因。"""
    settings = runtime_settings(session)
    configured = settings.ai_provider == "fake" or bool(settings.ai_api_key)
    last = session.scalars(
        select(AIAnalysis).order_by(AIAnalysis.id.desc()).limit(1)
    ).first()
    return {
        "provider": settings.ai_provider,
        "model": settings.ai_model,
        "configured": configured,
        "hint": (
            None
            if configured
            else "未配置 DeepSeek API Key。请前往系统设置录入密钥，或把 AI_PROVIDER 设为 fake。"
        ),
        "last_call_at": last.created_at.isoformat()
        if last and last.created_at
        else None,
        "last_status": last.status if last else None,
        "last_error": last.error_message if last else None,
    }


def ai_last_call_failed(status: dict[str, Any]) -> bool:
    """最近一次模型调用是否失败。

    只看最后一篇：之前失败过但随后成功（余额充上了、Key 换对了）不该继续报红。
    """
    return status.get("last_status") == AIAnalysisStatus.FAILED
