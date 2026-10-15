from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import AdminOnly, get_db, get_wecom_client
from app.connectors.wecom.client import WeComClient
from app.core.time_utils import utcnow
from app.models.sync_job import SyncJob
from app.services.runtime_config_service import (
    RuntimeConfigError,
    update_runtime_values,
)
from app.services.department_sync import sync_departments
from app.services.employee_sync import sync_employees
from app.services.sync_scheduler import (
    REPORT_SYNC_ALL_HISTORY,
    REPORT_SYNC_CURRENT_PERIOD,
    REPORT_SYNC_HISTORY,
    REPORT_SYNC_LAST_PERIOD,
    classify_sync_error_text,
    describe_report_sync_params,
    enqueue_manual_sync,
    enqueue_report_sync,
    serialize_sync_job,
    sync_status,
)
from app.services.weekly_report_sync import sync_weekly_reports

router = APIRouter(prefix="/api/sync", tags=["sync"])


class ReportSyncRequest(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    days: int | None = Field(default=None, ge=1, le=365)


class ReportScheduleUpdate(BaseModel):
    auto_enabled: bool | None = None
    auto_period_summary_enabled: bool | None = None
    ai_auto_enqueue_on_sync: bool | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)


class ReportQueueRequest(BaseModel):
    """手动抓周报：上一周期 / 近 N 天 / 全部历史 / 当前周期。"""

    mode: Literal[
        REPORT_SYNC_LAST_PERIOD,
        REPORT_SYNC_HISTORY,
        REPORT_SYNC_ALL_HISTORY,
        REPORT_SYNC_CURRENT_PERIOD,
    ] = REPORT_SYNC_LAST_PERIOD
    days: int | None = Field(default=None, ge=1, le=365)


@router.post("/wecom/departments", dependencies=[AdminOnly])
def sync_wecom_departments(
    session: Annotated[Session, Depends(get_db)],
    client: Annotated[WeComClient, Depends(get_wecom_client)],
    triggered_by: str = "api",
) -> dict:
    return sync_departments(session, client, triggered_by=triggered_by).as_dict()


@router.post("/wecom/employees", dependencies=[AdminOnly])
def sync_wecom_employees(
    session: Annotated[Session, Depends(get_db)],
    client: Annotated[WeComClient, Depends(get_wecom_client)],
    root_department_id: int = 1,
    triggered_by: str = "api",
) -> dict:
    return sync_employees(
        session, client, root_department_id, triggered_by=triggered_by
    ).as_dict()


