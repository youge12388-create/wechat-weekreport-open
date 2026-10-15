"""周报文本清洗（需求第 7 节）。

规则：
  - 去除明显无意义的空白字符（零宽字符、全角空格、行尾空格等）
  - 统一换行为 \n
  - 压缩连续空行
  - **不修改原始记录**：清洗结果只写入新字段，原文永远保留

清洗是纯函数，无副作用、便于单测。
"""

from __future__ import annotations

import re

# 零宽字符与 BOM
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
# 行尾空白（含全角空格）
_TRAILING_WS = re.compile(r"[ \t\u3000]+$", re.MULTILINE)
# 3 个及以上连续换行
_MULTI_BLANK = re.compile(r"\n{3,}")
# 其他不可见控制字符（保留 \n 与 \t）
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(raw: str | None) -> str:
    """清洗周报正文。None 视为空串。"""
    if not raw:
        return ""

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTROL.sub("", text)
    text = _TRAILING_WS.sub("", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()
