"""企微「资源需求」直出卡点与历史队列回填的闭环测试。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import deps
from app.main import create_app
from app.models.analysis_queue import AnalysisQueue
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AnalysisQueueStatus, EmployeeStatus
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.processors.journal_parser import extract_resource_demand
from app.services.analysis_queue_service import (
    enqueue_missing_period_reports,
    enqueue_report,
)
from app.services.structured_data_service import (
    backfill_resource_demands,
    materialize_report_analysis,
    materialize_resource_demand,
)
from app.services.insight_service import analysis_coverage
from app.services.weekly_report_sync import sync_weekly_reports
from tests.fake_wecom import FakeWeComClient, _dept, _journal_info, _user, text_apply_data


PERIOD_START = date(2026, 8, 31)
PERIOD_END = date(2026, 9, 6)


@pytest.fixture(autouse=True)
def stable_blocker_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定生命周期判断日期，避免历史夹具随现实时间推移而变成休眠/归档。"""
    monkeypatch.setattr(
        "app.services.structured_data_service.business_date",
        lambda _value: date(2026, 9, 14),
    )


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _report(
    session: Session,
    external_id: str,
    content: str,
    *,
    start: date = PERIOD_START,
    end: date = PERIOD_END,
) -> WeeklyReport:
    employee = session.query(Employee).filter_by(external_id="resource-emp").one_or_none()
    if employee is None:
        employee = Employee(
            source="wecom",
            external_id="resource-emp",
            name="资源测试员",
            status=EmployeeStatus.ACTIVE,
        )
        session.add(employee)
        session.flush()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id=external_id,
        report_type="weekly",
        title="本周工作汇报",
        content=content,
        content_raw=content,
        start_date=start,
        end_date=end,
        submitted_at=datetime.combine(start, datetime.min.time()),
        source="wecom",
    )
    session.add(report)
    session.commit()
    return report


def test_extract_resource_demand_filters_empty_values() -> None:
    assert extract_resource_demand("资源需求：需要市场经理确认投放预算\n下周计划：继续跟进") == "需要市场经理确认投放预算"
    assert extract_resource_demand("资源需求：暂无\n下周计划：继续跟进") == ""
    assert extract_resource_demand("Resource Requirements: N/A\nNext Week Plan: follow up") == ""
    assert extract_resource_demand("资源需求：无特殊资源需求。") == ""
    assert extract_resource_demand("资源需求：暂无重大资源限制\n下周计划：继续跟进") == ""
    assert extract_resource_demand(
        "资源需求：No additional resources required at present.\n"
        "Continue current work as planned.\n下周计划：follow up"
    ) == ""
    assert extract_resource_demand("资源需求：/\n下周计划：follow up") == ""


def test_extract_resource_demand_filters_placeholder_answers() -> None:
    """「Discuss later」「暂无需要公司协调解决的问题」是占位回答，不是需求。

    物化层必须与经营看板同一套宽口径判空，否则看板排掉的空话会漏进
    处理中心，还被每周重复填写推成「长期卡点」置顶（线上实测）。
    """
    assert extract_resource_demand("资源需求：Discuss later\n下周计划：follow up") == ""
    assert extract_resource_demand("资源需求：Discuss Later.\n下周计划：follow up") == ""
    assert (
        extract_resource_demand("资源需求：We can discuss this later\n下周计划：follow up")
        == ""
    )
    assert (
        extract_resource_demand("资源需求：暂无需要公司协调解决的问题\n下周计划：继续跟进")
        == ""
    )
    assert (
        extract_resource_demand(
            "资源需求：no additional support is required at the moment\n下周计划：follow up"
        )
        == ""
    )
    # 转折句保护：先否定后补充的仍视为真实候选，不得静默丢弃。
    assert extract_resource_demand("资源需求：暂无特殊资源需求，希望后续能提供院校资料\n下周计划：跟进") != ""


def test_numbered_resource_demand_has_readable_title(db_session: Session) -> None:
    report = _report(
        db_session,
        "numbered-resource",
        "资源需求：1.审批节点确认：请协助推进\n2.材料模板\n下周计划：继续跟进",
    )
    materialize_resource_demand(db_session, report)
    item = db_session.scalar(
        select(ReportItem).where(
            ReportItem.report_id == report.id,
            ReportItem.task_key.like("resource_demand:%"),
        )
    )
    assert item is not None
    assert item.title == "资源需求：审批节点确认"


