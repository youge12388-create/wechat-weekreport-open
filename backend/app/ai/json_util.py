"""模型输出的容错 JSON 解析。

现实：即便提示词写了「只输出 JSON」，模型仍可能带 ```json 代码块、
前后寒暄、或多个 JSON 片段。这里做逐级降级的解析，全部失败才抛错，
让上层能明确区分「模型答了但格式不对」和「模型根本没答」。
"""

from __future__ import annotations

import json
import re

from app.ai.base import LLMResponseError

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _strip_fences(text: str) -> str:
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _balanced_slice(text: str) -> str | None:
    """从第一个 { 开始，按括号配平且跳过字符串字面量，截取一个完整对象。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def extract_json_object(text: str) -> dict:
    """从模型输出里尽力抠出一个 JSON 对象。"""
    if not text or not text.strip():
        raise LLMResponseError("模型返回为空")

    candidates = [text.strip(), _strip_fences(text)]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue

    sliced = _balanced_slice(_strip_fences(text))
    if sliced:
        try:
            parsed = json.loads(sliced)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMResponseError(f"模型输出不是合法 JSON：{text[:300]}") from exc

    raise LLMResponseError(f"模型输出中未找到 JSON 对象：{text[:300]}")
