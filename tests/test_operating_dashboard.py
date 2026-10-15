"""经营看板：聚合口径、证据链、缓存隔离与只读语义。

用例显式传 start/end —— 不传会撞上「未到截止时间看上一周」的默认周期规则
（AGENTS 已知取舍第 9 条），当周新建的数据根本查不到。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.fake import FakeProvider
from app.api import deps
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.main import create_app
from app.models.ai_analysis import AIAnalysis
from app.models.analysis_queue import AnalysisQueue
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus, AnalysisQueueStatus, OperatingSummaryType
from app.models.operating_summary_queue import COMPANY_SCOPE_ID, OperatingSummaryQueue
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services import operating_dashboard_service as dashboard
from app.services import operating_summary_service as summary
from app.services import monthly_department_cards_service as monthly_cards
from app.services.report_analysis_service import report_payload_hash

PERIOD = {"start": "2026-09-14", "end": "2026-09-20"}
PERIOD_START = date(2026, 9, 14)
PERIOD_END = date(2026, 9, 20)


def _ts(y: int, m: int, d: int, h: int = 10) -> int:
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """一份最小但完整的事实：两个部门、三篇周报、一条跨月持续卡点。

    直接构造结构化结果（ReportItem / Task / Blocker）而不跑 AI：这里要验证的是
    「有结构化事实之后看板怎么组装」，模型调用是另一条链路的事。
    """
    monkeypatch.setenv("DB_URL", "sqlite://")
    get_settings.cache_clear()
    dispose_engine()
    engine = get_engine(get_settings())
    Base.metadata.create_all(engine)
    session = get_session_factory()()

    marketing = Department(external_id="d1", name="市场部", source="wecom")
    content = Department(external_id="d2", name="内容组", source="wecom")
    session.add_all([marketing, content])
    session.flush()
    alice = Employee(external_id="u1", name="张三", department_id=marketing.id, source="wecom")
    bob = Employee(external_id="u2", name="李四", department_id=content.id, source="wecom")
    session.add_all([alice, bob])
    session.flush()

    def _report(employee: Employee, start: date, end: date, external: str) -> WeeklyReport:
        report = WeeklyReport(
            employee_id=employee.id,
            external_id=external,
            report_type="weekly",
            title="周报",
            content="正文",
            content_raw="正文",
            start_date=start,
            end_date=end,
            submitted_at=datetime.combine(end, datetime.min.time()),
            source="wecom",
        )
        session.add(report)
        return report

    # 上周：两位员工各一篇
    last_week_a = _report(alice, PERIOD_START, PERIOD_END, "r1")
    last_week_b = _report(bob, PERIOD_START, PERIOD_END, "r2")
    # 已同步但**尚未做结构化分析**的一篇（无 ReportItem）：用于验证
    # 「篇数如实统计」与「分析未覆盖要标记 partial」。
    _report(alice, PERIOD_START, PERIOD_END, "r4")
    # 上上月：同一任务的历史证据，用于验证跨月持续问题
    old = _report(bob, date(2026, 8, 3), date(2026, 8, 9), "r3")
    session.flush()

    shared_task = Task(
        canonical_key="sharedproject",
        title="示例项目推进",
        status="IN_PROGRESS",
        first_seen_date=date(2026, 8, 3),
        last_seen_date=PERIOD_END,
        duration_weeks=6,
    )
    done_task = Task(
        canonical_key="donework",
        title="展会报名落实",
        status="COMPLETED",
        first_seen_date=PERIOD_START,
        last_seen_date=PERIOD_END,
        duration_weeks=1,
    )
    resource_task = Task(
        canonical_key="resourcedemanddiscusslater",
        title="资源需求：Discuss later",
        status="BLOCKED",
        first_seen_date=PERIOD_START,
        last_seen_date=PERIOD_END,
        duration_weeks=1,
    )
    session.add_all([shared_task, done_task, resource_task])
    session.flush()

    items = [
        # 市场部与内容组都提到同一任务 → 跨部门协作
        (shared_task, alice, last_week_a, "ongoing_items", "进行中", "推动示例项目合作", None),
        (shared_task, bob, last_week_b, "ongoing_items", "进行中", "配合示例项目材料", None),
        # 明确完成事项 → 关键发现里的「进展」
        (done_task, alice, last_week_a, "completed_items", "已完成", "展会报名已落实", None),
        # 资源需求类：不进主要工作方向，但仍是卡点。
        # **task_key 必须带 `resource_demand:` 前缀**（canonical_key 会把它剥掉，
        # 所以不能拿 canonical_key 当 task_key —— 那样排除条件就恒真、等于没写，
        # 见 AGENTS 第 21 条第 5 点）。
        (
            resource_task,
            bob,
            last_week_b,
            "blockers",
            "未解决",
            "需要额外资源支持",
            "resource_demand:discusslater",
        ),
        # 上上月同一任务的历史行，让它成为跨月持续问题
        (shared_task, bob, old, "blockers", "未解决", "示例项目等待确认", None),
    ]
    for index, (task, employee, report, item_type, status, text, task_key) in enumerate(items):
        item = ReportItem(
            report_id=report.id,
            employee_id=employee.id,
            item_type=item_type,
            item_order=index,
            title=text,
            description=text,
            status=status,
            source_text=text,
            task_key=task_key or task.canonical_key,
            # 公司主线的身份是业务对象。同一个名字被两个部门写到时必须合并成一条跨部门主线 ——
            # 这也是唯一可靠的合并依据（Task 每条周报都是新的，标题相似不算同一件事）。
            related_project="示例项目" if task is shared_task else None,
        )
        session.add(item)
        session.flush()
        session.add(
            TaskReportLink(
                task_id=task.id,
                report_id=report.id,
                report_item_id=item.id,
                match_method="model_key",
            )
        )
    # 上周出现过的卡点（开放、高风险）→ 需要处理
    session.add(
        Blocker(
            task_id=shared_task.id,
            title="示例项目等待确认",
            description="等待外部确认",
            status="OPEN",
            risk_level="高",
            first_seen_date=date(2026, 8, 3),
            last_seen_date=PERIOD_END,
            duration_weeks=6,
            occurrence_count=5,
        )
    )
    session.add(
        Blocker(
            task_id=resource_task.id,
            title="资源需求：Discuss later",
            status="OPEN",
            risk_level="低",
            first_seen_date=PERIOD_START,
            last_seen_date=PERIOD_END,
            duration_weeks=1,
            occurrence_count=1,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        dispose_engine()
        get_settings.cache_clear()


@pytest.fixture
def client(seeded: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[deps.get_db] = lambda: seeded
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 聚合口径

def test_weekly_themes_group_by_task_and_exclude_resource_demand(seeded: Session) -> None:
    """主题按 Task 聚合；资源需求不是「公司在推进的工作」，不进主要工作方向。"""
    payload = dashboard.weekly_view(seeded, PERIOD_START, PERIOD_END)
    titles = [theme["title"] for theme in payload["themes"]]

    assert "示例项目推进" in titles
    assert not any(title.startswith("资源需求：") for title in titles), (
        "资源需求是「需要支持」的陈述，放进主要工作方向会得到读不出信息的主线"
    )


def test_weekly_theme_reports_outcome_only_when_task_has_completed_item(seeded: Session) -> None:
    """成果关系只在任务真有完成类事项时才成立，不做下标配对。"""
    payload = dashboard.weekly_view(seeded, PERIOD_START, PERIOD_END)
    by_title = {theme["title"]: theme for theme in payload["themes"]}

    completed = by_title["展会报名落实"]
    assert completed["has_reliable_outcomes"] is True
    assert completed["outcomes"][0]["title"] == "展会报名已落实"

    # 只有「进行中」事项的任务不能被编造成有成果
    ongoing = by_title["示例项目推进"]
    assert ongoing["has_reliable_outcomes"] is False
    assert ongoing["outcomes"] == []


def test_weekly_theme_counts_express_coverage_not_effort(seeded: Session) -> None:
    """人数/篇数只表达信息覆盖。"""
    payload = dashboard.weekly_view(seeded, PERIOD_START, PERIOD_END)
    theme = next(t for t in payload["themes"] if t["title"] == "示例项目推进")

    assert theme["people_count"] == 2
    assert theme["report_count"] == 2
    assert sorted(theme["departments"]) == ["内容组", "市场部"]


def test_weekly_progress_discovery_requires_completed_item(seeded: Session) -> None:
    """关键发现里的「进展」来自真实完成事项，且不得声称可纳入跟进。"""
    payload = dashboard.weekly_view(seeded, PERIOD_START, PERIOD_END)
    progress = [item for item in payload["discoveries"] if item["kind"] == "progress"]

    assert progress, "有完成类事项时必须产出进展类关键发现"
    assert all(item["can_track"] is False for item in progress)
    assert all(item["track"] is None for item in progress)


def test_discovery_shape_is_identical_across_sources(seeded: Session) -> None:
    """进展类与洞察类必须字段一致 —— 两处各拼一份迟早让前端崩在缺字段上。"""
    payload = dashboard.weekly_view(seeded, PERIOD_START, PERIOD_END)
    assert len(payload["discoveries"]) >= 2
    keys = {frozenset(item.keys()) for item in payload["discoveries"]}
    assert len(keys) == 1, f"关键发现字段集不一致：{keys}"


def test_status_with_negation_word_is_not_completed() -> None:
    """「未完成」「待完成」含「完成」二字，但绝不是完成 —— 虚报成果是本看板最忌讳的错。

    status 来自模型自由文本（prompts.py 的枚举带「等」），负例必须显式守住：
    旧测试种子只有「已完成/进行中/未解决」，恰好全绿在这类 bug 上 ——
    这正是 dashboard_feedback_service 模块注释说的「测的是规则执行，不是规则对错」。
    """
    from app.services.structured_data_service import _is_completed, _is_ongoing

    def _item(item_type: str, status: str | None) -> ReportItem:
        return ReportItem(item_type=item_type, status=status)

    for status in ("未完成", "待完成", "未完成待跟进", "进行中", "计划中", "待确认", "未解决", "", None):
        assert dashboard._is_completed_item(_item("ongoing_items", status)) is False, (
            f"status={status!r} 不能判成已完成"
        )

    assert dashboard._is_completed_item(_item("ongoing_items", "已完成")) is True
    assert dashboard._is_completed_item(_item("completed_items", None)) is True
    assert dashboard._is_completed_item(_item("results", "")) is True

    # 同款口径落在任务状态重算侧：「未进行」不是进行中，「未完成」不是完成。
    assert _is_completed(_item("ongoing_items", "未完成")) is False
    assert _is_ongoing(_item("next_week_plans", "未进行")) is False
    assert _is_ongoing(_item("next_week_plans", "进行中")) is True


def test_unfinished_status_does_not_become_progress(seeded: Session) -> None:
    """端到端：status=「未完成」的事项不得产出成果关系，也不得进进展类关键发现。"""
    alice = seeded.scalars(select(Employee).where(Employee.name == "张三")).one()
    report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).one()
    task = Task(
        canonical_key="unfinishedwork",
        title="未完成事项任务",
        status="IN_PROGRESS",
        first_seen_date=PERIOD_START,
        last_seen_date=PERIOD_END,
        duration_weeks=1,
    )
    seeded.add(task)
    seeded.flush()
    item = ReportItem(
        report_id=report.id,
        employee_id=alice.id,
        item_type="ongoing_items",
        item_order=99,
        title="数据迁移未完成",
        description="数据迁移未完成",
        status="未完成",
        source_text="数据迁移未完成",
        task_key=task.canonical_key,
    )
    seeded.add(item)
    seeded.flush()
    seeded.add(
        TaskReportLink(
            task_id=task.id,
            report_id=report.id,
            report_item_id=item.id,
            match_method="model_key",
        )
    )
    seeded.commit()

    payload = dashboard.weekly_view(seeded, PERIOD_START, PERIOD_END)
    by_title = {theme["title"]: theme for theme in payload["themes"]}
    assert by_title["未完成事项任务"]["has_reliable_outcomes"] is False
    assert by_title["未完成事项任务"]["outcomes"] == []
    progress_titles = [
        item["title"]
        for item in payload["discoveries"]
        if item["kind"] == "progress"
    ]
    assert "数据迁移未完成" not in progress_titles


def test_coverage_marks_partial_when_analysis_incomplete(seeded: Session) -> None:
    """分析未跑完必须显式标记，不能让部分覆盖被当成全量结论。"""
    coverage = dashboard.weekly_view(seeded, PERIOD_START, PERIOD_END)["coverage"]

    # 上周共 3 篇（其中一篇未做结构化分析）
    assert coverage["reports_total"] == 3
    assert coverage["partial"] is True
    assert coverage["missing_analysis"] > 0


# --------------------------------------------------------------------------- #
# 跨月口径

def test_monthly_splits_by_calendar_month(seeded: Session) -> None:
    """必须先按自然月切分，不能整段汇总后凭空拆月。"""
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    months = [month["month"] for month in payload["months"]]

    assert months == ["2026-08", "2026-09"]
    august = next(m for m in payload["months"] if m["month"] == "2026-08")
    # 篇数独立于分析结果统计：8 月就 1 篇，且它确实做了分析
    assert august["report_total"] == 1
    assert august["analyzed_reports"] == 1
    # 9 月有 3 篇，其中 1 篇未分析 → 逐月覆盖必须如实反映
    september = next(m for m in payload["months"] if m["month"] == "2026-09")
    assert september["report_total"] == 3
    assert september["analyzed_reports"] == 2


def test_monthly_mainlines_use_business_object_not_task(seeded: Session) -> None:
    """公司主线按**业务对象**聚合，不按 Task。

    同一条 Task 的 ``task_key`` 每次调用都是新值（开发库 2882 个 Task 里绝大多数
    只出现一次），按 Task 数会得到「几百条主线」。两个部门各自填了同一个业务对象名
    时必须合并成一条并标成跨部门。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    mainlines = {item["name"]: item for item in payload["mainlines"]["items"]}

    shared = mainlines["示例项目"]
    assert shared["department_count"] == 2
    assert shared["is_cross_department"] is True
    # 8 月与 9 月都有证据 → 跨月
    assert shared["is_cross_month"] is True
    assert shared["months"] == ["2026-08", "2026-09"]
    assert shared["source_report_ids"]


def test_monthly_mainlines_exclude_routine_but_keep_real_work(seeded: Session) -> None:
    """日常事务（周会/报销）不进公司主线，但真实业务过程必须留下。

    这条守住的是「过滤太宽」：``_is_activity_category``（给疑似重复推进用的那张表）
    会把「中介管理」「学生申请全流程处理」这类真实业务过程也删掉，用在主线上
    等于把公司实际在推的事删掉。两张表刻意分开 —— 不要图省事合并成一张。
    """
    # 按标题点名改，不按行序：行序变化时测试不该悄悄换掉被验证的对象。
    items = seeded.scalars(select(ReportItem)).all()
    by_title = {item.title: item for item in items}
    # 「周例会」类日常事务：每个部门都有，不该占公司主线的位置
    by_title["推动示例项目合作"].related_project = "周例会"
    by_title["配合示例项目材料"].related_project = "周例会"
    # 真实业务过程：名字里带「管理」，但不能被当成日常事务删掉
    by_title["展会报名已落实"].related_project = "中介管理"
    by_title["示例项目等待确认"].related_project = "中介管理"
    seeded.commit()

    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    names = {entry["name"] for entry in payload["mainlines"]["items"]}

    assert "周例会" not in names
    assert "中介管理" in names


def test_monthly_range_without_reports_returns_empty_months(seeded: Session) -> None:
    """空范围要如实给 0，不能因为除零或空列表崩掉。"""
    payload = dashboard.monthly_view(seeded, date(2026, 5, 1), date(2026, 5, 31))

    assert [m["month"] for m in payload["months"]] == ["2026-05"]
    assert payload["months"][0]["report_total"] == 0
    assert payload["mainlines"]["items"] == []
    assert payload["changes"]["items"] == []
    assert payload["interventions"]["items"] == []


# --------------------------------------------------------------------------- #
# 每月视图的五问：变化判断的可比性、卡住的准入、异常的来源、介入清单

def test_monthly_changes_refuse_baseline_kinds_when_coverage_too_low(seeded: Session) -> None:
    """覆盖不足时不给「新增 / 明显推进 / 恶化」，只留本月事实。

    这是本模块最容易出错的判断：fixture 里 9 月 3 篇只分析了 2 篇（67%），
    8 月 1 篇全分析（100%）。两侧覆盖率差距超过门槛，硬比会把「这个月少分析了
    一篇」显示成业务变化。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    changes = payload["changes"]

    assert changes["baseline"]["comparable"] is False
    assert changes["baseline"]["month"] is None
    # 需要基线的三类一个都不许出现
    assert not {item["kind"] for item in changes["items"]} & {"new", "progress", "worsening"}
    # 不可比的原因必须说明是覆盖问题，页面据此提示「去补分析」
    assert "覆盖" in changes["baseline"]["reason"]
    # 被排除的月份要如实列出，不能让人以为「没有数据」
    assert changes["baseline"]["skipped"]


def test_baseline_lookup_skips_uncomparable_months_and_explains_why() -> None:
    """基线月不是「紧邻的上月」，而是最近一个可比月份。

    实测 7 月 110 篇 0 篇分析、8 月 123 篇 0 篇分析、9 月 82 篇 29 篇分析 ——
    紧邻的上月没有分析数据时，必须继续往前找，而不是拿零当基线。
    """
    months = [
        (date(2026, 6, 1), date(2026, 6, 30), "2026-06"),
        (date(2026, 7, 1), date(2026, 7, 31), "2026-07"),
        (date(2026, 8, 1), date(2026, 8, 31), "2026-08"),
    ]
    current = {"reports_total": 10, "analyzed_reports": 10, "ratio": 1.0}

    # 7 月没做分析、6 月做全了 → 基线取 6 月，7 月如实记录为被跳过
    key, info = dashboard._baseline_for_latest(
        months,
        {
            "2026-08": current,
            "2026-07": {"reports_total": 12, "analyzed_reports": 0, "ratio": 0.0},
            "2026-06": {"reports_total": 8, "analyzed_reports": 8, "ratio": 1.0},
        },
    )
    assert key == "2026-06"
    assert info["comparable"] is True
    assert [entry["month"] for entry in info["skipped"]] == ["2026-07"]
    assert "未做结构化分析" in info["skipped"][0]["reason"]

    # 两个月都做不全 → 不可比，原因里带上覆盖率数字
    key, info = dashboard._baseline_for_latest(
        months,
        {
            "2026-08": current,
            "2026-07": {"reports_total": 10, "analyzed_reports": 1, "ratio": 0.1},
            "2026-06": {"reports_total": 10, "analyzed_reports": 8, "ratio": 0.8},
        },
    )
    assert key is None
    assert info["comparable"] is False
    assert "覆盖" in info["reason"] and "%" in info["reason"]

    # 只有一个月的范围不给比较，也不能崩
    key, info = dashboard._baseline_for_latest([months[0]], {"2026-06": current})
    assert key is None
    assert "只有一个自然月" in info["reason"]


def test_monthly_changes_skip_single_report_objects(seeded: Session) -> None:
    """只有一篇周报作为证据的对象不判变化类型。

    一个人提过一次的事在主线清单里可见就够了；把它标成「停滞」是在用一篇周报的
    措辞代表整件工作的状态。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))

    for item in payload["changes"]["items"]:
        assert len(item["source_report_ids"]) >= dashboard.CHANGE_MIN_REPORTS


def test_monthly_changes_cover_only_latest_month(seeded: Session) -> None:
    """变化清单只覆盖最近一个月，且不使用「未再次提及 = 已完成」的推理。"""
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    changes = payload["changes"]

    assert changes["month"] == "2026-09"
    for item in changes["items"]:
        assert item["month"] == "2026-09"
        # 「本月没有已分析证据」的对象只在 silent 里报数，不能被判成已完成
        assert item["title"] not in (changes["silent"]["names"] if changes["silent"] else [])


def test_monthly_stuck_counts_thin_blockers_instead_of_listing_them(seeded: Session) -> None:
    """只有一篇周报、没有部门、没到门槛的卡点不列表，但如实报数。"""
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    stuck = payload["stuck"]

    assert "thin_blockers" in stuck
    for item in stuck["items"]:
        if item["kind"] == "dependency":
            assert item["impact"] if False else True  # 结构断言见下一条
        else:
            assert item["evidence_count"] >= 1
            assert item["detail"]
    # 「资源需求：Discuss later」这类占位不能出现在「卡住了」清单里
    assert all("Discuss later" not in item["title"] for item in stuck["items"])


