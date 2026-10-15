"""AI 检索范围（scope）与覆盖率回报 —— 阶段 1 的行为守护。

这条改动要解决两个真实问题：

1. 「全局检索」以前在 API 层**根本不存在**：``resolve_period`` 在缺参数时会落到
   默认周期，所以不传 start/end 永远查不到跨周期内容。
2. 命中为空或被截断时，前端只能拿到一个没有分母的数字，用户无法判断
   「模型是没找到」还是「根本没读」。

全部用 FakeProvider 替身，测试不联网、不花钱。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.fake import FakeProvider
from app.api import deps
from app.main import create_app
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.weekly_report import WeeklyReport
from app.services import ai_search_service

ANSWER = '{"answer": "已根据材料作答。", "sources": [], "not_found": false}'

LAST_WEEK = (date(2026, 8, 31), date(2026, 9, 6))
THIS_WEEK = (date(2026, 9, 7), date(2026, 9, 13))
TWO_WEEKS_AGO = (date(2026, 8, 24), date(2026, 8, 30))


@dataclass
class _Settings:
    """替身运行时配置，用来在测试里压小篇数/字符预算。"""

    ai_provider: str = "fake"
    ai_model: str = "fake-model"
    ai_search_max_reports: int = 30
    ai_max_input_chars: int = 12000


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    settings = _Settings(**overrides)
    monkeypatch.setattr(ai_search_service, "runtime_settings", lambda _session: settings)


def _provider(monkeypatch: pytest.MonkeyPatch, response: str = ANSWER) -> FakeProvider:
    fake = FakeProvider(response=response)
    monkeypatch.setattr(ai_search_service, "get_provider", lambda settings: fake)
    return fake


def _employee(
    session: Session, name: str, department_id: int | None = None
) -> Employee:
    employee = Employee(
        source="wecom",
        external_id=f"emp_{name}",
        name=name,
        status=EmployeeStatus.ACTIVE,
        department_id=department_id,
    )
    session.add(employee)
    session.flush()
    return employee


def _report(
    session: Session,
    employee: Employee,
    title: str,
    content: str,
    period: tuple[date, date],
    submitted: datetime,
) -> WeeklyReport:
    report = WeeklyReport(
        employee_id=employee.id,
        external_id=f"rep_{title}",
        report_type="weekly",
        title=title,
        content=content,
        content_raw=content,
        start_date=period[0],
        end_date=period[1],
        submitted_at=submitted,
        source="wecom",
    )
    session.add(report)
    session.commit()
    return report


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


def test_default_scope_stays_within_period(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 scope 时行为必须与改动前完全一致：只看指定周期。"""
    employee = _employee(db_session, "张三")
    last = _report(
        db_session, employee, "上周", "上周完成 X。", LAST_WEEK, datetime(2026, 9, 3, 10)
    )
    this = _report(
        db_session, employee, "本周", "本周完成 Y。", THIS_WEEK, datetime(2026, 9, 10, 10)
    )
    _provider(monkeypatch)

    result = ai_search_service.search_reports(
        db_session, "做了哪些事？", start="2026-09-07", end="2026-09-13"
    )

    assert result["scope"] == "period"
    assert result["matched_total"] == 1
    assert result["candidate_ids"] == [this.id]
    assert result["truncated"] is False
    assert last.id not in result["candidate_ids"]


