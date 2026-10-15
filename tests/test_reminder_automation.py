"""个人免提交与后台自动催交回归测试。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import deps
from app.main import create_app
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import EmployeeStatus, ReminderStatus
from app.models.reminder_log import ReminderLog
from app.models.weekly_report import WeeklyReport
from app.services.reminder_scheduler import maybe_send_scheduled_reminders
from app.services.runtime_config_service import runtime_settings, update_runtime_values
from app.services.view_service import reportable_employee_ids

PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)
FRIDAY_DUE_UTC = datetime(2026, 9, 11, 13, 0, 0)


def _employee(
    session: Session,
    external_id: str,
    name: str,
    department_id: int,
    *,
    report_exempt: bool = False,
) -> Employee:
    employee = Employee(
        source="wecom",
        external_id=external_id,
        name=name,
        department_id=department_id,
        status=EmployeeStatus.ACTIVE,
        report_exempt=report_exempt,
    )
    session.add(employee)
    session.flush()
    return employee


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


def test_individual_exempt_employee_is_removed_from_reporting_scope(
    client: TestClient, db_session: Session
) -> None:
    department = Department(source="wecom", external_id="d1", name="总部")
    db_session.add(department)
    db_session.flush()
    employee = _employee(db_session, "u1", "领导", department.id)
    db_session.commit()
    assert employee.id in reportable_employee_ids(db_session)

    response = client.patch(
        f"/api/employees/{employee.id}", json={"report_exempt": True}
    )
    assert response.status_code == 200
    assert response.json()["report_exempt"] is True
    assert employee.id not in reportable_employee_ids(db_session)

    exempt = client.get(
        "/api/employees", params={"report_exempt": "true", "status": "active"}
    ).json()
    assert exempt["total"] == 1
    assert exempt["items"][0]["name"] == "领导"

    restored = client.patch(
        f"/api/employees/{employee.id}", json={"report_exempt": False}
    )
    assert restored.status_code == 200
    assert employee.id in reportable_employee_ids(db_session)


def test_batch_report_exemption_update(
    client: TestClient, db_session: Session
) -> None:
    department = Department(source="wecom", external_id="d1", name="总部")
    db_session.add(department)
    db_session.flush()
    first = _employee(db_session, "u1", "领导一", department.id)
    second = _employee(db_session, "u2", "领导二", department.id)
    db_session.commit()

    response = client.patch(
        "/api/employees/report-exemptions",
        json={
            "updates": [
                {"id": first.id, "report_exempt": True},
                {"id": second.id, "report_exempt": True},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["updated"] == 2
    assert reportable_employee_ids(db_session) == set()

    restored = client.patch(
        "/api/employees/report-exemptions",
        json={"updates": [{"id": first.id, "report_exempt": False}]},
    )
    assert restored.status_code == 200
    assert reportable_employee_ids(db_session) == {first.id}


def test_scheduled_reminder_sends_once_and_respects_individual_exemption(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    department = Department(source="wecom", external_id="d1", name="总部")
    db_session.add(department)
    db_session.flush()
    submitted = _employee(db_session, "submitted", "已提交", department.id)
    pending = _employee(db_session, "pending", "未提交", department.id)
    exempt = _employee(
        db_session, "exempt", "领导", department.id, report_exempt=True
    )
    db_session.add(
        WeeklyReport(
            employee_id=submitted.id,
            external_id="report-1",
            report_type="weekly",
            content="完成工作",
            content_raw="完成工作",
            start_date=PERIOD_START,
            end_date=PERIOD_END,
            source="wecom",
        )
    )
    db_session.commit()
    update_runtime_values(
        db_session,
        {
            "remind_auto_enabled": "true",
            "remind_auto_weekday": "4",
            "remind_auto_hour": "21",
        },
    )
    db_session.commit()
    assert runtime_settings(db_session).remind_auto_enabled is True

    sent: list[list[str]] = []

    def fake_send_text_card(self, user_ids, *args, **kwargs):
        sent.append(list(user_ids))
        return {"errcode": 0, "errmsg": "ok"}

    from app.connectors.wecom.client import WeComClient

    monkeypatch.setattr(WeComClient, "send_text_card", fake_send_text_card)
    first = maybe_send_scheduled_reminders(db_session, now=FRIDAY_DUE_UTC)
    assert first["status"] == "sent"
    assert sent == [[pending.external_id]]
    assert exempt.external_id not in sent[0]

    second = maybe_send_scheduled_reminders(db_session, now=FRIDAY_DUE_UTC)
    assert second["status"] == "already_sent"
    assert sent == [[pending.external_id]]

    logs = db_session.query(ReminderLog).all()
    assert len(logs) == 1
    assert logs[0].employee_id == pending.id
    assert logs[0].status == ReminderStatus.SENT
    assert logs[0].triggered_by.startswith("auto:weekly_due:")


def test_scheduled_reminder_waits_until_configured_time(db_session: Session) -> None:
    update_runtime_values(db_session, {"remind_auto_enabled": "true"})
    db_session.commit()

    before_due = datetime(2026, 9, 11, 12, 59, 0)
    result = maybe_send_scheduled_reminders(db_session, now=before_due)
    assert result["status"] == "not_due"
    assert result["next_run_at"] == FRIDAY_DUE_UTC


def test_scheduled_reminder_records_no_target_run(db_session: Session) -> None:
    update_runtime_values(db_session, {"remind_auto_enabled": "true"})
    db_session.commit()

    result = maybe_send_scheduled_reminders(db_session, now=FRIDAY_DUE_UTC)
    assert result["status"] == "no_targets"

    from app.models.reminder_run import ReminderRun
    from app.services.reminder_scheduler import reminder_status

    run = db_session.query(ReminderRun).one()
    assert run.status == "skipped"
    status = reminder_status(db_session, now=FRIDAY_DUE_UTC)
    assert status["last_status"] == "skipped"
    assert status["last_target_count"] == 0


def test_settings_api_can_enable_and_read_automation(
    client: TestClient, db_session: Session
) -> None:
    response = client.patch(
        "/api/settings/reminder",
        json={"auto_enabled": True, "weekday": 4, "hour": 21},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["auto_enabled"] is True
    assert body["weekday_label"] == "周五"
    assert body["hour"] == 21
    assert body["next_run_at"]

    loaded = client.get("/api/settings/reminder").json()
    assert loaded["auto_enabled"] is True
    assert loaded["current_period"]["start"] <= loaded["current_period"]["end"]
    assert db_session.query(ReminderLog).count() == 0


def test_settings_api_can_edit_reminder_card_text(
    client: TestClient, db_session: Session
) -> None:
    """催交文案可编辑，改完下一次发送即生效（不需要重启 Worker）。"""
    response = client.patch(
        "/api/settings/reminder",
        json={
            "card_title": "请交周报",
            "card_body_zh": "【提醒】{period} 的周报还没交。\n请今天补交。",
            "card_body_en": "Reminder: {period}\nPlease submit today.",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["card_title"] == "请交周报"
    assert body["card_body_zh"].startswith("【提醒】")
    assert body["card_body_en"].startswith("Reminder:")

    loaded = client.get("/api/settings/reminder").json()
    assert loaded["card_title"] == "请交周报"
    assert runtime_settings(db_session).remind_card_body_zh.startswith("【提醒】")


def test_reminder_card_text_empty_keeps_existing_value(
    client: TestClient, db_session: Session
) -> None:
    """空字符串=保持原值（与密钥同一约定），不能把已有文案静默清掉。"""
    client.patch(
        "/api/settings/reminder",
        json={"card_title": "自定义标题"},
    )
    client.patch(
        "/api/settings/reminder",
        json={"card_title": "", "card_body_zh": "   "},
    )
    loaded = client.get("/api/settings/reminder").json()
    assert loaded["card_title"] == "自定义标题"
    assert loaded["card_body_zh"]


def test_reminder_card_text_reset_restores_defaults(
    client: TestClient, db_session: Session
) -> None:
    client.patch("/api/settings/reminder", json={"card_title": "自定义标题"})
    assert client.get("/api/settings/reminder").json()["card_title"] == "自定义标题"

    reset = client.patch("/api/settings/reminder", json={"clear_card_text": True})
    assert reset.status_code == 200
    loaded = client.get("/api/settings/reminder").json()
    assert loaded["card_title"] == "周报提醒 · Weekly Report"
    assert "{period}" in loaded["card_body_zh"]


def test_custom_card_text_is_rendered_with_period_placeholder(
    client: TestClient, db_session: Session
) -> None:
    """自定义文案里的 {period} 必须被替换，不能原样发给同事。"""
    client.patch(
        "/api/settings/reminder",
        json={
            "card_title": "请交周报",
            "card_body_zh": "周期：{period}",
            "card_body_en": "Period {start} ~ {end}",
        },
    )
    preview = client.post(
        "/api/notifications/remind",
        json={"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
    ).json()

    assert preview["card"]["title"] == "请交周报"
    assert "2026-09-07 ~ 2026-09-13" in preview["card"]["description"]
    assert "{period}" not in preview["card"]["description"]
    assert "{start}" not in preview["card"]["description"]


def test_custom_card_text_is_escaped_before_html_preview(
    client: TestClient, db_session: Session
) -> None:
    """可编辑文案按纯文本渲染，不能借由卡片预览注入 HTML。"""
    client.patch(
        "/api/settings/reminder",
        json={
            "card_body_zh": '<img src=x onerror="alert(1)">',
            "card_body_en": "<script>alert(2)</script>",
        },
    )

    preview = client.post(
        "/api/notifications/remind",
        json={"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
    ).json()
    description = preview["card"]["description"]

    assert "<img" not in description
    assert "<script" not in description
    assert "&lt;img src=x onerror=\"alert(1)\"&gt;" in description
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in description


def _seed_one_pending(db_session: Session) -> Employee:
    """一个部门 + 一个未提交的人，供自动催交的投递结果用例复用。"""
    department = Department(source="wecom", external_id="d9", name="市场部")
    db_session.add(department)
    db_session.flush()
    pending = _employee(db_session, "pending9", "未提交", department.id)
    db_session.commit()
    update_runtime_values(
        db_session,
        {"remind_auto_enabled": "true", "remind_auto_weekday": "4", "remind_auto_hour": "21"},
    )
    db_session.commit()
    return pending


def test_auto_reminder_marks_partial_when_wecom_refuses_all_recipients(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求被受理但一个人都没送到 -> 记 partial，不是 failed。

    这类成因（不在应用可见范围 / 已不在通讯录）重试治不好，记成 failed 会让
    Worker 每 15 分钟重发一轮，既没进展又反复打扰。现场验收实测到的就是这个。
    """
    from app.connectors.wecom.client import WeComClient

    _seed_one_pending(db_session)

    def fake_send(self, user_ids, *args, **kwargs):
        # 企微自报成功，实际把所有人都列为不可投递
        return {"errcode": 0, "errmsg": "ok", "invaliduser": "|".join(user_ids)}

    monkeypatch.setattr(WeComClient, "send_text_card", fake_send)
    result = maybe_send_scheduled_reminders(db_session, now=FRIDAY_DUE_UTC)

    assert result["status"] == "partial"
    assert result["delivered_count"] == 0
    assert "可见范围" in (result["error"] or "")

    from app.models.reminder_run import ReminderRun

    run = db_session.query(ReminderRun).one()
    assert run.status == "partial"
    # 部分投递已被记为「本轮已执行」，不会 15 分钟后重发
    again = maybe_send_scheduled_reminders(db_session, now=FRIDAY_DUE_UTC)
    assert again["status"] == "already_sent"
    assert db_session.query(ReminderLog).count() == 1


