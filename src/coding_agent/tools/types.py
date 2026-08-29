"""工具层的数据契约：能力、风险、执行结果与运行环境。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..settings import ToolLimits

if TYPE_CHECKING:
    from ..session import WorkspaceAccess


class ToolCapability(Enum):
    """工具操作类型（read / write / exec）。"""

    READ = "read"
    WRITE = "write"
    EXEC = "exec"


class RiskLevel(Enum):
    """工具固有风险，与 ``ToolCapability`` 是正交的两轴。

    - ``capability`` 回答「这是读、写还是执行」——决定 Plan / AcceptEdits / Bypass
      怎么分流。
    - ``risk`` 回答「这次操作有多危险」。真正参与判定的是 ``CRITICAL``：即使 Bypass、
      AcceptEdits 或本会话始终允许，也强制逐次确认。其余级别用于审批面板展示，
      让用户看到比 capability 更细的风险提示（例如 bash 是 EXEC + HIGH，
      未来的 ``delete_file`` 可以是 WRITE + CRITICAL，而不必改 capability 语义）。
    """

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ToolRunResult:
    """工具单次执行结果。

    ``content`` 回喂模型；``metadata`` 仅供 CLI 渲染（如 diff），不进上下文。
    """

    ok: bool
    content: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **metadata: Any) -> ToolRunResult:
        return cls(ok=True, content=content, metadata=metadata)

    @classmethod
    def failure(cls, error: str, **metadata: Any) -> ToolRunResult:
        return cls(ok=False, error=error, metadata=metadata)

    def to_model_text(self) -> str:
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


@dataclass
class ProcessOutput:
    """子进程执行结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration: float
