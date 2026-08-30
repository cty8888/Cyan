"""把较早的对话区间压成 SummaryMessage，事件表保留原文。"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core.prompts import COMPACT_SYSTEM_PROMPT
from ..errors import LLMError
from ..llm.parser import parse_tool_arguments
from ..llm.types import (
    AssistantMessage,
    ContinueMessage,
    LLMResponse,
    Message,
    SummaryMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from ..settings.compact import CompactPolicy
from ..settings.tools import DEFAULT_TOOL_RESULT_CHARS
from .events import (
    COMPACT,
    COMPACT_REASON_AUTO,
    COMPACT_REASON_EMERGENCY,
    SUMMARY,
)
from .session import Session
from .types import ToolHistory
from .view import rebuild_messages

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

    ``keep_recent_turns <= 0`` 是紧急切点：压掉 system 之后的全部历史，
    当前用户任务由 ``_apply_compact`` 再插回。``[-0]`` 在 Python 里等于
    ``[0]``，不能套用「倒数第 N 条」公式。
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

    if keep_recent_turns <= 0:
        if assistant_indices:
            return len(messages)
        if any(_is_oversized_user(messages[index]) for index in user_indices):
            return len(messages)
        return None

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


def resolve_keep_from(
    messages: list[Message],
    keep_recent_turns: int,
    *,
    max_keep: int | None = None,
) -> int | None:
    """选出一个切点：优先保留更多轮，必要时降到 1 轮乃至全部压进摘要。

    ``max_keep`` 限制最多保留几轮（溢出重试传 0，强制紧急压缩）。
    """
    preferred = keep_recent_turns if max_keep is None else min(keep_recent_turns, max_keep)
    for keep in range(preferred, -1, -1):
        cut = find_keep_from(messages, keep)
        if cut is not None:
            return cut
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
    if resolve_keep_from(session.messages, policy.keep_recent_turns) is None:
        return False
    threshold = compact_threshold(policy)
    current = estimated_tokens if estimated_tokens is not None else estimate_session_tokens(session)
    if current >= threshold:
        return True
    return session.usage.last_prompt_tokens >= threshold


def estimate_payload_tokens(payloads: list[dict]) -> int:
    """粗估一组 API payload 的 token 数。

    ASCII 按 4 字符 ≈ 1 token；中日韩等按 1 字符 ≈ 1 token，避免 chars/4 把中文估低。
    """
    return _estimate_text_tokens(json.dumps(payloads, ensure_ascii=False))


_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    return max(0, cjk + (len(text) - cjk) // 4)


def estimate_session_tokens(session: Session) -> int:
    """粗估当前会话体积（工具正文不截断）。Loop 应改用量过组窗后的 wire。"""
    payloads = [_message_to_wire(message, session.tool_history) for message in session.messages]
    return estimate_payload_tokens(payloads)


def try_compact(
    session: Session,
    call_llm: CallLLM,
    policy: CompactPolicy,
    *,
    max_keep: int | None = None,
    keep_from: int | None = None,
    drop_end: int | None = None,
    reason: str | None = None,
) -> bool:
    """压缩被压缩段。成功返回 True；无可切区间、空回复或 LLMError 返回 False，事件表仍在。

    ``max_keep`` 限制最多保留几轮；溢出恢复传 0，把能压的历史全部收成摘要。
    ``keep_from`` / ``drop_end`` 指定视图半开区间 ``[dropped_start, keep_from)``；
    ``drop_end`` 若给出则为闭区间终点（summarize-from 用）。
    """
    has_system = bool(session.messages) and isinstance(session.messages[0], SystemMessage)
    dropped_start = 1 if has_system else 0
    if keep_from is None:
        keep_from = resolve_keep_from(session.messages, policy.keep_recent_turns, max_keep=max_keep)
    if keep_from is None:
        return False
    if drop_end is not None:
        dropped = session.messages[keep_from : drop_end + 1]
        preserve_user = False
    else:
        dropped = session.messages[dropped_start:keep_from]
        preserve_user = True
    if not dropped:
        return False

    tool_limit = _compact_tool_char_limit(dropped, policy)
    payloads = [{"role": "system", "content": COMPACT_SYSTEM_PROMPT}]
    payloads.extend(
        _message_to_wire(message, session.tool_history, tool_limit) for message in dropped
    )

    try:
        response = call_llm(payloads, None)
    except LLMError:
        return False

    session.record_usage(response.usage, for_trigger=False)
    text = (response.message.text or "").strip()
    if not text:
        return False

    if reason is None:
        reason = COMPACT_REASON_EMERGENCY if max_keep is not None and max_keep <= 0 else COMPACT_REASON_AUTO
    _apply_compact(session, dropped, text, reason=reason, preserve_current_user=preserve_user)
    session.usage.last_prompt_tokens = 0
    session.persist_head()
    return True


def _apply_compact(
    session: Session,
    dropped: list[Message],
    summary: str,
    *,
    reason: str,
    preserve_current_user: bool = True,
) -> None:
    """往事件表 append summary + compact，再按 overlay 重建视图。不删事件原文。"""
    preserved: UserMessage | None = None
    if preserve_current_user:
        last: UserMessage | None = None
        for message in session.messages:
            if _is_real_user(message):
                last = message
        if last is not None and any(message is last for message in dropped):
            preserved = last

    oversized = preserved is not None and _is_oversized_user(preserved)
    keep: set[int] = set()
    if preserve_current_user:
        keep.update(id(message) for message in _trailing_continues(session.messages))
        if preserved is not None and not oversized:
            keep.add(id(preserved))
    hide = [message for message in dropped if id(message) not in keep]
    call_ids = _tool_call_ids(hide)
    hidden_ids = [message.id for message in hide if message.id]
    if not hidden_ids:
        return
    placement = _placement_id(session, dropped[0])
    end_id = next((message.id for message in reversed(hide) if message.id), hidden_ids[-1])
    text = _truncate_tool_text(summary, DEFAULT_TOOL_RESULT_CHARS)
    summary_payload: dict[str, Any] = {"text": text}
    if oversized and preserved is not None:
        truncated = _truncate_preserved_user(preserved)
        summary_payload["preserved_user_text"] = truncated.text or ""
        summary_payload["preserved_user_event_id"] = preserved.id
    summary_event = session.append_event(SUMMARY, summary_payload)
    session.append_event(
        COMPACT,
        {
            "start_event_id": placement,
            "end_event_id": end_id,
            "summary_event_id": summary_event.id,
            "hidden_event_ids": hidden_ids,
            "reason": reason,
        },
    )
    _forget_dropped_reads(session, call_ids)
    rebuild_messages(session)


def _placement_id(session: Session, message: Message) -> str:
    """摘要插入点：普通消息用自身 id；已有 Summary 用当初 compact 的 start。"""
    if isinstance(message, SummaryMessage) and message.id:
        for event in session.events:
            if event.type == COMPACT and event.payload.get("summary_event_id") == message.id:
                start = event.payload.get("start_event_id")
                if start:
                    return str(start)
    return message.id or ""


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


def _trailing_continues(messages: list[Message]) -> list[ContinueMessage]:
    """最后一条真实用户任务之后的续写指令，紧急压缩时也要留给模型。"""
    last_user = -1
    for index, message in enumerate(messages):
        if _is_real_user(message):
            last_user = index
    if last_user < 0:
        return []
    return [
        message
        for message in messages[last_user + 1 :]
        if isinstance(message, ContinueMessage)
    ]


def _is_oversized_user(message: Message, limit: int = DEFAULT_TOOL_RESULT_CHARS) -> bool:
    """首条用户粘贴就能撑爆窗口：没有 Assistant 时也要能压。

    已经按上限截过的副本不再当成 oversized，避免每轮紧急压缩。
    """
    if not _is_real_user(message):
        return False
    text = message.text or ""
    if text.endswith(_TRUNCATE_MARKER):
        return False
    return len(text) > limit


def _truncate_preserved_user(message: UserMessage, limit: int = DEFAULT_TOOL_RESULT_CHARS) -> UserMessage:
    """紧急压缩后若把原文整段插回，窗口还是满的。超限只留开头。"""
    text = message.text or ""
    if len(text) <= limit:
        return message
    return UserMessage.of(_truncate_tool_text(text, limit))


def _forget_dropped_reads(session: Session, call_ids: list[str]) -> None:
    """被压掉的读写结果不再在上下文里，对应文件的已读标记作废。"""
    from ..security.paths import resolve_path

    for call_id in call_ids:
        execution = session.tool_history.get(call_id)
        if execution is None or execution.tool_name not in {"read_file", "write_file", "edit_file"}:
            continue
        try:
            args = parse_tool_arguments(execution.arguments or "{}", execution.tool_name)
            raw = args.get("path")
            if not raw:
                continue
            session.unmark_read(resolve_path(session.workspace.root, str(raw)))
        except Exception:
            continue


def _tool_call_ids(messages: list[Message]) -> list[str]:
    ids: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        block = message.tool_result
        if block and block.tool_call_id:
            ids.append(block.tool_call_id)
    return ids


def _compact_tool_char_limit(dropped: list[Message], policy: CompactPolicy) -> int:
    """摘要请求的单条工具正文上限：按即将出门的窗口均分，避免总结那次自己先超窗。"""
    tool_count = sum(1 for message in dropped if isinstance(message, ToolMessage))
    if tool_count <= 0:
        return DEFAULT_TOOL_RESULT_CHARS
    budget_chars = max(0, policy.max_context_tokens - policy.reserve_tokens) * 4
    per_tool = budget_chars // (tool_count * 2)
    return max(1_000, min(DEFAULT_TOOL_RESULT_CHARS, per_tool))


def _message_to_wire(
    message: Message, tool_history: ToolHistory, tool_limit: int = DEFAULT_TOOL_RESULT_CHARS
) -> dict:
    if isinstance(message, ToolMessage):
        return message.to_api(content=_tool_text(tool_history, message, tool_limit))
    payload = message.to_api()
    content = payload.get("content")
    if isinstance(content, str):
        payload["content"] = _truncate_tool_text(content, tool_limit)
    return payload


def _tool_text(tool_history: ToolHistory, message: ToolMessage, limit: int) -> str:
    """摘要请求带上工具正文，但按上限截尾，避免总结那次自己先超窗。"""
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
    return _truncate_tool_text(text, limit)


_TRUNCATE_MARKER = "...[truncated]"


def _truncate_tool_text(text: str, limit: int) -> str:
    """与组窗同一规则：超限留开头。标记算进上限，截完后长度不超过 ``limit``。"""
    if limit <= 0 or len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATE_MARKER))
    return text[:keep] + _TRUNCATE_MARKER
