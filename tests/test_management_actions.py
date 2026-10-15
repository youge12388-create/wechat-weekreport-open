from datetime import date, datetime
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import deps
from app.main import create_app
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _add_blocker_evidence(session: Session) -> tuple[int, int]:
    department = Department(source="wecom", external_id="management-dept", name="技术部", status="active")
    employee = Employee(
        external_id="management-owner",
        name="管理负责人",
        department=department,
        status="active",
        source="wecom",
    )
    report = WeeklyReport(
        employee=employee,
        external_id="management-report",
        report_type="weekly",
        title="本周周报",
        content="等待外部验收确认",
        content_raw="等待外部验收确认",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        submitted_at=datetime(2026, 9, 12, 10, 0),
        source="wecom",
    )
    session.add(report)
    session.flush()
    employee_id = int(employee.id)
    item = ReportItem(
        report_id=report.id,
        employee_id=employee_id,
        item_type="blockers",
        item_order=0,
        title="等待外部验收",
        description="等待验收方确认时间",
        status="未解决",
        source_text="等待外部验收确认",
        risk_level="中",
    )
    task = Task(
        canonical_key="等待外部验收",
        title="等待外部验收",
        status="BLOCKED",
        first_seen_date=date(2026, 9, 7),
        last_seen_date=date(2026, 9, 13),
        duration_weeks=1,
    )
    session.add_all([item, task])
    session.flush()
    blocker = Blocker(
        task_id=task.id,
        title="等待外部验收",
        description="等待验收方确认时间",
        status="OPEN",
        risk_level="中",
        first_seen_date=date(2026, 9, 7),
        last_seen_date=date(2026, 9, 13),
        duration_weeks=1,
        occurrence_count=1,
    )
    session.add_all([blocker, TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)])
    session.commit()
    return blocker.id, employee_id


def test_management_action_create_update_reopen_and_audit(
    client: TestClient, db_session: Session
) -> None:
    blocker_id, owner_id = _add_blocker_evidence(db_session)
    payload = {
        "source_type": "blocker",
        "source_id": blocker_id,
        "start": "2026-09-07",
        "end": "2026-09-13",
    }

    created = client.post("/api/management-actions", json=payload)
    assert created.status_code == 200
    body = created.json()
    assert body["created"] is True
    assert body["status"] == "OPEN"
    assert body["source_id"] == blocker_id
    assert body["source_snapshot"]["source_report_ids"]
    assert body["events"][0]["event_type"] == "created"

    duplicate = client.post("/api/management-actions", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    assert duplicate.json()["id"] == body["id"]

    updated = client.patch(
        f"/api/management-actions/{body['id']}",
        json={
            "owner_employee_id": owner_id,
            "due_date": "2026-09-18",
            "next_step": "确认验收时间并同步项目群",
            "note": "老板会议已确认优先推动",
            "priority": "high",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["owner_employee_id"] == owner_id
    assert updated.json()["priority"] == "high"
    assert any(event["event_type"] == "updated" for event in updated.json()["events"])

    in_progress = client.post(
        f"/api/management-actions/{body['id']}/status",
        json={"status": "IN_PROGRESS", "note": "已安排本周跟进"},
    )
    assert in_progress.status_code == 200
    assert in_progress.json()["status"] == "IN_PROGRESS"

    resolved = client.post(
        f"/api/management-actions/{body['id']}/status",
        json={"status": "RESOLVED", "note": "验收时间已确认"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"
    assert resolved.json()["closed_at"] is not None

    reopened = client.post(
        f"/api/management-actions/{body['id']}/status",
        json={"status": "IN_PROGRESS", "note": "新增验收范围，重新跟进"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "IN_PROGRESS"
    assert reopened.json()["closed_at"] is None
    assert len(reopened.json()["events"]) >= 5

    listed = client.get(
        "/api/management-actions",
        params={"start": "2026-09-07", "end": "2026-09-13"},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["next_step"] == "确认验收时间并同步项目群"


def test_tracked_blocker_leaves_dashboard_discovery_for_every_action_status(
    client: TestClient, db_session: Session
) -> None:
    blocker_id, _ = _add_blocker_evidence(db_session)
    blocker = db_session.get(Blocker, blocker_id)
    assert blocker is not None
    blocker.duration_weeks = 3
    db_session.commit()
    period = {"start": "2026-09-07", "end": "2026-09-13"}

    def home_signals() -> dict:
        response = client.get("/api/dashboard/overview", params=period)
        assert response.status_code == 200
        return response.json()

    before = home_signals()
    assert any(item.get("blocker_id") == blocker_id for item in before["insights"])
    assert any(item.get("source_id") == blocker_id for item in before["action_items"])

    created = client.post("/api/management-actions", json={
        "source_type": "blocker", "source_id": blocker_id, **period,
    })
    assert created.status_code == 200
    action_id = created.json()["id"]
    for status in (None, "IN_PROGRESS", "WAITING", "RESOLVED", "OPEN", "CANCELLED"):
        if status is not None:
            changed = client.post(
                f"/api/management-actions/{action_id}/status",
                json={"status": status, "notify": False},
            )
            assert changed.status_code == 200
        overview = home_signals()
        assert all(item.get("blocker_id") != blocker_id for item in overview["insights"])
        assert all(item.get("source_id") != blocker_id for item in overview["action_items"])
        assert overview["blocker_total"] == 1  # 原始卡点仍开放，统计口径不变

    # 跟进记录及原文证据仍可回看，不会因首页移除而丢失。
    assert client.get(f"/api/management-actions/{action_id}").json()["status"] == "CANCELLED"
    assert client.get(f"/api/blockers/{blocker_id}", params=period).json()["sources"]


def test_management_action_requires_current_source_evidence(client: TestClient) -> None:
    response = client.post(
        "/api/management-actions",
        json={
            "source_type": "blocker",
            "source_id": 99999,
            "start": "2026-09-07",
            "end": "2026-09-13",
        },
    )
    assert response.status_code == 400
    assert "证据" in response.json()["detail"]
