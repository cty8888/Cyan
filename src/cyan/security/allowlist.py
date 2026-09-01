"""本会话「始终允许」白名单：按同类操作记键，而不是整个工具名。

- 写入：``write:{目录}``。根目录文件为 ``write:.``，只放行根下其它文件，不含子目录；
  ``write:pkg`` 放行 ``pkg/`` 及其子目录。
- 执行：``exec:{命令名}``（如 ``exec:pytest``、``exec:git status``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tools.types import ToolCapability
from .paths import write_target_display
from .shell import command_head, split_command_segments

if TYPE_CHECKING:
    from ..tools.base import Tool

WRITE_SCOPE_PREFIX = "write:"
EXEC_SCOPE_PREFIX = "exec:"
MAX_PERSISTED_COMMANDS = 5


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
        keys = _exec_keys(str(args.get("command") or ""))
        return bool(keys) and all(key in always_allowed for key in keys)
    return tool.name in always_allowed


def remember(workspace: Path, tool: Tool, args: dict[str, Any], always_allowed: set[str]) -> None:
    """把本次操作的范围键写入会话白名单。复合命令按段各记一条，最多 5 条。"""
    always_allowed.update(always_keys(workspace, tool, args))


def persistable_allow_rules(workspace: Path, tool: Tool, args: dict[str, Any]) -> list[str]:
    """bash 的「始终允许」写成 ``Bash(pytest *)``；写入不落盘。复合命令每段一条，最多 5 条。"""
    if tool.capability is not ToolCapability.EXEC:
        return []
    rules: list[str] = []
    for key in always_keys(workspace, tool, args):
        if not key.startswith(EXEC_SCOPE_PREFIX):
            continue
        head = key[len(EXEC_SCOPE_PREFIX) :]
        if head:
            rules.append(f"Bash({head} *)")
    return rules


def always_keys(workspace: Path, tool: Tool, args: dict[str, Any]) -> list[str]:
    """生成白名单键。复合命令按段收集，最多 ``MAX_PERSISTED_COMMANDS`` 条。"""
    if tool.capability is ToolCapability.WRITE:
        scope = write_dir_scope(workspace, args)
        return [f"{WRITE_SCOPE_PREFIX}{scope}"] if scope is not None else []
    if tool.capability is ToolCapability.EXEC:
        return _persistable_exec_keys(str(args.get("command") or ""))
    return [tool.name]


def _exec_segment_key(segment: str) -> str | None:
    """一段命令的白名单键：有命令头就能记。"""
    head = command_head(segment)
    if not head:
        return None
    return f"{EXEC_SCOPE_PREFIX}{head}"


def _exec_keys(command: str) -> list[str] | None:
    """复合命令每一段的白名单键。任一段无法归类则整串都不能靠白名单放行。"""
    segments = split_command_segments(command)
    if not segments:
        return None
    keys: list[str] = []
    for segment in segments:
        key = _exec_segment_key(segment)
        if key is None:
            return None
        keys.append(key)
    return keys


def _persistable_exec_keys(command: str) -> list[str]:
    """可写入始终允许的段。最多 5 条。"""
    segments = split_command_segments(command)
    if not segments:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for segment in segments:
        key = _exec_segment_key(segment)
        if key is None or key in seen:
            continue
        seen.add(key)
        keys.append(key)
        if len(keys) >= MAX_PERSISTED_COMMANDS:
            break
    return keys


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
        keys = always_keys(workspace, tool, args)
        if not keys:
            return None
        heads = [key[len(EXEC_SCOPE_PREFIX) :] for key in keys]
        if len(heads) == 1:
            return f"{heads[0]} 命令"
        return "、".join(heads) + " 命令"
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
