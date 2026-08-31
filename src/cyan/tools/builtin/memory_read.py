"""memory_read —— 读取 .cyan/memory/ 下某一个 md。"""

from __future__ import annotations

from ...errors import ToolError
from ...memory.store import read_memory_file
from ...memory.types import ALLOWED_FILENAMES, INDEX_FILENAME, KIND_FILENAMES, MemoryKind
from ..base import Tool
from ..types import ToolCapability, ToolContext, ToolRunResult

MEMORY_READ_NAME = "memory_read"
MEMORY_READ_DESCRIPTION = (
    "读取项目自动记忆中的一个文件。name 可以是 MEMORY.md、user.md、feedback.md、"
    "project.md、reference.md，或 user / feedback / project / reference。"
)
MEMORY_READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "记忆文件名或类型（user / feedback / project / reference / MEMORY.md）",
        },
    },
    "required": ["name"],
}


def resolve_memory_name(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ToolError("name 不能为空")
    if text in ALLOWED_FILENAMES:
        return text
    lowered = text.lower().removesuffix(".md")
    if lowered == "memory":
        return INDEX_FILENAME
    try:
        return KIND_FILENAMES[MemoryKind(lowered)]
    except ValueError as exc:
        allowed = ", ".join(sorted(ALLOWED_FILENAMES))
        raise ToolError(f"未知记忆文件 {raw}。可选：{allowed}") from exc


class MemoryReadTool(Tool):
    name = MEMORY_READ_NAME
    description = MEMORY_READ_DESCRIPTION
    capability = ToolCapability.READ
    parameters = MEMORY_READ_PARAMETERS

    def run(self, ctx: ToolContext, name: str = "", **kwargs) -> ToolRunResult:
        filename = resolve_memory_name(name)
        content = read_memory_file(ctx.workspace, filename)
        notice = ""
        from ...memory.types import MAX_INDEX_CHARS, MAX_INDEX_LINES

        if filename == INDEX_FILENAME:
            lines = content.splitlines()
            if len(lines) > MAX_INDEX_LINES or len(content) > MAX_INDEX_CHARS:
                notice = (
                    f"\n\n索引接近上限（{MAX_INDEX_LINES} 行 / {MAX_INDEX_CHARS} 字符），"
                    "请把细节挪到类型文件并缩短 MEMORY.md。"
                )
        return ToolRunResult.success(f"# {filename}\n\n{content}{notice}")
