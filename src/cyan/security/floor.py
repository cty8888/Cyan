"""关键路径删除：allow 不能预先批准，但用户可以当场确认。

工作区沙箱仍由路径层负责。curl|sh、写块设备、fork bomb 走普通执行审批，不再做不可审批熔断。
"""

from __future__ import annotations

import re
from pathlib import Path

from .shell import (
    executable_name,
    iter_command_substitutions,
    peel_leading_assignments,
    split_command_segments,
    tokenize,
    unwrap_argv,
)

_HOME_ROOTS = frozenset({"~", "$HOME", "${HOME}"})
_DOT_TARGETS = frozenset({".", "..", "*"})
_VAR_GLOB = re.compile(
    r"^(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*)/(?:\*\*?)?$"
)
_BARE_VAR = re.compile(r"^(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*)$")
_VAR_FOLLOW = frozenset({"/*", "*", "/**", "/"})
_HOME_GLOBS = frozenset({"~/*", "~/**", "$HOME/*", "${HOME}/*", "$HOME/**", "${HOME}/**"})
_CRITICAL_RM_MSG = (
    "检测到可能删除根目录、顶级目录、家目录、工作区或其父目录的 rm/rmdir，需要确认；"
    "allow 不能预先批准。"
)


def floor_deny_reason(
    command: str, *, workspace: Path | None = None, cwd: Path | None = None
) -> str | None:
    """已无不可审批的结构熔断。关键删除见 ``critical_rm_reason``。"""
    return None


def critical_rm_reason(
    command: str, *, workspace: Path | None = None, cwd: Path | None = None
) -> str | None:
    """命中关键路径删除时返回确认理由；否则 ``None``。"""
    if _is_critical_rm(command, workspace=workspace, cwd=cwd):
        return _CRITICAL_RM_MSG
    return None


def _is_critical_rm(
    command: str, *, workspace: Path | None = None, cwd: Path | None = None
) -> bool:
    """``rm`` / ``rmdir`` 打到 ``/``、顶级目录、家目录、``.``、工作区或其祖先。

    命令替换 / 进程替换里的删除同样检查：``echo "$(rm -rf ~)"`` 不能靠外层只读逃掉。
    """
    origin = cwd if cwd is not None else workspace
    for text in (command, *iter_command_substitutions(command)):
        for segment in split_command_segments(text) or [text]:
            if _segment_is_critical_rm(segment, workspace=workspace, cwd=origin):
                return True
    return False


def _segment_is_critical_rm(
    segment: str, *, workspace: Path | None, cwd: Path | None
) -> bool:
    tokens = peel_leading_assignments(unwrap_argv(tokenize(segment)).tokens, all_assignments=True)
    if not tokens:
        return False
    name = executable_name(tokens[0])
    if name == "rm":
        targets = _rm_targets(tokens)
    elif name == "rmdir":
        targets = _rmdir_targets(tokens)
    else:
        return False
    if _has_var_glob_target(targets):
        return True
    for target in targets:
        if _is_critical_rm_target(target, workspace=workspace, cwd=cwd):
            return True
    return False


def _rm_targets(tokens: list[str]) -> list[str]:
    targets: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            targets.extend(tokens[index + 1 :])
            break
        if token.startswith("-") and token != "-":
            index += 1
            continue
        targets.append(token)
        index += 1
    return targets


def _rmdir_targets(tokens: list[str]) -> list[str]:
    targets: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            targets.extend(tokens[index + 1 :])
            break
        if token.startswith("-") and token != "-":
            if token in {"-p", "--parents", "-v", "--verbose", "--ignore-fail-on-non-empty"}:
                index += 1
                continue
            if "=" in token:
                index += 1
                continue
            index += 1
            continue
        targets.append(token)
        index += 1
    return targets


def _has_var_glob_target(targets: list[str]) -> bool:
    """``$DIR/*`` / ``$DIR/`` / ``"$DIR" /*``：变量为空时会从文件系统根删。"""
    for index, target in enumerate(targets):
        stripped = target.strip("\"'")
        if stripped in _HOME_GLOBS or _VAR_GLOB.fullmatch(stripped):
            return True
        if _BARE_VAR.fullmatch(stripped) and index + 1 < len(targets):
            follow = targets[index + 1].strip("\"'")
            if follow in _VAR_FOLLOW:
                return True
    return False


def _is_critical_rm_target(
    target: str, *, workspace: Path | None = None, cwd: Path | None = None
) -> bool:
    stripped = _strip_rm_target(target)
    if stripped == "/" or target in {"/*"}:
        return True
    if stripped in _HOME_ROOTS or stripped in _DOT_TARGETS:
        return True
    resolved = _resolve_rm_target(target, cwd=cwd, workspace=workspace)
    if resolved is None:
        return False
    if resolved == Path("/"):
        return True
    if resolved.parent == Path("/"):
        return True
    try:
        if resolved == Path.home().resolve():
            return True
    except (OSError, RuntimeError):
        pass
    if workspace is None:
        return False
    try:
        root = Path(workspace).resolve()
        return resolved == root or resolved in root.parents
    except (OSError, RuntimeError, ValueError):
        return False


def _strip_rm_target(target: str) -> str:
    """去掉末尾 ``/``，但保留根目录本身。"""
    if target.startswith("/") and target.replace("/", "") == "":
        return "/"
    return target.rstrip("/") or target


def _resolve_rm_target(
    target: str, *, cwd: Path | None, workspace: Path | None
) -> Path | None:
    text = _expand_home_prefix(target)
    try:
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        base = Path(cwd).resolve() if cwd is not None else (
            Path(workspace).resolve() if workspace is not None else None
        )
        if base is None:
            return None
        return (base / candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _expand_home_prefix(target: str) -> str:
    """把 ``$HOME`` / ``${HOME}`` 收成 ``~``，交给 ``Path.expanduser``。"""
    stripped = _strip_rm_target(target)
    if stripped in {"$HOME", "${HOME}"}:
        return "~"
    if stripped.startswith("$HOME/"):
        return "~/" + stripped[len("$HOME/") :]
    if stripped.startswith("${HOME}/"):
        return "~/" + stripped[len("${HOME}/") :]
    return target
