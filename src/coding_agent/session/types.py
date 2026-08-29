"""会话数据层的字段定义：元信息、工作区、用量，以及工具执行历史。"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from ..security.types import PermissionMode

RECENT_CALL_WINDOW = 8


def _new_session_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


@dataclass
class SessionMetadata:
    session_id: str = field(default_factory=_new_session_id)
    created_at: float = field(default_factory=_now)
    # 状态变更时由 touch() 刷新，供未来会话列表按「最近使用」排序。
    updated_at: float = field(default_factory=_now)
    # 会话标题；持久化接入前保持为空。
    title: str | None = None

    @classmethod
    def create(cls, title: str | None = None) -> SessionMetadata:
        """创建一组新的会话元信息。"""
        return cls(title=title)

    def touch(self) -> None:
        self.updated_at = _now()


@dataclass
class SessionPermissions:
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    # 按「同类操作」记键，而不是整个工具名：写入是 ``write:{目录}``（根目录文件为
    # ``write:.``，只放行根下其它文件，不含子目录；``write:pkg`` 放行 ``pkg/`` 及其子目录），
    # 执行是 ``exec:{命令名}``（如 ``exec:pytest``）。敏感路径 / CRITICAL 走 force=True，
    # 不受这份白名单影响。
    always_allowed: set[str] = field(default_factory=set)


@dataclass
class SessionWorkspace:
    root: Path = field(default_factory=Path.cwd)
    cwd: Path | None = None
    # 已读文件：read_file 写入，write_file / edit_file 用 has_read 做前置检查。
    opened_files: set[Path] = field(default_factory=set, repr=False)
    # 本会话写过的文件，由 write_file / edit_file 维护。
    modified_files: set[Path] = field(default_factory=set, repr=False)

    @classmethod
    def for_root(cls, root: Path) -> SessionWorkspace:
        """绑定项目根目录，其余字段使用默认值。"""
        return cls(root=root.resolve())

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        if self.cwd is None:
            self.cwd = self.root
        else:
            self.cwd = self.cwd.resolve()


@dataclass
class SessionState:
    current_task: str | None = None
    consecutive_tool_failures: int = 0
    # 最近若干次工具调用的指纹，用于检测「同工具 + 同参数」重复调用。
    recent_calls: deque[str] = field(
        default_factory=lambda: deque(maxlen=RECENT_CALL_WINDOW), repr=False
    )


@dataclass
class SessionUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ToolResultStatus(Enum):
    """工具执行结果的状态。"""

    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


RenderMode = Literal["summary", "full"]


@dataclass
class ToolResult:
    """一次工具执行的输出数据，供压缩与展示策略读取。"""

    content: str | None = None
    summary: str | None = None
    ref: str | None = None

    @property
    def has_summary(self) -> bool:
        return self.summary is not None

    @property
    def content_removed(self) -> bool:
        return self.content is None and self.ref is not None

    def render(self, mode: RenderMode = "summary") -> str:
        """按指定模式渲染给模型看的文本。

        ``mode="summary"``：优先返回 ``summary``，没有则返回 ``content``。
        ``mode="full"``：若 ``content`` 存在则返回原文；若原文已删除但有 ``ref`` 则返回引用提示；
        否则退回 ``summary``。
        """
        if mode == "summary":
            if self.summary is not None:
                return self.summary
            return self.content or ""

        if self.content is not None:
            return self.content
        if self.ref is not None:
            hint = f"[完整结果已移除，存储引用：{self.ref}]"
            return f"{self.summary}\n{hint}" if self.summary else hint
        return self.summary or ""


@dataclass
class ToolExecution:
    """一次工具调用的完整事实记录。"""

    id: str = ""
    tool_name: str = ""
    arguments: str = ""
    status: ToolResultStatus = ToolResultStatus.RUNNING
    result: ToolResult | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    duration: float = 0.0
    error: str | None = None


@dataclass
class ToolHistory:
    """会话内所有工具执行记录，按 call id 索引。

    只负责保存与查询（``record`` / ``get`` / ``remove``），不承担「如何展示给模型」。
    """

    executions: dict[str, ToolExecution] = field(default_factory=dict)

    def record(self, execution: ToolExecution) -> None:
        self.executions[execution.id] = execution

    def get(self, call_id: str) -> ToolExecution | None:
        return self.executions.get(call_id)

    def remove(self, call_id: str) -> ToolExecution | None:
        """移除指定 call id 的记录；存在则返回被移除的 ``ToolExecution``，否则 ``None``。"""
        return self.executions.pop(call_id, None)

    def clear(self) -> None:
        self.executions.clear()
