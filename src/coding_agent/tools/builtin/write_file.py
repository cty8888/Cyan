"""write_file —— 整文件写入或新建。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...errors import ToolError
from ...security.paths import display, resolve_path
from ...security.rules import reject_restricted_write
from ..base import Tool
from ..diff import unified_diff
from ..types import RiskLevel, ToolCapability, ToolContext, ToolRunResult

if TYPE_CHECKING:
    from ...session import WorkspaceAccess

WRITE_FILE_NAME = "write_file"
WRITE_FILE_DESCRIPTION = (
    "把内容整体写入文件, 文件不存在则创建 (父目录会自动创建). "
    "修改已有文件的局部内容时应优先使用 edit_file, 只有新建文件或整体重写时才用本工具. "
    "覆写已存在的文件之前必须先用 read_file 读过它, 否则会被拒绝."
)
WRITE_FILE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件路径, 相对于工作目录"},
        "content": {"type": "string", "description": "写入的完整内容"},
    },
    "required": ["path", "content"],
}


class WriteFileTool(Tool):
    name = WRITE_FILE_NAME
    description = WRITE_FILE_DESCRIPTION
    capability = ToolCapability.WRITE
    risk = RiskLevel.MEDIUM
    parameters = WRITE_FILE_PARAMETERS

    def describe(
        self,
        args: dict[str, Any],
        workspace: Path,
        workspace_access: WorkspaceAccess | None = None,
    ) -> tuple[str, str | None, str]:
        """审批预览：摘要说明新建还是覆写，详情是 unified diff。"""
        raw_path = str(args.get("path", ""))
        try:
            target, existed, old_content = _prepare_write(
                workspace, raw_path, workspace_access=workspace_access
            )
        except ToolError as exc:
            return f"无法写入 {raw_path}", str(exc), "text"
        except Exception:
            return f"写入 {raw_path}", None, "text"

        new_content = str(args.get("content", ""))
        action = "覆写" if existed else "新建"
        return (
            f"{action}文件 {display(workspace, target)}",
            unified_diff(old_content, new_content, display(workspace, target)),
            "diff",
        )

    def run(self, ctx: ToolContext, path: str, content: str) -> ToolRunResult:
        target, existed, old_content = _prepare_write(
            ctx.workspace, path, workspace_access=ctx.workspace_access
        )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        ctx.workspace_access.mark_read(target)
        ctx.workspace_access.mark_modified(target)

        line_count = len(content.splitlines())
        action = "覆写" if existed else "创建"
        return ToolRunResult.success(
            f"已{action} {display(ctx.workspace, target)}（{line_count} 行）",
            diff=unified_diff(old_content, content, display(ctx.workspace, target)),
        )


def _prepare_write(
    workspace: Path,
    path: str,
    *,
    workspace_access: WorkspaceAccess | None,
) -> tuple[Path, bool, str]:
    """解析目标、读旧内容，并做与 run 相同的前置检查。"""
    target, existed, old_content = _snapshot(workspace, path)
    reject_restricted_write(display(workspace, target))
    if existed and workspace_access is not None and not workspace_access.has_read(target):
        raise ToolError(
            f"{display(workspace, target)} 已存在，但本会话中还没有读取过它。"
            "为避免覆盖未知内容，请先用 read_file 确认当前内容，再决定是否整体覆写。"
        )
    return target, existed, old_content


def _snapshot(workspace: Path, path: str) -> tuple[Path, bool, str]:
    """解析目标路径并读出当前内容，供 describe / run 共用。"""
    target = resolve_path(workspace, path)
    existed = target.is_file()
    old_content = target.read_text(encoding="utf-8", errors="replace") if existed else ""
    return target, existed, old_content
