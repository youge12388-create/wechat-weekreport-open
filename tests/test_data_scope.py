"""部门管理员（dept_admin）数据范围隔离。

覆盖验收标准的骨架：
1. dept_admin 只能看到授权部门及其子部门的周报 / 员工 / 部门 / 看板 / 导出；
2. 公司级能力（系统设置 / 登录授权 / 经营看板 / 全员周期总结 / 消息中心）
   一律 403；
3. 撤销勾选后下一次请求立即生效（回源复核）；
4. 密码超管行为不变（全公司）。

中间件开启（APP_ENV=dev），与 test_wecom_login 的 login_client 同一做法。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from app.main import create_app
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import (
    DepartmentStatus,
    EmployeeStatus,
    LoginRole,
)
from app.models.analysis_queue import AnalysisQueue
from app.models.sync_job import SyncJob
from app.models.weekly_report import WeeklyReport

PERIOD = (date(2026, 8, 31), date(2026, 9, 6))


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
    userid: str,
    name: str,
    department: Department | None = None,
) -> Employee:
    employee = Employee(
        source="wecom",
        external_id=userid,
        name=name,
        status=EmployeeStatus.ACTIVE,
        department_id=department.id if department else None,
    )
    session.add(employee)
    session.flush()
    return employee


def _report(session: Session, employee: Employee, title: str) -> WeeklyReport:
    report = WeeklyReport(
        employee_id=employee.id,
        external_id=f"rep_{title}",
        report_type="weekly",
        title=title,
        content=f"{title}的正文。",
        content_raw=f"{title}的正文。",
        start_date=PERIOD[0],
        end_date=PERIOD[1],
        submitted_at=datetime(2026, 9, 4, 18, 0),
        source="wecom",
    )
    session.add(report)
    session.flush()
    return report


@pytest.fixture
def org(db_session: Session) -> dict[str, object]:
    """技术部（dept_admin 授权，含子部门研发一组） + 市场部（未授权）。

    leader 挂技术部；张三在技术部、李四在研发一组、王五在市场部，
    三人各有一篇同一周期的周报。
    """
    dept_a = _dept(db_session, "10", "技术部", login_enabled=True, login_role=LoginRole.DEPT_ADMIN)
    dept_a1 = _dept(db_session, "11", "研发一组", parent=dept_a)
    dept_b = _dept(db_session, "20", "市场部")

    leader = _employee(db_session, "leader", "赵领导", dept_a)
    zhang = _employee(db_session, "zhangsan", "张三", dept_a)
    li = _employee(db_session, "lisi", "李四", dept_a1)
    wang = _employee(db_session, "wangwu", "王五", dept_b)

    r1 = _report(db_session, zhang, "技术部张三的周报")
    r2 = _report(db_session, li, "研发一组李四的周报")
    r3 = _report(db_session, wang, "市场部王五的周报")
    db_session.commit()
    return {
        "dept_a": dept_a,
        "dept_a1": dept_a1,
        "dept_b": dept_b,
        "leader": leader,
        "r1": r1,
        "r2": r2,
        "r3": r3,
    }


@pytest.fixture
def auth_client(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> Iterator[TestClient]:
    """开启鉴权中间件的客户端，复用 db_session 的内存库。"""
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-strong-admin-password")
    monkeypatch.setenv("SESSION_SECRET", "k" * 48)
    monkeypatch.setenv("WECOM_AGENT_ID", "1000002")
    monkeypatch.setenv("WECOM_LOGIN_ENABLED", "true")
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": ()}
    )
    get_settings.cache_clear()
    engine = get_engine(get_settings())
    Base.metadata.create_all(engine)
    from app.main import create_app

    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()
        dispose_engine()


def _login_leader(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """leader 走一遍企微回调登录（部门授权已给 dept_admin）。"""
    monkeypatch.setattr(
        "app.api.routes.auth.resolve_userid",
        lambda c, code: {"userid": "leader"},
    )
    start = client.get("/api/auth/wecom/start", follow_redirects=False)
    assert start.status_code == 302
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    resp = client.get(
        f"/api/auth/wecom/callback?code=CODE&state={state}", follow_redirects=False
    )
    assert resp.status_code == 200


def test_dept_admin_reports_scoped_to_subtree(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_leader(auth_client, db_session, monkeypatch)

    resp = auth_client.get(
        "/api/reports", params={"start": "2026-08-31", "end": "2026-09-06"}
    )
    assert resp.status_code == 200
    titles = {item["title"] for item in resp.json()["items"]}
    assert "技术部张三的周报" in titles
    assert "研发一组李四的周报" in titles
    assert "市场部王五的周报" not in titles


def test_dept_admin_report_detail_outside_scope_is_404(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_leader(auth_client, db_session, monkeypatch)

    inside = auth_client.get(f"/api/reports/{org['r1'].id}")
    assert inside.status_code == 200
    outside = auth_client.get(f"/api/reports/{org['r3'].id}")
    # 范围外必须 404 而不是 403：不向越权者泄露「这篇周报存在」
    assert outside.status_code == 404


def test_dept_admin_agent_rejects_department_outside_scope(
    auth_client: TestClient,
    db_session: Session,
    org: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式部门筛选不能绕过 dept_admin 的管辖范围。"""
    _login_leader(auth_client, db_session, monkeypatch)

    response = auth_client.post(
        "/api/ai/agent",
        json={
            "question": "本期做了什么？",
            "start": "2026-09-07",
            "end": "2026-09-13",
            "department_id": org["dept_b"].id,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "指定部门不存在或无权访问"


def test_dept_admin_employees_and_departments_scoped(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_leader(auth_client, db_session, monkeypatch)

    employees = auth_client.get("/api/employees").json()
    names = {item["name"] for item in employees["items"]}
    assert "王五" not in names
    assert "张三" in names

    departments = auth_client.get("/api/departments").json()
    dept_names = {item["name"] for item in departments}
    assert dept_names == {"技术部", "研发一组"}


def test_dept_admin_dashboard_scoped(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_leader(auth_client, db_session, monkeypatch)

    overview = auth_client.get("/api/dashboard/overview").json()
    # 应提交基数 = 管辖子树内 3 人（赵领导/张三/李四），不含市场部王五
    assert overview["employee_total"] == 3


def test_dept_admin_company_level_endpoints_403(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_leader(auth_client, db_session, monkeypatch)

    for path, method, payload in [
        ("/api/settings/ai", "GET", None),
        ("/api/access/tree", "GET", None),
        ("/api/dashboard/period-summary", "POST", {}),
        ("/api/notifications/feed", "GET", None),
    ]:
        resp = auth_client.request(method, path, json=payload)
        assert resp.status_code == 403, f"{method} {path} 应仅超管可用"


def test_dept_admin_cannot_start_company_sync(
    auth_client: TestClient,
    db_session: Session,
    org: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步写入口仅可写管理员可用；部门管理员被拒绝时不能留下任务。"""
    _login_leader(auth_client, db_session, monkeypatch)

    existing_ids = set(db_session.scalars(select(SyncJob.id)))
    db_session.commit()
    for path in (
        "/api/sync/wecom/departments",
        "/api/sync/wecom/employees",
        "/api/sync/wecom/all",
        "/api/sync/wecom/reports/queue",
        "/api/sync/wecom/reports",
    ):
        response = auth_client.post(path, json={})
        assert response.status_code == 403, path

    assert set(db_session.scalars(select(SyncJob.id))) == existing_ids
    db_session.commit()

    # 密码超管仍可使用后台同步入口，且任务只入队、不在请求里执行。
    auth_client.post("/api/auth/logout")
    login = auth_client.post(
        "/api/auth/login", json={"password": "a-strong-admin-password"}
    )
    assert login.status_code == 200
    queued = auth_client.post("/api/sync/wecom/all", json={})
    assert queued.status_code == 200
    assert len(queued.json()["jobs"]) == 3


def test_dept_admin_duplicate_compat_route_hides_out_of_scope_reports(
    auth_client: TestClient,
    db_session: Session,
    org: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容分析入口遵守周报范围，范围外与不存在的 ID 都返回 404。"""
    _login_leader(auth_client, db_session, monkeypatch)

    inside = auth_client.post(
        "/api/ai/duplicate/check", json={"report_id": org["r1"].id}
    )
    assert inside.status_code == 200

    queued_ids = set(db_session.scalars(select(AnalysisQueue.id)))
    db_session.commit()
    outside = auth_client.post(
        "/api/ai/duplicate/check", json={"report_id": org["r3"].id}
    )
    missing = auth_client.post(
        "/api/ai/duplicate/check", json={"report_id": 999999}
    )
    outside_analysis = auth_client.post(
        f"/api/ai/reports/{org['r3'].id}/analyze"
    )
    assert outside.status_code == 404
    assert missing.status_code == 404
    assert outside_analysis.status_code == 404
    assert set(db_session.scalars(select(AnalysisQueue.id))) == queued_ids
    db_session.commit()


def test_dept_admin_export_scoped(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_leader(auth_client, db_session, monkeypatch)

    resp = auth_client.get(
        "/api/exports/reports", params={"start": "2026-08-31", "end": "2026-09-06"}
    )
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    assert "技术部张三的周报" in text
    assert "市场部王五的周报" not in text


def test_revoke_grant_takes_effect_immediately(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_leader(auth_client, db_session, monkeypatch)
    assert auth_client.get("/api/reports").status_code == 200

    # 撤销授权：最近显式设置变为「禁止」，下一次请求立即失效
    dept_a: Department = org["dept_a"]
    dept_a.login_enabled = False
    db_session.commit()

    resp = auth_client.get("/api/reports")
    assert resp.status_code == 401
    assert resp.json()["error"] == "AccessRevoked"


def test_password_admin_still_sees_everything(
    auth_client: TestClient, db_session: Session, org: dict
) -> None:
    resp = auth_client.post("/api/auth/login", json={"password": "a-strong-admin-password"})
    assert resp.status_code == 200

    reports = auth_client.get(
        "/api/reports", params={"start": "2026-08-31", "end": "2026-09-06"}
    ).json()
    titles = {item["title"] for item in reports["items"]}
    assert titles == {"技术部张三的周报", "研发一组李四的周报", "市场部王五的周报"}

    # 公司级能力依旧可用
    assert auth_client.get("/api/settings/ai").status_code == 200
    assert auth_client.get("/api/access/tree").status_code == 200


def test_employee_level_dept_admin_grant_scopes_to_own_department(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """个人级勾选为 dept_admin 的跨部门领导：管辖自己所在部门的子树。"""
    dept_a: Department = org["dept_a"]
    dept_a.login_enabled = None  # 关掉部门级授权，改用个人级
    leader: Employee = org["leader"]
    leader.login_enabled = True
    leader.login_role = LoginRole.DEPT_ADMIN
    db_session.commit()

    _login_leader(auth_client, db_session, monkeypatch)

    titles = {
        item["title"]
        for item in auth_client.get(
            "/api/reports", params={"start": "2026-08-31", "end": "2026-09-06"}
        ).json()["items"]
    }
    assert "技术部张三的周报" in titles
    assert "市场部王五的周报" not in titles


def test_unscoped_subtree_sees_nothing(
    auth_client: TestClient, db_session: Session, org: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """挂在无部门员工上的 dept_admin：范围为空，宁可看不到也不能看全公司。"""
    leader: Employee = org["leader"]
    leader.department_id = None
    leader.login_enabled = True
    leader.login_role = LoginRole.DEPT_ADMIN
    db_session.commit()

    _login_leader(auth_client, db_session, monkeypatch)

    resp = auth_client.get(
        "/api/reports", params={"start": "2026-08-31", "end": "2026-09-06"}
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
