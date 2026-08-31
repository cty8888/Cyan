"""memory_list —— 列出项目 .cyan/memory/ 下的记忆文件。"""

from __future__ import annotations

from ...memory.store import list_memory_files
from ..base import Tool
from ..types import ToolCapability, ToolContext, ToolRunResult

MEMORY_LIST_NAME = "memory_list"
MEMORY_LIST_DESCRIPTION = (
    "列出项目自动记忆目录 .cyan/memory/ 中的文件。"
    "索引 MEMORY.md 每轮都会进系统提示；user.md / feedback.md / project.md / reference.md "
    "需要用 memory_read 按需读取。"
)
MEMORY_LIST_PARAMETERS = {"type": "object", "properties": {}}


class MemoryListTool(Tool):
    name = MEMORY_LIST_NAME
    description = MEMORY_LIST_DESCRIPTION
    capability = ToolCapability.READ
    parameters = MEMORY_LIST_PARAMETERS

    def run(self, ctx: ToolContext, **kwargs) -> ToolRunResult:
        items = list_memory_files(ctx.workspace)
        if not items:
            return ToolRunResult.success("自动记忆目录为空（还没有任何 .md）。")
        lines = [f"{name}  {size} 字节" for name, size in items]
        return ToolRunResult.success("\n".join(lines))
