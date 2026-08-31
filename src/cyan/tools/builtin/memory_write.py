"""memory_write —— 写入 .cyan/memory/ 下某一类笔记并更新索引。"""

from __future__ import annotations

from ...errors import ToolError
from ...memory.settings import auto_memory_enabled
from ...memory.store import write_entry
from ...memory.types import MemoryEntry, MemoryKind
from ..base import Tool
from ..types import ToolCapability, ToolContext, ToolRunResult

MEMORY_WRITE_NAME = "memory_write"
MEMORY_WRITE_DESCRIPTION = (
    "把一条跨会话笔记写入 .cyan/memory/。"
    "kind 为 user（偏好/角色）、feedback（纠错与被确认的做法）、"
    "project（代码里看不到的进度与决策）、reference（仓库外入口）。"
    "只记以后还用得上、且 cyan.md / 代码里没有的内容。不要记密钥。"
    "mode=append 追加；replace 覆盖该类型文件。"
)
MEMORY_WRITE_PARAMETERS = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["user", "feedback", "project", "reference"],
            "description": "记忆类型",
        },
        "content": {"type": "string", "description": "要记住的内容"},
        "summary": {
            "type": "string",
            "description": "写入 MEMORY.md 的一行摘要；省略则用 content 首行",
        },
        "mode": {
            "type": "string",
            "enum": ["append", "replace"],
            "description": "append 追加（默认），replace 覆盖该类型文件",
            "default": "append",
        },
    },
    "required": ["kind", "content"],
}


class MemoryWriteTool(Tool):
    name = MEMORY_WRITE_NAME
    description = MEMORY_WRITE_DESCRIPTION
    capability = ToolCapability.WRITE
    parameters = MEMORY_WRITE_PARAMETERS

    def run(
        self,
        ctx: ToolContext,
        kind: str = "",
        content: str = "",
        summary: str = "",
        mode: str = "append",
        **kwargs,
    ) -> ToolRunResult:
        if not auto_memory_enabled():
            raise ToolError("自动记忆已关闭（CYAN_DISABLE_AUTO_MEMORY）。")
        try:
            memory_kind = MemoryKind(kind.strip().lower())
        except ValueError as exc:
            raise ToolError("kind 必须是 user / feedback / project / reference") from exc
        body = (content or "").strip()
        if not body:
            raise ToolError("content 不能为空")
        line = (summary or "").strip() or body.splitlines()[0][:80]
        if mode not in {"append", "replace"}:
            raise ToolError("mode 必须是 append 或 replace")
        wrote = write_entry(
            ctx.workspace,
            MemoryEntry(kind=memory_kind, summary=line, detail=body),
            mode=mode,
        )
        if not wrote:
            return ToolRunResult.success("与已有记忆或 cyan.md 重复，未写入。")
        return ToolRunResult.success(f"已写入 {memory_kind.value}：{line}")
