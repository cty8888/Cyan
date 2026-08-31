"""工具层的数据契约：能力、风险、执行结果与运行环境。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..settings import ToolLimits

if TYPE_CHECKING:
    from ..session import TodoAccess, WorkspaceAccess


class ToolCapability(Enum):
    """工具操作类型（read / write / exec）。"""

    READ = "read"
    WRITE = "write"
    EXEC = "exec"


@dataclass
class ToolRunResult:
    """工具单次执行结果。

    ``content`` 回喂模型；``metadata`` 仅供 CLI 渲染（如 diff），不进上下文。
    """

    ok: bool
    content: str = ""
    error: str | None = None
    # 权限拒绝、任务中断补的回复不算「工具没跑成」，不累加连续失败。
    counts_as_failure: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **metadata: Any) -> ToolRunResult:
        return cls(ok=True, content=content, metadata=metadata)

    @classmethod
    def failure(cls, error: str, *, counts_as_failure: bool = True, **metadata: Any) -> ToolRunResult:
        return cls(ok=False, error=error, counts_as_failure=counts_as_failure, metadata=metadata)

    def to_model_text(self) -> str:
        """回喂模型的文本：成功用 content，失败带「错误：」前缀。"""
        if self.ok:
            return self.content or "(执行成功，无输出)"
        return f"错误：{self.error}"


@dataclass
class ToolContext:
    """工具执行时可访问的运行环境。

    只暴露 workspace 视图（``WorkspaceAccess``）+ 受控 mutator，不传入整个 ``Session``——
    工具不应该能触达消息历史、权限白名单、token 用量等与"执行一次工具"无关的状态。
    """

    workspace: Path
    tool_limits: ToolLimits
    workspace_access: WorkspaceAccess
    todos: TodoAccess


@dataclass
class ProcessOutput:
    """子进程执行结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration: float
    output_capped: bool = False
