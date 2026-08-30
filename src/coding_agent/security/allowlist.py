"""本会话「始终允许」白名单：按同类操作记键，而不是整个工具名。

- 写入：``write:{目录}``。根目录文件为 ``write:.``，只放行根下其它文件，不含子目录；
  ``write:pkg`` 放行 ``pkg/`` 及其子目录。
- 执行：``exec:{命令名}``（如 ``exec:pytest``）。``python -m pytest`` 视为一个命令名。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tools.types import ToolCapability
from .paths import write_target_display
from .shell import command_head

if TYPE_CHECKING:
    from ..tools.base import Tool

WRITE_SCOPE_PREFIX = "write:"
EXEC_SCOPE_PREFIX = "exec:"


def is_always_allowed(
    workspace: Path, tool: Tool, args: dict[str, Any], always_allowed: set[str]
) -> bool:
    """白名单是否覆盖这次操作。"""
    if tool.capability is ToolCapability.WRITE:
        current = write_dir_scope(workspace, args)
        if current is None:
            return False
        for key in always_allowed:
            if key.startswith(WRITE_SCOPE_PREFIX) and _write_dir_matches(
                key[len(WRITE_SCOPE_PREFIX) :], current
            ):
                return True
        return False
    if tool.capability is ToolCapability.EXEC:
        head = command_head(str(args.get("command") or ""))
        return bool(head) and f"{EXEC_SCOPE_PREFIX}{head}" in always_allowed
    return tool.name in always_allowed


def remember(workspace: Path, tool: Tool, args: dict[str, Any], always_allowed: set[str]) -> None:
    """把本次操作的范围键写入会话白名单。"""
    key = always_key(workspace, tool, args)
    if key:
        always_allowed.add(key)


def always_key(workspace: Path, tool: Tool, args: dict[str, Any]) -> str | None:
    """生成白名单键：``write:{目录}`` / ``exec:{命令名}``；无法归类时退回工具名。"""
    if tool.capability is ToolCapability.WRITE:
        scope = write_dir_scope(workspace, args)
        return f"{WRITE_SCOPE_PREFIX}{scope}" if scope is not None else None
    if tool.capability is ToolCapability.EXEC:
        head = command_head(str(args.get("command") or ""))
        return f"{EXEC_SCOPE_PREFIX}{head}" if head else None
    return tool.name


def always_label(workspace: Path, tool: Tool, args: dict[str, Any]) -> str | None:
    """审批面板上「始终允许」对应的范围说明。"""
    if tool.capability is ToolCapability.WRITE:
        scope = write_dir_scope(workspace, args)
        if scope is None:
            return None
        if scope == ".":
            return "工作目录根下的写入"
        return f"{scope}/ 下的写入"
    if tool.capability is ToolCapability.EXEC:
        head = command_head(str(args.get("command") or ""))
        return f"{head} 命令" if head else None
    return None


def write_dir_scope(workspace: Path, args: dict[str, Any]) -> str | None:
    """写入目标所在目录（相对工作区）。根目录文件记为 ``.``。"""
    target = write_target_display(workspace, args)
    if target is None:
        return None
    text = target.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text or text == ".":
        return "."
    if "/" not in text:
        return "."
    return text.rsplit("/", 1)[0]


def _write_dir_matches(allowed_dir: str, current_dir: str) -> bool:
    """``write:.`` 只匹配根目录文件；``write:pkg`` 匹配该目录及其子目录。"""
    if allowed_dir == ".":
        return current_dir == "."
    return current_dir == allowed_dir or current_dir.startswith(allowed_dir + "/")
