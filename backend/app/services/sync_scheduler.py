"""企业微信同步调度：排队、自动触发、状态汇总。

设计边界：
  - 同步本身仍由 department_sync / employee_sync / weekly_report_sync 执行；
    本模块只负责决定“什么时候跑”和“由谁跑”，不复制业务逻辑。
  - 手动点击和后台定时都先落一条 PENDING SyncJob，Worker 再用 CAS 认领，
    避免长请求占用网页线程，也避免多进程重复拉取。
  - 自动同步默认关闭（sync_auto_enabled=false），Linux 服务器按需开启。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.time_utils import custom_week_range, utcnow
from app.models.analysis_queue import AnalysisQueue
from app.models.blocker_summary_queue import BlockerSummaryQueue
from app.models.enums import (
    AnalysisQueueStatus,
    DataSource,
    SyncJobStatus,
    SyncType,
)
from app.models.operating_summary_queue import OperatingSummaryQueue
from app.models.period_summary_queue import PeriodSummaryQueue
from app.models.sync_job import SyncJob
from app.services.runtime_config_service import runtime_settings

logger = get_logger(__name__)

AUTO_TRIGGER = "auto"
MANUAL_TRIGGER = "api:queued"

# 进程崩溃后需要「放回 PENDING 重跑」的队列表。**新增队列必须登记在这里。**
#
# 漏登记一个的后果不是「少回收一次」，而是那一类任务被永久钉在 RUNNING：
# Worker 消费时先 CAS 认领成 RUNNING，进程被杀就没有任何代码把它写回来，
# 而界面上的「重新生成」按钮在 RUNNING 态下不会重置（只能救 FAILED/PENDING/stale）。
# 卡点总结队列 `BlockerSummaryQueue` 就曾因为这里只列了两张表而整类失效。
# tests/test_sync_scheduler.py 有一条结构性守护会遍历 app.models 里所有 *Queue
# 模型，漏登记即测试失败 —— 所以这张表不需要靠人记得维护。
REQUEUE_ON_INTERRUPT: tuple[tuple[str, Any], ...] = (
    ("analysis_queue", AnalysisQueue),
    ("period_summary_queue", PeriodSummaryQueue),
    ("blocker_summary_queue", BlockerSummaryQueue),
    ("operating_summary_queue", OperatingSummaryQueue),
)

# 周报同步入队模式：上一完整周期 / 近 N 天历史 / 全部历史 / 当前周期（本周概览用）。
REPORT_SYNC_LAST_PERIOD = "last_period"
REPORT_SYNC_HISTORY = "history"
REPORT_SYNC_ALL_HISTORY = "all_history"
# 当前周期：抓本周期开始到现在的周报。用于「本周概览」——先抓到最新，
# 再由前端接着请求同周期的 AI 总结，所以这里只负责窗口，不生成总结。
REPORT_SYNC_CURRENT_PERIOD = "current_period"
REPORT_SYNC_MODES = (
    REPORT_SYNC_LAST_PERIOD,
    REPORT_SYNC_HISTORY,
    REPORT_SYNC_ALL_HISTORY,
    REPORT_SYNC_CURRENT_PERIOD,
)

# 企微汇报列表接口必须传 starttime/endtime，无法表达“从最早一条开始”。
# Unix epoch 附近是无业务假设的技术下界；但企微拒绝 starttime=0，
# 因此从 epoch 后 1 秒开始。Connector 会按自然月切分，不会把超长时间窗
# 直接传给企微。全历史抓取是后台一次性动作，空月份只产生列表请求。
ALL_HISTORY_START = datetime(1970, 1, 1, 0, 0, 1)

_ACTIVE_STATUSES = (SyncJobStatus.PENDING, SyncJobStatus.RUNNING)
_FINISHED_SUCCESS_STATUSES = (SyncJobStatus.SUCCESS, SyncJobStatus.PARTIAL)
_FINISHED_PROBLEM_STATUSES = (SyncJobStatus.PARTIAL, SyncJobStatus.FAILED)
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _first_error(job: SyncJob | None) -> str | None:
    if job is None or not job.error:
        return None
    first = job.error[0] if isinstance(job.error, list) and job.error else None
    if isinstance(first, dict):
        message = first.get("message")
        code = first.get("code")
        if code and message:
            return f"[{code}] {message}"
        return message or str(first)
    return str(first)


# 企微错误码 -> 一句话处理建议。命中时前端只显示这条建议，
# 不再把 hint、devtool 链接和中英混排的原始报文糊到页面上。
_SYNC_ERROR_HINTS: dict[str, str] = {
    "60020": (
        "把上面显示的出口 IP 加入管理后台「应用管理 → 该自建应用 → 开发者接口 → "
        "企业可信IP」，必要时同时加入「我的企业 → 企业信息 → 企业可信IP」，"
        "保存后重新检测（注意填的是公网出口 IP，不是服务器内网地址）。"
    ),
    "60011": "该应用没有访问此成员的权限，请检查自建应用的可见范围是否覆盖该部门。",
    "48002": (
        "该自建应用未被授予通讯录接口权限。请在管理后台把应用加入通讯录"
        "「可调用接口的应用」范围，并确认可见范围包含所需部门。"
    ),
    "301065": (
        "自建应用没有调用汇报接口的权限。请在管理后台把该应用加入"
        "「汇报 - 可调用接口的应用」，并确认用的是自建应用 Secret。"
    ),
    "301066": "企微拒绝了汇报查询参数；请升级到已修正历史时间下界的版本后重新同步。",
    "40001": "access_token 无效，通常是企微密钥被重置；请在系统设置里更新 Secret 后重新同步。",
    "40014": "access_token 无效，通常是企微密钥被重置；请在系统设置里更新 Secret 后重新同步。",
    "42001": "access_token 已过期，等下一轮自动同步即可，无需手工处理。",
    "45009": "接口调用超过企微频率限制，稍后会自动重试。",
}

# 连接器异常类名 -> 中文分类，用于给用户一个可读的标题，
# 而不是直接抛出 Python 类名（ConnectorPermissionError 之类）。
_SYNC_ERROR_KINDS: dict[str, tuple[str, str]] = {
    "ConnectorPermissionError": ("permission", "企微接口权限/IP 校验未通过"),
    "ConnectorAuthError": ("auth", "企业微信凭据缺失或失效"),
    "ConnectorRateLimitError": ("rate_limit", "触发企微频率限制"),
    "ConnectorUpstreamError": ("upstream", "企业微信服务异常或网络不通"),
    "ConnectorResponseError": ("response", "企业微信返回了业务错误"),
}

# 企微错误码 -> (分类, 标题)。探针抛出的报文里没有 Python 类名
# （连接器自己把它去掉了），所以还必须能只靠错误码判断分类。
_SYNC_ERROR_CODES: dict[str, tuple[str, str]] = {
    "60020": ("permission", "企业微信拒绝了当前服务器的出口 IP"),
    "48002": ("permission", "该自建应用未被授予通讯录接口权限"),
    "60011": ("permission", "该自建应用没有该成员的访问权限"),
    "301065": ("permission", "自建应用未加入「汇报 - 可调用接口的应用」"),
    "301066": ("request", "企微拒绝了汇报查询参数"),
    "40001": ("auth", "企业微信凭据无效"),
    "40014": ("auth", "企业微信凭据无效"),
    "42001": ("auth", "企业微信凭据已过期"),
    "45009": ("rate_limit", "触发企微频率限制"),
}

_VENDOR_CODE_RE = re.compile(r"\[(\d{4,6})\]")
_IP_RE = re.compile(r"from ip:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})")
_EXCEPTION_PREFIX_RE = re.compile(r"^\s*(?:[A-Za-z_][\w.]*\.)?(\w*Error)\s*[:：]\s*")
_LEADING_CODE_RE = re.compile(r"^\s*\[\d{4,6}\]\s*")
_SUMMARY_LIMIT = 160


def _clean_message(raw: str) -> str:
    """去掉异常类名、错误码前缀和企微 hint/链接噪声，只留看得懂的一句。"""
    text = _EXCEPTION_PREFIX_RE.sub("", raw)
    text = _LEADING_CODE_RE.sub("", text)
    # 连接器会在原文尾部再附一段「处理：...」，与 hint 重复，只保留第一句。
    text = text.split("。处理：")[0].split(" 处理：")[0]
    text = text.split(", hint:")[0].split(" hint:")[0]
    text = text.split("more info at")[0]
    return text.strip(" ,。;；")


def classify_sync_error_text(
    raw: str, *, batch_code: str | None = None, step: str | None = None
) -> dict[str, Any]:
    """把一段企微错误原文整理成前端可直接展示的结构。

    返回 ``summary``（一句话结论）、``hint``（怎么处理）和 ``raw``（原始报文）。
    原文只在用户主动展开时显示：它又长又含 hint 链接，铺满页面会把关键信息挤没。
    """
    vendor_match = _VENDOR_CODE_RE.search(raw)
    vendor_code = vendor_match.group(1) if vendor_match else None
    # 有些错误把业务码放在 code 字段（如测试构造的数据），一并认出来。
    if vendor_code is None and batch_code and batch_code.isdigit():
        vendor_code = batch_code
    code = vendor_code or batch_code

    kind = "unknown"
    title = "同步执行失败"
    for class_name, (kind_value, title_value) in _SYNC_ERROR_KINDS.items():
        if class_name in raw:
            kind = kind_value
            title = title_value
            break
    else:
        # 报文里没有异常类名时（连接器只留了业务码），退回按错误码判断。
        by_code = _SYNC_ERROR_CODES.get(vendor_code or "")
        if by_code:
            kind, title = by_code

    ip_match = _IP_RE.search(raw)
    egress_ip = ip_match.group(1) if ip_match else None
    if kind == "permission" and vendor_code == "60020":
        title = "企业微信拒绝了当前服务器的出口 IP"

    clean = _clean_message(raw)
    if kind == "permission" and vendor_code == "60020" and egress_ip:
        # 报文里的「not allow to access from your ip」对用户没有信息量，
        # 直接换成「哪个 IP 被拒」才能拿去核对白名单。
        summary = f"出口 IP {egress_ip} 不在企业可信 IP 名单内。"
    elif vendor_code:
        summary = f"[{vendor_code}] {clean}" if clean else f"[{vendor_code}] 企微接口报错"
    else:
        summary = clean or title

    hint = _SYNC_ERROR_HINTS.get(vendor_code or "")
    if hint is None and kind == "auth":
        hint = "请在系统设置里补全企业微信 CorpID / Secret，并确认使用的是自建应用。"
    if len(summary) > _SUMMARY_LIMIT:
        summary = f"{summary[: _SUMMARY_LIMIT - 1]}…"

    return {
        "code": code,
        "vendor_code": vendor_code,
        "kind": kind,
        "title": title,
        "summary": summary,
        "hint": hint,
        "raw": raw,
        "egress_ip": egress_ip,
        "step": step,
    }


def classify_sync_error(job: SyncJob | None) -> dict[str, Any] | None:
    """把一条失败任务整理成前端可直接展示的结构（见 classify_sync_error_text）。"""
    if job is None or not job.error:
        return None
    first = job.error[0] if isinstance(job.error, list) and job.error else None
    if isinstance(first, dict):
        raw = str(first.get("message") or first.get("code") or first)
        batch_code = first.get("code")
    else:
        raw = str(first)
        batch_code = None
    detail = classify_sync_error_text(raw, batch_code=batch_code)
    detail.update(
        {
            "type": job.type,
            "job_id": job.id,
            "at": job.end_time.isoformat() if job.end_time else None,
        }
    )
    return detail


def serialize_sync_job(job: SyncJob) -> dict[str, Any]:
    params = job.params or {}
    return {
        "id": job.id,
        "source": job.source,
        "type": job.type,
        "status": job.status,
        "start_time": job.start_time.isoformat() if job.start_time else None,
        "end_time": job.end_time.isoformat() if job.end_time else None,
        "success_count": job.success_count,
        "created_count": params.get("created_count") if job.type == SyncType.WEEKLY_REPORTS else None,
        "created_weekly_count": params.get("created_weekly_count") if job.type == SyncType.WEEKLY_REPORTS else None,
        "failed_count": job.failed_count,
        "skipped_count": job.skipped_count,
        "triggered_by": job.triggered_by,
        "error": job.error,
        "error_message": _first_error(job),
        "window": describe_report_sync_params(params) if job.type == SyncType.WEEKLY_REPORTS else None,
    }


def _active_job(session: Session, sync_type: str) -> SyncJob | None:
    return session.scalars(
        select(SyncJob)
        .where(SyncJob.type == sync_type, SyncJob.status.in_(_ACTIVE_STATUSES))
        .order_by(SyncJob.id.desc())
        .limit(1)
    ).first()


def _latest_job(
    session: Session,
    sync_type: str,
    statuses: tuple[str, ...] | None = None,
    triggered_by_in: tuple[str, ...] | None = None,
) -> SyncJob | None:
    query = select(SyncJob).where(
        SyncJob.type == sync_type, SyncJob.end_time.isnot(None)
    )
    if statuses is not None:
        query = query.where(SyncJob.status.in_(statuses))
    if triggered_by_in is not None:
        query = query.where(SyncJob.triggered_by.in_(triggered_by_in))
    return session.scalars(
        query.order_by(SyncJob.end_time.desc(), SyncJob.id.desc()).limit(1)
    ).first()


def enqueue_sync_job(
    session: Session,
    sync_type: str,
    *,
    params: dict[str, Any] | None = None,
    triggered_by: str = MANUAL_TRIGGER,
) -> tuple[SyncJob, bool]:
    """幂等入队：同类型已有 PENDING/RUNNING 任务时直接返回，不重复排队。

    并发安全是两层，缺一不可：
      ① 先查一次（常规路径，直接返回原任务，不产生异常、不打扰调用方）；
      ② 两个进程同时穿过 ① 时，DB 的 ``uq_sync_jobs_active_type`` 挡下第二条 INSERT，
         此处捕获后回查并返回已存在的那条，语义与 ① 一致（``created=False``）。

    ② 必须用 SAVEPOINT 隔离：``enqueue_manual_sync`` 连续入队三条后统一 commit，
    直接 ``session.rollback()`` 会把同事务里前面已 add 的任务一并丢掉
    （表现为「点了同步但只有一半任务排上队」）。

    如实说明残留边界：能走到唯一约束冲突，说明对方那条**已经提交**
    （若是未提交的冲突写，SQLite 报的是锁等待而非唯一约束），所以回查通常能看到它。
    残留窗口来自快照隔离：事务若在本进程更早的读上取了旧快照，回查仍可能看不到，
    此时原样抛出 ``IntegrityError`` —— 这比放行第二条并行抓取更安全。
    真要彻底消除，需要把入队改成独立事务，属另一项改造。
    （SQLite 现已在连接层开启 WAL，见 ``db/session.py``。）
    """
    active = _active_job(session, sync_type)
    if active is not None:
        return active, False

    now = utcnow()
    job = SyncJob(
        source=DataSource.WECOM,
        type=sync_type,
        status=SyncJobStatus.PENDING,
        # start_time 是排队时间；Worker 认领后会覆盖为真正开跑时间。
        start_time=now,
        params=params or {},
        triggered_by=triggered_by,
        created_at=now,
        success_count=0,
        failed_count=0,
        skipped_count=0,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        logger.warning("并发入队被唯一索引拦下，改为返回已有任务：type=%s", sync_type)
        existing = _active_job(session, sync_type)
        if existing is None:
            raise
        return existing, False
    return job, True


def enqueue_manual_sync(session: Session) -> list[dict[str, Any]]:
    """点击「立即同步」时入队：部门 -> 员工 -> 近 N 天周报。"""
    settings = get_settings()
    entries: list[tuple[SyncJob, bool]] = [
        enqueue_sync_job(session, SyncType.DEPARTMENTS),
        enqueue_sync_job(
            session,
            SyncType.EMPLOYEES,
            params={"root_department_id": 1},
        ),
        enqueue_sync_job(
            session,
            SyncType.WEEKLY_REPORTS,
            params={"days": settings.wecom_sync_lookback_days},
        ),
    ]
    session.commit()
    return [
        {**serialize_sync_job(job), "created": created}
        for job, created in entries
    ]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _report_sync_window(
    session: Session, now: datetime
) -> tuple[datetime, datetime]:
    """计算周报增量窗口：上次结束时间往前重叠，首次使用配置回溯天数。"""
    settings = get_settings()
    last = _latest_job(
        session, SyncType.WEEKLY_REPORTS, _FINISHED_SUCCESS_STATUSES
    )
    if last is not None and last.end_time is not None:
        start = last.end_time - timedelta(days=settings.sync_overlap_days)
    else:
        start = now - timedelta(days=settings.wecom_sync_lookback_days)
    # 防止异常时钟或超长时间未运行导致一次拉取过大。
    earliest = now - timedelta(days=settings.wecom_sync_lookback_days)
    return max(start, earliest), now


def _business_now(now: datetime, offset_hours: int) -> datetime:
    return now + timedelta(hours=offset_hours)


def previous_period_date_range(
    session: Session, now: datetime | None = None
) -> tuple[date, date]:
    """业务时区下「上一个完整汇报周期」的日期范围（含首尾）。

    跟随 `report_period_rule`，与 `time_utils.period_range` 推导周报周期用的是
    同一套规则，避免这里按自然周算、那里按自然月算导致「上周周报」抓错区间。
    """
    settings = runtime_settings(session)
    current = now or utcnow()
    local_day = _business_now(
        current, settings.report_timezone_offset_hours
    ).date()
    static = get_settings()

    if static.report_period_rule == "month":
        month_start = local_day.replace(day=1)
        previous_end = month_start - timedelta(days=1)
        return previous_end.replace(day=1), previous_end

    if static.report_period_rule == "custom_week":
        week_start, _ = custom_week_range(
            local_day, static.report_custom_week_start
        )
    else:
        week_start = local_day - timedelta(days=local_day.weekday())
    return week_start - timedelta(days=7), week_start - timedelta(days=1)


def previous_period_time_window(
    session: Session, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """「上一个完整汇报周期」对应的 UTC naive 起止时刻，用于企微拉取。"""
    settings = runtime_settings(session)
    offset = settings.report_timezone_offset_hours
    start_date, end_date = previous_period_date_range(session, now)
    start = datetime.combine(start_date, time.min) - timedelta(hours=offset)
    end = datetime.combine(end_date, time.max) - timedelta(hours=offset)
    return start, end


def current_period_date_range(
    session: Session, now: datetime | None = None
) -> tuple[date, date]:
    """业务时区下「当前汇报周期」的完整日期范围（含首尾）。

    与 `previous_period_date_range` 同源同规则，只是把窗口移到本期：
    周期末还没到也用完整周期（本周一~周日），否则今天抓出来的总结会写成
    「周一~今天」，等本周后续周报补进来时又要重新生成一份，口径会和首页不一致。
    """
    settings = runtime_settings(session)
    current = now or utcnow()
    local_day = _business_now(
        current, settings.report_timezone_offset_hours
    ).date()
    static = get_settings()

    if static.report_period_rule == "month":
        month_end = local_day.replace(
            day=calendar.monthrange(local_day.year, local_day.month)[1]
        )
        return local_day.replace(day=1), month_end

    if static.report_period_rule == "custom_week":
        week_start, week_end = custom_week_range(
            local_day, static.report_custom_week_start
        )
        return week_start, week_end

    week_start = local_day - timedelta(days=local_day.weekday())
    return week_start, week_start + timedelta(days=6)


def current_period_time_window(
    session: Session, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """当前周期开始 → 现在（UTC naive），用于企微拉取最新周报。"""
    settings = runtime_settings(session)
    offset = settings.report_timezone_offset_hours
    current = now or utcnow()
    start_date, _ = current_period_date_range(session, current)
    start = datetime.combine(start_date, time.min) - timedelta(hours=offset)
    return start, current


def describe_report_sync_params(params: dict[str, Any]) -> str:
    """给前端/日志用的窗口说明，避免只看到一串 ISO 时间。"""
    mode = params.get("mode")
    if mode == REPORT_SYNC_LAST_PERIOD:
        return "上一完整周期"
    if mode == REPORT_SYNC_CURRENT_PERIOD:
        return "当前周期"
    if mode == REPORT_SYNC_ALL_HISTORY:
        return "全部历史周报"
    if mode == REPORT_SYNC_HISTORY:
        return f"近 {params.get('days')} 天"
    return "自定义区间"


def enqueue_report_sync(
    session: Session,
    *,
    mode: str = REPORT_SYNC_LAST_PERIOD,
    days: int | None = None,
) -> tuple[SyncJob, bool]:
    """只排队周报同步（不动部门/员工）。

    幂等由 `enqueue_sync_job` 保证：同类型已有 PENDING/RUNNING 任务时返回原任务，
    created=False，重复点击不会叠出多条抓取。
    """
    if mode not in REPORT_SYNC_MODES:
        raise ValueError(f"不支持的周报同步模式：{mode}")

    static = get_settings()
    if mode == REPORT_SYNC_LAST_PERIOD:
        start, end = previous_period_time_window(session)
        params: dict[str, Any] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "mode": mode,
        }
    elif mode == REPORT_SYNC_CURRENT_PERIOD:
        start, end = current_period_time_window(session)
        params = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "mode": mode,
        }
    elif mode == REPORT_SYNC_ALL_HISTORY:
        end = utcnow()
        params = {
            "start": ALL_HISTORY_START.isoformat(),
            "end": end.isoformat(),
            "mode": mode,
        }
    else:
        window_days = days or static.wecom_sync_lookback_days
        end = utcnow()
        start = end - timedelta(days=window_days)
        params = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": window_days,
            "mode": mode,
        }

    job, created = enqueue_sync_job(
        session,
        SyncType.WEEKLY_REPORTS,
        params=params,
        triggered_by=MANUAL_TRIGGER,
    )
    session.commit()
    return job, created


def _latest_report_schedule_target(
    session: Session, now: datetime
) -> datetime:
    """返回当前时刻之前最近一次计划时刻（UTC naive）。"""
    settings = runtime_settings(session)
    local = _business_now(now, settings.report_timezone_offset_hours)
    days_since = (local.weekday() - settings.sync_reports_weekday) % 7
    target = datetime.combine(
        local.date() - timedelta(days=days_since),
        time(
            hour=settings.sync_reports_hour,
            minute=settings.sync_reports_minute,
        ),
    )
    if local < target:
        target -= timedelta(days=7)
    return target - timedelta(hours=settings.report_timezone_offset_hours)


def _report_schedule_due(
    session: Session, now: datetime
) -> tuple[bool, datetime]:
    """计划点后没有成功的**自动**同步时到期；停机后启动会补跑一次。

    手动「同步上周」不能代表当前周期已同步。旧实现取周报类型的任意成功任务，
    导致周六手动补抓上周后把本周自动补抓标记成已完成，晚交周报会一直漏到下周。
    """
    target = _latest_report_schedule_target(session, now)
    last = _latest_job(
        session,
        SyncType.WEEKLY_REPORTS,
        _FINISHED_SUCCESS_STATUSES,
        triggered_by_in=(AUTO_TRIGGER,),
    )
    return last is None or last.end_time is None or last.end_time < target, target


def report_schedule_status(
    session: Session, now: datetime | None = None
) -> dict[str, Any]:
    """返回周报固定调度的配置与下次运行时间。"""
    settings = runtime_settings(session)
    current = now or utcnow()
    next_run: datetime | None = None
    if settings.sync_auto_enabled:
        active = _active_job(session, SyncType.WEEKLY_REPORTS)
        if active is not None:
            next_run = current
        else:
            due, target = _report_schedule_due(session, current)
            if due:
                next_run = target
            else:
                local = _business_now(current, settings.report_timezone_offset_hours)
                days_ahead = (
                    settings.sync_reports_weekday - local.weekday()
                ) % 7
                candidate = datetime.combine(
                    local.date() + timedelta(days=days_ahead),
                    time(
                        hour=settings.sync_reports_hour,
                        minute=settings.sync_reports_minute,
                    ),
                )
                if candidate <= local:
                    candidate += timedelta(days=7)
                next_run = candidate - timedelta(
                    hours=settings.report_timezone_offset_hours
                )
    return {
        "auto_enabled": settings.sync_auto_enabled,
        "auto_period_summary_enabled": settings.sync_auto_period_summary_enabled,
        "ai_auto_enqueue_on_sync": settings.ai_auto_enqueue_on_sync,
        "weekday": settings.sync_reports_weekday,
        "weekday_label": WEEKDAY_LABELS[settings.sync_reports_weekday],
        "hour": settings.sync_reports_hour,
        "minute": settings.sync_reports_minute,
        "next_run_at": next_run.isoformat() if next_run else None,
    }


def maybe_enqueue_scheduled_sync(
    session: Session, now: datetime | None = None
) -> list[dict[str, Any]]:
    """按配置把到期的同步任务放入队列；未到时间或已有活跃任务则跳过。"""
    settings = runtime_settings(session)
    if not settings.sync_auto_enabled:
        return []

    current = now or utcnow()
    created: list[dict[str, Any]] = []

    for sync_type, params in (
        (SyncType.DEPARTMENTS, None),
        (SyncType.EMPLOYEES, {"root_department_id": 1}),
    ):
        last = _latest_job(session, sync_type, _FINISHED_SUCCESS_STATUSES)
        due = (
            last is None
            or last.end_time is None
            or last.end_time
            <= current - timedelta(hours=settings.sync_org_interval_hours)
        )
        if due:
            job, was_created = enqueue_sync_job(
                session, sync_type, params=params, triggered_by=AUTO_TRIGGER
            )
            if was_created:
                created.append(serialize_sync_job(job))

    report_due, scheduled_for = _report_schedule_due(session, current)
    if report_due:
        start, end = _report_sync_window(session, current)
        job, was_created = enqueue_sync_job(
            session,
            SyncType.WEEKLY_REPORTS,
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "mode": REPORT_SYNC_CURRENT_PERIOD,
                "scheduled_for": scheduled_for.isoformat(),
            },
            triggered_by=AUTO_TRIGGER,
        )
        if was_created:
            created.append(serialize_sync_job(job))

    if created:
        session.commit()
        logger.info("自动同步已入队 %s 个任务", len(created))
    return created


def recover_interrupted_jobs(
    session: Session, *, max_age_minutes: int | None = None
) -> dict[str, int]:
    """回收停在 RUNNING 的僵尸任务。

    为什么必须有这一步：`enqueue_sync_job` 的幂等语义是「同类型已有 PENDING/RUNNING
    就直接返回原任务」，而 RUNNING 只在任务收尾时被写回。进程被 kill / OOM / 重启
    会让那行永远停在 RUNNING —— 此后每一次手动点击和每一次定时调度都去重到这个
    僵尸上，**周报同步永久停止**，而且 `sync_status` 一直显示 active=true。

    `max_age_minutes=None` 表示无条件回收（Worker 启动时用，此刻旧进程必然已不在）；
    传数值则只回收超过租约时长的（运行期用，防单个线程卡死）。

    回收策略按「重跑是否安全」区分：
      - SyncJob → FAILED：可能已部分写入，不自动重放；标记失败让消息中心可见、运维可重跑。
      - 分析/周期总结/卡点总结队列 → PENDING：按篇或按周期幂等，直接重跑没有副作用。
        覆盖范围由 `REQUEUE_ON_INTERRUPT` 登记表决定，新增队列漏登记会让那一类
        任务永久停在 RUNNING 且用户无法自救（有结构性测试守护，见该表注释）。
    """
    now = utcnow()
    cutoff = (
        None
        if max_age_minutes is None
        else now - timedelta(minutes=max_age_minutes)
    )

    sync_query = select(SyncJob).where(SyncJob.status == SyncJobStatus.RUNNING)
    if cutoff is not None:
        sync_query = sync_query.where(SyncJob.start_time <= cutoff)
    interrupted: list[SyncJob] = list(session.scalars(sync_query).all())
    for job in interrupted:
        started = job.start_time.isoformat() if job.start_time else "未知"
        job.status = SyncJobStatus.FAILED
        job.end_time = now
        job.failed_count = (job.failed_count or 0) or 1
        job.error = [
            {
                "external_id": None,
                "code": "interrupted",
                "message": (
                    f"任务在执行中被中断（开始于 {started}，进程退出或线程卡死），"
                    "已自动回收。请重新触发同步。"
                ),
            }
        ]

    recovered = {
        key: _requeue_running_queue(
            session, model, AnalysisQueueStatus.RUNNING, cutoff
        )
        for key, model in REQUEUE_ON_INTERRUPT
    }
    recovered_total = sum(recovered.values())

    total = len(interrupted) + recovered_total
    if total:
        session.commit()
        logger.warning(
            "回收中断任务：同步 %s 个（标记失败）、队列 %s 个（重新排队）%s",
            len(interrupted),
            recovered_total,
            recovered,
        )
    return {"sync_jobs": len(interrupted), **recovered}


def _requeue_running_queue(
    session: Session, model: Any, running_status: str, cutoff: datetime | None
) -> int:
    """把卡在 RUNNING 的队列项放回 PENDING（分析类任务按篇幂等，可安全重跑）。"""
    query = select(model).where(model.status == running_status)
    if cutoff is not None:
        query = query.where(model.updated_at <= cutoff)
    rows = list(session.scalars(query).all())
    for row in rows:
        row.status = AnalysisQueueStatus.PENDING
        row.next_attempt_at = None
    return len(rows)


def claim_pending_sync_jobs(session: Session, limit: int = 1) -> list[int]:
    """CAS 认领待执行任务。SQLite 会忽略行锁，但条件更新保证只有一次成功。"""
    rows = session.scalars(
        select(SyncJob)
        .where(SyncJob.status == SyncJobStatus.PENDING)
        .order_by(SyncJob.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claimed: list[int] = []
    now = utcnow()
    for row in rows:
        result = session.execute(
            update(SyncJob)
            .where(
                SyncJob.id == row.id,
                SyncJob.status == SyncJobStatus.PENDING,
            )
            .values(status=SyncJobStatus.RUNNING, start_time=now)
        )
        if result.rowcount:
            claimed.append(row.id)
    if claimed:
        session.commit()
    return claimed


def _run_claimed_job(session: Session, job: SyncJob, client: Any | None) -> None:
    from app.services.department_sync import sync_departments
    from app.services.employee_sync import sync_employees
    from app.services.weekly_report_sync import sync_weekly_reports

    if job.type == SyncType.DEPARTMENTS:
        sync_departments(
            session,
            client=client,
            triggered_by=job.triggered_by,
            existing_job=job,
        )
        return

    if job.type == SyncType.EMPLOYEES:
        params = job.params or {}
        sync_employees(
            session,
            client=client,
            root_department_id=int(params.get("root_department_id") or 1),
            triggered_by=job.triggered_by,
            existing_job=job,
        )
        return

    if job.type == SyncType.WEEKLY_REPORTS:
        params = job.params or {}
        end = _parse_datetime(params.get("end")) or utcnow()
        start = _parse_datetime(params.get("start"))
        if start is None and params.get("days"):
            start = end - timedelta(days=int(params["days"]))
        sync_weekly_reports(
            session,
            start=start,
            end=end,
            client=client,
            triggered_by=job.triggered_by,
            existing_job=job,
            full_history=params.get("mode") == REPORT_SYNC_ALL_HISTORY,
        )
        return

    raise ValueError(f"不支持的同步任务类型：{job.type}")


def run_pending_sync_jobs(
    session: Session, limit: int = 1, client: Any | None = None
) -> list[dict[str, Any]]:
    """执行至多 limit 个排队任务；单任务失败只落日志，不影响后续轮询。"""
    claimed_ids = claim_pending_sync_jobs(session, limit=limit)
    results: list[dict[str, Any]] = []
    for job_id in claimed_ids:
        job = session.get(SyncJob, job_id)
        if job is None:
            continue
        try:
            _run_claimed_job(session, job, client)
            session.refresh(job)
            results.append(serialize_sync_job(job))
        except Exception as exc:  # noqa: BLE001 Worker 不能因单个同步任务退出
            session.rollback()
            job = session.get(SyncJob, job_id)
            if job is not None:
                job.status = SyncJobStatus.FAILED
                job.end_time = utcnow()
                # SyncJobRunner 可能已把批次异常写入 failed_count/error；
                # 这里只兜底，避免同一异常被计成两次失败。
                job.failed_count = (job.failed_count or 0) or 1
                if not job.error:
                    job.error = [
                        {
                            "external_id": None,
                            "code": "scheduler_error",
                            "message": str(exc)[:500],
                        }
                    ]
                session.commit()
                results.append(serialize_sync_job(job))
            logger.warning("同步任务 %s 执行失败：%s", job_id, exc)
    return results


def _next_run_at(
    session: Session, sync_type: str, interval: timedelta
) -> str | None:
    if not runtime_settings(session).sync_auto_enabled:
        return None
    if _active_job(session, sync_type) is not None:
        return utcnow().isoformat()
    last = _latest_job(session, sync_type, _FINISHED_SUCCESS_STATUSES)
    if last is None or last.end_time is None:
        return utcnow().isoformat()
    return (last.end_time + interval).isoformat()


def _unresolved_failure(session: Session, sync_type: str) -> SyncJob | None:
    """该同步类型「至今仍未恢复」的那次失败，没有则返回 None。

    语义：失败只在**之后没有再成功过**时才继续上报。最近一次执行如果是成功，
    说明问题（网络、可信 IP、密钥）已经解决，页面上就不该再挂旧红字 ——
    曾经出现过「可信 IP 已加好、周报也同步成功，首页却一直显示 60020」。
    """
    latest = _latest_job(session, sync_type)
    if latest is None or latest.status not in _FINISHED_PROBLEM_STATUSES:
        return None
    return latest


def _type_status(
    session: Session, sync_type: str, interval: timedelta
) -> dict[str, Any]:
    success = _latest_job(session, sync_type, (SyncJobStatus.SUCCESS,))
    problem = _latest_job(
        session,
        sync_type,
        _FINISHED_PROBLEM_STATUSES,
        triggered_by_in=(AUTO_TRIGGER, MANUAL_TRIGGER),
    )
    active = _active_job(session, sync_type)
    unresolved = _unresolved_failure(session, sync_type)
    detail = classify_sync_error(unresolved)
    return {
        "type": sync_type,
        "active": bool(active),
        "active_status": active.status if active else None,
        "last_success_at": success.end_time.isoformat()
        if success and success.end_time
        else None,
        "last_success_window": (
            describe_report_sync_params(success.params or {})
            if success and sync_type == SyncType.WEEKLY_REPORTS
            else None
        ),
        "last_failure_at": problem.end_time.isoformat()
        if problem and problem.end_time
        else None,
        "last_error": _first_error(problem),
        "last_error_detail": detail,
        "last_error_resolved": bool(problem) and detail is None,
        "next_run_at": _next_run_at(session, sync_type, interval),
    }


def sync_status(session: Session) -> dict[str, Any]:
    """给前端/运维看的同步状态：上次成功、失败原因、下次运行和排队数量。"""
    settings = runtime_settings(session)
    org_interval = timedelta(hours=settings.sync_org_interval_hours)

    pending = int(
        session.scalar(
            select(func.count()).select_from(SyncJob).where(
                SyncJob.status == SyncJobStatus.PENDING
            )
        )
        or 0
    )
    running = int(
        session.scalar(
            select(func.count()).select_from(SyncJob).where(
                SyncJob.status == SyncJobStatus.RUNNING
            )
        )
        or 0
    )
    jobs = session.scalars(
        select(SyncJob).order_by(SyncJob.id.desc()).limit(12)
    ).all()

    latest_problem = session.scalars(
        select(SyncJob)
        .where(
            SyncJob.status.in_(_FINISHED_PROBLEM_STATUSES),
            SyncJob.end_time.isnot(None),
            SyncJob.triggered_by.in_((AUTO_TRIGGER, MANUAL_TRIGGER)),
        )
        .order_by(SyncJob.end_time.desc(), SyncJob.id.desc())
        .limit(1)
    ).first()
    latest_success = session.scalars(
        select(SyncJob)
        .where(
            SyncJob.status == SyncJobStatus.SUCCESS,
            SyncJob.end_time.isnot(None),
        )
        .order_by(SyncJob.end_time.desc(), SyncJob.id.desc())
        .limit(1)
    ).first()

    report_schedule = report_schedule_status(session)
    report_status = _type_status(
        session, SyncType.WEEKLY_REPORTS, timedelta(days=7)
    )
    report_status["next_run_at"] = report_schedule["next_run_at"]
    departments = _type_status(session, SyncType.DEPARTMENTS, org_interval)
    employees = _type_status(session, SyncType.EMPLOYEES, org_interval)

    # 顶层错误取「三个同步类型里仍然没恢复的最新一次失败」。
    # 每一类各自成功过就不再计入，避免已修好的旧故障长期挂在首页。
    unresolved = [
        item["last_error_detail"]
        for item in (departments, employees, report_status)
        if item["last_error_detail"] is not None
    ]
    unresolved.sort(key=lambda item: item.get("at") or "", reverse=True)
    overview_error = unresolved[0] if unresolved else None

    return {
        "auto_enabled": settings.sync_auto_enabled,
        "org_interval_hours": settings.sync_org_interval_hours,
        "report_schedule": report_schedule,
        "pending_count": pending,
        "running_count": running,
        "last_success_at": latest_success.end_time.isoformat()
        if latest_success and latest_success.end_time
        else None,
        "last_failure_at": latest_problem.end_time.isoformat()
        if latest_problem and latest_problem.end_time
        else None,
        "last_error": overview_error["summary"] if overview_error else None,
        "last_error_detail": overview_error,
        "last_error_resolved": bool(latest_problem) and overview_error is None,
        "departments": departments,
        "employees": employees,
        "reports": report_status,
        "jobs": [serialize_sync_job(job) for job in jobs],
    }
