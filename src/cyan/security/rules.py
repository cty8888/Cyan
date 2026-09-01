"""执行层二次拦截：转调硬地板 + 内置规则。

权限判定走 PermissionManager + RuleSet。这里给 bash / write_file / grep / glob
在绕过权限层直接 execute 时再拦一次。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..errors import SecurityError
from .floor import floor_deny_reason
from .policy import RuleSet


@lru_cache(maxsize=1)
def _builtin() -> RuleSet:
    """只加载 defaults.json，不读用户/项目文件。"""
    return RuleSet.builtin()


def blocked_command(
    command: str, *, workspace: Path | None = None, cwd: Path | None = None
) -> str | None:
    """硬地板，或内置 deny 的 bash 规则。关键删除在权限层询问，不在这里拒绝。"""
    floor = floor_deny_reason(command, workspace=workspace, cwd=cwd)
    if floor:
        return floor
    return _builtin().bash_denied(command)


def restricted_command(command: str) -> str | None:
    """兼容旧导入。bash 的强硬限制已并入内置 deny，由 ``blocked_command`` 覆盖。"""
    return None


def reject_restricted_write(relative_path: str) -> None:
    """执行层二次拦截：命中内置 write deny（如 ``.git/``）时抛 ``SecurityError``。"""
    reason = restricted_path(relative_path)
    if reason:
        raise SecurityError(reason)


def restricted_path(relative_path: str) -> str | None:
    """路径命中内置 write deny，或 read deny（连带挡写）。"""
    return _builtin().path_write_denied(relative_path)


def read_denied_path(relative_path: str) -> str | None:
    """路径命中内置 read deny。"""
    return _builtin().path_read_denied(relative_path)


def write_confirm_path(relative_path: str) -> str | None:
    """bash 写触点命中内置 write/read ask（受保护路径、密钥）。"""
    return _builtin().path_write_needs_confirm(relative_path)


def sensitive_path(relative_path: str) -> str | None:
    """路径命中内置 read 的 ask/deny（密钥、.env 等）；write 保护名单不算。"""
    return _builtin().path_is_sensitive(relative_path)
