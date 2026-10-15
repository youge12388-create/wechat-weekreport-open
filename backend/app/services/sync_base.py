"""同步基础设施：任务日志 + 原始数据幂等写入。

需求第 6.4 节要求：
    幂等 / 可重试 / 有日志 / 有失败状态 / 可手动重跑 / 单条异常不中断批次。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import RawPayload
from app.core.logging import get_logger
from app.core.time_utils import utcnow
from app.models.enums import ProcessStatus, SyncJobStatus
from app.models.raw_record import RawRecord
from app.models.sync_job import SyncJob

logger = get_logger(__name__)


def compute_content_hash(payload: dict) -> str:
    """原始 payload 指纹，用于判断重复同步时数据是否发生变化。"""
    serialized = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def upsert_raw_record(session: Session, raw: RawPayload) -> tuple[RawRecord, bool]:
    """写入 / 更新 RawRecord。

    返回 (record, changed)：
      - changed=True  首次写入，或 payload 发生变化（需重新处理）
      - changed=False 内容未变，下游可跳过后续处理

    幂等键：(source, source_type, external_id)
    """
    content_hash = compute_content_hash(raw.payload)
    record = session.scalar(
        select(RawRecord).where(
            RawRecord.source == raw.source,
            RawRecord.source_type == raw.source_type,
            RawRecord.external_id == raw.external_id,
        )
    )

    if record is None:
        record = RawRecord(
            source=raw.source,
            source_type=raw.source_type,
            external_id=raw.external_id,
            raw_payload=raw.payload,
            content_hash=content_hash,
            fetched_at=raw.fetched_at,
            process_status=ProcessStatus.PENDING,
            created_at=utcnow(),
        )
        session.add(record)
        session.flush()
        return record, True

    if record.content_hash != content_hash:
        # 内容变化：保留历史不可变语义的同时更新为最新原始数据，
        # 并把处理状态回退，触发下游重新处理。
        record.raw_payload = raw.payload
        record.content_hash = content_hash
        record.fetched_at = raw.fetched_at
        record.process_status = ProcessStatus.PENDING
        record.error_message = None
        session.flush()
        return record, True

    return record, False


def mark_processed(session: Session, record: RawRecord) -> None:
    record.process_status = ProcessStatus.PROCESSED
    record.processed_at = utcnow()
    record.error_message = None
    session.flush()


def mark_failed(session: Session, record: RawRecord, message: str) -> None:
    record.process_status = ProcessStatus.FAILED
    record.processed_at = utcnow()
    record.error_message = message[:2000]
    session.flush()


@dataclass
class SyncResult:
    """同步结果摘要，供 API / CLI 返回。"""

    source: str
    type: str
    job_id: int | None = None
    status: str = SyncJobStatus.SUCCESS
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "type": self.type,
            "job_id": self.job_id,
            "status": self.status,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "errors": self.errors[:50],  # 避免一次返回过多
        }


class SyncJobRunner:
    """同步任务上下文。

    用法：
        with SyncJobRunner(session, source, type, params) as job:
            ...
            job.success() / job.failed(external_id, message) / job.skipped()

    job 行在 __enter__ 时即提交，保证即使后续批次崩溃也能看到「进行中/失败」记录。
    """

    def __init__(
        self,
        session: Session,
        source: str,
        sync_type: str,
        params: dict | None = None,
        triggered_by: str = "system",
        job: SyncJob | None = None,
    ) -> None:
        self.session = session
        self.source = source
        self.sync_type = sync_type
        self._adopted_job = job
        self.result = SyncResult(
            source=source, type=sync_type, started_at=utcnow()
        )
        self.job = job or SyncJob(
            source=source,
            type=sync_type,
            status=SyncJobStatus.RUNNING,
            start_time=self.result.started_at,
            params=params or {},
            triggered_by=triggered_by,
            created_at=utcnow(),
        )

    def __enter__(self) -> SyncJobRunner:
        if self._adopted_job is None:
            self.session.add(self.job)
        else:
            # 接管排队任务：创建时间保留原始排队时间，开始时间改为真正开跑时间。
            self.job.status = SyncJobStatus.RUNNING
            self.job.start_time = self.result.started_at
            self.job.end_time = None
            self.job.error = None
        self.session.add(self.job)
        self.session.commit()
        return self

    def success(self, count: int = 1) -> None:
        self.result.success_count += count

    def skipped(self, count: int = 1) -> None:
        self.result.skipped_count += count

    def failed(self, external_id: str | None, message: str, code: str = "") -> None:
        self.result.failed_count += 1
        if len(self.result.errors) < 200:
            self.result.errors.append(
                {"external_id": external_id, "code": code, "message": message[:500]}
            )

    def _final_status(self, raised: bool) -> str:
        if raised and self.result.success_count == 0:
            return SyncJobStatus.FAILED
        if self.result.failed_count > 0:
            return (
                SyncJobStatus.PARTIAL
                if self.result.success_count > 0
                else SyncJobStatus.FAILED
            )
        return SyncJobStatus.SUCCESS

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        try:
            if exc_type is not None:
                # 批次级异常：回滚未提交的业务写入，但保留已提交的进度
                self.session.rollback()
                self.result.failed_count += 1
                self.result.errors.append(
                    {
                        "external_id": None,
                        "code": "batch_error",
                        "message": f"{exc_type.__name__}: {exc}",
                    }
                )

            finished = utcnow()
            self.job.end_time = finished
            self.job.success_count = self.result.success_count
            self.job.failed_count = self.result.failed_count
            self.job.skipped_count = self.result.skipped_count
            self.job.error = self.result.errors[:200]
            self.job.status = self._final_status(raised=exc_type is not None)

            self.result.job_id = self.job.id
            self.result.status = self.job.status
            self.result.finished_at = finished

            self.session.add(self.job)
            try:
                self.session.commit()
            except Exception as commit_exc:  # noqa: BLE001
                # 这里的 commit 是任务离开 RUNNING 的**唯一**位置：写不进去就必须让
                # 调用方知道。旧实现把异常吞掉后，上层 poll 出的仍是 RUNNING/成功，
                # 于是「运维看到成功、DB 里永远 RUNNING、后续同步被幂等去重挡死」。
                self.session.rollback()
                self.result.status = SyncJobStatus.FAILED
                self.result.errors.append(
                    {
                        "external_id": None,
                        "code": "persist_failed",
                        "message": f"任务状态写入失败：{commit_exc}",
                    }
                )
                logger.error(
                    "同步任务 %s 收尾状态写入失败，任务可能停留在 RUNNING：%s",
                    self.job.id,
                    commit_exc,
                )
                raise
        except Exception:  # noqa: BLE001  日志写入失败不应掩盖原始异常
            self.session.rollback()
        return False
