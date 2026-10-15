"""后台同步调度：排队幂等、自动触发、状态汇总。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import get_settings
from app.core.time_utils import utcnow
from app.main import create_app
from app.models.analysis_queue import AnalysisQueue
from app.models.blocker_summary_queue import BlockerSummaryQueue
from app.models.enums import AnalysisQueueStatus, SyncJobStatus, SyncType
from app.models.raw_record import RawRecord
from app.models.sync_job import SyncJob
from app.models.weekly_report import WeeklyReport
from app.services import sync_scheduler
from app.services.department_sync import sync_departments
from app.services.employee_sync import sync_employees
from app.services.runtime_config_service import runtime_settings
from app.services.sync_scheduler import (
    ALL_HISTORY_START,
    REPORT_SYNC_ALL_HISTORY,
    REPORT_SYNC_CURRENT_PERIOD,
    REPORT_SYNC_HISTORY,
    REPORT_SYNC_LAST_PERIOD,
    classify_sync_error,
    classify_sync_error_text,
    current_period_date_range,
    current_period_time_window,
    enqueue_manual_sync,
    enqueue_report_sync,
    enqueue_sync_job,
    maybe_enqueue_scheduled_sync,
    previous_period_date_range,
    previous_period_time_window,
    recover_interrupted_jobs,
    run_pending_sync_jobs,
    sync_status,
)
from tests.fake_wecom import FakeWeComClient, _dept, _journal_info, _user, text_apply_data


def _pending_types(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(SyncJob.type)
            .where(SyncJob.status == SyncJobStatus.PENDING)
            .order_by(SyncJob.id)
        )
    )


def _count(session: Session, model) -> int:  # type: ignore[no-untyped-def]
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _only_pending_report_job(session: Session) -> SyncJob:
    job = session.scalar(
        select(SyncJob)
        .where(
            SyncJob.type == SyncType.WEEKLY_REPORTS,
            SyncJob.status == SyncJobStatus.PENDING,
        )
        .order_by(SyncJob.id.desc())
    )
    assert job is not None
    return job


def test_previous_period_window_follows_natural_week(db_session: Session) -> None:
    """北京时间周五：上一周期 = 本周（周一~周日）之前的那一周。"""
    # UTC 2026-09-18 03:00 = 北京时间 11:00（周五，落在 9/14~9/20 这一周）
    now = datetime(2026, 9, 18, 3, 0, 0)
    assert previous_period_date_range(db_session, now) == (
        date(2026, 9, 7),
        date(2026, 9, 13),
    )
    start, end = previous_period_time_window(db_session, now)
    # 周期首日 00:00 与末日 23:59:59.999999 换算成 UTC naive（业务时区偏移 8 小时）。
    assert start == datetime(2026, 9, 6, 16, 0, 0)
    assert end == datetime(2026, 9, 13, 15, 59, 59, 999999)


def test_previous_period_window_on_monday_is_the_week_just_ended(
    db_session: Session,
) -> None:
    """周一取「刚结束的那一周」，与首页默认周期口径一致。"""
    # UTC 2026-09-21 01:00 = 北京时间 09:00（周一）
    now = datetime(2026, 9, 21, 1, 0, 0)
    assert previous_period_date_range(db_session, now) == (
        date(2026, 9, 14),
        date(2026, 9, 20),
    )


def test_current_period_window_covers_full_natural_week(
    db_session: Session,
) -> None:
    """本周概览的周期口径：固定整周（周一~周日），与上一周期同一套规则。

    周期末还没到也必须给完整区间，否则今天抓出来的总结写成「周一~今天」，
    本周后续周报补进来时口径会和首页对不上，又要重新生成。
    """
    # UTC 2026-09-18 03:00 = 北京时间 11:00（周五，落在 9/14~9/20 这一周）
    now = datetime(2026, 9, 18, 3, 0, 0)
    assert current_period_date_range(db_session, now) == (
        date(2026, 9, 14),
        date(2026, 9, 20),
    )
    start, end = current_period_time_window(db_session, now)
    # 抓取窗口从本周一 00:00 开始，结束于「现在」——未来的周报还不存在。
    assert start == datetime(2026, 9, 13, 16, 0, 0)
    assert end == now


def test_enqueue_report_sync_current_period_window_starts_this_week(
    db_session: Session,
) -> None:
    """current_period 模式抓的是本周，而不是上一周。"""
    job, created = enqueue_report_sync(
        db_session, mode=REPORT_SYNC_CURRENT_PERIOD
    )

    assert created is True
    assert job.type == SyncType.WEEKLY_REPORTS
    assert job.params["mode"] == REPORT_SYNC_CURRENT_PERIOD
    start = datetime.fromisoformat(job.params["start"])
    end = datetime.fromisoformat(job.params["end"])
    # 起点必须晚于「上一周期」的起点，否则等于在重复抓上周。
    previous_start, _ = previous_period_time_window(db_session)
    assert start > previous_start
    assert start < end


def test_enqueue_report_sync_only_queues_reports(db_session: Session) -> None:
    """只抓周报的入口不能顺手把部门/员工也排进队列。"""
    job, created = enqueue_report_sync(db_session, mode=REPORT_SYNC_LAST_PERIOD)

    assert created is True
    assert job.type == SyncType.WEEKLY_REPORTS
    assert _pending_types(db_session) == [SyncType.WEEKLY_REPORTS]
    assert job.params["mode"] == REPORT_SYNC_LAST_PERIOD
    start = datetime.fromisoformat(job.params["start"])
    end = datetime.fromisoformat(job.params["end"])
    assert start < end
    # 上一周期是整周（周一~周日），跨度接近 7 天。
    assert timedelta(days=6) < end - start <= timedelta(days=7)


def test_enqueue_report_sync_history_uses_days(db_session: Session) -> None:
    job, created = enqueue_report_sync(
        db_session, mode=REPORT_SYNC_HISTORY, days=14
    )

    assert created is True
    assert job.params["mode"] == REPORT_SYNC_HISTORY
    assert job.params["days"] == 14
    start = datetime.fromisoformat(job.params["start"])
    end = datetime.fromisoformat(job.params["end"])
    assert timedelta(days=13) < end - start <= timedelta(days=14)


def test_enqueue_report_sync_all_history_uses_technical_lower_bound(
    db_session: Session,
) -> None:
    job, created = enqueue_report_sync(
        db_session, mode=REPORT_SYNC_ALL_HISTORY
    )

    assert created is True
    assert job.params["mode"] == REPORT_SYNC_ALL_HISTORY
    assert "days" not in job.params
    assert datetime.fromisoformat(job.params["start"]) == ALL_HISTORY_START
    assert int(ALL_HISTORY_START.replace(tzinfo=timezone.utc).timestamp()) > 0
    assert datetime.fromisoformat(job.params["end"]) > ALL_HISTORY_START


def test_enqueue_report_sync_dedupes_repeat_clicks(db_session: Session) -> None:
    """重复点击不叠加任务：第二次返回同一条任务且 created=False。"""
    first, created_first = enqueue_report_sync(db_session)
    second, created_second = enqueue_report_sync(db_session)

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert len(_pending_types(db_session)) == 1


def test_manual_enqueue_is_idempotent(db_session: Session) -> None:
    first = enqueue_manual_sync(db_session)
    assert [item["type"] for item in first] == [
        SyncType.DEPARTMENTS,
        SyncType.EMPLOYEES,
        SyncType.WEEKLY_REPORTS,
    ]
    assert all(item["created"] for item in first)

    second = enqueue_manual_sync(db_session)
    assert all(not item["created"] for item in second)
    assert _pending_types(db_session) == [
        SyncType.DEPARTMENTS,
        SyncType.EMPLOYEES,
        SyncType.WEEKLY_REPORTS,
    ]


def _pending_job(sync_type: str) -> SyncJob:
    return SyncJob(
        source="wecom",
        type=sync_type,
        status=SyncJobStatus.PENDING,
        start_time=utcnow(),
        params={},
        triggered_by="api:queued",
        created_at=utcnow(),
        success_count=0,
        failed_count=0,
        skipped_count=0,
    )


def test_active_job_unique_index_blocks_second_active(db_session: Session) -> None:
    """应用层被绕过时，DB 必须挡下第二条同类型未结束任务。

    应用层 `_active_job` 是「先查再插」的乐观检查，多 Worker / 多实例并发时
    可能双双通过；`uq_sync_jobs_active_type` 是不依赖进程的最终保证。
    """
    db_session.add(_pending_job(SyncType.DEPARTMENTS))
    db_session.flush()

    db_session.add(_pending_job(SyncType.DEPARTMENTS))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_active_job_index_allows_history_and_other_types(
    db_session: Session,
) -> None:
    """索引只约束未结束的行：已结束的同类型任务不挡路，不同类型互不干扰。"""
    finished = _pending_job(SyncType.DEPARTMENTS)
    finished.status = SyncJobStatus.SUCCESS
    finished.end_time = utcnow()
    db_session.add(finished)
    db_session.add(_pending_job(SyncType.EMPLOYEES))
    db_session.flush()

    job, created = enqueue_sync_job(db_session, SyncType.DEPARTMENTS)

    assert created is True
    assert job.type == SyncType.DEPARTMENTS
    assert job.status == SyncJobStatus.PENDING


def test_concurrent_enqueue_recovers_existing_job(
    db_session: Session, monkeypatch
) -> None:
    """并发穿透时回查返回原任务：语义与常规幂等路径一致（created=False）。

    构造方式：让第一次 `_active_job` 返回 None（模拟「检查那一刻对方还没提交」），
    随后 INSERT 撞上唯一索引，兜底分支回查应拿到已存在的那条。
    """
    first, created_first = enqueue_sync_job(db_session, SyncType.WEEKLY_REPORTS)
    assert created_first is True
    db_session.commit()

    real_active_job = sync_scheduler._active_job
    calls = {"count": 0}

    def miss_once(session: Session, sync_type: str) -> SyncJob | None:
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return real_active_job(session, sync_type)

    monkeypatch.setattr(sync_scheduler, "_active_job", miss_once)

    second, created_second = enqueue_sync_job(db_session, SyncType.WEEKLY_REPORTS)

    assert calls["count"] == 2, "冲突后必须回查一次"
    assert created_second is False
    assert second.id == first.id
    assert _count(db_session, SyncJob) == 1


def test_conflicting_enqueue_keeps_sibling_jobs(
    db_session: Session, monkeypatch
) -> None:
    """一条入队并发冲突，不能连带回滚同事务里其他已入队任务。

    守护实现细节：冲突兜底必须用 SAVEPOINT 隔离。若图省事写 `session.rollback()`，
    employees / weekly_reports 两条会一起消失——页面显示「已排队」，
    实际只有一个任务在跑，而且没有任何报错。
    """
    existing, _ = enqueue_sync_job(db_session, SyncType.DEPARTMENTS)
    db_session.commit()

    real_active_job = sync_scheduler._active_job
    calls = {"count": 0}

    def miss_departments_once(session: Session, sync_type: str) -> SyncJob | None:
        if sync_type == SyncType.DEPARTMENTS:
            calls["count"] += 1
            if calls["count"] == 1:
                return None
        return real_active_job(session, sync_type)

    monkeypatch.setattr(sync_scheduler, "_active_job", miss_departments_once)

    entries = enqueue_manual_sync(db_session)
    by_type = {item["type"]: item for item in entries}

    assert by_type[SyncType.DEPARTMENTS]["created"] is False
    assert by_type[SyncType.DEPARTMENTS]["id"] == existing.id
    assert by_type[SyncType.EMPLOYEES]["created"] is True
    assert by_type[SyncType.WEEKLY_REPORTS]["created"] is True
    assert _pending_types(db_session) == [
        SyncType.DEPARTMENTS,
        SyncType.EMPLOYEES,
        SyncType.WEEKLY_REPORTS,
    ]


def test_pending_jobs_execute_all_phases(db_session: Session) -> None:
    report_time = int(utcnow().timestamp())
    client = FakeWeComClient(
        departments=[_dept(1, "总部"), _dept(2, "信息部", 1)],
        employees=[_user("u1", "张三", 2)],
        reports={
            "j1": _journal_info(
                report_time=report_time,
                userid="u1",
                apply_data=text_apply_data("本周优化同步链路"),
            )
        },
    )
    enqueue_manual_sync(db_session)

    results = run_pending_sync_jobs(db_session, limit=3, client=client)

    assert [item["type"] for item in results] == [
        SyncType.DEPARTMENTS,
        SyncType.EMPLOYEES,
        SyncType.WEEKLY_REPORTS,
    ]
    assert all(item["status"] == SyncJobStatus.SUCCESS for item in results)
    assert db_session.scalar(select(WeeklyReport).where(WeeklyReport.external_id == "j1")) is not None


def test_auto_scheduler_is_disabled_by_default(db_session: Session) -> None:
    assert get_settings().sync_auto_enabled is False
    assert maybe_enqueue_scheduled_sync(db_session) == []
    assert _pending_types(db_session) == []


def test_auto_scheduler_enqueues_once_and_uses_overlap_window(
    db_session: Session, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "sync_auto_enabled", True)
    monkeypatch.setattr(settings, "sync_overlap_days", 3)
    monkeypatch.setattr(settings, "sync_org_interval_hours", 24)
    monkeypatch.setattr(settings, "sync_reports_weekday", 4)
    monkeypatch.setattr(settings, "sync_reports_hour", 19)
    monkeypatch.setattr(settings, "sync_reports_minute", 0)
    # UTC 11:00 = 北京时间周五 19:00。
    now = datetime(2026, 9, 18, 11, 0, 0)
    previous_end = now - timedelta(hours=2)
    db_session.add(
        SyncJob(
            source="wecom",
            type=SyncType.WEEKLY_REPORTS,
            status=SyncJobStatus.SUCCESS,
            start_time=previous_end - timedelta(days=3),
            end_time=previous_end,
            success_count=1,
            failed_count=0,
            skipped_count=0,
            params={},
            triggered_by="auto",
        )
    )
    db_session.commit()

    first = maybe_enqueue_scheduled_sync(db_session, now=now)
    second = maybe_enqueue_scheduled_sync(db_session, now=now)

    assert {item["type"] for item in first} == {
        SyncType.DEPARTMENTS,
        SyncType.EMPLOYEES,
        SyncType.WEEKLY_REPORTS,
    }
    assert second == []
    report_job = db_session.scalar(
        select(SyncJob)
        .where(
            SyncJob.type == SyncType.WEEKLY_REPORTS,
            SyncJob.status == SyncJobStatus.PENDING,
        )
        .order_by(SyncJob.id.desc())
    )
    assert report_job is not None
    assert report_job.params["start"] == (previous_end - timedelta(days=3)).isoformat()
    assert report_job.params["end"] == now.isoformat()
    assert report_job.params["mode"] == REPORT_SYNC_CURRENT_PERIOD
    assert report_job.params["scheduled_for"] == now.isoformat()


def test_report_schedule_waits_until_configured_time(
    db_session: Session, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "sync_auto_enabled", True)
    monkeypatch.setattr(settings, "sync_reports_weekday", 4)
    monkeypatch.setattr(settings, "sync_reports_hour", 19)
    monkeypatch.setattr(settings, "sync_reports_minute", 0)
    # UTC 10:00 = 北京时间周五 18:00，尚未到 19:00。
    now = datetime(2026, 9, 18, 10, 0, 0)
    previous_end = now - timedelta(days=1)
    db_session.add(
        SyncJob(
            source="wecom",
            type=SyncType.WEEKLY_REPORTS,
            status=SyncJobStatus.SUCCESS,
            start_time=previous_end - timedelta(minutes=5),
            end_time=previous_end,
            success_count=1,
            failed_count=0,
            skipped_count=0,
            params={},
            triggered_by="auto",
        )
    )
    db_session.commit()

    created = maybe_enqueue_scheduled_sync(db_session, now=now)

    assert SyncType.WEEKLY_REPORTS not in {item["type"] for item in created}


def test_report_schedule_catches_up_once_after_worker_downtime(
    db_session: Session, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "sync_auto_enabled", True)
    monkeypatch.setattr(settings, "sync_reports_weekday", 4)
    monkeypatch.setattr(settings, "sync_reports_hour", 19)
    monkeypatch.setattr(settings, "sync_reports_minute", 0)
    # Worker 周一才恢复，上次成功还是上周五之前，应补跑一次。
    now = datetime(2026, 9, 21, 2, 0, 0)
    previous_end = datetime(2026, 9, 11, 10, 0, 0)
    db_session.add(
        SyncJob(
            source="wecom",
            type=SyncType.WEEKLY_REPORTS,
            status=SyncJobStatus.SUCCESS,
            start_time=previous_end - timedelta(minutes=5),
            end_time=previous_end,
            success_count=1,
            failed_count=0,
            skipped_count=0,
            params={},
            triggered_by="auto",
        )
    )
    db_session.commit()

    first = maybe_enqueue_scheduled_sync(db_session, now=now)
    second = maybe_enqueue_scheduled_sync(db_session, now=now)

    assert SyncType.WEEKLY_REPORTS in {item["type"] for item in first}
    assert second == []


def test_manual_previous_period_success_does_not_suppress_current_schedule(
    db_session: Session, monkeypatch
) -> None:
    """手动补抓上周成功后，仍要按计划补抓本周。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "sync_auto_enabled", True)
    monkeypatch.setattr(settings, "sync_reports_weekday", 4)
    monkeypatch.setattr(settings, "sync_reports_hour", 19)
    monkeypatch.setattr(settings, "sync_reports_minute", 0)
    now = datetime(2026, 9, 19, 2, 0, 0)  # 北京时间周六 10:00
    db_session.add(
        SyncJob(
            source="wecom",
            type=SyncType.WEEKLY_REPORTS,
            status=SyncJobStatus.SUCCESS,
            start_time=now - timedelta(hours=1),
            end_time=now - timedelta(hours=1),
            success_count=25,
            failed_count=0,
            skipped_count=0,
            params={"mode": REPORT_SYNC_LAST_PERIOD},
            triggered_by="manual",
        )
    )
    db_session.commit()

    created = maybe_enqueue_scheduled_sync(db_session, now=now)

    assert SyncType.WEEKLY_REPORTS in {item["type"] for item in created}
    report_job = db_session.scalar(
        select(SyncJob)
        .where(
            SyncJob.type == SyncType.WEEKLY_REPORTS,
            SyncJob.status == SyncJobStatus.PENDING,
        )
        .order_by(SyncJob.id.desc())
    )
    assert report_job is not None
    assert report_job.params["mode"] == REPORT_SYNC_CURRENT_PERIOD


