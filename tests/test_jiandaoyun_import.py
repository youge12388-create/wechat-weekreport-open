"""简道云历史周报导入脚本测试。

覆盖：宽表解析分组、自然周换算、员工匹配（映射/精确同名/离职建档）、
幂等（重复导入不产生重复数据）、回滚删除。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))

import import_jiandaoyun_history as importer  # noqa: E402

from app.models.enums import DataSource, EmployeeStatus  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.raw_record import RawRecord  # noqa: E402
from app.models.weekly_report import WeeklyReport  # noqa: E402


@pytest.fixture
def sample_xlsx(tmp_path: Path) -> Path:
    """构造一份最小简道云宽表：两个报告组、六区块各一条事项。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "工作周报 Weekly Work Summary"
    header = ["所属周"] + [None] * 38
    header[1], header[2], header[3], header[4] = (
        "姓名/Name", "部门/Deparement", "日期/Date", "选择抄送上级/CC Supervisor"
    )
    header[5], header[17], header[36], header[37] = (
        "本周重点工作/Key Tasks", "本周工作表现及个人总结说明", "提交人", "提交时间"
    )
    ws.append(header)
    ws.append([None] * 39)  # 第二层表头占位（解析器跳过前两行）

    def group_row(week, name, dept, submit_dt, title, status, progress, remark):
        row = [None] * 39
        row[0], row[1], row[2] = week, name, dept
        row[3] = submit_dt
        row[5], row[6], row[7], row[8] = title, status, progress, remark
        row[17] = "本周总结文本"
        row[36], row[37] = name, submit_dt
        return row

    ws.append(group_row("第22周", "Iris 涂晴云", "申请服务部",
                        datetime(2026, 5, 29, 18, 31), "做方案", "已完成 – Completed",
                        None, None))
    ws.append([None] * 9 + ["日常事项", "进行中 – In Progress", 0.5, "等回复"])
    ws.append(group_row("第21周", "Mystery 哈希人", "华太西就国际",
                        datetime(2026, 5, 22, 19, 0), "海外市场", "已完成 – Completed",
                        None, "Signed 1 contract"))
    wb.save(tmp_path / "jd.xlsx")
    return tmp_path / "jd.xlsx"


@pytest.fixture
def seed_employees(db_session: Session) -> None:
    db_session.add(Employee(external_id="Iris", name="Iris 涂晴云",
                            status=EmployeeStatus.ACTIVE, source=DataSource.WECOM))
    db_session.commit()


def test_parse_groups(sample_xlsx: Path) -> None:
    groups = importer.parse_workbook(sample_xlsx)
    assert len(groups) == 2
    first, second = groups
    assert (first["week"], first["name"], first["department"]) == (
        "第22周", "Iris 涂晴云", "申请服务部"
    )
    # 第二行事项无人员信息，应归入上一组（向下填充）
    assert len(first["items"]) == 2
    assert first["items"][1]["category"] == "本周日常例行工作"
    assert second["items"][0]["title"] == "海外市场"


def test_week_range_natural_week() -> None:
    # 2026 年第 22 周 = 05-25（周一）~ 05-31（周日）
    assert importer.week_range("第22周", date(2026, 5, 29)) == (
        date(2026, 5, 25), date(2026, 5, 31)
    )
    # 2026-01-01 是周四：ISO 第 1 周 = 2025-12-29 ~ 2026-01-04，第 2 周从 01-05 起
    assert importer.week_range("第1周", date(2026, 1, 4)) == (
        date(2025, 12, 29), date(2026, 1, 4)
    )
    assert importer.week_range("第2周", date(2026, 1, 5)) == (
        date(2026, 1, 5), date(2026, 1, 11)
    )
    assert importer.week_range("垃圾文本", None) is None


def test_render_content_contains_sections(sample_xlsx: Path) -> None:
    groups = importer.parse_workbook(sample_xlsx)
    text = importer.render_content(groups[0])
    assert "【本周工作总结】" in text
    assert "【本周重点工作】" in text
    assert "1. 做方案（已完成）" in text
    # 每个类别内重新编号
    assert "1. 日常事项（进行中，进度 50%）" in text
    assert "说明：等回复" in text


def test_external_id_stable(sample_xlsx: Path) -> None:
    groups = importer.parse_workbook(sample_xlsx)
    assert importer.external_id_for(groups[0]) == importer.external_id_for(groups[0])


