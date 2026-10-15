"""管理跟进的双向沟通：落库、已读、视图剥离与企微投递结果。

零真实外发：所有投递都走假客户端（注入 client_factory），
既要验证「谁收到了」，也要验证「谁没收到、为什么」。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import deps
from app.connectors import base as cb
from app.core.time_utils import utcnow
from app.main import create_app
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import ActionNotifyStatus, ManagementActionAuthor
from app.models.management_action import ManagementAction, ManagementActionMessage
from app.models.report_item import ReportItem
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services import management_action_message_service as message_service


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


class FakeSendClient:
    """假企微客户端：记录调用，按预设的 invaliduser 返回。"""

    calls: list[dict] = []
    recalls: list[str] = []
    invalid: str = ""
    raise_error: bool = False
    recall_error: bool = False
    msgid: str = "MSGID-1"

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "FakeSendClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def close(self) -> None:
        return None

    def send_text_card(
        self, user_ids, title, description, url, btn_text="去填写"
    ) -> dict:
        FakeSendClient.calls.append(
            {
                "user_ids": list(user_ids),
                "title": title,
                "description": description,
                "url": url,
                "btn_text": btn_text,
            }
        )
        if FakeSendClient.raise_error:
            raise cb.ConnectorResponseError("企微请求失败（模拟）")
        return {
            "errcode": 0,
            "errmsg": "ok",
            "invaliduser": FakeSendClient.invalid,
            "msgid": FakeSendClient.msgid,
        }

    def recall_message(self, msgid: str) -> dict:
        FakeSendClient.recalls.append(msgid)
        if FakeSendClient.recall_error:
            raise cb.ConnectorResponseError("撤回失败（模拟）：超过 24 小时")
        return {"errcode": 0, "errmsg": "ok"}


@pytest.fixture(autouse=True)
def fake_send() -> Iterator[None]:
    FakeSendClient.calls = []
    FakeSendClient.recalls = []
    FakeSendClient.invalid = ""
    FakeSendClient.raise_error = False
    FakeSendClient.recall_error = False
    FakeSendClient.msgid = "MSGID-1"
    yield


def _seed(session: Session, *, owner_external_id: str = "owner-user") -> tuple[int, int, int]:
    """建一个可跟进的卡点事项，并指派给一名有企微 userid 的员工。"""
    department = Department(
        source="wecom", external_id="conv-dept", name="技术部", status="active"
    )
    author = Employee(
        source="wecom",
        external_id="author-user",
        name="张老板",
        department=department,
        status="active",
    )
    owner = Employee(
        source="wecom",
        external_id=owner_external_id,
        name="李负责人",
        department=department,
        status="active",
    )
    report = WeeklyReport(
        employee=author,
        external_id="conv-report",
        report_type="weekly",
        title="本周周报",
        content="等待外部验收确认",
        content_raw="等待外部验收确认",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        submitted_at=datetime(2026, 9, 12, 10, 0),
        source="wecom",
    )
    session.add_all([author, owner, report])
    session.flush()
    item = ReportItem(
        report_id=report.id,
        employee_id=author.id,
        item_type="blockers",
        item_order=0,
        title="等待外部验收",
        description="等待验收方确认时间",
        status="未解决",
        source_text="等待外部验收确认",
        risk_level="中",
    )
    task = Task(
        canonical_key="等待外部验收",
        title="等待外部验收",
        status="BLOCKED",
        first_seen_date=date(2026, 9, 7),
        last_seen_date=date(2026, 9, 13),
        duration_weeks=1,
    )
    session.add_all([item, task])
    session.flush()
    blocker = Blocker(
        task_id=task.id,
        title="等待外部验收",
        description="等待验收方确认时间",
        status="OPEN",
        risk_level="中",
        first_seen_date=date(2026, 9, 7),
        last_seen_date=date(2026, 9, 13),
        duration_weeks=1,
        occurrence_count=1,
    )
    session.add_all(
        [blocker, TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)]
    )
    session.commit()
    return blocker.id, author.id, owner.id


def _create_action(
    client: TestClient, blocker_id: int, owner_id: int, *, note: str = "内部判断：响应偏慢"
) -> dict:
    created = client.post(
        "/api/management-actions",
        json={
            "source_type": "blocker",
            "source_id": blocker_id,
            "start": "2026-09-07",
            "end": "2026-09-13",
        },
    )
    assert created.status_code == 200, created.text
    action_id = created.json()["id"]
    updated = client.patch(
        f"/api/management-actions/{action_id}",
        json={
            "owner_employee_id": owner_id,
            "next_step": "确认验收时间",
            "note": note,
            "title": "等待外部验收",
        },
    )
    assert updated.status_code == 200, updated.text
    return updated.json()


def test_admin_message_notifies_owner_and_creates_unread(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管理侧发消息：消息落库、推给负责人、负责人侧出现未读。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)

    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)
    sent = client.post(
        f"/api/management-actions/{action['id']}/messages",
        json={"body": "请今天内确认验收时间"},
    )
    assert sent.status_code == 200, sent.text
    payload = sent.json()
    assert payload["message"]["body"] == "请今天内确认验收时间"
    assert payload["message"]["author_role"] == "admin"
    assert payload["delivery"]["status"] == ActionNotifyStatus.SENT
    assert payload["delivery"]["delivered"][0]["id"] == owner_id

    # 推给了负责人本人，且卡片里不含内部备注
    assert FakeSendClient.calls[-1]["user_ids"] == ["owner-user"]
    assert "响应偏慢" not in FakeSendClient.calls[-1]["description"]
    assert "/my-actions/" in FakeSendClient.calls[-1]["url"]

    thread = client.get(f"/api/management-actions/{action['id']}/messages")
    assert thread.status_code == 200
    assert thread.json()["unread_count"] == 0  # 自己发的不算自己的未读
    assert len(thread.json()["items"]) == 1

    # 负责人侧：同一条消息显示为未读
    mine = db_session.scalars(
        select(ManagementActionMessage).where(
            ManagementActionMessage.action_id == action["id"]
        )
    ).all()
    assert mine[0].owner_read_at is None
    assert mine[0].admin_read_at is not None  # 自己那侧视为已读


def test_owner_reply_notifies_admin_and_is_not_pushed_to_self(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """负责人回复：推给参与过的企微管理员，不推给负责人自己。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    # 先由「企微登录的管理员」发一条，使他成为本会话的参与方
    first = message_service.append_message(
        db_session,
        db_session.get(ManagementAction, action["id"]),
        author_role=ManagementActionAuthor.ADMIN,
        body="请确认验收时间",
        author_employee_id=author_id,
        author_name="张老板",
    )
    db_session.commit()

    # 负责人回话（走服务层，模拟员工身份）
    reply, delivery = message_service.post_message(
        db_session,
        db_session.get(ManagementAction, action["id"]),
        author_role=ManagementActionAuthor.OWNER,
        body="已联系验收方，明天上午确认",
        author_employee_id=owner_id,
        author_name="李负责人",
    )
    db_session.commit()
    assert delivery is not None
    assert delivery.audience == "admin"
    # 只推给参与过的管理员（张老板），不含负责人本人
    assert FakeSendClient.calls[-1]["user_ids"] == ["author-user"]

    thread = message_service.thread(
        db_session, db_session.get(ManagementAction, action["id"]), view="admin"
    )
    assert [item["author_role"] for item in thread] == ["admin", "owner"]
    # 管理侧未读 = 负责人那条
    unread = message_service.unread_counts(
        db_session, [action["id"]], reader="admin"
    )
    assert unread[action["id"]] == 1


