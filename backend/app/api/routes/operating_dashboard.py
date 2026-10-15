"""经营看板只读聚合接口。

一个入口服务三个视图，避免前端为同一份统计拼接六个请求、也避免把新模块的
计算塞进 ``/api/dashboard``（方案 7.2：现有 dashboard 的响应字段和行为不因
新模块改变）。

四条边界：
* **数据只读**：视图接口只有 GET。触发 AI 总结是独立 POST，且只写队列不在此同步等模型。
* **轮询走 GET**：面板会周期性刷新，GET 必须只读 —— 若 GET 顺带入队，
  每几秒一次的轮询会把正在跑的任务反复打回待处理（管理简报踩过这个坑）。
* **member 不可访问**：由 ``AuthMiddleware`` 的成员路径白名单拦下（GET 也 403），
  本路由不需要额外的角色判断。
* **效度判定是写操作，独立成 POST**（``/feedback``）：它写的是「人对结论的判断」，
  与只读的视图数据分属两类。GET 只把已有判定读回来展示，不产生任何写入。
"""

from __future__ import annotations

import copy
import csv
import threading
import time
from datetime import date
from io import StringIO
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import AdminOnly, CurrentActor, CurrentSession, get_db
from app.core.config import get_settings
from app.services import dashboard_feedback_service as feedback
from app.services import operating_dashboard_service as dashboard
from app.services import operating_summary_service as summary
from app.services import monthly_department_cards_service as monthly_cards
from app.services.view_service import InvalidPeriodError, resolve_period

router = APIRouter(prefix="/api/operating-dashboard", tags=["operating-dashboard"], dependencies=[AdminOnly])

# 看板是只读聚合接口，单次计算约 2 秒。短 TTL 的进程内快照用于覆盖用户连续刷新、
# 切回页面和多个管理员同时打开的场景；测试环境关闭它，避免跨用例共享数据。
# summary 缓存与读缓存同 TTL：它防的是同一类「短窗口内重复全量重算」，
# 给 3 秒只会在连续刷新时几乎每次 miss；高频轮询已改走 /summary?poll=1，
# 不再依赖这份缓存兜轮询负载。
_READ_CACHE_TTL_SECONDS = 15.0
_SUMMARY_CACHE_TTL_SECONDS = 15.0
_READ_CACHE_MAX_ENTRIES = 32
_SUMMARY_CACHE_MAX_ENTRIES = 64
_read_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_summary_cache: dict[tuple[str, str, str, int | None], tuple[float, dict]] = {}
_read_cache_lock = threading.Lock()


def _clear_read_cache() -> None:
    with _read_cache_lock:
        _read_cache.clear()
        _summary_cache.clear()
    # 环比基数快照同清：入队意味着数据即将变化，上一周期的对比数字也要重算。
    dashboard.clear_previous_metrics_cache()


def _cached_summary_state(
    session: Session,
    view: SummaryScope,
    start_date: date,
    end_date: date,
    department_id: int | None = None,
) -> dict:
    if get_settings().app_env == "test":
        return summary.summary_state(
            session, view, start_date, end_date, department_id=department_id
        )

    key = (view, start_date.isoformat(), end_date.isoformat(), department_id)
    now = time.monotonic()
    with _read_cache_lock:
        cached = _summary_cache.get(key)
        if cached and now - cached[0] <= _SUMMARY_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])
        if cached:
            _summary_cache.pop(key, None)

    state = summary.summary_state(
        session, view, start_date, end_date, department_id=department_id
    )
    with _read_cache_lock:
        if len(_summary_cache) >= _SUMMARY_CACHE_MAX_ENTRIES:
            _summary_cache.pop(next(iter(_summary_cache)), None)
        _summary_cache[key] = (time.monotonic(), copy.deepcopy(state))
    return state


def _cached_dashboard_payload(
    session: Session, view: DashboardView, start_date: date, end_date: date
) -> dict:
    if get_settings().app_env == "test":
        return dashboard.build_view(session, view, start_date, end_date)

    key = (view, start_date.isoformat(), end_date.isoformat())
    now = time.monotonic()
    with _read_cache_lock:
        cached = _read_cache.get(key)
        if cached and now - cached[0] <= _READ_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])
        if cached:
            _read_cache.pop(key, None)

    payload = dashboard.build_view(session, view, start_date, end_date)
    with _read_cache_lock:
        if len(_read_cache) >= _READ_CACHE_MAX_ENTRIES:
            _read_cache.pop(next(iter(_read_cache)), None)
        _read_cache[key] = (time.monotonic(), copy.deepcopy(payload))
    return payload

