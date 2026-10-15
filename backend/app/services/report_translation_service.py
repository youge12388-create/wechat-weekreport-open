from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_utils import utcnow
from app.models.report_translation import ReportTranslation
from app.models.weekly_report import WeeklyReport
from app.processors.journal_parser import strip_html
from app.processors.text_cleaner import clean_text
from app.services.runtime_config_service import runtime_settings
from app.translation import get_translation_provider


def translation_source(report: WeeklyReport) -> str:
    cleaned = clean_text(strip_html(report.content))
    if cleaned:
        return cleaned
    return clean_text(strip_html(report.content_raw))


def translation_source_hash(report: WeeklyReport) -> str:
    return hashlib.sha256(translation_source(report).encode("utf-8")).hexdigest()


def normalize_translation_content(content: str) -> str:
    """翻译结果只保留可读纯文本，避免把富文本标签展示给用户。"""
    return clean_text(strip_html(content))


def _row(session: Session, report_id: int, target_language: str = "zh") -> ReportTranslation | None:
    return session.scalar(
        select(ReportTranslation).where(
            ReportTranslation.report_id == report_id,
            ReportTranslation.target_language == target_language,
        )
    )


def translation_state(session: Session, report: WeeklyReport) -> dict:
    if report.content_language != "english":
        return {
            "available": False,
            "stale": False,
            "status": "not_requested",
            "content": None,
        }
    row = _row(session, report.id)
    source = translation_source(report)
    if not source:
        return {"available": False, "stale": False, "status": "empty", "content": None}
    if row is None:
        return {"available": False, "stale": False, "status": "not_requested", "content": None}
    stale = row.source_hash != translation_source_hash(report)
    return {
        "available": row.status == "success" and not stale,
        "stale": stale,
        "status": "stale" if stale else row.status,
        "content": row.translated_content if row.status == "success" and not stale else None,
        "source_language": row.source_language,
        "target_language": row.target_language,
        "provider": row.provider,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "error": row.error_message if row.status == "failed" and not stale else None,
    }


def translate_report(session: Session, report_id: int, force: bool = False) -> dict:
    report = session.get(WeeklyReport, report_id)
    if report is None:
        raise ValueError("周报不存在")
    if report.content_language != "english":
        raise ValueError("只有英文周报支持翻译为中文。")
    source = translation_source(report)
    if not source:
        raise ValueError("周报正文为空，无法翻译。")
    source_hash = translation_source_hash(report)
    row = _row(session, report.id)
    if row is not None and row.status == "success" and row.source_hash == source_hash and not force:
        return {**translation_state(session, report), "cached": True}
    if row is None:
        row = ReportTranslation(
            report_id=report.id,
            target_language="zh",
            source_hash=source_hash,
            status="running",
        )
        session.add(row)
    else:
        row.source_hash = source_hash
        row.status = "running"
        row.error_message = None
        row.translated_content = None
    try:
        result = get_translation_provider(runtime_settings(session)).translate(source)
        translated_content = normalize_translation_content(result.content)
        if not translated_content:
            raise ValueError("翻译结果为空，请稍后重试。")
        row.source_language = result.source_language
        row.translated_content = translated_content
        row.status = "success"
        row.error_message = None
        row.updated_at = utcnow()
        session.commit()
    except Exception as exc:
        row.status = "failed"
        row.error_message = str(exc)[:1000]
        row.updated_at = utcnow()
        session.commit()
        raise
    return {**translation_state(session, report), "cached": False}
