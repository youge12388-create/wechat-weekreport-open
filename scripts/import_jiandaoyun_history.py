#!/usr/bin/env python
"""简道云历史周报一次性导入（2026 第 5~22 周）。

数据路径（遵守分层约束：原始数据先落 RawRecord，业务表只放派生结果）：
    简道云导出 xlsx
      -> RawRecord(source=jiandaoyun, source_type=weekly_report, raw_payload=原始行数据)
      -> WeeklyReport(source=jiandaoyun, external_id=确定性哈希, content=渲染后正文)

约定（项目负责人 2026-10-08 确认）：
  1. 员工匹配：优先按「人工映射表 + 员工表精确同名」匹配在职/已有员工；
     匹配不上的按**离职员工**建档入库（status=resigned，position=简道云部门，
     source=jiandaoyun，不参与企微同步、不计入未提交统计）。
  2. 周期归属：按简道云「所属周」换算 2026 年自然周（周一~周日），
     不按提交时间重新推导；与提交时间推导结果不一致时在预览报告中列出。

用法（项目根目录，PYTHONPATH 由脚本自行注入）：
    # 预览（默认 dry-run，只读，不改库）
    python scripts/import_jiandaoyun_history.py \
        --xlsx "docs/简道云工作周报汇总_原始导出.xlsx"

    # 正式导入（SQLite 库会先自动备份到 <db>.bak-jiandaoyun-<时间戳>）
    python scripts/import_jiandaoyun_history.py --xlsx ... --apply

    # 回滚（删除 source=jiandaoyun 的周报与原始记录；新建的离职员工保留，
    # 如需一并删除加 --drop-employees）
    python scripts/import_jiandaoyun_history.py --rollback [--drop-employees]

幂等：按 unique(source, external_id) 去重，重复执行不会产生重复数据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import openpyxl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.time_utils import utcnow  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.enums import DataSource, EmployeeStatus, ProcessStatus  # noqa: E402
from app.models.raw_record import RawRecord  # noqa: E402
from app.models.weekly_report import WeeklyReport  # noqa: E402
from app.processors.language_detector import detect_content_language  # noqa: E402

TEMPLATE_ID = "jiandaoyun-weekly"
TEMPLATE_NAME = "简道云工作周报"

# 企微账号为拼音/别名、无法按显示名精确匹配的员工（项目负责人确认口径）。
# 键 = 简道云「姓名」列原文，值 = 员工表 external_id（wecom userid）。
MANUAL_NAME_MAP: dict[str, str] = {
    "Mia 马逸凡": "mayifan",
    "Brandy杨华": "YangHua",
    "Sammi 张云云": "ZhangYunYun",
    "Jasmine 王思蓉": "WangSiRong",
    "khadija": "khadijatulkubra",
    "Michelle 蔺雪梅": "Michelle",
    "Ruby 刘婥": "Ruby",
    "Nora 胡文静": "Nora",
    # 系统里有两个 Nuri（u5 为测试号，nurzhannat 为哈萨克斯坦合作同事）。
    "Nuri": "nurzhannat",
}

# 简道云同步产物中的账号哈希不是可读姓名。展示时用明确的历史账号标签，
# 原始账号仍保留在 RawRecord.raw_payload 和 Employee.external_id 中。
JIANDAOYUN_ACCOUNT_DISPLAY_NAMES: dict[str, str] = {
    "69af884b9e257aee53aac326": "简道云历史账号（69af884b）",
    "69674c70cdb5e8472cd12fba": "简道云历史账号（69674c70）",
    "6948ce4886a490f9528d444f": "简道云历史账号（6948ce48）",
    "695338d9927db9db8d681e17": "简道云历史账号（695338d9）",
}

# 简道云宽表的六个事项区块：(类别名, 事项列, 状态列, 进度列, 说明列)。
CATEGORIES: list[tuple[str, int, int, int, int]] = [
    ("本周重点工作", 5, 6, 7, 8),
    ("本周日常例行工作", 9, 10, 11, 12),
    ("本周其他工作", 13, 14, 15, 16),
    ("下周计划-重点工作", 18, 19, 20, 21),
    ("下周计划-日常例行工作", 22, 23, 24, 25),
    ("下周计划-其他工作", 26, 27, 28, 29),
]


def _s(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_status(value: object) -> str | None:
    text = _s(value)
    if not text:
        return None
    if "已完成" in text or "Completed" in text:
        return "已完成"
    if "进行中" in text or "In Progress" in text:
        return "进行中"
    if "未开始" in text or "Not Started" in text:
        return "未开始"
    return text


def _as_utc_naive(value: datetime | None) -> datetime | None:
    """简道云时间为东八区本地时刻；库内约定存 UTC naive。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        from app.core.time_utils import to_naive_utc

        return to_naive_utc(value)
    offset = get_settings().report_timezone_offset_hours
    return value - timedelta(hours=offset)


