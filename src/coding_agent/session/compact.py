"""把较早的对话区间压成 SummaryMessage，并删掉对应的 tool_history。"""

from __future__ import annotations

import json
from typing import Callable

from ..core.prompts import COMPACT_SYSTEM_PROMPT
from ..errors import LLMError
from ..llm.types import (
    LLMResponse,
    Message,
    SummaryMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from ..settings.compact import CompactPolicy
from .session import Session
from .types import ToolHistory

CallLLM = Callable[[list[dict], list[dict] | None], LLMResponse]


def compact_threshold(policy: CompactPolicy) -> int:
    """触发压缩的 prompt token 下限。"""
    usable = max(0, policy.max_context_tokens - policy.reserve_tokens)
    return int(usable * policy.trigger_ratio)


def find_keep_from(messages: list[Message], keep_recent_turns: int) -> int | None:
    """保留段起点：倒数第 ``keep_recent_turns`` 条真正的 UserMessage。

    切点落在 User 下标上，避免拆开 tool_call 对。没有可压缩区间时返回 ``None``。
    """
    start = 1 if messages and isinstance(messages[0], SystemMessage) else 0
    user_indices = [
        index
        for index, message in enumerate(messages)
        if index >= start and isinstance(message, UserMessage) and not isinstance(message, SummaryMessage)
    ]
    if len(user_indices) <= keep_recent_turns:
        return None
    keep_from = user_indices[-keep_recent_turns]
    if keep_from <= start:
        return None
    return keep_from


def needs_compact(session: Session, policy: CompactPolicy) -> bool:
    """是否既有可压缩区间，又达到 token 阈值。"""
    if find_keep_from(session.messages, policy.keep_recent_turns) is None:
        return False
    threshold = compact_threshold(policy)
    if session.usage.last_prompt_tokens >= threshold:
        return True
    if session.usage.last_prompt_tokens == 0:
        return estimate_session_tokens(session) >= threshold
    return False


def estimate_session_tokens(session: Session) -> int:
    """用 JSON 字符数 / 4 粗估当前会话体积，供尚无 API usage 时预判。"""
    payloads = [_message_to_wire(message, session.tool_history) for message in session.messages]
    return max(0, len(json.dumps(payloads, ensure_ascii=False)) // 4)


def try_compact(session: Session, call_llm: CallLLM, policy: CompactPolicy) -> bool:
    """压缩被压缩段。成功返回 True；无可切区间、空回复或 LLMError 返回 False，Session 不动。"""
    keep_from = find_keep_from(session.messages, policy.keep_recent_turns)
    if keep_from is None:
        return False

    has_system = bool(session.messages) and isinstance(session.messages[0], SystemMessage)
    dropped_start = 1 if has_system else 0
    dropped = session.messages[dropped_start:keep_from]
    if not dropped:
        return False

    payloads = [{"role": "system", "content": COMPACT_SYSTEM_PROMPT}]
    payloads.extend(_message_to_wire(message, session.tool_history) for message in dropped)

    try:
        response = call_llm(payloads, None)
    except LLMError:
        return False

    session.record_usage(response.usage, for_trigger=False)
    text = (response.message.text or "").strip()
    if not text:
        return False

    _apply_compact(session, keep_from, text, has_system=has_system)
    session.usage.last_prompt_tokens = 0
    return True


def _apply_compact(session: Session, keep_from: int, summary: str, *, has_system: bool) -> None:
    """用 SummaryMessage 替换被压缩段，并删除对应 tool_history。"""
    head: list[Message] = [session.messages[0]] if has_system else []
    dropped_start = 1 if has_system else 0
    dropped = session.messages[dropped_start:keep_from]
    call_ids = _tool_call_ids(dropped)
    session.messages[:] = [*head, SummaryMessage.of(summary), *session.messages[keep_from:]]
    for call_id in call_ids:
        session.tool_history.remove(call_id)
    session.metadata.touch()


def _tool_call_ids(messages: list[Message]) -> list[str]:
    ids: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        block = message.tool_result
        if block and block.tool_call_id:
            ids.append(block.tool_call_id)
    return ids


def _message_to_wire(message: Message, tool_history: ToolHistory) -> dict:
    if isinstance(message, ToolMessage):
        return message.to_api(content=_tool_text(tool_history, message))
    return message.to_api()


def _tool_text(tool_history: ToolHistory, message: ToolMessage) -> str:
    """摘要请求里必须带上工具全文；此时 history 还不能删。"""
    block = message.tool_result
    if block is None:
        return ""
    execution = tool_history.get(block.tool_call_id)
    if execution is None:
        return ""
    if execution.result is not None:
        rendered = execution.result.render("full")
        if rendered:
            return rendered
    return execution.error or ""
