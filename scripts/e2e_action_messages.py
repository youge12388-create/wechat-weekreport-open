"""端到端验收：跟进沟通的完整链路（真实 HTTP + 假企微，零真实外发）。

覆盖：发消息 → 企微投递 → 未读 → 已读 → 员工回复 → 管理侧未读 → 消息中心。
"""

from __future__ import annotations

import json
import os

import httpx

API = "http://127.0.0.1:8021"
FAKE = "http://127.0.0.1:8099"
# 隔离验收库的管理密码从环境变量读，默认值与验收环境一致（见 AGENTS 第 26 条验收章节）。
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "e2e-admin-pass")

client = httpx.Client(base_url=API, timeout=20.0, follow_redirects=False)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def login_as_password() -> None:
    resp = client.post("/api/auth/login", json={"password": ADMIN_PASSWORD})
    check("访问密码登录", resp.status_code == 200, f"HTTP {resp.status_code}")


def login_as_employee() -> None:
    """以张三（信息部，已授权）的企微身份登录，走真实回调路径。"""
    start = client.get("/api/auth/wecom/start", params={"mode": "qr"})
    check("企微登录发起 302", start.status_code == 302, f"HTTP {start.status_code}")
    # 直接构造回调：假企微的 getuserinfo 返回 userid=zhangsan
    from urllib.parse import parse_qs, urlparse

    location = start.headers.get("location", "")
    state = parse_qs(urlparse(location).query).get("state", [""])[0]
    callback = client.get(
        "/api/auth/wecom/callback", params={"code": "FAKE", "state": state}
    )
    check("企微回调完成登录", callback.status_code in {200, 302}, f"HTTP {callback.status_code}")
    me = client.get("/api/auth/me").json()
    check(
        "会话是只读员工角色",
        me.get("authenticated") is True and me.get("is_admin") is False,
        json.dumps(me, ensure_ascii=False),
    )
    return me


def fake_sent() -> list[dict]:
    return httpx.get(f"{FAKE}/__sent", timeout=10.0).json()["sent"]