def test_sync_status_reports_failure_and_next_run(
    db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "sync_auto_enabled", True)
    failed = SyncJob(
        source="wecom",
        type=SyncType.WEEKLY_REPORTS,
        status=SyncJobStatus.FAILED,
        start_time=datetime(2026, 9, 14, 8, 0, 0),
        end_time=datetime(2026, 9, 14, 8, 1, 0),
        success_count=0,
        failed_count=1,
        skipped_count=0,
        error=[{"external_id": "j1", "code": "60020", "message": "可信 IP 不在白名单"}],
        params={},
        triggered_by="auto",
    )
    db_session.add(failed)
    db_session.commit()

    result = sync_status(db_session)

    assert result["last_failure_at"] == failed.end_time.isoformat()
    assert result["last_error"] == "[60020] 可信 IP 不在白名单"
    assert result["reports"]["next_run_at"] is not None


def _failed_job(
    sync_type: str, end_time: datetime, error: list[dict] | None = None
) -> SyncJob:
    return SyncJob(
        source="wecom",
        type=sync_type,
        status=SyncJobStatus.FAILED,
        start_time=end_time - timedelta(minutes=1),
        end_time=end_time,
        success_count=0,
        failed_count=1,
        skipped_count=0,
        error=error
        or [{"external_id": None, "code": "batch_error", "message": "boom"}],
        params={},
        triggered_by="auto",
    )


