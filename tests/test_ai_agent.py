"""周报 Agent：范围锁定、工具循环、步数上限、失败降级。

全部走 FakeProvider 脚本，不联网、不花钱；关键断言都落在「模型实际收到了什么」
和「回给前端的步骤/来源」上，而不是实现细节。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.base import LLMError, ToolCall
from app.ai.fake import FakeProvider
from app.api import deps
from app.api.routes import ai as ai_routes
from app.main import create_app
from app.models.ai_analysis import AIAnalysis
from app.models.ai_embedding import AIEmbedding
from app.models.agent_feedback import AgentFeedback
from app.models.agent_memory import AgentMemory
from app.models.blocker import Blocker
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import AIAnalysisStatus, EmployeeStatus
from app.models.report_item import ReportItem
from app.models.report_translation import ReportTranslation
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.services import agent_service
from app.services.agent_tools import ToolContext, execute_tool, load_full_context
from app.services.data_scope import DataScope, SCOPE_INFO_KEY
from app.services.report_translation_service import translation_source_hash
from app.services.agent_query_plan import resolve_agent_query
from app.services.structured_data_service import materialize_resource_demand

THIS_WEEK = (date(2026, 9, 7), date(2026, 9, 13))
OLDER_WEEK = (date(2026, 8, 24), date(2026, 8, 30))
OTHER_WEEK = (date(2026, 8, 31), date(2026, 9, 6))


def _ctx(session: Session, **kwargs) -> ToolContext:
    return ToolContext(
        session=session,
        period_start=THIS_WEEK[0],
        period_end=THIS_WEEK[1],
        **kwargs,
    )


def _call_tool(ctx: ToolContext, name: str, arguments: dict) -> dict:
    return json.loads(execute_tool(ctx, name, json.dumps(arguments, ensure_ascii=False)))


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


def _report(
    session: Session,
    title: str,
    content: str,
    period: tuple[date, date] = THIS_WEEK,
    submitted_at: datetime | None = None,
    employee: Employee | None = None,
) -> WeeklyReport:
    if employee is None:
        employee = Employee(
            source="wecom",
            external_id=f"emp_{title}",
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
        start_date=period[0],
        end_date=period[1],
        submitted_at=submitted_at or datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.commit()
    return report


def _tool(name: str, arguments: dict, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments, ensure_ascii=False))


def _provider(monkeypatch: pytest.MonkeyPatch, script, response: str) -> FakeProvider:
    fake = FakeProvider(response=response, tool_call_script=list(script))
    monkeypatch.setattr(agent_service, "get_provider", lambda settings: fake)
    return fake


def _tool_messages(fake: FakeProvider, round_index: int) -> list[dict]:
    messages = fake.message_calls[round_index]["messages"]
    return [message for message in messages if message.get("role") == "tool"]


def test_agent_calls_tools_then_answers(db_session: Session, monkeypatch) -> None:
    """模型先检索、再读全文、最后作答：步骤与来源都要能对上。"""
    report = _report(db_session, "本周进展", "完成了支付接口联调，已提测。")
    fake = _provider(
        monkeypatch,
        [
            [_tool("search_reports", {"keyword": "支付"})],
            [_tool("get_reports", {"report_ids": [report.id]}, "call_2")],
        ],
        response=f"本周完成了支付接口联调，已提测。[#{report.id}]",
    )

    payload = agent_service.run_agent(
        db_session, "支付接口进展如何？", start="2026-09-07", end="2026-09-13"
    )

    assert [step["tool"] for step in payload["steps"]] == ["search_reports", "get_reports"]
    assert payload["steps"][0]["summary"].startswith("命中 1 篇")
    assert payload["read_count"] == 1
    assert [source["report_id"] for source in payload["sources"]] == [report.id]
    assert payload["cited_count"] == 1
    assert payload["valid_citation_ids"] == [report.id]
    # 第二回合必须能看到第一次检索的真实结果，否则等于没查
    assert "支付接口联调" in fake.message_calls[1]["messages"][-1]["content"]


def test_colloquial_time_uses_model_dates_instead_of_page_week(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = _report(db_session, "八月底进展", "完成八月底项目联调。", OLDER_WEEK)
    page = _report(db_session, "页面本周", "本周只处理页面事项。", THIS_WEEK)
    fake = _provider(
        monkeypatch,
        [
            [_tool("search_reports", {"start": "2026-08-24", "end": "2026-08-30", "scope": "all"})],
            [_tool("get_reports", {"report_ids": [older.id]}, "call_2")],
        ],
        response=f"八月底完成项目联调。[#{older.id}]",
    )
    payload = agent_service.run_agent(
        db_session, "前些日子周报有什么进展？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["full_context"]["reason"] == "time_needs_resolution"
    assert payload["period"] == {"start": "2026-08-24", "end": "2026-08-30"}
    assert payload["resolved_scope"]["time_source"] == "model"
    assert [source["report_id"] for source in payload["sources"]] == [older.id]
    assert page.id not in payload["valid_citation_ids"]
    assert "本周只处理页面事项" not in str(fake.message_calls)


def test_colloquial_time_without_date_does_not_claim_page_week(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _report(db_session, "页面本周", "本周只处理页面事项。", THIS_WEEK)
    _provider(monkeypatch, [], response="本周项目正常。")

    payload = agent_service.run_agent(
        db_session, "前些日子周报有什么进展？", start="2026-09-07", end="2026-09-13"
    )

    assert "unresolved_time" in payload["coverage_gaps"]
    assert "请给出具体日期" in payload["answer"]
    assert payload["sources"] == []


def test_agent_does_not_expand_empty_period_search(db_session: Session) -> None:
    """无命中保持在本轮周期，旧周期证据不能被静默混入。"""
    older = _report(db_session, "三周前", "三周前的工作内容。", OLDER_WEEK)
    ctx = _ctx(db_session, keyword="工作", lock_scope=True)

    result = _call_tool(ctx, "search_reports", {"keyword": "工作", "scope": "all"})

    assert result["scope"] == "period"
    assert result["period"] == {"start": "2026-09-07", "end": "2026-09-13"}
    assert result["matched_total"] == 0
    assert result["reports"] == []
    assert result["escalated_from"] is None
    assert older.id not in ctx.offered_report_ids
    assert not ctx.escalations


def test_agent_keyword_is_strict_or_filter_and_uses_only_current_translations(
    db_session: Session,
) -> None:
    original_match = _report(db_session, "支付", "支付接口已完成。")
    wanted = _report(db_session, "请款", "purchase order approved")
    wanted.content_language = "english"
    db_session.flush()
    db_session.add(
        ReportTranslation(
            report_id=wanted.id,
            source_hash=translation_source_hash(wanted),
            source_language="en",
            target_language="zh",
            translated_content="预算已经批准",
            status="success",
        )
    )
    stale = _report(db_session, "旧译文", "finance review completed")
    stale.content_language = "english"
    db_session.flush()
    db_session.add(
        ReportTranslation(
            report_id=stale.id,
            source_hash="stale-hash",
            source_language="en",
            target_language="zh",
            translated_content="预算已经批准",
            status="success",
        )
    )
    db_session.commit()

    ctx = _ctx(db_session, keyword="预算 支付", lock_scope=True)
    result = _call_tool(ctx, "search_reports", {"keyword": "替换词"})

    assert result["matched_total"] == 2  # 任一词命中
    assert {row["report_id"] for row in result["reports"]} == {original_match.id, wanted.id}
    assert result["keyword_coverage"]["translation_gaps"] == 1
    assert stale.id not in ctx.offered_report_ids


def test_query_plan_question_scope_overrides_page_and_unknown_person_clarifies(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.agent_query_plan as query_plan

    monkeypatch.setattr(query_plan, "business_date", lambda _now: date(2026, 9, 30))
    dept = Department(external_id="query-plan-dept", name="技术部", source="wecom")
    db_session.add(dept)
    db_session.flush()
    selected = Employee(
        source="wecom", external_id="query-selected", name="王明",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    )
    mentioned = Employee(
        source="wecom", external_id="query-mentioned", name="李雷",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    )
    db_session.add_all([selected, mentioned])
    db_session.commit()
    db_session.info[SCOPE_INFO_KEY] = DataScope("subtree", (dept.id,))

    plan = resolve_agent_query(
        db_session,
        question="李雷上个月做了什么？",
        start="2026-09-07",
        end="2026-09-13",
        scope="period",
        department_id=dept.id,
        employee_id=selected.id,
    )
    assert (plan.start, plan.end) == (date(2026, 8, 1), date(2026, 8, 31))
    assert plan.employee_id == mentioned.id
    assert plan.time_source == "question"

    unknown = resolve_agent_query(
        db_session,
        question="王五上个月做了什么？",
        start="2026-09-07",
        end="2026-09-13",
        scope="period",
        department_id=dept.id,
        employee_id=selected.id,
    )
    assert unknown.clarification and unknown.clarification["reason"] == "unknown_employee"

    duplicate = Employee(
        source="wecom", external_id="query-duplicate", name="李雷",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    )
    outside_dept = Department(
        external_id="query-plan-outside", name="市场部", source="wecom"
    )
    db_session.add_all([duplicate, outside_dept])
    db_session.flush()
    outside_employee = Employee(
        source="wecom", external_id="query-outside", name="王五",
        status=EmployeeStatus.ACTIVE, department_id=outside_dept.id,
    )
    db_session.add(outside_employee)
    db_session.commit()

    ambiguous = resolve_agent_query(
        db_session,
        question="李雷上个月做了什么？",
        start=None,
        end=None,
        scope="period",
        department_id=None,
        employee_id=None,
    )
    assert ambiguous.clarification and ambiguous.clarification["reason"] == "ambiguous_employee"
    assert len(ambiguous.clarification["options"]) == 2

    inaccessible = resolve_agent_query(
        db_session,
        question="王五上个月做了什么？",
        start=None,
        end=None,
        scope="period",
        department_id=None,
        employee_id=None,
    )
    assert inaccessible.clarification
    assert inaccessible.clarification["reason"] == "inaccessible_employee"
    assert "王五" not in inaccessible.clarification["message"]

    ambiguous_employee_with_resolved_time = resolve_agent_query(
        db_session,
        question="李雷最近两周做了什么？",
        start=None,
        end=None,
        scope="period",
        department_id=None,
        employee_id=None,
    )
    assert (
        ambiguous_employee_with_resolved_time.clarification
        and ambiguous_employee_with_resolved_time.clarification["reason"] == "ambiguous_employee"
    )


@pytest.mark.parametrize(
    ("question", "expected_year"),
    [("今年的周报全部拿来分析，总结三个问题给我", 2026), ("去年周报有哪些问题", 2025)],
)
def test_query_plan_year_in_question_overrides_all_scope(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    expected_year: int,
) -> None:
    import app.services.agent_query_plan as query_plan

    monkeypatch.setattr(query_plan, "business_date", lambda _now: date(2026, 10, 9))
    plan = resolve_agent_query(
        db_session,
        question=question,
        start="2026-09-28",
        end="2026-10-04",
        scope="all",
        department_id=None,
        employee_id=None,
    )
    assert plan.clarification is None
    assert (plan.start, plan.end) == (date(expected_year, 1, 1), date(expected_year, 12, 31))
    assert plan.scope == "period"
    assert plan.time_source == "question"


@pytest.mark.parametrize(
    ("question", "expected_start", "expected_end"),
    [
        ("2026-10-03的周报", date(2026, 10, 3), date(2026, 10, 3)),
        ("最近两周的周报", date(2026, 9, 26), date(2026, 10, 9)),
        ("前段时间有哪些进展", date(2026, 9, 12), date(2026, 10, 9)),
        ("上季度做了什么", date(2026, 7, 1), date(2026, 9, 30)),
        ("去年第三季度", date(2025, 7, 1), date(2025, 9, 30)),
        ("近一季度", date(2026, 7, 10), date(2026, 10, 9)),
        ("上上周的汇报", date(2026, 9, 21), date(2026, 9, 27)),
        ("2025年第一季度", date(2025, 1, 1), date(2025, 3, 31)),
    ],
)
def test_query_plan_understands_common_relative_periods(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    expected_start: date,
    expected_end: date,
) -> None:
    import app.services.agent_query_plan as query_plan

    monkeypatch.setattr(query_plan, "business_date", lambda _now: date(2026, 10, 9))
    plan = resolve_agent_query(
        db_session, question=question, start="2026-10-05", end="2026-10-11",
        scope="period", department_id=None, employee_id=None,
    )
    assert plan.clarification is None
    assert (plan.start, plan.end) == (expected_start, expected_end)
    assert plan.time_source == "question"


@pytest.mark.parametrize(
    ("today", "question", "expected_start", "expected_end"),
    [
        (date(2026, 10, 9), "九月的工作完成情况怎么样", date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 10, 9), "9月的工作完成情况怎么样", date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 10, 9), "2025年9月的工作完成情况", date(2025, 9, 1), date(2025, 9, 30)),
        (date(2026, 10, 9), "2025年九月的工作完成情况", date(2025, 9, 1), date(2025, 9, 30)),
        (date(2026, 10, 9), "去年九月的工作完成情况", date(2025, 9, 1), date(2025, 9, 30)),
        (date(2027, 1, 9), "十二月的工作完成情况", date(2026, 12, 1), date(2026, 12, 31)),
    ],
)
def test_query_plan_month_in_question_is_time_not_employee(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    today: date,
    question: str,
    expected_start: date,
    expected_end: date,
) -> None:
    import app.services.agent_query_plan as query_plan

    monkeypatch.setattr(query_plan, "business_date", lambda _now: today)
    dept = Department(external_id="month-dept", name="技术部", source="wecom")
    db_session.add(dept)
    db_session.flush()
    db_session.add(Employee(
        source="wecom", external_id="month-named-employee", name="九月",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    ))
    db_session.commit()

    plan = resolve_agent_query(
        db_session,
        question=question,
        start="2026-10-05",
        end="2026-10-11",
        scope="all",
        department_id=None,
        employee_id=None,
    )
    assert plan.clarification is None
    assert (plan.start, plan.end) == (expected_start, expected_end)
    assert plan.scope == "period"
    assert plan.time_source == "question"
    assert plan.employee_id is None


def test_query_plan_unknown_person_with_month_still_clarifies(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.agent_query_plan as query_plan

    monkeypatch.setattr(query_plan, "business_date", lambda _now: date(2026, 10, 9))
    plan = resolve_agent_query(
        db_session,
        question="王五九月做了什么？",
        start=None,
        end=None,
        scope="all",
        department_id=None,
        employee_id=None,
    )
    assert plan.clarification and plan.clarification["reason"] == "unknown_employee"
    assert (plan.start, plan.end) == (date(2026, 9, 1), date(2026, 9, 30))

    dept = Department(external_id="known-person-dept", name="技术部", source="wecom")
    db_session.add(dept)
    db_session.flush()
    known = Employee(
        source="wecom", external_id="known-month-person", name="李雷",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    )
    db_session.add(known)
    db_session.commit()
    known_plan = resolve_agent_query(
        db_session,
        question="李雷九月做了什么？",
        start=None,
        end=None,
        scope="all",
        department_id=None,
        employee_id=None,
    )
    assert known_plan.clarification is None
    assert known_plan.employee_id == known.id
    assert (known_plan.start, known_plan.end) == (date(2026, 9, 1), date(2026, 9, 30))

    month_named = Employee(
        source="wecom", external_id="known-month-name", name="李九月",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    )
    db_session.add(month_named)
    db_session.commit()
    named_plan = resolve_agent_query(
        db_session,
        question="李九月做了什么？",
        start="2026-10-05",
        end="2026-10-11",
        scope="period",
        department_id=None,
        employee_id=None,
    )
    assert named_plan.clarification is None
    assert named_plan.employee_id == month_named.id
    assert (named_plan.start, named_plan.end) == (date(2026, 10, 5), date(2026, 10, 11))


def test_query_plan_resolves_case_insensitive_english_alias_and_all_reports(
    db_session: Session,
) -> None:
    employee = Employee(
        source="wecom", external_id="leo-full-name", name="Leo 游丰奕",
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add(employee)
    db_session.commit()

    plan = resolve_agent_query(
        db_session,
        question="分析leo的全部周报",
        start="2026-09-07",
        end="2026-09-13",
        scope="period",
        department_id=None,
        employee_id=None,
    )
    assert plan.clarification is None
    assert plan.employee_id == employee.id
    assert plan.scope == "all"


def test_query_plan_english_name_requires_word_boundary(db_session: Session) -> None:
    db_session.add(Employee(
        source="wecom", external_id="leo-boundary", name="Leo 游丰奕",
        status=EmployeeStatus.ACTIVE,
    ))
    db_session.commit()
    plan = resolve_agent_query(
        db_session, question="分析Leonardo的全部周报", start="2026-09-07", end="2026-09-13",
        scope="all", department_id=None, employee_id=None,
    )
    assert plan.employee_id is None
    assert plan.clarification and plan.clarification["reason"] == "unknown_employee"


def test_query_plan_ambiguous_english_alias_asks_which_employee(
    db_session: Session,
) -> None:
    dept = Department(external_id="leo-ambiguous-dept", name="技术部", source="wecom")
    db_session.add(dept)
    db_session.flush()
    db_session.add_all([
        Employee(source="wecom", external_id="leo-short", name="Leo", status=EmployeeStatus.ACTIVE, department_id=dept.id),
        Employee(source="wecom", external_id="leo-full", name="Leo 游丰奕", status=EmployeeStatus.ACTIVE, department_id=dept.id),
    ])
    db_session.commit()

    plan = resolve_agent_query(
        db_session, question="分析leo的全部周报", start="2026-09-07", end="2026-09-13",
        scope="all", department_id=None, employee_id=None,
    )
    assert plan.clarification and plan.clarification["reason"] == "ambiguous_employee"
    assert {option["label"].split("（")[0] for option in plan.clarification["options"]} == {"Leo", "Leo 游丰奕"}

    selected_id = next(
        option["employee_id"] for option in plan.clarification["options"]
        if option["label"].startswith("Leo 游丰奕")
    )
    selected = resolve_agent_query(
        db_session, question="分析leo的全部周报", start="2026-09-07", end="2026-09-13",
        scope="all", department_id=None, employee_id=selected_id,
    )
    assert selected.clarification is None
    assert selected.employee_id == selected_id

    full_name = resolve_agent_query(
        db_session, question="分析Leo 游丰奕的全部周报", start="2026-09-07", end="2026-09-13",
        scope="all", department_id=None, employee_id=None,
    )
    assert full_name.clarification is None
    assert full_name.employee_name == "Leo 游丰奕"


def test_two_week_stagnation_wording_is_clarified(db_session: Session) -> None:
    events = list(
        agent_service.stream_agent(
            db_session,
            "两周未推进的任务有哪些？",
            start="2026-09-07",
            end="2026-09-13",
        )
    )
    assert events == [
        {
            "type": "clarify",
            "reason": "unsupported_stalled_period",
            "message": "停滞任务的统一业务口径是连续至少三周未推进。请按三周口径查询，或说明其他时间范围。",
            "options": [],
        }
    ]


def test_agent_stops_at_max_steps_and_forces_final_answer(
    db_session: Session, monkeypatch
) -> None:
    """模型一直想调工具时必须以步数上限收口，且收尾回合不再给工具。"""
    _report(db_session, "本周进展", "本周内容。")
    script = [[_tool("search_reports", {}, f"call_{i}")] for i in range(6)]
    fake = _provider(monkeypatch, script, response="基于已有材料作答。")

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13", max_steps=2
    )

    assert payload["max_steps"] == 2
    assert len(payload["steps"]) == 2
    # 收尾回合不能再传工具清单，否则模型会继续要求检索
    assert fake.message_calls[-1]["tools"] is None
    assert payload["answer"].startswith("当前数据中没有足够信息。")


def test_agent_tool_failure_does_not_break_the_turn(
    db_session: Session, monkeypatch
) -> None:
    """单次工具失败要变成可读结果，而不是让整轮问答 500。"""
    _report(db_session, "本周进展", "本周内容。")
    fake = _provider(
        monkeypatch,
        [
            [_tool("search_reports", {})],
            [_tool("no_such_tool", {}, "call_bad")],
            [_tool("get_reports", {"report_ids": ["不是数字"]}, "call_bad2")],
        ],
        response="虽然工具报错，但我给出了回答。",
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    summaries = [step["summary"] for step in payload["steps"]]
    assert any(summary.startswith("失败：未知工具") for summary in summaries)
    assert any(summary.startswith("失败：") for summary in summaries[2:])
    assert payload["answer"].startswith("当前数据中没有足够信息。")


def test_agent_drops_invented_citations(db_session: Session, monkeypatch) -> None:
    """模型编造的周报 ID 不能变成可点的来源。"""
    report = _report(db_session, "本周进展", "本周内容。")
    _provider(
        monkeypatch,
        [[_tool("search_reports", {})]],
        response=f"依据 [#{report.id}] 与 [#999999] 可以判断。",
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert [source["report_id"] for source in payload["sources"]] == [report.id]
    assert payload["cited_count"] == 1


def test_agent_accepts_citation_written_with_zhoubao_word(
    db_session: Session, monkeypatch
) -> None:
    """模型把引用写成 [#周报365] 时也要能解析出来。

    实测踩到：真实模型在部分回答里写成 `[#周报365]`，旧正则解析不了，
    用户端「引用来源」直接变成空 —— 明明引用了却拿不到可点来源（违反 AC-17）。
    提取必须容忍这种写法，不能只靠提示词约束模型。
    """
    report = _report(db_session, "本周进展", "本周内容。")
    _provider(
        monkeypatch,
        [[_tool("search_reports", {})]],
        response=f"依据 [#周报{report.id}] 可以判断。",
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["valid_citation_ids"] == [report.id]
    assert payload["sources_cited"] is True
    assert payload["sources"][0]["report_id"] == report.id


def test_agent_still_rejects_non_numeric_citation(db_session: Session, monkeypatch) -> None:
    """放宽格式后，非数字的伪引用仍必须被丢弃。"""
    _report(db_session, "本周进展", "本周内容。")
    _provider(
        monkeypatch,
        [[_tool("search_reports", {})]],
        response="依据 [#周报ID] 无法判断。",
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["valid_citation_ids"] == []


def test_agent_get_reports_rejects_ids_it_never_offered(
    db_session: Session, monkeypatch
) -> None:
    """只能读 search_reports 返回过的 ID，其余一律丢弃。"""
    report = _report(db_session, "本周进展", "本周内容。")
    # 刻意放在别的周期：全量上下文只覆盖用户选定周期，它才算「没被给过的 ID」
    other = _report(db_session, "别人的周报", "不该被读到。", OTHER_WEEK)
    fake = _provider(
        monkeypatch,
        [
            [_tool("search_reports", {"keyword": "本周"})],
            [_tool("get_reports", {"report_ids": [report.id, other.id]}, "call_2")],
        ],
        response="已作答。",
    )

    agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    tool_output = json.loads(_tool_messages(fake, 2)[-1]["content"])
    assert tool_output["rejected_ids"] == [other.id]
    assert [item["report_id"] for item in tool_output["reports"]] == [report.id]


def test_agent_records_failure_and_returns_error_event(
    db_session: Session, monkeypatch
) -> None:
    """模型不可用时要写一条 failed 留痕，并给出 error 事件而不是堆栈。"""
    _report(db_session, "本周进展", "本周内容。")

    def boom(settings=None):
        raise LLMError("模型服务炸了")

    monkeypatch.setattr(agent_service, "get_provider", boom)

    events = list(
        agent_service.stream_agent(
            db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
        )
    )

    assert events[-1]["type"] == "error"
    assert events[-1]["message"] == "本次问答未能完成，请稍后重试；如持续出现请联系管理员。"
    row = (
        db_session.query(AIAnalysis)
        .filter(AIAnalysis.analysis_type == "agent_search")
        .order_by(AIAnalysis.id.desc())
        .first()
    )
    assert row is not None and row.status == "failed"
    assert row.error_message == "模型服务炸了"


def test_agent_endpoint_returns_payload(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    report = _report(db_session, "本周进展", "本周完成了支付联调。")
    _provider(
        monkeypatch,
        [[_tool("search_reports", {"keyword": "支付"})]],
        response=f"完成了支付联调。[#{report.id}]",
    )

    resp = client.post(
        "/api/ai/agent",
        json={"question": "支付进展？", "start": "2026-09-07", "end": "2026-09-13"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["steps"][0]["tool"] == "search_reports"
    assert body["sources"][0]["report_id"] == report.id


def test_agent_memory_crud_and_sensitive_content_guard(
    client: TestClient, db_session: Session
) -> None:
    created = client.post(
        "/api/ai/memories",
        json={
            "key": "report.period.rule",
            "category": "company_rule",
            "content": "公司周报周期为周一到周日。",
        },
    )
    assert created.status_code == 201
    memory_id = created.json()["id"]
    assert client.get("/api/ai/memories").json()[0]["enabled"] is True

    disabled = client.patch(f"/api/ai/memories/{memory_id}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    rejected = client.post(
        "/api/ai/memories",
        json={
            "key": "unsafe.secret",
            "category": "company_rule",
            "content": "API Key: demo-placeholder-value",
        },
    )
    assert rejected.status_code == 400

    bearer = client.post(
        "/api/ai/memories",
        json={
            "key": "unsafe.bearer",
            "category": "company_rule",
            "content": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        },
    )
    assert bearer.status_code == 400

    policy_text = client.post(
        "/api/ai/memories",
        json={
            "key": "safe.policy",
            "category": "company_rule",
            "content": "任何回答都不得展示 API Key 或密码。",
        },
    )
    assert policy_text.status_code == 201

    duplicate = client.post(
        "/api/ai/memories",
        json={
            "key": "safe.policy",
            "category": "company_rule",
            "content": "重复标识。",
        },
    )
    assert duplicate.status_code == 409

    assert client.delete(f"/api/ai/memories/{memory_id}").status_code == 204
    assert db_session.get(AgentMemory, memory_id) is None


def test_agent_receives_enabled_long_term_memory(
    db_session: Session, monkeypatch
) -> None:
    db_session.add_all(
        [
            AgentMemory(
                key="answer.style",
                category="preference",
                content="回答先给管理结论，再提供证据。",
                enabled=True,
            ),
            AgentMemory(
                key="disabled.rule",
                category="company_rule",
                content="这条停用内容不应进入模型。",
                enabled=False,
            ),
        ]
    )
    db_session.commit()
    _report(db_session, "本周进展", "本周完成了支付联调。")
    fake = _provider(monkeypatch, [], response="已完成。")

    agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    system = fake.message_calls[0]["messages"][0]["content"]
    assert "回答先给管理结论，再提供证据" in system
    assert "这条停用内容不应进入模型" not in system


def test_agent_history_can_be_soft_deleted_and_restored(
    client: TestClient, db_session: Session
) -> None:
    row = AIAnalysis(
        entity_type="ai_search",
        entity_id=0,
        analysis_type="agent_search",
        result_json={"question": "这周做了什么？", "answer": "已作答。", "sources": []},
        status="success",
    )
    db_session.add(row)
    db_session.commit()

    deleted = client.patch(f"/api/ai/history/{row.id}", json={"deleted": True})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/api/ai/history").json() == []

    restored = client.patch(f"/api/ai/history/{row.id}", json={"deleted": False})
    assert restored.status_code == 200
    assert restored.json()["deleted"] is False
    assert client.get("/api/ai/history").json()[0]["id"] == row.id


def test_agent_history_groups_turns_by_conversation_and_deletes_together(
    client: TestClient, db_session: Session
) -> None:
    first = AIAnalysis(
        entity_type="ai_search",
        entity_id=0,
        analysis_type="agent_search",
        conversation_id="conversation-1",
        result_json={
            "question": "这周做了什么？",
            "answer": "完成了支付联调。",
            "conversation_id": "conversation-1",
            "department_id": 42,
        },
        status="success",
    )
    second = AIAnalysis(
        entity_type="ai_search",
        entity_id=0,
        analysis_type="agent_search",
        conversation_id="conversation-1",
        result_json={
            "question": "还有什么风险？",
            "answer": "上线窗口较紧。",
            "conversation_id": "conversation-1",
            "department_id": 42,
        },
        status="success",
    )
    db_session.add_all([first, second])
    db_session.commit()

    history = client.get("/api/ai/history").json()
    assert len(history) == 1
    assert history[0]["id"] == second.id
    assert history[0]["question"] == "这周做了什么？"
    assert history[0]["department_id"] == 42
    assert [turn["question"] for turn in history[0]["turns"]] == [
        "这周做了什么？",
        "还有什么风险？",
    ]

    deleted = client.patch(f"/api/ai/history/{second.id}", json={"deleted": True})
    assert deleted.json()["affected"] == 2
    assert client.get("/api/ai/history").json() == []

    restored = client.patch(f"/api/ai/history/{second.id}", json={"deleted": False})
    assert restored.json()["affected"] == 2
    assert len(client.get("/api/ai/history").json()) == 1


def test_agent_stream_emits_sse_events(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """SSE 上检索步骤与答案增量都要能被前端实时拿到。"""
    report = _report(db_session, "本周进展", "本周完成了支付联调。")
    fake = _provider(
        monkeypatch,
        [
            [_tool("search_reports", {"keyword": "支付"})],
            [_tool("get_reports", {"report_ids": [report.id]}, "call_2")],
        ],
        response=f"完成了支付联调。[#{report.id}]",
    )
    fake.stream_chunk_size = 4
    monkeypatch.setattr(ai_routes, "_open_stream_session", lambda: db_session)

    resp = client.post(
        "/api/ai/agent/stream",
        json={"question": "支付进展？", "start": "2026-09-07", "end": "2026-09-13"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    kinds = [event["type"] for event in events]
    assert kinds.count("step") == 2
    assert kinds.count("delta") > 1, "答案必须分多帧下发，否则不叫流式"
    assert kinds[-1] == "done"
    assert events[-1]["payload"]["sources"][0]["report_id"] == report.id


def test_agent_stream_emits_clarification_for_unknown_person(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_routes, "_open_stream_session", lambda: db_session)
    response = client.post(
        "/api/ai/agent/stream",
        json={
            "question": "王五上个月做了什么？",
            "start": "2026-09-07",
            "end": "2026-09-13",
        },
    )
    events = [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert response.status_code == 200
    assert events == [
        {
            "type": "clarify",
            "reason": "unknown_employee",
            "message": "无法在当前可访问范围内唯一定位该人员，请核对姓名或联系管理员。",
            "options": [],
        }
    ]


def test_history_remains_available_before_feedback_migration(
    client: TestClient, db_session: Session
) -> None:
    row = AIAnalysis(
        entity_type="search_query",
        entity_id=0,
        analysis_type="agent_search",
        result_json={
            "question": "历史查询",
            "answer": "历史回答",
            "access_scope": {"kind": "all"},
        },
        status=AIAnalysisStatus.SUCCESS,
    )
    db_session.add(row)
    db_session.commit()
    AgentFeedback.__table__.drop(db_session.get_bind())

    history = client.get("/api/ai/history")
    assert history.status_code == 200
    assert history.json()[0]["feedback"] is None
    feedback = client.post(
        f"/api/ai/history/{row.id}/feedback",
        json={"category": "fact"},
    )
    assert feedback.status_code == 503


def test_search_reports_paginates_with_offset(db_session: Session) -> None:
    """命中超过一页时必须能翻到底，并且如实报告还有没有下一页。

    这是「读不全」短板的直接修复：以前没有 offset，第 21 篇之后永远取不出来。
    """
    for index in range(12):
        _report(
            db_session,
            f"第{index}篇",
            f"内容 {index}",
            submitted_at=datetime(2026, 9, 10, 10, index, 0),
        )

    ctx = _ctx(db_session)
    first = _call_tool(ctx, "search_reports", {"limit": 5})
    assert first["matched_total"] == 12
    assert first["returned"] == 5
    assert first["has_more"] is True
    assert first["next_offset"] == 5

    second = _call_tool(
        ctx, "search_reports", {"limit": 5, "offset": first["next_offset"]}
    )
    assert second["returned"] == 5
    assert second["has_more"] is True
    assert second["next_offset"] == 10

    third = _call_tool(
        ctx, "search_reports", {"limit": 5, "offset": second["next_offset"]}
    )
    assert third["returned"] == 2
    assert third["has_more"] is False
    assert third["next_offset"] is None

    # 三页合起来正好覆盖全部 12 篇，且不重不漏
    seen = [
        item["report_id"]
        for page in (first, second, third)
        for item in page["reports"]
    ]
    assert len(seen) == 12
    assert len(set(seen)) == 12


def test_search_reports_matches_any_keyword_token(db_session: Session) -> None:
    """多关键词按「任一命中」匹配，整句话硬匹配会一篇都查不到。"""
    _report(db_session, "支付联调", "完成了支付接口联调。")
    _report(db_session, "风控上线", "风控规则已上线。")

    ctx = _ctx(db_session)
    result = _call_tool(ctx, "search_reports", {"keyword": "支付 风控"})
    assert result["matched_total"] == 2
    assert result["returned"] == 2


def test_search_reports_explicit_range_hits_target_week(db_session: Session) -> None:
    """全部周期档位 + 显式日期：直接命中指定那一周，不用翻页盲找。

    复现线上问题：用户在「全部周期」下问 2026-08-31~09-06 那一周，
    以前模型只能拿「九月」当关键词（零命中）或翻页（步数耗尽），
    现在可以直接用 start/end 把该周 26 篇里的数据定位出来。
    """
    target = _report(db_session, "九月第一周", "九月第一周的工作。", OTHER_WEEK)
    _report(db_session, "本周进展", "本周的工作。", THIS_WEEK)

    ctx = _ctx(db_session, default_scope="all")
    result = _call_tool(
        ctx,
        "search_reports",
        {"scope": "all", "start": "2026-08-31", "end": "2026-09-06"},
    )

    assert result["matched_total"] == 1
    assert [item["report_id"] for item in result["reports"]] == [target.id]
    assert result["period"] == {"start": "2026-08-31", "end": "2026-09-06"}
    assert result["explicit_range"] == {
        "start": "2026-08-31",
        "end": "2026-09-06",
    }
    assert result["has_more"] is False
    # 命中的 ID 进入可读白名单，模型才能用 get_reports 读到正文
    assert target.id in ctx.offered_report_ids


def test_search_reports_explicit_range_intersects_locked_scope(
    db_session: Session,
) -> None:
    """显式日期是「再收窄」不是「换范围」：与锁定档位无交集时如实零命中。"""
    target = _report(db_session, "九月第一周", "九月第一周的工作。", OTHER_WEEK)

    ctx = _ctx(db_session, lock_scope=True)
    result = _call_tool(
        ctx,
        "search_reports",
        {"start": "2026-08-31", "end": "2026-09-06"},
    )

    # 界面锁定周期是 THIS_WEEK(09-07~09-13)，与 08-31~09-06 无交集，
    # 那一周的周报绝不能被漏进来
    assert result["matched_total"] == 0
    assert result["reports"] == []
    assert result["period"] == {"start": "2026-08-31", "end": "2026-09-06"}


def test_search_reports_explicit_range_partial_overlap(db_session: Session) -> None:
    """显式日期与锁定周期部分重叠时只保留交集，不会越界。"""
    _report(db_session, "本周进展", "本周的工作。", THIS_WEEK)
    _report(db_session, "九月第一周", "九月第一周的工作。", OTHER_WEEK)

    ctx = _ctx(db_session, lock_scope=True)
    # 请求 09-01~09-20：与锁定周期 09-07~09-13 的交集是 09-07~09-13
    result = _call_tool(
        ctx,
        "search_reports",
        {"start": "2026-09-01", "end": "2026-09-20"},
    )

    assert result["matched_total"] == 1
    assert result["reports"][0]["period_start"] == "2026-09-07"
    assert result["period"] == {"start": "2026-09-07", "end": "2026-09-13"}


def test_search_reports_explicit_range_rejects_half_range(
    db_session: Session,
) -> None:
    """只传 start 或 end 之一必须报错：补默认值会把「查某一周」静默变成「查当前周期」。"""
    _report(db_session, "本周进展", "本周的工作。")
    ctx = _ctx(db_session, default_scope="all")

    result = _call_tool(ctx, "search_reports", {"start": "2026-08-31"})
    assert "error" in result
    assert "matched_total" not in result

    result = _call_tool(ctx, "search_reports", {"start": "2026-09-06", "end": "2026-08-31"})
    assert "error" in result

    result = _call_tool(ctx, "search_reports", {"start": "bad-date", "end": "2026-09-06"})
    assert "error" in result


def test_list_submissions_reports_who_did_not_submit(db_session: Session) -> None:
    """「谁没交」必须来自统计口径：没交的人根本不在正文检索结果里。"""
    _report(db_session, "已交的人", "本周内容。")
    idle = Employee(
        source="wecom",
        external_id="emp_idle",
        name="李四",
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add(idle)
    db_session.commit()

    result = _call_tool(_ctx(db_session), "list_submissions", {})
    assert result["total"] == 2
    assert result["submitted_count"] == 1
    assert result["unsubmitted_count"] == 1
    assert [item["name"] for item in result["unsubmitted"]] == ["李四"]


def test_list_submissions_excludes_exempt_and_inactive(db_session: Session) -> None:
    """免提交与离职不进基数，否则催交名单会点名不该被点名的人。"""
    exempt = Employee(
        source="wecom",
        external_id="emp_exempt",
        name="免提交",
        status=EmployeeStatus.ACTIVE,
        report_exempt=True,
    )
    left = Employee(
        source="wecom",
        external_id="emp_left",
        name="已离职",
        status="inactive",
    )
    active = Employee(
        source="wecom",
        external_id="emp_active",
        name="在职",
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add_all([exempt, left, active])
    db_session.commit()

    result = _call_tool(_ctx(db_session), "list_submissions", {})
    assert result["total"] == 1
    assert [item["name"] for item in result["unsubmitted"]] == ["在职"]


def test_list_report_quality_reads_stored_scores(db_session: Session) -> None:
    """质量类问题要读系统已算好的评分，而不是按正文长度自行判断。"""
    good = _report(db_session, "详实周报", "内容很详实。" * 40)
    thin = _report(db_session, "敷衍周报", "无")
    good.ai_checked_at = thin.ai_checked_at = datetime(2026, 9, 14)
    db_session.add_all(
        [
            AIAnalysis(
                entity_type="weekly_report",
                entity_id=good.id,
                analysis_type="report_analysis",
                result_json={"score": 88, "summary": "结构完整"},
                status="success",
            ),
            AIAnalysis(
                entity_type="weekly_report",
                entity_id=thin.id,
                analysis_type="report_analysis",
                result_json={"score": 12, "is_thin": True, "thin_reason": "敷衍"},
                status="success",
            ),
        ]
    )
    db_session.commit()

    ascending = _call_tool(_ctx(db_session), "list_report_quality", {"sort": "score_asc"})
    assert ascending["scored"] == 2
    assert ascending["unscored"] == 0
    assert [item["score"] for item in ascending["items"]] == [12, 88]

    descending = _call_tool(_ctx(db_session), "list_report_quality", {"sort": "score_desc"})
    assert [item["score"] for item in descending["items"]] == [88, 12]


def test_list_report_quality_does_not_rank_unanalyzed_as_low(db_session: Session) -> None:
    """未分析 ≠ 低分：没有结论的周报排在最后，不能混进排名诬陷人。"""
    analyzed = _report(db_session, "已分析", "内容。" * 20)
    pending = _report(db_session, "未分析", "内容。" * 20)
    analyzed.ai_checked_at = datetime(2026, 9, 14)
    db_session.add(
        AIAnalysis(
            entity_type="weekly_report",
            entity_id=analyzed.id,
            analysis_type="report_analysis",
            result_json={"score": 30},
            status="success",
        )
    )
    db_session.commit()

    result = _call_tool(_ctx(db_session), "list_report_quality", {"sort": "score_asc"})
    assert result["scored"] == 1
    assert result["unscored"] == 1
    # 低分在前，但「未分析」必须排在已分析的后面
    assert result["coverage_complete"] is False
    assert result["items"] == []


def test_get_reports_reports_truncation(db_session: Session, monkeypatch) -> None:
    """正文被字符预算截断时要明确回报，不能让模型以为读到了全文。"""
    report = _report(db_session, "超长周报", "内容" * 500)
    ctx = _ctx(db_session, max_read_chars=100)
    _call_tool(ctx, "search_reports", {})

    result = _call_tool(ctx, "get_reports", {"report_ids": [report.id]})
    assert result["truncated_ids"] == [report.id]
    assert result["reports"][0]["content_truncated"] is True
    assert result["budget_remaining"] == 0


def test_quality_tool_output_can_be_cited(db_session: Session, monkeypatch) -> None:
    """模型引用「刚在评分结果里看到的周报」不能被当成编造而丢弃。

    list_report_quality 只是通过另一条途径把同样的周报 ID 交给模型，
    这些 ID 必须同样登记为可引用，否则引用会被静默扔掉、来源变空。
    """
    report = _report(db_session, "高分周报", "内容详实。" * 20)
    report.ai_checked_at = datetime(2026, 9, 14)
    db_session.add(
        AIAnalysis(
            entity_type="weekly_report",
            entity_id=report.id,
            analysis_type="report_analysis",
            result_json={"score": 88, "summary": "结构完整"},
            status="success",
        )
    )
    db_session.commit()
    _provider(
        monkeypatch,
        [[_tool("list_report_quality", {"scope": "period"})]],
        response=f"本周评分最高的是 [#{report.id}]。",
    )

    payload = agent_service.run_agent(
        db_session, "谁写得最好？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["valid_citation_ids"] == [report.id]
    assert payload["sources_cited"] is True
    assert payload["sources"][0]["report_id"] == report.id


def test_agent_replays_conversation_history(db_session: Session, monkeypatch) -> None:
    """追问要能看到上一轮问答，而不是每轮重新猜「上面」指什么。"""
    db_session.add(
        AIAnalysis(
            entity_type="search_query",
            entity_id=0,
            analysis_type="agent_search",
            conversation_id="conv-1",
            result_json={
                "question": "本周有哪些卡点？",
                "answer": "有三个卡点。",
                "conversation_id": "conv-1",
            },
            status="success",
        )
    )
    db_session.commit()
    _report(db_session, "本周进展", "本周内容。")
    fake = _provider(monkeypatch, [], response="它们在上一轮已列出。")

    agent_service.run_agent(
        db_session,
        "上面那些卡点谁负责？",
        start="2026-09-07",
        end="2026-09-13",
        conversation_id="conv-1",
    )

    sent = fake.message_calls[0]["messages"]
    roles = [message["role"] for message in sent]
    assert roles[:3] == ["system", "user", "assistant"]
    assert sent[1]["content"] == "本周有哪些卡点？"
    assert "上面那些卡点谁负责？" in sent[-1]["content"]


def test_agent_history_replays_only_matching_department_scope(db_session: Session) -> None:
    selected = Department(external_id="dept-selected", name="技术部", source="wecom")
    other = Department(external_id="dept-other", name="市场部", source="wecom")
    db_session.add_all([selected, other])
    db_session.flush()
    db_session.add_all(
        [
            AIAnalysis(
                entity_type="search_query",
                entity_id=0,
                analysis_type="agent_search",
                conversation_id="shared-id",
                result_json={
                    "question": "技术部的问题",
                    "answer": "技术部答案",
                    "department_id": selected.id,
                },
                status="success",
            ),
            AIAnalysis(
                entity_type="search_query",
                entity_id=0,
                analysis_type="agent_search",
                conversation_id="shared-id",
                result_json={
                    "question": "市场部的问题",
                    "answer": "市场部答案",
                    "department_id": other.id,
                },
                status="success",
            ),
            AIAnalysis(
                entity_type="search_query",
                entity_id=0,
                analysis_type="agent_search",
                conversation_id="shared-id",
                result_json={"question": "旧版问题", "answer": "旧版答案"},
                status="success",
            ),
        ]
    )
    db_session.commit()

    selected_history = agent_service._conversation_messages(
        db_session, "shared-id", department_id=selected.id
    )
    company_history = agent_service._conversation_messages(
        db_session, "shared-id", department_id=None
    )

    assert [message["content"] for message in selected_history] == [
        "技术部的问题",
        "技术部答案",
    ]
    assert [message["content"] for message in company_history] == [
        "旧版问题",
        "旧版答案",
    ]


def test_agent_marks_sources_not_cited_when_answer_has_no_citation(
    db_session: Session, monkeypatch
) -> None:
    """回答里没有 [#ID] 时，来源只能标成「读过」，不能冒充引用出处。"""
    report = _report(db_session, "本周进展", "本周内容。")
    _provider(
        monkeypatch,
        [
            [_tool("search_reports", {})],
            [_tool("get_reports", {"report_ids": [report.id]}, "call_2")],
        ],
        response="我读了这篇，但没写引用编号。",
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["sources_cited"] is False
    assert payload["sources"][0]["cited"] is False
    assert payload["sources"][0]["reason"] == "本次读取过，但回答未显式引用"


@pytest.mark.parametrize("question", [
    "帮我把刚才的回答改写得通俗些",
    "把刚才的回答压缩成一句话",
    "再概括一下上面的回答",
    "刚才那段话再简短一点",
])
def test_agent_followup_rephrasing_uses_history_without_reloading_reports(
    db_session: Session, monkeypatch, question: str
) -> None:
    report = _report(db_session, "本周进展", "项目 A 已完成联调。")
    _provider(monkeypatch, [], response=f"项目 A 已完成联调 [#{report.id}]。")
    agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13",
        conversation_id="rephrase-chat",
    )
    fake = _provider(monkeypatch, [], response="可以写成：项目 A 的联调工作已经完成。")

    payload = agent_service.run_agent(
        db_session, question, start="2026-09-07", end="2026-09-13",
        conversation_id="rephrase-chat",
    )

    assert payload["answer"] == "可以写成：项目 A 的联调工作已经完成。"
    assert payload["evidence_type"] == "conversation"
    assert payload["read_count"] == 0
    assert payload["steps"] == []
    assert fake.message_calls[0]["tools"] is None
    assert any("项目 A 已完成联调" in message["content"] for message in fake.message_calls[0]["messages"])


def test_agent_greeting_does_not_read_reports_but_fact_question_still_does(
    db_session: Session, monkeypatch
) -> None:
    report = _report(db_session, "本周进展", "项目 A 已完成联调。")
    fake = _provider(monkeypatch, [], response="你好，我可以帮你查阅周报。")
    greeting = agent_service.run_agent(
        db_session, "你好", start="2026-09-07", end="2026-09-13",
    )
    assert greeting["answer"] == "你好，我可以帮你查阅周报。"
    assert greeting["evidence_type"] == "conversation"
    assert fake.message_calls[0]["tools"] is None
    assert "项目 A 已完成联调" not in str(fake.message_calls[0]["messages"])

    fact_fake = _provider(monkeypatch, [], response=f"项目 A 已完成联调 [#{report.id}]。")
    factual = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13",
    )
    assert factual["evidence_type"] == "weekly_reports"
    assert factual["full_context"]["enabled"] or fact_fake.message_calls[0]["tools"] is not None


@pytest.mark.parametrize("question", [
    "你好，请只用一句话回复。",
    "您好，简短回答即可。",
    # 2026-10-10 线上验收：这句问候曾被当成数据问题，模型空搜一轮后
    # 触发 only_excerpts 覆盖缺口，给回答错误地附上「覆盖不完整」提示。
    "你好，请用一句话介绍你能做什么。",
    "你能做什么？",
    "请自我介绍一下。",
])
def test_greeting_with_reply_style_instruction_does_not_read_reports(
    db_session: Session, monkeypatch, question: str
) -> None:
    _report(db_session, "本周进展", "本条内容不应随问候发送给模型。")
    fake = _provider(monkeypatch, [], response="你好。")

    payload = agent_service.run_agent(
        db_session,
        question,
        scope="all",
        start="2026-09-07",
        end="2026-09-13",
    )

    assert payload["evidence_type"] == "conversation"
    assert payload["coverage_status"] == "complete"
    assert payload["coverage_gaps"] == []
    assert payload["read_count"] == 0
    assert payload["steps"] == []
    assert payload["full_context"]["enabled"] is False
    assert fake.message_calls[0]["tools"] is None
    assert "本条内容不应随问候发送给模型" not in str(fake.message_calls[0]["messages"])


def test_greeting_with_report_question_keeps_report_path(
    db_session: Session, monkeypatch
) -> None:
    report = _report(db_session, "本周进展", "项目 A 本周完成联调。")
    fake = _provider(monkeypatch, [], response=f"项目 A 完成联调 [#{report.id}]。")

    payload = agent_service.run_agent(
        db_session,
        "你好，请总结本周进展。",
        start="2026-09-07",
        end="2026-09-13",
    )

    assert payload["evidence_type"] == "weekly_reports"
    assert "项目 A 本周完成联调" in str(fake.message_calls[0]["messages"])


@pytest.mark.parametrize("question", ["为什么这么说？", "能举个例子吗", "你觉得呢"])
def test_agent_common_followups_use_conversation_path(
    db_session: Session, monkeypatch, question: str
) -> None:
    fake = _provider(monkeypatch, [], response="我解释一下。")
    payload = agent_service.run_agent(
        db_session, question, start="2026-09-07", end="2026-09-13",
    )
    assert payload["evidence_type"] == "conversation"
    assert fake.message_calls[0]["tools"] is None


def test_agent_general_advice_does_not_load_reports_even_for_all_scope(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _report(db_session, "本周项目周报", "只用于周报查询的敏感工作内容。")
    fake = _provider(monkeypatch, [], response="先把目标拆成可执行的小步骤。")

    payload = agent_service.run_agent(
        db_session,
        "请教我怎么把一个复杂任务拆成今天能开始执行的三个小步骤。",
        scope="all",
        start="2026-09-07",
        end="2026-09-13",
    )

    assert payload["evidence_type"] == "conversation"
    assert payload["read_count"] == 0
    assert payload["steps"] == []
    assert fake.message_calls[0]["tools"] is None
    assert "只用于周报查询的敏感工作内容" not in str(fake.message_calls[0]["messages"])


def test_agent_strict_keyword_keeps_report_query_path(db_session: Session, monkeypatch) -> None:
    fake = _provider(monkeypatch, [], response="没有找到相关记录。")
    payload = agent_service.run_agent(
        db_session, "解释一下这句话", keyword="项目 A",
        start="2026-09-07", end="2026-09-13",
    )
    assert payload["evidence_type"] == "weekly_reports"
    assert "没有找到匹配的周报" in payload["answer"]
    assert fake.message_calls == []


@pytest.mark.parametrize("question", [
    "上周有多少人没交周报？",
    "上周没交周报的有几个人？",
    "上周哪些人未交周报？",
])
def test_agent_submission_count_uses_structured_data(
    db_session: Session, monkeypatch, question: str
) -> None:
    fake = _provider(monkeypatch, [], response="未提交人数以统计结果为准。")
    payload = agent_service.run_agent(
        db_session, question, start="2026-09-07", end="2026-09-13",
    )

    assert payload["evidence_type"] == "structured_data"
    assert payload["steps"][0]["tool"] == "list_submissions"
    assert payload["full_context"]["enabled"] is False
    assert fake.message_calls[0]["tools"] is None
    assert "未提交人数以统计结果为准" in payload["answer"]


def test_agent_marks_sources_cited_when_answer_has_citation(
    db_session: Session, monkeypatch
) -> None:
    report = _report(db_session, "本周进展", "本周内容。")
    _provider(
        monkeypatch,
        [[_tool("search_reports", {})]],
        response=f"依据 [#{report.id}] 可以判断。",
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["sources_cited"] is True
    assert payload["sources"][0]["cited"] is True


def test_agent_cited_sources_not_truncated_by_max_sources(
    db_session: Session, monkeypatch
) -> None:
    """回答里引用了几篇，来源面板就要能核对几篇。

    引用是回答的证据链，不能被 MAX_SOURCES（5）截断；
    截断只允许发生在「读过但未引用」的兜底列表上。
    """
    reports = [
        _report(db_session, f"进展{i}", f"本周内容{i}。") for i in range(1, 7)
    ]
    citations = "".join(f"[#{report.id}]" for report in reports)
    _provider(
        monkeypatch,
        [[_tool("search_reports", {})]],
        response=f"综合 {citations} 共 6 篇周报得出结论。",
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["sources_cited"] is True
    assert len(payload["sources"]) == len(reports)
    assert [source["cited"] for source in payload["sources"]] == [True] * len(reports)


def test_agent_payload_reports_truncated_reads(db_session: Session, monkeypatch) -> None:
    """截断信息必须进最终 payload，前端的「已读全部」错觉才能被消掉。"""
    report = _report(db_session, "超长周报", "内容" * 500)
    fake = _provider(
        monkeypatch,
        [
            [_tool("search_reports", {})],
            [_tool("get_reports", {"report_ids": [report.id]}, "call_2")],
        ],
        response="已作答。",
    )
    # 把预算压到只够读一部分，触发截断
    monkeypatch.setattr(
        agent_service,
        "runtime_settings",
        lambda session: _SettingsStub(ai_max_input_chars=80),
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["read_truncated"] is True
    assert payload["truncated_report_ids"] == [report.id]


class _SettingsStub:
    """只提供 Agent 循环需要的最小字段，避免测试依赖整份运行时配置。

    默认把「全量上下文」预算设为 0，强制走工具检索路径；
    需要验证全量上下文的用例显式传入正数。
    """

    def __init__(
        self,
        ai_max_input_chars: int = 12000,
        ai_agent_full_context_chars: int = 0,
        ai_agent_max_context_chars: int = 90000,
        ai_agent_batch_chars: int = 32000,
        ai_agent_batch_concurrency: int = 4,
    ) -> None:
        self.ai_max_input_chars = ai_max_input_chars
        self.ai_agent_full_context_chars = ai_agent_full_context_chars
        self.ai_agent_max_context_chars = ai_agent_max_context_chars
        self.ai_agent_batch_chars = ai_agent_batch_chars
        self.ai_agent_batch_concurrency = ai_agent_batch_concurrency
        self.ai_provider = "fake"


@pytest.fixture(autouse=True)
def _tool_fallback_for_agent_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具循环用例显式走大范围 fallback；全量上下文用例单独覆盖阈值。"""
    settings = _SettingsStub(ai_agent_full_context_chars=0)
    monkeypatch.setattr(agent_service, "runtime_settings", lambda _session: settings)
    import app.services.agent_query_plan as query_plan

    monkeypatch.setattr(query_plan, "business_date", lambda _now: date(2026, 9, 8))



# ---------------------------------------------------------------------------
# 全量上下文：小范围不再做有损的关键词检索
# ---------------------------------------------------------------------------


def _drive_with_settings(monkeypatch, **kwargs) -> _SettingsStub:
    settings = _SettingsStub(**kwargs)
    monkeypatch.setattr(agent_service, "runtime_settings", lambda session: settings)
    return settings


def test_agent_can_read_imported_jiandaoyun_history(db_session: Session) -> None:
    """Imported history remains searchable even when its employee is resigned."""
    report = _report(db_session, "旧平台周报", "简道云历史项目已完成交接。", OLDER_WEEK)
    report.source = "jiandaoyun"
    employee = db_session.get(Employee, report.employee_id)
    assert employee is not None
    employee.status = EmployeeStatus.RESIGNED
    db_session.commit()

    context = load_full_context(_ctx(db_session, default_scope="all"), budget=100000, scope="all")
    assert context["enabled"] is True
    assert report.id in context["report_ids"]
    assert "简道云历史项目已完成交接" in context["text"]

    search = _call_tool(_ctx(db_session, default_scope="all"), "search_reports", {
        "keyword": "历史项目", "scope": "all",
    })
    assert report.id in [item["report_id"] for item in search["reports"]]

def test_load_full_context_includes_all_reports_in_period(db_session: Session) -> None:
    """范围装得下时必须给出全部原文，并且篇数与 ID 可核对。"""
    first = _report(db_session, "甲的报告", "甲做了支付联调。")
    second = _report(db_session, "乙的报告", "乙做了风控上线。")
    _report(db_session, "上周的", "不该进本周期全量。", OLDER_WEEK)

    result = load_full_context(_ctx(db_session), budget=100000)

    assert result["enabled"] is True
    assert result["report_count"] == 2
    assert sorted(result["report_ids"]) == sorted([first.id, second.id])
    assert "甲做了支付联调" in result["text"]
    assert "乙做了风控上线" in result["text"]
    assert "不该进本周期全量" not in result["text"], "全量上下文必须严格限定在所选周期"


def test_department_scope_filters_full_context_and_uses_department_prompt(
    db_session: Session, monkeypatch
) -> None:
    """部门详情进入 Agent 后，全文、提示词和引用都必须锁定该部门。"""
    selected = Department(external_id="dept-selected", name="技术部", source="wecom")
    other = Department(external_id="dept-other", name="市场部", source="wecom")
    db_session.add_all([selected, other])
    db_session.flush()
    selected_employee = Employee(
        source="wecom",
        external_id="selected-employee",
        name="技术同事",
        department_id=selected.id,
        status=EmployeeStatus.ACTIVE,
    )
    other_employee = Employee(
        source="wecom",
        external_id="other-employee",
        name="市场同事",
        department_id=other.id,
        status=EmployeeStatus.ACTIVE,
    )
    db_session.add_all([selected_employee, other_employee])
    db_session.commit()
    selected_report = _report(
        db_session, "技术部报告", "技术部完成了接口联调。", employee=selected_employee
    )
    other_report = _report(
        db_session, "市场部报告", "市场部完成了渠道复盘。", employee=other_employee
    )
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=100000)
    fake = _provider(
        monkeypatch,
        [],
        response=f"技术部完成接口联调。[#{selected_report.id}]",
    )

    result = load_full_context(_ctx(db_session, department_id=selected.id), budget=100000)
    payload = agent_service.run_agent(
        db_session,
        "本期做了什么？",
        start="2026-09-07",
        end="2026-09-13",
        department_id=selected.id,
    )

    assert result["report_ids"] == [selected_report.id]
    assert "技术部完成了接口联调" in result["text"]
    assert "市场部完成了渠道复盘" not in result["text"]
    sent_messages = fake.message_calls[0]["messages"]
    assert "当前请求来自公司经营看板的「部门」视图" in sent_messages[0]["content"]
    assert "本轮实际部门：技术部" in sent_messages[-1]["content"]
    assert "市场部完成了渠道复盘" not in sent_messages[-1]["content"]
    assert payload["department_id"] == selected.id
    assert payload["department_name"] == "技术部"
    assert payload["surface"] == "operating_dashboard"
    assert payload["dashboard_view"] == "department"
    assert payload["valid_citation_ids"] == [selected_report.id]
    assert payload["valid_citation_ids"] != [other_report.id]


def test_load_full_context_disabled_when_too_large(db_session: Session) -> None:
    """装不下时明确禁用并回退检索，而不是悄悄截断成一个残缺的「全量」。"""
    _report(db_session, "超长", "内容" * 500)
    _report(db_session, "也长", "内容" * 500)

    result = load_full_context(_ctx(db_session), budget=100)

    assert result["enabled"] is False
    assert result["reason"] == "too_large"
    assert result["text"] == ""


def test_agent_prefetches_full_context_and_skips_search(
    db_session: Session, monkeypatch
) -> None:
    """小范围问题：全文直接进上下文，模型不必（也不该）先检索。"""
    report = _report(db_session, "本周进展", "本周完成了支付接口联调。")
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=100000)
    fake = _provider(monkeypatch, [], response=f"完成了支付联调。[#{report.id}]")

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["full_context"]["enabled"] is True
    assert payload["full_context"]["report_count"] == 1
    assert payload["steps"] == [], "全量上下文已给全，不该再检索"
    user_message = fake.message_calls[0]["messages"][-1]["content"]
    assert "全部 1 篇周报原文" in user_message
    assert "本周完成了支付接口联调" in user_message
    # 全量上下文里的周报也必须可被引用
    assert payload["valid_citation_ids"] == [report.id]
    assert payload["sources"][0]["report_id"] == report.id


def test_agent_reads_every_report_for_named_employee_across_all_periods(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dept = Department(external_id="leo-history-dept", name="技术部", source="wecom")
    db_session.add(dept)
    db_session.flush()
    employee = Employee(
        source="wecom", external_id="leo-history", name="Leo 游丰奕",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    )
    db_session.add(employee)
    db_session.commit()
    reports = []
    for index in range(15):
        start = date.fromordinal(date(2026, 6, 1).toordinal() + 7 * index)
        end = date.fromordinal(start.toordinal() + 6)
        reports.append(_report(
            db_session, f"Leo 第 {index + 1} 周", f"第 {index + 1} 周完成的工作。",
            (start, end), employee=employee,
        ))
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=60000)
    fake = _provider(monkeypatch, [], response=f"共 15 篇。[#{reports[0].id}]")

    payload = agent_service.run_agent(
        db_session, "分析leo的全部周报", scope="period",
        start="2026-09-07", end="2026-09-13",
    )

    assert payload["scope"] == "all"
    assert payload["employee_id"] == employee.id
    assert payload["full_context"]["enabled"] is True
    assert payload["full_context"]["report_count"] == 15
    assert payload["read_count"] == 15
    assert payload["coverage_status"] == "complete"
    assert payload["steps"] == []
    material = fake.message_calls[0]["messages"][-1]["content"]
    assert "全部 15 篇周报原文" in material
    assert all(f"周报ID: {report.id}" in material for report in reports)


def test_agent_followup_inherits_employee_from_same_conversation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dept = Department(external_id="followup-dept", name="技术部", source="wecom")
    db_session.add(dept)
    db_session.flush()
    employee = Employee(
        source="wecom", external_id="followup-leo", name="Leo 游丰奕",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    )
    db_session.add(employee)
    db_session.commit()
    report = _report(
        db_session, "Leo 上月工作", "完成客服问题修复。", OLDER_WEEK, employee=employee
    )
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=100000)
    _provider(monkeypatch, [], response=f"完成客服问题修复。[#{report.id}]")

    agent_service.run_agent(
        db_session, "分析Leo的全部周报", scope="all",
        start="2026-08-31", end="2026-09-06", conversation_id="followup-chat",
    )
    followup = agent_service.run_agent(
        db_session, "那他上个月做了什么？", scope="period",
        start="2026-09-07", end="2026-09-13", conversation_id="followup-chat",
    )

    assert followup["employee_id"] == employee.id
    assert followup["period"] == {"start": "2026-08-01", "end": "2026-08-31"}


def test_agent_falls_back_to_search_when_context_too_large(
    db_session: Session, monkeypatch
) -> None:
    """范围过大（如全部周期）时回退到工具检索，全量上下文关闭。"""
    report = _report(db_session, "本周进展", "本周内容。" * 200)
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=10)
    fake = _provider(
        monkeypatch,
        [[_tool("search_reports", {"keyword": "内容"})]],
        response=f"已作答。[#{report.id}]",
    )

    payload = agent_service.run_agent(
        db_session, "这周做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["full_context"]["enabled"] is False
    assert [step["tool"] for step in payload["steps"]] == ["search_reports"]


def test_agent_all_scope_falls_back_when_library_exceeds_budget(
    db_session: Session, monkeypatch
) -> None:
    """「全部周期」的库超过预算时必须回退检索，而不是把整库塞进一次调用。

    全量上下文是「装得下就全给」，与范围无关 —— 但历史库会无限增长，
    所以真正的保证是：超预算就退回工具检索 + 分页，绝不静默截断。
    """
    for index in range(4):
        _report(
            db_session,
            f"报告{index}",
            "内容" * 400,
            submitted_at=datetime(2026, 9, 10, 10, index, 0),
        )
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=1000)
    _provider(monkeypatch, [], response="已作答。")

    payload = agent_service.run_agent(
        db_session,
        "所有历史里有什么？",
        scope="all",
        start="2026-09-07",
        end="2026-09-13",
    )

    assert payload["full_context"]["enabled"] is False
    assert payload["full_context"]["reason"] == "too_large"
    assert "no_search_after_fallback" in payload["coverage_gaps"]
    assert payload["coverage_status"] == "incomplete"


def test_agent_reads_large_explicit_scope_in_batches_and_confirms_coverage(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(
        db_session, "年度事项", "全年持续推进的具体工作。" * 1600,
        (date(2026, 1, 5), date(2026, 1, 11)),
    )
    _drive_with_settings(
        monkeypatch, ai_agent_full_context_chars=100, ai_agent_batch_chars=8000
    )
    fake = _provider(monkeypatch, [], response=f"年度问题已归纳。[#{report.id}]")

    events = list(agent_service.stream_agent(
        db_session,
        "今年的周报全部拿来分析，总结三个问题给我",
        scope="all",
        start="2026-09-07",
        end="2026-09-13",
    ))
    payload = events[-1]["payload"]

    assert payload["period"] == {"start": "2026-01-01", "end": "2026-12-31"}
    assert payload["batch_read"]["enabled"] is True
    assert payload["batch_read"]["report_count"] == 1
    assert payload["batch_read"]["batch_count"] >= 2
    assert payload["read_count"] == 1
    assert payload["coverage_status"] == "complete"
    assert payload["valid_citation_ids"] == [report.id]
    assert all(event["type"] in {"step", "progress", "delta"} for event in events[:-1])
    batch_prompts = [call["messages"][-1]["content"] for call in fake.message_calls]
    assert any("第 1/" in prompt for prompt in batch_prompts)
    assert any("第 2/" in prompt for prompt in batch_prompts)


def test_agent_batches_an_over_budget_previous_month_summary(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(
        db_session,
        "上月事项",
        "上月完成跨部门项目对接与交付。" * 500,
        (date(2026, 8, 10), date(2026, 8, 16)),
    )
    _drive_with_settings(
        monkeypatch,
        ai_agent_full_context_chars=100,
        ai_agent_batch_chars=8000,
    )
    _provider(monkeypatch, [], response=f"上月工作已归纳。[#{report.id}]")

    payload = agent_service.run_agent(
        db_session,
        "上月公司主要做了什么？",
        scope="period",
        start="2026-09-07",
        end="2026-09-13",
    )

    assert payload["period"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert payload["batch_read"]["enabled"] is True
    assert payload["read_count"] == 1
    assert payload["coverage_status"] == "complete"


def test_batch_read_includes_every_report_across_periods(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [
        _report(
            db_session, f"历史周报{index}", f"第 {index} 篇的独立工作。" * 180,
            (date(2026, 6, 22), date(2026, 6, 28)) if index < 8 else THIS_WEEK,
        )
        for index in range(15)
    ]
    _drive_with_settings(
        monkeypatch, ai_agent_full_context_chars=100, ai_agent_batch_chars=16000
    )
    fake = _provider(monkeypatch, [], response=f"已综合分析。[#{reports[0].id}]")

    payload = agent_service.run_agent(
        db_session, "分析全部历史周报并总结三个问题", scope="all",
        start="2026-09-07", end="2026-09-13",
    )

    assert payload["batch_read"]["report_count"] == 15
    assert payload["read_count"] == 15
    assert payload["batch_read"]["batch_count"] > 1
    batch_prompts = "\n".join(
        call["messages"][-1]["content"] for call in fake.message_calls[:-1]
    )
    assert all(f"周报ID: {report.id}" in batch_prompts for report in reports)


def test_final_answer_receives_every_batch_after_multilevel_merge(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [
        _report(
            db_session,
            f"合并完整性周报{index}",
            f"第 {index} 篇周报包含一项需要保留的独立进展。" * 180,
            (date(2026, 6, 22), date(2026, 6, 28)),
        )
        for index in range(12)
    ]
    _drive_with_settings(
        monkeypatch,
        ai_agent_full_context_chars=100,
        ai_agent_batch_chars=4000,
    )
    recorded_calls: list[list[dict]] = []

    class IDPreservingProvider(FakeProvider):
        def chat_messages(self, messages, tools=None):
            recorded_calls.append(list(messages))
            text = "\n".join(str(message.get("content") or "") for message in messages)
            present_ids = [
                str(report.id)
                for report in reports
                if f"周报ID: {report.id}" in text or f"[#{report.id}]" in text
            ]
            citations = " ".join(f"[#{report_id}]" for report_id in present_ids)
            system = str(messages[0].get("content") or "")
            if "摘要合并助手" in system:
                answer = f"已合并全部证据 {citations}"
            elif "这是第 " in text:
                answer = f"本批摘要 {citations}"
            else:
                answer = f"已综合全部批次 [#{reports[0].id}]"
            return self._result(answer)

    monkeypatch.setattr(
        agent_service, "get_provider", lambda settings: IDPreservingProvider()
    )
    payload = agent_service.run_agent(
        db_session,
        "总结全部历史周报，提炼每篇的项目进展",
        scope="all",
        start="2026-09-07",
        end="2026-09-13",
    )

    assert payload["batch_read"]["batch_count"] >= 10
    final_prompt = recorded_calls[-1][-1]["content"]
    assert all(f"[#{report.id}]" in final_prompt for report in reports)
    assert payload["read_count"] == len(reports)
    assert payload["coverage_status"] == "complete"


def test_large_batch_count_can_merge_beyond_five_levels(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Lock

    report = _report(
        db_session,
        "多轮摘要合并用例",
        "本条周报用于验证很多批次摘要必须继续归并，不能在固定轮数时失败。" * 20,
        OLDER_WEEK,
    )
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=100)
    monkeypatch.setattr(
        agent_service,
        "load_report_batches",
        lambda *_args, **_kwargs: {
            "report_count": 641,
            "batches": [f"周报ID: {report.id}\n第 {index} 批原文" for index in range(38)],
            "report_ids": [report.id],
        },
    )

    verbose_summary = "x" * 22000
    merge_calls = 0
    merge_lock = Lock()

    class VerboseSummaryProvider(FakeProvider):
        def chat_messages(self, messages, tools=None):
            nonlocal merge_calls
            system = str(messages[0].get("content") or "")
            if "摘要合并助手" in system:
                with merge_lock:
                    merge_calls += 1
                return self._result(f"合并摘要：{verbose_summary} [#{report.id}]")
            if "这是第 " in str(messages[-1].get("content") or ""):
                return self._result(f"本批摘要：{verbose_summary} [#{report.id}]")
            return self._result(f"全部历史已完成综合分析 [#{report.id}]")

    monkeypatch.setattr(
        agent_service, "get_provider", lambda _settings: VerboseSummaryProvider()
    )

    payload = agent_service.run_agent(
        db_session,
        "总结全部历史周报，梳理主要进展和风险",
        scope="all",
        start="2026-09-07",
        end="2026-09-13",
    )

    assert payload["batch_read"]["report_count"] == 641
    assert payload["batch_read"]["batch_count"] == 38
    assert merge_calls > 5
    assert payload["answer"] == f"全部历史已完成综合分析 [#{report.id}]"
    assert payload["coverage_status"] == "complete"


def test_agent_reads_batches_with_bounded_parallelism(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Lock
    from time import sleep

    reports = [
        _report(
            db_session,
            f"并行历史周报{index}",
            f"第 {index} 篇的独立工作内容。" * 220,
            (date(2026, 6, 22), date(2026, 6, 28)),
        )
        for index in range(6)
    ]
    _drive_with_settings(
        monkeypatch,
        ai_agent_full_context_chars=100,
        ai_agent_batch_chars=4000,
        ai_agent_batch_concurrency=3,
    )
    active = 0
    max_active = 0
    lock = Lock()

    class SlowFakeProvider(FakeProvider):
        def chat_messages_stream(self, messages, tools=None):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                sleep(0.03)
                yield from super().chat_messages_stream(messages, tools)
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(
        agent_service,
        "get_provider",
        lambda settings: SlowFakeProvider(response=f"汇总完成 [#{reports[0].id}]"),
    )

    payload = agent_service.run_agent(
        db_session,
        "分析全部历史周报并总结主要进展",
        scope="all",
        start="2026-09-07",
        end="2026-09-13",
    )

    assert payload["batch_read"]["report_count"] == len(reports)
    assert payload["batch_read"]["batch_count"] >= 4
    assert payload["batch_read"]["concurrency"] == 3
    assert 1 < max_active <= 3
    assert payload["answer"].startswith("汇总完成")


def test_agent_marks_unread_reports_after_fallback_incomplete(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [_report(db_session, f"历史周报{index}", "工作内容。" * 30) for index in range(3)]
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=10)
    _provider(monkeypatch, [
        [_tool("search_reports", {"scope": "all", "limit": 20})],
        [_tool("get_reports", {"report_ids": [reports[0].id]}, "read_one")],
    ], response=f"已阅读一篇。[#{reports[0].id}]")

    payload = agent_service.run_agent(
        db_session, "查找全部历史周报里支付相关内容", scope="all",
        start="2026-09-07", end="2026-09-13",
    )
    assert payload["read_count"] == 1
    assert "unread_search_results" in payload["coverage_gaps"]
    assert payload["coverage_status"] == "incomplete"


# ---------------------------------------------------------------------------
# 卡点 / 任务统计：AC-16 要求「必须实际统计」
# ---------------------------------------------------------------------------


def _task_with_blocker(
    session: Session,
    *,
    title: str,
    department: Department | None,
    duration_weeks: int = 1,
    risk_level: str = "低",
    status: str = "OPEN",
    task_status: str = "IN_PROGRESS",
    plan_reports: int = 0,
) -> tuple[Task, Blocker]:
    employee = Employee(
        source="wecom",
        external_id=f"emp_{title}",
        name=f"{title}负责人",
        status=EmployeeStatus.ACTIVE,
        department_id=department.id if department else None,
    )
    session.add(employee)
    session.flush()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id=f"rep_{title}",
        report_type="weekly",
        title=title,
        content=f"{title} 的正文",
        content_raw=f"{title} 的正文",
        start_date=THIS_WEEK[0],
        end_date=THIS_WEEK[1],
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.flush()
    item = ReportItem(
        report_id=report.id,
        item_type="blockers",
        item_order=1,
        title=title,
        description=f"{title} 的描述",
        source_text=f"{title} 的原文片段",
    )
    session.add(item)
    session.flush()
    task = Task(
        canonical_key=f"key_{title}",
        title=title,
        status=task_status,
        first_seen_date=THIS_WEEK[0],
        last_seen_date=THIS_WEEK[1],
        duration_weeks=duration_weeks,
    )
    session.add(task)
    session.flush()
    session.add(
        TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)
    )
    blocker = Blocker(
        task_id=task.id,
        title=title,
        description=f"{title} 的描述",
        status=status,
        risk_level=risk_level,
        first_seen_date=THIS_WEEK[0],
        last_seen_date=THIS_WEEK[1],
        duration_weeks=duration_weeks,
        occurrence_count=duration_weeks,
    )
    session.add(blocker)
    session.commit()
    return task, blocker


def _add_plan_evidence(
    session: Session,
    task: Task,
    reports: int,
    department: Department | None = None,
    index_offset: int = 0,
) -> None:
    """给任务补 N 篇把「下周计划」写进周报的证据。

    「没推进」的权威口径要求任务连续出现在下周计划里，所以测试必须造出这层证据，
    否则测到的是另一个语义。
    """
    for index in range(index_offset, index_offset + reports):
        employee = Employee(
            source="wecom",
            external_id=f"plan_emp_{task.id}_{index}",
            name=f"计划人{index}",
            status=EmployeeStatus.ACTIVE,
            department_id=department.id if department else None,
        )
        session.add(employee)
        session.flush()
        report = WeeklyReport(
            employee_id=employee.id,
            external_id=f"plan_rep_{task.id}_{index}",
            report_type="weekly",
            title=f"{task.title} 第{index}周计划",
            content="下周继续推进。",
            content_raw="下周继续推进。",
            start_date=THIS_WEEK[0],
            end_date=THIS_WEEK[1],
            submitted_at=datetime(2026, 9, 10, 10, 0, 0),
            source="wecom",
        )
        session.add(report)
        session.flush()
        item = ReportItem(
            report_id=report.id,
            item_type="next_week_plans",
            item_order=1,
            title=task.title,
            source_text=f"{task.title} 下周计划",
        )
        session.add(item)
        session.flush()
        session.add(
            TaskReportLink(task_id=task.id, report_id=report.id, report_item_id=item.id)
        )
    session.commit()


def test_department_scope_filters_period_employee_and_task_evidence_tools(
    db_session: Session,
) -> None:
    selected = Department(source="wecom", external_id="d_selected", name="技术部")
    other = Department(source="wecom", external_id="d_other", name="市场部")
    db_session.add_all([selected, other])
    db_session.commit()

    task, _ = _task_with_blocker(
        db_session,
        title="技术部跨部门卡点",
        department=selected,
        duration_weeks=4,
    )
    selected_report = db_session.scalar(
        select(WeeklyReport).where(WeeklyReport.title == "技术部跨部门卡点")
    )
    other_employee = Employee(
        source="wecom",
        external_id="market-task-owner",
        name="市场同事",
        status=EmployeeStatus.ACTIVE,
        department_id=other.id,
    )
    db_session.add(other_employee)
    db_session.flush()
    other_report = _report(
        db_session,
        "市场部共同证据",
        "市場部共同证据正文",
        employee=other_employee,
    )
    other_item = ReportItem(
        report_id=other_report.id,
        item_type="blockers",
        item_order=1,
        title=task.title,
        source_text="市场部的同一事项",
    )
    db_session.add(other_item)
    db_session.flush()
    db_session.add(
        TaskReportLink(task_id=task.id, report_id=other_report.id, report_item_id=other_item.id)
    )
    other_history = _report(
        db_session,
        "市场部历史周期",
        "市场部历史正文",
        OLDER_WEEK,
        employee=other_employee,
    )
    _add_plan_evidence(db_session, task, 3, selected)
    _add_plan_evidence(db_session, task, 1, other, index_offset=10)

    ctx = _ctx(db_session, department_id=selected.id)
    periods = _call_tool(ctx, "list_periods", {})
    employees = _call_tool(ctx, "list_employees", {})
    requested_other_employees = _call_tool(
        ctx, "list_employees", {"department_id": other.id}
    )
    blockers = _call_tool(ctx, "list_blockers", {})
    tasks = _call_tool(ctx, "list_tasks", {"min_weeks": 3})

    assert [item["start"] for item in periods["periods"]] == [THIS_WEEK[0].isoformat()]
    assert all(item["department"] == "技术部" for item in employees["employees"])
    assert requested_other_employees["employees"] == []
    assert blockers["by_department"] == [
        {
            "department": "技术部",
            "blocker_count": 1,
            "high_risk_count": 0,
            "long_running_count": 1,
        }
    ]
    for items in (blockers["items"], tasks["items"]):
        source_ids = items[0]["source_report_ids"]
        assert selected_report.id in source_ids
        assert other_report.id not in source_ids
        assert all(
            db_session.scalar(
                select(WeeklyReport.id)
                .join(Employee, Employee.id == WeeklyReport.employee_id)
                .where(
                    WeeklyReport.id == report_id,
                    Employee.department_id == selected.id,
                )
            )
            == report_id
            for report_id in source_ids
        )
    assert other_history.id not in blockers["items"][0]["source_report_ids"]
    assert other_history.id not in tasks["items"][0]["source_report_ids"]


def test_list_blockers_counts_by_department(db_session: Session) -> None:
    """「哪个部门卡点最多」必须是真实计数，且能指出最多的那个部门。"""
    market = Department(source="wecom", external_id="d_market", name="市场部")
    service = Department(source="wecom", external_id="d_service", name="申请服务部")
    db_session.add_all([market, service])
    db_session.commit()

    _task_with_blocker(db_session, title="市场卡点甲", department=market)
    _task_with_blocker(db_session, title="市场卡点乙", department=market)
    _task_with_blocker(db_session, title="服务卡点丙", department=service)

    result = _call_tool(_ctx(db_session), "list_blockers", {})

    assert result["total"] == 3
    assert result["top_department"] == "市场部"
    counts = {row["department"]: row["blocker_count"] for row in result["by_department"]}
    assert counts == {"市场部": 2, "申请服务部": 1}


def test_list_blockers_filters_high_risk_and_long_running(db_session: Session) -> None:
    """高风险与长期卡点要能被单独筛出来，这是 AC-16 的常见问法。"""
    dept = Department(source="wecom", external_id="d1", name="市场部")
    db_session.add(dept)
    db_session.commit()

    _task_with_blocker(db_session, title="高风险", department=dept, risk_level="高")
    _task_with_blocker(
        db_session, title="长期", department=dept, duration_weeks=5, risk_level="中"
    )
    _task_with_blocker(db_session, title="普通", department=dept)

    long_result = _call_tool(_ctx(db_session), "list_blockers", {"min_weeks": 3})
    assert long_result["total"] == 1
    assert long_result["items"][0]["title"] == "长期"

    high_result = _call_tool(_ctx(db_session), "list_blockers", {"risk_level": "高"})
    assert high_result["total"] == 1
    assert high_result["items"][0]["title"] == "高风险"


def test_list_blockers_excludes_resolved_by_default(db_session: Session) -> None:
    """默认只看未解决卡点，否则「当前还有多少卡点」会被历史数据撑大。"""
    dept = Department(source="wecom", external_id="d1", name="市场部")
    db_session.add(dept)
    db_session.commit()
    _task_with_blocker(db_session, title="还在", department=dept, status="OPEN")
    _task_with_blocker(db_session, title="已解决", department=dept, status="RESOLVED")

    assert _call_tool(_ctx(db_session), "list_blockers", {})["total"] == 1
    assert _call_tool(_ctx(db_session), "list_blockers", {"status": "ALL"})["total"] == 2

def test_list_tasks_finds_stalled_work(db_session: Session) -> None:
    """「哪些事情连续多周没有推进」按权威口径：连续出现在下周计划且仍未完成。"""
    dept = Department(source="wecom", external_id="d1", name="市场部")
    db_session.add(dept)
    db_session.commit()
    stalled, _ = _task_with_blocker(
        db_session, title="拖了五周", department=dept, duration_weeks=5
    )
    _add_plan_evidence(db_session, stalled, 3, dept)

    thin, _ = _task_with_blocker(
        db_session, title="只出现两周", department=dept, duration_weeks=2
    )
    _add_plan_evidence(db_session, thin, 3, dept)

    no_evidence, _ = _task_with_blocker(
        db_session, title="没有计划证据", department=dept, duration_weeks=5
    )

    done, _ = _task_with_blocker(
        db_session,
        title="已完成",
        department=dept,
        duration_weeks=8,
        task_status="COMPLETED",
    )
    _add_plan_evidence(db_session, done, 3, dept)

    result = _call_tool(_ctx(db_session), "list_tasks", {"min_weeks": 3})

    titles = [item["title"] for item in result["items"]]
    assert titles == ["拖了五周"], "只有「持续 ≥3 周 + 连续出现在下周计划 + 未完成」才算没推进"
    assert result["items"][0]["duration_weeks"] == 5
    assert result["items"][0]["department"] == "市场部"
    assert result["items"][0]["plan_report_count"] == 3


def test_list_tasks_excludes_resource_demand_entries(db_session: Session) -> None:
    """资源需求不是「未推进的工作」，不能被列成停滞任务。

    必须走 `materialize_resource_demand` 真实写入路径：本用例原先手写
    `task.canonical_key = "resource_demand:..."`，而生产里 canonical_key 由
    `_canonical_key` 生成、会剥掉冒号与下划线 —— 于是测试验证的是一个生产里
    不可能出现的字段值，把「排除条件恒真、等于没写」这个缺陷放了过去。
    夹具不能用「No additional resources are required」这类占位回答：
    物化层接宽口径判空后它们根本不会生成任务，必须用真实诉求。
    """
    dept = Department(source="wecom", external_id="d1", name="市场部")
    db_session.add(dept)
    db_session.commit()

    report = _report(
        db_session,
        "资源需求周报",
        "重点工作：推进中\n资源需求：需要市场经理确认投放预算\n下周计划：继续推进",
    )
    materialize_resource_demand(db_session, report)
    task = db_session.scalar(
        select(Task)
        .join(TaskReportLink, TaskReportLink.task_id == Task.id)
        .join(ReportItem, ReportItem.id == TaskReportLink.report_item_id)
        .where(ReportItem.task_key.like("resource_demand:%"))
    )
    assert task is not None, "资源需求应生成一个带前缀 task_key 的任务"
    assert not task.canonical_key.startswith("resource_demand:"), (
        "生产里 canonical_key 不会保留前缀，这正是旧过滤条件匹配不到的原因"
    )
    task.duration_weeks = 9
    db_session.commit()
    _add_plan_evidence(db_session, task, 3, dept)

    result = _call_tool(_ctx(db_session), "list_tasks", {"min_weeks": 3})
    assert result["items"] == []


def test_list_tasks_still_reports_real_stalled_work(db_session: Session) -> None:
    """排除资源需求不能把真实停滞任务一起误伤（守护上一条的边界）。"""
    dept = Department(source="wecom", external_id="d1", name="市场部")
    db_session.add(dept)
    db_session.commit()
    task, _ = _task_with_blocker(
        db_session, title="拖了五周", department=dept, duration_weeks=5
    )
    _add_plan_evidence(db_session, task, 3, dept)

    result = _call_tool(_ctx(db_session), "list_tasks", {"min_weeks": 3})
    assert [item["title"] for item in result["items"]] == ["拖了五周"]


def test_list_blockers_exposes_citable_report_ids(db_session: Session) -> None:
    """卡点统计必须带可引用的周报 ID。

    实测踩到：模型拿不到证据周报 ID 时会去引用 blocker_id（如 [#65]），
    而引用体系只认周报 ID —— 既拿不到可点来源，也存在「卡点 id 恰好等于
    某篇周报 id 时指向错周报」的风险。这里锁住 source_report_ids 的存在与可引用性。
    """
    dept = Department(source="wecom", external_id="d1", name="市场部")
    db_session.add(dept)
    db_session.commit()
    _task_with_blocker(db_session, title="卡点甲", department=dept)

    ctx = _ctx(db_session)
    result = _call_tool(ctx, "list_blockers", {})

    source_ids = result["items"][0]["source_report_ids"]
    assert source_ids, "卡点必须带证据周报 ID，否则无法追溯"
    # 关键：这些必须是 WeeklyReport 的 id，而不是 blocker/task 的 id。
    # 两个编号空间独立且可能恰好相等（本用例首个 blocker 与首篇周报都是 1），
    # 所以断言它们确实对应到周报，才能证明引用不会指错。
    existing = set(
        db_session.scalars(
            select(WeeklyReport.id).where(WeeklyReport.id.in_(source_ids))
        )
    )
    assert existing == set(source_ids)
    # 登记为可引用：模型引用它时不能被当成编造丢弃
    assert set(source_ids) <= ctx.offered_report_ids


def test_agent_can_cite_evidence_from_blocker_stats(db_session: Session, monkeypatch) -> None:
    """引用统计工具给的证据周报 ID 时，必须产出可点来源（需求 3.2 可追溯）。"""
    dept = Department(source="wecom", external_id="d1", name="市场部")
    db_session.add(dept)
    db_session.commit()
    _task_with_blocker(db_session, title="卡点甲", department=dept)
    _drive_with_settings(monkeypatch, ai_agent_full_context_chars=0)
    fake = _provider(
        monkeypatch,
        [[_tool("list_blockers", {})]],
        response="答案",
    )
    # 从工具真实返回值里取证据 ID，模拟模型照实引用
    payload = agent_service.run_agent(
        db_session, "哪个部门卡点最多？", start="2026-09-07", end="2026-09-13"
    )
    structured = fake.message_calls[0]["messages"][-1]["content"]
    tool_output = json.loads(structured.rsplit("：\n", 1)[1])
    evidence_ids = tool_output["items"][0]["source_report_ids"]

    assert evidence_ids, "工具必须提供证据周报 ID"


def test_search_reports_offset_cap_does_not_loop_forever(db_session: Session) -> None:
    """命中集超过分页上限时，必须诚实收尾，不能给出一个做不到的 next_offset。

    真实缺陷：返回的 next_offset 没有被 cap 限制，于是 offset=220 会被夹回 200、
    返回同一页、却仍报 has_more=true / next_offset=220。模型按提示词「翻到
    has_more 为 false」，永远翻不到，把剩余步数全部烧在重复读同一页上。
    """
    from app.services.agent_tools import SEARCH_MAX_OFFSET

    total = SEARCH_MAX_OFFSET + 25
    for index in range(total):
        _report(
            db_session,
            f"海量第{index}篇",
            f"内容 {index}",
            submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        )

    ctx = _ctx(db_session)
    # 从最后一页继续翻：offset=180 仍可达（下一页正好是 200，是合法 offset）
    page = _call_tool(
        ctx, "search_reports", {"limit": 20, "offset": SEARCH_MAX_OFFSET - 20}
    )
    assert page["matched_total"] == total
    assert page["offset"] == SEARCH_MAX_OFFSET - 20
    assert page["has_more"] is True
    assert page["next_offset"] == SEARCH_MAX_OFFSET

    # 关键一步：到达 cap 之后必须收尾，不能再回绕到 200 反复读同一页
    last = _call_tool(
        ctx, "search_reports", {"limit": 20, "offset": page["next_offset"]}
    )
    assert last["offset"] == SEARCH_MAX_OFFSET
    assert last["has_more"] is False
    assert last["next_offset"] is None
    assert last["truncated_reason"] == "offset_cap"
    # 两页不能重叠（回绕的典型症状就是读到同一批数据）
    read_ids = {item["report_id"] for item in page["reports"]} | {
        item["report_id"] for item in last["reports"]
    }
    assert len(read_ids) == len(page["reports"]) + len(last["reports"])

    # 越界请求同样必须收尾而不是回绕到 cap 上重复读
    beyond = _call_tool(ctx, "search_reports", {"limit": 20, "offset": 99999})
    assert beyond["offset"] == SEARCH_MAX_OFFSET
    assert beyond["has_more"] is False
    assert beyond["next_offset"] is None
    assert beyond["truncated_reason"] == "offset_cap"

    # 正常翻页路径不受影响：结果集装得下时，必须能一路翻到底并如实报 has_more
    normal = _call_tool(ctx, "search_reports", {"limit": 20, "offset": 0})
    assert normal["has_more"] is True
    assert normal["next_offset"] == 20
    assert normal["truncated_reason"] is None


# --------------------------------------------------------------------------- #
# 总输入预算：全量上下文 + 所有工具结果共享上限
# --------------------------------------------------------------------------- #


def test_tool_output_is_truncated_and_marked_when_budget_exhausted(monkeypatch) -> None:
    """工具结果超出剩余额度时必须截断，并带显式标注。

    不能静默截断：模型会把残缺数据当成完整数据转述给管理层。
    """
    from app.services.agent_service import (
        TOOL_TRUNCATED_MARKER,
        _clamp_tool_output,
    )

    big = "x" * 5000
    clamped, truncated = _clamp_tool_output(big, 1000)
    assert truncated is True
    assert len(clamped) <= 1000
    assert TOOL_TRUNCATED_MARKER in clamped

    # 额度够用时原样返回
    same, truncated = _clamp_tool_output(big, 99999)
    assert truncated is False
    assert same == big


def test_tool_output_dropped_when_no_budget_left() -> None:
    """额度为 0 时不能塞空 content，要给一条可读的占位说明。"""
    from app.services.agent_service import (
        TOOL_DROPPED_PLACEHOLDER,
        _clamp_tool_output,
    )

    dropped, truncated = _clamp_tool_output("x" * 100, 0)
    assert truncated is True
    assert dropped == TOOL_DROPPED_PLACEHOLDER
    assert dropped.strip(), "tool 消息不能为空，否则模型会困惑"


def test_agent_stops_early_when_context_budget_exhausted(db_session, monkeypatch) -> None:
    """预算触顶时必须提前收尾，且如实标记 context_budget.exhausted。

    真实风险：messages 每轮全量重发，成本随轮数上升；工具单项上限很宽，
    几步就能撑爆上下文并让整轮问答 400 失败。这里用一个「每次返回巨量结果」
    的 stub 工具验证预算确实生效。
    """
    _report(db_session, "预算法周报", "内容", submitted_at=datetime(2026, 9, 10, 10, 0, 0))

    class HugeProvider:
        """每次都要求调工具，并让工具结果非常大（不能预取全文，否则一开始就撑满）。"""

        def __init__(self) -> None:
            self.calls = 0

        def chat_messages_stream(self, messages, tools):
            from app.ai.base import LLMStreamChunk, LLMResult, ToolCall

            self.calls += 1
            if self.calls <= 3 and tools:
                result = LLMResult(
                    content="",
                    model="stub",
                    prompt_tokens=1,
                    completion_tokens=1,
                    tool_calls=[
                        ToolCall(
                            id=f"call-{self.calls}",
                            name="search_reports",
                            arguments='{"limit": 20}',
                        )
                    ],
                )
            else:
                result = LLMResult(
                    content="最终回答 [#1]",
                    model="stub",
                    prompt_tokens=1,
                    completion_tokens=1,
                    tool_calls=[],
                )
            yield LLMStreamChunk(text=result.content, done=True, result=result)

    # 预算设成「系统提示词 + 一点余量」：太小的话基线（system prompt 本身
    # 无法裁剪）就已经超限，测到的就不是工具结果的裁剪了。
    from app.ai.prompts import AGENT_SYSTEM

    limit = len(AGENT_SYSTEM) + 1200
    settings = _SettingsStub(
        ai_agent_full_context_chars=0,
        ai_agent_max_context_chars=limit,
    )
    monkeypatch.setattr(agent_service, "runtime_settings", lambda session: settings)
    monkeypatch.setattr(agent_service, "get_provider", lambda s: HugeProvider())
    # 让工具输出足够大，逼近预算
    monkeypatch.setattr(
        agent_service, "execute_tool", lambda ctx, name, args: "y" * 3000
    )

    payload = agent_service.run_agent(
        db_session, "预算法问题", start="2026-09-07", end="2026-09-13"
    )

    budget = payload["context_budget"]
    assert budget["limit"] == limit
    # 工具结果必须被压进剩余额度。允许的少量超出仅来自固定的收尾提示词
    # （不可裁剪，否则模型会继续要工具、更贵）。
    from app.ai.prompts import AGENT_FINAL_HINT

    assert budget["used"] <= budget["limit"] + len(AGENT_FINAL_HINT)
    assert budget["exhausted"] is True
    assert any(step.get("truncated") for step in payload["steps"]), (
        "被截断的工具步骤必须打标，前端才能提示信息不完整"
    )


def test_agent_budget_not_flagged_when_under_limit(db_session, monkeypatch) -> None:
    """预算充裕时不能被误标 exhausted（否则正常回答会无端显示「信息可能不完整」）。"""
    _report(db_session, "正常周报", "内容", submitted_at=datetime(2026, 9, 10, 10, 0, 0))

    class SmallProvider:
        def __init__(self) -> None:
            self.calls = 0

        def chat_messages_stream(self, messages, tools):
            from app.ai.base import LLMStreamChunk, LLMResult

            self.calls += 1
            result = LLMResult(
                content="回答无需引用",
                model="stub",
                prompt_tokens=1,
                completion_tokens=1,
                tool_calls=[],
            )
            yield LLMStreamChunk(text=result.content, done=True, result=result)

    settings = _SettingsStub(ai_agent_full_context_chars=0)
    monkeypatch.setattr(agent_service, "runtime_settings", lambda session: settings)
    monkeypatch.setattr(agent_service, "get_provider", lambda s: SmallProvider())

    payload = agent_service.run_agent(
        db_session, "小问题", start="2026-09-07", end="2026-09-13"
    )
    assert payload["context_budget"]["exhausted"] is False


# ---------------------------------------------------------------------------
# only_excerpts：检索命中后一篇全文都没读，必须显式标注浅读
# ---------------------------------------------------------------------------


def test_agent_flags_only_excerpts_when_no_full_text_read(
    db_session: Session, monkeypatch
) -> None:
    """模型检索命中后未读任何全文就作答：coverage_gaps 必须带 only_excerpts。"""
    _report(db_session, "本周进展", "完成了支付接口联调，已提测。")
    _provider(
        monkeypatch,
        [[_tool("search_reports", {})]],
        response="本周整体进展平稳。",
    )

    payload = agent_service.run_agent(
        db_session, "公司这周主要做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["read_count"] == 0
    assert "only_excerpts" in payload["coverage_gaps"]
    assert payload["coverage_status"] == "incomplete"
    # 来源列表仍然如实给出「读过摘要」的篇目，供使用者回看
    assert len(payload["sources"]) > 0


def test_agent_no_only_excerpts_when_full_text_read(
    db_session: Session, monkeypatch
) -> None:
    """模型读了全文再作答：不得误标 only_excerpts。"""
    report = _report(db_session, "本周进展", "完成了支付接口联调，已提测。")
    _provider(
        monkeypatch,
        [
            [_tool("search_reports", {})],
            [_tool("get_reports", {"report_ids": [report.id]}, "call_2")],
        ],
        response=f"本周完成了支付接口联调。[#{report.id}]",
    )

    payload = agent_service.run_agent(
        db_session, "公司这周主要做了什么？", start="2026-09-07", end="2026-09-13"
    )

    assert payload["read_count"] == 1
    assert "only_excerpts" not in payload["coverage_gaps"]
    assert payload["cited_count"] == 1


def test_query_plan_collective_words_do_not_clarify(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """集体称谓 + 时间词组合（如「这周整体做了什么」）不得被误判成查不到的人员/部门。"""
    import app.services.agent_query_plan as query_plan

    monkeypatch.setattr(query_plan, "business_date", lambda _now: date(2026, 9, 30))
    dept = Department(external_id="collective-dept", name="技术部", source="wecom")
    db_session.add(dept)
    db_session.flush()
    employee = Employee(
        source="wecom", external_id="collective-emp", name="王明",
        status=EmployeeStatus.ACTIVE, department_id=dept.id,
    )
    db_session.add(employee)
    db_session.commit()
    db_session.info[SCOPE_INFO_KEY] = DataScope("subtree", (dept.id,))

    for question in (
        "这周整体做了什么？",
        "上周大家做了什么？",
        "部门这周做了什么？",
        "这个部门做了什么？",
        "请总结上周各部门的主要进展，并给出可回看的周报原文依据。",
    ):
        plan = resolve_agent_query(
            db_session,
            question=question,
            start="2026-09-07",
            end="2026-09-13",
            scope="period",
            department_id=dept.id,
            employee_id=None,
        )
        assert plan.clarification is None, question

    # 库中不存在的人仍要正常触发澄清，不能因为放宽白名单而漏判
    unknown = resolve_agent_query(
        db_session,
        question="王五这周做了什么？",
        start="2026-09-07",
        end="2026-09-13",
        scope="period",
        department_id=dept.id,
        employee_id=None,
    )
    assert unknown.clarification
    assert unknown.clarification["reason"] == "unknown_employee"


def _seed_analysis(session: Session, report_id: int, summary: str) -> None:
    """给一篇周报播种一条成功的 AI 分析记录（预计算摘要的数据源）。"""
    session.add(
        AIAnalysis(
            entity_type="weekly_report",
            entity_id=report_id,
            analysis_type="report_analysis",
            model_provider="fake",
            model_name="stub",
            result_json={"summary": summary, "score": 80},
            status=AIAnalysisStatus.SUCCESS,
            created_at=datetime(2026, 9, 11, 9, 0, 0),
        )
    )
    session.commit()


def test_search_excerpt_prefers_ai_summary_in_browse_mode(db_session: Session) -> None:
    """浏览模式（无关键词）下摘要优先用入库的 AI 摘要，而不是正文前缀切片。"""
    report = _report(
        db_session,
        "本周进展",
        "开头是常规寒暄内容。" + " filler" * 200 + " 结尾才是重点结论。",
    )
    _seed_analysis(db_session, report.id, "AI 摘要：完成支付接口联调并提测，风险已闭环。")
    ctx = _ctx(db_session)

    result = _call_tool(ctx, "search_reports", {})

    item = result["reports"][0]
    assert item["report_id"] == report.id
    assert item["excerpt_origin"] == "ai_summary"
    assert item["excerpt"].startswith("AI 摘要")


def test_search_excerpt_keeps_keyword_context_from_body(db_session: Session) -> None:
    """关键词命中正文中后段时，摘要在命中位置附近截取，而不是只给开头。"""
    report = _report(
        db_session,
        "本周进展",
        "开头是常规内容，" + "没有关键词的铺垫。" * 60 + "本周完成了支付接口联调，已提测。",
    )
    ctx = _ctx(db_session)

    result = _call_tool(ctx, "search_reports", {"keyword": "支付"})

    item = result["reports"][0]
    assert item["report_id"] == report.id
    assert item["excerpt_origin"] == "body"
    assert "支付接口联调" in item["excerpt"]


# ---------------------------------------------------------------------------
# 语义检索：浏览模式按「问题 ↔ 正文向量」余弦排序，故障/未配置一律降级
# ---------------------------------------------------------------------------


class _FakeEmbeddingClient:
    """按文本映射返回预设向量，不发起任何网络请求。"""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        import types

        self.vectors_by_text = vectors_by_text
        self.settings = types.SimpleNamespace(ai_embedding_model="fake-embed")

    def configured(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.vectors_by_text[text] for text in texts]


def _seed_embedding(session: Session, report_id: int, vector: list[float]) -> None:
    session.add(
        AIEmbedding(
            report_id=report_id,
            model="fake-embed",
            dim=len(vector),
            vector=vector,
            source_hash=f"hash-{report_id}",
        )
    )
    session.commit()


def test_search_semantic_order_ranks_relevant_first(db_session: Session) -> None:
    """浏览模式下语义相关度高的周报排前面，并返回 semantic 元信息。"""
    import types

    import app.ai.embedding as embedding_module

    relevant = _report(db_session, "客户投诉处理", "处理了客户投诉并完成回访。")
    unrelated = _report(db_session, "内训安排", "组织了内部培训与团建。")
    _seed_embedding(db_session, relevant.id, [0.95, 0.1])
    _seed_embedding(db_session, unrelated.id, [0.05, 0.98])

    question = "客户投诉处理进展如何？"
    fake = _FakeEmbeddingClient({question: [1.0, 0.0]})
    original_client = embedding_module.get_embedding_client
    embedding_module.get_embedding_client = lambda settings=None: fake  # type: ignore[assignment]
    try:
        ctx = _ctx(db_session, question=question)
        result = _call_tool(ctx, "search_reports", {})
    finally:
        embedding_module.get_embedding_client = original_client

    assert result["semantic"] is not None
    assert result["semantic"]["ranked"] == 2
    assert result["reports"][0]["report_id"] == relevant.id
    assert result["reports"][1]["report_id"] == unrelated.id

    # 有关键词时不得启用语义排序（关键词是严格证据筛选）
    ctx_keyword = _ctx(db_session, question=question)
    keyword_result = _call_tool(ctx_keyword, "search_reports", {"keyword": "培训"})
    assert keyword_result["semantic"] is None
    assert [item["report_id"] for item in keyword_result["reports"]] == [unrelated.id]


def test_search_semantic_silently_degrades_without_vectors(db_session: Session) -> None:
    """范围内没有向量时语义不生效，检索退回时间排序且 semantic 为 null。"""
    _report(db_session, "本周进展", "完成了支付接口联调。")
    ctx = _ctx(db_session, question="支付进展如何？")

    result = _call_tool(ctx, "search_reports", {})

    assert result["semantic"] is None
    assert result["matched_total"] == 1