@router.post("/wecom/all", dependencies=[AdminOnly])
def queue_full_wecom_sync(
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """手动同步入口：只入队，不在请求线程里跑企微拉取。

    返回的 created=false 表示同类型已有排队/执行中的任务，本次点击被去重。
    """
    jobs = enqueue_manual_sync(session)
    return {
        "queued": True,
        "jobs": jobs,
        "message": "同步任务已提交后台执行，可在同步状态中查看进度。",
    }


@router.post("/wecom/reports/queue", dependencies=[AdminOnly])
def queue_report_sync(
    payload: ReportQueueRequest,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """只排队周报抓取，不动部门/员工通讯录。

    - `mode=last_period`：抓「上一个完整周期」（跟随 report_period_rule）
    - `mode=history`：抓「近 N 天」（默认 wecom_sync_lookback_days）
    - `mode=all_history`：从 Unix 时间起点按月扫描，抓企微可返回的全部历史
    - `mode=current_period`：抓「本周期开始到现在」，供「本周概览」看最新进度
    同类型已有排队/执行中的任务时 created=false，重复点击不会叠出多条抓取。
    """
    job, created = enqueue_report_sync(
        session, mode=payload.mode, days=payload.days
    )
    window = describe_report_sync_params(job.params or {})
    return {
        "queued": True,
        "created": created,
        "job": serialize_sync_job(job),
        "window": window,
        "message": (
            f"周报同步任务已提交后台（{window}），可在同步状态中查看进度。"
            if created
            else f"已有周报同步任务在排队/执行中（{window}），本次点击未重复创建。"
        ),
    }


@router.post("/wecom/reports", dependencies=[AdminOnly])
def sync_wecom_reports(
    payload: ReportSyncRequest,
    session: Annotated[Session, Depends(get_db)],
    client: Annotated[WeComClient, Depends(get_wecom_client)],
    triggered_by: str = "api",
) -> dict:
    end = payload.end or utcnow()
    start = payload.start
    if start is None and payload.days:
        start = end - timedelta(days=payload.days)
    return sync_weekly_reports(
        session, start, end, client, triggered_by=triggered_by
    ).as_dict()


@router.get("/status")
def get_sync_status(
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """同步健康状态：上次成功/失败、失败原因、下次运行时间和排队数量。"""
    return sync_status(session)


@router.post("/wecom/probe", dependencies=[AdminOnly])
def probe_wecom_connectivity(
    client: Annotated[WeComClient, Depends(get_wecom_client)],
) -> dict:
    """现场验证企微连通性，并回显「企微看到的出口 IP」。

    用于回答「我已经把 IP 加进可信名单了，到底生效没有」：
    不必等下一次定时同步，直接跑一次探针即可。只读，不写库、不改业务数据。
    """
    result = client.health_check()
    failed = next((step for step in result["steps"] if not step["ok"]), None)
    detail = None
    if failed:
        detail = classify_sync_error_text(
            str(failed.get("error", "")), step=str(failed.get("step", ""))
        )
    return {
        "ok": result["ok"],
        "steps": result["steps"],
        "egress_ip": detail.get("egress_ip") if detail else None,
        "error": detail,
        "checked_at": utcnow().isoformat(),
    }


@router.patch("/schedule", dependencies=[AdminOnly])
def update_report_schedule(
    payload: ReportScheduleUpdate,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    """保存周报固定调度；Worker 下一轮按新配置执行，无需重启。"""
    values: dict[str, str] = {}
    fields = payload.model_fields_set
    if "auto_enabled" in fields and payload.auto_enabled is not None:
        values["sync_auto_enabled"] = (
            "true" if payload.auto_enabled else "false"
        )
    if (
        "auto_period_summary_enabled" in fields
        and payload.auto_period_summary_enabled is not None
    ):
        values["sync_auto_period_summary_enabled"] = (
            "true" if payload.auto_period_summary_enabled else "false"
        )
    if (
        "ai_auto_enqueue_on_sync" in fields
        and payload.ai_auto_enqueue_on_sync is not None
    ):
        values["ai_auto_enqueue_on_sync"] = (
            "true" if payload.ai_auto_enqueue_on_sync else "false"
        )
    if "weekday" in fields and payload.weekday is not None:
        values["sync_reports_weekday"] = str(payload.weekday)
    if "hour" in fields and payload.hour is not None:
        values["sync_reports_hour"] = str(payload.hour)
    if "minute" in fields and payload.minute is not None:
        values["sync_reports_minute"] = str(payload.minute)
    if not values:
        return sync_status(session)
    try:
        update_runtime_values(session, values)
        session.commit()
        return sync_status(session)
    except RuntimeConfigError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/jobs")
def list_sync_jobs(
    session: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    sync_type: Annotated[str | None, Query(alias="type")] = None,
) -> list[dict]:
    """查看同步日志（需求第 6.4 / 19 节）。"""
    query = select(SyncJob)
    if sync_type:
        query = query.where(SyncJob.type == sync_type)
    jobs = session.scalars(query.order_by(SyncJob.id.desc()).limit(limit))
    return [serialize_sync_job(job) for job in jobs]


@router.get("/jobs/{job_id}", dependencies=[AdminOnly])
def get_sync_job(
    job_id: int,
    session: Annotated[Session, Depends(get_db)],
) -> dict:
    job = session.get(SyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    return serialize_sync_job(job)
