"""Agent / RAG 聊天记录按人隔离。

验收标准：
1. 每个人（含超管）在 /api/ai/history 只能看到自己的会话；
2. 密码超管只看到 owner 为 NULL 的会话（自己的 + 迁移前的历史记录）；
3. 用户 B 用用户 A 的 history_id 调可见性接口 → 404；
4. 会话上下文回放按归属过滤（B 复用 A 的 conversation_id 灌不进 A 的问答）；
5. 企微会话发起的检索写入 owner 归属。

端到端用 FakeProvider 替身，测试不联网、不花钱。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.fake import FakeProvider
from app.api import deps
from app.core.config import Settings, get_settings
from app.core.security import SESSION_COOKIE, create_session_token
from app.db.base import Base
from app.db.session import dispose_engine, get_engine
from app.main import create_app
from app.models.ai_analysis import AIAnalysis
from app.models.agent_feedback import AgentFeedback
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import (
    AIAnalysisStatus,
    DepartmentStatus,
    EmployeeStatus,
    LoginRole,
)
from app.services import ai_search_service, agent_service
from app.services.data_scope import DataScope, SCOPE_INFO_KEY

ANSWER = '{"answer": "已根据材料作答。", "sources": [], "not_found": false}'


@dataclass
class _Settings:
    ai_provider: str = "fake"
    ai_model: str = "fake-model"
    ai_search_max_reports: int = 30
    ai_max_input_chars: int = 12000


def _patch_ai(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    settings = _Settings()
    monkeypatch.setattr(ai_search_service, "runtime_settings", lambda _s: settings)
    monkeypatch.setattr(ai_search_service, "get_provider", lambda _s: FakeProvider(response=ANSWER))
    return FakeProvider(response=ANSWER)


def _wecom_employee(
    session: Session, userid: str, name: str, department: Department | None
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


def _chat_row(
    session: Session,
    *,
    conversation_id: str,
    owner_employee_id: int | None,
    question: str,
    analysis_type: str = "agent_search",
) -> AIAnalysis:
    row = AIAnalysis(
        entity_type="search_query",
        entity_id=0,
        analysis_type=analysis_type,
        conversation_id=conversation_id,
        owner_employee_id=owner_employee_id,
        result_json={"question": question, "answer": f"关于{question}的回答"},
        status=AIAnalysisStatus.SUCCESS,
        created_at=datetime(2026, 9, 25, 10, 0),
    )
    session.add(row)
    session.commit()
    return row


def _cookie_for(employee: Employee, role: str = LoginRole.DEPT_ADMIN) -> dict[str, str]:
    token = create_session_token(
        subject=employee.external_id,
        role=role,
        source="wecom",
        name=employee.name,
        employee_id=employee.id,
    )
    return {SESSION_COOKIE: token}


@pytest.fixture
def people(db_session: Session) -> dict[str, Employee]:
    dept_a = Department(
        external_id="10",
        name="技术部",
        source="wecom",
        status=DepartmentStatus.ACTIVE,
        login_enabled=True,
        login_role=LoginRole.DEPT_ADMIN,
    )
    db_session.add(dept_a)
    db_session.flush()
    alice = _wecom_employee(db_session, "alice", "甲", dept_a)
    bob = _wecom_employee(db_session, "bob", "乙", dept_a)
    db_session.commit()
    return {"alice": alice, "bob": bob}


@pytest.fixture
def auth_client(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> Iterator[TestClient]:
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


def test_history_isolated_per_user(
    db_session: Session, people: dict[str, Employee]
) -> None:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[deps.get_db] = _override  # type: ignore[arg-type]
    try:
        client = TestClient(app)
        alice, bob = people["alice"], people["bob"]
        row_a = _chat_row(db_session, conversation_id="conv-a", owner_employee_id=alice.id, question="甲的问题")
        row_b = _chat_row(db_session, conversation_id="conv-b", owner_employee_id=bob.id, question="乙的问题")
        _chat_row(db_session, conversation_id=None, owner_employee_id=None, question="超管的旧问题")

        # 甲：只看到自己的会话
        seen_a = client.get("/api/ai/history", cookies=_cookie_for(alice)).json()
        questions = {turn["question"] for item in seen_a for turn in item["turns"]}
        assert questions == {"甲的问题"}

        # 乙：看不到甲的
        seen_b = client.get("/api/ai/history", cookies=_cookie_for(bob)).json()
        questions_b = {turn["question"] for item in seen_b for turn in item["turns"]}
        assert questions_b == {"乙的问题"}

        # 密码超管（employee_id=None）：只看 owner 为 NULL 的历史记录
        admin_cookie = {SESSION_COOKIE: create_session_token()}
        seen_admin = client.get("/api/ai/history", cookies=admin_cookie).json()
        questions_admin = {turn["question"] for item in seen_admin for turn in item["turns"]}
        assert questions_admin == {"超管的旧问题"}

        # 乙拿甲的记录做软删除 → 404
        resp = client.patch(
            f"/api/ai/history/{row_a.id}", json={"deleted": True}, cookies=_cookie_for(bob)
        )
        assert resp.status_code == 404

        # 本人可以软删除自己的
        resp = client.patch(
            f"/api/ai/history/{row_b.id}", json={"deleted": True}, cookies=_cookie_for(bob)
        )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_conversation_context_respects_owner(db_session: Session, people: dict) -> None:
    """B 复用 A 的 conversation_id 时，回放不到 A 的历史轮次。"""
    alice, bob = people["alice"], people["bob"]
    for i in range(2):
        _chat_row(
            db_session,
            conversation_id="conv-a",
            owner_employee_id=alice.id,
            question=f"甲的第{i}问",
        )

    mine = agent_service._conversation_messages(
        db_session, "conv-a", owner_employee_id=alice.id
    )
    assert len(mine) == 4  # 2 轮 × (user + assistant)

    theirs = agent_service._conversation_messages(
        db_session, "conv-a", owner_employee_id=bob.id
    )
    assert theirs == []


def test_shared_conversation_id_soft_delete_is_owner_scoped(
    db_session: Session, people: dict[str, Employee]
) -> None:
    """同一个 conversation_id 跨用户复用时，批量软删除仍按归属人隔离。"""
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[deps.get_db] = _override  # type: ignore[arg-type]
    try:
        client = TestClient(app)
        alice, bob = people["alice"], people["bob"]
        row_a = _chat_row(
            db_session, conversation_id="same-id", owner_employee_id=alice.id,
            question="甲的问题",
        )
        row_b = _chat_row(
            db_session, conversation_id="same-id", owner_employee_id=bob.id,
            question="乙的问题",
        )

        response = client.patch(
            f"/api/ai/history/{row_a.id}",
            json={"deleted": True},
            cookies=_cookie_for(alice),
        )
        assert response.status_code == 200
        db_session.refresh(row_b)
        assert row_b.deleted_at is None
        assert client.get("/api/ai/history", cookies=_cookie_for(bob)).json()[0]["id"] == row_b.id
    finally:
        app.dependency_overrides.clear()


def test_scope_downgrade_hides_unsafe_history_and_replay(
    db_session: Session, people: dict[str, Employee]
) -> None:
    """失去全公司权限后，旧全域答案不再展示、回放或允许批量软删除。"""
    alice = people["alice"]
    department_id = alice.department_id
    assert department_id is not None
    safe = _chat_row(
        db_session, conversation_id="downgraded", owner_employee_id=alice.id,
        question="安全范围内的问题",
    )
    safe.result_json = {
        **safe.result_json,
        "department_id": department_id,
        "access_scope": {"kind": "departments", "department_ids": [department_id]},
    }
    unsafe = _chat_row(
        db_session, conversation_id="downgraded", owner_employee_id=alice.id,
        question="全公司范围的旧问题",
    )
    unsafe.result_json = {
        **unsafe.result_json,
        "access_scope": {"kind": "all"},
    }
    db_session.commit()
    db_session.info[SCOPE_INFO_KEY] = DataScope("subtree", (department_id,))

    replay = agent_service._conversation_messages(
        db_session, "downgraded", owner_employee_id=alice.id,
        department_id=department_id,
    )
    assert [message["content"] for message in replay] == [
        "安全范围内的问题",
        "关于安全范围内的问题的回答",
    ]

    rag_safe = _chat_row(
        db_session, conversation_id="rag-downgraded", owner_employee_id=alice.id,
        question="RAG 安全范围内的问题", analysis_type="search",
    )
    rag_safe.result_json = {
        **rag_safe.result_json,
        "access_scope": {"kind": "departments", "department_ids": [department_id]},
    }
    _chat_row(
        db_session, conversation_id="rag-downgraded", owner_employee_id=alice.id,
        question="RAG 全公司旧问题", analysis_type="search",
    ).result_json = {"access_scope": {"kind": "all"}}
    db_session.commit()
    rag_replay = ai_search_service._conversation_context(
        db_session, "rag-downgraded", owner_employee_id=alice.id
    )
    assert [turn["question"] for turn in rag_replay] == ["RAG 安全范围内的问题"]

    app = create_app()

    def _override() -> Iterator[Session]:
        db_session.info[SCOPE_INFO_KEY] = DataScope("subtree", (department_id,))
        yield db_session

    app.dependency_overrides[deps.get_db] = _override  # type: ignore[arg-type]
    try:
        client = TestClient(app)
        cookies = _cookie_for(alice)
        history = client.get("/api/ai/history", cookies=cookies)
        assert history.status_code == 200
        assert history.json() == []  # 任一不安全轮次会隐藏整个会话
        deleted = client.patch(
            f"/api/ai/history/{safe.id}", json={"deleted": True}, cookies=cookies
        )
        assert deleted.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_feedback_is_private_to_owner_and_linked_only_to_audit(
    db_session: Session, people: dict[str, Employee]
) -> None:
    alice, bob = people["alice"], people["bob"]
    row = _chat_row(
        db_session, conversation_id="feedback", owner_employee_id=alice.id,
        question="这个回答准确吗？",
    )
    row.result_json = {
        **row.result_json,
        "access_scope": {"kind": "departments", "department_ids": [alice.department_id]},
    }
    db_session.commit()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[deps.get_db] = _override  # type: ignore[arg-type]
    try:
        client = TestClient(app)
        alice_cookies = _cookie_for(alice)
        bob_cookies = _cookie_for(bob)
        db_session.info[SCOPE_INFO_KEY] = DataScope("subtree", (alice.department_id,))
        submitted = client.post(
            f"/api/ai/history/{row.id}/feedback",
            json={"category": "evidence", "reason": "缺少原文引用"},
            cookies=alice_cookies,
        )
        assert submitted.status_code == 200
        assert submitted.json()["analysis_id"] == row.id
        assert "answer" not in submitted.json()
        assert client.get(
            f"/api/ai/history/{row.id}/feedback", cookies=alice_cookies
        ).json()["category"] == "evidence"
        denied = client.get(
            f"/api/ai/history/{row.id}/feedback", cookies=bob_cookies
        )
        assert denied.status_code == 404
        assert db_session.query(AgentFeedback).count() == 1
    finally:
        app.dependency_overrides.clear()


def test_search_logs_owner_and_isolates_history(
    auth_client: TestClient,
    db_session: Session,
    people: dict[str, Employee],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """企微会话发起检索：聊天行写入归属人，且历史按人可见。"""
    _patch_ai(monkeypatch)

    # dept_admin 范围解析依赖 get_db 注入；这里直连端到端客户端（带中间件）
    alice, bob = people["alice"], people["bob"]

    def _login(userid: str) -> None:
        monkeypatch.setattr(
            "app.api.routes.auth.resolve_userid",
            lambda c, code: {"userid": userid},
        )
        start = auth_client.get("/api/auth/wecom/start", follow_redirects=False)
        from urllib.parse import parse_qs, urlparse

        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        resp = auth_client.get(
            f"/api/auth/wecom/callback?code=CODE&state={state}", follow_redirects=False
        )
        assert resp.status_code == 200

    _login("alice")
    resp = auth_client.post("/api/ai/search", json={"question": "甲在问什么？"})
    assert resp.status_code == 200

    rows = db_session.scalars(
        select(AIAnalysis).where(AIAnalysis.analysis_type == "search")
    ).all()
    assert len(rows) == 1
    assert rows[0].owner_employee_id == alice.id
    # 立即释放连接：auth_client 拆卸时会 dispose 引擎，
    # 这里若挂着未结束的事务，db_session 拆卸时的 rollback 会打在已关闭的连接上
    db_session.commit()

    # 甲能看到；乙登录后看不到甲的
    seen_a = auth_client.get("/api/ai/history").json()
    assert len(seen_a) == 1

    auth_client.cookies.clear()
    _login("bob")
    seen_b = auth_client.get("/api/ai/history").json()
    assert seen_b == []
