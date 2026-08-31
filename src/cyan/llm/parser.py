"""模型输出解析。

两件事：

1. 把厂商 SDK 的响应对象转换成内部 ``LLMResponse``。
2. 容错解析工具参数——模型偶尔会把 JSON 包在 markdown 代码块里、用单引号、或留下尾随逗号，
   这里逐级降级修复；实在无法解析才抛出 ``InvalidToolArgumentsError`` 让模型自己重试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import InvalidToolArgumentsError, LLMResponseError
from .types import AssistantMessage, LLMResponse, StreamChunk, ToolCallBlock, Usage

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

    tool_calls: list[ToolCallBlock] = []
    for index, item in enumerate(getattr(message, "tool_calls", None) or []):
        function = getattr(item, "function", None)
        if function is None:
            continue
        tool_calls.append(
            ToolCallBlock(
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
        message=AssistantMessage.of(
            text=getattr(message, "content", None),
            tool_calls=tool_calls,
        ),
        finish_reason=getattr(choice, "finish_reason", "stop") or "stop",
        usage=usage,
    )


@dataclass
class _ToolCallBuffer:
    """按 ``delta.tool_calls[].index`` 分桶累积的一次工具调用分片。"""

    id: str | None = None
    name: str = ""
    arguments: str = ""


@dataclass
class _StreamState:
    text_parts: list[str] = field(default_factory=list)
    tool_calls: dict[int, _ToolCallBuffer] = field(default_factory=dict)
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)


class StreamAssembler:
    """累积一次流式补全的分片，最终装配成与 ``parse_completion`` 同构的 ``LLMResponse``。

    OpenAI 兼容的流式协议里，``tool_calls`` 的 ``name``/``arguments`` 会分好几个
    分片逐步补全（``id`` 与 ``name`` 通常只在该 tool_call 的第一个分片给出），
    因此按 ``index`` 分桶累加，直到流结束再拼成完整 JSON 字符串交给
    ``parse_tool_arguments`` 解析。
    """

    def __init__(self) -> None:
        self._state = _StreamState()

    def feed(self, raw_chunk: Any) -> StreamChunk | None:
        """吃一个 SSE 分片，返回它贡献的增量（没有就返回 None）。

        文本分片和 tool_call 分片不会同时出现在同一个 ``StreamChunk`` 里——
        OpenAI 兼容协议里一次 delta 只携带其中一种。
        """
        choices = getattr(raw_chunk, "choices", None) or []
        result: StreamChunk | None = None
        if choices:
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if content:
                    self._state.text_parts.append(content)
                    result = StreamChunk(text_delta=content)
                for tool_call in getattr(delta, "tool_calls", None) or []:
                    result = self._merge_tool_call(tool_call)
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason:
                self._state.finish_reason = finish_reason

        usage_raw = getattr(raw_chunk, "usage", None)
        if usage_raw is not None:
            self._state.usage = Usage(
                prompt_tokens=getattr(usage_raw, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage_raw, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage_raw, "total_tokens", 0) or 0,
            )
        return result

    def _merge_tool_call(self, tool_call: Any) -> StreamChunk:
        index = getattr(tool_call, "index", 0) or 0
        buffer = self._state.tool_calls.setdefault(index, _ToolCallBuffer())
        call_id = getattr(tool_call, "id", None)
        if call_id:
            buffer.id = call_id
        name_delta = ""
        arguments_delta = ""
        function = getattr(tool_call, "function", None)
        if function is not None:
            name = getattr(function, "name", None)
            if name:
                buffer.name += name
                name_delta = name
            arguments = getattr(function, "arguments", None)
            if arguments:
                buffer.arguments += arguments
                arguments_delta = arguments
        return StreamChunk(
            tool_call_index=index,
            tool_call_id=call_id,
            tool_call_name=name_delta or None,
            tool_call_arguments_delta=arguments_delta,
        )

    def finalize(self) -> LLMResponse:
        """流结束后调用，装配完整响应；可以在没有任何分片时调用（返回空回复）。"""
        tool_calls = [
            ToolCallBlock(
                id=buffer.id or f"call_{index}",
                name=buffer.name,
                arguments=buffer.arguments,
            )
            for index, buffer in sorted(self._state.tool_calls.items())
        ]
        text = "".join(self._state.text_parts) or None
        return LLMResponse(
            message=AssistantMessage.of(text=text, tool_calls=tool_calls),
            finish_reason=self._state.finish_reason,
            usage=self._state.usage,
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
