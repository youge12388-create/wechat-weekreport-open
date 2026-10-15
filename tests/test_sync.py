"""Sprint 1 核心验收：幂等、溯源、错误隔离、同步日志。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.connectors.base import RawPayload
from app.models.department import Department
from app.models.employee import Employee
from app.models.analysis_queue import AnalysisQueue
from app.models.enums import AnalysisQueueStatus, DataSource, EmployeeStatus, SyncJobStatus
from app.models.raw_record import RawRecord
from app.models.sync_job import SyncJob
from app.models.weekly_report import WeeklyReport
from app.services.department_sync import sync_departments
from app.services.employee_sync import sync_employees
from app.services.weekly_report_sync import sync_weekly_reports
from app.services.report_analysis_service import report_payload_hash
from tests.fake_wecom import (
    FakeWeComClient,
    _dept,
    _journal_info,
    _user,
    text_apply_data,
)

REPORT_TIME = int(datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc).timestamp())


def test_full_history_sync_is_not_truncated_by_normal_batch_cap(
    db_session: Session, monkeypatch
) -> None:
    """全历史首次补档必须有多少抓多少，不能沿用日常同步的单批上限。"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "sync_max_records_per_run", 1)
    client = FakeWeComClient(
        reports={
            "history-1": _journal_info(
                report_time=REPORT_TIME,
                apply_data=text_apply_data("第一篇历史周报"),
            ),
            "history-2": _journal_info(
                report_time=REPORT_TIME + 60,
                apply_data=text_apply_data("第二篇历史周报"),
            ),
        }
    )

    result = sync_weekly_reports(
        db_session,
        datetime(1970, 1, 1),
        datetime(2026, 9, 16),
        client,
        full_history=True,
    )

    assert result.success_count == 2
    assert _count(db_session, WeeklyReport) == 2


def test_exempt_report_sync_keeps_archive_without_ai_queue(db_session):
    from app.models.period_summary_queue import PeriodSummaryQueue

    employee = Employee(source="wecom", external_id="u1", name="免提交人员", report_exempt=True)
    db_session.add(employee)
    db_session.commit()
    client = FakeWeComClient(reports={"exempt-report": _journal_info(
        report_time=REPORT_TIME, apply_data=text_apply_data("完成接口联调与验收")
    )})
    result = sync_weekly_reports(db_session, datetime(2026, 9, 7), datetime(2026, 9, 13), client)
    assert result.success_count == 1
    assert db_session.query(WeeklyReport).count() == 1
    assert db_session.query(RawRecord).count() == 1
    assert db_session.query(AnalysisQueue).count() == 0
    assert db_session.query(PeriodSummaryQueue).count() == 0


def _count(session: Session, model) -> int:  # type: ignore[no-untyped-def]
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_sync_does_not_auto_enqueue_analysis_by_default(db_session: Session) -> None:
    """默认关闭自动分析：同步只入库不建队列位；内容变化只清过期标记。"""
    client = FakeWeComClient(reports={"j1": _journal_info(
        report_time=REPORT_TIME, apply_data=text_apply_data("完成接口联调与验收")
    )})
    result = sync_weekly_reports(db_session, datetime(2026, 9, 7), datetime(2026, 9, 13), client)
    assert result.success_count == 1
    assert _count(db_session, AnalysisQueue) == 0

    # 内容在企微侧被修改后重新同步：不入队，但旧的「已分析」标记必须清掉，
    # 否则页面会拿旧结论冒充新分析。
    report = db_session.scalar(select(WeeklyReport).where(WeeklyReport.external_id == "j1"))
    assert report is not None
    report.ai_checked_at = datetime(2026, 9, 9)
    report.analysis_payload_hash = report_payload_hash(report)
    db_session.commit()

    client.reports["j1"] = _journal_info(
        report_time=REPORT_TIME, apply_data=text_apply_data("本周需求变更，重写周报")
    )
    result2 = sync_weekly_reports(db_session, datetime(2026, 9, 7), datetime(2026, 9, 13), client)
    assert result2.success_count == 1
    db_session.refresh(report)
    assert report.ai_checked_at is None
    assert _count(db_session, AnalysisQueue) == 0


