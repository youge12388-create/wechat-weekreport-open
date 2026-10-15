"""P2-7 催交审计 + 冷却去重；P2-10 疑似离职（MISSING）标记。

发消息会打扰真人、撤回不了，所以：
- 默认 dry_run 只预览，不写日志、不真发
- 真发无论成败都落 ReminderLog，供审计
- 同一员工同一周期冷却期内不重复发
- 接口未返回的员工绝不自动标离职，只有「在职 + 超阈值未出现」才标 MISSING
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.connectors import base as cb
from app.connectors.wecom.client import WeComClient
from app.core.config import get_settings
from app.core.time_utils import utcnow
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.main import create_app
from app.models.employee import Employee
from app.models.enums import DataSource, EmployeeStatus, ReminderStatus
from app.models.reminder_log import ReminderLog
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
        _user("u3", "王五", 3, position="顾问"),  # 未提交
        _user("u4", "赵六", 3, position="顾问", status=5),  # 离职，不进基数
    ]
    reports = {
        "j1": _journal_info(report_time=_ts(2026, 9, 7), userid="u1",
                            apply_data=text_apply_data("本周开发 A 功能")),
        "j2": _journal_info(report_time=_ts(2026, 9, 8), userid="u2",
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


def test_remind_dry_run_writes_no_log(
    client: TestClient, seeded_session: Session
) -> None:
    body = client.post("/api/notifications/remind", json={**WEEK}).json()
    assert body["dry_run"] is True
    assert body["sent"] is False
    assert seeded_session.query(ReminderLog).count() == 0


def test_remind_targets_returns_complete_dry_run_preview(
    client: TestClient, seeded_session: Session
) -> None:
    body = client.get("/api/notifications/remind/targets", params=WEEK).json()

    assert body["dry_run"] is True
    assert body["sent"] is False
    assert body["error"] is None
    # 张三在周一（09-07）交的是**上一周**（08-31~09-06）的周报，
    # 因此本周他也还没交 —— 这正是「周一交的被算成当周、周五催交漏人」的修复点。
    assert body["target_count"] == 2
    assert [item["name"] for item in body["targets"]] == ["张三", "王五"]
    assert body["card"]["title"] == "周报提醒 · Weekly Report"
    assert body["card"]["btn_text"] == "去填写"
    # 卡片逐人投递：正文里不能出现其他人的姓名（等于把催交名单群发）
    assert "张三" not in body["card"]["description"]
    assert "王五" not in body["card"]["description"]
    assert seeded_session.query(ReminderLog).count() == 0


def test_remind_card_is_bilingual_and_interpolates_period(
    client: TestClient, seeded_session: Session
) -> None:
    body = client.get("/api/notifications/remind/targets", params=WEEK).json()
    description = body["card"]["description"]

    # 有英文同事（库内约 22% 正文几乎无汉字），只发中文等于没提醒
    assert "has not been submitted" in description
    assert "没提交" in description
    assert "2026-09-07 ~ 2026-09-13" in description
    # 未替换的占位符意味着模板渲染漏了；高亮行由最后一行生成
    assert "{period}" not in description
    assert '<div class="highlight">' in description


def test_remind_send_writes_sent_log(
    client: TestClient, seeded_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_send_text_card(self, user_ids, title, description, url, btn_text="去填写"):
        captured["user_ids"] = user_ids
        return {"errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr(WeComClient, "send_text_card", fake_send_text_card)

    body = client.post(
        "/api/notifications/remind", json={**WEEK, "dry_run": False}
    ).json()
    assert body["sent"] is True
    assert captured["user_ids"] == ["u1", "u3"]  # 周一交上周的人 + 从未提交的人
    assert body["result"]["delivered_count"] == 2

    logs = seeded_session.query(ReminderLog).all()
    assert len(logs) == 2
    assert all(log.status == ReminderStatus.SENT for log in logs)
    assert {log.employee_id for log in logs} == {item["id"] for item in body["targets"]}
    assert all(log.period_start.isoformat() == WEEK["start"] for log in logs)
    assert all(log.period_end.isoformat() == WEEK["end"] for log in logs)


def test_remind_reports_undelivered_recipients_instead_of_claiming_success(
    client: TestClient, seeded_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """errcode=0 只代表请求被受理：有人不在应用可见范围时必须如实报出来。

    旧实现只读 errcode，于是「已提醒 20 人」和「实际只有 1 人收到」在页面上
    长得一模一样，事后也没有任何记录能查出是谁没收到。
    """

    def fake_send_text_card(self, user_ids, title, description, url, btn_text="去填写"):
        # 王五（u3）不在自建应用可见范围，企微照样返回 errcode=0，只把它列进 invaliduser
        return {"errcode": 0, "errmsg": "ok", "invaliduser": "u3"}

    monkeypatch.setattr(WeComClient, "send_text_card", fake_send_text_card)

    body = client.post(
        "/api/notifications/remind", json={**WEEK, "dry_run": False}
    ).json()

    assert body["sent"] is True
    assert body["result"]["delivered_count"] == 1
    assert [item["name"] for item in body["result"]["undelivered"]] == ["王五"]
    assert "王五" in body["error"]
    assert any(item["name"] == "王五" for item in body["skipped"])

    logs = {log.employee_id: log for log in seeded_session.query(ReminderLog).all()}
    delivered_id = next(
        item["id"] for item in body["targets"] if item["name"] == "张三"
    )
    undelivered_id = next(
        item["id"] for item in body["targets"] if item["name"] == "王五"
    )
    assert logs[delivered_id].status == ReminderStatus.SENT
    assert logs[undelivered_id].status == ReminderStatus.FAILED
    # 未送达的人不能进冷却计时：否则 24 小时内就连重发都排不进去
    assert "可见范围" in (logs[undelivered_id].detail or "")


def test_remind_cooldown_skips_recent(
    client: TestClient, seeded_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_send_text_card(self, *args, **kwargs):
        return {"errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr(WeComClient, "send_text_card", fake_send_text_card)

    r1 = client.post(
        "/api/notifications/remind", json={**WEEK, "dry_run": False}
    ).json()
    assert r1["sent"] is True

    # 立即再发：两人都在冷却期内，应被跳过，且不再写新日志
    r2 = client.post(
        "/api/notifications/remind", json={**WEEK, "dry_run": False}
    ).json()
    assert r2["target_count"] == 0
    assert r2["sent"] is False
    assert any("冷却期内已提醒" in s["reason"] for s in r2["skipped"])
    # 日志仅第一次的 2 条 SENT，第二次未投递所以不写
    assert seeded_session.query(ReminderLog).count() == 2


def test_remind_failure_writes_failed_log(
    client: TestClient, seeded_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self, *args, **kwargs):
        raise cb.ConnectorError("企微接口异常")

    monkeypatch.setattr(WeComClient, "send_text_card", boom)

    body = client.post(
        "/api/notifications/remind", json={**WEEK, "dry_run": False}
    ).json()
    assert body["sent"] is False
    assert body["error"] is not None

    logs = seeded_session.query(ReminderLog).all()
    assert len(logs) == 2  # 两个目标各落一条失败记录，不会只记一次「整批失败」
    assert all(log.status == ReminderStatus.FAILED for log in logs)
    assert all("企微接口异常" in (log.detail or "") for log in logs)


def test_missing_marks_stale_absent_active(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在职 + 本次未返回 + 超过阈值天数 = 疑似离职（MISSING）。"""
    old = utcnow() - timedelta(days=400)
    fresh = utcnow() - timedelta(days=1)
    db_session.add(
        Employee(source=DataSource.WECOM, external_id="u1", name="在编",
                 status=EmployeeStatus.ACTIVE, last_seen_at=fresh)
    )
    db_session.add(
        Employee(source=DataSource.WECOM, external_id="u2", name="消失超期",
                 status=EmployeeStatus.ACTIVE, last_seen_at=old)
    )
    db_session.add(
        Employee(source=DataSource.WECOM, external_id="u3", name="已离职",
                 status=EmployeeStatus.RESIGNED, last_seen_at=old)
    )
    db_session.commit()

    monkeypatch.setattr(get_settings(), "employee_missing_days", 180)
    # 本次同步只返回 u1（u2/u3 不在 seen 集合里）
    sync_employees(db_session, FakeWeComClient(employees=[_user("u1", "在编", 1)]))

    rows = {e.external_id: e.status for e in db_session.query(Employee).all()}
    assert rows["u1"] == EmployeeStatus.ACTIVE    # 在 seen 里，不动
    assert rows["u2"] == EmployeeStatus.MISSING   # 消失且超阈值 → 标 MISSING
    assert rows["u3"] == EmployeeStatus.RESIGNED   # 已离职，不动


def test_missing_empty_batch_no_false_positive(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空批次（接口抽风）绝不能标任何人，否则会误判全员离职。"""
    old = utcnow() - timedelta(days=400)
    db_session.add(
        Employee(source=DataSource.WECOM, external_id="u9", name="x",
                 status=EmployeeStatus.ACTIVE, last_seen_at=old)
    )
    db_session.commit()

    monkeypatch.setattr(get_settings(), "employee_missing_days", 1)
    sync_employees(db_session, FakeWeComClient(employees=[]))

    emp = db_session.query(Employee).filter_by(external_id="u9").one()
    assert emp.status == EmployeeStatus.ACTIVE
