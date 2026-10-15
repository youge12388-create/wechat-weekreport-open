"""企业微信登录（网页授权 / 扫码）与树状登录授权。

覆盖三块：
1. **授权判定**（`services/access_service`）：就近继承、显式禁止、个人级追加、
   离职拦截 —— 这是「谁能进系统」的唯一真源。
2. **登录链路**（`/api/auth/wecom/*`）：state 防篡改 + 防登录 CSRF、
   code 换身份、未授权人员的落地页、回调域名与 URL 构造。
3. **会话与权限**（AuthMiddleware）：企微会话每次请求回源复核、
   member 只读（写操作 403）、撤销授权立即生效。

测试环境（APP_ENV=test）默认关闭鉴权中间件，本文件单独开启。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import (
    SESSION_COOKIE,
    create_session_token,
    read_session_token,
    sign_payload,
)
from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import (
    AccessDenyReason,
    DepartmentStatus,
    EmployeeStatus,
    LoginRole,
    LoginSource,
)
from app.services import access_service

# --------------------------------------------------------------------------- #
# 组织与员工夹具
# --------------------------------------------------------------------------- #


def _dept(
    session: Session,
    external_id: str,
    name: str,
    *,
    parent: Department | None = None,
    login_enabled: bool | None = None,
    login_role: str = LoginRole.MEMBER,
) -> Department:
    dept = Department(
        external_id=external_id,
        name=name,
        source="wecom",
        status=DepartmentStatus.ACTIVE,
        parent_id=parent.id if parent else None,
        parent_external_id=parent.external_id if parent else None,
        login_enabled=login_enabled,
        login_role=login_role,
    )
    session.add(dept)
    session.flush()
    return dept


def _employee(
    session: Session,
    external_id: str,
    name: str,
    *,
    department: Department | None = None,
    login_enabled: bool = False,
    login_role: str = LoginRole.MEMBER,
    status: str = EmployeeStatus.ACTIVE,
) -> Employee:
    emp = Employee(
        external_id=external_id,
        name=name,
        source="wecom",
        status=status,
        department_id=department.id if department else None,
        login_enabled=login_enabled,
        login_role=login_role,
    )
    session.add(emp)
    session.flush()
    return emp


# --------------------------------------------------------------------------- #
# 1. 授权判定
# --------------------------------------------------------------------------- #


def test_department_authorization_covers_whole_subtree(db_session: Session) -> None:
    """勾选一个部门 = 该部门**及全部下级**的人都能登录。

    这是「授权不占位置」的核心：不产生逐人授权行，组织调整也不用维护名单。
    """
    company = _dept(db_session, "1", "全公司")
    tech = _dept(db_session, "2", "技术部", parent=company)
    backend = _dept(db_session, "3", "后端组", parent=tech)
    other = _dept(db_session, "4", "市场部", parent=company)
    tech.login_enabled = True

    deep = _employee(db_session, "u-deep", "深层员工", department=backend)
    direct = _employee(db_session, "u-direct", "直属员工", department=tech)
    sibling = _employee(db_session, "u-sibling", "他部门员工", department=other)

    assert access_service.decide_access(db_session, deep).allowed is True
    assert access_service.decide_access(db_session, direct).allowed is True
    assert access_service.decide_access(db_session, sibling).allowed is False


def test_nearest_department_wins_over_ancestor(db_session: Session) -> None:
    """就近判定：更近的部门说了算，而不是「任一祖先授权即放行」。

    若用「任一祖先授权即放行」，就没法表达「全公司开放、某部门除外」——
    公司一开，下面所有部门都会被自动放行，授权也就失去了边界意义。
    """
    company = _dept(db_session, "1", "全公司", login_enabled=True)
    blocked = _dept(db_session, "2", "外包组", parent=company, login_enabled=False)
    open_dep = _dept(db_session, "3", "技术部", parent=company)

    blocked_emp = _employee(db_session, "u-blocked", "外包同学", department=blocked)
    open_emp = _employee(db_session, "u-open", "技术同学", department=open_dep)

    blocked_decision = access_service.decide_access(db_session, blocked_emp)
    assert blocked_decision.allowed is False
    assert blocked_decision.reason == AccessDenyReason.DENIED_BY_DEPARTMENT
    assert access_service.decide_access(db_session, open_emp).allowed is True


def test_unset_department_inherits_from_ancestor(db_session: Session) -> None:
    """未设置（None）与显式禁止（False）必须区分：前者继承，后者截断。"""
    company = _dept(db_session, "1", "全公司", login_enabled=True)
    unset = _dept(db_session, "2", "未设置部门", parent=company)  # login_enabled=None
    emp = _employee(db_session, "u1", "员工", department=unset)
    assert unset.login_enabled is None
    assert access_service.decide_access(db_session, emp).allowed is True


def test_employee_explicit_deny_overrides_department_admin(db_session: Session) -> None:
    """取消员工的“允许登录”后，不能再从部门继承管理员权限。"""
    company = _dept(db_session, "1", "全公司", login_enabled=True, login_role=LoginRole.ADMIN)
    emp = _employee(db_session, "u1", "员工", department=company)
    emp.login_denied = True
    decision = access_service.decide_access(db_session, emp)
    assert decision.allowed is False
    assert decision.reason == AccessDenyReason.DENIED_BY_EMPLOYEE


def test_no_authorization_anywhere_denies_by_default(db_session: Session) -> None:
    """链上没有任何显式设置 -> 拒绝。

    默认不授权是刻意的：升级到新版本不该把全公司一次性放进来。
    """
    company = _dept(db_session, "1", "全公司")
    emp = _employee(db_session, "u1", "员工", department=company)
    decision = access_service.decide_access(db_session, emp)
    assert decision.allowed is False
    assert decision.reason == AccessDenyReason.NOT_AUTHORIZED


def test_employee_level_grant_appends_to_department_rules(db_session: Session) -> None:
    """个人级授权用于跨部门领导：只追加权限，不参与「否决」。"""
    company = _dept(db_session, "1", "全公司", login_enabled=False)
    leader = _employee(
        db_session,
        "u-leader",
        "跨部门负责人",
        department=company,
        login_enabled=True,
        login_role=LoginRole.ADMIN,
    )
    colleague = _employee(db_session, "u2", "普通同事", department=company)

    leader_decision = access_service.decide_access(db_session, leader)
    assert leader_decision.allowed is True
    assert leader_decision.role == LoginRole.ADMIN
    assert access_service.decide_access(db_session, colleague).allowed is False


def test_department_role_is_inherited_by_subtree(db_session: Session) -> None:
    """部门授权携带角色：整棵子树按该角色登录（管理员不必逐人加白）。"""
    company = _dept(db_session, "1", "全公司")
    tech = _dept(
        db_session,
        "2",
        "技术部",
        parent=company,
        login_enabled=True,
        login_role=LoginRole.ADMIN,
    )
    sub = _dept(db_session, "3", "前端组", parent=tech)
    emp = _employee(db_session, "u1", "前端同学", department=sub)
    assert access_service.decide_access(db_session, emp).role == LoginRole.ADMIN


def test_inactive_employee_cannot_login(db_session: Session) -> None:
    """离职/禁用员工即便有部门授权也不能登录。"""
    dept = _dept(db_session, "1", "技术部", login_enabled=True)
    resigned = _employee(
        db_session, "u1", "已离职", department=dept, status=EmployeeStatus.RESIGNED
    )
    decision = access_service.decide_access(db_session, resigned)
    assert decision.allowed is False
    assert decision.reason == AccessDenyReason.INACTIVE


def test_unknown_userid_is_not_found(db_session: Session) -> None:
    decision = access_service.decide_access(db_session, None)
    assert decision.allowed is False
    assert decision.reason == AccessDenyReason.NOT_FOUND


def test_unknown_role_falls_back_to_read_only(db_session: Session) -> None:
    """角色值异常时必须降级为只读，不能猜一个更宽松的。"""
    dept = _dept(db_session, "1", "技术部", login_enabled=True, login_role="superuser")
    emp = _employee(db_session, "u1", "员工", department=dept)
    assert access_service.decide_access(db_session, emp).role == LoginRole.MEMBER


def test_department_ancestors_survives_cycle(db_session: Session) -> None:
    """部门数据成环时不能死循环（坏数据不该把接口挂死）。"""
    a = _dept(db_session, "1", "A")
    b = _dept(db_session, "2", "B", parent=a)
    a.parent_id = b.id  # 人为造环
    db_session.flush()
    chain = access_service.department_ancestors(db_session, b.id)
    assert len(chain) <= 2


def test_authorized_employee_ids_matches_per_employee_decisions(
    db_session: Session,
) -> None:
    """批量口径（设置页统计）必须与逐人判定完全一致。

    两条路径各算一套是这类功能最容易出的错：界面显示「已授权 12 人」，
    实际只有 9 个人能进来。
    """
    company = _dept(db_session, "1", "全公司")
    tech = _dept(
        db_session,
        "2",
        "技术部",
        parent=company,
        login_enabled=True,
        login_role=LoginRole.ADMIN,
    )
    blocked = _dept(db_session, "3", "外包组", parent=company, login_enabled=False)
    sales = _dept(db_session, "4", "市场部", parent=company)

    employees = [
        _employee(db_session, "u1", "技术A", department=tech),
        _employee(db_session, "u2", "技术B", department=tech),
        _employee(db_session, "u3", "外包C", department=blocked),
        _employee(db_session, "u4", "市场D", department=sales),
        _employee(
            db_session, "u5", "跨部门E", department=sales, login_enabled=True
        ),
        _employee(
            db_session,
            "u6",
            "离职F",
            department=tech,
            status=EmployeeStatus.RESIGNED,
        ),
    ]
    db_session.flush()

    allowed, admins = access_service.authorized_employee_ids(db_session)
    expected_allowed = {
        emp.id for emp in employees if access_service.decide_access(db_session, emp).allowed
    }
    expected_admins = {
        emp.id
        for emp in employees
        if access_service.decide_access(db_session, emp).allowed
        and access_service.decide_access(db_session, emp).role == LoginRole.ADMIN
    }
    assert allowed == expected_allowed
    assert admins == expected_admins


# --------------------------------------------------------------------------- #
# 2. 登录链接构造与身份换取
# --------------------------------------------------------------------------- #


def _login_settings(**overrides) -> Settings:
    base = {
        "wecom_corp_id": "ww-corp-id",
        "wecom_corp_secret": "secret",
        "wecom_agent_id": "1000002",
        "wecom_login_enabled": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_qr_login_url_encodes_redirect_uri() -> None:
    """回调地址必须整体 URLEncode。

    不转义时 `redirect_uri` 里的 `://` 与 `&` 会让外层 query 解析错位，
    企业微信侧看到的是被截断的域名，报「页面域名与登录授权域名不一致」。
    """
    from app.connectors.wecom.login import build_qr_login_url

    settings = _login_settings()
    url = build_qr_login_url(
        settings, "https://zhoubao.example.com/api/auth/wecom/callback", "state-token"
    )
    assert url.startswith("https://login.work.weixin.qq.com/wwlogin/sso/login?")
    assert "login_type=CorpApp" in url
    assert "appid=ww-corp-id" in url
    assert "agentid=1000002" in url
    # 冒号与斜杠被转义：原始形式不得直接出现
    assert "https%3A%2F%2Fzhoubao.example.com%2Fapi%2Fauth%2Fwecom%2Fcallback" in url
    assert "state=state-token" in url


def test_oauth_url_has_wechat_redirect_fragment() -> None:
    """网页授权链接必须带 #wechat_redirect，否则企业微信客户端不注入身份。"""
    from app.connectors.wecom.login import build_oauth_url

    url = build_oauth_url(_login_settings(), "https://x.example.com/cb", "s1")
    assert url.startswith("https://open.weixin.qq.com/connect/oauth2/authorize?")
    assert "scope=snsapi_base" in url
    assert url.endswith("#wechat_redirect")
    assert "agentid=1000002" in url


