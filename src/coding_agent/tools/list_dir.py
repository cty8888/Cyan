"""list_dir —— 树形列出目录内容。"""

from __future__ import annotations

from pathlib import Path

from ..errors import ToolError
from ..security.paths import display, resolve_path
from .base import RiskLevel, Tool, ToolCapability, ToolContext, ToolResult

# 遍历时跳过的噪声目录
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".idea", ".vscode",
}


class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "列出目录内容，以树形结构返回。用于了解项目结构。"
        "会自动跳过 .git、node_modules、__pycache__ 等噪声目录。"
    )
    capability = ToolCapability.READ
    risk = RiskLevel.MINIMAL
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "目录路径，相对于工作目录。默认为工作目录根。",
                "default": ".",
            },
            "depth": {
                "type": "integer",
                "description": "递归深度，1 表示只列出当前层。默认 2。",
                "default": 2,
            },
        },
    }

    def run(self, ctx: ToolContext, path: str = ".", depth: int = 2) -> ToolResult:
        target = resolve_path(ctx.workspace, path, must_exist=True)
        if not target.is_dir():
            raise ToolError(f"{display(ctx.workspace, target)} 不是目录，如需读取文件请使用 read_file")

        depth = max(1, min(int(depth), 6))
        lines: list[str] = [f"{display(ctx.workspace, target)}/"]
        truncated = _walk(target, depth, ctx.tool_config.max_dir_entries, lines, prefix="  ")

        if len(lines) == 1:
            lines.append("  (空目录)")
        if truncated:
            lines.append(f"  ... 条目过多，已截断至 {ctx.tool_config.max_dir_entries} 条")

        return ToolResult.success("\n".join(lines), entry_count=len(lines) - 1)


def _walk(directory: Path, depth: int, budget: int, lines: list[str], prefix: str) -> bool:
    """深度优先写入树形结构；条目超限时返回 True。"""
    if depth <= 0:
        return False
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        lines.append(f"{prefix}(无权限访问)")
        return False

    for entry in entries:
        if len(lines) - 1 >= budget:
            return True
        if entry.is_dir():
            if entry.name in _SKIP_DIRS:
                continue
            lines.append(f"{prefix}{entry.name}/")
            if _walk(entry, depth - 1, budget, lines, prefix + "  "):
                return True
        else:
            lines.append(f"{prefix}{entry.name}  ({_human_size(entry)})")
    return False


def _human_size(path: Path) -> str:
    """格式化为人类可读的文件大小。"""
    try:
        size = path.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}GB"
