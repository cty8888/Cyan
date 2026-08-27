"""模型输出解析。

两件事：

1. 把厂商 SDK 的响应对象转换成内部 ``LLMResponse``。
2. 容错解析工具参数——模型偶尔会把 JSON 包在 markdown 代码块里、用单引号、或留下尾随逗号，
   这里逐级降级修复；实在无法解析才抛出 ``InvalidToolArgumentsError`` 让模型自己重试。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import InvalidToolArgumentsError, LLMResponseError
from .types import LLMResponse, Message, ToolCall, Usage

_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def parse_completion(raw: Any) -> LLMResponse:
    """把 OpenAI 兼容的 ChatCompletion 对象转成内部结构。"""
    choices = getattr(raw, "choices", None)
    if not choices:
        raise LLMResponseError("模型响应中没有 choices 字段")

    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        raise LLMResponseError("模型响应缺少 message 字段")

    tool_calls: list[ToolCall] = []
    for index, item in enumerate(getattr(message, "tool_calls", None) or []):
        function = getattr(item, "function", None)
        if function is None:
            continue
        tool_calls.append(
            ToolCall(
                id=getattr(item, "id", None) or f"call_{index}",
                name=getattr(function, "name", "") or "",
                arguments=getattr(function, "arguments", "") or "",
            )
        )

    usage_raw = getattr(raw, "usage", None)
    usage = Usage(
        prompt_tokens=getattr(usage_raw, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage_raw, "completion_tokens", 0) or 0,
        total_tokens=getattr(usage_raw, "total_tokens", 0) or 0,
    )

    return LLMResponse(
        message=Message.assistant(
            content=getattr(message, "content", None),
            tool_calls=tool_calls,
        ),
        finish_reason=getattr(choice, "finish_reason", "stop") or "stop",
        usage=usage,
    )


def parse_tool_arguments(raw: str, tool_name: str = "") -> dict[str, Any]:
    """把 tool_call 的原始参数字符串解析成 dict，尽最大努力容错。"""
    text = (raw or "").strip()
    if not text:
        return {}

    for candidate in _repair_candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        raise InvalidToolArgumentsError(
            f"工具 {tool_name} 的参数必须是 JSON 对象，实际解析出 {type(parsed).__name__}"
        )

    preview = text if len(text) <= 200 else text[:200] + "..."
    raise InvalidToolArgumentsError(
        f"工具 {tool_name} 的参数不是合法 JSON，请重新以标准 JSON 对象格式调用。原始内容：{preview}"
    )


def _repair_candidates(text: str) -> list[str]:
    """按「越靠前越保守」的顺序给出待尝试的解析文本。"""
    candidates = [text]

    fenced = _CODE_FENCE.match(text)
    if fenced:
        candidates.append(fenced.group(1))

    # 截取最外层花括号，丢弃模型附加的解释性文字
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    # 在已有候选之上再尝试去掉尾随逗号
    candidates.extend(_TRAILING_COMMA.sub(r"\1", c) for c in list(candidates))

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        stripped = candidate.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            unique.append(stripped)
    return unique