def test_sync_auto_enqueues_analysis_when_enabled(db_session: Session) -> None:
    """设置页打开「抓取后自动 AI 分析」后，新增周报自动进入队列。"""
    from app.services.runtime_config_service import update_runtime_values

    update_runtime_values(db_session, {"ai_auto_enqueue_on_sync": "true"})
    db_session.commit()
    client = FakeWeComClient(reports={"j1": _journal_info(
        report_time=REPORT_TIME, apply_data=text_apply_data("完成接口联调与验收")
    )})
    result = sync_weekly_reports(db_session, datetime(2026, 9, 7), datetime(2026, 9, 13), client)
    assert result.success_count == 1
    queue = db_session.scalar(select(AnalysisQueue))
    assert queue is not None
    assert queue.status == AnalysisQueueStatus.PENDING


# --------------------------------------------------------------------------- #
# 部门
# --------------------------------------------------------------------------- #
def test_department_sync_is_idempotent(db_session: Session) -> None:
    client = FakeWeComClient(departments=[_dept(1, "总部"), _dept(2, "信息部", 1)])

    first = sync_departments(db_session, client)
    assert first.status == SyncJobStatus.SUCCESS
    assert first.success_count == 2
    assert _count(db_session, Department) == 2

    # 第二次同步：内容未变，应全部跳过且不新增
    second = sync_departments(db_session, client)
    assert second.success_count == 0
    assert second.skipped_count == 2
    assert _count(db_session, Department) == 2


def test_department_rename_and_parent_change(db_session: Session) -> None:
    client = FakeWeComClient(departments=[_dept(1, "总部"), _dept(2, "信息部", 1)])
    sync_departments(db_session, client)

    client.departments = [_dept(1, "总部"), _dept(2, "信息技术部", 1)]
    result = sync_departments(db_session, client)

    assert result.success_count == 1  # 只有变更的那条
    assert _count(db_session, Department) == 2
    dept2 = db_session.scalar(
        select(Department).where(Department.external_id == "2")
    )
    assert dept2 is not None and dept2.name == "信息技术部"
    assert dept2.parent_id is not None  # 父子关系已解析


def test_department_add_and_deactivate(db_session: Session) -> None:
    client = FakeWeComClient(departments=[_dept(1, "总部"), _dept(2, "信息部", 1)])
    sync_departments(db_session, client)

    # 信息部消失，新增市场部
    client.departments = [_dept(1, "总部"), _dept(3, "市场部", 1)]
    sync_departments(db_session, client)

    dept2 = db_session.scalar(select(Department).where(Department.external_id == "2"))
    dept3 = db_session.scalar(select(Department).where(Department.external_id == "3"))
    assert dept2 is not None and dept2.status == "inactive"  # 停用而非物理删除
    assert dept3 is not None and dept3.name == "市场部"
    assert dept3.parent_id is not None
    assert _count(db_session, Department) == 3


# --------------------------------------------------------------------------- #
# 员工
# --------------------------------------------------------------------------- #
def test_employee_sync_is_idempotent_and_links_department(db_session: Session) -> None:
    client = FakeWeComClient(
        departments=[_dept(1, "总部"), _dept(2, "信息部", 1)],
        employees=[_user("u1", "张三", 2, position="工程师")],
    )
    sync_departments(db_session, client)
    first = sync_employees(db_session, client)
    assert first.success_count == 1

    employee = db_session.scalar(select(Employee).where(Employee.external_id == "u1"))
    assert employee is not None
    assert employee.name == "张三"
    assert employee.position == "工程师"
    assert employee.status == EmployeeStatus.ACTIVE
    dept2 = db_session.scalar(select(Department).where(Department.external_id == "2"))
    assert dept2 is not None and employee.department_id == dept2.id

    second = sync_employees(db_session, client)
    assert second.success_count == 0 and second.skipped_count == 1
    assert _count(db_session, Employee) == 1


