"""统一结构化分析 + 后台队列（P0 架构改造）。

验证：每篇只调一次模型、结果缓存不重复调用、内容变化失效重入队、
Worker 成功/失败路径、空正文零调用。全部用 FakeProvider，不联网。
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.base import LLMRateLimitError
from app.ai.fake import FakeProvider
from app.api import deps
from app.core.time_utils import business_date, utcnow
from app.main import create_app
from app.models.ai_analysis import AIAnalysis
from app.models.analysis_queue import AnalysisQueue
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus, AnalysisQueueStatus, EmployeeStatus
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.report_item import ReportItem
from app.models.report_score import ReportScore
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services import report_analysis_service
from app.services.analysis_queue_service import (
    enqueue_report,
    run_worker_once,
)
from app.services.report_analysis_service import (
    REPORT_ANALYSIS_TYPE,
    analyze_report,
    normalize_result,
    report_payload_hash,
)
from app.services import analysis_queue_service
from app.services.runtime_config_service import update_runtime_values

PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)


def test_saved_runtime_key_is_passed_to_report_provider(db_session, monkeypatch):
    report = _make_report(db_session)
    update_runtime_values(db_session, {"ai_api_key": "saved-runtime-key"})
    db_session.commit()
    fake = FakeProvider(
        response=_unified_response(),
        prompt_tokens=21,
        completion_tokens=13,
        prompt_cache_hit_tokens=8,
        prompt_cache_miss_tokens=13,
        http_request_count=2,
    )
    captured = {}

    def provider_for(settings):
        captured["api_key"] = settings.ai_api_key
        return fake

    monkeypatch.setattr(report_analysis_service, "get_provider", provider_for)

    report_analysis_service.analyze_report(db_session, report.id)

    assert captured["api_key"] == "saved-runtime-key"
    assert len(fake.calls) == 1
    assert fake.option_calls == [{"thinking": False, "json_mode": True}]
    analysis = db_session.scalar(
        select(AIAnalysis)
        .where(
            AIAnalysis.entity_type == "weekly_report",
            AIAnalysis.entity_id == report.id,
            AIAnalysis.analysis_type == REPORT_ANALYSIS_TYPE,
        )
    )
    assert analysis is not None
    assert analysis.prompt_tokens == 21
    assert analysis.completion_tokens == 13
    assert analysis.input_data["usage"] == {
        "model_call_count": 1,
        "http_request_count": 2,
        "prompt_cache_hit_tokens": 8,
        "prompt_cache_miss_tokens": 13,
    }


def test_sparse_analysis_items_normalize_to_complete_result_shape():
    result = normalize_result(
        {
            "blockers": [
                {
                    "title": "等待审批",
                    "description": "等待预算审批",
                    "status": "未解决",
                    "source_text": "预算仍待审批",
                }
            ],
            "risks": [
                {
                    "title": "延期风险",
                    "description": "交付时间可能延后",
                    "level": "高",
                    "source_text": "预计可能延期",
                    "actionable": False,
                    "existing_task_ref": "42",
                }
            ],
        }
    )

    blocker = result["blockers"][0]
    assert set(blocker) == set(report_analysis_service.ITEM_KEYS) | {
        "actionable",
        "existing_task_ref",
    }
    assert blocker["related_project"] == ""
    assert blocker["task_key"] == ""
    assert blocker["mentioned_people"] == []
    assert blocker["confidence"] is None
    assert blocker["actionable"] is True
    assert blocker["existing_task_ref"] is None

    risk = result["risks"][0]
    assert risk["actionable"] is False
    assert risk["existing_task_ref"] == 42


def test_empty_json_mode_response_is_recorded_as_failed_with_usage(
    db_session, monkeypatch
):
    from app.ai.base import LLMResponseError

    report = _make_report(db_session)
    fake = FakeProvider(
        response="",
        prompt_tokens=9,
        completion_tokens=0,
        prompt_cache_hit_tokens=6,
        prompt_cache_miss_tokens=3,
        http_request_count=1,
    )
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)

    with pytest.raises(LLMResponseError, match="模型返回为空"):
        analyze_report(db_session, report.id)

    analysis = db_session.scalar(
        select(AIAnalysis)
        .where(
            AIAnalysis.entity_type == "weekly_report",
            AIAnalysis.entity_id == report.id,
            AIAnalysis.analysis_type == REPORT_ANALYSIS_TYPE,
        )
    )
    assert analysis is not None
    assert analysis.status == AIAnalysisStatus.FAILED
    assert analysis.prompt_tokens == 9
    assert analysis.completion_tokens == 0
    assert analysis.input_data["usage"]["model_call_count"] == 1
    assert analysis.input_data["usage"]["prompt_cache_hit_tokens"] == 6


@pytest.mark.parametrize("department_exempt", [False, True])
def test_exempt_reports_are_archived_without_ai(client, db_session, monkeypatch, department_exempt):
    from app.core.config import get_settings
    from app.services.ai_scan_service import scan_reports
    from app.services.period_summary_service import enqueue_period_summary
    from app.services.quality_service import check_report_quality
    from app.services.duplicate_service import check_report_duplicate

    report = _make_report(db_session)
    employee = db_session.get(Employee, report.employee_id)
    if department_exempt:
        department = Department(source="wecom", external_id="exempt-dept", name="免提交部门")
        db_session.add(department)
        db_session.flush()
        employee.department_id = department.id
        monkeypatch.setenv("REPORT_EXEMPT_DEPARTMENTS", '["免提交部门"]')
        get_settings.cache_clear()
    else:
        employee.report_exempt = True
    db_session.commit()

    fake = FakeProvider(response=_unified_response())
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)
    assert enqueue_report(db_session, report, force=True) is None
    for analyze in (report_analysis_service.analyze_report, check_report_quality, check_report_duplicate):
        with pytest.raises(ValueError, match="免提交"):
            analyze(db_session, report.id)
    assert client.post(f"/api/ai/reports/{report.id}/analyze?force=true").status_code == 400
    assert client.post("/api/ai/duplicate/check", json={"report_id": report.id}).status_code == 400
    period = {"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()}
    assert client.post("/api/ai/scan", json={**period, "force": True}).json()["total"] == 0
    assert client.post("/api/ai/queue/backfill", json=period).json()["total"] == 0
    assert scan_reports(db_session, **period, force=True)["scanned"] == 0
    assert enqueue_period_summary(db_session, PERIOD_START, PERIOD_END)["status"] == "empty"
    from app.services.agent_tools import ToolContext, execute_tool

    ctx = ToolContext(db_session, PERIOD_START, PERIOD_END)
    searched = json.loads(execute_tool(ctx, "search_reports", '{"scope":"all"}'))
    assert searched["matched_total"] == 0
    # 已提供过 ID 后变更免提交，读取全文也必须重新检查。
    ctx.offered_report_ids.add(report.id)
    fetched = json.loads(execute_tool(ctx, "get_reports", json.dumps({"report_ids": [report.id]})))
    assert fetched["returned"] == 0
    assert db_session.query(WeeklyReport).count() == 1
    assert db_session.query(AIAnalysis).count() == 0
    assert fake.calls == []

    employee.report_exempt = False
    employee.department_id = None
    db_session.commit()
    assert client.post("/api/ai/scan", json=period).json()["queued"] == 1
    assert run_worker_once(concurrency=1)["processed"] == 1
    assert len(fake.calls) == 1


def test_pending_queue_pauses_after_exemption_and_resumes(db_session, monkeypatch):
    report = _make_report(db_session)
    queue = enqueue_report(db_session, report)
    employee = db_session.get(Employee, report.employee_id)
    employee.report_exempt = True
    db_session.commit()
    fake = FakeProvider(response=_unified_response())
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)
    assert run_worker_once(concurrency=1)["claimed"] == 0
    db_session.refresh(queue)
    assert queue.status == AnalysisQueueStatus.PENDING
    assert fake.calls == []
    employee.report_exempt = False
    db_session.commit()
    assert run_worker_once(concurrency=1)["processed"] == 1
    assert len(fake.calls) == 1


def test_exempt_content_change_is_marked_for_post_exemption_scan(db_session, monkeypatch):
    report = _make_report(db_session, content="免提交前的正文")
    fake = FakeProvider(response=_unified_response())
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)
    report_analysis_service.analyze_report(db_session, report.id)
    old_hash = report.analysis_payload_hash

    employee = db_session.get(Employee, report.employee_id)
    employee.report_exempt = True
    report.content = "免提交期间更新后的正文"
    report.content_raw = report.content
    db_session.commit()

    assert enqueue_report(db_session, report) is None
    db_session.commit()
    db_session.refresh(report)
    assert report.ai_checked_at is None
    assert report.analysis_payload_hash is None
    assert old_hash is not None
    # 原分析留痕不删除；只是把当前冗余状态标为待重新计算。
    assert db_session.query(AIAnalysis).count() == 1

    employee.report_exempt = False
    db_session.commit()
    from app.services.analysis_queue_service import enqueue_period_reports

    queued = enqueue_period_reports(
        db_session,
        start=PERIOD_START,
        end=PERIOD_END,
    )
    assert len(queued) == 1
    db_session.commit()
    assert run_worker_once(concurrency=1)["processed"] == 1
    assert len(fake.calls) == 2


def test_queue_api_hides_currently_exempt_reports(client, db_session):
    report = _make_report(db_session)
    enqueue_report(db_session, report)
    employee = db_session.get(Employee, report.employee_id)
    employee.report_exempt = True
    db_session.commit()

    payload = client.get("/api/ai/queue").json()
    assert payload["totals"] == {}
    assert payload["items"] == []


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


def _make_report(session: Session, content: str = "完成支付接口联调并提测") -> WeeklyReport:
    employee = Employee(
        source="wecom",
        external_id="emp_a",
        name="张三",
        status=EmployeeStatus.ACTIVE,
    )
    session.add(employee)
    session.flush()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id="rep_a",
        report_type="weekly",
        title="本周进展",
        content=content,
        content_raw=content,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.commit()
    return report


def _make_reports(session: Session, count: int, prefix: str = "cap") -> list[WeeklyReport]:
    """批量造 N 篇**互不重复**的周报。

    不能用 _make_report 循环：它固定用同一个 external_id，第二次就会撞唯一约束。
    """
    reports: list[WeeklyReport] = []
    for index in range(count):
        employee = Employee(
            source="wecom",
            external_id=f"{prefix}_emp_{index}",
            name=f"{prefix}员工{index}",
            status=EmployeeStatus.ACTIVE,
        )
        session.add(employee)
        session.flush()
        report = WeeklyReport(
            employee_id=employee.id,
            external_id=f"{prefix}_rep_{index}",
            report_type="weekly",
            title=f"{prefix}周报{index}",
            content=f"第 {index} 篇内容",
            content_raw=f"第 {index} 篇内容",
            start_date=PERIOD_START,
            end_date=PERIOD_END,
            submitted_at=datetime(2026, 9, 10, 10, 0, 0),
            source="wecom",
        )
        session.add(report)
        reports.append(report)
    session.commit()
    return reports


def _unified_response() -> str:
    return json.dumps(
        {
            "summary": "完成支付接口联调并提测，等待验收。",
            "score": 82,
            "is_thin": False,
            "thin_reason": "有具体进展",
            "is_duplicate": False,
            "duplicate_score": 8,
            "duplicate_reason": "与历史有实质差异",
            "completed_items": [
                {
                    "title": "支付接口联调",
                    "description": "完成与银行侧联调",
                    "status": "已完成",
                    "related_project": "支付中台",
                    "mentioned_people": ["李四"],
                    "confidence": 0.95,
                    "source_text": "完成支付接口联调并提测",
                }
            ],
            "ongoing_items": [],
            "results": [],
            "blockers": [],
            "problems": [],
            "dependencies": [],
            "next_week_plans": [],
            "risks": [],
        },
        ensure_ascii=False,
    )


def test_analyze_one_call_and_second_call_is_cached(
    db_session: Session, monkeypatch
) -> None:
    report = _make_report(db_session)
    fake = FakeProvider(response=_unified_response())
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)

    result = report_analysis_service.analyze_report(db_session, report.id)
    assert result["score"] == 82
    assert result["completed_items"][0]["title"] == "支付接口联调"
    assert len(fake.calls) == 1

    row = (
        db_session.query(AIAnalysis)
        .filter(
            AIAnalysis.entity_id == report.id,
            AIAnalysis.analysis_type == "report_analysis",
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
        )
        .one()
    )
    assert row.input_data["content_hash"]
    assert row.duration_ms is not None

    cached = report_analysis_service.analyze_report(db_session, report.id)
    assert cached["cached"] is True
    assert len(fake.calls) == 1  # 页面再次打开不再调模型
    assert fake.option_calls == [{"thinking": False, "json_mode": True}]


def test_empty_body_never_calls_model(db_session: Session, monkeypatch) -> None:
    report = _make_report(db_session, content="")
    report.title = ""
    db_session.commit()
    fake = FakeProvider(response="{}")
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)

    result = report_analysis_service.analyze_report(db_session, report.id)
    assert result["is_thin"] is True
    assert fake.calls == []


def test_worker_runs_queued_report(db_session: Session, monkeypatch) -> None:
    report = _make_report(db_session)
    enqueue_report(db_session, report)
    db_session.commit()
    assert (
        db_session.query(AnalysisQueue)
        .filter_by(report_id=report.id, status=AnalysisQueueStatus.PENDING)
        .one()
    )

    fake = FakeProvider(response=_unified_response())
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)
    result = run_worker_once(concurrency=1)
    assert result["processed"] == 1
    assert result["failed"] == 0

    db_session.refresh(report)
    assert report.ai_checked_at is not None
    assert report.ai_duplicate_flag is False
    queue = db_session.query(AnalysisQueue).filter_by(report_id=report.id).one()
    assert queue.status == AnalysisQueueStatus.SUCCESS
    analysis = db_session.query(AIAnalysis).filter_by(
        entity_id=report.id, analysis_type=REPORT_ANALYSIS_TYPE
    ).one()
    assert analysis.input_data["queue_attempt"] == 1
    assert analysis.input_data["usage"]["model_call_count"] == 1


def test_worker_does_not_mark_waiting_summary_jobs_running(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue claims fit executor capacity so RUNNING means a thread has started it."""
    report_started = threading.Event()
    summary_started = threading.Event()
    release_report = threading.Event()
    output: list[dict[str, object]] = []
    errors: list[BaseException] = []

    monkeypatch.setattr(analysis_queue_service, "_summary_queue_rotation", 0)
    monkeypatch.setattr(
        analysis_queue_service,
        "_claim_batch",
        lambda session, limit: list(range(100, 100 + limit)),
    )
    for name, queue_id in (
        ("_claim_period_summary_batch", 201),
        ("_claim_blocker_summary_batch", 202),
        ("_claim_operating_summary_batch", 203),
    ):
        monkeypatch.setattr(
            analysis_queue_service,
            name,
            lambda session, limit, queue_id=queue_id: [queue_id] if limit else [],
        )

    def hold_report(queue_id: int) -> dict[str, str]:
        report_started.set()
        if not release_report.wait(timeout=5):
            raise TimeoutError("test did not release report worker")
        return {"queue_id": str(queue_id), "status": "success"}

    def start_summary(queue_id: int) -> dict[str, str]:
        summary_started.set()
        return {"queue_id": str(queue_id), "status": "success"}

    monkeypatch.setattr(analysis_queue_service, "process_queue_item", hold_report)
    from app.services import (
        blocker_summary_service,
        operating_summary_service,
        period_summary_service,
    )

    monkeypatch.setattr(blocker_summary_service, "process_blocker_summary_item", start_summary)
    monkeypatch.setattr(operating_summary_service, "process_summary_item", start_summary)
    monkeypatch.setattr(period_summary_service, "process_period_summary_item", start_summary)

    def run_worker() -> None:
        try:
            output.append(run_worker_once(concurrency=3))
        except BaseException as exc:  # keep thread failures visible to the test
            errors.append(exc)

    thread = threading.Thread(target=run_worker)
    thread.start()
    try:
        assert report_started.wait(timeout=3)
        assert summary_started.wait(timeout=3), (
            "summary queue was claimed RUNNING but waited behind all report threads"
        )
    finally:
        release_report.set()
        thread.join(timeout=6)

    assert not thread.is_alive()
    assert errors == []
    assert len(output) == 1
    result = output[0]
    assert result["claimed"] + result["period_claimed"] + result["blocker_claimed"] + result[
        "operating_claimed"
    ] == 3


