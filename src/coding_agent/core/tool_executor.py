"""工具执行入口，预留 hooks / 超时 / 埋点的扩展点。"""

from __future__ import annotations

from typing import Any

from ..tools.registry import ToolRegistry
from ..tools.types import ToolContext, ToolRunResult


class ToolExecutor:
    """把一次工具调用转交给 ``ToolRegistry``。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        *,
        validated: bool = False,
    ) -> ToolRunResult:
        return self._registry.execute(name, args, ctx, validated=validated)
