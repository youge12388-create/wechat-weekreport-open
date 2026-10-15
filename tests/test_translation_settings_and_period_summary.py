"""翻译、运行时 AI 设置与全员周期总结的非联网回归测试。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.ai.fake import FakeProvider
from app.main import create_app
from app.models.ai_analysis import AIAnalysis
from app.models.enums import AnalysisQueueStatus, EmployeeStatus, SyncType
from app.models.period_summary_queue import PeriodSummaryQueue
from app.models.report_translation import ReportTranslation
from app.models.system_setting import SystemSetting
from app.models.sync_job import SyncJob
from app.models.weekly_report import WeeklyReport
from app.services import period_summary_service
from app.services.period_summary_service import (
    enqueue_period_summary,
    period_summary_state,
    process_period_summary_item,
)
from app.services.sync_scheduler import previous_period_time_window
from app.services.report_translation_service import (
    translate_report,
    translation_state,
)
from app.services.sync_base import SyncResult
from app.services.weekly_report_sync import sync_weekly_reports
from app.services.runtime_config_service import (
    runtime_settings,
    settings_snapshot,
    update_runtime_values,
)
from app.translation.base import TranslationResult
from tests.fake_wecom import (
    FakeWeComClient,
    _dept,
    _journal_info,
    _user,
    text_apply_data,
)


PERIOD_START = date(2026, 9, 7)
PERIOD_END = date(2026, 9, 13)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 9, 14), date(2026, 9, 20), ("本周", "本周重点", "下周计划")),
        (date(2026, 9, 7), date(2026, 9, 13), ("上周", "上周重点", "本周计划")),
        (date(2026, 9, 1), date(2026, 9, 30), ("本月", "本月重点", "后续计划")),
    ],
)
def test_period_summary_prompt_context_distinguishes_dashboard_tabs(
    start: date, end: date, expected: tuple[str, str, str]
) -> None:
    context = period_summary_service._period_prompt_context(
        start, end, date(2026, 9, 16)
    )
    assert (context["label"], context["focus"], context["next"]) == expected


def _allow_hosts(monkeypatch, hosts: list[str]) -> None:
    """把出站 AI 主机白名单设为指定值（该配置刻意只从环境变量读取）。"""
    from app.core.config import get_settings

    monkeypatch.setenv("AI_ALLOWED_BASE_HOSTS", json.dumps(hosts))
    get_settings.cache_clear()


def test_period_summary_rechecks_exemptions_before_model_call(db_session, monkeypatch):
    from app.models.employee import Employee

    included = _report(db_session, "正常人员完成验收", "included")
    excluded = _report(db_session, "免提交人员的独有机密内容", "excluded")
    enqueue_period_summary(db_session, PERIOD_START, PERIOD_END)
    queue = db_session.query(PeriodSummaryQueue).one()
    employee = db_session.get(Employee, excluded.employee_id)
    employee.report_exempt = True
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()
    fake = FakeProvider(response="完成验收")
    monkeypatch.setattr(period_summary_service, "get_provider", lambda settings: fake)
    assert process_period_summary_item(queue.id)["status"] == "requeued"
    assert fake.calls == []
    db_session.refresh(queue)
    assert queue.report_count == 1
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()
    assert process_period_summary_item(queue.id)["status"] == "success"
    assert "免提交人员的独有机密内容" not in str(fake.calls)
    assert "正常人员完成验收" in str(fake.calls)
    state = period_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["reports_total"] == 1
    assert state["processed_reports"] == 1


def _report(session, content: str, external_id: str = "translation-report") -> WeeklyReport:
    from app.models.employee import Employee

    employee = Employee(
        source="wecom",
        external_id=f"employee-{external_id}",
        name="Alex",
        status=EmployeeStatus.ACTIVE,
    )
    session.add(employee)
    session.flush()
    report = WeeklyReport(
        employee_id=employee.id,
        external_id=external_id,
        report_type="weekly",
        title="Weekly update",
        content=content,
        content_raw=content,
        content_language="english",
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        submitted_at=datetime(2026, 9, 10, 10, 0, 0),
        source="wecom",
    )
    session.add(report)
    session.commit()
    return report


def test_runtime_settings_encrypt_and_never_return_keys(db_session) -> None:
    update_runtime_values(
        db_session,
        {
            "ai_api_key": "deepseek-secret",
            "ai_model": "deepseek-reasoner",
            "baidu_translate_app_id": "app-id",
            "baidu_translate_api_key": "baidu-secret",
        },
    )
    db_session.commit()

    settings = runtime_settings(db_session)
    assert settings.ai_model == "deepseek-reasoner"
    assert settings.ai_api_key == "deepseek-secret"
    assert settings.baidu_translate_api_key == "baidu-secret"
    snapshot = settings_snapshot(db_session)
    assert snapshot["deepseek"]["api_key_configured"] is True
    assert snapshot["baidu_translation"]["api_key_configured"] is True
    assert "deepseek-secret" not in str(snapshot)
    assert "baidu-secret" not in str(snapshot)
    encrypted = db_session.query(SystemSetting).filter_by(key="ai_api_key").one()
    assert "deepseek-secret" not in encrypted.value_encrypted


def test_settings_api_returns_only_redacted_configuration(db_session) -> None:
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.patch(
                "/api/settings/ai",
                json={
                    "ai_model": "deepseek-reasoner",
                    "ai_api_key": "api-key-must-not-leak",
                    "baidu_translate_app_id": "test-app-id",
                },
            )
            assert response.status_code == 200
            assert "api-key-must-not-leak" not in response.text
            body = client.get("/api/settings/ai").json()
            assert body["deepseek"]["model"] == "deepseek-reasoner"
            assert body["deepseek"]["api_key_configured"] is True
    finally:
        app.dependency_overrides.clear()


def test_settings_blank_secret_does_not_clear_saved_key(db_session) -> None:
    update_runtime_values(
        db_session,
        {
            "ai_api_key": "saved-deepseek-key",
            "baidu_translate_app_id": "saved-app-id",
            "baidu_translate_api_key": "saved-baidu-key",
        },
    )
    db_session.commit()
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.patch(
                "/api/settings/ai",
                json={
                    "ai_api_key": "",
                    "baidu_translate_api_key": "",
                    "baidu_translate_secret_key": "",
                },
            )
            assert response.status_code == 200
            snapshot = settings_snapshot(db_session)
            assert snapshot["deepseek"]["api_key_configured"] is True
            assert snapshot["baidu_translation"]["api_key_configured"] is True
    finally:
        app.dependency_overrides.clear()


def test_settings_model_list_reuses_saved_key_without_leaking_it(
    db_session, monkeypatch
) -> None:
    # 出站主机白名单是部署侧配置：显式声明 example.com 为允许目标，
    # 用来验证「已声明主机上复用已保存密钥」这条正常路径不被误伤。
    _allow_hosts(monkeypatch, ["example.com"])
    update_runtime_values(
        db_session,
        {
            "ai_api_key": "saved-deepseek-key",
            "ai_model": "deepseek-chat",
        },
    )
    db_session.commit()
    captured: dict[str, str] = {}

    class FakeModelProvider:
        def list_models(self) -> list[str]:
            return ["deepseek-chat", "deepseek-reasoner"]

    def fake_get_provider(settings):
        captured["api_key"] = settings.ai_api_key
        captured["base_url"] = settings.ai_base_url
        return FakeModelProvider()

    monkeypatch.setattr("app.api.routes.settings.get_provider", fake_get_provider)
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/settings/ai/models",
                json={"ai_base_url": "https://example.com/v1"},
            )
            assert response.status_code == 200
            assert response.json()["models"] == [
                "deepseek-chat",
                "deepseek-reasoner",
            ]
            assert "saved-deepseek-key" not in response.text
            assert captured == {
                "api_key": "saved-deepseek-key",
                "base_url": "https://example.com/v1",
            }
    finally:
        app.dependency_overrides.clear()


def test_translation_cache_does_not_overwrite_original(db_session, monkeypatch) -> None:
    report = _report(db_session, "Completed the API integration and deployment.")

    class FakeTranslation:
        def translate(self, text: str) -> TranslationResult:
            assert "API integration" in text
            return TranslationResult("完成 API 集成和部署。", "en", 1)

    monkeypatch.setattr(
        "app.services.report_translation_service.get_translation_provider",
        lambda settings: FakeTranslation(),
    )
    first = translate_report(db_session, report.id)
    assert first["available"] is True
    assert first["content"] == "完成 API 集成和部署。"
    assert report.content_raw == "Completed the API integration and deployment."

    cached = translate_report(db_session, report.id)
    assert cached["cached"] is True
    report.content = "Completed the API integration and production deployment."
    report.content_raw = report.content
    db_session.commit()
    assert translation_state(db_session, report)["stale"] is True


def test_translation_uses_clean_source_and_strips_html_output(
    db_session, monkeypatch
) -> None:
    report = _report(db_session, "placeholder")
    report.content = "本周工作总结：Completed the API integration."
    report.content_raw = (
        '本周工作总结：<div><p>Completed the <strong>API integration</strong>.'
        "</p></div>"
    )
    db_session.commit()
    captured: list[str] = []

    class FakeTranslation:
        def translate(self, text: str) -> TranslationResult:
            captured.append(text)
            return TranslationResult(
                "<p>本周已完成 API 集成。</p><ul><li>准备发布</li></ul>",
                "en",
                1,
            )

    monkeypatch.setattr(
        "app.services.report_translation_service.get_translation_provider",
        lambda settings: FakeTranslation(),
    )

    result = translate_report(db_session, report.id)

    assert captured == ["本周工作总结：Completed the API integration."]
    assert result["content"] == "本周已完成 API 集成。\n准备发布"
    assert "<p>" not in result["content"]
    assert "<li>" not in result["content"]


def test_non_english_report_translation_is_rejected(db_session) -> None:
    report = _report(db_session, "本周完成接口集成。")
    report.content_language = "chinese"
    db_session.commit()

    state = translation_state(db_session, report)
    assert state["status"] == "not_requested"
    assert state["content"] is None
    with pytest.raises(ValueError, match="只有英文周报支持翻译为中文"):
        translate_report(db_session, report.id)


def _sync_report(
    session,
    content: str,
    external_id: str = "auto-translation-report",
) -> tuple[SyncResult, FakeWeComClient]:
    client = FakeWeComClient(
        departments=[_dept(1, "总部")],
        employees=[_user("u1", "Alex", 1)],
        reports={
            external_id: _journal_info(
                report_time=int(
                    datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc).timestamp()
                ),
                userid="u1",
                apply_data=text_apply_data(content),
            )
        },
    )
    from app.services.department_sync import sync_departments
    from app.services.employee_sync import sync_employees

    sync_departments(session, client)
    sync_employees(session, client)
    result = sync_weekly_reports(
        session,
        datetime(2026, 9, 1),
        datetime(2026, 9, 13),
        client,
    )
    return result, client


def test_english_report_sync_auto_translates_and_uses_cache(
    db_session, monkeypatch
) -> None:
    calls: list[str] = []

    class FakeTranslation:
        def translate(self, text: str) -> TranslationResult:
            calls.append(text)
            return TranslationResult("已完成接口集成并准备发布。", "en", 1)

    monkeypatch.setattr(
        "app.services.report_translation_service.get_translation_provider",
        lambda settings: FakeTranslation(),
    )

    result, client = _sync_report(
        db_session, "Completed the API integration and prepared the release."
    )
    assert result.status == "success"
    assert len(calls) == 1
    assert "Completed the API integration and prepared the release." in calls[0]

    report = (
        db_session.query(WeeklyReport)
        .filter_by(external_id="auto-translation-report")
        .one()
    )
    state = translation_state(db_session, report)
    assert state["available"] is True
    assert state["content"] == "已完成接口集成并准备发布。"

    second = sync_weekly_reports(
        db_session,
        datetime(2026, 9, 1),
        datetime(2026, 9, 13),
        client,
    )
    assert second.skipped_count == 1
    assert len(calls) == 1


def test_chinese_report_sync_does_not_call_translation(
    db_session, monkeypatch
) -> None:
    def unexpected_provider(settings):
        raise AssertionError("中文周报不应触发翻译")

    monkeypatch.setattr(
        "app.services.report_translation_service.get_translation_provider",
        unexpected_provider,
    )

    result, _client = _sync_report(db_session, "本周完成接口集成，并准备版本发布。")
    assert result.status == "success"
    assert db_session.query(ReportTranslation).count() == 0


def test_sync_auto_enqueues_period_summary_for_changed_weekly_reports(
    db_session,
) -> None:
    update_runtime_values(
        db_session, {"sync_auto_period_summary_enabled": "true"}
    )
    db_session.commit()

    result, client = _sync_report(
        db_session, "本周完成接口集成，并准备版本发布。"
    )

    assert result.status == "success"
    queue = db_session.query(PeriodSummaryQueue).one()
    assert queue.start_date == PERIOD_START
    assert queue.end_date == PERIOD_END
    assert queue.status == AnalysisQueueStatus.PENDING

    second = sync_weekly_reports(
        db_session,
        datetime(2026, 9, 1),
        datetime(2026, 9, 13),
        client,
    )
    assert second.skipped_count == 1
    assert db_session.query(PeriodSummaryQueue).count() == 1


def test_sync_auto_requeues_period_summary_when_content_changes(
    db_session,
) -> None:
    update_runtime_values(
        db_session, {"sync_auto_period_summary_enabled": "true"}
    )
    db_session.commit()

    result, client = _sync_report(
        db_session, "本周完成接口集成，并准备版本发布。"
    )
    assert result.status == "success"
    queue = db_session.query(PeriodSummaryQueue).one()
    queue.status = AnalysisQueueStatus.SUCCESS
    db_session.commit()

    client.reports["auto-translation-report"] = _journal_info(
        report_time=int(
            datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc).timestamp()
        ),
        userid="u1",
        apply_data=text_apply_data("本周完成接口集成，发布后发现并修复了一个问题。"),
    )
    changed = sync_weekly_reports(
        db_session,
        datetime(2026, 9, 1),
        datetime(2026, 9, 13),
        client,
    )

    assert changed.success_count == 1
    changed_queue = db_session.query(PeriodSummaryQueue).one()
    assert changed_queue.status == AnalysisQueueStatus.PENDING


def test_sync_auto_summary_can_be_disabled(db_session) -> None:
    update_runtime_values(
        db_session, {"sync_auto_period_summary_enabled": "false"}
    )
    db_session.commit()

    result, _client = _sync_report(
        db_session, "本周完成接口集成，并准备版本发布。"
    )

    assert result.status == "success"
    assert db_session.query(PeriodSummaryQueue).count() == 0


def test_sync_auto_summary_runs_end_to_end_without_opening_the_panel(
    db_session, monkeypatch
) -> None:
    """抓完周报就自动出总览：入队 → Worker 消费 → GET 有结论。

    这是「本周概览不用点开才跑」的核心链路：用户没有发起任何请求，
    周报同步本身就触发了总结，页面只需要读缓存。
    """
    update_runtime_values(db_session, {"sync_auto_period_summary_enabled": "true"})
    db_session.commit()
    fake = FakeProvider(
        response="## 本周重点\n接口集成完成。\n## 管理建议\n- 安排回归测试 [#1]"
    )
    monkeypatch.setattr(period_summary_service, "get_provider", lambda settings: fake)

    result, _client = _sync_report(db_session, "本周完成接口集成，并准备版本发布。")
    assert result.status == "success"

    queue = db_session.query(PeriodSummaryQueue).one()
    assert queue.status == AnalysisQueueStatus.PENDING
    # 消费前：GET 只暴露 pending，不会假装已有结论。
    assert period_summary_state(db_session, PERIOD_START, PERIOD_END)["status"] == (
        AnalysisQueueStatus.PENDING
    )

    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()
    assert process_period_summary_item(queue.id)["status"] == "success"

    db_session.expire_all()
    state = period_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["status"] == AnalysisQueueStatus.SUCCESS
    assert state["summary"]
    assert state["processed_reports"] == 1
    assert state["full_context"] is True


def test_translation_failure_does_not_fail_report_sync(
    db_session, monkeypatch
) -> None:
    class FailingThenSucceedingTranslation:
        attempts = 0

        def translate(self, text: str) -> TranslationResult:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("translation upstream unavailable")
            return TranslationResult("发布准备与验证已完成。", "en", 1)

    provider = FailingThenSucceedingTranslation()

    monkeypatch.setattr(
        "app.services.report_translation_service.get_translation_provider",
        lambda settings: provider,
    )

    result, client = _sync_report(
        db_session, "Completed the release preparation and validation."
    )
    assert result.status == "success"
    assert result.failed_count == 0

    report = (
        db_session.query(WeeklyReport)
        .filter_by(external_id="auto-translation-report")
        .one()
    )
    state = translation_state(db_session, report)
    assert state["status"] == "failed"
    assert "translation upstream unavailable" in state["error"]

    second = sync_weekly_reports(
        db_session,
        datetime(2026, 9, 1),
        datetime(2026, 9, 13),
        client,
    )
    assert second.skipped_count == 1
    assert provider.attempts == 2
    db_session.refresh(report)
    recovered = translation_state(db_session, report)
    assert recovered["available"] is True
    assert recovered["content"] == "发布准备与验证已完成。"


def test_period_summary_reads_all_reports_and_marks_success(db_session, monkeypatch) -> None:
    first = _report(db_session, "Finished project A.", "summary-a")
    second = _report(db_session, "Investigated blocker B and planned next steps.", "summary-b")
    fake = FakeProvider(
        response="## 本周重点\n项目推进。\n## 管理建议\n- 建议加快阻塞项处理 [#1]",
        prompt_tokens=100,
        completion_tokens=20,
        prompt_cache_hit_tokens=75,
        prompt_cache_miss_tokens=25,
        http_request_count=1,
    )
    monkeypatch.setattr(period_summary_service, "get_provider", lambda settings: fake)
    monkeypatch.setattr(
        period_summary_service, "utcnow", lambda: datetime(2026, 9, 16, 6, 0, 0)
    )

    queued = enqueue_period_summary(db_session, PERIOD_START, PERIOD_END)
    assert queued["status"] == AnalysisQueueStatus.PENDING
    queue = db_session.query(PeriodSummaryQueue).one()
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()

    result = process_period_summary_item(queue.id)
    assert result["status"] == "success"
    assert result["processed_reports"] == 2
    db_session.expire_all()
    state = period_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["status"] == AnalysisQueueStatus.SUCCESS
    assert state["processed_reports"] == 2
    assert state["summary"]
    # 范围内装得下时**只调一次模型**：分批会把跨篇关联切断，那正是总览要读的东西。
    assert len(fake.calls) == 1
    assert fake.option_calls == [{"thinking": False}]
    analysis = db_session.query(AIAnalysis).filter_by(analysis_type="period_summary").one()
    assert analysis.prompt_tokens == 100
    assert analysis.completion_tokens == 20
    assert analysis.input_data["usage"] == {
        "model_call_count": 1,
        "http_request_count": 1,
        "prompt_cache_hit_tokens": 75,
        "prompt_cache_miss_tokens": 25,
    }
    assert analysis.input_data["queue_attempt"] == 1
    assert state["full_context"] is True
    assert str(first.id) in fake.calls[0]["user"]
    assert str(second.id) in fake.calls[0]["user"]
    assert "周期语义：上周" in fake.calls[0]["user"]
    assert "## 上周重点" in fake.calls[0]["user"]
    assert "## 本周计划" in fake.calls[0]["user"]


def test_period_summary_falls_back_to_batches_when_over_budget(
    db_session, monkeypatch
) -> None:
    """正文超过单次读取上限时回退分批，并如实标注 full_context=false。"""
    _report(db_session, "详" * 400, "summary-big-a")
    _report(db_session, "情" * 400, "summary-big-b")
    fake = FakeProvider(response="## 本周重点\n合并结论。\n## 管理建议\n- 建议一 [#1]")
    monkeypatch.setattr(period_summary_service, "get_provider", lambda settings: fake)
    # 把单次全量预算压到装不下两篇，验证自动回退而不是静默截断。
    monkeypatch.setattr(
        period_summary_service,
        "runtime_settings",
        lambda session: runtime_settings(session).model_copy(
            update={"ai_period_summary_full_context_chars": 200}
        ),
    )

    enqueue_period_summary(db_session, PERIOD_START, PERIOD_END)
    queue = db_session.query(PeriodSummaryQueue).one()
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()

    result = process_period_summary_item(queue.id)
    assert result["status"] == "success"
    # 分批 = 每个原文批次一次 + 至少一次合并/最终请求
    assert len(fake.calls) >= 2
    assert len(fake.option_calls) == len(fake.calls)
    assert all(call["thinking"] is False for call in fake.option_calls)
    db_session.expire_all()
    analysis = db_session.query(AIAnalysis).filter_by(analysis_type="period_summary").one()
    assert analysis.input_data["usage"]["model_call_count"] == len(fake.calls)
    state = period_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["status"] == AnalysisQueueStatus.SUCCESS
    assert state["full_context"] is False


def test_period_summary_sends_cleaned_text_not_raw_html(
    db_session, monkeypatch
) -> None:
    """送进模型的必须是清洗后正文。

    实测同一周期 raw（富文本）合计 27 万字符、清洗后仅 4 万 —— 喂 raw 会把表格样式
    这类无意义标签塞满上下文，既贵又淹没有效信息，还会让「装得下就一次读完」白白失效。
    """
    report = _report(db_session, "本周完成接口集成。", "summary-html")
    report.content_raw = "<table style='width: 1496px; border-collapse: collapse;'><tr><td>本周完成接口集成。</td></tr></table>"
    db_session.commit()

    fake = FakeProvider(response="## 本周重点\n接口集成完成。")
    monkeypatch.setattr(period_summary_service, "get_provider", lambda settings: fake)
    enqueue_period_summary(db_session, PERIOD_START, PERIOD_END)
    queue = db_session.query(PeriodSummaryQueue).one()
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()

    assert process_period_summary_item(queue.id)["status"] == "success"
    user = fake.calls[0]["user"]
    assert "本周完成接口集成。" in user
    assert "<td>" not in user and "border-collapse" not in user
    assert len(fake.calls) == 1


def test_period_summary_excludes_non_weekly_reports(db_session) -> None:
    _report(db_session, "Weekly source.", "summary-weekly")
    other = _report(db_session, "日报 source，不应进入周报总览。", "summary-other")
    other.report_type = "other"
    db_session.commit()

    queued = enqueue_period_summary(db_session, PERIOD_START, PERIOD_END)
    assert queued["reports_total"] == 1
    assert db_session.query(PeriodSummaryQueue).one().report_count == 1


def test_period_summary_becomes_stale_when_report_changes(db_session) -> None:
    report = _report(db_session, "Initial English report.", "summary-stale")
    enqueue_period_summary(db_session, PERIOD_START, PERIOD_END)
    report.content = "Updated English report."
    db_session.commit()
    state = period_summary_state(db_session, PERIOD_START, PERIOD_END)
    assert state["stale"] is True
    assert state["status"] == "stale"


def test_period_summary_get_is_read_only_and_does_not_reset_queue(
    db_session,
) -> None:
    """「本周概览」轮询必须走 GET：POST 会重置队列，反复轮询等于反复重生成。

    这里显式验证 GET 不会改动队列状态，
    否则前端每 3 秒轮询一次就会把正在跑的任务一直打回 PENDING。
    """
    _report(db_session, "Weekly source.", "summary-readonly")
    enqueue_period_summary(db_session, PERIOD_START, PERIOD_END)
    queue = db_session.query(PeriodSummaryQueue).one()
    queue.status = AnalysisQueueStatus.RUNNING
    db_session.commit()

    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/dashboard/period-summary",
                params={
                    "start": PERIOD_START.isoformat(),
                    "end": PERIOD_END.isoformat(),
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == AnalysisQueueStatus.RUNNING
            assert body["reports_total"] == 1
    finally:
        app.dependency_overrides.clear()

    db_session.expire_all()
    assert (
        db_session.query(PeriodSummaryQueue).one().status
        == AnalysisQueueStatus.RUNNING
    )


def test_period_summary_get_reports_empty_period(db_session) -> None:
    """本期一篇周报都没有时，GET 也要能给出 empty，而不是报错或假装有结论。"""
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/dashboard/period-summary",
                params={
                    "start": PERIOD_START.isoformat(),
                    "end": PERIOD_END.isoformat(),
                },
            )
            assert response.status_code == 200
            assert response.json()["status"] == "empty"
    finally:
        app.dependency_overrides.clear()


def test_report_queue_endpoint_accepts_current_period_mode(db_session) -> None:
    """「本周概览」用 current_period 入队，窗口口径必须能被前端识别。"""
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/sync/wecom/reports/queue",
                json={"mode": "current_period"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["created"] is True
            assert body["window"] == "当前周期"
    finally:
        app.dependency_overrides.clear()

    # 序列化响应不含 params（那是内部实现），窗口口径直接查库确认。
    job = db_session.query(SyncJob).filter_by(type=SyncType.WEEKLY_REPORTS).one()
    assert job.params["mode"] == "current_period"
    start = datetime.fromisoformat(job.params["start"])
    previous_start, _ = previous_period_time_window(db_session)
    assert start > previous_start


# --------------------------------------------------------------------------- #
# 出站 Base URL 白名单：防止已保存的 AI 密钥被发往任意主机
# --------------------------------------------------------------------------- #


def _settings_client(db_session):
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    return app


def test_base_url_to_unlisted_host_is_rejected(db_session, monkeypatch) -> None:
    """把 base_url 改成白名单外的主机必须被拒。

    这是密钥外发的主路径：base_url 是设置页可写的任意 URL，而下一次 AI 调用
    会把已保存的 API Key 放进 Authorization 头发到该地址。旧实现只校验
    「以 http(s):// 开头」，等于完全不设防。
    """
    _allow_hosts(monkeypatch, ["api.deepseek.com"])
    app = _settings_client(db_session)
    try:
        with TestClient(app) as client:
            resp = client.patch(
                "/api/settings/ai",
                json={"ai_base_url": "https://attacker.example.com"},
            )
            assert resp.status_code == 422
            assert "不允许的 Base URL 主机" in resp.text
    finally:
        app.dependency_overrides.clear()


def test_base_url_plain_http_to_unlisted_host_is_rejected(db_session, monkeypatch) -> None:
    """明文 http 的目标同样要被白名单挡住（密钥会明文过网）。"""
    _allow_hosts(monkeypatch, ["api.deepseek.com"])
    app = _settings_client(db_session)
    try:
        with TestClient(app) as client:
            resp = client.patch(
                "/api/settings/ai",
                json={"ai_base_url": "http://internal-host.local/v1"},
            )
            assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_base_url_change_requires_new_key(db_session, monkeypatch) -> None:
    """在白名单内换主机也必须同时提供新 Key，不能让旧 Key 静默发往新主机。"""
    _allow_hosts(monkeypatch, ["api.deepseek.com", "gateway.internal"])
    update_runtime_values(db_session, {"ai_api_key": "saved-key"})
    db_session.commit()
    app = _settings_client(db_session)
    try:
        with TestClient(app) as client:
            # 只改主机、不带新 Key -> 拒绝
            resp = client.patch(
                "/api/settings/ai",
                json={"ai_base_url": "https://gateway.internal/v1"},
            )
            assert resp.status_code == 400
            assert "必须同时重新填写 API Key" in resp.text

            # 带上新 Key -> 允许，且旧 Key 不会被复用
            resp = client.patch(
                "/api/settings/ai",
                json={
                    "ai_base_url": "https://gateway.internal/v1",
                    "ai_api_key": "brand-new-key",
                },
            )
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert runtime_settings(db_session).ai_base_url == "https://gateway.internal/v1"
    assert runtime_settings(db_session).ai_api_key == "brand-new-key"


def test_same_host_path_change_does_not_require_new_key(db_session, monkeypatch) -> None:
    """同一主机的路径调整不涉及密钥外发，不应强迫重填（避免误伤正常运维）。"""
    _allow_hosts(monkeypatch, ["api.deepseek.com"])
    update_runtime_values(db_session, {"ai_api_key": "saved-key"})
    db_session.commit()
    app = _settings_client(db_session)
    try:
        with TestClient(app) as client:
            resp = client.patch(
                "/api/settings/ai",
                json={"ai_base_url": "https://api.deepseek.com/v1"},
            )
            assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert runtime_settings(db_session).ai_base_url == "https://api.deepseek.com/v1"
    assert runtime_settings(db_session).ai_api_key == "saved-key"


def test_allowed_hosts_are_deploy_side_not_settings_page(db_session, monkeypatch) -> None:
    """白名单本身不能通过设置页修改 —— 否则它不是控制措施。

    运行时写接口只接受 RUNTIME_KEYS，ai_allowed_base_hosts 不在其中。
    """
    from app.services.runtime_config_service import RUNTIME_KEYS

    assert "ai_allowed_base_hosts" not in RUNTIME_KEYS


# --------------------------------------------------------------------------- #
# 加密根密钥：不再派生自可默认的 session_secret
# --------------------------------------------------------------------------- #


def test_root_key_is_not_derived_from_public_default_session_secret(
    db_session, monkeypatch, tmp_path
) -> None:
    """密钥文件必须与 session_secret 无关。

    旧实现把 Fernet key 派生自 SESSION_SECRET，而默认 session_secret 是仓库里
    公开的常量 —— 等于没有密钥，能读数据库的人就能解出明文 Key；
    而且轮换 SESSION_SECRET 会让已存密钥全部解不开。
    """
    from app.core.config import get_settings
    from app.services import runtime_config_service as rcs

    key_file = tmp_path / ".config_secret.key"
    monkeypatch.setenv("CONFIG_KEY_FILE", str(key_file))
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert not key_file.exists()
        first = rcs._read_or_create_root_key(db_session, settings)
        assert key_file.exists(), "首次使用应生成密钥文件"
        second = rcs._read_or_create_root_key(db_session, settings)
        assert first == second, "密钥必须稳定（重启后仍能解密已存配置）"

        # 换一个完全不同的 session_secret，密钥不应改变
        monkeypatch.setenv("SESSION_SECRET", "another-totally-different-secret-value-here")
        get_settings.cache_clear()
        third = rcs._read_or_create_root_key(db_session, get_settings())
        assert third == first, "密钥不能随 session_secret 变化"
    finally:
        get_settings.cache_clear()


def test_legacy_derived_rows_are_migrated_to_key_file(
    db_session, monkeypatch, tmp_path
) -> None:
    """存量数据（旧派生密钥加密）必须能平滑迁移，不能升级后读不出旧 Key。"""
    from base64 import urlsafe_b64encode

    from cryptography.fernet import Fernet

    from app.core.config import get_settings
    from app.models.system_setting import SystemSetting
    from app.services import runtime_config_service as rcs

    monkeypatch.setenv("CONFIG_KEY_FILE", str(tmp_path / ".config_secret.key"))
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "")
    monkeypatch.setenv("SESSION_SECRET", "legacy-session-secret-for-migration-test")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        # 用旧派生方式写入一条密文
        legacy_key = rcs._legacy_derived_key(settings)
        cipher = Fernet(legacy_key.encode("ascii"))
        db_session.add(
            SystemSetting(
                key="ai_api_key",
                value_encrypted=cipher.encrypt(b"legacy-secret-value").decode("utf-8"),
            )
        )
        db_session.commit()

        # 触发迁移 + 读取
        from app.services.runtime_config_service import runtime_settings

        merged = runtime_settings(db_session)
        assert merged.ai_api_key == "legacy-secret-value", "旧密文必须仍能解出明文"

        # 库里的密文应已变成新密钥加密的可解密内容
        row = db_session.query(SystemSetting).filter_by(key="ai_api_key").one()
        new_cipher = Fernet(rcs._read_or_create_root_key(db_session, settings).encode("ascii"))
        assert new_cipher.decrypt(row.value_encrypted.encode("utf-8")) == b"legacy-secret-value"
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# runtime_settings 缓存：必须真的生效，且对写操作完全透明
# --------------------------------------------------------------------------- #


def test_runtime_settings_cache_skips_repeated_validation(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置未变时不能反复重跑 Settings 校验（实测单次 4.6 ms，占该调用 83%）。

    调用是链式的（sync_status → report_schedule_status → _report_schedule_due →
    _next_run_at），/api/notifications/feed 一次请求实测进入 6 次 —— 而那个接口
    每个打开的页面每 60 秒轮询一次。这里用「校验次数」锁住，避免有人把缓存删掉。
    """
    from app.core.config import Settings as SettingsClass
    from app.services import runtime_config_service as rcs

    rcs.invalidate_runtime_settings_cache()
    original = SettingsClass.model_validate
    calls = {"n": 0}

    def counting_validate(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(SettingsClass, "model_validate", staticmethod(counting_validate))

    runtime_settings(db_session)
    assert calls["n"] == 1, "第一次调用必须做一次真实校验"

    for _ in range(5):
        runtime_settings(db_session)
    assert calls["n"] == 1, "配置未变时不应重复校验"

    # 值变了必须重新校验（这是「保存后立即生效」的实现方式）
    update_runtime_values(db_session, {"ai_model": "deepseek-reasoner"})
    db_session.commit()
    assert runtime_settings(db_session).ai_model == "deepseek-reasoner"
    assert calls["n"] == 2, "值变化必须重新校验，而不是吃缓存"


def test_runtime_settings_cache_is_transparent_to_writes(db_session) -> None:
    """缓存不能挡住写入：保存与清空覆盖都必须在下一次读取立刻可见。"""
    from app.services import runtime_config_service as rcs

    rcs.invalidate_runtime_settings_cache()
    baseline = runtime_settings(db_session).ai_model

    update_runtime_values(db_session, {"ai_model": "deepseek-reasoner"})
    db_session.commit()
    assert runtime_settings(db_session).ai_model == "deepseek-reasoner"

    # 保存成空串 = 删除覆盖，必须回落到部署默认值（而不是继续吃已删掉的缓存）
    update_runtime_values(db_session, {"ai_model": ""})
    db_session.commit()
    assert db_session.query(SystemSetting).filter_by(key="ai_model").count() == 0
    assert runtime_settings(db_session).ai_model == baseline


def test_runtime_settings_returns_a_defensive_copy(db_session) -> None:
    """缓存实例是进程级共享的，就地改字段不能让后续请求拿到被污染的配置。"""
    from app.services import runtime_config_service as rcs

    rcs.invalidate_runtime_settings_cache()
    first = runtime_settings(db_session)
    first.ai_model = "tampered-by-caller"

    second = runtime_settings(db_session)
    assert second.ai_model != "tampered-by-caller", (
        "调用方就地修改被写进了共享缓存，后续所有请求都会读到被改过的配置"
    )
    assert second.ai_model == baseline_model(db_session)


def baseline_model(db_session) -> str:
    """部署默认值（绕过缓存直接取）。"""
    from app.core.config import get_settings

    return get_settings().ai_model