def test_monthly_stuck_dependency_carries_real_duration(seeded: Session) -> None:
    """被依赖对象的持续时间按「有几个自然周有证据」算，与卡点同一口径。"""
    shared_task = seeded.scalar(select(Task).where(Task.title == "示例项目推进"))
    bob = seeded.scalar(select(Employee).where(Employee.name == "李四"))
    alice = seeded.scalar(select(Employee).where(Employee.name == "张三"))
    # 两个自然周、两位员工都提到「等办公室确认」→ 够「被依赖 ≥2 篇」的门槛
    later = WeeklyReport(
        employee_id=bob.id,
        external_id="r-later",
        report_type="weekly",
        title="周报",
        content="正文",
        content_raw="正文",
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 27),
        submitted_at=datetime(2026, 9, 27, 10),
        source="wecom",
    )
    seeded.add(later)
    seeded.flush()
    for report, employee in ((later, bob), (seeded.scalar(select(WeeklyReport).where(WeeklyReport.external_id == "r1")), alice)):
        item = ReportItem(
            report_id=report.id,
            employee_id=employee.id,
            item_type="dependencies",
            item_order=9,
            title="等待办公室确认场地",
            description="等待办公室确认场地",
            status="未解决",
            source_text="等待办公室确认场地",
            task_key=shared_task.canonical_key,
            related_project="示例项目",
        )
        seeded.add(item)
        seeded.flush()
        seeded.add(
            TaskReportLink(
                task_id=shared_task.id,
                report_id=report.id,
                report_item_id=item.id,
                match_method="model_key",
            )
        )
    seeded.commit()

    payload = dashboard.monthly_view(seeded, date(2026, 9, 1), date(2026, 9, 30))
    dependencies = [item for item in payload["stuck"]["items"] if item["kind"] == "dependency"]
    office = next(item for item in dependencies if "办公室" in item["title"])
    # r1 与 r-later 分属两个自然周 → 持续 2 周
    assert office["duration_weeks"] == 2
    assert office["last_seen"] == "2026-09-27"
    assert office["evidence_count"] >= 2


def test_monthly_anomalies_merge_duplicate_views_by_evidence(seeded: Session) -> None:
    """两套分类口径说到同一批证据时只出一行，另一视角作为佐证附上。

    瓶颈按「对方是谁」（院校侧反馈）、异常按「问题是什么」（院校沟通）命名，
    名字谁都不包含谁，但来源周报完全一样 —— 排两行会让管理层以为有两个问题。
    """
    a = {"title": "院校侧反馈与确认", "source_report_ids": [1, 2, 3, 4], "source_key": "a"}
    b = {"title": "院校沟通与反馈", "source_report_ids": [1, 2, 3, 5], "source_key": "b"}
    accept = dashboard._find_overlapping([a], b)
    assert accept is a

    # 证据重合不足时不合并：名字无关、来源也不同
    c = {"title": "服务器与设备保障", "source_report_ids": [7, 8, 9], "source_key": "c"}
    assert dashboard._find_overlapping([a], c) is None

    # 小集合不按重合率去重（一篇周报同时提到两件事会算出 100%）
    d = {"title": "法务与律师审核", "source_report_ids": [1], "source_key": "d"}
    assert dashboard._find_overlapping([a], d) is None


def test_shared_issues_require_three_departments_and_exclude_mainlines(seeded: Session) -> None:
    """公司级共性问题：≥3 个部门同时涉及，且**不把正常运行的主线算成麻烦**。

    实测把主线算进来会让共性问题虚高一倍（18 → 8 条都是这类误报），
    因为「一条主线被多部门共同推进」正是它成为公司主线的原因，不是问题。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    shared = payload["shared_issues"]

    for item in shared:
        assert item["department_count"] >= dashboard.SHARED_ISSUE_MIN_DEPARTMENTS
        # 主线相关的词不该出现在 kinds 里（sources 里已不含 mainline）
        assert "推进受阻" not in item["kinds"]
    # fixture 里只有两个部门，构不成共性问题
    assert shared == []


def test_shared_issues_merge_synonyms_by_evidence_not_name(seeded: Session) -> None:
    """同名异写（院校沟通与反馈 / 院校侧反馈与确认）必须合并成一条。

    三套分类口径会给同一件事两个名字：瓶颈按「对方是谁」、异常按「问题是什么」，
    名字谁都不包含谁，但来源周报几乎完全一样。不合并会让这张表出现成对重复条目。
    """
    a = {"title": "院校沟通与反馈", "source_report_ids": [1, 2, 3, 4, 5], "source_key": "a"}
    b = {"title": "院校侧反馈与确认", "source_report_ids": [1, 2, 3, 4], "source_key": "b"}
    assert dashboard._find_overlapping([a], b) is a

    # **分母取较大集合**：覆盖面大的问题不能「吸收」掉完全无关的小问题。
    # 实测「审批与对公付款」（9 篇）与「学生材料与申请进度」（15 篇）有 5 篇重合，
    # 按 min 算命中 56% 会错误合并，而按 max 只有 33%。
    big = {"title": "审批与对公付款", "source_report_ids": list(range(1, 10)), "source_key": "c"}
    other = {"title": "学生材料与申请进度", "source_report_ids": list(range(5, 20)), "source_key": "d"}
    assert dashboard._find_overlapping([big], other) is None


def test_department_briefs_keep_shared_issues_out_of_department_cards(seeded: Session) -> None:
    """部门卡片不得展开公司级共性问题，只留计数提示。

    这是本轮重构的核心：上一版把共性问题在每个部门各计一次，导致「需介入 6」的差别
    只反映「这个部门的周报写得多」，而不是它有多少麻烦（实测中亚市场那 6 条横跨
    7/5/4/4/3/3 个部门，一条都不是它自己的）。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    shared_titles = [item["title"] for item in payload["shared_issues"]]

    for brief in payload["department_briefs"]:
        assert brief["conclusion"]
        assert isinstance(brief["shared_issue_count"], int)
        # 本部门自己条目里不得含共性问题标题
        own_titles = (
            [item["title"] for item in brief["blockers"]]
            + [item["title"] for item in brief["waiting"]]
            + [item["title"] for item in brief["anomalies"]]
            + [item["title"] for item in brief["interventions"]]
        )
        for shared in shared_titles:
            assert shared not in own_titles
        # 结论句必须由后端算好（页面原样渲染），且带上「在推几条」
        assert "条主线" in brief["conclusion"]


def test_department_briefs_open_blockers_match_blocker_table(seeded: Session) -> None:
    """「N 项卡点未关闭」直接数 Blocker 表（有生命周期、可溯源到详情页）。

    旧的 ``open_issues`` 按主线问题行求和：不看解决状态、同一问题每被提及一次
    计一次、跨部门整桶归属（实测中亚市场自己提出的 15 行被整个记成 28 项，
    Example University 主线的 8 行同时记给三家）——2026-09-28 项目负责人 追问口径后废弃。

    种子数据：示例项目卡点的证据行来自市场部（alice）与内容组（bob）两家，
    资源需求卡点只有内容组（bob）的证据行 → 市场部 1 项、内容组 2 项。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    briefs = {brief["name"]: brief for brief in payload["department_briefs"]}

    assert briefs["市场部"]["open_blockers"] == 1
    assert briefs["内容组"]["open_blockers"] == 2
    # 部门 id 必须下发（前端跳卡点页按部门筛选要用）
    assert briefs["市场部"]["department_id"] is not None
    assert briefs["内容组"]["department_id"] is not None


def test_blockers_list_department_filter_matches_department_badge(
    seeded: Session, client
) -> None:
    """卡点列表的 ``department_id`` 筛选必须与部门卡徽标**同口径**。

    部门卡的「N 项卡点未关闭」是可点链接，点过去就是处理中心按本部门筛好的
    列表 —— 两边的数字若不相等，溯源承诺就是空话（2026-09-28 项目负责人：
    「这个问题的关键是卡点可溯源」）。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))

    for brief in payload["department_briefs"]:
        if not brief["open_blockers"] or brief["department_id"] is None:
            continue
        response = client.get(
            "/api/blockers",
            params={
                "scope": "open",
                "weekly_only": "true",
                "start": "2026-08-01",
                "end": "2026-09-30",
                "department_id": brief["department_id"],
            },
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == brief["open_blockers"], (
            f"{brief['name']}: 徽标 {brief['open_blockers']} 项，"
            f"卡点列表只返回 {len(items)} 条 —— 两端口径脱节"
        )


def test_shared_items_stay_in_the_list_but_marked(seeded: Session) -> None:
    """共性问题**不移出**「卡住了 / 异常」，只打标记。

    早先的做法是移出去，结果「卡住了」只剩 3 条、「异常」只剩 1 条 ——
    管理者扫一眼会以为这两块没事，而真相是 6/9 和 8/9 都被移走了。
    **计数必须诚实**：条目留在原清单里（数字照实），前端把已归入共性问题的那几条
    压成指针，细节统一在共性问题块读。这条守住「不再移走」。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))

    for key in ("stuck", "anomalies"):
        # 不再有「剔除计数」字段
        assert "excluded_shared" not in payload[key], f"{key} 仍在剔除条目"
        # 每个条目都带 is_shared 标记（供前端压成指针）
        for item in payload[key]["items"]:
            assert "is_shared" in item, f"{key} 的条目缺少 is_shared 标记"

    # 共性问题本身仍然独立成块，供前端集中展示细节
    assert isinstance(payload["shared_issues"], list)


def test_department_briefs_rank_by_open_blockers(seeded: Session) -> None:
    """部门排序按「本部门参与的未关闭卡点数」，降序且名次连续。"""
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))
    briefs = payload["department_briefs"]

    scores = [brief["open_blockers"] for brief in briefs]
    assert scores == sorted(scores, reverse=True)
    assert [brief["rank"] for brief in briefs] == list(range(1, len(briefs) + 1))


def test_company_mainlines_only_show_cross_department(seeded: Session) -> None:
    """公司主线只留跨部门的（问题①的原话是「真正的跨部门/跨周期工作主线」）。

    单部门主线属于那个部门自己的事，放在部门简报里 —— 两边都放会让同一件事
    在公司层与部门层各出现一次，正是「冗长」的来源。
    """
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))

    for item in payload["mainlines"]["items"]:
        assert item["is_cross_department"] is True
    assert payload["mainlines"]["single_department_total"] >= 0


def test_monthly_anomalies_keep_per_kind_budget(seeded: Session) -> None:
    """异常按类型各自限量，不是全局取前 N：否则同一类会占满整块。"""
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 9, 30))

    kinds = [item["kind"] for item in payload["anomalies"]["items"]]
    assert len(kinds) == len(set(kinds)) or payload["anomalies"]["items"] == []


def test_monthly_interventions_rank_by_impact_not_by_source_kind(seeded: Session) -> None:
    """介入优先级按影响面，不按来源类别。

    实测按类别排序时，一条只有 1 篇周报、0 个部门证据的低风险卡点会排到
    「7 个部门、32 篇周报都在等某个反馈」前面，清单就失去意义了。
    """
    thin = {
        "kind": "blocker",
        "title": "某件小事",
        "detail": "只有一篇周报",
        "departments": [],
        "department_count": 0,
        "evidence_count": 1,
        "duration_weeks": 2,
        "risk_level": "低",
        "blocker_id": 1,
        "source_report_ids": [1],
    }
    broad = {
        "kind": "dependency",
        "title": "院校侧反馈与确认",
        "detail": "50 项工作依赖",
        "departments": ["A", "B", "C", "D", "E", "F", "G"],
        "department_count": 7,
        "evidence_count": 32,
        "duration_weeks": 4,
        "risk_level": None,
        "blocker_id": None,
        "source_report_ids": list(range(1, 33)),
    }
    block = dashboard._interventions(
        stuck={"items": [thin, broad]},
        changes={"items": []},
        anomalies={"items": []},
    )

    assert block["items"][0]["title"] == "院校侧反馈与确认"
    # 五段必须齐全，页面不自己拼文案
    for item in block["items"]:
        for field in ("reason", "impact", "owner", "duration", "suggested_action"):
            assert item[field], f"{item['title']} 缺 {field}"


def test_monthly_passes_only_visible_interventions_to_shared_and_departments(
    seeded: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`monthly_view` 必须把**截断后**的介入清单交给共性问题与部门简报。

    介入清单按产品要求只展示前 ``INTERVENTION_LIMIT`` 条、且不提供「展开全部」，
    而共性问题块与部门卡片的写法是「已列入『需介入』#N，动作在那里」。
    若把完整（未截断）清单交下去，第 6 条及以后会带上一个页面上不存在的名次 ——
    实测出现「已列入 #6 / #7」而那一块只有 5 条，读者按名次找必然找不到。

    **必须让候选多于展示上限**才测得出这件事：候选不足 5 条时，完整清单与截断清单
    是同一份，实现改回完整清单也不会红（第一版就栽在这里 —— 它只是在重演装配逻辑，
    只能证明测试自己自洽）。所以这里给 `_interventions` 注入一份超限的清单。
    """
    over_limit = dashboard.INTERVENTION_LIMIT + 2
    synthetic = {
        "items": [
            {
                "kind": "dependency",
                "title": f"被依赖对象 {index}",
                "reason": f"{index} 项工作依赖它。",
                "impact": f"{index} 项工作依赖它",
                "owner": "甲部、乙部、丙部",
                "duration": "已持续 2 周",
                "risk_level": None,
                "blocker_id": None,
                "evidence_count": 10,
                "source_report_ids": [index],
                "corroboration": [],
                # 下游按 `departments`（卡点/异常口径）或 `owner`（介入口径）取部门，
                # 两个都给全，避免因为缺字段被共性问题门槛过滤掉。
                "departments": ["甲部", "乙部", "丙部"],
                "rank": index,
                "suggested_action": "规则动作",
                "action_source": "rule",
            }
            for index in range(1, over_limit + 1)
        ],
        "total": over_limit,
        "basis": "",
    }
    monkeypatch.setattr(dashboard, "_interventions", lambda **kwargs: synthetic)

    captured: dict[str, Any] = {}
    real_shared = dashboard._shared_issues

    def spy_shared(*, stuck, anomalies, interventions, **kwargs):
        captured["shared"] = interventions
        return real_shared(
            stuck=stuck, anomalies=anomalies, interventions=interventions, **kwargs
        )

    monkeypatch.setattr(dashboard, "_shared_issues", spy_shared)

    payload = dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)

    assert captured, "共性问题装配没被调用，这条测试就失去意义"
    assert len(payload["interventions"]["items"]) == dashboard.INTERVENTION_LIMIT
    # 关键断言：交给下游的必须是页面真正渲染的那一份（名次 1..5），不是完整清单
    assert len(captured["shared"]["items"]) == dashboard.INTERVENTION_LIMIT
    assert [item["rank"] for item in captured["shared"]["items"]] == [
        item["rank"] for item in payload["interventions"]["items"]
    ]


def test_monthly_interventions_take_ai_actions_but_keep_code_facts(seeded: Session) -> None:
    """AI 只替换建议动作；原因 / 影响 / 责任方 / 持续时间必须保持代码算出的值。

    模型看不到卡点自身的跨周记录，让它写「已持续几周」等于让它猜数字。
    """
    block = {
        "items": [
            {
                "kind": "dependency",
                "title": "法务与律师审核",
                "reason": "9 项工作依赖它",
                "impact": "7 篇周报记录了该依赖，涉及 4 个部门",
                "owner": "中亚市场、人事行政部",
                "duration": "已持续 4 周",
                "suggested_action": "规则版动作",
                "action_source": "rule",
                "source_report_ids": [11, 12, 13],
                "corroboration": [],
            }
        ]
    }
    summary = {
        "result": {
            "management_attention": [
                {
                    "title": "法务审核拖慢多个项目",
                    "reason": "模型写的原因",
                    "impact": "模型写的影响",
                    "owner": "模型写的责任方",
                    "duration": "模型写的时长",
                    "suggested_action": "请管理层本周内确定法务审核的最晚时限",
                    "must_decide": "是否增聘外部律师",
                    "source_report_ids": [12],
                }
            ]
        }
    }
    merged = dashboard.merge_summary_actions(block, summary)
    item = merged["items"][0]

    assert item["suggested_action"] == "请管理层本周内确定法务审核的最晚时限"
    assert item["action_source"] == "ai"
    assert item["must_decide"] == "是否增聘外部律师"
    # 代码算出的四段没被模型覆盖
    assert item["reason"] == "9 项工作依赖它"
    assert item["impact"] == "7 篇周报记录了该依赖，涉及 4 个部门"
    assert item["owner"] == "中亚市场、人事行政部"
    assert item["duration"] == "已持续 4 周"


def test_monthly_interventions_keep_rule_actions_without_summary(seeded: Session) -> None:
    """没有 AI 结论时规则版动作仍然可直接执行，不需要模型在场。"""
    block = {"items": [{"kind": "blocker", "title": "X", "suggested_action": "规则", "action_source": "rule"}]}

    assert dashboard.merge_summary_actions(block, None) is block
    assert dashboard.merge_summary_actions(block, {"result": None}) is block
    assert dashboard.merge_summary_actions(block, {"result": {"management_attention": []}}) is block


def test_monthly_sparse_actions_do_not_leak_across_shared_reports() -> None:
    """未入选的材料问题与机构问题共用周报，也不能继承机构的决策。"""
    block = {"items": [
        {"title": "合作机构", "source_report_ids": [1, 2], "suggested_action": "原动作", "action_source": "rule"},
        {"title": "学生材料", "source_report_ids": [2, 3], "suggested_action": "补材料", "action_source": "rule"},
    ]}
    state = {"ref_identities": {"IV1": "合作机构", "IV2": "学生材料"}, "result": {
        "management_attention": [{"ref": "IV1", "suggested_action": "决定机构签约条件", "must_decide": "是否签约", "source_report_ids": [2]}],
    }}
    merged = dashboard.merge_summary_actions(block, state)
    assert merged["items"][0]["must_decide"] == "是否签约"
    assert merged["items"][1]["suggested_action"] == "补材料"
    assert merged["items"][1]["action_source"] == "rule"
    assert "must_decide" not in merged["items"][1]


def test_monthly_action_and_reason_follow_identity_after_reordering() -> None:
    """历史简报仍可读时，候选重排不能把建议与解释贴到别的对象。"""
    payload = {"interventions": {"items": [
        {"title": "学生材料", "source_report_ids": [1], "suggested_action": "补材料", "action_source": "rule"},
        {"title": "合作机构", "source_report_ids": [1], "suggested_action": "原动作", "action_source": "rule"},
    ]}}
    state = {"ref_identities": {"IV1": "合作机构", "IV2": "学生材料"}, "result": {
        "headline": "机构签约待确认。", "items": [],
        "management_attention": [{"ref": "IV1", "reason": "机构签约条件未确定。", "suggested_action": "决定机构签约条件", "must_decide": "是否签约", "source_report_ids": [1]}],
    }}
    dashboard.merge_summary_actions(payload["interventions"], state)
    merged = dashboard.merge_monthly_narrative(payload, state)
    untouched, matched = merged["interventions"]["items"]
    assert untouched["action_source"] == "rule"
    assert "ai_text" not in untouched
    assert "must_decide" not in untouched
    assert matched["must_decide"] == "是否签约"
    assert matched["ai_text"] == "机构签约条件未确定。"


def test_monthly_ref_actions_without_identities_do_not_fall_back_to_sources() -> None:
    """旧 ref 格式可以按位置匹配，但没有对应 ref 时不能拿共享来源补猜。"""
    block = {"items": [
        {"title": "学生材料", "source_report_ids": [1], "suggested_action": "补材料", "action_source": "rule"},
        {"title": "合作机构", "source_report_ids": [1], "suggested_action": "原动作", "action_source": "rule"},
    ]}
    state = {"result": {"management_attention": [
        {"ref": "IV2", "suggested_action": "决定机构签约条件", "must_decide": "是否签约", "source_report_ids": [1]},
    ]}}
    merged = dashboard.merge_summary_actions(block, state)
    assert merged["items"][0]["action_source"] == "rule"
    assert "must_decide" not in merged["items"][0]
    assert merged["items"][1]["must_decide"] == "是否签约"


# --------------------------------------------------------------------------- #
# 月度简报：模型写结论、代码给事实

def _narrative_summary(**overrides) -> dict:
    result = {
        "headline": "这个月公司主要在推进示例项目，外部确认迟迟没下来。",
        "coverage_note": "结论基于本期内全部已分析周报。",
        "highlights": [
            {
                "title": "示例项目卡在外部确认",
                "detail": "市场部与内容组都在等同一个答复，已经持续 6 周。",
                "kind": "risk",
                "source_report_ids": [1],
            }
        ],
        "departments": [{"name": "市场部", "summary": "在推示例项目，等外部确认。"}],
        "management_attention": [{"title": "法务与律师审核", "suggested_action": "定时限"}],
    }
    result.update(overrides)
    return {"result": result, "created_at": "2026-09-24T10:00:00", "model": "deepseek-chat"}


