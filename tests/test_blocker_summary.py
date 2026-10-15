"""周期卡点 AI 总结的非联网回归测试。"""

from __future__ import annotations

from datetime import date, datetime
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import deps
from app.ai.fake import FakeProvider
from app.core.config import get_settings
from app.main import create_app
from app.models.blocker import Blocker
from app.models.blocker_summary_queue import BlockerSummaryQueue
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AnalysisQueueStatus
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services import blocker_summary_service
from app.services.analysis_queue_service import run_worker_once
from app.services.blocker_summary_service import (
    blocker_summary_state,
    enqueue_blocker_summary,
    process_blocker_summary_item,
)

PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)


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


def _add_blocker_evidence(
    session: Session,
    *,
    title: str = "B2B合同签署持续为零",
    source_text: str = "本月B2B合同签署数为0，因两个代理商仍处在等待签约状态。",
    external_id: str = "blocker-summary-report",
) -> Blocker:
    department = Department(
        source="wecom", external_id=f"dept-{external_id}", name="商务部", status="active"
    )
    employee = Employee(
        external_id=f"employee-{external_id}",
        name="客户发展部Ruby",
        department=department,
        status="active",
        source="wecom",
    )
    report = WeeklyReport(
        employee=employee,
        external_id=external_id,
        report_type="weekly",
        title="本周周报",
        content=source_text,
        content_raw=source_text,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.flush()
    item = ReportItem(
        report_id=report.id,
        employee_id=employee.id,
        item_type="blockers",
        item_order=0,
        title=title,
        description="等待签约状态",
        status="未解决",
        source_text=source_text,
        risk_level="中",
    )
    task = Task(
        canonical_key=title,
        title=title,
        status="BLOCKED",
        first_seen_date=PERIOD_START,
        last_seen_date=PERIOD_END,
        duration_weeks=1,
    )
    session.add_all([item, task])
    session.flush()
    blocker = Blocker(
        task_id=task.id,
        title=title,
        description="等待签约状态",
        status="OPEN",
        risk_level="中",
        first_seen_date=PERIOD_START,
        last_seen_date=PERIOD_END,
        duration_weeks=1,
        occurrence_count=1,
    )
    session.add_all(
        [blocker, TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)]
    )
    session.commit()
    return blocker


