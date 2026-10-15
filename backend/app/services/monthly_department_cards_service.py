"""Independent, batched monthly department cards backed by the existing summary queue."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai import get_provider
from app.ai.json_util import extract_json_object
from app.ai.prompts import OPERATING_MONTHLY_DEPARTMENTS_SYSTEM, OPERATING_MONTHLY_DEPARTMENTS_PROMPT_VERSION
from app.core.time_utils import utcnow
from app.models.ai_analysis import AIAnalysis
from app.models.enums import AIAnalysisStatus, AnalysisQueueStatus, OperatingSummaryType
from app.models.operating_summary_queue import COMPANY_SCOPE_ID, OperatingSummaryQueue
from app.services import operating_dashboard_service as dashboard
from app.services import operating_summary_service as summary
from app.services.report_analysis_service import report_payload_hash
from app.services.runtime_config_service import runtime_settings

_CARD_FIELD_LIMITS = {
    "headline": 36,
    "progress": 90,
    "blocker": 60,
    "suggested_action": 36,
}


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _own_digests(session: Session, report_ids: list[int], budget: int = 6000) -> list[dict[str, Any]]:
    """Read one current analysis per report; old successful revisions are not evidence."""
    rows = session.execute(
        select(AIAnalysis, summary.WeeklyReport, summary.Employee.name)
        .join(summary.WeeklyReport, summary.WeeklyReport.id == AIAnalysis.entity_id)
        .join(summary.Employee, summary.Employee.id == summary.WeeklyReport.employee_id, isouter=True)
        .where(
            AIAnalysis.entity_type == summary.REPORT_ANALYSIS_ENTITY_TYPE,
            AIAnalysis.analysis_type == summary.REPORT_ANALYSIS_TYPE,
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
            AIAnalysis.entity_id.in_(report_ids),
        ).order_by(AIAnalysis.id.desc())
    )
    seen: set[int] = set()
    digests: list[dict[str, Any]] = []
    used = 0
    omitted = 0
    for analysis, report, employee_name in rows:
        if report.id in seen or (analysis.input_data or {}).get("content_hash") != report_payload_hash(report):
            continue
        seen.add(report.id)
        result = analysis.result_json or {}
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        body = " ".join(str(inner.get("summary") or "").split())[:summary.DIGEST_MAX_CHARS]
        if not body:
            continue
        if used + len(body) > budget:
            omitted += 1
            continue
        used += len(body)
        digests.append({"report_id": report.id, "employee": employee_name, "summary": body})
    if omitted:
        digests.append({"未纳入的周报摘要": omitted, "原因": "篇幅预算已用尽"})
    return digests


def _inputs(
    session: Session, start: date, end: date, *, monthly_payload: dict[str, Any] | None = None,
) -> dict[int, dict[str, Any]]:
    monthly_payload = monthly_payload if monthly_payload is not None else dashboard.monthly_view(session, start, end)
    full = summary._monthly_material(
        session, start, end, department_id=None, payload=monthly_payload
    ) or {}
    briefs = monthly_payload.get("department_briefs") or []
    inputs: dict[int, dict[str, Any]] = {}
    for brief, material in zip(briefs, full.get("各部门情况") or []):
        department_id = brief.get("department_id")
        if not isinstance(department_id, int) or brief.get("name") != material.get("部门"):
            continue  # Names or ordering diverged: never attach another department's evidence.
        report_ids = summary._report_ids_for_period(session, start, end, department_id=department_id)
        if not report_ids:
            continue
        # Give each requested department its own digest budget. Filtering the
        # full-company digest could omit every report from a later department.
        own_digests = _own_digests(session, report_ids)
        reports = session.scalars(select(summary.WeeklyReport).where(summary.WeeklyReport.id.in_(report_ids))).all()
        facts = {
            "department_id": department_id,
            "department": brief["name"],
            "situation": material,
            "report_digests": own_digests,
        }
        readiness = summary.monthly_analysis_readiness(session, report_ids)
        inputs[department_id] = {
            "name": brief["name"],
            "report_ids": report_ids,
            "facts": facts,
            "readiness": readiness,
            "input_hash": _fingerprint({
                "prompt_version": OPERATING_MONTHLY_DEPARTMENTS_PROMPT_VERSION,
                "facts": facts,
                "reports": sorted((report.id, report_payload_hash(report)) for report in reports),
            }),
        }
    return inputs


def _valid_card(raw: Any, allowed: set[int]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        source_ids = list(dict.fromkeys(int(value) for value in raw.get("source_report_ids") or []))
    except (TypeError, ValueError):
        return None
    if not source_ids or any(value not in allowed for value in source_ids):
        return None
    card = {key: " ".join(str(raw.get(key) or "").split()) for key in
            ("headline", "progress", "blocker", "suggested_action")}
    if (not card["headline"] or not (card["progress"] or card["blocker"])
            or any(len(card[key]) > limit for key, limit in _CARD_FIELD_LIMITS.items())
            or len(source_ids) > 3):
        return None
    return {**card, "source_report_ids": source_ids}


def _saved_cards(
    session: Session, queue: OperatingSummaryQueue | None, inputs: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    if queue is None:
        return {}
    rows = session.scalars(
        select(AIAnalysis).where(
            AIAnalysis.entity_type == summary.ENTITY_TYPE,
            AIAnalysis.entity_id == queue.id,
            AIAnalysis.analysis_type == summary.ANALYSIS_TYPE,
            AIAnalysis.status == AIAnalysisStatus.SUCCESS,
        ).order_by(AIAnalysis.id.desc())
    )
    cards: dict[int, dict[str, Any]] = {}
    for row in rows:
        for entry in (row.result_json or {}).get("cards") or []:
            if not isinstance(entry, dict):
                continue
            department_id = entry.get("department_id")
            item = inputs.get(department_id)
            if item is None or department_id in cards or entry.get("input_hash") != item["input_hash"]:
                continue
            card = _valid_card(entry.get("card"), set(item["report_ids"]))
            if card:
                cards[department_id] = card
        if len(cards) == len(inputs):
            break
    return cards


def _company_cards(company_state: dict[str, Any] | None, inputs: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """A current v14 full result remains usable until a separate card replaces it."""
    if OPERATING_MONTHLY_DEPARTMENTS_PROMPT_VERSION != "v1":
        return {}  # A later card prompt must not silently reuse the v14 wording.
    if not company_state or company_state.get("status") != "success" or company_state.get("stale"):
        return {}
    if company_state.get("prompt_version") != "v14":
        return {}
    identities = company_state.get("ref_identities") or {}
    source_map = company_state.get("department_report_ids") or {}
    result = company_state.get("result") or {}
    cards: dict[int, dict[str, Any]] = {}
    for entry in result.get("items") or []:
        if not isinstance(entry, dict):
            continue
        name = identities.get(str(entry.get("ref") or "").upper())
        for department_id, item in inputs.items():
            if (name != item["name"] or not item["readiness"]["analysis_ready"]
                    or not set(item["report_ids"]).issuperset(source_map.get(name) or [])):
                continue
            card = _valid_card(entry, set(item["report_ids"]))
            if card:
                cards[department_id] = card
    return cards


def _targets(inputs: dict[int, dict[str, Any]], cards: dict[int, dict[str, Any]]) -> list[int]:
    return sorted(department_id for department_id, item in inputs.items()
                  if item["readiness"]["analysis_ready"] and department_id not in cards)


def _batch_hash(inputs: dict[int, dict[str, Any]], targets: list[int]) -> str:
    return _fingerprint([(department_id, inputs[department_id]["input_hash"]) for department_id in targets])


def cards_state(
    session: Session, start: date, end: date, *, company_state: dict[str, Any] | None = None,
    poll: bool = False,
    monthly_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    queue = summary._queue_row(session, OperatingSummaryType.MONTHLY_DEPARTMENTS, start, end, COMPANY_SCOPE_ID)
    if poll:
        return {"status": queue.status if queue else "not_generated",
                "queue_updated_at": queue.updated_at.isoformat() if queue and queue.updated_at else None,
                "last_error": queue.last_error if queue else None}
    inputs = _inputs(session, start, end, monthly_payload=monthly_payload)
    cards = _saved_cards(session, queue, inputs)
    cards.update({key: value for key, value in _company_cards(company_state, inputs).items() if key not in cards})
    targets = _targets(inputs, cards)
    state = {
        "status": queue.status if queue else "not_generated",
        "queue_updated_at": queue.updated_at.isoformat() if queue and queue.updated_at else None,
        "last_error": queue.last_error if queue else None,
        "ready_to_generate": len(targets),
        "items": [
            {"department_id": department_id, "name": item["name"],
             **item["readiness"], "status": (
                 "current" if department_id in cards else
                 "waiting_analysis" if not item["readiness"]["analysis_ready"] else
                 "failed" if queue and queue.status == AnalysisQueueStatus.FAILED else "not_generated"
             ), "card": cards.get(department_id)}
            for department_id, item in inputs.items()
        ],
    }
    return state


def enqueue_cards(session: Session, start: date, end: date) -> dict[str, Any]:
    company_state = summary.summary_state(session, "monthly", start, end)
    inputs = _inputs(session, start, end)
    queue = summary._queue_row(session, OperatingSummaryType.MONTHLY_DEPARTMENTS, start, end, COMPANY_SCOPE_ID)
    cards = _saved_cards(session, queue, inputs)
    cards.update({key: value for key, value in _company_cards(company_state, inputs).items() if key not in cards})
    targets = _targets(inputs, cards)
    if not targets:
        return cards_state(session, start, end, company_state=company_state)
    if queue and queue.status in (AnalysisQueueStatus.PENDING, AnalysisQueueStatus.RUNNING):
        return cards_state(session, start, end, company_state=company_state)
    payload_hash = _batch_hash(inputs, targets)
    if queue is None:
        queue = OperatingSummaryQueue(
            summary_type=OperatingSummaryType.MONTHLY_DEPARTMENTS,
            start_date=start, end_date=end, department_id=COMPANY_SCOPE_ID,
            payload_hash=payload_hash, report_count=sum(len(inputs[key]["report_ids"]) for key in targets),
            status=AnalysisQueueStatus.PENDING,
        )
        session.add(queue)
    else:
        queue.payload_hash = payload_hash
        queue.report_count = sum(len(inputs[key]["report_ids"]) for key in targets)
        queue.status = AnalysisQueueStatus.PENDING
        queue.attempts = 0
        queue.next_attempt_at = None
        queue.last_error = None
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # The existing unique scope constraint prevents concurrent duplicate jobs.
    return cards_state(session, start, end, company_state=company_state)


def process_cards_item(queue_id: int) -> dict[str, Any]:
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        queue = session.get(OperatingSummaryQueue, queue_id)
        if queue is None or queue.status != AnalysisQueueStatus.RUNNING:
            return {"queue_id": queue_id, "status": "skipped"}
        inputs = _inputs(session, queue.start_date, queue.end_date)
        company = summary.summary_state(session, "monthly", queue.start_date, queue.end_date)
        cards = _saved_cards(session, queue, inputs)
        cards.update({key: value for key, value in _company_cards(company, inputs).items() if key not in cards})
        targets = _targets(inputs, cards)
        if not targets:
            queue.status = AnalysisQueueStatus.SUCCESS
            session.commit()
            return {"queue_id": queue_id, "status": "skipped"}
        current_hash = _batch_hash(inputs, targets)
        if queue.payload_hash != current_hash:
            queue.payload_hash = current_hash
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = None
            session.commit()
            return {"queue_id": queue_id, "status": "requeued"}
        facts = [inputs[department_id]["facts"] for department_id in targets]
        provider = get_provider(runtime_settings(session))
        started = time.monotonic()
        response = provider.chat(
            OPERATING_MONTHLY_DEPARTMENTS_SYSTEM,
            f"统计周期：{queue.start_date.isoformat()} ~ {queue.end_date.isoformat()}\n"
            f"只为以下 {len(targets)} 个部门生成卡片：\n{json.dumps(facts, ensure_ascii=False)}",
        )
        raw = extract_json_object(response.content)
        if not isinstance(raw.get("items"), list):
            raise ValueError("部门简报缺少 items 数组")
        # Release the transaction/identity map used to build the prompt so a
        # concurrent sync is visible before publishing the model result.
        session.rollback()
        queue = session.get(OperatingSummaryQueue, queue_id)
        if queue is None or queue.status != AnalysisQueueStatus.RUNNING:
            return {"queue_id": queue_id, "status": "skipped"}
        fresh = _inputs(session, queue.start_date, queue.end_date)
        if any(department_id not in fresh or not fresh[department_id]["readiness"]["analysis_ready"]
               or fresh[department_id]["input_hash"] != inputs[department_id]["input_hash"]
               for department_id in targets):
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = None
            session.commit()
            return {"queue_id": queue_id, "status": "requeued"}
        completed: list[dict[str, Any]] = []
        seen: set[int] = set()
        for entry in raw["items"]:
            if not isinstance(entry, dict):
                continue
            try:
                department_id = int(entry.get("department_id"))
            except (TypeError, ValueError):
                continue
            if department_id not in targets:
                continue
            if department_id in seen:
                continue
            card = _valid_card(entry, set(inputs[department_id]["report_ids"]))
            if card:
                completed.append({"department_id": department_id,
                                  "input_hash": inputs[department_id]["input_hash"], "card": card})
                seen.add(department_id)
        if not completed:
            raise ValueError("模型未返回有本部门来源支持的部门卡片")
        summary._write_summary(
            session, queue,
            {"cards": completed, "source_report_ids": sorted({source for entry in completed for source in entry["card"]["source_report_ids"]})},
            AIAnalysisStatus.SUCCESS, response.model,
            int(response.prompt_tokens or 0) or None,
            int(response.completion_tokens or 0) or None,
            int(round((time.monotonic() - started) * 1000)),
        )
        queue.status = AnalysisQueueStatus.SUCCESS
        queue.last_error = None if len(completed) == len(targets) else f"{len(targets) - len(completed)} 个部门缺少合法卡片，可单独重试"
        session.commit()
        return {"queue_id": queue_id, "status": "success", "departments": len(completed)}
    except Exception as exc:  # queue failures remain visible and retryable
        session.rollback()
        queue = session.get(OperatingSummaryQueue, queue_id)
        if queue is None:
            return {"queue_id": queue_id, "status": "failed", "error": str(exc)[:500]}
        queue.attempts = (queue.attempts or 0) + 1
        settings = runtime_settings(session)
        if queue.attempts >= settings.ai_analysis_max_attempts:
            queue.status = AnalysisQueueStatus.FAILED
        else:
            queue.status = AnalysisQueueStatus.PENDING
            queue.next_attempt_at = utcnow() + timedelta(seconds=min(60 * 2 ** queue.attempts, 900))
        queue.last_error = str(exc)[:1000]
        session.commit()
        return {"queue_id": queue_id, "status": queue.status, "error": str(exc)[:500]}
    finally:
        session.close()
