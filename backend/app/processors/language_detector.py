"""本地周报语言粗分类。

只服务于“仅英文周报”筛选：不识别员工国籍、不调用外部服务，也不决定翻译。
混合内容宁可标为 other，避免把中英文混写误筛成英文周报。
"""

from __future__ import annotations

import re

_LATIN = re.compile(r"[A-Za-z]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def detect_content_language(text: str | None) -> str:
    value = (text or "").strip()
    latin_count = len(_LATIN.findall(value))
    cjk_count = len(_CJK.findall(value))
    letters = latin_count + cjk_count
    if letters < 3:
        return "unknown"
    # 标点、数字与链接不参与分母；至少 85% 的中英文字符为拉丁字母，
    # 且中文占比极低，才算英文正文。
    if latin_count / letters >= 0.85 and cjk_count / letters <= 0.1:
        return "english"
    if cjk_count / letters >= 0.6:
        return "chinese"
    return "other"