def test_worker_failure_is_recorded_and_retries(
    db_session: Session, monkeypatch
) -> None:
    report = _make_report(db_session)
    enqueue_report(db_session, report)
    db_session.commit()

    class FailingProvider:
        def chat_with_options(
            self, system, user, *, thinking=None, json_mode=None
        ):
            raise LLMRateLimitError(
                "模型限流",
                http_request_count=3,
                prompt_tokens=20,
                completion_tokens=0,
                prompt_cache_hit_tokens=12,
                prompt_cache_miss_tokens=8,
            )

    monkeypatch.setattr(
        report_analysis_service, "get_provider", lambda settings: FailingProvider()
    )
    result = run_worker_once(concurrency=1)
    assert result["failed"] == 1

    queue = db_session.query(AnalysisQueue).filter_by(report_id=report.id).one()
    assert queue.attempts == 1
    # 未到最大次数：回到 pending 并退避，而不是永远失败
    assert queue.status == AnalysisQueueStatus.PENDING
    assert queue.next_attempt_at is not None
    assert queue.last_error
    failure = db_session.query(AIAnalysis).filter_by(
        entity_id=report.id,
        analysis_type=REPORT_ANALYSIS_TYPE,
        status=AIAnalysisStatus.FAILED,
    ).one()
    assert failure.input_data["queue_attempt"] == 1
    assert failure.input_data["usage"] == {
        "model_call_count": 1,
        "http_request_count": 3,
        "prompt_cache_hit_tokens": 12,
        "prompt_cache_miss_tokens": 8,
    }
    assert failure.prompt_tokens == 20
    assert failure.completion_tokens == 0