def test_resource_demand_title_splits_on_english_colon(db_session: Session) -> None:
    report = _report(
        db_session,
        "english-colon-resource",
        "资源需求：Approval owner: please help confirm\n下周计划：继续跟进",
    )
    materialize_resource_demand(db_session, report)
    item = db_session.scalar(
        select(ReportItem).where(
            ReportItem.report_id == report.id,
            ReportItem.task_key.like("resource_demand:%"),
        )
    )
    assert item is not None
    assert item.title == "资源需求：Approval owner"


def test_sync_materializes_resource_demand_as_candidate_blocker(db_session: Session) -> None:
    client = FakeWeComClient(
        departments=[_dept(1, "总部"), _dept(2, "申请部", 1)],
        employees=[_user("resource-emp", "资源测试员", 2)],
        reports={
            "resource-report": _journal_info(
                report_time=int(datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp()),
                userid="resource-emp",
                apply_data=text_apply_data(
                    "需要市场经理确认投放预算", title="资源需求"
                ),
            )
        },
    )
    from app.services.department_sync import sync_departments
    from app.services.employee_sync import sync_employees

    sync_departments(db_session, client)
    sync_employees(db_session, client)
    sync_weekly_reports(
        db_session,
        datetime(2026, 8, 31),
        datetime(2026, 9, 6),
        client,
    )

    item = db_session.query(ReportItem).one()
    assert item.item_type == "blockers"
    assert item.task_key.startswith("resource_demand:")
    assert item.source_text == "资源需求：需要市场经理确认投放预算"
    link = db_session.query(TaskReportLink).one()
    assert link.match_method == "resource_demand"
    blocker = db_session.query(Blocker).one()
    # 单周一次性资源需求只进候选层，不再直接淹没处理中心待处理列表。
    assert blocker.status == "CANDIDATE"
    assert blocker.title == "资源需求：需要市场经理确认投放预算"

    # 同一条企微记录再次同步只会跳过，不重复生成任务/卡点。
    sync_weekly_reports(
        db_session,
        datetime(2026, 8, 31),
        datetime(2026, 9, 6),
        client,
    )
    assert db_session.query(ReportItem).count() == 1
    assert db_session.query(Task).count() == 1
    assert db_session.query(Blocker).count() == 1
    assert analysis_coverage(db_session, PERIOD_START, PERIOD_END) == {
        "reports_total": 1,
        "analyzed_reports": 0,
        "rate": 0,
    }


def test_empty_resource_demand_does_not_create_blocker(db_session: Session) -> None:
    report = _report(db_session, "empty-resource", "重点工作：完成测试\n资源需求：暂无\n下周计划：发布")
    assert materialize_resource_demand(db_session, report) is False
    assert db_session.query(ReportItem).count() == 0
    assert db_session.query(Blocker).count() == 0