def test_auto_reminder_marks_failed_only_when_request_rejected(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """企微根本没受理请求（网络/鉴权）才是 failed —— 那种情况重试有意义。"""
    from app.connectors import base as cb
    from app.connectors.wecom.client import WeComClient

    _seed_one_pending(db_session)

    def boom(self, *args, **kwargs):
        raise cb.ConnectorUpstreamError("企业微信请求失败：timeout")

    monkeypatch.setattr(WeComClient, "send_text_card", boom)
    result = maybe_send_scheduled_reminders(db_session, now=FRIDAY_DUE_UTC)

    assert result["status"] == "failed"
    assert "timeout" in (result["error"] or "")

    from app.models.reminder_run import ReminderRun

    assert db_session.query(ReminderRun).one().status == "failed"


def test_auto_reminder_no_targets_is_skipped_not_failed(
    db_session: Session,
) -> None:
    """全员已交/无可发对象 = skipped。记成 failed 会让正常周一看起来像故障。"""
    update_runtime_values(
        db_session,
        {"remind_auto_enabled": "true", "remind_auto_weekday": "4", "remind_auto_hour": "21"},
    )
    db_session.commit()

    result = maybe_send_scheduled_reminders(db_session, now=FRIDAY_DUE_UTC)
    assert result["status"] == "no_targets"

    from app.models.reminder_run import ReminderRun

    assert db_session.query(ReminderRun).one().status == "skipped"


def test_attribution_days_is_exposed_and_saved(
    client: TestClient, db_session: Session
) -> None:
    """周期归属天数必须在设置页可读可改：周一交的那篇属于上周。"""
    initial = client.get("/api/settings/reminder").json()
    assert initial["attribution_days"] == 1
    assert initial["attribution_label"] == "周一提交的周报归上一周期"

    saved = client.patch(
        "/api/settings/reminder", json={"attribution_days": 3}
    ).json()
    assert saved["attribution_days"] == 3
    assert saved["attribution_label"] == "周一~周三提交的周报归上一周期"

    assert runtime_settings(db_session).report_period_attribution_days == 3


def test_period_attribution_label_covers_all_values() -> None:
    """0 与最大值都要能说清楚，不能让界面显示一个看不懂的天数。"""
    from app.services.reminder_scheduler import period_attribution_label

    assert period_attribution_label(0) == "按提交当天所在周期归类（不做回溯）"
    assert period_attribution_label(1) == "周一提交的周报归上一周期"
    assert period_attribution_label(2) == "周一~周二提交的周报归上一周期"
    assert period_attribution_label(7) == "周期开始后的前 7 天都归上一周期"
    assert period_attribution_label(1, period_rule="month") == "每月前 1 天提交的报告归上一月"
    assert period_attribution_label(
        2, period_rule="custom_week", custom_week_start=2
    ) == "周三~周四提交的周报归上一周期"


def test_reject_out_of_range_attribution_days(
    client: TestClient, db_session: Session
) -> None:
    """超过一周就没有「上一周期」可归属了；负数更是无意义。"""
    assert client.patch(
        "/api/settings/reminder", json={"attribution_days": 8}
    ).status_code == 422
    assert client.patch(
        "/api/settings/reminder", json={"attribution_days": -1}
    ).status_code == 422


def test_attribution_setting_changes_actual_period_classification(
    db_session: Session,
) -> None:
    """设置页改的值必须真的影响归属 —— 否则界面就是个装饰。

    场景：周二交的一篇周报。
      - 回溯 1 天（默认）-> 算当周
      - 回溯 2 天        -> 算上一周期
    这条同时守住「运行时配置真的被同步链路读到」这一点：只改 Settings 默认值
    而没接进 runtime_settings，这里就会失败。
    """
    from datetime import datetime as _dt

    from app.services.weekly_report_sync import _upsert_weekly_report
    from app.processors.journal_parser import ParsedJournal
    from app.connectors.base import RawPayload

    tuesday = _dt(2026, 9, 8, 6, 0)  # UTC 06:00 = 北京 14:00 周二
    raw = RawPayload(
        source="wecom", source_type="weekly_report",
        external_id="late-tue", payload={},
    )

    def _upsert() -> tuple:
        parsed = ParsedJournal(
            external_id="tue-report", content="周二提交", content_raw="周二提交",
            report_type="weekly", report_time=tuesday,
        )
        report, _ = _upsert_weekly_report(db_session, None, raw, parsed)
        db_session.flush()
        return report.start_date, report.end_date

    # 默认：周二算当周
    update_runtime_values(db_session, {"report_period_attribution_days": "1"})
    db_session.commit()
    assert _upsert() == (date(2026, 9, 7), date(2026, 9, 13))

    # 回溯到 2 天：周二算上一周期
    update_runtime_values(db_session, {"report_period_attribution_days": "2"})
    db_session.commit()
    assert _upsert() == (date(2026, 8, 31), date(2026, 9, 6))

    # 0：回到按提交当天算
    update_runtime_values(db_session, {"report_period_attribution_days": "0"})
    db_session.commit()
    assert _upsert() == (date(2026, 9, 7), date(2026, 9, 13))
