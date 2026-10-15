"""AI 治理底座 + 批量体检测试（P0/P1）。

覆盖：token 用量落库、AI 状态自检、用量账本、批量体检（回写/幂等/容错）、AI 标记筛选。
全部走 FakeProvider，不联网、不花钱。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.ai.base import LLMError, LLMResult
from app.ai.fake import FakeProvider
from app.api import deps
from app.main import create_app
from app.models.ai_analysis import AIAnalysis
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.weekly_report import WeeklyReport
from app.services import (
    ai_scan_service,
    ai_search_service,
    duplicate_service,
    quality_service,
)

PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)


class TokenFakeProvider(FakeProvider):
    """带 usage 的替身：真实厂商每次调用都会返回 token 消耗。"""

    def chat(self, system: str, user: str) -> LLMResult:
        self.calls.append({"system": system, "user": user})
        return LLMResult(
            content=self.response,
            model=self.model,
            prompt_tokens=100,
            completion_tokens=20,
        )


def _make_report(
    session: Session, title: str, content: str, external_id: str | None = None
) -> WeeklyReport:
    employee = Employee(
        source="wecom",
        external_id=external_id or f"emp_{title}",
        name="张三",
        status=EmployeeStatus.ACTIVE,
    )
    session.add(employee)
    session.flush()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id=f"rep_{title}",
        report_type="weekly",
        title=title,
        content=content,
        content_raw=content,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.commit()
    return report


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


def test_search_logs_token_usage(db_session: Session, monkeypatch) -> None:
    """检索成功后 token 用量必须落库 —— 这是成本账本的数据源头。"""
    fake = TokenFakeProvider(
        response=json.dumps({"answer": "ok", "sources": [], "not_found": False})
    )
    monkeypatch.setattr(ai_search_service, "get_provider", lambda settings: fake)
    _make_report(db_session, "周报A", "完成支付联调")

    ai_search_service.search_reports(
        db_session, question="本周做了什么", start="2026-09-07", end="2026-09-13"
    )

    row = db_session.query(AIAnalysis).order_by(AIAnalysis.id.desc()).first()
    assert row is not None
    assert row.prompt_tokens == 100
    assert row.completion_tokens == 20


def test_status_endpoint(client: TestClient) -> None:
    """AI 状态自检：前端靠它决定要不要展示「去配置 Key」的引导。"""
    resp = client.get("/api/ai/status")
    assert resp.status_code == 200
    body = resp.json()
    assert {"configured", "provider", "model", "hint"} <= set(body)


def test_usage_endpoint_counts_tokens(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """用量账本：调用数与 token 数能对上，账本才可核对。"""
    fake = TokenFakeProvider(
        response=json.dumps({"answer": "ok", "sources": [], "not_found": False})
    )
    monkeypatch.setattr(ai_search_service, "get_provider", lambda settings: fake)
    _make_report(db_session, "周报B", "完成客诉处理")
    assert (
        client.post(
            "/api/ai/search",
            json={
                "question": "谁处理了客诉",
                "start": "2026-09-07",
                "end": "2026-09-13",
            },
        ).status_code
        == 200
    )

    body = client.get("/api/ai/usage").json()
    assert body["total_calls"] >= 1
    assert body["success"] >= 1
    assert body["prompt_tokens"] >= 100
    assert body["total_tokens"] == body["prompt_tokens"] + body["completion_tokens"]
    assert body["by_type"]


def test_scan_writes_back_and_is_idempotent(
    db_session: Session, monkeypatch
) -> None:
    """批量体检：结论回写周报表；重复调用不重复花钱（幂等）。"""
    fake = TokenFakeProvider(
        response=json.dumps(
            {"is_duplicate": False, "score": 10, "reason": "ok", "is_thin": False}
        )
    )
    monkeypatch.setattr(duplicate_service, "get_provider", lambda settings: fake)
    monkeypatch.setattr(quality_service, "get_provider", lambda settings: fake)

    _make_report(db_session, "周报C", "完成A事项")
    _make_report(db_session, "周报D", "完成B事项", external_id="emp_d")

    result = ai_scan_service.scan_reports(
        db_session, start="2026-09-07", end="2026-09-13"
    )
    assert result["scanned"] == 2
    assert result["remaining"] == 0

    checked = (
        db_session.query(WeeklyReport)
        .filter(WeeklyReport.ai_checked_at.isnot(None))
        .count()
    )
    assert checked == 2

    calls_before = len(fake.calls)
    result2 = ai_scan_service.scan_reports(
        db_session, start="2026-09-07", end="2026-09-13"
    )
    assert result2["scanned"] == 0
    assert len(fake.calls) == calls_before  # 幂等：一篇都没重跑


def test_scan_survives_single_failure(db_session: Session, monkeypatch) -> None:
    """单篇抽风不能拖垮整批：失败的记入 failed，其余照常出结论。"""

    class FlakyProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__(
                response=json.dumps({"is_thin": False, "score": 50, "reason": "ok"})
            )
            self.n = 0

        def chat(self, system: str, user: str) -> LLMResult:
            self.n += 1
            if self.n == 1:
                raise LLMError("第一次调用抽风")
            return super().chat(system, user)

    flaky = FlakyProvider()
    monkeypatch.setattr(duplicate_service, "get_provider", lambda settings: flaky)
    monkeypatch.setattr(quality_service, "get_provider", lambda settings: flaky)

    _make_report(db_session, "周报E", "完成C事项")
    _make_report(db_session, "周报F", "完成D事项", external_id="emp_f")

    result = ai_scan_service.scan_reports(
        db_session, start="2026-09-07", end="2026-09-13"
    )
    assert result["scanned"] == 1  # 第一篇失败，第二篇成功
    assert result["failed_count"] == 1


def test_reports_ai_flag_filter(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """AI 标记必须能从列表筛出来，否则标记了也白标记。"""
    fake = TokenFakeProvider(
        response=json.dumps(
            {"is_duplicate": False, "score": 10, "reason": "ok", "is_thin": False}
        )
    )
    monkeypatch.setattr(duplicate_service, "get_provider", lambda settings: fake)
    monkeypatch.setattr(quality_service, "get_provider", lambda settings: fake)

    report_g = _make_report(db_session, "周报G", "完成E事项")
    ai_scan_service.scan_reports(db_session, start="2026-09-07", end="2026-09-13")
    _make_report(db_session, "周报H", "完成F事项", external_id="emp_h")  # 未体检

    params = {"start": "2026-09-07", "end": "2026-09-13"}
    normal = client.get("/api/reports", params={**params, "ai_flag": "normal"}).json()
    unchecked = client.get(
        "/api/reports", params={**params, "ai_flag": "unchecked"}
    ).json()
    bogus = client.get("/api/reports", params={**params, "ai_flag": "bogus"})

    assert [it["id"] for it in normal["items"]] == [report_g.id]
    assert normal["items"][0]["quality"]["is_thin"] is False
    assert any(it["id"] != report_g.id for it in unchecked["items"])
    assert bogus.status_code == 400
