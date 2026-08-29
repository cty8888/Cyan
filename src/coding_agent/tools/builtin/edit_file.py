"""edit_file —— 精确字符串替换，修改已有文件的首选方式。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...errors import InvalidToolArgumentsError, ToolError
from ...security.paths import display, resolve_path
from ...security.rules import reject_restricted_write
from ..base import Tool
from ..diff import unified_diff
from ..types import RiskLevel, ToolCapability, ToolContext, ToolRunResult

if TYPE_CHECKING:
    from ...session import WorkspaceAccess

EDIT_FILE_NAME = "edit_file"
EDIT_FILE_DESCRIPTION = (
    "通过精确字符串替换修改文件的局部内容, 比整文件重写更省 token, 是修改已有文件的首选方式. "
    "old_string 必须与文件中的内容逐字符完全一致 (含缩进), "
    "且在文件中唯一——如果不唯一, 请多带几行上下文使其唯一, 或设置 replace_all=true. "
    "编辑前必须先用 read_file 读过该文件, 否则会被拒绝."
)
EDIT_FILE_DEFAULT_REPLACE_ALL = False
EDIT_FILE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径, 相对于工作目录"},
        "old_string": {"type": "string", "description": "要被替换的原文, 必须完全匹配且唯一"},
        "new_string": {"type": "string", "description": "替换后的新内容, 留空表示删除"},
        "replace_all": {
            "type": "boolean",
            "description": "是否替换所有匹配项, 默认 false (要求唯一匹配)",
            "default": EDIT_FILE_DEFAULT_REPLACE_ALL,
        },
    },
    "required": ["path", "old_string", "new_string"],
}


class EditFileTool(Tool):
    name = EDIT_FILE_NAME
    description = EDIT_FILE_DESCRIPTION
    capability = ToolCapability.WRITE
    risk = RiskLevel.MEDIUM
    parameters = EDIT_FILE_PARAMETERS

    def describe(
        self,
        args: dict[str, Any],
        workspace: Path,
        workspace_access: WorkspaceAccess | None = None,
    ) -> tuple[str, str | None, str]:
        raw_path = str(args.get("path", ""))
        try:
            target, original, updated, occurrences = _prepare_edit(
                workspace,
                raw_path,
                str(args.get("old_string", "")),
                str(args.get("new_string", "")),
                bool(args.get("replace_all")),
                workspace_access=workspace_access,
            )
        except (ToolError, InvalidToolArgumentsError) as exc:
            return f"无法编辑 {raw_path}", str(exc), "text"
        except Exception:
            return f"编辑 {raw_path}", None, "text"
        return (
            f"编辑文件 {display(workspace, target)}（替换 {occurrences} 处）",
            unified_diff(original, updated, display(workspace, target)),
            "diff",
        )

    def run(
        self,
        ctx: ToolContext,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = EDIT_FILE_DEFAULT_REPLACE_ALL,
    ) -> ToolRunResult:
        target, original, updated, occurrences = _prepare_edit(
            ctx.workspace,
            path,
            old_string,
            new_string,
            replace_all,
            workspace_access=ctx.workspace_access,
        )
        target.write_text(updated, encoding="utf-8")
        ctx.workspace_access.mark_read(target)
        ctx.workspace_access.mark_modified(target)

        return ToolRunResult.success(
            f"已修改 {display(ctx.workspace, target)}（替换 {occurrences} 处）",
            diff=unified_diff(original, updated, display(ctx.workspace, target)),
        )


def _prepare_edit(
    workspace: Path,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    *,
    workspace_access: WorkspaceAccess | None,
) -> tuple[Path, str, str, int]:
    """读原文、校验前置条件、算出替换结果。describe 与 run 共用，避免预览和执行分叉。"""
    target = resolve_path(workspace, path, must_exist=True)
    if not target.is_file():
        raise ToolError(f"{display(workspace, target)} 不是文件")
    reject_restricted_write(display(workspace, target))
    if workspace_access is not None and not workspace_access.has_read(target):
        raise ToolError(
            f"编辑 {display(workspace, target)} 之前必须先用 read_file 读取它的当前内容。"
        )
    if old_string == new_string:
        raise InvalidToolArgumentsError("old_string 与 new_string 相同，无需编辑")

    original = target.read_text(encoding="utf-8")
    occurrences = original.count(old_string)

    if occurrences == 0:
        raise ToolError(
            f"在 {display(workspace, target)} 中找不到 old_string。"
            "请先用 read_file 确认原文（注意缩进、空格和换行必须逐字符一致）。"
        )
    if occurrences > 1 and not replace_all:
        raise ToolError(
            f"old_string 在 {display(workspace, target)} 中出现了 {occurrences} 次，不唯一。"
            "请补充上下文让它唯一，或设置 replace_all=true 替换全部。"
        )

    updated = original.replace(old_string, new_string, -1 if replace_all else 1)
    return target, original, updated, occurrences