def test_employee_resigned_and_position_change(db_session: Session) -> None:
    client = FakeWeComClient(
        departments=[_dept(1, "总部"), _dept(2, "信息部", 1)],
        employees=[_user("u1", "张三", 2, position="工程师")],
    )
    sync_departments(db_session, client)
    sync_employees(db_session, client)

    client.employees = [_user("u1", "张三", 2, status=5, position="高级工程师")]
    sync_employees(db_session, client)

    employee = db_session.scalar(select(Employee).where(Employee.external_id == "u1"))
    assert employee is not None
    assert employee.status == EmployeeStatus.RESIGNED
    assert employee.position == "高级工程师"


def test_employee_not_in_list_is_not_marked_resigned(db_session: Session) -> None:
    """接口偶发分页失败时，不得把全员误判为离职。"""
    client = FakeWeComClient(
        departments=[_dept(1, "总部")],
        employees=[_user("u1", "张三", 1)],
    )
    sync_departments(db_session, client)
    sync_employees(db_session, client)

    client.employees = []
    sync_employees(db_session, client)

    employee = db_session.scalar(select(Employee).where(Employee.external_id == "u1"))
    assert employee is not None and employee.status == EmployeeStatus.ACTIVE


# --------------------------------------------------------------------------- #
# 周报
# --------------------------------------------------------------------------- #
def _report_client() -> FakeWeComClient:
    return FakeWeComClient(
        departments=[_dept(1, "总部"), _dept(2, "信息部", 1)],
        employees=[_user("u1", "张三", 2)],
        reports={
            "j1": _journal_info(
                report_time=REPORT_TIME,
                userid="u1",
                apply_data=text_apply_data("本周优化筛查网站英文版本"),
            )
        },
    )


def test_table_template_chinese_report_is_not_marked_english(
    db_session: Session,
) -> None:
    """表格模板中文周报不得因富文本 HTML 标记被误判成英文。

    历史缺陷：语言按未清洗的 content_raw 判定，HTML 标记
    （style="width:1496px" 等）拉丁字母占比 92%+，人事/申请服务等用表格
    模板写的中文周报全被标成 english，「仅英文周报」筛选随之失真。
    语言必须按清洗后正文判定（与周期总结/单篇分析同一口径）。
    """
    html_body = (
        '<table style="width:1496px; border-collapse:collapse"><tbody>'
        '<tr><td style="padding:4px 8px; border:1px solid rgb(201,201,201); '
        'vertical-align:middle; text-align:left">本周工作总结</td>'
        '<td style="padding:4px 8px; border:1px solid rgb(201,201,201); '
        'vertical-align:middle; text-align:left">完成情况</td></tr>'
        '<tr><td style="padding:4px 8px; border:1px solid rgb(201,201,201); '
        'vertical-align:middle; text-align:left">'
        "回复华东师范大学，确认 Aibika 目前还能转什么专业</td>"
        '<td style="padding:4px 8px; border:1px solid rgb(201,201,201); '
        'vertical-align:middle; text-align:left">已完成</td></tr>'
        "</tbody></table>"
    )
    client = FakeWeComClient(
        departments=[_dept(1, "总部"), _dept(2, "申请服务部", 1)],
        employees=[_user("u1", "涂晴云", 2)],
        reports={
            "j1": _journal_info(
                report_time=REPORT_TIME,
                userid="u1",
                apply_data=text_apply_data(html_body),
            )
        },
    )
    sync_departments(db_session, client)
    sync_employees(db_session, client)
    sync_weekly_reports(db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client)

    report = db_session.scalar(
        select(WeeklyReport).where(WeeklyReport.external_id == "j1")
    )
    assert report is not None
    # 前提自检：raw 必须确实是富文本，防止夹具退化后测不到目标场景
    assert "<td" in report.content_raw
    assert report.content.strip()
    assert report.content_language == "chinese"


def test_weekly_report_sync_is_idempotent(db_session: Session) -> None:
    client = _report_client()
    sync_departments(db_session, client)
    sync_employees(db_session, client)

    start = datetime(2026, 9, 1)
    end = datetime(2026, 9, 10)

    first = sync_weekly_reports(db_session, start, end, client)
    assert first.status == SyncJobStatus.SUCCESS
    assert first.success_count == 1
    # RawRecord = 2 部门 + 1 员工 + 1 周报
    assert _count(db_session, RawRecord) == 4
    assert _count(db_session, WeeklyReport) == 1

    second = sync_weekly_reports(db_session, start, end, client)
    assert second.success_count == 0
    assert second.skipped_count == 1
    assert _count(db_session, WeeklyReport) == 1
    assert _count(db_session, RawRecord) == 4


