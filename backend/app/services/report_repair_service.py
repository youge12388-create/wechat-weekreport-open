"""历史周报数据修复：补员工关联、按模板修正分类、重算周期、重判语言。

安全边界：
  - 默认 dry_run=True，只返回将发生的修改；
  - 只更新 WeeklyReport.employee_id / report_type / template_name / title /
    start_date / end_date / content_language，不触碰 RawRecord.raw_payload，
    不删除任何记录，不调用模型；
  - 无法确定归属或分类的记录只进入 unresolved，不猜测。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.enums import DataSource, EmployeeStatus
from app.models.raw_record import RawRecord
from app.models.weekly_report import WeeklyReport
from app.core.time_utils import period_range
from app.processors.language_detector import detect_content_language
from app.services.runtime_config_service import runtime_settings


def _submitter_userid(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    submitter = info.get("submitter")
    if not isinstance(submitter, dict):
        return None
    userid = submitter.get("userid")
    return str(userid) if userid else None


def repair_report_employee_links(
    session: Session, *, dry_run: bool = True
) -> dict[str, Any]:
    """把已有 RawRecord 里的提交人 userid 补回 WeeklyReport.employee_id。"""
    reports = session.scalars(
        select(WeeklyReport).where(WeeklyReport.employee_id.is_(None))
    ).all()
    employees = {
        employee.external_id: employee
        for employee in session.scalars(
            select(Employee).where(Employee.source == DataSource.WECOM)
        )
    }

    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    placeholder_employees: dict[str, Employee] = {}
    for report in reports:
        raw = (
            session.get(RawRecord, report.raw_record_id)
            if report.raw_record_id
            else None
        )
        userid = _submitter_userid(raw.raw_payload if raw else None)
        employee = employees.get(userid) if userid else None
        if employee is None and not userid:
            unresolved.append(
                {
                    "report_id": report.id,
                    "external_id": report.external_id,
                    "submitter_userid": userid,
                    "reason": "原始记录缺少提交人",
                }
            )
            continue

        action = "linked_existing"
        if employee is None:
            # 历史账号已从企微通讯录消失：建立 MISSING 占位员工。
            # 它不会进入在职统计；若后续重新同步到真实员工，会按 external_id 覆盖姓名/状态。
            action = "created_missing_employee"
            employee = placeholder_employees.get(userid or "")
            if employee is None and not dry_run:
                employee = Employee(
                    external_id=userid or "",
                    name=userid or "",
                    status=EmployeeStatus.MISSING,
                    source=DataSource.WECOM,
                )
                session.add(employee)
                session.flush()
                placeholder_employees[userid or ""] = employee
                employees[userid or ""] = employee

        changes.append(
            {
                "report_id": report.id,
                "external_id": report.external_id,
                "submitter_userid": userid,
                "employee_id": employee.id if employee else None,
                "employee_name": employee.name if employee else userid,
                "action": action,
            }
        )
        if not dry_run and employee is not None:
            report.employee_id = employee.id

    if not dry_run and changes:
        session.commit()

    return {
        "checked": len(reports),
        "changed": len(changes),
        "unresolved": len(unresolved),
        "changes": changes,
        "unresolved_items": unresolved,
        "dry_run": dry_run,
    }


def _dominant_template_metadata(
    session: Session,
) -> dict[str, dict[str, str]]:
    """从已正确分类的记录里学习 template_id -> 类型/模板名。"""
    rows = session.execute(
        select(
            WeeklyReport.template_id,
            WeeklyReport.report_type,
            WeeklyReport.template_name,
        ).where(
            WeeklyReport.template_id.isnot(None),
            WeeklyReport.report_type != "other",
        )
    ).all()
    type_counts: dict[str, Counter[str]] = defaultdict(Counter)
    name_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for template_id, report_type, template_name in rows:
        if not template_id:
            continue
        if report_type:
            type_counts[template_id][report_type] += 1
        if template_name:
            name_counts[template_id][template_name] += 1

    result: dict[str, dict[str, str]] = {}
    for template_id, counts in type_counts.items():
        report_type = counts.most_common(1)[0][0]
        names = name_counts.get(template_id)
        template_name = names.most_common(1)[0][0] if names else ""
        result[template_id] = {
            "report_type": report_type,
            "template_name": template_name,
        }
    return result


def repair_report_classifications(
    session: Session, *, dry_run: bool = True
) -> dict[str, Any]:
    """按同一 template_id 的已知正确样本，修正“其他”及缺失模板名。"""
    metadata = _dominant_template_metadata(session)
    reports = session.scalars(
        select(WeeklyReport).where(WeeklyReport.report_type == "other")
    ).all()

    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for report in reports:
        template_id = report.template_id
        known = metadata.get(template_id) if template_id else None
        if known is None:
            unresolved.append(
                {
                    "report_id": report.id,
                    "external_id": report.external_id,
                    "template_id": template_id,
                    "reason": "没有同一模板的正确分类样本",
                }
            )
            continue

        new_type = known["report_type"]
        new_name = report.template_name or known["template_name"] or None
        old_title = report.title
        new_title = old_title
        if new_name and (not old_title or old_title.endswith("汇报")):
            new_title = new_name

        changes.append(
            {
                "report_id": report.id,
                "external_id": report.external_id,
                "template_id": template_id,
                "before": {
                    "report_type": report.report_type,
                    "template_name": report.template_name,
                    "title": old_title,
                },
                "after": {
                    "report_type": new_type,
                    "template_name": new_name,
                    "title": new_title,
                },
            }
        )
        if not dry_run:
            report.report_type = new_type
            report.template_name = new_name
            report.title = new_title

    if not dry_run and changes:
        session.commit()

    return {
        "checked": len(reports),
        "changed": len(changes),
        "unresolved": len(unresolved),
        "changes": changes,
        "unresolved_items": unresolved,
        "dry_run": dry_run,
    }


def repair_report_periods(
    session: Session, *, dry_run: bool = True
) -> dict[str, Any]:
    """按当前周期规则重算 start_date / end_date（含归属回溯）。

    历史数据是在旧推导规则下入库的：周一才交上来的那一篇被算成了**本周**，于是
    本周凭空多出一篇、本人反被当成已提交（周五催交就会漏掉他）。周期是推导值
    而非接口原值，因此可以安全重算 —— 唯一依据是接口给的 submitted_at，
    不猜测、不动正文、不删除记录。

    回溯天数取**运行时配置**（设置页那个值），否则管理员在界面上从 1 天放宽到
    2 天之后，重跑修复仍然只按 1 天算 —— 界面显示的和实际生效的会对不上。
    """
    grace = runtime_settings(session).report_period_attribution_days
    reports = session.scalars(
        select(WeeklyReport).where(WeeklyReport.submitted_at.isnot(None))
    ).all()

    changes: list[dict[str, Any]] = []
    for report in reports:
        start, end = period_range(
            report.submitted_at,
            report.report_type or "other",
            attribution_days=grace,
        )
        if report.start_date == start and report.end_date == end:
            continue
        changes.append(
            {
                "report_id": report.id,
                "external_id": report.external_id,
                "submitted_at": report.submitted_at.isoformat(),
                "report_type": report.report_type,
                "before": {
                    "start_date": report.start_date.isoformat()
                    if report.start_date
                    else None,
                    "end_date": report.end_date.isoformat() if report.end_date else None,
                },
                "after": {"start_date": start.isoformat(), "end_date": end.isoformat()},
            }
        )
        if not dry_run:
            report.start_date = start
            report.end_date = end

    if not dry_run and changes:
        session.commit()

    return {
        "checked": len(reports),
        "changed": len(changes),
        "changes": changes,
        "dry_run": dry_run,
    }


def repair_report_languages(session: Session, *, dry_run: bool = True) -> dict[str, Any]:
    """按当前检测口径（清洗后正文优先）重判 content_language。

    历史数据是用**未清洗富文本**判定的：表格模板的 HTML 标记把拉丁字母占比
    推到 92%+，中文周报被误标 english，「仅英文周报」筛选与翻译入口随之失真。
    语言是派生值，唯一依据是已存档的正文本身，可安全重算 —— 不调用模型、
    不动正文、不删除记录。content 为空才回退 content_raw（与同步同口径）。
    """
    reports = session.scalars(select(WeeklyReport)).all()

    changes: list[dict[str, Any]] = []
    for report in reports:
        new_language = detect_content_language(report.content or report.content_raw)
        if report.content_language == new_language:
            continue
        changes.append(
            {
                "report_id": report.id,
                "external_id": report.external_id,
                "before": {"content_language": report.content_language},
                "after": {"content_language": new_language},
            }
        )
        if not dry_run:
            report.content_language = new_language

    if not dry_run and changes:
        session.commit()

    return {
        "checked": len(reports),
        "changed": len(changes),
        "changes": changes,
        "dry_run": dry_run,
    }


def repair_reports(session: Session, *, dry_run: bool = True) -> dict[str, Any]:
    """执行全部修复，返回一份可审计的变更清单。"""
    links = repair_report_employee_links(session, dry_run=dry_run)
    classifications = repair_report_classifications(session, dry_run=dry_run)
    periods = repair_report_periods(session, dry_run=dry_run)
    languages = repair_report_languages(session, dry_run=dry_run)
    return {
        "dry_run": dry_run,
        "employee_links": links,
        "classifications": classifications,
        "periods": periods,
        "languages": languages,
    }
