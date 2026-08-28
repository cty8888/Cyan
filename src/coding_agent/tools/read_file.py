"""read_file —— 带行号读取文本文件，支持分段与预算截断。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, ClassVar

from ..errors import ToolError
from .base import RiskLevel, Tool, ToolContext, ToolResult


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "读取文本文件内容，返回结果带行号（格式为 `行号 | 内容`）。"
        "修改任何文件之前都必须先读取它，write_file/edit_file 会强制检查这一点。"
        "不传 limit 时尝试整篇读取；文件超过单次读取上限会返回 [PARTIAL VIEW] 提示，"
        "按提示传 offset 续读，或显式传 limit 分段读取。"
    )
    risk = RiskLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对于工作目录"},
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始）。默认从头读。",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": (
                    "最多读取的行数。不传表示尽量整篇读取；"
                    "显式传入且该范围超过单次读取上限时会报错，请调小 limit。"
                ),
            },
        },
        "required": ["path"],
    }

    # 按后缀注册特殊文件读取器；当前为空，一律走纯文本路径
    _SPECIAL_READERS: ClassVar[dict[str, Callable[[Path, ToolContext], ToolResult]]] = {}

    def _read_special(self, target: Path, ctx: ToolContext) -> ToolResult | None:
        """按文件后缀查找特殊读取器；未注册则返回 None。"""
        handler = self._SPECIAL_READERS.get(target.suffix.lower())
        return handler(target, ctx) if handler else None

    def run(self, ctx: ToolContext, path: str, offset: int = 1, limit: int | None = None) -> ToolResult:
        target = ctx.policy.resolve_path(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{ctx.policy.display(target)} 是目录，请使用 list_dir")

        special = self._read_special(target, ctx)
        if special is not None:
            return special

        raw = target.read_bytes()
        if b"\x00" in raw[:8192]:
            raise ToolError(f"{ctx.policy.display(target)} 看起来是二进制文件，无法以文本读取")

        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        total = len(all_lines)

        if total == 0:
            ctx.session.mark_read(target)
            return ToolResult.success(f"{ctx.policy.display(target)} 文件存在，但内容为空。")

        offset = max(1, int(offset))
        if offset > total:
            return ToolResult.success(
                f"{ctx.policy.display(target)} 共 {total} 行，第 {offset} 行起没有内容。"
            )

        explicit_limit = limit is not None
        requested_end = min(total, offset - 1 + max(1, int(limit))) if explicit_limit else total

        budget = ctx.tool_config.max_file_read_chars
        body, shown_to = _render_lines(all_lines, offset, requested_end, budget)
        truncated_by_budget = shown_to < requested_end

        if truncated_by_budget and explicit_limit:
            raise ToolError(
                f"offset={offset} limit={limit} 请求的内容超过单次读取上限（约 {budget} 字符），"
                "请调小 limit 分段读取。"
            )

        # 本次读取请求已完整满足时，标记为已读，供 write_file/edit_file 前置检查
        if not truncated_by_budget:
            ctx.session.mark_read(target)

        header = f"{ctx.policy.display(target)}（共 {total} 行，当前展示 {offset}-{shown_to} 行）"
        if truncated_by_budget:
            header += (
                f"\n[PARTIAL VIEW] 受单次读取上限限制，未能展示到第 {requested_end} 行，"
                f"如需继续请传 offset={shown_to + 1}"
            )
        elif shown_to < total:
            header += f"\n... 还有 {total - shown_to} 行未显示"

        return ToolResult.success(f"{header}\n{body}", total_lines=total, partial=truncated_by_budget)


def _render_lines(lines: list[str], offset: int, end: int, budget: int) -> tuple[str, int]:
    """渲染 [offset, end] 行（1-based，含端点），受字符预算限制。

    返回 (渲染结果, 实际展示到的行号)。
    """
    width = len(str(end))
    parts: list[str] = []
    size = 0
    shown_to = offset - 1
    for line_no in range(offset, end + 1):
        rendered = f"{line_no:>{width}} | {lines[line_no - 1]}"
        added = len(rendered) + 1
        if parts and size + added > budget:
            break
        parts.append(rendered)
        size += added
        shown_to = line_no
    return "\n".join(parts), shown_to