def _success_job(sync_type: str, end_time: datetime) -> SyncJob:
    return SyncJob(
        source="wecom",
        type=sync_type,
        status=SyncJobStatus.SUCCESS,
        start_time=end_time - timedelta(minutes=1),
        end_time=end_time,
        success_count=1,
        failed_count=0,
        skipped_count=0,
        error=[],
        params={},
        triggered_by="auto",
    )


_IP_BLOCKED_ERROR = [
    {
        "external_id": None,
        "code": "batch_error",
        "message": (
            "ConnectorPermissionError: [60020] not allow to access from your ip, "
            "hint: [example-request-id], from ip: 203.0.113.10, "
            "more info at https://open.work.weixin.qq.com/devtool/query?e=60020"
        ),
    }
]


def test_stale_failure_cleared_after_later_success(db_session: Session) -> None:
    """可信 IP 修好后，旧红字必须消失：之后成功过就不再上报该失败。"""
    db_session.add(
        _failed_job(
            SyncType.WEEKLY_REPORTS,
            datetime(2026, 9, 15, 7, 39, 58),
            error=_IP_BLOCKED_ERROR,
        )
    )
    db_session.commit()
    assert sync_status(db_session)["last_error"] is not None

    db_session.add(
        _success_job(SyncType.WEEKLY_REPORTS, datetime(2026, 9, 15, 9, 0, 0))
    )
    db_session.commit()

    result = sync_status(db_session)
    assert result["last_error"] is None
    assert result["last_error_detail"] is None
    assert result["last_error_resolved"] is True
    # 历史失败时间戳保留，便于运维回溯，但不再当作「当前故障」。
    assert result["last_failure_at"] == "2026-09-15T07:39:58"
    assert result["reports"]["last_error_resolved"] is True
    assert result["reports"]["last_error_detail"] is None