class _FakeLoginClient:
    """只实现登录所需的一个方法；真实 WeComClient 的其余能力与本测试无关。"""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.codes: list[str] = []

    def get_login_userinfo(self, code: str) -> dict:
        self.codes.append(code)
        return self.response


def test_resolve_userid_strips_corp_prefix() -> None:
    """上下游企业的 userid 形如 `CorpId/userid`，本地通讯录存的是裸 userid。"""
    from app.connectors.wecom.login import resolve_userid

    client = _FakeLoginClient({"userid": "ww123/zhangsan", "errcode": 0})
    assert resolve_userid(client, "CODE")["userid"] == "zhangsan"


def test_resolve_userid_reports_non_member() -> None:
    """扫码的人不是本企业成员时没有任何 userid，只返回 openid。"""
    from app.connectors.wecom.login import resolve_userid

    client = _FakeLoginClient({"openid": "o-xyz", "errcode": 0})
    result = resolve_userid(client, "CODE")
    assert result["userid"] is None
    assert result["openid"] == "o-xyz"


# --------------------------------------------------------------------------- #
# 3. 会话令牌
# --------------------------------------------------------------------------- #


def _strong_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = "k" * 48
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": ()}
    )
    get_settings.cache_clear()
    return secret


def test_wecom_session_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    _strong_secret(monkeypatch)
    try:
        token = create_session_token(
            subject="zhangsan",
            role=LoginRole.MEMBER,
            source=LoginSource.WECOM,
            name="张三",
            employee_id=7,
        )
        user = read_session_token(token)
        assert user is not None
        assert user.subject == "zhangsan"
        assert user.role == LoginRole.MEMBER
        assert user.source == LoginSource.WECOM
        assert user.employee_id == 7
    finally:
        get_settings.cache_clear()


