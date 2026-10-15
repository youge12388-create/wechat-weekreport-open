"""AI 层：JSON 容错解析 + 检索问答 + 重复检测。

全部用 FakeProvider 替身，测试不联网、不花钱，且能断言「喂给模型的提示词」。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from app.ai.fake import FakeProvider
from app.ai.json_util import extract_json_object
from app.core.config import get_settings
from app.models.ai_analysis import AIAnalysis
from app.models.employee import Employee
from app.models.enums import EmployeeStatus
from app.models.weekly_report import WeeklyReport
from app.services import ai_search_service
from app.services import duplicate_service

PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)


def _make_report(session: Session, title: str, content: str) -> tuple[Employee, WeeklyReport]:
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
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.commit()
    return employee, report


def test_extract_json_plain() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_with_chatter() -> None:
    text = '好的，结果如下：\n```json\n{"answer": "x", "sources": []}\n```\n希望有帮助'
    assert extract_json_object(text)["answer"] == "x"


def test_extract_json_invalid_raises() -> None:
    from app.ai.base import LLMResponseError

    with pytest.raises(LLMResponseError):
        extract_json_object("我无法回答这个问题")


def test_search_without_candidates_skips_model(db_session: Session, monkeypatch) -> None:
    """没有候选周报就不该花钱调模型。"""
    fake = FakeProvider(response="{}")
    monkeypatch.setattr(ai_search_service, "get_provider", lambda settings: fake)

    result = ai_search_service.search_reports(
        db_session, "谁没交周报？", start="2026-09-07", end="2026-09-13"
    )
    assert result["not_found"] is True
    assert "没有找到任何周报" in result["answer"]
    assert fake.calls == []


def test_search_returns_answer_and_filters_bogus_sources(
    db_session: Session, monkeypatch
) -> None:
    """模型只能引用上下文里真实存在的周报 ID，编造的必须被丢弃。"""
    _, report = _make_report(db_session, "本周进展", "完成了 A 项目上线与 B 客户签约。")

    fake = FakeProvider(
        response=json.dumps(
            {
                "answer": "张三本周完成了 A 项目上线。",
                "sources": [
                    {"report_id": report.id, "reason": "提到 A 项目"},
                    {"report_id": 999999, "reason": "模型编造的 ID"},
                ],
                "not_found": False,
            },
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr(ai_search_service, "get_provider", lambda settings: fake)

    result = ai_search_service.search_reports(
        db_session, "本周谁做了 A 项目？", start="2026-09-07", end="2026-09-13"
    )

    assert result["not_found"] is False
    assert "A 项目" in result["answer"]
    assert [s["report_id"] for s in result["sources"]] == [report.id]
    # 提示词里必须带上真实的周报正文，否则模型无从作答
    assert "A 项目上线" in fake.calls[0]["user"]


def test_search_reuses_conversation_context(db_session: Session, monkeypatch) -> None:
    """同一会话的追问要把最近回答带回提示词，而不是视觉上连续、语义上断开。"""
    _make_report(db_session, "本周进展", "完成了 A 项目上线。")
    fake = FakeProvider(
        response=json.dumps(
            {"answer": "可以继续看 A 项目", "sources": [], "not_found": False},
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr(ai_search_service, "get_provider", lambda settings: fake)

    ai_search_service.search_reports(
        db_session,
        "本周完成了什么？",
        start="2026-09-07",
        end="2026-09-13",
        conversation_id="conversation-test",
    )
    ai_search_service.search_reports(
        db_session,
        "上面那个项目的风险呢？",
        start="2026-09-07",
        end="2026-09-13",
        conversation_id="conversation-test",
    )

    assert len(fake.calls) == 2
    assert "此前同一会话的问答" in fake.calls[1]["user"]
    assert "本周完成了什么？" in fake.calls[1]["user"]


def test_history_text_prefers_cleaned_body_over_raw(db_session: Session) -> None:
    """历史拼接与本篇同口径：优先清洗后的 content，不把富文本塞进提示词。"""
    employee, _ = _make_report(db_session, "本周正文", "本周继续推进客户沟通。")
    history = WeeklyReport(
        employee_id=employee.id,
        external_id="rep_hist_cleaned",
        report_type="weekly",
        title="上周",
        content="上周推进了客户沟通与材料整理。",
        content_raw='<p style="width:1496px">上周推进了客户沟通与材料整理。</p>',
        start_date=date(2026, 8, 31),
        end_date=date(2026, 9, 6),
        source="wecom",
    )
    db_session.add(history)
    db_session.commit()

    text = duplicate_service._history_text([history])
    assert "上周推进了客户沟通与材料整理。" in text
    assert "width:1496px" not in text
    assert "<p" not in text


def test_history_text_never_falls_back_to_title(db_session: Session) -> None:
    """content 为空时从 content_raw 恢复，两者都空才给占位串——标题不得冒充正文。

    此前这里是 ``content or title``，而同模板周报标题常常一样（如「周报汇报」），
    拿标题当正文会让重复检测把两篇无关周报判成重复。
    """
    employee, _ = _make_report(db_session, "本周正文2", "本周继续推进客户沟通。")
    recovered = WeeklyReport(
        employee_id=employee.id,
        external_id="rep_hist_recover",
        report_type="weekly",
        title="周报汇报",
        content="",
        content_raw="<p>上周完成了材料归档。</p>",
        start_date=date(2026, 8, 31),
        end_date=date(2026, 9, 6),
        source="wecom",
    )
    empty = WeeklyReport(
        employee_id=employee.id,
        external_id="rep_hist_empty",
        report_type="weekly",
        title="周报汇报",
        content="",
        content_raw="",
        start_date=date(2026, 8, 24),
        end_date=date(2026, 8, 30),
        source="wecom",
    )
    db_session.add_all([recovered, empty])
    db_session.commit()

    text = duplicate_service._history_text([recovered, empty])
    assert "上周完成了材料归档。" in text  # 从 raw 恢复，而不是退到标题
    assert "（无正文）" in text  # 两者都空才占位
    assert "周报汇报" not in text  # 标题不得冒充正文


def test_duplicate_check_without_history(db_session: Session, monkeypatch) -> None:
    """没有历史周报就不调模型，直接给确定结论。"""
    _, report = _make_report(db_session, "首次周报", "第一周内容")
    fake = FakeProvider(response="{}")
    monkeypatch.setattr(duplicate_service, "get_provider", lambda settings: fake)

    result = duplicate_service.check_report_duplicate(db_session, report.id)
    assert result["is_duplicate"] is False
    assert "无历史周报" in result["reason"]
    assert fake.calls == []


def test_duplicate_check_flags_and_logs(db_session: Session, monkeypatch) -> None:
    """判定为重复时，结论要落库，列表才能打标。"""
    employee, _ = _make_report(db_session, "本周", "本周继续推进客户沟通与材料整理。")

    history = WeeklyReport(
        employee_id=employee.id,
        external_id="rep_prev",
        report_type="weekly",
        title="上周",
        content="上周继续推进客户沟通与材料整理。",
        content_raw="",
        start_date=date(2026, 8, 31),
        end_date=date(2026, 9, 6),
        submitted_at=datetime(2026, 9, 3, 10, 0, 0),
        source="wecom",
    )
    db_session.add(history)
    db_session.commit()

    current = (
        db_session.query(WeeklyReport)
        .filter(WeeklyReport.external_id == "rep_本周")
        .one()
    )
    fake = FakeProvider(
        response=json.dumps(
            {"is_duplicate": True, "score": 92, "reason": "内容与上周几乎一致"},
            ensure_ascii=False,
        )
    )
    monkeypatch.setattr(duplicate_service, "get_provider", lambda settings: fake)

    result = duplicate_service.check_report_duplicate(db_session, current.id)
    assert result["is_duplicate"] is True
    assert result["score"] == 92
    assert history.id in result["compared_with"]

    flags = duplicate_service.latest_duplicate_flags(db_session, [current.id])
    assert flags[current.id]["is_duplicate"] is True
    assert flags[current.id]["score"] == 92


def test_duplicate_check_failure_is_logged(db_session: Session, monkeypatch) -> None:
    """模型报错不能吞掉：要写一条 failed 记录，方便排查。"""
    _, report = _make_report(db_session, "本周", "内容")
    _make_report_history(db_session, report)

    def boom(settings):
        raise duplicate_service.LLMError("模型服务炸了")

    monkeypatch.setattr(duplicate_service, "get_provider", boom)

    with pytest.raises(duplicate_service.LLMError):
        duplicate_service.check_report_duplicate(db_session, report.id)

    row = (
        db_session.query(AIAnalysis)
        .filter(
            AIAnalysis.entity_id == report.id,
            AIAnalysis.analysis_type == "duplicate_check",
        )
        .one()
    )
    assert row.status == "failed"


def _make_report_history(session: Session, report: WeeklyReport) -> None:
    session.add(
        WeeklyReport(
            employee_id=report.employee_id,
            external_id="rep_hist_helper",
            report_type="weekly",
            title="历史",
            content="历史内容",
            content_raw="",
            start_date=date(2026, 8, 24),
            end_date=date(2026, 8, 30),
            submitted_at=datetime(2026, 8, 27, 10, 0, 0),
            source="wecom",
        )
    )
    session.commit()


def test_settings_have_ai_defaults() -> None:
    settings = get_settings()
    assert settings.ai_provider in {"deepseek", "fake"}
    assert settings.ai_max_input_chars > 0
