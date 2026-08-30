"""从 shell 命令里抽出能看清的路径，按文件工具同一套规则判定。

解析不到的（python -c、命令替换、``$VAR``）标成 opaque，由权限层强制确认，
不在这里假装能拦住。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import PathOutsideWorkspaceError, SecurityError
from . import rules
from .messages import ENV_DUMP_MSG, OPAQUE_EXEC_MSG
from .paths import display, resolve_path
from .shell import split_command_segments

_REDIRECT = re.compile(r"(?<!&)(?:\d*)(>>?|<<?)(?!&)\s*([^\s|&;<>]+)")
_UNRESOLVED = re.compile(r"^\$|/\$\{|\$\{")

_WRITE_ALL = frozenset(
    {"rm", "rmdir", "mkdir", "touch", "unlink", "chmod", "chown", "chgrp", "tee", "truncate"}
)
_WRITE_LAST = frozenset({"cp", "mv", "ln", "install"})
_READ_ARGS = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "file",
        "stat",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "nl",
        "sort",
        "uniq",
        "cut",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "diff",
        "realpath",
        "readlink",
    }
)
_VALUE_FLAGS = frozenset(
    {"-n", "-c", "-C", "-o", "-O", "-f", "-d", "-t", "-w", "-I", "--file", "--directory"}
)
_OPAQUE_PATTERNS = (
    re.compile(r"`"),
    re.compile(r"\$\("),
    re.compile(r"\beval\b"),
    re.compile(r"\b(python|python3)\b[^\n]*\s-c\b"),
    re.compile(r"\b(node|nodejs)\b[^\n]*\s-e\b"),
    re.compile(r"\b(ruby|perl)\b[^\n]*\s-e\b"),
    re.compile(r">\s*\$"),
)


@dataclass(frozen=True)
class PathTouch:
    raw: str
    kind: str  # "read" | "write"


@dataclass
class CommandPathAnalysis:
    touches: list[PathTouch] = field(default_factory=list)
    opaque: bool = False
    dumps_env: bool = False


def analyze_command(command: str) -> CommandPathAnalysis:
    """抽出路径、是否不透明、是否会倾倒环境变量。"""
    analysis = CommandPathAnalysis(opaque=_is_opaque(command))
    for segment in split_command_segments(command):
        _analyze_segment(segment, analysis)
    return analysis


def outside_workspace_reason(workspace: Path, command: str, cwd: Path | None = None) -> str | None:
    """命令里能看清的路径若落在工作区外，返回拒绝理由。"""
    return _outside_with_cwd_tracking(workspace, command, cwd or workspace)


def restricted_write_reason(workspace: Path, command: str, cwd: Path | None = None) -> str | None:
    """写入目标命中 Restricted 路径（如 ``.git/``）时返回理由。"""
    for relative, kind in _resolved_touches(workspace, command, cwd or workspace):
        if kind == "write":
            reason = rules.restricted_path(relative)
            if reason:
                return reason
    return None


def forced_exec_reason(workspace: Path, command: str, cwd: Path | None = None) -> str | None:
    """敏感路径、倾倒环境、或不透明命令：必须逐次确认。"""
    analysis = analyze_command(command)
    if analysis.opaque:
        return OPAQUE_EXEC_MSG
    if analysis.dumps_env:
        return ENV_DUMP_MSG
    for relative, kind in _resolved_touches(workspace, command, cwd or workspace):
        reason = rules.sensitive_path(relative)
        if reason:
            if kind == "read":
                return f"{relative} 可能包含密钥 / 凭据，读取也需要确认。"
            return reason
    return None


def reject_unsafe_paths(workspace: Path, command: str, cwd: Path | None = None) -> None:
    """执行层二次拦截：区外路径与 Restricted 写入。"""
    origin = cwd or workspace
    outside = outside_workspace_reason(workspace, command, origin)
    if outside:
        raise PathOutsideWorkspaceError(outside)
    restricted = restricted_write_reason(workspace, command, origin)
    if restricted:
        raise SecurityError(restricted)


def _outside_with_cwd_tracking(workspace: Path, command: str, start: Path) -> str | None:
    cwd = start
    for segment in split_command_segments(command):
        for raw, _kind in _segment_touches(segment):
            if _unresolved(raw):
                continue
            try:
                resolved = resolve_path(workspace, raw, base=cwd)
            except PathOutsideWorkspaceError as exc:
                return str(exc)
            except Exception:
                continue
            if _segment_is_cd(segment):
                cwd = resolved
    return None


def _resolved_touches(workspace: Path, command: str, start: Path) -> list[tuple[str, str]]:
    cwd = start
    found: list[tuple[str, str]] = []
    for segment in split_command_segments(command):
        for raw, kind in _segment_touches(segment):
            if _unresolved(raw):
                continue
            try:
                resolved = resolve_path(workspace, raw, base=cwd)
            except PathOutsideWorkspaceError:
                continue
            except Exception:
                continue
            found.append((display(workspace, resolved), kind))
            if _segment_is_cd(segment):
                cwd = resolved
    return found


def _analyze_segment(segment: str, analysis: CommandPathAnalysis) -> None:
    for raw, kind in _segment_touches(segment):
        analysis.touches.append(PathTouch(raw=raw, kind=kind))
    tokens = _tokenize(segment)
    if tokens and tokens[0] in {"printenv", "env"} and _is_bare_env(tokens):
        analysis.dumps_env = True


def _segment_touches(segment: str) -> list[tuple[str, str]]:
    touches: list[tuple[str, str]] = []
    for match in _REDIRECT.finditer(segment):
        op, target = match.group(1), match.group(2)
        if target.startswith("&"):
            continue
        kind = "write" if ">" in op else "read"
        touches.append((target, kind))

    tokens = _tokenize(segment)
    if not tokens:
        return touches
    head, *rest = tokens
    if head == "cd":
        target = rest[-1] if rest else str(Path.home())
        touches.append((target, "read"))
        return touches

    paths = _path_args(tokens)
    if head in _WRITE_ALL:
        touches.extend((path, "write") for path in paths)
    elif head in _WRITE_LAST and paths:
        touches.extend((path, "read") for path in paths[:-1])
        touches.append((paths[-1], "write"))
    elif head in _READ_ARGS:
        touches.extend((path, "read") for path in paths)
    return touches


def _path_args(tokens: list[str]) -> list[str]:
    paths: list[str] = []
    skip_next = False
    for index, token in enumerate(tokens[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            paths.extend(tokens[index + 1 :])
            break
        if token in _VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith("-") or re.fullmatch(r"[0-7]{3,4}", token):
            continue
        paths.append(token)
    return paths


def _tokenize(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _is_bare_env(tokens: list[str]) -> bool:
    if tokens[0] == "printenv":
        return True
    if tokens[0] != "env":
        return False
    rest = tokens[1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            return not rest[i + 1 :]
        if arg in {"-u", "--unset", "-C", "--chdir"}:
            i += 2
            continue
        if arg.startswith("-") and not arg.startswith("-="):
            i += 1
            continue
        if "=" in arg:
            i += 1
            continue
        return False
    return True


def _is_opaque(command: str) -> bool:
    return any(pattern.search(command) for pattern in _OPAQUE_PATTERNS)


def _unresolved(raw: str) -> bool:
    return bool(_UNRESOLVED.search(raw))


def _segment_is_cd(segment: str) -> bool:
    tokens = _tokenize(segment)
    return bool(tokens) and tokens[0] == "cd"