def test_weekly_report_is_traceable_to_raw_record(db_session: Session) -> None:
    client = _report_client()
    sync_departments(db_session, client)
    sync_employees(db_session, client)
    sync_weekly_reports(db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client)

    report = db_session.scalar(
        select(WeeklyReport).where(WeeklyReport.external_id == "j1")
    )
    assert report is not None
    assert report.raw_record_id is not None
    raw = db_session.get(RawRecord, report.raw_record_id)
    assert raw is not None
    assert raw.source == DataSource.WECOM
    assert raw.source_type == "weekly_report"
    assert raw.external_id == "j1"
    # 原始 payload 完整保存
    assert raw.raw_payload["info"]["submitter"]["userid"] == "u1"
    # 周期由 report_time 推导。09-07 是周一，按归类口径归上一周期，
    # 因此归到上一周（08-31~09-06）而不是周一所在的那一周 —— 见
    # tests/test_time_utils.py::test_weekly_report_submitted_on_monday_counts_as_previous_week
    assert report.start_date == date(2026, 8, 31)
    assert report.end_date == date(2026, 9, 6)
    assert report.employee_id is not None


def test_original_content_is_never_overwritten(db_session: Session) -> None:
    """需求第 24 节第 10 条：原始周报永远不可被 AI 结果覆盖。"""
    client = _report_client()
    sync_departments(db_session, client)
    sync_employees(db_session, client)
    sync_weekly_reports(db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client)

    report = db_session.scalar(
        select(WeeklyReport).where(WeeklyReport.external_id == "j1")
    )
    assert report is not None
    original_raw = report.content_raw
    report.content = "【AI 摘要】本周主要做了系统优化"
    db_session.commit()

    # payload 未变化 -> 跳过处理 -> AI 写入的内容不会被原始数据覆盖
    sync_weekly_reports(db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client)
    db_session.refresh(report)
    assert report.content == "【AI 摘要】本周主要做了系统优化"
    assert report.content_raw == original_raw


def test_payload_change_triggers_reprocessing(db_session: Session) -> None:
    client = _report_client()
    sync_departments(db_session, client)
    sync_employees(db_session, client)
    sync_weekly_reports(db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client)

    client.reports["j1"] = _journal_info(
        report_time=REPORT_TIME,
        userid="u1",
        apply_data=text_apply_data("内容已修改：新增知识库维护"),
    )
    result = sync_weekly_reports(
        db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client
    )
    assert result.success_count == 1
    assert _count(db_session, WeeklyReport) == 1
    report = db_session.scalar(
        select(WeeklyReport).where(WeeklyReport.external_id == "j1")
    )
    assert report is not None and "知识库维护" in report.content


def test_non_analysis_payload_change_keeps_cached_analysis(db_session: Session) -> None:
    """企微原始 payload 有无关字段变化时，不得清空同正文的 AI 缓存。"""
    from app.services.runtime_config_service import update_runtime_values

    # 该用例验证的是「自动入队开启」时的缓存保护语义。
    update_runtime_values(db_session, {"ai_auto_enqueue_on_sync": "true"})
    db_session.commit()
    client = _report_client()
    sync_departments(db_session, client)
    sync_employees(db_session, client)
    sync_weekly_reports(db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client)
    report = db_session.scalar(select(WeeklyReport).where(WeeklyReport.external_id == "j1"))
    assert report is not None
    report.ai_checked_at = datetime(2026, 9, 9)
    report.analysis_payload_hash = report_payload_hash(report)
    report.ai_duplicate_flag = False
    queue = db_session.scalar(select(AnalysisQueue).where(AnalysisQueue.report_id == report.id))
    assert queue is not None
    queue.status = AnalysisQueueStatus.SUCCESS
    queue.payload_hash = report.analysis_payload_hash
    db_session.commit()

    # 触发 RawRecord 重新处理，但正文、员工、周期等模型输入完全未变。
    client.reports["j1"]["opaque_upstream_revision"] = "v2"
    result = sync_weekly_reports(db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client)
    assert result.success_count == 1
    db_session.refresh(report)
    db_session.refresh(queue)
    assert report.ai_checked_at is not None
    assert report.analysis_payload_hash == report_payload_hash(report)
    assert queue.status == AnalysisQueueStatus.SUCCESS