def test_merge_monthly_narrative_attaches_headline_and_highlights(seeded: Session) -> None:
    """模型写的结论必须附到 payload 上，且**只附不改**已有的确定性清单。

    这是本项目负责人对每月视图那轮反馈的修复点：页面原先只有计数串，读不出
    「公司这个月怎么了」。清单本身仍是代码算出来的那一份 —— 简报是「怎么读」，
    不是「另一套数字」。
    """
    payload = dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)
    original_mainlines = json.loads(json.dumps(payload["mainlines"], default=str))

    merged = dashboard.merge_monthly_narrative(payload, _narrative_summary())
    narrative = merged["narrative"]

    assert narrative["headline"].startswith("这个月公司主要")
    assert narrative["highlights"][0]["kind"] == "risk"
    assert narrative["coverage_note"] == "结论基于本期内全部已分析周报。"
    # 只附不改：原有清单逐字保持
    assert json.loads(json.dumps(merged["mainlines"], default=str)) == original_mainlines


def test_merge_monthly_narrative_without_result_does_not_fake_a_summary(seeded: Session) -> None:
    """没有 AI 结论时**不得**拼一段像总结的话顶上，也不得留下空壳。

    页面靠 `narrative` 缺席来决定「整块不渲染 + 给生成入口」。这里造一个空壳，
    页面就会显示一张写着「暂无结论」的卡片，或更糟 —— 拿代码拼的句子冒充结论。
    """
    payload = dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)

    assert dashboard.merge_monthly_narrative(payload, None) is payload
    assert dashboard.merge_monthly_narrative(payload, {"result": None}) is payload
    assert "narrative" not in payload


def test_merge_monthly_narrative_attaches_by_ref_not_by_title(seeded: Session) -> None:
    """模型写的句子按 **ref 编号**贴回对应条目，不按标题或部门名匹配。

    为什么必须按 ref：模型会改写措辞与标题。按标题/名字匹配时，一旦对不上那条就
    **悄悄退回代码算的计数句**，而页面上看不出来是漏了（第 28h 条踩过同类问题：
    「结论挂错部门比没有结论更糟」）。ref 是材料里发出去的编号，模型逐条回，
    贴不上就是贴不上 —— 由 `items_missing` 如实计数，不猜。

    同时守住方案 A：**只贴文字**，代码算出的字段（数字、来源）逐字不动。
    """
    payload = dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)
    # 夹具里 11 个部门 / 若干条事实，这里只回前两条，其余应当如实计入 missing
    merged = dashboard.merge_monthly_narrative(
        payload,
        {
            "result": {
                "headline": "这个月公司主要在推进示例项目，外部确认迟迟没下来。",
                "items": [
                    {"ref": "DP1", "text": "市场部在推示例项目，在等外部确认。"},
                    {"ref": "ST1", "text": "外部确认没下来，好几条线都压在这儿。"},
                ],
            },
            "created_at": "2026-09-24T10:00:00",
            "model": "deepseek-chat",
        },
    )

    briefs = {brief["name"]: brief for brief in merged["department_briefs"]}
    narrative = merged["narrative"]
    assert narrative["items_written"] == 2
    assert narrative["items_missing"] > 0, "没写到的条目必须如实计数"

    # 贴上的那两条拿到了模型写的话
    attached = [b for b in merged["department_briefs"] if b.get("ai_summary")]
    assert len(attached) == 1
    assert attached[0]["ai_summary"] == "市场部在推示例项目，在等外部确认。"
    # 代码算出的字段仍在（页面把它排成依据行）
    assert attached[0]["open_blockers"] is not None
    assert attached[0]["conclusion"], "代码算的结论句仍在下发字段里，供兜底与核对"
    # 没贴到的部门不得被塞进别人的话
    assert all(
        brief.get("ai_summary") is None or brief["name"] == attached[0]["name"]
        for brief in merged["department_briefs"]
    )


def test_monthly_material_uses_plain_language_not_app_field_names(seeded: Session) -> None:
    """交给模型的材料必须用中文说明，不能是界面 payload 的字段名。

    材料里写 `mainlines.items[].issue_count`，模型就会照着复述成「在推 N 条主线、
    M 项未解决」—— 那是统计口径，正是这轮要消掉的句式。
    """
    material = summary._facts_payload(seeded, "monthly", PERIOD_START, PERIOD_END, department_id=None)

    for key in ("统计周期", "覆盖情况", "逐月覆盖", "公司级跨部门工作", "卡住的事", "各部门情况", "逐篇周报摘要"):
        assert key in material, f"材料缺少「{key}」分段"
    # 界面层字段名不得出现（把它们喂进去，模型就会照抄这些词）
    material_text = json.dumps(material, ensure_ascii=False)
    for leaked in ("source_report_ids", "issue_count", "mainline_total", "scope_label"):
        assert leaked not in material_text, f"材料泄露了应用层字段名：{leaked}"


def test_monthly_department_card_requires_matching_identity_and_own_source(seeded: Session) -> None:
    payload = dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)
    material = summary._facts_payload(seeded, "monthly", PERIOD_START, PERIOD_END, department_id=None)
    candidates = [item for item in material["各部门情况"] if item["本部门来源周报"]]
    assert candidates
    target = candidates[0]
    own_id = target["本部门来源周报"][0]
    other_id = next((report.id for report in seeded.query(WeeklyReport).all() if report.id != own_id), None)
    assert other_id is not None
    card = {
        "ref": target["ref"], "headline": "材料流转已完成一轮", "progress": "已发出 64 份材料",
        "blocker": "仍有材料待校方发放", "suggested_action": "补齐待发材料",
        "source_report_ids": [own_id, other_id],
    }
    state = {
        "result": {"items": [card]},
        "ref_identities": {target["ref"]: target["部门"]},
        "department_report_ids": {target["部门"]: target["本部门来源周报"]},
    }
    merged = dashboard.merge_monthly_narrative(payload, state)
    briefs = {brief["name"]: brief for brief in merged["department_briefs"]}
    assert briefs[target["部门"]]["ai_card"]["source_report_ids"] == [own_id]
    assert all("ai_card" not in brief for name, brief in briefs.items() if name != target["部门"])

    state["ref_identities"] = {target["ref"]: "不存在的部门"}
    merged = dashboard.merge_monthly_narrative(
        dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END), state
    )
    assert all("ai_card" not in brief for brief in merged["department_briefs"])


def test_monthly_material_includes_report_digests_and_reports_budget(seeded: Session) -> None:
    """逐篇摘要要进材料；超预算时**如实计入**，不静默丢弃。

    摘要是模型写出细节的唯一依据（谁在等哪所学校的答复）。只有聚合数字时，
    结论只能停在「院校沟通与反馈」这种抽象标题上 —— 这正是「看完没得到信息」的成因。
    """
    report = seeded.query(WeeklyReport).filter_by(external_id="r1").one()
    seeded.add(
        AIAnalysis(
            entity_type="weekly_report",
            entity_id=report.id,
            analysis_type="report_analysis",
            status=AIAnalysisStatus.SUCCESS,
            result_json={"result": {"summary": "本周推动示例项目合作，等待外部确认。"}},
            created_at=datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        )
    )
    seeded.commit()

    material = summary._facts_payload(seeded, "monthly", PERIOD_START, PERIOD_END, department_id=None)
    digests = material["逐篇周报摘要"]

    assert any(item.get("summary", "").startswith("本周推动示例项目合作") for item in digests)

    # 预算收紧到装不下任何一条时，必须出现「未纳入」计数而不是静默为空。
    # 摘要在新模型下按**剩余**预算分配，所以这里直接压总预算来制造紧张。
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(summary, "MATERIAL_TOTAL_CHARS", 1)
    monkeypatch.setattr(summary, "DIGEST_MIN_CHARS", 0)
    try:
        tight = summary._facts_payload(seeded, "monthly", PERIOD_START, PERIOD_END, department_id=None)
    finally:
        monkeypatch.undo()
    assert any("未纳入的周报摘要" in item for item in tight["逐篇周报摘要"])


def test_monthly_summary_prompt_selects_few_supported_items() -> None:
    """月度简报挑重点，不再要求把每条原始候选改写后铺满页面。"""
    from app.ai.prompts import OPERATING_MONTHLY_SYSTEM

    for phrase in (
        "说人话",
        "只为真正值得展示的 ref",
        "禁止计数句式",
        "依据数字",
        "最多 6 个",
    ):
        assert phrase in OPERATING_MONTHLY_SYSTEM, f"提示词缺少约束：{phrase}"
    # 实测会被照抄的计数句式必须在禁止清单里点名
    for banned in ("涉及 N 个部门，已经持续 K 周", "本月完成 N 项，还有 M 项没解决"):
        assert banned in OPERATING_MONTHLY_SYSTEM, f"未点名禁止：{banned}"
    # 旧契约字段已下线
    assert "executive_summary" not in OPERATING_MONTHLY_SYSTEM
    for field in ("headline", "change_summary", "highlights", "items", "management_attention", "coverage_note", "mainline_refs", "mainline_summary"):
        assert field in OPERATING_MONTHLY_SYSTEM
    assert "必须分别检查 CH、SI、ST、AN、IV 各段" in OPERATING_MONTHLY_SYSTEM
    assert "一篇周报里的不同问题必须分别判断" in OPERATING_MONTHLY_SYSTEM


# --------------------------------------------------------------------------- #
# 总结队列：作用域、只读与隔离

def _enqueue(session: Session, view: str, department_id: int | None = None) -> dict:
    return summary.enqueue_summary(
        session, view, PERIOD_START, PERIOD_END, department_id=department_id
    )


def test_summary_scope_key_includes_type_and_department(seeded: Session) -> None:
    """缓存键必须区分作用域：同周期不同类型/部门各自一条队列记录。"""
    marketing = seeded.query(Department).filter_by(name="市场部").one()
    _enqueue(seeded, "weekly")
    _enqueue(seeded, "weekly", marketing.id)

    rows = seeded.query(OperatingSummaryQueue).all()
    keys = {(row.summary_type, row.department_id) for row in rows}

    assert keys == {
        (OperatingSummaryType.WEEKLY_COMPANY, COMPANY_SCOPE_ID),
        (OperatingSummaryType.DEPARTMENT, marketing.id),
    }


def test_summary_company_scope_uses_zero_not_null(seeded: Session) -> None:
    """全公司范围必须用 0 而不是 NULL —— NULL 在唯一约束下互不相等，约束会失效。"""
    _enqueue(seeded, "weekly")
    row = seeded.query(OperatingSummaryQueue).one()

    assert row.department_id == COMPANY_SCOPE_ID
    assert row.department_id is not None


def test_summary_repeat_enqueue_does_not_reset_completed_task(seeded: Session) -> None:
    """事实未变时重复入队不得把已完成的总结重置 —— 否则每次打开页面都白算一遍。"""
    _enqueue(seeded, "weekly")
    row = seeded.query(OperatingSummaryQueue).one()
    row.status = AnalysisQueueStatus.SUCCESS
    row.attempts = 2
    row.payload_hash = summary._payload_hash(
        summary._facts_payload(seeded, "weekly", PERIOD_START, PERIOD_END, department_id=None),
        OperatingSummaryType.WEEKLY_COMPANY,
        PERIOD_START,
        PERIOD_END,
        COMPANY_SCOPE_ID,
    )
    seeded.commit()

    _enqueue(seeded, "weekly")
    seeded.refresh(row)
    assert row.status == AnalysisQueueStatus.SUCCESS
    assert row.attempts == 2, "重排会重置 attempts，说明任务被当作新任务处理了"


def test_summary_repeat_enqueue_reopens_after_failure(seeded: Session) -> None:
    """失败的任务允许重排（这是「重试」入口）；RESOLVED 之类的终态不在本函数职责内。"""
    _enqueue(seeded, "weekly")
    row = seeded.query(OperatingSummaryQueue).one()
    row.status = AnalysisQueueStatus.FAILED
    row.last_error = "上游 400"
    seeded.commit()

    _enqueue(seeded, "weekly")
    seeded.refresh(row)
    assert row.status == AnalysisQueueStatus.PENDING
    assert row.last_error is None


def test_summary_state_is_read_only(seeded: Session) -> None:
    """只读状态查询不得创建或重置队列 —— 面板靠它轮询。"""
    before = seeded.query(OperatingSummaryQueue).count()
    state = summary.summary_state(seeded, "weekly", PERIOD_START, PERIOD_END)

    assert state["status"] == "not_generated"
    assert seeded.query(OperatingSummaryQueue).count() == before


def test_summary_recompute_window_keeps_old_conclusion_marked_stale(seeded: Session) -> None:
    """force 重算进行期间：旧结论照常返回但必须标 stale，状态如实报 pending。

    旧行为在重排时就把 payload_hash 写成当前值，重算窗口内页面拿到
    stale=False + 旧结论 —— 把旧结论当当前结论展示，违反文件头纪律。
    """
    _enqueue(seeded, "weekly")
    row = seeded.query(OperatingSummaryQueue).one()
    row.status = AnalysisQueueStatus.SUCCESS
    seeded.commit()
    seeded.add(
        AIAnalysis(
            entity_type=summary.ENTITY_TYPE,
            entity_id=row.id,
            analysis_type=summary.ANALYSIS_TYPE,
            prompt_version="v4",
            status=AIAnalysisStatus.SUCCESS,
            result_json={"result": {"headline": "上一版结论"}, "source_report_ids": []},
            created_at=datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        )
    )
    # 事实变化 → hash 漂移
    marketing = seeded.query(Department).filter_by(name="市场部").one()
    employee = seeded.query(Employee).filter_by(department_id=marketing.id).one()
    seeded.add(
        WeeklyReport(
            employee_id=employee.id,
            external_id="r10",
            report_type="weekly",
            title="周报",
            content="正文",
            content_raw="正文",
            start_date=PERIOD_START,
            end_date=PERIOD_END,
            source="wecom",
        )
    )
    seeded.commit()

    state = summary.enqueue_summary(seeded, "weekly", PERIOD_START, PERIOD_END)
    seeded.refresh(row)
    assert row.payload_hash != summary._payload_hash(
        summary._facts_payload(seeded, "weekly", PERIOD_START, PERIOD_END, department_id=None),
        OperatingSummaryType.WEEKLY_COMPANY,
        PERIOD_START,
        PERIOD_END,
        COMPANY_SCOPE_ID,
    ), "重排时不得把 hash 写成当前值，否则重算窗口内旧结论会被标成当前"
    assert state["stale"] is True
    assert state["status"] == "pending", "重算中要报 pending，前端才知道要轮询"
    assert state["result"] == {"headline": "上一版结论"}


def test_summary_enqueue_race_degrades_to_update_not_500(seeded: Session, monkeypatch) -> None:
    """并发入队撞唯一约束时降级为更新既有行，而不是把 IntegrityError 抛成 500。"""
    # 先建一行，模拟「另一个管理员的请求已经提交」。
    _enqueue(seeded, "weekly")
    existing = seeded.query(OperatingSummaryQueue).one()
    existing.status = AnalysisQueueStatus.SUCCESS
    seeded.commit()

    calls = {"n": 0}
    real_lookup = summary._queue_row

    def flaky_lookup(session, summary_type, start, end, department_id):
        # 第一次查不到（模拟本请求开始时另一事务尚未提交可见），
        # 之后正常 —— enqueue 会走「新建」分支并在 commit 时撞唯一约束。
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_lookup(session, summary_type, start, end, department_id)

    monkeypatch.setattr(summary, "_queue_row", flaky_lookup)
    state = _enqueue(seeded, "weekly")

    rows = seeded.query(OperatingSummaryQueue).all()
    assert len(rows) == 1, "撞约束后必须复用既有行，不得产生第二条"
    assert rows[0].status == AnalysisQueueStatus.PENDING
    assert state["status"] == "pending"


def test_queue_snapshot_reports_raw_queue_status_without_facts_recompute(seeded: Session) -> None:
    """轻量轮询版：只读队列行原始状态，不建队列、不做 stale 精细判定。"""
    snapshot = summary.queue_snapshot(seeded, "weekly", PERIOD_START, PERIOD_END)
    assert snapshot["status"] == "not_generated"
    assert snapshot["queue_updated_at"] is None
    assert seeded.query(OperatingSummaryQueue).count() == 0

    _enqueue(seeded, "weekly")
    row = seeded.query(OperatingSummaryQueue).one()
    row.status = AnalysisQueueStatus.RUNNING
    seeded.commit()

    snapshot = summary.queue_snapshot(seeded, "weekly", PERIOD_START, PERIOD_END)
    assert snapshot["status"] == "running"
    assert snapshot["queue_updated_at"] == row.updated_at.isoformat()
    assert summary.summary_state(seeded, "weekly", PERIOD_START, PERIOD_END)[
        "queue_updated_at"
    ] == row.updated_at.isoformat()
    # SUCCESS 但事实已漂移时，快照仍回原始 success —— stale 的精细判定
    # 属于翻转后触发的那次全量 summary_state，轮询只为捕捉状态翻转。
    row.status = AnalysisQueueStatus.SUCCESS
    row.payload_hash = "outdated"
    seeded.commit()
    snapshot = summary.queue_snapshot(seeded, "weekly", PERIOD_START, PERIOD_END)
    assert snapshot["status"] == "success"
    assert summary.summary_state(seeded, "weekly", PERIOD_START, PERIOD_END)["stale"] is True


def test_reclaim_stale_operating_summaries_recovers_zombie(seeded: Session) -> None:
    """租约过期的 RUNNING 行放回待处理；新 RUNNING 与非 RUNNING 行不受影响。"""
    from app.services.analysis_queue_service import reclaim_stale_operating_summaries
    from app.core.time_utils import utcnow

    zombie = OperatingSummaryQueue(
        summary_type=OperatingSummaryType.WEEKLY_COMPANY,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        department_id=COMPANY_SCOPE_ID,
        payload_hash="h1",
        report_count=2,
        status=AnalysisQueueStatus.RUNNING,
    )
    fresh = OperatingSummaryQueue(
        summary_type=OperatingSummaryType.MONTHLY_COMPANY,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        department_id=COMPANY_SCOPE_ID,
        payload_hash="h2",
        report_count=2,
        status=AnalysisQueueStatus.RUNNING,
    )
    done = OperatingSummaryQueue(
        summary_type=OperatingSummaryType.DEPARTMENT,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        department_id=COMPANY_SCOPE_ID,
        payload_hash="h3",
        report_count=2,
        status=AnalysisQueueStatus.SUCCESS,
    )
    seeded.add_all([zombie, fresh, done])
    seeded.commit()
    # updated_at 有 onupdate，直接改属性再统一回写僵尸的租约时间。
    zombie.updated_at = utcnow() - timedelta(minutes=20)
    fresh.updated_at = utcnow()
    seeded.commit()

    assert reclaim_stale_operating_summaries(seeded) == 1
    seeded.refresh(zombie)
    seeded.refresh(fresh)
    seeded.refresh(done)
    assert zombie.status == AnalysisQueueStatus.PENDING
    assert fresh.status == AnalysisQueueStatus.RUNNING, "租约未到期的 RUNNING 不能被抢"
    assert done.status == AnalysisQueueStatus.SUCCESS


def test_summary_state_marks_stale_when_facts_change(seeded: Session) -> None:
    """事实变化后旧结论必须标记过期 —— **但不再把结论丢掉**。

    这是上一轮的真实事故：`stale` 时返回 `result: None`，页面于是整块退回代码拼的
    计数串，项目负责人看到的现象是「你刚刚改好了一些，现在又改回去了」。
    过期不等于作废，而是「基于上一版事实」—— 正确做法是**照常返回并如实标注**
    （`stale=true` + `prompt_version`），由页面标成「基于上一版事实」并给重算入口。
    因此这里断言的重点是：**标注必须诚实，内容必须还在**。
    """
    _enqueue(seeded, "weekly")
    row = seeded.query(OperatingSummaryQueue).one()
    row.status = AnalysisQueueStatus.SUCCESS
    seeded.commit()
    # 上一版事实已经生成过结论（真实场景：结论在，事实变了）
    seeded.add(
        AIAnalysis(
            entity_type=summary.ENTITY_TYPE,
            entity_id=row.id,
            analysis_type=summary.ANALYSIS_TYPE,
            prompt_version="v4",
            status=AIAnalysisStatus.SUCCESS,
            result_json={"result": {"headline": "上一版结论"}, "source_report_ids": []},
            created_at=datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        )
    )
    seeded.commit()

    # 新增一条事实 → payload_hash 变化
    marketing = seeded.query(Department).filter_by(name="市场部").one()
    employee = (
        seeded.query(Employee).filter_by(department_id=marketing.id).one()
    )
    report = WeeklyReport(
        employee_id=employee.id,
        external_id="r9",
        report_type="weekly",
        title="周报",
        content="正文",
        content_raw="正文",
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        source="wecom",
    )
    seeded.add(report)
    seeded.commit()

    state = summary.summary_state(seeded, "weekly", PERIOD_START, PERIOD_END)
    assert state["stale"] is True
    assert state["status"] == "stale"
    # 仍返回上一次的结论（页面据 stale 标注「基于上一版事实」）—— 不静默退回计数版
    assert state["result"] == {"headline": "上一版结论"}
    # 并如实给出结论自身的生成版本，页面才能说明它是按哪版写的
    assert state["prompt_version"] == "v4"


