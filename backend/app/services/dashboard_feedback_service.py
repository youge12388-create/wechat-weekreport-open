"""经营看板结论的人工效度判定与准确率统计。

**为什么需要它**：看板的自动化测试只能证明「代码按规则跑」，证明不了「规则符合
现实」。实测过的三类错误（同一任务被算成两条主线、「状态不一致」把两件不同的事判成
矛盾、空资源需求占掉首屏名额）在 47 项测试里全绿 —— 它们测的是规则执行，不是规则
对错。要让「准不准」变成可讨论的事实，就必须有人对结论表态，并把这批表态算出来。

三条纪律：

1. **指纹必须跨重算稳定**。``signal_key`` 由模块、周期与结论的业务键组成，不含
   分数、序号或任何会被算法改动影响的值 —— 否则改一次权重，之前攒的判定全部对不上号。
2. **不自动判定**。只记录人工表态，绝不因为「结论被纳入跟进」就推断它是准确的。
3. **「说不清」是有效答案**。证据不足的结论既不该硬判准确，也不该算作错误；
   它单独统计，避免把两种完全不同的原因混成一个准确率。
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.dashboard_feedback import DashboardFeedback

# 可判定的模块。与前端按钮一一对应，新增模块必须同时改这里与前端。
FEEDBACK_MODULES = ("attention", "bottleneck", "duplicate", "conflict", "project")

VERDICTS = ("correct", "wrong", "unclear")

# 判定文字。放在后端：前端只渲染，不再维护第二份映射（两处各一份迟早对不上）。
VERDICT_LABELS = {
    "correct": "说得对",
    "wrong": "说错了",
    "unclear": "说不清",
}

# 准确率的样本门槛：低于这个数量不给百分比。
# 一条判对的结论算出 100% 会误导人去信任它，而它的样本量根本不支持任何结论。
MIN_SAMPLE_FOR_RATE = 5


def build_signal_key(module: str, start: date, end: date, key: str) -> str:
    """结论指纹。``key`` 是结论的业务键（瓶颈对象名 / 事项名 / 项目名）。"""
    return f"{module}|{start.isoformat()}|{end.isoformat()}|{key}"


def _normalize_key(value: str | None) -> str:
    return " ".join((value or "").split())[:400]


def record_feedback(
    session: Session,
    *,
    module: str,
    start: date,
    end: date,
    key: str,
    verdict: str,
    reviewer: str,
    title: str | None = None,
    reason: str | None = None,
) -> DashboardFeedback:
    """写入或更新一条判定（同一人同一结论只保留最新一条）。

    校验放在这里而不是只靠路由：漏校验的调用方会往库里写进无法聚合的脏数据，
    而准确率报表是按这些字段分组的，脏数据会让报表给出看似有效的错误结论。
    """
    if module not in FEEDBACK_MODULES:
        raise ValueError(f"不支持的判定模块：{module}")
    if verdict not in VERDICTS:
        raise ValueError(f"不支持的判定结论：{verdict}")
    normalized = _normalize_key(key)
    if not normalized:
        raise ValueError("判定必须带结论标识")

    signal_key = build_signal_key(module, start, end, normalized)
    row = session.scalar(
        select(DashboardFeedback).where(
            DashboardFeedback.signal_key == signal_key,
            DashboardFeedback.reviewer == reviewer,
        )
    )
    if row is None:
        row = DashboardFeedback(
            module=module,
            signal_key=signal_key,
            period_start=start,
            period_end=end,
            reviewer=reviewer,
        )
        session.add(row)
    row.verdict = verdict
    row.title = (title or row.title)[:2000] if (title or row.title) else None
    # 改成「说得对 / 说不清」时清掉旧原因：留着一条与当前判定不符的说明会误导复盘。
    row.reason = (reason or "")[:2000] or None if verdict == "wrong" else None
    session.commit()
    session.refresh(row)
    return row


def feedback_entry(row: DashboardFeedback) -> dict[str, Any]:
    """单条判定的展示形状。

    ``feedback_index``（回显索引）与 ``POST /feedback`` 的响应共用这一个构造，
    保证「索引里的一条」与「提交后前端立即合并的那条」永远同形 —— 两处各拼一份，
    迟早有一天字段对不上，页面就会静默丢「已判定」状态。
    """
    return {
        "verdict": row.verdict,
        "label": VERDICT_LABELS.get(row.verdict, row.verdict),
        "reason": row.reason,
        "reviewer": row.reviewer,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def feedback_index(
    session: Session, *, start: date, end: date, reviewer: str | None = None
) -> dict[str, dict[str, Any]]:
    """某个周期内已存在的判定，按指纹索引，供接口返回给页面回显。

    页面要能看出「这条我已经判过了」，否则每次打开都得重新判断一遍，
    也就没人愿意持续用这个功能。
    """
    conditions = [
        DashboardFeedback.period_start == start,
        DashboardFeedback.period_end == end,
    ]
    if reviewer:
        conditions.append(DashboardFeedback.reviewer == reviewer)
    rows = session.scalars(select(DashboardFeedback).where(*conditions)).all()
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        entries = index.setdefault(row.signal_key, {})
        # 同一结论多人判定时，最后写入的一条作为展示值；完整数据在统计接口里。
        entries[row.reviewer] = feedback_entry(row)
    return index


def _rate(counts: Counter[str]) -> dict[str, Any]:
    """把判定计数折算成准确率，样本不足时不给百分比。"""
    correct = int(counts.get("correct", 0))
    wrong = int(counts.get("wrong", 0))
    unclear = int(counts.get("unclear", 0))
    # 「说不清」不进分母：它是「证据不足以判断」，既不是对也不是错。
    decided = correct + wrong
    return {
        "correct": correct,
        "wrong": wrong,
        "unclear": unclear,
        "decided": decided,
        "total": correct + wrong + unclear,
        "accuracy": round(correct / decided * 100, 1) if decided else None,
        # 样本不足时不报百分比，但如实说明还差多少 —— 让人知道该继续判几条。
        "enough_sample": decided >= MIN_SAMPLE_FOR_RATE,
        "min_sample": MIN_SAMPLE_FOR_RATE,
    }


def feedback_summary(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    reviewer: str | None = None,
) -> dict[str, Any]:
    """按模块汇总人工判定的准确率。

    这是「这套看板准不准」唯一诚实的答案来源：它来自人工核对，不来自代码自证。
    """
    conditions = []
    if start is not None:
        conditions.append(DashboardFeedback.period_start == start)
    if end is not None:
        conditions.append(DashboardFeedback.period_end == end)
    if reviewer:
        conditions.append(DashboardFeedback.reviewer == reviewer)
    query = (
        select(DashboardFeedback.module, DashboardFeedback.verdict, func.count(DashboardFeedback.id))
        .group_by(DashboardFeedback.module, DashboardFeedback.verdict)
    )
    if conditions:
        query = query.where(*conditions)
    rows = session.execute(query).all()

    by_module: dict[str, Counter[str]] = {module: Counter() for module in FEEDBACK_MODULES}
    for module, verdict, count in rows:
        by_module.setdefault(module, Counter())[verdict] += int(count)
    modules = {module: _rate(counts) for module, counts in by_module.items()}

    overall = Counter()
    for counts in by_module.values():
        overall.update(counts)
    # 未判定的结论条数一并给出：只报准确率会让人以为「已经判完了」，
    # 而真正决定这份报表可信度的是「覆盖率」——判过的除以全部结论。
    return {
        "period": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        "modules": modules,
        "overall": _rate(overall),
        "verdict_labels": VERDICT_LABELS,
    }


def pending_keys(
    session: Session, *, start: date, end: date, module: str | None = None
) -> set[str]:
    """该周期已判定过的结论键（不含 reviewer 过滤），用于算未判定数量。"""
    conditions = [
        DashboardFeedback.period_start == start,
        DashboardFeedback.period_end == end,
    ]
    if module:
        conditions.append(DashboardFeedback.module == module)
    rows = session.scalars(select(DashboardFeedback.signal_key).where(*conditions)).all()
    return set(rows)


def feedback_for_keys(
    session: Session, *, start: date, end: date, keys: list[str]
) -> dict[str, dict[str, Any]]:
    """按指纹批量取判定（一次查询，不随结论数增长）。"""
    if not keys:
        return {}
    rows = session.scalars(
        select(DashboardFeedback).where(
            DashboardFeedback.period_start == start,
            DashboardFeedback.period_end == end,
            DashboardFeedback.signal_key.in_(keys),
        )
    ).all()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result.setdefault(row.signal_key, {})[row.reviewer] = feedback_entry(row)
    return result


__all__ = [
    "FEEDBACK_MODULES",
    "MIN_SAMPLE_FOR_RATE",
    "VERDICTS",
    "VERDICT_LABELS",
    "build_signal_key",
    "feedback_entry",
    "feedback_for_keys",
    "feedback_index",
    "feedback_summary",
    "pending_keys",
    "record_feedback",
]
