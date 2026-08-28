"""会话状态：Agent 的过去与当前，纯数据层。

Session 保存 metadata / messages / tool_history / state / workspace /
permissions / usage / config，不参与执行逻辑；循环驱动由 ``Runtime`` 负责。

TODO: Session 理想形态是纯数据；record_* / add 等写入方法后续可下沉到 Runtime 或 Repository。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...llm.types import Message, ToolResultStatus, Usage
from ..tool_history import ToolExecution, ToolHistory, ToolResult
from .config import SessionConfig
from .metadata import SessionMetadata
from .permissions import SessionPermissions
from .state import SessionState
from .usage import SessionUsage
from .workspace import SessionWorkspace


@dataclass
class Session:
    metadata: SessionMetadata = field(default_factory=SessionMetadata)
    messages: list[Message] = field(default_factory=list)
    tool_history: ToolHistory = field(default_factory=ToolHistory)
    state: SessionState = field(default_factory=SessionState)
    workspace: SessionWorkspace = field(default_factory=SessionWorkspace)
    permissions: SessionPermissions = field(default_factory=SessionPermissions)
    usage: SessionUsage = field(default_factory=SessionUsage)
    config: SessionConfig = field(default_factory=SessionConfig)

    @classmethod
    def create(
        cls,
        *,
        workspace: Path,
        system_prompt: str,
        app_config: Any | None = None,
        title: str | None = None,
    ) -> Session:
        now = time.time()
        session_config = (
            SessionConfig.from_app_config(app_config, system_prompt)
            if app_config is not None
            else SessionConfig(system_prompt=system_prompt)
        )
        return cls(
            metadata=SessionMetadata(
                id=str(uuid.uuid4()),
                created_at=now,
                updated_at=now,
                title=title,
            ),
            workspace=SessionWorkspace(root=workspace.resolve()),
            config=session_config,
        )

    def touch(self) -> None:
        self.metadata.updated_at = time.time()

    # ------------------------------------------------------------------ 兼容访问器（逐步迁移到嵌套字段）
    # TODO: 移除 always_allowed / bash_cwd / consecutive_tool_failures 兼容 property，
    #       调用方改为 session.permissions.* / session.workspace.* / session.state.*

    @property
    def always_allowed(self) -> set[str]:
        return self.permissions.always_allowed

    @property
    def bash_cwd(self) -> Path | None:
        return self.workspace.cwd

    @bash_cwd.setter
    def bash_cwd(self, value: Path | None) -> None:
        self.workspace.cwd = value

    @property
    def consecutive_tool_failures(self) -> int:
        return self.state.consecutive_tool_failures

    @consecutive_tool_failures.setter
    def consecutive_tool_failures(self, value: int) -> None:
        self.state.consecutive_tool_failures = value

    # ------------------------------------------------------------------ messages

    def add(self, message: Message) -> None:
        self.messages.append(message)
        self.touch()

    # ------------------------------------------------------------------ tool_history
    # TODO: 两阶段记录——ToolStarted 时写入 RUNNING + started_at，完成后再更新 result/status/finished_at

    def record_tool_execution(
        self,
        call_id: str,
        tool_name: str,
        arguments: str,
        ok: bool,
        content: str,
        *,
        error: str | None = None,
        duration: float = 0.0,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> None:
        status = ToolResultStatus.OK if ok else ToolResultStatus.ERROR
        if not ok and error is None:
            error = content
        now = time.time()
        self.tool_history.record(
            ToolExecution(
                id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                status=status,
                result=ToolResult(content=content),
                started_at=started_at if started_at is not None else now,
                finished_at=finished_at if finished_at is not None else now,
                duration=duration,
                error=error,
            )
        )
        self.touch()

    # ------------------------------------------------------------------ usage

    def record_usage(self, usage: Usage) -> None:
        self.usage.input_tokens += usage.prompt_tokens
        self.usage.output_tokens += usage.completion_tokens
        self.usage.total_tokens += usage.total_tokens
        self.usage.llm_calls += 1
        self.touch()

    def record_tool_outcome(self, ok: bool) -> None:
        self.usage.tool_calls += 1
        self.state.consecutive_tool_failures = 0 if ok else self.state.consecutive_tool_failures + 1
        self.touch()

    # ------------------------------------------------------------------ state (loop control)

    def record_call_fingerprint(self, name: str, args: dict[str, Any]) -> int:
        payload = json.dumps({"name": name, "args": args}, sort_keys=True, ensure_ascii=False, default=str)
        fingerprint = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        self.state.recent_calls.append(fingerprint)
        return self.state.recent_calls.count(fingerprint)

    def record_progress(self) -> None:
        self.state.recent_calls.clear()

    def reset_repeat_tracking(self) -> None:
        self.state.recent_calls.clear()

    # ------------------------------------------------------------------ workspace

    def mark_read(self, path: Path) -> None:
        self.workspace.opened_files.add(path)
        self.touch()

    def has_read(self, path: Path) -> bool:
        return path in self.workspace.opened_files

    def mark_modified(self, path: Path) -> None:
        self.workspace.modified_files.add(path)
        self.touch()

    # ------------------------------------------------------------------ lifecycle

    def clear(self) -> None:
        """清空对话与运行时状态，保留 metadata / permissions / config。"""
        # TODO: /clear「新会话」语义——是否应重置 metadata.id / created_at？
        self.messages.clear()
        self.tool_history.clear()
        self.state = SessionState()
        self.workspace.cwd = None
        self.workspace.opened_files.clear()
        self.workspace.modified_files.clear()
        self.usage = SessionUsage()
        self.touch()

    def stats(self) -> dict[str, Any]:
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