def test_content_change_resets_queue_and_cache(
    db_session: Session, monkeypatch
) -> None:
    report = _make_report(db_session, content="第一版内容")
    fake = FakeProvider(response=_unified_response())
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)
    report_analysis_service.analyze_report(db_session, report.id)
    old_hash = report.analysis_payload_hash

    report.content = "正文被员工修改后的第二版"
    db_session.commit()
    row = enqueue_report(db_session, report)
    db_session.commit()

    db_session.refresh(report)
    assert report.ai_checked_at is None
    assert report.analysis_payload_hash is None
    assert row.status == AnalysisQueueStatus.PENDING
    assert row.payload_hash == report_analysis_service.report_payload_hash(report)
    assert row.payload_hash != old_hash


def test_scan_enqueues_and_detail_reads_stored_analysis(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """API 批量入口只入队；页面详情从库里读已生成分析，而不是现场调模型。"""
    report = _make_report(db_session)
    fake = FakeProvider(response=_unified_response())
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)

    resp = client.post(
        "/api/ai/scan",
        json={"start": "2026-09-07", "end": "2026-09-13", "force": False},
    )
    assert resp.status_code == 200
    assert resp.json()["queued"] == 1
    assert fake.calls == []  # 入队不等于现场调用模型

    result = run_worker_once(concurrency=1)
    assert result["processed"] == 1

    db_session.expire_all()  # Worker 在独立 Session 写库，测试会话需重读
    detail = client.get(f"/api/reports/{report.id}").json()
    assert detail["analysis"]["result"]["score"] == 82
    assert detail["analysis_queue"]["status"] == "success"