def test_unresolved_failure_still_reported(db_session: Session) -> None:
    """失败之后没有再成功过，仍要上报，且只对未恢复的同步类型报。"""
    db_session.add(
        _success_job(SyncType.DEPARTMENTS, datetime(2026, 9, 14, 2, 56, 40))
    )
    db_session.add(
        _failed_job(
            SyncType.WEEKLY_REPORTS,
            datetime(2026, 9, 15, 7, 39, 58),
            error=_IP_BLOCKED_ERROR,
        )
    )
    db_session.commit()

    result = sync_status(db_session)
    assert result["last_error_resolved"] is False
    assert result["departments"]["last_error_detail"] is None
    assert result["reports"]["last_error"] is not None
    detail = result["last_error_detail"]
    assert detail is not None
    assert detail["vendor_code"] == "60020"
    assert detail["kind"] == "permission"
    assert detail["title"] == "企业微信拒绝了当前服务器的出口 IP"
    assert "203.0.113.10" in detail["summary"]
    assert "企业可信IP" in (detail["hint"] or "")


def test_classify_sync_error_strips_wecom_noise() -> None:
    """摘要把 hint 链接等噪声去掉，原始报文只在展开时看。"""
    detail = classify_sync_error(_failed_job(SyncType.EMPLOYEES, utcnow(), error=_IP_BLOCKED_ERROR))
    assert detail is not None
    assert "devtool/query" not in detail["summary"]
    assert "hint" not in detail["summary"]
    assert "devtool/query" in detail["raw"]

    auth = classify_sync_error(
        _failed_job(
            SyncType.DEPARTMENTS,
            utcnow(),
            error=[
                {
                    "code": "batch_error",
                    "message": "ConnectorAuthError: 缺少企业微信凭据：请设置 WECOM_CORPID",
                }
            ],
        )
    )
    assert auth is not None
    assert auth["kind"] == "auth"
    assert "系统设置" in (auth["hint"] or "")
    assert "ConnectorAuthError" not in auth["summary"]

    assert classify_sync_error(None) is None


