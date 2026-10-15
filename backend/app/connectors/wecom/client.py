"""企业微信 Connector。

接口事实（已核对官方文档，勿凭记忆修改）：

  - 汇报接口路径是 /cgi-bin/oa/journal/*，不是 journalrecord。
  - 汇报接口必须使用 **自建应用** 的 access_token，且该应用需被配置到
    管理后台「汇报 - 可调用接口的应用」中，并配置可信 IP。
    2023-12-01 起系统应用 secret 不再支持，否则报 301065。
  - get_record_list 的 starttime / endtime **间隔不能超过一个月**，
    且社区已确认企微按年分表，**跨年区间查询返回空列表**。
    所以所有区间必须先经 time_utils.split_time_range 切分。
  - 汇报正文有两条通道：
      * 自定义模板  -> info.apply_data.contents[]
      * 原生汇报模板 -> info.sys_journal_data（富文本，不在 apply_data 中）
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.connectors import base as cb
from app.core.config import Settings, get_settings
from app.core.time_utils import split_time_range, to_epoch_seconds, utcnow
from app.models.enums import DataSource, RawSourceType

TOKEN_PATH = "/cgi-bin/gettoken"
DEPARTMENT_LIST_PATH = "/cgi-bin/department/list"
USER_LIST_PATH = "/cgi-bin/user/list"
JOURNAL_LIST_PATH = "/cgi-bin/oa/journal/get_record_list"
JOURNAL_DETAIL_PATH = "/cgi-bin/oa/journal/get_record_detail"
MESSAGE_SEND_PATH = "/cgi-bin/message/send"
# 撤回应用消息。官方限制：仅 24 小时内、仅企微端数据（/document/path/94867）。
MESSAGE_RECALL_PATH = "/cgi-bin/message/recall"
# 登录 code 换用户身份（网页授权 / 扫码登录共用）
LOGIN_USERINFO_PATH = "/cgi-bin/auth/getuserinfo"

# token 无效 / 过期：刷新后重试一次
TOKEN_INVALID_CODES = {40001, 42001, 40014}
# 系统繁忙：可重试
SYSTEM_BUSY_CODES = {-1}
RATE_LIMIT_CODES = {45009, 45033}
# 无汇报应用数据拉取权限 -> 需要在管理后台授权，重试无用
# 301065：自建应用未被加入「汇报 - 可调用接口的应用」
# 301002：应用无接口权限
# 48002：api forbidden，接口未对当前应用开放（同样需在「汇报」应用后台
#        把自建应用加入「可调用接口的应用」，或该接口未开通）
PERMISSION_CODES = {301065, 301002, 48002}
# 来源 IP 不在白名单 -> 需把公网出口 IP 加入「企业可信IP」，重试无用
IP_BLOCKED_CODES = {60020}
# 应用消息全部收件人都不可投递（不在通讯录 / 不在应用可见范围）-> 重试无用。
# 这是「催交只有个别人收到」的另一种表现：部分无效时企微仍返回 errcode=0
# （把无效的人列在 invaliduser 里），**全部**无效时才直接报这个错。
ALL_RECIPIENTS_INVALID_CODES = {81013, 81014}
# 参数错误等：重试无用
RESPONSE_ERROR_CODES = {301066, 40058, 41001}

JOURNAL_LIST_MAX_LIMIT = 100
JOURNAL_LIST_MIN_INTERVAL = 1.0  # 秒；接口限制 600 次/分，保守节流

# 详情接口的节流间隔（秒）。**这一项曾经缺失，是「抓不全历史周报」的根因**：
# 只节流列表、不节流详情时，详情请求以网络速度猛打（本机可到 1000+ 次/分），
# 几秒内就超过企微 600 次/分的上限，之后**每一个**详情调用都吃 45009 限流错误。
# 而这些错误被同步循环的单条容错当成「这一条失败」，循环继续猛打、限流持续，
# 最后任务以「有成功也有失败」收场 —— 看着像成功，实际只存进了限流前的那几十条。
# 0.2s ≈ 300 次/分，留一半余量。
REPORT_DETAIL_MIN_INTERVAL = 0.2
# 限流退避序列（秒）。**必须够长**：企微的限流是按**分钟**窗口计数的，
# 等 1~3 秒不可能恢复 —— 实测退避 1s/2s/4s 共 3 次后仍被拒（见
# tests/test_sync.py::test_full_history_sync_survives_upstream_rate_limiting）。
# 这里直接跨越一个分钟窗口，让下一个请求落在新窗口里。
RATE_LIMIT_BACKOFF_SECONDS = (5.0, 20.0, 45.0)

# 企业微信 user/list 的 status 映射。
# 注：status=5（退出企业）是否出现在 user/list 返回值中需以实测为准，
#     scripts/probe_wecom.py 会打印实际字段用于确认。
USER_STATUS_MAP = {
    1: "active",
    2: "disabled",
    4: "pending",
    5: "resigned",
}


class WeComClient(cb.OrganizationSource, cb.ReportSource):
    """企业微信数据源。

    注意：本类 **不做任何业务映射**，只返回原始 dict / RawPayload。
    业务字段映射在 processors/ 与 services/ 中完成。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._http = http_client or httpx.Client(
            timeout=self.settings.wecom_http_timeout
        )
        self._owns_client = http_client is None
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        self._last_journal_call_at: float = 0.0
        self._last_detail_call_at: float = 0.0

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    @property
    def source(self) -> str:
        return DataSource.WECOM

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> WeComClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_credentials(self) -> None:
        if not self.settings.wecom_corp_id or not self.settings.wecom_corp_secret:
            raise cb.ConnectorAuthError(
                "缺少企业微信凭据：请设置 WECOM_CORP_ID 与 WECOM_CORP_SECRET"
            )

    def get_access_token(self, force_refresh: bool = False) -> str:
        """获取并缓存 access_token。

        企业微信 token 有效期 7200 秒，这里提前 300 秒刷新。
        """
        self._require_credentials()
        if (
            not force_refresh
            and self._token
            and time.monotonic() < self._token_expire_at
        ):
            return self._token

        data = self._raw_request(
            "GET",
            TOKEN_PATH,
            params={
                "corpid": self.settings.wecom_corp_id,
                "corpsecret": self.settings.wecom_corp_secret,
            },
            with_token=False,
        )
        token = data.get("access_token")
        if not token:
            raise cb.ConnectorAuthError(
                f"获取 access_token 失败：errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg')}"
            )
        expires_in = int(data.get("expires_in") or 7200)
        self._token = token
        self._token_expire_at = time.monotonic() + max(expires_in - 300, 60)
        return token

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        with_token: bool = True,
        retried_token: bool = False,
    ) -> dict:
        """发起请求并把 errcode 映射为 Connector 异常。"""
        url = f"{self.settings.wecom_base_url.rstrip('/')}{path}"
        query = dict(params or {})
        if with_token:
            query["access_token"] = self.get_access_token()

        last_error: Exception | None = None
        # 尝试次数要够走完限流退避序列：退避条目数是 len(RATE_LIMIT_BACKOFF_SECONDS)，
        # 加上最后那一次「不再重试、抛出」的尝试。只用配置的 wecom_http_retries（默认 3）
        # 会在退避还没走完时就放弃 —— 那正是「抓全历史只抓到一点」的残余形态。
        max_attempts = max(
            self.settings.wecom_http_retries, len(RATE_LIMIT_BACKOFF_SECONDS) + 1
        )
        for attempt in range(max_attempts):
            try:
                response = self._http.request(
                    method, url, params=query, json=json_body
                )
                response.raise_for_status()
                data: dict = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < max_attempts:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise cb.ConnectorUpstreamError(f"企业微信请求失败：{exc}") from exc

            errcode = data.get("errcode", 0)

            if errcode in TOKEN_INVALID_CODES and with_token and not retried_token:
                self.get_access_token(force_refresh=True)
                query["access_token"] = self._token or ""
                return self._raw_request(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    with_token=with_token,
                    retried_token=True,
                )

            if errcode == 0:
                return data

            errmsg = data.get("errmsg", "")
            if errcode in PERMISSION_CODES:
                raise cb.ConnectorPermissionError(
                    f"[{errcode}] {errmsg}。"
                    f"请确认自建应用已加入管理后台「汇报 - 可调用接口的应用」，"
                    f"且已配置可信 IP。"
                )
            if errcode in IP_BLOCKED_CODES:
                raise cb.ConnectorPermissionError(
                    f"[{errcode}] {errmsg}。"
                    f"处理：把上面 from ip 显示的公网出口 IP 添加到管理后台 "
                    f"「应用管理 → 该自建应用 → 开发者接口 → 企业可信IP」，"
                    f"必要时同时加到「我的企业 → 企业信息 → 企业可信IP」。"
                )
            if errcode in ALL_RECIPIENTS_INVALID_CODES:
                raise cb.ConnectorPermissionError(
                    f"[{errcode}] {errmsg}。"
                    f"所列收件人全部收不到该应用的消息，通常是自建应用的「可见范围」"
                    f"没包含他们所在的部门，或他们已不在企业通讯录中。"
                    f"处理：在管理后台「应用管理 → 该自建应用 → 可见范围」中调整。"
                    f"注意部分收件人无效时企微仍会返回成功，所以「发送成功」不等于"
                    f"「人人都收到了」。"
                )
            if errcode in RATE_LIMIT_CODES:
                # 限流必须**退避到下一个分钟窗口**再重试，不能立刻重发、也不能短退避：
                # 企微按分钟计数，等 1~3 秒毫无意义（实测仍被拒）。
                # 更不能直接抛出 —— 上层会把它当成「这一条失败」继续猛打，
                # 于是每个请求都继续吃限流，任务以 PARTIAL 收场、看着像成功。
                if attempt < len(RATE_LIMIT_BACKOFF_SECONDS):
                    time.sleep(RATE_LIMIT_BACKOFF_SECONDS[attempt])
                    continue
                raise cb.ConnectorRateLimitError(
                    f"[{errcode}] {errmsg}。已退避重试 "
                    f"{len(RATE_LIMIT_BACKOFF_SECONDS)} 次（共 "
                    f"{int(sum(RATE_LIMIT_BACKOFF_SECONDS))}s）仍被限流。"
                    f"接口限制约 600 次/分钟，请降低抓取并发后重试。"
                )
            if errcode in SYSTEM_BUSY_CODES:
                if attempt + 1 < max_attempts:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise cb.ConnectorUpstreamError(f"[{errcode}] {errmsg}")
            raise cb.ConnectorResponseError(f"[{errcode}] {errmsg}")

        raise cb.ConnectorUpstreamError(f"企业微信请求失败：{last_error}")

    # ------------------------------------------------------------------ #
    # 登录（网页授权 / 扫码登录换 code）
    # ------------------------------------------------------------------ #
    def get_login_userinfo(self, code: str) -> dict:
        """用登录 code 换取访问用户身份。

        只做「取数」：返回原始 dict，识别 userid / openid 由调用方处理。
        走本类自己的 access_token 缓存与重试，避免另起一套 HTTP 栈。
        """
        if not code:
            raise cb.ConnectorResponseError("缺少授权 code")
        return self._raw_request("GET", LOGIN_USERINFO_PATH, params={"code": code})

    # ------------------------------------------------------------------ #
    # 消息推送（周报催交提醒）
    # ------------------------------------------------------------------ #
    def send_text_card(
        self,
        user_ids: list[str],
        title: str,
        description: str,
        url: str,
        btn_text: str = "去填写",
    ) -> dict:
        """发送文本卡片消息（textcard）给指定 userid。

        注意：这是**会打扰到真人**的操作。调用方必须先做二次确认，
        且默认走 dry_run，只有显式要求时才真正投递。
        """
        if not user_ids:
            raise cb.ConnectorResponseError("收件人为空，拒绝发送")
        agent_id = self.settings.wecom_agent_id
        if not agent_id:
            raise cb.ConnectorAuthError(
                "缺少 WECOM_AGENT_ID：发送应用消息必须指定自建应用 AgentId"
            )
        return self._raw_request(
            "POST",
            MESSAGE_SEND_PATH,
            json_body={
                "touser": "|".join(user_ids),
                "msgtype": "textcard",
                "agentid": int(agent_id),
                "textcard": {
                    "title": title,
                    "description": description,
                    "url": url,
                    "btntxt": btn_text,
                },
            },
        )

    def recall_message(self, msgid: str) -> dict:
        """撤回一条已发送的应用消息。

        官方限制（已核对文档 /document/path/94867）：
        - **只能撤回 24 小时内**通过发送应用消息接口推送的消息；
        - **仅可撤回企业微信端的数据**，微信插件端的数据不支持撤回；
        - 必须提供发送时返回的 `msgid`（事后再查不到，只能发送当次保存）。

        这些限制由接口返回的错误码体现，本方法原样抛出让调用方转成
        可展示的原因 —— 不要在这里吞掉，否则用户点了撤回却不知道为什么没撤掉。
        """
        if not msgid:
            raise cb.ConnectorResponseError("缺少 msgid，无法撤回")
        return self._raw_request(
            "POST", MESSAGE_RECALL_PATH, json_body={"msgid": msgid}
        )

    def _throttle_journal_list(self) -> None:
        elapsed = time.monotonic() - self._last_journal_call_at
        if elapsed < JOURNAL_LIST_MIN_INTERVAL:
            time.sleep(JOURNAL_LIST_MIN_INTERVAL - elapsed)
        self._last_journal_call_at = time.monotonic()

    def _throttle_report_detail(self) -> None:
        """详情调用前的节流。

        与列表分开记账：两者的调用频率与配额互不影响，共用一个时间戳会让
        「刚查完列表」白白拖慢紧随其后的详情请求。见常量处的根因说明。
        """
        elapsed = time.monotonic() - self._last_detail_call_at
        if elapsed < REPORT_DETAIL_MIN_INTERVAL:
            time.sleep(REPORT_DETAIL_MIN_INTERVAL - elapsed)
        self._last_detail_call_at = time.monotonic()

    # ------------------------------------------------------------------ #
    # OrganizationSource
    # ------------------------------------------------------------------ #
    def list_departments(self) -> list[cb.RawPayload]:
        data = self._raw_request("GET", DEPARTMENT_LIST_PATH)
        departments = data.get("department") or []
        fetched_at = utcnow()
        return [
            cb.RawPayload(
                source=DataSource.WECOM,
                source_type="department",
                external_id=str(dept.get("id")),
                payload=dept,
                fetched_at=fetched_at,
            )
            for dept in departments
            if dept.get("id") is not None
        ]

    def list_employees(self, root_department_id: int = 1) -> list[cb.RawPayload]:
        """拉取全员。

        使用 department_id=根部门 + fetch_child=1 一次性取回。
        已知规模限制：单次返回上限较大，超过约万人级组织需改为按部门分批，
        当前 V1 未实现分批（企业规模未达该量级）。
        """
        data = self._raw_request(
            "GET",
            USER_LIST_PATH,
            params={"department_id": root_department_id, "fetch_child": 1},
        )
        users = data.get("userlist") or []
        fetched_at = utcnow()
        return [
            cb.RawPayload(
                source=DataSource.WECOM,
                source_type="employee",
                external_id=str(user.get("userid")),
                payload=user,
                fetched_at=fetched_at,
            )
            for user in users
            if user.get("userid")
        ]

    # ------------------------------------------------------------------ #
    # ReportSource
    # ------------------------------------------------------------------ #
    def iter_report_ids(
        self,
        start: datetime,
        end: datetime,
        template_ids: list[str] | None = None,
        limit: int = JOURNAL_LIST_MAX_LIMIT,
        **kwargs: object,
    ) -> Iterator[str]:
        """按「不跨年、不跨月」的片段分页产出 journal_uuid。"""
        limit = max(1, min(limit, JOURNAL_LIST_MAX_LIMIT))
        template_ids = template_ids or self.settings.wecom_report_template_ids

        for seg_start, seg_end in split_time_range(start, end):
            cursor = 0
            endflag = 0
            while not endflag:
                self._throttle_journal_list()
                body: dict[str, Any] = {
                    # start/end 是 UTC naive，必须按 UTC 换算成绝对时间戳。
                    # 用 int(dt.timestamp()) 会按本机时区解释，窗口整体偏移一个时区。
                    "starttime": to_epoch_seconds(seg_start),
                    "endtime": to_epoch_seconds(seg_end),
                    "cursor": cursor,
                    "limit": limit,
                }
                if template_ids:
                    body["filters"] = [
                        {"key": "template_id", "value": tid} for tid in template_ids
                    ]
                data = self._raw_request(
                    "POST", JOURNAL_LIST_PATH, json_body=body
                )
                for uuid in data.get("journaluuid_list") or []:
                    yield uuid
                endflag = int(data.get("endflag") or 0)
                if endflag:
                    break
                next_cursor = int(data.get("next_cursor") or 0)
                if next_cursor <= cursor:
                    # 游标没有前进，直接跳出避免死循环
                    break
                cursor = next_cursor

    def get_report_detail(self, external_id: str) -> cb.RawPayload:
        self._throttle_report_detail()
        data = self._raw_request(
            "POST", JOURNAL_DETAIL_PATH, json_body={"journaluuid": external_id}
        )
        info = data.get("info")
        if not isinstance(info, dict):
            raise cb.ConnectorResponseError(
                f"汇报 {external_id} 返回结构异常：缺少 info 字段"
            )
        # 用接口返回的 journal_uuid 兜底，保证与列表一致
        info.setdefault("journal_uuid", external_id)
        return cb.RawPayload(
            source=DataSource.WECOM,
            source_type=RawSourceType.WEEKLY_REPORT,
            external_id=external_id,
            payload=data,
            fetched_at=utcnow(),
        )

    # ------------------------------------------------------------------ #
    # 自检
    # ------------------------------------------------------------------ #
    def health_check(self) -> dict:
        """连通性与权限自检。

        分三步定位问题：凭据 -> 通讯录 -> 汇报权限。
        任何一步失败都会给出可直接执行的处置建议。
        """
        result: dict[str, Any] = {"source": self.source, "ok": True, "steps": []}

        def step(name: str, fn) -> bool:  # type: ignore[no-untyped-def]
            started = time.monotonic()
            try:
                detail = fn()
                result["steps"].append(
                    {
                        "step": name,
                        "ok": True,
                        "detail": detail,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    }
                )
                return True
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["steps"].append(
                    {
                        "step": name,
                        "ok": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    }
                )
                return False

        if not step("get_access_token", lambda: f"token={self.get_access_token()[:8]}..."):
            return result

        if not step(
            "department/list",
            lambda: f"{len(self.list_departments())} 个部门",
        ):
            return result

        def probe_reports() -> str:
            end = utcnow()
            start = end - timedelta(days=7)
            ids = list(self.iter_report_ids(start, end))
            sample: dict[str, Any] = {}
            if ids:
                detail = self.get_report_detail(ids[0]).payload
                info = detail.get("info", {})
                sample = {
                    "template_name": info.get("template_name"),
                    "has_apply_data": bool(info.get("apply_data", {}).get("contents")),
                    "has_sys_journal_data": bool(info.get("sys_journal_data")),
                }
            return f"最近 7 天 {len(ids)} 条汇报；样本 {sample or '（无数据，无法确认正文通道）'}"

        step("journal/list+detail", probe_reports)
        return result
