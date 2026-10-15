"""Connector 行为测试：分段、分页、错误码映射、token 刷新。

用 httpx.MockTransport 打桩，不打网络也不 mock 掉自己的逻辑，
因此能真正验证「请求体长什么样」。
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.connectors import base as cb
from app.connectors.wecom.client import RATE_LIMIT_BACKOFF_SECONDS, WeComClient


def _client(handler, monkeypatch: pytest.MonkeyPatch) -> WeComClient:
    monkeypatch.setattr(
        "app.connectors.wecom.client.JOURNAL_LIST_MIN_INTERVAL", 0.0
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return WeComClient(http_client=http)


def _ok(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"errcode": 0, "errmsg": "ok", **data})


def test_list_segments_never_cross_month_or_year(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        bodies.append(json.loads(request.content))
        return _ok({"journaluuid_list": [], "next_cursor": 0, "endflag": 1})

    client = _client(handler, monkeypatch)
    list(
        client.iter_report_ids(
            datetime(2025, 11, 15), datetime(2026, 2, 10)
        )
    )

    assert len(bodies) >= 4
    for body in bodies:
        # 一律按 UTC 解释：用 datetime.fromtimestamp()（本机时区）会在
        # 非 UTC 服务器上把预期值一起算错，从而掩盖真实偏差。
        start = datetime.fromtimestamp(body["starttime"], tz=timezone.utc)
        end = datetime.fromtimestamp(body["endtime"], tz=timezone.utc)
        # 不跨年
        assert start.year == end.year, f"请求跨年：{start} ~ {end}"
        # 不跨月
        assert (start.year, start.month) == (end.year, end.month)
        # 接口限制：间隔不超过一个月
        assert (end - start).days < 32
        assert body["limit"] <= 100
    # 覆盖完整区间，且必须是**绝对时间戳**（不受运行机器时区影响）
    assert bodies[0]["starttime"] == _utc_epoch(datetime(2025, 11, 15))


def _utc_epoch(value: datetime) -> int:
    """UTC naive -> 绝对时间戳，作为独立于实现的正交预期值。"""
    return calendar.timegm(value.utctimetuple())


def test_time_window_is_utc_absolute_regardless_of_host_tz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """时间窗必须按 UTC 换算，不能按本机时区。

    这是真实缺陷的回归守护：旧实现用 int(dt.timestamp())，对 UTC naive 值会按
    本机时区解释。部署在 GMT+8 时窗口整体前移 8 小时，周期末尾提交的周报
    （周日 16:00 之后）永远不在任何一次抓取范围内；而 UTC 服务器上无症状。
    这里同时断言「等于 UTC 的绝对时间戳」和「不等于本机时区解释的结果」，
    保证在东八区机器上跑也能抓到问题。
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        bodies.append(json.loads(request.content))
        return _ok({"journaluuid_list": [], "next_cursor": 0, "endflag": 1})

    client = _client(handler, monkeypatch)
    # 周一 00:00（UTC naive）—— 自然周的起点
    window_start = datetime(2026, 9, 7, 0, 0, 0)
    list(client.iter_report_ids(window_start, datetime(2026, 9, 13, 23, 59, 59)))

    assert bodies, "应当至少发出一次请求"
    expected = calendar.timegm(window_start.utctimetuple())
    assert bodies[0]["starttime"] == expected
    # 本机时区解释的（错误）结果：非 UTC 机器上必须不相等，否则说明没有真正按 UTC 换算
    host_tz_result = int(window_start.timestamp())
    if host_tz_result != expected:
        assert bodies[0]["starttime"] != host_tz_result, (
            "时间戳被按本机时区解释，企微窗口会整体偏移"
        )


def test_all_history_lower_bound_never_sends_zero_starttime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企微会对 starttime=0 返回 301066，全历史窗口必须从正数开始。"""
    from app.services.sync_scheduler import ALL_HISTORY_START

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        bodies.append(json.loads(request.content))
        return _ok({"journaluuid_list": [], "next_cursor": 0, "endflag": 1})

    client = _client(handler, monkeypatch)
    list(client.iter_report_ids(ALL_HISTORY_START, datetime(1970, 1, 2)))

    assert bodies[0]["starttime"] == 1


def test_pagination_follows_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        0: (["a", "b"], 2, 0),
        2: (["c"], 3, 1),
    }
    seen_cursors: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        body = json.loads(request.content)
        seen_cursors.append(body["cursor"])
        uuids, next_cursor, endflag = pages[body["cursor"]]
        return _ok(
            {
                "journaluuid_list": uuids,
                "next_cursor": next_cursor,
                "endflag": endflag,
            }
        )

    client = _client(handler, monkeypatch)
    ids = list(client.iter_report_ids(datetime(2026, 9, 1), datetime(2026, 9, 30)))

    assert ids == ["a", "b", "c"]
    assert seen_cursors == [0, 2]


def test_pagination_stops_when_cursor_stalls(monkeypatch: pytest.MonkeyPatch) -> None:
    """游标不前进时不能死循环。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        calls["n"] += 1
        return _ok({"journaluuid_list": ["a"], "next_cursor": 0, "endflag": 0})

    client = _client(handler, monkeypatch)
    ids = list(client.iter_report_ids(datetime(2026, 9, 1), datetime(2026, 9, 30)))

    assert ids == ["a"]
    assert calls["n"] == 1


