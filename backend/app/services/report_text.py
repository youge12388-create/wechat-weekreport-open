"""交给模型的周报正文取用口径（唯一实现）。

**为什么必须只有一个入口**：`content` 是清洗后的纯文本，`content_raw` 是接口
提取出的未清洗正文（实测多为富文本 HTML）。同一个周期实测 raw 合计 27 万字符、
清洗后仅 4 万 —— 表格样式（`style="width:1496px..."`）本身就能占掉几万字符。
喂 raw 既贵 5 倍 token，又更容易把有效信息淹没在标签里。

此前各模块各取一套：周期总结用清洗后正文，单篇分析与重复检测却优先取
`content_raw`。结果是「装得下却判成装不下」、预算被撑爆、以及「正文没变但
富文本重排」被误判为内容变化而重算。统一到这里，避免再各写一份。
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["cleaned_body", "report_body"]

EMPTY_PLACEHOLDER = "（无正文）"


class _HasBody(Protocol):
    content: str | None
    content_raw: str | None


def cleaned_body(report: _HasBody) -> str:
    """优先用清洗后的 `content`；为空才现场剥离 `content_raw` 的标签。

    回退到 strip_html 是必要的：历史数据或某次清洗异常可能让 `content` 为空，
    直接判定「无正文」会丢数据。
    """
    cleaned = (report.content or "").strip()
    if cleaned:
        return cleaned
    from app.processors.journal_parser import strip_html

    return (strip_html(report.content_raw or "") or "").strip()


def report_body(report: _HasBody, *, placeholder: str = EMPTY_PLACEHOLDER) -> str:
    """提示词里可直接内联的正文（空正文给占位串，而不是空字符串）。"""
    return cleaned_body(report) or placeholder