def test_blocker_summary_empty_period(client: TestClient) -> None:
    """本期没有卡点时明确返回 empty，不假装有结论也不报错。"""
    response = client.get(
        "/api/blockers/period-summary",
        params={"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["blockers_total"] == 0


def test_blocker_summary_enqueue_process_and_state(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = _add_blocker_evidence(db_session)
    fake = FakeProvider(
        response="## 总体判断\n卡点集中在商务签约环节。\n## 管理建议\n- 推动代理商签约 [#1]"
    )
    monkeypatch.setattr(blocker_summary_service, "get_provider", lambda settings: fake)

    queued = enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    assert queued["status"] == AnalysisQueueStatus.PENDING
    assert queued["blockers_total"] == 1
    queue = db_session.query(BlockerSummaryQueue).one()
    assert queue.blocker_count == 1

    # 入队幂等：重复入队不新建任务行
    enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    assert db_session.query(BlockerSummaryQueue).count() == 1

    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()
    result = process_blocker_summary_item(queue.id)
    assert result["status"] == "success"
    assert result["processed_blockers"] == 1
    # 卡点量小，单次调用读完，没有分批路径
    assert len(fake.calls) == 1
    assert blocker.title in fake.calls[0]["user"]
    assert "提交人" in fake.calls[0]["user"]

    db_session.expire_all()
    state = blocker_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["status"] == AnalysisQueueStatus.SUCCESS
    assert state["summary"]
    assert state["processed_blockers"] == 1
    assert 1 in state["source_report_ids"]


def test_blocker_summary_get_is_read_only(db_session: Session) -> None:
    """轮询走 GET 不能重置队列，否则前端轮询等于反复重新生成。"""
    _add_blocker_evidence(db_session)
    enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    queue = db_session.query(BlockerSummaryQueue).one()
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()

    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/blockers/period-summary",
                params={
                    "start": PERIOD_START.isoformat(),
                    "end": PERIOD_END.isoformat(),
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == AnalysisQueueStatus.RUNNING
            assert body["blockers_total"] == 1
    finally:
        app.dependency_overrides.clear()

    db_session.expire_all()
    assert (
        db_session.query(BlockerSummaryQueue).one().status
        == AnalysisQueueStatus.RUNNING
    )


def test_blocker_summary_post_enqueues_pending(db_session: Session) -> None:
    """POST 只入队（状态 pending），模型调用交给后台 Worker。"""
    _add_blocker_evidence(db_session)
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/blockers/period-summary",
                json={"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
            )
            assert response.status_code == 200
            assert response.json()["status"] == AnalysisQueueStatus.PENDING
    finally:
        app.dependency_overrides.clear()

    assert db_session.query(BlockerSummaryQueue).one().status == (
        AnalysisQueueStatus.PENDING
    )


def test_blocker_summary_becomes_stale_when_blocker_changes(db_session: Session) -> None:
    """卡点标题/状态变化后旧结论过期，不再展示旧总结。"""
    blocker = _add_blocker_evidence(db_session)
    enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    blocker.status = "RESOLVED"
    db_session.commit()
    state = blocker_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["stale"] is True
    assert state["status"] == "stale"


def test_blocker_summary_excludes_exempt_employee_sources(db_session: Session) -> None:
    """免提交人员的周报来源不进入卡点总结。"""
    blocker = _add_blocker_evidence(db_session, external_id="blocker-summary-exempt")
    employee = (
        db_session.query(Employee).filter_by(external_id="employee-blocker-summary-exempt").one()
    )
    employee.report_exempt = True
    db_session.commit()
    # 唯一来源被豁免后，该周期没有可统计的卡点
    state = blocker_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["status"] == "empty"
    queued = enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    assert queued["status"] == "empty"
    assert db_session.query(BlockerSummaryQueue).count() == 0


def test_worker_consumes_blocker_summary_queue(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：POST 入队 → Worker 一轮消费 → GET 有结论。"""
    _add_blocker_evidence(db_session)
    fake = FakeProvider(response="## 总体判断\n商务签约卡点需介入。\n## 管理建议\n- 升级处理 [#1]")
    monkeypatch.setattr(blocker_summary_service, "get_provider", lambda settings: fake)

    enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    result = run_worker_once(concurrency=1)
    assert result["blocker_claimed"] == 1
    assert result["blocker_processed"] == 1
    assert result["failed"] == 0

    db_session.expire_all()
    state = blocker_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["status"] == AnalysisQueueStatus.SUCCESS
    assert state["summary"]
    assert len(fake.calls) == 1


def test_blocker_summary_requeues_when_blocker_changes_before_processing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入队到消费之间卡点变化：放回 pending 按最新数据处理，不用旧指纹出结论。"""
    blocker = _add_blocker_evidence(db_session)
    fake = FakeProvider(response="## 总体判断\n更新后的结论。")
    monkeypatch.setattr(blocker_summary_service, "get_provider", lambda settings: fake)
    enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    queue = db_session.query(BlockerSummaryQueue).one()
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()

    blocker.risk_level = "高"
    db_session.commit()
    assert process_blocker_summary_item(queue.id)["status"] == "requeued"
    assert fake.calls == []

    db_session.expire_all()
    queue = db_session.query(BlockerSummaryQueue).one()
    assert queue.status == AnalysisQueueStatus.PENDING
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()
    assert process_blocker_summary_item(queue.id)["status"] == "success"
    assert "高" in fake.calls[0]["user"]


# --------------------------------------------------------------------------- #
# 输入预算：这条路径没有分批回退，超限就是整轮失败，所以必须有上限且如实标注
# --------------------------------------------------------------------------- #


def _add_many_blockers(session: Session, count: int, *, sources_each: int = 1) -> None:
    """造 count 个互相独立的卡点，每个带 sources_each 篇周报依据。"""
    department = Department(
        source="wecom", external_id="dept-many", name="商务部", status="active"
    )
    employee = Employee(
        external_id="employee-many",
        name="张三",
        department=department,
        status="active",
        source="wecom",
    )
    session.add(employee)
    session.flush()
    for index in range(count):
        task = Task(
            canonical_key=f"task-{index}",
            title=f"卡点{index}",
            status="BLOCKED",
            first_seen_date=PERIOD_START,
            last_seen_date=PERIOD_END,
            duration_weeks=1,
        )
        session.add(task)
        session.flush()
        session.add(
            Blocker(
                task_id=task.id,
                title=f"卡点{index}",
                description="描述" * 40,
                status="OPEN",
                risk_level="中",
                first_seen_date=PERIOD_START,
                last_seen_date=PERIOD_END,
                duration_weeks=1,
                occurrence_count=1,
            )
        )
        for source_index in range(sources_each):
            report = WeeklyReport(
                employee=employee,
                external_id=f"many-report-{index}-{source_index}",
                report_type="weekly",
                title=f"周报{index}-{source_index}",
                content="正文",
                content_raw="正文",
                start_date=PERIOD_START,
                end_date=PERIOD_END,
                submitted_at=datetime(2026, 9, 10, 10, 0, 0),
                source="wecom",
            )
            session.add(report)
            session.flush()
            item = ReportItem(
                report_id=report.id,
                employee_id=employee.id,
                item_type="blockers",
                item_order=0,
                title=f"卡点{index}-{source_index}",
                source_text="依据" * 60,
                risk_level="中",
            )
            session.add(item)
            session.flush()
            session.add(
                TaskReportLink(
                    task_id=task.id, report_id=report.id, report_item_id=item.id
                )
            )
    session.commit()


def test_evidence_per_blocker_is_capped(db_session: Session) -> None:
    """单个卡点的原文依据必须有条数上限（按最近优先保留）。

    不设上限时，一个从年初挂到现在的卡点会把每一周的原文都拼进提示词，
    单条卡点就能吃掉几万字符。
    """
    blocker = _add_blocker_evidence(db_session)
    task_id = blocker.task_id
    employee = db_session.query(Employee).first()
    seen_ids: list[int] = []
    for index in range(8):
        report = WeeklyReport(
            employee=employee,
            external_id=f"evidence-{index}",
            report_type="weekly",
            title=f"第{index}周",
            content="正文",
            content_raw="正文",
            start_date=PERIOD_START,
            end_date=PERIOD_END,
            submitted_at=datetime(2026, 9, 10, 10, 0, 0),
            source="wecom",
        )
        db_session.add(report)
        db_session.flush()
        item = ReportItem(
            report_id=report.id,
            employee_id=employee.id,
            item_type="blockers",
            item_order=0,
            title=f"依据{index}",
            source_text=f"第{index}周的依据",
            risk_level="中",
        )
        db_session.add(item)
        db_session.flush()
        db_session.add(
            TaskReportLink(
                task_id=task_id, report_id=report.id, report_item_id=item.id
            )
        )
        seen_ids.append(report.id)
    db_session.commit()

    items = blocker_summary_service._blockers_for_period(db_session, PERIOD_START, PERIOD_END)
    target = next(item for item in items if item["id"] == blocker.id)
    assert len(target["sources"]) == blocker_summary_service.EVIDENCE_PER_BLOCKER_LIMIT


def test_blocker_context_respects_budget_and_reports_truncation() -> None:
    """上下文拼装受预算约束，且如实报告覆盖了多少条。"""
    items = [
        {
            "id": index,
            "title": f"卡点{index}",
            "description": "描述" * 20,
            "status": "OPEN",
            "risk_level": "高",
            "duration_weeks": 3,
            "occurrence_count": 3,
            "related_project": None,
            "updated_at": None,
            "sources": [],
        }
        for index in range(50)
    ]

    full = blocker_summary_service._blocker_context_text(items, 10_000_000)
    assert full["included"] == 50
    assert full["total"] == 50
    assert full["truncated"] is False

    tight = blocker_summary_service._blocker_context_text(items, 1000)
    assert tight["truncated"] is True
    assert 0 < tight["included"] < 50
    assert tight["chars"] <= 1000
    # 绝不静默截断：正文里只应出现被声明覆盖的那几条
    assert tight["text"].count("标题:") == tight["included"]


def test_blocker_summary_truncation_is_surfaced_to_the_caller(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超限时提示词与结论都必须如实说明「只覆盖了前 N 条」。

    回归守护：旧实现把「本周期全部 N 个卡点已完整提供」写死在提示词里，而正文
    其实可能被上游拒绝、或者根本装不下 —— 模型会据此把局部当全量下结论。
    """
    _add_many_blockers(db_session, 20)
    fake = FakeProvider(response="## 总体判断\n按风险处理。")
    monkeypatch.setattr(blocker_summary_service, "get_provider", lambda settings: fake)
    # 把预算压到配置允许的最小值，让 20 条卡点确实装不下（每条约 250 字符）
    monkeypatch.setattr(
        blocker_summary_service,
        "runtime_settings",
        lambda session: get_settings().model_copy(
            update={"ai_blocker_summary_context_chars": 2000}
        ),
    )

    enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    queue = db_session.query(BlockerSummaryQueue).one()
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()
    assert process_blocker_summary_item(queue.id)["status"] == "success"

    assert len(fake.calls) == 1
    prompt = fake.calls[0]["user"]
    assert len(prompt) < 2000 + 600, "提示词必须受预算约束，不能把全部卡点都塞进去"
    assert "其余因篇幅未提供" in prompt, "模型必须知道自己只看到了一部分"

    db_session.expire_all()
    state = blocker_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["status"] == AnalysisQueueStatus.SUCCESS
    assert state["truncated"] is True
    assert state["blockers_total"] == 20
    assert state["covered_blockers"] is not None
    assert state["covered_blockers"] < state["blockers_total"]
    assert state["context_chars"] is not None


def test_blocker_summary_reports_full_coverage_when_it_fits(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装得下时如实报告全量覆盖，不误报截断。"""
    _add_many_blockers(db_session, 3)
    fake = FakeProvider(response="## 总体判断\n全都看完了。")
    monkeypatch.setattr(blocker_summary_service, "get_provider", lambda settings: fake)

    enqueue_blocker_summary(db_session, PERIOD_START, PERIOD_END)
    queue = db_session.query(BlockerSummaryQueue).one()
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()
    assert process_blocker_summary_item(queue.id)["status"] == "success"

    assert "已完整提供" in fake.calls[0]["user"]

    db_session.expire_all()
    state = blocker_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["truncated"] is False
    assert state["covered_blockers"] == state["blockers_total"] == 3