def test_employee_resolution_paths(
    db_session: Session, sample_xlsx: Path, seed_employees: None
) -> None:
    """mapped / exact / created 三条匹配路径。"""
    importer.MANUAL_NAME_MAP["Mystery 哈希人"] = "mystery_hash"
    db_session.add(Employee(external_id="mystery_hash", name="神秘人",
                            status=EmployeeStatus.ACTIVE, source=DataSource.WECOM))
    db_session.commit()
    try:
        resolver = importer.EmployeeResolver(db_session)
        emp1, how1 = resolver.resolve("Mystery 哈希人", "华太西就国际")
        assert (emp1.external_id, how1) == ("mystery_hash", "mapped")
        emp2, how2 = resolver.resolve("Iris 涂晴云", "申请服务部")
        assert (emp2.name, how2) == ("Iris 涂晴云", "exact")
        emp3, how3 = resolver.resolve("无人 404", "渠道资源部")
        assert how3 == "created"
        assert emp3.status == EmployeeStatus.RESIGNED
        assert emp3.source == DataSource.JIANDAOYUN
        assert emp3.position == "渠道资源部"
    finally:
        importer.MANUAL_NAME_MAP.pop("Mystery 哈希人", None)


def test_import_apply_idempotent_and_rollback(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    sample_xlsx: Path,
    seed_employees: None,
) -> None:
    """--apply 两次执行只建一次数据；--rollback 清空（含离职员工）。"""
    # 直接用 db_session 跑核心流程（绕开脚本自己的 session_scope）
    from contextlib import contextmanager

    @contextmanager
    def fake_scope():
        yield db_session

    monkeypatch.setattr(importer, "session_scope", fake_scope)
    assert importer.run_import(sample_xlsx, apply=True) == 0
    reports = db_session.scalars(select(WeeklyReport)).all()
    assert len(reports) == 2
    assert all(r.source == DataSource.JIANDAOYUN for r in reports)
    assert all(r.start_date is not None and r.end_date is not None for r in reports)
    raws = db_session.scalars(select(RawRecord)).all()
    assert len(raws) == 2
    created_emp = db_session.scalar(
        select(Employee).where(Employee.name == "Mystery 哈希人")
    )
    assert created_emp is not None
    assert created_emp.status == EmployeeStatus.RESIGNED

    # 幂等：重复导入不产生重复数据
    assert importer.run_import(sample_xlsx, apply=True) == 0
    assert len(db_session.scalars(select(WeeklyReport)).all()) == 2
    assert len(db_session.scalars(select(RawRecord)).all()) == 2

    # 回滚（项目 Session 为 autoflush=False，断言前需显式 flush 落库删除）
    assert importer.run_rollback(drop_employees=True) == 0
    db_session.flush()
    db_session.expire_all()
    assert db_session.scalars(select(WeeklyReport)).all() == []
    assert db_session.scalars(select(RawRecord)).all() == []
    assert db_session.scalar(
        select(Employee).where(Employee.name == "Mystery 哈希人")
    ) is None


def test_hashed_account_is_readable_and_import_remains_idempotent(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    sample_xlsx: Path,
) -> None:
    """哈希账号用可读展示名，源值保留，重复导入复用同一员工。"""
    from contextlib import contextmanager

    account_id = "69674c70cdb5e8472cd12fba"
    workbook = openpyxl.load_workbook(sample_xlsx)
    ws = workbook.active
    ws.cell(row=5, column=2).value = account_id
    ws.cell(row=5, column=37).value = account_id
    hashed_xlsx = sample_xlsx.with_name("hashed-account.xlsx")
    workbook.save(hashed_xlsx)
    workbook.close()

    @contextmanager
    def fake_scope():
        yield db_session

    monkeypatch.setattr(importer, "session_scope", fake_scope)
    assert importer.run_import(hashed_xlsx, apply=True) == 0

    employee = db_session.scalar(
        select(Employee).where(Employee.external_id == account_id)
    )
    assert employee is not None
    assert employee.name == "简道云历史账号（69674c70）"
    assert employee.status == EmployeeStatus.RESIGNED
    raw = next(
        record
        for record in db_session.scalars(select(RawRecord)).all()
        if record.raw_payload.get("name") == account_id
    )
    assert raw is not None
    assert raw.raw_payload["name"] == account_id

    # 真实再跑时通过 source external_id 找回更名后的员工，不额外建档。
    assert importer.run_import(hashed_xlsx, apply=True) == 0
    employees = db_session.scalars(
        select(Employee).where(Employee.external_id == account_id)
    ).all()
    reports = db_session.scalars(
        select(WeeklyReport).where(WeeklyReport.source == DataSource.JIANDAOYUN)
    ).all()
    assert len(employees) == 1
    assert len(reports) == 2
