"""从事件表重放出 messages / tool_history 视图。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.types import (
    AssistantMessage,
    ContinueMessage,
    SummaryMessage,
    SystemMessage,
    ToolCallBlock,
    ToolMessage,
    UserMessage,
)
from ..security.types import PermissionMode
from .events import (
    ASSISTANT,
    CHECKPOINT,
    COMPACT,
    CONTINUE,
    SESSION_STARTED,
    SUMMARY,
    TOOL_RESULT,
    USER,
    SessionEvent,
)
from .types import SessionUsage, ToolExecution, ToolHistory, ToolResult, ToolResultStatus

if TYPE_CHECKING:
    from .session import Session
    from .store import SessionMeta


def rebuild_messages(session: Session) -> None:
    """按 compact 叠加重放对话视图；不改 cwd / 已读 / 用量（live compact 用）。"""
    messages, history = project_view(session.events)
    session.messages[:] = messages
    session.tool_history = history
    session.metadata.touch()


def project_view(events: list[SessionEvent]) -> tuple[list, ToolHistory]:
    """事件表 → (messages, 未隐藏的 tool_history)。"""
    hidden, placements = _compact_overlays(events)
    emitted_starts: set[str] = set()
    messages: list = []
    history = ToolHistory()

    for event in events:
        if event.id in placements and event.id not in emitted_starts:
            summary_event = placements[event.id]
            if summary_event.id not in hidden:
                messages.append(_summary_message(summary_event))
                preserved = _preserved_user(summary_event)
                if preserved is not None:
                    messages.append(preserved)
            emitted_starts.add(event.id)
        if event.id in hidden:
            continue
        if event.type == SESSION_STARTED:
            messages.append(_system_message(event))
        elif event.type == USER:
            messages.append(_user_message(event))
        elif event.type == CONTINUE:
            messages.append(_continue_message(event))
        elif event.type == ASSISTANT:
            messages.append(_assistant_message(event))
        elif event.type == TOOL_RESULT:
            messages.append(_tool_message(event))
            history.record(_execution_from_result(event))
        elif event.type == SUMMARY:
            # 只通过 placements 插入，避免按文件末尾再出现一次
            continue

    return messages, history


def _compact_overlays(events: list[SessionEvent]) -> tuple[set[str], dict[str, SessionEvent]]:
    """按时间应用 compact：hidden ids，以及 start_event_id → 仍可见的 summary 事件。"""
    by_id = {event.id: event for event in events}
    hidden: set[str] = set()
    placements: dict[str, SessionEvent] = {}
    for event in events:
        if event.type != COMPACT:
            continue
        payload = event.payload
        extra = payload.get("hidden_event_ids") or []
        hidden.update(str(item) for item in extra)
        start = str(payload.get("start_event_id") or "")
        end = str(payload.get("end_event_id") or "")
        if start and end and not extra:
            hidden.update(_ids_between(events, start, end))
        summary_id = str(payload.get("summary_event_id") or "")
        summary_event = by_id.get(summary_id)
        if not start or summary_event is None:
            continue
        if summary_id in hidden:
            current = placements.get(start)
            if current is not None and current.id == summary_id:
                del placements[start]
            continue
        placements[start] = summary_event
    return hidden, placements


def _ids_between(events: list[SessionEvent], start_id: str, end_id: str) -> set[str]:
    collecting = False
    ids: set[str] = set()
    for event in events:
        if event.id == start_id:
            collecting = True
        if collecting:
            ids.add(event.id)
        if event.id == end_id:
            break
    return ids


def _system_message(event: SessionEvent) -> SystemMessage:
    text = str(event.payload.get("system_prompt") or event.payload.get("text") or "")
    message = SystemMessage.of(text)
    message.id = event.id
    return message


def _user_message(event: SessionEvent) -> UserMessage:
    message = UserMessage.of(str(event.payload.get("text") or ""))
    message.id = event.id
    return message


def _continue_message(event: SessionEvent) -> ContinueMessage:
    message = ContinueMessage.of(str(event.payload.get("text") or ""))
    message.id = event.id
    return message


def _summary_message(event: SessionEvent) -> SummaryMessage:
    message = SummaryMessage.of(str(event.payload.get("text") or ""))
    message.id = event.id
    return message


def _assistant_message(event: SessionEvent) -> AssistantMessage:
    raw_calls = event.payload.get("tool_calls") or []
    calls: list[ToolCallBlock] = []
    if isinstance(raw_calls, list):
        for item in raw_calls:
            if not isinstance(item, dict):
                continue
            calls.append(
                ToolCallBlock(
                    id=str(item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=str(item.get("arguments") or "{}"),
                )
            )
    text = event.payload.get("text")
    message = AssistantMessage.of(str(text) if text else None, calls or None)
    message.id = event.id
    return message


def _tool_message(event: SessionEvent) -> ToolMessage:
    call_id = str(event.payload.get("call_id") or "")
    message = ToolMessage.of(call_id)
    message.id = event.id
    return message


def _execution_from_result(event: SessionEvent) -> ToolExecution:
    payload = event.payload
    ok = bool(payload.get("ok"))
    content = str(payload.get("content") or "")
    error = payload.get("error")
    return ToolExecution(
        id=str(payload.get("call_id") or ""),
        tool_name=str(payload.get("name") or ""),
        arguments=str(payload.get("arguments") or "{}"),
        status=ToolResultStatus.OK if ok else ToolResultStatus.ERROR,
        result=ToolResult(content=content),
        duration=float(payload.get("duration") or 0.0),
        error=None if error is None else str(error),
        finished_at=event.ts,
        started_at=event.ts,
    )


def apply_meta(session: Session, meta: SessionMeta) -> None:
    """用 sidecar head 覆盖 cwd / 已读 / 白名单 / 用量。"""
    session.metadata.title = meta.title
    session.metadata.created_at = meta.created_at or session.metadata.created_at
    session.metadata.updated_at = meta.updated_at or session.metadata.updated_at
    session.metadata.parent_id = meta.parent_id
    session.metadata.forked_from_event_id = meta.forked_from_event_id
    if meta.cwd:
        session.workspace.cwd = Path(meta.cwd)
    session.workspace.opened_files = {Path(item) for item in meta.opened_files}
    session.workspace.modified_files = {Path(item) for item in meta.modified_files}
    session.permissions.always_allowed = set(meta.always_allowed)
    try:
        session.permissions.permission_mode = PermissionMode(meta.permission_mode)
    except ValueError:
        pass
    usage = meta.usage
    session.usage = SessionUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        llm_calls=int(usage.get("llm_calls") or 0),
        tool_calls=int(usage.get("tool_calls") or 0),
        last_prompt_tokens=int(usage.get("last_prompt_tokens") or 0),
    )


def apply_system_prompt(session: Session, text: str) -> None:
    """刷新内存里的系统提示，含 ``session_started`` 事件，compact 重放时不会退回旧文案。

    不改 jsonl：磁盘仍保留当时的提示词；每次打开会话再刷一次。
    """
    for event in session.events:
        if event.type == SESSION_STARTED:
            event.payload["system_prompt"] = text
            break
    if session.messages and isinstance(session.messages[0], SystemMessage):
        fresh = SystemMessage.of(text)
        fresh.id = session.messages[0].id
        session.messages[0] = fresh


def _preserved_user(summary_event: SessionEvent) -> UserMessage | None:
    """超大粘贴被藏进 hidden 后，摘要事件上带着截断副本，resume 时插回视图。"""
    text = summary_event.payload.get("preserved_user_text")
    if not text:
        return None
    message = UserMessage.of(str(text))
    uid = summary_event.payload.get("preserved_user_event_id")
    if uid:
        message.id = str(uid)
    return message


def apply_checkpoint(session: Session, events: list[SessionEvent], at_event_id: str) -> None:
    """rewind：套锚点提交时的 checkpoint（写在该 user 事件后面）。"""
    matched: SessionEvent | None = None
    latest_before: SessionEvent | None = None
    seen_anchor = False
    for event in events:
        if event.id == at_event_id:
            seen_anchor = True
        if event.type == CHECKPOINT:
            if str(event.payload.get("after_event_id") or "") == at_event_id:
                matched = event
            if not seen_anchor:
                latest_before = event
    latest = matched or latest_before
    if latest is None:
        session.workspace.cwd = session.workspace.root
        session.workspace.opened_files.clear()
        session.workspace.modified_files.clear()
        return
    payload = latest.payload
    cwd = payload.get("cwd")
    if cwd:
        session.workspace.cwd = Path(str(cwd))
    session.workspace.opened_files = {Path(item) for item in payload.get("opened_files") or []}
    session.workspace.modified_files = {Path(item) for item in payload.get("modified_files") or []}
    session.permissions.always_allowed = set(payload.get("always_allowed") or [])
    mode = payload.get("permission_mode")
    if mode:
        try:
            session.permissions.permission_mode = PermissionMode(str(mode))
        except ValueError:
            pass


def repair_unpaired_tool_calls(session: Session) -> None:
    """resume：assistant 带了 tool_calls 但没有对应 tool_result 时补失败结果。"""
    responded: set[str] = set()
    for event in session.events:
        if event.type == TOOL_RESULT:
            call_id = str(event.payload.get("call_id") or "")
            if call_id:
                responded.add(call_id)
    assistant = None
    for event in reversed(session.events):
        if event.type == ASSISTANT and event.payload.get("tool_calls"):
            assistant = event
            break
    if assistant is None:
        return
    raw_calls = assistant.payload.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        return
    missing = []
    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        call_id = str(item.get("id") or "")
        if call_id and call_id not in responded:
            missing.append(item)
    if not missing:
        return
    for item in missing:
        call_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        arguments = str(item.get("arguments") or "{}")
        if session.tool_history.get(call_id) is None:
            session.start_tool_execution(call_id=call_id, tool_name=name, arguments=arguments)
        session.finish_tool_execution(
            call_id=call_id,
            ok=False,
            content="会话中断时该工具调用未执行。",
            counts_as_failure=False,
        )
        # finish 已写 tool_result 事件；补一条 ToolMessage 视图由 rebuild 负责
    rebuild_messages(session)