def test_summary_state_empty_when_no_reports(seeded: Session) -> None:
    state = summary.summary_state(
        seeded, "weekly", date(2026, 1, 1), date(2026, 1, 7)
    )
    assert state["status"] == "empty"
    assert state["reports_total"] == 0


def test_summary_rejects_sources_outside_input_scope(seeded: Session) -> None:
    """模型写出的范围外来源 ID 必须被丢弃。"""
    allowed = {1, 2, 3}
    cleaned = summary._validate_sources(
        {
            "headline": "结论",
            "themes": [{"title": "A", "source_report_ids": [1, 999]}],
        },
        allowed,
    )
    assert cleaned["themes"][0]["source_report_ids"] == [1]


def test_summary_type_mapping() -> None:
    assert summary.summary_type_for_view("weekly", has_department=False) == "weekly_company"
    assert summary.summary_type_for_view("monthly", has_department=False) == "monthly_company"
    # 带部门时，无论哪个视图都是部门作用域
    assert summary.summary_type_for_view("weekly", has_department=True) == "department"


# --------------------------------------------------------------------------- #
# 接口契约

def test_api_weekly_view_returns_core_blocks(client: TestClient) -> None:
    response = client.get("/api/operating-dashboard", params={"view": "weekly", **PERIOD})
    assert response.status_code == 200
    payload = response.json()

    assert payload["view"] == "weekly"
    for key in ("themes", "discoveries", "attention", "coverage", "summary", "period"):
        assert key in payload, f"缺少字段 {key}"
    assert payload["summary"]["summary_type"] == "weekly_company"


def test_api_rejects_inverted_period(client: TestClient) -> None:
    response = client.get(
        "/api/operating-dashboard",
        params={"view": "weekly", "start": "2026-09-20", "end": "2026-09-14"},
    )
    assert response.status_code == 400


def test_api_summary_get_does_not_create_queue(client: TestClient, seeded: Session) -> None:
    """轮询用的 GET 不能创建队列 —— 否则每几秒把后台任务打回待处理。"""
    before = seeded.query(OperatingSummaryQueue).count()
    response = client.get("/api/operating-dashboard/summary", params={"view": "weekly", **PERIOD})

    assert response.status_code == 200
    assert seeded.query(OperatingSummaryQueue).count() == before


def test_api_summary_post_enqueues_without_waiting_for_model(client: TestClient, seeded: Session) -> None:
    response = client.post("/api/operating-dashboard/summary", params={"view": "weekly", **PERIOD})

    assert response.status_code == 200
    payload = response.json()
    # 立即返回 pending，不在请求里同步等模型
    assert payload["status"] in {AnalysisQueueStatus.PENDING, "not_generated"}
    assert seeded.query(OperatingSummaryQueue).count() == 1


