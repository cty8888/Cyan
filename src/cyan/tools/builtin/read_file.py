"""read_file —— 带行号读取文本文件，支持分段与预算截断。"""

from __future__ import annotations

import io

from ...errors import ToolError
from ...security.paths import display, resolve_path
from ..base import Tool
from ..types import ToolCapability, ToolContext, ToolRunResult

READ_FILE_NAME = "read_file"
READ_FILE_DESCRIPTION = (
    "读取文本文件内容, 返回结果带行号 (格式为 `行号 | 内容`). "
    "行号和竖线只供定位, 调用 edit_file 时 old_string 不要带上它们. "
    "修改任何文件之前都必须先完整读取它, write_file/edit_file 会检查本会话是否已经整篇读过；"
    "分段 limit 或超出字符预算的截断读取不算。 "
    "不传 limit 时尝试整篇读取; 文件超过单次读取上限会返回 [PARTIAL VIEW] 提示, "
    "按提示传 offset 续读, 或显式传 limit 分段读取."
)
READ_FILE_DEFAULT_OFFSET = 1
_PREVIEW_MAX_LINES = 20  # CLI 渲染代码预览面板时最多展示的行数，避免长文件把终端刷屏
READ_FILE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径, 相对于项目根目录"},
        "offset": {
            "type": "integer",
            "description": "起始行号 (从 1 开始). 默认从头读.",
            "default": READ_FILE_DEFAULT_OFFSET,
        },
        "limit": {
            "type": "integer",
            "description": (
                "最多读取的行数. 不传表示尽量整篇读取; "
                "显式传入且该范围超过单次读取上限时会报错, 请调小 limit."
            ),
        },
    },
    "required": ["path"],
}


class ReadFileTool(Tool):
    name = READ_FILE_NAME
    description = READ_FILE_DESCRIPTION
    capability = ToolCapability.READ
    parameters = READ_FILE_PARAMETERS

    def run(
        self,
        ctx: ToolContext,
        path: str,
        offset: int = READ_FILE_DEFAULT_OFFSET,
        limit: int | None = None,
    ) -> ToolRunResult:
        target = resolve_path(ctx.workspace, path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{display(ctx.workspace, target)} 是目录，请使用 list_dir")

        size = target.stat().st_size
        explicit_limit = limit is not None
        huge = size > ctx.tool_limits.max_file_bytes
        if huge and not explicit_limit:
            raise ToolError(
                f"{display(ctx.workspace, target)} 约 {size} 字节，超过 "
                f"{ctx.tool_limits.max_file_bytes} 字节上限，请用 offset/limit 分段读取。"
            )

        offset = max(1, int(offset))
        max_take = max(1, int(limit)) if explicit_limit else None
        all_lines, total, hit_eof = _read_lines(target, offset, max_take, count_all=not huge)

        if total == 0:
            ctx.workspace_access.mark_read(target)
            return ToolRunResult.success(f"{display(ctx.workspace, target)} 文件存在，但内容为空。")

        if offset > total:
            suffix = f"共 {total} 行，" if hit_eof else ""
            return ToolRunResult.success(
                f"{display(ctx.workspace, target)} {suffix}第 {offset} 行起没有内容。"
            )

        requested_end = offset - 1 + len(all_lines)
        budget = ctx.tool_limits.max_file_read_chars
        body, shown_to = _render_lines(all_lines, offset, requested_end, budget)
        truncated_by_budget = shown_to < requested_end

        if truncated_by_budget and explicit_limit:
            raise ToolError(
                f"offset={offset} limit={limit} 请求的内容超过单次读取上限（约 {budget} 字符），"
                "请调小 limit 分段读取。"
            )

        entire_file_shown = (
            offset == 1 and hit_eof and shown_to >= total and not truncated_by_budget
        )
        if entire_file_shown:
            ctx.workspace_access.mark_read(target)

        if hit_eof:
            header = f"{display(ctx.workspace, target)}（共 {total} 行，当前展示 {offset}-{shown_to} 行）"
        else:
            header = f"{display(ctx.workspace, target)}（当前展示 {offset}-{shown_to} 行，其后未读取）"
        if truncated_by_budget:
            header += (
                f"\n[PARTIAL VIEW] 受单次读取上限限制，未能展示到第 {requested_end} 行，"
                f"如需继续请传 offset={shown_to + 1}"
            )
        elif hit_eof and shown_to < total:
            header += f"\n... 还有 {total - shown_to} 行未显示"

        preview_count = min(len(all_lines), shown_to - offset + 1, _PREVIEW_MAX_LINES)
        preview = "\n".join(all_lines[:preview_count]) if preview_count > 0 else ""

        return ToolRunResult.success(
            f"{header}\n{body}",
            total_lines=total,
            partial=truncated_by_budget,
            path=display(ctx.workspace, target),
            preview=preview,
            preview_start=offset,
        )


def _read_lines(
    target, offset: int, max_take: int | None, *, count_all: bool
) -> tuple[list[str], int, bool]:
    """按行读取。``count_all`` 时扫完全文以便报总行数；否则只取请求窗口。"""
    collected: list[str] = []
    line_no = 0
    hit_eof = True
    with target.open("rb") as raw:
        sample = raw.read(8192)
        if b"\x00" in sample:
            raise ToolError(f"{target.name} 看起来是二进制文件，无法以文本读取")
        raw.seek(0)
        wrapper = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        for line in wrapper:
            line_no += 1
            if line_no < offset:
                continue
            if max_take is not None and len(collected) >= max_take:
                if count_all:
                    continue
                hit_eof = False
                break
            collected.append(line.rstrip("\r\n"))
        else:
            hit_eof = True
    total = line_no if hit_eof or count_all else offset - 1 + len(collected)
    return collected, total, hit_eof


def _render_lines(lines: list[str], offset: int, end: int, budget: int) -> tuple[str, int]:
    """渲染已取出的行（lines[0] 对应 offset），受字符预算限制。"""
    width = len(str(end))
    parts: list[str] = []
    size = 0
    shown_to = offset - 1
    for index, line in enumerate(lines):
        line_no = offset + index
        if line_no > end:
            break
        rendered = f"{line_no:>{width}} | {line}"
        added = len(rendered) + 1
        if parts and size + added > budget:
            break
        parts.append(rendered)
        size += added
        shown_to = line_no
    return "\n".join(parts), shown_to
