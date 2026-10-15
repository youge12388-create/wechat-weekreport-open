"""卡点生命周期状态机（候选 → 正式 → 休眠 → 归档 / 已解决）。

全部用真实 `_recalculate_task` 推导路径构造证据，不 mock 内部函数；
日期相对 `date.today()` 构造，避免测试因运行日期漂移而失效。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import Mock

from sqlalchemy.orm import Session

from app.models.blocker import Blocker
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services.structured_data_service import (
    _recalculate_task,
    sweep_blocker_lifecycle,
)


def _week_bounds(offset_weeks: int) -> tuple[date, date]:
    """以本周为基准回退 N 个自然周，返回该周的周一/周日。"""
    today = date.today()
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=offset_weeks)
    return monday, monday + timedelta(days=6)


def _make_evidence(
    session: Session,
    *,
    weeks: list[int] | None = None,
    end_offsets_days: list[int] | None = None,
    risk_level: str = "低",
    item_type: str = "blockers",
    employee_count: int = 1,
    key: str = "lifecycle-case",
    latest_status: str = "未解决",
) -> Task:
    """为同一任务构造跨周/跨员工的卡点证据，返回该 Task。

    ``weeks`` 是相对本周的回退周数列表；``end_offsets_days`` 直接指定每条
    证据的 end_date 距今天数（静默周数用整周计算，必须用天数控制边界）。
    两者只传其一；多员工时证据按员工轮流分布。
    """
    if (weeks is None) == (end_offsets_days is None):
        raise ValueError("weeks 与 end_offsets_days 必须二选一")
    offsets = end_offsets_days if end_offsets_days is not None else [
        week * 7 + 5 for week in weeks
    ]
    task = Task(canonical_key=key, title=f"{key}标题", status="BLOCKED")
    session.add(task)
    session.flush()
    for index, offset_days in enumerate(offsets):
        employee_index = index % employee_count
        employee = session.query(Employee).filter_by(
            external_id=f"life-emp-{employee_index}"
        ).one_or_none()
        if employee is None:
            employee = Employee(
                source="wecom",
                external_id=f"life-emp-{employee_index}",
                name=f"生命周期员工{employee_index}",
                status=EmployeeStatus.ACTIVE,
            )
            session.add(employee)
            session.flush()
        end = date.today() - timedelta(days=offset_days)
        start = end - timedelta(days=6)
        report = WeeklyReport(
            employee_id=employee.id,
            external_id=f"life-rep-{key}-{index}",
            report_type="weekly",
            title=f"生命周期周报{index}",
            content="证据正文",
            content_raw="证据正文",
            start_date=start,
            end_date=end,
            submitted_at=datetime.combine(end, datetime.min.time()),
            source="wecom",
        )
        session.add(report)
        session.flush()
        item = ReportItem(
            report_id=report.id,
            employee_id=employee.id,
            item_type=item_type,
            item_order=0,
            title=f"{key}标题",
            status=latest_status,
            source_text="证据原文",
            risk_level=risk_level if item_type in {"blockers", "risks"} else None,
        )
        session.add(item)
        session.flush()
        session.add(
            TaskReportLink(
                task_id=task.id, report_id=report.id, report_item_id=item.id
            )
        )
    session.commit()
    _recalculate_task(session, task.id)
    session.commit()
    return task


def _blocker(session: Session, task: Task) -> Blocker:
    return session.query(Blocker).filter_by(task_id=task.id).one()


def test_single_low_risk_mention_stays_candidate(db_session: Session) -> None:
    task = _make_evidence(db_session, weeks=[0], key="single-low")
    assert _blocker(db_session, task).status == "CANDIDATE"


def test_two_distinct_periods_upgrade_to_open(db_session: Session) -> None:
    task = _make_evidence(db_session, weeks=[0, 1], key="two-periods")
    blocker = _blocker(db_session, task)
    assert blocker.status == "OPEN"
    assert blocker.occurrence_count == 2


def test_cross_employee_same_week_upgrades_to_open(db_session: Session) -> None:
    task = _make_evidence(
        db_session, weeks=[0, 0], employee_count=2, key="cross-emp"
    )
    blocker = _blocker(db_session, task)
    assert blocker.status == "OPEN"
    assert blocker.occurrence_count == 2


def test_high_risk_single_mention_goes_straight_to_open(db_session: Session) -> None:
    task = _make_evidence(db_session, weeks=[0], risk_level="高", key="high-risk")
    assert _blocker(db_session, task).status == "OPEN"


def test_open_blocker_goes_dormant_after_two_silent_weeks(db_session: Session) -> None:
    # 最近证据距今 17 天（整周计算 = 2 周）：升级为 OPEN 后静默降为休眠。
    task = _make_evidence(db_session, end_offsets_days=[17, 24], key="dormant-case")
    blocker = _blocker(db_session, task)
    assert blocker.status == "DORMANT"


def test_any_blocker_archives_after_three_silent_weeks(db_session: Session) -> None:
    open_case = _make_evidence(db_session, end_offsets_days=[24, 31], key="archive-open")
    assert _blocker(db_session, open_case).status == "ARCHIVED"
    candidate_case = _make_evidence(db_session, end_offsets_days=[24], key="archive-candidate")
    assert _blocker(db_session, candidate_case).status == "ARCHIVED"


def test_new_evidence_revives_archived_blocker(db_session: Session) -> None:
    task = _make_evidence(db_session, end_offsets_days=[24], key="revive-case")
    assert _blocker(db_session, task).status == "ARCHIVED"

    # 新增本周证据后重算：单周 + 低风险 → 复活为候选；若是跨周证据则直接正式。
    start, end = _week_bounds(0)
    employee = db_session.query(Employee).filter_by(external_id="life-emp-0").one()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id="life-rep-revive-now",
        report_type="weekly",
        title="复活周报",
        content="再次出现",
        content_raw="再次出现",
        start_date=start,
        end_date=end,
        submitted_at=datetime.combine(end, datetime.min.time()),
        source="wecom",
    )
    db_session.add(report)
    db_session.flush()
    item = ReportItem(
        report_id=report.id,
        employee_id=employee.id,
        item_type="blockers",
        item_order=0,
        title="revive-case标题",
        status="未解决",
        source_text="再次出现的原文",
        risk_level="低",
    )
    db_session.add(item)
    db_session.flush()
    db_session.add(
        TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)
    )
    db_session.commit()
    _recalculate_task(db_session, task.id)
    db_session.commit()
    assert _blocker(db_session, task).status == "OPEN"


def test_resolved_blocker_survives_silence_and_low_risk_recalc(
    db_session: Session,
) -> None:
    task = _make_evidence(db_session, weeks=[0], key="resolved-keep")
    blocker = _blocker(db_session, task)
    blocker.status = "RESOLVED"  # 模拟手动标记已解决
    db_session.commit()

    # 静默 + 低风险重算不得覆盖手动解决。
    _recalculate_task(db_session, task.id)
    db_session.commit()
    assert _blocker(db_session, task).status == "RESOLVED"


def test_high_risk_new_evidence_reopens_resolved_blocker(
    db_session: Session,
) -> None:
    task = _make_evidence(db_session, weeks=[0], key="resolved-reopen")
    blocker = _blocker(db_session, task)
    blocker.status = "RESOLVED"
    db_session.commit()

    # 风险升高到高：允许重开为正式卡点。
    link = db_session.query(TaskReportLink).filter_by(task_id=task.id).first()
    item = db_session.query(ReportItem).filter_by(id=link.report_item_id).one()
    item.risk_level = "高"
    db_session.commit()
    _recalculate_task(db_session, task.id)
    db_session.commit()
    assert _blocker(db_session, task).status == "OPEN"


def test_sweep_is_idempotent(db_session: Session) -> None:
    task = _make_evidence(db_session, end_offsets_days=[24, 31], key="sweep-case")
    assert _blocker(db_session, task).status == "ARCHIVED"

    first = sweep_blocker_lifecycle(db_session)
    db_session.commit()
    second = sweep_blocker_lifecycle(db_session)
    db_session.commit()
    assert _blocker(db_session, task).status == "ARCHIVED"
    assert first["swept"] == second["swept"]


# --------------------------------------------------------------------------- #
# API：生命周期 scope 与手动状态接口
# --------------------------------------------------------------------------- #


import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import deps  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def client(db_session: Session) -> TestClient:
    app = create_app()

    def _override() -> Session:
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_blocker_api_exposes_lifecycle_scopes(
    client: TestClient, db_session: Session
) -> None:
    candidate = _make_evidence(db_session, end_offsets_days=[1], key="api-candidate")
    open_case = _make_evidence(db_session, weeks=[0, 1], key="api-open")
    archived = _make_evidence(db_session, end_offsets_days=[24], key="api-archived")

    # 默认（status=OPEN）主列表：只有正式卡点，候选/归档不出现。
    default_items = client.get("/api/blockers").json()["items"]
    default_ids = {item["id"] for item in default_items}
    assert all(item["status"] == "OPEN" for item in default_items)
    assert _blocker(db_session, open_case).id in default_ids
    assert candidate.id not in default_ids
    assert archived.id not in default_ids

    candidate_items = client.get(
        "/api/blockers", params={"scope": "candidate"}
    ).json()["items"]
    assert [item["id"] for item in candidate_items] == [candidate.id]
    assert candidate_items[0]["status"] == "CANDIDATE"

    archived_items = client.get(
        "/api/blockers", params={"scope": "archived"}
    ).json()["items"]
    assert [item["id"] for item in archived_items] == [archived.id]

    # 非法 scope 仍然 400。
    assert client.get("/api/blockers", params={"scope": "nope"}).status_code == 400


def test_blocker_list_filters_period_risk_duration_and_count(
    client: TestClient, db_session: Session
) -> None:
    high_long = _make_evidence(
        db_session, weeks=[0, 1, 2], risk_level="高", key="api-high-long"
    )
    low_long = _make_evidence(
        db_session, weeks=[0, 1, 2], risk_level="低", key="api-low-long"
    )
    recent = _make_evidence(
        # 固定在本周内；周一运行时「昨天」是上周日，不能算本周新增。
        db_session, end_offsets_days=[0], risk_level="高", key="api-recent"
    )
    high_blocker = _blocker(db_session, high_long)
    high_blocker.status = "OPEN"
    high_blocker.risk_level = "高"
    high_blocker.duration_weeks = 3
    low_blocker = _blocker(db_session, low_long)
    low_blocker.status = "OPEN"
    low_blocker.risk_level = "低"
    low_blocker.duration_weeks = 3
    recent_blocker = _blocker(db_session, recent)
    recent_blocker.status = "OPEN"
    recent_blocker.risk_level = "高"
    db_session.commit()
    current_monday = date.today() - timedelta(days=date.today().weekday())
    params = {
        "start": (current_monday - timedelta(weeks=3)).isoformat(),
        "end": (current_monday + timedelta(days=6)).isoformat(),
    }

    def ids(**extra: object) -> set[int]:
        response = client.get("/api/blockers", params={**params, **extra})
        assert response.status_code == 200
        return {item["id"] for item in response.json()["items"]}

    assert ids(scope="high") == {high_blocker.id, recent_blocker.id}
    assert ids(scope="long") == {high_blocker.id, low_blocker.id}
    new_this_week = client.get(
        "/api/blockers",
        params={
            "scope": "new",
            "start": current_monday.isoformat(),
            "end": (current_monday + timedelta(days=6)).isoformat(),
        },
    )
    assert {item["id"] for item in new_this_week.json()["items"]} == {recent_blocker.id}
    assert ids(weekly_only="true") == {
        high_blocker.id,
        low_blocker.id,
        recent_blocker.id,
    }
    assert ids(department_id=999999) == set()
    count = client.get("/api/blockers", params={**params, "scope": "all", "count_only": "true"})
    assert count.json() == {"total": 3}
    assert client.get("/api/blockers", params={"status": "invalid"}).status_code == 400
    assert client.get("/api/blockers", params={"risk_level": "urgent"}).status_code == 400


def test_blocker_detail_validates_period_and_returns_evidence(
    client: TestClient, db_session: Session
) -> None:
    task = _make_evidence(db_session, weeks=[0, 1], key="api-detail")
    blocker = _blocker(db_session, task)

    assert client.get(f"/api/blockers/{blocker.id}", params={"start": "2026-09-01"}).status_code == 400
    assert client.get(
        f"/api/blockers/{blocker.id}",
        params={"start": "2026-09-10", "end": "2026-09-01"},
    ).status_code == 400
    response = client.get(f"/api/blockers/{blocker.id}")
    assert response.status_code == 200
    assert response.json()["id"] == blocker.id
    assert response.json()["sources"]
    assert client.get("/api/blockers/999999").status_code == 404


def test_blocker_period_summary_routes_use_requested_period_and_force(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.routes import blockers as blocker_routes

    state = {"status": "ready"}
    queued = {"queued": True}
    read = Mock(return_value=state)
    enqueue = Mock(return_value=queued)
    monkeypatch.setattr(blocker_routes, "blocker_summary_state", read)
    monkeypatch.setattr(blocker_routes, "enqueue_blocker_summary", enqueue)
    params = {"start": "2026-09-07", "end": "2026-09-13"}

    assert client.get("/api/blockers/period-summary", params=params).json() == state
    assert client.post(
        "/api/blockers/period-summary", json={**params, "force": True}
    ).json() == queued
    assert read.call_args.args[1].isoformat() == "2026-09-07"
    assert enqueue.call_args.args[1].isoformat() == "2026-09-07"
    assert enqueue.call_args.kwargs == {"force": True}


def test_manual_status_resolve_survives_sweep_and_reopens(
    client: TestClient, db_session: Session
) -> None:
    task = _make_evidence(db_session, end_offsets_days=[1], key="api-manual")
    blocker = _blocker(db_session, task)
    assert blocker.status == "CANDIDATE"

    resp = client.post(f"/api/blockers/{blocker.id}/status", json={"action": "resolve"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "RESOLVED"

    # 静默/重算不得覆盖手动解决。
    sweep_blocker_lifecycle(db_session)
    db_session.commit()
    db_session.refresh(blocker)
    assert blocker.status == "RESOLVED"

    resp = client.post(f"/api/blockers/{blocker.id}/status", json={"action": "reopen"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANDIDATE"

    assert client.post(
        f"/api/blockers/{blocker.id}/status", json={"action": "nope"}
    ).status_code == 400
    assert client.post(
        "/api/blockers/999999/status", json={"action": "resolve"}
    ).status_code == 404


def test_manual_status_requires_admin(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings, reload_settings

    task = _make_evidence(db_session, end_offsets_days=[1], key="api-perm")
    blocker = _blocker(db_session, task)

    # 测试环境默认放行 require_admin；切到生产语义后无管理员角色一律 403。
    monkeypatch.setenv("APP_ENV", "prod")
    reload_settings()
    try:
        resp = client.post(
            f"/api/blockers/{blocker.id}/status", json={"action": "resolve"}
        )
        assert resp.status_code == 403
    finally:
        monkeypatch.delenv("APP_ENV", raising=False)
        reload_settings()
