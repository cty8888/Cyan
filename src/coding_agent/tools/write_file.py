"""write_file —— 整文件写入或新建。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ToolError
from ..security.paths import display, resolve_path
from ._diff import unified_diff
from .base import RiskLevel, Tool, ToolCapability, ToolContext, ToolResult


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "把内容整体写入文件，文件不存在则创建（父目录会自动创建）。"
        "修改已有文件的局部内容时应优先使用 edit_file，只有新建文件或整体重写时才用本工具。"
        "覆写已存在的文件之前必须先用 read_file 读过它，否则会被拒绝。"
    )
    capability = ToolCapability.WRITE
    risk = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对于工作目录"},
            "content": {"type": "string", "description": "写入的完整内容"},
        },
        "required": ["path", "content"],
    }

    def describe(self, args: dict[str, Any], workspace: Path) -> tuple[str, str | None, str]:
        raw_path = str(args.get("path", ""))
        try:
            target = resolve_path(workspace, raw_path)
        except Exception:
            return f"写入 {raw_path}", None, "text"

        new_content = str(args.get("content", ""))
        old_content = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        action = "覆写" if target.is_file() else "新建"
        diff = unified_diff(old_content, new_content, display(workspace, target))
        return f"{action}文件 {display(workspace, target)}", diff, "diff"

    def run(self, ctx: ToolContext, path: str, content: str) -> ToolResult:
        target = resolve_path(ctx.workspace, path)
        existed = target.is_file()
        if existed and not ctx.session.has_read(target):
            raise ToolError(
                f"{display(ctx.workspace, target)} 已存在，但本会话中还没有读取过它。"
                "为避免覆盖未知内容，请先用 read_file 确认当前内容，再决定是否整体覆写。"
            )
        old_content = target.read_text(encoding="utf-8", errors="replace") if existed else ""

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        ctx.session.mark_read(target)

        line_count = len(content.splitlines())
        action = "覆写" if existed else "创建"
        return ToolResult.success(
            f"已{action} {display(ctx.workspace, target)}（{line_count} 行）",
            diff=unified_diff(old_content, content, display(ctx.workspace, target)),
        )