def test_mark_read_only_clears_other_side_messages(
    client: TestClient, db_session: Session
) -> None:
    """标记已读只清对方发的，不动自己发的。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    model = db_session.get(ManagementAction, action["id"])
    message_service.append_message(
        db_session, model,
        author_role=ManagementActionAuthor.ADMIN, body="a", author_name="张老板",
    )
    message_service.append_message(
        db_session, model,
        author_role=ManagementActionAuthor.OWNER, body="b", author_name="李负责人",
    )
    db_session.commit()

    assert message_service.unread_counts(db_session, [action["id"]], reader="admin")[action["id"]] == 1
    updated = message_service.mark_read(db_session, model, reader="admin")
    db_session.commit()
    assert updated == 1
    assert message_service.unread_counts(db_session, [action["id"]], reader="admin") == {}

    # 负责人那侧不受影响（他还没读管理员那条）
    assert message_service.unread_counts(db_session, [action["id"]], reader="owner")[action["id"]] == 1


def test_undelivered_recipient_is_reported_not_treated_as_sent(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """企微把收件人列进 invaliduser：状态是 undelivered，且带上处理路径。

    这是第 25 条踩过的坑：errcode=0 但人没收到，只看 errcode 会把
    「已通知」显示成成功。
    """
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)
    FakeSendClient.invalid = "owner-user"

    sent = client.post(
        f"/api/management-actions/{action['id']}/messages",
        json={"body": "请回话"},
    )
    assert sent.status_code == 200, sent.text
    delivery = sent.json()["delivery"]
    assert delivery["status"] == ActionNotifyStatus.UNDELIVERED
    assert delivery["delivered_count"] == 0
    assert "可见范围" in delivery["undelivered"][0]["reason"]

    row = db_session.scalars(select(ManagementActionMessage)).one()
    assert row.notify_status == ActionNotifyStatus.UNDELIVERED
    assert "可见范围" in (row.notify_error or "")


def test_failed_notify_keeps_message(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """投递失败不回滚消息：推送是辅助，消息才是真源。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)
    FakeSendClient.raise_error = True

    sent = client.post(
        f"/api/management-actions/{action['id']}/messages",
        json={"body": "这条必须留下"},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["delivery"]["status"] == ActionNotifyStatus.FAILED

    thread = client.get(f"/api/management-actions/{action['id']}/messages").json()
    assert [item["body"] for item in thread["items"]] == ["这条必须留下"]
    row = db_session.scalars(select(ManagementActionMessage)).one()
    assert row.notify_status == ActionNotifyStatus.FAILED
    assert "模拟" in (row.notify_error or "")


def test_cooldown_skips_second_notification_but_keeps_message(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冷却期内第二次发送：消息照常落库，但不再打扰对方。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    first = client.post(
        f"/api/management-actions/{action['id']}/messages", json={"body": "第一条"}
    )
    assert first.json()["delivery"]["status"] == ActionNotifyStatus.SENT
    second = client.post(
        f"/api/management-actions/{action['id']}/messages", json={"body": "第二条"}
    )
    assert second.json()["delivery"]["status"] == ActionNotifyStatus.SKIPPED
    assert "分钟内已通知过" in second.json()["delivery"]["skipped_reason"]
    assert len(FakeSendClient.calls) == 1  # 只真发了一次

    thread = client.get(f"/api/management-actions/{action['id']}/messages").json()
    assert [item["body"] for item in thread["items"]] == ["第一条", "第二条"]

    # force 可以越过冷却（人明确点「重新通知」）
    forced = client.post(
        f"/api/management-actions/{action['id']}/notify", json={"force": True}
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["delivery"]["status"] == ActionNotifyStatus.SENT
    assert len(FakeSendClient.calls) == 2


def test_notify_readiness_reports_each_blocking_reason(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """投递前提逐条检查：缺什么都必须说清去哪儿补。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    model = db_session.get(ManagementAction, action["id"])
    # 先让「部门已授权」这一条通过，才能隔离出后面几条各自的原因。
    department = db_session.get(Department, db_session.get(Employee, owner_id).department_id)
    department.login_enabled = True
    db_session.commit()

    from app.core.config import get_settings

    base = get_settings().model_copy(
        update={
            "wecom_login_enabled": True,
            "wecom_login_frontend_url": "https://example.com",
        }
    )
    assert message_service.notify_readiness(db_session, model, settings=base)["can_notify"] is True

    # ① 未开企微登录：**卡片仍能送到**，只是对方点不开 —— 属 warning，不阻断发送。
    #    （旧实现把它当阻断，界面显示「对方收不到通知」，与事实不符。）
    disabled = base.model_copy(update={"wecom_login_enabled": False})
    readiness = message_service.notify_readiness(db_session, model, settings=disabled)
    assert readiness["can_notify"] is True
    entry = next(i for i in readiness["reasons"] if i["code"] == "login_disabled")
    assert entry["severity"] == "warning"

    # ② 部门未授权（负责人点开通知也进不来）：同样只影响「点开」，不影响送达
    department.login_enabled = False
    db_session.commit()
    readiness = message_service.notify_readiness(db_session, model, settings=base)
    entry = next(i for i in readiness["reasons"] if i["code"] == "not_authorized")
    assert entry["severity"] == "warning"
    assert readiness["can_notify"] is True
    department.login_enabled = True
    db_session.commit()

    # ③ 完全没有通知地址：卡片里的链接无从生成 —— 这才是阻断级
    no_url = base.model_copy(
        update={"wecom_login_frontend_url": "", "remind_card_url": ""}
    )
    readiness = message_service.notify_readiness(db_session, model, settings=no_url)
    assert any(item["code"] == "no_url" for item in readiness["reasons"])
    assert readiness["can_notify"] is False

    # ④ 负责人没有企微 userid
    owner = db_session.get(Employee, owner_id)
    owner.external_id = ""
    db_session.commit()
    readiness = message_service.notify_readiness(db_session, model, settings=base)
    assert any(item["code"] == "no_userid" for item in readiness["reasons"])

    # ⑤ 没有负责人
    model.owner_employee_id = None
    db_session.commit()
    readiness = message_service.notify_readiness(db_session, model, settings=base)
    assert any(item["code"] == "no_owner" for item in readiness["reasons"])


def test_member_view_hides_internal_fields(
    client: TestClient, db_session: Session
) -> None:
    """负责人视角不得出现内部备注与来源快照。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id, note="内部判断：响应偏慢")

    detail = client.get(f"/api/management-actions/{action['id']}").json()
    assert detail["note"] == "内部判断：响应偏慢"  # 管理侧照常可见

    from app.services import management_action_view as view_service

    model = db_session.get(ManagementAction, action["id"])
    member = view_service.serialize_action(db_session, model, view="member")
    for field in ("note", "source_snapshot", "source_stale", "source_href"):
        assert field not in member, f"{field} 不应出现在负责人视角"
    assert member["title"] == "等待外部验收"
    assert member["next_step"] == "确认验收时间"


def test_readiness_is_included_after_posting(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发消息的响应里带前提检查，前端据此提示「他可能收不到」。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)
    sent = client.post(
        f"/api/management-actions/{action['id']}/messages", json={"body": "x"}
    )
    assert "readiness" in sent.json()


def test_notify_requires_existing_message(client: TestClient, db_session: Session) -> None:
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    resp = client.post(f"/api/management-actions/{action['id']}/notify", json={})
    assert resp.status_code == 400
    assert "先发一条" in resp.json()["detail"]


def test_empty_message_rejected(client: TestClient, db_session: Session) -> None:
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    resp = client.post(
        f"/api/management-actions/{action['id']}/messages", json={"body": "   "}
    )
    assert resp.status_code in {400, 422}


def test_admin_list_includes_unread_and_last_message(
    client: TestClient, db_session: Session
) -> None:
    """列表带未读数与最后一条摘要，老板不点进去也能看出「谁回话了」。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    model = db_session.get(ManagementAction, action["id"])
    message_service.append_message(
        db_session, model,
        author_role=ManagementActionAuthor.OWNER,
        body="已联系验收方",
        author_employee_id=owner_id,
        author_name="李负责人",
    )
    db_session.commit()

    listed = client.get(
        "/api/management-actions", params={"start": "2026-09-07", "end": "2026-09-13"}
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    item = body["items"][0]
    assert item["message_count"] == 1
    assert item["unread_count"] == 1
    assert item["last_message"]["excerpt"] == "已联系验收方"
    assert body["unread_total"] == 1


def test_thread_is_read_only_and_does_not_clear_unread(
    client: TestClient, db_session: Session
) -> None:
    """GET 不产生副作用：轮询不能把未读清空。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    model = db_session.get(ManagementAction, action["id"])
    message_service.append_message(
        db_session, model,
        author_role=ManagementActionAuthor.OWNER, body="回话", author_name="李负责人",
    )
    db_session.commit()

    for _ in range(3):
        thread = client.get(f"/api/management-actions/{action['id']}/messages").json()
        assert thread["unread_count"] == 1
    row = db_session.scalars(
        select(ManagementActionMessage).where(
            ManagementActionMessage.author_role == ManagementActionAuthor.OWNER
        )
    ).one()
    assert row.admin_read_at is None


def test_unread_reply_surfaces_in_notification_feed(
    client: TestClient, db_session: Session
) -> None:
    """负责人的回复要出现在消息中心，否则老板不会知道有人在等他。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    model = db_session.get(ManagementAction, action["id"])
    message_service.append_message(
        db_session, model,
        author_role=ManagementActionAuthor.OWNER,
        body="已联系验收方，明天上午确认",
        author_employee_id=owner_id,
        author_name="李负责人",
    )
    db_session.commit()

    feed = client.get("/api/notifications/feed").json()
    card = next((item for item in feed["items"] if item["kind"] == "action_reply"), None)
    assert card is not None, [item["kind"] for item in feed["items"]]
    assert "1 件跟进事项" in card["title"]
    assert card["href"] == "/blockers?tab=actions"
    assert card["meta"]["unread"] == 1

    # 读掉之后从消息中心消失（读状态是真源，不是前端本地标记）
    message_service.mark_read(db_session, model, reader="admin")
    db_session.commit()
    feed = client.get("/api/notifications/feed").json()
    assert not [item for item in feed["items"] if item["kind"] == "action_reply"]


def test_closed_action_reply_does_not_keep_the_bell_lit(
    client: TestClient, db_session: Session
) -> None:
    """已解决事项的未读不再提示：那些事项不需要老板再回话。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    model = db_session.get(ManagementAction, action["id"])
    message_service.append_message(
        db_session, model,
        author_role=ManagementActionAuthor.OWNER, body="已完成", author_name="李负责人",
    )
    model.status = "RESOLVED"
    db_session.commit()

    assert message_service.unread_action_replies(db_session) == []
    feed = client.get("/api/notifications/feed").json()
    assert not [item for item in feed["items"] if item["kind"] == "action_reply"]


def test_reply_recipients_setting_round_trip(
    client: TestClient, db_session: Session
) -> None:
    """通知接收人可在设置页配置：空值保持原值，clear 才清空。"""
    initial = client.get("/api/settings/reminder").json()
    assert initial["action_notify_admin_userids"] == ""

    saved = client.patch(
        "/api/settings/reminder",
        json={"action_notify_admin_userids": "author-user, other-user"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["action_notify_admin_userids"] == "author-user, other-user"

    # 空字符串 = 保持原值（与密钥同一约定）
    kept = client.patch("/api/settings/reminder", json={"action_notify_admin_userids": ""})
    assert kept.json()["action_notify_admin_userids"] == "author-user, other-user"

    cleared = client.patch(
        "/api/settings/reminder", json={"clear_action_notify_userids": True}
    )
    assert cleared.json()["action_notify_admin_userids"] == ""


def test_configured_recipient_receives_owner_reply(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """访问密码登录的管理员没有企微身份，靠设置页配置的 userid 收回复通知。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    client.patch(
        "/api/settings/reminder", json={"action_notify_admin_userids": "author-user"}
    )
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    model = db_session.get(ManagementAction, action["id"])
    _msg, delivery = message_service.post_message(
        db_session, model,
        author_role=ManagementActionAuthor.OWNER,
        body="有进展",
        author_employee_id=owner_id,
        author_name="李负责人",
    )
    db_session.commit()
    assert delivery is not None
    assert delivery.status == ActionNotifyStatus.SENT
    assert FakeSendClient.calls[-1]["user_ids"] == ["author-user"]


def test_configured_recipient_missing_from_directory_is_reported(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配错 userid 要说出来，不能静默变成「没有可通知的管理员」。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    client.patch(
        "/api/settings/reminder", json={"action_notify_admin_userids": "not-exist-user"}
    )
    model = db_session.get(ManagementAction, action["id"])
    targets, skipped = message_service.resolve_notify_targets(
        db_session, model, audience="admin"
    )
    assert targets == []
    assert any("通讯录里找不到" in item["reason"] for item in skipped)


def test_msgid_is_saved_and_recall_uses_it(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撤回的唯一凭据是发送时返回的 msgid，必须落库。

    没有它就无法撤回（企微事后再也查不到这个 id），所以这条守护的是
    「发送成功后 msgid 确实被保存」这一环。
    """
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    sent = client.post(
        f"/api/management-actions/{action['id']}/messages", json={"body": "请回话"}
    )
    assert sent.status_code == 200, sent.text
    row = db_session.scalars(select(ManagementActionMessage)).one()
    assert row.notify_msgids == ["MSGID-1"], row.notify_msgids
    assert sent.json()["message"]["notify"]["can_recall"] is True

    recalled = client.post(
        f"/api/management-actions/{action['id']}/messages/{row.id}/recall", json={}
    )
    assert recalled.status_code == 200, recalled.text
    body = recalled.json()
    assert body["recall"]["recalled"] is True
    assert FakeSendClient.recalls == ["MSGID-1"]
    assert body["message"]["recalled"] is True

    db_session.refresh(row)
    assert row.recalled_at is not None
    assert row.recalled_by_role == "admin"


def test_recall_is_terminal_and_cannot_be_repushed(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已撤回的消息不能再推送 —— 否则收回去的卡片会重新出现在对方窗口里。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    client.post(f"/api/management-actions/{action['id']}/messages", json={"body": "一"})
    row = db_session.scalars(select(ManagementActionMessage)).one()
    client.post(f"/api/management-actions/{action['id']}/messages/{row.id}/recall", json={})

    before = len(FakeSendClient.calls)
    again = client.post(
        f"/api/management-actions/{action['id']}/notify", json={"force": True}
    )
    assert again.status_code == 200
    assert again.json()["delivery"]["status"] == ActionNotifyStatus.SKIPPED
    assert "已撤回" in (again.json()["delivery"]["skipped_reason"] or "")
    assert len(FakeSendClient.calls) == before

    # 重复撤回是幂等的，不报错、也不再调企微
    recalls_before = len(FakeSendClient.recalls)
    twice = client.post(
        f"/api/management-actions/{action['id']}/messages/{row.id}/recall", json={}
    )
    assert twice.status_code == 200
    assert len(FakeSendClient.recalls) == recalls_before
    assert "此前已撤回" in (twice.json()["recall"]["reason"] or "")


def test_recall_reports_beyond_24h_window_without_calling_wecom(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 24 小时：直接说明原因，不做无意义的网络往返。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)
    client.post(f"/api/management-actions/{action['id']}/messages", json={"body": "一"})

    row = db_session.scalars(select(ManagementActionMessage)).one()
    row.notified_at = utcnow() - timedelta(hours=30)
    db_session.commit()

    resp = client.post(
        f"/api/management-actions/{action['id']}/messages/{row.id}/recall", json={}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["recall"]["recalled"] is False
    assert "24 小时" in body["recall"]["reason"]
    assert FakeSendClient.recalls == []  # 没白调一次企微


def test_recall_failure_is_not_reported_as_success(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """企微撤回失败时不能标记为已撤回：那会让用户以为卡片已经收回去了。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)
    client.post(f"/api/management-actions/{action['id']}/messages", json={"body": "一"})
    row = db_session.scalars(select(ManagementActionMessage)).one()

    FakeSendClient.recall_error = True
    resp = client.post(
        f"/api/management-actions/{action['id']}/messages/{row.id}/recall", json={}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["recall"]["recalled"] is False
    assert "模拟" in body["recall"]["reason"]
    assert body["message"]["recalled"] is False

    db_session.refresh(row)
    assert row.recalled_at is None
    assert "模拟" in (row.recall_error or "")


def test_member_cannot_recall(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """员工没有撤回语义：收回自己说过的话等于篡改记录。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)
    client.post(f"/api/management-actions/{action['id']}/messages", json={"body": "一"})
    row = db_session.scalars(select(ManagementActionMessage)).one()

    from app.api.deps import CurrentSession, get_current_session
    from app.main import create_app
    from app.models.enums import LoginRole

    app = create_app()

    def _override_db():
        yield db_session

    # 伪造一个只读成员会话：员工只能看到自己负责的事项，
    # 但即便能看到，撤回也必须被拒。
    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[get_current_session] = lambda: CurrentSession(
        is_admin=False, employee_id=owner_id, name="李负责人"
    )
    try:
        member_client = TestClient(app)
        resp = member_client.post(
            f"/api/management-actions/{action['id']}/messages/{row.id}/recall", json={}
        )
        assert resp.status_code == 403
        assert "管理员" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_loopback_url_is_reported_as_warning_not_blocking(
    client: TestClient, db_session: Session
) -> None:
    """通知地址是 127.0.0.1 时必须报出来，但**不能**阻断发送。

    这是真实验收踩到的坑：卡片确实送到了 Example User 手机，但他点开是 127.0.0.1，
    永远打不开。旧实现把「未开登录」也算成阻断，界面显示「对方收不到通知」，
    与实际（收到了、只是点不开）不符。
    """
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    department = db_session.get(Department, db_session.get(Employee, owner_id).department_id)
    department.login_enabled = True
    db_session.commit()
    model = db_session.get(ManagementAction, action["id"])

    from app.core.config import get_settings

    local = get_settings().model_copy(
        update={
            "wecom_login_enabled": True,
            # 开发默认值就是本机地址
            "wecom_login_frontend_url": "",
            "remind_card_url": "http://127.0.0.1:3000",
        }
    )
    readiness = message_service.notify_readiness(db_session, model, settings=local)
    codes = {item["code"] for item in readiness["reasons"]}
    assert "unreachable_url" in codes
    # 仍然可以发送：卡片能送到
    assert readiness["can_notify"] is True
    severity = {
        item["code"]: item.get("severity") for item in readiness["reasons"]
    }
    assert severity["unreachable_url"] == "warning"

    # 换成公网地址后这条警告消失
    public = local.model_copy(update={"wecom_login_frontend_url": "https://reports.example.com"})
    readiness = message_service.notify_readiness(db_session, model, settings=public)
    assert "unreachable_url" not in {item["code"] for item in readiness["reasons"]}


def test_list_does_not_scale_queries_with_action_count(
    client: TestClient, db_session: Session
) -> None:
    """列表的消息数据必须批量取：查询数不随事项数线性增长。

    旧实现每个事项查两次消息表（条数 + 最后一条），N 个事项就是 2N 次。
    """
    from sqlalchemy import event

    from app.services import management_action_view as view_service

    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    model = db_session.get(ManagementAction, action["id"])
    message_service.append_message(
        db_session, model, author_role=ManagementActionAuthor.OWNER,
        body="回话", author_name="李负责人",
    )
    db_session.commit()

    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*args, **kwargs):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        one = db_session.get(ManagementAction, action["id"])
        counter["n"] = 0
        view_service.serialize_actions(db_session, [one], view="admin")
        base = counter["n"]

        many = []
        for i in range(4):
            extra = ManagementAction(
                source_type="insight", source_key=f"scale:{i}",
                title=f"事项 {i}", priority="medium", status="OPEN",
            )
            db_session.add(extra)
            db_session.flush()
            db_session.add(
                ManagementActionMessage(
                    action_id=extra.id, author_role="admin",
                    author_name="管理员", body=f"消息 {i}",
                )
            )
            many.append(extra)
        db_session.flush()

        counter["n"] = 0
        view_service.serialize_actions(db_session, [one] + many, view="admin")
        with_more = counter["n"]
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    # 5 个事项与 1 个事项的消息查询同为 2 次（条数 + 最后一条），不随条数增长。
    # 上限给得宽裕：只捕捉「重新变回按条查询」这种回归，不锁死具体实现。
    assert with_more <= base + 12, (
        f"1 个事项 {base} 条 → 5 个事项 {with_more} 条，疑似 N+1"
    )


def test_recalled_body_is_hidden_from_owner_but_kept_for_admin(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撤回后员工不得再读到正文 —— 否则撤回只收回了卡片、内容照样泄露。

    管理侧要保留原文：审计需要知道「当时到底发了什么、为什么撤回」。
    """
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    client.post(
        f"/api/management-actions/{action['id']}/messages",
        json={"body": "这句话不该被他看到"},
    )
    row = db_session.scalars(select(ManagementActionMessage)).one()
    client.post(
        f"/api/management-actions/{action['id']}/messages/{row.id}/recall", json={}
    )
    db_session.refresh(row)

    member = message_service.message_view(row, "member")
    assert member["recalled"] is True
    assert "不该被他看到" not in member["body"]
    assert "撤回" in member["body"]

    admin = message_service.message_view(row, "admin")
    assert admin["recalled"] is True
    assert admin["body"] == "这句话不该被他看到"


def test_status_change_notifies_owner_and_leaves_thread_record(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改状态要通知负责人，并在会话里留下一条记录。

    缺少通知时，负责人被告知「这件事要你跟进」，之后却只能靠自己翻页面
    才知道它被改成了「已解决」—— 等于把他排除在这件事之外。
    """
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    resp = client.post(
        f"/api/management-actions/{action['id']}/status",
        json={"status": "IN_PROGRESS", "note": "已安排本周跟进"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "IN_PROGRESS"
    notice = body["status_notice"]
    assert notice["status"] == ActionNotifyStatus.SENT
    assert notice["audience"] == "owner"
    assert FakeSendClient.calls[-1]["user_ids"] == ["owner-user"]

    # 会话里留下一条可回看的记录，双方都能看到状态被改过
    thread = client.get(f"/api/management-actions/{action['id']}/messages").json()
    bodies = [item["body"] for item in thread["items"]]
    assert any("处理中" in text for text in bodies), bodies
    assert any("已安排本周跟进" in text for text in bodies)


def test_status_notice_force_bypasses_cooldown(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冷却不能挡住办结通知。

    负责人 10 分钟前刚收到一条消息，此时老板确认办结 —— 若被冷却拦下，
    他会一直以为这件事还在推进。
    """
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    client.post(f"/api/management-actions/{action['id']}/messages", json={"body": "请跟进"})
    sent_before = len(FakeSendClient.calls)

    resolved = client.post(
        f"/api/management-actions/{action['id']}/status", json={"status": "RESOLVED"}
    )
    assert resolved.status_code == 200
    assert resolved.json()["status_notice"]["status"] == ActionNotifyStatus.SENT
    assert len(FakeSendClient.calls) == sent_before + 1


def test_status_change_can_skip_notification(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """可以显式关掉通知：只是修正填错的备注时不该再打扰对方。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    before = len(FakeSendClient.calls)
    resp = client.post(
        f"/api/management-actions/{action['id']}/status",
        json={"status": "IN_PROGRESS", "notify": False},
    )
    assert resp.status_code == 200
    assert resp.json()["status_notice"] is None
    assert len(FakeSendClient.calls) == before
    assert client.get(f"/api/management-actions/{action['id']}/messages").json()["items"] == []


def test_status_change_to_same_value_does_not_notify(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """状态没变就不该推送：重复点同一个按钮不该打扰对方。"""
    blocker_id, author_id, owner_id = _seed(db_session)
    action = _create_action(client, blocker_id, owner_id)
    monkeypatch.setattr(message_service, "WeComClient", FakeSendClient)

    before = len(FakeSendClient.calls)
    resp = client.post(
        f"/api/management-actions/{action['id']}/status", json={"status": "OPEN"}
    )
    assert resp.status_code == 200
    assert resp.json()["status_notice"] is None
    assert len(FakeSendClient.calls) == before