def test_password_session_cannot_be_downgraded_or_impersonated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """密码路径只有一个超管：subject 必须 admin、角色必须 admin。

    否则一个被改成 member 的密码会话会把老板锁在只读权限里，
    而 subject 改成别人的令牌会被当成那个人的企微会话。
    """
    secret = _strong_secret(monkeypatch)
    try:
        forged_subject = sign_payload(
            {
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "sub": "zhangsan",
                "role": LoginRole.ADMIN,
                "src": LoginSource.PASSWORD,
                "name": "张三",
            },
            secret,
        )
        assert read_session_token(forged_subject) is None

        forged_role = sign_payload(
            {
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "sub": "admin",
                "role": LoginRole.MEMBER,
                "src": LoginSource.PASSWORD,
                "name": "管理员",
            },
            secret,
        )
        assert read_session_token(forged_role) is None
    finally:
        get_settings.cache_clear()


def test_legacy_admin_token_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧格式令牌（无 role/src）等价于管理员密码会话，避免升级即全员掉线。"""
    secret = _strong_secret(monkeypatch)
    try:
        legacy = sign_payload(
            {"iat": int(time.time()), "exp": int(time.time()) + 600, "sub": "admin"},
            secret,
        )
        user = read_session_token(legacy)
        assert user is not None
        assert user.role == LoginRole.ADMIN
        assert user.source == LoginSource.PASSWORD
    finally:
        get_settings.cache_clear()


def test_wecom_session_without_employee_id_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企微会话必须带员工 id：没有它就无法回源复核授权。"""
    secret = _strong_secret(monkeypatch)
    try:
        token = sign_payload(
            {
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
                "sub": "zhangsan",
                "role": LoginRole.MEMBER,
                "src": LoginSource.WECOM,
                "name": "张三",
            },
            secret,
        )
        assert read_session_token(token) is None
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 4. 登录接口与中间件（开启鉴权）
# --------------------------------------------------------------------------- #


@pytest.fixture
def login_client(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> TestClient:
    """开启鉴权的客户端，复用 db_session 的内存库。

    必须复用同一个库：内存库是 per-connection 的，另建引擎会得到一个空库，
    测试数据就查不到了。
    """
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-strong-admin-password")
    monkeypatch.setenv("SESSION_SECRET", "k" * 48)
    monkeypatch.setenv("WECOM_CORP_ID", "ww-corp-id")
    monkeypatch.setenv("WECOM_CORP_SECRET", "secret")
    monkeypatch.setenv("WECOM_AGENT_ID", "1000002")
    monkeypatch.setenv("WECOM_LOGIN_ENABLED", "true")
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": ()}
    )
    get_settings.cache_clear()
    # db_session 已经把引擎指向内存库；这里只补建表结构
    engine = get_engine(get_settings())
    Base.metadata.create_all(engine)
    from app.main import create_app

    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()
        dispose_engine()


def test_wecom_start_redirects_to_qr_login(
    login_client: TestClient, db_session: Session
) -> None:
    """未登录也能发起登录：/api/auth/wecom/* 必须在白名单内。"""
    resp = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://login.work.weixin.qq.com/wwlogin/sso/login?")
    # 回调域与 state 绑定 Cookie 同时下发
    assert "redirect_uri=" in location
    assert login_client.cookies.get("wb_login_state")


def test_wecom_start_uses_oauth_inside_wecom_client(
    login_client: TestClient, db_session: Session
) -> None:
    """企业微信客户端内打开走网页授权（免扫码），PC 浏览器走扫码。"""
    resp = login_client.get(
        "/api/auth/wecom/start",
        headers={"User-Agent": "Mozilla/5.0 wxwork/4.1.0 MicroMessenger/7.0"},
        follow_redirects=False,
    )
    assert resp.headers["location"].startswith(
        "https://open.weixin.qq.com/connect/oauth2/authorize?"
    )
    assert resp.headers["location"].endswith("#wechat_redirect")


