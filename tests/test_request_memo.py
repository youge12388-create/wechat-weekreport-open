"""请求内只读记忆化的语义守护（memo 机制本身）。

背景（2026-09-23 加载性能排查）：经营看板与首页会把同一批事实算好几遍 ——
一次 ``GET /api/operating-dashboard?view=overview`` 里 ``insights()`` 被三个消费者
（关键发现、需要你处理、总览信号）各调一次，而每次都要扫描全部任务关联
（开发库 4304 条实测 585 ms，SQL 只占 60 ms）。重复组装正是「点开要等几秒」的成因。

这里锁住两件**很容易改坏且改坏后不会报错**的事：
1. **同一 Session 内重复调用只算一次**（性能性质，结果对不出来的那种坏）；
2. **任何写入都让缓存失效** —— 测试与脚本会复用同一个 Session，写完再读必须看到
   新结果；漏掉失效就会拿到写入前的旧数字，而且完全静默。

另外锁住「不同参数共享同一份计算」不会互相污染：瓶颈列表按 limit 截断时
不能就地改掉被共享的对象（另一处调用拿到的是同一份）。
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

PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)


class _QueryCounter:
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


@pytest.fixture
def seeded(db_session: Session) -> Session:
    """两个部门各一篇周报、一条跨部门任务，够触发跨部门与停滞两类洞察。"""
    department_a = Department(source="wecom", external_id="memo_d1", name="甲部")
    department_b = Department(source="wecom", external_id="memo_d2", name="乙部")
    db_session.add_all([department_a, department_b])
    db_session.flush()

    task = Task(
        canonical_key="memo_cross_department",
        title="跨部门协作事项",
        status="IN_PROGRESS",
        related_project="某业务对象",
        first_seen_date=PERIOD_START,
        last_seen_date=PERIOD_END,
        duration_weeks=3,
    )
    db_session.add(task)
    db_session.flush()

    for index, department in enumerate((department_a, department_b)):
        employee = Employee(
            source="wecom",
            external_id=f"memo_e{index}",
            name=f"员工{index}",
            status=EmployeeStatus.ACTIVE,
            department_id=department.id,
        )
        db_session.add(employee)
        db_session.flush()
        report = WeeklyReport(
            employee_id=employee.id,
            external_id=f"memo_r{index}",
            report_type="weekly",
            title=f"周报{index}",
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
            item_type="blockers",
            item_order=1,
            title=task.title,
            source_text="原文",
        )
        db_session.add(item)
        db_session.flush()
        db_session.add(
            TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)
        )
    db_session.commit()
    return db_session


def test_insights_computes_once_per_period_in_a_request(seeded: Session) -> None:
    """同一 Session 内不同 limit 的 ``insights()`` 只做一次完整计算。

    三个消费者传的 limit 各不相同（关键发现 20、需要你处理 20~80、行动区 5），
    要的却是同一份计算。旧行为是各算一遍，每次都要扫描全部任务关联。
    """
    from app.services.insight_service import insights

    with _QueryCounter(seeded) as first:
        narrow = insights(seeded, PERIOD_START, PERIOD_END, limit=5)
    with _QueryCounter(seeded) as second:
        wide = insights(seeded, PERIOD_START, PERIOD_END, limit=50)

    assert first.count > 0, "首次调用必须真的查库"
    assert second.count == 0, (
        f"第二次调用又跑了 {second.count} 条查询，说明完整计算没有被复用"
    )
    assert len(narrow) <= len(wide), "limit 只能截断，不能改变结论集合"
    assert narrow == wide[: len(narrow)], "不同 limit 拿到的必须是同一份结论的前缀"


def test_insights_recomputes_for_another_period(seeded: Session) -> None:
    """不同周期是不同缓存条目，不能互相串味。

    注意并非所有洞察都看周期：``long_blocker`` / ``cross_department`` /
    ``stalled_plan`` 有意读全历史（同一件事跨周才算「长期」），所以这里用
    **按周期取证**的 ``blocker_growth`` 来判定缓存键是否真的带了周期。
    """
    from app.services.insight_service import insights

    current = insights(seeded, PERIOD_START, PERIOD_END, limit=50)
    empty_period = insights(seeded, date(2026, 8, 31), date(2026, 9, 6), limit=50)

    assert any(item["kind"] == "blocker_growth" for item in current), (
        "本周期有 2 条问题事项、前序周期 0 条，应产出「问题事项明显增加」"
    )
    assert not any(item["kind"] == "blocker_growth" for item in empty_period), (
        "空周期不得命中本周期按周期取证的结论（缓存键少了周期？）"
    )


def test_memo_is_invalidated_by_writes(seeded: Session) -> None:
    """写入之后必须立刻看到新事实，不能拿到写入前的快照。

    缓存用一个 Session 当作用域，而测试与脚本正是跨调用复用它的 ——
    失效一旦漏掉，就是「加了数据但结论没变」这种完全静默的错。
    """
    from app.services.insight_service import insights

    before = insights(seeded, PERIOD_START, PERIOD_END, limit=50)
    assert not any(item["kind"] == "long_blocker" for item in before)

    # 新增一条持续 3 周以上的开放卡点 → 「长期未解决」洞察应立刻出现
    task = seeded.query(Task).first()
    seeded.add(
        Blocker(
            task_id=task.id,
            title="新增长期卡点",
            status="OPEN",
            risk_level="高",
            first_seen_date=PERIOD_START,
            last_seen_date=PERIOD_END,
            duration_weeks=4,
        )
    )
    seeded.commit()

    after = insights(seeded, PERIOD_START, PERIOD_END, limit=50)
    assert any(item["kind"] == "long_blocker" for item in after), (
        "写入后仍拿到同一份结论：请求内缓存没有在写入时失效"
    )


def test_bottlenecks_reuse_aggregate_across_limits(
    seeded: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """瓶颈聚合按不同 limit 取用时只算一遍。

    总览卡要 6 条、信号排序要 20 条，要的是同一份聚合；旧实现每换一个 limit
    就把全部事项重新聚合一遍（规则匹配是纯 CPU 开销，SQL 计数看不出来，
    所以这里直接数聚合函数被调了几次）。
    """
    from app.services import operating_dashboard_service as dashboard

    calls = {"count": 0}
    original = dashboard._build_bottlenecks

    def _counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard, "_build_bottlenecks", _counting)

    narrow = dashboard._bottlenecks(seeded, PERIOD_START, PERIOD_END, limit=6)
    wide = dashboard._bottlenecks(seeded, PERIOD_START, PERIOD_END, limit=50)

    assert calls["count"] == 1, (
        f"同一周期的聚合被算了 {calls['count']} 次，预期 1 次（换 limit 不应重算）"
    )
    assert wide["total"] >= narrow["total"]
    # 窄档必须是宽档的前缀，且两档对同一条目的判定一致
    assert [item["rank"] for item in narrow["items"]] == list(
        range(1, len(narrow["items"]) + 1)
    )
    for narrow_item, wide_item in zip(narrow["items"], wide["items"]):
        assert narrow_item["target"] == wide_item["target"]
        assert narrow_item["rank"] == wide_item["rank"]
        assert narrow_item["severity"] == wide_item["severity"]

    # 再取一次窄档：宽档的存在不能改变它的结果
    again = dashboard._bottlenecks(seeded, PERIOD_START, PERIOD_END, limit=6)
    assert again["items"] == narrow["items"]


def test_blocker_projection_shares_one_computation_across_limits(
    seeded: Session,
) -> None:
    """「需要你处理」的卡点投影按不同 limit 取用时只算一遍。"""
    from app.services.insight_service import _current_blocker_projection

    _current_blocker_projection(seeded, PERIOD_START, PERIOD_END, limit=None)
    with _QueryCounter(seeded) as cached:
        limited = _current_blocker_projection(seeded, PERIOD_START, PERIOD_END, limit=5)

    assert cached.count == 0, (
        f"按 limit 取用时又跑了 {cached.count} 条查询，说明投影没有被复用"
    )
    assert isinstance(limited, list)


def test_task_rows_projection_keeps_required_shape(seeded: Session) -> None:
    """列投影后 ``task_rows`` 仍必须提供调用方读到的每个字段。"""
    from app.services.insight_service import insights, task_rows

    rows = task_rows(seeded)
    assert rows, "应能取到任务关联"
    task, item, report, employee, department = rows[0]
    # 洞察判定读的字段
    assert task.id and task.title and task.status
    assert isinstance(task.duration_weeks, int)
    assert item.item_type
    assert item.mentioned_people is None or isinstance(item.mentioned_people, list)
    assert report.id
    assert employee is None or employee.id
    assert department is None or department.name

    # 投影不改结论：跨部门洞察照样产出，且带得出部门名
    cross = [
        entry
        for entry in insights(seeded, PERIOD_START, PERIOD_END, limit=50)
        if entry["kind"] == "cross_department"
    ]
    assert cross, "列投影不能把跨部门洞察算丢"
    assert cross[0]["departments"] == ["乙部", "甲部"]