def test_scope_all_spans_every_period(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope=all 必须真的跨周期；period 字段回报命中数据的实际跨度。"""
    employee = _employee(db_session, "张三")
    _report(
        db_session, employee, "上周", "上周完成 X。", LAST_WEEK, datetime(2026, 9, 3, 10)
    )
    _report(
        db_session, employee, "本周", "本周完成 Y。", THIS_WEEK, datetime(2026, 9, 10, 10)
    )
    _provider(monkeypatch)

    result = ai_search_service.search_reports(db_session, "做了哪些事？", scope="all")

    assert result["scope"] == "all"
    assert result["matched_total"] == 2
    assert result["read_count"] == 2
    assert result["truncated"] is False
    # 全局检索没有固定周期，用自己的数据跨度如实回报，而不是借用默认周
    assert result["period"] == {"start": "2026-08-31", "end": "2026-09-13"}


def test_scope_all_still_honours_employee_and_department_filters(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """放开周期不等于放开所有筛选：员工/部门条件在全局检索下依然生效。"""
    department = Department(source="wecom", external_id="d1", name="渠道资源部")
    db_session.add(department)
    db_session.flush()

    alpha = _employee(db_session, "甲", department.id)
    beta = _employee(db_session, "乙")
    _report(
        db_session, alpha, "甲上周", "甲的内容。", LAST_WEEK, datetime(2026, 9, 3, 10)
    )
    _report(
        db_session, alpha, "甲本周", "甲的别的内容。", THIS_WEEK, datetime(2026, 9, 10, 10)
    )
    _report(
        db_session, beta, "乙本周", "乙的内容。", THIS_WEEK, datetime(2026, 9, 10, 11)
    )
    _provider(monkeypatch)

    by_employee = ai_search_service.search_reports(
        db_session, "做了哪些事？", scope="all", employee_id=alpha.id
    )
    assert by_employee["matched_total"] == 2

    by_department = ai_search_service.search_reports(
        db_session, "做了哪些事？", scope="all", department_id=department.id
    )
    assert by_department["matched_total"] == 2


def test_keyword_miss_returns_no_result_without_expanding_evidence(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关键词严格筛选时无命中不能悄悄改喂其他周报。"""
    employee = _employee(db_session, "李四")
    _report(
        db_session,
        employee,
        "本周进展",
        "上线时间往后挪了两周。",
        THIS_WEEK,
        datetime(2026, 9, 10, 10),
    )
    fake = _provider(monkeypatch)

    result = ai_search_service.search_reports(
        db_session,
        "交付延期风险？",
        start="2026-09-07",
        end="2026-09-13",
        keyword="交付延期",
    )

    assert result["fallback"] is False
    assert result["matched_total"] == 0
    assert result["read_count"] == 0
    assert result["not_found"] is True
    assert "没有找到任何周报" in result["answer"]
    assert fake.calls == []


def test_keyword_hit_does_not_fall_back(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关键词命中了就不该多喂无关周报。"""
    employee = _employee(db_session, "王五")
    _report(
        db_session,
        employee,
        "本周进展",
        "完成了支付接口联调，已提测。",
        THIS_WEEK,
        datetime(2026, 9, 10, 10),
    )
    _report(
        db_session,
        employee,
        "另一篇",
        "整理了下季度的招聘需求。",
        THIS_WEEK,
        datetime(2026, 9, 10, 11),
    )
    fake = _provider(monkeypatch)

    result = ai_search_service.search_reports(
        db_session,
        "支付接口进展如何？",
        start="2026-09-07",
        end="2026-09-13",
        keyword="支付接口",
    )

    assert result["fallback"] is False
    assert result["matched_total"] == 1
    assert "招聘需求" not in fake.calls[0]["user"]
    assert "放宽条件" not in fake.calls[0]["user"]


def test_truncation_reports_matched_total_and_read_count(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过篇数上限时必须如实回报「命中多少、读了多少」，而不是静默截断。"""
    employee = _employee(db_session, "赵六")
    for index in range(3):
        _report(
            db_session,
            employee,
            f"第{index}篇",
            f"第 {index} 篇的正文内容。",
            THIS_WEEK,
            datetime(2026, 9, 10, 10 + index),
        )
    _patch_settings(monkeypatch, ai_search_max_reports=2)
    fake = _provider(monkeypatch)

    result = ai_search_service.search_reports(
        db_session, "这周都在做什么？", start="2026-09-07", end="2026-09-13"
    )

    assert result["matched_total"] == 3
    assert result["read_count"] == 2
    assert result["truncated"] is True
    assert "样本不完整" in fake.calls[0]["user"]


def test_char_budget_never_yields_zero_reads(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单篇就超预算时也必须读进一篇：「预算不够」不能被下游读成「库里没有」。"""
    employee = _employee(db_session, "孙七")
    _report(
        db_session,
        employee,
        "超长周报",
        "很长的正文。" * 500,
        THIS_WEEK,
        datetime(2026, 9, 10, 10),
    )
    _patch_settings(monkeypatch, ai_max_input_chars=80)
    fake = _provider(monkeypatch)

    result = ai_search_service.search_reports(
        db_session, "有什么风险？", start="2026-09-07", end="2026-09-13"
    )

    assert result["read_count"] == 1
    # 关键在「模型真的被调用了」，而不是走进了「库里没有周报」那条短路分支
    assert "没有找到任何周报" not in result["answer"]
    assert len(fake.calls) == 1


def test_scope_all_with_empty_db_skips_model(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全局检索遇到空库仍然是「不花钱的确定答案」。"""
    fake = _provider(monkeypatch)

    result = ai_search_service.search_reports(db_session, "有什么风险？", scope="all")

    assert result["scope"] == "all"
    assert result["matched_total"] == 0
    assert result["read_count"] == 0
    assert result["not_found"] is True
    assert result["period"] == {"start": None, "end": None}
    assert fake.calls == []


def test_invalid_scope_is_rejected(db_session: Session) -> None:
    """非法范围直接报错，不静默降级成某个默认范围。"""
    with pytest.raises(ValueError):
        ai_search_service.search_reports(db_session, "问题", scope="everything")


def _periods_of(session: Session, report_ids: list[int]) -> list[date]:
    """按 report_ids 给出的顺序取回各自所属周期，用来断言抽样的时间跨度。"""
    by_id = {
        report.id: report.start_date
        for report in session.scalars(
            select(WeeklyReport).where(WeeklyReport.id.in_(report_ids))
        )
    }
    return [by_id[report_id] for report_id in report_ids]


def test_scope_all_takes_one_report_per_period(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全局检索每周期只留一篇，且保留的是该周期最新提交的那一篇。"""
    employee = _employee(db_session, "轮转")
    _report(
        db_session, employee, "本周早", "本周早内容。", THIS_WEEK, datetime(2026, 9, 10, 10)
    )
    _report(
        db_session, employee, "本周晚", "本周晚内容。", THIS_WEEK, datetime(2026, 9, 10, 12)
    )
    _report(
        db_session, employee, "上周", "上周内容。", LAST_WEEK, datetime(2026, 9, 3, 10)
    )
    _report(
        db_session, employee, "前两周", "前两周内容。", TWO_WEEKS_AGO, datetime(2026, 8, 27, 10)
    )
    _provider(monkeypatch)

    result = ai_search_service.search_reports(db_session, "整体情况如何？", scope="all")

    assert result["matched_total"] == 4
    assert len(result["candidate_ids"]) == 3
    assert _periods_of(db_session, result["candidate_ids"]) == [
        THIS_WEEK[0],
        LAST_WEEK[0],
        TWO_WEEKS_AGO[0],
    ]
    newest = db_session.get(WeeklyReport, result["candidate_ids"][0])
    assert newest.title == "本周晚"


def test_scope_all_does_not_collapse_into_the_newest_period(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候选篇数受限时不能全落在最近一周 —— 这是实测踩到的回归。"""
    employee = _employee(db_session, "轮转2")
    for hour in (10, 11, 12):
        _report(
            db_session,
            employee,
            f"本周{hour}",
            f"本周 {hour} 点的内容。",
            THIS_WEEK,
            datetime(2026, 9, 10, hour),
        )
    _report(
        db_session, employee, "上周", "上周内容。", LAST_WEEK, datetime(2026, 9, 3, 10)
    )
    _patch_settings(monkeypatch, ai_search_max_reports=2)
    _provider(monkeypatch)

    result = ai_search_service.search_reports(db_session, "整体情况如何？", scope="all")

    periods = _periods_of(db_session, result["candidate_ids"])
    assert len(periods) == 2
    assert len(set(periods)) == 2, "两篇候选落在同一个周期，说明轮转抽样失效了"
    assert periods == [THIS_WEEK[0], LAST_WEEK[0]]


def test_scope_all_fair_share_keeps_period_coverage(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一篇超长周报不能吃掉整个预算，把周期覆盖度打回一周。"""
    employee = _employee(db_session, "配额")
    _report(
        db_session,
        employee,
        "本周超长",
        "超长正文。" * 2500,
        THIS_WEEK,
        datetime(2026, 9, 10, 10),
    )
    _report(
        db_session, employee, "上周", "上周的简短内容。", LAST_WEEK, datetime(2026, 9, 3, 10)
    )
    _report(
        db_session,
        employee,
        "前两周",
        "前两周的简短内容。",
        TWO_WEEKS_AGO,
        datetime(2026, 8, 27, 10),
    )
    _patch_settings(monkeypatch, ai_max_input_chars=3000)
    fake = _provider(monkeypatch)

    result = ai_search_service.search_reports(db_session, "整体情况如何？", scope="all")

    assert result["read_count"] == 3
    assert len(set(_periods_of(db_session, result["candidate_ids"]))) == 3
    assert "已按单篇配额截断" in fake.calls[0]["user"]


def test_search_endpoint_rejects_invalid_scope(client: TestClient) -> None:
    resp = client.post("/api/ai/search", json={"question": "有什么风险？", "scope": "everything"})
    assert resp.status_code == 400


def test_search_endpoint_accepts_all_scope(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_settings(monkeypatch)
    _provider(monkeypatch)
    employee = _employee(db_session, "周八")
    _report(
        db_session, employee, "本周", "本周完成了支付联调。", THIS_WEEK, datetime(2026, 9, 10, 10)
    )

    resp = client.post("/api/ai/search", json={"question": "做了哪些事？", "scope": "all"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "all"
    assert body["matched_total"] == 1
    assert body["read_count"] == 1
    assert body["fallback"] is False
