"""首页/部门页热路径的查询次数与接口契约守护。

背景（2026-09-18 性能排查）：`/api/dashboard/departments` 原来在循环里逐部门调用
`department_analysis`，每个部门要跑「事项 + 覆盖率 + 卡点 + 来源」四组查询，
13 个部门实测 267 条 SQL；`/api/dashboard/overview` 一次 108 条，其中 18 条是同一个
「免提交部门」查询。两者都是页面级热路径，所以用「查询次数不随部门数线性增长」
锁住这批改造，而不是只断言结果对不对 —— 后者在 N+1 存在时同样会通过。

同时守护新接口的契约：
- `/api/dashboard/departments?detail=false` 必须仍然给出提交口径（首页依赖它），
  且**不含**分析类字段（避免调用方误以为分析已经算过）。
- `/api/dashboard/status` 的字段口径必须与 `/overview` 一致，否则前端用它做轮询
  会与整页刷新显示不一致。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
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
WEEK = {"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()}


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


def _seed_department(session: Session, index: int, *, with_blocker: bool) -> Department:
    """造一个部门：1 名在职员工 + 1 篇本周期周报 + 6 条事项（可选 1 个开放卡点）。"""
    dept = Department(
        source="wecom", external_id=f"dept_{index}", name=f"部门{index}", status="active"
    )
    session.add(dept)
    session.flush()
    employee = Employee(
        source="wecom",
        external_id=f"emp_{index}",
        name=f"员工{index}",
        status=EmployeeStatus.ACTIVE,
        department_id=dept.id,
    )
    session.add(employee)
    session.flush()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id=f"report_{index}",
        report_type="weekly",
        title=f"周报{index}",
        content="内容",
        content_raw="内容",
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.flush()
    for order, item_type in enumerate(
        ["completed_items", "completed_items", "ongoing_items", "ongoing_items",
         "next_week_plans", "blockers"]
    ):
        item = ReportItem(
            report_id=report.id,
            item_type=item_type,
            item_order=order,
            title=f"{item_type}-{index}",
            source_text="原文",
        )
        session.add(item)
        session.flush()
        if item_type != "blockers":
            continue
        task = Task(
            canonical_key=f"task_{index}",
            title=f"任务{index}",
            status="IN_PROGRESS",
            first_seen_date=PERIOD_START,
            last_seen_date=PERIOD_END,
            duration_weeks=3,
        )
        session.add(task)
        session.flush()
        session.add(
            TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)
        )
        if with_blocker:
            session.add(
                Blocker(
                    task_id=task.id,
                    title=f"卡点{index}",
                    status="OPEN",
                    risk_level="高",
                    first_seen_date=PERIOD_START,
                    last_seen_date=PERIOD_END,
                    duration_weeks=3,
                )
            )
    session.commit()
    return dept


@pytest.fixture
def seeded(db_session: Session) -> Session:
    _seed_department(db_session, 0, with_blocker=True)
    return db_session


@pytest.fixture
def client(seeded: Session) -> TestClient:
    from app.api import deps
    from app.main import create_app

    app = create_app()

    def _override():
        yield seeded

    app.dependency_overrides[deps.get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_dashboard_departments_has_no_per_department_n_plus_one(
    seeded: Session,
) -> None:
    """部门数从 1 增到 6 时，查询次数不能接近线性增长。

    旧实现每个部门要跑 4 组查询（实测 13 部门 267 条），增量随部门数放大；
    批量后增量只来自各组查询本身，与部门数无关。
    """
    from app.api.routes.dashboard import department_stats

    # 先预热，避免首查的编译/建连开销错配
    department_stats(seeded, start=WEEK["start"], end=WEEK["end"], detail=True)
    with _QueryCounter(seeded) as one:
        department_stats(seeded, start=WEEK["start"], end=WEEK["end"], detail=True)

    for index in range(1, 6):
        _seed_department(seeded, index, with_blocker=True)

    with _QueryCounter(seeded) as many:
        department_stats(seeded, start=WEEK["start"], end=WEEK["end"], detail=True)

    # 5 个新部门若仍是逐部门分析，会多出约 20 条查询；批量后增量很小。
    assert many.count - one.count <= 6, (
        f"查询次数随部门数增长过多（1部门={one.count}，6部门={many.count}），"
        "疑似部门分析又退回了逐部门往返"
    )


def test_department_analysis_batch_matches_single_department_path(
    seeded: Session,
) -> None:
    """批量路径与单部门路径的结论必须逐字段一致。

    两条路径并存（部门详情页走单个、看板走批量），口径一旦分叉，
    同一个部门在两个页面会显示不同的温度，且不会有任何报错。
    """
    from app.services.insight_service import (
        department_analysis,
        department_analysis_batch,
    )

    dept = seeded.query(Department).filter_by(name="部门0").one()
    single = department_analysis(seeded, dept.id, PERIOD_END, 1)
    batch = department_analysis_batch(seeded, [dept.id], PERIOD_END, 1)[dept.id]

    for key in ("coverage", "counts", "status", "summary", "period"):
        assert batch[key] == single[key], f"{key} 在批量与单部门路径下不一致"
    assert [b["id"] for b in batch["blockers"]] == [b["id"] for b in single["blockers"]]
    # 批量路径刻意不返回 trend（单部门时间序列由调用方补），其余字段必须齐全
    assert "trend" not in batch
    assert "trend" in single


def test_departments_detail_false_still_reports_submission_but_skips_analysis(
    client: TestClient,
) -> None:
    """首页用的精简档：提交口径照常，分析类字段不出现。

    首页只渲染部门进度条（submitted/employee_count/unsubmitted），
    靠 detail=false 省掉整轮分析；若哪天它改回默认档，这条会红。
    """
    slim = client.get(
        "/api/dashboard/departments", params={**WEEK, "detail": "false"}
    ).json()
    assert slim["detail"] is False
    item = slim["items"][0]
    assert item["employee_count"] == 1
    assert item["submitted"] == 1
    assert item["unsubmitted"] == 0
    assert item["reports_count"] == 1
    # 参考分是首页部门进度条提示要用的，精简档也必须给出（它只是一条分组聚合）
    assert "average_score" in item
    for heavy in (
        "work_item_total",
        "work_item_done",
        "work_item_in_progress",
        "blocker_total",
        "department_status",
        "analysis_coverage",
    ):
        assert heavy not in item, f"精简档不应返回 {heavy}"

    full = client.get(
        "/api/dashboard/departments", params={**WEEK, "detail": "true"}
    ).json()
    assert full["detail"] is True
    assert full["items"][0]["blocker_total"] == 1
    assert full["items"][0]["department_status"] == "注意"
    assert full["items"][0]["analysis_coverage"]["rate"] == 100.0
    # 两档的提交口径必须完全一致（同一份统计，只是分析部分被省掉）
    assert full["items"][0]["submitted"] == slim["items"][0]["submitted"]
    assert full["items"][0]["unsubmitted"] == slim["items"][0]["unsubmitted"]


def test_dashboard_status_matches_overview_fields(client: TestClient) -> None:
    """轮询接口与整页概览的同名字段必须一致。

    前端在后台任务进行中改打 /status 轮询（省掉重算），只有确有进展才整页重拉。
    两个接口若给出不同的进度结论，界面会卡在「还在分析」或提前跳「已完成」。
    """
    status = client.get("/api/dashboard/status", params=WEEK).json()
    overview = client.get("/api/dashboard/overview", params=WEEK).json()

    assert status["period"] == overview["period"]
    assert status["analysis_coverage"] == overview["analysis_coverage"]
    assert status["ai_period_summary"] == overview["ai_period_summary"]
    # 状态接口刻意只返回这两块，避免它自己又长成一个重接口
    assert set(status) == {"period", "analysis_coverage", "ai_period_summary"}


def test_item_rows_is_memoized_per_session(seeded: Session) -> None:
    """同一 Session 内相同区间的 ``_item_rows`` 只查一次库。

    首页一次渲染里本周期会被 dashboard_summary / insights / period_comparison
    各要一遍，每次都完整实例化四个实体 —— 这是首页耗时的主要来源（SQL 只占
    约四分之一）。缓存作用域是 Session，所以跨请求不会读到旧数据。
    """
    from app.services.insight_service import _item_rows

    seeded.info.pop("insight_service.item_rows", None)
    statements: list[str] = []
    engine = seeded.get_bind()

    def _on_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        first = _item_rows(seeded, PERIOD_START, PERIOD_END)
        assert _item_rows(seeded, PERIOD_START, PERIOD_END) == first
        assert _item_rows(seeded, PERIOD_START, PERIOD_END) == first
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)

    # 只数「取事项行」那条：同一次调用里的免提交部门查询是另一回事
    # （它自己也有请求内缓存，见下面的用例）。
    row_queries = [
        s for s in statements if "report_items" in s and "weekly_reports" in s
    ]
    assert len(row_queries) == 1, (
        f"相同区间取了 {len(row_queries)} 次事项行，预期 1 次（请求内缓存失效？）"
    )

    # 不同区间/不同部门必须是不同的缓存条目，不能串味
    other = _item_rows(seeded, PERIOD_START, PERIOD_END - timedelta(days=1))
    assert other is not first
    scoped = _item_rows(seeded, PERIOD_START, PERIOD_END, department_id=99999)
    assert scoped == [], "按不存在部门过滤应返回空，不能命中上一个缓存"
    assert first, "原缓存仍应保留"


def test_dashboard_overview_does_not_recount_exempt_departments(
    seeded: Session,
) -> None:
    """整页概览里免提交部门查询不能重复（一次请求几十条重复 SQL）。"""
    from app.services.insight_service import dashboard_summary

    monkeypatch_target = seeded
    monkeypatch_target.info.pop("view_service.exempt_department_ids", None)
    monkeypatch_target.info.pop("insight_service.item_rows", None)

    engine = seeded.get_bind()
    statements: list[str] = []

    def _on_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        dashboard_summary(seeded, PERIOD_START, PERIOD_END)
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)

    exempt_lookups = [
        s for s in statements if s.startswith("SELECT departments.id FROM departments")
    ]
    assert len(exempt_lookups) <= 1, (
        f"免提交部门查询重复了 {len(exempt_lookups)} 次，预期 0~1 次（请求内缓存失效？）"
    )


def test_exempt_department_lookup_is_memoized_per_session(
    seeded: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 Session 内重复调用只查一次库（首页一次请求会问几十遍）。"""
    from app.core.config import get_settings
    from app.services.view_service import exempt_department_ids

    monkeypatch.setattr(
        get_settings(), "report_exempt_departments", ["总裁办", "部门0"]
    )
    seeded.info.pop("view_service.exempt_department_ids", None)

    with _QueryCounter(seeded) as counter:
        first = exempt_department_ids(seeded)
        for _ in range(10):
            assert exempt_department_ids(seeded) == first

    assert counter.count == 1, (
        f"免提交部门查询重复了 {counter.count} 次，预期 1 次（请求内缓存失效？）"
    )
    # 返回的必须是新 set：调用方就地修改不能污染缓存
    first.add(99999)
    assert 99999 not in exempt_department_ids(seeded)


def test_exempt_department_cache_reacts_to_config_change(
    seeded: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置变化必须让缓存失效，否则改完设置还得重启才能看到新结论。

    缓存键带上部门名称元组正是为了这个：测试里的 monkeypatch、运行期的设置保存，
    都会改变名称集合，于是下一次调用重新查库。
    """
    from app.core.config import get_settings
    from app.services.view_service import exempt_department_ids

    dept_id = seeded.query(Department).one().id

    monkeypatch.setattr(get_settings(), "report_exempt_departments", ["部门0"])
    seeded.info.pop("view_service.exempt_department_ids", None)
    assert exempt_department_ids(seeded) == {dept_id}

    # 换成另一个不存在的部门名：必须重新查库并给出空结果，
    # 而不是继续返回上一次缓存的 {dept_id}。
    monkeypatch.setattr(get_settings(), "report_exempt_departments", ["不存在的部门"])
    assert exempt_department_ids(seeded) == set()

    # 清空配置同样要立即生效
    monkeypatch.setattr(get_settings(), "report_exempt_departments", [])
    assert exempt_department_ids(seeded) == set()