def parse_workbook(path: Path) -> list[dict]:
    """解析简道云宽表 → 每人每周一组。"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()

    groups: list[dict] = []
    current: dict | None = None
    for row in rows[2:]:  # 前两行为双层表头
        row = list(row) + [None] * max(0, 39 - len(row))
        if _s(row[0]) and _s(row[1]):
            current = {
                "week": _s(row[0]),
                "name": _s(row[1]),
                "department": _s(row[2]),
                "report_date": row[3] if isinstance(row[3], datetime) else None,
                "cc": _s(row[4]),
                "summary": _s(row[17]),
                "visit": [_s(row[30]), _s(row[31]), _s(row[32])],
                "sign": [_s(row[33]), _s(row[34]), _s(row[35])],
                "submitter": _s(row[36]),
                "submit_time": row[37] if isinstance(row[37], datetime) else None,
                "update_time": row[38] if isinstance(row[38], datetime) else None,
                "items": [],
            }
            groups.append(current)
        if current is None:
            continue
        for cat, c_t, c_s, c_p, c_r in CATEGORIES:
            title, status = _s(row[c_t]), _clean_status(row[c_s])
            progress, remark = row[c_p], _s(row[c_r])
            if title is None and status is None and progress is None and remark is None:
                continue
            current["items"].append(
                {"category": cat, "title": title, "status": status,
                 "progress": progress, "remark": remark}
            )
    return groups


def week_range(week_label: str, fallback_day: date | None) -> tuple[date, date] | None:
    """「第N周」→ 2026 年 ISO 自然周（周一~周日）。"""
    match = re.match(r"第(\d+)周", week_label or "")
    if not match:
        return None
    week_no = int(match.group(1))
    year = (fallback_day or date.today()).year
    try:
        monday = date.fromisocalendar(year, week_no, 1)
    except ValueError:
        return None
    return monday, monday + timedelta(days=6)


def render_content(group: dict) -> str:
    """把结构化事项渲染成可读正文（页面展示 + AI 输入）。"""
    lines: list[str] = []
    if group["summary"]:
        lines.append("【本周工作总结】")
        lines.append(group["summary"])
    current_cat: str | None = None
    seq = 0
    for item in group["items"]:
        if item["category"] != current_cat:
            current_cat = item["category"]
            seq = 0
            lines.append(f"【{current_cat}】")
        seq += 1
        head = f"{seq}. {item['title'] or '（未填写事项名称）'}"
        tail = ""
        if item["status"]:
            tail += item["status"]
        progress = item["progress"]
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            tail += f"，进度 {round(float(progress) * 100)}%"
        elif progress:
            tail += f"，进度：{progress}"
        if tail:
            head += f"（{tail}）"
        lines.append(head)
        if item["remark"]:
            lines.append(f"   说明：{item['remark']}")
    return "\n".join(lines).strip()


def external_id_for(group: dict) -> str:
    seed = f"{group['week']}|{group['name']}|{group['submit_time']}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
    return f"jd-{digest}"


class EmployeeResolver:
    """员工匹配：人工映射 > 精确同名 > 新建离职员工（岗位=简道云部门）。"""

    def __init__(self, session) -> None:
        self.session = session
        self.by_external = {
            emp.external_id: emp
            for emp in session.query(Employee).all()
        }
        self.by_name: dict[str, list[Employee]] = {}
        for emp in self.by_external.values():
            self.by_name.setdefault(emp.name, []).append(emp)
        self.created: list[Employee] = []
        self.resolutions: dict[str, tuple[Employee | None, str]] = {}

    def resolve(self, jd_name: str, department: str | None) -> tuple[Employee | None, str]:
        """返回 (员工, 匹配方式)：人工映射、已导入账号、同名或新建。"""
        if jd_name in self.resolutions:
            return self.resolutions[jd_name]

        external_id = MANUAL_NAME_MAP.get(jd_name)
        if external_id and external_id in self.by_external:
            emp = self.by_external[external_id]
            self.resolutions[jd_name] = (emp, "mapped")
            return emp, "mapped"

        # 已导入的简道云员工可能使用了可读展示名，按源账号复用以保持幂等。
        emp = self.by_external.get(jd_name)
        if emp is not None and emp.source == DataSource.JIANDAOYUN:
            self.resolutions[jd_name] = (emp, "source_id")
            return emp, "source_id"

        candidates = self.by_name.get(jd_name, [])
        if len(candidates) == 1:
            emp = candidates[0]
            self.resolutions[jd_name] = (emp, "exact")
            return emp, "exact"
        # 无匹配，或同名多条且无人工映射（猜错归属比新建离职档案更糟）：
        # 一律按离职员工建档，岗位标注 = 简道云部门。

        emp = Employee(
            external_id=jd_name,
            name=JIANDAOYUN_ACCOUNT_DISPLAY_NAMES.get(jd_name, jd_name),
            position=department or None,
            status=EmployeeStatus.RESIGNED,
            source=DataSource.JIANDAOYUN,
        )
        self.session.add(emp)
        self.session.flush()
        self.by_external[jd_name] = emp
        self.by_name.setdefault(jd_name, []).append(emp)
        self.created.append(emp)
        self.resolutions[jd_name] = (emp, "created")
        return emp, "created"


def backup_sqlite() -> Path | None:
    url = get_settings().db_url or ""
    match = re.match(r"sqlite:///(.+)", url)
    if not match:
        return None
    db_path = Path(match.group(1))
    if not db_path.exists():
        return None
    target = db_path.with_name(f"{db_path.name}.bak-jiandaoyun-{utcnow():%Y%m%d_%H%M%S}")
    shutil.copyfile(db_path, target)
    return target


def run_import(xlsx: Path, apply: bool) -> int:
    """dry-run 用不提交的裸会话（预览绝不能写库）；--apply 走 session_scope 提交。"""
    if apply:
        with session_scope() as session:
            return _import_with_session(session, xlsx, apply=True)
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        return _import_with_session(session, xlsx, apply=False)
    finally:
        session.rollback()
        session.close()


def _import_with_session(session, xlsx: Path, apply: bool) -> int:
    groups = parse_workbook(xlsx)
    if not groups:
        print("[import] 未解析到任何周报数据，请检查文件。")
        return 1

    created_reports = 0
    skipped_reports = 0
    period_mismatch: list[str] = []
    match_lines: list[str] = []

    resolver = EmployeeResolver(session)
    for group in groups:
        emp, how = resolver.resolve(group["name"], group["department"])
        ext_id = external_id_for(group)
        fallback_day = (
            group["submit_time"].date()
            if isinstance(group["submit_time"], datetime)
            else (group["report_date"].date() if isinstance(group["report_date"], datetime) else None)
        )
        period = week_range(group["week"], fallback_day)
        # 口径核对：所属周 vs 提交日所在自然周（仅提示，不改变归属）
        if fallback_day and period:
            monday = fallback_day - timedelta(days=fallback_day.weekday())
            if monday != period[0]:
                period_mismatch.append(
                    f"{group['week']} {group['name']}：提交日 {fallback_day}"
                    f"（自然周 {monday}~）与所属周 {period[0]}~{period[1]} 不一致"
                )

        payload = {
            "week": group["week"],
            "name": group["name"],
            "department": group["department"],
            "report_date": str(group["report_date"] or ""),
            "cc_supervisors": group["cc"],
            "summary": group["summary"],
            "planned_visits": dict(zip(["intermediaries", "schools", "students"], group["visit"])),
            "planned_signings": dict(zip(["intermediaries", "schools", "students"], group["sign"])),
            "submitter": group["submitter"],
            "submit_time": str(group["submit_time"] or ""),
            "update_time": str(group["update_time"] or ""),
            "items": group["items"],
        }
        content = render_content(group)
        content_raw = json.dumps(payload, ensure_ascii=False)
        content_hash = hashlib.sha256(content_raw.encode("utf-8")).hexdigest()

        existing_report = (
            session.query(WeeklyReport)
            .filter(
                WeeklyReport.source == DataSource.JIANDAOYUN,
                WeeklyReport.external_id == ext_id,
            )
            .one_or_none()
        )
        match_lines.append(
            f"  [{how:>7}] {group['week']} {group['name']} -> "
            f"{emp.name if emp else '(无)'}：事项 {len(group['items'])} 条"
        )
        if existing_report is not None:
            skipped_reports += 1
            continue

        if apply:
            raw_record = (
                session.query(RawRecord)
                .filter(
                    RawRecord.source == DataSource.JIANDAOYUN,
                    RawRecord.source_type == "weekly_report",
                    RawRecord.external_id == ext_id,
                )
                .one_or_none()
            )
            if raw_record is None:
                raw_record = RawRecord(
                    source=DataSource.JIANDAOYUN,
                    source_type="weekly_report",
                    external_id=ext_id,
                )
                session.add(raw_record)
            raw_record.raw_payload = payload
            raw_record.content_hash = content_hash
            raw_record.fetched_at = utcnow()
            raw_record.process_status = ProcessStatus.PROCESSED
            raw_record.processed_at = utcnow()

            report = WeeklyReport(
                source=DataSource.JIANDAOYUN,
                external_id=ext_id,
                employee_id=emp.id if emp else None,
                report_type="weekly",
                title=TEMPLATE_NAME,
                content=content,
                content_raw=content_raw,
                content_language=detect_content_language(content),
                start_date=period[0] if period else None,
                end_date=period[1] if period else None,
                submitted_at=_as_utc_naive(group["submit_time"]),
                template_id=TEMPLATE_ID,
                template_name=TEMPLATE_NAME,
                raw_record_id=None,
            )
            session.add(report)
            session.flush()
            report.raw_record_id = raw_record.id
            created_reports += 1

    if apply:
        print(
            f"[import] --apply：新增周报 {created_reports} 篇，"
            f"已存在跳过 {skipped_reports} 篇；"
            f"新建离职员工 {len(resolver.created)} 人。"
        )
    else:
        print(
            f"[import] dry-run：将导入周报 {len(groups) - skipped_reports} 篇"
            f"（已存在 {skipped_reports} 篇跳过）。加 --apply 正式写入。"
        )

    print("\n== 员工匹配明细 ==")
    for line in match_lines:
        print(line)
    if resolver.created:
        print("\n== 新建离职员工（position=简道云部门）==")
        for emp in resolver.created:
            print(f"  {emp.name} | 部门：{emp.position}")
    if period_mismatch:
        print(f"\n== 周期归属核对提示（{len(period_mismatch)} 条，仍按所属周归属）==")
        for line in period_mismatch[:30]:
            print(f"  {line}")
    return 0


def run_rollback(drop_employees: bool) -> int:
    with session_scope() as session:
        reports = (
            session.query(WeeklyReport)
            .filter(WeeklyReport.source == DataSource.JIANDAOYUN)
            .all()
        )
        raws = (
            session.query(RawRecord)
            .filter(
                RawRecord.source == DataSource.JIANDAOYUN,
                RawRecord.source_type == "weekly_report",
            )
            .all()
        )
        print(f"[rollback] 将删除周报 {len(reports)} 篇、原始记录 {len(raws)} 条。")
        for report in reports:
            session.delete(report)
        for raw in raws:
            session.delete(raw)
        if drop_employees:
            emps = (
                session.query(Employee)
                .filter(Employee.source == DataSource.JIANDAOYUN)
                .all()
            )
            print(f"[rollback] 同时删除导入的离职员工 {len(emps)} 人。")
            for emp in emps:
                session.delete(emp)
    print("[rollback] 完成。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="简道云历史周报一次性导入")
    parser.add_argument(
        "--xlsx",
        type=str,
        default=None,
        help="简道云导出 xlsx 路径（导入/预览时必填）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="正式写入（默认 dry-run 只预览）",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="回滚：删除 source=jiandaoyun 的周报与原始记录",
    )
    parser.add_argument(
        "--drop-employees",
        action="store_true",
        help="回滚时一并删除导入的离职员工",
    )
    args = parser.parse_args()

    if args.rollback:
        return run_rollback(args.drop_employees)

    if not args.xlsx:
        parser.error("需要 --xlsx 指向简道云导出文件（或使用 --rollback）")
    xlsx = Path(args.xlsx)
    if not xlsx.exists():
        print(f"[import] 文件不存在：{xlsx}")
        return 1

    if args.apply:
        backup = backup_sqlite()
        if backup:
            print(f"[import] 已备份数据库 -> {backup}")
        else:
            print("[import] 非 SQLite 本地库，跳过自动备份（生产库请先自行备份）。")
    return run_import(xlsx, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
