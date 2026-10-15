"""洞察计算的查询次数守护。

背景：`insights()` 在首页热路径上（`dashboard_summary` 与 `management_action_items`
都会调用）。它先按任务聚合，再为每个任务各查一次来源 —— 任务越多往返越多，
是典型的 N+1。`task_sources` 本就支持批量，所以这里用「查询次数不随任务数
线性增长」来锁住这个性质，而不是只断言结果对（结果对和有没有 N+1 无关）。

同时锁住「任务来源确实带出来了」：批量改造最容易把结果改成空列表而测试仍绿。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services.insight_service import insights

PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)


class _QueryCounter:
    """统计一次调用期间真正发往数据库的语句条数。"""

    def __init__(self, session: Session) -> None:
        self.engine = session.get_bind()
        self.count = 0

    def __enter__(self) -> "_QueryCounter":
        def _on_execute(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
            self.count += 1

        self._listener = _on_execute
        event.listen(self.engine, "before_cursor_execute", _on_execute)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._listener)


def _cross_department_task(session: Session, index: int) -> Task:
    """造一个「出现在两个部门」的任务：这是 cross_department 洞察的触发条件。"""
    task = Task(
        canonical_key=f"cross_{index}",
        title=f"跨部门事项{index}",
        status="IN_PROGRESS",
        first_seen_date=PERIOD_START,
        last_seen_date=PERIOD_END,
        duration_weeks=2,
    )
    session.add(task)
    session.flush()

    for dept_index, dept_name in enumerate((f"甲部门{index}", f"乙部门{index}")):
        dept = Department(source="wecom", external_id=f"d_{index}_{dept_index}", name=dept_name)
        session.add(dept)
        session.flush()
        employee = Employee(
            source="wecom",
            external_id=f"e_{index}_{dept_index}",
            name=f"员工{index}-{dept_index}",
            status=EmployeeStatus.ACTIVE,
            department_id=dept.id,
        )
        session.add(employee)
        session.flush()
        report = WeeklyReport(
            employee_id=employee.id,
            external_id=f"r_{index}_{dept_index}",
            report_type="weekly",
            title=f"周报{index}-{dept_index}",
            content="内容",
            content_raw="内容",
            start_date=PERIOD_START,
            end_date=PERIOD_END,
            submitted_at=datetime(2026, 9, 10, 10, 0, 0),
            source="wecom",
        )
        session.add(report)
        session.flush()
        item = ReportItem(
            report_id=report.id,
            item_type="blockers",
            item_order=1,
            title=task.title,
            source_text="原文",
        )
        session.add(item)
        session.flush()
        session.add(
            TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)
        )
    session.commit()
    return task


def test_insights_returns_sources_for_cross_department_tasks(db_session: Session) -> None:
    """批量取来源不能把结果取空 —— 改造后仍必须带出可追溯来源。"""
    _cross_department_task(db_session, 0)

    items = insights(db_session, PERIOD_START, PERIOD_END, limit=50)
    cross = [item for item in items if item["kind"] == "cross_department"]
    assert cross, "同时出现在两个部门的任务必须产出跨部门洞察"
    assert cross[0]["sources"], "必须带出可追溯的周报来源"
    assert cross[0]["departments"] == ["乙部门0", "甲部门0"]


def test_insights_query_count_does_not_grow_with_task_count(db_session: Session) -> None:
    """查询次数不能随任务数线性增长（N+1 回归守护）。

    旧实现在每个任务里各查一次来源，任务数翻倍则往返翻倍。
    这里比较 1 个任务与 4 个任务的语句数：允许少量差异，但不能接近线性放大。
    """
    _cross_department_task(db_session, 999)  # 先预热，避免首查的建连/编译开销错配
    with _QueryCounter(db_session) as one:
        insights(db_session, PERIOD_START, PERIOD_END, limit=50)

    for index in range(4):
        _cross_department_task(db_session, index)

    with _QueryCounter(db_session) as many:
        insights(db_session, PERIOD_START, PERIOD_END, limit=50)

    # 任务从 1 个增到 5 个（+4 个跨部门任务）。若仍是 N+1，会多出约 4 条来源查询；
    # 批量后增长应远小于这个量级。
    assert many.count - one.count <= 3, (
        f"查询次数随任务数增长过多（1任务={one.count}，5任务={many.count}），"
        "疑似来源查询又退回了逐任务往返"
    )
