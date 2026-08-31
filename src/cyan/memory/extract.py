"""任务成功结束后的一次记忆提取（不走 Agent Loop）。"""

from __future__ import annotations

from typing import Callable

from ..errors import InvalidToolArgumentsError
from ..llm.parser import parse_tool_arguments
from ..llm.types import (
    AssistantMessage,
    ContinueMessage,
    Message,
    SummaryMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from ..session.session import Session
from ..session.types import ToolHistory
from ..settings.tools import DEFAULT_TOOL_RESULT_CHARS
from .settings import auto_memory_enabled
from .store import load_memory_index_layer, write_entry
from .types import MemoryEntry, MemoryKind

CallLLM = Callable[[list[dict], list[dict] | None], object]

EXTRACT_SYSTEM_PROMPT = """你在为同一个编程助手提取「值得跨会话记住」的笔记。只根据随后的对话，不要编造。

只记录：用户纠正、用户明确要求记住的偏好、代码/git/cyan.md 里看不到的项目事实、仓库外入口。
不要记录：能从代码看出来的架构、一次性任务细节、当前 diff、API Key / .env / 密钥、对话摘要。
若条目已出现在「已有索引」或「项目 cyan.md」里，不要再写。

只输出一个 JSON 对象，不要 Markdown 围栏，不要其它文字。格式：
{"entries": [{"kind": "user|feedback|project|reference", "summary": "一行摘要", "detail": "可选细节"}]}
没有值得记住的内容时输出：{"entries": []}
"""


def persist_auto_memory(session: Session, call_llm: CallLLM) -> int:
    """COMPLETED 任务结束后提取并写入。返回新写入条数；LLM 失败向上抛，由 Loop 提示。"""
    if not auto_memory_enabled():
        return 0
    payloads = [
        {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": _extract_user_payload(session)},
    ]
    response = call_llm(payloads, None)
    text = ""
    message = getattr(response, "message", None)
    if isinstance(message, AssistantMessage):
        text = message.text or ""
    written = 0
    for entry in _parse_entries(text):
        if write_entry(session.workspace.root, entry):
            written += 1
    return written


def _extract_user_payload(session: Session) -> str:
    parts = [
        f"# 当前任务\n{session.state.current_task or ''}",
        _cyan_excerpt(session.workspace.root),
        _index_excerpt(session.workspace.root),
        "# 本任务对话",
        _conversation_excerpt(_current_task_messages(session.messages), session.tool_history),
    ]
    return "\n\n".join(part for part in parts if part.strip())


def _cyan_excerpt(workspace) -> str:
    from ..prompt.files import project_instruction_path

    path = project_instruction_path(workspace)
    if not path.is_file():
        return "# 项目 cyan.md\n（无）"
    try:
        text = path.read_text(encoding="utf-8").strip() or "（空）"
    except OSError:
        text = "（无法读取）"
    if len(text) > 4000:
        text = text[:4000] + "...[truncated]"
    return f"# 项目 cyan.md\n{text}"


def _index_excerpt(workspace) -> str:
    layer = load_memory_index_layer(workspace)
    if layer is None:
        return "# 已有索引\n（无）"
    return f"# 已有索引\n{layer.text}"


def _is_task_user(message: Message) -> bool:
    return isinstance(message, UserMessage) and not isinstance(
        message, (SummaryMessage, ContinueMessage)
    )


def _current_task_messages(messages: list[Message]) -> list[Message]:
    """只保留最近一条用户任务及其后的对话，避免把旧任务写进记忆。"""
    start = 0
    for index, message in enumerate(messages):
        if _is_task_user(message):
            start = index
    return messages[start:]


def _conversation_excerpt(messages: list[Message], tool_history: ToolHistory) -> str:
    chunks: list[str] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, ToolMessage):
            block = message.tool_result
            call_id = block.tool_call_id if block else ""
            execution = tool_history.get(call_id)
            body = ""
            if execution is not None and execution.result is not None:
                body = execution.result.content or ""
            if len(body) > 800:
                body = body[:800] + "...[truncated]"
            chunks.append(f"tool: {body}")
            continue
        role = getattr(message.role, "value", "user")
        text = (message.text or "").strip()
        if not text and not getattr(message, "tool_calls", None):
            continue
        if getattr(message, "tool_calls", None):
            names = ", ".join(call.name for call in message.tool_calls)
            extra = f" [tools: {names}]" if names else ""
            chunks.append(f"{role}: {text}{extra}".strip())
        else:
            chunks.append(f"{role}: {text}")
    blob = "\n".join(chunks)
    if len(blob) > DEFAULT_TOOL_RESULT_CHARS:
        blob = blob[:DEFAULT_TOOL_RESULT_CHARS] + "...[truncated]"
    return blob


def _parse_entries(text: str) -> list[MemoryEntry]:
    try:
        data = parse_tool_arguments(text)
    except InvalidToolArgumentsError:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("entries") or []
    if not isinstance(items, list):
        return []
    entries: list[MemoryEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind_raw = str(item.get("kind") or "").strip().lower()
        try:
            kind = MemoryKind(kind_raw)
        except ValueError:
            continue
        summary = str(item.get("summary") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if not summary:
            continue
        entries.append(MemoryEntry(kind=kind, summary=summary, detail=detail))
    return entries
