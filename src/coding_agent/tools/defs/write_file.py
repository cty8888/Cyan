"""write_file —— 整文件写入或新建。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...constants.tools.defs.write_file import WRITE_FILE_DESCRIPTION, WRITE_FILE_NAME, WRITE_FILE_PARAMETERS
from ...errors import ToolError
from ...security.paths import display, resolve_path
from .._diff import unified_diff
from ..base import RiskLevel, Tool, ToolCapability, ToolContext, ToolRunResult


class WriteFileTool(Tool):
    name = WRITE_FILE_NAME
    description = WRITE_FILE_DESCRIPTION
    capability = ToolCapability.WRITE
    risk = RiskLevel.MEDIUM
    parameters = WRITE_FILE_PARAMETERS

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

    def run(self, ctx: ToolContext, path: str, content: str) -> ToolRunResult:
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
        return ToolRunResult.success(
            f"已{action} {display(ctx.workspace, target)}（{line_count} 行）",
            diff=unified_diff(old_content, content, display(ctx.workspace, target)),
        )
