"""Unknown department detection must not mistake collective quantifiers for names."""

from sqlalchemy.orm import Session

from app.models.department import Department
from app.services.agent_query_plan import resolve_agent_query


def _plan(session: Session, question: str):
    return resolve_agent_query(
        session,
        question=question,
        start="2026-09-07",
        end="2026-09-13",
        scope="period",
        department_id=None,
        employee_id=None,
    )


def test_collective_all_is_not_an_unknown_department(db_session: Session) -> None:
    for question in (
        "请汇总全部历史周报中 World Citizen 合作的进展。",
        "在全部周期里，列出周报中提到的合作伙伴候选。",
        "请查看全部周报。",
    ):
        assert _plan(db_session, question).clarification is None, question


def test_unknown_department_still_clarifies(db_session: Session) -> None:
    db_session.add(Department(external_id="known-dept", name="技术部", source="wecom"))
    db_session.commit()

    assert _plan(db_session, "技术部的周报有哪些？").clarification is None
    assert _plan(db_session, "技术部上周做了什么？").clarification is None
    unknown = _plan(db_session, "财务部的周报有哪些？")
    assert unknown.clarification is not None
    assert unknown.clarification["reason"] == "unknown_department"