# --------------------------------------------------------------------------- #
# 失败隔离与日志
# --------------------------------------------------------------------------- #
def test_single_bad_record_does_not_break_batch(db_session: Session) -> None:
    client = FakeWeComClient(
        departments=[_dept(1, "总部")],
        employees=[_user("u1", "张三", 1)],
        reports={
            "j1": _journal_info(report_time=REPORT_TIME, apply_data=text_apply_data("A")),
            "j2": _journal_info(report_time=REPORT_TIME, apply_data=text_apply_data("B")),
            "j3": _journal_info(report_time=REPORT_TIME, apply_data=text_apply_data("C")),
        },
        detail_errors={"j2": RuntimeError("mock upstream 500")},
    )
    sync_departments(db_session, client)
    sync_employees(db_session, client)

    result = sync_weekly_reports(
        db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client
    )

    assert result.success_count == 2
    assert result.failed_count == 1
    assert result.status == SyncJobStatus.PARTIAL
    assert _count(db_session, WeeklyReport) == 2
    assert result.errors[0]["external_id"] == "j2"


def test_sync_job_logs_timings_and_counts(db_session: Session) -> None:
    client = _report_client()
    sync_departments(db_session, client)
    sync_employees(db_session, client)
    result = sync_weekly_reports(
        db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client
    )

    job = db_session.get(SyncJob, result.job_id)
    assert job is not None
    assert job.source == DataSource.WECOM
    assert job.type == "weekly_reports"
    assert job.start_time is not None and job.end_time is not None
    assert job.end_time >= job.start_time
    assert job.success_count == 1
    assert job.failed_count == 0
    assert job.status == SyncJobStatus.SUCCESS
    assert job.params["start"].startswith("2026-09-01")
    assert job.params["created_count"] == 1
    assert job.params["created_weekly_count"] == 1


def test_report_sync_records_new_count_separately_from_reprocessing(
    db_session: Session,
) -> None:
    from app.services.sync_scheduler import serialize_sync_job

    start, end = datetime(2026, 9, 1), datetime(2026, 9, 10)
    original = _report_client()
    first = sync_weekly_reports(db_session, start, end, original)
    assert serialize_sync_job(db_session.get(SyncJob, first.job_id))["created_count"] == 1

    repeated = sync_weekly_reports(db_session, start, end, original)
    repeated_job = db_session.get(SyncJob, repeated.job_id)
    assert repeated_job.params["created_count"] == 0
    assert repeated_job.params["created_weekly_count"] == 0
    assert (repeated_job.success_count, repeated_job.skipped_count) == (0, 1)

    changed = FakeWeComClient(reports={"j1": _journal_info(
        report_time=REPORT_TIME,
        userid="u1",
        apply_data=text_apply_data("本周新增了另一项进展"),
    )})
    refreshed = sync_weekly_reports(db_session, start, end, changed)
    refreshed_job = db_session.get(SyncJob, refreshed.job_id)
    assert refreshed_job.params["created_count"] == 0
    assert refreshed_job.params["created_weekly_count"] == 0
    assert refreshed_job.success_count == 1
    assert serialize_sync_job(refreshed_job)["created_count"] == 0


def test_report_sync_new_weekly_count_excludes_other_report_types(
    db_session: Session,
) -> None:
    client = FakeWeComClient(reports={
        "weekly": _journal_info(
            report_time=REPORT_TIME,
            template_name="本周工作汇报",
            template_id="tpl-weekly",
            apply_data=text_apply_data("周报正文"),
        ),
        "daily": _journal_info(
            report_time=REPORT_TIME,
            template_name="每日日报",
            template_id="tpl-daily",
            apply_data=text_apply_data("日报正文"),
        ),
    })
    result = sync_weekly_reports(
        db_session, datetime(2026, 9, 1), datetime(2026, 9, 10), client
    )
    job = db_session.get(SyncJob, result.job_id)
    assert job.params["created_count"] == 2
    assert job.params["created_weekly_count"] == 1