def test_scan_repairs_success_queue_without_current_analysis(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """The monthly dashboard's missing analysis must not be skipped as a queue success."""
    from app.services.operating_summary_service import monthly_analysis_readiness

    missing, complete = _make_reports(db_session, 2, prefix="scan_gap")
    missing_hash = report_payload_hash(missing)
    complete_hash = report_payload_hash(complete)
    for report, current_hash in ((missing, missing_hash), (complete, complete_hash)):
        report.ai_checked_at = utcnow()
        report.analysis_payload_hash = current_hash
        db_session.add(AnalysisQueue(
            report_id=report.id, payload_hash=current_hash,
            status=AnalysisQueueStatus.SUCCESS,
        ))
    db_session.add(AIAnalysis(
        entity_type="weekly_report", entity_id=complete.id,
        analysis_type=REPORT_ANALYSIS_TYPE, status=AIAnalysisStatus.SUCCESS,
        input_data={"content_hash": complete_hash}, result_json={},
    ))
    db_session.commit()

    report_ids = [missing.id, complete.id]
    assert monthly_analysis_readiness(db_session, report_ids)["analysis_completed"] == 1
    response = client.post(
        "/api/ai/scan",
        json={"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
    )
    assert response.status_code == 200
    assert response.json()["queued"] == 1
    db_session.expire_all()
    assert db_session.scalar(
        select(AnalysisQueue.status).where(AnalysisQueue.report_id == missing.id)
    ) == AnalysisQueueStatus.PENDING
    assert db_session.scalar(
        select(AnalysisQueue.status).where(AnalysisQueue.report_id == complete.id)
    ) == AnalysisQueueStatus.SUCCESS

    fake = FakeProvider(response=_unified_response())
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)
    assert run_worker_once(concurrency=1)["processed"] == 1
    db_session.expire_all()
    assert monthly_analysis_readiness(db_session, report_ids)["analysis_completed"] == 2
    assert client.post(
        "/api/ai/scan",
        json={"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
    ).json()["queued"] == 0

def test_structured_results_create_cross_week_task_and_resolved_blocker(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """P1：同一 task_key 跨周聚合，后续完成后卡点必须变为已解决。"""
    first = _make_report(db_session, content="等待深圳大学确认申请材料")
    second = WeeklyReport(
        employee_id=first.employee_id,
        external_id="rep_b",
        report_type="weekly",
        title="下周进展",
        content="深圳大学已确认申请材料",
        content_raw="深圳大学已确认申请材料",
        start_date=date(2026, 9, 14),
        end_date=date(2026, 9, 20),
        submitted_at=datetime(2026, 9, 18, 10, 0, 0),
        source="wecom",
    )
    db_session.add(second)
    db_session.commit()

    waiting = json.loads(_unified_response())
    waiting["completed_items"] = []
    waiting["ongoing_items"] = [
        {
            "title": "深圳大学材料确认",
            "description": "持续跟进学校确认",
            "status": "进行中",
            "related_project": "深圳大学申请",
            "task_key": "深圳大学材料确认",
            "mentioned_people": [],
            "confidence": 0.9,
            "source_text": "等待深圳大学确认申请材料",
        }
    ]
    waiting["blockers"] = [
        {
            "title": "深圳大学材料确认",
            "description": "等待学校回复",
            "status": "未解决",
            "related_project": "深圳大学申请",
            "task_key": "深圳大学材料确认",
            "mentioned_people": [],
            "confidence": 0.9,
            "source_text": "等待深圳大学确认申请材料",
        }
    ]
    waiting["score_breakdown"] = {
        "completeness": 12,
        "specificity": 16,
        "results": 8,
        "advancement": 12,
        "planning": 8,
        "problem_quality": 4,
        "duplicate": 9,
        "reason": "有明确事项和问题。",
    }
    completed = json.loads(json.dumps(waiting))
    completed["ongoing_items"] = []
    completed["blockers"] = []
    completed["completed_items"] = [
        {
            "title": "深圳大学材料确认",
            "description": "学校已确认材料",
            "status": "已完成",
            "related_project": "深圳大学申请",
            "task_key": "深圳大学材料确认",
            "mentioned_people": [],
            "confidence": 0.95,
            "source_text": "深圳大学已确认申请材料",
        }
    ]

    fake = FakeProvider(response=json.dumps(waiting, ensure_ascii=False))
    monkeypatch.setattr(report_analysis_service, "get_provider", lambda settings: fake)
    report_analysis_service.analyze_report(db_session, first.id)
    overview = client.get(
        "/api/dashboard/overview?start=2026-09-07&end=2026-09-13"
    )
    assert overview.status_code == 200
    assert overview.json()["work_item_in_progress"] == 1
    # 单周低风险卡点只进候选层，管理简报只统计正式（OPEN）卡点。
    assert overview.json()["blocker_total"] == 0

    fake.response = json.dumps(completed, ensure_ascii=False)
    report_analysis_service.analyze_report(db_session, second.id)

    assert db_session.query(ReportItem).count() == 3
    score = db_session.query(ReportScore).filter_by(report_id=first.id).one()
    assert score.completeness == 12
    task = db_session.query(Task).filter_by(canonical_key="深圳大学材料确认").one()
    assert task.duration_weeks == 2
    assert task.status == "COMPLETED"
    blocker = db_session.query(Blocker).filter_by(task_id=task.id).one()
    assert blocker.status == "RESOLVED"
    assert blocker.duration_weeks == 1

    listing = client.get("/api/blockers?status=RESOLVED")
    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert item["occurrence_count"] == 1
    assert {source["report_id"] for source in item["sources"]} == {first.id, second.id}


def test_p2_management_views_use_structured_data_and_keep_sources(
    client: TestClient, db_session: Session
) -> None:
    """P2 不调模型：总览、洞察和员工页均从结构化投影读取，并可回看来源。"""
    report = _make_report(db_session)
    department = Department(source="wecom", external_id="2", name="技术部", status="active")
    db_session.add(department)
    db_session.flush()
    employee = db_session.get(Employee, report.employee_id)
    assert employee is not None
    employee.department_id = department.id
    done = ReportItem(
        report_id=report.id, employee_id=employee.id, item_type="completed_items", item_order=0,
        title="完成接口联调", description="已完成验收准备", status="已完成",
        source_text="完成支付接口联调并提测",
    )
    blocked = ReportItem(
        report_id=report.id, employee_id=employee.id, item_type="blockers", item_order=0,
        title="等待外部验收", description="等待验收方回复", status="未解决",
        source_text="等待外部验收确认",
    )
    task = Task(
        canonical_key="等待外部验收", title="等待外部验收", status="BLOCKED",
        first_seen_date=date(2026, 8, 24), last_seen_date=PERIOD_END, duration_weeks=3,
    )
    db_session.add_all([done, blocked, task])
    db_session.flush()
    db_session.add_all([
        TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=blocked.id),
        Blocker(task_id=task.id, title="等待外部验收", status="OPEN", risk_level="中",
                first_seen_date=date(2026, 8, 24), last_seen_date=PERIOD_END,
                duration_weeks=3, occurrence_count=3),
        ReportScore(report_id=report.id, total_score=82, completeness=12),
    ])
    # 历史周的卡点不应混入本周概览。
    old_report = WeeklyReport(
        employee_id=employee.id, external_id="rep_old", report_type="weekly",
        title="历史周报", content="历史卡点", content_raw="历史卡点",
        start_date=date(2026, 8, 24), end_date=date(2026, 8, 30),
        submitted_at=datetime(2026, 8, 28, 10, 0, 0), source="wecom",
    )
    db_session.add(old_report)
    db_session.flush()
    old_item = ReportItem(
        report_id=old_report.id, employee_id=employee.id, item_type="blockers", item_order=0,
        title="历史卡点", status="未解决", source_text="历史卡点",
    )
    old_task = Task(
        canonical_key="历史卡点", title="历史卡点", status="BLOCKED",
        first_seen_date=date(2026, 8, 3), last_seen_date=date(2026, 8, 30), duration_weeks=4,
    )
    db_session.add_all([old_item, old_task])
    db_session.flush()
    db_session.add_all([
        TaskReportLink(task_id=old_task.id, report_id=old_report.id, report_item_id=old_item.id),
        Blocker(task_id=old_task.id, title="历史卡点", status="OPEN", risk_level="低",
                first_seen_date=date(2026, 8, 3), last_seen_date=date(2026, 8, 30),
                duration_weeks=4, occurrence_count=4),
    ])
    db_session.commit()

    overview = client.get("/api/dashboard/overview", params={"start": "2026-09-07", "end": "2026-09-13"})
    assert overview.status_code == 200
    assert overview.json()["work_item_done"] == 1
    assert overview.json()["blocker_total"] == 1
    assert overview.json()["analysis_coverage"] == {"reports_total": 1, "analyzed_reports": 1, "rate": 100.0}
    assert overview.json()["top_items"][0]["sources"][0]["report_id"] == report.id
    period_comparison = overview.json()["period_comparison"]
    assert period_comparison["previous_period"] == {
        "start": "2026-08-31",
        "end": "2026-09-06",
    }
    assert period_comparison["current"] == {
        "completed": 1,
        "in_progress": 0,
        "blockers": 1,
        "high_risk_blockers": 0,
    }
    assert period_comparison["previous"] == {
        "completed": 0,
        "in_progress": 0,
        "blockers": 0,
        "high_risk_blockers": 0,
    }
    assert period_comparison["deltas"] == {
        "completed": 1,
        "in_progress": 0,
        "blockers": 1,
        "high_risk_blockers": 0,
    }
    action = overview.json()["action_items"][0]
    assert action["kind"] == "blocker"
    assert action["title"] == "等待外部验收"
    assert action["risk_level"] == "中"
    assert action["duration_weeks"] == 3
    assert action["source_count"] == 1
    assert action["target_href"].startswith("/blockers/")
    assert action["departments"] == ["技术部"]
    assert client.get("/api/reports", params={"start": "2026-09-07", "end": "2026-09-13", "work_state": "done"}).json()["total"] == 1
    assert client.get("/api/reports", params={"start": "2026-09-07", "end": "2026-09-13", "work_state": "ongoing"}).json()["total"] == 0

    result = client.get("/api/insights", params={"start": "2026-09-07", "end": "2026-09-13"})
    assert result.status_code == 200
    long_blocker = next(item for item in result.json()["items"] if item["title"] == "长期未解决：等待外部验收")
    blocker = db_session.query(Blocker).filter_by(task_id=task.id).one()
    assert long_blocker["blocker_id"] == blocker.id
    assert long_blocker["title"] == "长期未解决：等待外部验收"
    assert long_blocker["sources"][0]["report_id"] == report.id
    assert any(item["title"] == "长期未解决：历史卡点" for item in result.json()["items"])
    assert next(item for item in overview.json()["insights"] if item["kind"] == "long_blocker")["blocker_id"] == blocker.id
    assert all(item["title"] != "长期未解决：历史卡点" for item in overview.json()["insights"])
    detail = client.get(f"/api/blockers/{blocker.id}", params={"start": "2026-09-07", "end": "2026-09-13"})
    assert detail.status_code == 200
    assert detail.json()["title"] == "等待外部验收"
    assert detail.json()["sources"][0]["report_id"] == report.id
    assert client.get("/api/blockers", params={"start": "2026-09-07", "end": "2026-09-13"}).json()["items"][0]["title"] == "等待外部验收"

    employee_result = client.get(f"/api/employees/{employee.id}/analysis", params={"end": "2026-09-13", "weeks": 8})
    assert employee_result.status_code == 200
    assert employee_result.json()["current_blockers"][0]["sources"][0]["report_id"] == report.id

    department_result = client.get(f"/api/departments/{department.id}", params={"start": "2026-09-07", "end": "2026-09-13", "weeks": 1})
    assert department_result.status_code == 200
    assert department_result.json()["analysis"]["counts"]["completed"] == 1

    # 部门温度必须跟随所选周期；没有该周期周报时，历史开放卡点不可冒出。
    dashboard_departments = client.get(
        "/api/dashboard/departments",
        params={"start": "2026-09-14", "end": "2026-09-20"},
    )
    assert dashboard_departments.status_code == 200
    department_snapshot = next(
        item for item in dashboard_departments.json()["items"]
        if item["id"] == department.id
    )
    assert department_snapshot["analysis_coverage"]["rate"] == 0
    assert department_snapshot["blocker_total"] == 0
    assert department_snapshot["department_status"] == "正常"


# --------------------------------------------------------------------------- #
# 正文口径与预算：喂给模型的必须是清洗后的正文，且必须受预算约束
# --------------------------------------------------------------------------- #


def _rich_report(session: Session, raw_html: str, cleaned: str) -> WeeklyReport:
    employee = Employee(
        source="wecom",
        external_id="body-emp",
        name="正文测试员",
        status=EmployeeStatus.ACTIVE,
    )
    session.add(employee)
    session.flush()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id="body-report",
        report_type="weekly",
        title="本周工作汇报",
        content=cleaned,
        content_raw=raw_html,
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        submitted_at=datetime(2026, 9, 13, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.commit()
    return report


def test_analysis_prompt_uses_cleaned_body_not_raw_html(
    db_session: Session, monkeypatch
) -> None:
    """提示词里必须是清洗后的正文，不能把富文本标签塞进上下文。

    ``content_raw`` 实测能把同一周期撑到 27 万字符（表格 style 就占几万），
    喂 raw 既贵 5 倍 token 又更容易淹没有效信息。
    """
    raw_html = '<table style="width:1496px"><tr><td>完成验收</td></tr></table>'
    report = _rich_report(db_session, raw_html, "完成验收")
    captured: dict[str, str] = {}

    class _Provider:
        def chat(self, system: str, user: str):
            captured["user"] = user
            from app.ai.base import LLMResult

            return LLMResult(
                content=json.dumps(
                    {
                        "summary": "完成验收",
                        "completed": [],
                        "in_progress": [],
                        "blockers": [],
                        "risks": [],
                        "next_week_plans": [],
                        "mentioned_people": [],
                        "quality_flags": [],
                        "duplicate": {"is_duplicate": False, "score": 0, "reason": ""},
                        "quality": {"is_thin": False, "score": 80, "reason": ""},
                        "confidence": 0.9,
                    }
                ),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
            )

    monkeypatch.setattr(
        "app.services.report_analysis_service.get_provider", lambda s: _Provider()
    )
    analyze_report(db_session, report.id, force=True)

    assert "完成验收" in captured["user"]
    assert "style=" not in captured["user"], "富文本标签不应出现在提示词里"
    assert "1496px" not in captured["user"]


def test_payload_hash_ignores_raw_html_reflow(db_session: Session) -> None:
    """raw HTML 仅仅重排（样式顺序变化）不应判定为「内容有变」。

    否则一次无意义的重排会清掉已成功的 AI 结论、把 ai_checked_at 置空，
    并重新计费调用模型。
    """
    first_raw = '<p style="width:100px;color:red">完成验收</p>'
    second_raw = '<p style="color:red;width:100px">完成验收</p>'
    report = _rich_report(db_session, first_raw, "完成验收")
    before = report_payload_hash(report)

    report.content_raw = second_raw
    db_session.commit()
    assert report_payload_hash(report) == before

    # 清洗后正文真的变了，必须换指纹
    report.content = "完成验收并已上线"
    db_session.commit()
    assert report_payload_hash(report) != before


def test_oversized_body_is_truncated_and_marked(
    db_session: Session, monkeypatch
) -> None:
    """单篇超长正文必须被预算截断，并在提示词里如实标注。

    旧实现只截历史、不截正文：单篇超长会把请求撑爆 -> 报错 -> 按重试次数
    反复计费；而静默截断会让模型把半篇当成完整正文。
    """
    monkeypatch.setenv("AI_MAX_INPUT_CHARS", "200")
    from app.core.config import get_settings

    get_settings.cache_clear()
    long_body = "很长的正文。" * 200
    report = _rich_report(db_session, long_body, long_body)
    captured: dict[str, str] = {}

    class _Provider:
        def chat(self, system: str, user: str):
            captured["user"] = user
            from app.ai.base import LLMResult

            return LLMResult(
                content=json.dumps(
                    {
                        "summary": "s",
                        "completed": [],
                        "in_progress": [],
                        "blockers": [],
                        "risks": [],
                        "next_week_plans": [],
                        "mentioned_people": [],
                        "quality_flags": [],
                        "duplicate": {"is_duplicate": False, "score": 0, "reason": ""},
                        "quality": {"is_thin": False, "score": 80, "reason": ""},
                        "confidence": 0.9,
                    }
                ),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
            )

    monkeypatch.setattr(
        "app.services.report_analysis_service.get_provider", lambda s: _Provider()
    )
    result = analyze_report(db_session, report.id, force=True)

    assert len(captured["user"]) < 1000, "超长正文必须被截断"
    assert "已按单篇预算截断" in captured["user"], "截断必须如实标注"

    row = db_session.scalar(
        select(AIAnalysis).where(
            AIAnalysis.entity_type == "weekly_report",
            AIAnalysis.entity_id == report.id,
            AIAnalysis.analysis_type == REPORT_ANALYSIS_TYPE,
        )
    )
    assert row is not None
    assert row.result_json["summary"] == result["summary"]
    assert row.status == AIAnalysisStatus.SUCCESS


def test_scan_respects_max_reports_cap(db_session, monkeypatch) -> None:
    """一键分析必须受 ai_scan_max_reports 限制，并如实报告还剩多少篇。

    每个排队项都是一次真实模型调用，不限量的话拉宽区间点一下就能把整月预算
    烧穿 —— 该配置此前只被旧的 ai_scan_service 读取，而一键分析走的是
    enqueue_period_reports，护栏形同虚设。
    """
    from app.core.config import get_settings
    from app.services.analysis_queue_service import enqueue_period_reports

    _make_reports(db_session, 8, prefix="cap")

    # 直接验证服务层上限：limit=3 时只能排 3 篇
    rows = enqueue_period_reports(
        db_session, PERIOD_START, PERIOD_END, force=True, limit=3
    )
    db_session.commit()
    assert len(rows) == 3, "单次排队必须受 limit 约束"

    # 接口层同样带上 remaining，让用户知道只排了一部分
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            body = client.post(
                "/api/ai/scan",
                json={
                    "start": PERIOD_START.isoformat(),
                    "end": PERIOD_END.isoformat(),
                    "force": True,
                },
            ).json()
        assert "remaining" in body
        assert body["limited"] is (body["remaining"] > 0)
    finally:
        app.dependency_overrides.clear()


def test_scan_repeated_clicks_advance_past_cap(db_session) -> None:
    """重复点击「分析本周期」必须能推进到补齐，而不是永远检视同一批。

    曾为真实缺陷：每次点击都按同一顺序检视前 limit 篇，已在队列/已分析的
    也占上限，第 limit+1 篇之后的永远排不上 —— 前端「请再次点击继续」
    形同虚设。现在已在队列/已分析成功的篇目直接跳过且不占上限。
    """
    from app.services.analysis_queue_service import (
        enqueue_period_reports_with_remaining,
    )

    _make_reports(db_session, 8, prefix="advance")

    expected_chunks = [3, 3, 2]
    expected_remaining = [5, 2, 0]
    for chunk, exp_remaining in zip(expected_chunks, expected_remaining):
        rows, remaining = enqueue_period_reports_with_remaining(
            db_session, PERIOD_START, PERIOD_END, limit=3
        )
        db_session.commit()
        assert len(rows) == chunk
        assert remaining == exp_remaining

    # 全部入队后再点：不重复入队，也没有「未检视」残留。
    rows, remaining = enqueue_period_reports_with_remaining(
        db_session, PERIOD_START, PERIOD_END, limit=3
    )
    db_session.commit()
    assert rows == []
    assert remaining == 0
    assert db_session.scalar(select(func.count()).select_from(AnalysisQueue)) == 8


def test_coverage_endpoint_reports_gap_and_department_scope(
    client: TestClient, db_session: Session
) -> None:
    """/api/ai/coverage 与洞察共用口径：缺口如实透出，支持部门过滤。"""
    reports = _make_reports(db_session, 2, prefix="cov")
    department = Department(
        source="wecom", external_id="42", name="市场部", status="active"
    )
    db_session.add(department)
    db_session.flush()
    employee = db_session.get(Employee, reports[0].employee_id)
    assert employee is not None
    employee.department_id = department.id
    db_session.add(
        ReportItem(
            report_id=reports[0].id,
            employee_id=employee.id,
            item_type="completed_items",
            item_order=0,
            title="完成联调",
            description="已完成",
            status="已完成",
            source_text="完成联调",
        )
    )
    db_session.commit()

    query = f"start={PERIOD_START.isoformat()}&end={PERIOD_END.isoformat()}"
    body = client.get(f"/api/ai/coverage?{query}").json()
    assert body["reports_total"] == 2
    assert body["analyzed_reports"] == 1
    assert body["remaining"] == 1
    assert body["queue"] == {"pending": 0, "running": 0, "failed": 0}

    scoped = client.get(f"/api/ai/coverage?{query}&department_id={department.id}").json()
    assert scoped["reports_total"] == 1
    assert scoped["analyzed_reports"] == 1
    assert scoped["remaining"] == 0

    # 员工维度：只统计该员工的周报（员工页「近 8 周」按钮的口径）。
    other = db_session.get(Employee, reports[1].employee_id)
    assert other is not None
    by_employee = client.get(f"/api/ai/coverage?{query}&employee_id={other.id}").json()
    assert by_employee["reports_total"] == 1
    assert by_employee["analyzed_reports"] == 0
    assert by_employee["remaining"] == 1


def test_scan_with_employee_id_only_enqueues_that_employee(db_session) -> None:
    """员工页补分析只入队该员工的周报，不波及同周期其他人。"""
    from app.services.analysis_queue_service import enqueue_period_reports

    reports = _make_reports(db_session, 2, prefix="emp_scope")
    rows = enqueue_period_reports(
        db_session,
        PERIOD_START,
        PERIOD_END,
        employee_id=reports[0].employee_id,
    )
    db_session.commit()
    assert len(rows) == 1
    assert rows[0].report_id == reports[0].id


def test_scan_default_cap_matches_config(db_session, monkeypatch) -> None:
    """不传 limit 时，默认上限必须来自配置，而不是「不限量」。"""
    from app.core.config import get_settings

    monkeypatch.setenv("AI_SCAN_MAX_REPORTS", "2")
    get_settings.cache_clear()
    try:
        _make_reports(db_session, 5, prefix="cfg")

        from app.services.analysis_queue_service import enqueue_period_reports

        rows = enqueue_period_reports(
            db_session, PERIOD_START, PERIOD_END, force=True
        )
        db_session.commit()
        assert len(rows) == 2, "默认上限必须读 ai_scan_max_reports"
    finally:
        get_settings.cache_clear()


def test_scan_reports_remaining_count(db_session, monkeypatch) -> None:
    """remaining 必须是「区间内匹配总数 - 本轮已排队数」，不能恒为 0。"""
    monkeypatch.setenv("AI_SCAN_MAX_REPORTS", "3")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        _make_reports(db_session, 7, prefix="rem")

        from app.services.analysis_queue_service import (
            enqueue_period_reports_with_remaining,
        )

        rows, remaining = enqueue_period_reports_with_remaining(
            db_session, PERIOD_START, PERIOD_END, force=True
        )
        db_session.commit()
        assert len(rows) == 3
        assert remaining == 4
    finally:
        get_settings.cache_clear()


def test_worker_once_only_backfills_when_asked(db_session, monkeypatch) -> None:
    """常规轮询不能每轮都跑历史投影。

    它是数据修复动作（反连接 + 最多 100 篇重物化），旧实现挂在每 3 秒轮询里，
    等于常驻后台负载，且拿不到 ReportScore 的周报会被无限重复物化。
    """
    from app.services import structured_data_service
    from app.services.analysis_queue_service import run_worker_once

    calls: list[int] = []
    original = structured_data_service.backfill_cached_analyses

    def _spy(session, limit: int = 100) -> int:
        calls.append(limit)
        return original(session, limit=limit)

    monkeypatch.setattr(
        structured_data_service, "backfill_cached_analyses", _spy
    )

    # 默认（常规轮询）：不该触发
    run_worker_once(concurrency=1)
    assert calls == [], "常规轮询不应每轮都跑历史投影"

    # 显式要求（worker --once）：应当触发
    run_worker_once(concurrency=1, run_backfill=True)
    assert len(calls) == 1, "显式要求时必须执行历史投影"


# --------------------------------------------------------------------------- #
# P1 语义去重 + P2 actionable 降噪
# --------------------------------------------------------------------------- #


def _materialize_result(**overrides) -> dict:
    base = {
        "completed_items": [],
        "ongoing_items": [],
        "results": [],
        "blockers": [],
        "problems": [],
        "dependencies": [],
        "next_week_plans": [],
        "risks": [],
        "score": 70,
        "score_breakdown": {},
    }
    base.update(overrides)
    return base


def test_actionable_false_blocker_stays_report_item_only(db_session: Session) -> None:
    """模型判定「不需要管理层介入」的卡点只留 ReportItem，不生成 Task/Blocker。"""
    report = _make_report(db_session, content="日常吐槽：服务器偶尔卡顿，已自行重启解决")
    result = _materialize_result(
        blockers=[
            {
                "title": "服务器卡顿",
                "description": "已自行解决",
                "status": "未解决",
                "source_text": "服务器偶尔卡顿，已自行重启解决",
                "actionable": False,
            }
        ]
    )
    from app.services.structured_data_service import materialize_report_analysis

    materialize_report_analysis(db_session, report, result, "test")
    db_session.commit()

    item = db_session.query(ReportItem).filter_by(report_id=report.id).one()
    assert item.item_type == "blockers", "提及本身仍要留痕备查"
    assert db_session.query(Task).count() == 0
    assert db_session.query(Blocker).count() == 0


def test_existing_task_ref_reuses_task_and_upgrades_blocker(
    db_session: Session,
) -> None:
    """模型引用已有卡点编号时必须复用同一 Task，并按证据升级状态。"""
    current_end = business_date(utcnow())
    current_end -= timedelta(days=current_end.weekday() + 1)
    first_end = current_end - timedelta(weeks=1)

    employee = Employee(
        source="wecom", external_id="ref-emp", name="引用员工",
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add(employee)
    db_session.flush()
    first = WeeklyReport(
        employee_id=employee.id, external_id="ref-rep-1", report_type="weekly",
        title="第一周", content="等待学校材料", content_raw="等待学校材料",
        start_date=first_end - timedelta(days=6), end_date=first_end,
        submitted_at=datetime.combine(first_end - timedelta(days=3), datetime.min.time()), source="wecom",
    )
    second = WeeklyReport(
        employee_id=employee.id, external_id="ref-rep-2", report_type="weekly",
        title="第二周", content="还在等学校材料", content_raw="还在等学校材料",
        start_date=current_end - timedelta(days=6), end_date=current_end,
        submitted_at=datetime.combine(current_end - timedelta(days=3), datetime.min.time()), source="wecom",
    )
    db_session.add_all([first, second])
    db_session.flush()
    item = ReportItem(
        report_id=first.id, employee_id=employee.id, item_type="blockers",
        item_order=0, title="等待学校材料", status="未解决",
        source_text="等待学校材料", risk_level="低",
    )
    db_session.add(item)
    db_session.flush()
    task = Task(canonical_key="等待学校材料", title="等待学校材料", status="BLOCKED")
    db_session.add(task)
    db_session.flush()
    db_session.add(
        TaskReportLink(task_id=task.id, report_id=first.id, report_item_id=item.id)
    )
    db_session.commit()
    from app.services.structured_data_service import (
        _recalculate_task,
        materialize_report_analysis,
    )

    _recalculate_task(db_session, task.id)
    db_session.commit()
    assert db_session.query(Blocker).filter_by(task_id=task.id).one().status == "CANDIDATE"

    # 第二周换了个说法再提同一件事，模型返回 existing_task_ref 指向已有卡点。
    result = _materialize_result(
        blockers=[
            {
                "title": "学校材料仍未回复",
                "description": "继续等待",
                "status": "未解决",
                "source_text": "还在等学校材料",
                "actionable": True,
                "existing_task_ref": task.id,
            }
        ]
    )
    materialize_report_analysis(db_session, second, result, "test")
    db_session.commit()

    link = (
        db_session.query(TaskReportLink)
        .filter_by(report_id=second.id)
        .one()
    )
    assert link.task_id == task.id
    assert link.match_method == "model_ref"
    blocker = db_session.query(Blocker).filter_by(task_id=task.id).one()
    assert blocker.occurrence_count == 2
    assert blocker.status == "OPEN", "两个周期的证据必须升级为正式卡点"
    assert db_session.query(Task).count() == 1, "不得因为换措辞拆出第二条任务"


def test_analysis_prompt_injects_blocker_refs(db_session, monkeypatch) -> None:
    """本人近期未关闭卡点要回灌进提示词，existing_task_ref 才有依据。"""
    current_end = business_date(utcnow())
    current_end -= timedelta(days=current_end.weekday() + 1)
    old_end = current_end - timedelta(weeks=1)

    from app.services.structured_data_service import (
        _recalculate_task,
    )

    employee = Employee(
        source="wecom", external_id="prompt-emp", name="提示词员工",
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add(employee)
    db_session.flush()
    old = WeeklyReport(
        employee_id=employee.id, external_id="prompt-rep-1", report_type="weekly",
        title="上周", content="等待预算审批", content_raw="等待预算审批",
        start_date=old_end - timedelta(days=6), end_date=old_end,
        submitted_at=datetime.combine(old_end - timedelta(days=3), datetime.min.time()), source="wecom",
    )
    current = WeeklyReport(
        employee_id=employee.id, external_id="prompt-rep-2", report_type="weekly",
        title="本周", content="本周继续推进", content_raw="本周继续推进",
        start_date=current_end - timedelta(days=6), end_date=current_end,
        submitted_at=datetime.combine(current_end - timedelta(days=3), datetime.min.time()), source="wecom",
    )
    db_session.add_all([old, current])
    db_session.flush()
    item = ReportItem(
        report_id=old.id, employee_id=employee.id, item_type="blockers",
        item_order=0, title="等待预算审批", status="未解决",
        source_text="等待预算审批", risk_level="低",
    )
    db_session.add(item)
    db_session.flush()
    task = Task(canonical_key="等待预算审批", title="等待预算审批", status="BLOCKED")
    db_session.add(task)
    db_session.flush()
    db_session.add(
        TaskReportLink(task_id=task.id, report_id=old.id, report_item_id=item.id)
    )
    db_session.commit()
    _recalculate_task(db_session, task.id)
    db_session.commit()

    captured: dict[str, str] = {}

    class _Provider:
        def chat(self, system: str, user: str):
            captured["user"] = user
            from app.ai.base import LLMResult

            return LLMResult(
                content=json.dumps(_materialize_result(score=80)),
                model="fake",
                prompt_tokens=1,
                completion_tokens=1,
            )

    monkeypatch.setattr(
        report_analysis_service, "get_provider", lambda s: _Provider()
    )
    report_analysis_service.analyze_report(db_session, current.id, force=True)

    assert "本人近期未关闭的卡点" in captured["user"]
    assert f"#{task.id}" in captured["user"]
    assert "existing_task_ref" in captured["user"]
