"""在读周报或调用模型前，确定用户本轮明确的时间与对象范围。"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_utils import business_date, iso_week_range, utcnow
from app.models.department import Department
from app.models.employee import Employee
from app.services.data_scope import current_scope
from app.services.view_service import resolve_period


@dataclass(frozen=True)
class AgentQueryPlan:
    start: date
    end: date
    scope: str
    department_id: int | None
    department_name: str | None
    employee_id: int | None
    employee_name: str | None
    time_source: str
    clarification: dict[str, Any] | None = None


def _month_range(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _shift_month(today: date, months: int) -> date:
    index = today.year * 12 + today.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1]))


_CHINESE_MONTHS = {
    name: month for month, name in enumerate(
        ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), 1
    )
}
_ABSOLUTE_MONTH_RE = re.compile(
    r"(?<!\d)(?:(?P<year>20\d{2})年\s*|(?P<relative_year>今年|去年)\s*)?"
    r"(?P<month>1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月(?!\d)"
)


def _month_expression(question: str, known_names: set[str]) -> re.Match[str] | None:
    for month_match in _ABSOLUTE_MONTH_RE.finditer(question):
        # 完整人员或部门名称覆盖月份字样时，先保留这个名称供对象识别。
        if any(
            name in question
            and len(name) > len(month_match.group(0))
            and any(
                occurrence.start() <= month_match.start() and month_match.end() <= occurrence.end()
                for occurrence in re.finditer(re.escape(name), question)
            )
            for name in known_names
        ):
            continue
        return month_match
    return None


def _explicit_period(
    question: str, today: date, month_match: re.Match[str] | None
) -> tuple[tuple[date, date] | None, str | None]:
    range_match = re.search(
        r"(20\d{2}-\d{2}-\d{2})\s*(?:至|到|~|～|—|–)\s*(20\d{2}-\d{2}-\d{2})",
        question,
    )
    if range_match:
        try:
            start, end = (date.fromisoformat(value) for value in range_match.groups())
        except ValueError:
            return None, "日期格式无效，请使用 YYYY-MM-DD 至 YYYY-MM-DD。"
        if start > end:
            return None, "日期区间起始日晚于结束日，请改为有效区间。"
        return (start, end), None

    single_date = re.search(r"20\d{2}-\d{2}-\d{2}", question)
    if single_date:
        try:
            day = date.fromisoformat(single_date.group(0))
        except ValueError:
            return None, "日期格式无效，请使用 YYYY-MM-DD。"
        return (day, day), None

    if month_match:
        month_text = month_match.group("month")
        month = int(month_text) if month_text.isascii() else _CHINESE_MONTHS[month_text]
        if month_match.group("year"):
            year = int(month_match.group("year"))
        elif month_match.group("relative_year"):
            year = today.year if month_match.group("relative_year") == "今年" else today.year - 1
        else:
            # 无年份时选择最近一个已开始的对应月份，避免跨年后查到未来。
            year = today.year if month <= today.month else today.year - 1
        return _month_range(year, month), None

    if "近一季度" in question:
        return (_shift_month(today, -3) + timedelta(days=1), today), None

    quarter_match = re.search(
        r"(?:(?P<year>20\d{2}|今年|去年)年?\s*)?第?(?P<quarter>[1-4一二三四])季度",
        question,
    )
    if quarter_match or re.search(r"本季度|这个季度|上季度", question):
        if quarter_match:
            year_text = quarter_match.group("year")
            year = (
                int(year_text) if year_text and year_text.isdigit()
                else today.year - 1 if year_text == "去年"
                else today.year
            )
            quarter_text = quarter_match.group("quarter")
            quarter = int(quarter_text) if quarter_text.isdigit() else "一二三四".index(quarter_text) + 1
        else:
            year = today.year
            quarter = (today.month - 1) // 3 + 1
            if "上季度" in question:
                quarter -= 1
                if quarter == 0:
                    year, quarter = year - 1, 4
        start_month = (quarter - 1) * 3 + 1
        return (date(year, start_month, 1), _month_range(year, start_month + 2)[1]), None

    year_match = re.search(r"(?<!\d)(20\d{2})年(?!\s*(?:1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月)", question)
    if year_match or re.search(r"今年|去年", question):
        year = int(year_match.group(1)) if year_match else today.year if "今年" in question else today.year - 1
        return (date(year, 1, 1), date(year, 12, 31)), None

    if re.search(r"上个月|上月|上一个月", question):
        first = today.replace(day=1)
        last = first.fromordinal(first.toordinal() - 1)
        return _month_range(last.year, last.month), None
    if re.search(r"本月|这个月", question):
        return _month_range(today.year, today.month), None
    if re.search(r"上上周|上上星期", question):
        start, end = iso_week_range(today)
        return (start - timedelta(days=14), end - timedelta(days=14)), None
    if re.search(r"上周|上星期", question):
        start, end = iso_week_range(today)
        return (start - timedelta(days=7), end - timedelta(days=7)), None
    if re.search(r"本周|这周|本星期|这星期", question):
        return iso_week_range(today), None
    recent = re.search(r"(?:最近|近|过去)\s*(\d+|[一二两三四五六七八九十])\s*(天|周|个月)", question)
    if recent:
        raw_count, unit = recent.groups()
        count = int(raw_count) if raw_count.isdigit() else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}[raw_count]
        start = _shift_month(today, -count) + timedelta(days=1) if unit == "个月" else today - timedelta(days=count * (7 if unit == "周" else 1) - 1)
        return (start, today), None
    if re.search(r"最近|近期|前段时间|最近一阵", question):
        return (today - timedelta(days=27), today), None
    return None, None


def _best_matches(question: str, rows: list[Any], name_getter) -> list[Any]:
    def spans(name: str) -> list[tuple[int, int]]:
        pattern = re.escape(name)
        if name[0].isascii() and name[0].isalnum():
            pattern = r"(?<![A-Za-z0-9])" + pattern
        if name[-1].isascii() and name[-1].isalnum():
            pattern += r"(?![A-Za-z0-9])"
        return [match.span() for match in re.finditer(pattern, question, re.IGNORECASE)]

    matches = [row for row in rows if name_getter(row) and spans(str(name_getter(row)))]
    if not matches:
        return []
    names = {str(name_getter(row)) for row in matches}
    occurrences = {name: spans(name) for name in names}
    selected_names = set(names)
    for name in names:
        spans = occurrences[name]
        longer_spans = [
            span
            for other, other_spans in occurrences.items()
            if len(other) > len(name)
            for span in other_spans
        ]
        # Ignore a short name only when every occurrence is contained inside a longer name.
        if spans and all(
            any(long_start <= start and end <= long_end for long_start, long_end in longer_spans)
            for start, end in spans
        ):
            selected_names.discard(name)
    return [row for row in matches if str(name_getter(row)) in selected_names]


def _employee_matches(question: str, employees: list[Employee]) -> list[Employee]:
    """完整姓名优先；英文简称只在词边界匹配，重名时交给澄清流程。"""
    exact = _best_matches(question, employees, lambda row: row.name)
    if any(" " in row.name for row in exact):
        return exact

    matches = list(exact)
    matched_ids = {row.id for row in matches}
    for employee in employees:
        parts = (employee.name or "").split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z]{2,}", parts[0]):
            continue
        alias = re.escape(parts[0])
        if employee.id not in matched_ids and re.search(
            rf"(?<![A-Za-z0-9]){alias}(?![A-Za-z0-9])", question, re.IGNORECASE
        ):
            matches.append(employee)
            matched_ids.add(employee.id)
    return matches


def _ambiguity(kind: str, rows: list[Any], name_getter, dept_name_by_id: dict[int, str]) -> dict[str, Any]:
    label = "人员" if kind == "employee" else "部门"
    options = []
    seen: set[tuple[int, str]] = set()
    for row in rows:
        name = str(name_getter(row))
        key = (int(row.id), name)
        if key in seen:
            continue
        seen.add(key)
        option: dict[str, Any] = {"label": name}
        if kind == "employee":
            option["employee_id"] = row.id
            dept = dept_name_by_id.get(row.department_id)
            if dept:
                option["label"] = f"{name}（{dept}）"
        else:
            option["department_id"] = row.id
        options.append(option)
    return {
        "reason": f"ambiguous_{kind}",
        "message": f"问题里提到的{label}无法唯一确定，请选择要查询的对象。",
        "options": options[:10],
    }


# 人名探测的误报防线：贪婪捕获常把时间词吞进候选（如「这周整体做了什么」
# 捕到「这周整体」），先剥掉首部时间词，再判断剩余部分是否集体称谓。
_PERSON_TIME_PREFIXES = (
    "上个月", "这个月", "上星期", "本星期", "这星期",
    "上上星期", "上季度", "本季度", "这个季度", "上上周",
    "上月", "本月", "上周", "本周", "这周", "本期", "本次", "当前",
)
_COLLECTIVE_PERSON_WORDS = {
    "公司", "部门", "团队", "大家", "我们", "你们", "他们", "她们", "员工", "同事",
    "整体", "全体", "全员", "各方", "各位",
    "本部门", "各部门", "这个部门", "该部门", "我们部门", "你们部门",
    "本周", "这周", "上周", "本月", "这个月", "上月", "上个月",
    "本星期", "这星期", "上星期", "本期", "本次", "当前",
    "上季度", "本季度", "这个季度", "上上周", "上上星期",
}


def _unknown_person_candidate(question: str) -> str | None:
    """从「XX 做了什么 / XX 的工作」句式提取疑似人名；无候选返回 None。

    候选先剥掉首部时间词再参与判断，避免「这周整体」「上周大家」这类
    时间词+集体词的组合被误判成查不到的员工。
    """
    match = re.search(
        r"(?:^|[，,。！？?\s])(?:(?:帮我看|帮我查|请问|查一下|看一下|看看|分析|查看))?([\u4e00-\u9fff]{2,4})(?=\s*(?:上个月|上月|本月|这个月|上周|本周|这周|做了什么|的工作|工作进展))",
        question,
    )
    if not match:
        latin = re.search(
            r"(?:分析|查看|看看|查一下|帮我看|帮我查)?\s*([A-Za-z][A-Za-z0-9_-]{1,})\s*的(?:全部|所有|历史|历次)?(?:周报|汇报)",
            question,
            re.IGNORECASE,
        )
        return latin.group(1) if latin else None
    candidate = match.group(1)
    # 时间词可能出现在候选任意一侧：「这周整体」在首，「部门这周」在尾。
    # 各剥一次且保证剥后非空；剩余部分再进集体词判断。
    for token in _PERSON_TIME_PREFIXES:
        if len(candidate) > len(token) and candidate.startswith(token):
            candidate = candidate[len(token):]
            break
    for token in _PERSON_TIME_PREFIXES:
        if len(candidate) > len(token) and candidate.endswith(token):
            candidate = candidate[:-len(token)]
            break
    if candidate in {"总结", "请总结", "概括", "请概括", "归纳", "请归纳"}:
        return None
    return candidate


def resolve_agent_query(
    session: Session,
    *,
    question: str,
    start: str | None,
    end: str | None,
    scope: str | None,
    department_id: int | None,
    employee_id: int | None,
) -> AgentQueryPlan:
    """应用问题中清楚表达的时间/对象，再校验不超出当前账号权限。"""
    today = business_date(utcnow())
    departments = list(session.scalars(select(Department)))
    all_employees = list(session.scalars(select(Employee)))
    month_match = _month_expression(
        question, {row.name for row in (*departments, *all_employees) if row.name}
    )
    explicit_period, time_error = _explicit_period(question, today, month_match)
    if time_error:
        base_start, base_end = resolve_period(start, end)
        return AgentQueryPlan(
            base_start, base_end, "period", department_id, None, employee_id, None,
            "page", {"reason": "ambiguous_time", "message": time_error, "options": []},
        )

    base_start, base_end = explicit_period or resolve_period(start, end)
    explicitly_all_open = bool(
        re.search(r"全部未解决|所有未解决|全部存量|存量卡点", question)
    )
    explicitly_all_reports = bool(re.search(
        r"(?:全部|所有)(?:的|历史|以往|往期)*(?:周报|汇报|周期)|(?:历次|历史)(?:的)?(?:周报|汇报)",
        question,
    ))
    effective_scope = (
        "period"
        if explicit_period
        else "all"
        if explicitly_all_open or explicitly_all_reports or (scope or "").lower() == "all"
        else "period"
    )
    has_time_language = bool(re.search(
        r"最近|近期|过去|以前|之前|前段时间|前些日子|那段时间|近来|近\s*\d+",
        question,
    ))
    time_source = (
        "question" if explicit_period else
        "question_unresolved" if has_time_language else
        "page"
    )
    # 只移除本轮实际识别出的时间片段，保留其他文字供对象匹配。
    entity_question = (
        question[:month_match.start()] + " " + question[month_match.end():]
        if month_match and explicit_period else question
    )

    access = current_scope(session)
    accessible_departments = (
        departments
        if access.is_all
        else [item for item in departments if item.id in access.department_ids]
    )
    accessible_department_ids = {item.id for item in accessible_departments}
    dept_names = {item.id: item.name for item in departments}
    global_department_matches = _best_matches(entity_question, departments, lambda row: row.name)
    department_matches = [row for row in global_department_matches if row.id in accessible_department_ids]
    if len({row.id for row in global_department_matches}) > 1:
        if not department_matches:
            return AgentQueryPlan(
                base_start, base_end, effective_scope, department_id, None, employee_id, None,
                time_source, {"reason": "inaccessible_department", "message": "当前账号无法查询问题中指定的部门，请检查权限或联系管理员。", "options": []},
            )
        return AgentQueryPlan(
            base_start, base_end, effective_scope, department_id, None, employee_id, None,
            time_source, _ambiguity("department", department_matches, lambda row: row.name, dept_names),
        )
    if global_department_matches and not department_matches:
        return AgentQueryPlan(
            base_start, base_end, effective_scope, department_id, None, employee_id, None,
            time_source, {"reason": "inaccessible_department", "message": "当前账号无法查询问题中指定的部门，请检查权限或选择可访问的部门。", "options": []},
        )
    if not global_department_matches:
        department_hint = re.search(
            r"(?:(?:请问|帮我查|帮我看|查一下|看看|分析))?([\u3400-\u9fffA-Za-z0-9]{2,12}(?:分公司|事业部|中心|部门|部))",
            entity_question,
        )
        if department_hint and not department_hint.group(1).endswith(
            ("全部", "部分", "各部", "各部门", "各个部门", "所有部门", "全部部门", "每个部门", "这个部门", "该部门", "本部门", "我们部门", "你们部门")
        ) and not department_hint.group(1).startswith(
            ("哪个", "哪些", "各个", "各部", "所有", "这些", "当前", "本月", "本周",
             "部门", "各部门", "本部门", "这个部门", "该部门", "我们部门", "你们部门")
        ):
            return AgentQueryPlan(
                base_start, base_end, effective_scope, department_id, None, employee_id, None,
                time_source, {"reason": "unknown_department", "message": "无法在当前可访问范围内匹配该部门，请核对部门全称或联系管理员。", "options": []},
            )

    accessible_employees = (
        all_employees
        if access.is_all
        else [row for row in all_employees if row.department_id in access.department_ids]
    )
    accessible_employee_ids = {row.id for row in accessible_employees}
    global_employee_matches = _employee_matches(entity_question, all_employees)
    # 用户从歧义候选中点选后，原问题仍带同一个简称；尊重已选 ID，
    # 否则会再次返回同一组候选，形成无法退出的澄清循环。
    if employee_id is not None and any(row.id == employee_id for row in global_employee_matches):
        global_employee_matches = [row for row in global_employee_matches if row.id == employee_id]
    employee_matches = [row for row in global_employee_matches if row.id in accessible_employee_ids]
    if len({row.id for row in global_employee_matches}) > 1:
        if not employee_matches:
            return AgentQueryPlan(
                base_start, base_end, effective_scope, department_id, None, employee_id, None,
                time_source, {"reason": "inaccessible_employee", "message": "当前账号无法查询问题中指定的人员，请检查权限或联系管理员。", "options": []},
            )
        return AgentQueryPlan(
            base_start, base_end, effective_scope, department_id, None, employee_id, None,
            time_source, _ambiguity("employee", employee_matches, lambda row: row.name, dept_names),
        )
    if global_employee_matches and not employee_matches:
        return AgentQueryPlan(
            base_start, base_end, effective_scope, department_id, None, employee_id, None,
            time_source, {"reason": "inaccessible_employee", "message": "当前账号无法查询问题中指定的人员，请检查权限或选择可访问的人员。", "options": []},
        )
    if not global_employee_matches:
        candidate = _unknown_person_candidate(entity_question)
        if candidate and candidate not in _COLLECTIVE_PERSON_WORDS and candidate not in {
            row.name for row in department_matches
        }:
            return AgentQueryPlan(
                base_start, base_end, effective_scope, department_id, None, None, None,
                time_source, {"reason": "unknown_employee", "message": "无法在当前可访问范围内唯一定位该人员，请核对姓名或联系管理员。", "options": []},
            )

    explicit_department = department_matches[0] if department_matches else None
    explicit_employee = employee_matches[0] if employee_matches else None
    if explicit_department is not None and explicit_employee is None:
        # 问题指定部门优先于页面中残留的员工筛选。
        employee_id = None
    if employee_id is not None and explicit_employee is None:
        chosen_employee = session.get(Employee, employee_id)
        if (
            chosen_employee is None
            or chosen_employee.department_id is None
            or (not access.is_all and chosen_employee.department_id not in access.department_ids)
        ):
            return AgentQueryPlan(
                base_start, base_end, effective_scope, department_id, None, employee_id, None,
                time_source, {"reason": "inaccessible_employee", "message": "当前账号无法查询所选人员，请检查权限或选择可访问的人员。", "options": []},
            )
        explicit_employee = chosen_employee

    if explicit_employee is not None:
        if explicit_department is not None and explicit_employee.department_id != explicit_department.id:
            return AgentQueryPlan(
                base_start, base_end, effective_scope, department_id, None, explicit_employee.id, explicit_employee.name,
                time_source, {"reason": "conflicting_scope", "message": "问题中指定的人员与部门不一致，请修改查询条件。", "options": []},
            )
        # 问题中的人员优先于页面部门筛选。身份只在当前 DataScope 内匹配或校验。
        department_id = explicit_department.id if explicit_department else None
        employee_id = explicit_employee.id
        department_name = dept_names.get(department_id) if department_id else None
        employee_name = explicit_employee.name
    elif explicit_department is not None:
        department_id = explicit_department.id
        employee_id = None
        department_name = explicit_department.name
        employee_name = None
    else:
        department_name = dept_names.get(department_id) if department_id else None
        selected_employee = session.get(Employee, employee_id) if employee_id else None
        employee_name = selected_employee.name if selected_employee else None

    return AgentQueryPlan(
        base_start,
        base_end,
        effective_scope,
        department_id,
        department_name,
        employee_id,
        employee_name,
        time_source,
    )
