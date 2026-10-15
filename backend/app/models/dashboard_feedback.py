"""经营看板结论的人工效度判定。

这是**唯一能回答「结论对不对」的数据**。看板的自动化测试只能证明「代码按规则跑」，
证明不了「规则符合现实」；实测过的三类错误（同一任务被算成两条主线、「状态不一致」
把两件不同的事判成矛盾、空资源需求占掉首屏名额）在测试里全绿，因为它们测的是
规则执行而不是规则对错。要让准确率可见，就必须有人对每条结论表态。

设计取舍：

* **只增不改**：同一条结论的判定可以随时间改变（本月说「不准」、下月说「准确」），
  但历史判定保留 —— 那正是「我们改了什么、效果如何」的证据。
* **指纹不含结论内容**：``signal_key`` 由模块、周期与结论的业务键组成（例如
  「瓶颈 + 2026-09-14~2026-09-20 + 办公室确认与发布」）。它必须**跨重算稳定**，
  否则算法一改指纹就变，之前攒的判定全部对不上号，等于白攒。
* **按人一条**：同一结论可以由多人分别判定（老板与部门负责人看法可能不同），
  所以唯一约束是 (指纹, 判定人)，不是单条结论只能判一次。
* **不写入结论本身**：``title`` / ``conclusion`` 只作快照供报表展示，避免周期
  太久后算不出来只能显示一串 ID。它们不参与判定逻辑。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class DashboardFeedback(Base, TimestampMixin):
    """一条结论（信号 / 瓶颈 / 摩擦 / 项目）的人工判定。"""

    __tablename__ = "dashboard_feedback"
    __table_args__ = (
        # 同一个人对同一条结论只保留一条判定：改主意是更新，不是新增。
        UniqueConstraint("signal_key", "reviewer", name="uq_dashboard_feedback_signal_reviewer"),
        # 准确率报表按「模块 + 周期」聚合，这是它的主查询路径。
        Index("ix_dashboard_feedback_module_period", "module", "period_start", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 模块：attention（今天值得关注）/ bottleneck / duplicate / conflict / project。
    module: Mapped[str] = mapped_column(String(32), nullable=False)
    # 结论的业务键，与 module 组合后唯一标识一条结论。跨重算稳定。
    signal_key: Mapped[str] = mapped_column(String(500), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    # 结论快照，仅供报表展示。
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 判定：correct（说得对）/ wrong（说错了）/ unclear（说不清，例如证据不足）。
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    # 判错时的一句话原因。可选，但它是改进算法最有价值的输入。
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 判定人。访问密码登录时固定为 admin；企微登录时为员工标识。
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)


__all__ = ["DashboardFeedback"]
