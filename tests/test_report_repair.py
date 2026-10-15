"""历史周报数据修复：补员工关联与按模板纠偏分类。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_utils import utcnow
from app.models.employee import Employee
from app.models.enums import DataSource
from app.models.raw_record import RawRecord
from app.models.weekly_report import WeeklyReport
from app.services.report_repair_service import (
    repair_report_classifications,
    repair_report_employee_links,
    repair_reports,
)


def _raw(session: Session, external_id: str, userid: str) -> RawRecord:
    row = RawRecord(
        source=DataSource.WECOM,
        source_type="weekly_report",
        external_id=external_id,
        raw_payload={"info": {"submitter": {"userid": userid}}},
        content_hash=f"hash-{external_id}",
        fetched_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _report(
    session: Session,
    *,
    external_id: str,
    raw_record_id: int | None = None,
    report_type: str = "weekly",
    template_id: str | None = "tpl-1",
    template_name: str | None = "本周工作汇报",
    employee_id: int | None = None,
) -> WeeklyReport:
    row = WeeklyReport(
        employee_id=employee_id,
        external_id=external_id,
        report_type=report_type,
        title=template_name,
        content="content",
        content_raw="content",
        content_language="chinese",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        submitted_at=datetime(2026, 9, 11, 10, 0, 0),
        source=DataSource.WECOM,
        raw_record_id=raw_record_id,
        template_id=template_id,
        template_name=template_name,
    )
    session.add(row)
    session.flush()
    return row


def test_repair_links_reports_from_raw_submitter(db_session: Session) -> None:
    employee = Employee(
        external_id="u1",
        name="张三",
        status="active",
        source=DataSource.WECOM,
    )
    db_session.add(employee)
    db_session.flush()
    raw = _raw(db_session, "j1", "u1")
    report = _report(db_session, external_id="j1", raw_record_id=raw.id)
    db_session.commit()

    dry = repair_report_employee_links(db_session, dry_run=True)
    assert dry["changed"] == 1
    assert db_session.get(WeeklyReport, report.id).employee_id is None

    applied = repair_report_employee_links(db_session, dry_run=False)
    assert applied["changed"] == 1
    assert db_session.get(WeeklyReport, report.id).employee_id == employee.id


def test_repair_creates_missing_employee_placeholder(db_session: Session) -> None:
    raw = _raw(db_session, "gone-1", "historical_user")
    report = _report(db_session, external_id="gone-1", raw_record_id=raw.id)
    db_session.commit()

    result = repair_report_employee_links(db_session, dry_run=False)

    assert result["changed"] == 1
    employee = db_session.scalar(
        select(Employee).where(Employee.external_id == "historical_user")
    )
    assert employee is not None
    assert employee.name == "historical_user"
    assert employee.status == "missing"
    assert db_session.get(WeeklyReport, report.id).employee_id == employee.id


def test_repair_classification_learns_from_same_template(db_session: Session) -> None:
    _report(
        db_session,
        external_id="good-1",
        report_type="weekly",
        template_id="tpl-weekly",
        template_name="本周工作汇报",
    )
    wrong = _report(
        db_session,
        external_id="wrong-1",
        report_type="other",
        template_id="tpl-weekly",
        template_name=None,
    )
    db_session.commit()

    dry = repair_report_classifications(db_session, dry_run=True)
    assert dry["changed"] == 1
    assert db_session.get(WeeklyReport, wrong.id).report_type == "other"

    repair_report_classifications(db_session, dry_run=False)
    refreshed = db_session.get(WeeklyReport, wrong.id)
    assert refreshed is not None
    assert refreshed.report_type == "weekly"
    assert refreshed.template_name == "本周工作汇报"
    assert refreshed.title == "本周工作汇报"


def test_repair_all_never_touches_raw_payload(db_session: Session) -> None:
    employee = Employee(
        external_id="u1",
        name="张三",
        status="active",
        source=DataSource.WECOM,
    )
    db_session.add(employee)
    db_session.flush()
    raw = _raw(db_session, "j1", "u1")
    original = dict(raw.raw_payload)
    report = _report(
        db_session,
        external_id="j1",
        raw_record_id=raw.id,
        report_type="other",
        template_id=None,
        template_name=None,
    )
    db_session.commit()

    result = repair_reports(db_session, dry_run=False)
    db_session.refresh(raw)
    assert result["dry_run"] is False
    assert raw.raw_payload == original
    assert db_session.get(WeeklyReport, report.id).employee_id == employee.id


def test_repair_report_languages_rechecks_cleaned_content(
    db_session: Session,
) -> None:
    """历史语言标记是用未清洗富文本判的，要能按清洗后正文重判。

    表格模板的 HTML 标记把拉丁字母占比推到 85%+，中文周报被误标 english；
    真英文周报不得被波及；dry-run 不写库。
    """
    from app.services.report_repair_service import repair_report_languages

    mislabeled = _report(db_session, external_id="j-html")
    mislabeled.content_language = "english"
    mislabeled.content = "本周完成院校沟通三所，回复华东师范大学确认转专业事宜，均已提交"
    mislabeled.content_raw = (
        '<table style="width:1496px"><tr><td style="padding:4px">'
        "本周完成院校沟通三所</td></tr></table>"
    )
    truth_english = _report(db_session, external_id="j-en")
    truth_english.content_language = "english"
    truth_english.content = (
        "This week I handled seven new student inquiries and all of them "
        "were high intent leads."
    )
    db_session.commit()

    dry = repair_report_languages(db_session, dry_run=True)
    assert dry["changed"] == 1
    assert dry["changes"][0]["after"] == {"content_language": "chinese"}
    # dry-run 绝不写库
    assert db_session.get(WeeklyReport, mislabeled.id).content_language == "english"

    result = repair_report_languages(db_session, dry_run=False)
    assert result["changed"] == 1
    assert db_session.get(WeeklyReport, mislabeled.id).content_language == "chinese"
    # 真英文周报不受影响
    assert db_session.get(WeeklyReport, truth_english.id).content_language == "english"


def test_repair_report_periods_moves_monday_report_to_previous_week(
    db_session: Session,
) -> None:
    """存量数据里「周一批交被算成本周」的记录要能被重算回上一周。

    周期是推导值而非接口原值，所以可以安全重算；依据只有 submitted_at。
    """
    from app.services.report_repair_service import repair_report_periods

    report = _report(db_session, external_id="j1")
    # 模拟旧规则算出的结果：周一（09-07）提交，却被记成本周
    report.start_date = date(2026, 9, 7)
    report.end_date = date(2026, 9, 13)
    report.submitted_at = datetime(2026, 9, 7, 3, 0, 0)
    db_session.commit()

    dry = repair_report_periods(db_session, dry_run=True)
    assert dry["changed"] == 1
    assert dry["changes"][0]["after"] == {
        "start_date": "2026-08-31",
        "end_date": "2026-09-06",
    }
    # dry-run 绝不写库
    assert db_session.get(WeeklyReport, report.id).start_date == date(2026, 9, 7)

    repair_report_periods(db_session, dry_run=False)
    refreshed = db_session.get(WeeklyReport, report.id)
    assert refreshed is not None
    assert refreshed.start_date == date(2026, 8, 31)
    assert refreshed.end_date == date(2026, 9, 6)


def test_repair_report_periods_leaves_correct_rows_untouched(db_session: Session) -> None:
    """周五正常提交的周报不该被重算波及（幂等：没有变化就不报告变化）。"""
    from app.services.report_repair_service import repair_report_periods

    _report(db_session, external_id="j1")  # 默认就是周五 09-11 提交、09-07~09-13
    db_session.commit()

    result = repair_report_periods(db_session, dry_run=False)
    assert result["changed"] == 0