def test_detail_returns_both_channel_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        return _ok(
            {
                "info": {
                    "journal_uuid": "j1",
                    "template_name": "本周工作汇报",
                    "report_time": 1786000000,
                    "submitter": {"userid": "u1"},
                    "sys_journal_data": "<p>富文本正文</p>",
                }
            }
        )

    client = _client(handler, monkeypatch)
    raw = client.get_report_detail("j1")

    assert raw.source == "wecom"
    assert raw.source_type == "weekly_report"
    assert raw.external_id == "j1"
    assert raw.payload["info"]["sys_journal_data"] == "<p>富文本正文</p>"


def test_permission_error_301065_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        return httpx.Response(
            200, json={"errcode": 301065, "errmsg": "no privilege"}
        )

    client = _client(handler, monkeypatch)
    with pytest.raises(cb.ConnectorPermissionError) as exc:
        client.get_report_detail("j1")

    message = str(exc.value)
    assert "301065" in message
    assert "可调用接口的应用" in message  # 必须给出可执行的处置建议
    assert exc.value.retryable is False


def test_token_expired_triggers_single_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    token_calls = {"n": 0}
    requests_with_bad_token = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            token_calls["n"] += 1
            return _ok({"access_token": f"tk{token_calls['n']}", "expires_in": 7200})
        if token_calls["n"] == 1:
            requests_with_bad_token["n"] += 1
            return httpx.Response(
                200, json={"errcode": 42001, "errmsg": "access_token expired"}
            )
        return _ok({"department": [{"id": 1, "name": "总部", "parentid": 0}]})

    client = _client(handler, monkeypatch)
    departments = client.list_departments()

    assert len(departments) == 1
    assert token_calls["n"] == 2  # 过期后刷新一次
    assert requests_with_bad_token["n"] == 1


def test_missing_credentials_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.delenv("WECOM_CORP_SECRET", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        client = WeComClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: _ok({}))))
        with pytest.raises(cb.ConnectorAuthError):
            client.get_access_token()
    finally:
        get_settings.cache_clear()


def test_all_recipients_invalid_reports_visibility_range_not_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """81013 = 所有收件人都不可投递，多为应用可见范围没覆盖。

    只抛一句 errcode，管理员会继续以为是代码坏了；这里必须直接指出
    「去调可见范围」——接口返回成功并不代表消息送达。
    """

    monkeypatch.setenv("WECOM_AGENT_ID", "1000002")
    from app.core.config import get_settings

    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 81013, "errmsg": "invalid user"})

    try:
        client = _client(handler, monkeypatch)
        with pytest.raises(cb.ConnectorPermissionError) as excinfo:
            client.send_text_card(
                user_ids=["u1", "u2"],
                title="周报提醒",
                description="<div>test</div>",
                url="https://example.com",
            )
    finally:
        get_settings.cache_clear()
    message = str(excinfo.value)
    assert "81013" in message
    assert "可见范围" in message


def test_rate_limit_is_retried_with_backoff_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """限流必须退避后重试，不能直接把异常抛给上层。

    这是「抓全部历史周报只抓到一点」的直接成因：详情调用被限流时立即抛出，
    上层的单条容错把它当成「这一条失败」继续以同样速率打下一个请求，
    于是每个请求都继续吃 45009 —— 任务最后以「有成功也有失败」收场，
    看起来像成功，实际只存进了限流前的那几十条。
    """
    import time as _time

    sleeps: list[float] = []
    monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        "app.connectors.wecom.client.RATE_LIMIT_BACKOFF_SECONDS", (5.0, 20.0, 45.0)
    )
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(200, json={"errcode": 45009, "errmsg": "api freq out of limit"})
        return _ok({"info": {"journal_uuid": "j1"}})

    client = _client(handler, monkeypatch)
    payload = client.get_report_detail("j1")

    assert payload.external_id == "j1"
    assert attempts["n"] == 3, "应在限流后重试到成功"
    # 退避必须跨越企微的分钟窗口；1~3 秒的短退避实测无效
    assert sleeps[:2] == [5.0, 20.0], f"退避序列不对：{sleeps[:2]}"
    assert min(RATE_LIMIT_BACKOFF_SECONDS) >= 5.0, "退避过短，跨不过分钟的限流窗口"


def test_rate_limit_gives_up_with_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试次数用尽后要给出「降低频率」的处置建议，而不是裸错误码。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 45009, "errmsg": "api freq out of limit"})

    client = _client(handler, monkeypatch)
    with pytest.raises(cb.ConnectorRateLimitError) as excinfo:
        client.get_report_detail("j1")

    assert "退避" in str(excinfo.value) or "频率" in str(excinfo.value)


def test_report_detail_is_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """详情调用必须节流（此前只有列表调用被节流，是限流的根因）。"""
    import time as _time

    monkeypatch.setattr(
        "app.connectors.wecom.client.REPORT_DETAIL_MIN_INTERVAL", 0.5
    )
    # 用一个假时钟驱动节流逻辑，避免测试真的睡。
    # 起点取一个较大值：真实 monotonic() 是系统开机时长，不会从 0 开始；
    # 从 0 开始会与「首次调用时间戳的初始值 0.0」相撞，让第一次调用也被节流。
    clock = {"t": 1000.0}
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(_time, "monotonic", fake_monotonic)
    monkeypatch.setattr(_time, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return _ok({"access_token": "tk", "expires_in": 7200})
        return _ok({"info": {"journal_uuid": "j"}})

    client = _client(handler, monkeypatch)
    # _client 把列表间隔置 0，但详情间隔保持上面设的 0.5
    client.get_report_detail("a")
    client.get_report_detail("b")

    assert sleeps == [0.5], f"第二次详情调用未被节流：{sleeps}"
