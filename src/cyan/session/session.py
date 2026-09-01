"""会话门面：消息历史、工具记录、工作区状态与用量统计。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..llm.parser import parse_tool_arguments
from ..llm.types import (
    AssistantMessage,
    ContinueMessage,
    Message,
    SummaryMessage,
    SystemMessage,
    ToolMessage,
    Usage,
    UserMessage,
)
from ..security.types import PermissionMode
from .events import (
    ASSISTANT,
    CHECKPOINT,
    CONTINUE,
    FILE_OP,
    SESSION_STARTED,
    SUMMARY,
    TOOL_RESULT,
    USER,
    SessionEvent,
    new_event_id,
)
from .types import (
    SessionMetadata,
    SessionPermissions,
    SessionState,
    SessionUsage,
    SessionWorkspace,
    TodoItem,
    ToolExecution,
    ToolHistory,
    ToolResult,
    ToolResultStatus,
)

if TYPE_CHECKING:
    from .store import DiskStore


@dataclass
class Session:
    """一次运行的会话状态。``events`` 是完整历史；``messages`` 是组窗视图。"""

    workspace: SessionWorkspace
    metadata: SessionMetadata = field(default_factory=SessionMetadata)
    messages: list[Message] = field(default_factory=list)
    tool_history: ToolHistory = field(default_factory=ToolHistory)
    state: SessionState = field(default_factory=SessionState)
    permissions: SessionPermissions = field(default_factory=SessionPermissions)
    usage: SessionUsage = field(default_factory=SessionUsage)
    events: list[SessionEvent] = field(default_factory=list)
    store: DiskStore | None = None
    model: str = ""
    # todo_write 维护的任务清单；跟 opened_files/always_allowed 一样是「当前状态」，
    # 不进事件表，只随 checkpoint / meta.json 走。
    todos: list[TodoItem] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        workspace: Path,
        system_prompt: str = "",
        title: str | None = None,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        store: DiskStore | None = None,
        model: str = "",
        session_id: str | None = None,
        parent_id: str | None = None,
        forked_from_event_id: str | None = None,
    ) -> Session:
        """绑定工作目录并写入系统提示，作为一次新会话的起点。"""
        metadata = SessionMetadata.create(title=title)
        if session_id:
            metadata.session_id = session_id
        elif store is not None:
            metadata.session_id = store.session_id
        metadata.parent_id = parent_id
        metadata.forked_from_event_id = forked_from_event_id
        session = cls(
            metadata=metadata,
            workspace=SessionWorkspace.for_root(workspace),
            permissions=SessionPermissions(permission_mode=permission_mode),
            store=store,
            model=model,
        )
        if system_prompt:
            session.add(SystemMessage.of(system_prompt))
        else:
            session.persist_head()
        return session

    @property
    def always_allowed(self) -> set[str]:
        return self.permissions.always_allowed

    @property
    def bash_cwd(self) -> Path | None:
        return self.workspace.cwd

    @bash_cwd.setter
    def bash_cwd(self, value: Path | None) -> None:
        if value is None:
            self.workspace.cwd = self.workspace.root
        else:
            self.workspace.cwd = value.resolve()
        self.persist_head()

    @property
    def consecutive_tool_failures(self) -> int:
        return self.state.consecutive_tool_failures

    @consecutive_tool_failures.setter
    def consecutive_tool_failures(self, value: int) -> None:
        self.state.consecutive_tool_failures = value

    def set_todos(self, items: list[TodoItem]) -> None:
        """整体替换任务清单（todo_write 每次都传完整列表，不是增量 patch）。"""
        self.todos = items
        self.metadata.touch()
        self.persist_head()

    def add(self, message: Message) -> None:
        """追加一条消息。对话类消息写入事件表；ToolMessage 只挂到已有 tool_result。"""
        if isinstance(message, ToolMessage):
            self._attach_tool_message(message)
            self.messages.append(message)
            self.metadata.touch()
            return

        if isinstance(message, SystemMessage):
            event = self._append_event(
                SESSION_STARTED,
                {
                    "system_prompt": message.text or "",
                    "model": self.model,
                    "permission_mode": self.permissions.permission_mode.value,
                },
            )
        elif isinstance(message, ContinueMessage):
            event = self._append_event(CONTINUE, {"text": message.text or ""})
        elif isinstance(message, SummaryMessage):
            event = self._append_event(SUMMARY, {"text": message.text or ""})
        elif isinstance(message, UserMessage):
            payload: dict[str, Any] = {"text": message.text or ""}
            files = [
                {
                    "path": block.path,
                    "content": block.content,
                    "start_line": block.start_line,
                    "end_line": block.end_line,
                }
                for block in message.file_blocks
            ]
            if files:
                payload["files"] = files
            event = self._append_event(USER, payload)
        elif isinstance(message, AssistantMessage):
            event = self._append_event(
                ASSISTANT,
                {
                    "text": message.text,
                    "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in message.tool_calls
                    ],
                },
            )
        else:
            raise TypeError(f"不支持写入事件表的消息类型：{type(message).__name__}")

        message.id = event.id
        self.messages.append(message)
        self.metadata.touch()

        if isinstance(message, UserMessage) and not isinstance(message, (SummaryMessage, ContinueMessage)):
            if not self.metadata.title:
                text = (message.text or "").strip().replace("\n", " ")
                self.metadata.title = text[:80] or None
            self._append_checkpoint(after_event_id=event.id)
            self.persist_head()
            self._mark_last()
        else:
            self.persist_head()

    def _attach_tool_message(self, message: ToolMessage) -> None:
        call_id = ""
        block = message.tool_result
        if block is not None:
            call_id = block.tool_call_id
        for event in reversed(self.events):
            if event.type == TOOL_RESULT and str(event.payload.get("call_id") or "") == call_id:
                message.id = event.id
                return

    def start_tool_execution(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: str,
    ) -> None:
        """记一条 RUNNING 状态的工具执行，供后续 finish 补全结果。"""
        execution = ToolExecution(
            id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            status=ToolResultStatus.RUNNING,
        )
        self.tool_history.record(execution)
        self.metadata.touch()

    def finish_tool_execution(
        self,
        *,
        call_id: str,
        ok: bool,
        content: str,
        error: str | None = None,
        duration: float | None = None,
        counts_as_failure: bool = True,
    ) -> None:
        """把工具调用标为完成，并按需更新连续失败计数。

        ``counts_as_failure=False``：权限拒绝、任务中断补齐等——回喂模型即可，
        不应当成「工具没跑成」去掐任务。
        """
        execution = self.tool_history.get(call_id)
        if execution is None:
            raise RuntimeError(f"不存在对应的工具调用记录: {call_id}")

        now = time.time()
        execution.status = ToolResultStatus.OK if ok else ToolResultStatus.ERROR
        execution.result = ToolResult(content=content)
        if not ok and error is None:
            error = content
        execution.error = error
        execution.finished_at = now  # noqa  与 types.ToolExecution.finished_at 同步写入，暂无读取方
        execution.duration = duration if duration is not None else (now - execution.started_at)

        self.usage.tool_calls += 1
        if ok:
            self.state.consecutive_tool_failures = 0
        elif counts_as_failure:
            self.state.consecutive_tool_failures += 1
        self.metadata.touch()

        self._append_event(
            TOOL_RESULT,
            {
                "call_id": call_id,
                "name": execution.tool_name,
                "arguments": execution.arguments,
                "ok": ok,
                "content": content,
                "error": execution.error,
                "duration": execution.duration,
                "counts_as_failure": counts_as_failure,
            },
        )
        if ok and execution.tool_name in {"write_file", "edit_file"}:
            path = ""
            try:
                parsed = parse_tool_arguments(execution.arguments or "{}", execution.tool_name)
                path = str(parsed.get("path") or "")
            except Exception:
                path = ""
            if path:
                self._append_event(FILE_OP, {"path": path, "kind": execution.tool_name})
        self.persist_head()

    def record_usage(self, usage: Usage, *, for_trigger: bool = True) -> None:
        """累加本轮模型调用的 token 与次数。

        ``for_trigger=True`` 时更新 ``last_prompt_tokens``（任务调用）。
        压缩用的那次 chat 传 ``False``，避免刚压完又立刻再压。
        """
        self.usage.input_tokens += usage.prompt_tokens
        self.usage.output_tokens += usage.completion_tokens
        self.usage.llm_calls += 1
        if for_trigger:
            self.usage.last_prompt_tokens = usage.prompt_tokens
        self.metadata.touch()
        self.persist_head()

    def append_event(self, event_type: str, payload: dict[str, Any]) -> SessionEvent:
        """供 compact / fork 写入非消息事件。"""
        return self._append_event(event_type, payload)

    def _append_event(self, event_type: str, payload: dict[str, Any]) -> SessionEvent:
        parent_id = self.events[-1].id if self.events else None
        event = SessionEvent(
            type=event_type,
            payload=payload,
            id=new_event_id(),
            parent_id=parent_id,
        )
        self.events.append(event)
        if self.store is not None:
            self.store.append(event)
        return event

    def _append_checkpoint(self, *, after_event_id: str) -> None:
        self._append_event(
            CHECKPOINT,
            {
                "after_event_id": after_event_id,
                "cwd": str(self.workspace.cwd or self.workspace.root),
                "opened_files": [str(path) for path in sorted(self.workspace.opened_files, key=str)],
                "modified_files": [str(path) for path in sorted(self.workspace.modified_files, key=str)],
                "always_allowed": sorted(self.permissions.always_allowed),
                "permission_mode": self.permissions.permission_mode.value,
                "todos": [item.to_json() for item in self.todos],
            },
        )

    def persist_head(self) -> None:
        """把最新 cwd / 已读 / 白名单 / 用量写到 sidecar。无 store 时是空操作。"""
        if self.store is None:
            return
        from .store import SessionMeta

        meta = SessionMeta(
            id=self.metadata.session_id,
            title=self.metadata.title,
            created_at=self.metadata.created_at,
            updated_at=self.metadata.updated_at,
            workspace=str(self.workspace.root),
            parent_id=self.metadata.parent_id,
            forked_from_event_id=self.metadata.forked_from_event_id,
            cwd=str(self.workspace.cwd or self.workspace.root),
            opened_files=[str(path) for path in sorted(self.workspace.opened_files, key=str)],
            modified_files=[str(path) for path in sorted(self.workspace.modified_files, key=str)],
            always_allowed=sorted(self.permissions.always_allowed),
            permission_mode=self.permissions.permission_mode.value,
            todos=[item.to_json() for item in self.todos],
            usage={
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "llm_calls": self.usage.llm_calls,
                "tool_calls": self.usage.tool_calls,
                "last_prompt_tokens": self.usage.last_prompt_tokens,
            },
        )
        self.store.write_meta(meta)

    def _mark_last(self) -> None:
        if self.store is not None:
            self.store.set_last()

    # ------------------------------------------------------------------ 循环控制

    def record_call_fingerprint(self, name: str, args: dict[str, Any]) -> int:
        """记录本次调用指纹，返回当前连续相同次数。

        只统计连续重复：中间换成另一次调用会重新从 1 计。成功进展由
        ``reset_repeat_tracking`` 清零，避免合法重试被窗口计数误杀。
        """
        payload = json.dumps({"name": name, "args": args}, sort_keys=True, ensure_ascii=False, default=str)
        fingerprint = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        if self.state.last_call_fingerprint == fingerprint:
            self.state.consecutive_identical_calls += 1
        else:
            self.state.last_call_fingerprint = fingerprint
            self.state.consecutive_identical_calls = 1
        return self.state.consecutive_identical_calls

    def reset_repeat_tracking(self) -> None:
        """工具调用有进展（任意成功结果）时清空连续重复计数。"""
        self.state.last_call_fingerprint = None
        self.state.consecutive_identical_calls = 0

    # ------------------------------------------------------------------ 工作区

    def mark_read(self, path: Path) -> None:
        """标记文件已读，供 write_file / edit_file 做修改前检查。"""
        self.workspace.opened_files.add(path.resolve())
        self.metadata.touch()
        self.persist_head()

    def unmark_read(self, path: Path) -> None:
        """文件被其它途径改过（或读结果已被压缩丢掉）后，撤销已读标记。"""
        self.workspace.opened_files.discard(path.resolve())
        self.metadata.touch()
        self.persist_head()

    def clear_reads(self) -> None:
        """看不清写了哪些文件时，保守清空全部已读标记。"""
        self.workspace.opened_files.clear()
        self.metadata.touch()
        self.persist_head()

    def has_read(self, path: Path) -> bool:
        """判断本会话是否已经整篇读取过该文件（分段 / 截断读取不算）。"""
        return path.resolve() in self.workspace.opened_files

    def mark_modified(self, path: Path) -> None:
        """标记文件已被本会话修改。"""
        self.workspace.modified_files.add(path.resolve())
        self.metadata.touch()
        self.persist_head()

    # ------------------------------------------------------------------ 生命周期

    def clear(self) -> None:
        """清空对话与运行时状态，保留 metadata / permissions / 首条 SystemMessage。"""
        system_message = (
            self.messages[0]
            if self.messages and isinstance(self.messages[0], SystemMessage)
            else None
        )
        self.messages.clear()
        if system_message is not None:
            self.messages.append(system_message)
        self.tool_history.clear()
        self.state = SessionState()
        self.workspace.cwd = self.workspace.root
        self.workspace.opened_files.clear()
        self.workspace.modified_files.clear()
        self.usage = SessionUsage()
        self.metadata.touch()

    def stats(self) -> dict[str, Any]:
        """供任务结束面板与 ``/usage`` 展示的汇总数字。"""
        return {
            "llm_calls": self.usage.llm_calls,
            "tool_calls": self.usage.tool_calls,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "total_tokens": self.usage.total_tokens,
            "prompt_tokens": self.usage.input_tokens,
            "completion_tokens": self.usage.output_tokens,
            "messages": len(self.messages),
        }