def test_classify_sync_error_works_without_exception_class_name() -> None:
    """探针抛出的报文没有 Python 类名，必须能只靠企微错误码判出分类。"""
    detail = classify_sync_error_text(
        "[60020] not allow to access from your ip, hint: [1789], "
        "from ip: 203.0.113.10, more info at https://x?e=60020"
        "。处理：把上面 from ip 显示的公网出口 IP 添加到管理后台。"
    )
    assert detail["kind"] == "permission"
    assert detail["vendor_code"] == "60020"
    assert detail["title"] == "企业微信拒绝了当前服务器的出口 IP"
    assert detail["egress_ip"] == "203.0.113.10"
    # 摘要不能再出现重复错误码或连接器追加的处理段。
    assert detail["summary"] == "出口 IP 203.0.113.10 不在企业可信 IP 名单内。"
    assert detail["summary"].count("[60020]") == 0
    assert "处理：" not in detail["summary"]
    assert "企业可信IP" in (detail["hint"] or "")

    forbidden = classify_sync_error_text("[301065] api forbidden, hint: [abc]")
    assert forbidden["kind"] == "permission"

    invalid_params = classify_sync_error_text("[301066] param error")
    assert invalid_params["kind"] == "request"
    assert invalid_params["hint"] is not None
    assert forbidden["title"] == "自建应用未加入「汇报 - 可调用接口的应用」"


class _StubProbeClient:
    """只实现探针用到的 health_check，避免测试真的打企微。"""

    def __init__(self, result: dict) -> None:
        self._result = result

    def health_check(self) -> dict:
        return self._result


