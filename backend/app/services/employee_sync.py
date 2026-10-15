"""企业微信 -> Employee。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import OrganizationSource, RawPayload
from app.connectors.wecom.client import USER_STATUS_MAP
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import DataSource, EmployeeStatus, SyncType
from app.models.sync_job import SyncJob
from app.services.sync_base import (
    SyncJobRunner,
    SyncResult,
    mark_processed,
    upsert_raw_record,
)

logger = get_logger(__name__)


def _resolve_department_id(
    session: Session, payload: dict, source: str
) -> int | None:
    """员工部门：优先 main_department，退回 department[0]。"""
    external_id = payload.get("main_department")
    if external_id in (None, "", 0):
        departments = payload.get("department") or []
        external_id = departments[0] if departments else None
    if external_id in (None, "", 0):
        return None

    department = session.scalar(
        select(Department).where(
            Department.source == source,
            Department.external_id == str(external_id),
        )
    )
    if department is None:
        logger.warning(
            "员工 %s 的部门 %s 尚未入库，本次不关联（请先同步部门）",
            payload.get("userid"),
            external_id,
        )
        return None
    return department.id


def _upsert_employee(session: Session, raw: RawPayload) -> Employee:
    payload = raw.payload
    employee = session.scalar(
        select(Employee).where(
            Employee.source == raw.source,
            Employee.external_id == raw.external_id,
        )
    )
    if employee is None:
        employee = Employee(source=raw.source, external_id=raw.external_id)
        session.add(employee)

    employee.name = str(payload.get("name") or "")
    employee.position = payload.get("position") or None
    employee.status = USER_STATUS_MAP.get(payload.get("status"), EmployeeStatus.ACTIVE)
    employee.department_id = _resolve_department_id(session, payload, raw.source)
    employee.last_seen_at = raw.fetched_at
    employee.updated_at = utcnow()
    session.flush()
    return employee


def sync_employees(
    session: Session,
    client: OrganizationSource | None = None,
    root_department_id: int = 1,
    triggered_by: str = "system",
    existing_job: SyncJob | None = None,
) -> SyncResult:
    """同步员工。

    覆盖场景：新增 / 离职(status=5) / 部门调动 / 岗位变化。

    已知取舍：对于「本次接口未返回」的员工，**不会**自动标记为离职。
    接口偶发分页失败会导致全员被误判离职，风险远大于收益；
    这里只更新 last_seen_at，供后续人工或看板判断。
    """
    if client is None:
        from app.connectors.wecom.client import WeComClient

        client = WeComClient()

    seen_external_ids: set[str] = set()
    with SyncJobRunner(
        session,
        DataSource.WECOM,
        SyncType.EMPLOYEES,
        params={"root_department_id": root_department_id},
        triggered_by=triggered_by,
        job=existing_job,
    ) as job:
        payloads = client.list_employees(root_department_id=root_department_id)
        seen_external_ids = {raw.external_id for raw in payloads}

        for raw in payloads:
            try:
                record, changed = upsert_raw_record(session, raw)
                _upsert_employee(session, raw)
                mark_processed(session, record)
                session.commit()
                if changed:
                    job.success()
                else:
                    job.skipped()
            except Exception as exc:  # noqa: BLE001  单条失败不影响整批
                session.rollback()
                logger.warning("员工 %s 同步失败：%s", raw.external_id, exc)
                job.failed(raw.external_id, str(exc))

    # 疑似离职标记（P2-10）：仅当本次确有数据返回（空批次=接口抽风，绝不触发，
    # 否则会误判全员离职），且只把「在职、超过阈值天数未出现」的员工标为 MISSING。
    if seen_external_ids:
        from datetime import timedelta

        cutoff = utcnow() - timedelta(days=get_settings().employee_missing_days)
        stale = session.scalars(
            select(Employee)
            .where(
                Employee.source == DataSource.WECOM,
                Employee.status == EmployeeStatus.ACTIVE,
                Employee.external_id.notin_(seen_external_ids),
                Employee.last_seen_at < cutoff,
            )
        ).all()
        for emp in stale:
            emp.status = EmployeeStatus.MISSING
        if stale:
            session.commit()
            logger.info("标记 %d 名员工为疑似离职（MISSING）", len(stale))

    return job.result
