"""P2-9 导出：未提交名单 / 周报列表 -> CSV（UTF-8 BOM，Excel 友好）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timezone
import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.main import create_app
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.report_item import ReportItem
from app.models.weekly_report import WeeklyReport
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

PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)


def _ts(y: int, m: int, d: int, h: int = 10) -> int:
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())


def _seed(session: Session) -> None:
    depts = [_dept(1, "总部"), _dept(2, "信息部", 1), _dept(3, "咨询部", 1)]
    emps = [
        _user("u1", "张三", 2, position="工程师"),
        _user("u2", "李四", 2, position="开发"),
        _user("u3", "王五", 3, position="顾问"),  # 未提交
        _user("u4", "赵六", 3, position="顾问", status=5),  # 离职，不进基数
    ]
    reports = {
        # 本期（09-07~09-13）的两篇都在周五当周提交
        "j1": _journal_info(report_time=_ts(2026, 9, 11), userid="u1",
                            apply_data=text_apply_data("本周开发 A 功能")),
        "j2": _journal_info(report_time=_ts(2026, 9, 11, 18), userid="u2",
                            apply_data=text_apply_data("本周测试 B 模块")),
    }
    client = FakeWeComClient(departments=depts, employees=emps, reports=reports)
    sync_departments(session, client)
    sync_employees(session, client)
    sync_weekly_reports(session, datetime(2026, 9, 1), datetime(2026, 9, 13), client)


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

    def _override() -> Iterator[Session]:
        yield seeded_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


WEEK = {"start": "2026-09-07", "end": "2026-09-13"}


def test_export_unsubmitted_csv(client: TestClient) -> None:
    resp = client.get("/api/exports/unsubmitted", params=WEEK)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.content
    # utf-8-sig 必须在文件头写 BOM，否则 Excel 打开中文会乱码
    assert body[:3] == b"\xef\xbb\xbf"
    text = body.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == "姓名,企微ID,部门,岗位"
    # 在职应提交 3 人，已交 2（张三/李四），未交 1（王五）；离职赵六不进名单
    names = [ln.split(",")[0] for ln in lines[1:]]
    assert names == ["王五"]


def test_export_reports_csv_with_keyword(client: TestClient) -> None:
    resp = client.get("/api/exports/reports", params={**WEEK, "q": "B 模块"})
    assert resp.status_code == 200
    assert resp.content[:3] == b"\xef\xbb\xbf"
    text = resp.content.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == "周报ID,员工,部门,类型,标题,周期起,周期止,提交时间,来源,周报原文"
    # 关键字 B 模块走 content 匹配，只命中李四那篇。
    assert len(lines) == 2
    assert "李四" in lines[1]
    assert "信息部" in lines[1]


def test_export_all_history_ignores_page_filters_and_only_exports_weekly(
    client: TestClient, seeded_session: Session
) -> None:
    employee = seeded_session.scalar(
        select(Employee).where(Employee.external_id == "u1")
    )
    assert employee is not None
    seeded_session.add_all(
        [
            WeeklyReport(
                employee_id=employee.id,
                external_id="old-weekly",
                report_type="weekly",
                title="早期周报",
                content="历史正文",
                content_raw="历史正文",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 7),
                submitted_at=datetime(2024, 1, 7, 10, 0, 0),
                source="wecom",
            ),
            WeeklyReport(
                employee_id=employee.id,
                external_id="old-daily",
                report_type="daily",
                title="历史日报",
                content="不应导出",
                content_raw="不应导出",
                start_date=date(2024, 1, 8),
                end_date=date(2024, 1, 8),
                submitted_at=datetime(2024, 1, 8, 10, 0, 0),
                source="wecom",
            ),
        ]
    )
    seeded_session.commit()

    resp = client.get(
        "/api/exports/reports",
        params={
            "all_history": "true",
            **WEEK,
            "department_id": 999,
            "q": "绝对匹配不到",
            "report_type": "daily",
        },
    )

    assert resp.status_code == 200
    assert "weekly_reports_all_history.csv" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert len(rows) == 4  # 表头 + 原有两篇周报 + 一篇早期周报
    titles = {row[4] for row in rows[1:]}
    assert "早期周报" in titles
    assert "历史日报" not in titles
    assert all(row[3] == "weekly" for row in rows[1:])
    assert any(row[-1] == "历史正文" for row in rows[1:])


def test_export_reports_csv_department_filter(client: TestClient) -> None:
    resp = client.get("/api/exports/reports", params={**WEEK, "department_id": 2})
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    lines = text.splitlines()
    # 信息部（id=2）本周 2 篇
    assert len(lines) == 3


def test_export_reports_csv_matches_type_and_work_state_filters(
    client: TestClient, seeded_session: Session
) -> None:
    reports = list(
        seeded_session.scalars(select(WeeklyReport).order_by(WeeklyReport.id))
    )
    done_report, ongoing_report = reports
    done_report.report_type = "other"
    ongoing_report.report_type = "weekly"
    seeded_session.add_all(
        [
            ReportItem(
                report_id=done_report.id,
                employee_id=done_report.employee_id,
                item_type="completed_items",
                item_order=0,
                title="完成 A 功能",
                source_text="本周开发 A 功能",
            ),
            ReportItem(
                report_id=ongoing_report.id,
                employee_id=ongoing_report.employee_id,
                item_type="ongoing_items",
                item_order=0,
                title="继续测试 B 模块",
                source_text="本周测试 B 模块",
            ),
        ]
    )
    seeded_session.commit()

    resp = client.get(
        "/api/exports/reports",
        params={**WEEK, "report_type": "other", "work_state": "done"},
    )
    assert resp.status_code == 200
    lines = resp.content.decode("utf-8-sig").splitlines()
    assert len(lines) == 2
    assert f"{done_report.id}," in lines[1]
    assert ",other," in lines[1]

    no_match = client.get(
        "/api/exports/reports",
        params={**WEEK, "report_type": "other", "work_state": "ongoing"},
    )
    assert no_match.status_code == 200
    assert len(no_match.content.decode("utf-8-sig").splitlines()) == 1


def test_export_reports_rejects_unknown_work_state(client: TestClient) -> None:
    resp = client.get(
        "/api/exports/reports",
        params={**WEEK, "work_state": "blocked"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "work_state 只支持 done / ongoing"


# --------------------------------------------------------------------------- #
# CSV 公式注入：导出内容会被 Excel 当公式执行
# --------------------------------------------------------------------------- #


def test_csv_cells_starting_with_formula_chars_are_neutralized() -> None:
    """以 = + - @ 开头的单元格必须前置单引号，否则 Excel 打开时执行公式。

    标题/姓名/岗位都来自企微（外部数据），写一条 `=HYPERLINK(...)` 形式的标题
    就能在别人打开导出文件时被执行。
    """
    from app.api.routes.exports import _csv_cell

    assert _csv_cell("=HYPERLINK(\"http://evil\",\"点我\")").startswith("'=")
    assert _csv_cell("+1+1").startswith("'+")
    assert _csv_cell("-2+3").startswith("'-")
    assert _csv_cell("@SUM(A1)").startswith("'@")
    assert _csv_cell("\tcmd").startswith("'\t")
    # 正常文本不能被改动
    assert _csv_cell("正常标题") == "正常标题"
    assert _csv_cell("") == ""
    assert _csv_cell(None) == ""
    # 数字 ID 原样（导出用字符串，不该被加引号）
    assert _csv_cell("123") == "123"


def test_export_reports_neutralizes_malicious_title(db_session) -> None:
    """端到端：恶意标题经导出接口后，CSV 里不能再以 = 开头。"""
    employee = Employee(
        source="wecom", external_id="x1", name="张三", status=EmployeeStatus.ACTIVE
    )
    db_session.add(employee)
    db_session.flush()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id="r1",
        report_type="weekly",
        title="=cmd|'/c calc'!A0",
        content="内容",
        content_raw="内容",
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    db_session.add(report)
    db_session.commit()

    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            resp = client.get(
                "/api/exports/reports",
                params={
                    "start": PERIOD_START.isoformat(),
                    "end": PERIOD_END.isoformat(),
                },
            )
        assert resp.status_code == 200
        text = resp.content.decode("utf-8-sig")
        # 必须解析 CSV 再判断，不能直接在原文里找子串：
        # 中和后的 "'=cmd" 依然包含 "=cmd" 这段文字，子串判断会永远通过/失败。
        parsed = list(csv.reader(io.StringIO(text)))
        header, first = parsed[0], parsed[1]
        title = first[header.index("标题")]
        assert not title.startswith("="), "危险公式必须被中和"
        assert title.startswith("'="), "中和方式是前置单引号（Excel 强制文本）"
    finally:
        app.dependency_overrides.clear()


def test_export_reports_is_bounded(db_session) -> None:
    """导出必须带上行数上限，避免「导出全部」把整表读进内存。"""
    from app.api.routes.exports import EXPORT_MAX_ROWS

    assert EXPORT_MAX_ROWS > 0
    employee = Employee(
        source="wecom", external_id="y1", name="李四", status=EmployeeStatus.ACTIVE
    )
    db_session.add(employee)
    db_session.flush()
    # 造 3 篇，确认正常数量下不受影响
    for index in range(3):
        db_session.add(
            WeeklyReport(
                employee_id=employee.id,
                external_id=f"b{index}",
                report_type="weekly",
                title=f"标题{index}",
                content="内容",
                content_raw="内容",
                start_date=PERIOD_START,
                end_date=PERIOD_END,
                submitted_at=datetime(2026, 9, 10, 10, 0, 0),
                source="wecom",
            )
        )
    db_session.commit()

    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            resp = client.get(
                "/api/exports/reports",
                params={
                    "start": PERIOD_START.isoformat(),
                    "end": PERIOD_END.isoformat(),
                },
            )
        assert resp.status_code == 200
        lines = [line for line in resp.content.decode("utf-8-sig").splitlines() if line]
        assert len(lines) == 4, "表头 + 3 行数据"
    finally:
        app.dependency_overrides.clear()
