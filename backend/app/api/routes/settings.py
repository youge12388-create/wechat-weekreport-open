"""管理员 AI 设置接口。

由网关层鉴权保护；只返回脱敏状态，密钥只能通过写入接口替换，不能读取。
"""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.ai import get_provider
from app.api.deps import AdminOnly, get_db
from app.core.config import HEX_COLOR_PATTERN, get_settings
from app.core.url_guard import validate_ai_base_url
from app.services.reminder_scheduler import reminder_status
from app.services.runtime_config_service import (
    RuntimeConfigError,
    runtime_settings,
    settings_snapshot,
    theme_snapshot,
    update_runtime_values,
)
from app.translation import get_translation_provider

# 系统设置仅超管可见可改（除主题读取外）：设置页能改模型密钥、催交文案、
# 登录授权与同步调度，任何非 admin 角色都不应触达。member 由中间件白名单拦截，
# dept_admin 由本依赖显式 403，不依赖「顺带挡住」。
router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[AdminOnly])

# 主题读取单独成路由（无 AdminOnly）：主题是部署级外观配置，读无敏感性，
# 所有登录角色都应按公司主题渲染 UI；写入（PATCH）仍在上方 AdminOnly 路由。
# 非管理员的 GET 放行由中间件显式控制（仅 GET，见 main.py），写请求在
# 中间件层就会被 403。
theme_read_router = APIRouter(prefix="/api/settings", tags=["settings"])


def _validated_base_url(value: str | None) -> str | None:
    """主机白名单不允许从设置页放开，读的是部署侧配置（环境变量）。"""
    return validate_ai_base_url(value, get_settings().ai_allowed_base_hosts)


