"""企业微信 -> Department。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import OrganizationSource, RawPayload
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.department import Department
from app.models.enums import DataSource, DepartmentStatus, SyncType
from app.models.sync_job import SyncJob
from app.services.sync_base import (
    SyncJobRunner,
    SyncResult,
    mark_processed,
    upsert_raw_record,
)

logger = get_logger(__name__)


def _upsert_department(session: Session, raw: RawPayload) -> Department:
    payload = raw.payload
    parent_external_id = payload.get("parentid")
    # 根部门 parentid=0，不视为有效父级
    parent_external_id = (
        str(parent_external_id) if parent_external_id not in (None, 0, "0") else None
    )

    department = session.scalar(
        select(Department).where(
            Department.source == raw.source,
            Department.external_id == raw.external_id,
        )
    )
    if department is None:
        department = Department(
            source=raw.source, external_id=raw.external_id
        )
        session.add(department)

    department.name = str(payload.get("name") or "")
    department.parent_external_id = parent_external_id
    department.status = DepartmentStatus.ACTIVE
    department.last_seen_at = raw.fetched_at
    department.updated_at = utcnow()
    session.flush()
    return department


def _resolve_parent_ids(session: Session, source: str) -> None:
    """第二遍：按 external_id 解析父子关系，避免依赖插入顺序。"""
    departments = list(
        session.scalars(select(Department).where(Department.source == source))
    )
    by_external_id = {d.external_id: d for d in departments}
    for department in departments:
        if department.parent_external_id:
            parent = by_external_id.get(department.parent_external_id)
            department.parent_id = parent.id if parent else None
        else:
            department.parent_id = None
    session.flush()


def _deactivate_missing(
    session: Session, source: str, seen_external_ids: set[str]
) -> int:
    """把本次未出现的部门置为 inactive（不物理删除）。

    仅在本批次 **零失败** 时执行：分页失败会导致部门列表不完整，
    此时停用会误伤整个组织架构。
    """
    if not seen_external_ids:
        return 0
    missing = list(
        session.scalars(
            select(Department).where(
                Department.source == source,
                Department.status == DepartmentStatus.ACTIVE,
                Department.external_id.notin_(seen_external_ids),
            )
        )
    )
    for department in missing:
        department.status = DepartmentStatus.INACTIVE
        department.updated_at = utcnow()
    if missing:
        session.flush()
        logger.info("部门同步：%s 个部门未在数据源中出现，已置为 inactive", len(missing))
    return len(missing)


def sync_departments(
    session: Session,
    client: OrganizationSource | None = None,
    triggered_by: str = "system",
    existing_job: SyncJob | None = None,
) -> SyncResult:
    """同步部门。

    覆盖场景：新增 / 改名 / 父子关系变化 / 删除停用。
    幂等：重复执行不产生重复数据。
    """
    if client is None:
        from app.connectors.wecom.client import WeComClient

        client = WeComClient()

    with SyncJobRunner(
        session,
        DataSource.WECOM,
        SyncType.DEPARTMENTS,
        triggered_by=triggered_by,
        job=existing_job,
    ) as job:
        payloads = client.list_departments()
        seen: set[str] = set()

        for raw in payloads:
            try:
                record, changed = upsert_raw_record(session, raw)
                _upsert_department(session, raw)
                mark_processed(session, record)
                session.commit()
                seen.add(raw.external_id)
                if changed:
                    job.success()
                else:
                    job.skipped()
            except Exception as exc:  # noqa: BLE001  单条失败不影响整批
                session.rollback()
                logger.warning("部门 %s 同步失败：%s", raw.external_id, exc)
                job.failed(raw.external_id, str(exc))

        if job.result.failed_count == 0:
            try:
                _resolve_parent_ids(session, DataSource.WECOM)
                _deactivate_missing(session, DataSource.WECOM, seen)
                session.commit()
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                job.failed(None, f"解析父子关系/停用判定失败：{exc}", code="post_process")

    return job.result
