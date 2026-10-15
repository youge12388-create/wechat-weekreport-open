"""非敏感的全局配置透出（公开接口，无需鉴权）。

仅返回统计口径与周期规则等「说明性」信息，不含任何凭据或员工数据。
前端用它把「当前按什么规则统计」显式呈现出来，避免周期推导成为静默假设。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.time_utils import period_rule_label
from app.services.runtime_config_service import RuntimeConfigError, runtime_settings

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config(session: Annotated[Session, Depends(get_db)]) -> dict:
    """登录页也要读这个接口，所以它不能要求先登录。

    只暴露「用什么规则统计」和「有哪几种登录方式可选」这类说明性信息，
    不含任何凭据、员工数据或授权结果。
    """
    settings = get_settings()
    try:
        # 登录开关是运行期设置，登录页必须看到**当前值**，
        # 否则管理员打开企微登录后，登录页仍不显示入口。
        login_settings = runtime_settings(session)
        wecom_login_enabled = login_settings.wecom_login_enabled
    except RuntimeConfigError:
        wecom_login_enabled = settings.wecom_login_enabled

    return {
        "period_rule": settings.report_period_rule,
        "period_rule_label": period_rule_label(),
        "timezone_offset_hours": settings.report_timezone_offset_hours,
        "due_weekday": settings.report_due_weekday,
        "due_hour": settings.report_due_hour,
        # 登录方式：前端据此决定是否显示企微登录/扫码入口与密码兜底
        "wecom_login_enabled": wecom_login_enabled,
        "password_login_enabled": True,
    }