class AISettingsUpdate(BaseModel):
    ai_api_key: str | None = Field(default=None, max_length=1000)
    ai_base_url: str | None = Field(default=None, max_length=500)
    ai_model: str | None = Field(default=None, max_length=200)
    baidu_translate_app_id: str | None = Field(default=None, max_length=100)
    baidu_translate_api_key: str | None = Field(default=None, max_length=1000)
    baidu_translate_secret_key: str | None = Field(default=None, max_length=1000)
    # 语义检索（向量）：三项齐备即启用；清空任意一项即整体停用。
    ai_embedding_api_key: str | None = Field(default=None, max_length=1000)
    ai_embedding_base_url: str | None = Field(default=None, max_length=500)
    ai_embedding_model: str | None = Field(default=None, max_length=200)
    clear_ai_api_key: bool = False
    clear_baidu_translate_api_key: bool = False
    clear_baidu_translate_secret_key: bool = False
    clear_ai_embedding_api_key: bool = False

    @field_validator("ai_base_url", "ai_embedding_base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        # embedding 出站主机有独立白名单（部署侧 ai_embedding_allowed_hosts），
        # 这里先按通用规则校验格式；主机合法性在读取端按各自名单判定。
        return _validated_base_url(value)


class ConnectionTestRequest(BaseModel):
    target: Literal["deepseek", "baidu_translation"]


class ModelListRequest(BaseModel):
    ai_base_url: str | None = Field(default=None, max_length=500)
    ai_api_key: str | None = Field(default=None, max_length=1000)

    @field_validator("ai_base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _validated_base_url(value)


SECRET_FIELDS = {
    "ai_api_key",
    "baidu_translate_api_key",
    "baidu_translate_secret_key",
    "ai_embedding_api_key",
}


class ReminderSettingsUpdate(BaseModel):
    auto_enabled: bool | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    hour: int | None = Field(default=None, ge=0, le=23)
    # 周期归属天数：周期开始后前 N 天内提交仍归上一周期（周一才交上来的那篇属于上周）。
    # 0 = 关闭该行为，最大 7（超过一周就没有「上一周」可归属了）。
    attribution_days: int | None = Field(default=None, ge=0, le=7)
    # 催交文案。留空（None / ""）表示保持原值，与 AI 密钥同一约定；
    # 要恢复默认请用 clear_*，否则「清空输入框」会被误当成「恢复默认」。
    card_title: str | None = Field(default=None, max_length=128)
    card_body_zh: str | None = Field(default=None, max_length=1000)
    card_body_en: str | None = Field(default=None, max_length=1000)
    clear_card_text: bool = False
    # 跟进沟通的通知接收人（企微 userid，逗号分隔）。留空 = 保持原值，
    # 要清空用 clear_action_notify_userids —— 与密钥同一约定，避免误清。
    action_notify_admin_userids: str | None = Field(default=None, max_length=2000)
    clear_action_notify_userids: bool = False


THEME_RUNTIME_KEYS = {
    "primary": "ui_primary_color",
    "nav": "ui_nav_color",
    "accent": "ui_accent_color",
    "background": "ui_background_color",
    "surface": "ui_surface_color",
}


class ThemeSettingsUpdate(BaseModel):
    primary: str | None = Field(default=None, max_length=7)
    nav: str | None = Field(default=None, max_length=7)
    accent: str | None = Field(default=None, max_length=7)
    background: str | None = Field(default=None, max_length=7)
    surface: str | None = Field(default=None, max_length=7)
    reset: bool = False

    @field_validator("primary", "nav", "accent", "background", "surface")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(HEX_COLOR_PATTERN, value):
            raise ValueError("颜色必须是 6 位十六进制值，例如 #1A1C28")
        return value.lower()


@router.get("/ai")
def get_ai_settings(session: Annotated[Session, Depends(get_db)]) -> dict:
    try:
        return settings_snapshot(session)
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/reminder")
def get_reminder_settings(
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    try:
        return reminder_status(session)
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@theme_read_router.get("/theme")
def get_theme_settings(session: Annotated[Session, Depends(get_db)]) -> dict[str, str]:
    try:
        return theme_snapshot(session)
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/theme")
def update_theme_settings(
    payload: ThemeSettingsUpdate,
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    if payload.reset:
        values: dict[str, str | None] = {
            runtime_key: None for runtime_key in THEME_RUNTIME_KEYS.values()
        }
    else:
        values = {
            runtime_key: getattr(payload, field)
            for field, runtime_key in THEME_RUNTIME_KEYS.items()
            if field in payload.model_fields_set
        }
    if not values:
        return theme_snapshot(session)
    try:
        update_runtime_values(session, values)
        session.commit()
        return theme_snapshot(session)
    except RuntimeConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/reminder")
def update_reminder_settings(
    payload: ReminderSettingsUpdate,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    values: dict[str, str] = {}
    if "auto_enabled" in payload.model_fields_set and payload.auto_enabled is not None:
        values["remind_auto_enabled"] = "true" if payload.auto_enabled else "false"
    if "weekday" in payload.model_fields_set and payload.weekday is not None:
        values["remind_auto_weekday"] = str(payload.weekday)
    if "hour" in payload.model_fields_set and payload.hour is not None:
        values["remind_auto_hour"] = str(payload.hour)
    if (
        "attribution_days" in payload.model_fields_set
        and payload.attribution_days is not None
    ):
        values["report_period_attribution_days"] = str(payload.attribution_days)
    # 文案：空字符串按「保持原值」处理（update_runtime_values 会删掉该行 = 回退
    # 环境默认值，那不是管理员点「保存」时想要的语义）。要清空请用 clear_card_text。
    for field, runtime_key in (
        ("card_title", "remind_card_title"),
        ("card_body_zh", "remind_card_body_zh"),
        ("card_body_en", "remind_card_body_en"),
    ):
        value = getattr(payload, field, None)
        if field in payload.model_fields_set and value and value.strip():
            values[runtime_key] = value.strip()
    if payload.clear_card_text:
        values.update(
            {
                "remind_card_title": "",
                "remind_card_body_zh": "",
                "remind_card_body_en": "",
            }
        )
    # 跟进通知接收人：同样是空值 = 保持原值，clear_action_notify_userids 才清空。
    userids = payload.action_notify_admin_userids
    if "action_notify_admin_userids" in payload.model_fields_set and userids and userids.strip():
        values["action_notify_admin_userids"] = userids.strip()
    if payload.clear_action_notify_userids:
        values["action_notify_admin_userids"] = ""
    if not values:
        return reminder_status(session)
    try:
        update_runtime_values(session, values)
        session.commit()
        return reminder_status(session)
    except RuntimeConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/ai")
def update_ai_settings(
    payload: AISettingsUpdate, session: Annotated[Session, Depends(get_db)]
) -> dict:
    values: dict[str, str | None] = {}
    fields = payload.model_fields_set
    for key in (
        "ai_api_key",
        "ai_base_url",
        "ai_model",
        "baidu_translate_app_id",
        "baidu_translate_api_key",
        "baidu_translate_secret_key",
        "ai_embedding_api_key",
        "ai_embedding_base_url",
        "ai_embedding_model",
    ):
        if key in fields:
            value = getattr(payload, key)
            # 已保存密钥必须显式“删除”才能清除；空字符串视为“保持不变”。
            if key in SECRET_FIELDS and not (value or "").strip():
                continue
            values[key] = value
    if payload.clear_ai_api_key:
        values["ai_api_key"] = None
    if payload.clear_baidu_translate_api_key:
        values["baidu_translate_api_key"] = None
    if payload.clear_baidu_translate_secret_key:
        values["baidu_translate_secret_key"] = None
    if payload.clear_ai_embedding_api_key:
        values["ai_embedding_api_key"] = None
    if not values:
        # 空白密钥表示保持原值；只有空白密钥时是一次安全的无操作。
        return settings_snapshot(session)

    _guard_base_url_change(payload, values, session)

    try:
        update_runtime_values(session, values)
        session.commit()
        return settings_snapshot(session)
    except RuntimeConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _guard_base_url_change(
    payload: AISettingsUpdate, values: dict[str, str | None], session: Session
) -> None:
    """切换出站主机时必须同时提供新密钥。

    否则会命中一个很隐蔽的外发路径：只改 base_url、不填新 Key 时，系统会把
    **已保存的旧密钥**发到新主机（保存后的下一次 AI 调用即刻生效）。要求显式
    重新录入密钥，等于让「密钥被送往新主机」变成管理员的一次明确动作。
    """
    new_base = values.get("ai_base_url")
    if not isinstance(new_base, str):
        return
    try:
        current = runtime_settings(session).ai_base_url
    except RuntimeConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if urlparse(current).hostname == urlparse(new_base).hostname:
        # 同一主机的路径/协议微调不涉及密钥外发，无需重填。
        return
    if not (payload.ai_api_key or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "修改 Base URL 时必须同时重新填写 API Key："
                "否则已保存的密钥会被发往新的主机。"
            ),
        )


@router.post("/ai/models")
def list_ai_models(
    payload: ModelListRequest, session: Annotated[Session, Depends(get_db)]
) -> dict:
    """使用已保存密钥和当前 Base URL 探测上游模型列表。"""
    try:
        settings = runtime_settings(session)
        base_url = payload.ai_base_url or settings.ai_base_url
        api_key = (payload.ai_api_key or "").strip() or settings.ai_api_key
        effective = settings.model_copy(
            update={"ai_base_url": base_url, "ai_api_key": api_key}
        )
        provider = get_provider(effective)
        if not hasattr(provider, "list_models"):
            raise ValueError("当前模型服务不支持读取模型列表。")
        models = provider.list_models()
        return {
            "target": "deepseek",
            "ok": True,
            "models": models,
            "current_model": effective.ai_model,
            "base_url": effective.ai_base_url,
        }
    except (RuntimeConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ai/test")
def test_ai_connection(
    payload: ConnectionTestRequest, session: Annotated[Session, Depends(get_db)]
) -> dict:
    """显式连通性测试；此操作会产生一次第三方调用。"""
    try:
        settings = runtime_settings(session)
        if payload.target == "deepseek":
            result = get_provider(settings).chat("你是连通性测试。", "只回复 OK")
            return {"target": payload.target, "ok": True, "model": result.model or settings.ai_model}
        result = get_translation_provider(settings).translate("Weekly report connection test.")
        return {
            "target": payload.target,
            "ok": True,
            "sample": result.content[:120],
            "source_language": result.source_language,
        }
    except (RuntimeConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
