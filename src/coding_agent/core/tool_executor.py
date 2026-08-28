"""工具执行器: 接收 ToolCall, 执行后返回 ToolRunResult."""

from __future__ import annotations

from typing import Any

from ..tools.base import ToolContext, ToolRunResult
from ..tools.registry import ToolRegistry


class ToolExecutor:
    """实际执行工具，不负责写入 Session。"""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolRunResult:
        return self._registry.execute(name, args, ctx)