def test_placeholder_resource_demand_is_cleaned_on_rematerialize(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧版本物化过的占位候选，在宽口径生效后随回填/重复同步清除。

    生产存量噪音的修复路径就是它：改代码后跑一次
    ``POST /api/ai/queue/backfill``（或任意一次同内容重新同步），
    ReportItem / Task / Blocker 一并清掉，不需要单独的删除脚本。
    """
    report = _report(db_session, "placeholder-resource", "资源需求：Discuss later\n下周计划：继续跟进")
    monkeypatch.setattr(
        "app.services.structured_data_service.extract_resource_demand",
        lambda _content: "Discuss later",
    )
    assert materialize_resource_demand(db_session, report) is True
    # session 是 autoflush=False：_recalculate_task 里 add 的 Blocker 要显式
    # flush 才对后续查询可见。
    db_session.commit()
    blocker = db_session.query(Blocker).one()
    assert blocker.title == "资源需求：Discuss later"

    monkeypatch.undo()
    assert materialize_resource_demand(db_session, report) is False
    db_session.commit()
    assert db_session.query(Blocker).count() == 0
    assert db_session.query(Task).count() == 0
    assert db_session.query(ReportItem).count() == 0
    assert db_session.query(TaskReportLink).count() == 0


def test_model_channel_placeholder_blocker_is_not_linked(db_session: Session) -> None:
    """模型把「资源需求：Discuss Later」复述成卡点/风险时不建 Task/Blocker。

    直出层判空后，噪音会从模型通道换个标题回来（实测标题固定为
    「资源需求未明确」）；过滤按标题走与经营看板同一份哨兵表，
    事项本身仍落 ReportItem 备查。重放历史缓存即可清掉存量脏关联。
    """
    report = _report(db_session, "model-placeholder", "资源需求：Discuss Later\n下周计划：跟进")
    result = {
        "completed_items": [],
        "ongoing_items": [],
        "results": [],
        "blockers": [
            {"title": "资源需求未明确", "source_text": "资源需求：Discuss Later", "status": "待确认"},
            {"title": "交付物料存在返工可能", "source_text": "交付给院校的物料可能需要返工", "status": "待确认"},
        ],
        "problems": [],
        "dependencies": [],
        "next_week_plans": [],
        "risks": [
            {"title": "资源需求未明确", "source_text": "资源需求：Discuss Later", "level": "低"},
        ],
        "score": 60,
        "score_breakdown": {},
    }
    materialize_report_analysis(db_session, report, result, "test")
    db_session.commit()

    titles = {
        task.title
        for task in db_session.query(Task)
        .join(TaskReportLink, TaskReportLink.task_id == Task.id)
        .all()
    }
    # 占位复述不建关联；真实卡点照常保留
    assert titles == {"交付物料存在返工可能"}
    blocker = db_session.query(Blocker).one()
    assert blocker.title == "交付物料存在返工可能"


def test_model_materialization_preserves_resource_candidate_and_links_cross_week(
    db_session: Session,
) -> None:
    first = _report(db_session, "resource-week-1", "资源需求：申请部确认材料")
    second = _report(
        db_session,
        "resource-week-2",
        "资源需求：申请部确认材料",
        start=date(2026, 9, 7),
        end=date(2026, 9, 13),
    )
    materialize_resource_demand(db_session, first)
    materialize_resource_demand(db_session, second)
    result = {
        "completed_items": [],
        "ongoing_items": [],
        "results": [],
        "blockers": [
            {
                "title": "申请部确认材料",
                "description": "资源需求仍待确认",
                "source_text": "资源需求：申请部确认材料",
            }
        ],
        "problems": [],
        "dependencies": [],
        "next_week_plans": [],
        "risks": [],
        "score": 70,
        "score_breakdown": {},
    }
    materialize_report_analysis(db_session, second, result, "test")
    db_session.commit()

    assert db_session.query(ReportItem).count() == 3
    task = db_session.query(Task).one()
    assert task.duration_weeks == 2
    blocker = db_session.query(Blocker).one()
    assert blocker.status == "OPEN"
    assert blocker.occurrence_count == 2


def test_backfill_only_creates_missing_queue_and_resource_candidates(
    db_session: Session,
) -> None:
    existing = _report(db_session, "queued-resource", "资源需求：已有队列")
    missing = _report(db_session, "missing-resource", "资源需求：历史材料待确认")
    enqueue_report(db_session, existing)
    db_session.commit()
    existing_queue = db_session.query(AnalysisQueue).filter_by(report_id=existing.id).one()

    rows = enqueue_missing_period_reports(db_session, PERIOD_START, PERIOD_END)
    db_session.commit()
    assert [row.report_id for row in rows] == [missing.id]
    db_session.refresh(existing_queue)
    assert existing_queue.status == AnalysisQueueStatus.PENDING
    assert db_session.query(AnalysisQueue).count() == 2

    result = backfill_resource_demands(db_session, PERIOD_START, PERIOD_END)
    db_session.commit()
    assert result == {"scanned": 2, "matched": 2}
    assert db_session.query(ReportItem).count() == 2


def test_queue_backfill_api_is_explicit_and_does_not_reset_existing(
    client: TestClient,
    db_session: Session,
) -> None:
    existing = _report(db_session, "api-existing", "资源需求：已有待处理队列")
    missing = _report(db_session, "api-missing", "资源需求：历史材料待确认")
    enqueue_report(db_session, existing)
    db_session.commit()
    existing_queue = db_session.query(AnalysisQueue).filter_by(report_id=existing.id).one()
    existing_queue.attempts = 2
    db_session.commit()

    response = client.post(
        "/api/ai/queue/backfill",
        json={"start": "2026-08-31", "end": "2026-09-06"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["backfilled"] == 1
    assert body["resource_candidates"] == 2
    db_session.refresh(existing_queue)
    assert existing_queue.attempts == 2
    assert db_session.query(AnalysisQueue).filter_by(report_id=missing.id).count() == 1

    # 回填接口可重复调用，不增加重复事项。
    result_again = backfill_resource_demands(db_session, PERIOD_START, PERIOD_END)
    db_session.commit()
    assert result_again == {"scanned": 2, "matched": 2}
    assert db_session.query(ReportItem).count() == 2


def test_blocker_scopes_support_all_long_new_high_and_resolved(
    client: TestClient,
    db_session: Session,
) -> None:
    report = _report(db_session, "scope-resource", "资源需求：本期新增高风险材料")
    materialize_resource_demand(db_session, report)
    db_session.commit()
    blocker = db_session.query(Blocker).one()
    blocker.risk_level = "高"
    blocker.duration_weeks = 3
    # 模拟已升级的正式卡点（风险升高需重算才会生效，这里直接置状态）。
    blocker.status = "OPEN"
    db_session.commit()

    assert client.get("/api/blockers", params={"scope": "all"}).json()["items"]
    assert client.get(
        "/api/blockers", params={"scope": "long", "start": "2026-08-31", "end": "2026-09-06"}
    ).json()["items"][0]["id"] == blocker.id
    item = client.get(
        "/api/blockers", params={"scope": "open", "start": "2026-08-31", "end": "2026-09-06"}
    ).json()["items"][0]
    assert item["source_kind"] == "resource_need"
    assert item["source_field"] == "资源需求"
    assert item["pending_confirmation"] is True
    assert client.get(
        "/api/blockers", params={"scope": "new", "start": "2026-08-31", "end": "2026-09-06"}
    ).json()["items"][0]["id"] == blocker.id
    assert client.get("/api/blockers", params={"scope": "high"}).json()["items"][0]["id"] == blocker.id

    blocker.status = "RESOLVED"
    db_session.commit()
    assert client.get("/api/blockers", params={"scope": "resolved"}).json()["items"][0]["id"] == blocker.id


def test_blocker_weekly_ranges_follow_evidence_period(client: TestClient, db_session: Session) -> None:
    """周分类按卡点依据归属；旧问题重复出现不丢失，也不重复计数。"""
    old = _report(db_session, "old", "资源需求：等待预算审批",
                  start=date(2026, 8, 24), end=date(2026, 8, 30))
    last = _report(db_session, "last", "资源需求：等待预算审批")
    current = _report(db_session, "current", "资源需求：等待预算审批",
                      start=date(2026, 9, 7), end=date(2026, 9, 13))
    last_only = _report(db_session, "last-only", "资源需求：需要市场资料")
    daily = _report(db_session, "daily", "资源需求：日报独有问题")
    daily.report_type = "daily"
    for report in [old, last, current, last_only, daily]:
        materialize_resource_demand(db_session, report)
    db_session.commit()
    recurring = db_session.query(Blocker).filter(Blocker.occurrence_count == 3).one()
    assert recurring.duration_weeks == 3
    common = {"weekly_only": True, "scope": "open"}
    last_params = {**common, "start": "2026-08-31", "end": "2026-09-06"}
    current_params = {**common, "start": "2026-09-07", "end": "2026-09-13"}
    # last_only 单周低风险只进候选层，不再出现在 open 主列表。
    for params, expected in [(last_params, 1), (current_params, 1)]:
        response = client.get("/api/blockers", params=params)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == expected
        assert client.get("/api/blockers", params={**params, "count_only": True}).json() == {"total": expected}
        assert recurring.id in [item["id"] for item in items]
    last_items = client.get("/api/blockers", params=last_params).json()["items"]
    assert next(item for item in last_items if item["id"] == recurring.id)["sources"][0]["report_id"] == last.id
    # 历史证据所属周期可直接用来建立管理跟进，无需伪造成本周来源。
    follow_up = client.post("/api/management-actions", json={
        "source_type": "blocker", "source_id": recurring.id,
        "start": "2026-08-24", "end": "2026-08-30",
    })
    assert follow_up.status_code == 200, follow_up.text
    recurring.status = "RESOLVED"
    db_session.commit()
    assert client.get("/api/blockers", params={**last_params, "scope": "resolved"}).json()["items"][0]["id"] == recurring.id
    old.employee.report_exempt = True
    db_session.commit()
    assert client.get("/api/blockers", params={**last_params, "scope": "all"}).json()["items"] == []
    assert client.get("/api/blockers", params={**last_params, "count_only": True}).json() == {"total": 0}


def test_weekly_only_requires_blocker_evidence_in_period(client: TestClient, db_session: Session) -> None:
    """旧的普通工作事项不应被当作卡点依据。"""
    old = _report(db_session, "old-plan", "计划采购设备",
                  start=date(2026, 8, 24), end=date(2026, 8, 30))
    current = _report(db_session, "new-blocker", "资源需求：需要采购设备")
    materialize_resource_demand(db_session, current)
    task = db_session.query(Task).one()
    item = ReportItem(report_id=old.id, item_type="ongoing_items", item_order=0,
                      title="采购设备", source_text="计划采购设备")
    db_session.add(item)
    db_session.flush()
    db_session.add(TaskReportLink(task_id=task.id, report_id=old.id, report_item_id=item.id))
    db_session.commit()
    assert client.get("/api/blockers", params={"start": "2026-08-24", "end": "2026-08-30", "weekly_only": True}).json()["items"] == []
