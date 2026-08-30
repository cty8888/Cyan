"""会话门面：消息历史、工具记录、工作区状态与用量统计。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llm.types import Message, SystemMessage, Usage
from ..security.types import PermissionMode
from .types import (
    SessionMetadata,
    SessionPermissions,
    SessionState,
    SessionUsage,
    SessionWorkspace,
    ToolExecution,
    ToolHistory,
    ToolResult,
    ToolResultStatus,
)


@dataclass
class Session:
    """一次运行的会话状态。"""

    workspace: SessionWorkspace
    metadata: SessionMetadata = field(default_factory=SessionMetadata)
    messages: list[Message] = field(default_factory=list)
    tool_history: ToolHistory = field(default_factory=ToolHistory)
    state: SessionState = field(default_factory=SessionState)
    permissions: SessionPermissions = field(default_factory=SessionPermissions)
    usage: SessionUsage = field(default_factory=SessionUsage)

    @classmethod
    def create(
        cls,
        *,
        workspace: Path,
        system_prompt: str = "",
        title: str | None = None,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> Session:
        """绑定工作目录并写入系统提示，作为一次新会话的起点。"""
        session = cls(
            metadata=SessionMetadata.create(title=title),
            workspace=SessionWorkspace.for_root(workspace),
            permissions=SessionPermissions(permission_mode=permission_mode),
        )
        if system_prompt:
            session.add(SystemMessage.of(system_prompt))
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

    @property
    def consecutive_tool_failures(self) -> int:
        return self.state.consecutive_tool_failures

    @consecutive_tool_failures.setter
    def consecutive_tool_failures(self, value: int) -> None:
        self.state.consecutive_tool_failures = value

    def add(self, message: Message) -> None:
        """追加一条消息并刷新会话的最近更新时间。"""
        self.messages.append(message)
        self.metadata.touch()

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
    ) -> None:
        """把工具调用标为完成，并更新连续失败计数。"""
        execution = self.tool_history.get(call_id)
        if execution is None:
            raise RuntimeError(f"不存在对应的工具调用记录: {call_id}")

        now = time.time()
        execution.status = ToolResultStatus.OK if ok else ToolResultStatus.ERROR
        execution.result = ToolResult(content=content)
        if not ok and error is None:
            error = content
        execution.error = error
        execution.finished_at = now
        # 优先用调用方传入的 monotonic 耗时（不含审批等待）；未传入时按墙钟估算。
        execution.duration = duration if duration is not None else (now - execution.started_at)

        self.usage.tool_calls += 1
        if ok:
            self.state.consecutive_tool_failures = 0
        else:
            self.state.consecutive_tool_failures += 1
        self.metadata.touch()

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

    def has_read(self, path: Path) -> bool:
        """判断本会话是否已经整篇读取过该文件（分段 / 截断读取不算）。"""
        return path.resolve() in self.workspace.opened_files

    def mark_modified(self, path: Path) -> None:
        """标记文件已被本会话修改。"""
        self.workspace.modified_files.add(path.resolve())
        self.metadata.touch()

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
