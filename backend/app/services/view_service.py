"""视图层公共助手：周期计算与序列化。

Sprint 2 以只读查询为主，接口返回 dict 而非强 schema。
统计口径（与看板一致，勿在别处另写一套）：

- 「本周」区间 = 前端传入的 start / end；缺省时取当前自然周的周一~周日
- 已提交 = 在 [start,end] 内 **有汇报周期重叠** 的去重员工数
- 未提交 = 在职员工数 - 已提交（未提交基线 = 全部在职员工，已确认）
- 汇报周期重叠判定：WeeklyReport.start_date <= end AND end_date >= start
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.time_utils import utcnow
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import EmployeeStatus, SyncJobStatus
from app.models.sync_job import SyncJob
from app.models.weekly_report import WeeklyReport
from app.services.data_scope import current_scope


class InvalidPeriodError(ValueError):
    """start / end 参数非法。"""


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def business_now() -> datetime:
    """业务时区的「现在」。库里存 UTC naive，但周五几点是业务概念，必须换算。"""
    settings = get_settings()
    return utcnow() + timedelta(hours=settings.report_timezone_offset_hours)


def default_period(now: datetime | None = None) -> tuple[date, date]:
    """默认统计周期：没到本周截止时间就看上周，过了就看本周。

    周报是周五晚上才交的。若周一一早就盯着本周看，满屏都是「未提交」，
    那是假警报而不是真的有人拖延 —— 所以未到截止时间默认退回上一周。
    手动传 start/end 时不走这里。
    """
    settings = get_settings()
    current = now or business_now()
    monday = current.date() - timedelta(days=current.weekday())
    due_at = datetime.combine(
        monday + timedelta(days=settings.report_due_weekday),
        time(hour=settings.report_due_hour),
    )
    if current < due_at:
        # 还没到交周报的时点 → 看上一周
        return monday - timedelta(days=7), monday - timedelta(days=1)
    return monday, monday + timedelta(days=6)


def default_week_range() -> tuple[date, date]:
    """向后兼容的别名：现在默认周期会随截止时间在本周/上周之间切换。"""
    return default_period()


def resolve_period(start: str | None, end: str | None) -> tuple[date, date]:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date is None or end_date is None:
        return default_period()
    if end_date < start_date:
        raise InvalidPeriodError("end 不得早于 start")
    return start_date, end_date


def report_period_overlap_conditions(start: date, end: date):
    """返回两条 SQL 条件（闭区间重叠）：start<=end 且 实际结束>=start。

    用法：where(*report_period_overlap_conditions(s, e))
    """
    from sqlalchemy import func as _func

    eff_end = _func.coalesce(WeeklyReport.end_date, WeeklyReport.start_date)
    return (WeeklyReport.start_date <= end, eff_end >= start)


def employee_status_label(status: str | None) -> str:
    return {
        EmployeeStatus.ACTIVE: "在职",
        EmployeeStatus.RESIGNED: "离职",
        EmployeeStatus.DISABLED: "已禁用",
        EmployeeStatus.PENDING: "未激活",
        EmployeeStatus.MISSING: "疑似离职",
    }.get(status or "", status or "未知")


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def serialize_employee(employee: Employee, department_name: str | None = None) -> dict[str, Any]:
    return {
        "id": employee.id,
        "external_id": employee.external_id,
        "name": employee.name,
        "department_id": employee.department_id,
        "department_name": department_name,
        "position": employee.position,
        "status": employee.status,
        "status_label": employee_status_label(employee.status),
        "report_exempt": bool(employee.report_exempt),
        # 登录授权：个人级开关只用于「追加」权限（跨部门领导）。
        # 继承自部门树的授权刻意不在这里展开 —— 前端有专门的授权接口
        # 计算「最终能不能登录」，避免两处各算一套口径。
        "login_enabled": bool(employee.login_enabled),
        "login_role": employee.login_role,
        "source": employee.source,
        "last_seen_at": employee.last_seen_at.isoformat() if employee.last_seen_at else None,
    }


def serialize_report(report: WeeklyReport, employee: Employee | None = None, department_name: str | None = None) -> dict[str, Any]:
    return {
        "id": report.id,
        "employee_id": report.employee_id,
        "employee_name": employee.name if employee else None,
        "department_name": department_name,
        "external_id": report.external_id,
        "report_type": report.report_type,
        "title": report.title,
        "content_language": report.content_language,
        "period_start": report.start_date.isoformat() if report.start_date else None,
        "period_end": report.end_date.isoformat() if report.end_date else None,
        "submitted_at": report.submitted_at.isoformat() if report.submitted_at else None,
        "template_name": report.template_name,
        "source": report.source,
        "raw_record_id": report.raw_record_id,
    }


def serialize_department(
    dept: Department, employee_count: int = 0, exempt: bool = False
) -> dict[str, Any]:
    return {
        "id": dept.id,
        "external_id": dept.external_id,
        "name": dept.name,
        "parent_id": dept.parent_id,
        "status": dept.status,
        "source": dept.source,
        "employee_count": employee_count,
        # 免提交部门：前端据此打「免提交」标签，不再算作欠交
        "exempt": exempt,
        # 登录授权三态：null=未设置（继承上级）/ true=允许 / false=显式禁止
        "login_enabled": dept.login_enabled,
        "login_role": dept.login_role,
    }


# --------------------------------------------------------------------------- #
# 统计查询
# --------------------------------------------------------------------------- #
# session.info 里存免提交部门查询结果的键（session.info 是 SQLAlchemy 提供的
# 每 Session 用户字典；接口层每次请求一个新 Session，正好是请求内的缓存作用域）。
_EXEMPT_IDS_CACHE_KEY = "view_service.exempt_department_ids"


def exempt_department_ids(session: Session) -> set[int]:
    """免提交周报的部门 id（按名称精确匹配，不含子部门）。

    结果缓存在 ``session.info`` 上：这个函数经 ``reportable_employee_conditions``
    与 ``non_exempt_report_condition`` 被间接调用，首页一次渲染就重复几十次
    （实测 dashboard_summary 41 条 SQL 里有 18 条是这个查询），而答案在整个
    请求里不会变。缓存键带上名称元组，名称变化（含测试 monkeypatch）自然失效。
    每次返回新 set，避免调用方就地修改污染缓存。
    """
    names = tuple(
        n.strip() for n in get_settings().report_exempt_departments if n and n.strip()
    )
    cached = session.info.get(_EXEMPT_IDS_CACHE_KEY)
    if cached is not None and cached[0] == names:
        return set(cached[1])

    if not names:
        ids: set[int] = set()
    else:
        ids = set(session.scalars(select(Department.id).where(Department.name.in_(names))))
    session.info[_EXEMPT_IDS_CACHE_KEY] = (names, frozenset(ids))
    return set(ids)


def reportable_employee_conditions(session: Session, status: str | None = None) -> list:
    """「应提交周报」的员工条件：在职（或指定状态）且个人/部门均未免提交。

    注意 SQL 三值逻辑：department_id 为 NULL 的员工，用 NOT IN 会被整条
    条件判为 NULL 而漏掉，所以必须把 is_(None) 单独 or 出来。

    dept_admin 会话时（get_db 注入了 DataScope）自动叠加部门子树过滤，
    看板/洞察/导出等复用本函数的查询全部 inherit 该范围。
    """
    conditions = [
        Employee.status == (status or EmployeeStatus.ACTIVE),
        Employee.report_exempt.is_(False),
    ]
    exempt = exempt_department_ids(session)
    if exempt:
        conditions.append(
            or_(
                Employee.department_id.is_(None),
                Employee.department_id.notin_(exempt),
            )
        )
    scope = current_scope(session)
    if scope.is_scoped:
        conditions.append(scope.employee_condition())
    return conditions


def reportable_employee_ids(session: Session) -> set[int]:
    """应提交周报的在职员工 id（已剔除免提交部门）。"""
    return set(
        session.scalars(
            select(Employee.id).where(*reportable_employee_conditions(session))
        )
    )


def non_exempt_report_condition(session: Session):
    """管理聚合与 AI 只使用当前需提交人员的周报。

    未关联员工的历史档案保留在查询范围内，避免历史修复前的数据被静默丢失；
    已关联到个人免提交员工或免提交部门的周报则不参与计算。

    dept_admin 会话时叠加子树过滤：未归属员工的历史周报同样被排除
    （无法证明属于管辖部门）。本条件被 insight_service / agent_tools /
    ai_search_service 广泛复用，是 AI 检索范围隔离的落点。
    """
    exempt = exempt_department_ids(session)
    excluded = select(Employee.id).where(
        or_(Employee.report_exempt.is_(True), Employee.department_id.in_(exempt))
    )
    condition = or_(
        WeeklyReport.employee_id.is_(None), WeeklyReport.employee_id.notin_(excluded)
    )
    scope = current_scope(session)
    if scope.is_scoped:
        from sqlalchemy import and_

        condition = and_(condition, scope.report_employee_id_condition())
    return condition


def ai_report_condition(session: Session):
    """向后兼容的 AI 过滤别名；统一复用管理聚合的免提交口径。"""
    return non_exempt_report_condition(session)


def report_ai_exempt(session: Session, report: WeeklyReport) -> bool:
    return session.scalar(
        select(WeeklyReport.id).where(WeeklyReport.id == report.id, ai_report_condition(session))
    ) is None


def department_employee_counts(
    session: Session, exclude_dept_ids: set[int] | None = None
) -> dict[int, int]:
    conditions = list(reportable_employee_conditions(session))
    excluded = exempt_department_ids(session) | (exclude_dept_ids or set())
    if excluded:
        conditions.append(
            or_(
                Employee.department_id.is_(None),
                Employee.department_id.notin_(excluded),
            )
        )
    rows = session.execute(
        select(Employee.department_id, func.count(Employee.id))
        .where(*conditions)
        .group_by(Employee.department_id)
    )
    return {dept_id: count for dept_id, count in rows if dept_id is not None}


def active_employees_by_department(
    session: Session, exclude_dept_ids: set[int] | None = None
) -> dict[int, list[int]]:
    conditions = list(reportable_employee_conditions(session))
    excluded = exempt_department_ids(session) | (exclude_dept_ids or set())
    if excluded:
        conditions.append(
            or_(
                Employee.department_id.is_(None),
                Employee.department_id.notin_(excluded),
            )
        )
    rows = session.execute(select(Employee.department_id, Employee.id).where(*conditions))
    result: dict[int, list[int]] = {}
    for dept_id, emp_id in rows:
        if dept_id is not None:
            result.setdefault(dept_id, []).append(emp_id)
    return result


def submitted_employee_ids(session: Session, start: date, end: date) -> set[int]:
    eff_end = func.coalesce(WeeklyReport.end_date, WeeklyReport.start_date)
    conditions = [WeeklyReport.start_date <= end, eff_end >= start]
    scope = current_scope(session)
    if scope.is_scoped:
        conditions.append(scope.report_employee_id_condition())
    rows = session.execute(
        select(distinct(WeeklyReport.employee_id)).where(*conditions)
    )
    return {emp_id for (emp_id,) in rows if emp_id is not None}


def submitted_per_department(session: Session, start: date, end: date) -> dict[int, int]:
    """每个部门内、周期重叠的去重已提交员工数。"""
    eff_end = func.coalesce(WeeklyReport.end_date, WeeklyReport.start_date)
    conditions = [WeeklyReport.start_date <= end, eff_end >= start]
    scope = current_scope(session)
    if scope.is_scoped:
        conditions.append(scope.employee_condition())
    rows = session.execute(
        select(Employee.department_id, func.count(distinct(WeeklyReport.employee_id)))
        .join(WeeklyReport, WeeklyReport.employee_id == Employee.id)
        .where(*conditions)
        .group_by(Employee.department_id)
    )
    return {dept_id: count for dept_id, count in rows if dept_id is not None}


def department_name_map(session: Session) -> dict[int, str]:
    return {
        dept.id: dept.name
        for dept in session.scalars(select(Department))
    }


def last_sync_at(
    session: Session, sync_type: str | None = None
) -> datetime | None:
    """最近一次成功同步的结束时间（用于前端「数据新鲜度」提示）。

    sync_type 为空时取任意类型最近一次成功同步，反映整体数据新鲜度。
    """
    stmt = select(SyncJob).where(SyncJob.status == SyncJobStatus.SUCCESS)
    if sync_type:
        stmt = stmt.where(SyncJob.type == sync_type)
    job = session.scalars(stmt.order_by(SyncJob.end_time.desc()).limit(1)).first()
    return job.end_time if job else None
