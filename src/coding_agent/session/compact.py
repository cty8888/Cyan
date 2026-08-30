"""把较早的对话区间压成 SummaryMessage，并删掉对应的 tool_history。"""

from __future__ import annotations

import json
from typing import Callable

from ..core.prompts import COMPACT_SYSTEM_PROMPT
from ..errors import LLMError
from ..llm.types import (
    AssistantMessage,
    LLMResponse,
    Message,
    SummaryMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from ..settings.compact import CompactPolicy
from ..settings.tools import DEFAULT_TOOL_RESULT_CHARS
from .session import Session
from .types import ToolHistory

CallLLM = Callable[[list[dict], list[dict] | None], LLMResponse]


def compact_threshold(policy: CompactPolicy) -> int:
    """触发压缩的 prompt token 下限。"""
    usable = max(0, policy.max_context_tokens - policy.reserve_tokens)
    return int(usable * policy.trigger_ratio)


def _is_real_user(message: Message) -> bool:
    """真正的用户任务，不含压缩摘要（SummaryMessage 的 role 也是 user）。"""
    return isinstance(message, UserMessage) and not isinstance(message, SummaryMessage)


def find_keep_from(messages: list[Message], keep_recent_turns: int) -> int | None:
    """保留段起点：倒数第 ``keep_recent_turns`` 条 AssistantMessage。

    切点落在 Assistant（或其紧前的 User）上，不拆 tool_call 对。
    同一条用户任务里只要模型轮次多于保留数，就能压掉更早的工具轮。
    若当前问题还没有 assistant 回复，则退回按用户任务切，兼容多轮交互。
    """
    start = 1 if messages and isinstance(messages[0], SystemMessage) else 0
    assistant_indices = [
        index
        for index, message in enumerate(messages)
        if index >= start and isinstance(message, AssistantMessage)
    ]
    user_indices = [
        index
        for index, message in enumerate(messages)
        if index >= start and _is_real_user(message)
    ]

    if len(assistant_indices) > keep_recent_turns:
        keep_from = assistant_indices[-keep_recent_turns]
        if keep_from > start and _is_real_user(messages[keep_from - 1]):
            keep_from -= 1
        return keep_from if keep_from > start else None

    if user_indices and len(user_indices) > keep_recent_turns:
        last_user = user_indices[-1]
        if not any(index > last_user for index in assistant_indices):
            keep_from = user_indices[-keep_recent_turns]
            return keep_from if keep_from > start else None
    return None


def needs_compact(
    session: Session,
    policy: CompactPolicy,
    *,
    estimated_tokens: int | None = None,
) -> bool:
    """是否既有可压缩区间，又达到 token 阈值。

    主判断是「当前整体有多大」：优先用调用方量过的即将出门的 wire
    （``estimated_tokens``），否则粗估 Session。上一轮 API 回报的
    ``last_prompt_tokens`` 只作补充——那次已经超阈值，这轮出门前先压。
    """
    if find_keep_from(session.messages, policy.keep_recent_turns) is None:
        return False
    threshold = compact_threshold(policy)
    current = estimated_tokens if estimated_tokens is not None else estimate_session_tokens(session)
    if current >= threshold:
        return True
    return session.usage.last_prompt_tokens >= threshold


def estimate_payload_tokens(payloads: list[dict]) -> int:
    """用 JSON 字符数 / 4 粗估一组 API payload 的 token 数。"""
    return max(0, len(json.dumps(payloads, ensure_ascii=False)) // 4)


def estimate_session_tokens(session: Session) -> int:
    """粗估当前会话体积（工具正文不截断）。Loop 应改用量过组窗后的 wire。"""
    payloads = [_message_to_wire(message, session.tool_history) for message in session.messages]
    return estimate_payload_tokens(payloads)


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
    """用 SummaryMessage 替换被压缩段，并删除对应 tool_history。

    切在当前任务内部时，用户那句话会落在被压段里；摘要请求需要它，
    会话里也必须留下，否则模型丢掉当前任务原文。
    """
    head: list[Message] = [session.messages[0]] if has_system else []
    dropped_start = 1 if has_system else 0
    dropped = session.messages[dropped_start:keep_from]
    preserved_user = _user_to_preserve(session.messages, dropped_start, keep_from)
    call_ids = _tool_call_ids(dropped)
    kept: list[Message] = [*head, SummaryMessage.of(summary)]
    if preserved_user is not None:
        kept.append(preserved_user)
    kept.extend(session.messages[keep_from:])
    session.messages[:] = kept
    for call_id in call_ids:
        session.tool_history.remove(call_id)
    session.metadata.touch()


def _user_to_preserve(messages: list[Message], dropped_start: int, keep_from: int) -> UserMessage | None:
    """被压段若含当前用户任务，返回那条 UserMessage，供写回保留段。"""
    last_index: int | None = None
    last_message: UserMessage | None = None
    for index, message in enumerate(messages):
        if _is_real_user(message):
            last_index = index
            last_message = message
    if last_index is not None and dropped_start <= last_index < keep_from:
        return last_message
    return None


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
    """摘要请求带上工具正文，但按组窗同一上限截尾，避免总结那次自己先超窗。"""
    block = message.tool_result
    if block is None:
        return ""
    execution = tool_history.get(block.tool_call_id)
    if execution is None:
        return ""
    if execution.result is not None and execution.result.content:
        text = execution.result.content
    else:
        text = execution.error or ""
    return _truncate_tool_text(text, DEFAULT_TOOL_RESULT_CHARS)


def _truncate_tool_text(text: str, limit: int) -> str:
    """与组窗同一规则：超限留开头。``limit <= 0`` 表示不截。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
