"""企业微信汇报 -> RawRecord + WeeklyReport。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.connectors.base import ReportSource, RawPayload
from app.connectors.wecom.client import REPORT_DETAIL_MIN_INTERVAL
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.time_utils import period_range, utcnow
from app.models.employee import Employee
from app.models.enums import DataSource, ProcessStatus, RawSourceType, SyncType
from app.models.sync_job import SyncJob
from app.models.weekly_report import WeeklyReport
from app.processors.journal_parser import ParsedJournal, parse_journal_info
from app.processors.language_detector import detect_content_language
from app.services.runtime_config_service import runtime_settings
from app.services.view_service import report_ai_exempt
from app.services.sync_base import (
    SyncJobRunner,
    SyncResult,
    mark_processed,
    upsert_raw_record,
)

logger = get_logger(__name__)


def _auto_translate_english_report(
    session: Session, report: WeeklyReport | None
) -> None:
    """英文周报同步后自动翻译；失败只影响译文状态，不影响周报同步。"""
    if report is None or report.content_language != "english":
        return

    from app.services.report_translation_service import translate_report

    try:
        result = translate_report(session, report.id)
        if not result.get("cached"):
            logger.info("英文周报 %s 已自动翻译为中文", report.external_id)
    except Exception as exc:  # noqa: BLE001 翻译是派生数据，不能拖垮同步
        logger.warning("英文周报 %s 自动翻译失败：%s", report.external_id, exc)


def _enqueue_auto_period_summaries(
    session: Session, periods: set[tuple[date, date]]
) -> None:
    """按本次受影响周期触发全员总结；失败只记录，不影响周报同步结果。

    这是「抓完就自动出总览」的入口：只要本批有新增或内容变化的周报，
    对应周期就会被入队，Worker 消费后首页「本周概览」直接有结论，
    不需要用户点开面板才开始跑。
    """
    if not periods:
        return
    try:
        if not runtime_settings(session).sync_auto_period_summary_enabled:
            logger.info("自动生成本期 AI 总结已关闭，跳过 %s 个周期", len(periods))
            return
        from app.services.period_summary_service import enqueue_period_summary

        for start, end in sorted(periods):
            state = enqueue_period_summary(session, start, end)
            logger.info(
                "周报同步后自动生成 %s ~ %s 周期总结：status=%s，本期 %s 篇",
                start,
                end,
                state.get("status"),
                state.get("reports_total"),
            )
    except Exception as exc:  # noqa: BLE001 派生 AI 结果不能拖垮周报入库
        session.rollback()
        logger.warning("自动生成本期 AI 总结失败：%s", exc)


def _resolve_employee_id(session: Session, userid: str | None) -> int | None:
    if not userid:
        return None
    employee = session.scalar(
        select(Employee).where(
            Employee.source == DataSource.WECOM,
            Employee.external_id == userid,
        )
    )
    if employee is None:
        logger.warning(
            "汇报提交人 %s 未在员工表中，本条周报的 employee_id 留空（请先同步员工）",
            userid,
        )
        return None
    return employee.id


def _resolve_template_metadata(
    session: Session, template_id: str | None
) -> tuple[str | None, str | None]:
    """按同一模板的已正确样本补齐类型/模板名。

    企微偶发不返回 template_name，若只按名称识别就会把同一份模板误分到
    “其他”。template_id 是稳定标识，因此这里把它当作纠偏锚点。
    """
    if not template_id:
        return None, None

    type_row = session.execute(
        select(WeeklyReport.report_type, func.count(WeeklyReport.id))
        .where(
            WeeklyReport.template_id == template_id,
            WeeklyReport.report_type != "other",
        )
        .group_by(WeeklyReport.report_type)
        .order_by(func.count(WeeklyReport.id).desc())
        .limit(1)
    ).first()
    name_row = session.execute(
        select(WeeklyReport.template_name, func.count(WeeklyReport.id))
        .where(
            WeeklyReport.template_id == template_id,
            WeeklyReport.template_name.isnot(None),
            WeeklyReport.template_name != "",
        )
        .group_by(WeeklyReport.template_name)
        .order_by(func.count(WeeklyReport.id).desc())
        .limit(1)
    ).first()
    known_type = str(type_row[0]) if type_row and type_row[0] else None
    known_name = str(name_row[0]) if name_row and name_row[0] else None
    return known_type, known_name


def _upsert_weekly_report(
    session: Session, record_id: int | None, raw: RawPayload, parsed: ParsedJournal
) -> tuple[WeeklyReport, bool]:
    report = session.scalar(
        select(WeeklyReport).where(
            WeeklyReport.source == raw.source,
            WeeklyReport.external_id == raw.external_id,
        )
    )
    is_new = report is None
    if report is None:
        report = WeeklyReport(source=raw.source, external_id=raw.external_id)
        session.add(report)

    previous_payload = (
        report.employee_id,
        report.report_type,
        report.title,
        report.content_raw,
        report.content,
        report.content_language,
        report.start_date,
        report.end_date,
        report.submitted_at,
    )

    known_type, known_name = _resolve_template_metadata(session, parsed.template_id)
    resolved_type = (
        known_type
        if parsed.report_type == "other" and known_type
        else parsed.report_type
    )
    resolved_name = parsed.template_name or known_name

    if parsed.report_time:
        # 归属天数走**运行时配置**：周一才交上来的那篇属于上周，回溯几天由
        # 管理员在设置页按实际情况调，改完下一次同步即生效，无需重启。
        start_date, end_date = period_range(
            parsed.report_time,
            resolved_type,
            attribution_days=runtime_settings(session).report_period_attribution_days,
        )
        report.start_date = start_date
        report.end_date = end_date
        report.submitted_at = parsed.report_time

    report.employee_id = _resolve_employee_id(session, parsed.submitter_userid)
    report.report_type = resolved_type
    report.title = resolved_name or f"{resolved_type}汇报"
    # content_raw 为接口提取出的未清洗正文；content 为清洗后正文。
    # 真正的原始存档是 RawRecord.raw_payload，永不被覆盖。
    report.content_raw = parsed.content_raw
    report.content = parsed.content
    # 语言按**清洗后正文**判定：content_raw 是未清洗富文本，表格模板的 HTML
    # 标记（style="width:1496px" 等）全是拉丁字母，实测能把中文周报的拉丁占比
    # 推到 92%+ 而被误判成 english。与周期总结/单篇分析同一口径：正文以清洗后为准。
    report.content_language = detect_content_language(
        parsed.content or parsed.content_raw
    )
    report.template_id = parsed.template_id
    report.template_name = resolved_name
    report.raw_record_id = record_id
    report.updated_at = utcnow()
    session.flush()

    # 仅新入库或影响模型输入的字段变化时才清缓存并重新入队。
    # 同一份周报重复同步绝不能把已成功结论清空，否则页面会误报“未分析”。
    from app.services.analysis_queue_service import enqueue_report
    from app.services.report_analysis_service import clear_report_analysis_cache
    from app.services.structured_data_service import materialize_resource_demand

    current_payload = (
        report.employee_id,
        report.report_type,
        report.title,
        report.content_raw,
        report.content,
        report.content_language,
        report.start_date,
        report.end_date,
        report.submitted_at,
    )
    if is_new or previous_payload != current_payload:
        if runtime_settings(session).ai_auto_enqueue_on_sync:
            enqueue_report(session, report)
        elif report.ai_checked_at is not None:
            # 自动分析关闭时同步只入库不入队；但内容已变化，旧的
            # 「已分析」标记是过期结论，清掉让页面如实显示未分析。
            clear_report_analysis_cache(session, report)
    # 「资源需求」是企微表单中的确定字段，即使后台 AI 尚未消费队列，也要先
    # 作为待确认卡点可检索；但免提交人员不进入统计，候选也要等取消免提交后
    # 再通过扫描/回填生成。重复同步只会幂等刷新这一条候选。
    if not report_ai_exempt(session, report):
        materialize_resource_demand(session, report)
    return report, is_new


def sync_weekly_reports(
    session: Session,
    start: datetime | None = None,
    end: datetime | None = None,
    client: ReportSource | None = None,
    triggered_by: str = "system",
    existing_job: SyncJob | None = None,
    full_history: bool = False,
) -> SyncResult:
    """同步周报。

    幂等控制：
      - RawRecord  按 (source, source_type, external_id)
      - WeeklyReport 按 (source, external_id)
      - 内容未变化且已处理过的记录直接跳过，重复同步不产生新数据

    失败隔离：
      - 列表翻页失败 = 批次级失败（游标无法续传），由 SyncJobRunner 记录后抛出
      - 详情 / 解析失败 = 单条级失败，记录后继续
    """
    settings = get_settings()
    if client is None:
        from app.connectors.wecom.client import WeComClient

        client = WeComClient()

    end = end or utcnow()
    start = start or (end - timedelta(days=settings.wecom_sync_lookback_days))
    affected_periods: set[tuple[date, date]] = set()

    with SyncJobRunner(
        session,
        DataSource.WECOM,
        SyncType.WEEKLY_REPORTS,
        params={
            "start": start.isoformat(),
            "end": end.isoformat(),
            "template_ids": settings.wecom_report_template_ids,
        },
        triggered_by=triggered_by,
        job=existing_job,
    ) as job:
        processed = 0
        created_count = 0
        created_weekly_count = 0
        try:
            for external_id in client.iter_report_ids(start, end):
                if (
                    not full_history
                    and processed >= settings.sync_max_records_per_run
                ):
                    logger.warning(
                        "达到单批次上限 %s 条，剩余记录本次不同步",
                        settings.sync_max_records_per_run,
                    )
                    break
                processed += 1

                try:
                    raw = client.get_report_detail(external_id)
                    record, changed = upsert_raw_record(session, raw)

                    if not changed and record.process_status == ProcessStatus.PROCESSED:
                        report = session.scalar(
                            select(WeeklyReport).where(
                                WeeklyReport.source == raw.source,
                                WeeklyReport.external_id == raw.external_id,
                            )
                        )
                        _auto_translate_english_report(session, report)
                        job.skipped()
                        continue

                    info = raw.payload.get("info") or {}
                    parsed = parse_journal_info(info, external_id, settings)
                    report, is_new = _upsert_weekly_report(
                        session, record.id, raw, parsed
                    )
                    mark_processed(session, record)
                    session.commit()
                    if is_new:
                        created_count += 1
                        if report.report_type == "weekly":
                            created_weekly_count += 1

                    if parsed.warnings:
                        logger.warning(
                            "汇报 %s 解析告警：%s", external_id, "；".join(parsed.warnings)
                        )
                    job.success()
                    if (
                        report.report_type == "weekly"
                        and not report_ai_exempt(session, report)
                        and report.start_date is not None
                        and report.end_date is not None
                    ):
                        affected_periods.add((report.start_date, report.end_date))
                    _auto_translate_english_report(session, report)
                except Exception as exc:  # noqa: BLE001  单条失败不影响整批
                    session.rollback()
                    logger.warning("汇报 %s 同步失败：%s", external_id, exc)
                    job.failed(external_id, str(exc))
        finally:
            # 与 success_count（新增 + 重新处理）分开保存。旧任务没有这个键，
            # 展示时必须标为「未记录」，不能误报新增 0 篇。
            job.job.params = {
                **(job.job.params or {}),
                "created_count": created_count,
                "created_weekly_count": created_weekly_count,
            }

        # 抓取结束时的覆盖说明。**全历史模式特别需要它**：这个模式的目的是「建档」，
        # 而失败率高时任务状态只是 PARTIAL（有成功也有失败），看起来像「跑完了」。
        # 把「计划处理多少 / 实际失败多少」写进日志与错误列表，
        # 运维才看得出「是不是只抓到一点」。
        if full_history and job.result.failed_count:
            attempted = processed
            logger.warning(
                "全历史抓取结束：计划处理 %s 篇，成功 %s，失败 %s，跳过 %s。"
                "失败率高通常是被企微限流（45009）——详情接口已按 %.1fs 间隔节流并退避重试；"
                "若仍大面积失败，请降低抓取并发后重试。",
                attempted,
                job.result.success_count,
                job.result.failed_count,
                job.result.skipped_count,
                REPORT_DETAIL_MIN_INTERVAL,
            )

        _enqueue_auto_period_summaries(session, affected_periods)
        # 自动总结失败会 rollback 派生写入，重新附上本批统计供任务收尾提交。
        job.job.params = {
            **(job.job.params or {}),
            "created_count": created_count,
            "created_weekly_count": created_weekly_count,
        }
    return job.result