def test_department_failure_counts_as_partial(db_session: Session) -> None:
    """部门单条失败同样不中断批次。"""

    class BrokenClient(FakeWeComClient):
        def list_departments(self):  # type: ignore[no-untyped-def]
            return [
                RawPayload("wecom", "department", "1", {"id": 1, "name": "总部", "parentid": 0}),
                # external_id 为 None，写入时违反 NOT NULL 约束
                RawPayload("wecom", "department", None, {"id": 2, "name": "信息部"}),
                RawPayload("wecom", "department", "3", {"id": 3, "name": "市场部"}),
            ]

    result = sync_departments(db_session, BrokenClient())
    assert result.success_count == 2
    assert result.failed_count >= 1
    assert result.status in (SyncJobStatus.PARTIAL, SyncJobStatus.FAILED)


def test_lookback_window_default(db_session: Session) -> None:
    client = _report_client()
    sync_departments(db_session, client)
    sync_employees(db_session, client)
    now = datetime(2026, 9, 10)
    sync_weekly_reports(db_session, None, now, client)
    start_seen, end_seen = client.list_calls[0]
    assert end_seen == now
    assert start_seen == now - timedelta(days=30)


def test_full_history_sync_survives_upstream_rate_limiting(
    db_session: Session, monkeypatch
) -> None:
    """上游限流时，全历史抓取必须退避重试并最终把记录全部存下来。

    这是「抓全部历史周报只抓到一点」的端到端复现与守护。原先的三个缺陷叠加：
    详情调用没节流 → 撞 45009 → 限流错误直接抛出 → 被单条容错当成「这一条失败」
    继续猛打 → 任务以 PARTIAL 收场（看着像成功，实际只存进限流前那几篇）。

    这里用**真实 WeComClient** 打 httpx MockTransport，只让前几次详情调用返回限流，
    然后恢复正常 —— 修复后应该全部同步成功，没有失败条目。
    """
    import json as _json

    import httpx

    from app.connectors.wecom.client import WeComClient as RealClient

    reports = {
        f"hist-{index}": _journal_info(
            report_time=REPORT_TIME + index,
            apply_data=text_apply_data(f"第 {index} 篇历史周报"),
        )
        for index in range(6)
    }
    # 前 2 次详情调用返回限流（同一条记录会退避重试），之后放行。
    # 阈值必须小于「退避次数 + 1」：否则那条记录会在退避用尽后正确地失败，
    # 那是预期行为，不是这个用例要验的东西。
    detail_calls = {"n": 0}
    list_calls = {"n": 0}
    rate_limited_until = 2

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(
                200,
                json={"errcode": 0, "errmsg": "ok", "access_token": "tk", "expires_in": 7200},
            )
        if "get_record_list" in request.url.path:
            list_calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "errmsg": "ok",
                    "journaluuid_list": list(reports.keys()),
                    "endflag": 1,
                },
            )
        if "get_record_detail" in request.url.path:
            detail_calls["n"] += 1
            if detail_calls["n"] <= rate_limited_until:
                return httpx.Response(
                    200, json={"errcode": 45009, "errmsg": "api freq out of limit"}
                )
            body = _json.loads(request.content)
            uuid = body["journaluuid"]
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "errmsg": "ok",
                    "info": {**reports[uuid], "journal_uuid": uuid},
                },
            )
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    # 不真的睡：退避是逻辑正确性，不是本测试要测的时长
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    client = RealClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    result = sync_weekly_reports(
        db_session,
        datetime(1970, 1, 1),
        datetime(2026, 9, 16),
        client,
        full_history=True,
    )

    assert result.failed_count == 0, f"限流把同步打成了部分失败：{result.errors[:3]}"
    assert result.success_count == 6
    assert _count(db_session, WeeklyReport) == 6
    # 确认限流确实发生过（否则这个测试没测到东西）。
    # 注意 mock 的列表接口对**每个**月度片段都返回全部 6 条 ID（真实企微不会），
    # 所以 detail_calls 远大于 6 —— 这里只断言「至少发生过限流」。
    assert detail_calls["n"] >= rate_limited_until + 6
