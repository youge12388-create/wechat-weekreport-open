"""Sprint 2 查看接口测试：统计口径与筛选正确性。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.connectors.wecom.client import WeComClient
from app.main import create_app
from app.models.ai_analysis import AIAnalysis
from app.models.employee import Employee
from app.models.report_item import ReportItem
from app.models.report_score import ReportScore
from app.models.blocker import Blocker
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services.view_service import default_period
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


def _ts(y: int, m: int, d: int, h: int = 10) -> int:
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


def _seed(session: Session) -> None:
    depts = [_dept(1, "总部"), _dept(2, "信息部", 1), _dept(3, "咨询部", 1)]
    emps = [
        _user("u1", "张三", 2, position="工程师"),
        _user("u2", "李四", 2, position="开发"),
        _user("u3", "王五", 3, position="顾问"),   # 未提交
        _user("u4", "赵六", 3, position="顾问", status=5),  # 已离职，不算
    ]
    reports = {
        # 本周 W36: 2026-09-07~09-13，在周五（09-11）本人当周提交
        "j1": _journal_info(report_time=_ts(2026, 9, 11), userid="u1",
                            apply_data=text_apply_data("本周开发 A 功能")),
        "j2": _journal_info(report_time=_ts(2026, 9, 11, 18), userid="u2",
                            apply_data=text_apply_data("本周测试 B 模块")),
        # 上周 W35: 2026-08-31~09-06 的周报，在周一（09-07）交 ——
        # 要归上周（见 test_period_range_*），不能算成本周的提交
        "j3": _journal_info(report_time=_ts(2026, 9, 7), userid="u2",
                            apply_data=text_apply_data("上周开发 C 功能")),
    }
    client = FakeWeComClient(departments=depts, employees=emps, reports=reports)
    sync_departments(session, client)
    sync_employees(session, client)
    sync_weekly_reports(session, datetime(2026, 8, 30), datetime(2026, 9, 13), client)


@pytest.fixture
def seeded_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    monkeypatch.setenv("DB_URL", "sqlite://")
    get_settings.cache_clear()
    dispose_engine()
    engine = get_engine(get_settings())
    Base.metadata.create_all(engine)
    session = get_session_factory()()
    _seed(session)
    try:
        yield session
    finally:
        session.close()
        dispose_engine()
        get_settings.cache_clear()


@pytest.fixture
def client(seeded_session: Session) -> Iterator[TestClient]:
    from app.api import deps

    app = create_app()

    def _override():
        yield seeded_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


WEEK = {"start": "2026-09-07", "end": "2026-09-13"}
LAST_WEEK = {"start": "2026-08-31", "end": "2026-09-06"}


def test_dashboard_overview_counts(client: TestClient) -> None:
    response = client.get("/api/dashboard/overview", params=WEEK)
    assert response.status_code == 200
    body = response.json()
    # 在职 = 张三/李四/王五（赵六离职不算）
    assert body["employee_total"] == 3
    assert body["submitted"] == 2          # 张三、李四
    assert body["unsubmitted"] == 1        # 王五
    assert body["submit_rate"] == round(2 / 3 * 100, 1)
    assert body["work_item_total"] == 0    # AI 未上线，真实统计
    assert body["report_total"] == 2
    assert body["average_score"] is None
    assert len(body["submission_trend"]) == 8
    assert body["submission_trend"][-1]["submitted"] == 2
    assert body["abnormal_reports"] == []


def test_dashboard_home_score_and_abnormal_report_are_evidence_based(
    client: TestClient, seeded_session: Session
) -> None:
    report = seeded_session.query(WeeklyReport).filter_by(external_id="j1").one()
    report.ai_checked_at = datetime(2026, 9, 10, 12, 0, 0)
    seeded_session.add_all(
        [
            ReportScore(report_id=report.id, total_score=88),
            AIAnalysis(
                entity_type="weekly_report",
                entity_id=report.id,
                analysis_type="report_analysis",
                status="success",
                result_json={"is_thin": True},
            ),
            ReportItem(
                report_id=report.id,
                item_type="risks",
                item_order=0,
                title="供应商交付风险",
                source_text="供应商交付存在风险",
                risk_level="高",
            ),
        ]
    )
    seeded_session.commit()

    body = client.get("/api/dashboard/overview", params=WEEK).json()
    assert body["average_score"] == 88.0
    assert body["abnormal_reports"][0]["id"] == report.id
    assert body["abnormal_reports"][0]["reasons"] == ["存在高风险事项", "信息不足"]

    departments = client.get("/api/dashboard/departments", params=WEEK).json()
    info = next(item for item in departments["items"] if item["name"] == "信息部")
    assert info["average_score"] == 88.0


def test_exempt_reports_are_excluded_from_management_aggregates(
    client: TestClient, seeded_session: Session
) -> None:
    report = seeded_session.query(WeeklyReport).filter_by(external_id="j1").one()
    report.employee.report_exempt = True
    item = ReportItem(
        report_id=report.id,
        item_type="blockers",
        item_order=0,
        title="免提交报告卡点",
        source_text="免提交报告卡点",
    )
    task = Task(
        canonical_key="exempt-management-task",
        title="免提交报告卡点",
        status="ONGOING",
        first_seen_date=date(2026, 9, 7),
        last_seen_date=date(2026, 9, 7),
    )
    seeded_session.add_all([item, task])
    seeded_session.flush()
    seeded_session.add(
        Blocker(
            task_id=task.id,
            title="免提交报告卡点",
            status="OPEN",
            risk_level="高",
            first_seen_date=date(2026, 9, 7),
            last_seen_date=date(2026, 9, 7),
        )
    )
    seeded_session.flush()
    seeded_session.add(
        TaskReportLink(
            task_id=task.id,
            report_id=report.id,
            report_item_id=item.id,
        )
    )
    seeded_session.commit()

    overview = client.get("/api/dashboard/overview", params=WEEK).json()
    assert overview["report_total"] == 1
    assert overview["work_item_total"] == 0
    assert overview["blocker_total"] == 0
    assert overview["abnormal_reports"] == []
    assert overview["analysis_coverage"]["reports_total"] == 1
    assert overview["analysis_coverage"]["analyzed_reports"] == 0

    departments = client.get("/api/dashboard/departments", params=WEEK).json()
    info = next(item for item in departments["items"] if item["name"] == "信息部")
    assert info["reports_count"] == 1
    assert info["work_item_total"] == 0
    assert info["blocker_total"] == 0

    blockers = client.get("/api/blockers", params=WEEK).json()
    assert blockers["items"] == []


def test_dashboard_overview_respects_period(client: TestClient) -> None:
    body = client.get("/api/dashboard/overview", params=LAST_WEEK).json()
    assert body["submitted"] == 1  # 只有李四上周提交


def test_dashboard_departments(client: TestClient) -> None:
    body = client.get("/api/dashboard/departments", params=WEEK).json()
    by_name = {item["name"]: item for item in body["items"]}
    info = by_name["信息部"]
    assert info["employee_count"] == 2
    assert info["submitted"] == 2
    assert info["unsubmitted"] == 0
    assert info["reports_count"] == 2
    consult = by_name["咨询部"]
    assert consult["employee_count"] == 1
    assert consult["submitted"] == 0
    assert consult["unsubmitted"] == 1


def test_reports_list_filters(client: TestClient) -> None:
    # 部门 + 周期筛选
    body = client.get(
        "/api/reports",
        params={**WEEK, "department_id": 2},
    ).json()
    assert body["total"] == 2

    # 关键字筛选
    body = client.get("/api/reports", params={**WEEK, "q": "B 模块"}).json()
    assert body["total"] == 1
    assert body["items"][0]["employee_name"] == "李四"

    # 上周只有一条
    body = client.get("/api/reports", params=LAST_WEEK).json()
    assert body["total"] == 1
    assert body["items"][0]["report_type"] == "weekly"


def test_employee_all_history_is_not_limited_to_default_period(client: TestClient, seeded_session: Session) -> None:
    employee = seeded_session.query(Employee).filter_by(external_id="u2").one()
    body = client.get(
        "/api/reports",
        params={"employee_id": employee.id, "all_history": "true", "page_size": 1},
    ).json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    second_page = client.get(
        "/api/reports",
        params={"employee_id": employee.id, "all_history": "true", "page": 2, "page_size": 1},
    ).json()
    assert len(second_page["items"]) == 1
    assert second_page["items"][0]["id"] != body["items"][0]["id"]


def test_reports_can_filter_english_content(client: TestClient, seeded_session: Session) -> None:
    employee = seeded_session.query(WeeklyReport).filter_by(external_id="j1").one().employee
    report = WeeklyReport(
        employee_id=employee.id,
        external_id="english-weekly",
        report_type="weekly",
        title="English weekly report",
        content="Completed migration validation and prepared the release plan.",
        content_raw="Completed migration validation and prepared the release plan.",
        content_language="english",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    seeded_session.add(report)
    seeded_session.commit()

    body = client.get("/api/reports", params={**WEEK, "content_language": "english"}).json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == report.id
    assert body["items"][0]["content_language"] == "english"


def test_reports_cover_ai_flags_and_work_state_filters(
    client: TestClient, seeded_session: Session
) -> None:
    reports = {
        report.external_id: report
        for report in seeded_session.query(WeeklyReport).all()
    }
    reports["j1"].ai_checked_at = datetime(2026, 9, 12, 10)
    reports["j1"].ai_duplicate_flag = True
    reports["j1"].ai_quality_flag = False
    reports["j2"].ai_checked_at = datetime(2026, 9, 12, 11)
    reports["j2"].ai_duplicate_flag = False
    reports["j2"].ai_quality_flag = True
    reports["j3"].ai_checked_at = datetime(2026, 9, 7, 10)
    reports["j3"].ai_duplicate_flag = False
    reports["j3"].ai_quality_flag = False
    normal_report = WeeklyReport(
        employee_id=reports["j1"].employee_id,
        external_id="checked-normal",
        report_type="weekly",
        title="正常周报",
        content="本周完成接口联调",
        content_raw="本周完成接口联调",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        submitted_at=datetime(2026, 9, 12, 12),
        source="wecom",
        ai_checked_at=datetime(2026, 9, 12, 12),
        ai_duplicate_flag=False,
        ai_quality_flag=False,
    )
    unchecked_report = WeeklyReport(
        employee_id=reports["j1"].employee_id,
        external_id="unchecked-current-week",
        report_type="weekly",
        title="未检测周报",
        content="本周准备验收",
        content_raw="本周准备验收",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        submitted_at=datetime(2026, 9, 13, 12),
        source="wecom",
    )
    seeded_session.add_all([normal_report, unchecked_report])
    seeded_session.flush()
    seeded_session.add_all(
        [
            ReportItem(
                report_id=reports["j1"].id,
                employee_id=reports["j1"].employee_id,
                item_type="completed_items",
                item_order=0,
                title="完成迁移",
                source_text="完成迁移",
            ),
            ReportItem(
                report_id=reports["j2"].id,
                employee_id=reports["j2"].employee_id,
                item_type="next_week_plans",
                item_order=0,
                title="继续验收",
                source_text="继续验收",
            ),
        ]
    )
    seeded_session.commit()

    def report_ids(**params: str) -> set[int]:
        body = client.get("/api/reports", params={**WEEK, **params}).json()
        return {item["id"] for item in body["items"]}

    assert report_ids(ai_flag="duplicate") == {reports["j1"].id}
    assert report_ids(ai_flag="thin") == {reports["j2"].id}
    assert report_ids(ai_flag="normal") == {normal_report.id}
    assert report_ids(ai_flag="unchecked") == {unchecked_report.id}
    assert report_ids(work_state="done") == {reports["j1"].id}
    assert report_ids(work_state="ongoing") == {reports["j2"].id}
    assert client.get("/api/reports", params={**WEEK, "ai_flag": "other"}).status_code == 400
    assert client.get("/api/reports", params={**WEEK, "work_state": "other"}).status_code == 400
    assert client.get(
        "/api/reports", params={**WEEK, "content_language": "unknown"}
    ).status_code == 400


def test_report_list_pagination_and_detail_for_checked_report(
    client: TestClient, seeded_session: Session
) -> None:
    checked = seeded_session.query(WeeklyReport).filter_by(external_id="j1").one()
    checked.ai_checked_at = datetime(2026, 9, 12, 10)
    seeded_session.commit()

    page = client.get("/api/reports", params={**WEEK, "page": 2, "page_size": 1}).json()
    assert page["total"] == 2
    assert page["page"] == 2
    assert len(page["items"]) == 1

    detail = client.get(f"/api/reports/{checked.id}").json()
    assert detail["duplicate"] is None
    assert detail["quality"] is None
    assert detail["analysis"] is None
    assert detail["analysis_queue"] is None


def test_reports_list_ai_status_placeholder(client: TestClient) -> None:
    body = client.get("/api/reports", params=WEEK).json()
    for item in body["items"]:
        assert item["ai_status"] == "not_processed"
        assert item["work_item_count"] == 0


def test_report_detail(client: TestClient) -> None:
    body = client.get("/api/reports", params=WEEK).json()
    report_id = body["items"][0]["id"]

    detail = client.get(f"/api/reports/{report_id}").json()
    assert detail["id"] == report_id
    assert detail["employee"]["name"] in ("张三", "李四")
    assert detail["content"]  # 清洗后
    assert detail["content_raw"]  # 原始
    assert detail["employee"]["department_name"] == "信息部"
    assert detail["raw_record_id"] is not None
    assert detail["ai_analyses"] == []


def test_employees_list_and_detail(client: TestClient) -> None:
    body = client.get("/api/employees").json()
    assert body["total"] == 4  # 含离职
    active = client.get("/api/employees", params={"status": "active"}).json()
    assert active["total"] == 3

    zhang = next(i for i in body["items"] if i["name"] == "张三")
    assert zhang["department_name"] == "信息部"
    assert zhang["last_report_at"] is not None

    detail = client.get(f"/api/employees/{zhang['id']}").json()
    assert detail["reports_total"] == 1
    reports = client.get(f"/api/employees/{zhang['id']}/reports").json()
    assert len(reports) == 1


def test_employees_submit_status_filter(client: TestClient) -> None:
    """首页「未提交名单」的数据源：口径必须与看板一致（在职 - 已提交）。"""
    unsubmitted = client.get(
        "/api/employees", params={**WEEK, "submit_status": "unsubmitted"}
    ).json()
    # 在职 3 人，已提交 2 人 → 未提交 1 人（王五）；离职的赵六不进名单
    assert unsubmitted["total"] == 1
    assert unsubmitted["items"][0]["name"] == "王五"
    assert unsubmitted["items"][0]["has_submitted"] is False

    submitted = client.get(
        "/api/employees", params={**WEEK, "submit_status": "submitted"}
    ).json()
    assert submitted["total"] == 2
    assert {i["name"] for i in submitted["items"]} == {"张三", "李四"}
    assert all(i["has_submitted"] is True for i in submitted["items"])

    # 不传 submit_status 时行为不变（含离职共 4 人）
    assert client.get("/api/employees").json()["total"] == 4


def test_exempt_department_excluded_from_reporting_base(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """免提交部门：不进基数、不进分子、不进催交名单，但已交的周报仍可查。"""
    # 信息部有张三/李四（本周都交了），把整个部门设为免提交
    monkeypatch.setattr(
        get_settings(), "report_exempt_departments", ["信息部"]
    )

    body = client.get("/api/dashboard/overview", params=WEEK).json()
    # 基数从 3 → 1（只剩咨询部王五）
    assert body["employee_total"] == 1
    # 分子也要剔除：否则会出现「1 人应提交、2 人已提交」的怪数字
    assert body["submitted"] == 0
    assert body["unsubmitted"] == 1

    unsubmitted = client.get(
        "/api/employees", params={**WEEK, "submit_status": "unsubmitted"}
    ).json()
    assert [i["name"] for i in unsubmitted["items"]] == ["王五"]

    # 已提交的周报仍在（只影响统计，不影响存档）
    assert client.get("/api/reports", params=WEEK).json()["total"] == 2

    depts = {d["name"]: d for d in client.get("/api/departments", params=WEEK).json()}
    assert depts["信息部"]["exempt"] is True
    assert depts["信息部"]["employee_count"] == 0
    assert depts["咨询部"]["exempt"] is False


def test_remind_is_dry_run_by_default(client: TestClient) -> None:
    """不显式要求就不能真发 —— 发消息撤回不了，默认必须只预览。"""
    body = client.post(
        "/api/notifications/remind", json={**WEEK}
    ).json()
    assert body["dry_run"] is True
    assert body["sent"] is False
    assert body["target_count"] == 1          # 本周只有王五没交
    assert body["targets"][0]["name"] == "王五"
    assert body["targets"][0]["userid"] == "u3"
    assert body["card"]["title"] == "周报提醒 · Weekly Report"
    # 库里存在英文周报，卡片必须带英文正文，否则这批人等于没被提醒
    assert "has not been submitted" in body["card"]["description"]


def test_remind_send_invokes_wecom_card(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式 dry_run=false 时才投递。用假发送函数验证，绝不打真实接口。"""
    captured: dict[str, object] = {}

    def fake_send_text_card(
        self, user_ids, title, description, url, btn_text="去填写"
    ) -> dict:
        captured.update(
            user_ids=user_ids, title=title, description=description, url=url
        )
        return {"errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr(WeComClient, "send_text_card", fake_send_text_card)

    body = client.post(
        "/api/notifications/remind", json={**WEEK, "dry_run": False}
    ).json()
    assert body["sent"] is True
    assert body["result"]["errcode"] == 0
    assert body["result"]["delivered_count"] == 1
    assert captured["user_ids"] == ["u3"]
    assert "周报" in str(captured["description"])


def test_default_period_switches_after_weekly_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """周五 21:00 前默认看上周，之后看本周 —— 避免周一一早满屏假警报。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "report_due_weekday", 4)   # 周五
    monkeypatch.setattr(settings, "report_due_hour", 21)

    # 2026-09-07(一) ~ 09-13(日) 是本周，08-31 ~ 09-06 是上周
    assert default_period(datetime(2026, 9, 10, 20, 0)) == (  # 周四 20:00，未到
        date(2026, 8, 31), date(2026, 9, 6)
    )
    assert default_period(datetime(2026, 9, 11, 21, 0)) == (  # 周五 21:00 整点，已到
        date(2026, 9, 7), date(2026, 9, 13)
    )
    assert default_period(datetime(2026, 9, 12, 10, 0)) == (  # 周六
        date(2026, 9, 7), date(2026, 9, 13)
    )


def test_employees_submit_status_requires_valid_period(client: TestClient) -> None:
    """缺周期参数必须 400，不能静默取默认周，否则统计口径会漂。"""
    assert client.get(
        "/api/employees", params={"submit_status": "unsubmitted"}
    ).status_code == 400
    assert client.get(
        "/api/employees", params={"start": "2026-09-07", "submit_status": "unsubmitted"}
    ).status_code == 400
    assert client.get(
        "/api/employees", params={**WEEK, "submit_status": "typo"}
    ).status_code == 400


def test_employee_exemption_updates_validate_all_ids_and_take_effect(
    client: TestClient, seeded_session: Session
) -> None:
    zhang = seeded_session.query(Employee).filter_by(external_id="u1").one()
    li = seeded_session.query(Employee).filter_by(external_id="u2").one()

    updated = client.patch(
        f"/api/employees/{zhang.id}", json={"report_exempt": True}
    )
    assert updated.status_code == 200
    assert updated.json()["report_exempt"] is True

    batch = client.patch(
        "/api/employees/report-exemptions",
        json={"updates": [{"id": li.id, "report_exempt": True}]},
    )
    assert batch.status_code == 200
    assert batch.json()["updated"] == 1
    assert batch.json()["items"][0]["report_exempt"] is True
    assert client.get("/api/employees", params={"report_exempt": "true"}).json()["total"] == 2

    missing = client.patch(
        "/api/employees/report-exemptions",
        json={"updates": [{"id": 999999, "report_exempt": False}]},
    )
    assert missing.status_code == 404
    assert client.patch(
        "/api/employees/999999", json={"report_exempt": True}
    ).status_code == 404


def test_employee_roster_reportable_filter(client: TestClient) -> None:
    """应交周报名单：在职且未免提交；离职的人不出现，且带累计篇数。

    这是周报查询页「人员名单」视图的数据源：名单必须与统计/催交同口径，
    否则页面名单和未提交统计会出现两套人数。
    """
    body = client.get("/api/employees", params={"reportable": "true"})
    assert body.status_code == 200
    by_name = {item["name"]: item for item in body.json()["items"]}
    # 张三/李四/王五在职；赵六离职不算
    assert set(by_name) == {"张三", "李四", "王五"}
    assert by_name["张三"]["reports_total"] == 1
    assert by_name["李四"]["reports_total"] == 2
    assert by_name["王五"]["reports_total"] == 0
    assert by_name["张三"]["last_report_at"] is not None
    assert by_name["王五"]["last_report_at"] is None


def test_employee_roster_excludes_exempt_after_update(
    client: TestClient, seeded_session: Session
) -> None:
    """免提交开关是把人移出/移回名单的唯一人工入口，必须即时生效。"""
    zhang = seeded_session.query(Employee).filter_by(external_id="u1").one()
    client.patch(f"/api/employees/{zhang.id}", json={"report_exempt": True})
    body = client.get("/api/employees", params={"reportable": "true"}).json()
    assert {item["name"] for item in body["items"]} == {"李四", "王五"}


def test_employee_analysis_validates_end_date_and_returns_analysis(
    client: TestClient, seeded_session: Session
) -> None:
    employee = seeded_session.query(Employee).filter_by(external_id="u1").one()

    assert client.get(f"/api/employees/{employee.id}/analysis", params={"end": "not-a-date"}).status_code == 400
    response = client.get(
        f"/api/employees/{employee.id}/analysis",
        params={"end": "2026-09-13", "weeks": 8},
    )
    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_departments_list_and_detail(client: TestClient) -> None:
    body = client.get("/api/departments", params=WEEK).json()
    by_name = {item["name"]: item for item in body}
    # 员工直接挂在子部门下，总部无直属员工
    assert by_name["总部"]["employee_count"] == 0
    info = by_name["信息部"]
    assert info["submitted"] == 2
    assert info["unsubmitted"] == 0

    detail = client.get("/api/departments/3", params=WEEK).json()
    assert detail["stats"]["employee_total"] == 1
    assert detail["stats"]["submitted"] == 0
    assert detail["employees"][0]["name"] == "王五"
    assert detail["employees"][0]["has_submitted"] is False


def test_unknown_ids_return_404(client: TestClient) -> None:
    assert client.get("/api/reports/999999").status_code == 404
    assert client.get("/api/employees/999999").status_code == 404
    assert client.get("/api/departments/999999").status_code == 404


def test_date_validation(client: TestClient) -> None:
    # end 早于 start
    response = client.get(
        "/api/dashboard/overview", params={"start": "2026-09-13", "end": "2026-09-07"}
    )
    assert response.status_code in (400, 422)
