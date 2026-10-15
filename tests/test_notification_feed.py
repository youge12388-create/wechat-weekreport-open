"""消息中心聚合接口：报错与提醒汇总、排序、已读语义依赖的字段稳定性。

用例全部显式传 start/end：不传会撞上「未到截止时间看上一周」的默认周期规则
（见 AGENTS.md 已知取舍第 9 条），本周新建的数据根本查不到。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.main import create_app
from app.models.ai_analysis import AIAnalysis
from app.models.analysis_queue import AnalysisQueue
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus, AnalysisQueueStatus, SyncJobStatus, SyncType
from app.models.sync_job import SyncJob
from app.services.department_sync import sync_departments
from app.services.employee_sync import sync_employees
from app.services.weekly_report_sync import sync_weekly_reports
from tests.fake_wecom import (
    FakeWeComClient,
    _dept,
    _journal_info,
    _user,
    text_apply_data,
)

WEEK = {"start": "2026-09-07", "end": "2026-09-13"}


def _ts(y: int, m: int, d: int, h: int = 10) -> int:
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """内存库 + 一份最小通讯录（张三/李四提交，王五未提交）。"""
    monkeypatch.setenv("DB_URL", "sqlite://")
    get_settings.cache_clear()
    dispose_engine()
    engine = get_engine(get_settings())
    Base.metadata.create_all(engine)
    db = get_session_factory()()

    client = FakeWeComClient(
        departments=[_dept(1, "总部"), _dept(2, "信息部", 1)],
        employees=[
            _user("u1", "张三", 2),
            _user("u2", "李四", 2),
            _user("u3", "王五", 2),
        ],
        reports={
            "j1": _journal_info(
                report_time=_ts(2026, 9, 8),
                userid="u1",
                apply_data=text_apply_data("本周开发 A 功能"),
            ),
            "j2": _journal_info(
                report_time=_ts(2026, 9, 9),
                userid="u2",
                apply_data=text_apply_data("本周测试 B 模块"),
            ),
        },
    )
    sync_departments(db, client)
    sync_employees(db, client)
    # 消息中心的「排队中/分析失败」知会依赖同步自动入队，这里显式开启。
    from app.services.runtime_config_service import update_runtime_values

    update_runtime_values(db, {"ai_auto_enqueue_on_sync": "true"})
    db.commit()
    sync_weekly_reports(db, datetime(2026, 9, 6), datetime(2026, 9, 13), client)
    try:
        yield db
    finally:
        db.close()
        dispose_engine()
        get_settings.cache_clear()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override():
        yield session

    app.dependency_overrides[deps.get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _failed_job(
    sync_type: str,
    end_time: datetime,
    error: list[dict] | None = None,
) -> SyncJob:
    return SyncJob(
        source="wecom",
        type=sync_type,
        status=SyncJobStatus.FAILED,
        start_time=end_time - timedelta(minutes=1),
        end_time=end_time,
        success_count=0,
        failed_count=1,
        skipped_count=0,
        error=error or [{"external_id": None, "code": "batch_error", "message": "boom"}],
        params={},
        triggered_by="auto",
    )


def _success_job(sync_type: str, end_time: datetime) -> SyncJob:
    return SyncJob(
        source="wecom",
        type=sync_type,
        status=SyncJobStatus.SUCCESS,
        start_time=end_time - timedelta(minutes=1),
        end_time=end_time,
        success_count=1,
        failed_count=0,
        skipped_count=0,
        error=[],
        params={},
        triggered_by="auto",
    )


def _find(body: dict, item_id: str) -> dict | None:
    return next((item for item in body["items"] if item["id"] == item_id), None)


def _clear_sync_jobs(session: Session) -> None:
    """种子同步会留下 triggered_by=system 的成功任务。

    「未恢复的失败」按最新一条任务判断，所以要先把这些成功任务清掉，
    否则被测的失败任务永远排在它们前面之后，测到的是另一条语义。
    """
    from sqlalchemy import delete

    session.execute(delete(SyncJob))
    session.commit()


_IP_BLOCKED = [
    {
        "external_id": None,
        "code": "batch_error",
        "message": (
            "ConnectorPermissionError: [60020] not allow to access from your ip, "
            "hint: [example-request-id], from ip: 203.0.113.10, "
            "more info at https://open.work.weixin.qq.com/devtool/query?e=60020"
        ),
    }
]


def test_feed_reports_unresolved_sync_failure(client: TestClient, session: Session) -> None:
    """未恢复的同步失败必须出现在消息中心，并带上可直接点的处理入口。"""
    _clear_sync_jobs(session)
    session.add(_failed_job(SyncType.EMPLOYEES, datetime(2026, 9, 15, 7, 30), _IP_BLOCKED))
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()

    item = _find(body, "sync_failure:employees")
    assert item is not None
    assert item["level"] == "error"
    assert item["title"] == "员工同步失败"
    assert item["meta"]["merged"] is False
    # 出口 IP 要能直接被拿去核对白名单，而不是让用户翻原始报文
    assert "203.0.113.10" in item["summary"]
    assert item["meta"]["vendor_code"] == "60020"
    assert item["meta"]["egress_ip"] == "203.0.113.10"
    assert item["href"] == "/settings?category=sync"
    # 技术细节必须保留可展开的原文
    assert item["raw"]


def test_feed_merges_same_root_cause_sync_failures(
    client: TestClient, session: Session
) -> None:
    """同一个可信 IP 没加白会同时打挂三类同步：只报一条，不铺三张一样的卡。

    这正是消息中心要消掉的噪音 —— 读三遍同一句话才知道是一条问题。
    """
    _clear_sync_jobs(session)
    for sync_type in (SyncType.DEPARTMENTS, SyncType.EMPLOYEES, SyncType.WEEKLY_REPORTS):
        session.add(_failed_job(sync_type, datetime(2026, 9, 15, 7, 30), _IP_BLOCKED))
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()

    failures = [item for item in body["items"] if item["kind"] == "sync_failure"]
    assert len(failures) == 1
    merged = failures[0]
    assert merged["id"] == "sync_failure:departments,employees,weekly_reports"
    assert merged["title"] == "部门、员工、周报同步失败"
    assert merged["meta"]["merged"] is True
    assert merged["meta"]["sync_types"] == [
        SyncType.DEPARTMENTS,
        SyncType.EMPLOYEES,
        SyncType.WEEKLY_REPORTS,
    ]
    assert body["counts"]["error"] == 1


def test_feed_keeps_different_root_causes_separate(
    client: TestClient, session: Session
) -> None:
    """根因不同就不是同一条消息：换一个错误原因要各自成卡。"""
    _clear_sync_jobs(session)
    session.add(_failed_job(SyncType.EMPLOYEES, datetime(2026, 9, 15, 7, 30), _IP_BLOCKED))
    session.add(
        _failed_job(
            SyncType.WEEKLY_REPORTS,
            datetime(2026, 9, 15, 8, 30),
            [{"external_id": None, "code": "batch_error", "message": "Connection timed out"}],
        )
    )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    failures = [item for item in body["items"] if item["kind"] == "sync_failure"]

    assert len(failures) == 2
    assert body["counts"]["error"] == 2
    assert {item["id"] for item in failures} == {
        "sync_failure:employees",
        "sync_failure:weekly_reports",
    }


def test_feed_drops_failure_after_later_success(client: TestClient, session: Session) -> None:
    """可信 IP 修好之后，消息中心不能再挂着旧红字。"""
    _clear_sync_jobs(session)
    session.add(_failed_job(SyncType.EMPLOYEES, datetime(2026, 9, 15, 7, 30), _IP_BLOCKED))
    session.add(_success_job(SyncType.EMPLOYEES, datetime(2026, 9, 15, 9, 0)))
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()

    assert _find(body, "sync_failure:employees") is None
    assert body["counts"]["error"] == 0


def test_feed_lists_unsubmitted_with_period_scope(client: TestClient) -> None:
    """未提交提醒的人数口径必须与首页提交率一致（在职 - 免提交 - 已提交）。"""
    body = client.get("/api/notifications/feed", params=WEEK).json()

    item = _find(body, "unsubmitted:2026-09-07")
    assert item is not None
    assert item["level"] == "warning"
    assert item["meta"] == {"unsubmitted": 1, "reportable": 3}
    assert "1 人" in item["title"]
    assert body["period"] == WEEK


def test_feed_omits_unsubmitted_when_period_is_complete(
    client: TestClient, session: Session
) -> None:
    """全员交齐时不该留一条 0 人的空提醒。"""
    # 让最后一位未提交者所在周期也有周报
    report_time = _ts(2026, 9, 10)
    client_obj = FakeWeComClient(
        departments=[_dept(1, "总部"), _dept(2, "信息部", 1)],
        employees=[_user("u3", "王五", 2)],
        reports={
            "j3": _journal_info(
                report_time=report_time,
                userid="u3",
                apply_data=text_apply_data("本周完成 C 需求"),
            )
        },
    )
    sync_weekly_reports(
        session, datetime(2026, 9, 6), datetime(2026, 9, 13), client_obj
    )

    body = client.get("/api/notifications/feed", params=WEEK).json()
    assert _find(body, "unsubmitted:2026-09-07") is None


def test_feed_flags_unconfigured_ai(client: TestClient) -> None:
    """没配密钥时给出明确引导，而不是让用户点了才发现不能用。"""
    body = client.get("/api/notifications/feed", params=WEEK).json()

    item = _find(body, "ai_unconfigured")
    assert item is not None
    assert item["href"] == "/settings?category=ai"
    assert item["level"] == "warning"


def test_feed_reports_last_ai_call_failure(client: TestClient, session: Session) -> None:
    """最近一次模型调用失败要报错；随后的成功调用必须让它消失。"""
    session.add(
        AIAnalysis(
            entity_type="report",
            entity_id=1,
            analysis_type="report_analysis",
            status=AIAnalysisStatus.FAILED,
            error_message="HTTP 402 余额不足",
        )
    )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    item = _find(body, "ai_call_failed")
    assert item is not None
    assert item["level"] == "error"
    assert "402" in item["summary"]

    session.add(
        AIAnalysis(
            entity_type="report",
            entity_id=2,
            analysis_type="report_analysis",
            status=AIAnalysisStatus.SUCCESS,
        )
    )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    assert _find(body, "ai_call_failed") is None


def test_feed_reports_analysis_backlog(client: TestClient, session: Session) -> None:
    """周报已入库但还没分析完时，消息中心要说清还剩几篇待分析。

    同步入库本身就会为每篇周报建一条 pending 队列位，所以这里不需要造假数据：
    直接用种子数据断言「排队中」这条知会确实存在，且数量是真实行数。
    """
    body = client.get("/api/notifications/feed", params=WEEK).json()

    item = _find(body, "analysis_waiting:2026-09-07")
    assert item is not None
    assert item["level"] == "info"
    assert item["meta"] == {"waiting": 2}


def test_feed_reports_failed_analysis_instead_of_waiting(
    client: TestClient, session: Session
) -> None:
    """失败优先于待处理：同一批里既有失败又有排队时只报需要人管的那条。"""
    from sqlalchemy import select

    from app.models.analysis_queue import AnalysisQueue as Queue
    from app.models.enums import AnalysisQueueStatus as QueueStatus
    from app.models.weekly_report import WeeklyReport

    queue = session.scalars(select(Queue).order_by(Queue.report_id)).all()
    assert len(queue) >= 2
    queue[0].status = QueueStatus.FAILED
    queue[0].attempts = 3
    queue[0].last_error = "HTTP 402 余额不足"
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    failed = _find(body, "analysis_failed:2026-09-07")
    assert failed is not None
    assert failed["level"] == "warning"
    assert failed["meta"]["failed"] == 1
    assert _find(body, "analysis_waiting:2026-09-07") is None


def test_feed_reports_period_summary_pending(client: TestClient) -> None:
    """刚同步完本期周报时，总结已在后台排队 —— 这是一条知会，不是故障。"""
    body = client.get("/api/notifications/feed", params=WEEK).json()

    item = _find(body, "period_summary_running:2026-09-07")
    assert item is not None
    assert item["level"] == "info"
    assert item["href"].startswith("/dashboard?start=2026-09-07")


def test_feed_reports_missing_period_summary(
    client: TestClient, session: Session
) -> None:
    """从未生成过总结（连队列都没有）时给出「去生成」的入口。"""
    from sqlalchemy import delete

    from app.models.period_summary_queue import PeriodSummaryQueue

    session.execute(delete(PeriodSummaryQueue))
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()

    item = _find(body, "period_summary_missing:2026-09-07")
    assert item is not None
    assert item["level"] == "info"
    assert item["href"].startswith("/dashboard?start=2026-09-07")


def test_feed_reports_sync_queue(client: TestClient, session: Session) -> None:
    """有任务在跑时给出知会，并说明队列里各有多少条。"""
    _clear_sync_jobs(session)
    session.add(
        SyncJob(
            source="wecom",
            type=SyncType.WEEKLY_REPORTS,
            status=SyncJobStatus.PENDING,
            params={},
            triggered_by="api:queued",
        )
    )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    item = _find(body, "sync_queue")
    assert item is not None
    assert item["level"] == "info"
    assert item["meta"] == {"pending": 1, "running": 0}


def test_feed_sorts_errors_before_warnings_and_info(client: TestClient, session: Session) -> None:
    """排序是「先看要紧的」：报错 → 提醒 → 知会，同级内新消息在前。"""
    _clear_sync_jobs(session)
    session.add(_failed_job(SyncType.EMPLOYEES, datetime(2026, 9, 15, 7, 30), _IP_BLOCKED))
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    levels = [item["level"] for item in body["items"]]

    assert levels == sorted(
        levels, key=lambda level: {"error": 0, "warning": 1, "info": 2}[level]
    )
    assert body["items"][0]["id"] == "sync_failure:employees"
    assert body["total"] == len(body["items"])
    assert body["counts"]["error"] == 1


def test_feed_includes_auto_sync_hint_when_disabled(client: TestClient) -> None:
    """自动同步默认关闭：消息中心应指向开关，而不是让用户以为系统坏了。"""
    body = client.get("/api/notifications/feed", params=WEEK).json()

    item = _find(body, "auto_sync_disabled")
    assert item is not None
    assert item["href"] == "/settings?category=sync"


def test_feed_rejects_bad_period_format(client: TestClient) -> None:
    response = client.get(
        "/api/notifications/feed", params={"start": "09/07/2026", "end": "2026-09-13"}
    )
    assert response.status_code == 400


def test_feed_reports_reminder_recipients_wecom_refused_to_deliver(
    client: TestClient, session: Session
) -> None:
    """企微「请求成功但这个人送不到」必须出现在消息中心。

    否则管理员在设置页看到「已提醒 N 人」，实际上有人一个字都没收到 ——
    这类问题只有调自建应用的可见范围才能修，而它不会自愈。
    """
    from sqlalchemy import select

    from app.models.enums import ReminderStatus
    from app.models.reminder_log import ReminderLog

    employee = session.scalar(select(Employee).where(Employee.external_id == "u3"))
    session.add(
        ReminderLog(
            employee_id=employee.id,
            period_start=date(2026, 9, 7),
            period_end=date(2026, 9, 13),
            status=ReminderStatus.FAILED,
            detail="企微未投递（不在应用可见范围或通讯录中，请检查该应用的可见范围）",
            triggered_by="api:manual",
        )
    )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    item = _find(body, "reminder_undelivered:2026-09-07")
    assert item is not None
    assert item["level"] == "warning"
    assert "王五" in item["summary"]
    assert "可见范围" in item["hint"]


def test_feed_distinguishes_batch_failure_from_recipient_undelivered(
    client: TestClient, session: Session
) -> None:
    """81013/81014 整批拒绝不能被错误归类为部分收件人未送达。"""
    from sqlalchemy import select

    from app.models.reminder_log import ReminderLog

    employee = session.scalar(select(Employee).where(Employee.external_id == "u3"))
    session.add(
        ReminderLog(
            employee_id=employee.id,
            period_start=date(2026, 9, 7),
            period_end=date(2026, 9, 13),
            status="failed",
            detail="batch_failure:[81013] invalid user。所列收件人全部收不到该应用的消息，通常是可见范围未覆盖。",
            triggered_by="api:manual",
        )
    )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()

    assert _find(body, "reminder_undelivered:2026-09-07") is None
    failure = _find(body, "reminder_failure:2026-09-07")
    assert failure is not None
    assert failure["level"] == "error"
    assert "81013" in failure["summary"]


def test_feed_deduplicates_and_clears_recipient_delivery_warning(
    client: TestClient, session: Session
) -> None:
    """重复失败只计一人，后续成功后旧未送达告警必须消失。"""
    from sqlalchemy import select

    from app.models.enums import ReminderStatus
    from app.models.reminder_log import ReminderLog

    employee = session.scalar(select(Employee).where(Employee.external_id == "u3"))
    for minute in (1, 2):
        session.add(
            ReminderLog(
                employee_id=employee.id,
                period_start=date(2026, 9, 7),
                period_end=date(2026, 9, 13),
                status=ReminderStatus.FAILED,
                detail="recipient_undelivered:企微未投递（不在应用可见范围或通讯录中，请检查该应用的可见范围）",
                triggered_by=f"api:manual:{minute}",
                created_at=datetime(2026, 9, 15, 8, minute),
            )
        )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    warning = _find(body, "reminder_undelivered:2026-09-07")
    assert warning is not None
    assert warning["title"] == "1 人没收到催交提醒"
    assert warning["meta"] == {"undelivered": 1}

    session.add(
        ReminderLog(
            employee_id=employee.id,
            period_start=date(2026, 9, 7),
            period_end=date(2026, 9, 13),
            status=ReminderStatus.SENT,
            detail="ok",
            triggered_by="api:manual:fixed",
            created_at=datetime(2026, 9, 15, 9, 0),
        )
    )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    assert _find(body, "reminder_undelivered:2026-09-07") is None


def test_feed_ignores_whole_batch_reminder_failures(
    client: TestClient, session: Session
) -> None:
    """整批发送失败（ConnectorError）走同步/AI 报错通道，不在「谁没收到」里报。"""
    from sqlalchemy import select

    from app.models.enums import ReminderStatus
    from app.models.reminder_log import ReminderLog

    employee = session.scalar(select(Employee).where(Employee.external_id == "u3"))
    session.add(
        ReminderLog(
            employee_id=employee.id,
            period_start=date(2026, 9, 7),
            period_end=date(2026, 9, 13),
            status=ReminderStatus.FAILED,
            detail="企微接口异常",
            triggered_by="api:manual",
        )
    )
    session.commit()

    body = client.get("/api/notifications/feed", params=WEEK).json()
    assert _find(body, "reminder_undelivered:2026-09-07") is None