# GET 的可视视图。**不含 `department`**：部门视图已作为产品界面裁掉（部门维度的信息
# 并入总览的瓶颈/摩擦），所以 `?view=department` 现在是 422，而不是返回一份没有
# 页面消费的 payload。
DashboardView = Literal["overview", "weekly", "monthly"]

# 总结作用域。overview 是周度综合视图的 UI 名称，内部复用 weekly_company 总结。
# **保留 `department`**：部门 AI 结论是独立能力（结构化输出含 focus/blockers/dependencies），
# 不是那个被裁掉的部门视图，仍可由 API 直接触发。
SummaryScope = Literal["overview", "weekly", "monthly", "monthly_departments", "department"]

# CSV 里以这些字符开头的单元格会被 Excel 当公式执行（DDE 等）。看板导出的
# 标题来自周报正文，属于外部输入，必须中和 —— 与 exports.py 同一套做法。
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def _csv_response(filename: str, header: list[str], rows: list[list[str]]) -> Response:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_cell(cell) for cell in row])
    # utf-8-sig：Excel 需要 BOM 才能正确识别中文，否则整片乱码。
    return Response(
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _period(start: str | None, end: str | None) -> tuple[date, date]:
    try:
        return resolve_period(start, end)
    except InvalidPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def read_dashboard(
    session: Annotated[Session, Depends(get_db)],
    view: DashboardView = Query(default="weekly"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """按视图返回经营看板的完整数据（含只读的总结状态）。

    每个视图都返回 ``coverage``：篇数与分析覆盖率是「结论是否可信」的前提信息，
    不能只在某一个视图里给。
    """
    start_date, end_date = _period(start, end)
    payload = _cached_dashboard_payload(session, view, start_date, end_date)
    payload["period"] = {"start": start_date.isoformat(), "end": end_date.isoformat()}
    payload["summary"] = _cached_summary_state(session, view, start_date, end_date)
    if view == "monthly":
        payload["summary"]["department_cards"] = monthly_cards.cards_state(
            session, start_date, end_date, company_state=payload["summary"], monthly_payload=payload,
        )
    # 每月视图的「需要管理层介入」清单由代码算出骨架，AI 只补建议动作：
    # 原因 / 影响 / 责任方 / 持续时间保持代码算出的值 —— 模型看不到卡点自身的跨周记录，
    # 让它写「已持续几周」等于让它猜数字。没有 AI 结论时规则版动作仍然可执行。
    #
    # **必须在这里合并而不是在缓存里**：缓存的是确定性骨架（不含 AI 结论），
    # 合并放进去会让「AI 结论变了但骨架没变」的请求拿到旧动作。
    if view == "monthly" and isinstance(payload.get("interventions"), dict):
        payload["interventions"] = dashboard.merge_summary_actions(
            payload["interventions"], payload["summary"]
        )
        # AI 写的月度简报（headline / highlights / 每部门一句话）也在这里附上。
        # 没有生成过时 `narrative` 为缺席，页面只显示等待/失败空态。
        payload = dashboard.merge_monthly_narrative(payload, payload["summary"])
        payload = dashboard.merge_monthly_department_cards(
            payload, payload["summary"].get("department_cards")
        )
    # 总览各卡片（重点项目 / 组织摩擦 / 长期趋势）的正文同理：缓存里是规则版结论，
    # AI 写的（item_notes）在这里按 ref_identities 校验名字后贴回。
    # 没生成过 AI 结论时保留规则版，页面不空。
    payload = dashboard.merge_ai_item_notes(payload, payload.get("summary"))
    # 已有的人工判定随 payload 一起返回，页面据此回显「这条我判过了」。
    # 只读：这里绝不写入，判定走独立的 POST /feedback。
    payload["feedback"] = feedback.feedback_index(session, start=start_date, end=end_date)
    return payload


class FeedbackIn(BaseModel):
    """一条结论的人工判定。"""

    module: Literal["attention", "bottleneck", "duplicate", "conflict", "project"]
    key: str = Field(min_length=1, max_length=400, description="结论的业务键")
    verdict: Literal["correct", "wrong", "unclear"]
    title: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)
    start: str | None = None
    end: str | None = None


@router.post("/feedback")
def submit_feedback(
    payload: FeedbackIn,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[CurrentSession, CurrentActor],
) -> dict:
    """记录人工对一条结论的判定（说得对 / 说错了 / 说不清）。

    **这是本模块唯一的写入口**，且只写「人对结论的判断」，不改任何业务数据。
    它存在的理由：自动化测试只能证明「代码按规则跑」，证明不了「规则符合现实」。
    没有这批判定，审计发现的三类错误（同一任务算成两条主线、不同的事判成状态矛盾、
    空资源需求占位）只能靠人工翻原文才能发现，而且无法量化。

    判定人取自网关注入的会话（`CurrentActor`），不由前端传 —— 否则任何人都能
    伪造他人判定，这份报表作为审计证据就不成立了。

    响应带 ``entry``（与 GET 返回的 ``feedback`` 索引里的条目同形）：判定只影响
    回显索引、不改变视图数据，前端拿到后**本地合并**即可 —— 不需要全量刷新视图，
    也不需要清只读缓存（见下）。
    """
    start_date, end_date = _period(payload.start, payload.end)
    try:
        row = feedback.record_feedback(
            session,
            module=payload.module,
            start=start_date,
            end=end_date,
            key=payload.key,
            verdict=payload.verdict,
            reviewer=feedback_reviewer(actor),
            title=payload.title,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # **不清理 _read_cache/_summary_cache**：那两份缓存里根本没有 feedback 数据 ——
    # GET 是在缓存之后才把 feedback 索引合并进 payload 的（见 read_dashboard）。
    # 判定不改变任何被缓存的计算结果，清缓存只会把其他管理员的命中率白打掉。
    return {
        "ok": True,
        "signal_key": row.signal_key,
        "module": row.module,
        "verdict": row.verdict,
        "verdict_label": feedback.VERDICT_LABELS.get(row.verdict, row.verdict),
        "reviewer": row.reviewer,
        "entry": feedback.feedback_entry(row),
    }


def feedback_reviewer(actor: Any) -> str:
    """判定人标识。

    企微会话用员工 id（`employee:{id}`），访问密码会话固定 `admin` ——
    后者没有员工身份（employee_id 恒为 None），用名字会把多个管理员混成一条。
    """
    employee_id = getattr(actor, "employee_id", None)
    if employee_id is not None:
        return f"employee:{int(employee_id)}"
    return "admin"


@router.get("/feedback/summary")
def read_feedback_summary(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict:
    """按模块汇总人工判定的准确率。

    样本不足时 `accuracy` 为 null 并带 `min_sample`：一条判对的结论算出 100%
    会误导人去信任它，而它的样本量根本不支持任何结论。
    """
    start_date, end_date = _period(start, end)
    return feedback.feedback_summary(session, start=start_date, end=end_date)


@router.get("/summary")
def read_summary(
    session: Annotated[Session, Depends(get_db)],
    view: SummaryScope = Query(default="weekly"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    department_id: int | None = Query(default=None, ge=1),
    poll: bool = Query(default=False),
) -> dict:
    """只读的总结状态。

    两种模式：
    * 默认走 TTL 快照缓存（``_cached_summary_state``），供页面加载与合并；
    * ``?poll=1`` 是轻量轮询模式 —— 只读队列行状态，**不重算事实指纹**。
      ``summary_state`` 判 stale 必须先重算整份看板（秒级），而前端每 4 秒轮询
      一次只关心 status 翻转；让它直接打全量等于每个开着的面板持续制造
      全量重算。翻转后的 stale/结果合并由那次触发的整页请求完成。
    """
    start_date, end_date = _period(start, end)
    if view == "monthly_departments":
        return monthly_cards.cards_state(
            session, start_date, end_date, poll=poll,
            company_state=None if poll else _cached_summary_state(session, "monthly", start_date, end_date),
        )
    if poll:
        return summary.queue_snapshot(
            session, view, start_date, end_date, department_id=department_id
        )
    return _cached_summary_state(
        session, view, start_date, end_date, department_id=department_id
    )


@router.post("/summary")
def generate_summary(
    session: Annotated[Session, Depends(get_db)],
    view: SummaryScope = Query(default="weekly"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    department_id: int | None = Query(default=None, ge=1),
    force: bool = Query(default=False),
) -> dict:
    """把总结排进后台队列并立即返回。

    不同步等待模型：一次总结是几十秒级的调用，放在请求里会顶到网关超时，
    也会让用户以为页面卡死。结果由前端轮询只读 GET 获取。

    作用域含 ``department``（需同时传 ``department_id``）——它服务部门的 AI 结论，
    与被裁掉的部门**视图**是两件事。
    """
    start_date, end_date = _period(start, end)
    if view == "monthly_departments":
        result = monthly_cards.enqueue_cards(session, start_date, end_date)
        _clear_read_cache()
        return result
    result = summary.enqueue_summary(
        session,
        view,
        start_date,
        end_date,
        department_id=department_id,
        force=force,
    )
    _clear_read_cache()
    return result


@router.get("/export")
def export_overview(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> Response:
    """导出本周分析报告（CSV）。

    导出的每一行都带来源周报 ID：看板上的结论是聚合出来的，离开页面之后
    必须还能回到原文核对，否则这份文件只能被当成结论照抄。
    """
    start_date, end_date = _period(start, end)
    payload = dashboard.build_view(session, "overview", start_date, end_date)
    # 导出要包含 AI 结论，所以这里补上只读的总结状态（与 GET 接口同一份，
    # 不会因为导出而触发模型调用）。
    payload["summary"] = summary.summary_state(session, "weekly", start_date, end_date)
    # 导出同样贴回 AI 结论（与 GET 同一份状态，不触发模型调用）。
    payload = dashboard.merge_ai_item_notes(payload, payload.get("summary"))
    rows: list[list[str]] = []
    coverage = payload.get("coverage") or {}
    rows.append(
        [
            f"统计周期 {start_date.isoformat()} ~ {end_date.isoformat()}",
            f"周报 {coverage.get('reports_total', 0)} 篇",
            f"已分析 {coverage.get('analyzed_reports', 0)} 篇",
            "分析不完整，结论仅供参考" if coverage.get("partial") else "分析完整",
        ]
    )
    rows.append([])
    rows.append(["一、今天值得关注", "结论", "依据篇数", "来源周报"])
    for item in payload.get("attention", {}).get("items", []):
        rows.append(
            [
                f"[{item.get('label', '')}] {item.get('title', '')}",
                item.get("conclusion", ""),
                f"{item.get('evidence_count', 0)} 篇",
                _ids(item.get("source_report_ids")),
            ]
        )
    rows.append([])
    rows.append(["二、最大运行瓶颈", "影响工作数", "涉及部门", "依据篇数", "来源周报"])
    for item in payload.get("bottlenecks", {}).get("items", []):
        rows.append(
            [
                item.get("target", ""),
                str(item.get("workstream_count", 0)),
                str(item.get("department_count", 0)),
                f"{item.get('evidence_count', 0)} 篇",
                _ids(item.get("source_report_ids")),
            ]
        )
    rows.append([])
    rows.append(["三、组织摩擦", "类型", "涉及部门/人员", "来源周报"])
    frictions = payload.get("frictions") or {}
    for item in frictions.get("duplicates", []):
        rows.append(
            [
                item.get("project", ""),
                "疑似重复推进，建议核对分工",
                "、".join(item.get("departments") or []),
                _ids(item.get("source_report_ids")),
            ]
        )
    for item in frictions.get("conflicts", []):
        rows.append(
            [
                item.get("title", ""),
                "状态描述不一致",
                f"已完成：{'、'.join(item.get('finished_by') or [])} / 未解决：{'、'.join(item.get('open_by') or [])}",
                _ids(item.get("source_report_ids")),
            ]
        )
    rows.append([])
    rows.append(["四、重点项目进展", "当前状态", "结论", "覆盖篇数", "来源周报"])
    for item in payload.get("projects", {}).get("items", []):
        rows.append(
            [
                item.get("name", ""),
                item.get("status_label", ""),
                item.get("conclusion", ""),
                f"{item.get('report_count', 0)} 篇",
                _ids(item.get("source_report_ids")),
            ]
        )
    rows.append([])
    rows.append(["五、下周需要关注", "事实", "建议动作", "来源周报"])
    summary_result = payload.get("summary") or {}
    result = summary_result.get("result") if isinstance(summary_result.get("result"), dict) else {}
    for item in (result.get("attention") or []):
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                item.get("title", ""),
                item.get("reason", ""),
                item.get("suggested_action", ""),
                _ids(item.get("source_report_ids")),
            ]
        )
    if not (result.get("attention") or []):
        rows.append(["（尚未生成 AI 结论）", "", "", ""])

    return _csv_response(
        f"operating_overview_{start_date.isoformat()}_{end_date.isoformat()}.csv",
        ["项目", "说明", "范围", "来源"],
        rows,
    )


def _ids(values: Any) -> str:
    """把周报 ID 列表渲染成可核对的文本；空列表写「—」而不是留空。"""
    if not values:
        return "—"
    return "、".join(f"#{int(value)}" for value in values)
