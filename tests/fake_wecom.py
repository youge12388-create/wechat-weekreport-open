"""测试用假 Connector。

它实现与真实 WeComClient 相同的 Protocol，因此测试的是 Service 的全部逻辑，
而不是被 mock 掉的空壳。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from app.connectors.base import OrganizationSource, RawPayload, ReportSource
from app.core.time_utils import from_timestamp, utcnow
from app.models.enums import DataSource, RawSourceType


def _dept(dept_id: int, name: str, parent_id: int = 0) -> dict:
    return {"id": dept_id, "name": name, "parentid": parent_id, "order": 1}


def _user(userid: str, name: str, dept: int, status: int = 1, position: str = "") -> dict:
    return {
        "userid": userid,
        "name": name,
        "department": [dept],
        "main_department": dept,
        "position": position,
        "status": status,
    }


def _journal_info(
    *,
    template_name: str = "本周工作汇报",
    report_time: int,
    userid: str = "u1",
    apply_data: dict | None = None,
    sys_journal_data: str | None = None,
    template_id: str = "tpl-weekly",
) -> dict:
    info: dict = {
        "journal_uuid": "generated",
        "template_name": template_name,
        "template_id": template_id,
        "report_time": report_time,
        "submitter": {"userid": userid},
        "receivers": [{"userid": "boss"}],
        "readed_receivers": [],
        "comments": [],
    }
    if apply_data is not None:
        info["apply_data"] = apply_data
    if sys_journal_data is not None:
        info["sys_journal_data"] = sys_journal_data
    return info


def text_apply_data(text: str, title: str = "工作内容") -> dict:
    return {
        "contents": [
            {
                "control": "Textarea",
                "id": "Text-1",
                "title": [{"text": title}],
                "value": {"text": text},
            }
        ]
    }


class FakeWeComClient(OrganizationSource, ReportSource):
    def __init__(
        self,
        departments: list[dict] | None = None,
        employees: list[dict] | None = None,
        reports: dict[str, dict] | None = None,
        detail_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.departments = departments if departments is not None else []
        self.employees = employees if employees is not None else []
        self.reports = reports or {}
        self.detail_errors = detail_errors or {}
        self.list_calls: list[tuple[datetime, datetime]] = []
        self.detail_calls: list[str] = []

    @property
    def source(self) -> str:
        return DataSource.WECOM

    # ---- OrganizationSource ----
    def list_departments(self) -> list[RawPayload]:
        now = utcnow()
        return [
            RawPayload(
                source=DataSource.WECOM,
                source_type="department",
                external_id=str(d["id"]),
                payload=d,
                fetched_at=now,
            )
            for d in self.departments
        ]

    def list_employees(self, root_department_id: int = 1) -> list[RawPayload]:
        now = utcnow()
        return [
            RawPayload(
                source=DataSource.WECOM,
                source_type="employee",
                external_id=str(u["userid"]),
                payload=u,
                fetched_at=now,
            )
            for u in self.employees
        ]

    # ---- ReportSource ----
    def iter_report_ids(
        self, start: datetime, end: datetime, **kwargs: object
    ) -> Iterator[str]:
        self.list_calls.append((start, end))
        for external_id, info in self.reports.items():
            report_time = from_timestamp(info.get("report_time"))
            if report_time is not None and start <= report_time <= end:
                yield external_id

    def get_report_detail(self, external_id: str) -> RawPayload:
        self.detail_calls.append(external_id)
        if external_id in self.detail_errors:
            raise self.detail_errors[external_id]
        info = self.reports.get(external_id)
        if info is None:
            raise KeyError(f"未找到汇报 {external_id}")
        payload = {"errcode": 0, "errmsg": "ok", "info": dict(info)}
        payload["info"]["journal_uuid"] = external_id
        return RawPayload(
            source=DataSource.WECOM,
            source_type=RawSourceType.WEEKLY_REPORT,
            external_id=external_id,
            payload=payload,
            fetched_at=utcnow(),
        )