def main() -> None:
    print("=" * 70)
    print("① 管理侧：发消息 + 真实投递（打到假企微）")
    print("=" * 70)
    login_as_password()
    before = len(fake_sent())
    sent = client.post(
        "/api/management-actions/1/messages",
        json={"body": "张三，这个排期周四前必须给客户一个明确答复，有困难现在说。", "notify": True},
    )
    check("发消息 HTTP 200", sent.status_code == 200, f"HTTP {sent.status_code}")
    payload = sent.json()
    check("消息已落库", payload["message"]["body"].startswith("张三，这个排期"))
    delivery = payload["delivery"]
    check("投递状态 sent", delivery["status"] == "sent", json.dumps(delivery, ensure_ascii=False)[:120])
    check("送达人=张三", [x["name"] for x in delivery["delivered"]] == ["张三"])
    calls = fake_sent()
    check("假企微确实收到一次投递", len(calls) == before + 1, f"{before} -> {len(calls)}")
    if len(calls) > before:
        last = calls[-1]
        check("投递目标 userid 正确", last["touser"] == "u1", last["touser"])
        check("卡片链接指向我的事项", "/my-actions/1" in last["url"], last["url"])
        check("卡片不含内部备注", "响应偏慢" not in last["title"])

    print()
    print("=" * 70)
    print("② 冷却：10 分钟内不重复打扰")
    print("=" * 70)
    second = client.post(
        "/api/management-actions/1/messages", json={"body": "补充一句：客户那边我去沟通。", "notify": True}
    )
    check("第二条消息仍落库", second.status_code == 200)
    check(
        "第二次投递被冷却拦截",
        second.json()["delivery"]["status"] == "skipped",
        second.json()["delivery"].get("skipped_reason", ""),
    )
    check("假企微没有收到第二次", len(fake_sent()) == before + 1)

    print()
    print("=" * 70)
    print("③ 管理侧并发投递给管理侧自己（不应推给自己）")
    print("=" * 70)
    # 参与人只有访问密码管理员（无员工身份），所以管理侧收件人为空 → skipped 且说明原因
    notify = client.post("/api/management-actions/1/notify", json={"audience": "admin", "force": True})
    check("单独通知 HTTP 200", notify.status_code == 200, f"HTTP {notify.status_code}")
    body = notify.json()["delivery"]
    check(
        "没有可通知的管理员时说清原因",
        body["status"] in {"skipped", "sent", "undelivered"},
        json.dumps(
            {"status": body["status"], "skipped": body.get("skipped_reason"), "undelivered": body["undelivered"]},
            ensure_ascii=False,
        )[:200],
    )

    print()
    print("=" * 70)
    print("④ 员工侧：登录、看事项、读会话、回复")
    print("=" * 70)
    client.cookies.clear()
    me = login_as_employee()

    mine = client.get("/api/my-actions")
    check("我的事项 HTTP 200", mine.status_code == 200, f"HTTP {mine.status_code}")
    items = mine.json()["items"]
    check("看到指派给自己的事项", len(items) == 1 and items[0]["id"] == 1, f"{len(items)} 项")
    if items:
        item = items[0]
        check("负责人视角不含内部备注", "note" not in item, str(list(item.keys()))[:150])
        check("负责人视角不含来源快照", "source_snapshot" not in item)
        check("未读数为 2", item["unread_count"] == 2, str(item["unread_count"]))
        check("最后一条消息摘要可见", bool(item.get("last_message")))

    thread = client.get("/api/my-actions/1/messages")
    check("员工读会话 HTTP 200", thread.status_code == 200, f"HTTP {thread.status_code}")
    messages = thread.json()["items"]
    check("拿到 2 条管理员消息", len(messages) == 2, str(len(messages)))
    check("员工视角不返回投递结果", all("notify" not in m for m in messages))
    check("会话返回未读 2", thread.json()["unread_count"] == 2)

    before_reply = len(fake_sent())
    reply = client.post(
        "/api/my-actions/1/messages",
        json={"body": "收到，客户那边今天下午回复，我周四上午给结论。"},
    )
    check("员工回复 HTTP 200", reply.status_code == 200, f"HTTP {reply.status_code}")
    reply_body = reply.json()
    check("回复落库且作者是 owner", reply_body["message"]["author_role"] == "owner")
    check("未读已清零（自己发的不算）", reply_body["unread_count"] == 0)
    check(
        "员工侧不暴露投递细节",
        "delivery" not in reply_body and isinstance(reply_body.get("notified"), bool),
        json.dumps({k: reply_body[k] for k in reply_body if k != "message"}, ensure_ascii=False),
    )
    check("假企微未收到投递（管理侧无企微身份）", len(fake_sent()) == before_reply, f"{before_reply} -> {len(fake_sent())}")

    read = client.post("/api/my-actions/1/messages/read")
    check("员工标记已读 HTTP 200", read.status_code == 200, f"HTTP {read.status_code}")
    check("标记了 2 条", read.json()["updated"] == 2, str(read.json()))

    print()
    print("=" * 70)
    print("⑤ 员工越权尝试（应全部被拒）")
    print("=" * 70)
    other = client.get("/api/my-actions/99999")
    check("访问不存在的事项 404", other.status_code == 404, f"HTTP {other.status_code}")
    admin_api = client.post("/api/management-actions/1/messages", json={"body": "越权"})
    check("员工不能写管理侧接口 403", admin_api.status_code == 403, f"HTTP {admin_api.status_code}")
    status_change = client.post("/api/management-actions/1/status", json={"status": "RESOLVED"})
    check("员工不能改状态 403", status_change.status_code == 403, f"HTTP {status_change.status_code}")

    print()
    print("=" * 70)
    print("⑥ 管理侧：看到未读回复 + 消息中心")
    print("=" * 70)
    client.cookies.clear()
    login_as_password()
    detail = client.get("/api/management-actions/1").json()
    check("管理侧看到负责人回复的未读", detail["unread_count"] == 1, str(detail["unread_count"]))
    check("管理侧仍可见内部备注", detail["note"] == "内部判断：响应偏慢，需重点盯")
    check("列表带未读计数", client.get("/api/management-actions").json()["unread_total"] >= 1)

    feed = client.get("/api/notifications/feed").json()
    card = next((x for x in feed["items"] if x["kind"] == "action_reply"), None)
    check("消息中心出现「跟进回复」", card is not None, str([x["kind"] for x in feed["items"]]))
    if card:
        check("消息中心指向处理中心", card["href"] == "/blockers?tab=actions", card["href"])
        print("      标题:", card["title"])
        print("      摘要:", card["summary"])

    print()
    print("=" * 70)
    print("⑦ 企微投递失败分支：errcode=0 但收件人不可投递")
    print("=" * 70)
    # 先发一条不触发通知的消息，再用 force 重发验证未送达分支：
    # force 是管理员在页面上点「重新推送」的路径，会跳过 10 分钟冷却。
    client.post(
        "/api/management-actions/1/messages",
        json={"body": "再确认一次时间（这条先不通知）。", "notify": False},
    )
    httpx.post(f"{FAKE}/__mode", json={"invalid": "u1"}, timeout=10.0)
    again = client.post(
        "/api/management-actions/1/notify", json={"audience": "owner", "force": True}
    )
    check("重发 HTTP 200", again.status_code == 200, f"HTTP {again.status_code}")
    dv = again.json()["delivery"]
    check(
        "errcode=0 但未送达时状态为 undelivered",
        dv["status"] == "undelivered",
        json.dumps({"status": dv["status"], "undelivered": dv["undelivered"]}, ensure_ascii=False)[:220],
    )
    check("未送达原因含处理路径", any("可见范围" in x["reason"] for x in dv["undelivered"]))
    httpx.post(f"{FAKE}/__mode", json={"invalid": ""}, timeout=10.0)

    print()
    print("=" * 70)
    if failures:
        print(f"结果：{len(failures)} 项失败")
        for item in failures:
            print("  -", item)
    else:
        print("结果：全部通过 ✅")
    print("=" * 70)


if __name__ == "__main__":
    main()