def test_wecom_start_blocked_when_disabled(
    login_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WECOM_LOGIN_ENABLED", "false")
    get_settings.cache_clear()
    resp = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    assert resp.status_code == 403
    assert "企业微信登录" in resp.text


def test_callback_rejects_unsigned_state(login_client: TestClient) -> None:
    """state 没有签名 -> 拒绝。否则攻击者能塞进任意 code。"""
    resp = login_client.get(
        "/api/auth/wecom/callback?code=CODE&state=forged-state",
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert "登录链接已失效" in resp.text


def test_callback_rejects_state_without_matching_cookie(
    login_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """state 合法但不是本机发起的 -> 拒绝（防登录 CSRF）。

    攻击者可以自己走一遍登录拿到合法 state，再把
    `?code=<攻击者的code>&state=<合法state>` 发给受害者，让受害者浏览器
    以攻击者身份登录（污染审计与操作归属）。没有这层校验就挡不住。
    """
    issued = int(time.time())
    state = sign_payload(
        {
            "iat": issued,
            "exp": issued + 600,
            "n": "attacker-nonce",
            "next": "/dashboard",
            "m": "qr",
        }
    )
    # 刻意不设置 wb_login_state Cookie（受害者浏览器里没有对应的 nonce）
    resp = login_client.get(
        f"/api/auth/wecom/callback?code=CODE&state={state}", follow_redirects=False
    )
    assert resp.status_code == 403
    assert "登录链接已失效" in resp.text


def test_callback_denies_unauthorized_employee_with_actionable_page(
    login_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未授权成员拿到的是「原因 + 找谁处理」，而不是一句登录失败。"""
    dept = _dept(db_session, "1", "技术部")  # 未授权
    _employee(db_session, "zhangsan", "张三", department=dept)
    db_session.commit()

    monkeypatch.setattr(
        "app.api.routes.auth.resolve_userid",
        lambda client, code: {"userid": "zhangsan"},
    )
    start = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    state = _state_from_location(start.headers["location"])

    resp = login_client.get(
        f"/api/auth/wecom/callback?code=CODE&state={state}", follow_redirects=False
    )
    assert resp.status_code == 403
    assert "还没有被授权访问本系统" in resp.text
    assert "登录与授权" in resp.text


def test_callback_denies_non_member(login_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """扫码的人是外部人员（没有 userid）时必须给出对应原因。"""
    monkeypatch.setattr(
        "app.api.routes.auth.resolve_userid",
        lambda client, code: {"userid": None, "openid": "o-1"},
    )
    start = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    state = _state_from_location(start.headers["location"])
    resp = login_client.get(
        f"/api/auth/wecom/callback?code=CODE&state={state}", follow_redirects=False
    )
    assert resp.status_code == 403
    assert "不是本企业成员" in resp.text


def test_callback_grants_session_for_authorized_employee(
    login_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """授权成员登录成功：下发会话，并等待会话生效后进入前端页面。"""
    dept = _dept(db_session, "1", "技术部", login_enabled=True)
    _employee(db_session, "zhangsan", "张三", department=dept)
    db_session.commit()

    monkeypatch.setattr(
        "app.api.routes.auth.resolve_userid",
        lambda client, code: {"userid": "zhangsan"},
    )
    start = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    state = _state_from_location(start.headers["location"])

    resp = login_client.get(
        f"/api/auth/wecom/callback?code=CODE&state={state}", follow_redirects=False
    )
    assert resp.status_code == 200
    assert "登录成功，正在进入系统" in resp.text
    assert 'fetch("/api/auth/me"' in resp.text
    assert "/dashboard" in resp.text
    assert "127.0.0.1" not in resp.text
    assert "testserver" not in resp.text
    assert resp.headers["cache-control"] == "no-store, max-age=0"
    assert login_client.cookies.get(SESSION_COOKIE)

    me = login_client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    assert me["name"] == "张三"
    assert me["role"] == LoginRole.MEMBER
    assert me["source"] == LoginSource.WECOM
    assert me["is_admin"] is False


def test_callback_next_must_be_internal_path(
    login_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """next 只接受站内相对路径：`//evil.com` 会被浏览器当外链（开放重定向）。"""
    dept = _dept(db_session, "1", "技术部", login_enabled=True)
    _employee(db_session, "zhangsan", "张三", department=dept)
    db_session.commit()
    monkeypatch.setattr(
        "app.api.routes.auth.resolve_userid",
        lambda client, code: {"userid": "zhangsan"},
    )

    start = login_client.get(
        "/api/auth/wecom/start?next=//evil.example.com", follow_redirects=False
    )
    state = _state_from_location(start.headers["location"])
    resp = login_client.get(
        f"/api/auth/wecom/callback?code=CODE&state={state}", follow_redirects=False
    )
    assert resp.status_code == 200
    assert "/dashboard" in resp.text
    assert "evil.example.com" not in resp.text


def test_member_cannot_write_and_cannot_read_company_data(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读成员：读与写都被拦在公司级数据之外（详见下一条测试的说明）。"""
    _login_as_member(login_client, db_session, monkeypatch, role=LoginRole.MEMBER)
    # 旧的「GET 放行」在这里被推翻了：/api/sync/jobs 属运维面，member 不该看到
    assert login_client.get("/api/sync/jobs").status_code == 403
    resp = login_client.post(
        "/api/sync/wecom/reports/queue", json={"mode": "last_period"}
    )
    assert resp.status_code == 403
    assert "只读成员" in resp.json()["message"]


def test_member_can_only_reach_my_actions(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读成员能且只能访问 `/api/my-actions`。

    这条守护的是 `AuthMiddleware._MEMBER_ALLOWED_PREFIXES` —— 全公司数据的
    安全边界。**改宽它之前请先读这条测试的注释**：

    旧实现是「member 可以读全部、只是不能写」，于是任何企业微信能登录的同事
    都能读到 `/api/reports`（全公司周报正文）、`/api/employees`（通讯录）、
    `/api/dashboard/*`（部门经营指标）、`/api/exports/reports`（全量 CSV）。
    那是隐私泄漏。现在改为白名单：GET 也拦。
    """
    _login_as_member(login_client, db_session, monkeypatch, role=LoginRole.MEMBER)

    # 名单内：通过中间件进入路由（事项不存在 → 404，说明没被 403 拦住）
    inside = login_client.post("/api/my-actions/12345/messages", json={"body": "进展"})
    assert inside.status_code == 404
    assert inside.json()["detail"] == "事项不存在或未指派给你"

    # 名单外：**读**与**写**一律 403。这里刻意覆盖了「旧实现允许的只读路径」，
    # 它们正是隐私泄漏点，回归时必须继续拦住。
    for method, path in (
        # 写操作
        ("post", "/api/management-actions/1/messages"),
        ("patch", "/api/management-actions/1"),
        ("post", "/api/management-actions/1/status"),
        ("post", "/api/sync/wecom/reports/queue"),
        # 读操作：全公司数据面
        ("get", "/api/reports"),
        ("get", "/api/reports/1"),
        ("get", "/api/employees"),
        ("get", "/api/employees/1"),
        ("get", "/api/departments"),
        ("get", "/api/dashboard/overview"),
        ("get", "/api/dashboard/departments"),
        ("get", "/api/blockers"),
        ("get", "/api/blockers/1"),
        ("get", "/api/insights"),
        ("get", "/api/exports/reports"),
        ("get", "/api/exports/unsubmitted"),
        ("get", "/api/notifications/feed"),
        ("get", "/api/management-actions"),
        ("get", "/api/management-actions/1/messages"),
        ("get", "/api/settings/ai"),
        ("get", "/api/ai/usage"),
        # 前缀伪造：不能靠「前缀像」混进来
        ("get", "/api/my-actions-evil/x"),
        ("post", "/api/my-actions-evil/x"),
    ):
        response = getattr(login_client, method)(path)
        assert response.status_code == 403, f"{method.upper()} {path} 不应放行"
        assert "只读成员" in response.json()["message"]


def test_admin_role_still_reaches_company_data(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部门管理员（login_role=admin）不受 member 白名单限制。

    收口 member 不能顺带把部门负责人也锁死 —— 他们要看经营数据本就是设计意图。
    """
    _login_as_member(login_client, db_session, monkeypatch, role=LoginRole.ADMIN)
    for path in ("/api/employees", "/api/reports", "/api/dashboard/overview"):
        assert login_client.get(path).status_code == 200, path


def test_member_read_of_others_action_returns_404_not_403(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """访问别人的事项返回 404：403 会确认「这个 id 确实存在别人的事项」。"""
    from datetime import date, datetime

    from app.models.enums import ManagementActionAuthor
    from app.models.management_action import ManagementAction

    _login_as_member(login_client, db_session, monkeypatch, role=LoginRole.MEMBER)
    employee = access_service.find_employee_by_userid(db_session, "zhangsan")
    assert employee is not None

    # 一条指派给别人的事项
    other = _employee(db_session, "u-other-owner", "别的负责人")
    db_session.flush()
    action = ManagementAction(
        source_type="insight",
        source_key="insight:test:404",
        title="别人的事项",
        priority="medium",
        status="OPEN",
        owner_employee_id=other.id,
    )
    db_session.add(action)
    db_session.commit()

    # 别人的事项：在 member 唯一可用的入口里返回 404。
    # 注意这里**不能**再用 /api/management-actions 对比 —— 那条路径对 member
    # 已经是 403（全公司数据边界），它证明的是「前缀被拦住」，与 id 是否存在无关。
    assert login_client.get(f"/api/my-actions/{action.id}").status_code == 404
    assert login_client.get(f"/api/my-actions/{action.id}/messages").status_code == 404

    # 自己的事项可见
    mine = ManagementAction(
        source_type="insight",
        source_key="insight:test:mine",
        title="我的事项",
        priority="medium",
        status="OPEN",
        owner_employee_id=employee.id,
    )
    db_session.add(mine)
    db_session.commit()
    detail = login_client.get(f"/api/my-actions/{mine.id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    # 负责人视角不含内部字段
    for field in ("note", "source_snapshot", "source_stale", "source_href"):
        assert field not in body


def test_admin_role_can_write(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部门管理员（admin 角色）可以写：否则「部门负责人」这个角色没有意义。"""
    _login_as_member(login_client, db_session, monkeypatch, role=LoginRole.ADMIN)
    resp = login_client.patch("/api/access/settings", json={"enabled": True})
    assert resp.status_code == 200


def test_revoking_authorization_takes_effect_immediately(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消授权后立刻失效，不必等令牌过期。

    令牌只证明「该用户在某时刻通过了认证」。若把令牌当授权依据，
    撤销授权要等最长 12 小时才生效 —— 这正是必须在中间件回源复核的原因。
    """
    dept = _dept(db_session, "1", "技术部", login_enabled=True)
    _login_as_member(login_client, db_session, monkeypatch, role=LoginRole.MEMBER)
    # 探针用 member 自己的入口：/api/sync/jobs 对 member 已经是 403，
    # 拿它当「会话有效」的判据会把权限拦截误读成会话失效。
    assert login_client.get("/api/my-actions").status_code == 200

    dept.login_enabled = False
    db_session.commit()

    resp = login_client.get("/api/my-actions")
    assert resp.status_code == 401
    assert resp.json()["error"] == "AccessRevoked"
    # /api/auth/me 会给出具体原因，供登录页展示
    assert login_client.get("/api/auth/me").json()["authenticated"] is False


def test_employee_explicit_deny_invalidates_existing_session(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """个人明确禁止必须覆盖部门管理员权限，并让在线会话立即失效。"""
    dept = _dept(
        db_session,
        "1",
        "技术部",
        login_enabled=True,
        login_role=LoginRole.ADMIN,
    )
    employee = _employee(db_session, "zhangsan", "张三", department=dept)
    db_session.commit()
    _login_via_callback(login_client, monkeypatch, "zhangsan")
    assert login_client.get("/api/dashboard/overview").status_code == 200

    employee.login_denied = True
    db_session.commit()

    response = login_client.get("/api/my-actions")
    assert response.status_code == 401
    assert response.json()["error"] == "AccessRevoked"


def test_downgrade_admin_keeps_conversation_but_blocks_reports(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消管理员只降级：企微事项对话保留，公司周报与管理页面立即封锁。"""
    dept = _dept(
        db_session,
        "1",
        "技术部",
        login_enabled=True,
        login_role=LoginRole.ADMIN,
    )
    employee = _employee(db_session, "zhangsan", "张三", department=dept)
    db_session.commit()
    _login_via_callback(login_client, monkeypatch, "zhangsan")
    assert login_client.get("/api/reports").status_code == 200

    employee.login_enabled = True
    employee.login_denied = False
    employee.login_role = LoginRole.MEMBER
    db_session.commit()

    assert login_client.get("/api/my-actions").status_code == 200
    for path in ("/api/reports", "/api/dashboard/overview", "/api/employees"):
        assert login_client.get(path).status_code == 403, path


def test_resigned_employee_session_invalidated(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """员工离职后其在线会话立即失效。"""
    dept = _dept(db_session, "1", "技术部", login_enabled=True)
    emp = _employee(db_session, "zhangsan", "张三", department=dept)
    db_session.commit()
    _login_via_callback(login_client, monkeypatch, "zhangsan")
    assert login_client.get("/api/my-actions").status_code == 200

    emp.status = EmployeeStatus.RESIGNED
    db_session.commit()
    assert login_client.get("/api/my-actions").status_code == 401


def test_password_login_still_works_and_returns_admin(
    login_client: TestClient,
) -> None:
    """密码登录保持可用（企微配置坏掉时的兜底入口），且恒为管理员。"""
    resp = login_client.post(
        "/api/auth/login", json={"password": "a-strong-admin-password"}
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["is_admin"] is True
    assert login_client.get("/api/sync/jobs").status_code == 200


def test_access_endpoints_are_admin_only(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """授权管理整块只对可写管理员开放，**读也不给**。

    授权树会暴露「谁有管理权限」，属于管理工具而非业务数据；
    让普通成员能读它没有业务价值，却把管理面暴露给所有人。
    """
    _login_as_member(login_client, db_session, monkeypatch, role=LoginRole.MEMBER)

    assert login_client.get("/api/access/overview").status_code == 403
    assert login_client.get("/api/access/tree").status_code == 403
    assert login_client.patch("/api/access/departments/1", json={"enabled": True}).status_code == 403
    assert login_client.patch("/api/access/settings", json={"enabled": False}).status_code == 403


def test_access_endpoints_open_for_admin(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """可写管理员（部门授权出来的 admin 角色）能读能写。"""
    _login_as_member(login_client, db_session, monkeypatch, role=LoginRole.ADMIN)
    assert login_client.get("/api/access/overview").status_code == 200
    assert login_client.get("/api/access/tree").status_code == 200
    assert login_client.patch("/api/access/settings", json={"enabled": True}).status_code == 200


def test_password_admin_can_read_access_tree(
    login_client: TestClient, db_session: Session
) -> None:
    """访问密码会话恒为可写管理员，可读授权树。"""
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})
    assert login_client.get("/api/access/tree").status_code == 200


def test_access_tree_reports_inherited_effective_state(
    login_client: TestClient, db_session: Session
) -> None:
    """树接口必须给出「继承后的有效结论」，前端不再自己算一套规则。"""
    company = _dept(db_session, "1", "全公司", login_enabled=True)
    tech = _dept(db_session, "2", "技术部", parent=company)  # 未设置 -> 继承允许
    blocked = _dept(db_session, "3", "外包组", parent=company, login_enabled=False)
    db_session.commit()
    login_client.post(
        "/api/auth/login", json={"password": "a-strong-admin-password"}
    )

    tree = login_client.get("/api/access/tree").json()
    by_id = {node["id"]: node for node in tree["departments"]}
    assert by_id[tech.id]["setting"] is None
    assert by_id[tech.id]["effective"]["allowed"] is True
    assert by_id[blocked.id]["effective"]["allowed"] is False
    assert by_id[blocked.id]["effective"]["source_name"] == "外包组"


def test_access_patch_department_three_states(
    login_client: TestClient, db_session: Session
) -> None:
    """三态写接口：true / false / null 都能落库且可回读。"""
    dept = _dept(db_session, "1", "技术部")
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})

    resp = login_client.patch(
        f"/api/access/departments/{dept.id}",
        json={"enabled": True, "role": LoginRole.ADMIN},
    )
    assert resp.json() == {"id": dept.id, "setting": True, "role": LoginRole.ADMIN}

    resp = login_client.patch(
        f"/api/access/departments/{dept.id}", json={"enabled": False}
    )
    assert resp.json()["setting"] is False

    resp = login_client.patch(
        f"/api/access/departments/{dept.id}", json={"enabled": None}
    )
    assert resp.json()["setting"] is None


def test_access_patch_employee_disable_sets_explicit_deny(
    login_client: TestClient, db_session: Session
) -> None:
    """员工取消一次即可禁止，授权树必须同步返回最终状态。"""
    dept = _dept(
        db_session,
        "1",
        "技术部",
        login_enabled=True,
        login_role=LoginRole.ADMIN,
    )
    employee = _employee(
        db_session,
        "u1",
        "员工",
        department=dept,
        login_enabled=True,
        login_role=LoginRole.ADMIN,
    )
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})

    response = login_client.patch(
        f"/api/access/employees/{employee.id}", json={"enabled": False}
    )
    assert response.status_code == 200
    assert response.json()["denied"] is True

    tree = login_client.get("/api/access/tree").json()
    node = next(item for item in tree["employees"] if item["id"] == employee.id)
    assert node["denied"] is True
    assert employee.id not in tree["authorized_employee_ids"]

    enabled = login_client.patch(
        f"/api/access/employees/{employee.id}", json={"enabled": True}
    )
    assert enabled.status_code == 200
    assert enabled.json()["role"] == LoginRole.MEMBER
    tree = login_client.get("/api/access/tree").json()
    assert employee.id in tree["authorized_employee_ids"]
    assert employee.id not in tree["admin_employee_ids"]


def test_access_overview_lists_prerequisites(
    login_client: TestClient, db_session: Session
) -> None:
    """前提条件要一次列全，缺哪项直接告诉管理员去哪里配。"""
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})
    overview = login_client.get("/api/access/overview").json()
    keys = {item["key"] for item in overview["prerequisites"]}
    assert {"corp_id", "agent_id", "contacts", "redirect_uri"} <= keys
    assert overview["enabled"] is True
    assert overview["agent_id"] == "1000002"


def test_wecom_login_settings_saved_at_runtime(
    login_client: TestClient, db_session: Session
) -> None:
    """开关与回调地址属于运行期设置：保存后下一次登录即生效，无需重启。"""
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})
    resp = login_client.patch(
        "/api/access/settings",
        json={
            "enabled": False,
            "redirect_uri": "https://zhoubao.example.com/api/auth/wecom/callback",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert resp.json()["redirect_uri"].startswith("https://zhoubao.example.com")

    # 关闭后发起登录被拒
    assert login_client.get("/api/auth/wecom/start", follow_redirects=False).status_code == 403


def test_config_endpoint_exposes_login_switch_without_auth(
    login_client: TestClient, db_session: Session
) -> None:
    """登录页要读 /api/config 决定显示哪些入口，所以它必须免登录可读。"""
    db_session.commit()
    body = login_client.get("/api/config").json()
    assert body["wecom_login_enabled"] is True
    assert body["password_login_enabled"] is True


def test_callback_uses_configured_redirect_uri(
    login_client: TestClient, db_session: Session
) -> None:
    """配置了回调地址时必须用它 —— 企微侧校验的是后台登记的那个域名。"""
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})
    login_client.patch(
        "/api/access/settings",
        json={"redirect_uri": "https://zhoubao.example.com/api/auth/wecom/callback"},
    )
    login_client.post("/api/auth/logout")
    resp = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    assert (
        "https%3A%2F%2Fzhoubao.example.com%2Fapi%2Fauth%2Fwecom%2Fcallback"
        in resp.headers["location"]
    )


def test_start_appends_callback_path_when_only_domain_configured(
    login_client: TestClient, db_session: Session
) -> None:
    """回调地址只填域名时，系统必须自动补上回调路径。

    真实事故（2026-09-18 线上）：管理员照抄企微后台的「可信域名」，回调地址只填了
    `https://域名`。系统原样把它当 redirect_uri 下发，扫码后企微把
    `?code=...&state=...` 回跳到**站点根**，而前端 `/` 直接重定向到 `/dashboard`
    （query 被丢掉）—— code 永远到不了回调接口，用户看到的就是「登录了一圈又回到
    登录页」。授权勾没勾根本不是原因：这条断点对所有人成立。
    """
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})
    login_client.patch(
        "/api/access/settings", json={"redirect_uri": "https://zhoubao.example.com"}
    )
    login_client.post("/api/auth/logout")

    resp = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    assert (
        "https%3A%2F%2Fzhoubao.example.com%2Fapi%2Fauth%2Fwecom%2Fcallback"
        in resp.headers["location"]
    )
    # 裸域名（无路径）绝不能作为 redirect_uri 下发
    assert "redirect_uri=https%3A%2F%2Fzhoubao.example.com&" not in resp.headers["location"]


def _fake_wecom_transport(monkeypatch: pytest.MonkeyPatch, userid: str) -> list[str]:
    """把企微的 HTTP 端点换成进程内假服务，其余代码全部走真实实现。

    只替换**网络出口**，不替换 `WeComClient` / `resolve_userid` / 授权判定 ——
    这条链路（换 token → 换 userid → 剥 CorpId 前缀 → 判定授权 → 下发会话）
    就是要端到端验证的部分。返回请求路径列表，供断言调用顺序。
    """
    import httpx

    from app.connectors.wecom import client as wecom_client

    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == wecom_client.TOKEN_PATH:
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "T", "expires_in": 7200}
            )
        if request.url.path == wecom_client.LOGIN_USERINFO_PATH:
            # 互联企业的 userid 带 CorpId/ 前缀，本地通讯录存裸 userid
            return httpx.Response(200, json={"errcode": 0, "userid": f"ww-corp/{userid}"})
        return httpx.Response(404, json={"errcode": 404, "errmsg": "unexpected path"})

    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(wecom_client.httpx, "Client", factory)
    return paths


def test_login_end_to_end_from_start_link_to_authenticated_session(
    login_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：从 /start 给出的链接出发，按企微的方式回跳，落到可用会话。

    这条用例刻意**从 start 链接里读 redirect_uri 再模拟回跳**，而不是把回调路径
    写死在测试里 —— 旧用例（把完整地址写进配置再断言它出现在链接中）结构上
    不可能发现「配置只填域名 → 企微回跳到站点根 → code 丢失」这类断点，
    而那正是 2026-09-18 线上全员登不进来的原因。

    链路：/start → 企微（假 HTTP）→ redirect_uri → /callback → 会话 Cookie →
    /api/auth/me → 写操作放行。除浏览器与企微服务器外无简化。
    """
    company = _dept(db_session, "1", "西就国际", login_enabled=True, login_role=LoginRole.ADMIN)
    _employee(db_session, "example_user", "游丰奕", department=company)
    db_session.commit()

    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})
    # 复现线上配置：回调地址只填了域名（照抄企微后台的「可信域名」）
    login_client.patch(
        "/api/access/settings", json={"redirect_uri": "https://zhoubao.example.com"}
    )
    login_client.post("/api/auth/logout")

    paths = _fake_wecom_transport(monkeypatch, "example_user")

    start = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    assert start.status_code == 302
    from urllib.parse import parse_qs, urlsplit

    wecom_query = parse_qs(urlsplit(start.headers["location"]).query)
    redirect_uri = wecom_query["redirect_uri"][0]
    state = wecom_query["state"][0]

    # ① 回跳地址必须落在回调接口上，而不是站点根（根路径会丢掉 ?code）
    assert urlsplit(redirect_uri).path == "/api/auth/wecom/callback", redirect_uri

    # ② 模拟企微回跳：code + state 附在同一个 redirect_uri 上，Cookie 仍在本机
    callback = login_client.get(
        f"{urlsplit(redirect_uri).path}?code=CODE&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 200
    assert "登录成功，正在进入系统" in callback.text
    assert "/dashboard" in callback.text
    assert login_client.cookies.get(SESSION_COOKIE)

    # ③ 换身份确实走了真实客户端（token → getuserinfo）
    assert paths == ["/cgi-bin/gettoken", "/cgi-bin/auth/getuserinfo"]

    # ④ 会话可用，且身份与角色都取自已授权的部门
    me = login_client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    assert me["employee_id"] is not None
    assert me["source"] == LoginSource.WECOM
    assert me["role"] == LoginRole.ADMIN

    # ⑤ 授权是 admin，写操作放行（只读成员会被中间件 403，见另一条用例）
    resp = login_client.patch(
        f"/api/access/departments/{company.id}", json={"enabled": True}
    )
    assert resp.status_code == 200


def test_start_repairs_bare_domain_already_stored_in_runtime_config(
    login_client: TestClient, db_session: Session
) -> None:
    """库里已经是裸域名（修复前存的）时，发起登录也必须能拼出正确回调地址。

    这条与「保存时归一化」不是同一件事，且更贴近线上：线上那条坏配置是**修复前**
    写进 system_settings 的，保存路径的校验对它无效。只有发起登录时再归一化一次，
    已有部署才能不重新保存设置就恢复正常 —— 否则「升级了但没人重新点保存」
    这套故障会一直挂着。
    """
    from app.services.runtime_config_service import (
        invalidate_runtime_settings_cache,
        update_runtime_values,
    )

    db_session.commit()
    # 绕过 API，直接把裸域名写进运行时配置：模拟修复前的存量数据
    update_runtime_values(
        db_session, {"wecom_login_redirect_uri": "https://zhoubao.example.com"}
    )
    db_session.commit()
    invalidate_runtime_settings_cache()

    resp = login_client.get("/api/auth/wecom/start", follow_redirects=False)
    assert resp.status_code == 302
    assert (
        "https%3A%2F%2Fzhoubao.example.com%2Fapi%2Fauth%2Fwecom%2Fcallback"
        in resp.headers["location"]
    )


def test_wecom_login_settings_normalizes_and_validates_redirect_uri(
    login_client: TestClient, db_session: Session
) -> None:
    """保存时就归一化并校验：存进去的必须是完整的回调 URL。"""
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})

    saved = login_client.patch(
        "/api/access/settings", json={"redirect_uri": "https://zhoubao.example.com"}
    )
    assert saved.status_code == 200
    assert saved.json()["redirect_uri"] == (
        "https://zhoubao.example.com/api/auth/wecom/callback"
    )

    # 明显不能作为回调地址的值直接拒绝，不写入（避免存进去之后才在扫码时炸）
    for bad in ("not-a-url", "javascript:alert(1)", "//zhoubao.example.com"):
        resp = login_client.patch("/api/access/settings", json={"redirect_uri": bad})
        assert resp.status_code == 400, bad


def test_callback_url_normalization_rules() -> None:
    """归一化规则：只补路径，不猜协议、不接受相对地址。"""
    from app.connectors.wecom.login import normalize_callback_url

    callback = "https://zhoubao.example.com/api/auth/wecom/callback"
    assert normalize_callback_url("https://zhoubao.example.com") == callback
    assert normalize_callback_url("https://zhoubao.example.com/") == callback
    assert normalize_callback_url("https://zhoubao.example.com:8443") == (
        "https://zhoubao.example.com:8443/api/auth/wecom/callback"
    )
    assert normalize_callback_url("  " + callback + "  ") == callback
    # 自定义路径原样保留（企微只校验域名，路径可以自选）
    assert normalize_callback_url("https://zhoubao.example.com/custom/cb") == (
        "https://zhoubao.example.com/custom/cb"
    )
    # 带 query/fragment 的粘贴值不能原样下发：企微回跳时会自己再拼 ?code=...
    assert normalize_callback_url(callback + "?x=1#y") == callback
    # 空值 = 未配置（由调用方按访问域名推导）
    assert normalize_callback_url("") == ""
    # 非法值不给「看起来能用」的结果
    assert normalize_callback_url("zhoubao.example.com") == ""
    assert normalize_callback_url("javascript:alert(1)") == ""


def test_access_overview_reports_effective_callback_url(
    login_client: TestClient, db_session: Session
) -> None:
    """设置页要能显示「实际会用哪个地址」，避免配置与生效值各说一套。"""
    db_session.commit()
    login_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})
    login_client.patch(
        "/api/access/settings", json={"redirect_uri": "https://zhoubao.example.com"}
    )
    overview = login_client.get("/api/access/overview").json()
    assert overview["effective_callback_url"] == (
        "https://zhoubao.example.com/api/auth/wecom/callback"
    )
    assert overview["callback_path"] == "/api/auth/wecom/callback"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _state_from_location(location: str) -> str:
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(location).query)
    return query["state"][0]


def _login_via_callback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, userid: str
) -> None:
    monkeypatch.setattr(
        "app.api.routes.auth.resolve_userid",
        lambda c, code: {"userid": userid},
    )
    start = client.get("/api/auth/wecom/start", follow_redirects=False)
    state = _state_from_location(start.headers["location"])
    client.get(
        f"/api/auth/wecom/callback?code=CODE&state={state}", follow_redirects=False
    )


def _login_as_member(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str,
) -> str:
    """以企微身份登录一个已授权员工；已有组织数据时只补齐员工。"""
    dept = session.scalar(select(Department).order_by(Department.id))
    if dept is None:
        dept = _dept(session, "1", "技术部", login_enabled=True, login_role=role)
    else:
        dept.login_enabled = True
        dept.login_role = role
    employee = access_service.find_employee_by_userid(session, "zhangsan")
    if employee is None:
        _employee(session, "zhangsan", "张三", department=dept)
    else:
        employee.department_id = dept.id
        employee.status = EmployeeStatus.ACTIVE
        employee.login_enabled = False
    session.commit()
    _login_via_callback(client, monkeypatch, "zhangsan")
    return "zhangsan"