def test_probe_endpoint_reports_egress_ip_on_ip_block(db_session: Session) -> None:
    """探针要明确回显「企微看到的出口 IP」，用户才能核对白名单加对了没有。"""
    app = create_app()

    def _override_db():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[deps.get_wecom_client] = lambda: _StubProbeClient(
        {
            "source": "wecom",
            "ok": False,
            "steps": [
                {"step": "get_access_token", "ok": True, "detail": "token=abcd1234..."},
                {
                    "step": "department/list",
                    "ok": False,
                    "error": (
                        "ConnectorPermissionError: [60020] not allow to access from "
                        "your ip, hint: [1789], from ip: 203.0.113.10, "
                        "more info at https://open.work.weixin.qq.com/devtool/query"
                    ),
                    "error_type": "ConnectorPermissionError",
                },
            ],
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post("/api/sync/wecom/probe")
            assert response.status_code == 200
            body = response.json()
    finally:
        app.dependency_overrides.clear()

    assert body["ok"] is False
    assert body["egress_ip"] == "203.0.113.10"
    assert body["error"]["vendor_code"] == "60020"
    assert "203.0.113.10" in body["error"]["summary"]
    assert body["checked_at"] is not None


def test_probe_endpoint_ok_when_healthy(db_session: Session) -> None:
    app = create_app()

    def _override_db():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override_db
    app.dependency_overrides[deps.get_wecom_client] = lambda: _StubProbeClient(
        {"source": "wecom", "ok": True, "steps": [{"step": "get_access_token", "ok": True}]}
    )
    try:
        with TestClient(app) as client:
            body = client.post("/api/sync/wecom/probe").json()
    finally:
        app.dependency_overrides.clear()

    assert body["ok"] is True
    assert body["egress_ip"] is None
    assert body["error"] is None


def test_report_queue_api_creates_and_dedupes(
    db_session: Session,
) -> None:
    """接口层：首次 created=true，重复点击 created=false 且窗口说明可读。"""
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            first = client.post(
                "/api/sync/wecom/reports/queue",
                json={"mode": REPORT_SYNC_LAST_PERIOD},
            )
            assert first.status_code == 200
            body = first.json()
            assert body["created"] is True
            assert body["window"] == "上一完整周期"
            assert body["job"]["type"] == SyncType.WEEKLY_REPORTS

            second = client.post(
                "/api/sync/wecom/reports/queue",
                json={"mode": REPORT_SYNC_LAST_PERIOD},
            )
            assert second.status_code == 200
            second_body = second.json()
            assert second_body["created"] is False
            assert second_body["job"]["id"] == body["job"]["id"]

            history = client.post(
                "/api/sync/wecom/reports/queue",
                json={"mode": REPORT_SYNC_HISTORY, "days": 45},
            )
            # 已有排队任务，历史模式同样被去重（不叠加第二条抓取）。
            assert history.json()["created"] is False
    finally:
        app.dependency_overrides.clear()

    assert _pending_types(db_session) == [SyncType.WEEKLY_REPORTS]


def test_report_queue_api_rejects_unknown_mode(db_session: Session) -> None:
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/sync/wecom/reports/queue",
                json={"mode": "yesterday"},
            )
            assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_queued_report_sync_executes_with_period_params(
    db_session: Session,
) -> None:
    """排队任务能被 Worker 认领执行；start/end 来自入队参数，不依赖 days。"""
    report_time = int((utcnow() - timedelta(days=7)).timestamp())
    client = FakeWeComClient(
        departments=[_dept(1, "总部")],
        employees=[_user("u1", "张三", 1)],
        reports={
            "j1": _journal_info(
                report_time=report_time,
                userid="u1",
                apply_data=text_apply_data("上周完成同步幂等验证"),
            )
        },
    )
    job, created = enqueue_report_sync(
        db_session, mode=REPORT_SYNC_LAST_PERIOD
    )
    assert created is True
    # 窗口覆盖一周前，假 Connector 只按区间返回，因此必能命中 j1。
    assert "days" not in job.params

    results = run_pending_sync_jobs(db_session, limit=1, client=client)

    assert [item["type"] for item in results] == [SyncType.WEEKLY_REPORTS]
    assert results[0]["status"] == SyncJobStatus.SUCCESS
    assert (
        db_session.scalar(
            select(WeeklyReport).where(WeeklyReport.external_id == "j1")
        )
        is not None
    )

    app = create_app()
    app.dependency_overrides[deps.get_db] = lambda: db_session
    try:
        with TestClient(app) as api:
            detail = api.get(f"/api/sync/jobs/{job.id}")
            listing = api.get("/api/sync/jobs?type=weekly_reports&limit=3")
    finally:
        app.dependency_overrides.clear()
    assert detail.status_code == 200
    assert detail.json()["created_weekly_count"] == 1
    assert detail.json()["window"] == "上一完整周期"
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [job.id]


def test_repeated_last_period_sync_never_duplicates_data(
    db_session: Session,
) -> None:
    """端到端复现用户诉求：连点「同步上周周报」不会重复入库、不会重复聊模型。

    覆盖完整链路：入队 -> Worker 认领执行 -> 再次入队 -> 再次执行。
    第二次必须整条链路都被拦住：入队被去重、RawRecord 未变、WeeklyReport 不新增、
    AI 分析队列不追加条目。
    """
    # 数据放在运行时间的「上一个完整周期」内，避免日期剃移后落到当前周而让首次同步无数据。
    previous_start, _ = previous_period_time_window(db_session)
    report_time = int(
        (previous_start + timedelta(hours=1))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    client = FakeWeComClient(
        departments=[_dept(1, "总部")],
        employees=[_user("u1", "张三", 1)],
        reports={
            "j1": _journal_info(
                report_time=report_time,
                userid="u1",
                apply_data=text_apply_data("本周完成同步幂等验证"),
            )
        },
    )
    sync_departments(db_session, client)
    sync_employees(db_session, client)

    # 该用例验证同步幂等（含自动入队只建一条队列位），显式打开自动分析。
    from app.services.runtime_config_service import update_runtime_values

    update_runtime_values(db_session, {"ai_auto_enqueue_on_sync": "true"})
    db_session.commit()

    # 第一次：真正跑通一次抓取。
    job1, created1 = enqueue_report_sync(db_session, mode=REPORT_SYNC_LAST_PERIOD)
    assert created1 is True
    first_run = run_pending_sync_jobs(db_session, limit=1, client=client)
    assert first_run[0]["success_count"] == 1

    reports_after_first = _count(db_session, WeeklyReport)
    raws_after_first = _count(db_session, RawRecord)
    queued_after_first = _count(db_session, AnalysisQueue)
    assert reports_after_first == 1
    assert queued_after_first == 1

    # 模拟 AI 已经算出结论并回写到冗余字段。
    report = db_session.scalar(
        select(WeeklyReport).where(WeeklyReport.external_id == "j1")
    )
    assert report is not None
    report.ai_quality_flag = False
    report.ai_duplicate_flag = False
    report.ai_duplicate_score = 3
    report.ai_checked_at = utcnow()
    db_session.commit()
    db_session.refresh(job1)
    assert job1.status == SyncJobStatus.SUCCESS

    # 第二次点击：重新入队并执行。
    job2, created2 = enqueue_report_sync(db_session, mode=REPORT_SYNC_LAST_PERIOD)
    assert created2 is True
    assert job2.id != job1.id
    second_run = run_pending_sync_jobs(db_session, limit=1, client=client)

    # 内容未变 -> 全部走 skipped 分支，不产生任何新数据。
    assert second_run[0]["status"] == SyncJobStatus.SUCCESS
    assert second_run[0]["success_count"] == 0
    assert second_run[0]["skipped_count"] == 1
    assert _count(db_session, WeeklyReport) == reports_after_first
    assert _count(db_session, RawRecord) == raws_after_first
    assert _count(db_session, AnalysisQueue) == queued_after_first

    # 已算出的 AI 结论不能被重复同步清空（页面会误报「未分析」）。
    db_session.refresh(report)
    assert report.ai_quality_flag is False
    assert report.ai_duplicate_flag is False
    assert report.ai_duplicate_score == 3
    assert report.ai_checked_at is not None


def test_report_schedule_api_updates_runtime_settings(db_session: Session) -> None:
    app = create_app()

    def _override():
        yield db_session

    app.dependency_overrides[deps.get_db] = _override
    try:
        with TestClient(app) as client:
            response = client.patch(
                "/api/sync/schedule",
                json={
                    "auto_enabled": True,
                    "auto_period_summary_enabled": False,
                    "weekday": 2,
                    "hour": 18,
                    "minute": 35,
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["report_schedule"]["auto_enabled"] is True
            assert (
                body["report_schedule"]["auto_period_summary_enabled"] is False
            )
            assert body["report_schedule"]["weekday"] == 2
            assert body["report_schedule"]["hour"] == 18
            assert body["report_schedule"]["minute"] == 35
    finally:
        app.dependency_overrides.clear()

    saved = runtime_settings(db_session)
    assert saved.sync_auto_enabled is True
    assert saved.sync_auto_period_summary_enabled is False
    assert saved.sync_reports_weekday == 2
    assert saved.sync_reports_hour == 18
    assert saved.sync_reports_minute == 35


# --------------------------------------------------------------------------- #
# 中断任务回收：进程被杀会让任务永远停在 RUNNING，把同类型同步永久挡死
# --------------------------------------------------------------------------- #


def test_recover_interrupted_jobs_unblocks_weekly_sync(db_session: Session) -> None:
    """僵尸 RUNNING 任务被回收后，同类型同步必须能重新入队。

    这是真实故障的回归守护：`enqueue_sync_job` 把「同类型已有 RUNNING」当作活跃
    任务直接返回，所以只要有一行永远停在 RUNNING，之后每一次手动点击与定时调度
    都会被去重掉，周报同步永久停止，且 sync_status 一直显示 active=true。
    """
    zombie = SyncJob(
        source="wecom",
        type=SyncType.WEEKLY_REPORTS,
        status=SyncJobStatus.RUNNING,
        start_time=utcnow() - timedelta(hours=5),
        triggered_by="auto",
        created_at=utcnow() - timedelta(hours=5),
    )
    db_session.add(zombie)
    db_session.commit()

    # 回收前：入队被僵尸挡掉，拿回的是同一个任务
    blocked, created = enqueue_report_sync(db_session, mode=REPORT_SYNC_LAST_PERIOD)
    assert created is False
    assert blocked.id == zombie.id

    recovered = recover_interrupted_jobs(db_session)
    assert recovered["sync_jobs"] == 1

    db_session.refresh(zombie)
    assert zombie.status == SyncJobStatus.FAILED
    assert zombie.end_time is not None
    assert zombie.error and zombie.error[0]["code"] == "interrupted"

    # 回收后：可以正常入队新任务
    job, created = enqueue_report_sync(db_session, mode=REPORT_SYNC_LAST_PERIOD)
    assert created is True
    assert job.id != zombie.id


def test_recover_running_queue_items_are_requeued(db_session: Session) -> None:
    """分析类队列项按篇幂等，卡在 RUNNING 应放回 PENDING 重跑，而不是标记失败。"""
    report = WeeklyReport(
        source="wecom",
        external_id="r-1",
        report_type="weekly",
        title="周报",
        content="正文",
        submitted_at=utcnow(),
    )
    db_session.add(report)
    db_session.commit()

    pending = AnalysisQueue(
        report_id=report.id,
        payload_hash="h",
        status=AnalysisQueueStatus.RUNNING,
    )
    db_session.add(pending)
    db_session.commit()

    recovered = recover_interrupted_jobs(db_session)
    assert recovered["analysis_queue"] == 1
    db_session.refresh(pending)
    assert pending.status == AnalysisQueueStatus.PENDING
    assert pending.next_attempt_at is None


def test_recover_with_lease_only_touches_stale_jobs(db_session: Session) -> None:
    """带租约的运行期回收不能误伤刚开始跑的任务。"""
    fresh = SyncJob(
        source="wecom",
        type=SyncType.WEEKLY_REPORTS,
        status=SyncJobStatus.RUNNING,
        start_time=utcnow(),
        triggered_by="api:queued",
        created_at=utcnow(),
    )
    stale = SyncJob(
        source="wecom",
        type=SyncType.EMPLOYEES,
        status=SyncJobStatus.RUNNING,
        start_time=utcnow() - timedelta(minutes=200),
        triggered_by="auto",
        created_at=utcnow() - timedelta(minutes=200),
    )
    db_session.add_all([fresh, stale])
    db_session.commit()

    recovered = recover_interrupted_jobs(db_session, max_age_minutes=90)
    assert recovered["sync_jobs"] == 1

    db_session.refresh(fresh)
    db_session.refresh(stale)
    assert fresh.status == SyncJobStatus.RUNNING, "刚开跑的任务不应被回收"
    assert stale.status == SyncJobStatus.FAILED


def test_recover_is_noop_when_nothing_running(db_session: Session) -> None:
    """没有僵尸任务时回收是空操作。

    期望值从 `REQUEUE_ON_INTERRUPT` 推导而不是写死队列名：写死的话每新增一个
    队列都要回来改这条与本意无关的断言。该表本身的完整性由
    `test_recover_covers_every_queue_model` 守护。
    """
    assert recover_interrupted_jobs(db_session) == {
        "sync_jobs": 0,
        **{key: 0 for key, _ in sync_scheduler.REQUEUE_ON_INTERRUPT},
    }


def test_recover_covers_every_queue_model(db_session: Session) -> None:
    """**结构性守护**：app.models 里每个 *Queue 模型都必须登记进回收表。

    这条测试存在的理由是一次真实故障：卡点总结队列 `BlockerSummaryQueue` 上线时
    没有加进 `recover_interrupted_jobs`，于是 Worker 被杀后那一行永远停在 RUNNING，
    处理中心一直显示「AI 正在汇总」，而面板上的「重新生成」按钮在 RUNNING 态下
    不会重置（只救 FAILED/PENDING/stale）—— 用户没有任何自救路径。

    这类缺陷靠「记得改两处」防不住（上一版正是手写三条语句，漏了一条），所以这里
    改成从 SQLAlchemy 的 mapper 注册表反查：新增队列模型忘了登记，本测试就会红。
    """
    from app.db.base import Base

    all_queue_models = {
        mapper.class_.__name__
        for mapper in Base.registry.mappers
        if mapper.class_.__name__.endswith("Queue")
    }
    registered = {model.__name__ for _, model in sync_scheduler.REQUEUE_ON_INTERRUPT}

    assert all_queue_models, "没有发现任何队列模型，说明注册表枚举方式失效了"
    assert all_queue_models == registered, (
        "有队列模型没有被 recover_interrupted_jobs 回收，"
        "进程被杀后该队列会永久卡在 RUNNING："
        f"未登记={sorted(all_queue_models - registered)}，"
        f"多余登记={sorted(registered - all_queue_models)}"
    )


def test_recover_requeues_blocker_summary_queue(db_session: Session) -> None:
    """卡点总结卡在 RUNNING 必须被放回 PENDING（回归：曾整类漏回收）。"""
    zombie = BlockerSummaryQueue(
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        payload_hash="h",
        blocker_count=3,
        status=AnalysisQueueStatus.RUNNING,
        updated_at=utcnow() - timedelta(hours=5),
    )
    db_session.add(zombie)
    db_session.commit()

    recovered = recover_interrupted_jobs(db_session)
    assert recovered["blocker_summary_queue"] == 1

    db_session.refresh(zombie)
    assert zombie.status == AnalysisQueueStatus.PENDING
    assert zombie.next_attempt_at is None, "回收后必须立即可被领取，不能带退避时间"


def test_recover_lease_does_not_touch_fresh_blocker_summary(db_session: Session) -> None:
    """带租约的运行期回收不能误伤刚开始跑的卡点总结。"""
    fresh = BlockerSummaryQueue(
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 13),
        payload_hash="h",
        blocker_count=3,
        status=AnalysisQueueStatus.RUNNING,
        updated_at=utcnow(),
    )
    db_session.add(fresh)
    db_session.commit()

    recovered = recover_interrupted_jobs(db_session, max_age_minutes=90)
    assert recovered["blocker_summary_queue"] == 0

    db_session.refresh(fresh)
    assert fresh.status == AnalysisQueueStatus.RUNNING