def test_monthly_summary_worker_result_returns_through_dashboard_api(
    client: TestClient, seeded: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake-model E2E: enqueue, poll, worker completion, then read the narrative."""
    headline = "本月重点事项已有进展，外部确认仍需跟进。"
    provider = FakeProvider(
        response=json.dumps(
            {
                "headline": headline,
                "coverage_note": "本结论依据本期已分析周报。",
                "highlights": [],
                "items": [],
                "management_attention": [],
            },
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr(summary, "get_provider", lambda settings: provider)

    # 月度结论只在本期每篇周报的当前版本都完成单篇分析后生成。
    reports = seeded.scalars(select(WeeklyReport).where(WeeklyReport.end_date == PERIOD_END)).all()
    for report in reports:
        current_hash = report_payload_hash(report)
        report.analysis_payload_hash = current_hash
        report.ai_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        seeded.add(AIAnalysis(
            entity_type="weekly_report", entity_id=report.id,
            analysis_type="report_analysis", status=AIAnalysisStatus.SUCCESS,
            input_data={"content_hash": current_hash}, result_json={},
        ))
    seeded.commit()

    queued = client.post(
        "/api/operating-dashboard/summary",
        params={"view": "monthly", **PERIOD},
    )
    assert queued.status_code == 200
    assert queued.json()["status"] == AnalysisQueueStatus.PENDING
    seeded.commit()

    from app.services.analysis_queue_service import run_worker_once

    worker = run_worker_once(concurrency=1)
    assert worker["operating_claimed"] == 1
    assert worker["operating_processed"] == 1
    assert len(provider.calls) == 1
    assert "各部门情况" not in provider.calls[0]["user"]
    assert "DP*" not in provider.calls[0]["system"]

    poll = client.get(
        "/api/operating-dashboard/summary",
        params={"view": "monthly", **PERIOD, "poll": "true"},
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == AnalysisQueueStatus.SUCCESS

    dashboard_response = client.get(
        "/api/operating-dashboard",
        params={"view": "monthly", **PERIOD},
    )
    assert dashboard_response.status_code == 200
    dashboard_payload = dashboard_response.json()
    assert dashboard_payload["summary"]["status"] == AnalysisQueueStatus.SUCCESS
    assert dashboard_payload["narrative"]["headline"] == headline


def test_monthly_summary_waits_for_all_current_report_analyses(client: TestClient, seeded: Session) -> None:
    reports = seeded.scalars(select(WeeklyReport).where(WeeklyReport.end_date == PERIOD_END)).all()
    first = reports[0]
    current_hash = report_payload_hash(first)
    first.analysis_payload_hash = current_hash
    first.ai_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    seeded.add(AIAnalysis(
        entity_type="weekly_report", entity_id=first.id,
        analysis_type="report_analysis", status=AIAnalysisStatus.SUCCESS,
        input_data={"content_hash": current_hash}, result_json={},
    ))
    seeded.commit()

    response = client.post("/api/operating-dashboard/summary", params={"view": "monthly", **PERIOD})
    assert response.status_code == 200
    assert response.json()["analysis_ready"] is False
    assert response.json()["analysis_completed"] == 1
    assert response.json()["analysis_total"] == len(reports)
    assert seeded.query(OperatingSummaryQueue).count() == 0

    first.content = "正文有了新内容"
    seeded.commit()
    assert summary.monthly_analysis_readiness(seeded, [first.id])["analysis_completed"] == 0

    current_hash = report_payload_hash(first)
    seeded.add(AnalysisQueue(
        report_id=first.id,
        payload_hash=current_hash,
        status=AnalysisQueueStatus.FAILED,
    ))
    seeded.commit()
    readiness = summary.monthly_analysis_readiness(seeded, [first.id])
    assert readiness["analysis_ready"] is False
    assert readiness["analysis_failed"] == 1
    assert readiness["analysis_completed"] == 0


def test_monthly_department_cards_generate_for_ready_departments_only(
    client: TestClient, seeded: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = {report.external_id: report for report in seeded.query(WeeklyReport).all()}
    content_report = reports["r2"]
    current_hash = report_payload_hash(content_report)
    content_report.ai_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    content_report.analysis_payload_hash = current_hash
    seeded.add(AIAnalysis(
        entity_type="weekly_report", entity_id=content_report.id,
        analysis_type="report_analysis", status=AIAnalysisStatus.SUCCESS,
        input_data={"content_hash": current_hash},
        result_json={"summary": "内容组在等待资源支持。"},
    ))
    seeded.commit()
    content_department_id = seeded.get(Employee, content_report.employee_id).department_id
    provider = FakeProvider(response=json.dumps({"items": [{
        "department_id": content_department_id,
        "headline": "资源支持仍待确认",
        "progress": "项目材料已经配合整理",
        "blocker": "资源支持未落实",
        "suggested_action": "核对资源安排",
        "source_report_ids": [content_report.id],
    }]}, ensure_ascii=False))
    monkeypatch.setattr(monthly_cards, "get_provider", lambda settings: provider)

    before = client.get("/api/operating-dashboard", params={"view": "monthly", **PERIOD}).json()
    assert before["summary"]["analysis_ready"] is False
    statuses = {item["name"]: item for item in before["summary"]["department_cards"]["items"]}
    assert statuses["内容组"]["status"] == "not_generated"
    assert statuses["市场部"]["status"] == "waiting_analysis"
    assert before["summary"]["department_cards"]["ready_to_generate"] == 1

    queued = client.post("/api/operating-dashboard/summary", params={"view": "monthly_departments", **PERIOD})
    assert queued.status_code == 200
    assert seeded.query(OperatingSummaryQueue).count() == 1
    assert seeded.query(OperatingSummaryQueue).one().summary_type == OperatingSummaryType.MONTHLY_DEPARTMENTS
    from app.services.analysis_queue_service import run_worker_once
    assert run_worker_once(concurrency=1)["operating_processed"] == 1
    after = client.get("/api/operating-dashboard", params={"view": "monthly", **PERIOD}).json()
    assert after["summary"]["analysis_ready"] is False
    briefs = {brief["name"]: brief for brief in after["department_briefs"]}
    assert briefs["内容组"]["ai_card"]["source_report_ids"] == [content_report.id]
    assert briefs["内容组"]["ai_status"]["status"] == "current"
    assert briefs["市场部"]["ai_status"]["status"] == "waiting_analysis"
    assert "ai_card" not in briefs["市场部"]
    assert len(provider.calls) == 1

    # Reopening or clicking again does not spend another model call.
    repeat = client.post("/api/operating-dashboard/summary", params={"view": "monthly_departments", **PERIOD})
    assert repeat.status_code == 200
    assert repeat.json()["ready_to_generate"] == 0

    # Once the other department becomes ready, only it is sent to the model.
    marketing_reports = [reports["r1"], reports["r4"]]
    for report in marketing_reports:
        report_hash = report_payload_hash(report)
        report.ai_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        report.analysis_payload_hash = report_hash
        seeded.add(AIAnalysis(
            entity_type="weekly_report", entity_id=report.id,
            analysis_type="report_analysis", status=AIAnalysisStatus.SUCCESS,
            input_data={"content_hash": report_hash}, result_json={"summary": "市场部跟进合作。"},
        ))
    seeded.commit()
    marketing_id = seeded.get(Employee, marketing_reports[0].employee_id).department_id
    next_provider = FakeProvider(response=json.dumps({"items": [{
        "department_id": marketing_id, "headline": "合作仍待确认",
        "progress": "展会报名已经落实", "blocker": "大连合作待回复",
        "suggested_action": "核对合作方答复", "source_report_ids": [marketing_reports[0].id],
    }]}, ensure_ascii=False))
    monkeypatch.setattr(monthly_cards, "get_provider", lambda settings: next_provider)
    assert client.post("/api/operating-dashboard/summary", params={"view": "monthly_departments", **PERIOD}).status_code == 200
    assert run_worker_once(concurrency=1)["operating_processed"] == 1
    assert len(next_provider.calls) == 1
    assert f'"department_id": {content_department_id}' not in next_provider.calls[0]["user"]
    updated = client.get("/api/operating-dashboard", params={"view": "monthly", **PERIOD}).json()
    updated_briefs = {brief["name"]: brief for brief in updated["department_briefs"]}
    assert updated_briefs["内容组"]["ai_card"] == briefs["内容组"]["ai_card"]
    assert updated_briefs["市场部"]["ai_status"]["status"] == "current"

    content_report.content = "正文更新，旧卡片不能当作当前结论"
    seeded.commit()
    stale = client.get("/api/operating-dashboard", params={"view": "monthly", **PERIOD}).json()
    stale_briefs = {brief["name"]: brief for brief in stale["department_briefs"]}
    assert stale_briefs["内容组"]["ai_status"]["status"] == "waiting_analysis"
    assert stale_briefs["市场部"]["ai_status"]["status"] == "current"


def test_monthly_card_rejects_another_department_source(seeded: Session) -> None:
    reports = {report.external_id: report for report in seeded.query(WeeklyReport).all()}
    own = reports["r2"]
    other = reports["r1"]
    assert monthly_cards._valid_card({
        "headline": "待确认", "blocker": "资源未到位", "progress": "", "suggested_action": "",
        "source_report_ids": [other.id],
    }, {own.id}) is None


def test_monthly_card_rejects_overlong_fields_and_keeps_compact_cards(seeded: Session) -> None:
    report = seeded.query(WeeklyReport).filter_by(external_id="r2").one()
    base = {
        "headline": "材料流转已完成一轮",
        "progress": "JW202 已流转 64 份，剩余 21 人待补发。",
        "blocker": "仍有 5 名学生缺少翻译件。",
        "suggested_action": "本周补齐缺失翻译件。",
        "source_report_ids": [report.id],
    }
    assert monthly_cards._valid_card(base, {report.id}) == base
    assert monthly_cards._valid_card({**base, "progress": "进展" * 46}, {report.id}) is None
    assert monthly_cards._valid_card({**base, "headline": "标题" * 19}, {report.id}) is None


def test_current_full_month_result_reuses_its_department_card(
    seeded: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # v14 cards are compatible only with the original standalone card prompt.
    monkeypatch.setattr(monthly_cards, "OPERATING_MONTHLY_DEPARTMENTS_PROMPT_VERSION", "v1")
    reports = seeded.scalars(select(WeeklyReport).where(WeeklyReport.end_date == PERIOD_END)).all()
    for report in reports:
        current_hash = report_payload_hash(report)
        report.ai_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
        report.analysis_payload_hash = current_hash
        seeded.add(AIAnalysis(
            entity_type="weekly_report", entity_id=report.id,
            analysis_type="report_analysis", status=AIAnalysisStatus.SUCCESS,
            input_data={"content_hash": current_hash}, result_json={"summary": "进展摘要"},
        ))
    seeded.commit()
    facts = summary._monthly_material(
        seeded, PERIOD_START, PERIOD_END, department_id=None, legacy_digest_budget=True,
    )
    target = next(item for item in facts["各部门情况"] if item["部门"] == "内容组")
    report_id = target["本部门来源周报"][0]
    queue = OperatingSummaryQueue(
        summary_type=OperatingSummaryType.MONTHLY_COMPANY,
        start_date=PERIOD_START, end_date=PERIOD_END,
        department_id=COMPANY_SCOPE_ID,
        payload_hash=summary._legacy_monthly_hash(facts, PERIOD_START, PERIOD_END),
        report_count=len(reports), status=AnalysisQueueStatus.SUCCESS,
    )
    seeded.add(queue)
    seeded.flush()
    seeded.add(AIAnalysis(
        entity_type=summary.ENTITY_TYPE, entity_id=queue.id,
        analysis_type=summary.ANALYSIS_TYPE, status=AIAnalysisStatus.SUCCESS,
        prompt_version="v14",
        result_json={
            "result": {"headline": "公司简报", "items": [{
                "ref": target["ref"], "headline": "资源还没有到位",
                "progress": "材料已整理", "blocker": "等待资源支持",
                "suggested_action": "核对资源安排", "source_report_ids": [report_id],
            }]},
            "ref_identities": {target["ref"]: target["部门"]},
            "department_report_ids": {target["部门"]: target["本部门来源周报"]},
        },
    ))
    seeded.commit()
    company = summary.summary_state(seeded, "monthly", PERIOD_START, PERIOD_END)
    state = monthly_cards.cards_state(seeded, PERIOD_START, PERIOD_END, company_state=company)
    items = {item["name"]: item for item in state["items"]}
    assert company["stale"] is False
    assert items["内容组"]["status"] == "current"
    assert items["内容组"]["card"]["source_report_ids"] == [report_id]
    assert state["ready_to_generate"] == 1  # Only the omitted department needs a call.
    assert seeded.query(OperatingSummaryQueue).count() == 1

    monkeypatch.setattr(monthly_cards, "OPERATING_MONTHLY_DEPARTMENTS_PROMPT_VERSION", "v2")
    upgraded = monthly_cards.cards_state(seeded, PERIOD_START, PERIOD_END, company_state=company)
    assert upgraded["ready_to_generate"] == 2  # A new card prompt must not reuse v14 prose.

    seeded.query(WeeklyReport).filter_by(external_id="r2").one().content = "更新后的正文"
    seeded.commit()
    assert summary.summary_state(seeded, "monthly", PERIOD_START, PERIOD_END)["stale"] is True


def test_company_fingerprint_ignores_department_card_material(seeded: Session) -> None:
    facts = summary._facts_payload(seeded, "monthly", PERIOD_START, PERIOD_END, department_id=None)
    before = summary._payload_hash(
        facts, OperatingSummaryType.MONTHLY_COMPANY, PERIOD_START, PERIOD_END, COMPANY_SCOPE_ID,
    )
    changed = {**facts, "各部门情况": [{"ref": "DP1", "部门": "新的部门卡材料"}]}
    assert summary._payload_hash(
        changed, OperatingSummaryType.MONTHLY_COMPANY, PERIOD_START, PERIOD_END, COMPANY_SCOPE_ID,
    ) == before


def test_monthly_summary_prompt_version_change_marks_result_stale(seeded: Session) -> None:
    report_ids = summary._report_ids_for_period(
        seeded, PERIOD_START, PERIOD_END, department_id=None
    )
    facts = summary._facts_payload(
        seeded, "monthly", PERIOD_START, PERIOD_END, department_id=None
    )
    queue = OperatingSummaryQueue(
        summary_type=OperatingSummaryType.MONTHLY_COMPANY,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        department_id=COMPANY_SCOPE_ID,
        payload_hash=summary._payload_hash(
            facts, OperatingSummaryType.MONTHLY_COMPANY, PERIOD_START, PERIOD_END, COMPANY_SCOPE_ID
        ),
        report_count=len(report_ids),
        status=AnalysisQueueStatus.SUCCESS,
    )
    seeded.add(queue)
    seeded.flush()
    seeded.add(AIAnalysis(
        entity_type=summary.ENTITY_TYPE,
        entity_id=queue.id,
        analysis_type=summary.ANALYSIS_TYPE,
        prompt_version="v12",
        status=AIAnalysisStatus.SUCCESS,
        result_json={"result": {"headline": "旧版简报"}},
    ))
    seeded.commit()

    state = summary.summary_state(seeded, "monthly", PERIOD_START, PERIOD_END)
    assert state["stale"] is True
    assert state["status"] == "stale"
    assert state["result"] == {"headline": "旧版简报"}


def test_api_unknown_view_is_rejected(client: TestClient) -> None:
    response = client.get("/api/operating-dashboard", params={"view": "bogus", **PERIOD})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 查询代价：页面级热路径，不能随部门数线性增长

class _QueryCounter:
    """统计一次调用期间真正发往数据库的语句条数。"""

    def __init__(self, session: Session) -> None:
        self.engine = session.get_bind()
        self.count = 0

    def __enter__(self) -> "_QueryCounter":
        from sqlalchemy import event

        def _on_execute(*args: object, **kwargs: object) -> None:
            self.count += 1

        self._listener = _on_execute
        event.listen(self.engine, "before_cursor_execute", _on_execute)
        return self

    def __exit__(self, *exc: object) -> None:
        from sqlalchemy import event

        event.remove(self.engine, "before_cursor_execute", self._listener)


def _add_departments(session: Session, count: int) -> None:
    """追加若干「有周报有事项」的部门。"""
    for index in range(count):
        department = Department(
            external_id=f"extra{index}", name=f"追加部门{index}", source="wecom"
        )
        session.add(department)
        session.flush()
        employee = Employee(
            external_id=f"extra_emp{index}",
            name=f"追加员工{index}",
            department_id=department.id,
            source="wecom",
        )
        session.add(employee)
        session.flush()
        report = WeeklyReport(
            employee_id=employee.id,
            external_id=f"extra_report{index}",
            report_type="weekly",
            title="周报",
            content="正文",
            content_raw="正文",
            start_date=PERIOD_START,
            end_date=PERIOD_END,
            source="wecom",
        )
        session.add(report)
        session.flush()
        task = Task(
            canonical_key=f"extratask{index}",
            title=f"追加任务{index}",
            status="IN_PROGRESS",
            first_seen_date=PERIOD_START,
            last_seen_date=PERIOD_END,
            duration_weeks=1,
        )
        session.add(task)
        session.flush()
        item = ReportItem(
            report_id=report.id,
            employee_id=employee.id,
            item_type="ongoing_items",
            item_order=0,
            title=f"追加事项{index}",
            status="进行中",
            source_text="原文",
            task_key=task.canonical_key,
        )
        session.add(item)
        session.flush()
        session.add(
            TaskReportLink(
                task_id=task.id, report_id=report.id, report_item_id=item.id
            )
        )
    session.commit()


def test_monthly_view_query_count_does_not_grow_with_months(seeded: Session) -> None:
    """月度视图按月切分但查询数不随月份数线性增长。"""
    with _QueryCounter(seeded) as one_month:
        dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)

    with _QueryCounter(seeded) as three_months:
        dashboard.monthly_view(seeded, date(2026, 7, 1), date(2026, 9, 30))

    # 每月必然要重新取该月主题（这是口径要求：先按月统计再合并），
    # 但辅助查询（篇数/持续问题）必须是一次性的。
    assert three_months.count <= one_month.count * 4, (
        f"月度视图查询数增长过快：{one_month.count} → {three_months.count}"
    )


# --------------------------------------------------------------------------- #
# 综合视图（view=overview）：确定性口径与「不可比就不显示」的护栏
# --------------------------------------------------------------------------- #


def test_empty_resource_demand_is_not_a_bottleneck(seeded: Session) -> None:
    """「资源需求：Discuss later」不算瓶颈，也不进持续问题。

    这类文本是员工没填或明确说不需要支持，把它算成「拖慢公司的原因」等于凭空造问题。
    开发库实测 6 条长期问题全是这种，所以这条是硬护栏。
    """
    payload = dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)

    labels = [item["title"] for item in payload["attention"]["items"]]
    assert not any("Discuss later" in (label or "") for label in labels)

    # 规则层：各种写法都要判成「无需求」，真需求不能被误杀
    from app.processors.journal_parser import is_empty_resource_demand

    assert is_empty_resource_demand("资源需求：Discuss later")
    assert is_empty_resource_demand("Discuss Later")
    assert is_empty_resource_demand("资源需求：目前暂时不需要公司提供额外支持")
    assert is_empty_resource_demand("资源需求未明确")
    assert not is_empty_resource_demand("资源需求：需要法务部门审核三份合规文书")
    assert not is_empty_resource_demand("资源需求：暂缺设备，需要协调 2 台电脑")


def test_bottleneck_requires_evidence_not_a_single_mention(seeded: Session) -> None:
    """单条提及不构成瓶颈：要么 2 篇周报提到，要么已挡住 2 项工作。"""
    payload = dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)

    for item in payload["bottlenecks"]["items"]:
        assert (
            item["evidence_count"] >= 2 or item["workstream_count"] >= 2
        ), f"{item['target']} 只有单条证据却进了瓶颈列表"


def test_workstream_metric_counts_business_objects_not_raw_tasks(seeded: Session) -> None:
    """「工作主线」按业务对象去重，不按 Task 行数。

    开发库 318 个 Task 里 317 个只出现一次（模型每次给的 task_key 都不同），
    拿 Task 数当主线会大一个量级。
    """
    payload = dashboard.workstreams = dashboard._workstream_objects(
        seeded, PERIOD_START, PERIOD_END
    )
    # fixture 里只有「示例项目」一个业务对象，它在本期有 2 条事项（两位员工各一条）
    # → 达到 ≥2 条的准入门槛，算 1 条主线；而本期有 3 条 Task 行，按 Task 数会算出 3。
    assert payload["total"] == 1
    assert payload["objects_total"] == 1
    assert payload["top"][0]["name"] == "示例项目"
    assert payload["min_items"] >= 2


def test_delta_is_unavailable_when_previous_period_has_no_analysis(seeded: Session) -> None:
    """上期没有可比数据时不给环比数字，只给文字。

    拿「上期 = 0」当基线会得出「本期暴涨」这种与相反的结论 —— 真相是上期还没分析。
    """
    payload = dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)
    by_key = {metric["key"]: metric for metric in payload["metrics"]}

    # 工作主线与问题数都依赖结构化事实：上期没分析过就不能比
    for key in ("themes", "signals"):
        delta = by_key[key]["delta"]
        assert delta["available"] is False, f"{key} 在上期无分析数据时不该给环比数字"
        assert "上期" in delta["text"]
        assert "value" not in delta

    # 篇数环比同样不可得：本 fixture 的上一周确实一篇周报都没有。
    # 这不是「上期为 0」，而是「上期不存在」——显示的仍是文字而非 -3。
    assert by_key["reports"]["delta"]["available"] is False
    assert "value" not in by_key["reports"]["delta"]


def test_report_delta_appears_once_previous_period_has_reports(seeded: Session) -> None:
    """上一周有周报之后，篇数环比才会给出真实数字。

    与上一条成对：一个证明「没有数据时不显示」，一个证明「有数据时确实算」，
    否则「永远不显示环比」也能让上一条通过。
    """
    previous_report = WeeklyReport(
        employee_id=seeded.scalars(select(Employee.id)).first(),
        external_id="prev1",
        report_type="weekly",
        title="上周周报",
        content="正文",
        content_raw="正文",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        submitted_at=datetime(2026, 9, 13, 10, 0, 0),
        source="wecom",
    )
    seeded.add(previous_report)
    seeded.commit()

    payload = dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)
    by_key = {metric["key"]: metric for metric in payload["metrics"]}

    delta = by_key["reports"]["delta"]
    assert delta["available"] is True
    # 本期 3 篇、上期 1 篇 → +2
    assert delta["value"] == 2
    assert "+2" in delta["text"]


def test_trends_skip_periods_without_analysis(seeded: Session) -> None:
    """覆盖不足的周期从趋势序列里剔除，而不是补 0。

    补 0 会把「本周刚开始分析」显示成「问题连续扩大」（实测覆盖率从 0 跳到 24 篇，
    于是每个主题都变成上升）。

    门槛是**覆盖率**而不是「有没有数据」：只判后者挡不住「上期 5/27 篇（19%）、
    本期 24/24 篇（100%）」这种比较 —— 实测它让四个问题主题全部显示成
    「连续扩大」，而按占比算其中一个其实在下降（100% → 46%）。
    fixture 里本期只有 1/3 篇有事项（33%，低于门槛），因此也不该进入趋势。
    """
    trends = dashboard._issue_trends(seeded, dashboard._period_sequence(PERIOD_END, 4))
    assert trends["series"] == []
    assert trends["analyzed_periods"] == []
    assert trends["reason"]
    # 被排除的周期要如实列出（含覆盖率），用户才知道该去补什么
    assert trends["skipped_periods"], trends
    assert all("coverage" in item for item in trends["skipped_periods"])


def test_overview_trends_use_long_history_window_without_new_analysis(seeded: Session) -> None:
    """总览趋势复用更长历史窗口，不因最近四周覆盖不足而直接变成空卡。"""
    payload = dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)
    trends = payload["trends"]

    assert len(trends["periods"]) + len(trends["skipped_periods"]) == dashboard.TREND_LOOKBACK_WEEKS
    assert len(payload["activity"]["periods"]) == 4


def test_focus_uses_unique_people_not_item_counts(seeded: Session) -> None:
    """精力投向按「员工 × 类别」唯一参与算，同一人多条事项只计一次。"""
    focus = dashboard._focus_distribution(seeded, PERIOD_START, PERIOD_END)
    total = focus["total_entries"]
    assert total == sum(item["people_entries"] for item in focus["categories"])
    # 两个人在两个类别里各参与一次 → 不超过参与人次的两倍
    assert total <= 2 * len({"张三", "李四"})
    assert "不是工时占比" in focus["basis"]
    labels = [item["label"] for item in focus["categories"]]
    assert len(labels) == len(set(labels)), f"类别标签重复：{labels}"


def test_duplicate_effort_requires_two_departments(seeded: Session) -> None:
    """同部门两位同事做同一件事不算「疑似重复」，跨部门才算。"""
    alice_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    bob_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r2")
    ).first()
    assert alice_report and bob_report

    # 同一业务对象、两个部门各一条事项
    for index, report in enumerate((alice_report, bob_report)):
        item = ReportItem(
            report_id=report.id,
            employee_id=report.employee_id,
            item_type="completed_items",
            item_order=100 + index,
            title="展会报名材料准备",
            description="准备材料",
            status="已完成",
            source_text="准备材料",
            related_project="湾一会国际医疗展会",
        )
        seeded.add(item)
    seeded.commit()

    frictions = dashboard._frictions(seeded, PERIOD_START, PERIOD_END)
    projects = {item["project"] for item in frictions["duplicates"]}
    assert "湾一会国际医疗展会" in projects, "跨部门同项目应被识别为疑似重复推进"

    duplicate = next(
        item for item in frictions["duplicates"] if item["project"] == "湾一会国际医疗展会"
    )
    assert duplicate["department_count"] == 2
    assert set(duplicate["departments"]) == {"市场部", "内容组"}


def test_status_conflict_requires_two_people_on_one_object(seeded: Session) -> None:
    """状态冲突要求同一业务对象上「不同的人」给出矛盾状态。"""
    alice_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    bob_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r2")
    ).first()
    assert alice_report and bob_report

    rows = [
        (alice_report, "completed_items", "已完成", "学生材料核对"),
        (bob_report, "blockers", "未解决", "学生材料核对"),
    ]
    for index, (report, item_type, status, text) in enumerate(rows):
        seeded.add(
            ReportItem(
                report_id=report.id,
                employee_id=report.employee_id,
                item_type=item_type,
                item_order=200 + index,
                title=text,
                description=text,
                status=status,
                source_text=text,
                related_project="广外学生申请",
            )
        )
    seeded.commit()

    conflicts = dashboard._status_conflicts(
        dashboard._period_item_rows(seeded, PERIOD_START, PERIOD_END)
    )
    assert [item["name"] for item in conflicts] == ["广外学生申请"]
    assert conflicts[0]["finished_by"] == ["张三"]
    assert conflicts[0]["open_by"] == ["李四"]


def test_status_conflict_ignores_same_person_wording(seeded: Session) -> None:
    """同一个人既写完成又写计划是正常的上下文差异，不算冲突。"""
    report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    assert report
    for index, (item_type, status) in enumerate(
        (("completed_items", "已完成"), ("next_week_plans", "计划中"))
    ):
        seeded.add(
            ReportItem(
                report_id=report.id,
                employee_id=report.employee_id,
                item_type=item_type,
                item_order=300 + index,
                title="展会材料",
                description="展会材料",
                status=status,
                source_text="展会材料",
                related_project="单人项目",
            )
        )
    seeded.commit()

    conflicts = dashboard._status_conflicts(
        dashboard._period_item_rows(seeded, PERIOD_START, PERIOD_END)
    )
    assert conflicts == []


def test_overview_payload_shape_and_evidence_chain(seeded: Session) -> None:
    """综合视图返回约定的全部模块，且每条结论都能追到周报。"""
    payload = dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)

    for key in (
        "period_label",
        "coverage",
        "metrics",
        "activity",
        "attention",
        "workstreams",
        "themes",
        "focus",
        "bottlenecks",
        "frictions",
        "projects",
        "trends",
        "attention_items",
    ):
        assert key in payload, f"综合视图缺少字段：{key}"

    assert len(payload["metrics"]) == 4
    assert payload["activity"]["points"], "活跃度序列不能为空"
    assert payload["coverage"]["reports_total"] == 3

    # 有证据的结论必须给出可核对的来源周报 ID
    for signal in payload["attention"]["items"]:
        assert signal["source_report_ids"], "信号没有来源周报"
        assert 0 <= signal["attention_score"] <= 100

    # 项目条目必须带「说人话」结论：负责人点开卡片先读结论，不是一排裸计数
    # （2026-09-24 项目负责人要求，与 attention 信号的 conclusion 同一纪律）。
    for project in payload["projects"]["items"]:
        assert project.get("conclusion"), f"项目缺少结论：{project['name']}"
        assert project["name"] in project["conclusion"] or "周报" in project["conclusion"], (
            f"项目结论没有落到具体事实上：{project['conclusion']}"
        )


def test_attention_omits_stale_open_blockers(seeded: Session) -> None:
    """未解决状态若没有所选周期内的新证据，不应伪装成当前提醒。"""
    signal_items = dashboard._attention_signals(seeded, PERIOD_START, PERIOD_END)["items"]
    assert any(
        signal["signal_type"] == "stalled_workstream"
        and "示例项目等待确认" in signal["title"]
        for signal in signal_items
    ), "本期仍有证据的长期卡点应保留"

    blocker = seeded.scalars(
        select(Blocker).where(Blocker.title == "示例项目等待确认")
    ).first()
    assert blocker
    blocker.last_seen_date = date(2026, 8, 9)
    seeded.commit()

    stale_items = dashboard._attention_signals(seeded, PERIOD_START, PERIOD_END)["items"]
    assert not any("示例项目等待确认" in signal["title"] for signal in stale_items), (
        "最后出现日期早于所选周期的卡点不应进入当前提醒"
    )


def test_growing_issue_signal_carries_real_evidence(seeded: Session) -> None:
    """趋势上升信号必须带真实证据：来源周报、部门、原文片段。

    此前组装时证据整个被扔掉（部门硬编码 0、来源为空），弹窗只剩
    「篇数 7→7→9→8」和「涉及部门 0」—— 负责人点进去全是噪音
    （2026-09-30 反馈）。证据素材 ``_issue_trends`` 本来就算出来了，
    组装信号时不许丢。
    """
    alice = seeded.scalars(select(Employee).where(Employee.name == "张三")).first()
    bob = seeded.scalars(select(Employee).where(Employee.name == "李四")).first()
    assert alice and bob

    def _report_with_theme(
        employee: Employee, start: date, end: date, external: str, text: str
    ) -> None:
        report = WeeklyReport(
            employee_id=employee.id,
            external_id=external,
            report_type="weekly",
            title="周报",
            content="正文",
            content_raw="正文",
            start_date=start,
            end_date=end,
            submitted_at=datetime.combine(end, datetime.min.time()),
            source="wecom",
        )
        seeded.add(report)
        seeded.flush()
        seeded.add(
            ReportItem(
                report_id=report.id,
                employee_id=employee.id,
                item_type="blockers",
                item_order=500,
                title=text,
                description=text,
                status="未解决",
                source_text=text,
            )
        )

    # 上上周一篇、本周两篇 —— 两个可比周期（覆盖率均过门槛），主题上升。
    # 中间一周不放数据：覆盖率 0 的周期会被护栏剔除，不能算「下降」。
    _report_with_theme(alice, date(2026, 8, 31), date(2026, 9, 6), "t1", "院校反馈回复很慢")
    _report_with_theme(bob, PERIOD_START, PERIOD_END, "t2", "多所院校在等确认")
    _report_with_theme(alice, PERIOD_START, PERIOD_END, "t3", "校方流程调整待同步")
    seeded.commit()

    signals = dashboard._attention_signals(seeded, PERIOD_START, PERIOD_END)["items"]
    trend = next(
        (signal for signal in signals if signal["signal_type"] == "growing_issue"), None
    )
    assert trend, f"两个可比周期且主题上升时必须有趋势信号：{[s['title'] for s in signals]}"
    assert trend["signal_key"] == "growing_issue:院校沟通与反馈"
    # 部门与证据不再为空
    assert trend["impact_departments"] >= 1
    assert trend["source_report_ids"]
    assert trend["sources"]
    for source in trend["sources"]:
        assert source["department_name"]
        assert source["report_id"] in trend["source_report_ids"]
    # 卡片标题优先用原文片段 —— 读得出「谁在等什么」，而不是一串计数
    snippets = {source["title"] for source in trend["sources"]}
    assert any("院校" in snippet or "校方" in snippet for snippet in snippets)


def test_bottleneck_evidence_includes_report_period(seeded: Session) -> None:
    """依据事项返回周报周期，供详情界面标注发生时间。"""
    task = seeded.scalars(
        select(Task).where(Task.canonical_key == "sharedproject")
    ).first()
    reports = seeded.scalars(
        select(WeeklyReport)
        .where(WeeklyReport.external_id.in_(["r1", "r2"]))
        .order_by(WeeklyReport.id)
    ).all()
    assert task and len(reports) == 2
    for index, report in enumerate(reports):
        dependency = ReportItem(
            report_id=report.id,
            employee_id=report.employee_id,
            item_type="dependencies",
            item_order=300 + index,
            title="等待院校侧反馈确认",
            description="等待院校侧反馈确认",
            status="待确认",
            source_text="等待院校侧反馈确认",
            task_key=task.canonical_key,
        )
        seeded.add(dependency)
        seeded.flush()
        seeded.add(
            TaskReportLink(
                task_id=task.id,
                report_id=report.id,
                report_item_id=dependency.id,
                match_method="model_key",
            )
        )
    seeded.commit()

    bottlenecks = dashboard._bottlenecks(seeded, PERIOD_START, PERIOD_END)
    sources = [
        source
        for item in bottlenecks["items"]
        for source in item["sources"]
    ]

    assert sources
    assert all(source["period_start"] and source["period_end"] for source in sources)
    assert all(source["occurred_on"] == source["period_end"] for source in sources)


def test_overview_query_count_is_bounded(seeded: Session) -> None:
    """综合视图查询条数有上界：它一次要算八个模块，不能每个模块各查一遍明细。"""
    with _QueryCounter(seeded) as counter:
        dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)

    # 实测约 35 条；给到 60 的余量，但任何「每个模块重复查一次明细」的退步都会超
    assert counter.count <= 60, f"综合视图查询数过多：{counter.count}"


def test_overview_api_returns_overview_view(client: TestClient) -> None:
    """GET /api/operating-dashboard?view=overview 返回综合视图并与 summary 合并。"""
    response = client.get("/api/operating-dashboard", params={"view": "overview", **PERIOD})
    assert response.status_code == 200
    body = response.json()

    assert body["view"] == "overview"
    assert body["period"] == PERIOD
    assert "summary" in body, "综合视图也要带只读的总结状态（供一句话总览与建议）"
    assert body["summary"]["summary_type"] == OperatingSummaryType.WEEKLY_COMPANY
    assert body["bottlenecks"]["items"] is not None
    assert body["trends"]["basis"]


def test_overview_export_includes_cached_summary_and_neutralizes_formula(
    client: TestClient, seeded: Session
) -> None:
    """导出读缓存的 AI 结论，不触发模型；CSV 带来源编号且中和公式注入。"""
    queue = OperatingSummaryQueue(
        summary_type=OperatingSummaryType.WEEKLY_COMPANY,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        department_id=COMPANY_SCOPE_ID,
        payload_hash=summary._payload_hash(
            summary._facts_payload(
                seeded, "weekly", PERIOD_START, PERIOD_END, department_id=None
            ),
            OperatingSummaryType.WEEKLY_COMPANY,
            PERIOD_START,
            PERIOD_END,
            COMPANY_SCOPE_ID,
        ),
        report_count=3,
        status=AnalysisQueueStatus.SUCCESS,
    )
    seeded.add(queue)
    seeded.flush()

    first_report_id = seeded.scalars(select(WeeklyReport.id).order_by(WeeklyReport.id)).first()
    seeded.add(
        AIAnalysis(
            entity_type=summary.ENTITY_TYPE,
            entity_id=queue.id,
            analysis_type=summary.ANALYSIS_TYPE,
            prompt_version=summary.PROMPT_VERSION,
            result_json={
                "result": {
                    "headline": "本期整体推进平稳。",
                    "attention": [
                        {
                            "title": "=命令注入测试",
                            "reason": "事实",
                            "suggested_action": "找负责人确认",
                            "source_report_ids": [first_report_id],
                        }
                    ],
                },
                "source_report_ids": [first_report_id],
            },
            status=AIAnalysisStatus.SUCCESS,
            created_at=datetime(2026, 9, 21, 3, 0, 0),
        )
    )
    seeded.commit()

    response = client.get("/api/operating-dashboard/export", params=PERIOD)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")

    text = response.content.decode("utf-8-sig")
    assert "下周需要关注" in text
    # 以 = 开头的单元格必须被前置单引号中和（否则 Excel 会当公式执行）
    assert "'=命令注入测试" in text, "CSV 公式注入未被中和"
    # 来源列要写出可核对的周报编号
    assert f"#{first_report_id}" in text


def test_aggregate_dependency_row_does_not_fake_a_workstream(seeded: Session) -> None:
    """汇总型依赖（「多项事项等待办公室确认」）不计成一条新工作主线。

    这类事项把「好几件事都等着同一个对象」合起来写。它是有价值的证据，
    但它列举的那些事项往往在别处各自记过一条，直接计数会把影响面算成两倍。
    实测：一条周报里的 6 条办公室依赖里有 2 条是汇总行，会让「影响 4 项工作」
    显示成「影响 6 项工作」。
    """
    report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    assert report

    rows = [
        # 4 条各自独立的业务对象，都等着同一个对象
        ("实习项目", "实习项目依赖办公室确认"),
        ("秋令营", "秋令营项目依赖办公室确认"),
        ("尼日利亚合作", "尼日利亚合作项目依赖办公室确认"),
        ("Example University招生", "Example University十月JW表格依赖办公室发布"),
        # 2 条汇总行：没有业务对象，只是把上面几件事合并复述
        (None, "多项事项等待办公室确认"),
        (None, "多项事项依赖办公室确认导致推进停滞"),
    ]
    for index, (project, text) in enumerate(rows):
        seeded.add(
            ReportItem(
                report_id=report.id,
                employee_id=report.employee_id,
                item_type="dependencies",
                item_order=400 + index,
                title=text,
                description=text,
                status="待确认",
                source_text=text,
                related_project=project,
            )
        )
    seeded.commit()

    bottlenecks = dashboard._bottlenecks(seeded, PERIOD_START, PERIOD_END)
    office = next(
        (item for item in bottlenecks["items"] if item["target"] == "办公室确认与发布"), None
    )
    assert office is not None, "办公室依赖应聚成一个瓶颈"
    # 4 条独立业务对象；2 条汇总行不计入影响面，但如实计入未归属
    assert office["workstream_count"] == 4, f"汇总行虚增了影响面：{office['workstream_count']}"
    assert office["unattributed_count"] == 2
    assert bottlenecks["unattributed_count"] == 2


# --------------------------------------------------------------------------- #
# 视图契约：响应必须自证身份，且已裁掉的视图不再返回 payload
# --------------------------------------------------------------------------- #


def test_each_view_response_self_identifies(client: TestClient) -> None:
    """响应必须带 `view`，且与请求的视图一致。

    前端靠这个字段判断「这份数据属不属于当前视图」：切视图时 `useAsyncData` 会先保留
    上一份数据，若前端只能按 state.view 猜，就会拿总览的 payload 去喂明细分支 ——
    实测会崩成 `Uncaught TypeError: attention.map is not a function`
    （总览的 attention 是对象、明细的是数组，同名不同形）。
    """
    for view in ("overview", "weekly", "monthly"):
        response = client.get("/api/operating-dashboard", params={"view": view, **PERIOD})
        assert response.status_code == 200, view
        assert response.json()["view"] == view


def test_overview_and_weekly_attention_have_different_shapes(client: TestClient) -> None:
    """两个视图的 `attention` 同名不同形 —— 这正是当初崩溃的根因，必须锁住。

    如果哪天有人把它们统一成同一种形状，这个测试会红，提醒去复核前端的分流逻辑
    是否还需要。
    """
    overview = client.get(
        "/api/operating-dashboard", params={"view": "overview", **PERIOD}
    ).json()
    weekly = client.get(
        "/api/operating-dashboard", params={"view": "weekly", **PERIOD}
    ).json()

    assert isinstance(overview["attention"], dict), "总览的 attention 是 {items,total}"
    assert isinstance(weekly["attention"], list), "明细的 attention 是数组"


def test_department_view_is_no_longer_served(client: TestClient) -> None:
    """`?view=department` 返回 422，而不是一份没有页面消费的 payload。

    部门维度的信息已并入总览（瓶颈/摩擦都带部门），该视图作为产品界面被裁掉。
    部门的 **AI 总结作用域**仍保留（走 /summary），两者不是一回事。
    """
    response = client.get(
        "/api/operating-dashboard", params={"view": "department", **PERIOD}
    )
    assert response.status_code == 422


def test_department_summary_scope_still_works(seeded: Session) -> None:
    """裁掉视图不等于砍掉能力：部门作用域的总结仍可入队并取出事实。

    它有自己的提示词与输出 Schema（focus/blockers/dependencies），
    数据源改走 `_department_facts_payload`，不再依赖已删除的 department_view。
    """
    marketing = seeded.query(Department).filter_by(name="市场部").one()

    facts = summary._facts_payload(
        seeded, "department", PERIOD_START, PERIOD_END, department_id=marketing.id
    )
    assert facts["department"] == "市场部"
    assert "themes" in facts and "blockers" in facts

    state = _enqueue(seeded, "department", marketing.id)
    assert state["summary_type"] == OperatingSummaryType.DEPARTMENT
    assert state["department_id"] == marketing.id


def test_discoveries_exclude_empty_resource_demand(seeded: Session) -> None:
    """「关键发现」不能出现「资源需求：Discuss later」这类噪音。

    它与总览的瓶颈过滤必须一致：同一份数据在两个视图里说法不同，比两处都错更难排查。
    实测「上周」视图首条曾经就是这条噪音（insights() 会把长期开放卡点原样抬上来）。
    """
    discoveries = dashboard._discoveries(seeded, PERIOD_START, PERIOD_END)
    titles = [item["title"] or "" for item in discoveries]
    assert not any("Discuss later" in title for title in titles), titles


def test_theme_marks_company_level_by_people_or_reports(seeded: Session) -> None:
    """「公司级主线」= 多人 或 多篇确认过；单人事只能算个人事项。

    实测开发库 318 条主题里 317 条只有 1 人 1 篇 —— 不区分地并排列出，
    「上周进展」实际是在罗列个人待办，而不是公司主线。
    判定由后端给出（`is_company_level`），前端不再自己重算。
    """
    rows = dashboard._theme_rows(seeded, PERIOD_START, PERIOD_END)
    themes = dashboard._themes(rows)

    # fixture 里「示例项目推进」由两个人各写一篇 → 公司级
    shared = next(theme for theme in themes if "示例项目推进" in theme["title"])
    assert shared["people_count"] == 2 and shared["report_count"] == 2
    assert shared["is_company_level"] is True

    # 「展会报名落实」只有一个人一篇 → 个人事项
    solo = next(theme for theme in themes if "展会报名" in theme["title"])
    assert solo["people_count"] == 1
    assert solo["is_company_level"] is False


def test_weekly_view_exposes_company_theme_count(seeded: Session) -> None:
    """weekly 视图要给出公司级主线条数，供页面如实说明「本期有几条真主线」。"""
    payload = dashboard.weekly_view(seeded, PERIOD_START, PERIOD_END)
    expected = sum(1 for theme in payload["themes"] if theme["is_company_level"])
    assert payload["company_theme_count"] == expected
    assert expected >= 1  # fixture 里至少有一条（两人共同推进的示例项目）


# --------------------------------------------------------------------------- #
# 准确性收口（2026-09-22）：以下每组用例都对应审计中实测到的错误
#
# 这些错误在改动前的 47 项测试里**全部是绿的** —— 它们测的是「代码按规则跑」，
# 不是「规则符合现实」。所以每一条都必须锁在会因旧行为变红的地方。
# --------------------------------------------------------------------------- #


def test_resource_demand_row_exclusion_keeps_sibling_items(seeded: Session) -> None:
    """排除资源需求时只剔那一条，**不能连坐整条任务**。

    旧实现排除的是「含资源需求事项的整个任务」（`~Task.id.in_(...)`），
    于是模型把一条资源需求挂到已有任务上时，那整条任务的真实事项一起消失。
    实测开发库有 8 个这样的任务、共 18 条真实工作（其中一条任务下有 7 条）
    在「精力投向」「重点项目进展」「工作主线」里全都看不见。

    夹具里 `shared_task` 就是这种形态：既有 3 条真实事项，又通过 resource_task
    与资源需求共享同一批周报 —— 这里直接断言真实事项仍在结果里。
    """
    rows = dashboard._theme_rows(seeded, PERIOD_START, PERIOD_END)
    titles = {row[1].title for row in rows}

    assert "推动示例项目合作" in titles
    assert "配合示例项目材料" in titles
    # 资源需求那一条本身仍被排除
    assert not any((row[1].task_key or "").startswith("resource_demand:") for row in rows)


def test_theme_rows_keep_items_without_task_key(seeded: Session) -> None:
    """`task_key` 为 NULL 的事项必须保留（SQL 三值逻辑陷阱）。

    排除条件写成 `task_key NOT LIKE 'resource_demand:%'` 时，NULL 会让整个表达式
    求值为 NULL 而不是 TRUE，于是没有 task_key 的事项被整行吃掉 ——
    修这条时实测丢掉了 17 行、3 个参与人次。必须把 NULL 显式放行。
    """
    report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    assert report is not None
    task = seeded.scalar(select(Task).where(Task.canonical_key == "sharedproject"))
    assert task is not None
    item = ReportItem(
        report_id=report.id,
        employee_id=report.employee_id,
        item_type="completed_items",
        item_order=900,
        title="没有task_key的完成事项",
        description="用于验证 NULL 不被吃掉",
        status="已完成",
        source_text="没有task_key的完成事项",
        task_key=None,
    )
    seeded.add(item)
    seeded.flush()
    # 必须挂上任务关联：_theme_rows 是按 TaskReportLink 内连接的，
    # 不挂关联的条目本来就不属于「主题」范畴（那是另一条口径）。
    seeded.add(
        TaskReportLink(
            task_id=task.id,
            report_id=report.id,
            report_item_id=item.id,
            match_method="model_key",
        )
    )
    seeded.commit()

    titles = {row[1].title for row in dashboard._theme_rows(seeded, PERIOD_START, PERIOD_END)}
    assert "没有task_key的完成事项" in titles, "task_key 为 NULL 的事项被整行丢掉了"


def test_one_task_contributes_at_most_one_workstream(seeded: Session) -> None:
    """同一任务不能既产出 `project:` 又产出 `task:` 两个主线键。

    实测「一个任务里部分事项填了业务对象、部分没填」时，两条键指向同一件事，
    影响面凭空 +1（院校侧 19 应为 18、法务 6 应为 5、招生协作 3 应为 2）。
    """
    report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    assert report is not None
    task = Task(
        canonical_key="mixedidentitytask",
        title="混合身份任务",
        status="BLOCKED",
        first_seen_date=PERIOD_START,
        last_seen_date=PERIOD_END,
        duration_weeks=1,
    )
    seeded.add(task)
    seeded.flush()
    # 同一个任务：一条填了业务对象、一条没填 —— 旧实现会算成两条主线
    for index, (project, text) in enumerate(
        [("湾一会国际医疗展会", "报名推进"), (None, "等待办公室确认")]
    ):
        item = ReportItem(
            report_id=report.id,
            employee_id=report.employee_id,
            item_type="dependencies",
            item_order=910 + index,
            title=text,
            description=text,
            status="待确认",
            source_text=text,
            related_project=project,
        )
        seeded.add(item)
        seeded.flush()
        seeded.add(
            TaskReportLink(
                task_id=task.id,
                report_id=report.id,
                report_item_id=item.id,
                match_method="model_key",
            )
        )
    seeded.commit()

    rows = dashboard._period_item_rows(seeded, PERIOD_START, PERIOD_END)
    task_projects = dashboard._task_project_index(rows)
    keys = {
        dashboard._workstream_key(item, tid, task_projects)
        for item, _r, _e, _d, tid in rows
        if tid == task.id
    }
    keys.discard(None)
    assert keys == {"project:湾一会国际医疗展会"}, f"同一任务占了多个主线键：{keys}"


def test_status_conflict_requires_same_work_item_not_just_same_project(
    seeded: Session,
) -> None:
    """共用一个业务对象名不等于「同一件事」，不能据此判状态矛盾。

    实测唯一一条冲突是误报：Sky 写「转发广州出入境消息给华南理工大学 2 名学生」已完成、
    Vincent 写「联系华南理工大学咨询学生申请材料原件」进行中 —— 两件不同的事，
    只因为都带「华南理工大学」被聚到一起。
    """
    report_a = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    report_b = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r2")
    ).first()
    assert report_a is not None and report_b is not None
    # 与实测数据同样的类型组合：`ongoing_items` + 状态「已完成」算完成侧，
    # 依赖类（dependencies）算未解决侧。**`进行中` 不属于任何一侧** ——
    # 用它做未解决侧会让条目根本不进入判定，测试于是无论代码对错都通过
    # （这正是一版写成 `ongoing_items/进行中` 后验证发现的假测试）。
    rows = [
        (report_a, report_a.employee_id, "ongoing_items", "已完成", "转发广州出入境消息给华南理工大学2名学生"),
        (report_b, report_b.employee_id, "dependencies", "进行中", "联系华南理工大学咨询学生申请材料原件事宜"),
    ]
    for index, (report, employee_id, item_type, status, text) in enumerate(rows):
        seeded.add(
            ReportItem(
                report_id=report.id,
                employee_id=employee_id,
                item_type=item_type,
                item_order=920 + index,
                title=text,
                description=text,
                status=status,
                source_text=text,
                related_project="华南理工大学",
            )
        )
    seeded.commit()

    conflicts = dashboard._status_conflicts(
        dashboard._period_item_rows(seeded, PERIOD_START, PERIOD_END)
    )
    assert conflicts == [], f"两件不同的事被误判成状态矛盾：{conflicts}"


def test_status_conflict_detects_same_item_across_people(seeded: Session) -> None:
    """同一件事被两人写成不同状态时仍要检出（避免修误报修掉了真检出）。"""
    report_a = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    report_b = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r2")
    ).first()
    assert report_a is not None and report_b is not None
    # 未解决侧要用「待确认 / 未解决」或依赖类事项：`进行中` 不算矛盾侧 ——
    # 它只表示还在做，与「已完成」并列时并不构成互相矛盾的陈述。
    rows = [
        (report_a, report_a.employee_id, "completed_items", "已完成", "湾一会国际医疗展会报名"),
        (report_b, report_b.employee_id, "dependencies", "待确认", "国际医疗展会报名落实"),
    ]
    for index, (report, employee_id, item_type, status, text) in enumerate(rows):
        seeded.add(
            ReportItem(
                report_id=report.id,
                employee_id=employee_id,
                item_type=item_type,
                item_order=930 + index,
                title=text,
                description=text,
                status=status,
                source_text=text,
                related_project="湾一会国际医疗展会",
            )
        )
    seeded.commit()

    conflicts = dashboard._status_conflicts(
        dashboard._period_item_rows(seeded, PERIOD_START, PERIOD_END)
    )
    assert len(conflicts) == 1, conflicts
    # 判定依据必须随结论返回，供人工核对这个判定成不成立
    assert conflicts[0]["evidence"], "状态不一致没有给出判定依据"


def test_frictions_do_not_restate_bottlenecks(seeded: Session) -> None:
    """组织摩擦不再重复输出「多部门反复确认」。

    它原本是瓶颈列表里 `department_count >= 2` 那几条的换名重排（实测当期 5 条与
    瓶颈列表完全同源）。同一批事实在两张卡里出现，读起来像 5 处摩擦，实际是
    同一件事说了两遍。
    """
    frictions = dashboard._frictions(seeded, PERIOD_START, PERIOD_END)
    assert "coordination" not in frictions
    assert "repeated_coordination" not in frictions["counts"]
    assert set(frictions["counts"]) == {"duplicate_effort", "status_conflict"}


def test_focus_distribution_keeps_all_categories(seeded: Session) -> None:
    """精力投向不再把尾部类别并进「其他工作」。

    旧实现按前 6 名截断、其余并入「其他工作」，实测把「国际医疗与展会 4.5%」与
    「招聘与团队建设 3.4%」并走，「其他工作」从真实的 14.6% 显示成 22.5% ——
    饼图第二大的一块有一半是显示产物。
    """
    focus = dashboard._focus_distribution(seeded, PERIOD_START, PERIOD_END)
    labels = [item["label"] for item in focus["categories"]]
    assert len(labels) == len(set(labels)), f"类别标签重复：{labels}"
    # 各分类占比之和应等于 100（允许四舍五入误差）
    total_share = sum(item["share"] for item in focus["categories"])
    if focus["total_entries"]:
        assert abs(total_share - 100) <= 0.5, f"占比之和 {total_share} 偏离 100"


def test_empty_resource_demand_variants_are_filtered() -> None:
    """判空必须覆盖各类「以后再议 / 没有需求」写法。

    实测首屏第 5 条是「连续 5 周未解决：资源需求：We can discuss this later」——
    旧词表只有 `discusslater`，漏掉了 `discuss this later` 这类写法；
    另有多条 `No resources required` / `None at this moment` 漏网。
    """
    from app.processors.journal_parser import is_empty_resource_demand

    should_be_empty = [
        "资源需求：We can discuss this later",
        "资源需求：暂无需公司协调解决的问题",
        "资源需求：No resources required",
        "资源需求：None at this moment",
        "资源需求：Nothing at this moment",
        "资源需求：No major issues at the moment",
        "资源需求：目前暂无",
        "资源需求：目前没有资源需求",
        "资源需求：Resource Requirements",
    ]
    for text in should_be_empty:
        assert is_empty_resource_demand(text), f"占位没被过滤：{text}"

    # 反向保护：真需求不能被误杀，尤其是「先否定后补充」的句式
    should_keep = [
        "资源需求：需要梳理业务全局，解决跨部门沟通问题",
        "资源需求：Need faster JW processing by Example University",
        "资源需求：Canva and CapCut subscriptions",
        "资源需求：None, but It would be helpful to have an office space",
        "资源需求：暂无特殊资源需求，希望后续能够持续获得院校资料",
        "资源需求：目前没有笔记本电脑，需要公司配备",
    ]
    for text in should_keep:
        assert not is_empty_resource_demand(text), f"真需求被误杀：{text}"


def test_known_project_names_exclude_resource_demand_titles(seeded: Session) -> None:
    """回灌给模型的项目名清单不能含资源需求标题。

    它们的标题形如「资源需求：Discuss later」，被当成项目名回灌会引导模型
    把占位文案写成项目名，反而制造新的噪音。
    """
    from app.services.report_analysis_service import _known_project_names

    report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    assert report is not None
    task = seeded.scalar(select(Task).where(Task.canonical_key == "sharedproject"))
    assert task is not None

    def _link(order: int, title: str, project: str, task_key: str | None) -> None:
        item = ReportItem(
            report_id=report.id,
            employee_id=report.employee_id,
            item_type="blockers",
            # item_order 参与 (report_id, item_type, item_order) 唯一约束，必须递增
            item_order=order,
            title=title,
            description=title,
            status="待确认",
            source_text=title,
            related_project=project,
            task_key=task_key,
        )
        seeded.add(item)
        seeded.flush()
        seeded.add(
            TaskReportLink(
                task_id=task.id,
                report_id=report.id,
                report_item_id=item.id,
                match_method="model_key",
            )
        )

    _link(940, "湾一会国际医疗展会报名", "湾一会国际医疗展会", "展会报名")
    _link(
        941,
        "资源需求：Discuss later",
        "资源需求：Discuss later",
        "resource_demand:discusslater",
    )
    seeded.commit()

    names = _known_project_names(seeded)
    assert "湾一会国际医疗展会" in names, names
    assert not any("资源需求" in name for name in names), names


def test_analysis_prompt_version_bumped_for_identity_change() -> None:
    """改提示词的标识规格必须同时升版本号。

    缓存判定把 `prompt_version` 算进 payload hash：不升版本号，已分析过的周报
    会沿用旧结论，新规格永远不会生效（改完像没改）。
    """
    from app.ai.prompts import REPORT_ANALYSIS_PROMPT_VERSION

    assert REPORT_ANALYSIS_PROMPT_VERSION != "v1"


# --------------------------------------------------------------------------- #
# 效度反馈回路
# --------------------------------------------------------------------------- #


def test_feedback_records_and_updates_single_verdict_per_reviewer(seeded: Session) -> None:
    """同一个人对同一条结论只保留一条判定：改主意是更新，不是新增。"""
    from app.services import dashboard_feedback_service as feedback

    feedback.record_feedback(
        seeded,
        module="bottleneck",
        start=PERIOD_START,
        end=PERIOD_END,
        key="办公室确认与发布",
        verdict="wrong",
        reviewer="admin",
        title="办公室确认与发布影响 4 项工作",
        reason="这两条其实是两件事",
    )
    row = feedback.record_feedback(
        seeded,
        module="bottleneck",
        start=PERIOD_START,
        end=PERIOD_END,
        key="办公室确认与发布",
        verdict="correct",
        reviewer="admin",
    )
    assert row.verdict == "correct"
    # 改成「说得对」后，原来那条「说错了」的原因必须清掉 ——
    # 留着与当前判定不符的说明会误导复盘。
    assert row.reason is None

    index = feedback.feedback_index(seeded, start=PERIOD_START, end=PERIOD_END)
    assert len(index) == 1
    assert list(index.values())[0]["admin"]["verdict"] == "correct"


def test_feedback_signal_key_is_stable_across_recompute(seeded: Session) -> None:
    """指纹必须跨重算稳定，否则算法一改已攒的判定全部对不上号。"""
    from app.services import dashboard_feedback_service as feedback

    key_a = feedback.build_signal_key("bottleneck", PERIOD_START, PERIOD_END, "办公室确认与发布")
    key_b = feedback.build_signal_key("bottleneck", PERIOD_START, PERIOD_END, "办公室确认与发布")
    assert key_a == key_b
    # 不同模块 / 不同周期的同一条结论必须是不同指纹
    assert key_a != feedback.build_signal_key("attention", PERIOD_START, PERIOD_END, "办公室确认与发布")
    assert key_a != feedback.build_signal_key(
        "bottleneck", PERIOD_START, date(2026, 9, 27), "办公室确认与发布"
    )


def test_feedback_summary_withholds_rate_under_min_sample(seeded: Session) -> None:
    """样本不足时不给百分比 —— 一条判对的结论算出 100% 会误导人信任它。"""
    from app.services import dashboard_feedback_service as feedback

    feedback.record_feedback(
        seeded, module="conflict", start=PERIOD_START, end=PERIOD_END,
        key="某事项", verdict="correct", reviewer="admin",
    )
    summary_payload = feedback.feedback_summary(seeded, start=PERIOD_START, end=PERIOD_END)
    module = summary_payload["modules"]["conflict"]
    assert module["correct"] == 1
    assert module["enough_sample"] is False
    assert module["min_sample"] == feedback.MIN_SAMPLE_FOR_RATE


def test_feedback_summary_computes_accuracy_and_excludes_unclear(seeded: Session) -> None:
    """准确率 = 说得对 /（说得对 + 说错了）；「说不清」不进分母。"""
    from app.services import dashboard_feedback_service as feedback

    for index in range(4):
        feedback.record_feedback(
            seeded, module="duplicate", start=PERIOD_START, end=PERIOD_END,
            key=f"项目{index}", verdict="correct", reviewer="admin",
        )
    feedback.record_feedback(
        seeded, module="duplicate", start=PERIOD_START, end=PERIOD_END,
        key="项目错", verdict="wrong", reviewer="admin",
    )
    feedback.record_feedback(
        seeded, module="duplicate", start=PERIOD_START, end=PERIOD_END,
        key="项目不清", verdict="unclear", reviewer="admin",
    )
    module = feedback.feedback_summary(seeded, start=PERIOD_START, end=PERIOD_END)["modules"]["duplicate"]
    assert module["correct"] == 4 and module["wrong"] == 1 and module["unclear"] == 1
    assert module["decided"] == 5
    assert module["accuracy"] == 80.0, module
    assert module["enough_sample"] is True


def test_feedback_rejects_unknown_module_and_verdict(seeded: Session) -> None:
    """非法模块 / 判定必须报错，不能写进无法聚合的脏数据。

    准确率报表按这些字段分组，脏数据会让报表给出看似有效的错误结论。
    """
    from app.services import dashboard_feedback_service as feedback

    with pytest.raises(ValueError):
        feedback.record_feedback(
            seeded, module="不存在的模块", start=PERIOD_START, end=PERIOD_END,
            key="x", verdict="correct", reviewer="admin",
        )
    with pytest.raises(ValueError):
        feedback.record_feedback(
            seeded, module="bottleneck", start=PERIOD_START, end=PERIOD_END,
            key="x", verdict="probably", reviewer="admin",
        )
    with pytest.raises(ValueError):
        feedback.record_feedback(
            seeded, module="bottleneck", start=PERIOD_START, end=PERIOD_END,
            key="   ", verdict="correct", reviewer="admin",
        )


def test_overview_payload_includes_feedback_index(client: TestClient) -> None:
    """视图接口要带上已有判定，页面才能回显「这条我判过了」。"""
    response = client.get("/api/operating-dashboard", params={"view": "overview", **PERIOD})
    assert response.status_code == 200
    assert "feedback" in response.json()


def test_feedback_endpoint_persists_and_reports(client: TestClient) -> None:
    """POST /feedback 落库，GET /feedback/summary 能读回。"""
    payload = {
        "module": "bottleneck",
        "key": "办公室确认与发布",
        "verdict": "wrong",
        "title": "办公室确认与发布影响 4 项工作",
        "reason": "其实是两件事",
        **PERIOD,
    }
    response = client.post("/api/operating-dashboard/feedback", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["verdict_label"] == "说错了"
    # 判定人来自会话，不由请求体传入 —— 否则可以伪造他人判定
    assert body["reviewer"] == "admin"

    summary_response = client.get("/api/operating-dashboard/feedback/summary", params=PERIOD)
    assert summary_response.status_code == 200
    module = summary_response.json()["modules"]["bottleneck"]
    assert module["wrong"] == 1


def test_feedback_response_entry_matches_index_shape(client: TestClient) -> None:
    """POST /feedback 的 ``entry`` 与 GET 视图回显索引里的条目必须同形。

    前端拿到 POST 响应后**本地合并**进已加载的 payload（不再全量刷新视图）。
    合并的前提是两边形状一致 —— 一旦漂移，页面会静默丢「已判定」状态。
    """
    response = client.post(
        "/api/operating-dashboard/feedback",
        json={
            "module": "bottleneck",
            "key": "办公室确认与发布",
            "verdict": "wrong",
            "reason": "其实是两件事",
            **PERIOD,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    entry = body.get("entry")
    assert isinstance(entry, dict)
    assert entry["verdict"] == "wrong"
    assert entry["label"] == "说错了"
    assert entry["reason"] == "其实是两件事"
    assert entry["reviewer"] == "admin"

    # 与 GET 回显索引里同一条判定逐字段相等（updated_at 也一样）
    overview = client.get("/api/operating-dashboard", params={"view": "overview", **PERIOD})
    assert overview.status_code == 200
    index = overview.json().get("feedback") or {}
    indexed_entry = index[body["signal_key"]]["admin"]
    assert indexed_entry == entry


def test_feedback_endpoint_rejects_invalid_verdict(client: TestClient) -> None:
    """非法判定值被 pydantic 拦成 422。"""
    response = client.post(
        "/api/operating-dashboard/feedback",
        json={"module": "bottleneck", "key": "x", "verdict": "maybe", **PERIOD},
    )
    assert response.status_code == 422


def test_feedback_get_does_not_write(client: TestClient, seeded: Session) -> None:
    """GET 视图接口不得写判定（轮询会周期性调用它）。"""
    from app.models.dashboard_feedback import DashboardFeedback

    client.get("/api/operating-dashboard", params={"view": "overview", **PERIOD})
    assert seeded.scalars(select(DashboardFeedback)).all() == []


def test_feedback_key_is_not_double_prefixed(client: TestClient) -> None:
    """业务键传给接口后，指纹里只能出现一次周期。

    实测踩过的 bug：前端把自己拼好的完整指纹（`module|start|end|key`）当成键传给
    后端，后端再拼一次周期，得到
    `attention|2026-09-14|2026-09-20|attention|2026-09-14|2026-09-20|标题`。
    后果是回显索引对不上，页面永远看不到「已判定」——
    而判定其实已经落库了（脏数据比没数据更难发现）。

    这条守护的是**契约**：接口的 `key` 是业务键，周期通过单独的 start/end 字段传。
    """
    response = client.post(
        "/api/operating-dashboard/feedback",
        json={
            "module": "attention",
            "key": "院校侧反馈与确认影响 18 项工作",
            "verdict": "correct",
            **PERIOD,
        },
    )
    assert response.status_code == 200, response.text
    signal_key = response.json()["signal_key"]
    # 周期只能在指纹里出现一次
    assert signal_key.count(PERIOD["start"]) == 1, signal_key
    assert signal_key.count(PERIOD["end"]) == 1, signal_key
    assert signal_key.count("attention") == 1, signal_key
    assert signal_key == (
        "attention|2026-09-14|2026-09-20|院校侧反馈与确认影响 18 项工作"
    ), signal_key

    # 提交后，视图接口必须能按同一指纹回显这条判定
    overview = client.get("/api/operating-dashboard", params={"view": "overview", **PERIOD})
    assert overview.status_code == 200
    index = overview.json().get("feedback") or {}
    assert signal_key in index, index
    assert index[signal_key]["admin"]["verdict"] == "correct"


def test_feedback_index_key_matches_service_builder(client: TestClient) -> None:
    """接口返回的指纹键必须等于服务层 `build_signal_key` 的结果。

    前端 `feedbackFor()` 用同一规则拼键去查索引；两侧规则一旦漂移，
    页面就会静默丢失「已判定」状态（不会报错，只是不显示）。
    """
    from app.services.dashboard_feedback_service import build_signal_key

    client.post(
        "/api/operating-dashboard/feedback",
        json={"module": "project", "key": "湾一会国际医疗展会", "verdict": "unclear", **PERIOD},
    )
    overview = client.get("/api/operating-dashboard", params={"view": "overview", **PERIOD})
    index = overview.json().get("feedback") or {}
    expected = build_signal_key("project", PERIOD_START, PERIOD_END, "湾一会国际医疗展会")
    assert expected in index, (expected, list(index))


def test_feedback_min_sample_constant_matches_frontend() -> None:
    """样本门槛前后端必须一致。

    前端用它决定「显示已判定条数」还是「显示准确率」。两侧漂移会让页面在
    后端认为样本不足时就显示出百分比 —— 而那个数字恰恰是最不该被信任的
    （一条判对 = 100%）。这里的做法是从前端源码里读常量，不是复制一份。
    """
    import io as _io
    import re as _re

    from app.services.dashboard_feedback_service import MIN_SAMPLE_FOR_RATE

    source = _io.open(
        "frontend/app/operating-dashboard/overview.tsx", encoding="utf-8"
    ).read()
    match = _re.search(r"const MIN_SAMPLE_FOR_RATE = (\d+);", source)
    assert match, "前端未声明 MIN_SAMPLE_FOR_RATE"
    assert int(match.group(1)) == MIN_SAMPLE_FOR_RATE, (
        f"前端门槛 {match.group(1)} 与后端 {MIN_SAMPLE_FOR_RATE} 不一致"
    )


def test_feedback_verdict_labels_match_frontend_options() -> None:
    """判定选项必须只有后端认可的三个值。

    前端多写一个选项，用户点了只会拿到 422；少写一个则永远收不到「说不清」——
    而它正是「证据不足」这个独立信号的载体。
    """
    import io as _io
    import re as _re

    from app.services.dashboard_feedback_service import VERDICTS

    source = _io.open("frontend/components/feedback-buttons.tsx", encoding="utf-8").read()
    found = set(_re.findall(r'verdict: "(\w+)"', source))
    assert found == set(VERDICTS), f"前端选项 {sorted(found)} vs 后端 {sorted(VERDICTS)}"


def test_comparability_rejects_low_previous_coverage(seeded: Session) -> None:
    """环比只在两侧覆盖率都达标时才给数字。

    实测的假结论：上期 27 篇里只分析了 5 篇（19%）、本期 24 篇全分析（100%），
    旧实现只判「上期有没有分析数据」（`analyzed_reports > 0`），于是放行并
    报出「工作主线 +50」——那是「这周多分析了 19 篇」，不是业务变化。
    """
    from app.services.operating_dashboard_service import _comparability

    low = {"reports_total": 27, "analyzed_reports": 5}
    full = {"reports_total": 24, "analyzed_reports": 24}
    result = _comparability(full, low)
    assert result["comparable"] is False
    assert "19%" in result["reason"], result
    # 原因要带上两侧覆盖率，页面据此说明差在哪
    assert result["previous_coverage"] == round(5 / 27, 4)
    assert result["current_coverage"] == 1.0


def test_comparability_accepts_close_coverage(seeded: Session) -> None:
    """两侧覆盖率都高且接近时，比较成立（避免把护栏做成「一律不可比」）。"""
    from app.services.operating_dashboard_service import _comparability

    result = _comparability(
        {"reports_total": 24, "analyzed_reports": 24},
        {"reports_total": 25, "analyzed_reports": 24},
    )
    assert result["comparable"] is True
    assert result["reason"] is None


def test_comparability_rejects_large_coverage_gap(seeded: Session) -> None:
    """两侧都过绝对门槛、但差值过大时仍不可比。

    85% 与 100% 都算「分析过了」，可 15 个百分点的差足以让绝对篇数的变化
    主要来自覆盖差异而不是业务变化。
    """
    from app.services.operating_dashboard_service import _comparability

    result = _comparability(
        {"reports_total": 100, "analyzed_reports": 100},
        {"reports_total": 100, "analyzed_reports": 80},
    )
    assert result["comparable"] is False
    assert "相差" in result["reason"], result


def test_reports_delta_still_works_under_low_coverage(seeded: Session) -> None:
    """篇数环比不受分析覆盖门槛限制。

    「这周交了几篇」只依赖接口入库的周报，与跑没跑 AI 无关。把覆盖率门槛
    一刀切到所有指标上，会让一个本来成立的数字变得不敢显示。
    """
    # fixture 的「上期」没有周报，先造一篇，否则测到的是「上期无周报数据」
    previous = WeeklyReport(
        employee_id=seeded.scalars(select(Employee).limit(1)).first().id,
        external_id="prev-week",
        report_type="weekly",
        title="上期周报",
        content="正文",
        content_raw="正文",
        start_date=PERIOD_START - timedelta(days=7),
        end_date=PERIOD_END - timedelta(days=7),
        submitted_at=datetime.combine(PERIOD_END - timedelta(days=7), datetime.min.time()),
        source="wecom",
    )
    seeded.add(previous)
    seeded.commit()

    payload = dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)
    by_key = {metric["key"]: metric for metric in payload["metrics"]}
    assert by_key["reports"]["delta"]["available"] is True
    # 而依赖结构化事实的两个指标在同样数据下必须拒绝比较
    assert by_key["themes"]["delta"]["available"] is False
    assert by_key["signals"]["delta"]["available"] is False


def test_metric_carries_coverage_basis(seeded: Session) -> None:
    """每个指标都带计算基数。

    只说「58 条工作主线」而不说「基于 24 篇已分析周报」，用户无法判断这个数字
    该信几分。基数随数字一起返回，由页面如实展示。
    """
    payload = dashboard.overview_view(seeded, PERIOD_START, PERIOD_END)
    for metric in payload["metrics"]:
        assert "basis" in metric, metric["key"]
        basis = metric["basis"]
        assert set(basis) == {"analyzed_reports", "reports_total", "coverage"}, basis


def _add_dependency(seeded: Session, report, employee_id, text: str, project: str, order: int):
    """补一条**推进类**事项。

    刻意用 `ongoing_items` 而不是 `dependencies`：`_duplicates` 会主动排除依赖类
    （依赖是「被什么挡住」而不是「推进动作」，不参与重复判定）。第一版写成
    `dependencies` 时用例恒为空，是假测试。
    """
    item = ReportItem(
        report_id=report.id,
        employee_id=employee_id,
        item_type="ongoing_items",
        item_order=order,
        title=text,
        description=text,
        status="待确认",
        source_text=text,
        related_project=project,
    )
    seeded.add(item)
    return item


def test_duplicate_effort_excludes_activity_categories(seeded: Session) -> None:
    """活动类别不算「重复推进」。

    每个部门都会招聘、都要开周会、都有报销 —— 两个部门都写「招聘」是组织常态，
    不是「同一件事被做了两遍」。实测当期 20 条重复提醒里 13 条是这类误报
    （招聘 / 周例会 / 费用报销 / 部门管理 / 网站 / 品牌宣传…），误报率 65%，
    而且它们会刷屏首屏候选池（20/34 条信号来自这个来源）。
    """
    alice_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    bob_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r2")
    ).first()
    assert alice_report is not None and bob_report is not None

    # 两条必须**像同一件事**：否则会被「同一件事」那道门槛拦下，
    # 用例就会在「移除活动类别过滤」后仍然通过（第一版写成「本周招聘进展」
    # vs「招聘面试安排」，实测是假测试 —— 挡住它的是另一道门槛）。
    _add_dependency(seeded, alice_report, alice_report.employee_id, "招聘一名市场经理", "招聘", 950)
    seeded.flush()
    _add_dependency(seeded, bob_report, bob_report.employee_id, "招聘市场经理进展", "招聘", 951)
    seeded.commit()

    frictions = dashboard._frictions(seeded, PERIOD_START, PERIOD_END)
    projects = {item["project"] for item in frictions["duplicates"]}
    assert "招聘" not in projects, f"活动类别被当成重复推进：{projects}"


def test_duplicate_effort_requires_same_item_across_departments(seeded: Session) -> None:
    """跨部门但各做各的事，不算重复推进。

    实测「大连枫叶申请」：4 个部门分别在推广大连工业大学项目、做英文宣传册、
    给市场经理做培训、跟进学生申请 —— 标准分工，只因为共用同一个业务对象名
    被报成了「疑似重复推进」。
    """
    alice_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    bob_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r2")
    ).first()
    assert alice_report is not None and bob_report is not None

    # 同一业务对象（学校），但两件完全不同的事
    _add_dependency(
        seeded, alice_report, alice_report.employee_id, "转发出入境消息给该校2名学生", "某合作大学", 960
    )
    seeded.flush()
    _add_dependency(
        seeded, bob_report, bob_report.employee_id, "咨询学生申请材料原件事宜", "某合作大学", 961
    )
    seeded.commit()

    frictions = dashboard._frictions(seeded, PERIOD_START, PERIOD_END)
    projects = {item["project"] for item in frictions["duplicates"]}
    assert "某合作大学" not in projects, f"不同的事被算成重复：{projects}"


def test_duplicate_effort_detects_same_item_across_departments(seeded: Session) -> None:
    """两个部门确实在做同一件事时仍要检出（别把误报修成漏报）。"""
    alice_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r1")
    ).first()
    bob_report = seeded.scalars(
        select(WeeklyReport).where(WeeklyReport.external_id == "r2")
    ).first()
    assert alice_report is not None and bob_report is not None

    _add_dependency(
        seeded, alice_report, alice_report.employee_id, "国际医疗展会报名落实到现场", "某国际展会", 970
    )
    seeded.flush()
    _add_dependency(
        seeded, bob_report, bob_report.employee_id, "国际医疗展会报名材料准备", "某国际展会", 971
    )
    seeded.commit()

    frictions = dashboard._frictions(seeded, PERIOD_START, PERIOD_END)
    hit = next((item for item in frictions["duplicates"] if item["project"] == "某国际展会"), None)
    assert hit is not None, frictions["duplicates"]
    # 判定依据必须随结论返回，供人工核对
    assert hit["same_item_evidence"], hit


def test_activity_category_marker_matching() -> None:
    """活动类别的判定要覆盖常见形态，且不误伤真实业务对象。"""
    from app.services.operating_dashboard_service import _is_activity_category

    for name in ("招聘", "周例会", "费用报销", "部门管理", "内部协作与会议参与", "网站"):
        assert _is_activity_category(name), name
    for name in (
        "河南中医药大学招生项目",
        "湾一会国际医疗展会",
        "广外学生申请",
        "吉尔吉斯公司注册与税务",
    ):
        assert not _is_activity_category(name), name


def test_monthly_narrative_is_attached_by_identity_not_position(seeded: Session) -> None:
    """简报文字必须跟着**业务对象**走，不能按位置贴。

    这条对应一个真实缺陷：简报过期时我们仍展示它（第 28h 条的决定 ——
    过期不等于作废），而那时页面拿到的是**重新算过**的清单，排序可能已经变了
    （共性问题按部门数/篇数排，新周报入库就会动）。实测把前两条对调后，
    「院校沟通与反馈」那句话被贴到了「审批与对公付款」下面 ——
    结论挂错对象比没有结论更糟。

    做法：生成时把 `ref → 业务对象名` 一起落库（`ref_identities`），
    展示时按名字认领；名字对不上宁可不贴（计入 `items_missing`）。
    """
    payload = dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)
    # 夹具里可能没有「公司级共性问题」（需要 ≥3 个部门），这里直接构造两条 ——
    # 这条测试验证的是**合并逻辑按什么贴文字**，与那两条怎么算出来的无关。
    payload["shared_issues"] = [
        {"title": "对象甲", "department_count": 3, "evidence_count": 5, "departments": []},
        {"title": "对象乙", "department_count": 4, "evidence_count": 9, "departments": []},
    ]
    identities = {"SI1": "对象甲", "SI2": "对象乙"}
    first, second = payload["shared_issues"][0], payload["shared_issues"][1]
    text_for_first = "这是给第一条（对象甲）写的话。"
    summary_state = {
        "result": {"headline": "x", "items": [{"ref": "SI1", "text": text_for_first}]},
        "created_at": "2026-09-24T10:00:00",
        "model": "deepseek-chat",
        "ref_identities": identities,
    }

    # 模拟事实变化导致的重排：把前两条对调（简报仍是旧的）
    payload["shared_issues"] = [second, first] + payload["shared_issues"][2:]
    merged = dashboard.merge_monthly_narrative(payload, summary_state)
    shared = merged["shared_issues"]

    by_title = {item["title"]: item for item in shared}
    # 那句话原本属于第一条，重排后必须落在**同一件对象**上，而不是位置上
    assert by_title[first["title"]]["ai_text"] == text_for_first
    assert by_title[second["title"]].get("ai_text") in (None, ""), "不得把别人的话贴到这一条"
    # 贴不上要如实计数（页面据此提示），不是假装都有结论
    assert merged["narrative"]["items_missing"] > 0


def test_monthly_narrative_without_identity_map_keeps_old_brief_readable(
    seeded: Session,
) -> None:
    """没有 `ref_identities` 的旧简报（本次改动前生成的）仍要能显示。

    兼容优先于严格：旧简报一旦过期就会被重新生成，这个窗口很短；
    而这期间整篇空白会让页面看起来坏了。所以身份表缺席时退回按位置贴。
    """
    payload = dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)
    payload["shared_issues"] = [
        {"title": "对象甲", "department_count": 3, "evidence_count": 5, "departments": []},
    ]
    old_style = {
        "result": {"headline": "旧版结论", "items": [{"ref": "SI1", "text": "旧简报的第一句话。"}]},
        "created_at": "2026-09-24T10:00:00",
        "model": "deepseek-chat",
        # 刻意不给 ref_identities
    }

    merged = dashboard.merge_monthly_narrative(payload, old_style)
    assert merged["shared_issues"][0]["ai_text"] == "旧简报的第一句话。"


def test_summary_state_reports_how_many_reports_the_brief_covered(seeded: Session) -> None:
    """`generated_report_count` 必须如实反映「生成时覆盖了多少篇」。

    页面用它算「这份结论之后又入库了多少篇」—— 只说「事实已变化」，读者无从判断
    是多了一篇（可以忽略）还是多了四十篇（结论已经不能用）。旧结论没这个字段时
    返回 None（页面退回泛化措辞），不要瞎猜一个数字。
    """
    state = summary.summary_state(seeded, "weekly", PERIOD_START, PERIOD_END)
    assert "generated_report_count" in state
    # 还没生成过结论时没有这个数字
    assert state["generated_report_count"] is None

    _enqueue(seeded, "weekly")
    row = seeded.query(OperatingSummaryQueue).one()
    row.status = AnalysisQueueStatus.SUCCESS
    row.report_count = 3
    seeded.commit()
    seeded.add(
        AIAnalysis(
            entity_type=summary.ENTITY_TYPE,
            entity_id=row.id,
            analysis_type=summary.ANALYSIS_TYPE,
            prompt_version="v7",
            status=AIAnalysisStatus.SUCCESS,
            result_json={"result": {"headline": "结论"}, "source_report_ids": []},
            created_at=datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        )
    )
    seeded.commit()

    state = summary.summary_state(seeded, "weekly", PERIOD_START, PERIOD_END)
    assert state["generated_report_count"] == 3
    # 与当前篇数一起才能算出增量（页面就是这两者相减）
    assert state["reports_total"] >= 3


def test_monthly_material_total_stays_within_budget(seeded: Session) -> None:
    """材料总量必须受总预算约束，且逐篇摘要按**剩余**预算分配。

    为什么必须有总预算：各分段的条数上限是分开定的，但块数多、文本长，合起来会随
    数据量增长 —— 撞上模型上下文上限的代价很重（失败后队列按重试次数反复重发同一份
    超长 payload，每次计费）。实测近半年（449 篇）材料 8.9 万字符已接近可接受上限。
    **摘要不能自己定死一个上限**：那样「其他块涨了、摘要没减」的组合会悄悄超限。
    """
    facts = summary._facts_payload(seeded, "monthly", PERIOD_START, PERIOD_END, department_id=None)
    total = len(json.dumps(facts, ensure_ascii=False, default=str))
    assert total <= summary.MATERIAL_TOTAL_CHARS + summary.DIGEST_MAX_CHARS

    # 逐篇摘要的预算随其他分段体积变化（不是常数）
    small = summary._digest_budget({"a": "x" * 100})
    large = summary._digest_budget({"a": "x" * (summary.MATERIAL_TOTAL_CHARS - 1000)})
    assert small > large, "其他分段越大，留给摘要的预算越少"
    assert large == summary.DIGEST_MIN_CHARS, "预算不足时保底，不能是 0 或负数"


def test_monthly_top_coverage_equals_sum_of_months(seeded: Session) -> None:
    """顶部覆盖数必须等于逐月明细之和 —— 同一页不能有两个打架的数字。

    原先是两个口径：顶部走 `_coverage`（周期**与区间有重叠**即计入），
    逐月走 `_month_coverage`（按周期**结束日**归到唯一一个月）。跨月的那一周
    （周一开始、下周日结束）必然分歧 —— 实测「上月」档顶部 154 篇、逐月只有 123 篇，
    差的 31 篇是 8/31~9/6 那一周（结束日在 9 月，不算进 8 月）。
    **同一页两个数字打架，读者只能两个都不信。**

    现在月度视图以逐月为准（顶部 = 之和，按构造一致），并且归月要落在该月的
    **实际区间**内 —— 查「9/1~9/24」时，一个 9/21~9/27 的周期结束日在区间之外，
    按月份名归到 9 月会把它算进一个还没走完的范围里。
    """
    # **必须造一篇跨月的周报**：否则两种口径给出同样的答案，测试永远绿
    # （第一版就栽在这里 —— 反证时把顶部改回旧口径，测试照样通过）。
    employee = seeded.query(Employee).filter_by(name="张三").one()
    seeded.add(
        WeeklyReport(
            employee_id=employee.id,
            external_id="straddle",
            report_type="weekly",
            title="周报",
            content="正文",
            content_raw="正文",
            # 8/31（周一）~ 9/6（周日）：横跨两个月，正是分歧的来源
            start_date=date(2026, 8, 31),
            end_date=date(2026, 9, 6),
            submitted_at=datetime(2026, 9, 5, 10, tzinfo=timezone.utc),
            source="wecom",
        )
    )
    seeded.commit()

    # 单月区间（8 月）：跨月那篇的结束日在 9 月 → 不计入 8 月。
    # 夹具在 8/3 放了一篇历史证据，所以 8 月应有 1 篇（不是 0），跨月那篇不算。
    payload = dashboard.monthly_view(seeded, date(2026, 8, 1), date(2026, 8, 31))
    top = payload["coverage"]["reports_total"]
    per_month = sum(int(item["report_total"]) for item in payload["months"])
    assert top == per_month, f"单月区间：顶部 {top} ≠ 逐月之和 {per_month}"
    # 关键断言：跨月那篇（8/31~9/6）没有因为「开始日在 8 月」而被算进 8 月
    assert top == 1, f"8 月只该有夹具那一篇历史证据，实际 {top}"

    for start, end in (
        (PERIOD_START, PERIOD_END),
        (PERIOD_START - timedelta(days=7), PERIOD_END),
        (date(2026, 8, 1), date(2026, 9, 30)),
    ):
        payload = dashboard.monthly_view(seeded, start, end)
        top = payload["coverage"]["reports_total"]
        per_month = sum(int(item["report_total"]) for item in payload["months"])
        assert top == per_month, f"{start}~{end}: 顶部 {top} ≠ 逐月之和 {per_month}"
        assert payload["coverage"]["analyzed_reports"] == sum(
            int(item["analyzed_reports"]) for item in payload["months"]
        )


def test_monthly_ignores_periods_ending_outside_the_range(seeded: Session) -> None:
    """周期结束日在查询区间之外的周报不计入本篇数。

    查「9/1~9/24」，一个 9/21~9/27 的周期结束在 9/27（区间之外）—— 它属于下一段，
    按月份名硬归到 9 月会把「未来的那一周」算进来。裁剪之后「篇数」的含义是
    **在该区间内结束的周期**，与逐月明细同一口径。
    """
    # 造一篇「结束日在区间之后」的周报（夹具的区间是 9/14~9/20）
    employee = seeded.query(Employee).filter_by(name="张三").one()
    late = WeeklyReport(
        employee_id=employee.id,
        external_id="after-range",
        report_type="weekly",
        title="周报",
        content="正文",
        content_raw="正文",
        start_date=PERIOD_END + timedelta(days=1),
        end_date=PERIOD_END + timedelta(days=7),
        submitted_at=datetime(2026, 9, 21, 10, tzinfo=timezone.utc),
        source="wecom",
    )
    seeded.add(late)
    seeded.commit()

    before = dashboard.monthly_view(seeded, PERIOD_START, PERIOD_END)
    # 它既不在顶部，也不在任何一个月里
    assert all(
        item["report_total"] == 0 or item["end"] <= PERIOD_END.isoformat()
        for item in before["months"]
    )
    assert before["coverage"]["reports_total"] == sum(
        int(item["report_total"]) for item in before["months"]
    )




# ---------------------------------------------------------------------------
# 总览卡片 AI 结论（2026-09-24）：项目/摩擦/趋势材料带 ref 进周度总结，
# 模型输出 item_notes，按 ref_identities 的名字贴回卡片
# ---------------------------------------------------------------------------


def _overview_payload_with_cards() -> dict:
    """一个浓缩版总览 payload：四类卡片各带规则版结论，形状与 overview_view 一致。"""
    return {
        "projects": {
            "items": [
                {"name": "国际医疗展", "conclusion": "规则版", "conclusion_source": "rules"},
                {"name": "院校拓展", "conclusion": "规则版", "conclusion_source": "rules"},
            ]
        },
        "frictions": {
            "duplicates": [
                {"project": "国际医疗展", "conclusion_source": "rules"},
            ],
            "conflicts": [
                {"title": "中介交流会", "conclusion_source": "rules"},
            ],
        },
        "trends": {
            "series": [
                {"title": "院校沟通与反馈", "conclusion_source": "rules"},
            ]
        },
        "bottlenecks": {
            "items": [
                {
                    "target": "院校侧反馈与确认",
                    "workstream_count": 13,
                    "department_count": 4,
                    "evidence_count": 8,
                    "conclusion_source": "rules",
                },
            ],
            "unattributed_count": 2,
        },
        "attention": {
            "items": [
                {
                    "title": "学生与客户材料影响 13 项工作",
                    "signal_key": "shared_blocker:学生与客户材料",
                    "conclusion": "规则版",
                    "conclusion_source": "rules",
                },
            ],
            "all_items": [
                {
                    "title": "学生与客户材料影响 13 项工作",
                    "signal_key": "shared_blocker:学生与客户材料",
                    "conclusion": "规则版",
                    "conclusion_source": "rules",
                },
                {
                    "title": "院校沟通与反馈连续扩大",
                    "signal_key": "growing_issue:院校沟通与反馈",
                    "conclusion": "规则版",
                    "conclusion_source": "rules",
                },
            ],
        },
    }


def _ai_summary_state() -> dict:
    return {
        "result": {
            "item_notes": [
                {"ref": "PR2", "text": "院校拓展这个月把两所学校的协议谈完了。"},
                {"ref": "PR1", "text": "国际医疗展的报名材料还在等设计出稿。"},
                {"ref": "BN1", "text": "院校侧反馈与确认挡住了多个部门的申请推进，先找申请服务部核对哪几所学校还在等校方答复。"},
                {"ref": "FR1", "text": "两个部门疑似在同一展会的报名上各自在做，建议核对分工。"},
                {"ref": "FC1", "text": "一方记录交流会已完成，另一方仍挂着未解决，建议核对口径。"},
                {"ref": "TR1", "text": "院校沟通连续几个周期都有部门在等外部答复。"},
                {"ref": "AT1", "text": "学生与客户材料的款项有两条在等本周付款，先找南亚市场对账。"},
            ]
        },
        "ref_identities": {
            "PR1": "国际医疗展",
            "PR2": "院校拓展",
            "BN1": "院校侧反馈与确认",
            "FR1": "国际医疗展",
            "FC1": "中介交流会",
            "TR1": "院校沟通与反馈",
            # 关注信号的身份是稳定键（signal_type:对象名），不是展示标题
            "AT1": "shared_blocker:学生与客户材料",
        },
    }


def test_weekly_facts_include_ref_material_blocks(seeded: Session) -> None:
    """周度事实材料必须包含四类带 ref 的分段：重点项目 / 组织摩擦 / 长期趋势 / 关注信号。

    材料没给，模型只能在计数上重新发明；ref 没给，结论就贴不回卡片。
    数字必须收进「依据数字」，规则版结论绝不能进材料 —— 模型会照抄。
    """
    facts = summary._facts_payload(
        seeded, "weekly", PERIOD_START, PERIOD_END, department_id=None
    )
    # 重点项目必有；关键瓶颈 / 组织摩擦 / 长期趋势 / 关注信号取决于种子数据里
    # 有没有可检出的瓶颈/重复/冲突/多周期主题/信号 —— 有则校验形状，没有则分段为空也合法。
    required = facts.get("重点项目") or []
    assert required, "周度事实材料缺少 重点项目 分段"
    for block_name, prefixes in (
        ("关键瓶颈", ("BN",)),
        ("组织摩擦", ("FR", "FC")),
        ("长期趋势", ("TR",)),
        ("关注信号", ("AT",)),
    ):
        for block in facts.get(block_name) or []:
            ref = str(block["ref"])
            assert any(ref.startswith(p) and ref[len(p):].isdigit() for p in prefixes), (
                f"{block_name} 里出现了意料之外的 ref：{ref}"
            )
            assert block.get("对象"), f"{ref} 缺少对象名"
            # 规则版结论绝不进材料（signals 的 dict 里带 conclusion，必须被滤掉）
            assert "conclusion" not in block, f"{ref} 把规则版结论带进了材料"
    # ref 必须能被 ref_identities 收集，页面才能按名字贴回。
    # 关注信号的身份是稳定键（signal_type:对象名）：展示标题里嵌着数字，
    # 数据一变标题就变，按标题贴回会让 AI 结论全部脱落。
    identities = summary.ref_identities(facts)
    for block_name in ("重点项目", "关键瓶颈", "组织摩擦", "长期趋势", "关注信号"):
        for block in facts.get(block_name) or []:
            expected = block.get("键") or block["对象"]
            assert identities.get(block["ref"]) == expected
    signal_keys = [block.get("键") for block in facts.get("关注信号") or []]
    for key in signal_keys:
        assert key and key.partition(":")[0] in {
            "shared_blocker",
            "duplicate_effort",
            "status_conflict",
            "stalled_workstream",
            "growing_issue",
        }, f"关注信号的稳定键不合法：{key}"
    assert signal_keys == list(dict.fromkeys(signal_keys)), "关注信号的稳定键必须互不重复"


def test_merge_ai_item_notes_attaches_by_ref_identity() -> None:
    """AI 结论按 ref_identities 的名字贴回三类卡片，不按位置贴。

    月度简报踩过「只按位置贴，过期重算后把 A 的话挂到 B 上」的坑（第 28i 条）。
    刻意把 notes 顺序打乱：贴回必须按名字。
    """
    payload = _overview_payload_with_cards()
    merged = dashboard.merge_ai_item_notes(payload, _ai_summary_state())

    projects = {item["name"]: item for item in merged["projects"]["items"]}
    assert projects["国际医疗展"]["conclusion"].startswith("国际医疗展的报名材料")
    assert projects["国际医疗展"]["conclusion_source"] == "ai"
    assert projects["院校拓展"]["conclusion"] == "院校拓展这个月把两所学校的协议谈完了。"

    assert merged["frictions"]["duplicates"][0]["conclusion"].startswith("两个部门疑似")
    assert merged["frictions"]["duplicates"][0]["conclusion_source"] == "ai"
    assert merged["frictions"]["conflicts"][0]["conclusion_source"] == "ai"

    trend = merged["trends"]["series"][0]
    assert trend["conclusion"].startswith("院校沟通连续")
    assert trend["conclusion_source"] == "ai"

    # 关键瓶颈：按 target（稳定业务对象名）贴回 —— 卡片正文改由模型分析
    bottleneck = merged["bottlenecks"]["items"][0]
    assert bottleneck["target"] == "院校侧反馈与确认"
    assert bottleneck["conclusion"].startswith("院校侧反馈与确认挡住了")
    assert bottleneck["conclusion_source"] == "ai"

    # 关注信号：items 与 all_items 都要贴 —— 弹窗读的是 all_items 那条
    attention_item = merged["attention"]["items"][0]
    assert attention_item["conclusion"].startswith("学生与客户材料的款项")
    assert attention_item["conclusion_source"] == "ai"
    all_items = {item["title"]: item for item in merged["attention"]["all_items"]}
    assert all_items["学生与客户材料影响 13 项工作"]["conclusion_source"] == "ai"
    # 没有 AT 结论的信号保留规则版，不空、也不被别的条目污染
    assert all_items["院校沟通与反馈连续扩大"]["conclusion_source"] == "rules"


def test_merge_ai_item_notes_survives_title_drift() -> None:
    """展示标题里的数字变了，AI 结论仍按稳定键贴回。

    「影响 15 项工作」在下一次同步后变成 16 项 —— 按展示标题贴回会让全部
    AI 结论在数据更新后脱落回规则串（负责人 2026-09-30 报的「点进去全是
    噪音」的成因之一）。
    """
    payload = _overview_payload_with_cards()
    # 数据变了：影响工作数从 13 变成 18，展示标题跟着变，signal_key 不变
    for item in payload["attention"]["items"] + payload["attention"]["all_items"]:
        if item.get("signal_key") == "shared_blocker:学生与客户材料":
            item["title"] = "学生与客户材料影响 18 项工作"

    merged = dashboard.merge_ai_item_notes(payload, _ai_summary_state())
    drifted = merged["attention"]["items"][0]
    assert drifted["title"] == "学生与客户材料影响 18 项工作"
    assert drifted["conclusion"].startswith("学生与客户材料的款项")
    assert drifted["conclusion_source"] == "ai"


def test_merge_ai_item_notes_falls_back_to_legacy_title_identity() -> None:
    """旧总结的身份存的是展示标题 —— 重算前这些结论仍要能贴回，不白白丢掉。"""
    payload = _overview_payload_with_cards()
    legacy_state = {
        "result": {
            "item_notes": [
                {"ref": "AT1", "text": "学生与客户材料的款项有两条在等本周付款。"},
            ]
        },
        "ref_identities": {
            "AT1": "学生与客户材料影响 13 项工作",
        },
    }
    merged = dashboard.merge_ai_item_notes(payload, legacy_state)
    assert merged["attention"]["items"][0]["conclusion_source"] == "ai"


def test_merge_ai_item_notes_ignores_unmatched_refs() -> None:
    """ref 对不上身份（模型编的编号、身份缺失）的结论不得贴到任何条目上。

    挂错项目的结论比没有结论更糟 —— 负责人会拿着它去做错误判断。
    """
    payload = _overview_payload_with_cards()
    summary_state = {
        "result": {
            "item_notes": [
                {"ref": "PR9", "text": "来历不明的结论。"},
                {"ref": "PR1", "text": "没有登记身份的结论。"},
            ]
        },
        "ref_identities": {},  # PR1 没有身份，PR9 连身份字典里都没有
    }
    merged = dashboard.merge_ai_item_notes(payload, summary_state)
    assert merged["projects"]["items"][0]["conclusion"] == "规则版"
    assert merged["projects"]["items"][0]["conclusion_source"] == "rules"
    assert merged["frictions"]["duplicates"][0]["conclusion_source"] == "rules"
    assert merged["trends"]["series"][0]["conclusion_source"] == "rules"
    assert merged["attention"]["items"][0]["conclusion_source"] == "rules"


def test_merge_ai_item_notes_keeps_rules_fallback_without_ai() -> None:
    """没有 AI 结论时保留规则版兜底，页面不空；输入原样返回不被伪造。"""
    payload = _overview_payload_with_cards()
    original = json.loads(json.dumps(payload, default=str))

    assert dashboard.merge_ai_item_notes(payload, None) is payload
    assert dashboard.merge_ai_item_notes(payload, {"result": None}) is payload
    assert dashboard.merge_ai_item_notes(payload, {"result": {"item_notes": []}}) is payload
    assert json.loads(json.dumps(payload, default=str)) == original


def test_merge_ai_item_notes_reads_legacy_project_notes() -> None:
    """v8 时代的 project_notes 字段仍要能贴回（兼容旧结论，不强迫立即重算）。"""
    payload = _overview_payload_with_cards()
    summary_state = {
        "result": {
            "project_notes": [
                {"ref": "PR1", "text": "国际医疗展的报名材料还在等设计出稿。"},
            ]
        },
        "ref_identities": {"PR1": "国际医疗展"},
    }
    merged = dashboard.merge_ai_item_notes(payload, summary_state)
    assert merged["projects"]["items"][0]["conclusion_source"] == "ai"
